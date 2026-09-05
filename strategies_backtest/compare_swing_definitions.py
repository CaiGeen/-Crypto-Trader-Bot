"""新波段口径：KAMA同向运动波段

定义：
1. 起点：第一根收盘站上KAMA+1ATR的位置（做多）/ 收盘跌破KAMA-1ATR（做空）
2. 持续：KAMA值一直同向运动（做多时KAMA持续上升，做空时持续下降）
3. 结束：一旦KAMA反向运动，视为波段结束，终点是整个KAMA同向运动中的最高点

对比现有口径（swing_dataset.py）：
- 现有：6K突破 + KAMA收盘终结（close[j] < KAMA[j-1]）
- 新：KAMA+1ATR站上 + KAMA反向运动终结

关键差异：
1. 起点信号不同：6K突破 vs KAMA+1ATR站上
2. 终结条件不同：收盘跌破KAMA vs KAMA本身反向
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = BacktestConfig()

# 加载数据
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

# 计算KAMA
kama_vals = kama(closes, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

# 计算ATR
def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)

print("=" * 100)
print("波段口径对比：新定义 vs 现有定义")
print("=" * 100)

print("\n【新口径定义】")
print("1. 起点：收盘站上KAMA+1ATR（做多）/ 收盘跌破KAMA-1ATR（做空）")
print("2. 持续：KAMA同向运动（做多时KAMA持续上升）")
print("3. 终结：KAMA反向运动（KAMA[i] < KAMA[i-1]），终点=期间最高点")

print("\n【现有口径定义（swing_dataset.py）】")
print("1. 起点：6K收盘突破前6K区间极值")
print("2. 持续：持仓中")
print("3. 终结：收盘跌破KAMA[i-1]（做多），终点=期间最高点")

print("\n【关键差异】")
print("| 维度 | 新口径 | 现有口径 |")
print("|------|--------|----------|")
print("| 起点信号 | KAMA+1ATR站上 | 6K突破 |")
print("| 终结条件 | KAMA反向运动 | 收盘跌破KAMA |")
print("| 灵敏度 | 更敏感（KAMA拐点即终结） | 较迟钝（等收盘确认） |")
print("| 波段长度 | 更短 | 更长 |")

# 实现新口径波段
print("\n" + "=" * 100)
print("一、实现新口径波段")
print("=" * 100)

def detect_kama_swing_breakouts():
    """检测KAMA+1ATR站上的起点"""
    swings = []
    n = len(closes)
    
    for i in range(1, n):
        if np.isnan(kama_vals[i]) or np.isnan(atr_vals[i-1]):
            continue
        
        kama_prev = kama_vals[i-1]
        atr_val = atr_vals[i-1]
        close = closes[i]
        
        # 做多：收盘站上KAMA+1ATR
        if close >= kama_prev + atr_val:
            # 找KAMA同向运动的终点
            peak = close
            peak_idx = i
            end_idx = i
            
            for j in range(i+1, n):
                if np.isnan(kama_vals[j-1]) or np.isnan(kama_vals[j]):
                    break
                
                # KAMA反向运动 = 波段终结
                if kama_vals[j] < kama_vals[j-1]:
                    end_idx = j
                    break
                
                # 更新最高点
                if closes[j] > peak:
                    peak = closes[j]
                    peak_idx = j
                
                end_idx = j
            
            # 计算涨跌幅
            amp = (peak - kama_prev) / kama_prev * 100 if kama_prev > 0 else 0
            duration = end_idx - i
            
            swings.append({
                '方向': '做多',
                '起点K': i,
                '终点K': end_idx,
                '峰值K': peak_idx,
                '起点时间': times[i],
                '终点时间': times[end_idx],
                '起点价格': kama_prev,
                '峰值价格': peak,
                '波段涨跌幅%': amp,
                'K数': duration
            })
    
    return swings

new_swings = detect_kama_swing_breakouts()
new_df = pd.DataFrame(new_swings)
print(f"\n新口径波段数: {len(new_df)}")

if len(new_df) > 0:
    print(f"\n新口径统计:")
    print(f"  涨跌幅: 中位={new_df['波段涨跌幅%'].median():.2f}% 均值={new_df['波段涨跌幅%'].mean():.2f}%")
    print(f"  K数: 中位={new_df['K数'].median():.0f} 均值={new_df['K数'].mean():.1f}")

# 读取现有口径波段
print("\n" + "=" * 100)
print("二、现有口径波段")
print("=" * 100)

csv_path = os.path.join(HERE, "results", "全时段波段数据集_6K突破_KAMA收盘终结.csv")
if os.path.exists(csv_path):
    old_df = pd.read_csv(csv_path, encoding="utf-8-sig")
    old_df = old_df[old_df['方向'] == '做多']
    print(f"\n现有口径波段数: {len(old_df)}")
    print(f"  涨跌幅: 中位={old_df['波段涨跌幅%'].median():.2f}% 均值={old_df['波段涨跌幅%'].mean():.2f}%")
    print(f"  K数: 中位={old_df['起点至终点K数'].median():.0f} 均值={old_df['起点至终点K数'].mean():.1f}")

# 对比分析
print("\n" + "=" * 100)
print("三、口径对比分析")
print("=" * 100)

if len(new_df) > 0 and os.path.exists(csv_path):
    print(f"\n{'指标':>20} {'新口径':>15} {'现有口径':>15}")
    print(f"  {'波段数':>18} {len(new_df):>15} {len(old_df):>15}")
    print(f"  {'中位涨跌幅%':>18} {new_df['波段涨跌幅%'].median():>15.2f} {old_df['波段涨跌幅%'].median():>15.2f}")
    print(f"  {'均值涨跌幅%':>18} {new_df['波段涨跌幅%'].mean():>15.2f} {old_df['波段涨跌幅%'].mean():>15.2f}")
    print(f"  {'中位K数':>18} {new_df['K数'].median():>15.0f} {old_df['起点至终点K数'].median():>15.0f}")
    print(f"  {'均值K数':>18} {new_df['K数'].mean():>15.1f} {old_df['起点至终点K数'].mean():>15.1f}")

# 新口径分桶统计
print("\n" + "=" * 100)
print("四、新口径分桶统计")
print("=" * 100)

if len(new_df) > 0:
    bins = [(0,5), (5,10), (10,15), (15,20), (20,30), (30,50), (50,100)]
    print(f"\n{'K数区间':>10} {'波段数':>6} {'占比':>6} {'中位涨跌幅':>10} {'均值涨跌幅':>10}")
    for lo, hi in bins:
        sub = new_df[(new_df['K数'] >= lo) & (new_df['K数'] < hi)]
        if len(sub) == 0:
            continue
        pct = len(sub) / len(new_df) * 100
        print(f"  [{lo:>3},{hi:>3})     {len(sub):>5} {pct:>5.1f}% {sub['波段涨跌幅%'].median():>9.2f}% {sub['波段涨跌幅%'].mean():>9.2f}%")

# 新口径涨跌幅分桶
print(f"\n{'涨跌幅区间':>12} {'波段数':>6} {'占比':>6} {'中位K数':>8}")
amp_bins = [(0,5), (5,10), (10,15), (15,20), (20,30), (30,50), (50,100)]
for lo, hi in amp_bins:
    sub = new_df[(new_df['波段涨跌幅%'] >= lo) & (new_df['波段涨跌幅%'] < hi)]
    if len(sub) == 0:
        continue
    pct = len(sub) / len(new_df) * 100
    print(f"  [{lo:>3},{hi:>3})%    {len(sub):>5} {pct:>5.1f}% {sub['K数'].median():>7.0f}")

# 结论
print("\n" + "=" * 100)
print("五、口径对比结论")
print("=" * 100)

print("""
逻辑对比:
1. 新口径起点更敏感：KAMA+1ATR站上 vs 6K突破
2. 新口径终结更早：KAMA反向运动 vs 收盘跌破KAMA
3. 新口径波段更短：更早捕捉趋势，更早离场

适用场景:
- 新口径：适合趋势跟踪策略，更灵敏
- 现有口径：适合波段策略，更稳健

建议:
- 先用新口径做一次回测，对比绩效差异
- 如果新口径波段更短但涨跌幅更大，可能更适合实盘
""")
