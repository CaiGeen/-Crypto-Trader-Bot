"""假突破优化组合测试：0.5%确认阈值 + 紧止损"""
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

def simulate(signal, init_stop_mult=1.0, confirm_thresh=0.0,
             tp_l=0.18, tp_s=0.12, addon=False, addon_thresh=0.023):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    if confirm_thresh > 0:
        cg = (closes[i]-bt)/bt if is_long else (bt-closes[i])/bt
        if cg < confirm_thresh:
            return None

    entry_idx = i; entry_price = bt
    if is_long: init_stop = entry_price - init_stop_mult * av; tp_price = bt * (1 + tp_l)
    else: init_stop = entry_price + init_stop_mult * av; tp_price = bt * (1 - tp_s)

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
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d, "pre3k": bh<=3}
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r1 = (entry_price-ep)/av; r2 = ((addon_price-ep)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d, "pre3k": bh<=3}
        if is_long and highs[j] >= tp_price:
            r1 = (tp_price-entry_price)/av; r2 = ((tp_price-addon_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d, "pre3k": bh<=3}
        if not is_long and lows[j] <= tp_price:
            r1 = (entry_price-tp_price)/av; r2 = ((addon_price-tp_price)/av if addon_done else 0.0)
            return {"r": r1+r2, "exit_idx": j, "hold": bh, "year": int(dates[j]), "dir": d, "pre3k": bh<=3}
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    ep = float(closes[-1])
    r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
    r2 = ((ep-addon_price)/av if is_long else (addon_price-ep)/av) if addon_done else 0.0
    return {"r": r1+r2, "exit_idx": n-1, "hold": n-1-entry_idx, "year": int(dates[n-1]), "dir": d, "pre3k": False}

def run(**kwargs):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, **kwargs)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

base = run(addon=True)

print("=" * 100)
print("组合优化：确认阈值 × 紧止损（全部带加仓）")
print("=" * 100)
print(f"\n基准+加仓: {base['r'].sum():.1f}R, {len(base)}单, 均R={base['r'].mean():.2f}, 胜率={(base['r']>0).mean():.0%}")
print(f"\n{'确认':>6} {'止损':>6} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6} {'单数':>6} {'前3K单':>6} {'前3K亏损':>10} {'跳过':>6}")
print("-" * 75)

best = None
for thresh in [0.0, 0.003, 0.005, 0.008, 0.01]:
    for mult in [1.0, 0.75, 0.5, 0.3]:
        rdf = run(confirm_thresh=thresh, init_stop_mult=mult, addon=True)
        pre = rdf[rdf['pre3k']]
        vs = rdf["r"].sum() - base["r"].sum()
        skipped = len(base) - len(rdf)
        print(f"{thresh:>5.1%} {mult:>5.2f}x {rdf['r'].sum():>8.1f} {vs:>+8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(rdf):>6} {len(pre):>6} {pre['r'].sum():>10.1f} {skipped:>6}")
        if best is None or rdf['r'].sum() > best[0]:
            best = (rdf['r'].sum(), thresh, mult, len(rdf), rdf['r'].mean(), (rdf['r']>0).mean())

print(f"\n最优组合: 确认={best[1]:.1%}, 止损={best[2]:.2f}x → {best[0]:.1f}R ({best[0]-base['r'].sum():+.1f}R), {best[3]}单, 均R={best[4]:.2f}, 胜率={best[5]:.0%}")

# 最优组合的年度分解
print(f"\n{'='*100}")
print(f"最优组合年度分解 vs 基准")
print(f"{'='*100}")
thresh_opt, mult_opt = best[1], best[2]
rdf_opt = run(confirm_thresh=thresh_opt, init_stop_mult=mult_opt, addon=True)
print(f"\n{'年份':>6}  {'基准':>10}  {'优化':>10}  {'差值':>8}  {'基准单数':>6}  {'优化单数':>6}")
print("-" * 55)
for yr in sorted(base['year'].unique()):
    b = base[base['year']==yr]['r'].sum()
    o = rdf_opt[rdf_opt['year']==yr]['r'].sum()
    bn = len(base[base['year']==yr])
    on = len(rdf_opt[rdf_opt['year']==yr])
    print(f"{yr:>6}  {b:>10.1f}  {o:>10.1f}  {o-b:>+8.1f}  {bn:>6}  {on:>6}")
print(f"{'合计':>6}  {base['r'].sum():>10.1f}  {rdf_opt['r'].sum():>10.1f}  {rdf_opt['r'].sum()-base['r'].sum():>+8.1f}  {len(base):>6}  {len(rdf_opt):>6}")

# 确认阈值0.5%单独 vs 组合的跳过单分析
print(f"\n{'='*100}")
print(f"0.5%确认阈值跳过的404单分析：这些单在基准中是赚还是亏？")
print(f"{'='*100}")
# 找出被0.5%跳过的信号
base_idxs = set(); last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s, addon=True)
    if r: base_idxs.add(s.idx); last_exit = r["exit_idx"]

skip_idxs = set(); last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s, confirm_thresh=0.005, addon=True)
    if r: skip_idxs.add(s.idx); last_exit = r["exit_idx"]
    else:
        # 检查是否因为确认阈值被跳过
        bt = s.breakthrough; is_long = (s.direction == "long")
        cg = (closes[s.idx]-bt)/bt if is_long else (bt-closes[s.idx])/bt
        if cg < 0.005:
            skip_idxs.add(-s.idx)  # 标记为跳过

skipped_signals = base_idxs - skip_idxs
# 重新模拟这些被跳过的信号在基准中的R值
skip_r = []; skip_pre3k = 0; skip_post3k = 0
for s in signals:
    if s.idx not in skipped_signals: continue
    r = simulate(s, addon=True)
    if r:
        skip_r.append(r['r'])
        if r['pre3k']: skip_pre3k += 1
        else: skip_post3k += 1

if skip_r:
    skip_r = np.array(skip_r)
    print(f"\n  被跳过单数: {len(skip_r)}")
    print(f"  累计R: {skip_r.sum():.1f} (这些单在基准中贡献的R)")
    print(f"  均R: {skip_r.mean():.2f}")
    print(f"  胜率: {(skip_r>0).mean():.0%}")
    print(f"  前3K止损: {skip_pre3k}单, 第3K后: {skip_post3k}单")
    print(f"  R分布: <−1R: {(skip_r<-1).sum()}, −1~0R: {((skip_r>=-1)&(skip_r<0)).sum()}, 0~1R: {((skip_r>=0)&(skip_r<1)).sum()}, 1~3R: {((skip_r>=1)&(skip_r<3)).sum()}, >3R: {(skip_r>=3).sum()}")
    print(f"\n  → 跳过这些单净省: {-skip_r.sum():.1f}R")
