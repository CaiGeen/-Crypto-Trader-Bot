"""分批止盈：50%@12%, 50%@22%或KAMA（实盘真实，开盘价出场）"""
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
dates = df['open_time'].dt.year.to_numpy()

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

def simulate_base(signal, tp_l=0.18, tp_s=0.12, addon=False, addon_thresh=0.023):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    addon_done = False; addon_price = 0.0
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(ks, bt) if is_long else min(ks, bt)
        if addon and not addon_done:
            cg = (closes[j]-bt)/bt if is_long else (bt-closes[j])/bt
            if cg >= addon_thresh: addon_done = True; addon_price = float(closes[j])
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            r1 = (ep-bt)/av; r2 = ((ep-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r1 = (bt-ep)/av; r2 = ((addon_price-ep)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
        if is_long and highs[j] >= tp_price:
            r1 = (tp_price-bt)/av; r2 = ((tp_price-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
        if not is_long and lows[j] <= tp_price:
            r1 = (bt-tp_price)/av; r2 = ((addon_price-tp_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
    ep = float(closes[-1])
    r1 = (ep-bt)/av if is_long else (bt-ep)/av
    r2 = ((ep-addon_price)/av if is_long else (addon_price-ep)/av) if addon_done else 0.0
    return {"r": r1+r2, "exit_idx": n-1, "hold": n-1-i, "year": int(dates[n-1]), "dir": d}

def simulate_partial(signal, addon=False, addon_thresh=0.023):
    """分批止盈：做多50%@12%+50%@22%或KAMA；做空50%@8%+50%@12%或KAMA"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long:
        init_stop = bt - av
        tp1 = bt * 1.12; tp2 = bt * 1.22  # 12%, 22%
    else:
        init_stop = bt + av
        tp1 = bt * 0.92; tp2 = bt * 0.88  # 8%, 12%
    r1_ratio = 0.50; r2_ratio = 0.50
    addon_done = False; addon_price = 0.0
    tp1_done = False; realized_r = 0.0; remaining = 1.0
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(ks, bt) if is_long else min(ks, bt)
        if addon and not addon_done:
            cg = (closes[j]-bt)/bt if is_long else (bt-closes[j])/bt
            if cg >= addon_thresh: addon_done = True; addon_price = float(closes[j])
        if not tp1_done:
            if is_long and highs[j] >= tp1:
                realized_r += r1_ratio * ((tp1-bt)/av)
                if addon_done: realized_r += r1_ratio * ((tp1-addon_price)/av)
                remaining -= r1_ratio; tp1_done = True
            elif not is_long and lows[j] <= tp1:
                realized_r += r1_ratio * ((bt-tp1)/av)
                if addon_done: realized_r += r1_ratio * ((addon_price-tp1)/av)
                remaining -= r1_ratio; tp1_done = True
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            r = realized_r + remaining * ((ep-bt)/av)
            if addon_done: r += remaining * ((ep-addon_price)/av)
            return {"r": r, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r = realized_r + remaining * ((bt-ep)/av)
            if addon_done: r += remaining * ((addon_price-ep)/av)
            return {"r": r, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
        if is_long and highs[j] >= tp2:
            r = realized_r + remaining * ((tp2-bt)/av)
            if addon_done: r += remaining * ((tp2-addon_price)/av)
            return {"r": r, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
        if not is_long and lows[j] <= tp2:
            r = realized_r + remaining * ((bt-tp2)/av)
            if addon_done: r += remaining * ((addon_price-tp2)/av)
            return {"r": r, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
    ep = float(closes[-1])
    r = realized_r + remaining * ((ep-bt)/av if is_long else (bt-ep)/av)
    if addon_done: r += remaining * ((ep-addon_price)/av if is_long else (addon_price-ep)/av)
    return {"r": r, "exit_idx": n-1, "hold": n-1-i, "year": int(dates[n-1]), "dir": d}

def run(sim_func, **kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = sim_func(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 110)
print("分批止盈 50%@12%+50%@22% vs 基准（实盘真实）")
print("=" * 110)

base = run(simulate_base)
base_addon = run(simulate_base, addon=True)
partial = run(simulate_partial)
partial_addon = run(simulate_partial, addon=True)

print(f"\n{'配置':>35} {'累计R':>8} {'均R':>6} {'胜率':>6} {'单数':>6}")
print("-" * 65)
for label, rdf in [("基准(22%/12%固定止盈)", base),
                    ("基准+加仓", base_addon),
                    ("分批50%@12%+50%@22%", partial),
                    ("分批+加仓", partial_addon)]:
    print(f"{label:>35} {rdf['r'].sum():>8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(rdf):>6}")

# 年度
print(f"\n{'='*110}")
print("年度稳定性")
print(f"{'='*110}")
for label, rdf in [("基准+加仓", base_addon), ("分批+加仓", partial_addon)]:
    print(f"\n  {label}: 累计R={rdf['r'].sum():.1f}")
    print(f"  {'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'回撤R':>8}")
    print(f"  {'-'*46}")
    for yr in sorted(rdf['year'].unique()):
        sub = rdf[rdf['year']==yr]
        cum = sub['r'].cumsum()
        max_dd = (cum - cum.cummax()).min()
        print(f"  {yr:>6} {len(sub):>6} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f} {(sub['r']>0).mean():>5.0%} {max_dd:>8.1f}")

# R分布
print(f"\n{'='*110}")
print("R值分布")
print(f"{'='*110}")
for label, rdf in [("基准+加仓", base_addon), ("分批+加仓", partial_addon)]:
    rvals = rdf['r']
    print(f"\n  {label}: 累计R={rvals.sum():.1f}, 均R={rvals.mean():.2f}, 胜率={(rvals>0).mean():.0%}")
    for lo, hi, tag in [(-99,-2,"R<-2"),(-2,-1,"-2~-1"),(-1,0,"-1~0"),(0,1,"0~1"),(1,2,"1~2"),(2,5,"2~5"),(5,99,"5+")]:
        cnt = ((rvals>=lo)&(rvals<hi)).sum()
        print(f"    {tag:>8}: {cnt:>5} ({cnt/len(rvals)*100:>5.1f}%)")

# 多空
print(f"\n{'='*110}")
print("多空分拆")
print(f"{'='*110}")
for label, rdf in [("基准+加仓", base_addon), ("分批+加仓", partial_addon)]:
    print(f"\n  {label}:")
    for d in ["long", "short"]:
        sub = rdf[rdf['dir']==d]
        print(f"    {d}: {len(sub)}单, 累计R={sub['r'].sum():.1f}, 均R={sub['r'].mean():.2f}, 胜率={(sub['r']>0).mean():.0%}")

print(f"\n{'='*110}")
