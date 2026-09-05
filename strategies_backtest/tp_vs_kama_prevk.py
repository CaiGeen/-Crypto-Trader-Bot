"""偏离度改为突破K前一K收盘价计算，对比止盈12-18% + 纯KAMA。"""
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
    if d == "long": init_stop = bt - av; tp_price = bt*(1+tp) if tp > 0 else float("inf")
    else: init_stop = bt + av; tp_price = bt*(1-tp) if tp > 0 else 0.0
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
        if tp > 0:
            if d == "long" and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
            if d == "short" and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    ret = (exit_price-bt)/bt if d == "long" else (bt-exit_price)/bt
    r = (exit_price-bt)/av if d == "long" else (bt-exit_price)/av
    # 偏离度：突破K前一K的收盘价 vs 前一K对应日期的日线EMA70
    prev_idx = i - 1
    if prev_idx >= 0:
        prev_date = pd.to_datetime(times[prev_idx], utc=True).date()
        ema = date_to_ema.get(prev_date, np.nan)
        dev = (closes[prev_idx] - ema) / ema * 100 if not np.isnan(ema) else np.nan
    else:
        dev = np.nan
    return {"direction": d, "r_value": r, "final_return": ret,
            "max_excursion": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "signal_idx": i, "exit_idx": exit_idx, "ema70_dev": dev}


def run_backtest(tp):
    results = []; last_exit_idx = None
    for s in signals:
        if last_exit_idx is not None and s.idx <= last_exit_idx: continue
        r = simulate(s, tp=tp)
        if r: results.append(r); last_exit_idx = r["exit_idx"]
    return pd.DataFrame(results)


def print_table(f, tp_label):
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = f[f["direction"] == d]
        if sub.empty: continue
        sr = sub["r_value"]; w = sr[sr > 0]; l = sr[sr <= 0]
        pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
        pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
        print(f"{tp_label:>10} {lbl:>6} {len(sub):>6} {sr.sum():>8.1f} {sr.mean():>8.2f} {sr.median():>8.2f} "
              f"{(sr>0).mean():>8.0%} {sub['final_return'].sum():>10.1%} {pf_str:>8}")
    sr = f["r_value"]; w = sr[sr > 0]; l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    print(f"{tp_label:>10} {'合并':>6} {len(f):>6} {sr.sum():>8.1f} {sr.mean():>8.2f} {sr.median():>8.2f} "
          f"{(sr>0).mean():>8.0%} {f['final_return'].sum():>10.1%} {pf_str:>8}")


tps = [0.0, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18]
tp_labels = {0.0: "纯KAMA", 0.12: "12%", 0.13: "13%", 0.14: "14%", 0.15: "15%", 0.16: "16%", 0.17: "17%", 0.18: "18%"}

print("=" * 105)
print("偏离度改为突破K前一K收盘价计算 — 固定止盈 vs 纯KAMA对比")
print("=" * 105)

# 先看偏离度分布变化
rdf0 = run_backtest(0.12)
f0 = rdf0[rdf0["max_excursion"].abs() >= 0.10].reset_index(drop=True)
dev = f0["ema70_dev"]
print(f"\n偏离度分布（突破K前一K）: 均值={dev.mean():.2f}%  中位={dev.median():.2f}%  min={dev.min():.2f}%  max={dev.max():.2f}%")
for d, lbl in [("long", "做多"), ("short", "做空")]:
    sub = f0[f0["direction"] == d]["ema70_dev"]
    print(f"  {lbl}: 均值={sub.mean():.2f}%  中位={sub.median():.2f}%")

print(f"\n偏离度[-5%,15%)过滤后波段数: {len(f0[(f0['ema70_dev'] >= -5) & (f0['ema70_dev'] < 15)])} / {len(f0)}")

for label, use_filter in [("无过滤", False), ("EMA70[-5%,15%)（前一K）", True)]:
    print(f"\n{'─'*105}")
    print(f"  {label}")
    print(f"{'─'*105}")
    print(f"{'止盈':>10} {'方向':>6} {'单数':>6} {'累计R':>8} {'均R':>8} {'中位R':>8} {'胜率':>8} {'累计收益':>10} {'PF':>8}")
    print("-" * 105)
    for tp in tps:
        rdf = run_backtest(tp)
        f = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
        if use_filter:
            f = f[(f["ema70_dev"] >= -5) & (f["ema70_dev"] < 15)].reset_index(drop=True)
        print_table(f, tp_labels[tp])
        print()

print("=" * 105)
