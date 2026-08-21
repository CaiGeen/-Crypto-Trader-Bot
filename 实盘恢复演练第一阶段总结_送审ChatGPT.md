# 实盘恢复演练第一阶段总结（送审 ChatGPT）

日期：2026-08-21 ｜ 版本：v1.0 ｜ 送审目的：演练过程 + 遇到的问题 + 应对修复方式交叉审查
依据：`实盘恢复演练执行清单.md` v1.1（Step 2→3→4→5 路径，用户已确认 Step3 → Step5 小仓 → Step4 合并）

> **文档声明**：本文所有代码行号均于 2026-08-21 17:00 从当前工作树实测（Grep/Read），非外部 AI 转述。
> 请审查时以 `trader_260725.py`（HEAD=47f8228）实际源码为准，若发现行号不符请指出以便同步。

---

## 一、演练总览与当前状态

| 步骤 | 内容 | 状态 | 结果/事故 |
|---|---|---|---|
| Step 1 | 清事故遗留仓位（对账） | ✅ 2026-08-20 22:37 | reconcile 通过，0仓0单 |
| Step 2 | 启动恢复测试（互斥体/READY/零告警） | 🔁 反复执行 | **D-004 事故**（8-20 23:28）→ Watchdog 安全补丁 v1（R1/R2/R3）→ 8-21 复测期间触发**事件3** → 四件套修复 |
| Step 3 | 错误 TP 演练（R1 阻断） | ✅ 2026-08-21 15:5x | R1 阻断**符合预期**（零挂单），但意外暴露**事件4：TP 补挂死锁** → F1/F2/F3 修复 |
| Step 4 | 改价自愈演练（R2//tp） | ⬜ 待执行 | 计划与 Step 5 合并（趁小仓在位） |
| Step 5 | 小仓灰度（0.001 BTC 单层） | ⬜ 待执行 | 下一项 |
| Step 4.5 | R3 熔断专项 | ✅ 离线已覆盖 | test_tp_validation.py T1 6 场景，实盘不注入 |

**演练额外收获**：演练不是"4 步走完"，而是变成了**实战化故障注入**——每走一步都暴露一个此前测试未覆盖的真实缺陷（D-004 守护进程风暴 → 事件3 通知风暴/层叠重复 → 事件4 TP 死锁）。每起事故均按清单总原则处理：**停止 → 保留现场 → 分析 → 送审确认 → 修复 → 专项测试 + 全量回归 → 提交 → 重启验证**，全程无热修。

---

## 二、问题清单与应对修复汇总（按时间序）

| # | 时间 | 事故/问题 | 根因（已源码实证） | 修复（已提交） | 验证 |
|---|---|---|---|---|---|
| A | 8-20 23:28 | **D-004 Watchdog 重复启动风暴** | 互斥体拒绝路径 GBK 编码崩溃 → 退出码 42 变 1 → watchdog 无限重启 → crash_alert 通知风暴 | Watchdog 安全补丁 v1：R1 入口编码保护 / R2 启动熔断（60s×5）/ R3 crash_alert 同因去重（10min） | test_watchdog_guard.py；Step 2 复测 |
| B | 8-21 08:0x | **事件3：补挂 SL/TP 层叠重复**（batch_080043，L0/L1 两层成交） | ① verify 时机：create 后立即 fetch，Binance 条件单可见性延迟 → OrderNotFound → NOT_CONFIRMED（4/4 单全命中）② 运行期无重查：自愈只在启动调用 → NOT_CONFIRMED 永久卡死 ③ 撤销链断裂：current_sl_id 恒 null → 旧层单永不撤销 → 层叠重复（1.247 > 持仓 0.817） | 四件套 R-A/B/C/D（2a36522）：R-A verify OrderNotFound 2s×3 短窗口重试 / R-B 主循环 30s 运行期周期自愈 + 连续 10 轮 critical 升级 / R-C 滚动撤销链（新单确认后撤旧层）/ R-D pending 按 registry 实况清理 | 专项 20 场景 + 全量回归 22 套件 |
| C | 8-21 09:2x | **事件3 续：MISMATCH 误判风暴**（6 TG + 4 邮件）+ reconcile 误报 | ① `_order_matches_intent` 用 ccxt 归一化 type（'market'）比对 intent（'STOP_MARKET'）→ 有效单恒不匹配 ② ccxt fetch 已撤销单返回 status='canceled' 对象不抛异常 → 当"订单存在" ③ reconcile symbol 归一化不一致（'BTCUSDT' vs 'BTC/USDT:USDT'） | F1/F2/F3/F4b（73fdc60）+ F3b（ebbdc54）：F1 intent 匹配重写（info.type 优先 / reduceOnly `_as_bool`）F2 订单生命周期分层（status∉{new,open,active}→ABSENT）F3/F3b reconcile symbol 归一化 F4b 启动前历史条目告警降级 | 专项 34/34 + 全量回归 22 套件；重跑对账通过（持仓 1.191 vs 本地 1.19 吻合） |
| D | 8-21 11:2x | **保本按钮确认路由失效**（be_confirm_ 报"未找到活跃批次"） | bot_runner if/elif 链：`be_` 分支 naive replace 吞掉 `be_confirm_`/`be_cancel_` → batch_id 变 'confirm_batch_...'；对应 elif 是死代码 | 1 行修复（e9a32c1）：`startswith("be_") and not startswith(("be_confirm_","be_cancel_"))` | 全量 13 种按钮路由核对 |
| E | 8-21 15:5x | **事件4：首层成交后 TP 补挂死锁**（batch_155732，0.003 BTC 演练仓） | 见 §三 详细根因链 | **F1/F2/F3（47f8228）**，见 §四 | 专项 22/22 + 全量回归 23/23 |

---

## 三、事件4 详细记录（本次演练最重要的发现）

### 3.1 现象

演练仓（batch_20260821_155732_2fbcfe）首层成交后：
- SL 自动挂出 ✅（current_sl_id=3000002149580888）
- **TP 未挂出**：registry 显示 TP CONFIRMED（order_id=3000002149580750）但物理上已 canceled；批次级 `tp_order_id=null`
- R14 每轮补挂 → 闸门仲裁拦截（3 次后静默）→ **死锁循环**（registry CONFIRMED 永不终结）

### 3.2 根因链（代码 + 状态双证）

1. 首层成交同 cycle：prefill（`_place_prepared_orders_immediately`）先挂 TP/SL 并 Commit → registry TP CONFIRMED（updated_at=1787299134.574 = 15:58:54.574）
2. 随后状态重载使局部 `tp_order_id` 获知刚提交的 ID
3. `need_update_tp=(batch_filled_count>last_filled_count)=(1>0)=True` → TP 更新段**先撤销刚挂出的 TP（程序自杀）** → `_assert_create_allowed` 检查但**未传 replace_order_id**（registry 仍 CONFIRMED）→ 拦截 → `tp_order_id=None` 落盘
4. R14 每轮补挂 → 闸门永久拦截 → 死锁（registry CONFIRMED 永不终结；R-B 自愈只重查 PENDING_VERIFY/NOT_CONFIRMED，**CONFIRMED 条目永不重查**）

**SL 存活纯靠运气**：prefill SL verify 瞬断 → PENDING_VERIFY（未 Commit）→ 风险段 SL 分支闸门拦截但**未撤销物理单** → R-B 15:59:00.099 收编为 CONFIRMED。SL 风险段存在**同款"先撤后建无 replace"缺陷**，本例被时序运气掩盖。

### 3.3 三重佐证

① registry 时间戳 TP 15:58:54.574 < SL 15:59:00.099（prefill SL 段先于 TP 段，SL CONFIRMED 反而在后 = R-B 收编而非 prefill 直挂）② watchdog.log 11:33 后无重启（同进程内事件）③ 16:08 .bak 与 16:10 当前 trade_state.json 无 diff（死锁纯内存轮询）

### 3.4 演练合规性说明

- 演练按清单 §四执行：TG 发送方向性错误 TP 信号 → **R1 阻断符合预期**（`❌ 止盈价方向错误！` + critical 恰 1 条 + 交易所零痕迹 + 无批次状态）
- 事件4 是**后续真实灰度开仓**（Step 5 预演，0.003 BTC 单层）时暴露——不是 R1 演练本身失败，而是演练流程成功触达了真实运行路径

---

## 四、事件4 修复方案 F1/F2/F3（47f8228，+296 行）

### 4.1 F1：风险维护段适配仲裁换挂语义（trader_260725.py L4743-4794 区域）

- SL/TP 更新段**撤旧前先过闸门**：`_assert_create_allowed(desc='替换止损/止盈单', replace_order_id=old_id)`（换挂语义：CONFIRMED + replace_order_id 匹配 → 允许，确认的旧单将被撤销替换，无双单）
- **撤销确认 / Unknown order（-2011）** → `_update_registry(state='ABSENT', terminated_reason='canceled_by_update_replace' / 'order_not_found_on_replace')`
- **网络异常 fail-closed**：不清 id、不创建、保留下轮（未知 ≠ 不存在）
- **TP 段 `tp_skip_create` 标志**：替换被闸门拒绝/网络异常 → True → 主闸门走 `'F1_replace_blocked_skip_create'` 分支 → **保留 tp_order_id 落盘**（防 R14 再触发）；其余拦截分支仍清空
- **SL 段拒绝替换保留 old_sl_id** → 创建分支自然跳过（防双单，天然等价于 tp_skip_create）

### 4.2 F2：监控循环 terminal 检测同步终结（L4069 SL / L4202 TP）

- SL/TP 检测分支遇到 `canceled/expired` → **按 order_id 精确遍历 registry 定位 identity**（找不到回退最新层 identity）→ `state='ABSENT', terminated_reason=f'terminal_status_{status}'`
- 目的：CONFIRMED 条目物理终结时同步 registry，否则永不终结 = 死锁根因
- 设计选择：order_id 精确遍历替代"层号回退"，防 layer 漂移（多批次同层共享 order 空间）

### 4.3 F3：R14 补挂前 registry 实况裁决（新 helper `_adjudicate_recreate_before_repair`，L2838 定义；接入 L4013 SL 缺失兜底 / L4254 TP R14）

裁决矩阵（返回 `(verdict, order_id)`）：

| 实况 | 裁决 | 动作 |
|---|---|---|
| 无条目 / ABSENT / FAILED | **allow** | 放行补挂 |
| 在场 + intent 匹配 | **adopt** | 先升 CONFIRMED 再收养（补 Commit），防双挂 |
| 在场 + intent 不匹配 | **mismatch** | critical 告警，禁自动处理 |
| 网络异常 / 结果未知 | **hold** | 保守保留下轮 |
| OrderNotFound | allow | 先写 ABSENT（f3_adjudicate_order_not_found）再放行 |
| terminal status | allow | 先写 ABSENT（f3_adjudicate_status_<s>）再放行 |
| PENDING_CREATE | hold | 意图已落盘，create 可能已发出 → 防双单 |

### 4.4 配套

- `_update_registry` 扩展 `terminated_reason=None` 关键字参数落盘（与既有 R-B/R-C 手动赋值风格对齐）
- 专项测试 `test_tp_deadlock_fix.py` **22/22**（F3 裁决矩阵 12 + F1 替换语义 3 + terminated_reason 落盘 1 + 源码断言 6）
- 锚点漂移同步：sg4（11 处）/ b2_create_gate（6 处）/ b2_close_gap（5 处）重新 Grep 实测
- **全量回归 23/23 全绿**

---

## 五、验证与闭环证据

| 项 | 结果 |
|---|---|
| test_tp_deadlock_fix.py（F1/F2/F3 专项） | 22/22 ✅ |
| 全量回归（23 套件，排除 orphan_guard 后） | 23/23 ALL GREEN ✅ |
| test_orphan_guard.py（bot 停机窗口补跑，2026-08-21 17:00） | **5/5 全绿** ✅ |
| 备份 | backups/20260821_tp_deadlock_fix_before\|after/ |
| 提交 | 47f8228（5 文件 +581/-47），工作树净（未追踪：文档 + backups/） |
| 死锁批次愈合路径 | 重启后 F3 自动裁决：TP 单物理已撤 → fetch terminal → ABSENT → 放行补挂；或在场 → adopt 收养防双挂 |

---

## 六、当前待办

1. **重启 watchdog 加载 47f8228**（当前运行实例仍为 10:01 启动的旧代码）——重启后先观察事件3/事件4 修复生效（无 MISMATCH 风暴、死锁批次被 F3 自动愈合、启动无通知风暴 F4b）
2. **Step 5 小仓灰度** 0.001 BTC 单层（TP +0.8% / SL -0.5%，清单 §七）
3. **Step 4 改价自愈** 趁小仓在位合并执行（R2 拦截 → /tp 修复 → 行为闭环，清单 §五）
4. 收尾平仓小仓，重跑 reconcile 确认 0 仓 0 单
5. 全部完成后整理**复用资产**（本阶段经验 → 演练复盘模板/故障注入验收清单升级）

---

## 七、请 ChatGPT 审查的重点问题

1. **F1/F2/F3 死锁闭环是否完备**：还有没有其他路径能让 registry 条目"CONFIRMED 但物理已终结"且永不终结？（重点：预生成段 `_place_prepared_orders_immediately`、新层成交重建段、部分减仓段是否存在同款"先撤后建"未适配 replace_order_id？）
2. **`tp_skip_create` 标志语义**：替换被阻断时保留 tp_order_id 落盘 → 下轮 `tp_order_id` 非空 → TP 更新段是否仍会走"撤销→重建"？（若 intent 参数已变化，是否应仍尝试替换？现有判定是否足够？）
3. **F3 adopt 先升 CONFIRMED 再收养**：是否存在"收养了别人的单"风险窗口？（order_id 精确匹配 + intent 匹配是否足够强？）
4. **F2 order_id 精确遍历回退逻辑**：回退"最新层 identity"在多层同 role 场景是否可能标错条目？（ABSENT 的层本就不应再被撤销，标错的影响面？）
5. **演练遗留问题**（清单 §十，未实施）：
   - `update_take_profit` 成功路径未调用 `_clear_tp_param_invalid` → 标记长期留存（无害，建议对称补清除）
   - R1 阻断 critical 直发无去重（重复错误信号产生等量 TG，属预期边界，可选优化）
6. **事故模式归纳**：D-004/事件3/事件4 是否指向同一类设计缺口（"程序先动物理单、再问仲裁"的顺序反置）？是否值得升格为宪法级不变量（**任何撤单/换挂前必须先过仲裁并携带 replace_order_id**）？

---

## 附：本文引用的当前源码锚点（2026-08-21 17:00 实测）

- `_adjudicate_recreate_before_repair`：定义 L2838 ｜ 接入 L4013（SL 缺失兜底）/ L4254（TP R14）
- F2 terminal 检测：SL L4069 / TP L4202（`terminated_reason=f'terminal_status_...'`）
- F1 换挂闸门 + tp_skip_create：L4743-4794（含 `'F1_replace_blocked_skip_create'` 分支 L4789）
- `_update_registry`：L2574 起（`terminated_reason` 参数）
- 互斥体（bot_runner）：L158-177（命名互斥体权威判据，退出码 42）
- R14 补挂：TP 缺失兜底（原 L4127 区域，F3 接入后漂移至 L4254 附近）
