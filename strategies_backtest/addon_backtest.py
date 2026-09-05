"""回测基于首次突破阈值的加仓策略。

加仓逻辑：
- 基础仓位：1单位，入场=突破线，止损=突破线±ATR
- 加仓仓位：首次实体突破阈值时加仓，入场=该根收盘价，止损=突破线（保本）
- 两笔仓位分别止盈（固定22%/12%）
- 总R = 基础R + 加仓R
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

def simulate_with_addon(signal, tp_l=0.18, tp_s=0.12,
                         addon_thresh=0.023, addon_bar_limit=999, addon_size=1.0):
    """带加仓的回测。
    addon_thresh: 首次实体突破此阈值时加仓
    addon_bar_limit: 仅在前addon_bar_limit根内突破才加仓
    addon_size: 加仓比例（相对基础仓位）
    """
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long:
        init_stop = bt - av; tp_price = bt * (1 + tp_l) if tp_l > 0 else float("inf")
    else:
        init_stop = bt + av; tp_price = bt * (1 - tp_s) if tp_s > 0 else 0.0

    # 基础仓位状态
    base_active = True
    base_exit_idx = base_exit_price = base_exit_reason = None
    # 加仓仓位状态
    addon_active = False
    addon_entry = addon_exit_idx = addon_exit_price = addon_exit_reason = None
    addon_entry_bar = None

    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i

        # KAMA跟踪止损线（第3K后）
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if is_long: stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"

        # 检查加仓触发（在基础仓位还活着的时候）
        if not addon_active and base_active and bh <= addon_bar_limit:
            close_exc = (closes[j]-bt)/bt if is_long else (bt-closes[j])/bt
            if close_exc >= addon_thresh:
                addon_active = True
                addon_entry = float(closes[j])
                addon_entry_bar = bh
                # 加仓止损=突破线（保本）
                addon_stop = bt

        # 基础仓位止损/止盈
        if base_active:
            if is_long and lows[j] < stop_line:
                base_active = False; base_exit_idx, base_exit_price, base_exit_reason = j, float(stop_line), s_type
            elif not is_long and highs[j] > stop_line:
                base_active = False; base_exit_idx, base_exit_price, base_exit_reason = j, float(stop_line), s_type
            elif tp_l > 0 and is_long and highs[j] >= tp_price:
                base_active = False; base_exit_idx, base_exit_price, base_exit_reason = j, float(tp_price), "take_profit"
            elif tp_s > 0 and not is_long and lows[j] <= tp_price:
                base_active = False; base_exit_idx, base_exit_price, base_exit_reason = j, float(tp_price), "take_profit"

        # 加仓仓位止损/止盈
        if addon_active:
            # 加仓用KAMA跟踪（第3K后）或保本止损
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

    # 未平仓处理
    if base_exit_idx is None: base_exit_idx = n-1; base_exit_price = float(closes[-1]); base_exit_reason = "end"
    if addon_active and addon_exit_idx is None: addon_exit_idx = n-1; addon_exit_price = float(closes[-1]); addon_exit_reason = "end"

    # 计算R值
    base_r = (base_exit_price-bt)/av if is_long else (bt-base_exit_price)/av
    base_ret = (base_exit_price-bt)/bt if is_long else (bt-base_exit_price)/bt

    addon_r = 0.0; addon_ret = 0.0
    if addon_entry is not None:
        addon_r = (addon_exit_price-addon_entry)/av if is_long else (addon_entry-addon_exit_price)/av
        addon_ret = (addon_exit_price-addon_entry)/addon_entry if is_long else (addon_entry-addon_exit_price)/addon_entry

    total_r = base_r + addon_size * addon_r
    total_ret = base_ret + addon_size * addon_ret * (bt / addon_entry if addon_entry else 1)

    return {"direction": d, "base_r": base_r, "addon_r": addon_r * addon_size,
            "total_r": total_r, "base_ret": base_ret, "total_ret": total_ret,
            "has_addon": addon_entry is not None, "addon_bar": addon_entry_bar,
            "exit_idx": max(base_exit_idx, addon_exit_idx or 0),
            "hold_bars": max(base_exit_idx, addon_exit_idx or 0) - i}

def run(addon_thresh=0.023, addon_bar_limit=999, addon_size=1.0):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate_with_addon(s, addon_thresh=addon_thresh, addon_bar_limit=addon_bar_limit, addon_size=addon_size)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 110)
print("加仓策略回测（首次突破阈值时加仓）")
print("=" * 110)

# 基准（不加仓）
base = run(addon_thresh=999, addon_size=0)
print(f"\n基准（不加仓）: {len(base)}单, 累计R={base['total_r'].sum():.1f}, 均R={base['total_r'].mean():.2f}")

# 1. 扫描阈值 × 加仓时间限制
print(f"\n{'='*110}")
print("1. 扫描阈值 × 加仓时间限制（加仓1倍）")
print(f"{'='*110}")
print(f"\n{'阈值':>6} {'时间限制':>8} {'总单数':>6} {'加仓单':>6} {'加仓率':>6} {'累计R':>8} {'均R':>6} {'vs基准':>8} {'基础R':>8} {'加仓R':>8}")
print("-" * 80)
for thresh in [0.02, 0.023, 0.03, 0.04, 0.06]:
    for bar_limit in [999, 2, 3, 5]:
        rdf = run(addon_thresh=thresh, addon_bar_limit=bar_limit, addon_size=1.0)
        addon_cnt = rdf["has_addon"].sum()
        vs = rdf["total_r"].sum() - base["total_r"].sum()
        bl = "不限" if bar_limit == 999 else f"前{bar_limit}根"
        print(f"{thresh:>5.1%} {bl:>8} {len(rdf):>6} {addon_cnt:>6} {addon_cnt/len(rdf):>5.0%} {rdf['total_r'].sum():>8.1f} {rdf['total_r'].mean():>6.2f} {vs:>+8.1f} {rdf['base_r'].sum():>8.1f} {rdf['addon_r'].sum():>8.1f}")

# 2. 扫描加仓比例
print(f"\n{'='*110}")
print("2. 扫描加仓比例（阈值2.3%，不限时间）")
print(f"{'='*110}")
print(f"\n{'加仓比例':>8} {'累计R':>8} {'均R':>6} {'vs基准':>8} {'基础R':>8} {'加仓R':>8} {'收益':>9}")
print("-" * 60)
for size in [0.5, 1.0, 1.5, 2.0]:
    rdf = run(addon_thresh=0.023, addon_bar_limit=999, addon_size=size)
    vs = rdf["total_r"].sum() - base["total_r"].sum()
    print(f"{size:>7.1f}x {rdf['total_r'].sum():>8.1f} {rdf['total_r'].mean():>6.2f} {vs:>+8.1f} {rdf['base_r'].sum():>8.1f} {rdf['addon_r'].sum():>8.1f} {rdf['total_ret'].sum():>9.1%}")

# 3. 最优参数详细分析
print(f"\n{'='*110}")
print("3. 详细分析：阈值2.3% + 不限时间 + 加仓1倍")
print(f"{'='*110}")
rdf = run(addon_thresh=0.023, addon_bar_limit=999, addon_size=1.0)
addon = rdf[rdf["has_addon"]]
no_addon = rdf[~rdf["has_addon"]]

print(f"\n  基准: {len(base)}单, 累计R={base['total_r'].sum():.1f}")
print(f"  加仓策略: {len(rdf)}单, 累计R={rdf['total_r'].sum():.1f}, 差异={rdf['total_r'].sum()-base['total_r'].sum():+.1f}R")
print(f"\n  加仓的单子: {len(addon)}单")
print(f"    基础R: {addon['base_r'].sum():.1f}, 加仓R: {addon['addon_r'].sum():.1f}, 总R: {addon['total_r'].sum():.1f}")
print(f"    加仓R占比: {addon['addon_r'].sum()/addon['total_r'].sum():.0%}")
print(f"  未加仓的单子: {len(no_addon)}单, 总R={no_addon['total_r'].sum():.1f}")

# 加仓时间分布
print(f"\n  加仓触发时间分布:")
print(f"  {'首次在第N根':>10} {'单数':>5} {'基础均R':>8} {'加仓均R':>8} {'总均R':>8} {'加仓R占比':>9}")
print("  " + "-" * 55)
for lo, hi, lbl in [(1,1,"第1根"),(2,2,"第2根"),(3,3,"第3根"),(4,5,"第4-5根"),(6,8,"第6-8根"),(9,12,"第9-12根"),(13,999,"第13根+")]:
    sub = addon[(addon["addon_bar"]>=lo)&(addon["addon_bar"]<=hi)]
    if sub.empty: continue
    ar = sub["addon_r"].sum(); tr = sub["total_r"].sum()
    print(f"  {lbl:>10} {len(sub):>5} {sub['base_r'].mean():>8.2f} {sub['addon_r'].mean():>8.2f} {sub['total_r'].mean():>8.2f} {ar/tr:>8.0%}")

# 多空分离
print(f"\n  多空分离:")
for d, lbl in [("long", "做多"), ("short", "做空")]:
    b_sub = base[base["direction"] == d]
    r_sub = rdf[rdf["direction"] == d]
    print(f"  {lbl}: 基准{len(b_sub)}单/R={b_sub['total_r'].sum():.1f}, 加仓{len(r_sub)}单/R={r_sub['total_r'].sum():.1f}, 差异{r_sub['total_r'].sum()-b_sub['total_r'].sum():+.1f}R")

# 4. 不同阈值的最优加仓比例
print(f"\n{'='*110}")
print("4. 不同阈值的最优配置对比")
print(f"{'='*110}")
print(f"\n{'配置':>30} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'加仓率':>6}")
print("-" * 65)
configs = [
    ("基准（不加仓）", 999, 999, 0),
    ("2.3%阈值/不限/1x", 0.023, 999, 1.0),
    ("2.3%阈值/前2根/1x", 0.023, 2, 1.0),
    ("2.3%阈值/前3根/1x", 0.023, 3, 1.0),
    ("3%阈值/不限/1x", 0.03, 999, 1.0),
    ("3%阈值/前3根/1x", 0.03, 3, 1.0),
    ("4%阈值/不限/1x", 0.04, 999, 1.0),
    ("2.3%阈值/不限/0.5x", 0.023, 999, 0.5),
    ("2.3%阈值/不限/2x", 0.023, 999, 2.0),
    ("6%阈值/不限/1x", 0.06, 999, 1.0),
]
for label, thresh, bar_lim, size in configs:
    rdf = run(addon_thresh=thresh, addon_bar_limit=bar_lim, addon_size=size)
    vs = rdf["total_r"].sum() - base["total_r"].sum()
    ar = rdf["has_addon"].mean()
    print(f"{label:>30} {rdf['total_r'].sum():>8.1f} {vs:>+8.1f} {rdf['total_r'].mean():>6.2f} {ar:>5.0%}")

print(f"\n{'='*110}")
