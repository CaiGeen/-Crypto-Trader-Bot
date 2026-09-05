"""拆解v1 vs v2差异来源：止损线出场 vs 开盘价出场 vs 收盘价出场"""
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

def simulate(signal, trail_atr=0.5, exit_price_mode="stop_line"):
    """
    exit_price_mode:
      "stop_line"  = 止损线出场（原版v1，乐观）
      "open_price" = 开盘价出场（实盘跳空场景）
      "close_3k"   = 第3K收盘平仓（v2）
    """
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

        # v2: 第3K收盘检查（用trailing stop线，不是init_stop）
        if exit_price_mode == "close_3k" and bh == 3:
            trail_sl = max(peak - trail_atr * av, bt, ks) if is_long else min(peak + trail_atr * av, bt, ks)
            if is_long and closes[j] < trail_sl:
                return {"r": (closes[j]-bt)/av, "exit_idx": j, "exit_price": float(closes[j]),
                        "reason": "3k_close", "hold": bh, "sig_idx": i}
            if not is_long and closes[j] > trail_sl:
                return {"r": (bt-closes[j])/av, "exit_idx": j, "exit_price": float(closes[j]),
                        "reason": "3k_close", "hold": bh, "sig_idx": i}

        # 止损检查
        if is_long and lows[j] < sl:
            if exit_price_mode == "open_price" and opens[j] < sl:
                ep = float(opens[j])  # 开盘跳空，用开盘价
                reason = "gap_open"
            else:
                ep = float(sl)  # 止损线
                reason = "trailing_stop"
            r = (ep-bt)/av if is_long else (bt-ep)/av
            return {"r": r, "exit_idx": j, "exit_price": ep, "reason": reason, "hold": bh, "sig_idx": i}
        if not is_long and highs[j] > sl:
            if exit_price_mode == "open_price" and opens[j] > sl:
                ep = float(opens[j])
                reason = "gap_open"
            else:
                ep = float(sl)
                reason = "trailing_stop"
            r = (bt-ep)/av if not is_long else (ep-bt)/av
            return {"r": r, "exit_idx": j, "exit_price": ep, "reason": reason, "hold": bh, "sig_idx": i}

        # 止盈
        if is_long and highs[j] >= tp_price:
            return {"r": (tp_price-bt)/av, "exit_idx": j, "exit_price": float(tp_price),
                    "reason": "tp", "hold": bh, "sig_idx": i}
        if not is_long and lows[j] <= tp_price:
            return {"r": (bt-tp_price)/av, "exit_idx": j, "exit_price": float(tp_price),
                    "reason": "tp", "hold": bh, "sig_idx": i}

        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])

    return {"r": (closes[-1]-bt)/av if is_long else (bt-closes[-1])/av,
            "exit_idx": n-1, "exit_price": float(closes[-1]), "reason": "end", "hold": n-1-i, "sig_idx": i}

def run(mode):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, exit_price_mode=mode)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

v1_stop = run("stop_line")    # v1: 止损线出场（乐观）
v1_open = run("open_price")   # v1b: 开盘价出场（实盘跳空）
v2_close = run("close_3k")    # v2: 第3K收盘平仓

print("=" * 100)
print("v1 vs v2 差异来源拆解")
print("=" * 100)

print(f"\n{'方案':>35} {'累计R':>8} {'均R':>6} {'胜率':>6} {'单数':>6}")
print("-" * 65)
for label, rdf in [("v1: 止损线出场（回测乐观）", v1_stop),
                    ("v1b: 开盘价出场（实盘跳空）", v1_open),
                    ("v2: 第3K收盘平仓", v2_close)]:
    print(f"{label:>35} {rdf['r'].sum():>8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(rdf):>6}")

print(f"\n差异拆解:")
print(f"  v1(止损线) - v1b(开盘价) = {v1_stop['r'].sum()-v1_open['r'].sum():.1f}R  ← 回测乐观假设的虚高")
print(f"  v1b(开盘价) - v2(收盘价) = {v1_open['r'].sum()-v2_close['r'].sum():.1f}R  ← 真实策略差异")

# v1b出场原因分布
print(f"\nv1b出场原因分布:")
for reason in v1_open['reason'].unique():
    sub = v1_open[v1_open['reason']==reason]
    print(f"  {reason}: {len(sub)}单 ({len(sub)/len(v1_open)*100:.0f}%), 累计R={sub['r'].sum():.1f}, 均R={sub['r'].mean():.2f}")

# 对比v1和v1b中跳空出场的单子
gap = v1_open[v1_open['reason']=='gap_open']
print(f"\n跳空出场单子: {len(gap)}单")
print(f"  v1(止损线): 累计R={v1_stop[v1_stop['sig_idx'].isin(gap['sig_idx'])]['r'].sum():.1f}")
print(f"  v1b(开盘价): 累计R={gap['r'].sum():.1f}")
print(f"  差异: {v1_stop[v1_stop['sig_idx'].isin(gap['sig_idx'])]['r'].sum()-gap['r'].sum():.1f}R（回测虚高）")

print(f"\n{'='*100}")
