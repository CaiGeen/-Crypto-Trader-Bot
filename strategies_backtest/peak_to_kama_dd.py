"""≥12%波段从MFE峰值回调到KAMA出场的回撤幅度统计（纯KAMA，无固定止盈）。"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
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


def simulate(signal):
    """纯KAMA跟踪，无固定止盈。记录MFE峰值价和KAMA出场价。"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if d == "long": init_stop = bt - av
    else: init_stop = bt + av
    max_exc = -np.inf; peak_idx = i
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if d == "long" else (bt-lows[j])/bt
        if exc > max_exc:
            max_exc = exc; peak_idx = j
        bh = j - i
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if d == "long": stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"
        if d == "long" and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if d == "short" and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    ret = (exit_price-bt)/bt if d == "long" else (bt-exit_price)/bt
    r = (exit_price-bt)/av if d == "long" else (bt-exit_price)/av
    # MFE峰值价
    peak_price = highs[peak_idx] if d == "long" else lows[peak_idx]
    # 从峰值到KAMA出场的回撤
    if d == "long":
        dd = (peak_price - exit_price) / peak_price
    else:
        dd = (exit_price - peak_price) / peak_price if peak_price > 0 else 0.0
    # 回吐幅度（MFE - 实际收益）
    giveback = max_exc - ret
    # 回吐比例
    giveback_ratio = giveback / max_exc if max_exc > 0 else 0.0
    return {"direction": d, "r_value": r, "final_return": ret,
            "max_excursion": float(max_exc) if max_exc != -np.inf else 0.0,
            "peak_price": peak_price, "exit_price": exit_price,
            "dd_peak_to_kama": dd, "giveback": giveback, "giveback_ratio": giveback_ratio,
            "exit_reason": exit_reason, "exit_idx": exit_idx}


results = []; last_exit_idx = None
for s in signals:
    if last_exit_idx is not None and s.idx <= last_exit_idx: continue
    r = simulate(s)
    if r: results.append(r); last_exit_idx = r["exit_idx"]

rdf = pd.DataFrame(results)
f = rdf[rdf["max_excursion"].abs() >= 0.12].reset_index(drop=True)
print(f"纯KAMA跟踪，≥12%波段: {len(f)} 单\n")

dd = f["dd_peak_to_kama"]
gb = f["giveback"]
gr = f["giveback_ratio"]

print("=" * 100)
print("≥12%波段：从MFE峰值回调到KAMA出场的回撤幅度统计")
print("=" * 100)

print(f"\n样本数: {len(f)}")
print(f"\n—— 峰值到KAMA出场回撤（价格回撤%）——")
print(f"  均值={dd.mean():.2%}  中位={dd.median():.2%}  std={dd.std():.2%}")
print(f"  P10={dd.quantile(0.1):.2%}  P25={dd.quantile(0.25):.2%}  P75={dd.quantile(0.75):.2%}  P90={dd.quantile(0.9):.2%}")
print(f"  min={dd.min():.2%}  max={dd.max():.2%}")

print(f"\n—— 回吐幅度（MFE - 实际收益）——")
print(f"  均值={gb.mean():.2%}  中位={gb.median():.2%}  std={gb.std():.2%}")
print(f"  P10={gb.quantile(0.1):.2%}  P25={gb.quantile(0.25):.2%}  P75={gb.quantile(0.75):.2%}  P90={gb.quantile(0.9):.2%}")

print(f"\n—— 回吐比例（回吐/MFE）——")
print(f"  均值={gr.mean():.1%}  中位={gr.median():.1%}  std={gr.std():.1%}")
print(f"  P10={gr.quantile(0.1):.1%}  P25={gr.quantile(0.25):.1%}  P75={gr.quantile(0.75):.1%}  P90={gr.quantile(0.9):.1%}")

# 多空分开
print(f"\n—— 多空分开 ——")
print(f"{'方向':>6} {'单数':>6} {'回撤均值':>8} {'回撤中位':>8} {'回吐均值':>8} {'回吐中位':>8} {'回吐比例中位':>10} {'MFE中位':>8} {'收益中位':>8}")
print("-" * 90)
for d, lbl in [("long", "做多"), ("short", "做空")]:
    sub = f[f["direction"] == d]
    sd = sub["dd_peak_to_kama"]; sg = sub["giveback"]; sr = sub["giveback_ratio"]
    print(f"{lbl:>6} {len(sub):>6} {sd.mean():>8.2%} {sd.median():>8.2%} {sg.mean():>8.2%} {sg.median():>8.2%} "
          f"{sr.median():>10.1%} {sub['max_excursion'].median():>8.2%} {sub['final_return'].median():>8.2%}")

# 箱体分布
print(f"\n—— 峰值到KAMA回撤 箱体分布 ——")
print(f"{'回撤区间':>14} {'单数':>6} {'占比':>8} {'累计':>8}")
print("-" * 50)
bins_d = np.arange(0, 0.50, 0.02)
cum = 0
for k in range(len(bins_d) - 1):
    cnt = ((dd >= bins_d[k]) & (dd < bins_d[k + 1])).sum()
    cum += cnt
    if cnt == 0: continue
    print(f"[{bins_d[k]:>5.0%}, {bins_d[k+1]:>5.0%}) {cnt:>6} {cnt/len(dd):>8.1%} {cum/len(dd):>8.1%}")

# 回吐比例箱体
print(f"\n—— 回吐比例 箱体分布 ——")
print(f"{'回吐/MFE':>14} {'单数':>6} {'占比':>8} {'累计':>8}")
print("-" * 50)
bins_r = np.arange(0, 1.05, 0.05)
cum = 0
for k in range(len(bins_r) - 1):
    cnt = ((gr >= bins_r[k]) & (gr < bins_r[k + 1])).sum()
    cum += cnt
    if cnt == 0: continue
    print(f"[{bins_r[k]:>5.0%}, {bins_r[k+1]:>5.0%}) {cnt:>6} {cnt/len(gr):>8.1%} {cum/len(gr):>8.1%}")

print("=" * 100)
