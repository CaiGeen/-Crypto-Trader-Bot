#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""健康巡检.py —— 死人心跳巡检（v6.5 生产运维，2026-09-23）

职责：判定「守护链是否活着」——异常告警，正常完全静默。
数据源：watchdog 维护的 .heartbeat.json（每 60s 原子刷新）。
告警通道：Telegram（经 .env 的 BINANCE_PROXY）→ 代理不可用则直连重试
         → 仍失败降级 QQ 邮件（SMTP 国内直连，代理挂掉时仍可达）。
去重：同一问题 30 分钟内只告警一次（.patrol_alert.state.json）。

设计约束（刻意为之）：
  ① 纯标准库——venv 包损坏 / pip 环境被改时仍能报警；
  ② 绝不触碰交易状态（只读心跳与锁文件，不写 trade_state）；
  ③ pythonw.exe 下 sys.stdout 为 None → 入口即做空值防护；
  ④ 自身故障绝不抛出（exit 0），只在日志留痕，避免计划任务刷"失败"。

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
# 🔥 2026-09-23 18:29 事故新增：输出停滞阈值——bot 正常每 ~60s 必打一行（限流观测），
# 超过该阈值仍无新行 = "进程活着但线程冻结"（此前的进程/心跳检测无法识别此形态）。
OUTPUT_STALL_SECONDS = int(os.getenv("PATROL_OUTPUT_STALL_SECONDS", "600"))

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


def send_email(env: dict, subject: str, text: str) -> bool:
    """QQ 邮件兜底：SMTP 国内直连，代理不可达时仍可用。"""
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
        sent = send_email(env, "🚨 Bot 健康巡检异常", text)
    if sent:
        state[key] = now
        write_json(ALERT_STATE_FILE, state)
    else:
        log(f"❌ [{key}] 所有告警通道均失败（请人工检查机器与网络）")


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

    # 🔥 2026-09-23 18:29 事故新增：输出停滞检测（"进程/心跳健康但线程冻结"）。
    #   本次事故中 watchdog 被控制台卡住 → 管道积满 → bot 阻塞在 print 且持锁 →
    #   TG/监控全灭 3 小时，而心跳与 bot_alive 全程"正常"，原有两项检测全盲。
    #   判据：bot_alive=true 且最新 logs/bot_*.log 超过 OUTPUT_STALL_SECONDS 无新行。
    #   （盲区静默 AUTH_BLOCKED 期间也可能无输出，提示文案同时覆盖两种成因。）
    if hb and hb.get("bot_alive") and not hb.get("stopped"):
        try:
            newest, newest_mtime = None, 0.0
            if os.path.isdir(LOG_DIR):
                for name in os.listdir(LOG_DIR):
                    if name.startswith("bot_") and name.endswith(".log"):
                        p = os.path.join(LOG_DIR, name)
                        m = os.path.getmtime(p)
                        if m > newest_mtime:
                            newest, newest_mtime = p, m
            if newest:
                out_age = time.time() - newest_mtime
                if out_age > OUTPUT_STALL_SECONDS:
                    issues.append((
                        "output_stalled",
                        f"bot 输出停滞 {out_age:.0f}s（阈值 {OUTPUT_STALL_SECONDS}s）",
                        f"进程与心跳均正常但 {os.path.basename(newest)} 长时间无新行——"
                        "线程级冻结特征（输出阻塞持锁/死锁），或 API 盲区静默。"
                        "现象：TG 无响应、控制台不再刷新。处置："
                        f"taskkill /F /T /PID {hb.get('watchdog_pid')} 后 "
                        "schtasks /Run /TN CryptoBot-Autostart 整链重启。"))
                else:
                    summary.append(f"输出新鲜 {out_age:.0f}s 前")
        except Exception:
            pass

    if proxy_url and not proxy_ready(proxy_url):
        issues.append(("proxy_down", "本地代理不可达",
                       f"{proxy_hostport(proxy_url)} 无法连接：币安 API 与 TG 均不可用，"
                       "bot 的监控循环会持续失败。请启动代理软件。"))

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
            send_email(env, "🚨 Bot 开机自启被跳过", text)
        st = read_json(ALERT_STATE_FILE) or {}
        st["autostart_skipped"] = time.time()
        write_json(ALERT_STATE_FILE, st)
        return 0

    if "--selftest" in args:
        # 运维部署自检：逐通道实测（TG 经代理 / TG 直连 / 邮件兜底），结果只进日志。
        msg = ("🧪【自检】CryptoBot 健康巡检通道测试\n"
               "此消息证明告警通道可用，可忽略。\n"
               f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log("=== 通道自检开始 ===")
        ok_proxy = send_tg(env, msg + "\n（通道：TG 经代理）", use_proxy=True)
        ok_direct = send_tg(env, msg + "\n（通道：TG 直连）", use_proxy=False)
        ok_mail = send_email(env, "🧪 巡检通道自检", msg + "\n（通道：邮件兜底）")
        log(f"=== 自检结果: TG经代理={ok_proxy} TG直连={ok_direct} 邮件={ok_mail} ===")
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


