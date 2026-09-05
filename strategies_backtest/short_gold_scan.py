"""找出做空的高质量区间（均R>=1.0且样本>=5）。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig

rdf = pd.read_parquet(BacktestConfig().base_dir / "cache" / "tier_v4_rdf.parquet")
short = rdf[rdf["dir"]=="short"]

def stats(sub):
    if sub.empty: return "—"
    sr = sub["r"]
    w = sr[sr>0].sum(); l = sr[sr<=0].sum()
    pf = f"{w/abs(l):.1f}" if l != 0 else "∞"
    return f"{len(sub)}单/{sr.sum():.1f}R/均{sr.mean():.2f}/PF{pf}/胜{(sr>0).mean():.0%}"

dev_bins = [(-2,-1),(-1,0),(0,1),(1,2),(2,4),(4,6),(6,15)]
atr_bins = [(0,0.5),(0.5,1.0),(1.0,1.5),(1.5,2.0),(2.0,100)]

print("做空各格子（均R>=1.0且样本>=5）:")
print(f"  {'dev':>10} {'ATR%':>12} {'统计':>40}")
cells = []
for dlo, dhi in dev_bins:
    for alo, ahi in atr_bins:
        sub = short[(short['dev']>=dlo)&(short['dev']<dhi)&(short['atr_pct']>=alo)&(short['atr_pct']<ahi)]
        if len(sub) >= 5 and sub['r'].mean() >= 1.0:
            s = stats(sub)
            cells.append((dlo, dhi, alo, ahi, s))
            print(f"  dev[{dlo},{dhi})  ATR%[{alo},{ahi})  {s}")

print("\n做空候选黄金区间（合并相邻格子）:")
candidates = [
    ("dev[0,2)&ATR%[0.5,1.0)", lambda r: 0<=r['dev']<2 and 0.5<=r['atr_pct']<1.0),
    ("dev[1,2)&ATR%[1.0,1.5)", lambda r: 1<=r['dev']<2 and 1.0<=r['atr_pct']<1.5),
    ("dev[6,15)&ATR%[1.0,1.5)", lambda r: 6<=r['dev']<15 and 1.0<=r['atr_pct']<1.5),
    ("dev[4,6)&ATR%[1.5,2.0)", lambda r: 4<=r['dev']<6 and 1.5<=r['atr_pct']<2.0),
    ("dev[-2,-1)&ATR%[1.5,2.0)", lambda r: -2<=r['dev']<-1 and 1.5<=r['atr_pct']<2.0),
    ("dev[0,2)&ATR%[0.5,1.5)", lambda r: 0<=r['dev']<2 and 0.5<=r['atr_pct']<1.5),
    ("dev[1,2)&ATR%[0.5,2.0)", lambda r: 1<=r['dev']<2 and 0.5<=r['atr_pct']<2.0),
    ("dev[0,1)&ATR%[0.5,1.0)", lambda r: 0<=r['dev']<1 and 0.5<=r['atr_pct']<1.0),
    ("dev[4,6)&ATR%[1.0,2.0)", lambda r: 4<=r['dev']<6 and 1.0<=r['atr_pct']<2.0),
    ("dev[6,15)&ATR%[1.0,1.5)∪dev[0,2)&ATR%[0.5,1.0)", lambda r: (6<=r['dev']<15 and 1.0<=r['atr_pct']<1.5) or (0<=r['dev']<2 and 0.5<=r['atr_pct']<1.0)),
]
for label, fn in candidates:
    sub = short[short.apply(fn, axis=1)]
    print(f"  {label}: {stats(sub)}")

print("\n做空整体参考:")
for lo, hi in atr_bins:
    print(f"  ATR%[{lo},{hi}): {stats(short[(short['atr_pct']>=lo)&(short['atr_pct']<hi)])}")
