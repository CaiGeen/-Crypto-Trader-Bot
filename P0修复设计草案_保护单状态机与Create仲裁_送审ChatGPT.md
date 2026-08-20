# P0 修复设计草案：保护单状态机 + Create 仲裁 + 双通道订单视图（送审 ChatGPT）

- **日期**：2026-08-20 01:00（北京时间）
- **状态**：设计草案 v1，待 ChatGPT 终审后按 TDD 九步纪律实施
- **前置**：ChatGPT 两轮交叉评审（事故定级 + 架构裁决）已闭环，本草案按其第十三节实施顺序编制
- **性质**：只读分析产出，未改任何代码

---

## 0. 裁决吸收确认

ChatGPT 终审的核心裁决与本文档的对应关系：

| ChatGPT 裁决 | 本草案章节 |
|---|---|
| 4 态状态机（CONFIRMED / NOT_CONFIRMED / ABSENT / FAILED），NOT_CONFIRMED ≠ ABSENT | §1 |
| P0-1 Create 副作用幂等仲裁（逻辑保护单唯一身份，任何时刻最多一个未终结 Create） | §2 |
| P0-2 条件单 Verify 按 order_kind 显式路由，不靠 helper 猜 | §1.3 |
| P0-3 5/5 硬锁（真熔断器而非计数器，禁止跨轮软复位） | §2.4 |
| P1-1 双通道 open orders（normal + conditional，ID/类型/状态统一） | §3 |
| C5 不整体 revert，保留 retries=1，局部回退危险语义 | §5 |
| P1-2 通知节流（状态转换式） / P1-3 cooldown 落盘 / P2 日志分层 | §4 |
| 候选宪法第 9 条："上一笔 Create 未确认前禁止再次 Create" | §6 |

---

## 1. 设计一：保护单生命周期状态机（4+1 态）

### 1.1 状态定义

```
                    create_order()
                        │
            ┌───────────┴───────────┐
            ▼                       ▼
     抛异常（无订单ID）          返回 exchange_order_id
            │                       │
            ▼                       ▼
         FAILED               PENDING_VERIFY
            │                       │
            │              ┌────────┼────────┐
            │              ▼        ▼        ▼
            │          CONFIRMED  查询失败  查询异常
            │              │        │        │
            │              ▼        ▼        ▼
            │           COMMIT   NOT_CONFIRMED（含 UNKNOWN）
            │                        │
            ▼                        ▼
   fail_count++（≥5 → 硬锁）   禁止 Create，进入重查自愈/人工恢复
```

| 状态 | 进入条件 | 能否再次 Create | 说明 |
|---|---|---|---|
| `FAILED` | create_order 抛异常，**未拿到任何订单 ID** | ✅ 允许（带计数） | 无副作用证据，重试安全 |
| `PENDING_VERIFY` | create_order 返回 ID（瞬时态） | ❌ | 注册表立即落盘此态（崩溃安全） |
| `CONFIRMED` | verify 精准查询确认存在 | ❌（该单已在） | 调用方 Commit 业务字段 |
| `NOT_CONFIRMED` | verify 返回 OrderNotFound **或** 网络/查询异常 | **❌ 永不自动** | "查不到 ≠ 不存在"（宪法第 9 条候选） |
| `ABSENT` | **仅人工或独立证据链**确认订单从未存在/已终结 | ✅（人工解锁后） | 本期自动路径**不产生** ABSENT |

**关键语义修正**（对应 ChatGPT P0-4）：现 C5 的 `not_found → 既有失败路径（可安全重试）` 全部废除。OrderNotFound 只证明"本次查询未通过当前查询路径找到"，不证明"订单不存在"。

### 1.2 NOT_CONFIRMED 的两条出路（均无副作用）

1. **重查自愈（自动）**：监控循环每轮对 NOT_CONFIRMED 记录重查一次（用正确的 order_kind 路由）：
   - 查到存在 → 升级 CONFIRMED，**补 Commit**（这是唯一允许的自动动作：把已存在的单收编进状态，不新建）
   - 仍查不到/查询失败 → 维持 NOT_CONFIRMED，静默（告警仅状态转换时发，见 §4）
2. **人工恢复**：用户到交易所核实后，通过解锁通道（§2.5）标记 ABSENT（"确认没有此单"）或直接提供订单号补录 CONFIRMED。

**禁止**：任何自动路径把 NOT_CONFIRMED 翻转为 ABSENT 再触发 Create。

### 1.3 Verify 路由按 order_kind 显式声明（P0-2）

`_verify_order_created` 签名扩展（ChatGPT 裁决：helper 必须知道自己验证的订单类型，不猜）：

```python
def _verify_order_created(self, order_id, symbol, order_kind='conditional'):
    """
    order_kind:
      'conditional' → fetch_order(..., params={'stop': True})  # algo 通道，覆盖 STOP_MARKET / TAKE_PROFIT_MARKET
      'normal'      → fetch_order(...)                          # 普通通道，LIMIT / MARKET
    """
    params = {'stop': True} if order_kind == 'conditional' else None
    ...
```

**源码级依据**（已验证，.venv ccxt 4.5.68）：
- create 侧：`isConditional = (triggerPrice is not None) or ...` → 所有带 stopPrice 的单 `POST /fapi/v1/algoOrder`（L6379-6386）→ 返回 3000 开头 algo ID
- 查询侧：`isConditional = safe_bool_n(params, ['stop','trigger','conditional'])` → algoId 查 `GET /fapi/v1/algoOrder`（L6746+）
- 项目内前人惯例佐证：recover L940-945 / SL 触发检测 L2645-2646 / 旧 SL 查询 L2940 / 撤旧单 L2948-2949 **全部带 `params={'stop': True}`**——只有 C5 的 12 处 Verify 漏带

**调用点 kind 映射**（14 处全覆盖）：

| 调用点（现行号） | 订单类型 | order_kind |
|---|---|---|
| 1071 / 1190（修改 TP / SL） | TAKE_PROFIT_MARKET / STOP_MARKET | conditional |
| 1372（保本损） | STOP_MARKET | conditional |
| 2475 / 2522（减仓后挂新 SL/TP） | STOP_MARKET / TAKE_PROFIT_MARKET | conditional |
| 2978 / 3055（补挂 SL / 降级恢复） | STOP_MARKET | conditional |
| 3146（TP 更新） | TAKE_PROFIT_MARKET | conditional |
| 3342 / 3405 / 3461（预生成 SL / 兜底 SL / 预生成 TP） | STOP_MARKET / TAKE_PROFIT_MARKET | conditional |
| 1811（B 级开仓条件单） | STOP_MARKET | conditional（本期仅 retries=1，见 §2.6 开放问题 Q2） |
| 3677 / 3860（C 级市价/限价平仓） | MARKET / LIMIT | normal（不走 Verify，维持现状） |

---

## 2. 设计二：Create 副作用幂等仲裁（P0-1 + P0-3 + P0-4）

### 2.1 逻辑保护单唯一身份（幂等键）

```
identity = f"{protection_type}|layer{layer}|{position_side}"
# 例如: "SL|layer2|LONG"、"TP|layer0|LONG"
# 批次内唯一（注册表挂在 batch_state 下，天然含 batch_id + symbol）
```

### 2.2 保护单注册表（持久化）

`batch_state` 新增字段（随既有 `save_batch_state` 原子落盘，重启安全）：

```python
'protection_registry': {
    "SL|layer2|LONG": {
        'state': 'NOT_CONFIRMED',       # FAILED / PENDING_VERIFY / CONFIRMED / NOT_CONFIRMED / ABSENT
        'order_id': '3000002145678590',
        'order_kind': 'conditional',
        'created_at': 1787160000,
        'last_verify_at': 1787160600,
        'fail_count': 0,                # 仅 FAILED（create 本身失败）计数
        'hard_locked': False,           # True = CRITICAL_STOP
    },
    ...
}
```

### 2.3 统一收口：`_create_protection_order()` 唯一 Create 闸门

当前 14 处 create_order 调用分散在 5 类路径（修改/保本/减仓换挂/补挂/预生成/降级），每处自带重试与判定——这是本次事故的结构土壤。**全部保护单 Create 收编为唯一入口**：

```python
def _create_protection_order(self, symbol, batch_id, layer, ptype, position_side,
                             order_type, side, amount, params, order_kind):
    identity = f"{ptype}|layer{layer}|{position_side}"
    reg = self._get_protection_registry_entry(symbol, batch_id, identity)

    # ── 仲裁 1：存在未终结 Create（PENDING_VERIFY / NOT_CONFIRMED）→ 拒绝，返回 blocked
    # ── 仲裁 2：hard_locked=True → 拒绝（CRITICAL_STOP）
    # ── 仲裁 3：fail_count >= 5 → 置 hard_locked + critical 告警 + 拒绝
    # ── 执行：create_order(retries=1)
    #      异常 → FAILED，fail_count++，≥5 → 硬锁；返回 failed
    #      成功 → 注册 PENDING_VERIFY（先落盘！）→ verify(order_kind)
    #           CONFIRMED      → 注册表 CONFIRMED + 返回 (success, order_id) → 调用方 Commit 业务字段
    #           NOT_CONFIRMED  → 注册表 NOT_CONFIRMED + critical 告警（一次性，§4 节流）+ 返回 blocked
    #           UNKNOWN        → 同 NOT_CONFIRMED（本设计不分 not_found/unknown，统一禁副作用）
```

**PENDING_VERIFY 先落盘再 verify**：崩溃发生在 create 返回与 verify 之间时，重启后注册表仍有该记录 → 仍受仲裁保护（不会重新 Create），只会进入重查自愈。

### 2.4 5/5 硬锁 = CRITICAL_STOP（P0-3，修正软计数）

现状缺陷（源码实证）：
- `sl_fail_count` 按层计数（L3031），5/5 触发仅发告警（L3036-3044，文案谎称"已停止自动重试"）
- "同步维护"路径 L2968-2975 **确有** `layer_failed` 闸门，但：
  - 降级恢复路径（L3046+）**绕过该闸门**继续 create_order
  - 新层成交 → 新 layer key → 计数器从头开始（事故日志"第 3 层 5/5 → 第 4 层 2/5"实证）
  - `sl_failed_layers` 列表**只写不读**（L3125-3127 追加，全文件无任何读取方）——死字段
- 成功路径 L3021-3022 重置计数是对的（真失败→成功应复位），保留此语义但**只对 FAILED 态有效**

新语义：
```
fail_count（FAILED 计数）≥ 5 → hard_locked=True（落盘，跨轮/跨重启有效）
hard_locked 后：
  - 一切该 identity 的 Create 拒绝（含预生成、补挂、降级恢复、修改换挂）
  - 单次 critical 告警（进入时），此后静默直到解锁
  - 解锁仅人工通道（§2.5）
```

### 2.5 人工解锁通道（最小方案）

本期不建 TG 命令体系（避免扩大改动面），解锁 = 用户确认后由程序侧提供：
- **方案 U1（推荐）**：TG 收到 CRITICAL_STOP 告警后，用户回电脑在 `trade_state.json` 对应条目手动改 `hard_locked: false` / `state: 'ABSENT'`，然后重启 bot。配套：告警消息中写明操作步骤与字段路径。
- 方案 U2：新增 TG 命令 `/unlock <batch_id> <layer> <SL|TP>`（bot_runner 命令处理器）。更顺手但改动面 +1，且命令本身需要鉴权与幂等设计。
- **待裁决 Q3**：选 U1 还是 U2？

### 2.6 涉及的开放问题

- **Q1（ABSENT 证据标准）**：本期自动路径完全不产生 ABSENT 是否过于保守？例如 fetch_order(stop=True) 返回明确的 `rejected`/`expired`（从未激活）可否自动判 ABSENT？**本草案倾向保守**：Binance algo 单查询返回的状态机语义未经实测验证，且"查询返回了订单但状态是 X"与"订单从未存在"仍需区分，本期交人工。
- **Q2（B 级开仓条件单是否纳入仲裁）**：1811 的开仓 STOP_MARKET 同样走 algo 端点、同样有副作用（**重复开仓 = 直接资金风险，比保护单重复更危险**）。当前它连 retries 之外零保护。若开仓单 create 后假阴性/重启，是否也需要注册表仲裁（protection_type='ENTRY'）？**本草案倾向纳入**（identity = "ENTRY|batch|LONG"，同一套闸门），但会扩大实施范围，待裁决。
- **Q3**：解锁通道 U1（手改 state 文件）vs U2（TG 命令）。
- **Q4**：本设计三批全部完成后，test_sg4 现有 25 场景中 not_found→可重试 相关断言需按新语义重写（不是改断言凑绿，是规格本身变了）；test_sg3_p1 28 场景受 §3 SG3 复活影响需重验。测试语义变更清单将在实施前单独列出供确认。

---

## 3. 设计三：双通道订单视图（P1-1）

### 3.1 新增 helper

```python
def _fetch_all_open_orders(self, symbol):
    """双通道快照：normal + conditional。返回 (orders_list, conditional_view_valid)"""
    normal = self._safe_api_call(self.exchange.fetch_open_orders, symbol) or []
    try:
        conditional = self._safe_api_call(
            self.exchange.fetch_open_orders, symbol, params={'stop': True}) or []
    except Exception:
        conditional = []
        conditional_view_valid = False   # 条件单通道查询失败
    # 打来源标记后合并：{id: {**order, 'view_source': 'normal'|'conditional'}}
```

**Fail-Closed 原则**：`conditional_view_valid=False` 时，一切依赖"条件单不在快照中"的推断（缺失判断类）一律跳过本轮并告警一次；**绝不**把"看不到"当"不存在"（宪法第 8 条 Fail-Closed）。

### 3.2 替换 3 处调用点

| 现调用点 | 用途 | 替换后行为 |
|---|---|---|
| L2114（监控主循环） | open_orders_map 构建 | 双通道合并 map |
| L1539（SG2 加仓风险闸门） | 有效 SL 校验（current_sl_id 在 open_orders 中） | 双通道合并（**现状 bug**：SL 是条件单永不在普通快照 → SG2 的"有效 SL"检查对条件单恒 False → 宪法②"无有效 SL 禁加仓"实际一直靠 UNKNOWN 拒绝路径兜底） |
| L1570（孤儿/终态检测） | known_order_ids | 双通道合并 |

### 3.3 修复 C4/SG3 死代码（本次源码核查新发现）

**事实**：SG3-P1 语义校验分支 L2676（SL）/ L2795（TP）条件均为 `str(id) in open_orders_map`——而条件单**从来不在**普通 open_orders 快照中 → **这两个分支自 C4 上线以来从未执行过（死代码）**。本项目所有 SL/TP 全是条件单，即 C4 的运行时保护语义校验实际从未生效。

双通道合并后这些分支将**首次真正运行**，行为变化需重点验收：
- SL/TP 在场时每轮做方向/数量/保护语义校验（`_check_protection_order_validity`，L1932）
- 校验失败 → 撤销重挂（L2694 need_recover_sl 路径）——**该路径的撤销重挂必须走 §2 闸门**（否则引入新的重复 Create 入口）

同时保留 L2640-2646 的逐单 `fetch_order(stop=True)` 兜底（双保险：map 未含时精准查询终态）。注意双通道后正常情况下条件单**会**出现在 map 中，L2640 分支只在单子已触发/撤销时进入——语义从"每轮必进"变为"终态检测"，需在测试中覆盖。

### 3.4 最小孤儿检测告警（可选附加，待裁决 Q5）

事故已证明孤儿单检测的现实价值（24 个孤儿单靠人眼在 App 发现）。openAlgoOrders 快照到手后，与状态库/protection_registry 比对"交易所存在但程序不认识的条件单"只需纯读逻辑（已有 `_check_protection_order_validity` 邻近的 SG5 SKIP 测试可复用）。**仅告警不仲裁**，与 ChatGPT 早前"孤儿检测移出 C5"裁决不冲突（当时移出是因为扩大 C5 范围；现在是独立 P1-A 附加项）。**待裁决 Q5：是否纳入本批？**

---

## 4. 告警与日志（P1-2 / P1-B / P1-C / P2）

1. **418 倒计时节流**：进入熔断立即 1 条 + 之后每 60 秒 1 条 heartbeat（或仅打印状态变化：剩余 60s/30s/10s/解除）。
2. **critical 告警节流（状态转换式）**：复用 `_sg3_alerted` 去重模式，键 = (batch_id, layer, error_class)，同键 10 分钟窗口内只发 1 次；**状态转换（首次失败 / 触发硬锁 / 恢复）允许立即发**。邮件与 TG 同规则。
3. **cooldown 落盘（P1-3，即原 P1-API-01）**：`api_cooldown_until` 写入独立文件（`api_cooldown.json`，原子写），启动时读取：仍在封禁期 → 直接进入等待，禁止立即打 API（防"重启→立即请求→再撞 418"）。
4. **TG Markdown 修复（P2）**：`_verify_failure_msg` 的模板反引号与 TG parse_mode 组合在含特殊字符 symbol/ID 时解析失败（事故日志每条都降级）。修复：通知统一改 HTML parse_mode 或纯文本 + 一次性回归验证。
5. **日志分层（P2，本轮仅 stdout 降噪）**：stdout 只留关键状态转换 + heartbeat；完整事件进文件日志（watchdog 转发落盘同步做，解决"PyCharm 卡死即证据全灭"的取证困境）。完整结构化日志体系归 C6/SG9，不抢跑。

---

## 5. C5 commit（f20ae62）处置

按 ChatGPT 裁决：**不整体 revert**。

- **保留**：14 处 `retries=1` 禁盲重（独立正确的 P0）
- **就地改造**（不 revert 的原因：新状态机直接以 `_verify_order_created` + 11 处接入点为骨架演进，revert 后再重做等于三倍工作量 + 双倍风险）：
  - `_verify_order_created` → 加 order_kind 参数（§1.3）
  - not_found"可安全重试"语义 → NOT_CONFIRMED 禁副作用（§1.1）——所有调用点的 `not_found` 分支改走 blocked
  - 11 处散装 create+verify → 收编进 `_create_protection_order` 闸门（§2.3）
- **顺序保证**：批次 1（§1 语义）先行使系统脱离"假阴性→重挂"危险区，批次 2（§2 仲裁）收口。批次 1 完成前实盘保持停止。

---

## 6. 实施批次与测试计划（TDD：红→改→绿→全量回归→确认→commit）

| 批次 | 内容 | 测试 |
|---|---|---|
| **B1** | §1.3 verify order_kind + §1.1 三态→四态语义（not_found/unknown 统一 NOT_CONFIRMED 禁副作用）+ NOT_CONFIRMED 重查自愈（只补 Commit 不新建） | test_sg4 重写语义断言（红：新规格 vs 现行为）|
| **B2** | §2 protection_registry + `_create_protection_order` 闸门收编全部路径 + 5/5 硬锁 CRITICAL_STOP + 解锁通道 | 新 test_protection_registry（闸门/硬锁/崩溃恢复/重启仲裁）|
| **B3** | §3 双通道 fetch + SG2/SG3 复活 + Fail-Closed 视图标记 + （Q5 若裁决纳入）孤儿最小告警 | test_sg3_p1 重验 + 新 test_dual_channel |
| **B4** | §4.1-4.3：倒计时节流 + 告警状态转换节流 + cooldown 落盘 | test_cooldown_alert 扩展 |
| **B5** | §4.4-4.5：TG Markdown + stdout 降噪/watchdog 落盘 | 小项回归 |

每批独立 commit（用户逐批确认）；B1-B2 为恢复实盘的前置条件；B3-B5 可在实盘恢复后推进（B3 建议也在恢复前，因 SG2 现状 bug）。

**建议实盘恢复门槛**：B1+B2 完成 + 全量回归绿 + ChatGPT 对 B1/B2 实施记录复核通过。

---

## 7. 汇总：待 ChatGPT 裁决的 5 个开放问题

| # | 问题 | 本草案倾向 |
|---|---|---|
| Q1 | ABSENT 是否允许自动判定（如查询返回明确 rejected/expired）？ | 否，本期仅人工 |
| Q2 | B 级开仓条件单（L1811）是否纳入 Create 仲裁（ENTRY 身份）？ | 纳入（重复开仓风险更高） |
| Q3 | 硬锁解锁通道：手改 state 文件（U1）vs TG 命令（U2）？ | U1（最小改动） |
| Q4 | 测试语义变更清单（test_sg4 not_found 断言重写、test_sg3_p1 重验）在实施前单独确认——程序性确认即可？ | 是 |
| Q5 | 最小孤儿检测告警（纯读、仅告警不仲裁）是否纳入 B3？ | 纳入（事故已证价值） |

---

## 8. 附：本草案源码锚点（全部经 Grep/Read 实证，2026-08-20 00:54）

- `_verify_order_created` L1932 区 / 14 处调用点：1071 / 1190 / 1372 / 1811 / 2475 / 2522 / 2978 / 3055 / 3146 / 3342 / 3405 / 3461 / 3677 / 3860
- 前人 `params={'stop': True}` 惯例：L940-945（recover）/ L2645-2646（SL 触发检测）/ L2940（旧 SL 查询）/ L2948-2949（撤旧单）
- 软计数缺陷：L2087（MAX=5）/ L2968-2975（layer_failed 闸门，仅此路径）/ L3031（计数）/ L3036-3044（告警文案谎称已停止）/ L3046+（降级路径绕过闸门）/ L3125-3127（sl_failed_layers 只写不读）
- open_orders 单通道：L1539 / L1570 / L2114；SG3 死代码分支 L2676 / L2795
- 状态持久化：`save_batch_state`（含 pending_sl_orders / sl_fail_count / sl_failed_layers 字段先例）
- ccxt 4.5.68 路由：create L6379-6386（isConditional→algoOrder）/ fetch_order L6746+（isConditional→algoId）/ fetch_open_orders L7086+（同 isConditional 路由）
