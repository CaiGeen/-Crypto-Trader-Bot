"""分档分析v4：探索成交量/波动率regime/资金费率/OI等入场时已知的辅助维度。
基于标记价方案A，与纯ATR%三档(1465.5R/-9.3)对比。
"""
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
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns: df[col] = df[col].astype(float)
    if "open_time" not in df.columns:
        df["open_time"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)

df_4h_latest = pd.read_parquet(cache / "BTCUSDT_4h.parquet")
for col in ["open", "high", "low", "close", "volume"]:
    if col in df_4h_latest.columns: df_4h_latest[col] = df_4h_latest[col].astype(float)
vol_map = dict(zip(df_4h_latest["open_time_ms"].to_numpy(), df_4h_latest["volume"].to_numpy()))

fund_df = pd.read_csv(cache / "funding_BTCUSDT.csv")
fund_ts = fund_df["ts"].to_numpy()
fund_rate = fund_df["rate"].to_numpy()

oi_df = pd.read_parquet(cache / "metrics_oi_BTCUSDT.parquet")
oi_ts = oi_df["ts"].to_numpy()
oi_vals = oi_df["oi"].to_numpy()

kama_vals = kama(df_4h["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy()
n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy()
times = df_4h["open_time"].tolist()
ms_4h = df_4h["open_time_ms"].to_numpy()

k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
date_to_ema = dict(zip(daily_df["open_time"].dt.date, de70))

def get_dev(i):
    if i - 1 < 0: return np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100

ms_1m = df_1m["open_time_ms"].to_numpy()
h1m = df_1m["high"].to_numpy(); l1m = df_1m["low"].to_numpy()
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

volumes = np.array([vol_map.get(ms_4h[i], np.nan) for i in range(n4h)])

def get_vol_ratio(i):
    if i < 20: return np.nan
    past = volumes[i-20:i]
    past = past[~np.isnan(past)]
    if len(past) == 0 or np.nanmean(past) == 0: return np.nan
    if np.isnan(volumes[i]): return np.nan
    return volumes[i] / np.nanmean(past)

def get_pre3k_vol_ratio(i):
    if i < 23: return np.nan
    s1 = volumes[i-3:i]; s2 = volumes[i-23:i-3]
    s1 = s1[~np.isnan(s1)]; s2 = s2[~np.isnan(s2)]
    if len(s1) == 0 or len(s2) == 0 or np.nansum(s2) == 0: return np.nan
    return np.nansum(s1) / np.nansum(s2)

def get_atr_pctile(i):
    if i < 500: return np.nan
    window = atr_vals[i-500:i]
    if np.isnan(atr_vals[i-1]): return np.nan
    return np.mean(window <= atr_vals[i-1]) * 100

def get_atr_change(i):
    if i < 6: return np.nan
    if atr_vals[i-6] == 0 or np.isnan(atr_vals[i-6]) or np.isnan(atr_vals[i-1]): return np.nan
    return atr_vals[i-1] / atr_vals[i-6] - 1

def get_funding(i):
    if i == 0: return np.nan, np.nan
    t = ms_4h[i]
    idx = np.searchsorted(fund_ts, t, side="right") - 1
    if idx < 0: return np.nan, np.nan
    cur = fund_rate[idx]
    start = max(0, idx - 7)
    mean8 = np.mean(fund_rate[start:idx+1])
    return cur, mean8

def get_oi_change(i):
    if i == 0: return np.nan, np.nan
    t = ms_4h[i]
    idx = np.searchsorted(oi_ts, t, side="right") - 1
    if idx < 48: return np.nan, np.nan
    cur = oi_vals[idx]
    oi_1h = oi_vals[idx - 12]
    oi_4h = oi_vals[idx - 48]
    ch1h = (cur - oi_1h) / oi_1h * 100 if oi_1h != 0 else np.nan
    ch4h = (cur - oi_4h) / oi_4h * 100 if oi_4h != 0 else np.nan
    return ch1h, ch4h

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
    vr = get_vol_ratio(i); p3vr = get_pre3k_vol_ratio(i)
    ap = get_atr_pctile(i); ac = get_atr_change(i)
    fr, fm = get_funding(i); oic1, oic4 = get_oi_change(i)
    base = {"dir": d, "year": int(years[i]), "sig_idx": i, "dev": dev, "atr": av,
            "atr_pct": atr_pct, "pre_3k_pct": pre_3k_pct, "vol_ratio": vr,
            "pre3k_vol_ratio": p3vr, "atr_pctile": ap, "atr_change": ac,
            "funding": fr, "funding_mean8": fm, "oi_change_1h": oic1, "oi_change_4h": oic4}
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {**base, "exit_idx": i, "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                "confirmed": False, "fill_price": fill_price}
    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; sl_at_addon = None
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = init_stop if bh <= 3 else (max(ks, fill_price) if is_long else min(ks, fill_price))
        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j]); sl_at_addon = float(sl)
        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)
        if hit_stop and hit_tp:
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep = result
                if reason_1m == "stop":
                    exit_idx = j; exit_price = ep
                    exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
                else:
                    exit_idx = j; exit_price = ep; exit_reason = "止盈"
                break
            ep = float(sl); exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_stop:
            ep = float(sl); exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
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
    return {**base, "exit_idx": exit_idx, "r": r1+r2, "hold": exit_idx-i, "exit_reason": exit_reason,
            "confirmed": True, "fill_price": fill_price}

def passes(r):
    if np.isnan(r["dev"]): return False
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.8: return True
    return False

signals = detect_breakout(df_4h, WINDOW)
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r and passes(r):
        results.append(r)
        last_exit = r["exit_idx"]
rdf = pd.DataFrame(results)

def stats(sub):
    if sub.empty: return "—"
    sr = sub["r"]
    w = sr[sr > 0].sum(); l = sr[sr <= 0].sum()
    pf = w / abs(l) if l != 0 else float("inf")
    return f"{len(sub):>4}单/{sr.sum():>6.1f}R/均{sr.mean():>4.2f}/PF{pf:.1f}/胜{(sr>0).mean():.0%}"

def weighted_eval(tier_funcs, rdf, label=""):
    rdf = rdf.copy()
    rdf["tier"] = 1.0
    for tier, fn in tier_funcs.items():
        mask = rdf.apply(fn, axis=1)
        rdf.loc[mask, "tier"] = tier
    rdf["wr"] = rdf["r"] * rdf["tier"]
    rdf_s = rdf.sort_values("sig_idx")
    cum = rdf_s["wr"].cumsum()
    max_dd = (cum - cum.cummax()).min()
    total_wr = rdf["wr"].sum()
    if label: print(f"\n【{label}】")
    for tier in sorted(tier_funcs.keys(), reverse=True):
        sub = rdf[rdf["tier"] == tier]
        print(f"  {tier}x档: {stats(sub)}  加权R={(sub['r']*tier).sum():.1f}")
    if (rdf["tier"] == 1.0).any() and 1.0 not in tier_funcs:
        sub = rdf[rdf["tier"] == 1.0]
        print(f"  1x档(默认): {stats(sub)}  加权R={sub['r'].sum():.1f}")
    base_dd = (rdf.sort_values("sig_idx")["r"].cumsum() - rdf.sort_values("sig_idx")["r"].cumsum().cummax()).min()
    print(f"  → 加权总R={total_wr:.1f}  回撤={max_dd:.1f}R  （不分档: R={rdf['r'].sum():.1f}/回撤={base_dd:.1f}）")
    return total_wr, max_dd

D = f"{'━'*110}"
print("=" * 110)
print(f"分档分析v4（辅助维度探索，标记价方案A，{len(rdf)}单）")
print("=" * 110)
print("已有维度: ATR%/偏离度/前3K/方向 | 新探索: 成交量/波动率regime/资金费率/OI")
print(f"\n基准——纯ATR%三档: 加权R=1465.5/回撤=-9.3")

print(f"\n{D}\n一、成交量维度\n{D}")
print(f"  信号K量比统计: min={rdf['vol_ratio'].min():.2f} median={rdf['vol_ratio'].median():.2f} max={rdf['vol_ratio'].max():.2f}")
for lo, hi in [(0,0.8),(0.8,1.2),(1.2,2.0),(2.0,5.0),(5.0,999)]:
    print(f"  量比[{lo},{hi}): {stats(rdf[(rdf['vol_ratio']>=lo)&(rdf['vol_ratio']<hi)])}")
print(f"\n  前3K累计量比:")
for lo, hi in [(0,0.8),(0.8,1.0),(1.0,1.5),(1.5,999)]:
    print(f"  前3K量比[{lo},{hi}): {stats(rdf[(rdf['pre3k_vol_ratio']>=lo)&(rdf['pre3k_vol_ratio']<hi)])}")

print(f"\n{D}\n二、波动率regime\n{D}")
print(f"  ATR分位数(过去500根):")
for lo, hi in [(0,20),(20,40),(40,60),(60,80),(80,101)]:
    print(f"  分位[{lo},{hi}): {stats(rdf[(rdf['atr_pctile']>=lo)&(rdf['atr_pctile']<hi)])}")
print(f"\n  ATR变化率(6根):")
for lo, hi in [(-1,-0.1),(-0.1,0.0),(0.0,0.1),(0.1,1.0)]:
    print(f"  ATR变化[{lo},{hi}): {stats(rdf[(rdf['atr_change']>=lo)&(rdf['atr_change']<hi)])}")

print(f"\n{D}\n三、资金费率\n{D}")
print(f"  当前资金费率(入场前最近):")
for lo, hi in [(-1,-0.0003),(-0.0003,-0.0001),(-0.0001,0.0001),(0.0001,0.0003),(0.0003,1)]:
    print(f"  费率[{lo:.4f},{hi:.4f}): {stats(rdf[(rdf['funding']>=lo)&(rdf['funding']<hi)])}")
print(f"\n  资金费率×方向(正=顺势,负=逆势):")
rdf2 = rdf.copy()
rdf2["fr_align"] = rdf2.apply(lambda r: r["funding"] * (1 if r["dir"]=="long" else -1), axis=1)
for lo, hi in [(-1,-0.0001),(-0.0001,0.0001),(0.0001,1)]:
    print(f"  顺逆[{lo:.4f},{hi:.4f}): {stats(rdf2[(rdf2['fr_align']>=lo)&(rdf2['fr_align']<hi)])}")

print(f"\n{D}\n四、OI(未平仓合约)变化\n{D}")
oi_valid = rdf.dropna(subset=["oi_change_4h"])
print(f"  有效OI数据: {len(oi_valid)}/{len(rdf)}单")
for lo, hi in [(-100,-2),(-2,0),(0,2),(2,100)]:
    print(f"  OI 4H变化[{lo}%,{hi}%): {stats(rdf[(rdf['oi_change_4h']>=lo)&(rdf['oi_change_4h']<hi)])}")

print(f"\n{D}\n五、组合分档 vs 纯ATR%三档\n{D}")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0, 1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5}, rdf, "基准: 纯ATR%三档")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 and r["vol_ratio"] < 2.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5}, rdf, "A: ATR%三档+量比<2升2x")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 and r["atr_pctile"] < 60,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5}, rdf, "B: ATR%三档+ATR分位<60升2x")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 and r["vol_ratio"] >= 1.2}, rdf, "C: ATR%三档+放量中档升1.5x")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 and r["atr_change"] < 0.1,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5}, rdf, "D: ATR%三档+ATR不扩张升2x")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5,
               0.5: lambda r: r["atr_pct"] >= 1.5 and r["atr_pctile"] > 80}, rdf, "E: ATR%三档+极高波动降0.5x")

print(f"\n{D}\n六、扩大2x范围的组合分档\n{D}")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or r["vol_ratio"] >= 2.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 and r["vol_ratio"] < 2.0}, rdf, "F: ATR%<1或量比>=2→2x")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or r["atr_pctile"] < 20,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 and r["atr_pctile"] >= 20}, rdf, "G: ATR%<1或ATR分位<20→2x")
weighted_eval({2: lambda r: r["vol_ratio"] >= 2.0,
               1.5: lambda r: 1.2 <= r["vol_ratio"] < 2.0}, rdf, "H: 纯量比三档(>=2→2x,1.2-2→1.5x)")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or r["vol_ratio"] >= 2.0,
               1.5: lambda r: (1.0 <= r["atr_pct"] < 1.5) or (r["atr_pct"] >= 1.5 and 1.2 <= r["vol_ratio"] < 2.0)}, rdf, "I: ATR%<1或量比>=2→2x,中档+放量→1.5x")
weighted_eval({2: lambda r: r["vol_ratio"] >= 1.2 and r["atr_pct"] < 1.5,
               1.5: lambda r: r["vol_ratio"] >= 1.2 and r["atr_pct"] >= 1.5}, rdf, "J: 放量(>=1.2)&低波动→2x")

print(f"\n{D}\n七、量比×ATR% 交叉表\n{D}")
print(f"  {'':>16} {'量比<1.2':>22} {'量比[1.2,2)':>22} {'量比>=2.0':>22}")
for lo, hi in [(0,1.0),(1.0,1.5),(1.5,100)]:
    parts = [f"  ATR%[{lo},{hi}):{'':>6}"]
    for vlo, vhi in [(0,1.2),(1.2,2.0),(2.0,999)]:
        sub = rdf[(rdf['atr_pct']>=lo)&(rdf['atr_pct']<hi)&(rdf['vol_ratio']>=vlo)&(rdf['vol_ratio']<vhi)]
        parts.append(f"{stats(sub):>22}")
    print(" ".join(parts))

print(f"\n{D}\n八、缩量降档精细化\n{D}")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or r["vol_ratio"] >= 2.0,
               1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5 and r["vol_ratio"] < 2.0,
               0.5: lambda r: r["atr_pct"] >= 1.5 and r["vol_ratio"] < 1.2}, rdf, "K: F基础上高波动+缩量→0.5x")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or r["vol_ratio"] >= 2.0,
               1.5: lambda r: not (r["atr_pct"] < 1.0 or r["vol_ratio"] >= 2.0) and r["vol_ratio"] >= 0.8,
               0.5: lambda r: r["vol_ratio"] < 0.8}, rdf, "L: F基础上极缩量→0.5x")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 or r["vol_ratio"] >= 2.0,
               1.5: lambda r: not (r["atr_pct"] < 1.0 or r["vol_ratio"] >= 2.0) and not (r["atr_pct"] >= 1.5 and r["vol_ratio"] < 1.2),
               0.5: lambda r: r["atr_pct"] >= 1.5 and r["vol_ratio"] < 1.2}, rdf, "M: F+高波动缩量→0.5x,其余中档→1.5x")
weighted_eval({2: lambda r: (r["atr_pct"] < 1.0 and r["vol_ratio"] >= 1.2) or r["vol_ratio"] >= 2.0,
               1.5: lambda r: not ((r["atr_pct"] < 1.0 and r["vol_ratio"] >= 1.2) or r["vol_ratio"] >= 2.0) and r["vol_ratio"] >= 0.8,
               0.5: lambda r: r["vol_ratio"] < 0.8}, rdf, "N: 低波动+放量→2x,极缩量→0.5x")

rdf.to_parquet(cache / "tier_v4_rdf.parquet", index=False)
print(f"\n→ rdf已保存到 cache/tier_v4_rdf.parquet ({len(rdf)}行)")
