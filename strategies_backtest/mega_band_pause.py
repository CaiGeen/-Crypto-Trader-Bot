"""大波段归并 + 暂停机制扫描。

逻辑：
  1. 跌破KAMA出场后，kama_window根K线内的新信号归入同一大波段
  2. 大波段结束（超出kama_window无新信号，或固定止盈/ATR止损出场）
  3. 大波段累计R > r_threshold → 暂停pause_bars根K线
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
            "exit_reason": exit_reason, "exit_idx": exit_idx, "signal_idx": i}


def run_mega(kama_window, r_threshold, pause_bars, tp_l=0.18, tp_s=0.12):
    """大波段归并 + 暂停机制。"""
    all_results = []
    mega_bands = []
    last_exit = -1; resume_idx = 0
    mega_subs = []; mega_deadline = 0; in_mega = False

    for s in signals:
        if s.idx <= last_exit or s.idx < resume_idx: continue
        r = simulate(s, tp_l, tp_s)
        if r is None: continue
        all_results.append(r); last_exit = r["exit_idx"]

        # 判断是否归入当前大波段
        if in_mega and s.idx <= mega_deadline:
            mega_subs.append(r)
        else:
            if mega_subs:
                mega_bands.append(mega_subs)
                mega_r = sum(x["r_value"] for x in mega_subs)
                if mega_r > r_threshold:
                    resume_idx = mega_subs[-1]["exit_idx"] + pause_bars
            mega_subs = [r]

        # 跌破KAMA → 设置归并截止
        if r["exit_reason"] in ("kama_trail", "breakeven"):
            in_mega = True
            mega_deadline = r["exit_idx"] + kama_window
        else:
            in_mega = False
            mega_deadline = 0

    if mega_subs:
        mega_bands.append(mega_subs)

    return all_results, mega_bands


print("=" * 110)
print("大波段归并 + 暂停机制扫描（止盈18%/12%）")
print("=" * 110)

# 1. 先看大波段归并效果（无暂停）
print(f"\n—— 大波段归并统计（无暂停，不同kama_window）——")
print(f"{'kama_window':>12} {'子波段数':>8} {'大波段数':>8} {'归并率':>7} {'全量累计R':>10} {'大波段均R':>10} {'子波段均R':>9}")
print("-" * 75)
for kw in [5, 10, 15, 20, 30]:
    all_r, mega = run_mega(kw, 999, 0)
    cum_r = sum(x["r_value"] for x in all_r)
    mega_rs = [sum(x["r_value"] for x in m) for m in mega]
    mega_mean = np.mean(mega_rs) if mega_rs else 0
    sub_mean = cum_r / len(all_r) if all_r else 0
    merge_rate = 1 - len(mega) / len(all_r) if all_r else 0
    print(f"{kw:>12} {len(all_r):>8} {len(mega):>8} {merge_rate:>7.0%} {cum_r:>10.1f} {mega_mean:>10.2f} {sub_mean:>9.2f}")

# 2. 大波段R分布（kama_window=10）
print(f"\n—— 大波段R分布（kama_window=10，无暂停）——")
all_r, mega = run_mega(10, 999, 0)
mega_rs = [sum(x["r_value"] for x in m) for m in mega]
mega_sizes = [len(m) for m in mega]
print(f"  大波段数: {len(mega)}")
print(f"  大波段R: 均值{np.mean(mega_rs):.2f}, 中位{np.median(mega_rs):.2f}, 范围[{min(mega_rs):.1f}, {max(mega_rs):.1f}]")
print(f"  子波段数/大波段: 均值{np.mean(mega_sizes):.2f}, 中位{np.median(mega_sizes):.0f}, 最大{max(mega_sizes)}")
print(f"\n  {'大波段R区间':>16} {'个数':>5} {'占比':>6} {'子波段均数':>10}")
print("  " + "-" * 45)
for lo, hi in [(-999, -2+0.001), (-2, 0), (0, 3), (3, 5), (5, 8), (8, 15), (15, 999)]:
    idx = [i for i, r in enumerate(mega_rs) if lo <= r < hi]
    if not idx: continue
    avg_size = np.mean([mega_sizes[i] for i in idx])
    print(f"  [{lo:>5}, {hi:>5}) {len(idx):>5} {len(idx)/len(mega):>6.0%} {avg_size:>10.1f}")

# 3. 暂停机制扫描（kama_window=10）
print(f"\n—— 暂停机制扫描（kama_window=10，大波段R>阈值后暂停N根K线）——")
print(f"{'R阈值':>6} │", end="")
pause_list = [0, 30, 50, 60, 80, 100, 150, 200]
for pb in pause_list:
    print(f" {'暂停'+str(pb)+'K':>10}", end="")
print()
print("-" * (8 + 11 * len(pause_list)))

best_r = -999; best_params = None
for rt in [3, 5, 8, 10, 15]:
    print(f"{rt:>6} │", end="")
    for pb in pause_list:
        all_r, mega = run_mega(10, rt, pb)
        cum_r = sum(x["r_value"] for x in all_r)
        print(f" {cum_r:>10.1f}", end="")
        if cum_r > best_r:
            best_r = cum_r; best_params = (rt, pb)
    print()

# 4. kama_window × 暂停 联合扫描
print(f"\n—— kama_window × 暂停K线 联合扫描（R>5）——")
print(f"{'kw':>4} │", end="")
for pb in [0, 50, 60, 100, 150]:
    print(f" {'暂停'+str(pb)+'K':>10}", end="")
print()
print("-" * (6 + 11 * 5))
best_r2 = -999; best_p2 = None
for kw in [5, 10, 15, 20]:
    print(f"{kw:>4} │", end="")
    for pb in [0, 50, 60, 100, 150]:
        all_r, mega = run_mega(kw, 5, pb)
        cum_r = sum(x["r_value"] for x in all_r)
        print(f" {cum_r:>10.1f}", end="")
        if cum_r > best_r2:
            best_r2 = cum_r; best_p2 = (kw, 5, pb)
    print()

# 5. 基准对比
print(f"\n—— 基准对比 ——")
all_base, _ = run_mega(10, 999, 0)
base_r = sum(x["r_value"] for x in all_base)
print(f"  无暂停基准:           {len(all_base)}单, 累计R={base_r:.1f}")

all_opt, mega_opt = run_mega(best_p2[0], best_p2[1], best_p2[2])
opt_r = sum(x["r_value"] for x in all_opt)
print(f"  最优(kw={best_p2[0]},R>{best_p2[1]},暂停{best_p2[2]}K): {len(all_opt)}单, 累计R={opt_r:.1f}, 大波段{len(mega_opt)}个")
print(f"  变化: 累计R {opt_r-base_r:+.1f} ({(opt_r/base_r-1)*100:+.1f}%), 单数 {len(all_opt)-len(all_base):+d}")

# 6. 最优配置详细分析
print(f"\n{'='*110}")
print(f"最优配置详情：kw={best_p2[0]}, R>{best_p2[1]}, 暂停{best_p2[2]}K")
print(f"{'='*110}")

all_opt, mega_opt = run_mega(best_p2[0], best_p2[1], best_p2[2])
mega_rs = [sum(x["r_value"] for x in m) for m in mega_opt]
mega_sizes = [len(m) for m in mega_opt]
n_pause = sum(1 for r in mega_rs if r > best_p2[1])

print(f"\n  子波段: {len(all_opt)}单, 累计R={opt_r:.1f}")
print(f"  大波段: {len(mega_opt)}个, 大波段R均值={np.mean(mega_rs):.2f}")
print(f"  归并率: {1-len(mega_opt)/len(all_opt):.0%}, 子波段/大波段均值={np.mean(mega_sizes):.2f}")
print(f"  暂停触发: {n_pause}次, 每次暂停{best_p2[2]}K={best_p2[2]*4/24:.1f}天, 总暂停{n_pause*best_p2[2]*4/24:.0f}天")

# 多空分离
for d, lbl in [("long", "做多"), ("short", "做空")]:
    sub = [x for x in all_opt if x["direction"] == d]
    if not sub: continue
    sr = pd.Series([x["r_value"] for x in sub])
    print(f"  {lbl}: {len(sub)}单, 累计R={sr.sum():.1f}, 均R={sr.mean():.2f}, 胜率={(sr>0).mean():.0%}")

# 年度
rdf_yr = pd.DataFrame(all_opt)
rdf_yr["year"] = pd.to_datetime([times[r["exit_idx"]] for _, r in rdf_yr.iterrows()]).year
print(f"\n  年度统计:")
print(f"  {'年份':>6} {'单数':>5} {'累计R':>8} {'均R':>7} {'胜率':>6}")
print("  " + "-" * 35)
for yr in sorted(rdf_yr["year"].unique()):
    sub = rdf_yr[rdf_yr["year"] == yr]
    sr = sub["r_value"]
    print(f"  {yr:>6} {len(sub):>5} {sr.sum():>8.1f} {sr.mean():>7.2f} {(sr>0).mean():>6.0%}")

print(f"\n{'='*110}")
