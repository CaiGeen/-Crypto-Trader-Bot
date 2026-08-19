# Phase C 落地清单与下一步行动

> **生成时间**：2026-08-19 15:47 (GMT+8) / **v2 更新**：15:57（吸收 ChatGPT 审查结论）/ **v3 更新**：16:05（双实例审计闭环）
> **用途**：整理 Phase C 已完成/剩余项、418 事故教训、未提交代码清单，排列优先级化行动列表
> **关联文档**：418封禁事件总结_送审ChatGPT.md（事故详情 + 审计闭环）、安全不变量_系统宪法.md（8条不变量 + 2026-08-19 延伸）
> **v2 变更**：ban-until 持久化升为 P1-A（第一优先）；新增双实例事后审计；C5 增加双实例威胁模型；P0 微调四项
> **v3 变更**：双实例三方对账闭环（Binance ✅ 用户确认无重复挂单 + trade_state ✅ 零写入 + TG ⚠️ 无数据源）→ 窗口关闭，结论锁定，不扩展 Phase C 范围

---

## 一、总体进度总览

```
Phase A ✅ 已推送 GitHub（commit 896ed49）
Phase B ✅ 已本地提交（commit e5cf8a1，push 需用户手动）
Phase C 部分完成（C1/C2/C3 ✅，C4/C5/C6 待执行）
P0 修复 ✅ 已实施（未提交）
当前 Git：工作区有 4 文件修改 + 4 个未跟踪测试文件
```

### 完整修复链路

| Phase | 项 | 内容 | 状态 | 测试 |
|-------|-----|------|------|------|
| A | R11 | fetch_positions 失败返回 None 不误清批次（3处） | ✅ 推送 | 5 场景 |
| A | R13-B | 监控 save 保留 user_modified（2处） | ✅ 推送 | 含在上方 |
| A | R14 | TP None 时主循环补挂条件 | ✅ 推送 | 含在上方 |
| A | R3-v2 | 恢复语义：NO_WORK ≠ FAILURE | ✅ 推送 | 5 场景 |
| B | R1 | 熔断告警状态机（generation 防跨周期错配） | ✅ 本地提交 | 4 场景 |
| B | SG1 | READY 门控（Fail-Closed，唯一置位=recover True） | ✅ 本地提交 | 7 场景 |
| B | SG2 | 加仓前风险闸门（delta≠0 拒绝 + SL∈open_orders） | ✅ 本地提交 | 11 场景 |
| B | TG降级 | BadRequest 纯文本重发（防 Fail-Silent） | ✅ 本地提交 | 4 场景 |
| C | C1/R6 | 裸调用收编（3处：2处收编 + 1处只套信号量） | ✅ 未提交 | 3 场景 |
| C | C2/R10 | 限价监控异常退出告警（critical TG，不写 monitor_error） | ✅ 未提交 | 4 场景 |
| C | C3/R12 | 状态备份（_persist_states chokepoint：备份→tempfile→os.replace） | ✅ 未提交 | 5 场景 |
| P0 | 进程树清理 | watchdog taskkill /F /T + 退出码42识别 | ✅ 未提交 | 含在下方 |
| P0 | 单实例锁 v3 | Windows 命名互斥体（use_last_error + HANDLE 类型） | ✅ 未提交 | 5 场景 |

**测试总计**：9 个测试文件，47 场景全绿

---

## 二、Phase C 剩余项

### C4 / SG3-P1：SL/TP 有效性验证（零 API 成本）

**现状**：监控循环 SL 检查仍是纯存在性判定（`sl_id ∈ open_orders`），无方向/reduceOnly/数量校验。

**目标**：利用监控已有的 `open_orders_map`（每周期开头 fetch），对每批次的 SL/TP 做有效性校验：
- 方向是否正确（SL 应为反方向 reduceOnly）
- reduceOnly 标记是否存在
- 数量是否与持仓匹配

**特点**：零额外 API 成本（复用已有数据），可参考 SG2 的 `_check_sl_coverage` helper 结构。

**风险等级**：中（不改变交易行为，只增加检测和告警）

### C5 / SG4 + SG4-B：Create→Verify→Commit 幂等性

**现状**：全文件 **14 处** `create_order['id']` 直接提交，无一验证。

**关键前置**：设计阶段必须先做 14 处调用点分级评估：

| 分级 | 路径 | 特征 | 处理策略 |
|------|------|------|----------|
| 关键 | SL/TP 保护单 | 失败=裸仓风险 | 必须 Create→Verify→Commit |
| 次关键 | 入场单 | 失败=少一层仓位 | 需 Verify，但重试风险可控 |
| 天然幂等 | 平仓单（reduceOnly） | 重复提交不增仓 | 可降级，不强制 Verify |

**最大缺口**：首成交路径 `_place_prepared_orders_immediately`（纯状态判定，无 open_orders 交叉验证）

**⚠️ ChatGPT 审查新增威胁模型**：本次 418 事故证明"单实例 ≠ 绝对不会出现双执行者"。即使未来程序崩溃重启、网络超时、双执行者短暂出现，只要写操作前后有交易所事实验证（Create→Verify→Commit），系统自愈能力更强。因此 C5 设计评审**必须把"双实例/重复执行"作为威胁模型之一**。

**风险等级**：高（改变交易行为，需分级+逐路径设计+单独测试）

**原则**：不为凑完整闭环把天然幂等路径也重构（违反最小改动纪律）

### C6 / SG9：审计日志

**现状**：无 `trade_events.json`，所有事件只写 trade_state.json（状态快照，非事件流水）。

**目标**：建立不可变事件日志，记录每次挂单/撤单/成交/告警的完整上下文。

**风险等级**：低（纯增量，不改现有逻辑）

---

## 三、418 事故教训与衍生改进

### 3.1 已修复（P0）

| 修复项 | 文件 | 内容 | 测试 |
|--------|------|------|------|
| 进程树清理 | watchdog.py | `_kill_main_process_tree` + KeyboardInterrupt/异常退出调用 + 退出码42识别 | test_orphan_guard.py |
| 单实例锁 v3 | bot_runner.py | Windows 命名互斥体 + `use_last_error=True` + `ctypes.get_last_error()` + 显式 HANDLE | test_orphan_guard.py |

### 3.2 衍生改进（P1，ChatGPT 审查后重排）

**ChatGPT 审查定级（2026-08-19 15:5x）**：

| 改进项 | 原定级 | ChatGPT 定级 | 理由 |
|--------|--------|--------------|------|
| **ban-until 持久化** | P1 高 | 🔴 **P1-A（第一优先，P0.5/P1-High）** | "已经知道自己被封了，重启却再次主动打 API"——12:03 事故的直接教训 |
| **API 调用计数器** | P1 最优先 | 🟠 **P1-B（第二优先）** | 很有价值，但没有测量对象之前，应先把"已知封禁不重启"做掉 |
| 启动 API 削减（__init__ 3→1 / 60→90s / fast_poll） | P1 中 | 🟡 **P1-C（第三优先）** | 降低正常负载，但**没有计数器之前不建议凭感觉继续砍** |

### 3.3 API 保护体系四层架构（ChatGPT 审查产出）

```
             Binance API
                  ↑
        ┌─────────┴─────────┐
        │ ④ 请求计数/观测层 │   P1-B（待建）
        └─────────┬─────────┘
                  ↑
        ┌─────────┴─────────┐
        │ ③ _safe_api_call │   ✅ 已有（retry/semaphore/cooldown/418）
        └─────────┬─────────┘
                  ↑
        ┌─────────┴─────────┐
        │ ② 持久化熔断状态  │   ❌ 缺口（P1-A：ban_until 落盘）
        └─────────┬─────────┘
                  ↑
        ┌─────────┴─────────┐
        │ ① 单实例生命周期  │   ✅ 已有（watchdog 进程树 + mutex v3）
        └───────────────────┘
```

**当前状态**：① ✅ ③ ✅ ｜ ② ❌（真实 P1 缺口） ④ ⏳

### 3.4 ban-until 持久化设计要点（P1-A）

ChatGPT 明确的设计边界：

- **持久化内容**：极小 JSON 状态（不止存 ban_until，保留来源与更新时间）
  ```json
  {
      "api_ban_until": 1787127291,
      "source": "binance_418",
      "updated_at": 1787126000
  }
  ```
- **启动逻辑**：读取 ban_until → `now < ban_until` → **拒绝启动并告知准确解除时间**；watchdog 不进入无限重启
- **不建议"自动等到解禁后启动"**：会让 watchdog 长时间占着进程生命周期。明确拒绝 + 告知时间 + 用户手动启动最符合"安全优先、不为便利放宽"纪律
- **落点**：写入时机=`_alert_cooldown_start`（收到 418 时）;读取时机=bot_runner 启动链首个 API 调用前
- **新原则**：UNKNOWN ≠ EMPTY 延伸——"进程重启后，未知的 ban 状态不能被当成没有封禁"（已登记入宪法不变量 1）

### 3.5 双实例事后审计（12:15-15:27，ChatGPT ⚠️ 要求必须查）

> **背景**：双实例并跑期间存在状态文件互相覆盖（`_state_lock` 是 `threading.Lock()` 进程内锁，防不了跨进程）、同批次重复监控、重复下单三类风险。不能仅凭"最终没出资金事故"结束。

**已完成的本地取证（15:55，读 trade_state.json）**：
- 状态文件 mtime=10:56 → **双实例窗口内零状态写入**（唯一批次从未成交，无 SL/TP/成交事件需要落盘）
- 批次 `batch_20260819_081653_0cd379`：`last_filled_count=0`、`current_sl_id=null`、`tp_order_id=null`、`filled_details` 全 0 → **无批次丢失/重复/回退/成交异常/SL ID 异常**
- 推论：该批次无成交 → 两实例都在"空监控"（fetch_open_orders + fetch_positions），**无 create_order 触发点 → 无重复下单的代码路径实际被执行**
- **注意**：这个结论只对"当前这个未成交批次"成立，不能外推——未来若有活跃成交批次遇双实例，C5 的 Create→Verify 是唯一防线

**三方对账最终状态（16:05 闭环）**：
- **A. Binance Order History**：✅ **用户已确认 12:15-15:27 BTCUSDT 无重复挂单**（权威证据，窗口正式关闭）
- **B. trade_state.json**：✅ 已查（零写入，无异常，见上）
- **C. TG 日志**：⚠️ **无独立落盘日志**（bot 输出仅到控制台；watchdog.log 该窗口内零记录）→ 该环本地无数据源，但不影响"无重复下单"结论
- **最终结论**（采纳 ChatGPT 修正表述）：从本地状态与代码执行证据 + Binance 订单历史来看，**未发现重复下单**。审计结论锁定，不回头扩展 Phase C 范围。
- **不放松项**：C5/SG4 设计评审仍必须吸收"双实例/重复执行 + 重启后状态未知 + Create≠Success"威胁模型；ban-until 持久化维持 P0.5/P1-High 定级，Phase C 封板后第一批执行

### 3.6 P0 微调四项（ChatGPT 审查附加，小改动）

| # | 项 | 内容 | 理由 |
|---|-----|------|------|
| 1 | watchdog 真实行为测试 | 启动 watchdog → 启动 bot_runner 子进程 → 触发停止 → 断言 bot_runner 真死（真实 subprocess，非仅 AST 断言） | 进程树语义只有真实行为能证明，AST 只能证明"写了代码" |
| 2 | mutex 名称常量化 | `Global\my_crypto_bot_single_instance` 提为常量 + 注释"机器级单实例锁，非 session 级" | 防维护者误删 Global 前缀 |
| 3 | 退出码 42 协议化 | 已定义 `BOT_EXIT_INSTANCE_REFUSED=42` 常量 ✅；补 watchdog 侧醒目日志"🚫 Bot 启动拒绝：检测到已有实例，watchdog 停止不重启" | 用户不记得 42 语义时不会误以为崩溃 |
| 4 | 拒绝日志醒目化 | bot_runner 已打印"检测到另一个 Bot 实例正在运行" ✅ 基本达标 | 保持一致 |

**测试方法论沉淀（v2 补充）**：
- **单实例/进程类防护必须用真实子进程测试**（`subprocess.Popen`），同进程 mock 会绕过 ctypes/内核层问题
- **Windows ctypes last error 读取必须用 `use_last_error=True` + `ctypes.get_last_error()`**，不可直接调 `GetLastError()`
- **退出码必须语义化**，通用退出码会被 watchdog 误判为崩溃

---

## 四、未提交代码清单

```
Git 工作区状态：
  Modified:  .gitignore          （补 trade_state.json.bak + .bot_instance.lock）
  Modified:  bot_runner.py       （C1/R6 收编 + P0-2 单实例锁 v3）
  Modified:  trader_260725.py    （C1/R6 -1021分支 + C2/R10 告警 + C3/R12 备份）
  Modified:  watchdog.py         （P0-1 进程树清理 + 退出码42识别）
  Untracked: test_orphan_guard.py     （P0 测试，5场景）
  Untracked: test_r6_bare_calls.py    （C1 测试，3场景）
  Untracked: test_r10_limit_close_alert.py （C2 测试，4场景）
  Untracked: test_r12_state_backup.py （C3 测试，5场景）
```

**建议**：尽快 `git commit` 建立回滚锚点，再 `git push origin main` 同步远端。

**commit message 建议**：
```
feat: Phase C (C1-C3) + P0 孤儿进程防护

C1/R6: 裸调用收编（bot_runner 2处 + trader 1处只套信号量）
C2/R10: 限价监控异常退出 critical TG 告警（不写 monitor_error 防误清）
C3/R12: _persist_states chokepoint（备份→tempfile→os.replace）
P0-1: watchdog 进程树清理（taskkill /F /T + 退出码42识别）
P0-2: bot_runner 单实例锁 v3（Windows 命名互斥体 use_last_error）
测试: 4 文件 17 场景 + 全量回归 47 场景全绿
```

---

## 五、下一步行动清单（ChatGPT 审查后 v2，按优先级排列）

> **ChatGPT 明确建议**：不暂停 Phase C 去处理 P1。"发现新问题 ≠ 立刻改，先登记、定级、单独设计。" 执行顺序 = 提交锚点 → 双实例审计 → **Phase C 继续（C4→C5→C6）** → Phase C 后第一批 P1（ban-until → 计数器 → 削减）。

### 🔴 P0-紧急（立即执行）

| 序号 | 行动 | 负责方 | 预估时间 |
|------|------|--------|----------|
| 1 | **提交 Phase C + P0 代码到 GitHub**（建回滚锚点） | 用户操作 | 5分钟 |
| 2 | **push Phase B 到 GitHub**（commit e5cf8a1 仍未推送） | 用户操作 | 1分钟 |

### 🟠 P0.5-已完成（双实例风险窗口正式关闭 ✅ 16:05）

| 序号 | 行动 | 结果 |
|------|------|------|
| 3 | **双实例事后审计三方对账**：① Binance Order History（12:15-15:27 BTCUSDT）✅ 用户确认无重复挂单 ② trade_state.json ✅ 零写入无异常 ③ TG 日志 ⚠️ 无落盘数据源（不影响结论） | **窗口关闭，结论锁定，不扩展 Phase C 范围**（详见 418 总结文档第十章） |

### 🟡 P1-Phase C 继续（ChatGPT 审定：保持 C4→C5→C6 连续）

| 序号 | 行动 | 前置条件 | 预估工作量 |
|------|------|----------|------------|
| 4 | **C4/SG3-P1** SL/TP 有效性验证（方向/reduceOnly/数量，零 API 成本） | Phase C 锚点已建 | ~40行 + 测试 |
| 5 | **C5/SG4+SG4-B** Create→Verify→Commit（14 处分级 + **双实例威胁模型**） | C4 完成 + 14 处调用点分级评估 | ~80行 + 测试 |
| 6 | **C6/SG9** 审计日志（trade_events.json） | C5 完成 | ~60行 |

### 🟢 P2-Phase C 完成后第一批 P1（ChatGPT 审定顺序）

| 序号 | 行动 | 前置条件 | 预估工作量 |
|------|------|----------|------------|
| 7 | **P1-API-01：ban-until 持久化**（🔴 定级 P0.5/P1-High，启动前检查封禁期拒绝启动） | Phase C 全部完成 | ~20行 + 测试 |
| 8 | **P1-API-02：API 调用计数器**（按端点计数 + 周期汇总 TG 报告） | P1-API-01 完成 | ~30行 + 测试 |
| 9 | **P1-API-03：启动 API 削减**（__init__ 3→1 / 60→90s / fast_poll 3s×3→5s×2） | 计数器有数据支撑后 | ~8行 |
| 10 | **P0 微调四项**（watchdog 真实行为测试 / mutex 常量 / 42 协议化日志 / 拒绝日志） | 可随时插队 | ~30行 + 测试 |

### ⚪ P3-低优先（稳定后执行）

| 序号 | 行动 | 前置条件 | 预估工作量 |
|------|------|----------|------------|
| 11 | **D-004** 手工仓 SL 覆盖识别（方案 A 张力缓解） | 单独评审 | ~30行 + 设计 |
| 12 | **D-001** KAMA 跟踪止盈（影子模式→灰度） | 审计全部稳定 | ~200行 |

### 📋 持续运营提醒

- 收到 R1 熔断告警后**必须等解除时间过后再重启**
- 手机 App 手工交易高峰**尽量用手机流量**（避免同 WiFi 共享 IP 配额）
- 重启时观察日志：应见"✅ [状态恢复] 恢复流程完成" + "系统 READY"（SG1）
- 如果误启动双实例，watchdog 日志应显示"单实例锁拒绝"并自动停止

---

## 六、测试资产清单

| 文件 | 场景数 | 覆盖范围 | 位置 |
|------|--------|----------|------|
| test_recover_semantics.py | 5 | R3-v2 恢复语义 | 项目根目录 |
| test_cooldown_alert.py | 4 | R1 熔断竞态 | 项目根目录 |
| test_sg1_ready_gate.py | 7 | SG1 READY 门控 | 项目根目录 |
| test_sg2_risk_gate.py | 11 | SG2 加仓风险闸门 | 项目根目录 |
| test_tg_fallback.py | 4 | TG Markdown 降级 | 项目根目录 |
| test_r6_bare_calls.py | 3 | C1/R6 裸调用（AST 结构级） | 项目根目录 |
| test_r10_limit_close_alert.py | 4 | C2/R10 限价监控告警 | 项目根目录 |
| test_r12_state_backup.py | 5 | C3/R12 状态备份（真实文件操作） | 项目根目录 |
| test_orphan_guard.py | 5 | P0 孤儿防护（含跨进程子进程） | 项目根目录 |
| **合计** | **47** | | |

---

## 七、安全不变量验证状态

| # | 不变量 | Phase A/B/C + P0 后状态 |
|---|--------|------------------------|
| ① | UNKNOWN ≠ EMPTY | ✅ R11 修复（None 三态逻辑） |
| ② | 无有效 SL 禁加仓 | ✅ SG2 修复（delta≠0 + SL∈open_orders） |
| ③ | 恢复不扩大风险 | ✅ SG1 修复（READY Fail-Closed） |
| ④ | 用户修改不覆盖 | ✅ R13-B 修复（user_modified 保留） |
| ⑤ | 交易所优先 | ✅ 架构固有（每周期 fetch） |
| ⑥ | Create ≠ Success | ⏳ C5/SG4 待实施（14处 create_order 未验证） |
| ⑦ | 未 READY 不产新风险 | ✅ SG1 修复（execute_signal 入口门控） |
| ⑧ | Fail-Closed not Fail-Silent | ✅ R1 告警 + R10 告警 + TG 降级 + R3-v2 语义 |

**唯一未闭环**：不变量⑥（Create ≠ Success）→ C5/SG4 是最后一块拼图。
