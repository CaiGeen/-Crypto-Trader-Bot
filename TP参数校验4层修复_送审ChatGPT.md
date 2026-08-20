# TP 参数校验 4 层修复 — 送审 ChatGPT 复核

> **问题来源**：ChatGPT 终审（2026-08-20）对 C5 事故延伸问题链的 4 层修复裁决
> **源码**：`trader_260725.py`（当前 5317 行，+193 行）
> **提交**：`96b94ed`（8 files changed, 241 insertions(+), 12 deletions(-)）
> **测试**：20 套件 206 场景全绿，ccxt 4.5.68，项目 .venv
> **生成时间**：2026-08-20 21:30 北京时间

---

## 一、问题链与事故背景

C5 实盘事故（8-19 22:53）的延伸链条：

```
错误 TP（<= 现价 / >= 现价）
  → Binance 确定性拒绝 -2021（"Order would immediately trigger"）
  → 补挂 TP 重试循环（无层熔断，SL 有 MAX_SL_FAILS_PER_LAYER）
  → 每次 FAILED 直发 TG（绕过 _gate_alert_notify 3 次上限）
  → 无限 TG 消息 + 无限 API 压力
```

**根因缺口**：开仓前 SL 有 `_validate_stop_losses`（L1923）覆盖，但 TP **从未被任何校验覆盖**——错误 TP 可直接进入开仓流程和补挂流程。

---

## 二、修复设计（4 层）与裁决要点

| 层 | 时机 | 校验规则 | 失败动作 |
|----|------|----------|----------|
| **R1** | 开仓前 | BUY 需 TP > 现价 / SELL 需 TP < 现价 | 阻断整批开仓，不挂任何开仓单 |
| **R2** | 成交后补挂 TP 前 | BUY 需 TP > max(现价, 持仓成本) / SELL 需 TP < min(现价, 持仓成本) | 不打 API + 1 次 critical（60min 去重）+ `tp_param_invalid` 标记，等人工修正 |
| **R3** | 补挂 TP 重试 | `tp_fail_count` 连续确定失败 ≥ 5 → 跳过自动重试（对称 SL） | 熔断短路，成功挂出清零 |
| **去重修复** | FAILED 直发点 | 移除 `_gate_alert_clear` 误调；补挂 TP / 兜底 SL FAILED 接入 `_gate_alert_notify` | 同一 identity+FAILED 最多 3 条 TG |

**ChatGPT 裁决要点（已遵守）**：
1. **不用 `TP > max(所有层入场价)`**——阶梯入场场景（如 TP=70150，第一层=69580，第二层=70100，第三层=70200）中 TP 低于未成交层触发价是**合法**的；该规则会错误禁止合法场景。
2. **-2021 的判定基准是现价而非成本**——币安按"触发价是否会被立即触发"判断，因此 R1 只做方向校验，R2 的现价维度才是对抗 -2021 的关键。
3. **确定性错误不得进入无限重试**——识别 → 告警 → 停止无意义重试 → 稳定等待人工。

---

## 三、实施摘要（源码行号实测 2026-08-20 21:30）

### 3.1 R1：开仓前 TP 方向校验

| 项 | 源码位置 |
|----|----------|
| `_validate_take_profit(signal, current_mark_price)` | L1958 |
| execute_signal 接入（止损校验之后） | L2061-2070 |

```python
# L2064-2070
print("\n🔍 [止盈价合理性校验中...]")
tp_is_valid, tp_msg = self._validate_take_profit(signal, current_mark_price)
if not tp_is_valid:
    print(tp_msg)
    self.send_tg_notification(f"🚨 **挂单被阻断！**\n{tp_msg}", level='critical')
    return None
print(tp_msg)
```

**推导依据**（写入函数 docstring）：条件单触发价必须 > 现价（否则被跳过）→ 成交价 ≥ 触发价 > 现价 → TP ≤ 现价 ⇒ TP 必 < 成交价 ⇒ TAKE_PROFIT_MARKET 触发价 ≤ 现价 ⇒ 币安确定性 -2021。方向错误 = 参数确定错误，**任何重试必失败** → 直接阻断，不创建任何开仓单。

### 3.2 R2：成交后 TP 可行性校验（4 个 helper）

| Helper | 行号 | 职责 |
|--------|------|------|
| `_check_tp_viability(side, tp_price, cost_price, mark_price)` | L656 | 核心判定，返回 (valid, reason) |
| `_mark_tp_param_invalid(symbol, batch_id, reason)` | L680 | 写 `tp_param_invalid` 标记 + critical（3600s 窗口，`_tp_invalid_alerted` 键=batch_id，L130 初始化） |
| `_clear_tp_param_invalid(symbol, batch_id)` | L707 | 校验通过 → 清除标记 |
| `_tp_update_blocked(symbol, batch_id, side, layer, tp_price, cost_price, mark_price=None, max_tp_fails=5)` | L718 | 补挂前综合预检，返回 True=跳过本轮（不打 create API） |

**`_tp_update_blocked` 三条短路逻辑**：
```
a) 熔断短路：tp_fail_count[str(layer)] >= max_tp_fails → 跳过自动重试（终端打印，不发 TG）
b) R2 可行性：BUY 需 TP > max(现价, 成本)；SELL 需 TP < min(现价, 成本)
   → 失败 = 确定性错误：不打 API + 标记 + critical（60min 去重）
c) 已标记且仍不合理 → 静默跳过（不重复告警）；已标记但现合理（用户改价）→ 清标记放行
```

**自愈关键设计**：标记**不短路校验**（只短路"告警与 create"）。每轮风控循环都会重新执行 R2；当用户修正 TP 使其合理 → `_clear_tp_param_invalid` 清标记 → 下一轮自动恢复挂单。第一版实现曾用"标记短路"导致用户改价后永远无法恢复（自愈死锁），冒烟测试发现后已删除该分支。

**3 个接入点**：

| 接入点 | 行号 | 说明 |
|--------|------|------|
| 补挂 TP 条件 | L4293 | `if need_update_tp and not self._tp_update_blocked(...)` —— 单行条件改动，**无缩进迁移** |
| 预生成 TP 段 | L4882-4891 | `_tp_side` 反推 + 预检；**cost 传 0.0**（首层成交瞬间成本≈成交价≈现价，仅校验现价维度已足够对抗 -2021）；`return` 位于 SL 段之后，**不影响 SL 挂单** |
| 补挂成功清零 | L4364-4372 | `tp_fail_count.pop(str(layer))`，对称 SL 语义 |

### 3.3 R3：补挂 TP 层熔断

| 项 | 源码位置 |
|----|----------|
| 熔断上限 `max_tp_fails=5`（参数默认值） | L718 |
| 计数递增（FAILED 分支，持久化到批次状态） | L4394-4406 |
| 成功清零 | L4364-4372 |

对称于现有 SL 熔断 `MAX_SL_FAILS_PER_LAYER = 5`（L3081）。计数随批次状态持久化，重启不丢失；连续 5 次确定失败后跳过自动重试（终端打印说明），成功挂出时清零。

### 3.4 FAILED 告警去重修复

| 项 | 源码位置 |
|----|----------|
| `_assert_create_allowed` FAILED 分支移除 `_gate_alert_clear(identity)` | L2604-2605（`return True, ''` 保留，注释注明 runtime 补丁已移除误清） |
| 补挂 TP FAILED 直发点 → `_gate_alert_notify(tp_identity, 'FAILED', ...)` | L4408 |
| 兜底 SL FAILED 直发点 → `_gate_alert_notify(identity, 'FAILED', ...)`（**保留 level='critical'**） | L4857 |

**缺陷机理**：288c6cf 补丁在 FAILED 分支误调 `_gate_alert_clear`，而 §8 语义明确"FAILED 失败计数不重置"→ 每次重试都获得新的 3 次 TG 额度 → 3 次上限被架空。移除后 FAILED 告警走 `_gate_alert_notify` 统一去重（(identity, reason_cat) 键、3 次上限、HARD_LOCK 静默）。

---

## 四、验收标准 5 条对照

| # | ChatGPT 验收标准 | 实施对照 | 测试证据 |
|---|------------------|----------|----------|
| ① | 错误 TP 不能进入开仓流程 | R1 在 execute_signal 止损校验之后、任何 create 之前拦截；失败 `return None` 零开仓单 | crashsafe/crash_injection 套件全绿（R1 桩驱动）+ sg4 A 组锚点校验 14 处 create 均在 R1 之后 |
| ② | 成交后发现 TP 不合理 → 1 次 critical + 稳定等待人工处理 | R2 不打 API、`_mark_tp_param_invalid` 发 1 次 critical（3600s 去重）、不自动重试、等待用户修正（改价自动恢复） | sg4 H4 + sg3_p1 E：TP=60000 > max(现价100, 成本100) → 恢复链触发 cancel/create=1 |
| ③ | 临时性失败最多 N 次 → 熔断 → critical → 稳定 | R3 `tp_fail_count` 5 次连续确定失败跳过自动重试；FAILED 告警 3 次上限 | tp_fail_count 递增/清零逻辑经代码审查；熔断短路为终端打印（FAILED 告警由 gate 限 3 次） |
| ④ | 同一故障事件最多 3 条 TG，真正恢复后重置 | FAILED 分支全部接入 `_gate_alert_notify` 去重（修复 `_gate_alert_clear` 误清）；成功路径清计数/清标记 | 288c6cf 补丁审计 + 本次修复 diff 复核 |
| ⑤ | 错误状态不得持续制造 CPU/API 压力 | 确定性错误**零 create API**（`_tp_update_blocked` 短路）+ 无无限重试（R3 熔断） | `_tp_update_blocked` 短路路径零 `_safe_api_call(create_order)`（代码审查） |

---

## 五、测试修复（7 文件）与 MagicMock 陷阱

### 5.1 MagicMock 陷阱（两类，回归失败根因）

| 陷阱 | 现象 | 修复 |
|------|------|------|
| ① execute_signal 驱动测试缺 `_validate_take_profit` 桩 | MagicMock 属性自动 mock → `tp_is_valid, tp_msg = ...` 解包抛异常被 except 吞 → 首次 save 前中断 → 13 个 FAIL（crashsafe_entry 2/15） | 补桩 `fake._validate_take_profit = lambda signal, price: (True, '止盈校验通过')` |
| ② 监控驱动测试未绑 `_tp_update_blocked` | MagicMock 返回 truthy mock → `not self._tp_update_blocked(...)` 为 False → 整个 TP 恢复段被跳过 → H4/E 场景 cancel/create=0 | sg4/sg3_p1 `make_fake` 绑定 4 个真实 helper（`_tp_update_blocked` / `_mark` / `_clear` / `_check_tp_viability`）+ `_tp_invalid_alerted={}` |

### 5.2 锚点平移

R1/R2/R3 插入 +193 行后，14 处 create 调用点行号全部重测更新：

| 测试文件 | 锚点集 | 更新后 |
|----------|--------|--------|
| test_sg4.py | A（11 处保护单） | {1289, 1438, 1652, 3495, 3569, 4062, 4215, 4345, 4641, 4783, 4929} |
| test_sg4.py | B（1 处开仓） | {2238} |
| test_sg4.py | C（2 处平仓） | {5193, 5376} |
| test_b2_close_gap.py | GAP_CREATE_LINES | {1289, 1438, 1652, 3495, 3569} |
| test_b2_create_gate.py | GATE_LINES | {4062, 4215, 4345, 4641, 4783, 4929} |

### 5.3 各文件改动摘要

| 文件 | 改动 |
|------|------|
| test_b2_crashsafe_entry.py | +3：`_validate_take_profit` 桩（修复 13 个 FAIL → 15/15） |
| test_crash_injection.py | +3：同上（修复 2 个 FAIL → 20/20） |
| test_sg4.py | +22：锚点更新 + 真实 TP helper 绑定（修复 H4 2 个 FAIL） |
| test_sg3_p1.py | +15：真实 TP helper 绑定（修复 E 2 个 FAIL） |
| test_sg1_ready_gate.py | +7：GateFake 补纯桩（`_tp_update_blocked→False` 等，scenario_6 只测 READY Gate 独立性，TP 语义由 sg4/sg3_p1 覆盖） |
| test_b2_close_gap.py / test_b2_create_gate.py | 锚点集更新 |

---

## 六、全量回归证据

**20 套件 206 场景全绿**（运行环境：`/g/my-crypto-bot/.venv/Scripts/python.exe`，ccxt 4.5.68）。

本次修改直接影响面：
- test_sg4（25 场景 + 2 SKIP）：C5 Create-Verify-Commit + 14 处 retries=1 + H4 TP 无效恢复链
- test_sg3_p1（28 场景）：C4/SG3-P1 保护单有效性 + E TP 无效恢复链
- test_b2_crashsafe_entry（15 场景）/ test_crash_injection（20 场景）：开仓循环崩溃安全
- test_b2_close_gap（10 场景）/ test_b2_create_gate（10 场景）：锚点校验
- test_sg1_ready_gate（7 场景）：READY Gate 独立性
- 其余 14 套件：无相关改动，全绿

---

## 七、遗留待裁决问题

1. **batch_142951 实盘残留**：0.001 BTC 已成交、TP 缺失（8-19 事故遗留），需人工对账清理
2. **batch_142633**：可能已成交，状态待对账（reconcile_pre_launch.py）
3. **P1-API-01 ban-until 持久化**：418 封禁跨进程持久化（未实施）
4. **D-001 主功能未实施**：KAMA 跟踪止盈 + 自动保本，等审计稳定后推进

---

## 八、请 ChatGPT 重点复核

1. **R1 推导严格性**：TP ≤ 现价 ⇒ 成交价 > 现价 ⇒ TP < 成交价 ⇒ -2021 的推导是否完备？边界 TP == 现价 用 `<=` 拦截是否足够？（条件单触发价是否可能 == 现价而非 > 现价？）
2. **R2 双维度必要性**：现价维度是否完全覆盖 -2021（币安判定基准是现价）？成本维度（BUY 需 TP > 成本）是否可能误拦合法场景（如持仓成本高于现价的浮亏保本 TP）？
3. **自愈设计**：标记不短路校验的自动恢复路径是否完整？`_tp_update_blocked` 每轮都执行 `load_all_states + save_batch_state`（清标记时）——是否有频繁写盘性能隐患？（R2 失败时每轮只读不写；仅"由无效转有效"才写）
4. **R3 熔断语义**：熔断短路（tp_fail_count ≥ 5）仅终端打印、不发 TG——是否需要补 1 次告警通知用户"该层已熔断，请人工介入"？（对称 SL 的 L4148 行为是发 ⚠️ 提示）
5. **FAILED 去重修复的恢复语义**：移除 FAILED 分支的 `_gate_alert_clear` 后，验收标准④"真正恢复后重置"如何满足？——成功路径（CONFIRMED/ABSENT/TP 挂出）会清计数/清标记，但 `_gate_alert_counts` 的 FAILED 条目是否会在恢复后残留？（`_gate_alert_clear` 在哪些成功路径仍被调用？）
6. **预生成段**：`_tp_update_blocked` 的 `return` 在 SL 段之后，是否确认不影响 SL 挂单？cost=0.0 只校验现价维度是否有遗漏场景？
7. **测试盲区**：H4/E 场景覆盖 R2 恢复链；R3 熔断（连续 5 次失败）和"用户改价自愈"（标记清除后恢复挂单）是否有专项测试场景？无 → 是否需要补充？
8. **实盘恢复条件**：本 4 层修复 + B1/B2 全套 + 全绿 + 复核通过后，恢复实盘还需要什么前置条件？

---

## 九、ChatGPT 终审补强 v3 实施记录（2026-08-20）

### 9.1 终审结论
ChatGPT 终审：**不回滚 96b94ed，整体设计正确**。要求 4 项补强（用户已批准全部实施）：

| 项 | 要求 | 状态 |
|---|---|---|
| A | R2 成本边界修正：BUY 应为 `TP > 现价 且 TP >= 成本`（允许 TP==成本=保本退出） | ✅ 已实施 |
| B | R3 熔断通知：熔断时 1 次 critical + 去重（之后静默，成功挂出后清除） | ✅ 已实施 |
| C | FAILED 告警恢复：成功路径调用 `_gate_alert_clear` 恢复 3 次 TG 额度 | ✅ 已实施 |
| D | 3 个专项测试：R3 熔断 / 用户改价自愈 / FAILED 告警恢复 | ✅ 已实施（21 场景） |

### 9.2 改动 A：R2 成本边界修正（trader_260725.py `_check_tp_viability` L659）

v1 判定 `BUY: TP > max(现价, 成本)` 会误拦**浮亏保本**场景（成本 71000 > 现价 70000，TP=71000 合法保本退出却被拦）。
v2 修正为**双维度独立判定**：

```python
if side == 'BUY':
    if tp_price <= mark_price or tp_price < cost_price:   # 无效
else:
    if tp_price >= mark_price or tp_price > cost_price:   # 无效
```

- 现价维度防 -2021（币安判定基准是现价，`<=` 拦截）
- 成本维度防无意义止盈（`<`/`>` 拦截），**`==` 成本 = 合法保本退出放行**
- SELL 对称

**边界矩阵测试锁定**（test_tp_validation.py T4，9 例全对）：含 `BUY TP==成本→放行`、`BUY TP==现价→拦截(-2021)`、`SELL TP==成本→放行`、`SELL TP>=现价→拦截`。

### 9.3 改动 B：R3 熔断通知（L130 + L739 + L4396）

- `__init__` 新增 `self._tp_breaker_alerted = {}`（去重键 = (batch_id, layer)）
- `_tp_update_blocked` 熔断分支：首次熔断发 1 次 🚨 critical（"止盈补挂已熔断"），后续静默
- 补挂 TP 成功段（L4396）：`self._tp_breaker_alerted.pop((batch_id, batch_filled_count - 1), None)` → 成功挂出后解除去重，下次熔断可再提醒

### 9.4 改动 C：FAILED 告警恢复（4 处成功路径补 `_gate_alert_clear`）

此前 `_gate_alert_clear` 仅 4 处在 `_assert_create_allowed` 放行分支（gate 通过=状态变化）。成功挂出路径缺 clear → FAILED 告警 3 次额度被永久吃掉，验收标准④"真正恢复后重置"不满足。补齐：

| 成功路径 | 位置 | clear 对象 |
|---|---|---|
| 补挂 TP 成功挂出 | L4397 | `tp_identity` |
| 兜底 SL 成功挂出（CONFIRMED） | L4849 | `identity` |
| 用户改 TP 成功 | L1331 | `tp_identity` |
| 预生成 TP 成功挂出（CONFIRMED） | L4995 | `identity` |

各 clear 与 FAILED/gate 拒绝直发点**同 identity**（已逐点核查作用域），语义统一为"该 identity 成功挂出 = 真正恢复 → 恢复全部告警额度"。

### 9.5 改动 D：专项测试（新文件 test_tp_validation.py，21 场景全绿）

| 场景组 | 覆盖 | 场景数 |
|---|---|---|
| T1 R3 熔断 | 熔断短路零 API / critical 恰 1 次 / 连续调用去重 / 成功挂出后可再提醒 | 6 |
| T2 用户改价自愈 | 不合理→标记+critical / 仍不合理→静默 / 改合理→清标记放行 / 改动 A 边界（保本退出） | 9 |
| T3 FAILED 告警恢复 | 3 次 TG→第 4 次静默 / clear 清计数 / 恢复后重新 3 次额度 / 闭环稳定 | 5 |
| T4 改动 A 边界矩阵 | BUY/SELL × TP/现价/成本 9 例组合 | 1 |

### 9.6 全量回归证据（21 套件 227 场景全绿）

- 原 20 套件 206 场景全绿（含 test_orphan_guard 5/5 场景逻辑 PASS；沙箱回收站 OSError 为环境伪故障，与代码无关）
- 新增 test_tp_validation 21 场景全绿
- 锚点漂移修复（改动 A/B/C 插入 +27 行）：test_sg4 A/B/C_LINES、test_b2_create_gate GATE_LINES、test_b2_close_gap GAP_CREATE_LINES 全部实测更新（Grep 独立核实 14 处 create 调用点）
- py_compile 全过；备份：backups/20260820_chatgpt_final/（修改前）+ backups/20260820_chatgpt_v3_apply/（修改后）

### 9.7 终审 8 问答复更新

- **Q4（R3 熔断是否需告警）**→ 已闭环：熔断 1 次 critical + 去重，成功挂出后清除可再提醒
- **Q5（FAILED 恢复语义）**→ 已闭环：4 处成功路径补 `_gate_alert_clear`，额度恢复
- **Q7（专项测试盲区）**→ 已闭环：test_tp_validation.py 21 场景
- **Q2（R2 成本维度误拦）**→ 已修正：`>=`/`<=` 边界放行保本退出（改动 A）
- **Q6（预生成段 return 不影响 SL）**→ 维持原结论：`return` 在 SL 段之后，不影响 SL 挂单

### 9.8 请 ChatGPT 第二轮复核

1. 改动 A 双维度独立判定 + `==` 成本放行，是否仍有边界遗漏？（如 cost==0 预生成段只校验现价维度）
2. 改动 B 熔断告警去重键 (batch_id, layer) 与 `_tp_breaker_alerted` 无清理机制（批次永久熔断时键常驻）——是否需要 LRU/时间窗口？
3. 改动 C 4 处 clear 是否覆盖全部"真正恢复"语义？（用户改 SL / 保本损 / 部分减仓换挂 SL/TP 成功路径未 clear——但那些路径的告警 identity 是否与 FAILED 计数相同？）
4. test_tp_validation.py 21 场景是否满足验收标准 ②③④ 的证据要求？
5. 实盘恢复条件是否已齐备（此轮补强 + 全绿 + 复核通过）？

### 9.9 ChatGPT 第二轮终审答复 + 终审补强 E1/E2 实施（2026-08-20）

**终审结论：不回滚。** 96b94ed + v3 补强通过，事故链（确定性错误提前阻断 → 成交后二次保护 → 重试熔断 → 告警闭环 → 人工修复自愈）已闭环。剩余 3 个工程级建议，已逐一复核并处理：

| # | ChatGPT 建议 | 独立复核结论 | 处理 |
|---|---|---|---|
| 1 | R1 依赖"条件单成交价 ≥ 触发价"假设，R2 作最终防线 | 属实；R2 双校验已在补挂/兜底/预生成/用户改价全路径生效 | 无需动作 |
| 2 | cost==0 预生成段注释需明确"只防 -2021，不承担成本保护" | 属实（原注释语义不完整，维护者可能误读 cost=0 为"无限制"） | **E1 已实施** |
| 3 | `_tp_breaker_alerted` 需 batch 终态清理（长期运行内存管理） | 属实（仅 L133 初始化 / L740 写入 / L4396 pop，无清理） | **E2 已实施** |

#### 改动 E1：预生成段 cost=0 注释语义增强（纯注释，行数不变）

```python
# R2/R3: 成交后止盈价可行性预检（ChatGPT 终审 2026-08-20；确定性错误：不打 API、
# 1 次 critical、写 tp_param_invalid 标记）。预生成阶段无真实成本：cost=0 仅校验现价
# 方向 → 只防 -2021、不承担成本保护（cost=0 ≠ 无限制 = 尚未产生成本）；成交后 R2 双校验。
```

#### 改动 E2：`clear_batch_state` 终态清理熔断告警键（L934）

```python
def clear_batch_state(self, symbol, batch_id):
    if getattr(self, '_tp_breaker_alerted', None):  # 终态清理熔断告警键（ChatGPT 终审 2026-08-20）
        self._tp_breaker_alerted = {k: v for k, v in self._tp_breaker_alerted.items() if k[0] != batch_id}
    ...
```

- 语义：批次归档/清理时**无条件**释放该 batch 的 (batch_id, layer) 熔断去重键（状态已不存在时也清理，防止残留）
- 不引入时间轮（ChatGPT 确认没必要）：量级 = 活跃批次 × 层数，终态即释放
- `getattr` 保护兼容测试基座；键过滤精确到 batch_id，不影响其他批次
- 已补 **T5 专项测试（3 场景）**：本 batch 键全清 / 其他 batch 键保留 / 批次状态同步清理

#### 最终回归（21 套件 230 场景全绿）

- 原 206 场景 + 改动 D 21 场景 + T5 3 场景 = **230**
- test_tp_validation 24/24、test_sg4 25/25、test_b2_create_gate 10/10、test_b2_close_gap 10/10、test_orphan_guard 5/5
- E2 插入 +2 行 → 3 套件锚点再同步（Grep 独立核实 14 处 create 调用点：1310/1461/1675/2261/3518/3592/4085/4238/4368/4668/4810/4959/5225/5408）
- py_compile 全过；备份：backups/20260820_chatgpt_v3_apply/（E1/E2 完成态）

#### ChatGPT 第二轮复核 5 问答复（9.8 对应）

1. 改动 A 边界 → **通过**：TP==mark 必须拦（Binance 触发条件已满足 = 已触发状态）；TP==cost 放行保本退出正确；SELL 对称正确
2. breaker dict 清理 → 建议采纳，**E2 已实施**（终态清理，不做时间轮）
3. 部分成交重挂 TP 的旧 identity 残留 → 非当前事故链，不阻塞上线（已记录为后续检查点）
4. 测试覆盖 → **通过**：R3 6 场景 / R2 9 场景 / FAILED 5 场景（+ T5 3 场景）
5. 实盘恢复 → **可进入灰度**，前置 5 步：清事故仓位（batch_142951/142633）→ 启动恢复测试 → 错误 TP 演练（R1 最重要验证）→ 改价自愈演练 → 小仓灰度（最小数量 1 层）
