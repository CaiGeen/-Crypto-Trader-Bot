# C5/SG4+SG4-B 设计草案（送审 ChatGPT）

> 状态：**草案 v2（已吸收 ChatGPT 评审裁决，待再审确认）** ｜ 日期：2026-08-19 ｜ 上游锚点：af64765（C4 已推送）
> 纪律：只读源码定位 → 盘点 → 分级 → 威胁模型 → 草案 → **评审闭环（本版已含）** → 测试红 → 最小修改 → 绿 → 回归 → commit

---

## 〇、ChatGPT 评审结论摘要（2026-08-19 第二轮）

> ChatGPT 裁决：**v1 方向正确，但不建议直接进入 TDD。需先修正 4 个关键规格问题。修正后 C5 收缩为干净的 P0。**

**C5 第一阶段只解决两件事：**
1. **所有 `create_order` 禁止盲重（`_safe_api_call` 不得重试 create_order）**
2. **A 级保护单必须 Create → Verify → Commit**

**除此之外一律不进入本阶段**：未知结果认领（reconcile）、孤儿订单仲裁、双实例协调、clientOrderId。

评审裁决明细见**附录 A**（10 条逐项闭环），正文已按裁决修订。

---

## 一、背景与目标

**不变量 6（Create ≠ Success）是唯一未闭环项**。SG4 = Create→Verify→Commit 提交一致性；SG4-B = 幂等性。
C4（SG3-P1）已完成**读侧**有效性验证（保护单存在 ≠ 有效）；C5 处理**写侧**提交一致性（创建返回 ≠ 订单存在）。

**C4/C5 职责分离（ChatGPT 审定）**：
- C4 = 读侧：**存在 ≠ 有效**（open_orders 周期校验，已落地 af64765）
- C5 = 写侧：**返回 ≠ 成功**（Create→Verify→Commit 同步事务边界）
- 不把 C4 的 `_check_protection_order_validity` 扩大成 C5 实现

---

## 二、14 处 create_order 调用点全表（当前源码行号，af64765）

| # | 行号 | 函数/路径 | 订单类型 | 级 | 幂等性表述（修订后） | 返回值→状态提交 | 失败路径 |
|---|---|---|---|---|---|---|---|
| 1 | 1071 | update_batch_tp（/tp） | TP_MARKET reduceOnly | A | API 非幂等 | id→tp_order_id→save | return False |
| 2 | 1182 | update_batch_sl（/sl） | STOP_MARKET reduceOnly | A | API 非幂等 | id→current_sl_id→save | return False |
| 3 | 1356 | _update_sl_no_validation（保本损/be） | STOP_MARKET reduceOnly | A | API 非幂等 | id→current_sl_id→save | return False |
| 4 | 1787 | execute_signal（首仓+加仓多层条件单） | STOP_MARKET 开仓单 | B | API 非幂等 | id→entry_orders→save | 跳过/raise |
| 5 | 2418 | 部分减仓后 SL 更新（M1） | STOP_MARKET reduceOnly | A | API 非幂等 | id→内存→save | 保留旧单+告警 |
| 6 | 2456 | 部分减仓后 TP 更新（M1） | TP_MARKET reduceOnly | A | API 非幂等 | id→内存→save | 保留旧单+告警 |
| 7 | 2903 | SL 恢复链（need_recover_sl） | STOP_MARKET reduceOnly | A | API 非幂等 | id→current_sl_id | sl_fail_count 熔断 |
| 8 | 2966 | SL 降级恢复（旧止损价） | STOP_MARKET reduceOnly | A | API 非幂等 | id→current_sl_id | 熔断+critical |
| 9 | 3043 | TP 恢复链（need_update_tp） | TP_MARKET reduceOnly | A | API 非幂等 | id→tp_order_id | 下轮重试 |
| 10 | 3229 | 首成交预挂 SL（_place_prepared_orders_immediately） | STOP_MARKET reduceOnly | A | API 非幂等 | id→current_sl_id→save | 计数+critical |
| 11 | 3282 | 首成交兜底 SL（同上） | STOP_MARKET reduceOnly | A | API 非幂等 | id→current_sl_id→save | 计数+critical |
| 12 | 3327 | 首成交预挂 TP（同上） | TP_MARKET reduceOnly | A | API 非幂等 | id→tp_order_id→save | 仅打印 |
| 13 | 3532 | /close 市价平仓 | MARKET reduceOnly | C | **业务结果较强幂等，但 API 本身非幂等** | 不提交 id（仅取 average 算盈亏）→clear | except |
| 14 | 3713 | /limit-close 限价平仓 | LIMIT reduceOnly | C | **业务结果较强幂等，但 API 本身非幂等** | id→limit_close_order_id→save+监控线程 | except |

> **修订说明（裁决 1/7）**：v1 的"C 级幂等"表述已废弃。`reduceOnly` 只是**业务结果**上的强幂等（重复执行最多平掉全部持仓、不超平），**不等于 create_order API 请求幂等**（重复执行仍会产生两个不同订单）。因此 C 级同样适用"禁止盲重"。

**分级依据**：
- **A 级（10 处）**：SL/TP 保护单。挂错 = 保护缺失（裸仓）或超量平仓，直接关系资金安全
- **B 级（1 处）**：开仓条件单。挂错主要影响策略完整性（层数缺失/双倍开仓）
- **C 级（2 处）**：平仓单。业务结果较强幂等 + 已有成交监控（市价立即成交、限价有 `_monitor_limit_close` 线程）

---

## 三、核心发现（写侧真实风险，按严重度排序）

### 发现 1 🔴 最高：create_order 的 `_safe_api_call` 盲重重试 = 重复下单

14 处全部走 `_safe_api_call`（L606），**默认 `retries=5`**。NetworkError 分支（L696-701）：
`sleep(delay*(i+1)) → continue → 再次 func(*args, **kwargs) → 再次 create_order`。

**盲点**：响应丢失（请求到达交易所、响应在途超时）→ ccxt 抛 NetworkError → 重试 → **第二张订单**。
Binance 的 ccxt 默认 clientOrderId 随机生成，**无天然幂等**。

| 场景 | 后果 |
|---|---|
| A 级保护单重复 | 双 SL / 双 TP → 超量平仓（真实资金风险） |
| B 级开仓单重复 | 双倍开仓（风险最大，直接放大仓位） |
| C 级平仓单重复 | 业务结果幂等兜底（不超平），但 API 层仍产生额外订单 |

**结论（裁决 1 采纳）**：

> **任何 `create_order` 都不得通过 `_safe_api_call` 盲重。**

工程不变量：**"create_order 一律禁止盲重"**——14 处统一显式 `retries=1`。理由不是"哪级需要哪级不需要"，而是 **create_order 本身非幂等**，统一规则比记忆"哪些订单可以 retries=5"更不易出错。已确认非 create 的读/撤单调用不受影响（读幂等、撤单幂等，可保留默认重试）。

### 发现 2 🟠 成功路径 Create → Commit 无 Verify

14 处全部 `new_id = order['id']` → 立即写状态。若响应异常/订单未真正成立 → 状态记录幽灵 id → 保护失效。
读侧 SG3-P1（C4）每轮校验兜底（窗口 = 一个监控周期 60-90s）。写侧 Verify 价值：**即时确认**（同步事务边界）+ 双实例下发现孤儿单。

### 发现 3 🟠 双实例：状态 last-writer-wins + 孤儿保护单

- `_state_lock`（L108）仅**进程内** threading.Lock，跨进程不互斥
- 双实例下：A 挂 SL1 写状态 → B 挂 SL2 写状态（覆盖 SL1）→ 交易所双 SL，状态只记 SL2
- SG3-P1 只校验状态记录的 id → **漏检 SL1 孤儿单**（双止损仍在场）

**C5 边界（裁决 6 采纳）**：只做 **Detect + Alert**（Verify 时检查同 symbol 保护单数量 > 预期 → critical + TG），**不自动仲裁删除**。程序创建的 / 用户手工的 / 另一实例的 / 历史遗留的——来源不可区分，自动删任何一张都有风险。Identify + Reconcile + Delete 留给 D-004 / 更高层架构。

### 发现 4 🟡 创建结果未知（无 order_id）→ 重挂双单 —— **本阶段不解决**

即使 create `retries=1`：抛异常 → 无 order_id → 无法 fetch_order 验证 → 状态无 SL；但交易所**可能已有**这张单 → 下轮监控判定"无有效 SL" → need_recover_sl → **又挂一张 → 双 SL**。

**裁决 5 采纳**：这是 **Create outcome reconciliation**（未知订单的归属恢复），已从 C5 移出、**单独立项**（后续 D 系列讨论，推荐方案：b）读侧 SG3-P1 扩展"状态无 SL 但 open_orders 有同参数 SL → 认领而非重挂"，零 API 下轮生效；a）创建失败后 reconcile 扫描为备选）。C5 不试图构建"订单事务协调器"。

**C5 内对 UNKNOWN 的处理**（裁决 4/9）：critical 告警 + 不 Commit + **不进入 need_recover 自动补单**（否则双单问题原地复活），等待后续 reconcile 立项解决。

---

## 四、分级结论 + Verify 状态机（裁决 2/3/4/9 采纳）

| 级 | 处理 | 理由 |
|---|---|---|
| **A（10 处保护单）** | **必须**：Create → Verify（`fetch_order(order_id)`）→ Commit | 保护单错误代价 = 资金安全；写侧即时确认 |
| **B（1 处开仓单）** | `retries=1`；Create → Commit（不 Verify） | 假 id 只导致少开仓、不扩大风险；Verify 每层 +1 API 成本高。**必须保留失败后的状态安全语义**：create 抛异常 → 不 Commit → 状态无此单 → 下轮监控基于周期开头 open_orders 快照（部分天然幂等）自愈 |
| **C（2 处平仓单）** | `retries=1`（同禁盲重）；不 Verify | 业务结果较强幂等 + 已有成交监控（行为级 Verify 已存在） |

### Verify 实现：`fetch_order(order_id)`（裁决 3 采纳，锁死）

**不用 open_orders 快照**。理由：
- `open_orders_map` 是监控循环的**周期快照**；Create→Verify 是**同步事务边界**，两者语义不同
- 快照存在"订单真实存在但本轮还没刷新"的窗口 → 误判 Verify 失败
- 一个快照不能同时承担两个不同的事务语义；open_orders 继续承担周期监控职责（C4 的读侧）

成本：+1 API/创建事件（创建频率 = 成交/恢复/用户操作事件级，日均个位数，可忽略）。

### Verify 三态状态机（裁决 4/9 采纳，锁死）

```
             create_order
                  │
          ┌───────┴────────┐
          │                │
       有 order_id       无 order_id
          │                │
          ▼                ▼
       Verify          VERIFY_UNKNOWN
          │                │
    ┌─────┴─────┐          │
    │           │          │
 SUCCESS     NOT_FOUND     │
    │           │          │
    ▼           ▼          ▼
  Commit     不 Commit   不 Commit
             +告警       +critical 告警
```

**三态定义**（禁止退化为 True/False）：
- **VERIFY_SUCCESS**：`fetch_order(order_id)` 返回订单 → Commit（写 current_sl_id / tp_order_id / entry_orders）
- **VERIFY_NOT_FOUND**：`fetch_order` 抛 `OrderNotFound`（ccxt）→ 不 Commit + 按各点既有失败路径告警
- **VERIFY_UNKNOWN**：`fetch_order` 抛网络类异常（NetworkError 等）→ 不 Commit + critical 告警 + **不触发自动补单**

**关键语义（裁决 4 锁死）**：`fetch_order` 网络异常 **≠** 订单不存在。绝不能把 UNKNOWN 写成"订单存在"或"订单不存在"。这与系统宪法 **UNKNOWN ≠ EMPTY** 是同一个工程原则（已写进安全不变量 1 延伸）。

---

## 五、实施范围提案（ChatGPT 收缩后，最小改动）

### 本阶段做（P0，两件事）

1. **全部 14 处 create_order 显式 `retries=1`**（改调用签名，+0 结构）——防发现 1。理由统一：**create_order 一律禁止盲重**
2. **A 级 10 处接入 Verify 门**（新建 helper `_verify_order_created(order_id, symbol) -> ('success'|'not_found'|'unknown')`：`fetch_order` 三态判断）——防发现 2
   - Verify 通过 → 按原路径 Commit
   - Verify 失败 → 不 Commit，按各点既有失败路径处理（sl_fail_count 熔断 / 计数+critical / return False 等），UNKNOWN 额外 critical 且不自动补单
3. **A 级 Verify 附带孤儿保护单计数检查**（同 symbol open_orders 中 reduceOnly SL/TP 数 > 预期 → critical + TG，**只告警不仲裁**）——防发现 3

### 明确不做（本阶段）

- ❌ 未知结果认领 / Create outcome reconciliation（发现 4，单独立项）
- ❌ 孤儿订单仲裁与删除（D-004）
- ❌ clientOrderId 幂等键（ccxt/binance 支持但需持久化映射，改动面大）
- ❌ 双实例全量协调（C5 只告警不仲裁）
- ❌ 14 处调用点业务逻辑重构（只加 Verify 门，不碰业务）

---

## 六、测试计划（裁决 8 采纳，补充 UNKNOWN/NOT_FOUND 双场景）

离线 Mock 场景清单：

| # | 场景 | 断言 |
|---|---|---|
| 1 | AST：14 处 create_order 全部显式 retries=1 | 无一处使用默认重试 |
| 2 | AST：A 级 10 处接入 Verify 调用点存在 | 插入点齐全 |
| 3 | Verify SUCCESS → Commit | fetch_order 正常返回 → 状态写入 id |
| 4 | 🔴 **Verify 抛 NetworkError → 不 Commit** | 状态无幽灵 id + critical 告警 + **不触发 need_recover** |
| 5 | 🔴 **Verify 抛 OrderNotFound → 不 Commit** | 状态无幽灵 id + 既有失败路径告警（与 UNKNOWN 语义分离） |
| 6 | 孤儿保护单计数超限 → critical 告警 | 只告警、零撤单/零下单（不仲裁） |
| 7 | 恢复链回归（SL/TP 恢复仍正常走通） | 既有 28 场景不破坏 |
| 8 | 全量回归：9 文件 + test_orphan_guard（Bot 停机窗口） | 无破坏 |

> **测试层防止的语义错误**：把"我不知道"（UNKNOWN）错误处理成"不存在"（NOT_FOUND）或"存在"（SUCCESS）。UNKNOWN/NOT_FOUND 均为不 Commit，但告警级别与后续动作不同。

---

## 七、源码锚点（af64765 当前行号）

- `_safe_api_call`：L606-723（retries 默认 5；NetworkError L696-701 盲重）
- `_state_lock`：L108（进程内锁）
- `save_batch_state`：L761-767（get-modify-put，跨进程 last-writer-wins）
- 监控循环 open_orders 快照：L2055-2058（C4 读侧，保持原职责）
- SG3-P1 SL 插入点：L2600-2620；TP 插入点：L2719-2739（C4 已实施）
- 恢复链合并：L2823-2836（need_update_sl/tp → 恢复）
- create_order 14 处：L1071 / 1182 / 1356 / 1787 / 2418 / 2456 / 2903 / 2966 / 3043 / 3229 / 3282 / 3327 / 3532 / 3713

---

## 附录 A：ChatGPT 评审闭环（10 条逐项记录）

| # | 裁决项 | 裁决 | 采纳动作 |
|---|---|---|---|
| 1 | `create_order` 默认 retries=5 | 🔴 必须处理 | 14 处统一 `retries=1`；不变量表述改为"**create_order 一律禁止盲重**"（见 §三 发现1） |
| 2 | A 级 Create→Verify→Commit | ✅ 同意 | 正文 §四 保留；严格流程 Create→拿 id→Verify→成功才 Commit |
| 3 | Verify 用 fetch_order | ✅ 优先于 open_orders 快照 | 锁死 §四：fetch_order(order_id)，快照继续只做周期监控 |
| 4 | Verify 失败立即 Commit | ❌ 绝对禁止 | 锁死 §四：三态 VERIFY_SUCCESS/NOT_FOUND/UNKNOWN，UNKNOWN ≠ NOT_FOUND ≠ SUCCESS |
| 5 | 创建失败后"认领"（a/b） | ❌ 暂时不实现 | 移出 C5，发现 4 单独立项（§三 发现4）；C5 内 UNKNOWN = critical + 不补单 |
| 6 | 孤儿保护单 | ✅ C5 纳入检测+告警，不仲裁 | §三 发现3 + §五 第3条：Detect + Alert，Identify/Reconcile/Delete 留 D-004 |
| 7 | C 级"幂等"表述 | ⚠️ 需要修改 | §二 全表 C 级改"业务结果较强幂等，但 API 本身非幂等"；C 级同样 retries=1 |
| 8 | 测试补 NetworkError/OrderNotFound 双场景 | 🔴 必须 | §六 场景 4/5：UNKNOWN 与 NOT_FOUND 分离断言 |
| 9 | C5 状态机 | 建议锁死 | §四 三态状态机图，已落档 |
| 10 | 14 处一次性大改 | ⚠️ 不建议 | 通过范围收缩解决：只做 retries=1（签名级）+ A 级 Verify 门，不动业务逻辑 |

**修订版待 ChatGPT 确认**：如无异议，本 v2 即为 C5 实施规格，进入 TDD 红阶段。
