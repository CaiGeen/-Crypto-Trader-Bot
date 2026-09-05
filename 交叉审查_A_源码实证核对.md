# 交叉审查 A：送审文档 × 生产源码 实证核对

> 审查对象：`事故_市价平仓-4061_精确diff_送审ChatGPT.md`（v2，1114 行）
> 生产源码：`trader_260725.py`（7858 行，HEAD `e953d79`，`git diff --stat HEAD` 为空）
> 提议代码：`G:/tmp/new_helpers_after.py`（82 行）
> 审查方式：AST 解析 + 逐行精确串匹配 + 源码原样抽取后离线执行
> 审查性质：**只读**。全程未修改 `G:/my-crypto-bot` 下任何文件，未调用任何交易所 API。
> 复核脚本（均在 `G:/tmp/`）：`verify_doc.py`、`verify_linenums.py`、`census_create_order.py`、
> `prove_dead_code.py`、`check_after_code.py`、`verify_uniqueness.py`

---

## 核对结论速览

| 大项 | 结论 |
|---|---|
| ① before 摘录行号与内容 | **32 项断言，28 通过，4 项不一致（1 致命 / 3 轻微）** |
| ② create_order 14 处 / 13+1 说法 | ✅ **成立**（独立统计复核） |
| ③ `_MERGE_RATCHET_BOOL_FIELDS` 定义 | ✅ **成立**（L41-42，内容逐字一致） |
| ④ 两处回滚是死代码 | ✅ **成立**（源码原样抽取实证） |
| ⑤ after 代码可套用性 | ✅ **无阻断问题** |

**最终结论：存在 1 处致命失真 + 3 处轻微偏差，送审前必须修正（至少致命项）。**

---

## 一、before 摘录逐条核对（32 项断言）

### 1.1 逐条明细

| # | 文档标注行号 | 源码实际行号 | 逐字一致 | 判定 |
|---|---|---|---|---|
| 1 | 改动1 BEFORE **`L7004-7046`**（§二标题 + BEFORE 小标题） | **`L7003-7045`** | ✅ 内容 43 行逐字一致 | ⚠️ 轻微（整体 +1 偏移） |
| 2 | 改动1 位置 **`L7005-7046`**（§一改动总表） | **`L7003-7045`** | 同上 | ⚠️ 轻微（与 #1 互相矛盾） |
| 3 | create_order 硬编码 **`L7019-7027`** | `order = self._safe_api_call(` **L7019** … `)` **L7027** | ✅ 逐字一致 | ✅ 通过 |
| 4 | `params={'reduceOnly': True}` | **L7025** | ✅ 逐字一致 | ✅ 通过 |
| 5 | 市价撤 ENTRY 块 **`L7005-7013`** | 注释 L7005 / 代码 L7006-7013（`pass`=L7013） | ✅ 逐字一致 | ✅ 通过 |
| 6 | 市价回滚 **`L7128-7136`** | **L7128-7136** | ✅ 逐字一致 | ✅ 通过 |
| 7 | 限价撤 ENTRY 块 **`L7541-7549`** | **`L7540-7549`**（`try:` 在 L7540） | ✅ 内容 10 行逐字一致 | ⚠️ 轻微（起始 -1） |
| 8 | 限价参数构造 **`L7578-7582`** | **L7578-7582** | ✅ 逐字一致 | ✅ 通过 |
| 9 | 限价 `params_base.copy()` **`L7578`** | **L7578** | ✅ 逐字一致 | ✅ 通过 |
| 10 | 限价回滚 **`L7668-7676`** | **L7668-7676** | ✅ 逐字一致 | ✅ 通过 |
| 11 | `save_batch_state` 定义 **`L1389`** | **L1389** | ✅ 逐字一致 | ✅ 通过 |
| 12 | merge 调用 **`L1425`** | **L1425** | ✅ 逐字一致 | ✅ 通过（措辞见 1.2） |
| 13 | `_merge_batch_state` 定义 **`L1614`** | **L1614** | ✅ 逐字一致 | ✅ 通过 |
| 14 | 棘轮逻辑 **`L1621-1629`** | **L1621-1629** | ✅ 逐字一致 | ✅ 通过 |
| 15 | P0 冻结 `continue` **`L5244-5248`** | **L5244-5248** | ✅ 逐字一致 | ✅ 通过 |
| 16 | 冻结点之后起点 **`L5250`** | **L5250** | ✅（`sl_triggered = False`） | ✅ 通过 |
| 17 | SL 缺失补挂 **`L5257+`** | **L5257** | ✅（`if not current_sl_id and …`） | ✅ 通过 |
| 18 | SL 在场性校验 **`L5281+`** | **L5281** | ✅ | ✅ 通过 |
| 19 | `_get_current_position_amt` 定义 **`L2656`** | **L2656** | ✅ 位置正确 | ✅ 通过（代码块见 1.2） |
| 20 | `_MERGE_RATCHET_BOOL_FIELDS` **`L41-42`** | **L41-42** | ✅ 逐字一致 | ✅ 通过 |
| 21 | `params_base` **`L2997-3011`** | 范围 **L2997-3011** 正确 | ⚠️ 内容为 4 行压缩摘要 | ⚠️ 轻微 |
| 22 | 非 hedge 才加 reduceOnly **`L2292-2293`** | **L2292-2293** | ✅ 逐字一致 | ✅ 通过 |
| 23 | `_safe_api_call` **`L1160`** | **L1160** | ✅ 定义位置正确 | ✅ 通过 |
| 24 | `fetch_open_orders` 用法 **`L2759`** | **L2759-2759/2760** | ✅ 逐字一致 | ✅ 通过 |
| 25 | `fetch_open_orders` 用法 **`L7289`** | **L7289-7290** | ✅ 逐字一致 | ✅ 通过 |
| 26 | `fetch_open_orders` 用法 **`L7410`** | **L7410-7411** | ✅ 逐字一致 | ✅ 通过 |
| 27 | 插入点 **`L6945` 附近** | L6945 = 段落注释 / **L6947 = `def close_position_market`** | ✅ | ✅ 通过 |
| 28 | 撤 ENTRY 插入点 **`L7045` 之后** | L7045=`pass`，L7047=`# 获取实际成交价格` | ✅ | ✅ 通过 |
| 29 | `settled_by_limit_close=True` **`L7745`** | **L7745** | ✅ 逐字一致 | ✅ 通过 |
| 30 | `_converge_batch_orders_before_clear` 存在 | **L7264** | ✅ | ✅ 通过 |
| 31 | **`_get_current_position_amt` 的 4 个调用方 `L1807`/`L1910`/`L2660`/`L6338`** | **实际 L2934 / L3521 / L4871 / L7305** | ❌ **4/4 行号全错** | 🔴 **致命** |
| 32 | §7.1 `_get_current_position_amt` 代码块 | L2656-2676 | ⚠️ 省略了 `for attempt in range(retries)` 重试循环与 `except → return None` | ⚠️ 轻微 |

### 1.2 不一致项详述

#### 🔴 致命 ① — `_get_current_position_amt` 的 4 个调用方行号全部错误（md 第 331-332 行）

文档原文：

> 新 helper 保留它的匹配逻辑（`pos['symbol']` / `info['symbol']` 双路匹配、`contracts` / `positionAmt` 双路取值），
> 但**只把它当读数原语**，判定权交给 delta。`_get_current_position_amt` 的其它 **4 个调用方
> （L1807 / L1910 / L2660 / L6338 周边）**不在本次 diff 范围内，保持不动。

源码实证（全库 `_get_current_position_amt` 仅 5 处出现）：

```
2656:  def _get_current_position_amt(...)          ← 定义
2934:  current_pos = self._get_current_position_amt(symbol, is_hedge_mode=False, side=side)
3521:  pos_amt = self._get_current_position_amt(symbol, False) or 0.0
4871:  current_actual_position = self._get_current_position_amt(symbol, is_hedge_mode, side=side)
7305:  pos_amt = self._get_current_position_amt(symbol, bool(b_data.get('is_hedge_mode')), side=_side)
```

被文档点名的 4 行实际内容：

| 文档行号 | 源码实际内容 | 是否为 `_get_current_position_amt` 调用 |
|---|---|---|
| L1807 | `positions = self._safe_api_call(self.exchange.fetch_positions)` | ❌ 否，直接 `fetch_positions` |
| L1910 | `positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])` | ❌ 否，直接 `fetch_positions` |
| L2660 | `positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])` | ❌ 否，**这是该函数自己的函数体** |
| L6338 | `positions = self._safe_api_call(self.exchange.fetch_positions, [symbol])` | ❌ 否，直接 `fetch_positions` |

**性质**：4 个行号无一命中，且其中 L2660 根本不是调用方而是被调函数自身。调用方**总数 4 这个数字恰好正确**，
但**每一个行号都指向了完全不同的代码**。外部复审者若按 L1807 / L1910 / L6338 去核对，会看到三处
`fetch_positions` 直调——与文档所述"复用 `_get_current_position_amt` 的调用方"完全对不上，
足以让复审者怀疑整份文档的行号体系不可信。

**修正建议**：改为 `L2934 / L3521 / L4871 / L7305`。

---

#### ⚠️ 轻微 ② — 改动1 BEFORE 区间标注 +1 偏移，且文档内部两处标注互相矛盾

- §二标题与 BEFORE 小标题写 **`L7004-7046`**
- §一改动总表写 **`L7005-7046`**
- 源码实际：文档粘贴的 43 行内容**精确唯一匹配 `L7003-7045`**（`verify_uniqueness.py` 验证：该 43 行块全库仅出现 1 次）

即：文档为同一段代码给出了两个互相不同的区间，且都与真实位置差 1 行。
内容本身逐字无误，属标注偏移。

**修正建议**：统一改为 `L7003-7045`。

---

#### ⚠️ 轻微 ③ — 限价撤 ENTRY 块起始行 -1

文档 §三标注 `L7541-7549`（9 行），但粘贴内容首行是 `        try:`，该行在 **L7540**。
10 行块精确唯一匹配 **L7540-7549**（全库仅 1 次）。

**修正建议**：改为 `L7540-7549`。

---

#### ⚠️ 轻微 ④ — 两处代码块为压缩摘要，未声明

1. **`params_base`（文档称 L2997-3011）** —— 文档贴的 4 行：
   ```python
   params_base = {}
   if hedge: params_base['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'
   params_base['workingType'] = 'MARK_PRICE'
   params_base['leverage'] = signal.leverage
   ```
   源码 L2997-3011 实际为 15 行，含 `is_hedge_mode = False`、
   `fapiPrivateGetPositionSideDual` 探测、try/except 回退。行号范围正确，但**内容是压缩改写**。
   其中 `if hedge:` 实为 `if res and res.get('dualSidePosition'):`。

2. **`_get_current_position_amt`（§7.1）** —— 文档贴的 9 行省略了 `for attempt in range(retries):`
   重试循环与 `except → return None`（L2671-2676）。省略的这段恰好决定了该函数
   **在重试耗尽后返回 `None` 而非 `0.0`** —— 与文档论述的"假确认陷阱"（返回 0.0）是两条不同路径。

**影响**：不误导行号定位，但会让复审者误以为看到的是原文。**建议在两处加"（以下为压缩示意，非原文）"标注。**

---

### 1.3 附带发现：L1425 的 merge 并非"无条件"

任务书转述为"其内部无条件 merge"。文档本身未出现"无条件"字样（grep 计数 0），
但需澄清源码事实：L1425 的 merge 调用**受 L1424 保护**：

```python
1423|                     existing = all_states[symbol].get(batch_id)
1424|                     if isinstance(existing, dict) and existing:
1425|                         batch_data = self._merge_batch_state(existing, batch_data)
```

即：批次在磁盘上不存在或为空 dict 时**不 merge**。在本次回滚场景中批次必然已在磁盘上，
故 merge 确实会执行，**不影响文档的任何实质性结论**，但"无条件"这一措辞若出现在文档中则不属实。

---

## 二、create_order 全库统计独立复核（文档称 14 处 / 13 派生 / 1 硬编码）

统计方法：AST 遍历 `Attribute(attr='create_order')`，归属到最小包围函数，
再回溯 `params` 实参的赋值链（最多 3 跳）判定是否源自 `params_base`。

> 注意：`create_order` 均以**函数引用**形式传给 `self._safe_api_call(...)`，非直接调用，
> 故需按 Attribute 引用点而非 Call 节点统计（首次统计误得 0，已修正）。

| # | 行号 | 所属函数 | params 实参 | params 来源 | 判定 |
|---|---|---|---|---|---|
| 1 | 2158 | `update_batch_tp` | `tp_params` | L2127 `target_b_data['params_base'].copy()` | ✅ 派生 |
| 2 | 2321 | `update_batch_sl` | `sl_params` | L2290 `target_b_data['params_base'].copy()` | ✅ 派生 |
| 3 | 2541 | `_update_sl_no_validation` | `sl_params` | L2481 `b_data['params_base'].copy()` | ✅ 派生 |
| 4 | 3164 | `execute_signal` | `order_params` | L3159 `params_base.copy()` | ✅ 派生 |
| 5 | 5048 | `_start_monitoring` | `sl_params` | L5009 `params_base.copy()` | ✅ 派生 |
| 6 | 5128 | `_start_monitoring` | `tp_params` | L5089 `params_base.copy()` | ✅ 派生 |
| 7 | 5768 | `_start_monitoring` | `sl_params` | L5710 `params_base.copy()` | ✅ 派生 |
| 8 | 5931 | `_start_monitoring` | `recovery_params` | L5888 `params_base.copy()` | ✅ 派生 |
| 9 | 6109 | `_start_monitoring` | `tp_params` | L6058 `params_base.copy()` | ✅ 派生 |
| 10 | 6430 | `_place_prepared_orders_immediately` | `sl_params['params']` | L6386 ← `layer_sl_params[idx]`，其元素由 L3186 / L4469 `params_base` 构造 | ✅ 派生 |
| 11 | 6590 | `_place_prepared_orders_immediately` | `sl_params` | L6537 `params_base.copy()` | ✅ 派生 |
| 12 | 6755 | `_place_prepared_orders_immediately` | `tp_params['params']` | L6749 ← `prepared_tp_params`，由 L3219 / L4490 `params_base` 构造 | ✅ 派生 |
| 13 | 7585 | `close_position_limit` | `order_params` | L7578 `target_b_data['params_base'].copy()` | ✅ 派生 |
| 14 | **7020** | **`close_position_market`** | **`{'reduceOnly': True}`** | **字面量硬编码** | 🔴 **唯一硬编码** |

**统计结果**

- `create_order` 引用点总数：**14**（另有 3 处 `create_order` 非调用点：L3369 / L3629 docstring、L7576 注释）
- params 派生自 `params_base`：**13**
- 硬编码字面量：**1**（L7020，`close_position_market`）

✅ **文档「14 处 / 13 处派生 / 只有市价平仓硬编码」的说法完全成立。**
唯一硬编码点即本次 -4061 事故根因，且限价平仓（L7578-7582）确为"派生 + 按 `is_hedge_mode` 分支"
的正确范式 —— 文档「改动 1 复用限价平仓已被实盘验证的参数构造」这一论据成立。

---

## 三、`_MERGE_RATCHET_BOOL_FIELDS` 核对

文档称：「`_MERGE_RATCHET_BOOL_FIELDS`（L41-42）= `('pending_close', 'is_programmatic_cancel', 'settled_by_limit_close')`」

源码 L38-42 原文：

```python
  38| # C1 字段级 merge 分类字段表（v2 §5.1 + v3 §5 七类）：
  39| #   A 棘轮（close_phase 专列 int max）/ G user_modified OR / B 单调账本 /
  40| #   C registry 逐 identity / D id 镜像 / E 静态幂等 / F 簿记最新者胜（默认）。
  41| _MERGE_RATCHET_BOOL_FIELDS = ('pending_close', 'is_programmatic_cancel',
  42|                               'settled_by_limit_close')
```

✅ **位置（L41-42）与内容（三元组、顺序一致）均逐字正确。** 全库仅此一处定义（无重定义/覆盖）。

---

## 四、「两处回滚是死代码」独立推导

### 4.1 前置事实核对

**回滚代码用的是赋值 `False`，不是 `pop()`。**

市价回滚 L7132-7134：
```python
rollback_b_data['is_programmatic_cancel'] = False
rollback_b_data['pending_close'] = False
rollback_b_data['close_phase'] = 0  # P0 Batch A：1→0 合法回滚（平仓单未创建）
self.save_batch_state(target_symbol, batch_id, rollback_b_data)
```
限价回滚 L7672-7675 逐字相同（仅 L7676 的 print 文案不同）。

**`load_all_states()`（L1306）每次调用都 `json.load()` 重新解析，无缓存** ——
这是推导成立的关键：回滚处拿到的 `rollback_b_data`（对象 D）与 `save_batch_state` 内部
重新读到的 `existing`（对象 E）是**两个不同对象**，磁盘值仍为 True/True/1。

### 4.2 推导

`save_batch_state` → `_merge_batch_state(disk=E, snap=D)`：

1. `merged = dict(snap)` → 三字段为 False/False/0
2. `close_phase`：`max(int(disk.get('close_phase',0) or 0), int(snap.get('close_phase',0) or 0))`
   = `max(1, 0)` = **1** → 回退失败
3. 布尔棘轮 L1627-1629：`if disk.get(f) and not snap.get(f): merged[f] = disk[f]`
   - `pending_close`：disk=True, snap=False → `merged=True` → 回退失败
   - `is_programmatic_cancel`：disk=True, snap=False → `merged=True` → 回退失败

### 4.3 实证（`prove_dead_code.py`：AST 从源码原样抽取 `_merge_batch_state` + 常量后执行）

```
--- 市价平仓 rollback（L7128-7136）（回滚方式=assign）---
  磁盘(disk) : close_phase=1, pending_close=True, is_programmatic_cancel=True
  快照(snap) : close_phase=0, pending_close=False, is_programmatic_cancel=False
  落盘(merged): close_phase=1, pending_close=True, is_programmatic_cancel=True
  >>> 回滚是否真正生效: ❌ 未生效（死代码）

--- 限价平仓 rollback（L7668-7676）（回滚方式=assign）---
  （同上）>>> 回滚是否真正生效: ❌ 未生效（死代码）
```

✅ **文档「两处回滚都是死代码，布尔棘轮会让磁盘 True 覆盖快照 False」的结论成立。**

### 4.4 附加回答：`pop()` 在 merge 语义下是否真的无效？

**无效，与赋值 `False` 等价。** 实证：

```
--- 假设改用 pop() 的对照（回滚方式=pop）---
  快照(snap) : close_phase=None, pending_close=None, is_programmatic_cancel=None
  落盘(merged): close_phase=1, pending_close=True, is_programmatic_cancel=True
  >>> 回滚是否真正生效: ❌ 未生效（死代码）
```

原因（逐字段）：

| 方式 | `disk.get('pending_close')` | `snap.get('pending_close')` | 棘轮条件 `disk.get(f) and not snap.get(f)` |
|---|---|---|---|
| 赋值 False | `True` | `False` | **True → 棘轮触发** |
| `pop()` | `True` | `None`（键缺失） | **True → 棘轮触发** |

因为 `not None` 同样为 `True`，缺失键与显式 `False` 在棘轮条件上不可区分，
`merged[f] = disk[f]` 照样把磁盘旧值写回。**想靠 `pop()` 绕过棘轮是无效的。**

---

## 五、after 代码可套用性审查

### 5.1 `G:/tmp/new_helpers_after.py`

| 检查项 | 结果 |
|---|---|
| `py_compile` 语法 | ✅ 通过 |
| 定义方法 | `_read_position_amt` / `_confirm_position_reduced` |
| `time` 是否可用 | ✅ `trader_260725.py` L7 `import time`，全库无局部遮蔽（grep `time =` 无命中） |
| `_safe_api_call` 是否真实方法 | ✅ L1160 `def _safe_api_call(self, func, *args, retries=5, delay=2, auth_probe=False, **kwargs)` |
| 调用形态与签名匹配 | ✅ `self._safe_api_call(self.exchange.fetch_positions, [symbol])` → `func`=实参0，`[symbol]` 进 `*args` |
| 与生产类已有方法命名冲突 | ✅ 无（`_read_position_amt` / `_confirm_position_reduced` 全库 0 命中） |
| 与 `_cancel_and_verify_entry_orders` 冲突 | ✅ 无（全库 0 命中） |

**功能性冒烟**（mock 交易所，零网络，注入真实类体后执行）：

| 场景 | 期望 | 实测 |
|---|---|---|
| S1 读取有仓位 | 0.001 | ✅ 0.001 |
| S2 读取空仓位 | 0.0 | ✅ 0.0 |
| S5 方向传错（陷阱） | 0.0 | ✅ 0.0 |
| S4 平仓后连续读失败 | False | ✅ 未通过（Fail-Closed） |
| S3 多批次 delta（0.002→0.001，平 0.001） | True | ✅ True |
| S1 单批全平（0.001→0） | True | ✅ True |

**缩进层级**：两个方法均为 4 空格（类方法层），与 L6947 `def close_position_market` 所在层级一致，
可直接粘贴到 L6945 附近（段落注释后、L6947 之前）。

**未 import 模块**：无。仅用到 `time`（模块级已导入）与 `self._safe_api_call`（同类方法）。

**变量引用**：`_confirm_position_reduced` 内部调用 `self._read_position_amt` —— 两者同批次新增，无前向引用问题。

### 5.2 文档中其余 after 片段

- **改动1 after**：`pos_before` / `close_position_confirmed` / `_detail` / `order_params` 均为新增局部变量，
  无未定义引用；`side`（L6967）、`last_filled_count`（L6964）、`target_symbol` / `batch_id` / `target_b_data`
  均在 `close_position_market` 作用域内已定义 ✅
- **`target_b_data['params_base']`** 用直接下标而非 `.get()` —— 与限价平仓 L7578 既有写法一致，
  非本次新增风险（若 `params_base` 缺失会 KeyError，但限价路径同样如此，属既有契约）
- **改动4 after**：`_ROLLBACK_RATCHET_FIELDS` 为函数内局部变量，与模块级 `_MERGE_RATCHET_BOOL_FIELDS`
  不冲突；AST 未发现语法问题
- **改动4 after 实证**（`check_after_code.py`）：
  ```
  allow=True  : close_phase=0 pending_close=False is_programmatic_cancel=False settled_by_limit_close=True
  → 回滚生效且结算事实未降级: ✅
  allow=False : close_phase=1 pending_close=True  is_programmatic_cancel=True  settled_by_limit_close=True
  ```
  即：受控通道确实使回滚生效，`settled_by_limit_close` 保持单向 ✅ 与文档 §改动4 声明一致
- **改动5b after** 引用的 `rollback_b_data.get('entry_orders')` 与 `last_filled_count` 均在作用域内 ✅

---

## 六、最终结论

| 类别 | 数量 | 明细 |
|---|---|---|
| 核对项总数 | **32** | 第 1 项 32 条行号/内容断言 |
| 通过 | **28** | — |
| 不一致 | **4** | 致命 1 / 轻微 3 |

**致命失真（送审前必须修正）**

1. `_get_current_position_amt` 的 4 个调用方行号 **L1807 / L1910 / L2660 / L6338 全部错误**，
   真实调用方为 **L2934 / L3521 / L4871 / L7305**。其中 L2660 甚至不在调用方之列（是该函数自身函数体）。
   文档 md 第 331-332 行。

**轻微偏差（建议修正）**

2. 改动1 BEFORE 区间标注 `L7004-7046`（§二）与 `L7005-7046`（§一表格）互相矛盾，
   真实位置 **`L7003-7045`**。
3. 限价撤 ENTRY 块标注 `L7541-7549`，真实为 **`L7540-7549`**（粘贴内容含 L7540 的 `try:`）。
4. `params_base`（L2997-3011）与 §7.1 `_get_current_position_amt` 两处代码块为**压缩摘要非原文**，
   建议显式标注，避免复审者误当逐字原文。

**结论：存在 1 处致命失真（含 4 个错误行号）+ 3 处轻微偏差，送审前必须修正。**

修正致命项与 2 处轻微行号偏移后，文档其余全部摘录与论断（14/13/1 的 create_order 统计、
`_MERGE_RATCHET_BOOL_FIELDS` 定义、两处回滚为死代码 —— 含 `pop()` 同样无效的推论、
after 代码的可套用性）均经源码实证成立，可送审。

---

## 附：审查过程副作用声明

- `G:/my-crypto-bot` 下所有文件**零改动**（复核 `git status --porcelain -- trader_260725.py` 为空，
  `git diff --stat HEAD` 为空，HEAD 仍 `e953d79`）
- 本报告为**本次审查唯一新增**的 `G:/my-crypto-bot` 下文件
- 临时脚本全部位于 `G:/tmp/`：`verify_doc.py`、`verify_linenums.py`、`census_create_order.py`、
  `prove_dead_code.py`、`check_after_code.py`、`verify_uniqueness.py`
- 全程零交易所 API 调用（mock 对象驱动）
