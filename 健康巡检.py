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
  ⑤ 出口 IP 白名单巡检（D，2026-09-30）：经代理取当前出口 IP，未登记时做一次
     **只读**签名探活（GET /fapi/v1/balance）判定是否仍在币安 API 白名单；
     不下单、不撤单、不改仓、不写 trade_state；取不到 IP 或网络失败一律只记日志、
     绝不告警（避免代理抖动变成噪音），仅在币安明确回 -2015/-2014 时才告警。

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
EGRESS_CHECK_ENABLED = os.getenv("EGRESS_IP_CHECK", "true").strip().lower() in ("1", "true", "yes", "on")
# 同一出口 IP 每 6h 复检一次（防「白名单后来被移除」这类反向变化），其余轮次零币安签名调用
EGRESS_VERIFY_TTL_SECONDS = int(os.getenv("EGRESS_IP_VERIFY_TTL_SECONDS", "21600"))
EGRESS_STATE_MAX_ENTRIES = 32          # 状态文件只留最近 N 个出口，防无限增长
EGRESS_IP_TIMEOUT = int(os.getenv("EGRESS_IP_TIMEOUT", "8"))
EGRESS_PROBE_TIMEOUT = int(os.getenv("EGRESS_PROBE_TIMEOUT", "12"))

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
    """只读 GET。返回 (status, body)。网络失败由调用方捕获（本函数不吞异常）。"""
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    if proxy_url:
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    else:
        handler = urllib.request.ProxyHandler({})          # 显式不走系统代理
    opener = urllib.request.build_opener(handler)
    with opener.open(req, timeout=timeout) as resp:
        code = getattr(resp, "status", None) or resp.getcode()
        return int(code), resp.read().decode("utf-8", "replace")


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
            if ip and 3 <= len(ip) <= 45:
                return ip
            last_err = f"{url} → 响应不可解析: {body[:80]!r}"
        except Exception as e:
            last_err = f"{url} → {type(e).__name__}: {e}"
    log(f"ℹ️ 出口 IP 获取失败（本轮跳过白名单核验）: {last_err}")
    return None


def probe_auth_for_ip(env: dict, proxy_url: str = ""):
    """只读鉴权探活：当前出口 IP 下签名请求是否被币安接受。

    返回 (verdict, detail)，verdict ∈ {"ok", "rejected", "error", "skip"}。
    只读：GET /fapi/v1/balance（余额查询）——不下单、不撤单、不改仓。
    """
    key = env.get("BINANCE_API_KEY", "")
    secret = env.get("BINANCE_SECRET", "")
    if not (key and secret):
        return "skip", "未配置 BINANCE_API_KEY/BINANCE_SECRET"
    try:
        import hmac
        from urllib.parse import urlencode
        query = urlencode({"timestamp": int(time.time() * 1000), "recvWindow": 5000})
        signature = hmac.new(secret.encode(), query.encode(), "sha256").hexdigest()
        url = f"https://fapi.binance.com/fapi/v1/balance?{query}&signature={signature}"
        status, body = _egress_http_get(url, proxy_url, EGRESS_PROBE_TIMEOUT,
                                        headers={"X-MBX-APIKEY": key})
        if 200 <= status < 300:
            return "ok", f"HTTP {status}（出口 IP 鉴权探活通过）"
        low = (body or "").lower()
        # 仅当币安**明确**拒绝鉴权才算「不在白名单」；其余（429/5xx/网关）归为 error
        if "-2015" in (body or "") or "-2014" in (body or "") or "invalid api-key" in low:
            return "rejected", f"HTTP {status}: {(body or '')[:300]}"
        return "error", f"HTTP {status}: {(body or '')[:200]}"
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"


def _egress_state_load() -> dict:
    """读已核验出口 IP 记录 {ip: verified_at}。损坏/缺失一律当空表。"""
    data = read_json(EGRESS_STATE_FILE)
    if not isinstance(data, dict):
        return {}
    clean = {}
    for ip, ts in data.items():
        try:
            clean[str(ip)] = float(ts)
        except (TypeError, ValueError):
            continue
    return clean


def _egress_state_save(ips: dict):
    keep = sorted(ips.items(), key=lambda kv: kv[1])[-EGRESS_STATE_MAX_ENTRIES:]
    write_json(EGRESS_STATE_FILE, {ip: ts for ip, ts in keep})


def check_egress_ip(env: dict, proxy_url: str = "", summary=None):
    """新增/轮换的出口 IP 是否仍在币安 API 白名单。返回 issues（3 元组列表）。

    判定分级（刻意保守，避免噪音）：
      · 已登记且未过 TTL        → 零币安签名调用，只在 summary 留痕；
      · 未登记/已过期 + 探活通过 → 登记并写状态文件，不告警（新 IP 合法是常态）；
      · 探活被明确拒绝(-2015)   → 告警 egress_ip_rejected（走 alert 的 30min 去重）；
      · 取不到 IP / 网络失败     → 只记日志，绝不告警，下轮重试。
    """
    issues = []
    if not EGRESS_CHECK_ENABLED:
        return issues
    try:
        ip = get_egress_ip(env, proxy_url)
        if not ip:
            return issues
        ips = _egress_state_load()
        now = time.time()
        verified_at = ips.get(ip, 0.0)
        if verified_at and (now - verified_at) < EGRESS_VERIFY_TTL_SECONDS:
            if summary is not None:
                summary.append(f"出口 IP {ip} 白名单已核验({int(now - verified_at)}s 前)")
            return issues

        verdict, detail = probe_auth_for_ip(env, proxy_url)
        if verdict == "ok":
            ips[ip] = now
            _egress_state_save(ips)
            log(f"✅ 出口 IP {ip} 鉴权探活通过，已登记"
                f"（{EGRESS_VERIFY_TTL_SECONDS}s 内不再探活）")
            if summary is not None:
                summary.append(f"出口 IP {ip} 探活通过")
        elif verdict == "rejected":
            issues.append((
                "egress_ip_rejected",
                f"出口 IP {ip} 不在币安 API 白名单",
                f"只读探活被拒：{detail}\n"
                f"影响：该出口下 bot 的签名请求会返回 -2015，生产代码单次即进入"
                f"盲区安全模式（全部 API 停摆，需人工发送 /auth_reset 才恢复）。\n"
                f"处置：① 把 {ip} 加入币安 API key 白名单；或 ② 在 FlClash 固定出口节点。"
                f"完成后下轮探活通过即自动登记、告警消失。"))
        elif verdict == "skip":
            log(f"ℹ️ 出口 IP {ip} 白名单核验跳过：{detail}")
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
            log(f"ℹ️ 巡检: 状态=已主动停止（{hb.get('stopped_reason')}）→ 不告警 ; "
                + " ; ".join(summary))
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

    proxy_ok = proxy_ready(proxy_url) if proxy_url else True
    if proxy_url and not proxy_ok:
        issues.append(("proxy_down", "本地代理不可达",
                       f"{proxy_hostport(proxy_url)} 无法连接：币安 API 与 TG 均不可用，"
                       "bot 的监控循环会持续失败。请启动代理软件。"))

    # D（2026-09-30）：出口 IP 白名单核验。放在 proxy_ok 分支内——代理不通时
    # 本就取不到出口 IP，再去探活只会多一条无意义日志。
    if proxy_ok:
        issues.extend(check_egress_ip(env, proxy_url, summary))

    if issues:
        for key, title, detail in issues:
            log(f"🚨 [{key}] {title} | {detail}")
            alert(env, key, title, detail, dry_run)
        return 0

    log("✅ 巡检正常: " + " ; ".join(summary or ["无心跳明细"]))
    return 0


def main(argv) -> int:
    args = list(argv[1:])
    dry_run = "--dry-run" in args
    env = load_env()
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


