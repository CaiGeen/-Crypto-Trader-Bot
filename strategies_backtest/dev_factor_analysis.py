"""偏离度因子分析：结合E'方案(方向+ATR%<2)探索偏离度分档。"""
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

def weighted_eval(tier_funcs, rdf, label=""):
    rdf = rdf.copy()
    rdf["tier"] = 1.0
    for tier, fn in tier_funcs.items():
        mask = rdf.apply(fn, axis=1)
        rdf.loc[mask, "tier"] = tier
    rdf["wr"] = rdf["r"] * rdf["tier"]
    rdf_s = rdf.sort_values("sig_idx")
    cum = rdf_s["wr"].cumsum()
    max_dd = (cum - cum.cummax()).min()
    total_wr = rdf["wr"].sum()
    if label: print(f"\n【{label}】")
    for tier in sorted(tier_funcs.keys(), reverse=True):
        sub = rdf[rdf["tier"] == tier]
        print(f"  {tier}x: {stats(sub)}  加权R={(sub['r']*tier).sum():.1f}")
    if (rdf["tier"] == 1.0).any() and 1.0 not in tier_funcs:
        sub = rdf[rdf["tier"] == 1.0]
        print(f"  1x(默认): {stats(sub)}  加权R={sub['r'].sum():.1f}")
    print(f"  → 加权R={total_wr:.1f}  回撤={max_dd:.1f}R")
    return total_wr, max_dd

D = f"{'━'*95}"
print("=" * 95)
print(f"偏离度因子分析（{len(rdf)}单，E'基准: 1567.3R/-8.1）")
print("=" * 95)
print(f"  偏离度 dev = (close[i-1] - 日线EMA70) / 日线EMA70 × 100")
print(f"  dev<0: 价格在EMA70下方 | dev>=0: 价格在EMA70上方")

print(f"\n{D}\n一、偏离度单独分组\n{D}")
for lo, hi in [(-2,-1),(-1,0),(0,2),(2,4),(4,6),(6,15)]:
    print(f"  dev[{lo},{hi}): {stats(rdf[(rdf['dev']>=lo)&(rdf['dev']<hi)])}")

print(f"\n{D}\n二、偏离度 × 方向\n{D}")
print(f"  {'':>16} {'做多':>26} {'做空':>26}")
for lo, hi in [(-2,0),(0,2),(2,4),(4,6),(6,15)]:
    parts = [f"  dev[{lo},{hi}):{'':>6}"]
    for d in ["long","short"]:
        sub = rdf[(rdf['dir']==d)&(rdf['dev']>=lo)&(rdf['dev']<hi)]
        parts.append(f"{stats(sub):>26}")
    print(" ".join(parts))

print(f"\n{D}\n三、偏离度 × 方向 × ATR%<2\n{D}")
low_atr = rdf[rdf['atr_pct']<2.0]
print(f"  (仅ATR%<2.0的子集，{len(low_atr)}单)")
print(f"  {'':>16} {'做多':>26} {'做空':>26}")
for lo, hi in [(-2,0),(0,2),(2,4),(4,6),(6,15)]:
    parts = [f"  dev[{lo},{hi}):{'':>6}"]
    for d in ["long","short"]:
        sub = low_atr[(low_atr['dir']==d)&(low_atr['dev']>=lo)&(low_atr['dev']<hi)]
        parts.append(f"{stats(sub):>26}")
    print(" ".join(parts))

print(f"\n{D}\n四、E'基准 + 偏离度分档组合\n{D}")
weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<2.0,
               1.5: lambda r: r["dir"]=="short" and r["atr_pct"]<2.0}, rdf, "E'基准: 做多&ATR%<2→2x, 做空&ATR%<2→1.5x")

weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<2.0 and r["dev"]<0,
               1.5: lambda r: (r["dir"]=="long" and r["atr_pct"]<2.0 and r["dev"]>=0) or
                             (r["dir"]=="short" and r["atr_pct"]<2.0)}, rdf,
              "E1: 做多&ATR%<2&dev<0→2x, 做多&ATR%<2&dev>=0→1.5x, 做空&ATR%<2→1.5x")

weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<2.0,
               1.5: lambda r: r["dir"]=="short" and r["atr_pct"]<2.0 and r["dev"]<6,
               0.5: lambda r: r["dir"]=="short" and r["atr_pct"]<2.0 and r["dev"]>=6}, rdf,
              "E2: E'基础上做空&dev>=6降0.5x(追空高偏离)")

weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<2.0 and r["dev"]<4,
               1.5: lambda r: (r["dir"]=="long" and r["atr_pct"]<2.0 and r["dev"]>=4) or
                             (r["dir"]=="short" and r["atr_pct"]<2.0 and r["dev"]<6)}, rdf,
              "E3: 做多&ATR%<2&dev<4→2x, 做多&dev>=4→1.5x, 做空&dev<6→1.5x")

weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<2.0 and r["dev"]<2,
               1.5: lambda r: (r["dir"]=="long" and r["atr_pct"]<2.0 and r["dev"]>=2) or
                             (r["dir"]=="short" and r["atr_pct"]<2.0)}, rdf,
              "E4: 做多&ATR%<2&dev<2→2x, 做多&dev>=2→1.5x, 做空&ATR%<2→1.5x")

weighted_eval({2: lambda r: r["atr_pct"]<2.0 and r["dev"]<0,
               1.5: lambda r: r["atr_pct"]<2.0 and r["dev"]>=0}, rdf,
              "E5: 不分方向, ATR%<2&dev<0→2x, ATR%<2&dev>=0→1.5x")

weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<2.0 and r["dev"]<0,
               1.5: lambda r: r["atr_pct"]<2.0 and not (r["dir"]=="long" and r["dev"]<0)}, rdf,
              "E6: 做多&ATR%<2&dev<0→2x, 其余ATR%<2→1.5x")
