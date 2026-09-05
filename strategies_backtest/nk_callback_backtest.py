"""N K回调基准挂单回测：不需要突破信号，NK形成回调极值就挂单等成交。
3K=第1K极值+后2K不创新高/低。对比3K/4K/5K/6K窗口。
入场=限价单挂基准线，成交价=基准线（不计滑点）。
止损=1ATR, 止盈=做多22%/做空12%, KAMA跟踪, 1.5ATR加仓, 1min消歧。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama

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


def find_callback_extreme_nk(j, window):
    """在j收盘时，检查j-window+1..j是否形成回调极值。
    做多：j-window+1是最高点，后面window-1根K不创新高
    做空：j-window+1是最低点，后面window-1根K不创新低
    返回 (entry_base, direction) 或 (None, None)
    """
    peak_idx = j - window + 1
    if peak_idx < 0: return None, None
    # 做多回调高点
    is_long_cb = True
    for k in range(peak_idx + 1, j + 1):
        if highs[k] > highs[peak_idx]:
            is_long_cb = False; break
    # 做空回调低点
    is_short_cb = True
    for k in range(peak_idx + 1, j + 1):
        if lows[k] < lows[peak_idx]:
            is_short_cb = False; break
    if is_long_cb and is_short_cb:
        return None, None  # 同时形成，跳过
    if is_long_cb:
        return highs[peak_idx], "long"
    if is_short_cb:
        return lows[peak_idx], "short"
    return None, None


def simulate_holding(fill_k, d, fill_price, av, sig_k):
    """从fill_k+1开始模拟持仓。"""
    is_long = (d == "long")
    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)

    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None; ambig = False
    mfe = -np.inf if is_long else np.inf; mae = np.inf if is_long else -np.inf

    for j in range(fill_k+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - sig_k
        sl = init_stop if bh <= 3 else (max(ks, fill_price) if is_long else min(ks, fill_price))

        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
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
    mfe_pct = (mfe-fill_price)/fill_price*100 if is_long else (fill_price-mfe)/fill_price*100
    mae_pct = (mae-fill_price)/fill_price*100 if is_long else (fill_price-mae)/fill_price*100
    return {"exit_idx": exit_idx, "r": r1+r2, "exit_reason": exit_reason,
            "exit_price": exit_price, "addon": addon_done, "addon_price": addon_price,
            "ambig": ambig, "mfe_pct": mfe_pct, "mae_pct": mae_pct,
            "hold": exit_idx - fill_k, "init_stop": init_stop, "tp_price": tp_price}


def run_backtest(window):
    """NK回调基准挂单回测。"""
    results = []; last_exit = -1
    for j in range(window - 1, n4h):
        if j <= last_exit: continue
        entry_base, d = find_callback_extreme_nk(j, window)
        if entry_base is None: continue

        av = atr_vals[j]
        if np.isnan(av) or av <= 0: continue

        is_long = (d == "long")
        # 挂单后检查j+1是否成交
        fill_k = j + 1
        if fill_k >= n4h: continue
        filled = (lows[fill_k] <= entry_base) if is_long else (highs[fill_k] >= entry_base)
        if not filled: continue  # 没成交就跳过（信号K内撤单逻辑）

        fill_price = float(entry_base)  # 成交价=基准线
        h = simulate_holding(fill_k, d, fill_price, av, fill_k)
        results.append({
            "sig_idx": j, "fill_k": fill_k, "dir": d,
            "entry_base": entry_base, "fill_price": fill_price, "atr": av,
            "year": int(years[fill_k]), **h
        })
        last_exit = h["exit_idx"]
    return pd.DataFrame(results)


print("=" * 110)
print("NK回调基准挂单回测（不需要突破信号，回调极值形成即挂单）")
print("=" * 110)
print(f"配置：回调基准挂单+1ATR止损+1.5ATR加仓+做多22%/做空12%止盈+1min消歧")
print(f"      成交价=基准线，出场价=线价（不计滑点），信号K内撤单")
print(f"数据：{df_4h['open_time'].iloc[0]} ~ {df_4h['open_time'].iloc[-1]}，{n4h}根4H K线")
print()

for window in [3, 4, 5, 6]:
    rdf = run_backtest(window)
    sr = rdf["r"]
    print(f"{'━'*110}")
    print(f"{window}K回调基准（第1K极值+后{window-1}K确认）")
    print(f"{'━'*110}")
    if len(rdf) == 0:
        print("  无信号"); print(); continue
    pf = sr[sr>0].sum() / abs(sr[sr<=0].sum()) if sr[sr<=0].sum() != 0 else float("inf")
    print(f"  单数={len(rdf)}  累计R={sr.sum():.1f}  均R={sr.mean():.2f}  胜率={(sr>0).mean():.0%}  PF={pf:.1f}")
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
