"""持仓时间分析：持仓K线数 vs R值/MFE/出场原因，寻找"持仓超过N根K后不再大涨"的拐点。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
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

def simulate(signal, tp_l=0.18, tp_s=0.12):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long:
        init_stop = bt - av; tp_price = bt * (1 + tp_l) if tp_l > 0 else float("inf")
    else:
        init_stop = bt + av; tp_price = bt * (1 - tp_s) if tp_s > 0 else 0.0
    max_exc = -np.inf; exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if is_long else (bt-lows[j])/bt
        if exc > max_exc: max_exc = exc
        bh = j - i
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if is_long: stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"
        if is_long and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if not is_long and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if tp_l > 0 and is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if tp_s > 0 and not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    ret = (exit_price-bt)/bt if is_long else (bt-exit_price)/bt
    hold_bars = exit_idx - i
    return {"direction": d, "r_value": r, "final_return": ret,
            "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "exit_idx": exit_idx,
            "hold_bars": hold_bars, "atr_pct": av / bt}

all_results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r: all_results.append(r); last_exit = r["exit_idx"]
rdf = pd.DataFrame(all_results)

print("=" * 110)
print("持仓时间分析（18%/12%止盈，全量1249单）")
print("=" * 110)
print(f"\n全量: {len(rdf)}单, 累计R={rdf['r_value'].sum():.1f}, 均R={rdf['r_value'].mean():.2f}")

# 1. 持仓K线数分布
print(f"\n{'='*110}")
print("1. 持仓K线数分布")
print(f"{'='*110}")
print(f"\n  中位: {rdf['hold_bars'].median():.0f}根, 均值: {rdf['hold_bars'].mean():.1f}根, 范围: [{rdf['hold_bars'].min()}, {rdf['hold_bars'].max()}]")
bins = [0, 3, 5, 8, 12, 18, 25, 35, 50, 9999]
labels = ["1-3", "4-5", "6-8", "9-12", "13-18", "19-25", "26-35", "36-50", "50+"]
print(f"\n{'持仓K线':>8} {'单数':>5} {'占比':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'MFE中位':>8} {'MFE均值':>8}")
print("-" * 60)
for i in range(len(bins)-1):
    sub = rdf[(rdf["hold_bars"] > bins[i]) & (rdf["hold_bars"] <= bins[i+1])]
    if sub.empty: continue
    sr = sub["r_value"]
    print(f"{labels[i]:>8} {len(sub):>5} {len(sub)/len(rdf):>5.0%} {sr.sum():>8.1f} {sr.mean():>6.2f} {(sr>0).mean():>5.0%} {sub['mfe'].median():>7.1%} {sub['mfe'].mean():>7.1%}")

# 2. 出场原因 vs 持仓时间
print(f"\n{'='*110}")
print("2. 出场原因 vs 持仓时间")
print(f"{'='*110}")
reason_map = {"atr_stop": "ATR止损", "breakeven": "保本平仓", "kama_trail": "KAMA跟踪", "take_profit": "固定止盈", "end": "未平仓"}
print(f"\n{'出场原因':>10} {'单数':>5} {'持仓中位':>8} {'持仓均值':>8} {'均R':>6} {'累计R':>8} {'MFE中位':>8}")
print("-" * 60)
for reason, label in reason_map.items():
    sub = rdf[rdf["exit_reason"] == reason]
    if sub.empty: continue
    print(f"{label:>10} {len(sub):>5} {sub['hold_bars'].median():>7.0f} {sub['hold_bars'].mean():>7.1f} {sub['r_value'].mean():>6.2f} {sub['r_value'].sum():>8.1f} {sub['mfe'].median():>7.1%}")

# 3. 持仓时间 vs 出场原因构成（关键：持仓越久，固定止盈占比是否下降？）
print(f"\n{'='*110}")
print("3. 持仓时间 vs 出场原因构成（持仓越久是否越不可能走到固定止盈？）")
print(f"{'='*110}")
print(f"\n{'持仓K线':>8} {'单数':>5} {'ATR止损':>8} {'保本':>6} {'KAMA跟踪':>8} {'固定止盈':>8} {'固定止盈%':>9}")
print("-" * 55)
for i in range(len(bins)-1):
    sub = rdf[(rdf["hold_bars"] > bins[i]) & (rdf["hold_bars"] <= bins[i+1])]
    if sub.empty: continue
    c_as = (sub["exit_reason"] == "atr_stop").sum()
    c_be = (sub["exit_reason"] == "breakeven").sum()
    c_kt = (sub["exit_reason"] == "kama_trail").sum()
    c_tp = (sub["exit_reason"] == "take_profit").sum()
    print(f"{labels[i]:>8} {len(sub):>5} {c_as:>8} {c_be:>6} {c_kt:>8} {c_tp:>8} {c_tp/len(sub):>8.0%}")

# 4. 持仓时间 vs R值分布（细粒度）
print(f"\n{'='*110}")
print("4. 持仓时间 vs R值分布（每根K线都看）")
print(f"{'='*110}")
print(f"\n{'持仓K线':>8} {'单数':>5} {'累计R':>8} {'均R':>6} {'R中位':>6} {'R>3占比':>8} {'R>5占比':>8} {'R<-0.5占比':>10}")
print("-" * 70)
fine_bins = list(range(0, 31)) + [9999]
fine_labels = [str(i+1) for i in range(30)] + ["31+"]
for i in range(len(fine_bins)-1):
    sub = rdf[(rdf["hold_bars"] > fine_bins[i]) & (rdf["hold_bars"] <= fine_bins[i+1])]
    if sub.empty: continue
    sr = sub["r_value"]
    print(f"{fine_labels[i]:>8} {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>6.2f} {sr.median():>6.2f} {(sr>3).mean():>7.0%} {(sr>5).mean():>7.0%} {(sr<-0.5).mean():>9.0%}")

# 5. 持仓超过N根K线后的累计R贡献（边际分析）
print(f"\n{'='*110}")
print("5. 持仓超过N根K线后的累计R贡献（边际递减分析）")
print(f"{'='*110}")
print(f"\n{'持仓>':>7} {'剩余单数':>8} {'剩余累计R':>10} {'剩余均R':>8} {'占总R%':>8} {'边际R':>8}")
print("-" * 55)
prev_cum = 0
for threshold in [0, 3, 5, 8, 10, 12, 15, 18, 22, 25, 30]:
    sub = rdf[rdf["hold_bars"] > threshold]
    sr = sub["r_value"]
    cum = sr.sum()
    marginal = cum - prev_cum
    print(f"{threshold:>6} {len(sub):>8} {cum:>10.1f} {sr.mean():>8.2f} {cum/rdf['r_value'].sum():>7.0%} {marginal:>+8.1f}")
    prev_cum = cum

# 6. 固定止盈单的持仓时间分布
print(f"\n{'='*110}")
print("6. 固定止盈单 vs KAMA跟踪单 的持仓时间对比")
print(f"{'='*110}")
tp_sub = rdf[rdf["exit_reason"] == "take_profit"]
kt_sub = rdf[rdf["exit_reason"] == "kama_trail"]
print(f"\n  固定止盈: {len(tp_sub)}单, 持仓中位{tp_sub['hold_bars'].median():.0f}根, 均值{tp_sub['hold_bars'].mean():.1f}根, 均R={tp_sub['r_value'].mean():.2f}")
print(f"  KAMA跟踪: {len(kt_sub)}单, 持仓中位{kt_sub['hold_bars'].median():.0f}根, 均值{kt_sub['hold_bars'].mean():.1f}根, 均R={kt_sub['r_value'].mean():.2f}")
print(f"\n  固定止盈持仓分布:")
for i in range(len(bins)-1):
    sub = tp_sub[(tp_sub["hold_bars"] > bins[i]) & (tp_sub["hold_bars"] <= bins[i+1])]
    if sub.empty: continue
    bar = "█" * (len(sub) * 50 // len(tp_sub))
    print(f"    {labels[i]:>6}: {len(sub):>4} ({len(sub)/len(tp_sub):>4.0%}) {bar}")
print(f"\n  KAMA跟踪持仓分布:")
for i in range(len(bins)-1):
    sub = kt_sub[(kt_sub["hold_bars"] > bins[i]) & (kt_sub["hold_bars"] <= bins[i+1])]
    if sub.empty: continue
    bar = "█" * (len(sub) * 50 // len(kt_sub))
    print(f"    {labels[i]:>6}: {len(sub):>4} ({len(sub)/len(kt_sub):>4.0%}) {bar}")

# 7. 持仓时间内MFE到达时间分析（MFE是在前几根K线达到的？）
print(f"\n{'='*110}")
print("7. MFE达到时间分析（最大涨幅在第几根K线出现？）")
print(f"{'='*110}")

def find_mfe_bar(signal, tp_l=0.18, tp_s=0.12):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long:
        init_stop = bt - av; tp_price = bt * (1 + tp_l) if tp_l > 0 else float("inf")
    else:
        init_stop = bt + av; tp_price = bt * (1 - tp_s) if tp_s > 0 else 0.0
    max_exc = -np.inf; mfe_bar = 0; exit_idx = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if is_long else (bt-lows[j])/bt
        if exc > max_exc: max_exc = exc; mfe_bar = j - i
        bh = j - i
        if bh <= 3: stop_line = init_stop
        else:
            if is_long: stop_line = max(ks, bt)
            else: stop_line = min(ks, bt)
        if is_long and lows[j] < stop_line: exit_idx = j; break
        if not is_long and highs[j] > stop_line: exit_idx = j; break
        if tp_l > 0 and is_long and highs[j] >= tp_price: exit_idx = j; break
        if tp_s > 0 and not is_long and lows[j] <= tp_price: exit_idx = j; break
    if exit_idx is None: exit_idx = n-1
    return mfe_bar

mfe_bars = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    mb = find_mfe_bar(s)
    if mb is not None:
        mfe_bars.append(mb)
        r = simulate(s)
        if r: last_exit = r["exit_idx"]

mb_s = pd.Series(mfe_bars)
print(f"\n  MFE达到时间: 中位{mb_s.median():.0f}根, 均值{mb_s.mean():.1f}根")
print(f"\n{'MFE在第N根':>10} {'单数':>5} {'占比':>6} {'累计占比':>8}")
print("-" * 35)
cum = 0
for i in range(0, 25):
    cnt = (mb_s == i).sum()
    if cnt == 0: continue
    cum += cnt
    print(f"{'第'+str(i)+'根':>10} {cnt:>5} {cnt/len(mb_s):>5.0%} {cum/len(mb_s):>7.0%}")
cnt = (mb_s >= 25).sum()
cum += cnt
print(f"{'第25根+':>10} {cnt:>5} {cnt/len(mb_s):>5.0%} {cum/len(mb_s):>7.0%}")

print(f"\n{'='*110}")
