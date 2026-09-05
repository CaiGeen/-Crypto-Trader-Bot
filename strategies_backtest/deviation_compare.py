"""对比突破K vs 前一K收盘价的日线EMA70偏离度箱体分布（≥10%波段）。"""
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


def simulate(signal, tp=0.12):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if d == "long": init_stop = bt - av; tp_price = bt*(1+tp)
    else: init_stop = bt + av; tp_price = bt*(1-tp)
    max_exc = -np.inf; exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if d == "long" else (bt-lows[j])/bt
        if exc > max_exc: max_exc = exc
        bh = j - i
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if d == "long": stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"
        if d == "long" and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if d == "short" and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if d == "long" and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if d == "short" and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    ret = (exit_price-bt)/bt if d == "long" else (bt-exit_price)/bt
    r = (exit_price-bt)/av if d == "long" else (bt-exit_price)/av
    # 两种偏离度
    cur_date = pd.to_datetime(times[i], utc=True).date()
    ema_cur = date_to_ema.get(cur_date, np.nan)
    dev_cur = (closes[i] - ema_cur) / ema_cur * 100 if not np.isnan(ema_cur) else np.nan
    if i - 1 >= 0:
        prev_date = pd.to_datetime(times[i-1], utc=True).date()
        ema_prev = date_to_ema.get(prev_date, np.nan)
        dev_prev = (closes[i-1] - ema_prev) / ema_prev * 100 if not np.isnan(ema_prev) else np.nan
    else:
        dev_prev = np.nan
    return {"direction": d, "r_value": r, "final_return": ret,
            "max_excursion": float(max_exc) if max_exc != -np.inf else 0.0,
            "signal_idx": i, "exit_idx": exit_idx,
            "dev_cur": dev_cur, "dev_prev": dev_prev}


results = []; last_exit_idx = None
for s in signals:
    if last_exit_idx is not None and s.idx <= last_exit_idx: continue
    r = simulate(s)
    if r: results.append(r); last_exit_idx = r["exit_idx"]

rdf = pd.DataFrame(results)
f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
print(f"≥10%过滤后 {len(f)} 波段\n")

dev_cur = f["dev_cur"]
dev_prev = f["dev_prev"]

print("=" * 100)
print("日线EMA70偏离度箱体分布对比（≥10%波段，100单）")
print("=" * 100)

# 统计概览
for label, dev in [("突破K收盘价", dev_cur), ("突破K前一K收盘价", dev_prev)]:
    print(f"\n{label}:")
    print(f"  均值={dev.mean():.2f}%  中位={dev.median():.2f}%  std={dev.std():.2f}")
    print(f"  min={dev.min():.2f}%  Q25={dev.quantile(0.25):.2f}%  Q75={dev.quantile(0.75):.2f}%  max={dev.max():.2f}%")
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = f[f["direction"] == d]
        sd = sub["dev_cur"] if label == "突破K收盘价" else sub["dev_prev"]
        print(f"    {lbl}: 均值={sd.mean():.2f}%  中位={sd.median():.2f}%  n={len(sub)}")

# 箱体分布对比
bins = [-50, -30, -20, -15, -10, -5, 0, 5, 10, 15, 20, 30, 50, 100]
print(f"\n{'─'*100}")
print(f"{'区间(%)':>18} {'突破K':>8} {'占比':>8} {'累计':>8} │ {'前一K':>8} {'占比':>8} {'累计':>8} │ {'差值':>6}")
print(f"{'─'*100}")
cum_c = 0; cum_p = 0
for k in range(len(bins) - 1):
    cnt_c = ((dev_cur >= bins[k]) & (dev_cur < bins[k + 1])).sum()
    cnt_p = ((dev_prev >= bins[k]) & (dev_prev < bins[k + 1])).sum()
    cum_c += cnt_c; cum_p += cnt_p
    if cnt_c == 0 and cnt_p == 0: continue
    print(f"[{bins[k]:>5}, {bins[k+1]:>5}) {cnt_c:>8} {cnt_c/len(f):>8.1%} {cum_c/len(f):>8.1%} │ "
          f"{cnt_p:>8} {cnt_p/len(f):>8.1%} {cum_p/len(f):>8.1%} │ {cnt_c-cnt_p:>+6d}")

# [-5%,15%)过滤对比
in_c = ((dev_cur >= -5) & (dev_cur < 15)).sum()
in_p = ((dev_prev >= -5) & (dev_prev < 15)).sum()
print(f"\n{'─'*100}")
print(f"[-5%, 15%) 过滤保留:  突破K={in_c}单  前一K={in_p}单  差异={in_c-in_p}单")
print(f"  突破K 累计R: {f[(dev_cur >= -5) & (dev_cur < 15)]['r_value'].sum():.1f}  均R: {f[(dev_cur >= -5) & (dev_cur < 15)]['r_value'].mean():.2f}")
print(f"  前一K 累计R: {f[(dev_prev >= -5) & (dev_prev < 15)]['r_value'].sum():.1f}  均R: {f[(dev_prev >= -5) & (dev_prev < 15)]['r_value'].mean():.2f}")

# 多空分开过滤对比
print(f"\n  多空分开:")
for d, lbl in [("long", "做多"), ("short", "做空")]:
    sub = f[f["direction"] == d]
    sc = sub[(sub["dev_cur"] >= -5) & (sub["dev_cur"] < 15)]
    sp = sub[(sub["dev_prev"] >= -5) & (sub["dev_prev"] < 15)]
    print(f"    {lbl}: 突破K={len(sc)}单/{sc['r_value'].sum():.1f}R  前一K={len(sp)}单/{sp['r_value'].sum():.1f}R")

print("=" * 100)
