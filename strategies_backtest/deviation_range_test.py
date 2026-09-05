"""测试新口径下不同偏离度区间的表现

偏离度定义：(收盘价[i-1] - EMA70) / EMA70 * 100
之前的结论：[-2,2]和[4,6]区间做单效果最好
"""
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

# 计算日线EMA70
daily_df = pd.read_parquet(cfg.base_dir / 'cache' / 'BTCUSDT_1d_mark.parquet')
if 'close' in daily_df.columns:
    daily_df['close'] = daily_df['close'].astype(float)
k_ema = 2.0/(70+1)
de70 = np.zeros(len(daily_df))
de70[0] = daily_df['close'].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df['close'].iloc[i] * k_ema + de70[i-1] * (1 - k_ema)
date_to_ema = dict(zip(daily_df['open_time'].dt.date, de70))

print("=" * 100)
print("新口径下：偏离度区间表现测试")
print("=" * 100)

def detect_big_swings(min_kama_run=20):
    swings = []
    i = 1
    while i < n:
        if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            i += 1
            continue
        
        if kama_vals[i] > kama_vals[i-1]:
            kama_turn = kama_vals[i]
            atr_val = atr_vals[i-1]
            start_price = kama_turn + atr_val
            turn_idx = i
            
            # 计算偏离度
            prev_date = pd.to_datetime(times[i-1]).date()
            ema_prev = date_to_ema.get(prev_date, np.nan)
            dev = (closes[i-1] - ema_prev) / ema_prev * 100 if not np.isnan(ema_prev) and ema_prev > 0 else np.nan
            
            peak = closes[i]
            end_idx = i
            kama_run = 1
            
            for j in range(i+1, n):
                if np.isnan(kama_vals[j]) or np.isnan(kama_vals[j-1]):
                    break
                if kama_vals[j] < kama_vals[j-1]:
                    end_idx = j
                    break
                if closes[j] > peak:
                    peak = closes[j]
                kama_run += 1
                end_idx = j
            
            if kama_run >= min_kama_run:
                amp = (peak - start_price) / start_price * 100 if start_price > 0 else 0
                duration = end_idx - turn_idx
                
                swings.append({
                    'turn_idx': turn_idx,
                    'amp': amp,
                    'duration': duration,
                    'kama_run': kama_run,
                    'dev': dev,
                    'direction': '做多'
                })
            
            i = end_idx + 1
        else:
            i += 1
    
    return swings

swings = detect_big_swings(min_kama_run=20)
swings_df = pd.DataFrame(swings)

print(f"\nKAMA同向>=20K的大波段数: {len(swings_df)}")
print(f"有偏离度数据的波段数: {swings_df['dev'].notna().sum()}")

# 偏离度分桶统计
print(f"\n{'偏离度区间':>12} {'波段数':>6} {'涨跌幅中位':>10} {'涨跌幅均值':>10} {'持续K数中位':>10}")

bins = [(-10, -5), (-5, -2), (-2, 0), (0, 2), (2, 4), (4, 6), (6, 10), (10, 20)]
for lo, hi in bins:
    sub = swings_df[(swings_df['dev'] >= lo) & (swings_df['dev'] < hi)]
    if len(sub) == 0:
        continue
    print(f"  [{lo:>4},{hi:>4})    {len(sub):>5} {sub['amp'].median():>9.2f}% {sub['amp'].mean():>9.2f}% {sub['duration'].median():>9.0f}")

# 之前的结论：[-2,2]和[4,6]最好
print(f"\n" + "=" * 100)
print("验证之前的结论：[-2,2]和[4,6]区间做单效果最好")
print("=" * 100)

# 对比区间
key_ranges = {
    '[-2,2]': (-2, 2),
    '[4,6]': (4, 6),
    '[-5,-2]': (-5, -2),
    '[2,4]': (2, 4),
    '[6,10]': (6, 10),
    '[-10,-5]': (-10, -5),
}

print(f"\n{'偏离度区间':>12} {'波段数':>6} {'涨跌幅中位':>10} {'涨跌幅均值':>10} {'持续K数':>8}")
for label, (lo, hi) in key_ranges.items():
    sub = swings_df[(swings_df['dev'] >= lo) & (swings_df['dev'] < hi)]
    if len(sub) == 0:
        continue
    print(f"  {label:>10} {len(sub):>6} {sub['amp'].median():>9.2f}% {sub['amp'].mean():>9.2f}% {sub['duration'].median():>7.0f}")

# 整体对比
print(f"\n整体统计:")
print(f"  全部: {len(swings_df)}个, 涨跌幅中位={swings_df['amp'].median():.2f}%")
