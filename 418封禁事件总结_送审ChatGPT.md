# 418 API 封禁事故总结（送审 ChatGPT）

> **用途**：供 ChatGPT 审查本次 API 熔断事件的起因、经过、诊断过程和修复动作是否完整合理。
> **生成时间**：2026-08-19 15:47 (GMT+8)
> **涉及文件**：watchdog.py、bot_runner.py、trader_260725.py

---

## 一、事件概要

| 项目 | 内容 |
|------|------|
| 事故类型 | Binance USDM 期货 API 418 IP 封禁 |
| 发生时间 | 2026-08-19 ~10:52（首次 418）→ ~12:03（重启延长封禁）→ ~12:14 封禁解除 |
| 影响范围 | 所有 API 调用被拒（fetch_open_orders / fetch_ticker / load_time_difference 等） |
| 资金影响 | 无（封禁期间无持仓操作需求，监控线程自动熔断暂停轮询） |
| 告警链 | R1 熔断告警状态机首次真实触发，critical TG + 邮件兜底均正常送达 |

---

## 二、起因分析（三因叠加）

### 因素 1：watchdog 停止路径漏洞（确凿源码证据）

**问题**：watchdog.py 的 KeyboardInterrupt 处理器直接 `sys.exit(0)`，**从不杀 bot_runner 子进程**。

- 杀子进程的代码（`process.terminate()`）只存在于"崩溃重启"和"定时重启"两条路径
- 用户日常重启习惯是 PyCharm Ctrl+F5（发 SIGINT/KeyboardInterrupt）
- 结果：Ctrl+F5 杀掉 watchdog 根进程，但 bot_runner 子进程成为**孤儿进程继续运行**
- 孤儿 bot_runner 仍在轮询 Telegram getUpdates + 执行监控循环 → 持续打 API
- 新实例启动后 = 双实例并行 = 双倍 API 消耗

**源码定位**：watchdog.py KeyboardInterrupt handler 原始代码——
```python
except KeyboardInterrupt:
    sys.exit(0)  # 从不调用 process.terminate()，不杀子进程
```

### 因素 2：手机 App 同 WiFi 共享 IP

- Binance 按 IP 地址计数请求配额
- 用户手机连同一 WiFi 用 Binance App 做手工交易
- App 的请求与 Bot 的请求共用同一 IP 配额
- 高峰时段（多个交易对行情刷新 + 下单）会挤占配额

### 因素 3：历史频繁重启 + 429 累积升级

- 当日因 Phase A/B 测试重启 ≥5 次
- 每次重启的启动链（fetch_time + load_markets + fetch_balance + fetch_positions + fetch_open_orders）= 一次性突发 5-8 请求
- 多次 429 退避后 Binance 升级为 418 IP 封禁（Binance 的递进惩罚机制）

**负载核实**：稳态监控 2 批次 ≈ 4 请求/分钟 < 限额 1%。**正常轮询不可能打爆**，问题全在叠加因素。

---

## 三、事件经过（时间线）

| 时间 (GMT+8) | 事件 |
|---|---|
| ~10:52 | 监控线程 fetch_open_orders 返回 418 "Way too many requests; IP banned until 12:06" |
| ~10:52 | R1 熔断告警状态机触发：critical TG 通知 + 邮件兜底发送 + `_api_cooldown_until` 设为 12:06 |
| ~10:52-12:06 | 监控线程进入熔断冷却（`_wait_for_api_cooldown` 循环 sleep），所有 API 调用被 `_safe_api_call` 拦截 |
| ~12:03 | 用户在封禁期内重启（还剩 ~3 分钟）→ 启动链 `fetch_time` 又触发 418 → **封禁延长至 12:14:51** |
| ~12:06 | 原定解除时间，但因 12:03 的延长，实际未解除 |
| ~12:14:51 | 封禁解除 |
| ~12:15 | 用户带 P0 修复重启（第一版：文件锁方案） |
| ~12:15 | watchdog 停止日志显示"🧹 清理进程树"→ taskkill /F /T 成功杀掉所有 python 进程 ✅ |
| ~12:15 | bot_runner 启动 → 单实例锁文件检测到"旧实例存活"（死 PID 被 Windows 复用）→ 错误拒绝启动 |
| ~12:15 | watchdog 把锁拒绝（退出码 1）当崩溃 → 3 秒无限重启循环 + 崩溃告警刷屏 |
| ~12:15-12:20 | 诊断 v1 缺陷，30 分钟内修复为 v2（命名互斥体方案） |
| ~12:20 | v2 重启成功，单实例锁正常获取 |
| ~15:27 | 用户报告双实例并存（4 个 python 进程）→ 互斥体 v2 ctypes bug 暴露 |
| ~15:33 | v3 修复（`use_last_error=True` + `ctypes.get_last_error()` + 显式 HANDLE 类型） |
| ~15:35 | v3 验收通过（跨进程子进程测试 5/5） |

---

## 四、诊断过程

### 4.1 封禁根因定位

1. **排除正常负载**：核实稳态 2 批次监控 ≈ 4 req/min，远低于限额 1% → 正常轮询不可能触发 418
2. **排查进程**：用户执行 `taskkill /F /IM python.exe /T` 清零后确认有孤儿进程残留
3. **源码审计**：发现 watchdog KeyboardInterrupt 路径不杀子进程（确凿源码证据）
4. **IP 共享确认**：用户确认手机 App 同 WiFi 交易
5. **重启频率确认**：当日重启 ≥5 次，启动链突发请求叠加

### 4.2 P0-2 单实例锁三次迭代诊断

#### v1（文件锁 + PID 存活检测）缺陷：
- `taskkill /F` 强杀不触发 `atexit` → 死锁文件永远残留
- 死 PID 在 14 秒重启窗口被 Windows 复用给其他进程 → `OpenProcess` 误判存活 → 错误拒绝
- 退出码 1 被 watchdog 当崩溃 → 无限重启循环

#### v2（Windows 命名互斥体）缺陷：
- `ctypes.windll.kernel32.GetLastError()` 直接调用**不可靠**——ctypes 内部操作覆盖线程 last error
- `CreateMutexW` 返回 64 位 HANDLE，默认 `restype=int` 截断
- 测试盲区：v2 测试只做同进程二次调用（恰好绕过 ctypes 可靠性问题），未做真实跨进程子进程测试

#### v3（修正版，当前版本）：
- `ctypes.WinDLL('kernel32', use_last_error=True)` + `ctypes.get_last_error()`（唯一可靠读取）
- 显式 `restype=wintypes.HANDLE` + `argtypes` 声明
- 新增跨进程子进程测试（`subprocess.Popen` 真实启动第二实例 → returncode 42）

---

## 五、修复动作

### P0-1：watchdog 进程树清理（watchdog.py）

**新增** `_kill_main_process_tree(pid)` 函数：
```python
def _kill_main_process_tree(pid):
    subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], ...)
    # /T = 进程树强杀（process.terminate 只杀根进程，不杀子孙）
```

**修改点**：
- KeyboardInterrupt 处理器：调用 `_kill_main_process_tree` 后再 `sys.exit(0)`
- 异常退出路径：同样调用
- 新增退出码 42 识别：bot_runner 单实例锁拒绝时返回 42，watchdog 识别后**自行停止**不进崩溃重启循环

### P0-2：bot_runner 单实例锁（bot_runner.py，v3 最终版）

```python
def acquire_instance_lock():
    global _mutex_handle
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = kernel32.CreateMutexW(None, True, "Global\\my_crypto_bot_single_instance")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            sys.exit(BOT_EXIT_INSTANCE_REFUSED)  # 退出码 42
        _mutex_handle = handle
```

**设计原则**：
- Windows 命名互斥体为权威判据（内核对象，进程死亡自动释放，免疫 PID 复用）
- 锁文件降级纯诊断（写 PID 供排查，**永不阻断**）
- 非Windows 退化用 PID 文件方案
- `main()` 入口第一行调用

### .gitignore 补充
- `trade_state.json.bak`
- `.bot_instance.lock`

### 测试覆盖
- `test_orphan_guard.py` 5 场景：①首次获取+诊断锁写入 ②同进程二次调用→SystemExit(42) ③锁文件死 PID 不影响判定 ④watchdog AST 含 /T 杀树+识别 42 ⑤**真实子进程**模拟第二实例→returncode 42

---

## 六、教训沉淀

### 6.1 运营教训
1. **收到熔断告警后必须等解除时间过后再重启**——封禁期内重启的 `fetch_time` 会触发 418 延长封禁
2. **手机 App 手工交易高峰用手机流量**——同 WiFi 共享 IP 配额
3. **PyCharm Ctrl+F5 不是安全重启方式**——只杀根进程不杀子进程（现在 P0-1 已修复，但理解原理有助于避免类似问题）

### 6.2 工程教训
1. **Windows ctypes last error 读取必须用 `use_last_error=True` + `ctypes.get_last_error()`**——直接调 `GetLastError()` 会被 ctypes 内部操作覆盖
2. **单实例/进程类防护必须用真实子进程测试**——同进程 mock 会绕过 ctypes/内核层问题（v2 测试盲区的根因）
3. **退出码必须语义化**——通用退出码 1 被 watchdog 当崩溃→无限循环；专用 42 后 watchdog 识别并自行停止
4. **进程树强杀用 `taskkill /F /T`**——`process.terminate()` 只杀根进程，Windows 下子进程变孤儿

### 6.3 待实施改进（P1 档，未实施）
1. **API 调用计数器**（最优先）——按端点计数 + 周期汇总，"没有测量就没有优化"
2. **持久化 ban-until 到文件**——启动前检查是否仍在封禁期，拒绝启动
3. `__init__` 冗余时间调用 3→1
4. 监控基准间隔 60→90s
5. fast_poll 3s×3→5s×2

---

## 七、当前状态

| 项目 | 状态 |
|------|------|
| P0-1 进程树清理 | ✅ 已实施（watchdog.py） |
| P0-2 单实例锁 v3 | ✅ 已实施（bot_runner.py） |
| test_orphan_guard.py | ✅ 5/5 全绿 |
| 全量回归 | ✅ 47 场景全绿（Phase A/B/C + P0） |
| 语法检查 | ✅ 三文件通过 |
| Git 提交 | ❌ 未提交（C1/C2/C3 + P0 + 4 测试文件均在工作区） |
| 用户重启运行 | ✅ 已正常重启运行 |
| P1 API 削减 | ❌ 待用户确认后实施 |

---

## 八、请 ChatGPT 审查要点

1. **P0-1 进程树清理方案是否完备**：KeyboardInterrupt + 异常退出两条路径是否都覆盖？是否有遗漏的退出路径？
2. **P0-2 v3 互斥体实现是否有残留风险**：`use_last_error=True` + `get_last_error()` 组合在所有 Windows 版本上是否可靠？互斥体名称 `Global\` 前缀是否合适？
3. **退出码 42 方案是否健壮**：watchdog 识别 42 后自行停止，如果用户忘记这一点会不会有新问题？
4. **"封禁期内重启延长封禁"问题的改进方案**：持久化 ban-until 到文件 + 启动前检查，这个方案是否值得优先实施？
5. **双实例并跑期间的风险评估**：约 12:15-15:27 期间可能有双实例并行（双倍 API + TG getUpdates 两实例抢占 + 同批次双监控），是否需要额外排查有无重复挂单或状态文件竞争写入？
6. **P1 API 削减包的优先级排序**：API 调用计数器是否应作为最优先项？还是有更紧急的项？

---

## 九、ChatGPT 审查结论（15:5x 回执，已闭环）

> 审查判定：**诊断链成立，P0 修复方向正确**；事故核心问题是"API 熔断保护的是运行中的请求，而不是整个进程生命周期"。

### 逐项裁决

| 项 | ChatGPT 裁决 | 落地动作 |
|----|-------------|----------|
| P0-1 进程树清理 | ✅ 正确应保留，但需补**真实行为测试**（非仅 AST 断言 `/T`） | 登记 P0 微调项 #1 |
| P0-2 Mutex v3 | ✅ 方向正确；`Global\` 前缀符合"机器级单实例"目标 | 登记 P0 微调项 #2（名称常量化 + 注释） |
| 退出码 42 | ✅ 合理，但应定义成**协议**而非 magic number（启动日志醒目提示） | 常量已定义 `BOT_EXIT_INSTANCE_REFUSED=42` ✅；登记 P0 微调项 #3（watchdog 侧醒目日志） |
| 双实例风险 | ⚠️ **必须事后审计**，不能凭"没出资金事故"翻篇 | 本地取证已做（trade_state.json 零写入无异常）；Binance 侧对账待用户 |
| ban-until 持久化 | 🔴 **升为 P0.5/P1-High，第一优先** | 登记 P1-API-01；设计要点已落档 |
| API 调用计数器 | 🟠 有价值但排在 ban-until 后 | 登记 P1-API-02 |
| C3/C4/C5/C6 | 继续；**C5 吸收双实例威胁模型** | C5 设计评审新增"双实例/重复执行"威胁 |
| 新工程原则 | **UNKNOWN ≠ EMPTY 适用于本地熔断状态**（重启后未知 ban ≠ 无 ban） | 已登记入宪法不变量 1 延伸 |

### 双实例事后审计（本地部分已完成）

- trade_state.json mtime=10:56 → 双实例窗口（12:15-15:27）**零状态写入**
- 唯一批次从未成交（last_filled_count=0，SL/TP 均 null）→ 无批次丢失/重复/回退/成交异常/SL ID 异常
- 推论：两实例均在"空监控"未成交批次，无 create_order 触发点 → **无重复下单的代码路径实际被执行**
- **待用户**：Binance Order History 12:15-15:27 窗口对账 + TG 日志复查 → 三方一致后正式关闭窗口

### 本次事故沉淀的工程原则（已入宪法）

> **UNKNOWN ≠ EMPTY 不只适用于交易所订单状态，也适用于本地 API 熔断状态：进程重启后，未知的 ban 状态不能被当成"没有封禁"。**

### API 保护体系四层架构（审查产出）

① 单实例生命周期 ✅ → ② 持久化熔断状态 ❌（P1-API-01）→ ③ _safe_api_call ✅ → ④ 请求计数/观测层 ⏳（P1-API-02）

---

## 十、双实例审计闭环（16:0x 最终结论）

> ChatGPT 裁决：**审计结论锁定，不回头扩展 Phase C 范围**；证据边界必须保持——本地取证不能证明交易所侧绝对无重复，以 Binance 订单历史为准。

### 三方对账最终状态

| 对账方 | 核查内容 | 结果 | 状态 |
|--------|----------|------|------|
| ① Binance 订单历史 | 2026-08-19 12:15–15:27，BTCUSDT 查异常重复订单/保护单 | **用户网页核实：无重复挂单** | ✅ |
| ② trade_state.json | 双实例窗口状态写入（批次丢失/重复/回退/SL ID 异常） | mtime=10:56 → **窗口内零写入**；唯一批次 last_filled_count=0、SL/TP 均 null | ✅ |
| ③ TG 日志 | 双实例窗口重复消息（挂单成功/SL 重挂/成交/结算） | **无独立落盘日志**（bot 输出仅到控制台，watchdog.log 窗口内零记录） | ⚠️ 无数据源 |

### 最终结论（证据边界内成立）

1. **交易所侧**：Binance 订单历史核对无重复挂单 → 双实例未产生重复订单（权威证据 ✅）
2. **本地状态侧**：窗口内零写入 + 批次从未成交 → 无状态竞争/批次异常（代码执行证据 ✅）
3. **TG 侧**：本地无落盘日志可查 → 该环无法本地取证；但因其不影响"无重复下单"的资金安全结论，窗口正式关闭
4. **严格表述**（采纳 ChatGPT 修正）："从本地状态与代码执行证据 + Binance 订单历史来看，**未发现重复下单**；窗口关闭，不扩展 Phase C 范围。"

### 对后续的影响

- **C5/SG4 设计评审仍必须吸收**"双实例/重复执行 + 重启后状态未知 + Create≠Success"威胁模型（不因本次未出事而放松）
- **ban-until 持久化**按 ChatGPT 定级为 Phase C 封板后第一批 P1（P1-API-01，P0.5/P1-High）
