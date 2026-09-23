<#
安装开机自启.ps1 —— 注册 / 卸载 CryptoBot 计划任务（2026-09-23 生产运维 #5）

注册两个任务：
  CryptoBot-Autostart     登录后 1 分钟 → 自启-启动Bot.bat（先等本地代理就绪再拉 bot）
  CryptoBot-HealthPatrol  登录时 + 每 15 分钟 → 健康巡检.py（pythonw 静默；异常才告警）

关键点：ExecutionTimeLimit 必须为 0（无限）——否则 Windows 默认 3 天后会 Kill 任务，
        把长驻的 bot 一起杀掉（静默停机，且心跳会随即陈旧由巡检发现）。

用法（需管理员 PowerShell）：
  安装：powershell -ExecutionPolicy Bypass -File "G:\my-crypto-bot\安装开机自启.ps1"
  卸载：powershell -ExecutionPolicy Bypass -File "G:\my-crypto-bot\安装开机自启.ps1" -Uninstall
#>
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$proj = 'G:\my-crypto-bot'
$user = "$env:COMPUTERNAME\$env:USERNAME"
$names = @('CryptoBot-Autostart', 'CryptoBot-HealthPatrol')

if ($Uninstall) {
    foreach ($n in $names) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "已卸载任务: $n"
        } else {
            Write-Host "任务不存在，跳过: $n"
        }
    }
    exit 0
}

if (-not (Test-Path $proj)) { throw "项目目录不存在: $proj" }
if (-not (Test-Path "$proj\自启-启动Bot.bat")) { throw "缺少 自启-启动Bot.bat" }
if (-not (Test-Path "$proj\健康巡检.py")) { throw "缺少 健康巡检.py" }

# 公共设置：无时间上限 / 电池不中断 / 已运行则忽略新实例 / 错过的触发尽快补跑
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable
$settings.ExecutionTimeLimit = 'PT0S'     # 0 = 无限制（务必保留！）
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest

# ---------- 任务 1：开机自启 ----------
$trig1 = New-ScheduledTaskTrigger -AtLogOn -User $user
$trig1.Delay = 'PT1M'
$act1 = New-ScheduledTaskAction -Execute 'cmd.exe' `
    -Argument "/c `"$proj\自启-启动Bot.bat`"" -WorkingDirectory $proj
Register-ScheduledTask -TaskName 'CryptoBot-Autostart' -Action $act1 -Trigger $trig1 `
    -Settings $settings -Principal $principal -Force `
    -Description 'CryptoBot 开机自启：登录后 1 分钟，先等本地代理就绪再拉起 watchdog+bot，避免恢复链失败导致批次失管' | Out-Null
Write-Host "已注册: CryptoBot-Autostart（登录后 1 分钟触发）"

# ---------- 任务 2：健康巡检 ----------
$trig2a = New-ScheduledTaskTrigger -AtLogOn -User $user
$trig2b = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$act2 = New-ScheduledTaskAction -Execute "$proj\.venv\Scripts\pythonw.exe" `
    -Argument "`"$proj\健康巡检.py`"" -WorkingDirectory $proj
Register-ScheduledTask -TaskName 'CryptoBot-HealthPatrol' -Action $act2 `
    -Trigger @($trig2a, $trig2b) -Settings $settings -Principal $principal -Force `
    -Description 'CryptoBot 健康巡检：每 15 分钟读 .heartbeat.json，守护链陈旧/代理不可达/bot 不存活时 TG+邮件告警（正常完全静默）' | Out-Null
Write-Host "已注册: CryptoBot-HealthPatrol（登录时 + 每 15 分钟）"

Write-Host ''
Write-Host '--- 当前状态 ---'
Get-ScheduledTask -TaskName $names | Select-Object TaskName, State | Format-Table -AutoSize
