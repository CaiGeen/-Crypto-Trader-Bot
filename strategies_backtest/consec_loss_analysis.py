"""连损分析与改进方案对比：2x放大连损问题"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
cache = cfg.base_dir / "cache"
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3
CONFIRM_ATR = 0.5; ADDON_ATR = 1.5

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

def simulate(signal):
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
            "atr_pct": atr_pct, "pre_3k_pct": pre_3k_pct}
    confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {**base, "exit_idx": i, "r": r1, "exit_reason": "弱突破平仓", "confirmed": False}
    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; sl_at_addon = None
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = init_stop if bh <= 3 else (max(ks, fill_price) if is_long else min(ks, fill_price))
        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= ADDON_ATR*av: addon_done = True; addon_price = float(closes[j]); sl_at_addon = float(sl)
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
    return {**base, "exit_idx": exit_idx, "r": r1+r2, "exit_reason": exit_reason, "confirmed": True}

def passes(r):
    if np.isnan(r["dev"]): return False
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.8: return True
    return False

def e_prime_tier(r):
    if r["dir"]=="long" and r["atr_pct"]<2.0: return 2.0
    if r["dir"]=="short" and r["atr_pct"]<2.0: return 1.5
    return 1.0

signals = detect_breakout(df_4h, WINDOW)
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r and passes(r):
        results.append(r)
        last_exit = r["exit_idx"]
rdf = pd.DataFrame(results)
rdf["tier"] = rdf.apply(e_prime_tier, axis=1)
rdf["wr"] = rdf["r"] * rdf["tier"]
rdf = rdf.sort_values("sig_idx").reset_index(drop=True)

D = f"{'━'*90}"
print("=" * 90)
print("连损分析与改进方案对比")
print("=" * 90)

# 一、当前E'连损分析
print(f"\n{D}\n一、当前E'方案连损分析\n{D}")
wr = rdf["wr"].values
is_loss = wr < 0
streaks = []; current = 0
for i in range(len(wr)):
    if is_loss[i]:
        current += 1
    else:
        if current > 0: streaks.append(current)
        current = 0
if current > 0: streaks.append(current)
print(f"  最大连损单数: {max(streaks) if streaks else 0}")
print(f"  连损≥3次: {sum(1 for s in streaks if s>=3)}次")
print(f"  连损≥5次: {sum(1 for s in streaks if s>=5)}次")

streak_loss = []; current_r = 0; current_n = 0
for i in range(len(wr)):
    if is_loss[i]:
        current_r += wr[i]; current_n += 1
    else:
        if current_n > 0: streak_loss.append((current_n, current_r))
        current_r = 0; current_n = 0
if current_n > 0: streak_loss.append((current_n, current_r))
streak_loss.sort(key=lambda x: x[1])
print(f"\n  最差连损段（按加权R排序）:")
print(f"  {'连损单数':>8} {'加权R':>8}")
for n, r in streak_loss[:10]:
    print(f"  {n:>8} {r:>8.1f}")

# 2x档连损
print(f"\n  2x档连损分析:")
tier2 = rdf[rdf["tier"]==2.0].reset_index(drop=True)
wr2 = tier2["wr"].values
is_loss2 = wr2 < 0
streaks2 = []; current = 0
for i in range(len(wr2)):
    if is_loss2[i]:
        current += 1
    else:
        if current > 0: streaks2.append(current)
        current = 0
if current > 0: streaks2.append(current)
print(f"  2x档最大连损: {max(streaks2) if streaks2 else 0}单")
streak_loss2 = []; current_r = 0; current_n = 0
for i in range(len(wr2)):
    if is_loss2[i]:
        current_r += wr2[i]; current_n += 1
    else:
        if current_n > 0: streak_loss2.append((current_n, current_r))
        current_r = 0; current_n = 0
if current_n > 0: streak_loss2.append((current_n, current_r))
streak_loss2.sort(key=lambda x: x[1])
print(f"  2x档最差连损段:")
for n, r in streak_loss2[:5]:
    print(f"    {n}单/{r:.1f}R")

# 二、改进方案
print(f"\n{D}\n二、改进方案对比\n{D}")

def eval_scheme(rdf, tier_fn, label, dynamic=False):
    """评估分档方案。dynamic=True时tier_fn接受(rdf_row, state)并返回(tier, new_state)"""
    rdf = rdf.copy()
    if dynamic:
        tiers = []; state = None
        for _, row in rdf.iterrows():
            tier, state = tier_fn(row, state)
            tiers.append(tier)
        rdf["tier"] = tiers
    else:
        rdf["tier"] = rdf.apply(tier_fn, axis=1)
    rdf["wr"] = rdf["r"] * rdf["tier"]
    s = rdf.sort_values("sig_idx"); cum = s["wr"].cumsum()
    dd = (cum - cum.cummax()).min()
    wr = s["wr"].values
    is_l = wr < 0
    streaks = []; cur = 0
    for i in range(len(wr)):
        if is_l[i]: cur += 1
        else:
            if cur > 0: streaks.append(cur)
            cur = 0
    if cur > 0: streaks.append(cur)
    max_streak = max(streaks) if streaks else 0
    sl = []; cr = 0; cn = 0
    for i in range(len(wr)):
        if is_l[i]: cr += wr[i]; cn += 1
        else:
            if cn > 0: sl.append((cn, cr))
            cr = 0; cn = 0
    if cn > 0: sl.append((cn, cr))
    worst_streak_r = min(sl, key=lambda x: x[1])[1] if sl else 0
    total_wr = rdf["wr"].sum()
    print(f"  {label:>30} 加权R={total_wr:>7.1f} 回撤={dd:>5.1f} 最大连损={max_streak:>3}单 最差连损段={worst_streak_r:>6.1f}R")
    return total_wr, dd, max_streak, worst_streak_r

# E'基准
e_prime = lambda r: e_prime_tier(r)
eval_scheme(rdf, e_prime, "E'(基准)")

# 方案1: 连损2单降档
def tier_consec_down(row, state):
    if state is None: state = {"streak": 0}
    base_tier = e_prime_tier(row)
    if state["streak"] >= 2:
        tier = max(1.0, base_tier - 0.5)
    else:
        tier = base_tier
    if row["r"] < 0:
        state["streak"] += 1
    else:
        state["streak"] = 0
    return tier, state
eval_scheme(rdf, tier_consec_down, "连损2单降0.5x", dynamic=True)

# 方案2: 连损3单降档
def tier_consec3_down(row, state):
    if state is None: state = {"streak": 0}
    base_tier = e_prime_tier(row)
    if state["streak"] >= 3:
        tier = max(1.0, base_tier - 0.5)
    else:
        tier = base_tier
    if row["r"] < 0:
        state["streak"] += 1
    else:
        state["streak"] = 0
    return tier, state
eval_scheme(rdf, tier_consec3_down, "连损3单降0.5x", dynamic=True)

# 方案3: 回撤自适应（当前回撤>3R时降档）
def tier_dd_adapt(row, state):
    if state is None: state = {"peak": 0, "cum": 0}
    base_tier = e_prime_tier(row)
    state["cum"] += row["r"] * base_tier
    state["peak"] = max(state["peak"], state["cum"])
    dd = state["cum"] - state["peak"]
    if dd <= -3:
        tier = max(1.0, base_tier - 0.5)
    else:
        tier = base_tier
    return tier, state
eval_scheme(rdf, tier_dd_adapt, "回撤>3R降0.5x", dynamic=True)

# 方案4: 回撤自适应（回撤>5R时降档）
def tier_dd5_adapt(row, state):
    if state is None: state = {"peak": 0, "cum": 0}
    base_tier = e_prime_tier(row)
    state["cum"] += row["r"] * base_tier
    state["peak"] = max(state["peak"], state["cum"])
    dd = state["cum"] - state["peak"]
    if dd <= -5:
        tier = max(1.0, base_tier - 0.5)
    else:
        tier = base_tier
    return tier, state
eval_scheme(rdf, tier_dd5_adapt, "回撤>5R降0.5x", dynamic=True)

# 方案5: 2x档亏损后下一单降为1.5x
def tier_2x_loss_down(row, state):
    if state is None: state = {"last_2x_loss": False}
    base_tier = e_prime_tier(row)
    if base_tier == 2.0 and state["last_2x_loss"]:
        tier = 1.5
    else:
        tier = base_tier
    state["last_2x_loss"] = (base_tier == 2.0 and row["r"] < 0)
    return tier, state
eval_scheme(rdf, tier_2x_loss_down, "2x亏后下单降1.5x", dynamic=True)

# 方案6: 2x档连续亏损2单后降为1x直到盈利
def tier_2x_consec2(row, state):
    if state is None: state = {"streak": 0, "downgraded": False}
    base_tier = e_prime_tier(row)
    if base_tier == 2.0 and state["downgraded"]:
        tier = 1.0
    else:
        tier = base_tier
    if base_tier == 2.0 and row["r"] < 0:
        state["streak"] += 1
        if state["streak"] >= 2: state["downgraded"] = True
    elif row["r"] > 0:
        state["streak"] = 0; state["downgraded"] = False
    return tier, state
eval_scheme(rdf, tier_2x_consec2, "2x连亏2单降1x到盈利", dynamic=True)

# 方案7: 2x上限改为1.8x（降低放大倍数）
def tier_18x(row):
    base = e_prime_tier(row)
    if base == 2.0: return 1.8
    return base
eval_scheme(rdf, tier_18x, "2x→1.8x(降杠杆)")

# 方案8: 2x上限改为1.5x（E'做多也1.5x）
def tier_15x_all(row):
    base = e_prime_tier(row)
    if base == 2.0: return 1.5
    return base
eval_scheme(rdf, tier_15x_all, "2x→1.5x(全1.5x)")

# 方案9: 连损2单降档+盈利恢复（渐进式）
def tier_gradual(row, state):
    if state is None: state = {"streak": 0}
    base_tier = e_prime_tier(row)
    if state["streak"] >= 2:
        tier = max(1.0, base_tier - 1.0)
    elif state["streak"] == 1:
        tier = max(1.0, base_tier - 0.5)
    else:
        tier = base_tier
    if row["r"] < 0:
        state["streak"] += 1
    else:
        state["streak"] = 0
    return tier, state
eval_scheme(rdf, tier_gradual, "连损渐进降(1单-0.5x,2单-1x)", dynamic=True)

# 三、逐年对比（选最好的几个方案）
print(f"\n{D}\n三、逐年对比（E'基准 vs 连损2单降档 vs 2x亏后降1.5x）\n{D}")
schemes = {
    "E'基准": (e_prime, False),
    "回撤>3R降0.5x": (tier_dd_adapt, True),
    "2x亏后下单降1.5x": (tier_2x_loss_down, True),
    "2x→1.8x": (tier_18x, False),
}
print(f"  {'年份':>6}", end="")
for label in schemes:
    print(f" {label:>16}", end="")
print()
for yr in sorted(rdf["year"].unique()):
    print(f"  {yr:>6}", end="")
    for label, (fn, is_dyn) in schemes.items():
        sub = rdf[rdf["year"]==yr].copy()
        if is_dyn:
            tiers = []; state = None
            for _, row in sub.iterrows():
                tier, state = fn(row, state)
                tiers.append(tier)
            sub["tier"] = tiers
        else:
            sub["tier"] = sub.apply(fn, axis=1)
        wr_sum = (sub["r"] * sub["tier"]).sum()
        print(f" {wr_sum:>16.1f}", end="")
    print()
