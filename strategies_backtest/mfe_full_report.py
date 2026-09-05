"""全波段MFE分布完整报告：建立做单心理预期。"""
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
            "exit_reason": exit_reason, "exit_idx": exit_idx}


results = []; last_exit_idx = None
for s in signals:
    if last_exit_idx is not None and s.idx <= last_exit_idx: continue
    r = simulate(s)
    if r: results.append(r); last_exit_idx = r["exit_idx"]

rdf = pd.DataFrame(results)
print(f"合并后 {len(rdf)} 波段（全部，无MFE过滤）\n")

mfe = rdf["max_excursion"]
ret = rdf["final_return"]
out_dir = cfg.base_dir / "results"

# ========== 1. MFE完整百分位 ==========
print("=" * 100)
print("全波段MFE（盘中最大涨跌幅）分布完整报告")
print("=" * 100)

print(f"\n样本数: {len(rdf)}")
print(f"\n—— MFE 百分位 ——")
pcts = [1, 5, 10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 95, 99]
for p in pcts:
    print(f"  P{p:>2}: {mfe.quantile(p/100):>7.2%}")

print(f"\n—— 实际涨跌幅 百分位 ——")
for p in pcts:
    print(f"  P{p:>2}: {ret.quantile(p/100):>7.2%}")

# ========== 2. MFE箱体分布 ==========
print(f"\n—— MFE 箱体分布（2%一档）——")
print(f"{'区间':>14} {'单数':>6} {'占比':>8} {'累计':>8} │ {'做多':>6} {'做空':>6}")
print("-" * 70)
bins = np.arange(-0.40, 0.65, 0.02)
cum = 0
for k in range(len(bins) - 1):
    mask = (mfe >= bins[k]) & (mfe < bins[k + 1])
    cnt = mask.sum()
    cum += cnt
    if cnt == 0: continue
    long_cnt = (mask & (rdf["direction"] == "long")).sum()
    short_cnt = (mask & (rdf["direction"] == "short")).sum()
    print(f"[{bins[k]:>6.0%}, {bins[k+1]:>6.0%}) {cnt:>6} {cnt/len(rdf):>8.1%} {cum/len(rdf):>8.1%} │ {long_cnt:>6} {short_cnt:>6}")

# ========== 3. 关键概率 ==========
print(f"\n—— 做单心理预期（概率）——")
print(f"{'目标涨幅':>10} {'达到概率':>10} {'做不到概率':>10} │ {'做多达到':>10} {'做空达到':>10}")
print("-" * 70)
for x in [0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]:
    p_all = (mfe >= x).mean()
    p_long = (mfe[rdf["direction"] == "long"] >= x).mean()
    p_short = (mfe[rdf["direction"] == "short"] >= x).mean()
    print(f"{'≥'+f'{x:.0%}':>10} {p_all:>10.1%} {1-p_all:>10.1%} │ {p_long:>10.1%} {p_short:>10.1%}")

# ========== 4. 实际涨跌幅 vs MFE 对比 ==========
print(f"\n—— MFE vs 实际涨跌幅 对比 ——")
print(f"{'指标':>10} {'MFE':>10} {'实际':>10} {'回吐':>10}")
print("-" * 50)
print(f"{'均值':>10} {mfe.mean():>10.2%} {ret.mean():>10.2%} {mfe.mean()-ret.mean():>10.2%}")
print(f"{'中位':>10} {mfe.median():>10.2%} {ret.median():>10.2%} {mfe.median()-ret.median():>10.2%}")
valid = mfe > 0
capture = (ret[valid] / mfe[valid])
print(f"{'捕获率':>10} {'—':>10} {'—':>10} {capture.median():>10.0%}")

# ========== 5. 多空分开 ==========
print(f"\n—— 多空分开 MFE 统计 ——")
for d, lbl in [("long", "做多"), ("short", "做空")]:
    sub = rdf[rdf["direction"] == d]
    sm = sub["max_excursion"]
    print(f"\n  {lbl} ({len(sub)}单):")
    print(f"    MFE: 均$均值={sm.mean():.2%}  中位={sm.median():.2%}  P10={sm.quantile(0.1):.2%}  P90={sm.quantile(0.9):.2%}")
    for x in [0.02, 0.05, 0.10, 0.12, 0.20]:
        print(f"    ≥{x:.0%}: {(sm>=x).mean():.1%}")

# ========== 6. 画图 ==========
fig, axes = plt.subplots(2, 2, figsize=(16, 12))
fig.suptitle("全波段MFE分布完整报告（1263波段）", fontsize=14, fontweight="bold")

ax = axes[0, 0]
ax.hist(mfe * 100, bins=40, edgecolor="black", color="steelblue", alpha=0.7)
ax.axvline(mfe.median() * 100, color="green", linestyle="--", linewidth=2, label=f"中位={mfe.median():.1%}")
ax.axvline(mfe.mean() * 100, color="red", linestyle="--", linewidth=2, label=f"均值={mfe.mean():.1%}")
ax.set_xlabel("MFE(%)"); ax.set_ylabel("频次"); ax.set_title("MFE直方图（全部波段）"); ax.legend()

ax = axes[0, 1]
for d, lbl, color in [("long", "做多", "orange"), ("short", "做空", "purple")]:
    sub = rdf[rdf["direction"] == d]["max_excursion"] * 100
    ax.hist(sub, bins=30, edgecolor="black", color=color, alpha=0.5, label=f"{lbl}(n={len(sub)})")
ax.set_xlabel("MFE(%)"); ax.set_ylabel("频次"); ax.set_title("多空MFE对比"); ax.legend()

ax = axes[1, 0]
targets = [0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30]
probs = [(mfe >= x).mean() for x in targets]
probs_l = [(mfe[rdf["direction"]=="long"] >= x).mean() for x in targets]
probs_s = [(mfe[rdf["direction"]=="short"] >= x).mean() for x in targets]
ax.plot([f"{x:.0%}" for x in targets], probs, "o-", color="steelblue", linewidth=2, label="合并")
ax.plot([f"{x:.0%}" for x in targets], probs_l, "s--", color="orange", linewidth=1.5, label="做多")
ax.plot([f"{x:.0%}" for x in targets], probs_s, "^--", color="purple", linewidth=1.5, label="做空")
ax.set_xlabel("目标涨幅"); ax.set_ylabel("达到概率"); ax.set_title("达到目标涨幅的概率"); ax.legend(); ax.grid(alpha=0.3)

ax = axes[1, 1]
ax.hist(ret * 100, bins=40, edgecolor="black", color="green", alpha=0.6, label="实际涨跌幅")
ax.hist(mfe * 100, bins=40, edgecolor="black", color="steelblue", alpha=0.4, label="MFE")
ax.set_xlabel("涨跌幅(%)"); ax.set_ylabel("频次"); ax.set_title("MFE vs 实际涨跌幅"); ax.legend()

plt.tight_layout()
plt.savefig(out_dir / "mfe_full_distribution.png", dpi=150, bbox_inches="tight")
print(f"\n图表已保存: strategies_backtest/results/mfe_full_distribution.png")
print("=" * 100)
