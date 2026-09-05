# P0 平仓竞态修复 Batch B v1 实施规格（送审 ChatGPT）

> 基线：HEAD = **01bb44f**（Batch A 991f84f + Batch C 01bb44f 已封版），工作树净。
> 全部行号本轮（2026-08-29）在 01bb44f 工作树 Grep/Read 重新实证。
> 本文 = v3 终审稿 §6 Batch B 行的展开：**`_monitor_limit_close` 撤 TP + converge
> 两源扫描（L1/L2/L3）+ CONVERGENCE_UNKNOWN + proof 门 clear（Fail-Closed，
> 无程序侧逃生门）**。不动生产代码，待 ChatGPT 终审 APPROVED 后才实施。

---

## §0 现状基线与既有构件（全部零新增依赖）

| 构件 | 位置（01bb44f） | Batch B 复用方式 |
|---|---|---|
| `close_phase` 状态机 | 写入点 L6640/6695/6911（=1）、L6786/7150（=2）；读取侧 L3200/3224/3537/4974 | converge/proof 全程只读；clear 通过时写 =3（CLOSED） |
| `_collect_batch_order_ids` | def L1425（镜像字段 `tp_order_id`/`current_sl_id`/`limit_close_order_id`（L43）+ registry 全部 order_id） | **L1 id 全集收集直接复用**（Batch C 已为墓碑 built） |
| `_order_matches_intent` | def L3677（参数级归属，事件3+Mock 盲区双实战） | L2 归属判定 |
| 双通道扫描范式 | `_self_heal_no_id`（def L4055）：L4086-4093 `fetch_open_orders` normal + `params={'stop': True}` conditional，任一异常 → INVALID（UNKNOWN≠EMPTY，L2454-2456 同哲学先例） | converge 两源扫描**逐行复用该模式**（normal/conditional 双通道 + id 去重合并 orders_by_id，L4097-4103 惯例） |
| `_commit_registry_txn` | def L3463（Batch C 选项1：锁内 load→apply→persist 直写） | converge 撤单成功后写 PROGRAMMATIC_CANCELED 的落盘通道 |
| `clear_batch_state` | def L1338-1371（Batch C 已写墓碑，docstring L1340 显式预告"Batch B 接 proof 门后升级为 L1/L2 撤销成功全集"） | **proof 门插入点**（§4） |
| G3a 撤单/核账 helper | `_g3a_cancel_race_order` L3238 / `_g3a_recheck_position` L3259 | 参考实现（cancel 幂等：-2011/Unknown order = 事实终态） |
| `_get_current_position_amt` | def L2398（fetch_positions L2402） | proof 的 position 核验 |
| `_cancel_remaining_entries` | def L2354（仅 entry 挂单，except 吞错） | 保留不动；converge 以 L1 全集覆盖其吞错盲区 |
| `_cancel_limit_close_order` | def L6849（A1：clear 前撤限价平仓单，-2011 幂等） | 保留不动；limit_close_order_id 已在 L1 全集（镜像字段） |

**事故根因现状（结算侧 TP 缺口，本轮重新实证）**：`_monitor_limit_close`
（def L7083）成交结算段：写 `close_phase=2`（L7148-7150）→ **只撤 `current_sl_id`**
（L7156-7166，`params={'stop': True}`）→ **TP 完全不撤**。TP 的撤销目前只发生在
`close_position_limit` 入口（L6959-6974，N14），且该处撤失败时注释明示
"监控冻结 + **Batch B 兜底**"（L6973）——本批次的法定职责已被前两批预留。

---

## §1 B0：结算段撤 TP（`_monitor_limit_close`）

**落点**：L7156-7166 撤 SL 块**之后**、结算 break 之前，对称追加撤 TP 块：

- 读 `b_data.get('tp_order_id')`（N14 语义：**id 保留不清**）；
- `cancel_order(tp_id, symbol, params={'stop': True})`；
- **幂等**：`-2011` / "Unknown order" → 已离开交易所 = 事实终态，视为成功
  （`_cancel_limit_close_order` L6865-6867 同惯例；TP 在入口已撤过的正常路径
  走到此分支，零多余告警）；
- 撤成功 / 已终态 → registry 写 `PROGRAMMATIC_CANCELED`（identity 用
  `_find_registry_identity_by_order_id` def L3350 反查，reason=`close_settled_canceled_tp`，
  经 `_commit_registry_txn` 落盘；id_known=True）；
- 撤失败（非 -2011）→ 不写终态、不阻断结算消息，**交 B1 converge 兜底**
  （主循环 finally 清理前 converge 会再次以 L1 精确归属处置该 id）；
- 该块 try/except 独立包裹，异常不覆盖既有结算链（SL 撤块现状即吞异常模式）。

**验收**：结算后交易所本批次 TP 残单 = 0（测试矩阵 R-B3）。

---

## §2 B1：`_converge_batch_orders_before_clear(symbol, batch_id)` 全规格

新函数（建议落点：`_cancel_limit_close_order` L6849 之后、`close_position_limit`
之前，平仓/清理语义聚居区）。签名返回 proof dict 或 `None`（不收敛）：

```
proof = None                 → 不收敛（UNKNOWN / 撤单失败 / position 非 0）→ 调用方跳过 clear
proof = {§2.4 对象}          → 收敛 → 调用方传 proof 调 clear
```

### 2.1 执行序列（全锁外，可发 API）

```
① load_all_states()（锁外读）→ b = all_states[symbol][batch_id]
   b is None → 返回 None（批次已不存在，无收敛对象，调用方按现状处理）
② 两源扫描（复用 L4086-4093 模式）：
   normal    = fetch_open_orders(symbol)                    # 通道一
   conditional = fetch_open_orders(symbol, params={'stop': True})  # 通道二（algo 端点）
   任一通道异常 → return None + exchange_scan='unknown' 语义（§2.3 CONVERGENCE_UNKNOWN，
   内部记 proof_failed_reason='scan_unknown'，供告警文案）
   合并 orders_by_id（L4097-4103 去重惯例，normal 优先保留字段）
③ position 核验：_get_current_position_amt(symbol, is_hedge_mode, side)
   本批次贡献 = max(0, symbol持仓 − Σ 其他活跃批次 current_filled)
   贡献 ≠ 0 → 不收敛（return None，告警说明持仓残留，绝不 clear）
④ 跨批次 L1/L2 全集预计算（判定 L3 需要）：
   for 其他 active batch in all_states[symbol]：
       其 L1 id 全集 + 其 registry intents → other_l1_ids / other_intents
⑤ 逐单处置（对 orders_by_id 中每个 open order）：
   ├─ id ∈ 本批次 L1 全集 → 自动撤（§3 L1）
   ├─ 不在 L1，且 _order_matches_intent(order, 本批次某 registry intent) → 自动撤（§3 L2）
   ├─ 属于其他 active 批次 L1/L2 → 跳过（他批次资产，绝不碰）
   └─ 无主 → L3：只告警不撤（§3 L3）
⑥ 撤单幂等规则（每次撤单统一）：
   成功 → 记入 l1_canceled / l2_canceled + registry PROGRAMMATIC_CANCELED
         （reason='converge_l1'/'converge_l2'，_commit_registry_txn 落盘，id 保留）
   -2011/Unknown order → 已终态：记入 state_ids_resolved（无需撤）
   其他异常 → 撤单失败：不收敛（return None）+ 🚨 critical
         （v2 §2.4：L1/L2 撤单失败 → 不 clear、critical）
⑦ 扫描复核（撤完重扫一次两源）：本批次 L1/L2 归属残单 ≠ 0 → 不收敛
   （防撤单请求成功但交易所延迟生效的窗口；一次复核足够，不做无限重试）
⑧ 全部通过 → 组装 proof 对象（§2.4）返回
```

### 2.2 registry 未决条目的收敛

对每条 registry 条目（锁外读快照 → `_commit_registry_txn` 锁内提交）：

- 有 order_id（含 `id_known=True` 的 PENDING_VERIFY/NOT_CONFIRMED 未决态）→
  已被 ⑤ 的 L1 处置；
- `id_known=False` 的 PENDING_VERIFY → ⑤ 的 L2 处置（intent 匹配）；L2 无匹配
  且不在交易所 → **终态化 ABSENT**（"缺席≠从未存在"不变量①下，position=0 +
  扫描 zero 已证安全，写入让 proof 条件④成立）；
- G3a UNKNOWN 遗留（PENDING_VERIFY + hard_locked + id_known=True）→ L1 精确
  归属自动撤（v3 §1.3 末分支的法定落点："该 id id_known=True → L1 精确归属 → 自动撤"）。

### 2.3 CONVERGENCE_UNKNOWN（P0-5）

- ② 双通道**任一**异常 = `exchange_scan='unknown'`：**绝不 clear**，批次保持
  `close_phase=2`，🚨 critical（文案含"无法证明残单为零，批次保持 CLOSE_SETTLING
  待下轮重试"），走既有告警去重（同键 3 轮 SILENCED）；
- **重试载体**（本轮实证的天然回路，零新增线程）：
  - 监控线程结算路径（L4622/4630/4699/5152/5321）→ UNKNOWN 时不 break、
    不 clear、批次保持 is_active → 下一监控周期重入结算检测 → 重试 converge；
  - `close_position_market`（同步命令路径，L6792）→ converge 失败时**不 clear**、
    返回成功消息但附"⚠️ 残单收敛未完成，监控线程将继续清理"；市价平仓后
    position=0 → 监控线程持仓归零路径（L4699）自动接手重试；
- **绝对禁止** except 分支把 open_orders 置 `[]` 走"扫描通过"（v3 §4 原文）。

### 2.4 proof 对象（v3 §3.1 契约，逐键落地）

```python
{
  "batch_id": str, "symbol": str, "checked_at": float,   # time.time()
  "scope": "FULL" | "PRE_ENTRY",                          # §5 调用点分类
  "position_zero": True,          # ③ 的批次贡献核验结果（多批次扣减，§8 决策点 D-B1）
  "state_ids_resolved": [id,...], # 本批次 L1 全集的处置清单（撤成功 ∪ 已终态 ∪ 不在场）
  "exchange_scan": "zero",        # 仅两源扫描成功且复核 ⑦ 通过才写 'zero'（P0-5）
  "l1_canceled": [id,...], "l2_canceled": [id,...],
  "l3_orphans": [ {id, type, side, qty, stopPrice}, ... ]  # 只列示，不阻塞 clear（P0-6）
}
```

**FULL 四条件**（全部成立才返回 proof）：
1. 本批次持仓贡献 = 0（多批次同 symbol 时扣减其他活跃批次份额）；
2. L1 全集全部处置完成（state_ids_resolved 覆盖全集）；
3. 交易所双源扫描成功 + 复核后本批次归属残单 = 0；
4. registry 无未决残留（全部到达终态，含 converge 写入的终态化）。

PRE_ENTRY 条件：入场单已确认终态 + registry 全终态 + position=0（未建仓路径
的 FULL 子集，行为完全一致，仅 proof['scope'] 标注不同——scope 只服务于
clear 的校验侧语义核对）。

---

## §3 L1/L2/L3 具体定义（ChatGPT 要求②，v2 §2.2 落地版）

| 级 | 精确定义（基于 01bb44f 构件） | 动作 |
|---|---|---|
| **L1 id 精确归属** | order.id ∈ `_collect_batch_order_ids(b_data)` = 镜像字段 {`tp_order_id`, `current_sl_id`, `limit_close_order_id`}（L43 全集）∪ registry 全部条目 order_id（**含 PENDING_VERIFY/NOT_CONFIRMED 未决态与 id_known=True 者**——未决 ≠ 无主，程序自己的单） | **自动撤**（幂等规则 §2.1⑥）+ registry 终态 + 记 l1_canceled |
| **L2 参数归属** | 不在 L1，但与本批次某 registry identity 的 intent 经 `_order_matches_intent`（L3677）完整匹配（明确不匹配即 False 的软检查语义不变，宁可不收编） | **自动撤** + 🚨 告警说明按参数归属 + 记 l2_canceled |
| **L3 无主** | 不属于本批次 L1/L2，**也不属于同 symbol 任何其他 active 批次的 L1/L2**（§2.1④ 跨批次预计算） | **只告警不撤**：🚨【资金安全】critical 列订单详情（id/type/side/qty/stopPrice），人工裁决（/adjust 或手动撤）；**不阻塞 clear**（v3 §3.1：proof 携带 l3_orphans 列示） |

**L1/L2/L3 共同前置**：订单必须出现在两源扫描结果中（在场单才有处置意义）；
本批次 L1 全集中不在场的 id = 已终态（曾成交/曾撤销），直接进
state_ids_resolved，不发 cancel（避免对已终结 id 无谓触发 OrderNotFound）。

**L3 只告警不撤的理由**（P0-6 裁定维持）：用户 App 手动 reduceOnly 单、外部
工具单都不在 registry → L3 误撤 = 侵入用户手动操作域；与宪法"批次账本归因
只能人工 /adjust 裁决"对齐；L1/L2 = 有程序侧归属证据的收敛（合法自动）。

**归因降级方向安全**：registry 损坏/条目丢失 → L2 退化 L3 → 只告警（Fail-Closed
方向，v2 §2.4）。

---

## §4 B2：proof 门插入点（ChatGPT 要求③）

### 4.1 `clear_batch_state` 改造（唯一插入函数）

**落点**：def L1338，在 `with self._state_lock:`（L1344）取锁后、墓碑写入
（L1349）**之前**插入验证块：

```python
def clear_batch_state(self, symbol, batch_id, proof=None) -> bool:
    ...（保留 L1342-1343 熔断告警键清理）...
    with self._state_lock:
        # —— P0 Batch B（v3 §3.1）：proof 门，持锁验证，Fail-Closed ——
        1. proof is None 或缺任一必需键（batch_id/symbol/scope/position_zero/
           state_ids_resolved/exchange_scan）→ 拒绝：🚨 critical
           （"无收敛证明的状态删除被阻断，批次保持待收敛"）+ return False
        2. proof['exchange_scan'] != 'zero' → 拒绝（P0-5）+ return False
        3. proof['batch_id'/'symbol'] ≠ 实参 → 拒绝（防错传）+ return False
        4. scope 校验：批次（磁盘最新 state）有已成交份额/registry 存在非终态
           历史痕迹/有平仓单 id → proof['scope'] 必须 == 'FULL'
        5. proof 有效 → 写 close_phase=3 (CLOSED)（v3 §2.1：紧邻 clear 之前）
           → 走既有 Batch C 墓碑链（converged_order_ids 升级为
           l1_canceled + l2_canceled 全集——docstring L1340 预告的升级点）
           → del state → _persist_states → return True
```

- **无程序侧逃生门**（v3 §3.3 显式声明）：不加 `force=True`；converge 反复失败
  的卡死批次 = 运维离线人工处置（删/修 state 文件，D-005 恢复指引模式），
  程序侧 Fail-Closed 保持告警；
- **同栈直传**：proof 由调用方在**同一次调用栈**内 converge→clear 直传
  （checked_at 时效无窗口问题；converge 全程 close_phase≥1 冻结下无新增风险面）；
- 拒绝路径 return False 后**不写墓碑、不删 state**——清理失败可被下一轮重试。

### 4.2 10 个调用点改造（ChatGPT 要求④，逐点源码落点）

| # | 行号（01bb44f） | 调用链/语境 | scope | 改造模式 |
|---|---|---|---|---|
| 1 | L1789 | 启动恢复 `run_trader_recovery_on_startup` → stale_batches 清理（判定点 L1645 monitor_error / L1702 无挂单无持仓） | 收敛时定 | `proof = converge(...)` → `if proof: clear(..., proof)`；**monitor_error 批次可能仍有挂单/持仓——现状直接 clear 无任何撤单，本点是 proof 门 Fail-Closed 价值的直接实证**（测试矩阵 G-B7） |
| 2 | L4556 | 监控循环：未建仓终止（entry 全撤后） | PRE_ENTRY | 同上（_cancel_remaining_entries 吞错盲区由 converge L1 兜底） |
| 3 | L4622 | 监控循环：限价平仓已处理跳过重复结算 | FULL | 同上；UNKNOWN → 不 break 保持 is_active 下轮重试（§2.3） |
| 4 | L4630 | 监控循环：程序平仓跳过结算 | FULL | 同 #3 |
| 5 | L4699 | 监控循环：持仓归零路径（A1/N8 已撤 SL+限价单） | FULL | 同 #3 |
| 6 | L5152 | 监控循环：SL 结算路径（A1 已撤限价单） | FULL | 同 #3 |
| 7 | L5321 | 监控循环：TP 结算路径（A1 已撤限价单） | FULL | 同 #3 |
| 8 | L6049 | `cancel_open_orders` → 程序撤单标记清理 | FULL | 同上（同步路径：converge 失败 → 不 clear + critical） |
| 9 | L6071 | `cancel_open_orders` → 无持仓清理（current_pos==0 已验） | 收敛时定 | 同 #1；UNKNOWN（current_pos is None 分支现状本就不清理，L6071 elif 已有） |
| 10 | L6792 | `close_position_market` 市价平仓结算（L6786 已写 close_phase=2） | FULL | converge 失败 → 不 clear + 返回消息附"监控线程将继续清理"（§2.3 重试载体） |

**改造纪律**：每点两行式插码（`proof = self._converge_batch_orders_before_clear(...)`
+ `clear_batch_state(..., proof=proof)` 带条件分支），沿用项目"逐点插码 +
AST 锚点核对"惯例，diff 可审计。**监控循环 5 点的 UNKNOWN 不 break 语义
（#3-#7）是唯一非纯插入改动**——clear 调用移入 `if proof:` 分支，原 break
保持（批次仍活跃，下轮重入）。

### 4.3 既有检查的保留声明

- `_cancel_limit_close_order`（L6849）与 `_cancel_remaining_entries`（L2354）
  保留原位不动：它们是 fail-fast 风险递减动作；converge 是 proof 生产者。
  重复撤单由幂等规则（-2011 = 终态）天然消解；
- `close_phase=3` 写入点**唯一**：proof 门内（v3 §2.1 锚定，不新增第二写入点）。

---

## §5 API 预算与限流面（增量核算）

每次 converge = 2× `fetch_open_orders`（normal+conditional）+ 1×
`fetch_positions` + N× `cancel_order`（仅在场归属单）+ 1× 复扫（2×
fetch_open_orders）。全部经 `_safe_api_call`（Semaphore(1) + 全局熔断 + 429
退避既有保护）。

**频率面**：converge 仅在批次终结时触发（每批次生命周期一次 + UNKNOWN 重试
下限为监控周期 60-120s），非周期性轮询 → 无新增限流风险。双通道扫描模式与
`_self_heal_no_id`（L4086-4090，既有周期性调用）同构，已被实盘验证。

---

## §6 测试矩阵与预期 RED/GREEN（ChatGPT 要求⑤）

新建 `test_b_batch.py`（RED 基线先行 → 实施后翻绿），FakeExchange 注入沿用
`test_close_race_replay.py` 基建（双通道 orders 分桶 + 挂起钩子惯例）。

### RED 基线（01bb44f 现状，预期 FAIL = 缺陷实证）

| # | 注入 | 断言（现状必然 FAIL 的点） |
|---|---|---|
| R-B1 | 交易所残留本批次 TP（tp_order_id 在场）+ 驱动结算 | 结算后交易所本批次残单 ≠ 0（结算段只撤 SL，B0 缺口） |
| R-B2 | 直接调 `clear_batch_state`（无 proof） | state 被删、零告警（现状无门）——断言"拒绝 + critical + state 保留" |
| R-B3 | `fetch_open_orders` 抛 NetworkError + 驱动清理 | 现状 clear 不扫描直接删 → 断言"UNKNOWN 不 clear + close_phase 保持 2" |
| R-B4 | 挂一张无主 reduceOnly 条件单 + 驱动清理 | 现状无 L3 分级无告警 → 断言"critical 列单详情 + 不自动撤" |
| R-B5 | 残留本批次 L1 单撤单失败（非 -2011）+ 驱动清理 | 现状 clear 照删 → 断言"不 clear + HARD_LOCK + critical" |

### GREEN 验收（实施后）

| # | 断言 |
|---|---|
| G-B1 | 正常结算：B0 撤 TP + converge L1 全撤 + proof 四条件成立 → clear 通过 → 墓碑 converged_order_ids = l1_canceled ∪ l2_canceled → close_phase 终值 3（墓碑可查） |
| G-B2 | 幂等：TP 在 close 入口已撤（-2011）→ B0 视为终态成功零告警；converge 不在场 id 不发 cancel |
| G-B3 | UNKNOWN：clear 拒绝 + close_phase 保持 2 + critical 恰一次（3 轮去重）→ 下轮重试成功后 clear |
| G-B4 | L2：registry intent 匹配但 id 未知的在场单 → 自动撤 + 参数归属告警 + l2_canceled 记录 |
| G-B5 | L3：无主单告警 + 不撤 + **clear 仍通过**（l3_orphans 列示进 proof，P0-6） |
| G-B6 | proof 缺键/None/batch_id 不匹配 → clear 拒绝（三 case） |
| G-B7 | 启动 monitor_error 批次（L1789）：交易所残留挂单 → converge 撤 L1 → 才 clear（现状直删的封死） |
| G-B8 | 多批次同 symbol：批次 A 平仓、批次 B 持仓在场 → position 贡献扣减正确，A 可 clear、B 的单不被碰 |
| G-B9 | `grep` 断言：`clear_batch_state` 签名无 `force` 参数（无逃生门）；`close_phase=3` 写入点唯一 |
| G-B10 | L1 撤成功 → registry 经 `_commit_registry_txn` 到 PROGRAMMATIC_CANCELED（终态，order_id 保留） |

### 回归链（批次验收门禁）

1. `test_b_batch.py` RED 基线归档（log 不 commit）→ 实施后全 GREEN；
2. `test_close_race_replay.py` 28/28 保持 GREEN（Batch A 冻结层零回退）；
3. `test_c_batch.py` 22/22 三跑（Batch C 墓碑/merge 零回退，proof 门在墓碑
   写入之前不与 C 类冲突）；
4. `test_b2_restart.py` 9/9（重启链路零回退）；
5. 全量回归 33 文件：除 orphan_guard 既有环境型（rc=42 灰度实盘进程持锁）
   外全部 rc=0——**严谨口径：不得写"33/33 全绿"**；
6. 插码后 AST 锚点重对齐（§4.2 改动集中在 L1338-1371/L6049-6071/L6792/
   L7156-7166 及新函数段，预计位移量逐文件核实；t25/t26/sg4/b2_* 锚点按惯例
   AST 核对同步）。

---

## §7 决策点（呈 ChatGPT 裁定，无隐含决策）

| # | 决策点 | 我方倾向 | 备选 |
|---|---|---|---|
| D-B1 | **position_zero 的多批次语义**：symbol 级总持仓 ≠ 0 时（其他活跃批次在场），本批次贡献 = 总持仓 − Σ其他批次份额（全部由 state 计算，零额外 API） | 贡献扣减法（批次级模型自洽） | 简化"仅 symbol 无其他活跃批次时才 clear"（更保守但多批次常态下批次永远无法清理） |
| D-B2 | converge 复扫次数（撤完重扫一遍证残单=0） | 单次复扫（§2.1⑦） | 不复扫（依赖撤单 API 成功返回）——省 2 次 API 但留下生效延迟窗口 |
| D-B3 | `id_known=False` 且 L2 无匹配的未决 registry 条目终态化为 ABSENT | 终态化（position=0+扫描 zero 双证据下安全） | 保持未决 → proof 条件④永不成立 → 批次卡死只能人工（更保守但可用性差） |
| D-B4 | L3 孤儿单是否阻塞 clear | 不阻塞（P0-6 语义：非本批次资产，列示告警，v3 §3.1 proof 契约） | 阻塞（更保守，但无主单永远无人处置时批次永不清理——把 L3 告警升级成 clear 死锁） |
| D-B5 | `close_position_market` 同步路径 converge 失败的返回语义 | 平仓成功照报 + 附"残单收敛未完成，监控线程将继续清理"（平仓本身已成功，不能谎报失败） | 整体返回 False（用户会误以为平仓失败而重试 → 重复平仓请求） |

---

## §8 实施边界（钉死，与 v3 §6 一致）

- **做**：B0 结算撤 TP、`_converge_batch_orders_before_clear`、proof 门
  clear（含 close_phase=3 唯一写入）、10 调用点两行式插码、
  `test_b_batch.py`、锚点重对齐；
- **不做**（承 v3 终审"明确禁止"清单）：StateManager / Event Sourcing /
  数据库 / CAS / Redis / 状态机框架 / 大规模锁改造 / L3 自动撤 / proof 强制
  逃生门 / 回滚 N14 / 新增后台 converge 周期线程；
- **实施纪律**：RED 基线先行归档 → 实施 → G-B1~G-B10 全绿 → 回归链 1-5 →
  备份 `backups/20260829_p0_batchB_before_/` → 一次性封版 commit（生产代码 +
  test_b_batch.py + 锚点修复测试文件，log 不入库）→ 呈报 → 实盘前最终审计。

---

## 附：本轮源码证据清单（全部 2026-08-29 在 01bb44f 工作树 Read/Grep 复核）

- `trader_260725.py` 总行数 7230；`clear_batch_state` def L1338-1371（墓碑链 +
  L1340 "Batch B 接 proof 门"预告注释）；
- clear 调用点 10 处：L1789 / L4556 / L4622 / L4630 / L4699 / L5152 / L5321 /
  L6049 / L6071 / L6792（Grep 穷举，无第 11 处）；
- `_monitor_limit_close` def L7083；结算段 close_phase=2 写 L7148-7150、撤 SL
  L7156-7166（**TP 零处理实证**）；市价结算 L6780-6792；
- `close_position_limit` 撤 TP（N14）L6959-6974，L6973 "Batch B 兜底"注释；
- 双通道扫描范式：`_self_heal_no_id` def L4055，L4086-4093 双 fetch、
  L4097-4103 orders_by_id 去重；
- L1 全集：`_collect_batch_order_ids` def L1425；`_MERGE_ID_MIRROR_FIELDS` L43；
  `_REGISTRY_TERMINAL_STATES` L48；
- L2 锚：`_order_matches_intent` def L3677；identity 反查
  `_find_registry_identity_by_order_id` def L3350；
- 锁内事务：`_commit_registry_txn` def L3463；`load_all_states` L1268；
  `save_batch_state` L1296；墓碑 `_load_tombstones` L1375 / `_persist_tombstones` L1389；
- position：`_get_current_position_amt` def L2398（fetch_positions L2402）；
- G3a helper：L3238（撤单）/ L3259（核账）；
- 撤单幂等先例：`_cancel_limit_close_order` def L6849（-2011 分支 L6865-6867）；
  `_cancel_remaining_entries` def L2354（except 吞错盲区）；
- 启动 stale 判定：L1645（monitor_error 直清——无撤单实证）/ L1702（无挂单
  无持仓已验后清）。
