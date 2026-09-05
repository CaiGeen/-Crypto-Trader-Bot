"""R值深度统计：累计R、曲线回撤、年度分组、连续盈亏、偏离度/ATR关联、百分位。"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

from config import BacktestConfig
from data_loader import load_klines, _build_exchange
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
closes = df["close"].to_numpy(); times = df["open_time"].tolist()
n = len(df)


def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m):
        av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

# 日线EMA70
daily_df = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1d.parquet")
k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
daily_df["ema70"] = de70
daily_df["date"] = daily_df["open_time"].dt.date
date_to_ema = dict(zip(daily_df["date"], daily_df["ema70"]))


def simulate(signal, tp=0.12):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if d == "long": init_stop = bt - av; tp_price = bt*(1+tp)
    else: init_stop = bt + av; tp_price = bt*(1-tp)
    max_exc = -np.inf; exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if d == "long" else (bt-lows[j])/bt
        if exc > max_exc: max_exc = exc
        bh = j - i
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if d == "long": stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"
        if d == "long" and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if d == "short" and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if d == "long" and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if d == "short" and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    ret = (exit_price-bt)/bt if d == "long" else (bt-exit_price)/bt
    r = (exit_price-bt)/av if d == "long" else (bt-exit_price)/av
    sd = pd.to_datetime(signal.time, utc=True).date()
    ema = date_to_ema.get(sd, np.nan)
    dev = (closes[i]-ema)/ema*100 if not np.isnan(ema) else np.nan
    return {"signal_time": signal.time, "direction": d, "atr_pct": av/bt,
            "r_value": r, "final_return": ret, "max_excursion": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "duration_bars": exit_idx-i, "ema70_dev": dev, "signal_idx": i}


results = []; last_exit = None
for s in signals:
    if last_exit is not None and s.time <= last_exit: continue
    r = simulate(s)
    if r: results.append(r); last_exit = r["signal_time"] if False else None
    if r: last_exit = times[r["signal_idx"] + r["duration_bars"]]

# 重新跑合并逻辑
results = []; last_exit = None
for s in signals:
    if last_exit is not None and s.time <= last_exit: continue
    r = simulate(s)
    if r:
        results.append(r)
        last_exit = times[r["signal_idx"] + r["duration_bars"]]

rdf = pd.DataFrame(results)
f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
f["signal_time"] = pd.to_datetime(f["signal_time"], utc=True)
print(f"≥10%过滤后 {len(f)} 波段\n")

r = f["r_value"]
out_dir = cfg.base_dir / "results"

# ========== 1. 核心指标 ==========
print("=" * 95)
print("R值深度统计（12%统一止盈）")
print("=" * 95)
wins = r[r > 0]; losses = r[r <= 0]
cum_r = r.sum()
print(f"\n{'─'*50}")
print(f"  波段数:         {len(f)}")
print(f"  累计R:          {cum_r:.1f}R  （每单1%风险→总收益{cum_r:.0f}%）")
print(f"  均值R:          {r.mean():.2f}R")
print(f"  中位R:          {r.median():.2f}R")
print(f"  std:            {r.std():.2f}")
print(f"  胜率:           {(r>0).mean():.1%} ({len(wins)}胜/{len(losses)}负)")
print(f"  平均盈利R:      {wins.mean():.2f}R")
print(f"  平均亏损R:      {losses.mean():.2f}R" if len(losses) else "  平均亏损R:      N/A")
print(f"  盈亏比(PF):     {wins.sum()/abs(losses.sum()):.2f}" if len(losses) else "  盈亏比(PF):     inf")
print(f"  Expectancy:     {r.mean():.2f}R/单")
print(f"  Sharpe-like:    {r.mean()/r.std():.2f}")
print(f"  累计收益%:      {f['final_return'].sum():.1%}")
print(f"{'─'*50}")

# ========== 2. 累计R曲线 + 最大回撤 ==========
f_sorted = f.sort_values("signal_time").reset_index(drop=True)
cum_curve = f_sorted["r_value"].cumsum()
peak = cum_curve.cummax()
drawdown = cum_curve - peak
max_dd = drawdown.min()
max_dd_idx = drawdown.idxmin()

fig, axes = plt.subplots(2, 2, figsize=(16, 10))
fig.suptitle(f"R值深度统计（{len(f)}波段，累计{cum_r:.1f}R）", fontsize=14, fontweight="bold")

ax = axes[0, 0]
dates = f_sorted["signal_time"]
ax.plot(dates, cum_curve, color="steelblue", linewidth=1.5)
ax.fill_between(dates, cum_curve, 0, where=cum_curve > 0, alpha=0.15, color="green")
ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
ax.scatter(dates.iloc[max_dd_idx], cum_curve.iloc[max_dd_idx], color="red", s=50, zorder=5)
ax.annotate(f"最大回撤\n{max_dd:.1f}R", (dates.iloc[max_dd_idx], cum_curve.iloc[max_dd_idx]),
            textcoords="offset points", xytext=(15, -30), fontsize=9, color="red",
            arrowprops=dict(arrowstyle="->", color="red"))
ax.set_title(f"累计R曲线（最大回撤 {max_dd:.1f}R）"); ax.set_ylabel("累计R"); ax.grid(alpha=0.3)

# ========== 3. 年度分组 ==========
ax = axes[0, 1]
f_sorted["year"] = f_sorted["signal_time"].dt.year
yearly = f_sorted.groupby("year").agg(n=("r_value", "count"), cumR=("r_value", "sum"),
    meanR=("r_value", "mean"), win=("r_value", lambda x: (x > 0).mean()))
colors_y = plt.cm.viridis(np.linspace(0.2, 0.8, len(yearly)))
ax.bar(yearly.index.astype(str), yearly["cumR"], color=colors_y, edgecolor="black")
ax.set_title("年度累计R"); ax.set_ylabel("累计R"); ax.grid(alpha=0.3, axis="y")
for idx, row in yearly.iterrows():
    ax.text(str(idx), row["cumR"] + 1, f"{row['n']}单\n{row['win']:.0%}", ha="center", fontsize=8)

print(f"\n—— 年度分组 ——")
print(f"{'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>8} {'胜率':>8} {'做多R':>8} {'做空R':>8}")
for yr, row in yearly.iterrows():
    sub = f_sorted[f_sorted["year"] == yr]
    lr = sub[sub["direction"] == "long"]["r_value"].sum()
    sr = sub[sub["direction"] == "short"]["r_value"].sum()
    print(f"{yr:>6} {row['n']:>6} {row['cumR']:>8.1f} {row['meanR']:>8.2f} {row['win']:>8.0%} {lr:>8.1f} {sr:>8.1f}")

# ========== 4. 连续盈亏 ==========
ax = axes[1, 0]
streaks = []; cur = 0; cur_type = f_sorted["r_value"].iloc[0] > 0
for rv in f_sorted["r_value"]:
    is_win = rv > 0
    if is_win == cur_type: cur += 1
    else: streaks.append((cur_type, cur)); cur = 1; cur_type = is_win
streaks.append((cur_type, cur))
win_streaks = [s[1] for s in streaks if s[0]]; loss_streaks = [s[1] for s in streaks if not s[0]]
max_ws = max(win_streaks) if win_streaks else 0; max_ls = max(loss_streaks) if loss_streaks else 0
ax.bar(range(len(f_sorted)), f_sorted["r_value"], color=["green" if x > 0 else "red" for x in f_sorted["r_value"]], alpha=0.7)
ax.set_title(f"逐单R值（最长连胜{max_ws}，最长连亏{max_ls}）"); ax.set_ylabel("R"); ax.grid(alpha=0.3)

print(f"\n—— 连续盈亏 ——")
print(f"  最长连胜: {max_ws}单    最长连亏: {max_ls}单")
print(f"  连胜分布: {sorted(win_streaks, reverse=True)[:10]}")
print(f"  连亏分布: {sorted(loss_streaks, reverse=True)[:10]}")

# ========== 5. EMA70偏离度 vs R值 ==========
ax = axes[1, 1]
dev = f_sorted["ema70_dev"]
ax.scatter(dev, f_sorted["r_value"], c=["orange" if d == "long" else "purple" for d in f_sorted["direction"]], alpha=0.6, edgecolors="black", linewidth=0.5)
ax.axhline(0, color="gray", linestyle="--", alpha=0.5); ax.axvline(0, color="gray", linestyle="--", alpha=0.5)
ax.set_title("EMA70偏离度 vs R值"); ax.set_xlabel("偏离度(%)"); ax.set_ylabel("R"); ax.grid(alpha=0.3)

print(f"\n—— EMA70偏离度分组 vs R值 ——")
print(f"{'偏离度区间':>16} {'单数':>6} {'累计R':>8} {'均R':>8} {'胜率':>8} {'均收益':>10}")
for lo, hi in [(-100, -15), (-15, -5), (-5, 5), (5, 15), (15, 100)]:
    sub = f_sorted[(f_sorted["ema70_dev"] >= lo) & (f_sorted["ema70_dev"] < hi)]
    if sub.empty: continue
    print(f"[{lo:>5}%, {hi:>5}%) {len(sub):>6} {sub['r_value'].sum():>8.1f} {sub['r_value'].mean():>8.2f} "
          f"{(sub['r_value']>0).mean():>8.0%} {sub['final_return'].mean():>10.2%}")

# ========== 6. ATR% vs R值 ==========
print(f"\n—— ATR%（止损空间）分组 vs R值 ——")
print(f"{'ATR%区间':>16} {'单数':>6} {'累计R':>8} {'均R':>8} {'胜率':>8} {'均收益':>10}")
for lo, hi in [(0, 0.015), (0.015, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 1)]:
    sub = f_sorted[(f_sorted["atr_pct"] >= lo) & (f_sorted["atr_pct"] < hi)]
    if sub.empty: continue
    print(f"[{lo:>5.1%}, {hi:>5.1%}) {len(sub):>6} {sub['r_value'].sum():>8.1f} {sub['r_value'].mean():>8.2f} "
          f"{(sub['r_value']>0).mean():>8.0%} {sub['final_return'].mean():>10.2%}")

# ========== 7. R值百分位 ==========
print(f"\n—— R值百分位 ——")
pcts = [1, 5, 10, 25, 50, 75, 90, 95, 99]
for p in pcts:
    print(f"  P{p:>2}: {r.quantile(p/100):>6.2f}R")

# ========== 8. 多空核心指标 ==========
print(f"\n—— 多空分开核心指标 ——")
print(f"{'方向':>6} {'单数':>6} {'累计R':>8} {'均R':>8} {'中位R':>8} {'胜率':>8} {'PF':>8} {'累计收益':>10}")
for d, label in [("long", "做多"), ("short", "做空")]:
    sub = f_sorted[f_sorted["direction"] == d]
    sr = sub["r_value"]; w = sr[sr > 0]; l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) else float("inf")
    print(f"{label:>6} {len(sub):>6} {sr.sum():>8.1f} {sr.mean():>8.2f} {sr.median():>8.2f} "
          f"{(sr>0).mean():>8.0%} {pf:>8.2f} {sub['final_return'].sum():>10.1%}")

plt.tight_layout()
plt.savefig(out_dir / "r_deep_stats.png", dpi=150, bbox_inches="tight")
print(f"\n图表已保存: strategies_backtest/results/r_deep_stats.png")
print("=" * 95)
