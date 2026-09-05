"""确认阈值：跳过不入场 vs 入场后平仓 对比"""
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

def simulate(signal, init_stop_mult=0.3, confirm_atr=0.20,
             mode="skip", tp_l=0.18, tp_s=0.12, addon=True, addon_thresh=0.023):
    """
    mode="skip": 不满足确认阈值 → 跳过不入场
    mode="closeout": 不满足确认阈值 → 入场后在突破K收盘价平仓
    mode="none": 无确认阈值（基准）
    """
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    cg_abs = (closes[i]-bt) if is_long else (bt-closes[i])
    confirmed = (cg_abs >= confirm_atr * av)

    if mode == "skip" and not confirmed:
        return None  # 跳过不入场

    entry_idx = i; entry_price = bt
    if is_long: init_stop = entry_price - init_stop_mult * av; tp_price = bt * (1 + tp_l)
    else: init_stop = entry_price + init_stop_mult * av; tp_price = bt * (1 - tp_s)

    # 入场后立即平仓模式：未确认则在收盘价平仓
    if mode == "closeout" and not confirmed:
        ep = float(closes[i])
        r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
        return {"r": r1, "exit_idx": i, "hold": 0, "year": int(dates[i]), "dir": d,
                "pre3k": True, "weak": True, "confirmed": False}

    peak = entry_price; addon_done = False; addon_price = 0.0
    for j in range(entry_idx + 1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - entry_idx
        if bh <= 3: sl = init_stop
        else: sl = max(ks, bt) if is_long else min(ks, bt)
        if addon and not addon_done:
            cg = (closes[j]-bt)/bt if is_long else (bt-closes[j])/bt
            if cg >= addon_thresh: addon_done = True; addon_price = float(closes[j])
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            r1 = (ep-entry_price)/av; r2 = ((ep-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d,
                    "pre3k": bh<=3, "weak": not confirmed, "confirmed": confirmed}
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r1 = (entry_price-ep)/av; r2 = ((addon_price-ep)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d,
                    "pre3k": bh<=3, "weak": not confirmed, "confirmed": confirmed}
        if is_long and highs[j] >= tp_price:
            r1 = (tp_price-entry_price)/av; r2 = ((tp_price-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d,
                    "pre3k": bh<=3, "weak": not confirmed, "confirmed": confirmed}
        if not is_long and lows[j] <= tp_price:
            r1 = (entry_price-tp_price)/av; r2 = ((addon_price-tp_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d,
                    "pre3k": bh<=3, "weak": not confirmed, "confirmed": confirmed}
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    ep = float(closes[-1])
    r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
    r2 = ((ep-addon_price)/av if is_long else (addon_price-ep)/av) if addon_done else 0.0
    return {"r": r1+r2, "exit_idx": n-1, "hold": n-1-entry_idx, "year": int(dates[n-1]), "dir": d,
            "pre3k": False, "weak": not confirmed, "confirmed": confirmed}

def run(**kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

# 三种模式
base = run(mode="none")
skip = run(mode="skip")
closeout = run(mode="closeout")

print("=" * 100)
print("三种模式对比（0.20ATR确认 + 0.3ATR止损 + 加仓）")
print("=" * 100)

for name, rdf in [("基准（无确认）", base), ("跳过不入场", skip), ("入场后平仓", closeout)]:
    weak = rdf[rdf.get('weak', False)] if 'weak' in rdf.columns else pd.DataFrame()
    conf = rdf[rdf.get('confirmed', True)] if 'confirmed' in rdf.columns else rdf
    print(f"\n{name}:")
    print(f"  累计R={rdf['r'].sum():.1f}, {len(rdf)}单, 均R={rdf['r'].mean():.2f}, 胜率={(rdf['r']>0).mean():.0%}")
    if 'weak' in rdf.columns:
        print(f"  已确认单: {len(conf)}单, R={conf['r'].sum():.1f}")
        print(f"  弱突破单: {len(weak)}单, R={weak['r'].sum():.1f}, 均R={weak['r'].mean():.3f}")

# 弱突破单详细分析
print(f"\n{'='*100}")
print("弱突破单（收盘价超过突破线但<0.20ATR）在两种模式下的表现")
print(f"{'='*100}")

weak_skip = skip[skip['weak']] if 'weak' in skip.columns else pd.DataFrame()
weak_close = closeout[closeout['weak']] if 'weak' in closeout.columns else pd.DataFrame()

print(f"\n  跳过模式: {len(weak_skip)}单被跳过（这些单在基准中的表现:）")
# 找出被跳过的信号在基准中的表现
skip_idxs = set(); last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s, mode="skip")
    if r: last_exit = r["exit_idx"]
    else:
        # 检查是否因为确认阈值被跳过
        bt = s.breakthrough; is_long = (s.direction == "long")
        av = atr_vals[s.idx-1]
        cg_abs = (closes[s.idx]-bt) if is_long else (bt-closes[s.idx])
        if cg_abs < 0.20 * av:
            skip_idxs.add(s.idx)

# 在基准中模拟这些被跳过的信号
base_skip_r = []
for s in signals:
    if s.idx not in skip_idxs: continue
    r = simulate(s, mode="none")
    if r: base_skip_r.append(r)

if base_skip_r:
    base_skip_df = pd.DataFrame(base_skip_r)
    print(f"  被跳过{len(base_skip_df)}单在基准中: R={base_skip_df['r'].sum():.1f}, 均R={base_skip_df['r'].mean():.2f}, 胜率={(base_skip_df['r']>0).mean():.0%}")
    pre3k = base_skip_df[base_skip_df['pre3k']]
    post3k = base_skip_df[~base_skip_df['pre3k']]
    print(f"    前3K止损: {len(pre3k)}单, R={pre3k['r'].sum():.1f}")
    print(f"    第3K后:   {len(post3k)}单, R={post3k['r'].sum():.1f}")

print(f"\n  入场后平仓模式: {len(weak_close)}单弱突破在收盘平仓")
if len(weak_close) > 0:
    print(f"    R={weak_close['r'].sum():.1f}, 均R={weak_close['r'].mean():.3f}, 胜率={(weak_close['r']>0).mean():.0%}")
    print(f"    R范围: [{weak_close['r'].min():.3f}, {weak_close['r'].max():.3f}]")

# 年度对比
print(f"\n{'='*100}")
print("年度分解")
print(f"{'='*100}")
print(f"\n{'年份':>6}  {'基准':>10}  {'跳过':>10}  {'入场后平仓':>10}  {'平仓-跳过':>10}")
print("-" * 55)
for yr in sorted(base['year'].unique()):
    b = base[base['year']==yr]['r'].sum()
    s = skip[skip['year']==yr]['r'].sum()
    c = closeout[closeout['year']==yr]['r'].sum()
    print(f"{yr:>6}  {b:>10.1f}  {s:>10.1f}  {c:>10.1f}  {c-s:>+10.1f}")
print(f"{'合计':>6}  {base['r'].sum():>10.1f}  {skip['r'].sum():>10.1f}  {closeout['r'].sum():>10.1f}  {closeout['r'].sum()-skip['r'].sum():>+10.1f}")

# 考虑手续费
print(f"\n{'='*100}")
print("考虑手续费后（每笔0.06%双边）")
print(f"{'='*100}")
fee = 0.0006  # 单边0.03%，双边0.06%
for name, rdf in [("跳过不入场", skip), ("入场后平仓", closeout)]:
    # 每笔扣2*fee*R换算... 实际上手续费对R的影响 = 2*fee*entry_price / ATR
    # 简化：用平均ATR%估算
    avg_atr_pct = 0.0172  # 1.72%
    fee_r = 2 * fee / avg_atr_pct  # 每笔手续费约0.07R
    total_r = rdf['r'].sum() - len(rdf) * fee_r
    print(f"  {name}: 原始{rdf['r'].sum():.1f}R - 手续费{len(rdf)*fee_r:.1f}R = {total_r:.1f}R")
