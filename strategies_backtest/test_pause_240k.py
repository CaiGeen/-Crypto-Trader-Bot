"""测试：跌破KAMA后暂停240根K线禁止交易，240K后恢复。

触发定义：价格收盘跌破KAMA线（做多视角）或上穿KAMA线（做空视角）
暂停规则：从跌破当根K开始，往后240根K内所有新信号跳过
对比：有暂停 vs 无暂停（基准）
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
CONFIRM_ATR = 0.5; ADDON_ATR = 1.5; ADDON_BODY_PCT = 0.03; ATR_LEN = 20; EMA_LEN = 70
DD_THRESHOLD = 3.0
PAUSE_K = 240

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

# 检测"跌破KAMA"事件：收盘价从上往下破KAMA（做多视角），或从下往上破KAMA（做空视角）
pause_start = []
for j in range(1, n4h):
    if np.isnan(kama_vals[j]) or np.isnan(kama_vals[j-1]):
        continue
    # 做多跌破（从上往下破KAMA）
    if closes[j-1] >= kama_vals[j-1] and closes[j] < kama_vals[j]:
        pause_start.append(j)
    # 做空跌破（从下往上破KAMA）
    elif closes[j-1] <= kama_vals[j-1] and closes[j] > kama_vals[j]:
        pause_start.append(j)

pause_start = sorted(set(pause_start))
print(f"跌破KAMA事件数: {len(pause_start)}")

# 构建暂停区间：每个跌破事件后240根K暂停
paused = np.zeros(n4h, dtype=bool)
for ps in pause_start:
    end = min(ps + PAUSE_K, n4h)
    paused[ps:end] = True

print(f"暂停覆盖K线数: {paused.sum()} ({paused.sum()/n4h*100:.1f}%)")

signals = detect_breakout(df_4h, WINDOW)

def run_backtest(use_pause):
    results = []; last_exit = -1
    cum_r = 0; max_cum = 0; dd = 0
    skipped_pause = 0
    for s in signals:
        if s.idx <= last_exit:
            continue
        if use_pause and paused[s.idx]:
            skipped_pause += 1
            continue
        r = simulate(s)
        if r is None:
            continue
        tier = e_prime_tier(r)
        if dd > DD_THRESHOLD: tier = max(1.0, tier - 0.5)
        r["tier"] = tier
        results.append(r); last_exit = r["exit_idx"]
        cum_r += r["r"] * tier; max_cum = max(max_cum, cum_r); dd = max_cum - cum_r
    return results, skipped_pause

def stats(results, label):
    rdf = pd.DataFrame(results)
    n = len(rdf)
    sr = rdf["r"]; wr = rdf["r"] * rdf["tier"]
    weighted_r = wr.sum()
    wins = sr[sr > 0]; losses = sr[sr < 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float('inf')
    win_rate = (sr > 0).mean()
    cum = wr.cumsum(); max_cum = cum.expanding().max(); mdd = (max_cum - cum).max()
    addon_n = rdf["addon"].sum()
    print(f"{label:>30} | {n:>5}单 | 基础{sr.sum():>8.1f}R | 加权{weighted_r:>8.1f}R | PF{pf:>5.1f} | 胜{win_rate:>3.0%} | 回撤-{mdd:>4.1f}R | 加仓{addon_n:>4}单")

print("\n" + "=" * 100)
print(f"跌破KAMA后暂停{PAUSE_K}根K线测试")
print("=" * 100)

results_no, _ = run_backtest(use_pause=False)
stats(results_no, "1.无暂停(基准)")

results_pause, skipped = run_backtest(use_pause=True)
stats(results_pause, "2.跌破KAMA后暂停240K")

print(f"\n暂停跳过的信号数: {skipped}")

# 逐年对比
print("\n逐年对比:")
rdf_no = pd.DataFrame(results_no)
rdf_pause = pd.DataFrame(results_pause)
for yr in sorted(rdf_no["year"].unique()):
    sub_no = rdf_no[rdf_no["year"]==yr]
    sub_p = rdf_pause[rdf_pause["year"]==yr]
    wr_no = (sub_no["r"]*sub_no["tier"]).sum() if len(sub_no)>0 else 0
    wr_p = (sub_p["r"]*sub_p["tier"]).sum() if len(sub_p)>0 else 0
    print(f"  {yr}: 基准{len(sub_no)}单/{wr_no:.0f}R -> 暂停{len(sub_p)}单/{wr_p:.0f}R (差异{wr_p-wr_no:+.0f}R)")