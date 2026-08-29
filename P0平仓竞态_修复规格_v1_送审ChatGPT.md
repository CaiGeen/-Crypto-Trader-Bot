# P0 平仓竞态修复规格 v1（送审 ChatGPT 三轮裁定）

> 依据：ChatGPT 二轮 P0 裁定（2026-08-28）第十八节 12 问 + RED 基线证据
> （test_close_race_replay.py，9 RED-CONFIRMED，close_race_replay_RED_20260828.log）。
> 本文档**只回答问题、给落点方案，不改任何生产代码**。全部行号经本轮 Grep/Read 在
> HEAD=c147543 工作树重新实证（零改动）。

---

## 一、12 问逐项回答（源码实证）

### Q1：CLOSE_REQUESTED 的唯一进入点有哪些？

`pending_close=True` 写点全量共 3 个（Grep 全文无遗漏），全部由 bot_runner
asyncio executor 线程同步调用进入：

| # | 入口 | 置位点 | 触发来源（bot_runner） |
|---|---|---|---|
| 1 | `cancel_open_orders` | L5981 | 撤单按钮 L1056 / L1538 |
| 2 | `close_position_market` | L6034-6036 | 市价平仓按钮 L1467-1472 |
| 3 | `close_position_limit` | L6225-6227 | 最优价挂单按钮 L1478-1484、自定义价格 L1818 |

**合法回退点（CLOSE_REQUESTED → ACTIVE）2 个**：
- `close_position_market` 平仓失败回滚 L6151-6154（清 is_programmatic_cancel/pending_close）
- `close_position_limit` 挂限价单失败回滚 L6351-6354

结论：进入点收敛、可枚举，具备在 3 个入口统一升级为显式状态字段的条件。

### Q2：哪些函数可以创建 TP（实际发 create_order）？

| # | 函数 | create 位置 | 场景 |
|---|---|---|---|
| 1 | `execute_signal` | L2707 | 开仓首挂 TP（batch 刚创建） |
| 2 | 监控循环 `_start_monitoring` | L4241-4270 | 逐层成交后首次挂 TP |
| 3 | 监控循环 TP 维护段 | L5125-5236 | **R14 补挂 / replace / 滚动（本次事故路径）** |
| 4 | `_place_prepared_orders_immediately` | L5771-5863 | 成交后立即挂预生成 TP |
| 5 | `update_batch_tp` | L1609-1653 | 用户 /tp 改价换挂 |

排除项（已核实非 create）：`_rebuild_entry_orders_from_registry` L3673-3678 只写
`prepared_tp_params` 参数骨架落盘，实际发单由 #2/#4 消费。

### Q3：哪些函数可以创建 SL？

| # | 函数 | create 位置 | 场景 |
|---|---|---|---|
| 1 | `execute_signal` | L2646 / L2674 | 开仓 SL |
| 2 | 监控循环 | L4190-4196 | 逐层成交后首次挂 SL |
| 3 | 监控循环 SL 维护 | L4881-4887 / L5038-5044 | replace / 滚动更新 |
| 4 | `_place_prepared_orders_immediately` | L5660-5666 | 成交后立即挂 SL |
| 5 | `update_batch_sl` | L1804-1810 | 用户 /sl 改价换挂 |
| 6 | `_update_sl_no_validation` | L2016-2024 | 保本 / 内部滚动（set_breakeven_sl 消费） |

### Q4：哪些函数可以修改 TP？

- `update_batch_tp`（L1552，/tp）：撤旧挂新 + registry 换挂语义（B2-8）
- 监控循环 TP 维护段（L5117-5248）：replace 闸门 → PENDING_CREATE → create → verify → Commit
- （滚动 TP = D-001 KAMA，阶段2 未实现，当前无此代码路径）

### Q5：哪些函数可以修改 SL？

- `update_batch_sl`（L1713，/sl）
- `set_breakeven_sl`（L1847，保本按钮）→ 内部走 `_update_sl_no_validation`
- `_update_sl_no_validation`（L1957）
- 监控循环 SL 维护两段（L4881-4887 / L5038-5044，滚动止损）

### Q6：哪些函数可以清理 state？

`clear_batch_state`（L1275-1285）是唯一删除入口：**纯删 JSON + 清内存告警键，零 API**
（`_persist_states` L1248 是唯一落盘通道，调用方持 `_state_lock`）。
调用点全量 10 处：

| 调用点 | 上下文 |
|---|---|
| L1546 | `recover_active_batches` 启动恢复清理陈旧批次 |
| L3978 / L4044 / L4052 | 监控循环持仓归零三分支（含 settled_by_limit_close / pending_close） |
| L4121 / L4551 / L4720 | 监控循环 TP/SL 触发结算分支 |
| L5430 / L5452 | 监控循环 finally 清理 |
| L6119 | `close_position_market` 成功路径 |

⚠️ 交易所残单收敛义务完全在调用方：`_cancel_limit_close_order`（L6165）注释自认
"调用点必须紧接着 clear_batch_state"。本次事故证明该约定不成立（结算只撤 SL、
跳过结算分支不撤、finally 无持仓清理只 clear）。

### Q7：哪些线程可以修改 batch state？

| 线程 | 修改通道 | 写点 |
|---|---|---|
| asyncio executor 工作线程（bot_runner run_in_executor） | execute_signal / update_batch_tp / update_batch_sl / set_breakeven_sl / cancel_open_orders / close_position_market / close_position_limit | 各函数内 load→mutate→save |
| 监控线程（spawn 于 L2774 execute_signal / L1519 恢复） | `_start_monitoring` 循环 | L5360 整批保存 + 多处 reload-merge 保存（L4402/L4655/L4926/L5087/L5115/L5242/L5280/L5400） |
| 限价平仓结算线程（spawn 于 L6334） | `_monitor_limit_close` | L6426 / L6470 |
| 日报线程 L206 / TG 通知线程 L609 | **只读不写** | — |

**3 类写线程并存**。所有保护单 create 发生在前两类 → 风控冻结必须同时约束
命令线程（update_batch_tp/sl、set_breakeven）与监控线程（R14/维护段/逐层挂单）。

### Q8：哪些地方存在 stale snapshot（load→长时间处理→整批覆盖 save）？

| # | 位置 | 窗口内容 | 后果 |
|---|---|---|---|
| 1 | **监控循环 L4360 → L5335/L5360** | 整轮监控（fetch_open_orders + 多 API + create + verify），数秒~分钟 | **B5 实证**：`latest_b_data.copy()` 只保留快照字段，settled_by_limit_close 等不在 update 清单 → 整批覆盖丢失 |
| 2 | `close_position_limit` L6194 → L6227 | 撤 TP + fetch_ticker + create LIMIT | 可覆盖结算线程写入 |
| 3 | `close_position_market` L6004 → L6036 | 市价平仓单 + 撤单 | 同上 |
| 4 | `cancel_open_orders` L5905 → L5927/5963/5982 | 逐个撤单 | 同上 |
| 5 | **`save_batch_state` L1270-1272 无条件重建批次** | 任何在途旧快照保存 | **批次复活**：close_position_market 线程 L6119 clear 后，监控线程 L5360 用陈旧快照保存 → 已删除批次以 is_active=True 回魂（比 B5 更进一步） |

对照已有好范式（修复可复用）：`update_batch_tp` L1670-1672 save 前 reload+merge；
`_monitor_limit_close` L6420 load 即改即存（短窗口）。

### Q9：create_order() 前最终闸门放在哪里？

现状：`_assert_create_allowed`（L2990-3045）是既有中央仲裁，13 个调用点已覆盖
Q2/Q3 全部 create（**除 execute_signal 开仓路径**——batch 刚创建无竞态面）。
已有能力：registry 状态机禁建（PENDING_CREATE/CONFIRMED/MISMATCH/未决态）+
HARD_LOCK + 全局 cooldown + **每次实时 load_all_states**（L3012）。

三个缺口（本次事故实证）：

1. **不检查关闭状态**：pending_close / is_programmatic_cancel / close_phase 一概不读
   → 事故路径直接放行
2. **批次已清除 = 允许**：L3013-3016 `b is None` → `return True, ''`（"首次创建"
   语义）→ **场景 B 结算后 create 的放行通道**
3. **判定与执行之间存在 TOCTOU 窗口**：R14 路径 L5134/L5179 判定 → PENDING_CREATE
   落盘 → create_order，中间多步 → 场景 B 的 #012 判定合法、#013 结算、#017 create

建议落点（最小改动，不新增大框架）：

- **G1（闸门扩展）**：`_assert_create_allowed` 内新增检查：批次必须存在（b is None
  且非开仓路径 → 拒绝）+ pending_close / is_programmatic_cancel / close_phase ≥
  CLOSE_REQUESTED → 拒绝。所有 13 个调用点自动受益。
- **G2（create 紧前最终复核）**：各 create 调用点在 `create_order` 紧前重读磁盘
  state，任一关闭信号 → abort + 回滚 PENDING_CREATE（防 TOCTOU）。
- **G3（Commit 前复核 + 收敛）**：create 返回后、写 registry/tp_order_id 前，复核
  批次仍 ACTIVE；若已关闭 → **立即撤销刚创建的单**，不留孤儿。

### Q10：如何保证 CLOSED → ACTIVE 永不可能发生？

四件事叠加：

1. 新增单调字段 `close_phase ∈ {ACTIVE, CLOSE_REQUESTED, CLOSE_SETTLING, CLOSED}`，
   只进不退；3 个入口（Q1）统一写 CLOSE_REQUESTED，结算线程写 CLOSE_SETTLING，
   clear 前写 CLOSED
2. **回滚点收紧**：L6151/L6351 回退仅允许 `phase==CLOSE_REQUESTED 且未发生任何
   成交/结算`；一旦进入 SETTLING/CLOSED 永不回退
3. **save 相位棘轮**：`save_batch_state` 合并时若磁盘 close_phase 等级 > 写入快照
   等级 → 保留磁盘值（旧快照不得降级 = B5 的语义级修复，而非只保一个字段）
4. **防批次复活**：`clear_batch_state` 写墓碑（batch_id+时间戳，TTL 7 天）；
   `save_batch_state` 见墓碑 → 拒绝重建该批次（解决 Q8 #5）

### Q11：如何保证 clear_state 之前交易所残单已经归零？

结算路径（_monitor_limit_close 及全部 clear 调用点）执行**两源扫描收敛**：

1. state 已知 id 全集：tp_order_id / current_sl_id / entry_orders 各层 id /
   limit_close_order_id / protection_registry 内全部 order_id
2. 交易所实况：`fetch_open_orders(symbol)` 按 positionSide + 订单类型
   （TAKE_PROFIT_MARKET/STOP_MARKET/entry LIMIT）过滤本批次残单
   （registry order_id 可精确归属；无归属者进兜底扫描，见 Q12 防线 3）

验收门禁（全部满足才允许 clear）：

```text
position = 0
AND state 已知 id 残单数 = 0
AND fetch_open_orders 本批次残单数 = 0
AND limit close order = closed/canceled
```

撤单失败 / API 异常 → **不 clear**，🚨【资金安全】critical 告警 + HARD_LOCK
（Fail-Closed，与既有不变量①⑧一致）。
`clear_batch_state` 保持零 API（架构边界：纯状态层），收敛义务封装为调用方统一
结算序列（建议新增 helper `_converge_batch_orders_before_clear`，供三个平仓入口
与监控结算分支复用）。

### Q12：B 场景（create 已进入等待 → batch 被关闭 → create 恢复）最终仍不能创建 TP？

三重防线叠加，各自独立成立：

1. **防线 1（G2 create 紧前复核）**：create 恢复执行瞬间重读磁盘——pending_close /
   close_phase / 批次不存在 → abort。场景 B 时间线 #016 gate 释放后、#017 create
   前被拦
2. **防线 2（G3 Commit 前复核）**：拦"create 与复核之间又发生结算"的残余窗口——
   批次已 clear（b is None）→ 撤掉刚创建的单 + 不写 registry
3. **防线 3（结算两源扫描兜底）**：CLOSE_SETTLING 在最终 clear 前 fetch_open_orders
   全扫——即使前两道漏掉，无主保护单（不在任何 active batch registry 中）被发现 →
   撤销 + 告警。同时天然覆盖"state 无 TP ID 但交易所有 TP"（ChatGPT 场景 E）与
   跨批次污染（场景 G）

---

## 二、N14 语义分离（不回滚，改为显式状态）

ChatGPT 裁定"主动撤单"和"异常缺单"不能用同一状态表达。当前 `tp_order_id=None`
五义（未创建/创建失败/程序主动撤/已终结/正在平仓）。最小改法：

- N14（close_position_limit L6274）**不再清空 tp_order_id**，改为保留 id +
  registry 记 `state='PROGRAMMATIC_CANCELED'`（视为已终结，create 闸门禁建）
- state 层以既有 `is_programmatic_cancel=True`（L6225 已写）+ 新 close_phase 为准
- R14 判定（L4638）同时读 close_phase/is_programmatic_cancel，`None` 不再是
  "缺失需补"的充分条件

---

## 三、修复分层（供三轮裁定排批次，全部待批后才动代码）

| 批次 | 内容 | 对应 GREEN 场景 |
|---|---|---|
| Batch A：风控冻结 + create 闸门 | close_phase 字段 + Q1 三入口写相位；G1/G2/G3；R14/SL/TP 维护段入口冻结；命令线程（/tp /sl 保本）同冻结 | A、B、C、D |
| Batch B：结算残单归零 | _monitor_limit_close 撤 TP + `_converge_batch_orders_before_clear` 两源扫描 + clear 前验收门禁 + Fail-Closed | E、G、残单归零断言 |
| Batch C：防回退/防复活 | save 相位棘轮 + 墓碑 + 回滚点收紧 | F、B5 |

每批纪律：先跑 test_close_race_replay.py 对应断言转绿 → 全量回归 30 文件 →
备份 → 逐项呈报。

**明确不做**（与二轮裁定一致）：全局 StateManager / Event Sourcing / 数据库 /
CAS / Redis / 全状态机框架 / 大规模锁改造 / 回滚 N14。

---

## 四、GREEN 目标测试映射（升级现有 test_close_race_replay.py）

| ChatGPT 场景 | 测试落点 | 判定 |
|---|---|---|
| A：监控维护先于成交 | 现有场景 A 断言 1/3/4/7 翻绿 | 补挂=0、残单=0、ord2 被收敛 |
| B：成交先于维护完成 | 现有场景 B 断言 1/3/4/7/B5 翻绿 | gate 释放后 create 被 G2 拦；旗标存活 |
| C：rolling TP 禁止 | 新增：CLOSE_REQUESTED 期间驱动 TP 维护 need_update 路径 | 无 modify/create |
| D：TP fetch 失败不 recreate | 新增：F3 裁决 fetch 抛异常 | 不进入补挂 |
| E：state 无 TP ID 但交易所有 TP | 新增：结算时 tp_order_id=None + 交易所残单在场 | 两源扫描发现并撤 |
| F：旧快照不得回退 | 新增单测：磁盘 phase=CLOSE_SETTLING + 旧快照 ACTIVE 保存 | 棘轮保留高相位 |
| G：跨批次污染 | 新增：旧批次孤儿 TP + 同 symbol 新批次建仓 | 兜底扫描发现无主单 → 撤 + 告警 |

---

## 五、验收硬标准（引自二轮裁定，原文照录）

> 凡是一个 batch 已经进入 CLOSE_REQUESTED，是否还有任何路径能够增加风险、
> 增加订单、恢复保护单？**答案必须是：不能。**

以 Q2/Q3 全部 create 点 + Q4/Q5 全部 modify 点逐一生效拦截测试作为关闭条件，
配合现有 9 项 RED 断言全数翻绿 + 全量回归绿。
