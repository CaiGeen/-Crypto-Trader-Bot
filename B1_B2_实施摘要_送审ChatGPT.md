# B1+B2 全套实施摘要 — 送审 ChatGPT 复核

> **规格出处**：`P0最终规格_状态机与Create仲裁_v2_送审ChatGPT.md`（项目根目录）
> **源码**：`trader_260725.py`（5137 行）
> **提交链**：68f35a9 (B1) → 86372ec (B2-0/2/1) → 69ba58e (B2-3) → b972a92 (B2-4) → fa091ce (B2-5) → 344ba08 (B2-6) → 574df2f (B2-7)
> **测试**：19 套件 196 场景全绿（+2 SKIP），ccxt 4.5.68，项目 .venv
> **生成时间**：2026-08-20 19:30 北京时间

---

## 一、事故背景

C5 实盘事故（8-19 22:53）：`fetch_order` 漏带 `params={'stop': True}`，条件单在 algo 端点查不到 → 12 处 verify 全误判 not_found → 不 Commit → 无限重挂 → **24 个真实孤儿 SL/TP**。用户已手动移出。

根因修复 = B1 `verify order_kind='conditional'` → `params={'stop': True}` 路由。

P0 规格 v2 在此基础上定义了完整的 6 态状态机 + Create 仲裁闸门 + 崩溃安全时序 + 重启恢复规则。

---

## 二、B1 实施摘要（commit 68f35a9）

| 规格 | 实施 | 源码行 |
|------|------|--------|
| §3 状态机（6 态 + HARD_LOCK 标志） | `ABSENT / PENDING_CREATE / PENDING_VERIFY / CONFIRMED / NOT_CONFIRMED / FAILED` + `hard_locked` 布尔标志 | registry 结构 |
| §5.8 verify order_kind 路由 | `_verify_order_created(order_id, symbol, order_kind='conditional')` — conditional → `params={'stop': True}` | L2109 |
| §5.1 幂等键含 batch_id | `_protection_identity(batch_id, role, layer, side)` → `f'{batch_id}\|{role}\|L{layer}\|{side}'` | L2171 |
| §5.2 registry 持久化 | `_update_registry(symbol, batch_id, identity, state, order_id, ...)` 原子写 | L2190 |
| §6 重启恢复 | `_recheck_registry_self_heal(symbol, batch_id)` — 遍历未决条目 → verify → 收编 | L2412 |
| 异常分类 | `_classify_create_exception(e)` — ExchangeError / NetworkError 分流 | L2159 |
| 11 处 verify 显式 'conditional' | 全部 create_order 调用点后的 verify 均带 `order_kind='conditional'` | 见 §5.8 映射表 |

**测试**：`test_b1_state_machine.py` — 9 场景（T1-T9）全绿

---

## 三、B2 实施摘要（7 个子批次）

### B2-0：verify 三态语义统一（commit 86372ec 合并）

| 规格 | 实施 | 源码行 |
|------|------|--------|
| §3.2 verify 分支禁 raise 禁计数禁自动重挂 | `_verify_and_update_registry` 统一三态入口：not_found → NOT_CONFIRMED（不 raise）、network → PENDING_VERIFY（id_unknown）、found → 匹配 → CONFIRMED | L2136 |
| 补挂 SL/TP / 降级恢复 not_found → NOT_CONFIRMED | 残留 `not_found→raise→计数` 全改为 NOT_CONFIRMED | 多处 |
| create 异常 classify 分流 | NetworkError → PENDING_CREATE(id_unknown) 不计数；ExchangeError → FAILED 计数 | L2159 |

**测试**：`test_b2_verify_semantics.py` — 6 场景全绿

### B2-2 / B2-1：intent 指纹不可变 + 自愈匹配（commit 86372ec 合并）

| 规格 | 实施 | 源码行 |
|------|------|--------|
| §5.2 intent 先落盘（崩溃安全） | `_build_intent(symbol, side, qty, order_type, stop_price, reduce_only)` 6 字段不可变指纹 | L2348 |
| intent 防漂移 | `entry.setdefault('intent', ...)` — 已有则不覆盖 | _update_registry 内 |
| §6.3 _order_matches_intent | `FOUND + intent 完整匹配 = CONFIRMED + 收编` / `不匹配 = MISMATCH + critical 不收编` / `无 intent = 保守 NOT_CONFIRMED` | L2370 |
| 5 保护单调用点 PENDING_CREATE + intent 先落盘 | create_order 前先 `_update_registry(state='PENDING_CREATE', intent=...)` | 6 处 |

**测试**：`test_b2_intent.py` — 10 场景全绿

### B2-3：Create 仲裁闸门（commit 69ba58e）

| 规格 | 实施 | 源码行 |
|------|------|--------|
| §5.3 仲裁器唯一入口 | `_assert_create_allowed(symbol, batch_id, identity, desc)` — cooldown 置最前 + 禁止集 `{PENDING_CREATE, PENDING_VERIFY, NOT_CONFIRMED, CONFIRMED, MISMATCH}`，仅 `FAILED / ABSENT / 无条目` 允许 | L2232 |
| 6 调用点门控在 PENDING_CREATE 持久化之前 | 每个 create_order 前 `allowed, reason = self._assert_create_allowed(...)` → 不允许则告警跳过 | 6 处 |
| 被阻止 → 告警跳过 | `if not allowed: send_tg_notification(...); return` | 6 处 |

**测试**：`test_b2_create_gate.py` — 10 场景全绿

### B2-4：HARD_LOCK 真熔断（commit b972a92）

| 规格 | 实施 | 源码行 |
|------|------|--------|
| §5.4 HARD_LOCK 真熔断 | `fail_count >= 5 → hard_locked=True 落盘 + 进入时 1 次 critical 此后静默` | _update_registry 内 |
| §6.2 启动校验 | `_validate_registry_locks_on_startup()` — 非法解锁回滚+critical / 合法解锁审计三字段不干预 / FAILED 旧数据补置锁 / 已锁静默 | L2277 |
| §5.5 解锁审计三字段 | `unlock_reason / unlock_time / unlock_operator` | _validate 内检查 |
| 顺手修复预生成 TP except else 悬空 PENDING_CREATE | 修复悬空分支 | 预生成 TP 段 |

**测试**：`test_b2_hardlock.py` — 16 场景全绿

### B2-5：开仓循环崩溃安全前置落盘（commit fa091ce）

| 规格 | 实施 | 源码行 |
|------|------|--------|
| §5.6 Case F 开仓循环崩溃安全 | 进循环前落批次骨架 + 全部将尝试层 `ENTRY PENDING_CREATE + intent`（价格过滤同规则，跳过层不预写） | L1791 区域 |
| 循环内逐单更新 | create 成功 → `_update_registry(state='PENDING_VERIFY', order_id, id_known=True)`；-2021 → `state='ABSENT'` | 循环内 |
| 循环结束业务 Commit | 完整 batch_state_data 构造后、save_batch_state 前合并 registry，全部 ENTRY → CONFIRMED | 循环后 |
| 恢复护栏 | `_registry_has_unresolved_entries(b_data)` — 骨架批次保留证据不清理不接管，终态照常清理无回归 | L2177 |
| 骨架补元数据 | `entry_layers` + `entry_stop_steps`（layer→SL 价格权威映射） | 骨架段 |

**测试**：`test_b2_crashsafe_entry.py` — 15 场景全绿（T1 断言查骨架快照 save_snapshots[0]）

### B2-6：Case A-F crash injection 矩阵（commit 344ba08）

| 规格 | 实施 | 源码行 |
|------|------|--------|
| §6.3 身份签名匹配（无 ID 自愈） | `_self_heal_no_id(symbol, batch_id)` — PENDING_CREATE / PENDING_VERIFY(id_unknown) 无 order_id 的 ENTRY 条目 → 双通道 open orders 快照（normal + stop=True）合并 → intent 全等匹配：命中唯一 → CONFIRMED + order_id 收编 / 多条 → NOT_CONFIRMED + critical / 快照 VALID 无单 → NOT_CONFIRMED（缺席≠从未存在）/ 快照 INVALID → 维持 PENDING_VERIFY(id_unknown) 静默下轮 | L2477 |
| Case F 收编重建 | `_rebuild_entry_orders_from_registry(symbol, batch_id)` — CONFIRMED ENTRY 按 layer 重建 `entry_orders + stop_steps / target_amounts / batch_total_amount / layer_sl_params / prepared_tp_params / pending_sl_orders`（接管监控必需，否则层成交后 SL/TP 无参数，违反不变量②） | L2565 |
| recover_active_batches 骨架分支升级 | B2-5「保留证据不清理」→ B2-6 自愈对账（`_self_heal_no_id + _recheck_registry_self_heal + rebuild`）→ 收编成功正常接管监控；仍无法确认 → 保留证据待人工 | recover 段 |

**测试**：`test_crash_injection.py` — 20 场景全绿（Case A-F 矩阵，双通道快照 + fetch_order 查询表）

**Case 矩阵覆盖**：
- A：干净态正常 Create
- B1：PENDING_CREATE + 快照唯一匹配 → CONFIRMED + 收编
- B2：快照 VALID 无单 → NOT_CONFIRMED
- B3：快照 INVALID → 维持 PENDING_VERIFY(id_unknown)
- C：PENDING_CREATE + 交易所已有真实单 → 身份匹配收编（24 孤儿单事故通用防线）
- D：PENDING_VERIFY(id_known) → verify 自愈 CONFIRMED
- E1：OrderNotFound → NOT_CONFIRMED
- E2：NetworkError → 维持 PENDING_VERIFY
- F：开仓循环第 3 层崩溃 → recover_active_batches 完整路径（L0/L1 身份匹配收编 + entry_orders 重建接管，L2 缺失层 NOT_CONFIRMED 人工裁决，零补挂任何层）

### B2-7：重启恢复语义测试（commit 574df2f，纯测试增强）

无源码改动。用真实 CryptoTrader 实例 + 真实 trade_state.json 文件 I/O 替代 MagicMock fake。

**测试**：`test_b2_restart_semantics.py` — 9 场景全绿

| 场景 | 覆盖规格 | 断言 |
|------|----------|------|
| R1 | §6 不变量#15 | PENDING_VERIFY 连续 5 次重启幂等——状态不变、闸门始终拒绝、verify 持续 NetworkError → 维持原状态不 FAILED 不计数 |
| R2a | §5.4 + §6.2 | FAILED fail_count=5 → 启动校验补置硬锁落盘 |
| R2b | §5.4 + §6.2 | 再重启 HARD_LOCK 仍锁定 + 闸门拦截 |
| R2c | §5.5 + §6.2 | 非法解锁（无审计三字段）回滚 + critical |
| R2d | §5.5 + §6.2 | 合法解锁（审计三字段）不干预 |
| R3+R4 | §6 + §6.3 | 骨架批次真实文件重启 → recover_active_batches → L0/L1 身份匹配收编 CONFIRMED + entry_orders 重建接管 + L2 NOT_CONFIRMED 人工裁决 + 零 Create 零 Cancel |
| R5 | §5.4 | FAILED fail_count=2 (<5) 不锁可重试 → 新失败 3 → 重启仍 3 且可重试（持久化安全状态） |

真实 ccxt 异常对象：`ccxt.NetworkError`（R1 自愈、R2 fetch_order）、`ccxt.OrderNotFound`（R3 查询表缺省）——非字符串模拟。

---

## 四、全量回归证据

19 套件 196/196 绿（+2 SKIP）：

| # | 文件 | 场景 | 覆盖 |
|---|------|------|------|
| 1 | test_recover_semantics.py | 5 | R3-v2 三态语义 |
| 2 | test_cooldown_alert.py | 4 | R1 熔断告警 |
| 3 | test_sg1_ready_gate.py | 7 | SG1 READY 门控 |
| 4 | test_sg2_risk_gate.py | 11 | SG2 加仓前风险闸门 |
| 5 | test_tg_fallback.py | 4 | TG 通知降级 |
| 6 | test_r6_bare_calls.py | 3 | C1/R6 裸调用收编 |
| 7 | test_r10_limit_close_alert.py | 4 | C2/R10 限价平仓 |
| 8 | test_r12_state_backup.py | 5 | C3/R12 状态备份 |
| 9 | test_sg3_p1.py | 28 | C4/SG3-P1 保护单有效性 |
| 10 | test_sg4.py | 25+2SKIP | C5/SG4 Create-Verify-Commit + 14 处 retries=1 |
| 11 | test_b1_state_machine.py | 9 | B1 状态机语义 |
| 12 | test_b2_verify_semantics.py | 6 | B2-0 verify 三态 |
| 13 | test_b2_intent.py | 10 | B2-2/B2-1 intent + 匹配 |
| 14 | test_b2_create_gate.py | 10 | B2-3 仲裁闸门 |
| 15 | test_b2_hardlock.py | 16 | B2-4 硬锁 |
| 16 | test_b2_crashsafe_entry.py | 15 | B2-5 开仓循环崩溃安全 |
| 17 | test_crash_injection.py | 20 | B2-6 Case A-F 矩阵 |
| 18 | test_b2_restart_semantics.py | 9 | B2-7 重启恢复语义 |
| 19 | test_orphan_guard.py | 5 | P0 孤儿进程防护 |
| **合计** | | **196 + 2 SKIP** | |

**运行环境**：`/g/my-crypto-bot/.venv/Scripts/python.exe`（ccxt 4.5.68）

---

## 五、规格合规映射

| 规格章节 | 规格内容 | 实施 | 测试 | 状态 |
|----------|----------|------|------|------|
| §2 | 交易系统宪法（8+8 条不变量） | 全部代码路径遵循 | 各套件间接覆盖 | ✅ |
| §3.1 | 6 持久化态 + HARD_LOCK 标志 | registry 结构 | b1 T4-T9, b2_verify | ✅ |
| §3.2 | 状态语义（FAILED 仅限 ExchangeError） | _classify_create_exception | b1 T3, b2_verify T1-T3 | ✅ |
| §4 | 状态转移表（18 条转移） | _verify_and_update_registry + _update_registry + _recheck_registry_self_heal + _self_heal_no_id | 全套件 | ✅ |
| §5.1 | 幂等键含 batch_id | _protection_identity | b1 T4 | ✅ |
| §5.2 | registry 持久化结构 + 崩溃安全时序 | _update_registry + _build_intent + save_batch_state | b1 T5, b2_intent, crashsafe | ✅ |
| §5.3 | 仲裁器唯一入口 | _assert_create_allowed | b2_create_gate | ✅ |
| §5.4 | HARD_LOCK 真熔断 | fail_count≥5 → hard_locked | b2_hardlock, restart R2a-b | ✅ |
| §5.5 | 解锁审计三字段 | _validate_registry_locks_on_startup | b2_hardlock, restart R2c-d | ✅ |
| §5.6 | 开仓条件单纳入 | 骨架 + 逐单更新 + 合并 Commit + 恢复护栏 | b2_crashsafe, crash F | ✅ |
| §5.7 | 14 处 Create 调用点 → 仲裁器映射 | 6 处保护单 + 1 处开仓 + 2 处平仓 + 预生成 | sg4 A 组 | ✅ |
| §5.8 | 11 处 Verify 调用点 → order_kind 映射 | 全部 conditional 显式 | sg4 B 组, b1 T1-T2 | ✅ |
| §6 | 重启恢复规则（恢复路径永不 Create） | recover_active_batches + _recheck_registry_self_heal + _self_heal_no_id + _rebuild_entry_orders_from_registry | restart R3+R4 | ✅ |
| §6.3 | 身份签名匹配（无 ID 自愈） | _self_heal_no_id + _order_matches_intent | crash B1-B3, C, restart R3 | ✅ |
| §7 | Case A-F 崩溃场景测试矩阵 | —（测试规格） | test_crash_injection | ✅ |
| §8 | 双通道统一订单视图 | fetch_open_orders(normal) + fetch_open_orders(stop=True) 合并 | crash B1-B3, restart R3 | ✅ |
| §10 | 418/cooldown 与 Create 仲裁关系 | cooldown 置仲裁器最前 | cooldown_alert | ✅ |

---

## 六、源码 helper 索引（全部 Grep 实证）

| Helper | 行号 | 职责 |
|--------|------|------|
| `_check_protection_order_validity` | L2072 | SG3-P1 保护单有效性校验 |
| `_verify_order_created` | L2109 | 三态 verify（conditional→stop=True 路由） |
| `_verify_and_update_registry` | L2136 | 统一三态入口（禁 raise 禁计数禁自动重挂） |
| `_classify_create_exception` | L2159 | ExchangeError / NetworkError 分流 |
| `_protection_identity` | L2171 | 幂等键 `{batch_id}\|{role}\|L{layer}\|{side}` |
| `_registry_has_unresolved_entries` | L2177 | 恢复护栏（骨架批次保留证据） |
| `_update_registry` | L2190 | registry 原子写（state/order_id/intent/fail_count） |
| `_assert_create_allowed` | L2232 | 仲裁闸门（cooldown + 禁止集） |
| `_validate_registry_locks_on_startup` | L2277 | 启动校验（补锁/回滚/审计） |
| `_build_intent` | L2348 | 6 字段不可变 intent 指纹 |
| `_order_matches_intent` | L2370 | FOUND + intent 全等 = CONFIRMED |
| `_recheck_registry_self_heal` | L2412 | 有 ID 条目自愈（verify → 收编） |
| `_self_heal_no_id` | L2477 | 无 ID 条目自愈（双通道身份匹配） |
| `_rebuild_entry_orders_from_registry` | L2565 | CONFIRMED ENTRY 按 layer 重建接管 |

---

## 七、遗留待裁决问题

1. **24 个真实孤儿 SL/TP 清理**：用户已手动移出 test_order.py，但孤儿单清理状态待确认
2. **P1-API-01 ban-until 持久化**：418 封禁事故的跨进程持久化（ChatGPT 审查建议 P1-High，未实施）
3. **已知张力**：方案 A 下手工持仓期间该交易对程序新信号全拒（D-004 候选）
4. **D-001 主功能未实施**：KAMA 跟踪止盈 + 自动保本，等审计稳定后推进（影子模式→灰度）

---

## 八、请 ChatGPT 重点复核

1. **状态转移完备性**：§4 转移表的 18 条转移是否全部被代码覆盖？有无遗漏路径？
2. **崩溃安全时序**：§5.2 六步时序（T0 意图先落盘 → T1 Create → T2c 落 ID → T3 verify → T4 Commit → 恢复）在 Case A-F 测试矩阵中是否每个崩溃窗口都有对应场景？
3. **恢复路径零 Create**：§6 恢复总原则——恢复路径只做确认和维持安全状态，永不 Create——代码中 recover_active_batches → _self_heal_no_id → _recheck_registry_self_heal 路径是否严格遵守？
4. **24 孤儿单事故防线**：Case C（PENDING_CREATE + 交易所已有真实单 → 身份匹配收编）是否真正阻止了同 identity 的二次 Create？
5. **HARD_LOCK 跨重启持久化**：R2a-d 场景是否充分证明硬锁状态在重启后不被清零、非法解锁被回滚？
6. **intent 不可变**：_build_intent 产出的 6 字段在 _update_registry 中是否真的不可覆盖？setdefault 是否足够？
7. **测试充分性**：19 套件 196 场景是否覆盖了所有关键路径？有无测试盲区？
8. **实盘恢复条件**：B1+B2 全套完成 + 全绿 + ChatGPT 复核通过后恢复实盘——还需要什么前置条件？
