"""统计MFE≥6%波段的突破K前一K日线EMA70偏离度分布。"""
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


def find_callback_extreme(i, direction, window=6):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n: continue
        if direction == "long":
            if highs[j + 1] <= highs[j] and highs[j + 2] <= highs[j]:
                return highs[j], j
        else:
            if lows[j + 1] >= lows[j] and lows[j + 2] >= lows[j]:
                return lows[j], j
    return None, None


def simulate(signal):
    """纯KAMA回测，用回调高点算MFE，返回偏离度信息。"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    if cb_extreme is None: cb_extreme = bt

    if is_long: init_stop = bt - av
    else: init_stop = bt + av

    max_exc_cb = -np.inf; max_exc_bt = -np.inf
    exit_idx = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        if is_long:
            exc_cb = (highs[j] - cb_extreme) / cb_extreme
            exc_bt = (highs[j] - bt) / bt
        else:
            exc_cb = (cb_extreme - lows[j]) / cb_extreme
            exc_bt = (bt - lows[j]) / bt
        if exc_cb > max_exc_cb: max_exc_cb = exc_cb
        if exc_bt > max_exc_bt: max_exc_bt = exc_bt
        bh = j - i
        if bh <= 3: stop_line = init_stop
        else:
            if is_long: stop_line = max(ks, bt)
            else: stop_line = min(ks, bt)
        if is_long and lows[j] < stop_line: exit_idx = j; break
        if not is_long and highs[j] > stop_line: exit_idx = j; break
    if exit_idx is None: exit_idx = n - 1

    # 前一K的日线EMA70偏离度
    if i - 1 >= 0:
        prev_date = pd.to_datetime(times[i-1], utc=True).date()
        ema_prev = date_to_ema.get(prev_date, np.nan)
        dev_prev = (closes[i-1] - ema_prev) / ema_prev * 100 if not np.isnan(ema_prev) else np.nan
    else:
        dev_prev = np.nan

    # 突破K的偏离度
    cur_date = pd.to_datetime(times[i], utc=True).date()
    ema_cur = date_to_ema.get(cur_date, np.nan)
    dev_cur = (closes[i] - ema_cur) / ema_cur * 100 if not np.isnan(ema_cur) else np.nan

    r = (closes[exit_idx]-bt)/av if is_long else (bt-closes[exit_idx])/av

    return {
        "direction": d, "signal_idx": i, "exit_idx": exit_idx,
        "mfe_cb": float(max_exc_cb) if max_exc_cb != -np.inf else 0.0,
        "mfe_bt": float(max_exc_bt) if max_exc_bt != -np.inf else 0.0,
        "dev_prev": dev_prev, "dev_cur": dev_cur,
        "atr_pct": av / bt, "r_value": r,
        "cb_offset_pct": (bt - cb_extreme) / bt if is_long else (cb_extreme - bt) / bt,
    }


results = []; last_exit_idx = None
for s in signals:
    if last_exit_idx is not None and s.idx <= last_exit_idx: continue
    r = simulate(s)
    if r: results.append(r); last_exit_idx = r["exit_idx"]

rdf = pd.DataFrame(results)

# MFE≥6%（用回调高点）
f6 = rdf[rdf["mfe_cb"] >= 0.06].reset_index(drop=True)
f6_bt = rdf[rdf["mfe_bt"] >= 0.06].reset_index(drop=True)

print("=" * 100)
print(f"MFE≥6%波段 — 突破K前一K日线EMA70偏离度分布")
print("=" * 100)
print(f"\n总波段: {len(rdf)}")
print(f"MFE≥6%(回调高点): {len(f6)}单")
print(f"MFE≥6%(突破线): {len(f6_bt)}单")

# 1. 偏离度基本统计
print(f"\n—— 前一K偏离度基本统计（MFE≥6%, 回调高点, {len(f6)}单）——")
print(f"  均值: {f6['dev_prev'].mean():.2f}%")
print(f"  中位: {f6['dev_prev'].median():.2f}%")
print(f"  标准差: {f6['dev_prev'].std():.2f}%")
print(f"  范围: [{f6['dev_prev'].min():.2f}%, {f6['dev_prev'].max():.2f}%]")
print(f"  P10={f6['dev_prev'].quantile(0.1):.2f}%, P25={f6['dev_prev'].quantile(0.25):.2f}%, "
      f"P50={f6['dev_prev'].quantile(0.5):.2f}%, P75={f6['dev_prev'].quantile(0.75):.2f}%, "
      f"P90={f6['dev_prev'].quantile(0.9):.2f}%")

# 2. 多空分开
print(f"\n—— 多空分开 ——")
for d, lbl in [("long", "做多"), ("short", "做空")]:
    sub = f6[f6["direction"] == d]
    if sub.empty: continue
    print(f"  {lbl}: {len(sub)}单, 偏离度均值{sub['dev_prev'].mean():.2f}%, 中位{sub['dev_prev'].median():.2f}%")

# 3. 按5%间隔分箱（多空分离）
print(f"\n—— 前一K偏离度分箱（5%间隔，多空分离）——")
print(f"{'偏离度区间':>16} {'单数':>5} {'占比':>6} {'MFE中位':>8} {'均R':>7} {'≥10%':>6} │ {'多单':>4} {'MFE中':>6} {'均R':>6} {'≥10%':>6} │ {'空单':>4} {'MFE中':>6} {'均R':>6} {'≥10%':>6}")
print("-" * 110)
bins = [(-50, -15), (-15, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 15), (15, 20), (20, 50)]
for lo, hi in bins:
    sub = f6[(f6["dev_prev"] >= lo) & (f6["dev_prev"] < hi)]
    if sub.empty: continue
    m = sub["mfe_cb"]
    sl = sub[sub["direction"] == "long"]; ss = sub[sub["direction"] == "short"]
    ml = sl["mfe_cb"] if not sl.empty else pd.Series([0]); ms = ss["mfe_cb"] if not ss.empty else pd.Series([0])
    print(f"[{lo:>4}%, {hi:>4}%) {len(sub):>5} {len(sub)/len(f6):>6.0%} {m.median():>8.1%} {sub['r_value'].mean():>7.2f} {(m>=0.10).mean():>6.0%} │ "
          f"{len(sl):>4} {ml.median():>6.1%} {sl['r_value'].mean():>6.2f} {(ml>=0.10).mean():>6.0%} │ "
          f"{len(ss):>4} {ms.median():>6.1%} {ss['r_value'].mean():>6.2f} {(ms>=0.10).mean():>6.0%}")

# 4. 按2%间隔细分（核心区间）
print(f"\n—— 前一K偏离度细分（2%间隔, -10%~15%）——")
print(f"{'偏离度区间':>16} {'单数':>5} {'占比':>6} {'MFE中位':>8} {'≥10%概率':>10} {'≥8%概率':>8} {'做多':>5} {'做空':>5}")
print("-" * 75)
for lo in range(-10, 15, 2):
    hi = lo + 2
    sub = f6[(f6["dev_prev"] >= lo) & (f6["dev_prev"] < hi)]
    if sub.empty: continue
    m = sub["mfe_cb"]
    n_long = (sub["direction"] == "long").sum()
    n_short = (sub["direction"] == "short").sum()
    print(f"[{lo:>4}%, {hi:>4}%) {len(sub):>5} {len(sub)/len(f6):>6.0%} {m.median():>8.1%} {(m>=0.10).mean():>10.0%} {(m>=0.08).mean():>8.0%} {n_long:>5} {n_short:>5}")

# 5. 对比：MFE≥6% vs 全量 的偏离度分布（多空分离）
print(f"\n—— MFE≥6% vs 全量 偏离度分布对比（多空分离）——")
print(f"{'偏离度区间':>16} │ {'全量':>4} {'≥6%':>4} {'富集':>5} │ {'多全量':>5} {'多≥6%':>5} {'多富集':>6} │ {'空全量':>5} {'空≥6%':>5} {'空富集':>6}")
print("-" * 95)
rdf_l = rdf[rdf["direction"] == "long"]; rdf_s = rdf[rdf["direction"] == "short"]
f6_l = f6[f6["direction"] == "long"]; f6_s = f6[f6["direction"] == "short"]
for lo, hi in bins:
    sub_all = rdf[(rdf["dev_prev"] >= lo) & (rdf["dev_prev"] < hi)]
    sub_6 = f6[(f6["dev_prev"] >= lo) & (f6["dev_prev"] < hi)]
    if sub_all.empty: continue
    al_l = rdf_l[(rdf_l["dev_prev"] >= lo) & (rdf_l["dev_prev"] < hi)]
    al_s = rdf_s[(rdf_s["dev_prev"] >= lo) & (rdf_s["dev_prev"] < hi)]
    s6_l = f6_l[(f6_l["dev_prev"] >= lo) & (f6_l["dev_prev"] < hi)]
    s6_s = f6_s[(f6_s["dev_prev"] >= lo) & (f6_s["dev_prev"] < hi)]
    enrich = len(sub_6)/len(sub_all) if len(sub_all) > 0 else 0
    el = len(s6_l)/len(al_l) if len(al_l) > 0 else 0
    es = len(s6_s)/len(al_s) if len(al_s) > 0 else 0
    print(f"[{lo:>4}%, {hi:>4}%) │ {len(sub_all):>4} {len(sub_6):>4} {enrich:>5.2f} │ "
          f"{len(al_l):>5} {len(s6_l):>5} {el:>6.2f} │ {len(al_s):>5} {len(s6_s):>5} {es:>6.2f}")

# 6. 同时用突破线MFE≥6%对比
print(f"\n—— 回调高点 vs 突破线 MFE≥6% 偏离度分布对比 ——")
print(f"{'偏离度区间':>16} {'回调高点':>8} {'突破线':>8}")
print("-" * 40)
for lo, hi in bins:
    sub_cb = f6[(f6["dev_prev"] >= lo) & (f6["dev_prev"] < hi)]
    sub_bt = f6_bt[(f6_bt["dev_prev"] >= lo) & (f6_bt["dev_prev"] < hi)]
    if sub_cb.empty and sub_bt.empty: continue
    print(f"[{lo:>4}%, {hi:>4}%) {len(sub_cb):>8} {len(sub_bt):>8}")

print(f"\n{'='*100}")
