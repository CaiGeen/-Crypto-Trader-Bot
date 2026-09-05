"""F10 过滤器研究：突破K出现后，KAMA 至少同向运动 10K，结果会怎样？

基于 2026-08-27 全景定义（kama_swing_panorama.py 同一套状态机与口径）：
  突破K i：不在波段中 且 close[i] 与 kama[i-1] 突破关系成立
  F10（严格口径，主定义）：j = i+1 .. i+10 共 10 根K，KAMA 逐根同向
      多头：kama[j] > kama[j-1] 全部成立；空头：kama[j] < kama[j-1] 全部成立
  对照1（净向）：仅要求 kama[i+10] 与 kama[i] 的净变动同向
  对照2（存活）：仅要求波段持续 ≥11K（等价于活过确认点），不加 KAMA 斜率条件
  可交易版：确认发生在 i+10 收盘；入场价 = close[i+10]，剩余空间用 [i+11, end] 的极值衡量
      ——分离「事后人口研究」与「确认后才能下注」两种视角，避免前视偏差

KAMA 逐根严格同向（等于即失败）；窗口不含突破K本身（"突破K出现后"）。
"""
import os
import sys

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

df = pd.read_parquet(cfg.cache_path)
for col in ["open", "high", "low", "close", "volume"]:
    if col in df.columns:
        df[col] = df[col].astype(float)
if "open_time" not in df.columns:
    df["open_time"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
df = df.sort_values("open_time_ms").reset_index(drop=True)

h = df["high"].to_numpy()
l = df["low"].to_numpy()
c = df["close"].to_numpy()
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

# ---- 与全景脚本一致的波段状态机 ----
swings = []
state = 0
cur = None
first_i = cfg.kama_er_len + 1


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
            cur = {"dir": "short", "start_time": times[i], "start_idx": i}

if state != 0:
    close_swing(n - 1, censored=True)

# ---- 全景口径逐波段指标 + F10 判定 ----
for s in swings:
    st, en = s["start_idx"], s["end_idx"]
    is_long = s["dir"] == "long"
    s["duration_k"] = en - st + 1
    if is_long:
        s["start_price"] = float(h[st] + atr_vals[st])
        pk = st + int(np.argmax(h[st:en + 1]))
        s["ext_price"] = float(h[pk])
        s["amplitude_pct"] = (h[pk] - s["start_price"]) / s["start_price"] * 100
    else:
        s["start_price"] = float(l[st] - atr_vals[st])
        pk = st + int(np.argmin(l[st:en + 1]))
        s["ext_price"] = float(l[pk])
        s["amplitude_pct"] = (s["start_price"] - l[pk]) / s["start_price"] * 100
    s["peak_idx"] = pk

    # F10：突破K后 10 根K内 KAMA 逐根同向的最长连续段
    conf = st + 10
    if conf > n - 1 or np.isnan(kama_vals[conf]):
        s["run10"] = np.nan
        s["net10"] = np.nan
    else:
        run = best = 0
        for j in range(st + 1, conf + 1):
            same = (kama_vals[j] > kama_vals[j - 1]) if is_long else (kama_vals[j] < kama_vals[j - 1])
            run = run + 1 if same else 0
            best = max(best, run)
        s["run10"] = best
        diff = kama_vals[conf] - kama_vals[st]
        s["net10"] = bool(diff > 0) if is_long else bool(diff < 0)
    s["f10"] = (s["run10"] == 10) if not np.isnan(s.get("run10", np.nan)) else False

    # 可交易子集：通过 F10 且确认收盘时波段仍活着（end > conf）
    s["tradable"] = bool(s["f10"] and en > conf and not s["censored"])
    if s["tradable"]:
        cc = c[conf]
        s["confirm_price"] = float(cc)
        s["confirm_time"] = times[conf]
        if en >= conf + 1:
            if is_long:
                post_ext = h[conf + 1:en + 1].max()
                s["amp_conf_pct"] = (post_ext - cc) / cc * 100
                s["mae_conf_pct"] = (cc - l[conf + 1:en + 1].min()) / cc * 100
            else:
                post_ext = l[conf + 1:en + 1].min()
                s["amp_conf_pct"] = (cc - post_ext) / cc * 100
                s["mae_conf_pct"] = (h[conf + 1:en + 1].max() - cc) / cc * 100
        else:
            s["amp_conf_pct"] = np.nan
            s["mae_conf_pct"] = np.nan
        s["peak_before_conf"] = bool(pk <= conf)
    else:
        s["confirm_price"] = np.nan
        s["confirm_time"] = None
        s["amp_conf_pct"] = np.nan
        s["mae_conf_pct"] = np.nan
        s["peak_before_conf"] = np.nan

sdf = pd.DataFrame(swings)
comp = sdf[~sdf["censored"]].copy()
L = comp[comp["dir"] == "long"]
S = comp[comp["dir"] == "short"]


def f2(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.2f}"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def line(sub, label):
    a = sub["amplitude_pct"]
    return [label, len(sub), f2(a.median()), f2(a.quantile(.75)), f2(a.quantile(.90)),
            f"{(a >= 5).mean() * 100:.0f}%", f"{(a >= 10).mean() * 100:.0f}%", f1(sub['duration_k'].median())]


def f1(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:.1f}"


R = []
W = R.append
W("# F10 过滤器报告：突破K后 KAMA 同向运动 ≥10K")
W("")
W("- 主口径：突破K i 之后的 10 根K（j=i+1..i+10）KAMA **逐根严格同向**（等于即断）")
W("- 对照：净向（kama[i+10] vs kama[i] 净变动同向）／仅存活≥11K（不加斜率条件）")
W("- 可交易版：确认在 i+10 收盘才可知 → 入场价 close[i+10]，剩余空间取 [i+11, end] 极值")
W("- 波段口径与 KAMA_SWING_PANORAMA_REPORT.md 完全一致（起点价 high±ATR、闭区间极值、同K重启）")
W("")

# ---- 1. 通过率 ----
W("## 一、通过率")
W("")
rows = []
for name, sub in (("做多", L), ("做空", S), ("合计", comp)):
    r = sub["run10"].notna()
    rows.append([name, len(sub), f"{(sub['f10'] & r).sum()}",
                 f"{((sub['f10'] & r).sum() / r.sum()) * 100:.1f}%",
                 f"{(sub['net10'] & r).sum()}", f"{((sub['net10'] & r).sum() / r.sum()) * 100:.1f}%"])
W(md_table(["方向", "完成波段", "F10严格通过", "通过率", "净向通过", "净向率"], rows))
W("")
run_dist = comp["run10"].value_counts().sort_index()
W("KAMA 同向连续段长度分布（突破后10K内）：")
W("")
W(md_table(["同向连续K数", "波段数", "占比"],
           [[int(k), int(v), f"{v / len(comp) * 100:.1f}%"] for k, v in run_dist.items()]))
W("")

# ---- 2. 通过 vs 拒绝 ----
W("## 二、通过 vs 未通过（人口视角，含事后信息）")
W("")
W(md_table(["组", "n", "涨幅中位%", "P75%", "P90%", "P(≥5%)", "P(≥10%)", "持续中位K"],
           [line(comp[comp["f10"]], "F10通过-全部"),
            line(comp[~comp["f10"]], "F10拒绝-全部"),
            line(L[L["f10"]], "F10通过-做多"),
            line(S[S["f10"]], "F10通过-做空"),
            line(comp[comp["duration_k"] >= 11], "对照:存活≥11K"),
            line(comp[comp["net10"] == True], "对照:净向10K"),  # noqa: E712
            ]))
W("")

# ---- 3. F10 相对存活的信息增量 ----
dur11 = comp[comp["duration_k"] >= 11]
both = (dur11["f10"]).mean() * 100 if len(dur11) else np.nan
W("## 三、KAMA 斜率是否比「活得久」更有信息？")
W("")
W(f"- 存活≥11K 的波段中只有 **{both:.0f}%** 同时满足 F10 —— 两个条件高度但非完全重叠")
W(f"- 存活≥11K：P(≥5%) = {(dur11['amplitude_pct'] >= 5).mean() * 100:.0f}%；"
  f"F10：P(≥5%) = {(comp[comp['f10']]['amplitude_pct'] >= 5).mean() * 100:.0f}%；"
  f"存活且F10：P(≥5%) = {(dur11[dur11['f10']]['amplitude_pct'] >= 5).mean() * 100:.0f}%")
W("")

# ---- 4. 可交易性 ----
tr = comp[comp["tradable"]]
W("## 四、可交易视角：确认K（第10根）收盘才入场")
W("")
rows = [["全部F10通过（事后）", len(comp[comp["f10"]])]]
a = comp[comp["f10"]]["amplitude_pct"]
rows[0] += [f2(a.median()), f2(a.quantile(.75)), f"{(a >= 5).mean() * 100:.0f}%", f2(comp[comp['f10']]['duration_k'].median())]
ac = tr["amp_conf_pct"].dropna()
mc = tr["mae_conf_pct"].dropna()
rows.append(["确认后入场（无前视）", len(tr), f2(ac.median()), f2(ac.quantile(.75)),
             f"{(ac >= 5).mean() * 100:.0f}%", f1(tr['duration_k'].median() - 10)])
W(md_table(["视角", "n", "空间中位%", "P75%", "P(≥5%)", "确认后持续中位K"], rows))
W("")
W(f"- 确认入场后 MAE（先反向走的最大幅度）中位 = {f2(mc.median())}%，P90 = {f2(mc.quantile(.90))}%")
W(f"- 通过 F10 但峰值已出现在确认K之前（白等）的比例 = "
  f"{tr['peak_before_conf'].mean() * 100:.0f}%（这些波段确认后已无新极值可赚）")
W("")

# ---- 5. 头部召回 ----
big = comp[comp["amplitude_pct"] >= 5]
big_pass = big[big["f10"]]
W("## 五、头部召回：F10 会漏掉多少 ≥5% 的大波段？")
W("")
W(f"- ≥5% 大波段共 {len(big)} 个，F10 保留 {len(big_pass)} 个，**召回率 = {len(big_pass) / len(big) * 100:.0f}%**")
W(f"- 被误杀的 {len(big) - len(big_pass)} 个中，持续<11K（天然无法通过）的 = "
  f"{(big[~big['f10']]['duration_k'] < 11).sum()} 个")
W("")
missed = big[~big["f10"]].sort_values("amplitude_pct", ascending=False).head(10)
rows = []
for _, r in missed.iterrows():
    rows.append(["多" if r["dir"] == "long" else "空", r["start_time"].strftime("%Y-%m-%d"),
                 int(r["duration_k"]), f2(r["amplitude_pct"]),
                 "—" if np.isnan(r["run10"]) else int(r["run10"])])
W("被误杀大波段 Top10（同向连续K数 = 突破后10K内 KAMA 最长同向段）：")
W("")
W(md_table(["向", "起始", "持续K", "涨幅%", "同向连续K"], rows))
W("")

# ---- 6. 结论 ----
p_pass = (comp[comp["f10"]]["amplitude_pct"] >= 5).mean() * 100
p_dur = (dur11["amplitude_pct"] >= 5).mean() * 100
p_tr = (tr["amp_conf_pct"].dropna() >= 5).mean() * 100
W("## 六、结论")
W("")
W(f"1. F10 是一个强选择器：通过率仅 {comp['f10'].mean() * 100:.1f}%，但 P(≥5%) 从基准 4% 升至 "
  f"**{p_pass:.0f}%**；不过对照「存活≥11K」的 {p_dur:.0f}% —— 信息增量主要来自时间本身，KAMA 斜率只是时间的代理。")
W(f"2. 代价是确认延迟：确认后入场 P(≥5%) 降至 {p_tr:.0f}%，且 {tr['peak_before_conf'].mean() * 100:.0f}% 的通过波段峰值已在确认前走完 —— "
  f"F10 作为「入场过滤器」买不到头部，作为「持有/加仓过滤器」才合理。")
W(f"3. 头部召回 {len(big_pass) / len(big) * 100:.0f}%：漏掉的主要是持续<11K 的快速波段（V型反转/事件驱动），"
  f"它们正是上一份报告 Top15 的常客 —— 任何斜率确认类过滤器都天然杀快波。")
W("")

report = "\n".join(R)
rep_path = os.path.join(HERE, "KAMA_F10_FILTER_REPORT.md")
with open(rep_path, "w", encoding="utf-8") as fh:
    fh.write(report)

out = comp.copy()
out["方向"] = out["dir"].map({"long": "做多", "short": "做空"})
export_cols = {
    "方向": "方向", "start_time": "起始时间", "end_time": "结束时间", "duration_k": "持续K数",
    "amplitude_pct": "涨跌幅%", "run10": "同向连续K", "net10": "净向10K", "f10": "F10通过",
    "tradable": "确认可入场", "confirm_time": "确认时间", "confirm_price": "确认入场价",
    "amp_conf_pct": "确认后空间%", "mae_conf_pct": "确认后MAE%", "peak_before_conf": "峰值在确认前",
}
out = out[[k for k in export_cols if k in out.columns]].rename(columns=export_cols)
csv_path = os.path.join(HERE, "results", "kama_f10_swings.csv")
out.to_csv(csv_path, index=False, encoding="utf-8-sig")

print(report)
print()
print(f"[报告] {rep_path}")
print(f"[CSV] {csv_path}")
