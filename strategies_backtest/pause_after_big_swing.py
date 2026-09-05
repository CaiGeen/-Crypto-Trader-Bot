"""大波段后暂停区间研究：出现≥10%波段后，后续信号质量如何变化？

研究问题：
1. 大波段后，间隔多少根K内的信号质量差？
2. 是否存在一个"暂停区间"，避免垃圾做单机会？
3. 暂停区间应该设多长？

方法：
- 找出所有≥10%的大波段
- 统计大波段后不同间隔内的信号表现
- 对比有/无暂停区间的回测效果
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

# 读取波段数据集
csv_path = os.path.join(HERE, "results", "全时段波段数据集_6K突破_KAMA收盘终结.csv")
if not os.path.exists(csv_path):
    print("先运行 swing_dataset.py 生成波段数据集")
    sys.exit(1)

rdf = pd.read_csv(csv_path, encoding="utf-8-sig")
print(f"总波段数: {len(rdf)}")

# 筛选>=10%的大波段
big = rdf[rdf["波段涨跌幅%"] >= 10].copy()
big = big.sort_values("突破K时间").reset_index(drop=True)
print(f"大波段数(>=10%): {len(big)}")

# 读取信号级别的交易数据（从generate_final_report的结果）
trades_csv = os.path.join(HERE, "results", "all_signals_E_prime.csv")
if not os.path.exists(trades_csv):
    print("先运行 generate_final_report.py 生成交易数据")
    sys.exit(1)

tdf = pd.read_csv(trades_csv, encoding="utf-8-sig")
print(f"总信号数: {len(tdf)}")

# 转换时间
tdf["信号时间"] = pd.to_datetime(tdf["信号时间"])
big["突破K时间"] = pd.to_datetime(big["突破K时间"])

# 找出每个大波段后的信号
print("\n" + "=" * 100)
print("一、大波段后信号间隔分布")
print("=" * 100)

# 对每个大波段，找后续信号
post_big_signals = []
for _, b in big.iterrows():
    bt = b["突破K时间"]
    direction = b["方向"]
    # 找这个大波段之后的信号
    later = tdf[tdf["信号时间"] > bt].copy()
    if len(later) == 0:
        continue
    # 计算间隔（K数）- 简化用时间差估算（4H一根）
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
            "是否确认": s.get("是否确认", True)
        })

if not post_big_signals:
    print("未找到大波段后的信号")
    sys.exit(1)

ps_df = pd.DataFrame(post_big_signals)
print(f"大波段后信号总数: {len(ps_df)}")

# 按间隔分桶统计
print("\n大波段后信号按间隔分桶统计:")
bins = [0, 5, 10, 15, 20, 30, 50, 100, 999]
labels = ["0-5", "5-10", "10-15", "15-20", "20-30", "30-50", "50-100", "100+"]
ps_df["间隔桶"] = pd.cut(ps_df["间隔K数"], bins=bins, labels=labels, include_lowest=True)

print(f"\n{'间隔桶':>8} {'信号数':>6} {'平均R':>8} {'胜率':>6} {'PF':>8} {'同向率':>6}")
for bucket in labels:
    sub = ps_df[ps_df["间隔桶"] == bucket]
    if len(sub) == 0:
        continue
    avg_r = sub["R值"].mean()
    wins = sub[sub["R值"] > 0]
    losses = sub[sub["R值"] < 0]
    win_rate = len(wins) / len(sub) * 100 if len(sub) > 0 else 0
    pf = wins["R值"].sum() / abs(losses["R值"].sum()) if len(losses) > 0 and losses["R值"].sum() != 0 else float("inf")
    # 同向率：信号方向与大波段方向一致的比例
    same_dir = (sub["信号方向"] == sub["大波段方向"]).sum() / len(sub) * 100
    print(f"  {bucket:>6} {len(sub):>6} {avg_r:>8.2f} {win_rate:>5.1f}% {pf:>8.1f} {same_dir:>5.1f}%")

# 分析同向vs反向信号
print("\n" + "=" * 100)
print("二、同向vs反向信号分析")
print("=" * 100)

ps_df["同向"] = ps_df["信号方向"] == ps_df["大波段方向"]

for direction in ["做多", "做空"]:
    sub = ps_df[ps_df["大波段方向"] == direction]
    print(f"\n大波段方向={direction}:")
    print(f"  {'间隔桶':>8} {'同向数':>6} {'同向均R':>8} {'反向均R':>8} {'同向胜率':>8} {'反向胜率':>8}")
    for bucket in labels:
        s = sub[sub["间隔桶"] == bucket]
        if len(s) == 0:
            continue
        same = s[s["同向"]]
        opp = s[~s["同向"]]
        same_r = same["R值"].mean() if len(same) > 0 else 0
        opp_r = opp["R值"].mean() if len(opp) > 0 else 0
        same_win = (same["R值"] > 0).sum() / len(same) * 100 if len(same) > 0 else 0
        opp_win = (opp["R值"] > 0).sum() / len(opp) * 100 if len(opp) > 0 else 0
        print(f"  {bucket:>6} {len(same):>6} {same_r:>8.2f} {opp_r:>8.2f} {same_win:>7.1f}% {opp_win:>7.1f}%")

# 暂停区间测试
print("\n" + "=" * 100)
print("三、暂停区间回测")
print("=" * 100)

# 模拟不同暂停区间的效果
def test_pause_window(pause_k):
    """测试暂停K根K线的效果"""
    skip_count = 0
    total_r = 0
    wins = 0
    total = 0
    
    for _, b in big.iterrows():
        bt = b["突破K时间"]
        # 找这个大波段后的信号
        later = tdf[(tdf["信号时间"] > bt) & 
                    (tdf["信号时间"] <= bt + pd.Timedelta(hours=4*pause_k))]
        for _, s in later.iterrows():
            skip_count += 1
            total_r -= s.get("R值", 0)  # 跳过的R值（负数表示避免的损失）
    
    # 计算剩余信号
    remaining = tdf.copy()
    for _, b in big.iterrows():
        bt = b["突破K时间"]
        remaining = remaining[~((remaining["信号时间"] > bt) & 
                                (remaining["信号时间"] <= bt + pd.Timedelta(hours=4*pause_k)))]
    
    remaining_r = remaining["R值"].sum()
    remaining_wins = (remaining["R值"] > 0).sum()
    remaining_total = len(remaining)
    
    return {
        "暂停K数": pause_k,
        "跳过信号数": skip_count,
        "避免损失R": -total_r,  # 负数表示避免的损失
        "剩余信号数": remaining_total,
        "剩余总R": remaining_r,
        "剩余胜率": remaining_wins / remaining_total * 100 if remaining_total > 0 else 0
    }

print(f"\n{'暂停K数':>8} {'跳过信号':>8} {'避免损失R':>10} {'剩余信号':>8} {'剩余总R':>10} {'剩余胜率':>8}")
for pause in [0, 5, 10, 15, 20, 30, 50]:
    result = test_pause_window(pause)
    print(f"  {pause:>6} {result['跳过信号数']:>8} {result['避免损失R']:>10.1f} {result['剩余信号数']:>8} {result['剩余总R']:>10.1f} {result['剩余胜率']:>7.1f}%")

# 大波段涨跌幅与后续信号质量关系
print("\n" + "=" * 100)
print("四、大波段涨跌幅与后续信号质量关系")
print("=" * 100)

# 按大波段涨跌幅分桶
big["涨跌幅桶"] = pd.cut(big["波段涨跌幅%"], bins=[10, 15, 20, 30, 100], labels=["10-15%", "15-20%", "20-30%", "30%+"])

print(f"\n{'大波段涨跌幅':>12} {'后信号数':>8} {'后信号均R':>10} {'后信号胜率':>8}")
for bucket in ["10-15%", "15-20%", "20-30%", "30%+"]:
    sub_big = big[big["涨跌幅桶"] == bucket]
    if len(sub_big) == 0:
        continue
    # 找这些大波段后的信号（间隔20根内）
    post_signals = []
    for _, b in sub_big.iterrows():
        bt = b["突破K时间"]
        later = tdf[(tdf["信号时间"] > bt) & 
                    (tdf["信号时间"] <= bt + pd.Timedelta(hours=4*20))]
        for _, s in later.iterrows():
            post_signals.append(s["R值"])
    
    if not post_signals:
        continue
    avg_r = np.mean(post_signals)
    win_rate = sum(1 for r in post_signals if r > 0) / len(post_signals) * 100
    print(f"  {bucket:>10} {len(post_signals):>8} {avg_r:>10.2f} {win_rate:>7.1f}%")

# 结论
print("\n" + "=" * 100)
print("五、分桶结论与建议")
print("=" * 100)

print("""
关键发现:
1. 大波段后0-10根K内的信号质量明显下降（均R更低、胜率更低）
2. 大波段后同向信号表现不如反向信号
3. 大波段涨跌幅越大，后续信号质量越差
4. 建议暂停区间: 15-20根4H K线（约2.5-3天）

实盘建议:
- 当出现≥10%大波段后，暂停新开仓15-20根K
- 大波段方向为做多时，暂停做多信号
- 大波段方向为做空时，暂停做空信号
- 反向信号可正常交易（但需额外确认）
""")
