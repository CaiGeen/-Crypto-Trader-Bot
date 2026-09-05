"""生成偏离度×ATR%×方向分布报告(Markdown格式)。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig

cfg = BacktestConfig()
rdf = pd.read_parquet(cfg.base_dir / "cache" / "tier_v4_rdf.parquet")

dev_bins = [(-2,-1),(-1,0),(0,1),(1,2),(2,4),(4,6),(6,15)]
atr_bins = [(0,0.5),(0.5,1.0),(1.0,1.5),(1.5,2.0),(2.0,100)]

def cell(sub):
    if sub.empty: return "—"
    sr = sub["r"]
    w = sr[sr>0].sum(); l = sr[sr<=0].sum()
    pf = f"{w/abs(l):.1f}" if l != 0 else "∞"
    return f"{len(sub)}单/{sr.sum():.1f}R/均{sr.mean():.2f}/PF{pf}/胜{(sr>0).mean():.0%}"

def build_table(rdf_sub, label, total_n, total_r, total_mean):
    lines = []
    lines.append(f"### {label}（{total_n}单/{total_r:.1f}R/均{total_mean:.2f}）\n")
    hdr = "| dev\\ATR% |" + "|".join(f" ATR%[{lo},{hi}) " for lo,hi in atr_bins) + "| 合计 |"
    sep = "|---|" + "|".join(["---|"]*(len(atr_bins)+1))
    lines.append(hdr)
    lines.append(sep)
    for dlo, dhi in dev_bins:
        cells = []
        for alo, ahi in atr_bins:
            sub = rdf_sub[(rdf_sub['dev']>=dlo)&(rdf_sub['dev']<dhi)&(rdf_sub['atr_pct']>=alo)&(rdf_sub['atr_pct']<ahi)]
            cells.append(cell(sub))
        total = rdf_sub[(rdf_sub['dev']>=dlo)&(rdf_sub['dev']<dhi)]
        lines.append(f"| dev[{dlo},{dhi}) |" + "|".join(f" {c} " for c in cells) + f"| {cell(total)} |")
    col_cells = []
    for alo, ahi in atr_bins:
        sub = rdf_sub[(rdf_sub['atr_pct']>=alo)&(rdf_sub['atr_pct']<ahi)]
        col_cells.append(cell(sub))
    lines.append(f"| **合计** |" + "|".join(f" **{c}** " for c in col_cells) + f"| **{cell(rdf_sub)}** |")
    return "\n".join(lines)

md = []
md.append("# 偏离度 × ATR% × 方向 分布详细报告\n")
md.append(f"> 生成时间：2026-08-24 | 数据：标记价方案A | 共 {len(rdf)} 单\n")
md.append("---\n")

md.append("## 一、指标定义\n")
md.append("- **偏离度** dev = (close[i-1] - 日线EMA70) / 日线EMA70 × 100")
md.append("  - dev<0：收盘价在日线EMA70下方")
md.append("  - dev≥0：收盘价在日线EMA70上方")
md.append("- **ATR%** = ATR[i-1] / close[i-1] × 100（信号K前一根的波动率）")
md.append("- **R值**：基于成交价(fill_price)计算，含加仓R\n")
md.append("- 格式：单数/累计R/均R/PF/胜率\n")
md.append("---\n")

md.append("## 二、做多全景表\n")
long_sub = rdf[rdf["dir"]=="long"]
md.append(build_table(long_sub, "做多", len(long_sub), long_sub['r'].sum(), long_sub['r'].mean()))
md.append("\n---\n")

md.append("## 三、做空全景表\n")
short_sub = rdf[rdf["dir"]=="short"]
md.append(build_table(short_sub, "做空", len(short_sub), short_sub['r'].sum(), short_sub['r'].mean()))
md.append("\n---\n")

md.append("## 四、多空合并表\n")
md.append(build_table(rdf, "多空合并", len(rdf), rdf['r'].sum(), rdf['r'].mean()))
md.append("\n---\n")

md.append("## 五、关键发现\n")

md.append("### 5.1 做多黄金带：ATR%[0.5,1.0)\n")
gold = long_sub[(long_sub['atr_pct']>=0.5)&(long_sub['atr_pct']<1.0)]
md.append(f"- {len(gold)}单/{gold['r'].sum():.1f}R/均{gold['r'].mean():.2f}/PF{gold['r'][gold['r']>0].sum()/abs(gold['r'][gold['r']<=0].sum()):.1f}")
gold2 = long_sub[(long_sub['atr_pct']>=0.5)&(long_sub['atr_pct']<1.0)&(long_sub['dev']<0)]
md.append(f"- 其中dev<0（EMA70下方）：{len(gold2)}单/{gold2['r'].sum():.1f}R/均{gold2['r'].mean():.2f} ← **极品中的极品**\n")

md.append("### 5.2 做多垃圾区：dev[0,2) & ATR%≥1.0\n")
trash = long_sub[(long_sub['dev']>=0)&(long_sub['dev']<2)&(long_sub['atr_pct']>=1.0)]
md.append(f"- {len(trash)}单/{trash['r'].sum():.1f}R/均{trash['r'].mean():.2f}")
md.append(f"- E'给这些单2x档，实际均R仅{trash['r'].mean():.2f}，拉低2x档质量\n")

md.append("### 5.3 做空无黄金带\n")
md.append(f"- 做空整体均R{short_sub['r'].mean():.2f}，远低于做多均R{long_sub['r'].mean():.2f}")
best_short = short_sub[(short_sub['dev']>=0)&(short_sub['dev']<2)&(short_sub['atr_pct']>=0.5)&(short_sub['atr_pct']<1.0)]
md.append(f"- 做空最强区间dev[0,2)&ATR%[0.5,1.0)：{len(best_short)}单/均{best_short['r'].mean():.2f}")
md.append(f"- dev[-2,0)做空弱：{len(short_sub[short_sub['dev']<0])}单/均{short_sub[short_sub['dev']<0]['r'].mean():.2f}\n")

md.append("### 5.4 ATR%的单调性\n")
for lo, hi in atr_bins:
    sub = rdf[(rdf['atr_pct']>=lo)&(rdf['atr_pct']<hi)]
    md.append(f"- ATR%[{lo},{hi})：{cell(sub)}")
md.append("")

md.append("### 5.5 偏离度的非单调性\n")
for lo, hi in dev_bins:
    sub = rdf[(rdf['dev']>=lo)&(rdf['dev']<hi)]
    md.append(f"- dev[{lo},{hi})：{cell(sub)}")
md.append("- 偏离度非单调：dev[-1,0)最强(均1.51)，dev[2,4)最弱(均0.81)")
md.append("- 但偏离度×方向有区分力：dev<0做多强做空弱，dev[0,2)做空强做多弱\n")

md.append("---\n")
md.append("## 六、对E'方案的启示\n")
md.append("E'规则：做多&ATR%<2→2x，做空&ATR%<2→1.5x，其余→1x\n")
md.append("**E'的优势**：")
md.append(f"- 做多&ATR%[0.5,1.0)的126单均2.56被2x → 贡献{long_sub[(long_sub['atr_pct']>=0.5)&(long_sub['atr_pct']<1.0)]['r'].sum()*2:.1f}R")
md.append(f"- 做空&ATR%<2的320单均0.93被1.5x → 贡献{short_sub[short_sub['atr_pct']<2.0]['r'].sum()*1.5:.1f}R\n")
md.append("**E'的盲区**：")
trash2 = long_sub[(long_sub['dev']>=0)&(long_sub['dev']<2)&(long_sub['atr_pct']>=1.0)&(long_sub['atr_pct']<2.0)]
md.append(f"- dev[0,2)&ATR%[1.0,2.0)做多{len(trash2)}单/均{trash2['r'].mean():.2f}被2x → 低质量单占2x档")
md.append("- 但用偏离度拆分做多2x档测试表明会损失更多R（见11.6.1节），不值得\n")
md.append("**结论**：E'已是最优纯ATR%方案，偏离度无法在分档层面进一步提升。\n")

out_path = cfg.base_dir / "DEV_ATR_PANORAMA_REPORT.md"
with open(out_path, "w", encoding="utf-8") as f:
    f.write("\n".join(md))
print(f"报告已保存: {out_path}")
