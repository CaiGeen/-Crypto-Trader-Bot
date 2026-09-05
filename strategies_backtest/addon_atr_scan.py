"""细粒度扫描ATR动态阈值的最优N值。"""
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
closes = df["close"].to_numpy(); n = len(df)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

def simulate(signal, tp_l=0.18, tp_s=0.12, n_atr=1.5, include_bk=False):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    actual_thresh = n_atr * (av / bt)
    if is_long:
        init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else:
        init_stop = bt + av; tp_price = bt * (1 - tp_s)
    base_active = True
    base_exit_idx = base_exit_price = None
    addon_active = False
    addon_entry = addon_exit_idx = addon_exit_price = None
    if include_bk:
        close_exc = (closes[i]-bt)/bt if is_long else (bt-closes[i])/bt
        if close_exc >= actual_thresh:
            addon_active = True; addon_entry = float(closes[i]); addon_stop = bt
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: stop_line = init_stop
        else: stop_line = max(ks, bt) if is_long else min(ks, bt)
        if not addon_active and base_active:
            close_exc = (closes[j]-bt)/bt if is_long else (bt-closes[j])/bt
            if close_exc >= actual_thresh:
                addon_active = True; addon_entry = float(closes[j]); addon_stop = bt
        if base_active:
            if is_long and lows[j] < stop_line:
                base_active = False; base_exit_idx, base_exit_price = j, float(stop_line)
            elif not is_long and highs[j] > stop_line:
                base_active = False; base_exit_idx, base_exit_price = j, float(stop_line)
            elif is_long and highs[j] >= tp_price:
                base_active = False; base_exit_idx, base_exit_price = j, float(tp_price)
            elif not is_long and lows[j] <= tp_price:
                base_active = False; base_exit_idx, base_exit_price = j, float(tp_price)
        if addon_active:
            a_stop = addon_stop if bh <= 3 else (max(ks, addon_stop) if is_long else min(ks, addon_stop))
            if is_long and lows[j] < a_stop:
                addon_active = False; addon_exit_idx, addon_exit_price = j, float(a_stop)
            elif not is_long and highs[j] > a_stop:
                addon_active = False; addon_exit_idx, addon_exit_price = j, float(a_stop)
            elif is_long and highs[j] >= tp_price:
                addon_active = False; addon_exit_idx, addon_exit_price = j, float(tp_price)
            elif not is_long and lows[j] <= tp_price:
                addon_active = False; addon_exit_idx, addon_exit_price = j, float(tp_price)
        if not base_active and not addon_active: break
    if base_exit_idx is None: base_exit_idx = n-1; base_exit_price = float(closes[-1])
    if addon_active and addon_exit_idx is None: addon_exit_idx = n-1; addon_exit_price = float(closes[-1])
    base_r = (base_exit_price-bt)/av if is_long else (bt-base_exit_price)/av
    addon_r = 0.0
    if addon_entry is not None:
        addon_r = (addon_exit_price-addon_entry)/av if is_long else (addon_entry-addon_exit_price)/av
    return {"total_r": base_r + addon_r, "has_addon": addon_entry is not None,
            "addon_r": addon_r, "exit_idx": max(base_exit_idx, addon_exit_idx or 0)}

def run(n_atr, include_bk=False):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, n_atr=n_atr, include_bk=include_bk)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 110)
print("ATR动态阈值细粒度扫描")
print("=" * 110)
base_r = 996.0
print(f"\n基准: 累计R={base_r}")

print(f"\n{'N×ATR':>6} │ {'不含BK累计R':>12} {'vs基准':>8} {'加仓率':>6} │ {'含BK累计R':>12} {'vs基准':>8} {'加仓率':>6} │ {'含-不含':>8}")
print("─" * 85)
best_no = (-999, 0); best_yes = (-999, 0)
for n_atr in [x * 0.25 for x in range(4, 17)]:
    r_no = run(n_atr, include_bk=False)
    r_yes = run(n_atr, include_bk=True)
    vs_no = r_no["total_r"].sum() - base_r
    vs_yes = r_yes["total_r"].sum() - base_r
    diff = vs_yes - vs_no
    ar_no = r_no["has_addon"].mean()
    ar_yes = r_yes["has_addon"].mean()
    marker = ""
    if vs_no > best_no[0]: best_no = (vs_no, n_atr); marker += " ←不含最优"
    if vs_yes > best_yes[0]: best_yes = (vs_yes, n_atr); marker += " ←含BK最优"
    print(f"{n_atr:>5.2f}x │ {r_no['total_r'].sum():>12.1f} {vs_no:>+8.1f} {ar_no:>5.0%} │ {r_yes['total_r'].sum():>12.1f} {vs_yes:>+8.1f} {ar_yes:>5.0%} │ {diff:>+8.1f}{marker}")

print(f"\n  不含BK最优: N={best_no[1]:.2f}x, vs基准={best_no[0]:.1f}R")
print(f"  含BK最优:   N={best_yes[1]:.2f}x, vs基准={best_yes[0]:.1f}R")

print(f"\n{'='*110}")
