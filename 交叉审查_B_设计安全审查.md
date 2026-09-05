# 交叉审查 B：市价平仓 -4061 修复方案的**设计安全性审查**

> 审查对象：`事故_市价平仓-4061_精确diff_送审ChatGPT.md`（v2）
> 被审代码：`trader_260725.py` @ `e953d79`（7858 行）、`G:/tmp/new_helpers_after.py`、
> `test_position_close_confirmation.py`、`test_merge_rollback_semantics.py`、`test_ast_rollback_guard.py`
> 审查立场：** adversarial design review**。不复述方案优点，只找它会在哪里出错。
> 约束遵守：生产文件与既有文档零改动；探针只读（零交易所 API、零生产写盘）。
> 探针产物：`G:/tmp/review_b_delta_probe.py`、`G:/tmp/review_b_guard_bypass.py`、
> `G:/tmp/review_b_merge_probe.py`、`G:/tmp/guard_bypass_fixtures/`

---

## 〇、一句话结论

**方向对，判据错，落点险。**

方案正确识别了两个真问题（-4061 参数契约、棘轮吞回滚），但**为「必须修 1」选的 delta 判据在数学上无法归因**——它无法区分「我的单成交了」和「别人的仓位减少了」，而后者在本次事故的真实环境（同 symbol 同方向多批次并存）里是常态事件。确认门因此会在最危险的时刻给出**假确认**，直接制造它本要防止的裸仓。同时，确认失败的代价被严重低估：它落进的是一个**无自愈、无重试、无二次告警的永久冻结态**，而方案修复的回滚通道对这条路径完全无效。

**需修 5 处致命 + 6 处需明确才能送审。**

---

## 一、先确认：文档对源码的描述是否属实

在攻击设计之前，先验证文档引用的源码事实（这是 B 点要求做的实证）。结论：

| 文档主张 | 源码实证 | 判定 |
|---|---|---|
| 「P0 Batch A 异常通道」真实存在 | ✅ `trader_260725.py:7115-7125`（市价）/ `7656-7665`（限价） | 属实 |
| 该通道「不回滚关闭标记」 | ✅ `if close_order_placed:` 分支直接 `return False`，不触碰任何状态字段 | 属实 |
| 该通道「发 🚨 critical 告警」 | ✅ `send_tg_notification(..., level='critical')` | 属实 |
| 该通道「SL/TP 保持不动」 | ✅ 确认门在撤 SL/TP 之前，raise 后控流直接跳到 except | 属实 |
| 「冻结跳过全部 SL/TP 维护」 | ✅ `L5244-5248` 的 `continue` 位于 `L5257+`（SL 补挂 / F3 收养）、`L5281+`（在场性校验）、TP R14 / 保本 / 滚动止损之前 | 属实 |
| 「重启不自愈」 | ✅ `bot_runner.py` 全文 grep `close_phase\|pending_close\|is_programmatic_cancel` **零命中**；`watchdog.py` 同样零命中 | 属实 |
| 「多批次同向并存是常态」 | ✅ `L5202-5216`（持仓覆盖区分单/多批）、`L4973-4979`（部分减仓检测区分）均为此写了分支 | 属实 |
| `settled_by_limit_close` 全库读取点 | ✅ **仅 1 处**：`L4880`（防重复结算） | 属实 |
| `_get_current_position_amt` side 传错返回 0.0 | ✅ `L2661-2670`：循环内不匹配则落到循环外 `return 0.0` | 属实 |

**文档对源码的描述是准确的**，没有发现"描述与源码不符"这一类致命问题（B 点担心的情形）。问题**全部出在新增设计本身**。

---

## 二、🔴 致命（5 条）

### B-01 delta 判据无法归因：他方减仓 = 假确认 → 裸仓

**代码位置**：`new_helpers_after.py:75` `if delta >= expected - tol: return True`
**调用位置**：改动 1 AFTER，`create_order` 成功之后、撤 SL/TP 之前

**问题**：`_read_position_amt` 读的是 **symbol + 方向的总敞口**（`new_helpers_after.py:30-42` 对所有匹配行求和），不是本批次的敞口。`delta` 因此衡量的是**全市场参与者对该 symbol 该方向的净影响**，而不是"我的平仓单成交了多少"。

任何第三方减仓都会被记到本批次头上：

- 同 symbol 同方向的**另一批次触发 SL**（`L5296` 检测到 SL 成交 → 该批走结算）
- 用户在交易所 App 手动平仓
- 强平 / ADL / 爆仓
- 另一批次的限价平仓单成交（`_monitor_limit_close` `L7714`）

**实测证据**（`G:/tmp/review_b_delta_probe.py` S8 / S8b）：

```
[S8] 本批次市价单未成交，同向另一批次被 SL 平掉 0.001
   before=0.002  after=0.001  expected=0.001
   -> confirmed=True   detail=敞口 0.002 → 0.001（减少 0.001，预期 0.001）
   🚨 假确认：确认门放行 → 将撤销 SL/TP，而本批次 0.001 仍在场 = 裸仓

[S8b] 三批并存 0.003 → 0.001，本批只占 0.001，另两批被他方平掉
   -> confirmed=True   delta=0.002 ≥ 0.001，他方减仓被误记为本批成交
```

**最尖锐的一点**：S8 的观测数据（`before=0.002, after=0.001, expected=0.001`）与方案用来**论证 delta 正确性**的 S3（`test_position_close_confirmation.py:132-135`）**完全同形**。也就是说，方案的正样本 S3 同时就是假确认的正样本——这两个场景在 delta 判据下**物理不可区分**。测试集里没有任何负向对照能分开它们。

**后果链**：确认门放行 → `L7031-7045` 撤 TP、撤 SL → 本批次 0.001 仍在场且**无任何保护单** → 同时状态进入 `close_phase=2` → 监控冻结（`L5244`）→ 不补挂 → **裸仓 + 无告警 + 不恢复**。这正是本次事故审计最不应该引入的结果，而且触发条件（另一批次 SL 成交）在剧烈行情下恰恰与"我要市价平仓"高度同时发生。

**处置（必须）**：判据从"持仓维度"换成"订单维度"——
1. `create_order` 返回的 `order['id']` → `fetch_order(order_id)` 查 `status` 与 `filled`；`status in ('closed','filled') and filled >= expected - tol` 才算确认。这是**每单独立、可归因**的，天然免疫多批次、免疫他方减仓、免疫方向传错。
2. delta 降级为**二级交叉校验**（可选告警），不再作为放行判据。
3. 若坚持用 delta，则必须叠加"本批次专属锚点"，而这在交易所 API 上做不到——所以只能走 1。

### B-02 确认失败 = 永久冻结，而修复 B 的回滚通道对这条路径完全无效

**代码位置**：`trader_260725.py:7119-7125`；冻结判据 `L5244-5248`

**问题**：确认门抛 `RuntimeError` 时，`close_order_placed` **必然为 True**（`L7028` 已置位），因此走的是"不回滚"分支：

```python
if close_order_placed:
    self.send_tg_notification(f"🚨【资金安全】市价平仓单已发出但后续结算异常（未回滚关闭标记）！…", level='critical')
    return False, f"❌ 市价平仓结算异常（平仓单已创建，close_phase 保持）: {e}"
```

状态停在 `close_phase=1 / pending_close=True / is_programmatic_cancel=True`。**这是本次事故的原始缺陷（缺陷 B）的精确复现**：

- 监控永久跳过 SL/TP 维护（`L5244-5248` 的 `continue`）
- 冻结分支**只有 `print`，不发 TG**（`L5246`）→ 此后再无任何告警
- `bot_runner.py` / `watchdog.py` 均无 `close_phase` 引用 → **重启不自愈**
- 平仓不会被重试（没有任何驱动者）

**而方案修复的 B（`allow_flag_rollback`）只在 `close_order_placed=False` 时启用**。两者是**正交**的：确认门失败的所有路径都不经过回滚通道。

**这意味着方案新增的确认门，把一类高频软失败（读数抖动、亚秒级传播延迟、他方未减仓、台账漂移）升级成了一类不可逆硬失败（永久冻结）**。修复缺陷 B 的同时，新增了一条通往同一个缺陷 B 的路。

**处置（必须）**：
1. 确认失败必须**分级**：`delta <= 0`（敞口完全没动）才算"未成交"；`0 < delta < expected`（部分成交）应记为 critical 并**保留可重试/可补平的能力**，而不是一视同仁 raise。
2. 为冻结态加出口：启动时 reconcile（`close_phase==1` 且无在途平仓单 → 回落 0 或直接重新驱动平仓），或冻结超过 N 分钟发**重复** critical（现在是一次性的）。
3. 明确写清"确认失败后人工如何恢复"——方案 Test 3 只验证了成功回滚路径，没有验证确认失败路径的恢复动作。

### B-03 `expected > pos_before` → 永远判不通过 → 该批次永久不可平

**代码位置**：改动 1 AFTER 用 `current_filled_amount`（**台账名义量**）作为 `expected`；`new_helpers_after.py:62` 只校验 `expected <= 0`，**不校验 `expected <= pos_before`**

**触发条件**（都不是边缘场景）：
- 上一次平仓尝试部分成交（B-02 冻结后人工重试 → 必走这条路）
- 仓位被 SL 部分平掉而 `last_filled_count` 未同步
- 用户手动减仓
- 台账 `target_amounts` 与交易所实际漂移（`L5202-5216` 明确承认这种漂移存在，且在多批次下**主动放弃修正**）

**实测证据**（探针 S9）：

```
[S9] 本批台账 0.001，实际剩余敞口只有 0.0005
   before=0.0005  after=0.0  expected=0.001
   -> confirmed=False   detail=敞口未如预期减少：before=0.0005 after=0.0 预期减少>=0.001
   🚨 即使仓位真实归零也判不通过 → 永久冻结
```

**后果**：唯一的人工补救手段（再点一次平仓）会走进**同一个判据**，再次失败，形成死循环。此时仓位还在、SL/TP 还在，但系统拒绝承认任何平仓成功，批次永远卡在 `close_phase=1`。

**处置（必须）**：`expected := min(current_filled_amount, pos_before)`；并把 `pos_before` 落到 state 里，供重试/对账使用。

### B-04 市价平仓无重入保护 + 去掉 `reduceOnly` 后依赖未经确认的交易所语义

**代码位置**：`trader_260725.py:6951-6984`（入口）；`bot_runner.py:1493-1497`

**问题 1 —— 无重入保护**：`close_position_market` 入口只查 `is_active` 和 `current_filled_amount > 0`，**不检查 `close_phase` / `pending_close`**。而 `bot_runner.py:1493` 对每个回调都 `run_in_executor` 起**新线程**。全库只有 5 把锁（`L194/202/206/214/221/235`：`_api_lock` / `_state_lock` / `api_cooldown_lock` / `_active_monitors_lock` / `_gate_alert_lock` / `_auth_recovery_lock`），**没有任何 per-batch 锁**。

→ 双击按钮 / 网络重试 / 用户在 Telegram 上连点两次，会**并发跑两个完整的平仓事务**。

**问题 2 —— 参数语义未确认**：改动 1 在 hedge 模式下**移除** `reduceOnly`、改用 `positionSide`。而"SELL + positionSide=LONG 在无 LONG 仓位时会被拒绝、不会反向开仓"这一语义，正是文档 §四 情况 B **自己标注为「待交易所语义确认，不用于升/降级」**的那一条：

> 我不据此下结论……**按纪律，标为"待交易所语义确认"，不用于升/降级**

现在它却成了修复方案的核心落点，被用来发真实的市价平仓单。这是文档内部的不一致。

**两个问题叠加的后果**：

| 线程 A | 线程 B |
|---|---|
| pre-read `before=0.001` | pre-read `before=0.001`（同一时刻） |
| `MARKET SELL 0.001 positionSide=LONG` → 成交 | `MARKET SELL 0.001 positionSide=LONG` → LONG 敞口已为 0 |
| 确认门：delta=0.001 ≥ 0.001 → **通过** | 确认门：delta=0.001 ≥ 0.001 → **通过**（用 A 的成交当自己的证据，B-01） |
| 撤 SL/TP | 撤 SL/TP（已撤） |
| 结算 → clear | 结算 → clear |

若交易所语义是"接受"而非"拒绝"，第二张单会开出**反向仓位**（无 `reduceOnly` 约束），而确认门对两张单都放行，SL/TP 被撤，批次被 clear → **留下一个无人看管、无保护的反向仓位**。

**处置（必须）**：
1. per-batch 平仓互斥：`close_phase >= 1 or pending_close` 时直接拒绝二次进入（或 `threading.Lock` per batch_id）。
2. 下单前 `amount := min(current_filled_amount, 实际持仓读数)`。
3. 加 `clientOrderId` 保证幂等（源码 `L7576` 自己就写着「C5：create_order 非幂等，禁止盲重」——方案在同一个文件里违反了自己已记录的纪律）。
4. 上线前用**小额真实单**确认"超额 SELL + positionSide=LONG"的交易所行为，或在 params 上保留一条不会反向开仓的兜底。

### B-05 撤 ENTRY 的「交易所侧校验」证明不了「已撤销」——对「已成交」是 Fail-Open

**代码位置**：改动 2 `_cancel_and_verify_entry_orders`，校验逻辑为 `fetch_open_orders` → 判"不在挂单列表" = 清零（`new_helpers_after` 对应段落）

**问题**：一个订单不在 `open_orders` 里，有两种互斥的可能：**已被撤销**，或**已成交**。这两种情况在 `fetch_open_orders` 的返回上**完全相同**，而它们的后果完全相反：

- 已撤销 → 目标状态 ✅
- 已成交 → **仓位被加回去了**，而此时：
  - 平仓确认门**已经通过**（确认门在撤 ENTRY 之前）
  - SL/TP **已经被撤**
  - 批次即将进入 `close_phase=2` → `_converge_batch_orders_before_clear` → `clear_batch_state`

→ 新增的仓位**既无 SL/TP，也即将失去监控**（批次被 clear），成为真正的孤儿仓位。

文档把这条校验描述为"Fail-Closed proof"，实际上它只在"查询失败"这一个维度上 Fail-Closed，在**"撤单 vs 成交"这个业务语义维度上是 Fail-Open 的**。

**触发概率**：撤单请求发出 → 校验查询之间存在网络往返（数十至数百 ms）。ENTRY 是 stop 条件单，在剧烈行情下被触发的概率恰恰在此时最高。

**处置（必须）**：校验改为二者之一：
- `fetch_order(oid)` 确认终态为 `canceled`（而非 `filled`）；或
- 撤单后**再读一次敞口**，要求"敞口没有增加"。

---

## 三、🟡 需要明确（6 条）

### B-06 回滚落盘后与监控线程结算态的竞态：`close_phase` 降级无下界

**代码位置**：改动 4 `merged['close_phase'] = int(snap.get('close_phase', 0) or 0)`；监控线程 `L7748-7751` 写 `phase=2`；`_state_lock`（`L202`）只保护单次 `save`，不保护跨调用的 load-modify-save

`allow_flag_rollback=True` 对 `close_phase` **没有任何下界约束**——`2`（CLOSE_SETTLING）也能被打回 `0`。

**实测证据**（`G:/tmp/review_b_merge_probe.py`）：

```
C2 disk={'close_phase':2, pending_close:True, prog_cancel:True, settled:True}
   snap=全清                                    allow_flag_rollback=True
   生产原样 -> close_phase=2  …  settled=True
   方案 AFTER -> close_phase=0  prog_cancel=False  settled=True   ⚠️ 正在结算的批次被解冻

C3 snap 缺 close_phase 键（陈旧快照）
   方案 AFTER -> close_phase=0  …                  ⚠️ 陈旧快照即可把 phase 打回 0
```

**后果**：若回滚抢在监控线程写完 `phase=2` 之后落盘，批次被**解冻** → 监控恢复保护单维护（`L5257+` SL 补挂 / F3 收养 / R14 补挂 / 保本移动 / 滚动止损）→ 在结算进行中复活补挂通道 → 孤儿 TP/SL 竞态（这是 `L7552-7558` 注释记录的 N14 事故通道）。

**注意**：这个竞态在生产里**原本不存在**，因为棘轮让回滚变成静默 no-op。方案把它从"静默失败"变成"可能危险的写入"，却没有同步引入互斥。

**另外**：方案的写后回读自证（改动 5a/5b 的 `_ok` 判定）只验证**"写进去了"**，不验证**"该不该写"**。C3 会被判 `_ok=True`，无告警。

**建议**：回滚限定为 `1 → 0`，且要求 `disk['close_phase'] == 1 and not disk.get('settled_by_limit_close')`；或在回滚路径上持锁复核。

### B-07 AST 守卫是测试期静态检查，实测 4/8 形式可绕过

**代码位置**：`test_ast_rollback_guard.py`

**实测证据**（`G:/tmp/review_b_guard_bypass.py`，喂给仓库里的**真实守卫脚本**）：

| 绕过形式 | 守卫 rc | 判定 |
|---|---|---|
| X1 `kw = {'allow_flag_' + 'rollback': True}; f(**kw)` | **rc=0** | 🚨 绕过 |
| X2 `exec("self.save_batch_state(s,b,d,allow_flag_rollback=True)")` | **rc=0** | 🚨 绕过 |
| X5 模块级 `lambda` 内调用（不在任何 `FunctionDef` 内） | **rc=0** | 🚨 绕过 |
| X8 跨模块调用（`bot_runner.py` / 任何新模块） | **rc=0** | 🚨 结构性绕过 |
| X3 `getattr(self,'save_'+'batch_state')(...)` | rc=1 | ✅ 抓到（callee 不匹配） |
| X4 `functools.partial(...)` | rc=1 | ✅ 抓到 |
| X6 `**{'allow_flag_rollback': True}` 内联 | rc=1 | ✅ 抓到 |
| X7 嵌套内层函数 | rc=1 | ✅ 抓到 |

**根因**：
- 守卫的 `stray` 检查用 `n.value == 'allow_flag_rollback'` **精确等值**（`L90`），字符串拼接是 `ast.BinOp` 不是 `Constant`，`exec` 的长字符串也不是精确等值 → 都漏。
- `collect()` 只对 `FunctionDef` 内部做 walk（`L76`），模块级 lambda 完全不在视野。
- `DEFAULT_TARGET = ... / 'trader_260725.py'`（`L42`）→ **跨模块调用天然不可见**。

**更根本的问题**：**AST 守卫不是运行时控制**。它不阻止任何调用，只在有人主动跑它的时候报警。把"防止 `allow_flag_rollback` 泛滥"这个安全目标寄托于一个测试文件，与方案自己强调的"Fail-Closed 应落在运行时"不一致。

**建议**：
1. 真控制点前移到 `save_batch_state` **运行时**：`allow_flag_rollback=True` 时用 `inspect` 校验调用栈中最近的帧是否为白名单函数；并针对 `close_phase` 加 B-06 的下界条件。
2. AST 守卫保留，但**降级**为辅助回归，不再作为唯一的授权机制（文档 §改动 6 称其"锁定授权范围"，表述过强）。
3. 补充 X1/X2/X5/X8 四个负向对照样本到 fixture 生成器。

### B-08 限价路径「先撤 ENTRY」的残留风险只覆盖了失败分支的一半

**代码位置**：`trader_260725.py:7541-7549`（撤单 `except Exception: pass`，**连 print 都没有**）；改动 5b（只在 `create_order` **失败**时告警）

**未覆盖的路径**：`create_order` **成功** 但部分 ENTRY 撤单失败。

此时：
- 限价平仓单挂着（可能几小时）
- 未撤掉的 ENTRY 在这期间继续成交
- 平仓单数量 < 实际持仓 → **残留仓位**
- **零告警、零校验**（改动 2 的交易所侧校验只加在市价路径）

**部分兜底**：`_verify_clear_proof` 要求 `position_zero is True`（`L7252-7253`）才会 clear，所以批次不会被误清理，但残留仓位会一直挂着，**没有 SL/TP**（限价路径保留 SL，但 SL 数量是平仓前的全额，与实际持仓不匹配）。方案没有说明这个状态的处置。

**建议**：限价路径也加撤单后校验（撤销 or 成交的二态判定，同 B-05）；并在 `_monitor_limit_close` 里周期性比对"实际持仓 == 平仓单数量"，不等则 critical。

### B-09 pre-read 的 Fail-Closed 方向用反了

**代码位置**：改动 1 AFTER

```python
pos_before = self._read_position_amt(...)
if pos_before is None:
    raise RuntimeError("平仓前读取持仓敞口失败（Fail-Closed：不发出平仓单）")
```

**问题**：平仓是**降风险**动作。对降风险动作做 Fail-Closed，等于"读不到持仓就不许逃生"。

Fail-Closed 在本项目里的正确用法是对**加风险**动作——例如 `_check_sl_coverage`（`L2685`）拒绝在无法证明覆盖时开新仓，这是对的。同一个纪律套到平仓上，方向是反的。

**实际触发**：
- `fetch_positions` 抖动 / 超时 → 紧急平仓被拒绝
- `AUTH_BLOCKED`（盲区安全模式 `L1166-1174` 直接 `raise AuthBlockedError`）→ 永远读不到 → **永远不能市价平仓**

后一种尤其严重：盲区安全模式下，用户唯一能做的风险动作（市价平掉）被这个 pre-read 门封死了。

**建议**：pre-read 失败应降级为"照常发单 + 事后用 `fetch_order(order_id)` 确认"（与 B-01 的处置一致）；Fail-Closed 只保留在"撤 SL/TP 之前必须有确认"这一处——那才是真正的不可逆动作。

### B-10 确认门的时序参数两头不落地

**问题 1 —— 轮询窗口太短**：`attempts=3, delay=0.6` → 名义窗口 **1.2s**（2 次 sleep）。对 Binance 仓位端点在剧烈行情下的传播延迟没有安全余量。而假失败的代价是**永久冻结**（B-02）。方案用 S6（1 次延迟）论证"轮询能救回来"，但没做延迟分布的实测。

**问题 2 —— 失败耗时太长**：`_read_position_amt` 走 `self._safe_api_call(...)` 且**未显式传 `retries`**，因此吃默认值 `retries=5, delay=2`（`L1160`）。按 `L1269-1296` 的分支：

| 失败类型 | 单次读数耗时 |
|---|---|
| 网络抖动/超时（`L1273` `sleep(delay*(i+1))`） | 2+4+6+8 = **20s** |
| 交易所维护（`L1285` `sleep(30)`） | **120s** |
| 429 限频（`L1255` 全局冷却 30-60s） | **120-240s**，且**阻塞所有线程** |
| 普通错误（`L1296` `sleep(2)`） | 8s |

确认门最坏情况：**分钟级**。而 429 路径会设置 `api_cooldown_until`（`L1256-1260`）触发**全局熔断**，把所有线程（包括监控线程的 SL/TP 维护）一起降速。

**净效果**：方案在最关键的平仓路径上新增 4 次 API 调用（1 次 pre-read + 最多 3 次确认）+ 1 次 `fetch_open_orders`，在限频环境下**提高了触发全局熔断的概率**，而全局熔断会削弱的正是保护单维护能力。

**建议**：读数显式 `retries=1~2`；确认门设总时间预算（如 5s）；超预算不 raise 而是降级为 critical + 保留冻结但**允许下轮重试**（与 B-02 联动）。

### B-11 §2.1(5) 为保留 `positionSide` 显式赋值给出的理由不成立；§2.1(1) 对 `leverage` 的断言错误

**（5）逻辑漏洞**：文档说

> `params_base` 里的 `positionSide` 是**建仓时刻**按当时的 `hedge` 检测结果写入的；而这里的分支判据是**账本里当前的** `is_hedge_mode`。两者来源不同、写入时点不同。

实际代码（改动 1 AFTER）：

```python
order_params = target_b_data['params_base'].copy()      # ← 来自 target_b_data
if target_b_data.get('is_hedge_mode', False):           # ← 同样来自 target_b_data
    order_params['positionSide'] = ...
```

两者都读自 `target_b_data`，是**同一份状态快照**。若 `is_hedge_mode` 过期，两处**同时**过期，显式赋值不提供任何额外保护。文档为"保留"给出的论证是无效的（保留本身无害，但不能用这个理由）。

真正的加固是下单前实时 `fetch_position_mode()`——探针 §7.2 已证明该调用可用。

**（1）事实错误**：文档称

> `workingType` / `leverage` 对 MARKET 单是无关参数

`leverage` 不是无关参数。Binance 下单请求携带 `leverage` 会**触发杠杆变更**，受"每 symbol 每 10s 一次"的变更冷却约束，可能返回 `-4028`（杠杆降低不支持）/ `-4168`（净仓位存在时不允许改杠杆）等错误。限价路径用过一次且成功，不代表 MARKET 路径稳定（两者的触发时机与账户状态不同）。

**建议**：`order_params` 显式构造为 `{'positionSide': ...}` 或 `{'reduceOnly': True}`，**不要**继承 `params_base` 的 `leverage` / `workingType`。

---

## 四、🟢 观察项（5 条）

### B-12 容差的实际作用被高估（但不是缺陷）

**实测**（探针 S10，`expected=0.001`）：

```
少平 0      (delta=0.0010000000) -> confirmed=True
少平 1e-09  (delta=0.0009999990) -> confirmed=True
少平 1e-08  (delta=0.0009999900) -> confirmed=True
少平 1e-07  (delta=0.0009999000) -> confirmed=False
少平 1e-06  (delta=0.0009990000) -> confirmed=False
```

`tol = 1e-8 + |expected|*1e-6 ≈ 1.1e-8`，相对 BTC 步长 `0.001` = `1.1e-5` 个步长。**精度噪声打不穿它**——提问中担心的"容差被精度噪声击穿导致误判通过"不成立。

但反过来：容差**只抗浮点误差，不抗任何真实短少**（少平 `1e-7` 即判不通过）。这不是缺陷（方向是保守的），只是文档把它描述成"抗浮点/精度误差"时应说明边界。

另：与既有 `_get_amount_precision`（`L7172-7188`）"用交易所 amount precision 而非裸 epsilon"的既定做法不一致——新 helper 又回到了裸 epsilon。建议统一到 `_get_amount_precision`。

### B-13 「防重复结算」的论证成立，但后果被高估

`settled_by_limit_close` 全库**只有 1 处读取**（`L4880`）：

```python
if latest_b_data.get('settled_by_limit_close', False):
    print(f"ℹ️ [限价平仓已处理] 批次 [{batch_id}] 跳过重复结算")
```

白名单排除它 → 不会被回滚 → **防重复结算的论证成立 ✅**。

但"重复结算"的实际后果是：`L4900-4970` 那条路径只做「发 TG 盈亏报告（`L4949`）+ 撤 SL/TP + converge + clear」，**不调用 `_record_realized_pnl`**（该函数只在 `L7109` 市价结算和 `_monitor_limit_close` 里调用）。所以即便 flag 被误清，直接后果是**重复/错误的盈亏播报**（用 `L4912` 的 ticker 现价代替真实成交价），**不是资金损失**。方案把它列为"绝不回滚"的头号理由，定级偏重——不影响结论，但影响后续维护者的判断权重。

### B-14 改动 5b 的 ENTRY 告警没有去重

改动 5b 的 `_attempted` critical 告警在**每次**限价挂单失败时发送，没有像 `_converge_alert`（`L7205-7221` 的 3 轮上限）那样做去重。反复挂单失败会刷屏，且在 TG 限频下可能挤掉更重要的告警。建议复用 `_converge_alert` 的去重机制。

### B-15 方案完全没覆盖的失败路径清单

| 路径 | 当前行为 | 风险 |
|---|---|---|
| 进程在确认门轮询期间被杀 | 停在 `close_phase=1`，`bot_runner.py` / `watchdog.py` 均无 `close_phase` → **重启不自愈** | 🔴 本次事故的原始缺陷，新代码新增了一条通往它的路 |
| `create_order` 超时但单已成交（非幂等，`L7576` 自承认） | 回滚清 flags → 孤儿平仓单成交后走 `L4900` 正常结算，用 `L4912` ticker 现价代替真实成交价 | 🟡 盈亏失真；且**无 clientOrderId**，重试会重复发单（与 B-04 叠加） |
| AUTH_BLOCKED 盲区模式 | pre-read 立即失败 → 拒绝发单（B-09） | 🟡 紧急平仓被封死 |
| 交易所维护 / 网络分区 | 确认门最坏分钟级阻塞，429 触发全局熔断拖慢所有线程 | 🟡 SL/TP 维护被连带降速 |
| `is_hedge_mode` 状态字段缺失/过期 | 非 hedge 分支下 `_read_position_amt` 会把 LONG+SHORT 两行**相加**，delta 语义失真 | 🟢 Fail-Closed 方向（delta 变小 → 判不通过），可接受但需说明 |

### B-16 §三 的候选排序值得重新考虑

文档把候选丙（限价路径只加校验、不改顺序）评价为"成本最低但缺陷 C 仍在"。但**丙恰好覆盖了 B-05 和 B-08**——那两处才是限价路径真正的资金风险点，而"缺陷 C（ENTRY 无法自动恢复）"的后果是运维负担而非资金损失。

建议改为**甲 + 丙合并**：限价路径保持先撤 ENTRY（甲），同时加上"撤销 or 成交"的二态校验与失败告警（丙）。

---

## 五、改动建议汇总

| # | 级别 | 问题 | 关键位置 | 建议处置 |
|---|---|---|---|---|
| B-01 | 🔴 | delta 无法归因 → 他方减仓假确认 → 裸仓 | `new_helpers_after.py:75` | 改用 `fetch_order(order_id)` 按单确认；delta 降为二级校验 |
| B-02 | 🔴 | 确认失败 = 永久冻结，回滚通道无效 | `trader_260725.py:7119-7125` / `5244-5248` | 失败分级；加启动 reconcile / 冻结超时重复告警 |
| B-03 | 🔴 | `expected > pos_before` → 永久不可平 | 改动 1 AFTER | `expected := min(台账量, pos_before)` |
| B-04 | 🔴 | 无重入保护 + 依赖未确认的交易所语义 | `6951-6984` / `bot_runner.py:1493` | per-batch 互斥；`amount := min(台账, 实际持仓)`；加 clientOrderId；上线前实测超额平仓语义 |
| B-05 | 🔴 | ENTRY 校验对"已成交"Fail-Open | 改动 2 | `fetch_order` 判终态，或撤后再读敞口要求未增加 |
| B-06 | 🟡 | `close_phase` 降级无下界，与结算态竞态 | 改动 4 | 限定 `1→0` + `not settled_by_limit_close`；回滚路径持锁复核 |
| B-07 | 🟡 | AST 守卫 4/8 可绕过，且非运行时控制 | `test_ast_rollback_guard.py` | 控制点前移到 `save_batch_state` 运行时；守卫降级为辅助 |
| B-08 | 🟡 | 限价路径撤单失败无告警无校验 | `7541-7549` | 限价路径也加二态校验 + 挂单期间持仓比对 |
| B-09 | 🟡 | pre-read Fail-Closed 方向用反 | 改动 1 AFTER | 读数失败照常发单，改用订单维度确认 |
| B-10 | 🟡 | 时序参数两头不落地（1.2s 太短 / 分钟级太长） | 改动 1.5 | 显式 `retries=1~2` + 总时间预算 + 降级为可重试 |
| B-11 | 🟡 | §2.1(5) 论证无效；§2.1(1) `leverage` 断言错误 | 改动 1 AFTER | 显式构造 params，不继承 `leverage`；实时 `fetch_position_mode()` |
| B-12 | 🟢 | 容差只抗浮点误差 | `new_helpers_after.py:64` | 统一到 `_get_amount_precision`；说明边界 |
| B-13 | 🟢 | 重复结算后果被高估 | `L4880` / `L4900` | 定级下调为"盈亏播报失真" |
| B-14 | 🟢 | ENTRY 告警无去重 | 改动 5b | 复用 `_converge_alert` |
| B-15 | 🟢 | 未覆盖路径清单 | 多处 | 逐条补：启动自愈 / clientOrderId / 盲区模式降级 |
| B-16 | 🟢 | 候选丙被低估 | §三 | 甲 + 丙合并 |

---

## 六、最终裁定

**需修 5 处致命（B-01 ~ B-05）+ 6 处需明确（B-06 ~ B-11）才能送审。**

不是"设计有根本缺陷需重做"——问题的定位（-4061 参数契约、棘轮吞回滚、P0 冻结的严重性、多批次并存的实证）**全部正确且证据扎实**，A 修复（参数）和 B 修复（受控逆向迁移）的骨架可以保留。

但**「必须修 1」的落点必须换判据**。当前的 delta 判据不是"有瑕疵"，是**在数学上无法回答它自己要回答的问题**：它回答的是"这个方向的总敞口少了多少"，而方案需要的是"我的这张单成交了多少"。在本次事故的真实环境里（同 symbol 同方向多批次并存、SL 会随时触发、用户会手动干预），这两者的差异不是边缘情况，是**每天都会发生的正常事件**。

送审前必须解决的最小集合：**B-01（换订单维度判据）、B-02（给冻结态一个出口）、B-04（加重入保护与幂等）**。这三条解决后，B-03 / B-05 可以合并进同一次改动；B-06 ~ B-11 需要在送审稿里明确写出处置或明确的"已知并接受"。

---

*审查人：交叉审查 B（adversarial design review）*
*审查日期：2026-08-30 ｜ 生产代码 HEAD：`e953d79`，工作树零改动已确认*
