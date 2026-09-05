"""双箱体抬升挂单入场策略回测（修正版）。

关键修正：跌破KAMA不是取消信号，而是重置强势箱体b的等待。
基准箱体a（6K突破）始终有效，直到连续3根收盘跌破突破线才取消。

状态机：
  WAITING_BOX_B → PENDING_ORDER → IN_POSITION
  跌破KAMA / 箱体b范围过大 → 回到WAITING_BOX_B（不取消信号）
  连续3根收盘跌破突破线 → 取消信号
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

TP_LONG = 0.22
TP_SHORT = 0.12


def simulate_dual_box(signal):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough
    d = signal.direction
    is_long = (d == "long")

    # 初始化强势箱体b等待
    running_extreme = highs[i] if is_long else lows[i]
    peak_idx = i
    counter = 0
    below_count = 0
    state = "WAITING_BOX_B"

    entry_price = stop_loss = atr_ref = box_b_range = None
    box_b_end = None
    reset_count = 0  # 重置次数统计

    for j in range(i + 1, n):
        ks = kama_vals[j - 1]
        if np.isnan(ks): continue

        # 全局取消条件：连续3根收盘在突破线反向
        if is_long:
            if closes[j] < bt: below_count += 1
            else: below_count = 0
        else:
            if closes[j] > bt: below_count += 1
            else: below_count = 0
        if below_count >= 3:
            return {"status": "cancelled", "cancel_reason": "break_back_3bars",
                    "signal_idx": i, "reset_count": reset_count}

        if state == "WAITING_BOX_B":
            # 跌破KAMA → 重置强势箱体b等待（不取消信号）
            kama_break = (is_long and lows[j] < ks) or (not is_long and highs[j] > ks)
            if kama_break:
                running_extreme = highs[j] if is_long else lows[j]
                peak_idx = j
                counter = 0
                reset_count += 1
                continue

            # 检查创新高/不创新高
            if is_long:
                if highs[j] > running_extreme:
                    running_extreme = highs[j]
                    peak_idx = j
                    counter = 0
                else:
                    counter += 1
            else:
                if lows[j] < running_extreme:
                    running_extreme = lows[j]
                    peak_idx = j
                    counter = 0
                else:
                    counter += 1

            if counter >= 2:
                atr_ref = atr_vals[peak_idx]
                if np.isnan(atr_ref) or atr_ref <= 0:
                    running_extreme = highs[j] if is_long else lows[j]
                    peak_idx = j
                    counter = 0
                    reset_count += 1
                    continue
                if is_long:
                    entry_price = highs[peak_idx] + atr_ref
                    stop_loss = highs[peak_idx]
                else:
                    entry_price = lows[peak_idx] - atr_ref
                    stop_loss = lows[peak_idx]
                box_b_range = max(highs[peak_idx:j + 1]) - min(lows[peak_idx:j + 1])
                box_b_end = j
                state = "PENDING_ORDER"

        elif state == "PENDING_ORDER":
            # 取消条件1：箱体b范围 > 3*ATR(当前K线) → 重置
            cur_atr = atr_vals[j]
            if not np.isnan(cur_atr) and cur_atr > 0 and box_b_range > 3 * cur_atr:
                running_extreme = highs[j] if is_long else lows[j]
                peak_idx = j
                counter = 0
                state = "WAITING_BOX_B"
                reset_count += 1
                continue

            # 取消条件2：跌破KAMA → 重置
            kama_break = (is_long and lows[j] < ks) or (not is_long and highs[j] > ks)
            if kama_break:
                running_extreme = highs[j] if is_long else lows[j]
                peak_idx = j
                counter = 0
                state = "WAITING_BOX_B"
                reset_count += 1
                continue

            # 检查成交
            if (is_long and highs[j] >= entry_price) or (not is_long and lows[j] <= entry_price):
                entry_idx = j
                state = "IN_POSITION"
                break

    if state != "IN_POSITION":
        return {"status": "cancelled", "cancel_reason": "no_fill_to_end",
                "signal_idx": i, "reset_count": reset_count}

    # ===== 阶段3：持仓中，止盈止损 =====
    tp_price = entry_price * (1 + TP_LONG) if is_long else entry_price * (1 - TP_SHORT)
    max_exc = -np.inf
    exit_idx = exit_price = exit_reason = None

    for j in range(entry_idx, n):
        ks = kama_vals[j - 1]
        if np.isnan(ks): continue

        if is_long:
            exc = (highs[j] - entry_price) / entry_price
        else:
            exc = (entry_price - lows[j]) / entry_price
        if exc > max_exc: max_exc = exc

        bh = j - entry_idx
        if bh <= 3:
            stop_line = stop_loss
            s_type = "init_stop"
        else:
            if is_long:
                stop_line = max(ks, entry_price)
                s_type = "breakeven" if ks < entry_price else "kama_trail"
            else:
                stop_line = min(ks, entry_price)
                s_type = "breakeven" if ks > entry_price else "kama_trail"

        if is_long and lows[j] < stop_line:
            exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if not is_long and highs[j] > stop_line:
            exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if is_long and highs[j] >= tp_price:
            exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if not is_long and lows[j] <= tp_price:
            exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break

    if exit_idx is None:
        exit_idx = n - 1; exit_price = float(closes[-1]); exit_reason = "end"

    if is_long:
        final_return = (exit_price - entry_price) / entry_price
        r_value = (exit_price - entry_price) / atr_ref
    else:
        final_return = (entry_price - exit_price) / entry_price
        r_value = (entry_price - exit_price) / atr_ref

    return {
        "status": "filled",
        "direction": d,
        "signal_idx": i,
        "entry_idx": entry_idx,
        "exit_idx": exit_idx,
        "peak_idx": peak_idx,
        "box_b_end": box_b_end,
        "breakthrough": bt,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "final_return": final_return,
        "r_value": r_value,
        "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
        "atr_ref": atr_ref,
        "atr_pct": atr_ref / bt,
        "bars_to_entry": entry_idx - i,
        "bars_in_position": exit_idx - entry_idx,
        "box_b_range_pct": box_b_range / bt,
        "reset_count": reset_count,
    }


# ===== 运行回测 =====
results = []; cancelled = []; last_exit_idx = None

for s in signals:
    if last_exit_idx is not None and s.idx <= last_exit_idx: continue
    r = simulate_dual_box(s)
    if r is None: continue
    if r["status"] == "cancelled":
        cancelled.append(r)
    else:
        results.append(r)
        last_exit_idx = r["exit_idx"]

rdf = pd.DataFrame(results)
cdf = pd.DataFrame(cancelled)

# ===== 输出统计 =====
print("=" * 100)
print("双箱体抬升挂单入场策略回测报告（修正版：跌破KAMA重置而非取消）")
print("=" * 100)

print(f"\n6K突破信号总数: {len(signals)}")
print(f"取消/未入场: {len(cdf)}")
print(f"成功入场: {len(rdf)}")

if not cdf.empty:
    print(f"\n—— 取消原因分布 ——")
    vc = cdf["cancel_reason"].value_counts()
    for reason, cnt in vc.items():
        print(f"  {reason:>20} {cnt:>5} ({cnt/len(cdf):.0%})")
    print(f"  取消前重置次数: 中位{cdf['reset_count'].median():.0f}, 均值{cdf['reset_count'].mean():.1f}")

if not rdf.empty:
    print(f"\n—— 入场统计 ——")
    print(f"  信号→入场等待K线数: 中位{rdf['bars_to_entry'].median():.0f}, 均值{rdf['bars_to_entry'].mean():.1f}")
    print(f"  强势箱体b范围占比: 中位{rdf['box_b_range_pct'].median():.1%}, 均值{rdf['box_b_range_pct'].mean():.1%}")
    print(f"  ATR%: 中位{rdf['atr_pct'].median():.1%}, 均值{rdf['atr_pct'].mean():.1%}")
    print(f"  入场前重置次数: 中位{rdf['reset_count'].median():.0f}, 均值{rdf['reset_count'].mean():.1f}")

    # 不同MFE过滤标准对比
    print(f"\n—— 不同MFE过滤标准对比 ——")
    print(f"{'MFE过滤':>10} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6} {'累计收益':>9}")
    print("-" * 55)
    for mfe_min in [0.0, 0.02, 0.03, 0.05, 0.08, 0.10]:
        ff = rdf[rdf["mfe"].abs() >= mfe_min]
        if ff.empty: continue
        sr = ff["r_value"]
        print(f"{'≥'+f'{mfe_min:.0%}':>10} {len(ff):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {ff['final_return'].sum():>9.1%}")

    # 全量统计
    sr_all = rdf["r_value"]
    print(f"\n—— 全量统计（{len(rdf)}单）——")
    print(f"  累计R: {sr_all.sum():.1f}, 均R: {sr_all.mean():.2f}, 胜率: {(sr_all>0).mean():.0%}, 累计收益: {rdf['final_return'].sum():.1%}")

    # 多空分开
    print(f"\n—— 多空分开（全量）——")
    print(f"{'方向':>6} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6} {'累计收益':>9}")
    print("-" * 50)
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = rdf[rdf["direction"] == d]
        if sub.empty: continue
        sr = sub["r_value"]
        print(f"{lbl:>6} {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {sub['final_return'].sum():>9.1%}")

    # 出场原因分布
    print(f"\n—— 出场原因分布 ——")
    vc = rdf["exit_reason"].value_counts()
    for reason, cnt in vc.items():
        sub = rdf[rdf["exit_reason"] == reason]
        print(f"  {reason:>15} {cnt:>5} ({cnt/len(rdf):.0%})  均R={sub['r_value'].mean():.2f}")

    # 年度统计
    print(f"\n—— 年度统计（全量）——")
    rdf_yr = rdf.copy()
    rdf_yr["year"] = pd.to_datetime([times[idx] for idx in rdf_yr["signal_idx"]]).year
    print(f"{'年份':>6} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6}")
    print("-" * 40)
    for yr in sorted(rdf_yr["year"].unique()):
        sub = rdf_yr[rdf_yr["year"] == yr]
        sr = sub["r_value"]
        print(f"{yr:>6} {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%}")

    # 与直接入场对比
    print(f"\n{'='*100}")
    print("与直接入场策略对比")
    print(f"{'='*100}")
    print(f"{'策略':>16} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6} {'累计收益':>9}")
    print("-" * 60)
    print(f"{'双箱体(全量)':>16} {len(rdf):>5} {sr_all.sum():>8.1f} {sr_all.mean():>7.2f} {(sr_all>0).mean():>6.0%} {rdf['final_return'].sum():>9.1%}")
    print(f"{'直接入场22%/12%':>16} {'99':>5} {'649.7':>8} {'6.56':>7} {'98%':>6} {'1058.5%':>9}")

print(f"\n{'='*100}")
