"""对比新旧逻辑同一信号的差异。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout
from entry_atr_offset import simulate, find_callback_extreme, atr_vals, n, dates

cfg = BacktestConfig()
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
closes = df["close"].to_numpy(); opens = df["open"].to_numpy()

# 新逻辑
new_results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s, use_callback=True, addon=True)
    if r:
        new_results.append((s, r))
        if r["filled"]: last_exit = r["exit_idx"]

# 旧逻辑（入场=回调基准, 确认0.5ATR, 弱突破平仓, 紧止损0.3ATR）
def simulate_old(signal):
    i = signal.idx
    if i + 1 >= n: return None
    bt_6k = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
    base = cb_extreme if cb_extreme is not None else bt_6k
    confirm_atr = 0.5; stop_mult = 0.3
    if is_long:
        confirmed = closes[i] >= base + confirm_atr * av
    else:
        confirmed = closes[i] <= base - confirm_atr * av
    entry_price = base
    if is_long:
        init_stop = entry_price - stop_mult * av
        tp_price = base * 1.22
    else:
        init_stop = entry_price + stop_mult * av
        tp_price = base * (1 - 0.12)
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
        return {"r": r1, "exit_idx": i, "hold": 0, "confirmed": False}
    addon_done = False; addon_price = 0.0
    for j in range(i + 1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(ks, base) if is_long else min(ks, base)
        if not addon_done:
            if is_long: cg = closes[j] - base
            else: cg = base - closes[j]
            if cg >= 1.5 * av:
                addon_done = True; addon_price = float(closes[j])
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            r1 = (ep-entry_price)/av; r2 = ((ep-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "confirmed": True}
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r1 = (entry_price-ep)/av; r2 = ((addon_price-ep)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "confirmed": True}
        if is_long and highs[j] >= tp_price:
            r1 = (tp_price-entry_price)/av; r2 = ((tp_price-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "confirmed": True}
        if not is_long and lows[j] <= tp_price:
            r1 = (entry_price-tp_price)/av; r2 = ((addon_price-tp_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "confirmed": True}
    ep = float(closes[-1])
    r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
    r2 = ((ep-addon_price)/av if is_long else (addon_price-ep)/av) if addon_done else 0.0
    return {"r": r1+r2, "exit_idx": n-1, "hold": n-1-i, "confirmed": True}

old_results = []; last_exit_old = -1
for s in signals:
    if s.idx <= last_exit_old: continue
    r = simulate_old(s)
    if r:
        old_results.append((s, r))
        last_exit_old = r["exit_idx"]

print("=" * 130)
print("新旧逻辑对比（同一信号序列，回调基准+加仓）")
print("=" * 130)
print(f"\n旧逻辑: 入场=回调基准, 确认0.5ATR弱突破平仓, 止损0.3ATR, 加仓1.5ATR, 止盈22%/12%")
print(f"新逻辑: 入场=回调基准±1ATR挂单, 止损=回调基准, 加仓=基准±1.5ATR, 止盈22%/12%")
print(f"\n旧逻辑: {len(old_results)}笔, 累计R={sum(r['r'] for _,r in old_results):.1f}")
new_filled = [(s,r) for s,r in new_results if r['filled']]
print(f"新逻辑: {len(new_results)}笔(成交{len(new_filled)}, 未成交{len(new_results)-len(new_filled)}), 累计R={sum(r['r'] for s,r in new_filled):.1f}")

# 逐年对比
print(f"\n{'='*130}")
print("逐年对比")
print(f"{'='*130}")
print(f"\n{'年份':>6}  {'旧逻辑单数':>8} {'旧逻辑R':>10}  {'新逻辑成交':>8} {'新逻辑未成交':>8} {'新逻辑R':>10}  {'差异':>10}")
print("-" * 75)

old_by_year = {}
for s, r in old_results:
    yr = int(dates[s.idx])
    old_by_year.setdefault(yr, []).append(r['r'])

new_by_year = {}; new_unfilled_by_year = {}
for s, r in new_results:
    yr = int(dates[s.idx])
    if r['filled']:
        new_by_year.setdefault(yr, []).append(r['r'])
    else:
        new_unfilled_by_year.setdefault(yr, []).append(1)

all_years = sorted(set(list(old_by_year.keys()) + list(new_by_year.keys()) + list(new_unfilled_by_year.keys())))
for yr in all_years:
    old_r = sum(old_by_year.get(yr, []))
    new_r = sum(new_by_year.get(yr, []))
    old_cnt = len(old_by_year.get(yr, []))
    new_cnt = len(new_by_year.get(yr, []))
    new_unfilled = len(new_unfilled_by_year.get(yr, []))
    print(f"{yr:>6}  {old_cnt:>8} {old_r:>10.1f}  {new_cnt:>8} {new_unfilled:>8} {new_r:>10.1f}  {new_r-old_r:>10.1f}")

print(f"{'合计':>6}  {len(old_results):>8} {sum(r['r'] for _,r in old_results):>10.1f}  "
      f"{len(new_filled):>8} {len(new_results)-len(new_filled):>8} {sum(r['r'] for s,r in new_filled):>10.1f}  "
      f"{sum(r['r'] for s,r in new_filled)-sum(r['r'] for _,r in old_results):>10.1f}")

# 前30笔逐笔对比
print(f"\n{'='*130}")
print("前30笔逐笔对比")
print(f"{'='*130}")
print(f"\n{'序号':>4} {'信号时间':>16} {'方向':>4} {'基准':>10} {'ATR':>8} "
      f"{'旧入场':>10} {'旧R':>8} {'旧持仓':>6}  "
      f"{'新挂单':>10} {'新成交':>6} {'新入场':>10} {'新R':>8} {'新持仓':>6}  {'差异':>8}")
print("-" * 120)

for idx in range(min(30, len(new_results))):
    s_new, r_new = new_results[idx]
    sig_time = df['open_time'].iloc[s_new.idx] + pd.Timedelta(hours=8)
    av = atr_vals[s_new.idx - 1]
    base = r_new["base"]
    d = "多" if r_new['dir'] == 'long' else "空"

    if idx < len(old_results):
        s_old, r_old = old_results[idx]
        old_entry = base  # 旧逻辑入场=基准
        old_r = r_old['r']
        old_hold = r_old['hold']
    else:
        old_entry = base; old_r = 0; old_hold = 0

    if r_new['filled']:
        new_fill = "是"
        new_entry = r_new['fill_price']
        new_r = r_new['r']
        new_hold = r_new['hold']
    else:
        new_fill = "否"
        new_entry = 0; new_r = 0; new_hold = 0

    print(f"{idx+1:>4} {sig_time.strftime('%Y/%m/%d %H:%M'):>16} {d:>4} {base:>10.1f} {av:>8.1f} "
          f"{old_entry:>10.1f} {old_r:>8.2f} {old_hold:>6}  "
          f"{r_new['order_price']:>10.1f} {new_fill:>6} {new_entry:>10.1f} {new_r:>8.2f} {new_hold:>6}  {new_r-old_r:>8.2f}")
