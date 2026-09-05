"""ATR替代对比：当前ATR(20) vs BB Middle（ATR%的布林带中轨）。

BB Middle = SMA((ATR(20)/close)×100, 20)，是百分比值。
替代时转换为绝对值：atr_bb = BB_Middle[i-1] / 100 × entry_base
取值K线：突破K前一根（i-1），和当前ATR一致。
策略逻辑与 export_optimal_csv.py 相同（回调基准+0.5ATR确认+0.3ATR止损+1.5ATR加仓+22%/12%止盈）。
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
opens = df["open"].to_numpy()
n = len(df)
years = df["open_time"].dt.year.to_numpy()


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

# 计算BB Middle = SMA((ATR/close)*100, 20)
atr_pct_series = (atr_vals / closes) * 100
bb_middle = pd.Series(atr_pct_series).rolling(20).mean().to_numpy()

TP_LONG = 0.22
TP_SHORT = 0.12


def find_callback_extreme(i, direction, window=6):
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


def simulate(signal, use_bb=False):
    """回测单波段。use_bb=False用当前ATR，True用BB Middle替代。"""
    i = signal.idx
    if i + 1 >= n:
        return None
    bt_6k = signal.breakthrough
    d = signal.direction
    is_long = (d == "long")

    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    has_cb = cb_extreme is not None
    entry_base = cb_extreme if has_cb else bt_6k

    # ATR取值
    atr_idx = i - 1
    if atr_idx < 0:
        return None
    if use_bb:
        bm = bb_middle[atr_idx]
        if np.isnan(bm) or bm <= 0:
            return None
        av = bm / 100.0 * entry_base  # 百分比转绝对值
    else:
        av = atr_vals[atr_idx]
    if av <= 0:
        return None

    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5 * av) if is_long else (closes[i] <= entry_base - 0.5 * av)

    if not confirmed:
        ep = float(closes[i])
        r1 = (ep - entry_base) / av if is_long else (entry_base - ep) / av
        return {"dir": d, "exit_idx": i, "filled": filled, "r": r1, "hold": 0,
                "exit_reason": "弱突破平仓", "atr": av, "atr_pct": av / entry_base,
                "year": int(years[i])}

    if is_long:
        init_stop = entry_base - 0.3 * av
        tp_price = entry_base * (1 + TP_LONG)
    else:
        init_stop = entry_base + 0.3 * av
        tp_price = entry_base * (1 - TP_SHORT)

    addon_done = False
    addon_price = 0.0
    exit_idx = exit_price = exit_reason = None
    for j in range(i + 1, n):
        ks = kama_vals[j - 1]
        if np.isnan(ks):
            continue
        bh = j - i
        if bh <= 3:
            sl = init_stop
        else:
            sl = max(ks, entry_base) if is_long else min(ks, entry_base)
        if not addon_done:
            cg_abs = (closes[j] - entry_base) if is_long else (entry_base - closes[j])
            if cg_abs >= 1.5 * av:
                addon_done = True
                addon_price = float(closes[j])
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            exit_idx, exit_price, exit_reason = j, ep, "前3K止损" if bh <= 3 else "KAMA跟踪止损"
            break
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            exit_idx, exit_price, exit_reason = j, ep, "前3K止损" if bh <= 3 else "KAMA跟踪止损"
            break
        if is_long and highs[j] >= tp_price:
            exit_idx, exit_price, exit_reason = j, float(tp_price), "止盈"
            break
        if not is_long and lows[j] <= tp_price:
            exit_idx, exit_price, exit_reason = j, float(tp_price), "止盈"
            break
    if exit_idx is None:
        exit_idx = n - 1
        exit_price = float(closes[-1])
        exit_reason = "未平仓"

    r1 = (exit_price - entry_base) / av if is_long else (entry_base - exit_price) / av
    r2 = ((exit_price - addon_price) / av if is_long else (addon_price - exit_price) / av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "filled": filled, "r": r1 + r2,
            "hold": exit_idx - i, "exit_reason": exit_reason, "atr": av,
            "atr_pct": av / entry_base, "year": int(years[i]), "addon": addon_done}


def run(use_bb=False):
    results = []
    last_exit = -1
    for s in signals:
        if s.idx <= last_exit:
            continue
        r = simulate(s, use_bb)
        if r:
            results.append(r)
            last_exit = r["exit_idx"]
    return pd.DataFrame(results)


# ATR值差异分析
print("=" * 110)
print("ATR替代对比：当前ATR(20) vs BB Middle（ATR%的布林带中轨）")
print("=" * 110)
print(f"BB Middle = SMA((ATR(20)/close)×100, 20)，替代时转为绝对值：atr_bb = BB_Middle/100 × entry_base")
print(f"取值K线：突破K前一根（i-1），与当前ATR一致")
print()

# 逐信号对比ATR值
diffs = []
for s in signals:
    i = s.idx
    if i - 1 < 0 or i - 1 >= n:
        continue
    cb_extreme, _ = find_callback_extreme(i, s.direction, cfg.window)
    entry_base = cb_extreme if cb_extreme is not None else s.breakthrough
    av_cur = atr_vals[i - 1]
    bm = bb_middle[i - 1]
    if np.isnan(bm) or av_cur <= 0:
        continue
    av_bb = bm / 100.0 * entry_base
    diffs.append({"av_cur": av_cur, "av_bb": av_bb, "ratio": av_bb / av_cur,
                  "atr_pct_cur": av_cur / entry_base, "atr_pct_bb": av_bb / entry_base})

ddf = pd.DataFrame(diffs)
print(f"—— ATR值差异（{len(ddf)} 个信号）——")
print(f"  当前ATR:     中位={ddf['av_cur'].median():.1f} 均值={ddf['av_cur'].mean():.1f}")
print(f"  BB Middle替代: 中位={ddf['av_bb'].median():.1f} 均值={ddf['av_bb'].mean():.1f}")
print(f"  替代/当前比值:  中位={ddf['ratio'].median():.3f} 均值={ddf['ratio'].mean():.3f} 范围=[{ddf['ratio'].min():.3f}, {ddf['ratio'].max():.3f}]")
print(f"  → BB Middle比当前ATR平均{'高' if ddf['ratio'].mean() > 1 else '低'}{abs(ddf['ratio'].mean()-1):.1%}")
print(f"  → BB Middle是20根均值，更平滑；当前ATR是单根值，波动更大")

# 回测对比
print(f"\n{'=' * 110}")
print("回测对比（最优配置：回调基准+0.5ATR确认+0.3ATR止损+1.5ATR加仓+22%/12%止盈）")
print(f"{'=' * 110}")

rdf_cur = run(use_bb=False)
rdf_bb = run(use_bb=True)

modes = [("当前ATR(20)", rdf_cur), ("BB Middle替代", rdf_bb)]

print(f"\n{'方案':>16} {'总单':>6} {'成交':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'前3K止损':>8} {'KAMA跟踪':>8} {'止盈':>6} {'弱突破':>6}")
print("-" * 85)
for label, rdf in modes:
    filled = rdf[rdf["filled"]]
    sr = filled["r"]
    pre3k = filled[filled["exit_reason"] == "前3K止损"]
    kama_t = filled[filled["exit_reason"] == "KAMA跟踪止损"]
    tp = filled[filled["exit_reason"] == "止盈"]
    weak = filled[filled["exit_reason"] == "弱突破平仓"]
    print(f"{label:>16} {len(rdf):>6} {len(filled):>6} {sr.sum():>8.1f} {sr.mean():>6.2f} "
          f"{(sr > 0).mean():>5.0%} {len(pre3k):>8} {len(kama_t):>8} {len(tp):>6} {len(weak):>6}")

# 多空分开
print(f"\n{'方案':>16}  {'做多单':>6} {'做多累计R':>10} {'做多均R':>8}  {'做空单':>6} {'做空累计R':>10} {'做空均R':>8}")
print("-" * 75)
for label, rdf in modes:
    filled = rdf[rdf["filled"]]
    for d, col in [("long", "做多"), ("short", "做空")]:
        sub = filled[filled["dir"] == d]
        if d == "long":
            ln, lr, lm = len(sub), sub["r"].sum(), sub["r"].mean() if not sub.empty else 0
        else:
            sn, sr2, sm = len(sub), sub["r"].sum(), sub["r"].mean() if not sub.empty else 0
    print(f"{label:>16}  {ln:>6} {lr:>10.1f} {lm:>8.2f}  {sn:>6} {sr2:>10.1f} {sm:>8.2f}")

# 年度分解
print(f"\n{'=' * 110}")
print("年度分解对比")
print(f"{'=' * 110}")
print(f"\n{'年份':>6}  {'当前ATR累计R':>12} {'当前单数':>8}  {'BB Middle累计R':>14} {'BB单数':>8}  {'差R':>8}")
print("-" * 70)
all_years = sorted(set(rdf_cur["year"]).union(set(rdf_bb["year"])))
for yr in all_years:
    c = rdf_cur[(rdf_cur["filled"]) & (rdf_cur["year"] == yr)]
    b = rdf_bb[(rdf_bb["filled"]) & (rdf_bb["year"] == yr)]
    cr = c["r"].sum()
    br = b["r"].sum()
    print(f"{yr:>6}  {cr:>12.1f} {len(c):>8}  {br:>14.1f} {len(b):>8}  {br - cr:>+8.1f}")
tc = rdf_cur[rdf_cur["filled"]]["r"].sum()
tb = rdf_bb[rdf_bb["filled"]]["r"].sum()
print(f"{'合计':>6}  {tc:>12.1f} {len(rdf_cur[rdf_cur['filled']]):>8}  {tb:>14.1f} {len(rdf_bb[rdf_bb['filled']]):>8}  {tb - tc:>+8.1f}")

# ATR%分布对比
print(f"\n{'=' * 110}")
print("ATR%分布对比（成交单）")
print(f"{'=' * 110}")
print(f"\n{'方案':>16} {'ATR%中位':>10} {'ATR%均值':>10} {'ATR%标准差':>10} {'ATR%范围':>22}")
print("-" * 75)
for label, rdf in modes:
    filled = rdf[rdf["filled"]]
    ap = filled["atr_pct"]
    print(f"{label:>16} {ap.median():>10.2%} {ap.mean():>10.2%} {ap.std():>10.2%} [{ap.min():.2%}, {ap.max():.2%}]")

# R值分布对比
print(f"\n{'=' * 110}")
print("R值分布对比")
print(f"{'=' * 110}")
print(f"\n{'R区间':>12}  {'当前ATR单数':>10} {'当前累计R':>10}  {'BB Middle单数':>12} {'BB累计R':>10}")
print("-" * 62)
for lo, hi in [(-2, -1), (-1, 0), (0, 1), (1, 2), (2, 5), (5, 100)]:
    c = rdf_cur[(rdf_cur["filled"]) & (rdf_cur["r"] >= lo) & (rdf_cur["r"] < hi)]
    b = rdf_bb[(rdf_bb["filled"]) & (rdf_bb["r"] >= lo) & (rdf_bb["r"] < hi)]
    print(f"[{lo}, {hi}):  {len(c):>10} {c['r'].sum():>10.1f}  {len(b):>12} {b['r'].sum():>10.1f}")

print(f"\n{'=' * 110}")
