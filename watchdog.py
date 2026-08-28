#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
watchdog.py - 量化交易 Bot 守护进程
功能：
1. 主程序崩溃时自动重启
2. 每 4 小时在整点自动重启（北京时间）【可通过开关控制】
3. 重启通知发送到 TG
"""

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
    print(log_entry.strip())
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception:
        pass


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
        return process
    except Exception as e:
        log_message(f"❌ 启动主程序失败: {e}")
        return None


def monitor_process(process):
    """监控主程序输出"""
    try:
        for line in process.stdout:
            print(line, end='')
            if "CRASH" in line or "FATAL" in line or "Unhandled exception" in line:
                log_message("⚠️ 检测到主程序异常，准备重启")
                return False
    except Exception as e:
        log_message(f"⚠️ 监控输出异常: {e}")
        return False
    return True


# ==================== 主循环 ====================
def main():
    make_stdout_crash_safe()  # R1: 入口级编码保护（D-004 根因1）
    log_message("=" * 60)
    log_message("🛡️ Watchdog 守护进程启动 (稳定版 v2.2)")
    log_message(f"📁 工作目录: {BASE_DIR}")
    log_message(f"📄 主程序: {os.path.basename(MAIN_SCRIPT)}")
    log_message(f"⏱️  崩溃自动重启: ✅ 启用")
    log_message(f"⏱️  定时重启: {'✅ 启用' if ENABLE_SCHEDULED_RESTART else '❌ 禁用'}")
    log_message(f"📊 重启后汇总: {'✅ 启用' if ENABLE_SUMMARY_ON_RESTART else '❌ 禁用'}")
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
            atomic_write_notify(
                f"crash_alert|🚨【资金安全】🛑 Watchdog 启动熔断：主程序连续 "
                f"{MAX_INIT_FAILURES} 次在 {INIT_FAILURE_WINDOW} 秒内退出，已停止自动重启。"
                f"最后原因: {restart_reason}。请人工排查后再启动。")
            _kill_main_process_tree()
            sys.exit(1)

        if restart_reason:
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
        sys.exit(0)
    except Exception as e:
        log_message(f"❌ Watchdog 异常: {e}")
        import traceback

        traceback.print_exc()
        _kill_main_process_tree()  # P0-1: 异常退出同样清理，防孤儿
        sys.exit(1)