"""trailing stop出场后走势分析：是过早出场还是及时锁利？"""
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

def simulate_trailing(signal, trail_atr=0.5):
    """无未来函数trailing stop"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * 1.18
    else: init_stop = bt + av; tp_price = bt * 0.88
    peak = bt
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else:
            if is_long: sl = max(peak - trail_atr * av, bt, ks)
            else: sl = min(peak + trail_atr * av, bt, ks)
        if is_long and lows[j] < sl: return {"exit_idx": j, "exit_price": float(sl), "r": (sl-bt)/av}
        if not is_long and highs[j] > sl: return {"exit_idx": j, "exit_price": float(sl), "r": (bt-sl)/av}
        if is_long and highs[j] >= tp_price: return {"exit_idx": j, "exit_price": float(tp_price), "r": (tp_price-bt)/av}
        if not is_long and lows[j] <= tp_price: return {"exit_idx": j, "exit_price": float(tp_price), "r": (bt-tp_price)/av}
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    return {"exit_idx": n-1, "exit_price": float(closes[-1]), "r": (closes[-1]-bt)/av if is_long else (bt-closes[-1])/av}

def simulate_base(signal):
    """基准：固定止盈+KAMA止损"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * 1.18
    else: init_stop = bt + av; tp_price = bt * 0.88
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(ks, bt) if is_long else min(ks, bt)
        if is_long and lows[j] < sl: return {"exit_idx": j, "exit_price": float(sl), "r": (sl-bt)/av}
        if not is_long and highs[j] > sl: return {"exit_idx": j, "exit_price": float(sl), "r": (bt-sl)/av}
        if is_long and highs[j] >= tp_price: return {"exit_idx": j, "exit_price": float(tp_price), "r": (tp_price-bt)/av}
        if not is_long and lows[j] <= tp_price: return {"exit_idx": j, "exit_price": float(tp_price), "r": (bt-tp_price)/av}
    return {"exit_idx": n-1, "exit_price": float(closes[-1]), "r": (closes[-1]-bt)/av if is_long else (bt-closes[-1])/av}

# 收集所有trailing stop出场单，看出场后走势
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate_trailing(s, trail_atr=0.5)
    if r is None: continue
    r_base = simulate_base(s)
    exit_j = r["exit_idx"]
    bt = s.breakthrough; is_long = (s.direction == "long")
    av = atr_vals[s.idx - 1]

    # 出场后8根K线的最大有利变动
    post_max = bt
    for k in range(exit_j + 1, min(exit_j + 9, n)):
        if is_long: post_max = max(post_max, highs[k])
        else: post_max = min(post_max, lows[k])

    if is_long:
        missed = (post_max - r["exit_price"]) / av  # 错过的R
        post_gain_pct = (post_max - r["exit_price"]) / r["exit_price"] * 100
    else:
        missed = (r["exit_price"] - post_max) / av
        post_gain_pct = (r["exit_price"] - post_max) / r["exit_price"] * 100

    results.append({
        "r_trail": r["r"],
        "r_base": r_base["r"] if r_base else 0,
        "missed_r": missed,
        "post_gain_pct": post_gain_pct,
        "hold_trail": exit_j - s.idx,
        "hold_base": r_base["exit_idx"] - s.idx if r_base else 0,
        "direction": s.direction,
    })
    last_exit = exit_j

rdf = pd.DataFrame(results)

print("=" * 100)
print("trailing stop出场后走势分析（0.5x，无未来函数）")
print("=" * 100)

print(f"\n总单数: {len(rdf)}")
print(f"trailing stop 累计R: {rdf['r_trail'].sum():.1f}, 均R: {rdf['r_trail'].mean():.2f}")
print(f"基准(固定止盈+KAMA) 累计R: {rdf['r_base'].sum():.1f}, 均R: {rdf['r_base'].mean():.2f}")
print(f"差异: {rdf['r_trail'].sum() - rdf['r_base'].sum():+.1f}R")

# 出场后8根最大涨幅分布
print(f"\n{'='*100}")
print("出场后8根K线最大涨幅分布（错过的利润）")
print("=" * 100)
print(f"\n{'涨幅区间':>12} {'单数':>6} {'占比':>6} {'这些单的':>10} {'这些单的':>10} {'这些单':>10}")
print(f"{'':>12} {'':>6} {'':>6} {'trailing均R':>12} {'基准均R':>10} {'错过均R':>10}")
print("-" * 70)
for lo, hi, label in [(-99,-2,"跌2%+"),(-2,-1,"跌1-2%"),(-1,0,"跌0-1%"),(0,0.5,"涨0-0.5%"),(0.5,1,"涨0.5-1%"),(1,2,"涨1-2%"),(2,5,"涨2-5%"),(5,99,"涨5%+")]:
    sub = rdf[(rdf['post_gain_pct']>=lo) & (rdf['post_gain_pct']<hi)]
    if len(sub) == 0: continue
    print(f"{label:>12} {len(sub):>6} {len(sub)/len(rdf)*100:>5.1f}% {sub['r_trail'].mean():>12.2f} {sub['r_base'].mean():>10.2f} {sub['missed_r'].mean():>10.2f}")

# 逐单对比：trailing赚更多 vs 基准赚更多
print(f"\n{'='*100}")
print("逐单对比：trailing vs 基准")
print("=" * 100)
trail_better = rdf[rdf['r_trail'] > rdf['r_base']]
base_better = rdf[rdf['r_base'] > rdf['r_trail']]
equal = rdf[rdf['r_trail'] == rdf['r_base']]
print(f"\n  trailing赚更多: {len(trail_better)}单 ({len(trail_better)/len(rdf)*100:.0f}%), 累计ΔR={rdf['r_trail'].sum()-rdf['r_base'].sum():+.1f}")
print(f"    其中trailing均R={trail_better['r_trail'].mean():.2f}, 基准均R={trail_better['r_base'].mean():.2f}")
print(f"  基准赚更多: {len(base_better)}单 ({len(base_better)/len(rdf)*100:.0f}%)")
print(f"    其中trailing均R={base_better['r_trail'].mean():.2f}, 基准均R={base_better['r_base'].mean():.2f}")
print(f"  相同: {len(equal)}单")

# 基准赚更多的单子分析：这些是trailing过早出场但基准持有到更大涨幅的
print(f"\n{'='*100}")
print(f"基准赚更多的{len(base_better)}单深入分析（trailing过早出场？）")
print("=" * 100)
print(f"\n  这些单 trailing累计R={base_better['r_trail'].sum():.1f}, 基准累计R={base_better['r_base'].sum():.1f}")
print(f"  差额: {base_better['r_base'].sum() - base_better['r_trail'].sum():.1f}R（基准多赚的）")
print(f"  trailing均R={base_better['r_trail'].mean():.2f}, 基准均R={base_better['r_base'].mean():.2f}")
print(f"  trailing持仓中位{base_better['hold_trail'].median():.0f}K, 基准持仓中位{base_better['hold_base'].median():.0f}K")
print(f"  出场后8根最大涨幅: 均值{base_better['post_gain_pct'].mean():.1f}%, 中位{base_better['post_gain_pct'].median():.1f}%")

# trailing赚更多的单子分析
print(f"\n{'='*100}")
print(f"trailing赚更多的{len(trail_better)}单深入分析（及时锁利？）")
print("=" * 100)
print(f"\n  这些单 trailing累计R={trail_better['r_trail'].sum():.1f}, 基准累计R={trail_better['r_base'].sum():.1f}")
print(f"  差额: {trail_better['r_trail'].sum() - trail_better['r_base'].sum():.1f}R（trailing多赚的）")
print(f"  trailing均R={trail_better['r_trail'].mean():.2f}, 基准均R={trail_better['r_base'].mean():.2f}")
print(f"  trailing持仓中位{trail_better['hold_trail'].median():.0f}K, 基准持仓中位{trail_better['hold_base'].median():.0f}K")
print(f"  出场后8根最大涨幅: 均值{trail_better['post_gain_pct'].mean():.1f}%, 中位{trail_better['post_gain_pct'].median():.1f}%")

# 按trailing的R值分组看错过情况
print(f"\n{'='*100}")
print("按trailing出场R值分组：出场后走势")
print("=" * 100)
print(f"\n{'trailing R':>12} {'单数':>6} {'trailing均R':>12} {'基准均R':>10} {'错过均R':>10} {'出场后涨幅':>12}")
print("-" * 70)
for lo, hi, label in [(-2,-1,"R=-1~-0"),(-1,0,"R=0~1"),(1,3,"R=1~3"),(3,5,"R=3~5"),(5,99,"R=5+")]:
    sub = rdf[(rdf['r_trail']>=lo) & (rdf['r_trail']<hi)]
    if len(sub) == 0: continue
    print(f"{label:>12} {len(sub):>6} {sub['r_trail'].mean():>12.2f} {sub['r_base'].mean():>10.2f} {sub['missed_r'].mean():>10.2f} {sub['post_gain_pct'].mean():>11.1f}%")

print(f"\n{'='*100}")
