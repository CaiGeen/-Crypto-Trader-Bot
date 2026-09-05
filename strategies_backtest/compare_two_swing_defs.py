"""对比两种波段口径下的大波段后信号质量（都用修正后的方法：每个信号只归属最近前一个大波段）。

口径1（旧）：6K突破 + KAMA收盘终结，波段涨跌幅%=从突破线算起
口径2（新）：KAMA掉头 + KAMA+ATR突破，amp=从KAMA+ATR算起
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

# 信号数据
tdf = pd.read_csv(os.path.join(HERE, "results", "all_signals_E_prime.csv"), encoding="utf-8-sig")
tdf['信号时间'] = pd.to_datetime(tdf['信号时间'], utc=True)
signal_times = tdf['信号时间'].to_numpy()

def analyze_big_swing_after(big_times_sorted, label):
    """修正方法：每个信号归属最近的前一个大波段，按间隔分桶统计"""
    prev_big = np.searchsorted(big_times_sorted, signal_times, side='left') - 1
    k_gaps = np.array([
        (signal_times[i] - big_times_sorted[prev_big[i]]) / pd.Timedelta(hours=4)
        if prev_big[i] >= 0 else np.inf
        for i in range(len(signal_times))
    ])
    
    has_big = prev_big >= 0
    print(f"\n{label}: 大波段数={len(big_times_sorted)}, 有前大波段信号={has_big.sum()}")
    
    bins = [0, 5, 10, 15, 20, 50, 100, 100000]
    labels = ['0-5', '5-10', '10-15', '15-20', '20-50', '50-100', '100+']
    
    print(f"  {'间隔桶':>8} {'信号数':>6} {'均R':>8} {'胜率':>7} {'PF':>8} {'累计R':>9}")
    for lo, hi, bucket in [(0,5,'0-5'), (5,10,'5-10'), (10,15,'10-15'), (15,20,'15-20'), (20,50,'20-50'), (50,100,'50-100'), (100,100000,'100+')]:
        mask = has_big & (k_gaps >= lo) & (k_gaps < hi)
        sub = tdf[mask]
        if len(sub) == 0:
            continue
        r = sub['R值']
        avg_r = r.mean()
        win_rate = (r > 0).mean() * 100
        wins = r[r > 0]; losses = r[r < 0]
        pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float('inf')
        print(f"  {bucket:>6} {len(sub):>6} {avg_r:>8.2f} {win_rate:>6.1f}% {pf:>8.1f} {r.sum():>9.1f}")
    
    return k_gaps, has_big

print("=" * 100)
print("两种波段口径对比（修正后方法）")
print("=" * 100)

# ========== 口径2（新）：KAMA掉头 + KAMA+ATR突破 ==========
kama_vals = kama(closes, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)

new_swings = []
for i in range(1, n):
    if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
        continue
    if kama_vals[i] > kama_vals[i-1]:
        kama_turn = kama_vals[i]
        breakout_level = kama_turn + atr_vals[i-1]
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
                amp = (peak - breakout_level) / breakout_level * 100
                if amp >= 10:
                    new_swings.append(times[j])
                break

new_times = np.sort(pd.to_datetime(new_swings, utc=True).to_numpy())
print(f"\n【口径2（新）】KAMA掉头+KAMA+ATR突破，>=10%大波段: {len(new_times)}个")
analyze_big_swing_after(new_times, "口径2（新）")

# ========== 口径1（旧）：6K突破 + KAMA收盘终结 ==========
old_csv = os.path.join(HERE, "results", "全时段波段数据集_6K突破_KAMA收盘终结.csv")
if os.path.exists(old_csv):
    old_df = pd.read_csv(old_csv, encoding="utf-8-sig")
    old_df = old_df[old_df['波段涨跌幅%'] >= 10].copy()
    old_times = np.sort(pd.to_datetime(old_df['突破K时间'], utc=True).to_numpy())
    print(f"\n【口径1（旧）】6K突破+KAMA收盘终结，>=10%大波段: {len(old_times)}个")
    analyze_big_swing_after(old_times, "口径1（旧）")

print("\n" + "=" * 100)
print("对比总结")
print("=" * 100)
print("""
关键对比点：
1. 大波段数量差异
2. 0-5根K优质区间的均R差异
3. 垃圾区间的位置差异
4. 100K之后恢复情况
""")