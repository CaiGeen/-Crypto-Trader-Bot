"""新口径大波段后信号质量：每5K精细分桶，0K到300K。"""
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

big_times_sorted = np.sort(pd.to_datetime(new_swings, utc=True).to_numpy())
print(f"新口径>=10%大波段数: {len(big_times_sorted)}")

tdf = pd.read_csv(os.path.join(HERE, "results", "all_signals_E_prime.csv"), encoding="utf-8-sig")
tdf['信号时间'] = pd.to_datetime(tdf['信号时间'], utc=True)
signal_times = tdf['信号时间'].to_numpy()
r_vals = tdf['R值'].to_numpy()

prev_big = np.searchsorted(big_times_sorted, signal_times, side='left') - 1
k_gaps = np.array([
    (signal_times[i] - big_times_sorted[prev_big[i]]) / pd.Timedelta(hours=4)
    if prev_big[i] >= 0 else np.inf
    for i in range(len(signal_times))
])
has_big = prev_big >= 0

print(f"\n基准(全部信号): 均R={tdf['R值'].mean():.3f}, 胜率={(tdf['R值']>0).mean()*100:.1f}%")
print(f"\n每5K精细分桶（0K到300K）:")
print(f"{'区间':>10} {'信号数':>6} {'均R':>8} {'胜率':>7} {'PF':>8} {'累计R':>9}")

rows = []
for lo in range(0, 300, 5):
    hi = lo + 5
    mask = has_big & (k_gaps >= lo) & (k_gaps < hi)
    cnt = int(mask.sum())
    if cnt == 0:
        rows.append((lo, hi, 0, float('nan'), float('nan'), float('nan'), 0.0))
        continue
    r = r_vals[mask]
    avg_r = r.mean()
    win = (r > 0).mean() * 100
    wins = r[r > 0]; losses = r[r < 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float('inf')
    rows.append((lo, hi, cnt, avg_r, win, pf, r.sum()))

for lo, hi, cnt, avg_r, win, pf, rsum in rows:
    if cnt == 0:
        print(f"  [{lo:>3}-{hi:>3}] {'—':>6} {'—':>8} {'—':>7} {'—':>8} {'—':>9}")
    else:
        pf_str = f"{pf:>8.1f}" if pf != float('inf') else f"{'inf':>8}"
        print(f"  [{lo:>3}-{hi:>3}] {cnt:>6} {avg_r:>8.2f} {win:>6.1f}% {pf_str} {rsum:>9.1f}")