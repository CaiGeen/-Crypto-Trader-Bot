"""反向思考：最大化全量累计R（不过MFE事后过滤），扫描最优配置。

实盘不能用MFE过滤（事后才知道），所以全量累计R才是真实可实现的。
扫描：止盈配置 × 事前过滤条件 → 全量累计R。
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


def simulate(signal, tp_long=0.22, tp_short=0.12,
             atr_max=1.0, dev_lo=-999, dev_hi=999):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    atr_pct = av / bt
    dev_prev = get_prev_dev(i)

    # 事前过滤
    if atr_pct > atr_max: return None
    if np.isnan(dev_prev) or dev_prev < dev_lo or dev_prev >= dev_hi: return None

    if is_long:
        init_stop = bt - av
        tp_price = bt * (1 + tp_long) if tp_long > 0 else float("inf")
    else:
        init_stop = bt + av
        tp_price = bt * (1 - tp_short) if tp_short > 0 else 0.0

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
        if tp_long > 0 and is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if tp_short > 0 and not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    ret = (exit_price-bt)/bt if is_long else (bt-exit_price)/bt
    return {"direction": d, "r_value": r, "final_return": ret,
            "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "exit_idx": exit_idx,
            "atr_pct": atr_pct, "dev_prev": dev_prev}


def run(tp_long, tp_short, atr_max=1.0, dev_lo=-999, dev_hi=999):
    results = []; last_exit = None
    for s in signals:
        if last_exit is not None and s.idx <= last_exit: continue
        r = simulate(s, tp_long, tp_short, atr_max, dev_lo, dev_hi)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)


def summary(rdf):
    if rdf.empty: return 0, 0, 0, 0, 0, 0, 0
    sr = rdf["r_value"]
    n_ge10 = (rdf["mfe"].abs() >= 0.10).sum()
    return len(rdf), sr.sum(), sr.mean(), (sr>0).mean(), rdf["final_return"].sum(), n_ge10, sr[sr<=0].sum()


print("=" * 110)
print("反向思考：最大化全量累计R（不过MFE事后过滤）")
print("=" * 110)

# 1. 止盈配置扫描（无过滤）
print(f"\n—— 止盈配置扫描（无事前过滤，全量累计R）——")
print(f"{'止盈配置':>16} {'全量单数':>8} {'全量累计R':>10} {'均R':>7} {'胜率':>6} {'累计收益':>9} │ {'≥10%单数':>8} {'≥10%累计R':>10}")
print("-" * 95)
tp_configs = [
    (0.0, 0.0, "纯KAMA"),
    (0.12, 0.12, "12%/12%"),
    (0.15, 0.12, "15%/12%"),
    (0.18, 0.12, "18%/12%"),
    (0.22, 0.12, "22%/12%"),
    (0.25, 0.12, "25%/12%"),
    (0.30, 0.12, "30%/12%"),
    (0.22, 0.15, "22%/15%"),
    (0.22, 0.08, "22%/8%"),
    (0.22, 0.10, "22%/10%"),
    (0.18, 0.10, "18%/10%"),
    (0.15, 0.10, "15%/10%"),
]
best_full_r = -999; best_config = ""
for tp_l, tp_s, label in tp_configs:
    rdf = run(tp_l, tp_s)
    cnt, cum_r, mean_r, win, ret, n10, neg_r = summary(rdf)
    print(f"{label:>16} {cnt:>8} {cum_r:>10.1f} {mean_r:>7.2f} {win:>6.0%} {ret:>9.1%} │ {n10:>8} {rdf[rdf['mfe'].abs()>=0.10]['r_value'].sum():>10.1f}")
    if cum_r > best_full_r:
        best_full_r = cum_r; best_config = label

print(f"\n  最优止盈配置: {best_config}, 全量累计R={best_full_r:.1f}")

# 2. ATR过滤扫描（用最优止盈）
print(f"\n—— ATR过滤扫描（22%/12%止盈，全量累计R）——")
print(f"{'ATR上限':>10} {'全量单数':>8} {'全量累计R':>10} {'均R':>7} {'胜率':>6} {'累计收益':>9} │ {'≥10%单数':>8}")
print("-" * 75)
for atr_max in [1.0, 0.03, 0.025, 0.02, 0.018, 0.015, 0.012, 0.01]:
    rdf = run(0.22, 0.12, atr_max=atr_max)
    cnt, cum_r, mean_r, win, ret, n10, neg_r = summary(rdf)
    print(f"{'≤'+f'{atr_max:.1%}':>10} {cnt:>8} {cum_r:>10.1f} {mean_r:>7.2f} {win:>6.0%} {ret:>9.1%} │ {n10:>8}")

# 3. 偏离度过滤扫描（用最优止盈）
print(f"\n—— 偏离度过滤扫描（22%/12%止盈，全量累计R）——")
print(f"{'偏离度区间':>16} {'全量单数':>8} {'全量累计R':>10} {'均R':>7} {'胜率':>6} {'累计收益':>9}")
print("-" * 70)
dev_ranges = [
    (-999, 999, "无过滤"),
    (-5, 15, "[-5,15)"),
    (-5, 10, "[-5,10)"),
    (-5, 5, "[-5,5)"),
    (-10, 10, "[-10,10)"),
    (-10, 15, "[-10,15)"),
    (-15, 20, "[-15,20)"),
    (-3, 8, "[-3,8)"),
    (-8, 12, "[-8,12)"),
]
for lo, hi, label in dev_ranges:
    rdf = run(0.22, 0.12, dev_lo=lo, dev_hi=hi)
    cnt, cum_r, mean_r, win, ret, n10, neg_r = summary(rdf)
    print(f"{label:>16} {cnt:>8} {cum_r:>10.1f} {mean_r:>7.2f} {win:>6.0%} {ret:>9.1%}")

# 4. 组合过滤扫描
print(f"\n—— 组合过滤扫描（22%/12%止盈，全量累计R）——")
print(f"{'ATR上限':>8} {'偏离度':>10} {'单数':>6} {'全量累计R':>10} {'均R':>7} {'胜率':>6} {'收益':>9} │ {'≥10%单':>6} {'≥10%累计R':>10}")
print("-" * 90)
combos = [
    (1.0, -999, 999, "无"),
    (0.02, -999, 999, "无"),
    (0.015, -999, 999, "无"),
    (1.0, -5, 15, "[-5,15)"),
    (1.0, -5, 10, "[-5,10)"),
    (0.02, -5, 15, "[-5,15)"),
    (0.02, -5, 10, "[-5,10)"),
    (0.015, -5, 15, "[-5,15)"),
    (0.015, -5, 10, "[-5,10)"),
    (0.025, -10, 15, "[-10,15)"),
    (0.03, -10, 20, "[-10,20)"),
    (0.025, -8, 12, "[-8,12)"),
]
best_combo_r = -999; best_combo = ""
for atr_max, dev_lo, dev_hi, dev_label in combos:
    rdf = run(0.22, 0.12, atr_max=atr_max, dev_lo=dev_lo, dev_hi=dev_hi)
    cnt, cum_r, mean_r, win, ret, n10, neg_r = summary(rdf)
    atr_label = "无" if atr_max >= 1.0 else f"≤{atr_max:.1%}"
    print(f"{atr_label:>8} {dev_label:>10} {cnt:>6} {cum_r:>10.1f} {mean_r:>7.2f} {win:>6.0%} {ret:>9.1%} │ {n10:>6} {rdf[rdf['mfe'].abs()>=0.10]['r_value'].sum():>10.1f}")
    if cum_r > best_combo_r:
        best_combo_r = cum_r; best_combo = f"ATR{atr_label}+偏离{dev_label}"

print(f"\n  最优组合: {best_combo}, 全量累计R={best_combo_r:.1f}")

# 5. 最优配置多空分离详情
print(f"\n{'='*110}")
print(f"最优配置详情：{best_combo}")
print(f"{'='*110}")
# 找到最优参数
for atr_max, dev_lo, dev_hi, dev_label in combos:
    atr_label = "无" if atr_max >= 1.0 else f"≤{atr_max:.1%}"
    if f"ATR{atr_label}+偏离{dev_label}" == best_combo:
        rdf = run(0.22, 0.12, atr_max=atr_max, dev_lo=dev_lo, dev_hi=dev_hi)
        break

print(f"\n{'方向':>6} {'全量单':>6} {'全量R':>8} {'均R':>7} {'胜率':>6} {'收益':>9} │ {'≥10%单':>6} {'≥10%R':>8} │ {'<10%单':>6} {'<10%R':>8}")
print("-" * 90)
for d, lbl in [("long", "做多"), ("short", "做空"), ("all", "合并")]:
    sub = rdf if d == "all" else rdf[rdf["direction"] == d]
    if sub.empty: continue
    sr = sub["r_value"]
    f10 = sub[sub["mfe"].abs() >= 0.10]; f_lt = sub[sub["mfe"].abs() < 0.10]
    print(f"{lbl:>6} {len(sub):>6} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {sub['final_return'].sum():>9.1%} │ "
          f"{len(f10):>6} {f10['r_value'].sum():>8.1f} │ {len(f_lt):>6} {f_lt['r_value'].sum():>8.1f}")

# 年度
print(f"\n  年度统计（全量）:")
rdf_yr = rdf.copy()
rdf_yr["year"] = pd.to_datetime([times[r["exit_idx"]] for _, r in rdf_yr.iterrows()]).year
print(f"  {'年份':>6} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6}")
print("  " + "-" * 35)
for yr in sorted(rdf_yr["year"].unique()):
    sub = rdf_yr[rdf_yr["year"] == yr]
    sr = sub["r_value"]
    print(f"  {yr:>6} {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%}")

print(f"\n{'='*110}")
