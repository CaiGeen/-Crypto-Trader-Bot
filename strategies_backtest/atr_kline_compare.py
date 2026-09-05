"""ATR取值K线对比：突破K前一根 vs 回调基准点 vs 6K极值点。

策略逻辑与 export_optimal_csv.py 相同（回调基准+0.5ATR确认+0.3ATR止损+1.5ATR加仓+22%/12%止盈）。
唯一变量：ATR取哪根K线的值。
  方案1 atr_break：atr_vals[i-1]（当前，突破K前一根）
  方案2 atr_cb：    atr_vals[cb_idx]（回调基准点那根K线，无回调则回退6K极值点）
  方案3 atr_ext：   atr_vals[ext_idx]（6K极值点那根K线）
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


def find_extreme_idx(i, direction, window=6):
    """找6K极值点的索引。"""
    start = max(i - window, 0)
    end = i
    if direction == "long":
        seg = highs[start:end]
        return start + int(np.argmax(seg))
    else:
        seg = lows[start:end]
        return start + int(np.argmin(seg))


def simulate(signal, atr_mode="break"):
    """回测单波段。atr_mode: 'break'=突破K前一根, 'cb'=回调基准点, 'ext'=6K极值点。"""
    i = signal.idx
    if i + 1 >= n:
        return None
    bt_6k = signal.breakthrough
    d = signal.direction
    is_long = (d == "long")

    cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
    has_cb = cb_extreme is not None
    entry_base = cb_extreme if has_cb else bt_6k
    ext_idx = find_extreme_idx(i, d, cfg.window)

    # ATR取值
    if atr_mode == "break":
        atr_idx = i - 1
    elif atr_mode == "cb":
        atr_idx = cb_idx if has_cb else ext_idx
    elif atr_mode == "ext":
        atr_idx = ext_idx

    if atr_idx < 0 or atr_idx >= n:
        return None
    av = atr_vals[atr_idx]
    if np.isnan(av) or av <= 0:
        return None

    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5 * av) if is_long else (closes[i] <= entry_base - 0.5 * av)

    if not confirmed:
        ep = float(closes[i])
        r1 = (ep - entry_base) / av if is_long else (entry_base - ep) / av
        return {"dir": d, "exit_idx": i, "filled": filled, "r": r1, "hold": 0,
                "exit_reason": "弱突破平仓", "atr": av, "atr_idx": atr_idx,
                "atr_pct": av / entry_base, "year": int(years[i])}

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
            "hold": exit_idx - i, "exit_reason": exit_reason, "atr": av, "atr_idx": atr_idx,
            "atr_pct": av / entry_base, "year": int(years[i]), "addon": addon_done}


def run(atr_mode):
    results = []
    last_exit = -1
    for s in signals:
        if s.idx <= last_exit:
            continue
        r = simulate(s, atr_mode)
        if r:
            results.append(r)
            last_exit = r["exit_idx"]
    return pd.DataFrame(results)


# 先分析ATR值差异
print("=" * 110)
print("ATR取值K线对比：突破K前一根 vs 回调基准点 vs 6K极值点")
print("=" * 110)

atr_diffs = []
for s in signals:
    i = s.idx
    cb_extreme, cb_idx = find_callback_extreme(i, s.direction, cfg.window)
    ext_idx = find_extreme_idx(i, s.direction, cfg.window)
    atr_break = atr_vals[i - 1] if i - 1 >= 0 else np.nan
    atr_cb = atr_vals[cb_idx] if cb_idx is not None and 0 <= cb_idx < n else np.nan
    atr_ext = atr_vals[ext_idx] if 0 <= ext_idx < n else np.nan
    if not any(np.isnan([atr_break, atr_ext])):
        atr_diffs.append({
            "atr_break": atr_break,
            "atr_cb": atr_cb if not np.isnan(atr_cb) else atr_ext,
            "atr_ext": atr_ext,
            "has_cb": cb_idx is not None,
            "dist_cb": (i - 1 - cb_idx) if cb_idx is not None else None,
            "dist_ext": (i - 1 - ext_idx),
        })

adf = pd.DataFrame(atr_diffs)
print(f"\n—— ATR值差异分析（{len(adf)} 个信号）——")
print(f"  atr_break（突破K前一根）: 中位={adf['atr_break'].median():.1f} 均值={adf['atr_break'].mean():.1f}")
print(f"  atr_cb（回调基准点）:     中位={adf['atr_cb'].median():.1f} 均值={adf['atr_cb'].mean():.1f}")
print(f"  atr_ext（6K极值点）:     中位={adf['atr_ext'].median():.1f} 均值={adf['atr_ext'].mean():.1f}")
print(f"\n  相对atr_break的差异:")
print(f"    atr_cb/atr_break:  中位={(adf['atr_cb']/adf['atr_break']).median():.3f} 均值={(adf['atr_cb']/adf['atr_break']).mean():.3f}")
print(f"    atr_ext/atr_break: 中位={(adf['atr_ext']/adf['atr_break']).median():.3f} 均值={(adf['atr_ext']/adf['atr_break']).mean():.3f}")

print(f"\n  回调基准点距突破K前一根的距离（K线数）:")
dist_cb = adf[adf["has_cb"]]["dist_cb"]
print(f"    中位={dist_cb.median():.0f} 均值={dist_cb.mean():.1f} 范围=[{dist_cb.min()},{dist_cb.max()}]")
print(f"  6K极值点距突破K前一根的距离:")
print(f"    中位={adf['dist_ext'].median():.0f} 均值={adf['dist_ext'].mean():.1f} 范围=[{adf['dist_ext'].min()},{adf['dist_ext'].max()}]")

# 三种方案回测对比
print(f"\n{'=' * 110}")
print("三种ATR取值方案回测对比（最优配置：回调基准+0.5ATR确认+0.3ATR止损+1.5ATR加仓）")
print(f"{'=' * 110}")

modes = [
    ("atr_break（突破K前一根）", "break"),
    ("atr_cb（回调基准点）", "cb"),
    ("atr_ext（6K极值点）", "ext"),
]

results = {}
for label, mode in modes:
    results[label] = run(mode)

print(f"\n{'方案':>28} {'总单':>6} {'成交':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'累计收益':>10} {'前3K止损':>8} {'止盈':>6}")
print("-" * 95)
for label, _ in modes:
    rdf = results[label]
    filled = rdf[rdf["filled"]]
    sr = filled["r"]
    pre3k = filled[filled["exit_reason"] == "前3K止损"]
    tp = filled[filled["exit_reason"] == "止盈"]
    print(f"{label:>28} {len(rdf):>6} {len(filled):>6} {sr.sum():>8.1f} {sr.mean():>6.2f} "
          f"{(sr > 0).mean():>5.0%} {sr.sum():>10.1f}R {len(pre3k):>8} {len(tp):>6}")

# 多空分开
print(f"\n{'方案':>28}  {'做多累计R':>10} {'做多均R':>8} {'做空累计R':>10} {'做空均R':>8}")
print("-" * 75)
for label, _ in modes:
    filled = results[label][results[label]["filled"]]
    for d, col in [("long", "做多"), ("short", "做空")]:
        sub = filled[filled["dir"] == d]
        if d == "long":
            lr, lm = sub["r"].sum(), sub["r"].mean() if not sub.empty else 0
        else:
            sr2, sm = sub["r"].sum(), sub["r"].mean() if not sub.empty else 0
    print(f"{label:>28}  {lr:>10.1f} {lm:>8.2f} {sr2:>10.1f} {sm:>8.2f}")

# 年度分解
print(f"\n{'=' * 110}")
print("年度分解对比")
print(f"{'=' * 110}")
print(f"\n{'年份':>6}", end="")
for label, _ in modes:
    print(f"  {label:>26}", end="")
print()
print("-" * 90)
all_years = sorted(set().union(*[set(r[r["filled"]]["year"]) for r in results.values()]))
for yr in all_years:
    print(f"{yr:>6}", end="")
    for label, _ in modes:
        filled = results[label][results[label]["filled"]]
        val = filled[filled["year"] == yr]["r"].sum()
        print(f"  {val:>26.1f}", end="")
    print()
print(f"{'合计':>6}", end="")
for label, _ in modes:
    filled = results[label][results[label]["filled"]]
    print(f"  {filled['r'].sum():>26.1f}", end="")
print()

# ATR%分布对比
print(f"\n{'=' * 110}")
print("ATR%分布对比（成交单）")
print(f"{'=' * 110}")
print(f"\n{'方案':>28} {'ATR%中位':>10} {'ATR%均值':>10} {'ATR%范围':>20}")
print("-" * 75)
for label, _ in modes:
    filled = results[label][results[label]["filled"]]
    ap = filled["atr_pct"]
    print(f"{label:>28} {ap.median():>10.2%} {ap.mean():>10.2%} [{ap.min():.2%}, {ap.max():.2%}]")

print(f"\n{'=' * 110}")
