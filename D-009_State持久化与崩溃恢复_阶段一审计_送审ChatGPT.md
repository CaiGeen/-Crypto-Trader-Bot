# D-009 State Persistence / Crash Recovery —— 阶段一源码审计报告

> 阶段性质：**只审计，零代码改动**。生产代码 HEAD 仍为 `c35f014`（P0 Batch B 封版），工作树净。
> 报告用途：呈 ChatGPT 交叉评审，作为最终方案（`fsync` / `.bak` 恢复 / Fail-Closed）的裁定依据。
> 审计方法：全项源码实证（AST + grep + 运行时文件取证 + 本机 IO 基准），不采信 docstring 自述。

---

## 0. 触发事件（真实证据）

2026-08-29 电脑死机（非优雅断电）后重启，启动日志出现：

```
⚠️ [D-010] notify state 损坏，计数重置（最多重试 3 轮）: Expecting value: line 1 column 1 (char 0)
```

实证：断电把 `.notify.state.json` **截断为 0 字节**。该文件损坏无实质损失（仅为告警去重计数，有界 3 轮），
但它是本项目第一次在真实断电条件下暴露出：

> **原子写 ≠ 持久写。**

同一套写入范式也用在 `trade_state.json` 上，而后者损坏的后果完全不对等——这是本批次立项的唯一理由。

---

## 审计① `trade_state.json` 写入链

### 写入路径（唯一咽喉）

```
save_batch_state()            L1296  ─┐
clear_batch_state()           L1338  ─┤
_commit_protection_with_g3()  L3242  ─┼→ _persist_states(all_states)  L1277
_update_registry()            L3437  ─┤        （持 _state_lock 内的唯一落盘咽喉）
_commit_registry_txn()        L3499  ─┘
```

`_persist_states` 实现（L1282–1294）：

```python
# Step 1  .bak（注意：shutil.copy2 —— 非原子）
if os.path.exists(STATE_FILE):
    shutil.copy2(STATE_FILE, STATE_FILE + '.bak')
# Step 2  原子写主文件
with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
    json.dump(all_states, tf, indent=4, ensure_ascii=False)   # 无 fsync
os.replace(temp_name, STATE_FILE)                              # 无 dir fsync
```

### 写入频率（供审计⑤ 成本评估）

| 项 | 实测值 |
|---|---|
| 监控主循环周期 | `_calculate_monitoring_interval()` L2411：活跃批次 ≤2 → **60–80s**；>6 → 120–160s（含随机抖动） |
| 循环内 `save_batch_state` 调用点 | 14 处（L4509/4624/4976/5051/5307/5403/5587/5754/5782/5915/5953/6033/6073/6261），**全部条件触发**（新层成交 / TP 命中 / SL 移动 / 平仓推进），**无每轮无条件写** |
| 上游调用面 | `save_batch_state` 42 处、`_update_registry` 64 处 |
| 空闲实测 | `trade_state.json` mtime = 08-28 21:05，**16 小时未变**（0 批次时零写入） |

**定性：事件驱动 + 低频**，量级约「每批次每 1–5 分钟 1 次」。

### 全库原子写清单（五处，全部无 fsync）

| 行号 | 文件 | flush | fsync | dir fsync |
|---|---|---|---|---|
| L386 | `.notify_queue/*.notify`（事件入队） | ✅ | ❌ | ❌ |
| L455 | `auth_blocked.json` | ✅ | ❌ | ❌ |
| L670 | `trade_stats.json` | ❌ | ❌ | ❌ |
| **L1292** | **`trade_state.json`（主状态）** | ❌ | ❌ | ❌ |
| L1429 | `trade_tombstones.json` | ❌ | ❌ | ❌ |

`grep fsync` 全库 **零命中**。

---

## 审计② 读取链：损坏后启动恢复的实际行为（🔴 核心风险）

### 读取路径

```
load_all_states()  L1268
    if os.path.exists(STATE_FILE):
        try:  return json.load(f)
        except Exception as e:
            print(f"⚠️ 读取状态文件失败: {e}")    # ← 仅打印
    return {}                                        # ← Fail-Open：损坏 == 空账本
```

全库 **74 处**调用 `load_all_states()`，其中启动恢复链为：

```
bot_runner.run_trader_recovery_on_startup()  L2364
  → trader.recover_active_batches()          L1653
      → load_all_states()                    L1664   ← 损坏时返回 {}
      → for symbol, symbol_batches in all_states.items():   L1668
```

### 损坏后的完整灾难链（逐步实证）

| 步骤 | 实际行为 | 证据 |
|---|---|---|
| 1 | `load_all_states()` 返回 `{}` | L1273–1275 |
| 2 | `recover_active_batches` 遍历空 dict → **循环体零迭代** | L1668 |
| 3 | `fetch_positions` 校验（L1688）**根本不会被调用** —— 因为它在循环体内 | L1688 |
| 4 | 打印「✅ 恢复流程完成，共接管 0 个历史活跃批次」→ 返回 True | L1828 |
| 5 | 启动检测判定 `系统 READY` | bot_runner L2399 |
| 6 | **交易所上真实持仓 + TP + SL + limit close 单依然存在，但程序账本为空** | — |
| 7 | 后续新信号可正常建仓：风控 `_count_active_batches`（L674）基于错误空账本计算 → 限额形同虚设 | L674 |

### 是否存在兜底机制？

**没有。** 逐项排除：

- `orphan_guard`：进程级防护（防双实例 / 孤儿进程），**不检测交易所孤儿持仓与本地账本的不一致**
- 启动「硬锁校验」：`.bot_instance.lock` 实例锁，与状态无关
- **反向对账（交易所持仓 → 本地有无对应批次）不存在**。现有对账全是「正向」：以本地批次为准去交易所核对（`for batch in all_states: fetch_positions`）。本地为空 → 连查都不查
- Batch B 的 `converge` 只在 `clear` 时执行，覆盖不到「账本凭空消失」场景

> **结论：这是一个真正的 P0 级 Fail-Open 缺口。**
> 触发条件苛刻（需断电恰好命中写入窗口 **且** 当时有持仓），但一旦触发＝仓位与保护单失去全部程序侧监护。

---

## 审计③ `.bak` 生成语义实证（⚠️ 反直觉发现）

### 生成时机（L1283–1284）

`.bak` 在**每次保存前**由 `shutil.copy2(STATE_FILE, STATE_FILE+'.bak')` 生成。
即 `.bak` = **本次保存动作开始前的那份状态**，而非「已验证可用的最近版本」。

### 实测证据（本机当前文件）

```
trade_state.json      size=2      mtime 08-28 21:05:15   内容 {}        ← 空
trade_state.json.bak  size=6042   mtime 08-28 21:04:16   内容 BTCUSDT
   └─ batch_20260828_204902_da: is_active=True  close_phase=None
      tp_order_id=3000002162  current_sl_id=3000002162  protection_registry=4 项
```

**这份 `.bak` 里躺着一个 `is_active=True` 的完整活跃批次，而主文件是空的。**

时间线还原（属正常流程）：21:04 批次仍活跃 → 21:05 批次被正常 clear → 主文件写为 `{}`，`.bak` 保留清理前的旧状态。

### 对「能否用 .bak 恢复」的答案

直接回答 ChatGPT 的问题③：**`.bak` 不是严格意义的 last-known-good，不能作为静默自动恢复源。**

| 维度 | 实证结论 |
|---|---|
| 语义 | 保存前的上一版本快照，**完全可能是已被正常清理的旧批次** |
| 自身原子性 | ❌ `shutil.copy2` 非原子写，断电时自身可被截断 |
| 若从 .bak 恢复 | 会恢复出一个交易所上已不存在的「幽灵批次」（tp/sl 单号俱在） |
| 幽灵批次后续 | 启动恢复时走到 L1729「无匹配单/快照失败 → 保留证据待人工对账，不清理不接管」→ 批次卡死 + 告警 |
| 安全方向 | **方向正确但不可静默**：倾向「认为有仓位」→ 触发交易所核对 → Fail-Closed，优于返回 `{}` |
| 结论 | 只能作为**降级恢复源（degraded recovery）**，恢复后**必须强制人工确认**，绝不可静默继续 |

补充：写入顺序本身是安全的——`copy2` 阶段断电只会损坏 `.bak`（此时主文件仍是旧的、完好的）；
`replace` 后断电则主文件可能截断而 `.bak` 完好。二者同时损坏需命中 `copy2` 与 `replace` 之间的毫秒级窗口。

---

## 审计④ 六个持久化文件：Fail-Open / Fail-Closed 逐个定性

依据「损坏后是否导致程序账本与交易所现实不一致」判定，**不搞一刀切**：

| 文件 | 损坏后现状 | 数据语义 | **应定方向** | 判定理由 |
|---|---|---|---|---|
| **`trade_state.json`** | `return {}` **Fail-Open** 🔴 | 批次生命周期 / `tp_order_id` / `current_sl_id` / `limit_close_order_id` / `close_phase` / `protection_registry` | **Fail-Closed** | 账本消失但交易所有仓位＝孤儿仓+孤儿单，风控失效（审计②） |
| `.notify.state.json` | `return {}` Fail-Open ✅ | 告警去重计数 | **Fail-Open**（维持） | 最坏多重复几轮告警，且有界 3 轮 |
| `auth_blocked.json` | **Fail-Closed**（按 BLOCKED + 显式告警）✅ | 鉴权阻塞状态 | **Fail-Closed**（维持） | 已有正确实现：`_load_auth_state` L399，docstring 明示 ChatGPT 裁定 |
| `signal_dedup.json` | **Fail-Closed**（拒绝执行信号）✅ | 信号去重指纹 | **Fail-Closed**（维持） | D-005 既有，bot_runner L2046 注释明示 |
| `trade_stats.json` | **Fail-Closed**（`return 0.0, False`）✅ | 当日已实现盈亏 | **Fail-Closed**（维持） | D-006 既有，`_get_today_realized_pnl` L687 docstring 明示「未知状态 ≠ 允许」 |
| `trade_tombstones.json` | `return {}` Fail-Open ⚠️ | 已清理批次防复活登记（7 天 TTL） | **Fail-Closed（告警降级）** | `_load_tombstones` L1406；墓碑丢失＝复活通道重新打开，退回到 Batch C 之前的风险面；但不直接致资金损失，宜「告警 + 降级标志 + 继续」而非拒绝启动 |

### 🔑 一致性铁证

同项目内 **已有三处**确立并在 docstring 中写明同一哲学：

> 「未知状态 ≠ 允许」—— D-005 去重表、D-006 盈亏、D-010 鉴权

**唯独 `trade_state.json`（数据最关键的那个）是 `return {}`。**
这证明其 Fail-Open 是**遗漏/不一致**，而非有意设计——修复它属于「补齐既有惯例」，不是引入新机制。

---

## 审计⑤ fsync 成本实测

本机基准（临时目录，200 次均值，模拟 1.7KB / 3 批次状态文件，与 `.bak` 量级相当）：

| 方案 | 单次耗时 | 增量 |
|---|---|---|
| 现状 `flush + replace` | 2.300 ms | — |
| **方案 A `flush + fsync + replace`** | **3.119 ms** | **+0.820 ms（+36%）** |
| 现状 `.bak` copy2（每次保存都要付） | 3.639 ms | ← 已是大头 |

按「单批次每 5 分钟保存 1 次」折算：

- 现状：**1.71 s/天**
- 加 fsync：**1.95 s/天**（**增加 0.24 s/天**）

**裁定：fsync 成本可忽略。** 且现有每次保存已在付 5.94 ms（copy2 + 原子写），fsync 的 0.82 ms 仅使总耗时 +14%。

唯一需留意：`_persist_states` 在**持 `_state_lock` 内**执行，fsync 会延长锁持有约 0.82 ms（相对现状 5.94 ms 的锁内 IO，+14%），对监控线程无实质影响。

> 备注：审计要求「不能仅凭『状态写得不频繁』判断」——已用实测频率（审计①）+ 实测 IO（本项）双重验证。

---

## 审计⑥ 断电恢复测试设计

### T1 层：损坏注入测试（**可完全自动化，建议实施**）

注入 6 种损坏样本，断言读取侧行为符合审计④ 定性表：

| # | 样本 | `trade_state.json` 期望 | `.notify.state.json` 期望 | 墓碑期望 |
|---|---|---|---|---|
| T1-1 | 0 字节 | **拒绝启动 / READY=False**（Fail-Closed） | 重置 `{}` | 告警 + 降级标志 |
| T1-2 | 截断 JSON（`{"BTCUSDT": {"b1": {"is_ac`） | 同上 | 重置 `{}` | 同上 |
| T1-3 | 非法内容（`not json at all`） | 同上 | 重置 `{}` | 同上 |
| T1-4 | 合法 JSON 但根节点非 dict（`[]`） | 同上 | 重置 `{}` | 同上 |
| T1-5 | 合法空 `{}` | **正常启动**（与文件不存在同义，非损坏） | 正常 | 正常 |
| T1-6 | 合法非空 | 正常 | 正常 | 正常 |

补充断言：
- T1-7：Fail-Closed 触发时必须发送 critical 告警（且进程内去重 1 次）
- T1-8：Fail-Closed 时**不得**静默从 `.bak` 恢复后正常运行（审计③ 结论）
- T1-9：主文件损坏 + `.bak` 完好 → 恢复源可用，但必须标记 `DEGRADED` 并强制人工确认

### T2 层：真实断电演练（**不可自动化，建议人工一次**）

- 时机选择：**必须无持仓时**（当前即满足条件）
- 方法：在 `_persist_states` 执行窗口内物理断电（或用 VM 快照强制断电）
- 验证：主文件 / `.bak` / 启动行为 / 交易所状态四项
- 风险：可能损坏开发机文件系统——**建议仅在无持仓且已备份时进行，或跳过**

### T3 层：fsync 有效性验证

用户态**无法可靠验证** fsync 的持久性保证（OS/磁盘可撒谎）。
只能靠：① 代码审查确认调用点 ② 文档化意图 ③ T1 层保证即使 fsync 失效，Fail-Closed 仍能兜底。
**因此：方案 A（fsync）是「降低概率」，方案 B（Fail-Closed）是「控制后果」——后者才是真正的 P0。**

---

## 结论与建议方案

### 优先级裁定（与 ChatGPT 一致）

> **真正的 P0 是 `trade_state` 损坏后的 Fail-Open 语义，而不是单纯缺 fsync。**

fsync 降低损坏概率；Fail-Closed 控制损坏后果。概率可以降到很低但不为零，
而后果若不控制，一次命中即为孤儿仓 + 孤儿单。**后者必须先做。**

### 建议方案（A + B，且 B 需按审计③ 收紧）

**A — 持久性加固（低成本，实测 +0.24 s/天）**

1. `_persist_states`：`flush` → `os.fsync(tf.fileno())` → `os.replace`
2. `os.replace` 后对目录 `os.fsync`（保证目录项落盘）
3. `.bak` 的 `copy2` 后同样 fsync（否则 bak 自身不可信）
4. 范围：**仅主状态 + 墓碑**（低频关键）；`.notify_queue` 事件入队不加（语义允许丢失）

**B — Fail-Closed 语义（真正 P0）**

`load_all_states()` 损坏时**禁止 `return {}`**，改为分级：

```
主文件损坏
   ├─ 尝试 .bak → 解析成功 → 标记 DEGRADED，强制人工确认（/state_confirm 或重启参数）
   │                          ↓ 未确认前：READY=False，禁止新建仓位（SG1 风格）
   └─ .bak 也损坏/不存在 → READY=False + critical 告警 + 禁止新建仓位
                            （保持监控循环对已有批次的监护？——需裁定，见下）
```

**⚠️ 由审计③ 收紧的关键约束**：`.bak` 恢复**绝不可静默**——它可能是已清理的幽灵批次，
必须走人工确认路径，否则「自动恢复」会制造新的不一致。

### 待 ChatGPT / 用户裁定的三个问题

**Q1**：`trade_state.json` 损坏且 `.bak` 不可用时，是**拒绝启动（进程退出）**还是
**启动但进入 READY=False 盲区安全模式（禁止新建仓位、保留监控）**？
- 倾向后者：与 D-010 的 BLIND-SAFE 同构，保留对交易所既有持仓的最后观测能力，
  而进程退出等于彻底放弃监护。

**Q2**：是否引入**反向对账**（启动时 `fetch_positions` 全量拉取 → 与本地账本比对 →
发现交易所持仓在本地无对应批次即告警）？
- 这是唯一能兜住「账本消失」的正向检测手段，但属新机制、增加启动 API 权重与复杂度。
- 建议列为 D-009 阶段 2，阶段 1 只做 A + B。

**Q3**：墓碑损坏的处理粒度——`告警 + 降级标志 + 继续`（推荐）还是 `Fail-Closed 拒绝启动`？
- 倾向前者：墓碑丢失不直接致损，且拒绝启动会让系统在仅墓碑损坏时不可用，代价不对等。

---

## 附：本次审计未改动任何生产代码

```
HEAD = c35f014 （P0 Batch B 封版）
git status: tracked 工作树净
```

审计全过程为只读操作（grep / AST / 文件读取 / 临时目录 IO 基准），
`trade_state.json`、`.bak` 及全部状态文件均未被修改。
