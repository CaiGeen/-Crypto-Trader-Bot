"""分仓位档位分析v2：只用入场时已知的条件（偏离度/ATR%/前3K/方向）。
确认和突破实体需收盘才知道，不能用于分档。计算加权R+加权最大回撤。
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

kama_vals = kama(df_4h["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy()
n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy()
times = df_4h["open_time"].tolist()

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
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {"dir": d, "exit_idx": i, "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                "confirmed": False, "fill_price": fill_price, "year": int(years[i]), "sig_idx": i,
                "dev": dev, "atr": av, "atr_pct": atr_pct, "pre_3k_pct": pre_3k_pct}
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
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "r": r1+r2, "hold": exit_idx-i, "exit_reason": exit_reason,
            "confirmed": True, "fill_price": fill_price, "year": int(years[i]), "sig_idx": i,
            "dev": dev, "atr": av, "atr_pct": atr_pct, "pre_3k_pct": pre_3k_pct}

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

def weighted_eval(tier_funcs, rdf):
    """计算加权R和加权最大回撤。tier_funcs: {tier: mask_func}。"""
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
    for tier in sorted(tier_funcs.keys(), reverse=True):
        sub = rdf[rdf["tier"] == tier]
        print(f"  {tier}x档: {stats(sub)}  加权R={(sub['r']*tier).sum():.1f}")
    if (rdf["tier"] == 1.0).any() and 1.0 not in tier_funcs:
        sub = rdf[rdf["tier"] == 1.0]
        print(f"  1x档(默认): {stats(sub)}  加权R={sub['r'].sum():.1f}")
    print(f"  → 加权总R={total_wr:.1f}  加权最大回撤={max_dd:.1f}R  （不分档: R={rdf['r'].sum():.1f}/回撤={ (rdf.sort_values('sig_idx')['r'].cumsum() - rdf.sort_values('sig_idx')['r'].cumsum().cummax()).min():.1f}）")
    return total_wr, max_dd

print("=" * 110)
print(f"分仓位档位分析v2（入场时已知条件 only，标记价方案A，{len(rdf)}单）")
print("=" * 110)
print("入场时已知：偏离度/ATR%/前3K涨幅/方向  |  入场时未知：确认/突破实体（需收盘）")

# 入场时已知条件的分组质量
print(f"\n{'━'*110}")
print("一、ATR% 分组（入场时已知）")
print(f"{'━'*110}")
for lo, hi in [(0,1.0),(1.0,1.5),(1.5,2.0),(2.0,100)]:
    print(f"  ATR%[{lo},{hi}): {stats(rdf[(rdf['atr_pct']>=lo)&(rdf['atr_pct']<hi)])}")

print(f"\n{'━'*110}")
print("二、偏离度分组（入场时已知）")
print(f"{'━'*110}")
for lo, hi in [(-2,0),(0,2),(2,4),(4,6),(6,15)]:
    print(f"  dev[{lo}%,{hi}%): {stats(rdf[(rdf['dev']>=lo)&(rdf['dev']<hi)])}")

print(f"\n{'━'*110}")
print("三、前3K涨幅分组（入场时已知）")
print(f"{'━'*110}")
for lo, hi in [(-100,-2),(-2,0),(0,3),(3,100)]:
    print(f"  前3K[{lo},{hi}): {stats(rdf[(rdf['pre_3k_pct']>=lo)&(rdf['pre_3k_pct']<hi)])}")

print(f"\n{'━'*110}")
print("四、方向分组（入场时已知）")
print(f"{'━'*110}")
for d, lbl in [("long","做多"),("short","做空")]:
    print(f"  {lbl}: {stats(rdf[rdf['dir']==d])}")

# 二维交叉：ATR% × 偏离度
print(f"\n{'━'*110}")
print("五、ATR% × 偏离度 交叉（入场时均已知）")
print(f"{'━'*110}")
print(f"  {'':>16} {'ATR%<1.0':>20} {'ATR%[1.0,1.5)':>20} {'ATR%>=1.5':>20}")
for lo, hi in [(-2,0),(0,4),(4,6),(6,15)]:
    parts = [f"  dev[{lo},{hi}):{'':>6}"]
    for alo, ahi in [(0,1.0),(1.0,1.5),(1.5,100)]:
        sub = rdf[(rdf['dev']>=lo)&(rdf['dev']<hi)&(rdf['atr_pct']>=alo)&(rdf['atr_pct']<ahi)]
        parts.append(f"{stats(sub):>20}")
    print(" ".join(parts))

# 分档方案（全部用入场时已知条件）
print(f"\n{'━'*110}")
print("六、分档方案（入场时已知条件，可执行）")
print(f"{'━'*110}")

print("\n【方案A】ATR%三档")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0, 1.5: lambda r: 1.0 <= r["atr_pct"] < 1.5}, rdf)

print("\n【方案B】偏离度三档（高质量区间2x）")
weighted_eval({2: lambda r: (-2 <= r["dev"] < 0) or (4 <= r["dev"] < 6),
               1.5: lambda r: (0 <= r["dev"] < 4)}, rdf)

print("\n【方案C】ATR%+偏离度组合（低波动&高质量偏离→2x）")
weighted_eval({2: lambda r: r["atr_pct"] < 1.5 and ((-2 <= r["dev"] < 0) or (4 <= r["dev"] < 6)),
               1.5: lambda r: r["atr_pct"] < 1.5 or ((-2 <= r["dev"] < 0) or (4 <= r["dev"] < 6))}, rdf)

print("\n【方案D】前3K回调+ATR%（回调&低波动→2x）")
weighted_eval({2: lambda r: r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.5,
               1.5: lambda r: r["pre_3k_pct"] < 0 or r["atr_pct"] < 1.5}, rdf)

print("\n【方案E】方向+ATR%（做多&低波动→2x，做空&低波动→1.5x）")
weighted_eval({2: lambda r: r["dir"] == "long" and r["atr_pct"] < 1.5,
               1.5: lambda r: (r["dir"] == "short" and r["atr_pct"] < 1.5) or (r["dir"] == "long" and r["atr_pct"] >= 1.5)}, rdf)

print("\n【方案F】ATR%<1.0→2x, ATR%[1.0,2.0)→1.5x, ATR%>=2.0→1x")
weighted_eval({2: lambda r: r["atr_pct"] < 1.0, 1.5: lambda r: 1.0 <= r["atr_pct"] < 2.0}, rdf)
