"""正确时序：6K区间[i-6,i-1]在i-1收盘形成，i开盘挂单，6根K内成交否则作废。
检查信号K内未成交的485单，在i+1到i+5内有多少能成交。
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
print("挂单有效期分析：信号K内未成交 → i+1到i+5内成交情况")
print("=" * 110)

unfilled_records = []
for s in signals:
    i = s.idx
    if i + 1 >= n4h: continue
    d = s.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: continue
    cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
    if cb_extreme is None: continue
    entry_base = cb_extreme

    # 信号K内是否成交
    filled_in_k = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    if filled_in_k: continue  # 只分析信号K内未成交的

    # 从i+1到i+5检查成交
    fill_k = None
    fill_price = None
    for k in range(i+1, min(i+6, n4h)):
        if is_long and lows[k] <= entry_base:
            fill_k = k
            fill_price = float(opens[k]) if opens[k] < entry_base else float(entry_base)
            break
        if not is_long and highs[k] >= entry_base:
            fill_k = k
            fill_price = float(opens[k]) if opens[k] > entry_base else float(entry_base)
            break

    unfilled_records.append({
        "sig_idx": i, "dir": d, "entry_base": entry_base,
        "fill_k": fill_k, "fill_delay": fill_k - i if fill_k else None,
        "fill_price": fill_price,
        "close_at_sig": closes[i],
        "gap_pct": (closes[i] - entry_base) / entry_base * 100 if is_long else (entry_base - closes[i]) / entry_base * 100
    })

rdf = pd.DataFrame(unfilled_records)
total = len(rdf)
filled_within_6 = rdf[rdf["fill_k"].notna()]
never_filled = rdf[rdf["fill_k"].isna()]

print(f"\n信号K内未成交的单: {total}")
print(f"  i+1~i+5内成交: {len(filled_within_6)} ({len(filled_within_6)/total:.0%})")
print(f"  6根K内仍未成交(作废): {len(never_filled)} ({len(never_filled)/total:.0%})")

if len(filled_within_6) > 0:
    print(f"\n{'━'*110}")
    print("成交延迟分布（信号K后第几根成交）")
    print(f"{'━'*110}")
    for delay in sorted(filled_within_6["fill_delay"].unique()):
        sub = filled_within_6[filled_within_6["fill_delay"] == delay]
        print(f"  +{delay}K: {len(sub):>4}单  跳空幅度均值={sub['gap_pct'].mean():.2f}%  成交价偏离entry_base均值={((sub['fill_price']-sub['entry_base'])/sub['entry_base']*100).mean():.3f}%")

print(f"\n{'━'*110}")
print("6根K内仍未成交的单（作废）")
print(f"{'━'*110}")
if len(never_filled) > 0:
    print(f"  {len(never_filled)}单")
    print(f"  跳空幅度: 均值={never_filled['gap_pct'].mean():.2f}%  中位={never_filled['gap_pct'].median():.2f}%")
    print(f"  方向分布: {never_filled['dir'].value_counts().to_dict()}")
