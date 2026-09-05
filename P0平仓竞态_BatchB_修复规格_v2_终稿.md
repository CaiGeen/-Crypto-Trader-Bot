# P0 平仓竞态修复 Batch B v2 实施规格（终稿，ChatGPT APPROVED 2026-08-29）

> 状态：**APPROVED（有条件批准）**——五决策点锁定为实施约束 + 两处规格修正已并入本文。
> 基线：HEAD = **01bb44f**（Batch A 991f84f + Batch C 01bb44f 已封版）。
> v1 全文（`P0平仓竞态_BatchB_修复规格_v1_送审ChatGPT.md`）中未被本文推翻的部分继续有效；
> 本文只记录**终审裁定的锁定项与修正项**，冲突时以本文为准。

---

## §1 ChatGPT 终审裁决总览（2026-08-29）

| 项 | 裁决 | 锁定约束 |
|---|---|---|
| 总体 | **APPROVED**，允许实施（RED → 实施阶段） | 架构方向：close 清理从「状态删除动作」升级为「经过证明的状态迁移」 |
| D-B1 | **贡献扣减法** | `本批贡献 = symbol持仓 − Σ其他活跃批次 current_filled`；**贡献 ≤ 0 才允许 position_zero=True**（不能"接近 0"）；精度容差 `abs(贡献) ≤ amount_precision`（交易所 precision），不用裸 epsilon |
| D-B2 | **单次复扫** | 一次撤、一次证明、失败→下一周期；converge 内不做无限重试（防隐藏线程） |
| D-B3 | **批准终态化，附三条件** | `PENDING_VERIFY → ABSENT` 必须同时满足 `position_zero=True + exchange_scan='zero' + 本批次无 L2 匹配`，写成代码条件，不能仅凭"订单没看到" |
| D-B4 | **L3 不阻塞 clear** | L1/L2 = 程序负责；L3 = 人工负责；proof 携带 `l3_orphans` 列示即可 |
| D-B5 | **平仓成功照报 + 残单收敛提示** | position=SUCCESS、cleanup=PENDING 两态分离，不谎报失败（防用户二次 close） |
| 实施纪律 | **converge 禁止内部调用 clear** | 调用栈必须可审计：`proof = converge(); if proof: clear(proof)`——防未来绕过 proof 门 |
| 签名纪律 | **唯一入口 proof object** | `clear_batch_state(symbol, batch_id, proof=None)`；**禁止** `proof=True` / `force=False` / `skip_verify` 任何形式布尔逃生门 |
| 范围冻结 | B0 / converge helper / proof gate / 10 调用点 / test_b_batch.py / AST 锚点修复 ✅；StateManager / 新线程 / Redis / CAS / L3 自动撤 / force clear / 修改 Batch C merge 规则 ❌ | 到此为止，不扩成"所有 load→modify→save 锁内化" |

---

## §2 规格修正 1：proof 门 scope 校验——当前活跃敞口，不用历史痕迹

**v1 §4.1 条件 4 作废**（"有已成交份额/registry 存在非终态历史痕迹 → FULL"）。

**修正后（ChatGPT 原文语义）**：scope 必要性由**当前 batch 是否存在 active exposure** 决定：

```
current_filled > 0          （last_filled_count > 0）
or close_order 存在          （limit_close_order_id 非空，或 close_phase ≥ 1 平仓流程中）
or position contribution > 0（由 proof['position_zero'] 侧保证）
→ 需要 FULL；否则 PRE_ENTRY 即可
```

理由：Batch C 终态集（PROGRAMMATIC_CANCELED/CONFIRMED/ABSENT/…）的**历史存在**不代表**当前需要 FULL**。落码为 `_batch_has_active_exposure(b_data)` helper（纯状态字段判定，锁内可调，零 API）；converge 的 proof['scope'] 与门的复核都用同一 helper，两侧语义必然一致。

---

## §3 规格修正 2：B0 的审计定位——提前终态化已知 id，不是证明批次完成

B0（结算段撤 TP）经 `_commit_registry_txn`/`_update_registry` 写 `PROGRAMMATIC_CANCELED` **必须附 `reason='close_settled_canceled_tp'` 并保留**（v1 已有，此处升格为硬约束：该 reason 字段**不得改动**）。审计语义钉死：

- B0 只是**提前终态化已知 id**（close_phase=2 期间结算段的单点处置）；
- B0 **不是**"证明整个 batch 完成"——批次完成的唯一证明 = converge proof + clear 门通过；
- 后续 converge 再扫描时看到 `registry: PROGRAMMATIC_CANCELED + exchange: 不存在` = 一致，无冲突。

---

## §4 D-B1 落码公式（锁定版）

```
symbol_pos    = _get_current_position_amt(symbol, is_hedge_mode, side)   # None → UNKNOWN 不 clear
others_filled = Σ 其他 is_active 批次 sum(target_amounts[:last_filled_count])
contribution  = symbol_pos − others_filled
amount_precision = markets[symbol].precision.amount（小数位/步长两种语义归一；缺失 → 0 严格）
position_zero = (contribution <= 0) or (abs(contribution) <= amount_precision)
```

- `contribution <= 0`：本批次份额已被完全消化（其他批次解释了全部持仓）；
- 精度容差只用于 `0 < contribution <= amount_precision` 的精度噪声带；
- `contribution > amount_precision`（明确的正贡献）→ **不收敛、不 clear、critical**。

---

## §5 实施顺序（ChatGPT 批准，十步冻结）

```
1. 备份 backups/20260829_p0_batchB_before_/
2. RED：test_b_batch.py 记录失败
3. 实施 B0（结算撤 TP，reason=close_settled_canceled_tp）
4. 实施 converge（_converge_batch_orders_before_clear）
5. 实施 proof gate（clear_batch_state 持锁验证）
6. 逐个改 10 个 clear 调用点
7. GREEN：G-B1~G-B10
8. 回归：close_race 28/28 / c_batch 22/22×3 / b2_restart 9/9 / 全量 33 文件
9. git diff 审计
10. 一次 commit
```

## §6 Batch B 完成目标（终态定义）

```
close_phase=3 只能由 converge proof 触发（唯一写入点 = proof 门内）。
无 proof：不能 clear。
```

## §7 停下来复核的触发条件（ChatGPT 实施纪律）

发现以下任一情况**不自行扩大范围**，停下来呈报复核：
- L1/L2 归属冲突；
- clear 调用点遗漏（>10 处或语义不符）；
- fake 测试污染（MagicMock 吸收新 helper 惯例坑，第 7 次实证风险）；
- close_phase=3 出现第二写入点。

## §8 回归面预告（v1 §6 回归链补充，本基线 Grep 实证）

直接调 `clear_batch_state` 或经 stale 清理路径的既有测试，Batch B 后需适配
（proof 直传 / fake 绑定 converge / 两参 lambda 补 **kw）：
- `test_c_batch.py` L132/161/175/178/196（直调 clear → 需传合法 proof）；
- `test_tp_validation.py` L310（直调真实 clear）；
- `test_r12_state_backup.py` L101 + AST 持久化断言（proof 门不改 persist 结构，预期零适配，跑后核实）；
- `test_b2_crashsafe_entry.py` L103/285、`test_crash_injection.py` L119/285
  （`lambda s, b:` 两参绑定 + recover_active_batches stale 路径 → 需补 converge 绑定与 **kw）；
- `test_recover_semantics.py` L54（两参 lambda → MagicMock converge 返回 truthy，
  proof=kwarg 会 TypeError → 补 **kw）；
- `test_b2_restart_semantics.py` L233（恢复全链路 → 视 fake 结构补 converge 绑定）；
- `test_close_race_replay.py` L275（真实 clear 绑定 → 补 proof 透传，靠 10 调用点真实 converge 驱动）。
