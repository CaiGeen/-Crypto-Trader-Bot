"""对比「实体涨跌幅>3%」作为信号过滤 vs 加仓许可 vs 两者都加 vs 基准。

方案1 基准：所有信号都做，加仓无许可门槛（浮盈>=1.5ATR即加仓）
方案2 信号过滤：只有突破K实体涨跌幅>3%的信号才入场，加仓无许可门槛
方案3 加仓许可：所有信号都入场，但只有突破K实体涨跌幅>3%才允许加仓
方案4 两者都加：信号过滤+加仓许可
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

daily_close = daily_df["close"].to_numpy()
daily_ms = daily_df["open_time_ms"].to_numpy()

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

def get_daily_ema(idx):
    pos = np.searchsorted(daily_ms, ms_4h[idx]) - 1
    if pos < 0 or pos >= len(daily_close): return None
    return float(daily_close[pos])

def get_dev(i):
    ema = get_daily_ema(i)
    if ema is None or ema == 0: return np.nan
    return (closes[i-1] - ema) / ema * 100

def e_prime_tier(r):
    if r["dir"]=="long" and r["atr_pct"]<2.0: return 2.0
    if r["dir"]=="short" and r["atr_pct"]<2.0: return 1.5
    return 1.0

def simulate(signal, use_body_filter=False, use_body_addon_gate=False):
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_nk = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, cb_idx = find_callback_extreme(i, d, WINDOW)
    if cb_extreme is not None:
        entry_base = cb_extreme
    else:
        entry_base = bt_nk
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    fill_price = float(entry_base) if filled else 0.0
    if not filled: return None

    sig_body_pct = abs(closes[i] - opens[i]) / opens[i] if opens[i] > 0 else 0.0

    # 信号过滤：如果use_body_filter，只有实体涨跌幅>3%才做
    if use_body_filter and sig_body_pct < ADDON_BODY_PCT:
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
    # 加仓许可：如果use_body_addon_gate，只有实体涨跌幅>3%才允许加仓
    addon_eligible = (not use_body_addon_gate) or (sig_body_pct >= ADDON_BODY_PCT)
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        bh = j - i
        if bh <= 3 or np.isnan(ks):
            sl = init_stop
        else:
            sl = max(ks, fill_price) if is_long else min(ks, fill_price)
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
        if addon_risk is None or np.isnan(addon_risk) or addon_risk <= 0:
            r2 = 0.0
        else:
            r2 = ((exit_price-addon_price)/addon_risk) if is_long else ((addon_price-exit_price)/addon_risk)
    else:
        r2 = 0.0
    return {**base, "exit_idx": exit_idx, "exit_price": exit_price, "r": r1+r2,
            "r_base": r1, "r_addon": r2,
            "confirmed": True, "addon": addon_done}

def run_backtest(use_body_filter=False, use_body_addon_gate=False):
    results = []; last_exit = -1; cum_r = 0; max_cum = 0; dd = 0
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, use_body_filter, use_body_addon_gate)
        if r is None: continue
        tier = e_prime_tier(r)
        if dd > DD_THRESHOLD: tier = max(1.0, tier - 0.5)
        r["tier"] = tier
        results.append(r); last_exit = r["exit_idx"]
        cum_r += r["r"] * tier
        max_cum = max(max_cum, cum_r); dd = max_cum - cum_r
    return pd.DataFrame(results)

def stats(rdf, label):
    n = len(rdf)
    sr = rdf["r"]; wr = rdf["r"] * rdf["tier"]
    total_r = sr.sum(); weighted_r = wr.sum()
    wins = sr[sr > 0]; losses = sr[sr < 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float("inf")
    win_rate = (sr > 0).mean()
    cum = wr.cumsum(); max_cum = cum.expanding().max(); dd = (max_cum - cum).max()
    addon_n = rdf["addon"].sum()
    addon_r = rdf[rdf["addon"]]["r_addon"].sum()
    print(f"{label:>30} | {n:>5}单 | 基础{total_r:>8.1f}R | 加权{weighted_r:>8.1f}R | PF{pf:>5.1f} | 胜{win_rate:>3.0%} | 回撤-{dd:>4.1f}R | 加仓{addon_n:>4}单/{addon_r:>6.1f}R")

signals = detect_breakout(df_4h, WINDOW)

print("=" * 120)
print("「实体涨跌幅>3%」用法对比测试")
print("=" * 120)
print()
print(f"{'方案':>30} | {'单数':>5}  | {'基础R':>8}   | {'加权R':>8}   | {'PF':>5} | {'胜率':>3} | {'回撤':>5}  | {'加仓':>12}")
print("-" * 120)

rdf1 = run_backtest(use_body_filter=False, use_body_addon_gate=False)
stats(rdf1, "1.基准(无过滤+无加仓许可)")

rdf2 = run_backtest(use_body_filter=True, use_body_addon_gate=False)
stats(rdf2, "2.信号过滤(只做实体>3%)")

rdf3 = run_backtest(use_body_filter=False, use_body_addon_gate=True)
stats(rdf3, "3.加仓许可(实体>3%才加仓)")

rdf4 = run_backtest(use_body_filter=True, use_body_addon_gate=True)
stats(rdf4, "4.两者都加(过滤+许可)")

print()
print("=" * 120)
print("详细分析")
print("=" * 120)

for label, rdf in [("方案1 基准", rdf1), ("方案2 信号过滤", rdf2), ("方案3 加仓许可", rdf3), ("方案4 两者都加", rdf4)]:
    confirmed = rdf[rdf["confirmed"]]
    weak = rdf[~rdf["confirmed"]]
    addon = rdf[rdf["addon"]]
    no_addon = rdf[~rdf["addon"]]
    print(f"\n{label}:")
    print(f"  确认单: {len(confirmed)}单 / R={confirmed['r'].sum():.1f} / 加仓{confirmed['addon'].sum()}单")
    print(f"  弱突破: {len(weak)}单 / R={weak['r'].sum():.1f}")
    print(f"  加仓单: {len(addon)}单 / 底仓R={addon['r_base'].sum():.1f} / 加仓R={addon['r_addon'].sum():.1f} / 总R={addon['r'].sum():.1f}")
    print(f"  非加仓: {len(no_addon)}单 / R={no_addon['r'].sum():.1f}")
    # 逐年
    yr = rdf.groupby("year").agg(n=("r","count"), r=("r","sum"), wr=("r", lambda x: (x*rdf.loc[x.index,"tier"]).sum()))
    parts = []
    for y, row in yr.iterrows():
        parts.append(f"{int(y)}:{int(row['n'])}单/{row['wr']:.0f}R")
    print(f"  逐年: {', '.join(parts)}")
