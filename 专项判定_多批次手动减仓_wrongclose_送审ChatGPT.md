# 只读专项判定书：同方向多批次 + 手动部分减仓 → wrong-close / protection-loss

> 2026-09-01 ｜ 只读取证，未改任何代码 ｜ 判定：**确认 P0**（强候选转正）
> ChatGPT 的假设修正已采纳并查证：**超量不会开反向仓**（见②），风险收敛为 wrong-close + 错账 + 跨批次污染。

---

## ① 守恒破坏后，场上残留的保护单数量（源码实证）

SL/TP 数量来源 = **各批次自己的台账量**：

- 预生成路径：`sl_params['amount'] = batch_filled_amount`（L6485）
- 维护替换路径：同源台账推导（L5649 起的同步维护）

数字剧本（A/B 各成交 0.001，各挂 SL/TP 0.001，App 手动减 0.001）：

```
交易所 LONG 实际 = 0.001
A 台账 0.001 · A SL 0.001 · A TP 0.001
B 台账 0.001 · B SL 0.001 · B TP 0.001
部分减仓检测：多批次 → 静默跳过（L4994-5000 回避策略，零告警）
```

**场上 4 张保护单全部数量失真，且程序无任何感知。**

## ② Binance Hedge Mode 超量语义（查证结果）

- Hedge Mode 下 `positionSide=LONG + side=SELL` **只能减 LONG，不能开 SHORT**（SHORT 必须显式 `positionSide=SHORT`）——社区与官方 FAQ 一致表述为「hedge mode 下这类单已经是 reduce only」
- 超量（剩余 0.0005 也要平 0.001）→ **-2022 ReduceOnly rejected 或按剩余截断**，不会反向开仓
- **ChatGPT 的假设修正确立**：我此前的「意外 SHORT 敞口」尾部被排除；风险上限 = protection-loss + 台账发散，不是反向敞口

## ③ 触发后的发散链（源码实证）

```
A 的 SL 先触发 → SELL 0.001 成交 → 平掉唯一剩余 0.001（实为 B 的仓）
→ aggregate LONG = 0
→ A、B 两个 monitor 都命中归零分支（current_actual_position == 0 是 symbol 级判断，L4934）
→ A 按自己台账结算 0.001（把 B 的平仓错记成 A 的）→ clear A
→ B 按自己台账结算 0.001（同一笔平仓再错记一次）→ clear B
→ 双重错账 PnL 播报，真实盈亏归属完全失真
→ B 的 SL（0.001）残留交易所：仓位 0 期间触发被拒（-2022）；
   若之后开新 LONG 批次 → 旧 SL 可能触发 → 错误平掉新批次的仓（跨批次污染）
```

补充确认点（不改变判定）：归零 clear 时 B 的残留 TP 是否被撤——未逐行验证，列入 v6.4 审计表。

## ④ 守恒逻辑复用：可以，检测成本极低

现成件：`_survey_same_side_batches` 的 `sum_all`（Σ tracked）+ `_get_current_position_amt` 的 actual。

```
actual < sum_all − tolerance  →  ATTRIBUTION_CONFLICT（账本与交易所不守恒，
                                 任何 batch 级数量判断不可信）
```

最小检测落点 = 部分减仓检测的多批次跳过分支（L4994-5000）：把「静默跳过」改为「先守恒检查，冲突 → critical」。**零新机制、零新存储、零新锁。**

---

## 判定

**确认 P0：wrong-close（A 的 SL 平掉 B 的仓）+ 双重错账结算 + 跨批次污染尾部。**

- 不是理论：手动减仓是已发生的真实用户行为（部分减仓检测的存在本身即证明）
- 触达概率上升：10s 轮询后同方向双批次并存正成为常态用法
- 自动分摊（FIFO/LIFO）明确不做——「少了 0.001」无法证明是谁的，不能把未知伪装成已知
- 最小处置方向（需单独设计，今天不定）：检测（④的守恒比较）+ critical 告警 + 人工 reconcile 提示；自动动作（撤谁的保护单/是否平仓）不在第一版
