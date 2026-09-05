"""测试新过滤条件：dev>2不做空，-2<dev<0不做多。对比方案A vs 方案A+新过滤。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
cache = cfg.base_dir / "cache"
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3

df_4h = pd.read_parquet(cache / "BTCUSDTUSDT_4h_mark.parquet")
df_1m = pd.read_parquet(cache / "BTCUSDT_1m_mark.parquet")
daily_df = pd.read_parquet(cache / "BTCUSDT_1d_mark.parquet")
for df in (df_4h, df_1m, daily_df):
    for col in ["open","high","low","close","volume"]:
        if col in df.columns: df[col] = df[col].astype(float)
    if "open_time" not in df.columns:
        df["open_time"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)

kama_vals = kama(df_4h["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy(); times = df_4h["open_time"].tolist()
ms_4h = df_4h["open_time_ms"].to_numpy()

k_ema = 2.0/71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
date_to_ema = dict(zip(daily_df["open_time"].dt.date, de70))

def get_dev(i):
    if i-1 < 0: return np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100

ms_1m = df_1m["open_time_ms"].to_numpy()
h1m = df_1m["high"].to_numpy(); l1m = df_1m["low"].to_numpy()
four_h_ms = 4*60*60*1000
min_start_idx = np.searchsorted(ms_1m, ms_4h)
min_end_idx = np.searchsorted(ms_1m, ms_4h + four_h_ms)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)

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
    pre_3k_pct = np.nan
    if i - 4 >= 0 and closes[i-4] > 0:
        raw = (closes[i-1] - closes[i-4]) / closes[i-4] * 100
        pre_3k_pct = raw if is_long else -raw
    atr_pct = av / closes[i-1] * 100 if i-1 >= 0 else np.nan
    if not filled: return None
    base = {"dir": d, "year": int(years[i]), "sig_idx": i, "dev": dev,
            "atr_pct": atr_pct, "pre_3k_pct": pre_3k_pct}
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {**base, "exit_idx": i, "r": r1, "exit_reason": "弱突破平仓", "confirmed": False}
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
                    exit_idx = j; exit_price = ep; exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
                else:
                    exit_idx = j; exit_price = ep; exit_reason = "止盈"
                break
            ep = float(sl); exit_idx = j; exit_price = ep; exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_stop:
            ep = float(sl); exit_idx = j; exit_price = ep; exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); exit_reason = "止盈"; break
    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1]); exit_reason = "未平仓"
    r1 = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {**base, "exit_idx": exit_idx, "r": r1+r2, "exit_reason": exit_reason, "confirmed": True}

def passes_a(r):
    if np.isnan(r["dev"]): return False
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.8: return True
    return False

def passes_a_dev(r):
    if not passes_a(r): return False
    if r["dir"] == "long" and r["dev"] <= -2: return False
    if r["dir"] == "short" and r["dev"] >= 2: return False
    return True

signals = detect_breakout(df_4h, WINDOW)
sim_cache = {}
for s in signals:
    r = simulate(s)
    if r: sim_cache[s.idx] = r

def run_passes(pass_fn):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = sim_cache.get(s.idx)
        if r and pass_fn(r):
            results.append(r)
            last_exit = r["exit_idx"]
    return pd.DataFrame(results)

rdf_a = run_passes(passes_a)
rdf_new = run_passes(passes_a_dev)

def stats(sub):
    if sub.empty: return "—"
    sr = sub["r"]
    w = sr[sr > 0].sum(); l = sr[sr <= 0].sum()
    pf = w / abs(l) if l != 0 else float("inf")
    return f"{len(sub):>4}单/{sr.sum():>6.1f}R/均{sr.mean():>4.2f}/PF{pf:.1f}/胜{(sr>0).mean():.0%}"

def max_dd(sub):
    s = sub.sort_values("sig_idx")
    cum = s["r"].cumsum()
    return (cum - cum.cummax()).min()

def weighted_eval(rdf, tier_funcs, label=""):
    rdf = rdf.copy()
    rdf["tier"] = 1.0
    for tier, fn in tier_funcs.items():
        rdf.loc[rdf.apply(fn, axis=1), "tier"] = tier
    rdf["wr"] = rdf["r"] * rdf["tier"]
    s = rdf.sort_values("sig_idx")
    cum = s["wr"].cumsum()
    dd = (cum - cum.cummax()).min()
    if label: print(f"\n【{label}】")
    for tier in sorted(tier_funcs.keys(), reverse=True):
        sub = rdf[rdf["tier"] == tier]
        print(f"  {tier}x: {stats(sub)}  加权R={(sub['r']*tier).sum():.1f}")
    if (rdf["tier"] == 1.0).any() and 1.0 not in tier_funcs:
        sub = rdf[rdf["tier"] == 1.0]
        print(f"  1x: {stats(sub)}  加权R={sub['r'].sum():.1f}")
    print(f"  → 加权R={rdf['wr'].sum():.1f}  回撤={dd:.1f}R")
    return rdf["wr"].sum(), dd

D = f"{'━'*95}"
print("=" * 95)
print(f"新过滤测试: dev>-2才做多, dev<2才做空")
print("=" * 95)

print(f"\n{D}\n一、过滤效果对比\n{D}")
print(f"  方案A:           {stats(rdf_a)}  回撤={max_dd(rdf_a):.1f}R")
print(f"  方案A+新过滤:    {stats(rdf_new)}  回撤={max_dd(rdf_new):.1f}R")
print(f"  少做: {len(rdf_a)-len(rdf_new)}单  R变化: {rdf_new['r'].sum()-rdf_a['r'].sum():.1f}R")

filtered_long = rdf_a[(rdf_a['dir']=='long') & (rdf_a['dev'] <= -2)]
filtered_short = rdf_a[(rdf_a['dir']=='short') & (rdf_a['dev'] >= 2)]
print(f"\n  被过滤的单:")
print(f"    做多&dev<=-2: {stats(filtered_long)}")
print(f"    做空&dev>=2:   {stats(filtered_short)}")
print(f"    合计: {len(filtered_long)+len(filtered_short)}单  R={filtered_long['r'].sum()+filtered_short['r'].sum():.1f}")

print(f"\n{D}\n二、方向明细对比\n{D}")
for d, lbl in [("long","做多"),("short","做空")]:
    print(f"  {lbl} 方案A: {stats(rdf_a[rdf_a['dir']==d])}")
    print(f"  {lbl} 新过滤: {stats(rdf_new[rdf_new['dir']==d])}")

print(f"\n{D}\n三、E'分档对比\n{D}")
e_prime = {2: lambda r: r["dir"]=="long" and r["atr_pct"]<2.0,
           1.5: lambda r: r["dir"]=="short" and r["atr_pct"]<2.0}
weighted_eval(rdf_a, e_prime, "E' × 方案A")
weighted_eval(rdf_new, e_prime, "E' × 方案A+新过滤")

print(f"\n{D}\n四、逐年对比（方案A+新过滤）\n{D}")
print(f"  {'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6}")
for yr in sorted(rdf_new["year"].unique()):
    sub = rdf_new[rdf_new["year"]==yr]
    print(f"  {yr:>6} {len(sub):>6} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f} {(sub['r']>0).mean():>6.0%}")
