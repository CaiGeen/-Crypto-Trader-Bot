"""对比未成交单处理方案：A=当前(占用last_exit) B=跳过 C=市价追入。计算完整R。"""
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
o1m = df_1m["open"].to_numpy(); h1m = df_1m["high"].to_numpy()
l1m = df_1m["low"].to_numpy(); c1m = df_1m["close"].to_numpy()
n1m = len(df_1m)
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
            if l1m[k] < sl: return ("stop", float(sl), k)
            if h1m[k] >= tp_price: return ("tp", float(tp_price), k)
        else:
            if h1m[k] > sl: return ("stop", float(sl), k)
            if l1m[k] <= tp_price: return ("tp", float(tp_price), k)
    return None

def simulate_full(i, d, entry_base, av, fill_price):
    """完整模拟：含加仓、1min消歧、KAMA跟踪。返回(exit_idx, r, exit_reason, confirmed, addon)"""
    is_long = (d == "long")
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)

    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-entry_base)/av if is_long else (entry_base-ep)/av
        return i, r1, "弱突破平仓", False, False

    if is_long: init_stop = entry_base - STOP_MULT*av; tp_price = entry_base*(1+TP_LONG)
    else: init_stop = entry_base + STOP_MULT*av; tp_price = entry_base*(1-TP_SHORT)

    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None

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
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep, _ = result
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
    return exit_idx, r1+r2, exit_reason, True, addon_done


def run_backtest(mode):
    """mode: 'A'=当前(未成交占用last_exit), 'B'=跳过, 'C'=市价追入"""
    results = []; last_exit = -1; stats = {"total": 0, "filled": 0, "unfilled": 0, "blocked": 0, "chased": 0}

    for s in signals:
        if s.idx <= last_exit:
            stats["blocked"] += 1
            continue
        i = s.idx
        if i + 1 >= n4h: continue
        stats["total"] += 1

        bt_6k = s.breakthrough; d = s.direction; is_long = (d == "long")
        av = atr_vals[i-1] if i-1 >= 0 else np.nan
        if np.isnan(av) or av <= 0: continue
        cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
        entry_base = cb_extreme if cb_extreme is not None else bt_6k
        filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)

        if filled:
            if is_long: fill_price = float(opens[i]) if opens[i] < entry_base else float(entry_base)
            else: fill_price = float(opens[i]) if opens[i] > entry_base else float(entry_base)
            exit_idx, r, reason, confirmed, addon = simulate_full(i, d, entry_base, av, fill_price)
            results.append({"sig_idx": i, "dir": d, "r": r, "exit_reason": reason,
                            "confirmed": confirmed, "addon": addon, "filled": True, "chased": False,
                            "entry_base": entry_base, "fill_price": fill_price, "exit_idx": exit_idx})
            last_exit = exit_idx
            stats["filled"] += 1
        else:
            stats["unfilled"] += 1
            if mode == 'A':
                # 当前逻辑：模拟持仓并占用last_exit（但不计入统计）
                exit_idx, r, reason, confirmed, addon = simulate_full(i, d, entry_base, av, 0.0)
                last_exit = exit_idx
            elif mode == 'B':
                # 跳过：不占用last_exit
                pass
            elif mode == 'C':
                # 市价追入：下一根K线开盘价买入
                if i + 1 < n4h:
                    chase_price = float(opens[i+1])
                    stats["chased"] += 1
                    exit_idx, r, reason, confirmed, addon = simulate_full(i+1, d, chase_price, av, chase_price)
                    results.append({"sig_idx": i, "dir": d, "r": r, "exit_reason": reason,
                                    "confirmed": confirmed, "addon": addon, "filled": True, "chased": True,
                                    "entry_base": chase_price, "fill_price": chase_price, "exit_idx": exit_idx})
                    last_exit = exit_idx
                    stats["filled"] += 1

    return pd.DataFrame(results), stats


print("=" * 110)
print("未成交单处理方案对比")
print("=" * 110)
print(f"配置：回调基准+0.5ATR确认+1ATR止损+1.5ATR加仓+做多22%/做空12%止盈+1min消歧")
print()

for mode, label in [("A", "方案A：当前逻辑（未成交占用last_exit，不计入统计）"),
                     ("B", "方案B：跳过未成交（不占用last_exit）"),
                     ("C", "方案C：市价追入（下一根K线开盘价买入）")]:
    rdf, stats = run_backtest(mode)
    sr = rdf["r"]
    print(f"{'━'*110}")
    print(label)
    print(f"{'━'*110}")
    print(f"  总信号: {stats['total']}  成交: {stats['filled']}  未成交: {stats['unfilled']}  "
          f"被阻塞: {stats['blocked']}  追入: {stats['chased']}")
    if len(rdf) > 0:
        print(f"  累计R={sr.sum():.1f}  均R={sr.mean():.2f}  胜率={(sr>0).mean():.0%}  单数={len(rdf)}")
        for d, lbl in [("long", "做多"), ("short", "做空")]:
            sub = rdf[rdf["dir"] == d]
            if sub.empty: continue
            print(f"    {lbl}: {len(sub)}单  累计R={sub['r'].sum():.1f}  均R={sub['r'].mean():.2f}  胜率={(sub['r']>0).mean():.0%}")

        if mode == 'C':
            chased = rdf[rdf["chased"]]
            normal = rdf[~rdf["chased"]]
            print(f"  ─ 追入单: {len(chased)}单  累计R={chased['r'].sum():.1f}  均R={chased['r'].mean():.2f}  胜率={(chased['r']>0).mean():.0%}")
            print(f"  ─ 正常单: {len(normal)}单  累计R={normal['r'].sum():.1f}  均R={normal['r'].mean():.2f}  胜率={(normal['r']>0).mean():.0%}")

        # 按出场方式
        print(f"  出场方式:")
        for reason, cnt in rdf["exit_reason"].value_counts().items():
            sub = rdf[rdf["exit_reason"] == reason]
            print(f"    {reason:>10}: {cnt:>5}单  累计R={sub['r'].sum():>7.1f}  均R={sub['r'].mean():>6.2f}")
    print()
