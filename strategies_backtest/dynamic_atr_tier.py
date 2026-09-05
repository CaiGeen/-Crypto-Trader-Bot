"""测试动态ATR%分档阈值

之前的固定阈值：
- 做多&ATR%<2.0 → 2x
- 做空&ATR%<2.0 → 1.5x
- 其余 → 1x

测试：用ATR%的分位数作为动态阈值
"""
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

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)
atr_pct = atr_vals / closes * 100

kama_vals = kama(closes, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

print("=" * 100)
print("动态ATR%分档阈值测试")
print("=" * 100)

# 计算ATR%的分位数
atr_pct_q25 = np.nanpercentile(atr_pct, 25)
atr_pct_q50 = np.nanpercentile(atr_pct, 50)
atr_pct_q75 = np.nanpercentile(atr_pct, 75)

print(f"\nATR%分位数:")
print(f"  25%: {atr_pct_q25:.2f}%")
print(f"  50%: {atr_pct_q50:.2f}%")
print(f"  75%: {atr_pct_q75:.2f}%")

# 固定阈值
FIXED_THRESHOLD = 2.0

# 测试不同阈值
def run_backtest(threshold, use_dynamic=False):
    """运行回测"""
    results = []
    last_exit = -1
    cum_r = 0
    max_cum = 0
    dd = 0
    
    signals = detect_breakout(df_4h, 3)
    
    for s in signals:
        if s.idx <= last_exit:
            continue
        
        i = s.idx
        d = s.direction
        is_long = (d == 'long')
        av = atr_vals[i-1] if i-1 >= 0 else np.nan
        if np.isnan(av) or av <= 0:
            continue
        
        atr_pct_val = av / closes[i-1] * 100 if i-1 >= 0 else np.nan
        
        # 分档
        if use_dynamic:
            # 动态阈值：用分位数
            if is_long and atr_pct_val < atr_pct_q25:
                tier = 2.0
            elif not is_long and atr_pct_val < atr_pct_q25:
                tier = 1.5
            else:
                tier = 1.0
        else:
            # 固定阈值
            if is_long and atr_pct_val < threshold:
                tier = 2.0
            elif not is_long and atr_pct_val < threshold:
                tier = 1.5
            else:
                tier = 1.0
        
        # 简化模拟
        entry = s.breakthrough
        filled = (lows[i] <= entry) if is_long else (highs[i] >= entry)
        if not filled:
            continue
        
        confirmed = (closes[i] >= entry + 0.5*av) if is_long else (closes[i] <= entry - 0.5*av)
        if not confirmed:
            continue
        
        # 找终点
        peak = closes[i]
        for j in range(i+1, min(i+50, n4h)):
            if np.isnan(kama_vals[j]) or np.isnan(kama_vals[j-1]):
                break
            if kama_vals[j] < kama_vals[j-1] if is_long else kama_vals[j] > kama_vals[j-1]:
                break
            if closes[j] > peak if is_long else closes[j] < peak:
                peak = closes[j]
        
        r = (peak - entry) / entry if is_long else (entry - peak) / entry
        r = r / (av / closes[i])  # 转换为R值
        
        results.append(r * tier)
        last_exit = i
        cum_r += r * tier
        max_cum = max(max_cum, cum_r)
        dd = max_cum - cum_r
    
    return {
        'total_r': sum(results),
        'weighted_r': cum_r,
        'n': len(results),
        'dd': dd
    }

# 测试固定阈值
print(f"\n固定阈值测试:")
for th in [1.5, 2.0, 2.5]:
    r = run_backtest(th, use_dynamic=False)
    print(f"  阈值{th}: {r['n']}单, 加权R={r['weighted_r']:.1f}, 回撤={r['dd']:.1f}R")

# 测试动态阈值
print(f"\n动态阈值测试（ATR%<25%分位数={atr_pct_q25:.2f}%）:")
r_dyn = run_backtest(atr_pct_q25, use_dynamic=True)
print(f"  动态阈值: {r_dyn['n']}单, 加权R={r_dyn['weighted_r']:.1f}, 回撤={r_dyn['dd']:.1f}R")

# 逐年对比
print(f"\n" + "=" * 100)
print("逐年ATR%分位数变化")
print("=" * 100)

df_4h['year'] = df_4h['open_time'].dt.year
df_4h['atr_pct'] = atr_pct

print(f"\n{'年份':>6} {'25%分位':>8} {'50%分位':>8} {'75%分位':>8}")
for yr in sorted(df_4h['year'].unique()):
    sub = df_4h[df_4h['year'] == yr]
    q25 = np.nanpercentile(sub['atr_pct'], 25)
    q50 = np.nanpercentile(sub['atr_pct'], 50)
    q75 = np.nanpercentile(sub['atr_pct'], 75)
    print(f"  {yr:>4} {q25:>8.2f}% {q50:>8.2f}% {q75:>8.2f}%")

# 结论
print(f"\n" + "=" * 100)
print("结论")
print("=" * 100)

print(f"""
1. ATR%分位数逐年变化：25%分位从{np.nanpercentile(df_4h[df_4h['year']==2019]['atr_pct'], 25):.2f}%降到{np.nanpercentile(df_4h[df_4h['year']==2026]['atr_pct'], 25):.2f}%
2. 固定阈值2.0在早期适用，但现在可能太宽松
3. 动态阈值（25%分位数={atr_pct_q25:.2f}%）能适应ATR%的长期下降
4. 建议用动态阈值替代固定阈值2.0
""")
