# REST/API 权重消融审计 —— Phase 1（只读静态分析，零代码改动）

日期：2026-09-02（r2 修订：raw endpoint 按 ccxt 4.5.68 源码实锤后重算）｜ 方法：AST 全量枚举
`trader_260725.py`(104 调用点) + `bot_runner.py`(8 调用点)，按「端点 × 宿主函数 × 周期性/事件性」归类。

## 0. raw endpoint 映射（ccxt 4.5.68 源码实锤，非方法名推断）

| ccxt 方法 | raw endpoint（我们实际走的） | 官方 IP weight |
|---|---|---|
| fetch_positions | **/fapi/v3/positionRisk**（binance.py L10783，默认 v3；v2 仅 useV2 选项） | **5**（官方 Trade REST 页实锤） |
| fetch_balance | **/fapi/v3/account**（binance.py L3825，默认 v3） | 5（v3 账户类） |
| fetch_open_orders | /fapi/v1/openOrders（带 symbol） | 1 |
| fetch_order | /fapi/v1/order | 1 |
| fetch_ticker | /fapi/v1/ticker/24hr（带 symbol） | 1 |
| fetch_time / set_leverage | /fapi/v1/time · /fapi/v1/leverage | 1 · 1 |
| create/cancel_order | 0 weight（受订单频控 300/10s 约束） | 0 |

口径说明：2024-08-07 changelog 曾记录 v2 balance/account/positionRisk 5→10（ChatGPT 核实；
注：官方 changelog 与当前 API Reference 页面对 v2 权重表述存在不一致，勿作当前 v2 绝对事实），
但本程序经 ccxt 默认走 **v3（weight 5，官方 API Reference 实锤）**，v2 未被使用。
r1 报告的「1~5 区间」取上界即得 r2 数值。

## 1. 周期性调用面（只有两个 while 循环 + 一个 30s 节流）

| 驱动 | 周期 | 每周期 REST | 来源 |
|---|---|---|---|
| 批次监控主循环 `_start_monitoring`（每批次独立线程） | ≤2 批次：10~15s；3~4 批次：75~100s；5~6：90~120s；成交检测后 fast_poll=3s | **fetch_positions ×1（每周期无条件，weight 5）**；fetch_open_orders ×1（30s 节流，weight 1）；fetch_order ×2（仅当 SL/TP 疑似缺失才查）；fetch_ticker ×1（仅归零结算） | L5219/L5249/L5491/L5954/L6104 |
| 限价平仓监控 `_monitor_limit_close`（每笔在途限价单一线程） | **固定 3s** | fetch_order ×1（weight 1） | L9831-9847 |
| registry 自愈对账 | 30s 节流 | fetch_open_orders ×1 | L5234 |
| 启动恢复 | 事件 | set_leverage + fetch_positions + SL 验证 per 批次 | L2033+ |
| bot_runner 侧 | 全部事件驱动（TG 命令/信号），零周期轮询 | — | — |

## 2. 稳态预算（重算，r2 口径）

| 场景 | calls/min | weight/min（v3 口径） | 占 2400 限额 |
|---|---|---|---|
| 1 批次 | ~6.8 | **~26**（4.8×5 + openOrders 2/min） | ≈1.1% |
| 2 批次 | ~13.6 | **~52** | ≈2.2% |
| 3 批次（间隔切 75~100s 档） | ~7~9 | **~17** | ≈0.7% |
| 2 批次 fast_poll 突发（3s 双线程） | ~40 | **~200** | ≈8.4% |
| 限价平仓在途（每笔） | +20 | +20 | ≈0.8% |

**结论 1（措辞按 ChatGPT 收窄）**：静态预算**显著降低**了稳态轮询作为 418 主因的概率——
即使全部按上界 weight 计，静默运行 + fast_poll 突发仍 <10% 限额；但不写「洗清」，
Phase 1 是静态模型，真实一分钟峰值（多 monitor 同时醒来 + retry×3 + converge + 命令操作
+ 手机 App + 共享出口其它用户）需 Phase 2 观测数据。

## 3. 突发放大器（按嫌疑排序，候选原因非根因）

| 放大器 | 机制 | 峰值量级 |
|---|---|---|
| 操作密集期 | 今日 17:22-17:31：8 次信号扫描 + 2 次成交换挂 + 2 次 /partial 全链 + 孤儿清扫 | 无观测数据，Phase 2 补 |
| fast_poll | 成交检测后 3s 间隔双批次并行 | ~200 weight/min |
| 失败重试 | fetch_positions 失败重试 ×3；_safe_api_call 重试链 | ×3 |
| converge/自愈风暴 | converge 每次 openOrders ×4；异常态每轮重试 | 异常持续期成倍 |
| 恢复风暴 | 重启 per 批次 4~6 调用 + 防冲突扫描 | 批次多时瞬时几十次 |

## 4. 三分类（Phase 2 决策输入）

- **必要实时**：fetch_positions（裸仓窗口风控 5~15s 级，v6.2-P0-1 实盘教训）、成交后 fast_poll、确认链轮询——**不建议动**。
- **可降频**：`_monitor_limit_close` 固定 3s（限价单等待通常分钟级，可改 5~10s + 距成交价自适应）；converge 的 openOrders×4 可合并。
- **完全重复/可消融**：暂未发现纯重复调用点——104 处均为事件驱动或已有节流（动态间隔本身是历史降频成果）。

## 5. 结论

1. 静态预算**显著降低**稳态轮询主因概率（<10% 限额），但不写「洗清」——真实峰值归 Phase 2。
2. 17:31 封禁形成于操作密集期 + 共享出口叠加，归因保持 UNKNOWN（与 ChatGPT 冻结口径一致）。
3. **Phase 2（待批准，不改交易逻辑）**：`_safe_api_call` 单点薄观测层——**真实响应头优先**
   （X-MBX-USED-WEIGHT-1M / ORDER-COUNT-10S/1M 按 endpoint 计数），静态 weight 估值仅作辅助；
   含 429/418 前最后 60s 快照。观测证据将把「Bot 密集突发」与「邻居连坐」两种假设直接分开。
4. WebSocket 迁移：暂不做——静态数据不支持必要性，Phase 2 观测若推翻本报告再议。
