"""标记回退6K极值条目：基于最优配置CSV，分组对比'回调基准' vs '回退6K极值'。

策略逻辑与 export_optimal_csv.py 完全相同（回调基准，无则回退6K极值），
本脚本只做分组标记与对比分析，不重新跑回测。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from config import BacktestConfig

cfg = BacktestConfig()
csv_in = cfg.results_path / "最优配置_回调基准_ATR动态.csv"
df = pd.read_csv(csv_in, encoding="utf-8-sig")

# 标记入场基准类型
df["入场基准类型"] = np.where(df["是否有回调基准"] == "是", "回调基准", "回退6K极值")

# 回退6K极值时"回调基准"列为空，填入6K极值方便阅读
mask_fallback = df["入场基准类型"] == "回退6K极值"
df.loc[mask_fallback, "回调基准"] = df.loc[mask_fallback, "6K极值"]
df.loc[mask_fallback, "回调基准偏移%"] = 0.0

print("=" * 110)
print("回退6K极值标记报告：回调基准 vs 回退6K极值 分组对比")
print("=" * 110)
print(f"数据源：{csv_in.name}")
print(f"总条目：{len(df)}")

cb = df[df["入场基准类型"] == "回调基准"]
fb = df[df["入场基准类型"] == "回退6K极值"]
print(f"  回调基准:  {len(cb)} 条 ({len(cb)/len(df):.0%})")
print(f"  回退6K极值: {len(fb)} 条 ({len(fb)/len(df):.0%})")


def stats(sub, label):
    print(f"\n{'─' * 90}")
    print(f"【{label}】{len(sub)} 条")
    print(f"{'─' * 90}")
    if sub.empty:
        print("  无数据")
        return
    filled = sub[sub["是否成交"] == "成交"]
    unfilled = sub[sub["是否成交"] == "未成交"]
    sr = filled["R值"]
    print(f"  成交: {len(filled)} ({len(filled)/len(sub):.0%})  未成交: {len(unfilled)} ({len(unfilled)/len(sub):.0%})")
    if not filled.empty:
        print(f"  累计R={sr.sum():>8.1f}  均R={sr.mean():>6.2f}  胜率={(sr > 0).mean():>5.0%}  累计收益={filled['R值'].sum():.1f}R")
        for d, lbl in [("做多", "做多"), ("做空", "做空")]:
            sub2 = filled[filled["方向"] == d]
            if sub2.empty:
                continue
            sr2 = sub2["R值"]
            print(f"    {lbl:>4} {len(sub2):>5}单 累计R={sr2.sum():>7.1f} 均R={sr2.mean():>6.2f} 胜率={(sr2 > 0).mean():>5.0%}")
        print(f"  出场原因: ", end="")
        vc = filled["出场原因"].value_counts()
        for reason, cnt in vc.items():
            print(f"{reason}={cnt}({cnt/len(filled):.0%}) ", end="")
        print()
        print(f"  MFE%: 中位={filled['MFE%'].median():.1f} 均值={filled['MFE%'].mean():.1f}")
        print(f"  ATR%: 中位={filled['ATR%'].median():.2%} 均值={filled['ATR%'].mean():.2%}")
        print(f"  持仓K数: 中位={filled['持仓K数'].median():.0f} 均值={filled['持仓K数'].mean():.1f}")
    if not unfilled.empty:
        print(f"  未成交可能错过R={unfilled['可能错过R值'].sum():.1f}")


stats(cb, "回调基准组（6K内找到2K不创新高确认的极值点）")
stats(fb, "回退6K极值组（6K内无2K确认极值点，回退用6K极值入场）")

# 年度分解
print(f"\n{'=' * 110}")
print("年度分解对比")
print(f"{'=' * 110}")
all_years = sorted(df["年份"].unique())
print(f"\n{'年份':>6}  {'回调基准单数':>10} {'回调累计R':>10}  {'回退单数':>8} {'回退累计R':>10}  {'回退占比':>8}")
print("-" * 70)
for yr in all_years:
    c = cb[cb["年份"] == yr]
    f = fb[fb["年份"] == yr]
    c_filled = c[c["是否成交"] == "成交"]
    f_filled = f[f["是否成交"] == "成交"]
    cr = c_filled["R值"].sum() if not c_filled.empty else 0
    fr = f_filled["R值"].sum() if not f_filled.empty else 0
    total = len(c) + len(f)
    print(f"{yr:>6}  {len(c):>10} {cr:>10.1f}  {len(f):>8} {fr:>10.1f}  {len(f)/total:>7.0%}")
tc = cb[cb["是否成交"] == "成交"]["R值"].sum()
tf = fb[fb["是否成交"] == "成交"]["R值"].sum()
print(f"{'合计':>6}  {len(cb):>10} {tc:>10.1f}  {len(fb):>8} {tf:>10.1f}  {len(fb)/len(df):>7.0%}")

# 回退组特征分析
print(f"\n{'=' * 110}")
print("回退6K极值组特征分析（为什么找不到回调基准？）")
print(f"{'=' * 110}")
if not fb.empty:
    fb_filled = fb[fb["是否成交"] == "成交"]
    print(f"  方向分布: ", end="")
    vd = fb["方向"].value_counts()
    for d, cnt in vd.items():
        print(f"{d}={cnt}({cnt/len(fb):.0%}) ", end="")
    print()
    print(f"  ATR%分布: 中位={fb['ATR%'].median():.2%} 均值={fb['ATR%'].mean():.2%}")
    print(f"    → {'高波动' if fb['ATR%'].mean() > df['ATR%'].mean() else '低波动'}环境为主（全样本ATR%均值={df['ATR%'].mean():.2%}）")
    if not fb_filled.empty:
        print(f"  成交后表现: 累计R={fb_filled['R值'].sum():.1f} 均R={fb_filled['R值'].mean():.2f} 胜率={(fb_filled['R值']>0).mean():.0%}")
        cb_filled = cb[cb["是否成交"] == "成交"]
        if not cb_filled.empty:
            print(f"  vs 回调基准组: 累计R={cb_filled['R值'].sum():.1f} 均R={cb_filled['R值'].mean():.2f} 胜率={(cb_filled['R值']>0).mean():.0%}")
            print(f"  → 回退组均R {'优于' if fb_filled['R值'].mean() > cb_filled['R值'].mean() else '劣于'} 回调基准组")

# 汇总
print(f"\n{'=' * 110}")
print("汇总")
print(f"{'=' * 110}")
cb_f = cb[cb["是否成交"] == "成交"]
fb_f = fb[fb["是否成交"] == "成交"]
print(f"\n{'指标':>20}  {'回调基准组':>14}  {'回退6K极值组':>14}  {'全样本':>12}")
print("-" * 68)
rows = [
    ("条目数", len(cb), len(fb), len(df), "d"),
    ("成交数", len(cb_f), len(fb_f), len(df[df["是否成交"] == "成交"]), "d"),
    ("累计R", cb_f["R值"].sum(), fb_f["R值"].sum(), df[df["是否成交"] == "成交"]["R值"].sum(), ".1f"),
    ("均R", cb_f["R值"].mean() if not cb_f.empty else 0, fb_f["R值"].mean() if not fb_f.empty else 0, df[df["是否成交"] == "成交"]["R值"].mean(), ".2f"),
    ("胜率", (cb_f["R值"] > 0).mean() if not cb_f.empty else 0, (fb_f["R值"] > 0).mean() if not fb_f.empty else 0, (df[df["是否成交"] == "成交"]["R值"] > 0).mean(), ".1%"),
]
for label, cv, fv, av, fmt in rows:
    if fmt == "d":
        print(f"{label:>20}  {cv:>14d}  {fv:>14d}  {av:>12d}")
    else:
        print(f"{label:>20}  {cv:>14{fmt}}  {fv:>14{fmt}}  {av:>12{fmt}}")

# 导出标记版CSV
csv_out = cfg.results_path / "最优配置_回调基准_标记回退.csv"
cols = list(df.columns)
cols.remove("入场基准类型")
cols.insert(cols.index("是否有回调基准") + 1, "入场基准类型")
df[cols].to_csv(csv_out, index=False, encoding="utf-8-sig")
print(f"\n标记版CSV已导出: {csv_out}")
print(f"新增列'入场基准类型'：回调基准 / 回退6K极值")
print(f"{'=' * 110}")
