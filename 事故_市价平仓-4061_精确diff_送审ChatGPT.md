# 市价平仓 -4061 事故：精确 diff 送审稿 **v6.1**

> 提交给：ChatGPT 逐行审查
> v1：2026-08-29 20:0x ／ v2：2026-08-29 20:4x ／ v3：2026-08-30 ／ v4：2026-08-30 ／ v5：2026-08-30
> **v6**：2026-08-30（回应你对 v5 的复审——「只剩 2 个事务边界」，2 边界 + 1 语义收紧全部处置）
> **v6.1（当前）**：2026-08-30（回应你对 v6 的复审——3 个 P0 + 2 项修正 + 测试/checker 假绿，
> 全部核实成立并处置；**另含我自查抓到的 1 个落地级 P0（C-7）**）
> **状态：生产代码零改动**（`git diff --stat HEAD -- trader_260725.py bot_runner.py watchdog.py parser.py`
> 为空，HEAD 仍 `e953d79`）。
> 本文所有 `before` 均为磁盘源码原样摘录（含行号），`after` 为可直接套用的完整代码。
>
> **机器检查**（送审前必跑，**四项均 rc=0**）：
> | 检查器 | 判据 | 当前 |
> |---|---|---|
> | ① `check_doc_code_blocks.py` | 全部 `python` 块 ast.parse + **Ellipsis 占位检测**（§八） | 14/14 |
> | ② `check_doc_helper_parity.py` | helper 全集与 `送审附件_v6.1/new_helpers_v6.py` **逐函数 `ast.dump` 同构**（§八-B） | 11/11，自测 M1-M5 |
> | ③ `check_doc_helper_calls.py` | **调用闭包 7 查**（NameError / 死代码 / 位置参数数 / 关键字名 / 重名 / 生产签名改动清单 / 副本遮蔽实现）（§八-C，v6.1 新增） | rc=0，自测 M1,M2,M3,M4,M6 |
> | ④ `run_mutation_checks_v61.py` | 撤销任一防护 ⇒ 指定用例必须失败（含 M0 基线对照） | 10/10 杀死 |
>
> ③ 是 C-7 的来源：**`ast.parse` 合法 ≠ 落地可用**。
> 块类型语义（采纳你 §七 的措辞要求）：
> **`python` = 可执行完整替换块，不含任何 `...` 占位**；
> **`python-frag` = 人工 diff 片段 / 源码截断摘录，豁免语法检查**。
>
> **v6.1 相对 v6 的改动集中在 5 处**：改动 1b 追加 2 / 追加 3（`_record_realized_pnl`
> 签名 + 日报读取侧，C-7）、改动 1d（限价 ENTRY gate，你的 P0-3）、
> helper 第 11 个 `_set_close_reason_if_current`、BEGIN/rollback/reason 的
> `_persist_states()` 返回值检查、`_derive_close_txn_vars` 三项完整性校验。
> **待你裁定 3 项**：N-1（冻结告警残留 3 条 fail-silent 分支）／
> N-3（限价 gate 回滚后监控误判手动撤单）／
> D-006 是否要把 `prior_reduction_unknown` 升级为 Fail-Closed 触发条件。

## 🚨 v5 修订说明（回应你对 v4 终审的 4 个上线阻断项）

先说核实：你终审里的每一条代码位置声明我都独立 Grep/Read 复核过，**4 项全部成立**。
其中 §一 是本次最重的一条——**它证明我的 v4 实现没有真正实现我自己提出的不变量**。

| 终审 | 你的指控 | 我的源码核实 | v5 处置 |
|---|---|---|---|
| §一 P0 | `_survey_same_side_batches()` 把 target 排除了（`close_phase>=1 → continue`），而真实调用顺序是先写 `close_phase=1` 再调 guard → 你自己的决定性例子会**再次放行** | ✅ 成立。`G:/tmp/new_helpers_v4.py` L245 确有该过滤；生产 `close_phase=1` 写入在 **L6983**、`_close_amount_guard()` 调用在 **L7013 之后**。**负向测试 G6-v4 复现：v4 在 target 已 phase=1 的状态下返回 0.001（放行）** | `sum_all` 改为**强制含 target**（coverage 不变量要求）；新增 `blocking_count` 暴露"其他批次也在关闭中"（§六）；新增 `G6/G6b/G6c` 三个场景 + `G6-v4` 负向（改动 1.5） |
| §二 P0 | `close_op_id` 只保护 rollback，没保护"close 的所有权"；TG callback 起新线程、入口无 `phase==0` 原子 claim → 双击两次都下单 | ✅ 成立。`bot_runner.py` L1492/L1503/L1560 确为 `run_in_executor` 起新线程；`close_position_market` **L6947-6984 入口零 `close_phase` 检查** | 新增 **`_begin_close_request_if_active()`**：锁内生成 `uuid4` + 校验 `close_phase==0` + 原子 claim + 落盘。**只有 claim 成功者才能继续调交易所**（改动 3v5） |
| §三 P0 | `close_op_id` 生成得太晚（`close_order_placed=False` 段，L7003），但 `close_phase=1` 落盘在 L6983 → 拼起来是 **NameError**；毫秒时间戳不适合做事务身份 | ✅ 成立。生产 `close_order_placed = False` 在 **L7003**，`close_phase=1` 在 **L6983**（更早）；`import uuid` 在 **L12** 已存在 | op_id 改由 BEGIN 内 `uuid.uuid4().hex` 生成，"生成 + claim + 落盘"成为同一原子步骤（改动 3v5） |
| §四 P0 | ENTRY helper 返回 False 被忽略，旧 converge 从后门把 `None → []` 放回 | ✅ 成立。文档 v4 改动 1 的调用行确实忽略返回值；生产 `_converge_batch_orders_before_clear` **L7288-7290 与 L7409-7411** 确为 `fetch_open_orders(...) or []`，且 `_safe_api_call`(L1160) 在底层返回 None 时**不抛异常、原样返回** | 调用处改为 `_entries_ok = ...; if not _entries_ok: raise RuntimeError(...)`（禁止进入本轮 clear）；**顺手清掉 converge 两处 `or []`** → 非 list 走 CONVERGENCE_UNKNOWN（改动 1 + 新增改动 9） |
| §五 | TERMINAL_ZERO 还差最后一个 Fail-Closed：`filled or 0` 是小型 UNKNOWN→ZERO；`closed + filled=0` 是矛盾组合 | ✅ 成立。v4 helper L148 `filled = float(order.get('filled') or 0)` | `filled` 缺失/None → **UNKNOWN**；`closed/filled + filled==0` → **UNKNOWN**；TERMINAL_ZERO 只允许 `canceled/expired/rejected` + **权威 filled 明确存在且 == 0**。负向 `T1-v4/T2-v4` 锁定 v4 会误给回滚资格（改动 1.5） |
| §六 | 正在关闭的其他 batch 不能从 coverage 中消失；建议同方向一次只允许一个 close transaction 在途 | ✅ 采纳（你的理由：`limit_pending` 仓位可能 100% 在场，同时推理多个 MARKET/LIMIT close 比禁止并行更复杂） | BEGIN 内新增同 symbol+同方向单飞检查（拒绝 `same_side_close_inflight`）；`sum_all` 保守计入所有同方向批次台账（改动 3v5 + 1.5） |
| §七 | `ast.parse == 语法合法` ≠ `diff 可直接应用`；`...` 在 Python 里合法 → checker 会 GREEN | ✅ 成立。v4 改动 1 AFTER 尾部确为 `...  # ← 占位` | **AFTER 里不再出现任何 `...`**：既有 L7115-7143 异常通道**原文完整写入**。checker 新增 Ellipsis 检测，含占位的块一律判失败（§八） |
| §八-1 | `_read_position_amt` 非 list 时仍返回 `total=0.0`，不是严格 Fail-Closed | ✅ 成立。v4 L46 `for pos in positions if isinstance(positions, list) else []` | 非 list → **None**（负向 `P1-v4` 证明 v4 返回 0.0）（改动 1.5） |
| §八-2 | 台账漂移（ledger 0.001 / 实际只剩 0.0005）时，仅靠最后 0.0005 的成交**无法恢复此前那 0.0005 的真实退出价与 PnL** | ✅ 同意，属账务正确性问题 | 结算标记 `pnl_partial` + `prior_reduction_unknown`，**禁止把不完整 PnL 展示成完整批次精确已实现盈亏**（改动 1b 追加） |

**我对你 §四 理论修正的回应**：你写「多批次同方向时，安全判断应该基于『总实际敞口是否
足以覆盖所有仍需保留的 tracked exposure』」——这一条我完全接受，并且它正是 v4 想实现
的东西。**但你的实现批评也对：v4 的过滤条件让它退化掉了。** 已按你的覆盖不变量推导
（`actual >= sum_tracked` → 平掉 `L_target` 后 `actual - L_target >= sum_tracked - L_target`）
把 `sum_tracked` 修正为**含 target** 的合计。

**本次新增的负向对照**（证明 v5 的修正是实质修复、不是装饰品）：

| 负向场景 | v4 实测 | v5 期望 | 结论 |
|---|---|---|---|
| `G6-v4`：target 已 `close_phase=1` + 另一批 0.001，总敞口 0.001 | **返回 0.001（放行）** | `None`（拦截） | v4 决定性例子漏过 |
| `T1-v4`：`closed + filled=0` | **TERMINAL_ZERO（给回滚资格）** | `UNKNOWN` | v4 存在小型 UNKNOWN→ZERO |
| `T2-v4`：`canceled` 但 `filled` 字段缺失 | **TERMINAL_ZERO** | `UNKNOWN` | 同上 |
| `P1-v4`：`fetch_positions` 返回 dict | **0.0** | `None` | v4 非严格 Fail-Closed |

**测试规模**：`test_close_confirmation_v5.py` —— **52 场景 rc=0**。

---

## 🚨 v6 修订说明（回应你对 v5 复审的 2 个事务边界 + 1 个语义收紧）

你的复审结论我逐条核实过：**3 条全部成立**，且第 1 条（BEGIN 与 transaction snapshot
未绑定）是 v5 自己引入的、我此前完全没看到的窗口。

| 你的复审 | 指控 | 我的源码核实 | v6 处置 |
|---|---|---|---|
| **§一 P0** | BEGIN 只返回 `(ok, op_id, reason)`，调用方在 BEGIN **之后**仍使用 BEGIN **之前**的旧 `target_b_data` 快照 → 「我 claim 的状态」≠「我按下单的状态」 | ✅ 成立。市价 **L6964-6967** 在入口就算好 `last_filled_count / target_amounts / current_filled_amount / side`，BEGIN 在 **L6980-6984**（更晚）；而监控线程 **L6226 / L6245 / L6255** 确实会在检测到新成交后更新 `last_filled_count` 并 `save_batch_state`。限价路径同构（L7491-7494 早于 L7508-7512） | BEGIN 改为返回**四元组** `(ok, op_id, reason, snapshot)`（锁内 `dict(b)`）；新增 **`_derive_close_txn_vars()`** 供调用方以 claimed 快照为唯一基线重算全部 batch-derived 变量（改动 1c + 3v6）。
⚠️ **我自查补的一条**：你点名 4 个字段，实际是 **10 个**——监控线程那次落盘是一整个 update 块（L6231-6254，一次写 8 个字段），漏掉的 `entry_orders` / `tp_order_id` / `current_sl_id` 会产生**孤儿保护单**（详见下方「10 字段清单」）。这三个已补进 `_derive_close_txn_vars`，并把 TP/SL 撤单处改为 `_txn_vars[...]` 使来源显在使用点 |
| **§二 P0** | 市价路径「先撤 SL/TP，再 verify ENTRY」→ ENTRY gate 失败时保护单已撤，残余仓位裸奔 | ✅ 成立。v5 改动 1 的 AFTER 确为「撤 TP(L345) → 撤 SL(L353) → `_cancel_and_verify_entry_orders`(L365) → raise」；文档说明(3) 自己也写了「插入点在 SL/TP 撤销之后」 | 顺序改为 **CONFIRMED_FULL → 撤 ENTRY → 逐 ID 验证 ENTRY → gate=True 才撤 TP/SL**；gate 失败**直接 return**（不进 except），文案明确「SL/TP 已保留未撤」（改动 1） |
| **§二 小点** | `_verify_entry_order_terminal` 的 `OrderNotFound` 判 `gone` 语义过强——不存在 ≠ 没成交 | ✅ 成立。生产 **L1992** 注释原文：「订单确实不存在（已撤销/**已成交**/已过期）→ 安全清除」；G3a 的「-2011 = 已收敛」只对**撤销**目标成立，对 ENTRY gate 的目标（证明没成交）不成立 | `OrderNotFound` → **`unknown`** → gate=False → 保留 SL/TP + critical。正常路径不受影响：**L4151 实证**「自愈 fetch 已撤销订单返回 `status=canceled` 对象（不抛 OrderNotFound）」（改动 1.5） |
| **§六 裁定** | 同方向单飞 | — | ✅ **你已批准**，v6 原样保留，不再讨论 |

**范围核实（我自己加的一条）**：§二 的顺序问题**只存在于市价路径**。限价路径
**SL 全程不撤**（L7639 文案「止损单仍保留作为保护」），ENTRY 撤单**动作**本就在
撤 TP / 挂 LIMIT 之前，所以 v6 当时只补了 §一 的快照重算。
> ⚠️ **v6.1 更正**：我这段当时写「撤 ENTRY（gate）」是**错的**——生产 L7543-7549
> 是 `try: cancel_order / except Exception: pass`，无验证、无 gate。已被你
> 交叉审核 P0-3 抓出，v6.1 新增**改动 1d** 把「先撤」从动作升级为事实确认。

**本次新增的负向对照**：

| 负向场景 | v5 实测 | v6 期望 | 结论 |
|---|---|---|---|
| `S2`：ENTRY 在 MARKET 期间成交（对 v5 改动 1 的 AFTER 存档做**运行时**调用序列断言） | 撤 TP/SL 调用次数 = **2**，且发生在 verify ENTRY **之前** | 撤 TP/SL 调用次数 = **0** | v5 会裸仓冻结 |
| `O1-v5`：`fetch_order(ENTRY)` → `OrderNotFound` | `_verify_entry_order_terminal` 返回 **`gone`** | `unknown` | v5 把「查不到」当成「没成交」 |
| `O2-v5`：同上，`_cancel_and_verify_entry_orders` 整体 | 返回 **True**（放行 → 会撤 SL/TP） | False | v5 的 ENTRY gate 可被 OrderNotFound 击穿 |
| `D-neg`：入口读 `last_filled_count=1`，BEGIN 前监控线程更新为 2 | 下单量 **0.001**（只平一半） | **0.002** | v5 的 claim 与 transaction 是两个状态 |
| `D7-v5`：静态看撤 TP/SL 的 id 来源 | 取自 **`target_b_data`**（入口旧快照） | 取自 `_txn_vars`（= claimed 快照） | v5 的「唯一基线」只覆盖 8 个字段 |
| `D8-v5`：**运行时**调用序列（入口 SL_1 / claimed SL_2） | `cancel:TP1 → cancel:SL1 → cancel:E2` —— 撤的是**已被监控线程撤掉的旧保护单**，真 SL_2 **从未被撤** → 孤儿单 | `cancel:E2 → fetch_order:E2 → cancel:TP2 → cancel:SL2` | v5 同时命中 §一（stale id）与 §二（顺序在前） |

`D8` 的实测调用序列（同一输入下 v6 / v5 对照，一条序列同时证明 §一 与 §二）：

```text
v6: create_order → fetch_order:OID1 → cancel:E2 → fetch_order:E2 → cancel:TP2 → cancel:SL2
v5: create_order → fetch_order:OID1 → cancel:TP1 → cancel:SL1 → cancel:E2 → fetch_order:E2
                                       ↑ 撤的是已被监控撤掉的旧 SL，真 SL2 从未被撤 → 孤儿单
                                       ↑ 且发生在 cancel:E2 之前（§二 顺序问题同源可见）
```

**测试规模（v6）**：`test_close_confirmation_v6.py` —— **87 场景 rc=0**（v5 的 52 场景
全部保留：C=6 / T=6 / N=3 / B=19；G、P、E 三组见下）。v6 新增 **28 项**：

| 组 | 项数 | 内容 | 其中 v5 负向 |
|---|---|---|---|
| **D** | 22 | claimed snapshot 派生（D0 能力缺失前提 / D1-D1d 正常 / D2-D2c BEGIN 快照同源 / D3-D3b 明细缺层 / D4 无需平仓 / D5-D5b 账本损坏 / **D6-D6b 10 字段契约完整 / D7 静态来源 / D8-D8b 运行时 stale-vs-claimed**） | 3（D-neg：v5 范式只平 0.001；D7-v5 取自 target_b_data；D8-v5 撤 stale 保护单→孤儿） |
| **O** | 5 | ENTRY OrderNotFound 收紧（O1/O2/O3） | 2（O1-v5 判 gone / O2-v5 放行） |
| **S** | 8 | 事务顺序（S1 静态 AST + S2/S2b/S2c/S2d 运行时调用序列） | 3（S1-v5 / S2-v5 / S2-v5b） |

其余 **G=11 / P=2 / E=5** 为 v5 既有（归因守卫 / 持仓读取 / ENTRY 逐 ID 验证）。
合计 6+6+3+19+22+5+8+11+2+5 = **87**。

---

## 🚨 v6.1 修订说明（回应你对 v6 的交叉审核：3 个 P0 + 2 个修正 + 测试/checker 假绿封堵）

两轮交叉审核（含你对测试与 checker 源码的逐行审 + 蓄意破坏实测）我全部逐条源码核实过：
**全部成立**。其中 P0-3 直接推翻了我自己在 v6 稿头写的「范围核实」——我当时声称
「限价路径生产 L7541-7573 本就是撤 ENTRY(gate) → 撤 TP → 挂 LIMIT」，**这是错的**：
生产 L7543-7549 原文是 `try: cancel_order(...)` / `except Exception: pass`，
没有 fetch_open_orders 快照、没有逐 ID fetch_order、没有 bool gate。我把
「撤单**动作**顺序在前」误述成了「**gate** 在前」，在此更正并致歉。

| 你的审核 | 指控 | 我的源码核实 | v6.1 处置 |
|---|---|---|---|
| **P0-1** | BEGIN / rollback 忽略 `_persist_states()` 返回值 → 写盘失败仍宣称取得所有权 / 已回滚 | ✅ 成立。生产 **L1340** `-> bool`（L1369 账本损坏 False / L1387 写盘异常 False）；helper 两处均未检查 | BEGIN：`not persist → return False, '', 'claim_persist_failed', None`；rollback：`not persist → return False, 'rollback_persist_failed'`。**「写盘成功」成为「claim 成功」的一部分**（改动 1.5） |
| **P0-2** | derive 缺 `len(target_amounts) >= last_filled_count` 对称校验 → 切片静默少平 → 按单确认通过 → gate 假通过 → 撤保护 → 残留裸仓 | ✅ 成立。helper 旧 L196 `sum(target_amounts[:last_filled_count])` 无长度检查 | 新增 **`target_amounts_short`** Fail-Closed；附带 **`side_invalid`** 严格校验（非法 side 不得默认 BUY → 反向开仓风险）（改动 1.5） |
| **P0-3** | 「限价已有 ENTRY gate」不成立：生产是 try/cancel + except:pass，撤单失败静默吞掉后直接撤 TP、挂 LIMIT | ✅ 成立（上方更正）。生产 **L7543-7549** 原文核实 | 限价撤 ENTRY 升级为 `_cancel_and_verify_entry_orders` **gate，位置仍在 TP/LIMIT 之前**（**甲方案顺序不变**，只是把「先撤」从动作升级为事实确认）。gate 失败时平仓单未挂、仓位零变化 → **优先 CAS 回滚让监控恢复**（TP/SL 全程未动）；回滚失败才落 `limit_entry_unknown` 冻结（**改动 1d 新增**） |
| **修正1** | 「10 字段契约」计数错误：实际 **10 raw + 1 derived = 11 vars**；D6 未锁 exact key set | ✅ 成立。helper 返回 11 个键；D6 只断言了 3 个新补字段存在 | 文档统一改为「10 raw + 1 derived = 11」；D6 改 `set(vars.keys()) == EXPECTED_TXN_KEYS`（11 键，多一个少一个都报警） |
| **修正2** | ENTRY gate 失败不切 `close_reason` → 批次永停 `market_confirming` → 冻结监控只 print 不再周期 critical | ✅ 成立，且我发现连带坑：改动 4 的 critical 名单是**白名单**——新 reason 不在名单里会 **fail-silent**，必须同时改白名单 | 新增第 11 个 helper **`_set_close_reason_if_current`**（CAS 范围 = close_op_id 匹配 + phase>=1，persist 检查）；市价 gate 失败写 **`market_entry_unknown`**；改动 4 白名单 +2（改动 1 / 1d / 4） |
| **R2-①** | Fake `_persist_states` 无 return（永远 None）→ 恰好掩盖 P0-1，测试还 GREEN | ✅ 成立。测试旧 L135-137 无 return | Fake 改 `persist_ok=True` 参数 + 显式 return；新增 **B11/B12**（persist=False → claim / rollback 拒绝）+ v6.0 负向对照 |
| **R2-②** | `target_amounts_short` 完全漏测 | ✅ 成立 | 新增 **D3c** + v6.0 负向（v6.0 派生 0.001 放行，少平一半实证） |
| **R2-③** | D8 人工注入两个独立对象，没证明 `target_b_data = _claimed` 在完整链路上真的生效；且 stale/claimed 的 `entry_orders` 恰好相同 → ENTRY 侧缺陷不可见 | ✅ 成立 | 新增 **D9 完整集成链**：stale 磁盘 → 模拟监控更新（加 E3、移 SL2）→ **真 BEGIN → 真 derive → rebind → 跑文档 AFTER**；断言 cancel E2 **且 E3**、TP2、SL2。**D9-neg** 敏感性对照：故意喂 stale `target_b_data`（模拟整合时漏掉 rebind）→ E3 漏撤**必须被检出**——这正是 D8 看不到的 |
| **R2-④** | D6 无 exact key set | ✅ 成立 | 见修正1 |
| **R2-⑤** | 限价路径零事务级测试（extract_doc_after 硬定位市价） | ✅ 成立 | 新增 **L 组**：L1 gate 通过才放行 / L2 OrderNotFound → 不撤 TP、不挂 LIMIT、回滚恢复 / L3 fetch_open_orders=None / L4 ENTRY 已 filled / L5 调用序列锁死 |
| **R2-⑥** | S2 未查 gate 失败后的 `close_reason` | ✅ 成立 | 新增 **S2e/S2f**：persisted 最新快照 `close_reason == 'market_entry_unknown'` 且 `!= 'market_confirming'` |
| **R2-⑦** | D6b 过宽：`entry_orders` 缺失不能无条件归零 | ✅ 成立，附一处细节修正：D6b 现有输入 `last_filled_count=2 == len(target_amounts)`，恰是**安全情形**（无未成交层），其期望值保持 True **不变**；真正缺的是「有未成交层 + 缺失」 | helper：有未成交计划层时 `entry_orders` 缺失/不足 → Fail-Closed；新增 **D6c**（missing）/ **D6d**（short）+ v6.0 负向（⚠️ short 判据经送审前自审 F-1 收窄为「精确截断签名放行」，见下方自审表） |
| **R2-⑧⑨** | checker 两个 mutation 假绿（extra helper → rc=0；duplicate doc helper → rc=0）；「函数集合完全一致」名不副实 | ✅ 成立。checker 旧 L129 的 `in DEFAULT_HELPERS` 过滤使 extra 检查恒空；旧 L155「至少一份同构」放行错误副本 | checker 重写判据：每 helper 文档**恰好 1 份**且与实现 ast.dump 相等；`set(hfuncs) == set(DEFAULT_HELPERS)` 双向；新增 **`--self-test`** 两个 mutation 必须 rc=1（列为送审前置检查） |

**v6.1 新增负向对照（全部先对 v6.0 RED，再对 v6.1 GREEN）**：

| 负向场景 | v6.0 实测（缺陷） | v6.1 期望 |
|---|---|---|
| `B11-v60`：BEGIN 时 persist=False | **ok=True 照样发下单资格** | ok=False / snapshot=None |
| `B12-v60`：rollback 时 persist=False | **谎报 rolled_back**（磁盘仍 phase=1） | rollback_persist_failed |
| `D3c-v60`：last_filled=2 配 target_amounts=[0.001] | **派生 0.001 放行（少平一半）** | target_amounts_short |
| `D6c-v60`：有未成交层但 entry_orders 缺失 | **归零放行（UNKNOWN→EMPTY）** | entry_orders_missing |
| `D9-neg`：完整链路上故意喂 stale target_b_data | E3 漏撤（D8 注入式看不到） | **必须被检出** |
| `M1/M2`：checker 蓄意破坏 | extra / duplicate 均 rc=0 | 均 rc=1 |

**测试规模（v6.1）**：`test_close_confirmation_v6.py` —— **133 场景 rc=0**
（v6 的 87 场景全部保留；v6.1 新增 46 项：B 组 +9 / D 组 +22 / S 组 +2 /
L 组 +11 / M 组 +2，含下方自审追加的 7 项）。

**机器检查（全部列为送审前置，均可离线复跑）**：

| 检查器 | 判据 | 自测 | 当前 |
|---|---|---|---|
| `check_doc_code_blocks.py` | 文档 `python` 块 ast.parse + **零 `...` 占位** | — | ✅ **14/14**（另 10 个 `python-frag` 摘录块豁免） |
| `check_doc_helper_parity.py` | 11 个 helper 与文档**逐函数 ast.dump 相等** | **M1/M2/M3/M4/M5** 全过 | ✅ rc=0 |
| `check_doc_helper_calls.py` | 调用闭包 7 查（NameError / 死代码 / 位置参数数 / 关键字名 / 重名 / 生产签名改动清单 / 副本遮蔽实现） | **M1/M2/M3/M4/M6** 全过 | ✅ rc=0 |
| `run_mutation_checks_v61.py` | 撤销任一防护 ⇒ 指定用例必须失败 | 含 M0 基线对照 | ✅ **10/10 杀死** |

parity 的 5 个自测中 M4/M5 是本轮新增，专门锁死
`PROD_SIGNATURE_OVERRIDES` 白名单：M4 证明「已登记方法出现第 2 份定义」仍会
失败（白名单只豁免"是不是 helper"，不豁免"有几份"）；M5 证明「未登记的新
下划线定义」照样被拦（白名单没把 Fail-Closed 变摆设）。

**变异检查（本轮新增，把你的判据固化成可复跑工件）**：`run_mutation_checks_v61.py`
—— 逐个往 helper 注入「撤销某一项 v6.1 防护」的变异体，跑完整测试套件，断言
**指定用例必须失败**；含 **M0 基线对照**（注入无害注释，期望 rc=0 零失败，
证明「跑绿」本身有信息量）。当前 **10 个变异体全部被杀死，rc=0**：

```
M0-baseline           对照组（无害注释）            rc=0  零失败          ← 证明跑绿有意义
M1-begin-persist      撤销 BEGIN 写盘检查           → B11, B11b, B11c
M2-rollback-persist   撤销 rollback 写盘检查        → B12, B12b
M3-reason-persist     撤销 reason 写盘检查          → B13, B13b
M4-target-short       撤销 target_amounts 长度校验  → D3c, D3cb
M5-side-check         撤销 side 严格校验            → D3d, D3db
M6-entry-orders       撤销 entry_orders 两项校验    → D6c, D6cb, D6d, D6db
M7-f1-wide            entry_orders 回退宽判据(F-1)  → D6e, D6g, D6gb
M8-verify-always-gone 逐 ID 终态验证恒返回 gone     → E3, E4, S2, S2b, S2c, S2e, L2, L2c
M9-snapshot-check     绕过 gate 第 1 层快照检查     → E1, L3, L3c
```

复跑方式：`python run_mutation_checks_v61.py`（约 2.5 分钟，全离线）。

> **路径契约（v6.1 收尾改动，直接关系你能否复现）**：
> 早期版本的 helper 实现文件放在项目外的 `G:/tmp/`，不受版本控制 —— 整条验证链
> 挂在一个可能随时消失的临时文件上。现已把 **7 个附件全部纳入项目内**
> `送审附件_v6.1/`（v6 / v6.0 / v5 / v4 / v3 helper、`v5_after_market_close.py`、
> `new_helpers_v3_entry.py`），4 个工件（3 个 checker + 测试）的默认路径改为
> **相对自身 `__file__` 解析**，不再依赖 CWD。
> 变异体注入也从「文本替换测试里那行路径」改为**环境变量**
> （`HELPER_PROJECT_DIR` 指向项目根、`V6_HELPER_OVERRIDE` 指向变异体），
> 并保留 `TEST_HOOK` 校验：测试若不再读取该变量，注入点被悄悄移除会直接
> fail-loud（`TEST_HOOK_NOT_FOUND`）而不是静默假绿。
> 因此**只有产物**写入 `G:/tmp/mut61_auto/`，被审对象都在项目内、可随仓库搬移。

**checker 的变异体也要防"锚点漂移"**（本轮实测踩到）：变异体靠
`doc.replace(锚点, 变异, 1)` 注入。若锚点只是一个名字串，它会漂移到**文档正文**
（例如自审表格里引用该调用的一格），变异"生效"了却打在错误位置 → 假绿。
故三道硬约束：①锚点绑代码结构（「调用末行 + 紧随其后的围栏闭合行」）
②锁出现次数（`count(锚点) == 期望`，文档改版即 fail-loud）③反向探针
（`G:/tmp/drift_probe_gen.py`：把锚点故意退回会漂移的短串，守卫必须报错）。

---

### 🔁 送审前三路交叉自审（A 源码实证 / B 设计安全 / C 测试有效性，2026-08-30）

v6.1 成稿后、送审前，我先跑了三路独立自审（变异测试 + 生产源码逐点攻击），
发现并已处置如下。**F-1 是 v6.1 初版自己引入的回归，必须向你披露**：

| 发现 | 内容 | 核实 | 处置 |
|---|---|---|---|
| **F-1**（B 路致命） | `entry_orders_short` 误伤 🗑️ 按钮批次：生产 `cancel_open_orders`（**L6896-6897**）只截断 `entry_orders` 到 `last_filled_count`、**不动** `target_amounts` → `len(entry_orders)==last_filled_count < len(target_amounts)` 是生产自己创造的**合法状态**，v6.1 初版校验把它永久 Fail-Closed → 此类批次永远无法程序平仓（v6.0 无此校验，**v6.1 新引入回归**） | ✅ 成立（L6896-6897 逐字核实；L6879-6902 无 `target_amounts` 写入） | 校验收窄为**精确截断签名**：仅 `0 < len(_eo) == last_filled_count` 放行（未成交层已被有意移除，pending_ids 恒空，gate 无单可撤自然通过）；`len(_eo) < last_filled_count`（已成交层 ID 丢失=账本损坏）与部分截断（`last < len < target`，无生产路径）**维持 Fail-Closed**；`len==last==0` 也拦（无生产来源）。新增 **D6e**（🗑️ 签名放行）/ **D6f**（ID 丢失仍拦）/ **D6g/D6gb**（🗑️ 批次完整链 BEGIN→derive→gate 全通过且零撤单） |
| **N-1**（B 路） | `market_confirming` 冻结 fail-silent 还有 3 条残留分支（TERMINAL_ZERO 回滚被拒 / except 通道回滚被拒 / 结算后 clear 未收敛），修正2 只堵了 ENTRY gate 一条 | ✅ 成立 | **披露，不改逻辑**。裁定理由：三条分支均有一发 critical 兜底，且触发条件均为「已有人工处置入口」的终态；把它们全部切 reason 需要再动 3 处 AFTER，超出 v6.1 最小必改集。**请你裁定**：接受「一发 critical 即足够」，或指示 v6.2 补齐 |
| **N-2**（B 路） | `_set_close_reason_if_current` 返回值被忽略 → 双重故障（gate 失败 + 写盘失败）下退回 fail-silent | ✅ 成立 | **已修**：三处 AFTER（市价×2 / 限价×1）全部接收返回值，写盘失败时 critical 文案追加「reason 切换失败（why），冻结告警可能不再周期触发」 |
| **N-3**（B 路） | 限价 gate 失败回滚后，监控可能将被撤层误判「手动撤单」并连带撤掉其余未成交层（生产既有行为 L4809-4856），且发误导性「手动撤销」告警 | ✅ 成立（监控检测在冻结检查之前；生产无 ENTRY 补挂逻辑） | **披露，不改逻辑**。两种情形均不死锁、不裸仓（`canceled_layers` 跳过、SL/TP 只依赖已成交层）；属生产既有行为被新路径触发，非 v6.1 引入。落地后首次限价 gate 回滚若发生，按此预期解读告警 |
| **C-1**（C 路存活变异体） | `_set_close_reason_if_current` 的 `persist_failed` 分支零覆盖（删掉它 126 场景仍全绿，P0-1 同型盲区） | ✅ 成立（变异体实证） | 新增 **B13/B13b**：persist=False → 必须 ok=False + `persist_failed`，绝不谎称 reason 已切换 |
| **C-2**（C 路） | 4 个 v6.0 负向对照（D3c/D3d/D6c/D6d-v60）在归档被污染时以 TypeError 崩溃而非干净断言失败 | ✅ 成立 | 已改条件表达式（`if vars else None`），归档污染时记 ❌ 而非中断 |
| **C-3**（变异检查器 M7 抓到） | **D6gb 是装饰品断言**：只断言 `cancels == []`，但 F-1 缺陷版下 `derive` 失败 → gate 被 `if ok6g2 else None` 跳过 → 撤单数同样是 0 → **缺陷版反而通过** | ✅ 成立（M7-f1-wide 实测：D6e/D6g 失败而 D6gb 通过） | 断言改为 `(ok6g2, gate6g, cancels6g) == (True, True, [])`，把「链已成功」钉成前置条件 |
| **C-4**（变异检查器 M8 抓到） | S2c / S2-v5b 用裸 `list.index()` 做调用序列断言：变异体一旦移除某个 API 调用就抛 `ValueError`，测试**崩溃**而非干净失败，诊断信息全丢 | ✅ 成立（`ValueError: 'fetch_order:E2' is not in list`） | 新增 `seq_index()`（缺失返回 None），两处改为「下标存在且严格递增」 |
| **C-5**（变异检查器 M8 抓到） | AFTER 块未显式返回时 `run_txn` 返回 `None`，下游 `ret[0]` 抛 `TypeError` → 整套件崩溃 | ✅ 成立（`TypeError: 'NoneType' object is not subscriptable`） | 引入 `NO_RETURN` 哨兵；L1 期望值同步改为 `NO_RETURN`（其语义「落后续生产段」不变） |
| **C-6**（变异检查器 M9 抓到） | gate 第 1 层「快照不可判定」分支**只被异常兜底覆盖**：若把 `remaining is None or not isinstance(...)` 改回 `or []`（事故原型的 UNKNOWN→EMPTY），`for o in None` 抛 TypeError，被 catch 吞掉后外在表现与干净 Fail-Closed **完全一致**，原 L3 分辨不出 | ✅ 成立（M9 首轮「零失败」即为此盲区） | 新增 **L3c**（catch 模式断言「必须干净拦截，不得依赖异常兜底」）；`run_entry` 一律捕获异常并转成可断言值；L3 改用 catch 模式，崩溃变成可见 ❌ 而非整套件中止 |
| **C-7**（🔴 本轮新增，`check_doc_helper_calls.py`【4】抓到） | **只给调用、不给定义**：v6.1 前稿在 1b 追加段调用 `_record_realized_pnl(..., pnl_partial=_pnl_partial)`，却**通篇没有给出该函数的新签名**（全文 `def _record_realized_pnl` 命中 0 次）。照此落地 → 结算阶段 `TypeError`，且发生在**平仓已成交之后**，会中断结算/clear 把批次钉在 `close_phase=1` → 监控冻结，**正是本次事故的症状**。生产实证：L678 定义仅 8 个必填位置参数，4 个调用点（L5408/L5579/L7109/L7818）全是位置传参 | ✅ 成立（生产 L678-680 逐字核实；送审稿改动前 0 处定义） | 补出**生产签名改动 diff**（1b 追加 2）：新增 `pnl_partial: bool = False` + record dict 落 `prior_reduction_unknown`；并补 **1b 追加 3** 让日报消费该标记（否则标记只是装饰品）。**并新增 C-8** 说明为什么两个既有 checker 都没抓到 |
| **C-8**（本轮新增，checker 自身的设计盲区） | 已有 `check_doc_code_blocks`（语法）+ `check_doc_helper_parity`（逐字一致）**都不检查「谁调用了谁、签名对不对」** —— `ast.parse` 合法 ≠ 落地可用（v5 前科：`close_op_id` 生成太晚 → NameError）。且【4】最初只查 11 个 helper，而文档里 33 处关键字调用**全部落在生产方法**上 → 该项空转 | ✅ 成立（【4】首轮空转实测） | 新建 `check_doc_helper_calls.py`（7 查）。**过程中它自己先出了一次设计错误**：我最初让「文档自带定义」优先级最高，结果 M2 变异（给 helper 加必填参数）被文档里未改动的 helper 副本**遮蔽** → 假绿。修正为 **helper 实现文件 > 文档自带定义 > 生产现状**，并新增【7】把「副本 ≠ 实现」判致命 |
| A 路 | 14 项生产引用核实全过；唯一瑕疵 `_commit_protection_with_g3` 行号 off-by-one | L3463 空行，实际 **L3464** | 已修（3 处：送审稿×2 + helper docstring×1） |

**自审方法论说明（可复核）**：B 路报告 `G:/tmp/review_B_v61_design.md`（含 7 个
已排除攻击面的排除依据）；C 路报告 `G:/tmp/review_C_v61_tests.md`（12 个变异体
全部被精确杀死，无一装饰品断言；仅上述 1 个存活变异体，已补 B13/B13b 锁死）；
A 路报告 `G:/tmp/review_A_v61_source.md`。

**变异实证已固化为可复跑工件**：上面那两个临时变异脚本已升级成
`run_mutation_checks_v61.py`（10 个变异体 + M0 基线对照，见前文「变异检查」
小节）。**C-3 ~ C-6 这 4 个问题全部是它挖出来的**——在此之前，测试 132/132
全绿，但其中 1 条断言是装饰品（C-3）、3 条会以崩溃而非失败告终（C-4/C-5），
还有 1 条 Fail-Closed 分支实际只被异常兜底覆盖（C-6）。

**C-7 / C-8 是第三道机器检查（`check_doc_helper_calls.py`）挖出来的**，性质与
前 4 项不同：前 4 项是「测试写得不够硬」，C-7 是**文档本身缺了一块落地必需的
diff**（只给调用不给定义 → 结算阶段 TypeError → 把批次钉死在 `close_phase=1`，
正是本次事故的症状），C-8 则是我新写的 checker 自己的设计错误
（文档副本遮蔽实现 → M2 变异假绿），已由它的自测抓出并修正。

换言之：**场景数从 132 涨到 133 并不重要，重要的是盲区被逐个堵上**——
本轮新增的不是断言数量，而是「调用闭包」这个此前完全没人守的维度。

## 🚨 v4 修订说明（回应你对 v3 终审的 8 项）

| 终审 | 内容 | v4 处置 |
|---|---|---|
| §一 P0 | `not_filled` 过宽：open / partial / not_found 最终都可回滚 | 拆**六态**：只有 `TERMINAL_ZERO`（明确终态 + filled=0）可回滚；`close_order_placed=True` 置位后**绝不改回 False**；`not_found` 恢复 `_verify_order_created` 的 NOT_CONFIRMED 语义，绝不解释成「证明没成交」（改动 1 + 1.5） |
| §二 P0 | stale snapshot / 并发 rollback 竞态（`settled=True + close_phase=0` 自相矛盾） | **撤销 `allow_flag_rollback` escape hatch 与 AST 守卫**（采纳你的建议：问题整体消失、不再需要防扩散）；改为 `_rollback_close_request_if_current`：`_state_lock` 内重读磁盘 + `close_op_id` CAS（复用 G3b 范式 L3464，「锁内重读、同锁段落盘」）（改动 3v4） |
| §三 P0 | ENTRY 校验 `None→[]`（与 C-1 完全同型的 UNKNOWN→EMPTY 假确认） | 显式拦截 None / 非 list → critical；并**逐 ID `fetch_order(stop=True)` 终态确认**（与已接受的「事务事实按 ID 归因」原则一致）（改动 2） |
| §四 P0 | `min(台账,总敞口)` 在多批次 + 台账漂移下无法归因 | 归因规则收紧为**总敞口 vs 台账合计**：多批次下 `总敞口 < 台账合计` → 禁止自动平 + critical + 人工 reconcile。**⚠️ 同时指出：你的规则（`actual < ledger` 才拦截）拦不住你自己的决定性例子**——A/B 各台账 0.001、总实际 0.001 时 `actual == ledger_A`，单看本批台账发现不了漂移；可检测的不变量是「总敞口 vs 台账合计（含本批）」，测试 G3 用你的原始例子验证（改动 1.5） |
| §五 | 结算仍用旧 `current_filled_amount`（S9 只证明了订单确认、没证明结算正确） | `confirmed_filled_amount`（来自确认后的 fetch_order）**贯穿全部 6 处结算**（改动 1b） |
| §六 | AFTER 存在重复代码块，贴出去语法错误（「可直接套用」承诺失实） | 已修复；`check_doc_code_blocks.py`（文档代码块 → ast.parse）列为**送审前置机器检查**（改动 6） |
| §七 | 冻结告警触发过宽：正常限价平仓（close_phase=1 挂单数小时）也会 critical | 新增 `close_reason` 分型：`limit_pending_normal` 只 print；`market_confirm_unknown` / `market_partial` / `settlement_stuck` 才 critical；**无 reason 的遗留冻结按 stuck 处理（fail-noisy）**——部署前已冻结的批次（如事故批次 2）部署后第一次进监控循环就会告警（改动 8v4） |
| §八 | §五 验证计划残留 v2 delta 文案、测试表引用旧 7 场景脚本 | §五 全文重写；测试改 `test_close_confirmation_v4.py`（27 场景 + 对 v3 行为的负向对照） |

**已裁定项落地确认**：B-09 Fail-Closed ✅（读不到敞口不发单 + critical + 人工平仓）；
甲方案不变 ✅（市价：确认成交 → 撤 ENTRY → 逐 ID 验证；限价：先撤 ENTRY → 再挂 LIMIT）；
AST 两个蓄意绕过盲区——**随 escape hatch 一并撤销，此项不再需要裁定** ✅。

：v2 的「必须修 1」判据被我自己推翻

我没有把 v2 直接送给你，而是先组织了**三路独立子代理交叉审查**——
A 源码实证核对 / B 设计安全审查 / C 测试有效性审查，三者互不通信、各自出报告
（仓库内 `交叉审查_A_源码实证核对.md`、`交叉审查_B_设计安全审查.md`、
`交叉审查_C_测试有效性审查.md`）。

**结论：v2 不能送审。我自己设计的判据被证伪了。**

### 最重要的一条：delta 判据无法归因 → 假确认 → 裸仓（B-01）

delta（敞口减少量 ≥ 被平数量）回答的是「**总敞口少了多少**」，而方案需要知道的是
「**我这张平仓单成交了多少**」。`_read_position_amt` 读的是 symbol+方向的**总敞口**，
所以另一批次 SL 成交 / 用户 App 手动平仓 / ADL / 另一批限价平仓成交，**都会被记到本批头上**。

**决定性证据**（我认为无法辩驳）：delta 的**正样本**（v2 用来论证它正确的 S3：
`before=0.002, after=0.001, expected=0.001`）与**假确认样本**（本单 `filled=0`
根本没成交、总敞口被他方打掉）在观测数据上**完全同形**——两个场景在 delta 判据下
**物理不可区分**。而触发条件在剧烈行情下恰恰与「我要市价平仓」高度同时发生。

后果链：确认门放行 → 撤 TP/SL → 本批仓位仍在且无保护单 → `close_phase=2` →
监控冻结（`L5244`）→ 不补挂 → **裸仓 + 无告警 + 不恢复**。

**v3 改用 `fetch_order(order_id)` 按单确认**——每单独立、可归因，天然免疫多批次与
他方减仓。而且这不是新发明：项目里早有成熟的 Create→Verify→Commit 实现
`_verify_order_created`（**L3368**），明确写着「Verify 必须用 fetch_order（事务确认
点），不用 open_orders 快照」「UNKNOWN ≠ NOT_FOUND」，并踩过 OrderNotFound 可见性
延迟的坑（事件 3：4/4 单 create 成功但 0 秒 verify 全部假阴性 → 12 处误判 → 24 个
孤儿单）。**v3 直接复用这套三态语义。这是本轮我最该早点做的事。**

### v3 本轮改动（最小必改集）

| 问题 | 处置 | 位置 |
|---|---|---|
| 🔴 **B-01** delta 无法归因 → 假确认 → 裸仓 | 判据改为 `fetch_order` 按单确认（三态）；delta 降为二级交叉校验 | §七、改动 1.5 |
| 🔴 **B-02** 确认失败 = 永久冻结 + 冻结无告警 | 三态分级：`not_filled`→可回滚（不冻结）；`unknown`→不回滚 + 🚨critical；冻结分支加 TG 告警 | 改动 1、改动 8（新增） |
| 🔴 **B-03** `expected > pos_before` → 永久不可平 | `expected := min(台账量, pos_before)` | 改动 1.5 |
| 🔴 **A-1** 文档 4 个行号全错 | `_get_current_position_amt` 调用方订正为 **L2934 / L3521 / L4871 / L7305** | §7.1、改动 1 |
| 🔴 **C-1** `None`→`[]` → Fail-Closed 失效 | 显式拦截非异常 None，返回 None；新增 S8 钉死 | 改动 1.5、§7.4 |
| 🟠 **C-2** AST 守卫嵌套函数重复计数（1 处可凑成 2 处） | 调用点改归属**最近** FunctionDef 祖先 | 改动 6 |
| 🟡 **Q3** 依赖未经确认的交易所语义 | 代码层兜底：`amount := min(台账, 实测敞口)`，不依赖"超额 SELL 会被拒绝" | 改动 1.5 |

**已知残余盲区（如实记录，未修）**：AST 守卫仍有 2/11 绕过形式未被拦截
（`字符串拼接 + 转发 helper`、`functools.partial` 遮蔽 callee 名）；
另有 5 个语义变异体存活（去掉 `abs()` / symbol 过滤 / `is_hedge_mode` 过滤 /
`'both'` 分支 / 轮询次数均无断言覆盖）。详见「改动 6」。

---

### 附：v2 修订说明（回应你的终审裁定，保留存档）

你的结论：**架构批准 / 甲方案批准 / B 升 P0 批准，但当前 diff 不批准落生产**。
两个必须修 + 三条建议，本稿逐条落地：

| 你的要求 | 本稿处理 | 位置 |
|---|---|---|
| 🔴 **必须修 1**：市价 `create_order()` 成功 ≠ 仓位已平 | 插入 pre-read + delta 确认门；确认前**绝不撤 SL/TP** | 改动 1 + 新增改动 1.5 |
| 🔴 **必须修 2**：「3 字段白名单」文案与实现不一致 | 改为「**1 个 int 独立处理 + 2 个 boolean 白名单**」，并加对照表 | 改动 4 |
| 🟡 §十二 AST 守卫加固（动态 `**` 绕过） | 升级为 6 项检查，**八向对照**实测 | 改动 6 |
| 🟡 §十五 增加 `settled_by_limit_close` 不可回滚测试 | 新增 6 场景，含**负向对照** | 改动 7 |
| 🟡 §八 `_lost` 措辞强于事实 | 改名 `_attempted`，告警改为「已尝试撤销」 | 改动 5b |
| 🟡 §十七 `close_order_placed` 注释歧义 | 新增 `close_position_confirmed` 区分两个事实 | 改动 1 |
| 🟡 §四 `positionSide` 重复赋值 | **保留**显式赋值，理由见 §2.1(1) | 改动 1 |
| 🟡 §十六 实盘验证顺序 | 采用你的 Test 1-4 顺序 | §五 |
| 🔎 **自查追加**（你没指出，但属同类） | 守卫 RED 文案对「0 处调用」误报为「越界/不合规」；fixtures 实为 8 个样本而文档写 7 个 | 改动 6 |

最后一行是我按「必须修 2」的同一条纪律自查出来的：**表述与实现不一致**不止出现在
「3 字段白名单」那一处。已一并订正，避免你复审时再撞到第二例。

**⚠️ v2 我在落实「必须修 1」时曾推翻过一次自己的判据**：「仓位必须归零」看似自然，
但同 symbol 同方向允许多批次并存（D-006 最多 3 批），平掉一批后敞口本来就不为零
→ **100% 误判**。于是 v2 改用 **delta**。
**⚠️ 而 v3 又推翻了 delta 本身**（无法归因 → 他方减仓即假确认），最终改用
`fetch_order` 按单确认。**两次推翻的实证都在 §七。**

---

## 〇、本稿范围

摊开 **8 处改动**的精确 before/after（v3 新增改动 8：P0 冻结告警），外加两个必须由你裁定的议题：

1. **限价平仓的 ENTRY 撤单时机不能一刀切**（§三）—— 你已裁定甲方案，本稿据此固化
2. **§八 的 P0 升级条件，我做了源码实证，结论是 B 应升 P0**（§四）—— 你已批准

新增章节：**§七 探针实证**（支撑「必须修 1」判据选型的真实账户数据）。

---

## 一、改动总表

| # | 位置 | 内容 | 对应你的终审 |
|---|---|---|---|
| **1** | `close_position_market` L7004-7046 | A 修复（params 与限价平仓对称化）+ C 修复（撤 ENTRY 移到确认成交后**且返回值成为 clear gate**）+ 六态确认 + 归因守卫。**v6：ENTRY gate 移到撤 SL/TP 之前，gate 失败直接 return** | §一 / §四 / §五 / v6 §二 |
| **1b** | L7052-7109（6 处） | `confirmed_filled_amount` 贯穿结算 + **PnL 标记**（`pnl_partial` / `prior_reduction_unknown`） | §五 / §八-2 |
| **1c** | L6964-6984 / L7491-7512 | 🆕 **BEGIN 之后以 claimed snapshot 重算 transaction 变量**（市价 + 限价两处），入口不再派生任何交易参数 | v6 §一 |
| **1.5** | 新增 helper 全集 | v6：BEGIN（**返回 claimed 快照**）/ `_derive_close_txn_vars` / CAS / coverage 守卫 / 六态 / ENTRY 逐 ID（**OrderNotFound→unknown**） | §一 §二 §三 §四 §五 §六 §八-1 / v6 §一 §二 |
| **2** | L6945 前（新函数） | ENTRY 撤单 + 双层验证（v4 已有；**v5 追加：返回值必须成 clear gate**） | §三 / §四 |
| **3v6** | L6980-6984 / L7508-7512 | atomic BEGIN 替换 flags 写入：锁内 `uuid4` + `close_phase==0` 校验 + 同方向单飞 + **返回快照** | §二 / §三 / §六 / v6 §一 |
| **4** | L5244-5248 | 冻结告警按 `close_reason` 分型（`close_reason` 由 BEGIN 写入） | §七 |
| **5** | L7131-7136 / L7668-7676 | 两处 rollback → CAS 原子回滚 | §二 |
| **6** | 新增 `check_doc_code_blocks.py` | 文档代码块 ast.parse **+ Ellipsis 占位检测** | §七 |
| **9** | L7287-7290 / L7408-7411 | 🆕 converge 两处 `fetch_open_orders(...) or []` → 显式 CONVERGENCE_UNKNOWN | §四 |

**v6 相对 v5 的净变化**，只有三处（你的原话：「v6 实际只需要很小」）：
① BEGIN 返回 claimed 快照 + 调用方重算（改动 1c + 3v6）；
② 市价路径 ENTRY gate 前移到撤 SL/TP 之前（改动 1）；
③ ENTRY 的 `OrderNotFound` 从 `gone` 收紧为 `unknown`（改动 1.5）。

**v5 相对 v4 的净变化**（已获你批准，全部保留）：新增 BEGIN、coverage 含 target、
ENTRY 返回值 gate + converge 后门清理、TERMINAL_ZERO 收紧、`_read_position_amt`
严格化、PnL 标记、文档不含 `...` 占位。
六态状态机 / CAS 原子回滚 / close_reason 分型 / confirmed_filled_amount 贯穿 —— 主体未动。

---

### 改动 1：`close_position_market` L7004-7046（A + C）

#### BEFORE（源码原文，L7004-7046）

```python-frag
        close_order_placed = False  # P0 Batch A（回滚收紧）：平仓单是否已创建成功
        try:
            # 先撤销所有未成交的开仓条件单（保护单不撤，仍在位保护仓位）
            entry_orders = target_b_data.get('entry_orders', [])
            for idx, order_id in enumerate(entry_orders):
                if idx >= last_filled_count:
                    try:
                        self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol, params={'stop': True})
                        print(f"  └─ 已撤销开仓挂单: {order_id}")
                    except Exception:
                        pass

            # 🔥 修复漏洞1：先市价平仓，成功后再撤 SL/TP（原代码先撤 SL/TP 再平仓，
            # 若平仓失败则裸仓无保护且监控线程因 is_programmatic_cancel 不补挂）
            # reduceOnly 平仓后 SL/TP 即使短暂存在也不会反向开仓，风险远低于先撤保护再赌平仓
            close_side = 'sell' if side == 'BUY' else 'buy'
            order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='MARKET',
                side=close_side,
                amount=current_filled_amount,
                params={'reduceOnly': True},
                retries=1
            )
            close_order_placed = True  # P0 Batch A：平仓单已创建 → 此后失败绝不回滚关闭标记

            # 平仓成功 — 现在安全撤销保护单
            if target_b_data.get('tp_order_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, target_b_data['tp_order_id'], target_symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销止盈单: {target_b_data['tp_order_id']}")
                except Exception:
                    pass

            if target_b_data.get('current_sl_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, target_b_data['current_sl_id'], target_symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销止损单: {target_b_data['current_sl_id']}")
                except Exception:
                    pass
```

#### AFTER

```python
        close_order_placed = False    # 订单已创建（仅此而已）
        close_position_confirmed = False  # 仓位已真实减少（交易所侧事实）
        # ⚠️ close_op_id 由改动 3v6 的 atomic BEGIN 提供（锁内 uuid4 生成 + claim +
        # 落盘），**不在这里生成**。
        # v4 把它放在本段是明确的 integration bug：生产真实顺序是
        # close_phase=1 落盘（L6983）在前、本段（L7003）在后 → 按 v4 拼起来
        # NameError。BEGIN 让"生成 + claim + 落盘"成为同一个原子步骤。
        try:
            # 🆕 平仓确认·第 1 步：平仓【前】取本方向敞口基数。
            # 用途仅两件事：① `_close_amount_guard` 的归因判断 ② 成交后的二级
            # 交叉校验。**不是放行判据**（放行判据 = fetch_order 按单归因）。
            pos_before = self._read_position_amt(
                target_symbol, side, target_b_data.get('is_hedge_mode', False))
            if pos_before is None:
                # B-09（你已批准）：Fail-Closed 不发单 + critical。
                # 阻断的是【自动平仓】，不是【人工平仓】；不发单不造成资金损失。
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓中止：无法读取实际持仓敞口，"
                    f"无法确定安全平仓数量（Fail-Closed，未发单）。\n"
                    f"🆔 批次: `{batch_id}`\n⚠️ 请人工在交易所核对并平仓！",
                    level='critical')
                raise RuntimeError("平仓前读取持仓敞口失败（Fail-Closed：不发出平仓单）")

            # 🔥 归因守卫（§一 v5 修正后）：sum_all **含本批次**，
            # 因此"target 已 close_phase=1 后被自己排除"的退化不再可能。
            close_amount, _amt_detail = self._close_amount_guard(
                target_symbol, side, target_b_data.get('is_hedge_mode', False),
                current_filled_amount, batch_id)
            if not close_amount:
                # 归因冲突 / 读取失败 / 同方向在途：绝不猜归属，转人工 reconcile
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓中止（归因守卫）：{_amt_detail}\n"
                    f"🆔 批次: `{batch_id}`\n"
                    f"⚠️ 账本与交易所可能已漂移，请先 reconcile 再人工处置！",
                    level='critical')
                raise RuntimeError(f"平仓数量守卫拦截（{_amt_detail}）")
            print(f"  └─ {_amt_detail}")

            # 🔥 修复漏洞1：先市价平仓，成功后再撤 SL/TP
            close_side = 'sell' if side == 'BUY' else 'buy'
            # 🔥 A 修复（2026-08-29 -4061 事故）：与限价平仓（L7578-7582）共用
            # params_base 派生；双向持仓 → positionSide，单向 → reduceOnly，
            # 不同时塞两个参数。
            order_params = target_b_data['params_base'].copy()
            if target_b_data.get('is_hedge_mode', False):
                order_params['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'
            else:
                order_params['reduceOnly'] = True

            order = self._safe_api_call(
                self.exchange.create_order,
                symbol=target_symbol,
                type='MARKET',
                side=close_side,
                amount=close_amount,
                params=order_params,
                retries=1
            )
            # ⚠️ 铁律（§一）：仅表示【订单已创建】，置 True 后**绝不改回 False**。
            # 回滚资格由六态判据决定，不再操作本标志。
            close_order_placed = True

            close_order_id = order.get('id') if isinstance(order, dict) else None
            if not close_order_id:
                # 拿不到 id 就无法按单归因 → 绝不放行撤 SL/TP（UNKNOWN 处置）
                raise RuntimeError("平仓单已提交但未返回订单 ID，无法按单确认成交")

            # 🆕 平仓确认·第 2 步（六态）：fetch_order(order_id) 按单归因。
            #   CONFIRMED_FULL / TERMINAL_ZERO / PARTIAL / PENDING / UNKNOWN /
            #   NOT_CONFIRMED —— 只有前两者改变流程走向，其余一律不回滚。
            _verdict, _detail, _filled = self._confirm_close_filled(
                target_symbol, side, target_b_data.get('is_hedge_mode', False),
                close_order_id, close_amount, pos_before)

            if _verdict == 'CONFIRMED_FULL':
                close_position_confirmed = True
                # （§五）：结算数量以确认后的成交事实为准，不再用台账名义量
                confirmed_filled_amount = float(_filled or close_amount)
            elif _verdict == 'TERMINAL_ZERO':
                # 唯一可回滚状态（canceled/expired/rejected + **权威 filled 明确
                # 存在且 == 0**）。回滚 = close_op_id CAS 原子操作（改动 3v6-4），
                # 不再碰 close_order_placed，也不再依赖锁外旧快照。
                _rb_ok, _rb_why = self._rollback_close_request_if_current(
                    target_symbol, batch_id, close_op_id)
                if _rb_ok:
                    print(f"  └─ 🔄 平仓单未成交，已原子回滚（{_rb_why}），"
                          f"批次回 ACTIVE，SL/TP 继续在位保护")
                    self.send_tg_notification(
                        f"ℹ️ [程序撤单] 市价平仓单未成交（{_detail}），"
                        f"已原子回滚，批次回 ACTIVE。\n🆔 批次: `{batch_id}`")
                    # 直接 return：不进 except（那里会因 close_order_placed=True
                    # 走不回滚+critical——但本分支回滚已成功，无需双报）
                    return False, f"❌ 市价平仓未成交（已回滚）: {_detail}"
                # CAS 拒绝 = 状态已被其他操作接管 → 绝不强行覆盖，走 critical
                raise RuntimeError(
                    f"平仓单未成交且回滚被拒绝（{_rb_why}），转人工处置")
            else:
                # PARTIAL / PENDING / UNKNOWN / NOT_CONFIRMED —— 一律**不回滚**：
                #   PARTIAL       仓位已真实变化，回滚 = 伪装"没平过"
                #   PENDING       订单还活着，回滚后它再成交就无人管辖
                #   UNKNOWN       查询失败 / filled 不可判定（UNKNOWN ≠ EMPTY）
                #   NOT_CONFIRMED create 有 ID 但 fetch 不到 ≠ 没成交
                # → 保持 close_phase=1 + close_reason='market_confirm_unknown'，
                #   由冻结告警（改动 4）+ 本函数 except 的 critical 通道兜住。
                try:
                    self.save_batch_state(target_symbol, batch_id,
                                          {'close_reason': 'market_confirm_unknown'})
                except Exception:
                    pass
                raise RuntimeError(
                    f"市价平仓单结果未确认（{_verdict}）：{_detail}。"
                    f"不回滚，保持冻结等人工处置")

            # 🔥 v6（你的 §二）：**先撤未成交 ENTRY 并逐 ID 验证**，通过后才撤保护单。
            # v5 把这段放在撤 TP/SL 之后，形成这条事故链：
            #     MARKET 平掉 0.001 → 尚未撤的 ENTRY 恰好成交 0.001
            #     → 又产生 LONG 0.001 → 先撤 TP → 先撤 SL
            #     → 才 verify ENTRY 发现「已成交」→ raise → 冻结
            #     → 批次冻结，但仓位已无 SL/TP（**裸仓**）。
            # gate 在前时，同一场景的后果是：冻结 + **SL/TP 仍在位** + critical。
            # 这也与你已批准的甲方案一致：市价 = 平仓确认 → 撤 ENTRY → 验证 ENTRY。
            #
            # 🔥 v5（§四）：**返回值必须成为 clear gate** —— 忽略返回值的话，
            # helper 正确识别出的 UNKNOWN 会被 legacy converge 的
            # `fetch_open_orders(...) or []` 从后门变回 EMPTY → 继续生成 proof → clear。
            _entries_ok = self._cancel_and_verify_entry_orders(
                target_symbol, batch_id, target_b_data, last_filled_count)
            if not _entries_ok:
                # 🛡️ 本段已前移 → **SL/TP 仍在位**，仓位保护没有丢失。
                # 不进 except：那里是通用的「结算异常」文案，无法表达
                # 「保护单还在、需要人工处理残留 ENTRY」这个具体事实，
                # 且会因 close_order_placed=True 与此处双报 critical。
                # 🔒 v6.1（交叉审核修正2 + 自审 N-2）：gate 失败必须把 close_reason
                # 切成异常态；返回值必须接收——若 reason 切换本身写盘失败
                # （双重故障），critical 文案里如实追加，绝不静默退回 fail-silent。
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'market_entry_unknown')
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓已成交，但 ENTRY 收敛未确认！\n"
                    f"🆔 批次: `{batch_id}`\n"
                    f"🛡️ SL/TP **已保留未撤**，仓位仍有保护\n"
                    f"🚫 批次保持冻结（close_phase=1），本轮禁止进入 clear\n"
                    f"⚠️ 请立即人工核对残留开仓单与持仓！"
                    + ('' if _rs_ok else
                       f"\n⚠️ close_reason 切换失败（{_rs_why}），"
                       "冻结告警可能不再周期触发"),
                    level='critical')
                return False, ("❌ 市价平仓已成交但 ENTRY 收敛未确认"
                               "（SL/TP 保留，批次冻结待人工处置）")

            # 仓位已按单确认成交 **且 ENTRY 已确认清零** — 现在才安全撤销保护单
            #
            # 🔑 v6 自查补齐：这两个 ID **必须取自 `_txn_vars`（= claimed 快照），
            # 不能取自入口那份 `target_b_data`**。事故面（见改动 1.5 的 10 字段清单）：
            #     /close 入口读 current_sl_id = SL_1
            #     监控线程滚动止损/保本移 SL → current_sl_id = SL_2 并落盘
            #     BEGIN claim 的是 SL_2
            #     若此处仍撤 SL_1 → 它早被监控线程撤掉 → cancel 抛 OrderNotFound
            #     → `except Exception: pass` 静默吞掉 → **SL_2 成为孤儿单**
            #     → clear_batch_state 抹掉批次 → 永久无主。
            # 用 `_txn_vars[...]` 把来源写在**使用点**，不再依赖
            # 「target_b_data 恰好已被换成 _claimed」这一行赋值。
            if _txn_vars.get('tp_order_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, _txn_vars['tp_order_id'], target_symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销止盈单: {_txn_vars['tp_order_id']}")
                except Exception:
                    pass

            if _txn_vars.get('current_sl_id'):
                try:
                    self._safe_api_call(self.exchange.cancel_order, _txn_vars['current_sl_id'], target_symbol,
                                        params={'stop': True})
                    print(f"  └─ 已撤销止损单: {_txn_vars['current_sl_id']}")
                except Exception:
                    pass

        except Exception as e:
            # P0 Batch A（回滚收紧）：平仓单已创建成功后的异常 = 结算/簿记失败，
            # 绝不回滚 close_phase/flags——否则"平仓后失败误回滚"会让冻结解除、
            # 保护单复活。
            # ⚠️ v5（§七）：本块是 L7115-7143 的**完整原文 + CAS 替换**，
            # 不再使用 `...` 占位（占位块不得声称"可直接套用"）。
            if close_order_placed:
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓单已发出但后续结算异常（未回滚关闭标记）！\n"
                    f"🆔 批次: {batch_id}\n💡 原因: {str(e)[:150]}\n"
                    f"⚠️ 请立即人工核对持仓与挂单！",
                    level='critical')
                return False, f"❌ 市价平仓结算异常（平仓单已创建，close_phase 保持）: {e}"
            # 🔥 修复漏洞1b：失败回滚 —— 改为 CAS 原子回滚（§二）
            try:
                _rb_ok, _rb_why = self._rollback_close_request_if_current(
                    target_symbol, batch_id, close_op_id)
            except Exception as _rb_err:
                _rb_ok, _rb_why = False, f'CAS 调用异常（{_rb_err}）'
            if _rb_ok:
                print(f"  └─ 🔄 平仓失败回滚：CAS 原子回滚成功（{_rb_why}），"
                      f"已清除 is_programmatic_cancel/pending_close/close_phase，监控线程恢复保护")
            else:
                print(f"  └─ ⚠️ 回滚被拒绝: {_rb_why}（状态已被其他操作接管，需人工检查）")
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓失败且回滚被拒绝！\n批次: `{batch_id}`\n"
                    f"原因: {_rb_why}\n请立即检查仓位是否仍有 SL 保护！",
                    level='critical')
            return False, f"❌ 市价平仓失败: {e}"
```


#### 说明

**（1）为什么 `params_base.copy()` 是安全的** —— 限价平仓 L7578 已在用同一份
`params_base.copy()` 且 8-29 16:30 实盘成功；`workingType` / `leverage` 对 MARKET 单
是无关参数。显式 `positionSide` 赋值保留（防账本 `is_hedge_mode` 与 `params_base`
写入时点不一致时静默退化）。

**（2）不加 try/except -4061 重试** —— 确定性参数契约错误，修参数不修重试。

**（3）撤 ENTRY 的插入点（v6 修正）**：在 `_confirm_close_filled` 判 CONFIRMED_FULL
**之后**、撤 SL/TP **之前**。v5 曾把它放在撤 SL/TP 之后，被你 §二 指出会裸仓——
已前移。diff 净效果 = 删掉开头 9 行（原平仓前的 ENTRY 撤单循环）+ 在确认后插入
**撤 ENTRY → verify ENTRY → 撤 TP/SL** 这一整段。

**（3b）为什么 ENTRY 撤单不能留在平仓前（v4 的原位置）**：留在平仓前，当平仓单
TERMINAL_ZERO 需要回滚时，ENTRY 已被撤掉且**不可恢复**（撤销是交易所侧既成事实）
→ 批次回滚到 ACTIVE 却永远等不到后续层成交。所以正确位置只能是「确认成交之后」，
而「确认成交之后」内部又必须早于撤 SL/TP —— 这正是 v6 的落点。

**（4）异常通道保持不变** —— 抛 `RuntimeError` 复用既有 L7114 分水岭：
`close_order_placed=True` → 不回滚 + 🚨critical + SL/TP 保持不动。
v4 唯一的例外是 TERMINAL_ZERO 回滚成功分支**直接 return**（回滚已完成，
不进 except，避免双报 critical）。

**（5）六态与回滚资格对照**（v3 → v4 的关键变化）：

| 订单事实 | v3 判定 | v3 后果 | v4 判定 | v4 后果 |
|---|---|---|---|---|
| closed + filled 达标 | confirmed | 放行 | CONFIRMED_FULL | 放行 ✅ |
| open ×3 | **not_filled** | **回滚** 🚨 | PENDING | 不回滚 + critical |
| 部分成交 filled=0.0005 | **not_filled** | **回滚** 🚨 | PARTIAL | 不回滚 + critical |
| canceled/expired + filled=0 | not_filled | 回滚 | **TERMINAL_ZERO** | **唯一可回滚**（CAS） |
| create 有 ID 但 fetch 不到 | **not_filled** | **回滚** 🚨 | NOT_CONFIRMED | 不回滚 + critical |
| 查询异常 ×3 | unknown | 不回滚 | UNKNOWN | 不回滚 + critical ✅ |

v3 的三行 🚨 就是 ChatGPT 终审 §一 的指控：S7（partial）判 not_filled 后回滚，
交易所事实（已成交 0.0005）被伪装成「这次 close 没发生」。测试 C2/C3/C6 +
负向对照 N-* 复现了这条错误链。

#### 改动 1c（v6 新增）：BEGIN 之后以 claimed snapshot 重算 transaction 变量

**你的 §一 原话**：

> BEGIN 已经是新状态，但交易事务参数仍然可能来自旧状态。
> 这个其实是 atomic BEGIN 的最后半步：**claim 与 transaction snapshot 必须绑定。**

**我的源码核实（全部成立）**：

| 声明 | 位置 | 核实 |
|---|---|---|
入口先算 transaction 变量 | 市价 **L6964-6967** / 限价 **L7491-7494** | ✅ `last_filled_count` / `target_amounts` / `current_filled_amount` / `side` 都在入口由 `target_b_data` 派生 |
BEGIN（原 flags 写入）更晚 | 市价 **L6980-6984** / 限价 **L7508-7512** | ✅ 晚 16 行 |
监控线程会更新 `last_filled_count` 并落盘 | **L6226 / L6245 / L6255** | ✅ `last_filled_count = batch_filled_count` → `batch_state_data.update({'last_filled_count': ...})` → `save_batch_state(...)`，同一轮还更新 `target_amounts` / `filled_details` / `total_entry_fee` |

构造出的真实窗口（你的例子，我按生产行号重述）：

```text
T1 /close 入口 L6964 : last_filled_count=1, current_filled_amount=0.001
T2 监控线程 L6226    : 新一层 ENTRY 成交 → last_filled_count=2 → L6255 落盘
T1 BEGIN   L6980     : 锁内读到最新状态 → phase==0 → claim 成功
T1 下单    L7024     : 若仍用旧 current_filled_amount → MARKET 只平 0.001
                       （实际持仓 0.002， residual 0.001 无保护）
```

#### BEFORE（市价 L6964-6984，源码原文）

```python-frag
        last_filled_count = target_b_data.get('last_filled_count', 0)
        target_amounts = target_b_data.get('target_amounts', [])
        current_filled_amount = sum(target_amounts[:last_filled_count])
        side = target_b_data.get('side', 'BUY')

        if current_filled_amount <= 0:
            return False, f"⚠️ 批次 `{batch_id}` 尚未建仓，无需平仓"

        # 🔥 修复漏洞1b：先获取市价，成功后再设 flags（原代码先设 flags 再取 ticker，
        # ticker 失败时 flags 已落盘 → 监控线程误判"程序平仓中"不恢复 SL）
        try:
            ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception as e:
            return False, f"❌ 获取市价失败: {e}"

        # 标记程序主动平仓，监控线程将静默退出（ticker 已成功，安全设 flags）
        target_b_data['is_programmatic_cancel'] = True
        target_b_data['pending_close'] = True
        target_b_data['close_phase'] = 1  # P0 Batch A：CLOSE_REQUESTED（唯一权威，P0-1）
        self.save_batch_state(target_symbol, batch_id, target_b_data)
```

#### AFTER（市价）

```python
        # ⚠️ v6（你的 §一）：入口**不再派生任何 transaction 变量**。
        # 原 L6964-6967 在入口就算好 last_filled_count / current_filled_amount，
        # 但监控线程（L6226/L6245/L6255）会在其间更新它们并落盘 —— BEGIN 声称的
        # 是最新状态，下单参数却来自 BEGIN 之前的旧快照。claim 与 transaction
        # 必须绑定，所以全部变量改为 BEGIN 之后、用 claimed 快照派生。
        # 🔥 修复漏洞1b（保留）：先取市价，成功后再动批次状态。
        try:
            ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception as e:
            return False, f"❌ 获取市价失败: {e}"

        # 🆕 atomic BEGIN（改动 3v6）：claim + 落盘 + **返回 claimed 快照**
        _begin_ok, close_op_id, _begin_why, _claimed = self._begin_close_request_if_active(
            target_symbol, batch_id, 'market_confirming')
        if not _begin_ok:
            # 未取得所有权 → **绝不发出任何交易所订单**（改动 3v6-1）
            self.send_tg_notification(
                f"🚨【资金安全】市价平仓未启动：未取得平仓事务所有权。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_begin_why}\n"
                f"⚠️ 未发出任何订单，请人工核对批次状态。",
                level='critical')
            return False, f"❌ 市价平仓未启动（{_begin_why}）"

        # 🔑 v6（你的 §一）：以 BEGIN 锁内 claim 的快照为**唯一基线**派生本次
        # transaction 的全部 batch-derived 变量。
        # 必须整套同源：结算段有 `target_amounts[i] * filled_details[i] for i in
        # range(last_filled_count)` —— 若层数用新值而明细用旧值 → IndexError。
        _vars_ok, _txn_vars, _vars_why = self._derive_close_txn_vars(_claimed, batch_id)
        if not _vars_ok:
            # claimed 快照显示无需平仓 / 账本残缺 → 撤销这次 claim 再退出
            _rb_ok, _rb_why = self._rollback_close_request_if_current(
                target_symbol, batch_id, close_op_id)
            self.send_tg_notification(
                f"🚨【资金安全】市价平仓中止：claimed 快照不能用于下单。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_vars_why}\n"
                f"🔄 回滚本次平仓标记: {'成功' if _rb_ok else '失败（' + _rb_why + '）'}\n"
                f"⚠️ 未发出任何订单，请人工核对账本与批次状态。",
                level='critical')
            return False, f"❌ 市价平仓中止（{_vars_why}）"

        target_b_data = _claimed
        last_filled_count = _txn_vars['last_filled_count']
        target_amounts = _txn_vars['target_amounts']
        current_filled_amount = _txn_vars['current_filled_amount']
        side = _txn_vars['side']
```

#### AFTER（限价，同法，仅 `close_reason` 不同）

限价的 BEFORE 是 L7491-7512（结构同构，仅多取 bid/ask），AFTER 完全同法：

```python
        # ⚠️ v6（你的 §一）：入口**不再派生任何 transaction 变量**。
        # 原 L6964-6967 在入口就算好 last_filled_count / current_filled_amount，
        # 但监控线程（L6226/L6245/L6255）会在其间更新它们并落盘 —— BEGIN 声称的
        # 是最新状态，下单参数却来自 BEGIN 之前的旧快照。claim 与 transaction
        # 必须绑定，所以全部变量改为 BEGIN 之后、用 claimed 快照派生。
        # 🔥 修复漏洞1b（保留）：先取市价，成功后再动批次状态。
        try:
            ticker = self._safe_api_call(self.exchange.fetch_ticker, target_symbol)
            current_price = float(ticker.get('last') or ticker.get('close') or 0.0)
        except Exception as e:
            return False, f"❌ 获取市价失败: {e}"

        # 🆕 atomic BEGIN（改动 3v6）：claim + 落盘 + **返回 claimed 快照**
        _begin_ok, close_op_id, _begin_why, _claimed = self._begin_close_request_if_active(
            target_symbol, batch_id, 'limit_pending_normal')
        if not _begin_ok:
            # 未取得所有权 → **绝不发出任何交易所订单**（改动 3v6-1）
            self.send_tg_notification(
                f"🚨【资金安全】限价平仓未启动：未取得平仓事务所有权。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_begin_why}\n"
                f"⚠️ 未发出任何订单，请人工核对批次状态。",
                level='critical')
            return False, f"❌ 限价平仓未启动（{_begin_why}）"

        # 🔑 v6（你的 §一）：以 BEGIN 锁内 claim 的快照为**唯一基线**派生本次
        # transaction 的全部 batch-derived 变量。
        # 必须整套同源：结算段有 `target_amounts[i] * filled_details[i] for i in
        # range(last_filled_count)` —— 若层数用新值而明细用旧值 → IndexError。
        _vars_ok, _txn_vars, _vars_why = self._derive_close_txn_vars(_claimed, batch_id)
        if not _vars_ok:
            # claimed 快照显示无需平仓 / 账本残缺 → 撤销这次 claim 再退出
            _rb_ok, _rb_why = self._rollback_close_request_if_current(
                target_symbol, batch_id, close_op_id)
            self.send_tg_notification(
                f"🚨【资金安全】限价平仓中止：claimed 快照不能用于下单。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_vars_why}\n"
                f"🔄 回滚本次平仓标记: {'成功' if _rb_ok else '失败（' + _rb_why + '）'}\n"
                f"⚠️ 未发出任何订单，请人工核对账本与批次状态。",
                level='critical')
            return False, f"❌ 限价平仓中止（{_vars_why}）"

        target_b_data = _claimed
        last_filled_count = _txn_vars['last_filled_count']
        target_amounts = _txn_vars['target_amounts']
        current_filled_amount = _txn_vars['current_filled_amount']
        side = _txn_vars['side']
```

**为什么 `filled_details` / `total_entry_fee` 不在这里重取**：紧随其后的
L6987-6990（市价）/ L7528-7531（限价）会从 `target_b_data` 取这两个字段，
而 `target_b_data` 已被替换为 `_claimed`（BEGIN 时刻的快照）→ 自动取到新值，
无需改动。同时 `_derive_close_txn_vars` **已经校验过**
`len(filled_details) >= last_filled_count`，因此那段求和不会 IndexError。
（v5 若直接把新层数配旧明细，就会在这里崩溃 —— 这也是必须「整套同源」的原因。）

### 🔑 自查补齐：batch-derived 字段的完整清单是 **10 个 raw 字段 + 1 个派生量 = 11 个**，不是 4 个

你 §一 点名了 4 个（`last_filled_count` / `target_amounts` / `current_filled_amount` /
`side`）。我照着做的时候把它当成了全集，**这是不够的**——我自己复核生产源码后发现，
监控线程那次落盘是**一整个 update 块**（`trader_260725.py` L6231-6254），一次性写入：

```python-frag
batch_state_data.update({
    'is_active': True,            'batch_id': batch_id,
    'symbol': symbol,             'side': side,
    'entry_orders': entry_orders,          # ← L6236
    'stop_steps': stop_steps,     'take_profit_price': take_profit_price,
    'current_sl_id': current_sl_id,        # ← L6239
    'tp_order_id': tp_order_id,            # ← L6240
    'batch_total_amount': batch_total_amount,
    'target_amounts': target_amounts,      # ← L6242
    'params_base': params_base,            # ← L6243
    'is_hedge_mode': is_hedge_mode,        # ← L6244
    'last_filled_count': last_filled_count,        # ← L6245
    'filled_details': filled_details,              # ← L6246
    'total_entry_fee': total_entry_fee,            # ← L6247
    ...
})
```

也就是说，**你引用的那个窗口（L6226 / L6245 / L6255）实际覆盖 8 个字段**，
不是 4 个。加上入口 L6967 读的 `side` 与「撤哪些单」的 3 个字段，本次 close
transaction 依赖的 batch-derived **raw 字段共 10 个**；再加派生量
`current_filled_amount`，`_txn_vars` 实际返回 **11 个键**（v6.1 措辞修正：
raw 与 derived 分开数，且 `tp_order_id` / `current_sl_id` 是两个独立字段，
不再合并计数）：

| # | 字段 | 用途 | 漏掉会怎样 |
|---|---|---|---|
| 1 | `last_filled_count` | 算平多少 / 撤哪些 ENTRY | 少平（你 §一 的决定性例子） |
| 2 | `target_amounts` | 同上 | 同上 |
| 3 | `current_filled_amount`（派生） | 下单量 | 同上 |
| 4 | `filled_details` | 平均成本 / PnL | 层数与明细不同源 → **IndexError** |
| 5 | `total_entry_fee` | PnL | 手续费失真 |
| 6 | `side` | 平仓方向 / positionSide | **反向开仓** |
| 7 | `params_base` | 平仓 params 基线 | 回到 -4061 |
| 8 | `is_hedge_mode` | 决定 positionSide / reduceOnly | 同上 |
| 9 | **`entry_orders`** | 撤未成交开仓单 | 残留 ENTRY |
| 10 | **`tp_order_id`** | 撤止盈单 | **孤儿 TP**（下方事故链） |
| 11 | **`current_sl_id`** | 撤止损单 | **孤儿 SL**（下方事故链） |

（11 行 = 10 个 raw 字段 + 第 3 行派生量 `current_filled_amount`。v6 稿曾把
`tp_order_id`/`current_sl_id` 合并计数为「10 个字段」，v6.1 按你的修正分开数。）

**第 10 项是我自查才发现的、你没点名的一条**，也是最危险的一条：

```text
/close 入口读   current_sl_id = SL_1
监控线程        滚动止损 / 保本移 SL → current_sl_id = SL_2，落盘
BEGIN          claim 到的是 SL_2（锁内最新）
撤保护单        若仍用入口的 SL_1
  → SL_1 早已被监控线程撤掉
  → cancel 抛 OrderNotFound
  → `except Exception: pass` 静默吞掉   ← 生产现有代码就是 pass
  → SL_2 成为孤儿单
  → clear_batch_state 抹掉批次
  → 永久无主的止损单，无人管辖
```

这正是我们这几轮一直在消灭的「孤儿保护单」类型，而它**不在你 §一 的 4 个字段里**。

**v6 的两层处置**：
1. `_derive_close_txn_vars()` 的返回值**补齐这 3 个字段**（`entry_orders` /
   `tp_order_id` / `current_sl_id`），使「11 个键同源」成为**可机器校验的契约**
   （v6.1：D6 已锁 exact key set，多一个少一个都报警），而不是一句口号。
2. 市价 AFTER 撤 TP/SL 处**改用 `_txn_vars[...]`**，把来源写在**使用点**——
   不再依赖「`target_b_data` 恰好已被换成 `_claimed`」这一行赋值。
   （`entry_orders` 仍随 `target_b_data` 传给 `_cancel_and_verify_entry_orders`，
   因为该 helper 的签名就是收 `b_data`；而 `target_b_data` 此刻已是 `_claimed`。）

**一个已知的次要副作用（如实记录）**：`target_b_data` 换成 claimed 快照后，
限价路径 L7599-7602 的 `save_batch_state(target_symbol, batch_id, target_b_data)`
写回的是 **BEGIN 时刻**的批次状态而非入口时刻的。二者差异极小（BEGIN 之后监控
线程因 `pending_close=True` 已退出），且写回更新的一份更接近真实。v5 写入口旧
快照同样有这个性质，不是 v6 新引入。

#### 改动 1d（v6.1 新增，你的 P0-3）：限价路径「尝试撤 ENTRY」升级为撤销确认 gate

**先更正我的错误表述**：v6 稿头与 §六 我都写了「限价路径生产 L7541-7573 本就是
撤 ENTRY(gate) → 撤 TP → 挂 LIMIT」——**这是错的**。生产实际代码：

#### BEFORE（源码原文，L7540-7549）

```python-frag
        try:
            # 先撤销所有未成交的开仓条件单
            entry_orders = target_b_data.get('entry_orders', [])
            for idx, order_id in enumerate(entry_orders):
                if idx >= last_filled_count:
                    try:
                        self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol, params={'stop': True})
                        print(f"  └─ 已撤销开仓挂单: {order_id}")
                    except Exception:
                        pass
```

没有 `fetch_open_orders` 快照、没有逐 ID `fetch_order`、没有 bool gate ——
撤单失败被 `except: pass` 静默吞掉，随后**直接撤 TP、直接挂 LIMIT**。你指出的
风险链完全成立：ENTRY cancel 实际失败 → 静默 pass → 撤 TP → 挂 LIMIT 0.001 →
LIMIT 等数小时 → 残留 ENTRY 期间成交 +0.001 → 实际仓位 0.002 → LIMIT 只平
0.001 → 最终残留 0.001。

**甲方案顺序不变**（ENTRY 仍在 TP/LIMIT 之前），只是把「先撤」从动作升级为
事实确认——复用市价同款 helper：

替换的是 L7541-7549 整个 for 循环（外层 `try:` 与其 `except` 均为生产既有、零改动）：

#### AFTER

```python
            # 🆕 v6.1（你的 P0-3）：限价路径「尝试撤 ENTRY」升级为撤销确认 gate。
            # 生产旧代码是 try: cancel / except: pass —— 撤单失败被静默吞掉，
            # 随后直接撤 TP、挂 LIMIT：残留 ENTRY 在限价挂单期间成交 → 最终少平。
            # gate 复用市价同款 helper（甲方案顺序不变：ENTRY 仍在 TP/LIMIT 之前）。
            _entries_ok = self._cancel_and_verify_entry_orders(
                target_symbol, batch_id, target_b_data, last_filled_count)
            if not _entries_ok:
                # 🛡️ 与市价路径的关键区别：此时**平仓单还没挂**，仓位零变化 ——
                # 优先 CAS 回滚让监控恢复（TP/SL 全程未动，SL 本就不撤）；
                # 回滚失败才落异常 reason 冻结（fail-noisy，白名单含
                # limit_entry_unknown）。已撤掉的 ENTRY 不恢复（用户本就要平仓），
                # 监控不补挂 ENTRY，批次以既有仓位继续受 TP/SL 保护。
                _rb_ok, _rb_why = self._rollback_close_request_if_current(
                    target_symbol, batch_id, close_op_id)
                # 🔒 自审 N-2：返回值必须接收——回滚失败且 reason 切换本身也
                # 写盘失败时（双重故障），critical 文案里如实追加。
                _rs_ok, _rs_why = True, ''
                if not _rb_ok:
                    _rs_ok, _rs_why = self._set_close_reason_if_current(
                        target_symbol, batch_id, close_op_id, 'limit_entry_unknown')
                self.send_tg_notification(
                    f"🚨【资金安全】限价平仓中止：ENTRY 收敛未确认！\n"
                    f"🆔 批次: `{batch_id}`\n"
                    f"🛡️ 未挂平仓单，TP/SL 全程未动，仓位保护完整\n"
                    f"🔄 回滚本次平仓标记: {'成功（监控已恢复）' if _rb_ok else '失败（' + _rb_why + '）'}\n"
                    f"⚠️ 请人工核对残留开仓单后再重新发起平仓！"
                    + ('' if _rs_ok else
                       f"\n⚠️ close_reason 切换失败（{_rs_why}），"
                       "冻结告警可能不再周期触发"),
                    level='critical')
                return False, ("❌ 限价平仓中止：ENTRY 收敛未确认"
                               "（未挂平仓单，TP/SL 保留）")

            # gate 通过 → 才允许进入生产的「撤 TP → 挂 LIMIT」（L7551 起，零改动）
```

gate 通过后才落到生产的撤 TP（L7551-7573）与挂 LIMIT（L7584-7593），那两段
**零改动**。注意 `_rollback_close_request_if_current` 在 v6.1 已检查 persist
返回值（P0-1），回滚失败会如实反映在 `_rb_ok` 上。

#### 改动 1b：`confirmed_filled_amount` 贯穿结算（ChatGPT 终审 §五）

下单数量已是 `close_amount`，但结算 6 处仍用台账名义量 `current_filled_amount`
——S9 场景（台账 0.001、实际只平 0.0005）会按 0.001 记账。v4 以确认后的
`fetch_order` 成交事实为准：

| 行号 | BEFORE（生产源码原文） | AFTER |
|---|---|---|
| L7052 | `actual_gross_pnl = (actual_price - avg_price) * current_filled_amount` | `... * confirmed_filled_amount` |
| L7054 | `actual_gross_pnl = (avg_price - actual_price) * current_filled_amount` | `... * confirmed_filled_amount` |
| L7056 | `actual_exit_fee = actual_price * current_filled_amount * TAKER_FEE_RATE` | `... * confirmed_filled_amount * ...` |
| L7060 | `capital_base = avg_price * current_filled_amount if current_filled_amount > 0 else 1` | `capital_base = avg_price * confirmed_filled_amount if confirmed_filled_amount > 0 else 1` |
| L7093 | `f"📊 **持仓**：\`{current_filled_amount}\` ({last_filled_count}层)\n"` | `f"📊 **持仓**：\`{confirmed_filled_amount}\` (实际成交)\n"` |
| L7109 | `self._record_realized_pnl(batch_id, target_symbol, side, current_filled_amount,` | `self._record_realized_pnl(batch_id, target_symbol, side, confirmed_filled_amount,` |

边界说明：`CONFIRMED_FULL` 蕴含 `filled >= eff_expected`，故
`confirmed_filled_amount >= close_amount` 只在 `pos_before < 台账`（B-03 单批次
min 域）时可能出现——此时按实际成交记账正是修复目的。

#### 1b 追加（v5，你的 §八-2）：账本漂移时不得把 PnL 展示成完整精确值

若 `confirmed_filled_amount < current_filled_amount`（台账 0.001、实际只平 0.0005），
说明此前发生过**未被跟踪的减仓**（手动减仓 / ADL / 他方平仓）。此时仅凭最后这
0.0005 的成交**无法恢复此前那 0.0005 的真实退出价与手续费**，按整批计算出的
`net_pnl` 不是该批次的完整已实现盈亏。

```python
        # 紧跟 confirmed_filled_amount 赋值之后（L7066 附近）
        _pnl_partial = confirmed_filled_amount < current_filled_amount - 1e-12
        _pnl_note = ''
        if _pnl_partial:
            _pnl_note = (f"\n\n⚠️ **[账务不完整]** 台账 {current_filled_amount}、"
                         f"本次实际成交 {confirmed_filled_amount}，此前存在未被跟踪的减仓。\n"
                         f"本条盈亏**仅覆盖本次成交部分**，非该批次完整已实现盈亏"
                         f"（prior_reduction_unknown）。")
        # 追加到 result_msg 构造之后（L7104 之后）
        result_msg += _pnl_note
        # 记账侧同时打标（_record_realized_pnl 的 pnl_partial 参数；
        #   生产签名改动 diff 见下方「1b 追加 2」——漏改定义 = 结算阶段 TypeError）
        self._record_realized_pnl(batch_id, target_symbol, side, confirmed_filled_amount,
                                  avg_price, actual_price, actual_net_pnl, "市价平仓",
                                  pnl_partial=_pnl_partial)
```

#### 1b 追加 2：`_record_realized_pnl` 生产签名改动 diff（🔴 v6.1 自查抓出的**落地阻断项**）

> **这是 v6.1 自审（`check_doc_helper_calls.py`【4】）抓出的 P0 遗漏**：v6.1 前稿
> 在上面调用处传了 `pnl_partial=`，却**通篇没有给出该函数的签名改动**——
> 全文 `def _record_realized_pnl` 命中 **0 次**。照此落地，结算阶段会抛
> `TypeError: _record_realized_pnl() got an unexpected keyword argument 'pnl_partial'`，
> 而且发生在**平仓已经成交之后**：结算/clear 被打断，批次停在 `close_phase=1`
> → 监控线程冻结、跳过全部 SL/TP 维护，**与本次事故的最终症状（B 类：close_phase
> 卡死 → 监控冻结）完全一致**。这正是「只给调用、不给定义」的代价。

**调用点实证**（`grep -n "_record_realized_pnl" trader_260725.py`，磁盘 HEAD=e953d79）：

| 行号 | 性质 | 现状 |
|---|---|---|
| L678 | 定义 | 8 个必填位置参数（batch_id/symbol/side/amount/avg_price/exit_price/net_pnl/mode），**无 `pnl_partial`** |
| L5408 | 调用（止损触发） | 8 个位置参数 |
| L5579 | 调用（止盈触发） | 8 个位置参数 |
| L7109 | 调用（市价平仓，**本改动 1b 的目标**） | 8 个位置参数 |
| L7818 | 调用（限价平仓） | 8 个位置参数 |

即：生产共 **4 个调用点**，本次只改 L7109；**其余 3 处（L5408 / L5579 / L7818）
因新参数带默认值而零影响**。
（前稿写的「其余 5 处调用点」是笔误——4 是调用点合计，不是"其余"；特此更正。）

**AFTER（L678 起整段替换；为便于独立解析已 dedent 到列 0，落地时保持类内 4 空格缩进）**：

```python
def _record_realized_pnl(self, batch_id: str, symbol: str, side: str, amount: float,
                         avg_price: float, exit_price: float, net_pnl: float,
                         mode: str, pnl_partial: bool = False) -> None:
    """记录一笔已实现盈亏到 trade_stats.json（原子写入，失败静默）

    pnl_partial=True —— 本次实际成交**小于台账**（此前存在未被跟踪的减仓：
    手动减仓 / ADL / 他方平仓）。此时 net_pnl 仅覆盖本次成交部分，**不是该
    批次的完整已实现盈亏**；落 `prior_reduction_unknown` 标记供日报/汇总识别。
    """
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        stats_file = os.path.join(base_dir, "trade_stats.json")
        with self._state_lock:
            stats = {}
            if os.path.exists(stats_file):
                try:
                    with open(stats_file, "r", encoding="utf-8") as f:
                        stats = json.load(f)
                except Exception:
                    stats = {}
            record = {
                "time": datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "batch_id": batch_id,
                "symbol": symbol,
                "side": side,
                "amount": round(float(amount), 6),
                "avg_price": round(float(avg_price), 4),
                "exit_price": round(float(exit_price), 4),
                "net_pnl": round(float(net_pnl), 4),
                "mode": mode,
                # 🔴 v6.1 新增（§八-2）：账务不完整标记，默认 False（其余 3 处调用点零影响）
                "prior_reduction_unknown": bool(pnl_partial),
            }
            # ↓↓↓ 以下原样不动：append → 原子写盘（NamedTemporaryFile + os.replace）→ except 兜底
            stats.setdefault("trades", []).append(record)
            with tempfile.NamedTemporaryFile("w", dir=base_dir, delete=False, encoding="utf-8") as tf:
                json.dump(stats, tf, ensure_ascii=False, indent=2)
                temp_name = tf.name
            os.replace(temp_name, stats_file)
    except Exception as e:
        print(f"⚠️ [盈亏记录] 写入失败: {e}")
```

兼容性：只增键不删键，读取方一律 `dict.get` 取用，**旧 trade_stats.json 零影响**
（老记录无该键 → `.get()` 回落 False）。

#### 1b 追加 3：日报汇总侧必须消费该标记（否则标记只是"装饰品"）

只落标记、不改读取方，等于什么都没做 —— 日报 `_send_daily_report`（L862-898）目前
是**无条件累加** `net_pnl`：

```python-frag
            for t in stats.get("trades", []):
                if str(t.get("time", "")).startswith(report_date):
                    today_trades.append(t)
                    total_pnl += float(t.get("net_pnl", 0.0))
```

那样一笔「部分成交记账」的 PnL 会被当成完整批次盈亏累进，正是 §八-2 要禁止的。
故读取侧同步改（**只加提示、不改累加口径**：少记总比把不完整值展示成"精确值"
安全，且被漏记的那部分在任何版本里都不在账本中）：

```python
            # 紧跟 msg 的「📊 昨日平仓: N 笔」那一行之后（L888-889 之后）
            _partial_n = sum(1 for t in today_trades if t.get("prior_reduction_unknown"))
            if _partial_n:
                msg += (f"⚠️ 其中 `{_partial_n}` 笔为**部分成交记账**"
                        f"（此前存在未被跟踪的减仓）\n"
                        f"   其盈亏**仅覆盖已确认成交部分**，累计值不完整\n")
```

**已知残留盲区（披露，本轮不改）**：D-006 的 `_get_today_realized_pnl`（L744-754）
同样无条件累加 `net_pnl`，用于日亏损限额。部分成交记账会让它**低估**当日已实现
亏损。但被漏掉的那部分亏损在任何版本里都不在账本中（App 手动平仓不记账是既有
盲区，属 D-008-1 地盘），故本改动既不改善也不恶化 D-006 的输入。
是否要把 `prior_reduction_unknown` 升级为 D-006 的 Fail-Closed 触发条件，请你裁定；
我的倾向是**不改**——那会让一次已经成功平仓的批次反向卡住后续风控闸门，
收益不抵复杂度。

**定性与你的裁定一致**：这是账务正确性问题，不是本轮资金安全阻断项。TTL/精度：`filled` 来自
交易所权威字段，不做二次裁剪；若 `_filled` 异常为空，回落 `close_amount`
（`float(_filled or close_amount)`）。

### 改动 1.5：新增 helper 全集（v6 重写，v6.1 增至 **11 个**：BEGIN/rollback persist 检查 + derive 三校验 + 新增 `_set_close_reason_if_current`；终审 §一/§二/§三/§四/§五/§六/§八-1 与 v6 §一/§二、v6.1 P0-1/P0-2/修正2 的落点）

完整源码（从 `class _Holder:` 起与磁盘 `送审附件_v6.1/new_helpers_v6.py` 逐字一致，文件头 import 略；ast 隔离测试从实现文件提取执行）：

```python
class _Holder:
    # ══════════════════════════════════════════════════════════════════
    # 2026-08-29 -4061 事故 · helper 全集 v5 → v6
    #   v5（ChatGPT 对 v4 的终审：架构批准、diff 不批准）→ 六态状态机 / CAS 回滚 / coverage 含 target / TERMINAL_ZERO 收紧
    #   v6（ChatGPT 复审 v5：只剩 2 个事务边界 + 1 个语义收紧）→ 见下方 ══ v6 ══ 段
    #
    # ChatGPT 终审原文：「v4 已经解决了 v3 最核心的三个方向错误……但当前还有
    # 4 个上线阻断项」。v5 逐项处置：
    #
    #   P0-1（§二+§三）atomic BEGIN/claim —— 新增 _begin_close_request_if_active：
    #        锁内生成 uuid → 校验 close_phase==0 → 原子占有 → 落盘。
    #        只有 claim 成功的线程才能继续调用交易所。
    #        同时修掉 v4 的 integration bug：close_op_id 在 BEGIN 内生成，
    #        不再出现在 close_phase=1 落盘之后（v4 那样写必然 NameError）。
    #   P0-2（§一）  _survey_same_side_batches 排除 target 导致决定性例子漏过：
    #        真实调用顺序是先写 close_phase=1 再调 guard → target 被自己排除
    #        → sum_all 不含 target → actual == sum_all → 又放行。
    #        v5：target 强制计入 sum_all（coverage 不变量要求含 target），
    #        并新增 blocking_count 单独暴露"其他批次也在关闭中"。
    #   P0-3（§四）  ENTRY helper 的 False 必须成为 clear gate（调用处加 raise，
    #        见文档改动 2）；converge 内的 `or []` 一并清掉（见文档改动 9）。
    #   P0-4（§五）  TERMINAL_ZERO 收紧：filled 缺失/None → UNKNOWN；
    #        closed/filled + filled==0 是矛盾组合 → UNKNOWN，不给回滚资格。
    #   非阻断（§八）_read_position_amt 非 list → None（严格 Fail-Closed）；
    #        PnL 标记 pnl_partial（账本漂移时不得展示为完整精确 PnL）。
    #
    # v4 已批准部分在 v5 全部保留：六态状态机 / close_op_id CAS 原子回滚 /
    # ENTRY 逐 ID 终态验证 / close_reason 分型 / confirmed_filled_amount 贯穿。
    #
    # ══ v6（ChatGPT 复审 v5：只剩 2 个事务边界 + 1 个语义收紧）══
    #   B-1  BEGIN 必须返回它锁内刚 claim 的 batch 副本（第 4 个返回值），
    #        并新增 _derive_close_txn_vars 让调用方以 claimed 快照为唯一基线
    #        重算全部 batch-derived 变量。否则「我 claim 的状态」与
    #        「我按下单的状态」仍是两个状态（生产 L6964-6967 早于 BEGIN，
    #        监控线程 L6226/L6245/L6255 会在其间更新 last_filled_count）。
    #   B-2  ENTRY gate 必须位于撤 SL/TP **之前**（市价路径）。v5 把它放在
    #        撤 TP → 撤 SL 之后 → ENTRY 成交被发现时保护单已撤 → 裸仓冻结。
    #        （helper 侧体现为 docstring 契约；代码块顺序见送审文档改动 1）
    #   B-3  _verify_entry_order_terminal 的 OrderNotFound 从 'gone' 改为
    #        'unknown'：生产 L1992 已写明「订单不存在 = 已撤销/已成交/已过期」，
    #        三种可能里只有一种是安全的，不能据此判定 ENTRY 没成交。
    #
    # ══ v6.1（ChatGPT 交叉审核 v6：3 个 P0 + 2 个修正）══
    #   P0-1 BEGIN / rollback 必须检查 _persist_states() 返回值（生产 L1340
    #        契约 -> bool）。否则锁内 claim 成功但写盘失败 → 磁盘仍 phase=0
    #        → 第二个线程再次 claim → 两张 MARKET。「写盘成功」必须是
    #        「claim 成功」的一部分；rollback 同型（不能谎报 rolled_back）。
    #   P0-2 _derive_close_txn_vars 增加 target_amounts_short 对称校验
    #        （切片不报错，last_filled_count=2 配 [0.001] 会静默少平一半），
    #        附带 side 严格校验（非法值不得默认 BUY → 反向开仓风险）。
    #   P0-3 限价路径「尝试撤 ENTRY（except: pass）」升级为撤销确认 gate
    #        —— 落点在送审文档（新增限价 gate 段），复用本文件
    #        _cancel_and_verify_entry_orders，helper 本身无需改。
    #   修正1 entry_orders 缺失只在「无未成交计划层」时允许归零
    #        （D6b 收窄；否则 missing→[]→pending_ids=[]→gate 恒 True，
    #        又是一个 UNKNOWN→EMPTY）。
    #   修正2 新增第 11 个 helper _set_close_reason_if_current：ENTRY gate
    #        失败必须把 close_reason 切成异常态（CAS 范围 + 写盘检查），
    #        否则批次永远停在 market_confirming，冻结监控只 print 不 critical。
    #
    # ══ v6.1 送审前三路交叉自审（A 源码实证 / B 设计安全 / C 测试有效性）══
    #   F-1（B 路致命）entry_orders_short 误伤 🗑️ 按钮批次：生产
    #        cancel_open_orders（L6896-6897）只截断 entry_orders 到
    #        last_filled_count、不动 target_amounts → 合法状态被永久挡死
    #        （v6.1 新引入回归）。处置：仅 0<len(_eo)==last_filled_count
    #        （🗑️ 精确截断签名）放行，其余 short 情形维持 Fail-Closed。
    #   C-1（C 路存活变异体）_set_close_reason_if_current 的 persist_failed
    #        分支补回归用例（测试 B13/B13b）。
    # ══════════════════════════════════════════════════════════════════

    # ── 原子 BEGIN（v5 P0-1：close transaction 的所有权）──────────────

    def _begin_close_request_if_active(self, symbol: str, batch_id: str,
                                       close_reason: str):
        """原子 BEGIN：取得本次 close transaction 的**唯一所有权**。

        ChatGPT 终审 §二/§三：「CAS 本身写得对，但发生得太晚了……问题发生在
        close 开始阶段」。TG callback 走 `run_in_executor` 起新线程、
        `close_position_market` 入口无 phase==0 检查 → 双击/重复 callback
        可以让两个线程都看到 phase=0、都写 phase=1、都去下单 →
        第二张单可能平到**另一个批次**的仓位。close_op_id CAS 只能在事后
        阻止"谁还能回滚"，阻止不了"两个人都已取得下单资格"。

        本 helper 是完整事务的第一段：
            atomic BEGIN → exchange action → verify → atomic rollback/settle

        语义（全部在 `_state_lock` 内一次性完成，锁内零交易所 API）：
          1. batch 存在且 is_active
          2. close_phase == 0（严格，不允许 1/2/3）
          3. pending_close 为假
          4. 无 settled_by_limit_close 事实（已发生的事实绝不降级）
          5. 🔒 v5 §六：同 symbol + 同方向，**除本批次外**没有任何批次处于
             close_phase>=1 或 pending_close —— 一次只允许一个自动 close
             transaction 在途。理由：正在关闭的其他批次（尤其 limit_pending）
             仓位可能 100% 仍在场，若允许并行，coverage 推理要同时处理多个
             MARKET/LIMIT close，比"禁止并行"复杂且更易错。
          全部通过 → 生成 uuid → 写 close_phase=1/pending_close=True/
          is_programmatic_cancel=True/close_op_id/close_reason → _persist_states

        返回 (ok, close_op_id, reason, snapshot) 四元组：
          ok=True  → snapshot 是**本次 claim 所依据的 batch 副本**
                     （锁内 dict(b)，与落盘内容逐字段一致）。
                     🔑 v6（ChatGPT 终审 §一）：调用方**必须**以这份快照为
                     唯一基线重算本次 transaction 的全部 batch-derived
                     变量（见 _derive_close_txn_vars）。
          ok=False → snapshot 为 None，调用方**立即返回，绝不发出任何
                     交易所订单**。

        ⚠️ 为什么必须把快照返出来（v6 修正的窗口）：
        生产源码顺序是：入口 L6964-6967 先算 last_filled_count /
        current_filled_amount → fetch ticker → 才 BEGIN（L6980-6984）。
        而监控线程在检测到新成交后会更新 last_filled_count 并
        save_batch_state（L6226 / L6245 / L6255）。于是：
            T1 /close 入口: last_filled_count=1, current=0.001
            监控线程: 新 ENTRY 成交 → last_filled_count=2（落盘）
            T1 BEGIN  : 锁内看到最新 → claim 成功
            T1 下单   : 若仍用旧 current_filled_amount → 只平 0.001 ❌
        claim 与 transaction snapshot 必须绑定，BEGIN 才算完整。

        ⚠️ 为什么 op_id 用 uuid4 而不是毫秒时间戳（ChatGPT §三）：
        时间戳恰恰在"双击并发"这种最需要区分 identity 的场景下可能碰撞；
        trader_260725.py L12 已 `import uuid`，无需新增依赖。
        ⚠️ 为什么 op_id 必须在**这里**生成：v4 把生成放在"执行市价平仓"段
        （L7003 附近），但 close_phase=1 的落盘在 L6983（更早）→ 按文档拼
        起来是 NameError。BEGIN 让"生成 + claim + 落盘"成为同一个原子步骤。
        """
        if not close_reason:
            return False, '', 'missing_close_reason（BEGIN 必须带分型原因）', None
        with self._state_lock:
            try:
                all_states = self.load_all_states()  # 锁内重读，禁旧快照（G3b 范式）
            except Exception as e:
                return False, '', f'state_unreadable（{e}）', None
            b = (all_states.get(symbol, {}) or {}).get(batch_id)
            if not isinstance(b, dict):
                return False, '', 'batch_missing', None
            if not b.get('is_active', True):
                return False, '', 'batch_inactive', None
            if int(b.get('close_phase', 0) or 0) != 0:
                return False, '', (f'close_phase_not_zero（disk='
                                   f'{b.get("close_phase")}，已有平仓事务在途）'), None
            if b.get('pending_close'):
                return False, '', 'pending_close_already_set', None
            if b.get('settled_by_limit_close'):
                return False, '', 'settled_fact_present（结算事实已发生，绝不重启）', None

            side = b.get('side') or 'BUY'
            for _bid, _bd in (all_states.get(symbol) or {}).items():
                if _bid == batch_id or not isinstance(_bd, dict):
                    continue
                if (_bd.get('side') or 'BUY') != side:
                    continue
                if int(_bd.get('close_phase', 0) or 0) >= 1 or _bd.get('pending_close'):
                    return False, '', (f'same_side_close_inflight（同方向批次 {_bid} '
                                       f'已有平仓事务在途，一次只允许一个）'), None

            op_id = uuid.uuid4().hex
            b['close_phase'] = 1
            b['pending_close'] = True
            b['is_programmatic_cancel'] = True
            b['close_op_id'] = op_id
            b['close_reason'] = close_reason
            # 🔒 v6.1（ChatGPT 交叉审核 P0-1）：写盘成功必须成为 claim 成功的
            # 一部分。_persist_states 契约是 -> bool（生产 L1340：账本损坏
            # 主动 return False，写盘异常 return False）。若忽略返回值：
            #   T1 锁内 claim OP1 → 写盘失败 → 函数仍 ok=True → T1 去下 MARKET
            #   磁盘实际仍 phase=0 → T2 重读磁盘再次 claim OP2 → 第二张 MARKET
            # 「未取得唯一所有权者绝不发交易所订单」就此击穿。
            if not self._persist_states(all_states):
                return False, '', ('claim_persist_failed（状态写盘失败，'
                                   '视为未取得所有权，绝不发单）'), None
            # 🔑 v6：把刚 claim 的副本交还调用方作为 transaction 唯一基线
            return True, op_id, 'claimed', dict(b)

    # ── transaction 变量派生（v6 B-1）───────────────────────────────

    def _derive_close_txn_vars(self, snapshot: dict, batch_id: str):
        """从 BEGIN 返回的 claimed snapshot 派生本次 close transaction 的变量。

        🔑 v6（ChatGPT 终审 §一）：atomic BEGIN 的最后半步 —— claim 与
        transaction snapshot 必须绑定。调用方 BEGIN 成功后**必须**立即调用
        本函数，并以返回的 vars 覆盖入口算出的同名局部变量。

        为什么必须整套一起换（不能只换 last_filled_count）：
        生产结算段有 `target_amounts[i] * filled_details[i] for i in
        range(last_filled_count)`。若 last_filled_count 用新值（2）而
        filled_details 仍是旧值（长度 1）→ **IndexError**；反之则平均成本
        与实际层数不符 → PnL 失真。因此下列 10 个字段必须同源。

        🔑 10 个 batch-derived 字段的完整清单（v6 自查补齐）：
        ChatGPT 复审 §一 点名了 4 个，但生产监控线程那次落盘是**一整个
        update 块**（trader_260725.py L6231-6254），一次性写入 8 个字段：
            L6236 entry_orders   L6243 params_base   L6246 filled_details
            L6239 current_sl_id  L6244 is_hedge_mode L6247 total_entry_fee
            L6240 tp_order_id    L6245 last_filled_count
        加上由它们派生的 current_filled_amount、以及入口 L6967 读的 side，
        共 **10 个**。其中 entry_orders / tp_order_id / current_sl_id 三个
        最容易漏：它们不参与"算平多少"，而参与"撤哪些单"。
        漏掉 current_sl_id 的具体事故面：
            /close 入口读 current_sl_id = SL_1
            监控线程滚动止损/保本移 SL → current_sl_id = SL_2 并落盘
            BEGIN claim 的是 SL_2，但调用方若仍撤 SL_1
            → SL_1 早已被监控线程撤掉 → cancel 抛 OrderNotFound
            → `except Exception: pass` 静默吞掉 → **SL_2 成为孤儿单**，
              随后 clear_batch_state 抹掉批次 → 永久无主。
        这正是本项目一直在猎杀的「孤儿保护单」类型，所以这三个字段
        **必须进入本函数的返回值**（契约完整、可机器校验），而不是
        依赖调用方"恰好把 target_b_data 整个换成 _claimed"。

        返回 (ok, vars, why)：
          ok=True  → vars 为 dict，**exactly 11 个键**（v6.1 措辞修正：
                     10 个 raw snapshot 字段 + 1 个派生量 current_filled_amount）
          ok=False → 账本残缺/类型异常，调用方必须**回滚 BEGIN 并 Fail-Closed**
                     （此时绝不能带着残缺台账去下单）
        why 取值：'snapshot_not_dict' / 'no_filled_amount'
                 / 'ledger_broken（{异常}）' / 'filled_details_short（缺 N 层成交明细）'
                 / 'target_amounts_short（计划层 < 已成交层）'      ← v6.1 P0-2
                 / 'side_invalid（非 BUY/SELL）'                     ← v6.1 P0-2
                 / 'entry_orders_missing（有未成交层但缺失）'        ← v6.1 D6b 收窄
                 / 'entry_orders_short（长度与已成交层/计划层不一致）' ← v6.1 D6b 收窄
                   （自审 F-1：仅 0<len==last_filled_count 的 🗑️ 截断签名放行）

        ⚠️ 本函数只读，不触碰 self、不调交易所、不取锁。
        """
        if not isinstance(snapshot, dict):
            return False, None, 'snapshot_not_dict'
        try:
            last_filled_count = int(snapshot.get('last_filled_count', 0) or 0)
            target_amounts = snapshot.get('target_amounts', []) or []
            filled_details = snapshot.get('filled_details', []) or []
            current_filled_amount = float(sum(target_amounts[:last_filled_count]))
            total_entry_fee = float(snapshot.get('total_entry_fee', 0.0) or 0.0)
        except (TypeError, ValueError) as e:
            return False, None, f'ledger_broken（{e}）'

        if last_filled_count <= 0 or current_filled_amount <= 0:
            return False, None, 'no_filled_amount（claimed 快照显示无需平仓）'
        if len(filled_details) < last_filled_count:
            return False, None, (f'filled_details_short（缺 '
                                 f'{last_filled_count - len(filled_details)} 层成交明细）')
        # 🔒 v6.1（ChatGPT 交叉审核 P0-2）：对称长度校验。Python 切片不因
        # 长度不足报错 —— last_filled_count=2 而 target_amounts=[0.001] 时
        # sum(target_amounts[:2]) 静默得到 0.001（应平 0.002 只派生 0.001）
        # → 少平 → 按单确认这 0.001 完整成交 → ENTRY gate 认为前两层都已
        # 成交 → gate=True → 撤 TP/SL → clear → 实际残留 LONG 0.001。
        # 这正是「少平仓位 → gate 假通过 → 撤保护」，必须 Fail-Closed。
        if len(target_amounts) < last_filled_count:
            return False, None, (f'target_amounts_short（台账计划层 '
                                 f'{len(target_amounts)} < 已成交层 '
                                 f'{last_filled_count}）')
        # 🔒 v6.1（P0-2 附带）：side 是平仓方向与 positionSide 的来源，
        # 非法值绝不能默认成 BUY（反向开仓风险）。
        side = snapshot.get('side')
        if side not in ('BUY', 'SELL'):
            return False, None, f'side_invalid（{side!r}，必须是 BUY/SELL）'
        # 🔒 v6.1（ChatGPT 交叉审核：D6b 收窄）：entry_orders 缺失只在
        # 「可证明不存在未成交计划层」时才允许归零（全部层已成交、无单可撤，
        # 此时 pending_ids 本就为空）。否则 missing → [] → pending_ids=[]
        # → ENTRY gate 恒 True —— 又是一个 UNKNOWN → EMPTY。
        # 🔒 v6.1（送审前交叉自审 F-1）：short 校验再收窄——🗑️ 按钮
        # （cancel_open_orders，生产 L6896-6897）只截断 entry_orders 到
        # last_filled_count、不动 target_amounts，len(_eo)==last_filled_count
        # 是生产自己创造的合法状态（未成交层已被有意移除，pending_ids 恒空，
        # gate 无单可撤自然通过），必须放行。只拦两种真残缺：
        #   ① len(_eo) < last_filled_count（已成交层 ID 都丢失 = 账本损坏）
        #   ② last_filled_count < len(_eo) < len(target_amounts)（部分截断，
        #      无任何生产路径产生 = 可疑中间态）
        # 另：len(_eo)==last_filled_count==0 也拦（0<len 条件不满足）——
        # 「一层未成交且 ID 全空」与 🗑️ 签名形似但无生产来源，Fail-Closed。
        if len(target_amounts) > last_filled_count:
            _eo = snapshot.get('entry_orders')
            if not isinstance(_eo, list):
                return False, None, ('entry_orders_missing（存在 '
                                     f'{len(target_amounts) - last_filled_count} '
                                     '个未成交计划层，但 entry_orders 缺失/非列表）')
            if len(_eo) < len(target_amounts) and not (0 < len(_eo) == last_filled_count):
                return False, None, (f'entry_orders_short（entry_orders 长度 '
                                     f'{len(_eo)} 与已成交层数 {last_filled_count} /'
                                     f' 计划层数 {len(target_amounts)} 不一致，'
                                     '未成交层无法逐 ID 归因）')

        return True, {
            'last_filled_count': last_filled_count,
            'target_amounts': target_amounts,
            'current_filled_amount': current_filled_amount,
            'filled_details': filled_details,
            'total_entry_fee': total_entry_fee,
            'side': side,  # v6.1：上方已严格校验 ∈ {BUY, SELL}
            'params_base': snapshot.get('params_base') or {},
            'is_hedge_mode': bool(snapshot.get('is_hedge_mode', False)),
            # ── v6 自查补齐：参与「撤哪些单」的三个字段 ──────────────
            # 不进本函数就会退化成「靠调用方恰好把 target_b_data 整个换成
            # _claimed」——正确但不可校验。见上方 docstring 的 SL_2 孤儿链。
            'entry_orders': snapshot.get('entry_orders') or [],
            'tp_order_id': snapshot.get('tp_order_id'),
            'current_sl_id': snapshot.get('current_sl_id'),
        }, 'ok'

    # ── 原子回滚（v4 已有，v5/v6 未改动）────────────────────────────

    def _rollback_close_request_if_current(self, symbol: str, batch_id: str,
                                           close_op_id: str):
        """受控逆向迁移的唯一入口：原子回滚本次 close 请求的临时状态。

        范式复用 trader_260725.py::_commit_protection_with_g3（L3464，G3b）：
          持 _state_lock → 锁内 load_all_states() 重读最新磁盘（禁旧快照，
          消灭 TOCTOU）→ 同一锁段内判定 + 修改 + _persist_states。

        回滚资格（全部满足才执行，任一不满足拒绝）：
          1. batch 仍存在
          2. disk.close_op_id == 我这次的 close_op_id   ← 操作身份，证明
             "这是我的那一个 1"，不是别人正在推进的流程
          3. disk.close_phase 仍为 1                    ← 没有别的线程推进过
          4. 无 settled_by_limit_close 事实             ← 已发生的事实绝不降级

        只改三个字段：close_phase=0 / pending_close=False / is_programmatic_cancel=False。
        （close_op_id/close_reason 保留作为取证痕迹，供人工核对。）

        边界（G3b 契约）：_state_lock 非重入 → 锁内禁止调 save_batch_state /
        _update_registry（内部再取锁会死锁），直接操作 dict + _persist_states；
        锁内零交易所 API。

        返回 (ok: bool, reason: str)。
        """
        with self._state_lock:
            try:
                all_states = self.load_all_states()  # 硬约束：锁内重读，禁旧快照
            except Exception as e:
                return False, f'state_unreadable（{e}）'
            b = (all_states.get(symbol, {}) or {}).get(batch_id)
            if b is None:
                return False, 'batch_missing'
            disk_op_id = b.get('close_op_id') or ''
            if disk_op_id != (close_op_id or ''):
                return False, (f'op_id_mismatch（disk={disk_op_id!r} ≠ '
                               f'mine={close_op_id!r}，已有其他操作接管）')
            if int(b.get('close_phase', 0) or 0) != 1:
                return False, 'phase_changed（close_phase 已被推进，非本次请求）'
            if b.get('settled_by_limit_close'):
                return False, 'settled_fact_present（结算事实已发生，绝不降级）'
            b['close_phase'] = 0
            b['pending_close'] = False
            b['is_programmatic_cancel'] = False
            # 🔒 v6.1（P0-1 同型）：写盘失败绝不能报告「已回滚」——否则 TG
            # 告诉用户「监控恢复了」，磁盘却仍是 close_phase=1（监控冻结）。
            if not self._persist_states(all_states):
                return False, ('rollback_persist_failed（回滚写盘失败，'
                               '磁盘仍为 close_phase=1）')
            return True, 'rolled_back'

    # ── close_reason 异常态切换（v6.1 新增，第 11 个 helper）─────────

    def _set_close_reason_if_current(self, symbol: str, batch_id: str,
                                     close_op_id: str, reason: str):
        """把 close_reason 切换为异常态的 CAS 写入（ENTRY gate 失败等场景）。

        🔒 v6.1（ChatGPT 交叉审核 R1-§六）：市价 ENTRY gate 失败时若只发
        critical 而不更新 close_reason，批次将永远停留在 BEGIN 写入的
        'market_confirming' → 冻结监控（改动 4 的分型白名单）只 print、
        不再周期 critical —— 与已批准的「异常冻结 fail-noisy」直接矛盾。

        CAS 范围与 BEGIN / rollback 同原则：只有本批次仍属于**本次**事务
        （close_op_id 匹配 + close_phase>=1）时才写入，绝不覆盖别人已推进
        的状态。写盘失败返回 False（P0-1：持久化结果必须显式）。

        返回 (ok, why)。why ∈ 'reason_set' / 'missing_reason' /
        'batch_missing' / 'op_id_mismatch' / 'not_in_close' /
        'state_unreadable' / 'persist_failed'。
        """
        if not reason:
            return False, 'missing_reason'
        with self._state_lock:
            try:
                all_states = self.load_all_states()  # 锁内重读，禁旧快照
            except Exception as e:
                return False, f'state_unreadable（{e}）'
            b = (all_states.get(symbol, {}) or {}).get(batch_id)
            if not isinstance(b, dict):
                return False, 'batch_missing'
            if (b.get('close_op_id') or '') != (close_op_id or ''):
                return False, 'op_id_mismatch（已有其他操作接管，不覆盖）'
            if int(b.get('close_phase', 0) or 0) < 1:
                return False, 'not_in_close（无在途平仓事务）'
            b['close_reason'] = reason
            if not self._persist_states(all_states):
                return False, 'persist_failed（reason 写盘失败）'
            return True, 'reason_set'

    # ── 判据原语 ────────────────────────────────────────────────────

    def _read_position_amt(self, symbol: str, side: str, is_hedge_mode: bool) -> float | None:
        """读取【symbol + 持仓方向】的持仓绝对值。

        返回 None = 查询失败（不可判定）→ 调用方必须 Fail-Closed。
        返回 0.0  = 该方向无敞口。

        ⚠️ 读的是 symbol+方向【总敞口】，不是本批次敞口（D-006 同方向最多 3 批）。
          禁止单独用作放行判据——2026-08-29 探针实证（G:/tmp/probe_position_shape.py）：
          side 传错同样返回 0.0，与「已平仓」物理不可区分。

        v5（ChatGPT 终审 §八-1）：`positions` **非 list 一律返回 None**。
        v4 的 `for pos in positions if isinstance(positions, list) else []`
        对 dict/tuple/异常结构返回 total=0.0，仍是 UNKNOWN→ZERO 的同型退化
        （虽然多数情况下导致"不发单"，但按项目纪律必须严格 Fail-Closed）。
        """
        try:
            positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])
        except Exception as e:
            print(f"  ⚠️ 读取持仓失败: {e}")
            return None
        if positions is None:
            # 非异常的 None 返回同样不可判定，绝不能退化成 0.0（C-1 同型漏洞）
            print("  ⚠️ 读取持仓失败：fetch_positions 返回 None（非异常）")
            return None
        if not isinstance(positions, list):
            # v5：非 list 结构（dict/tuple/异常载荷）同样不可判定
            print(f"  ⚠️ 读取持仓失败：fetch_positions 返回非列表结构"
                  f"（{type(positions).__name__}），不可判定")
            return None
        target = 'long' if side == 'BUY' else 'short'
        want_raw = symbol.replace('/', '').split(':')[0]
        total = 0.0
        for pos in positions:
            info = pos.get('info', {}) or {}
            if pos.get('symbol') != symbol and info.get('symbol') != want_raw:
                continue
            if is_hedge_mode:
                ps = str(pos.get('side') or info.get('positionSide') or '').lower()
                if ps not in (target, 'both'):
                    continue
            try:
                total += abs(float(pos.get('contracts', 0) or pos.get('positionAmt', 0) or 0))
            except (TypeError, ValueError):
                return None
        return total

    def _fetch_close_order_state(self, order_id, symbol, retry_not_found: int = 3,
                                 not_found_delay: float = 2.0):
        """按单查询平仓单，返回 (state, order)。state ∈ {'success','not_found','unknown'}。

        复用 trader_260725.py::_verify_order_created（L3368）的既有三态语义：
          success   → 订单真实存在，order 可用
          not_found → OrderNotFound（重试排除可见性延迟后仍不存在）
          unknown   → 其他异常 → 调用方必须 Fail-Closed（UNKNOWN ≠ EMPTY）

        平仓单走 'normal' 端点（不带 params={'stop': True}）。
        not_found 必须重试后再定案（2026-08-29 事件 3 实证：4/4 单 0 秒 verify
        全部 OrderNotFound 假阴性 → 曾致 12 处误判 24 个孤儿单）。
        """
        try:
            order = self._safe_api_call(self.exchange.fetch_order, order_id, symbol, retries=1)
            return 'success', order
        except ccxt.OrderNotFound:
            for _ in range(retry_not_found):
                time.sleep(not_found_delay)
                try:
                    order = self._safe_api_call(
                        self.exchange.fetch_order, order_id, symbol, retries=1)
                    return 'success', order
                except ccxt.OrderNotFound:
                    continue
                except Exception:
                    return 'unknown', None
            return 'not_found', None
        except Exception:
            return 'unknown', None

    # ── 六态确认器（v5：TERMINAL_ZERO 收紧）─────────────────────────

    def _confirm_close_filled(self, symbol: str, side: str, is_hedge_mode: bool,
                              order_id, expected: float, pos_before: float | None = None,
                              attempts: int = 3, delay: float = 0.6):
        """确认【这张平仓单】的成交事实。返回 (verdict, detail, filled_amount)。

        verdict（六态，ChatGPT 终审 §一 批准方向 + §五 收紧 TERMINAL_ZERO）：
          'CONFIRMED_FULL'  → 完整成交 → 放行撤 SL/TP
          'TERMINAL_ZERO'   → **唯一有资格回滚**的状态。v5 收紧后只可能是：
                              status ∈ (canceled, expired, rejected)
                              + 权威 filled 字段**明确存在**且 == 0。
                              v4 的 `filled = float(order.get('filled') or 0)`
                              会把 filled 缺失/None 变成 0 → 又是一个小型
                              UNKNOWN→ZERO（ChatGPT 终审 §五）；且
                              closed/filled + filled==0 本身是矛盾组合，
                              v5 一律判 UNKNOWN，不给回滚资格。
          'PARTIAL'         → filled > 0 但不足 → **绝不回滚**（仓位已真实变化，
                              回滚=把"已部分平掉"伪装成"未平过"）→ 保持保护单
                              + critical 人工接管
          'PENDING'         → new/open/active → **绝不回滚**（订单活着，稍后可能
                              成交；回滚后订单再成交，状态机已回 ACTIVE 却无人管）
          'UNKNOWN'         → 查询异常 / 字段不可判定 → 不回滚 + critical
          'NOT_CONFIRMED'   → create 已返回 ID 但 fetch 查不到（重试后仍 OrderNotFound）
                              → **绝不回滚**。复用 _verify_order_created 的语义：
                              not_found = NOT_CONFIRMED（不 Commit），绝不是
                              "证明订单没成交可以放心反向操作"。

        核心不变量：只要 create_order() 成功返回了有效 order_id，
        close_order_placed=True 就不再改回 False。回滚不再操作这个标志，
        而是通过 _rollback_close_request_if_current 的 close_op_id CAS。

        判据为什么必须是「订单维度」：v2 的 delta（总敞口减少量）无法归因——
        另一批次 SL 成交 / 用户手动平仓 / ADL 都会让总敞口下降 → 假确认 → 裸仓。
        fetch_order 回答「我这张单成交了多少」，天然免疫他方行为。
        delta 已降级为 CONFIRMED_FULL 后的二级交叉校验（仅告警不阻断）。
        """
        if expected is None or expected <= 0:
            return 'UNKNOWN', f"参数不可判定（expected={expected}）", None

        # B-03：有效预期 = min(台账量, pos_before)。台账量可能大于实际剩余
        # （上次部分成交未同步 / 用户手动减仓），若直接用台账量判，仓位真实
        # 归零也永远判不通过 → 永久不可平。
        if pos_before is not None and pos_before > 0:
            eff_expected = min(expected, pos_before)
        else:
            eff_expected = expected
        tol = 1e-8 + abs(eff_expected) * 1e-6
        zero_tol = 1e-12

        n = max(1, attempts)
        last_detail = ''
        for i in range(n):
            state, order = self._fetch_close_order_state(order_id, symbol)

            if state == 'success':
                if not isinstance(order, dict):
                    return 'UNKNOWN', f"订单结构异常（{type(order).__name__}）", None
                status = str(order.get('status') or '').lower()

                # ── v5（§五）：权威 filled 必须明确存在，否则 UNKNOWN
                if 'filled' not in order or order.get('filled') is None:
                    return 'UNKNOWN', (f"订单 {order_id} 的 filled 字段缺失"
                                       f"（status={status!r}）——无法证明零成交，"
                                       f"无回滚资格"), None
                try:
                    filled = float(order['filled'])
                except (TypeError, ValueError):
                    return 'UNKNOWN', (f"订单 {order_id} 的 filled 字段不可解析"
                                       f"（{order.get('filled')!r}）——无回滚资格"), None

                if status in ('closed', 'filled'):
                    if filled >= eff_expected - tol:
                        detail = (f"订单 {order_id} 已成交 filled={filled}"
                                  f"（有效预期 {eff_expected}，台账 {expected}），status={status}")
                        # 二级交叉校验（B-01 处置 2）：按单已确认成交，再看敞口是否
                        # 真的相应减少。仅告警不阻断——多批次下其他批次的减仓会让
                        # 这里出现正常的不匹配，阻断会把正常路径卡死。
                        if pos_before is not None:
                            after = self._read_position_amt(symbol, side, is_hedge_mode)
                            if after is not None and (pos_before - after) < eff_expected - tol:
                                print(f"  ⚠️ [交叉校验] 订单已成交但敞口未见相应减少："
                                      f"before={pos_before} after={after} "
                                      f"预期减少>={eff_expected}（多批次下可能正常，请人工留意）")
                        return 'CONFIRMED_FULL', detail, filled
                    if filled > zero_tol:
                        # 部分成交：仓位已真实变化，绝不回滚
                        return 'PARTIAL', (
                            f"订单 {order_id} 部分成交 filled={filled} < 预期 {eff_expected}"
                            f"（status={status}）。仓位已变化，不可回滚；"
                            f"保持保护单，需人工接管剩余 {eff_expected - filled}"), filled
                    # v5（§五）：closed/filled 却 zero filled = 矛盾组合 → UNKNOWN
                    return 'UNKNOWN', (
                        f"订单 {order_id} 状态为 {status} 但权威 filled=0（矛盾/异常组合，"
                        f"预期 {eff_expected}）——按 UNKNOWN 处理，无回滚资格，"
                        f"转人工核对"), None

                if status in ('canceled', 'expired', 'rejected'):
                    if filled > zero_tol:
                        return 'PARTIAL', (
                            f"订单 {order_id} 终态 {status} 但已成交 filled={filled} > 0"
                            f"（预期 {eff_expected}）。仓位已变化，不可回滚"), filled
                    # v5：唯一可回滚门 —— 权威 filled 明确存在且 == 0
                    return 'TERMINAL_ZERO', (
                        f"订单 {order_id} 终态 {status} 且权威 filled=0"
                        f"（预期 {eff_expected}）——唯一可回滚状态"), 0.0

                if status in ('new', 'open', 'active', 'pending', 'partially_filled'):
                    last_detail = (f"订单 {order_id} 仍在活动中：status={status} "
                                   f"filled={filled}（预期 {eff_expected}）")
                    if i < n - 1:
                        time.sleep(delay)
                        continue
                    # 订单活着 → 绝不回滚（回滚后它再成交就无人管辖）
                    return 'PENDING', last_detail, filled if filled > zero_tol else None

                # 未知 status 字符串：不猜，按 UNKNOWN
                return 'UNKNOWN', f"订单 {order_id} 状态不可识别：status={status!r}", None

            elif state == 'not_found':
                # create 已返回 ID → fetch 查不到 ≠ 没成交。
                # _verify_order_created 的 not_found 语义是 NOT_CONFIRMED（不 Commit），
                # 不是"证明订单不存在可以反向操作"。
                return 'NOT_CONFIRMED', (
                    f"订单 {order_id} create 返回了 ID，但 fetch_order 重试后仍查不到"
                    f"（NOT_CONFIRMED，绝不回滚）"), None
            else:  # unknown
                last_detail = f"查询订单 {order_id} 失败（结果未知）"
                if i < n - 1:
                    time.sleep(delay)
                    continue
                return 'UNKNOWN', f"查询订单 {order_id} 连续 {n} 次失败，结果未知", None

        return 'UNKNOWN', (last_detail or f"订单 {order_id} 确认流程异常结束"), None

    # ── 归因守卫（v5：coverage 不变量修正）──────────────────────────

    def _survey_same_side_batches(self, symbol: str, side: str,
                                  target_batch_id: str):
        """勘察同 symbol + 同方向的批次分布。

        返回 (others_count, sum_all, blocking_count)：
          others_count    = 除 target 外，同方向且台账>0 的其他批次数
          sum_all         = **含 target 在内**的同方向批次台账合计（coverage 需
                            要覆盖"仍需保留的 tracked exposure"，见下）
          blocking_count  = 除 target 外，已进入平仓流程（close_phase>=1 或
                            pending_close）的其他批次数
        返回 (-1, -1, -1) = 无法判定 → 调用方必须 Fail-Closed。

        🔑 v5 修正（ChatGPT 终审 §一）：v4 在这里 `continue` 掉所有
        close_phase>=1 / pending_close 的批次，**包括 target 自己**。
        但真实调用顺序是：BEGIN 先写 close_phase=1（落盘）→ 才调
        _close_amount_guard → _survey_same_side_batches。于是 target 被自己
        的过滤条件排除，决定性例子重跑一遍会再次放行：

            A(target) 0.001 [close_phase=1 → 被排除]
            B         0.001
            actual    0.001
            → v4: sum_all=0.001, actual=0.001 → actual < sum_all 为 False → 放行 ❌
            → v5: sum_all=0.002, actual=0.001 → Fail-Closed ✅

        为什么 sum_all 必须含 target（coverage 不变量的推导，ChatGPT 原文批准）：
            actual >= sum_tracked
            平掉 L_target 后： actual - L_target >= sum_tracked - L_target
            → 剩余所有 tracked batches 仍有足够实际仓位覆盖。
        若 sum_tracked 不含 target，这个推导不成立。

        §六：其他批次处于 close_phase>=1 时，其仓位可能 100% 仍在场
        （limit_pending_normal 可挂数小时）→ 从 coverage 角度仍需占用储备。
        v5 把它们计入 sum_all（保守）并单独暴露 blocking_count；BEGIN 的
        同方向单飞检查已让这种情况理论不可达，若仍出现说明并发窗口或人工
        改过状态 → 调用方按 Fail-Closed 处理。
        """
        try:
            all_states = self.load_all_states()
        except Exception as e:
            print(f"  ⚠️ 勘察同方向批次失败（无法判定归因）: {e}")
            return -1, -1, -1
        batches = all_states.get(symbol, {}) or {}
        others = 0
        blocking = 0
        sum_all = 0.0
        for bid, b in batches.items():
            if not isinstance(b, dict):
                continue
            if b.get('side', 'BUY') != side:
                continue
            try:
                filled = float(sum((b.get('target_amounts') or [])
                                   [:int(b.get('last_filled_count', 0) or 0)]))
            except (TypeError, ValueError):
                return -1, -1, -1
            if filled <= 0:
                continue
            # v5：target 与任何 close_phase 的批次都计入 coverage
            sum_all += filled
            if bid == target_batch_id:
                continue
            if int(b.get('close_phase', 0) or 0) >= 1 or b.get('pending_close'):
                blocking += 1
            others += 1
        return others, sum_all, blocking

    def _close_amount_guard(self, symbol: str, side: str, is_hedge_mode: bool,
                            ledger_amount: float, batch_id: str):
        """下单数量守卫（v5：coverage 不变量，ChatGPT 终审 §一 批准 + §六）。

        规则：
          读取失败 / 勘察失败 → None（Fail-Closed 不发单，B-09 已获批准）
          blocking_count > 0  → None（同方向另有在途平仓事务，理论不可达；
                                若发生说明并发窗口或人工改过状态 → Fail-Closed）
          单批次方向（others == 0）：
            actual >= ledger → 按台账平
            actual <  ledger → 按实测平（min 的合法域：归因唯一成立，B-03）
          多批次方向：
            actual < 台账合计（**含本批**） → 归因冲突，禁止自动平（Fail-Closed）
            actual >= 台账合计 → 按台账平

        返回 (amount, detail)。amount=None → 调用方必须 Fail-Closed 不发单。
        """
        actual = self._read_position_amt(symbol, side, is_hedge_mode)
        if actual is None:
            return None, "读取实际持仓失败，无法确定平仓数量（Fail-Closed，不发单）"
        tol = 1e-8 + abs(ledger_amount) * 1e-6

        others, sum_all, blocking = self._survey_same_side_batches(symbol, side, batch_id)
        if others < 0:
            return None, "同方向批次勘察失败，归因不可判定（Fail-Closed，不发单）"
        if blocking > 0:
            return None, (f"同方向另有 {blocking} 个批次正处于平仓流程中，"
                          f"其实际仓位占用不可判定（BEGIN 应已拒绝，出现即说明并发窗口"
                          f"或人工改过状态）→ 禁止自动平仓（Fail-Closed，人工 reconcile）")

        if others == 0:
            # 单批次：归因唯一，min 是合法域
            if actual >= ledger_amount - tol:
                if actual <= 0 and ledger_amount <= 0:
                    return 0.0, "台账与实测敞口均为 0，无需平仓"
                return ledger_amount, f"单批次方向，总敞口 {actual} ≥ 台账 {ledger_amount}，按台账量平仓"
            if actual <= 0:
                return 0.0, f"实际敞口为 0（台账 {ledger_amount}），无需平仓"
            return actual, (f"单批次方向，台账 {ledger_amount} > 实测 {actual}，"
                            f"归因唯一成立，按实测 {actual} 平仓")

        # 多批次：总敞口 vs 台账合计（含本批）
        if actual < sum_all - tol:
            return None, (f"归因冲突：总敞口 {actual} < 同方向批次台账合计 {sum_all}"
                          f"（本批 {ledger_amount} + 其他 {others} 批）——账本与交易所已漂移，"
                          f"总量数据不能证明 batch 归属，禁止自动平仓"
                          f"（Fail-Closed，critical + 人工 reconcile）")
        if actual > sum_all + tol:
            print(f"  ⚠️ [归因] 总敞口 {actual} > 台账合计 {sum_all}：存在未跟踪敞口，"
                  f"平本批台账量不会侵占其他批次，但请人工留意多余敞口的来源")
        return ledger_amount, (f"多批次方向但台账合计 {sum_all} ≤ 总敞口 {actual}，"
                               f"归属成立，按台账量 {ledger_amount} 平仓")

    # ── ENTRY 撤单 + 逐 ID 终态验证（v5 未改动，调用处改为 gate）────

    def _verify_entry_order_terminal(self, order_id, symbol: str,
                                     attempts: int = 3, delay: float = 0.8):
        """逐 ID 确认单个 ENTRY 挂单已消失（事务事实按 ID 归因，与平仓确认同原则）。

        返回 verdict ∈ {'gone','filled','open','unknown'}：
          gone    → canceled/expired/rejected（**交易所明确返回终态对象**）
          filled  → ENTRY 在等待期间成交了 → 仓位已变化，必须中断放行流程
          open    → 仍然活着
          unknown → 查询失败 / OrderNotFound，不可判定

        🔒 v6（ChatGPT 终审 §二 小点）：OrderNotFound 从 gone 改为 unknown。
        G3a 的「-2011/Unknown order = 已收敛」只对**撤销**这个目标成立
        （目标 = 这张单不再挂着）。而本 helper 服务于 ENTRY gate，需要证明的
        是「这张 ENTRY 没有成交」—— 生产 L1992 的既有认知明确写着：
            # 订单确实不存在（已撤销/已成交/已过期）→ 安全清除
        「已成交」就在其中。OrderNotFound 能证明「不用再 cancel 了」，
        证明不了「它没有成交」。三种可能里只有一种是安全的 → Fail-Closed。

        ✅ 正常路径不受影响：生产 L4151 实证「自愈 fetch 已撤销订单返回
        status=canceled 对象（不抛 OrderNotFound）」→ 正常撤单后 fetch 会
        拿到 canceled，仍走 gone。只有真正查不到的异常路径才 Fail-Closed。
        """
        for i in range(max(1, attempts)):
            try:
                order = self._safe_api_call(
                    self.exchange.fetch_order, order_id, symbol,
                    params={'stop': True}, retries=1)
            except ccxt.OrderNotFound:
                # 🔒 v6：不存在 ≠ 未成交（可能已成交）→ Fail-Closed
                return 'unknown', None
            except Exception:
                return 'unknown', None
            if order is None:
                # _safe_api_call 静默失败（限流/网络）→ 未知，绝不当成"已消失"
                if i < attempts - 1:
                    time.sleep(delay)
                    continue
                return 'unknown', None
            status = str((order or {}).get('status') or '').lower()
            if status in ('canceled', 'expired', 'rejected'):
                return 'gone', order
            if status in ('closed', 'filled'):
                return 'filled', order
            if i < attempts - 1:
                time.sleep(delay)
                continue
            return 'open' if status else 'unknown', order
        return 'unknown', None

    def _cancel_and_verify_entry_orders(self, symbol: str, batch_id: str,
                                        b_data: dict, last_filled_count: int) -> bool:
        """平仓成功后撤未成交 ENTRY 并做交易所侧验证。

        ⚠️ v5 契约（ChatGPT 终审 §四）：**返回值必须被调用方当作 clear gate**。
        返回 False 时调用方必须 raise，绝不继续进入
        `_converge_batch_orders_before_clear()` / `clear_batch_state()`。
        否则最坏链是：
            helper 正确识别 UNKNOWN → return False
            → 调用方忽略 → legacy converge 的 `fetch_open_orders(...) or []`
              把 None 变成 [] → EMPTY → 生成 proof → clear
        等于"前门修好、后门又放回来"。

        🚨 v6 调用顺序契约（ChatGPT 终审 §二）：本 helper 必须在**撤销 SL/TP
        之前**完成。市价路径正确次序：
            MARKET 按单 CONFIRMED_FULL
            → 撤未成交 ENTRY（本 helper）
            → 逐 ID 确认 ENTRY 全部安全终结（本 helper）
            → 只有 gate=True 才撤 TP / SL
            → 结算 / converge / clear
        v5 把它放在撤 TP→撤 SL 之后，形成这条事故链：
            MARKET 平掉 0.001 → 未撤的 ENTRY 恰好成交 0.001 → 又产生
            LONG 0.001 → 先撤 TP → 先撤 SL → 才 verify ENTRY 发现成交
            → raise → 批次冻结，但仓位已无 SL/TP 保护（裸仓）。
        gate 在前时，同一场景的后果是：批次冻结 + **SL/TP 仍在位** + critical。

        双缺陷修复（ChatGPT 终审 §三）：
          1. `fetch_open_orders(...) or []` —— 与 C-1 完全同型的假确认：
             None → [] → remaining_ids 空 → still_alive 空 → ✅"全部清零"。
             实际查询根本没给出有效结果。UNKNOWN → EMPTY，而本 helper 的安全
             意义恰恰是"证明 ENTRY 不会重新开仓"。
          2. 只用 open_orders 快照判清零，违反项目"事务事实按 ID 归因"原则
             （L3371：Verify 必须用 fetch_order）。
        """
        entry_orders = b_data.get('entry_orders', []) or []
        pending_ids = [oid for idx, oid in enumerate(entry_orders)
                       if idx >= last_filled_count and oid]
        if not pending_ids:
            return True

        for order_id in pending_ids:
            try:
                self._safe_api_call(self.exchange.cancel_order, order_id, symbol,
                                    params={'stop': True})
                print(f"  └─ 已撤销开仓挂单: {order_id}")
            except Exception as e:
                if '-2011' in str(e) or 'Unknown order' in str(e):
                    print(f"  └─ 开仓挂单 {order_id} 已不存在（视为已撤）")
                else:
                    print(f"  └─ ⚠️ 撤销开仓挂单失败: {order_id} ({e})（由逐 ID 验证阶段定案）")

        # ── 第 1 层：open_orders 快照（v4：禁 or []，None/非 list = Fail-Closed）
        try:
            remaining = self._safe_api_call(
                self.exchange.fetch_open_orders, symbol, params={'stop': True})
        except Exception as e:
            remaining = None
            print(f"  └─ ⚠️ 撤单后交易所快照查询异常: {e}")
        if remaining is None or not isinstance(remaining, list):
            self.send_tg_notification(
                f"🚨【资金安全】平仓后 ENTRY 校验失败（快照不可判定）！\n"
                f"🆔 批次: {batch_id}\n"
                f"⚠️ fetch_open_orders 返回 {type(remaining).__name__}，"
                f"无法确认残留 ENTRY 是否已清零，请立即人工核对！",
                level='critical')
            return False

        remaining_ids = {str(o.get('id')) for o in remaining if isinstance(o, dict)}
        still_alive = [oid for oid in pending_ids if str(oid) in remaining_ids]
        if still_alive:
            print(f"  └─ 🚨 撤单后交易所仍存在 ENTRY: {still_alive}")
            self.send_tg_notification(
                f"🚨【资金安全】平仓成功后仍有未撤销的开仓条件单！\n"
                f"🆔 批次: {batch_id}\n📌 残留订单: {still_alive}\n"
                f"⚠️ 这些挂单成交后将形成无保护仓位，请立即人工处理！",
                level='critical')
            return False

        # ── 第 2 层：逐 ID fetch_order 终态确认
        for oid in pending_ids:
            verdict, _order = self._verify_entry_order_terminal(oid, symbol)
            if verdict == 'gone':
                continue
            if verdict == 'filled':
                detail = f"ENTRY {oid} 在平仓等待期间成交（仓位已变化）"
            elif verdict == 'open':
                detail = f"ENTRY {oid} 撤单后仍存活"
            else:
                detail = f"ENTRY {oid} 终态无法判定（查询失败）"
            print(f"  └─ 🚨 ENTRY 逐 ID 验证未通过: {detail}")
            self.send_tg_notification(
                f"🚨【资金安全】平仓后 ENTRY 逐 ID 验证未通过！\n"
                f"🆔 批次: {batch_id}\n📌 {detail}\n"
                f"⚠️ 可能形成无保护仓位，请立即人工核对持仓与挂单！",
                level='critical')
            return False

        print(f"  └─ ✅ ENTRY 撤单已交易所侧校验通过"
              f"（快照 + 逐 ID 终态，{len(pending_ids)} 个全部确认消失）")
        return True
```

**v3 → v4 差异速览**：

| helper | v3 | v4 |
|---|---|---|
| `_read_position_amt` | None 拦截（交叉审查 C-1 修复） | **不变**；另把 `positions or []` 收紧为 `isinstance` 检查 |
| `_fetch_close_order_state` | 三态 + not_found 重试 | **不变** |
| `_confirm_close_filled` | 二态（confirmed / not_filled / unknown）——**not_filled 过宽** | **六态**；`not_found → NOT_CONFIRMED`（绝不回滚）；返回 `(verdict, detail, filled)` 供结算贯穿 |
| `_close_amount_guard` | `min(台账, 总敞口)`——**多批次无法归因** | 归因规则：单批次 min 合法域；**多批次看「总敞口 vs 台账合计」**；依赖新增 `_survey_same_side_batches` |
| `_rollback_close_request_if_current` | （无——用锁外旧快照 + escape hatch） | **新增**：`_state_lock` 内重读 + `close_op_id` CAS（复用 G3b 范式） |
| `_verify_entry_order_terminal` | （无——只用 open_orders 快照） | **新增**：逐 ID `fetch_order(stop=True)` 终态确认。**v6：`OrderNotFound` 从 `gone` 收紧为 `unknown`**（不存在 ≠ 没成交，见改动 2 语义边界） |
| `_cancel_and_verify_entry_orders` | `or []` 假确认（P0） | None/非 list → critical；双层验证（快照 + 逐 ID） |

### 改动 2：ENTRY 撤单验证（v4 引入，v5 追加 clear gate；你的 §三 + §四）

插入位置：紧邻 `close_position_market` 之前（L6945 附近）。完整源码见改动 1.5
（`_cancel_and_verify_entry_orders` + `_verify_entry_order_terminal`）。

**v3 的两个缺陷**（你的原文）：

1. `fetch_open_orders(...) or []` —— 与 C-1 完全同型的假确认：
   `None → [] → remaining_ids 空 → still_alive 空 → ✅"全部清零"`，
   而查询根本没有给出有效结果（UNKNOWN → EMPTY）。这个 helper 的安全意义
   恰恰是「证明 ENTRY 不会重新开仓」，属 P0。
2. 只用 open_orders 快照判清零，违反「事务事实按 ID 归因」原则
   （L3371：Verify 必须用 fetch_order）。

**v4 修复**（⚠️ 以下为**结构示意**，`...` 表示省略的参数，不是可套用代码
——v5 §七：含占位的块一律标 `python-frag`；完整源码见改动 1.5）：

```python-frag
        # ── 第 1 层：open_orders 快照（v4：禁 or []，None/非 list = Fail-Closed）
        if remaining is None or not isinstance(remaining, list):
            self.send_tg_notification(..., level='critical')
            return False
        # ── 第 2 层（v4 新增）：逐 ID fetch_order 终态确认
        for oid in pending_ids:
            verdict, _order = self._verify_entry_order_terminal(oid, symbol)
            # gone / filled / open / unknown —— 后三者全部 critical + return False
```

**语义边界（v6 收紧）**：`_verify_entry_order_terminal` 的 `OrderNotFound`
**不再判 `gone`**，改为 `unknown`（你的 §二 小点）。

理由按你的原话成立，我补了生产侧的实证：

- G3a 的「-2011/Unknown order = 已收敛」（L3497）只对**撤销**这个目标成立
  （目标 = 这张单不再挂着）。本 helper 服务于 ENTRY gate，需要证明的是
  「**这张 ENTRY 没有成交**」。
- 生产 **L1992** 的注释原文：`# 订单确实不存在（已撤销/已成交/已过期）→ 安全清除`
  —— 「已成交」就在其中，三种可能里只有一种是安全的。
- ✅ 正常路径不受影响：生产 **L4151** 实证「自愈 fetch 已撤销订单返回
  `status=canceled` 对象（**不抛 OrderNotFound**）」→ 正常撤单后 fetch 拿到
  `canceled`，仍走 `gone`。只有真正查不到的异常路径才 Fail-Closed。

`filled` 单列一档（ENTRY 在平仓等待期间成交 → 仓位已变化 → 必须中断放行流程），
这是快照方案完全看不见的状态。

**调用形态与既有代码一致**：`fetch_open_orders(symbol, params={'stop': True})`
生产已有 L2759 / L7289 / L7410 三处相同用法。

**一个刻意保留的选择**：没有复用 Batch B 的 `_converge_batch_orders_before_clear`
——它的失败结果是 `cleanup=PENDING` 软提示不 critical，而「持仓已平 + ENTRY 未撤」
需要人工立刻知道。独立校验 + critical。

**v5 追加（你的 §四 前半）：helper 的 `False` 必须成为 clear gate。**

helper 内部修得再对，调用方忽略返回值就等于是把漏洞从**前门修掉、后门又放回来**：

```text
helper: None → 正确识别 UNKNOWN → return False
   ↓ 返回值被忽略
legacy converge: fetch_open_orders(...) or [] → None → [] → EMPTY
   ↓
生成 proof → clear
```

因此调用处必须是（已经按你的 §二 前移到撤 SL/TP 之前，见改动 1 的 AFTER）：

```python
            _entries_ok = self._cancel_and_verify_entry_orders(
                target_symbol, batch_id, target_b_data, last_filled_count)
            if not _entries_ok:
                # 🛡️ 本段已前移 → SL/TP 仍在位，仓位保护没有丢失。
                # 不进 except：那是通用「结算异常」文案，无法区分此场景，
                # 且会因 close_order_placed=True 与此处双报 critical。
                # 🔒 v6.1（交叉审核修正2 + 自审 N-2）：gate 失败必须把 close_reason
                # 切成异常态（改动 4 白名单已含 market_entry_unknown）；
                # 返回值必须接收，双重故障时 critical 文案如实追加。
                _rs_ok, _rs_why = self._set_close_reason_if_current(
                    target_symbol, batch_id, close_op_id, 'market_entry_unknown')
                self.send_tg_notification(
                    f"🚨【资金安全】市价平仓已成交，但 ENTRY 收敛未确认！\n"
                    f"🆔 批次: `{batch_id}`\n"
                    f"🛡️ SL/TP **已保留未撤**，仓位仍有保护\n"
                    f"🚫 批次保持冻结（close_phase=1），本轮禁止进入 clear\n"
                    f"⚠️ 请立即人工核对残留开仓单与持仓！"
                    + ('' if _rs_ok else
                       f"\n⚠️ close_reason 切换失败（{_rs_why}），"
                       "冻结告警可能不再周期触发"),
                    level='critical')
                return False, ("❌ 市价平仓已成交但 ENTRY 收敛未确认"
                               "（SL/TP 保留，批次冻结待人工处置）")
```

**（v6）为什么从 `raise` 改成直接 `return False`**：两个原因。
① `raise` 会进 `except`，那里因 `close_order_placed=True` 走的是
「结算异常」通用文案 + critical，与本场景的具体事实（保护单还在、
需要人工处理残留 ENTRY）不匹配，且容易让人误判成 SL/TP 已撤。
② 前移之后 SL/TP 未撤是**明确的既成事实**，应当显式告知。
两者的流程后果相同：不回滚、不进 clear、批次保持 `close_phase=1`。

**同时清掉后门**：`_converge_batch_orders_before_clear` 自己的 `or []` 一并改为
显式 CONVERGENCE_UNKNOWN（新增改动 9）——它是最终 proof 生产者，那里不该存在
UNKNOWN→EMPTY。

---
### 改动 3v6：close transaction 对称原子化（§二 + §三 + §六 + v6 §一）

**撤销声明（沿用 v4）**：v3 改动 3（`save_batch_state` 加 `allow_flag_rollback`
参数通道）、改动 4（`_merge_batch_state` escape hatch）、改动 5、改动 6（AST 守卫）
**全部撤回**。`_merge_batch_state` / `save_batch_state` **零改动**（棘轮原样保留）。

**v4 缺的那半边**：v4 只有 `_rollback_close_request_if_current()`（对称事务的
**结束**端），没有开始端的 claim。你的原话——「CAS 发生得太晚了……问题发生在
close 开始阶段」。v5 补上对称的：

```text
atomic BEGIN（本改动 3v6-1）→ **返回 claimed 快照**
   ↓
derive transaction vars（改动 1c，v6 新增——claim 与参数绑定）
   ↓
exchange action（create_order / cancel_entry / cancel_sl_tp）
   ↓
verify（六态，改动 1）
   ↓
atomic rollback（改动 3v6-4）或 settle
```

#### 3v6-1：atomic BEGIN 替换 flags 写入（市价 L6980-6984 / 限价 L7508-7512，同一改法）

```python
        # BEFORE（市价 L6980-6984；限价为 L7508-7512，仅 close_reason 不同）
        # 标记程序主动平仓，监控线程将静默退出（ticker 已成功，安全设 flags）
        target_b_data['is_programmatic_cancel'] = True
        target_b_data['pending_close'] = True
        target_b_data['close_phase'] = 1  # P0 Batch A：CLOSE_REQUESTED（唯一权威，P0-1）
        self.save_batch_state(target_symbol, batch_id, target_b_data)

        # AFTER（v6）
        # 🆕 atomic BEGIN：取得本次 close transaction 的**唯一所有权**，
        # 并**返回它锁内刚 claim 的 batch 副本**作为 transaction 基线。
        # 位置保持在 ticker 成功之后（保留 L6972「先取市价成功后再设 flags」的既有修复）。
        _begin_ok, close_op_id, _begin_why, _claimed = self._begin_close_request_if_active(
            target_symbol, batch_id, 'market_confirming')
        if not _begin_ok:
            # 未取得所有权 → **绝不发出任何交易所订单**。
            # 双击/并发的第二个人在这里终止，不会形成第二张平仓单。
            self.send_tg_notification(
                f"🚨【资金安全】市价平仓未启动：未取得平仓事务所有权。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_begin_why}\n"
                f"⚠️ 未发出任何订单，请人工核对批次状态。",
                level='critical')
            return False, f"❌ 市价平仓未启动（{_begin_why}）"
        # 🔑 v6：紧接着用 _claimed 派生 transaction 变量（详见改动 1c）
        _vars_ok, _txn_vars, _vars_why = self._derive_close_txn_vars(_claimed, batch_id)
        if not _vars_ok:
            _rb_ok, _rb_why = self._rollback_close_request_if_current(
                target_symbol, batch_id, close_op_id)
            self.send_tg_notification(
                f"🚨【资金安全】市价平仓中止：claimed 快照不能用于下单。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_vars_why}\n"
                f"🔄 回滚本次平仓标记: {'成功' if _rb_ok else '失败（' + _rb_why + '）'}\n"
                f"⚠️ 未发出任何订单，请人工核对账本与批次状态。",
                level='critical')
            return False, f"❌ 市价平仓中止（{_vars_why}）"
        target_b_data = _claimed
        last_filled_count = _txn_vars['last_filled_count']
        target_amounts = _txn_vars['target_amounts']
        current_filled_amount = _txn_vars['current_filled_amount']
        side = _txn_vars['side']
```

限价平仓同法，仅 `close_reason` 不同（`limit_pending_normal`）：

```python
        _begin_ok, close_op_id, _begin_why, _claimed = self._begin_close_request_if_active(
            target_symbol, batch_id, 'limit_pending_normal')
        if not _begin_ok:
            self.send_tg_notification(
                f"🚨【资金安全】限价平仓未启动：未取得平仓事务所有权。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_begin_why}\n"
                f"⚠️ 未发出任何订单，请人工核对批次状态。",
                level='critical')
            return False, f"❌ 限价平仓未启动（{_begin_why}）"
        _vars_ok, _txn_vars, _vars_why = self._derive_close_txn_vars(_claimed, batch_id)
        if not _vars_ok:
            _rb_ok, _rb_why = self._rollback_close_request_if_current(
                target_symbol, batch_id, close_op_id)
            self.send_tg_notification(
                f"🚨【资金安全】限价平仓中止：claimed 快照不能用于下单。\n"
                f"🆔 批次: `{batch_id}`\n💡 原因: {_vars_why}\n"
                f"🔄 回滚本次平仓标记: {'成功' if _rb_ok else '失败（' + _rb_why + '）'}\n"
                f"⚠️ 未发出任何订单，请人工核对账本与批次状态。",
                level='critical')
            return False, f"❌ 限价平仓中止（{_vars_why}）"
        target_b_data = _claimed
        last_filled_count = _txn_vars['last_filled_count']
        target_amounts = _txn_vars['target_amounts']
        current_filled_amount = _txn_vars['current_filled_amount']
        side = _txn_vars['side']
```

`is_programmatic_cancel` / `pending_close` / `close_phase=1` 三个字段由 BEGIN 在
锁内**与 `close_op_id`、`close_reason` 一起**写入并落盘——不再是调用方分散赋值。

#### 3v6-1b：为什么 BEGIN 必须把快照返出来（你的 §一）

v5 的 BEGIN 返回 `(ok, op_id, reason)`。调用方拿到 `ok=True` 之后，**只能**继续用
它自己在 BEGIN 之前算好的局部变量——因为 BEGIN 拿到的那份最新状态在锁内用完就丢了。
结果是「我 claim 的状态」与「我按下单的状态」仍然是两个状态。

v6 补上第四返回值 `snapshot`（锁内 `dict(b)`，与本次落盘内容逐字段一致），
并配套 `_derive_close_txn_vars()` 做**整套同源**派生 + 完整性校验。
测试 `D1-D5` + 负向 `D-neg` 锁定这个行为。

#### 3v6-2：BEGIN 资格判定（五条全过才 claim）

```text
1. batch 存在且 is_active                  → 否则 'batch_missing' / 'batch_inactive'
2. disk.close_phase == 0（严格，非 >=0）   → 否则 'close_phase_not_zero'
3. disk.pending_close 为假                 → 否则 'pending_close_already_set'
4. 无 settled_by_limit_close 事实          → 否则 'settled_fact_present'
5. 🔒 同 symbol + 同方向，除本批次外没有任何批次
   处于 close_phase>=1 或 pending_close    → 否则 'same_side_close_inflight'
```

全部通过 → `op_id = uuid.uuid4().hex` → 写 5 个字段 → `_persist_states`（锁内一次）。

#### 3v6-3：为什么 `op_id` 必须是 uuid 而不是毫秒时间戳（你的 §三）

毫秒时间戳恰恰在"双击并发"这种最需要区分 identity 的场景下可能碰撞。
`trader_260725.py` **L12 已 `import uuid`**，零新增依赖。
且 op_id 必须在 BEGIN 内生成——v4 把它放在 `close_order_placed=False` 段
（L7003），而 `close_phase=1` 落盘在 L6983（更早）→ 按 v4 拼起来是
**NameError**。测试 `B1c/B8` 锁定 32 位 hex 与 5 次独立 claim 互不相同。

#### 3v6-4：两处 rollback 调用点替换为 CAS（沿用 v4/v5，未改动）

**市价 except 分支**（create_order 抛异常路径，L7131-7136）——见改动 1 的 AFTER
（已写成完整原文 + CAS 替换，不再有 `...` 占位）。
**限价平仓 rollback（L7668-7676）**：同样替换为 CAS 调用（调用点 2/2），
`close_op_id` 取自限价平仓 BEGIN 的返回值。

#### 3v6-5：CAS 资格判定（四条全过才执行，与 BEGIN 对称）

```text
1. batch 仍存在                        → 否则 'batch_missing'
2. disk.close_op_id == 我这次的 op_id  → 否则 'op_id_mismatch'（已被接管）
3. disk.close_phase 仍 == 1            → 否则 'phase_changed'（已被推进）
4. 无 settled_by_limit_close 事实      → 否则 'settled_fact_present'（绝不降级）
```

只改三个字段：`close_phase=0 / pending_close=False / is_programmatic_cancel=False`。
`close_op_id` / `close_reason` **保留**（取证痕迹，供人工核对谁发起过这次平仓）。

**完整事务闭环推演**（你的 §二 场景）：

```text
T1 BEGIN: phase 0→1, op=OP1        ✅ claim 成功
T2 BEGIN: 见 phase=1               ❌ 'close_phase_not_zero' → 不发任何订单
T1 下单 → 确认 → settle: phase=2 + settled=True
T1 若此时尝试回滚: 见 phase=2      ❌ 'phase_changed' → settled 事实不被降级
```

`settled=True + close_phase=0` 的自相矛盾状态**不再可能出现**。

#### 3v6-6：同方向单飞对运维的影响（**你已批准**，此处仅存档）

§六 的"同方向一次只允许一个自动 close transaction 在途"**会改变现有运维行为**：
若某批次处于 `limit_pending_normal`（限价挂单数小时不成交），同方向其他批次
在此期间**无法通过程序自动平仓**，必须先等它成交或人工处置。

**你的裁定（v6 复审原文）**：

> 批准。……Fail-Closed 阻断自动平仓 > 冒险并行执行两个同方向 close transaction。
> 这项不用再改方向。

保留的三条安全网（原样不动）：

1. `close_reason='limit_pending_normal'` 的批次**不 critical**（改动 4），避免刷屏；
2. BEGIN 被拒时**发 critical**（本改动 3v6-1），人工立即知道"为什么平不掉"；
3. **⚠️ 已知运维影响（非缺陷）**：事故批次 2 目前就是 `close_phase=1` 卡死状态。
   部署后，**同方向其他批次将无法程序自动平仓**，直到批次 2 被人工处置。
   这是 Fail-Closed 的预期表现（宁可不平，不可错平），且会由第 2 点的 critical
   显式暴露，不会静默。**落生产前需先人工处置批次 2。**

#### 3v6-7：`close_reason` 的取值域与写入时机

| 值 | 写入时机 | 冻结告警行为（改动 4） |
|---|---|---|
| `market_confirming` | 市价平仓 BEGIN（3v6-1） | 只 print（在途数秒~数十秒，正常） |
| `limit_pending_normal` | 限价平仓 BEGIN（3v6-1） | 只 print（挂单数小时，正常） |
| `market_confirm_unknown` | 六态落入 PARTIAL/PENDING/UNKNOWN/NOT_CONFIRMED 时 | 🚨 critical（60 分钟去重） |
| `market_partial` | （保留值，PARTIAL 处置细化时启用） | 🚨 critical |
| `settlement_stuck` | （保留值，结算卡死排查用） | 🚨 critical |
| `market_entry_unknown` | **v6.1**：市价 ENTRY gate 失败（改动 1 AFTER，`_set_close_reason_if_current` 写入） | 🚨 critical |
| `limit_entry_unknown` | **v6.1**：限价 ENTRY gate 失败且 CAS 回滚失败（改动 1d） | 🚨 critical |
| **（缺失）** | 部署前遗留的冻结批次（如事故批次 2） | **按 stuck 处理 → critical（fail-noisy）** |

#### 3v6-8：不动 `cancel_open_orders` 的 L6927-6929（明确边界）

`cancel_open_orders`（无已成交层 → 撤全部 + 终止批次）也会写 `close_phase=1`，
**但本轮不动它**，理由：

- 它不是 close transaction（不产生平仓单、不走六态确认、不需要回滚）；
- 因此它写入的 `close_phase=1` **不带 `close_op_id`** ⇒ 任何 CAS 回滚对它
  **恒拒绝**（`op_id_mismatch`）——这正是我们想要的 fail-safe 方向：
  撤单终止后不允许任何平仓路径反向解除标记；
- 改动面不扩大（符合你"不再扩大审查面"的要求）。

---
### 改动 4：P0 冻结分支告警按 `close_reason` 分型（v3 改动 8 修订；你的 §七）

**你批准的目标不变**：冻结不能 Fail-Silent。**修订点**：v3 把 critical 挂在
`close_phase >= 1 or pending_close` 上，而正常限价平仓也会 close_phase=1 挂单数小时
——第一次进冻结循环就 critical、之后每 60 分钟一次，会把设计中的正常 closing 状态
误报成异常冻结，critical 通道很快失去信噪比。

v5 以 `close_reason` 分型（写入点见改动 3v6-1 的 BEGIN 与 3v6-7 的取值域）：**只有
`market_confirm_unknown` / `market_partial` / `settlement_stuck` 才 critical**（v6.1
白名单 +2：`market_entry_unknown` / `limit_entry_unknown` —— ENTRY gate 失败的异常态）；
`limit_pending_normal` / `market_confirming` 只保留 print。**无 reason 的遗留冻结
（部署前已 close_phase=1 的批次，如事故批次 2）按 stuck 处理 → critical**——
宁可误报不可漏报，部署后第一次进监控循环就会把存量冻结暴露出来。

#### BEFORE（源码原文，L5244-5248）

```python-frag
                if close_phase >= 1 or b_data.get('pending_close'):
                    print(f"  └─ 🧊 [P0 冻结] 批次 {batch_id} 处于平仓流程"
                          f"(close_phase={close_phase})，本轮跳过保护单维护")
                    continue
```

#### AFTER

```python
                _b_close_phase = int((latest_b_data or {}).get('close_phase', 0) or 0)
                if _b_close_phase >= 1 or (latest_b_data or {}).get('pending_close'):
                    _close_reason = ((latest_b_data or {}).get('close_reason')
                                     or 'settlement_stuck')  # 缺失 = 遗留冻结 → fail-noisy
                    print(f"  └─ 🧊 [P0 冻结] 批次 {batch_id} 处于平仓流程"
                          f"(close_phase={_b_close_phase}, reason={_close_reason})，"
                          f"本轮跳过保护单维护")
                    # 🆕 v5（你的 §七）：close_phase>=1 ≠ 异常冻结，
                    # 只有 unknown/stuck 类原因才 critical（60 分钟去重）。
                    # v6.1：白名单 +2（market_entry_unknown / limit_entry_unknown）
                    # —— 新 reason 若不在白名单会 fail-silent（只 print），
                    # 与「异常冻结 fail-noisy」矛盾（交叉审核修正2 的连带坑）。
                    if _close_reason in ('market_confirm_unknown', 'market_partial',
                                         'settlement_stuck',
                                         'market_entry_unknown', 'limit_entry_unknown'):
                        if time.time() - self._freeze_alerted.get(batch_id, 0) >= 3600:
                            self._freeze_alerted[batch_id] = time.time()
                            self.send_tg_notification(
                                f"🚨【资金安全】批次平仓流程卡死，保护单停止维护！\n"
                                f"🆔 批次: `{batch_id}`\n"
                                f"🧊 close_phase={_b_close_phase}, reason={_close_reason}\n"
                                f"⚠️ 该批次的 SL/TP 不再被补挂 / 换挂 / 降级恢复。\n"
                                f"💡 请人工核对持仓与挂单，必要时手动平仓。",
                                level='critical')
                    continue
```

#### 配套 1/1：`__init__` 增加去重字典（紧邻 L224）

```python
        # 🔥 运行时安全补丁 v2：TP 参数无效 critical 告警去重（键=batch_id，60 分钟窗口）
        self._tp_invalid_alerted = {}
        # 🔥 v4（B-02）：平仓流程卡死告警去重（键=batch_id，60 分钟窗口）
        self._freeze_alerted = {}
```

**⚠️ 这一处只能让冻结"可见"，不能让冻结"自愈"。** 真正的自愈（启动时 reconcile
`close_phase`、或冻结超时后自动回滚）不在本次最小必改集内，列为后续项：
`bot_runner.py` / `watchdog.py` 目前对 `close_phase` 零引用，重启不修复。

---
### 改动 9（v5 新增）：`_converge_batch_orders_before_clear` 两处 `or []` → CONVERGENCE_UNKNOWN

**你的原话**：「它本身就是最终 clear proof 生产者，那里不应该存在 `UNKNOWN → EMPTY`」。

**为什么这是真漏洞不是洁癖**：`_safe_api_call`（L1160）在底层 `func()` 返回 `None`
时**不抛异常、原样返回 None**（只有抛异常才走重试 / AUTH_BLOCKED 分支）。所以
`fetch_open_orders(...) or []` 在查询静默失败时会把 UNKNOWN 变成 EMPTY →
`_open_map` 空 → L1/L2/L3 全部无事可做 → 产出 proof → clear。

两处位置与同一改法（L7287-7290 首扫、L7408-7411 复扫）：

```python
        # BEFORE（L7287-7290）
        try:
            _normal = self._safe_api_call(self.exchange.fetch_open_orders, symbol) or []
            _stops = self._safe_api_call(self.exchange.fetch_open_orders, symbol,
                                         params={'stop': True}) or []
        except Exception as e:
            self._converge_alert(('scan_unknown', symbol, batch_id),
                                 f"🚨【资金安全】批次 `{batch_id}`({symbol}) 收敛扫描失败"
                                 f"（CONVERGENCE_UNKNOWN），本轮不 clear，下周期重试。\n"
                                 f"错误: {e}", level='critical')
            return None

        # AFTER（v5）
        try:
            _normal = self._safe_api_call(self.exchange.fetch_open_orders, symbol)
            _stops = self._safe_api_call(self.exchange.fetch_open_orders, symbol,
                                         params={'stop': True})
        except Exception as e:
            self._converge_alert(('scan_unknown', symbol, batch_id),
                                 f"🚨【资金安全】批次 `{batch_id}`({symbol}) 收敛扫描失败"
                                 f"（CONVERGENCE_UNKNOWN），本轮不 clear，下周期重试。\n"
                                 f"错误: {e}", level='critical')
            return None
        # 🆕 v5（你的 §四）：UNKNOWN ≠ EMPTY。非 list 响应不得退化成"没有挂单"，
        # 否则 proof 会在查询根本没成功的情况下被生产出来。
        if not isinstance(_normal, list) or not isinstance(_stops, list):
            self._converge_alert(('scan_unknown', symbol, batch_id),
                                 f"🚨【资金安全】批次 `{batch_id}`({symbol}) 收敛扫描返回"
                                 f"非列表结构（normal={type(_normal).__name__}, "
                                 f"stops={type(_stops).__name__}）——UNKNOWN≠EMPTY，"
                                 f"本轮不 clear，下周期重试。", level='critical')
            return None
```

复扫同法（变量名 `_n2` / `_s2`，alert key 用 `'rescan_unknown'`，文案改为
「撤单后复扫」）。

**为什么不算"顺手重构"**：这两行与 ENTRY helper 是**同一条漏洞链的两半**
（前门 = 新增 helper，后门 = 既有 converge）。只修前门不修后门等于没修——
正是你 §四 指出的那条最坏链。改动宽度 = 2 处 × 6 行，不改任何判定规则
（异常分支的 `return None` 语义原样保留）。

---
## 三、✅ 已闭合（v6）：限价平仓的 ENTRY 撤单时机不能一刀切

> ## ✅ v6 状态：本节**已闭合**，不再是未决项
>
> 你在 v5 复审里的原话：
>
> > 「**ENTRY clear gate + converge 后门：批准。**」
> > 「这实际上也更符合此前已经批准的**甲方案**核心：
> > *市价：平仓确认 → 撤 ENTRY → 验证 ENTRY。*」
>
> 因此**甲方案已被采纳**。本节保留原文仅作决策存档，v6 的落点如下：
>
> | 路径 | v6 处理 | 依据 |
> |---|---|---|
> | **市价** | 改为「CONFIRMED_FULL → 撤 ENTRY → 逐 ID 验证 → gate=True 才撤 TP/SL」 | 你 §二 的阻断项 2（见改动 1 AFTER） |
> | **限价** | ~~零改动~~ **v6.1 已改（改动 1d）**：「尝试撤 ENTRY」升级为撤销确认 gate，位置仍在 TP/LIMIT 之前 | v6 稿此处「本就是 gate」表述错误（你的 P0-3：生产是 try/cancel + `except: pass`）；**SL 全程不撤**（L7639）不变；甲方案「ENTRY 在 LIMIT 之前」不变 |
>
> 缺陷 C（平仓失败时已撤的 ENTRY 无法自动恢复）在限价路径按甲方案处置为
> **失败可观测**（critical 明确告知「已撤销的 ENTRY 无法自动恢复，需人工确认是否重挂」），
> 而不是改顺序——理由见下方「关键差异」：改顺序会给限价路径新增一个
> **长度不可控的加仓窗口**（限价单可能挂几小时）。
>
> **若你对「限价仍保持先撤 ENTRY」有异议，请直接指出；否则本节无需再裁定。**


你的 §六 给的事务顺序（平仓成功 → 撤 ENTRY → 校验清零）我完全同意，**但对市价平仓成立，对限价平仓不成立**。

### 限价平仓 BEFORE（源码原文，L7541-7549）

```python-frag
        try:
            # 先撤销所有未成交的开仓条件单
            entry_orders = target_b_data.get('entry_orders', [])
            for idx, order_id in enumerate(entry_orders):
                if idx >= last_filled_count:
                    try:
                        self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol, params={'stop': True})
                        print(f"  └─ 已撤销开仓挂单: {order_id}")
                    except Exception:
                        pass
```

### 关键差异

| | 市价平仓 | 限价平仓 |
|---|---|---|
| 平仓动作 | 即时成交 | **挂单，可能长期不成交** |
| 若保留 ENTRY 到平仓后 | 无窗口（平仓已完成，仓位归零） | **挂单等待期间 ENTRY 可能继续成交加仓** |
| 后果 | — | 平仓单数量 < 实际持仓 → 平仓后残留未平仓位 |

限价平仓"先撤 ENTRY"是**刻意的正确设计**：防止平仓单挂出后到成交前这段时间里继续加仓。
把它挪到"挂单成功后"，等于新增一个加仓窗口，且窗口长度不可控（限价单可能挂几小时）。

### 我的建议（已体现在改动 5b）

**限价平仓保持先撤 ENTRY**，缺陷 C 在限价路径改用「**失败可观测**」而非「改顺序」：
挂单失败回滚时 critical 告知"已撤销的 ENTRY 无法自动恢复，需人工确认是否重挂"。

理由：改顺序引入的是**新的交易风险**（残留仓位），不改顺序留下的是**已知的运维负担**（人工补挂 + 已有告警）。
按 Fail-Safe，选后者。

### 三个候选（v6：已选**甲**，本节存档）

| 候选 | 描述 | 我的评价 |
|---|---|---|
| **甲（我倾向）** | 市价改顺序+校验；限价保持顺序+失败告警 | 零新增交易风险 |
| 乙 | 两条路径都改成平仓后撤 | 限价路径新增加仓窗口，不推荐 |
| 丙 | 限价路径改为"先撤 ENTRY，挂单成功后校验 ENTRY 确已清零" | 只加校验不改进顺序，成本最低但缺陷 C 仍在 |

---

## 四、附录：你 §八 的 P0 升级条件 —— 源码实证结果

你说"不要猜，专门验证"。我做了，**结论是 B 应从 P1 升到 P0**。

### 情况 A：保护单可能因冻结而不被维护 → ✅ 成立

L5244-5248 的 `continue` 跳过的范围，是 L5250 之后的**全部保护单维护**：

```python-frag
5244→                _b_close_phase = int((latest_b_data or {}).get('close_phase', 0) or 0)
5245→                if _b_close_phase >= 1 or (latest_b_data or {}).get('pending_close'):
5246→                    print(f"  └─ 🧊 [P0 冻结] 批次 {batch_id} 处于平仓流程"
5247→                          f"(close_phase={_b_close_phase})，本轮跳过保护单维护")
5248→                    continue
```

被跳过的包括：SL 缺失补挂（L5257+，含 F3 裁决收养）、SL 在场性校验（L5281+）、
TP R14 补挂、保本移动、滚动止损。

**含义**：冻结期间若 SL 因任何原因不在场（人为撤、交易所拒单、API 失败、强平），
系统**不会重建** → 裸仓无下行保护。当前批次2 未触发，是因为 SL/TP 恰好都还在场（实证），
**这是运气，不是设计保证**。

### 情况 B：保护单触发可能开反向仓 → 🟡 倾向不成立，但证据强度不足

源码事实：hedge 模式下 SL/TP 的 params 均派生自 `params_base`（含 `positionSide`），
且 **L2292-2293 只在非 hedge 时加 `reduceOnly`**。即 hedge 模式下保护单靠 `positionSide` 锁定方向，
`SELL + positionSide=LONG` 语义为"平多"。

交易所语义（外部证据，非本账户实盘）：
- Binance 开发者社区官方讨论（dev.binance.vision）：hedge 模式下 `positionSide=LONG` 的 SELL
  在无 LONG 仓位时被**拒绝**（"Reduce Only order rejected"），不会创建反向仓位
- nautilus_trader 文档：Binance hedge mode 必须 `use_reduce_only=False`（reduceOnly 不被支持）

**我不据此下结论**：该讨论场景是 signal bot（webhook），与直接 API 下单不完全等价，
且我们没有任何本账户的实盘样本。**按纪律，标为"待交易所语义确认"，不用于升/降级**。

### 情况 C：冻结状态下仓位变化无法重建保护 → ✅ 成立（与 A 同源）

冻结只跳过维护，结算路径（成交检测 / 持仓归零）仍在跑（注释明示冻结点位于其后）。
但"仓位变化后需要新建/换挂保护单"的分支全部被跳过 → 与 A 同一根因。

### 情况 D：重启无法解除 + 无告警 → ✅ 成立

- `bot_runner.py` grep `close_phase` **无启动自愈**（上轮已确认）
- 冻结分支只有 `print`，**不发 TG** → 用户无从得知批次已进入永久冻结
- 本事故实证：19:32:50 后 `close_phase=1` 持续，19:36 三次采样无变化，全程零告警

### 结论

**A、C、D 明确成立 → 按你的规则（任一成立即 P0），B 应定级 P0。**

补充一条我的判断：B 之所以此前没被判 P0，是因为"看起来只是状态没回滚"。
但实质是——**一次任意原因的平仓失败，会让该批次永久失去保护单维护能力，且重启不修复、无告警**。
这不是显示问题，是交易行为的持续改变。

---

## 五、改完后的验证计划（采用你的 §十六 顺序，v5 全文重写 + v6 新增 Test 8/8b/9/10）

**先拆开验证，不做"一步到位的完整实盘测试"** —— 便于定位，也避免一次失败牵连多个修复点。

### 静态与回归（每个 Test 之前都要跑）

| 项目 | 命令 | 判据 |
|---|---|---|
| 离线场景测试 | `.venv/Scripts/python.exe test_close_confirmation_v6.py` | rc=0（**含 v5/v4/v3 负向对照**） |
| 文档代码块 | `.venv/Scripts/python.exe check_doc_code_blocks.py` | rc=0（ast.parse **+ 无 `...` 占位**） |
| helper 一致性 | `.venv/Scripts/python.exe check_doc_helper_parity.py` | rc=0（**10 个 helper 逐函数 `ast.dump` 同构** + 无重名重复定义） |
| 全量回归 | 既有 `test_*.py` | **退出码逐项记录**（orphan_guard rc=42 单列，不并入失败统计） |
| 生产零改动 | `git diff --stat HEAD -- trader_260725.py watchdog.py bot_runner.py` | 输出为空 |

**全量回归基线（2026-08-30 实测，42 个 test_*.py）**：**40 个 rc=0**；
2 个 rc=1 且**均为 v3 已撤销机制的遗留测试，不是本轮回归**：

| 文件 | 测试对象 | 为什么现在必然 rc=1 |
|---|---|---|
| `test_ast_rollback_guard.py` | 断言 `allow_flag_rollback` 调用点恰好 2 处 | v4 已按你 §二 建议**整体撤销** escape hatch → 生产 0 处 → 守卫目标不存在 |
| `test_merge_rollback_semantics.py` | `_merge_batch_state` 的 escape hatch 回滚语义 | 同上，机制已撤销 |

⚠️ **这两个文件应在 v5 落生产的同一批次里归档（标注 DEPRECATED 或移入 Trash/）**，
否则会长期污染回归统计。本轮不动（遵守未确认不改码纪律）。

### Test 1：纯参数验证（不建仓，最小代价）

在 Hedge Mode 下确认 `MARKET SELL + positionSide=LONG` 不再 -4061。
**离线先跑** `test_close_confirmation_v6.py`（v5 的 52 场景全部保留）：

| 组 | 场景 | 覆盖的终审项 |
|---|---|---|
| C1-C7 | 六态判定（CONFIRMED_FULL / PENDING / PARTIAL / TERMINAL_ZERO / NOT_CONFIRMED / UNKNOWN） | §一 |
| T1-T4 | TERMINAL_ZERO 收紧（closed+0→UNKNOWN、filled 缺失→UNKNOWN、权威 0→TERMINAL_ZERO） | §五 |
| **T1-v4 / T2-v4** | **负向：v4 在这两种情形会给回滚资格** | §五（证明修正是实质修复） |
| B1-B10 | BEGIN claim（首次成功 / 二次拒绝 / 同方向在途 / uuid 唯一 / BEGIN→CAS 闭环 / 假冒 op_id） | §二 §三 §六 |
| G1-G7 | 归因守卫 | §一 §六 |
| **G6 / G6-v4** | **决定性例子（target 已 phase=1）：v5 拦截 / v4 放行** | §一（核心） |
| P1 / P1-v4 | `_read_position_amt` 非 list → None（v4 返回 0.0） | §八-1 |
| E1-E4 / E1-负向 | ENTRY 双层验证 + v3 的 `or []` 假确认 | §三 |
| N-* | v3 的 not_filled 过宽 | §一 |
| **D1-D5 / D-neg** | **claimed snapshot 派生 + v5 旧快照负向（只平一半）** | v6 §一 |
| **O1-O3 / O1-v5 / O2-v5** | **ENTRY `OrderNotFound` 收紧（v5 判 gone → 放行）** | v6 §二 |
| **S1-S2 / S1-v5 / S2-v5** | **市价事务顺序：撤 SL/TP 必须最后（v5 撤 2 次）** | v6 §二（核心） |

### Test 2：正常市价平仓事务

确认顺序（**v6 语义**；v5 曾把 ENTRY 放在 SL/TP 之后，已前移）：

```text
ticker → atomic BEGIN（claim + close_op_id + close_reason + claimed 快照）
→ 以 claimed 快照派生 transaction 变量（改动 1c）
→ pre-read 敞口 → 归因守卫 → MARKET 下单 → fetch_order 按单确认(CONFIRMED_FULL)
→ ENTRY 撤销 → ENTRY 快照+逐 ID 双层验证（False 则禁止 clear、SL/TP 保留）
→ SL/TP 撤销
```

判据：日志出现「按单确认 filled=…」与「ENTRY 撤单已交易所侧校验通过（快照 +
逐 ID 终态）」；结算金额 = `confirmed_filled_amount`（订单实际成交量），
`_record_realized_pnl` 入账与之相等；账本出现 `close_op_id`（32 位 hex）。
**顺序判据**：日志里 ENTRY 的两行必须**早于**「已撤销止盈单 / 已撤销止损单」。

### Test 3：人为制造市价平仓失败（create_order 抛异常路径）

确认：**不撤 ENTRY、不丢 SL/TP**，且 CAS 回滚成功后

```text
close_phase=0  pending_close=False  is_programmatic_cancel=False
close_op_id 保留（取证痕迹）
```

日志出现「CAS 原子回滚成功」。前置：`test_close_confirmation_v6.py` B1/B9/R*。

### Test 3b：未成交路径（TERMINAL_ZERO）

用远高于市价的价格挂 LIMIT 平仓单再立即取消（或 mock），制造
「create 成功 → canceled + 权威 filled=0」：确认走 TERMINAL_ZERO → CAS 回滚 →
批次回 ACTIVE，SL/TP 原封不动。
**新增前置判据（§五）**：若构造出的是 `closed + filled=0`，v5 必须判 **UNKNOWN
（不回滚）** —— 用 `T1` 场景离线锁定，实盘不得依赖这种矛盾组合。

### Test 4：限价平仓失败

确认：BEGIN claim → ENTRY 先撤 → LIMIT 创建失败 → CAS 回滚 → critical 告警
→ **不误清 `settled_by_limit_close`**（CAS 资格第 4 条拒绝该场景）。

### Test 5：归因冲突注入（真实调用顺序版，v5 修正）

人工把台账改错（同方向两批台账 0.001+0.001、实际总敞口 0.001），点市价平仓：
确认被 `_close_amount_guard` 拦截（不发单）+ 🚨critical「归因冲突」。
⚠️ 改台账必须停 bot 后操作（运维铁律）。
**v5 关键点**：本测试必须在 **target 已 `close_phase=1`** 的状态下进行——
这正是 v4 会漏过的场景（离线 `G6-v4` 已复现其放行行为）。

### Test 6（v5 新增）：双 close 并发（§二 阻断项的核心负向）

**离线**：`test_close_confirmation_v6.py` 的 B1-B10。

**实盘**：同一批次连点两次「市价平仓」按钮（两个 TG callback 各起一个
`run_in_executor` 线程，生产 `bot_runner.py` L1492 起）：

```text
判据：
  ✅ 只有一个线程取得所有权（日志/账本只出现一个 close_op_id）
  ✅ 另一个立即返回「未取得平仓事务所有权（close_phase_not_zero）」
  ✅ 交易所侧**只出现一张平仓单**（verify_no_duplicate_orders.py --snapshot/--compare
     比对订单 ID 集合，而非数量）
  ✅ 不存在「第二张单平到另一批次仓位」的情形
```

**这是 v4 无法通过的测试**（v4 无 BEGIN，两个线程都会下单）。

### Test 7（v5 新增）：同方向单飞

批次 X 挂限价平仓（`limit_pending_normal`）期间，对同方向批次 Y 点市价平仓：
确认 Y 被 BEGIN 拒绝（`same_side_close_inflight`）+ critical，且 **Y 未发出任何订单**。
反向对照：对**反方向**批次点平仓，确认不受影响（离线 B4）。


### Test 8（v6 新增）：claim 与 transaction snapshot 绑定（你的 §一）

**离线**：`test_close_confirmation_v6.py` 的 **D 组 22 项**：
D0-D5b（13 项）+ D-neg（2 项）+ **D6-D6b（2 项）+ D7/D7-v5（2 项）+ D8/D8b/D8-v5（3 项）**。

`D-neg` 复现 v5 的调用范式（缺陷版）：

```text
入口读: last_filled_count=1 → current_filled_amount=0.001
监控线程（BEGIN 之前）: 第 2 层 ENTRY 成交 → last_filled_count=2（落盘）
BEGIN: 锁内读到最新状态 → claim 成功
v5 范式下单: 0.001（只平一半，残留 0.001 无保护）   ← 实测
v6 范式下单: 0.002（claimed 快照派生）              ← 期望
```

**实盘**：开一个多层阶梯批次，在第 2 层 ENTRY 成交的瞬间点市价平仓
（低成本替代：停 bot → 手工把 `last_filled_count` 改大并落盘 → 启动后立刻点平仓）：

```text
判据：
  ✅ 日志「平仓数量」= 更新后的层数合计，不是入口读到的旧值
  ✅ 交易所侧平仓单数量与之相等（不残留未平的那一层）
  ✅ 若 claimed 快照显示无需平仓（no_filled_amount）→ 不发单 + CAS 回滚 + critical
```

#### Test 8b（v6 自查新增）：10 个字段同源 —— 孤儿保护单负向

你 §一 点名 4 个字段，我自查发现实际是 **10 个**（监控线程 L6231-6254 一次 update 写 8 个）。
其中 `entry_orders` / `tp_order_id` / `current_sl_id` 不参与「算平多少」而参与「撤哪些单」，
漏掉会直接产生**孤儿保护单**。

**离线**：`D6/D6b`（契约完整）+ `D7/D7-v5`（静态来源）+ `D8/D8b/D8-v5`（运行时调用序列）。

`D8` 构造：入口快照 `current_sl_id=SL_1`，claimed 快照 `current_sl_id=SL_2`
（模拟监控线程在 BEGIN 之前已把 SL 移走）。同一输入下 v6 / v5 的实测调用序列：

```text
v6: create_order → fetch_order:OID1 → cancel:E2 → fetch_order:E2 → cancel:TP2 → cancel:SL2
v5: create_order → fetch_order:OID1 → cancel:TP1 → cancel:SL1 → cancel:E2 → fetch_order:E2
                                       ↑ 撤的是已被监控线程撤掉的旧保护单
                                       ↑ 真正的 SL2 从未被撤 → 孤儿单
                                       ↑ 且发生在 cancel:E2 之前（§二 顺序问题同源可见）
```

判据：
  ✅ v6 命中 `cancel:SL2` / `cancel:TP2`，且**不含** `cancel:SL1`
  ❌ v5（负向）命中 `cancel:SL1` / `cancel:TP1`，且**不含** `cancel:SL2` → 孤儿单成立

**实盘**（可选，成本较高）：建仓 → 触发一次滚动止损/保本移 SL → 立刻点市价平仓：

```text
判据：
  ✅ 交易所侧被撤的是「移 SL 之后的那张新止损单」，批次结束后无残留止损单
  ❌ 若残留 → 说明撤的是旧 SL id（v5 行为）
```

### Test 9（v6 新增）：市价事务顺序 —— SL/TP 必须最后撤（你的 §二 核心负向）

你点名要的两个断言都做了，**一个静态、一个运行时**，都拿 v5 的 AFTER 存档做负向对照。

**（a）运行时调用序列断言**（你建议的做法）：把改动 1 的 AFTER 代码块包装成可执行
函数，注入 FakeExchange 记录**每一次交易所调用**，构造
「MARKET 已 CONFIRMED_FULL + 一张 ENTRY 恰好在这期间成交」：

```text
断言调用顺序:
    create_order(MARKET)
  < fetch_order(MARKET)            # 按单确认
  < cancel_order(ENTRY)
  < fetch_open_orders              # 快照第 1 层
  < fetch_order(ENTRY)             # 逐 ID 第 2 层 → filled
断言（关键）:
    cancel_order(TP) 调用次数 == 0
    cancel_order(SL) 调用次数 == 0
```

**（b）静态 AST 顺序断言**：解析改动 1 的 AFTER 代码块，断言

```text
lineno(_confirm_close_filled) < lineno(_cancel_and_verify_entry_orders)
                              < min(lineno(cancel_order ×2))
```

**负向对照（两者都做）**：对 v5 的 AFTER 存档
（`送审附件_v6.1/v5_after_market_close.py`，文档被 v6 覆盖前固化的原文）跑同一断言：

```text
v5 (a): 撤 TP/SL = 2 次，且发生在 verify ENTRY 之前  → 断言失败 ✅（对照有效）
v5 (b): lineno(cancel_order)=347/355 < lineno(gate)=365 → 断言失败 ✅
```

**实盘**：无低成本复现方式（需要 ENTRY 在 MARKET 成交后的秒级窗口内成交）。
离线断言已锁死顺序；实盘改为人工核对日志中 ENTRY 两行早于 SL/TP 两行。

### Test 10（v6 新增）：ENTRY `OrderNotFound` 收紧（你的 §二 小点）

**离线**：**O1/O2 + O1-v5/O2-v5 负向 + O3 正常路径**。

```text
O1     : fetch_order(ENTRY) 抛 OrderNotFound → v6 判 'unknown'
O1-v5  : 同一输入 v5 判 'gone'                      ← 负向复现
O2     : _cancel_and_verify_entry_orders 整体 → v6 返回 False（保留 SL/TP）
O2-v5  : 同一输入 v5 返回 True（放行 → 会撤 SL/TP）  ← 负向复现
O3     : 正常 canceled → 两者都 True（证明正常路径未受影响）
```

**实盘**：无需专门构造。部署后若 ENTRY 查不到，预期表现是
🚨critical「ENTRY 收敛未确认」+ **SL/TP 保留** + 批次冻结 —— 不再静默继续。

---

### Test 11（v6.1 新增）：persist 契约 / 台账对称校验 / 限价 gate / 异常 reason / checker 假绿

**离线（全部先对 v6.0 RED、再对 v6.1 GREEN）**：

```text
B11/B12   : persist=False → BEGIN 拒发下单资格 / rollback 拒报 rolled_back
B11-v60   : 同一输入 v6.0 照样 ok=True（发下单资格）          ← 负向复现
B12-v60   : 同一输入 v6.0 谎报 rolled_back（磁盘仍 phase=1）  ← 负向复现
B13/B13b  : _set_close_reason persist=False → ok=False + persist_failed
            （C 路存活变异体，P0-1 同型盲区锁死）              ← 自审追加
D3c       : last_filled=2 配 target_amounts=[0.001] → target_amounts_short
D3c-v60   : 同一输入 v6.0 派生 0.001 放行（少平一半实证）      ← 负向复现
D3d       : side='HOLD' → side_invalid（不得默认 BUY）
D6        : set(vars.keys()) == EXPECTED_TXN_KEYS（11 键 exact set）
D6c/D6d   : 有未成交计划层 + entry_orders 缺失/部分截断 → Fail-Closed
D6c-v60   : 同一输入 v6.0 归零放行（UNKNOWN→EMPTY）           ← 负向复现
D6e       : 🗑️ 截断签名（len==last_filled_count < target 5 层）→ 放行
            （F-1 回归锁：生产 L6896-6897 合法状态）           ← 自审追加
D6f       : 已成交层 ID 丢失（len < last_filled_count）→ 仍拦  ← 自审追加
D6g/D6gb  : 🗑️ 批次完整链 BEGIN→derive→gate 全通过且零撤单     ← 自审追加
D9        : 完整集成链 —— stale 磁盘 → 模拟监控更新（加 E3、移 SL2）
            → 真 BEGIN → 真 derive → rebind → 跑文档 AFTER
            → 断言撤 E2 **且 E3**、TP2、SL2（非注入式，重走生产整合顺序）
D9-neg    : 同一链路故意喂 stale target_b_data（模拟整合时漏掉
            target_b_data=_claimed）→ E3 漏撤**必须被检出**
            （D8 的注入式看不到这条，D9 看得到）
L1-L5     : 限价 gate —— L1 通过才放行（序列：cancel E < fetch_open_orders
            < fetch_order E）/ L2 OrderNotFound → 不撤 TP、不挂 LIMIT、
            CAS 回滚恢复监控 / L3 fetch_open_orders=None → 同上 /
            L4 ENTRY 已 filled → 同上 / L5 正常序列锁死
S2e/S2f   : 市价 gate 失败 → persisted 最新快照
            close_reason == 'market_entry_unknown' 且 != 'market_confirming'
M1        : check_doc_helper_parity.py --self-test（extra helper /
            duplicate doc helper 两个 mutation 都必须 rc=1）
```

**实盘**：无需专门构造。部署后 ENTRY gate 失败的预期表现变为 🚨critical +
`close_reason=market_entry_unknown` + **每 60 分钟周期 critical**（改动 4 白名单），
不再只 print 一次就沉默。限价 gate 失败则相反方向：批次**回滚恢复正常监控**
（TP/SL 全程未动），critical 告知人工核对残留 ENTRY。

---

## 六、未改动声明

`git diff --stat HEAD` 为空，HEAD = `e953d79`。生产三文件（trader / bot_runner / watchdog）零改动。
批次2 保持事故现场不动（LONG 0.001 @77692.6 + SL 75001 + TP 80000 均在交易所）。

**v5 明确不动的部分**：

| 区域 | 状态 | 理由 |
|---|---|---|
| `_merge_batch_state` / `save_batch_state` | 零改动 | escape hatch 方案已撤销（棘轮原样保留） |
| `cancel_open_orders` 的 `close_phase=1`（L6927-6929） | 零改动 | 非 close transaction，不带 `close_op_id` ⇒ CAS 对它恒拒绝（fail-safe 方向），见 3v6-8 |
| `_converge_batch_orders_before_clear` 的判定规则 | **仅改两处 `or []`** | 异常分支 `return None` 语义原样保留（改动 9） |
| 限价平仓撤 ENTRY 的顺序 | 保持原顺序 | 甲方案：限价挂单可能长期不成交，挪后会新增"挂单期间 ENTRY 继续成交"窗口（§三 已裁定） |
| `bot_runner.py` / `watchdog.py` | 零改动 | 冻结自愈不在本轮最小必改集（列为后续项） |

**v6 明确不动的部分**（我自己核实后加的边界）：

| 区域 | 状态 | 理由 |
|---|---|---|
| 限价平仓的 **ENTRY gate 位置** | ~~零改动~~ **v6.1 已改（改动 1d）** | ⚠️ v6 此处「生产本就是撤 ENTRY(gate)」表述**错误**（你的 P0-3）：生产 L7543-7549 是 try/cancel + `except: pass`，无验证无 gate。v6.1 升级为撤销确认 gate，**位置仍在 TP/LIMIT 之前**（甲方案不变）；SL 全程不撤（L7639）维持不变 |
| 市价平仓的 `except` 通道 | 零改动 | ENTRY gate 失败改走直接 `return`（不进 except），异常通道语义完全不变 |
| `_converge_batch_orders_before_clear` | 沿用 v5 改动 9 | 后门已堵，v6 不再触碰 |

**v5 新增但不改变既有判定语义的部分**：`_record_realized_pnl` 的 `pnl_partial`
为**新增可选参数**（默认 False，其余 5 处调用点零影响）。

---

## 七、探针实证 + 交叉审查：判据选型（v3 已推翻 v2 的 delta）

⚠️ **本节在 v3 有根本性反转**。v2 这里写的是「为什么判据只能是 delta」，并把它
作为方案的核心落点。**我在出稿后自己组织了三路子代理交叉审查，delta 被证伪了。**
探针事实（7.1/7.2）依然有效且是推理基础，但 7.3 的结论从「delta 采用」改成了
「delta 否决」。完整审查报告见仓库内 `交叉审查_A/B/C_*.md`。

### 7.1 为什么要先做探针

`trader_260725.py` 里已经有一个现成的 `_get_current_position_amt()`（L2656），
看起来可以直接拿来用。但它有一个**假确认陷阱**（以下是**结构伪代码摘录**，
`...` 为省略标记，不是可套用源码——v5 §七：含占位一律 `python-frag`）：

```python-frag
for pos in positions:
    if pos.get('symbol') == symbol or ...:
        if is_hedge_mode:
            if pos.get('side') == target_side:
                return abs(...)      # 匹配到就返回
        else:
            return abs(...)
return 0.0                            # ← 没匹配到，也返回 0.0
```

「方向没匹配上」和「确实已平仓」**都返回 0.0**。调用方若把 0.0 读作「已归零」，
就会在**持仓仍然在场**时撤掉 SL/TP → 裸仓。这比不改更危险。

所以必须先确认 ccxt 的真实返回结构，再决定 helper 怎么写。

### 7.2 实测结果（只读探针，真实账户）

探针：`G:/tmp/probe_position_shape.py`、`G:/tmp/probe_zero_position_shape.py`
（只调 `fetch_positions` / `fetch_position_mode`，零下单零撤单零写盘）

**事实 1 —— 账户确实是双向持仓**

```text
fetch_position_mode() -> {'info': {'dualSidePosition': True}, 'hedged': True}
```

**事实 2 —— ccxt 归一化结构（两种 symbol 形态返回完全一致）**

```text
symbol='BTC/USDT:USDT'  side='long'  contracts=0.001
info.symbol='BTCUSDT'   info.positionSide='LONG'   info.positionAmt='0.001'
```

`fetch_positions(['BTCUSDT'])` 与 `fetch_positions(['BTC/USDT:USDT'])` 结果相同
→ 生产里传 `target_symbol='BTCUSDT'`（state 键形态）可用，无需转换。

**事实 3 —— 假确认陷阱确实存在（用生产函数本体实测）**

把 `_get_current_position_amt` 用 `ast` 从磁盘源码原样提取、注入真实交易所执行：

```text
symbol='BTCUSDT'  is_hedge=True  side=BUY  -> 0.001   ✅ 正确
symbol='BTCUSDT'  is_hedge=True  side=SELL -> 0.0     🚨 而实际持仓 long 0.001 在场
```

**事实 4 —— ccxt 会过滤零仓位行（这条决定 helper 能不能用「查不到=零」）**

```text
raw  /fapi/v2/positionRisk  ETHUSDT → 2 行（LONG 0.000 / SHORT 0.000）
ccxt fetch_positions(['ETHUSDT'])   → 0 条
raw  /fapi/v2/positionRisk  BTCUSDT → 2 行（LONG 0.001 / SHORT 0.000）
ccxt fetch_positions(['BTCUSDT'])   → 1 条（只有 LONG）
```

→ 「查不到该 symbol 的条目」确实等价于「无敞口」，这一点可以放心使用。
但也意味着 **side 传错导致的「查不到」与「真的平掉了」在返回值上完全无法区分**。

### 7.3 判据选型（v3：持仓维度三个候选**全部否决**）

| 候选判据 | 多批次同向（D-006 最多 3 批） | side 传错 | **归因能力** | 结论 |
|---|---|---|---|---|
| 平仓后仓位 == 0 | ❌ 永远不为零 → **100% 误判** | ❌ | — | 否决 |
| side 过滤后读数 == 0 | ✅ | 🚨 **假确认 → 裸仓** | — | 否决 |
| **delta（减少量 ≥ 被平数量）** | ✅ | ✅ Fail-Closed | 🚨 **无法归因**（B-01） | **否决**（v2 曾采用，已推翻） |
| **`fetch_order(order_id)` 按单确认** | ✅ 免疫 | ✅ 免疫 | ✅ **每单独立、可归因** | **采用（v3）** |

前两行的否决理由同 v2，不再重复。**第三行是 v3 新增的否决 —— 被否决的正是我 v2 自己选的判据。**

#### 为什么 delta 必须否决：它回答的不是我们要问的问题

`_read_position_amt` 读的是 **symbol + 方向的总敞口**（对所有匹配行求和），
不是本批次的敞口。所以 `delta` 衡量的是**所有市场参与者对该 symbol 该方向的净影响**，
而方案需要知道的是「**我这张平仓单成交了多少**」。任何第三方减仓都会被记到本批头上：

- 同 symbol 同方向的**另一批次触发 SL**（`L5296` 检测 → 该批走结算）
- 用户在交易所 App 手动平仓
- 强平 / ADL
- 另一批次的限价平仓单成交（`_monitor_limit_close`）

**决定性证据（这是我认为无法辩驳的一点）**：

> delta 的**正样本**（v2 用来论证它正确的 S3：`before=0.002, after=0.001, expected=0.001`）
> 与**假确认样本**（本单 `filled=0` 根本没成交、总敞口被他方打掉的 S8）在观测数据上
> **完全同形**。两个场景在 delta 判据下**物理不可区分**——测试集里没有任何负向对照能把它们分开。

而触发条件（另一批次 SL 成交）在剧烈行情下，恰恰与「我要市价平仓」**高度同时发生**。
后果链：确认门放行 → 撤 TP、撤 SL → 本批仓位仍在且**无任何保护单** → 同时进入
`close_phase=2` → 监控冻结（`L5244`）→ 不补挂 → **裸仓 + 无告警 + 不恢复**。
这正是本次事故审计最不应该引入的结果。

#### 关键教训：项目里早有正确模式，我不该新造

改用 `fetch_order` 不是新发明——`trader_260725.py` 里已经有成熟的
**Create→Verify→Commit** 实现：`_verify_order_created`（**L3368**），它的 docstring
写得比我的方案清楚：

> Verify 必须用 fetch_order（事务确认点），不用 open_orders 快照（周期监控数据，
> 不承担事务语义）……返回三态（禁止退化为 True/False）……
> **UNKNOWN ≠ NOT_FOUND** —— 网络未知不能被当成"不存在"

而且它踩过的坑正是我要踩的：

> OrderNotFound 短窗口重试（2s × 3）：仍查不到才返回 not_found。
> 事件 3 实证：**4/4 单 create 成功但 0 秒 verify 全部 OrderNotFound 假阴性**。
> 曾致 12 处全误判 not_found → **无限重挂 24 个孤儿单**。

**v3 的 helper 因此直接复用这套三态语义，不另造一套。** 这是本轮我最该早点做的事。

### 7.4 判据测试结果（**v3 时代存档**）

> ⚠️ 本节的 11 场景是 **v3 时代**的判据选型档案，仅用于说明「为什么持仓维度三个候选全部被否决」。
> **现行测试规模**：`test_close_confirmation_v6.py` —— **87 场景 rc=0**（含 v5 的 52 场景全部保留，以及 v6 新增的 D/O/S 三组负向对照）。
> 最新验证计划见 §五（Test 1-10）。

**`test_close_confirmation_v3.py`**：11 场景 + 1 负向对照，零网络、零 API、零写盘。

| 场景 | 期望 verdict | 实测 | 说明 |
|---|---|---|---|
| S1 单批次全平（filled=0.001） | confirmed | ✅ | |
| S2 订单在场但未成交 | not_filled | ✅ | |
| **S3 本单未成交但总敞口 0.002→0.001（他方减仓）** | **not_filled** | ✅ | **B-01 核心场景** |
| S4 查询订单连续异常 | unknown | ✅ | Fail-Closed，绝不当成"没成交" |
| S5 订单不存在（重试后仍 not_found） | not_filled | ✅ | 可安全回滚 |
| S6 create 后可见性延迟（先 not_found 后成功） | confirmed | ✅ | 事件 3 实证场景，重试救回 |
| S7 部分成交（filled=0.0005 < 0.001） | not_filled | ✅ | |
| **S8 `fetch_positions` 返回 None（非异常）** | **None** | ✅ | **见下，C 高危项** |
| S9 台账 0.001 > 实测 0.0005（filled=0.0005） | confirmed | ✅ | **B-03**：expected 取 min |
| S10 数量兜底：台账 0.001 / 实测 0.0005 | 0.0005 | ✅ | Q3 兜底，防超额下单 |
| S11 数量兜底：读取失败 | None | ✅ | Fail-Closed，不发单 |

**S8 是测试自己暴露的第二个致命缺陷**：v2 的 `FakeExchange` 写的是
`return v if v is not None else []`，而 `_read_position_amt` 的 `positions or []`
会把**非异常的 None 返回**静默当成"无敞口" → 返回 `0.0` → `delta = pos_before - 0.0`
≥ expected → **判「已平仓」**。持仓根本没动却放行 → 撤 SL/TP → 裸仓。
v3 显式拦截 None 并返回 None（Fail-Closed），S8 专门钉死它。

**负向对照（证明 B-01 不是纸面推演）**——同一场景 S3 下两个判据的实际输出：

```text
  v2 delta  → confirmed=True   (敞口 0.002 → 0.001（减少 0.001，预期 0.001）)
  v3 按单   → not_filled       (订单 OID1 未成交或成交不足：status=open filled=0.0 预期>=0.001)
```

v2 在他方减仓时给出**假确认**，会撤 SL/TP → 裸仓。`test_close_confirmation_v3.py`
内置这段对照并把它纳入最终判定：若 v2 未复现假确认，测试直接判失败（防"负向对照"自己变成装饰品）。

**`test_merge_rollback_semantics.py`**（你的 §十五）

| 实现 | 结果 |
|---|---|
| 生产原样 | rc=1（可读 RED：签名中无 `allow_flag_rollback`，改动未落地） |
| `merge_after.py`（提议实现） | ✅ rc=0，6/6 |
| `merge_after_broken.py`（白名单误含 `settled_by_limit_close`） | ✅ rc=1，S2/S3 失败 |

S2 就是你要的那一条：disk 全 True、snap 全 False、`allow=True` →
`close_phase=0 / pending_close=False / is_programmatic_cancel=False`，
**而 `settled_by_limit_close` 仍为 True**。一次证明「受控通道只回滚临时状态」。

样本由 `G:/tmp/make_merge_fixtures.py` 从生产源码**抽取后做最小字符串手术**生成
（带 assert 校验锚点唯一，源码漂移会直接报错），不是手抄副本。


---

## 八、机器检查（`check_doc_code_blocks.py`）：v5 新增 Ellipsis 检测

你的 §七 指出的问题我完全接受，并且它比"补一句免责声明"更重要：

> `ast.parse == 语法合法`，不等于 `diff 可直接应用`。`...` 在 Python 里是合法语法，
> 所以 checker 会 GREEN，但把 AFTER 整块贴进生产反而会破坏生产。

**v5 的做法不是给占位加免责声明，而是让占位不再存在**：

1. 改动 1 的 AFTER 已把既有 L7115-7143 异常通道**原文完整写入**，
   `...` 占位彻底消失（你可以 grep 本文件确认 `...`# 占位零命中）；
2. checker 新增 **Ellipsis 检测**：任何 `python` 块若 AST 中含 `ast.Ellipsis`
   节点 → **直接判失败**，不允许"语法合法但含占位"蒙混过关。

运行方式（送审前必跑）：

```text
.venv/Scripts/python.exe check_doc_code_blocks.py
```

检测逻辑：

| 检查 | 对象 | 判定 |
|---|---|---|
| 语法 | 全部 ```python 块 | 四种解析策略（raw / dedent / class 包裹 / async def 包裹）任一通过即 OK |
| **占位** | 同上 | 解析成功后 `ast.walk` 找 `ast.Ellipsis` → 命中即失败 |
| 豁免 | ```python-frag 块 | 不参与（源码截断摘录，仅人工阅读） |

**块类型语义（采纳你的措辞要求，已写入稿头）**：

```text
python      = 可执行完整替换块，不含任何 `...` 占位
python-frag = 人工 diff 片段 / 源码截断摘录，豁免语法检查
```

**当前状态（v6.1 终稿实测）**：**14/14 个可执行块全部通过**（v6.1 新增改动 1b
追加 2 的 `_record_realized_pnl` AFTER 完整定义 + 追加 3 的日报片段），
另有 **10 个 `python-frag`** 摘录块豁免。

> 顺带一条自检记录：v4 出稿时这个 checker 首跑就抓出了 4 个语法错误
> （3 个源码摘录块被误判为可执行 + 改动 1 AFTER 外层 try 未闭合）。
> 你的 §七 让它又抓出一类新问题——**能自己抓住的，总比被你抓住好**。

---

## 八-B、机器检查②（`check_doc_helper_parity.py`）：v6 新增 helper 一致性核验

v6 新增这一项的动机，是我在整理 v6 时发现的**一个真实存在的交付风险**：

> 送审稿里贴的 helper 全集代码块，和 `送审附件_v6.1/new_helpers_vX.py` 里被测的那份实现，
> **没有任何东西保证它们是同一份**。文档改了 helper 没改（或反之）、文档里同一函数
> 残留两份（BEFORE / AFTER）——这两种情况我这轮都近距离碰到过。
> 一旦发生，你逐行审的文档与我跑测试的代码就是两回事，审查结论直接失效。

核验方式（**语义比对，不是文本比对**）：

```text
两边同名函数 → ast.parse → 剥离 lineno/col_offset → ast.dump → 逐字符比对
```

刻意用 AST 而非文本 diff：文档排版（缩进、换行、注释位置）必然与源文件不同，
文本比对只会产生噪音；而 AST 同构恰好对应"贴进生产后行为一致"这个真正关心的性质。

| 检查 | 判定 |
|---|---|
| 函数集合 | 文档含全部 11 个 helper（v6.1 第 11 个 `_set_close_reason_if_current`），且无多余 |
| **生产签名改动** | `PROD_SIGNATURE_OVERRIDES` 登记的方法不算"多余 helper"，但**文档必须恰好 1 份定义**（【1c】） |
| 逐函数同构 | 每个 helper 至少有一份与实现 `ast.dump` 一致（允许 BEFORE/AFTER 并存） |
| 重复定义 | 实现文件内无重名 `def` |
| frag 豁免 | 判据是 **AST 中存在 `...` 占位表达式**，不是正文里出现 `...` 字面量——注释里写 `fetch_open_orders(...)` 不会被误伤 |

运行方式（送审前必跑，可传参以适应 v7+）：

```text
.venv/Scripts/python.exe check_doc_helper_parity.py [送审稿路径] [helper文件路径]
```

**当前状态**：**11/11 逐函数同构，无重名，rc=0**；【1c】`_record_realized_pnl` 文档 1 份 ✅。

**自带变异自测**（`--self-test`）：M1 文件多函数 / M2 文档错误副本 /
M3 基线 / **M4 已登记方法出现第 2 份定义** / **M5 未登记的新 helper 定义**
—— 5 个全部符合预期，rc=0。M4/M5 是本轮为 `PROD_SIGNATURE_OVERRIDES`
白名单补的：证明白名单**只豁免"是不是 helper"，不豁免"有几份"**，
也不豁免"未登记的"。

> 这一项与 §八 那一项目前是**并列的两道门**：§八 保证"贴出去不炸"，
> §八-B 保证"贴出去的就是被测的那份"。

---

## 八-C、机器检查③（`check_doc_helper_calls.py`）：v6.1 新增调用闭包核验

前两道门都守不住一类缺陷 —— **NameError / 签名错位**。它们的共同特征是：
`ast.parse` **完全合法**，只有运行时才炸。

前科是 v5：`close_op_id` 在 `close_phase=1` 落盘**之后**才生成，语法毫无
问题，落地即 `NameError`。本轮 §八-2 又抓到同类的第二个 —— `_record_realized_pnl`
只给调用不给定义（C-7），落地即 `TypeError`，且炸在平仓已成交之后。

七查：

| # | 判据 | 级别 |
|---|---|---|
| 【1】 | 文档调用的 `self._xxx` 必须有定义（helper 全集 或 生产既有方法），否则落地 NameError | 致命 |
| 【2】 | helper 全集中零调用者 → 死代码 / 漏写调用点 | 提示 |
| 【3】 | 位置参数个数必须落在签名区间内 | 致命 |
| 【4】 | **关键字参数名必须真实存在**（写错 = 运行时 TypeError） | 致命 |
| 【5】 | helper 不得与生产 110 个既有方法重名（防静默覆盖） | 致命 |
| 【6】 | 文档自带定义与生产同名 ⇒ **逐条列出**（提议的生产签名改动清单） | 提示，但必须显式暴露 |
| 【7】 | 文档 helper 副本签名 ≠ 实现 ⇒ 致命（防副本遮蔽实现） | 致命 |

**签名解析优先级：`helper 实现文件 > 文档自带定义 > 生产现状`。**

这一条我改错过一次，值得记录：最初我让「文档自带定义」优先级最高，
结果 **M2 变异（给 helper 加必填参数）被文档里未改动的 helper 副本遮蔽，
checker 假绿**。所以 helper 实现文件必须是权威 —— 文档里那份 `class _Holder:`
是副本，副本与实现不一致本身就该报错（【7】），不能让副本反客为主。

运行方式（送审前必跑）：

```text
.venv/Scripts/python.exe check_doc_helper_calls.py [送审稿] [helper文件] [生产文件]
.venv/Scripts/python.exe check_doc_helper_calls.py --self-test
```

**当前状态**：rc=0；11 个 helper 零重名；【4】18 个被调用方法的关键字参数
全部合规；【6】仅 `_record_realized_pnl` 一项（本方案唯一的签名级改动）。
自带变异自测 **M1/M2/M3/M4/M6** 全部符合预期，其中 **M6 正是 C-7 原缺陷的
复现**（把文档新签名退回生产旧签名 → 必须 rc=1）。

