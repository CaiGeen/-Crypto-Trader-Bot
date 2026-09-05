"""事前变量初筛：哪些入场即知/早期路径信息能预判单段空间（<2% 靶区）。

背景：LEG_AMPLITUDE_REPORT.md §8 的 2% 门槛是事后分桶（波峰要等段走完才确认）。
本脚本把候选事前变量逐个做条件统计：
  A. t=0（突破K收盘即知）：突破幅度R、突破K实体效率、收盘位置、量比、6K区间紧致度
  B. t=3（第3K收盘即知，对齐策略3K时间锁）：存活状态 + 前3根MFE
  C. 砍段价值预览：第3K仍存活但MFE<1%的段，改为第3K收盘离场的盈亏对比
注意：单变量、全样本内统计——发现的规则必须再过 order_state_backtest_6k 分年验证。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
from scipy import stats as sps

HERE = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(os.path.join(HERE, "results", "单段涨跌幅_6K突破线到KAMA线.csv"))
AMP, PNL = "涨跌幅%(突破线→波峰)", "盈亏%(突破线→KAMA线)"

raw = pd.read_parquet(os.path.join(HERE, "cache", "BTCUSDTUSDT_4h.parquet"))
h, l, c, o, v = (raw[k].to_numpy(dtype=float) for k in ("high", "low", "close", "open", "volume"))
m = len(c)
tr = np.zeros(m)
tr[0] = h[0] - l[0]
for i in range(1, m):
    tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
atr = np.zeros(m)
atr[0] = tr[0]
aa = 1.0 / 20
for i in range(1, m):
    atr[i] = aa * tr[i] + (1 - aa) * atr[i - 1]

rows = []
for _, r in df.iterrows():
    i = int(r["突破K索引"])
    d = 1.0 if r["方向"] == "做多" else -1.0
    entry = float(r["突破线(起点)"])
    atrv = atr[i - 1]
    # t=0 特征
    exc0 = d * (c[i] - entry) / entry * 100            # 突破幅度%（收盘超出线）
    exc0_r = exc0 / (atrv / entry * 100)                # 突破幅度R
    rng = h[i - 6:i].max() - l[i - 6:i].min()
    body_eff = abs(c[i] - o[i]) / (h[i] - l[i]) if h[i] > l[i] else 0.0
    close_pos = (c[i] - l[i]) / (h[i] - l[i]) if h[i] > l[i] else 1.0
    vol_ratio = v[i] / v[max(i - 20, 0):i].mean() if i > 0 else np.nan
    compact = rng / atrv if atrv > 0 else np.nan        # 6K区间紧致度（区间高/ATR）
    # t=3 路径（第3K收盘即知：bar i..i+3 的盘中极值偏移，方向化后恒为已实现的有利幅度）
    ex = int(r["KAMA终结K索引"]) if not bool(r["是否未终结"]) else m - 1
    alive3 = ex > i + 3
    if i + 3 < m:
        seg_h, seg_l = h[i:i + 4], l[i:i + 4]
        mfe3 = ((seg_h.max() - entry) if d > 0 else (entry - seg_l.min())) / entry * 100
    else:
        mfe3 = np.nan
    rows.append({"exc0": exc0, "exc0_r": exc0_r, "body": body_eff, "cpos": close_pos,
                 "volr": vol_ratio, "compact": compact, "alive3": alive3, "mfe3": mfe3,
                 "amp": r[AMP], "pnl": r[PNL], "ex_idx": ex, "i": i, "d": r["方向"], "entry": entry})
X = pd.DataFrame(rows)
X["big"] = X["amp"] >= 3.0  # 空间≥3%（跟踪胜率83%+的档）

print("===== A. t=0 入场即知变量（与最终空间的相关性） =====")
for col, name in [("exc0_r", "突破幅度R"), ("exc0", "突破幅度%"), ("body", "突破K实体效率"),
                  ("cpos", "收盘位置"), ("volr", "量比"), ("compact", "区间紧致度")]:
    ok = X[["amp"] + [col]].dropna()
    rho, p = sps.spearmanr(ok[col], ok["amp"])
    print(f"{name:>8}: Spearman ρ={rho:+.2f} (p={p:.1e})  n={len(ok)}")

print("\n—— 突破幅度R 分桶 ——")
bR = [(0, 0.3), (0.3, 0.6), (0.6, 1.0), (1.0, 1.5), (1.5, 99)]
print(f"{'突破幅度R':>10} {'n':>5} {'空间中位':>8} {'P(空间≥3%)':>10} {'盈亏中位':>8}")
for lo, hi in bR:
    s = X[(X["exc0_r"] >= lo) & (X["exc0_r"] < hi)]
    if len(s):
        print(f"[{lo:>4},{hi if hi < 99 else '∞':>4}) {len(s):>5} {s['amp'].median():>8.2f} {s['big'].mean():>10.1%} {s['pnl'].median():>8.2f}")

print("\n—— 量比 分桶 ——")
for lo, hi in [(0, 1), (1, 2), (2, 99)]:
    s = X[(X["volr"] >= lo) & (X["volr"] < hi)]
    print(f"[{lo},{hi if hi < 99 else '∞'}) n={len(s):>4} 空间中位={s['amp'].median():.2f}% P(≥3%)={s['big'].mean():.1%} 盈亏中位={s['pnl'].median():.2f}%")

print("\n===== B. t=3 第3K收盘即知（对齐策略3K时间锁） =====")
dead3 = X[~X["alive3"]]
print(f"第3K内已终结: n={len(dead3)} ({len(dead3)/len(X):.1%})  空间中位={dead3['amp'].median():.2f}%  盈亏中位={dead3['pnl'].median():.2f}%")
al = X[X["alive3"]].dropna(subset=["mfe3"])
print(f"\n第3K仍存活，按前3根MFE分桶（n={len(al)}）：")
print(f"{'前3K MFE':>10} {'n':>5} {'占比':>6} {'空间中位':>8} {'P(空间≥3%)':>10} {'盈亏中位':>8}")
for lo, hi in [(0, 0.5), (0.5, 1), (1, 2), (2, 4), (4, 999)]:
    s = al[(al["mfe3"] >= lo) & (al["mfe3"] < hi)]
    if len(s):
        print(f"[{lo:>4},{hi if hi < 999 else '∞':>4}) {len(s):>5} {len(s)/len(al):>6.1%} {s['amp'].median():>8.2f} {s['big'].mean():>10.1%} {s['pnl'].median():>8.2f}")

print("\n===== C. 砍段价值预览：第3K存活但 MFE<1% → 改第3K收盘离场 =====")
cut = al[al["mfe3"] < 1.0].copy()
keep = al[al["mfe3"] >= 1.0]
alt = []
for _, r in cut.iterrows():
    j = int(r["i"]) + 3
    d = 1.0 if r["d"] == "做多" else -1.0
    alt.append(d * (c[j] - r["entry"]) / r["entry"] * 100)
cut["alt_pnl"] = alt
print(f"目标段: n={len(cut)} ({len(cut)/len(X):.1%} of 全部段)")
print(f"  原策略(持有到KAMA线): 合计={cut['pnl'].sum():+.1f}%  中位={cut['pnl'].median():+.2f}%")
print(f"  改第3K收盘离场:       合计={cut['alt_pnl'].sum():+.1f}%  中位={cut['alt_pnl'].median():+.2f}%")
print(f"  逐段改善>0占比: {(cut['alt_pnl'] > cut['pnl']).mean():.1%}")
print(f"对照(第3K MFE≥1%的段): n={len(keep)} 合计={keep['pnl'].sum():+.1f}% 中位={keep['pnl'].median():+.2f}%")
rho, p = sps.spearmanr(al["mfe3"], al["amp"])
print(f"第3K MFE ~ 最终空间: Spearman ρ={rho:.2f} (p={p:.1e})")
