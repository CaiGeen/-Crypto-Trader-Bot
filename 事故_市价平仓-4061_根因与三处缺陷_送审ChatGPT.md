# 🚨 实盘事故：市价平仓 -4061 —— 根因链与三处缺陷

> 发生时间：2026-08-29 19:32（北京时间）
> 触发方式：TG 按钮「🚀 市价平仓」→ `close_market_{batch_id}` → `trader.close_position_market()`
> 影响批次：`batch_20260829_155343_cfdf77`（BTCUSDT，long 0.001 @77692.6）
> 取证方式：源码 Grep/Read + 只读交易所核对（未发任何交易指令）
> **当前未改动任何生产代码**（`git diff --stat HEAD` 为空）

---

## 零、当前实际状态（只读取证，19:36）

| 项目 | 状态 |
|---|---|
| 持仓 | long **0.001 BTC** @77692.6 — **仍未平** |
| SL `3000002163739625` | ✅ **在交易所**（75001） |
| TP `3000002163739660` | ✅ **在交易所**（80000） |
| 批次1 四层 ENTRY | ✅ 全部 intact |
| 交易所条件单总数 | 6（**无孤儿单、无重复单**） |
| 批次2 `close_phase` | **1（卡死）**，19:32:50 起持续 3+ 分钟不变 |
| 批次2 `pending_close` / `is_programmatic_cancel` | True / True（**回滚未生效**） |
| 批次2 第2层加仓单 `3000002163649920` | ❌ **已被撤销且未恢复** |

**结论：仓位仍有交易所侧 SL/TP 保护，无即时资金损失。但批次已进入「永久冻结」状态，需人工介入。**

---

## 一、P0-A：市价平仓缺少 `positionSide`（直接原因）

```python
# trader_260725.py L7019-7027  close_position_market()
order = self._safe_api_call(
    self.exchange.create_order,
    symbol=target_symbol, type='MARKET', side=close_side,
    amount=current_filled_amount,
    params={'reduceOnly': True},        # ❌ 硬编码，无 positionSide
    retries=1)
```

账户为**双向持仓模式**（账本 `is_hedge_mode = True`，启动时日志
`💡 检测到账户为 [双向持仓模式]，方向: LONG`）。
双向持仓下 Binance 要求显式 `positionSide`；缺省为 `BOTH`，被拒：

```
-4061  Order's position side does not match user's setting.
```

### 对照组：限价平仓是**对的**

```python
# L7576-7582  _place_limit_close()
order_params = target_b_data['params_base'].copy()
if target_b_data.get('is_hedge_mode', False):
    order_params['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'
else:
    order_params['reduceOnly'] = True
```

**这就是 16:30 那次「最优价平仓」能成功、而这次市价平仓失败的原因。**

### 影响范围：全库唯一漏网路径

全库 14 处 `create_order` 中，13 处的 params 均派生自 `params_base`
（账本实测 `params_base = {'positionSide': 'LONG', 'workingType': 'MARK_PRICE', 'leverage': 100}`，
已含 positionSide）。**只有 `close_position_market` L7019-7027 是硬编码。**

---

## 二、P0-B：回滚结构性失效 —— 比 -4061 更严重

失败后程序打印了：

```
🔄 平仓失败回滚：已清除 is_programmatic_cancel/pending_close/close_phase，监控线程恢复保护
```

**但状态根本没被清除。** 实测（19:36:03 / 19:36:11 / 19:36:19 三次采样）：

```
close_phase= 1  pending= True  prog= True  mtime= 19:32:50
```

### 根因：`save_batch_state` 的棘轮 merge 吃掉了回滚

```python
# L1389 save_batch_state() —— 无任何绕过参数
existing = all_states[symbol].get(batch_id)
if isinstance(existing, dict) and existing:
    batch_data = self._merge_batch_state(existing, batch_data)   # ← 无条件 merge
```

```python
# L1621-1629  _merge_batch_state()
merged['close_phase'] = max(int(disk.get('close_phase', 0) or 0),
                            int(snap.get('close_phase', 0) or 0))     # max(1, 0) = 1
for f in _MERGE_RATCHET_BOOL_FIELDS:      # ('pending_close','is_programmatic_cancel','settled_by_limit_close')
    if disk.get(f) and not snap.get(f):
        merged[f] = disk[f]                                            # 磁盘 True 快照 False → 保留 True
```

回滚代码 L7128-7135 是「读最新→改 False→写回」，但写入时被 merge 判为
「快照想降级安全面」→ **强制保留磁盘 True**。**回滚对这三个字段恒为 no-op，却打印成功。**

> 注意：`target_b_data.pop('is_programmatic_cancel', None)`（L6891）同样无效 ——
> 键缺失时 `snap.get(f)` 为 None，`not None` 为真，仍然保留磁盘 True。

### 受影响的两处回滚（都是死代码）

| 位置 | 函数 |
|---|---|
| L7128-7135 | `close_position_market` 市价平仓失败回滚 |
| L7664-7676 | `_place_limit_close` 挂限价单失败回滚 |

### 后果链

```
一次失败的平仓
   ↓
close_phase=1 / pending_close=True 永久留存
   ↓
监控线程 L5244-5248：
   if close_phase >= 1 or pending_close: print("🧊 [P0 冻结]"); continue
   ↓
【全部 SL/TP 补挂与维护被跳过】
   ↓
且重启不修复 —— bot_runner 无任何 close_phase 启动自愈（已 Grep 确认）
```

**即：只要平仓失败一次，该批次永久失去程序侧保护单维护，重启也无法恢复。**

---

## 三、P1-C：失败前已撤的开仓单不回滚

```python
# L7005-7013（在 try 内、create_order 之前）
entry_orders = target_b_data.get('entry_orders', [])
for idx, order_id in enumerate(entry_orders):
    if idx >= last_filled_count:
        try:
            self._safe_api_call(self.exchange.cancel_order, order_id, ...)
```

设计意图（成功平仓时应撤未成交层）本身合理，但**失败回滚 L7128-7135 不恢复这些单**。

实测后果：批次2 第2层加仓单 `3000002163649920` 已消失，策略从 2 层退化为 1 层，
且无告警、无自动补挂。

---

## 四、修复方案（**待确认，未实施**）

### 修复 1：市价平仓 params 对齐限价平仓

```python
close_side = 'sell' if side == 'BUY' else 'buy'
order_params = target_b_data.get('params_base', {}).copy()
if target_b_data.get('is_hedge_mode', False):
    order_params['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'
else:
    order_params['reduceOnly'] = True

order = self._safe_api_call(
    self.exchange.create_order, symbol=target_symbol, type='MARKET',
    side=close_side, amount=current_filled_amount,
    params=order_params, retries=1)
```

与 L7576-7582 完全对称；`params_base` 含 `workingType`/`leverage`，
限价平仓路径已实证对 MARKET/LIMIT 均无副作用。

### 修复 2：给回滚一条能真正清标的写入通道

棘轮的设计目的（防陈旧快照降级安全面）是对的，**不应拆除**。
应当为「显式回滚」提供受控口子，例如：

- `save_batch_state(..., merge=False)` 全量替换，或
- `save_batch_state(..., force_fields=('close_phase','pending_close','is_programmatic_cancel'))`

并**仅在两处回滚点使用**（L7135 / L7675），不得扩散。

⚠️ 需 ChatGPT 裁定：口子本身有被滥用的风险（等于给安全棘轮开了一道门），
是否加白名单/审计日志/调用点 AST 守卫。

### 修复 3：平仓失败时恢复已撤开仓单

可选两案：
- (a) 把撤开仓单挪到 `create_order` 成功之后（与「先平仓、成功后再撤 SL/TP」的既有修复同构）
- (b) 保留顺序，在回滚分支补挂已撤的 entry

倾向 (a)：与 L7015-7017 已有注释的设计哲学一致，且改动面更小。

---

## 五、当前批次的处置选项（**需你决策**）

仓位有交易所侧 SL/TP，不急；但冻结状态需解除，否则保护单一旦成交/失效，
程序不会结算也不会补挂 —— 在双向持仓下，孤儿 SL 触发有开反向仓的风险。

> **用户已决策（2026-08-29）：暂不动批次2，先送 ChatGPT 裁定。**
> 依据：仓位有交易所侧 SL/TP（75001 / 80000），当前价约 77600，距两侧均远，
> 无即时触发风险，有时间走完审查流程。
> 冻结本身的风险窗口 = 「SL/TP 成交或被撤时程序不结算、不补挂」。

| 方案 | 操作 | 评价 |
|---|---|---|
| **A. 用限价平仓收掉** | TG「最优价平仓」（该路径不受 -4061 影响） | 最快脱身；但 close_phase=1 会让监控冻结，结算仍可能异常 |
| **B. 保持持仓，先解冻** | 停 bot → 手工把 `close_phase/pending_close/is_programmatic_cancel` 清零 → 启动 | 保住仓位与策略；需停服窗口 |
| **C. 先修代码再处置** | 按修复 1/2 改完、重启，再正常平仓 | 最干净，但耗时最长 |

⚠️ 无论哪种，**在 bot 运行期间手工改 `trade_state.json` 会被内存副本覆盖**，
必须停 bot 后改。

---

## 五之二、精确改动清单（**待批准，尚未实施**）

共 5 处、3 个文件位置集中在 `trader_260725.py`。
`_merge_batch_state` 全库仅 1 处定义（L1614）+ 1 处调用（L1425），改动面干净。

### 改动 1｜L7005-7028 `close_position_market`（缺陷 A + C）

```diff
-            # 先撤销所有未成交的开仓条件单（保护单不撤，仍在位保护仓位）
-            entry_orders = target_b_data.get('entry_orders', [])
-            for idx, order_id in enumerate(entry_orders):
-                if idx >= last_filled_count:
-                    try:
-                        self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol, params={'stop': True})
-                        print(f"  └─ 已撤销开仓挂单: {order_id}")
-                    except Exception:
-                        pass
-
             # 🔥 修复漏洞1：先市价平仓，成功后再撤 SL/TP（原代码先撤 SL/TP 再平仓，
             # 若平仓失败则裸仓无保护且监控线程因 is_programmatic_cancel 不补挂）
             # reduceOnly 平仓后 SL/TP 即使短暂存在也不会反向开仓，风险远低于先撤保护再赌平仓
+            #
+            # 🔥 缺陷A修复（2026-08-29 实盘 -4061）：params 构造与限价平仓 L7576-7582 对称。
+            #    双向持仓账户必须显式 positionSide；缺省时 Binance 取 BOTH →
+            #    -4061 "Order's position side does not match user's setting"。
+            #    全库其余 create_order 的 params 均派生自 params_base（已含 positionSide），
+            #    唯独此处曾硬编码 {'reduceOnly': True}。
             close_side = 'sell' if side == 'BUY' else 'buy'
+            order_params = target_b_data.get('params_base', {}).copy()
+            if target_b_data.get('is_hedge_mode', False):
+                order_params['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'
+            else:
+                order_params['reduceOnly'] = True
+
             order = self._safe_api_call(
                 self.exchange.create_order,
                 symbol=target_symbol,
                 type='MARKET',
                 side=close_side,
                 amount=current_filled_amount,
-                params={'reduceOnly': True},
+                params=order_params,
                 retries=1
             )
             close_order_placed = True  # P0 Batch A：平仓单已创建 → 此后失败绝不回滚关闭标记
+
+            # 🔥 缺陷C修复（2026-08-29）：撤未成交开仓单由「平仓前」移到「平仓成功后」。
+            #    原位置在 create_order 之前，平仓失败时单已撤且回滚不恢复 →
+            #    批次静默丢失未成交层（实盘批次2 第2层 3000002163649920 即因此消失）。
+            entry_orders = target_b_data.get('entry_orders', [])
+            for idx, order_id in enumerate(entry_orders):
+                if idx >= last_filled_count:
+                    try:
+                        self._safe_api_call(self.exchange.cancel_order, order_id, target_symbol, params={'stop': True})
+                        print(f"  └─ 已撤销开仓挂单: {order_id}")
+                    except Exception:
+                        pass
```

> 说明：`params_base` 含 `workingType: MARK_PRICE` 与 `leverage: 100`。
> 限价平仓路径已用同一构造实证无副作用；MARKET 单对这两个参数无语义冲突。

### 改动 2｜L1389 `save_batch_state` 签名（缺陷 B 通道）

```diff
-    def save_batch_state(self, symbol: str, batch_id: str, batch_data: dict):
+    def save_batch_state(self, symbol: str, batch_id: str, batch_data: dict,
+                         *, allow_flag_rollback: bool = False):
```

```diff
                     if isinstance(existing, dict) and existing:
-                        batch_data = self._merge_batch_state(existing, batch_data)
+                        batch_data = self._merge_batch_state(existing, batch_data,
+                                                            allow_flag_rollback=allow_flag_rollback)
```

### 改动 3｜L1614 / L1621-1629 `_merge_batch_state`（缺陷 B 核心）

```diff
-    def _merge_batch_state(self, disk: dict, snap: dict) -> dict:
+    def _merge_batch_state(self, disk: dict, snap: dict, *, allow_flag_rollback: bool = False) -> dict:
```

```diff
         # —— A 类棘轮：close_phase int max + Boolean False→True 单向 ——
-        try:
-            merged['close_phase'] = max(int(disk.get('close_phase', 0) or 0),
-                                        int(snap.get('close_phase', 0) or 0))
-        except (TypeError, ValueError):
-            pass
-        for f in _MERGE_RATCHET_BOOL_FIELDS:
-            if disk.get(f) and not snap.get(f):
-                merged[f] = disk[f]  # 磁盘 True 快照 False → 保留 True
+        if allow_flag_rollback:
+            # 显式回滚通道（2026-08-29 缺陷B）：仅 close/limit-close 失败回滚可用。
+            # 调用方已「读最新磁盘→显式置 0/False」，快照即最新事实，以快照为准。
+            # 调用点数量由 AST 守卫锁定（见改动 5）。
+            try:
+                merged['close_phase'] = int(snap.get('close_phase', 0) or 0)
+            except (TypeError, ValueError):
+                pass
+            for f in _MERGE_RATCHET_BOOL_FIELDS:
+                merged[f] = bool(snap.get(f))
+        else:
+            try:
+                merged['close_phase'] = max(int(disk.get('close_phase', 0) or 0),
+                                            int(snap.get('close_phase', 0) or 0))
+            except (TypeError, ValueError):
+                pass
+            for f in _MERGE_RATCHET_BOOL_FIELDS:
+                if disk.get(f) and not snap.get(f):
+                    merged[f] = disk[f]  # 磁盘 True 快照 False → 保留 True
```

### 改动 4｜L7135 / L7675 两处回滚调用（缺陷 B 落点 + 写后自证）

```diff
                     rollback_b_data['close_phase'] = 0  # P0 Batch A：1→0 合法回滚（平仓单未创建）
-                    self.save_batch_state(target_symbol, batch_id, rollback_b_data)
-                    print(f"  └─ 🔄 平仓失败回滚：已清除 is_programmatic_cancel/pending_close/close_phase，监控线程恢复保护")
+                    self.save_batch_state(target_symbol, batch_id, rollback_b_data,
+                                          allow_flag_rollback=True)
+                    _v = ((self.load_all_states().get(target_symbol, {}) or {}).get(batch_id, {}) or {})
+                    print(f"  └─ 🔄 平仓失败回滚：close_phase={_v.get('close_phase')} "
+                          f"pending_close={_v.get('pending_close')} "
+                          f"is_programmatic_cancel={_v.get('is_programmatic_cancel')} "
+                          f"（期望 0/False/False）")
```

限价平仓 L7675 同构（原文案为「挂限价单失败回滚」）。

> **写后回读自证**是本次的关键增补：原代码的问题是「打印成功但实际未生效」，
> 加上回读打印后，回滚失效会立刻暴露为 `close_phase=1`，不会再出现静默冻结。

### 改动 5｜AST 守卫（**请 ChatGPT 裁定是否必要**）

`allow_flag_rollback=True` 等于给安全棘轮开了一道门。建议加一条与 G-B9 同构的
正则/AST 断言测试，锁定：

```
① 全库 allow_flag_rollback=True 的调用点必须恰好 2 处（市价平仓 L7135 / 限价平仓 L7675）
② save_batch_state / _merge_batch_state 的参数默认值必须为 False
③ 除这两处外，任何位置出现 allow_flag_rollback 即判失败
```

**请裁定**：口子本身是否可接受？若否，替代方案是「不走 save_batch_state，
为回滚单独实现一个直写通道（绕过 merge 但保留墓碑检查）」——
后者不引入新参数，但需要复制一遍墓碑/持久化逻辑，重复代码是它的代价。

---

## 六、未改动声明

- 生产代码零改动：`git diff --stat HEAD` 为空，HEAD = `e953d79`
- 本轮所有取证均为只读：源码 Grep/Read + `verify_no_duplicate_orders.py`（只读核对）
- 未向交易所发出任何下单/撤单指令
