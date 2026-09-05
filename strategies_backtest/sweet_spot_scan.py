"""固定止盈甜蜜点扫描：做多12-30% vs 做空8-18%，找累计R最高的平衡点。"""
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
            "exit_idx": exit_idx}


def run_backtest(tp):
    results = []; last_exit_idx = None
    for s in signals:
        if last_exit_idx is not None and s.idx <= last_exit_idx: continue
        r = simulate(s, tp=tp)
        if r: results.append(r); last_exit_idx = r["exit_idx"]
    return pd.DataFrame(results)


# 纯KAMA基准
rdf0 = run_backtest(0.0)
f0 = rdf0[rdf0["max_excursion"].abs() >= 0.10].reset_index(drop=True)
base_long = f0[f0["direction"] == "long"]["r_value"].sum()
base_short = f0[f0["direction"] == "short"]["r_value"].sum()
base_all = f0["r_value"].sum()

print("=" * 100)
print("固定止盈甜蜜点扫描（≥10%波段，累计R vs 纯KAMA基准）")
print("=" * 100)
print(f"纯KAMA基准: 做多={base_long:.1f}R  做空={base_short:.1f}R  合并={base_all:.1f}R\n")

# 做多扫描
print(f"—— 做多止盈扫描 ——")
print(f"{'止盈':>8} {'单数':>6} {'累计R':>8} {'均R':>8} {'胜率':>8} {'vs纯KAMA':>10} {'累计收益':>10}")
print("-" * 70)
long_tps = [0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.25, 0.30, 0.35, 0.40]
best_long_tp = 0; best_long_r = 0
for tp in long_tps:
    rdf = run_backtest(tp)
    f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
    sub = f[f["direction"] == "long"]
    sr = sub["r_value"]
    vs = sr.sum() - base_long
    marker = " ◄最优" if sr.sum() > best_long_r else ""
    if sr.sum() > best_long_r: best_long_r = sr.sum(); best_long_tp = tp
    print(f"{tp:>8.0%} {len(sub):>6} {sr.sum():>8.1f} {sr.mean():>8.2f} {(sr>0).mean():>8.0%} {vs:>+10.1f} {sub['final_return'].sum():>10.1%}{marker}")

# 做空扫描
print(f"\n—— 做空止盈扫描 ——")
print(f"{'止盈':>8} {'单数':>6} {'累计R':>8} {'均R':>8} {'胜率':>8} {'vs纯KAMA':>10} {'累计收益':>10}")
print("-" * 70)
short_tps = [0.08, 0.12, 0.14, 0.16, 0.18, 0.20, 0.25]
best_short_tp = 0; best_short_r = 0
for tp in short_tps:
    rdf = run_backtest(tp)
    f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
    sub = f[f["direction"] == "short"]
    sr = sub["r_value"]
    vs = sr.sum() - base_short
    marker = " ◄最优" if sr.sum() > best_short_r else ""
    if sr.sum() > best_short_r: best_short_r = sr.sum(); best_short_tp = tp
    print(f"{tp:>8.0%} {len(sub):>6} {sr.sum():>8.1f} {sr.mean():>8.2f} {(sr>0).mean():>8.0%} {vs:>+10.1f} {sub['final_return'].sum():>10.1%}{marker}")

# 合并扫描
print(f"\n—— 合并止盈扫描（统一值）——")
print(f"{'止盈':>8} {'单数':>6} {'累计R':>8} {'均R':>8} {'胜率':>8} {'vs纯KAMA':>10} {'累计收益':>10}")
print("-" * 70)
all_tps = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.25]
best_all_tp = 0; best_all_r = 0
for tp in all_tps:
    rdf = run_backtest(tp)
    f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
    sr = f["r_value"]
    vs = sr.sum() - base_all
    marker = " ◄最优" if sr.sum() > best_all_r else ""
    if sr.sum() > best_all_r: best_all_r = sr.sum(); best_all_tp = tp
    print(f"{tp:>8.0%} {len(f):>6} {sr.sum():>8.1f} {sr.mean():>8.2f} {(sr>0).mean():>8.0%} {vs:>+10.1f} {f['final_return'].sum():>10.1%}{marker}")

# 最优组合
print(f"\n{'='*100}")
print(f"结论:")
print(f"  做多最优止盈: {best_long_tp:.0%} (累计R={best_long_r:.1f}, 纯KAMA={base_long:.1f}, 差={best_long_r-base_long:+.1f})")
print(f"  �/做空最优止盈: {best_short_tp:.0%} (累计R={best_short_r:.1f}, 纯KAMA={base_short:.1f}, 差={best_short_r-base_short:+.1f})")
print(f"  合并最优统一值: {best_all_tp:.0%} (累计R={best_all_r:.1f})")
print(f"  多空分开设最优: 做多{best_long_tp:.0%}+做空{best_short_tp:.0%} = {best_long_r+best_short_r:.1f}R")
print(f"  vs 12%统一: {619.2:.1f}R")
print(f"{'='*100}")
