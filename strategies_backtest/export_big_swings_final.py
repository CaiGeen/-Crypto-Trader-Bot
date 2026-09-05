"""重新设计：按"大波段"检测（而非"入场点"）

算法：
1. 检测KAMA掉头上升的点（KAMA[i] > KAMA[i-1]）
2. 从掉头点开始，跟踪KAMA同向运动，直到KAMA反向（KAMA[j] < KAMA[j-1]）
3. 整个期间作为一个"大波段"
4. 计算起点价格 = KAMA掉头值 + 1ATR（突破线）
5. 计算峰值 = 整个波段期间的最高价
6. 计算涨跌幅 = (峰值 - 起点价格) / 起点价格
7. 只保留KAMA同向运行>=20K的大波段
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
print('按"大波段"检测算法（重新设计）')
print("=" * 100)

print('''
算法逻辑：
1. 检测KAMA掉头上升的点（KAMA[i] > KAMA[i-1]）
2. 从掉头点开始，跟踪KAMA同向运动，直到KAMA反向（KAMA[j] < KAMA[j-1]）
3. 整个期间作为一个"大波段"
4. 起点价格 = KAMA掉头值 + 1ATR（突破线）
5. 峰值 = 整个波段期间的最高价
6. 涨跌幅 = (峰值 - 起点价格) / 起点价格
7. 只保留KAMA同向运行>=20K的大波段
''')

# 检测大波段
def detect_big_swings(min_kama_run=20):
    swings = []
    i = 1
    while i < n:
        if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            i += 1
            continue
        
        # KAMA掉头上升
        if kama_vals[i] > kama_vals[i-1]:
            kama_turn = kama_vals[i]
            atr_val = atr_vals[i-1]
            start_price = kama_turn + atr_val  # 起点价格 = KAMA + ATR
            turn_idx = i
            
            # 跟踪KAMA同向运动，直到KAMA反向
            peak = closes[i]
            peak_idx = i
            end_idx = i
            kama_run = 1
            
            for j in range(i+1, n):
                if np.isnan(kama_vals[j]) or np.isnan(kama_vals[j-1]):
                    break
                
                # KAMA反向 = 波段结束
                if kama_vals[j] < kama_vals[j-1]:
                    end_idx = j
                    break
                
                # 更新最高价
                if closes[j] > peak:
                    peak = closes[j]
                    peak_idx = j
                
                kama_run += 1
                end_idx = j
            
            # 只保留KAMA同向运行>=min_kamaRun的大波段
            if kama_run >= min_kama_run:
                amp = (peak - start_price) / start_price * 100 if start_price > 0 else 0
                duration = end_idx - turn_idx
                
                swings.append({
                    '掉头K索引': turn_idx,
                    '掉头K时间': times[turn_idx],
                    '起点KAMA值': round(kama_turn, 2),
                    'ATR值': round(atr_val, 2),
                    '起点价格(突破线)': round(start_price, 2),
                    '峰值价格': round(peak, 2),
                    '峰值K索引': peak_idx,
                    '终点K索引': end_idx,
                    '涨跌幅%': round(amp, 2),
                    '持续K数': duration,
                    'KAMA同向运行根数': kama_run
                })
            
            i = end_idx + 1
        else:
            i += 1
    
    return swings

swings = detect_big_swings(min_kama_run=20)
swings_df = pd.DataFrame(swings)

# 时间转换为UTC+8
swings_df['掉头K时间'] = pd.to_datetime(swings_df['掉头K时间'], utc=True).dt.tz_convert('Asia/Shanghai')
swings_df['掉头K时间'] = swings_df['掉头K时间'].dt.strftime('%Y-%m-%d %H:%M')

print(f"\nKAMA同向>=20K的大波段数: {len(swings_df)}")
print(f"涨跌幅中位: {swings_df['涨跌幅%'].median():.2f}%")
print(f"涨跌幅均值: {swings_df['涨跌幅%'].mean():.2f}%")
print(f"持续K数中位: {swings_df['持续K数'].median():.0f}")

# 保存CSV
output_path = os.path.join(HERE, "results", "KAMA同向20K大波段数据_最终版.csv")
swings_df.to_csv(output_path, index=False, encoding='utf-8-sig')
print(f"\n已导出: {output_path}")

# 显示前20条
print(f"\n前20条数据:")
print(swings_df.head(20).to_string(index=False))

# 涨跌幅分桶统计
print(f"\n涨跌幅分桶统计:")
for lo, hi in [(0,5), (5,10), (10,15), (15,20), (20,30), (30,50), (50,100)]:
    sub = swings_df[(swings_df['涨跌幅%'] >= lo) & (swings_df['涨跌幅%'] < hi)]
    if len(sub) > 0:
        print(f"  [{lo}-{hi})%: {len(sub)}个, 中位K数={sub['持续K数'].median():.0f}, 均值涨跌幅={sub['涨跌幅%'].mean():.2f}%")
