"""检查关键参数的不连续性和结论可靠性。

1. 3K收盘认赔 vs 前3K止损的冲突
2. 加仓许可vs触发阈值的不连续性
3. 止盈不对称性(22% vs 12%)
4. ATR%<2.0分档阈值的依据
5. 回撤自适应阈值3R的优化
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
CONFIRM_ATR = 0.5; ADDON_ATR = 1.5; ADDON_BODY_PCT = 0.03; ATR_LEN = 20; EMA_LEN = 70
DD_THRESHOLD = 3.0

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

print("=" * 100)
print("一、3K收盘认赔 vs 前3K止损 冲突分析")
print("=" * 100)

def find_callback_extreme(i, direction, window):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n4h: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]: return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] <= lows[j]: return lows[j], j
    return None, None

signals = detect_breakout(df_4h, WINDOW)
count_stop = 0; count_3k = 0; count_both = 0
both_examples = []

for s in signals:
    i = s.idx; d = s.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: continue
    cb_extreme, _ = find_callback_extreme(i, d, WINDOW)
    entry_base = cb_extreme if cb_extreme is not None else s.breakthrough
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    if not filled: continue
    confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
    if not confirmed: continue
    fill_price = entry_base
    init_stop = fill_price - STOP_MULT*av if is_long else fill_price + STOP_MULT*av
    
    for j in range(i+1, min(i+5, n4h)):
        bh = j - i
        if bh > 3: continue
        hit_stop = (lows[j] < init_stop) if is_long else (highs[j] > init_stop)
        close_breached = (bh == 3) and ((is_long and closes[j] < fill_price) or (not is_long and closes[j] > fill_price))
        if hit_stop and bh == 3 and close_breached:
            count_both += 1
            if len(both_examples) < 3:
                both_examples.append((i, j, fill_price, init_stop, closes[j], is_long))
            break
        elif hit_stop:
            count_stop += 1
            break
        elif bh == 3 and close_breached:
            count_3k += 1
            break

print(f"前3K止损: {count_stop}")
print(f"3K收盘认赔: {count_3k}")
print(f"两者同时触发: {count_both}")
print(f"\n问题: {count_both}单同时触发两种出场，但代码只执行前3K止损")
print(f"如果第3K收盘价在init_stop和成本价之间，按前3K止损出场更差")

print(f"\n示例:")
for ex in both_examples:
    i, j, fp, sl, cp, is_long = ex
    print(f"  信号K idx={i}: fill={fp:.2f}, init_stop={sl:.2f}, 第3K收盘={cp:.2f}")
    print(f"    前3K止损R={(sl-fp)/atr_vals[i-1] if is_long else (fp-sl)/atr_vals[i-1]:.2f}")
    print(f"    3K收盘认赔R={(cp-fp)/atr_vals[i-1] if is_long else (fp-cp)/atr_vals[i-1]:.2f}")

print("\n" + "=" * 100)
print("二、加仓许可vs触发阈值的不连续性")
print("=" * 100)
print(f"许可条件: 突破K实体涨跌幅 >= {ADDON_BODY_PCT:.0%}")
print(f"触发条件: 浮盈 >= {ADDON_ATR} x ATR")
print(f"问题: 两个阈值单位不同（百分比 vs ATR倍数），无法直接比较")
print(f"许可看的是信号K特征，触发看的是入场后累计浮盈，逻辑不连贯")

print("\n" + "=" * 100)
print("三、止盈不对称性(22% vs 12%)")
print("=" * 100)
print(f"做多止盈: +{TP_LONG:.0%}")
print(f"做空止盈: -{TP_SHORT:.0%}")
print(f"问题: 不对称性没有明确的理论依据")

# 测试对称止盈
print("\n测试对称止盈效果:")

def simulate_symmetric_tp(signal, tp_long=0.22, tp_short=0.12):
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_nk = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, _ = find_callback_extreme(i, d, WINDOW)
    entry_base = cb_extreme if cb_extreme is not None else bt_nk
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    fill_price = float(entry_base) if filled else 0.0
    if not filled: return None
    confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
    if not confirmed: return None
    init_stop = fill_price - STOP_MULT*av if is_long else fill_price + STOP_MULT*av
    tp_price = fill_price*(1+tp_long) if is_long else fill_price*(1-tp_short)
    
    for j in range(i+1, n4h):
        bh = j - i
        if bh <= 3 or np.isnan(kama_vals[j-1]):
            sl = init_stop
        else:
            sl = max(kama_vals[j-1], fill_price) if is_long else min(kama_vals[j-1], fill_price)
        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)
        if hit_stop or hit_tp:
            ep = float(sl) if hit_stop else float(tp_price)
            r = (ep-fill_price)/av if is_long else (fill_price-ep)/av
            return r
    r = (closes[-1]-fill_price)/av if is_long else (fill_price-closes[-1])/av
    return r

# 测试不同止盈组合
tp_combos = [(0.22, 0.12), (0.17, 0.17), (0.20, 0.20), (0.15, 0.15)]
for tp_l, tp_s in tp_combos:
    total_r = 0
    for s in signals:
        r = simulate_symmetric_tp(s, tp_l, tp_s)
        if r is not None: total_r += r
    print(f"  做多+{tp_l:.0%}/做空-{tp_s:.0%}: 总R={total_r:.1f}")

print("\n" + "=" * 100)
print("四、ATR%<2.0分档阈值依据检查")
print("=" * 100)
print("当前: ATR%<2.0 → 2x(做多) / 1.5x(做空)")
print("问题: 2.0这个阈值是如何确定的？是否经过优化？")

# 收集ATR%分布
atr_pct_vals = []
for s in signals:
    i = s.idx; d = s.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: continue
    cb_extreme, _ = find_callback_extreme(i, d, WINDOW)
    entry_base = cb_extreme if cb_extreme is not None else s.breakthrough
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    if not filled: continue
    confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
    if not confirmed: continue
    atr_pct = av/closes[i-1]*100
    atr_pct_vals.append(atr_pct)

atr_pct_vals = np.array(atr_pct_vals)
print(f"\nATR%分布:")
print(f"  中位数: {np.median(atr_pct_vals):.2f}%")
print(f"  均值: {np.mean(atr_pct_vals):.2f}%")
print(f"  25%分位: {np.percentile(atr_pct_vals, 25):.2f}%")
print(f"  75%分位: {np.percentile(atr_pct_vals, 75):.2f}%")
print(f"  <2.0的比例: {(atr_pct_vals < 2.0).mean():.1%}")
print(f"  <1.5的比例: {(atr_pct_vals < 1.5).mean():.1%}")
print(f"  <2.5的比例: {(atr_pct_vals < 2.5).mean():.1%}")

print("\n" + "=" * 100)
print("五、回撤自适应阈值3R的优化检查")
print("=" * 100)
print(f"当前: 回撤 > {DD_THRESHOLD:.0f}R 时降0.5x")
print("问题: 3R这个阈值是否经过优化？")

# 测试不同阈值
print("\n测试不同回撤阈值:")
for dd_thresh in [2.0, 3.0, 4.0, 5.0]:
    total_weighted_r = 0; max_dd = 0; cum_r = 0; max_cum = 0
    for s in signals:
        i = s.idx; d = s.direction; is_long = (d == "long")
        av = atr_vals[i-1] if i-1 >= 0 else np.nan
        if np.isnan(av) or av <= 0: continue
        cb_extreme, _ = find_callback_extreme(i, d, WINDOW)
        entry_base = cb_extreme if cb_extreme is not None else s.breakthrough
        filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
        if not filled: continue
        confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
        if not confirmed: continue
        
        # 简化计算
        tier = 2.0 if (is_long and av/closes[i-1]*100 < 2.0) else (1.5 if (not is_long and av/closes[i-1]*100 < 2.0) else 1.0)
        if max_dd > dd_thresh: tier = max(1.0, tier - 0.5)
        
        # 简化R计算
        if is_long:
            r = (min(highs[i+1:i+10].max() if i+10 < n4h else closes[-1], entry_base*(1+TP_LONG)) - entry_base) / av
        else:
            r = (entry_base - max(lows[i+1:i+10].min() if i+10 < n4h else closes[-1], entry_base*(1-TP_SHORT))) / av
        
        total_weighted_r += r * tier
        cum_r += r * tier; max_cum = max(max_cum, cum_r); max_dd = max(max_dd, max_cum - cum_r)
    
    print(f"  阈值{dd_thresh:.0f}R: 加权R={total_weighted_r:.1f}, 最大回撤={max_dd:.1f}R")
