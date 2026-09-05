"""检查时序问题：回调基准j成立后到信号K(i)之间，价格是否已回落到entry_base成交。
当前代码只检查信号K内lows[i]<=entry_base，可能漏掉j+3~i-1之间的成交。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from signal_detector import detect_breakout

cfg = BacktestConfig()
df_4h = load_klines(cfg)
signals = detect_breakout(df_4h, cfg.window)
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy()
n4h = len(df_4h)
times = df_4h["open_time"].tolist()

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)

def find_callback_extreme(i, direction, window=6):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n4h: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]: return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] >= lows[j]: return lows[j], j
    return None, None

print("=" * 110)
print("时序分析：回调基准成立后到信号K之间的成交情况")
print("=" * 110)

records = []
for s in signals:
    i = s.idx
    if i + 1 >= n4h: continue
    d = s.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: continue
    cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
    if cb_extreme is None: continue
    entry_base = cb_extreme

    # 回调基准在cb_idx形成，cb_idx+2收盘确认，cb_idx+3开盘可挂单
    hang_start = cb_idx + 3  # 最早挂单时间

    # 当前代码：只检查信号K内
    filled_in_sig_k = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)

    # 实盘：从hang_start到i之间，价格是否回落到entry_base
    filled_real = False
    first_fill_k = None
    for k in range(hang_start, i + 1):
        if k < 0 or k >= n4h: continue
        if is_long and lows[k] <= entry_base:
            filled_real = True; first_fill_k = k; break
        if not is_long and highs[k] >= entry_base:
            filled_real = True; first_fill_k = k; break

    # 成交发生在信号K之前（当前代码漏掉）
    filled_before_sig = filled_real and first_fill_k < i
    # 当前代码判未成交但实盘已成交
    missed = (not filled_in_sig_k) and filled_real

    records.append({
        "sig_idx": i, "dir": d, "cb_idx": cb_idx, "hang_start": hang_start,
        "entry_base": entry_base, "filled_in_sig_k": filled_in_sig_k,
        "filled_real": filled_real, "first_fill_k": first_fill_k,
        "filled_before_sig": filled_before_sig, "missed": missed,
        "gap_k": i - first_fill_k if first_fill_k else None
    })

rdf = pd.DataFrame(records)
total = len(rdf)

print(f"\n总信号(有回调基准): {total}")
print(f"\n{'━'*110}")
print("成交判定对比")
print(f"{'━'*110}")
print(f"  当前代码(只检查信号K内): 成交={rdf['filled_in_sig_k'].sum()}  未成交={(~rdf['filled_in_sig_k']).sum()}")
print(f"  实盘(从挂单到信号K):     成交={rdf['filled_real'].sum()}  未成交={(~rdf['filled_real']).sum()}")
print(f"\n  当前代码判'未成交'但实盘已成交(漏掉): {rdf['missed'].sum()}")
print(f"  成交发生在信号K之前(当前代码可能误判): {rdf['filled_before_sig'].sum()}")

# 详细看成交发生在信号K之前的分布
before = rdf[rdf['filled_before_sig']]
if len(before) > 0:
    print(f"\n{'━'*110}")
    print(f"成交发生在信号K之前的分布（{len(before)}单）")
    print(f"{'━'*110}")
    print(f"  成交提前K数分布:")
    print(f"  {'提前K数':>8} {'单数':>6} {'占比':>6}")
    for gap in sorted(before['gap_k'].unique()):
        cnt = (before['gap_k'] == gap).sum()
        print(f"  {gap:>8} {cnt:>6} {cnt/len(before):>5.0%}")

    # 这些单当前代码怎么处理的？
    print(f"\n  这些单在当前代码中的状态:")
    print(f"    判为成交(信号K内也到了entry_base): {before['filled_in_sig_k'].sum()}")
    print(f"    判为未成交(信号K内没到entry_base): {(~before['filled_in_sig_k']).sum()} → 这些被漏掉了")

# 如果用实盘逻辑，成交时间分布
filled_real = rdf[rdf['filled_real']]
print(f"\n{'━'*110}")
print(f"实盘成交时间分布（{len(filled_real)}单）")
print(f"{'━'*110}")
print(f"  {'成交位置':>12} {'单数':>6} {'占比':>6}")
sig_k = (filled_real['first_fill_k'] == filled_real['sig_idx']).sum()
before_k = (filled_real['first_fill_k'] < filled_real['sig_idx']).sum()
print(f"  {'信号K内':>12} {sig_k:>6} {sig_k/len(filled_real):>5.0%}")
print(f"  {'信号K之前':>12} {before_k:>6} {before_k/len(filled_real):>5.0%}")

# 提前成交的K数分布
print(f"\n  提前成交K数分布（相对信号K）:")
print(f"  {'提前K数':>8} {'单数':>6}")
for gap in sorted(filled_real['gap_k'].unique()):
    cnt = (filled_real['gap_k'] == gap).sum()
    if cnt >= 1:
        print(f"  {gap:>8} {cnt:>6}")
