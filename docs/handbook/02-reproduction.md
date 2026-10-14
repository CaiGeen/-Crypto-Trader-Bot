# 环境、配置与安全复现

[返回总览](README.md) · [下一篇：业务操作](03-business.md)

## 1. 先区分四种环境

| 环境 | 凭证/网络 | 可以做什么 | 不能声称什么 |
|---|---|---|---|
| 离线开发 checkout | 无真实凭证；阻断外发 | 解析、纯规则、mock 场景、正式测试集合 | 已验证实盘执行/通知送达 |
| 授权只读联机 | 用户提供的安全访问方式；真实查询 API | 核对账户模式、成交、订单、Income | 零 API 成本或允许下单 |
| 测试账户/测试网 | 单独账户/测试密钥/已核实端点 | 网络与订单适配实验 | 测试网与实盘行为一定完全相同 |
| 实盘生产 | 真实资金与权限 | 经明确授权的业务执行 | 因“快速测试”默认拥有任何操作权限 |

本项目没有一个在 `.env` 写 `DRY_RUN=true` 就能让所有入口无副作用的统一模拟模式。
`CryptoTrader(is_demo=True)` 有历史测试网端点分支，但 Telegram 主入口与 quick_trade
明确传 `is_demo=False`，且不能仅凭该历史 URL 分支保证 CCXT 当前所有端点都是 sandbox。
若需要测试网，应单独审查 endpoint 覆盖与账户隔离，不在生产脚本里临时改一下布尔值。

## 2. 复现的外部前提

### 离线复现所需

- Windows 10/11 或相应 Windows Server；本项目互斥体/生产探测主要验证在 Windows。
- Python：推荐先用已验证的 3.11 系列；原验证环境为 3.11.1。
- Git、可创建 venv 的工作目录，以及能安装依赖的受控网络或离线 wheel 来源。
- 完整仓库（含现役测试调用的附件 helper），不是只复制三个主程序。

### 实盘/联机另外需要

- Binance USDⓈ-M 账户、正确权限、预期账户持仓模式和实际可用余额。
- 正确出口网络及按需配置的代理；API IP 白名单与实际出口相匹配。
- 自建 Telegram Bot、授权用户 ID、无冲突的第二个 getUpdates 客户端。
- 若启用邮件：QQ 邮箱授权码和 STARTTLS 服务可达。
- 确切的生产状态材料、版本 manifest、回滚方案和运行授权。

源码不附带生产 `.env`、账户状态、密钥或完整市场缓存；这些材料不能从公开仓库“自动生成”。

## 3. 确认版本和 checkout

在新目录中执行，而不是进入既有生产目录：

```powershell
git clone --branch backtest/markprice-4h-fetch https://github.com/CaiGeen/-Crypto-Trader-Bot.git crypto-bot-lab
Set-Location .\crypto-bot-lab
git status --short --branch
git log -1 --oneline
git rev-parse HEAD
```

为复现 PR #23 合并后的那个基线，可在**这个独立实验 clone**中检出
`df9fe0f6204a3e2740fb28e73c6d2ee93e27f66d`。本套指南可能需要从其文档分支单独阅读；
不要因为切到旧 commit 没有指南就复制生产文件进来。

不要默认 `main` 最新，也不要把 checkout 中现存的未提交文件当成你的改动。
如果已有别人工作或 open PR，先征求新 worktree 隔离，不覆写。

## 4. 新建 Python 环境

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pytest pytest-subtests
.\.venv\Scripts\python.exe -m pip check
```

`requirements.txt` 是运行依赖，并非完全锁定的测试依赖集合：

| 依赖 | 仓库要求 | 说明 |
|---|---|---|
| CCXT | `==4.5.68` | 固定版本对普通单/algo 适配特别重要 |
| python-telegram-bot | `>=22.8` | 有下限但无上限，未来版本需回归确认 |
| python-dotenv | `>=1.2.2` | 运行配置解析 |
| pytz | `>=2024.1` | 北京时间与报表时间处理 |
| HTTPX | `>=0.28.1` | PTB HTTP 请求 |
| pytest、pytest-subtests | 开发安装 | 正式可收集测试与 subtests |

如果要高度复现历史测试结果，可另外把 PTB 固定到 22.8，记录其余实际安装版本。
不要随意升级 CCXT 来“修复网络”或在生产 venv 安装测试依赖。

版本记录只写到实验目录，例如：

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip freeze > lab-dependencies.txt
```

依赖 freeze 文件需审阅后再归档：避免其中私人包源/路径等信息泄露。

## 5. 离线环境：先不要创建 `.env`

干净 checkout 的测试应在没有机器本地 `.env` 时可运行。
不要为了让一个配置测试通过，把生产 `.env` 复制进实验目录。

清除测试进程可能继承的凭证与代理变量；这不是改生产服务的环境：

```powershell
$names = @(
  'BINANCE_API_KEY','BINANCE_SECRET','BINANCE_PROXY',
  'TG_BOT_TOKEN','TG_ALLOWED_USER_ID',
  'QQ_MAIL_USER','QQ_MAIL_AUTH_CODE','QQ_MAIL_TO',
  'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY',
  'http_proxy','https_proxy','all_proxy'
)
foreach ($name in $names) {
  [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
```

无凭证仍然可能调用公开 API；关闭邮件也不能阻止所有外发。
强隔离推荐独立 Windows VM/测试用户与网络策略，或对测试 Python 可执行文件做专属外发阻断。
**不要对生产 Python/代理做全机网络封锁。** `egress_tripwire.py` 提供进程内测试绊线，
它能覆盖相应 socket/SMTP 路径，但不是所有子进程和任意扩展的 OS 沙箱。

## 6. 零网络的最小“程序能理解信号”演示

以下只调用 parser，不构造 trader，不启动 Application，不发送 Telegram：

```powershell
@'
from parser import parse_signal_from_dict

signal = parse_signal_from_dict({
    "symbol": "BTCUSDT",
    "side": "BUY",
    "leverage": 2,
    "entries": [
        {"trigger_price": 80000, "amount": 0.001, "stop_loss": 79000}
    ],
    "take_profit": 82000,
    "initial_stop_loss": 79000
})
assert signal.side == "BUY"
assert len(signal.entries) == 1
print("解析通过；未执行下单：", signal.batch_id)
'@ | .\.venv\Scripts\python.exe -
```

价格/数量仅为语法例子，不能复制进真实 Telegram 作为推荐交易。

## 7. 有边界的相关离线测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  test_bot_runner_config.py test_tg_diagnostics.py `
  test_health_progress.py test_pnl_accounting.py test_smtp_transport.py
```

这些用例分别覆盖配置、被动诊断、健康协议、纯对账规则和假 SMTP。
**不要删除后面的明确文件清单变成裸 pytest**：回测目录存在 import 时取市场数据的脚本。
测试真实请求路径的外发安全，必须提前检查 mock/绊线以及构造阶段。

脚本式测试通常需要直接跑，例如：

```powershell
.\.venv\Scripts\python.exe test_p1_settlement_price.py
.\.venv\Scripts\python.exe test_heartbeat_isolation.py
```

直接执行会使用真实交易类方法，但依赖 fake exchange/临时账本的测试装配。
不得把 `CryptoTrader` 的 mock 构造替换成真实密钥以“更贴近生产”。

## 8. 完整正式门禁

在已确认隔离和无 Bot 运行的测试环境：

```powershell
.\.venv\Scripts\python.exe run_test_gate.py --strict
$LASTEXITCODE
```

它先进行 fail-closed 进程探测，再运行指定 pytest 忽略集合及根目录脚本。
如果检测到真实 Bot 或无法确认，将返回 3，**没有执行测试**；不要自动追加 `--allow-live`。
Windows 具名互斥体会跨 worktree 起作用，不能通过新文件夹骗过单实例语义。
非 Windows 的完整门禁进程探测当前不是已验证的替代路线。

完整解释、基线与跳过项见 [测试手册](04-operations.md#3-正式测试门禁)。

## 9. 联机配置：只有批准以后才做

复制 `.env.example` 到**批准的目标 checkout**；用本地编辑器填写，禁止把真实内容贴给 AI。
建议最初邮件关闭，待明确授权后才做通知通道测试。

| 键 | 当前源码/默认 | 操作说明 |
|---|---|---|
| `BINANCE_API_KEY` / `BINANCE_SECRET` | trader 从配置读取 | 权限最小化，禁提现；启动可能签名查账户 |
| `BINANCE_PROXY` | 可选 | 同时影响交易所与 Telegram 主请求，不是只影响 Binance |
| `TG_BOT_TOKEN` | 主入口要求项目 `.env` 有效 | main 不允许只使用继承的 Telegram 凭证 |
| `TG_ALLOWED_USER_ID` | 数字格式 | 鉴权比较 effective user ID，不要仅凭某聊天窗口可见判断权限 |
| `MAX_LEVERAGE` | 代码默认 100；模板示例 20 | 模板值与代码 fallback 不同；不是风险建议 |
| `RISK_MAX_ACTIVE_BATCHES` | 默认 3 | `<=0` 代表禁用，不是“最安全零仓位” |
| `RISK_MAX_ACTIVE_SYMBOLS` | 默认 1 | `<=0` 代表禁用 |
| `RISK_DAILY_REALIZED_LOSS_LIMIT` | 默认 0 | 0 禁用；亏损闸门还受权威证据可用性影响 |
| `IP_CHECK_ENABLED` | 默认 true | trader 普通 IP 观察，可产生联网与本地状态 |
| `EMAIL_ALERT_ENABLED` | 默认 true | 邮件总开关；false 禁止邮件，仍可能 TG/网络 |
| `DAILY_REPORT_EMAIL_ENABLED` | 默认 true | 日报独立开关，还受总开关约束 |
| `EMAIL_ALERT_ONLY_WITH_POSITION` | 兼容保留 | 当前政策不靠它放行普通邮件，模板/老注释有历史描述 |
| `QQ_MAIL_USER` / `QQ_MAIL_AUTH_CODE` / `QQ_MAIL_TO` | 可选 | 用户/授权码/收件人，收件人缺省可用发件人 |
| `EGRESS_IP_CHECK` | 巡检默认 true | 独立出口核验，可能签名查询 |
| `EGRESS_IP_VERIFY_TTL_SECONDS` | 21600 | 核验缓存时间，不是永久白名单保证 |
| `EGRESS_REJECT_COOLDOWN_SECONDS` | 3600 | 拒绝后冷却，避免反复消耗请求 |
| `EGRESS_IP_TIMEOUT` / `EGRESS_PROBE_TIMEOUT` | 5 / 12 | 巡检出口/探活 timeout |

### 配置优先级注意

- trader 的 `load_dotenv()` 通常不覆盖已有环境变量，启动环境可能遮蔽文件值。
- Telegram main 特意读取本项目 `.env`，把 token/user_id 固定后再启动。
- 巡检配置存在自己的解析/缓存规则，不能把所有文件都视为统一的 dotenv 行内注释语义。
- 最稳妥的配置行是单纯 `KEY=VALUE`，不要把注释、重复键、复杂引号放到凭证行里。
- 改文件不等于运行中进程已更新配置。重启是否允许由运维授权决定，不由教程决定。

## 10. 真实运行路线（危险：不在本轮执行）

在授权的新实例中，先确认账户/订单/持仓和状态版本，再选择**唯一入口**：

```text
实验/业务用户启动 watchdog.py
  → watchdog 拉起 bot_runner.py
  → main 固定 TG 配置、取单实例锁
  → post_init 初始化 trader
  → 恢复明确成功并报告 READY
  → 用户首先仅查询 /system、/status、/summary
  → 获得逐笔风险授权后才提交信号
```

`python bot_runner.py` 也能直接启动，但没有 watchdog 的日志/重启监督。
不要同时运行两种入口，不要同时运行 quick_trade。

`启动Bot.bat`、`自启-启动Bot.bat`、`安装开机自启.ps1` 硬编码 `G:\my-crypto-bot`；
在实验目录双击也会进入那条生产路径。**新机器不可直接执行这些原脚本。**
计划任务移植是独立经过批准的运维变更，需要替换目录、解释器、用户、网络前提及权限。

## 11. 回测复现是另一路线

回测依赖单独安装到另一 venv：

```powershell
py -3.11 -m venv .venv-backtest
.\.venv-backtest\Scripts\python.exe -m pip install -r strategies_backtest/requirements.txt
```

还需要精确行情来源、时间范围、缓存、资金费和策略配置。
`.parquet` 等多数大缓存不随仓库分发；缺缓存可能触发真实公开行情拉取。
零网络任务先取得经允许的离线数据；不要直接 import 某个 `*_test.py` 研究文件探测是否可用。
回测收益不能等同于实盘收益，手续费、滑点、盘口、条件单执行和网络延迟需独立建模。

## 12. 环境验收记录模板

```text
checkout/branch/commit：
Python、Windows build、依赖 freeze：
有无本地 .env（只记存在性，不记内容）：
真实网络是否可达/被阻断，作用于哪个实验进程：
相关测试结果：passed / skipped / warnings：
完整门禁结果：pytest + 脚本 + rc：
未验证内容：
是否进行真实 API / 通知 / 交易：
是否改变生产或备份：
```
