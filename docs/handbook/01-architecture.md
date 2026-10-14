# 架构、数据流与安全状态机

[返回总览](README.md) · [下一篇：环境与复现](02-reproduction.md)

## 1. 运行拓扑

```text
人工/Telegram
   │ 命令、JSON 文本、JSON 文件、按钮回调
   ▼
bot_runner.py（asyncio Application）
   ├─ 用户授权 / 信号快照 / 去重 / TRADER_LOCK
   ├─ executor → CryptoTrader 同步交易操作
   ├─ 回复、通知队列消费、control 健康进度
   └─ tg_diagnostics 被动探针
             │
             ▼
trader_260725.py（CryptoTrader）
   ├─ CCXT binanceusdm → Binance 普通单与 algo/条件单通道
   ├─ 批次账本 / 保护单注册表 / 入场意图
   ├─ 每批次监控线程 / 恢复与故障降级
   ├─ 平仓事务 / 幂等结算 / 墓碑
   └─ 低优先级后台盈亏对账 → pnl_accounting.py

watchdog.py
   ├─ 拉起 bot_runner 子进程，观察退出码与运行时长
   ├─ 主程序输出按天落盘
   └─ 每 60s 写进程心跳

独立调度 → 健康巡检.py
   ├─ 读 watchdog 心跳、control/batch 进度与本地状态
   ├─ 联机模式可探测代理、出口 IP 与鉴权
   └─ 告警 → Telegram / email_gate → smtp_transport
```

**watchdog 活着不等于 trader READY；trader 线程存在不等于业务轮询成功；
HTTP 200 不等于 getUpdates 返回有效更新结构。** 分层健康指标就是为避免这类误判。

## 2. 模块责任表

| 文件 | 主要责任 | 阅读锚点 |
|---|---|---|
| `parser.py` | JSON → `TradeSignal`，基础字段校验和 batch_id 生成 | `parse_signal_from_dict` |
| `bot_runner.py` | Telegram 主入口、鉴权、命令、信号去重、恢复调度、通知消费 | `main`、`run_trader_execution`、`run_trader_recovery_on_startup` |
| `trader_260725.py` | 账户/API/订单、批次状态、监控、保护单、平仓、收益记录 | `CryptoTrader`、`execute_signal`、`_start_monitoring` |
| `pnl_accounting.py` | 无网络、无持久化的纯对账规则 | `reconcile_income_facts`、`apply_authoritative_revision` |
| `poll_alert_state.py` | 故障周期的持久化通知预算，不改变交易所状态 | `PollAlertBudget` |
| `email_gate.py` | 事件政策：哪些情况允许邮件 | `should_send_email` |
| `smtp_transport.py` | QQ 587 STARTTLS，校验证书，TLS 前不登录 | `send_qq_mail` |
| `health_progress.py` | 当前实例标识、control 与 batch 进度 | `write_progress`、`read_progress` |
| `tg_diagnostics.py` | 包装真实请求/更新/回复边界，诊断自身不改变业务返回/异常 | `install_poll_request_probe` 等 |
| `watchdog.py` | 子进程监督、启动熔断、日志、心跳 | `record_process_exit`、进程监控主循环 |
| `健康巡检.py` | 独立健康与出口核验、故障告警 | `run_check`、`main` |
| `egress_tripwire.py` | 测试期网络绊线，拦截并计数 | `install`、`assert_zero` |
| `run_test_gate.py` | 正式 pytest 集合与脚本集合、退出码、事后哨兵 | `build_pytest_args`、`main` |
| `reconcile_pre_launch.py` | 授权后的真实账户只读状态对照 | `fetch_all_open_orders` |
| `legacy_preflight_check.py` | 旧账本 ENTRY 链可恢复性检查，零 API | `main` |

## 3. 启动不是简单的“进入 while True”

典型流程：

1. Telegram 项目配置从本项目 `.env` 读取并校验，在启动边界固定有效 token/user_id。
2. Windows 获取全局具名互斥体；已存在实例则拒绝，避免重复执行与 API 配额放大。
3. 初始化 Telegram 普通 API 请求客户端和独立 getUpdates 请求客户端。
4. Application `post_init` 构造 trader，准备恢复链。
5. 若持久化鉴权锁存在，走探活 → RECOVERING → 对账 → 解锁；不是直接删锁。
6. 校验注册表锁并恢复历史批次；恢复明确成功后才置 `_ready=True`。
7. 启动后台盈亏对账，继续通知/批次监控。

等待 READY 的原因：程序必须先知道历史订单和持仓的归属，才能判断新信号是否制造新风险。
仅看到“Telegram 监听服务已启动”或“API 已连接”横幅不代表完整恢复已经成功。

## 4. 什么叫一个批次

`batch_id` 是程序内部的策略执行单元。一个批次包含：

- 一个 symbol 和方向；
- 多层计划入场价格与数量；
- 对应的入场订单身份、成交数量/成本证据；
- 止损/止盈意图与注册表；
- 平仓事务状态、减仓事件与结算标记；
- 监控代次和最后成功处理进度。

交易所实际持仓通常按 **symbol/positionSide 汇总**，不按本程序 batch_id 分隔。
因此“批次独立”是本地的记账与管理模型，不是交易所自动帮你把同方向持仓分成独立仓位。
这也是手工模糊减仓、重复运行第二个 bot、他方订单会引起归属与守恒问题的根本原因。

## 5. 生命周期的概念模型

```text
接收信号 → 解析/去重 → READY/风险/账户模式检查
  → 持久化批次骨架和创建意图 → 创建入场单
  → 核实订单 ID、身份、方向、数量和成交
  → 注册监控 → 维护成交账本 → 创建/确认保护单
  → 平仓事务（市价/限价/部分平仓/外部事件）
  → 确认实际成交与撤后收敛
  → 本地暂算结算与持久化对账任务
  → 清账 + 墓碑（只有安全条件具备才允许）
  → 后台权威盈亏对账
```

失败分支不是统统返回“无持仓”：

- 创建请求超时：可能已经在交易所创建，进入未决意图核验，而不是盲目再挂。
- 查单失败：UNKNOWN，不是 ABSENT；缺证据不允许随意撤/清账。
- 已成交但成本缺失：保留待补证；计划量/成本不能冒充真实成交。
- 保护单不匹配或创建未决：注册表持久化并阻断对应不安全动作。
- 平仓在途：冻结普通业务变更，避免平仓与保护单修改并发制造重复风险。
- 历史账本不可读：fail-closed，保留现场，不覆盖为空账本。

## 6. ENTRY、SL、TP 与 normal/algo 通道

- ENTRY：入场单；每层可能有条件单 ID 与触发后的真实成交子订单 ID。
- SL：保护止损，当前实现包含逐层保护与注册表管理。
- TP：批次止盈保护；具体数量与替换依据净持仓和状态机。
- ordinary order：普通订单通道，例如限价平仓。
- algo/stop order：条件单通道，查询不能只看普通 open orders。

在 CCXT 适配中会出现 `params={'stop': True}` 与 normal 双通道查询。
“普通未结订单为空”不足以证明条件单不存在；“algo ID 和真实 trade order ID 不同”
也不代表可以直接丢弃归属。应沿源码的身份链核验。

不要在新 AI 的改动中把两类查询合并为只调用一次普通 `fetch_open_orders`。

## 7. 保护单注册表与事务护栏

注册表不是单纯缓存订单 ID，而是记录“意图/实际/核验/锁定”的持久化事实。
典型状态包括：

| 状态 | 概念含义 |
|---|---|
| `PENDING_CREATE` | 创建意图已记录，还不能称交易所已存在 |
| `PENDING_VERIFY` | 已尝试创建或已知部分身份，需要进一步核验 |
| `NOT_CONFIRMED` | 尚无法证实，不能直接当作不存在 |
| `CONFIRMED` | 已核实符合该保护意图 |
| `MISMATCH` | 查询事实与期望不一致 |
| `HARD_LOCK` | 硬锁，不允许一般逻辑擅自继续 |
| `PROGRAMMATIC_CANCELED` | 程序已处理的取消终态 |
| `ABSENT` / `FAILED` | 特定核验路径确定的终态；不能仅凭一次网络错误写入 |

保护单替换倾向先创建并确认新保护，再撤旧保护，以缩小空窗。
这不等于所有 API 超时/交易所拒绝场景都保证零空窗；检查必须针对具体路径。

平仓主要字段是 `close_phase`、`close_op_id`、`pending_close`、`close_reason`。
其中 `close_phase` 单调推进、owner 身份限制和字段 merge，防止旧线程用旧快照回退新事务。
部分减仓或撤销限价平仓的保护重建只允许匹配 owner 的窄例外，不能泛化为随时绕过冻结门。

## 8. 持久化文件地图

| 文件/目录 | 类型与用途 | 禁止的快捷修复 |
|---|---|---|
| `.env` | 私密配置；交易/Telegram/邮件凭证等 | 复制到审计附件、打印全量 env |
| `trade_state.json` | 活跃批次主账本 | 损坏后改成 `{}` 再启动 |
| `trade_state.json.bak` | 历史/备份保护材料，具体恢复规则看代码 | 擅自覆盖或“清理” |
| `trade_tombstones.json` | 已清批次墓碑，抵抗旧快照复活 | 为放行而删除墓碑 |
| `trade_stats.json` | 平仓暂算记录、费用来源、accounting_v2 任务与证据 | 用截图的两位小数覆盖精确记录 |
| `signal.json` | 最近信号文件，不是全部交易历史 | 把它当独立执行队列反复重放 |
| `signal_dedup.json` | 信号去重/一次性批准状态 | 删除后再次发送同信号 |
| `auth_blocked.json` | 鉴权封锁持久状态 | 手工删锁以恢复下单 |
| `.notify_queue/` | 一事件一文件的通知队列 | 清空以消除错误日志 |
| `.notify.state.json`、`.notify_audit.log` | 通知尝试、静默和审计 | 当临时缓存无脑删除 |
| `.poll_alert.state.json` 及 marker | 轮询故障周期的持久通知预算 | 反复重建导致预算重置 |
| `.daily_report.state.json` | 日报发送/去重状态 | 重置后重复发送 |
| `.heartbeat.json` | watchdog 进程层健康 | 伪造 ts 表示恢复 |
| `.bot_health/control.json` | 当前实例控制面业务进度 | 写新时间冒充处理成功 |
| `.bot_health/batch_*.json` | 每批次成功业务进度 | 失败时仍更新进度掩盖停滞 |
| `.tg_diag.json` | 被动 Telegram 边界诊断 | 依据 total 单独判断请求已发出 |
| `.egress_ip.state.json` | 出口核验缓存/拒绝冷却 | 删掉缓存强迫每轮签名探活 |
| `.last_ip.txt` | trader 的出口 IP 记录 | 当凭证或权威账户模式证据 |
| `.bot_instance.lock` | PID 诊断，Windows 主要排他机制是具名互斥体 | 删文件以强启第二实例 |
| `logs/`、`watchdog.log` | 业务和运行日志 | 上传未脱敏原始文件 |

### 路径陷阱

有些文件基于 `__file__`，而 `STATE_FILE`/墓碑等仍可基于当前工作目录。
所以常规运行应使用 checkout 根目录作为 CWD；测试必须在构造之前重定向相关状态。
worktree 并不保证所有文件/API 都已隔离：硬编码生产路径、继承的环境变量、默认 CWD、
测试导入副作用都可能穿透这个边界。

## 9. 并发与幂等

- Telegram 控制面：asyncio；统一执行入口和 `TRADER_LOCK` 管理相关串行段。
- 交易 API：共享信号量与锁，控制调用频率、冷却和鉴权模式。
- 批次监控：线程；所有状态更新必须考虑快照过期与事务 owner。
- 通知/诊断：后台线程或事件队列，不应把观测故障变成交易故障。
- 持久化：进程锁保护 read-modify-write，但不等于跨进程数据库事务。
- 盈亏：`dedup_key` 防同一平仓重复记账；对账任务租约与游标允许重启后续跑。

“原子 replace”只描述替换操作的性质，不保证 Windows 句柄共享允许替换、
不保证所有目录元数据落盘，也不保证该程序不存在跨线程业务竞态。

### 结构示意：不是可写回的状态模板

以下只展示主要嵌套关系，省略了很多注册表、意图、时间和费用字段。
不能拿它初始化真实账户，也不能删掉未知字段后写回账本。

```text
trade_state.json
  symbol
    batch_id
      side / is_active / target_amounts / entry_orders
      filled_details / last_filled_count / fill_evidence
      protection_registry / current_sl_id / tp_order_id
      close_phase / close_op_id / close_reason
      settlement_reported / realized_reduce_amount / ...

trade_stats.json
  trades[]
    batch_id / symbol / side / amount / avg_price / exit_price
    net_pnl / local_provisional_net_pnl
    entry_fee_source / exit_fee_source / pnl_not_authoritative
    accounting_status / trade_evidence / ...
  accounting_v2
    tasks
      dedup_key
        status / trade_evidence / income_facts / fx_facts
        due_times_ms / due_index / blocking_reasons / ...
```

字段的真实校验/合并/恢复规则以 `trader_260725.py` 和 `pnl_accounting.py` 为准。
不能仅因某字段在旧记录缺失就把旧文件判断为整个损坏，也不能默默把缺失证据补成零。

## 10. 不变式：修改之前必须记住

1. UNKNOWN ≠ EMPTY；缺证据不能推断无订单/无持仓。
2. 成功请求 ≠ 实际成交；计划数量 ≠ 实际成交数量。
3. 程序批次 ≠ 交易所独立仓位。
4. 创建结果未决不能简单重试为第二张单。
5. 清账不是 UI 清空，要满足收敛、结算和墓碑等条件。
6. 通知入队 ≠ Telegram 成功；SMTP accepted ≠ 收件箱实际送达。
7. API 暂时恢复不能解除创建未决或未完成的鉴权恢复事务。
8. 估算费用 ≠ 权威净盈亏；权威结果必须满足完整性和归属。
9. 诊断 failure 不应修改原调用返回、异常或 CancelledError。
10. 账户余额变化不能自动全部归属到某一个批次。
