"""3K偏离度过滤区间精细扫描。下界-20~0，上界5~30，步长0.5。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
df_4h = load_klines(cfg)
kama_vals = kama(df_4h["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy()
n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy()
times = df_4h["open_time"].tolist()

daily_df = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1d.parquet")
k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
daily_df["date"] = daily_df["open_time"].dt.date
date_to_ema = dict(zip(daily_df["date"], de70))

def get_dev(i):
    if i - 1 < 0: return np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100

df_1m = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1m.parquet")
ms_1m = df_1m["open_time_ms"].to_numpy()
h1m = df_1m["high"].to_numpy(); l1m = df_1m["low"].to_numpy()
ms_4h = df_4h["open_time_ms"].to_numpy()
four_h_ms = 4 * 60 * 60 * 1000
min_start_idx = np.searchsorted(ms_1m, ms_4h)
min_end_idx = np.searchsorted(ms_1m, ms_4h + four_h_ms)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3

def disambiguate_1m(j, sl, tp_price, is_long):
    s = min_start_idx[j]; e = min_end_idx[j]
    if s >= e: return None
    for k in range(s, e):
        if is_long:
            if l1m[k] < sl: return ("stop", float(sl))
            if h1m[k] >= tp_price: return ("tp", float(tp_price))
        else:
            if h1m[k] > sl: return ("stop", float(sl))
            if l1m[k] <= tp_price: return ("tp", float(tp_price))
    return None

def find_callback_extreme(i, direction, window):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n4h: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]: return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] >= lows[j]: return lows[j], j
    return None, None

def simulate(signal):
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_nk = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, _ = find_callback_extreme(i, d, WINDOW)
    entry_base = cb_extreme if cb_extreme is not None else bt_nk
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)
    fill_price = float(entry_base) if filled else 0.0
    dev = get_dev(i)
    if not filled: return None
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {"dir": d, "exit_idx": i, "r": r1, "year": int(years[i]), "sig_idx": i, "dev": dev}
    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = init_stop if bh <= 3 else (max(ks, fill_price) if is_long else min(ks, fill_price))
        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j])
        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)
        if hit_stop and hit_tp:
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep = result
                if reason_1m == "stop":
                    exit_idx = j; exit_price = ep
                else:
                    exit_idx = j; exit_price = ep
                break
            ep = float(sl); exit_idx = j; exit_price = ep; break
        elif hit_stop:
            ep = float(sl); exit_idx = j; exit_price = ep; break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); break
    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1])
    r1 = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "r": r1+r2, "year": int(years[i]), "sig_idx": i, "dev": dev}

signals = detect_breakout(df_4h, WINDOW)
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r:
        results.append(r)
        last_exit = r["exit_idx"]

rdf = pd.DataFrame(results)
print(f"3K无过滤: {len(rdf)}单  累计R={rdf['r'].sum():.1f}  均R={rdf['r'].mean():.2f}")
print()

# 精细扫描
print("=" * 100)
print("偏离度过滤区间扫描（下界-20~0步长1 × 上界5~30步长1）")
print("=" * 100)

best_cum = {"val": 0, "lo": 0, "hi": 0, "n": 0, "mean_r": 0, "pf": 0}
best_meanr = {"val": 0, "lo": 0, "hi": 0, "n": 0, "cum": 0, "pf": 0}
best_pf = {"val": 0, "lo": 0, "hi": 0, "n": 0, "cum": 0, "mean_r": 0}

scan_results = []
for lo in range(-20, 1):
    for hi in range(5, 31):
        sub = rdf[(rdf["dev"] >= lo) & (rdf["dev"] < hi)]
        if len(sub) < 50: continue
        sr = sub["r"]
        cum = sr.sum()
        mean_r = sr.mean()
        pf = sr[sr>0].sum() / abs(sr[sr<=0].sum()) if sr[sr<=0].sum() != 0 else 999
        n = len(sub)
        scan_results.append({"lo": lo, "hi": hi, "n": n, "cum": cum, "mean_r": mean_r, "pf": pf, "win": (sr>0).mean()})
        if cum > best_cum["val"]:
            best_cum = {"val": cum, "lo": lo, "hi": hi, "n": n, "mean_r": mean_r, "pf": pf}
        if mean_r > best_meanr["val"] and n >= 200:
            best_meanr = {"val": mean_r, "lo": lo, "hi": hi, "n": n, "cum": cum, "pf": pf}
        if pf > best_pf["val"] and pf < 999 and n >= 200:
            best_pf = {"val": pf, "lo": lo, "hi": hi, "n": n, "cum": cum, "mean_r": mean_r}

sdf = pd.DataFrame(scan_results)

print(f"\n{'━'*100}")
print("Top 10 累计R")
print(f"{'━'*100}")
print(f"  {'下界':>6} {'上界':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'PF':>6}")
for _, r in sdf.nlargest(10, "cum").iterrows():
    print(f"  {r['lo']:>5}% {r['hi']:>5}% {r['n']:>6} {r['cum']:>8.1f} {r['mean_r']:>6.2f} {r['win']:>5.0%} {r['pf']:>6.1f}")

print(f"\n{'━'*100}")
print("Top 10 均R（单数≥200）")
print(f"{'━'*100}")
print(f"  {'下界':>6} {'上界':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'PF':>6}")
for _, r in sdf[sdf["n"]>=200].nlargest(10, "mean_r").iterrows():
    print(f"  {r['lo']:>5}% {r['hi']:>5}% {r['n']:>6} {r['cum']:>8.1f} {r['mean_r']:>6.2f} {r['win']:>5.0%} {r['pf']:>6.1f}")

print(f"\n{'━'*100}")
print("Top 10 PF（单数≥200）")
print(f"{'━'*100}")
print(f"  {'下界':>6} {'上界':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'PF':>6}")
for _, r in sdf[sdf["n"]>=200].nlargest(10, "pf").iterrows():
    print(f"  {r['lo']:>5}% {r['hi']:>5}% {r['n']:>6} {r['cum']:>8.1f} {r['mean_r']:>6.2f} {r['win']:>5.0%} {r['pf']:>6.1f}")

# 综合评分：累计R × 均R（兼顾总量和质量）
print(f"\n{'━'*100}")
print("Top 10 综合评分=累计R×均R（兼顾总量与质量）")
print(f"{'━'*100}")
sdf["score"] = sdf["cum"] * sdf["mean_r"]
print(f"  {'下界':>6} {'上界':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'PF':>6} {'评分':>8}")
for _, r in sdf.nlargest(10, "score").iterrows():
    print(f"  {r['lo']:>5}% {r['hi']:>5}% {r['n']:>6} {r['cum']:>8.1f} {r['mean_r']:>6.2f} {r['win']:>5.0%} {r['pf']:>6.1f} {r['score']:>8.1f}")
