"""trailing stop：固定ATR vs 动态ATR对比（无未来函数）"""
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

def simulate(signal, tp_l=0.18, tp_s=0.12, trail_atr=0.5, dynamic_atr=False):
    """无未来函数trailing stop。dynamic_atr=True时ATR每根更新"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av_fixed = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av_fixed) or av_fixed <= 0: return None
    if is_long: init_stop = bt - av_fixed; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av_fixed; tp_price = bt * (1 - tp_s)
    peak = bt
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        # ATR选择：固定 or 动态
        av = atr_vals[j-1] if dynamic_atr else av_fixed
        if np.isnan(av) or av <= 0: av = av_fixed
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
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av_fixed if is_long else (bt-exit_price)/av_fixed
    return {"r_value": r, "exit_idx": exit_idx, "hold_bars": exit_idx - i,
            "direction": d, "exit_reason": exit_reason, "year": int(dates[exit_idx])}

def run(sim_func, **kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = sim_func(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 100)
print("trailing stop：固定ATR vs 动态ATR对比（无未来函数）")
print("=" * 100)

base = run(simulate, trail_atr=999)
print(f"\n基准(无trailing): {len(base)}单, 累计R={base['r_value'].sum():.1f}, 均R={base['r_value'].mean():.2f}, 胜率={(base['r_value']>0).mean():.0%}")

print(f"\n{'N*ATR':>6} │ {'固定ATR(开仓时锁定)':>35} │ {'动态ATR(每根更新)':>35} │ {'差异':>10}")
print(f"{'':>6} │ {'累计R':>8} {'均R':>6} {'胜率':>6} {'单数':>6} │ {'累计R':>8} {'均R':>6} {'胜率':>6} {'单数':>6} │ {'ΔR':>8}")
print("-" * 100)
for trail in [0.5, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5]:
    df_f = run(simulate, trail_atr=trail, dynamic_atr=False)
    df_d = run(simulate, trail_atr=trail, dynamic_atr=True)
    dr = df_d['r_value'].sum() - df_f['r_value'].sum()
    print(f"{trail:>5.2f}x │ {df_f['r_value'].sum():>8.1f} {df_f['r_value'].mean():>6.2f} {(df_f['r_value']>0).mean():>5.0%} {len(df_f):>6} │ {df_d['r_value'].sum():>8.1f} {df_d['r_value'].mean():>6.2f} {(df_d['r_value']>0).mean():>5.0%} {len(df_d):>6} │ {dr:>+8.1f}")

# 动态ATR年度稳定性
print(f"\n{'='*100}")
print("动态ATR trailing 0.5x 年度稳定性")
print("=" * 100)
rdf = run(simulate, trail_atr=0.5, dynamic_atr=True)
print(f"\n  总计: {len(rdf)}单, 累计R={rdf['r_value'].sum():.1f}, 均R={rdf['r_value'].mean():.2f}, 胜率={(rdf['r_value']>0).mean():.0%}")
print(f"\n  {'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'最大回撤R':>10}")
print(f"  {'-'*52}")
for yr in sorted(rdf['year'].unique()):
    sub = rdf[rdf['year']==yr]
    cum = sub['r_value'].cumsum()
    max_dd = (cum - cum.cummax()).min()
    print(f"  {yr:>6} {len(sub):>6} {sub['r_value'].sum():>8.1f} {sub['r_value'].mean():>6.2f} {(sub['r_value']>0).mean():>5.0%} {max_dd:>10.1f}")

# R分布对比
print(f"\n{'='*100}")
print("R值分布对比（0.5x）")
print("=" * 100)
for label, dyn in [("固定ATR", False), ("动态ATR", True)]:
    rdf = run(simulate, trail_atr=0.5, dynamic_atr=dyn)
    rvals = rdf['r_value']
    print(f"\n  {label}: 累计R={rvals.sum():.1f}, 均R={rvals.mean():.2f}, 胜率={(rvals>0).mean():.0%}")
    for lo, hi, tag in [(-99,-2,"R<-2"),(-2,-1,"-2~-1"),(-1,0,"-1~0"),(0,1,"0~1"),(1,2,"1~2"),(2,5,"2~5"),(5,99,"5+")]:
        cnt = ((rvals>=lo)&(rvals<hi)).sum()
        print(f"    {tag:>8}: {cnt:>5} ({cnt/len(rvals)*100:>5.1f}%)")

print(f"\n{'='*100}")
