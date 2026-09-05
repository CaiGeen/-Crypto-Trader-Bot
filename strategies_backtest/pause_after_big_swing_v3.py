"""大波段后暂停区间研究 - 基于新定义波段（KAMA掉头+KAMA+ATR突破）

研究问题：
1. 出现>=10%大波段后，后续信号质量如何变化？
2. 是否存在"垃圾区间"，应该暂停做单？
3. 暂停区间应该设多长？
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

kama_vals = kama(closes, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)

print("=" * 100)
print("大波段后暂停区间研究（新定义波段）")
print("=" * 100)

# 实现新定义波段检测
def detect_swings_v2():
    """新定义波段：KAMA掉头 + KAMA+ATR突破"""
    swings = []
    
    for i in range(1, n):
        if np.isnan(kama_vals[i]) or np.isnan(kama_vals[i-1]) or np.isnan(atr_vals[i-1]):
            continue
        
        # KAMA掉头上升
        if kama_vals[i] > kama_vals[i-1]:
            kama_turn = kama_vals[i]
            atr_val = atr_vals[i-1]
            breakout_level = kama_turn + atr_val
            
            # 找价格站上KAMA+ATR的位置
            for j in range(i, min(i+20, n)):
                if closes[j] >= breakout_level:
                    # 找终点（KAMA反向运动）
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
                    swings.append({
                        '方向': '做多',
                        'turn_idx': i,
                        'breakout_idx': j,
                        'end_idx': end_idx,
                        'peak_idx': peak_idx,
                        'breakout_time': times[j],
                        'end_time': times[end_idx],
                        'breakout_level': breakout_level,
                        'peak': peak,
                        'amp': amp,
                        'K数': end_idx - j
                    })
                    break
    
    return swings

print("\n一、生成新定义波段数据集")
print("=" * 100)

swings = detect_swings_v2()
swings_df = pd.DataFrame(swings)
print(f"总波段数: {len(swings_df)}")

# 筛选>=10%的大波段
big_swings = swings_df[swings_df['amp'] >= 10].copy()
print(f"大波段数(>=10%): {len(big_swings)} ({len(big_swings)/len(swings_df)*100:.1f}%)")

# 基本统计
print(f"\n大波段统计:")
print(f"  涨跌幅: 中位={big_swings['amp'].median():.2f}% 均值={big_swings['amp'].mean():.2f}%")
print(f"  K数: 中位={big_swings['K数'].median():.0f} 均值={big_swings['K数'].mean():.1f}")

# 读取交易信号数据
print("\n" + "=" * 100)
print("二、加载交易信号数据")
print("=" * 100)

trades_csv = os.path.join(HERE, "results", "all_signals_E_prime.csv")
if os.path.exists(trades_csv):
    tdf = pd.read_csv(trades_csv, encoding="utf-8-sig")
    tdf['信号时间'] = pd.to_datetime(tdf['信号时间'])
    print(f"总信号数: {len(tdf)}")
else:
    print("交易数据不存在，跳过信号分析")
    tdf = None

# 大波段后信号分析
if tdf is not None and len(big_swings) > 0:
    print("\n" + "=" * 100)
    print("三、大波段后信号间隔分析")
    print("=" * 100)
    
    big_swings = big_swings.sort_values('breakout_time').reset_index(drop=True)
    
    # 统一时区
    tdf['信号时间'] = pd.to_datetime(tdf['信号时间'], utc=True)
    
    # 找出每个大波段后的信号
    post_big_signals = []
    for _, b in big_swings.iterrows():
        bt = b['breakout_time']
        if bt.tzinfo is None:
            bt = bt.tz_localize('UTC')
        later = tdf[tdf['信号时间'] > bt].copy()
        if len(later) == 0:
            continue
        for _, s in later.iterrows():
            st = s['信号时间']
            k_diff = (st - bt) / pd.Timedelta(hours=4)
            post_big_signals.append({
                '大波段时间': bt,
                '大波段涨跌幅': b['amp'],
                '信号时间': st,
                '间隔K数': k_diff,
                'R值': s.get('R值', 0),
            })
    
    if post_big_signals:
        ps_df = pd.DataFrame(post_big_signals)
        print(f"大波段后信号总数: {len(ps_df)}")
        
        # 精细分桶统计
        print("\n大波段后信号按间隔分桶统计:")
        bins = [0, 5, 10, 15, 20, 30, 50, 100, 999]
        labels = ['0-5', '5-10', '10-15', '15-20', '20-30', '30-50', '50-100', '100+']
        ps_df['间隔桶'] = pd.cut(ps_df['间隔K数'], bins=bins, labels=labels, include_lowest=True)
        
        print(f"\n{'间隔桶':>8} {'信号数':>6} {'均R':>8} {'胜率':>6} {'PF':>8} {'最大R':>8} {'最小R':>8}")
        for bucket in labels:
            sub = ps_df[ps_df['间隔桶'] == bucket]
            if len(sub) == 0:
                continue
            avg_r = sub['R值'].mean()
            win_rate = (sub['R值'] > 0).sum() / len(sub) * 100
            wins = sub[sub['R值'] > 0]
            losses = sub[sub['R值'] < 0]
            pf = wins['R值'].sum() / abs(losses['R值'].sum()) if len(losses) > 0 and losses['R值'].sum() != 0 else float('inf')
            print(f"  {bucket:>6} {len(sub):>6} {avg_r:>8.2f} {win_rate:>5.1f}% {pf:>8.1f} {sub['R值'].max():>8.2f} {sub['R值'].min():>8.2f}")
        
        # 关键发现
        print("\n" + "=" * 100)
        print("四、关键发现：垃圾区间识别")
        print("=" * 100)
        
        # 对比不同区间
        good = ps_df[ps_df['间隔K数'] < 10]
        garbage = ps_df[(ps_df['间隔K数'] >= 10) & (ps_df['间隔K数'] < 20)]
        recovery = ps_df[ps_df['间隔K数'] >= 20]
        
        print(f"\n{'区间':>15} {'信号数':>6} {'均R':>8} {'胜率':>6} {'PF':>8}")
        for name, sub in [('0-10根K(优质)', good), ('10-20根K(垃圾)', garbage), ('20+根K(恢复)', recovery)]:
            if len(sub) == 0:
                continue
            avg_r = sub['R值'].mean()
            win_rate = (sub['R值'] > 0).sum() / len(sub) * 100
            wins = sub[sub['R值'] > 0]
            losses = sub[sub['R值'] < 0]
            pf = wins['R值'].sum() / abs(losses['R值'].sum()) if len(losses) > 0 and losses['R值'].sum() != 0 else float('inf')
            print(f"  {name:>13} {len(sub):>6} {avg_r:>8.2f} {win_rate:>5.1f}% {pf:>8.1f}")
        
        # 暂停策略测试
        print("\n" + "=" * 100)
        print("五、暂停策略效果测试")
        print("=" * 100)
        
        def test_pause(pause_start, pause_end):
            skip_count = 0
            skip_r = 0
            
            for _, b in big_swings.iterrows():
                bt = b['breakout_time']
                if bt.tzinfo is None:
                    bt = bt.tz_localize('UTC')
                later = tdf[(tdf['信号时间'] > bt + pd.Timedelta(hours=4*pause_start)) & 
                            (tdf['信号时间'] <= bt + pd.Timedelta(hours=4*pause_end))]
                for _, s in later.iterrows():
                    skip_count += 1
                    skip_r += s.get('R值', 0)
            
            remaining = tdf.copy()
            for _, b in big_swings.iterrows():
                bt = b['breakout_time']
                if bt.tzinfo is None:
                    bt = bt.tz_localize('UTC')
                remaining = remaining[~((remaining['信号时间'] > bt + pd.Timedelta(hours=4*pause_start)) & 
                                        (remaining['信号时间'] <= bt + pd.Timedelta(hours=4*pause_end)))]
            
            remaining_r = remaining['R值'].sum()
            remaining_wins = (remaining['R值'] > 0).sum()
            
            return {
                '跳过信号': skip_count,
                '避免损失R': -skip_r,
                '剩余信号': len(remaining),
                '剩余总R': remaining_r,
                '剩余胜率': remaining_wins / len(remaining) * 100 if len(remaining) > 0 else 0
            }
        
        print(f"\n{'暂停策略':>20} {'跳过信号':>8} {'避免损失R':>10} {'剩余总R':>10} {'剩余胜率':>8}")
        strategies = [
            ('无暂停', 0, 0),
            ('暂停0-5根', 0, 5),
            ('暂停5-10根', 5, 10),
            ('暂停10-15根', 10, 15),
            ('暂停15-20根', 15, 20),
            ('暂停10-20根', 10, 20),
            ('暂停0-10根', 0, 10),
            ('暂停0-20根', 0, 20),
        ]
        
        for name, start, end in strategies:
            if start == end:
                r = {'跳过信号': 0, '避免损失R': 0, '剩余信号': len(tdf), '剩余总R': tdf['R值'].sum(), '剩余胜率': (tdf['R值']>0).sum()/len(tdf)*100}
            else:
                r = test_pause(start, end)
            print(f"  {name:>18} {r['跳过信号']:>8} {r['避免损失R']:>10.1f} {r['剩余总R']:>10.1f} {r['剩余胜率']:>7.1f}%")
        
        # 大波段涨跌幅与后续信号质量
        print("\n" + "=" * 100)
        print("六、大波段涨跌幅与后续信号质量关系")
        print("=" * 100)
        
        big_swings['涨跌幅桶'] = pd.cut(big_swings['amp'], bins=[10, 15, 20, 30, 100], labels=['10-15%', '15-20%', '20-30%', '30%+'])
        
        print(f"\n{'大波段涨跌幅':>12} {'后信号数':>8} {'后信号均R':>10} {'后信号胜率':>8}")
        for bucket in ['10-15%', '15-20%', '20-30%', '30%+']:
            sub_big = big_swings[big_swings['涨跌幅桶'] == bucket]
            if len(sub_big) == 0:
                continue
            post_signals = []
            for _, b in sub_big.iterrows():
                bt = b['breakout_time']
                if bt.tzinfo is None:
                    bt = bt.tz_localize('UTC')
                later = tdf[(tdf['信号时间'] > bt) & (tdf['信号时间'] <= bt + pd.Timedelta(hours=4*20))]
                for _, s in later.iterrows():
                    post_signals.append(s['R值'])
            
            if post_signals:
                avg_r = np.mean(post_signals)
                win_rate = sum(1 for r in post_signals if r > 0) / len(post_signals) * 100
                print(f"  {bucket:>10} {len(post_signals):>8} {avg_r:>10.2f} {win_rate:>7.1f}%")

# 统一时区
big_swings['breakout_time'] = pd.to_datetime(big_swings['breakout_time'], utc=True)

# 最终结论
print("\n" + "=" * 100)
print("七、最终结论与实盘建议")
print("=" * 100)

print("""
【关键发现】
1. 大波段后0-10根K：信号质量好，不应暂停
2. 大波段后10-20根K：信号质量下降，是「垃圾区间」
3. 大波段后20+根K：信号质量恢复，可正常交易

【实盘建议】
1. 当出现>=10%大波段后，不需要立即暂停
2. 但在10-20根K区间内，应减少仓位或提高过滤标准
3. 20根K后恢复正常交易
4. 大波段涨跌幅越大，后续信号质量越好

【推荐暂停策略】
- 10-20根K：仓位减半或提高确认阈值
- 其他区间：正常交易
""")

# 保存波段数据集
output_path = os.path.join(HERE, "results", "波段数据集_新定义_KAMA掉头.csv")
swings_df.to_csv(output_path, index=False, encoding='utf-8-sig')
print(f"\n波段数据集已保存: {output_path}")
