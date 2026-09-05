"""正确时序回测：信号K=i开盘挂单entry_base，6根K内成交否则作废。
成交即持仓，从成交K+1起止损止盈。对比当前逻辑(只检查信号K内)。
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
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; FILL_WINDOW = 6

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


def simulate_holding(fill_k, d, entry_base, av, sig_k):
    """从fill_k+1开始模拟持仓。sig_k=信号K(用于确认判断和前3K止损基准)。"""
    is_long = (d == "long")
    confirmed = (closes[sig_k] >= entry_base + 0.5*av) if is_long else (closes[sig_k] <= entry_base - 0.5*av)

    # 弱突破 + 信号K内成交 → 当K(信号K)平仓
    if not confirmed and fill_k == sig_k:
        ep = float(closes[sig_k])
        r1 = (ep-entry_base)/av if is_long else (entry_base-ep)/av
        return sig_k, r1, "弱突破平仓", False, False, ep

    # 弱突破 + 信号K后成交 → 正常持仓（弱突破当K平仓已过）
    # 确认或弱突破后成交 → 正常持仓
    if is_long: init_stop = entry_base - STOP_MULT*av; tp_price = entry_base*(1+TP_LONG)
    else: init_stop = entry_base + STOP_MULT*av; tp_price = entry_base*(1-TP_SHORT)

    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None; ambig = False

    for j in range(fill_k+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - sig_k  # 持仓K数相对信号K（前3K止损用）
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
    return exit_idx, r1+r2, exit_reason, True, addon_done, exit_price


def run(mode):
    """mode: 'current'=只检查信号K内, 'correct'=6根K内成交"""
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        i = s.idx
        if i + 1 >= n4h: continue
        d = s.direction; is_long = (d == "long")
        av = atr_vals[i-1] if i-1 >= 0 else np.nan
        if np.isnan(av) or av <= 0: continue
        cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
        entry_base = cb_extreme if cb_extreme is not None else s.breakthrough

        # 找成交K
        fill_k = None; fill_price = None
        if mode == "current":
            # 只检查信号K内
            if (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base):
                fill_k = i
                fill_price = float(opens[i]) if (opens[i] < entry_base if is_long else opens[i] > entry_base) else float(entry_base)
        else:
            # 6根K内成交
            for k in range(i, min(i + FILL_WINDOW, n4h)):
                if is_long and lows[k] <= entry_base:
                    fill_k = k
                    fill_price = float(opens[k]) if opens[k] < entry_base else float(entry_base)
                    break
                if not is_long and highs[k] >= entry_base:
                    fill_k = k
                    fill_price = float(opens[k]) if opens[k] > entry_base else float(entry_base)
                    break

        if fill_k is None:
            continue  # 未成交/作废

        exit_idx, r, reason, confirmed, addon, exit_price = simulate_holding(fill_k, d, entry_base, av, i)
        results.append({
            "sig_idx": i, "fill_k": fill_k, "fill_delay": fill_k - i,
            "dir": d, "r": r, "exit_reason": reason, "confirmed": confirmed,
            "addon": addon, "entry_base": entry_base, "fill_price": fill_price,
            "exit_idx": exit_idx, "year": int(years[i]),
        })
        last_exit = exit_idx

    return pd.DataFrame(results)


print("=" * 110)
print("时序对比：当前(只检查信号K内) vs 正确(6根K内成交)")
print("=" * 110)
print(f"配置：回调基准+0.5ATR确认+1ATR止损+1.5ATR加仓+做多22%/做空12%止盈+1min消歧")
print(f"      信号K开盘挂单entry_base，6根K内成交否则作废，成交即持仓")
print()

for mode, label in [("current", "当前逻辑（只检查信号K内成交）"),
                     ("correct", "正确时序（6根K内成交，成交即持仓）")]:
    rdf = run(mode)
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

    if mode == "correct":
        # 成交延迟分布
        print(f"  成交延迟分布:")
        for delay in sorted(rdf["fill_delay"].unique()):
            sub = rdf[rdf["fill_delay"] == delay]
            print(f"    信号K+{delay}: {len(sub):>5}单  累计R={sub['r'].sum():>7.1f}  均R={sub['r'].mean():>6.2f}  胜率={(sub['r']>0).mean():.0%}")

    print(f"  年度:")
    for yr in sorted(rdf["year"].unique()):
        sub = rdf[rdf["year"] == yr]
        print(f"    {yr}: {len(sub):>4}单  累计R={sub['r'].sum():>7.1f}  均R={sub['r'].mean():.2f}")
    print()
