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
import subprocess
import threading
from datetime import datetime, timedelta
import pytz

# ==================== 配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(BASE_DIR, "bot_runner.py")
LOG_FILE = os.path.join(BASE_DIR, "watchdog.log")

# 🔥 定时重启开关
# True = 启用定时重启（每 4 小时整点重启）
# False = 禁用定时重启（只保留崩溃自动重启）
ENABLE_SCHEDULED_RESTART = False  # 默认关闭
# 🔥 新增：重启后发送持仓汇总
ENABLE_SUMMARY_ON_RESTART = True  # 默认启用

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


def send_tg_notification(text: str):
    try:
        notify_file = os.path.join(BASE_DIR, ".notify")
        with open(notify_file, "w", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()}|{text}")
    except Exception:
        pass


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
                restart_reason = f"⚠️ 程序异常退出 (退出码: {process.returncode})"
                log_message(restart_reason)
                break

            time.sleep(1)

        stop_monitor.set()
        monitor_thread.join(timeout=2)

        if process.poll() is None:
            log_message("🛑 正在终止主程序...")
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

            if ENABLE_SUMMARY_ON_RESTART:
                try:
                    notify_file = os.path.join(BASE_DIR, ".notify")
                    with open(notify_file, "w", encoding="utf-8") as f:
                        f.write(f"{datetime.now().isoformat()}|summary_restart")
                    log_message("📊 已请求发送重启后持仓汇总")
                except Exception as e:
                    log_message(f"⚠️ 请求汇总失败: {e}")
            elif "崩溃" in restart_reason or "异常" in restart_reason:
                send_tg_notification(restart_reason)

            time.sleep(3)

        try:
            notify_file = os.path.join(BASE_DIR, ".notify")
            if os.path.exists(notify_file):
                os.remove(notify_file)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log_message("👋 用户手动停止 Watchdog")
        sys.exit(0)
    except Exception as e:
        log_message(f"❌ Watchdog 异常: {e}")
        sys.exit(1)