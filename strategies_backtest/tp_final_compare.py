"""12% vs 18% vs 纯KAMA 止盈对比，多空分开，R值详细统计。"""
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


def simulate(signal, tp=0.12):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if d == "long": init_stop = bt - av; tp_price = bt*(1+tp) if tp > 0 else float("inf")
    else: init_stop = bt + av; tp_price = bt*(1-tp) if tp > 0 else 0.0
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
        if tp > 0:
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


def calc_stats(sub):
    sr = sub["r_value"]
    w = sr[sr > 0]; l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    # 最大回撤（累计R曲线）
    cum = sr.cumsum(); peak = cum.cummax(); dd = (cum - peak).min()
    return {
        "n": len(sub), "cumR": sr.sum(), "meanR": sr.mean(), "medR": sr.median(),
        "win": (sr > 0).mean(), "cumRet": sub["final_return"].sum(),
        "pf": pf, "maxDD": dd, "p25": sr.quantile(0.25), "p75": sr.quantile(0.75),
        "min": sr.min(), "max": sr.max(),
    }


configs = [(0.0, "纯KAMA"), (0.12, "12%止盈"), (0.18, "18%止盈")]
data = {}
for tp, label in configs:
    rdf = run_backtest(tp)
    f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
    data[label] = f

print("=" * 115)
print("12% vs 18% vs 纯KAMA 止盈对比（R值详细统计，≥10%波段）")
print("=" * 115)

for label in ["纯KAMA", "12%止盈", "18%止盈"]:
    f = data[label]
    print(f"\n{'━'*115}")
    print(f"  {label}")
    print(f"{'━'*115}")
    print(f"{'方向':>6} {'单数':>6} {'累计R':>8} {'均R':>8} {'中位R':>8} {'P25':>6} {'P75':>6} "
          f"{'min':>6} {'max':>6} {'胜率':>6} {'PF':>8} {'最大回撤':>8} {'累计收益':>10}")
    print("-" * 115)
    for d, lbl in [("long", "做多"), ("short", "做空"), ("all", "合并")]:
        sub = f if d == "all" else f[f["direction"] == d]
        if sub.empty: continue
        s = calc_stats(sub)
        pf_str = f"{s['pf']:.1f}" if s['pf'] < 1000 else "inf"
        print(f"{lbl:>6} {s['n']:>6} {s['cumR']:>8.1f} {s['meanR']:>8.2f} {s['medR']:>8.2f} "
              f"{s['p25']:>6.1f} {s['p75']:>6.1f} {s['min']:>6.1f} {s['max']:>6.1f} "
              f"{s['win']:>6.0%} {pf_str:>8} {s['maxDD']:>8.1f}R {s['cumRet']:>10.1%}")

# 汇总对比表
print(f"\n{'━'*115}")
print(f"  汇总对比")
print(f"{'━'*115}")
print(f"{'':>10} {'做多累计R':>10} {'做空累计R':>10} {'合并累计R':>10} │ {'做多均R':>8} {'做空均R':>8} {'合并均R':>8} │ {'做多胜率':>8} {'做空胜率':>8}")
print("-" * 115)
for label in ["纯KAMA", "12%止盈", "18%止盈"]:
    f = data[label]
    sl = calc_stats(f[f["direction"] == "long"])
    ss = calc_stats(f[f["direction"] == "short"])
    sa = calc_stats(f)
    print(f"{label:>10} {sl['cumR']:>10.1f} {ss['cumR']:>10.1f} {sa['cumR']:>10.1f} │ "
          f"{sl['meanR']:>8.2f} {ss['meanR']:>8.2f} {sa['meanR']:>8.2f} │ "
          f"{sl['win']:>8.0%} {ss['win']:>8.0%}")

print("=" * 115)
