"""量比降权方案快速测试。加载已保存的rdf，测试不同降权策略。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig

cfg = BacktestConfig()
cache = cfg.base_dir / "cache"
rdf = pd.read_parquet(cache / "tier_v4_rdf.parquet")

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
        print(f"  {tier}x档: {stats(sub)}  加权R={(sub['r']*tier).sum():.1f}")
    if (rdf["tier"] == 1.0).any() and 1.0 not in tier_funcs:
        sub = rdf[rdf["tier"] == 1.0]
        print(f"  1x档(默认): {stats(sub)}  加权R={sub['r'].sum():.1f}")
    print(f"  → 加权总R={total_wr:.1f}  回撤={max_dd:.1f}R")
    return total_wr, max_dd

D = f"{'━'*90}"
print("=" * 90)
print(f"量比降权方案测试（{len(rdf)}单，基准: 纯ATR%三档=1465.5R/-9.3, 原方案F=1623.9R/-9.5）")
print("=" * 90)

print(f"\n{D}\n基准对比\n{D}")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0, 1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5}, rdf, "纯ATR%三档(基准)")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or r["vol_ratio"] >= 2.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 and r["vol_ratio"] < 2.0}, rdf, "原方案F: ATR%<1或量比>=2→2x")

print(f"\n{D}\n降权方案：量比不单独触发2x，最高到1.5x\n{D}")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 or (r["atr_pct"] >= 1.5 and r["vol_ratio"] >= 2.0)}, rdf, "降1: ATR%<1→2x, 量比>=2只升到1.5x")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 or r["vol_ratio"] >= 2.0}, rdf, "降2: ATR%<1→2x, ATR%<1.5或量比>=2→1.5x")

print(f"\n{D}\n降权方案：量比阈值提高\n{D}")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or r["vol_ratio"] >= 3.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 and r["vol_ratio"] < 3.0}, rdf, "降3: 量比>=3才触发2x(原>=2)")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or r["vol_ratio"] >= 5.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 and r["vol_ratio"] < 5.0}, rdf, "降4: 量比>=5才触发2x(极端放量)")

print(f"\n{D}\n降权方案：量比需配合ATR%才触发2x\n{D}")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or (r["atr_pct"] < 1.5 and r["vol_ratio"] >= 2.0),
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 and r["vol_ratio"] < 2.0}, rdf, "降5: ATR%<1或(ATR%<1.5且量比>=2)→2x")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or (r["atr_pct"] < 2.0 and r["vol_ratio"] >= 2.0),
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 and not (r["atr_pct"] < 2.0 and r["vol_ratio"] >= 2.0)}, rdf, "降6: ATR%<1或(ATR%<2且量比>=2)→2x")

print(f"\n{D}\n降权方案：ATR%为主+量比微调\n{D}")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5,
               0.5: lambda r: r["atr_pct"] >= 1.5 and r["vol_ratio"] < 1.2}, rdf, "降7: 纯ATR%三档+缩量降0.5x(量比只降不升)")
