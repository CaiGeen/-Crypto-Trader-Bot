# 批次3「手动撤单 → 程序自动清理」取证报告 + D-009 异常重启测试基线

> 生成时间：2026-08-29 17:47（GMT+8）
> 运行实例：watchdog 20044→21396，bot_runner **12240**→9144，启动于 17:10:07
> 运行代码：**e953d79**（P0 已加载，已由运行期日志实证）
> 取证方式：**全部只读**。唯一写入是基线快照 JSON 与本报告
> 生产代码自 e953d79 后**零改动**

---

## 一、本轮事件时间线

| 时刻 | 事件 | 证据 |
|---|---|---|
| 17:23:53 | 批次3 `batch_20260829_172353_dd6431` 建立，2 层条件单 `…724624`(80521) / `…724627`(80701) | 终端日志 |
| 17:23:59 | 重发同参数信号 → D-005 拦截 → Markdown BadRequest **offset 138** | 终端日志 |
| 17:24:00 | **P0 降级生效**（+0.54s），TG 收到完整拦截通知 | 终端日志 + 用户 TG |
| 约 17:38 | 用户在 App **手动撤销** `…724627`（第 2 层） | 用户操作 |
| 17:38:32 | 程序检测到手动撤单 → 撤销剩余 `…724624` → converge（`scope=PRE_ENTRY`, L1=0 L2=0 L3=0）→ **clear → 墓碑 close_phase=3** | 终端日志 + 墓碑 |
| ~17:39 | **批次2 层0 真实成交** @ 77692.6，程序挂出 SL `…739625` / TP `…739660` | 账本 + 交易所 |
| 17:44:51 | 建立强杀测试基线快照 | `G:/tmp/killtest_before.json` |

---

## 二、批次3 清理链 —— 首次真实触发 `scope=PRE_ENTRY` 收敛证明

### 日志链（完整、无缺失环节）

```
⚠️ 🛑 [手动撤单提醒] 第 2 层开仓条件单被撤销 (ID: 3000002163724627)
🚨 [批次终止] 本批次未建仓且开仓挂单被撤销，正在退出...
🧹 正在清理本批次残余开仓挂单...
  └─ 已成功撤销开仓挂单: 3000002163724624
✅ [B1] 批次 ... 收敛证明生成：L1=0 L2=0 L3=0 scope=PRE_ENTRY
🧹 批次 [...] 状态归档/清理完毕（proof 门通过，墓碑已登记 close_phase=3，7 天防复活）
👀 批次 [...] 监控已移除 (剩余活跃监控数: 2)
🧹 批次 [...] 监控线程已退出
```

### 墓碑（建仓前清理的最干净形态）

```json
{
  "symbol": "BTCUSDT", "side": "BUY",
  "cleared_at": 1787996312.8702645,
  "converged_order_ids": ["3000002163724624", "3000002163724627"],
  "known_order_ids":     ["3000002163724624", "3000002163724627"],
  "close_phase": 3
}
```

**`known == converged`（2=2），与首个平仓批次形成对照：**

| 批次 | 触发方式 | scope | known / converged | 差值原因 |
|---|---|---|---|---|
| `160337_79d97e` | 手动平仓 | `FULL` | 5 / 4 | 差的是**限价平仓单本体** `1119893808642`，已成交离场，物理上不需撤销（与 TP 的 -2011 幂等同属一类） |
| `172353_dd6431` | 手动撤单 | `PRE_ENTRY` | **2 / 2** | 无成交、无保护单，两个 entry 单均被程序撤销 → 全部进 converged |

两种 scope 目前**均已取得真实样本**，且都通过了 proof 门。

### scope 判定的 Fail-Closed 守卫（源码实证）

- `trader L7454`：`_scope = 'FULL' if self._batch_has_active_exposure(b_data) else 'PRE_ENTRY'`
- `trader L7258-7261`：若 `_scope` 非二者之一 → 拒绝；且注释与判据明确
  **「当前存在活跃敞口，PRE_ENTRY proof 不足（修正1）」** → 有敞口时不会放行

即：**建仓后的批次不可能用 PRE_ENTRY 蒙混过关**，必须由 FULL 扫描证明。批次3 走 PRE_ENTRY 是因其 `filled_count == 0`、无持仓、无保护单，判定正确。

---

## 三、手动撤单的分治设计（源码实证 trader L4809-4856）

这是本轮新确认的行为，对"程序需兼容手动操作"这一用户场景很关键：

| 撤单时的批次状态 | 程序行为 |
|---|---|
| **`filled_count == 0`（未建仓）** | 撤销**全部**剩余开仓挂单 → converge → clear → 墓碑 → **终止批次**（本次批次3 走此路） |
| **`filled_count > 0`（已建仓）** | **只撤销未成交层的挂单**，已成交仓位**继续运行 SL/TP**，批次**不终止**（L4821-4856） |

另有两处细节：

- **程序主动撤单不会误报**：`is_programmatic` 标志（L4752-4757）区分程序撤单与人工撤单，程序撤单打印 `ℹ️ [程序撤单]` 而不触发 `⚠️ 🛑 [手动撤单提醒]`，也不会置 `manual_canceled_detected`。
- **手动撤单会发 TG 通知**（L4761 `⚠️ 🛑 [撤单提醒]`），批次终止后另发一条 `🧹 [批次终止]`。

⚠️ **需要留意的边界**：因为未建仓批次撤一单即终止整批，若在 App 上误撤某个未成交 entry 单，整个批次会被清理（其余层一并撤销）。方向是 Fail-Closed（避免账本与交易所长期不一致），符合既有设计哲学，**此处只记录、不改**。

---

## 四、核对器升级 v1.0 → v1.1（修正两个误报，均为脚本缺陷非生产问题）

批次2 成交后，v1.0 报出「交易所多出 2 个孤儿单 + 账本多出 1 个」。逐项核实后确认是脚本口径错误：

| v1.0 误报 | 真实原因 |
|---|---|
| 「交易所多出 `…739625` / `…739660`（孤儿单）」 | 这两个是批次2 成交后挂出的 **SL / TP**，存在 `current_sl_id` / `tp_order_id`，**不在 `entry_orders` 里**，v1.0 只扫 entry_orders |
| 「账本多出 `…649917`」 | 该单即**已成交的层0**，成交后条件单从交易所 algo 列表消失（正常），对应 `filled_details[0]=77690.0` |

v1.1 修正：

1. 账本侧集合 = 未成交 entry 单 **+ `current_sl_id` + `tp_order_id`**
2. `filled_details[i] > 0` 的 entry 单标记为「已成交、预期不在交易所」，单独列出，**不计入差异**
3. 交易所订单按 `info['type']`（ccxt 归一化后 `type` 会退化成 `market`，真实类型在 `info` 里）归类为 ENTRY / SL / TP
4. 新增 `--snapshot` / `--compare`，支持 **ID 集合相等**判定与进程 + 文件指纹对比

**v1.1 核对结果：rc=0，逐单归属清晰**

```
交易所条件单 7  |  账本预期 7
  …649040 / …649051 / …649052 / …649055   ENTRY  批次1 层0-3
  …649920                                  ENTRY  批次2 层1
  …739625                                  SL     批次2
  …739660                                  TP     批次2
（已成交、预期不在交易所: 1）
  …649917  批次2 层0  ✅已成交
普通单 0    持仓 1 条（BTC/USDT:USDT long 0.001 @ 77692.6）
✅ 交易所无任何账本外的条件单 → 无重复挂单、无孤儿单
✅ 账本预期的每一个单在交易所都能查到
```

---

## 五、批次2 真实建仓（第二个真实成交批次）

```
触发价 77690 → 成交价 77692.6（STOP_MARKET 触发后市价成交，滑点 2.6 U，正常）
持仓 0.001 BTC  |  entryPrice 77692.6  |  手续费 0.038845
SL …739625（75001）  TP …739660（80000）  均已挂出并确认在交易所
```

账本字段：`last_filled_count=1`、`filled_details=[77690.0, 0.0]`、`pending_sl_orders=[1]`、`close_phase=0`。

**这意味着强杀测试将带真实持仓进行** —— 验证价值高于空仓场景：恢复后必须证明 SL/TP 未被重复挂出、也未被丢失。

---

## 六、强杀测试基线（`G:/tmp/killtest_before.json`，17:44:51）

### 进程

| PID | PPID | 启动时刻 | 角色 |
|---|---|---|---|
| 20044 | 12284 | 17:10:07 | watchdog（venv 启动器） |
| 21396 | 20044 | 17:10:07 | watchdog（Python311 子进程） |
| **12240** | 21396 | 17:10:07 | **bot_runner（venv 启动器）← taskkill 目标** |
| 9144 | 12240 | 17:10:07 | bot_runner（Python311 子进程） |

### 交易所（7 个条件单，ID 全量）

```
3000002163649040  buy   ← 批次1 层0
3000002163649051  buy   ← 批次1 层1
3000002163649052  buy   ← 批次1 层2
3000002163649055  buy   ← 批次1 层3
3000002163649920  buy   ← 批次2 层1
3000002163739625  sell  ← 批次2 SL
3000002163739660  sell  ← 批次2 TP
普通单 0    持仓 long 0.001 @ 77692.6
```

### 状态文件

| 文件 | 大小 | mtime | md5(前12) |
|---|---|---|---|
| trade_state.json | 13206 B | 17:39:55 | `c747ca676f0f` |
| trade_tombstones.json | 941 B | 17:38:32 | `9e344e86b6e5` |
| trade_stats.json | 1206 B | 16:30:03 | `f28f65b0abd1` |

### 账本

2 个活跃批次；`d009_gray_watch.py` **rc=0**（账本合法、墓碑 2 条、进程 4 个、pyc 与源码一致）。

---

## 七、强杀操作与验收标准

### 操作

```powershell
taskkill /PID 12240 /T /F
```

- `/T` 杀进程树（含 9144），`/F` 强制
- **不要杀 watchdog**（20044 / 21396）——它必须存活才能自动拉起
- 预期：bot_runner 消失 → watchdog 检测 → 自动启动新 bot_runner

### 验收（ChatGPT 指定的 6 项，已全部做成可执行判据）

| # | 验收项 | 判据 |
|---|---|---|
| ① | watchdog 存活 | PID 20044 / 21396 仍在 |
| ② | bot_runner 新 PID | `--compare` 输出「新增 PID」非空，且非 12240 |
| ③ | 账本完好、2 个活跃批次 | 对比表显示 `trade_state.json` 非 0 字节；`d009_gray_watch.py` rc=0 |
| ④ | **条件单 ID 集合完全相等** | `--compare` 输出「✅ 条件单 ID 集合完全相等」，而非仅数量相等 |
| ⑤ | 恢复链正常 | 终端出现 2 批次接管 + `系统 READY` |
| ⑥ | 无资金安全告警 | 终端无 🚨（除已知的手动撤单/程序撤单提示外） |

### 执行步骤

```bash
# 强杀前（若基线已超过 ~10 分钟，先刷新一次）
.venv/Scripts/python.exe verify_no_duplicate_orders.py --snapshot G:/tmp/killtest_before.json

# 强杀（PowerShell）
taskkill /PID 12240 /T /F

# 等 watchdog 拉起并完成恢复（约 10-20 秒）后，一次跑完所有判据
.venv/Scripts/python.exe verify_no_duplicate_orders.py --compare G:/tmp/killtest_before.json
.venv/Scripts/python.exe d009_gray_watch.py
```

### 对比判据的设计（避免两类误判）

1. **只比数量会漏判**：`A B C D E F → G H I J K L` 数量也是 6，但意味着全部重挂。
   因此 `--compare` 比的是 **ID 集合相等**。
2. **纯 ID 对比会误判**：已知程序存在「预生成 → 撤销 → 重挂同价格 SL/TP」行为（此前记录、暂缓优化），
   若强杀前后间隔较长，程序自发重挂也会让 ID 变化。
   因此加了**角色级对比**：ID 变但 `(batch, role)` 集合不变 → 判为 replace 并**提示人工核对终端日志**，
   判据是「恢复阶段不应出现任何『已挂出』字样」。
3. **已成交导致的消失合法**：消失的 ID 若落在 `filled_details` 已成交集合里，不算失败。

---

## 八、状态

| 项目 | 结论 |
|---|---|
| P0 通知降级 | ✅ GREEN（运行期实证：BadRequest → +0.54s → TG 实收） |
| 批次3 手动撤单清理链 | ✅ 通过（PRE_ENTRY scope 首次真实样本） |
| 两种收敛 scope（FULL / PRE_ENTRY） | ✅ 均有真实样本 |
| 手动撤单分治（无成交终止 / 有成交保仓） | ✅ 源码实证 |
| 批次2 真实建仓 + SL/TP 挂出 | ✅ 交易所侧确认 |
| 核对器 v1.1 | ✅ rc=0，误报已修 |
| 强杀测试 | ⏳ 待执行（基线已就绪） |

---

## 九、证据复现命令（全部只读）

```bash
# 交易所 ↔ 账本逐单核对（v1.1）
.venv/Scripts/python.exe verify_no_duplicate_orders.py

# 建基线 / 对比
.venv/Scripts/python.exe verify_no_duplicate_orders.py --snapshot G:/tmp/killtest_before.json
.venv/Scripts/python.exe verify_no_duplicate_orders.py --compare  G:/tmp/killtest_before.json

# 灰度检查器（只读，0 正常 / 1 WARN / 2 ERROR）
.venv/Scripts/python.exe d009_gray_watch.py

# P0 专项测试
.venv/Scripts/python.exe test_tg_reply_fallback.py

# D-005 offset 双点定量验证（兼回归护栏）
.venv/Scripts/python.exe verify_d005_offset_model.py
```
