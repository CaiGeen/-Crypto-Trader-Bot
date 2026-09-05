"""源数据完整性 + 交叉性审计。
检查项：
  完整性：时间戳单调无重复无缺失、OHLC逻辑、无NaN/零/负值、成交量非负、价格跳变
  交叉性：4H vs 1m聚合、1d vs 4H聚合、标记价4H vs 最新价4H
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from pathlib import Path

cache = Path("G:/my-crypto-bot/strategies_backtest/cache")
TF_MS = {"1m": 60_000, "3m": 180_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

files = {
    "4h": cache / "BTCUSDT_4h.parquet",
    "1m": cache / "BTCUSDTUSDT_1m.parquet",
    "1d": cache / "BTCUSDT_1d.parquet",
    "4h_mark": cache / "BTCUSDTUSDT_4h_mark.parquet",
}

def load(name):
    p = files[name]
    if not p.exists():
        print(f"  [跳过] {p.name} 不存在")
        return None
    df = pd.read_parquet(p)
    if "open_time" not in df.columns:
        df["open_time"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
    return df

def check_integrity(name, tf):
    df = load(name)
    if df is None: return
    n = len(df); step = TF_MS[tf]
    print(f"\n{'━'*100}")
    print(f"【{name}】{files[name].name}  {n}根  {df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]}")
    print(f"{'━'*100}")
    issues = []

    # 1. 时间戳单调递增
    ms = df["open_time_ms"].to_numpy()
    if not np.all(np.diff(ms) > 0):
        bad = np.where(np.diff(ms) <= 0)[0]
        issues.append(f"时间戳非单调递增: {len(bad)}处，首处 idx={bad[0]}")
    # 2. 重复
    dup = len(ms) - len(np.unique(ms))
    if dup: issues.append(f"重复时间戳: {dup}个")
    # 3. 缺失K线
    gaps = np.diff(ms)
    missing = gaps[gaps != step]
    if len(missing) > 0:
        issues.append(f"缺失/异常间隔: {len(missing)}处（应={step}ms），首处 gap={missing[0]}ms")
    # 4. NaN
    for col in ["open", "high", "low", "close"]:
        cnt = df[col].isna().sum()
        if cnt: issues.append(f"{col}列NaN: {cnt}个")
    # 5. 零值/负值
    for col in ["open", "high", "low", "close"]:
        cnt = (df[col] <= 0).sum()
        if cnt: issues.append(f"{col}列<=0: {cnt}个")
    if "volume" in df.columns:
        cnt = (df["volume"] < 0).sum()
        if cnt: issues.append(f"volume负值: {cnt}个")
    # 6. OHLC逻辑
    h, l, o, c = df["high"].to_numpy(), df["low"].to_numpy(), df["open"].to_numpy(), df["close"].to_numpy()
    if (h < l).any(): issues.append(f"high<low: {(h<l).sum()}个")
    if (h < o).any(): issues.append(f"high<open: {(h<o).sum()}个")
    if (h < c).any(): issues.append(f"high<close: {(h<c).sum()}个")
    if (l > o).any(): issues.append(f"low>open: {(l>o).sum()}个")
    if (l > c).any(): issues.append(f"low>close: {(l>c).sum()}个")
    # 7. 异常跳变（相邻close变化>30%）
    chg = np.abs(np.diff(c) / c[:-1])
    big = np.where(chg > 0.30)[0]
    if len(big) > 0:
        issues.append(f"相邻close跳变>30%: {len(big)}处，首处 idx={big[0]} {chg[big[0]]:.0%}")
    # 8. 价格范围合理性
    print(f"  价格范围: open[{o.min():.1f}, {o.max():.1f}]  close[{c.min():.1f}, {c.max():.1f}]")
    if "volume" in df.columns:
        print(f"  成交量范围: [{df['volume'].min():.2f}, {df['volume'].max():.2f}]")

    if issues:
        print(f"  ⚠ 发现 {len(issues)} 类问题:")
        for x in issues: print(f"    - {x}")
    else:
        print(f"  ✓ 完整性检查通过（单调/无重复/无缺失/OHLC逻辑/无NaN/无零负/无异常跳变）")
    return df

def check_cross_4h_1m():
    df4 = load("4h"); df1 = load("1m")
    if df4 is None or df1 is None: return
    print(f"\n{'━'*100}")
    print("【交叉检查】4H vs 1m聚合")
    print(f"{'━'*100}")
    ms4 = df4["open_time_ms"].to_numpy(); step4 = TF_MS["4h"]
    ms1 = df1["open_time_ms"].to_numpy()
    o1 = df1["open"].to_numpy(); h1 = df1["high"].to_numpy()
    l1 = df1["low"].to_numpy(); c1 = df1["close"].to_numpy()
    checked = 0; mismatches = 0; no_coverage = 0
    max_diff = {"open": 0, "high": 0, "low": 0, "close": 0}
    for i in range(len(df4)):
        s = ms4[i]; e = s + step4
        lo = np.searchsorted(ms1, s); hi = np.searchsorted(ms1, e)
        cnt = hi - lo
        if cnt != 240:
            if cnt > 0: no_coverage += 1
            continue
        agg_o = o1[lo]; agg_c = c1[hi-1]; agg_h = h1[lo:hi].max(); agg_l = l1[lo:hi].min()
        r = df4.iloc[i]
        diffs = {"open": abs(r["open"]-agg_o), "high": abs(r["high"]-agg_h),
                 "low": abs(r["low"]-agg_l), "close": abs(r["close"]-agg_c)}
        for k in diffs: max_diff[k] = max(max_diff[k], diffs[k])
        if any(d > 0.01 for d in diffs.values()):
            mismatches += 1
            if mismatches <= 3:
                t = pd.Timestamp(s, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")
                print(f"    不一致: {t}  4H={r['open']:.1f}/{r['high']:.1f}/{r['low']:.1f}/{r['close']:.1f}  agg={agg_o:.1f}/{agg_h:.1f}/{agg_l:.1f}/{agg_c:.1f}")
        checked += 1
    print(f"  检查了 {checked} 根4H（1m完整覆盖），{no_coverage}根1m未完全覆盖，{mismatches}根不一致")
    print(f"  最大绝对差异: open={max_diff['open']:.4f} high={max_diff['high']:.4f} low={max_diff['low']:.4f} close={max_diff['close']:.4f}")
    if mismatches == 0: print(f"  ✓ 4H与1m聚合完全一致")
    else: print(f"  ⚠ {mismatches}根4H与1m聚合不一致")

def check_cross_1d_4h():
    dfd = load("1d"); df4 = load("4h")
    if dfd is None or df4 is None: return
    print(f"\n{'━'*100}")
    print("【交叉检查】1d vs 4H聚合")
    print(f"{'━'*100}")
    msd = dfd["open_time_ms"].to_numpy(); stepd = TF_MS["1d"]
    ms4 = df4["open_time_ms"].to_numpy()
    checked = 0; mismatches = 0; no_coverage = 0
    max_diff = {"open": 0, "high": 0, "low": 0, "close": 0}
    for i in range(len(dfd)):
        s = msd[i]; e = s + stepd
        mask = (ms4 >= s) & (ms4 < e)
        if mask.sum() != 6:
            if mask.sum() > 0: no_coverage += 1
            continue
        sub = df4[mask]
        agg_o = sub["open"].iloc[0]; agg_c = sub["close"].iloc[-1]
        agg_h = sub["high"].max(); agg_l = sub["low"].min()
        r = dfd.iloc[i]
        diffs = {"open": abs(r["open"]-agg_o), "high": abs(r["high"]-agg_h),
                 "low": abs(r["low"]-agg_l), "close": abs(r["close"]-agg_c)}
        for k in diffs: max_diff[k] = max(max_diff[k], diffs[k])
        if any(d > 0.01 for d in diffs.values()):
            mismatches += 1
            if mismatches <= 3:
                t = pd.Timestamp(s, unit="ms", tz="UTC").strftime("%Y-%m-%d")
                print(f"    不一致: {t}  1d={r['open']:.1f}/{r['high']:.1f}/{r['low']:.1f}/{r['close']:.1f}  agg={agg_o:.1f}/{agg_h:.1f}/{agg_l:.1f}/{agg_c:.1f}")
        checked += 1
    print(f"  检查了 {checked} 根1d（4H完整覆盖），{no_coverage}根4H未完全覆盖，{mismatches}根不一致")
    print(f"  最大绝对差异: open={max_diff['open']:.4f} high={max_diff['high']:.4f} low={max_diff['low']:.4f} close={max_diff['close']:.4f}")
    if mismatches == 0: print(f"  ✓ 1d与4H聚合完全一致")
    else: print(f"  ⚠ {mismatches}根1d与4H聚合不一致")

def check_mark_vs_latest():
    dfm = load("4h_mark"); df4 = load("4h")
    if dfm is None or df4 is None: return
    print(f"\n{'━'*100}")
    print("【交叉检查】标记价4H vs 最新价4H")
    print(f"{'━'*100}")
    msm = dfm["open_time_ms"].to_numpy(); ms4 = df4["open_time_ms"].to_numpy()
    common = np.intersect1d(msm, ms4)
    print(f"  标记价{len(dfm)}根，最新价{len(df4)}根，共同{len(common)}根")
    if len(common) == 0: print("  ⚠ 无共同时间戳"); return
    m_lookup = dfm.set_index("open_time_ms")
    l_lookup = df4.set_index("open_time_ms")
    diffs = {"open": [], "high": [], "low": [], "close": []}
    for t in common:
        rm = m_lookup.loc[t]; rl = l_lookup.loc[t]
        for k in diffs:
            if rm[k] > 0: diffs[k].append(abs(rm[k] - rl[k]) / rm[k] * 100)
    for k in diffs:
        a = np.array(diffs[k])
        print(f"  {k}差异%: 均值{a.mean():.5f} 最大{a.max():.5f} >0.01%占比{(a>0.01).mean():.1%}")

print("=" * 100)
print("源数据完整性 + 交叉性审计")
print("=" * 100)

check_integrity("4h", "4h")
check_integrity("1d", "1d")
check_integrity("1m", "1m")
check_integrity("4h_mark", "4h")
check_cross_1d_4h()
check_cross_4h_1m()
check_mark_vs_latest()

print(f"\n{'='*100}")
print("审计完成")
