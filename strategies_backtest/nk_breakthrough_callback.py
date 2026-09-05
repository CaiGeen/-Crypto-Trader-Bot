"""NK突破 + NK回调基准组合回测。窗口统一：NK突破信号 + NK区间内2K确认回调极值。
对比3K/4K/5K/6K，其他逻辑同report_1atr.py。
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
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy()
n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy()
times = df_4h["open_time"].tolist()

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
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0

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
    """在[i-window, i-1]区间找2K确认的回调极值。"""
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n4h: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]: return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] >= lows[j]: return lows[j], j
    return None, None


def simulate(signal, window):
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_nk = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, _ = find_callback_extreme(i, d, window)
    entry_base = cb_extreme if cb_extreme is not None else bt_nk
    has_cb = cb_extreme is not None

    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)
    fill_price = float(entry_base) if filled else 0.0

    if not filled:
        return {"dir": d, "exit_idx": i, "filled": False, "has_cb": has_cb,
                "r": 0.0, "hold": 0, "exit_reason": "限价未成交",
                "confirmed": confirmed, "addon": False, "fill_price": 0.0,
                "exit_price": 0.0, "year": int(years[i]), "sig_idx": i}

    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {"dir": d, "exit_idx": i, "filled": True, "has_cb": has_cb,
                "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                "confirmed": False, "addon": False, "fill_price": fill_price,
                "exit_price": ep, "year": int(years[i]), "sig_idx": i}

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
                    exit_idx = j; exit_price = ep
                    exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
                else:
                    exit_idx = j; exit_price = ep; exit_reason = "止盈"
                break
            ep = float(sl)
            exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_stop:
            ep = float(sl)
            exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); exit_reason = "止盈"; break
    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1]); exit_reason = "未平仓"

    r1 = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "filled": True, "has_cb": has_cb,
            "r": r1+r2, "hold": exit_idx-i, "exit_reason": exit_reason,
            "confirmed": True, "addon": addon_done, "fill_price": fill_price,
            "exit_price": exit_price, "year": int(years[i]), "sig_idx": i}


def run(window):
    signals = detect_breakout(df_4h, window)
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, window)
        if r:
            results.append(r)
            if r["filled"]:
                last_exit = r["exit_idx"]
    return pd.DataFrame(results), len(signals)


print("=" * 110)
print("NK突破 + NK回调基准 组合回测")
print("=" * 110)
print(f"配置：NK突破信号 + NK区间2K确认回调极值 + 0.5ATR确认 + 1ATR止损 + 1.5ATR加仓")
print(f"      做多22%/做空12%止盈 + 1min消歧 + 成交价=基准线 + 出场价=线价")
print()

for window in [3, 4, 5, 6]:
    rdf, n_raw = run(window)
    filled = rdf[rdf["filled"]]
    sr = filled["r"]
    print(f"{'━'*110}")
    print(f"{window}K突破 + {window}K回调基准（原始信号{n_raw}）")
    print(f"{'━'*110}")
    if len(filled) == 0:
        print("  无成交"); print(); continue
    pf = sr[sr>0].sum() / abs(sr[sr<=0].sum()) if sr[sr<=0].sum() != 0 else float("inf")
    pf_str = f"{pf:.1f}" if pf < 1000 else "inf"
    print(f"  总波段={len(rdf)}  成交={len(filled)}  未成交={len(rdf)-len(filled)}  累计R={sr.sum():.1f}  均R={sr.mean():.2f}  胜率={(sr>0).mean():.0%}  PF={pf_str}")
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = filled[filled["dir"] == d]
        if sub.empty: continue
        print(f"    {lbl}: {len(sub)}单  累计R={sub['r'].sum():.1f}  均R={sub['r'].mean():.2f}  胜率={(sub['r']>0).mean():.0%}")
    # 回调基准 vs 回退NK极值
    for has_cb, lbl in [(True, "回调基准"), (False, f"回退{window}K极值")]:
        sub = filled[filled["has_cb"] == has_cb]
        if sub.empty: continue
        print(f"    {lbl}: {len(sub)}单 ({len(sub)/len(filled):.0%})  累计R={sub['r'].sum():.1f}  均R={sub['r'].mean():.2f}")
    print(f"  出场方式:")
    for reason, cnt in filled["exit_reason"].value_counts().items():
        sub = filled[filled["exit_reason"] == reason]
        print(f"    {reason:>10}: {cnt:>5}单 ({cnt/len(filled):.0%})  累计R={sub['r'].sum():>7.1f}  均R={sub['r'].mean():>6.2f}")
    print(f"  年度:")
    for yr in sorted(filled["year"].unique()):
        sub = filled[filled["year"] == yr]
        print(f"    {yr}: {len(sub):>4}单  累计R={sub['r'].sum():>7.1f}  均R={sub['r'].mean():.2f}")
    print()
