"""暂停交易机制扫描：R>阈值后暂停N根K线，找最优调参。"""
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
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
closes = df["close"].to_numpy(); times = df["open_time"].tolist()
n = len(df)


def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)


def simulate(signal, tp_l=0.18, tp_s=0.12):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    if is_long:
        init_stop = bt - av; tp_price = bt * (1 + tp_l) if tp_l > 0 else float("inf")
    else:
        init_stop = bt + av; tp_price = bt * (1 - tp_s) if tp_s > 0 else 0.0
    max_exc = -np.inf; exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if is_long else (bt-lows[j])/bt
        if exc > max_exc: max_exc = exc
        bh = j - i
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if is_long: stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"
        if is_long and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if not is_long and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if tp_l > 0 and is_long and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
        if tp_s > 0 and not is_long and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"
    r = (exit_price-bt)/av if is_long else (bt-exit_price)/av
    ret = (exit_price-bt)/bt if is_long else (bt-exit_price)/bt
    return {"direction": d, "r_value": r, "final_return": ret,
            "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "exit_idx": exit_idx}


def run_with_pause(r_threshold, pause_bars, tp_l=0.18, tp_s=0.12):
    results = []; last_exit = -1; resume_idx = 0
    for s in signals:
        if s.idx <= last_exit or s.idx < resume_idx: continue
        r = simulate(s, tp_l, tp_s)
        if r is None: continue
        results.append(r); last_exit = r["exit_idx"]
        if r["r_value"] > r_threshold:
            resume_idx = r["exit_idx"] + pause_bars
    return pd.DataFrame(results)


print("=" * 110)
print("暂停交易机制扫描（止盈18%/12%，R>阈值后暂停N根K线）")
print("=" * 110)

r_thresholds = [3, 5, 8, 10, 15]
pause_bars_list = [0, 20, 30, 50, 60, 80, 100, 150, 200]

# 网格扫描
print(f"\n—— 全量累计R 网格（R阈值 × 暂停K线数）——")
print(f"{'R阈值':>6} │", end="")
for pb in pause_bars_list:
    print(f" {'暂停'+str(pb)+'K':>10}", end="")
print()
print("-" * (8 + 11 * len(pause_bars_list)))

best_r = -999; best_params = ""; grid = {}
for rt in r_thresholds:
    print(f"{rt:>6} │", end="")
    for pb in pause_bars_list:
        rdf = run_with_pause(rt, pb)
        cum_r = rdf["r_value"].sum() if not rdf.empty else 0
        cnt = len(rdf)
        mean_r = rdf["r_value"].mean() if not rdf.empty else 0
        win = (rdf["r_value"] > 0).mean() if not rdf.empty else 0
        ret = rdf["final_return"].sum() if not rdf.empty else 0
        grid[(rt, pb)] = (cnt, cum_r, mean_r, win, ret)
        print(f" {cum_r:>10.1f}", end="")
        if cum_r > best_r:
            best_r = cum_r; best_params = (rt, pb)
    print()

print(f"\n  最优: R>{best_params[0]}, 暂停{best_params[1]}K, 全量累计R={best_r:.1f}")

# 基准对比
rdf_base = run_with_pause(999, 0)
base_cnt, base_r, base_mean, base_win, base_ret = len(rdf_base), rdf_base["r_value"].sum(), rdf_base["r_value"].mean(), (rdf_base["r_value"]>0).mean(), rdf_base["final_return"].sum()
opt = grid[best_params]
print(f"\n—— 基准对比 ——")
print(f"  无暂停基准:  {base_cnt}单, 累计R={base_r:.1f}, 均R={base_mean:.2f}, 胜率={base_win:.0%}, 收益={base_ret:.1%}")
print(f"  最优暂停:    {opt[0]}单, 累计R={opt[1]:.1f}, 均R={opt[2]:.2f}, 胜率={opt[3]:.0%}, 收益={opt[4]:.1%}")
print(f"  变化: 累计R {opt[1]-base_r:+.1f} ({(opt[1]/base_r-1)*100:+.1f}%), 单数 {opt[0]-base_cnt:+d}")

# 均R网格
print(f"\n—— 均R 网格 ——")
print(f"{'R阈值':>6} │", end="")
for pb in pause_bars_list:
    print(f" {'暂停'+str(pb)+'K':>10}", end="")
print()
print("-" * (8 + 11 * len(pause_bars_list)))
for rt in r_thresholds:
    print(f"{rt:>6} │", end="")
    for pb in pause_bars_list:
        print(f" {grid[(rt, pb)][2]:>10.2f}", end="")
    print()

# 最优参数详细分析
print(f"\n{'='*110}")
print(f"最优参数详细分析：R>{best_params[0]}, 暂停{best_params[1]}K")
print(f"{'='*110}")

for label, rdf in [("无暂停基准", rdf_base), (f"R>{best_params[0]}暂停{best_params[1]}K", run_with_pause(*best_params))]:
    print(f"\n  {label}:")
    for d, lbl in [("long", "做多"), ("short", "做空"), ("all", "合并")]:
        sub = rdf if d == "all" else rdf[rdf["direction"] == d]
        if sub.empty: continue
        sr = sub["r_value"]
        n_big = (sr > best_params[0]).sum()
        print(f"    {lbl:>4} {len(sub):>5} 累计R={sr.sum():>7.1f} 均R={sr.mean():>6.2f} 胜率={(sr>0).mean():>5.0%} 收益={sub['final_return'].sum():>8.1%} R>{best_params[0]}单={n_big}")
    rdf_yr = rdf.copy()
    rdf_yr["year"] = pd.to_datetime([times[r["exit_idx"]] for _, r in rdf_yr.iterrows()]).year
    print(f"    年度: ", end="")
    for yr in sorted(rdf_yr["year"].unique()):
        sub = rdf_yr[rdf_yr["year"] == yr]
        print(f"{yr}={sub['r_value'].sum():.0f}R ", end="")
    print()

# 暂停次数
rdf_opt = run_with_pause(*best_params)
n_pause = (rdf_opt["r_value"] > best_params[0]).sum()
print(f"\n—— 暂停次数统计 ——")
print(f"  R>{best_params[0]}的大赢单: {n_pause}单")
print(f"  每次暂停{best_params[1]}K = {best_params[1]*4}小时 = {best_params[1]*4/24:.1f}天")
print(f"  总暂停K线: {n_pause * best_params[1]}根 = {n_pause * best_params[1] * 4 / 24:.0f}天")

# 不同止盈配置
print(f"\n{'='*110}")
print(f"不同止盈配置下的最优暂停参数")
print(f"{'='*110}")
print(f"\n{'止盈配置':>10} {'R阈值':>6} {'暂停K':>6} {'单数':>6} {'全量累计R':>10} {'均R':>7} {'胜率':>6} {'收益':>9}")
print("-" * 70)
for tp_l, tp_s, tp_label in [(0.18, 0.12, "18%/12%"), (0.22, 0.12, "22%/12%"), (0.0, 0.0, "纯KAMA")]:
    best_r2 = -999; best_p2 = None
    for rt in [3, 5, 8]:
        for pb in [0, 50, 60, 100, 150]:
            rdf = run_with_pause(rt, pb, tp_l, tp_s)
            if rdf.empty: continue
            cum_r = rdf["r_value"].sum()
            if cum_r > best_r2:
                best_r2 = cum_r; best_p2 = (rt, pb, len(rdf), rdf["r_value"].mean(), (rdf["r_value"]>0).mean(), rdf["final_return"].sum())
    rt, pb, cnt, mr, w, ret = best_p2
    print(f"{tp_label:>10} {rt:>6} {pb:>6} {cnt:>6} {best_r2:>10.1f} {mr:>7.2f} {w:>6.0%} {ret:>9.1%}")

print(f"\n{'='*110}")
