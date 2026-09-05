"""单段涨跌幅（突破线→波峰）统计报告：分布、多空、超越概率、ATR标定、趋势、时长、聚类、盈亏衔接。

数据：results/单段涨跌幅_6K突破线到KAMA线.csv（1118段）
ATR：RMA(TR,20) 对齐 TradingView，取信号前一根收盘确认值 atr[i-1]（无前视）
输出：results/单段涨跌幅_*.png 五张图 + 终端统计量（LEG_AMPLITUDE_REPORT.md 的数据源）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats as sps

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

HERE = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(os.path.join(HERE, "results", "单段涨跌幅_6K突破线到KAMA线.csv"))
AMP, PNL = "涨跌幅%(突破线→波峰)", "盈亏%(突破线→KAMA线)"

# —— 合并 ATR（前一根收盘确认，无前视）——
cfg_dir = os.path.join(HERE, "cache")
raw = pd.read_parquet(os.path.join(cfg_dir, "BTCUSDTUSDT_4h.parquet"))
h, l, c = (raw[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
m = len(c)
tr = np.zeros(m)
tr[0] = h[0] - l[0]
for i in range(1, m):
    tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
atr = np.zeros(m)
atr[0] = tr[0]
a = 1.0 / 20
for i in range(1, m):
    atr[i] = a * tr[i] + (1 - a) * atr[i - 1]

df["ATR"] = atr[np.clip(df["突破K索引"] - 1, 0, None)]
df["ATR%"] = df["ATR"] / df["突破线(起点)"] * 100
df["峰值R"] = df[AMP] / df["ATR%"]
df["到峰K数"] = df["波峰K索引"] - df["突破K索引"]
df["捕获率"] = df[PNL] / df[AMP]

x = df[AMP].to_numpy()
lx = np.log(x)


def save(fig, name):
    p = os.path.join(HERE, "results", name)
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    print(f"已保存 {name}")


def q(s, qs=(0.1, 0.25, 0.5, 0.75, 0.9, 0.95)):
    return "  ".join(f"P{int(q_*100)}={s.quantile(q_):.2f}" for q_ in qs)


# ============ 1. 直方图 ============
fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
axes[0].hist(x, bins=np.arange(0, 26, 0.5), color="#4C78A8", edgecolor="white")
axes[0].axvline(np.median(x), color="#E45756", lw=2, label=f"中位 {np.median(x):.2f}%")
axes[0].axvline(x.mean(), color="#F58518", lw=2, ls="--", label=f"均值 {x.mean():.2f}%")
axes[0].set_xlabel("单段涨跌幅 %"); axes[0].set_ylabel("段数")
axes[0].set_title("单段涨跌幅直方图（0.5pp分箱）"); axes[0].legend()
axes[1].hist(lx, bins=40, color="#54A24B", edgecolor="white", density=True)
xs = np.linspace(lx.min(), lx.max(), 200)
axes[1].plot(xs, sps.norm.pdf(xs, lx.mean(), lx.std()), lw=2, color="#E45756",
             label=f"正态拟合(μ={lx.mean():.2f},σ={lx.std():.2f})")
axes[1].set_xlabel("ln(单段涨跌幅)"); axes[1].set_ylabel("密度")
axes[1].set_title(f"对数变换（偏度={sps.skew(lx):.2f}）"); axes[1].legend()
save(fig, "单段涨跌幅_直方图.png")

# ============ 2. 生存曲线 + 对数正态 ============
xs_sorted = np.sort(x)[::-1]
surv = np.arange(1, len(x) + 1) / len(x)
mu, s = lx.mean(), lx.std()
fit = sps.lognorm.sf(xs_sorted, s=s, scale=np.exp(mu))
fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
axes[0].plot(xs_sorted, surv, lw=2, label="经验生存曲线 P(X≥x)")
axes[0].plot(xs_sorted, fit, lw=2, ls="--", label=f"对数正态拟合(μ={mu:.2f},σ={s:.2f})")
for t in (2, 5, 10):
    axes[0].axvline(t, color="gray", ls=":", lw=1)
axes[0].set_xlabel("单段涨跌幅 %"); axes[0].set_ylabel("超越概率")
axes[0].set_title("生存曲线：任意段 ≥x 的概率"); axes[0].legend()
axes[1].loglog(xs_sorted, surv, lw=2, label="经验(log-log)")
axes[1].loglog(xs_sorted, fit, lw=2, ls="--", label="对数正态")
axes[1].set_xlabel("单段涨跌幅 % (log)"); axes[1].set_ylabel("P(X≥x) (log)")
axes[1].set_title("log-log：尾部衰减形态"); axes[1].legend()
save(fig, "单段涨跌幅_生存曲线.png")

# ============ 3. 分组箱线图 ============
bins_atr = [(0, 1.5, "<1.5%"), (1.5, 2, "1.5-2%"), (2, 3, "2-3%"), (3, 5, "3-5%"), (5, 99, "≥5%")]
df["ATR档"] = pd.cut(df["ATR%"], [b[0] for b in bins_atr] + [99], labels=[b[2] for b in bins_atr])
bins_pk = [(-1, 0, "0(突破当根)"), (0, 4, "1-3"), (4, 9, "4-8"), (9, 21, "9-20"), (21, 999, "≥21")]
df["到峰K数档"] = pd.cut(df["到峰K数"], [b[0] for b in bins_pk] + [999], labels=[b[2] for b in bins_pk])


def box(ax, groups, labels, title, ylim=(0, 15)):
    bp = ax.boxplot([g.dropna() for g in groups], tick_labels=labels, showmeans=True,
                    patch_artist=True, medianprops=dict(color="#E45756", lw=2))
    for patch, col in zip(bp["boxes"], plt.cm.tab10(np.linspace(0, 0.9, len(groups)))):
        patch.set_facecolor(col); patch.set_alpha(0.6)
    ax.set_ylim(*ylim); ax.set_ylabel("单段涨跌幅 %"); ax.set_title(title)


fig, axes = plt.subplots(2, 2, figsize=(14, 9))
years = sorted(df["年份"].unique())
box(axes[0][0], [df[df["方向"] == d][AMP] for d in ["做多", "做空"]], ["做多", "做空"], "按方向")
box(axes[0][1], [df[df["年份"] == y][AMP] for y in years], [str(y) for y in years], "按年度")
box(axes[1][0], [df[df["ATR档"] == b[2]][AMP] for b in bins_atr], [b[2] for b in bins_atr], "按突破时ATR%分档")
box(axes[1][1], [df[df["到峰K数档"] == b[2]][AMP] for b in bins_pk], [b[2] for b in bins_pk], "按到峰K数分档")
save(fig, "单段涨跌幅_箱线图组.png")

# ============ 4. ATR关系 + 峰值R ============
fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
axes[0].scatter(df["ATR%"], df[AMP], s=8, alpha=0.35, color="#4C78A8")
med_x, med_y = [], []
for lo, hi, tag in bins_atr:
    sub = df[(df["ATR%"] >= lo) & (df["ATR%"] < hi)]
    med_x.append(sub["ATR%"].median()); med_y.append(sub[AMP].median())
axes[0].plot(med_x, med_y, "o-", color="#E45756", lw=2, label="各档中位")
axes[0].set_xscale("log"); axes[0].set_yscale("log")
axes[0].set_xlabel("突破时ATR%"); axes[0].set_ylabel("单段涨跌幅 %")
axes[0].set_title("涨跌幅 vs ATR%（双对数）"); axes[0].legend()
axes[1].hist(df["峰值R"], bins=np.arange(0, 14, 0.25), color="#72B7B2", edgecolor="white")
axes[1].axvline(df["峰值R"].median(), color="#E45756", lw=2, label=f"中位 {df['峰值R'].median():.2f}R")
axes[1].set_xlabel("峰值R = 涨跌幅 / ATR%"); axes[1].set_ylabel("段数")
axes[1].set_title("规模无关量：峰值R分布"); axes[1].legend()
save(fig, "单段涨跌幅_ATR关系.png")

# ============ 5. 年度趋势 + 滚动中位 ============
fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
for ax, d in zip(axes, ["做多", "做空"]):
    g = df[df["方向"] == d].groupby("年份")[AMP]
    med, q25, q75 = g.median(), g.quantile(.25), g.quantile(.75)
    ax.fill_between(med.index, q25, q75, alpha=.25, label="P25-P75")
    ax.plot(med.index, med, "o-", lw=2, color="#E45756", label="中位")
    g.mean().plot(ax=ax, style="--", lw=2, color="#F58518", label="均值")
    ax.set_ylabel("单段涨跌幅 %"); ax.set_title(f"{d}：年度趋势")
    ax.set_xticks(med.index); ax.legend()
roll = df.sort_values("突破K索引")[AMP].rolling(60, min_periods=30).median()
axes[1].plot(np.arange(len(roll)), roll, lw=2, color="#4C78A8")
axes[1].set_xlabel("段序号（按时间，两方向合并）"); axes[1].set_ylabel("滚动60段中位 %")
axes[1].set_title("全历史滚动中位（不分方向）")
save(fig, "单段涨跌幅_年度趋势.png")

# ============ 统计量输出 ============
print("\n===== 1 分布形态 =====")
print(f"n={len(x)} 中位={np.median(x):.3f}% 均值={x.mean():.3f}% std={x.std():.3f} "
      f"偏度={sps.skew(x):.2f} 峰度={sps.kurtosis(x):.2f} CV={x.std()/x.mean():.2f} min={x.min():.3f} max={x.max():.2f}")
print(f"分位数: {q(pd.Series(x))}")
ks_stat, ks_p = sps.kstest(lx, "norm", args=(mu, s))
print(f"对数正态检验: ln偏度={sps.skew(lx):.3f}  KS D={ks_stat:.4f} p={ks_p:.4f}")

print("\n===== 2 多空对比 =====")
for d in ["做多", "做空"]:
    s = df[df["方向"] == d][AMP]
    print(f"{d}: n={len(s)} {q(s)}")
u, p = sps.mannwhitneyu(df[df["方向"] == "做多"][AMP], df[df["方向"] == "做空"][AMP])
ks2 = sps.ks_2samp(df[df["方向"] == "做多"][AMP], df[df["方向"] == "做空"][AMP])
print(f"Mann-Whitney p={p:.3f}  KS D={ks2.statistic:.3f} p={ks2.pvalue:.3f}")

print("\n===== 3 超越概率 =====")
for t in [0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30]:
    print(f"P(≥{t:>2}%) = {(x >= t).mean():.3f}  (约{max(1/(x >= t).mean(), 1):.0f}段里1段)")
top5 = df.nlargest(int(len(df) * 0.05), AMP)[AMP].sum()
print(f"Top5%段({int(len(df)*0.05)}段)贡献总涨幅的 {top5 / x.sum():.1%}")

print("\n===== 4 ATR标定与规模无关性 =====")
print(f"峰值R: 中位={df['峰值R'].median():.2f} 均值={df['峰值R'].mean():.2f} P90={df['峰值R'].quantile(.9):.2f}")
for lo, hi, tag in bins_atr:
    sub = df[df["ATR档"] == tag]
    print(f"ATR{tag:>7}: n={len(sub):>4} 涨跌幅中位={sub[AMP].median():.2f}% 峰值R中位={sub['峰值R'].median():.2f} CV={sub['峰值R'].std()/sub['峰值R'].mean():.2f}")
print(f"Spearman: 涨跌幅~ATR%={df[AMP].corr(df['ATR%'], method='spearman'):.2f}")

print("\n===== 5 年度趋势 =====")
yr = df.groupby("年份")[AMP].agg(["count", "median", "mean"]).round(2)
print(yr.to_string())
rho, pv = sps.spearmanr(df["年份"], df[AMP])
e1 = df[df["年份"] <= 2021][AMP]; e2 = df[df["年份"] >= 2022][AMP]
u2, p2 = sps.mannwhitneyu(e1, e2)
print(f"年份~涨跌幅 Spearman ρ={rho:.2f} p={pv:.1e}；2019-21 vs 2022-26 中位 {e1.median():.2f}%→{e2.median():.2f}% MW p={p2:.1e}")

print("\n===== 6 时长结构 =====")
bp = df["到峰K数"]
print(f"到峰K数: 中位={bp.median():.0f} 均值={bp.mean():.1f} P90={bp.quantile(.9):.0f} max={bp.max()}; 突破当根即见峰占比 {(bp == 0).mean():.1%}")
for tag in [b[2] for b in bins_pk]:
    sub = df[df["到峰K数档"] == tag]
    if len(sub):
        print(f"到峰{tag:>8}: n={len(sub):>4} 涨跌幅中位={sub[AMP].median():.2f}%")
print(f"Spearman: 涨跌幅~到峰K数={df[AMP].corr(bp, method='spearman'):.2f}  涨跌幅~突破至终结K数={df[AMP].corr(df['突破至终结K数'], method='spearman'):.2f}")

print("\n===== 7 聚类/自相关 =====")
for d in ["做多", "做空"]:
    seq = df[df["方向"] == d].sort_values("突破K索引")[AMP].to_numpy()
    r1, p1 = sps.spearmanr(seq[:-1], seq[1:])
    hi = np.argsort(seq)[:-11:-1]  # top10
    after_hi = np.clip(hi + 1, 0, len(seq) - 1)
    lo10 = np.argsort(seq)[:10]
    after_lo = np.clip(lo10 + 1, 0, len(seq) - 1)
    u3, p3 = sps.mannwhitneyu(seq[after_hi], seq[after_lo])
    print(f"{d}: 滞后1自相关Spearman ρ={r1:.2f}(p={p1:.2f})；top10段之后的段中位={np.median(seq[after_hi]):.2f}% vs bottom10之后={np.median(seq[after_lo]):.2f}% MW p={p3:.2f}")

print("\n===== 8 与盈亏衔接 =====")
print(f"捕获率(盈亏/涨跌幅): 中位={df['捕获率'].median():.1%} P25={df['捕获率'].quantile(.25):.1%} P75={df['捕获率'].quantile(.75):.1%}")
bins_amp = [(0, 0.5), (0.5, 1), (1, 1.5), (1.5, 2), (2, 3), (3, 5), (5, 10), (10, 999)]
print(f"{'涨跌幅区间':>12} {'段数':>5} {'盈亏>0占比':>10} {'盈亏中位':>8}")
for lo, hi in bins_amp:
    sub = df[(df[AMP] >= lo) & (df[AMP] < hi)]
    if len(sub):
        print(f"[{lo:>4},{hi if hi < 999 else '∞':>4}) {len(sub):>5} {(sub[PNL] > 0).mean():>10.1%} {sub[PNL].median():>8.2f}%")
