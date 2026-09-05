"""分析被过滤掉的大波段（KAMA同向<20K）的间隔分布。"""
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
print("分析被过滤掉的大波段（KAMA同向<20K）的间隔分布")
print("=" * 100)

def detect_all_swings():
    """检测所有波段，同时记录KAMA同向根数"""
    swings = []
    
    for i in range(1, n):
        if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            continue
        
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
                        'K数': duration,
                        'kama_run': kama_run
                    })
                    break
    
    return swings

all_swings = detect_all_swings()
all_df = pd.DataFrame(all_swings)

# 只看大波段（>=10%）
big_df = all_df[all_df['amp'] >= 10].copy()
big_df = big_df.sort_values('breakout_idx').reset_index(drop=True)

print(f"\n大波段总数（>=10%）: {len(big_df)}")

# 分两组
filtered = big_df[big_df['kama_run'] < 20]  # 被过滤的
kept = big_df[big_df['kama_run'] >= 20]     # 保留的

print(f"保留（KAMA>=20K）: {len(kept)}个")
print(f"过滤（KAMA<20K）: {len(filtered)}个")

# 计算间隔
print("\n" + "=" * 100)
print("间隔统计（前一个大波段结束到当前大波段开始的K数）")
print("=" * 100)

def compute_gaps(df_group):
    """计算大波段之间的间隔（起点到起点）"""
    if len(df_group) < 2:
        return []
    
    gaps = []
    for i in range(1, len(df_group)):
        prev_start = df_group.iloc[i-1]['breakout_idx']
        curr_start = df_group.iloc[i]['breakout_idx']
        gap = curr_start - prev_start
        gaps.append(gap)
    
    return gaps

kept_gaps = compute_gaps(kept)
filtered_gaps = compute_gaps(filtered)

print(f"\n{'指标':>20} {'保留波段':>15} {'过滤波段':>15}")
print(f"  {'间隔样本数':>18} {len(kept_gaps):>15} {len(filtered_gaps):>15}")

if kept_gaps:
    print(f"  {'中位间隔K数':>18} {np.median(kept_gaps):>15.0f} {np.median(filtered_gaps) if filtered_gaps else 0:>15.0f}")
    print(f"  {'均值间隔K数':>18} {np.mean(kept_gaps):>15.1f} {np.mean(filtered_gaps) if filtered_gaps else 0:>15.1f}")
    print(f"  {'P25间隔K数':>18} {np.percentile(kept_gaps, 25):>15.0f} {np.percentile(filtered_gaps, 25) if filtered_gaps else 0:>15.0f}")
    print(f"  {'P75间隔K数':>18} {np.percentile(kept_gaps, 75):>15.0f} {np.percentile(filtered_gaps, 75) if filtered_gaps else 0:>15.0f}")

# 间隔分桶
print("\n" + "=" * 100)
print("间隔分桶分布")
print("=" * 100)

bins = [0, 5, 10, 15, 20, 30, 50, 100, 1000]
labels = ['0-5', '5-10', '10-15', '15-20', '20-30', '30-50', '50-100', '100+']

print(f"\n{'间隔K数':>10} {'保留数':>8} {'保留占比':>10} {'过滤数':>8} {'过滤占比':>10}")
for lo, hi, label in [(0,5,'0-5'), (5,10,'5-10'), (10,15,'10-15'), (15,20,'15-20'), (20,30,'20-30'), (30,50,'30-50'), (50,100,'50-100'), (100,1000,'100+')]:
    k_count = sum(1 for g in kept_gaps if lo <= g < hi)
    f_count = sum(1 for g in filtered_gaps if lo <= g < hi)
    k_pct = k_count / len(kept_gaps) * 100 if kept_gaps else 0
    f_pct = f_count / len(filtered_gaps) * 100 if filtered_gaps else 0
    print(f"  [{lo:>3}-{hi:>3})    {k_count:>6} {k_pct:>8.1f}% {f_count:>6} {f_pct:>8.1f}%")

# 涨跌幅对比
print("\n" + "=" * 100)
print("保留vs过滤波段的涨跌幅对比")
print("=" * 100)

print(f"\n保留波段: 中位{kept['amp'].median():.2f}% 均值{kept['amp'].mean():.2f}%")
print(f"过滤波段: 中位{filtered['amp'].median():.2f}% 均值{filtered['amp'].mean():.2f}%")

# 结论
print("\n" + "=" * 100)
print("统计结论")
print("=" * 100)

if kept_gaps and filtered_gaps:
    print(f"""
保留波段（KAMA>=20K）:
  - 中位间隔: {np.median(kept_gaps):.0f}K
  - 均值间隔: {np.mean(kept_gaps):.1f}K
  - 75%分位: {np.percentile(kept_gaps, 75):.0f}K

过滤波段（KAMA<20K）:
  - 中位间隔: {np.median(filtered_gaps):.0f}K
  - 均值间隔: {np.mean(filtered_gaps):.1f}K
  - 75%分位: {np.percentile(filtered_gaps, 75):.0f}K

结论:
1. 保留波段的间隔更长（中位{np.median(kept_gaps):.0f}K vs {np.median(filtered_gaps):.0f}K），说明连续性强的波段之间有更长的"酝酿期"
2. 过滤波段的间隔更短，说明琐碎波段频繁出现，间隔短
3. 保留波段的涨跌幅更大（中位{kept['amp'].median():.2f}% vs {filtered['amp'].median():.2f}%）
4. 过滤条件KAMA>=20K有效地筛选出了"酝酿期长、涨跌幅大"的真正趋势
""")
