"""大波段后暂停区间研究 - 修正版（每个信号只归属最近的前一个大波段）"""
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
print("大波段后暂停区间研究（修正版：信号不重复计数）")
print("=" * 100)

# 新定义波段检测
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
                    peak = closes[j]; peak_idx = j; end_idx = j
                    for k in range(j+1, min(j+50, n)):
                        if np.isnan(kama_vals[k]) or np.isnan(kama_vals[k-1]):
                            break
                        if kama_vals[k] < kama_vals[k-1]:
                            end_idx = k
                            break
                        if closes[k] > peak:
                            peak = closes[k]; peak_idx = k
                        end_idx = k
                    amp = (peak - breakout_level) / breakout_level * 100 if breakout_level > 0 else 0
                    swings.append({
                        '方向': '做多', 'breakout_idx': j, 'end_idx': end_idx,
                        'breakout_time': times[j], 'end_time': times[end_idx],
                        'amp': amp, 'K数': end_idx - j
                    })
                    break
    return swings

swings = detect_swings_v2()
swings_df = pd.DataFrame(swings)
big_swings = swings_df[swings_df['amp'] >= 10].copy()
print(f"总波段数: {len(swings_df)}, 大波段数: {len(big_swings)}")

tdf = pd.read_csv(os.path.join(HERE, "results", "all_signals_E_prime.csv"), encoding="utf-8-sig")
tdf['信号时间'] = pd.to_datetime(tdf['信号时间'], utc=True)
print(f"总信号数: {len(tdf)}")

# 关键：对每个信号，找它之前最近的一个大波段
big_times_sorted = np.sort(pd.to_datetime(big_swings['breakout_time'], utc=True).to_numpy())

signal_times = tdf['信号时间'].to_numpy()
# 每个信号之前最近的大波段时间
prev_big = np.searchsorted(big_times_sorted, signal_times, side='left') - 1

# 间隔（根K）
k_gaps = np.array([
    (signal_times[i] - big_times_sorted[prev_big[i]]) / pd.Timedelta(hours=4)
    if prev_big[i] >= 0 else np.inf
    for i in range(len(signal_times))
])

tdf['距前大波段K数'] = k_gaps
tdf['有大波段'] = prev_big >= 0

# 只统计有大波段的信号
gapped = tdf[tdf['有大波段']].copy()
print(f"\n有前大波段的信号数: {len(gapped)} (不再重复计数)")

# 分桶统计
print("\n" + "=" * 100)
print("信号按距前大波段间隔分桶（每个信号只计一次）")
print("=" * 100)

bins = [0, 5, 10, 15, 20, 30, 50, 100, 100000]
labels = ['0-5', '5-10', '10-15', '15-20', '20-30', '30-50', '50-100', '100+']
gapped['间隔桶'] = pd.cut(gapped['距前大波段K数'], bins=bins, labels=labels, include_lowest=True)

print(f"\n{'间隔桶':>8} {'信号数':>6} {'均R':>8} {'胜率':>7} {'PF':>8} {'累计R':>9}")
for bucket in labels:
    sub = gapped[gapped['间隔桶'] == bucket]
    if len(sub) == 0:
        continue
    r = sub['R值']
    avg_r = r.mean()
    win_rate = (r > 0).mean() * 100
    wins = r[r > 0]; losses = r[r < 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float('inf')
    print(f"  {bucket:>6} {len(sub):>6} {avg_r:>8.2f} {win_rate:>6.1f}% {pf:>8.1f} {r.sum():>9.1f}")

# 与大波段涨跌幅的关系
print("\n" + "=" * 100)
print("大波段涨跌幅与后续信号质量")
print("=" * 100)

# 为每个大波段记录涨跌幅
big_amp_map = dict(zip(big_times_sorted, big_swings['amp'].to_numpy()))
# 用时间对齐
big_amp_arr = np.array([big_amp_map[t] for t in big_times_sorted])

prev_amp = np.array([
    big_amp_arr[prev_big[i]] if prev_big[i] >= 0 else np.nan
    for i in range(len(signal_times))
])
tdf['前大波段涨跌幅'] = prev_amp

print(f"\n{'大波段涨跌幅':>12} {'信号数':>6} {'均R':>8} {'胜率':>7} {'PF':>8}")
for lo, hi in [(10,15),(15,20),(20,30),(30,100)]:
    sub = tdf[(tdf['前大波段涨跌幅']>=lo) & (tdf['前大波段涨跌幅']<hi)]
    if len(sub) == 0:
        continue
    r = sub['R值']
    avg_r = r.mean()
    win_rate = (r > 0).mean() * 100
    wins = r[r > 0]; losses = r[r < 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float('inf')
    print(f"  [{lo:>3}%-{hi:>3}%) {len(sub):>6} {avg_r:>8.2f} {win_rate:>6.1f}% {pf:>8.1f}")

# 对比：有大波段 vs 无大波段
print("\n" + "=" * 100)
print("有无大波段信号的对比")
print("=" * 100)

no_big = tdf[~tdf['有大波段']]
print(f"\n有前大波段信号: {len(gapped)}单, 均R={gapped['R值'].mean():.2f}, 胜率={(gapped['R值']>0).mean()*100:.1f}%, 累计R={gapped['R值'].sum():.1f}")
print(f"无前大波段信号: {len(no_big)}单, 均R={no_big['R值'].mean():.2f}, 胜率={(no_big['R值']>0).mean()*100:.1f}%, 累计R={no_big['R值'].sum():.1f}")

# 基准（所有信号）
print(f"全部信号基准: {len(tdf)}单, 均R={tdf['R值'].mean():.2f}, 胜率={(tdf['R值']>0).mean()*100:.1f}%, 累计R={tdf['R值'].sum():.1f}")

print("\n" + "=" * 100)
print("结论")
print("=" * 100)
print("""
修正后的正确结论（待上述数据验证）：
- 之前"均R=3.70"是重复计数导致的错误
- 每个信号只归属最近的前一个大波段后，统计才正确
""")