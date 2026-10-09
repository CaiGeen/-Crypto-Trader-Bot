# R5 交易引擎资金安全修复 —— 发布说明（2026-10-09）

## 对象
- **候选引擎**：`trader_260725.py`，SHA256 `969B8138F30FEBB33F712603A54D8A2B3F787270D656BE6D8D296D3DD9DD8DD4`（1,140,842 B）。
- **基线**：`dd38cab`（生产当前提交）。
- **PR 目标分支**：`backtest/markprice-4h-fetch`（承载生产版本）。

## 范围（本批）
- R1–R5 复审闭环、F2/F3 成交成本与证据、per-read 三态读取、`clear_batch_state` 锁内核验、门禁七件、预算三层口径。
- `/be`、`/tp` 在**撤单前**拒绝未知成交成本；save 侧**已成交层缺价 fail-closed**（不把伪造 0 成本落盘）。

## 验证（已记录）
- `test_monitor_resume.py`：**44/44**；battery 28 套 + `test_converge_post_cancel` 124/124 + `test_r1_entry_before_fact_check` 49/49 全 rc=0。
- 停机窗口全量门禁 `gate_r5ext10_stopwindow_20261009.out`：**rc=0 = ALL-GREEN**（69/69 PASS；`test_orphan_guard.py` rc=0）。

## 部署约定
- **仅替换 `trader_260725.py`**；`bot_runner.py` 与 TG v3 保持现状。
- **不纳入**：`bot_runner.py`、`tmp_*` 探针、日志/ZIP、`deploy_tgdiag.py`。

## 未决（发布前硬门）
- 已提供 **83 hunk 核对索引**（`PROD_TO_CANDIDATE_HUNK_INDEX.md`）：缩进型 0 / 逻辑型 83；重排等价 827、逻辑 +4251/−1444。
- 该索引供逐段人工确认「累计改动全部对应已审修复、无未审代码」；**确认前不合并、不部署**。
