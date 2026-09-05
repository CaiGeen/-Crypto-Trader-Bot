"""全面检查止损线出场 vs 开盘价出场：基准策略和trailing stop"""
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

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

def simulate(signal, strategy="base", trail_atr=0.5, exit_mode="stop_line"):
    """
    strategy: "base" = 固定止盈+KAMA, "trailing" = trailing stop 0.5x
    exit_mode: "stop_line" = 止损线出场(乐观), "open_price" = 开盘价出场(实盘)
    """
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * 1.18
    else: init_stop = bt + av; tp_price = bt * 0.88
    peak = bt
    exit_idx = exit_price = exit_reason = None; exit_bh = 0
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if strategy == "base":
            if bh <= 3: sl = init_stop
            else: sl = max(ks, bt) if is_long else min(ks, bt)
        else:  # trailing
            if bh <= 3: sl = init_stop
            else: sl = max(peak - trail_atr * av, bt, ks) if is_long else min(peak + trail_atr * av, bt, ks)

        # 止损检查
        if is_long and lows[j] < sl:
            if exit_mode == "open_price" and opens[j] < sl:
                ep = float(opens[j]); reason = "gap_open"
            else:
                ep = float(sl); reason = "stop"
            r = (ep-bt)/av
            return {"r": r, "exit_idx": j, "hold": bh, "reason": reason,
                    "bh": bh, "pre3k": bh<=3, "sig_idx": i}
        if not is_long and highs[j] > sl:
            if exit_mode == "open_price" and opens[j] > sl:
                ep = float(opens[j]); reason = "gap_open"
            else:
                ep = float(sl); reason = "stop"
            r = (bt-ep)/av
            return {"r": r, "exit_idx": j, "hold": bh, "reason": reason,
                    "bh": bh, "pre3k": bh<=3, "sig_idx": i}

        # 止盈
        if is_long and highs[j] >= tp_price:
            return {"r": (tp_price-bt)/av, "exit_idx": j, "hold": bh, "reason": "tp",
                    "bh": bh, "pre3k": bh<=3, "sig_idx": i}
        if not is_long and lows[j] <= tp_price:
            return {"r": (bt-tp_price)/av, "exit_idx": j, "hold": bh, "reason": "tp",
                    "bh": bh, "pre3k": bh<=3, "sig_idx": i}

        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])

    return {"r": (closes[-1]-bt)/av if is_long else (bt-closes[-1])/av,
            "exit_idx": n-1, "hold": n-1-i, "reason": "end", "bh": n-1-i,
            "pre3k": False, "sig_idx": i}

def run(strategy, exit_mode):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, strategy=strategy, exit_mode=exit_mode)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 110)
print("全面检查：止损线出场 vs 开盘价出场")
print("=" * 110)

for strat, name in [("base", "基准（固定止盈+KAMA）"), ("trailing", "trailing stop 0.5x")]:
    sl = run(strat, "stop_line")   # 止损线出场（乐观）
    op = run(strat, "open_price")  # 开盘价出场（实盘）

    print(f"\n{'='*110}")
    print(f"{name}")
    print(f"{'='*110}")
    print(f"\n  {'方式':>20} {'累计R':>8} {'均R':>6} {'胜率':>6} {'单数':>6}")
    print(f"  {'-'*50}")
    print(f"  {'止损线(回测乐观)':>20} {sl['r'].sum():>8.1f} {sl['r'].mean():>6.2f} {(sl['r']>0).mean():>5.0%} {len(sl):>6}")
    print(f"  {'开盘价(实盘真实)':>20} {op['r'].sum():>8.1f} {op['r'].mean():>6.2f} {(op['r']>0).mean():>5.0%} {len(op):>6}")
    print(f"  {'差异(虚高)':>20} {sl['r'].sum()-op['r'].sum():>+8.1f}")

    # 前3K vs 第3K后拆解
    print(f"\n  前3K止损 vs 第3K后止损拆解:")
    print(f"  {'':>20} {'止损线R':>8} {'开盘价R':>8} {'差异':>8} {'跳空单':>6} {'跳空占比':>8}")
    print(f"  {'-'*65}")

    for pre3k, label in [(True, "前3K止损"), (False, "第3K后止损")]:
        sl_sub = sl[(sl['pre3k']==pre3k) & (sl['reason'].isin(['stop','gap_open']))]
        op_sub = op[(op['pre3k']==pre3k) & (op['reason'].isin(['stop','gap_open']))]
        gap_n = (op_sub['reason']=='gap_open').sum()
        print(f"  {label:>20} {sl_sub['r'].sum():>8.1f} {op_sub['r'].sum():>8.1f} {sl_sub['r'].sum()-op_sub['r'].sum():>+8.1f} {gap_n:>6} {gap_n/len(sl_sub)*100 if len(sl_sub)>0 else 0:>7.0f}%")

    # 止盈单不受影响
    tp_sl = sl[sl['reason']=='tp']
    tp_op = op[op['reason']=='tp']
    print(f"  {'止盈':>20} {tp_sl['r'].sum():>8.1f} {tp_op['r'].sum():>8.1f} {0:>8.1f} {0:>6} {'':>8}")

    # 前3K止损的单子：R分布对比
    print(f"\n  前3K止损单R分布:")
    sl_pre3 = sl[(sl['pre3k']) & (sl['reason'].isin(['stop','gap_open']))]
    op_pre3 = op[(op['pre3k']) & (op['reason'].isin(['stop','gap_open']))]
    for lo, hi, label in [(-99,-2,"R<-2"),(-2,-1.5,"-2~-1.5"),(-1.5,-1,"-1.5~-1"),(-1,-0.5,"-1~-0.5"),(-0.5,0,"-0.5~0")]:
        s_cnt = ((sl_pre3['r']>=lo)&(sl_pre3['r']<hi)).sum()
        o_cnt = ((op_pre3['r']>=lo)&(op_pre3['r']<hi)).sum()
        if s_cnt > 0 or o_cnt > 0:
            print(f"    {label:>12}: 止损线{s_cnt:>4}单, 开盘价{o_cnt:>4}单")

print(f"\n{'='*110}")
print("总结")
print("=" * 110)
base_sl = run("base", "stop_line")
base_op = run("base", "open_price")
trail_sl = run("trailing", "stop_line")
trail_op = run("trailing", "open_price")
print(f"""
  基准策略:
    止损线出场: {base_sl['r'].sum():.1f}R → 开盘价出场: {base_op['r'].sum():.1f}R (虚高{base_sl['r'].sum()-base_op['r'].sum():.1f}R)

  trailing stop 0.5x:
    止损线出场: {trail_sl['r'].sum():.1f}R → 开盘价出场: {trail_op['r'].sum():.1f}R (虚高{trail_sl['r'].sum()-trail_op['r'].sum():.1f}R)

  实盘真实对比:
    基准(开盘价): {base_op['r'].sum():.1f}R
    trailing(开盘价): {trail_op['r'].sum():.1f}R
    差异: {trail_op['r'].sum()-base_op['r'].sum():+.1f}R
""")
