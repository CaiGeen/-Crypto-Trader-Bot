@echo off
chcp 65001 >nul
title 应急停止 CryptoBot
echo 正在强杀 Watchdog + Bot 整个进程树...
echo（正常情况请优先在 Watchdog 窗口按 Ctrl+C，本脚本仅应急用）
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'watchdog\.py|bot_runner\.py' } | ForEach-Object { Write-Host ('kill PID ' + $_.ProcessId); taskkill /F /T /PID $_.ProcessId }"
echo.
echo 完成。重新启动：双击 启动Bot.bat（Bot 会自动恢复挂单/持仓监控）
pause
