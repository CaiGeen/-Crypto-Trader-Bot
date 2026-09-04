# T1-C Entry-Fee 记账 —— 设计草案送审 ChatGPT（**v1.3 + 补充条款**，2026-09-04）

> 状态：**设计稿，未动任何生产代码**。v1.3 = 四项账本级阻断收口；**补充条款**
> = ChatGPT 三条 ADDENDUM（NEARLY ALIGNED → 补齐即批准 F1–F13 实施）。
> 修订链：v1.0 → v1.1（探针）→ v1.2 → v1.3（本稿+补充条款）。
> 事实基线：HEAD = 32eb10c。**本轮零 API 调用。**

---

## 0-A. 补充条款（三条 ADDENDUM，全部源码核实，2026-09-04 09:4x）

### 补 1：`pnl_partial` 同样强制降级入场费来源

- **源码实锤（文档与实现不一致）**：docstring L898 声称 `pnl_partial=True` 时
  「落 `prior_reduction_unknown` 标记」，但 record 构造体（L916-929）**从未写入**
  该字段——真实缺陷，随本条款一并修复；
- 最终降级规则（两条件任一即降级，不允许 actual）：
  ```text
  realized_reduce_amount > 0   → estimated + entry_note='partial_allocation_unknown'
  settlement_qty < net_qty（pnl_partial=True）→ estimated + entry_note='prior_reduction_unknown'
  ```
- 实现修复：`pnl_partial=True` 时 record **必须真正落**
  `prior_reduction_unknown=True` 字段（既有 docstring 语义兑现）；
- 测试并入 **F13**（market confirmed < 净量反例同时断言降级与标记落盘）。

### 补 2：resolver 降级金额来源入契约

```python
_resolve_order_fees(symbol, order_ref, expected_qty,
                    estimated_fee,          # ← 调用方传入的有限估算值
                    order_snapshot=None)
```

- 任何降级路径（查询失败 / 空 fills / 非 USDT / 数量不完整 / 超窗）→
  返回**调用方传入的 `estimated_fee`**，**绝不能返回 0**；
- `order_snapshot`（可选）：调用方已持有的订单详情（如结算流程内的
  fetch_order 结果）——有则免重复查询，无则 resolver 自查；
- `expected_qty` 仅用于 actual 资格校验（数量归因），不参与金额计算。

### 补 3：`fee_breakdown` 校验语义定死（不留实施期）

结合 SL/TP/市价三路径**不消费 `_record_realized_pnl` 返回值**的现实，
`raise` 会让整条 PnL 记录丢失——不可接受。定死为：

```text
未知扩展键        → 忽略 + loud console print（限频）
必需字段缺失/非法 → 仍保存基础 PnL，record 追加 fee_metadata_error=True
resolver 出口保证 → 交给落盘层的金额均为有限数（NaN/inf 在 resolver 出口拦截）
```

- 新增测试：fee_breakdown 元数据异常（缺字段/非法值/未知键三案例）→
  **net_pnl 记录必须照常落盘**（读实际 trade_stats.json），且
  `fee_metadata_error=True` 存在；
- F5/F6 编号缺口为文档问题，标记「由 F10/F11 取代」，不阻断。

---

## 0. v1.2 → v1.3 修订记录（四项，全部源码核实）

| # | ChatGPT 阻断 | 源码核实 | v1.3 方案 |
|---|---|---|---|
| 1 | `_record_realized_pnl` 落盘契约未成立；`-> None` 注解错误 | **实锤**：L891 注解 `-> None` 但函数返回 True/False；record 由函数内部构造，调用方无法传入 fee 字段 | keyword-only `fee_breakdown: dict \| None = None`，函数内**校验固定字段集**后写入 record；注解改 `-> bool`；测试读**实际 trade_stats.json** 验证字段落盘 |
| 2 | `fee_rem` 不能在所有 partial 情形声称为 actual | **成立**：/partial 保留未成交 ENTRY（positional ledger 永不压缩），partial 后新层成交时聚合比例公式错误分摊新层费（反例：0.45 ≠ 0.525） | `realized_reduce_amount == 0` → 允许 actual；曾 partial → 沿用现有估算基数 + `entry_fee_source='estimated'` + `entry_note='partial_allocation_unknown'`；本期不做逐层剩余费账本 |
| 3 | 缺「本条记录数量」归因校验 | **实锤**：L10482 `_pnl_partial = confirmed_filled_amount < current_filled_amount − 1e-12`——记录只覆盖本次成交量，v1.2 却扣全部 fee_rem | `entry_fee_for_record = remaining_entry_fee × settlement_qty / net_qty`；`expected_qty` 纳入 resolver 契约；入场只解析 `entry_orders[:last_filled_count]`；订单类型来自生产调用点/registry，禁止按 id 长度猜测 |
| 4 | 测试规格错误 | **实锤**：F7 数字错；F4 断言错（限价 full-fill 本就必须进 phase 2）；F11 只有 AST 不够 | F7 采纳 ChatGPT 数字；F4 改「fee 降级不得**额外**改变既有 close_phase/clear gate/恢复语义」；F11 四路径行为断言（读实际落盘）+ 两个新反例 |

**新增事实（本轮源码核实，修正 v1.2 的错误认知）**：入场单是 `STOP_MARKET`
条件单（L4950-4957 实锤）——**批次全生命周期的订单里，只有市价平仓与限价平仓
是普通单**，入场全部层 + SL/TP 都是 algo 条件单 → ChatGPT 的 API 成本表修正
（最多 4+ 次 algo 映射）成立且采纳。

---

## 1. 落盘契约（阻断 1 收口）

`_record_realized_pnl` 签名变更（唯一签名改动，一次定死不留实施期）：

```python
def _record_realized_pnl(self, batch_id, symbol, side, amount, avg_price,
                         exit_price, net_pnl, mode, pnl_partial=False,
                         dedup_key=None, stats_file=None, *,
                         fee_breakdown: dict | None = None) -> bool:
```

- `fee_breakdown` 固定字段集（白名单校验，未知键 raise/丢弃+console 告警——
  实施期二选一，倾向 raise：资金字段宁可 loud）：
  ```python
  {'entry_fee': float,            # 本条 PnL 实际扣除的入场费（定义见下）
   'entry_fee_source': 'actual'|'estimated',
   'entry_note': str,
   'entry_fee_total': float,      # 全部历史入场费（若与实际扣除不同才写）
   'exit_fee': float, 'exit_fee_source': ..., 'exit_note': str,
   'fee_note': str}
  ```
- **字段定义对账铁律**：`net_pnl` 里扣掉的入场费 = `entry_fee`，逐笔可对账；
  历史全量入场费只能进 `entry_fee_total`，绝不与 `entry_fee` 混用；
- record 写入这些固定字段；测试**读实际 trade_stats.json** 断言字段落盘；
- `-> None` → `-> bool`（函数实际返回 True/False，docstring 已写明）。

## 2. 入场费规则（阻断 2 + 3 收口）

### 2.1 剩余入场费的口径

```
remaining_entry_fee =
    realized_reduce_amount == 0（无 partial 史）:
        Σ resolve(entry_orders[:last_filled_count])        # 可 actual
    曾发生 partial:
        total_entry_fee × (net_cost / gross_cost)          # 仅估算
        entry_fee_source='estimated', entry_note='partial_allocation_unknown'
```

- 反例依据（ChatGPT）：L1 费 0.50 → partial 减半（剩 0.25）→ L2 新层成交费
  0.20 → 正确剩余 0.45；聚合比例式得 0.525（L2 费被错误卷入早期分摊）→
  **曾 partial 一律 estimated，不猜**；
- 不扩张逐层剩余费账本（本期明确不做）。

### 2.2 本条记录的入场费（数量归因）

```
entry_fee_for_record = remaining_entry_fee × (settlement_qty / net_qty)
```

- 市价路径反例：`confirmed_filled_amount < current_filled_amount`
  （L10482，`pnl_partial=True`）→ 记录只覆盖本次成交量，费只扣对应份额；
- **resolver 契约新增 `expected_qty`**：actual 必须同时满足
  ① fills 合计量 ≈ 订单权威成交量（executedQty / actualQty，≤1e-6）
  ② 订单成交量 ≈ `expected_qty`；
- 入场解析范围：**仅 `entry_orders[:last_filled_count]`**——尾部可能是仍未
  成交的挂单（positional ledger 永不压缩），绝不全量解析；
- 订单类型（regular/algo）由**生产调用点或 registry** 显式给出
  （`order_kind` 字段已存在于 registry），禁止按 id 长度猜测。

## 3. 统一公式（v1.2 §2 维持 + F7 修正数字）

```python
# 全部四路径统一（入场费只进 total_fees；avg 净基准）
gross_pnl = (exit_price − net_cost/net_qty) × settlement_qty     # (BUY)
net_pnl   = gross_pnl − (entry_fee_for_record + exit_fee)
```

**F7 反例数字（采纳 ChatGPT）**：net_cost=153.7704（0.002@76885.20）、
fee_rem=0.0770、exit=0.002@77885.20、exit_fee=0.0389：

```text
旧公式：gross = 1.9230；net = 1.9230 − 0.0770 − 0.0389 = 1.8071   （双扣）
新公式：gross = 2.0000；net = 2.0000 − 0.0770 − 0.0389 = 1.8841
```

## 4. 接线表（四真实点，行为断言见 §5）

| # | 路径 | 位置 | 出场单类型 | 入场费 |
|---|---|---|---|---|
| 1 | 限价 finalizer | L4296 | 普通限价单（直查） | §2 全规则 |
| 2 | SL 触发结算 | L7380 | **algo 映射链** | §2 |
| 3 | TP 触发结算 | L7551 | **algo 映射链** | §2 |
| 4 | 市价平仓确认 | L10483 | 普通市价单（直查） | §2（含数量归因） |

- 入场单全部为条件单（L4950 实锤）→ **每层入场都走映射链**：API 成本 =
  层数 + SL/TP 次 algo 映射（各 weight 0.1 精查 / 5 列表）——典型 3 层入场 +
  1 出场 ≈ 4+ 次，权重极低，不构成阻断（采纳 ChatGPT 修正）；
- TG 消息「持仓均价」标注**「入场均价（未含手续费）」**（开放问题 1 裁决照办）。

## 5. 测试计划（F1–F13）

| # | 内容 | 关键断言 |
|---|---|---|
| F1 | 普通单直查 actual | fee/source/note 正确 |
| F2 | algo 映射链 | actualOrderId 命中 → actual；空 → estimated+note |
| F3 | 非 USDT | estimated + note='non_usdt_commission' |
| F4 | 查询失败不阻塞 | **fee 降级不得额外改变既有 close_phase / clear gate / 恢复语义**（限价 full-fill 本就必须进 phase 2——v1.2 断言作废重写） |
| F7 | 公式反例 | 旧 1.8071 / 新 1.8841（§3 数字） |
| F8 | partial 估算分摊 | SL 路径扣 fee_rem=0.077 而非全量 0.154 |
| F9 | fills 不完整 | estimated + note='fills_incomplete' |
| F10 | 多层 mixed | entry=actual+estimated 层合并 → 整体 estimated + note |
| F11 | **四路径行为断言**（非仅 AST） | 每路径：正确订单 ID、expected_qty、最终 net_pnl、**读实际 trade_stats.json 验证 fee 字段落盘** |
| F12 | **partial 后新层成交反例** | 新层费被错误卷入早期分摊 → 必须 estimated（0.45≠0.525 反例） |
| F13 | **market confirmed < 净量反例** | entry_fee_for_record 按比例份额；expected_qty 契约生效 |
| — | 回归 | 42 rc=0 + P6 12 项零新增；F7 数字为回归锚 |

## 6. KNOWN_LIMITATION（开放问题裁决记录 + 可度量升级触发）

| # | KNOWN_LIMITATION | UPGRADE_TRIGGER（可度量） |
|---|---|---|
| 1 | 净成本均价采纳；TG 标注「入场均价（未含手续费）」 | —（口径已统一，无升级项） |
| 2 | /partial 不记录中间 PnL：fee note 只能揭示该限制，不能补回缺失 PnL | 「partial 中间段 PnL 记录」立项实施后，`entry_note='partial_entry_estimated'` 消失、partial 段 trade 记录出现 |
| 3 | `net_cost/gross_cost` 比例分摊只能作 partial 的估算，永不能标 actual | 逐层剩余手续费账本（每层记录 actual 费 + 剩余份额）落地后，`entry_note='partial_allocation_unknown'` 消失、`entry_fee_source='actual'` 占比可统计（目标 100%） |
| 4 | 非 USDT commission（BNB 等）不做历史汇率换算 | 接入 BNB/USDT 历史汇率服务后，`entry_note='non_usdt_commission'` 消失 |
| 5 | userTrades/algo 历史 7 天窗口外订单无法取 actual | 窗口策略变更或逐单归档（结算时即时解析即天然规避——本期已是即时代） |

**不再跑 API 探针**（映射链已由三轮探针实证）。

## 7. 改动面（含补充条款增量）

- `_record_realized_pnl`：注解修正 + `fee_breakdown`（唯一签名改动）；
- 新增 `_resolve_order_fees`（v1.1 §3 规则 + §2 expected_qty 契约）；
- 四路径接线（公式修正 + resolver 调用）；
- trade_stats 新字段：entry_fee/entry_fee_source/entry_note/entry_fee_total/
  exit_fee/exit_fee_source/exit_note/fee_note（缺失=estimated，向后兼容）；
- **无账本结构变更、无 trade_state 字段变更、无 bot_runner/watchdog 改动**。

---
*修订链：v1.0 → v1.1（探针实证）→ v1.2（账本设计）→ v1.3（本稿：落盘契约/
数量归因/partial 估算边界/测试规格修正）。*

---
*修订链续：v1.3 + 三条补充条款（2026-09-04 09:4x，NEARLY ALIGNED → 补齐即批准实施）。*
