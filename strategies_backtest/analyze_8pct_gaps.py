"""分析涨跌幅>=8%的波段之间的间隔分布"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = BacktestConfig()

df = pd.read_parquet(cfg.cache_path)
for col in ['open','high','low','close']:
    if col in df.columns: df[col] = df[col].astype(float)
if 'open_time' not in df.columns:
    df['open_time'] = pd.to_datetime(df['open_time_ms'], unit='ms', utc=True)

closes = df['close'].to_numpy()
highs = df['high'].to_numpy()
lows = df['low'].to_numpy()
times = df['open_time'].tolist()
n = len(df)

kama_vals = kama(closes, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)

print("=" * 100)
print("涨跌幅>=8%的波段之间间隔分布")
print("=" * 100)

def detect_all_swings():
    swings = []
    for i in range(1, n):
        if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            continue
        if kama_vals[i] > kama_vals[i-1]:
            kama_turn = kama_vals[i]
            atr_val = atr_vals[i-1]
            breakout_level = kama_turn + atr_val
            for j in range(i, min(i+20, n)):
                if closes[j] >= breakout_level:
                    peak = closes[j]; end_idx = j
                    for k in range(j+1, min(j+50, n)):
                        if np.isnan(kama_vals[k]) or np.isnan(kama_vals[k-1]):
                            break
                        if kama_vals[k] < kama_vals[k-1]:
                            end_idx = k; break
                        if closes[k] > peak:
                            peak = closes[k]
                        end_idx = k
                    amp = (peak - breakout_level) / breakout_level * 100 if breakout_level > 0 else 0
                    duration = end_idx - j
                    swings.append({
                        'breakout_idx': j,
                        'breakout_time': times[j],
                        'amp': amp,
                        'K数': duration
                    })
                    break
    return swings

all_swings = detect_all_swings()
all_df = pd.DataFrame(all_swings)
all_df = all_df.sort_values('breakout_idx').reset_index(drop=True)

# 筛选>=8%
big_df = all_df[all_df['amp'] >= 8].copy()
print(f"\n涨跌幅>=8%的波段数: {len(big_df)}")

# 计算间隔（起点到起点）
if len(big_df) < 2:
    print("波段数不足，无法计算间隔")
else:
    gaps = []
    for i in range(1, len(big_df)):
        gap = big_df.iloc[i]['breakout_idx'] - big_df.iloc[i-1]['breakout_idx']
        gaps.append(gap)
    
    gaps = np.array(gaps)
    print(f"间隔样本数: {len(gaps)}")
    
    print(f"\n间隔统计:")
    print(f"  中位: {np.median(gaps):.0f}K")
    print(f"  均值: {np.mean(gaps):.1f}K")
    print(f"  最小: {np.min(gaps):.0f}K")
    print(f"  最大: {np.max(gaps):.0f}K")
    print(f"  P25: {np.percentile(gaps, 25):.0f}K")
    print(f"  P75: {np.percentile(gaps, 75):.0f}K")
    print(f"  P90: {np.percentile(gaps, 90):.0f}K")
    
    # 间隔分桶
    print(f"\n间隔分桶分布:")
    bins = [(0,5), (5,10), (10,15), (15,20), (20,30), (30,50), (50,100), (100,1000)]
    print(f"  {'间隔K数':>10} {'数量':>6} {'占比':>7} {'累计占比':>8}")
    
    total = len(gaps)
    cumulative = 0
    for lo, hi in bins:
        label = f"{lo}-{hi}" if hi < 1000 else "100+"
        count = sum(1 for g in gaps if lo <= g < hi)
        pct = count / total * 100
        cumulative += pct
        print(f"  [{lo:>3}-{hi:>3})    {count:>5} {pct:>6.1f}% {cumulative:>7.1f}%")
    
    # 不同涨跌幅区间的间隔对比
    print(f"\n不同涨跌幅区间的间隔对比:")
    for lo, hi, label in [(8,10,'8-10%'), (10,15,'10-15%'), (15,20,'15-20%'), (20,30,'20-30%'), (30,100,'30%+')]:
        sub = big_df[(big_df['amp'] >= lo) & (big_df['amp'] < hi)]
        if len(sub) < 2:
            print(f"  {label}: 样本不足")
            continue
        sub_gaps = [sub.iloc[i]['breakout_idx'] - sub.iloc[i-1]['breakout_idx'] for i in range(1, len(sub))]
        print(f"  {label}: 中位间隔{np.median(sub_gaps):.0f}K, 样本{len(sub)}个")
