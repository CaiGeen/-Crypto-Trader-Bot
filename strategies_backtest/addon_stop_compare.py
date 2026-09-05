"""加仓止损优化对比：当前共用止损 vs 加仓当K最低价 vs 加仓前K最低价 vs 突破线+1.5ATR"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
cache = cfg.base_dir / "cache"
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3
CONFIRM_ATR = 0.5; ADDON_ATR = 1.5; ATR_LEN = 20; EMA_LEN = 70

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

k_ema = 2.0/(EMA_LEN+1); de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
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

def calc_atr(h, l, c, length=ATR_LEN):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, ATR_LEN)

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

def simulate(signal, addon_stop_mode="shared"):
    """addon_stop_mode:
    'shared'  = 当前：加仓单和底仓共用止损
    'addon_k_low' = 加仓当K最低价（做多）/最高价（做空）
    'prev_k_low'  = 加仓前K最低价（做多）/最高价（做空）
    'break_1.5atr' = 突破线+1.5ATR（做多）/突破线-1.5ATR（做空）
    """
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_nk = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, _ = find_callback_extreme(i, d, WINDOW)
    entry_base = cb_extreme if cb_extreme is not None else bt_nk
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
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

    confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {**base, "exit_idx": i, "r_base": r1, "r_addon": 0.0, "r": r1,
                "exit_reason": "弱突破平仓", "confirmed": False, "addon": False,
                "addon_reason": "", "addon_sl": 0.0}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; addon_k = -1; addon_sl = 0.0; sl_at_addon = None
    addon_closed = False; addon_exit_price = 0.0; addon_exit_reason = ""
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = init_stop if bh <= 3 else (max(ks, fill_price) if is_long else min(ks, fill_price))

        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= ADDON_ATR*av:
                addon_done = True; addon_price = float(closes[j]); addon_k = j; sl_at_addon = float(sl)
                if addon_stop_mode == "addon_k_low":
                    addon_sl = float(lows[j]) if is_long else float(highs[j])
                elif addon_stop_mode == "prev_k_low":
                    addon_sl = float(lows[j-1]) if is_long else float(highs[j-1])
                elif addon_stop_mode == "break_1.5atr":
                    addon_sl = entry_base + 1.5*av if is_long else entry_base - 1.5*av
                else:
                    addon_sl = 0.0

        if addon_done and not addon_closed and addon_stop_mode != "shared":
            hit_addon_sl = (lows[j] < addon_sl) if is_long else (highs[j] > addon_sl)
            if hit_addon_sl:
                addon_closed = True
                addon_exit_price = float(addon_sl)
                addon_exit_reason = "加仓止损"

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

    r_base = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av

    if addon_done:
        if addon_stop_mode == "shared":
            addon_risk = (addon_price - sl_at_addon) if is_long else (sl_at_addon - addon_price)
            if addon_risk is None or np.isnan(addon_risk) or addon_risk <= 0:
                r_addon = 0.0
            else:
                r_addon = ((exit_price-addon_price)/addon_risk if is_long else (addon_price-exit_price)/addon_risk)
            addon_exit_reason = exit_reason
        else:
            addon_risk = (addon_price - addon_sl) if is_long else (addon_sl - addon_price)
            if addon_risk is None or np.isnan(addon_risk) or addon_risk <= 0:
                r_addon = 0.0
            elif addon_closed:
                r_addon = (addon_exit_price-addon_price)/addon_risk if is_long else (addon_price-addon_exit_price)/addon_risk
            else:
                addon_exit_price = exit_price
                r_addon = (exit_price-addon_price)/addon_risk if is_long else (addon_price-exit_price)/addon_risk
                addon_exit_reason = exit_reason
    else:
        r_addon = 0.0

    return {**base, "exit_idx": exit_idx, "r_base": r_base, "r_addon": r_addon,
            "r": r_base + r_addon, "exit_reason": exit_reason, "confirmed": True,
            "addon": addon_done, "addon_reason": addon_exit_reason,
            "addon_sl": addon_sl, "addon_price": addon_price if addon_done else 0.0}

def passes(r):
    if np.isnan(r["dev"]): return False
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.8: return True
    return False

def e_prime_tier(r):
    if r["dir"]=="long" and r["atr_pct"]<2.0: return 2.0
    if r["dir"]=="short" and r["atr_pct"]<2.0: return 1.5
    return 1.0

signals = detect_breakout(df_4h, WINDOW)

def run_backtest(mode):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, mode)
        if r and passes(r):
            results.append(r)
            last_exit = r["exit_idx"]
    return pd.DataFrame(results)

modes = {
    "当前(共用止损)": "shared",
    "加仓当K最低价": "addon_k_low",
    "加仓前K最低价": "prev_k_low",
    "突破线+1.5ATR": "break_1.5atr",
}

D = f"{'━'*95}"
print("=" * 95)
print("加仓止损优化对比")
print("=" * 95)

all_results = {}
for label, mode in modes.items():
    all_results[label] = run_backtest(mode)

# 总体对比
print(f"\n{D}\n一、总体对比\n{D}")
print(f"  {'方案':>16} {'总单':>5} {'底仓R':>8} {'加仓R':>8} {'总R':>8} {'加仓单':>6} {'加仓均R':>8} {'加仓胜率':>8}")
for label in modes:
    rdf = all_results[label]
    addon = rdf[rdf["addon"]]
    base_r = rdf["r_base"].sum()
    addon_r = rdf["r_addon"].sum()
    addon_mean = addon["r_addon"].mean() if len(addon)>0 else 0
    addon_wr = (addon["r_addon"]>0).mean() if len(addon)>0 else 0
    print(f"  {label:>16} {len(rdf):>5} {base_r:>8.1f} {addon_r:>8.1f} {rdf['r'].sum():>8.1f} "
          f"{len(addon):>6} {addon_mean:>8.2f} {addon_wr:>8.0%}")

# E'加权对比
print(f"\n{D}\n二、E'加权对比\n{D}")
print(f"  {'方案':>16} {'底仓加权R':>10} {'加仓加权R':>10} {'总加权R':>10} {'回撤':>8}")
for label in modes:
    rdf = all_results[label].copy()
    rdf["tier"] = rdf.apply(e_prime_tier, axis=1)
    rdf["wr_base"] = rdf["r_base"] * rdf["tier"]
    rdf["wr_addon"] = rdf["r_addon"] * rdf["tier"]
    rdf["wr"] = rdf["r"] * rdf["tier"]
    s = rdf.sort_values("sig_idx"); cum = s["wr"].cumsum()
    dd = (cum - cum.cummax()).min()
    print(f"  {label:>16} {rdf['wr_base'].sum():>10.1f} {rdf['wr_addon'].sum():>10.1f} "
          f"{rdf['wr'].sum():>10.1f} {dd:>8.1f}")

# 加仓单详细对比
print(f"\n{D}\n三、加仓单详细对比\n{D}")
for label in modes:
    rdf = all_results[label]
    addon = rdf[rdf["addon"]]
    if addon.empty: continue
    print(f"\n  【{label}】{len(addon)}单加仓")
    if label == "当前(共用止损)":
        reasons = addon["exit_reason"].value_counts()
    else:
        reasons = addon["addon_reason"].value_counts()
    for reason, cnt in reasons.items():
        sub = addon[addon["addon_reason"]==reason] if label != "当前(共用止损)" else addon[addon["exit_reason"]==reason]
        sr = sub["r_addon"]
        wr = (sr>0).mean() if len(sr)>0 else 0
        print(f"    {reason:>12}: {cnt:>4}单 R={sr.sum():>7.1f} 均{sr.mean():>5.2f} 胜{wr:.0%}")
    sr_all = addon["r_addon"]
    print(f"    {'合计':>12}: {len(addon):>4}单 R={sr_all.sum():>7.1f} 均{sr_all.mean():>5.2f} 胜{(sr_all>0).mean():.0%}")

# 加仓止损距离对比
print(f"\n{D}\n四、加仓止损距离对比（加仓价到止损价的ATR距离）\n{D}")
print(f"  {'方案':>16} {'均距离':>8} {'中位数':>8} {'最小':>8} {'最大':>8}")
for label in modes:
    rdf = all_results[label]
    addon = rdf[rdf["addon"] & (rdf["addon_sl"]!=0.0)]
    if addon.empty: continue
    if label == "当前(共用止损)": continue
    dist = ((addon["addon_price"] - addon["addon_sl"]) / addon.apply(
        lambda r: atr_vals[int(r["sig_idx"])-1], axis=1)).abs()
    print(f"  {label:>16} {dist.mean():>8.2f} {dist.median():>8.2f} {dist.min():>8.2f} {dist.max():>8.2f}")

# 逐年加仓R对比
print(f"\n{D}\n五、逐年加仓R对比\n{D}")
print(f"  {'年份':>6}", end="")
for label in modes:
    print(f" {label:>16}", end="")
print()
for yr in sorted(all_results["当前(共用止损)"]["year"].unique()):
    print(f"  {yr:>6}", end="")
    for label in modes:
        rdf = all_results[label]
        sub = rdf[(rdf["year"]==yr) & (rdf["addon"])]
        print(f" {sub['r_addon'].sum():>16.1f}", end="")
    print()
