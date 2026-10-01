#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""健康巡检.py —— 死人心跳巡检（v6.5 生产运维，2026-09-23）

职责：判定「守护链是否活着」——异常告警，正常完全静默。
数据源：watchdog 维护的 .heartbeat.json（每 60s 原子刷新）
       + .egress_ip.state.json（出口 IP 白名单核验记录，D 2026-09-30）。
告警通道：Telegram（经 .env 的 BINANCE_PROXY）→ 代理不可用则直连重试
         → 仍失败降级 QQ 邮件（SMTP 国内直连，代理挂掉时仍可达）。
去重：同一问题 30 分钟内只告警一次（.patrol_alert.state.json）。

设计约束（刻意为之）：
  ① 纯标准库——venv 包损坏 / pip 环境被改时仍能报警；
  ② 绝不触碰交易状态（只读心跳与锁文件，不写 trade_state）；
  ③ pythonw.exe 下 sys.stdout 为 None → 入口即做空值防护；
  ④ 自身故障绝不抛出（exit 0），只在日志留痕，避免计划任务刷"失败"；
  ⑤ 出口 IP 白名单巡检（D，2026-09-30；2026-10-01 复审修订）：经代理取当前出口 IP，
     未登记或过 6h TTL 时做一次**只读**签名探活（GET /fapi/v1/balance）判定是否仍被
     鉴权接受；不下单、不撤单、不改仓、不写 trade_state；**停机(stopped)期间照常执行**
     （它与守护链存活无关，而停机→重启正是启动探活撞 -2015 的高危窗口）。
     判定保守：取不到 IP / 网络失败 / -1021 / 非 balance 数组体一律只记日志绝不告警
     （代理抖动、本地时钟与异常响应体都不是白名单问题）；仅当响应体结构化 ``code`` 为
     -2015/-2014 时告警（按 code 判定、**不搜原始子串**，余额里含 -2015 的数字不会误报），
     且记入冷却期，冷却内不重复探活以免触发币安校验限流把真实拒绝信号稀释掉。

用法：
  健康巡检.py                 巡检一次（计划任务每 15 分钟调用）
  健康巡检.py --dry-run       只打印判定结果，不发任何网络请求
  健康巡检.py --wait-proxy N  等待本地代理就绪，最多 N 秒（开机自启用）
  健康巡检.py --notify-proxy-down  开机自启因代理未就绪放弃时的告警
"""
import json
import os
import socket
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

import email_gate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
HEARTBEAT_FILE = os.path.join(BASE_DIR, ".heartbeat.json")
LOCK_FILE = os.path.join(BASE_DIR, ".bot_instance.lock")
ALERT_STATE_FILE = os.path.join(BASE_DIR, ".patrol_alert.state.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "patrol.log")

MAX_AGE_SECONDS = int(os.getenv("PATROL_MAX_AGE_SECONDS", "300"))  # 心跳陈旧阈值
ALERT_DEDUP_SECONDS = 1800                                         # 同问题去重窗口
BOT_DEAD_THRESHOLD = 2        # 连续 N 次巡检 bot 不存活才告警（滤掉重启瞬窗）
# 业务活性阈值：控制循环/活跃批次进度超过此值即视为冻结
PROGRESS_STALL_SECONDS = int(os.getenv("PATROL_PROGRESS_STALL_SECONDS", "900"))
HEALTH_DIR = os.path.join(BASE_DIR, ".bot_health")
TRADE_STATE_FILE = os.path.join(BASE_DIR, "trade_state.json")

# ==================== 出口 IP 白名单巡检（D，2026-09-30）====================
# 背景：代理（FlClash 等）会在多个出口节点间轮换；币安 API key 若启用 IP 白名单，
# 新出口的签名请求直接返回 -2015，而生产代码 **单次** -2015 即进入盲区安全模式
# （AUTH_BLOCKED：全部 Binance API 停摆，需人工 /auth_reset）——2026-09-30 21:28 实盘发生过。
# 既有 `_check_ip_periodically` 只在「有活跃批次的监控循环」内运行 → 空仓期无人巡检；
# 故把该检查放进本进程：每 15 分钟一轮，空仓同样生效。
EGRESS_STATE_FILE = os.path.join(BASE_DIR, ".egress_ip.state.json")

# 环境变量解析：导入期**绝不**抛异常（设计约束④）——.env 写了非整数/非布尔也必须让巡检照常跑。
# 注意此段位于 log() 定义之前，不能直接调 log()：解析问题先入队，由 main() 首轮打日志。
_EGRESS_ENV_ERRORS: list = []
_EGRESS_TRUE = ("1", "true", "yes", "on")
_EGRESS_FALSE = ("0", "false", "no", "off")


def _egress_env_int(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _EGRESS_ENV_ERRORS.append(f"{name}={raw!r} 不是整数，已回退默认 {default}")
        return default


def _egress_env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in _EGRESS_TRUE:
        return True
    if raw in _EGRESS_FALSE:
        return False
    _EGRESS_ENV_ERRORS.append(f"{name}={raw!r} 不是布尔值(true/false)，已回退默认 {default}")
    return default


EGRESS_CHECK_ENABLED = _egress_env_flag("EGRESS_IP_CHECK", True)
# 同一出口 IP 每 6h 复检一次（防「白名单后来被移除」这类反向变化），其余轮次零币安签名调用
EGRESS_VERIFY_TTL_SECONDS = _egress_env_int("EGRESS_IP_VERIFY_TTL_SECONDS", 21600)
# P1（2026-10-01 复审）：被拒后的冷却窗口。冷却期内**不再重复发失败签名请求**——
# 币安对反复无效签名/IP 有限流（429/418），会把真实的拒绝信号稀释成"未完成"日志；
# 但 issues 仍每轮重发，由 alert 的 30min 去重控制实际告警频率。
EGRESS_REJECT_COOLDOWN_SECONDS = _egress_env_int("EGRESS_REJECT_COOLDOWN_SECONDS", 3600)
EGRESS_STATE_MAX_ENTRIES = 32          # verified/rejected 各自封顶，防状态文件无限增长
EGRESS_ALERT_KEEP_SECONDS = 7 * 86400   # .patrol_alert.state.json 内 egress 键的保留期
EGRESS_IP_TIMEOUT = _egress_env_int("EGRESS_IP_TIMEOUT", 5)    # 对齐 bot 侧 _get_public_ip 的 5s
EGRESS_PROBE_TIMEOUT = _egress_env_int("EGRESS_PROBE_TIMEOUT", 12)

# pythonw.exe 场景：stdout/stderr 为 None，print 会抛 AttributeError
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")


# ==================== 基础工具 ====================
def log(msg: str):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
        print(line, end="")
    except Exception:
        pass


def load_env() -> dict:
    env = {}
    try:
        with open(ENV_FILE, encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception as e:
        log(f"⚠️ .env 读取失败: {e}")
    return env


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def proxy_hostport(proxy_url: str):
    try:
        p = urlparse(proxy_url if "//" in proxy_url else f"http://{proxy_url}")
        return (p.hostname or "127.0.0.1"), int(p.port or 7890)
    except Exception:
        return ("127.0.0.1", 7890)


def proxy_ready(proxy_url: str, timeout: float = 2.0) -> bool:
    host, port = proxy_hostport(proxy_url)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def wait_proxy(proxy_url: str, seconds: int) -> bool:
    host, port = proxy_hostport(proxy_url)
    deadline = time.time() + seconds
    log(f"⏳ 等待本地代理 {host}:{port} 就绪（最多 {seconds}s）...")
    while time.time() < deadline:
        if proxy_ready(proxy_url):
            log(f"✅ 代理 {host}:{port} 已就绪")
            return True
        time.sleep(2)
    log(f"❌ 代理 {host}:{port} 在 {seconds}s 内未就绪")
    return False


def pid_alive(pid) -> bool:
    """Windows 下仅用 tasklist 判定（os.kill(pid,0) 在 Windows 会真杀进程，绝不可用）。"""
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                             capture_output=True, timeout=10).stdout
        return str(int(pid)).encode() in out
    except Exception:
        return False


# ==================== 告警通道（TG 双路 → 邮件兜底） ====================
def send_tg(env: dict, text: str, use_proxy: bool) -> bool:
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_ALLOWED_USER_ID", "")
    if not (token and chat_id):
        log("⚠️ TG 未配置（TG_BOT_TOKEN/TG_ALLOWED_USER_ID），跳过")
        return False
    try:
        import urllib.parse
        import urllib.request
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = urllib.parse.urlencode({
            "chat_id": chat_id, "text": text, "disable_web_page_preview": "true",
        }).encode()
        proxy_url = env.get("BINANCE_PROXY", "") if use_proxy else ""
        if proxy_url:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        else:
            opener = urllib.request.build_opener()
        with opener.open(url, data=body, timeout=12) as resp:
            ok = 200 <= resp.status < 300
        log(f"{'✅' if ok else '⚠️'} TG 告警({'经代理' if proxy_url else '直连'})返回 {ok}")
        return ok
    except Exception as e:
        log(f"⚠️ TG 告警({'经代理' if use_proxy else '直连'})失败: {e}")
        return False


def send_email(env: dict, subject: str, text: str, event: str = "generic") -> bool:
    """QQ 邮件兜底：SMTP 国内直连，代理不可达时仍可用。

    R0（2026-09-25，F10 修复）：健康巡检告警传 event="health" → 豁免持仓闸门。
    修复前：巡检每次运行重读磁盘 .env，磁盘 EMAIL_ALERT_ONLY_WITH_POSITION=true 且
    空仓时，TG 双路全失败的最后一层邮件兜底会被静默吞掉（无持仓 ≠ 无风险）。
    """
    allowed, gate_reason = email_gate.should_send_email(
        env=env, state_path=TRADE_STATE_FILE, event=event
    )
    if not allowed:
        log(f"ℹ️ 邮件已跳过（{gate_reason}）")
        return False

    user = env.get("QQ_MAIL_USER", "")
    code = env.get("QQ_MAIL_AUTH_CODE", "")
    to = env.get("QQ_MAIL_TO", "") or user
    if not (user and code and to):
        log("⚠️ 邮件未配置（QQ_MAIL_USER/AUTH_CODE），跳过")
        return False
    try:
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"], msg["To"] = user, to
        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=15) as s:
            s.login(user, code)
            s.sendmail(user, [to], msg.as_string())
        log("✅ 邮件告警已发送")
        return True
    except Exception as e:
        log(f"⚠️ 邮件告警失败: {e}")
        return False


def alert(env: dict, key: str, title: str, detail: str, dry_run: bool) -> None:
    """去重后多渠道告警：TG（经代理 → 直连）→ 邮件。"""
    now = time.time()
    state = read_json(ALERT_STATE_FILE) or {}
    last = float(state.get(key, 0) or 0)
    if now - last < ALERT_DEDUP_SECONDS:
        log(f"🔇 [{key}] 去重窗口内已告警过，本次跳过（{int(now - last)}s 前）")
        return
    text = (f"🚨【资金安全】Bot 健康巡检异常\n{title}\n{detail}\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"💡 处理：查看 logs\\bot_YYYYMMDD.log 与 watchdog.log；"
            f"必要时双击 停止Bot-应急.bat 后重启 启动Bot.bat")
    if dry_run:
        log(f"[dry-run] 本应告警 [{key}]: {title} | {detail}")
        return
    sent = send_tg(env, text, use_proxy=True)
    if not sent:
        sent = send_tg(env, text, use_proxy=False)
    if not sent:
        # R0：巡检告警属资金安全事件，邮件兜底不受持仓闸门限制（空仓期同样必须可达）
        sent = send_email(env, "🚨 Bot 健康巡检异常", text, event="health")
    if sent:
        state[key] = now
        write_json(ALERT_STATE_FILE, state)
    else:
        log(f"❌ [{key}] 所有告警通道均失败（请人工检查机器与网络）")


# ==================== 出口 IP 白名单核验（D）====================
def _egress_http_get(url, proxy_url="", timeout=10, headers=None):
    """只读 GET。返回 (status, body)。

    **必须**把 urllib 的 HTTPError 转成 (status, body)：币安鉴权失败以 HTTP 4xx 返回，
    而 urllib 对 4xx/5xx 抛 HTTPError、不返回元组。若不在此捕获，probe_auth_for_ip 会把它
    当网络异常归为 error —— 真实 -2015 永远发不出 egress_ip_rejected 告警（2026-10-01
    复审 P0，本地 400 实证：verdict=error、issues=[]、告警未触发）。
    连接类失败（DNS/拒连/超时）仍向上抛，由调用方捕获并按"网络问题"处理。
    """
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    if proxy_url:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    else:
        handler = urllib.request.ProxyHandler({})          # 显式不走系统代理
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return int(code), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # HTTPError 本身是响应对象，body 必须读出来，否则拿不到币安错误码（-2015 等）
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return int(getattr(e, "code", 0) or 0), body


def _is_ip_address(value) -> bool:
    """严格校验 IP 字面量（2026-10-01 复审 P2）。

    只判长度 3-45 会把 HTML/JSON 首行（如 `<!DOCTYPE html>`、`{"ip": ...}`）当 IP，
    进而写进状态文件、污染登记集合。
    """
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not 3 <= len(value) <= 45:
        return False
    try:
        import ipaddress
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def get_egress_ip(env: dict, proxy_url: str = ""):
    """当前代理出口 IP；拿不到返回 None（只记日志，不告警）。"""
    attempts = ("https://api.ipify.org?format=json", "https://ifconfig.me/ip")
    last_err = None
    for url in attempts:
        try:
            status, body = _egress_http_get(url, proxy_url, EGRESS_IP_TIMEOUT)
            if not 200 <= status < 300:
                last_err = f"{url} → HTTP {status}"
                continue
            ip = ""
            try:
                ip = str(json.loads(body).get("ip", "")).strip()
            except Exception:
                ip = ""
            if not ip and body:
                ip = body.strip().splitlines()[0].strip()
            if _is_ip_address(ip):
                return ip
            last_err = f"{url} → 响应不是合法 IP: {body[:80]!r}"
        except Exception as e:
            last_err = f"{url} → {type(e).__name__}: {e}"
    log(f"ℹ️ 出口 IP 获取失败（本轮跳过白名单核验）: {last_err}")
    return None


# 基址独立成常量：测试可把它指向本地服务器，从而走**真实 urllib 路径**复现 4xx HTTPError
_FAPI_BASE = "https://fapi.binance.com"


def probe_auth_for_ip(env: dict, proxy_url: str = ""):
    """只读鉴权探活：当前出口 IP 下签名请求是否被币安接受。

    返回 (verdict, detail)，verdict ∈ {"ok", "rejected", "error", "skip"}。
    只读：GET /fapi/v1/balance（余额查询）——不下单、不撤单、不改仓。

    P2（2026-10-01 复审）：recvWindow 从 5s 放宽到 60s —— 本机时钟偏移 >5s 时
    Binance 回 -1021，原实现会落进 error 分支**永久静默**（不告警、不登记、每轮重探）。
    recvWindow 上限按官方建议取 60000。
    """
    key = env.get("BINANCE_API_KEY", "")
    secret = env.get("BINANCE_SECRET", "")
    if not (key and secret):
        return "skip", "未配置 BINANCE_API_KEY/BINANCE_SECRET"
    try:
        import hmac
        from urllib.parse import urlencode
        query = urlencode({"timestamp": int(time.time() * 1000), "recvWindow": 60000})
        signature = hmac.new(secret.encode(), query.encode(), "sha256").hexdigest()
        url = f"{_FAPI_BASE}/fapi/v1/balance?{query}&signature={signature}"
        status, body = _egress_http_get(url, proxy_url, EGRESS_PROBE_TIMEOUT,
                                        headers={"X-MBX-APIKEY": key})
        verdict = _classify_probe(status, body)
        if verdict == "ok":
            return "ok", f"HTTP {status}（出口 IP 鉴权探活通过）"
        if verdict == "rejected":
            return "rejected", f"HTTP {status}: {(body or '')[:300]}"
        return "error", f"HTTP {status}: {(body or '')[:200]}"
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"


_EGRESS_REJECT_CODES = (-2015, -2014)


def _classify_probe(status, body) -> str:
    """按**响应结构**给探活结果定性，返回 "ok" / "rejected" / "error"。

    2026-10-01 第三轮复审 P1：原先只看状态码 + 搜原始子串，两向都会错——
      · 余额 JSON 的 ``crossUnrealizedPnl=-20150.5`` 含 ``-2015`` 子串 → 正常余额被
        误判成「鉴权被拒」，出假告警并把该 IP 塞进 rejected 桶（本地实测复现）；
      · 2xx + 非 balance 体（错误对象/空体）→ 误判成 ok 并登记 6h，探活形同虚设。

    判定顺序：
      ① dict + ``code`` ∈ {-2015,-2014} → rejected（任何状态码，含 2xx 带错误体的历史形态）；
      ② 2xx + **JSON 数组** → ok（``/fapi/v1/balance`` 恒返回数组）；2xx 但体不是数组
         → error，**绝不登记**；
      ③ 非 2xx：结构化体只按 code 判（已判过即 error）；只有非 JSON 体才允许按拒绝
         特征兜底（HTTPS 强证书校验下不会是代理注入的页面）。
    """
    text = body or ""
    try:
        data = json.loads(text)
    except Exception:
        data = None
    code = None
    if isinstance(data, dict):
        try:
            code = int(data.get("code"))
        except (TypeError, ValueError):
            code = None
    if code in _EGRESS_REJECT_CODES:
        return "rejected"
    if 200 <= status < 300:
        return "ok" if isinstance(data, list) else "error"
    if data is None and _looks_auth_rejected(text):
        return "rejected"
    return "error"


def _looks_auth_rejected(body) -> bool:
    """非 JSON 响应体上的鉴权拒绝特征（仅兜底；结构化体一律按 code 判定）。"""
    text = body or ""
    return "-2015" in text or "-2014" in text or "invalid api-key" in text.lower()


def _egress_state_load() -> dict:
    """读状态 → {"verified": {ip: ts}, "rejected": {ip: ts}}。

    缺失/损坏一律当空表；同时兼容 D 首版的扁平格式 {ip: verified_at}（当时未上线，
    仅 worktree 自测写过）。
    """
    data = read_json(EGRESS_STATE_FILE)
    out = {"verified": {}, "rejected": {}}
    if not isinstance(data, dict):
        return out
    buckets = None
    if isinstance(data.get("verified"), dict) or isinstance(data.get("rejected"), dict):
        buckets = {}
        for name in ("verified", "rejected"):
            src = data.get(name)
            buckets[name] = src if isinstance(src, dict) else {}
    else:
        buckets = {"verified": data, "rejected": {}}      # 旧扁平格式
    for name in ("verified", "rejected"):
        for ip, ts in (buckets.get(name) or {}).items():
            try:
                out[name][str(ip)] = float(ts)
            except (TypeError, ValueError):
                continue
    return out


def _egress_state_save(state: dict):
    def _cap(bucket):
        src = state.get(bucket) or {}
        keep = sorted(src.items(), key=lambda kv: kv[1])[-EGRESS_STATE_MAX_ENTRIES:]
        return {ip: ts for ip, ts in keep}

    write_json(EGRESS_STATE_FILE, {"v": 1,
                                   "verified": _cap("verified"),
                                   "rejected": _cap("rejected")})


def _egress_issue(ip: str, detail: str, note: str = "") -> tuple:
    """构造 egress 拒绝 issue。key 带 IP（2026-10-01 复审 P3）：去重按 key 计，
    FlClash 每 1-2 分钟轮换出口，30 分钟内**另一个**被拒 IP 会被无 IP 后缀的键吞掉，
    运维看到的是过期 IP。"""
    body = (f"只读探活被币安拒绝：{detail}\n"
            f"可能原因（按排查顺序）：\n"
            f"  ① {ip} 不在币安 API key 白名单——最常见，代理轮换出新出口即触发；\n"
            f"  ② 白名单已含该 IP，但 key 被禁用或权限不足（-2014 即 key 格式/状态异常）。\n"
            f"影响：该出口下 bot 的签名请求会返回同一错误，生产代码**单次**即进入"
            f"盲区安全模式（全部 API 停摆，需人工发送 /auth_reset 才恢复）。\n"
            f"处置：查明原因后下轮探活通过即自动登记、告警消失；或在 FlClash 固定出口节点。")
    if note:
        body = f"{note}\n{body}"
    return (f"egress_ip_rejected:{ip}", f"出口 IP {ip} 签名鉴权被拒（疑不在白名单）", body)


def _prune_egress_alert_keys(now: float):
    """按 IP 拆键会让 .patrol_alert.state.json 随出口轮换增长——清掉过期 egress 键。"""
    try:
        state = read_json(ALERT_STATE_FILE)
        if not isinstance(state, dict):
            return
        stale = [k for k, v in state.items()
                 if isinstance(k, str) and k.startswith("egress_ip_rejected:")
                 and isinstance(v, (int, float))
                 and now - float(v) > EGRESS_ALERT_KEEP_SECONDS]
        for k in stale:
            state.pop(k, None)
        if stale:
            write_json(ALERT_STATE_FILE, state)
    except Exception as e:
        log(f"⚠️ egress 告警键清理失败（不影响告警本身）: {e}")


def check_egress_ip(env: dict, proxy_url: str = "", summary=None):
    """新增/轮换的出口 IP 是否仍在币安 API 白名单。返回 issues（3 元组列表）。

    判定分级（刻意保守，避免噪音）：
      · 已登记且未过 TTL          → 零币安签名调用，只在 summary 留痕；
      · 已被拒 + 处于冷却期       → **不再发失败签名请求**，但仍出 issue（P1）；
      · 未登记/冷却过期 + 探活通过 → 登记 verified、清 rejected，不告警（新 IP 合法是常态）；
      · 探活被明确拒绝            → 记 rejected 时间戳 + issue `egress_ip_rejected:{ip}`；
      · 取不到 IP / 网络失败/-1021 → 只记日志，绝不告警，下轮重试。
    """
    issues = []
    if not EGRESS_CHECK_ENABLED:
        return issues
    try:
        ip = get_egress_ip(env, proxy_url)
        if not ip:
            return issues
        state = _egress_state_load()
        verified, rejected = state["verified"], state["rejected"]
        now = time.time()

        verified_at = verified.get(ip, 0.0)
        if verified_at and (now - verified_at) < EGRESS_VERIFY_TTL_SECONDS:
            if summary is not None:
                summary.append(f"出口 IP {ip} 白名单已核验({int(now - verified_at)}s 前)")
            return issues

        rejected_at = rejected.get(ip, 0.0)
        if rejected_at and (now - rejected_at) < EGRESS_REJECT_COOLDOWN_SECONDS:
            age = int(now - rejected_at)
            log(f"🔁 出口 IP {ip} 处于拒绝冷却期，跳过重复探活"
                f"（剩 {EGRESS_REJECT_COOLDOWN_SECONDS - age}s），告警仍按去重窗口发送")
            issues.append(_egress_issue(
                ip, f"冷却期内未重复探活（上次被拒 {age}s 前）",
                note=f"[冷却] 距上次被拒 {age}s，{EGRESS_REJECT_COOLDOWN_SECONDS}s "
                     f"冷却期结束或出口变化后才会重新探活。"))
            return issues

        verdict, detail = probe_auth_for_ip(env, proxy_url)
        if verdict == "ok":
            verified[ip] = now
            rejected.pop(ip, None)
            _egress_state_save({"verified": verified, "rejected": rejected})
            log(f"✅ 出口 IP {ip} 鉴权探活通过，已登记"
                f"（{EGRESS_VERIFY_TTL_SECONDS}s 内不再探活）")
            if summary is not None:
                summary.append(f"出口 IP {ip} 探活通过")
        elif verdict == "rejected":
            rejected[ip] = now
            verified.pop(ip, None)
            _egress_state_save({"verified": verified, "rejected": rejected})
            _prune_egress_alert_keys(now)
            issues.append(_egress_issue(ip, detail))
        elif verdict == "skip":
            log(f"ℹ️ 出口 IP {ip} 白名单核验跳过：{detail}")
        elif "-1021" in detail:
            # P2：时间戳问题不能混进"未完成"被忽略——它是可直接处置的本地故障
            log(f"⚠️ 出口 IP {ip} 探活被时间戳校验拒绝(-1021)：本机时钟可能偏移，"
                f"请校时（w32tm /resync）后重试；按设计不告警: {detail}")
        else:
            log(f"⚠️ 出口 IP {ip} 鉴权探活未完成（不告警，下轮重试）: {detail}")
    except Exception as e:
        # 设计约束④：自身故障绝不抛出
        log(f"⚠️ 出口 IP 白名单核验自身异常（按设计不抛出）: {type(e).__name__}: {e}")
    return issues


# ==================== 巡检主体 ====================
def run_check(env: dict, dry_run: bool) -> int:
    proxy_url = env.get("BINANCE_PROXY", "")
    issues = []      # (key, title, detail)
    summary = []
    egress_issues = []

    # D：出口 IP 核验与"守护链是否活着"无关，**必须先于**下面 stopped 提前返回执行。
    # 停机静默只针对守护链类误报（watchdog/bot 存活），不能连带吞掉鉴权拒绝——
    # 「停机→重启」正是 bot 启动探活撞 -2015 进盲区的高危窗口（2026-10-01 复审 P1b）。
    proxy_ok = proxy_ready(proxy_url) if proxy_url else True
    if proxy_ok:
        egress_issues = check_egress_ip(env, proxy_url, summary)
        for key, title, detail in egress_issues:
            log(f"🚨 [{key}] {title} | {detail}")
            alert(env, key, title, detail, dry_run)

    hb = read_json(HEARTBEAT_FILE)
    if not hb:
        lock_pid = None
        try:
            with open(LOCK_FILE, encoding="utf-8") as f:
                lock_pid = int((f.read() or "0").strip() or 0)
        except Exception:
            pass
        issues.append(("no_heartbeat", "未发现心跳文件 .heartbeat.json",
                       f"watchdog 可能未以新版启动（#5 心跳特性未生效）或文件被删。"
                       f"锁文件 PID={lock_pid}（存活={pid_alive(lock_pid) if lock_pid else 'N/A'}）。"))
    else:
        age = time.time() - float(hb.get("ts", 0) or 0)
        summary.append(f"心跳 {age:.0f}s 前 | watchdog_pid={hb.get('watchdog_pid')} | "
                       f"bot_pid={hb.get('bot_pid')} bot_alive={hb.get('bot_alive')} | "
                       f"restarts={hb.get('restarts')} 最后原因={hb.get('last_reason')}")
        if hb.get("stopped"):
            # 停机静默只覆盖守护链类问题（避免停机后反复误报）；
            # 出口 IP 告警已在本函数开头独立分发，此处不受影响。
            log(f"ℹ️ 巡检: 状态=已主动停止（{hb.get('stopped_reason')}）→ 守护链类不告警"
                + (" ; ⚠️ 出口 IP 异常已单独告警" if egress_issues else "")
                + (" ; " + " ; ".join(summary) if summary else ""))
            return 0
        # R0 / F11（2026-09-25）：Watchdog 启动熔断等致命故障写入心跳 fatal_alert。
        # 该场景 bot_runner 从未成功启动 → .notify_queue 无消费进程 → 队列里的
        # crash_alert 永不发；由**本独立进程**承担告警（不依赖 bot 存活），
        # 且走 alert(event="health") → 邮件豁免持仓闸门。
        fatal = hb.get("fatal_alert")
        if isinstance(fatal, dict) and fatal.get("reason"):
            issues.append((
                "watchdog_fatal",
                "Watchdog 已停止自动重启（致命故障）",
                f"{fatal.get('reason')}\n"
                f"发生时间: {fatal.get('ts_str', '未知')}\n"
                f"watchdog_pid={hb.get('watchdog_pid')} bot_pid={hb.get('bot_pid')}。"
                "自动重启已停止，交易所侧条件单仍有效但程序不再自愈；请人工排查后重新启动。"))
        if age > MAX_AGE_SECONDS:
            issues.append(("stale_heartbeat",
                           f"心跳陈旧 {age:.0f}s（阈值 {MAX_AGE_SECONDS}s）",
                           "守护链疑似已死/机器冻结/进程卡死。现场："
                           f"watchdog_pid={hb.get('watchdog_pid')} "
                           f"bot_pid={hb.get('bot_pid')} bot_alive={hb.get('bot_alive')}。"
                           "交易所侧已挂出的 SL/TP 仍由交易所维护，"
                           "但程序无法补挂/改单/撤单。"))
        elif not hb.get("bot_alive"):
            st = read_json(ALERT_STATE_FILE) or {}
            dead_cnt = int(st.get("_bot_dead_count", 0) or 0) + 1
            st["_bot_dead_count"] = dead_cnt
            write_json(ALERT_STATE_FILE, st)
            summary.append(f"bot_alive=False 连续 {dead_cnt} 次")
            if dead_cnt >= BOT_DEAD_THRESHOLD and not pid_alive(hb.get("bot_pid") or 0):
                issues.append(("bot_dead", "bot 子进程持续不存活",
                               f"watchdog 存活但 bot_runner 未运行（连续 {dead_cnt} 次巡检）。"
                               "若 watchdog 已触发启动熔断（连续 5 次启动失败）则需人工介入。"))
        else:
            st = read_json(ALERT_STATE_FILE) or {}
            if st.get("_bot_dead_count"):
                st["_bot_dead_count"] = 0
                write_json(ALERT_STATE_FILE, st)

    # 业务进度活性：区分正常空闲与真实冻结；不再把日志 mtime 当作线程心跳。
    if hb and hb.get("bot_alive") and not hb.get("stopped"):
        try:
            states = read_json(TRADE_STATE_FILE)
            if states is None or not isinstance(states, dict):
                raise ValueError("trade_state.json 无法解析")
            active = []
            for sym, batches in states.items():
                if not isinstance(batches, dict):
                    continue
                for bid, data in batches.items():
                    if isinstance(data, dict) and data.get("is_active"):
                        active.append((sym, bid))
            control = read_json(os.path.join(HEALTH_DIR, "control.json"))
            if not control or not control.get("instance_id"):
                raise ValueError("控制面进度文件缺失或损坏")
            age = time.time() - float(control.get("ts", 0) or 0)
            if age > PROGRESS_STALL_SECONDS:
                issues.append(("control_stalled", f"控制循环进度停滞 {age:.0f}s（阈值 {PROGRESS_STALL_SECONDS}s）",
                               f"instance={control.get('instance_id')}；请先查看 py-spy 与业务日志，再按应急流程重启。"))
            else:
                summary.append(f"控制进度新鲜 {age:.0f}s 前 | 活跃批次 {len(active)}")
            for sym, bid in active:
                p = read_json(os.path.join(HEALTH_DIR, f"batch_{bid}.json"))
                if not p or p.get("instance_id") != control.get("instance_id"):
                    issues.append(("batch_progress_missing", f"活跃批次 [{sym}/{bid}] 缺少当前实例进度",
                                   "监控线程可能未启动、已冻结或实例切换；请人工核查，禁止假设为空闲。"))
                else:
                    a = time.time() - float(p.get("ts", 0) or 0)
                    if a > PROGRESS_STALL_SECONDS:
                        issues.append(("batch_stalled", f"批次 [{sym}/{bid}] 监控进度停滞 {a:.0f}s",
                                       f"instance={p.get('instance_id')}；该批次存在活动状态，禁止按空闲处理。"))
        except ValueError as e:
            issues.append(("health_state_unknown", f"业务活性状态未知：{e}",
                           "无法确认 bot 是否空闲；按 Fail-Safe 处理，核查 trade_state.json 与 .bot_health 后再决定是否重启。"))

    # proxy_ok 已在函数开头算好（出口核验要用），这里只补 proxy_down 这一条 issue
    if proxy_url and not proxy_ok:
        issues.append(("proxy_down", "本地代理不可达",
                       f"{proxy_hostport(proxy_url)} 无法连接：币安 API 与 TG 均不可用，"
                       "bot 的监控循环会持续失败。请启动代理软件。"))

    # 出口 IP issue 已在开头分发过，此处只处理其余 issue，避免重复告警
    if issues:
        for key, title, detail in issues:
            log(f"🚨 [{key}] {title} | {detail}")
            alert(env, key, title, detail, dry_run)
    if issues or egress_issues:
        return 0

    log("✅ 巡检正常: " + " ; ".join(summary or ["无心跳明细"]))
    return 0


def main(argv) -> int:
    args = list(argv[1:])
    dry_run = "--dry-run" in args
    env = load_env()
    # 导入期无法打日志（log 此时还没定义），环境变量解析问题延到这里首行报出
    for _msg in _EGRESS_ENV_ERRORS:
        log(f"⚠️ 出口 IP 巡检环境变量: {_msg}")
    proxy_url = env.get("BINANCE_PROXY", "")

    if "--wait-proxy" in args:
        idx = args.index("--wait-proxy")
        seconds = int(args[idx + 1]) if len(args) > idx + 1 else 300
        return 0 if wait_proxy(proxy_url, seconds) else 2

    if "--notify-proxy-down" in args:
        log("🚨 开机自启被跳过：代理未就绪")
        text = ("🚨【资金安全】Bot 开机自启已被跳过\n"
                "原因：本地代理 127.0.0.1:7890 在等待窗口内未就绪。\n"
                "为避免「无代理启动 → 恢复链失败 → 持仓批次失管」，自启已主动放弃。\n"
                "请启动代理软件后，手动双击 启动Bot.bat。")
        if dry_run:
            log("[dry-run] 本应发送: " + text.replace("\n", " / "))
            return 0
        sent = send_tg(env, text, use_proxy=False)
        if not sent:
            # R0：开机自启被跳过 = 交易能力缺失，邮件兜底豁免持仓闸门
            send_email(env, "🚨 Bot 开机自启被跳过", text, event="health")
        st = read_json(ALERT_STATE_FILE) or {}
        st["autostart_skipped"] = time.time()
        write_json(ALERT_STATE_FILE, st)
        return 0

    if "--selftest" in args:
        # 运维部署自检：逐通道实测（TG 经代理 / TG 直连 / 邮件兜底），结果只进日志。
        # 复审 D4：邮件自检用 event="health" —— 与真实告警同一策略（致命豁免持仓闸门），
        # 否则空仓时自检会被闸门跳过，得出「邮件通道不可用」的错误结论。
        msg = ("🧪【自检】CryptoBot 健康巡检通道测试\n"
               "此消息证明告警通道可用，可忽略。\n"
               f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log("=== 通道自检开始 ===")
        ok_proxy = send_tg(env, msg + "\n（通道：TG 经代理）", use_proxy=True)
        ok_direct = send_tg(env, msg + "\n（通道：TG 直连）", use_proxy=False)
        ok_mail = send_email(env, "🧪 巡检通道自检", msg + "\n（通道：邮件兜底）",
                             event="health")
        log(f"=== 自检结果: TG经代理={ok_proxy} TG直连={ok_direct} 邮件={ok_mail} ===")
        # D：出口 IP 白名单只读核验（自检用，不发消息、不写状态文件）
        _ip = get_egress_ip(env, proxy_url)
        if _ip:
            _verdict, _detail = probe_auth_for_ip(env, proxy_url)
            log(f"=== 出口 IP 自检: ip={_ip} verdict={_verdict} ({_detail}) ===")
            if _verdict == "rejected":
                log(f"🚨 [egress_ip_rejected] 出口 IP {_ip} 不在币安 API 白名单，"
                    f"请加白或在 FlClash 固定出口节点")
        else:
            log("=== 出口 IP 自检: 无法获取出口 IP（见上方日志）===")
        return 0

    return run_check(env, dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as e:
        log(f"❌ 巡检自身异常（不阻断计划任务）: {e}")
        sys.exit(0)


