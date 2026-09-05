"""R值统计分析：ATR止损(1-3K) + 3K保本 + KAMA跟踪 + 12%固定止盈。

ATR: RMA(tr, 20), multiplier=1, 取突破K前一根的值
R = (出场价 - 突破线) / ATR（做多），做空对称
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
print("加载数据...")
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
print(f"共 {len(df)} 根K线，{len(signals)} 个原始信号\n")

highs = df["high"].to_numpy()
lows = df["low"].to_numpy()
closes = df["close"].to_numpy()
times = df["open_time"].tolist()
n = len(df)


# ========== ATR 计算 (RMA, length=20) ==========
def calc_atr(high, low, close, length=20):
    m = len(close)
    tr = np.zeros(m)
    tr[0] = high[0] - low[0]
    for i in range(1, m):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr_vals = np.zeros(m)
    atr_vals[0] = tr[0]
    alpha = 1.0 / length
    for i in range(1, m):
        atr_vals[i] = alpha * tr[i] + (1 - alpha) * atr_vals[i - 1]
    return atr_vals


atr_vals = calc_atr(highs, lows, closes, 20)
print(f"ATR(RMA,20) 计算完成，最新值: {atr_vals[-1]:.2f}\n")


# ========== 回测：4种平仓方式 ==========
def simulate_leg_r(signal, be_bars=3, tp=0.12):
    i = signal.idx
    if i + 1 >= n:
        return None
    bt = signal.breakthrough
    d = signal.direction
    atr_v = atr_vals[i - 1] if i - 1 >= 0 else np.nan
    if np.isnan(atr_v) or atr_v <= 0:
        return None

    if d == "long":
        init_stop = bt - atr_v
        tp_price = bt * (1 + tp)
    else:
        init_stop = bt + atr_v
        tp_price = bt * (1 - tp)

    max_exc = -np.inf
    peak_idx = i
    exit_idx = exit_price = exit_reason = None

    for j in range(i + 1, n):
        ks = kama_vals[j - 1]
        if np.isnan(ks):
            continue

        if d == "long":
            exc = (highs[j] - bt) / bt
        else:
            exc = (bt - lows[j]) / bt
        if exc > max_exc:
            max_exc = exc
            peak_idx = j

        bars_held = j - i
        if bars_held <= be_bars:
            stop_line = init_stop
            s_type = "atr_stop"
        else:
            if d == "long":
                stop_line = max(ks, bt)
                s_type = "breakeven" if ks < bt else "kama_trail"
            else:
                stop_line = min(ks, bt)
                s_type = "breakeven" if ks > bt else "kama_trail"

        if d == "long" and lows[j] < stop_line:
            exit_idx, exit_price, exit_reason = j, float(stop_line), s_type
            break
        if d == "short" and highs[j] > stop_line:
            exit_idx, exit_price, exit_reason = j, float(stop_line), s_type
            break

        if tp > 0:
            if d == "long" and highs[j] >= tp_price:
                exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"
                break
            if d == "short" and lows[j] <= tp_price:
                exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"
                break

    if exit_idx is None:
        exit_idx = n - 1
        exit_price = float(closes[-1])
        exit_reason = "end"

    if d == "long":
        ret = (exit_price - bt) / bt
        r = (exit_price - bt) / atr_v
        mfe_r = (max_exc * bt) / atr_v if max_exc != -np.inf else 0.0
    else:
        ret = (bt - exit_price) / bt
        r = (bt - exit_price) / atr_v
        mfe_r = (max_exc * bt) / atr_v if max_exc != -np.inf else 0.0

    return {
        "signal_time": signal.time, "direction": d, "breakthrough": bt,
        "atr": atr_v, "atr_pct": atr_v / bt,
        "initial_stop": init_stop, "tp_price": tp_price,
        "exit_time": times[exit_idx], "exit_price": exit_price,
        "final_return": ret, "r_value": r,
        "max_excursion": float(max_exc) if max_exc != -np.inf else 0.0,
        "mfe_r": mfe_r,
        "duration_bars": exit_idx - i, "duration_hours": (exit_idx - i) * 4.0,
        "exit_reason": exit_reason,
    }


# ========== 跑回测 ==========
results = []
last_exit = None
for s in signals:
    if last_exit is not None and s.time <= last_exit:
        continue
    r = simulate_leg_r(s)
    if r is not None:
        results.append(r)
        last_exit = r["exit_time"]

rdf = pd.DataFrame(results)
# 过滤 |MFE| >= 10%
filtered = rdf[rdf["max_excursion"].abs() >= 0.10].reset_index(drop=True)
print(f"合并后 {len(rdf)} 波段，≥10%过滤后 {len(filtered)} 波段\n")

# ========== R值统计 ==========
print("=" * 95)
print("R值统计分析（ATR止损(1-3K) + 3K保本 + KAMA跟踪 + 12%固定止盈）")
print("=" * 95)

r = filtered["r_value"]
print(f"\n样本数: {len(filtered)}")
print(f"R值: 均值={r.mean():.2f}R  中位={r.median():.2f}R  std={r.std():.2f}")
print(f"     min={r.min():.2f}R  max={r.max():.2f}R")
print(f"胜率(R>0): {(r > 0).mean():.1%}  期望(均值R): {r.mean():.2f}R")
print(f"盈亏比: {r[r > 0].mean() / abs(r[r <= 0].mean()):.2f}" if (r <= 0).any() else "盈亏比: N/A")

# ========== 4种平仓方式分类 ==========
print("\n—— 4种平仓方式分类统计 ——")
reason_labels = {
    "atr_stop": "(1) ATR止损(入场跌破)",
    "breakeven": "(2) 保本平仓(3K后上移)",
    "kama_trail": "(3) KAMA跟踪止盈",
    "take_profit": "(4) 固定止盈(12%)",
    "end": "持有至末端",
}
print(f"{'平仓方式':<28} {'单数':>6} {'占比':>8} {'均R':>8} {'中位R':>8} {'均收益':>10} {'胜率':>8}")
for reason, label in reason_labels.items():
    sub = filtered[filtered["exit_reason"] == reason]
    if sub.empty:
        continue
    print(f"{label:<28} {len(sub):>6} {len(sub)/len(filtered):>8.1%} "
          f"{sub['r_value'].mean():>8.2f} {sub['r_value'].median():>8.2f} "
          f"{sub['final_return'].mean():>10.2%} {(sub['r_value']>0).mean():>8.1%}")

# ========== 多空分开 ==========
print("\n—— 多空分开 R值统计 ——")
print(f"{'方向':>6} {'单数':>6} {'均R':>8} {'中位R':>8} {'胜率':>8} {'盈亏比':>8} {'均收益':>10}")
for d, label in [("long", "做多"), ("short", "做空")]:
    sub = filtered[filtered["direction"] == d]
    sr = sub["r_value"]
    pf = sr[sr > 0].mean() / abs(sr[sr <= 0].mean()) if (sr <= 0).any() else float("nan")
    print(f"{label:>6} {len(sub):>6} {sr.mean():>8.2f} {sr.median():>8.2f} "
          f"{(sr>0).mean():>8.1%} {pf:>8.2f} {sub['final_return'].mean():>10.2%}")

# ========== R值分布 ==========
print("\n—— R值分布 ——")
bins = [-2, -1, 0, 1, 2, 3, 4, 5, 8]
print(f"{'区间':>14} {'单数':>6} {'占比':>8} {'累计':>8}")
cum = 0
for k in range(len(bins) - 1):
    cnt = ((r >= bins[k]) & (r < bins[k + 1])).sum()
    cum += cnt
    print(f"[{bins[k]:>4.0f}R, {bins[k+1]:>4.0f}R) {cnt:>6} {cnt/len(r):>8.1%} {cum/len(r):>8.1%}")

# ========== ATR%统计 ==========
print("\n—— ATR%统计（止损空间占突破线的比例）——")
ap = filtered["atr_pct"]
print(f"ATR%: 中位={ap.median():.2%}  均值={ap.mean():.2%}  min={ap.min():.2%}  max={ap.max():.2%}")
print(f"1ATR平均止损距离: {ap.mean():.2%}（即平均每单初始风险约{ap.mean():.1%}）")

# ========== 输出CSV ==========
from pathlib import Path
out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
df_out = filtered.copy()
for c in ["signal_time", "exit_time"]:
    df_out[c] = pd.to_datetime(df_out[c], utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M")
df_out.to_csv(out_dir / "r_value_analysis.csv", index=False, encoding="utf-8-sig")
print(f"\n详细数据已保存: strategies_backtest/results/r_value_analysis.csv")
print("=" * 95)
