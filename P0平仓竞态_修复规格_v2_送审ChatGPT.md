# P0 平仓竞态修复规格 v2（送审 ChatGPT 第三轮 P0 裁定）

> 依据：规格 v1 + ChatGPT 对 v1 的六点评审（2026-08-28 晚）。本文档只钉死边界语义，
> **不改任何生产代码**。全部行号经本轮 Grep/Read 在 HEAD=c147543 工作树重新实证
> （git 验证 tracked 文件零改动）。
>
> 六点评审逐条对应：§1→评审①（G2/G3 全路径覆盖）、§2→评审②（批次归属判定）、
> §3→评审③（墓碑规则）、§4→评审④（N14 语义分离）、§5→评审⑤（字段级 merge）、
> §6→评审⑥（单向安全边界验收标准）。§7 实施批次重排，§8 待裁定开放决策点。

---

## §0 v1 更正记录（本轮取证发现，先自纠再送审）

| # | v1 表述 | 更正后（实证） |
|---|---|---|
| 1 | Q2/Q3 把 `execute_signal` L2644 归为 SL/TP create | L2644 实为**入场条件单**（STOP_MARKET entry，stopPrice=入场价，L2639-2651）；TP/SL 在 execute_signal 只预生成参数（`prepared_tp_params` L2705、`layer_sl_params` L2734），实际 create 在成交后由监控逐层/`_place_prepared_orders_immediately` 执行（L2749-2750 注释自证） |
| 2 | Q3 #4 归并 place_prepared 为 2 处 create | `_place_prepared_orders_immediately`（L5461-5899）内实际有 **3 处 create**：L5517（预生成 SL 分支一）、L5664（预生成 SL 分支二）、L5816（预生成 TP）。v1 漏列 L5517 |
| 3 | create 总数 "5 TP + 6 SL" | 权威全集 **14 处**：保护单 11 + 入场单 1 + 平仓单 2（见 §1 矩阵） |

更正后结论不变且更强：**保护单 create 全部 11 处既过 `_assert_create_allowed` 闸门、
又有 create 后 verify 钩子**——G1/G3 可零插码全路径覆盖，G2 仅需 11 处一行式插入。

---

## §1 评审①：G1/G2/G3 全路径覆盖矩阵（权威 create 全集）

### 1.1 create_order 调用点全集（14 处，Grep 全文穷举）

| # | 函数 | create | 闸门 `_assert_create_allowed` | verify 钩子 | G2 插码 |
|---|---|---|---|---|---|
| 1 | `update_batch_tp`（/tp） | L1651 | ✅ L1635 | ✅ L1660 `_verify_and_update_registry` | ✅ |
| 2 | `update_batch_sl`（/sl） | L1808 | ✅ L1792 | ✅ L1817 同上 | ✅ |
| 3 | `_update_sl_no_validation`（保本/内部滚动） | L2022 | ✅ L2005 | ✅ L2031 同上 | ✅ |
| 4 | 监控循环 SL 逐层首挂 | L4194 | ✅ L4171 | ✅ L4203 同上 | ✅ |
| 5 | 监控循环 TP 逐层首挂 | L4268 | ✅ L4245 | ✅ L4277 同上 | ✅ |
| 6 | 监控循环 SL 维护段一 | L4885 | ✅ L4853（另有 replace 预检 L4796） | ✅ L4895 同上 | ✅ |
| 7 | 监控循环 SL 维护段二 | L5042 | ✅ L5011 | ✅ L5051 同上 | ✅ |
| 8 | 监控循环 TP 维护段（**事故路径**） | L5214 | ✅ L5179（另有 replace 预检 L5134） | ✅ L5223 同上 | ✅ |
| 9 | `_place_prepared_orders_immediately` SL-a | L5517 | ✅ L5488 | ✅ L5526 `_verify_order_created` | ✅ |
| 10 | 同上 SL-b | L5664 | ✅ L5635 | ✅ L5673 同上 | ✅ |
| 11 | 同上 TP | L5816 | ✅ L5781 | ✅ L5825 同上 | ✅ |
| E | `execute_signal` 入场条件单 | L2644 | ❌ 无（批次刚创建，无关闭竞态面） | registry 直写 PENDING_VERIFY L2658-2661 | 豁免 |
| C1 | `close_position_market` 平仓单 | L6071 | 豁免（见 1.3） | — | 豁免 |
| C2 | `close_position_limit` 限价平仓单 | L6287 | 豁免（见 1.3） | — | 豁免 |

### 1.2 三道防线的落点方式（最小插码）

- **G1（闸门扩展，零插码）**：`_assert_create_allowed`（L2990-3045）内部新增两条检查——
  ①批次关闭态：`pending_close` / `is_programmatic_cancel` / `close_phase ≥ CLOSE_REQUESTED`
  → 拒绝；②批次不存在（`b is None`，L3013-3016 现状返回"允许"）：改为**区分调用语境**——
  保护单 create 语境下批次已消失 = 已结算/已清理 → 拒绝（封死场景 B 放行通道）。
  现有 13 个闸门调用点自动全量受益。
- **G2（create 紧前复核，11 处一行式插入）**：新增 helper
  `_final_pre_create_check(symbol, batch_id, identity)`（重读磁盘 state，任一关闭信号 →
  abort + 回滚 PENDING_CREATE registry 条目为 FAILED）。在 §1.1 #1-#11 每处
  `self._safe_api_call(self.exchange.create_order, ...)` **紧前**插入一次调用。
  选一行式插入而非 wrapper 重构：符合项目"逐点插码 + AST 锚点核对"惯例，diff 可审计。
- **G3（Commit 前复核+收敛，零插码）**：扩展既有 verify 钩子
  `_verify_and_update_registry`（L2891，覆盖 #1-#8）与 `_verify_order_created`
  （L2847，覆盖 #9-#11）：verify 返回后、写 CONFIRMED 前，复核批次仍存活且非关闭态；
  若已关闭/已清理 → **立即撤销刚创建的单**（cancel_order(order_id)，Fail-Closed：
  撤失败 → 🚨 critical 告警 + registry 记 HARD_LOCK）+ 不写 CONFIRMED。
  两个函数恰好覆盖全部 11 处保护单 create，无新增调用点。

### 1.3 豁免项理由（平仓单 C1/C2）

平仓单（市价 L6071 / 限价 L6287）为 reduceOnly 风险减少方向，且**只在平仓入口函数内
创建**——这些函数本身就是 CLOSE_REQUESTED 的写入者（Q1 三入口之二）。冻结语义是
"禁止增加风险"，不禁止减少风险。入场单 E 无竞态面（批次同一函数内刚创建，尚无监控
线程、无结算线程）。

---

## §2 评审②：什么叫"属于本 batch"——归属三级判定（Batch B 核心）

### 2.1 前置事实（源码实证）

- **全部 create 均不设置 clientOrderId**（Grep 全文零匹配）→ 无法靠交易所侧标签归属，
  只能靠程序侧记录。
- 既有归属机制两套：registry `order_id`（含 `id_known=True` 未决态）与
  `_order_matches_intent`（L3140-3207，参数级比对：symbol 归一化/side/
  info.type 还原/reduceOnly/stopPrice/amount，软检查规则，事件3+Mock 盲区双重实战）。

### 2.2 归属三级判定（CLOSE_SETTLING 两源扫描的自动撤范围）

| 级 | 判据 | 动作 |
|---|---|---|
| **L1 id 精确归属** | order_id ∈ 本 batch state 已知 id 全集 {`tp_order_id`, `current_sl_id`, `entry_orders[]`, `limit_close_order_id`} ∪ 本 batch registry 全部 `id_known=True` 条目（含 PENDING_VERIFY/NOT_CONFIRMED 未决态） | **自动撤**（有明确归属证据，程序自己的单，无归因争议） |
| **L2 参数归属** | 不在 L1，但与本 batch 某 registry identity 的 intent 指纹经 `_order_matches_intent` 完整匹配 | **自动撤** + 告警说明按参数归属（误匹配风险已被软检查设计压低：明确不匹配即 False，宁可不收编） |
| **L3 无主** | 不属于**任何** active batch 的 L1/L2，也不在本 batch L1/L2 | **只告警不撤**：🚨【资金安全】critical，列订单详情（id/type/side/qty/stopPrice），人工裁决（/adjust 或手动撤） |

### 2.3 为什么 L3 不自动撤（边界论证）

- 误撤风险实证面：用户在 App **手动**挂的 reduceOnly 条件单、其他工具的单，都不在
  程序 registry → L3 误撤 = 侵入用户手动操作域（用户交易模式明确包含手动操作场景）。
- 与项目宪法边界对齐：既有原则"订单/registry 可自动向交易所现实收敛（=R-A/B/C/D
  自愈合法）；**批次账本归因只能人工 /adjust 裁决**"。L1/L2 = 有程序侧归属证据的
  收敛（合法自动）；L3 = 归因不明（禁止自动）。
- ChatGPT 场景 G（跨批次污染）验收在 L3 语义下达成"发现+告警+阻断误触发"，不要求
  自动撤：新批次建仓时（`_check_sl_coverage` L2423 已有未归属仓位检查入口）扩展输出
  L3 无主保护单清单，提示用户先清理。

### 2.4 归属判定的失效模式与兜底

- registry 损坏/条目丢失 → L2 退化为 L3 → 只告警不撤（Fail-Closed 方向，安全）。
- 本 batch L1/L2 撤单失败 → 不 clear、HARD_LOCK、🚨 critical（沿用 v1 Q11 门禁）。
- 撤成功 id 记入墓碑收敛清单（§3），幂等防重撤。

---

## §3 评审③：墓碑机制完整规则

| 项 | 规则 |
|---|---|
| 存储 | 独立文件 `trade_tombstones.json`（**不并入** trade_state.json——clear 逻辑本身要从 state 删键，墓碑必须独立持久化才能抵抗"删记忆"） |
| 写入 | `clear_batch_state`（L1275-1285）内持 `_state_lock` 追加：`{batch_id: {symbol, side, cleared_at, converged_order_ids: [...]}}`；converged_order_ids = 本次结算收敛成功的全部 id（L1/L2 撤销成功者） |
| 检查 | `save_batch_state`（L1267-1273）持锁后先查墓碑：见本 batch_id 墓碑 → **拒绝写入** + 🚨 critical 告警（"已清理批次复活尝试被阻断"）——Q8 #5 批次复活通道的封死 |
| TTL | 7 天（cleared_at 起算）。理由：覆盖一次长假/停机重启窗口；超过后同 batch_id 复活概率已被 UUID 压为零，墓碑使命完成 |
| 过期清理 | 启动时 prune 一次 + 日报线程顺带 prune（文件极小，无性能面） |
| 重启恢复 | 纯磁盘文件天然跨重启；无内存态需要恢复 |
| batch_id 复用 | parser.py L127-129 实证：`batch_{YYYYmmdd_HHMMSS}_{uuid4().hex[:6]}`——UUID 后缀使复用概率 ≈ 0。即使假设碰撞：墓碑拒绝写入 = 新批次无法建 → Fail-Closed（拒绝建批优于放任复活），可接受 |
| 幂等 | 结算重扫发现 id 已在墓碑 converged_order_ids → 跳过撤单（防对已撤/已成交 id 再发 cancel 触发无谓 OrderNotFound） |
| 溯源 | 场景 G 的 L3 无主单告警时，若 id 命中某墓碑 converged_order_ids → 告警文本注明"疑似 7 天内已清理批次 X 的遗留单"（协助人工裁决） |
| 与 `.bak` 关系 | `_persist_states` 的 .bak（L1254-1256）是 last-known-good 备份，墓碑独立于该机制；.bak 恢复旧 state 时墓碑同样拦截已清理批次复活 |

---

## §4 评审④：N14 语义分离精确规格

### 4.1 新 registry 终态 `PROGRAMMATIC_CANCELED`

registry 现有状态集（实证）：PENDING_CREATE / PENDING_VERIFY / NOT_CONFIRMED /
CONFIRMED / MISMATCH / FAILED / ABSENT / HARD_LOCK（锁定标志）。
新增 **PROGRAMMATIC_CANCELED**：程序为平仓主动撤销的保护单。

### 4.2 转移规则（只进不出的终态）

| 规则 | 内容 |
|---|---|
| 写入点 | 唯一：程序主动撤保护单处——`close_position_limit` 撤 TP（N14 修改点 L6266-6275）、撤 SL 处、`close_position_market` 撤保护单处、`cancel_open_orders` 撤保护单处。**同时保留 order_id 不清**（N14 不再置 `tp_order_id=None`） |
| 终态性 | **任何路径不得转出**：`_adjudicate_recreate_before_repair`（L3209）fetch 到 canceled → 现在写 ABSENT（L3236/3245，事故链一环）→ 改为：registry 已是 PROGRAMMATIC_CANCELED 时 fetch canceled **不写 ABSENT、不允许重建** |
| 闸门语义 | `_assert_create_allowed` 禁建集（L3037）新增 PROGRAMMATIC_CANCELED（视同 CONFIRMED 拒建，且**无 replace 豁免**——换挂语义 B2-8 只适用于 CONFIRMED 的活单） |
| 语义分离达成 | `tp_order_id` 保留 + registry 终态 ⇒ `tp_order_id is None` 回归唯一语义"从未创建/创建即失败"；R14（L4638）判定不再被平仓路径触发 |

### 4.3 消费点核对（三处）

1. R14 补挂判定 L4638：读 close_phase / pending_close / is_programmatic_cancel /
   registry PROGRAMMATIC_CANCELED → 禁（多重冗余，单一失效不放大）
2. `_adjudicate_recreate_before_repair`：PROGRAMMATIC_CANCELED → 返回禁止重建（§4.2）
3. `_assert_create_allowed`：禁建集 + 无 replace 豁免（§4.2）

---

## §5 评审⑤：`save_batch_state` 字段级 merge 分类

现状实证：`save_batch_state`（L1267-1273）持锁 → load → **整批 dict 直接赋值覆盖**
（`all_states[symbol][batch_id] = batch_data`）→ 零 merge。B5 与批次复活同源于此。

字段全集实证：初始 22 字段（execute_signal L2714-2737）+ runtime 增补
（`pending_close`/`is_programmatic_cancel` L6225-6227、`settled_by_limit_close`、
`limit_close_order_id` L6300）+ 新增 `close_phase`。

### 5.1 六类字段规则表

| 类 | 字段 | 规则（save 时磁盘 vs 快照冲突处理） |
|---|---|---|
| **A 棘轮（只进不退）** | close_phase、pending_close、is_programmatic_cancel、settled_by_limit_close、user_modified | 布尔/相位取"更真"侧：False→True 单向、close_phase 取更高等级。旧快照不得降级（B5 的语义级修复） |
| **B 单调计数** | last_filled_count、filled_details（逐层）、total_entry_fee | 逐字段取 max / 逐层取较大。旧快照不得降级账本（结算线程已计的成交/费用不被监控旧快照抹掉） |
| **C registry 逐 identity merge** | protection_registry | 逐 identity 合并：磁盘条目 state ∈ {PENDING_CREATE, PENDING_VERIFY, NOT_CONFIRMED, CONFIRMED, MISMATCH, HARD_LOCK, **PROGRAMMATIC_CANCELED**} → 保留磁盘（未决/已锁/终态不许被旧快照降级）；磁盘 FAILED/ABSENT → 快照时间戳更新者胜。既有同型范式：L2740-2746（execute_signal 合并 registry）、L1670-1672（update_batch_tp reload-merge） |
| **D id 镜像字段** | tp_order_id、current_sl_id、limit_close_order_id | 磁盘非 None + 快照 None + 该 id 对应 registry identity **未终结**（非 PROGRAMMATIC_CANCELED/ABSENT/FAILED）且 close_phase < CLOSE_SETTLING → 保留磁盘；其余情况最新者胜（正常终结路径允许清 id） |
| **E 静态字段（创建后不变）** | batch_id、symbol、side、entry_orders、stop_steps、batch_total_amount、target_amounts、params_base、is_hedge_mode、layer_sl_params、prepared_tp_params | 幂等覆盖无害（创建后不变；entry_orders 成交记录变更由 B 类 filled_details 承载账本） |
| **F 簿记（最新者胜，维持现状）** | take_profit_price、sl_fail_count、sl_failed_layers、pending_sl_orders | 沿用现状；/tp /sl 命令路径已有 reload-merge 范式（L1670-1672），监控簿记旧值覆盖风险低且不属于安全面 |

### 5.2 实现边界

- merge 逻辑封装在 `save_batch_state` 内部（单一咽喉，3 类写线程全部经此——Q7 实证
  唯一落盘通道 `_persist_states` L1248 + 调用方持 `_state_lock`），**不新增锁、
  不改调用方**。
- 墓碑检查（§3）先于 merge 执行。
- merge 后字段集合 = 磁盘 ∪ 快照（快照新增字段正常写入）。

### 5.3 残余风险声明（供裁定）

D 类规则在"正常 TP 成交终结（清 id）vs 旧快照保 id"的窗口依赖 registry 终结态作锚。
若 registry 条目本身缺失（理论不可能——id 写入与 registry 同事务段落），D 退化为
最新者胜 = 回到现状语义，无新增风险。**该残余面小于 B5 现状风险，不做更重设计。**

---

## §6 评审⑥：单向安全边界——硬验收标准（最终表述）

### 6.1 硬不变量（写入项目宪法，与既有 8 不变量并列）

> **不变量⑩（平仓单向边界）：批次一旦进入 CLOSE_REQUESTED，任何线程对交易所的
> 唯一允许变更 = 撤销已有订单 / 创建 reduceOnly 平仓单 / 结算。任何创建保护单
> （TP/SL/入场单）或修改保护单的路径必须被拒绝并视为缺陷。**

### 6.2 逐路径验收矩阵（关闭条件 = 矩阵全绿）

| 路径 | 验收 | 落点 |
|---|---|---|
| §1.1 #1-#11 全部 11 处保护单 create | CLOSE_REQUESTED 后逐一驱动 → 每处 0 create | G1 闸门 + G2 紧前 + G3 收敛（三层独立，任一层拦截即通过，测试需证 G1 单独可拦） |
| modify 路径 4 处：update_batch_tp / update_batch_sl / set_breakeven_sl / 监控维护段 | CLOSE_REQUESTED 后逐一驱动 → 每处 0 create 0 modify（入口拒绝） | 命令入口读 close_phase 拒绝 + 维护段冻结（v1 Batch A 内容） |
| 平仓单 C1/C2 | **必须仍可创建**（豁免验证，防过度冻结） | §1.3 |
| 撤单路径 | **必须仍可执行**（风险减少） | 不受冻结约束 |
| B 场景（create 挂起中 batch 被结算） | gate 释放后 create 被拦 / 已 create 被撤 | G2/G3 |
| 现有 9 项 RED 断言 + GREEN A-G 七场景 | 全绿 | test_close_race_replay.py 升级 |
| 全量回归 30 文件 | 全绿（orphan_guard RC=1 既有怪癖除外） | 惯例 |

---

## §7 实施批次重排（v2 修正后）

| 批次 | 内容 | 验收 |
|---|---|---|
| **Batch A：风控冻结 + 三道闸门** | close_phase 字段 + 三入口写相位；G1 闸门扩展（含 b is None 语境区分）；G2 helper + 11 处插码；G3 verify 钩子扩展；R14/维护段/命令线程冻结；N14→PROGRAMMATIC_CANCELED（§4） | GREEN A/B/C/D + §6.2 矩阵前两行 + 9 RED 中 A1/A3/B1/B3 翻绿 |
| **Batch B：结算残单归零** | `_monitor_limit_close` 撤 TP；`_converge_batch_orders_before_clear` 两源扫描（§2 L1/L2/L3 分级）；clear 前四条件门禁 + Fail-Closed | GREEN E/G + A4/A5/B4/B7 翻绿 |
| **Batch C：防回退/防复活** | save 字段级 merge（§5 六类）+ 墓碑（§3）+ 回滚点收紧 | GREEN F + B5 翻绿 + 复活单测 |

每批纪律不变：对应断言翻绿 → 全量回归 → 备份 → 呈报。批次间依赖：A 先行（B 的
L1/L2 归属判定依赖 PROGRAMMATIC_CANCELED 终态；C 的 D 类规则依赖 registry 终态锚）。

**明确不做**（承二轮裁定）：StateManager/Event Sourcing/数据库/CAS/Redis/状态机
框架/大规模锁改造/回滚 N14/L3 无主单自动撤。

---

## §8 待三轮裁定的开放决策点（显式列出，不留隐含决策）

| # | 决策点 | 我方倾向 | 备选 |
|---|---|---|---|
| D1 | L3 无主单：告警人工（§2.3）vs 自动撤 | 告警人工（宪法"归因不自动"） | 自动撤但仅限 reduceOnly 条件单类型过滤 |
| D2 | G2 落点：11 处一行式插码 vs wrapper 重构 | 一行式（可审计、AST 锚点惯例） | wrapper 收敛但 diff 大、锚点全重排 |
| D3 | 墓碑 TTL 7 天 | 7 天 | 3 天 / 30 天 |
| D4 | D 类 id 镜像字段规则（§5.1 D 行） | 按 registry 终结态作锚 | 简化为"磁盘非 None 一律保留"（更保守但正常终结清 id 会被卡） |
| D5 | `b is None` 闸门语境区分的实现方式：helper 增参（如 `require_live_batch=True`）vs 依据 identity 反查 | 增参显式 | 反查隐式（少改调用点但语义模糊） |
| D6 | Batch A 是否包含 N14 改造（§4），还是拆 A0 小批先行 | 包含（N14 是 G1 前置语义） | 拆 A0（更小步，多一轮回归成本） |

---

## 附：本轮新增源码证据清单（全部本轮 Read/Grep 复核）

- create 全集 14 处：Grep `self.exchange.create_order`（L1651/1808/2022/2644/4194/
  4268/4885/5042/5214/5517/5664/5816/6071/6287）
- 闸门 13 调用点 + 语义全读：L2990-3045（禁建集/换挂/b is None 放行通道）
- verify 覆盖：`_verify_and_update_registry` 8 处（L1660/1817/2031/4203/4277/4895/
  5051/5223）+ `_verify_order_created` 3 处（L5526/5673/5825）
- clientOrderId 零使用（Grep 全文无匹配）
- batch_id 生成：parser.py L127-129（UUID 后缀）
- 初始 state 22 字段：L2714-2737；runtime 增补：L6225-6227 / L6300
- `save_batch_state` 整批覆盖实证：L1267-1273；`clear_batch_state` 零 API：L1275-1285
- `_order_matches_intent` 参数级归属匹配：L3140-3207
- registry 状态集：L2993-2999（闸门 docstring 权威枚举）+ L3037
- `_place_prepared_orders_immediately` 边界：L5461-5899（内含 3 create）
- 函数地图全量：L1287-6361（本表 Grep `^    def` 输出）
