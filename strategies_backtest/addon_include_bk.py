"""对比加仓观察是否包含突破当K的效果。

不含突破当K（原版）：从j=i+1开始观察
含突破当K：先检查close[i]是否满足加仓条件，再从j=i+1继续观察
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
closes = df["close"].to_numpy(); n = len(df)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

def simulate(signal, tp_l=0.18, tp_s=0.12,
             thresh_type="atr", thresh=1.5, addon_size=1.0, include_breakout_k=False):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    if thresh_type == "atr":
        actual_thresh = thresh * (av / bt)
    else:
        actual_thresh = thresh

    if is_long:
        init_stop = bt - av; tp_price = bt * (1 + tp_l) if tp_l > 0 else float("inf")
    else:
        init_stop = bt + av; tp_price = bt * (1 - tp_s) if tp_s > 0 else 0.0

    base_active = True
    base_exit_idx = base_exit_price = base_exit_reason = None
    addon_active = False
    addon_entry = addon_exit_idx = addon_exit_price = addon_exit_reason = None
    addon_entry_bar = None

    # 如果包含突破当K，先检查close[i]
    if include_breakout_k:
        close_exc = (closes[i]-bt)/bt if is_long else (bt-closes[i])/bt
        if close_exc >= actual_thresh:
            addon_active = True
            addon_entry = float(closes[i])
            addon_entry_bar = 0  # 突破当K
            addon_stop = bt

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
            "atr_pct": av / bt, "exit_idx": max(base_exit_idx, addon_exit_idx or 0)}

def run(thresh_type="atr", thresh=1.5, size=1.0, include_bk=False):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, thresh_type=thresh_type, thresh=thresh, addon_size=size, include_breakout_k=include_bk)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 110)
print("加仓观察是否包含突破当K 对比")
print("=" * 110)

base = run(thresh_type="fixed", thresh=999, size=0)
print(f"\n基准（不加仓）: {len(base)}单, 累计R={base['total_r'].sum():.1f}")

# 1. ATR动态阈值对比
print(f"\n{'='*110}")
print("1. ATR动态阈值：不含突破当K vs 含突破当K")
print(f"{'='*110}")
print(f"\n{'N×ATR':>6} {'不含BK加仓率':>12} {'含BK加仓率':>12} {'不含累计R':>10} {'含BK累计R':>10} {'不含vs基准':>10} {'含BKvs基准':>10} {'差异':>8}")
print("-" * 80)
for n_atr in [1.0, 1.5, 2.0, 2.5, 3.0]:
    r_no = run(thresh_type="atr", thresh=n_atr, include_bk=False)
    r_yes = run(thresh_type="atr", thresh=n_atr, include_bk=True)
    vs_no = r_no["total_r"].sum() - base["total_r"].sum()
    vs_yes = r_yes["total_r"].sum() - base["total_r"].sum()
    print(f"{n_atr:>5.1f}x {r_no['has_addon'].mean():>11.0%} {r_yes['has_addon'].mean():>11.0%} {r_no['total_r'].sum():>10.1f} {r_yes['total_r'].sum():>10.1f} {vs_no:>+10.1f} {vs_yes:>+10.1f} {vs_yes-vs_no:>+8.1f}")

# 2. 固定阈值对比
print(f"\n{'='*110}")
print("2. 固定阈值：不含突破当K vs 含突破当K")
print(f"{'='*110}")
print(f"\n{'阈值':>6} {'不含BK加仓率':>12} {'含BK加仓率':>12} {'不含累计R':>10} {'含BK累计R':>10} {'不含vs基准':>10} {'含BKvs基准':>10} {'差异':>8}")
print("-" * 80)
for thresh in [0.015, 0.02, 0.023, 0.03, 0.04]:
    r_no = run(thresh_type="fixed", thresh=thresh, include_bk=False)
    r_yes = run(thresh_type="fixed", thresh=thresh, include_bk=True)
    vs_no = r_no["total_r"].sum() - base["total_r"].sum()
    vs_yes = r_yes["total_r"].sum() - base["total_r"].sum()
    print(f"{thresh:>5.1%} {r_no['has_addon'].mean():>11.0%} {r_yes['has_addon'].mean():>11.0%} {r_no['total_r'].sum():>10.1f} {r_yes['total_r'].sum():>10.1f} {vs_no:>+10.1f} {vs_yes:>+10.1f} {vs_yes-vs_no:>+8.1f}")

# 3. 详细分析：ATR 1.5x 含突破当K
print(f"\n{'='*110}")
print("3. 详细分析：ATR 1.5x 含突破当K")
print(f"{'='*110}")
r_yes = run(thresh_type="atr", thresh=1.5, include_bk=True)
r_no = run(thresh_type="atr", thresh=1.5, include_bk=False)
addon_yes = r_yes[r_yes["has_addon"]]
addon_no = r_no[r_no["has_addon"]]

print(f"\n  不含BK: {len(r_no)}单, 加仓{len(addon_no)}单, 累计R={r_no['total_r'].sum():.1f}, 加仓R={addon_no['addon_r'].sum():.1f}")
print(f"  含BK:   {len(r_yes)}单, 加仓{len(addon_yes)}单, 累计R={r_yes['total_r'].sum():.1f}, 加仓R={addon_yes['addon_r'].sum():.1f}")

# 突破当K加仓的单子
bk_addon = addon_yes[addon_yes["addon_bar"] == 0]
later_addon = addon_yes[addon_yes["addon_bar"] > 0]
print(f"\n  突破当K加仓: {len(bk_addon)}单, 加仓均R={bk_addon['addon_r'].mean():.2f}, 加仓累计R={bk_addon['addon_r'].sum():.1f}")
print(f"  后续加仓:   {len(later_addon)}单, 加仓均R={later_addon['addon_r'].mean():.2f}, 加仓累计R={later_addon['addon_r'].sum():.1f}")

# 突破当K加仓的单子特征
print(f"\n  突破当K加仓的{len(bk_addon)}单特征:")
print(f"    基础均R={bk_addon['base_r'].mean():.2f}, 总均R={bk_addon['total_r'].mean():.2f}, ATR%中位={bk_addon['atr_pct'].median():.1%}")
for d, lbl in [("long", "做多"), ("short", "做空")]:
    sub = bk_addon[bk_addon["direction"] == d]
    if sub.empty: continue
    print(f"    {lbl}: {len(sub)}单, 加仓均R={sub['addon_r'].mean():.2f}, 基础均R={sub['base_r'].mean():.2f}")

# 4. 突破当K加仓的ATR%分布
print(f"\n  突破当K加仓的ATR%分布:")
print(f"  {'ATR%区间':>14} {'单数':>5} {'加仓均R':>8} {'基础均R':>8} {'加仓累计R':>10}")
print("  " + "-" * 50)
for lo, hi in [(0,0.01),(0.01,0.015),(0.015,0.02),(0.02,0.03),(0.03,1.0)]:
    sub = bk_addon[(bk_addon["atr_pct"]>=lo)&(bk_addon["atr_pct"]<hi)]
    if sub.empty: continue
    print(f"  [{lo:.1%},{hi:.1%})".rjust(14) + f" {len(sub):>5} {sub['addon_r'].mean():>8.2f} {sub['base_r'].mean():>8.2f} {sub['addon_r'].sum():>10.1f}")

# 5. 最优对比汇总
print(f"\n{'='*110}")
print("4. 最优配置汇总对比")
print(f"{'='*110}")
print(f"\n{'配置':>35} {'累计R':>8} {'vs基准':>8} {'加仓率':>6} {'加仓R':>8}")
print("-" * 70)
configs = [
    ("基准（不加仓）", "fixed", 999, False, 1.0),
    ("ATR1.5x 不含BK", "atr", 1.5, False, 1.0),
    ("ATR1.5x 含BK", "atr", 1.5, True, 1.0),
    ("ATR2.0x 含BK", "atr", 2.0, True, 1.0),
    ("固定2.3% 含BK", "fixed", 0.023, True, 1.0),
    ("ATR1.5x 含BK 2倍加仓", "atr", 1.5, True, 2.0),
]
for label, ttype, thresh, bk, size in configs:
    rdf = run(thresh_type=ttype, thresh=thresh, include_bk=bk, size=size)
    vs = rdf["total_r"].sum() - base["total_r"].sum()
    ar = rdf["has_addon"].mean()
    addon_r = rdf[rdf["has_addon"]]["addon_r"].sum()
    print(f"{label:>35} {rdf['total_r'].sum():>8.1f} {vs:>+8.1f} {ar:>5.0%} {addon_r:>8.1f}")

print(f"\n{'='*110}")
