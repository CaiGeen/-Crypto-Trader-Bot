# C4 / SG3-P1 设计草案（送审 ChatGPT）

> **生成时间**：2026-08-19 16:20 (GMT+8) / **v2 更新**：16:30（ChatGPT 评审通过，两处必修修正已落实）
> **基线**：commit `973b901`（远程 main 已同步）
> **性质**：设计评审稿——**评审通过前不修改代码、不写测试**
> **评审状态**：✅ **APPROVED — 可进入 TDD**（ChatGPT 2026-08-19 裁决，见附录八）
> **边界（ChatGPT 审定 + 本项目纪律）**：只做源码定位→事实核查→设计草案→交叉评审

---

## 一、任务定义

**目标**：把监控循环中 SL/TP 的"存在性判断"升级为"有效性判断"，落实原则——

> **"存在订单"与"订单有效"必须明确分离**：订单 ID 存在于 `open_orders_map` ≠ 保护已经成立。

**只验证三件事**（ChatGPT 边界锁定）：
1. 方向正确（side）
2. reduceOnly 正确（保护语义）
3. 数量 ≥ 批次已成交量

**不做的**：不进入 SG3 Phase 2 完整入场/恢复验证；不改变 SG2 的 delta 归属逻辑；不为了"有效性"重构监控循环；不校验 stopPrice（策略参数，用户可改）。

---

## 二、事实核查结论（973b901 源码逐行核实）

### 2.1 缺口确认：存在性检查的位置与形态

| 位置 | 代码 | 缺口 |
|------|------|------|
| L2525 | `if current_sl_id and (str(current_sl_id) not in open_orders_map) and has_entered_position:` | 订单**在** open_orders_map 时直接跳过，从不校验字段 |
| L2623 | `if tp_order_id and (str(tp_order_id) not in open_orders_map) and has_entered_position:` | 同上（TP 对称缺口） |

**结论**：只要 SL/TP 单"还在交易所挂着"，程序就默认它有效——即使方向反了、reduceOnly 丢了、数量缩水，程序一无所知。这正是 SG3-P1 要补的洞。

### 2.2 open_orders_map 构造（零新增 API 前提成立）

- L2015-2018：监控循环每轮一次 `fetch_open_orders(symbol)`，构建 `{str(ord['id']): ord}`，**后续所有存在性判断都用这份快照**
- SG3-P1 校验完全复用该快照 → **零新增 API** ✅
- L2525 / L2623 执行时 `open_orders_map` 仍在作用域内且为本轮最新值 ✅

### 2.3 ccxt 4.5.68 订单字段实测（关键！直接影响校验写法）

用 venv 实测 `ccxt.binanceusdm().parse_order()` 对三种 Binance 原始响应的解析结果：

| 字段 | 普通 STOP_MARKET（reduceOnly=true） | 用户手工一键止损（closePosition=true） | 无 reduceOnly 的 stop 单（风险单） |
|------|-------------------------------------|----------------------------------------|-----------------------------------|
| 顶层 `type` | `'market'`（**被归一化**，非 'STOP_MARKET'） | `'market'` | `'market'` |
| 顶层 `side` | `'sell'`（小写） | `'sell'` | `'sell'` |
| 顶层 `amount` | `0.01`（float） | **`None`**（origQty=0） | `0.01` |
| 顶层 `stopPrice` | `55000.0`（float） | `54000.0` | `52000.0` |
| 顶层 `reduceOnly` | **`None`（不映射！）** | `None` | `None` |
| 顶层 `positionSide` | **`None`（不映射！）** | `None` | `None` |
| `info.reduceOnly` | `'true'`（字符串） | `None`（无此字段） | `None`（无此字段） |
| `info.closePosition` | `None` | `'true'` | `None` |

**核查结论（影响设计的三条铁律）**：
1. **`reduceOnly`/`positionSide` 必须从 `ord['info']` 读，顶层读不到**（ccxt 4.5.68 不映射到顶层）
2. **不能校验 `type`**——ccxt 把 STOP_MARKET/TAKE_PROFIT_MARKET 归一化为 `'market'`，校验必误报
3. **closePosition 单 `amount=None`**——数量校验必须对 closePosition 单跳过

### 2.4 修复路径已存在（SG3-P1 零新增修复代码）

- `need_recover_sl` / `need_recover_tp` 置位后，L2748 分支已有完整"撤旧→挂新"逻辑：
  - SL：L2763-2791 先 fetch 旧单、cancel 旧单 → create 新单（含降级保护、失败熔断）
  - TP：L2934-2960 撤销旧单 → 挂新单
- **SG3-P1 只需新增"校验 + 置位"部分**，修复动作全部复用现有路径

### 2.5 user_modified 豁免机制已存在

- L2552-2554：`user_modified=True` 时 SL 被外部撤销 → **不自动补挂**（用户接管约定）
- L2735-2739：新层成交会重置 `user_modified=False`（重新回到程序管理）
- SG3-P1 校验失败时：`user_modified=True` → 豁免自动修复（尊重用户接管）

---

## 三、设计草案

### 3.1 校验语义（三项精确判定）

对 `open_orders_map[current_sl_id]`（或 tp）订单 `ord`，已知批次上下文（`side`=开仓方向 BUY/SELL、`is_hedge_mode`、`params_base['positionSide']`、`batch_filled_amount`）：

| # | 校验项 | 精确语义 | 失败含义 |
|---|--------|----------|----------|
| 1 | **方向** | `ord['side'] == expected_side`，其中 `expected_side = 'sell' if side=='BUY' else 'buy'`（SL/TP 同向） | 保护单方向反 → 触发即**反向开仓**（灾难） |
| 2 | **保护语义** | 单向模式：`str(ord['info'].get('reduceOnly')) == 'true'` **或** `str(ord['info'].get('closePosition')) == 'true'`；**Hedge 模式（修正版）：`side == expected_side` 已通过 且 `ord['info'].get('positionSide') == params_base['positionSide']`** —— 两个条件**必须同时成立**才判保护成立（`positionSide=LONG + side=BUY` 是增加 LONG 仓而非保护，方向条件不可省略） | reduceOnly 缺失 → 触发时可能开新仓（灾难）；positionSide 不匹配 → 保护错仓；Hedge 下方向+positionSide 任一不符 → 非保护 |
| 3 | **数量** | `ord['amount']` 非 None 时：`ord['amount'] >= batch_filled_amount * (1 - 0.001) - 1e-9`（相对容差 0.1%）；`ord['amount'] is None`（closePosition 单）→ 跳过（全仓平语义天然覆盖） | 保护覆盖不足 → 部分裸仓 |

**明确不校验**：
- `stopPrice`：用户可修改、策略阶梯可变（user_modified 场景天然兼容，不误报）
- `type`：ccxt 归一化为 `'market'`，校验必误报（2.3 实测）
- 超额数量（`amount > 已成交量`）：覆盖更足，不拒绝

### 3.2 触发与动作（Fail-Closed）

```
对 SL（L2525 块内）：
  if str(current_sl_id) in open_orders_map:      # 订单存在
      ord = open_orders_map[str(current_sl_id)]
      valid, reason = _check_protection_order_validity(ord, ...)
      if not valid:
          if user_modified:
              → 日志 + 一次性 TG 告警（不自动改单，尊重用户接管）
          else:
              → 日志 + 一次性 TG 告警（critical）
              → need_recover_sl = True            # 复用 L2748 撤旧→挂新
对 TP（L2623 块内）：对称逻辑 → need_recover_tp = True
```

- **告警节流**：同一 `(batch_id, order_id, reason)` 只发一次 TG；修复成功（该 id 从 open_orders_map 消失/被替换）后重置，允许下次异常再报
- **user_modified 豁免只豁免自动修复，不豁免告警**（用户要知道自己改出的问题）

### 3.3 新增代码形态（helper，便于单测与 AST 断言）

```python
def _check_protection_order_validity(self, ord, expected_side, is_hedge_mode,
                                     position_side, required_amount):
    """SG3-P1: 校验保护单（SL/TP）有效性。返回 (valid, reason)。
    只用 open_orders_map 已拉取的订单数据，零新增 API。"""
    # ① 方向
    if str(ord.get('side', '')).lower() != expected_side:
        return False, f"方向错误(期望{expected_side}，实际{ord.get('side')})"
    # ② 保护语义
    info = ord.get('info', {}) or {}
    if is_hedge_mode:
        if str(info.get('positionSide', '')).upper() != str(position_side).upper():
            return False, f"positionSide 不匹配(期望{position_side}，实际{info.get('positionSide')})"
    else:
        ro = str(info.get('reduceOnly') or '').lower()
        cp = str(info.get('closePosition') or '').lower()
        if ro != 'true' and cp != 'true':
            return False, "缺少保护语义(reduceOnly/closePosition 均非 true)"
    # ③ 数量（closePosition 单 amount=None → 跳过，全仓平语义覆盖）
    amount = ord.get('amount')
    if amount is not None:
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return False, f"数量字段异常({amount!r})"
        if amount < required_amount * (1 - 0.001) - 1e-9:
            return False, f"覆盖数量不足({amount} < {required_amount})"
    return True, ""
```

### 3.4 插入点（最小侵入，不重构循环）

- **SL**：L2525 `if` 块内新增 else/嵌套分支——订单在 open_orders_map 时执行校验
- **TP**：L2623 同构
- 不改变任何既有分支的走向；`need_recover_sl/tp` 置位后走既有 L2748 路径

---

## 四、边界锁定清单（对照 ChatGPT 约束逐条承诺）

| ChatGPT 约束 | 本设计如何满足 |
|--------------|----------------|
| 只利用监控循环已获取的 open_orders_map | ✅ 校验函数只读 `open_orders_map` 快照 |
| 零新增 API 请求 | ✅ 校验零 API；修复动作（撤旧挂新）走既有路径，本就存在 |
| 只验证方向/reduceOnly/数量 | ✅ 三项；数量对 closePosition 单跳过（字段不可得） |
| 不进入 SG3 Phase 2 | ✅ 不碰入场单/恢复验证 |
| 不改变 SG2 的 delta 归属逻辑 | ✅ SG2 只做存在性检查，SG3-P1 保障"存在即有效"，两者正交不互改 |
| 不重构监控循环 | ✅ 仅 L2525/L2623 块内新增分支 + 一个 helper |
| "存在"与"有效"分离 | ✅ 新增字段校验即分离 |

---

## 五、测试计划（实施阶段执行，本轮不做）

**测试规格 v2（已吸收 ChatGPT 5 组锁定矩阵 + 必修 2）**：

### A. Helper 纯语义测试（核心矩阵）

| 场景 | 预期 |
|------|------|
| 正确 side + reduceOnly + 足量 | valid |
| 错 side | invalid |
| 无 reduceOnly / closePosition | invalid |
| 数量不足 | invalid |
| 数量在 0.1% 容差内 | valid |
| 数量超过要求 | valid |
| amount=None + closePosition=true | valid |
| amount=None + 无 closePosition | 由保护语义先决定（不因 amount=None 直接判 fail） |
| hedge：正确 side + 正确 positionSide | valid |
| hedge：**错误 side + 正确 positionSide** | **invalid**（防"positionSide 匹配即保护"漏洞） |
| hedge：正确 side + 错误 positionSide | **invalid** |

### B. 零 API AST 断言

- `_check_protection_order_validity` 函数体内**不得出现任何 Call 到交易所/API 方法**（`fetch_*` / `create_order` / `cancel_order` 全禁）。纯读快照判断器，结构级断言锁死。

### C. SL 集成（核心链路）

- SL ∈ open_orders_map + invalid + user_modified=False → `need_recover_sl=True` → 走既有恢复链（撤旧→挂新）
- **C4 集成测试只证明到"恢复 flag 置位 + 既有恢复路径被触发"为止**；`create_order 网络失败/重复提交/幂等` 属 C5/SG4，不在 C4 范围内

### D. user_modified（必修 2 锁死）

- **`user_modified=True` 不改变 validity 判定**：invalid 仍然 invalid，测试必须断言 `valid == False` 而非"user_modified → valid"
- 只改变 invalid 后的动作：`need_recover_sl` **不置位**（不自动修复）+ **告警仍发送**（用户要知道自己改出的问题）
- 即：`检测结果：invalid / 修复策略：skip` 两个维度分开断言

### E. TP 对称测试

- TP invalid → `need_recover_tp=True`（防止只测 SL、TP 漏检）
- 复用与 SL 相同的 helper，测试锁定对称性

### 回归

- 现有 47 场景（9 文件）不破坏

### 测试纪律（ChatGPT 裁决）

- 不增加"忽略此告警"按钮（引入持久化/状态语义，超出 SG3-P1 范围，第一版保持自动节流）
- 告警节流键 `(batch_id, order_id, reason)`：同一订单同一原因只报一次；修复成功后重置

---

## 六、待 ChatGPT 评审的问题

1. **closePosition 单兼容判定**：单向模式下 `info.closePosition=='true'` 视为有效保护（跳过数量校验、不撤销）——合理吗？程序从不创建 closePosition 单，它只可能是用户手工一键止损。撤销它=破坏用户操作；保留它=保护语义成立。
2. **user_modified 豁免**：校验失败 + user_modified=True → 只告警不自动修复。与现有 L2552-2554 约定一致，是否同意？
3. **数量容差 0.1%**：SL/TP 挂单 amount 与 batch_filled_amount 同源（同一变量），理论零偏差；0.1% 容差是否足够/过宽？
4. ~~hedge 模式 positionSide 匹配即保护成立~~（**已修正**）：原表述不严谨。**修正后**：Hedge 模式 = `side == expected_side` **且** `positionSide == params_base['positionSide']` 同时成立。`positionSide=LONG + side=BUY` 是增加 LONG 仓（非保护），方向条件不可省略。helper 实现顺序（先 side 后 positionSide）本就正确，仅文档表述修正。
5. **告警节流**：同一 (batch_id, order_id, reason) 只报一次的方案是否够？是否需要在用户 TG 按钮上追加"忽略此告警"？
6. **不校验 stopPrice 的边界确认**：用户修改 SL 价格（user_modified 场景）时程序不介入——确认这不违背"无有效 SL 禁裸仓"？（价格错误≠无 SL，方向/保护/数量仍有效）
7. **SG2 联动确认**：SG3-P1 上线后，SG2 存在性检查成立的前提是"存在即有效"由监控循环保障——若监控循环异常退出（L3029 路径），SG2 仍会拒绝新信号吗？（会：SG2 查 open_orders 存在性，与 SG3-P1 无关）

---

## 七、与后续项的关系

- **C5/SG4**：SG3-P1 是"读侧校验"（验证已有保护单），C5 是"写侧幂等"（Create→Verify→Commit）。两者正交，SG3-P1 的 `_check_protection_order_validity` 可被 C5 复用为 verify 环节的种子函数
- **SG3 Phase 2**：入场单/恢复验证留待后续单独评审，不在 C4 范围内

---

## 八、ChatGPT 评审结论（2026-08-19，已闭环）

> **裁决：✅ APPROVED — 可进入 TDD**

### 通过项（全票确认）

- 三项核心校验（方向 / 保护语义 / 数量）✅
- closePosition → 跳过数量校验（程序从不创建 closePosition 单，撤销用户手工一键止损=破坏用户接管）✅
- user_modified 只豁免自动修复、不豁免告警 ✅
- 0.1% 数量容差（目标=不能少保护，非精确相等；amount>required 不判无效）✅
- stopPrice 不校验（保护结构 vs 保护参数分层；避免侵入 user_modified 协议）✅
- SG2 与 SG3-P1 正交（SG2=signal-time existence gate，SG3-P1=monitor-time validity enforcement）✅
- 告警节流键 `(batch_id, order_id, reason)` ✅
- 修复动作复用既有 L2748 恢复路径（最小侵入）✅
- helper 纯判断职责（不知道 user_modified/need_recover/TG/下单，策略动作留在监控循环）✅
- 零新增 API、不进入 Phase 2、不提前侵入 C5 ✅

### 必修修正（2 项，均已落实到本文档）

| # | 修正 | 落点 |
|---|------|------|
| **必修 1** | Hedge 模式表述改为：`side == expected_side` **且** `positionSide == params_base['positionSide']` 同时成立——不能写成"positionSide 匹配即保护成立"（`LONG+BUY` 是加仓非保护）。helper 代码顺序本就正确，仅文档表述修正 | §3.1 表格行 2、评审问题 4 |
| **必修 2** | 测试规格锁死：`user_modified=True` **不改变 validity 判定**（invalid 仍 invalid），只改变 invalid 后动作（告警、不自动恢复）。测试断言两维度分离：`检测结果: invalid / 修复策略: skip` | §五 测试规格 v2 组 D |

### 明确拒绝（第一版不做）

- **"忽略此告警"按钮**：引入持久化/状态语义（忽略状态存哪/重启是否继续/换 ID 是否重报/修复后何时解除），超出 SG3-P1 范围
- **C4 集成测试验证 C5 内容**：只证明 invalid→恢复 flag→既有恢复路径被触发；create_order 网络失败/重复提交/幂等验证属 C5/SG4

### 进入 TDD 的前置

1. ✅ 两处必修修正已落实
2. 下一步：**先写测试（红）→ 确认 → 实施 C4（helper + L2525/L2623 插入点）→ 测试绿 → 回归 47 场景**
3. 实施前需用户最终确认
