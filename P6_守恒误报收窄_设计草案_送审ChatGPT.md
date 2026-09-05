# P6 守恒误报收窄 —— 设计草案送审 ChatGPT（**v1.3**，2026-09-03）

> 状态：**设计稿，未动任何生产代码**。v1.3 = 补齐 ChatGPT 指出的调用契约矛盾
> + 事件内单调棘轮（CONDITIONALLY ALIGNED → 补齐即批准实施）。
> 修订链：v1.0 → v1.1 → v1.2 → v1.3。事实基线：HEAD = 4d67bce。

---

## 0. 修订记录

### v1.2 → v1.3（本稿）

| 项 | v1.2 | v1.3（本稿） | 裁定来源 |
|---|---|---|---|
| 调用契约 | 有同方向 sibling 才调观察器 × sibling<2 删记录（**矛盾**：sibling 消失后观察器不再被调，删除路径不可达，R41 不可实现） | **每轮无条件调用**；观察器内部 `同方向批次<2 → 删整份记录 + return`（单批次调用只负责清理历史事件，不产生守恒告警） | ChatGPT 接线矛盾 |
| 事件内棘轮 | 未定义 | **单调规则：`critical_count > 0` 后，即使后来出现有效在途事务，也不得降级回 warning / 重新获得 300s 宽限** | ChatGPT 建议 |

### v1.1 → v1.2（历史）

宽限资格改五条件合取（回滚保留 close_reason/close_op_id 作审计，L9117-9119
实证）；事件状态改「一次冲突一份记录、事件结束整份删除」（旧 alert_count
只增不清=生命周期上限缺陷一并消亡）；R38 重写 + R40/R41 新增。

### v1.0 → v1.1（历史）

方案 A 否决；全局 600s 否决改「有事务证据 300s」；收敛显式观察判定；计量/计时/
去重按 (symbol, side)；告警上下文纳入。

---

## 1. 问题定义（源码实证，v1.0 部分维持）

误报触发链：限价平仓成交 → 归档滞后窗口内，主监控多批次跳过分支（L6775-6782）
接线守恒检测 → `actual < Σnet − tol` 必然触发 critical。已知归属的在途平仓被当成
「外部减仓归属不可知」。

**v1.1 新增实证**：
- `same_side_close_inflight`（L8882）只禁**同方向**并行 close → LONG/SHORT 批次
  可并存；而现行守恒是「单方向 actual vs 双方向 Σnet」的不对称比较
  （Σnet 无 side 过滤 L3183-3187；side 取自任一首批次 L3197-3202）——本身就是
  一个待修的判定缺陷，不只是告警质量问题。

## 2. v1.1 设计（吸收三项裁定后收口）

### 2.1 计量边界：(symbol, side)

- Σnet 只累加**同方向**（`b['side']` 一致且 `is_active`）批次的净量；
- 计时、首见标记、告警去重计数全部以 `(symbol, side)` 为键；
- 观察器复用调用点已取得的方向仓位：主监控每轮已有方向感知
  `current_actual_position`（L6611，`_get_current_position_amt(symbol, is_hedge, side)`），
  **不再在检测器内任取首批次重查**。

### 2.2 调用契约：每轮无条件调用（v1.3 收口）

v1.2 的「有 sibling 才调观察器」与「sibling<2 删记录」互相矛盾——sibling 消失后
观察器不再被调，删除路径不可达，R41 的重置场景实现不到。v1.3 契约：

```
# 主监控每轮（取得方向仓位后），无条件调用：
_maybe_report_conservation_conflict(symbol, side, current_actual_position)

# 观察器内部第一步：
same_side_batches = 同方向 active 批次列表
if len(same_side_batches) < 2:
    删除 (symbol, side) 整份事件记录      # 事件上下文已消失
    return                                 # 单批次无归属歧义，不做守恒判定
# ...后续守恒判定/分级响应
```

- **零新增 API**（复用 L6611 已取得的方向仓位）；
- 单批次调用只负责清理历史事件，**绝不产生守恒告警**（len<2 短路）；
- 由此 2.4 的两个删除条件（显式恢复 / 批次<2）均**可达**，R41 可实现。

### 2.2.1 观察语义（len≥2 时）

- 观察器内每轮重算：actual vs Σnet(同方向)；
- **显式收敛清除**：本轮观察到「无冲突」→ 删除该 (symbol, side) 的整份事件记录
  （不使用任何 `last_seen>T` 时间猜测）；
- 冲突持续 → 按 2.3 分级响应。

### 2.3 分级响应：有**有效事务证据**才宽限

**宽限资格 = 「有效在途平仓事务」五条件合取**（v1.2 阻断 1 收口）：

```python
b['is_active'] is True
and int(b['close_phase']) >= 1
and b['pending_close'] is True
and (b['close_op_id'] or '') != ''
and b['close_reason'] in {'market_confirming',
                          'limit_pending_normal',
                          'partial_closing'}
# 附加：close_reason == 'limit_pending_normal' 时还须
# (b['limit_close_order_id'] or '') != ''
```

事实依据（源码实证）：回滚路径（L9117-9119）只复位 `close_phase=0 /
pending_close=False / is_programmatic_cancel=False`，**保留 `close_reason` +
`close_op_id` 作审计信息**——单看 reason 会把已回滚批次的陈旧标记当成在途
事务，给真实外部减仓错误宽限 300s。五条件合取下，回滚后的批次
`close_phase>=1 ∧ pending_close=True` 必然不成立，陈旧 reason 被结构性排除。

**分级规则**：

```
同方向守恒正常
  → 删除该 (symbol, side) 整份事件记录（显式收敛）
同方向冲突，且同方向无任何批次满足「有效在途事务」
  → 立即 critical（f1e135 外部减仓场景零延迟；陈旧 reason 不构成宽限）
同方向冲突，且存在有效在途事务
  → 首见 warning（TG level='warning'，console 记录）+ 开始计时
  → 持续 ≥300s 仍冲突 → critical
【事件内单调棘轮】critical_count > 0 后，即使后来出现有效在途事务，
  也不得降级回 warning / 重新获得 300s 宽限（事件内单向，防实现漂移）
```

- `partial_resize_pending` / `limit_cancel_restore_pending` **不进宽限集合**：
  两者净量已 durable（partial 已确认入账 / restore 归属已持久化），不构成
  「仓位在途未归档」的可解释性；
- `manual_review` / 异常态不进宽限集合：维持既有 loud 通道；
- `partial_closing` 纳入宽限（transient 成交确认窗，其 BEGIN 必然
  close_phase=1 + pending_close=True，天然满足五条件），但其**既有冻结 loud
  告警通道照常运行**——两通道并行，互不抑制；
- 无有效在途事务的外部减仓（f1e135 原场景）：**立即 critical，零延迟**。

### 2.4 事件状态：一次冲突一份记录，事件结束整份删除（v1.2 阻断 2 收口）

每个 `(symbol, side)` 至多一份事件记录：

```python
{'first_seen': ts, 'warning_sent': bool, 'critical_count': int}
```

**整份删除条件（任一即删；v1.3 起两个条件均经无条件调用契约**可达**）**：

1. 该轮观察判定守恒恢复（显式收敛）；
2. 同方向活跃批次 < 2（观察器入口短路时删除——sibling 消失后事件失去上下文，
   残留记录只会污染未来事件）。

由此：

- `≤3 次 critical` 是**单事件上限**，事件结束即归零——未来真实事故永不被
  历史计数封口（旧 `_conservation_alert_count` 只增不清、进程生命周期上限、
  达 3 即永久静默的缺陷随本设计一并消亡）；
- 事件记录不存在跨事件残留 → 「残留状态→提前 critical」路径被根除，不再
  依赖 fail-noisy 辩护（P6 本身就是告警质量修复，旧事件污染新事件=误报）；
- 新事件一律从 warning 首见开始（若届时存在有效在途事务）或立即 critical
  （若不存在）。

### 2.5 并发与工程约束

- 事件记录的创建/认领/删除用一把小锁保护（多方向监控线程可能并发触达同
  symbol 的两个 side）；**通知发送在锁外**（防持锁做 I/O）；
- 状态为内存态（重启清零）：重启后首轮即有观察，warning 首见不会因重启丢失
  报警机会；
- `CONSERVATION_GRACE_S = 300` 模块常量。

### 2.6 改动面

仅 `trader_260725.py`：`_check_conservation_conflict`（+side 过滤参数）、
`_maybe_report_conservation_conflict`（重写为分级观察器 + 事件记录管理）、
监控调用点（L6775-6782 分支外提为每轮调用）、**删除旧
`_conservation_alert_count` 语义**（被事件记录的 `critical_count` 取代）。
不新增账本字段、不改 trade_state 结构。

## 3. 测试计划（RED-first，R34–R42，v1.2 裁定 + v1.3 补充）

- **R34**：**有效**在途平仓（limit_pending_normal + phase=1 + pending=True +
  op_id + order_id）瞬时冲突 → 仅 warning，零 critical；
- **R35**：无有效在途事务的外部减仓冲突 → **立即** critical（零延迟验证）；
- **R36**：有效在途事务下冲突持续 ≥300s（注入时钟）→ 升级 critical；
- **R37**：显式观察到收敛（下一轮观察无冲突）→ 整份事件记录删除；新事件重新
  从 warning 计时；
- **R38**（v1.2 重写）：**有效在途事务** + 首见 warning 后观测间隔 >300s
  （稀疏观测）→ 再次观测仍冲突 → **必须升级 critical**——稀疏宽限路径本身
  必须可达（v1.1 版「无在途事务」场景只走立即告警，测不到稀疏宽限，作废）；
- **R39**：Hedge 双方向批次并存 → LONG/SHORT 的 Σnet、事件记录、告警计数完全
  隔离；
- **R40**（v1.2 新增，陈旧 reason 负测）：批次 `close_phase=0 + pending_close=
  False + close_reason='market_confirming'`（回滚后审计残留）+ 冲突 → **立即**
  critical，绝不进入宽限；
- **R41**（v1.2 新增，完整重置）：冲突首警（warning/critical）→ 同方向 sibling
  消失（<2）或守恒恢复 → 事件记录整份删除 → 新冲突**重新从 warning 开始**，
  且新事件的 critical_count 从 0 计（≤3 为单事件上限）；
- **R42**（v1.3 新增，单调棘轮）：事件内已触发 critical（critical_count≥1）→
  之后同一事件中出现有效在途事务 → **仍 critical**，不得降级回 warning 或
  重新计时（锁定 2.3 单调规则）；
- 回归：41 rc=0 基线不动；既有期望「冲突即报」的守恒测试改按新分级重基线。

## 4. 开放问题裁决记录（ChatGPT 已裁定，本稿照办）

1. 方案 A：**否决**。
2. 全局 T=600：**否决**；有事务证据时 300s。
3. `partial_closing`：纳入临时宽限，既有异常 loud 保留。
4. 告警上下文：**纳入**，只列同方向批次 batch_id/net/phase/reason。

## 5. 遗留给实施期的核实点（非开放问题）

- 同方向 sibling 的判定字段：`b['side']`（'BUY'/'SELL'）与 L6611 的 side 同源；
- warning 级别 TG 发送需确认 `_send_tg` 对 level='warning' 的既有支持
  （实施期验证，若只支持 critical/info 则用 info + 前缀标记）；
- 除复用 L6611 已有仓位外，本设计**零新增 API 查询**。

---
*修订链：v1.0（2026-09-03 晨）→ v1.1（2026-09-03 21:3x，吸收三项缺口裁定）。*
*全部行号可在 GitHub 4d67bce 直接核对。*

---
*修订链：v1.0 → v1.1（三项缺口）→ v1.2（两项阻断：五条件宽限 + 事件整份删除）→ v1.3（调用契约收口：每轮无条件调用 + 观察器内 len<2 短路清理 + 事件内单调棘轮）。CONDITIONALLY ALIGNED → 补齐即批准实施。全部行号可在 GitHub 4d67bce 直接核对。*
