"""扫描「实体涨跌幅>X%」不同阈值，作为信号过滤 vs 加仓许可的效果对比。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
cache = cfg.base_dir / "cache"
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3
CONFIRM_ATR = 0.5; ADDON_ATR = 1.5; ATR_LEN = 20; EMA_LEN = 70
DD_THRESHOLD = 3.0

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

def e_prime_tier(r):
    if r["dir"]=="long" and r["atr_pct"]<2.0: return 2.0
    if r["dir"]=="short" and r["atr_pct"]<2.0: return 1.5
    return 1.0

def simulate(signal, body_thresh, mode):
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

    if mode == "filter" and sig_body_pct < body_thresh:
        return None

    atr_pct = av / closes[i-1] * 100 if i-1 >= 0 else np.nan
    base = {"dir": d, "year": int(years[i]), "sig_idx": i,
            "atr_pct": atr_pct, "fill_price": fill_price, "atr": av,
            "sig_body_pct": sig_body_pct}

    confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {**base, "exit_idx": i, "exit_price": ep, "r": r1, "r_base": r1, "r_addon": 0.0,
                "confirmed": False, "addon": False}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; sl_at_addon = None
    addon_eligible = (mode != "gate") or (sig_body_pct >= body_thresh)
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
        if hit_stop and hit_tp:
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep = result
                exit_idx = j; exit_price = ep
                exit_reason = "止盈" if reason_1m == "tp" else ("前3K止损" if bh <= 3 else "KAMA跟踪止损")
                break
            ep = float(sl); exit_idx = j; exit_price = ep; exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_stop:
            ep = float(sl); exit_idx = j; exit_price = ep; exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); exit_reason = "止盈"; break
        if bh == 3:
            if (is_long and closes[j] < fill_price) or (not is_long and closes[j] > fill_price):
                exit_idx = j; exit_price = float(closes[j]); exit_reason = "3K收盘认赔"; break
    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1]); exit_reason = "未平仓"
    r1 = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av
    if addon_done:
        addon_risk = (addon_price - sl_at_addon) if is_long else (sl_at_addon - addon_price)
        if addon_risk is None or np.isnan(addon_risk) or addon_risk <= 0: r2 = 0.0
        else: r2 = ((exit_price-addon_price)/addon_risk) if is_long else ((addon_price-exit_price)/addon_risk)
    else: r2 = 0.0
    return {**base, "exit_idx": exit_idx, "exit_price": exit_price, "r": r1+r2,
            "r_base": r1, "r_addon": r2, "confirmed": True, "addon": addon_done}

def run_backtest(body_thresh, mode):
    results = []; last_exit = -1; cum_r = 0; max_cum = 0; dd = 0
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, body_thresh, mode)
        if r is None: continue
        tier = e_prime_tier(r)
        if dd > DD_THRESHOLD: tier = max(1.0, tier - 0.5)
        r["tier"] = tier
        results.append(r); last_exit = r["exit_idx"]
        cum_r += r["r"] * tier; max_cum = max(max_cum, cum_r); dd = max_cum - cum_r
    return pd.DataFrame(results)

def stats_line(rdf, label):
    n = len(rdf); sr = rdf["r"]; wr = rdf["r"] * rdf["tier"]
    weighted_r = wr.sum()
    wins = sr[sr > 0]; losses = sr[sr < 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float("inf")
    win_rate = (sr > 0).mean()
    cum = wr.cumsum(); max_cum = cum.expanding().max(); dd = (max_cum - cum).max()
    addon_n = rdf["addon"].sum(); addon_r = rdf[rdf["addon"]]["r_addon"].sum()
    print(f"  {label:>8} | {n:>5}单 | 加权{weighted_r:>8.1f}R | PF{pf:>5.1f} | 胜{win_rate:>3.0%} | 回撤-{dd:>4.1f}R | 加仓{addon_n:>4}单/{addon_r:>6.1f}R")

signals = detect_breakout(df_4h, WINDOW)

print("=" * 110)
print("「实体涨跌幅>X%」阈值扫描：信号过滤 vs 加仓许可")
print("=" * 110)

thresholds = [0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05]

print(f"\n{'─'*110}")
print("一、信号过滤（只有突破K实体涨跌幅>X%的信号才入场）")
print(f"{'─'*110}")
print(f"  {'阈值':>8} | {'单数':>5}  | {'加权R':>8}   | {'PF':>5} | {'胜率':>3} | {'回撤':>5}  | {'加仓':>12}")
for t in thresholds:
    rdf = run_backtest(t, "filter")
    stats_line(rdf, f"{t:.1%}")

print(f"\n{'─'*110}")
print("二、加仓许可（所有信号都入场，但只有突破K实体涨跌幅>X%才允许加仓）")
print(f"{'─'*110}")
print(f"  {'阈值':>8} | {'单数':>5}  | {'加权R':>8}   | {'PF':>5} | {'胜率':>3} | {'回撤':>5}  | {'加仓':>12}")
for t in thresholds:
    rdf = run_backtest(t, "gate")
    stats_line(rdf, f"{t:.1%}")

print(f"\n{'─'*110}")
print("三、基准（无过滤、无许可门槛）")
print(f"{'─'*110}")
rdf = run_backtest(0, "none")
stats_line(rdf, "基准")
