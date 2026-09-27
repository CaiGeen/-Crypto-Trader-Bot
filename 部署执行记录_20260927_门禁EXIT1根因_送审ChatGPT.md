# 部署执行记录与门禁 EXIT=1 根因 —— 送审 ChatGPT（2026-09-27）

> **现场状态（截至本文件写入）**：
> - 生产 `G:\my-crypto-bot` `HEAD = b595156`（**已回滚到部署前基线**，`trader` blob 与 `b595156` 完全一致）
> - **Bot 处于停机状态**（0 进程），心跳 `stopped=true` → `CryptoBot-HealthPatrol` 静默
> - **空仓、交易所无入场挂单**（执行前由操作者人工在交易所确认）
> - 账本 `trade_state.json` 全程未被改动：**14245 bytes / mtime 2026-09-23 23:07:05**
> - 双快照完好：`G:\_deploy_backup\20260927_135730\{01_pre_stop, 02_post_stop}`
> - 本轮**已按清单执行 → 门禁 EXIT=1 被阻断 → 按 §4 回滚**，未上线、未重启成功

---

## 1. 执行时间线（按编号步骤）

| 步骤 | 时间 | 动作 | 结果 |
| --- | --- | --- | --- |
| §2.1 | — | 本地 `ACTIVE_BATCHES=[]`（TOTAL=9 全 inactive）+ 人工交易所确认空仓无入场挂单 | ✅ 硬前置通过 |
| 步骤 0 | — | 预检 | ✅ 全绿（见 §2） |
| 步骤 1 | **13:57:30** | `$pre` 停机前快照 → `G:\_deploy_backup\20260927_135730\01_pre_stop` | ✅ 4 文件字节逐一相符；仓库内**无** `_deploy_backup` |
| 步骤 2 | **13:57:57** | 操作者在 `启动Bot.bat` 窗口 `Ctrl+C` | ✅ 进程清零、`watchdog.log` 走优雅路径、心跳 `stopped=true` |
| 步骤 2-④ | 13:57 后 | `$post` 停机后快照 + **解析备份件** | ✅ `BACKUP_PARSE_OK`；`01_pre_stop`(7 项) 与 `02_post_stop`(4 项) 并存 |
| 步骤 3 | 13:57 后 | `git merge --ff-only 884f721749d31f1a2a1e25a6c1270a26deade28d` | ✅ Fast-forward，`MATCH=True`，`git status` 仍只有 `?? AGENTS.md` |
| 步骤 4 | 13:57 后 | `run_test_gate.py --strict`（Bot 已停，**不带** `--allow-live`） | ❌ **EXIT=1，阻断启动** |
| §4-② | — | `git reset --hard b595156` | ✅ `MATCH=True`、`_update_registry_locked` 出现次数回到 0 |
| §4-④ | — | `run_test_gate.py --strict`（回滚后复验） | ✅ **EXIT=2 / FAIL 0 / NOT-VERIFIED 0** |
| §4-⑤ | — | 启动 | ⏸ **操作者选择暂不启动，保持停机** |

---

## 2. 步骤 0 预检记录（全绿）

```
git status                 = ?? AGENTS.md
生产 HEAD                  = b595156
git fetch                  = rc=0
$TARGET                    = 884f721749d31f1a2a1e25a6c1270a26deade28d
merge-base --is-ancestor   = rc=0   （可 --ff-only）
变更文件数                 = 25     （≥25）
a07ac6e..$TARGET 非 .md    = 0      （仅 3 个 .md；预检前新提交全是文档）
提交列表                   = 884f721 / 2dfa7d5 / f627d73 / 842b658 / 49d916e / be53c29（全 docs）
trade_state.json mtime     = 2026-09-23 23:07:05
venv / .env / gate / tests = True / True / True / 53
```

---

## 3. 步骤 4 门禁结果（部署后，`--strict`）

```
EXIT=1
pytest exit=0                                        ✅
脚本式 53 项：PASS 50 | BASELINE-FAIL 1 | NOT-VERIFIED 0 | BASELINE-DRIFT 0 | FAIL 2
  X FAIL rc=1  4.4s test_v64_p1_resize.py       （退出码超预期——真回归）
  X FAIL rc=1  4.8s test_v64_partial_close.py   （退出码超预期——真回归）
门禁结论：FAIL
```

> 亮点：**`NOT-VERIFIED` 由 1 → 0**，`test_orphan_guard.py` 在停机窗口内成功补验（清单第 4 条落地）。

---

## 4. 根因（逐条带证据）

### 4.1 失败内容

两个测试**同一个**错误文本：

```
test_v64_p1_resize.py       t01/t02/t04/t05  → GREEN: 6/10
test_v64_partial_close.py   t01/t04          → GREEN: 2/5
两者：AttributeError: 'StubTrader' object has no attribute '_update_registry_locked'
```

### 4.2 `_update_registry_locked` 是本轮新增

```
git log -S'_update_registry_locked' -- trader_260725.py
  → 1fbb546  C1/G1：7 个 SL 创建入口加意图落盘门禁（契约 §24.3 C1，未部署）

b595156 出现次数 = 0      （该方法当时不存在）
HEAD     出现次数 = 5      （def L5923 + return self._update_registry_locked(...) L5982 等）
```

### 4.3 测试替身没有跟上

`test_v64_partial_close.py`：

```python
class StubTrader:
    pass                      # 空类，方法靠 make_trader() 手工挂属性
def make_trader(states, ...):
    t = StubTrader()
    t._states = states; t._lock = threading.RLock(); ...
    def load_all_states(): ...          # 局部函数，随后挂到 t 上
    def _persist_states(all_states): ...
```

本轮让生产代码新调用了 `self._update_registry_locked(...)`，而 `StubTrader` 上没有该属性
→ 提取出的生产函数在 stub 上执行时抛 `AttributeError`。

**性质判断**：真实类里 `def _update_registry_locked`（L5923）存在，`pytest exit=0`、另 50 个脚本
PASS —— **无证据表明生产运行时有缺陷**；丢的是这 6 个子测对新路径的覆盖。

### 4.4 决定性：这两个测试硬编码读**生产目录**

```python
# test_v64_partial_close.py:18,20,22
TRADER_PATH    = r'G:\my-crypto-bot\trader_260725.py'
BOTRUNNER_PATH = r'G:\my-crypto-bot\bot_runner.py'
SRC = open(TRADER_PATH, encoding='utf-8').read()

# test_v64_p1_resize.py:22-29
import test_v64_partial_close as H
TRADER_PATH = H.TRADER_PATH ; SRC = H.SRC
BR_SRC = open(BOTRUNNER_PATH, encoding='utf-8').read()
```

它们是 **AST 解析 + `exec` 提取函数**跑逻辑（`import ast`），**不 import trader 模块**。

**这解释了一件必须纠正的事**：

> 🔴 **此前记录的「worktree 门禁 53 项 FAIL 0」是假绿。**
> 当时**生产目录还是 `b595156` 旧代码**，这两个脚本读到的是旧 trader → 通过；
> 而 worktree 自己的 `trader_260725.py` 是新代码，**根本不被它们读取**。
> 一合入生产，它们立刻转红；回滚后立刻转绿（见 §5）。

（我最初导出"基线"到临时目录实跑，也是同样的陷阱：临时目录里的 `trader_260725.py`
连 `parser` 模块都缺、import 必失败，测试却照常跑出结果 —— 正是因为它读的是绝对路径的生产文件。）

### 4.5 回滚后的对照（同环境、同门禁、仅代码不同）

```
git reset --hard b595156
  HEAD             = b595156
  git status       = ?? AGENTS.md
  trader blob      = fe0861c46257e16aaf91591485d227ab16a5687b（与 b595156:trader_260725.py 一致）
  _update_registry_locked 出现次数 = 0

run_test_gate.py --strict
  EXIT=2
  pytest exit=0
  脚本式 53 项：PASS 52 | BASELINE-FAIL 1 | NOT-VERIFIED 0 | BASELINE-DRIFT 0 | FAIL 0
```

`FAIL 2 → FAIL 0`，**坐实「本轮引入」**：`b595156` 里该符号为 0，基线不可能出现此错误。

---

## 5. 交叉核对：生产 vs worktree

```
trader_260725.py       SHA256 不同 → normalized_same=True（仅 CRLF/LF）
test_v64_p1_resize.py  SHA256 不同 → normalized_same=True
run_test_gate.py       SHA256 不同 → normalized_same=True
test_v64_partial_close.py  SHA256 相同
```

**代码内容完全一致**，两处手工跑 `test_v64_p1_resize.py` 都是 `wt_rc=1 / GREEN: 6/10`
—— 与 4.4 的结论自洽（都在读同一份生产文件）。

---

## 6. 未受影响 / 未变更项（如实登记）

- **账本全程未被污染**：`14245 bytes / 2026-09-23 23:07:05`，部署前后、多轮测试跑完均未变
- **`.env`、保护参数、冻结开关、真实单据**：未触碰
- 备份双快照在仓库外，`git status` 始终只有 `?? AGENTS.md`
- 门禁哨兵（运行期间生产状态文件变化）**未触发**
- 本轮**未上线**：生产回到 `b595156`

---

## 7. 待裁定问题（送审 ChatGPT）

1. **定性**：`test_v64_p1_resize.py` / `test_v64_partial_close.py` 的 FAIL
   —— 按 4.5 应判为**本轮引入的回归**（基线绿 / 新码红）。**你是否认可这个定性？**
   还是认为应登记为基线项（我理解不应：基线它俩是绿的）？
2. **测试与生产路径硬耦合**：`TRADER_PATH = r'G:\my-crypto-bot\trader_260725.py'`
   使这两个脚本**永远测生产目录里的那份文件**，worktree 里的新代码对它们不可见。
   这是否是**既有缺陷**（早于本轮）？是否应当另立契约修掉，而不是只补 stub？
3. **门禁哨兵**：`run_test_gate.py` 是否应加一条 ——
   **脚本硬编码指向生产目录路径时告警/拒绝**，否则任何"worktree 门禁全绿"都可能是假绿。
   这条要不要立？
4. **修法**：给 `StubTrader` 补语义正确的 `_update_registry_locked`
   （参照 `make_trader` 已挂的 `load_all_states` / `_persist_states` 写法）。
   ⚠️ 我判断**不能**简单 `return None` —— `t01` cancel-verify、`t02` stage skip、
   `t04` durable price、`t05` per-leg commit 都对注册表返回值有断言，
   **空实现会让测试空转通过（vacuous green），比红更危险**。
   请裁定：正确实现应怎么写？是否需要复审后才能再开部署窗口？
5. **6 个子测的覆盖丢失**：在 stub 修好之前，新路径（`_update_registry_locked` 调用链）
   **没有任何自动化覆盖**。是否接受在补齐前重新部署？我的意见是**不接受**。
6. **重开窗口**：修测 → 复审 → 重新走 §2.1 敞口核对 + 步骤 0-4？
   还是需要先把 2/3 两条结构性问题一并处理？

---

## 8. 当前需要人工决策的一件事

**Bot 仍停机**（空仓、无敞口、巡检静默，**无资金风险，但也没有交易能力**）。
是否恢复运行，由操作者决定：
- 恢复 = 双击 `G:\my-crypto-bot\启动Bot.bat`（当前 `HEAD = b595156`、门禁 `EXIT=2`，属已知良好状态）
- 或保持停机等待第 7 节裁定
