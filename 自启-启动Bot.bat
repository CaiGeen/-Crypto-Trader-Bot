@echo off
chcp 65001 >nul
title CryptoBot 开机自启（等待代理 → 启动 Bot）
cd /d G:\my-crypto-bot
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8

echo ============================================
echo   CryptoBot 开机自启
echo   第 1 步：等待本地代理就绪（最多 5 分钟）
echo   原因：币安 API 与 Telegram 均经该代理；代理未就绪就启动
echo         会导致"恢复链失败 → 持仓批次失管"，故必须先等。
echo ============================================
echo.

.venv\Scripts\python.exe 健康巡检.py --wait-proxy 300
if errorlevel 1 goto proxy_dead

echo.
echo 代理已就绪，转入常规启动流程...
echo.
call "%~dp0启动Bot.bat"
exit /b 0

:proxy_dead
echo.
echo ❌ 代理在 5 分钟内未就绪 —— 已放弃本次自启（避免无代理启动造成批次失管）。
echo    请先启动你的代理软件，然后手动双击 启动Bot.bat。
echo.
.venv\Scripts\python.exe 健康巡检.py --notify-proxy-down
echo.
echo 按任意键关闭本窗口...
pause >nul
