"""批量测试不同暂停时长：20K, 50K, 100K, 150K, 200K, 240K"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
cache = cfg.base_dir / "cache"
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3
CONFIRM_ATR = 0.5; ADDON_ATR = 1.5; ADDON_BODY_PCT = 0.03; ATR_LEN = 20; EMA_LEN = 70
DD_THRESHOLD = 3.0
ABOVE_K = 20  # 在KAMA之上运行至少20根K才算"趋势"

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
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy(); n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy()
ms_4h = df_4h["open_time_ms"].to_numpy()
ms_1m = df_1m["open_time_ms"].to_numpy()
h1m = df_1m["high"].to_numpy(); l1m = df_1m["low"].to_numpy()
four_h_ms = 4 * 3600 * 1000
min_start_idx = np.searchsorted(ms_1m, ms_4h)
min_end_idx = np.searchsorted(ms_1m, ms_4h + four_h_ms)

def calc_atr(h, l, c, length=ATR_LEN):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, ATR_LEN)

k_ema = 2.0/(EMA_LEN+1); de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
date_to_ema = dict(zip(daily_df["open_time"].dt.date, de70))

def get_dev(i):
    if i-1 < 0: return np.nan
    prev_date = pd.to_datetime(df_4h["open_time"].iloc[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100

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
            if lows[j+1] >= lows[j] and lows[j+2] <= lows[j]: return lows[j], j
    return None, None

def simulate(signal):
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_nk = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, cb_idx = find_callback_extreme(i, d, WINDOW)
    entry_base = cb_extreme if cb_extreme is not None else bt_nk
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    fill_price = float(entry_base) if filled else 0.0
    if not filled: return None

    sig_body_pct = abs(closes[i] - opens[i]) / opens[i] if opens[i] > 0 else 0.0
    atr_pct = av / closes[i-1] * 100 if i-1 >= 0 else np.nan
    dev = get_dev(i)
    base = {"dir": d, "year": int(years[i]), "sig_idx": i, "atr_pct": atr_pct, "fill_price": fill_price, "atr": av}

    confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {**base, "exit_idx": i, "r": r1, "r_base": r1, "r_addon": 0.0, "confirmed": False, "addon": False}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; sl_at_addon = None
    addon_eligible = sig_body_pct >= ADDON_BODY_PCT
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        bh = j - i
        if bh <= 3 or np.isnan(ks): sl = init_stop
        else: sl = max(ks, fill_price) if is_long else min(ks, fill_price)
        if not addon_done and addon_eligible:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= ADDON_ATR*av: addon_done = True; addon_price = float(closes[j]); sl_at_addon = float(sl)
        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)
        if bh == 3:
            if (is_long and closes[j] < fill_price) or (not is_long and closes[j] > fill_price):
                exit_idx = j; exit_price = float(closes[j]); break
        if hit_stop and hit_tp:
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep = result
                exit_idx = j; exit_price = ep
                break
            ep = float(sl); exit_idx = j; exit_price = ep; break
        elif hit_stop:
            ep = float(sl); exit_idx = j; exit_price = ep; break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); break
    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1])
    r1 = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av
    if addon_done:
        addon_risk = (addon_price - sl_at_addon) if is_long else (sl_at_addon - addon_price)
        if addon_risk is None or np.isnan(addon_risk) or addon_risk <= 0: r2 = 0.0
        else: r2 = ((exit_price-addon_price)/addon_risk) if is_long else ((addon_price-exit_price)/addon_risk)
    else: r2 = 0.0
    return {**base, "exit_idx": exit_idx, "r": r1+r2, "r_base": r1, "r_addon": r2, "confirmed": True, "addon": addon_done}

def e_prime_tier(r):
    if r["dir"]=="long" and r["atr_pct"]<2.0: return 2.0
    if r["dir"]=="short" and r["atr_pct"]<2.0: return 1.5
    return 1.0

# 检测跌破KAMA事件
pause_start = []
above_count = 0
for j in range(1, n4h):
    if np.isnan(kama_vals[j]):
        above_count = 0
        continue
    if closes[j] >= kama_vals[j]:
        above_count += 1
    else:
        if above_count >= ABOVE_K:
            pause_start.append(j)
        above_count = 0

pause_start = sorted(set(pause_start))
print(f"跌破KAMA事件数: {len(pause_start)}")

signals = detect_breakout(df_4h, WINDOW)

def run_backtest(pause_k):
    paused = np.zeros(n4h, dtype=bool)
    for ps in pause_start:
        end = min(ps + pause_k, n4h)
        paused[ps:end] = True
    
    results = []; last_exit = -1
    cum_r = 0; max_cum = 0; dd = 0
    skipped = 0
    for s in signals:
        if s.idx <= last_exit:
            continue
        if paused[s.idx]:
            skipped += 1
            continue
        r = simulate(s)
        if r is None:
            continue
        tier = e_prime_tier(r)
        if dd > DD_THRESHOLD: tier = max(1.0, tier - 0.5)
        r["tier"] = tier
        results.append(r); last_exit = r["exit_idx"]
        cum_r += r["r"] * tier; max_cum = max(max_cum, cum_r); dd = max_cum - cum_r
    return results, skipped, paused.sum()

def stats(results):
    rdf = pd.DataFrame(results)
    n = len(rdf)
    if n == 0:
        return {"n": 0, "wr": 0, "pf": 0, "win": 0, "dd": 0, "addon": 0}
    sr = rdf["r"]; wr = rdf["r"] * rdf["tier"]
    weighted_r = wr.sum()
    wins = sr[sr > 0]; losses = sr[sr < 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float('inf')
    win_rate = (sr > 0).mean()
    cum = wr.cumsum(); max_cum = cum.expanding().max(); mdd = (max_cum - cum).max()
    addon_n = rdf["addon"].sum()
    return {"n": n, "wr": weighted_r, "pf": pf, "win": win_rate, "dd": mdd, "addon": addon_n}

# 基准
results_no, _, _ = run_backtest(0)
base = stats(results_no)

print("\n" + "=" * 110)
print("不同暂停时长对比测试")
print("=" * 110)
print(f"\n{'暂停K数':>8} {'信号数':>6} {'加权R':>9} {'vs基准':>9} {'PF':>7} {'胜率':>6} {'回撤':>7} {'暂停覆盖':>8} {'跳过':>6}")
print("-" * 110)

pause_ks = [10, 15, 20, 30, 50, 75, 100, 150, 200, 240]
results_list = []

for pk in pause_ks:
    res, skipped, paused_k = run_backtest(pk)
    s = stats(res)
    diff = s["wr"] - base["wr"]
    pf_str = f"{s['pf']:.1f}" if s['pf'] != float('inf') else "inf"
    print(f"  {pk:>6} {s['n']:>6} {s['wr']:>9.1f} {diff:>+9.1f} {pf_str:>7} {s['win']*100:>5.1f}% -{s['dd']:>5.1f}R {paused_k/n4h*100:>6.1f}% {skipped:>6}")
    results_list.append((pk, s, skipped))

# 基准行
pf_base = f"{base['pf']:.1f}" if base['pf'] != float('inf') else "inf"
print(f"  {'基准':>6} {base['n']:>6} {base['wr']:>9.1f} {'0.0':>9} {pf_base:>7} {base['win']*100:>5.1f}% -{base['dd']:>5.1f}R {'0.0':>6}% {'0':>6}")

# 找最优
best = max(results_list, key=lambda x: x[1]["wr"])
print(f"\n最优暂停K数: {best[0]}K (加权R={best[1]['wr']:.1f}, 跳过{best[2]}单)")

# 找PF最优
best_pf = max(results_list, key=lambda x: x[1]["pf"] if x[1]["pf"] != float('inf') else 0)
print(f"最优PF暂停K数: {best_pf[0]}K (PF={best_pf[1]['pf']:.1f})")

# 找回撤最优
best_dd = min(results_list, key=lambda x: x[1]["dd"])
print(f"最优回撤暂停K数: {best_dd[0]}K (回撤-{best_dd[1]['dd']:.1f}R)")