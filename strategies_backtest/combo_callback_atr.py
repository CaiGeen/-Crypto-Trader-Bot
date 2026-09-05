"""综合对比：突破线(6K极值 vs 回调高点) × 加仓(无 vs 1.5ATR)
确认阈值0.5ATR弱突破平仓 + 0.3ATR止损 + 固定ATR
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
    """6K箱体内从右往左找最新的2K不创新高/低回调的极值点。"""
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
             confirm_atr=0.5, stop_mult=0.3, addon_atr=1.5,
             tp_l=0.22, tp_s=0.12):
    """
    use_callback: True=回调高点为基准, False=6K极值为基准
    addon: True=加仓, False=不加仓
    confirm_atr: 确认阈值ATR倍数（收盘价需站上突破基准+confirm_atr×ATR）
    stop_mult: 前3K止损ATR倍数
    addon_atr: 加仓阈值ATR倍数（不含突破当K）
    """
    i = signal.idx
    if i + 1 >= n: return None
    bt_6k = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    # 确定入场基准
    if use_callback:
        cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
        if cb_extreme is not None:
            entry_base = cb_extreme
        else:
            entry_base = bt_6k  # 找不到回调高点用6K极值
    else:
        entry_base = bt_6k

    # 确认阈值检查：收盘价是否站上entry_base + confirm_atr×ATR
    if is_long:
        confirmed = closes[i] >= entry_base + confirm_atr * av
    else:
        confirmed = closes[i] <= entry_base - confirm_atr * av

    entry_idx = i; entry_price = entry_base
    if is_long:
        init_stop = entry_price - stop_mult * av
        tp_price = entry_base * (1 + tp_l)
    else:
        init_stop = entry_price + stop_mult * av
        tp_price = entry_base * (1 - tp_s)

    # 弱突破：入场后在收盘价平仓
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
        return {"r": r1, "exit_idx": i, "hold": 0, "year": int(dates[i]),
                "dir": d, "pre3k": True, "confirmed": False, "weak": True}

    # 确认通过：正常持有
    peak = entry_price; addon_done = False; addon_price = 0.0
    for j in range(entry_idx + 1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - entry_idx
        if bh <= 3:
            sl = init_stop
        else:
            sl = max(ks, entry_base) if is_long else min(ks, entry_base)
        # 加仓：首次实体突破addon_atr×ATR（不含突破当K）
        if addon and not addon_done:
            if is_long:
                cg_abs = closes[j] - entry_base
            else:
                cg_abs = entry_base - closes[j]
            if cg_abs >= addon_atr * av:
                addon_done = True; addon_price = float(closes[j])
        # 止损
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            r1 = (ep-entry_price)/av
            r2 = ((ep-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "weak": False}
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r1 = (entry_price-ep)/av
            r2 = ((addon_price-ep)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "weak": False}
        # 止盈
        if is_long and highs[j] >= tp_price:
            r1 = (tp_price-entry_price)/av
            r2 = ((tp_price-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "weak": False}
        if not is_long and lows[j] <= tp_price:
            r1 = (entry_price-tp_price)/av
            r2 = ((addon_price-tp_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]),
                    "dir": d, "pre3k": bh<=3, "confirmed": True, "weak": False}
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    # 到最后未平仓
    ep = float(closes[-1])
    r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
    r2 = ((ep-addon_price)/av if is_long else (addon_price-ep)/av) if addon_done else 0.0
    return {"r": r1+r2, "exit_idx": n-1, "hold": n-1-entry_idx, "year": int(dates[n-1]),
            "dir": d, "pre3k": False, "confirmed": True, "weak": False}

def run(**kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

# 当前最优（0.20ATR确认+跳过+0.3止损+2.3%加仓）作为基准
print("=" * 115)
print("综合对比：突破线(6K极值 vs 回调高点) × 加仓(无 vs 1.5ATR)")
print("确认阈值0.5ATR弱突破平仓 | 0.3ATR止损 | 固定ATR | 止盈多22%/空12%")
print("=" * 115)

# 4个组合
combos = [
    ("6K极值·无加仓",   dict(use_callback=False, addon=False)),
    ("6K极值·1.5ATR加仓", dict(use_callback=False, addon=True)),
    ("回调高点·无加仓",  dict(use_callback=True,  addon=False)),
    ("回调高点·1.5ATR加仓", dict(use_callback=True,  addon=True)),
]

results = {}
for name, kwargs in combos:
    rdf = run(**kwargs)
    results[name] = rdf

# 汇总表
print(f"\n{'配置':>22} {'累计R':>8} {'单数':>6} {'均R':>6} {'胜率':>6} {'确认单':>6} {'弱突破单':>6} {'弱突破R':>8} {'前3K单':>6} {'前3K亏损':>10}")
print("-" * 95)
for name, rdf in results.items():
    conf = rdf[rdf['confirmed']]
    weak = rdf[rdf['weak']]
    pre3k = rdf[rdf['pre3k']]
    print(f"{name:>22} {rdf['r'].sum():>8.1f} {len(rdf):>6} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(conf):>6} {len(weak):>6} {weak['r'].sum():>8.1f} {len(pre3k):>6} {pre3k['r'].sum():>10.1f}")

# 年度分解
print(f"\n{'='*115}")
print("年度分解")
print(f"{'='*115}")
print(f"\n{'年份':>6}", end="")
for name in results:
    print(f"  {name:>20}", end="")
print()
print("-" * 90)
for yr in sorted(results[combos[0][0]]['year'].unique()):
    print(f"{yr:>6}", end="")
    for name in results:
        rdf = results[name]
        val = rdf[rdf['year']==yr]['r'].sum() if yr in rdf['year'].values else 0
        print(f"  {val:>20.1f}", end="")
    print()
print(f"{'合计':>6}", end="")
for name in results:
    print(f"  {results[name]['r'].sum():>20.1f}", end="")
print()

# 回调高点统计
print(f"\n{'='*115}")
print("回调高点统计")
print(f"{'='*115}")
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

print(f"\n  有回调高点: {cb_found}/{cb_total} ({cb_found/cb_total:.0%})")
if cb_offset:
    cb_offset = np.array(cb_offset)
    print(f"  回调高点偏移(6K极值-回调高点)/6K极值: 均值={cb_offset.mean():.3f}%, 中位={np.median(cb_offset):.3f}%")
    print(f"  → 回调高点比6K极值平均低{cb_offset.mean():.3f}%（做多）")

# 和之前最优对比
print(f"\n{'='*115}")
print("与之前最优对比")
print(f"{'='*115}")
print(f"\n  之前最优（0.20ATR确认跳过 + 0.3止损 + 2.3%加仓）: 1385.1R")
for name, rdf in results.items():
    vs = rdf['r'].sum() - 1385.1
    print(f"  {name}: {rdf['r'].sum():.1f}R (vs之前最优 {vs:+.1f}R)")
