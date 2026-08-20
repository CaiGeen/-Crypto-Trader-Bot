# C5 Verify 假阴性 → SL/TP 无限重复挂单事故报告（送审 ChatGPT 交叉评审）

- 日期：2026-08-19 23:45
- 作者：WorkBuddy（本地排查）
- 性质：只读排查报告，未修改任何代码
- 严重度：**P0 资金安全**（交易所侧堆积 24 个真实孤儿保护单；事故由 2026-08-19 17:44 推送的 C5 代码 commit f20ae62 直接引入）
- 关联文档：C5_SG4_设计草案_送审ChatGPT.md（v2，本事故证伪其核心假设之一）

---

## 一、事件概述（⚠️ 资金安全现场，需优先处理）

22:53 用户重启系统后，程序在处理 4 层买单成交、挂 SL/TP 保护单时，**每一单 create_order 都真实成功**（交易所返回真实订单 ID），但 C5 新增的 `_verify_order_created` 验证**全部误判为 not_found**，程序据此认为"未挂上"→ 不记录订单 ID（不 Commit）→ 下一轮监控重试 → 再挂一单 → 再误判 → **无限循环**。

4 分钟内（22:53-22:57）在交易所堆积 **24 个真实孤儿单（12 SL + 12 TP）**，与用户在 App 上观察到的"20 多单"吻合。用户手动停止 watchdog 后循环才终止。

### 孤儿单 ID 名单（请用户到交易所 App 逐一核对处理）

| 类型 | 订单 ID（全部为交易所真实 ID） |
|---|---|
| SL 止损单 ×12 | 3000002145678590, 3000002145678670, 3000002145678738, 3000002145678809, 3000002145679054, 3000002145679445, 3000002145679720, 3000002145683589, 3000002145683723, 3000002145684062, 3000002145684334, 3000002145684663 |
| TP 止盈单 ×12 | 3000002145678646, 3000002145678712, 3000002145678758, 3000002145678835, 3000002145679100, 3000002145679467, 3000002145679768, 3000002145683678, 3000002145683749, 3000002145684093, 3000002145684360, 3000002145684696 |

**风险**：这些 SL/TP 均为 reduceOnly/closePosition 性质的保护单，若价格触发将依次执行平仓。用户当前持仓为 4 层 BTCUSDT 多单（成交价 65483.7 / 65590.6 / 65697.6 / 65804.9）。**建议用户在 App 手动清理孤儿单后按需重建正确的 SL/TP。**

## 二、日志证据（用户提供终端日志摘录，22:53-22:57）

```
🎯 [批次 batch_20260819_081653_0cd379] 第 1 层买单成交！实际成交价: 65483.7
  └─ ⚡ 预生成止损单挂出失败: 预生成止损单创建验证失败: OrderNotFound (id=3000002145678590)
  └─ ⚡ 预生成止盈单挂出失败: 预生成止盈单创建验证失败: OrderNotFound (id=3000002145678646)
...（第 2/3/4 层同样模式）...
⚠️ [TP 补挂] 批次 ... 止盈单缺失(未创建或创建失败)，准备补挂...      ← 每轮重复
⚡ [批次 ...] 处理待补挂止损，等待主循环更新...                     ← 每轮重复
  └─ 🔧 补挂待处理止损层: [0, 1, 2, 3]
⚡ [批次 ...] 同步维护独立风控...
  └─ ❌ 挂出止损单失败: 止损单创建验证失败: OrderNotFound (id=3000002145678809)
  └─ ⚠️ 第 3 层止损单失败次数: 2/5 → 3/5 → 4/5 → 5/5
  └─ ⚠️ 无旧止损信息，无法降级恢复
  └─ ❌ 止盈单验证失败(not_found)，不 Commit: 3000002145678835
ℹ️ [TG通知] Markdown 解析失败(Can't parse entities...)，已降级纯文本发送   ← 每轮重复
📧 [邮件] 已发送: 🚨 资金安全告警                                      ← 4 分钟 8 封
```

关键观察：**订单 ID 单调递增**（5678590 → 5684696），证明每次都是交易所真实受理并分配新 ID——create 全部成功，问题只在 Verify 判定。

## 三、根因：ccxt `fetch_order` 的条件单端点路由（源码级铁证）

### 3.1 机制

ccxt 4.5.68（本机 .venv 实测源码）`binance.py` L6746-6810 `fetch_order`：

```python
isConditional = self.safe_bool_n(params, ['stop', 'trigger', 'conditional'])   # ← 只有 params 带了 stop/trigger/conditional 才为 True
...
elif market['linear'] and market['swap'] and isConditional and not isPortfolioMargin:
    request['algoId'] = id          # ← 条件单：algoId 查询
else:
    request['orderId'] = id         # ← 普通单：orderId 查询
...
if isConditional:
    response = self.fapiPrivateGetAlgoOrder(...)   # GET /fapi/v1/algoOrder（条件单端点）
else:
    response = self.fapiPrivateGetOrder(...)       # GET /fapi/v1/order（普通订单端点）
```

同时 ccxt 能力矩阵（L1696）明确声明：
```python
'fetchOrder': {
    'trigger': False,     # ← Binance 合约 fetchOrder 对触发单的支持依赖显式 params
    'trailing': False,
}
```

### 3.2 完整因果链

1. `create_order(type='STOP_MARKET'/'TAKE_PROFIT_MARKET', params={...stopPrice...})` → ccxt 识别为条件单 → 走**条件单端点**创建成功 → 返回真实订单 ID
2. C5 的 `_verify_order_created`（trader_260725.py L1994-2010）调用：
   ```python
   self._safe_api_call(self.exchange.fetch_order, order_id, symbol, retries=1)   # ← 无 params！
   ```
   → `isConditional=False` → 用 orderId 查**普通订单端点** → 条件单不在该端点 → **-2013 "Order does not exist" → ccxt.OrderNotFound**
3. `_verify_order_created` 返回 `'not_found'`
4. 按 C5 三态设计："not_found → 不 Commit + **既有失败路径（可安全重试）**" → 程序不记录订单 ID → 下一轮监控（60 秒）重挂 → 新单再被误判 → **无限循环挂单**
5. 多个路径同时踩坑：预生成 SL（L3351）、预生成 TP（L3470）、兜底 SL（L3414）、同步维护补挂、TP 补挂——**12 处 A 级 Verify 调用全部存在同一缺陷**

### 3.3 项目内早已有正确惯例（C5 实施遗漏）

`recover_active_batches`（trader_260725.py L940-945）验证既有 SL 单时：
```python
sl_order = self._safe_api_call(
    self.exchange.fetch_order,
    b_data['current_sl_id'],
    symbol,
    params={'stop': True}    # ← 前人已踩过此坑，查询条件单必须带 stop=True
)
```
**C5 新增的 12 处 Verify 全部没有传 `params={'stop': True}`**——设计草案与实施均未对齐这个项目既有惯例。

## 四、C5 设计假设被实盘证伪的点

设计草案 v2（ChatGPT 裁决）的核心假设："**not_found（OrderNotFound）→ 订单确实不存在 → 可安全重试**"。

实盘证伪：OrderNotFound 也可能来自**查询方式错误**（查询端点与订单类型不匹配的假阴性）。此时"安全重试"恰恰是最危险的动作——每次重试都在交易所真实加一单。**NOT_FOUND ≠ 一定不存在，正如 UNKNOWN ≠ 一定不确定——三态语义本身没错，错的是把"交易所查无此单"等同于"订单不存在"，忽略了查询通道本身可能查错地方。**

与本系统宪法第一条"UNKNOWN ≠ EMPTY"同构：本事故是它的镜像——**"查不到"不等于"不存在"**。

## 五、为什么测试（test_sg4.py 25/25 绿）没有抓到

1. **mock exchange 的 fetch_order 是 MagicMock**：不会真调 Binance 端点，永远按测试脚本设定的返回值走（成功/OrderNotFound/NetworkError）——**API 端点路由这种"查询通道语义"无法被离线 mock 暴露**
2. B 组测试只断言"Verify 被调用"（AST 检查 fetch_order 调用存在），不校验 params 正确性
3. 根因属于 **ccxt 交易所特定行为**（Binance 条件单双端点架构），设计草案、评审、测试三层的知识盲区一致

这是"离线Mock语义验收方法"的固有边界：语义层（调用了吗、三态分支对吗）可测，**通道层（这个调用在真实交易所会查对地方吗）不可测**——需要实盘影子验证或 ccxt 能力矩阵审查。

## 六、关联缺陷清单（同一次日志中暴露）

| # | 缺陷 | 证据 | 严重度 |
|---|---|---|---|
| 1 | **邮件风暴**：SL/TP 挂单失败的 critical 告警无节流，4 分钟 8 封"🚨 资金安全告警"邮件 | 终端日志 8 × `📧 [邮件] 已发送` | P1（告警疲劳会淹没真告警） |
| 2 | **TG 消息 Markdown 解析失败**：`_verify_failure_msg` 生成的消息触发 "Can't find end of the entity"，每条都降级纯文本 | 每轮 `ℹ️ [TG通知] Markdown 解析失败` | P2（降级兜底生效，但格式有 bug） |
| 3 | **失败计数语义混乱**：`第 3 层止损单失败次数 2/5→5/5` 与 `第 4 层 ... 2/5→5/5`——5/5 达到后仅有"无旧止损信息，无法降级恢复"，**没有阻止下一轮继续挂单**（第 4 层成交后又从头计数） | 日志时序 | P1（熔断机制形同虚设） |
| 4 | 监控标记残留提示（`监控标记残留，自动清理`）| 恢复流程 L958-962 与 _start_monitoring L2034-2038 的注册顺序问题，良性但日志误导 | P3 |

## 七、修复方向候选（供 ChatGPT 裁决，未实施任何一项）

**方向 A（最小修复）**：`_verify_order_created` 增加参数透传——所有 SL/TP（条件单）Verify 调用统一带 `params={'stop': True}`；普通限价单（C 级平仓单）不带。
- 风险点：需逐处确认订单类型与参数匹配；take_profit_market 与 stop_market 是否都路由到 algo 端点需实测确认

**方向 B（防御性增强）**：not_found 判定后**不直接重试**，先用 `fetch_open_orders` 快照二次确认（订单若在快照中 → 按成功处理并 Commit）。把"not_found→重试"升级为"not_found→快照复核→仍不存在才重试"。
- 与草案 v2 裁决"Verify 不用 open_orders 快照"不冲突：快照不做第一验证，只做 not_found 的复核兜底
- 本事故中该方案可 100% 拦截重复挂单

**方向 C（Create 返回值分级）**：create_order 正常返回完整订单结构（含 id + status）时，说明响应已收到、交易所已确认——此时 Verify 的边际价值仅剩"防御 ccxt 内部异常"。可讨论：create 返回结构完整时跳过 fetch_order verify，只在返回残缺/异常时 verify。
- 需 ChatGPT 裁决与三态设计的兼容性

**其他必修项**：
- SL/TP 挂单失败 critical 告警加节流（如每批次每 10 分钟最多 1 封）
- 失败计数 5/5 触顶后应有硬停止（停止该层补挂 + 保持 critical 人工介入），而非重置继续
- `_verify_failure_msg` 的 Markdown 格式修复（或统一改纯文本）

## 八、待 ChatGPT 裁决的问题

1. 方向 A/B/C 的取舍（可组合：A 为必修 + B 为兜底？）
2. "not_found → 可安全重试"的三态语义如何修正？（not_found 是否应降级为"疑似不存在"，重试前强制快照复核？）
3. 本次 24 个孤儿单的处理建议（全撤重挂？保留部分？）
4. 失败计数触顶后的正确行为（硬停止 vs 降级 vs 人工介入等待）
5. 是否需要为此类"通道语义"缺陷建立 ccxt 能力矩阵审查清单（fetchOrder.trigger=False 等红旗在实施前检查）？
6. **回滚问题**：C5 已推送（f20ae62）。在修复完成前，是否建议临时回滚 Verify 逻辑（保留 retries=1 禁盲重部分）？——retries=1 部分无此问题且是既有 P0 修复

---

## 附录 B：ChatGPT 交叉评审吸收 + 源码二次验证（2026-08-20 00:40 增补）

### B.1 ChatGPT 评审结论采纳情况

ChatGPT 5/5 星认同核心因果链，并提出 4 项结构性修正，全部采纳：

| ChatGPT 裁决 | 本报告原表述 | 修正 |
|---|---|---|
| **P0-1 阻止重复 Create**：一次 Create 已拿到交易所 ID 后，最终状态未确认前禁止再次 Create 同类保护单 | 方向 A+B 为修复主案 | **A/B 降级为辅助**，P0-1 Create 仲裁保护为第一优先 |
| **P0-4 重定义 NOT_FOUND → NOT_CONFIRMED**：OrderNotFound 只证明"本次查询未找到"，不证明"订单不存在" | not_found→可安全重试（沿用草案 v2 语义） | 三态语义修正：not_found → VERIFY_NOT_CONFIRMED → **禁止副作用操作**（不自动重挂） |
| **P0-3 5/5 硬锁死**：失败计数 5/5 应进入 CRITICAL_STOP（禁自动 Create/禁补挂/禁降级循环/保持人工介入），而非下一轮继续 | 已识别为缺陷 P1 | 升级 P0/P1，与 P0-1/P0-4 同批修复 |
| **stop=True 是必要非充分**：即使带 params，"verify 失败→重挂"的结构性风险仍在 | 方向 A 表述偏"最终方案" | 已按左列修正定位 |

**新增安全不变量（候选，待纳入系统宪法第 9 条）**：
> 任何具有交易所副作用的 Create，在上一笔 Create 的最终存在性未被确认之前，禁止再次 Create。
> （"查不到 ≠ 不存在" —— 宪法第 1 条 UNKNOWN≠EMPTY 的镜像条款）

### B.2 源码二次验证（回应 ChatGPT "stop=True 覆盖性需验证"的质疑）

**验证 1：create_order 条件单路由（.venv ccxt 4.5.68 binance.py L6340-6386 实测源码）**

```python
triggerPrice = self.safe_string_2(params, 'triggerPrice', 'stopPrice')
...
isConditional = (triggerPrice is not None) or isTrailingPercentOrder or isStopLoss or isTakeProfit
...
elif market['linear']:            # Binance USDM 合约
    if isConditional:
        request['algoType'] = 'CONDITIONAL'
        response = self.fapiPrivatePostAlgoOrder(request)   # POST /fapi/v1/algoOrder
    else:
        response = self.fapiPrivatePostOrder(request)       # POST /fapi/v1/order
```

**结论**：当前 ccxt 版本下，Binance USDM **所有带 stopPrice 的单**（项目全部 SL=STOP_MARKET、TP=TAKE_PROFIT_MARKET 无一例外）创建一律走 algo 端点并返回 algo ID（3000 开头 16 位，与事故日志 ID 格式吻合）。`fetch_order` 带 `params={'stop': True}` 走 `GET /fapi/v1/algoOrder` + `algoId` 查询同端点——**端点匹配确证，方向 A 覆盖当前全部条件单类型** ✅
（普通 LIMIT 单不带 stopPrice → 普通端点 → fetch_order 不带 params 查询匹配，C 级平仓单不受影响。）

**验证 2：⚠️ 新发现第二独立缺陷——fetch_open_orders 对条件单同样失明**

ccxt `fetch_open_orders`（L7086+）同样以 `isConditional = safe_bool_n(params, ['stop','trigger','conditional'])` 路由：
- 不带 params → `GET /fapi/v1/openOrders`（**只含普通单，algo 条件单完全不在结果里**）
- 带 `params={'stop': True}` → `GET /fapi/v1/openAlgoOrders`

**影响范围**：监控循环每轮的 `open_orders_map`（trader L2114，不带 params）**看不到任何 SL/TP 条件单**。潜在受影响路径：
1. C4/SG3 保护单有效性检查（以 open_orders_map 为数据源）——SL 单不进 map，检查失明（是"无害跳过"还是"误判缺失触发补挂"需逐分支确认，**待裁决**）
2. "SL 已触发执行"的检测（依赖 map 中 SL 消失来判断）——algo 单本来就不在 map，检测逻辑是否仍有效**待确认**
3. 用户手动撤单检测（manual_canceled_detected）同类问题

**注意**：该缺陷自项目使用 ccxt 4.5.68 起即存在（非 C5/C4 引入），但 C4（SG3 检查）与 C5（补挂循环）的上线可能使其从"休眠"转为"激活"。**本事故主因链已确认是 C5 不 Commit**（TP 补挂判断依据 `tp_order_id is None`，L2822；SL 补挂依据 `current_sl_id is None`——均为状态字段而非 open_orders_map），fetch_open_orders 失明未直接参与本次 24 单风暴，但其独立风险需 ChatGPT 一并裁决。

**验证 3：本事故主因链确认（回应"是否双重放大"）**

22:53 日志中每轮补挂的触发依据：
- TP："止盈单缺失" ← `tp_order_id is None`（L2822）← C5 not_found 不 Commit → 状态永远无 ID
- SL："同步维护独立风控"挂单 ← `current_sl_id is None` ← 同上

即：**24 单风暴 = C5 假阴性（不 Commit）× 自动补挂（无 Create 仲裁）× 5/5 无硬停止** 三重叠加，fetch_open_orders 失明未参与主链。与 ChatGPT 第十二节事故树一致，仅补充此确认。

### B.3 修正后的修复优先级（替代第七节，供最终裁决）

| 优先级 | 修复项 | 内容 |
|---|---|---|
| **P0-1** | Create 仲裁保护 | 状态机引入 PENDING_VERIFY：Create 拿到交易所 ID 后，最终状态确认前禁止再次 Create 同类保护单（not_found/unknown 均落入此态，永不自动重挂） |
| **P0-2** | Verify 端点修正 | `_verify_order_created` 对条件单传 `params={'stop': True}`（源码验证已确证匹配） |
| **P0-3** | 5/5 硬锁死 | 失败计数触顶 → layer_protection_state=CRITICAL_STOP：禁自动 Create/禁补挂/禁降级/单次 critical + 人工介入 |
| **P0-4** | 三态语义修正 | not_found → NOT_CONFIRMED（禁副作用）；unknown → 同禁；仅 SUCCESS 可 Commit |
| P1-A | fetch_open_orders 双端点合并查询 | 监控循环需同时拉普通单 + `params={'stop': True}` 条件单（影响 SG3/触发检测/手动撤单检测，**设计待 ChatGPT 裁决**） |
| P1-B | 418 倒计时打印节流 | 进入立即通知 + 之后 60 秒/条 |
| P1-C | critical 邮件节流 | 状态转换式（首次 1 封 + 恢复 1 封） |
| P2 | TG Markdown 格式修复 | 通知系统自身不得产生"通知失败"噪音 |

**C5 当前状态处置建议（回应 ChatGPT 第十三节）**：同意暂缓推进。候选处置：保留 retries=1 禁盲重部分（无争议且是 P0 修复），回滚/禁用 Verify 自动路径直至 P0-1~P0-4 完成——具体回滚方式（revert commit / 热改 flag / 分支冻结）待裁决。
