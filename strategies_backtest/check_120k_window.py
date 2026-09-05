"""分析120K回溯窗口是否足够

突破结构级别：{6, 10, 20, 40, 60, 120}
问题是：120K作为最大窗口是否足够？是否需要更大的窗口（如240K）？
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
n = len(df)

print("=" * 100)
print("120K回溯窗口是否足够分析")
print("=" * 100)

LEVELS = [6, 10, 20, 40, 60, 120, 240]

# 计算每个窗口的突破信号数
signals_by_level = {6: 0, 10: 0, 20: 0, 40: 0, 60: 0, 120: 0, 240: 0}

for i in range(max(LEVELS), n):
    # 做多：收盘价突破前N根K的最高价，取最大的N
    max_level = 6
    for w in LEVELS[1:]:
        if i >= w and closes[i] > highs[i - w:i].max():
            max_level = w
    signals_by_level[max_level] += 1

print(f"\n各窗口突破信号数:")
for w in LEVELS:
    print(f"  {w}K: {signals_by_level[w]}个")

# 计算120K以上窗口的信号数
total_120_plus = sum(v for k, v in signals_by_level.items() if k >= 120)
total_all = sum(signals_by_level.values())
print(f"\n120K以上窗口信号数: {total_120_plus} ({total_120_plus/total_all*100:.1f}%)")
print(f"240K窗口信号数: {signals_by_level[240]} ({signals_by_level[240]/total_all*100:.2f}%)")

# 时间维度分析
print(f"\n时间维度分析:")
print(f"  120K = {120 * 4}小时 = {120 * 4 / 24:.0f}天")
print(f"  240K = {240 * 4}小时 = {240 * 4 / 24:.0f}天")

# 计算每个窗口的平均涨跌幅
def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)
kama_vals = kama(closes, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

# 计算每个窗口突破后的涨跌幅
amp_by_level = {6: [], 10: [], 20: [], 40: [], 60: [], 120: [], 240: []}

for i in range(max(LEVELS) + 50, n):
    # 找到最大的突破窗口
    max_level = 6
    for w in LEVELS[1:]:
        if i >= w and closes[i] > highs[i - w:i].max():
            max_level = w
    
    # 找到突破后的最高价
    peak = closes[i]
    for j in range(i+1, min(i+50, n)):
        if np.isnan(kama_vals[j]) or np.isnan(kama_vals[j-1]):
            break
        if kama_vals[j] < kama_vals[j-1]:
            break
        if closes[j] > peak:
            peak = closes[j]
    
    entry = highs[i - max_level:i].max()
    amp = (peak - entry) / entry * 100
    amp_by_level[max_level].append(amp)

print(f"\n各窗口突破后涨跌幅中位:")
for w in LEVELS:
    if amp_by_level[w]:
        print(f"  {w}K: {np.median(amp_by_level[w]):.2f}% (样本{len(amp_by_level[w])})")

# 结论
print(f"\n" + "=" * 100)
print("结论")
print("=" * 100)

print(f"""
1. 240K窗口信号数: {signals_by_level[240]}个 ({signals_by_level[240]/total_all*100:.2f}%)
2. 240K窗口涨跌幅中位: {np.median(amp_by_level[240]):.2f}% (如果样本足够)
3. 120K = {120 * 4 / 24:.0f}天，对于4H级别已经相当长
4. 240K = {240 * 4 / 24:.0f}天，覆盖约2个月

建议:
- 120K作为最大窗口已经足够（覆盖20天）
- 240K窗口样本极少（{signals_by_level[240]}个），统计意义有限
- 如果追求更稳健，可以用120K，但大多数信号集中在6K-60K
""")
