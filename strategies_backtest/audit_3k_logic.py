"""检查3K收盘认赔 vs 前3K止损的逻辑冲突。

当前逻辑：
- 前3K止损：bh<=3时用init_stop（成交价-1ATR），触发出场
- 3K收盘认赔：bh==3时，如果收盘价穿成本价，按收盘价出场

问题：如果第3K的低价触发了init_stop，会在前3K止损处break，不会走到3K收盘认赔。
3K收盘认赔只在"第3K没有触发init_stop，但收盘价穿了成本价"的情况下触发。

这个逻辑是否合理？是否有冲突？
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
cache = cfg.base_dir / "cache"
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3
CONFIRM_ATR = 0.5; ADDON_ATR = 1.5; ADDON_BODY_PCT = 0.03; ATR_LEN = 20

df_4h = pd.read_parquet(cache / "BTCUSDTUSDT_4h_mark.parquet")
for col in ["open","high","low","close"]:
    if col in df_4h.columns: df_4h[col] = df_4h[col].astype(float)
if "open_time" not in df_4h.columns:
    df_4h["open_time"] = pd.to_datetime(df_4h["open_time_ms"], unit="ms", utc=True)

kama_vals = kama(df_4h["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy(); n4h = len(df_4h)

def calc_atr(h, l, c, length=ATR_LEN):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, ATR_LEN)

def find_callback_extreme(i, direction, window):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n4h: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]: return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] <= lows[j]: return lows[j], j
    return None, None

def simulate(signal):
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_nk = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, cb_idx = find_callback_extreme(i, d, WINDOW)
    entry_base = cb_extreme if cb_extreme is not None else bt_nk
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    fill_price = float(entry_base) if filled else 0.0
    if not filled: return None

    confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
    if not confirmed: return None

    if is_long: init_stop = fill_price - STOP_MULT*av
    else: init_stop = fill_price + STOP_MULT*av

    # 追踪第3K的情况
    for j in range(i+1, min(i+5, n4h)):
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(kama_vals[j-1], fill_price) if is_long else min(kama_vals[j-1], fill_price)

        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        close_breached = (bh == 3) and ((is_long and closes[j] < fill_price) or (not is_long and closes[j] > fill_price))

        print(f"  第{bh}K: open={opens[j]:.2f} high={highs[j]:.2f} low={lows[j]:.2f} close={closes[j]:.2f} sl={sl:.2f} fill={fill_price:.2f} 止损={'是' if hit_stop else '否'} 收盘穿={'是' if close_breached else '否'}")

        if hit_stop:
            return f"前3K止损(bh={bh})"
        if bh == 3 and close_breached:
            return f"3K收盘认赔"
    return "继续持仓"

signals = detect_breakout(df_4h, WINDOW)
print("=" * 100)
print("检查3K内出场情况")
print("=" * 100)

count_stop = 0; count_3k_close = 0; count_continue = 0
for s in signals:
    r = simulate(s)
    if r is None: continue
    if "前3K止损" in r: count_stop += 1
    elif "3K收盘认赔" in r: count_3k_close += 1
    elif "继续" in r: count_continue += 1

print(f"\n统计:")
print(f"  前3K止损: {count_stop}单")
print(f"  3K收盘认赔: {count_3k_close}单")
print(f"  继续持仓: {count_continue}单")

# 详细检查几个3K收盘认赔的例子
print(f"\n前5个3K收盘认赔的例子:")
found = 0
for s in signals:
    if found >= 5: break
    r = simulate(s)
    if r and "3K收盘认赔" in r:
        print(f"\n信号K idx={s.idx}, time={df_4h['open_time'].iloc[s.idx]}, direction={s.direction}")
        simulate(s)
        found += 1
