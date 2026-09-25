# Binance 永续合约量化交易 Bot

> 🗄️ **项目已归档（2026-09-23）**：本仓库冻结于 `frozen-archive` 分支，功能开发已停止。
> 当前为经**真金实盘演练验证**的封板版（v6.5），作者自用实例 7×24 运行中，此后仅做应急修复，不再迭代新功能。
> 归档前最后一轮安全收口详见下方「🛠️ 归档封板修补」章节。

> 基于 Python + CCXT + python-telegram-bot 的加密货币量化交易机器人，运行于 Binance USDM 永续合约。
> 采用 **"批次独立风控"** 架构：多层级阶梯挂单、独立止盈止损、后台实时风控监控，支持 Telegram 远程操控。

> ⚠️ **重要声明**：本项目用于学习与技术交流。加密货币交易存在高风险，使用本项目产生的任何资金损失由使用者自行承担。请使用小资金测试，严禁将 API 密钥提交到公开仓库。

---

## ✨ 功能特性

### 交易核心（`trader_260725.py`）
| 特性 | 说明 |
|------|------|
| **批次独立风控** | 每个批次独立管理入场、止盈、止损，互不干扰 |
| **多层级阶梯挂单** | 最多 4 层条件单（STOP_MARKET），分批进场摊薄成本 |
| **独立止盈止损** | 每层独立止损，整体止盈（TAKE_PROFIT_MARKET），非对冲模式全部带 `reduceOnly` 防超量 |
| **状态持久化** | `trade_state.json` 原子写入（tempfile + os.replace），支持断线恢复 |
| **双向持仓** | 支持 Hedge Mode（同时持有多空） |
| **部分减仓兼容** | 部分减仓/新层成交后自动重建保护单，**先挂新、再撤旧**，无保护空窗期 |
| **手动操作兼容** | 在交易所 App 手动撤单/平仓会被自动检测，程序正确终止或重建批次 |
| **盈亏记录** | `trade_stats.json` 记录每笔已实现盈亏，按日期生成日报 |
| **API 限流** | 信号量串行化 + 全局熔断 + 429/418 指数退避，防封号 |
| **孤儿单守卫** | 检测到不受管理的挂单时**只告警 + 阻断新批次，绝不自动撤单**（Fail-Closed，v6.5 起）；无持仓+无挂单的僵尸批次自动归档 |

### 风控与通知
| 特性 | 说明 |
|------|------|
| **告警分级** | `level='critical'` 自动加 🚨【资金安全】前缀，重要告警醒目 |
| **每日结算报告** | 每天 08:05（北京时间）发送昨日盈亏 + 余额 + 持仓快照到 Telegram |
| **IP 监控** | 自动检测公网 IP 变化并通知（TG + `.notify` 备用文件双通道） |
| **止损价校验** | 只校验入场价 vs 止损价合理性，不依赖市价 |

### 守护进程（`watchdog.py`）
| 特性 | 说明 |
|------|------|
| **崩溃自动重启** | 主程序异常退出后自动拉起 |
| **可选定时重启** | 默认关闭，可配置每 4 小时整点重启 |
| **重启汇总** | 重启后自动发送持仓汇总到 TG |

---

## 📁 项目结构

```
├── trader_260725.py      # 交易核心引擎（CryptoTrader 类）
├── bot_runner.py         # Telegram Bot 主程序（命令/按钮/通知）
├── watchdog.py           # 守护进程（崩溃重启 + 可选定时重启 + 交易日志落盘 + 心跳）
├── parser.py             # JSON 信号解析器
├── quick_trade.py        # 独立快速挂单工具（不与 watchdog 同时运行）
├── main.py               # 简易入口
├── 启动Bot.bat           # 一键启动（含 PYTHONUNBUFFERED，实时输出不被管道缓冲吞掉）
├── 停止Bot-应急.bat      # 应急强杀进程树（按命令行过滤，不误伤无关 python 进程）
├── 自启-启动Bot.bat      # 开机自启入口：先等本地代理就绪，再拉起 bot
├── 健康巡检.py           # 死人心跳巡检（读 .heartbeat.json；异常才告警，正常静默）
├── 安装开机自启.ps1      # 注册/卸载计划任务（-Uninstall 可卸载）
├── logs/                 # 运行时：bot_YYYYMMDD.log（交易全过程）+ patrol.log（巡检）
├── .heartbeat.json       # 运行时：watchdog 心跳（每 60s 原子刷新）
├── requirements.txt      # Python 依赖
├── .env.example          # 环境变量模板（复制为 .env 后填写）
└── .gitignore            # 已排除密钥/日志/状态文件
```

---

## 🚀 快速开始

### 1. 环境要求
- Python 3.10+
- Binance 账户（开通 USDM 永续合约）
- Telegram Bot Token（通过 @BotFather 创建）

### 2. 安装

```bash
git clone https://github.com/CaiGeen/-Crypto-Trader-Bot.git
cd ./-Crypto-Trader-Bot
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. 配置

```bash
cp .env.example .env
```

编辑 `.env`：

```ini
# Binance API（开通永续合约权限，仅开启交易权限）
BINANCE_API_KEY=your_binance_api_key
BINANCE_SECRET=your_binance_secret
# 代理（网络需要时）
BINANCE_PROXY=http://127.0.0.1:7890
# Telegram
TG_BOT_TOKEN=your_telegram_bot_token
TG_ALLOWED_USER_ID=your_telegram_user_id
# 邮件告警开关（Telegram 不受影响）
EMAIL_ALERT_ENABLED=true
# true=仅在本地 trade_state.json 有活跃批次时发邮件；状态不可读时不发邮件
EMAIL_ALERT_ONLY_WITH_POSITION=false
```

### 4. 启动

```bash
# 方式一：完整运行（推荐，watchdog 守护）
python watchdog.py

# 方式二：仅 Telegram Bot（无守护）
python bot_runner.py
```

---

## 📡 信号格式

通过 Telegram 发送 JSON 文本或上传 `.json` 文件即可下单：

```json
{
  "symbol": "BTCUSDT",
  "side": "BUY",
  "leverage": 20,
  "entries": [
    {"trigger_price": 65918.9, "amount": 0.478, "stop_loss": 65358.0},
    {"trigger_price": 66059.9, "amount": 0.429, "stop_loss": 65591.7},
    {"trigger_price": 66201.2, "amount": 0.285, "stop_loss": 65732.0},
    {"trigger_price": 66342.7, "amount": 0.129, "stop_loss": 65825.4}
  ],
  "take_profit": 72312.0,
  "initial_stop_loss": 65358.0
}
```

| 字段 | 说明 |
|------|------|
| `symbol` | 交易对，如 `BTCUSDT` |
| `side` | `BUY` / `SELL` |
| `leverage` | 杠杆倍数 |
| `entries[]` | 阶梯入场：`trigger_price` 触发价、`amount` 数量、`stop_loss` 该层止损 |
| `take_profit` | 整体止盈价 |
| `initial_stop_loss` | 初始止损价（整体兜底） |

---

## 🤖 Telegram 命令

| 命令 | 说明 |
|------|------|
| `/signal <JSON>` | 按信号下单 |
| `/test <SYMBOL> <SIDE> <AMOUNT>` | 生成远离市价的测试挂单 |
| `/be <batch_id>` | 一键保本损 |
| `/close <batch_id>` | 交互式平仓 |
| `/cancel <batch_id>` | 取消未成交挂单 |
| `/status` | 查看活跃批次 |
| `/summary` | 查看持仓汇总 |
| `/system` | 查看系统运行状态 |
| `/help` | 帮助 |

批次卡片支持 **[保本] [平仓] [撤单]** 按钮交互。

---

## 🏗️ 核心架构

### 批次生命周期

```
收到信号 → 防冲突扫描 → 设置杠杆 → 校验价格/资金
  → 挂 N 层条件单（STOP_MARKET）→ 注册独立监控线程
  → 每层成交后自动挂该层止损 + 整体止盈
  → 触发止盈/止损/手动平仓 → 结算 → 状态归档
```

### 监控与恢复

- 每个批次一个独立监控线程（交易所返回 symbol 级持仓，程序内部按批次映射）
- 启动时 `recover_active_batches()` 恢复历史未完成任务
- 状态文件原子写入，崩溃不损坏数据
- API 调用全局限流（Semaphore(1)）+ 全局熔断 + 429/418 退避

### 关键方法

| 方法 | 功能 |
|------|------|
| `execute_signal()` | 执行信号挂单 |
| `_start_monitoring()` | 批次后台监控循环 |
| `_safe_api_call()` | 带限流/熔断/退避的 API 调用 |
| `_replace_protective_sl()` | 保护单替换（先挂新再撤旧，无空窗） |
| `close_position_market()` | 市价平仓 |
| `close_position_limit()` | 限价平仓 |
| `set_breakeven_sl()` | 保本损 |

---

## 🔒 安全说明

1. **API 密钥只存在于 `.env`**（已被 `.gitignore` 排除），代码通过环境变量读取。
2. **切勿修改 `.gitignore` 放行密钥文件**。GitHub 会自动扫描并告警泄露的密钥。
3. 建议 Binance API Key 仅开启 **合约交易权限**，关闭提现权限。
4. 若密钥曾泄露（如误传仓库），请立即在币安后台**删除重建**。

---

## 🛠️ 归档封板修补（v6.5 · 2026-09-22/23）

归档前最后一轮安全收口，全部经**最小资金真金实盘演练**验证：开仓 → 手动撤 SL/TP **3 秒内自动补挂** → 孤儿单**只告警不触碰** → 外部手动平仓**自动结算 + 墓碑防复活** → 强杀进程后 watchdog 重启**完整恢复挂单监控**，全链路绿灯。

| # | 修补 | 说明 |
|---|------|------|
| 1 | **SG2 风险闸门按方向计量** | Hedge 模式下台账只累计本方向批次（对齐守恒冲突检测口径），`side` 必填、缺失即抛错，杜绝「LONG 持仓 vs 双方向台账」的误拒 |
| 2 | **Hedge Mode 检测前置 + Fail-Closed** | 持仓模式查询提前到持仓快照之前；检测失败直接阻断开仓，不再"默认单向持仓"猜测账户模式 |
| 3 | **孤儿单守卫收口** | ① known 集合补齐（`limit_close_order_id` + `protection_registry` 内订单），程序自己的 LIMIT 平仓单/已收编保护单不再被误判为孤儿；② unknown 挂单一律**不自动撤**，只分类告警 + 阻断新批次（用户裁定——旧"自动清理"逻辑走条件单通道，会精确误撤用户手工 SL/TP） |
| 4 | **阶梯止损单调性校验** | BUY 时 SL 序列必须逐层不降、SELL 逐层不升，杜绝成交新层后止损反向放宽 |
| 5 | **配套测试** | 更新 `test_v64_diag_fixes.py` / `tests_archive/test_sg2_risk_gate.py` / `tests_archive/test_tg_fallback.py`，新增 `test_transition_safety_gate.py` |
| 6 | **运维加固** | `.gitignore` 封堵大二进制 / 状态备份 / 运行残留的误提交；新增 `启动Bot.bat` / `停止Bot-应急.bat`（watchdog 一键启停，无缓冲实时输出） |

---

## 🩺 生产运维加固（2026-09-23 · 实盘上线后）

> 上线后全量复审识别出两个运维缺口：① 交易全过程只存在于控制台与 PIPE 转发，窗口一关或机器一重启就**无法取证**（8-19 二次 418 事故正是卡在"无落盘数据源"）；② 机器重启后无人叫醒，最长盲区可达一天（仅靠 08:05 日报"缺席"被动暗示）。已补齐下列能力，**未改动任何交易逻辑**（`watchdog.py` 为 155 行纯新增）。

| # | 能力 | 实现 | 验收证据 |
|---|------|------|----------|
| 1 | **交易日志落盘** | watchdog 在转发子进程 stdout 的同一处按天落盘 `logs/bot_YYYYMMDD.log`；逐行 flush（断电最多丢一行）；保留 14 天自动清理 | 重启后日志持续增长，含 `READY`、批次接管、每分钟 `[限流观测]` 全过程 |
| 2 | **心跳（死人心跳）** | watchdog 每 60s 原子写 `.heartbeat.json`（watchdog_pid / bot_pid / bot_alive / restarts / last_reason / stopped） | 每 60s 刷新且 `bot_alive=true`；用户主动停止写 `stopped=true`，避免停机后误报 |
| 3 | **独立健康巡检** | `健康巡检.py`（纯标准库，venv 损坏也能报警）：计划任务每 15 分钟读心跳，**心跳陈旧 / bot 持续不存活 / 代理不可达**即告警；正常时完全静默（零 TG 流量） | 巡检日志每 15 分钟一条"巡检正常"；通道自检 TG经代理 / TG直连 / 邮件 三通道全 True |
| 4 | **开机自启（先代理后 bot）** | `自启-启动Bot.bat` 先等 `127.0.0.1:7890` 就绪（最多 5 分钟）再拉起 bot；代理始终未就绪则**放弃启动并邮件告警**——避免"无代理启动 → 恢复链失败 → 持仓批次无人监控" | 计划任务 `CryptoBot-Autostart` 实测拉起成功，`schtasks /Run` 可手动触发 |
| 5 | **告警三通道 + 去重** | 巡检告警链路：TG（经代理）→ TG（直连）→ QQ 邮件（SMTP 国内直连，代理挂掉仍可达）；同一问题 30 分钟内只告警一次 | 自检实测三通道全部送达 |

```powershell
# 任务状态 / 手动触发自启 / 重新注册或卸载（需管理员）
schtasks /Query /TN "CryptoBot-Autostart" /FO LIST
schtasks /Run   /TN "CryptoBot-Autostart"
powershell -ExecutionPolicy Bypass -File "安装开机自启.ps1"
powershell -ExecutionPolicy Bypass -File "安装开机自启.ps1" -Uninstall
# 巡检三通道自检（结果写入 logs\patrol.log）
.venv\Scripts\python.exe 健康巡检.py --selftest
```

> ⚠️ 关键约束：计划任务的 `ExecutionTimeLimit` 必须为 **0（无限制）**，否则 Windows 默认 3 天后会 Kill 任务、连带杀掉长驻的 bot（`安装开机自启.ps1` 已显式设置）。

---

## 📌 已知限制

| 限制 | 说明 |
|------|------|
| 杠杆限制 | 新币安账户 30 天内最高 20x |
| 仅 BTCUSDT | 当前主要针对 BTC，其他品种需自行测试 |
| JSON 持久化 | 状态文件为 JSON，高并发场景建议换 SQLite |
| **代理出口 IP 非独享** | bot 全部流量（币安 API + TG）经 `BINANCE_PROXY` 出口，而该出口是**机场订阅节点**：权重池与同节点用户共享（自身实测仅占 ~1.7%，418 多由"邻居"打爆），且节点 IP 漂移会触发币安 API **IP 白名单不匹配 → `-2015` → AUTH_BLOCKED 盲区**（暂停全部 API）。**计划迁云后换独享固定出口**（自建 VPS） |
| 邮件兜底延迟 | QQ SMTP 首次发送约 30s（TLS 握手 + 登录），属非实时通道；TG 为主通道 |
| 强平价未监控 | 无 `liquidationPrice` 采集。当前参数下安全裕度充足（全仓 19.3k 权益 vs 145.8k 名义 → 强平距离 ≈12.8%，止损距离 ≈1.13%，安全系数 ≈8.8×）；若未来放大单批规模或权益降至 ~2.2k 以下需重新评估 |

---

## 🧭 未来规划（已冻结）

项目已归档，以下规划**不再实施**，仅作设计记录留存：

- KAMA 跟踪止盈 + 自动保本损（设计已定稿）
- 多策略同时运行
- Web Dashboard 可视化监控
- 回测系统

---

## 📄 License

本项目仅供学习参考，未指定开源许可证。使用前请评估风险，盈亏自负。
