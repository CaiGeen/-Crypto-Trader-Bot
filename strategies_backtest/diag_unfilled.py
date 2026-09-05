"""诊断未成交单：数量、占用last_exit情况、不同处理方式对比。"""
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

# 逐信号检查filled/confirmed状态
print("=" * 100)
print("未成交单诊断")
print("=" * 100)

records = []
for s in signals:
    i = s.idx
    if i + 1 >= n4h: continue
    bt_6k = s.breakthrough; d = s.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: continue
    cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
    has_cb = cb_extreme is not None
    entry_base = cb_extreme if has_cb else bt_6k
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)

    records.append({
        "sig_idx": i, "dir": d, "entry_base": entry_base, "bt_6k": bt_6k,
        "filled": filled, "confirmed": confirmed,
        "open": opens[i], "high": highs[i], "low": lows[i], "close": closes[i],
        "atr": av, "has_cb": has_cb
    })

rdf = pd.DataFrame(records)

total = len(rdf)
filled_df = rdf[rdf["filled"]]
unfilled_df = rdf[~rdf["filled"]]
unfilled_confirmed = rdf[(~rdf["filled"]) & rdf["confirmed"]]
unfilled_unconfirmed = rdf[(~rdf["filled"]) & (~rdf["confirmed"])]

print(f"\n总信号数: {total}")
print(f"  成交(filled=True): {len(filled_df)} ({len(filled_df)/total:.1%})")
print(f"  未成交(filled=False): {len(unfilled_df)} ({len(unfilled_df)/total:.1%})")
print(f"    未成交+确认(gap跳空): {len(unfilled_confirmed)}")
print(f"    未成交+未确认(弱突破): {len(unfilled_unconfirmed)}")

# 详细看未成交单
if len(unfilled_df) > 0:
    print(f"\n{'━'*100}")
    print("未成交单明细")
    print(f"{'━'*100}")
    print(f"{'idx':>6} {'方向':>6} {'entry_base':>12} {'open':>10} {'high':>10} {'low':>10} {'close':>10} {'filled':>7} {'confirmed':>9} {'gap':>8}")
    print("-" * 100)
    for _, r in unfilled_df.iterrows():
        gap = "跳空高开" if r["dir"]=="long" and r["open"] > r["entry_base"] else \
              "跳空低开" if r["dir"]=="short" and r["open"] < r["entry_base"] else ""
        print(f"{r['sig_idx']:>6} {r['dir']:>6} {r['entry_base']:>12.2f} {r['open']:>10.2f} {r['high']:>10.2f} "
              f"{r['low']:>10.2f} {r['close']:>10.2f} {str(r['filled']):>7} {str(r['confirmed']):>9} {gap:>8}")

# 模拟当前逻辑的last_exit占用
print(f"\n{'━'*100}")
print("当前逻辑：未成交单仍占用last_exit，阻塞后续信号")
print(f"{'━'*100}")

# 方案A：当前逻辑（未成交也占用last_exit）
results_A = []; last_exit_A = -1; blocked_by_unfilled = 0
for s in signals:
    if s.idx <= last_exit_A:
        blocked_by_unfilled += 1
        continue
    i = s.idx
    if i + 1 >= n4h: continue
    bt_6k = s.breakthrough; d = s.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: continue
    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    entry_base = cb_extreme if cb_extreme is not None else bt_6k
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)

    if not confirmed:
        exit_idx = i
    else:
        # 简化：只算到止损/止盈/未平仓，不算加仓和1min消歧
        if is_long:
            init_stop = entry_base - STOP_MULT*av; tp_price = entry_base*(1+TP_LONG)
        else:
            init_stop = entry_base + STOP_MULT*av; tp_price = entry_base*(1-TP_SHORT)
        exit_idx = n4h - 1
        for j in range(i+1, n4h):
            ks = kama_vals[j-1]
            if np.isnan(ks): continue
            bh = j - i
            sl = init_stop if bh <= 3 else (max(ks, entry_base) if is_long else min(ks, entry_base))
            hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
            hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)
            if hit_stop or hit_tp:
                exit_idx = j; break
    results_A.append({"sig_idx": i, "filled": filled, "confirmed": confirmed, "exit_idx": exit_idx})
    last_exit_A = exit_idx

rdf_A = pd.DataFrame(results_A)
filled_A = rdf_A[rdf_A["filled"]]
unfilled_A = rdf_A[~rdf_A["filled"]]

print(f"  总波段: {len(rdf_A)}  成交: {len(filled_A)}  未成交: {len(unfilled_A)}")
print(f"  被last_exit阻塞的信号数: {blocked_by_unfilled}")

# 方案B：未成交单不占用last_exit（跳过，不阻塞）
results_B = []; last_exit_B = -1; skipped_unfilled = 0
for s in signals:
    if s.idx <= last_exit_B: continue
    i = s.idx
    if i + 1 >= n4h: continue
    bt_6k = s.breakthrough; d = s.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: continue
    cb_extreme, _ = find_callback_extreme(i, d, cfg.window)
    entry_base = cb_extreme if cb_extreme is not None else bt_6k
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)

    if not confirmed:
        exit_idx = i
    else:
        if is_long:
            init_stop = entry_base - STOP_MULT*av; tp_price = entry_base*(1+TP_LONG)
        else:
            init_stop = entry_base + STOP_MULT*av; tp_price = entry_base*(1-TP_SHORT)
        exit_idx = n4h - 1
        for j in range(i+1, n4h):
            ks = kama_vals[j-1]
            if np.isnan(ks): continue
            bh = j - i
            sl = init_stop if bh <= 3 else (max(ks, entry_base) if is_long else min(ks, entry_base))
            hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
            hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)
            if hit_stop or hit_tp:
                exit_idx = j; break

    if not filled:
        skipped_unfilled += 1
        continue  # 不加入results，不更新last_exit

    results_B.append({"sig_idx": i, "filled": filled, "confirmed": confirmed, "exit_idx": exit_idx})
    last_exit_B = exit_idx

rdf_B = pd.DataFrame(results_B)

print(f"\n{'━'*100}")
print("方案B：未成交单跳过，不占用last_exit")
print(f"{'━'*100}")
print(f"  总波段: {len(rdf_B)}  (跳过未成交: {skipped_unfilled})")
print(f"  比方案A多: {len(rdf_B) - len(filled_A)} 单")

# 找出方案B比方案A多出来的单
sigs_A = set(rdf_A[rdf_A["filled"]]["sig_idx"].tolist())
sigs_B = set(rdf_B["sig_idx"].tolist())
new_sigs = sigs_B - sigs_A
print(f"  新增成交信号idx: {sorted(new_sigs)[:20]}{'...' if len(new_sigs)>20 else ''}")
