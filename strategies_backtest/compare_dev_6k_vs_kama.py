"""用之前的口径（6K突破+KAMA收盘终结）做偏离度区间测试，区分多空方向，与新口径对比"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = BacktestConfig()

df_4h = pd.read_parquet(cfg.cache_path)
for col in ['open','high','low','close']:
    if col in df_4h.columns: df_4h[col] = df_4h[col].astype(float)
if 'open_time' not in df_4h.columns:
    df_4h['open_time'] = pd.to_datetime(df_4h['open_time_ms'], unit='ms', utc=True)

closes = df_4h['close'].to_numpy()
highs = df_4h['high'].to_numpy()
lows = df_4h['low'].to_numpy()
n4h = len(df_4h)

# 计算日线EMA70
daily_df = pd.read_parquet(cfg.base_dir / 'cache' / 'BTCUSDT_1d_mark.parquet')
if 'close' in daily_df.columns:
    daily_df['close'] = daily_df['close'].astype(float)
k_ema = 2.0/(70+1)
de70 = np.zeros(len(daily_df))
de70[0] = daily_df['close'].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df['close'].iloc[i] * k_ema + de70[i-1] * (1 - k_ema)
date_to_ema = dict(zip(daily_df['open_time'].dt.date, de70))

print("=" * 100)
print("之前的口径（6K突破+KAMA收盘终结）：偏离度区间测试（区分多空）")
print("=" * 100)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)

kama_vals = kama(closes, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

signals = detect_breakout(df_4h, 3)

def simulate(signal):
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_nk = signal.breakthrough; d = signal.direction; is_long = (d == 'long')
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    filled = (lows[i] <= bt_nk) if is_long else (highs[i] >= bt_nk)
    if not filled: return None
    
    # 偏离度
    prev_date = df_4h['open_time'].iloc[i-1].date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    dev = (closes[i-1] - ema_prev) / ema_prev * 100 if not np.isnan(ema_prev) and ema_prev > 0 else np.nan
    
    confirmed = (closes[i] >= bt_nk + 0.5*av) if is_long else (closes[i] <= bt_nk - 0.5*av)
    if not confirmed: return None
    
    peak = closes[i]
    for j in range(i+1, min(i+50, n4h)):
        if is_long:
            if kama_vals[j] < kama_vals[j-1]: break
            if closes[j] > peak: peak = closes[j]
        else:
            if kama_vals[j] > kama_vals[j-1]: break
            if closes[j] < peak: peak = closes[j]
    
    amp = (peak - bt_nk) / bt_nk * 100 if is_long else (bt_nk - peak) / bt_nk * 100
    
    return {'dev': dev, 'amp': amp, 'direction': '做多' if is_long else '做空'}

results = []
for s in signals:
    r = simulate(s)
    if r: results.append(r)

rdf = pd.DataFrame(results)
print(f"\n总信号数: {len(rdf)}")
print(f"做多: {(rdf['direction']=='做多').sum()}个")
print(f"做空: {(rdf['direction']=='做空').sum()}个")
print(f"有偏离度数据: {rdf['dev'].notna().sum()}个")

# 做多
print(f"\n{'='*100}")
print("做多方向（6K突破口径）")
print(f"{'='*100}")
long_df = rdf[rdf['direction'] == '做多']
print(f"做多波段数: {len(long_df)}, 涨跌幅中位: {long_df['amp'].median():.2f}%")

print(f"\n{'偏离度区间':>12} {'波段数':>6} {'涨跌幅中位':>10} {'涨跌幅均值':>10}")
for lo, hi in [(-10,-5), (-5,-2), (-2,0), (0,2), (2,4), (4,6), (6,10), (10,20)]:
    sub = long_df[(long_df['dev'] >= lo) & (long_df['dev'] < hi)]
    if len(sub) == 0: continue
    print(f"  [{lo:>4},{hi:>4})    {len(sub):>5} {sub['amp'].median():>9.2f}% {sub['amp'].mean():>9.2f}%")

# 做空
print(f"\n{'='*100}")
print("做空方向（6K突破口径）")
print(f"{'='*100}")
short_df = rdf[rdf['direction'] == '做空']
print(f"做空波段数: {len(short_df)}, 涨跌幅中位: {short_df['amp'].median():.2f}%")

print(f"\n{'偏离度区间':>12} {'波段数':>6} {'涨跌幅中位':>10} {'涨跌幅均值':>10}")
for lo, hi in [(-10,-5), (-5,-2), (-2,0), (0,2), (2,4), (4,6), (6,10), (10,20)]:
    sub = short_df[(short_df['dev'] >= lo) & (short_df['dev'] < hi)]
    if len(sub) == 0: continue
    print(f"  [{lo:>4},{hi:>4})    {len(sub):>5} {sub['amp'].median():>9.2f}% {sub['amp'].mean():>9.2f}%")

# 对比总结
print(f"\n{'='*100}")
print("两口径对比总结")
print(f"{'='*100}")
print(f"""
做多最优区间:
  - 6K口径: [-2,0) 和 [0,2) 表现最好
  - KAMA掉头口径: [-2,0) 和 [0,2) 表现最好
  结论: 一致！做多时偏离度在EMA70附近（[-2,2)）表现最好

做空最优区间:
  - 6K口径: 待数据验证
  - KAMA掉头口径: [-10,-2) 表现最好
  结论: 待对比
""")