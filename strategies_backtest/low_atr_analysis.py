"""ATR%<0.5深度分析：行情极度压缩为何效果不好？"""
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

def simulate_full(signal):
    """完整模拟，记录MFE/MAE/ATR变化"""
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
            "atr_pct": atr_pct, "pre_3k_pct": pre_3k_pct,
            "entry_base": entry_base, "fill_price": fill_price, "atr": av,
            "sig_close": closes[i], "sig_time": pd.Timestamp(times[i]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")}

    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {**base, "exit_idx": i, "exit_price": ep, "r": r1, "hold": 0,
                "exit_reason": "弱突破平仓", "confirmed": False, "addon": False,
                "mfe": 0, "mae": 0, "mfe_atr": 0, "mae_atr": 0, "atr_after_3k": atr_vals[min(i+3,n4h-1)]}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; sl_at_addon = None
    exit_idx = exit_price = exit_reason = None
    mfe = 0.0; mae = 0.0
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = init_stop if bh <= 3 else (max(ks, fill_price) if is_long else min(ks, fill_price))
        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j]); sl_at_addon = float(sl)
        if is_long:
            mfe = max(mfe, highs[j] - fill_price)
            mae = min(mae, lows[j] - fill_price)
        else:
            mfe = max(mfe, fill_price - lows[j])
            mae = min(mae, fill_price - highs[j])
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
    atr_after = atr_vals[min(exit_idx, n4h-1)]
    return {**base, "exit_idx": exit_idx, "exit_price": exit_price, "r": r1+r2,
            "hold": exit_idx-i, "exit_reason": exit_reason, "confirmed": True, "addon": addon_done,
            "mfe": mfe, "mae": mae, "mfe_atr": mfe/av, "mae_atr": mae/av, "atr_after_3k": atr_vals[min(i+3,n4h-1)],
            "atr_at_exit": atr_after}

def passes(r):
    if np.isnan(r["dev"]): return False
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.8: return True
    return False

signals = detect_breakout(df_4h, WINDOW)

results = []; last_exit = -1
all_low_atr = []  # ATR%<0.5的所有信号（含被过滤的）
for s in signals:
    av = atr_vals[s.idx-1] if s.idx-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: continue
    atr_pct = av / closes[s.idx-1] * 100 if s.idx-1 >= 0 else np.nan
    if not np.isnan(atr_pct) and atr_pct < 0.5:
        all_low_atr.append({"idx": s.idx, "dir": s.direction, "atr_pct": atr_pct,
                            "time": pd.Timestamp(times[s.idx]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")})
    if s.idx <= last_exit: continue
    r = simulate_full(s)
    if r and passes(r):
        results.append(r)
        last_exit = r["exit_idx"]
rdf = pd.DataFrame(results)

D = f"{'━'*90}"
print("=" * 90)
print("ATR%<0.5 深度分析：行情极度压缩为何效果不好？")
print("=" * 90)

# 1. 所有ATR%<0.5的信号
print(f"\n{D}\n一、所有ATR%<0.5的3K突破信号（含被方案A过滤的）\n{D}")
print(f"  共{len(all_low_atr)}个信号")
for s in all_low_atr:
    r = rdf[rdf["sig_idx"]==s["idx"]]
    status = f"成交 R={r.iloc[0]['r']:.2f} ({r.iloc[0]['exit_reason']})" if len(r)>0 else "被过滤/未成交"
    print(f"  {s['time']} {s['dir']:>5} ATR%={s['atr_pct']:.3f} → {status}")

# 2. ATR%<0.5成交单详情
print(f"\n{D}\n二、ATR%<0.5成交单详情\n{D}")
low = rdf[rdf["atr_pct"]<0.5]
for _, r in low.iterrows():
    print(f"\n  {r['sig_time']} {'做多' if r['dir']=='long' else '做空'}")
    print(f"    成交价={r['fill_price']:.1f}  ATR={r['atr']:.1f}  ATR%={r['atr_pct']:.3f}%")
    print(f"    止损距离={r['atr']:.1f}（1ATR）  止损价={r['fill_price']-r['atr'] if r['dir']=='long' else r['fill_price']+r['atr']:.1f}")
    print(f"    MFE={r['mfe']:.1f}（{r['mfe_atr']:.2f}ATR）  MAE={r['mae']:.1f}（{r['mae_atr']:.2f}ATR）")
    print(f"    出场: {r['exit_reason']}  R={r['r']:.2f}  持仓{r['hold']}K")
    print(f"    ATR变化: 信号时{r['atr']:.1f} → 3K后{r['atr_after_3k']:.1f} → 出场时{r['atr_at_exit']:.1f}")
    print(f"    ATR%变化: {r['atr_pct']:.3f}% → {r['atr_after_3k']/closes[min(int(r['sig_idx'])+3,n4h-1)]*100:.3f}% → {r['atr_at_exit']/closes[int(r['exit_idx'])]*100:.3f}%")

# 3. 对比ATR%[0.5,1.0)黄金带
print(f"\n{D}\n三、对比：ATR%[0.5,1.0)黄金带\n{D}")
gold = rdf[(rdf["atr_pct"]>=0.5)&(rdf["atr_pct"]<1.0)]
print(f"  共{len(gold)}单 / R={gold['r'].sum():.1f} / 均R={gold['r'].mean():.2f}")
print(f"  MFE/ATR均={gold['mfe_atr'].mean():.2f}  MAE/ATR均={gold['mae_atr'].mean():.2f}")
print(f"  ATR扩张比(出场/信号)均={gold['atr_at_exit'].mean()/gold['atr'].mean():.2f}")

# 4. ATR%<0.5 vs 邻近区间对比
print(f"\n{D}\n四、ATR%分桶对比（确认单 only）\n{D}")
confirmed = rdf[rdf["confirmed"]]
print(f"  {'ATR%':>10} {'单数':>6} {'R':>8} {'均R':>6} {'MFE/ATR':>8} {'MAE/ATR':>8} {'ATR扩张':>8}")
for lo, hi in [(0,0.5),(0.5,0.7),(0.7,1.0),(1.0,1.5),(1.5,2.0),(2.0,3.0),(3.0,999)]:
    sub = confirmed[(confirmed["atr_pct"]>=lo)&(confirmed["atr_pct"]<hi)]
    if sub.empty: continue
    expand = sub["atr_at_exit"].mean()/sub["atr"].mean()
    print(f"  [{lo:.1f},{hi:.1f}) {len(sub):>6} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f} "
          f"{sub['mfe_atr'].mean():>8.2f} {sub['mae_atr'].mean():>8.2f} {expand:>8.2f}")

# 5. 核心问题：ATR扩张导致止损太紧
print(f"\n{D}\n五、核心问题分析：ATR扩张 vs 止损距离\n{D}")
print(f"  ATR%<0.5时，止损=1ATR基于低波动期ATR")
print(f"  但突破后ATR快速扩张，实际波动远大于止损距离")
for _, r in low.iterrows():
    if not r["confirmed"]: continue
    expand_ratio = r["atr_at_exit"] / r["atr"]
    print(f"\n  {r['sig_time']} {'做多' if r['dir']=='long' else '做空'}:")
    print(f"    信号ATR={r['atr']:.1f} → 出场ATR={r['atr_at_exit']:.1f}（扩张{expand_ratio:.1f}倍）")
    print(f"    止损距离={r['atr']:.1f}，但出场时1ATR={r['atr_at_exit']:.1f}")
    print(f"    MFE={r['mfe_atr']:.2f}ATR（信号ATR），={r['mfe']/r['atr_at_exit']:.2f}ATR（出场ATR）")
    print(f"    MAE={r['mae_atr']:.2f}ATR（信号ATR），={r['mae']/r['atr_at_exit']:.2f}ATR（出场ATR）")
    if r["exit_reason"] in ("前3K止损",):
        print(f"    → 前3K被扫！行情可能在止损后才爆发")
        # 看止损后5根K的走势
        ei = int(r["exit_idx"])
        for k in range(1, min(6, n4h-ei)):
            if r["dir"]=="long":
                chg = (closes[ei+k] - r["exit_price"]) / r["atr"] * 100
            else:
                chg = (r["exit_price"] - closes[ei+k]) / r["atr"] * 100
            print(f"      止损后第{k}K: close={closes[ei+k]:.1f} 偏离止损价{chg:.1f}%ATR")
