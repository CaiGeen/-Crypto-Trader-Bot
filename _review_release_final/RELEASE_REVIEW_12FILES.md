# 12 文件发布终审（只读走查）

- **日期**：2026-09-29
- **性质**：**自审**——候选作者对自己改动的逐项走查，**不是独立复审**；独立裁决仍需 ChatGPT 读本文件 + PR。
- **基准**：生产 HEAD `4f0afb4` = `origin/backtest/markprice-4h-fetch`；merge-base `8c96867`；候选 head `e2f5f06`。
  发布差异补丁 `_review_fourteenth/release-diff-4f0afb4-to-AB.patch`
  （sha256 `c0a9d9d3…`、335102 B、12 文件 +4888 −149、命中生产独有文件 0）。
- **方法**：`git diff 8c96867 HEAD -- <file>` 逐 hunk 走查（trader 35 个 hunk / U2），加四项横向专项核对；
  **本次终审不修改任何代码**，故不触发补丁重建。
- **既有门禁**：2026-09-29 停机窗口 `run_test_gate.py --strict` → **`rc=0` ALL-GREEN**
  （pytest 125 passed + 9 subtests；脚本 56 项全 PASS；`test_orphan_guard` 首次 rc=0）。

---

## 1. 文件清单与规模

> 规模列 = `git diff --numstat 8c96867 HEAD`；hunk 列 = `git diff -U2` 口径（U0 下更碎：trader 46、baseline 15）。

| # | 文件 | 增/删 | 性质 | hunk |
|---|---|---|---|---|
| 1 | `R1R2_收敛审查_验收契约矩阵.md` | +481 / −0 | **新增**（文档） | 1 |
| 2 | `run_test_gate.py` | +8 / −17 | 门禁基线**收紧** | 3 |
| 3 | `test_b2_create_gate.py` | +24 / −11 | 断言选取方式改造 | 2 |
| 4 | `test_gate_baseline.py` | +116 / −30 | 门禁自测补强 | 6 |
| 5 | `test_monitor_poll_recovery.py` | +1048 / −11 | 新增 12 个 `check_*` | 13 |
| 6 | `test_poll_degradation.py` | +1901 / −0 | **新增**（41 用例） | 1 |
| 7 | `test_recover_semantics.py` | +39 / −3 | 夹具保真度补齐 | 6 |
| 8 | `test_sg3_p1.py` | +11 / −5 | 夹具补原始 `info.type` | 6 |
| 9 | `test_v64_p3_lifecycle.py` | +55 / −12 | 夹具修复 → 3/9 变 9/9 | 5 |
| 10 | `test_v64_partial_close.py` | +2 / −1 | AST 夹具 NS 补导入 | 1 |
| 11 | `tests_archive/test_b2_crashsafe_entry.py` | +6 / −0 | fake 初始化闸门属性 | 1 |
| 12 | `trader_260725.py` | +1197 / −59 | **唯一生产运行文件** | 35 |
| | **合计** | **+4888 / −149** | | |

> **11 个是测试/文档，1 个是生产代码。** 生产影响面 = `trader_260725.py` 一个文件。

---

## 2. `trader_260725.py` 分组走查（35 hunk → 8 组，**逐 hunk 全覆盖**）

> 35 个 hunk（`git diff -U2`）已全部归入 G1–G8；逐 hunk 头 + 首三行新增原文落盘于
> `_review_release_final/_hunkmap_trader.txt`（复核用）。归属：G1=1,2,4,16,18｜G2=7,8,9｜
> G3=6｜G4=3,5,14,15,17,31,33,34｜G5=19–26,28,29（另承接 hunk1 内的判据函数）｜
> G6=27,30｜G7=12,13｜G8=10,11,32,35。

### G1 降级跟踪、阈值与告警
- `L357` 起 +101 行：类级常量 `POLL_FAIL_ALERT_ROUNDS=3`、`POLL_STALE_ALERT_SECONDS=120.0`、
  `POLL_ALERT_RETRY_INTERVAL=900`、白名单 `_STOP_ORDER_TYPES`（`L363`）、
  模块级纯函数 `_sl_order_verdict`（`L373`，归 G5）。
- `__init__` 新增状态：`_poll_fail_streak` / `_poll_first_fail_time` / `_poll_last_success_time` /
  `_poll_degraded_batches` / `_poll_alert_*` / `_unresolved_intent_batches`（+17）。
- `_alert_poll_degraded()`（`L1040`，+47）：critical 告警 + 900s 补充提醒。
- hunk16（+12）：轮询循环内**再次**按批初始化跟踪变量（注释：「测试 fake 可能不跑 `__init__`」），
  防止 fake 场景读到未初始化属性。
- 逐批失败跟踪（hunk18，+23，注释明示「恢复须在业务处理后，不在此宣称」）；
  陈旧判定**逐批**计算（阻断3：用本批次首次失败/成功时间，初值 0 时不参与计算，
  避免把陈旧直接算成 0）；触发条件
  `_streak >= POLL_FAIL_ALERT_ROUNDS or _stale >= POLL_STALE_ALERT_SECONDS`（`L8777`）。
- **覆盖**：`test_poll_degradation.py` 的 `test_single_batch_persistent_failure_alerts_and_marks_degraded`、
  `test_multi_batch_one_success_one_failure`、`test_block3_per_batch_stale_not_masked_by_other_batch`、
  `test_block4_alert_retry_on_notify_false`、`test_block8_notify_none_is_not_delivered`、
  `test_notification_exception_kills_nothing`；MPR `check_t2_persistent_failure_alerts`。

### G2 入口闸（拒新信号）——R1/R2 资金安全核心
- `L6390` 起（+19）：`execute_signal` 内、**所有前置门闸之后、任何副作用（骨架/`create_order`）之前**
  拒绝处于降级批次的新信号（Fail-Closed）。
- +11：每层 ENTRY 创建的**最终边界**复核暂停状态（骨架已落、尚未 `create_order` 时另一线程才置降级）。
- +1：`_attempted_layers.add(idx)` 口径注释。
- **覆盖**：`test_new_signal_blocked_when_degraded`、`test_block2_degrade_after_check_blocks_entry_create`、
  `test_block6_partial_create_not_clean_reject`、`test_block10_write_false_no_clean_reject`、
  `test_block26_refused_takeover_still_blocks_new_entry`、`test_block33_release_then_exit_blocks_entry`；
  另有 `tests_archive/test_b2_crashsafe_entry.py` 的 fake 属性初始化保证闸门读值不被 MagicMock 恒真。

### G3 启动重证（重启不等于恢复）
- `L3651` 起（+174/−1）：降级集合只在内存、重启即丢 → **不得把"重启后集合为空"当业务已恢复**；
  启动时按交易所实况重证（订单、持仓、SL 锚点与类型/方向/覆盖量逐项）。
- `_ready = False` + `_not_ready_reason` 落点 `L3804-L3806`：重证未通过 → **显式 Fail-Closed 置位**，
  打印「启动重证未通过，READY 保持 False」并发资金安全通知（`_ready`/`_not_ready_reason`
  两属性基线已存在，本次仅**新增这处赋值点**；`_not_ready_reason` 按其自身注释「仅诊断展示，
  永不参与安全判断」）。
- **覆盖**：`test_block7_restart_reverify_blocks_new_entry`、`test_block12_reverify_inconsistent_success`、
  `test_block13_reverify_position_without_sl`、`test_block24_restart_rejects_unresolved_skeleton`、
  `test_block29_reverify_uses_post_recovery_ledger`、`test_block31_unparsable_ledger_value`。
- **未验证（登记）**：重证对"交易所持续不可达"场景**未端到端**；`test_recover_semantics` 为夹具驱动。

### G4 接管与代次所有权（S6 后半）
- `_monitor_takeover_handoff()`（`L8383`，+167）：**解除接管闸门的唯一运行期入口**，仅由首轮完整业务轮询调用。
- **首轮接管（handoff）触发前提**（hunk31，+31/−3）：`phase=='starting'` **且** `_poll_state_saved`
  （本轮回读确认落盘，`save_batch_state(...) is True`）**且** `not _poll_orders_unresolved`
  **且** `current_actual_position is not None` **且** `_poll_protection_confirmed`；
  再重读最新账本（`load_all_states()`）核 `_position_safe` + `_batch_durable`
  （`entry_orders ⊆ 已落盘 ID`、`_registry_has_unresolved_entries` 为假），**两者都成立才调 handoff**。
  注释明示：仅凭「线程已注册」或「open order 读成功」**不足以**解除接管闸门。
- **清除证据墓碑**（hunk5，+12）：批次状态里写入 `clear_evidence` 紧凑副本
  （`scope`/`position_zero`/`state_ids_resolved`/`exchange_scan`）——
  monitor **只有在 `_verify_clear_proof` 同时证明账本闭合与交易所收敛时**，才可把该墓碑
  当作正常终止证据。
- 代次所有权先于**任何**共享状态写入（hunk17，+6，注释：旧代次不能覆盖新代次的健康进度序列）；
  `_active_monitor_generations` 三处初始化（`L634`/`L6800`/`L8575`）。
- 旧代次在**整个退出收尾**失去副作用权限（hunk34，+11/−1）；takeover 最终权属确认（hunk33，+20/−4）。
- **覆盖**：MPR `check_s6_old_generation_exit_is_ignored`、`check_s6_old_generation_exit_runs_no_cleanup_side_effects`、
  `check_s6_exception_side_effects_are_generation_gated`、`check_s6_monitor_error_write_is_atomic`；
  `test_block11_unknown_create_has_takeover`、`test_block15_unknown_create_real_monitor_takeover`、
  `test_block19_reconcile_drives_takeover`、`test_block20_takeover_params_layer_consistent`、
  `test_block22_takeover_refused_when_persist_unconfirmed`、`test_block34_takeover_alive_before_first_round_keeps_gate`。
- **未验证（登记）**：`S6j` 单进程受控时序、`S6j-3` 为 AST 断言、`S6k` 三返回值为桩注入。

### G5 S6 判据统一（类型白名单 / NaN / closePosition）
- `_sl_order_verdict`（`L373`，模块级纯函数，**夹具无法以 `self.<attr>` 替换**）：
  ① `info.type` ∈ 白名单；② side；③ Hedge 下 `positionSide`（缺失/BOTH → UNKNOWN 拒绝）；
  ④ 覆盖量必须**有限正数**（NaN/inf 显式排除）+ 相对/绝对容差；`amount=None` 仅当 `info.closePosition=='true'`。
- 标记位赋值点（**已按基线 `8c96867` 实测对比**，基线均为 0）：
  - `_poll_sl_validated = True` **0 → 3 处**：`L9712`、`L10227`、`L10397`
  - `_poll_orders_unresolved = True` **0 → 6 处**：`L8914`、`L8922`、`L9626`、`L9672`、`L9866`、`L9868`
  - `tp_status = None` **0 → 1 处**
  - 另新增「回查成功但非终态」「单订单回查非终态」两类分支（hunk 20、22）
- 运行期与启动重证**共用同一实现**，杜绝口径漂移。
- **覆盖**：`test_block16_sl_vanished_on_exchange`、`test_block17_sl_direction_and_coverage`、
  `test_block25_hedge_sl_variants`、`test_block27_non_stop_and_missing_pside`；
  MPR `check_s6_wrong_stop_type_blocks_takeover`、`check_s6_amount_none_needs_close_position`、
  `check_s6_normal_terminal_requires_durable_proof`；`test_sg3_p1` 已补原始 `info.type`。
- **未验证（登记）**：`test_sg3_p1` 仍用**手工夹具** → **不得称实际脱载荷校准**；`S6f-4` 未断言补挂带数量真止损。

### G6 恢复判定与 Fail-Closed（含 B34/B35）
- `L10017`（+14）：恢复判定放在**本轮必要保护处理与确认之后**（不在订单识别循环紧后方），
  并明确区分「订单数据可读」与「保护已确认」两个判据（注释 `L10030`）。
- `L10677`（**hunk30**，+48）：恢复判定**分开判据**、循环层级执行（在本轮止损/止盈维护**之后**；
  未成交批次不进入维护分支，故不能放在分支内），判据为
  ① 订单数据可读（`not _poll_orders_unresolved`）② 保护已确认（`pending_sl_orders` 为空
  **不能**证明交易所 SL 有效 → 额外要求 `current_sl_id` 存在且 `_poll_sl_validated`）
  ③ **持仓已知**。
- `L10699`（同属 **hunk30**，**B35**）：新增 `_poll_position_known = (current_actual_position is not None)`
  并加进主 if；该 hunk 落成**三个互斥分支**：
  - 全部满足 → 重置 streak、丢弃降级标记、必要时打印「✅ 全部批次监控恢复」；
  - `not _poll_position_known` → 打印「⏸️ 订单已可读但持仓 UNKNOWN（查询失败 ≠ 零仓）→ 保持暂停新增风险」
    —— **不重置 streak**（与 S6 首轮接管同款 Fail-Closed，不靠 S6 兜底）；
  - 保护未确认 → 打印「⏸️ 订单已可读但保护未确认（SL 锚点缺失/维护失败）→ 保持暂停新增风险」。
- **覆盖**：`test_block23_unresolved_survives_poll_recovery`、`test_block28_unresolved_gate_release_path`、
  `test_complete_recovery_after_failure`、`test_block5_unfilled_entry_does_not_block_recovery`、
  **`test_block35_position_unknown_keeps_degraded_gate`（B35a–e，RED→GREEN→变异）**、
  **`test_block34_takeover_alive_before_first_round_keeps_gate`（B34 两态）**。
- **运营影响（如实标注）**：持仓查询持续失败 → 该批次闸门保持、新信号被拒，需人工核实或重启重证
  —— Fail-Closed 的预期代价，不是缺陷，但**上线后会成为运维动作**。

### G7 落盘确认（收编成功 ≠ 已落盘）
- `_persist_states()`（`L2689`）返回值纳入判定（+5/−2）；收编路径落盘未确认时**不得返回 ok=True**
  （+6/−1），否则调用方会解除暂停并认为已收编。
- **覆盖**：`test_block22_takeover_refused_when_persist_unconfirmed`、`test_block10_write_false_no_clean_reject`、
  MPR `check_s6_alert_and_write_confirm_use_strict_success`。

### G8 CLEAN_REJECT 的证明口径（PROVEN-CLEAN）与其余小改
- **hunk11（+256/−8，本文件第三大块）** —— CLEAN_REJECT 的**全部前提**：
  - 本分支的成立前提是「**所有层**均无交易所副作用**且状态已落盘**」；暂停（`break`）会跳过
    后续层 → 先把**明确未尝试创建**的层可靠收敛为 `ABSENT`，再校验：只有全部层都无未决
    意图时才返回 `CLEAN_REJECT`，否则**保持 Fail-Closed（返回 None，让批次进入监控而非伪装干净）**。
  - 收敛必须用**能确认落盘**的 `_update_registry_checked`（`True ⟺ _persist_states True`）；
    注释明示 `_update_registry` 的返回值是 **fail_count，不代表写盘成功**。
  - `_attempted_layers` = 本轮**实际调用过 `create_order`** 的层号（**含失败尝试**——失败也可能
    已在交易所产生副作用须保留证据）→ **任何已尝试层 → 不得 CLEAN_REJECT**。
  - 第五轮补全：① 读到**预期骨架** ② 重读后仍无 `entry_orders` 且 registry 无未决 ENTRY 意图
    （读改写在 `_state_lock` 内原子完成）③ 停用**写盘已确认**（见 G7）——三者缺一即不收敛。
- **hunk10（+30/−6）**：B2-5 `-2021` 确定拒绝 = 证明无交易所副作用 → 该层 `ABSENT`、不残留
  `PENDING_CREATE`，故**不算 attempted**。
- **hunk32（+83/−26）**：monitor 异常路径的 `exit_reason` 归类
  （`f"monitor exception: ..."` 等），供上层分类处理。
- **hunk35（±1）**：尾部 print 一行（无逻辑）。
- **覆盖**：`test_block18_clean_reject_no_fake_exit`、`test_block10_write_false_no_clean_reject`、
  `test_block30_thread_exits_immediately`、`test_b2_create_gate.py`（骨架/clean-reject 门）。

---

## 3. 四项横向专项核对（本次终审新增）

| 专项 | 方法 | 结果 |
|---|---|---|
| **A. 下单/资金参数是否被触碰** | 抽取 diff 中所有 `^\+self.<attr> =` 与 `^\-self.<attr> =`，并统计下单关键字 | **未触碰**：新增赋值共 **11 个属性** —— 7 个 `_poll_*` 降级状态 + `_unresolved_intent_batches` + `_active_monitor_generations`（代次所有权）+ SG1 READY 门控在**启动重证失败路径**的两处新落点（`_ready`/`_not_ready_reason`，`L3804-3806`；属性本身基线已存在）；**被删除的 `self.<attr> =` 为 0 个**；`leverage` / `qty` / `quantity` / `price=` **+0 −0**；`create_order` 仅出现在 3 行的文字里 —— 两行纯注释 + 一行 `_attempted_layers = set()` 的行尾注释，**无新增调用、无参数改动**；`stopPrice`/`amount=` 的 11 行新增全部在**判据与注释**侧（`_sl_order_verdict`、重证文案），不在下单侧 |
| **B. 新增的运行参数（需运维确认取值）** | 常量块走查 | `POLL_FAIL_ALERT_ROUNDS=3`（连续 3 轮判降级）、`POLL_STALE_ALERT_SECONDS=120.0`（陈旧 120s 判降级）、`POLL_ALERT_RETRY_INTERVAL=900`（补充提醒最小间隔 15 分钟）、`_STOP_ORDER_TYPES` 6 类白名单。**均为类级常量，不在配置文件中**，改值需改码 |
| **C. 门禁基线变化方向** | `run_test_gate.py` 全量 diff（8+/17−） | **只收紧，无放宽**：`EXPECTED[p3]={1}→{0}`、`BASELINE_FAIL={p3}→∅`、`BASELINE_GREEN=(3,9)→(9,9)`、失败身份基线 6 项→∅（夹具修复后 9/9）。任何一项转失败即 `BASELINE-DRIFT`（fatal） |
| **D. 覆盖与执行证据** | 用例清点 | `test_poll_degradation` **41 用例**（注册 41 行、`GREEN: n/41`）；MPR 新增 **12 `check_*`**；门禁实跑 pytest 125 passed + 脚本 56 项全 PASS（`--strict` rc=0） |

---

## 4. 未验证 / 待裁清单（合并决策必须知悉）

**A. 已登记的未验证实现项（未扩 P2，仅登记）**
1. `S6k` 三返回值为桩注入；2. `S6j` 单进程受控时序；3. `S6j-3` 为 AST 断言；
4. `S6f-4` 未断言补挂带数量真止损；5. `test_sg3_p1` 仍用手工夹具（**不得称实际脱载荷校准**）；
6. 交易所持续不可达时对账**未端到端**；7. P1-3 两次 critical 失败后的持久提醒**未证实**。

**B. 本轮新增登记待裁**
8. **门禁哨兵空真**：`run_test_gate.py` 的 `ROOT` = 候选工作树，三个哨兵对象在工作树不存在
   → 其"未发现改动"本轮无比对对象；已由**生产目录手工前后 sha 比对**替代（未变、测试标记行 0/0/0）。
9. **`run_test_gate.py:450` 过期字面量**「50/50」（实为 56 项），判定逻辑不受影响；
   该文件在 12 文件内，修则三份补丁重建 → **未修**。
10. **重复进程观察**：生产重启后仍为 2×`watchdog.py` + 2×`bot_runner.py`（venv 与 python311 各一），
   停机前亦然 → **既存现象**；第二个 `bot_runner` 是否被具名互斥体挡住**未核实**（只读观察，未展开）。

**C. 层级边界**
11. 本文件是**自审走查**，不是独立复审；12 文件逐项**独立**发布级审查仍以 ChatGPT 裁决为准。
12. **更正④**：PR #1 现 66 文件（54 材料/证据文件），**合并即入生产分支**
   → 发布只应合入本 12 文件（另建仅含 12 文件的发布 PR 或按文件挑选）。
13. 分层口径不混报：**报告证据**（含门禁 rc=0、B34/B35 两态、发布差异三项验证）
    ｜**实现终审**未达（B35 已获复审通过；本 12 文件自审完成、**独立终审未获裁决**）
    ｜**发布就绪**未达（未获合并/部署/重启批准）。

---

## 5. 复现命令

```bash
git diff --numstat 8c96867 HEAD -- <12 个文件>          # 规模
git diff -U2   8c96867 HEAD -- trader_260725.py          # 35 hunk 走查
git diff       8c96867 HEAD -- run_test_gate.py          # 基线只收紧
git diff --numstat 8c96867 0ec6b4d                       # = 发布补丁（sha c0a9d9d3…）
python run_test_gate.py --strict                         # 停机窗口内 → rc=0
```
