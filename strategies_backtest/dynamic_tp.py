"""动态止盈策略回测：基于前3K涨幅 + 6%后连跌信号自适应调整止盈。"""
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


def simulate(signal, strategy="fixed"):
    """动态止盈回测。

    strategy:
      fixed    — 做多22%/做空12%固定
      dyn_v1   — 前3K涨<8%降止盈到12%/8%
      dyn_v2   — dyn_v1 + 到6%后连跌≥2根收紧到8%/6%
      dyn_v3   — fixed + 到6%后连跌≥2根收紧到8%/6%（不用前3K）
      dyn_gentle — 做多前3K<8%降到18%，做空不调；6%后连跌≥3根收紧到15%/10%
      dyn_boost  — 做多前3K≥8%放宽到26%，做空前3K≥8%放宽到16%（反向：好信号放更宽）
    """
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if d == "long": init_stop = bt - av
    else: init_stop = bt + av

    # 初始止盈
    tp_base = 0.22 if d == "long" else 0.12
    tp_low = 0.12 if d == "long" else 0.08
    tp_tight = 0.08 if d == "long" else 0.06
    tp_gentle_low = 0.18 if d == "long" else 0.12  # 温和方案：做多降到18%，做空不变
    tp_gentle_tight = 0.15 if d == "long" else 0.10
    tp_boost = 0.26 if d == "long" else 0.16  # 放宽方案

    if d == "long": tp_price = bt * (1 + tp_base)
    else: tp_price = bt * (1 - tp_base)

    max_exc = -np.inf; exit_idx = exit_price = exit_reason = None
    hit6 = None
    consec_down = 0
    first3_gain = None
    tp_adjusted = False

    for j in range(i + 1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue

        exc = (highs[j]-bt)/bt if d == "long" else (bt-lows[j])/bt
        if exc > max_exc: max_exc = exc

        # 到达6%标记
        if hit6 is None and exc >= 0.06:
            hit6 = j

        # 到达6%后跟踪收盘连跌
        if hit6 is not None and j > hit6:
            if d == "long" and closes[j] < closes[j-1]:
                consec_down += 1
            elif d == "short" and closes[j] > closes[j-1]:
                consec_down += 1
            else:
                consec_down = 0

            # 动态v2/v3：连跌≥2根 → 收紧止盈
            if consec_down >= 2 and not tp_adjusted and strategy in ("dyn_v2", "dyn_v3"):
                if d == "long": tp_price = bt * (1 + tp_tight)
                else: tp_price = bt * (1 - tp_tight)
                tp_adjusted = True
                exit_reason_tp = "tp_tight_6pct_dd"

            # 温和方案：连跌≥3根 → 收紧止盈
            if consec_down >= 3 and not tp_adjusted and strategy == "dyn_gentle":
                if d == "long": tp_price = bt * (1 + tp_gentle_tight)
                else: tp_price = bt * (1 - tp_gentle_tight)
                tp_adjusted = True
                exit_reason_tp = "tp_tight_6pct_dd"

        # 第3根收盘后检查前3K涨幅
        bh = j - i
        if bh == 3 and first3_gain is None:
            if d == "long":
                first3_gain = (max(highs[i+1:j+1]) - bt) / bt
            else:
                first3_gain = (bt - min(lows[i+1:j+1])) / bt

            # 动态v1/v2：前3K涨<8% → 降止盈
            if first3_gain < 0.08 and strategy in ("dyn_v1", "dyn_v2"):
                if not tp_adjusted:
                    if d == "long": tp_price = bt * (1 + tp_low)
                    else: tp_price = bt * (1 - tp_low)
                    tp_adjusted = True
                    exit_reason_tp = "tp_low_first3"

            # 温和方案：做多前3K<8% → 降到18%，做空不调
            if first3_gain < 0.08 and strategy == "dyn_gentle" and d == "long":
                if not tp_adjusted:
                    tp_price = bt * (1 + tp_gentle_low)
                    tp_adjusted = True
                    exit_reason_tp = "tp_low_first3"

            # 放宽方案：前3K≥8% → 放宽止盈
            if first3_gain >= 0.08 and strategy == "dyn_boost":
                if not tp_adjusted:
                    if d == "long": tp_price = bt * (1 + tp_boost)
                    else: tp_price = bt * (1 - tp_boost)
                    tp_adjusted = True
                    exit_reason_tp = "tp_boost_first3"

        # 止损线
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if d == "long": stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"

        # 止损检查
        if d == "long" and lows[j] < stop_line:
            exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if d == "short" and highs[j] > stop_line:
            exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break

        # 止盈检查
        if d == "long" and highs[j] >= tp_price:
            exit_idx, exit_price = j, float(tp_price)
            exit_reason = "tp_adjusted" if tp_adjusted else "take_profit"
            break
        if d == "short" and lows[j] <= tp_price:
            exit_idx, exit_price = j, float(tp_price)
            exit_reason = "tp_adjusted" if tp_adjusted else "take_profit"
            break

    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    ret = (exit_price-bt)/bt if d == "long" else (bt-exit_price)/bt
    r = (exit_price-bt)/av if d == "long" else (bt-exit_price)/av
    return {"direction": d, "r_value": r, "final_return": ret,
            "max_excursion": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "signal_idx": i, "exit_idx": exit_idx,
            "first3_gain": first3_gain if first3_gain is not None else np.nan}


def run_backtest(strategy):
    results = []; last_exit_idx = None
    for s in signals:
        if last_exit_idx is not None and s.idx <= last_exit_idx: continue
        r = simulate(s, strategy=strategy)
        if r: results.append(r); last_exit_idx = r["exit_idx"]
    return pd.DataFrame(results)


def print_stats(f, label):
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = f[f["direction"] == d]
        if sub.empty: continue
        sr = sub["r_value"]; w = sr[sr > 0]; l = sr[sr <= 0]
        pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
        pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
        print(f"  {lbl:>4} {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {sub['final_return'].sum():>9.1%} {pf_str:>7}")
    sr = f["r_value"]; w = sr[sr > 0]; l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    print(f"  {'合并':>4} {len(f):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {f['final_return'].sum():>9.1%} {pf_str:>7}")


strategies = [
    ("fixed",      "固定22%/12%"),
    ("dyn_v1",     "动态v1: 前3K<8%降止盈"),
    ("dyn_v3",     "动态v3: 6%后连跌≥2收紧"),
    ("dyn_v2",     "动态v2: v1+v3组合"),
    ("dyn_gentle", "温和: 做多前3K<8%降18%,连跌≥3收紧15%"),
    ("dyn_boost",  "放宽: 前3K≥8%放宽到26%/16%"),
]

print("=" * 80)
print("动态止盈 vs 固定止盈对比（MFE≥10%过滤）")
print("=" * 80)
print(f"\n{'策略':>24} {'方向':>4} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6} {'累计收益':>9} {'PF':>7}")
print("-" * 80)

summary = []
for strat, label in strategies:
    rdf = run_backtest(strat)
    f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
    print(f"\n{label}")
    print_stats(f, label)
    sr = f["r_value"]
    summary.append((label, len(f), sr.sum(), sr.mean(), (sr>0).mean(), f["final_return"].sum()))

print(f"\n{'='*80}")
print("汇总对比")
print(f"{'='*80}")
print(f"{'策略':>24} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6} {'累计收益':>9}")
print("-" * 80)
for label, cnt, cum_r, mean_r, win, ret in summary:
    print(f"{label:>24} {cnt:>5} {cum_r:>8.1f} {mean_r:>7.2f} {win:>6.0%} {ret:>9.1%}")

# 出场原因分布
print(f"\n{'='*80}")
print("各策略出场原因分布")
print(f"{'='*80}")
for strat, label in strategies:
    rdf = run_backtest(strat)
    f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
    print(f"\n{label}:")
    vc = f["exit_reason"].value_counts()
    for reason, cnt in vc.items():
        print(f"  {reason:>20} {cnt:>5} ({cnt/len(f):.0%})")

print(f"\n{'='*80}")
