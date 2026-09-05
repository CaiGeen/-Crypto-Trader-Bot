"""波段空间预测：寻找能提前判断最终MFF的特征。"""
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
    # 记录首次达到各涨幅的K线索引
    hit = {0.04: None, 0.06: None, 0.08: None, 0.10: None, 0.12: None}
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if d == "long" else (bt-lows[j])/bt
        if exc > max_exc: max_exc = exc
        for t in hit:
            if hit[t] is None and exc >= t: hit[t] = j
        bh = j - i
        if bh <= 3: stop_line = init_stop
        else:
            stop_line = max(ks, bt) if d == "long" else min(ks, bt)
        if d == "long" and lows[j] < stop_line: exit_idx = j; break
        if d == "short" and highs[j] > stop_line: exit_idx = j; break
    if exit_idx is None: exit_idx = n - 1
    mfe = float(max_exc) if max_exc != -np.inf else 0.0

    # 特征提取
    feat = {"direction": d, "mfe": mfe, "atr_pct": av / bt, "exit_idx": exit_idx}

    # 前3根K线累计涨幅
    first3 = min(i + 4, exit_idx + 1)
    if d == "long":
        feat["first3_gain"] = (max(highs[i+1:first3]) - bt) / bt
    else:
        feat["first3_gain"] = (bt - min(lows[i+1:first3])) / bt

    # 到达6%的速度和回调
    if hit[0.06] is not None:
        feat["bars_to_6pct"] = hit[0.06] - i
        # 到达6%后3根K线的回调
        j6 = hit[0.06]
        peak_at_6 = highs[j6] if d == "long" else lows[j6]
        end_check = min(j6 + 4, exit_idx + 1)
        if d == "long":
            min_after = min(lows[j6+1:end_check]) if j6 + 1 < end_check else peak_at_6
            feat["dd_after_6pct"] = (peak_at_6 - min_after) / peak_at_6
        else:
            max_after = max(highs[j6+1:end_check]) if j6 + 1 < end_check else peak_at_6
            feat["dd_after_6pct"] = (max_after - peak_at_6) / peak_at_6 if peak_at_6 > 0 else 0.0
        # 到达6%后是否连续回调（收盘连跌3根）
        consec_down = 0
        for jj in range(j6 + 1, min(j6 + 4, exit_idx + 1)):
            if d == "long" and closes[jj] < closes[jj - 1]: consec_down += 1
            elif d == "short" and closes[jj] > closes[jj - 1]: consec_down += 1
            else: break
        feat["consec_down_after6"] = consec_down
    else:
        feat["bars_to_6pct"] = None
        feat["dd_after_6pct"] = None
        feat["consec_down_after6"] = None

    # 到达4%的速度
    if hit[0.04] is not None:
        feat["bars_to_4pct"] = hit[0.04] - i
    else:
        feat["bars_to_4pct"] = None

    return feat


results = []; last_exit_idx = None
for s in signals:
    if last_exit_idx is not None and s.idx <= last_exit_idx: continue
    r = simulate(s)
    if r: results.append(r); last_exit_idx = r["exit_idx"]

rdf = pd.DataFrame(results)
# 只分析MFE≥6%的波段（因为需要到达6%的特征）
f = rdf[rdf["mfe"] >= 0.06].reset_index(drop=True)
print(f"纯KAMA跟踪，MFE≥6%波段: {len(f)} 单\n")

print("=" * 110)
print("波段空间预测：哪些特征能判断最终MFE？")
print("=" * 110)

# ========== 分析1：到达6%后回调 vs 最终MFE ==========
print(f"\n{'━'*110}")
print(f"分析1：到达6%后3根K线回调 vs 最终MFE")
print(f"{'━'*110}")
valid = f[f["dd_after_6pct"].notna()]
print(f"{'回调区间':>12} {'单数':>6} {'MFE均值':>8} {'MFE中位':>8} {'≥12%概率':>10} {'≥10%概率':>10} {'≥8%概率':>8}")
print("-" * 80)
for lo, hi in [(0, 0.01), (0.01, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 1)]:
    sub = valid[(valid["dd_after_6pct"] >= lo) & (valid["dd_after_6pct"] < hi)]
    if sub.empty: continue
    m = sub["mfe"]
    print(f"[{lo:.0%}, {hi:.0%}) {len(sub):>6} {m.mean():>8.1%} {m.median():>8.1%} "
          f"{(m>=0.12).mean():>10.0%} {(m>=0.10).mean():>10.0%} {(m>=0.08).mean():>8.0%}")

# ========== 分析2：到达6%的速度 vs 最终MFE ==========
print(f"\n{'━'*110}")
print(f"分析2：从突破到到达6%的速度（K线数） vs 最终MFE")
print(f"{'━'*110}")
print(f"{'速度':>12} {'单数':>6} {'MFE均值':>8} {'MFE中位':>8} {'≥12%概率':>10} {'≥10%概率':>10} {'≥8%概率':>8}")
print("-" * 80)
for lo, hi in [(0, 5), (5, 10), (10, 20), (20, 40), (40, 999)]:
    sub = valid[(valid["bars_to_6pct"] >= lo) & (valid["bars_to_6pct"] < hi)]
    if sub.empty: continue
    m = sub["mfe"]
    print(f"[{lo:>3}, {hi:>3})K {len(sub):>6} {m.mean():>8.1%} {m.median():>8.1%} "
          f"{(m>=0.12).mean():>10.0%} {(m>=0.10).mean():>10.0%} {(m>=0.08).mean():>8.0%}")

# ========== 分析3：前3根K线涨幅 vs 最终MFE ==========
print(f"\n{'━'*110}")
print(f"分析3：突破后前3根K线最大涨幅 vs 最终MFE")
print(f"{'━'*110}")
print(f"{'前3K涨幅':>12} {'单数':>6} {'MFE均值':>8} {'MFE中位':>8} {'≥12%概率':>10} {'≥10%概率':>10} {'≥8%概率':>8}")
print("-" * 80)
for lo, hi in [(0, 0.02), (0.02, 0.04), (0.04, 0.06), (0.06, 0.08), (0.08, 1)]:
    sub = f[(f["first3_gain"] >= lo) & (f["first3_gain"] < hi)]
    if sub.empty: continue
    m = sub["mfe"]
    print(f"[{lo:.0%}, {hi:.0%}) {len(sub):>6} {m.mean():>8.1%} {m.median():>8.1%} "
          f"{(m>=0.12).mean():>10.0%} {(m>=0.10).mean():>10.0%} {(m>=0.08).mean():>8.0%}")

# ========== 分析4：突破时ATR% vs 最终MFE ==========
print(f"\n{'━'*110}")
print(f"分析4：突破时ATR% vs 最终MFE（MFE≥6%波段）")
print(f"{'━'*110}")
print(f"{'ATR%':>12} {'单数':>6} {'MFE均值':>8} {'MFE中位':>8} {'≥12%概率':>10} {'≥10%概率':>10} {'≥8%概率':>8}")
print("-" * 80)
for lo, hi in [(0, 0.015), (0.015, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 1)]:
    sub = f[(f["atr_pct"] >= lo) & (f["atr_pct"] < hi)]
    if sub.empty: continue
    m = sub["mfe"]
    print(f"[{lo:.1%}, {hi:.1%}) {len(sub):>6} {m.mean():>8.1%} {m.median():>8.1%} "
          f"{(m>=0.12).mean():>10.0%} {(m>=0.10).mean():>10.0%} {(m>=0.08).mean():>8.0%}")

# ========== 分析5：连续回调根数 vs 最终MFE ==========
print(f"\n{'━'*110}")
print(f"分析5：到达6%后连续回调根数 vs 最终MFE")
print(f"{'━'*110}")
print(f"{'连跌根数':>10} {'单数':>6} {'MFE均值':>8} {'MFE中位':>8} {'≥12%概率':>10} {'≥10%概率':>10} {'≥8%概率':>8}")
print("-" * 80)
for k in range(4):
    sub = valid[valid["consec_down_after6"] == k]
    if sub.empty: continue
    m = sub["mfe"]
    print(f"{k:>10} {len(sub):>6} {m.mean():>8.1%} {m.median():>8.1%} "
          f"{(m>=0.12).mean():>10.0%} {(m>=0.10).mean():>10.0%} {(m>=0.08).mean():>8.0%}")

# ========== 分析6：组合特征 ==========
print(f"\n{'━'*110}")
print(f"分析6：组合特征（ATR% + 到达6%后回调）→ 预测≥12%概率")
print(f"{'━'*110}")
valid2 = valid[valid["atr_pct"].notna()]
print(f"{'ATR%':>10} {'6%后回调':>10} {'单数':>6} {'≥12%概率':>10} {'MFE中位':>8}")
print("-" * 60)
for atr_lo, atr_hi in [(0, 0.02), (0.02, 0.03), (0.03, 1)]:
    for dd_lo, dd_hi in [(0, 0.02), (0.02, 0.04), (0.04, 1)]:
        sub = valid2[(valid2["atr_pct"] >= atr_lo) & (valid2["atr_pct"] < atr_hi) &
                     (valid2["dd_after_6pct"] >= dd_lo) & (valid2["dd_after_6pct"] < dd_hi)]
        if len(sub) < 3: continue
        m = sub["mfe"]
        print(f"[{atr_lo:.1%},{atr_hi:.1%}) [{dd_lo:.0%},{dd_hi:.0%}) {len(sub):>6} {(m>=0.12).mean():>10.0%} {m.median():>8.1%}")

print("=" * 110)
