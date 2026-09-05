"""验证两个过滤条件：ATR%>1.5%不做 + 偏离度不在[-5%,10%)不做。"""
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

TP_LONG = 0.22; TP_SHORT = 0.12


def get_prev_dev(i):
    if i - 1 < 0: return np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100


def simulate(signal, use_filter=False):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    atr_pct = av / bt
    dev_prev = get_prev_dev(i)

    if use_filter:
        if atr_pct > 0.015: return None
        if np.isnan(dev_prev) or dev_prev < -5 or dev_prev >= 10: return None

    if is_long:
        init_stop = bt - av; tp_price = bt * (1 + TP_LONG)
    else:
        init_stop = bt + av; tp_price = bt * (1 - TP_SHORT)

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
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    ret = (exit_price-bt)/bt if is_long else (bt-exit_price)/bt
    return {"direction": d, "r_value": r, "final_return": ret,
            "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "exit_idx": exit_idx,
            "atr_pct": atr_pct, "dev_prev": dev_prev}


def run(use_filter):
    results = []; last_exit = None
    for s in signals:
        if last_exit is not None and s.idx <= last_exit: continue
        r = simulate(s, use_filter)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)


def print_stats(f, label):
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = f[f["direction"] == d]
        if sub.empty:
            print(f"  {lbl:>4} {'0':>5} {'—':>8} {'—':>7} {'—':>6} {'—':>9}")
            continue
        sr = sub["r_value"]
        print(f"  {lbl:>4} {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {sub['final_return'].sum():>9.1%}")
    sr = f["r_value"]; w = sr[sr > 0]; l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    print(f"  {'合并':>4} {len(f):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%} {f['final_return'].sum():>9.1%} {pf_str:>7}")


print("=" * 90)
print("过滤条件验证：ATR%≤1.5% & 偏离度∈[-5%,10%)")
print("=" * 90)

for label, use_filter in [("无过滤", False), ("双过滤", True)]:
    rdf = run(use_filter)
    f = rdf[rdf["mfe"].abs() >= 0.10].reset_index(drop=True)
    print(f"\n{'─'*90}")
    print(f"  {label}（MFE≥10%过滤）")
    print(f"{'─'*90}")
    print(f"{'方向':>6} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6} {'累计收益':>9} {'PF':>7}")
    print("-" * 60)
    print_stats(f, label)

    # 年度统计
    print(f"\n  年度统计:")
    rdf_yr = f.copy()
    rdf_yr["year"] = pd.to_datetime([times[s] for s in rdf_yr.index]).year if not f.empty else []
    # 用signal_idx更准确
    f_idx = rdf[rdf["mfe"].abs() >= 0.10].copy()
    f_idx["year"] = pd.to_datetime([times[r["exit_idx"]] for _, r in f_idx.iterrows()]).year
    print(f"  {'年份':>6} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6}")
    print("  " + "-" * 35)
    for yr in sorted(f_idx["year"].unique()):
        sub = f_idx[f_idx["year"] == yr]
        sr = sub["r_value"]
        print(f"  {yr:>6} {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%}")

    # 出场原因
    print(f"\n  出场原因:")
    vc = f["exit_reason"].value_counts()
    for reason, cnt in vc.items():
        sub = f[f["exit_reason"] == reason]
        print(f"    {reason:>15} {cnt:>4} ({cnt/len(f):.0%}) 均R={sub['r_value'].mean():.2f}")

# 过滤掉的信号统计
rdf_all = run(False)
rdf_filt = run(True)
filtered_out = len(rdf_all) - len(rdf_filt)
print(f"\n{'='*90}")
print(f"过滤效果汇总")
print(f"{'='*90}")
print(f"  过滤前总波段: {len(rdf_all)}")
print(f"  过滤后总波段: {len(rdf_filt)}")
print(f"  过滤掉波段: {filtered_out} ({filtered_out/len(rdf_all):.0%})")

f_all = rdf_all[rdf_all["mfe"].abs() >= 0.10]
f_filt = rdf_filt[rdf_filt["mfe"].abs() >= 0.10]
print(f"\n  MFE≥10%波段: {len(f_all)} → {len(f_filt)}（过滤掉{len(f_all)-len(f_filt)}单）")
print(f"  累计R: {f_all['r_value'].sum():.1f} → {f_filt['r_value'].sum():.1f}")
print(f"  均R: {f_all['r_value'].mean():.2f} → {f_filt['r_value'].mean():.2f}")
print(f"  胜率: {(f_all['r_value']>0).mean():.0%} → {(f_filt['r_value']>0).mean():.0%}")
print(f"  累计收益: {f_all['final_return'].sum():.1%} → {f_filt['final_return'].sum():.1%}")

# 被过滤掉的≥10%波段分析
f_all_idx = set(f_all.index)
f_filt_idx = set(f_filt.index)
removed_idx = f_all_idx - f_filt_idx
if removed_idx:
    removed = f_all.loc[list(removed_idx)]
    print(f"\n  被过滤掉的{len(removed)}单≥10%波段:")
    print(f"    累计R: {removed['r_value'].sum():.1f}, 均R: {removed['r_value'].mean():.2f}, 胜率: {(removed['r_value']>0).mean():.0%}")
    print(f"    ATR%中位: {removed['atr_pct'].median():.2%}, 偏离度中位: {removed['dev_prev'].median():.2f}%")

print(f"\n{'='*90}")
