@echo off
chcp 65001 >nul
title CryptoBot Watchdog (Ctrl+C 停止)
cd /d G:\my-crypto-bot
rem 让 bot_runner 的 print（含 READY 提示）实时透过 watchdog 管道显示，否则会被块缓冲"吞掉"
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
echo ============================================
echo   启动 Watchdog 守护进程（会自动拉起 Bot）
echo   停止方法：在本窗口按 Ctrl+C
echo   不要直接点 X 关窗（会强杀，虽可恢复但不优雅）
echo ============================================
echo.
.venv\Scripts\python.exe watchdog.py
echo.
echo Bot 已退出。按任意键关闭窗口...
pause >nul
