# P0 平仓竞态修复规格 v3（终审稿）

> 依据：规格 v2 + ChatGPT 第三轮 P0 裁定（2026-08-28 深夜）。本轮任务 = 把裁定
> **P0-1～P0-7 七条边界语义写死**。**不改任何生产代码**。新增行号全部本轮在
> HEAD=c147543 工作树 Read/Grep 实证（文末 git 验证）。
>
> v2 已被裁定"总体架构通过"，故本文是 **v2 的修订增量稿**：§2 归属判定、§3 墓碑、
> §1.1 create 全集矩阵（14 处）等未裁改动的内容**沿用 v2 原文不重复**；本文只写
> 七条裁定引发的修订 + 本轮新取证。读者（ChatGPT/用户）应对照 v2 阅读。

---

## §0 三轮裁定采纳总表

| 裁定条 | 内容 | 本稿落点 | 状态 |
|---|---|---|---|
| P0-1 | close_phase 唯一权威，Boolean 降级派生 | §2 | ✅ 已钉死 |
| P0-2 | G3 ≠ 简单 cancel；按订单最终状态分支收敛 | §1.3 | ✅ 已钉死 |
| P0-3 | G3 复核+Commit 构成不可穿插的原子边界 | §1.2 | ✅ 已钉死（新源码证据） |
| P0-4 | clear_batch_state 需 convergence proof，Fail-Closed | §3 | ✅ 已钉死 |
| P0-5 | fetch 异常 ≠ EMPTY → CONVERGENCE_UNKNOWN 禁 clear | §4 | ✅ 已钉死 |
| P0-6 | L3 无主单维持"只告警不撤" | v2 §2 原样采纳 | ✅ 裁定直接批准 |
| P0-7 | 批次顺序 A → C → B | §6 | ✅ 已重排 |

另采纳裁定 §9：`user_modified` 移出安全棘轮 → §5。

### §0.1 本轮新取证（P0-3/P0-4 的直接依据）

1. **G3 原子边界的可行性证据——既有锁拓扑**：
   - `_state_lock`（L153 `threading.Lock`，**非重入**）是全部 state 落盘的序列化点：
     `save_batch_state` L1268 / `clear_batch_state` L1278 / `_record_realized_pnl` L620。
   - `_persist_states`（L1248-1265）docstring 明确契约：**"调用方必须已持有
     _state_lock"** —— 持锁直写是项目既有合法范式（.bak 备份 + os.replace 原子写）。
   - **现状 TOCTOU 实证（裁定预判成立）**：`_update_registry`（L2956-2987）=
     `load_all_states()`（锁外）→ 内存改 registry → `save_batch_state()`（锁只在
     写盘瞬间持有）。**"复核"若放在调用方则完全在锁外** → 关闭线程可在复核与
     Commit 之间穿插写 CLOSE_REQUESTED。G3 必须改变这一结构（§1.2）。
2. **UNKNOWN ≠ EMPTY 既有先例（P0-5 的对齐锚点）**：`_verify_order_created`
   L2854/L2873（"网络未知不能被当成不存在"）、`_classify_create_exception`
   L2919（"网络未知 ≠ 创建失败"）——converge 阶段的 fetch_open_orders 异常处理
   直接复用同一哲学，非新原则。
3. **fetch_order 带条件单路由参数的既有惯例**：L2863-2865 `params={'stop': True}`
   （algo 端点；不带则恒假阴性，C5 事故根因）——G3a 获取订单最终状态必须沿用，
   普通/限价平仓单走 `order_kind='normal'` 默认端点（L2866-2867）。

---

## §1 G3 重定义（P0-2 + P0-3）——本轮最大修订

### 1.1 总体结构（裁定通过的主架构，不变）

```
G1（_assert_create_allowed 闸门扩展，读 close_phase）
  ↓
G2（create 紧前复核，11 处一行式插码，v2 §1.2 不变）
  ↓
create_order()
  ↓
G3a（订单状态收敛——锁外，可发 API）
  ↓
G3b（原子提交边界——持 _state_lock，零 API）
  ↓
registry CONFIRMED
```

G1/G2 落点沿用 v2 §1.2（G1 零插码、G2 11 处、平仓单 C1/C2 豁免），唯一修订：
**G1 的关闭态检查只读 close_phase（P0-1），不再以 Boolean 为判据**。

### 1.2 G3b：原子提交边界（P0-3 的实现规格）

**替换** v2 的"G3 = verify 钩子内复核后撤单"表述。verify 钩子
（`_verify_and_update_registry` L2891 / `_verify_order_created` L2847）扩展为调用
新 helper：

```python
_commit_protection_if_active(symbol, batch_id, identity, order_id,
                              order_kind, verify_result, ...)
```

**实现契约（全部为本轮源码实证的既有构件，零新机制）**：

```python
with self._state_lock:                       # 既有锁（L153）
    all_states = self.load_all_states()      # 锁内读最新磁盘
    b = all_states.get(symbol, {}).get(batch_id)
    if b is None or 墓碑命中(batch_id) \
       or close_phase(b) >= CLOSE_REQUESTED:  # ← 最终关闭复核（P0-1：只读 close_phase）
        return G3A_TRIGGERED                  # 不写 CONFIRMED，转 §1.3 收敛分支
    # —— 复核与 Commit 之间无线程穿插点：同一持锁段内完成 ——
    更新 b['protection_registry'][identity]   # CONFIRMED / order_id / id_known
    self._persist_states(all_states)          # 既有持锁直写范式（L1249 契约）
    return COMMITTED
```

**互斥论证（为什么关闭线程插不进来）**：CLOSE_REQUESTED 的全部写入点
（三入口 v2 §Q1）与批次清理（`clear_batch_state` L1278）**同样必须先拿
`_state_lock` 才能落盘**。因此"关闭写入"与"G3b 复核+Commit"在同一把锁上
串行化——裁定要求的"程序内原子边界"由既有锁天然提供，**不引入 CAS/数据库/
新锁**（守住"最小修改"边界）。

**边界声明**：
- 锁内**零交易所 API**（G3a 的 fetch/cancel 全部在锁外完成）→ 无死锁面、
  无持锁延迟面。
- `_state_lock` 非重入 → G3b **不得**调用 `save_batch_state`/`_update_registry`
  （它们内部会再取锁 → 死锁）；必须按上例直接操作 + `_persist_states`。
- 其他线程在锁外 load→改→save 的既有竞态（registry 丢更新面）**不由 G3b 解决**，
  由 Batch C 的 §5 C 类 registry merge 兜底（两道正交防线）。
- G3a 返回 G3A_TRIGGERED 后的撤单/告警动作在**锁释放后**执行（避免锁内 API）。

### 1.3 G3a：订单状态收敛分支（P0-2 的完整规格）

**替换** v2 的"发现关闭 → cancel_order + HARD_LOCK"简化表述。

触发条件：G3b 返回 G3A_TRIGGERED（= create 已发生但批次已进入关闭/已清理）。
此时该订单**已在交易所**，不得简单 cancel，必须先获取其最终状态再分支：

```
fetch_order(order_id, symbol, params={'stop': True} if conditional else {})
    ↓ 取 order['status'] / order['filled'] / order['amount']
├─ OPEN 且 filled == 0
│     → cancel_order(order_id)
│     → 成功：registry=PROGRAMMATIC_CANCELED, reason='g3_race_canceled'
│     → 失败：HARD_LOCK + 🚨 critical（v2 Fail-Closed 语义保留）
│
├─ OPEN 且 0 < filled < amount   （= PARTIALLY_FILLED，ccxt 表现为 open+部分成交）
│     → cancel_order 撤余量（Binance 撤单即撤未成交部分）
│     → 重 fetch 确认终态 → 重新核验 position（_get_current_position_amt L2137）
│     → registry=PROGRAMMATIC_CANCELED, reason='g3_race_partial_filled',
│       filled 数额记入条目（供结算对账）
│
├─ CANCELED / EXPIRED / REJECTED
│     → 已收敛，无需撤：registry=PROGRAMMATIC_CANCELED,
│       reason='g3_race_terminal_' + status
│
├─ CLOSED（FILLED）
│     → 不进入 CONFIRMED（裁定原文）
│     → reduceOnly 保护单成交 = 风险已减少，**非异常、不 HARD_LOCK**
│     → 重新核验 position；registry=PROGRAMMATIC_CANCELED,
│       reason='g3_race_filled'，filled 价额记入条目供结算核账
│
└─ fetch 异常（UNKNOWN）
      → 状态未知 ≠ 不存在（P0-5 同哲学）
      → registry 保持 PENDING_VERIFY + hard_locked=True + 🚨 critical
      → 交 Batch B 两源扫描兜底（该 id id_known=True → L1 精确归属 → 自动撤）
```

**要点**（直接回应裁定一/二节）：
- FILLED/PARTIALLY_FILLED **不得被误判为 cancel failure**——上述分支已把
  "订单已执行（风险减少）"与"撤单失败（风险残留）"分开处理。
- PARTIALLY_FILLED 撤余量失败 → 落入 HARD_LOCK + critical，不 clear。
- 收敛后的 registry 状态统一为 **PROGRAMMATIC_CANCELED**（不新增第二种
  终态、不成第二套状态机——见 §1.4），差异全部编码进 `terminated_reason`。

### 1.4 registry 终态的维度声明（回应裁定八）

- `close_phase`：**batch 生命周期**维度（ACTIVE→…→CLOSED，§2）。
- `PROGRAMMATIC_CANCELED`：**订单生命周期**维度终态——含义扩为"程序主动终结
  的保护单（撤销，或经 G3 收敛）"，**不承载 batch 生命周期语义**，二者不互转。
- 转移规则不变（v2 §4.2）：不可转出、`_adjudicate` 遇 fetch canceled 不写
  ABSENT、闸门禁建且无 replace 豁免、保留 order_id。
- 写入点在 v2 §4.2 基础上**新增**：G3a 四个收敛分支（reason 前缀 `g3_race_*`）。

---

## §2 P0-1：close_phase 唯一权威，Boolean 降级

### 2.1 字段与取值

- `close_phase`：int，单调递增；`0=ACTIVE / 1=CLOSE_REQUESTED / 2=CLOSE_SETTLING /
  3=CLOSED`。数值单调 → 棘轮比较（磁盘 vs 快照取大）天然成立。
- **写入点**（唯一权威写入，均在持 `_state_lock` 的落盘通道内）：
  - `=1`：三入口（cancel_open_orders L5981 / close_position_market L6035 /
    close_position_limit L6226，v2 §Q1 实证）；合法回滚点两处（L6151/L6351）
    **收紧为仅 `1→0` 且仅当平仓单创建失败/被拒时**（回滚条件进 §6 Batch A 验收）。
  - `=2`：结算开始（`_monitor_limit_close` 结算段、市价结算段）。
  - `=3`：convergence proof 验证通过时（§3），紧邻 clear 之前。
- **迁移规则**：存量 trade_state.json 无此字段 → 读取侧统一 `missing == ACTIVE(0)`。
  无需一次性迁移脚本（增量写入，老批次首笔 save 时补齐）。

### 2.2 Boolean 降级规则（裁定三的精确表述）

| 字段 | 新角色 | 写入 |
|---|---|---|
| `pending_close` | 兼容字段（供旧日志/展示） | 与 close_phase=1 同点写入 |
| `is_programmatic_cancel` | 业务原因标记（哪类平仓） | 同上 |
| `settled_by_limit_close` | 结算方式标记 | 与 close_phase=2 同点写入 |

**硬规则**：
1. 任何安全闸门（G1/G2/G3b/R14 判定/维护段冻结/命令入口拒绝）**只读
   close_phase**；Boolean 只作冗余辅助证据（日志、告警文本）。
2. 禁止出现"close_phase≥1 而闸门因 `not pending_close` 放行"的路径——
   G1 闸门实现即查 close_phase，Boolean 不参与判定表达式。
3. v2 §4.3 R14 消费点表述相应改为"读 close_phase 与 registry
   PROGRAMMATIC_CANCELED（多重冗余）"，不再列 Boolean 为判据。

---

## §3 P0-4：clear_batch_state 的 convergence proof 门

### 3.1 设计（采纳裁定四：proof 参数 + 函数内验证，不搞 StateManager）

```python
def clear_batch_state(self, symbol, batch_id, proof=None) -> bool:
```

- **proof 对象**（由 Batch B 的 `_converge_batch_orders_before_clear` 产出）：

```python
{
  "batch_id": ..., "symbol": ..., "checked_at": ts,
  "scope": "FULL",                  # 或 "PRE_ENTRY"（见 3.2）
  "position_zero": True,            # fetch_positions 核验
  "state_ids_resolved": [id, ...],  # state 已知 id 全集的处置清单（撤销成功/已终态）
  "exchange_scan": "zero",          # 'zero' | 'unknown'（P0-5：异常时绝不写 'zero'）
  "l1_canceled": [...], "l2_canceled": [...], "l3_orphans": [...],
}
```

- **验证规则（clear 函数内，持锁后执行）**：
  1. `proof is None` 或缺任一必需键 → **拒绝 clear** + 🚨 critical
     （"无收敛证明的状态删除被阻断"）+ 返回 False。
  2. `exchange_scan != 'zero'` → 拒绝（P0-5，见 §4）。
  3. scope 校验：批次有已知订单 id / registry 非空 / 曾有持仓 → 仅接受 FULL。
  4. proof 有效 → 先写 `close_phase=3 (CLOSED)` + 墓碑（v2 §3 规则不变：
     含 converged_order_ids = l1+l2 成功撤销 id）→ 删除 state → 返回 True。
- **10 个调用点全部改造为**：先调 converge 拿 proof，再传 proof 调 clear。
  任何调用方"自觉"不再是唯一防线——**漏 converge 的调用点会在 clear 处被
  Fail-Closed 拒绝并告警**，缺陷可被发现而非静默放行。

### 3.2 proof 的两档 scope

| scope | 适用 | 必需条件 |
|---|---|---|
| FULL | 有持仓/有保护单/有平仓单的批次结算 | 裁定五"四条件" + 交易所扫描 zero |
| PRE_ENTRY | 未建仓退出（entry 条件单撤单路径，L4019-4026 类） | 入场单已确认终态（canceled/拒单）+ registry 全终态 + position=0 |

### 3.3 无程序侧逃生门（显式声明）

程序内**不提供**绕过 proof 的 clear 通道（不加 `force=True`）。异常卡死批次
（converge 反复失败）的恢复 = 运维离线人工处置（删/修 state 文件），沿用
D-005 恢复指引模式：程序侧 Fail-Closed 保持告警，人工动作在程序外留痕。

---

## §4 P0-5：converge 阶段 UNKNOWN ≠ EMPTY

- `_converge_batch_orders_before_clear` 内 `fetch_open_orders`：
  - 正常返回 → 按 v2 §2 三级归属（L1/L2/L3）处置。
  - **任何异常（NetworkError/Timeout/RateLimit/429…）→ `exchange_scan='unknown'`
    = CONVERGENCE_UNKNOWN**：不 clear、批次保持 close_phase=2（CLOSE_SETTLING）、
    🚨 critical（含"无法证明残单为零"文案）、下一监控周期重试。
  - **绝对禁止** except 分支把 open_orders 置 `[]` 继续走"扫描通过"——
    与 `_verify_order_created` L2854 / `_classify_create_exception` L2919 的
    既有 UNKNOWN≠EMPTY 哲学同源（§0.1-2）。
- 验收测试新增：FakeExchange 注入 fetch_open_orders 抛 NetworkError →
  clear 必须被拒 + critical 恰一次（防重复刷屏走既有告警去重）。

---

## §5 字段级 merge 修订（裁定九：user_modified 移出棘轮）

v2 §5.1 A 类拆分：

| 类 | 字段 | 规则变化 |
|---|---|---|
| **A 安全棘轮（只进不退）** | close_phase、pending_close、is_programmatic_cancel、settled_by_limit_close | 不变（P0-1 后这四个就是同一语义簇，取"更真"侧） |
| **G 事实/审计型（新类）** | **user_modified** | **不参与任何安全闸门判定**；merge 取 OR（任一侧 True 则 True）——保守取向：True 只抑制程序自动重建（R14 业务判定），误置 True 降低自动化而非增加风险；/tp /sl 人工修改语义不被生命周期绑死 |

B/C/D/E/F 五类规则不变（v2 §5.1）。§5.3 残余风险声明不变。

---

## §6 P0-7：实施批次重排 A → C → B（采纳裁定十/十一）

| 批次 | 核心目标 | 内容 | GREEN 验收（对应回放断言） |
|---|---|---|---|
| **A：冻结** | 进入平仓后不得增加风险 | close_phase 字段+三入口写相位+回滚收紧；G1（读 close_phase）/G2/G3a+G3b；R14/维护段/命令线程冻结；N14→PROGRAMMATIC_CANCELED（含 G3a 写入点） | GREEN A/B/C/D + 9 RED 中 A1/A3/B1/B3 翻绿 + §7 验收矩阵 |
| **C：防回退** | 旧线程不得把 CLOSE 写回 / 已清批次不得复活 | save 字段级 merge（§5 七类）+ 墓碑（v2 §3）+ G3b 锁边界联动验证 | GREEN F + B5 翻绿 + 复活单测（墓碑拦截） |
| **B：最终收敛** | clear 前交易所必须干净 | `_monitor_limit_close` 撤 TP；converge 两源扫描（v2 §2 L1/L2/L3）+ CONVERGENCE_UNKNOWN（§4）+ proof 门 clear（§3） | GREEN E/G + A4/A5/A7/B4/B7 翻绿 + UNKNOWN 注入测试 |

**顺序理由（裁定原文采纳）**：C 是 B 的状态一致性前置——否则 Batch B 在
CLOSE_SETTLING converge 期间，旧监控快照仍可能回写污染 state/registry。
**依赖核对**：v2 原担心的"B 的 L1/L2 依赖 PROGRAMMATIC_CANCELED 终态"
不受顺序影响（终态在 Batch A 落地）；C 的 D 类规则锚 registry 终态同样已在
Batch A 就绪 → **A→C→B 无依赖冲突**。

每批纪律不变：对应断言翻绿 → 全量回归 30 文件 → 备份 → 呈报。
**明确不做**（承二/三轮裁定）：StateManager / Event Sourcing / 数据库 / CAS /
Redis / 状态机框架 / 大规模锁改造 / 回滚 N14 / L3 自动撤 / proof 强制逃生门。

---

## §7 验收标准（终版，吸收 P0-1~P0-7）

**一句话验收（裁定十二原文，写入宪法不变量⑩）**：

> 一个 batch 一旦进入 CLOSE_REQUESTED，任何线程都不得再增加风险、创建保护单、
> 修改保护单——除非该操作属于"撤销/清理已有风险"。

逐路径验收矩阵 = v2 §6.2 基础上追加四行：

| 路径 | 验收 |
|---|---|
| G3a FILLED 分支 | 注入"create 后订单已成交"→ 不 HARD_LOCK、不 CONFIRMED、position 重核验、reason='g3_race_filled' 落 registry |
| G3a PARTIALLY_FILLED 分支 | 注入部分成交 → 撤余量 + position 重核验 + 不 clear 直至收敛 |
| G3b 原子边界 | 并发驱动：G3 挂起于复核前 + 关闭线程写 CLOSE_REQUESTED + 释放 → 必须走 G3A_TRIGGERED 而非 COMMITTED（专用竞态测试） |
| proof 门 | 无 proof 调 clear → 拒绝 + critical；exchange_scan='unknown' → 拒绝；scope=PRE_ENTRY 用于有持仓批次 → 拒绝 |

---

## §8 决策点状态更新（v2 §8 六点 + 本轮）

| # | 决策点 | 状态 |
|---|---|---|
| D1 | L3 无主单告警 vs 自动撤 | **裁定已决：维持只告警**（P0-6） |
| D2 | G2 落点 = 11 处一行式插码 | 未被反对，按 v2 倾向采纳（待用户最终确认） |
| D3 | 墓碑 TTL = 7 天 | 未被反对，采纳（同上） |
| D4 | D 类 id 镜像按 registry 终结态作锚 | 未被反对，采纳（同上） |
| D5 | `b is None` 语境区分 = helper 增参 `require_live_batch=True` | 未被反对，采纳（同上） |
| D6 | N14 并入 Batch A | 未被反对，采纳（同上） |
| — | G3b 持锁直写 `_persist_states`（§1.2） | 本轮新增，需终审确认 |
| — | user_modified 归 G 类取 OR（§5） | 本轮新增，需终审确认 |

---

## 附：本轮新增源码证据清单（全部本轮 Read/Grep 复核，HEAD=c147543）

- `_state_lock` 全部使用点：L153（定义，threading.Lock 非重入）/ L620 / L1268 /
  L1278 —— 状态落盘单锁序列化点实证
- `_persist_states` L1248-1265：docstring"调用方必须已持有 _state_lock" +
  .bak + os.replace 原子写
- `save_batch_state` L1267-1273 / `clear_batch_state` L1275-1285：整批覆盖 /
  纯删除零 API，签名与锁边界
- `_update_registry` L2945-2988：load（锁外）→改→`save_batch_state`（锁只在
  写盘瞬间）——**G3b 修订的直接动机：现状复核与 Commit 之间存在穿插窗口**
- `_verify_and_update_registry` L2891-2910：三态 verify→registry 映射（G3 扩展点）
- `_verify_order_created` L2847-2889：fetch_order 条件单 `params={'stop': True}`
  惯例 + UNKNOWN≠EMPTY 先例（L2854/L2873）
- `_classify_create_exception` L2914-2924：UNKNOWN≠EMPTY 第二先例（L2919）
- `_get_current_position_amt` L2137：G3a position 重核验复用点

---

## §9 终审批准补遗（2026-08-28 ChatGPT 终审：APPROVED，三条硬约束入规格）

> 终审裁定：v3 整体批准进入 Batch A 实施。以下三条为本终审新增的**最终硬约束**，
> 与 §0 七条裁定同级，写入规格与测试验收；另加一条正交不变量。

### 9.1 硬约束①：G3b 必须使用锁内重新读取的最新磁盘 state

- **禁用 G3a/verify 阶段缓存的旧快照**：G3b 契约代码（§1.2）中
  `load_all_states()` 必须在 `with self._state_lock:` 段内**重新调用**，
  不得复用调用方（verify 钩子/G3a）传入的任何内存快照。
- 源码依据（已实证）：`load_all_states`（L1239-1246）每次调用均磁盘
  `json.load` 返回全新 dict → 锁内重读即磁盘最新。
- **验收**：测试注入"G3a 阶段快照 close_phase=0、锁内磁盘已被关闭线程写 1"
  → G3b 必须返回 G3A_TRIGGERED 而非 COMMITTED。

### 9.2 硬约束②：G3a 成交判定必须联合 filled + amount + status

- 判定优先级：**数量事实（filled/amount）第一优先级，status 第二**。
- `status='open'` 且 `filled > 0` → **PARTIALLY_FILLED**（部分成交），
  按 §1.3 第二分支处理（撤余量 + position 重核验），**绝不得按"未成交"处理**。
- filled 与 status 冲突时（如 status='canceled' 但 filled>0）→ 按已有成交事实
  处理（filled>0 即风险已部分/全部减少），走核账路径而非简单 ABSENT。
- **验收**：FILLED / PARTIALLY_FILLED 注入测试（§7 验收矩阵已有两行）。

### 9.3 硬约束③：user_modified 只是事实字段，绝非授权条件

- §5 G 类规则的基础上再钉死：`user_modified` **不得出现在任何安全判定
  表达式**——G1 / G2 / G3b 复核 / clear proof 校验 / close_phase 写入条件
  中一律不可引用。它只参与 merge 取 OR 与 R14 业务判定（抑制自动重建）。
- **验收**：grep 断言——新代码中 `user_modified` 仅出现在 merge/OR 赋值与
  R14 判定处，闸门函数内零引用。

### 9.4 正交不变量：PROGRAMMATIC_CANCELED 不反向驱动 close_phase

- 订单终态（PROGRAMMATIC_CANCELED）只承载订单生命周期（§1.4）；**任何
  订单级终态写入都不得触发/推导 batch close_phase 的变更**。close_phase 的
  写入点仍严格限定 §2.1 三入口+结算+proof 门。
- 实现双保险：`_adjudicate_recreate_before_repair` 遇 PROGRAMMATIC_CANCELED
  早返回 hold（不补挂）；`_update_registry` 终态守卫（不可转出）。

### 9.5 实施偏离的诚实记载：G2 落点在 PENDING_CREATE 写入之前

- G2 统一插码模式 = "gate 调用后追加 `_final_pre_create_check` 复核、失败
  流入既有 not-allowed 分支"——零重缩进、复用既有失败语义。
- **代价**：G2 实际落点在 `PENDING_CREATE` registry 写入**之前**、
  create_order 之前，而非字面"create 紧前"（create 与 PENDING_CREATE 之间
  仅隔 create_order 调用本身）。create 之后到 G3a/G3b 之间的残余竞态窗口
  **由 G3 全覆盖**（这正是 G3 存在的理由）。
- 曾评估的替代方案及否决理由：
  - create 紧前独立 if-block → 11 处位点需大段重缩进（verify+commit 侧效链
    无法绕过），违反最小修改纪律；
  - raise 专用异常 → 被位点既有 except 经 `_classify_create_exception` 误
    分类为 PENDING_VERIFY，语义污染。

### 9.6 实施链（终审确认）

Batch A（冻结）→ GREEN → Batch C（防回退）→ GREEN → Batch B（最终收敛）→
全量回归 → 实盘前审计。**明确禁止**：StateManager、数据库、CAS、Redis、
Event Sourcing、大锁（终审原文重申）。
