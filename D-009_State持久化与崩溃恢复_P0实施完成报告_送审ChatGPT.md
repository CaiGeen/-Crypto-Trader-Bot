# D-009 P0 实施完成报告 —— 送审 ChatGPT 终审

- **提交**：`580f077`（7 files changed, +761 / -49），前一批 `c35f014`（Batch B 封版）
- **基线备份**：`backups/d009_p0_20260829_141705/`（`trader_260725.py` + `bot_runner.py`，md5 校验一致）
- **裁定依据**：D-009 阶段二最终裁定 R1–R5 全部批准，授权直接进入第 4–7 步
- **日期**：2026-08-29

---

## 一、五项硬性验收条件逐条落实

| # | 条件 | 落实 |
|---|---|---|
| 1 | 改码前保存 `c35f014` 干净基线 | ✅ 改前 `git status` 生产代码零 `M`；备份目录 md5 比对一致 |
| 2 | 只改 trade_state + tombstone，不做全库 fsync 重构 | ✅ 仅 `trader_260725.py`(+282/-49) 与 `bot_runner.py`(+8/-2)；**未触碰** `.notify_queue` / `auth_blocked` / `trade_stats` / `.notify.state` / `signal_dedup` |
| 3 | `.bak` 永不静默恢复 | ✅ 仅 `os.path.exists` + `os.path.getsize` 做存在性诊断，**全路径零 `open()`**；测试 T5 以 `builtins.open` 探针断言 `.bak` 读取次数 = 0 |
| 4 | trade_state 损坏 Fail-Closed 且 `_ready=False` | ✅ 复用 SG1 `_ready`，未新建状态机；T9/T10 双断言 |
| 5 | RED→GREEN + 全量回归 + AST 扫描 + diff 审计 | ✅ 16/16 rc=0；35 文件 34/35 exit=0；AST 三层扫描；diff 20 块逐块核验 |

---

## 二、R1–R5 逐条落实

### R1 — Windows 目录 fsync 降级：已按裁定实现

```python
def _fsync_dir(dir_path: str) -> bool:
    try:
        fd = os.open(dir_path, getattr(os, 'O_DIRECTORY', os.O_RDONLY))
    except (PermissionError, OSError, AttributeError):
        return False          # Windows 常态路径：静默降级，不刷屏
    ...
```

实测（本次复验）：`os.name = nt`、`hasattr(os, 'O_DIRECTORY') = False`、`_fsync_dir(真实目录) → False`，**不抛异常、不影响写入成败**。

写入链：`json.dump → tf.flush() → os.fsync(tf.fileno()) → os.replace() → _fsync_dir()`。
`_persist_states` 与 `_persist_tombstones` 两条链均已补全。

### R2 — 复用 SG1 `_ready`，不新建状态机：已按裁定实现

语义按你的措辞修正落地，**READY=False 不是"保留监控能力"**：

```
账本损坏 → 无法建立可信 batch census → 禁止恢复/接管任何批次
        → recover 返回 False → _ready 保持 False → 禁止任何风险增加
        → 进程继续存活 + 等待人工处理
```

`bot_runner` 唯一置位源 L2397 只在 `recovery_result` 为真时执行，损坏路径天然不置位。

### R3 — `.bak` 仅证据源：已按裁定实现

采纳你的建议，**第一版不把 `.bak` 内容装载进 `all_states`**。损坏分支只做：

```
备份诊断: trade_state.json.bak 存在（N 字节），仅可作为人工恢复候选证据，程序绝不自动装载
```

三种分支（不存在 / 存在非空 / 存在但为空）都产出诊断文案，且**三种情况下一律 READY=False + CRITICAL**。

### R4 — 墓碑 DEGRADED 分治：已按裁定实现

刻意与 trade_state 不同处理，两者安全角色不同：

| 损坏对象 | 标志 | 系统状态 | 已有批次 | 全新批次 |
|---|---|---|---|---|
| `trade_state.json` | `_state_corrupted` | `_ready = False`（全面停摆） | — | — |
| `trade_tombstones.json` | `_tombstones_degraded` | **保持 READY** | 放行 | 拒绝 |

判定依据即你给出的"存在性由谁证明"：已有 batch 的存在性由 `trade_state` 自证，墓碑非必要条件；全新 batch 需墓碑排除复活，损坏时无法排除 → 拒绝。
标志**非粘性**（每次 `_load_tombstones` 重新判定），人工修好后自动恢复。

### R5 — RED 进入 GREEN：已完成

`test_d009_state_persistence.py` **RED 2/16 rc=1 → GREEN 16/16 rc=0，一次通过**。

---

## 三、⚠️ 裁定外新增一项，需你复核

实施过程中发现一个审计阶段未识别的缺口，已一并修复：

**`_persist_states` 在 `_state_corrupted` 时拒绝覆盖写入。**

理由：损坏态下 `load_all_states` 返回的是 `{}`。若 `save_batch_state` 照常走写入路径，会把磁盘上残留的其余批次**一次性抹掉**——把"读取失败"升级成"证据灭失"，这是比原缺陷更严重的二次灾难。

```
损坏读出 {}  →  写回  →  磁盘残留批次全部消失  →  人工恢复的最后证据也没了
```

实现：拒绝 + CRITICAL 告警 + 返回 `False`（调用方忽略返回值，故为纯新增保护，无兼容面）。

人工修复账本后需重启进程（标志在每次 `load_all_states` 重判，但 `recover` 只在启动跑一次）——这是 Fail-Closed 的预期代价。

---

## 四、AST 风险入口扫描（你的第六节要求）

写成脚本做可达性证明，不是人工抽查：

**风险函数集**：8 个含 `create_order` 的函数。

**新建批次（风险增加）唯一可达路径**：
```
Telegram 命令 / JSON 信号 / 文件触发（5 处 create_task）
      ↓ 全部汇聚
run_trader_execution（bot_runner L2170）
      ↓ [SG1 守卫① L2192: not trader._ready → return]
trader.execute_signal（唯一调用点 L2251）
      ↓ [SG1 守卫② trader L2685: not self._ready → return None]
      ├─ create_order（首层开仓，L3164）
      └─ Thread(_start_monitoring) L3295
            └─ _place_prepared_orders_immediately L4736
                  └─ create_order ×3（加仓层）
```

**监控线程启动点全库仅 2 处**（AST 枚举所有 `threading.Thread(target=...)`）：
- L3295 `execute_signal` 内 —— 被 SG1 守卫② 拦住
- L2015 `recover_active_batches` 内 —— **本次 Fail-Closed 后，损坏分支在循环体之前 `return False`，零线程启动**

**存量操作**（改 TP / 改 SL / 保本 / 限价平仓 / 市价平仓）5 个函数，全部以 `load_all_states()` 查找批次为前置，账本损坏 → `{}` → 找不到批次 → 在 `create_order` 之前 `return False`：
`update_batch_tp` L2065 / `update_batch_sl` L2232 / `set_breakeven_sl` L2377 / `close_position_limit` L7489 / `close_position_market` L6962。

> **结论：无绕过闸门的新建批次入口。账本损坏时全部 `create_order` 路径不可达。**

---

## 五、测试与回归

| 项目 | 结果 |
|---|---|
| D-009 专项 | **16/16 PASS，rc=0**（RED 基线 2/16 rc=1） |
| 全量回归 | **35 文件 34/35 exit=0** |
| 非 0 项 | `test_orphan_guard` rc=42 —— 沙箱互斥体/taskkill 需提权，**历史既有环境怪癖，本批未改动该文件** |
| 真实 I/O 冒烟 | 8 项全过（脱离 mock，真实临时目录） |
| diff 审计 | 20 块（19+1）逐块核验；Batch C merge 逻辑逐行完整保留 |

修复的三类测试问题：

1. **`test_c_batch` TC6 与 Q3 新规格直接冲突 —— 规格演进导致测试过期。**
   旧语义"墓碑损坏视同空 → save 放行不阻断主流程"，测的正是被 R3 推翻的行为（损坏墓碑后 save 一个**全新**批次 `other_batch`，期望写入成功）。
   已按新规格拆为两项：**TC6** 全新批次拒绝（优雅拒绝 + critical，不抛异常）+ **TC6b** 已存在批次放行。22 项 → 23 项。

2. **SG1 场景4 真实回归（本批唯一一次生产代码回归）。**
   `bot_runner` 写 `if not getattr(trader, '_state_corrupted', False)` 时，MagicMock 未绑定属性返回 MagicMock（**恒 truthy**）→ 条件恒走 else → 吞掉 reason 更新。
   修复：改用 `getattr(...) is not True` 严格身份判定。**跨 MagicMock 边界读布尔标志一律不可用 truthy。**

3. **行号锚点统一 +222**（`test_sg4` / `test_t25` / `test_t26`）。
   AST 实测 14/14 位移集合为**单值**（因全部插码点都在最早锚点 L1936 之前）；`test_t25` 断言本体硬编码值同步改；`test_t26` 三个区间平移后逐段校验关键字命中。

---

## 六、性能实测（澄清审计阶段数据）

50 批次账本，N=60，分离测量：

| 配置 | 耗时 |
|---|---|
| 完整新版（copy2 .bak + flush + fsync） | 9.803 ms/次 |
| 去掉 fsync | 8.768 ms/次 |
| 去掉 .bak copy2 | 4.594 ms/次 |
| 两者都去 | 3.791 ms/次 |

- **D-009 实际新增 = +1.035 ms/次**（仅 fsync），日增约 0.3s，可忽略
- **`.bak` copy2 = +5.209 ms/次 是既有成本**，非本次引入（这也印证了你的第九节判断：`.bak` 的持久化强化不要为它扩大 P0）

---

## 七、明确未做（按裁定）

- 不做真实断电 T2（你明确指出不要为验证 D-009 冒文件系统风险）
- `.bak` 持久化强化留后续增强（第九节"少改"）
- 其余四文件 fsync 留待专门批次（第八节"不要顺手全项目重构"）
- 完整 reconciliation 自动化（Q2 阶段二）——本批只做只读普查，不自动修复

---

## 八、待办与观察项

1. **重启 watchdog 加载 `580f077`**（当前运行实例为 13:26 启动，加载的是 `c35f014`）。重启后须 PID + CreationDate + pyc source-mtime + `git log` 成链验证，不能只看 HEAD。
2. 重启后观察首次写入是否正常（fsync 链进入主路径）。
3. 可择机做一次**受控**损坏演练：备份后手工写坏 `trade_state.json` 重启，验证 CRITICAL 告警文案、Census 普查内容、`.bak` 诊断、READY=False 全链。

---

## 九、一句话总结

> 宁可因为账本不可信而停止交易，也绝不能因为账本损坏而把"未知"解释成"没有仓位"。

本次实施把这条原则从"设计意图"变成了代码路径上的硬约束，并用 AST 可达性证明堵住了闸门绕过的可能性。
