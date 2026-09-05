"""测试第3K收盘检查：如果收盘价已低于peak-0.5ATR，直接收盘平仓"""
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
times = df['open_time']

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

def simulate_trailing_v1(signal, trail_atr=0.5):
    """原版：第4K起用trailing stop，不检查第3K收盘"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * 1.18
    else: init_stop = bt + av; tp_price = bt * 0.88
    peak = bt
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(peak - trail_atr * av, bt, ks) if is_long else min(peak + trail_atr * av, bt, ks)
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "trailing_stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "trailing_stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "exit_price": exit_price,
            "exit_reason": exit_reason, "hold_bars": exit_idx - i}

def simulate_trailing_v2(signal, trail_atr=0.5):
    """改进版：第3K收盘时检查，若收盘价已低于peak-0.5ATR则直接收盘平仓"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * 1.18
    else: init_stop = bt + av; tp_price = bt * 0.88
    peak = bt
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(peak - trail_atr * av, bt, ks) if is_long else min(peak + trail_atr * av, bt, ks)
        # 第3K收盘检查
        if bh == 3:
            if is_long:
                trail_check = peak - trail_atr * av
                if closes[j] < max(trail_check, bt, ks):
                    exit_idx, exit_price, exit_reason = j, float(closes[j]), "3k_close_exit"
                    break
            else:
                trail_check = peak + trail_atr * av
                if closes[j] > min(trail_check, bt, ks):
                    exit_idx, exit_price, exit_reason = j, float(closes[j]), "3k_close_exit"
                    break
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "trailing_stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "trailing_stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "exit_price": exit_price,
            "exit_reason": exit_reason, "hold_bars": exit_idx - i}

def simulate_trailing_v3(signal, trail_atr=0.5):
    """更激进版：每根K线收盘都检查，若收盘价低于trailing stop则收盘平仓"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * 1.18
    else: init_stop = bt + av; tp_price = bt * 0.88
    peak = bt
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(peak - trail_atr * av, bt, ks) if is_long else min(peak + trail_atr * av, bt, ks)
        # 先检查盘中止损/止盈
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "trailing_stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "trailing_stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        # 收盘检查：bh>3时，若收盘价低于trailing stop则收盘平仓
        if bh > 3:
            if is_long and closes[j] < sl:
                exit_idx, exit_price, exit_reason = j, float(closes[j]), "close_exit"
                break
            if not is_long and closes[j] > sl:
                exit_idx, exit_price, exit_reason = j, float(closes[j]), "close_exit"
                break
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "exit_price": exit_price,
            "exit_reason": exit_reason, "hold_bars": exit_idx - i}

def run(sim_func, **kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = sim_func(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 100)
print("第3K收盘检查对比（trailing 0.5x，无未来函数）")
print("=" * 100)

v1 = run(simulate_trailing_v1)
v2 = run(simulate_trailing_v2)
v3 = run(simulate_trailing_v3)

print(f"\n{'方案':>30} {'累计R':>8} {'均R':>6} {'胜率':>6} {'单数':>6}")
print("-" * 60)
for label, rdf in [("v1: 原版（第4K起trailing）", v1),
                    ("v2: 第3K收盘检查后平仓", v2),
                    ("v3: 每根收盘都检查", v3)]:
    print(f"{label:>30} {rdf['r_value'].sum():>8.1f} {rdf['r_value'].mean():>6.2f} {(rdf['r_value']>0).mean():>5.0%} {len(rdf):>6}")

# v2出场原因分布
print(f"\nv2出场原因分布:")
for reason in v2['exit_reason'].unique():
    sub = v2[v2['exit_reason']==reason]
    print(f"  {reason}: {len(sub)}单 ({len(sub)/len(v2)*100:.0f}%), 均R={sub['r_value'].mean():.2f}")

# v3出场原因分布
print(f"\nv3出场原因分布:")
for reason in v3['exit_reason'].unique():
    sub = v3[v3['exit_reason']==reason]
    print(f"  {reason}: {len(sub)}单 ({len(sub)/len(v3)*100:.0f}%), 均R={sub['r_value'].mean():.2f}")

# v2中3k_close_exit的单子，对比v1中同样信号的结果
print(f"\n{'='*100}")
print("v2中第3K收盘平仓的单子 vs v1中同样信号")
print("=" * 100)
# 重新跑，记录信号idx
def run_with_idx(sim_func, **kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = sim_func(s, **kwargs)
        if r: r["sig_idx"] = s.idx; results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

v1i = run_with_idx(simulate_trailing_v1)
v2i = run_with_idx(simulate_trailing_v2)

v2_3k = v2i[v2i['exit_reason']=='3k_close_exit']
v1_match = v1i[v1i['sig_idx'].isin(v2_3k['sig_idx'])]

print(f"\n  第3K收盘平仓: {len(v2_3k)}单")
print(f"    v2(收盘平仓): 累计R={v2_3k['r_value'].sum():.1f}, 均R={v2_3k['r_value'].mean():.2f}")
print(f"    v1(第4K止损): 累计R={v1_match['r_value'].sum():.1f}, 均R={v1_match['r_value'].mean():.2f}")
print(f"    差异: {v2_3k['r_value'].sum()-v1_match['r_value'].sum():+.1f}R")

# 看几个具体例子
print(f"\n  具体例子（前10单）:")
print(f"  {'信号idx':>8} {'v1出场价':>10} {'v1持仓K':>6} {'v1R':>6} │ {'v2出场价':>10} {'v2持仓K':>6} {'v2R':>6} │ {'差异':>6}")
for _, row in v2_3k.head(10).iterrows():
    v1r = v1i[v1i['sig_idx']==row['sig_idx']].iloc[0]
    print(f"  {row['sig_idx']:>8} {v1r['exit_price']:>10.1f} {v1r['hold_bars']:>6} {v1r['r_value']:>6.2f} │ {row['exit_price']:>10.1f} {row['hold_bars']:>6} {row['r_value']:>6.2f} │ {row['r_value']-v1r['r_value']:>+6.2f}")

print(f"\n{'='*100}")
