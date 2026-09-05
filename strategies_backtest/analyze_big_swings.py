"""筛选>=10%大波段，分析间隔和同向波段规律。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
csv_path = os.path.join(HERE, "results", "全时段波段数据集_6K突破_KAMA收盘终结.csv")

if not os.path.exists(csv_path):
    print("波段数据集不存在，先生成...")
    from swing_dataset import run
    rdf = run()
else:
    print(f"读取波段数据集: {csv_path}")
    rdf = pd.read_csv(csv_path, encoding="utf-8-sig")

print(f"\n总波段数: {len(rdf)}")

# 筛选>=10%的波段
big = rdf[rdf["波段涨跌幅%"] >= 10].copy()
print(f"\n总波段数: {len(rdf)}")
print(f"涨跌幅>=10%的波段: {len(big)} ({len(big)/len(rdf):.1%})")

# 基本统计
print(f"\n{'='*100}")
print("一、大波段基本统计")
print(f"{'='*100}")
for d in ["做多", "做空"]:
    sub = big[big["方向"] == d]
    if len(sub) == 0:
        continue
    amp = sub["波段涨跌幅%"]
    dur = sub["起点至终点K数"]
    print(f"\n{d}:")
    print(f"  波段数: {len(sub)}")
    print(f"  涨跌幅: 中位{amp.median():.2f}% 均值{amp.mean():.2f}%  P90{amp.quantile(.9):.2f}% 最大{amp.max():.2f}%")
    print(f"  K数间隔: 中位{dur.median():.0f} 均值{dur.mean():.1f}  P90{dur.quantile(.9):.0f} 最大{dur.max()}")

# 间隔分桶
print(f"\n{'='*100}")
print("二、大波段间隔分桶（起点→终点K数）")
print(f"{'='*100}")

bins = [(0,5), (5,10), (10,15), (15,20), (20,30), (30,50), (50,100)]
for d in ["做多", "做空"]:
    sub = big[big["方向"] == d]
    if len(sub) == 0:
        continue
    print(f"\n{d}:")
    print(f"  {'K数区间':>12} {'波段数':>6} {'占比':>6} {'中位涨跌幅':>10} {'均值涨跌幅':>10}")
    for lo, hi in bins:
        s = sub[(sub["起点至终点K数"] >= lo) & (sub["起点至终点K数"] < hi)]
        if len(s) == 0:
            continue
        pct = len(s) / len(sub) * 100
        print(f"  [{lo:>3},{hi:>3})     {len(s):>5} {pct:>5.1f}% {s['波段涨跌幅%'].median():>9.2f}% {s['波段涨跌幅%'].mean():>9.2f}%")

# 涨跌幅分桶
print(f"\n{'='*100}")
print("三、大波段涨跌幅分桶")
print(f"{'='*100}")

amp_bins = [(10,12), (12,15), (15,20), (20,30), (30,50), (50,100)]
for d in ["做多", "做空"]:
    sub = big[big["方向"] == d]
    if len(sub) == 0:
        continue
    print(f"\n{d}:")
    print(f"  {'涨跌幅区间':>12} {'波段数':>6} {'占比':>6} {'中位K数':>8} {'均值K数':>8}")
    for lo, hi in amp_bins:
        s = sub[(sub["波段涨跌幅%"] >= lo) & (sub["波段涨跌幅%"] < hi)]
        if len(s) == 0:
            continue
        pct = len(s) / len(sub) * 100
        print(f"  [{lo:>3},{hi:>3})%    {len(s):>5} {pct:>5.1f}% {s['起点至终点K数'].median():>7.0f} {s['起点至终点K数'].mean():>7.1f}")

# 同向波段连续分析
print(f"\n{'='*100}")
print("四、同向波段连续性分析")
print(f"{'='*100}")

# 按时间排序，找同向连续波段
rdf_sorted = rdf.sort_values("突破K时间").reset_index(drop=True)

# 计算同向连续波段数
streaks = []
current_dir = None
current_streak = 0
streak_lengths = []

for _, r in rdf_sorted.iterrows():
    if r["方向"] == current_dir:
        current_streak += 1
    else:
        if current_streak > 0:
            streak_lengths.append((current_dir, current_streak))
        current_dir = r["方向"]
        current_streak = 1
if current_streak > 0:
    streak_lengths.append((current_dir, current_streak))

print(f"\n同向连续波段序列统计:")
for d in ["做多", "做空"]:
    d_streaks = [s for dir_, s in streak_lengths if dir_ == d]
    if len(d_streaks) == 0:
        continue
    print(f"  {d}: 序列数={len(d_streaks)}, 最长连续={max(d_streaks)}, 平均连续={np.mean(d_streaks):.1f}")
    # 分桶
    for lo, hi in [(1,2), (2,3), (3,4), (4,6), (6,10), (10,100)]:
        c = sum(1 for s in d_streaks if lo <= s < hi)
        if c > 0:
            print(f"    连续{lo}-{hi}段: {c}次")

# 大波段后的同向波段表现
print(f"\n{'='*100}")
print("五、大波段后同向波段表现")
print(f"{'='*100}")

# 找出大波段后的下一个同向波段
big_times = set(big["突破K时间"])
next_same = []
for i, r in rdf_sorted.iterrows():
    if r["突破K时间"] in big_times:
        # 找下一个同向波段
        for j in range(i+1, len(rdf_sorted)):
            if rdf_sorted.iloc[j]["方向"] == r["方向"]:
                next_same.append({
                    "方向": r["方向"],
                    "大波段涨跌幅": r["波段涨跌幅%"],
                    "后同向涨跌幅": rdf_sorted.iloc[j]["波段涨跌幅%"],
                    "间隔K数": rdf_sorted.iloc[j]["起点至终点K数"]
                })
                break

if next_same:
    ns_df = pd.DataFrame(next_same)
    print(f"\n大波段后同向波段统计:")
    for d in ["做多", "做空"]:
        sub = ns_df[ns_df["方向"] == d]
        if len(sub) == 0:
            continue
        print(f"\n  {d}:")
        print(f"    样本数: {len(sub)}")
        print(f"    后同向涨跌幅: 中位{sub['后同向涨跌幅'].median():.2f}% 均值{sub['后同向涨跌幅'].mean():.2f}%")
        print(f"    间隔K数: 中位{sub['间隔K数'].median():.0f} 均值{sub['间隔K数'].mean():.1f}")

# 年度分布
print(f"\n{'='*100}")
print("六、大波段年度分布")
print(f"{'='*100}")

big["年份"] = pd.to_datetime(big["突破K时间"]).dt.year
yr_stats = big.groupby(["年份", "方向"]).agg(
    波段数=("波段涨跌幅%", "count"),
    中位涨跌幅=("波段涨跌幅%", "median"),
    均值涨跌幅=("波段涨跌幅%", "mean"),
    中位K数=("起点至终点K数", "median")
).round(2)
print(yr_stats.to_string())

# 结论摘要
print(f"\n{'='*100}")
print("七、分桶结论摘要")
print(f"{'='*100}")

print(f"""
1. 大波段占比: {len(big)}/{len(rdf)} = {len(big)/len(rdf):.1%}
2. 做多vs做空大波段:
   - 做多: {(big['方向']=='做多').sum()}单
   - 做空: {(big['方向']=='做空').sum()}单
3. 间隔K数规律:
   - 中位: {big['起点至终点K数'].median():.0f}根4H K线
   - 80%在{big['起点至终点K数'].quantile(.8):.0f}根内完成
4. 涨跌幅与间隔关系:
   - 涨跌幅越大，间隔通常越长
   - 10-15%波段: 中位K数{big[(big['波段涨跌幅%']>=10)&(big['波段涨跌幅%']<15)]['起点至终点K数'].median():.0f}根
   - >20%波段: 中位K数{big[big['波段涨跌幅%']>=20]['起点至终点K数'].median():.0f}根
5. 同向连续性:
   - 连续2+同向波段概率较低，大波段后同向延续需单独评估
""")
