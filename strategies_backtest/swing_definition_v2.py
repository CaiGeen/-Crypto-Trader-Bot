"""新口径波段起点定义修改版

修改后的定义：
1. 做多起点：先找到KAMA掉头上升的点（KAMA[i] > KAMA[i-1]），
   然后找价格站上KAMA+1ATR的位置作为起点
2. 做空起点：先找到KAMA掉头下降的点（KAMA[i] < KAMA[i-1]），
   然后找价格跌破KAMA-1ATR的位置作为起点

核心逻辑：
- KAMA掉头 = 趋势启动信号
- KAMA+1ATR站上 = 确认突破位置
- 两者结合 = 更精确的起点定义
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
print("波段起点定义修改版")
print("=" * 100)

print("""
【原定义】
做多起点：收盘价[i] >= KAMA[i-1] + ATR[i-1]

【新定义】
1. 先找到KAMA掉头上升的点（KAMA[i] > KAMA[i-1]）
2. 然后找价格站上KAMA+1ATR的位置作为起点
3. 起点价格 = 掉头当K的KAMA值 + 1ATR

【逻辑】
- KAMA掉头 = 趋势启动信号
- KAMA+1ATR站上 = 确认突破位置
- 两者结合 = 更精确的起点定义

【做空同理】
1. KAMA掉头下降（KAMA[i] < KAMA[i-1]）
2. 价格跌破KAMA-1ATR
3. 起点价格 = 掉头当K的KAMA值 - 1ATR
""")

# 实现新定义
print("\n" + "=" * 100)
print("一、实现新定义波段")
print("=" * 100)

def detect_swings_v2():
    """检测新定义波段"""
    swings = []
    n = len(closes)
    
    # 做多
    for i in range(1, n):
        if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            continue
        
        # KAMA掉头上升
        if kama_vals[i] > kama_vals[i-1]:
            kama_turn = kama_vals[i]  # 掉头当K的KAMA值
            atr_val = atr_vals[i-1]
            breakout_level = kama_turn + atr_val  # KAMA+ATR
            
            # 找价格站上KAMA+ATR的位置
            for j in range(i, min(i+20, n)):
                if closes[j] >= breakout_level:
                    # 找终点（KAMA反向运动）
                    peak = closes[j]
                    peak_idx = j
                    end_idx = j
                    
                    for k in range(j+1, min(j+50, n)):
                        if np.isnan(kama_vals[k]) or np.isnan(kama_vals[k-1]):
                            break
                        if kama_vals[k] < kama_vals[k-1]:
                            end_idx = k
                            break
                        if closes[k] > peak:
                            peak = closes[k]
                            peak_idx = k
                        end_idx = k
                    
                    amp = (peak - breakout_level) / breakout_level * 100 if breakout_level > 0 else 0
                    swings.append({
                        '方向': '做多',
                        'turn_idx': i,  # KAMA掉头K
                        'breakout_idx': j,  # 突破K
                        'end_idx': end_idx,
                        'peak_idx': peak_idx,
                        'turn_time': times[i],
                        'breakout_time': times[j],
                        'end_time': times[end_idx],
                        'kama_turn': kama_turn,
                        'breakout_level': breakout_level,
                        'peak': peak,
                        'amp': amp,
                        'K数': end_idx - j
                    })
                    break
    
    return swings

swings_v2 = detect_swings_v2()
df_v2 = pd.DataFrame(swings_v2)
print(f"\n新定义波段数: {len(df_v2)}")

if len(df_v2) > 0:
    print(f"\n新定义统计:")
    print(f"  涨跌幅: 中位={df_v2['amp'].median():.2f}% 均值={df_v2['amp'].mean():.2f}%")
    print(f"  K数: 中位={df_v2['K数'].median():.0f} 均值={df_v2['K数'].mean():.1f}")

# 详细示例
print("\n" + "=" * 100)
print("二、详细示例（找3个）")
print("=" * 100)

for idx in range(min(3, len(df_v2))):
    row = df_v2.iloc[idx]
    turn_idx = int(row['turn_idx'])
    breakout_idx = int(row['breakout_idx'])
    
    print(f"\n【示例{idx+1}】")
    print(f"  KAMA掉头K: {turn_idx}, 时间: {row['turn_time']}")
    print(f"  突破K: {breakout_idx}, 时间: {row['breakout_time']}")
    print(f"  终点K: {row['end_idx']}, 时间: {row['end_time']}")
    
    print(f"\n  关键节点:")
    print(f"  {'K索引':>6} {'时间':>20} {'收盘':>8} {'KAMA':>8} {'ATR':>8} {'KAMA+ATR':>10} {'备注':>10}")
    
    for j in range(max(turn_idx-2, 0), min(breakout_idx+5, n)):
        if np.isnan(kama_vals[j]) or np.isnan(atr_vals[j]):
            continue
        
        note = ""
        if j == turn_idx:
            note = "★KAMA掉头"
        elif j == breakout_idx:
            note = "★突破"
        elif j == row['end_idx']:
            note = "★终点"
        
        kama_plus_atr = kama_vals[j] + atr_vals[j]
        print(f"  {j:>4} {times[j].strftime('%Y-%m-%d %H:%M'):>18} {closes[j]:>8.2f} {kama_vals[j]:>8.2f} {atr_vals[j]:>8.2f} {kama_plus_atr:>10.2f} {note:>10}")
    
    print(f"\n  起点价格（KAMA+ATR）: {row['breakout_level']:.2f}")
    print(f"  峰值: {row['peak']:.2f}")
    print(f"  涨跌幅: {row['amp']:.2f}%")

# 对比原定义
print("\n" + "=" * 100)
print("三、新旧定义对比")
print("=" * 100)

def detect_swings_v1():
    """原定义波段"""
    swings = []
    for i in range(1, n):
        if np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            continue
        
        kama_prev = kama_vals[i-1]
        atr_val = atr_vals[i-1]
        
        if closes[i] >= kama_prev + atr_val:
            peak = closes[i]
            peak_idx = i
            end_idx = i
            
            for j in range(i+1, min(i+50, n)):
                if np.isnan(kama_vals[j]) or np.isnan(kama_vals[j-1]):
                    break
                if kama_vals[j] < kama_vals[j-1]:
                    end_idx = j
                    break
                if closes[j] > peak:
                    peak = closes[j]
                    peak_idx = j
                end_idx = j
            
            amp = (peak - (kama_prev + atr_val)) / (kama_prev + atr_val) * 100
            swings.append({
                'breakout_idx': i,
                'end_idx': end_idx,
                'amp': amp,
                'K数': end_idx - i
            })
    
    return swings

swings_v1 = detect_swings_v1()
df_v1 = pd.DataFrame(swings_v1)

print(f"\n{'指标':>20} {'新定义':>15} {'原定义':>15}")
print(f"  {'波段数':>18} {len(df_v2):>15} {len(df_v1):>15}")
if len(df_v2) > 0 and len(df_v1) > 0:
    print(f"  {'中位涨跌幅%':>18} {df_v2['amp'].median():>15.2f} {df_v1['amp'].median():>15.2f}")
    print(f"  {'均值涨跌幅%':>18} {df_v2['amp'].mean():>15.2f} {df_v1['amp'].mean():>15.2f}")
    print(f"  {'中位K数':>18} {df_v2['K数'].median():>15.0f} {df_v1['K数'].median():>15.0f}")

print("\n" + "=" * 100)
print("四、定义总结")
print("=" * 100)

print("""
【新定义】
1. KAMA掉头上升（KAMA[i] > KAMA[i-1]）
2. 突破KAMA+ATR位置（收盘价 >= KAMA[i] + ATR[i-1]）
3. 起点价格 = KAMA掉头值 + ATR

【原定义】
1. 收盘价 >= KAMA[i-1] + ATR[i-1]
2. 起点价格 = KAMA[i-1] + ATR

【关键差异】
- 新定义先找KAMA掉头信号，再确认突破位置
- 新定义的起点价格更高（KAMA掉头后通常更高）
- 新定义更精确地捕捉趋势启动点
""")
