#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
watchdog.py - 量化交易 Bot 守护进程
功能：
1. 主程序崩溃时自动重启
2. 每 4 小时在整点自动重启（北京时间）【可通过开关控制】
3. 重启通知发送到 TG
"""

import json
import os
import sys
import time
import signal
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
import pytz

# ==================== 配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(BASE_DIR, "bot_runner.py")
LOG_FILE = os.path.join(BASE_DIR, "watchdog.log")

# ==================== 生产运维补强（2026-09-23） ====================
# 背景：#4 —— trader/bot_runner 全量走 print，只进控制台与 watchdog 的 PIPE 转发；
#   窗口关闭 / 机器重启 / 终端崩溃后，交易全过程（成交、挂单、告警、限流观测）
#   不可追溯（8-19 二次 418 事故取证时正是卡在"无落盘数据源"）。
#   方案：watchdog 在转发子进程 stdout 的同一处顺手按天落盘——零侵入，不改 bot 代码。
# 背景：#5 —— 需要"死人心跳"判定守护链是否活着。watchdog 每 60s 原子写
#   .heartbeat.json；独立的 健康巡检.py 读它，陈旧即告警（正常时完全静默）。
BOT_LOG_DIR = os.path.join(BASE_DIR, "logs")
BOT_LOG_RETENTION_DAYS = 14        # 交易日志保留天数（防磁盘无界增长）
HEARTBEAT_FILE = os.path.join(BASE_DIR, ".heartbeat.json")
HEARTBEAT_INTERVAL = 60            # 心跳写入间隔（秒）

# 🔥 定时重启开关
ENABLE_SCHEDULED_RESTART = False
ENABLE_SUMMARY_ON_RESTART = True

RESTART_HOURS = [0, 4, 8, 12, 16, 20]
RESTART_MINUTE = 0

BEIJING_TZ = pytz.timezone('Asia/Shanghai')


# ==================== Watchdog 安全补丁 v1（D-004，2026-08-20） ====================
# 背景：双 watchdog 演练暴露三层缺陷——
#   根因1: 子进程 stdout 为 PIPE 时 Python 按本地 ANSI 代码页（cp936）编码，
#          emoji print 抛 UnicodeEncodeError，曾使单实例锁拒绝路径死在 print 上
#          （退出码 42 变 1，见 bot_runner.py L160 拒绝提示）；
#   根因2: 主程序持续启动失败时 watchdog 无限重启（无熔断）；
#   根因3: crash_alert 无同因去重 -> TG+邮件通知风暴。
# 修复：R1 编码保护 / R2 启动熔断 / R3 告警去重
# 详见 D-004_Watchdog重复启动风暴_事故档案.md

INIT_FAILURE_WINDOW = 60        # R2: 初始化窗口（秒）——窗口内退出视为启动失败（bot_runner 初始化含交易所连接，取保守值）
MAX_INIT_FAILURES = 5           # R2: 连续启动失败上限 -> 熔断停止自动重启
CRASH_ALERT_DEDUP_WINDOW = 600  # R3: 崩溃告警同因去重窗口（秒）


def make_stdout_crash_safe():
    """R1: 入口级编码保护——非 UTF-8 环境（GBK 控制台/管道）下 emoji print 不再抛
    UnicodeEncodeError。拒绝/告警路径必须比正常路径更稳定，不能死在一句提示上。"""
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None and hasattr(_stream, 'reconfigure'):
                _stream.reconfigure(errors='replace')
        except Exception:
            pass


# R3: crash_alert 同因去重状态（restart_reason -> 上次发送的 monotonic 时间）
_crash_alert_last_sent = {}


def crash_alert_allowed(reason: str) -> bool:
    """R3: 同一 restart_reason 在去重窗口内只发 1 次（防通知风暴）。"""
    now = time.monotonic()
    last = _crash_alert_last_sent.get(reason)
    if last is not None and (now - last) < CRASH_ALERT_DEDUP_WINDOW:
        return False
    _crash_alert_last_sent[reason] = now
    return True


def crash_alert_reset():
    """R3: 主程序稳定运行后解除同因去重（下次崩溃重新提醒）。"""
    _crash_alert_last_sent.clear()


# R2: 连续启动失败计数（初始化窗口内退出的次数）
_init_fail_count = 0


def record_process_exit(uptime: float) -> bool:
    """R2: 记录一次主程序退出。返回 True = 触发启动熔断（调用方应停止重启）。
    - 初始化窗口内退出 -> 计数 +1，连续达 MAX_INIT_FAILURES -> True
    - 稳定运行（超出窗口）-> 计数清零，并解除 R3 告警去重（恢复语义）"""
    global _init_fail_count
    if uptime < INIT_FAILURE_WINDOW:
        _init_fail_count += 1
        if _init_fail_count >= MAX_INIT_FAILURES:
            return True
    else:
        _init_fail_count = 0
        crash_alert_reset()
    return False


# ==================== 日志函数 ====================
def log_message(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {msg}\n"
    # 🔥 2026-09-23 18:29 事故 hotfix：原实现 print 在写文件之前——控制台卡死时
    # watchdog.log 也一起丢。改为文件优先；控制台输出走解耦队列（见 monitor_process）。
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception:
        pass
    try:
        _CONSOLE_QUEUE.put_nowait(log_entry)
    except Exception:
        try:
            print(log_entry.strip())
        except Exception:
            pass


# ==================== #4 交易日志落盘（2026-09-23） ====================
# 设计要点：
#   ① 按天轮转 logs/bot_YYYYMMDD.log；② 逐行 flush（断电/强杀最多丢一行）；
#   ③ 任何异常静默吞掉——观测面绝不打断监控/重启主链（与本项目"观测层零影响"一致）；
#   ④ 只在文件侧加 [HH:MM:SS] 前缀，控制台输出保持原样（零 UI 回归）。
_bot_log_lock = threading.Lock()
_bot_log_fh = None
_bot_log_day = None


def _bot_log_path(day: str = None) -> str:
    day = day or datetime.now().strftime("%Y%m%d")
    return os.path.join(BOT_LOG_DIR, f"bot_{day}.log")


def _prune_bot_logs():
    """保留最近 BOT_LOG_RETENTION_DAYS 天（调用方须持 _bot_log_lock，失败静默）。"""
    try:
        cutoff = time.time() - BOT_LOG_RETENTION_DAYS * 86400
        for name in os.listdir(BOT_LOG_DIR):
            if not (name.startswith("bot_") and name.endswith(".log")):
                continue
            p = os.path.join(BOT_LOG_DIR, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


def write_bot_log(line: str):
    """把主程序 stdout 的一行写入当日交易日志（跨天自动切换文件）。"""
    global _bot_log_fh, _bot_log_day
    try:
        day = datetime.now().strftime("%Y%m%d")
        with _bot_log_lock:
            if _bot_log_fh is None or _bot_log_day != day:
                if _bot_log_fh is not None:
                    try:
                        _bot_log_fh.close()
                    except Exception:
                        pass
                    _bot_log_fh = None
                os.makedirs(BOT_LOG_DIR, exist_ok=True)
                _bot_log_fh = open(_bot_log_path(day), "a",
                                   encoding="utf-8", errors="replace")
                _bot_log_day = day
                _prune_bot_logs()
            stamp = datetime.now().strftime("%H:%M:%S")
            _bot_log_fh.write(f"[{stamp}] {str(line).rstrip(chr(10) + chr(13))}\n")
            _bot_log_fh.flush()
    except Exception:
        pass


def close_bot_log():
    """退出前关闭句柄（best-effort，不做 fsync——日志非资金安全数据）。"""
    global _bot_log_fh, _bot_log_day
    try:
        with _bot_log_lock:
            if _bot_log_fh is not None:
                _bot_log_fh.close()
            _bot_log_fh = None
            _bot_log_day = None
    except Exception:
        pass


# ==================== #5 心跳文件（2026-09-23） ====================
# 语义：.heartbeat.json 由 watchdog 每 HEARTBEAT_INTERVAL 秒原子刷新一次。
#   文件陈旧  = 守护链已死 / 机器冻结 / 卡死（健康巡检据此告警）；
#   stopped=true = 用户主动停止（健康巡检不告警，避免告警疲劳）。
_heartbeat_state = {
    "watchdog_started_at": None,
    "bot_pid": None,
    "bot_started_at": None,
    "restarts": 0,
    "last_reason": None,
    "stopped": False,
    "stopped_reason": None,
    "fatal_alert": None,   # R0/F11：致命故障（启动熔断等）落盘，由独立巡检进程告警
}


def write_heartbeat():
    """原子写心跳（tmp → os.replace；失败静默，绝不干扰守护逻辑）。"""
    try:
        payload = dict(_heartbeat_state)
        payload["ts"] = time.time()
        payload["ts_str"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload["watchdog_pid"] = os.getpid()
        proc = _current_process
        payload["bot_alive"] = bool(proc is not None and proc.poll() is None)
        tmp = HEARTBEAT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
            f.flush()
        os.replace(tmp, HEARTBEAT_FILE)
    except Exception:
        pass


def mark_stopped(reason: str):
    """标记"用户主动停止"（健康巡检据此静默，避免停机后反复误报）。"""
    try:
        _heartbeat_state["stopped"] = True
        _heartbeat_state["stopped_reason"] = reason
        write_heartbeat()
    except Exception:
        pass


def mark_fatal(reason: str):
    """标记**致命故障**（R0 / F11，2026-09-25）：健康巡检据此独立告警。

    背景：启动熔断时 watchdog 自身 sys.exit(1) 退出，bot_runner 从未成功启动 →
    .notify_queue 无消费进程 → 队列里的 crash_alert 永远不会发出，
    「邮件兜底依赖 bot 内存态」的旧设计在此场景下彻底失效。
    本函数把致命信息落到心跳文件，由**独立进程**（健康巡检，计划任务每 15 分钟）
    读取并发 alert(event="health")，该通道不依赖 bot 是否活着。
    """
    try:
        _heartbeat_state["fatal_alert"] = {
            "reason": reason,
            "ts": time.time(),
            "ts_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        write_heartbeat()
    except Exception:
        pass


def _heartbeat_loop():
    while True:
        write_heartbeat()
        time.sleep(HEARTBEAT_INTERVAL)


def _generate_notify_event_id() -> str:
    """D-010 W1：事件实例身份，与 bot_runner/_trader 写入端格式完全对齐
    （{YYYYMMDD_HHMMSS_ffffff}_{uuid4 前 8 hex}，写入-消费端契约）"""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8]


def atomic_write_notify(content: str):
    """D-010 W1（C1 进程级原子入队）：写入 .notify_queue/{event_id}.notify 事件队列。
    原单槽 .notify（os.replace 覆盖写，多事件互相覆盖风险）已淘汰；watchdog 只写不删，
    由 bot_runner 消费循环负责消费与删除。内容格式 `type|msg`（type 描述事件来源/语义，
    不承担去重职责）。"""
    try:
        queue_dir = os.path.join(BASE_DIR, ".notify_queue")
        os.makedirs(queue_dir, exist_ok=True)
        event_id = _generate_notify_event_id()
        notify_file = os.path.join(queue_dir, f"{event_id}.notify")
        tmp_file = notify_file + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
        os.replace(tmp_file, notify_file)
        log_message(f"📝 已入队通知事件 {event_id}")
    except Exception as e:
        log_message(f"⚠️ 原子写入通知队列失败: {e}")


def send_tg_notification(text: str):
    """D-010 W2（E5 死格式修复）：原实现写 `{iso时间}|{text}`——消费端按 `type|msg`
    解析时 type=iso 时间戳，匹配不到任何已知分支（死格式）。改为 `watchdog_alert|{text}`
    专用类型（不复用 crash_alert：crash_alert 已有"主程序崩溃/重启"既定语义，
    watchdog 其他告警不必然等于 crash——ChatGPT 终审批定）。"""
    try:
        atomic_write_notify(f"watchdog_alert|{text}")
    except Exception as e:
        log_message(f"⚠️ 发送 watchdog 通知失败: {e}")


def get_next_restart_time() -> datetime:
    """计算下一个重启时间（北京时区）- 整点版本"""
    now = datetime.now(BEIJING_TZ)

    for minutes_ahead in range(24 * 7 * 60):
        check_time = now + timedelta(minutes=minutes_ahead)
        hour = check_time.hour
        minute = check_time.minute

        if hour in RESTART_HOURS and minute == RESTART_MINUTE:
            if check_time > now:
                return BEIJING_TZ.localize(
                    datetime(
                        check_time.year, check_time.month, check_time.day,
                        hour, RESTART_MINUTE, 0
                    )
                )

    tomorrow = now + timedelta(days=1)
    return BEIJING_TZ.localize(
        datetime(tomorrow.year, tomorrow.month, tomorrow.day, RESTART_HOURS[0], RESTART_MINUTE, 0)
    )


def get_restart_time_display(next_time: datetime) -> str:
    return next_time.strftime("%Y-%m-%d %H:%M:%S")


# ==================== 主程序管理 ====================
# P0-1: 当前主程序进程（供停止时清理进程树——防"手动停止只杀 watchdog 漏杀 bot_runner"）
_current_process = None


def _kill_main_process_tree():
    """P0-1: 强杀主程序进程树。
    背景（2026-08-19 418 事故）：KeyboardInterrupt 路径原直接 sys.exit(0)，
    bot_runner 子进程成为孤儿继续轮询交易所 → 与新实例并存 = 多倍 API 配额 + 重复挂单。
    taskkill /T 连带子进程强杀（process.terminate 只杀根进程）。"""
    global _current_process
    proc = _current_process
    _current_process = None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            log_message(f"🧹 正在清理主程序进程树 (PID: {proc.pid})...")
            if sys.platform == 'win32':
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                               capture_output=True, timeout=10)
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            log_message("✅ 主程序进程树已清理")
    except Exception as e:
        log_message(f"⚠️ 清理主程序进程树失败: {e}（请手动检查残留 python 进程！）")


def run_main_process():
    log_message("🚀 启动主程序...")
    try:
        process = subprocess.Popen(
            [sys.executable, MAIN_SCRIPT],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding='utf-8',
            errors='replace'
        )
        log_message(f"✅ 主进程已启动 (PID: {process.pid})")
        # #5：登记当前子进程（write_heartbeat / 健康巡检据此判定 bot 侧存活）
        _heartbeat_state["bot_pid"] = process.pid
        _heartbeat_state["bot_started_at"] = time.time()
        # #4：每次拉起新进程都在交易日志打分隔头（便于事后按会话切分取证）
        write_bot_log(f"===== bot_runner 启动 (PID {process.pid}) "
                      f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====")
        write_heartbeat()
        return process
    except Exception as e:
        log_message(f"❌ 启动主程序失败: {e}")
        return None


# 🔥 2026-09-23 18:29 事故 hotfix（P0）：控制台输出解耦。
#   事故链：控制台文字选中(mark 模式) → print(line) 无限阻塞 → 读线程停摆 →
#   管道积满 → 子进程阻塞在 print(_sum) 且持有 _api_semaphore → TG/监控全灭
#   （进程与心跳全程"健康"，巡检无法识别）。现改为：
#   读线程只做 读管道 → 落盘 → 入队；独立 writer 线程消费队列打控制台。
#   控制台再怎么卡，只影响"显示"，读取/落盘/守护判定永远继续。
import queue as _queue

_CONSOLE_QUEUE = _queue.Queue(maxsize=5000)


def _console_writer_loop():
    while True:
        try:
            line = _CONSOLE_QUEUE.get()
            if line is None:
                return
            try:
                print(line, end='')
            except Exception:
                pass
        except Exception:
            pass


threading.Thread(target=_console_writer_loop, daemon=True,
                 name="console_writer").start()


def _looks_like_crash(line: str) -> bool:
    """子程序输出是否表明**真崩溃**（第九轮复审修复，2026-09-25）。

    历史实现：`if "CRASH" in line or "FATAL" in line` —— **纯子串**匹配。
    代价实测：M4 启动横幅里的 `FATAL_EVENTS=[...]`（含 `FATAL`）被判为崩溃 →
    watchdog 反复杀进程重启，形成死循环（空仓、无持仓损失，但告警链被刷屏）。

    修法：
      1) 词边界匹配：`FATAL_EVENTS`、`CRASH_ALERT` 这类**标识符**不再误判
         （`_` 属单词字符，`\\b` 不成立），而 `FATAL: ...` / `CRASH ...` 仍会命中；
      2) 补 `Traceback (most recent call last)` —— Python 崩溃的规范标志，
         比依赖日志措辞更可靠（真实崩溃若未打印这些词，判定也不会失效）。
    """
    import re
    if "Traceback (most recent call last)" in line:
        return True
    return bool(re.search(r'\b(CRASH|FATAL)\b', line)) or "Unhandled exception" in line


def monitor_process(process):
    """监控主程序输出（读线程永不执行可能阻塞的控制台写）。"""
    try:
        for line in process.stdout:
            write_bot_log(line)      # #4：落盘优先（取证不依赖控制台）
            try:                      # 入队显示；队满则丢行（文件已有全量）
                _CONSOLE_QUEUE.put_nowait(line)
            except Exception:
                pass
            if _looks_like_crash(line):
                log_message("⚠️ 检测到主程序异常，准备重启")
                return False
    except Exception as e:
        log_message(f"⚠️ 监控输出异常: {e}")
        return False
    return True


# ==================== 主循环 ====================
def main():
    make_stdout_crash_safe()  # R1: 入口级编码保护（D-004 根因1）
    # #5：心跳线程（daemon，独立于主循环与监控线程，60s 刷新一次）
    _heartbeat_state["watchdog_started_at"] = time.time()
    threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()
    log_message("=" * 60)
    log_message("🛡️ Watchdog 守护进程启动 (稳定版 v2.2)")
    log_message(f"📁 工作目录: {BASE_DIR}")
    log_message(f"📄 主程序: {os.path.basename(MAIN_SCRIPT)}")
    log_message(f"⏱️  崩溃自动重启: ✅ 启用")
    log_message(f"⏱️  定时重启: {'✅ 启用' if ENABLE_SCHEDULED_RESTART else '❌ 禁用'}")
    log_message(f"📊 重启后汇总: {'✅ 启用' if ENABLE_SUMMARY_ON_RESTART else '❌ 禁用'}")
    log_message(f"🧾 交易日志落盘: logs{os.sep}bot_YYYYMMDD.log（保留 "
                f"{BOT_LOG_RETENTION_DAYS} 天）")
    log_message(f"💓 心跳文件: .heartbeat.json（每 {HEARTBEAT_INTERVAL}s，"
                f"供 健康巡检.py 判定存活）")
    log_message("=" * 60)

    while True:
        if ENABLE_SCHEDULED_RESTART:
            next_restart = get_next_restart_time()
            log_message(f"⏰ 下次定时重启: {get_restart_time_display(next_restart)}")
        else:
            next_restart = None
            log_message("💤 定时重启已禁用，仅监控崩溃")

        proc_start = time.monotonic()  # R2: 记录启动时刻（初始化窗口判定基准）
        process = run_main_process()
        if process is None:
            log_message("❌ 无法启动主程序，等待 30 秒后重试...")
            time.sleep(30)
            continue

        global _current_process
        _current_process = process  # P0-1: 登记，供停止路径杀进程树

        # 🔥 启动后等待 5 秒，让主程序初始化
        log_message("⏳ 等待主程序初始化 (5 秒)...")
        time.sleep(5)

        restart_reason = None
        crashed = False

        stop_monitor = threading.Event()

        def monitor_thread_func():
            nonlocal crashed
            try:
                if not monitor_process(process):
                    crashed = True
                    stop_monitor.set()
            except Exception as e:
                log_message(f"⚠️ 监控线程异常: {e}")
                crashed = True
                stop_monitor.set()

        monitor_thread = threading.Thread(target=monitor_thread_func, daemon=True)
        monitor_thread.start()

        while True:
            now = datetime.now(BEIJING_TZ)

            if ENABLE_SCHEDULED_RESTART and next_restart is not None and now >= next_restart:
                restart_reason = "⏰ 定时重启"
                log_message(f"⏰ 定时重启触发: {now.strftime('%Y-%m-%d %H:%M:%S')}")
                break

            if crashed:
                restart_reason = "💥 程序崩溃"
                break

            if process.poll() is not None:
                if process.returncode == 42:  # bot_runner 单实例锁拒绝（非崩溃，勿进重启循环）
                    log_message("🚫 主程序因单实例锁拒绝启动（已有其他实例在运行），watchdog 停止。")
                    log_message("   请先结束已有实例（任务管理器查 python.exe）后再启动。")
                    _kill_main_process_tree()
                    mark_stopped("单实例锁拒绝（已有实例在运行）")
                    close_bot_log()
                    sys.exit(0)
                restart_reason = f"⚠️ 程序异常退出 (退出码: {process.returncode})"
                log_message(restart_reason)
                break

            time.sleep(1)

        stop_monitor.set()
        monitor_thread.join(timeout=2)

        if process.poll() is None:
            log_message("🛑 正在终止主程序...")
            try:
                # 🔥 先发送 Ctrl+C 信号（让程序自己清理）
                if sys.platform == 'win32':
                    # Windows: CTRL_C_EVENT 会发给控制台所有进程（包括 watchdog 自身）
                    # 临时忽略 SIGINT，防止 watchdog 被自己的信号打断
                    _original_sigint = signal.getsignal(signal.SIGINT)
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
                    try:
                        process.send_signal(signal.CTRL_C_EVENT)
                        time.sleep(10)
                    finally:
                        signal.signal(signal.SIGINT, _original_sigint)
                else:
                    process.send_signal(signal.SIGINT)
                    time.sleep(10)

                if process.poll() is None:
                    log_message("⚠️ 程序未响应退出信号，强制终止...")
                    process.terminate()
                    time.sleep(3)
                    if process.poll() is None:
                        process.kill()
            except Exception as e:
                log_message(f"⚠️ 终止进程异常: {e}")
                try:
                    process.terminate()
                    time.sleep(3)
                    if process.poll() is None:
                        process.kill()
                except Exception:
                    pass
            log_message("✅ 主程序已终止")

        # R2: 启动熔断判定（D-004 根因2）——稳定运行清零计数并解除 R3 去重；
        # 初始化窗口内连续 MAX_INIT_FAILURES 次退出 -> 停止自动重启 + 1 条 critical
        if record_process_exit(time.monotonic() - proc_start):
            log_message(f"🛑 [启动熔断] 主程序连续 {MAX_INIT_FAILURES} 次在 {INIT_FAILURE_WINDOW} 秒内退出，"
                        f"停止自动重启（最后原因: {restart_reason}），请人工排查！")
            breaker_msg = (
                f"🚨【资金安全】🛑 Watchdog 启动熔断：主程序连续 "
                f"{MAX_INIT_FAILURES} 次在 {INIT_FAILURE_WINDOW} 秒内退出，已停止自动重启。"
                f"最后原因: {restart_reason}。请人工排查后再启动。")
            atomic_write_notify(f"crash_alert|{breaker_msg}")
            # R0 / F11：bot_runner 未成功启动 → 队列无人消费；致命信息必须落心跳，
            # 由独立巡检进程发 alert(event="health")，不依赖 bot 存活。
            mark_fatal(breaker_msg)
            _kill_main_process_tree()
            sys.exit(1)

        if restart_reason:
            _heartbeat_state["restarts"] += 1
            _heartbeat_state["last_reason"] = restart_reason
            write_heartbeat()
            log_message(f"🔄 重启原因: {restart_reason}")

            # 🔥 崩溃通知和汇总通知独立发送
            is_crash = "崩溃" in restart_reason or "异常" in restart_reason or "退出码" in restart_reason

            if is_crash:
                # R3: 同因去重（D-004 根因3）——同一 restart_reason 在
                # CRASH_ALERT_DEDUP_WINDOW 秒内只发 1 次，防持续故障通知风暴
                if crash_alert_allowed(restart_reason):
                    try:
                        atomic_write_notify(f"crash_alert|{restart_reason}")
                        log_message(f"💥 已发送崩溃报警: {restart_reason}")
                    except Exception as e:
                        log_message(f"⚠️ 发送崩溃报警失败: {e}")
                else:
                    log_message(f"🔇 崩溃报警同因去重（{CRASH_ALERT_DEDUP_WINDOW}s 窗口内已发）: {restart_reason}")

            if ENABLE_SUMMARY_ON_RESTART:
                # 🔥 D-010 W3：队列模型下 crash_alert 与 summary_restart 为独立事件文件，
                # 互不覆盖（原"检查 .notify 存在防覆盖"的单槽逻辑已无必要，直接入队）
                try:
                    atomic_write_notify(f"summary_restart|重启后持仓汇总")
                    log_message("📊 已请求发送重启后持仓汇总")
                except Exception as e:
                    log_message(f"⚠️ 请求汇总失败: {e}")

            time.sleep(3)

        # 🔥 D-010 W3：通知文件由 bot_runner 队列消费负责删除；此处仅清理可能残留的
        # 临时文件（旧单槽 .notify.tmp + 队列写入中断残留 {event_id}.notify.tmp）
        try:
            legacy_tmp = os.path.join(BASE_DIR, ".notify.tmp")
            if os.path.exists(legacy_tmp):
                os.remove(legacy_tmp)
            queue_dir = os.path.join(BASE_DIR, ".notify_queue")
            if os.path.isdir(queue_dir):
                for leftover in os.listdir(queue_dir):
                    if leftover.endswith(".tmp"):
                        try:
                            os.remove(os.path.join(queue_dir, leftover))
                        except Exception:
                            pass
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_message("👋 用户手动停止 Watchdog")
        _kill_main_process_tree()  # P0-1: 停止必须连带清理 bot_runner 进程树
        mark_stopped("用户手动停止 Watchdog（Ctrl+C）")
        close_bot_log()
        sys.exit(0)
    except Exception as e:
        log_message(f"❌ Watchdog 异常: {e}")
        import traceback

        traceback.print_exc()
        _kill_main_process_tree()  # P0-1: 异常退出同样清理，防孤儿
        close_bot_log()
        sys.exit(1)