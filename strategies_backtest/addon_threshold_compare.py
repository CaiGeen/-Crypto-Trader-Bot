"""对比固定加仓阈值 vs ATR动态阈值。

固定阈值：首次涨幅达到2%/2.3%/3%时加仓
动态阈值：首次涨幅达到N×ATR%时加仓（如1.5ATR、2ATR、2.5ATR）
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

def simulate(signal, tp_l=0.18, tp_s=0.12,
             addon_thresh_type="fixed", addon_thresh=0.023, addon_size=1.0):
    """addon_thresh_type: 'fixed'=固定阈值, 'atr'=ATR倍数"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    # 计算实际加仓阈值
    if addon_thresh_type == "atr":
        actual_thresh = addon_thresh * (av / bt)  # N × ATR%
    else:
        actual_thresh = addon_thresh

    if is_long:
        init_stop = bt - av; tp_price = bt * (1 + tp_l) if tp_l > 0 else float("inf")
    else:
        init_stop = bt + av; tp_price = bt * (1 - tp_s) if tp_s > 0 else 0.0

    base_active = True
    base_exit_idx = base_exit_price = base_exit_reason = None
    addon_active = False
    addon_entry = addon_exit_idx = addon_exit_price = addon_exit_reason = None
    addon_entry_bar = None

    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if is_long: stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"

        if not addon_active and base_active:
            close_exc = (closes[j]-bt)/bt if is_long else (bt-closes[j])/bt
            if close_exc >= actual_thresh:
                addon_active = True
                addon_entry = float(closes[j])
                addon_entry_bar = bh
                addon_stop = bt

        if base_active:
            if is_long and lows[j] < stop_line:
                base_active = False; base_exit_idx, base_exit_price, base_exit_reason = j, float(stop_line), s_type
            elif not is_long and highs[j] > stop_line:
                base_active = False; base_exit_idx, base_exit_price, base_exit_reason = j, float(stop_line), s_type
            elif tp_l > 0 and is_long and highs[j] >= tp_price:
                base_active = False; base_exit_idx, base_exit_price, base_exit_reason = j, float(tp_price), "take_profit"
            elif tp_s > 0 and not is_long and lows[j] <= tp_price:
                base_active = False; base_exit_idx, base_exit_price, base_exit_reason = j, float(tp_price), "take_profit"

        if addon_active:
            if bh <= 3: a_stop = addon_stop
            else:
                if is_long: a_stop = max(ks, addon_stop)
                else: a_stop = min(ks, addon_stop)
            if is_long and lows[j] < a_stop:
                addon_active = False; addon_exit_idx, addon_exit_price, addon_exit_reason = j, float(a_stop), "addon_stop"
            elif not is_long and highs[j] > a_stop:
                addon_active = False; addon_exit_idx, addon_exit_price, addon_exit_reason = j, float(a_stop), "addon_stop"
            elif tp_l > 0 and is_long and highs[j] >= tp_price:
                addon_active = False; addon_exit_idx, addon_exit_price, addon_exit_reason = j, float(tp_price), "addon_tp"
            elif tp_s > 0 and not is_long and lows[j] <= tp_price:
                addon_active = False; addon_exit_idx, addon_exit_price, addon_exit_reason = j, float(tp_price), "addon_tp"

        if not base_active and not addon_active: break

    if base_exit_idx is None: base_exit_idx = n-1; base_exit_price = float(closes[-1]); base_exit_reason = "end"
    if addon_active and addon_exit_idx is None: addon_exit_idx = n-1; addon_exit_price = float(closes[-1]); addon_exit_reason = "end"

    base_r = (base_exit_price-bt)/av if is_long else (bt-base_exit_price)/av
    base_ret = (base_exit_price-bt)/bt if is_long else (bt-base_exit_price)/bt
    addon_r = 0.0; addon_ret = 0.0
    if addon_entry is not None:
        addon_r = (addon_exit_price-addon_entry)/av if is_long else (addon_entry-addon_exit_price)/av
        addon_ret = (addon_exit_price-addon_entry)/addon_entry if is_long else (addon_entry-addon_exit_price)/addon_entry

    total_r = base_r + addon_size * addon_r
    total_ret = base_ret + addon_size * addon_ret * (bt / addon_entry if addon_entry else 1)

    return {"direction": d, "base_r": base_r, "addon_r": addon_r * addon_size,
            "total_r": total_r, "total_ret": total_ret,
            "has_addon": addon_entry is not None, "addon_bar": addon_entry_bar,
            "atr_pct": av / bt, "actual_thresh": actual_thresh if addon_entry else None,
            "exit_idx": max(base_exit_idx, addon_exit_idx or 0)}

def run(thresh_type="fixed", thresh=0.023, size=1.0):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, addon_thresh_type=thresh_type, addon_thresh=thresh, addon_size=size)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 110)
print("固定阈值 vs ATR动态阈值 加仓对比")
print("=" * 110)

base = run(thresh_type="fixed", thresh=999, size=0)
print(f"\n基准（不加仓）: {len(base)}单, 累计R={base['total_r'].sum():.1f}")

# 1. 固定阈值
print(f"\n{'='*110}")
print("1. 固定阈值")
print(f"{'='*110}")
print(f"\n{'阈值':>6} {'加仓单':>6} {'加仓率':>6} {'累计R':>8} {'vs基准':>8} {'加仓R':>8} {'均R':>6}")
print("-" * 50)
for thresh in [0.015, 0.02, 0.023, 0.025, 0.03, 0.035, 0.04]:
    rdf = run(thresh_type="fixed", thresh=thresh, size=1.0)
    addon_cnt = rdf["has_addon"].sum()
    vs = rdf["total_r"].sum() - base["total_r"].sum()
    print(f"{thresh:>5.1%} {addon_cnt:>6} {addon_cnt/len(rdf):>5.0%} {rdf['total_r'].sum():>8.1f} {vs:>+8.1f} {rdf['addon_r'].sum():>8.1f} {rdf['total_r'].mean():>6.2f}")

# 2. ATR动态阈值
print(f"\n{'='*110}")
print("2. ATR动态阈值（阈值 = N × ATR%）")
print(f"{'='*110}")
print(f"\n{'N×ATR':>6} {'加仓单':>6} {'加仓率':>6} {'累计R':>8} {'vs基准':>8} {'加仓R':>8} {'均R':>6} {'实际阈值中位':>12}")
print("-" * 65)
for n_atr in [1.0, 1.5, 2.0, 2.3, 2.5, 3.0, 3.5, 4.0]:
    rdf = run(thresh_type="atr", thresh=n_atr, size=1.0)
    addon_cnt = rdf["has_addon"].sum()
    vs = rdf["total_r"].sum() - base["total_r"].sum()
    addon = rdf[rdf["has_addon"]]
    med_thresh = addon["actual_thresh"].median() if not addon.empty else 0
    print(f"{n_atr:>5.1f}x {addon_cnt:>6} {addon_cnt/len(rdf):>5.0%} {rdf['total_r'].sum():>8.1f} {vs:>+8.1f} {rdf['addon_r'].sum():>8.1f} {rdf['total_r'].mean():>6.2f} {med_thresh:>11.1%}")

# 3. ATR动态阈值按ATR%分档看效果
print(f"\n{'='*110}")
print("3. ATR动态阈值按ATR%分档（看动态阈值是否在各ATR档都有效）")
print(f"{'='*110}")

for n_atr in [2.0, 2.5, 3.0]:
    rdf = run(thresh_type="atr", thresh=n_atr, size=1.0)
    addon = rdf[rdf["has_addon"]]
    print(f"\n  N={n_atr}x ATR阈值:")
    print(f"  {'ATR%区间':>14} {'总单数':>6} {'加仓单':>6} {'加仓率':>6} {'加仓均R':>8} {'加仓累计R':>10} {'实际阈值中位':>12}")
    print("  " + "-" * 70)
    for lo, hi in [(0,0.01),(0.01,0.015),(0.015,0.02),(0.02,0.03),(0.03,1.0)]:
        sub = rdf[(rdf["atr_pct"]>=lo)&(rdf["atr_pct"]<hi)]
        sub_addon = addon[(addon["atr_pct"]>=lo)&(addon["atr_pct"]<hi)]
        if sub.empty: continue
        med_t = sub_addon["actual_thresh"].median() if not sub_addon.empty else 0
        ar = sub_addon["addon_r"].mean() if not sub_addon.empty else 0
        ar_sum = sub_addon["addon_r"].sum() if not sub_addon.empty else 0
        print(f"  [{lo:.1%},{hi:.1%})".rjust(14) + f" {len(sub):>6} {len(sub_addon):>6} {len(sub_addon)/len(sub):>5.0%} {ar:>8.2f} {ar_sum:>10.1f} {med_t:>11.1%}")

# 4. 最优对比
print(f"\n{'='*110}")
print("4. 最优配置对比")
print(f"{'='*110}")
print(f"\n{'配置':>30} {'累计R':>8} {'vs基准':>8} {'加仓率':>6} {'均R':>6}")
print("-" * 65)
configs = [
    ("基准（不加仓）", "fixed", 999),
    ("固定2.3%", "fixed", 0.023),
    ("固定3%", "fixed", 0.03),
    ("ATR 2.0x", "atr", 2.0),
    ("ATR 2.5x", "atr", 2.5),
    ("ATR 3.0x", "atr", 3.0),
    ("ATR 2.0x + 2倍加仓", "atr", 2.0),
]
for label, ttype, thresh in configs:
    size = 2.0 if "2倍" in label else 1.0
    rdf = run(thresh_type=ttype, thresh=thresh, size=size)
    vs = rdf["total_r"].sum() - base["total_r"].sum()
    ar = rdf["has_addon"].mean()
    print(f"{label:>30} {rdf['total_r'].sum():>8.1f} {vs:>+8.1f} {ar:>5.0%} {rdf['total_r'].mean():>6.2f}")

# 5. 固定2.3% vs ATR动态 在不同ATR档的加仓率对比
print(f"\n{'='*110}")
print("5. 固定2.3% vs ATR动态 在不同ATR档的加仓率对比")
print(f"{'='*110}")
fixed_rdf = run(thresh_type="fixed", thresh=0.023, size=1.0)
atr_rdf = run(thresh_type="atr", thresh=2.5, size=1.0)

print(f"\n{'ATR%区间':>14} {'固定2.3%加仓率':>14} {'ATR2.5x加仓率':>14} {'固定加仓均R':>12} {'ATR加仓均R':>12}")
print("-" * 70)
for lo, hi in [(0,0.01),(0.01,0.015),(0.015,0.02),(0.02,0.03),(0.03,1.0)]:
    f_sub = fixed_rdf[(fixed_rdf["atr_pct"]>=lo)&(fixed_rdf["atr_pct"]<hi)]
    a_sub = atr_rdf[(atr_rdf["atr_pct"]>=lo)&(atr_rdf["atr_pct"]<hi)]
    if f_sub.empty and a_sub.empty: continue
    f_rate = f_sub["has_addon"].mean()
    a_rate = a_sub["has_addon"].mean()
    f_addon = f_sub[f_sub["has_addon"]]
    a_addon = a_sub[a_sub["has_addon"]]
    f_ar = f_addon["addon_r"].mean() if not f_addon.empty else 0
    a_ar = a_addon["addon_r"].mean() if not a_addon.empty else 0
    print(f"  [{lo:.1%},{hi:.1%})".rjust(14) + f" {f_rate:>13.0%} {a_rate:>13.0%} {f_ar:>12.2f} {a_ar:>12.2f}")

print(f"\n{'='*110}")
