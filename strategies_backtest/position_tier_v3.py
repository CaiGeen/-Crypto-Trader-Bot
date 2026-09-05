"""增加4H EMA20/60/120三线密集度维度，分析对分档的辅助判断。
密集度 = (max(ema20,ema60,ema120) - min(...)) / ema60 * 100（入场时已知，用i-1值）
spread小=密集(盘整末期)，spread大=分散(趋势中)。
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

closes_4h = df_4h["close"].to_numpy()
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
opens = df_4h["open"].to_numpy(); n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy()
times = df_4h["open_time"].tolist()

# 4H EMA20/60/120
def ema_arr(arr, period):
    k = 2.0 / (period + 1); out = np.zeros(len(arr)); out[0] = arr[0]
    for i in range(1, len(arr)): out[i] = arr[i]*k + out[i-1]*(1-k)
    return out
ema20 = ema_arr(closes_4h, 20)
ema60 = ema_arr(closes_4h, 60)
ema120 = ema_arr(closes_4h, 120)

def get_spread(i):
    """三线密集度(%)，用i-1的EMA值（入场时已知）。"""
    if i - 1 < 0: return np.nan, np.nan
    e20, e60, e120 = ema20[i-1], ema60[i-1], ema120[i-1]
    spread = (max(e20, e60, e120) - min(e20, e60, e120)) / e60 * 100
    # 三线排列：1=多头(e20>e60>e120), -1=空头, 0=缠绕
    if e20 > e60 > e120: align = 1
    elif e20 < e60 < e120: align = -1
    else: align = 0
    return spread, align

kama_vals = kama(closes_4h, cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

# 日线EMA70
k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
date_to_ema = dict(zip(daily_df["open_time"].dt.date, de70))

def get_dev(i):
    if i - 1 < 0: return np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes_4h[i-1] - ema_prev) / ema_prev * 100

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
atr_vals = calc_atr(highs, lows, closes_4h, 20)

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
    confirmed = (closes_4h[i] >= entry_base + 0.5*av) if is_long else (closes_4h[i] <= entry_base - 0.5*av)
    fill_price = float(entry_base) if filled else 0.0
    dev = get_dev(i)
    spread, align = get_spread(i)
    pre_3k_pct = np.nan
    if i - 4 >= 0 and closes_4h[i-4] > 0:
        raw = (closes_4h[i-1] - closes_4h[i-4]) / closes_4h[i-4] * 100
        pre_3k_pct = raw if is_long else -raw
    atr_pct = av / closes_4h[i-1] * 100 if i-1 >= 0 else np.nan
    if not filled: return None
    if not confirmed:
        ep = float(closes_4h[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {"dir": d, "exit_idx": i, "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                "confirmed": False, "fill_price": fill_price, "year": int(years[i]), "sig_idx": i,
                "dev": dev, "atr": av, "atr_pct": atr_pct, "pre_3k_pct": pre_3k_pct,
                "spread": spread, "align": align}
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
            cg_abs = (closes_4h[j]-fill_price) if is_long else (fill_price-closes_4h[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes_4h[j])
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
    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes_4h[-1]); exit_reason = "未平仓"
    r1 = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "r": r1+r2, "hold": exit_idx-i, "exit_reason": exit_reason,
            "confirmed": True, "fill_price": fill_price, "year": int(years[i]), "sig_idx": i,
            "dev": dev, "atr": av, "atr_pct": atr_pct, "pre_3k_pct": pre_3k_pct,
            "spread": spread, "align": align}

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

print("=" * 115)
print(f"EMA20/60/120三线密集度分析（标记价方案A，{len(rdf)}单）")
print("=" * 115)
print("spread = (max-min)/ema60*100，用i-1值（入场时已知）。小=密集(盘整末期)，大=分散(趋势中)")

# 1. 密集度分布
print(f"\n{'━'*115}")
print("一、三线密集度(spread%)分组")
print(f"{'━'*115}")
print(f"  {'spread区间':>16} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6} {'PF':>6} {'胜率':>6}")
for lo, hi in [(0,0.5),(0.5,1.0),(1.0,2.0),(2.0,4.0),(4.0,8.0),(8.0,100)]:
    sub = rdf[(rdf["spread"] >= lo) & (rdf["spread"] < hi)]
    if sub.empty: continue
    sr2 = sub["r"]
    pf = sr2[sr2>0].sum()/abs(sr2[sr2<=0].sum()) if sr2[sr2<=0].sum() != 0 else float("inf")
    print(f"  [{lo:>4.1f},{hi:>4.1f}): {len(sub):>6} {len(sub)/len(rdf):>5.0%} {sr2.sum():>8.1f} {sr2.mean():>6.2f} {pf:>6.1f} {(sr2>0).mean():>5.0%}")

# 2. 三线排列
print(f"\n{'━'*115}")
print("二、三线排列分组")
print(f"{'━'*115}")
for align, lbl in [(1, "多头排列(e20>e60>e120)"), (-1, "空头排列(e20<e60<e120)"), (0, "缠绕交叉")]:
    print(f"  {lbl}: {stats(rdf[rdf['align']==align])}")

# 3. 密集度 × 排列
print(f"\n{'━'*115}")
print("三、密集度 × 排列 交叉")
print(f"{'━'*115}")
print(f"  {'':>24} {'spread<1.0':>22} {'spread[1.0,2.0)':>22} {'spread>=2.0':>22}")
for align, lbl in [(1,"多头"),(-1,"空头"),(0,"缠绕")]:
    parts = [f"  {lbl+'排列':>22}"]
    for slo, shi in [(0,1.0),(1.0,2.0),(2.0,100)]:
        sub = rdf[(rdf["align"]==align)&(rdf["spread"]>=slo)&(rdf["spread"]<shi)]
        parts.append(f"{stats(sub):>22}")
    print(" ".join(parts))

# 4. 密集度 × ATR%
print(f"\n{'━'*115}")
print("四、密集度 × ATR% 交叉（两个入场时已知维度）")
print(f"{'━'*115}")
print(f"  {'':>16} {'ATR%<1.0':>22} {'ATR%[1.0,1.5)':>22} {'ATR%>=1.5':>22}")
for slo, shi in [(0,1.0),(1.0,2.0),(2.0,100)]:
    parts = [f"  spread[{slo},{shi}):{'':>4}"]
    for alo, ahi in [(0,1.0),(1.0,1.5),(1.5,100)]:
        sub = rdf[(rdf["spread"]>=slo)&(rdf["spread"]<shi)&(rdf["atr_pct"]>=alo)&(rdf["atr_pct"]<ahi)]
        parts.append(f"{stats(sub):>22}")
    print(" ".join(parts))

# 5. 密集度 × 偏离度
print(f"\n{'━'*115}")
print("五、密集度 × 偏离度 交叉")
print(f"{'━'*115}")
print(f"  {'':>16} {'spread<1.0':>22} {'spread[1.0,2.0)':>22} {'spread>=2.0':>22}")
for dlo, dhi in [(-2,0),(0,4),(4,6),(6,15)]:
    parts = [f"  dev[{dlo},{dhi}):{'':>8}"]
    for slo, shi in [(0,1.0),(1.0,2.0),(2.0,100)]:
        sub = rdf[(rdf["dev"]>=dlo)&(rdf["dev"]<dhi)&(rdf["spread"]>=slo)&(rdf["spread"]<shi)]
        parts.append(f"{stats(sub):>22}")
    print(" ".join(parts))

# 6. 分档方案：加入密集度
print(f"\n{'━'*115}")
print("六、加入密集度的分档方案")
print(f"{'━'*115}")

def weighted_eval(tier_funcs, rdf):
    rdf = rdf.copy(); rdf["tier"] = 1.0
    for tier, fn in tier_funcs.items():
        rdf.loc[rdf.apply(fn, axis=1), "tier"] = tier
    rdf["wr"] = rdf["r"] * rdf["tier"]
    rdf_s = rdf.sort_values("sig_idx"); cum = rdf_s["wr"].cumsum()
    max_dd = (cum - cum.cummax()).min()
    for tier in sorted(tier_funcs.keys(), reverse=True):
        sub = rdf[rdf["tier"] == tier]
        print(f"  {tier}x档: {stats(sub)}  加权R={(sub['r']*tier).sum():.1f}")
    sub1 = rdf[rdf["tier"] == 1.0]
    if not sub1.empty: print(f"  1x档: {stats(sub1)}  加权R={sub1['r'].sum():.1f}")
    print(f"  → 加权总R={rdf['wr'].sum():.1f}  回撤={max_dd:.1f}R  （不分档: R={rdf['r'].sum():.1f}/回撤={(rdf.sort_values('sig_idx')['r'].cumsum()-rdf.sort_values('sig_idx')['r'].cumsum().cummax()).min():.1f}）")

print("\n【G】ATR%+密集度（低波动&密集→2x）")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 and r["spread"] < 2.0,
               1.5: lambda r: (r["atr_pct"] < 1.5 and r["spread"] < 4.0) or (r["atr_pct"] < 1.0)}, rdf)

print("\n【H】密集度三档（密集→2x）")
weighted_eval({2: lambda r: r["spread"] < 1.0,
               1.5: lambda r: 1.0 <= r["spread"] < 2.0}, rdf)

print("\n【I】排列+密集度（顺向排列&密集→2x）")
weighted_eval({2: lambda r: r["align"] != 0 and r["spread"] < 2.0,
               1.5: lambda r: (r["align"] != 0 and r["spread"] >= 2.0) or (r["align"] == 0 and r["spread"] < 2.0)}, rdf)

print("\n【J】ATR%三档 + 密集度加成（ATR%<1&密集→2x, ATR%<1→1.5x, 其他→1x）")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0 and r["spread"] < 2.0,
               1.5: lambda r: r["atr_pct"] < 1.0}, rdf)
