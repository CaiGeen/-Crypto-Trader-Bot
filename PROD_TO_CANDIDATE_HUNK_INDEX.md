# 生产引擎 → 候选：hunk 核对索引

## 人工核对进度

原候选 `3077a80` / `969B8138…8DD4` 的83/83 个 hunk 已逐块阅读，详见 `PROD_TO_CANDIDATE_HUNK_REVIEW.md`。**逐块阅读完成不等于全部通过**：独立探针将U2、U3跨锁补证、U5裁为阻断，定点修订见 `REVIEW_FIX_U1_U5.md`。此表保留原候选历史索引，不代表后续修订候选的完整差异或批准；发布硬门未关闭，不合并、不部署。

生产 `trader_260725.py`(`98DA5139`) → 候选(`969B8138`)；共 **83** hunk：**缩进型 0 / 逻辑型 83**。

合计：重排等价行 **827**、逻辑新增 **4251**、逻辑删除 **1444**。

> 判据：每 hunk 内把「去掉行首空白后内容相同」的删/增行配对为**重排（缩进）**；其余为**逻辑增/删**。**不按注释判定。**
> 「上下文」= 候选侧最近 def/class；「首个新增行」仅定位提示。

| # | 生产行 | 候选行 | 上下文 | 类别 | -行 | +行 | 重排 | 逻辑+ | 逻辑- | 首个新增行(截断) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 99 | 99 | def _tombstone_entry_valid | **逻辑** | 0 | 29 | 0 | 29 | 0 | `# 🔥 事故修复 2026-10-06（转审第三轮）：仅在 **converge 撤单路径**局部收窄的` |
| 2 | 1361 | 1390 | def _fee_float | **逻辑** | 0 | 542 | 0 | 542 | 0 | `# ==================== F2/F3：真实成交价入账证据链 ====================` |
| 3 | 1375 | 1946 | def _resolve_order_fees | **逻辑** | 1 | 14 | 0 | 14 | 1 | `降级取值顺序：调用方估算 → 观测到的实际手续费 → None（unknown）。` |
| 4 | 1399 | 1983 | def _deg | **逻辑** | 1 | 3 | 0 | 3 | 1 | `# 🔥 F2/F3-R3：显式 retries=2 —— 费用解析必须有界` |
| 5 | 1410 | 1996 | def _deg | **逻辑** | 1 | 1 | 0 | 1 | 1 | `{'orderId': real_oid}, retries=2)` |
| 6 | 1446 | 2032 | def _deg | **逻辑** | 2 | 4 | 0 | 4 | 2 | `_o = (self._safe_api_call(self.exchange.fetch_order, real_oid,` |
| 7 | 1533 | 2121 | def _begin_qty_conflict_txn | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest, _rc_2152, _ = self._load_all_states_ex()` |
| 8 | 1561 | 2149 | def _begin_qty_conflict_txn | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(latest, read_corrupt=_rc_2152):` |
| 9 | 1597 | 2185 | def _finalize_qty_conflict | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest2, _rc_2196, _ = self._load_all_states_ex()` |
| 10 | 1605 | 2193 | def _finalize_qty_conflict | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(latest2, read_corrupt=_rc_2196):` |
| 11 | 2388 | 2976 | def _effective_429_cooldown | **逻辑** | 1 | 2 | 0 | 2 | 1 | `def _safe_api_call(self, func, *args, retries=5, delay=2, auth_p` |
| 12 | 2447 | 3036 | def _safe_api_call | **逻辑** | 0 | 8 | 0 | 8 | 0 | `# 🔥 事故修复 2026-10-06（转审第三轮两口径之一）：**本调用点局部**的` |
| 13 | 2619 | 3216 | def _load_all_states_ex | **逻辑** | 2 | 5 | 0 | 5 | 2 | `共享字段仅供未传 per-read 参数的**单元直调/历史桩**回退；**写盘闸门` |
| 14 | 2686 | 3286 | def load_all_states | **逻辑** | 1 | 1 | 0 | 1 | 1 | `def _persist_states(self, all_states: dict, read_corrupt: bool =` |
| 15 | 2703 | 3303 | def _persist_states | **逻辑** | 1 | 8 | 0 | 8 | 1 | `# 外部复审第 6 轮：闸门只信**调用方本次读取**的三元组（read_corrupt）；` |
| 16 | 2739 | 3346 | def _persist_states | **逻辑** | 1 | 2 | 0 | 2 | 1 | `def save_batch_state(self, symbol: str, batch_id: str, batch_dat` |
| 17 | 2750 | 3358 | def save_batch_state | **逻辑** | 1 | 20 | 0 | 20 | 1 | `merge 后字段集合 = 磁盘 ∪ 快照（快照新增字段正常写入，磁盘独有字段补回）。` |
| 18 | 2769 | 3396 | def _price7_ok | **逻辑** | 1 | 1 | 0 | 1 | 1 | `all_states, _rc_3433, _ = self._load_all_states_ex()` |
| 19 | 2782 | 3409 | def _price7_ok | **逻辑** | 8 | 131 | 7 | 124 | 1 | `# 外部复审第1项（第5轮）：凡快照带 entry_orders 键，默认在` |
| 20 | 2887 | 3637 | def clear_batch_state | **逻辑** | 10 | 50 | 0 | 50 | 10 | `all_states, _rc_3681, _rc_3681_detail = self._load_all_states_ex` |
| 21 | 2969 | 3759 | def clear_batch_state | **逻辑** | 1 | 1 | 0 | 1 | 1 | `_ledger_ok = self._persist_states(all_states, read_corrupt=_rc_3` |
| 22 | 2981 | 3771 | def clear_batch_state | **逻辑** | 1 | 10 | 0 | 10 | 1 | `if _rc_3681` |
| 23 | 3838 | 4637 | def update_batch_tp | **逻辑** | 0 | 33 | 0 | 33 | 0 | `# 复审（外审 7 同类路径，阻塞项）：`/tp` 与 `/be` 同属「成本依赖 + 撤旧保护」` |
| 24 | 4312 | 5144 | def set_breakeven_sl | **逻辑** | 0 | 33 | 0 | 33 | 0 | `# 🔥 F2/F3：数量已核实但成本待确认 → **暂停依赖成本的保本**。` |
| 25 | 5156 | 6021 | def _execute_partial_close | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest, _rc_5904, _ = self._load_all_states_ex()` |
| 26 | 5172 | 6037 | def _execute_partial_close | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(latest, read_corrupt=_rc_5904):` |
| 27 | 5254 | 6119 | def _resize_protection_after_partial | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest_a, _rc_5995, _ = self._load_all_states_ex()` |
| 28 | 5263 | 6128 | def _resize_protection_after_partial | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(latest_a, read_corrupt=_rc_5995):` |
| 29 | 5359 | 6224 | def _resize_protection_after_partial | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest2, _rc_6104, _ = self._load_all_states_ex()` |
| 30 | 5372 | 6237 | def _resize_protection_after_partial | **逻辑** | 2 | 2 | 0 | 2 | 2 | `if not self._persist_states(latest2, read_corrupt=_rc_6104):` |
| 31 | 5395 | 6260 | def _resize_protection_after_partial | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(latest, read_corrupt=_rc_6127):` |
| 32 | 5476 | 6341 | def _rearm_tp_registry_for_closecancel | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest, _rc_6240, _ = self._load_all_states_ex()` |
| 33 | 5508 | 6373 | def _rearm_tp_registry_for_closecancel | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(latest, read_corrupt=_rc_6240):` |
| 34 | 5522 | 6387 | def _commit_closecancel_attribution | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest, _rc_6274, _ = self._load_all_states_ex()` |
| 35 | 5542 | 6407 | def _commit_closecancel_attribution | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(latest, read_corrupt=_rc_6274):` |
| 36 | 5619 | 6484 | def _route_zero_position_limit_close | **逻辑** | 1 | 153 | 0 | 153 | 1 | `def _monitor_resume_class(self, symbol, batch_id):` |
| 37 | 5640 | 6657 | def _finally_cleanup_decision | **逻辑** | 0 | 23 | 0 | 23 | 0 | `# 🔥 事故修复 3（ChatGPT 裁决「超限四条」之三：禁止自动清账）：` |
| 38 | 5685 | 6725 | def _mark_limit_cancel_manual_review | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest, _rc_6614, _ = self._load_all_states_ex()` |
| 39 | 5707 | 6747 | def _mark_limit_cancel_manual_review | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(latest, read_corrupt=_rc_6614):` |
| 40 | 5856 | 6896 | def _finalize_limit_full_fill | **逻辑** | 1 | 7 | 0 | 7 | 1 | `# 🔥 F2/F3：结算前成本门槛——数量已核实但**入场成本**待补证 → 保持 phase=2` |
| 41 | 5872 | 6918 | def _finalize_limit_full_fill | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(latest, read_corrupt=_rc_6785):` |
| 42 | 5971 | 7017 | def _release_resize_inflight | **逻辑** | 1 | 2 | 0 | 2 | 1 | `def _monitor_lifecycle_check(self, latest_all, latest_b_data,` |
| 43 | 5979 | 7026 | def _monitor_lifecycle_check | **逻辑** | 3 | 9 | 0 | 9 | 3 | `'unknown' 账本损坏（D-009：本次读取返回 {} 且 per-read 三元组为损坏）` |
| 44 | 5999 | 7052 | def _claim_settlement_reported | **逻辑** | 2 | 2 | 0 | 2 | 2 | `latest_all, _rc_6926, _ = self._load_all_states_ex()` |
| 45 | 6795 | 7848 | def execute_signal | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest, _rc_7728, _ = self._load_all_states_ex()` |
| 46 | 6808 | 7861 | def execute_signal | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if self._persist_states(latest, read_corrupt=_rc_7728) is not Tr` |
| 47 | 7453 | 8506 | def _commit_protection_with_g3 | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if self._persist_states(all_states, read_corrupt=_ledger_corrupt` |
| 48 | 7650 | 8703 | def _update_registry_locked | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest_all, _rc_8608, _ = self._load_all_states_ex()` |
| 49 | 7688 | 8741 | def _update_registry_locked | **逻辑** | 1 | 1 | 0 | 1 | 1 | `persisted_ok = self._persist_states(latest_all, read_corrupt=_rc` |
| 50 | 7757 | 8810 | def _commit_registry_txn | **逻辑** | 1 | 1 | 0 | 1 | 1 | `latest_all, _rc_8703, _ = self._load_all_states_ex()` |
| 51 | 7783 | 8836 | def _commit_registry_txn | **逻辑** | 1 | 1 | 0 | 1 | 1 | `return self._persist_states(latest_all, read_corrupt=_rc_8703) i` |
| 52 | 8611 | 9664 | def _monitor_takeover_handoff | **逻辑** | 5 | 8 | 0 | 8 | 5 | `"""仅认可 clear proof 派生的 durable 墓碑为安全终结证据。` |
| 53 | 8762 | 9818 | def _run_monitor_takeover | **逻辑** | 0 | 57 | 0 | 57 | 0 | `def _safe_write_progress(self, instance_id, batch_id, symbol) ->` |
| 54 | 8856 | 9969 | def _is_current_monitor_generation | **逻辑** | 0 | 7 | 0 | 7 | 0 | `elif _conf_reason == 'cost_pending_settling':` |
| 55 | 8930 | 10050 | def _is_current_monitor_generation | **逻辑** | 671 | 2264 | 654 | 1610 | 17 | `# 🔥 R4（ChatGPT 执行版）：**单一外层 try/finally** 替换原「循环外收尾块` |
| 56 | 9723 | 12436 | def _is_current_monitor_generation | **逻辑** | 1415 | 272 | 139 | 133 | 1276 | `'sl_failed_layers': sl_failed_layers,` |
| 57 | 11202 | 12772 | def _is_current_monitor_generation | **逻辑** | 0 | 5 | 0 | 5 | 0 | `elif _fin_decision == 'skip' and _fin_snap \` |
| 58 | 11226 | 12801 | def _is_current_monitor_generation | **逻辑** | 0 | 36 | 0 | 36 | 0 | `# ==============================================================` |
| 59 | 11249 | 12860 | def _is_current_monitor_generation | **逻辑** | 1 | 5 | 0 | 5 | 1 | `if _fin_exhausted_no_clear:` |
| 60 | 11301 | 12916 | def _is_current_monitor_generation | **逻辑** | 1 | 4 | 0 | 4 | 1 | `if _fin_exhausted_no_clear:` |
| 61 | 11321 | 12939 | def _is_current_monitor_generation | **逻辑** | 1 | 4 | 0 | 4 | 1 | `if _fin_exhausted_no_clear:` |
| 62 | 11969 | 13590 | def _begin_close_request_if_active | **逻辑** | 1 | 1 | 0 | 1 | 1 | `all_states, _rc_13505, _ = self._load_all_states_ex()  # 锁内重读，禁旧` |
| 63 | 12017 | 13638 | def _begin_close_request_if_active | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(all_states, read_corrupt=_rc_13505):` |
| 64 | 12125 | 13746 | def _finite_zero_dv | **逻辑** | 5 | 65 | 0 | 65 | 5 | `# 🔥 F2/F3（数量证据门）：存在「数量未知 / 不匹配」的层 → Fail-Closed。` |
| 65 | 12213 | 13894 | def _rollback_close_request_if_current | **逻辑** | 1 | 1 | 0 | 1 | 1 | `all_states, _rc_13780, _ = self._load_all_states_ex()  # 硬约束：锁内重` |
| 66 | 12232 | 13913 | def _rollback_close_request_if_current | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(all_states, read_corrupt=_rc_13780):` |
| 67 | 12258 | 13939 | def _set_close_reason_if_current | **逻辑** | 1 | 1 | 0 | 1 | 1 | `all_states, _rc_13828, _ = self._load_all_states_ex()  # 锁内重读，禁旧` |
| 68 | 12280 | 13961 | def _set_close_reason_if_current | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(all_states, read_corrupt=_rc_13828):` |
| 69 | 12563 | 14244 | def _survey_same_side_batches | **逻辑** | 1 | 6 | 0 | 6 | 1 | `def _topology_ok(amounts, details, n, cost_ok=frozenset()):` |
| 70 | 12581 | 14267 | def _finite_zero | **逻辑** | 1 | 53 | 0 | 53 | 1 | `def _prefix_ok(i, v):` |
| 71 | 12597 | 14335 | def _fp | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not _topology_ok(_ta, _fd, _n, _cost_ok_of(b, _n)):` |
| 72 | 12970 | 14708 | def _commit_limit_close_order_if_current | **逻辑** | 1 | 1 | 0 | 1 | 1 | `all_states, _rc_14593, _ = self._load_all_states_ex()` |
| 73 | 12988 | 14726 | def _commit_limit_close_order_if_current | **逻辑** | 1 | 1 | 0 | 1 | 1 | `if not self._persist_states(all_states, read_corrupt=_rc_14593):` |
| 74 | 13426 | 15164 | def close_position_market | **逻辑** | 2 | 140 | 1 | 139 | 1 | `actual_exit_fee = actual_price * confirmed_filled_amount * TAKER` |
| 75 | 13449 | 15325 | def _defer_exit_fee_payload | **逻辑** | 3 | 0 | 0 | 0 | 3 | `` |
| 76 | 13627 | 15500 | def _converge_cancel_order | **逻辑** | 1 | 2 | 0 | 2 | 1 | `params={'stop': True},` |
| 77 | 13662 | 15536 | def _verify_clear_proof | **逻辑** | 0 | 785 | 0 | 785 | 0 | `def _batch_position_contribution(self, symbol, batch_id, b_data,` |
| 78 | 13673 | 16332 | def _converge_batch_orders_before_clear | **逻辑** | 0 | 5 | 0 | 5 | 0 | `**R1 顺序（2026-10-07 执行版）**：确认本批次归属 → 撤本批次入场单` |
| 79 | 13710 | 16374 | def _converge_batch_orders_before_clear | **逻辑** | 52 | 20 | 0 | 20 | 52 | `# ② D-B1 贡献扣减核验 —— **唯一口径实现见 _batch_position_contribution**，` |
| 80 | 13784 | 16416 | def _converge_batch_orders_before_clear | **逻辑** | 30 | 129 | 23 | 106 | 7 | `_entry_ids = {str(_x) for _x in (b_data.get('entry_orders') or [` |
| 81 | 13827 | 16558 | def _match_intent | **逻辑** | 0 | 3 | 0 | 3 | 0 | `# R1：该入场单必须进撤后成交证据集合 —— 否则复核看不到它的成交` |
| 82 | 13834 | 16568 | def _match_intent | **逻辑** | 3 | 37 | 3 | 34 | 0 | `# R1c（ChatGPT 复审 2026-10-08 漏项②）：intent 匹配确认的入场` |
| 83 | 13884 | 16652 | def _match_intent | **逻辑** | 3 | 42 | 0 | 42 | 3 | `# ⑥ 产出 proof —— **事实来源在生成端**：position_zero 由 ③' 撤后复核给出，` |
