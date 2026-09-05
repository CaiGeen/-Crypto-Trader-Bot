"""限价入场 vs 市价入场对比。
限价：挂entry_base等回落买入，止损止盈基于entry_base
市价：信号K收盘价买入，止损止盈基于close[i]，不存在未成交
出场都市价（触及线价成交，跳空用开盘价），不计滑点。
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
signals = detect_breakout(df_4h, cfg.window)
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy()
n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy()

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
TP_LONG = 0.22; TP_SHORT = 0.12
STOP_MULT = 1.0

def find_callback_extreme(i, direction, window=6):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n4h: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]: return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] >= lows[j]: return lows[j], j
    return None, None

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


def simulate_market(signal):
    """市价入场：入场价=close[i]，止损止盈基于close[i]"""
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_6k = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    has_cb = cb_extreme is not None
    entry_base = cb_extreme if has_cb else bt_6k

    # 市价入场价 = 收盘价
    entry_price = float(closes[i])

    # 确认：收盘距回调基准至少0.5ATR（弱突破过滤，保留）
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)

    if not confirmed:
        # 弱突破：当K平仓，入场价=出场价=close[i]，R=0
        return {"dir": d, "exit_idx": i, "r": 0.0, "hold": 0,
                "exit_reason": "弱突破平仓", "confirmed": False, "addon": False,
                "entry_price": entry_price, "exit_price": entry_price,
                "year": int(years[i]), "sig_idx": i}

    # 止损止盈基于入场价close[i]
    if is_long: init_stop = entry_price - STOP_MULT*av; tp_price = entry_price*(1+TP_LONG)
    else: init_stop = entry_price + STOP_MULT*av; tp_price = entry_price*(1-TP_SHORT)

    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None; ambig = False
    mfe = -np.inf if is_long else np.inf; mae = np.inf if is_long else -np.inf

    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        # KAMA保本线基于入场价
        sl = init_stop if bh <= 3 else (max(ks, entry_price) if is_long else min(ks, entry_price))

        if not addon_done:
            cg_abs = (closes[j]-entry_price) if is_long else (entry_price-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j])

        if is_long: mfe = max(mfe, highs[j]); mae = min(mae, lows[j])
        else: mfe = min(mfe, lows[j]); mae = max(mae, highs[j])

        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)

        if hit_stop and hit_tp:
            ambig = True
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep = result
                if reason_1m == "stop":
                    exit_idx = j; exit_price = ep
                    exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
                else:
                    exit_idx = j; exit_price = ep; exit_reason = "止盈"
                break
            if is_long: ep = float(opens[j]) if opens[j] < sl else float(sl)
            else: ep = float(opens[j]) if opens[j] > sl else float(sl)
            exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_stop:
            if is_long: ep = float(opens[j]) if opens[j] < sl else float(sl)
            else: ep = float(opens[j]) if opens[j] > sl else float(sl)
            exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); exit_reason = "止盈"; break

    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1]); exit_reason = "未平仓"

    r1 = (exit_price-entry_price)/av if is_long else (entry_price-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    mfe_pct = (mfe-entry_price)/entry_price*100 if is_long else (entry_price-mfe)/entry_price*100
    mae_pct = (mae-entry_price)/entry_price*100 if is_long else (entry_price-mae)/entry_price*100
    return {"dir": d, "exit_idx": exit_idx, "r": r1+r2, "hold": exit_idx-i,
            "exit_reason": exit_reason, "confirmed": True, "addon": addon_done,
            "entry_price": entry_price, "exit_price": exit_price,
            "mfe_pct": mfe_pct, "mae_pct": mae_pct, "ambig": ambig,
            "year": int(years[i]), "sig_idx": i}


def simulate_limit(signal):
    """限价入场（方案B）：挂entry_base等回落，未成交跳过不占用last_exit"""
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_6k = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    has_cb = cb_extreme is not None
    entry_base = cb_extreme if has_cb else bt_6k
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)

    if not filled:
        return {"dir": d, "exit_idx": i, "filled": False, "r": 0.0, "hold": 0,
                "exit_reason": "限价未成交", "confirmed": confirmed, "addon": False,
                "entry_price": 0.0, "exit_price": 0.0,
                "year": int(years[i]), "sig_idx": i}

    if is_long: fill_price = float(opens[i]) if opens[i] < entry_base else float(entry_base)
    else: fill_price = float(opens[i]) if opens[i] > entry_base else float(entry_base)

    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-entry_base)/av if is_long else (entry_base-ep)/av
        return {"dir": d, "exit_idx": i, "filled": True, "r": r1, "hold": 0,
                "exit_reason": "弱突破平仓", "confirmed": False, "addon": False,
                "entry_price": fill_price, "exit_price": ep,
                "year": int(years[i]), "sig_idx": i}

    if is_long: init_stop = entry_base - STOP_MULT*av; tp_price = entry_base*(1+TP_LONG)
    else: init_stop = entry_base + STOP_MULT*av; tp_price = entry_base*(1-TP_SHORT)

    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None; ambig = False

    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = init_stop if bh <= 3 else (max(ks, entry_base) if is_long else min(ks, entry_base))

        if not addon_done:
            cg_abs = (closes[j]-entry_base) if is_long else (entry_base-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j])

        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)

        if hit_stop and hit_tp:
            ambig = True
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep = result
                if reason_1m == "stop":
                    exit_idx = j; exit_price = ep
                    exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
                else:
                    exit_idx = j; exit_price = ep; exit_reason = "止盈"
                break
            if is_long: ep = float(opens[j]) if opens[j] < sl else float(sl)
            else: ep = float(opens[j]) if opens[j] > sl else float(sl)
            exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_stop:
            if is_long: ep = float(opens[j]) if opens[j] < sl else float(sl)
            else: ep = float(opens[j]) if opens[j] > sl else float(sl)
            exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); exit_reason = "止盈"; break

    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1]); exit_reason = "未平仓"

    r1 = (exit_price-entry_base)/av if is_long else (entry_base-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "filled": True, "r": r1+r2, "hold": exit_idx-i,
            "exit_reason": exit_reason, "confirmed": True, "addon": addon_done,
            "entry_price": fill_price, "exit_price": exit_price,
            "year": int(years[i]), "sig_idx": i}


def run(mode):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        if mode == "market":
            r = simulate_market(s)
            if r:
                results.append(r)
                last_exit = r["exit_idx"]
        else:
            r = simulate_limit(s)
            if r:
                results.append(r)
                if r.get("filled", False):
                    last_exit = r["exit_idx"]
    return pd.DataFrame(results)


print("=" * 110)
print("限价入场 vs 市价入场对比")
print("=" * 110)
print(f"配置：回调基准+0.5ATR确认+1ATR止损+1.5ATR加仓+做多22%/做空12%止盈+1min消歧")
print(f"      出场都市价（触及线价成交，跳空用开盘价），不计滑点")
print()

for mode, label in [("limit", "限价入场（方案B，当前）"), ("market", "市价入场（收盘价入场，止损止盈基于close[i]）")]:
    rdf = run(mode)
    if mode == "limit":
        rdf = rdf[rdf.get("filled", True) == True]
    sr = rdf["r"]
    print(f"{'━'*110}")
    print(label)
    print(f"{'━'*110}")
    print(f"  单数={len(rdf)}  累计R={sr.sum():.1f}  均R={sr.mean():.2f}  胜率={(sr>0).mean():.0%}  PF={sr[sr>0].sum()/abs(sr[sr<=0].sum()):.1f}")
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = rdf[rdf["dir"] == d]
        if sub.empty: continue
        print(f"    {lbl}: {len(sub)}单  累计R={sub['r'].sum():.1f}  均R={sub['r'].mean():.2f}  胜率={(sub['r']>0).mean():.0%}")
    print(f"  出场方式:")
    for reason, cnt in rdf["exit_reason"].value_counts().items():
        sub = rdf[rdf["exit_reason"] == reason]
        print(f"    {reason:>10}: {cnt:>5}单 ({cnt/len(rdf):.0%})  累计R={sub['r'].sum():>7.1f}  均R={sub['r'].mean():>6.2f}")
    print(f"  年度:")
    for yr in sorted(rdf["year"].unique()):
        sub = rdf[rdf["year"] == yr]
        print(f"    {yr}: {len(sub):>4}单  累计R={sub['r'].sum():>7.1f}  均R={sub['r'].mean():.2f}")
    print()
