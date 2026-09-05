"""持仓管理优化实验：在保本机制之外寻找减少亏损+不错过大涨幅的方法。"""
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
    if bh <= 3:
        return init_stop
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

def simulate_partial_tp(signal, tp_l=0.18, tp_s=0.12, partial_tp=0.06, partial_ratio=0.5):
    """方案A：分批止盈。涨到partial_tp先平(1-ratio)，剩余ratio继续KAMA+固定止盈"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l); pp = bt * (1 + partial_tp)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s); pp = bt * (1 - partial_tp)
    position = 1.0; partial_r = 0.0; partial_done = False
    exit_idx = exit_price = exit_reason = None; total_r = 0
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = get_stop_line(bh, init_stop, ks, bt, is_long)
        if not partial_done:
            if is_long and highs[j] >= pp:
                partial_r += (1 - partial_ratio) * ((pp - bt) / av)
                position = partial_ratio; partial_done = True
            elif not is_long and lows[j] <= pp:
                partial_r += (1 - partial_ratio) * ((bt - pp) / av)
                position = partial_ratio; partial_done = True
        if is_long and lows[j] < sl:
            total_r = partial_r + position * ((sl - bt) / av); exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if not is_long and highs[j] > sl:
            total_r = partial_r + position * ((bt - sl) / av); exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if is_long and highs[j] >= tp_price:
            total_r = partial_r + position * ((tp_price - bt) / av); exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price:
            total_r = partial_r + position * ((bt - tp_price) / av); exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
    if exit_idx is None:
        exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
        total_r = partial_r + position * ((exit_price - bt) / av if is_long else (bt - exit_price) / av)
    return {"r_value": total_r, "exit_idx": exit_idx, "hold_bars": exit_idx - i}

def simulate_trailing(signal, tp_l=0.18, tp_s=0.12, trail_atr=2.0):
    """方案B：移动保本线。第3K后保本线=peak-N*ATR，不低于突破线，与KAMA取更紧"""
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
    return {"r_value": r, "exit_idx": exit_idx, "hold_bars": exit_idx - i}

def simulate_step_stop(signal, tp_l=0.18, tp_s=0.12):
    """方案C：阶梯止损。涨0-2%保本，2-4%锁1%，4-6%锁2%，6%+锁3%"""
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
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "hold_bars": exit_idx - i}

def simulate_reduce_3k(signal, tp_l=0.18, tp_s=0.12, reduce_thresh=0.01, reduce_ratio=0.5):
    """方案D：第3K减仓。第3K涨幅<thresh则减仓(1-ratio)，剩余ratio保本+KAMA"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    position = 1.0; reduced_r = 0.0; reduced = False
    exit_idx = exit_price = exit_reason = None; total_r = 0
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = get_stop_line(bh, init_stop, ks, bt, is_long)
        if not reduced and bh == 3:
            cg = (closes[j] - bt) / bt if is_long else (bt - closes[j]) / bt
            if cg < reduce_thresh:
                rp = float(closes[j])
                reduced_r += (1 - reduce_ratio) * ((rp - bt) / av if is_long else (bt - rp) / av)
                position = reduce_ratio
            reduced = True
        if is_long and lows[j] < sl:
            total_r = reduced_r + position * ((sl - bt) / av); exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if not is_long and highs[j] > sl:
            total_r = reduced_r + position * ((bt - sl) / av); exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if is_long and highs[j] >= tp_price:
            total_r = reduced_r + position * ((tp_price - bt) / av); exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price:
            total_r = reduced_r + position * ((bt - tp_price) / av); exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
    if exit_idx is None:
        exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
        total_r = reduced_r + position * ((exit_price - bt) / av if is_long else (bt - exit_price) / av)
    return {"r_value": total_r, "exit_idx": exit_idx, "hold_bars": exit_idx - i}

def run(sim_func, **kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = sim_func(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 110)
print("持仓管理优化实验")
print("=" * 110)

base = run(simulate_base)
print(f"\n基准: {len(base)}单, 累计R={base['r_value'].sum():.1f}, 均R={base['r_value'].mean():.2f}")

# 方案A
print(f"\n{'='*110}")
print("方案A：分批止盈（涨到X%先平Y%，剩余继续KAMA+固定止盈）")
print(f"{'='*110}")
print(f"\n{'部分止盈':>8} {'平仓比例':>8} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6}")
print("-" * 50)
for ptp in [0.04, 0.06, 0.08, 0.10]:
    for ratio in [0.5, 0.3]:
        rdf = run(simulate_partial_tp, partial_tp=ptp, partial_ratio=ratio)
        vs = rdf["r_value"].sum() - base["r_value"].sum()
        print(f"{ptp:>7.0%} {1-ratio:>7.0%} {rdf['r_value'].sum():>8.1f} {vs:>+8.1f} {rdf['r_value'].mean():>6.2f} {(rdf['r_value']>0).mean():>5.0%}")

# 方案B
print(f"\n{'='*110}")
print("方案B：移动保本线（第3K后保本线=peak-N*ATR，不低于突破线，与KAMA取更紧）")
print(f"{'='*110}")
print(f"\n{'N*ATR':>6} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6}")
print("-" * 40)
for trail in [1.0, 1.5, 2.0, 2.5, 3.0]:
    rdf = run(simulate_trailing, trail_atr=trail)
    vs = rdf["r_value"].sum() - base["r_value"].sum()
    print(f"{trail:>5.1f}x {rdf['r_value'].sum():>8.1f} {vs:>+8.1f} {rdf['r_value'].mean():>6.2f} {(rdf['r_value']>0).mean():>5.0%}")

# 方案C
print(f"\n{'='*110}")
print("方案C：阶梯止损（涨0-2%保本，2-4%锁1%，4-6%锁2%，6%+锁3%）")
print(f"{'='*110}")
rdf = run(simulate_step_stop)
vs = rdf["r_value"].sum() - base["r_value"].sum()
print(f"\n  累计R={rdf['r_value'].sum():.1f}, vs基准={vs:+.1f}, 均R={rdf['r_value'].mean():.2f}, 胜率={(rdf['r_value']>0).mean():.0%}")

# 方案D
print(f"\n{'='*110}")
print("方案D：第3K减仓（第3K涨幅<X%则减仓50%，剩余保本+KAMA）")
print(f"{'='*110}")
print(f"\n{'阈值':>6} {'减仓比例':>8} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6}")
print("-" * 50)
for thresh in [0.005, 0.01, 0.015, 0.02]:
    for ratio in [0.5, 0.3]:
        rdf = run(simulate_reduce_3k, reduce_thresh=thresh, reduce_ratio=ratio)
        vs = rdf["r_value"].sum() - base["r_value"].sum()
        print(f"{thresh:>5.1%} {1-ratio:>7.0%} {rdf['r_value'].sum():>8.1f} {vs:>+8.1f} {rdf['r_value'].mean():>6.2f} {(rdf['r_value']>0).mean():>5.0%}")

# 汇总
print(f"\n{'='*110}")
print("汇总对比")
print(f"{'='*110}")
print(f"\n{'方案':>35} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6}")
print("-" * 65)
print(f"{'基准':>35} {base['r_value'].sum():>8.1f} {'—':>8} {base['r_value'].mean():>6.2f} {(base['r_value']>0).mean():>5.0%}")
for label, rdf in [
    ("A: 6%平50%分批止盈", run(simulate_partial_tp, partial_tp=0.06, partial_ratio=0.5)),
    ("A: 8%平50%分批止盈", run(simulate_partial_tp, partial_tp=0.08, partial_ratio=0.5)),
    ("A: 6%平70%分批止盈", run(simulate_partial_tp, partial_tp=0.06, partial_ratio=0.3)),
    ("A: 4%平50%分批止盈", run(simulate_partial_tp, partial_tp=0.04, partial_ratio=0.5)),
    ("B: trailing 2.0x ATR", run(simulate_trailing, trail_atr=2.0)),
    ("B: trailing 1.5x ATR", run(simulate_trailing, trail_atr=1.5)),
    ("B: trailing 1.0x ATR", run(simulate_trailing, trail_atr=1.0)),
    ("C: 阶梯止损", run(simulate_step_stop)),
    ("D: 第3K<1%减50%", run(simulate_reduce_3k, reduce_thresh=0.01, reduce_ratio=0.5)),
    ("D: 第3K<2%减50%", run(simulate_reduce_3k, reduce_thresh=0.02, reduce_ratio=0.5)),
]:
    vs = rdf["r_value"].sum() - base["r_value"].sum()
    print(f"{label:>35} {rdf['r_value'].sum():>8.1f} {vs:>+8.1f} {rdf['r_value'].mean():>6.2f} {(rdf['r_value']>0).mean():>5.0%}")

print(f"\n{'='*110}")
