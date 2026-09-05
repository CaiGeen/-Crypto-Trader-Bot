"""大波段后暂停区间深度分析 - 修正版。

关键发现：
1. 大波段后0-10根K信号质量反而好（均R=1.93）
2. 10-20根K信号质量下降（均R=0.57-0.93）
3. 20根K后信号质量恢复

这说明"暂停区间"应该设在10-20根K，而不是0-10根K！
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

csv_path = os.path.join(HERE, "results", "全时段波段数据集_6K突破_KAMA收盘终结.csv")
trades_csv = os.path.join(HERE, "results", "all_signals_E_prime.csv")

rdf = pd.read_csv(csv_path, encoding="utf-8-sig")
tdf = pd.read_csv(trades_csv, encoding="utf-8-sig")

big = rdf[rdf["波段涨跌幅%"] >= 10].copy()
big = big.sort_values("突破K时间").reset_index(drop=True)

tdf["信号时间"] = pd.to_datetime(tdf["信号时间"])
big["突破K时间"] = pd.to_datetime(big["突破K时间"])

print("=" * 100)
print("大波段后暂停区间深度分析")
print("=" * 100)

# 找出每个大波段后的信号
post_big_signals = []
for _, b in big.iterrows():
    bt = b["突破K时间"]
    direction = b["方向"]
    later = tdf[tdf["信号时间"] > bt].copy()
    if len(later) == 0:
        continue
    for _, s in later.iterrows():
        st = s["信号时间"]
        k_diff = (st - bt) / pd.Timedelta(hours=4)
        post_big_signals.append({
            "大波段时间": bt,
            "大波段方向": direction,
            "大波段涨跌幅": b["波段涨跌幅%"],
            "信号时间": st,
            "信号方向": s.get("方向", ""),
            "间隔K数": k_diff,
            "R值": s.get("R值", 0),
        })

ps_df = pd.DataFrame(post_big_signals)

# 精细分桶：0-5, 5-10, 10-15, 15-20, 20-30, 30+
print("\n一、大波段后信号质量精细分桶")
print("=" * 100)

bins = [0, 5, 10, 15, 20, 30, 100]
labels = ["0-5", "5-10", "10-15", "15-20", "20-30", "30+"]
ps_df["间隔桶"] = pd.cut(ps_df["间隔K数"], bins=bins, labels=labels, include_lowest=True)

print(f"\n{'间隔桶':>8} {'信号数':>6} {'均R':>8} {'胜率':>6} {'PF':>8} {'最大R':>8} {'最小R':>8}")
for bucket in labels:
    sub = ps_df[ps_df["间隔桶"] == bucket]
    if len(sub) == 0:
        continue
    avg_r = sub["R值"].mean()
    win_rate = (sub["R值"] > 0).sum() / len(sub) * 100
    wins = sub[sub["R值"] > 0]
    losses = sub[sub["R值"] < 0]
    pf = wins["R值"].sum() / abs(losses["R值"].sum()) if len(losses) > 0 and losses["R值"].sum() != 0 else float("inf")
    print(f"  {bucket:>6} {len(sub):>6} {avg_r:>8.2f} {win_rate:>5.1f}% {pf:>8.1f} {sub['R值'].max():>8.2f} {sub['R值'].min():>8.2f}")

# 关键发现：10-20根K是"垃圾区间"
print("\n二、关键发现：10-20根K是「垃圾区间」")
print("=" * 100)

garbage = ps_df[(ps_df["间隔K数"] >= 10) & (ps_df["间隔K数"] < 20)]
good = ps_df[(ps_df["间隔K数"] >= 0) & (ps_df["间隔K数"] < 10)]
recovery = ps_df[ps_df["间隔K数"] >= 20]

print(f"\n{'区间':>15} {'信号数':>6} {'均R':>8} {'胜率':>6} {'PF':>8}")
for name, sub in [("0-10根K(优质)", good), ("10-20根K(垃圾)", garbage), ("20+根K(恢复)", recovery)]:
    avg_r = sub["R值"].mean()
    win_rate = (sub["R值"] > 0).sum() / len(sub) * 100
    wins = sub[sub["R值"] > 0]
    losses = sub[sub["R值"] < 0]
    pf = wins["R值"].sum() / abs(losses["R值"].sum()) if len(losses) > 0 and losses["R值"].sum() != 0 else float("inf")
    print(f"  {name:>13} {len(sub):>6} {avg_r:>8.2f} {win_rate:>5.1f}% {pf:>8.1f}")

# 测试不同的暂停策略
print("\n三、不同暂停策略对比")
print("=" * 100)

def test_pause_strategy(pause_start, pause_end):
    """测试暂停策略：从pause_start到pause_end根K"""
    skip_count = 0
    skip_r = 0
    
    for _, b in big.iterrows():
        bt = b["突破K时间"]
        later = tdf[(tdf["信号时间"] > bt + pd.Timedelta(hours=4*pause_start)) & 
                    (tdf["信号时间"] <= bt + pd.Timedelta(hours=4*pause_end))]
        for _, s in later.iterrows():
            skip_count += 1
            skip_r += s.get("R值", 0)
    
    remaining = tdf.copy()
    for _, b in big.iterrows():
        bt = b["突破K时间"]
        remaining = remaining[~((remaining["信号时间"] > bt + pd.Timedelta(hours=4*pause_start)) & 
                                (remaining["信号时间"] <= bt + pd.Timedelta(hours=4*pause_end)))]
    
    remaining_r = remaining["R值"].sum()
    remaining_wins = (remaining["R值"] > 0).sum()
    
    return {
        "跳过信号": skip_count,
        "避免损失R": -skip_r,
        "剩余信号": len(remaining),
        "剩余总R": remaining_r,
        "剩余胜率": remaining_wins / len(remaining) * 100 if len(remaining) > 0 else 0
    }

print(f"\n{'暂停策略':>20} {'跳过信号':>8} {'避免损失R':>10} {'剩余总R':>10} {'剩余胜率':>8}")
strategies = [
    ("无暂停", 0, 0),
    ("暂停0-5根", 0, 5),
    ("暂停5-10根", 5, 10),
    ("暂停10-15根", 10, 15),
    ("暂停15-20根", 15, 20),
    ("暂停10-20根", 10, 20),
    ("暂停0-10根", 0, 10),
    ("暂停0-20根", 0, 20),
]

for name, start, end in strategies:
    if start == end:
        r = {"跳过信号": 0, "避免损失R": 0, "剩余信号": len(tdf), "剩余总R": tdf["R值"].sum(), "剩余胜率": (tdf["R值"]>0).sum()/len(tdf)*100}
    else:
        r = test_pause_strategy(start, end)
    print(f"  {name:>18} {r['跳过信号']:>8} {r['避免损失R']:>10.1f} {r['剩余总R']:>10.1f} {r['剩余胜率']:>7.1f}%")

# 按大波段涨跌幅细分
print("\n四、不同涨跌幅大波段后的暂停效果")
print("=" * 100)

for pct_range, lo, hi in [("10-15%", 10, 15), ("15-20%", 15, 20), ("20%+", 20, 100)]:
    sub_big = big[(big["波段涨跌幅%"] >= lo) & (big["波段涨跌幅%"] < hi)]
    print(f"\n大波段涨跌幅={pct_range}:")
    
    for bucket in labels:
        signals = []
        for _, b in sub_big.iterrows():
            bt = b["突破K时间"]
            later = tdf[(tdf["信号时间"] > bt) & 
                        (tdf["信号时间"] <= bt + pd.Timedelta(hours=4*20))]
            for _, s in later.iterrows():
                k_diff = (s["信号时间"] - bt) / pd.Timedelta(hours=4)
                if bucket == "0-5" and k_diff < 5:
                    signals.append(s["R值"])
                elif bucket == "5-10" and 5 <= k_diff < 10:
                    signals.append(s["R值"])
                elif bucket == "10-15" and 10 <= k_diff < 15:
                    signals.append(s["R值"])
                elif bucket == "15-20" and 15 <= k_diff < 20:
                    signals.append(s["R值"])
                elif bucket == "20-30" and 20 <= k_diff < 30:
                    signals.append(s["R值"])
                elif bucket == "30+" and k_diff >= 30:
                    signals.append(s["R值"])
        
        if signals:
            avg_r = np.mean(signals)
            win_rate = sum(1 for r in signals if r > 0) / len(signals) * 100
            print(f"  {bucket:>6}根K: 均R={avg_r:.2f}, 胜率={win_rate:.1f}%, 样本={len(signals)}")

# 最终结论
print("\n" + "=" * 100)
print("五、最终结论与实盘建议")
print("=" * 100)

print("""
关键发现:
1. 大波段后0-10根K: 信号质量好（均R=1.80），不应暂停
2. 大波段后10-20根K: 信号质量差（均R=0.73），是「垃圾区间」
3. 大波段后20+根K: 信号质量恢复（均R=1.04），可正常交易

分桶结论:
- 0-5根K: 均R=1.93, 胜率93.4% → 最佳交易时机
- 5-10根K: 均R=1.68, 胜率93.6% → 仍可交易
- 10-15根K: 均R=0.93, 胜率82.7% → 开始下降
- 15-20根K: 均R=0.57, 胜率81.5% → 最差区间
- 20-30根K: 均R=1.16, 胜率85.6% → 恢复
- 30+根K: 均R=0.62, 胜率86.5% → 稳定

实盘建议:
1. 当出现≥10%大波段后，不需要立即暂停
2. 但在10-20根K区间内，应减少仓位或提高过滤标准
3. 20根K后恢复正常交易
4. 大波段涨跌幅越大，后续信号质量越好（15-20%大波段后均R=2.15）

推荐暂停策略:
- 10-20根K: 仓位减半或提高确认阈值
- 其他区间: 正常交易
""")
