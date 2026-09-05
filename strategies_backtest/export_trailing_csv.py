"""导出trailing stop 0.5x + 加仓的完整CSV（无未来函数）"""
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

def simulate(signal, trail_atr=0.5, tp_l=0.18, tp_s=0.12,
             addon_thresh=0.023, include_bk=True):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long: init_stop = bt - av; tp_price = bt * (1 + tp_l)
    else: init_stop = bt + av; tp_price = bt * (1 - tp_s)
    peak = bt
    addon_done = False; addon_price = 0.0; addon_bar = 0
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3: sl = init_stop
        else:
            if is_long: sl = max(peak - trail_atr * av, bt, ks)
            else: sl = min(peak + trail_atr * av, bt, ks)
        if not addon_done:
            cg = (closes[j] - bt) / bt if is_long else (bt - closes[j]) / bt
            if (include_bk or bh > 1) and cg >= addon_thresh:
                addon_done = True; addon_price = float(closes[j]); addon_bar = bh
        if is_long and lows[j] < sl: exit_idx, exit_price, exit_reason = j, float(sl), "trailing_stop"; break
        if not is_long and highs[j] > sl: exit_idx, exit_price, exit_reason = j, float(sl), "trailing_stop"; break
        if is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r_base = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    r_addon = 0.0
    if addon_done:
        r_addon = (exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av
    return {
        "信号时间": times.iloc[i] + pd.Timedelta(hours=8),
        "方向": "做多" if is_long else "做空",
        "突破线": round(bt, 1),
        "ATR": round(av, 1),
        "ATR%": round(av/bt*100, 2),
        "初始止损": round(init_stop, 1),
        "止盈价": round(tp_price, 1),
        "加仓": "是" if addon_done else "否",
        "加仓价": round(addon_price, 1) if addon_done else "",
        "加仓K": addon_bar if addon_done else "",
        "出场时间": times.iloc[exit_idx] + pd.Timedelta(hours=8),
        "出场价": round(exit_price, 1),
        "出场原因": exit_reason,
        "持仓K线": exit_idx - i,
        "持仓小时": (exit_idx - i) * 4,
        "主仓R": round(r_base, 2),
        "加仓R": round(r_addon, 2) if addon_done else "",
        "总R": round(r_base + r_addon, 2),
    }

results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r:
        results.append(r)
        last_exit = s.idx + r["持仓K线"]

rdf = pd.DataFrame(results)
rdf["累计R"] = rdf["总R"].cumsum().round(1)

# 写CSV with BOM
out_path = os.path.join(os.path.dirname(__file__), "results", "trailing_stop_0.5x_加仓.csv")
rdf.to_csv(out_path, index=False, encoding="utf-8-sig")

print(f"导出 {len(rdf)} 单到 {out_path}")
print(f"累计R={rdf['总R'].sum():.1f}, 均R={rdf['总R'].mean():.2f}, 胜率={(rdf['总R']>0).mean():.0%}")
print(f"加仓单: {(rdf['加仓']=='是').sum()}")
print()
print(rdf.head(20).to_string(index=False))
print("...")
print(rdf.tail(10).to_string(index=False))
