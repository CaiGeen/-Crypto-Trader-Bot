"""弱突破平仓机会成本分析：弱突破平仓的490单如果继续持仓会怎样？"""
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

def simulate_hold(i, d, entry_base, av):
    """从信号K(i)开始模拟正常持仓（不执行弱突破平仓），返回继续持仓的结局。"""
    is_long = (d == "long")
    if is_long: init_stop = entry_base - STOP_MULT*av; tp_price = entry_base*(1+TP_LONG)
    else: init_stop = entry_base + STOP_MULT*av; tp_price = entry_base*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; sl_at_addon = None
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = init_stop if bh <= 3 else (max(ks, entry_base) if is_long else min(ks, entry_base))
        if not addon_done:
            cg_abs = (closes[j]-entry_base) if is_long else (entry_base-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j]); sl_at_addon = float(sl)
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
    r1 = (exit_price-entry_base)/av if is_long else (entry_base-exit_price)/av
    if addon_done:
        addon_risk = (addon_price - sl_at_addon) if is_long else (sl_at_addon - addon_price)
        if addon_risk is None or np.isnan(addon_risk) or addon_risk <= 0:
            r2 = 0.0
        else:
            r2 = ((exit_price-addon_price)/addon_risk if is_long else (addon_price-exit_price)/addon_risk)
    else:
        r2 = 0.0
    hold_k = exit_idx - i
    return {"hold_r": r1+r2, "hold_reason": exit_reason, "hold_idx": exit_idx, "hold_k": hold_k,
            "addon_done": addon_done}

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
            "atr_pct": atr_pct, "pre_3k_pct": pre_3k_pct, "entry_base": entry_base, "av": av}

    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        hold = simulate_hold(i, d, entry_base, av)
        return {**base, "exit_idx": i, "r": r1, "exit_reason": "弱突破平仓", "confirmed": False, **hold}

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
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j]); sl_at_addon = float(sl)
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
    return {**base, "exit_idx": exit_idx, "r": r1+r2, "exit_reason": exit_reason, "confirmed": True,
            "hold_r": r1+r2, "hold_reason": exit_reason, "hold_idx": exit_idx, "hold_k": exit_idx-i,
            "addon_done": addon_done}

def passes(r):
    if np.isnan(r["dev"]): return False
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.8: return True
    return False

signals = detect_breakout(df_4h, WINDOW)

results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r and passes(r):
        results.append(r)
        last_exit = r["exit_idx"]
rdf = pd.DataFrame(results)

weak = rdf[~rdf["confirmed"]].copy()
confirmed = rdf[rdf["confirmed"]].copy()

D = f"{'━'*95}"
print("=" * 95)
print("弱突破平仓机会成本分析：弱突破平仓的单如果继续持仓会怎样？")
print("=" * 95)

print(f"\n{D}\n一、总体对比\n{D}")
print(f"  全部单数: {len(rdf)}（确认{len(confirmed)} + 弱突破{len(weak)}）")
print(f"  弱突破平仓R: {weak['r'].sum():.1f}（均{weak['r'].mean():.2f}，胜{(weak['r']>0).mean():.0%}）")
print(f"  弱突破如果继续持有R: {weak['hold_r'].sum():.1f}（均{weak['hold_r'].mean():.2f}，胜{(weak['hold_r']>0).mean():.0%}）")
print(f"  净差R: {weak['hold_r'].sum()-weak['r'].sum():+.1f}（继续持有 - 弱突破平仓）")

print(f"\n{D}\n二、弱突破如果继续持有的出场原因分布\n{D}")
for reason in sorted(weak["hold_reason"].unique()):
    sub = weak[weak["hold_reason"]==reason]
    sr = sub["hold_r"]
    w = sr[sr>0].sum(); l = sr[sr<=0].sum()
    pf = f"{w/abs(l):.1f}" if l != 0 else "∞"
    print(f"  {reason:>12}: {len(sub):>4}单/{sr.sum():>7.1f}R/均{sr.mean():>5.2f}/PF{pf}/胜{(sr>0).mean():.0%}")

print(f"\n{D}\n三、'错过大单'分析（继续持有会止盈的单）\n{D}")
missed_tp = weak[weak["hold_reason"]=="止盈"]
print(f"  继续持有会止盈: {len(missed_tp)}单 / {missed_tp['hold_r'].sum():.1f}R（均{missed_tp['hold_r'].mean():.2f}R）")
print(f"  弱突破平仓这些单仅赚: {missed_tp['r'].sum():.1f}R（均{missed_tp['r'].mean():.2f}R）")
print(f"  机会成本: {missed_tp['hold_r'].sum()-missed_tp['r'].sum():.1f}R")
if len(missed_tp) > 0:
    print(f"\n  这些{len(missed_tp)}单详情（按继续持有R降序）:")
    detail = missed_tp.sort_values("hold_r", ascending=False)
    print(f"  {'sig_idx':>7} {'方向':>4} {'年份':>6} {'dev':>6} {'ATR%':>6} {'弱突破R':>8} {'继续R':>8} {'持仓K':>6} {'出场':>10}")
    for _, r in detail.iterrows():
        print(f"  {int(r['sig_idx']):>7} {r['dir']:>4} {int(r['year']):>6} {r['dev']:>6.1f} {r['atr_pct']:>6.2f} {r['r']:>8.2f} {r['hold_r']:>8.2f} {int(r['hold_k']):>6} {r['hold_reason']:>10}")

print(f"\n{D}\n四、按继续持有R分桶\n{D}")
bins = [(-999,-3),(-3,-2),(-2,-1),(-1,0),(0,1),(1,3),(3,10),(10,999)]
print(f"  {'继续持有R':>14} {'单数':>6} {'弱突破R':>8} {'继续R':>8} {'差R':>8} {'说明':>20}")
for lo, hi in bins:
    sub = weak[(weak["hold_r"]>=lo) & (weak["hold_r"]<hi)]
    if sub.empty: continue
    diff = sub["hold_r"].sum() - sub["r"].sum()
    lbl = "错过大单" if hi > 10 else ("错过中单" if hi > 3 else ("微利" if hi > 1 else ("亏损" if hi < 0 else "小利")))
    print(f"  [{lo:>5},{hi:>5}) {len(sub):>6} {sub['r'].sum():>8.1f} {sub['hold_r'].sum():>8.1f} {diff:>+8.1f} {lbl:>20}")

print(f"\n{D}\n五、按方向×ATR%分桶（弱突破单的继续持有表现）\n{D}")
for d in ["long", "short"]:
    sub = weak[weak["dir"]==d]
    if sub.empty: continue
    print(f"\n  【{d}】弱突破{len(sub)}单，弱突破R={sub['r'].sum():.1f}，继续持有R={sub['hold_r'].sum():.1f}")
    for lo, hi in [(0,0.5),(0.5,1.0),(1.0,1.5),(1.5,2.0),(2.0,999)]:
        s2 = sub[(sub["atr_pct"]>=lo)&(sub["atr_pct"]<hi)]
        if s2.empty: continue
        tp_n = (s2["hold_reason"]=="止盈").sum()
        print(f"    ATR%[{lo},{hi}): {len(s2):>4}单 弱突破R={s2['r'].sum():>6.1f} 继续R={s2['hold_r'].sum():>7.1f} 差={s2['hold_r'].sum()-s2['r'].sum():>+6.1f} 止盈{tp_n}单")

print(f"\n{D}\n六、逐年：弱突破R vs 继续持有R\n{D}")
print(f"  {'年份':>6} {'弱突破单':>6} {'弱突破R':>8} {'继续R':>8} {'差R':>8} {'错过止盈':>8}")
for yr in sorted(weak["year"].unique()):
    sub = weak[weak["year"]==yr]
    tp_n = (sub["hold_reason"]=="止盈").sum()
    print(f"  {yr:>6} {len(sub):>6} {sub['r'].sum():>8.1f} {sub['hold_r'].sum():>8.1f} {sub['hold_r'].sum()-sub['r'].sum():>+8.1f} {tp_n:>8}")

print(f"\n{D}\n七、关键结论\n{D}")
total_weak_r = weak["r"].sum()
total_hold_r = weak["hold_r"].sum()
net = total_hold_r - total_weak_r
print(f"  弱突破平仓{len(weak)}单赚{total_weak_r:.1f}R，如果继续持有赚{total_hold_r:.1f}R，净差{net:+.1f}R")
print(f"  其中错过止盈{len(missed_tp)}单（机会成本{missed_tp['hold_r'].sum()-missed_tp['r'].sum():.1f}R）")
print(f"  但继续持有中前3K止损{(weak['hold_reason']=='前3K止损').sum()}单（损失{(weak[weak['hold_reason']=='前3K止损']['hold_r']).sum():.1f}R）")
print(f"  弱突破平仓避免了这些前3K止损的-1R损失，同时锁定微利")
print(f"  另外弱突破平仓后last_exit更新快，后续信号能触发（无确认仅546单 vs 有确认771单）")
