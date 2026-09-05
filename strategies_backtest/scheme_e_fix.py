"""方案E漏洞修复探索：基于交叉表发现的最优分档。"""
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
print("方案E漏洞修复探索")
print("=" * 95)

print(f"\n{D}\n原方案E vs 阈值优化\n{D}")
weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<1.5,
               1.5: lambda r: r["dir"]=="short" and r["atr_pct"]<1.5}, rdf, "原E: ATR%<1.5, 做多2x做空1.5x")
weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<2.0,
               1.5: lambda r: r["dir"]=="short" and r["atr_pct"]<2.0}, rdf, "E1: ATR%<2.0, 做多2x做空1.5x")

print(f"\n{D}\n细分：ATR%<1.0和[1.0,2.0)分开\n{D}")
weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<1.0,
               1.5: lambda r: (r["dir"]=="short" and r["atr_pct"]<1.0) or (1.0<=r["atr_pct"]<2.0)}, rdf,
              "E2: 做多&ATR%<1→2x; 做空&ATR%<1或ATR%[1,2)→1.5x")
weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<1.0,
               1.5: lambda r: r["atr_pct"]<2.0 and not (r["dir"]=="long" and r["atr_pct"]<1.0)}, rdf,
              "E3: 做多&ATR%<1→2x; 其余ATR%<2→1.5x")

print(f"\n{D}\n高波动做空不降档（做空[1.5,2.0)均0.88>做多0.51）\n{D}")
weighted_eval({2: lambda r: r["dir"]=="long" and r["atr_pct"]<1.0,
               1.5: lambda r: (r["dir"]=="short" and r["atr_pct"]<2.0) or (r["dir"]=="long" and 1.0<=r["atr_pct"]<2.0)}, rdf,
              "E4: 做多&ATR%<1→2x; 其余ATR%<2→1.5x(含做空高波动)")

print(f"\n{D}\n2026年回撤分析\n{D}")
rdf_e = rdf.copy()
rdf_e["tier"] = 1.0
rdf_e.loc[(rdf_e["dir"]=="long")&(rdf_e["atr_pct"]<1.5), "tier"] = 2.0
rdf_e.loc[(rdf_e["dir"]=="short")&(rdf_e["atr_pct"]<1.5), "tier"] = 1.5
rdf_e["wr"] = rdf_e["r"] * rdf_e["tier"]
rdf_e = rdf_e.sort_values("sig_idx")
rdf_e["cum_wr"] = rdf_e["wr"].cumsum()
rdf_e["dd"] = rdf_e["cum_wr"] - rdf_e["cum_wr"].cummax()
worst = rdf_e.nsmallest(5, "dd")
print(f"  回撤最深的5笔:")
for _, r in worst.iterrows():
    print(f"    {int(r['year'])} sig_idx={r['sig_idx']} dir={r['dir']} ATR%={r['atr_pct']:.2f} "
          f"tier={r['tier']} R={r['r']:.2f} 加权R={r['wr']:.2f} cum={r['cum_wr']:.1f} dd={r['dd']:.1f}")
