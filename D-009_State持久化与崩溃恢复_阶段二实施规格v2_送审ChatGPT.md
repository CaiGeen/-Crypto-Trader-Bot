# D-009 State Persistence / Crash Recovery —— 阶段二实施规格 v2

> 依据：ChatGPT 阶段一正式裁定（Q1/Q2/Q3 + Persistence 批准）
> 基线：`c35f014`（P0 Batch B 封版，tracked 净，实盘运行中）
> 本文件性质：**实施规格**，含一处对裁定的**技术修正回呈**（§0.2）
> 全部引用行号均为当前 `c35f014` 源码实测值（AST/grep 双重核实）

---

## 0. 裁定接受与修正回呈

### 0.1 接受项（全部照办）

| 裁定项 | 结论 | 本规格落点 |
|---|---|---|
| **Q1** trade_state 损坏 | 启动进程，但 `READY=False`，进入 FAIL-CLOSED / BLIND-SAFE，**不退出** | §2 |
| **Q1 权限边界** | 禁止新建仓/风险增加/依赖 batch identity 的自动动作/自动创建认领 batch/.bak 静默恢复；保留 Telegram、API、只读诊断 | §2.3（逐条对照代码实证） |
| **Q2** 反向对账 | 完整 reconciliation 放阶段二之后；**阶段一必须加入最小 Exchange Position Census** | §4 |
| **Q2 禁止自动修复** | 不自动创建 batch / 不自动认领订单 / 不自动挂 SL/TP / 不自动平仓 / 不依 .bak 自动恢复 | §4.3 |
| **Q3** tombstones 损坏 | **局部 Fail-Closed**：告警 + DEGRADED + 禁止依赖墓碑完整性的自动清理/复活判断，**不强制 READY=False** | §5 |
| **Persistence** | 批准 `_persist_states` 加 `flush → fsync(file) → os.replace → fsync(directory)` | §3（含平台修正） |
| **优先级** | P0-A Fail-Closed > P0-B fsync > P0-C Position Census | §7 实施顺序按此排 |

一句话原则（照抄裁定，作为本批次设计宪法）：

> **"账本不可信时，系统绝不能把'不知道'当成'没有'。"**

### 0.2 ⚠️ 技术修正 R1：`fsync(directory)` 在本机平台不可行（需回呈裁定）

裁定批准的持久链：

```
json.dump → flush → fsync(file) → os.replace → fsync(directory)
```

**实测结论：前三步可用，第四步在本项目运行平台（Windows / NTFS）上抛异常。**

实证（本机 `.venv/Scripts/python.exe`，sys.platform=`win32`）：

| 操作 | 结果 |
|---|---|
| `os.fsync(file.fileno())` | ✅ 可用 |
| `os.open(dir, os.O_RDONLY)` + fsync | ❌ `PermissionError: [Errno 13] Permission denied` |
| `os.open(dir, os.O_RDONLY\|O_BINARY)` + fsync | ❌ 同上 |
| `os.open(dir, os.O_RDONLY\|O_DIRECTORY)` + fsync | ❌ 同上（`O_DIRECTORY` 在 Windows 上不存在） |

**根因**：POSIX 允许对目录取 fd 并 fsync（用于落盘 rename 的目录项）；Windows 无此语义，`os.open` 对目录一律拒绝。这是 Python 标准库的已知平台限制，非本项目代码缺陷。

**本机采用的处置（写入规格，非临时兜底）**：

```python
def _fsync_dir(dir_path: str) -> bool:
    """尽力而为的目录同步：
    - POSIX：可真正落盘目录项，返回 True
    - Windows：无标准 API，静默降级返回 False（绝不抛异常、绝不阻断保存）
    D-009 立场：文件级 fsync 已消除"内容截断为 0 字节"这一实测故障模式；
    目录项丢失属残余风险，由 P0-A Fail-Closed 兜底（见 §2），不属 fsync 职责。"""
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except Exception:
        return False
    try:
        os.fsync(fd)
        return True
    except Exception:
        return False
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
```

**要点**：
1. 不引入 `ctypes` + `CreateFile(FILE_FLAG_BACKUP_SEMANTICS)` —— 该方案可绕过限制，但属于平台专属原生调用，与项目"不引入新复杂度"纪律冲突，且收益（防目录项丢失）远小于 P0-A
2. 降级**静默但可观测**：返回 bool，实施阶段计入一次性启动日志，不重复告警
3. **fsync 定位不变**：降概率器，不是安全边界；安全边界是 P0-A

---

## 1. 架构简化：复用 SG1 `_ready` 闸门（裁定未覆盖的既有资产）

裁定建议的状态机（NORMAL → RECOVERY_DEGRADED → READY=False）**无需新建**——项目已有同构机制，实证如下：

| 实证点 | 位置 | 内容 |
|---|---|---|
| 默认 Fail-Closed | `trader_260725.py:178` | `self._ready = False  # SG1: READY 门控，默认 Fail-Closed` |
| 诊断字段 | `:179` | `self._not_ready_reason`（注释明示"仅诊断展示，永不参与安全判断"） |
| **唯一置位源** | `bot_runner.py:2397` | `trader._ready = True  # SG1: 唯一置位来源 = recover 明确返回 True` |
| 守卫 A（交易侧） | `trader_260725.py:2685` | `execute_signal` 内 `if not self._ready: 拒绝新信号` |
| 守卫 B（体验侧） | `bot_runner.py:2192` | 信号处理前 `if not trader._ready: 回复"系统未就绪"并 return` |

**结论**：D-009 只需保证 `recover_active_batches()` 在账本损坏时返回 `False`，即可让 `_ready` 永久保持 `False`，SG1 双守卫自动生效。**零新状态机、零新闸门、零对既有交易路径的侵入。**

（此点修正了裁定中"D-009 的启动状态应该明确成为 NORMAL / RECOVERY_DEGRADED"的表述——语义完全保留，实现上复用既有字段，不新增并行状态。）

---

## 2. P0-A：Fail-Closed 设计

### 2.1 核心改造点：`load_all_states` 必须区分三种"空"

当前实现（`trader_260725.py:1266-1273`）：

```python
def load_all_states(self) -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ 读取状态文件失败: {e}")
    return {}
```

**缺陷**：三种语义完全不同的情况统一返回 `{}`：

| 情形 | 语义 | 当前行为 | 应有行为 |
|---|---|---|---|
| 文件不存在 | 首次启动，合法空 | `{}` ✅ | `{}` + 正常启动 |
| 文件存在且内容为 `{}` | 合法空账本（无持仓） | `{}` ✅ | `{}` + 正常启动 |
| 文件存在但不可解析 / 根节点非 dict | **账本损坏** | `{}` 🔴 | `{}` + **置损坏标志**（返回值兼容，语义分离） |

这正是裁定测试清单第 7 项（"合法 `{}` 必须正常启动"）与第 1–6 项（损坏必须 Fail-Closed）能同时成立的技术前提。

**改造后**：

```python
def load_all_states(self) -> dict:
    """D-009 P0-A：状态读取唯一入口。
    返回值语义保持兼容（dict），损坏情形通过 self._state_corrupted 分离表达——
    因为"文件不存在 / 合法空账本 / 账本损坏"三者的安全含义完全不同：
    前两者 = 确认无持仓；后者 = 未知，绝不可当作"无"。
    """
    if not os.path.exists(STATE_FILE):
        self._state_corrupted = False
        return {}
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"根节点类型非法: {type(data).__name__}（期望 dict）")
        self._state_corrupted = False
        return data
    except Exception as e:
        self._state_corrupted = True
        self._state_corruption_detail = f"{type(e).__name__}: {e}"
        print(f"🚨 [D-009] trade_state.json 无法解析，账本不可信: {e}")
        return {}
```

**新增实例标志（`__init__` 区，紧邻 `:178` 的 SG1 字段）**：

```python
self._state_corrupted = False        # D-009 P0-A: 本地账本损坏（未知 ≠ 无）
self._state_corruption_detail = ""   # 诊断用，永不参与安全判断
self._tombstones_degraded = False    # D-009 Q3: 墓碑不可读，复活防护降级
```

### 2.2 拦截点：`recover_active_batches` 前置检查

在 `all_states = self.load_all_states()`（`:1664`）之后、进入 `for symbol, symbol_batches in all_states.items():`（`:1668`）之前插入：

```python
# 🔴 D-009 P0-A：账本损坏 → Fail-Closed，绝不把"不知道"当成"没有"
if getattr(self, '_state_corrupted', False):
    self._not_ready_reason = (
        "本地状态账本损坏（trade_state.json 不可解析），系统进入 BLIND-SAFE："
        "禁止新建仓位与一切依赖本地批次身份的自动操作")
    print(f"🚨 [D-009] 本地账本损坏，拒绝进入恢复流程: {self._state_corruption_detail}")
    self._run_position_census()          # P0-C：只读普查，绝不修改状态
    try:
        self.send_tg_notification(
            f"🚨【资金安全】本地状态账本损坏，系统进入 BLIND-SAFE\n"
            f"文件: `trade_state.json`\n"
            f"错误: `{self._state_corruption_detail[:200]}`\n"
            f"已禁止：新建仓位 / 自动平仓 / 保护单管理 / 任何依赖本地批次的操作\n"
            f"保留：Telegram 指令、交易所只读诊断、持仓普查结果（见下条）\n"
            f"处置：需人工核对交易所实际持仓与挂单后修复账本，勿直接删除文件。",
            level='critical')
    except Exception as _tg_e:
        print(f"⚠️ [D-009] BLIND-SAFE 告警发送失败: {_tg_e}")
    return False   # → bot_runner:2397 不置位 _ready → SG1 双守卫拦截
```

**为何返回 `False` 而非 `raise`**：`bot_runner.py:2395-2411` 已存在"recover 返回 False"的既有 Fail-Closed 分支（打印 + CRITICAL 告警 + 不置位）。返回 `False` 是**零改动复用**；抛异常会走到"重试 3 次耗尽"分支（`:2412-2425`），语义不符且延迟 20 秒。

**告警重复说明**（有意保留）：D-009 分支发精确告警后，`bot_runner` 的通用告警（"历史批次可能未正确恢复"）仍会发出。二者同 `critical` 级、内容互补。**符合项目既有哲学"不变量⑧ Fail-Closed but not Fail-Silent"——宁可重复，不可遗漏。**

### 2.3 权限边界逐条实证对照（裁定十条）

| # | 裁定要求 | 现有机制 / 实证 | 是否需新代码 |
|---|---|---|---|
| 1 | 禁止新建仓 | SG1：`_ready=False` → `trader_260725.py:2685` 拒绝新信号 | ❌ 零改动 |
| 2 | 禁止风险增加操作 | 同上；所有下单路径均经 `execute_signal` | ❌ 零改动 |
| 3 | 禁止依赖本地 batch identity 的自动交易动作 | **实证**：监控线程仅两处启动——`:1794`（在 recover 内，已 `return False` 不可达）、`:3074`（在 `execute_signal` 内，已被 SG1 拦截）。空 `all_states` 下**监控线程天然零启动** | ❌ 零改动 |
| 4 | 禁止自动创建/认领 batch | `recover_active_batches` 已 return；`.bak` 从不参与读取路径 | ❌ 零改动 |
| 5 | 禁止从 `.bak` 静默恢复 | 规格锁定：**`.bak` 永不进入任何自动读取路径**（阶段一+阶段二均不读） | ❌ 零改动（靠不动手） |
| 6 | 保留 Telegram / API / 只读诊断 | 进程不退出，`bot_runner` TG handler 与 `_safe_api_call` 均正常 | ❌ 零改动 |
| 7 | 可继续只读安全检查 | P0-C Position Census（§4） | ✅ 新增（只读） |

**结论：七条边界中六条由既有 SG1 + 空账本语义天然满足，D-009 只需新增一处前置检查 + 一处只读普查。**

> 裁定特别提醒："不要把'保留监控'理解成继续正常 batch monitor。" —— 已实证落实：trade_state 不可信时，原 batch monitor **一条都不会启动**。

---

## 3. P0-B：fsync 持久化（含平台降级）

### 3.1 改造点：`_persist_states`（`trader_260725.py:1277-1293`）

现状：`NamedTemporaryFile` 写 → `os.replace`，**无 fsync**（with 块退出时隐式 flush+close）。

改造后：

```python
with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
    json.dump(all_states, tf, indent=4, ensure_ascii=False)
    tf.flush()
    os.fsync(tf.fileno())          # D-009 P0-B：内容落盘，消除"截断为 0 字节"实测故障模式
    temp_name = tf.name
os.replace(temp_name, STATE_FILE)
```

### 3.2 `.bak` 保留但不强化（回应裁定"审视 .bak 持久化语义"）

裁定要求审视 `.bak`。**实证结论：不加强化，理由如下**：

1. `.bak` 用 `shutil.copy2`（`:1282`）非原子，**无法用 fsync 补救**——copy2 的失败模式是"复制到一半断电"，加 fsync 只在复制完成后才有意义，无法覆盖中途断电
2. 顺序本身安全：`copy2` 先于主写入，**该阶段断电只损坏 `.bak`，主文件仍完好**（旧值）——安全方向正确
3. **`.bak` 不是 last-known-good**（阶段一审计③实证：当前 `.bak` 含一个早已被清理的 `is_active=True` 幽灵批次，而主文件是 `{}`）
4. 阶段一 + 阶段二均**不读取** `.bak`

**规格决定**：`.bak` 维持现状作为**人工对账的证据副本**，明确写入注释——"仅供人工取证，任何自动路径不得读取"。不为它增加 fsync（增加代码复杂度而无安全收益）。

### 3.3 fsync 成本（阶段一实测，本机 200 次 / 1.7KB）

| 方案 | 单次 | 日成本（288 次/天/批次） |
|---|---|---|
| 现状 flush + replace | 2.300 ms | 1.71 s |
| **+ fsync(file)** | 3.119 ms（**+0.820 ms**） | 1.95 s（**+0.24 s**） |
| 现状 `.bak` copy2（每次都付） | 3.639 ms | — |

fsync 增量**比现有 `.bak` 复制还便宜一个数量级**。写入频率实测为事件驱动（监控周期 60–160s，14 处 save 全条件触发，空闲 16 小时零写入）。**成本可忽略。**

---

## 4. P0-C：Exchange Position Census（最小启动安全哨兵）

### 4.1 定位

严格遵循裁定：

> 目的只有一个：避免"本地账本消失后，程序连交易所是否还有真实仓位都不知道"。

**只读、一次性、启动期执行、绝不修改任何状态。**

### 4.2 实现规格

```python
def _run_position_census(self) -> None:
    """D-009 P0-C：启动期交易所持仓普查（READ-ONLY 安全哨兵）。

    触发条件：本地账本损坏，程序无法证明自己知道有哪些仓位。
    行为边界（裁定锁定，不得扩大）：
        允许：fetch_positions → 判断非零 → 告警
        禁止：自动创建 batch / 自动认领订单 / 自动挂 SL-TP / 自动平仓 / 依 .bak 恢复
    失败语义：API 不可达 = UNKNOWN，按"可能有仓"告警（UNKNOWN ≠ EMPTY）。
    """
```

**输出契约**（裁定给出的文案骨架，逐字落地）：

- 无仓位：
  ```
  [D-009] STATE CORRUPTION
  trade_state.json = INVALID

  [D-009] EXCHANGE POSITION CENSUS
  BTCUSDT LONG = 0
  BTCUSDT SHORT = 0

  System READY = False
  Reason = LOCAL_STATE_CORRUPTED
  Manual confirmation required
  ```
- 有仓位：追加 `🚨 CRITICAL` 前缀 + "禁止所有风险增加/自动管理操作，需要人工对账"
- API 失败：`UNKNOWN`，明确写"无法确认交易所持仓，按存在持仓处理"

**交易对范围**：取 `self.exchange.fetch_positions()` 全量，仅报告非零 `contracts`/`positionAmt` 的条目，不硬编码 symbol（避免遗漏非监控列表内的孤儿仓——这正是本哨兵的意义）。

### 4.3 不做什么（锁定）

不创建 batch、不认领订单、不挂 SL/TP、不平仓、不读 `.bak`、不写任何文件、不进入监控循环、不重试（单次，失败即按 UNKNOWN 告警）。

---

## 5. Q3：墓碑局部 Fail-Closed（裁定边界精确化）

### 5.1 现状实证

`_load_tombstones`（`:1406-1418`）docstring 明写：

> "缺文件/损坏 → 空 dict（读取 Fail-Open：墓碑是 Batch A 冻结之外的第二道防线，缺失最坏退化 = 回到无墓碑现状，**不新增风险面**）"

**该论证需修正**：退化不是"不新增风险面"，而是**退回到 Batch C 之前的风险面**（复活通道重新打开）。裁定正确指出："也不能简单：损坏 → {} → 当正常文件继续"。

失效路径实证（`:1306-1315`）：

```python
tombstones = self._load_tombstones()
t_entry = tombstones.get(batch_id)
if isinstance(t_entry, dict):        # 损坏时 → None
    ...
    if _age < TOMBSTONE_TTL_SECONDS:
        _tomb_alert = True
if not _tomb_alert:                  # 损坏时恒为 False
    ...继续写入...                    # 🔴 复活检测静默失效
```

### 5.2 精确化：区分"新建批次"与"已存在批次"

裁定要求"禁止依赖完整 tombstone 信息的自动清理/复活判断"，但未区分写入类型。**本规格按存在性证明来源精确化**：

| 写入类型 | 存在性由谁证明 | 墓碑 DEGRADED 时的处置 |
|---|---|---|
| **已存在 batch_id**（在 `all_states` 中） | 由 `trade_state` 证明，与墓碑无关 | ✅ **放行**（改 SL / 平仓记录 / registry 更新等正常路径不受影响） |
| **全新 batch_id** | 需墓碑排除"是 7 天 TTL 内已清理批次复活" | 🔴 **拒绝写入 + CRITICAL 告警**（无法证明 → 不允许建立新账本条目） |

**这个区分同时满足两条裁定要求**：
- "不因此强制 READY=False" → 已存在批次的正常管理全部放行，系统照常运行 ✅
- "禁止依赖 tombstone 完整性的复活判断" → 全新批次（复活的唯一形态）被 Fail-Closed 阻断 ✅

**副作用是可接受的**：墓碑 DEGRADED 期间无法新建仓位——这正是"禁止风险增加"的方向，与 Q1 精神一致，且范围严格限定在"新建"，不影响任何已有仓位的管理与保护。

### 5.3 实现点

1. `_load_tombstones`：损坏时设 `self._tombstones_degraded = True` + 告警（**不阻断启动，READY 正常**）；移除 docstring 中"不新增风险面"的错误论证
2. `save_batch_state`（`:1315` 之后、写入前）：新增 DEGRADED 分支，按 §5.2 表判定放行/拒绝
3. `clear_batch_state`（`:1367-1387`）写墓碑：**不受影响**（新建墓碑即可，无需读旧墓碑的完整性）
4. `_prune_tombstones`（`:1433`）：DEGRADED 时跳过 prune（无法安全判定 TTL）

---

## 6. RED 测试矩阵

对应裁定"特别要求新增测试覆盖"的 13 项，新增文件 `test_d009_state_persistence.py`：

| # | 用例 | 断言 |
|---|---|---|
| T1 | 0 字节 `trade_state.json` | `_state_corrupted=True`，recover 返回 False |
| T2 | 截断 JSON（`{"BTCUSDT": {"b1": {"is_act`） | 同上 |
| T3 | 非 JSON（纯文本乱写） | 同上 |
| T4 | 根节点非 dict（`[1,2,3]` / `"str"`） | 同上 |
| T5 | 主文件损坏 + `.bak` 完好 | **`.bak` 绝不被读取**；仍 Fail-Closed |
| T6 | 主文件损坏 + `.bak` 也损坏 | Fail-Closed + 告警 |
| T7 | 文件不存在（首次启动） | `_state_corrupted=False`，正常启动 |
| T8 | 合法 `{}` | `_state_corrupted=False`，正常启动，**READY 不得被误伤** |
| T9 | 损坏时 READY 恒 False | `_ready is False` 且 `_not_ready_reason` 非空 |
| T10 | 损坏时禁止风险增加 | `execute_signal` 被 SG1 拦截，返回拒绝 |
| T11 | 损坏时触发 Position Census | `fetch_positions` 被调用（**对照缺陷：修复前根本不调用**） |
| T12 | 交易所存在仓位 → CRITICAL | 告警含"CRITICAL"且含仓位数量 |
| T13 | `.bak` 绝不静默恢复为正常 READY | 断言 `.bak` 读取次数 = 0（mock `open` 计数） |
| T14 | tombstone 损坏只产生 DEGRADED | READY 正常、新批次被拒、已有批次更新放行 |
| T15 | fsync 被调用 | `_persist_states` 后断言 `os.fsync` 被调用 ≥1 次 |

> T11 是核心回归锚点：修复前 `fetch_positions`（`:1688`）位于循环体内，空账本导致**根本不执行**；修复后必须被调用。

---

## 7. 实施顺序（照裁定九步，本规格细化）

1. ✅ 阶段一审计（已完成，报告已送审）
2. ✅ 写规格 v2（本文件）
3. **RED**：新建 `test_d009_state_persistence.py`，跑出预期失败（T1-T6、T9-T15 应失败）
4. **P0-A**：`load_all_states` 三态分离 + `__init__` 标志 + `recover_active_batches` 前置拦截
5. **P0-B**：`_persist_states` 加 `flush` + `fsync(file)` + `_fsync_dir`（降级）
6. **P0-C**：`_run_position_census` 只读普查
7. **Q3**：`_load_tombstones` DEGRADED 标志 + `save_batch_state` 新建/已存在分治
8. **GREEN + 全量回归**（34 文件 + 新增 D-009）
9. **git diff 审计**（对照备份逐块核对纯插码）→ **一次性 commit**

---

## 8. 范围冻结（不做什么）

- ❌ 不引入完整 reconciliation（交易所 ↔ 本地一致性矩阵）——阶段二之后
- ❌ 不自动认领订单 / 不自动创建 batch / 不自动挂 SL-TP / 不自动平仓
- ❌ **不读取 `.bak`**（任何自动路径）
- ❌ 不动 Batch A/B/C 任何既有逻辑（尤其 `clear_batch_state` 的 proof 门）
- ❌ 不引入 `ctypes` / 平台原生调用（§0.2）
- ❌ 不修改 SG1 `_ready` 既有语义与置位点
- ❌ 不扩大 BLIND-SAFE 到 Batch B 的收敛链（converge 只服务已存在批次，损坏时无可收敛对象）

---

## 9. 待回呈确认项

| # | 问题 | 本规格倾向 |
|---|---|---|
| **R1** | `fsync(directory)` Windows 不可行 → 接受"文件 fsync + 目录尽力降级"？ | 倾向接受（P0-A 才是安全边界） |
| **R2** | Q3 精确化为"全新批次拒绝 / 已存在批次放行"是否符合裁定本意？ | 倾向符合（同时满足两条要求） |
| **R3** | `.bak` 不予 fsync 强化、仅作人工取证副本，是否同意？ | 倾向同意（copy2 非原子，fsync 无覆盖价值） |
| **R4** | 是否接受"复用 SG1 `_ready`"替代新建状态机？ | 倾向接受（语义等价，零侵入） |
