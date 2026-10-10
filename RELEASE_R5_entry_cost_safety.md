# R5 交易引擎资金安全修复 —— 发布说明（2026-10-09）

## 对象
- **修订候选引擎**：`trader_260725.py`，SHA256 `23FB23B445B3AC329CE633C67C710C7902D7ECF7C6C509076141A0C5AE11F9B8`（1,148,516 B，Windows/CRLF）；Git LF blob `d21d23a69c0035c67f39a3f31673f9d1e42cbe1c`。
- 原候选 `969B8138…8DD4` 经独立探针复审被阻断；本次三个定点修复组及RED/GREEN记录见 `REVIEW_FIX_U1_U5.md`，不能沿用原候选测试结果批准修订候选。
- **基线**：`dd38cab`（生产当前提交）。
- **PR 目标分支**：`backtest/markprice-4h-fetch`（承载生产版本）。

## 范围（本批）
- R1–R5 复审闭环、F2/F3 成交成本与证据、per-read 三态读取、`clear_batch_state` 锁内核验、门禁七件、预算三层口径。
- `/be`、`/tp` 在**撤单前**拒绝未知成交成本；save 侧**已成交层缺价 fail-closed**（不把伪造 0 成本落盘）。

## 验证（已记录）
- **修订23FB候选**：同组回归旧候选RED（13方法、39失败、0执行错误、rc=1），新候选13/13 GREEN、rc=0；第三轮干净导出副本完整门禁 **64/64脚本PASS、192 passed + 69 subtests、5 warnings、rc=0**。第二轮曾63/64、rc=1（监控失败未归因），不抹去该失败；轮次及ROOT哨兵范围详见 `REVIEW_FIX_U1_U5.md`。
- 下列为**原969B候选历史验证**，不等同修订候选验证。
- `test_monitor_resume.py`：**44/44**；battery 28 套 + `test_converge_post_cancel` 124/124 + `test_r1_entry_before_fact_check` 49/49 全 rc=0。
- 停机窗口全量门禁 `gate_r5ext10_stopwindow_20261009.out`：**rc=0 = ALL-GREEN**（69/69 PASS；`test_orphan_guard.py` rc=0）。

## 部署约定
- **仅替换 `trader_260725.py`**；`bot_runner.py` 与 TG v3 保持现状。
- **不纳入**：`bot_runner.py`、`tmp_*` 探针、日志/ZIP、`deploy_tgdiag.py`。

## 未决（发布前硬门）
- 已提供 **83 hunk 核对索引**（`PROD_TO_CANDIDATE_HUNK_INDEX.md`）：缩进型 0 / 逻辑型 83；重排等价 827、逻辑 +4251/−1444。
- 该索引供逐段人工确认「累计改动全部对应已审修复、无未审代码」；**确认前不合并、不部署**。
- 原候选逐块阅读记录 **83/83**，见 `PROD_TO_CANDIDATE_HUNK_REVIEW.md`；后续独立复审将U2、U3跨锁补证、U5裁为阻断。修订增量24块见 `REVIEW_FIX_U1_U5.md`，U3载荷CAS、U7与第二轮未归因监控失败仍交独立复审。硬门未关闭，不表示通过或发布授权。
