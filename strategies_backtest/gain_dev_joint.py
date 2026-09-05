"""波段涨幅 vs 日线EMA70偏离度 联合分析。

分析A：当涨幅首次达到X%时，偏离度分布
分析B：当偏离度首次达到Y%时，后续还能涨多少
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


def get_dev(j, price):
    cur_date = pd.to_datetime(times[j], utc=True).date()
    ema = date_to_ema.get(cur_date, np.nan)
    if np.isnan(ema): return np.nan
    return (price - ema) / ema * 100


def simulate_track(signal):
    """纯KAMA回测，逐根K线记录涨幅和偏离度。"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av
    else: init_stop = bt + av

    track = []  # 每根K线的 (涨幅, 偏离度_high, 偏离度_close)
    exit_idx = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        if is_long:
            gain = (highs[j] - bt) / bt
            dev_h = get_dev(j, highs[j])
            dev_c = get_dev(j, closes[j])
        else:
            gain = (bt - lows[j]) / bt
            dev_h = get_dev(j, lows[j])
            dev_c = get_dev(j, closes[j])
        track.append((gain, dev_h, dev_c, j))

        bh = j - i
        if bh <= 3: stop_line = init_stop
        else:
            if is_long: stop_line = max(ks, bt)
            else: stop_line = min(ks, bt)
        if is_long and lows[j] < stop_line: exit_idx = j; break
        if not is_long and highs[j] > stop_line: exit_idx = j; break
    if exit_idx is None: exit_idx = n - 1

    mfe = max(t[0] for t in track) if track else 0.0
    return {"direction": d, "signal_idx": i, "exit_idx": exit_idx,
            "mfe": mfe, "track": track}


# 运行回测
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate_track(s)
    if r: results.append(r); last_exit = r["exit_idx"]

print("=" * 110)
print("波段涨幅 vs 日线EMA70偏离度 联合分析")
print("=" * 110)
print(f"\n总波段: {len(results)}")

# ===== 分析A：当涨幅首次达到X%时，偏离度分布 =====
print(f"\n{'='*110}")
print("分析A：当涨幅首次达到X%时，EMA70偏离度分布")
print(f"{'='*110}")

gain_thresholds = [0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20]

for d, lbl in [("long", "做多"), ("short", "做空")]:
    print(f"\n—— {lbl} ——")
    print(f"{'涨幅阈值':>10} {'波段数':>6} {'偏离_high中位':>13} {'偏离_high均值':>13} {'偏离_close中位':>14} {'偏离_close P25':>14} {'偏离_close P75':>14}")
    print("-" * 95)
    for gt in gain_thresholds:
        dev_h_list = []; dev_c_list = []
        for r in results:
            if r["direction"] != d: continue
            for gain, dev_h, dev_c, j in r["track"]:
                if gain >= gt:
                    dev_h_list.append(dev_h)
                    dev_c_list.append(dev_c)
                    break  # 首次达到
        if not dev_h_list: continue
        dh = pd.Series(dev_h_list); dc = pd.Series(dev_c_list)
        print(f"{gt:>10.0%} {len(dev_h_list):>6} {dh.median():>13.2f} {dh.mean():>13.2f} {dc.median():>14.2f} {dc.quantile(0.25):>14.2f} {dc.quantile(0.75):>14.2f}")

# ===== 分析B：当偏离度首次达到Y%时，后续还能涨多少 =====
print(f"\n{'='*110}")
print("分析B：当偏离度首次达到Y%时，波段最终MFE分布")
print(f"{'='*110}")

dev_thresholds_long = [5, 10, 15, 20, 25, 30, 40]
dev_thresholds_short = [-5, -10, -15, -20, -25, -30, -40]

for d, lbl, dev_ths in [("long", "做多", dev_thresholds_long), ("short", "做空", dev_thresholds_short)]:
    print(f"\n—— {lbl}（偏离度用最高价/最低价算）——")
    print(f"{'偏离度阈值':>10} {'波段数':>6} {'MFE中位':>8} {'MFE均值':>8} {'MFE≥10%':>8} {'MFE≥15%':>8} {'已到顶概率':>10}")
    print("-" * 70)
    for dt in dev_ths:
        mfe_list = []; reached = 0; total = 0
        for r in results:
            if r["direction"] != d: continue
            total += 1
            found = False
            for gain, dev_h, dev_c, j in r["track"]:
                if d == "long" and dev_h >= dt:
                    found = True; break
                if d == "short" and dev_h <= dt:
                    found = True; break
            if found:
                mfe_list.append(r["mfe"])
        if not mfe_list: continue
        m = pd.Series(mfe_list)
        # "已到顶"= 偏离度达到dt时，MFE已经接近最终MFE（后续涨幅<2%）
        print(f"{dt:>10} {len(mfe_list):>6} {m.median():>8.1%} {m.mean():>8.1%} {(m>=0.10).mean():>8.0%} {(m>=0.15).mean():>8.0%} {(m>=0.20).mean():>10.0%}")

# ===== 分析C：涨幅达到X%时的偏离度 → 后续是否到顶 =====
print(f"\n{'='*110}")
print("分析C：涨幅达X%时偏离度分档 → 后续继续涨概率")
print(f"{'='*110}")

for d, lbl in [("long", "做多"), ("short", "做空")]:
    print(f"\n—— {lbl}：涨幅达10%时偏离度分档 ——")
    print(f"{'偏离度区间':>14} {'波段数':>6} {'最终MFE中位':>11} {'最终MFE均值':>11} {'继续涨>2%':>10} {'继续涨>5%':>10}")
    print("-" * 70)
    dev_bins = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 30), (30, 100)] if d == "long" else [(-100, -30), (-30, -20), (-20, -15), (-15, -10), (-10, -5), (-5, 0)]
    for lo, hi in dev_bins:
        mfe_list = []
        for r in results:
            if r["direction"] != d: continue
            for gain, dev_h, dev_c, j in r["track"]:
                if gain >= 0.10:
                    if lo <= dev_h < hi:
                        mfe_list.append(r["mfe"])
                    break
        if not mfe_list: continue
        m = pd.Series(mfe_list)
        print(f"[{lo:>4}, {hi:>4}) {len(mfe_list):>6} {m.median():>11.1%} {m.mean():>11.1%} {(m>0.12).mean():>10.0%} {(m>0.15).mean():>10.0%}")

# ===== 分析D：偏离度到达Y%时涨幅 → 后续空间 =====
print(f"\n{'='*110}")
print("分析D：偏离度达Y%时当前涨幅 → 后续剩余空间")
print(f"{'='*110}")

for d, lbl, dev_ths in [("long", "做多", [10, 15, 20, 25, 30]), ("short", "做空", [-10, -15, -20, -25, -30])]:
    print(f"\n—— {lbl} ——")
    print(f"{'偏离度阈值':>10} {'波段数':>6} {'当时涨幅中位':>12} {'最终MFE中位':>11} {'剩余空间中位':>12} {'剩余≥2%':>9}")
    print("-" * 70)
    for dt in dev_ths:
        cur_gains = []; final_mfes = []; remainings = []
        for r in results:
            if r["direction"] != d: continue
            for gain, dev_h, dev_c, j in r["track"]:
                reached = (d == "long" and dev_h >= dt) or (d == "short" and dev_h <= dt)
                if reached:
                    cur_gains.append(gain)
                    final_mfes.append(r["mfe"])
                    remainings.append(r["mfe"] - gain)
                    break
        if not cur_gains: continue
        cg = pd.Series(cur_gains); fm = pd.Series(final_mfes); rm = pd.Series(remainings)
        print(f"{dt:>10} {len(cur_gains):>6} {cg.median():>12.1%} {fm.median():>11.1%} {rm.median():>12.1%} {(rm>=0.02).mean():>9.0%}")

print(f"\n{'='*110}")
