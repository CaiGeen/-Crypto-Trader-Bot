"""确认阈值：固定百分比 vs ATR倍数 对比测试"""
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

def simulate(signal, init_stop_mult=0.3, confirm_pct=0.0, confirm_atr=0.0,
             tp_l=0.18, tp_s=0.12, addon=True, addon_thresh=0.023):
    """
    confirm_pct: 固定百分比确认阈值（如0.005=0.5%）
    confirm_atr: ATR倍数确认阈值（如0.3=0.3×ATR）
    两者互斥，都>0时优先用confirm_atr
    """
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    # 确认阈值检查
    cg_abs = (closes[i]-bt) if is_long else (bt-closes[i])  # 收盘价超过突破线的绝对距离
    if confirm_atr > 0:
        if cg_abs < confirm_atr * av:
            return None
    elif confirm_pct > 0:
        if cg_abs / bt < confirm_pct:
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

# 基准（无确认）
base = run(init_stop_mult=1.0, confirm_pct=0.0)
print(f"基准+加仓（无确认，1ATR止损）: {base['r'].sum():.1f}R, {len(base)}单")

print(f"\n{'='*100}")
print("对比1：固定百分比确认 × 0.3ATR止损")
print(f"{'='*100}")
print(f"\n{'确认%':>7} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6} {'单数':>6} {'跳过':>6}")
print("-" * 50)
for pct in [0.0, 0.003, 0.005, 0.008, 0.01]:
    rdf = run(init_stop_mult=0.3, confirm_pct=pct)
    vs = rdf["r"].sum() - base["r"].sum()
    print(f"{pct:>6.1%} {rdf['r'].sum():>8.1f} {vs:>+8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(rdf):>6} {len(base)-len(rdf):>6}")

print(f"\n{'='*100}")
print("对比2：ATR倍数确认 × 0.3ATR止损")
print(f"{'='*100}")
print(f"\n{'确认ATR':>8} {'累计R':>8} {'vs基准':>8} {'均R':>6} {'胜率':>6} {'单数':>6} {'跳过':>6}")
print("-" * 55)
for atr_mult in [0.0, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5]:
    rdf = run(init_stop_mult=0.3, confirm_atr=atr_mult)
    vs = rdf["r"].sum() - base["r"].sum()
    print(f"{atr_mult:>7.2f}x {rdf['r'].sum():>8.1f} {vs:>+8.1f} {rdf['r'].mean():>6.2f} {(rdf['r']>0).mean():>5.0%} {len(rdf):>6} {len(base)-len(rdf):>6}")

print(f"\n{'='*100}")
print("对比3：两种确认方式并排（0.3ATR止损）")
print(f"{'='*100}")
print(f"\n{'方式':>12} {'参数':>8} {'累计R':>8} {'均R':>6} {'胜率':>6} {'单数':>6}")
print("-" * 55)
# 固定0.5%
rdf1 = run(init_stop_mult=0.3, confirm_pct=0.005)
print(f"{'固定百分比':>10} {'0.5%':>8} {rdf1['r'].sum():>8.1f} {rdf1['r'].mean():>6.2f} {(rdf1['r']>0).mean():>5.0%} {len(rdf1):>6}")
# ATR 0.3x
rdf2 = run(init_stop_mult=0.3, confirm_atr=0.3)
print(f"{'ATR倍数':>10} {'0.3x':>8} {rdf2['r'].sum():>8.1f} {rdf2['r'].mean():>6.2f} {(rdf2['r']>0).mean():>5.0%} {len(rdf2):>6}")
# ATR 0.25x
rdf3 = run(init_stop_mult=0.3, confirm_atr=0.25)
print(f"{'ATR倍数':>10} {'0.25x':>8} {rdf3['r'].sum():>8.1f} {rdf3['r'].mean():>6.2f} {(rdf3['r']>0).mean():>5.0%} {len(rdf3):>6}")

# 年度对比
print(f"\n{'='*100}")
print("年度分解：固定0.5% vs ATR 0.3x vs ATR 0.25x（均带0.3ATR止损+加仓）")
print(f"{'='*100}")
print(f"\n{'年份':>6}  {'固定0.5%':>10}  {'ATR0.30x':>10}  {'ATR0.25x':>10}")
print("-" * 42)
for yr in sorted(rdf1['year'].unique()):
    r1 = rdf1[rdf1['year']==yr]['r'].sum()
    r2 = rdf2[rdf2['year']==yr]['r'].sum()
    r3 = rdf3[rdf3['year']==yr]['r'].sum()
    print(f"{yr:>6}  {r1:>10.1f}  {r2:>10.1f}  {r3:>10.1f}")
print(f"{'合计':>6}  {rdf1['r'].sum():>10.1f}  {rdf2['r'].sum():>10.1f}  {rdf3['r'].sum():>10.1f}")
