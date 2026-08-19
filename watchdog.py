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


def atomic_write_notify(content: str):
    """原子方式写入 .notify 文件（只写不删，由 bot_runner 负责删除）"""
    try:
        notify_file = os.path.join(BASE_DIR, ".notify")
        tmp_file = notify_file + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_file, notify_file)
        log_message(f"📝 已写入 .notify")
    except Exception as e:
        log_message(f"⚠️ 原子写入 .notify 失败: {e}")


def send_tg_notification(text: str):
    try:
        atomic_write_notify(f"{datetime.now().isoformat()}|{text}")
    except Exception as e:
        log_message(f"⚠️ 发送通知失败: {e}")


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

        if restart_reason:
            log_message(f"🔄 重启原因: {restart_reason}")

            # 🔥 崩溃通知和汇总通知独立发送
            is_crash = "崩溃" in restart_reason or "异常" in restart_reason or "退出码" in restart_reason

            if is_crash:
                try:
                    atomic_write_notify(f"crash_alert|{restart_reason}")
                    log_message(f"💥 已发送崩溃报警: {restart_reason}")
                except Exception as e:
                    log_message(f"⚠️ 发送崩溃报警失败: {e}")

            if ENABLE_SUMMARY_ON_RESTART:
                # 如果 crash_alert 已写入 .notify，不覆盖它
                notify_file = os.path.join(BASE_DIR, ".notify")
                if not os.path.exists(notify_file):
                    try:
                        atomic_write_notify(f"summary_restart|重启后持仓汇总")
                        log_message("📊 已请求发送重启后持仓汇总")
                    except Exception as e:
                        log_message(f"⚠️ 请求汇总失败: {e}")
                else:
                    log_message("📊 crash_alert 已写入，跳过 summary_restart")

            time.sleep(3)

        # 🔥 注意：watchdog 不再删除 .notify 文件
        # 由 bot_runner 读取后负责删除
        # 只清理可能残留的临时文件
        try:
            tmp_file = os.path.join(BASE_DIR, ".notify.tmp")
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
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