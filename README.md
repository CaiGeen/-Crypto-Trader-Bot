# Binance 永续合约量化交易 Bot

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
| **孤儿单清理** | 自动清理不受管理的挂单；无持仓+无挂单的僵尸批次自动归档 |

### 风控与通知
| 特性 | 说明 |
|------|------|
| **告警分级** | `level='critical'` 自动加 🚨【资金安全】前缀，重要告警醒目 |
| **每日结算报告** | 每天 00:05 发送盈亏 + 余额 + 持仓快照到 Telegram |
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
├── watchdog.py           # 守护进程（崩溃重启 + 可选定时重启）
├── parser.py             # JSON 信号解析器
├── quick_trade.py        # 独立快速挂单工具（不与 watchdog 同时运行）
├── main.py               # 简易入口
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
git clone https://github.com/<your-name>/<repo>.git
cd <repo>
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

## 📌 已知限制

| 限制 | 说明 |
|------|------|
| 杠杆限制 | 新币安账户 30 天内最高 20x |
| 仅 BTCUSDT | 当前主要针对 BTC，其他品种需自行测试 |
| JSON 持久化 | 状态文件为 JSON，高并发场景建议换 SQLite |

---

## 🧭 未来规划

- KAMA 跟踪止盈 + 自动保本损（设计已定稿，待实施）
- 多策略同时运行
- Web Dashboard 可视化监控
- 回测系统

---

## 📄 License

本项目仅供学习参考，未指定开源许可证。使用前请评估风险，盈亏自负。
