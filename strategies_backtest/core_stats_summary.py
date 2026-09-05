"""核心数据速览：基于最优配置CSV的全面统计"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd

csv_path = os.path.join(os.path.dirname(__file__), "results", "最优配置_回调基准_ATR动态.csv")
df = pd.read_csv(csv_path, encoding="utf-8-sig")

filled = df[df["是否成交"] == "成交"].copy()
unfilled = df[df["是否成交"] == "未成交"].copy()

print("=" * 100)
print("核心数据速览（最优配置：回调高点+ATR动态逻辑）")
print("=" * 100)

# 1. 总览
print("\n【一、总览】")
print(f"  总信号数: {len(df)}")
print(f"  成交单: {len(filled)} ({len(filled)/len(df)*100:.0f}%), 累计R={filled['R值'].sum():.1f}, 均R={filled['R值'].mean():.2f}, 胜率={(filled['R值']>0).mean():.0%}")
print(f"  未成交单: {len(unfilled)} ({len(unfilled)/len(df)*100:.0f}%), 可能错过R={unfilled['可能错过R值'].sum():.1f}")
print(f"  回调基准覆盖率: {(df['是否有回调基准']=='是').mean():.0%}")

# 2. R值分布
print(f"\n{'='*100}")
print("【二、R值分布（成交单）】")
r = filled["R值"]
print(f"\n  均值={r.mean():.2f}, 中位={r.median():.2f}, 标准差={r.std():.2f}")
print(f"  最大={r.max():.1f}, 最小={r.min():.1f}")
print(f"\n  {'R区间':>12} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6}")
print("  " + "-" * 45)
bins = [(-99,-3),(-3,-2),(-2,-1),(-1,-0.5),(-0.5,0),(0,0.5),(0.5,1),(1,2),(2,3),(3,5),(5,99)]
for lo, hi in bins:
    sub = filled[(r>=lo) & (r<hi)]
    label = f"[{lo},{hi})" if lo > -99 else f"<{hi}"
    label = f">={lo}" if hi >= 99 else label
    print(f"  {label:>12} {len(sub):>6} {len(sub)/len(filled)*100:>5.0f}% {sub['R值'].sum():>8.1f} {sub['R值'].mean() if len(sub)>0 else 0:>6.2f}")

# 3. 出场原因分布
print(f"\n{'='*100}")
print("【三、出场原因分布（成交单）】")
print(f"\n  {'出场原因':>14} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6} {'胜率':>6}")
print("  " + "-" * 50)
for reason in filled["出场原因"].unique():
    sub = filled[filled["出场原因"] == reason]
    print(f"  {reason:>14} {len(sub):>6} {len(sub)/len(filled)*100:>5.0f}% {sub['R值'].sum():>8.1f} {sub['R值'].mean():>6.2f} {(sub['R值']>0).mean():>5.0%}")

# 4. 涨跌幅分布（MFE/MAE）
print(f"\n{'='*100}")
print("【四、波段涨跌幅分布（成交单，从入场基准算）】")
print(f"\n  MFE（最大有利偏移）:")
mfe = filled["MFE%"]
print(f"    均值={mfe.mean():.2f}%, 中位={mfe.median():.2f}%, 最大={mfe.max():.2f}%")
print(f"\n  {'MFE区间':>12} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6}")
print("  " + "-" * 45)
mfe_bins = [(0,1),(1,3),(3,5),(5,8),(8,12),(12,18),(18,30),(30,99)]
for lo, hi in mfe_bins:
    sub = filled[(mfe>=lo) & (mfe<hi)]
    label = f">={lo}%" if hi >= 99 else f"[{lo},{hi})%"
    print(f"  {label:>12} {len(sub):>6} {len(sub)/len(filled)*100:>5.0f}% {sub['R值'].sum():>8.1f} {sub['R值'].mean() if len(sub)>0 else 0:>6.2f}")

print(f"\n  MAE（最大不利偏移）:")
mae = filled["MAE%"]
print(f"    均值={mae.mean():.2f}%, 中位={mae.median():.2f}%, 最小={mae.min():.2f}%")
print(f"\n  {'MAE区间':>12} {'单数':>6} {'占比':>6} {'累计R':>8}")
print("  " + "-" * 40)
mae_bins = [(-99,-3),(-3,-2),(-2,-1),(-1,-0.5),(-0.5,0),(0,1),(1,99)]
for lo, hi in mae_bins:
    sub = filled[(mae>=lo) & (mae<hi)]
    label = f"<{hi}%" if lo <= -99 else f">={lo}%" if hi >= 99 else f"[{lo},{hi})%"
    print(f"  {label:>12} {len(sub):>6} {len(sub)/len(filled)*100:>5.0f}% {sub['R值'].sum():>8.1f}")

# 5. 持仓K数分布
print(f"\n{'='*100}")
print("【五、持仓K数分布（成交单）】")
hold = filled["持仓K数"]
print(f"\n  均值={hold.mean():.1f}K, 中位={hold.median():.0f}K, 最大={hold.max()}K")
print(f"\n  {'持仓K数':>10} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6}")
print("  " + "-" * 45)
hold_bins = [(0,1),(1,3),(3,6),(6,12),(12,24),(24,48),(48,99)]
for lo, hi in hold_bins:
    sub = filled[(hold>=lo) & (hold<hi)]
    label = f">={lo}K" if hi >= 99 else f"[{lo},{hi})K"
    print(f"  {label:>10} {len(sub):>6} {len(sub)/len(filled)*100:>5.0f}% {sub['R值'].sum():>8.1f} {sub['R值'].mean() if len(sub)>0 else 0:>6.2f}")

# 6. 多空对比
print(f"\n{'='*100}")
print("【六、多空对比（成交单）】")
print(f"\n  {'方向':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'均MFE':>8} {'均持仓':>8}")
print("  " + "-" * 50)
for d in ["做多", "做空"]:
    sub = filled[filled["方向"] == d]
    print(f"  {d:>6} {len(sub):>6} {sub['R值'].sum():>8.1f} {sub['R值'].mean():>6.2f} {(sub['R值']>0).mean():>5.0%} {sub['MFE%'].mean():>7.2f}% {sub['持仓K数'].mean():>7.1f}K")

# 7. 加仓 vs 不加仓
print(f"\n{'='*100}")
print("【七、加仓效果（成交单）】")
print(f"\n  {'是否加仓':>8} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6}")
print("  " + "-" * 40)
for a in ["是", "否"]:
    sub = filled[filled["是否加仓"] == a]
    print(f"  {a:>8} {len(sub):>6} {sub['R值'].sum():>8.1f} {sub['R值'].mean():>6.2f} {(sub['R值']>0).mean():>5.0%}")

# 8. 年度分布
print(f"\n{'='*100}")
print("【八、年度分布（成交单）】")
print(f"\n  {'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'最大R':>6} {'最小R':>6}")
print("  " + "-" * 50)
for yr in sorted(filled["年份"].unique()):
    sub = filled[filled["年份"] == yr]
    print(f"  {yr:>6} {len(sub):>6} {sub['R值'].sum():>8.1f} {sub['R值'].mean():>6.2f} {(sub['R值']>0).mean():>5.0%} {sub['R值'].max():>6.1f} {sub['R值'].min():>6.1f}")

# 9. ATR%分布
print(f"\n{'='*100}")
print("【九、ATR%分布（成交单）】")
atr_pct = filled["ATR%"]
print(f"\n  均值={atr_pct.mean():.2f}%, 中位={atr_pct.median():.2f}%")
print(f"\n  {'ATR%':>10} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6}")
print("  " + "-" * 45)
atr_bins = [(0,1),(1,1.5),(1.5,2),(2,3),(3,99)]
for lo, hi in atr_bins:
    sub = filled[(atr_pct>=lo) & (atr_pct<hi)]
    label = f">={lo}%" if hi >= 99 else f"[{lo},{hi})%"
    print(f"  {label:>10} {len(sub):>6} {len(sub)/len(filled)*100:>5.0f}% {sub['R值'].sum():>8.1f} {sub['R值'].mean() if len(sub)>0 else 0:>6.2f}")

# 10. 未成交单分析
print(f"\n{'='*100}")
print("【十、未成交单分析】")
print(f"\n  总数: {len(unfilled)}, 可能错过R={unfilled['可能错过R值'].sum():.1f}")
print(f"\n  {'出场原因':>14} {'单数':>6} {'可能错过R':>10} {'均MFE%':>8}")
print("  " + "-" * 45)
for reason in unfilled["出场原因"].unique():
    sub = unfilled[unfilled["出场原因"] == reason]
    print(f"  {reason:>14} {len(sub):>6} {sub['可能错过R值'].sum():>10.1f} {sub['MFE%'].mean():>7.2f}%")

print(f"\n  未成交单MFE分布:")
umfe = unfilled["MFE%"]
print(f"    均值={umfe.mean():.2f}%, 中位={umfe.median():.2f}%, 最大={umfe.max():.2f}%")
for lo, hi in [(0,3),(3,8),(8,12),(12,22),(22,99)]:
    sub = unfilled[(umfe>=lo) & (umfe<hi)]
    label = f">={lo}%" if hi >= 99 else f"[{lo},{hi})%"
    print(f"    {label:>10}: {len(sub)}单, 可能错过R={sub['可能错过R值'].sum():.1f}")

# 11. 回调基准偏移分布
print(f"\n{'='*100}")
print("【十一、回调基准偏移分布】")
cb = df[df["是否有回调基准"] == "是"]
offset = cb["回调基准偏移%"]
print(f"\n  有回调基准: {len(cb)}/{len(df)} ({len(cb)/len(df)*100:.0f}%)")
print(f"  偏移均值={offset.mean():.3f}%, 中位={offset.median():.3f}%, 最大={offset.max():.3f}%")
print(f"\n  {'偏移区间':>10} {'单数':>6} {'占比':>6}")
print("  " + "-" * 30)
for lo, hi in [(0,0.3),(0.3,0.5),(0.5,1),(1,2),(2,5),(5,99)]:
    sub = cb[(offset>=lo) & (offset<hi)]
    label = f">={lo}%" if hi >= 99 else f"[{lo},{hi})%"
    print(f"  {label:>10} {len(sub):>6} {len(sub)/len(cb)*100:>5.0f}%")

# 12. 累计R曲线关键点
print(f"\n{'='*100}")
print("【十二、累计R曲线关键点（成交单）】")
cumr = filled["R值"].cumsum()
print(f"\n  最终累计R: {cumr.iloc[-1]:.1f}")
print(f"  最大回撤: {(cumr.cummax() - cumr).max():.1f}R")
max_dd = (cumr.cummax() - cumr).max()
max_dd_idx = (cumr.cummax() - cumr).idxmax()
print(f"  回撤发生位置: 第{max_dd_idx+1}单, 累计R从{cumr.iloc[:max_dd_idx+1].max():.1f}跌到{cumr.iloc[max_dd_idx]:.1f}")
print(f"\n  里程碑:")
for target in [200, 400, 600, 800, 1000, 1200, 1384]:
    hit = filled[cumr >= target]
    if len(hit) > 0:
        first = hit.iloc[0]
        print(f"    累计R>{target}: 第{first['序号']}单 ({first['信号时间']})")

print(f"\n{'='*100}")
print("速览完成")
print(f"{'='*100}")
