"""对比固定止盈+KAMA vs trailing stop 0.5x，仅MFE>=6%的波段"""
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
closes = df["close"].to_numpy(); n = len(df)
times = df['open_time']

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

def calc_mfe(signal):
    """计算波段MFE（最大有利偏移），用所有K线直到数据末尾"""
    i = signal.idx
    bt = signal.breakthrough; is_long = (signal.direction == "long")
    max_exc = -np.inf
    for j in range(i+1, n):
        if is_long: exc = (highs[j] - bt) / bt
        else: exc = (bt - lows[j]) / bt
        if exc > max_exc: max_exc = exc
    return max_exc

def simulate_base(signal, tp_l=0.18, tp_s=0.12):
    """固定止盈+KAMA跟踪止损（无未来函数）"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(ks, bt) if is_long else min(ks, bt)
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "exit_price": exit_price,
            "exit_reason": exit_reason, "hold_bars": exit_idx - i}

def simulate_trailing(signal, tp_l=0.18, tp_s=0.12, trail_atr=0.5):
    """trailing stop 0.5x（无未来函数）"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    peak = bt
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else: sl = max(peak - trail_atr * av, bt, ks) if is_long else min(peak + trail_atr * av, bt, ks)
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "trailing_stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "trailing_stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "tp"; break
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    return {"r_value": r, "exit_idx": exit_idx, "exit_price": exit_price,
            "exit_reason": exit_reason, "hold_bars": exit_idx - i}

# 收集所有信号，计算MFE，用信号合并规则
all_signals = []
last_exit_base = -1
for s in signals:
    if s.idx <= last_exit_base: continue
    mfe = calc_mfe(s)
    r_base = simulate_base(s)
    r_trail = simulate_trailing(s)
    if r_base is None or r_trail is None: continue
    is_long = (s.direction == "long")
    bt = s.breakthrough
    av = atr_vals[s.idx - 1]
    all_signals.append({
        "sig_idx": s.idx,
        "sig_time": times.iloc[s.idx] + pd.Timedelta(hours=8),
        "direction": "做多" if is_long else "做空",
        "breakthrough": bt,
        "atr": av,
        "atr_pct": av / bt * 100,
        "mfe": mfe,
        "mfe_pct": mfe * 100,
        # 基准
        "base_r": r_base["r_value"],
        "base_exit": r_base["exit_price"],
        "base_reason": r_base["exit_reason"],
        "base_hold": r_base["hold_bars"],
        # trailing
        "trail_r": r_trail["r_value"],
        "trail_exit": r_trail["exit_price"],
        "trail_reason": r_trail["exit_reason"],
        "trail_hold": r_trail["hold_bars"],
        # 差异
        "r_diff": r_trail["r_value"] - r_base["r_value"],
        "hold_diff": r_trail["hold_bars"] - r_base["hold_bars"],
    })
    last_exit_base = r_base["exit_idx"]

adf = pd.DataFrame(all_signals)

# MFE>=6%的波段
mfe6 = adf[adf["mfe"] >= 0.06].copy()
mfe6["cum_base_r"] = mfe6["base_r"].cumsum()
mfe6["cum_trail_r"] = mfe6["trail_r"].cumsum()

print("=" * 110)
print("MFE>=6%波段：固定止盈+KAMA vs trailing stop 0.5x")
print("=" * 110)

print(f"\n全量: {len(adf)}单")
print(f"  基准 累计R={adf['base_r'].sum():.1f}, 均R={adf['base_r'].mean():.2f}, 胜率={(adf['base_r']>0).mean():.0%}")
print(f"  trailing 累计R={adf['trail_r'].sum():.1f}, 均R={adf['trail_r'].mean():.2f}, 胜率={(adf['trail_r']>0).mean():.0%}")
print(f"\nMFE>=6%: {len(mfe6)}单 ({len(mfe6)/len(adf)*100:.0f}%)")
print(f"  基准 累计R={mfe6['base_r'].sum():.1f}, 均R={mfe6['base_r'].mean():.2f}, 胜率={(mfe6['base_r']>0).mean():.0%}")
print(f"  trailing 累计R={mfe6['trail_r'].sum():.1f}, 均R={mfe6['trail_r'].mean():.2f}, 胜率={(mfe6['trail_r']>0).mean():.0%}")
print(f"  差异: trailing-基准 = {mfe6['trail_r'].sum()-mfe6['base_r'].sum():+.1f}R")

# 逐单对比
print(f"\n{'='*110}")
print("逐单对比（MFE>=6%）")
print("=" * 110)
trail_better = mfe6[mfe6["r_diff"] > 0.01]
base_better = mfe6[mfe6["r_diff"] < -0.01]
similar = mfe6[abs(mfe6["r_diff"]) <= 0.01]
print(f"\n  trailing赚更多: {len(trail_better)}单, ΔR={trail_better['r_diff'].sum():.1f}")
print(f"  基准赚更多:     {len(base_better)}单, ΔR={base_better['r_diff'].sum():.1f}")
print(f"  基本相同:       {len(similar)}单")

# 出场原因分布
print(f"\n{'='*110}")
print("出场原因分布")
print("=" * 110)
print(f"\n  {'方式':>10} {'止盈':>6} {'KAMA止损':>8} {'trailing止损':>12} {'其他':>6}")
print(f"  {'-'*50}")
for label, col in [("基准", "base_reason"), ("trailing", "trail_reason")]:
    tp = (mfe6[col] == "tp").sum()
    stop = (mfe6[col] == "stop").sum()
    trail = (mfe6[col] == "trailing_stop").sum()
    other = len(mfe6) - tp - stop - trail
    print(f"  {label:>10} {tp:>6} {stop:>8} {trail:>12} {other:>6}")

# 持仓时间对比
print(f"\n{'='*110}")
print("持仓时间对比")
print("=" * 110)
print(f"\n  {'方式':>10} {'中位K':>6} {'均K':>6} {'1K':>4} {'2-3K':>5} {'4-7K':>5} {'8K+':>4}")
print(f"  {'-'*40}")
for label, col in [("基准", "base_hold"), ("trailing", "trail_hold")]:
    h = mfe6[col]
    print(f"  {label:>10} {h.median():>6.0f} {h.mean():>6.1f} {(h==1).sum():>4} {((h>=2)&(h<=3)).sum():>5} {((h>=4)&(h<=7)).sum():>5} {(h>=8).sum():>4}")

# R值分布对比
print(f"\n{'='*110}")
print("R值分布对比")
print("=" * 110)
print(f"\n  {'R区间':>10} {'基准单数':>8} {'trailing单数':>12} {'基准均R':>8} {'trailing均R':>10}")
print(f"  {'-'*55}")
for lo, hi, label in [(-99,-1,"R<-1"),(-1,0,"-1~0"),(0,1,"0~1"),(1,3,"1~3"),(3,5,"3~5"),(5,99,"5+")]:
    b = mfe6[(mfe6["base_r"]>=lo)&(mfe6["base_r"]<hi)]
    t = mfe6[(mfe6["trail_r"]>=lo)&(mfe6["trail_r"]<hi)]
    print(f"  {label:>10} {len(b):>8} {len(t):>12} {b['base_r'].mean() if len(b)>0 else 0:>8.2f} {t['trail_r'].mean() if len(t)>0 else 0:>10.2f}")

# 多空分拆
print(f"\n{'='*110}")
print("多空分拆")
print("=" * 110)
for d in ["做多", "做空"]:
    sub = mfe6[mfe6["direction"]==d]
    print(f"\n  {d}: {len(sub)}单")
    print(f"    基准: 累计R={sub['base_r'].sum():.1f}, 均R={sub['base_r'].mean():.2f}, 胜率={(sub['base_r']>0).mean():.0%}, 持仓中位{sub['base_hold'].median():.0f}K")
    print(f"    trailing: 累计R={sub['trail_r'].sum():.1f}, 均R={sub['trail_r'].mean():.2f}, 胜率={(sub['trail_r']>0).mean():.0%}, 持仓中位{sub['trail_hold'].median():.0f}K")
    print(f"    差异: {sub['trail_r'].sum()-sub['base_r'].sum():+.1f}R")

# 导出CSV
out_cols = ["sig_time", "direction", "breakthrough", "atr", "atr_pct", "mfe_pct",
            "base_r", "base_exit", "base_reason", "base_hold",
            "trail_r", "trail_exit", "trail_reason", "trail_hold", "r_diff", "hold_diff"]
out = mfe6[out_cols].copy()
out.columns = ["信号时间", "方向", "突破线", "ATR", "ATR%", "MFE%",
               "基准R", "基准出场价", "基准出场原因", "基准持仓K",
               "trailingR", "trailing出场价", "trailing出场原因", "trailing持仓K", "R差异", "持仓K差异"]
out["突破线"] = out["突破线"].round(1)
out["ATR"] = out["ATR"].round(1)
out["ATR%"] = out["ATR%"].round(2)
out["MFE%"] = out["MFE%"].round(1)
out["基准出场价"] = out["基准出场价"].round(1)
out["trailing出场价"] = out["trailing出场价"].round(1)
out["基准R"] = out["基准R"].round(2)
out["trailingR"] = out["trailingR"].round(2)
out["R差异"] = out["R差异"].round(2)
out_path = os.path.join(os.path.dirname(__file__), "results", "mfe6_对比.csv")
out.to_csv(out_path, index=False, encoding="utf-8-sig")
print(f"\n导出 {len(out)} 单到 {out_path}")

print(f"\n{'='*110}")
