"""前3K涨幅重算：回调基准起点 vs 6K极值起点。

策略逻辑与 export_optimal_csv.py 完全相同（回调基准+0.5ATR确认+0.3ATR止损+1.5ATR加仓）。
对每个成交+确认的单，分别用两种起点计算前3K涨幅，对比预测效果。
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


def simulate(signal):
    """完整回测，返回成交+确认单的 first3_gain（两种起点）和最终MFE/R。"""
    i = signal.idx
    if i + 1 >= n:
        return None
    bt_6k = signal.breakthrough
    d = signal.direction
    is_long = (d == "long")
    av = atr_vals[i - 1] if i - 1 >= 0 else np.nan
    if np.isnan(av) or av <= 0:
        return None

    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    has_cb = cb_extreme is not None
    entry_base = cb_extreme if has_cb else bt_6k

    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5 * av) if is_long else (closes[i] <= entry_base - 0.5 * av)

    if not confirmed:
        return None  # 弱突破平仓，无前3K涨幅

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

    # 前3K涨幅（两种起点），截断到止损前
    first3 = min(i + 4, exit_idx + 1)
    if first3 <= i + 1:
        first3_gain_6k = 0.0
        first3_gain_cb = 0.0
    else:
        if is_long:
            peak3 = float(highs[i + 1:first3].max())
            first3_gain_6k = (peak3 - bt_6k) / bt_6k
            first3_gain_cb = (peak3 - entry_base) / entry_base
        else:
            trough3 = float(lows[i + 1:first3].min())
            first3_gain_6k = (bt_6k - trough3) / bt_6k
            first3_gain_cb = (entry_base - trough3) / entry_base

    # 最终MFE（从回调基准算）
    if is_long:
        mfe = float(highs[i + 1:exit_idx + 1].max())
        mfe_pct = (mfe - entry_base) / entry_base
    else:
        mfe = float(lows[i + 1:exit_idx + 1].min())
        mfe_pct = (entry_base - mfe) / entry_base

    r1 = (exit_price - entry_base) / av if is_long else (entry_base - exit_price) / av
    r2 = ((exit_price - addon_price) / av if is_long else (addon_price - exit_price) / av) if addon_done else 0.0

    return {
        "dir": d, "year": int(years[i]), "filled": filled, "has_cb": has_cb,
        "bt_6k": bt_6k, "entry_base": entry_base,
        "cb_offset_pct": (bt_6k - entry_base) / bt_6k if is_long else (entry_base - bt_6k) / bt_6k,
        "first3_gain_6k": first3_gain_6k, "first3_gain_cb": first3_gain_cb,
        "mfe_pct": mfe_pct, "r_value": r1 + r2, "exit_reason": exit_reason,
        "hold": exit_idx - i, "atr_pct": av / entry_base,
    }


# 运行回测
results = []
last_exit = -1
for s in signals:
    if s.idx <= last_exit:
        continue
    r = simulate(s)
    if r:
        results.append(r)
        last_exit = s.idx  # 用信号idx合并（简化，和export_optimal_csv一致用exit_idx更准确但这里够了）

# 重新跑用exit_idx合并
results = []
last_exit = -1
for s in signals:
    if s.idx <= last_exit:
        continue
    r = simulate(s)
    if r:
        results.append(r)
        # 需要exit_idx来合并，重新算
        # 简化：用simulate内部已算exit_idx，但没返回。这里补上
        pass

# 补上exit_idx返回
def simulate2(signal):
    r = simulate(signal)
    return r

# 实际上simulate没返回exit_idx，重新写一个完整版
def simulate_full(signal):
    i = signal.idx
    if i + 1 >= n:
        return None
    bt_6k = signal.breakthrough
    d = signal.direction
    is_long = (d == "long")
    av = atr_vals[i - 1] if i - 1 >= 0 else np.nan
    if np.isnan(av) or av <= 0:
        return None
    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    has_cb = cb_extreme is not None
    entry_base = cb_extreme if has_cb else bt_6k
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5 * av) if is_long else (closes[i] <= entry_base - 0.5 * av)
    if not confirmed:
        i_exit = i
        ep = float(closes[i])
        r1 = (ep - entry_base) / av if is_long else (entry_base - ep) / av
        return {"dir": d, "exit_idx": i_exit, "filled": filled, "has_cb": has_cb,
                "bt_6k": bt_6k, "entry_base": entry_base, "confirmed": False,
                "first3_gain_6k": None, "first3_gain_cb": None,
                "mfe_pct": 0.0, "r_value": r1, "exit_reason": "弱突破平仓", "hold": 0}
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
    first3 = min(i + 4, exit_idx + 1)
    if first3 <= i + 1:
        first3_gain_6k = 0.0
        first3_gain_cb = 0.0
    else:
        if is_long:
            peak3 = float(highs[i + 1:first3].max())
            first3_gain_6k = (peak3 - bt_6k) / bt_6k
            first3_gain_cb = (peak3 - entry_base) / entry_base
        else:
            trough3 = float(lows[i + 1:first3].min())
            first3_gain_6k = (bt_6k - trough3) / bt_6k
            first3_gain_cb = (entry_base - trough3) / bt_6k
    if is_long:
        mfe = float(highs[i + 1:exit_idx + 1].max())
        mfe_pct = (mfe - entry_base) / entry_base
    else:
        mfe = float(lows[i + 1:exit_idx + 1].min())
        mfe_pct = (entry_base - mfe) / entry_base
    r1 = (exit_price - entry_base) / av if is_long else (entry_base - exit_price) / av
    r2 = ((exit_price - addon_price) / av if is_long else (addon_price - exit_price) / av) if addon_done else 0.0
    cb_offset = (bt_6k - entry_base) / bt_6k if is_long else (entry_base - bt_6k) / bt_6k
    return {"dir": d, "exit_idx": exit_idx, "filled": filled, "has_cb": has_cb,
            "bt_6k": bt_6k, "entry_base": entry_base, "confirmed": True,
            "cb_offset_pct": cb_offset,
            "first3_gain_6k": first3_gain_6k, "first3_gain_cb": first3_gain_cb,
            "mfe_pct": mfe_pct, "r_value": r1 + r2, "exit_reason": exit_reason,
            "hold": exit_idx - i, "atr_pct": av / entry_base, "year": int(years[i])}

results = []
last_exit = -1
for s in signals:
    if s.idx <= last_exit:
        continue
    r = simulate_full(s)
    if r:
        results.append(r)
        last_exit = r["exit_idx"]

rdf = pd.DataFrame(results)
filled = rdf[rdf["filled"]].copy()
confirmed = filled[filled["confirmed"]].copy()

print("=" * 110)
print("前3K涨幅重算：回调基准起点 vs 6K极值起点")
print("=" * 110)
print(f"总单数: {len(rdf)}，成交: {len(filled)}，成交+确认: {len(confirmed)}")
print(f"策略：回调基准+0.5ATR确认+0.3ATR止损+1.5ATR加仓+22%/12%止盈")
print()

# 1. 两种起点的前3K涨幅分布对比
print(f"{'━' * 110}")
print("1. 前3K涨幅分布对比（成交+确认单）")
print(f"{'━' * 110}")
print(f"{'指标':>15} {'6K极值起点':>14} {'回调基准起点':>14} {'差异':>10}")
print("-" * 58)
for label, q in [("中位", 0.5), ("均值", None), ("P25", 0.25), ("P75", 0.75)]:
    if q is None:
        v6 = confirmed["first3_gain_6k"].mean()
        vc = confirmed["first3_gain_cb"].mean()
    else:
        v6 = confirmed["first3_gain_6k"].quantile(q)
        vc = confirmed["first3_gain_cb"].quantile(q)
    print(f"{label:>15} {v6:>14.1%} {vc:>14.1%} {vc - v6:>+10.1%}")

# 2. 分档对比：前3K涨幅 vs 最终MFE
print(f"\n{'━' * 110}")
print("2. 前3K涨幅分档 vs 最终MFE（回调基准起点）— ≥8%预测效果")
print(f"{'━' * 110}")
print(f"{'前3K涨幅(回调基准)':>20} {'单数':>6} {'MFE中位':>8} {'MFE均值':>8} {'≥12%概率':>10} {'≥10%概率':>10} {'均R':>6} {'胜率':>6}")
print("-" * 85)
for lo, hi in [(0, 0.02), (0.02, 0.04), (0.04, 0.06), (0.06, 0.08), (0.08, 1)]:
    sub = confirmed[(confirmed["first3_gain_cb"] >= lo) & (confirmed["first3_gain_cb"] < hi)]
    if sub.empty:
        continue
    m = sub["mfe_pct"]
    print(f"[{lo:.0%}, {hi:.0%}){'':>10} {len(sub):>6} {m.median():>8.1%} {m.mean():>8.1%} "
          f"{(m >= 0.12).mean():>10.0%} {(m >= 0.10).mean():>10.0%} {sub['r_value'].mean():>6.2f} {(sub['r_value'] > 0).mean():>5.0%}")

# 3. 同一分档，两种起点对比
print(f"\n{'━' * 110}")
print("3. '前3K涨幅≥8%' 两种起点对比")
print(f"{'━' * 110}")
for label, col in [("6K极值起点", "first3_gain_6k"), ("回调基准起点", "first3_gain_cb")]:
    sub = confirmed[confirmed[col] >= 0.08]
    m = sub["mfe_pct"]
    print(f"\n  {label} ≥8%: {len(sub)} 单")
    print(f"    MFE中位={m.median():.1%}  ≥12%概率={(m >= 0.12).mean():.0%}  ≥10%概率={(m >= 0.10).mean():.0%}  均R={sub['r_value'].mean():.2f}  胜率={(sub['r_value'] > 0).mean():.0%}")
    for d2, lbl2 in [("long", "做多"), ("short", "做空")]:
        sub2 = sub[sub["dir"] == d2]
        if sub2.empty:
            continue
        m2 = sub2["mfe_pct"]
        print(f"      {lbl2}: {len(sub2)}单 MFE中位={m2.median():.1%} ≥12%概率={(m2 >= 0.12).mean():.0%} 均R={sub2['r_value'].mean():.2f}")

# 4. 差异单：6K极值<8%但回调基准≥8%
print(f"\n{'━' * 110}")
print("4. 因起点不同被重新分类的单")
print(f"{'━' * 110}")
diff = confirmed[(confirmed["first3_gain_6k"] < 0.08) & (confirmed["first3_gain_cb"] >= 0.08)]
same_8 = confirmed[(confirmed["first3_gain_6k"] >= 0.08) & (confirmed["first3_gain_cb"] >= 0.08)]
print(f"\n  6K极值<8% 但 回调基准≥8%: {len(diff)} 单（被回调基准重新纳入'强确定性'）")
if not diff.empty:
    m = diff["mfe_pct"]
    print(f"    MFE中位={m.median():.1%}  ≥12%概率={(m >= 0.12).mean():.0%}  ≥10%概率={(m >= 0.10).mean():.0%}  均R={diff['r_value'].mean():.2f}  胜率={(diff['r_value'] > 0).mean():.0%}")
    print(f"    → 这些单用6K极值算<8%会被漏掉，但实际表现：均R={diff['r_value'].mean():.2f}")
print(f"\n  两种起点都≥8%: {len(same_8)} 单")
if not same_8.empty:
    m = same_8["mfe_pct"]
    print(f"    MFE中位={m.median():.1%}  ≥12%概率={(m >= 0.12).mean():.0%}  均R={same_8['r_value'].mean():.2f}")

# 5. 回调基准偏移 vs first3_gain差异
print(f"\n{'━' * 110}")
print("5. 回调基准偏移越大，两种起点差异越大")
print(f"{'━' * 110}")
print(f"{'回调偏移':>12} {'单数':>6} {'6K起点中位':>10} {'回调起点中位':>12} {'差异中位':>10}")
print("-" * 55)
for lo, hi in [(0, 0.002), (0.002, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 1)]:
    sub = confirmed[(confirmed["cb_offset_pct"] >= lo) & (confirmed["cb_offset_pct"] < hi)]
    if sub.empty:
        continue
    d6 = sub["first3_gain_6k"].median()
    dc = sub["first3_gain_cb"].median()
    print(f"[{lo:.1%},{hi:.1%}){'':>2} {len(sub):>6} {d6:>10.1%} {dc:>12.1%} {dc - d6:>+10.1%}")

print(f"\n{'=' * 110}")
