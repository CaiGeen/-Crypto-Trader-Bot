"""回调突破线对比回测：6K极值基准 vs 回调突破线基准。

回调突破线定义：
  信号仍用6K极值突破检测（close[i] > max(high[i-6..i-1]) 做多）。
  在6K区间[i-6, i-1]中，从左到右扫描 j ∈ [i-6, i-3]：
    做多：找第一个满足 highs[j+1] <= highs[j] 且 highs[j+2] <= highs[j] 的 j，
          回调突破线 = highs[j]（前方K线最高点，经2K不创新高确认）
    做空：找第一个满足 lows[j+1] >= lows[j] 且 lows[j+2] >= lows[j] 的 j，
          回调突破线 = lows[j]
  找不到被2K确认的极值点时，该信号跳过不做单。

两版对比（其他逻辑完全相同）：
  基准版：突破线=6K极值，入场价=突破线，止损/止盈/涨跌幅以突破线计算
  回调版：入场价=回调突破线，止损/止盈/涨跌幅以回调突破线计算
  公共逻辑：1ATR初始止损 → 第3K后KAMA跟踪保本 → 做多22%/做空12%固定止盈 → 止损优先 → 信号合并
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
highs = df["high"].to_numpy()
lows = df["low"].to_numpy()
closes = df["close"].to_numpy()
times = df["open_time"].tolist()
years = df["open_time"].dt.year.to_numpy()
n = len(df)


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


atr_vals = calc_atr(highs, lows, closes, 20)
TP_LONG = 0.22
TP_SHORT = 0.12


def find_callback_breakthrough(i, direction, window=6):
    """在6K区间[i-window, i-1]中，从左到右找第一个被2K不创新高/低确认的极值点。

    做多：highs[j+1] <= highs[j] 且 highs[j+2] <= highs[j] → 回调突破线 = highs[j]
    做空：lows[j+1] >= lows[j] 且 lows[j+2] >= lows[j] → 回调突破线 = lows[j]
    返回(极值, j)，找不到返回(None, None)。
    """
    start = i - window
    end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n:
            continue
        if direction == "long":
            if highs[j + 1] <= highs[j] and highs[j + 2] <= highs[j]:
                return highs[j], j
        else:
            if lows[j + 1] >= lows[j] and lows[j + 2] >= lows[j]:
                return lows[j], j
    return None, None


def simulate(signal, base_price, atr_val):
    """回测单波段，以 base_price 作为入场价和涨跌幅/止损/止盈基准。

      - 前3K：ATR止损（init_stop = base_price ± 1×ATR）
      - 第3K后：KAMA跟踪保本（stop = max/min(KAMA[j-1], base_price)）
      - 固定止盈：做多22%/做空12%（从 base_price 计算）
      - 止损优先于止盈
      - R = (出场价 - base_price) / ATR
      - MFE = max((high - base_price) / base_price)
    """
    i = signal.idx
    if i + 1 >= n:
        return None
    d = signal.direction
    is_long = (d == "long")
    if np.isnan(atr_val) or atr_val <= 0:
        return None

    if is_long:
        init_stop = base_price - atr_val
        tp_price = base_price * (1 + TP_LONG)
    else:
        init_stop = base_price + atr_val
        tp_price = base_price * (1 - TP_SHORT)

    max_exc = -np.inf
    exit_idx = exit_price = exit_reason = None
    for j in range(i + 1, n):
        ks = kama_vals[j - 1]
        if np.isnan(ks):
            continue
        if is_long:
            exc = (highs[j] - base_price) / base_price
        else:
            exc = (base_price - lows[j]) / base_price
        if exc > max_exc:
            max_exc = exc

        bh = j - i
        if bh <= 3:
            stop_line = init_stop
            s_type = "atr止损"
        else:
            if is_long:
                stop_line = max(ks, base_price)
                s_type = "保本平仓" if ks < base_price else "KAMA跟踪"
            else:
                stop_line = min(ks, base_price)
                s_type = "保本平仓" if ks > base_price else "KAMA跟踪"

        if is_long and lows[j] < stop_line:
            exit_idx, exit_price, exit_reason = j, float(stop_line), s_type
            break
        if not is_long and highs[j] > stop_line:
            exit_idx, exit_price, exit_reason = j, float(stop_line), s_type
            break
        if is_long and highs[j] >= tp_price:
            exit_idx, exit_price, exit_reason = j, float(tp_price), "固定止盈"
            break
        if not is_long and lows[j] <= tp_price:
            exit_idx, exit_price, exit_reason = j, float(tp_price), "固定止盈"
            break

    if exit_idx is None:
        exit_idx = n - 1
        exit_price = float(closes[-1])
        exit_reason = "持有至末端"

    if is_long:
        r_value = (exit_price - base_price) / atr_val
        final_return = (exit_price - base_price) / base_price
    else:
        r_value = (base_price - exit_price) / atr_val
        final_return = (base_price - exit_price) / base_price

    return {
        "direction": d,
        "signal_idx": i,
        "signal_time": times[i],
        "exit_idx": exit_idx,
        "exit_time": times[exit_idx],
        "base_price": base_price,
        "r_value": r_value,
        "final_return": final_return,
        "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
        "atr_pct": atr_val / base_price,
        "exit_reason": exit_reason,
        "hold_bars": exit_idx - i,
        "year": int(years[i]),
    }


def run_backtest(use_callback=False):
    """运行回测。use_callback=False 用6K极值，True 用回调突破线（找不到则跳过）。"""
    results = []
    last_exit_idx = -1
    skipped_no_cb = 0
    skipped_overlap = 0
    for s in signals:
        if s.idx <= last_exit_idx:
            skipped_overlap += 1
            continue
        av = atr_vals[s.idx - 1] if s.idx - 1 >= 0 else np.nan
        if use_callback:
            cb, _ = find_callback_breakthrough(s.idx, s.direction, cfg.window)
            if cb is None:
                skipped_no_cb += 1
                continue
            base_price = cb
        else:
            base_price = s.breakthrough
        r = simulate(s, base_price, av)
        if r:
            r["bt_6k"] = s.breakthrough
            r["cb_offset_pct"] = (s.breakthrough - base_price) / s.breakthrough if s.direction == "long" else (base_price - s.breakthrough) / s.breakthrough
            results.append(r)
            last_exit_idx = r["exit_idx"]
    return results, skipped_no_cb, skipped_overlap


print("=" * 110)
print("回调突破线对比回测：6K极值基准 vs 回调突破线基准")
print("=" * 110)
print(f"数据范围：{df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]}，共 {n} 根4H K线")
print(f"原始信号数：{len(signals)}")
print(f"策略逻辑：1ATR初始止损 → 第3K后KAMA跟踪保本 → 做多22%/做空12%固定止盈 → 止损优先 → 信号合并")
print(f"回调突破线：6K区间内从左到右第一个被2K不创新高/低确认的极值点；找不到则跳过该信号")
print()

base_results, _, base_overlap = run_backtest(use_callback=False)
cb_results, cb_no_cb, cb_overlap = run_backtest(use_callback=True)

base_df = pd.DataFrame(base_results)
cb_df = pd.DataFrame(cb_results)


def print_stats(label, rdf, skipped_overlap, skipped_no_cb=0):
    print(f"\n{'─' * 90}")
    print(f"【{label}】共 {len(rdf)} 单（跳过重叠子信号 {skipped_overlap}" +
          (f"，跳过无回调突破线 {skipped_no_cb}" if skipped_no_cb else "") + "）")
    print(f"{'─' * 90}")
    if rdf.empty:
        print("  无数据")
        return
    sr = rdf["r_value"]
    w = sr[sr > 0]
    l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    print(f"  累计R={sr.sum():>8.1f}  均R={sr.mean():>6.2f}  胜率={(sr > 0).mean():>5.0%}  累计收益={rdf['final_return'].sum():>8.1%}  PF={pf_str}")
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = rdf[rdf["direction"] == d]
        if sub.empty:
            continue
        sr2 = sub["r_value"]
        print(f"    {lbl:>4} {len(sub):>5}单 累计R={sr2.sum():>7.1f} 均R={sr2.mean():>6.2f} 胜率={(sr2 > 0).mean():>5.0%} 收益={sub['final_return'].sum():>8.1%}")
    vc = rdf["exit_reason"].value_counts()
    print(f"  出场方式: ", end="")
    for reason, cnt in vc.items():
        print(f"{reason}={cnt}({cnt / len(rdf):.0%}) ", end="")
    print()
    print(f"  MFE分布: ", end="")
    for lo, hi in [(0, 0.02), (0.02, 0.06), (0.06, 0.10), (0.10, 0.22), (0.22, 1)]:
        c = ((rdf["mfe"] >= lo) & (rdf["mfe"] < hi)).sum()
        print(f"[{lo:.0%},{hi:.0%})={c}({c / len(rdf):.0%}) ", end="")
    print()
    print(f"  持仓K线: 中位={rdf['hold_bars'].median():.0f} 均值={rdf['hold_bars'].mean():.1f} 范围=[{rdf['hold_bars'].min()},{rdf['hold_bars'].max()}]")
    print(f"  ATR%: 中位={rdf['atr_pct'].median():.2%} 均值={rdf['atr_pct'].mean():.2%}")


print_stats("基准：6K极值突破线", base_df, base_overlap)
print_stats("回调突破线版", cb_df, cb_overlap, cb_no_cb)

# 回调突破线偏移统计
print(f"\n{'=' * 110}")
print("回调突破线偏移统计（回调版，相对6K极值）")
print(f"{'=' * 110}")
if not cb_df.empty:
    off = cb_df["cb_offset_pct"]
    print(f"  回调突破线 vs 6K极值偏移: 中位={off.median():.2%} 均值={off.mean():.2%} 范围=[{off.min():.2%}, {off.max():.2%}]")
    print(f"  → 回调突破线比6K极值平均{'低' if off.mean() > 0 else '高'}{abs(off.mean()):.2%}（做多方向）")
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = cb_df[cb_df["direction"] == d]
        if sub.empty:
            continue
        o2 = sub["cb_offset_pct"]
        print(f"    {lbl}: 偏移中位={o2.median():.2%} 均值={o2.mean():.2%}")

# 年度分解对比
print(f"\n{'=' * 110}")
print("年度分解对比")
print(f"{'=' * 110}")
all_years = sorted(set(base_df["year"].tolist() + cb_df["year"].tolist()))
print(f"\n{'年份':>6}  {'基准累计R':>10} {'基准单数':>8}  {'回调累计R':>10} {'回调单数':>8}  {'差R':>8}")
print("-" * 70)
for yr in all_years:
    b = base_df[base_df["year"] == yr]
    c = cb_df[cb_df["year"] == yr]
    br = b["r_value"].sum() if not b.empty else 0
    cr = c["r_value"].sum() if not c.empty else 0
    print(f"{yr:>6}  {br:>10.1f} {len(b):>8}  {cr:>10.1f} {len(c):>8}  {cr - br:>+8.1f}")
print(f"{'合计':>6}  {base_df['r_value'].sum():>10.1f} {len(base_df):>8}  {cb_df['r_value'].sum():>10.1f} {len(cb_df):>8}  {cb_df['r_value'].sum() - base_df['r_value'].sum():>+8.1f}")

# R值分布对比
print(f"\n{'=' * 110}")
print("R值分布对比")
print(f"{'=' * 110}")
print(f"\n{'R区间':>12}  {'基准单数':>8} {'基准累计R':>10}  {'回调单数':>8} {'回调累计R':>10}")
print("-" * 60)
for lo, hi in [(-2, -1), (-1, 0), (0, 1), (1, 2), (2, 5), (5, 100)]:
    b = base_df[(base_df["r_value"] >= lo) & (base_df["r_value"] < hi)]
    c = cb_df[(cb_df["r_value"] >= lo) & (cb_df["r_value"] < hi)]
    print(f"[{lo}, {hi}):  {len(b):>8} {b['r_value'].sum():>10.1f}  {len(c):>8} {c['r_value'].sum():>10.1f}")

# 汇总对比表
print(f"\n{'=' * 110}")
print("汇总对比")
print(f"{'=' * 110}")
print(f"\n{'指标':>20}  {'6K极值基准':>14}  {'回调突破线':>14}  {'差异':>12}")
print("-" * 70)
rows = [
    ("单数", len(base_df), len(cb_df), len(cb_df) - len(base_df), "d"),
    ("累计R", base_df["r_value"].sum(), cb_df["r_value"].sum(), cb_df["r_value"].sum() - base_df["r_value"].sum(), ".1f"),
    ("均R", base_df["r_value"].mean(), cb_df["r_value"].mean(), cb_df["r_value"].mean() - base_df["r_value"].mean(), ".2f"),
    ("胜率", (base_df["r_value"] > 0).mean(), (cb_df["r_value"] > 0).mean(), (cb_df["r_value"] > 0).mean() - (base_df["r_value"] > 0).mean(), ".1%"),
    ("累计收益", base_df["final_return"].sum(), cb_df["final_return"].sum(), cb_df["final_return"].sum() - base_df["final_return"].sum(), ".1%"),
    ("MFE中位", base_df["mfe"].median(), cb_df["mfe"].median(), cb_df["mfe"].median() - base_df["mfe"].median(), ".1%"),
    ("持仓中位(K)", base_df["hold_bars"].median(), cb_df["hold_bars"].median(), cb_df["hold_bars"].median() - base_df["hold_bars"].median(), ".0f"),
]
for label, bv, cv, diff, fmt in rows:
    if fmt == "d":
        print(f"{label:>20}  {bv:>14d}  {cv:>14d}  {diff:>+12d}")
    else:
        print(f"{label:>20}  {bv:>14{fmt}}  {cv:>14{fmt}}  {diff:>+12{fmt}}")

# 导出CSV（中文列名）
csv_path = cfg.results_path / "回调突破线对比.csv"
export_df = cb_df.copy()
export_df = export_df.rename(columns={
    "direction": "方向", "signal_time": "信号时间", "exit_time": "出场时间",
    "base_price": "回调突破线", "bt_6k": "6K极值", "cb_offset_pct": "回调偏移",
    "r_value": "R值", "final_return": "收益率", "mfe": "MFE",
    "atr_pct": "ATR占比", "exit_reason": "出场原因", "hold_bars": "持仓K线数", "year": "年份",
})
export_df["方向"] = export_df["方向"].map({"long": "做多", "short": "做空"})
export_cols = ["方向", "信号时间", "出场时间", "6K极值", "回调突破线", "回调偏移",
               "R值", "收益率", "MFE", "ATR占比", "出场原因", "持仓K线数", "年份"]
export_df[export_cols].to_csv(csv_path, index=False, encoding="utf-8-sig")
print(f"\n回调版明细已导出: {csv_path}")
print(f"{'=' * 110}")
