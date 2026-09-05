"""全面修正：用开盘价出场重新计算所有关键配置"""
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

def simulate(signal, strategy="base", trail_atr=0.5, tp_l=0.18, tp_s=0.12,
             addon=False, addon_thresh=0.023, include_bk=True):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    peak = bt
    addon_done = False; addon_price = 0.0
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if strategy == "base":
            if bh <= 3: sl = init_stop
            else: sl = max(ks, bt) if is_long else min(ks, bt)
        else:
            if bh <= 3: sl = init_stop
            else: sl = max(peak - trail_atr * av, bt, ks) if is_long else min(peak + trail_atr * av, bt, ks)
        if addon and not addon_done:
            cg = (closes[j] - bt) / bt if is_long else (bt - closes[j]) / bt
            if (include_bk or bh > 1) and cg >= addon_thresh:
                addon_done = True; addon_price = float(closes[j])
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            r_base = (ep-bt)/av
            r_add = ((ep-addon_price)/av if addon_done else 0.0)
            return {"r": r_base+r_add, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r_base = (bt-ep)/av
            r_add = ((addon_price-ep)/av if addon_done else 0.0)
            return {"r": r_base+r_add, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
        if is_long and highs[j] >= tp_price:
            r_base = (tp_price-bt)/av
            r_add = ((tp_price-addon_price)/av if addon_done else 0.0)
            return {"r": r_base+r_add, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
        if not is_long and lows[j] <= tp_price:
            r_base = (bt-tp_price)/av
            r_add = ((addon_price-tp_price)/av if addon_done else 0.0)
            return {"r": r_base+r_add, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d}
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    ep = float(closes[-1])
    r_base = (ep-bt)/av if is_long else (bt-ep)/av
    r_add = ((ep-addon_price)/av if is_long else (addon_price-ep)/av) if addon_done else 0.0
    return {"r": r_base+r_add, "exit_idx": n-1, "hold": n-1-i, "year": int(dates[n-1]), "dir": d}

def run(**kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 110)
print("实盘真实数据（开盘价出场，无未来函数）")
print("=" * 110)

base = run(strategy="base")
base_addon = run(strategy="base", addon=True)
print(f"\n基准: {len(base)}单, 累计R={base['r'].sum():.1f}, 均R={base['r'].mean():.2f}, 胜率={(base['r']>0).mean():.0%}")
print(f"基准+加仓: {len(base_addon)}单, 累计R={base_addon['r'].sum():.1f}, 均R={base_addon['r'].mean():.2f}, 胜率={(base_addon['r']>0).mean():.0%}")

print(f"\n{'='*110}")
print("trailing stop 细粒度扫描（开盘价出场）")
print(f"{'='*110}")
print(f"\n{'N*ATR':>6} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6} {'单数':>6}")
print("-" * 50)
trail_results = {}
for trail in [0.5, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5]:
    rdf = run(strategy="trailing", trail_atr=trail)
    vs = rdf["r"].sum() - base["r"].sum()
    trail_results[trail] = rdf
    print(f"{trail:>5.2f}x {rdf['r'].sum():>8.1f} {vs:>+8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(rdf):>6}")

print(f"\n{'='*110}")
print("trailing + 加仓（开盘价出场）")
print(f"{'='*110}")
print(f"\n{'N*ATR':>6} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6} {'单数':>6}")
print("-" * 50)
addon_results = {}
for trail in [0.5, 0.7, 1.0, 1.5]:
    rdf = run(strategy="trailing", trail_atr=trail, addon=True)
    vs = rdf["r"].sum() - base["r"].sum()
    addon_results[trail] = rdf
    print(f"{trail:>5.2f}x {rdf['r'].sum():>8.1f} {vs:>+8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(rdf):>6}")

print(f"\n{'='*110}")
print("年度稳定性（开盘价出场）")
print(f"{'='*110}")
for label, rdf in [("基准", base), ("trailing 0.5x", trail_results[0.5]), ("trailing 0.5x+加仓", addon_results[0.5])]:
    print(f"\n  {label}: 累计R={rdf['r'].sum():.1f}")
    print(f"  {'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'回撤R':>8}")
    print(f"  {'-'*46}")
    for yr in sorted(rdf['year'].unique()):
        sub = rdf[rdf['year']==yr]
        cum = sub['r'].cumsum()
        max_dd = (cum - cum.cummax()).min()
        print(f"  {yr:>6} {len(sub):>6} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f} {(sub['r']>0).mean():>5.0%} {max_dd:>8.1f}")

print(f"\n{'='*110}")
print("R值分布（开盘价出场）")
print(f"{'='*110}")
for label, rdf in [("基准", base), ("trailing 0.5x", trail_results[0.5])]:
    rvals = rdf['r']
    print(f"\n  {label}: 累计R={rvals.sum():.1f}, 均R={rvals.mean():.2f}, 胜率={(rvals>0).mean():.0%}")
    for lo, hi, tag in [(-99,-2,"R<-2"),(-2,-1,"-2~-1"),(-1,0,"-1~0"),(0,1,"0~1"),(1,2,"1~2"),(2,5,"2~5"),(5,99,"5+")]:
        cnt = ((rvals>=lo)&(rvals<hi)).sum()
        print(f"    {tag:>8}: {cnt:>5} ({cnt/len(rvals)*100:>5.1f}%)")

print(f"\n{'='*110}")
print("多空分拆（开盘价出场）")
print(f"{'='*110}")
for label, rdf in [("基准", base), ("trailing 0.5x", trail_results[0.5])]:
    print(f"\n  {label}:")
    for d in ["long", "short"]:
        sub = rdf[rdf['dir']==d]
        print(f"    {d}: {len(sub)}单, 累计R={sub['r'].sum():.1f}, 均R={sub['r'].mean():.2f}, 胜率={(sub['r']>0).mean():.0%}")

print(f"\n{'='*110}")
print("总结：实盘真实数据")
print(f"{'='*110}")
print(f"""
  基准:                {base['r'].sum():.1f}R (原回测996.0R, 虚高{996.0-base['r'].sum():.1f}R)
  trailing 0.5x:       {trail_results[0.5]['r'].sum():.1f}R (原回测1488.3R, 虚高{1488.3-trail_results[0.5]['r'].sum():.1f}R)
  基准+加仓:           {base_addon['r'].sum():.1f}R
  trailing 0.5x+加仓:  {addon_results[0.5]['r'].sum():.1f}R

  实盘真实差异:
    trailing vs 基准:          {trail_results[0.5]['r'].sum()-base['r'].sum():+.1f}R
    trailing+加仓 vs 基准:     {addon_results[0.5]['r'].sum()-base['r'].sum():+.1f}R
    trailing+加仓 vs 基准+加仓: {addon_results[0.5]['r'].sum()-base_addon['r'].sum():+.1f}R
""")
