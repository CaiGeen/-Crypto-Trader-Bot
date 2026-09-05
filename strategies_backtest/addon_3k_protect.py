"""加仓单3K保护分析：加仓发生在底仓第几K？加仓单是否有自己的3K保护？"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
cache = cfg.base_dir / "cache"
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3
CONFIRM_ATR = 0.5; ADDON_ATR = 1.5

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

def simulate(signal, addon_3k_protect=False):
    """addon_3k_protect=False: 当前（加仓共用底仓3K时间线）
    addon_3k_protect=True: 加仓单有自己的3K保护（加仓后前3K用init_stop，之后用max(KAMA, addon_price)）
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
                "addon_bh": 0, "addon_hold": 0}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; addon_k = -1; addon_bh = 0; sl_at_addon = None
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = init_stop if bh <= 3 else (max(ks, fill_price) if is_long else min(ks, fill_price))
        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= ADDON_ATR*av:
                addon_done = True; addon_price = float(closes[j]); addon_k = j; addon_bh = bh; sl_at_addon = float(sl)
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
    r_addon = 0.0
    if addon_done:
        addon_risk = (addon_price - sl_at_addon) if is_long else (sl_at_addon - addon_price)
        if addon_risk is not None and not np.isnan(addon_risk) and addon_risk > 0:
            r_addon = (exit_price-addon_price)/addon_risk if is_long else (addon_price-exit_price)/addon_risk
    addon_hold = (exit_idx - addon_k) if addon_done else 0
    return {**base, "exit_idx": exit_idx, "r_base": r_base, "r_addon": r_addon,
            "r": r_base + r_addon, "exit_reason": exit_reason, "confirmed": True,
            "addon": addon_done, "addon_bh": addon_bh, "addon_hold": addon_hold,
            "addon_price": addon_price if addon_done else 0.0,
            "fill_price": fill_price, "av": av, "is_long": is_long}

def simulate_addon_3k(signal):
    """加仓单有自己的3K保护：加仓后前3K用addon_init_stop=addon_price-1ATR，之后用max(KAMA, addon_price)"""
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
                "addon_bh": 0, "addon_hold": 0}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; addon_k = -1; addon_bh = 0
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
                addon_done = True; addon_price = float(closes[j]); addon_k = j; addon_bh = bh
        if addon_done and not addon_closed:
            addon_hk = j - addon_k
            addon_init_stop = (addon_price - STOP_MULT*av) if is_long else (addon_price + STOP_MULT*av)
            addon_sl = addon_init_stop if addon_hk <= 3 else (max(ks, addon_price) if is_long else min(ks, addon_price))
            hit_addon_sl = (lows[j] < addon_sl) if is_long else (highs[j] > addon_sl)
            if hit_addon_sl:
                addon_closed = True
                addon_exit_price = float(addon_sl)
                addon_exit_reason = "加仓前3K止损" if addon_hk <= 3 else "加仓KAMA止损"
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
        if addon_closed:
            r_addon = (addon_exit_price-addon_price)/av if is_long else (addon_price-addon_exit_price)/av
        else:
            r_addon = (exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av
            addon_exit_reason = exit_reason
    else:
        r_addon = 0.0
    addon_hold = (exit_idx - addon_k) if addon_done else 0
    return {**base, "exit_idx": exit_idx, "r_base": r_base, "r_addon": r_addon,
            "r": r_base + r_addon, "exit_reason": exit_reason, "confirmed": True,
            "addon": addon_done, "addon_bh": addon_bh, "addon_hold": addon_hold,
            "addon_price": addon_price if addon_done else 0.0,
            "fill_price": fill_price, "av": av, "is_long": is_long,
            "addon_closed": addon_closed, "addon_exit_reason": addon_exit_reason if addon_done else ""}

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

def run_backtest(use_addon_3k=False):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate_addon_3k(s) if use_addon_3k else simulate(s)
        if r and passes(r):
            results.append(r)
            last_exit = r["exit_idx"]
    return pd.DataFrame(results)

rdf_cur = run_backtest(False)
rdf_3k = run_backtest(True)

D = f"{'━'*90}"
print("=" * 90)
print("加仓单3K保护分析")
print("=" * 90)

# 1. 加仓发生时机分布
print(f"\n{D}\n一、加仓发生在底仓第几K？（addon_bh分布）\n{D}")
addon = rdf_cur[rdf_cur["addon"]]
print(f"  共{len(addon)}单加仓")
bh_dist = addon["addon_bh"].value_counts().sort_index()
print(f"  {'底仓第K':>8} {'单数':>6} {'加仓R':>8} {'均R':>6} {'胜率':>6}")
for bh, cnt in bh_dist.items():
    sub = addon[addon["addon_bh"]==bh]
    print(f"  {bh:>8} {cnt:>6} {sub['r_addon'].sum():>8.1f} {sub['r_addon'].mean():>6.2f} {(sub['r_addon']>0).mean():>6.0%}")

# 2. 关键问题：加仓时底仓止损状态
print(f"\n{D}\n二、加仓时底仓止损状态\n{D}")
in_3k = addon[addon["addon_bh"]<=3]
after_3k = addon[addon["addon_bh"]>3]
print(f"  加仓发生在底仓前3K内: {len(in_3k)}单（加仓后还有底仓init_stop保护）")
print(f"    加仓R={in_3k['r_addon'].sum():.1f} 均{in_3k['r_addon'].mean():.2f}")
print(f"  加仓发生在底仓第4K起: {len(after_3k)}单（止损已是max(KAMA,fill)，加仓无独立3K保护）")
print(f"    加仓R={after_3k['r_addon'].sum():.1f} 均{after_3k['r_addon'].mean():.2f}")

# 3. 对比：加仓单有自己的3K保护
print(f"\n{D}\n三、对比：加仓单有自己的3K保护\n{D}")
print(f"  {'方案':>24} {'总单':>5} {'底仓R':>8} {'加仓R':>8} {'总R':>8} {'加仓均R':>8}")
for label, rdf in [("当前(加仓共用底仓3K)", rdf_cur), ("加仓有自己的3K保护", rdf_3k)]:
    ad = rdf[rdf["addon"]]
    print(f"  {label:>24} {len(rdf):>5} {rdf['r_base'].sum():>8.1f} {rdf['r_addon'].sum():>8.1f} "
          f"{rdf['r'].sum():>8.1f} {ad['r_addon'].mean() if len(ad)>0 else 0:>8.2f}")

# E'加权
print(f"\n  E'加权:")
for label, rdf in [("当前(加仓共用底仓3K)", rdf_cur), ("加仓有自己的3K保护", rdf_3k)]:
    rdf = rdf.copy()
    rdf["tier"] = rdf.apply(e_prime_tier, axis=1)
    rdf["wr"] = rdf["r"] * rdf["tier"]
    s = rdf.sort_values("sig_idx"); cum = s["wr"].cumsum()
    dd = (cum - cum.cummax()).min()
    print(f"  {label:>24} 加权R={rdf['wr'].sum():.1f} 回撤={dd:.1f}")

# 4. 加仓有自己的3K保护时，加仓止损触发情况
print(f"\n{D}\n四、加仓3K保护下的加仓出场原因\n{D}")
addon_3k = rdf_3k[rdf_3k["addon"]]
for reason in sorted(addon_3k["addon_exit_reason"].unique()):
    sub = addon_3k[addon_3k["addon_exit_reason"]==reason]
    print(f"  {reason:>14}: {len(sub):>4}单 R={sub['r_addon'].sum():>7.1f} 均{sub['r_addon'].mean():>5.2f}")

# 5. 逐年对比
print(f"\n{D}\n五、逐年加仓R对比\n{D}")
print(f"  {'年份':>6} {'当前加仓R':>10} {'3K保护加仓R':>12} {'差':>8}")
for yr in sorted(rdf_cur["year"].unique()):
    c = rdf_cur[(rdf_cur["year"]==yr)&(rdf_cur["addon"])]["r_addon"].sum()
    k = rdf_3k[(rdf_3k["year"]==yr)&(rdf_3k["addon"])]["r_addon"].sum()
    print(f"  {yr:>6} {c:>10.1f} {k:>12.1f} {k-c:>+8.1f}")
