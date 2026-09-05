"""双箱体入场策略：事前特征分析，寻找可用的过滤条件。"""
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
highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
closes = df["close"].to_numpy(); times = df["open_time"].tolist()
n = len(df)


def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)
TP_LONG = 0.22; TP_SHORT = 0.12


def simulate_dual_box(signal):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    running_extreme = highs[i] if is_long else lows[i]
    peak_idx = i; counter = 0; below_count = 0
    state = "WAITING_BOX_B"
    entry_price = stop_loss = atr_ref = box_b_range = None
    box_b_end = None; reset_count = 0

    for j in range(i + 1, n):
        ks = kama_vals[j - 1]
        if np.isnan(ks): continue
        if is_long:
            if closes[j] < bt: below_count += 1
            else: below_count = 0
        else:
            if closes[j] > bt: below_count += 1
            else: below_count = 0
        if below_count >= 3:
            return None

        if state == "WAITING_BOX_B":
            kama_break = (is_long and lows[j] < ks) or (not is_long and highs[j] > ks)
            if kama_break:
                running_extreme = highs[j] if is_long else lows[j]
                peak_idx = j; counter = 0; reset_count += 1; continue
            if is_long:
                if highs[j] > running_extreme:
                    running_extreme = highs[j]; peak_idx = j; counter = 0
                else: counter += 1
            else:
                if lows[j] < running_extreme:
                    running_extreme = lows[j]; peak_idx = j; counter = 0
                else: counter += 1
            if counter >= 2:
                atr_ref = atr_vals[peak_idx]
                if np.isnan(atr_ref) or atr_ref <= 0:
                    running_extreme = highs[j] if is_long else lows[j]
                    peak_idx = j; counter = 0; reset_count += 1; continue
                if is_long:
                    entry_price = highs[peak_idx] + atr_ref; stop_loss = highs[peak_idx]
                else:
                    entry_price = lows[peak_idx] - atr_ref; stop_loss = lows[peak_idx]
                box_b_range = max(highs[peak_idx:j + 1]) - min(lows[peak_idx:j + 1])
                box_b_end = j; state = "PENDING_ORDER"
        elif state == "PENDING_ORDER":
            cur_atr = atr_vals[j]
            if not np.isnan(cur_atr) and cur_atr > 0 and box_b_range > 3 * cur_atr:
                running_extreme = highs[j] if is_long else lows[j]
                peak_idx = j; counter = 0; state = "WAITING_BOX_B"; reset_count += 1; continue
            kama_break = (is_long and lows[j] < ks) or (not is_long and highs[j] > ks)
            if kama_break:
                running_extreme = highs[j] if is_long else lows[j]
                peak_idx = j; counter = 0; state = "WAITING_BOX_B"; reset_count += 1; continue
            if (is_long and highs[j] >= entry_price) or (not is_long and lows[j] <= entry_price):
                entry_idx = j; state = "IN_POSITION"; break

    if state != "IN_POSITION": return None

    tp_price = entry_price * (1 + TP_LONG) if is_long else entry_price * (1 - TP_SHORT)
    max_exc = -np.inf; exit_idx = exit_price = exit_reason = None
    for j in range(entry_idx, n):
        ks = kama_vals[j - 1]
        if np.isnan(ks): continue
        if is_long: exc = (highs[j] - entry_price) / entry_price
        else: exc = (entry_price - lows[j]) / entry_price
        if exc > max_exc: max_exc = exc
        bh = j - entry_idx
        if bh <= 3: stop_line = stop_loss; s_type = "init_stop"
        else:
            if is_long:
                stop_line = max(ks, entry_price); s_type = "breakeven" if ks < entry_price else "kama_trail"
            else:
                stop_line = min(ks, entry_price); s_type = "breakeven" if ks > entry_price else "kama_trail"
        if is_long and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if not is_long and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n - 1; exit_price = float(closes[-1]); exit_reason = "end"

    if is_long:
        final_return = (exit_price - entry_price) / entry_price; r_value = (exit_price - entry_price) / atr_ref
    else:
        final_return = (entry_price - exit_price) / entry_price; r_value = (entry_price - exit_price) / atr_ref

    # 入场价相对突破线的偏移
    entry_premium = (entry_price - bt) / bt if is_long else (bt - entry_price) / bt

    return {
        "direction": d, "signal_idx": i, "entry_idx": entry_idx, "exit_idx": exit_idx,
        "r_value": r_value, "final_return": final_return,
        "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
        "atr_pct": atr_ref / bt, "entry_premium_pct": entry_premium,
        "bars_to_entry": entry_idx - i, "bars_in_position": exit_idx - entry_idx,
        "box_b_range_pct": box_b_range / bt, "reset_count": reset_count,
        "exit_reason": exit_reason,
    }


results = []; last_exit_idx = None
for s in signals:
    if last_exit_idx is not None and s.idx <= last_exit_idx: continue
    r = simulate_dual_box(s)
    if r: results.append(r); last_exit_idx = r["exit_idx"]

rdf = pd.DataFrame(results)
print(f"总入场: {len(rdf)}单, 累计R: {rdf['r_value'].sum():.1f}, 胜率: {(rdf['r_value']>0).mean():.0%}\n")

print("=" * 90)
print("事前特征 vs R值/胜率分析")
print("=" * 90)

# 1. ATR%
print(f"\n—— ATR%分组 ——")
print(f"{'ATR%':>10} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6} {'init_stop%':>11}")
print("-" * 60)
for lo, hi in [(0, 0.01), (0.01, 0.015), (0.015, 0.02), (0.02, 0.03), (0.03, 1)]:
    sub = rdf[(rdf["atr_pct"] >= lo) & (rdf["atr_pct"] < hi)]
    if sub.empty: continue
    sr = sub["r_value"]
    is_pct = (sub["exit_reason"] == "init_stop").mean()
    print(f"[{lo:.1%},{hi:.1%}) {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {is_pct:>11.0%}")

# 2. 入场价偏移
print(f"\n—— 入场价偏移（入场价 vs 突破线）——")
print(f"{'偏移%':>10} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6} {'init_stop%':>11}")
print("-" * 60)
for lo, hi in [(0, 0.02), (0.02, 0.04), (0.04, 0.06), (0.06, 0.08), (0.08, 0.12), (0.12, 1)]:
    sub = rdf[(rdf["entry_premium_pct"] >= lo) & (rdf["entry_premium_pct"] < hi)]
    if sub.empty: continue
    sr = sub["r_value"]
    is_pct = (sub["exit_reason"] == "init_stop").mean()
    print(f"[{lo:.1%},{hi:.1%}) {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {is_pct:>11.0%}")

# 3. 重置次数
print(f"\n—— 重置次数分组 ——")
print(f"{'重置次数':>10} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6}")
print("-" * 50)
for lo, hi in [(0, 1), (1, 3), (3, 5), (5, 10), (10, 1000)]:
    sub = rdf[(rdf["reset_count"] >= lo) & (rdf["reset_count"] < hi)]
    if sub.empty: continue
    sr = sub["r_value"]
    print(f"[{lo},{hi}) {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%}")

# 4. 等待K线数
print(f"\n—— 等待K线数分组 ——")
print(f"{'等待K线':>10} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6}")
print("-" * 50)
for lo, hi in [(0, 10), (10, 20), (20, 50), (50, 100), (100, 10000)]:
    sub = rdf[(rdf["bars_to_entry"] >= lo) & (rdf["bars_to_entry"] < hi)]
    if sub.empty: continue
    sr = sub["r_value"]
    print(f"[{lo},{hi}) {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%}")

# 5. 箱体b范围
print(f"\n—— 箱体b范围占比 ——")
print(f"{'范围%':>10} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6}")
print("-" * 50)
for lo, hi in [(0, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 0.08), (0.08, 1)]:
    sub = rdf[(rdf["box_b_range_pct"] >= lo) & (rdf["box_b_range_pct"] < hi)]
    if sub.empty: continue
    sr = sub["r_value"]
    print(f"[{lo:.1%},{hi:.1%}) {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%}")

# 6. 多空分开
print(f"\n—— 多空分开 ——")
print(f"{'方向':>6} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6} {'init_stop%':>11}")
print("-" * 55)
for d, lbl in [("long", "做多"), ("short", "做空")]:
    sub = rdf[rdf["direction"] == d]
    if sub.empty: continue
    sr = sub["r_value"]
    is_pct = (sub["exit_reason"] == "init_stop").mean()
    print(f"{lbl:>6} {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {is_pct:>11.0%}")

# 7. 组合过滤：入场偏移<4% + ATR<2%
print(f"\n—— 组合过滤测试 ——")
print(f"{'过滤条件':>30} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6}")
print("-" * 65)
combos = [
    ("入场偏移<4%", rdf[rdf["entry_premium_pct"] < 0.04]),
    ("入场偏移<6%", rdf[rdf["entry_premium_pct"] < 0.06]),
    ("ATR<2%", rdf[rdf["atr_pct"] < 0.02]),
    ("偏移<4% & ATR<2%", rdf[(rdf["entry_premium_pct"] < 0.04) & (rdf["atr_pct"] < 0.02)]),
    ("偏移<6% & ATR<2%", rdf[(rdf["entry_premium_pct"] < 0.06) & (rdf["atr_pct"] < 0.02)]),
    ("偏移<4% & 重置<5", rdf[(rdf["entry_premium_pct"] < 0.04) & (rdf["reset_count"] < 5)]),
    ("偏移<6% & 等待<20K", rdf[(rdf["entry_premium_pct"] < 0.06) & (rdf["bars_to_entry"] < 20)]),
    ("做多only", rdf[rdf["direction"] == "long"]),
    ("做多 & 偏移<6%", rdf[(rdf["direction"] == "long") & (rdf["entry_premium_pct"] < 0.06)]),
    ("做多 & 偏移<4% & ATR<2%", rdf[(rdf["direction"] == "long") & (rdf["entry_premium_pct"] < 0.04) & (rdf["atr_pct"] < 0.02)]),
]
for label, sub in combos:
    if sub.empty: continue
    sr = sub["r_value"]
    print(f"{label:>30} {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%}")

print(f"\n{'='*90}")
