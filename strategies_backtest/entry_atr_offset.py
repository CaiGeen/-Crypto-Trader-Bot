"""新入场逻辑v2：挂单于回调基准±1ATR，加回确认阈值、弱突破平仓、紧止损、挂单取消条件。

挂单价格 = 基准 ± 1ATR
挂单取消 = 跌破KAMA(kama_vals[j-1]) 或 连续3K跌破基准线
成交后：
  紧止损 = 入场价 ± 0.3ATR（前3K）
  确认阈值 = 入场价 ± 0.5ATR → 不满足则弱突破平仓（收盘价平仓）
  3K后KAMA跟踪底线 = 基准
  止盈 = 基准 × (1±22%/12%)
  加仓 = 基准 ± 1.5ATR（不含成交当K）
R值 = (出场价 - 入场价) / ATR
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
closes = df["close"].to_numpy(); opens = df["open"].to_numpy(); n = len(df)
dates = df['open_time'].dt.year.to_numpy()

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

def find_callback_extreme(i, direction, window=6):
    """6K箱体内从左往右找最早的2K不创新高/低回调的极值点。"""
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

def simulate(signal, use_callback=False, addon=False,
             entry_atr=1.0, confirm_atr=0.5, stop_mult=0.3, addon_atr=1.5,
             tp_l=0.22, tp_s=0.12):
    i = signal.idx
    if i + 1 >= n: return None
    bt_6k = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    # 确定基准
    if use_callback:
        cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
        if cb_extreme is None:
            return None
        base = cb_extreme
    else:
        base = bt_6k

    # 挂单价格 = 基准 ± entry_atr×ATR
    if is_long:
        order_price = base + entry_atr * av
        tp_price = base * (1 + tp_l)
        addon_threshold = base + addon_atr * av
    else:
        order_price = base - entry_atr * av
        tp_price = base * (1 - tp_s)
        addon_threshold = base - addon_atr * av

    # === 阶段1：等待挂单成交，同时检查取消条件 ===
    fill_idx = -1; fill_price = 0.0
    below_base_count = 0

    for j in range(i + 1, n):
        # 检查成交
        if is_long and highs[j] >= order_price:
            fill_idx = j
            fill_price = float(opens[j]) if opens[j] > order_price else float(order_price)
            break
        if not is_long and lows[j] <= order_price:
            fill_idx = j
            fill_price = float(opens[j]) if opens[j] < order_price else float(order_price)
            break

        # 检查取消条件1：跌破KAMA
        ks = kama_vals[j-1]
        if not np.isnan(ks):
            if is_long and lows[j] < ks:
                return {"r": 0.0, "exit_idx": j, "hold": 0, "year": int(dates[j]),
                        "dir": d, "filled": False, "cancel": "KAMA",
                        "fill_idx": -1, "fill_price": 0.0, "base": base,
                        "order_price": order_price, "confirmed": False}
            if not is_long and highs[j] > ks:
                return {"r": 0.0, "exit_idx": j, "hold": 0, "year": int(dates[j]),
                        "dir": d, "filled": False, "cancel": "KAMA",
                        "fill_idx": -1, "fill_price": 0.0, "base": base,
                        "order_price": order_price, "confirmed": False}

        # 检查取消条件2：连续3K跌破基准线
        if (is_long and closes[j] < base) or (not is_long and closes[j] > base):
            below_base_count += 1
        else:
            below_base_count = 0
        if below_base_count >= 3:
            return {"r": 0.0, "exit_idx": j, "hold": 0, "year": int(dates[j]),
                    "dir": d, "filled": False, "cancel": "3K跌破基准",
                    "fill_idx": -1, "fill_price": 0.0, "base": base,
                    "order_price": order_price, "confirmed": False}

    if fill_idx == -1:
        return {"r": 0.0, "exit_idx": n-1, "hold": 0, "year": int(dates[n-1]),
                "dir": d, "filled": False, "cancel": "未成交",
                "fill_idx": -1, "fill_price": 0.0, "base": base,
                "order_price": order_price, "confirmed": False}

    # === 阶段2：成交后检查 ===
    entry_price = fill_price
    # 前3K止损 = 回调基准
    init_stop = base
    # 确认阈值 = 入场价 ± 0.5ATR
    if is_long:
        confirm_threshold = entry_price + confirm_atr * av
    else:
        confirm_threshold = entry_price - confirm_atr * av

    # 成交K线：先检查止损，再检查确认阈值，再检查止盈
    j = fill_idx
    sl = init_stop
    # 2a. 止损
    if is_long and lows[j] < sl:
        ep = float(opens[j]) if opens[j] < sl else float(sl)
        r1 = (ep - entry_price) / av
        return {"r": r1, "exit_idx": j, "hold": 0, "year": int(dates[j]),
                "dir": d, "filled": True, "cancel": "",
                "fill_idx": fill_idx, "fill_price": fill_price, "base": base,
                "order_price": order_price, "confirmed": True}
    if not is_long and highs[j] > sl:
        ep = float(opens[j]) if opens[j] > sl else float(sl)
        r1 = (entry_price - ep) / av
        return {"r": r1, "exit_idx": j, "hold": 0, "year": int(dates[j]),
                "dir": d, "filled": True, "cancel": "",
                "fill_idx": fill_idx, "fill_price": fill_price, "base": base,
                "order_price": order_price, "confirmed": True}

    # 2b. 确认阈值 → 不满足则弱突破平仓
    confirmed = (closes[j] >= confirm_threshold) if is_long else (closes[j] <= confirm_threshold)
    if not confirmed:
        ep = float(closes[j])
        r1 = (ep - entry_price) / av if is_long else (entry_price - ep) / av
        return {"r": r1, "exit_idx": j, "hold": 0, "year": int(dates[j]),
                "dir": d, "filled": True, "cancel": "",
                "fill_idx": fill_idx, "fill_price": fill_price, "base": base,
                "order_price": order_price, "confirmed": False}

    # 2c. 止盈
    if is_long and highs[j] >= tp_price:
        r1 = (tp_price - entry_price) / av
        return {"r": r1, "exit_idx": j, "hold": 0, "year": int(dates[j]),
                "dir": d, "filled": True, "cancel": "",
                "fill_idx": fill_idx, "fill_price": fill_price, "base": base,
                "order_price": order_price, "confirmed": True}
    if not is_long and lows[j] <= tp_price:
        r1 = (entry_price - tp_price) / av
        return {"r": r1, "exit_idx": j, "hold": 0, "year": int(dates[j]),
                "dir": d, "filled": True, "cancel": "",
                "fill_idx": fill_idx, "fill_price": fill_price, "base": base,
                "order_price": order_price, "confirmed": True}

    # === 阶段3：确认通过，正常持有 ===
    addon_done = False; addon_price = 0.0

    for j in range(fill_idx + 1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - fill_idx
        if bh <= 3:
            sl = init_stop
        else:
            sl = max(ks, base) if is_long else min(ks, base)

        # 加仓（不含成交当K）
        if addon and not addon_done:
            if is_long and closes[j] >= addon_threshold:
                addon_done = True; addon_price = float(closes[j])
            elif not is_long and closes[j] <= addon_threshold:
                addon_done = True; addon_price = float(closes[j])

        # 止损
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            r1 = (ep - entry_price) / av
            r2 = ((ep - addon_price) / av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "filled": True, "cancel": "",
                    "fill_idx": fill_idx, "fill_price": fill_price, "base": base,
                    "order_price": order_price, "confirmed": True}
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r1 = (entry_price - ep) / av
            r2 = ((addon_price - ep) / av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "filled": True, "cancel": "",
                    "fill_idx": fill_idx, "fill_price": fill_price, "base": base,
                    "order_price": order_price, "confirmed": True}

        # 止盈
        if is_long and highs[j] >= tp_price:
            r1 = (tp_price - entry_price) / av
            r2 = ((tp_price - addon_price) / av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "filled": True, "cancel": "",
                    "fill_idx": fill_idx, "fill_price": fill_price, "base": base,
                    "order_price": order_price, "confirmed": True}
        if not is_long and lows[j] <= tp_price:
            r1 = (entry_price - tp_price) / av
            r2 = ((addon_price - tp_price) / av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "filled": True, "cancel": "",
                    "fill_idx": fill_idx, "fill_price": fill_price, "base": base,
                    "order_price": order_price, "confirmed": True}

    # 到最后未平仓
    ep = float(closes[-1])
    r1 = (ep - entry_price) / av if is_long else (entry_price - ep) / av
    r2 = ((ep - addon_price) / av if is_long else (addon_price - ep) / av) if addon_done else 0.0
    return {"r": r1+r2, "exit_idx": n-1, "hold": n-1-fill_idx, "year": int(dates[n-1]),
            "dir": d, "filled": True, "cancel": "",
            "fill_idx": fill_idx, "fill_price": fill_price, "base": base,
            "order_price": order_price, "confirmed": True}

def run(**kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, **kwargs)
        if r:
            results.append(r)
            last_exit = r["exit_idx"]
    return pd.DataFrame(results)

print("=" * 130)
print("新入场逻辑v2：挂单基准±1ATR | 挂单取消=跌破KAMA或连续3K跌破基准 | 紧止损0.3ATR(入场价) | 确认0.5ATR(入场价)")
print("弱突破平仓 | 3K后KAMA底线=基准 | 止盈=基准×(1±22%/12%) | 加仓=基准±1.5ATR(不含成交当K)")
print("=" * 130)

combos = [
    ("6K极值·无加仓",     dict(use_callback=False, addon=False)),
    ("6K极值·1.5ATR加仓", dict(use_callback=False, addon=True)),
    ("回调基准·无加仓",   dict(use_callback=True,  addon=False)),
    ("回调基准·1.5ATR加仓", dict(use_callback=True,  addon=True)),
]

results = {}
for name, kwargs in combos:
    rdf = run(**kwargs)
    results[name] = rdf

# 汇总表
print(f"\n{'配置':>22} {'累计R':>8} {'成交':>6} {'取消':>6} {'未成交':>6} {'弱突破':>6} {'均R':>6} {'胜率':>6} {'前3K':>6} {'前3K亏':>8}")
print("-" * 95)
for name, rdf in results.items():
    filled = rdf[rdf['filled']]
    cancelled = rdf[(~rdf['filled']) & (rdf['cancel'] != "未成交")]
    never_fill = rdf[rdf['cancel'] == "未成交"]
    weak = filled[~filled['confirmed']]
    pre3k = filled[filled['hold'] <= 3]
    total_r = filled['r'].sum()
    print(f"{name:>22} {total_r:>8.1f} {len(filled):>6} {len(cancelled):>6} {len(never_fill):>6} {len(weak):>6} "
          f"{filled['r'].mean():>6.2f} {(filled['r']>0).mean():>5.0%} {len(pre3k):>6} {pre3k['r'].sum():>8.1f}")

# 取消原因统计
print(f"\n{'='*130}")
print("挂单取消原因统计")
print(f"{'='*130}")
for name, rdf in results.items():
    unfilled = rdf[~rdf['filled']]
    if len(unfilled) > 0:
        cancel_counts = unfilled['cancel'].value_counts()
        print(f"\n  {name}:")
        for reason, cnt in cancel_counts.items():
            print(f"    {reason}: {cnt}")

# 年度分解
print(f"\n{'='*130}")
print("年度分解（仅成交单）")
print(f"{'='*130}")
print(f"\n{'年份':>6}", end="")
for name in results:
    print(f"  {name:>20}", end="")
print()
print("-" * 90)
all_years = sorted(set().union(*[set(results[name]['year'].unique()) for name in results]))
for yr in all_years:
    print(f"{yr:>6}", end="")
    for name in results:
        rdf = results[name]
        filled = rdf[rdf['filled']]
        val = filled[filled['year']==yr]['r'].sum() if yr in filled['year'].values else 0
        print(f"  {val:>20.1f}", end="")
    print()
print(f"{'合计':>6}", end="")
for name in results:
    filled = results[name][results[name]['filled']]
    print(f"  {filled['r'].sum():>20.1f}", end="")
print()

# 回调基准统计
print(f"\n{'='*130}")
print("回调基准统计")
print(f"{'='*130}")
cb_found = 0; cb_total = 0; cb_offset = []
for s in signals:
    cb_total += 1
    cb_extreme, cb_idx = find_callback_extreme(s.idx, s.direction, cfg.window)
    if cb_extreme is not None:
        cb_found += 1
        bt = s.breakthrough
        if s.direction == "long":
            cb_offset.append((bt - cb_extreme) / bt * 100)
        else:
            cb_offset.append((cb_extreme - bt) / bt * 100)
print(f"\n  有回调基准: {cb_found}/{cb_total} ({cb_found/cb_total:.0%})")
if cb_offset:
    cb_offset = np.array(cb_offset)
    print(f"  回调基准偏移: 均值={cb_offset.mean():.3f}%, 中位={np.median(cb_offset):.3f}%")

# 与之前最优对比
print(f"\n{'='*130}")
print("与之前最优对比")
print(f"{'='*130}")
print(f"\n  之前最优（回调基准+0.5ATR确认+0.3ATR止损+1.5ATR加仓）: 1603.9R")
for name, rdf in results.items():
    filled = rdf[rdf['filled']]
    vs = filled['r'].sum() - 1603.9
    print(f"  {name}: {filled['r'].sum():.1f}R (vs之前最优 {vs:+.1f}R)")
