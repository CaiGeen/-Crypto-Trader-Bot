"""波段涨跌幅报告（统一口径=触发线市价入场策略）。

策略（用户2026-08-22确认的三止损规则）：
  信号=6K极值突破；回调线严格模式；ATR=回调线当根收盘值
  入场=挂单在触发线（回调线±1ATR，自信号K开盘生效），触发即市价买入（成交价=触发线）
  止损：(1)初始止损=回调线，盘中触发按线价出（1H消歧成交当根先后顺序）
       (2)成交当K收盘<回调线±1.5ATR → 保护性平仓（按收盘价，止损优先）
       (3)买入后第3K收盘未打损 → 止损上移到成本线；此后止损=max/min(KAMA,成本线)
  止盈=回调线×(1+22%)/×(1-12%)，B合并
波段涨跌幅 = (峰值 − 回调线)/回调线，峰值=信号K..出场K区间最高/低（含信号K）
入场后涨跌幅 = 峰值改为成交K..出场K（从成交价算）
导出 results/波段涨跌幅_极值线vs回调线_v5.csv（北京时间，时间降序）
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import callback_atr_entry as m

res, nocb, nofill = m.run(m.simulate_new, "b", confirm_atr=1.5, same_bar_exit="1m")
times = m.df["open_time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M").to_numpy()
to_cn = {"atr_stop": "ATR止损", "breakeven": "成本线止损", "kama_trail": "KAMA跟踪(跌破KAMA)",
         "take_profit": "固定止盈", "confirm_exit": "保护性平仓", "end": "数据末尾"}

rows = []
for t in res:
    i = t["signal_idx"]; j = t["exit_idx"]; eb = t["entry_bar"]
    is_long = (t["dir"] == "long")
    cb = t["cb"]; bt = t["bt"]
    if is_long:
        seg = m.highs[i:j+1]
        pk = int(np.argmax(seg)) + i
        peak = float(seg.max())
        post = float(m.highs[eb:j+1].max()) if j >= eb else peak
        amp_cb = (peak - cb)/cb*100; amp_bt = (peak - bt)/bt*100
        amp_post = (post - t["entry_price"])/t["entry_price"]*100
        off = (bt - cb)/bt*100
    else:
        seg = m.lows[i:j+1]
        pk = int(np.argmin(seg)) + i
        peak = float(seg.min())
        post = float(m.lows[eb:j+1].min()) if j >= eb else peak
        amp_cb = (cb - peak)/cb*100; amp_bt = (bt - peak)/bt*100
        amp_post = (t["entry_price"] - post)/t["entry_price"]*100
        off = (cb - bt)/bt*100
    rows.append({
        "信号时间": times[i], "成交时间": times[eb], "成交方式": t["fill"],
        "方向": "做多" if is_long else "做空",
        "6K极值": round(bt, 2), "回调突破线": round(cb, 2), "回调偏移%": round(off, 3),
        "ATR": round(t["atr"], 2),
        "峰值价": round(peak, 2), "峰值K时间": times[pk],
        "波段涨跌幅%_回调线基准": round(amp_cb, 3),
        "波段涨跌幅%_极值基准": round(amp_bt, 3),
        "入场后涨跌幅%": round(amp_post, 3),
        "出场方式": to_cn[t["exit_reason"]], "出场时间": times[j],
        "持仓K数": t["hold"], "R值": round(t["r"], 4), "年份": t["year"],
    })
df = pd.DataFrame(rows)
df = df.sort_values("信号时间", ascending=False).reset_index(drop=True)
df.insert(0, "序号", range(1, len(df) + 1))

out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "波段涨跌幅_极值线vs回调线_v5.csv")
df.to_csv(out_path, index=False, encoding="utf-8-sig")

print("=" * 100)
print(f"波段涨跌幅（统一口径·触发线市价入场+三止损规则）| {len(df)}个波段")
print("=" * 100)
for lbl, col in [("回调突破线基准", "波段涨跌幅%_回调线基准"), ("6K极值基准(旧口径)", "波段涨跌幅%_极值基准")]:
    s = df[col]
    print(f"  {lbl}: 中位={s.median():.2f}% 均值={s.mean():.2f}% P25={s.quantile(.25):.2f}% P75={s.quantile(.75):.2f}%")

print(f"\n{'区间':>8} {'回调线基准':>14} {'极值基准':>14}")
for lo, hi, tag in [(0,2,"<2%"),(2,6,"2-6%"),(6,10,"6-10%"),(10,15,"10-15%"),(15,22,"15-22%"),(22,999,"≥22%")]:
    a = ((df["波段涨跌幅%_回调线基准"]>=lo)&(df["波段涨跌幅%_回调线基准"]<hi)).sum()
    b = ((df["波段涨跌幅%_极值基准"]>=lo)&(df["波段涨跌幅%_极值基准"]<hi)).sum()
    print(f"{tag:>8} {a:>6} ({a/len(df):>4.0%}) {b:>6} ({b/len(df):>4.0%})")

print(f"\n按出场方式（回调线基准）:")
for r, sub in df.groupby("出场方式"):
    print(f"  {r:>16}: {len(sub):>4}单 中位={sub['波段涨跌幅%_回调线基准'].median():>6.2f}% 均值={sub['波段涨跌幅%_回调线基准'].mean():>6.2f}%")

print(f"\n多空（回调线基准）:")
for d, sub in df.groupby("方向"):
    print(f"  {d}: {len(sub)}单 中位={sub['波段涨跌幅%_回调线基准'].median():.2f}% 均值={sub['波段涨跌幅%_回调线基准'].mean():.2f}%")

print(f"\n自查明细已导出: {out_path}（{len(df)}行，北京时间，降序）")
