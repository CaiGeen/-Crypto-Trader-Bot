"""单段完整指标导出 v2：全核心指标（2026-08-23，40→50列）。

价格列（显式命名）：
  - 入场价格(6K突破线) = 被突破的区间极值，本数据集盈亏的入场基准。
  - 出场价格(终结KAMA线) = 被收盘跌破/涨破的那条 KAMA[ex-1]（前K确认线，假定按线价出场）。
  - 另有 突破K开盘/收盘、终结K开盘/收盘 作为盘面参考。

新增列口径（全部无前视，除标注 t=3 外均为信号K收盘即知）：
  - MAE%(最大逆向偏移) = 持仓期间(bar i..ex 盘中)对入场价的最大不利偏移，恒≤0；
    MAE R = MAE%/ATR%。止损宽度研究的镜像指标（对应 MFE=涨跌幅%）。
  - 突破结构级别(根) = 收盘价突破的更大级别窗口：取 {6,10,20,40,60,120} 中被突破的最大窗口；
    120 表示 ≥120根新高/新低（级别越大，上方/下方套牢越少）。
  - 前高压力%(60根) = 入场线(6K突破线)到近60根内更高极值的距离%：
    0 = 区间顶就是60根之最（上方无近端套牢）；正值 = 上方还有更早高点压着。
    是否已收盘破更大级别新高由 突破结构级别(根) 表达（两者互补）。
  - 段内再突破次数 = 本段存续期间(i, ex] 内同方向再次收盘突破的次数（原被合并的嵌套信号数），
    度量趋势的持续推动，t=过程信息。
  - 终结K幅度%(负=逆向) = 终结K自身 (收-开)/开 按方向符号化；负值即逆向长实体。
  - 大波段组ID/组内角色/组类型/大波段终点价格/大波段延伸涨幅%：
    从 波段数据集 与 大波段组 两份CSV按 突破K索引 关联合并（峰到峰分组归属）。
  - 资金费率（8h结算，/fapi/v1/fundingRate 全历史，fetch_funding_oi.py 拉取）：
    入场时资金费率%(已结算) = 信号K收盘时刻最近一次已结算费率（t=0 已知）；
    持仓资金费合计%(多头口径) = 持仓期间 (T入场, T出场] 内结算费率之和，正=多头支付（空头相反收付）。
  - 持仓量OI（data.binance.vision daily metrics，sum_open_interest，5分钟粒度，2020-09-01 起）：
    入场时持仓量OI(BTC) = 信号时刻最近一条快照（t=0 已知）；入场时OI较7日前%（t=0 已知）；
    持仓期间OI变化% = 入场快照→出场前快照（t=过程信息）。2020-09 前无数据为空。
其余口径同 v1：ATR=RMA(TR,20)取atr[i-1]；R值=(出场价-入场价)/ATR；
日线EMA70偏离度=信号K收盘 vs 昨日已收日线EMA70(α=2/71)，前69日预热期NaN；
t=3列=第3K收盘才可知。时间均为UTC+8，按突破K时间降序。
"""
import bisect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd

from config import BacktestConfig
from indicators import kama as kama_fn
from signal_detector import detect_breakout

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = BacktestConfig()
AMP, PNL = "涨跌幅%(突破线→波峰)", "盈亏%(突破线→KAMA线)"
df = pd.read_csv(os.path.join(HERE, "results", "单段涨跌幅_6K突破线到KAMA线.csv"))

# —— 4H 原始数据：ATR / KAMA / OHLCV / 信号（缓存规范名迁移后自动兼容旧名）——
raw = pd.read_parquet(cfg.cache_path)
ms4 = raw["open_time_ms"].to_numpy(dtype=np.int64)
h, l, c, o, v = (raw[k].to_numpy(dtype=float) for k in ("high", "low", "close", "open", "volume"))
t4 = pd.to_datetime(raw["open_time_ms"], unit="ms", utc=True)
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
kama_vals = kama_fn(c, 10, 2, 30)
sig_idx = {"long": [], "short": []}
for s in detect_breakout(raw, cfg.window):
    sig_idx[s.direction].append(s.idx)

# —— 日线 EMA70（只取已收盘日线）——
d1 = pd.read_parquet(cfg.resolve_cache_path("1d"))
dc = d1["close"].to_numpy(dtype=float)
d_date = pd.to_datetime(d1["open_time_ms"], unit="ms", utc=True).dt.normalize()
k_ema = 2.0 / 71
ema = np.zeros(len(dc))
ema[0] = dc[0]
for j in range(1, len(dc)):
    ema[j] = dc[j] * k_ema + ema[j - 1] * (1 - k_ema)
warmup = 69

LEVELS = [6, 10, 20, 40, 60, 120]

# —— 资金费率 / 持仓量OI 查表（均已升序，ms 时间戳）——
fnd = pd.read_csv(os.path.join(HERE, "cache", "funding_BTCUSDT.csv"))
f_ts = fnd["ts"].to_numpy(dtype=np.int64)
f_rate = fnd["rate"].to_numpy(dtype=float)
oi_hist = pd.read_parquet(os.path.join(HERE, "cache", "metrics_oi_BTCUSDT.parquet"))
o_ts = oi_hist["ts"].to_numpy(dtype=np.int64)
o_val = oi_hist["oi"].to_numpy(dtype=float)
BAR_MS = 14_400_000

rows = []
for _, r in df.iterrows():
    i = int(r["突破K索引"])
    is_long = r["方向"] == "做多"
    d = 1.0 if is_long else -1.0
    entry = float(r["突破线(起点)"])
    atrv = float(atr[i - 1])
    atrp = atrv / entry * 100
    und = bool(r["是否未终结"])
    ex = int(r["KAMA终结K索引"]) if not und else None
    line = float(r["终结时KAMA线(出场)"]) if not und else np.nan
    peak_i = int(r["波峰K索引"])
    end_i = ex if ex is not None else m - 1

    # 日线EMA70偏离度（昨日已收盘日线）
    sig_close_time = t4.iloc[i] + pd.Timedelta(hours=4)
    d_prev = (sig_close_time - pd.Timedelta(days=1)).normalize()
    hit = d_date[d_date == d_prev].index
    if len(hit) and hit[0] >= warmup:
        e = float(ema[hit[0]])
        ema_dev = (c[i] - e) / e * 100
    else:
        e, ema_dev = np.nan, np.nan

    # t=3 路径（未终结段须已有≥3根后续K才算"存活至第3K"）
    alive3 = (ex is not None and ex > i + 3) or (ex is None and i + 3 < m)
    if alive3:
        seg_h, seg_l = h[i:i + 4], l[i:i + 4]
        mfe3 = ((seg_h.max() - entry) if is_long else (entry - seg_l.min())) / entry * 100
    else:
        mfe3 = np.nan

    # MAE：持仓期间最大逆向偏移（盘中，含终结K），有符号恒≤0，全程未逆动记0
    if is_long:
        mae = min(0.0, (l[i:end_i + 1].min() - entry) / entry * 100)
    else:
        mae = min(0.0, (entry - h[i:end_i + 1].max()) / entry * 100)

    # 突破结构级别：被收盘突破的最大窗口
    lvl = 6
    for w in LEVELS[1:]:
        if i >= w and ((is_long and c[i] > h[i - w:i].max()) or
                       (not is_long and c[i] < l[i - w:i].min())):
            lvl = w

    # 前高压力（60根内，窗口不足60用现有长度）
    w60 = min(i, 60)
    if is_long:
        resist = (h[i - w60:i].max() - entry) / entry * 100
    else:
        resist = (entry - l[i - w60:i].min()) / entry * 100

    # 段内再突破次数（同方向嵌套信号）
    slist = sig_idx["long" if is_long else "short"]
    nested = bisect.bisect_right(slist, end_i) - bisect.bisect_right(slist, i)

    amp, pnl = float(r[AMP]), float(r[PNL])

    # 资金费率 / OI（T=信号K收盘时刻，T_ex=出场K收盘时刻）
    T = int(ms4[i]) + BAR_MS
    T_ex = (int(ms4[ex]) + BAR_MS) if ex is not None else (int(ms4[m - 1]) + BAR_MS)
    k1 = np.searchsorted(f_ts, T, side="right") - 1
    rate0 = float(f_rate[k1]) if k1 >= 0 else np.nan
    fsum = float(f_rate[np.searchsorted(f_ts, T, side="right"):
                        np.searchsorted(f_ts, T_ex, side="right")].sum()) * 100
    j1 = np.searchsorted(o_ts, T, side="right") - 1
    # 新鲜度守卫：快照滞后>60分钟（源转储缺口，如2021-02-19宕机日）视为无数据，宁可空不用陈值
    oi_age_ms = T - o_ts[j1] if j1 >= 0 else None
    if j1 >= 0 and oi_age_ms <= 60 * 60_000:
        oi0 = float(o_val[j1])
    else:
        oi0 = np.nan
    j7 = np.searchsorted(o_ts, T - 7 * 86_400_000, side="right") - 1
    oi7chg = (oi0 / o_val[j7] - 1) * 100 if (not np.isnan(oi0) and j7 >= 0) else np.nan
    jx = np.searchsorted(o_ts, T_ex, side="right") - 1
    oichg = (o_val[jx] / oi0 - 1) * 100 if (not np.isnan(oi0) and jx >= 0) else np.nan
    rows.append({
        "方向": r["方向"],
        "突破K时间": r["突破K时间"],
        "突破K索引": i,
        "年份": r["年份"],
        # —— 入场（t=0 收盘即知）——
        "入场原因": "6K收盘突破",
        "入场价格(6K突破线)": round(entry, 2),
        "突破K开盘": round(o[i], 2),
        "突破K收盘": round(c[i], 2),
        "突破幅度%": round(d * (c[i] - entry) / entry * 100, 3),
        "突破幅度R": round(d * (c[i] - entry) / atrv, 3),
        "突破结构级别(根)": lvl,
        "前高压力%(60根)": round(resist, 3),
        "量比": round(v[i] / v[max(i - 20, 0):i].mean(), 3),
        "突破K实体效率": round(abs(c[i] - o[i]) / (h[i] - l[i]), 3) if h[i] > l[i] else 0.0,
        "6K区间紧致度": round((h[i - 6:i].max() - l[i - 6:i].min()) / atrv, 3),
        "入场时KAMA线": round(float(kama_vals[i - 1]), 2),
        "入场距KAMA线%": round(d * (entry - kama_vals[i - 1]) / entry * 100, 3),
        # —— 风险单位 ——
        "ATR值": round(atrv, 2),
        "ATR%": round(atrp, 3),
        # —— 日线环境（t=0 已知，无前视）——
        "日线EMA70(昨收)": round(e, 2),
        "日线EMA70偏离度%": round(ema_dev, 2) if not np.isnan(ema_dev) else np.nan,
        "入场时资金费率%(已结算)": round(rate0 * 100, 4) if not np.isnan(rate0) else np.nan,
        "入场时持仓量OI(BTC)": round(oi0, 0) if not np.isnan(oi0) else np.nan,
        "入场时OI较7日前%": round(oi7chg, 2) if not np.isnan(oi7chg) else np.nan,
        # —— 早期路径（t=3）——
        "存活至第3K": alive3,
        "前3K已走出幅度%(t=3)": round(mfe3, 3) if not np.isnan(mfe3) else np.nan,
        # —— 波峰 / 逆向 ——
        "波峰K时间": r["波峰K时间"],
        "波峰价格": r["波峰价格"],
        "到峰K数": peak_i - i,
        "涨跌幅%(突破线→波峰)": r[AMP],
        "峰值R": round(amp / atrp, 3),
        "MAE%(最大逆向偏移)": round(mae, 3),
        "MAE R": round(mae / atrp, 3),
        # —— 出场 ——
        "出场原因": ("收盘跌破KAMA线" if is_long else "收盘涨破KAMA线") if not und else "未终结(数据末端)",
        "出场价格(终结KAMA线)": r["终结时KAMA线(出场)"],
        "KAMA终结K时间": r["KAMA终结K时间"],
        "终结K开盘": round(o[ex], 2) if ex is not None else np.nan,
        "终结K收盘": r["终结K收盘"],
        "终结K幅度%(负=逆向)": round(d * (c[ex] - o[ex]) / o[ex] * 100, 3) if ex is not None else np.nan,
        "是否跳空越线出场": bool(ex is not None and
                                  (o[ex] < line if is_long else o[ex] > line)),
        "终结K距波峰K数": (ex - peak_i) if ex is not None else "",
        "段内再突破次数": nested,
        # —— 盈亏派生 ——
        PNL: r[PNL],
        "持仓资金费合计%(多头口径)": round(fsum, 4),
        "持仓期间OI变化%": round(oichg, 2) if not np.isnan(oichg) else np.nan,
        "R值(盈亏/ATR)": round(pnl / atrp, 3) if not np.isnan(pnl) else np.nan,
        "回吐%(涨跌幅-盈亏)": round(amp - pnl, 3) if not np.isnan(pnl) else np.nan,
        "捕获率%(盈亏/涨跌幅)": round(pnl / amp * 100, 1) if not np.isnan(pnl) else np.nan,
        "突破至终结K数": r["突破至终结K数"],
        "持仓天数": round(r["突破至终结K数"] * 4 / 24, 2) if not und else "",
        "是否未终结": und,
    })

out = pd.DataFrame(rows)

# —— 大波段组归属（两份CSV按 突破K索引/组ID 关联）——
sw = pd.read_csv(os.path.join(HERE, "results", "全时段波段数据集_6K突破_KAMA收盘终结.csv"))
grp = pd.read_csv(os.path.join(HERE, "results", "全时段大波段组_6K突破_KAMA收盘终结.csv"))
out = out.merge(sw[["突破K索引", "大波段组ID", "组内角色", "组类型", "大波段终点价格"]],
                on="突破K索引", how="left")
out = out.merge(grp[["大波段组ID", "延伸涨幅%"]].rename(columns={"延伸涨幅%": "大波段延伸涨幅%"}),
                on="大波段组ID", how="left")

# ============ 自检 ============
ok = out.dropna(subset=["R值(盈亏/ATR)"])
# 容差：ATR%列保留3位，小ATR%×大R时舍入相对误差被放大（实测最大~0.03R）
assert np.allclose(ok["R值(盈亏/ATR)"], ok[PNL] / ok["ATR%"], rtol=0.01, atol=0.01)
assert (out["MAE%(最大逆向偏移)"] <= 0.001).all(), "MAE应恒≤0"
assert (out["突破结构级别(根)"] >= 6).all() and (out["突破结构级别(根)"] % 2 == 0).all()
assert (out["段内再突破次数"] >= 0).all()
assert out["大波段组ID"].notna().all() and len(out) == len(df), "组归属应全覆盖"
al = out[out["存活至第3K"]]
assert al["前3K已走出幅度%(t=3)"].notna().all()
assert out[~out["存活至第3K"]]["前3K已走出幅度%(t=3)"].isna().all()
n_nest = int(out["段内再突破次数"].sum())
print(f"自检通过：R值一致性、MAE≤0、结构级别、组归属全覆盖（{len(out)}段）")
fr = out["入场时资金费率%(已结算)"].dropna()
assert fr.abs().max() < 0.5, "资金费率应在 ±0.3% 上限内"
oi_nan = int(out["入场时持仓量OI(BTC)"].isna().sum())
print(f"资金费率覆盖 {len(fr)}/{len(out)} 段（|费率|≤0.3% 上限校验通过），"
      f"中位={fr.median():.4f}%  P95={fr.quantile(.95):.4f}%")
print(f"OI覆盖 {len(out) - oi_nan}/{len(out)} 段（{oi_nan} 段早于2020-09-01无数据），"
      f"入场时中位={out['入场时持仓量OI(BTC)'].median():.0f} BTC")
print(f"持仓资金费合计%(多头口径): 中位={out['持仓资金费合计%(多头口径)'].median():.4f}%  "
      f"P90={out['持仓资金费合计%(多头口径)'].quantile(.9):.4f}%  "
      f"max={out['持仓资金费合计%(多头口径)'].max():.4f}%")
print(f"段内再突破合计 {n_nest}（应≈此前合并的嵌套信号数 1415±少量）")

out = out.sort_values("突破K时间", ascending=False).reset_index(drop=True)
out_csv = os.path.join(HERE, "results", "单段完整指标_6K突破到KAMA.csv")
out.to_csv(out_csv, index=False, encoding="utf-8-sig")
print(f"已导出 {out_csv}（{len(out)} 行 × {len(out.columns)} 列）")

# ============ 新增指标摘要 ============
print("\n===== v2 新增列摘要 =====")
print(f"MAE%: 中位={out['MAE%(最大逆向偏移)'].median():.2f}%  P25={out['MAE%(最大逆向偏移)'].quantile(.25):.2f}%  "
      f"P10={out['MAE%(最大逆向偏移)'].quantile(.1):.2f}%  max(最深)={out['MAE%(最大逆向偏移)'].min():.2f}%")
print("突破结构级别分布:")
print(out["突破结构级别(根)"].value_counts().sort_index().to_string())
print(f"前高压力%(60根)（恒≥0，0=上方无近端套牢）: =0占比 {(out['前高压力%(60根)'] == 0).mean():.1%}  "
      f"中位={out['前高压力%(60根)'].median():.2f}%")
big = out.groupby("突破结构级别(根)")[[AMP]].median().round(2)
print(f"各结构级别的空间中位:\n{big.to_string()}")
print(f"段内再突破次数: 中位={out['段内再突破次数'].median():.0f}  P75={out['段内再突破次数'].quantile(.75):.0f}  "
      f"max={out['段内再突破次数'].max()}")
print("\n前2行预览（最新，含新列）:")
prev = ["方向", "突破K时间", "入场价格(6K突破线)", "出场价格(终结KAMA线)", "突破结构级别(根)",
        "前高压力%(60根)", "MAE%(最大逆向偏移)", AMP, PNL, "R值(盈亏/ATR)", "大波段组ID"]
print(out[prev].head(2).to_string(index=False))
