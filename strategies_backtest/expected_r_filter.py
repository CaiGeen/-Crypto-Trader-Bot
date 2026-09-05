"""预期R值过滤：对比固定盈利空间法 vs 条件期望R法。

版本1（用户方法）：预期R = 固定盈利空间 / ATR%， < 阈值则过滤
版本2（条件期望R）：按ATR%分档查表E[R]，E[R] < 阈值则过滤
版本3（方向×ATR%）：按方向×ATR%分档查表E[R]，E[R] < 阈值则过滤
版本4（联合查表）：按ATR%×偏离度联合查表E[R]，E[R] < 阈值则过滤
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

daily_df = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1d.parquet")
k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
daily_df["date"] = daily_df["open_time"].dt.date
date_to_ema = dict(zip(daily_df["date"], de70))


def get_prev_dev(i):
    if i - 1 < 0: return np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100


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
    return {"direction": d, "r_value": r, "final_return": ret,
            "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "exit_idx": exit_idx,
            "atr_pct": av / bt, "dev_prev": get_prev_dev(i)}


# 先跑全量回测，建立查表模型
all_results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r: all_results.append(r); last_exit = r["exit_idx"]

rdf = pd.DataFrame(all_results)

print("=" * 110)
print("预期R值过滤分析")
print("=" * 110)
print(f"\n全量基准: {len(rdf)}单, 累计R={rdf['r_value'].sum():.1f}, 均R={rdf['r_value'].mean():.2f}")

# 建立查表模型
atr_bins = [(0, 0.01), (0.01, 0.015), (0.015, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 1.0)]
atr_lookup = {}
for lo, hi in atr_bins:
    sub = rdf[(rdf["atr_pct"] >= lo) & (rdf["atr_pct"] < hi)]
    if not sub.empty:
        atr_lookup[(lo, hi)] = sub["r_value"].mean()

dev_bins = [(-999, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 20), (20, 999)]
joint_lookup = {}
for (alo, ahi) in atr_bins:
    for (dlo, dhi) in dev_bins:
        sub = rdf[(rdf["atr_pct"] >= alo) & (rdf["atr_pct"] < ahi) &
                  (rdf["dev_prev"] >= dlo) & (rdf["dev_prev"] < dhi)]
        if len(sub) >= 3:
            joint_lookup[(alo, ahi, dlo, dhi)] = sub["r_value"].mean()

dir_atr_lookup = {}
for d in ["long", "short"]:
    for (lo, hi) in atr_bins:
        sub = rdf[(rdf["direction"] == d) & (rdf["atr_pct"] >= lo) & (rdf["atr_pct"] < hi)]
        if not sub.empty:
            dir_atr_lookup[(d, lo, hi)] = sub["r_value"].mean()

print(f"\n—— 条件期望R查表（按ATR%分档）——")
print(f"{'ATR%区间':>14} {'单数':>5} {'E[R]':>7} {'胜率':>6}")
print("-" * 40)
for (lo, hi), er in sorted(atr_lookup.items()):
    sub = rdf[(rdf["atr_pct"] >= lo) & (rdf["atr_pct"] < hi)]
    print(f"[{lo:.1%},{hi:.1%}) {len(sub):>5} {er:>7.2f} {(sub['r_value']>0).mean():>6.0%}")

print(f"\n—— 条件期望R查表（方向×ATR%分档）——")
print(f"{'方向':>6} {'ATR%区间':>14} {'单数':>5} {'E[R]':>7} {'胜率':>6}")
print("-" * 50)
for d, lbl in [("long", "做多"), ("short", "做空")]:
    for (lo, hi) in atr_bins:
        key = (d, lo, hi)
        if key not in dir_atr_lookup: continue
        sub = rdf[(rdf["direction"] == d) & (rdf["atr_pct"] >= lo) & (rdf["atr_pct"] < hi)]
        print(f"{lbl:>6} [{lo:.1%},{hi:.1%}) {len(sub):>5} {dir_atr_lookup[key]:>7.2f} {(sub['r_value']>0).mean():>6.0%}")

# 版本1：固定盈利空间法
print(f"\n{'='*110}")
print("版本1：固定盈利空间法（预期R = 盈利空间/ATR%， < 阈值则过滤）")
print(f"{'='*110}")
print(f"\n{'盈利空间':>8} {'R阈值':>6} {'等价ATR过滤':>12} {'单数':>6} {'累计R':>10} {'均R':>7} {'胜率':>6} {'收益':>9}")
print("-" * 75)
best_v1 = -999; best_v1p = None
for gain_space in [0.04, 0.06, 0.08, 0.10]:
    for r_thresh in [2, 3, 4, 5]:
        atr_cutoff = gain_space / r_thresh
        sub = rdf[rdf["atr_pct"] <= atr_cutoff]
        if sub.empty: continue
        sr = sub["r_value"]
        print(f"{gain_space:>8.0%} {r_thresh:>6} {'ATR<='+f'{atr_cutoff:.1%}':>12} {len(sub):>6} {sr.sum():>10.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {sub['final_return'].sum():>9.1%}")
        if sr.sum() > best_v1:
            best_v1 = sr.sum(); best_v1p = (gain_space, r_thresh, atr_cutoff)
print(f"\n  最优: 盈利空间={best_v1p[0]:.0%}, R阈值={best_v1p[1]}, 等价ATR<={best_v1p[2]:.1%}, 累计R={best_v1:.1f}")

# 版本2：条件期望R法（按ATR%查表）
print(f"\n{'='*110}")
print("版本2：条件期望R法（按ATR%查表E[R]，E[R] < 阈值则过滤）")
print(f"{'='*110}")

def get_expected_r(atr_pct):
    for (lo, hi), er in atr_lookup.items():
        if lo <= atr_pct < hi:
            return er
    return 0.0

print(f"\n{'E[R]阈值':>8} {'单数':>6} {'累计R':>10} {'均R':>7} {'胜率':>6} {'收益':>9} {'过滤掉':>6}")
print("-" * 60)
best_v2 = -999; best_v2p = None
for r_thresh in [0, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0]:
    mask = rdf["atr_pct"].apply(get_expected_r) >= r_thresh
    sub = rdf[mask]
    if sub.empty: continue
    sr = sub["r_value"]
    print(f"{r_thresh:>8.1f} {len(sub):>6} {sr.sum():>10.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {sub['final_return'].sum():>9.1%} {len(rdf)-len(sub):>6}")
    if sr.sum() > best_v2:
        best_v2 = sr.sum(); best_v2p = r_thresh
print(f"\n  最优: E[R]>={best_v2p}, 累计R={best_v2:.1f}")

# 版本3：方向×ATR%查表
print(f"\n{'='*110}")
print("版本3：条件期望R法（方向×ATR%查表E[R]，E[R] < 阈值则过滤）")
print(f"{'='*110}")

def get_expected_r_dir(direction, atr_pct):
    for (d, lo, hi), er in dir_atr_lookup.items():
        if d == direction and lo <= atr_pct < hi:
            return er
    return 0.0

print(f"\n{'E[R]阈值':>8} {'单数':>6} {'累计R':>10} {'均R':>7} {'胜率':>6} {'收益':>9} {'过滤掉':>6}")
print("-" * 60)
best_v3 = -999; best_v3p = None
for r_thresh in [0, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0]:
    mask = rdf.apply(lambda row: get_expected_r_dir(row["direction"], row["atr_pct"]) >= r_thresh, axis=1)
    sub = rdf[mask]
    if sub.empty: continue
    sr = sub["r_value"]
    print(f"{r_thresh:>8.1f} {len(sub):>6} {sr.sum():>10.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {sub['final_return'].sum():>9.1%} {len(rdf)-len(sub):>6}")
    if sr.sum() > best_v3:
        best_v3 = sr.sum(); best_v3p = r_thresh
print(f"\n  最优: E[R]>={best_v3p}, 累计R={best_v3:.1f}")

# 版本4：联合查表
print(f"\n{'='*110}")
print("版本4：条件期望R法（ATR%×偏离度联合查表，E[R] < 阈值则过滤）")
print(f"{'='*110}")

def get_expected_r_joint(atr_pct, dev_prev):
    if np.isnan(dev_prev): return 0.0
    for (alo, ahi, dlo, dhi), er in joint_lookup.items():
        if alo <= atr_pct < ahi and dlo <= dev_prev < dhi:
            return er
    return 0.0

print(f"\n{'E[R]阈值':>8} {'单数':>6} {'累计R':>10} {'均R':>7} {'胜率':>6} {'收益':>9} {'过滤掉':>6}")
print("-" * 60)
best_v4 = -999; best_v4p = None
for r_thresh in [0, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0]:
    mask = rdf.apply(lambda row: get_expected_r_joint(row["atr_pct"], row["dev_prev"]) >= r_thresh, axis=1)
    sub = rdf[mask]
    if sub.empty: continue
    sr = sub["r_value"]
    print(f"{r_thresh:>8.1f} {len(sub):>6} {sr.sum():>10.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {sub['final_return'].sum():>9.1%} {len(rdf)-len(sub):>6}")
    if sr.sum() > best_v4:
        best_v4 = sr.sum(); best_v4p = r_thresh
print(f"\n  最优: E[R]>={best_v4p}, 累计R={best_v4:.1f}")

# 汇总对比
print(f"\n{'='*110}")
print("汇总对比")
print(f"{'='*110}")
base_r = rdf["r_value"].sum()
print(f"\n{'方法':>30} {'单数':>6} {'累计R':>10} {'均R':>7} {'vs基准':>8}")
print("-" * 65)
print(f"{'无过滤基准':>30} {len(rdf):>6} {base_r:>10.1f} {rdf['r_value'].mean():>7.2f} {'—':>8}")

sub1 = rdf[rdf["atr_pct"] <= best_v1p[2]]
print(f"{'v1 固定空间法':>30} {len(sub1):>6} {best_v1:>10.1f} {sub1['r_value'].mean():>7.2f} {best_v1-base_r:>+8.1f}")

for label, best_r, best_t, func in [
    ("v2 E[R]按ATR%", best_v2, best_v2p, lambda row: get_expected_r(row["atr_pct"]) >= best_v2p),
    ("v3 E[R]方向×ATR%", best_v3, best_v3p, lambda row: get_expected_r_dir(row["direction"], row["atr_pct"]) >= best_v3p),
    ("v4 E[R]联合查表", best_v4, best_v4p, lambda row: get_expected_r_joint(row["atr_pct"], row["dev_prev"]) >= best_v4p),
]:
    mask = rdf.apply(func, axis=1)
    sub = rdf[mask]
    print(f"{label:>30} {len(sub):>6} {best_r:>10.1f} {sub['r_value'].mean():>7.2f} {best_r-base_r:>+8.1f}")


# 被过滤单子盈亏构成分析
print(f"\n{'='*110}")
print("被过滤单子盈亏构成分析（关键：过滤掉的是打损单还是盈利单？）")
print(f"{'='*110}")

def analyze_filtered(rdf, mask, label):
    kept = rdf[mask]
    filtered = rdf[~mask]
    f_win = filtered[filtered["r_value"] > 0]
    f_loss = filtered[filtered["r_value"] <= 0]
    print(f"\n  {label}")
    print(f"  {'':>4}保留: {len(kept)}单, 累计R={kept['r_value'].sum():.1f}")
    print(f"  {'':>4}过滤掉: {len(filtered)}单, 累计R={filtered['r_value'].sum():.1f}")
    if len(filtered) > 0:
        print(f"  {'':>4}  其中盈利单: {len(f_win)}单 ({len(f_win)/len(filtered):.0%}), 累计R={f_win['r_value'].sum():.1f}")
        print(f"  {'':>4}  其中打损单: {len(f_loss)}单 ({len(f_loss)/len(filtered):.0%}), 累计R={f_loss['r_value'].sum():.1f}")
        print(f"  {'':>4}  打损单平均R={f_loss['r_value'].mean():.2f}" if len(f_loss) > 0 else f"  {'':>4}  无打损单")
        net = -f_loss['r_value'].sum() if len(f_loss) > 0 else 0
        print(f"  {'':>4}  → 过滤避免亏损R={net:.1f}, 但错过盈利R={f_win['r_value'].sum():.1f}")
        if len(f_loss) > 0:
            ratio = net / (f_win['r_value'].sum() if f_win['r_value'].sum() != 0 else 0.01)
            print(f"  {'':>4}  → 避亏/错盈比={ratio:.2f}  {'✓ 有效过滤（主要避亏）' if ratio > 1 else '✗ 过滤有害（主要错盈）'}")

mask1 = rdf["atr_pct"] <= best_v1p[2]
analyze_filtered(rdf, mask1, f"v1 固定空间法（ATR<={best_v1p[2]:.1%}, 过滤{len(rdf)-mask1.sum()}单）")

mask2 = rdf["atr_pct"].apply(get_expected_r) >= best_v2p
analyze_filtered(rdf, mask2, f"v2 E[R]按ATR%（阈值>={best_v2p}, 过滤{len(rdf)-mask2.sum()}单）")

mask3 = rdf.apply(lambda row: get_expected_r_dir(row["direction"], row["atr_pct"]) >= best_v3p, axis=1)
analyze_filtered(rdf, mask3, f"v3 E[R]方向×ATR%（阈值>={best_v3p}, 过滤{len(rdf)-mask3.sum()}单）")

mask4 = rdf.apply(lambda row: get_expected_r_joint(row["atr_pct"], row["dev_prev"]) >= best_v4p, axis=1)
analyze_filtered(rdf, mask4, f"v4 E[R]联合查表（阈值>={best_v4p}, 过滤{len(rdf)-mask4.sum()}单）")

# 各阈值下被过滤单子盈亏构成趋势
print(f"\n{'='*110}")
print("各阈值下过滤效果趋势（v4联合查表为例）")
print(f"{'='*110}")
print(f"\n{'E[R]阈值':>8} {'保留':>5} {'过滤':>5} {'过滤中打损%':>12} {'过滤中盈利%':>12} {'避免亏损R':>10} {'错过盈利R':>10} {'净效果':>8}")
print("-" * 80)
for r_thresh in [0, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0]:
    mask = rdf.apply(lambda row: get_expected_r_joint(row["atr_pct"], row["dev_prev"]) >= r_thresh, axis=1)
    kept = rdf[mask]; filtered = rdf[~mask]
    if filtered.empty:
        print(f"{r_thresh:>8.1f} {len(kept):>5} {0:>5} {'—':>12} {'—':>12} {'—':>10} {'—':>10} {'—':>8}")
        continue
    f_win = filtered[filtered["r_value"] > 0]
    f_loss = filtered[filtered["r_value"] <= 0]
    avoid_loss = -f_loss["r_value"].sum() if len(f_loss) > 0 else 0
    miss_gain = f_win["r_value"].sum()
    net = avoid_loss - miss_gain
    print(f"{r_thresh:>8.1f} {len(kept):>5} {len(filtered):>5} {len(f_loss)/len(filtered):>11.0%} {len(f_win)/len(filtered):>11.0%} {avoid_loss:>10.1f} {miss_gain:>10.1f} {net:>+8.1f}")

print(f"\n{'='*110}")
