"""新口径波段起点终点详解 + 示例"""
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
print("新口径波段起点终点定义详解")
print("=" * 100)

print("""
【起点定义】
做多起点：收盘价[i] >= KAMA[i-1] + ATR[i-1]
做空起点：收盘价[i] <= KAMA[i-1] - ATR[i-1]

含义：价格收盘时站上（或跌破）KAMA线一个ATR的距离，说明价格已经"甩开"KAMA，趋势启动。

【终点定义】
做多终点：KAMA开始反向运动（KAMA[i] < KAMA[i-1]）后，整个波段期间的最高点
做空终点：KAMA开始反向运动（KAMA[i] > KAMA[i-1]）后，整个波段期间的最低点

含义：KAMA是自适应均线，当KAMA开始反向，说明趋势动能衰减。但终点取整个波段期间的最优价，
      因为KAMA反向时价格可能已经从高点回落。
""")

# 找几个具体的波段示例
print("\n" + "=" * 100)
print("一、做多起点示例（找3个典型案例）")
print("=" * 100)

examples = []
for i in range(10, n):
    if np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
        continue
    
    kama_prev = kama_vals[i-1]
    atr_val = atr_vals[i-1]
    close = closes[i]
    
    # 做多起点
    if close >= kama_prev + atr_val:
        # 找终点
        peak = close
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
        
        if len(examples) < 3:
            examples.append({
                'i': i,
                'kama_prev': kama_prev,
                'atr': atr_val,
                'close': close,
                'peak': peak,
                'peak_idx': peak_idx,
                'end_idx': end_idx,
                'amp': (peak - kama_prev) / kama_prev * 100
            })

for idx, ex in enumerate(examples):
    i = ex['i']
    print(f"\n【示例{idx+1}】做多起点")
    print(f"  起点K索引: {i}, 时间: {times[i]}")
    print(f"  起点条件:")
    print(f"    KAMA[i-1] = {ex['kama_prev']:.2f}")
    print(f"    ATR[i-1] = {ex['atr']:.2f}")
    print(f"    KAMA + ATR = {ex['kama_prev'] + ex['atr']:.2f}")
    print(f"    收盘价[i] = {ex['close']:.2f}")
    print(f"    收盘价 >= KAMA+ATR? {'是' if ex['close'] >= ex['kama_prev'] + ex['atr'] else '否'}")
    print(f"  终点:")
    print(f"    峰值: {ex['peak']:.2f} (K索引: {ex['peak_idx']})")
    print(f"    KAMA反向K: {ex['end_idx']}")
    print(f"  涨跌幅: {ex['amp']:.2f}%")
    print(f"  K数: {ex['end_idx'] - i}")

# 详细展示一个示例的KAMA变化
print("\n" + "=" * 100)
print("二、详细示例：KAMA变化过程")
print("=" * 100)

if examples:
    ex = examples[0]
    i = ex['i']
    print(f"\n起点K索引: {i}, 时间: {times[i]}")
    print(f"\n{'K索引':>6} {'时间':>20} {'收盘':>8} {'KAMA':>8} {'ATR':>8} {'KAMA+ATR':>10} {'备注':>10}")
    for j in range(max(i-2, 0), min(i+15, n)):
        if np.isnan(kama_vals[j]) or np.isnan(atr_vals[j]):
            continue
        close_j = closes[j]
        kama_j = kama_vals[j]
        atr_j = atr_vals[j]
        
        note = ""
        if j == i:
            note = "★起点"
        elif kama_j < kama_vals[j-1] if j > 0 else False:
            note = "★KAMA反向"
        
        print(f"  {j:>4} {times[j].strftime('%Y-%m-%d %H:%M'):>18} {close_j:>8.2f} {kama_j:>8.2f} {atr_j:>8.2f} {kama_j+atr_j:>10.2f} {note:>10}")

# 做空示例
print("\n" + "=" * 100)
print("三、做空起点示例")
print("=" * 100)

short_examples = []
for i in range(10, n):
    if np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
        continue
    
    kama_prev = kama_vals[i-1]
    atr_val = atr_vals[i-1]
    close = closes[i]
    
    # 做空起点
    if close <= kama_prev - atr_val:
        # 找终点
        low = close
        low_idx = i
        end_idx = i
        for j in range(i+1, min(i+50, n)):
            if np.isnan(kama_vals[j]) or np.isnan(kama_vals[j-1]):
                break
            if kama_vals[j] > kama_vals[j-1]:
                end_idx = j
                break
            if closes[j] < low:
                low = closes[j]
                low_idx = j
            end_idx = j
        
        if len(short_examples) < 2:
            short_examples.append({
                'i': i,
                'kama_prev': kama_prev,
                'atr': atr_val,
                'close': close,
                'low': low,
                'low_idx': low_idx,
                'end_idx': end_idx,
                'amp': (kama_prev - low) / kama_prev * 100
            })

for idx, ex in enumerate(short_examples):
    i = ex['i']
    print(f"\n【做空示例{idx+1}】")
    print(f"  起点K索引: {i}, 时间: {times[i]}")
    print(f"  KAMA[i-1] = {ex['kama_prev']:.2f}, ATR = {ex['atr']:.2f}")
    print(f"  KAMA - ATR = {ex['kama_prev'] - ex['atr']:.2f}")
    print(f"  收盘价 = {ex['close']:.2f}")
    print(f"  最低价 = {ex['low']:.2f} (K索引: {ex['low_idx']})")
    print(f"  涨跌幅: {ex['amp']:.2f}%")

# 总结
print("\n" + "=" * 100)
print("四、定义总结")
print("=" * 100)

print("""
【做多起点】
条件：收盘价[i] >= KAMA[i-1] + ATR[i-1]
含义：价格收盘时站上KAMA线一个ATR，说明趋势启动

【做多终点】
条件：KAMA[i] < KAMA[i-1]（KAMA开始下降）
价格：整个波段期间的最高点

【做空起点】
条件：收盘价[i] <= KAMA[i-1] - ATR[i-1]
含义：价格收盘时跌破KAMA线一个ATR，说明下跌趋势启动

【做空终点】
条件：KAMA[i] > KAMA[i-1]（KAMA开始上升）
价格：整个波段期间的最低点

【关键点】
1. 起点用KAMA[i-1]（前一K的值），避免未来函数
2. 终点用KAMA反向运动确认，但价格取整个波段的最优价
3. ATR用固定值atr_vals[i-1]，不动态更新
""")
