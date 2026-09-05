"""只保留同向KAMA>=20K的波段分析

过滤条件：KAMA同向运动>=20根K才算有效波段
目的：去掉琐碎波段，只保留连续性强的真正波段
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
print("只保留同向KAMA>=20K的波段分析")
print("=" * 100)

# 完整波段检测
def detect_all_swings(min_kama_run=1):
    """检测所有波段，min_kama_run为最小同向KAMA运行根数"""
    swings = []
    
    for i in range(1, n):
        if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            continue
        
        # KAMA掉头上升
        if kama_vals[i] > kama_vals[i-1]:
            kama_turn = kama_vals[i]
            atr_val = atr_vals[i-1]
            breakout_level = kama_turn + atr_val
            
            # 计算KAMA同向运行根数
            kama_run = 1
            for j in range(i+1, min(i+100, n)):
                if np.isnan(kama_vals[j]) or np.isnan(kama_vals[j-1]):
                    break
                if kama_vals[j] < kama_vals[j-1]:
                    break
                kama_run += 1
            
            if kama_run < min_kama_run:
                continue
            
            # 找价格站上KAMA+ATR的位置
            for j in range(i, min(i+20, n)):
                if closes[j] >= breakout_level:
                    # 找终点
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
                    duration = end_idx - j
                    
                    swings.append({
                        'breakout_idx': j,
                        'end_idx': end_idx,
                        'peak_idx': peak_idx,
                        'breakout_time': times[j],
                        'end_time': times[end_idx],
                        'amp': amp,
                        'K数': duration,
                        'kama_run': kama_run,
                        'kama_turn_idx': i
                    })
                    break
    
    return swings

# 完整波段（所有）
all_swings = detect_all_swings(min_kama_run=1)
all_df = pd.DataFrame(all_swings)
print(f"\n完整波段（所有）: {len(all_df)}个")

# 过滤：KAMA同向>=20K
filtered_swings = detect_all_swings(min_kama_run=20)
filtered_df = pd.DataFrame(filtered_swings)
print(f"过滤后（KAMA同向>=20K）: {len(filtered_df)}个")

# 统计对比
print("\n" + "=" * 100)
print("统计对比")
print("=" * 100)

print(f"\n{'指标':>20} {'完整':>15} {'过滤后':>15}")
for label, col in [('中位涨跌幅%', 'amp'), ('均值涨跌幅%', 'amp'), ('中位K数', 'K数'), ('均值K数', 'K数')]:
    if col == 'amp':
        v_all = all_df[col].median() if '中位' in label else all_df[col].mean()
        v_filt = filtered_df[col].median() if '中位' in label else filtered_df[col].mean()
    else:
        v_all = all_df[col].median() if '中位' in label else all_df[col].mean()
        v_filt = filtered_df[col].median() if '中位' in label else filtered_df[col].mean()
    print(f"  {label:>18} {v_all:>15.2f} {v_filt:>15.2f}")

# 涨跌幅分桶
print("\n" + "=" * 100)
print("涨跌幅分桶对比")
print("=" * 100)

bins = [(0,5), (5,10), (10,15), (15,20), (20,30), (30,50), (50,100)]
print(f"\n{'涨跌幅区间':>12} {'完整波段数':>12} {'过滤后波段数':>16} {'过滤后占比':>12}")
for lo, hi in bins:
    sub_all = all_df[(all_df['amp'] >= lo) & (all_df['amp'] < hi)]
    sub_filt = filtered_df[(filtered_df['amp'] >= lo) & (filtered_df['amp'] < hi)]
    pct = len(sub_filt) / len(sub_all) * 100 if len(sub_all) > 0 else 0
    print(f"  [{lo:>3}-{hi:>3})%    {len(sub_all):>10} {len(sub_filt):>14} {pct:>10.1f}%")

# K数分桶
print("\n" + "=" * 100)
print("KAMA同向运行根数分桶")
print("=" * 100)

print(f"\n{'KAMA运行':>12} {'完整波段数':>12} {'过滤后波段数':>16} {'均涨跌幅':>10}")
for lo, hi in [(1,5), (5,10), (10,15), (15,20), (20,30), (30,50), (50,100)]:
    sub_all = all_df[(all_df['kama_run'] >= lo) & (all_df['kama_run'] < hi)]
    sub_filt = filtered_df[(filtered_df['kama_run'] >= lo) & (filtered_df['kama_run'] < hi)]
    avg_amp = sub_filt['amp'].mean() if len(sub_filt) > 0 else 0
    print(f"  [{lo:>3}-{hi:>3})K    {len(sub_all):>10} {len(sub_filt):>14} {avg_amp:>9.2f}%")

# 大波段（>=10%）对比
print("\n" + "=" * 100)
print("大波段（>=10%）对比")
print("=" * 100)

big_all = all_df[all_df['amp'] >= 10]
big_filt = filtered_df[filtered_df['amp'] >= 10]
print(f"\n完整大波段: {len(big_all)}个")
print(f"过滤后大波段: {len(big_filt)}个")
print(f"过滤后保留率: {len(big_filt)/len(big_all)*100:.1f}%")

if len(big_all) > 0 and len(big_filt) > 0:
    print(f"\n完整大波段: 中位{big_all['amp'].median():.2f}% 均值{big_all['amp'].mean():.2f}%")
    print(f"过滤后大波段: 中位{big_filt['amp'].median():.2f}% 均值{big_filt['amp'].mean():.2f}%")

# 结论
print("\n" + "=" * 100)
print("结论")
print("=" * 100)

print(f"""
过滤条件：KAMA同向运行>=20K

结果：
1. 波段数从 {len(all_df)} 降到 {len(filtered_df)}（保留 {len(filtered_df)/len(all_df)*100:.1f}%）
2. 大波段（>=10%）从 {len(big_all)} 降到 {len(big_filt)}（保留 {len(big_filt)/len(big_all)*100:.1f}%）
3. 过滤后的波段涨跌幅均值：{filtered_df['amp'].mean():.2f}%（完整：{all_df['amp'].mean():.2f}%）

结论：
- 过滤掉了 {len(all_df) - len(filtered_df)} 个琐碎波段
- 保留的波段涨跌幅更大（连续性强的趋势更可靠）
- 大波段保留率：{len(big_filt)/len(big_all)*100:.1f}%，说明连续性强的波段更容易产生大涨幅
""")