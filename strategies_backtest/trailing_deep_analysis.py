"""trailing 0.5x vs 1.0x 深入对比：年度稳定性、R分布、持仓时间。"""
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
dates = df['open_time'].dt.year.to_numpy()

def simulate_trailing(signal, tp_l=0.18, tp_s=0.12, trail_atr=1.0):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    peak = bt
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
        if bh <= 3:
            sl = init_stop
        else:
            if is_long:
                trail_stop = peak - trail_atr * av
                sl = max(trail_stop, bt, ks)
            else:
                trail_stop = peak + trail_atr * av
                sl = min(trail_stop, bt, ks)
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "hold_bars": exit_idx - i,
            "direction": d, "exit_reason": exit_reason, "year": int(dates[exit_idx])}

def run(sim_func, **kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = sim_func(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

for trail in [0.5, 1.0]:
    rdf = run(simulate_trailing, trail_atr=trail)
    print(f"\n{'='*90}")
    print(f"trailing {trail}x ATR — 深入分析")
    print(f"{'='*90}")
    print(f"  总计: {len(rdf)}单, 累计R={rdf['r_value'].sum():.1f}, 均R={rdf['r_value'].mean():.2f}, 胜率={(rdf['r_value']>0).mean():.0%}")

    # 年度
    print(f"\n  年度稳定性:")
    print(f"  {'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'最大回撤R':>10}")
    print(f"  {'-'*52}")
    for yr in sorted(rdf['year'].unique()):
        sub = rdf[rdf['year']==yr]
        cum = sub['r_value'].cumsum()
        max_dd = (cum - cum.cummax()).min()
        print(f"  {yr:>6} {len(sub):>6} {sub['r_value'].sum():>8.1f} {sub['r_value'].mean():>6.2f} {(sub['r_value']>0).mean():>5.0%} {max_dd:>10.1f}")

    # R分布
    print(f"\n  R值分布:")
    rvals = rdf['r_value']
    for lo, hi, label in [(-99,-3,"R<-3"),(-3,-2,"-3~-2"),(-2,-1,"-2~-1"),(-1,0,"-1~0"),(0,1,"0~1"),(1,2,"1~2"),(2,5,"2~5"),(5,99,"5+")]:
        cnt = ((rvals>=lo)&(rvals<hi)).sum()
        pct = cnt/len(rvals)*100
        bar = "█"*int(pct/2)
        print(f"    {label:>8}: {cnt:>5} ({pct:>5.1f}%) {bar}")

    # 持仓时间
    print(f"\n  持仓时间分布:")
    hb = rdf['hold_bars']
    for lo, hi, label in [(1,2,"1K"),(2,4,"2-3K"),(4,8,"4-7K"),(8,16,"8-15K"),(16,99,"16K+")]:
        cnt = ((hb>=lo)&(hb<hi)).sum()
        pct = cnt/len(hb)*100
        print(f"    {label:>8}: {cnt:>5} ({pct:>5.1f}%)")
    print(f"    中位数: {hb.median():.0f}K, 均值: {hb.mean():.1f}K")

    # 多空
    print(f"\n  多空分拆:")
    for d in ["long", "short"]:
        sub = rdf[rdf['direction']==d]
        print(f"    {d}: {len(sub)}单, 累计R={sub['r_value'].sum():.1f}, 均R={sub['r_value'].mean():.2f}, 胜率={(sub['r_value']>0).mean():.0%}")

    # 出场原因
    print(f"\n  出场原因:")
    for reason in ["stop", "tp", "end"]:
        sub = rdf[rdf['exit_reason']==reason]
        if len(sub) > 0:
            print(f"    {reason}: {len(sub)}单 ({len(sub)/len(rdf)*100:.0f}%), 均R={sub['r_value'].mean():.2f}")

# 对比基准
print(f"\n{'='*90}")
print("关键对比")
print(f"{'='*90}")
base = run(simulate_trailing, trail_atr=999)
print(f"\n  基准(无trailing):     {len(base)}单, 累计R={base['r_value'].sum():.1f}, 均R={base['r_value'].mean():.2f}, 胜率={(base['r_value']>0).mean():.0%}, 持仓中位{base['hold_bars'].median():.0f}K")
for trail in [0.5, 1.0]:
    rdf = run(simulate_trailing, trail_atr=trail)
    print(f"  trailing {trail}x:         {len(rdf)}单, 累计R={rdf['r_value'].sum():.1f}, 均R={rdf['r_value'].mean():.2f}, 胜率={(rdf['r_value']>0).mean():.0%}, 持仓中位{rdf['hold_bars'].median():.0f}K")

print(f"\n{'='*90}")
