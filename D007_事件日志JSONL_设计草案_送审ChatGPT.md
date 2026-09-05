# D-007 最小事件日志（JSONL）—— 设计草案送审 ChatGPT（v1.0，2026-09-03）

> 状态：**设计稿，未动任何生产代码**。事实基线：HEAD = c2073f3（P6b）。
> 来源：阶段 1 路线图既有项「D-007 事件日志 JSONL」（D-005/D-006/D-009/D-010 同族）。

---

## 1. 问题定义

**现状：过程性证据只有 console print（终端关闭即失）**。事故复盘（-4061 型）时
无法回答「系统在事故前后各做了什么」——批次开启、成交、保护单挂撤、平仓事务
BEGIN/SETTLE、告警触发，全部没有持久化的结构化轨迹。现有持久层只覆盖终态：

| 载体 | 内容 | 缺口 |
|---|---|---|
| `trade_state.json` | 当前批次状态（不断被覆盖） | 无历史 |
| `trade_tombstones.json` | 批次终态摘要 | 无过程 |
| `trade_stats.json` | PnL 记录 | 无过程 |
| `.notify_queue/` | TG 备用通知（消费后删） | 仅告警类 |
| console print | 全部决策轨迹 | **易失、非结构化** |

## 2. 目标与非目标

**目标（最小边界）**：一条 append-only JSONL 轨迹，事后能按批次/时间重建
「发生了什么、顺序如何」。仅此而已。

**非目标（明确不做）**：
- 不是监控系统/告警通道（告警仍走 TG 三通道 + 3 次静默纪律，互不相干）；
- 不做查询/索引/看板；
- 不追求每事件落盘 durability（见 §5 失败语义——记账性，非资金安全）；
- v1 不含 bot_runner 进程（见 §6 多进程边界）。

## 3. 事件目录（v1 范围，12 类）

| type | 触发点（生产函数） | data 要点 |
|---|---|---|
| `batch_opened` | 建仓确认 | symbol/side/qty/杠杆 |
| `entry_fill` | 入场层成交捕获（L6441 邻域） | 层号/成交价/费(估) |
| `protection_placed` | SL/TP 挂单确认 | role/order_id/价格 |
| `protection_replaced` | 换挂（M1 先挂后撤） | 新旧 order_id |
| `partial_close` | _execute_partial_close 成功 | amount/net 均价 |
| `close_begin` | BEGIN 成功（市价/限价/partial 三入口） | op_id/类型 |
| `close_settled` | finalizer 结算成功（共享 finalizer） | op_id/net_pnl/fee_source* |
| `close_rolled_back` | 回滚成功 | op_id/why |
| `batch_cleared` | clear_batch_state 成功（墓碑前） | batch_id/reason |
| `alarm` | 任意 TG critical 发送处（守恒/冻结卡死/归因冲突…） | 摘要（复用消息首行） |
| `auth_blocked` / `auth_unblocked` | D-010 鉴权状态翻转 | 来源 |
| `closecancel` | /closecancel 提交与裁决结果 | 裁决四态 |

*`fee_source` 字段为 T1-C 预留，本设计不实现。

## 4. Schema（单行 JSON）

```json
{"v":1,"ts":"2026-09-03T21:00:00.123+08:00","epoch":1788447600.123,
 "type":"close_settled","symbol":"BTCUSDT","batch_id":"batch_20260903_x",
 "op_id":"…","data":{…事件自有字段…}}
```

- `type` 唯一必检字段；`symbol/batch_id/op_id` 可空（如 auth 类）；
- 单行 = 单次 `write()` + flush（行级完整性靠小行一次写入；**不做逐行 fsync**）。

## 5. 写盘与失败语义（关键设计约束）

- **Best-effort，绝不影响交易**：任何异常（磁盘满/权限/编码）→ 吞掉 + console
  print 一条降级提示（自身限频：每 60s 最多 1 条，防打印风暴）。事件日志是
  记账性组件，刻意与 Fail-Closed 字段区分（同 T1-C fee_source 逻辑）；
- **写法**：`open(path, 'a', encoding='utf-8')` 追加单行，每次开闭（不持长句柄
  ——多线程下最简单且行写入足够小）；**不做 fsync**（断电丢尾部数条可接受，
  权衡依据：状态权威在 trade_state.json，事件日志只做辅助重建）；
- **线程安全**：模块级一把 `threading.Lock` 包追加（多批次监控线程 + 主线程
  并发调）；
- **与既有纪律对齐**：JSONL 记录每次事件发生（不受 TG「3 次静默」约束——它是
  审计轨迹不是通知；`alarm` 事件的 msg 摘要由调用点传入，不重复构造）。

## 6. 轮转、保留与多进程边界

- **按日轮转**：`events_YYYYMMDD.jsonl`（写入时按当前日期取文件名，天然切日）；
- **保留 30 天**：进程启动时清理过期文件（按文件名日期解析，解析失败不动）；
- **目录**：`<repo>/events/`（与 `.notify_queue` 同级的运行产物目录）；
- **多进程边界（v1）**：仅 trader 进程写。bot_runner 的通知消费等暂不入
  （其关键动作大多由 trader 上报；如需再议跨进程方案——Windows 上多进程
  append 行原子性不保证，需单写者或命名锁，v1 刻意回避）。

## 7. 改动面

- 新增：模块常量（目录/保留天数）+ `_log_event(type, symbol=None, batch_id=None,
  op_id=None, **data)`（~40 行，含锁/轮转/清理/异常吞）+ 启动清理调用 1 处；
- 接线：§3 所列 12 类各 +1 行调用（部分在既有函数尾部，共约 12 行）；
- **不新增账本字段、不改 trade_state 结构、不改任何既有函数签名**；
- bot_runner/watchdog 零改动。

## 8. 测试计划（RED-first，E1–E5）

- **E1**：`_log_event` 写出合法 JSONL 单行（json.loads 逐行通过、schema 字段齐）；
- **E2**：故障注入（目录只读/路径为文件）→ 不抛异常、交易路径不受影响、
  console 降级提示限频（60s 内多次失败只 1 条提示）；
- **E3**：跨日轮转（mock 日期）→ 新旧文件各得其所；
- **E4**：保留期清理：伪造 31 天前文件 → 启动清理删除；非法文件名不误删；
- **E5**：并发：N 线程各写 M 条 → 行数 = N×M，每行 json.loads 可解析（无交叉写坏行）；
- 接线结构断言：§3 事件类型在 trader 源码中各有 ≥1 个 `_log_event('type'…)` 调用点
  （AST 断言，防「helper 建好没人用」的空转通过——P5 空转教训）；
- 回归：42 rc=0 基线 + orphan_guard 沙箱例外，零新增。

## 9. 开放问题（请 ChatGPT 裁决）

1. **事件目录（§3 的 12 类）**是否恰当？有无遗漏的关键决策点（如 SL 触发由
   交易所执行的被动事件——v1 不含「SL 成交」独立事件，因为交易所不回调，
   由持仓归零检测间接体现，是否需要合成一条？）；
2. **不逐行 fsync** 的取舍是否接受（断电丢尾部）；还是 critical 类事件
   （alarm）单独 fsync？
3. **保留 30 天**是否合适（磁盘占用估算：日常每天数百行，忽略不计）；
4. **bot_runner 进程不入 v1** 是否同意；
5. **文件命名/目录**（`events/events_YYYYMMDD.jsonl`）是否与既有运维习惯冲突。

---
*全部行号可在 GitHub c2073f3 直接核对。*
