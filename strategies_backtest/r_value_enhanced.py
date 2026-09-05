"""R值增强分析：1.R分布图 2.日线EMA70偏离度 3.≥8%波段时间距离分布 4.止盈12-18%对比。"""
import os
import sys
import time as _time
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
print("加载4H数据...")
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
highs = df["high"].to_numpy()
lows = df["low"].to_numpy()
closes = df["close"].to_numpy()
times = df["open_time"].tolist()
n = len(df)
print(f"共 {n} 根4H K线，{len(signals)} 个原始信号")


# ========== ATR ==========
def calc_atr(high, low, close, length=20):
    m = len(close)
    tr = np.zeros(m)
    tr[0] = high[0] - low[0]
    for i in range(1, m):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    av = np.zeros(m)
    av[0] = tr[0]
    a = 1.0 / length
    for i in range(1, m):
        av[i] = a * tr[i] + (1 - a) * av[i - 1]
    return av


atr_vals = calc_atr(highs, lows, closes, 20)


# ========== 日线数据 + EMA70 ==========
def load_daily():
    return load_klines(cfg, timeframe="1d")


def calc_ema(values, length):
    k = 2.0 / (length + 1)
    r = np.zeros(len(values))
    r[0] = values[0]
    for i in range(1, len(values)):
        r[i] = values[i] * k + r[i - 1] * (1 - k)
    return r


print("加载日线数据...")
daily_df = load_daily()
daily_ema70 = calc_ema(daily_df["close"].to_numpy(), 70)
daily_df["ema70"] = daily_ema70
daily_df["date"] = daily_df["open_time"].dt.date
date_to_ema = dict(zip(daily_df["date"], daily_df["ema70"]))
print(f"日线EMA70计算完成，最新值: {daily_ema70[-1]:.2f}\n")


# ========== 回测函数 ==========
def simulate_leg_r(signal, be_bars=3, tp=0.12):
    i = signal.idx
    if i + 1 >= n:
        return None
    bt = signal.breakthrough
    d = signal.direction
    av = atr_vals[i - 1] if i - 1 >= 0 else np.nan
    if np.isnan(av) or av <= 0:
        return None
    if d == "long":
        init_stop = bt - av
        tp_price = bt * (1 + tp)
    else:
        init_stop = bt + av
        tp_price = bt * (1 - tp)
    max_exc = -np.inf
    peak_idx = i
    exit_idx = exit_price = exit_reason = None
    for j in range(i + 1, n):
        ks = kama_vals[j - 1]
        if np.isnan(ks):
            continue
        if d == "long":
            exc = (highs[j] - bt) / bt
        else:
            exc = (bt - lows[j]) / bt
        if exc > max_exc:
            max_exc = exc
            peak_idx = j
        bh = j - i
        if bh <= be_bars:
            stop_line = init_stop
            s_type = "atr_stop"
        else:
            if d == "long":
                stop_line = max(ks, bt)
                s_type = "breakeven" if ks < bt else "kama_trail"
            else:
                stop_line = min(ks, bt)
                s_type = "breakeven" if ks > bt else "kama_trail"
        if d == "long" and lows[j] < stop_line:
            exit_idx, exit_price, exit_reason = j, float(stop_line), s_type
            break
        if d == "short" and highs[j] > stop_line:
            exit_idx, exit_price, exit_reason = j, float(stop_line), s_type
            break
        if tp > 0:
            if d == "long" and highs[j] >= tp_price:
                exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"
                break
            if d == "short" and lows[j] <= tp_price:
                exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"
                break
    if exit_idx is None:
        exit_idx = n - 1
        exit_price = float(closes[-1])
        exit_reason = "end"
    if d == "long":
        ret = (exit_price - bt) / bt
        r = (exit_price - bt) / av
    else:
        ret = (bt - exit_price) / bt
        r = (bt - exit_price) / av
    sig_date = pd.to_datetime(signal.time, utc=True).date()
    ema70 = date_to_ema.get(sig_date, np.nan)
    dev = (closes[i] - ema70) / ema70 * 100 if not np.isnan(ema70) else np.nan
    return {
        "signal_time": signal.time, "direction": d, "breakthrough": bt,
        "atr": av, "atr_pct": av / bt, "close_at_signal": closes[i],
        "daily_ema70": ema70, "ema70_deviation": dev,
        "exit_time": times[exit_idx], "exit_price": exit_price,
        "final_return": ret, "r_value": r,
        "max_excursion": float(max_exc) if max_exc != -np.inf else 0.0,
        "duration_bars": exit_idx - i, "duration_hours": (exit_idx - i) * 4.0,
        "exit_reason": exit_reason, "signal_idx": i,
    }


# ========== 跑回测（12%止盈） ==========
results = []
last_exit = None
for s in signals:
    if last_exit is not None and s.time <= last_exit:
        continue
    r = simulate_leg_r(s, tp=0.12)
    if r is not None:
        results.append(r)
        last_exit = r["exit_time"]
rdf = pd.DataFrame(results)
filtered = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
print(f"12%止盈: 合并后 {len(rdf)} 波段，≥10%过滤后 {len(filtered)} 波段\n")

out_dir = cfg.base_dir / "results"

# ========== 需求1：R值分布统计图 ==========
print("=" * 90)
print("需求1：R值分布统计图")
print("=" * 90)
r = filtered["r_value"]
fig, axes = plt.subplots(2, 2, figsize=(14, 10))
fig.suptitle("R值分布统计（ATR止损+3K保本+KAMA跟踪+12%止盈）", fontsize=14, fontweight="bold")

ax = axes[0, 0]
ax.hist(r, bins=20, edgecolor="black", color="steelblue", alpha=0.7)
ax.axvline(r.mean(), color="red", linestyle="--", linewidth=2, label=f"均值={r.mean():.2f}R")
ax.axvline(r.median(), color="green", linestyle="--", linewidth=2, label=f"中位={r.median():.2f}R")
ax.set_xlabel("R值"); ax.set_ylabel("频次"); ax.set_title("R值直方图（全部）")
ax.legend()

ax = axes[0, 1]
long_r = filtered[filtered["direction"] == "long"]["r_value"]
short_r = filtered[filtered["direction"] == "short"]["r_value"]
ax.hist(long_r, bins=15, edgecolor="black", color="orange", alpha=0.6, label=f"做多(n={len(long_r)})")
ax.hist(short_r, bins=15, edgecolor="black", color="purple", alpha=0.6, label=f"做空(n={len(short_r)})")
ax.set_xlabel("R值"); ax.set_ylabel("频次"); ax.set_title("多空R值对比")
ax.legend()

ax = axes[1, 0]
reasons = filtered["exit_reason"].value_counts()
colors = {"take_profit": "#2196F3", "kama_trail": "#4CAF50", "atr_stop": "#F44336", "breakeven": "#FF9800", "end": "#9E9E9E"}
bar_colors = [colors.get(x, "gray") for x in reasons.index]
ax.barh(range(len(reasons)), reasons.values, color=bar_colors, edgecolor="black")
ax.set_yticks(range(len(reasons)))
labels_map = {"take_profit": "固定止盈12%", "kama_trail": "KAMA跟踪止盈", "atr_stop": "ATR止损", "breakeven": "保本平仓", "end": "持有至末端"}
ax.set_yticklabels([labels_map.get(x, x) for x in reasons.index])
ax.set_xlabel("单数"); ax.set_title("平仓方式分布")

ax = axes[1, 1]
ax.boxplot([long_r, short_r], tick_labels=["做多", "做空"], patch_artist=True,
           boxprops=dict(facecolor="lightblue"), medianprops=dict(color="red", linewidth=2))
ax.set_ylabel("R值"); ax.set_title("多空R值箱线图")
ax.axhline(0, color="gray", linestyle="--", alpha=0.5)

plt.tight_layout()
plt.savefig(out_dir / "r_distribution.png", dpi=150, bbox_inches="tight")
print(f"图表已保存: strategies_backtest/results/r_distribution.png")

# ========== 需求2：日线EMA70偏离度 ==========
print("\n" + "=" * 90)
print("需求2：日线EMA70偏离度")
print("=" * 90)
dev = filtered["ema70_deviation"]
print(f"样本数: {dev.notna().sum()}")
print(f"偏离度(%): 均值={dev.mean():.2f}%  中位={dev.median():.2f}%  std={dev.std():.2f}")
print(f"  min={dev.min():.2f}%  max={dev.max():.2f}%")
print(f"  Q25={dev.quantile(0.25):.2f}%  Q75={dev.quantile(0.75):.2f}%")
print(f"\n做多 vs 做空 偏离度:")
for d, label in [("long", "做多"), ("short", "做空")]:
    sub = filtered[filtered["direction"] == d]["ema70_deviation"]
    print(f"  {label}: 均值={sub.mean():.2f}%  中位={sub.median():.2f}%  min={sub.min():.2f}%  max={sub.max():.2f}%")

print(f"\n偏离度分桶:")
bins_d = [-30, -20, -10, -5, 0, 5, 10, 20, 30, 50, 100]
print(f"{'区间(%)':>18} {'单数':>6} {'占比':>8} {'累计':>8}")
cum = 0
for k in range(len(bins_d) - 1):
    cnt = ((dev >= bins_d[k]) & (dev < bins_d[k + 1])).sum()
    cum += cnt
    if cnt > 0:
        print(f"[{bins_d[k]:>6}, {bins_d[k+1]:>6}) {cnt:>6} {cnt/len(dev):>8.1%} {cum/len(dev):>8.1%}")

# ========== 需求3：≥8%波段时间距离箱体分布 ==========
print("\n" + "=" * 90)
print("需求3：≥8%波段时间距离箱体分布（相邻波段信号K间隔，4H K线根数）")
print("=" * 90)
ge8 = rdf[rdf["max_excursion"].abs() >= 0.08].reset_index(drop=True)
print(f"≥8%波段数: {len(ge8)}")
if len(ge8) > 1:
    gaps = ge8["signal_idx"].diff().dropna().astype(int)
    print(f"时间距离(4H K线数): 均值={gaps.mean():.0f}  中位={gaps.median():.0f}  min={gaps.min()}  max={gaps.max()}")
    print(f"\n箱体分布:")
    bins_g = [0, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]
    print(f"{'区间(K线数)':>18} {'次数':>6} {'占比':>8} {'累计':>8}")
    cum = 0
    for k in range(len(bins_g) - 1):
        cnt = ((gaps >= bins_g[k]) & (gaps < bins_g[k + 1])).sum()
        cum += cnt
        if cnt > 0:
            print(f"[{bins_g[k]:>6}, {bins_g[k+1]:>6}) {cnt:>6} {cnt/len(gaps):>8.1%} {cum/len(gaps):>8.1%}")

# ========== 需求4：多空止盈12-18%对比 ==========
print("\n" + "=" * 90)
print("需求4：多空固定止盈 12%/13%/14%/15%/16%/17%/18% 效果对比")
print("=" * 90)
print(f"{'止盈':>6} {'方向':>6} {'单数':>6} {'均R':>8} {'中位R':>8} {'胜率':>8} {'均收益':>10} {'ATR止损':>8} {'KAMA止盈':>9} {'固定止盈':>9}")
print("-" * 90)
for tp in [0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18]:
    tp_results = []
    last_exit = None
    for s in signals:
        if last_exit is not None and s.time <= last_exit:
            continue
        r = simulate_leg_r(s, tp=tp)
        if r is not None:
            tp_results.append(r)
            last_exit = r["exit_time"]
    tp_df = pd.DataFrame(tp_results)
    tp_f = tp_df[tp_df["max_excursion"].abs() >= 0.10].reset_index(drop=True)
    for d, label in [("long", "做多"), ("short", "做空")]:
        sub = tp_f[tp_f["direction"] == d]
        sr = sub["r_value"]
        n_atr = (sub["exit_reason"] == "atr_stop").sum()
        n_kama = (sub["exit_reason"] == "kama_trail").sum()
        n_tp = (sub["exit_reason"] == "take_profit").sum()
        print(f"{tp:>6.0%} {label:>6} {len(sub):>6} {sr.mean():>8.2f} {sr.median():>8.2f} "
              f"{(sr>0).mean():>8.1%} {sub['final_return'].mean():>10.2%} "
              f"{n_atr:>8} {n_kama:>9} {n_tp:>9}")
    # 合并行
    sr = tp_f["r_value"]
    print(f"{tp:>6.0%} {'合并':>6} {len(tp_f):>6} {sr.mean():>8.2f} {sr.median():>8.2f} "
          f"{(sr>0).mean():>8.1%} {tp_f['final_return'].mean():>10.2%}")
    print()

# ========== 保存CSV ==========
df_out = filtered.copy()
for c in ["signal_time", "exit_time"]:
    df_out[c] = pd.to_datetime(df_out[c], utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M")
df_out.to_csv(out_dir / "r_value_enhanced.csv", index=False, encoding="utf-8-sig")
print(f"详细数据已保存: strategies_backtest/results/r_value_enhanced.csv")
print("=" * 90)
