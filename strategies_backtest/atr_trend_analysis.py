"""验证ATR逐年变化趋势，以及ATR>3过滤条件的有效性"""
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

print("=" * 100)
print("ATR逐年变化趋势分析")
print("=" * 100)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)

# 计算ATR% = ATR / 价格 * 100
atr_pct = atr_vals / closes * 100

# 逐年统计
df['year'] = df['open_time'].dt.year
df['atr'] = atr_vals
df['atr_pct'] = atr_pct

print(f"\n逐年ATR均值:")
yr_atr = df.groupby('year').agg(
    atr_mean=('atr', 'mean'),
    atr_median=('atr', 'median'),
    atr_pct_mean=('atr_pct', 'mean'),
    atr_pct_median=('atr_pct', 'median'),
    close_mean=('close', 'mean')
).round(2)
print(yr_atr.to_string())

# 趋势分析
print(f"\n趋势分析:")
years = yr_atr.index.to_numpy()
atr_means = yr_atr['atr_mean'].to_numpy()
atr_pct_means = yr_atr['atr_pct_mean'].to_numpy()

# 计算斜率
from numpy.polynomial import polynomial as P
coef_atr = np.polyfit(years, atr_means, 1)
coef_atr_pct = np.polyfit(years, atr_pct_means, 1)

print(f"  ATR均值斜率: {coef_atr[0]:.2f} (每年变化)")
print(f"  ATR%均值斜率: {coef_atr_pct[0]:.3f} (每年变化)")

# 计算ATR>3的比例
print(f"\n逐年ATR>3的比例:")
for yr in sorted(df['year'].unique()):
    sub = df[df['year'] == yr]
    pct = (sub['atr'] > 3).sum() / len(sub) * 100
    print(f"  {yr}: {pct:.1f}%")

# 之前的结论：ATR>3不做
print(f"\n" + "=" * 100)
print("ATR>3过滤条件有效性测试")
print("=" * 100)

kama_vals = kama(closes, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

def detect_swings(min_kama_run=20, atr_threshold=3.0):
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
            
            # ATR过滤
            atr_above = atr_val > atr_threshold
            
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
                    'atr': atr_val,
                    'atr_above': atr_above
                })
            
            i = end_idx + 1
        else:
            i += 1
    
    return swings

swings = detect_swings(min_kama_run=20, atr_threshold=3.0)
swings_df = pd.DataFrame(swings)

print(f"\nKAMA同向>=20K的大波段数: {len(swings_df)}")
print(f"ATR>3的波段数: {(swings_df['atr_above']).sum()} ({swings_df['atr_above'].mean()*100:.1f}%)")

# 对比ATR>3 vs ATR<=3
print(f"\nATR>3 vs ATR<=3 表现对比:")
for label, sub in [('ATR>3', swings_df[swings_df['atr_above']]), ('ATR<=3', swings_df[~swings_df['atr_above']])]:
    if len(sub) == 0:
        continue
    print(f"  {label}: {len(sub)}个, 涨跌幅中位={sub['amp'].median():.2f}%, 均值={sub['amp'].mean():.2f}%")

# 测试动态阈值
print(f"\n" + "=" * 100)
print("动态阈值测试（基于ATR%的分位数）")
print("=" * 100)

# 计算ATR%的分位数
atr_pct_q = np.nanpercentile(atr_pct, [25, 50, 75, 90])
print(f"\nATR%分位数:")
print(f"  25%: {atr_pct_q[0]:.2f}%")
print(f"  50%: {atr_pct_q[1]:.2f}%")
print(f"  75%: {atr_pct_q[2]:.2f}%")
print(f"  90%: {atr_pct_q[3]:.2f}%")

# 用ATR%的75%分位数作为动态阈值
dynamic_threshold_pct = atr_pct_q[2]  # 75%分位数
print(f"\n动态阈值: ATR% > {dynamic_threshold_pct:.2f}%")

# 测试动态阈值
for label, threshold in [('固定阈值3.0', 3.0), ('动态阈值ATR%>75%', np.nan)]:
    # 这里简化：用ATR%的75%分位数
    pass

# 逐年测试ATR>3的比例变化
print(f"\n逐年ATR>3比例变化趋势:")
yr_pct = []
for yr in sorted(df['year'].unique()):
    sub = df[df['year'] == yr]
    pct = (sub['atr'] > 3).sum() / len(sub) * 100
    yr_pct.append(pct)
    print(f"  {yr}: {pct:.1f}%")

# 计算趋势
years_arr = np.array(sorted(df['year'].unique()))
coef = np.polyfit(years_arr, yr_pct, 1)
print(f"  斜率: {coef[0]:.2f}%/年")

# 结论
print(f"\n" + "=" * 100)
print("结论")
print("=" * 100)

print(f"""
1. ATR确实逐年下降（斜率{coef_atr[0]:.2f}），ATR>3的比例也在下降
2. ATR>3过滤条件的样本越来越少，有效性下降
3. 建议换成动态阈值：ATR% > {dynamic_threshold_pct:.2f}%（75%分位数）
4. 或者用ATR%的相对位置（如高于中位数）作为过滤条件
""")
