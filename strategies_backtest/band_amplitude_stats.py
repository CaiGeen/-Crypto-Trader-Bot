"""波段涨跌幅统计报告：分布形态、箱线图、尾部规律、条件规律。

数据：results/波段涨跌幅_极值线vs回调线_v5.csv（统一口径1095段）
输出：results/波段涨跌幅_*.png 五张图 + 终端统计量
"""
import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats as sps

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

HERE = os.path.dirname(os.path.abspath(__file__))
df = pd.read_csv(os.path.join(HERE, "results", "波段涨跌幅_极值线vs回调线_v5.csv"), encoding="utf-8-sig")
AMP = "波段涨跌幅%_回调线基准"
x = df[AMP].to_numpy()
df["峰值R"] = df[AMP] / (df["ATR"] / df["回调突破线"] * 100)

def save(fig, name):
    p = os.path.join(HERE, "results", name)
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    print(f"已保存 {name}")

# ============ 1. 直方图 ============
fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
axes[0].hist(x, bins=np.arange(0, 26, 0.5), color="#4C78A8", edgecolor="white")
axes[0].axvline(np.median(x), color="#E45756", lw=2, label=f"中位 {np.median(x):.2f}%")
axes[0].axvline(x.mean(), color="#F58518", lw=2, ls="--", label=f"均值 {x.mean():.2f}%")
axes[0].set_xlabel("波段涨跌幅 %"); axes[0].set_ylabel("段数")
axes[0].set_title("波段涨跌幅直方图（0.5pp分箱）"); axes[0].legend()
lx = np.log(x)
axes[1].hist(lx, bins=40, color="#54A24B", edgecolor="white")
axes[1].set_xlabel("ln(波段涨跌幅)"); axes[1].set_ylabel("段数")
axes[1].set_title(f"对数变换后近正态（偏度={sps.skew(lx):.2f}）")
save(fig, "波段涨跌幅_直方图.png")

# ============ 2. 生存曲线 + 对数正态拟合 ============
xs = np.sort(x)[::-1]
surv = np.arange(1, len(x) + 1) / len(x)
mu, s = lx.mean(), lx.std()
fit = sps.lognorm.sf(xs, s=s, scale=np.exp(mu))
fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
axes[0].plot(xs, surv, lw=2, label="经验生存曲线 P(X≥x)")
axes[0].plot(xs, fit, lw=2, ls="--", label=f"对数正态拟合(μ={mu:.2f},σ={s:.2f})")
axes[0].axvline(2.68, color="gray", ls=":", lw=1); axes[0].axvline(6, color="gray", ls=":", lw=1)
axes[0].set_xlabel("波段涨跌幅 %"); axes[0].set_ylabel("超越概率")
axes[0].set_title("生存曲线：任意波段≥x的概率"); axes[0].legend()
axes[1].loglog(xs, surv, lw=2, label="经验(log-log)")
axes[1].loglog(xs, fit, lw=2, ls="--", label="对数正态")
axes[1].set_xlabel("波段涨跌幅 % (log)"); axes[1].set_ylabel("P(X≥x) (log)")
axes[1].set_title("log-log：尾部衰减形态"); axes[1].legend()
save(fig, "波段涨跌幅_生存曲线.png")

# ============ 3. 分组箱线图 ============
fig, axes = plt.subplots(2, 2, figsize=(14, 9))
def box(ax, groups, labels, title, ylim=(0, 15)):
    bp = ax.boxplot(groups, tick_labels=labels, showmeans=True, patch_artist=True,
                    medianprops=dict(color="#E45756", lw=2))
    for patch, c in zip(bp["boxes"], plt.cm.tab10(np.linspace(0, 0.9, len(groups)))):
        patch.set_facecolor(c); patch.set_alpha(0.6)
    ax.set_ylim(*ylim); ax.set_ylabel("波段涨跌幅 %"); ax.set_title(title)
order_reason = ["ATR止损", "保本平仓", "KAMA跟踪(跌破KAMA)", "固定止盈"]
box(axes[0][0], [df[df["方向"] == d][AMP] for d in ["做多", "做空"]], ["做多", "做空"],
    "按方向（红线上限15%视图）")
box(axes[0][1], [df[df["出场方式"] == r][AMP] for r in order_reason], ["ATR止损", "保本", "KAMA跟踪", "固定止盈"],
    "按出场方式")
years = sorted(df["年份"].unique())
box(axes[1][0], [df[df["年份"] == y][AMP] for y in years], [str(y) for y in years], "按年度")
bins_atr = [(0, 1.5, "<1.5%"), (1.5, 2, "1.5-2%"), (2, 3, "2-3%"), (3, 5, "3-5%"), (5, 99, "≥5%")]
df["ATR档"] = pd.cut(df["ATR"] / df["回调突破线"] * 100, [b[0] for b in bins_atr] + [99],
                     labels=[b[2] for b in bins_atr])
box(axes[1][1], [g.dropna() for g in [df[df["ATR档"] == b[2]][AMP] for b in bins_atr]],
    [b[2] for b in bins_atr], "按ATR%分档")
save(fig, "波段涨跌幅_箱线图组.png")

# ============ 4. ATR关系 + 峰值R ============
fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
atrp = df["ATR"] / df["回调突破线"] * 100
axes[0].scatter(atrp, df[AMP], s=8, alpha=0.35, color="#4C78A8")
med_x, med_y = [], []
for lo, hi, tag in bins_atr:
    m = df[(atrp >= lo) & (atrp < hi)]
    med_x.append(m["ATR"].to_numpy().size and (lo + hi) / 2 if hi < 99 else 6)
    med_y.append(m[AMP].median())
axes[0].plot(med_x, med_y, "o-", color="#E45756", lw=2, label="各档中位")
axes[0].set_xlabel("ATR%"); axes[0].set_ylabel("波段涨跌幅 %")
axes[0].set_xscale("log"); axes[0].set_yscale("log")
axes[0].set_title("波段涨跌幅 vs ATR%（双对数）"); axes[0].legend()
axes[1].hist(df["峰值R"], bins=np.arange(0, 12, 0.25), color="#72B7B2", edgecolor="white")
axes[1].axvline(df["峰值R"].median(), color="#E45756", lw=2, label=f"中位 {df['峰值R'].median():.2f}R")
axes[1].set_xlabel("峰值R = 波段涨跌幅 / ATR%"); axes[1].set_ylabel("段数")
axes[1].set_title("规模无关量：峰值R分布"); axes[1].legend()
save(fig, "波段涨跌幅_ATR关系.png")

# ============ 5. 年度趋势 + 偏移分组 ============
fig, ax = plt.subplots(figsize=(10, 4.5))
g = df.groupby("年份")[AMP]
med = g.median(); q25 = g.quantile(0.25); q75 = g.quantile(0.75)
ax.fill_between(med.index, q25, q75, alpha=0.25, label="P25-P75")
ax.plot(med.index, med, "o-", lw=2, color="#E45756", label="中位")
g.mean().plot(ax=ax, style="--", lw=2, color="#F58518", label="均值")
ax.set_ylabel("波段涨跌幅 %"); ax.set_title("年度趋势：波段空间收窄")
ax.set_xticks(med.index); ax.legend()
save(fig, "波段涨跌幅_年度趋势.png")

# ============ 统计量输出 ============
print("\n===== 分布形态 =====")
print(f"样本 n={len(x)}  中位={np.median(x):.3f}%  均值={x.mean():.3f}%  标准差={x.std():.3f}")
print(f"偏度={sps.skew(x):.2f}  峰度={sps.kurtosis(x):.2f}  变异系数={x.std()/x.mean():.2f}")
print(f"对数正态拟合: μ={mu:.3f} σ={s:.3f} → 隐含中位={np.exp(mu):.2f}% (实测{np.median(x):.2f}%)")
print(f"ln偏度={sps.skew(lx):.3f} (0=完美对数正态)")
ks_stat, ks_p = sps.kstest(lx, "norm", args=(mu, s))
print(f"对数正态KS检验: D={ks_stat:.4f} p={ks_p:.4f}")
print("\n===== 超越概率 =====")
for t in [1, 2, 4, 6, 8, 10, 15, 22]:
    print(f"P(≥{t:>2}%) = {(x>=t).mean():.3f}  (约{1/max((x>=t).mean(),1/len(x)):.0f}段里1段)")
print("\n===== 峰值R（规模无关量）=====")
print(f"中位={df['峰值R'].median():.2f}R 均值={df['峰值R'].mean():.2f}R P90={df['峰值R'].quantile(0.9):.2f}R")
for lo, hi, tag in bins_atr:
    m = df[(atrp >= lo) & (atrp < hi)]["峰值R"]
    print(f"ATR{tag:>7}: 中位峰值R={m.median():.2f} 均值={m.mean():.2f} 变异系数={m.std()/m.mean():.2f}")
print(f"\nSpearman相关: 涨跌幅~ATR%={df[AMP].corr(atrp, method='spearman'):.2f}  "
      f"涨跌幅~持仓={df[AMP].corr(df['持仓K数'], method='spearman'):.2f}  "
      f"涨跌幅~偏移={df[AMP].corr(df['回调偏移%'], method='spearman'):.2f}")
print("\n===== 组间检验 =====")
u, p = sps.mannwhitneyu(df[df["方向"] == "做多"][AMP], df[df["方向"] == "做空"][AMP])
print(f"多空 Mann-Whitney p={p:.3f} → {'无显著差异' if p>0.05 else '显著'}")
g0 = df[df["回调偏移%"] < 0.001][AMP]; g1 = df[df["回调偏移%"] >= 0.001][AMP]
u, p = sps.mannwhitneyu(g0, g1)
rng = np.random.default_rng(42)
diffs = [np.median(rng.choice(g1, len(g1))) - np.median(rng.choice(g0, len(g0))) for _ in range(2000)]
print(f"偏移=0 vs >0: 中位差={g1.median()-g0.median():.2f}pp  MW p={p:.4f}  "
      f"bootstrap95%CI=[{np.percentile(diffs,2.5):.2f},{np.percentile(diffs,97.5):.2f}]")
print(f"年度中位趋势: " + " ".join(f"{y}:{v:.1f}%" for y, v in med.items()))
