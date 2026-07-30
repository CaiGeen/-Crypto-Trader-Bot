# 🤖 Crypto Trader Bot

一个基于 Python + python-telegram-bot + CCXT 的**加密货币量化交易 Bot**，采用“批次独立风控”架构，支持多层级阶梯挂单、独立止盈止损、实时风控监控，并通过 Telegram 提供完整的交互界面。

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)](https://core.telegram.org/bots)
[![CCXT](https://img.shields.io/badge/CCXT-4.0+-green.svg)](https://github.com/ccxt/ccxt)

---

## 📋 功能特性

### 🚀 核心交易功能
- **多层级阶梯挂单** - 支持做多/做空，自动跳过不合理价格
- **独立批次风控** - 每批次独立止盈止损，互不影响
- **智能止损价校验** - 挂单前自动校验止损价合理性，防止错误挂单
- **预生成止盈止损** - 成交后 1 秒内自动挂出止盈止损单
- **部分减仓自动更新** - 持仓变化时自动调整止盈止损单数量

### 📱 Telegram 交互
- **3 个核心按钮**：保本、平仓、撤单
- **保本损确认** - 点击后显示完整计算详情，确认后执行
- **平仓模式选择** - 市价平仓 / 最优价挂单 / 自定义价格
- **撤单确认** - 防止误触，二次确认后执行
- **完整命令支持** - `/status`、`/summary`、`/signal`、`/be`、`/close`、`/cancel` 等

### 🛡️ 风控与监控
- **独立批次监控线程** - 每批次独立运行，互不干扰
- **熔断机制** - 连续失败 10 次自动暂停 60 秒
- **降级保护** - 新止损单挂单失败时自动恢复旧单
- **启动时自动恢复** - 程序重启后自动恢复活跃批次
- **程序撤单 / 手动撤单区分** - 精准识别，避免误报

### 💰 结算与通知
- **完整的盈亏结算** - 显示持仓均价、平仓价格、手续费、最终净盈亏
- **平仓模式标识** - 显示限价单(Maker) / 市价单(Taker) 及费率
- **防止重复结算** - 智能标记，避免重复消息

### 🛠️ 辅助工具
- **`quick_trade.py`** - 无需 Telegram 的快速挂单工具
- **Watchdog 守护进程** - 崩溃自动重启，保障长期运行

---

## 🏗️ 架构设计

### 核心设计理念

```
批次独立隔离
    ├── 每批次独立挂单
    ├── 每批次独立止盈止损
    ├── 每批次独立监控线程
    └── 批次之间互不影响
```

### 技术栈

| 组件 | 技术 |
|------|------|
| 编程语言 | Python 3.10+ |
| Bot 框架 | python-telegram-bot |
| 交易所 API | CCXT (Binance) |
| 异步处理 | asyncio |
| 状态持久化 | JSON 文件 |

---

## 📁 项目结构

```
my-crypto-bot/
├── bot_runner.py          # Telegram Bot 主程序
├── trader_260725.py       # 交易核心逻辑
├── watchdog.py            # 守护进程（崩溃重启）
├── parser.py              # JSON 信号解析器
├── quick_trade.py         # 快速挂单工具
├── signal.json            # 交易信号配置文件
├── trade_state.json       # 批次状态持久化文件
├── .env                   # 环境变量配置
└── requirements.txt       # 依赖包列表
```

---

## 🚀 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/yourusername/crypto-trader-bot.git
cd crypto-trader-bot
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

创建 `.env` 文件：

```bash
# Telegram Bot 配置
TG_BOT_TOKEN=你的Telegram Bot Token
TG_ALLOWED_USER_ID=你的Telegram用户ID

# Binance API 配置
BINANCE_API_KEY=你的Binance API Key
BINANCE_SECRET=你的Binance Secret

# 代理配置（可选）
BINANCE_PROXY=http://127.0.0.1:7890
```

### 4. 创建交易信号

编辑 `signal.json`：

```json
{
    "symbol": "BTCUSDT",
    "side": "BUY",
    "leverage": 100,
    "entries": [
        {"trigger_price": 64300, "amount": 0.001, "stop_loss": 64000},
        {"trigger_price": 64330, "amount": 0.001, "stop_loss": 64001},
        {"trigger_price": 66200, "amount": 0.001, "stop_loss": 63003},
        {"trigger_price": 66300, "amount": 0.001, "stop_loss": 63304}
    ],
    "take_profit": 66000,
    "initial_stop_loss": 63000
}
```

### 5. 启动程序

**方式一：通过 Watchdog（推荐，长期运行）**

```bash
python watchdog.py
```

**方式二：直接启动 Telegram Bot**

```bash
python bot_runner.py
```

**方式三：快速挂单（无需 Telegram）**

```bash
python quick_trade.py
```

---

## 📱 Telegram 命令

| 命令 | 说明 |
|------|------|
| `/start` | 启动 Bot，显示欢迎信息 |
| `/help` | 显示所有可用命令 |
| `/status` | 查看所有活跃批次状态（含按钮） |
| `/summary` | 查看持仓汇总（含浮动盈亏） |
| `/signal` | 快捷下单（完整参数） |
| `/signal_template` | 使用预设模板下单 |
| `/list_templates` | 查看所有预设模板 |
| `/test` | 生成远离市价的测试挂单 |
| `/be` | 一键保本损 |
| `/close` | 平仓（交互式选择模式） |
| `/cancel` | 取消该批次所有未成交挂单 |

---

## 🎯 交互流程

### 批次卡片按钮

每个活跃批次下方都有 3 个核心按钮：

```
[🔒 保本]  [💰 平仓]  [🗑️ 撤单]
```

| 按钮 | 交互流程 |
|------|----------|
| 🔒 保本 | 点击 → 显示保本计算详情 → 确认/取消 |
| 💰 平仓 | 点击 → 选择平仓方式（市价/最优价/自定义） → 执行 |
| 🗑️ 撤单 | 点击 → 显示待取消层数 → 确认/取消 |

---

## 📊 信号格式说明

### JSON 结构

```json
{
    "symbol": "BTCUSDT",              // 交易对
    "side": "BUY",                    // 方向: BUY / SELL
    "leverage": 100,                  // 杠杆倍数
    "entries": [                      // 阶梯挂单列表
        {
            "trigger_price": 64300,   // 触发价格
            "amount": 0.001,          // 数量
            "stop_loss": 64000        // 该层止损价
        }
    ],
    "take_profit": 66000,            // 止盈价格
    "initial_stop_loss": 63000       // 初始止损（兜底）
}
```

---

## ⚙️ 配置说明

### 环境变量

| 变量 | 说明 | 必填 |
|------|------|------|
| `TG_BOT_TOKEN` | Telegram Bot Token | ✅ |
| `TG_ALLOWED_USER_ID` | 授权用户的 Telegram ID | ✅ |
| `BINANCE_API_KEY` | Binance API Key | ✅ |
| `BINANCE_SECRET` | Binance Secret | ✅ |
| `BINANCE_PROXY` | 代理地址（HTTP/SOCKS5） | ❌ |

### 获取 Telegram Bot Token

1. 在 Telegram 搜索 `@BotFather`
2. 发送 `/newbot` 创建 Bot
3. 复制生成的 Token

### 获取用户 ID

1. 在 Telegram 搜索 `@userinfobot`
2. 发送 `/start`
3. 复制返回的用户 ID

---

## 🛠️ 开发与调试

### 日志

程序日志输出到控制台和 `watchdog.log`：

```bash
tail -f watchdog.log
```

### 状态文件

`trade_state.json` 存储所有活跃批次状态，可用于调试：

```bash
cat trade_state.json | jq '.'
```

---

## 🧪 测试建议

1. **使用测试网** - 在 `.env` 中配置测试网 API
2. **小金额测试** - 使用 0.001 BTC 测试完整流程
3. **观察日志** - 确认每个环节正常
4. **逐步增加** - 确认无误后增加资金

---

## 📝 注意事项

- ⚠️ 程序会自动执行交易，请确保信号参数正确
- ⚠️ 建议先熟悉功能再进行实盘交易
- ⚠️ 市场有风险，请合理控制仓位
- ⚠️ 确保账户有足够保证金，避免强平

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

## 📄 许可证

MIT License

---

## 🔗 相关链接

- [CCXT 文档](https://docs.ccxt.com/)
- [python-telegram-bot 文档](https://docs.python-telegram-bot.org/)
- [Binance API 文档](https://binance-docs.github.io/apidocs/)

---

## 📧 联系方式

如有问题，请提交 [GitHub Issue](https://github.com/yourusername/crypto-trader-bot/issues)
