"""质疑6K：扫描突破窗口3-12K，对比各窗口回测表现。"""
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
            "exit_idx": exit_idx}


def run_backtest(window, tp=0.12):
    sigs = detect_breakout(df, window)
    results = []; last_exit_idx = None
    for s in sigs:
        if last_exit_idx is not None and s.idx <= last_exit_idx: continue
        r = simulate(s, tp=tp)
        if r: results.append(r); last_exit_idx = r["exit_idx"]
    return pd.DataFrame(results), len(sigs)


print("=" * 110)
print("突破窗口扫描：3K ~ 12K（12%止盈，ATR止损+3K保本+KAMA跟踪）")
print("=" * 110)
print(f"{'窗口':>6} {'原始信号':>8} {'合并波段':>8} {'≥10%波段':>8} {'累计R':>8} {'均R':>8} {'中位R':>8} "
      f"{'胜率':>8} {'累计收益':>10} {'做多R':>8} {'做空R':>8} {'做多n':>6} {'做空n':>6}")
print("-" * 110)

best_cum = -1; best_window = 6

for w in range(3, 13):
    rdf, n_raw = run_backtest(w)
    f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
    sr = f["r_value"]
    lr = f[f["direction"] == "long"]["r_value"]
    sr_s = f[f["direction"] == "short"]["r_value"]
    cum = sr.sum()
    marker = " ◄" if w == 6 else ""
    if cum > best_cum: best_cum = cum; best_window = w
    print(f"{w:>5}K {n_raw:>8} {len(rdf):>8} {len(f):>8} {cum:>8.1f} {sr.mean():>8.2f} {sr.median():>8.2f} "
          f"{(sr>0).mean():>8.0%} {f['final_return'].sum():>10.1%} {lr.sum():>8.1f} {sr_s.sum():>8.1f} "
          f"{len(lr):>6} {len(sr_s):>6}{marker}")

print("-" * 110)
print(f"累计R最高: {best_window}K ({best_cum:.1f}R)")
print("=" * 110)

# 也对比纯KAMA
print(f"\n{'窗口':>6} {'≥10%波段':>8} {'累计R':>8} {'均R':>8} {'胜率':>8} {'累计收益':>10}  （纯KAMA，无固定止盈）")
print("-" * 70)
for w in range(3, 13):
    rdf, _ = run_backtest(w, tp=0.0)
    f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
    sr = f["r_value"]
    marker = " ◄" if w == 6 else ""
    print(f"{w:>5}K {len(f):>8} {sr.sum():>8.1f} {sr.mean():>8.2f} {(sr>0).mean():>8.0%} {f['final_return'].sum():>10.1%}{marker}")
print("=" * 110)
