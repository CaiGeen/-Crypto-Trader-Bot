# P0 通知降级上线验证报告（干净重启）送审 ChatGPT

> 生成时间：2026-08-29 17:15（GMT+8）
> 验证对象：commit `e953d79`「P0 通知可靠性：safe_reply 增加 Markdown → 纯文本降级」
> 重启方式：**人工干净重启**（watchdog 先清理旧进程树，再启动新实例）
> 取证方式：**全部只读**（除一次交易所只读查询 open orders，未调用任何写操作）

---

## 一、结论速览

| ChatGPT 要求的验收项 | 结果 | 证据 |
|---|---|---|
| 2 个活跃批次 | ✅ | `trade_state.json` 1 symbol / 2 批次 / 活跃 2 |
| 原来的 4 个未触发开仓单 | ✅（实为 6 个） | 交易所条件单 6 个，ID 与挂单时逐位一致 |
| **没有重复挂单** | ✅ | 双向差集均为空（见第四节） |
| 恢复监控 | ✅ | 两个批次各启动监控线程，活跃监控数 2 |
| READY | ✅ | 终端 `✅ [启动检测] 历史任务恢复校验完成！系统 READY` |
| P0 代码被运行实例加载 | ✅ | 时间序 + 源码一致性三重证据（见第二节） |
| **P0 运行期实际生效** | ⏳ **待一次 D-005 触发实测** | 需真实 Markdown 失败才能观测 |

---

## 二、P0 代码确已被加载（三重证据）

### 证据 1：源码与 HEAD 一致

```
$ git log --oneline -1
e953d79 P0 通知可靠性：safe_reply 增加 Markdown -> 纯文本降级

$ git diff --stat HEAD -- bot_runner.py
（空）
```

> 备注：磁盘 md5 `84b6ea8a…` 与 `git show HEAD:bot_runner.py` md5 `46cafb2a…` 不同，
> 原因是 `core.autocrlf=true` 的 CRLF 转换，**不是**代码不一致；`git diff HEAD` 为空才是判据。

磁盘确认含 P0 实现：

- `_strip_markdown()` — L549
- 降级分支 — L573（`plain = _strip_markdown(text)`）
- 降级日志 — L580（`ℹ️ [TG回复] Markdown 解析失败，已降级纯文本发送`）

### 证据 2：进程身份成链

```
PID 20044  17:10:07  .venv python.exe  watchdog.py        （启动器）
PID 21396  17:10:07  Python311         watchdog.py        （子进程）
PID 12240  17:10:07  .venv python.exe  bot_runner.py      （启动器 ← watchdog.log 所记）
PID 9144   17:10:07  Python311         bot_runner.py      （子进程）
```

watchdog.log 对应记录：

```
[17:10:07] 👋 用户手动停止 Watchdog
[17:10:07] 🧹 正在清理主程序进程树 (PID: 13512)...
[17:10:07] ✅ 主程序进程树已清理
[17:10:07] 🚀 启动主程序...
[17:10:07] ✅ 主程序已启动 (PID: 12240)
```

### 证据 3：时间序（关键）

```
bot_runner.py mtime = 2026-08-29 16:31:02
进程启动时刻   = 2026-08-29 17:10:07        ← 晚于文件定稿 39 分钟
```

`bot_runner.py` 以 `__main__` 身份运行，**PEP552 下不生成也不读取 pyc**，启动时直接读源文件。
文件在进程启动前 39 分钟定稿、期间 `git diff` 为空 → **运行实例加载的必然是含 P0 的源码**。

> 对比：上次 D-009 验证可以用 `trader_260725.cpython-311.pyc` 头部做硬证据，
> 因为 trader 是被 import 的模块；本次 P0 只改了 `__main__` 脚本，故改用时间序 + git 一致性。

---

## 三、启动恢复链

```
📊 活跃批次: 2 个
🔍 [恢复前健康检查] 正在验证交易所连接... ✅ 通过
🔄 [状态恢复] 识别到未完成的历史活跃任务 [batch_...155232_f49f2e] → ✅ 有效，接管 → 杠杆重设 100x
🔄 [状态恢复] 识别到未完成的历史活跃任务 [batch_...155343_cfdf77] → ✅ 有效，接管 → 杠杆重设 100x
✅ [状态恢复] 恢复流程完成，共接管 2 个历史活跃批次
✅ [启动检测] 历史任务恢复校验完成！系统 READY
```

- 无 `_state_corrupted`、无 `🚫`、无 `🚨` → D-009 Fail-Closed 未误触发
- 两个批次监控线程均已启动（活跃监控数 2）

---

## 四、无重复挂单 —— 交易所侧逐单核对

新增只读核对器 `verify_no_duplicate_orders.py`（仅 `fetch_open_orders` 普通端点 + `stop=True` 条件单端点，
绝不调用 create/cancel），结果：

```
[1] 账本
    批次 batch_20260829_155232_f49f2e: 层级 4 | is_active=True | filled=0
    批次 batch_20260829_155343_cfdf77: 层级 2 | is_active=True | filled=0
    账本 entry_order_id 合计: 6

[2] 交易所 BTCUSDT（只读）
    条件单(algo): 6
      - 3000002163649040  market buy  0.001
      - 3000002163649051  market buy  0.001
      - 3000002163649052  market buy  0.001
      - 3000002163649055  market buy  0.001
      - 3000002163649917  market buy  0.001
      - 3000002163649920  market buy  0.001
    普通单: 0
    持仓: 0

[4] 差集判定
    ✅ 交易所无任何账本外的条件单 → 无重复挂单、无孤儿单
    ✅ 账本记录的每一个 entry 单在交易所都能查到
rc=0
```

**关键点**：这 6 个 ID 与 15:52:32 / 15:53:43 **首次挂单时打印的 ID 完全相同**
（…649040/9051/9052/9055 属批次1，…649917/649920 属批次2）。
→ 恢复是**纯只读接管**，未向交易所重新挂出任何订单。

---

## 五、灰度检查器

```
$ .venv/Scripts/python.exe d009_gray_watch.py
[1] ✅ pyc 与源码逐位一致（trader_260725，预期 580f077）
[2] ✅ 账本合法（未损坏）→ 1 symbol / 2 批次 / 活跃 2；✅ 墓碑正常（1 条）
[3] .bak 17993B mtime=08-29 16:30:07（= last-state-before-this-write，非 last-known-good）
[4] ✅ 生产 python.exe 4 个，PID 20044/21396/12240/9144，全部 17:10:07
✅ 结论：全部正常，D-009 灰度观察进行中
rc=0
```

---

## 六、本轮两个新发现（仅记录，不改代码）

### 6.1「监控标记残留」是必然噪音，不是错误

重启日志中出现了两条：

```
└─ ⚠️ 批次 [batch_...155232_f49f2e] 监控标记残留，自动清理 (当前监控集合: {...f49f2e})
└─ ⚠️ 批次 [batch_...155343_cfdf77] 监控标记残留，自动清理 (当前监控集合: {...cfdf77, ...f49f2e})
```

源码实证为**双 add 设计**，非异常：

- `trader_260725.py` L2009-2013（恢复主流程，启动线程前）：`if in → discard → add`（占位，防并发重复启动）
- `trader_260725.py` L4544-4548（`_start_monitoring` 线程内）：`if in → discard → add`（幂等自愈）

因此**每次重启接管 N 个批次，必然打印 N 条该提示**。语义上 discard→add 幂等，集合最终正确。

> 判断：措辞（⚠️ / "残留"）有误导性，但行为正确。**按纪律只记录不修。**

### 6.2 D-005 EXECUTING 僵尸不会永久占槽（排除隐患）

去重表 7 条中有 2 条 `status=EXECUTING, batch_id=None`（含 16:06:15 被 D-006 拒的那单）。
源码核查（`bot_runner.py`）：

```python
SIGNAL_DEDUP_WINDOW_SEC = 600     # L2049
FORCE_APPROVAL_TTL_SEC  = 300     # L2051
age = now - rec.get('last_seen', 0)        # L2135
if age < SIGNAL_DEDUP_WINDOW_SEC:          # L2136 → 拦截
```

- **拦截分支不刷新 `last_seen`** → 反复重发被拦**不会**无限延长窗口，600 秒后自动解除
- `EXECUTING` 无 FAILED 态（L2150-2151 注释：干净失败与部分成交不可分，靠时间窗自解 + `/force` 人工裁决）→ 设计正确

---

## 七、尚未闭环的一项：P0 运行期实测

时间序证据已能证明"加载了 P0 代码"，但要证明"降级路径在生产真实触发并送达"，
需要一次真实的 Markdown 解析失败。

**触发方式**（不增加成交风险）：

1. 发一个**远离市价**的信号 → 建立批次3（不成交，无持仓风险）
2. **立即重发同参数信号** → D-005 命中 → `safe_reply` 发 Markdown → Telegram BadRequest → 降级纯文本
3. 观察终端是否出现：`ℹ️ [TG回复] Markdown 解析失败，已降级纯文本发送`
4. 观察 TG 是否**收到**拦截通知（修复前：完全收不到）

> 注意执行顺序：D-005 先于 D-006，因此即使批次已满，同指纹信号仍会先被 D-005 拦截。

**预期对照**：修复前（16:05:21）日志为
`ERROR - ⚠️ 回复消息给 Telegram 失败（BadRequest…）: Can't parse entities: can't find end of the entity starting at byte offset 139`
且 TG 无任何消息。

---

## 八、下一步：强杀异常重启测试（ChatGPT 第二步）

ChatGPT 明确区分：

| 类型 | 做法 | 本次 |
|---|---|---|
| 干净重启 | 停止 bot_runner → 重新启动 | ✅ 已完成（本报告） |
| **异常重启** | watchdog 保持运行 → 强杀 bot_runner → watchdog 自动拉起 | ⏳ 待做 |

**建议步骤**：

1. 保持 watchdog（PID 20044/21396）运行
2. 强杀 bot_runner 进程树：`taskkill /PID 12240 /T /F`
3. watchdog 应在数秒内检测到并自动拉起新的 bot_runner
4. 验证清单：
   - [ ] watchdog.log 出现崩溃检测 + 自动重启记录，**新 PID ≠ 12240**
   - [ ] 启动时 `活跃批次: 2 个`，接管两个批次，READY
   - [ ] 再次运行 `verify_no_duplicate_orders.py` → 仍 rc=0（无重复挂单）
   - [ ] `d009_gray_watch.py` → rc=0
   - [ ] 无 `🚨【资金安全】` 告警

> 说明：这仍不是真实断电，但比干净重启更接近 D-009 要防的场景
> （进程被外力突然终止 → 内存中未落盘的状态全部丢失 → 磁盘账本必须自证完整）。

---

## 九、证据复现命令（全部只读）

```bash
# 灰度检查器
.venv/Scripts/python.exe d009_gray_watch.py

# 无重复挂单核对（交易所只读 + 账本差集）
.venv/Scripts/python.exe verify_no_duplicate_orders.py

# P0 专项测试（GREEN）；指向改前备份即为 RED
.venv/Scripts/python.exe test_tg_reply_fallback.py
TG_FALLBACK_TARGET=backups/p0_tg_fallback_20260829_163026/bot_runner.py \
    .venv/Scripts/python.exe test_tg_reply_fallback.py
```
