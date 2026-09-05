"""探索分析：对3K回测的[6%,15%)偏离度区间，按多种趋势确认条件分组，
找出能筛出高质量子集的条件，为复合过滤设计提供依据。
"""
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

# 日线EMA70 + 斜率(5日)
daily_df = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1d.parquet")
k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
daily_df["date"] = daily_df["open_time"].dt.date
date_to_ema = dict(zip(daily_df["date"], de70))
# EMA70的5日斜率：用日线EMA70序列
ema_slope5 = np.zeros(len(daily_df))
for i in range(5, len(daily_df)):
    ema_slope5[i] = (de70[i] - de70[i-5]) / de70[i-5] * 100
date_to_slope = dict(zip(daily_df["date"], ema_slope5))

def get_dev_and_slope(i):
    if i - 1 < 0: return np.nan, np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    slope_prev = date_to_slope.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan, np.nan
    dev = (closes[i-1] - ema_prev) / ema_prev * 100
    return dev, slope_prev

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
    dev, ema_slope = get_dev_and_slope(i)
    # 辅助指标
    pre_3k_pct = np.nan
    if i - 4 >= 0 and closes[i-4] > 0:
        raw = (closes[i-1] - closes[i-4]) / closes[i-4] * 100
        pre_3k_pct = raw if is_long else -raw  # 方向调整：做多取正，做空取反
    atr_pct = av / closes[i-1] * 100 if i-1 >= 0 else np.nan
    body_atr = np.nan
    if av > 0:
        raw = (closes[i] - opens[i]) / av
        body_atr = raw if is_long else -raw  # 方向调整
    if not filled: return None
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {"dir": d, "exit_idx": i, "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                "confirmed": False, "fill_price": fill_price, "year": int(years[i]), "sig_idx": i,
                "dev": dev, "atr": av, "ema_slope": ema_slope, "pre_3k_pct": pre_3k_pct,
                "atr_pct": atr_pct, "body_atr": body_atr}
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
            "dev": dev, "atr": av, "ema_slope": ema_slope, "pre_3k_pct": pre_3k_pct,
            "atr_pct": atr_pct, "body_atr": body_atr}

signals = detect_breakout(df_4h, WINDOW)
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r:
        results.append(r)
        last_exit = r["exit_idx"]
rdf = pd.DataFrame(results)

print("=" * 100)
print("探索分析：[6%,15%)区间趋势确认条件区分度")
print("=" * 100)
print(f"3K全部成交单: {len(rdf)}单  累计R={rdf['r'].sum():.1f}")

# 各偏离度区间基线
print(f"\n{'━'*100}")
print("一、各偏离度区间基线（无额外过滤）")
print(f"{'━'*100}")
print(f"{'偏离度区间':>14} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'PF':>6}")
print("-" * 55)
for lo, hi in [(-100,-2),(-2,0),(0,2),(2,4),(4,6),(6,9),(9,12),(12,15),(15,100)]:
    sub = rdf[(rdf["dev"] >= lo) & (rdf["dev"] < hi)]
    if sub.empty: continue
    sr2 = sub["r"]
    pf = sr2[sr2>0].sum() / abs(sr2[sr2<=0].sum()) if sr2[sr2<=0].sum() != 0 else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    print(f"[{lo:>3}%,{hi:>3}%): {len(sub):>6} {sr2.sum():>8.1f} {sr2.mean():>6.2f} {(sr2>0).mean():>5.0%} {pf_str:>6}")

# 对[6%,15%)区间按各条件分组
target = rdf[(rdf["dev"] >= 6) & (rdf["dev"] < 15)].copy()
print(f"\n{'━'*100}")
print(f"二、[6%,15%)区间共{len(target)}单  累计R={target['r'].sum():.1f}  均R={target['r'].mean():.2f}  胜率={(target['r']>0).mean():.0%}")
print(f"{'━'*100}")

def show_bins(col, label, bins):
    print(f"\n  【{label}】分布与R值（[6%,15%)区间内）")
    print(f"  {'区间':>16} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'PF':>6}")
    print("  " + "-" * 55)
    for lo, hi in bins:
        sub = target[(target[col] >= lo) & (target[col] < hi)]
        if sub.empty: continue
        sr2 = sub["r"]
        pf = sr2[sr2>0].sum() / abs(sr2[sr2<=0].sum()) if sr2[sr2<=0].sum() != 0 else float("inf")
        pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
        print(f"  [{lo:>6.2f},{hi:>6.2f}): {len(sub):>6} {sr2.sum():>8.1f} {sr2.mean():>6.2f} {(sr2>0).mean():>5.0%} {pf_str:>6}")

show_bins("pre_3k_pct", "前3K方向涨幅%", [(-100,-3),(-3,0),(0,3),(3,6),(6,10),(10,20),(20,100)])
show_bins("ema_slope", "EMA70 5日斜率%", [(-100,-1),(-1,0),(0,1),(1,3),(3,5),(5,100)])
show_bins("atr_pct", "ATR%", [(0,0.8),(0.8,1.2),(1.2,1.8),(1.8,2.5),(2.5,3.5),(3.5,100)])
show_bins("body_atr", "突破K方向实体/ATR", [(-100,0),(0,0.3),(0.3,0.6),(0.6,1.0),(1.0,1.5),(1.5,100)])

# 组合条件探索：前3K涨幅 + EMA斜率
print(f"\n{'━'*100}")
print("三、组合条件探索（[6%,15%)区间内）")
print(f"{'━'*100}")
combos = [
    ("前3K涨幅>=3%", target[target["pre_3k_pct"] >= 3]),
    ("前3K涨幅>=5%", target[target["pre_3k_pct"] >= 5]),
    ("EMA斜率>=1%", target[target["ema_slope"] >= 1]),
    ("EMA斜率>=2%", target[target["ema_slope"] >= 2]),
    ("前3K>=3% & EMA>=1%", target[(target["pre_3k_pct"] >= 3) & (target["ema_slope"] >= 1)]),
    ("前3K>=5% & EMA>=1%", target[(target["pre_3k_pct"] >= 5) & (target["ema_slope"] >= 1)]),
    ("前3K>=3% & ATR%<2", target[(target["pre_3k_pct"] >= 3) & (target["atr_pct"] < 2)]),
    ("实体>=0.5ATR & 前3K>=3%", target[(target["body_atr"] >= 0.5) & (target["pre_3k_pct"] >= 3)]),
    ("实体>=0.5ATR & EMA>=1%", target[(target["body_atr"] >= 0.5) & (target["ema_slope"] >= 1)]),
]
print(f"  {'条件':>24} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'PF':>6}")
print("  " + "-" * 60)
for label, sub in combos:
    if sub.empty: print(f"  {label:>24} {'—':>6}"); continue
    sr2 = sub["r"]
    pf = sr2[sr2>0].sum() / abs(sr2[sr2<=0].sum()) if sr2[sr2<=0].sum() != 0 else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    print(f"  {label:>24} {len(sub):>6} {sr2.sum():>8.1f} {sr2.mean():>6.2f} {(sr2>0).mean():>5.0%} {pf_str:>6}")

# 模拟复合过滤的整体效果：[-2%,6%) OR ([6%,15%) AND 条件)
print(f"\n{'━'*100}")
print("四、复合过滤整体效果：[-2%,6%) OR ([6%,15%) AND 条件)")
print(f"{'━'*100}")
core = rdf[(rdf["dev"] >= -2) & (rdf["dev"] < 6)]
core_w = core["r"][core["r"] > 0].sum(); core_l = core["r"][core["r"] <= 0].sum()
core_pf = core_w / abs(core_l) if core_l != 0 else float("inf")
print(f"  核心[-2%,6%): {len(core)}单  累计R={core['r'].sum():.1f}  均R={core['r'].mean():.2f}  PF={core_pf:.1f}")
print()
print(f"  {'复合条件(6-15%部分)':>28} {'总单数':>6} {'总累计R':>8} {'均R':>6} {'胜率':>6} {'PF':>6} {'vs纯核心+R':>10}")
print("  " + "-" * 75)
for label, ext in combos:
    full = pd.concat([core, ext])
    sr2 = full["r"]
    pf = sr2[sr2>0].sum() / abs(sr2[sr2<=0].sum()) if sr2[sr2<=0].sum() != 0 else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    delta_r = full["r"].sum() - core["r"].sum()
    print(f"  {label:>28} {len(full):>6} {sr2.sum():>8.1f} {sr2.mean():>6.2f} {(sr2>0).mean():>5.0%} {pf_str:>6} {delta_r:>+10.1f}")
