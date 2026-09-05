"""trailing stop细粒度扫描 + 与加仓策略组合测试。"""
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

def simulate_trailing(signal, tp_l=0.18, tp_s=0.12, trail_atr=1.0):
    """trailing stop: 第3K后止损=max(peak-N*ATR, 突破线, KAMA)"""
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
            "direction": d, "exit_reason": exit_reason}

def simulate_trailing_addon(signal, tp_l=0.18, tp_s=0.12, trail_atr=1.0,
                             addon_thresh=0.023, include_bk=True):
    """trailing stop + 加仓：加仓在首次实体突破2.3%时触发，加仓止损=突破线(保本)"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    peak = bt
    addon_done = False; addon_idx = -1; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
        # 加仓检测
        if not addon_done:
            if include_bk and bh == 1:
                cg = (closes[j] - bt) / bt if is_long else (bt - closes[j]) / bt
                if cg >= addon_thresh:
                    addon_done = True; addon_idx = j; addon_price = float(closes[j])
            elif bh > 1:
                cg = (closes[j] - bt) / bt if is_long else (bt - closes[j]) / bt
                if cg >= addon_thresh:
                    addon_done = True; addon_idx = j; addon_price = float(closes[j])
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
    r_base = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    r_addon = 0.0
    if addon_done:
        r_addon = (exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av
    return {"r_value": r_base + r_addon, "exit_idx": exit_idx, "hold_bars": exit_idx - i,
            "direction": d, "exit_reason": exit_reason, "addon": addon_done}

def run(sim_func, **kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = sim_func(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 100)
print("trailing stop 细粒度扫描")
print("=" * 100)

base = run(simulate_trailing, trail_atr=999)  # 无trailing=基准
print(f"\n基准(无trailing): {len(base)}单, 累计R={base['r_value'].sum():.1f}, 均R={base['r_value'].mean():.2f}, 胜率={(base['r_value']>0).mean():.0%}")

print(f"\n{'N*ATR':>6} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6} {'止盈%':>6} {'止损%':>6} {'单数':>6}")
print("-" * 65)
for trail in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5]:
    rdf = run(simulate_trailing, trail_atr=trail)
    vs = rdf["r_value"].sum() - base["r_value"].sum()
    tp_pct = (rdf[rdf['exit_reason']=='tp']).shape[0] / len(rdf)
    sl_pct = (rdf[rdf['exit_reason']=='stop']).shape[0] / len(rdf)
    print(f"{trail:>5.2f}x {rdf['r_value'].sum():>8.1f} {vs:>+8.1f} {rdf['r_value'].mean():>6.2f} {(rdf['r_value']>0).mean():>5.0%} {tp_pct:>5.0%} {sl_pct:>5.0%} {len(rdf):>6}")

print(f"\n{'='*100}")
print("trailing stop + 加仓策略组合")
print("=" * 100)

# 基准加仓(无trailing)
base_addon = run(simulate_trailing_addon, trail_atr=999, addon_thresh=0.023, include_bk=True)
print(f"\n基准加仓(无trailing): {len(base_addon)}单, 累计R={base_addon['r_value'].sum():.1f}, 均R={base_addon['r_value'].mean():.2f}")

# trailing基准(无加仓)
base_trail = run(simulate_trailing, trail_atr=1.0)
print(f"基准trailing(无加仓): {len(base_trail)}单, 累计R={base_trail['r_value'].sum():.1f}, 均R={base_trail['r_value'].mean():.2f}")

print(f"\n{'N*ATR':>6} {'累计R':>8} {'vs基准':>8} {'vs加仓':>8} {'vsTrail':>9} {'均R':>6} {'胜率':>6} {'加仓单':>6}")
print("-" * 70)
for trail in [0.5, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5]:
    rdf = run(simulate_trailing_addon, trail_atr=trail, addon_thresh=0.023, include_bk=True)
    vs_base = rdf["r_value"].sum() - base["r_value"].sum()
    vs_addon = rdf["r_value"].sum() - base_addon["r_value"].sum()
    vs_trail = rdf["r_value"].sum() - base_trail["r_value"].sum()
    addon_n = rdf['addon'].sum() if 'addon' in rdf.columns else 0
    print(f"{trail:>5.2f}x {rdf['r_value'].sum():>8.1f} {vs_base:>+8.1f} {vs_addon:>+8.1f} {vs_trail:>+9.1f} {rdf['r_value'].mean():>6.2f} {(rdf['r_value']>0).mean():>5.0%} {int(addon_n):>6}")

# 多空分拆
print(f"\n{'='*100}")
print("trailing 1.0x 多空分拆")
print("=" * 100)
rdf = run(simulate_trailing, trail_atr=1.0)
for d in ["long", "short"]:
    sub = rdf[rdf['direction']==d]
    print(f"  {d}: {len(sub)}单, 累计R={sub['r_value'].sum():.1f}, 均R={sub['r_value'].mean():.2f}, 胜率={(sub['r_value']>0).mean():.0%}")

# 年度稳定性
print(f"\n{'='*100}")
print("trailing 1.0x 年度稳定性")
print("=" * 100)
rdf = run(simulate_trailing, trail_atr=1.0)
# 需要关联时间
sig_map = {}
last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    sig_map[s.idx] = s
    # 重新模拟获取exit_idx
rdf2 = run(simulate_trailing, trail_atr=1.0)
# 用exit_idx关联年份
dates = df['open_time'].dt.year.to_numpy()
yearly = {}
last_exit = -1
ri = 0
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate_trailing(s, trail_atr=1.0)
    if r:
        yr = dates[r['exit_idx']]
        yearly.setdefault(yr, []).append(r['r_value'])
        last_exit = r['exit_idx']
print(f"\n{'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6}")
print("-" * 40)
for yr in sorted(yearly.keys()):
    rs = yearly[yr]
    print(f"{yr:>6} {len(rs):>6} {sum(rs):>8.1f} {np.mean(rs):>6.2f} {(np.array(rs)>0).mean():>5.0%}")

print(f"\n{'='*100}")
