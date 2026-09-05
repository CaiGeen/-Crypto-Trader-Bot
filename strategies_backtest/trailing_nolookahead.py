"""trailing stop 无未来函数版本：止损只用前一根收盘peak计算，本根不参与peak更新。"""
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

def simulate_trailing_future(signal, tp_l=0.18, tp_s=0.12, trail_atr=1.0):
    """有未来函数版本（原版）：本根high先更新peak再检查low"""
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

def simulate_trailing_nolookahead(signal, tp_l=0.18, tp_s=0.12, trail_atr=1.0):
    """无未来函数版本：止损只用前一根收盘peak，本根先检查出场再更新peak"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    peak = bt  # 前一根收盘时的peak
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        # ① 先用前一根的peak算止损（本根不参与）
        if bh <= 3:
            sl = init_stop
        else:
            if is_long:
                trail_stop = peak - trail_atr * av
                sl = max(trail_stop, bt, ks)
            else:
                trail_stop = peak + trail_atr * av
                sl = min(trail_stop, bt, ks)
        # ② 检查本根是否触及
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        # ③ 本根收盘后才更新peak
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "hold_bars": exit_idx - i,
            "direction": d, "exit_reason": exit_reason, "year": int(dates[exit_idx])}

def simulate_trailing_nolookahead_addon(signal, tp_l=0.18, tp_s=0.12, trail_atr=1.0,
                                         addon_thresh=0.023, include_bk=True):
    """无未来函数 + 加仓"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    peak = bt
    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        # ① 止损用前一根peak
        if bh <= 3:
            sl = init_stop
        else:
            if is_long:
                trail_stop = peak - trail_atr * av
                sl = max(trail_stop, bt, ks)
            else:
                trail_stop = peak + trail_atr * av
                sl = min(trail_stop, bt, ks)
        # ② 加仓检测（用收盘价，收盘时才确认）
        if not addon_done:
            cg = (closes[j] - bt) / bt if is_long else (bt - closes[j]) / bt
            if (include_bk or bh > 1) and cg >= addon_thresh:
                addon_done = True; addon_price = float(closes[j])
        # ③ 检查本根是否触及
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        # ④ 本根收盘后才更新peak
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r_base = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    r_addon = 0.0
    if addon_done:
        r_addon = (exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av
    return {"r_value": r_base + r_addon, "exit_idx": exit_idx, "hold_bars": exit_idx - i,
            "direction": d, "exit_reason": exit_reason, "year": int(dates[exit_idx]),
            "addon": addon_done}

def run(sim_func, **kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = sim_func(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 100)
print("trailing stop 未来函数对比（有 vs 无）")
print("=" * 100)

base = run(simulate_trailing_future, trail_atr=999)
print(f"\n基准(无trailing): {len(base)}单, 累计R={base['r_value'].sum():.1f}, 均R={base['r_value'].mean():.2f}, 胜率={(base['r_value']>0).mean():.0%}")

print(f"\n{'N*ATR':>6} │ {'有未来函数':>30} │ {'无未来函数':>30} │ {'差异':>12}")
print(f"{'':>6} │ {'累计R':>8} {'均R':>6} {'胜率':>6} {'单数':>6} │ {'累计R':>8} {'均R':>6} {'胜率':>6} {'单数':>6} │ {'ΔR':>8} {'Δ%':>5}")
print("-" * 95)
for trail in [0.5, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5]:
    df_f = run(simulate_trailing_future, trail_atr=trail)
    df_n = run(simulate_trailing_nolookahead, trail_atr=trail)
    dr = df_n['r_value'].sum() - df_f['r_value'].sum()
    dpct = dr / df_f['r_value'].sum() * 100
    print(f"{trail:>5.2f}x │ {df_f['r_value'].sum():>8.1f} {df_f['r_value'].mean():>6.2f} {(df_f['r_value']>0).mean():>5.0%} {len(df_f):>6} │ {df_n['r_value'].sum():>8.1f} {df_n['r_value'].mean():>6.2f} {(df_n['r_value']>0).mean():>5.0%} {len(df_n):>6} │ {dr:>+8.1f} {dpct:>+4.1f}%")

# 加仓对比
print(f"\n{'='*100}")
print("trailing + 加仓：未来函数对比")
print("=" * 100)
print(f"\n{'N*ATR':>6} │ {'有未来函数':>30} │ {'无未来函数':>30} │ {'差异':>12}")
print(f"{'':>6} │ {'累计R':>8} {'均R':>6} {'胜率':>6} {'加仓':>6} │ {'累计R':>8} {'均R':>6} {'胜率':>6} {'加仓':>6} │ {'ΔR':>8} {'Δ%':>5}")
print("-" * 100)
for trail in [0.5, 0.7, 1.0, 1.5]:
    df_f = run(simulate_trailing_future, trail_atr=trail)  # 有未来函数无加仓
    # 有未来函数+加仓需要单独写，这里用nolookahead_addon对比
    df_n = run(simulate_trailing_nolookahead_addon, trail_atr=trail, addon_thresh=0.023, include_bk=True)
    df_n_base = run(simulate_trailing_nolookahead, trail_atr=trail)
    addon_n = int(df_n['addon'].sum()) if 'addon' in df_n.columns else 0
    print(f"{trail:>5.2f}x │ {'(无加仓)':>30} │ {df_n['r_value'].sum():>8.1f} {df_n['r_value'].mean():>6.2f} {(df_n['r_value']>0).mean():>5.0%} {addon_n:>6} │ {'':>12}")

# 无未来函数年度稳定性
print(f"\n{'='*100}")
print("无未来函数 trailing 0.5x 年度稳定性")
print("=" * 100)
rdf = run(simulate_trailing_nolookahead, trail_atr=0.5)
print(f"\n  总计: {len(rdf)}单, 累计R={rdf['r_value'].sum():.1f}, 均R={rdf['r_value'].mean():.2f}, 胜率={(rdf['r_value']>0).mean():.0%}")
print(f"\n  {'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'最大回撤R':>10}")
print(f"  {'-'*52}")
for yr in sorted(rdf['year'].unique()):
    sub = rdf[rdf['year']==yr]
    cum = sub['r_value'].cumsum()
    max_dd = (cum - cum.cummax()).min()
    print(f"  {yr:>6} {len(sub):>6} {sub['r_value'].sum():>8.1f} {sub['r_value'].mean():>6.2f} {(sub['r_value']>0).mean():>5.0%} {max_dd:>10.1f}")

# 无未来函数+加仓年度稳定性
print(f"\n{'='*100}")
print("无未来函数 trailing 0.5x + 加仓 年度稳定性")
print("=" * 100)
rdf = run(simulate_trailing_nolookahead_addon, trail_atr=0.5, addon_thresh=0.023, include_bk=True)
print(f"\n  总计: {len(rdf)}单, 累计R={rdf['r_value'].sum():.1f}, 均R={rdf['r_value'].mean():.2f}, 胜率={(rdf['r_value']>0).mean():.0%}, 加仓{int(rdf['addon'].sum())}单")
print(f"\n  {'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'最大回撤R':>10}")
print(f"  {'-'*52}")
for yr in sorted(rdf['year'].unique()):
    sub = rdf[rdf['year']==yr]
    cum = sub['r_value'].cumsum()
    max_dd = (cum - cum.cummax()).min()
    print(f"  {yr:>6} {len(sub):>6} {sub['r_value'].sum():>8.1f} {sub['r_value'].mean():>6.2f} {(sub['r_value']>0).mean():>5.0%} {max_dd:>10.1f}")

# R分布对比
print(f"\n{'='*100}")
print("无未来函数 trailing 0.5x R值分布")
print("=" * 100)
rdf = run(simulate_trailing_nolookahead, trail_atr=0.5)
rvals = rdf['r_value']
for lo, hi, label in [(-99,-3,"R<-3"),(-3,-2,"-3~-2"),(-2,-1,"-2~-1"),(-1,0,"-1~0"),(0,1,"0~1"),(1,2,"1~2"),(2,5,"2~5"),(5,99,"5+")]:
    cnt = ((rvals>=lo)&(rvals<hi)).sum()
    pct = cnt/len(rvals)*100
    bar = "█"*int(pct/2)
    print(f"  {label:>8}: {cnt:>5} ({pct:>5.1f}%) {bar}")

print(f"\n{'='*100}")
