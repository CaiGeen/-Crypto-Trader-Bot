"""单段涨跌幅+盈亏数据集：6K突破 → KAMA线终结（2026-08-23，独立文件不改旧数据）。

口径（用户定义）：
  - 段的划分：6K收盘突破起段；首个收盘跌破前一K收盘确认的KAMA线
    （做多 close[j] < KAMA[j-1]，做空镜像）即终结并重置；下一段必须等新的
    6K收盘突破，终结前的嵌套突破不重复计段（与 swing_dataset.py 段划分逐行一致）。
  - 涨跌幅%（主口径）= 突破线 → 段内波峰（wick极值：做多最高high / 做空最低low），
    与波段数据集的 波段涨跌幅% 同值（起点价格≡突破线）。
  - 盈亏% = 突破线 → 被跌破的KAMA线值 KAMA[ex-1]（前一K收盘确认的固定线，G6语义，
    假定按线价跟踪平仓），可为负（失败段：出场线低于入场基准）。
  - 时间均为 UTC+8；按突破K时间降序导出。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd

from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = BacktestConfig()

try:
    from data_loader import load_klines
    df = load_klines(cfg)
except Exception as e:
    print(f"增量拉取失败（{e}），回退本地缓存")
    df = pd.read_parquet(cfg.cache_path)

df = (df.drop_duplicates(subset="open_time_ms")
        .sort_values("open_time_ms").reset_index(drop=True))
now = datetime.now(timezone.utc)
if df["open_time"].iloc[-1] + timedelta(hours=4) > now:
    df = df.iloc[:-1].reset_index(drop=True)

highs = df["high"].to_numpy()
lows = df["low"].to_numpy()
closes = df["close"].to_numpy()
times = df["open_time"].tolist()
kama_vals = kama(closes, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
n = len(df)
W = cfg.window

AMP = "涨跌幅%(突破线→波峰)"
PNL = "盈亏%(突破线→KAMA线)"


def fmt(t) -> str:
    return pd.Timestamp(t).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")


records = []
last_exit = {"long": -1, "short": -1}
skipped_nested = 0

for s in detect_breakout(df, W):
    d = s.direction
    if s.idx <= last_exit[d]:
        skipped_nested += 1
        continue
    i = s.idx
    if np.isnan(kama_vals[i]):
        continue

    entry = s.breakthrough  # 起点 = 6K突破线

    # 终结：首个收盘跌破前一K收盘确认KAMA线的 K → 重置，等下一次突破
    exit_idx = None
    for j in range(i + 1, n):
        if np.isnan(kama_vals[j - 1]):
            continue
        if (d == "long" and closes[j] < kama_vals[j - 1]) or \
           (d == "short" and closes[j] > kama_vals[j - 1]):
            exit_idx = j
            break

    # 段内波峰（wick极值，涨跌幅的终点）
    search_end = exit_idx if exit_idx is not None else n - 1
    seg = highs[i:search_end + 1] if d == "long" else lows[i:search_end + 1]
    pick = int(np.argmax(seg)) if d == "long" else int(np.argmin(seg))
    peak_idx = i + pick
    peak = float(seg[pick])

    # 盈亏：按被跌破的KAMA线价出场
    exit_line = float(kama_vals[exit_idx - 1]) if exit_idx is not None else np.nan
    if d == "long":
        pnl = (exit_line - entry) / entry * 100 if exit_idx is not None else np.nan
        amp = (peak - entry) / entry * 100
    else:
        pnl = (entry - exit_line) / entry * 100 if exit_idx is not None else np.nan
        amp = (entry - peak) / entry * 100

    records.append({
        "方向": "做多" if d == "long" else "做空",
        "突破K时间": fmt(times[i]),
        "突破K索引": i,
        "突破线(起点)": round(entry, 2),
        "突破K收盘": round(float(closes[i]), 2),
        "波峰K时间": fmt(times[peak_idx]),
        "波峰K索引": peak_idx,
        "波峰价格": round(peak, 2),
        AMP: round(amp, 3),
        "KAMA终结K时间": fmt(times[exit_idx]) if exit_idx is not None else "",
        "KAMA终结K索引": exit_idx if exit_idx is not None else "",
        "终结时KAMA线(出场)": round(exit_line, 2) if exit_idx is not None else np.nan,
        "终结K收盘": round(float(closes[exit_idx]), 2) if exit_idx is not None else np.nan,
        PNL: round(pnl, 3) if exit_idx is not None else np.nan,
        "突破至终结K数": (exit_idx - i) if exit_idx is not None else "",
        "年份": pd.Timestamp(times[i]).tz_convert("Asia/Shanghai").year,
        "是否未终结": exit_idx is None,
    })
    last_exit[d] = exit_idx if exit_idx is not None else n - 1

rdf = pd.DataFrame(records)

# ============ 断言自检 ============
for _, r in rdf.iterrows():
    d = "long" if r["方向"] == "做多" else "short"
    i = int(r["突破K索引"])
    if d == "long":
        assert closes[i] > highs[i - W:i].max()
        assert r["突破线(起点)"] == round(float(highs[i - W:i].max()), 2)
        if not bool(r["是否未终结"]):
            assert r["波峰价格"] == round(float(highs[i:int(r["KAMA终结K索引"]) + 1].max()), 2)
    else:
        assert closes[i] < lows[i - W:i].min()
        assert r["突破线(起点)"] == round(float(lows[i - W:i].min()), 2)
        if not bool(r["是否未终结"]):
            assert r["波峰价格"] == round(float(lows[i:int(r["KAMA终结K索引"]) + 1].min()), 2)
    if not bool(r["是否未终结"]):
        ex = int(r["KAMA终结K索引"])
        if d == "long":
            assert closes[ex] < kama_vals[ex - 1], f"终结K未跌破前K KAMA线 @行{r.name}"
            assert all(closes[j] >= kama_vals[j - 1] for j in range(i + 1, ex))
        else:
            assert closes[ex] > kama_vals[ex - 1]
            assert all(closes[j] <= kama_vals[j - 1] for j in range(i + 1, ex))
for d in ("做多", "做空"):
    prev_ex = -1
    for _, r in rdf[rdf["方向"] == d].sort_values("突破K索引").iterrows():
        assert int(r["突破K索引"]) > prev_ex, f"{d}段未按KAMA破线重置 @行{r.name}"
        prev_ex = int(r["KAMA终结K索引"]) if not bool(r["是否未终结"]) else n - 1
print(f"断言自检通过：{len(rdf)} 段（嵌套突破已合并 {skipped_nested} 个信号，破线即重置）")

# 与波段数据集交叉验证：段划分一致；涨跌幅(突破线→波峰) ≡ 波段涨跌幅%
old = pd.read_csv(os.path.join(HERE, "results", "全时段波段数据集_6K突破_KAMA收盘终结.csv"))


def _norm(d):
    d = d.sort_values("突破K索引")[["突破K索引", "KAMA终结K索引"]].copy()
    for col in ("突破K索引", "KAMA终结K索引"):
        d[col] = pd.to_numeric(d[col], errors="coerce").fillna(-1).astype("int64")
    return d.reset_index(drop=True)


a, b = _norm(rdf), _norm(old)
print(f"与波段数据集交叉验证: 段划分{'完全一致' if a.equals(b) else '存在差异（数据尾部更新所致）'}"
      f"（{len(a)} vs {len(b)} 段）")
merged = rdf.sort_values("突破K索引").merge(
    old.sort_values("突破K索引")[["突破K索引", "波段涨跌幅%"]], on="突破K索引", how="inner")
diff = (merged[AMP] - merged["波段涨跌幅%"]).abs().max()
print(f"{AMP} ≡ 波段涨跌幅%: 最大偏差 {diff}")

# ============ 导出（UTC+8、降序） ============
rdf = rdf.sort_values("突破K时间", ascending=False).reset_index(drop=True)
out_csv = os.path.join(HERE, "results", "单段涨跌幅_6K突破线到KAMA线.csv")
rdf.to_csv(out_csv, index=False, encoding="utf-8-sig")
print(f"已导出 {out_csv}（{len(rdf)} 行 × {len(rdf.columns)} 列）")

# ============ 摘要 ============
print("\n===== 单段摘要（段 = 6K突破 → 收盘破KAMA线重置） =====")
print(f"数据范围: {fmt(times[0])} ~ {fmt(times[-1])} UTC+8")
for d in ["做多", "做空"]:
    sub = rdf[rdf["方向"] == d]
    amp, pnl = sub[AMP], sub[PNL].dropna()
    print(f"{d}: n={len(sub)}")
    print(f"  涨跌幅(突破线→波峰): 中位={amp.median():.2f}%  均值={amp.mean():.2f}%  "
          f"P75={amp.quantile(.75):.2f}%  P90={amp.quantile(.9):.2f}%  max={amp.max():.2f}%")
    print(f"  盈亏(突破线→KAMA线): 中位={pnl.median():.2f}%  均值={pnl.mean():.2f}%  "
          f"P90={pnl.quantile(.9):.2f}%  max={pnl.max():.2f}%  min={pnl.min():.2f}%  "
          f"负占比={(pnl < 0).mean():.1%}")
print("\n按年份: 涨跌幅中位 / 盈亏中位 / 盈亏负占比")
yr = rdf.groupby(["年份", "方向"]).agg(
    n=(AMP, "count"),
    涨跌幅中位=(AMP, lambda x: round(x.median(), 2)),
    盈亏中位=(PNL, lambda x: round(x.median(), 2)),
    盈亏负占比=(PNL, lambda x: round((x < 0).mean(), 3)))
print(yr.to_string())
cols = ["方向", "突破K时间", "突破线(起点)", "波峰价格", AMP, "终结时KAMA线(出场)", PNL, "突破至终结K数"]
print("\n涨跌幅最大的5段:")
print(rdf.nlargest(5, AMP)[cols].to_string(index=False))
print("\n盈亏最深负的5段:")
print(rdf.nsmallest(5, PNL)[cols].to_string(index=False))
