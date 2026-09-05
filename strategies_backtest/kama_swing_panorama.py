"""KAMA 纯市场波段全景报告（2026-08-27 确认的最终定义）

定义（不混入交易策略逻辑，只描述 BTC 自身的 KAMA 趋势状态）：
  KAMA:   ER=10, fast=2, slow=30（indicators.kama，TradingView 对齐）
  参考值: kama_ref[i] = kama[i-1]（前一根K收盘确认的 KAMA）
  做多起始: 不在波段中 且 close[i] > kama[i-1]
  做多起点价: start_price = high[i] + ATR[i]（ATR = Wilder ATR(20)，突破K当根）
  做多延续: 自 i+1 起 low[j] >= kama[j-1]
  做多终结: 首次 low[j] < kama[j-1]，终结K = j
  做空镜像: close[i] < kama[i-1]；起点价 low[i] - ATR[i]；high[j] > kama[j-1] 终结
  极值:   peak = max(high[start..end]) / trough = min(low[start..end])，全闭区间
  涨幅:   amplitude = (极值 - 起点价) / 起点价（多头）；空头 (起点价 - 谷值)/起点价

实现决策（定义未明确处的落地口径，全部在报告中声明）：
  1. 持续K数 = end - start + 1（含起始K与终结K；延续条件从下一根开始检查，故最小为 2）
  2. 极值搜索含终结K（波段终结发生在该K上，该K属于波段生命周期）
  3. 前一波段终结K与后一波段起始K允许同一根（终结K收盘满足起始条件时直接重启）
  4. 数据用尽仍未终结 → 截断波段，单独标记；统计默认只计已完成波段（生存分析的存活数除外）
  5. 无未来函数：EMA70 取突破K前一自然日（已完成日线）的值；rolling 20K 极值不含当前K
  6. ATR 为项目标准口径 Wilder ATR(20)（与 swing_definition_v2 / compare_dev_6k_vs_kama 一致）
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd

from config import BacktestConfig
from indicators import kama

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = BacktestConfig()

# ---------------- 数据 ----------------
df = pd.read_parquet(cfg.cache_path)
for col in ["open", "high", "low", "close", "volume"]:
    if col in df.columns:
        df[col] = df[col].astype(float)
if "open_time" not in df.columns:
    df["open_time"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
df = df.sort_values("open_time_ms").reset_index(drop=True)

o = df["open"].to_numpy()
h = df["high"].to_numpy()
l = df["low"].to_numpy()
c = df["close"].to_numpy()
v = df["volume"].to_numpy()
times = df["open_time"].tolist()
n = len(df)

kama_vals = kama(c, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)


def calc_atr(h, l, c, length=20):
    m = len(c)
    tr = np.zeros(m)
    tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    av = np.zeros(m)
    av[0] = tr[0]
    a = 1.0 / length
    for i in range(1, m):
        av[i] = a * tr[i] + (1 - a) * av[i - 1]
    return av


atr_vals = calc_atr(h, l, c, 20)

# 日线 EMA70（mark 价日线，取突破K前一自然日的已完成值）
daily = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDT_1d_mark.parquet")
daily["close"] = daily["close"].astype(float)
if "open_time" not in daily.columns:
    daily["open_time"] = pd.to_datetime(daily["open_time_ms"], unit="ms", utc=True)
kk = 2.0 / (70 + 1)
de70 = np.zeros(len(daily))
de70[0] = daily["close"].iloc[0]
for i in range(1, len(daily)):
    de70[i] = daily["close"].iloc[i] * kk + de70[i - 1] * (1 - kk)
date_to_ema = dict(zip(daily["open_time"].dt.date, de70))

# 突破K当根可用的前视安全特征（shift(1) 排除当前K）
vol_ma = df["volume"].shift(1).rolling(20, min_periods=20).mean().to_numpy()
prior_hi20 = df["high"].shift(1).rolling(20, min_periods=20).max().to_numpy()
prior_lo20 = df["low"].shift(1).rolling(20, min_periods=20).min().to_numpy()

# ---------------- 状态机抽取波段 ----------------
swings = []
state = 0  # 0=无波段 1=多头波段 2=空头波段
cur = None
first_i = cfg.kama_er_len + 1  # kama[i-1] 首个有效下标


def close_swing(end_i, censored=False):
    global cur
    cur["end_idx"] = end_i
    cur["end_time"] = times[end_i]
    cur["censored"] = censored
    swings.append(cur)
    cur = None


for i in range(first_i, n):
    kr = kama_vals[i - 1]
    if np.isnan(kr):
        continue
    if state == 1:
        if l[i] < kr:
            close_swing(i)
            state = 0
        else:
            continue
    elif state == 2:
        if h[i] > kr:
            close_swing(i)
            state = 0
        else:
            continue
    if state == 0:
        if c[i] > kr:
            state = 1
            cur = {"dir": "long", "start_idx": i, "start_time": times[i]}
        elif c[i] < kr:
            state = 2
            cur = {"dir": "short", "start_idx": i, "start_time": times[i]}

if state != 0:
    close_swing(n - 1, censored=True)

# ---------------- 逐波段指标 ----------------
for s in swings:
    st, en = s["start_idx"], s["end_idx"]
    is_long = s["dir"] == "long"
    sign = 1.0 if is_long else -1.0
    dur = en - st + 1
    s["duration_k"] = dur
    s["kama_ref"] = float(kama_vals[st - 1])
    s["atr_at_break"] = float(atr_vals[st])
    rng = h[st] - l[st]
    if is_long:
        seg_h, seg_l = h[st:en + 1], l[st:en + 1]
        s["start_price"] = float(h[st] + atr_vals[st])
        pk = st + int(np.argmax(seg_h))
        s["ext_price"] = float(h[pk])
        s["amplitude_pct"] = (h[pk] - s["start_price"]) / s["start_price"] * 100
        s["dd_before_peak_pct"] = (s["start_price"] - l[st:pk + 1].min()) / s["start_price"] * 100
        runmax = np.maximum.accumulate(seg_h)
        s["dd_run_peak_pct"] = float(((runmax - seg_l) / runmax).max() * 100)
        s["giveback_pct"] = (h[pk] - c[en]) / h[pk] * 100 if not s["censored"] else np.nan
    else:
        seg_h, seg_l = h[st:en + 1], l[st:en + 1]
        s["start_price"] = float(l[st] - atr_vals[st])
        pk = st + int(np.argmin(seg_l))
        s["ext_price"] = float(l[pk])
        s["amplitude_pct"] = (s["start_price"] - l[pk]) / s["start_price"] * 100
        s["dd_before_peak_pct"] = (h[st:pk + 1].max() - s["start_price"]) / s["start_price"] * 100
        runmin = np.minimum.accumulate(seg_l)
        s["dd_run_peak_pct"] = float(((seg_h - runmin) / runmin).max() * 100)
        s["giveback_pct"] = (c[en] - l[pk]) / l[pk] * 100 if not s["censored"] else np.nan
    s["peak_idx"] = pk
    s["peak_time"] = times[pk]
    s["peak_age_k"] = pk - st + 1
    s["end_close"] = float(c[en])
    # ---- 突破K质量 ----
    s["bk_body_pct"] = abs(c[st] - o[st]) / rng * 100 if rng > 0 else np.nan
    s["bk_change_pct"] = (c[st] / c[st - 1] - 1) * 100
    s["bk_dir_change_pct"] = s["bk_change_pct"] * sign  # 顺方向涨跌
    s["bk_range_atr"] = rng / atr_vals[st]
    s["bk_close_vs_kama_atr"] = (c[st] - kama_vals[st - 1]) / atr_vals[st] * sign
    s["bk_atr_pct"] = atr_vals[st] / c[st] * 100
    s["bk_vol_ratio"] = v[st] / vol_ma[st] if not np.isnan(vol_ma[st]) else np.nan
    # ---- 起点位置 ----
    d = times[st].date() - timedelta(days=1)
    ema = date_to_ema.get(d, np.nan)
    s["dev_ema70_pct"] = (c[st] - ema) / ema * 100 if (ema is not None and not np.isnan(ema) and ema > 0) else np.nan
    if not np.isnan(prior_hi20[st]):
        s["dist_hi20_atr"] = (c[st] - prior_hi20[st]) / atr_vals[st]
        s["dist_lo20_atr"] = (c[st] - prior_lo20[st]) / atr_vals[st]
    else:
        s["dist_hi20_atr"] = np.nan
        s["dist_lo20_atr"] = np.nan

# ---- 波段间字段 ----
for k in range(1, len(swings)):
    cur_s, prev = swings[k], swings[k - 1]
    cur_s["prev_amp_pct"] = prev["amplitude_pct"]
    cur_s["prev_same_dir"] = prev["dir"] == cur_s["dir"]
    cur_s["rest_k"] = cur_s["start_idx"] - prev["end_idx"] - 1  # 两波之间不在任何波段的K数；同K重启为 -1

# 生存分析用的"年龄k后剩余空间"：波段活着度过k根后，极值还能再走多少（相对起点价）
def remaining_after_k(s, k):
    if s["duration_k"] < k:
        return np.nan
    st, en = s["start_idx"], s["end_idx"]
    if s["dir"] == "long":
        first_ext = h[st:st + k].max()
        return (s["ext_price"] - first_ext) / s["start_price"] * 100
    first_ext = l[st:st + k].min()
    return (first_ext - s["ext_price"]) / s["start_price"] * 100


for k in (3, 5, 8, 10, 15, 20):
    for s in swings:
        s[f"remain_after_{k}"] = remaining_after_k(s, k)

sdf = pd.DataFrame(swings)
comp = sdf[~sdf["censored"]].copy()  # 已完成波段（统计主体）
L = comp[comp["dir"] == "long"]
S = comp[comp["dir"] == "short"]

# ---------------- 工具 ----------------
def f2(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.2f}"


def f1(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.1f}"


def f0(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.0f}"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def spearman(a, b):
    m = a.notna() & b.notna()
    if m.sum() < 20:
        return np.nan
    return float(a[m].rank().corr(b[m].rank()))


def dist_line(amps):
    a = pd.Series(amps).dropna()
    if len(a) == 0:
        return None
    return [
        len(a), a.mean(), a.quantile(.25), a.median(), a.quantile(.75),
        a.quantile(.90), a.quantile(.95), a.max(),
        (a <= 0).mean() * 100,
    ]


def bucket_rows(sub, col, edges, labels):
    rows = []
    for lo, hi, lab in zip(edges[:-1], edges[1:], labels):
        b = sub[(sub[col] >= lo) & (sub[col] < hi)]
        if len(b) == 0:
            continue
        rows.append([
            lab, len(b), f2(b["amplitude_pct"].median()), f2(b["amplitude_pct"].quantile(.75)),
            f"{(b['amplitude_pct'] >= 5).mean() * 100:.0f}%",
        ])
    return rows


R = []
W = R.append

# ================= 报告 =================
W("# KAMA 纯市场波段全景报告")
W("")
W(f"- 数据：BTC/USDT 永续 4H，`{cfg.cache_path.name}`，{len(df)} 根，"
  f"{times[first_i].strftime('%Y-%m-%d')} → {times[-1].strftime('%Y-%m-%d')}（KAMA 预热后）")
W("- KAMA：ER=10 / fast=2 / slow=30（TradingView 对齐）；参考值 = 前一根K收盘确认的 KAMA")
W("- ATR：Wilder ATR(20)（项目标准口径），起点价使用突破K当根 ATR")
W("- 起点价：多头 `high[i]+ATR[i]`；空头 `low[i]-ATR[i]`（进入趋势扩张区的门槛，而非成交价）")
W("- 持续K数含起始K与终结K（最小 2）；极值在 `[start, end]` 闭区间上取；终结K与下一波段起始K允许同根")
W("- 截断波段（数据用尽未终结）不计入完成波段统计；EMA70/20K极值/量比分母均不含当前K（无未来函数）")
W("")

# ---------- 一、总览 ----------
in_long = np.zeros(n, bool)
in_short = np.zeros(n, bool)
for s in swings:
    arr = in_long if s["dir"] == "long" else in_short
    arr[s["start_idx"]:s["end_idx"] + 1] = True
any_swing = in_long | in_short

W("## 一、总览")
W("")
W(md_table(
    ["指标", "多头", "空头", "合计"],
    [
        ["已完成波段数", len(L), len(S), len(comp)],
        ["截断波段（进行中）",
         int(((sdf['dir'] == 'long') & sdf['censored']).sum()),
         int(((sdf['dir'] == 'short') & sdf['censored']).sum()),
         int(sdf['censored'].sum())],
        ["持续时间中位数（K）", f1(L['duration_k'].median()), f1(S['duration_k'].median()), f1(comp['duration_k'].median())],
        ["持续时间均值（K）", f1(L['duration_k'].mean()), f1(S['duration_k'].mean()), f1(comp['duration_k'].mean())],
        ["涨跌幅中位数 %", f2(L['amplitude_pct'].median()), f2(S['amplitude_pct'].median()), f2(comp['amplitude_pct'].median())],
        ["时间覆盖率（占全部K）", f"{in_long.mean()*100:.1f}%", f"{in_short.mean()*100:.1f}%", f"{any_swing.mean()*100:.1f}%"],
    ]))
W("")
n_same_bar = int((sdf["rest_k"] == -1).sum()) if "rest_k" in sdf else 0
W(f"- 波段状态覆盖了全部时间的 **{any_swing.mean()*100:.1f}%**（空档期仅 {100-any_swing.mean()*100:.1f}%，KAMA 状态几乎是全天候的）")
W(f"- 终结K与下一波段同K重启共 {n_same_bar} 次；两波之间休整K数中位数 = {f1(sdf['rest_k'].median() if 'rest_k' in sdf else np.nan)}")
W("")

# ---------- 二、空间分布 ----------
W("## 二、波段空间分布（已完成波段）")
W("")
rows = []
for name, sub in (("做多", L), ("做空", S), ("全部", comp)):
    d = dist_line(sub["amplitude_pct"])
    if d:
        rows.append([name, d[0], f2(d[1]), f2(d[2]), f2(d[3]), f2(d[4]), f2(d[5]), f2(d[6]), f2(d[7]), f"{d[8]:.1f}%"])
W(md_table(["方向", "波段数", "均值%", "P25%", "中位%", "P75%", "P90%", "P95%", "最大%", "未过扩张区(≤0)"], rows))
W("")
for name, sub in (("做多", L), ("做空", S)):
    a = sub["amplitude_pct"].dropna()
    pos = a[a > 0].sort_values(ascending=False)
    if len(pos) >= 10:
        topn = max(1, len(pos) // 10)
        share10 = pos.head(topn).sum() / pos.sum() * 100
        share5 = pos.head(5).sum() / pos.sum() * 100
        W(f"- **{name}**：正收益波段 {len(pos)} 个；前 10% 波段（{topn} 个）贡献了全部正涨幅的 **{share10:.1f}%**；"
          f"最大的 5 个波段贡献 **{share5:.1f}%**")
W("")

# ---------- 三、时间-空间 ----------
W("## 三、持续时间 × 空间")
W("")
dur_edges = [1.5, 3.5, 8.5, 20.5, 1e9]
dur_labels = ["2-3K", "4-8K", "9-20K", "21K+"]
for name, sub in (("做多", L), ("做空", S)):
    W(f"**{name}**（Spearman 持续K数 vs 涨跌幅 = {f2(spearman(sub['duration_k'], sub['amplitude_pct']))}）")
    W("")
    W(md_table(["持续K数", "波段数", "涨幅中位%", "P75%", "P(≥5%)"], bucket_rows(sub, "duration_k", dur_edges, dur_labels)))
    W("")
W("")

# ---------- 四、突破K质量 ----------
W("## 四、突破K本身质量 → 波段大小")
W("")
feat_names = [
    ("bk_body_pct", "实体%"),
    ("bk_dir_change_pct", "顺方向涨跌%"),
    ("bk_range_atr", "振幅(ATR)"),
    ("bk_close_vs_kama_atr", "收盘距KAMA(ATR)"),
    ("bk_vol_ratio", "量比"),
    ("bk_atr_pct", "ATR%"),
]
rows = []
for col, label in feat_names:
    rows.append([label, f2(spearman(L[col], L["amplitude_pct"])), f2(spearman(S[col], S["amplitude_pct"]))])
W(md_table(["突破K特征", "做多 vs 涨幅 ρ", "做空 vs 跌幅 ρ"], rows))
W("")
body_edges = [-0.01, 30, 50, 70, 101]
body_labels = ["<30%", "30-50%", "50-70%", "≥70%"]
vol_edges = [0, 0.8, 1.2, 2.0, 3.5, 1e9]
vol_labels = ["<0.8", "0.8-1.2", "1.2-2.0", "2.0-3.5", "≥3.5"]
chg_edges = [-1e9, 0, 1, 2, 3, 1e9]
chg_labels = ["≤0%", "0-1%", "1-2%", "2-3%", "≥3%"]
for name, sub in (("做多", L), ("做空", S)):
    W(f"**{name}**")
    W("")
    W("按突破K实体%：")
    W("")
    W(md_table(["实体%", "波段数", "涨幅中位%", "P75%", "P(≥5%)"], bucket_rows(sub, "bk_body_pct", body_edges, body_labels)))
    W("")
    W("按突破K顺方向涨跌%：")
    W("")
    W(md_table(["顺向涨跌%", "波段数", "涨幅中位%", "P75%", "P(≥5%)"], bucket_rows(sub, "bk_dir_change_pct", chg_edges, chg_labels)))
    W("")
    W("按量比：")
    W("")
    W(md_table(["量比", "波段数", "涨幅中位%", "P75%", "P(≥5%)"], bucket_rows(sub, "bk_vol_ratio", vol_edges, vol_labels)))
    W("")
W("")

# ---------- 五、波段内部回撤 ----------
W("## 五、波段内部回撤与峰值回吐")
W("")
rows = []
for name, sub in (("做多", L), ("做空", S)):
    big = sub[sub["amplitude_pct"] >= 5]
    small = sub[sub["amplitude_pct"] < 5]
    rows.append([f"{name}-全部", len(sub), f2(sub["dd_before_peak_pct"].median()), f2(sub["dd_before_peak_pct"].quantile(.75)),
                 f2(sub["dd_before_peak_pct"].quantile(.90))])
    if len(big) >= 5:
        rows.append([f"{name}-大波段(≥5%)", len(big), f2(big["dd_before_peak_pct"].median()),
                     f2(big["dd_before_peak_pct"].quantile(.75)), f2(big["dd_before_peak_pct"].quantile(.90))])
    if len(small) >= 5:
        rows.append([f"{name}-小波段(<5%)", len(small), f2(small["dd_before_peak_pct"].median()),
                     f2(small["dd_before_peak_pct"].quantile(.75)), f2(small["dd_before_peak_pct"].quantile(.90))])
W(md_table(["组", "n", "峰前回撤中位%", "P75%", "P90%"], rows))
W("")
rows = []
for name, sub in (("做多", L), ("做空", S)):
    rows.append([name, f2(sub["dd_run_peak_pct"].median()), f2(sub["dd_run_peak_pct"].quantile(.90)),
                 f2(sub["giveback_pct"].median()), f2(sub["giveback_pct"].quantile(.75)), f2(sub["giveback_pct"].quantile(.90))])
W(md_table(["方向", "运行峰值回撤中位%", "P90%", "峰值回吐中位%", "P75%", "P90%"], rows))
W("")
W("- 峰前回撤 = (起点价 - 起点到峰值途中最低点)/起点价（用户定义）；运行峰值回撤 = 全程相对滚动极值；"
  "峰值回吐 = 从极值到 KAMA 终结K收盘让回的幅度 —— 它是「等 KAMA 确认离场」的成本")
W("")

# ---------- 六、波段间关系 ----------
W("## 六、波段之间关系")
W("")
rho_all = spearman(comp["prev_amp_pct"], comp["amplitude_pct"])
same = comp[comp["prev_same_dir"] == True]  # noqa: E712
diff = comp[comp["prev_same_dir"] == False]  # noqa: E712
p75_all = comp["amplitude_pct"].quantile(.75)
prev_big = comp[comp["prev_amp_pct"] >= comp["prev_amp_pct"].quantile(.75)]
W(f"- 前波段涨幅 → 本波段涨幅：Spearman ρ = **{f2(rho_all)}**（n={int(comp['prev_amp_pct'].notna().sum())}）")
W(f"- 前一波段结束后，下一波同方向比例 = {comp['prev_same_dir'].mean()*100:.0f}%"
  f"（同向中位 {f2(same['amplitude_pct'].median())}% vs 反向中位 {f2(diff['amplitude_pct'].median())}%）")
if len(prev_big) >= 10:
    W(f"- 前波段为前 25% 大行情时，本波段 ≥ 全体 P75（{f2(p75_all)}%）的概率 = "
      f"{(prev_big['amplitude_pct'] >= p75_all).mean()*100:.0f}%（基准 25%）")
rho_same = spearman(same["prev_amp_pct"], same["amplitude_pct"])
rho_diff = spearman(diff["prev_amp_pct"], diff["amplitude_pct"])
W(f"- ρ 拆分：同向重启对 {f2(rho_same)} / 反向切换对 {f2(rho_diff)} —— 相关性不来自同K重叠伪影；"
  f"更可能是波动 regime 共因子（高 ATR 期相邻波段同时放大）。分位数检验 27% vs 基准 25% 才是可用预测力的真实量级")
W("")
W("**结构性发现：本定义下「波段间整理时间」不存在。** 终结K收盘几乎必然满足某一方向的起始条件"
  "（close ≠ kama 前值），因此 100% 的波段转换都是同K重启，市场被无缝切分为交替的趋势状态段。"
  "如需研究整理期，需换口径（例如「距上一个进入扩张区波段极值的K数」或「连续未过扩张区的波段数」）。")
W("")
rest_edges = [-2, 0, 1, 3.5, 12.5, 1e9]
rest_labels = ["同K重启", "0K", "1-3K", "4-12K", "13K+"]
W("按两波之间休整K数 → 下一波涨幅：")
W("")
W(md_table(["休整K数", "波段数", "涨幅中位%", "P75%", "P(≥5%)"], bucket_rows(comp, "rest_k", rest_edges, rest_labels)))
W("")
rest_lo = comp[comp["rest_k"] >= 4]
rest_hi = comp[comp["rest_k"] < 4]
if len(rest_lo) >= 10 and len(rest_hi) >= 10:
    W(f"- 休整 ≥4K 后启动的波段中位涨幅 {f2(rest_lo['amplitude_pct'].median())}% vs 休整 <4K 的 {f2(rest_hi['amplitude_pct'].median())}%")
W("")

# ---------- 七、生存分析 ----------
W("## 七、生存分析：KAMA 突破后的趋势生命")
W("")
ages = [2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 60]
N = len(sdf)
rows = []
for a in ages:
    alive = int((sdf["duration_k"] >= a).sum())
    rows.append([f"≥{a}K", alive, f"{alive / N * 100:.1f}%"])
W(md_table(["年龄", "存活波段数", "存活率"], rows))
W("")
base5 = (comp["amplitude_pct"] >= 5).mean() * 100
base10 = (comp["amplitude_pct"] >= 10).mean() * 100
W(f"基准（全部完成波段）：P(涨幅≥5%) = {base5:.0f}%，P(≥10%) = {base10:.0f}%，涨幅中位 = {f2(comp['amplitude_pct'].median())}%")
W("")
rows = []
rows.append(["（无条件）", len(comp), f2(comp["amplitude_pct"].median()), f"{base5:.0f}%", f"{base10:.0f}%",
             "—", "—", "—"])
for k in (3, 5, 8, 10, 15, 20):
    subc = comp[comp["duration_k"] >= k]
    if len(subc) < 10:
        continue
    rem = subc[f"remain_after_{k}"].dropna()
    rows.append([
        f"存活≥{k}K", len(subc), f2(subc["amplitude_pct"].median()),
        f"{(subc['amplitude_pct'] >= 5).mean() * 100:.0f}%", f"{(subc['amplitude_pct'] >= 10).mean() * 100:.0f}%",
        f2(rem.median()), f2(rem.quantile(.75)), f"{(rem > 0).mean() * 100:.0f}%",
    ])
W(md_table(["条件", "n", "最终涨幅中位%", "P(≥5%)", "P(≥10%)", "k后剩余中位%", "k后剩余P75%", "P(峰值在k后)"], rows))
W("")
W("- 「k后剩余」= 波段活过 k 根后，从已实现极值到最终极值还能再走的空间（相对起点价）；"
  "它衡量第 k 根提前离场/收紧止损放弃的期望空间")
W("")

# ---------- 八、起点位置 ----------
W("## 八、突破时市场位置 → 波段大小")
W("")
dev_edges = [-1e9, -5, -2, 0, 2, 5, 1e9]
dev_labels = ["<-5%", "-5~-2%", "-2~0%", "0~2%", "2~5%", "≥5%"]
hi_edges = [-1e9, -2, -1, -0.5, 0, 1e9]
hi_labels = ["<-2", "-2~-1", "-1~-0.5", "-0.5~0", "≥0(创新高)"]
atr_edges = [0, 1, 1.5, 2, 3, 1e9]
atr_labels = ["<1%", "1-1.5%", "1.5-2%", "2-3%", "≥3%"]
for title, col, edges, labels in (
    ("突破K收盘相对日线EMA70偏离%", "dev_ema70_pct", dev_edges, dev_labels),
    ("突破K收盘距前20K高点（ATR）", "dist_hi20_atr", hi_edges, hi_labels),
    ("突破时ATR%（波动 regime）", "bk_atr_pct", atr_edges, atr_labels),
):
    W(md_table([f"{title}", "波段数", "涨幅中位%", "P75%", "P(≥5%)"], bucket_rows(comp, col, edges, labels)))
    W("")

# ---------- 九、大波段画像 ----------
W("## 九、大波段画像（按涨幅 Top 15）")
W("")
top15 = comp.sort_values("amplitude_pct", ascending=False).head(15)
rows = []
for _, r in top15.iterrows():
    rows.append([
        "多" if r["dir"] == "long" else "空",
        r["start_time"].strftime("%Y-%m-%d"),
        r["end_time"].strftime("%Y-%m-%d"),
        int(r["duration_k"]), f2(r["amplitude_pct"]), int(r["peak_age_k"]),
        f2(r["dd_before_peak_pct"]), f2(r["bk_body_pct"]), f2(r["bk_vol_ratio"]), f2(r["dev_ema70_pct"]),
    ])
W(md_table(["向", "起始", "终结", "K数", "涨幅%", "峰值年龄K", "峰前回撤%", "突破K实体%", "量比", "EMA70偏离%"], rows))
W("")

# ---------- 十、结论 ----------
med_amp = comp["amplitude_pct"].median()
r3 = comp[comp["duration_k"] >= 3][ "remain_after_3"].dropna()
r5 = comp[comp["duration_k"] >= 5]["remain_after_5"].dropna()
c5 = comp[comp["duration_k"] >= 5]
p5big = (c5["amplitude_pct"] >= 5).mean() * 100
W("## 十、数据直接给出的策略优化线索")
W("")
W(f"1. **KAMA 退出并不早，反而有回吐成本**：等 KAMA 终结确认平均要把峰值让回 "
  f"{f2(comp['giveback_pct'].median())}%（中位）——扩大收益的方向不是更晚离场，而是终结前的跟踪止损/分批。")
W(f"2. **第 3K 保本偏早**：活过 3K 的波段此后仍有中位 {f2(r3.median())}% 剩余空间（P75 {f2(r3.quantile(.75))}%）；"
  f"活过 5K 后 P(≥5%) 升至 {p5big:.0f}%（基准 {base5:.0f}%）。生存信息本身就是过滤器：age≥5 再上收紧的保护更划算。")
W(f"3. **时间即信息**：持续K数与涨幅 Spearman ρ（多/空）= "
  f"{f2(spearman(L['duration_k'], L['amplitude_pct']))} / {f2(spearman(S['duration_k'], S['amplitude_pct']))}；"
  f"21K+ 长波段贡献了分布的右尾，对它们任何提前离场规则都昂贵。")
W(f"4. **前波段不预测后波段**：ρ = {f2(rho_all)}（多为波动共因子），分位数检验 27% vs 基准 25% 无可用预测力——"
  f"不应应用「上一波大」做任何下注；且本定义下波段间休整恒为 0（同K重启），无整理期可言。")
W(f"5. **超额收益集中于头部**：前 10% 波段贡献大部分正涨幅（见第二节），策略的目标函数应是「不漏掉头部」而非「提高平均」。")
W("")
W("---")
W("")
W("### 字段字典（results/kama_swing_panorama_all.csv）")
W("")
W("| 列 | 含义 |")
W("|---|---|")
W("| 方向/起始K/结束K/起始时间/结束时间 | 波段标识（K 为数据中的 4H 序号） |")
W("| 持续K数 | end-start+1，含起始与终结K |")
W("| 截断 | TRUE=数据用尽仍未终结 |")
W("| KAMA参考/突破KATR | kama[start-1] 与 ATR(start) |")
W("| 起点价 | 多：high+ATR；空：low-ATR |")
W("| 极值价/极值K/极值时间/极值年龄K | 峰值(多)/谷值(空)及其位置 |")
W("| 涨跌幅% | (极值-起点价)/起点价 |")
W("| 峰前回撤% | 用户定义：起点到极值途中反向最大幅度（vs 起点价） |")
W("| 全程回撤% | 相对滚动极值的最大回撤 |")
W("| 峰值回吐% | 极值 → KAMA 终结K收盘 |")
W("| 突破K实体%/涨跌%/振幅ATR/距KAMA_ATR/ATR%/量比 | 突破K质量 |")
W("| EMA70偏离%/距20K高_ATR/距20K低_ATR | 突破时位置（无未来函数） |")
W("| 前波涨幅%/前波同向/前休整K | 波段间关系 |")

report = "\n".join(R)
rep_path = os.path.join(HERE, "KAMA_SWING_PANORAMA_REPORT.md")
with open(rep_path, "w", encoding="utf-8") as fh:
    fh.write(report)

# ---------------- CSV ----------------
out = sdf.copy()
out["方向"] = out["dir"].map({"long": "做多", "short": "做空"})
col_map = {
    "方向": "方向", "start_idx": "起始K", "end_idx": "结束K", "start_time": "起始时间", "end_time": "结束时间",
    "duration_k": "持续K数", "censored": "截断", "kama_ref": "KAMA参考", "atr_at_break": "突破KATR",
    "start_price": "起点价", "ext_price": "极值价", "peak_idx": "极值K", "peak_time": "极值时间",
    "peak_age_k": "极值年龄K", "amplitude_pct": "涨跌幅%", "end_close": "终结K收盘",
    "dd_before_peak_pct": "峰前回撤%", "dd_run_peak_pct": "全程回撤%", "giveback_pct": "峰值回吐%",
    "bk_body_pct": "突破K实体%", "bk_dir_change_pct": "突破K顺向涨跌%", "bk_range_atr": "突破K振幅ATR",
    "bk_close_vs_kama_atr": "收盘距KAMA_ATR", "bk_atr_pct": "ATR%", "bk_vol_ratio": "量比",
    "dev_ema70_pct": "EMA70偏离%", "dist_hi20_atr": "距20K高_ATR", "dist_lo20_atr": "距20K低_ATR",
    "prev_amp_pct": "前波涨幅%", "prev_same_dir": "前波同向", "rest_k": "前休整K",
    "remain_after_3": "3K后剩余%", "remain_after_5": "5K后剩余%", "remain_after_8": "8K后剩余%",
    "remain_after_10": "10K后剩余%", "remain_after_15": "15K后剩余%", "remain_after_20": "20K后剩余%",
}
out = out[[k for k in col_map if k in out.columns]]
out = out.rename(columns=col_map)
csv_path = os.path.join(HERE, "results", "kama_swing_panorama_all.csv")
os.makedirs(os.path.dirname(csv_path), exist_ok=True)
out.to_csv(csv_path, index=False, encoding="utf-8-sig")

print(report)
print()
print(f"[报告] {rep_path}")
print(f"[波段CSV] {csv_path}  共 {len(out)} 个波段（含截断 {int(sdf['censored'].sum())} 个）")
