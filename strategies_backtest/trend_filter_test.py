"""复合过滤方案事前过滤对比。
核心思路：[-2%,6%)为高质量核心，对[6%,15%)或[6%,30%)加趋势确认门槛。
事前过滤：不符合条件的信号不占last_exit，每方案独立跑回测。输出对比表+CSV。
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

daily_df = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1d.parquet")
k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
daily_df["date"] = daily_df["open_time"].dt.date
date_to_ema = dict(zip(daily_df["date"], de70))
ema_slope5 = np.zeros(len(daily_df))
for i in range(5, len(daily_df)):
    ema_slope5[i] = (de70[i] - de70[i-5]) / de70[i-5] * 100
date_to_slope = dict(zip(daily_df["date"], ema_slope5))

def get_dev_slope(i):
    if i - 1 < 0: return np.nan, np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    slope_prev = date_to_slope.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan, np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100, slope_prev

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
    dev, ema_slope = get_dev_slope(i)
    pre_3k_pct = np.nan
    if i - 4 >= 0 and closes[i-4] > 0:
        raw = (closes[i-1] - closes[i-4]) / closes[i-4] * 100
        pre_3k_pct = raw if is_long else -raw
    atr_pct = av / closes[i-1] * 100 if i-1 >= 0 else np.nan
    body_atr = np.nan
    if av > 0:
        raw = (closes[i] - opens[i]) / av
        body_atr = raw if is_long else -raw
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

# 过滤方案
def f_core(r): return -2 <= r["dev"] < 6
def f_relax15(r): return -2 <= r["dev"] < 15
def f_body_ema(r):
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["body_atr"] >= 0.5 and r["ema_slope"] >= 1: return True
    return False
def f_ema2(r):
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["ema_slope"] >= 2: return True
    return False
def f_atr_low(r):
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["atr_pct"] < 1.2: return True
    return False
def f_pullback(r):
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0: return True
    return False
def f_body_ema_ext(r):
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 30 and r["body_atr"] >= 0.5 and r["ema_slope"] >= 1: return True
    return False
def f_atr_low_ext(r):
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 30 and r["atr_pct"] < 1.2: return True
    return False
def f_pullback_30(r):
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 30 and r["pre_3k_pct"] < 0: return True
    return False
def f_pullback_or_strong(r):
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 30 and (r["pre_3k_pct"] < 0 or r["body_atr"] >= 1.5): return True
    return False
def f_pullback_atr(r):
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.8: return True
    return False

schemes = [
    ("核心[-2%,6%)", f_core),
    ("放宽[-2%,15%)", f_relax15),
    ("+实体0.5&EMA1(6-15)", f_body_ema),
    ("+EMA>=2(6-15)", f_ema2),
    ("+ATR%<1.2(6-15)", f_atr_low),
    ("+前3K<0(6-15)", f_pullback),
    ("+实体0.5&EMA1(6-30)", f_body_ema_ext),
    ("+ATR%<1.2(6-30)", f_atr_low_ext),
    ("+前3K<0(6-30)", f_pullback_30),
    ("+前3K<0|实体1.5(6-30)", f_pullback_or_strong),
    ("+前3K<0&ATR<1.8(6-15)", f_pullback_atr),
]

def run_scheme(fn):
    """事前过滤：不符合条件的信号不占last_exit"""
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s)
        if r and fn(r):
            results.append(r)
            last_exit = r["exit_idx"]
    return pd.DataFrame(results)

def calc_stats(rdf):
    if rdf.empty: return {}
    sr = rdf["r"]
    w = sr[sr > 0]; l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    rdf_s = rdf.sort_values("sig_idx")
    cum = rdf_s["r"].cumsum()
    max_dd = (cum - cum.cummax()).min()
    return {"单数": len(rdf), "累计R": sr.sum(), "均R": sr.mean(),
            "胜率": (sr>0).mean(), "PF": pf, "最大回撤R": max_dd,
            "做多R": rdf[rdf["dir"]=="long"]["r"].sum(),
            "做空R": rdf[rdf["dir"]=="short"]["r"].sum()}

print("=" * 115)
print("复合过滤方案事前过滤对比（3K突破）")
print("=" * 115)
print(f"数据：{n4h}根4H K线")
print()

print(f"{'━'*115}")
print("一、总览对比（事前过滤：不符合条件不占last_exit）")
print(f"{'━'*115}")
print(f"{'方案':>26} {'单数':>6} {'累计R':>9} {'均R':>6} {'胜率':>6} {'PF':>7} {'最大回撤':>8} {'做多R':>8} {'做空R':>8}")
print("-" * 95)
scheme_stats = []
for name, fn in schemes:
    rdf = run_scheme(fn)
    st = calc_stats(rdf)
    scheme_stats.append((name, rdf, st))
    pf_str = f"{st['PF']:.1f}" if st['PF'] < 1000 else "inf"
    print(f"{name:>26} {st['单数']:>6} {st['累计R']:>9.1f} {st['均R']:>6.2f} {st['胜率']:>5.0%} {pf_str:>7} {st['最大回撤R']:>8.1f} {st['做多R']:>8.1f} {st['做空R']:>8.1f}")

print(f"\n{'━'*115}")
print("二、年度分解对比（核心 / +实体&EMA(6-15) / +实体&EMA(6-30) / 放宽15）")
print(f"{'━'*115}")
key_idx = [0, 2, 6, 1]
print(f"{'年份':>6} | " + " | ".join([f"{schemes[i][0]:>22}" for i in key_idx]))
print("-" * 115)
all_years = sorted(set().union(*[set(rdf["year"].unique()) for _, rdf, _ in scheme_stats if not rdf.empty]))
for yr in all_years:
    parts = [f"{yr:>6} |"]
    for i in key_idx:
        rdf = scheme_stats[i][1]
        sub = rdf[rdf["year"] == yr]
        if sub.empty:
            parts.append(f"{'—':>22} |")
        else:
            sr2 = sub["r"]
            cum = sub.sort_values("sig_idx")["r"].cumsum()
            mdd = (cum - cum.cummax()).min()
            parts.append(f"{len(sub):>4}/{sr2.sum():>6.1f}R/{mdd:>5.1f} |")
    print(" ".join(parts))

print(f"\n{'━'*115}")
print("三、2021大牛市捕获对比")
print(f"{'━'*115}")
print(f"{'方案':>26} {'2021单数':>8} {'2021累计R':>10} {'2021均R':>8} {'2021胜率':>8}")
print("-" * 70)
for name, rdf, st in scheme_stats:
    sub = rdf[rdf["year"] == 2021]
    if sub.empty:
        print(f"{name:>26} {'—':>8}")
    else:
        sr2 = sub["r"]
        print(f"{name:>26} {len(sub):>8} {sr2.sum():>10.1f} {sr2.mean():>8.2f} {(sr2>0).mean():>7.0%}")

out_path = cfg.results_path / "复合过滤方案对比.csv"
with open(out_path, "w", encoding="utf-8-sig") as f:
    f.write("方案,单数,累计R,均R,胜率,PF,最大回撤R,做多R,做空R\n")
    for name, rdf, st in scheme_stats:
        pf_str = f"{st['PF']:.1f}" if st['PF'] < 1000 else "inf"
        nm = name.replace(",", "，")
        f.write(f"{nm},{st['单数']},{st['累计R']:.1f},{st['均R']:.2f},{st['胜率']:.0%},{pf_str},{st['最大回撤R']:.1f},{st['做多R']:.1f},{st['做空R']:.1f}\n")
    for name, rdf, st in scheme_stats:
        nm = name.replace(",", "，")
        f.write(f"\n【{nm}】明细（时间降序）\n")
        f.write("序号,信号时间,方向,成交价,ATR%,EMA70偏离度%,前3K涨幅%,EMA斜率%,突破实体/ATR,R值,出场原因,年份\n")
        if rdf.empty: continue
        rows = []
        for r in rdf.to_dict("records"):
            i = r["sig_idx"]
            sig_t = pd.Timestamp(times[i]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
            rows.append((sig_t, "做多" if r["dir"]=="long" else "做空",
                round(r["fill_price"],2), round(r["atr_pct"],3),
                round(r["dev"],2) if not np.isnan(r["dev"]) else "",
                round(r["pre_3k_pct"],2) if not np.isnan(r["pre_3k_pct"]) else "",
                round(r["ema_slope"],2) if not np.isnan(r["ema_slope"]) else "",
                round(r["body_atr"],2) if not np.isnan(r["body_atr"]) else "",
                round(r["r"],3), r["exit_reason"], int(r["year"])))
        rows.sort(key=lambda x: x[0], reverse=True)
        for k, row in enumerate(rows, 1):
            f.write(f"{k},{row[0]},{row[1]},{row[2]},{row[3]},{row[4]},{row[5]},{row[6]},{row[7]},{row[8]},{row[9]},{row[10]}\n")
print(f"\nCSV已导出: {out_path}")
