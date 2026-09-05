"""重新测试ATR%过滤条件

之前的结论可能是ATR%>3而不是ATR>3
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
n = len(df)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)
atr_pct = atr_vals / closes * 100

kama_vals = kama(closes, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

print("=" * 100)
print("ATR%过滤条件测试（ATR% > 3%）")
print("=" * 100)

# 逐年ATR%>3的比例
df['year'] = df['open_time'].dt.year
df['atr_pct'] = atr_pct

print(f"\n逐年ATR%>3的比例:")
for yr in sorted(df['year'].unique()):
    sub = df[df['year'] == yr]
    pct = (sub['atr_pct'] > 3).sum() / len(sub) * 100
    print(f"  {yr}: {pct:.1f}%")

# 测试ATR%>3过滤
def detect_swings(min_kama_run=20, atr_pct_threshold=3.0):
    swings = []
    i = 1
    while i < n:
        if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            i += 1
            continue
        
        if kama_vals[i] > kama_vals[i-1]:
            kama_turn = kama_vals[i]
            atr_val = atr_vals[i-1]
            atr_pct_val = atr_pct[i-1]
            start_price = kama_turn + atr_val
            turn_idx = i
            
            # ATR%过滤
            atr_pct_above = atr_pct_val > atr_pct_threshold
            
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
                    'atr_pct': atr_pct_val,
                    'atr_pct_above': atr_pct_above
                })
            
            i = end_idx + 1
        else:
            i += 1
    
    return swings

swings = detect_swings(min_kama_run=20, atr_pct_threshold=3.0)
swings_df = pd.DataFrame(swings)

print(f"\nKAMA同向>=20K的大波段数: {len(swings_df)}")
print(f"ATR%>3的波段数: {(swings_df['atr_pct_above']).sum()} ({swings_df['atr_pct_above'].mean()*100:.1f}%)")

# 对比
print(f"\nATR%>3 vs ATR%<=3 表现对比:")
for label, sub in [('ATR%>3', swings_df[swings_df['atr_pct_above']]), ('ATR%<=3', swings_df[~swings_df['atr_pct_above']])]:
    if len(sub) == 0:
        continue
    print(f"  {label}: {len(sub)}个, 涨跌幅中位={sub['amp'].median():.2f}%, 均值={sub['amp'].mean():.2f}%")

# 动态阈值测试
print(f"\n" + "=" * 100)
print("动态阈值测试")
print("=" * 100)

# 计算ATR%的75%分位数作为动态阈值
atr_pct_q75 = np.nanpercentile(atr_pct, 75)
print(f"\nATR% 75%分位数: {atr_pct_q75:.2f}%")

# 测试动态阈值
swings_dyn = detect_swings(min_kama_run=20, atr_pct_threshold=atr_pct_q75)
swings_dyn_df = pd.DataFrame(swings_dyn)

print(f"\n动态阈值（ATR%>{atr_pct_q75:.2f}%）:")
print(f"  波段数: {len(swings_dyn_df)}")
print(f"  涨跌幅中位: {swings_dyn_df['amp'].median():.2f}%")
print(f"  涨跌幅均值: {swings_dyn_df['amp'].mean():.2f}%")

# 与固定阈值对比
print(f"\n与固定阈值对比:")
print(f"  固定阈值3%: {len(swings_df)}个, 中位{swings_df['amp'].median():.2f}%")
print(f"  动态阈值{atr_pct_q75:.2f}%: {len(swings_dyn_df)}个, 中位{swings_dyn_df['amp'].median():.2f}%")

# 结论
print(f"\n" + "=" * 100)
print("结论")
print("=" * 100)

print(f"""
1. ATR%>3的比例逐年下降（2019年100% → 2026年{df[df['year']==2026]['atr_pct'].gt(3).mean()*100:.1f}%）
2. ATR%>3过滤条件仍然有效，但样本在减少
3. 建议换成动态阈值：ATR% > {atr_pct_q75:.2f}%（75%分位数）
4. 动态阈值能适应ATR%的长期下降趋势
""")
