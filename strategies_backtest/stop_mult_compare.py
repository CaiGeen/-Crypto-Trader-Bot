"""止损倍数对比：0.3ATR vs 1ATR。

其他配置不变：回调基准+0.5ATR确认+1.5ATR加仓+22%/12%止盈+KAMA跟踪保本。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
closes = df["close"].to_numpy(); opens = df["open"].to_numpy(); n = len(df)
years = df["open_time"].dt.year.to_numpy()

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)
TP_LONG = 0.22; TP_SHORT = 0.12

def find_callback_extreme(i, direction, window=6):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]: return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] >= lows[j]: return lows[j], j
    return None, None

def simulate(signal, stop_mult=0.3):
    i = signal.idx
    if i + 1 >= n: return None
    bt_6k = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    entry_base = cb_extreme if cb_extreme is not None else bt_6k
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-entry_base)/av if is_long else (entry_base-ep)/av
        return {"dir": d, "exit_idx": i, "filled": filled, "r": r1, "hold": 0,
                "exit_reason": "弱突破平仓", "year": int(years[i])}
    if is_long: init_stop = entry_base - stop_mult*av; tp_price = entry_base*(1+TP_LONG)
    else: init_stop = entry_base + stop_mult*av; tp_price = entry_base*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(ks, entry_base) if is_long else min(ks, entry_base)
        if not addon_done:
            cg_abs = (closes[j]-entry_base) if is_long else (entry_base-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j])
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            exit_idx, exit_price, exit_reason = j, ep, "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            exit_idx, exit_price, exit_reason = j, ep, "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        if is_long and highs[j] >= tp_price:
            exit_idx, exit_price, exit_reason = j, float(tp_price), "止盈"; break
        if not is_long and lows[j] <= tp_price:
            exit_idx, exit_price, exit_reason = j, float(tp_price), "止盈"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "未平仓"
    r1 = (exit_price-entry_base)/av if is_long else (entry_base-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "filled": filled, "r": r1+r2,
            "hold": exit_idx-i, "exit_reason": exit_reason, "year": int(years[i]), "addon": addon_done}

def run(stop_mult):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, stop_mult)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 110)
print("止损倍数对比：0.3ATR vs 1ATR（其他配置不变：回调基准+0.5ATR确认+1.5ATR加仓+22%/12%止盈）")
print("=" * 110)

rdf_03 = run(0.3)
rdf_10 = run(1.0)
modes = [("0.3ATR止损", rdf_03), ("1ATR止损", rdf_10)]

print(f"\n{'方案':>12} {'总单':>6} {'成交':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'前3K止损':>8} {'前3K亏R':>8} {'KAMA跟踪':>8} {'止盈':>6} {'弱突破':>6}")
print("-" * 90)
for label, rdf in modes:
    filled = rdf[rdf["filled"]]
    sr = filled["r"]
    pre3k = filled[filled["exit_reason"] == "前3K止损"]
    kama_t = filled[filled["exit_reason"] == "KAMA跟踪止损"]
    tp = filled[filled["exit_reason"] == "止盈"]
    weak = filled[filled["exit_reason"] == "弱突破平仓"]
    print(f"{label:>12} {len(rdf):>6} {len(filled):>6} {sr.sum():>8.1f} {sr.mean():>6.2f} "
          f"{(sr>0).mean():>5.0%} {len(pre3k):>8} {pre3k['r'].sum():>8.1f} {len(kama_t):>8} {len(tp):>6} {len(weak):>6}")

print(f"\n{'方案':>12}  {'做多单':>6} {'做多累计R':>10} {'做多均R':>8}  {'做空单':>6} {'做空累计R':>10} {'做空均R':>8}")
print("-" * 70)
for label, rdf in modes:
    filled = rdf[rdf["filled"]]
    for d in ["long", "short"]:
        sub = filled[filled["dir"] == d]
        if d == "long": ln, lr, lm = len(sub), sub["r"].sum(), sub["r"].mean() if not sub.empty else 0
        else: sn, sr2, sm = len(sub), sub["r"].sum(), sub["r"].mean() if not sub.empty else 0
    print(f"{label:>12}  {ln:>6} {lr:>10.1f} {lm:>8.2f}  {sn:>6} {sr2:>10.1f} {sm:>8.2f}")

print(f"\n{'=' * 110}")
print("年度分解")
print(f"{'=' * 110}")
print(f"\n{'年份':>6}  {'0.3ATR累计R':>12} {'0.3ATR单数':>10}  {'1ATR累计R':>10} {'1ATR单数':>8}  {'差R':>8}")
print("-" * 65)
all_years = sorted(set(rdf_03["year"]).union(set(rdf_10["year"])))
for yr in all_years:
    c = rdf_03[(rdf_03["filled"]) & (rdf_03["year"]==yr)]
    b = rdf_10[(rdf_10["filled"]) & (rdf_10["year"]==yr)]
    print(f"{yr:>6}  {c['r'].sum():>12.1f} {len(c):>10}  {b['r'].sum():>10.1f} {len(b):>8}  {b['r'].sum()-c['r'].sum():>+8.1f}")
tc = rdf_03[rdf_03["filled"]]["r"].sum(); tb = rdf_10[rdf_10["filled"]]["r"].sum()
print(f"{'合计':>6}  {tc:>12.1f} {len(rdf_03[rdf_03['filled']]):>10}  {tb:>10.1f} {len(rdf_10[rdf_10['filled']]):>8}  {tb-tc:>+8.1f}")

print(f"\n{'=' * 110}")
print("R值分布")
print(f"{'=' * 110}")
print(f"\n{'R区间':>12}  {'0.3ATR单数':>10} {'0.3累计R':>10}  {'1ATR单数':>10} {'1ATR累计R':>10}")
print("-" * 58)
for lo, hi in [(-2,-1),(-1,0),(0,1),(1,2),(2,5),(5,100)]:
    c = rdf_03[(rdf_03["filled"]) & (rdf_03["r"]>=lo) & (rdf_03["r"]<hi)]
    b = rdf_10[(rdf_10["filled"]) & (rdf_10["r"]>=lo) & (rdf_10["r"]<hi)]
    print(f"[{lo}, {hi}):  {len(c):>10} {c['r'].sum():>10.1f}  {len(b):>10} {b['r'].sum():>10.1f}")

print(f"\n{'=' * 110}")
