# T1C-v2A Integration Addendum v1.2 — 终签稿

## 0. 文档状态

```text
Status: DESIGN APPROVED / IMPLEMENTATION PENDING
Design version: v1.2
Approved baseline: 3d8b63b
Supersedes: v1.0 + v1.1 amendments
Frozen implementations: 5deb349、f7572cf（不得重放）
Design owner: ChatGPT
Implementation owner: WorkBuddy
Approval date: 2026-09-04
```

建议权威存档路径：

```text
docs/architecture/T1C-v2A-Integration-Addendum-v1.2.md
```

本文是完整、自洽的终签版本，不依赖聊天记录或旧版增量修订。

---

## 1. 目标与必要性判断

### 1.1 必须解决的问题

T1C-v2A 只解决以下已被源码或测试证明的问题：

1. 入场手续费可能被重复扣除；
2. 部分路径使用含费成本计算 gross PnL，导致会计口径混乱；
3. SL/TP 可能使用毛量而非 durable 净量；
4. TP 是 MARKET 条件单，但部分估算和展示仍使用 Maker 费率；
5. 结算证据与 stats 写入失败之间缺少可恢复锚点；
6. settlement 与 settlement_dispute 使用不同 dedup 身份会导致重试重复记账；
7. stats 写入失败后，部分路径仍可能清除批次，永久丢失对账证据。

### 1.2 本期不解决的问题

以下内容明确不属于 v2A：

* 不同步查询真实手续费；
* 不新增手续费 API 热路径；
* 不实现 v2B 异步手续费对账器；
* 不实现 v2C 日报和 D-006 消费政策；
* 不建立第二套交易生命周期状态机；
* 不新增线程、状态文件或 `close_phase` 数值；
* 不修复 `/partial` 不记录中间 PnL 的既有限制；
* 不自动处置核心证据 DISPUTED；
* 不修改 P6 conservation 设计与实现。

### 1.3 方案比较

| 方案                       | 结论 | 原因                                     |
| ------------------------ | -- | -------------------------------------- |
| 不修改                      | 拒绝 | 双重扣费、数量口径和失败后丢记录是真实缺陷                  |
| 在四个调用点分别打补丁              | 拒绝 | 重复证据构造、失败语义和恢复逻辑，容易再次漂移                |
| 在结算热路径同步查询手续费            | 拒绝 | 网络重试可能延迟保护单和残单处理                       |
| 新建独立结算状态机                | 拒绝 | 与 P5/P3 生命周期职责重叠，增加第二套恢复系统             |
| 在 P5/P3 结算边界增加批次内 outbox | 采用 | 最小化新增状态，并复用现有恢复、dedup、proof 和 clear 机制 |

---

## 2. 所有权边界

### 2.1 P5/P3 生命周期层负责

* 确认退出订单成交；
* 检查订单代际；
* 构造唯一结算证据对象；
* 原子写入 `pending_settlement`；
* 处理残余订单和保护状态；
* 调用共享 finalizer；
* 取得 fresh convergence proof；
* 决定是否允许 clear。

### 2.2 P0 stats 层负责

* MISSING / VALID / CORRUPT 三态读取；
* CORRUPT 拒写并保持原字节；
* 使用既有基础 dedup 身份；
* 原子追加、file fsync、replace、directory fsync；
* activation 与首条 v2 财务记录同一次原子替换；
* 返回 durable success 或 failure。

P0 不负责：

* 判定交易生命周期；
* 判断核心证据是否 PROVEN；
* 查询交易所；
* 清除批次；
* 修改保护单。

### 2.3 v2B 与 v2C 后续职责

* v2B：异步查询真实手续费，以追加 adjustment/reconciliation 记录修正，不回写原始 settlement。
* v2C：消费 `ESTIMATED / RECONCILED / DISPUTED / LEGACY`，执行日报和 D-006 风险政策。
* 手续费不确定本身不得成为开仓禁令；核心 PnL 证据 DISPUTED、stats CORRUPT 或风险累计真实触线仍可 Fail-Closed。
* v2C 风险预留基准为 `0.0005`，但不属于 v2A 实施范围。

---

## 3. 保留与移除

### 3.1 保留

* P0 终签基线 `3d8b63b`；
* 现有 `_record_realized_pnl` 方法名及 P5 测试兼容性；
* 现有基础 dedup 格式；
* `_batch_net_position` durable 净量来源；
* `_converge_batch_orders_before_clear`；
* `_verify_clear_proof`；
* 现有 tombstone 和 clear proof 契约；
* `close_phase=2`；
* 现有限价 finalizer 的旧批次兼容恢复入口；
* P6 R34–R45 全部行为。

### 3.2 不得重放

* `5deb349` 的可选松散参数 writer；
* `settlement_dispute:<symbol>:<order_id>` 新 dedup 前缀；
* 同步手续费 resolver；
* `f7572cf` 的第二套冲突结算状态机；
* 持久化并跨重启复用 convergence proof；
* 在 stats 失败后仍 clear 批次；
* 用 `actualOrderId` 替换条件单原始 `algoId` 作为基础记账身份；
* BUY/SELL 默认值或硬编码推断。

---

## 4. 唯一结算证据对象

四条结算路径必须构造同一种证据对象，不允许把 8–10 个松散可选参数直接交给 writer。

建议逻辑结构：

```python
SettlementEvidence = {
    "schema": 2,
    "batch_id": str,
    "symbol": str,
    "side": "BUY" | "SELL",
    "mode": "LIMIT" | "SL" | "TP" | "MARKET",

    "base_dedup_key": str,
    "settlement_id": str,

    "exit_order_ref": {
        "kind": "regular" | "algo",
        "order_id": str
    },
    "entry_order_refs": [
        {
            "kind": "regular" | "algo",
            "order_id": str,
            "expected_qty": float
        }
    ],

    "expected_qty": float,
    "observed_qty": float,
    "net_cost": float,
    "exit_price": float,

    "entry_fee_estimate": float | None,
    "exit_fee_estimate": float | None,
    "fee_risk_basis": {
        "entry_notional": float,
        "exit_notional": float,
        "allocation_policy": "CONSERVATIVE_FULL"
    },

    "generation": str | int,
    "created_at": str
}
```

### 4.1 基础 dedup 身份

必须继续沿用现有格式：

```text
<symbol>:<exit_order_identity>
```

规则：

* 普通限价/市价平仓：使用原始普通订单 ID；
* SL/TP 条件单：使用批次原始 `algoId`；
* `actualOrderId` 只能作为后续对账证据，不能替换基础身份；
* settlement 与 settlement_dispute 必须使用完全相同的 `base_dedup_key`；
* 禁止创建 `settlement_dispute:` 前缀；
* `settlement_id` 是供 adjustment 引用的独立字段，不参与旧 dedup 迁移。

### 4.2 核心证据资格

只有同时满足以下条件，核心结算证据才是 PROVEN：

* `side` 明确为 BUY 或 SELL；
* `exit_order_ref.order_id` 非空；
* `expected_qty` 有限且大于零；
* `observed_qty` 有限且大于零；
* expected 与 observed 在批准容差内相等；
* `net_cost` 有限且与 expected quantity 相容；
* `exit_price` 有限且大于零；
* 入场订单引用完整且类型明确；
* 退出订单代际与当前批次一致；
* 不使用毛量代替 `_batch_net_position`。

其中：

```text
expected_qty = durable _batch_net_position
observed_qty = 当前流程已经持有的退出订单成交快照
```

禁止为了证明数量新增手续费 API 或重复订单查询。

### 4.3 记录分类

核心证据完整：

```text
record_type = settlement
core_status = PROVEN
quantity_status = PROVEN
cost_basis_status = PROVEN
pnl_status = ESTIMATED
fee_status = ESTIMATED
```

核心证据缺失、非法或不一致：

```text
record_type = settlement_dispute
core_status = DISPUTED
pnl_status = DISPUTED
```

DISPUTED 记录：

* 不写权威 `net_pnl`；
* 可以写明确标识的 `net_pnl_estimate`；
* 使用同一个基础 dedup；
* 只允许追加一次；
* 不自动 clear；
* 不通过费用预留绕过核心证据缺失。

---

## 5. v2A 估算公式

v2A 不查询真实手续费。

### 5.1 PnL 口径

对于核心证据 PROVEN 的完整结算：

```text
avg_entry = net_cost / expected_qty
gross_pnl(BUY)  = (exit_price - avg_entry) × observed_qty
gross_pnl(SELL) = (avg_entry - exit_price) × observed_qty

net_pnl_estimate =
    gross_pnl
    - entry_fee_estimate
    - exit_fee_estimate
```

入场手续费只能在 `net_pnl_estimate` 中扣一次，不得同时进入 `avg_entry`。

### 5.2 入场手续费估算

若账本存在可用的全量估算：

```text
remaining_entry_fee_estimate =
    total_entry_fee × net_cost / gross_cost
```

规则：

* 该值始终是 estimated；
* 发生过 `/partial` 时不得标 actual 或 reconciled；
* partial 后无法证明逐层剩余手续费归属，必须标记 allocation unknown；
* 风控使用的 `fee_risk_basis` 必须保留更保守的全额可能未分配名义，不得冒充会计实际费用。

### 5.3 出场手续费估算

```text
exit_fee_estimate = exit_notional × fallback_rate
```

* LIMIT 普通平仓按其真实订单类型选择既有 fallback；
* SL、TP 均为 MARKET 条件单，必须使用 Taker 费率；
* TP 展示文字和计算值必须同时引用 Taker；
* 禁止出现“Taker 文案 + Maker 常量”。

---

## 6. 批次内事务 outbox

### 6.1 唯一新增状态

只允许新增一个批次字段：

```python
pending_settlement = {
    "schema": 2,
    "base_dedup_key": str,
    "settlement_id": str,
    "record": dict,
    "evidence": dict,
    "stats_committed": False
}
```

禁止：

* 新增状态文件；
* 新增线程；
* 新增 `close_phase` 数值；
* 持久化 `convergence_proof`；
* 增加第二套 manual-review 状态机。

### 6.2 `_merge_batch_state` 保护规则

`pending_settlement` 是独立 Transactional Protected Field：

1. disk 无、snapshot 有：接受 snapshot；
2. disk 有、snapshot 无：保留 disk；
3. 两边基础 dedup 相同：

   * record、evidence、settlement_id 以 disk 为准，不可改写；
   * `stats_committed` 只允许 `False → True`；
4. 两边基础 dedup 不同：

   * disk 获胜；
   * 拒绝新事务；
   * 发限频 critical；
5. 只有最终合法 clear 才能删除 `pending_settlement`；
6. 陈旧线程不得以 `None`、缺字段或旧 snapshot 冲掉 outbox。

---

## 7. 原子 BEGIN

BEGIN 必须在一个 `_state_lock` 临界区内完成：

1. 重读磁盘最新批次；
2. 确认批次仍 active；
3. 校验退出订单代际；
4. 确认不存在不同 dedup 的活动 outbox；
5. 构造完整、不可变的 evidence 与 record；
6. 在同一次 `_persist_states()` 中写入：

```text
close_phase = 2
pending_close = True
close_reason = settlement_pending
pending_settlement = 完整 outbox
```

7. 检查 `_persist_states()` 返回值。

禁止：

* 先写 `close_phase=2`，稍后再写 outbox；
* 持锁调用内部再次加锁的 `save_batch_state()`；
* 在 `_state_lock` 内执行任何交易所 API；
* 给 SL/TP/MARKET 伪造 `settled_by_limit_close`；
* BEGIN 持久化失败后继续宣称结算成功或清除批次。

BEGIN 失败时：

* 不写 stats；
* 不 clear；
* critical 告警；
* 仍允许执行必要的外部安全观察；
* 不凭未知状态盲目重试非幂等操作。

---

## 8. 共享 finalizer 与唯一恢复入口

### 8.1 调度优先级

启动恢复与运行期恢复统一由监控循环接管：

```text
if pending_settlement exists:
    resume_pending_settlement(...)
elif legacy limit-close recovery fields exist:
    maybe_runtime_finalize_limit(...)
else:
    normal monitoring
```

规则：

* outbox 恢复优先于旧限价恢复；
* LIMIT / SL / TP / MARKET 共用一个 finalizer；
* finalizer 只消费持久化 outbox，不依赖触发线程的临时变量；
* 启动恢复不另建线程；
* 不允许四条路径各写一套恢复逻辑。

### 8.2 PROVEN 结算时序

```text
ExitConfirmed
→ AtomicOutboxBegin
→ ReconcileRemainingOrders
→ StatsCommitted
→ FreshConvergenceProof
→ AuthorizedClear
```

#### ReconcileRemainingOrders

* 先读取当前交易所状态，再决定是否撤单；
* `cancel_order` 不是可盲目重试的幂等操作；
* timeout 或响应丢失后必须先重新查询状态；
* 不重复发送退出订单；
* 网络失败或状态 UNKNOWN 时保留 outbox，不 clear；
* 不把“没有异常”解释成撤单成功。

#### StatsCommitted

* 使用 `base_dedup_key` 幂等追加；
* writer 返回 durable success 或 dedup 已存在，才允许把 `stats_committed` 推进为 True；
* `stats_committed=False → True` 必须持久化；
* stats CORRUPT 或写盘失败时保留 outbox 和批次。

崩溃窗口：

```text
stats 已追加，但 stats_committed 尚未持久化
```

恢复后必须：

1. 用相同 base dedup 查询；
2. 命中既有记录；
3. 不重复追加；
4. 再持久化 `stats_committed=True`。

#### FreshConvergenceProof

* `convergence_proof` 不写入 outbox；
* 不从 trade_state、stats、墓碑或旧内存缓存恢复 proof；
* 每次准备 clear 时必须现场调用 `_converge_batch_orders_before_clear()`；
* proof 必须在同一次 finalizer attempt 中生成并立即传给 clear；
* proof 生成后到 clear 之间不得再执行订单 mutation 或无关网络等待；
* fresh proof 失败或外部状态 UNKNOWN 时保持冻结，不 clear。

### 8.3 DISPUTED 路径

核心证据 DISPUTED 时：

1. 原子写入 dispute outbox；
2. 使用基础 dedup 追加一次 `settlement_dispute`；
3. 持久化 `stats_committed=True`；
4. 设置：

```text
close_phase = 2
pending_close = True
close_reason = settlement_disputed
```

5. 不进入普通 clear；
6. 不执行会移除潜在剩余仓位保护的自动清理；
7. 不恢复普通保护维护；
8. 限频 critical，交 P3/manual review；
9. 重启后不得重复记录。

手续费 ESTIMATED 不属于核心 DISPUTED，不得因此冻结批次。

---

## 9. clear 授权门

当批次不存在 `pending_settlement` 时，旧清理路径继续使用既有 proof 契约，不受新增财务授权门影响。

当批次存在 `pending_settlement` 时，只有共享 finalizer 可以调用 clear，并必须传入：

```text
settlement_commit_authorization = base_dedup_key
fresh_convergence_proof = 当前 finalizer attempt 现场生成
```

clear 必须在锁内重读最新批次并同时验证：

1. authorization 与 outbox 基础 dedup 一致；
2. `stats_committed is True`；
3. 当前记录核心状态不是 DISPUTED；
4. fresh proof 通过既有 `_verify_clear_proof`；
5. proof 属于当前 symbol/batch/scope；
6. outbox 在验证期间没有被替换；
7. 当前调用不是普通旧路径伪造的授权。

任一条件失败：

* 不写墓碑；
* 不删除批次；
* 不删除 outbox；
* 返回失败并保留恢复能力。

---

## 10. schema activation

activation 记录：

```python
{
    "record_type": "schema_activation",
    "schema": 2,
    "dedup_key": "schema_activation:v2",
    "activated_at": "...",
    "baseline": "3d8b63b"
}
```

规则：

1. activation 不是 trade；
2. 日报和 D-006 必须按 `record_type` 跳过；
3. activation 与第一条新 v2 settlement/dispute 在同一次 stats 原子替换中写入；
4. 已存在 activation 时跳过，不重复；
5. stats CORRUPT 时禁止写 activation；
6. 如果基础 dedup 已存在但 activation 缺失，本次重试成功返回但零写入，不得在旧记录之后补画边界；
7. P0 单独部署不得自行激活 v2；
8. v2A 可以单独提交和审查，但不得在 v2C 消费层就绪前上线激活。

---

## 11. 四条生产路径

必须覆盖且只能覆盖现有四个 `_record_realized_pnl` 结算入口：

| 路径           | 出场身份      | observed quantity  | 特别要求                 |
| ------------ | --------- | ------------------ | -------------------- |
| 限价 finalizer | 普通订单 ID   | 已持有的订单成交快照         | 保留旧 phase-2 恢复兼容     |
| SL           | 原始 algoId | `sl_detail` 权威成交字段 | 不得使用毛量               |
| TP           | 原始 algoId | `tp_detail` 权威成交字段 | MARKET/Taker 费率与文案一致 |
| 市价平仓         | 普通订单 ID   | 已确认的市场单成交量         | 不得 clear 后才建立 outbox |

所有路径：

* 使用同一个 evidence builder；
* 使用同一个 atomic begin；
* 使用同一个 finalizer；
* 不同步查询手续费；
* 不默认 side；
* 不按订单 ID 长度猜类型；
* 不在 outbox durable 前 clear；
* 不忽略 writer 返回值。

---

## 12. 立即展示语义

* 核心 PROVEN、费用未对账：

```text
估算净盈亏
手续费待异步对账
```

* 核心 DISPUTED：

```text
结算证据冲突，等待人工核对
```

禁止：

* 把 ESTIMATED 显示为最终权威净盈亏；
* 把 DISPUTED 显示为正常已结算；
* 因手续费不确定直接声称禁止开仓；
* TP 显示 Taker 但使用 Maker 数值。

---

## 13. 验收条件

### 13.1 证据与公式

1. BUY/SELL 镜像公式行为正确；
2. 入场手续费只扣一次；
3. 净成本均价不含手续费；
4. SL/TP 使用 durable 净量；
5. TP 使用 Taker 常量和 Taker 文案；
6. 非有限数、空 side、空退出 ID、空引用、数量不一致均生成 DISPUTED；
7. 毛量不能通过数量证明；
8. v2A 生产热路径零手续费 API。

### 13.2 dedup 与 activation

9. settlement/dispute 使用相同基础 dedup；
10. dispute 重启重试只记录一次；
11. 条件单 actualOrderId 不改变基础身份；
12. activation 与第一条 v2 记录同一次原子替换；
13. activation 最多一条；
14. 旧 dedup 已存在而 activation 缺失时零写入；
15. CORRUPT 时 settlement、dispute、activation 全部拒写且原字节不变。

### 13.3 outbox 与 merge

16. phase 2 与完整 outbox 同一次持久化；
17. 陈旧 snapshot 无法删除 outbox；
18. 相同 dedup 的不可变 evidence 不能被改写；
19. 不同 dedup 不能覆盖活动 outbox；
20. `stats_committed` 只能 False→True；
21. BEGIN 持久化失败时零 stats、零 clear。

### 13.4 恢复与 clear

22. 四条路径均由通用恢复入口接管；
23. 旧 phase-2、无 outbox 的限价路径仍能恢复；
24. stats 成功、flag 持久化前崩溃，恢复后 dedup 命中且仅一条记录；
25. fresh proof 每次 clear 前重新取得；
26. 不接受任何持久化或缓存 proof；
27. proof 失败、UNKNOWN 或过期路径均不得 clear；
28. pending outbox 无匹配授权时 clear 被拒绝；
29. DISPUTED 不自动 clear；
30. 市价路径不存在 clear-before-outbox 窗口；
31. 恢复不得重复发送退出订单；
32. 残单 mutation 前必须先查询当前状态。

### 13.5 回归

必须通过：

* P0 stats durability S1–S17；
* P5 全套；
* P3 lifecycle；
* P6 R34–R45 原样通过；
* account risk、restart semantics 和相关状态持久化测试；
* 全量基线回归；
* 生产文件 hash 哨兵；
* 不得把 sandbox artifact 谎报为代码通过。

### 13.6 消融与简化

至少进行一次有针对性的消融，覆盖下列承重结构之一：

* 删除 outbox merge 保护；
* 绕过通用恢复入口；
* 绕过 clear authorization；
* 允许旧 proof clear；
* 将 dispute dedup 改成另一前缀。

消融必须使对应反例确定性变红。

全部测试通过后只做一次简化检查：

* 删除不必要 helper、分支和重复字段；
* 不削弱持久化、恢复、dedup、proof、保护或测试；
* 复验后停止修改。

不要求所有新测试机械性全 RED；只要求新增行为具有真实、可判别的失败证据。

---

## 14. 部署与权限边界

* 本文终签只批准设计，不自动批准实施；
* 实施必须从 `3d8b63b` 创建干净分支；
* 已创建但未修改的正确分支可以继续使用；
* WorkBuddy 不得重放 `5deb349` 或 `f7572cf`；
* 设计稿存档、代码实施、commit、push、merge、部署、重启、实盘分别受授权边界约束；
* v2A 可以在专用分支 commit/push 供审查；
* v2A 不得单独合并或上线激活；
* v2B、v2C 必须分别设计、复审和授权；
* 上线必须等 v2C 消费层兼容后单独审批；
* 禁止把测试或 mock 结果描述成交易所实盘验证。

---

## 15. 最终裁决

```text
ARCHITECTURE: FULLY ALIGNED
DESIGN: APPROVED
IMPLEMENTATION: PENDING EXPLICIT AUTHORIZATION
APPROVED BASELINE: 3d8b63b
FROZEN IMPLEMENTATIONS: 5deb349, f7572cf
```

v1.2 已完成设计、反驳、修订和限定源码可行性复核。除非实施发现新的事实会改变资金、外部状态或恢复语义，否则不再开启设计循环。
