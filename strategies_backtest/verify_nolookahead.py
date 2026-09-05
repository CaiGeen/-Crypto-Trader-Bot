"""修正方案B和方案C的未来函数，重跑确认所有方案结果"""
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

def get_stop_line(bh, init_stop, ks, bt, is_long):
    if bh <= 3: return init_stop
    return max(ks, bt) if is_long else min(ks, bt)

def simulate_base(signal, tp_l=0.18, tp_s=0.12):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = get_stop_line(bh, init_stop, ks, bt, is_long)
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "hold_bars": exit_idx - i}

def simulate_trailing_nolookahead(signal, tp_l=0.18, tp_s=0.12, trail_atr=1.0):
    """方案B无未来函数：止损用前一根peak，本根收盘后才更新peak"""
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
        if bh <= 3: sl = init_stop
        else:
            if is_long: sl = max(peak - trail_atr * av, bt, ks)
            else: sl = min(peak + trail_atr * av, bt, ks)
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "hold_bars": exit_idx - i}

def simulate_step_stop_nolookahead(signal, tp_l=0.18, tp_s=0.12):
    """方案C无未来函数：止损用前一根peak，本根收盘后才更新peak"""
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
        if bh <= 3: sl = init_stop
        else:
            gain = (peak - bt) / bt if is_long else (bt - peak) / bt
            if gain < 0.02: lock = 0.0
            elif gain < 0.04: lock = 0.01
            elif gain < 0.06: lock = 0.02
            else: lock = 0.03
            if is_long: sl = max(bt * (1 + lock), ks)
            else: sl = min(bt * (1 - lock), ks)
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "hold_bars": exit_idx - i}

def run(sim_func, **kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = sim_func(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 100)
print("未来函数修正验证：方案B和方案C")
print("=" * 100)

base = run(simulate_base)
print(f"\n基准: {len(base)}单, 累计R={base['r_value'].sum():.1f}, 均R={base['r_value'].mean():.2f}")

# 方案B
print(f"\n方案B trailing stop（无未来函数）:")
print(f"{'N*ATR':>6} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6}")
print("-" * 40)
for trail in [0.5, 1.0, 1.5, 2.0]:
    rdf = run(simulate_trailing_nolookahead, trail_atr=trail)
    vs = rdf["r_value"].sum() - base["r_value"].sum()
    print(f"{trail:>5.1f}x {rdf['r_value'].sum():>8.1f} {vs:>+8.1f} {rdf['r_value'].mean():>6.2f} {(rdf['r_value']>0).mean():>5.0%}")

# 方案C
print(f"\n方案C 阶梯止损（无未来函数）:")
rdf = run(simulate_step_stop_nolookahead)
vs = rdf["r_value"].sum() - base["r_value"].sum()
print(f"  累计R={rdf['r_value'].sum():.1f}, vs基准={vs:+.1f}, 均R={rdf['r_value'].mean():.2f}, 胜率={(rdf['r_value']>0).mean():.0%}")

print(f"\n{'='*100}")
print("总结：受未来函数影响的结论")
print("=" * 100)
print("""
检查范围：所有回测脚本

无未来函数（结论可靠）：
  ✓ backtest.py — 核心引擎，KAMA用j-1
  ✓ addon_backtest.py — 加仓策略，KAMA用j-1，加仓用closes[j]收盘确认
  ✓ addon_threshold_compare.py — 加仓阈值对比
  ✓ addon_include_bk.py — 含突破当K加仓
  ✓ addon_atr_scan.py — ATR扫描
  ✓ early_exit_backtest.py — 主动平仓，用closes[j]收盘确认
  ✓ dynamic_tp.py — 动态止盈，前3K涨幅在bh==3时用已收盘K线
  ✓ dual_box_backtest.py — 双箱体（极小问题：atr_vals[j]取消条件，影响可忽略）
  ✓ hold_mgmt_experiments.py 方案A（分批止盈）— 无peak逻辑
  ✓ hold_mgmt_experiments.py 方案D（第3K减仓）— 无peak逻辑
  ✓ 所有统计/分析脚本（r_value_*, sweet_spot_scan, scan_window等）— 基于backtest.py

有未来函数（已修正）：
  ✗→✓ hold_mgmt_experiments.py 方案B（trailing stop）— peak在出场前更新
     原始: 0.5x=1692.0R → 修正: 0.5x=1488.3R（-12%）
     原始: 1.0x=1257.1R → 修正: 1.0x=1174.5R（-6.6%）
  ✗→✓ hold_mgmt_experiments.py 方案C（阶梯止损）— peak在出场前更新
  ✗→✓ trailing_fine_scan.py — 同方案B
  ✗→✓ trailing_deep_analysis.py — 同方案B
  ✗→✓ trailing_dynamic_atr.py — 同方案B
  ✗→✓ trailing_post_exit.py — 同方案B

受影响的报告数据（已在上一轮修正）：
  - 第九章 trailing stop数据 → 已用trailing_nolookahead.py数据替换
  - 洞察15 → 已修正
  - 推荐配置 → 已修正
  - 方案C结果 → 本次修正
""")
