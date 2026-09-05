"""现货 vs 永续合约对比：最优配置全参数不变，仅切换数据源"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import ccxt
from pathlib import Path
from dotenv import load_dotenv
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

# ========== 现货数据加载 ==========
def load_spot_klines():
    """加载币安现货 BTC/USDT 4H K线"""
    root_env = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(dotenv_path=root_env)
    proxy = os.getenv("BINANCE_PROXY", "").strip()
    opts = {"enableRateLimit": True, "timeout": 30000}
    if proxy:
        opts["proxies"] = {"http": proxy, "https": proxy}
    exchange = ccxt.binance(opts)

    cache = Path(__file__).resolve().parent / "cache" / "BTCUSDT_spot_4h.parquet"
    symbol = "BTC/USDT"
    timeframe = "4h"
    since_str = "2019-09-01T00:00:00Z"

    df_existing = pd.DataFrame()
    if cache.exists():
        df_existing = pd.read_parquet(cache)

    if not df_existing.empty:
        since_ms = int(df_existing["open_time_ms"].iloc[-1]) + 1
        print(f"现货缓存命中 {len(df_existing)} 根，增量拉取自 {df_existing['open_time'].iloc[-1]}")
    else:
        since_ms = exchange.parse8601(since_str)
        print(f"无现货缓存，全量拉取自 {since_str}")

    all_ohlcv = []
    while True:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
        if not ohlcv:
            break
        all_ohlcv.extend(ohlcv)
        since_ms = ohlcv[-1][0] + 1
        if len(ohlcv) < 1000:
            break
        time.sleep(exchange.rateLimit / 1000)

    if all_ohlcv:
        from data_loader import drop_unclosed_bars, ensure_open_time, upsert_ohlcv, write_cache
        df_new = pd.DataFrame(all_ohlcv, columns=["open_time_ms", "open", "high", "low", "close", "volume"])
        df_new = ensure_open_time(df_new)
        df_existing = ensure_open_time(df_existing) if not df_existing.empty else df_existing
        closed_old, _ = drop_unclosed_bars(df_existing, timeframe) if not df_existing.empty else (df_existing, df_existing)
        df = upsert_ohlcv(closed_old, df_new)
        df, unclosed = drop_unclosed_bars(df, timeframe)
        write_cache(df, cache)
        print(f"拉取新增 {len(all_ohlcv)} 根，总计 {len(df)} 根，已写入缓存"
              + (f"（未收盘 {len(unclosed)} 根未落盘）" if len(unclosed) else ""))
        return df

    if not df_existing.empty:
        print(f"无新增数据，沿用缓存 {len(df_existing)} 根")
    return df_existing

# ========== 回测引擎（与最优配置完全一致）==========
def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

def find_callback_extreme(i, direction, highs, lows, n, window=6):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n: continue
        if direction == "long":
            if highs[j + 1] <= highs[j] and highs[j + 2] <= highs[j]:
                return highs[j], j
        else:
            if lows[j + 1] >= lows[j] and lows[j + 2] >= lows[j]:
                return lows[j], j
    return None, None

def simulate(signal, df, kama_vals, atr_vals, highs, lows, closes, opens, n, dates,
             use_callback=True, addon=True, confirm_atr=0.5, stop_mult=0.3,
             addon_atr=1.5, tp_l=0.22, tp_s=0.12, window=6):
    i = signal.idx
    if i + 1 >= n: return None
    bt_6k = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    if use_callback:
        cb_extreme, cb_idx = find_callback_extreme(i, d, highs, lows, n, window)
        entry_base = cb_extreme if cb_extreme is not None else bt_6k
    else:
        entry_base = bt_6k

    if is_long:
        confirmed = closes[i] >= entry_base + confirm_atr * av
    else:
        confirmed = closes[i] <= entry_base - confirm_atr * av

    entry_price = entry_base
    if is_long:
        init_stop = entry_price - stop_mult * av; tp_price = entry_base * (1 + tp_l)
    else:
        init_stop = entry_price + stop_mult * av; tp_price = entry_base * (1 - tp_s)

    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
        return {"r": r1, "exit_idx": i, "hold": 0, "year": int(dates[i]),
                "dir": d, "pre3k": True, "confirmed": False, "reason": "弱突破平仓",
                "addon": False, "filled": True}

    peak = entry_price; addon_done = False; addon_price = 0.0
    for j in range(i + 1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(ks, entry_base) if is_long else min(ks, entry_base)
        if addon and not addon_done:
            cg_abs = (closes[j]-entry_base) if is_long else (entry_base-closes[j])
            if cg_abs >= addon_atr * av:
                addon_done = True; addon_price = float(closes[j])
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            r1 = (ep-entry_price)/av; r2 = ((ep-addon_price)/av if addon_done else 0.0)
            reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "reason": reason,
                    "addon": addon_done, "filled": True}
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r1 = (entry_price-ep)/av; r2 = ((addon_price-ep)/av if addon_done else 0.0)
            reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "reason": reason,
                    "addon": addon_done, "filled": True}
        if is_long and highs[j] >= tp_price:
            r1 = (tp_price-entry_price)/av; r2 = ((tp_price-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "reason": "止盈",
                    "addon": addon_done, "filled": True}
        if not is_long and lows[j] <= tp_price:
            r1 = (entry_price-tp_price)/av; r2 = ((addon_price-tp_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "reason": "止盈",
                    "addon": addon_done, "filled": True}
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    ep = float(closes[-1])
    r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
    r2 = ((ep-addon_price)/av if is_long else (addon_price-ep)/av) if addon_done else 0.0
    return {"r": r1+r2, "exit_idx": n-1, "hold": n-1-i, "year": int(dates[n-1]),
            "dir": d, "pre3k": False, "confirmed": True, "reason": "未平仓",
            "addon": addon_done, "filled": True}

def run_backtest(df, label, use_callback=True, addon=True):
    cfg = BacktestConfig()
    kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
    signals = detect_breakout(df, cfg.window)
    highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
    closes = df["close"].to_numpy(); opens = df["open"].to_numpy(); n = len(df)
    dates = df['open_time'].dt.year.to_numpy()
    atr_vals = calc_atr(highs, lows, closes, 20)

    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, df, kama_vals, atr_vals, highs, lows, closes, opens, n, dates,
                     use_callback=use_callback, addon=addon)
        if r: results.append(r); last_exit = r["exit_idx"]
    rdf = pd.DataFrame(results)
    return rdf

# ========== 主流程 ==========
print("=" * 110)
print("现货 vs 永续合约 对比（最优配置：回调高点+0.5ATR确认+0.3ATR止损+1.5ATR加仓+多22%/空12%）")
print("=" * 110)

# 加载永续数据
from data_loader import load_klines
cfg = BacktestConfig()
print("\n--- 加载永续合约数据 ---")
df_futures = load_klines(cfg)

# 加载现货数据
print("\n--- 加载现货数据 ---")
df_spot = load_spot_klines()

print(f"\n永续: {len(df_futures)}根, {df_futures['open_time'].iloc[0].date()} ~ {df_futures['open_time'].iloc[-1].date()}")
print(f"现货: {len(df_spot)}根, {df_spot['open_time'].iloc[0].date()} ~ {df_spot['open_time'].iloc[-1].date()}")

# 对齐时间范围
t_start = max(df_futures['open_time'].iloc[0], df_spot['open_time'].iloc[0])
t_end = min(df_futures['open_time'].iloc[-1], df_spot['open_time'].iloc[-1])
df_f = df_futures[(df_futures['open_time'] >= t_start) & (df_futures['open_time'] <= t_end)].reset_index(drop=True)
df_s = df_spot[(df_spot['open_time'] >= t_start) & (df_spot['open_time'] <= t_end)].reset_index(drop=True)
print(f"对齐后: {t_start.date()} ~ {t_end.date()}, 永续{len(df_f)}根, 现货{len(df_s)}根")

# 价格差异统计
print(f"\n{'='*110}")
print("现货 vs 永续 价格差异")
print(f"{'='*110}")
merge = pd.merge(df_f[['open_time','close']].rename(columns={'close':'close_f'}),
                 df_s[['open_time','close']].rename(columns={'close':'close_s'}),
                 on='open_time')
diff_pct = (merge['close_f'] - merge['close_s']) / merge['close_s'] * 100
print(f"\n  收盘价差异(永续-现货)/现货: 均值={diff_pct.mean():.4f}%, 中位={diff_pct.median():.4f}%, 最大={diff_pct.max():.3f}%, 最小={diff_pct.min():.3f}%")

# 运行4组对比
combos = [
    ("永续·回调+加仓",   df_f, True, True),
    ("永续·回调无加仓",   df_f, True, False),
    ("现货·回调+加仓",   df_s, True, True),
    ("现货·回调无加仓",   df_s, True, False),
]

all_results = {}
for name, data, uc, ad in combos:
    rdf = run_backtest(data, name, use_callback=uc, addon=ad)
    all_results[name] = rdf

# 汇总
print(f"\n{'='*110}")
print("回测结果对比")
print(f"{'='*110}")
print(f"\n{'配置':>18} {'累计R':>8} {'单数':>6} {'均R':>6} {'胜率':>6} {'最大R':>6} {'最小R':>6} {'最大回撤':>8}")
print("-" * 70)
for name, rdf in all_results.items():
    cumr = rdf['r'].cumsum()
    max_dd = (cumr.cummax() - cumr).max()
    print(f"{name:>18} {rdf['r'].sum():>8.1f} {len(rdf):>6} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {rdf['r'].max():>6.1f} {rdf['r'].min():>6.1f} {max_dd:>8.1f}")

# 出场原因对比
print(f"\n{'='*110}")
print("出场原因对比（回调+加仓）")
print(f"{'='*110}")
print(f"\n{'出场原因':>14} {'永续单数':>8} {'永续R':>8} {'现货单数':>8} {'现货R':>8} {'R差异':>8}")
print("-" * 60)
fut = all_results["永续·回调+加仓"]
spot = all_results["现货·回调+加仓"]
for reason in fut['reason'].unique():
    f_sub = fut[fut['reason'] == reason]
    s_sub = spot[spot['reason'] == reason] if reason in spot['reason'].values else pd.DataFrame()
    s_r = s_sub['r'].sum() if len(s_sub) > 0 else 0
    s_n = len(s_sub)
    print(f"{reason:>14} {len(f_sub):>8} {f_sub['r'].sum():>8.1f} {s_n:>8} {s_r:>8.1f} {s_r-f_sub['r'].sum():>+8.1f}")

# 年度对比
print(f"\n{'='*110}")
print("年度对比（回调+加仓）")
print(f"{'='*110}")
print(f"\n{'年份':>6}  {'永续R':>10}  {'现货R':>10}  {'差异':>8}  {'永续单':>6}  {'现货单':>6}")
print("-" * 55)
for yr in sorted(fut['year'].unique()):
    f_r = fut[fut['year']==yr]['r'].sum()
    s_r = spot[spot['year']==yr]['r'].sum() if yr in spot['year'].values else 0
    f_n = len(fut[fut['year']==yr])
    s_n = len(spot[spot['year']==yr]) if yr in spot['year'].values else 0
    print(f"{yr:>6}  {f_r:>10.1f}  {s_r:>10.1f}  {s_r-f_r:>+8.1f}  {f_n:>6}  {s_n:>6}")
print(f"{'合计':>6}  {fut['r'].sum():>10.1f}  {spot['r'].sum():>10.1f}  {spot['r'].sum()-fut['r'].sum():>+8.1f}  {len(fut):>6}  {len(spot):>6}")

# 多空对比
print(f"\n{'='*110}")
print("多空对比（回调+加仓）")
print(f"{'='*110}")
print(f"\n{'方向':>6} {'数据源':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6}")
print("-" * 45)
for d in ["long", "short"]:
    for label, rdf in [("永续", fut), ("现货", spot)]:
        sub = rdf[rdf['dir'] == d]
        d_label = "做多" if d == "long" else "做空"
        print(f"{d_label:>6} {label:>6} {len(sub):>6} {sub['r'].sum():>8.1f} {sub['r'].mean() if len(sub)>0 else 0:>6.2f} {(sub['r']>0).mean() if len(sub)>0 else 0:>5.0%}")

# R值分布对比
print(f"\n{'='*110}")
print("R值分布对比（回调+加仓）")
print(f"{'='*110}")
print(f"\n{'R区间':>12} {'永续单数':>8} {'永续R':>8} {'现货单数':>8} {'现货R':>8}")
print("-" * 50)
bins = [(-99,-3),(-3,-1),(-1,0),(0,0.5),(0.5,1),(1,3),(3,5),(5,99)]
for lo, hi in bins:
    f_sub = fut[(fut['r']>=lo) & (fut['r']<hi)]
    s_sub = spot[(spot['r']>=lo) & (spot['r']<hi)]
    label = f"<{hi}" if lo <= -99 else f">={lo}" if hi >= 99 else f"[{lo},{hi})"
    print(f"{label:>12} {len(f_sub):>8} {f_sub['r'].sum():>8.1f} {len(s_sub):>8} {s_sub['r'].sum():>8.1f}")
