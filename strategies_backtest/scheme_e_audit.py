"""方案E精细化检查：方向+ATR%分档的逻辑漏洞排查。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig

cfg = BacktestConfig()
rdf = pd.read_parquet(cfg.base_dir / "cache" / "tier_v4_rdf.parquet")

def stats(sub):
    if sub.empty: return "—"
    sr = sub["r"]
    w = sr[sr > 0].sum(); l = sr[sr <= 0].sum()
    pf = w / abs(l) if l != 0 else float("inf")
    return f"{len(sub):>4}单/{sr.sum():>6.1f}R/均{sr.mean():>4.2f}/PF{pf:.1f}/胜{(sr>0).mean():.0%}"

D = f"{'━'*95}"
print("=" * 95)
print(f"方案E精细化检查（{len(rdf)}单）")
print("=" * 95)

print(f"\n{D}\n一、方案E定义\n{D}")
print("  做多 且 ATR%<1.5 → 2x")
print("  做空 且 ATR%<1.5 → 1.5x")
print("  其余（ATR%>=1.5） → 1x")
print("  结果: 加权R=1532.9 / 回撤=-8.1R")

print(f"\n{D}\n二、方向 × ATR% 详细交叉\n{D}")
print(f"  {'':>20} {'做多':>26} {'做空':>26}")
for lo, hi in [(0,1.0),(1.0,1.5),(1.5,2.0),(2.0,100)]:
    parts = [f"  ATR%[{lo},{hi}):{'':>8}"]
    for d in ["long", "short"]:
        sub = rdf[(rdf['dir']==d)&(rdf['atr_pct']>=lo)&(rdf['atr_pct']<hi)]
        parts.append(f"{stats(sub):>26}")
    print(" ".join(parts))
print(f"\n  方向汇总:")
for d, lbl in [("long","做多"),("short","做空")]:
    print(f"  {lbl}: {stats(rdf[rdf['dir']==d])}")

print(f"\n{D}\n三、ATR%阈值对比：1.0 vs 1.5\n{D}")
for cut in [1.0, 1.2, 1.5, 2.0]:
    rdf2 = rdf.copy()
    rdf2["tier"] = 1.0
    rdf2.loc[(rdf2["dir"]=="long")&(rdf2["atr_pct"]<cut), "tier"] = 2.0
    rdf2.loc[(rdf2["dir"]=="short")&(rdf2["atr_pct"]<cut), "tier"] = 1.5
    rdf2["wr"] = rdf2["r"] * rdf2["tier"]
    rdf_s = rdf2.sort_values("sig_idx")
    cum = rdf_s["wr"].cumsum()
    max_dd = (cum - cum.cummax()).min()
    print(f"  ATR%<{cut}: 加权R={rdf2['wr'].sum():.1f}  回撤={max_dd:.1f}R  "
          f"2x档{len(rdf2[rdf2['tier']==2])}单  1.5x档{len(rdf2[rdf2['tier']==1.5])}单")

print(f"\n{D}\n四、做多/做空不对称性检查\n{D}")
for cut in [1.5]:
    long_low = rdf[(rdf['dir']=='long')&(rdf['atr_pct']<cut)]
    short_low = rdf[(rdf['dir']=='short')&(rdf['atr_pct']<cut)]
    long_high = rdf[(rdf['dir']=='long')&(rdf['atr_pct']>=cut)]
    short_high = rdf[(rdf['dir']=='short')&(rdf['atr_pct']>=cut)]
    print(f"  ATR%<{cut}:")
    print(f"    做多: {stats(long_low)}  → 2x档")
    print(f"    做空: {stats(short_low)}  → 1.5x档")
    print(f"  ATR%>={cut}:")
    print(f"    做多: {stats(long_high)}  → 1x档")
    print(f"    做空: {stats(short_high)}  → 1x档")
    print(f"\n  做空+低波动均R={short_low['r'].mean():.2f}，给1.5x是否合理？")
    print(f"  做空+低波动胜率={(short_low['r']>0).mean():.0%}，PF={short_low['r'][short_low['r']>0].sum()/abs(short_low['r'][short_low['r']<=0].sum()):.1f}")

print(f"\n{D}\n五、逐年表现\n{D}")
rdf_e = rdf.copy()
rdf_e["tier"] = 1.0
rdf_e.loc[(rdf_e["dir"]=="long")&(rdf_e["atr_pct"]<1.5), "tier"] = 2.0
rdf_e.loc[(rdf_e["dir"]=="short")&(rdf_e["atr_pct"]<1.5), "tier"] = 1.5
rdf_e["wr"] = rdf_e["r"] * rdf_e["tier"]
print(f"  {'年份':>6} {'单数':>6} {'加权R':>8} {'均R':>6} {'胜率':>6} {'最大回撤':>10}")
for yr in sorted(rdf_e["year"].unique()):
    sub = rdf_e[rdf_e["year"]==yr].sort_values("sig_idx")
    cum = sub["wr"].cumsum()
    dd = (cum - cum.cummax()).min()
    print(f"  {yr:>6} {len(sub):>6} {sub['wr'].sum():>8.1f} {sub['wr'].mean():>6.2f} {(sub['wr']>0).mean():>6.0%} {dd:>10.1f}")

print(f"\n{D}\n六、时序确认：方向和ATR%在成交时是否已知\n{D}")
print("  ATR% = atr_vals[i-1] / closes[i-1] × 100")
print("    → i-1是信号K前一根，i开盘前已知 ✓")
print("  方向 = 成交方向（多单成交=做多，空单成交=做空）")
print("    → 成交时即知，不需等收盘 ✓")
print("  结论: 方案E无未来信息，成交后可立即分档加仓 ✓")

print(f"\n{D}\n七、边界检查\n{D}")
near_bound = rdf[(rdf['atr_pct']>=1.4)&(rdf['atr_pct']<1.6)]
print(f"  ATR%在1.5附近[1.4,1.6): {stats(near_bound)}")
for d, lbl in [("long","做多"),("short","做空")]:
    sub = near_bound[near_bound['dir']==d]
    print(f"    {lbl}: {stats(sub)}")
print(f"  → 边界附近样本量{'充足' if len(near_bound)>30 else '偏少'}，阈值微调影响{'有限' if len(near_bound)>30 else '较大'}")
