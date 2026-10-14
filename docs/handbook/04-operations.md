# 运维、测试、诊断与交付

[返回总览](README.md) · [下一篇：AI 接手清单](05-ai-handoff.md)

## 1. 遇到故障先问：哪一层失效

| 信号 | 能说明什么 | 不能说明什么 |
|---|---|---|
| watchdog PID 存在 | 监督进程存在 | bot/trader 正常、订单受保护 |
| `.heartbeat.json` 新鲜 | watchdog 刷新了进程心跳 | 每批次业务处理成功 |
| `control.json` 新鲜且 instance_id 匹配 | 当前控制面有进度 | 所有批次监控都有进度 |
| batch 进度新鲜 | 该批次成功处理进度在更新 | 所有外部订单都已归属 |
| READY | 启动恢复链明确成功 | 今后不会发生鉴权/网络故障 |
| poll_req_total 增长 | 进入 do_request 次数增长 | 请求真的已发出、响应有效 |
| poll_http_response 增长 | HTTP 响应到达 | HTTP 200 + ok=true + result 列表 |
| poll_success 增长 | 有效轮询成功 | 更新已被业务处理、回复已送达 |
| SMTP send 成功返回 | 服务器接受邮件 | 用户收件箱实际可见、没进垃圾箱 |

本程序强调“活着”和“业务进度”分离。看到一个绿色横幅不能跳过订单/持仓事实核验。

## 2. 检查工具的真实副作用

| 工具 | 网络 | 本地写入/业务影响 |
|---|---|---|
| `legacy_preflight_check.py` | 无 API | 只读指定账本/helper；覆盖窄 legacy 条件，不是所有上线门禁 |
| `reconcile_pre_launch.py` | 真实 CCXT/签名查询 | 设计为不下单/撤单，不写状态；仍需 API 授权与配额预算 |
| `健康巡检.py` 常规运行 | 可能 TG/SMTP/代理/IP/鉴权 | 巡检日志、告警/出口缓存状态 |
| `健康巡检.py --dry-run` | 源码的 dry-run 路径跳过网络 | 仍可读取本地配置/状态、记录日志；不是全程序沙箱 |
| `健康巡检.py --selftest` | TG 经代理/直连 + 邮件 + 出口/签名探活 | 必然外发；不是离线测试 |
| `健康巡检.py --wait-proxy` | 本地代理探测 | 影响自启等待；dry-run 才跳过 |
| `quick_trade.py` | 真实 API | 真实下单/状态，不能和 watchdog 并行 |
| `/auth_reset` | fetch_balance 探活及恢复查询 | 鉴权/业务状态恢复，不是删除一个文件 |
| `run_test_gate.py` | 理论上测试应 mock；探测会调用本地进程查询 | 测试可能写临时/checkout 文件；安全依赖隔离及 mock |
| 裸 `pytest` | 不保证 | 会误收集研究/历史脚本，可能公开行情拉取 |

“只读 API”也可能触发限流、鉴权拒绝与网络 timeout；不要把它当零成本。

## 3. 正式测试门禁

### 3.1 为什么要两段

现役测试不都是 pytest 形式。部分脚本将实际用例命名为 `scenario_*` 或 `t1_*`，
pytest 可能收集零项，但直接执行会运行数十个断言。

`run_test_gate.py` 同时：

1. 生成正式 pytest 参数，排除策略回测、`tests_archive` 和动态枚举的附件/恢复包目录。
2. 逐个以独立 Python 子进程运行根目录 `test_*.py`，统计真实退出码。
3. 对登记的基线核对数量及具体失败用例身份，防“失败数相同”掩盖不同回归。
4. 前后比较少量本地哨兵，但这是**事后检测，不是隔离**。

禁止只统计“脚本都启动了”，也不能把 rc=1 的脚本纳入全绿。
脚本 return 0 也可能是 pytest 专用文件没有 main；它们由 pytest 段实际覆盖。

### 3.2 命令

```text
python run_test_gate.py                 两段完整门禁，默认拒绝 Bot 运行/探测未知
python run_test_gate.py --strict        NOT-VERIFIED 也判失败
python run_test_gate.py --scripts       只脚本集合，不是完整门禁
python run_test_gate.py --pytest        只可收集集合，不是完整门禁
```

`--allow-live` 是显式接受测试与生产并存的危险选项，不是“启用隔离”开关。
若无本轮明确授权，检测拒绝就结束，不停机、不绕过、不换名字欺骗进程探测。

### 3.3 退出码和结论

| 退出码 | 结论 | 含义 |
|---|---|---|
| 0 | ALL-GREEN | 该次两段退出码符合全通过、无门禁待验证项 |
| 1 | FAIL | 真失败、基线漂移、strict 未验证或哨兵变化 |
| 2 | COMPLETED-WITH-EXCEPTIONS | 仅登记的基线失败/未验证，不能称全部通过 |
| 3 | REFUSED | Bot 在跑或探测未知，未执行测试 |

报告还需单独写 pytest skipped 和 warnings：门禁 0 不自动证明每个配置检查实际执行过。
例如干净 checkout 的机器 `.env` 比较是 skip，不应用复制生产凭证来消除 skip。

`test_orphan_guard.py` 在 Windows 实例互斥体被持有时会退出 42，
门禁标记 NOT-VERIFIED。若要实际运行，必须另有维护窗口/隔离机器授权，不能自行停止生产。

### 3.4 相关测试地图

| 改动/风险 | 优先看哪些测试 |
|---|---|
| parser/Telegram 配置/信号去重 | `test_bot_runner_config.py`、`test_dedup_entry.py`、`test_tg_reply_fallback.py` |
| getUpdates 诊断、安装接线、异常保真 | `test_tg_diagnostics.py`、`test_terminal_noise.py` |
| 健康进度、心跳失败隔离 | `test_health_progress.py`、`test_heartbeat_isolation.py` |
| SMTP 与邮件策略 | `test_smtp_transport.py`、`test_email_gate.py`、`test_email_channels.py`、`test_notification_email_policy.py` |
| 状态持久化/恢复/墓碑 | `test_d009_state_persistence.py`、`test_recover_semantics.py`、`test_monitor_resume.py` |
| 保护单事务与门 | `test_protection_write_gates.py`、`test_protection_chain_fix.py`、`test_b2_*` |
| 平仓/撤销/部分减仓 | `test_p5_closecancel.py`、`test_v64_partial_close.py`、`test_p6_conservation.py` |
| 成交成本与待补证 | `test_f2f3_fill_cost.py`、`test_q15_recovery_chain.py` |
| 净盈亏权威规则/worker | `test_pnl_accounting.py`、`test_pnl_reconciliation_worker.py`、`test_p1_settlement_price.py` |
| 轮询退化与通知预算 | `test_poll_degradation.py`、`test_poll_alert_budget.py` |
| 外发绊线 | `test_egress_tripwire.py` |

这是导航，不是批准的最小发布测试清单。同一改动可能跨多个组。

### 3.5 按影响决定验证范围

- 纯文档：检查内容、相对链接、示例安全及 diff，不重跑完整门禁。
- fixture 修正：先跑相关脚本/pytest，检查共享模块、后台线程与全局变量污染；
  若存在跨测试或进程级影响，再跑完整门禁。
- 运行逻辑或门禁机制：按既定发布门禁验证，必要时受控故障注入与基线对照。
- 实际联机/订单适配：离线通过只覆盖程序逻辑；真实行为需要单独批准的联机证据。

历史测试结果必须带 commit、环境和范围；不要把过去的 73/73 带到下一份候选上冒充复验。

## 4. 通知与网络运维

### 邮件当前政策

`EMAIL_ALERT_ENABLED=false` 拒绝所有邮件；启用时：

- 致命事件白名单（critical、auth_blocked、crash、startup_breaker、health 等）可发。
- 日报按独立开关决定。
- 人工 selftest 明确可发。
- 普通/未知事件拒绝邮件，即便有持仓；TG 与日志由调用方管理。

`EMAIL_ALERT_ONLY_WITH_POSITION` 是兼容旧配置，不是当前普通邮件放行机制。
老注释、旧 `.env.example` 的说明不应覆盖 `email_gate.should_send_email` 的实际语义。

SMTP 只用 QQ 587 STARTTLS，证书校验通过后再登录和提交。不能无 TLS 回退，
也不能因 send 超时简单无限重发；事件层应维护预算和发送状态。

### Telegram 当前请求设置

- 普通请求：connect/read/write 配置为 30s。
- getUpdates：connect 30s，configured read 120s，write 30s。
- 默认 long-poll timeout 10s，PTB 22.8 `Bot.get_updates` 加到 read budget，得到 130s。
- 成功的空轮询大约等待 10s 是正常行为。

排查按异常类型和证据分类：PoolTimeout 可表示连接池未拿到连接；ConnectTimeout 是建立
连接阶段；ReadTimeout 是等待读取；HTTP 5xx 是拿到响应但失败。
shutdown 的 CancelledError 与正常网络失败也不能无差别累加成同一个故障率。

不要另起第二个 getUpdates 客户端来“检测生产 TG”，会与主客户端竞争。
不要在没有证据的情况下切换生产代理节点、延长 timeout 或加更高频重试。

## 5. WinError 5 的现阶段处置

证据：10/12–10/13 `.bot_health` 替换偶发拒绝；隔离高竞争实验用普通 Python 读句柄
与 `os.replace` 成功复现同类错误。自然生产事件占用方仍未知。

现阶段不更改读取分享方式、不加重试、不改 ACL/杀软排除项。
生产再次自然出现时的只读取证要求：

1. 使用准确目标路径（control 或那一个 batch 文件），不要泛扫整个磁盘。
2. 提前约定采集时长和最大事件量，到时即停；操作开始本身需要运维授权。
3. 关联失败时间的 replace/rename 结果和文件 open/close 生命周期。
4. 查看进程/线程、共享方式、失败返回码，必要时检查过滤驱动相关事件。
5. 只看到某个进程读过文件，不足以证明它造成那次拒绝。
6. 保留脱敏证据，禁止在生产制造占用或反复试写。

30 分钟实验材料见 `docs/audits/health_replace_winerror5_20261013/`。
审计脚本是可执行实验材料，不是常驻任务，也不应纳入例行门禁反复压测。

## 6. 启停、恢复与计划任务

### 启停是业务风险操作

停止 bot/watchdog 不会自动保证账户没有持仓，也不保证交易所已有保护单都适当。
停止期间程序无法补保护、监控新成交或即时处理未知订单。
即便用户允许停机，也需确认是否允许撤单、平仓和自动恢复；这是不同权限。

Windows 的 `停止Bot-应急.bat` 为强制操作，不应作为常规测试的一步。
watchdog 自带崩溃自动拉起和启动失败熔断，当前定时重启默认关闭。

### 恢复前不能做的事

- 清空 trade_state 以取得 READY。
- 删除 auth_blocked、signal_dedup 或通知预算文件解决告警。
- 取消所有未知订单“清理账户”。
- 将旧冻结状态套到当前真实持仓上。
- 让多个 clone 同时使用一个账户/一个 Telegram token。

授权的恢复应按实际状态核对 normal/algo 未结单、汇总持仓与本地批次，
确认未知事实需要人工处置，再启动正式恢复链。

### Windows 计划任务

现有安装脚本注册登录后自启和每 15 分钟巡检，任务为交互式用户和最高权限；
长驻任务 `ExecutionTimeLimit=PT0S` 避免默认时间上限终止。
这些脚本有历史机器专用路径，不能当成跨平台服务管理器。
移植前必须审查工作目录、Python 路径、任务身份、代理启动顺序、日志权限及停止流程。

## 7. 交付与回滚的最低要求

```text
需求/授权 → 干净 worktree → 窄改动 → 相关测试/门禁
  → 审查 → 候选文件清单与 hash → 发布批准
  → 受控部署（另一次任务）→ READY/业务/状态验收 → 回滚保护核对
```

- PR 合并只改变 Git 历史，不能代替复制文件/重启/验收的授权。
- 回滚包不是垃圾文件，不要改、移、清理旧 R3/R2 归档来方便新发布。
- manifest 中的 SHA-256、候选、调用方与新增 helper 的移除条件必须对应同一版。
- 回滚恢复代码也可能与当前状态格式/订单事实不兼容，不能盲目执行。
- 源文件 hash 匹配只证明磁盘字节；加载进程版本还需启动时间、日志及批准的验收证据。

本套文档不重述某个生产路径的完整部署命令。真实变更需由负责人提供当天批准的
文件清单、状态条件、回滚包和执行边界；不能从历史部署报告复制危险命令照跑。

## 8. 故障报告的最小格式

```text
观察时间与时区：
版本/运行实例：
受影响层（TG/API/批次/状态/通知/对账）：
症状与实际业务影响：
脱敏异常类型链/错误码/路径：
已确认事实：
假设（尚未证明）：
已做的隔离/只读检查及期限：
订单/持仓/账本是否改变：
下一步授权需求与停止条件：
```
