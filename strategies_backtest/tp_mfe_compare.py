"""需求1：止盈12-18%对比加累计R  需求2：不同MFE阈值波段时间分布对比加累计R。"""
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
from data_loader import load_klines
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
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)


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
    return {"direction": d, "r_value": r, "final_return": ret,
            "max_excursion": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "signal_idx": i, "exit_idx": exit_idx}


def run_backtest(tp):
    results = []; last_exit_idx = None
    for s in signals:
        if last_exit_idx is not None and s.idx <= last_exit_idx: continue
        r = simulate(s, tp=tp)
        if r: results.append(r); last_exit_idx = r["exit_idx"]
    return pd.DataFrame(results)


out_dir = cfg.base_dir / "results"

# ========== 需求1：止盈12-18%对比（含累计R） ==========
print("=" * 100)
print("需求1：多空固定止盈 12%~18% 效果对比（含累计R）")
print("=" * 100)
print(f"{'止盈':>6} {'方向':>6} {'单数':>6} {'累计R':>8} {'均R':>8} {'中位R':>8} {'胜率':>8} {'累计收益':>10} {'PF':>8}")
print("-" * 100)

tp_data = {}
for tp in [0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18]:
    rdf = run_backtest(tp)
    f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
    tp_data[tp] = f
    for d, label in [("long", "做多"), ("short", "做空")]:
        sub = f[f["direction"] == d]
        sr = sub["r_value"]; w = sr[sr > 0]; l = sr[sr <= 0]
        pf = w.sum() / abs(l.sum()) if len(l) else float("inf")
        pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
        print(f"{tp:>6.0%} {label:>6} {len(sub):>6} {sr.sum():>8.1f} {sr.mean():>8.2f} {sr.median():>8.2f} "
              f"{(sr>0).mean():>8.0%} {sub['final_return'].sum():>10.1%} {pf_str:>8}")
    sr = f["r_value"]; w = sr[sr > 0]; l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    print(f"{tp:>6.0%} {'合并':>6} {len(f):>6} {sr.sum():>8.1f} {sr.mean():>8.2f} {sr.median():>8.2f} "
          f"{(sr>0).mean():>8.0%} {f['final_return'].sum():>10.1%} {pf_str:>8}")
    print()

# ========== 需求2：不同MFE阈值波段时间分布对比（含累计R） ==========
print("=" * 100)
print("需求2：≥8%/10%/12%/14%/16%/18% 波段时间分布对比（12%止盈回测，含累计R）")
print("=" * 100)

base_df = tp_data[0.12].sort_values("signal_idx").reset_index(drop=True)

print(f"\n{'MFE阈值':>8} {'波段数':>8} {'累计R':>8} {'均R':>8} {'距离中位':>8} {'距离均值':>8} {'min':>6} {'max':>8} {'<20K占比':>10} {'>100K占比':>10}")
print("-" * 100)

fig, axes = plt.subplots(2, 3, figsize=(18, 10))
fig.suptitle("不同MFE阈值下波段时间距离分布对比", fontsize=14, fontweight="bold")

mfe_thresholds = [0.08, 0.10, 0.12, 0.14, 0.16, 0.18]
for idx, mfe_t in enumerate(mfe_thresholds):
    sub = base_df[base_df["max_excursion"].abs() >= mfe_t].reset_index(drop=True)
    sr = sub["r_value"]
    if len(sub) > 1:
        gaps = sub["signal_idx"].diff().dropna().astype(int)
        med_g = gaps.median(); mean_g = gaps.mean(); min_g = gaps.min(); max_g = gaps.max()
        lt20 = (gaps < 20).mean(); gt100 = (gaps > 100).mean()
    else:
        med_g = mean_g = min_g = max_g = 0; lt20 = gt100 = 0; gaps = pd.Series(dtype=int)

    print(f"≥{mfe_t:>5.0%} {len(sub):>8} {sr.sum():>8.1f} {sr.mean():>8.2f} "
          f"{med_g:>8.0f} {mean_g:>8.0f} {min_g:>6d} {max_g:>8d} {lt20:>10.1%} {gt100:>10.1%}")

    ax = axes[idx // 3, idx % 3]
    if len(gaps) > 0:
        bins_g = [0, 10, 20, 50, 100, 200, 500, 1000]
        counts = []
        for k in range(len(bins_g) - 1):
            counts.append(((gaps >= bins_g[k]) & (gaps < bins_g[k + 1])).sum())
        ax.bar(range(len(counts)), counts, color=plt.cm.viridis(idx / 5), edgecolor="black", alpha=0.8)
        ax.set_xticks(range(len(counts)))
        ax.set_xticklabels([f"{bins_g[k]}-{bins_g[k+1]}" for k in range(len(counts))], rotation=45, fontsize=8)
    ax.set_title(f"≥{mfe_t:.0%}（{len(sub)}单，累计{sr.sum():.0f}R）", fontsize=11)
    ax.set_ylabel("频次"); ax.grid(alpha=0.3, axis="y")

plt.tight_layout()
plt.savefig(out_dir / "mfe_time_dist.png", dpi=150, bbox_inches="tight")
print(f"\n图表已保存: strategies_backtest/results/mfe_time_dist.png")

# ========== 需求2补充：累计R曲线对比 ==========
fig2, ax2 = plt.subplots(figsize=(14, 6))
for mfe_t in mfe_thresholds:
    sub = base_df[base_df["max_excursion"].abs() >= mfe_t].reset_index(drop=True)
    if sub.empty: continue
    cum = sub["r_value"].cumsum()
    dates = pd.to_datetime(df["open_time"].iloc[sub["signal_idx"]].values, utc=True)
    ax2.plot(dates, cum, linewidth=1.5, label=f"≥{mfe_t:.0%}（{len(sub)}单，累计{sub['r_value'].sum():.0f}R）")
ax2.axhline(0, color="gray", linestyle="--", alpha=0.5)
ax2.set_title("不同MFE阈值下累计R曲线对比", fontsize=13, fontweight="bold")
ax2.set_ylabel("累计R"); ax2.legend(); ax2.grid(alpha=0.3)
plt.tight_layout()
plt.savefig(out_dir / "mfe_cum_r.png", dpi=150, bbox_inches="tight")
print(f"图表已保存: strategies_backtest/results/mfe_cum_r.png")
print("=" * 100)
