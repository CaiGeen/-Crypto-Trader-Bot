"""现货早期数据验证：2015-01 ~ 2019-09（永续上线前），最优配置不变"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
import ccxt
from pathlib import Path
from dotenv import load_dotenv
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

# ========== 现货早期数据加载 ==========
def load_spot_early():
    root_env = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(dotenv_path=root_env)
    proxy = os.getenv("BINANCE_PROXY", "").strip()
    opts = {"enableRateLimit": True, "timeout": 30000}
    if proxy:
        opts["proxies"] = {"http": proxy, "https": proxy}
    exchange = ccxt.binance(opts)

    cache = Path(__file__).resolve().parent / "cache" / "BTCUSDT_spot_4h_early.parquet"
    symbol = "BTC/USDT"
    timeframe = "4h"
    since_str = "2015-01-01T00:00:00Z"
    end_str = "2019-09-08T00:00:00Z"  # 永续上线前

    df_existing = pd.DataFrame()
    if cache.exists():
        df_existing = pd.read_parquet(cache)

    if not df_existing.empty:
        since_ms = int(df_existing["open_time_ms"].iloc[-1]) + 1
        print(f"早期现货缓存命中 {len(df_existing)} 根，增量拉取自 {df_existing['open_time'].iloc[-1]}")
    else:
        since_ms = exchange.parse8601(since_str)
        print(f"无缓存，全量拉取现货 {since_str} ~ {end_str}")

    end_ms = exchange.parse8601(end_str)
    all_ohlcv = []
    while True:
        if since_ms >= end_ms:
            break
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
        df, _ = drop_unclosed_bars(df, timeframe)
        # 只保留永续上线前
        end_ts = pd.Timestamp(end_str, tz="UTC")
        df = df[df["open_time"] < end_ts].reset_index(drop=True)
        write_cache(df, cache)
        print(f"拉取新增 {len(all_ohlcv)} 根，总计 {len(df)} 根，已写入缓存")
        return df

    if not df_existing.empty:
        print(f"无新增数据，沿用缓存 {len(df_existing)} 根")
    return df_existing

# ========== 回测引擎 ==========
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

def simulate(signal, kama_vals, atr_vals, highs, lows, closes, opens, n, dates,
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
                "dir": d, "pre3k": True, "confirmed": False, "reason": "弱突破平仓", "addon": False}

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
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "reason": reason, "addon": addon_done}
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r1 = (entry_price-ep)/av; r2 = ((addon_price-ep)/av if addon_done else 0.0)
            reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "reason": reason, "addon": addon_done}
        if is_long and highs[j] >= tp_price:
            r1 = (tp_price-entry_price)/av; r2 = ((tp_price-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "reason": "止盈", "addon": addon_done}
        if not is_long and lows[j] <= tp_price:
            r1 = (entry_price-tp_price)/av; r2 = ((addon_price-tp_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "reason": "止盈", "addon": addon_done}
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    ep = float(closes[-1])
    r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
    r2 = ((ep-addon_price)/av if is_long else (addon_price-ep)/av) if addon_done else 0.0
    return {"r": r1+r2, "exit_idx": n-1, "hold": n-1-i, "year": int(dates[n-1]),
            "dir": d, "pre3k": False, "confirmed": True, "reason": "未平仓", "addon": addon_done}

def run_backtest(df, use_callback=True, addon=True):
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
        r = simulate(s, kama_vals, atr_vals, highs, lows, closes, opens, n, dates,
                     use_callback=use_callback, addon=addon)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

# ========== 主流程 ==========
print("=" * 110)
print("现货早期验证：2015-01 ~ 2019-09（永续上线前）")
print("最优配置：回调高点+0.5ATR确认+0.3ATR止损+1.5ATR加仓+多22%/空12%")
print("=" * 110)

df = load_spot_early()
print(f"\n数据: {len(df)}根, {df['open_time'].iloc[0].date()} ~ {df['open_time'].iloc[-1].date()}")
print(f"价格范围: ${df['close'].min():.0f} ~ ${df['close'].max():.0f}")

# 运行4组
combos = [
    ("回调+加仓",   True, True),
    ("回调无加仓",   True, False),
    ("6K极值+加仓",  False, True),
    ("6K极值无加仓", False, False),
]

results = {}
for name, uc, ad in combos:
    results[name] = run_backtest(df, use_callback=uc, addon=ad)

# 汇总
print(f"\n{'='*110}")
print("回测结果（2015-01 ~ 2019-09）")
print(f"{'='*110}")
print(f"\n{'配置':>14} {'累计R':>8} {'单数':>6} {'均R':>6} {'胜率':>6} {'最大R':>6} {'最小R':>6} {'最大回撤':>8}")
print("-" * 65)
for name, rdf in results.items():
    cumr = rdf['r'].cumsum()
    max_dd = (cumr.cummax() - cumr).max()
    print(f"{name:>14} {rdf['r'].sum():>8.1f} {len(rdf):>6} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {rdf['r'].max():>6.1f} {rdf['r'].min():>6.1f} {max_dd:>8.1f}")

# 与永续期对比
print(f"\n{'='*110}")
print("与永续期（2019-09 ~ 2026-08）对比")
print(f"{'='*110}")
fut_r = 1732.1  # 永续期回调+加仓
early_r = results["回调+加仓"]['r'].sum()
print(f"\n  早期(2015-2019): {early_r:.1f}R, {len(results['回调+加仓'])}单, 均R={results['回调+加仓']['r'].mean():.2f}")
print(f"  永续(2019-2026): {fut_r:.1f}R, 1478单, 均R=1.17")
print(f"  早期年均R: {early_r/4.7:.1f}R/年 (4.7年)")
print(f"  永续年均R: {fut_r/7.0:.1f}R/年 (7.0年)")

# 年度分布
print(f"\n{'='*110}")
print("年度分布（回调+加仓）")
print(f"{'='*110}")
rdf = results["回调+加仓"]
print(f"\n{'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'最大R':>6} {'最小R':>6}")
print("-" * 50)
for yr in sorted(rdf['year'].unique()):
    sub = rdf[rdf['year'] == yr]
    print(f"{yr:>6} {len(sub):>6} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f} {(sub['r']>0).mean():>5.0%} {sub['r'].max():>6.1f} {sub['r'].min():>6.1f}")
print(f"{'合计':>6} {len(rdf):>6} {rdf['r'].sum():>8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {rdf['r'].max():>6.1f} {rdf['r'].min():>6.1f}")

# 出场原因
print(f"\n{'='*110}")
print("出场原因分布（回调+加仓）")
print(f"{'='*110}")
print(f"\n{'出场原因':>14} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6} {'胜率':>6}")
print("-" * 50)
for reason in rdf['reason'].unique():
    sub = rdf[rdf['reason'] == reason]
    print(f"{reason:>14} {len(sub):>6} {len(sub)/len(rdf)*100:>5.0f}% {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f} {(sub['r']>0).mean():>5.0%}")

# 多空对比
print(f"\n{'='*110}")
print("多空对比（回调+加仓）")
print(f"{'='*110}")
print(f"\n{'方向':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6}")
print("-" * 40)
for d in ["long", "short"]:
    sub = rdf[rdf['dir'] == d]
    d_label = "做多" if d == "long" else "做空"
    print(f"{d_label:>6} {len(sub):>6} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f} {(sub['r']>0).mean():>5.0%}")

# R值分布
print(f"\n{'='*110}")
print("R值分布（回调+加仓）")
print(f"{'='*110}")
print(f"\n{'R区间':>12} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6}")
print("-" * 45)
bins = [(-99,-3),(-3,-1),(-1,0),(0,0.5),(0.5,1),(1,3),(3,5),(5,99)]
for lo, hi in bins:
    sub = rdf[(rdf['r']>=lo) & (rdf['r']<hi)]
    label = f"<{hi}" if lo <= -99 else f">={lo}" if hi >= 99 else f"[{lo},{hi})"
    print(f"{label:>12} {len(sub):>6} {len(sub)/len(rdf)*100:>5.0f}% {sub['r'].sum():>8.1f} {sub['r'].mean() if len(sub)>0 else 0:>6.2f}")

# 累计R曲线
print(f"\n{'='*110}")
print("累计R曲线关键点")
print(f"{'='*110}")
cumr = rdf['r'].cumsum()
print(f"\n  最终累计R: {cumr.iloc[-1]:.1f}")
print(f"  最大回撤: {(cumr.cummax()-cumr).max():.1f}R")
print(f"\n  里程碑:")
for t in [100,200,300,400,500,600,700,800]:
    hit = rdf[cumr >= t]
    if len(hit) > 0:
        f = hit.iloc[0]
        print(f"    累计R>{t}: 第{f.name+1}单 ({pd.Timestamp(df.iloc[int(f.name)]['open_time']).tz_convert('Asia/Shanghai').strftime('%Y-%m-%d')})")

# 市场环境对比
print(f"\n{'='*110}")
print("市场环境对比")
print(f"{'='*110}")
print(f"\n  早期(2015-2019): BTC ${df['close'].min():.0f}→${df['close'].max():.0f}, {len(df)}根4H K线")
print(f"  永续(2019-2026): BTC ~$700→$110000+, 15239根4H K线")
print(f"  早期波动更大（BTC从$200到$20000），测试策略在不同市场环境下的鲁棒性")
