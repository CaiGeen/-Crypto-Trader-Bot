"""波段空间预测v2：用"不再创新高"定义回调，对比"收盘连跌"定义。"""
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


def simulate(signal):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if d == "long": init_stop = bt - av
    else: init_stop = bt + av
    max_exc = -np.inf; exit_idx = None
    hit6 = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if d == "long" else (bt-lows[j])/bt
        if exc > max_exc: max_exc = exc
        if hit6 is None and exc >= 0.06: hit6 = j
        bh = j - i
        if bh <= 3: stop_line = init_stop
        else: stop_line = max(ks, bt) if d == "long" else min(ks, bt)
        if d == "long" and lows[j] < stop_line: exit_idx = j; break
        if d == "short" and highs[j] > stop_line: exit_idx = j; break
    if exit_idx is None: exit_idx = n - 1
    mfe = float(max_exc) if max_exc != -np.inf else 0.0

    feat = {"direction": d, "mfe": mfe, "atr_pct": av / bt, "exit_idx": exit_idx}

    # 前3K涨幅
    first3 = min(i + 4, exit_idx + 1)
    if d == "long": feat["first3_gain"] = (max(highs[i+1:first3]) - bt) / bt
    else: feat["first3_gain"] = (bt - min(lows[i+1:first3])) / bt

    if hit6 is not None:
        feat["bars_to_6pct"] = hit6 - i
        j6 = hit6
        check_end = min(j6 + 4, exit_idx + 1)

        # 定义A：收盘连跌（原来的定义）
        consec_down = 0
        for jj in range(j6 + 1, check_end):
            if d == "long" and closes[jj] < closes[jj - 1]: consec_down += 1
            elif d == "short" and closes[jj] > closes[jj - 1]: consec_down += 1
            else: break
        feat["consec_down_v1"] = consec_down

        # 定义B：不再创新高（到达6%后3根K线都没有超过j6的极值）
        peak_at_6 = highs[j6] if d == "long" else lows[j6]
        no_new_high = 0
        for jj in range(j6 + 1, check_end):
            if d == "long" and highs[jj] <= peak_at_6: no_new_high += 1
            elif d == "short" and lows[jj] >= peak_at_6: no_new_high += 1
            else: break
        feat["no_new_high_v2"] = no_new_high

        # 定义C：不再创新高（更宽松——3根K线内最高high没超过j6的high）
        if d == "long":
            max_after = max(highs[j6+1:check_end]) if j6 + 1 < check_end else peak_at_6
            feat["any_new_high_v2"] = 1 if max_after > peak_at_6 else 0
        else:
            min_after = min(lows[j6+1:check_end]) if j6 + 1 < check_end else peak_at_6
            feat["any_new_high_v2"] = 1 if min_after < peak_at_6 else 0
    else:
        feat["bars_to_6pct"] = None
        feat["consec_down_v1"] = None
        feat["no_new_high_v2"] = None
        feat["any_new_high_v2"] = None

    return feat


results = []; last_exit_idx = None
for s in signals:
    if last_exit_idx is not None and s.idx <= last_exit_idx: continue
    r = simulate(s)
    if r: results.append(r); last_exit_idx = r["exit_idx"]

rdf = pd.DataFrame(results)
f = rdf[rdf["mfe"] >= 0.06].reset_index(drop=True)
valid = f[f["consec_down_v1"].notna()]
print(f"纯KAMA，MFE≥6%波段: {len(f)} 单（有6%特征: {len(valid)} 单）\n")

print("=" * 110)
print("回调定义对比：v1=收盘连跌 vs v2=不再创新高（到达6%后3根K线）")
print("=" * 110)

# 对比两种定义的分布
print(f"\n—— 两种定义的分布对比 ——")
print(f"{'根数':>6} {'v1收盘连跌':>12} {'v2不再创新高':>14}")
print("-" * 40)
for k in range(4):
    c1 = (valid["consec_down_v1"] == k).sum()
    c2 = (valid["no_new_high_v2"] == k).sum()
    print(f"{k:>6} {c1:>12} {c2:>14}")

# v1: 收盘连跌 vs MFE
print(f"\n{'━'*110}")
print(f"v1（收盘连跌）：到达6%后连续收盘反向根数 vs 最终MFE")
print(f"{'━'*110}")
print(f"{'连跌根数':>10} {'单数':>6} {'MFE均值':>8} {'MFE中位':>8} {'≥12%概率':>10} {'≥10%概率':>10} {'≥8%概率':>8}")
print("-" * 80)
for k in range(4):
    sub = valid[valid["consec_down_v1"] == k]
    if sub.empty: continue
    m = sub["mfe"]
    print(f"{k:>10} {len(sub):>6} {m.mean():>8.1%} {m.median():>8.1%} "
          f"{(m>=0.12).mean():>10.0%} {(m>=0.10).mean():>10.0%} {(m>=0.08).mean():>8.0%}")

# v2: 不再创新高 vs MFE
print(f"\n{'━'*110}")
print(f"v2（不再创新高）：到达6%后连续未创新高根数 vs 最终MFE")
print(f"{'━'*110}")
print(f"{'未创新高根数':>12} {'单数':>6} {'MFE均值':>8} {'MFE中位':>8} {'≥12%概率':>10} {'≥10%概率':>10} {'≥8%概率':>8}")
print("-" * 80)
for k in range(4):
    sub = valid[valid["no_new_high_v2"] == k]
    if sub.empty: continue
    m = sub["mfe"]
    print(f"{k:>12} {len(sub):>6} {m.mean():>8.1%} {m.median():>8.1%} "
          f"{(m>=0.12).mean():>10.0%} {(m>=0.10).mean():>10.0%} {(m>=0.08).mean():>8.0%}")

# 前3K涨幅（不变，作为参照）
print(f"\n{'━'*110}")
print(f"参照：前3K涨幅 vs 最终MFE（不变）")
print(f"{'━'*110}")
print(f"{'前3K涨幅':>12} {'单数':>6} {'MFE中位':>8} {'≥12%概率':>10} {'≥10%概率':>10}")
print("-" * 60)
for lo, hi in [(0.02, 0.04), (0.04, 0.06), (0.06, 0.08), (0.08, 1)]:
    sub = f[(f["first3_gain"] >= lo) & (f["first3_gain"] < hi)]
    if sub.empty: continue
    m = sub["mfe"]
    print(f"[{lo:.0%}, {hi:.0%}) {len(sub):>6} {m.median():>8.1%} {(m>=0.12).mean():>10.0%} {(m>=0.10).mean():>10.0%}")

print("=" * 110)
