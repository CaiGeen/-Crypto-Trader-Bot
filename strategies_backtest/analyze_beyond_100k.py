"""分析KAMA掉头100K之后的信号质量，验证是否适合恢复交易。"""
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

def detect_swings_v2():
    swings = []
    for i in range(1, n):
        if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            continue
        if kama_vals[i] > kama_vals[i-1]:
            kama_turn = kama_vals[i]
            atr_val = atr_vals[i-1]
            breakout_level = kama_turn + atr_val
            for j in range(i, min(i+20, n)):
                if closes[j] >= breakout_level:
                    peak = closes[j]; end_idx = j
                    for k in range(j+1, min(j+50, n)):
                        if np.isnan(kama_vals[k]) or np.isnan(kama_vals[k-1]):
                            break
                        if kama_vals[k] < kama_vals[k-1]:
                            end_idx = k
                            break
                        if closes[k] > peak:
                            peak = closes[k]
                        end_idx = k
                    amp = (peak - breakout_level) / breakout_level * 100
                    swings.append({
                        'breakout_idx': j, 'end_idx': end_idx,
                        'breakout_time': times[j], 'end_time': times[end_idx],
                        'amp': amp
                    })
                    break
    return swings

swings = detect_swings_v2()
swings_df = pd.DataFrame(swings)
big_swings = swings_df[swings_df['amp'] >= 10].copy()
big_times_sorted = np.sort(pd.to_datetime(big_swings['breakout_time'], utc=True).to_numpy())

tdf = pd.read_csv(os.path.join(HERE, "results", "all_signals_E_prime.csv"), encoding="utf-8-sig")
tdf['信号时间'] = pd.to_datetime(tdf['信号时间'], utc=True)

signal_times = tdf['信号时间'].to_numpy()
prev_big = np.searchsorted(big_times_sorted, signal_times, side='left') - 1
k_gaps = np.array([
    (signal_times[i] - big_times_sorted[prev_big[i]]) / pd.Timedelta(hours=4)
    if prev_big[i] >= 0 else np.inf
    for i in range(len(signal_times))
])
tdf['距前大波段K数'] = k_gaps
tdf['有前大波段'] = prev_big >= 0

print("=" * 100)
print("100K之后信号质量深度分析")
print("=" * 100)

# 100K之后的信号进一步细分
beyond100 = tdf[tdf['距前大波段K数'] >= 100].copy()
print(f"\n100K之后的信号总数: {len(beyond100)}")
print(f"均R: {beyond100['R值'].mean():.2f}, 胜率: {(beyond100['R值']>0).mean()*100:.1f}%")
print(f"累计R: {beyond100['R值'].sum():.1f}")

# 细分100K之后
print("\n100K之后细分:")
for lo, hi, label in [(100,150,'100-150'), (150,200,'150-200'), (200,300,'200-300'), (300,500,'300-500'), (500,1000,'500-1000'), (1000,100000,'1000+')]:
    sub = beyond100[(beyond100['距前大波段K数']>=lo) & (beyond100['距前大波段K数']<hi)]
    if len(sub) == 0:
        continue
    r = sub['R值']
    avg_r = r.mean()
    win = (r > 0).mean()*100
    wins = r[r>0]; losses = r[r<0]
    pf = wins.sum()/abs(losses.sum()) if len(losses)>0 and losses.sum()!=0 else float('inf')
    print(f"  {label:>8}: {len(sub):>5}单, 均R={avg_r:>6.2f}, 胜率={win:>5.1f}%, PF={pf:>6.1f}, 累计R={r.sum():>7.1f}")

# 对比基准
print(f"\n基准(全部信号): 均R={tdf['R值'].mean():.2f}, 胜率={(tdf['R值']>0).mean()*100:.1f}%")

# 统计100K之后信号占全部的比例
print(f"\n100K之后信号占全部: {len(beyond100)}/{len(tdf)} = {len(beyond100)/len(tdf)*100:.1f}%")

# 关键：100K之后信号是否还是"受大波段影响"？
# 实际上100K=400小时≈17天，大波段影响应该已经消散
# 这些信号的质量应该回归基准

print("\n" + "=" * 100)
print("结论分析")
print("=" * 100)

# 对比不同区间的累计R贡献
print("\n各区间信号对总R的贡献:")
for lo, hi, label in [(0,5,'0-5'), (5,10,'5-10'), (10,20,'10-20'), (20,50,'20-50'), (50,100,'50-100'), (100,100000,'100+')]:
    sub = tdf[(tdf['距前大波段K数']>=lo) & (tdf['距前大波段K数']<hi)]
    if len(sub)==0:
        continue
    r_sum = sub['R值'].sum()
    print(f"  {label:>6}: {len(sub):>5}单, 累计R={r_sum:>7.1f}, 占全部R={r_sum/tdf['R值'].sum()*100:.1f}%")