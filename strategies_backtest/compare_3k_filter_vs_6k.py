"""3K+EMA70偏离度过滤 vs 6K无过滤 并排对比。
对比维度：单数/累计R/均R/PF/胜率/最大回撤/做多R/做空R + 年度分解 + R值分布。
输出：终端对比表 + 对比CSV（时间降序）。
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

# 日线EMA70
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

# 1min消歧
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
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0

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

def simulate(signal, window):
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_nk = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, _ = find_callback_extreme(i, d, window)
    entry_base = cb_extreme if cb_extreme is not None else bt_nk
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)
    fill_price = float(entry_base) if filled else 0.0
    dev = get_dev(i)
    if not filled: return None  # 方案B：未成交跳过，不占last_exit
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {"dir": d, "exit_idx": i, "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                "confirmed": False, "addon": False, "fill_price": fill_price, "exit_price": ep,
                "year": int(years[i]), "sig_idx": i, "dev": dev, "atr": av}
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
            "confirmed": True, "addon": addon_done, "fill_price": fill_price, "exit_price": exit_price,
            "year": int(years[i]), "sig_idx": i, "dev": dev, "atr": av}

def run_backtest(window, dev_rng=None):
    signals = detect_breakout(df_4h, window)
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, window)
        if r:
            if dev_rng is not None:
                if np.isnan(r["dev"]): continue
                if not (dev_rng[0] <= r["dev"] < dev_rng[1]): continue
            results.append(r)
            last_exit = r["exit_idx"]
    return pd.DataFrame(results)

def calc_stats(rdf):
    if rdf.empty: return {}
    sr = rdf["r"]
    w = sr[sr > 0]; l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    rdf_sorted = rdf.sort_values("sig_idx")
    cum = rdf_sorted["r"].cumsum()
    max_dd = (cum - cum.cummax()).min()
    return {"单数": len(rdf), "累计R": sr.sum(), "均R": sr.mean(),
            "胜率": (sr>0).mean(), "PF": pf, "最大回撤R": max_dd,
            "做多R": rdf[rdf["dir"]=="long"]["r"].sum(),
            "做空R": rdf[rdf["dir"]=="short"]["r"].sum(),
            "加仓单数": rdf["addon"].sum()}

strategies = [
    ("6K无过滤", run_backtest(6, None)),
    ("3K+[-2%,6%)", run_backtest(3, (-2, 6))),
    ("3K+[-1%,6%)", run_backtest(3, (-1, 6))),
]

print("=" * 100)
print("3K+EMA70偏离度过滤 vs 6K无过滤 对比")
print("=" * 100)
print(f"数据：{df_4h['open_time'].iloc[0]} ~ {df_4h['open_time'].iloc[-1]}，{n4h}根4H K线")
print()

# 1. 总览对比
print(f"{'━'*100}")
print("一、总览对比")
print(f"{'━'*100}")
print(f"{'策略':>14} {'单数':>6} {'累计R':>9} {'均R':>6} {'胜率':>6} {'PF':>7} {'最大回撤R':>10} {'做多R':>8} {'做空R':>8} {'加仓单':>6}")
print("-" * 90)
for name, rdf in strategies:
    st = calc_stats(rdf)
    pf_str = f"{st['PF']:.1f}" if st['PF'] < 1000 else "inf"
    print(f"{name:>14} {st['单数']:>6} {st['累计R']:>9.1f} {st['均R']:>6.2f} {st['胜率']:>5.0%} {pf_str:>7} {st['最大回撤R']:>10.1f} {st['做多R']:>8.1f} {st['做空R']:>8.1f} {int(st['加仓单数']):>6}")

# 2. 年度分解对比
print(f"\n{'━'*100}")
print("二、年度分解对比")
print(f"{'━'*100}")
all_years = sorted(set().union(*[set(rdf["year"].unique()) for _, rdf in strategies if not rdf.empty]))
print(f"{'年份':>6} | " + " | ".join([f"{name:>18}" for name, _ in strategies]))
print("-" * 100)
for yr in all_years:
    parts = [f"{yr:>6} |"]
    for name, rdf in strategies:
        sub = rdf[rdf["year"] == yr]
        if sub.empty:
            parts.append(f"{'—':>18} |")
        else:
            sr2 = sub["r"]
            cum = sub.sort_values("sig_idx")["r"].cumsum()
            mdd = (cum - cum.cummax()).min()
            parts.append(f"{len(sub):>4}单/{sr2.sum():>6.1f}R/{mdd:>5.1f} |")
    print(" ".join(parts))

# 3. R值分布对比
print(f"\n{'━'*100}")
print("三、R值分布对比")
print(f"{'━'*100}")
print(f"{'R区间':>12} | " + " | ".join([f"{name:>18}" for name, _ in strategies]))
print("-" * 100)
for lo, hi in [(-3,-2),(-2,-1),(-1,0),(0,1),(1,2),(2,5),(5,100)]:
    parts = [f"[{lo},{hi}):{'':>3} |"]
    for name, rdf in strategies:
        sub = rdf[(rdf["r"] >= lo) & (rdf["r"] < hi)]
        if sub.empty:
            parts.append(f"{'—':>18} |")
        else:
            parts.append(f"{len(sub):>4}单/{sub['r'].sum():>6.1f}R/{sub['r'].mean():>5.2f} |")
    print(" ".join(parts))

# 4. 出场方式对比
print(f"\n{'━'*100}")
print("四、出场方式对比")
print(f"{'━'*100}")
all_reasons = sorted(set().union(*[set(rdf["exit_reason"].unique()) for _, rdf in strategies if not rdf.empty]))
print(f"{'出场原因':>14} | " + " | ".join([f"{name:>18}" for name, _ in strategies]))
print("-" * 100)
for reason in all_reasons:
    parts = [f"{reason:>14} |"]
    for name, rdf in strategies:
        sub = rdf[rdf["exit_reason"] == reason]
        if sub.empty:
            parts.append(f"{'—':>18} |")
        else:
            parts.append(f"{len(sub):>4}单/{sub['r'].sum():>6.1f}R/{sub['r'].mean():>5.2f} |")
    print(" ".join(parts))

# 5. 导出对比CSV（每个策略一张表，时间降序）
out_path = cfg.results_path / "3K过滤_vs_6K_对比.csv"
with open(out_path, "w", encoding="utf-8-sig") as f:
    f.write("策略对比总览\n")
    f.write(f"{'策略':>14},{ '单数':>6},{ '累计R':>9},{ '均R':>6},{ '胜率':>6},{ 'PF':>7},{ '最大回撤R':>10},{ '做多R':>8},{ '做空R':>8},{ '加仓单':>6}\n")
    for name, rdf in strategies:
        st = calc_stats(rdf)
        pf_str = f"{st['PF']:.1f}" if st['PF'] < 1000 else "inf"
        nm = name.replace(",", "，")
        f.write(f"{nm},{st['单数']},{st['累计R']:.1f},{st['均R']:.2f},{st['胜率']:.0%},{pf_str},{st['最大回撤R']:.1f},{st['做多R']:.1f},{st['做空R']:.1f},{int(st['加仓单数'])}\n")
    f.write("\n")
    for name, rdf in strategies:
        nm = name.replace(",", "，")
        f.write(f"\n【{nm}】明细（时间降序）\n")
        f.write("序号,信号时间,方向,成交价,ATR,ATR%,EMA70偏离度%,是否确认,出场时间,出场价,持仓K数,R值,出场原因,年份\n")
        if rdf.empty: continue
        rows = []
        for idx, r in rdf.reset_index(drop=True).iterrows():
            i = int(r["sig_idx"])
            sig_t = pd.Timestamp(times[i]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
            exit_t = pd.Timestamp(times[int(r["exit_idx"])]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
            rows.append((sig_t, idx+1, "做多" if r["dir"]=="long" else "做空",
                round(r["fill_price"],2), round(r["atr"],2), round(r["atr"]/r["fill_price"]*100,3),
                round(r["dev"],2) if not np.isnan(r["dev"]) else "",
                "确认" if r["confirmed"] else "弱突破", exit_t, round(r["exit_price"],2),
                int(r["hold"]), round(r["r"],3), r["exit_reason"], int(r["year"])))
        rows.sort(key=lambda x: x[0], reverse=True)
        for k, row in enumerate(rows, 1):
            f.write(f"{k},{row[0]},{row[2]},{row[3]},{row[4]},{row[5]},{row[6]},{row[7]},{row[8]},{row[9]},{row[10]},{row[11]},{row[12]},{row[13]}\n")
print(f"\nCSV已导出: {out_path}")
