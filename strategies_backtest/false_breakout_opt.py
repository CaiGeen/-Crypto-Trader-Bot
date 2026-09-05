"""前3K假突破亏损分析与优化方案测试（实盘真实，开盘价出场）"""
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

def simulate(signal, init_stop_mult=1.0, entry_delay=0, confirm_thresh=0.0,
             tp_l=0.18, tp_s=0.12, addon=False, addon_thresh=0.023):
    """
    init_stop_mult: 初始止损乘数（1.0=1ATR, 0.5=0.5ATR）
    entry_delay: 入场延迟（0=突破K收盘入场, 1=下一根开盘入场）
    confirm_thresh: 确认阈值（收盘价超过突破线多少才入场，0=不确认）
    """
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    # 确认阈值：突破K收盘价需要超过突破线confirm_thresh才入场
    if confirm_thresh > 0:
        cg = (closes[i]-bt)/bt if is_long else (bt-closes[i])/bt
        if cg < confirm_thresh:
            return None  # 不满足确认条件，跳过信号

    # 入场时机
    if entry_delay == 0:
        entry_idx = i  # 突破K收盘入场
        entry_price = bt
    else:
        entry_idx = i + 1  # 下一根开盘入场
        if entry_idx >= n: return None
        entry_price = float(opens[entry_idx])

    if is_long: init_stop = entry_price - init_stop_mult * av; tp_price = bt * (1 + tp_l)
    else: init_stop = entry_price + init_stop_mult * av; tp_price = bt * (1 - tp_s)

    peak = entry_price
    addon_done = False; addon_price = 0.0
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

# 基准
base = run(addon=True)
base_noaddon = run()

print("=" * 110)
print("前3K假突破亏损分析")
print("=" * 110)

pre3k = base[base['pre3k']]
post3k = base[~base['pre3k']]
print(f"\n基准+加仓: {len(base)}单, 累计R={base['r'].sum():.1f}")
print(f"  前3K止损: {len(pre3k)}单 ({len(pre3k)/len(base)*100:.0f}%), 累计R={pre3k['r'].sum():.1f}, 均R={pre3k['r'].mean():.2f}")
print(f"  第3K后:   {len(post3k)}单 ({len(post3k)/len(base)*100:.0f}%), 累计R={post3k['r'].sum():.1f}, 均R={post3k['r'].mean():.2f}")

# 优化方案1：前3K更紧止损
print(f"\n{'='*110}")
print("方案1：前3K用更紧的止损（减少单笔亏损）")
print(f"{'='*110}")
print(f"\n{'止损乘数':>8} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6} {'单数':>6} {'前3K亏损':>10}")
print("-" * 60)
for mult in [1.0, 0.75, 0.5, 0.3]:
    rdf = run(init_stop_mult=mult, addon=True)
    pre = rdf[rdf['pre3k']]
    vs = rdf["r"].sum() - base["r"].sum()
    print(f"{mult:>7.2f}x {rdf['r'].sum():>8.1f} {vs:>+8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(rdf):>6} {pre['r'].sum():>10.1f}")

# 优化方案2：延迟入场（下一根开盘）
print(f"\n{'='*110}")
print("方案2：延迟入场（下一根开盘入场）")
print(f"{'='*110}")
for delay in [0, 1]:
    rdf = run(entry_delay=delay, addon=True)
    pre = rdf[rdf['pre3k']]
    vs = rdf["r"].sum() - base["r"].sum()
    print(f"  delay={delay}: 累计R={rdf['r'].sum():.1f}, vs基准={vs:+.1f}, 均R={rdf['r'].mean():.2f}, 胜率={(rdf['r']>0).mean():.0%}, 单数={len(rdf)}, 前3K亏损={pre['r'].sum():.1f}")

# 优化方案3：确认阈值（收盘价超过突破线一定幅度才入场）
print(f"\n{'='*110}")
print("方案3：确认阈值（收盘价超过突破线X%才入场）")
print(f"{'='*110}")
print(f"\n{'确认阈值':>8} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6} {'单数':>6} {'前3K亏损':>10} {'跳过':>6}")
print("-" * 65)
for thresh in [0.0, 0.005, 0.01, 0.015, 0.02, 0.03]:
    rdf = run(confirm_thresh=thresh, addon=True)
    pre = rdf[rdf['pre3k']]
    vs = rdf["r"].sum() - base["r"].sum()
    skipped = len(base) - len(rdf)
    print(f"{thresh:>7.1%} {rdf['r'].sum():>8.1f} {vs:>+8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(rdf):>6} {pre['r'].sum():>10.1f} {skipped:>6}")

# 优化方案4：组合（确认阈值+紧止损）
print(f"\n{'='*110}")
print("方案4：组合优化（确认阈值+紧止损）")
print(f"{'='*110}")
print(f"\n{'确认':>6} {'止损':>6} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6} {'单数':>6} {'前3K亏损':>10}")
print("-" * 65)
for thresh, mult in [(0.01, 0.75), (0.01, 0.5), (0.015, 0.5), (0.02, 0.5), (0.015, 0.75)]:
    rdf = run(confirm_thresh=thresh, init_stop_mult=mult, addon=True)
    pre = rdf[rdf['pre3k']]
    vs = rdf["r"].sum() - base["r"].sum()
    print(f"{thresh:>5.1%} {mult:>5.2f}x {rdf['r'].sum():>8.1f} {vs:>+8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(rdf):>6} {pre['r'].sum():>10.1f}")

# 前3K止损单的特征分析
print(f"\n{'='*110}")
print("前3K止损单的特征（能否事前识别？）")
print(f"{'='*110}")
pre3k_base = base[base['pre3k']]
post3k_base = base[~base['pre3k']]
# ATR%分布
print(f"\n  ATR%分布:")
print(f"  {'ATR%':>8} {'前3K单数':>8} {'第3K后单数':>10} {'前3K占比':>8}")
for lo, hi, label in [(0,1,"<1%"),(1,1.5,"1-1.5%"),(1.5,2,"1.5-2%"),(2,3,"2-3%"),(3,99,"3%+")]:
    p = pre3k_base  # 需要ATR%信息，这里简化
# 简化：直接看前3K vs 第3K后的ATR%
pre3k_atr = []
post3k_atr = []
last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s, addon=True)
    if r:
        av = atr_vals[s.idx-1]
        bt = s.breakthrough
        atr_pct = av/bt*100
        if r['pre3k']: pre3k_atr.append(atr_pct)
        else: post3k_atr.append(atr_pct)
        last_exit = r['exit_idx']

print(f"\n  前3K止损: ATR%均值={np.mean(pre3k_atr):.2f}%, 中位={np.median(pre3k_atr):.2f}%")
print(f"  第3K后:   ATR%均值={np.mean(post3k_atr):.2f}%, 中位={np.median(post3k_atr):.2f}%")

print(f"\n{'='*110}")
