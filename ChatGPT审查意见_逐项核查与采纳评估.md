# ChatGPT 审查意见逐项核查与采纳评估

> 性质：对 ChatGPT《实盘恢复演练第一阶段总结》审查意见的交叉验证报告（不同 AI 建议互相参考的宪法）
> 方法：全部结论基于 2026-08-21 17:1x 对 `trader_260725.py` 的独立 Grep/Read 实测，非凭记忆
> 结论标记：✅ 采纳 / ❌ 不采纳（附理由）/ ⚠️ 部分采纳（附边界）

---

## 〇、核查总览：23 处 cancel_order 调用点分类

| 行号 | 所在函数/段 | 场景 | 需 replace 语义 |
|---|---|---|---|
| 1285 | `update_batch_tp`（用户改TP命令） | 撤旧→ABSENT→闸门→建新 | ⚠️ 先撤后闸（见 §2.2） |
| 1436 | `update_batch_sl`（用户改SL命令） | 同上 | ⚠️ 先撤后闸 |
| 1655 | `_update_sl_no_validation`（保本损） | 同上 | ⚠️ 先撤后闸 |
| 1764 | `_cancel_remaining_entries` | 清理开仓挂单 | 否 |
| 1943 | `_check_existing_conflicts` | 孤儿单自动清理 | 否 |
| 3084 | R-C 滚动撤销链 | 新单已 CONFIRMED，撤旧层单 | 否 |
| 3613 | 手动撤单跟进 | 撤未成交开仓挂单 | 否 |
| 3732/3738 | SL 触发平仓收尾 | 撤 TP/SL | 否 |
| 3839 | 部分减仓 SL 换挂 | 先建新→verify→撤旧 | 否（先建后撤） |
| 3913 | 部分减仓 TP 换挂 | 同上 | 否（先建后撤） |
| 4161 | 止损平仓收尾 | 撤 TP | 否 |
| 4324 | 止盈平仓收尾 | 撤 SL | 否 |
| **4423** | **F1 SL 更新段** | 撤旧→ABSENT→建新 | **✅ 已带 replace 闸门（4408）** |
| **4761** | **F1 TP 更新段** | 撤旧→ABSENT→建新 | **✅ 已带 replace 闸门（4746）** |
| 5548 | `/cancel` 用户撤单 | 开仓挂单 | 否 |
| 5672/5680/5688 | 市价平仓 | 平仓收尾 | 否 |
| 5771 | 限价平仓 | 撤限价单 | 否 |
| 5853/5861 | 限价平仓 | 平仓收尾 | 否 |
| 6012 | 限价平仓结算 | 平仓收尾 | 否 |

**结论：F1/F2/F3 已覆盖所有"保护单换挂"路径；23 处撤单中仅 3 处用户命令入口为"先撤后闸"（非死锁，见 §2.2）。**

---

## 一、P0 不变量升格：Protection Mutation Ordering Invariant

**ChatGPT 建议**：任何保护单变更（cancel/replace/recreate/repair）必须 `Intent确认 → Registry仲裁 → Mutation执行 → Exchange确认 → Registry提交`，禁止"先动后问"。

**源码实证**：
- F1（L4408/L4746）：SL/TP 更新段已实现"撤旧前先仲裁（replace_order_id）"
- 部分减仓段（L3794/L3868）：已实现同款 replace 闸门
- F3（L2838）：补挂前实况裁决
- 残留"先动后问"：用户改 TP（L1285 撤 → L1306 闸）、用户改 SL（L1436 → L1457）、保本损（L1655 → L1670）

**评估结论：✅ 采纳（文档层面）**。事故模式归纳正确，且 F1/F2/F3 已在代码层面落实。建议将 "Protection Mutation Ordering Invariant" 作为正式条款写入《安全不变量_系统宪法.md》（文档操作，零代码改动）。残留的 3 处用户命令入口是否一并改造见 §2.2。

---

## 二、逐项回答

### 2.1 风险路径 A：`_place_prepared_orders_immediately` 预生成段

**ChatGPT 担忧**：预生成 TP 成功后 registry CONFIRMED，后续 batch 更新需重新计算 TP → 是否也是"撤旧→建新"未带 replace_order_id？

**源码实证（L5073-5089）**：函数注释与代码双重确认——**只在 `current_sl_id is None` 时调用（首次成交）**。预生成 TP（L5428）/兜底 SL（L5276）均为"首次/兜底挂单"，**无撤旧路径**。后续 TP 参数变化走监控循环 F1 段（L4746 已带 replace 闸门）或用户命令段。

**评估结论：❌ 不采纳（风险路径 A 不成立）**。预生成段无"撤旧→建新"结构，事件4 不会在此重演。可放心。

### 2.2 风险路径 B：部分减仓段

**ChatGPT 担忧**：减仓后旧 SL 1.0→新 SL 0.6，若 cancel old 后 create new 失败 → 裸仓。

**源码实证（L3775-3854 / L3856-3928）**：
- 整体为 **"先建新 → verify 成功 → 才撤旧"**（L3775 注释：M1 修复消除空窗期）
- 挂新失败/verify 失败 → **保留旧单 + 告警 + 下轮重试**（L3848-3854 / L3922-3928）
- 且 SL/TP 换挂前均带 `_assert_create_allowed(replace_order_id=...)` 闸门（L3794/L3868）
- 撤旧失败仅打日志（L3846），由 D-001 `pending_cancel_sl_ids` 延迟清理接管

**评估结论：✅ 采纳（已确认闭环，无需改动）**。部分减仓采用"先建后撤 + 旧单保留"，比事件4 的"先撤后建"更安全，旧保护单永远优先。ChatGPT 建议的 `partial_reduce_replace_fail` 专项测试值得新增（见 §3）。

### 2.3 tp_skip_create 是否导致下一轮继续撤建？

**ChatGPT 担忧**：替换被阻断 → 保留 tp_order_id → 下一轮 TP 参数仍变化 → 再次进入 replace → 重复尝试/重复告警/API 浪费。

**源码实证（L4744-4810 + L638-662）**：
- 下一轮确实会再次尝试替换（每次拒绝均保留 id，不死锁）
- **重复告警已治理**：`_gate_alert_notify` 去重键 = (identity, 拒绝类别)，同一场景最多 3 次 TG（L653），且闸门放行自动清零
- **API 无浪费**：替换闸门是本地仲裁（无 API 调用）；每轮最多 1 次尝试（监控循环周期）
- 死锁已排除：拒绝时不落 None → R14 不触发；R-B 自愈会在拒绝原因消除后放行

**评估结论：❌ 不采纳（维持现状）**。ChatGPT 建议的 registry `replace_pending` 状态机化是更"优雅"的方案，但现状已满足：不死锁、告警去重（≤3 次 TG）、无 API 浪费。状态机化需动 registry 状态模型 + 迁移 + 自愈联动，改动大、收益边际小，与"F5 单独评估"的既定排除一致。若后续观测到"长期反复替换尝试"再评估。

### 2.4 F3 adopt 是否可能收养别人的单？

**ChatGPT 建议**：intent 匹配增加 positionSide / closePosition。

**源码实证（L2838-2898）**：adopt 流程 = **registry 内 order_id 精确匹配 → fetch 该订单 → intent 语义复核**。Binance 订单号全局唯一，不存在"同方向不同批次保护单被误认"（order_id 已锁定唯一物理单）。intent 复核字段已含 symbol/side/order_type/reduceOnly/stopPrice/amount（L2780-2834）。

**评估结论：❌ 不采纳（防御已足够）**。order_id 精确性已保证不会收养别人的单，intent 是语义复核而非搜索匹配。positionSide/closePosition 属防御纵深，当前单向 + reduceOnly 模式不涉及 closePosition；positionSide 在 hedge 模式才区分，而 identity 本身已含持仓方向。可在 F3 注释补一行说明，不改逻辑。

### 2.5 F2 fallback 最新层 identity 是否安全？

**ChatGPT 建议**：order_id 精确 → 自动终结；找不到 → 降权标 UNKNOWN 等 reconcile，而非直接 ABSENT（错误终结比延迟终结危险）。

**源码实证（L4056-4069 / L4189-4202）**：order_id 精确遍历 registry 定位 identity（L4058-4061），**找不到才回退** `batch_filled_count - 1` 最新层 ABSENT。

**评估结论：⚠️ 部分采纳（维持行为，补注释）**：
- 回退触发条件极端：批次状态有 id 但 registry 无对应条目（条目丢失/崩溃恢复残留）
- **误终结伤害已被 F3 兜底**：若误标 ABSENT 而物理单仍在场，F3 补挂前裁决会 detect（在场+intent 匹配 → adopt；不匹配 → mismatch critical 告警），不会双单
- ChatGPT 方案的代价：延迟终结 → 真实撤单场景下保护单缺失窗口被拉长（hold 等 reconcile），裸奔风险不低
- **结论：维持回退 ABSENT**（有 F3 兜底），在 fallback 分支补注释说明"误终结由 F3 adopt/mismatch 捕获，延迟终结反而拉长裸奔窗口"。

### 2.6 演练遗留问题：update_take_profit 未清 invalid flag

**源码实证（L695-780）**：
- `_mark_tp_param_invalid`（L695）写 `tp_param_invalid` 标记
- `_clear_tp_param_invalid`（L722）只在 `_tp_update_blocked`（自动补挂预检）校验通过时调用（L780）
- **`update_batch_tp`（用户改 TP 命令，L1223）撤旧建新后不清标记** → 成功改价后 flag 残留

**评估结论：✅ 采纳（小修复，低风险）**。与 ChatGPT 一致：非资金问题，状态污染。残留期间不阻断功能（`_tp_update_blocked` 中 was_invalid 仅短路告警与 create，参数合理时下一轮自动 clear），但状态文件长期带脏标记，新信号读 flag 可能误判。修复 = `update_batch_tp` 成功后调 `_clear_tp_param_invalid`（约 2 行，需用户确认后实施）。

### 2.7 R1 critical 无去重

**ChatGPT 结论**：现在不用改（主动错误输入每次报警合理），可加"同 batch+同错误+30 秒"限频。

**评估结论：✅ 同意不改为现状**。与既有 `_gate_alert_notify` 治理边界一致：被动仲裁拒绝去重（≤3 次），主动用户输入不降噪（保留每次反馈）。30 秒限频若后续噪音实锤再加。

---

## 三、新增测试建议（T25/T26）

### T25：replace 失败保护测试

**ChatGPT 建议**：模拟旧 SL 存在 → replace 开始 → cancel 成功 → create 失败，必须明确旧保护状态。

**源码实证（F1 SL 段 L4422-4442 + L4465-4473）**：撤旧成功 → registry ABSENT → create 抛异常 → 异常处理（告警+下轮）→ 下轮 R14 补挂（registry=ABSENT → 放行）。**自愈路径已存在**。

**评估结论：✅ 采纳（新增专项测试）**。当前无测试锁定"撤旧后 create 失败 → ABSENT → R14 补挂"链路。注意测试需覆盖两类实现：F1 段（先撤后建）与部分减仓段（先建后撤），后者验证"挂新失败保留旧单"。

### T26：人工撤单恢复测试

**ChatGPT 建议**：交易所取消 SL → 程序不知道 → 下一轮必须 CONFIRMED→ABSENT→REPAIR。

**源码实证（L4051-4083 / L4185-4216）**：**F2 已实现**——terminal 检测（canceled/expired）→ registry ABSENT → `need_recover_sl/tp = True` → R14 补挂。含 is_programmatic / user_modified 区分（程序撤单与用户撤单不自动补，外部撤单自动补）。

**评估结论：✅ 采纳（新增专项测试锁定）**。代码已覆盖，缺回归测试固化（防未来回归）。

---

## 四、Step 5 观察窗口建议（Step5-A）

**ChatGPT 建议**：先只验证"开仓→保护生成→加仓→保护替换→撤单恢复"，不要马上测改价。

**评估结论：✅ 采纳（演练计划调整）**。刚修完 TP 死锁/registry 生命周期/terminal 同步，先验证保护链完整生命周期再注入改价，符合"小步验证"原则。与演练清单 §五（Step 5 小仓灰度）兼容——拆为 Step 5-A（保护链验证）→ Step 5-B（改价自愈）。清单 §九 执行记录表待补充该拆分。

---

## 五、最终行动清单

| 优先级 | 项目 | 类型 | 状态 |
|---|---|---|---|
| P0 | P0 不变量"Protection Mutation Ordering Invariant"写入宪法 | 文档 | 待确认 |
| P1 | `update_batch_tp` 成功后清 `tp_param_invalid`（约 2 行） | 代码 | 待确认 |
| P1 | T26 人工撤单恢复专项测试（F2 已有代码，缺回归锁定） | 测试 | 待确认 |
| P1 | T25 replace 失败保护专项测试（F1 先撤后建 + 部分减仓先建后撤两类） | 测试 | 待确认 |
| P2 | 3 处用户命令入口"先撤后闸"改"先闸后撤"（update_batch_tp/SL/保本损） | 代码 | 待确认（非死锁，纯不变量对齐） |
| P2 | F2 fallback 分支补注释（误终结由 F3 兜底、延迟终结拉长裸奔窗口） | 文档 | 待确认 |
| P2 | Step 5 拆分为 5-A 保护链验证 → 5-B 改价自愈 | 演练计划 | 待确认 |
| — | R1 30s 限频 | 不做 | 已否决（合理保留每次反馈） |

---

## 六、与 ChatGPT 的分歧摘要

1. **风险路径 A（预生成段）不成立**——函数仅在首次成交（current_sl_id is None）调用，无撤旧。ChatGPT 的担忧基于对代码结构的推断，实测不存在。
2. **tp_skip_create 不引入 replace_pending 状态机**——现状已不死锁 + 告警去重 ≤3 次 + 零 API 浪费，状态机化收益边际小。
3. **F2 fallback 维持 ABSENT（不降权 UNKNOWN）**——误终结被 F3 adopt/mismatch 兜底；延迟终结反而拉长裸奔窗口。
4. **F3 adopt 不补 positionSide/closePosition**——order_id 全局唯一已锁定物理单，intent 是语义复核非搜索。

其余建议（P0 不变量、invalid flag 清理、T25/T26、Step5-A）全部采纳。

---

# 第二次交叉讨论（终版，2026-08-21 17:2x）

> ChatGPT 复核本评估后新增 3 个实质点（P0 命名升级 / F2 fallback reason / Registry drift 观测），其余认可我方结论。
> 本终版基于 17:2x 对源码的补充实测（update_batch_tp L1223-1358、F2 fallback L4062-4069、_clear_tp_param_invalid L722-728）。

## 1. P0 不变量命名升级：Exchange Reality First Invariant ✅ 采纳

**ChatGPT 建议**：将 "Protection Mutation Ordering Invariant" 升级为 "Exchange Reality First Invariant"，覆盖所有状态变化动作（创建/撤销/收养），核心表述：

> 任何保护单状态迁移必须经过 Intent → Registry Arbitration → Exchange Mutation → Exchange Reality Confirmation → Registry Commit；Registry 不得先于交易所事实进入最终状态。

**评估**：命名更准确。原 Mutation Ordering 偏重"顺序"，Exchange Reality First 点出本质——**交易所真实状态是最终裁决源，registry 是投影**（正是 D-004/事件3/事件4 的共同病根：程序过度相信本地状态）。收养（adopt）路径本就"先有 exchange 事实、后补 registry"，该表述可自然涵盖。

**落地**：写入《安全不变量_系统宪法.md》时用此命名，并将既有 8 条不变量中涉及保护单状态迁移的条目统一指向该条款（文档操作，零代码改动）。

## 2. F2 fallback 增强 reason ⚠️ 部分采纳（行为不变，审计增强）

**ChatGPT 建议**：fallback（order_id 找不到回退最新层）标 ABSENT 时用独立 reason（如 `fallback_registry_missing`），便于审计区分"精确匹配终结"与"回退猜测终结"。

**源码实证（L4062-4069 SL / TP 段同款）**：fallback 分支与精确匹配分支共用 `terminated_reason=f'terminal_status_{sl_status}'`，审计无法区分。

**评估**：✅ 采纳 reason 区分（每处改 1 行字符串，零行为变化）。上轮"补注释"升级为"reason 落盘"——`terminated_reason` 字段本来就随 registry 持久化，区分后复盘事故可直接看出"哪条是精确终结、哪条是回退猜测"。流程逻辑维持 ABSENT 不变（误终结由 F3 兜底，理由见 §2.5）。

## 3. tp_skip_create 观测指标 ❌ 不实施（理由与上轮一致）

**ChatGPT 建议**：不做状态机（认可我方），但加 `replace_attempt_counter / last_replace_block_time` 观测日志，实盘持续 blocked >10 分钟再升级。

**评估**：观测指标本身低风险，但收益边际小——现状每轮拒绝已打印 `🚫 [仲裁] 跳过替换` + 拒绝原因 + `_gate_alert_notify` 去重计数，实盘"持续 replace blocked"从日志即可察觉（轮次间隔可见）。加 counter 仅省去人工数日志。**结论：不新增代码**，若实盘观测到持续 blocked 现象再评估状态机化（此时才有数据支撑）。

## 4. Registry Drift Monitoring ❌ 本轮不做（归入 F5 评估）

**ChatGPT 建议**：每 30 秒记录 registry vs 交易所匹配数（drift 计数），drift>0 再进修复流程；明确"不是必须现在做"。

**评估**：本质是**运行期 protection reconcile 循环**，与既定排除的 **F5（protection reconcile 循环 + 状态机重构）范畴重叠**。且现状已有两层近似能力：F3b reconcile（启动前全量对账）+ F3 补挂前实况裁决（运行期单点仲裁）。**结论：不新增**，列为 F5 的候选输入，待演练全部完成后与 F5 一并评估。

## 5. 用户命令段"先撤后闸"结论反转：❌ 不改造（必要设计，非违例）⭐ 本轮最重要修正

**上轮评估**（§〇/§一）：3 处用户命令入口（update_batch_tp L1285 / update_batch_sl L1436 / 保本损 L1655）为"先撤后闸"，列为 P2 待改。

**本轮实测修正（L1282-1310）**：
- 闸门 `_assert_create_allowed` **未传 replace_order_id**（L1306 仅 `desc='用户修改止盈'`）→ CONFIRMED 在场时闸门必拒
- 因此**必须先撤旧 → registry ABSENT → 闸门放行**——"先撤后闸"不是疏漏，是换挂语义下唯一可行的顺序（除非给闸门传 replace_order_id 改造为先闸后撤）
- **撤旧失败时 registry 保持原样**（L1293-1295 return，不标 ABSENT）→ 物理单状态未知但本地不伪造"已撤"，下轮监控循环 terminal 检测接管 → **无死锁、无双单、无裸奔窗口**
- 事件4 死锁只发生在**自动维护段**（监控循环内，R14 补挂联动）；用户命令段无 R14 联动，撤旧失败即中止，无死锁条件

**结论**：维持"先撤后闸"现状。若未来要让闸门理解替换语义，正确改法是**给用户命令段闸门传 replace_order_id**（先闸后撤），属行为变更需专项回归，当前无收益。此结论同步修正 §一 与 §五 P2 行。

## 6. 其余确认项（ChatGPT 认可我方，无新增动作）

| 项 | 结论 |
|---|---|
| `_place_prepared_orders_immediately` 风险 | ❌ 不存在（ChatGPT 同意我方） |
| F3 adopt 增强 | ❌ 不改（ChatGPT 同意 order_id+intent 已足够） |
| R1 限频 | ❌ 不做 |
| Step 5-A/B 拆分 | ✅ 采纳（演练清单 §五 待更新） |

---

## 终版行动清单（第二次交叉讨论后收敛）

| 优先级 | 项目 | 类型 | 工作量 | 状态 |
|---|---|---|---|---|
| P0 | 宪法新增 "Exchange Reality First Invariant"（替代原 Mutation Ordering 提法） | 文档 | — | 待确认 |
| P1 | `update_batch_tp` 成功后清 `tp_param_invalid`（L1348 附近 1 行） | 代码 | ~1 行 | 待确认 |
| P1 | F2 fallback reason 区分（SL L4067 + TP 同款，`terminal_status_<s>_fallback`） | 代码 | 2 行 | 待确认 |
| P1 | T26 人工撤单恢复专项测试（F2 代码已覆盖，缺回归锁定） | 测试 | 新增 | 待确认 |
| P1 | T25 replace 失败保护测试（T25-A F1 先撤后建恢复链 / T25-B 部分减仓旧单保留） | 测试 | 新增 | 待确认 |
| P2 | Step 5 拆为 5-A 保护链验证 → 5-B 改价自愈 | 演练计划 | 文档 | 待确认 |
| — | 用户命令段先闸后撤改造（上轮 P2） | **已反转** | 0 | ❌ 必要设计，不改 |
| — | tp_skip_create 观测指标 / Registry drift | **不实施** | 0 | ❌ 归入 F5 评估 |

**收敛原则**：核心逻辑冻结（47f8228 视为"可灰度实盘版"），本轮仅做 3 行代码增强 + 2 个测试锁 + 2 份文档，不引入新架构。
