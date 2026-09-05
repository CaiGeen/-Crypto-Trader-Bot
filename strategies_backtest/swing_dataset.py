"""全时段波段数据集：6K收盘突破锚定 + KAMA收盘终结。

波段定义（2026-08-23）：
  - 触发：某K收盘突破前6根K组成区间的最高点（做多）/最低点（做空）
    —— 即 signal_detector.detect_breakout(window=6)，参数复用6K策略。
  - 起点：突破K之前那个6K区间内的最后一个最高点（做多取区间最高high所在K，
    并列时取最后一根；做空镜像取区间最低low）。
  - 终点：突破后首个【收盘跌破当根收盘确认KAMA值】的K出现之前，
    整个波段的最高点（做空镜像为最低点）。KAMA=10/2/30 复用策略参数。
  - 去重：同一方向，前一波段KAMA终结K之前出现的突破不另起波段
    （"至少一个突破"即成段，信号合并口径同 peak_to_kama_dd.py）。

口径说明：
  - KAMA终结线取 KAMA[j-1]（前一K收盘确认的固定值，2026-08-23用户确认，
    与6K策略G6语义一致：线在K线走完前即可挂单，无盘中循环依赖）。
    终结判定 close[j] < KAMA[j-1]（做空 close[j] > KAMA[j-1]），收盘判定而非盘中。
    注：KAMA[j] 严格介于 KAMA[j-1] 与 close[j] 之间，故与 close[j]<KAMA[j]
    数学等价——口径切换不改变任何终结K，只改变报告线值与回吐基准。
  - 终点回吐至KAMA线% = 终点价格回吐到终结线 KAMA[ex-1] 的幅度
    （假定该区间内按跟踪线价平仓，做空镜像）。
  - 终结K自身的 high/low 计入终点搜索域（跌破发生在该K收盘，盘中极值在先）。
  - 数据末端仍未终结的波段标记 是否未终结=是，终点为截至末根的运行极值。
  - 起点价格 ≡ 突破线（6K区间极值即起点K的high/low），两列等值仅作自证。
  - 所有时间字段为 UTC+8；导出CSV按时间降序（最新在前）。

大波段归属分组（2026-08-23 二轮确认）：
  - 分组：同方向按段级"创新高/新低"链式成组——后续段终点创组内新高（做多）
    /新低（做空）则归入同组，否则当前组终结、该段开新组当首段（失败段归新组）。
  - 组起点 = 前一组终点价格（峰到峰锚定，用户确认："以突破K前一个最近的高点
    为准"；段链式相邻，前组终点即最近已确认高点）。首组无前组，用首段起点。
    组间价格零重叠；组终点未收复前高 → 延伸涨幅为负，标注 失败组。
  - 组角色：独立段（单段成组）/ 延续子集段（被组内后续段超越）/ 终点段（持有组极值）。
  - 双涨幅口径：延伸涨幅%（前高→组终点，主口径）+ 全腿涨幅%（首段起点→组终点，参考）。
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

# —— 数据：优先增量刷新，失败回退本地缓存 ——
try:
    from data_loader import load_klines
    df = load_klines(cfg)
except Exception as e:
    print(f"增量拉取失败（{e}），回退本地缓存")
    df = pd.read_parquet(cfg.cache_path)

df = (df.drop_duplicates(subset="open_time_ms")
        .sort_values("open_time_ms").reset_index(drop=True))

# 末根可能未走完（收盘未确认）→ 剔除，避免半根K产生假突破/假终结
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


def last_argmax(a) -> int:
    """最后一个最大值位置（起点并列最高时取最后一根）。"""
    return len(a) - 1 - int(np.argmax(a[::-1]))


def last_argmin(a) -> int:
    """最后一个最小值位置（做空起点并列最低时取最后一根）。"""
    return len(a) - 1 - int(np.argmin(a[::-1]))


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

    # 起点：6K区间内最后出现的极值K（做多=最高high，做空=最低low）
    if d == "long":
        start_idx = i - W + last_argmax(highs[i - W:i])
        start_price = float(highs[start_idx])
    else:
        start_idx = i - W + last_argmin(lows[i - W:i])
        start_price = float(lows[start_idx])

    # 终结：首个收盘跌破前一K收盘确认KAMA线（KAMA[j-1]）的 K
    exit_idx = None
    for j in range(i + 1, n):
        if np.isnan(kama_vals[j - 1]):
            continue
        if (d == "long" and closes[j] < kama_vals[j - 1]) or \
           (d == "short" and closes[j] > kama_vals[j - 1]):
            exit_idx = j
            break

    # 终点：起点→终结K（含）整个波段的极值，并列取首次触及
    search_end = exit_idx if exit_idx is not None else n - 1
    seg = highs[start_idx:search_end + 1] if d == "long" else lows[start_idx:search_end + 1]
    pick = int(np.argmax(seg)) if d == "long" else int(np.argmin(seg))
    end_idx = start_idx + pick
    end_price = float(seg[pick])

    kama_line = float(kama_vals[exit_idx - 1]) if exit_idx is not None else np.nan
    if d == "long":
        amp = (end_price - start_price) / start_price * 100
        giveback = ((end_price - kama_line) / end_price * 100
                    if exit_idx is not None else np.nan)
    else:
        amp = (start_price - end_price) / start_price * 100
        giveback = ((kama_line - end_price) / end_price * 100
                    if exit_idx is not None else np.nan)

    records.append({
        "方向": "做多" if d == "long" else "做空",
        "突破K时间": fmt(times[i]),
        "突破K索引": i,
        "突破线": round(s.breakthrough, 2),
        "突破K收盘": round(float(closes[i]), 2),
        "起点时间": fmt(times[start_idx]),
        "起点索引": start_idx,
        "起点价格": round(start_price, 2),
        "终点时间": fmt(times[end_idx]),
        "终点索引": end_idx,
        "终点价格": round(end_price, 2),
        "KAMA终结K时间": fmt(times[exit_idx]) if exit_idx is not None else "",
        "KAMA终结K索引": exit_idx if exit_idx is not None else "",
        "终结K收盘": round(float(closes[exit_idx]), 2) if exit_idx is not None else np.nan,
        "终结时KAMA线": round(kama_line, 2) if exit_idx is not None else np.nan,
        "波段涨跌幅%": round(amp, 3),
        "终点回吐至KAMA线%": round(giveback, 3) if exit_idx is not None else np.nan,
        "起点至终点K数": end_idx - start_idx,
        "起点至突破K数": i - start_idx,
        "突破至终结K数": (exit_idx - i) if exit_idx is not None else "",
        "年份": pd.Timestamp(times[i]).tz_convert("Asia/Shanghai").year,
        "是否未终结": exit_idx is None,
    })
    last_exit[d] = exit_idx if exit_idx is not None else n - 1

rdf = pd.DataFrame(records)

# ============ 全量断言自检 ============
for _, r in rdf.iterrows():
    d = "long" if r["方向"] == "做多" else "short"
    i, st, en = int(r["突破K索引"]), int(r["起点索引"]), int(r["终点索引"])
    if d == "long":
        assert closes[i] > highs[i - W:i].max(), f"突破不成立 @行{r.name}"
        assert r["突破线"] == round(float(highs[i - W:i].max()), 2), f"突破线≠区间极值 @行{r.name}"
        assert r["起点价格"] == round(float(highs[st]), 2), f"起点≠区间最高K @行{r.name}"
        assert r["终点价格"] == round(float(highs[st:en + 1].max()), 2), f"终点≠波段最高 @行{r.name}"
    else:
        assert closes[i] < lows[i - W:i].min(), f"突破不成立 @行{r.name}"
        assert r["突破线"] == round(float(lows[i - W:i].min()), 2), f"突破线≠区间极值 @行{r.name}"
        assert r["起点价格"] == round(float(lows[st]), 2), f"起点≠区间最低K @行{r.name}"
        assert r["终点价格"] == round(float(lows[st:en + 1].min()), 2), f"终点≠波段最低 @行{r.name}"
    # 非循环交叉断言：起点K的极值必须就是区间极值（曾经此处漏检做空argmax笔误）
    assert r["起点价格"] == r["突破线"], f"起点价格≠突破线 @行{r.name}"
    if not bool(r["是否未终结"]):
        ex = int(r["KAMA终结K索引"])
        assert en <= ex, f"终点在终结K之后 @行{r.name}"
        if d == "long":
            assert closes[ex] < kama_vals[ex - 1], f"终结K未跌破前K KAMA线 @行{r.name}"
            assert all(closes[j] >= kama_vals[j - 1] for j in range(i + 1, ex)), f"更早终结被漏检 @行{r.name}"
        else:
            assert closes[ex] > kama_vals[ex - 1], f"终结K未涨破前K KAMA线 @行{r.name}"
            assert all(closes[j] <= kama_vals[j - 1] for j in range(i + 1, ex)), f"更早终结被漏检 @行{r.name}"
for d in ("做多", "做空"):
    sub = rdf[rdf["方向"] == d]
    prev_ex = -1
    for _, r in sub.iterrows():
        assert int(r["突破K索引"]) > prev_ex, f"{d}波段重叠 @行{r.name}"
        prev_ex = int(r["KAMA终结K索引"]) if not bool(r["是否未终结"]) else n - 1
print(f"断言自检全部通过：{len(rdf)} 段（嵌套突破已合并 {skipped_nested} 个信号）")

# ============ 大波段归属分组 ============
gdf_rows = []
for d, tag in [("做多", "多"), ("做空", "空")]:
    idxs = rdf.index[rdf["方向"] == d].tolist()
    gs = []  # {m:行索引列表, ext, et, fs, ft, ei}
    for row_i in idxs:
        row = rdf.loc[row_i]
        ext = row["终点价格"]
        if not gs or (d == "做多" and ext <= gs[-1]["ext"]) or (d == "做空" and ext >= gs[-1]["ext"]):
            gs.append({"m": [row_i], "ext": ext, "et": row["终点时间"], "ei": int(row["终点索引"]),
                       "fs": row["起点价格"], "ft": row["起点时间"], "fi": int(row["起点索引"]),
                       "bt": row["突破K时间"]})
        else:
            gs[-1]["m"].append(row_i)
            gs[-1]["ext"] = ext
            gs[-1]["et"] = row["终点时间"]
            gs[-1]["ei"] = int(row["终点索引"])
    for k, g in enumerate(gs):
        gid = f"{tag}{k + 1:03d}"
        if k > 0:
            a_price, a_time, a_idx = gs[k - 1]["ext"], gs[k - 1]["et"], gs[k - 1]["ei"]
        else:
            a_price, a_time, a_idx = g["fs"], g["ft"], g["fi"]
        ext_amp = ((g["ext"] - a_price) / a_price * 100 if d == "做多"
                   else (a_price - g["ext"]) / a_price * 100)
        leg_amp = ((g["ext"] - g["fs"]) / g["fs"] * 100 if d == "做多"
                   else (g["fs"] - g["ext"]) / g["fs"] * 100)
        gtype = "延伸组" if ext_amp > 0 else "失败组"
        for pos, row_i in enumerate(g["m"]):
            if len(g["m"]) == 1:
                role = "独立段"
            else:
                role = "终点段" if pos == len(g["m"]) - 1 else "延续子集段"
            rdf.loc[row_i, "大波段组ID"] = gid
            rdf.loc[row_i, "组内序号"] = pos + 1
            rdf.loc[row_i, "组内角色"] = role
            rdf.loc[row_i, "组类型"] = gtype
            rdf.loc[row_i, "大波段终点时间"] = g["et"]
            rdf.loc[row_i, "大波段终点价格"] = round(float(g["ext"]), 2)
            rdf.loc[row_i, "组内段数"] = len(g["m"])
        member_amps = rdf.loc[g["m"], "波段涨跌幅%"]
        gdf_rows.append({
            "大波段组ID": gid,
            "方向": d,
            "组类型": gtype,
            "组内段数": len(g["m"]),
            "锚定突破K时间": g["bt"],
            "延伸起点时间": a_time,
            "延伸起点价格": round(float(a_price), 2),
            "延伸起点索引": a_idx,
            "组终点时间": g["et"],
            "组终点价格": round(float(g["ext"]), 2),
            "组终点索引": g["ei"],
            "延伸涨幅%": round(float(ext_amp), 3),
            "全腿起点时间": g["ft"],
            "全腿起点价格": round(float(g["fs"]), 2),
            "全腿涨幅%": round(float(leg_amp), 3),
            "组覆盖K数": g["ei"] - a_idx,
            "组内最大单段涨幅%": round(float(member_amps.max()), 3),
            "年份": pd.Timestamp(g["bt"]).year,
        })
gdf = pd.DataFrame(gdf_rows)

# 分组断言：峰到峰零重叠 + 归属完整
for d in ("做多", "做空"):
    sub = gdf[gdf["方向"] == d].sort_values("大波段组ID").reset_index(drop=True)
    for k in range(1, len(sub)):
        prev, cur = sub.iloc[k - 1], sub.iloc[k]
        assert cur["延伸起点价格"] == prev["组终点价格"], f"{d} 组{k}未峰到峰衔接"
        assert cur["延伸起点时间"] == prev["组终点时间"], f"{d} 组{k}起点时间≠前组终点时间"
assert len(rdf[rdf["大波段组ID"].notna()]) == len(rdf), "存在未归属的段"
for _, g in gdf.iterrows():
    m = rdf[rdf["大波段组ID"] == g["大波段组ID"]]
    assert len(m) == g["组内段数"]
    assert m["组内序号"].tolist() == list(range(1, g["组内段数"] + 1))
    if g["方向"] == "做多":
        assert m["终点价格"].iloc[-1] == g["组终点价格"], "组终点应为首末段链式极值（末段持有）"
    else:
        assert m["终点价格"].iloc[-1] == g["组终点价格"], "组终点应为末段持有"
print(f"分组断言通过：{len(gdf)} 组（延伸 {(gdf['组类型'] == '延伸组').sum()} / "
      f"失败 {(gdf['组类型'] == '失败组').sum()}），{len(rdf)} 段全部归属")

# ============ 导出（UTC+8、时间降序，最新在前） ============
rdf = rdf.sort_values("突破K时间", ascending=False).reset_index(drop=True)
gdf = gdf.sort_values("锚定突破K时间", ascending=False).reset_index(drop=True)
out_csv = os.path.join(HERE, "results", "全时段波段数据集_6K突破_KAMA收盘终结.csv")


def save_csv(frame, path):
    """Excel占用时降级写 <名>_latest.csv，避免整条管线中断（关闭Excel后重跑即恢复规范名）。"""
    try:
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"已导出 {path}（{len(frame)} 行 × {len(frame.columns)} 列）")
    except PermissionError:
        alt = path.replace(".csv", "_latest.csv")
        frame.to_csv(alt, index=False, encoding="utf-8-sig")
        print(f"⚠ 目标被占用（Excel?），已降级写入 {alt}（{len(frame)} 行 × {len(frame.columns)} 列）"
              f"——关闭Excel后重跑本脚本可恢复规范文件名")
os.makedirs(os.path.dirname(out_csv), exist_ok=True)
save_csv(rdf, out_csv)
save_csv(gdf, os.path.join(HERE, "results", "全时段大波段组_6K突破_KAMA收盘终结.csv"))

# ============ 摘要 ============
print("\n===== 波段数据集摘要 =====")
print(f"数据范围: {fmt(times[0])} ~ {fmt(times[-1])} UTC+8，共 {n} 根4H K线")
print(f"波段总数: {len(rdf)}  做多 {(rdf['方向'] == '做多').sum()}  做空 {(rdf['方向'] == '做空').sum()}"
      f"  未终结(数据末端) {int(rdf['是否未终结'].sum())}")
for d in ["做多", "做空"]:
    sub = rdf[rdf["方向"] == d]["波段涨跌幅%"]
    if len(sub) == 0:
        continue
    print(f"{d}: n={len(sub)}  中位={sub.median():.2f}%  均值={sub.mean():.2f}%  "
          f"P25={sub.quantile(.25):.2f}%  P75={sub.quantile(.75):.2f}%  "
          f"P90={sub.quantile(.9):.2f}%  max={sub.max():.2f}%  min={sub.min():.3f}%")
dur = rdf["起点至终点K数"]
print(f"起点→终点K数: 中位={dur.median():.0f}  均值={dur.mean():.1f}  P90={dur.quantile(.9):.0f}  max={dur.max()}")
gb = rdf["终点回吐至KAMA线%"].dropna()
print(f"终点回吐至KAMA线%: 中位={gb.median():.2f}  均值={gb.mean():.2f}  P75={gb.quantile(.75):.2f}  P90={gb.quantile(.9):.2f}")
print("\n按年份段数与中位涨跌幅:")
yr = rdf.groupby(["年份", "方向"])["波段涨跌幅%"].agg(["count", "median"]).round(2)
print(yr.to_string())
print("\n涨幅最大的5段:")
cols = ["方向", "突破K时间", "起点时间", "终点时间", "波段涨跌幅%", "起点至终点K数"]
print(rdf.nlargest(5, "波段涨跌幅%")[cols].to_string(index=False))
print("\n最小的5段（可负=突破后未创新极值即终结）:")
print(rdf.nsmallest(5, "波段涨跌幅%")[cols].to_string(index=False))
print("\n前5行预览:")
print(rdf[cols].head().to_string(index=False))

print("\n===== 大波段组统计（峰到峰锚定，延伸口径） =====")
for d in ["做多", "做空"]:
    sub = gdf[gdf["方向"] == d]
    ext_ = sub[sub["组类型"] == "延伸组"]["延伸涨幅%"]
    print(f"{d}: 组数{len(sub)}（延伸{len(ext_)} 失败{len(sub) - len(ext_)}）  "
          f"延伸组涨幅: 中位{ext_.median():.2f}%  P90{ext_.quantile(.9):.2f}%  max{ext_.max():.2f}%")
print("\n延伸涨幅最大的5组:")
gcols = ["大波段组ID", "方向", "延伸起点时间", "组终点时间", "延伸涨幅%", "全腿涨幅%", "组内段数", "组覆盖K数"]
print(gdf.nlargest(5, "延伸涨幅%")[gcols].to_string(index=False))
print("\n按年份组数:")
print(gdf.groupby(["年份", "方向"])["大波段组ID"].count().to_string())
