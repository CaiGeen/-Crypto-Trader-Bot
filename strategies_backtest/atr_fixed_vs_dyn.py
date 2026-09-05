"""ATR固定值 vs 持仓期间动态调整对比。"""
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

def simulate(signal, atr_mode="fixed"):
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
    if is_long: tp_price = fill_price*(1+TP_LONG)
    else: tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; sl_at_addon = None
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i

        if atr_mode == "dynamic":
            cur_av = atr_vals[j-1] if j-1 >= 0 else av
        else:
            cur_av = av

        if bh <= 3:
            if is_long: sl = fill_price - STOP_MULT * cur_av
            else: sl = fill_price + STOP_MULT * cur_av
        else:
            sl = max(ks, fill_price) if is_long else min(ks, fill_price)

        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= 1.5 * cur_av: addon_done = True; addon_price = float(closes[j]); sl_at_addon = float(sl)

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
    if addon_done:
        addon_risk = (addon_price - sl_at_addon) if is_long else (sl_at_addon - addon_price)
        if addon_risk is None or np.isnan(addon_risk) or addon_risk <= 0:
            r2 = 0.0
        else:
            r2 = ((exit_price-addon_price)/addon_risk if is_long else (addon_price-exit_price)/addon_risk)
    else:
        r2 = 0.0
    return {**base, "exit_idx": exit_idx, "r": r1+r2, "exit_reason": exit_reason, "confirmed": True}

def passes(r):
    if np.isnan(r["dev"]): return False
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.8: return True
    return False

signals = detect_breakout(df_4h, WINDOW)

def run_backtest(atr_mode):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, atr_mode)
        if r and passes(r):
            results.append(r)
            last_exit = r["exit_idx"]
    return pd.DataFrame(results)

rdf_fixed = run_backtest("fixed")
rdf_dyn = run_backtest("dynamic")

def stats(sub):
    if sub.empty: return "—"
    sr = sub["r"]
    w = sr[sr>0].sum(); l = sr[sr<=0].sum()
    pf = f"{w/abs(l):.1f}" if l != 0 else "∞"
    return f"{len(sub):>4}单/{sr.sum():>6.1f}R/均{sr.mean():>4.2f}/PF{pf}/胜{(sr>0).mean():.0%}"

def max_dd(sub):
    s = sub.sort_values("sig_idx"); cum = s["r"].cumsum()
    return (cum - cum.cummax()).min()

def weighted_eval(rdf, tier_funcs, label=""):
    rdf = rdf.copy()
    rdf["tier"] = 1.0
    for tier, fn in tier_funcs.items():
        rdf.loc[rdf.apply(fn, axis=1), "tier"] = tier
    rdf["wr"] = rdf["r"] * rdf["tier"]
    s = rdf.sort_values("sig_idx"); cum = s["wr"].cumsum()
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
print("ATR固定值 vs 持仓期间动态调整")
print("=" * 95)
print("  固定: 止损/加仓用atr_vals[i-1]，持仓期间不变")
print("  动态: 止损/加仓用atr_vals[j-1]，每根K动态更新")
print("  注: KAMA跟踪止损(第4K起)不涉及ATR，两种模式相同")

print(f"\n{D}\n一、回测结果对比\n{D}")
print(f"  固定ATR:  {stats(rdf_fixed)}  回撤={max_dd(rdf_fixed):.1f}R")
print(f"  动态ATR:  {stats(rdf_dyn)}  回撤={max_dd(rdf_dyn):.1f}R")

print(f"\n{D}\n二、出场原因对比\n{D}")
for mode, rdf in [("固定", rdf_fixed), ("动态", rdf_dyn)]:
    print(f"  {mode}:")
    for reason in sorted(rdf["exit_reason"].unique()):
        sub = rdf[rdf["exit_reason"]==reason]
        print(f"    {reason}: {stats(sub)}")

print(f"\n{D}\n三、逐年对比\n{D}")
print(f"  {'年份':>6} {'固定':>30} {'动态':>30}")
for yr in sorted(rdf_fixed["year"].unique()):
    s1 = rdf_fixed[rdf_fixed["year"]==yr]
    s2 = rdf_dyn[rdf_dyn["year"]==yr]
    print(f"  {yr:>6} {stats(s1):>30} {stats(s2):>30}")

print(f"\n{D}\n四、E'分档对比\n{D}")
e_prime = {2: lambda r: r["dir"]=="long" and r["atr_pct"]<2.0,
           1.5: lambda r: r["dir"]=="short" and r["atr_pct"]<2.0}
weighted_eval(rdf_fixed, e_prime, "E' × 固定ATR")
weighted_eval(rdf_dyn, e_prime, "E' × 动态ATR")

print(f"\n{D}\n五、差异较大的单（|R差|>0.5）\n{D}")
m = rdf_fixed[["sig_idx","r"]].merge(rdf_dyn[["sig_idx","r"]], on="sig_idx", suffixes=("_fix","_dyn"))
m["diff"] = m["r_fix"] - m["r_dyn"]
big = m[m["diff"].abs() > 0.5].sort_values("diff", key=abs, ascending=False)
print(f"  共{len(big)}单差异>0.5R（总差异={m['diff'].sum():.1f}R）")
for _, r in big.head(10).iterrows():
    print(f"    sig_idx={r['sig_idx']}  固定R={r['r_fix']:.2f}  动态R={r['r_dyn']:.2f}  差={r['diff']:.2f}")
