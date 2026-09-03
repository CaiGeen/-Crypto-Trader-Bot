# P5 `/closecancel` 设计草案 v3.1 —— 只读设计审计，零代码改动

日期：2026-09-03 ｜ 状态：v3 + 4 项接线契约补齐，待 ChatGPT 终签 FULLY ALIGNED
行号基准 857af40。v3.1 = v3 核心不变，仅补齐 ChatGPT 点名的 4 个现有机制接线契约。

## 0. 既有缺陷（不变，v1 发现 + ChatGPT 确认）

`_monitor_limit_close` L10259-10268 canceled/expired 分支只清 3 字段不恢复 ACTIVE
→ app 手动撤限价单 = 批次永久冻结；不检查 filled>0 → 部分成交无归属。P5 恢复
路径同时闭环本分支。

## 1. 核心：按成交量四态分型（v2 保留，不变）

分型依据 = confirmed_filled（fetch_order 权威读取）vs pre_net_qty；
`-2011/Unknown order` 只证明「不能再撤」，不证明 filled=0：

| confirmed_filled | 分型 | 处置 |
|---|---|---|
| ≈ 0 | PURE_CANCEL | 原子切 restore_pending → 只补 TP（SL 保留+核实）→ 专用 CAS 回 ACTIVE |
| 0 < filled < pre_net_qty | PARTIAL_FILL_CANCEL | 原子更新 realized_reduce_amount/cost + 切 restore_pending → 复用 P1 按剩余净量恢复双腿 → 专用 CAS 回 ACTIVE |
| ≈ pre_net_qty | FULL_FILL | 共享幂等 finalizer → converge + clear，绝不恢复 ACTIVE |
| > pre_net_qty / PENDING / UNKNOWN / NOT_CONFIRMED | Fail-Closed | 保持冻结 + 一次 critical + 命令可重发 |

## 2. 原子归属：close_reason 单向迁移防双计（v3 改动 1，替代 accounted 字段）

删除 v2 的 `limit_close_accounted_filled` 字段与 delta 机制。防双计改用**状态守卫**：

```python
# 复用 /partial 的 rollback CAS 模板（L3213-3228 同构）
with self._state_lock:
    锁内重读 → 校验 close_op_id + close_phase==1 + close_reason=='limit_pending_normal'
    pre_net_qty, pre_net_cost = self._batch_net_position(b)
    confirmed_filled <= pre_net_qty + 1e-12（超出 → Fail-Closed）
    reduce_cost_delta = confirmed_filled * pre_net_cost / pre_net_qty   # ← v3 修正：
    #   /partial L3219 同口径（账面净成本比例分摊），非成交均价 × delta
    b['realized_reduce_amount'] += confirmed_filled
    b['realized_reduce_cost']   += reduce_cost_delta
    b['close_reason'] = 'limit_cancel_restore_pending'
    _persist_states 必须 True
```

**防双计原理**：只有 `close_reason == limit_pending_normal` 才能提交；首次提交
改变 close_reason → 并发线程/重试的 CAS 立即失败 → 无需 accounted 字段。
（crash 后重试：reason 已是 restore_pending → 资格检查拒绝重入归属分支，
直接进 §3 恢复。）

## 3. 保护单恢复：复用 P1 机制（v3 改动 2，不建平行状态机）

P1 的冻结期保护单维护由 `_partial_resize_owner_ok` 窄放行（owner =
`partial_resize_pending`）。P5 最小扩展：

- `_partial_resize_owner_ok`（或同构窄判据）增加放行：
  `close_reason == 'limit_cancel_restore_pending'` 且 `close_op_id` 匹配——
  **P1/P5 共用同一套 G1/G2/G3b 冻结期 create/resize 机制**；
- **接线契约（v3.1 补，ChatGPT 点名）**：P1 的 reason 校验不止 owner 判据一处——
  `_resize_protection_after_partial` 的两次 stage commit、`_resume_partial_resize`、
  启动恢复分发、运行期自愈调度共四处硬编码接受 `partial_resize_pending`，
  **统一扩展为同时接受 `limit_cancel_restore_pending`**，否则 R7 的自动恢复走不通；
  恢复时 SL intent 按当前净量核对：**匹配则收编（不重建），不匹配才 resize**；
- **PURE_CANCEL**：SL 保留未撤 → 只核实匹配在场（fetch_order）；TP 已撤 →
  专用窄 re-arm 后按**原净量**重建 TP；
- **PARTIAL_FILL_CANCEL**：按**剩余净量** resize 双腿（SL resize + TP re-arm 后
  重建，数量均 = 新净量），完全复用 P1 的 resize 链（撤旧→有界确认→create→
  verify→CONFIRMED）；
- TP re-arm 专用窄事务五条件（不变）：restore_pending + close_phase==1 +
  close_op_id==owner + 旧 TP registry==PROGRAMMATIC_CANCELED +
  terminated_reason==close_requested_canceled → 开新一代 TP 订单；
  generic `_assert_create_allowed`/`_update_registry` 终态守卫**零改动**；
- 两腿 verify 完成 → 专用 CAS 回 ACTIVE：
  `close_phase=0, pending_close=False, is_programmatic_cancel=False, close_reason=''`
  并**原子清理限价事务字段** `limit_close_order_id / limit_close_price /
  limit_close_mode`（否则 ACTIVE 批次仍携带一张已终态的「活跃限价平仓单」镜像，
  v3.1 补，ChatGPT 点名）；
  close_op_id 保留审计；
- 恢复前置守恒门保留：`_close_amount_guard` ≈ 当前净量，失败不恢复 + critical。

## 4. FULL_FILL：共享幂等 finalizer（v3 改动 3）

抽取 `/closecancel` 与 `_monitor_limit_close` **真正共用的 finalizer**：

```
按单确认 FULL_FILL（fetch_order 权威）
→ CAS 认领（settled_by_limit_close=True + close_phase=2）
→ PnL 以 (symbol, limit_close_order_id) 幂等记录（去重键；或全局唯一 close_op_id，
   不假定不同 symbol 的 order ID 永不重复）
→ 调用现有 converge
→ clear_batch_state
失败/中断 → 保持 close_phase=2 → 重启恢复链有确定路径续跑
```

**接管语义（v3.1 修正，ChatGPT 点名）**：CAS 只负责确认 FULL_FILL 事实，
**不授予不可接管的独占权**——CAS 失败者不得直接退出。任何调用方/恢复线程看到
同一 `close_op_id + close_phase=2 + settled_by_limit_close=True`，都必须继续调用
幂等 finalizer（PnL 去重保证不重记）。否则存在窗口：monitor 赢得 CAS → 崩溃 →
command CAS 失败退出 → PnL/converge/clear 无人执行。
（v1「既有 settled_by_limit_close + 持仓归零已覆盖」表述删除：源码现状既不补记
PnL，sibling 批次在场时 aggregate 也不会归零。）
现有监控成交分支（L10119+）与缺陷分支均迁移到本 finalizer 的调用方。

## 5. 最小状态流（实施对照）

```
/closecancel → cancel 限价单 → _confirm_close_filled（fetch_order 权威）
├─ TERMINAL_ZERO(≈0)   → 原子切 restore_pending → 验证 SL + re-arm TP → 专用 CAS 回 ACTIVE
├─ PARTIAL(0<filled<net) → 原子更新 realized_reduce_amount/cost + 切 restore_pending
│                          → 复用 P1 按剩余净量恢复双腿 → 专用 CAS 回 ACTIVE
├─ CONFIRMED_FULL      → 共享幂等 finalizer → converge + clear
└─ PENDING/UNKNOWN/NOT_CONFIRMED → 保持冻结 + critical（命令可重发）
```

crash recovery 分型（v2 §7 不变）：limit_pending_normal 可证明终态 → 重入四态分型；
restore_pending → 自动续跑保护恢复（绝不再发 close order）；不可证明 → loud 人工。

## 6. RED 测试（R1-R11，只强化语义不扩张）

- R1 资格拒绝矩阵
- **R2 强化：PURE_CANCEL 只补 TP，匹配的 SL 不重建**
- **R3 强化：PARTIAL 使用正确账面成本（net_cost 比例分摊）+ 按剩余净量恢复双腿**
- **R4 强化：FULL_FILL 在 monitor 死亡时仍完成 PnL、converge、clear**
- R5 恢复守恒门失败 → 不恢复 + critical
- R6 幂等：重复 /closecancel
- **R8（v3.1 补回，P5 本源缺陷）：无 /closecancel 介入时，monitor 自己发现
  canceled/expired → 必须进入同一终态分型与恢复路径，不再永久冻结**
- **R7/R10 合并强化：每个持久化断点重启 → 不重发平仓单、不重复记账**
- R9 强化：命令线程与 monitor 并发 → 只允许一次状态提交 + 一条 PnL
- **R11 强化：恢复 ACTIVE 后 is_programmatic_cancel=False，正常结算不被吞**

## 7. 明确不做（不变）

不支持 market_confirming / partial_* / partial_closing 撤销；不做自动接市价；
不动 3s 轮询；P6 独立设计（基于本冻结语义 + PROVEN owned delta）。
