# D-004 事故档案：Watchdog 重复启动风暴

> 归档时间：2026-08-20 23:28 事发 / 2026-08-21 00:0x 归档
> 来源：实盘恢复演练 Step 2（互斥体验证）真实触发——比人工设计的故障注入价值更高
> 严重度：⭐⭐⭐⭐⭐（守护进程层故障隔离缺失）
> 处置状态：已修复（Watchdog 安全补丁 v1），修复后须重做 Step 2 演练

**事件定性（ChatGPT 终审确认）**：本事故**不是单实例互斥失败**——互斥体全程正常工作。准确定性是：**互斥成功，但拒绝路径不安全**（拒绝分支自身崩溃），并被守护进程无熔断的无限重启放大为系统级通知风暴。审计时勿误判为"双实例保护失效"。

---

## 一、时间线（双端日志交叉还原）

| 时刻 | 实例 | 事件 |
|---|---|---|
| 23:23:58 | PyCharm watchdog（实例1，健康） | 启动，bot_runner READY，接管 batch_20260820_230534_7d6772 |
| 23:28:35 | PowerShell watchdog（实例2，演练注入） | 启动，拉起第二个 bot_runner（PID 4112） |
| 23:28:40 | bot_runner#2 | 撞单实例互斥体 → `print("❌ 检测到另一个 Bot 实例...")` → **UnicodeEncodeError**（GBK 无法编码 U+274C）→ 异常退出码 1（设计应为 42） |
| 23:28:41 起 | watchdog#2 | 误判"程序崩溃"→ 无限重启循环（约 8s/轮：5s 初始化等待 + 3s 间隔），连续 7+ 轮 |
| 23:28:48 起 | 实例1（旁观受害） | 两个 watchdog 共用 `.notify` 文件 → 实例1 消费每条 crash_alert → **TG + QQ 邮件风暴**（TG 每轮 1 条、邮件每 2 轮 1 条，持续 ~4 分钟） |
| 23:31+ | 用户 | 手动停止 PowerShell watchdog，风暴终止 |

## 二、预期 vs 实际

| 环节 | 设计预期（watchdog.py L224-229） | 实际 |
|---|---|---|
| 互斥体检测 | 检测到已存在 → 打印拒绝 → `sys.exit(42)` | ✅ 互斥体**正常工作**（到达了拒绝分支） |
| 拒绝提示输出 | 正常打印 | ❌ `UnicodeEncodeError`，死在 print 上 |
| 退出码 | 42 | ❌ 1（未执行到 sys.exit） |
| watchdog 判定 | 识别 42 → 停止自身，不重启 | ❌ 退出码 1 → 误判崩溃 → 无限重启 |
| 告警 | 0 条（正常拒绝非事件） | ❌ 每轮 1 条 crash_alert → 风暴 |

## 三、根因（三层独立缺陷，任一单独存在都不会成灾）

1. **根因1（编码）**：bot_runner 被 watchdog 以 `stdout=subprocess.PIPE` 接管后，其 stdout 是**非交互流**，Python 的 TextIOWrapper 编码取决于运行环境与 Windows locale（本机默认继承 cp936/GBK），与控制台代码页无关——不能因为"用户 PowerShell 控制台显示正常"（交互控制台流走 WindowsConsoleIO/UTF-8）推断子进程管道安全。`❌`（U+274C）不在 GBK 中 → 拒绝路径 print 抛异常。**拒绝/告警路径必须比正常路径更稳定**——这是本次最重要的工程教训。（终审措辞修订：核心不是"PIPE 改变编码"，而是"非交互 stdout + Windows locale 默认 ANSI，与预期 UTF-8 不一致"）
2. **根因2（无熔断）**：watchdog 主循环 `while True` 对"子进程退出"一律重启，无失败计数、无窗口判定、无上限。任何持续启动失败（依赖损坏、API 初始化失败、状态文件损坏）都会演变成无限循环。
3. **根因3（无去重）**：crash_alert 每轮重启都写 `.notify`。此前修复的 `_gate_alert_notify`（3 次限制）只覆盖**交易层**，不覆盖**进程生命周期层**——告警治理存在范围盲区。

## 四、影响面评估

- 资金安全：**无影响**。batch_20260820_230534_7d6772 四层入场单（触发价 ~73103）全部 CONFIRMED 未成交，`pending_sl_orders [0,1,2,3]` 为"成交后才挂 SL"的正常待办，无裸仓。
- API 配额：第二个 bot_runner 均在初始化早期退出，未到达交易所调用阶段，无重复下单风险。
- 通知通道：TG + QQ 邮件被刷屏约 4 分钟（实例1 代发），可能掩盖真实告警。

## 五、修复（Watchdog 安全补丁 v1，随本档案一并提交）

| 编号 | 修复 | 位置 | 语义 |
|---|---|---|---|
| R1 | `make_stdout_crash_safe()`：`reconfigure(errors='replace')`，hasattr/try/None 三重保护 | bot_runner.py 模块头（实际崩溃点）+ watchdog.py main() 入口（预防） | 一次性修复**所有** emoji print 在非 UTF-8 环境的崩溃，含未来新增 |
| R2 | `record_process_exit(uptime)` 启动熔断 | watchdog.py 主循环 | 60s 初始化窗口内连续 **5** 次退出 → 1 条 critical（🚨【资金安全】前缀）+ watchdog 停止（退出码 1）；稳定运行 ≥60s → 计数清零（不误杀长运行后的正常崩溃重启） |
| R3 | `crash_alert_allowed(reason)` 同因去重 | watchdog.py is_crash 分支 | 同一 restart_reason **10 分钟**窗口只发 1 次；稳定运行后 `crash_alert_reset()` 解除（恢复后再崩重新提醒） |

**修复后的风暴推演**（W3 场景验证）：同一故障 → 第 1 轮 1 条 crash_alert + 第 5 轮 1 条熔断 critical → watchdog 停止。**总通知 2 条**（原风暴 ~10+ 条且不停止）。

## 六、测试证据

- 新增 `test_watchdog_guard.py` **22 场景全绿**：
  - W1（5）：真 GBK `TextIOWrapper` **再现** D-004 编码崩溃 → reconfigure 修复验证 → None/StringIO 兼容
  - W2（6）：去重语义（首条放行/同因静默/异因放行/窗口过期/恢复解除）
  - W3（5）：熔断计数（4 次不熔断/第 5 次熔断/稳定清零/连续性/清零联动解除 R3）
  - W4（6）：源码接入校验（两文件入口调用先于首个 emoji 字面量、主循环接线、orphan_guard 前置兼容）
- 全量回归：22 套件全绿（详见提交记录）
- test_orphan_guard 场景 4（watchdog AST 结构校验）确认兼容；场景 5（跨进程退出码 42）不受影响

## 七、重做 Step 2 演练的判定标准（修复后）

预期链：第二 watchdog 启动 → bot_runner#2 拒绝 print **不崩** → 退出码 **42** → 第二 watchdog 识别 → 杀进程树 → 干净退出（退出码 0）→ **零 crash_alert、零重启循环**。

- 通过：console 出现 `🚫 主程序因单实例锁拒绝启动`（watchdog 侧）+ bot_runner 侧拒绝提示正常显示（GBK 控制台下 emoji 显示为 ? 属正常，**不崩即可**）
- 失败判据：任何 UnicodeEncodeError / crash_alert 发送 / 第二 watchdog 存活超过一个初始化周期

## 八、经验沉淀

1. 演练即故障注入：本次"失败"的演练实际验证了互斥体有效性，并暴露了比 TP 事故更底层的守护进程缺陷层——**分层告警治理清单**（交易层 ✅ / 进程生命周期层 本档案补齐 / 其余待查：邮件通道限频？）
2. Python Windows 编码：管道流编码取本地 ANSI 代码页而非控制台代码页——"控制台显示正常"不代表"子进程管道安全"
3. 拒绝路径稳定性原则：任何"拒绝启动"分支（重复实例/配置错误/API key 缺失/状态损坏）不得依赖可能失败的输出操作
4. **R1 使用边界**：`make_stdout_crash_safe()` 的 `errors='replace'` 只适用于**展示流**（stdout/stderr 日志稳定性）。**严禁**将同类静默替换策略用于文件写入、交易参数、状态 JSON、订单 ID 等数据流——那些场景中非法字符必须显式失败而非被替换。当前实现仅 reconfigure 了 sys.stdout/stderr，未越界
5. **生命周期不变量 L1**：任何自动恢复机制不得无限重复制造自身故障。具体要求：① 有最大恢复边界（如 R2 的 60s×5 熔断）② 有失败状态通知（熔断时发 critical）③ 有人工接管入口（watchdog sys.exit 后由人决定下一步）。审计视角从单一交易层扩展为：交易层 → 状态层 → 进程层三层各自满足安全不变量
