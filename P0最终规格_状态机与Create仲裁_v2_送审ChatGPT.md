# P0 最终规格：保护单状态机 + Create 仲裁 + 不变量（v2，送审 ChatGPT）

- **日期**：2026-08-20 01:09（北京时间）
- **状态**：最终规格候选稿。**本规格审核通过前不启动 B1 编码**（ChatGPT 十八节终审要求）
- **前置**：v1 草案（2026-08-20 01:00）+ ChatGPT 十八节终审裁决
- **性质**：只读分析产出，未改任何代码。本版为 v1 的结构化升级：状态机 + 不变量 + 转移表 + 崩溃场景 + 全调用点映射

---

## 0. 终审裁决吸收确认（逐条）

| # | ChatGPT 终审裁决 | 本规格对应 | 与 v1 的差异 |
|---|---|---|---|
| 1 | 三大设计保留，不回退（仲裁闸门 / 状态机 / Fail-Closed） | §2 §3 §4 | 无变化，定稿 |
| 2 | **FAILED 只能代表"Create 明确没有产生副作用"**，网络异常 ≠ FAILED | §1.2 §3.2 | **v1 缺陷修正**：v1 把一切 create 异常归 FAILED |
| 3 | Create 三结果模型：CONFIRMED / FAILED / UNKNOWN→PENDING_VERIFY | §3 | v1 只有异常/返回 ID 两分支 |
| 4 | P0-1 Create 仲裁排在 stop=True 之前 | §9 优先级表 | 优先级重排 |
| 5 | 幂等键必须含 batch_id（旧/新批次 L2 SL 不能互认同一订单） | §5.1 | v1 键缺 batch_id 显式段 |
| 6 | registry 必须持久化（崩溃安全 Create：Create 前落盘意图） | §5.2 §6 | v1 只落盘 PENDING_VERIFY，**缺 PENDING_CREATE 前置落盘** |
| 7 | "Create 返回 ID"与"本地记录 ID"之间存在原子窗口，必须 crash injection 专项测试 | §7 Case A-F | v1 无崩溃测试矩阵 |
| 8 | fetch_open_orders 失明独立为 P1-A，不与 C5 混修 | §8 | 无变化，定稿 |
| 9 | 双通道必须是**统一视图 OrderSnapshot**（上层不知道有两个 endpoint），不是两个列表 | §8.1 | v1 返回 (orders_list, valid_flag)，**接口形态升级** |
| 10 | Fail-Closed 保留：INVALID ≠ EMPTY，严禁"查询失败→[]→认为没订单" | §8.2 | 无变化，定稿 |
| 11 | 5/5 硬锁必须是逻辑锁（hard_locked 持久化，重启不清零） | §5.4 | 无变化，定稿 |
| 12 | 解锁必须带审计信息（unlock_reason / unlock_time / unlock_operator） | §5.5 | v1 的 U1 缺审计字段 |
| 13 | **开仓条件单纳入仲裁**（重复开仓 = 直接增加仓位，比保护单重复更危险） | §5.6 | v1 的 Q2 已裁决：纳入，统一原则"任何副作用 Create 都过唯一入口" |
| 14 | 418 共享 IP 维持"高概率假设待证"，不扩大 P0 | §10 | 无变化 |
| 15 | 418 倒计时改 60 秒级，放 B3/B4，不抢 P0 主线 | §10.2 | 无变化 |
| 16 | 优先级重排为 P0-1~P0-6 | §9 | 采纳 |
| 17 | 8 条交易系统宪法 | §2 | 新增章节，与既有系统宪法合并方案 |
| 18 | 先出"状态机+不变量+转移表+崩溃场景"最终规格，审核通过再编码 | 本文档 | 本文档即该规格 |

**v1 五个开放问题的裁决吸收**：
- Q1（ABSENT 自动判定）：终审状态图中无 ABSENT 自动路径 → **维持仅人工**，见 §3.1
- Q2（开仓单纳入仲裁）：**已裁决纳入**（§5.6）
- Q3（解锁通道）：U1（手改 state 文件）+ **强制审计字段**（§5.5），U2 不做
- Q4（测试语义变更程序性确认）：未被否，维持"实施前单独列清单供程序性确认"
- Q5（孤儿告警纳入 B3）：终审未直接裁决，**维持待裁决**（§11）

---

## 1. 源码实证基础（本轮新增核查，2026-08-20 01:05）

| 事实 | 锚点 | 用途 |
|---|---|---|
| ccxt 异常层次：`ExchangeError`（交易所明确拒绝）与 `NetworkError`（结果未知）是**两个独立分支** | `.venv/ccxt/base/errors.py` L70 / L182 | FAILED 语义可实现：`isinstance(e, NetworkError)` 可区分"确定失败"与"结果未知" |
| `RequestTimeout` / `DDoSProtection` / `RateLimitExceeded` / `ExchangeNotAvailable` 均为 `NetworkError` 子类 | errors.py L186-212 | 418/超时/限流引发的 create 异常 → 一律 PENDING_VERIFY，**绝不 FAILED** |
| `OrderNotFound` 是 `InvalidOrder` → `ExchangeError` 子类 | errors.py L142-144 | verify 侧 not_found 与网络 unknown 天然可区分 |
| L1811 开仓循环内 batch_id / idx（层）/ side 变量齐备 | Read L1775-1839 | ENTRY 幂等键可用 |
| L1811 开仓单 create 后仅 append 到内存 `entry_orders` 列表，**循环结束后才落盘 batch_state** | Read L1819-1822 | **新发现崩溃窗口**：开仓循环中途崩溃 → 交易所已有单、本地零记录（Case F，§7） |
| L3342 预生成 SL 处 batch_id / idx / pending_sl_orders 齐备 | Read L3323-3368 | SL 幂等键可用 |
| 14 处 create_order：1071 / 1190 / 1372 / 1811 / 2475 / 2522 / 2978 / 3055 / 3146 / 3342 / 3405 / 3461 / 3677 / 3860 | Grep 实测 | §5.7 映射表 |
| 11 处 verify 调用点：1080 / 1199 / 1381 / 2484 / 2531 / 2987 / 3064 / 3155 / 3351 / 3414 / 3470（+定义 L1994） | Grep 实测 | §5.8 映射表。**注：终审要求"14 个 Verify 点映射"，实际现存 verify 调用点为 11 处**——14 处 create 中 1811（开仓）/ 3677 / 3860（平仓）三处无 verify 调用，差异在此说明，非行号漂移 |

---

## 2. 交易系统宪法（不变量层）

ChatGPT 十七节 8 条为**本规格的不变量层**，凌驾于一切实现细节。与既有《安全不变量_系统宪法.md》8 条的合并关系如下（新 8 条编号续接为第 9-16 条，等价关系标注）：

| # | 新不变量 | 与既有宪法关系 |
|---|---|---|
| 9 | 任何 Create 都必须经过唯一仲裁入口 | 新增（细化第 6 条 Create≠Success 的执行侧） |
| 10 | Create 结果未知时，禁止再次 Create | 新增（第 1 条 UNKNOWN≠EMPTY 在写侧的镜像） |
| 11 | 查询不到订单，不等于订单不存在 | **= 既有第 1 条**（等价，合并标注） |
| 12 | UNKNOWN / INVALID 状态只能自愈确认，不能自愈执行副作用 | 新增（细化既有第 8 条 Fail-Closed） |
| 13 | 交易所查询失败不得降级为 EMPTY | 新增（读侧 Fail-Closed，与第 8 条互补） |
| 14 | 熔断必须真正阻断副作用，而不是只打印"失败" | 新增（本次 5/5 软计数事故的教训固化） |
| 15 | 熔断、PENDING、HARD_LOCK 必须持久化，重启不能清零安全状态 | 新增（恢复不扩大风险的扩展，关联第 3 条） |
| 16 | 一个逻辑订单只能对应一个可被系统认可的 Create 生命周期 | 新增（幂等键的理论基础） |

**执行纪律**：B1-B2 任何代码若与上述 16 条冲突，以宪法为准；测试用例必须逐条可追溯到不变量编号（测试 docstring 标注 `不变量#10` 式引用）。

---

## 3. 状态机定义

### 3.1 状态集（6 持久化态 + 1 锁标志）

```
                 ┌──────────────┐
                 │    CREATE    │  （仲裁闸门批准后发起）
                 └──────┬───────┘
                        │
             ┌──────────┼─────────────┐
             ↓          ↓             ↓
        返回订单ID   ExchangeError   NetworkError/其他异常
             │      (确定拒绝)      (结果未知)
             ↓          ↓             ↓
      PENDING_VERIFY  FAILED    PENDING_VERIFY
        (id_known)  (无副作用)   (id_unknown)
             │          │             │
        ┌────┴────┐     │        ┌────┴────┐
        ↓         ↓     │        ↓         ↓
     FOUND   NOT_FOUND  │     FOUND*  NOT_FOUND*
        ↓         ↓     │        ↓         ↓
   CONFIRMED NOT_CONFIRMED    CONFIRMED  NOT_CONFIRMED
                        │
        FAILED 重试（fail_count++）→ ≥5 → HARD_LOCK
        NOT_CONFIRMED：永不自动 Create，只允许重查自愈/人工
        ABSENT：仅人工证据链可达（本期无自动路径）
```

*无 ID 的 PENDING_VERIFY 通过**身份签名匹配**自愈（§6.3）：在双通道 OrderSnapshot 中按 symbol+type+side+amount+stopPrice+positionSide 匹配。匹配到 → 收编 CONFIRMED；快照 VALID 且未匹配 → 仍 NOT_CONFIRMED（订单可能已触发终结，缺席≠从未存在，不变量#11）。

### 3.2 状态语义（含 FAILED 语义修正——终审第二节）

| 状态 | 进入条件 | 持久化 | 能否 Create | 能否 Verify | 能否补挂 |
|---|---|---|---|---|---|
| `PENDING_CREATE` | 仲裁批准后、create_order 发出**前**（意图落盘） | ✅ 强制 | —（正在执行） | — | ❌ |
| `PENDING_VERIFY` | ① create 返回 ID（id_known=true）② NetworkError/其他异常（id_known=false）③ 重启时发现 PENDING_CREATE 悬置 | ✅ 强制 | ❌ | ✅ | ❌ |
| `FAILED` | **仅** `ccxt.ExchangeError`（含 InvalidOrder/InsufficientFunds/OperationRejected 等，交易所收到请求并明确拒绝）——确定无副作用 | ✅ | ✅（fail_count++ 带计数） | — | ✅（同 Create 通道） |
| `CONFIRMED` | verify FOUND / 身份匹配 FOUND / 人工补录 | ✅ | ❌（该单已在） | 可选（终态巡检） | ❌ |
| `NOT_CONFIRMED` | verify NOT_FOUND **或** verify UNKNOWN（网络/查询失败）**或** 身份匹配缺席（快照 VALID） | ✅ | **❌ 永不自动**（不变量#10 #12） | ✅（重查自愈唯一通道） | ❌ |
| `ABSENT` | 仅人工：用户到交易所核实"从未存在/已终结" | ✅ | ✅（人工解锁后新生命周期） | — | ✅（新生命周期） |
| `HARD_LOCK` | fail_count ≥ 5（同 identity 连续确定失败）| ✅（含解锁审计字段） | **❌** | ✅（可） | **❌** |

**FAILED 判定硬规则**（写进设计文档第一条，B1 编码前置）：

```
create_order 抛异常时：
  isinstance(e, ccxt.NetworkError)          → PENDING_VERIFY (id_unknown)   # 超时/断连/429/418/维护
  isinstance(e, ccxt.ExchangeError)         → FAILED                        # 交易所明确拒绝，确定无副作用
  其他一切异常（本地解析错误、KeyError……）   → PENDING_VERIFY (id_unknown)   # 保守：请求可能已发出
```

**理由**：网络异常 ≠ Create 失败。请求可能已到达交易所并成交挂单，只是响应丢失。唯一确定无副作用的证据是"交易所收到了请求并拒绝了它"（ExchangeError）。宁可多一次人工介入，不可多一个孤儿单。

**仲裁器内 create 强制 `retries=1`**（禁止 `_safe_api_call` 自动重发）：网络异常下自动重发 = 盲重 = 直接违反不变量#10。

---

## 4. 状态转移表（终审十八节要求的完整版）

| 当前状态 | 事件 | 下一状态 | 允许 Create | 允许 Verify | 允许补挂 |
|---|---|---|:---:|:---:|:---:|
| （无记录） | 业务请求保护单 | PENDING_CREATE（先落盘） | ✅ | — | — |
| PENDING_CREATE | create 返回 ID | PENDING_VERIFY (id_known) | ❌ | ✅ | ❌ |
| PENDING_CREATE | ExchangeError | FAILED（fail_count++） | ✅ | — | ✅ |
| PENDING_CREATE | NetworkError/其他异常 | PENDING_VERIFY (id_unknown) | ❌ | ✅（身份匹配） | ❌ |
| PENDING_CREATE | 崩溃重启发现悬置 | PENDING_VERIFY (id_unknown) | **❌** | ✅（身份匹配） | **❌** |
| PENDING_VERIFY | verify FOUND | CONFIRMED | ❌ | — | ❌ |
| PENDING_VERIFY | verify NOT_FOUND | NOT_CONFIRMED | **❌** | ✅ | **❌** |
| PENDING_VERIFY | verify UNKNOWN | NOT_CONFIRMED | **❌** | ✅ | **❌** |
| PENDING_VERIFY | 身份匹配 FOUND（无 ID 自愈） | CONFIRMED（收编 ID） | ❌ | — | ❌ |
| PENDING_VERIFY | 崩溃重启发现 | PENDING_VERIFY（原样） | **❌** | ✅ | **❌** |
| NOT_CONFIRMED | 重查 FOUND | CONFIRMED（**只补 Commit，不新建**） | ❌ | — | ❌ |
| NOT_CONFIRMED | 重查 NOT_FOUND / UNKNOWN | NOT_CONFIRMED（静默维持） | **❌** | ✅ | **❌** |
| NOT_CONFIRMED | 人工核实"从未存在" | ABSENT（记审计字段） | ✅（新生命周期） | — | ✅ |
| NOT_CONFIRMED | 人工提供订单号 | CONFIRMED（补录） | ❌ | 可选 | ❌ |
| FAILED | 重试 Create | PENDING_CREATE | ✅（计数内） | — | — |
| FAILED | fail_count ≥ 5 | HARD_LOCK | **❌** | ✅ | **❌** |
| FAILED | 重试成功（CONFIRMED） | CONFIRMED（fail_count 清零） | ❌ | — | ❌ |
| CONFIRMED | 订单触发/终结（业务识别） | CONFIRMED→随批次清理 | ❌ | 可选 | ❌ |
| CONFIRMED | 用户改价换挂 | 先撤旧单（幂等）→ 新 identity 生命周期 | ✅（新 identity） | — | — |
| HARD_LOCK | 任意事件 | HARD_LOCK | **❌** | ✅ | **❌** |
| HARD_LOCK | 人工解锁（含审计三字段，§5.5） | ABSENT 或按核实结果 | ✅（新生命周期） | — | ✅ |

**全局旁路规则**（优先于一切行）：
1. API 全局 cooldown / 418 期间（§10）：Create 与 Verify 一律拒绝/暂停 → PENDING_VERIFY 堆积为合法稳态
2. OrderSnapshot INVALID（§8）期间：一切依赖"订单不在快照"的推断（补挂判断/终态判定/缺失告警）跳过本轮
3. 任何自动路径**禁止**把 NOT_CONFIRMED 翻转为 ABSENT（不变量#12：UNKNOWN 只能自愈确认，不能自愈执行副作用）

---

## 5. Create 仲裁器（唯一闸门）规格

### 5.1 幂等键（终审第五节：必须含 batch_id）

```
identity = f"{batch_id}|{order_role}|L{layer}|{position_side}"
# order_role ∈ {SL, TP, ENTRY}
# 示例：batch_20260819_081653_0cd379|SL|L2|LONG
#        batch_20260819_081653_0cd379|ENTRY|L1|LONG
```

- batch_id 显式入键：旧批次 L2 SL 与新批次 L2 SL 绝不互认（终审明确要求）
- 键在日志/TG 告警/解锁操作中**全量打印**（可审计、可定位）
- 同 identity 换挂新价 = 撤旧（幂等）→ 旧记录终态化 → 新记录新生命周期（转移表 CONFIRMED 行）

### 5.2 registry 持久化结构与崩溃安全 Create 时序

**存放位置**：`batch_state['protection_registry']`（复用既有 `save_batch_state` 原子写 + `_state_lock`，不新增状态文件、不新增锁协调面）。键 = §5.1 identity。

**崩溃安全六步时序**（每步落盘点明确）：

```
T0  仲裁检查（无未终结 Create / 非 HARD_LOCK / fail_count<5 / 非 cooldown）
     → registry 落盘 {state: PENDING_CREATE, params_snapshot{...}}     ← 意图先落盘
T1  create_order(retries=1)
T2a ExchangeError      → registry 落盘 {state: FAILED, fail_count++}
T2b NetworkError/其他  → registry 落盘 {state: PENDING_VERIFY, id_known: false}
T2c 返回订单 ID        → registry 立即落盘 {state: PENDING_VERIFY, order_id, id_known: true}
                                                                  ↑ Case C 原子窗口
T3  verify(order_kind 路由)
T4  FOUND → CONFIRMED 落盘 + 返回调用方 Commit 业务字段
    NOT_FOUND / UNKNOWN → NOT_CONFIRMED 落盘 + 一次性 critical 告警 + 返回 blocked
```

**registry 记录字段**（持久化字段表，终审十八节交付物③）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `state` | str | 6 态之一 |
| `order_id` | str/None | 交易所 ID（id_known=false 时为 None） |
| `id_known` | bool | 区分有/无 ID 的 PENDING_VERIFY |
| `order_kind` | str | 'conditional' / 'normal'（Verify 路由依据） |
| `params_snapshot` | dict | symbol/type/side/amount/stopPrice/positionSide（身份签名匹配 + 审计） |
| `created_at` / `last_verify_at` | int | 时间戳 |
| `fail_count` | int | 仅 FAILED（确定失败）计数；CONFIRMED 时清零 |
| `hard_locked` | bool | 逻辑锁（§5.4） |
| `unlock_reason` / `unlock_time` / `unlock_operator` | str | 解锁审计三字段（§5.5） |

### 5.3 仲裁器唯一入口

```python
def _create_protection_order(self, symbol, batch_id, layer, order_role, position_side,
                             order_type, side, amount, params, order_kind):
    """唯一 Create 闸门。B2 完成后，全代码库 SL/TP/ENTRY 的交易所 Create
    只允许出现在这一个函数体内（终审第四节架构要求）。
    返回 ('confirmed', order_id) / ('failed', reason) / ('blocked', reason)"""
```

仲裁顺序：HARD_LOCK → 未终结 Create 存在（PENDING_CREATE/PENDING_VERIFY/NOT_CONFIRMED）→ fail_count≥5（置锁）→ 全局 cooldown → 执行六步时序。

### 5.4 HARD_LOCK（真熔断，终审十一节）

- `fail_count ≥ 5` → `hard_locked=True` **落盘**，跨轮/跨重启有效（不变量#15：重启不清零）
- 锁定后该 identity 一切 Create/补挂/重试拒绝；进入时 1 次 critical（🚨【资金安全】前缀），此后静默
- 范围 = 单 identity（batch+role+layer+side）。是否升级为全局锁（任一 identity 硬锁 → 全批停）**待裁决（§11 Q7）**
- v1 已实证的软计数三缺陷（L3031 按层计数 / L3046 降级路径绕闸门 / L3125 sl_failed_layers 只写不读）随收编自然消灭，死字段删除

### 5.5 解锁通道（U1 + 强制审计，终审十二节）

- 解锁 = 用户手改 `trade_state.json` 对应 registry 条目，**但必须同时写入审计三字段**：`unlock_reason`（为什么解锁）、`unlock_time`（何时）、`unlock_operator`（谁，约定填人工标识）
- **启动校验**：bot 启动时发现 `hard_locked=false` 但无审计三字段的记录 → 视为非法解锁，**回滚为 hard_locked=true 并 critical 告警**（防"不知道为什么直接改 false"回到不可审计状态）
- 解锁后目标态由用户核实结果决定：交易所确无此单 → `ABSENT`；有单 → 提供订单号补录 `CONFIRMED`
- 告警消息中写明操作步骤与字段路径（v1 U1 已有，保留）

### 5.6 开仓条件单纳入（Q2 已裁决：纳入）

- L1811 开仓 STOP_MARKET 同走 algo 端点、副作用 = **直接增加仓位**，风险高于保护单重复 → 纳入同一闸门，`order_role='ENTRY'`
- **新发现的实施前提**（§1 事实表）：现码开仓循环 create 后仅存内存 `entry_orders`，循环结束才落盘 batch_state → 开仓中途崩溃 = 交易所有单、本地零记录（Case F）
- **修正规则**：信号接受后、进开仓循环**前**，先把批次骨架 + 全部 ENTRY 的 PENDING_CREATE 记录落盘（每层一条 identity），循环内逐单 T2c 更新。此规则同时封堵"开仓循环部分完成后崩溃"的既有盲区（该盲区独立于 C5 存在，本轮源码核查新发现）

### 5.7 14 处 Create 调用点 → 仲裁器映射（终审十八节交付物①）

| # | 行号 | 路径 / 用途 | order_kind | identity（order_role/L/side） | 收编批次 |
|---|---|---|---|---|---|
| 1 | 1071 | 用户修改 TP 换挂（撤旧→挂新） | conditional | TP / 层号按批次当前 TP 层 / 由 side 推导 | B2 |
| 2 | 1190 | 用户修改 SL 换挂（撤旧→挂新） | conditional | SL / 同上 | B2 |
| 3 | 1372 | 保本损（BE 移动 SL） | conditional | SL / 批次主 SL 层 | B2 |
| 4 | 1811 | **开仓条件单（多层循环）** | conditional | **ENTRY / L{idx} / side** | B2（含 §5.6 前置落盘规则） |
| 5 | 2475 | 减仓后挂新 SL | conditional | SL / 批次主层 | B2 |
| 6 | 2522 | 减仓后挂新 TP | conditional | TP / 批次主层 | B2 |
| 7 | 2978 | 同步维护补挂 SL | conditional | SL / 对应层 | B2 |
| 8 | 3055 | 降级恢复路径挂 SL | conditional | SL / 对应层 | B2（v1 实证 L3046 绕闸门路径，收编后消灭） |
| 9 | 3146 | TP 更新补挂 | conditional | TP / 对应层 | B2 |
| 10 | 3342 | 预生成 SL（成交后 1 秒内） | conditional | SL / L{idx}（pending_sl_orders 语义） | B2 |
| 11 | 3405 | 兜底 SL | conditional | SL / 对应层 | B2 |
| 12 | 3461 | 预生成 TP | conditional | TP / 对应层 | B2 |
| 13 | 3677 | 市价平仓 | normal | —（不纳入，见下） | 不收编 |
| 14 | 3860 | 限价平仓 | normal | —（不纳入，见下） | 不收编 |

**平仓单（3677/3860）不纳入本期的理由**（待终审确认，§11 Q6）：① `reduceOnly` 物理兜底——重复执行第二次因无仓位被交易所拒绝，不会反向开仓（源码注释 L3674/L3851 已阐明）；② normal 端点无 C5 假阴性问题（create 与 fetch 同端点）。风险量级与收益不匹配，P1 再议。**若终审认为宪法#9"任何 Create 都过唯一入口"必须字面全覆盖，则平仓单以最小形态纳入（仅仲裁检查，无 Verify）**——请裁决。

**B2 完成的验收标准**：`grep create_order trader_260725.py` 仅剩 `_create_protection_order` 函数体内 1 处 + 平仓 2 处（若 Q6 裁决不纳入）。

### 5.8 Verify 调用点 → order_kind 映射（终审十八节交付物②）

`_verify_order_created(order_id, symbol, order_kind)`：conditional → `fetch_order(..., params={'stop': True})`（algo 通道）；normal → 不带 params。

实际 verify 调用点 **11 处**（§1 已说明与"14"的数字差异）：

| 行号 | 验证对象 | kind | 前人 stop=True 惯例佐证 |
|---|---|---|---|
| 1080 | 用户改 TP 新单 | conditional | L1054 撤旧单已带 stop=True ✓ |
| 1199 | 用户改 SL 新单 | conditional | 同路径撤旧带 ✓ |
| 1381 | 保本损新 SL | conditional | — |
| 2484 | 减仓后新 SL | conditional | — |
| 2531 | 减仓后新 TP | conditional | — |
| 2987 | 补挂 SL | conditional | L2940 旧单查询带 ✓ |
| 3064 | 降级恢复 SL | conditional | — |
| 3155 | TP 更新补挂 | conditional | — |
| 3351 | 预生成 SL | conditional | — |
| 3414 | 兜底 SL | conditional | — |
| 3470 | 预生成 TP | conditional | — |

B2 收编后，这 11 处散装 verify 随 create 一起消失（verify 成为仲裁器 T3 步骤内部实现）；B1 阶段（语义先行）则先行给 `_verify_order_created` 加 kind 参数并修正 11 处调用。

---

## 6. 重启恢复规则（终审十八节交付物④）

启动时遍历所有 batch_state 的 protection_registry：

| 重启时发现的记录状态 | 恢复动作 | 不变量依据 |
|---|---|---|
| PENDING_CREATE | 视同 PENDING_VERIFY(id_unknown)：禁 Create，进身份匹配自愈 | #10 #16（请求可能已发出） |
| PENDING_VERIFY (id_known) | 禁 Create；首轮 verify(kind 路由) 重查 | #10 |
| PENDING_VERIFY (id_unknown) | 禁 Create；身份签名匹配（§6.3） | #10 #11 |
| NOT_CONFIRMED | 禁 Create；进重查自愈队列 | #10 #12 |
| FAILED (fail_count<5) | 允许经闸门重试（计数延续，**不清零**） | #15 |
| FAILED (fail_count≥5，未置锁的旧数据) | 补置 hard_locked + critical | #14 |
| HARD_LOCK (无审计三字段却 false) | 回滚 true + critical（§5.5） | #15 |
| HARD_LOCK (true) | 维持锁定，等待人工 | #14 #15 |
| CONFIRMED | 正常巡检；与双通道快照对账 | — |
| ABSENT | 允许新生命周期（已人工核实过） | — |

**恢复总原则**：恢复路径只做两类事——①确认（verify/匹配/对账）②维持安全状态。**恢复路径永不 Create**（既有宪法第 3 条"恢复不扩大风险"在 registry 上的投影）。

### 6.3 身份签名匹配（无 ID 自愈）规格

```
签名 = (symbol, type, side, amount, stopPrice, positionSide)  # 取自 params_snapshot
在 OrderSnapshot.orders_by_id 中查找签名全等（价格/数量按精度归一）的条件单：
  命中且唯一   → 收编 CONFIRMED（记录其真实 order_id）
  命中多条     → NOT_CONFIRMED + critical（人工裁决，禁止自动收编多条）
  未命中（快照 VALID）→ NOT_CONFIRMED（缺席≠从未存在：单可能已触发终结）
  快照 INVALID → 维持 PENDING_VERIFY，下轮再试
```

**可选增强（待实测，§11 Q8）**：create 时携带 `newClientOrderId`（程序自造客户端 ID）→ 网络异常后可精确查询。**Binance USDM algo 端点是否接受/回显 clientOrderId 未实测**，不作为基准方案依赖。

---

## 7. 崩溃场景测试矩阵（终审十八节交付物⑤，crash injection）

TDD 红灯阶段逐 Case 制造（mock 注入：在六步时序指定步骤抛 `SystemExit`/断点）：

| Case | 崩溃点 | 交易所侧 | 本地侧 | 期望恢复行为 | 验证不变量 |
|---|---|---|---|---|---|
| A | T0 落盘前 | 无单 | 无记录 | 重启后无记录 → 可正常 Create（干净态） | — |
| B | T1 请求发出后（T0 已落盘） | **未知**（可能已成交挂单） | PENDING_CREATE | 恢复为 PENDING_VERIFY(id_unknown) → 身份匹配；**禁止直接重挂** | #10 #11 |
| C | T2c 返回 ID 后、落盘前 | **已有真实单** | 无 ID 记录（仅 PENDING_CREATE） | 身份匹配收编 CONFIRMED；**这是 24 孤儿单事故的通用防线**（原事故为 C 的 verify 假阴性变体） | #10 #16 |
| D | T2c 落盘后、T3 verify 前 | 已有单 | PENDING_VERIFY(id_known) | 重启 verify 自愈 → CONFIRMED | #10 |
| E | T3 verify 查询进行中 | 已有单 | PENDING_VERIFY | 重启重查；verify 网络异常 → NOT_CONFIRMED（不 FAILED） | #12 |
| F | 开仓循环第 k 层 create 后（§5.6 新发现盲区） | 前 k 层单已存在 | （旧码）零记录 →（新码）前 k 条 PENDING_CREATE/VERIFY | 恢复时逐层身份匹配收编，**禁止补挂任何层**，缺失层人工裁决 | #10 #16 |

**每 Case 断言三件套**：① 恢复后同一 identity 未发生第二次真实 Create（mock 计数=1）② 恢复路径零副作用 ③ 状态与告警符合转移表。

---

## 8. 双通道统一订单视图（P1-A，独立于 C5 修复）

### 8.1 OrderSnapshot 统一接口（终审第九节：统一视图，非两个列表）

```python
class OrderSnapshot:
    orders_by_id: dict        # 双通道合并，{id: order_dict(+view_source 标记)}
    normal_valid: bool        # 普通通道查询成功
    conditional_valid: bool   # 条件单通道查询成功
    @property view_valid: bool  # normal_valid AND conditional_valid
    fetched_at: int
```

- **上层永远只见 `orders_by_id` + `view_valid`**，不知道 Binance 有两个 endpoint；C4/SG3/SG2/TP 检测/孤儿检测全部消费同一视图——终结"每个地方看的世界不一样"
- 业务代码**禁止**直接调用 `fetch_open_orders` / `fetch_open_orders(params={'stop':True})`（B3 收编，验收 grep 零散调用点为零）

### 8.2 Fail-Closed 语义

```
view_valid=True  + 快照为空     → VALID+EMPTY："确认没有订单"（可做缺失判断）
view_valid=False（任一通道失败） → INVALID："无法确认"（禁止一切缺失判断/补挂/终态推断，跳过本轮 + 状态转换式告警）
```

严禁"查询失败 → [] → 认为没有订单"（不变量#13）。INVALID 期间现有 SG2 加仓闸门按 UNKNOWN 拒绝路径处理（保守不放大）。

### 8.3 消费点替换（B3）

| 现调用点 | 用途 | 替换 |
|---|---|---|
| L2114 | 监控主循环 open_orders_map | orders_by_id |
| L1539 | SG2 加仓闸门"有效 SL"校验（现状对条件单恒 False 的 bug） | orders_by_id |
| L1570 | 孤儿/终态检测 known_order_ids | orders_by_id |
| L2676 / L2795 | C4/SG3 死代码分支（条件单永不在普通快照，自 C4 上线从未执行） | 首次真正运行，行为变化重点验收；其撤销重挂路径必须走 §5.3 闸门 |
| L2640-2646 | SL 触发检测逐单 fetch_order(stop=True) 兜底 | 保留（双保险）；双通道后语义从"每轮必进"变"终态检测"，测试覆盖 |

---

## 9. P0 优先级表（终审十六节，照单采纳）

| 优先级 | 项目 | 必须性 | 本规格章节 |
|---|---|---|---|
| **P0-1** | Create 仲裁闸门 | **最高** | §5 |
| **P0-2** | 四态状态机 / UNKNOWN 语义 | **最高** | §3 §4 |
| **P0-3** | conditional Verify 正确路由 stop=True | **最高** | §5.8 |
| **P0-4** | hard lock 真熔断 | **最高** | §5.4 |
| **P0-5** | 所有 SL/TP/ENTRY Create 收口唯一入口 | **最高** | §5.3 §5.7 |
| **P0-6** | 崩溃恢复 / registry 持久化 | **最高** | §5.2 §6 §7 |
| P1-A | normal+conditional 双通道统一视图 | 必须 | §8 |
| P1-B | 418 cooldown 持久化 | 必须 | §10 |
| P1-C | 通知节流 / 邮件风暴 | 必须 | §10.2 |
| P1-D | watchdog 孤儿进程治理 | 必须 | （已部分实施，P0 进程防护已 push） |
| P2 | 日志美化 / Markdown | 后置 | — |

**P0-1 排在 P0-3 之前的原理**（终审原文）：`stop=True` 只修 C5 的已知假阴性；Create 仲裁封堵**一切**未知结果（timeout/network/API unavailable）导致的重复副作用。Verify 路由再正确，也可能因网络抖动产生"不知道成功没成功 → 再挂一次"。

---

## 10. 418 / cooldown 与 Create 仲裁的关系（终审十八节交付物⑥）

### 10.1 关系定义

1. **仲裁器前置检查全局 cooldown**：`_api_cooldown_until` 未到期 → 仲裁器直接拒绝 Create（blocked），**不发请求**（避免主动撞 418 + 避免无意义重试）
2. **cooldown 期间 Verify 同样暂停**：PENDING_VERIFY / NOT_CONFIRMED 记录维持原状，进入"堆积合法稳态"——这是设计内行为，不是异常
3. **cooldown 解除 ≠ 清除安全状态**（不变量#15）：解除后自愈队列照常运转，但 HARD_LOCK / NOT_CONFIRMED / fail_count 一概不受 cooldown 生命周期影响
4. **cooldown 中的 NetworkError create**：按 §3.2 归 PENDING_VERIFY（418/429 属 NetworkError 子类，见 §1 事实表），**绝不 FAILED、绝不重试**
5. **cooldown 落盘（P1-B）**：`api_cooldown.json` 原子写；启动读取，仍在封禁期 → 直接等待，禁止"重启→立即请求→再撞 418"

### 10.2 通知节流（P1-C，B4，不抢 P0 主线）

- 418 倒计时：进入时 1 条（含 until 时刻）→ 期间每 60 秒 1 条 heartbeat → 解除时 1 条。2 小时 ~120 条（现 ~1440 条）
- critical 告警：状态转换式（键 = batch+layer+error_class，10 分钟窗口去重；首次失败/触发硬锁/恢复允许立即发）
- TG Markdown 修复与 stdout 降噪顺延（P2）

### 10.3 边界（终审十四节）

- 418 = 手机 App 与 Bot 共享公网 IP：维持"高概率假设待证"，**不进入任何代码设计的前提**
- 本规格不扩大 P0 范围：cooldown 相关仅做到 §10.1 的仲裁联动（必需，否则仲裁器会在封禁期发请求），完整节流归 B4

---

## 11. 实施批次（规格通过后启动）与遗留待裁决

### 11.1 实施批次（TDD：红→改→绿→全量回归→确认→commit）

| 批次 | 内容（优先级项） | 测试 |
|---|---|---|
| **B1** | P0-2：状态机语义（FAILED 精确分类/NOT_CONFIRMED 禁副作用/重查自愈只补 Commit）+ P0-3 verify order_kind 参数与 11 处调用修正 + registry 数据结构与落盘 | test_sg4 语义断言重写（红）+ 新 test_state_machine 基础转移 |
| **B2** | P0-1 + P0-4 + P0-5 + P0-6：`_create_protection_order` 闸门 + 14→11 处收编 + ENTRY 纳入（含开仓前置落盘规则）+ HARD_LOCK + 解锁审计 + 重启恢复 + Case A-F crash injection | 新 test_protection_registry + test_crash_injection（A-F 六场景三件套断言） |
| **B3** | P1-A：OrderSnapshot 双通道 + SG2/SG3 复活 + Fail-Closed + 孤儿告警（Q5 若裁决纳入） | test_sg3_p1 重验 + 新 test_dual_channel |
| **B4** | P1-B + P1-C：cooldown 落盘 + 仲裁联动（§10.1）+ 倒计时/告警节流 | test_cooldown_alert 扩展 |
| **B5** | P2 杂项 | 小项回归 |

**实盘恢复门槛**：B1+B2 完成 + 全量回归绿 + ChatGPT 对实施记录复核通过（含 Case A-F 全过）。B3 建议也前置（SG2 现状 bug），由终审裁量。

### 11.2 遗留待裁决问题（送审随本规格）

| # | 问题 | 本规格倾向 |
|---|---|---|
| Q5 | 孤儿检测告警（纯读、仅告警不仲裁）是否纳入 B3？ | 纳入（事故已证价值；OrderSnapshot 到手后边际成本极低） |
| Q6 | 平仓单（3677/3860，reduceOnly 物理兜底 + normal 端点无假阴性）是否纳入唯一入口？ | 本期不纳入（§5.7 理由）；若终审坚持宪法#9 字面全覆盖，以"仅仲裁检查无 Verify"最小形态纳入 |
| Q7 | HARD_LOCK 范围：单 identity 锁 vs 全批锁（任一 identity 硬锁 → 该 batch 全部 Create 停）？ | 单 identity 锁 + critical 告警；全批锁过度保守会误伤健康层（倾向单锁，待裁） |
| Q8 | Binance USDM algo 端点是否支持 newClientOrderId（增强无 ID 自愈精度）？ | 实测后再定，基准方案 = 身份签名匹配（§6.3），不依赖此项 |
| Q4 | 测试语义变更清单（test_sg4 not_found 断言重写 / test_sg3_p1 重验）程序性确认 | 维持 v1 方案 |

---

## 12. 附：本规格源码锚点（全部 Grep/Read 实证，2026-08-20 01:05）

- 14 处 create_order：1071 / 1190 / 1372 / 1811 / 2475 / 2522 / 2978 / 3055 / 3146 / 3342 / 3405 / 3461 / 3677 / 3860（Grep）
- 11 处 verify 调用：1080 / 1199 / 1381 / 2484 / 2531 / 2987 / 3064 / 3155 / 3351 / 3414 / 3470；定义 L1994（Grep）
- ccxt 异常层次：errors.py L70 ExchangeError / L142 OrderNotFound / L182 NetworkError / L186 DDoSProtection / L192 RateLimitExceeded / L194 ExchangeNotAvailable / L210 RequestTimeout
- 开仓循环内存暂存盲区：L1779-1822（entry_orders append，循环后落盘）
- 前人 stop=True 惯例：L940-945 / L1054 / L2645-2646 / L2940 / L2948-2949
- 软计数缺陷：L2087 / L2968-2975 / L3031 / L3036-3044 / L3046+ / L3125-3127
- 单通道消费点：L1539 / L1570 / L2114；SG3 死代码 L2676 / L2795
- ccxt 4.5.68 端点路由：create L6379-6386 / fetch_order L6746+ / fetch_open_orders L7086+

---

## 13. 规格攻击测试：8 个边界场景三问钉死（2026-08-20 09:08 增补，ChatGPT 终审要求）

> 终审要求：每个场景回答三问（① Binance 最终可能有什么？② 本地最终保存什么？③ 系统是否允许再次 Create？）。
> **第 3 问任何情况下不能模糊。** 8 场景全有唯一答案 → B1 方可编码。以下为逐场景定稿答案。

| # | 场景 | ① Binance 最终可能有什么 | ② 本地最终保存什么 | ③ 允许再次 Create？ |
|---|---|---|---|---|
| ① | Create 请求已到 Binance，本地收到 timeout | 订单已创建（真实挂单），或极少情况仍在处理/未接受 | T0 已落盘 PENDING_CREATE → T2b 转 `PENDING_VERIFY(id_unknown)`，params_snapshot 完整 | **禁止**。恢复=身份签名匹配自愈（§6.3）；匹配不到且快照 VALID → NOT_CONFIRMED → 人工 |
| ② | Create 返回 ID，进程立刻崩溃 | 订单已创建 | 崩溃点在 T2c 落盘前 → 本地仅 PENDING_CREATE；重启恢复规则：视同 `PENDING_VERIFY(id_unknown)` | **禁止**（Case C，24 孤儿单场景的通用防线） |
| ③ | Create 返回成功，registry 落盘失败 | 订单已创建 | 落盘失败 = 状态推进失败 → **内存态不前进，维持 PENDING_CREATE**，告警 + 重试落盘；绝不允许"基于内存 ID 直接 Commit"或"当没发生过" | **禁止**。恢复=身份匹配自愈 |
| ④ | Verify 普通端点查不到，conditional 端点能查到 | 订单存在（algo 端点） | `_verify_order_created` 带 `params={'stop': True}` 查到 → `CONFIRMED`（收编真实 ID） | **禁止**（已 CONFIRMED）。此为 C5 事故修复的正向验证场景 |
| ⑤ | 两个查询端点同时失败 | 订单可能存在（未知） | verify UNKNOWN → `NOT_CONFIRMED`；OrderSnapshot INVALID（B3 后） | **禁止**（NOT_CONFIRMED 永不自动 Create；INVALID 禁一切缺失判断） |
| ⑥ | 418 发生在 Create 之后 | 订单可能已创建（限流 ≠ 拒绝；请求可能已被处理） | RateLimitExceeded 属 NetworkError → `PENDING_VERIFY(id_unknown)`；cooldown 落盘（P1-B） | **禁止**。cooldown 期间不重试不 Create；解除后身份匹配自愈 |
| ⑦ | PENDING_VERIFY 状态连续重启 10 次 | 订单已创建（或保持未知） | 每次重启读到同一 PENDING_VERIFY → 恢复规则幂等，**状态不因重启而变**（不变量#15） | **禁止**。10 次重启也不能把 PENDING_VERIFY 降级为可重试 |
| ⑧ | 两个线程同时发现同一 protection 缺失 | 0 或 1 个订单（取决于仲裁原子性） | 仲裁检查在 `_state_lock` 内原子执行 → 仅一个线程通过 → 另一线程见 PENDING_CREATE/VERIFY 已存在 → `blocked` | **禁止**（只有一个线程能通过仲裁进入 Create，其余全部 blocked） |

**三问答案的推论（写入 B1/B2 验收）**：
1. 8 个场景第 3 问**全部为"禁止"**——不存在"允许再次 Create"的自动路径。唯一例外：FAILED（确定拒绝）→ 带计数重试（转移表 FAILED 行）
2. 场景 ③ 揭示硬规则：**registry 落盘失败 ≠ 可以重来**。落盘是状态推进的提交点，失败则状态回退/维持，绝不以内存为真
3. 场景 ⑦ 揭示重启幂等性：恢复规则必须纯函数（状态 → 动作），不引入"重启次数"这类隐式状态
4. 场景 ⑧ 是 B2 仲裁必须持锁的理由（B1 阶段散装 create 不承诺线程安全，B2 收口后生效）
