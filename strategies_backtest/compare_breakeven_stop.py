"""对比「3K后止损移到成本价保本」vs「3K后只用KAMA不保本」。

当前：前3K用init_stop(成交价-1ATR)，第4K起用max(KAMA, fill_price) — 有保本
对比：前3K用init_stop(成交价-1ATR)，第4K起只用KAMA — 无保本
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

def simulate(signal, use_breakeven=True):
    """use_breakeven=True: 第4K起用max(KAMA, fill_price)（保本）
       use_breakeven=False: 第4K起只用KAMA（不保本）"""
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
    base = {"dir": d, "year": int(years[i]), "sig_idx": i,
            "atr_pct": atr_pct, "fill_price": fill_price, "atr": av}

    confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {**base, "exit_idx": i, "exit_price": ep, "r": r1, "r_base": r1, "r_addon": 0.0,
                "confirmed": False, "addon": False, "exit_reason": "弱突破平仓"}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; sl_at_addon = None
    addon_eligible = sig_body_pct >= ADDON_BODY_PCT
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        bh = j - i
        if bh <= 3 or np.isnan(ks):
            sl = init_stop
        else:
            if use_breakeven:
                sl = max(ks, fill_price) if is_long else min(ks, fill_price)
            else:
                sl = float(ks)
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
            "r_base": r1, "r_addon": r2, "confirmed": True, "addon": addon_done,
            "exit_reason": exit_reason}

def run_backtest(use_breakeven=True):
    results = []; last_exit = -1; cum_r = 0; max_cum = 0; dd = 0
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, use_breakeven)
        if r is None: continue
        tier = e_prime_tier(r)
        if dd > DD_THRESHOLD: tier = max(1.0, tier - 0.5)
        r["tier"] = tier
        results.append(r); last_exit = r["exit_idx"]
        cum_r += r["r"] * tier; max_cum = max(max_cum, cum_r); dd = max_cum - cum_r
    return pd.DataFrame(results)

def full_stats(rdf, label):
    n = len(rdf); sr = rdf["r"]; wr = rdf["r"] * rdf["tier"]
    weighted_r = wr.sum()
    wins = sr[sr > 0]; losses = sr[sr < 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else float("inf")
    win_rate = (sr > 0).mean()
    cum = wr.cumsum(); max_cum = cum.expanding().max(); dd = (max_cum - cum).max()
    addon_n = rdf["addon"].sum(); addon_r = rdf[rdf["addon"]]["r_addon"].sum()
    print(f"\n{label}")
    print(f"  单数: {n} | 基础R: {sr.sum():.1f} | 加权R: {weighted_r:.1f} | PF: {pf:.1f} | 胜率: {win_rate:.0%} | 回撤: -{dd:.1f}R")
    print(f"  加仓: {addon_n}单 / 加仓R: {addon_r:.1f}")
    # 出场原因分布
    reasons = rdf["exit_reason"].value_counts()
    print(f"  出场原因: {', '.join(f'{k}({v}单)' for k, v in reasons.items())}")
    # 逐年
    yr_data = []
    for yr in sorted(rdf["year"].unique()):
        sub = rdf[rdf["year"]==yr]
        wr_yr = (sub["r"] * sub["tier"]).sum()
        yr_data.append(f"{yr}:{len(sub)}单/{wr_yr:.0f}R")
    print(f"  逐年: {', '.join(yr_data)}")

signals = detect_breakout(df_4h, WINDOW)

print("=" * 110)
print("3K后保本 vs 不保本 对比测试")
print("=" * 110)

rdf_be = run_backtest(use_breakeven=True)
rdf_no = run_backtest(use_breakeven=False)

full_stats(rdf_be, "【方案A】3K后保本（当前：max(KAMA, fill_price)）")
full_stats(rdf_no, "【方案B】3K后不保本（只用KAMA）")

# 对比差异
print(f"\n{'─'*110}")
print("差异分析")
print(f"{'─'*110}")
be_r = rdf_be["r"].sum(); no_r = rdf_no["r"].sum()
be_wr = (rdf_be["r"] * rdf_be["tier"]).sum(); no_wr = (rdf_no["r"] * rdf_no["tier"]).sum()
print(f"  基础R差异: {be_r:.1f} vs {no_r:.1f} (保本{'多' if be_r > no_r else '少'}{abs(be_r-no_r):.1f}R)")
print(f"  加权R差异: {be_wr:.1f} vs {no_wr:.1f} (保本{'多' if be_wr > no_wr else '少'}{abs(be_wr-no_wr):.1f}R)")

# 找出差异单
be_dict = {r["sig_idx"]: r for r in rdf_be.to_dict("records")}
no_dict = {r["sig_idx"]: r for r in rdf_no.to_dict("records")}
diff_reasons = {}
for idx in be_dict:
    if idx in no_dict:
        be_r_val = be_dict[idx]["r"]; no_r_val = no_dict[idx]["r"]
        if abs(be_r_val - no_r_val) > 0.01:
            be_reason = be_dict[idx]["exit_reason"]; no_reason = no_dict[idx]["exit_reason"]
            key = f"{be_reason}→{no_reason}"
            if key not in diff_reasons: diff_reasons[key] = []
            diff_reasons[key].append((idx, be_r_val, no_r_val))

print(f"\n  出场原因变化（保本→不保本）:")
for key, vals in sorted(diff_reasons.items(), key=lambda x: -len(x[1])):
    be_sum = sum(v[1] for v in vals); no_sum = sum(v[2] for v in vals)
    print(f"    {key}: {len(vals)}单, R: {be_sum:.1f}→{no_sum:.1f} (差异{no_sum-be_sum:+.1f})")
