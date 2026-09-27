# 部署执行记录与门禁 EXIT=1 根因 —— 送审 ChatGPT（2026-09-27）

> **现场状态（截至本文件更新）**：
> - 生产 `G:\my-crypto-bot` `HEAD = b595156`（**已回滚到部署前基线**，`trader` blob 与 `b595156` 完全一致）
> - **Bot 已按 §4⑤ 重启恢复运行**（2026-09-27 **15:27:53**，4 进程，`READY`，`stopped=false`，
>   `bot_alive=true`，`restarts=0`；`活跃批次 0`、`Binance API ✅`）—— 回滚收尾已完成，不再长期停机
> - **空仓、交易所无入场挂单**（执行前由操作者人工在交易所确认）
> - 账本 `trade_state.json` 全程未被改动：**14245 bytes / mtime 2026-09-23 23:07:05**
> - 双快照完好：`G:\_deploy_backup\20260927_135730\{01_pre_stop, 02_post_stop}`
> - 本轮**已按清单执行 → 门禁 EXIT=1 被阻断 → 按 §4 回滚 → §4⑤ 重启验收通过**，**未上线**
> - **定点修复已完成**（见 §9），worktree 门禁 `EXIT=2 / FAIL 0`；`$TARGET` 现为 `5c4d2fc`
> - ✅ **清单复审③ 已确认（附两条收窄，本版已落实）** —— 清单**可执行**。
>   下一个窗口前置：**重新选空仓窗口 + 现场核对交易所敞口 + 不带 `--allow-live` 的 `--strict` 门禁**

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

## 8. 恢复运行（已完成）

**操作者已于 15:27 重启，§4⑤ 收尾完成** —— 见下节验收。不再长期停机。

- 恢复 = 双击 `G:\my-crypto-bot\启动Bot.bat`（`HEAD = b595156`、门禁 `EXIT=2`，属已知良好状态）
- ChatGPT 明确：**不必为等六个问题的答案而继续停机**，`stopped=true` 让巡检静默**不能当作正常待步**。

### §4⑤ / 步骤 6 启动验收（2026-09-27 15:27）

| 判据 | 结果 |
| --- | --- |
| 进程 | 4 个（watchdog 43448/27444 + bot_runner 46568/18688），`start=15:27:53` ✅ |
| READY | `[15:27:58] ✅ [启动检测] 历史任务恢复校验完成！系统 READY` ✅ |
| 安全检查 | `MAX_LEVERAGE 100x ✅` / `Binance API ✅` / **`活跃批次 0`** ✅ |
| 心跳 | `stopped=false`、`bot_alive=true`、`restarts=0` ✅ |
| HEAD / 代码 | `b595156`，`trader` blob `fe0861c4…` 与基线一致 ✅ |
| 账本 | `14245 / 2026-09-23 23:07:05` 未变 ✅ |

---

## 9. ChatGPT 裁定 + 定点修复执行记录

### 9.1 ChatGPT 对 §7 六问的裁定

1. **定性认可**：这是**本轮触发的测试回归 + 既有测试路径缺陷**，**不能登记成基线失败**。
   同时**明确**：**不能仅凭真实类有该方法就宣称新代码的运行路径已验证。**
2. **修正它自己的复审口径**：它此前接受 worktree 门禁结果时**没核对这两个脚本实际读取的源码路径**，
   因而**高估了"全绿"对待部署代码的证明力** —— 是**它的审核遗漏**，不只是执行侧的问题。
3. **Q3 哨兵**：**暂不加**全库"硬编码路径扫描哨兵"，理由是"已知盲区就在这两个脚本"，
   先修路径和保真度即可。 → ⚠️ **该前提不成立，见 9.2。**
4. **Q4 修法**：绑**真实** `_update_registry_locked` 及实际调用所需 helper，
   沿用现有读写状态桩，**不写 `return None` 空实现**。
5. **Q6 重开窗口**：修测 → 隔离 worktree 重跑确认断言确实执行 → 跑门禁 →
   **通过后重新选择空仓窗口、重新核对交易所敞口再部署**。

### 9.2 ⚠️ 对 ChatGPT Q3 前提的更正：盲区是 **4 文件 10 处**，不是 2 个脚本

全库扫描 `G:\my-crypto-bot\` 硬编码（所有 `test_*.py`）：

| 文件 | 处数 | 内容 | 本次处置 |
| --- | --- | --- | --- |
| `test_v64_partial_close.py` | 3 | L18-20 `TRADER_PATH`/`HELPER_PATH`/`BOTRUNNER_PATH` | ✅ 改 `__file__` 相对 |
| `test_v64_diag_fixes.py` | 2 | L15-16 `TRADER_PATH`/`BOTRUNNER_PATH` | ✅ 改 `__file__` 相对 |
| `test_p5_closecancel.py` | 4 | L412/685/710/874 内联 `open(r'G:\my-crypto-bot\trader_260725.py')` | ✅ 改 `SRC_PATH` |
| `test_close_confirmation_v3.py` | 1 | L26 `sys.path.insert(.venv\Lib\site-packages)` | ⏸ **另一类，未改** |

- `test_p5_closecancel.py` 最能说明危害：它 `import trader_260725`（读 **CWD**）却 `open`
  **生产源码** —— **同一个测试里两份代码**。
- L26 那处指向的是**第三方库目录**（两目录共用同一 `.venv`），**不参与源码选取，不会造成假绿**，
  故本次不动，**留给裁定**。
- **未加扫描哨兵**（遵从 ChatGPT 决定），但**本次是人工全库扫描**，不是抽样。

### 9.3 修复 1：路径改读本仓库

`_HERE = os.path.dirname(os.path.abspath(__file__))` 拼接，`test_p5_closecancel.py` 提为模块级 `SRC_PATH`。
**生产侧行为不变**（`_HERE` 即生产目录）；worktree 侧从此读 worktree。

### 9.4 修复 2：绑真实实现，不写空实现

`1fbb546`(C1/G1) 把 `_update_registry`（trader L6812）改成**委托** `_update_registry_locked`（L6742），
而桩 `BINDS` 名单只有前者 → 委托即 `AttributeError`。

真实实现只依赖 **`self._state_lock` / `load_all_states` / `既有 `_persist_states` / `time`** ——
**四样桩里全都现成**（`make_trader` L163/L194/L198、`NS` L57），故直接把**真实实现**加进 `F` 名单，
走既有 `types.MethodType` 绑定循环（L212-213），读写仍走桩的状态桩。
**没有写空实现** —— 与 Q4 一致。

### 9.5 修复 3：重跑验证（worktree，**读的是 worktree 分支代码**）

```
TRADER_PATH = G:\my-crypto-bot-wt\trader_260725.py    ← 已不再指向生产
test_v64_partial_close.py   2/5 → 5/5      rc=0
test_v64_p1_resize.py       6/10 → 10/10   rc=0
test_v64_diag_fixes.py           6/6       rc=0
test_p5_closecancel.py           37/37     rc=0

run_test_gate.py --allow-live（worktree 预检，Bot 在跑）
EXIT=2 | pytest exit=0 | PASS 51 | BASELINE-FAIL 1 | NOT-VERIFIED 1 | FAIL 0
（NOT-VERIFIED 1 = test_orphan_guard 需停机窗口，非回归）
```

**红→绿对照**（去掉绑定即回到 6/10）证明**断言是活的，不是空转** —— 对应 ChatGPT「确认原有
逐项断言确实执行」的要求。

提交：`738e1ca..2b4f89c`（3 文件 +23/−10），已 push。

### 9.6 步骤 0 判据修订（清单复审③ —— ✅ **已确认**，附两条收窄）

清单原判据「`a07ac6e..$TARGET` **非 `.md` 计数 = 0**，否则中止」。修复提交 `2b4f89c` 是 `.py`：

```
a07ac6e..2b4f89c 共 7 文件 = 4 *.md + 3 test_*.py
非 .md 计数 = 3                    → 旧判据会「中止部署」❌（误杀）
非 .md 且非 test_*.py = 0          → 当时提的条件
```

原则：**改判据而非绕过判据** —— 本意守住"不夹带生产代码改动"，测试文件不进运行时。

### 9.7 ChatGPT 对 `2b4f89c` 的复核 + 两条收窄（本版已全部落实）

**认可**：三处改读所在仓库源码、绑**真实** `_update_registry_locked`、**未用空实现掩盖失败**；
第四处硬编码指向第三方库，与"误读生产源码"不同，**本批次可不动**；
**承认其"盲区只在两个脚本"的范围判断过窄，我方更正成立**；
§4⑤ 恢复运行**正确**。

**但放行范围再收窄两条**：

1. **白名单，不豁免整个 `test_*.py`** ——
   ❌ 我上一版写的是「非 `.md` 且非 `test_*.py` = 0 + 10 个运行时文件逐项 = 0」。
   ChatGPT 否决：**豁免所有 `test_*.py` 会让预检前新增的任意测试改动被自动视为已审**。
   ✅ 最终口径：**只放行 `*.md` + 本次已审的 3 个测试文件**
   （`test_v64_partial_close.py` / `test_v64_diag_fixes.py` / `test_p5_closecancel.py`）。
   **白名单天然覆盖"零夹带"，正好省掉重复列举 10 个运行时文件那条判据。**
2. **`.env` 不能算进 Git 证明** ——
   ❌ 我上一版把 `.env` 列进"运行时 10 项全零"。
   ChatGPT 指出：**`.env` 是未跟踪文件，`git diff` 对它恒为 0，根本证明不了它没变化**，
   是**假证据**。
   ✅ 剔出 Git 证明，改由**现场配置核对**：步骤 0 记 `Get-FileHash .env`（SHA256）+ mtime，
   **新增步骤 5 判据 ⑥** 启动前复核一致。

**改后实测（新 `$TARGET = 5c4d2fc`）**：

```
a07ac6e..$TARGET 共 7 文件
白名单（*.md + 3 个已审测试）外计数 = 0        ✅
```

> **ChatGPT 裁决：这两处是判据和措辞的定点修正，无需再扩测试或开启全面复审。
> 修正后重新选择空仓窗口，现场核对交易所敞口，并以不带 `--allow-live` 的
> 离线 `--strict` 门禁决定是否启动新版本。**
