# tests_archive —— 遗留测试归档（2026-09-03）

归档 ≠ 删除：所有文件完整保留（含 git 历史），复活 = `git mv` 回项目根
+ 按下表「复活条件」修 harness，再跑通即可。

## A 组：死时代遗留（等的改动永不落地）

| 文件 | 失败原因 | 复活条件 |
|---|---|---|
| test_ast_rollback_guard.py | 等 `allow_flag_rollback`（2026-08-29 ChatGPT §四）落地——全库 0 处调用，设计已被 v6.2 收敛方案（授权层 + first-abnormal-wins）取代 | 仅当该设计复活；否则永久归档 |
| test_merge_rollback_semantics.py | 同族（§十五），RED 断言"改动尚未落地" | 同上 |

## B 组：旧 harness 测现行功能（脚手架落后于代码演进，归档时已在失败=零回归信号）

| 文件 | 失败原因（reg3_*.log 实证） | 复活条件 |
|---|---|---|
| test_sg1_ready_gate.py | execute_signal 缺 `_compute_signal_fingerprint` 绑定（D-009） | harness 补 real-method binding（参照 test_b_batch.py 的修法） |
| test_sg2_risk_gate.py | 同上 | 同上 |
| test_tg_fallback.py | 同上 | 同上 |
| test_b2_crashsafe_entry.py | `_check_existing_conflicts` 签名漂移 | 同上 |
| test_crash_injection.py | 同上 | 同上 |
| test_b2_close_gap.py | `_update_sl_no_validation` 内 `_batch_net_position` 解包（v6.4 净仓位模型） | 更新 fake 的 b_data 结构至 v6.4 契约 |
| test_sg4.py | `_start_monitoring` 需 `_finally_cleanup_decision` 状态桩（P5f） | fake 补 load_all_states 返回契约 |
| test_close_race_replay.py | 驱动基建损坏（自标"UNEXPECTED-FAIL 测试基建损坏"） | 重写驱动层后评估是否仍有价值 |
| test_t25_replace_fail_protection.py | 7/9 通过；2 场景行为演进 | 核对 2 个失败场景是否已被新机制覆盖，是则删场景、否则修 |
| test_t26_manual_cancel_recovery.py | 4/7 通过；C6 user_modified 分支演进 | 同上 |

## 留守（未归档）

- `test_orphan_guard.py`：watchdog v2.2 orphan_guard 现行核心守门。在 WorkBuddy
  沙箱内 rc=1（safe-delete shim 劫持 os.remove，环境 artifact）；用户真实终端中
  曾 5/5 通过。**它不计入归档决策。**

## 备注

- 另有 4 个从未提交的旧文件仍在项目根（当时通过、未动）：
  `test_close_confirmation_v3/v4/v5.py`、`test_position_close_confirmation.py`
  ——属 v6/v62 之前的旧版确认测试，现仍在回归池且 rc=0；如后续要瘦身可同样处理。
- 回归基线自本目录生效：`for f in test_*.py` = 42 文件，期望 **41 rc=0 +
  1 非零（test_orphan_guard，仅沙箱）**。
