# D-009 上线加载验证报告（commit 580f077）

> **终审状态：`D-009 CLOSED / GRAY OBSERVATION`（ChatGPT 2026-08-29 终审通过）**
>
> ChatGPT 终审裁定：「580f077 可以维持当前线上状态，D-009 本批次不需要再改设计，也不建议为了
> *验证而验证*去做破坏性演练。」并**特别批准保留**裁定外新增的 `_state_corrupted` 写保护：
> 「这一项实际上比普通 fsync 更重要——读取失败 ≠ 空状态。」

> 触发：ChatGPT 裁定「D-009 可以封版上线，不再改设计、不做破坏性演练；直接重启加载 580f077，完成
> **进程身份 → 代码版本 → 启动恢复 → READY → TG /status** 的真实链路验证，然后进入灰度观察」。
>
> 用户于 **2026-08-29 15:23:55** 手动重启。本文为验证结果，生产代码**零改动**，状态文件**零改动**。

---

## 一、验证结论总览

| # | 环节 | 结论 | 证据强度 |
|---|---|---|---|
| ① | 进程身份 | ✅ PASS | 硬（进程对象 + 日志文件交叉） |
| ② | 代码版本 = 580f077 | ✅ PASS | 硬（PEP 552 pyc 头部逐位比对） |
| ③ | 启动恢复链 | ✅ PASS | 硬（生产真实状态文件离线复现） |
| ④ | READY | ✅ PASS | 硬（终端日志原文） |
| ⑤ | TG `/status` | ✅ PASS | 端点侧（用户实测秒回） |

**五段全部闭环，`rc=0`。**

**未做破坏性演练**（遵守裁定）；**未做真实断电 T2**（裁定明确不做）。

---

## 二、① 进程身份

```
watchdog     18232 (venv 启动器) → 18800 (Python311)   CreationDate 15:23:55
bot_runner   13512 (venv 启动器) → 15028 (Python311)   CreationDate 15:23:55
```

- 4 个 `python.exe` **同一个 CreationDate**，符合本项目「1 逻辑进程 = 2 python.exe（venv 启动器 → 系统 Python311 子进程）」规律。
- `watchdog.log` 独立记录：`✅ 主进程已启动 (PID: 13512)` —— **PID 与进程列表精确对上**，成链。
- 启动后 **10+ 分钟无崩溃、无自动重启日志**（watchdog 日志无新增事件），运行期活性：
  - PID 15028：CPU 2.16s / 104.2 MB / 3 线程
  - `.notify_queue/` 目录 mtime **15:30:35**（通知消费线程持续读写）
  - `.notify.state.json` mtime **15:29:15**（启动后被写入）

---

## 三、② 代码版本（核心硬证据）

**HEAD = `580f077`**，工作区生产代码零 `M`（仅未跟踪文档）。

### pyc 时间戳分布

```
08-29 15:23:56  trader_260725.cpython-311.pyc   ← 本次重启编译（启动后 1 秒）
08-29 08:11:40  watchdog.cpython-311.pyc        ← 陈旧（测试 import 产生）
08-29 08:11:40  bot_runner.cpython-311.pyc      ← 陈旧（测试 import 产生）
```

### 头部逐位比对（timestamp-based，flags=0）

| 字段 | pyc 头部记录 | 磁盘 580f077 源码 | 一致 |
|---|---|---|---|
| source mtime | 1787984449 (14:20:49) | 1787984449 | ✅ |
| source size | 488100 | 488100 | ✅ |

### 推理链

1. 旧进程（13:26 启动）当时源码仍是 c35f014，其 pyc 与旧源码匹配。
2. 14:20:49 我改写了 `trader_260725.py` → 旧 pyc 失效。
3. 15:23:55 新进程启动，`bot_runner.py:53` 执行 `from trader_260725 import CryptoTrader` → Python 判定 pyc 头部与源码不符 → **重新编译并覆写 pyc**，文件 mtime = **15:23:56**。
4. 新 pyc 头部记录的 mtime/size 与 580f077 源码**逐位一致**。

> **若运行的是旧代码，旧 pyc 有效、不会被重写。** 它被重写这一事实本身，就证明本次启动编译加载的是新源码。

### 关于 watchdog / bot_runner 的陈旧 pyc

**无害，不参与加载。** `__main__` 脚本在 CPython 中从不生成也不读取 pyc，只有被 `import` 的模块才有缓存。
全项目 `import bot_runner` 只出现在 `test_*.py`（生产链路零处），故这两个 pyc 由测试运行产生、与实际加载无关。
主脚本版本由「启动时间 15:23:55 晚于源码 mtime 14:20/14:30 + 主脚本直接读盘编译」保证。

> ⚠️ 这条是本次验证新沉淀的取证纪律：**不可用 `__main__` 脚本的 pyc 做版本证据**，否则会误报「加载旧代码」。

---

## 四、③ 启动恢复链（生产真实数据，只读复现）

**生产状态实况**

| 文件 | 大小 | 内容 | 判定 |
|---|---|---|---|
| `trade_state.json` | 2 B | `{}` | 合法空账本 |
| `trade_tombstones.json` | — | 不存在 | 缺文件 ≠ 损坏 |

**用真实文件跑 D-009 三态判定（全程只读，损坏用例走 tempfile 副本）**

| 用例 | 输入 | `_state_corrupted` | 结果 |
|---|---|---|---|
| 1 | **生产真实 `{}`** | `False` | ✅ 空账本未被误伤 → recover 放行 → READY |
| 2 | 墓碑文件缺失 | `_tombstones_degraded=False` | ✅ 缺文件 ≠ 损坏 → 新批次正常放行 |
| 3a | 截断 JSON | `True` | ✅ Fail-Closed |
| 3b | 0 字节（断电形态） | `True` | ✅ Fail-Closed |
| 3c | 纯空白 | `True` | ✅ Fail-Closed |
| 3d | 合法有批次 | `False` | ✅ 不误判 |
| 4 | 损坏态写入 | `_persist_states` 返 `False` | ✅ 拒绝覆盖，残留证据 `{"broken` 完好 |

> **用例 1 是本次改动最需要盯的线上风险点**：若空账本被误判为损坏，bot 会静默进入 BLIND-SAFE 不交易
> （Fail-Closed 方向安全，但可用性丧失且不易察觉）。离线复现证明**未误伤**。
> 生产文件验证后内容仍为 `{}`，**零改动**。

---

## 五、④⑤ READY 与 TG `/status`（用户实测闭环）

**终端日志原文（关键段）**

```
✅ [恢复前健康检查] 通过
✅ [状态恢复] 恢复流程完成，共接管 0 个历史活跃批次
✅ [启动检测] 历史任务恢复校验完成！系统 READY
```

```
📊 MAX_LEVERAGE: 100x ✅
📊 Binance API: ✅ 已连接
📊 活跃批次: 0 个
```

- **④ READY ✅**：出现 `✅ [启动检测] 历史任务恢复校验完成！系统 READY`（`bot_runner.py:2399`），
  **未出现** `🚫 系统未就绪`，**未出现** `🚨 [D-009] trade_state.json 读取失败`。
  → `_state_corrupted=False` 判定在**真实运行路径**上得到确认，与离线复现结论一致。
- **⑤ TG `/status` ✅**：用户实测秒回，端到端链路（trader 事件循环 + 交易所 API + 通知链）全活。
- **接管 0 个批次**：与空账本 `{}` 自洽 —— 无持仓即无批次，**不是**账本丢失。

**`/status` 副作用核查（只读性）**

| 文件 | 发送前 | 发送后 | 结论 |
|---|---|---|---|
| `trade_state.json` | 2 B / 08-28 21:05:15 | 2 B / 08-28 21:05:15 | ✅ 零改动 |
| `trade_stats.json` | 911 B / 08-28 21:04:16 | 911 B / 08-28 21:04:16 | ✅ 零改动 |

→ `/status` 为纯查询，不触发 `_persist_states`，不会把空账本写回磁盘。

**运行 17 分钟复检**：4 个进程 PID 全部未变（13512/15028/18232/18800），无重启、无崩溃，
watchdog 日志无新增事件。

---

## 六、灰度观察（ChatGPT 指定三项，只观察不改码）

| # | 观察事件 | 检查内容 | 何时触发 |
|---|---|---|---|
| ① | **首次真实有持仓后状态保存** | `trade_state.json` 正常更新；无意外 `_state_corrupted`；fsync 不抛异常 | 下次开仓后 |
| ② | **首次真实平仓** | open / algo / stop orders 三处收敛；`close_phase`、tombstone、registry 终态一致；TG 通知正常 | 下次平仓时 |
| ③ | **下一次自然异常重启** | `trade_state.json` 是否仍有效 —— 为 D-009 的 fsync 提供**真实断电样本** | **不人为制造** |

> 第 ③ 项尤其有价值：D-010 已有真实断电样本，但 **D-009 的 fsync 尚无真实断电验证**；
> 裁定明确「让下一次自然异常事件提供证据即可」，不主动制造。

### 观察工具：`d009_gray_watch.py`（新增，零侵入）

```bash
.venv/Scripts/python.exe d009_gray_watch.py     # 退出码 0=正常 / 1=WARN / 2=ERROR
```

输出 5 段：① 代码版本（pyc 头部硬证据）② 账本三态判定 ③ `.bak` 取证信息 ④ 进程身份 ⑤ 观察项清单。

安全边界：
- **全程只读** —— 只调 `load_all_states` / `_load_tombstones`，**绝不调用**任何写入方法，
  不碰下单/撤单/平仓路径。
- **`.bak` 只 `stat` 不 `open`** —— 遵守「仅人工取证、永不自动装载」裁定。
- **判定逻辑直接复用生产代码**，而非另写一套 —— 避免「检查器说没问题、生产却误判」的假安全感。
- 自动排除检查器自身的 python.exe 进程（否则每次都误报「多了 2 个进程」）。

常规项（沿用 Batch B）：首次真实平仓的 open/algo/stop orders 收敛、`close_phase`/tombstone/registry 终态、
TG 是否 `CONVERGENCE_UNKNOWN` 及恢复。

**D-009 新增观察项**
- [ ] **不得出现意外的 BLIND-SAFE**：无损坏时 `_state_corrupted` 恒为 False，若 TG 冒出
      `🚨 [D-009] trade_state.json 读取失败` 而磁盘文件正常 → 立即回报（属误判）
- [ ] 有持仓后观察 `_persist_states` 写入正常（新增 fsync，实测增量仅 +1.035 ms/次，日增约 0.3 s）
- [ ] 墓碑文件首次生成后，`_tombstones_degraded` 应为 False
- [ ] `.bak` 仍然只写不读（任何情况下不出现自动恢复）

> 生产目录实测存在 `trade_state.json.bak`（6042 B，08-28 21:04:16）—— 正是审计阶段发现的
> 「含 `is_active=True` 幽灵批次」的那份，再次印证 **`.bak` ≠ last-known-good**。

**已知遗留（不属缺陷，裁定明确留后续）**
- 其余四文件（`.notify_queue` / `auth_blocked` / `trade_stats` / `.notify.state` / `signal_dedup`）暂未加 fsync
- `.bak` 持久化未强化（`copy2` 非原子）
- 完整 reconciliation（Q2 阶段二）未做；本批仅实现只读 Census

---

## 六之二、观察项① 首次真实数据（2026-08-29 15:52–15:55）

用户开始程序下单测试，两单在检查窗口内落地。**全程生产代码零改动**（`git status` 无 `M`）。

### 落单事实

| 批次 | 时间 | 方向 | 规模 | 分层 | TP | entry stop | 止损 |
|---|---|---|---|---|---|---|---|
| `batch_20260829_155232_f49f2e` | 15:52:32 | BUY | 0.004 BTC | 4 层 × 0.001 | 155190 | 93114 / 108633 / 124152 / 139671 | 62076–73715 |
| `batch_20260829_155343_cfdf77` | 15:53:43 | BUY | 0.002 BTC | 2 层 × 0.001 | 80000 | — | — |

两批均为 **100x、条件单挂单**，`positionSide=LONG`、`workingType=MARK_PRICE`。

### D-009 观察项① 结论：写入侧 ✅ 通过

| 检查项 | 结果 |
|---|---|
| `trade_state.json` 增长 | `{}`（2 B）→ 7189 B → 11536 B → 11526 B，**多次真实写入无截断** |
| 三态判定 | 检查器 `rc=0`，`_state_corrupted=False`、`_tombstones_degraded=False` |
| fsync 链 | **未抛异常**（若抛异常 `_persist_states` 会打印警告且账本停止更新；实际账本持续正常增长 → 反证无异常） |
| 进程 | 13512 / 15028 / 18232 / 18800 全程未重启 |
| 检查器「有批次账本」预演 | ✅ 未误报（此前仅在空账本调试过，本次为首次带数据运行） |

### 附带收获：保护单状态机推进正常

批次1 的 4 条 ENTRY registry 全部由 `PENDING_VERIFY` 推进到 **`CONFIRMED`**（15:53:5x，
`updated_at` 从 `…956` 跳到 `…036`），批次2 的两条同样 `CONFIRMED`。
证明 `create_order → registry 登记 → 交易所确认` 闭环在真实链路上正常。

### `.bak` 语义再获实证实锤（非理论推演）

写入过程中 `.bak` 依次经历三种形态：

```text
6042 B（08-28 幽灵批次，历史遗留）
  → 4449 B（批次1 刚建，半成品：保护单尚未 prepare 完整）
  → 11559 B（2 批次完整，但 registry 仍为 PENDING_VERIFY）
```

取证对比（只读 `open` 用于**人工取证**，绝不装载、绝不写回）显示 **`.bak` 恒落后当前账本
一个写入周期**，最新一次差异为：

```text
protection_registry …ENTRY|L0..L3|LONG.state
    .bak = PENDING_VERIFY     ← 订单尚未确认
    now  = CONFIRMED          ← 已被交易所确认
```

**推论**：若崩溃后误用 `.bak` 恢复，会丢失「订单已被交易所确认」这一关键事实 ——
程序将重复挂单或漏管已存在订单。这是 ChatGPT **R3「`.bak` 仅证据源，禁止静默恢复」**
的现实依据，而非设计上的谨慎假设。

### 当前状态与尚未触发的节点

两批**均未成交**：`last_filled_count=0`、`filled_details` 全 0、`close_phase=0`、
`tp_order_id` / `current_sl_id` 均为 `None`。属条件单挂单阶段。

因此下列节点尚未触发，需等待真实行情：

1. **任一 entry 成交** → `last_filled_count` 递增、registry 新增 SL/TP 条目、账本写入频率上升
2. **持仓建立后保护单挂出** → `current_sl_id` / `tp_order_id` 由 `None` 变为有值
3. **平仓** → `close_phase` 推进 + tombstone 生成 —— **D-009 与 Batch B 的联合验收点**

---

## 七、一句话结论

**580f077 已在运行实例生效，五段验证全部闭环，生产代码与状态文件零改动。D-009 正式转入
`CLOSED / GRAY OBSERVATION`，不再改设计、不做破坏性演练。**

本次修复的本质（ChatGPT 终审小结）：

```text
   改前                              改后
   读取失败                          读取失败
     ↓                                 ↓
    {}                               UNKNOWN
     ↓                                 ↓
   没有仓位                          Fail-Closed
     ↓                                 ↓
   READY  ← 孤儿仓                  READY=False → 不增加风险
```

不是「给 JSON 加了 fsync」，而是把最危险的语义「读取失败 = 没有仓位」彻底删除。
