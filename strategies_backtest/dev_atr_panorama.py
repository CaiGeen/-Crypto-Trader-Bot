"""偏离度×ATR%×方向 全景交叉表。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig

rdf = pd.read_parquet(cfg.base_dir / "cache" / "tier_v4_rdf.parquet") if False else pd.read_parquet(BacktestConfig().base_dir / "cache" / "tier_v4_rdf.parquet")

dev_bins = [(-2,-1),(-1,0),(0,1),(1,2),(2,4),(4,6),(6,15)]
atr_bins = [(0,0.5),(0.5,1.0),(1.0,1.5),(1.5,2.0),(2.0,100)]

def cell(sub):
    if sub.empty: return "      —"
    sr = sub["r"]
    return f"{len(sub):>3}/{sr.sum():>5.1f}/均{sr.mean():.2f}"

D = "━"*120
print("="*120)
print(f"偏离度×ATR%×方向 全景交叉表（{len(rdf)}单）")
print("="*120)
print("  格式: 单数/累计R/均R")

for d, lbl in [("long","做多"),("short","做空")]:
    sub_d = rdf[rdf["dir"]==d]
    print(f"\n{D}")
    print(f"  {lbl}（{len(sub_d)}单/{sub_d['r'].sum():.1f}R/均{sub_d['r'].mean():.2f}）")
    print(D)
    hdr = "  dev\\ATR%    " + "  ".join(f"ATR%[{lo},{hi})" + " "*max(0,14-len(f"ATR%[{lo},{hi})")) for lo,hi in atr_bins) + "    合计"
    print(hdr)
    for dlo, dhi in dev_bins:
        row = f"  [{dlo},{dhi}){'':>6}" if dhi <= 0 or dlo >= 0 else f"  [{dlo},{dhi}){'':>6}"
        row = f"  dev[{dlo:>2},{dhi:<2})  "
        cells = []
        for alo, ahi in atr_bins:
            sub = sub_d[(sub_d['dev']>=dlo)&(sub_d['dev']<dhi)&(sub_d['atr_pct']>=alo)&(sub_d['atr_pct']<ahi)]
            cells.append(cell(sub))
        total = sub_d[(sub_d['dev']>=dlo)&(sub_d['dev']<dhi)]
        row += "  ".join(f"{c:<14}" for c in cells) + f"  {cell(total)}"
        print(row)
    col_tot = []
    for alo, ahi in atr_bins:
        sub = sub_d[(sub_d['atr_pct']>=alo)&(sub_d['atr_pct']<ahi)]
        col_tot.append(cell(sub))
    print(f"  合计        " + "  ".join(f"{c:<14}" for c in col_tot) + f"  {cell(sub_d)}")

print(f"\n{D}")
print("  多空合并（不区分方向）")
print(D)
hdr = "  dev\\ATR%    " + "  ".join(f"ATR%[{lo},{hi})" + " "*max(0,14-len(f"ATR%[{lo},{hi})")) for lo,hi in atr_bins) + "    合计"
print(hdr)
for dlo, dhi in dev_bins:
    row = f"  dev[{dlo:>2},{dhi:<2})  "
    cells = []
    for alo, ahi in atr_bins:
        sub = rdf[(rdf['dev']>=dlo)&(rdf['dev']<dhi)&(rdf['atr_pct']>=alo)&(rdf['atr_pct']<ahi)]
        cells.append(cell(sub))
    total = rdf[(rdf['dev']>=dlo)&(rdf['dev']<dhi)]
    row += "  ".join(f"{c:<14}" for c in cells) + f"  {cell(total)}"
    print(row)
col_tot = []
for alo, ahi in atr_bins:
    sub = rdf[(rdf['atr_pct']>=alo)&(rdf['atr_pct']<ahi)]
    col_tot.append(cell(sub))
print(f"  合计        " + "  ".join(f"{c:<14}" for c in col_tot) + f"  {cell(rdf)}")
