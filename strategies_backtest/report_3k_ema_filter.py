"""3K + EMA70偏离度过滤。先看偏离度分布，再对比有无过滤的效果。"""
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
    """突破K前一K收盘价 vs 日线EMA70偏离度"""
    if i - 1 < 0: return np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100

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
    dev = get_dev(i)

    if not filled:
        return None  # 3K未成交=0，直接跳过

    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {"dir": d, "exit_idx": i, "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                "confirmed": False, "addon": False, "fill_price": fill_price, "exit_price": ep,
                "year": int(years[i]), "sig_idx": i, "dev": dev, "atr": av}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None; ambig = False

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
            ambig = True
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep = result
                if reason_1m == "stop":
                    exit_idx = j; exit_price = ep; exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
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

signals = detect_breakout(df_4h, WINDOW)
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r:
        results.append(r)
        last_exit = r["exit_idx"]

rdf = pd.DataFrame(results)
sr = rdf["r"]

print("=" * 110)
print(f"{WINDOW}K突破 + EMA70偏离度分析")
print("=" * 110)
print(f"3K无过滤: {len(rdf)}单  累计R={sr.sum():.1f}  均R={sr.mean():.2f}  胜率={(sr>0).mean():.0%}")
print()

# 1. 偏离度分布
print(f"{'━'*110}")
print("一、偏离度分布（突破K前一K收盘价 vs 日线EMA70）")
print(f"{'━'*110}")
dev = rdf["dev"].dropna()
print(f"  均值={dev.mean():.2f}%  中位={dev.median():.2f}%  范围=[{dev.min():.1f}%, {dev.max():.1f}%]")
print()
print(f"  {'偏离度区间':>14} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6} {'胜率':>6}")
print("  " + "-" * 60)
for lo, hi in [(-100,-5),(-5,0),(0,5),(5,10),(10,15),(15,25),(25,50),(50,200)]:
    sub = rdf[(rdf["dev"] >= lo) & (rdf["dev"] < hi)]
    if sub.empty: continue
    print(f"  [{lo:>3}%,{hi:>3}%): {len(sub):>6} {len(sub)/len(rdf):>5.0%} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f} {(sub['r']>0).mean():>5.0%}")

# 2. 过滤效果对比
print(f"\n{'━'*110}")
print("二、EMA70偏离度过滤效果")
print(f"{'━'*110}")

filters = [
    ("无过滤", None),
    ("[-5%, 15%)", (-5, 15)),
    ("[-5%, 10%)", (-5, 10)),
    ("[-5%, 20%)", (-5, 20)),
    ("[0%, 15%)", (0, 15)),
    ("[-10%, 15%)", (-10, 15)),
]

print(f"  {'过滤条件':>14} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'PF':>6} {'做多R':>8} {'做空R':>8}")
print("  " + "-" * 90)
for label, rng in filters:
    if rng is None:
        sub = rdf
    else:
        sub = rdf[(rdf["dev"] >= rng[0]) & (rdf["dev"] < rng[1])]
    if sub.empty: continue
    sr2 = sub["r"]
    pf = sr2[sr2>0].sum() / abs(sr2[sr2<=0].sum()) if sr2[sr2<=0].sum() != 0 else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    long_r = sub[sub["dir"]=="long"]["r"].sum()
    short_r = sub[sub["dir"]=="short"]["r"].sum()
    print(f"  {label:>14} {len(sub):>6} {len(sub)/len(rdf):>5.0%} {sr2.sum():>8.1f} {sr2.mean():>6.2f} {(sr2>0).mean():>5.0%} {pf_str:>6} {long_r:>8.1f} {short_r:>8.1f}")

# 3. 最优过滤的年度分解
print(f"\n{'━'*110}")
print("三、[-5%, 15%)过滤后年度分解")
print(f"{'━'*110}")
sub = rdf[(rdf["dev"] >= -5) & (rdf["dev"] < 15)]
print(f"  {'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6}")
print("  " + "-" * 40)
for yr in sorted(sub["year"].unique()):
    s = sub[sub["year"] == yr]
    print(f"  {yr:>6} {len(s):>6} {s['r'].sum():>8.1f} {s['r'].mean():>6.2f} {(s['r']>0).mean():>5.0%}")
print(f"  {'合计':>6} {len(sub):>6} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f} {(sub['r']>0).mean():>5.0%}")

# 4. 导出CSV（[-5%,15%)过滤，时间降序）
rows = []
for idx, r in sub.reset_index(drop=True).iterrows():
    i = int(r["sig_idx"])
    sig_t = pd.Timestamp(times[i]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    exit_t = pd.Timestamp(times[int(r["exit_idx"])]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    rows.append({"序号": idx+1, "信号时间": sig_t, "方向": "做多" if r["dir"]=="long" else "做空",
        "成交价": round(r["fill_price"],2), "ATR": round(r["atr"],2),
        "ATR%": round(r["atr"]/r["fill_price"]*100,3),
        "EMA70偏离度%": round(r["dev"],2),
        "是否确认": "确认" if r["confirmed"] else "弱突破",
        "出场时间": exit_t, "出场价": round(r["exit_price"],2),
        "持仓K数": int(r["hold"]), "R值": round(r["r"],3),
        "出场原因": r["exit_reason"], "年份": int(r["year"])})

out_df = pd.DataFrame(rows)
out_df = out_df.sort_values("信号时间", ascending=False).reset_index(drop=True)
out_df["序号"] = range(1, len(out_df) + 1)
csv_path = cfg.results_path / "3K突破_EMA70过滤_完整报告.csv"
out_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
print(f"\nCSV已导出: {csv_path}（{len(out_df)}行×{len(out_df.columns)}列）")
