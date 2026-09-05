"""导出KAMA同向>=20K的大波段数据到CSV（修正版）

修正：
1. 按突破K索引去重，只保留涨跌幅最大的一条
2. 时间戳输出UTC+8
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

print("=" * 100)
print("导出KAMA同向>=20K的大波段数据（修正版）")
print("=" * 100)

def detect_swings(min_kama_run=20):
    swings = []
    for i in range(1, n):
        if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            continue
        if kama_vals[i] > kama_vals[i-1]:
            kama_turn = kama_vals[i]
            atr_val = atr_vals[i-1]
            breakout_level = kama_turn + atr_val
            
            kama_run = 1
            for j in range(i+1, min(i+100, n)):
                if np.isnan(kama_vals[j]) or np.isnan(kama_vals[j-1]):
                    break
                if kama_vals[j] < kama_vals[j-1]:
                    break
                kama_run += 1
            
            if kama_run < min_kama_run:
                continue
            
            for j in range(i, min(i+20, n)):
                if closes[j] >= breakout_level:
                    peak = closes[j]; peak_idx = j; end_idx = j
                    for k in range(j+1, min(j+50, n)):
                        if np.isnan(kama_vals[k]) or np.isnan(kama_vals[k-1]):
                            break
                        if kama_vals[k] < kama_vals[k-1]:
                            end_idx = k; break
                        if closes[k] > peak:
                            peak = closes[k]; peak_idx = k
                        end_idx = k
                    
                    amp = (peak - breakout_level) / breakout_level * 100 if breakout_level > 0 else 0
                    duration = end_idx - j
                    
                    swings.append({
                        '突破K索引': j,
                        '突破K时间': times[j],
                        '起点KAMA值': round(kama_turn, 2),
                        'ATR值': round(atr_val, 2),
                        '突破线价格': round(breakout_level, 2),
                        '峰值价格': round(peak, 2),
                        '峰值K索引': peak_idx,
                        '终点K索引': end_idx,
                        '涨跌幅%': round(amp, 2),
                        '持续K数': duration,
                        'KAMA同向运行根数': kama_run
                    })
                    break
    
    return swings

swings = detect_swings(min_kama_run=20)
swings_df = pd.DataFrame(swings)

# 按峰值K索引去重，保留涨跌幅最大的一条（同一波段只保留一条）
swings_df = swings_df.sort_values('涨跌幅%', ascending=False).drop_duplicates(subset='峰值K索引', keep='first')
swings_df = swings_df.sort_values('突破K索引').reset_index(drop=True)

# 时间转换为UTC+8
swings_df['突破K时间'] = pd.to_datetime(swings_df['突破K时间'], utc=True).dt.tz_convert('Asia/Shanghai')
swings_df['突破K时间'] = swings_df['突破K时间'].dt.strftime('%Y-%m-%d %H:%M')

print(f"\nKAMA同向>=20K的大波段数（去重后）: {len(swings_df)}")
print(f"涨跌幅中位: {swings_df['涨跌幅%'].median():.2f}%")
print(f"涨跌幅均值: {swings_df['涨跌幅%'].mean():.2f}%")

# 保存CSV
output_path = os.path.join(HERE, "results", "KAMA同向20K大波段数据_v3.csv")
swings_df.to_csv(output_path, index=False, encoding='utf-8-sig')
print(f"\n已导出: {output_path}")

# 显示前20条
print(f"\n前20条数据:")
print(swings_df.head(20).to_string(index=False))

# 涨跌幅分桶统计
print(f"\n涨跌幅分桶统计:")
for lo, hi in [(5,10), (10,15), (15,20), (20,30), (30,50), (50,100)]:
    sub = swings_df[(swings_df['涨跌幅%'] >= lo) & (swings_df['涨跌幅%'] < hi)]
    if len(sub) > 0:
        print(f"  [{lo}-{hi})%: {len(sub)}个, 中位K数={sub['持续K数'].median():.0f}")
