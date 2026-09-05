"""首次实体突破阈值后继续大涨的概率分析。

核心问题：如果突破K后某根K线收盘价首次涨了X%，后续继续大涨的概率有多大？
这相当于一个"突破确认信号"——首次达到X%说明突破有效，后续可能继续涨。
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

    # 记录首次实体突破各阈值的时间和后续涨幅
    thresholds = [0.01, 0.02, 0.023, 0.03, 0.04, 0.06]
    first_hit = {t: None for t in thresholds}  # 首次实体突破阈值t的K线根数

    max_exc = -np.inf; exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if is_long else (bt-lows[j])/bt
        close_exc = (closes[j]-bt)/bt if is_long else (bt-closes[j])/bt
        if exc > max_exc: max_exc = exc
        bh = j - i

        # 记录首次实体突破（用收盘价）
        for t in thresholds:
            if first_hit[t] is None and close_exc >= t:
                first_hit[t] = bh

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

    # 计算从首次突破各阈值开始，后续的最大涨幅（MFE - 阈值）
    result = {"direction": d, "r_value": r, "final_return": ret,
              "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
              "exit_reason": exit_reason, "exit_idx": exit_idx,
              "hold_bars": exit_idx - i, "atr_pct": av / bt}

    for t in thresholds:
        result[f"first_hit_{t:.3f}"] = first_hit[t]  # None表示从未达到

    return result

all_results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r: all_results.append(r); last_exit = r["exit_idx"]
rdf = pd.DataFrame(all_results)

print("=" * 110)
print("首次实体突破阈值后继续大涨的概率分析")
print("=" * 110)
print(f"\n全量: {len(rdf)}单, 累计R={rdf['r_value'].sum():.1f}")
print(f"\n说明：'实体突破X%'指某根K线收盘价首次达到突破线+X%")
print(f"      '后续涨幅'指从首次突破X%开始，波段剩余的最大涨幅（MFE - X%）")

# 对每个阈值分析
for thresh in [0.01, 0.02, 0.023, 0.03, 0.04, 0.06]:
    col = f"first_hit_{thresh:.3f}"
    hit = rdf[rdf[col].notna()]
    no_hit = rdf[rdf[col].isna()]

    print(f"\n{'='*110}")
    print(f"阈值：首次实体突破 {thresh:.1%}")
    print(f"{'='*110}")

    total = len(rdf)
    print(f"\n  达到{thresh:.1%}的: {len(hit)}单 ({len(hit)/total:.0%})")
    print(f"  从未达到{thresh:.1%}的: {len(no_hit)}单 ({len(no_hit)/total:.0%}), 这些单MFE中位={no_hit['mfe'].median():.1%}, 均R={no_hit['r_value'].mean():.2f}")

    if hit.empty: continue

    # 首次突破时间分布
    print(f"\n  首次突破{thresh:.1%}的时间分布:")
    print(f"  {'首次在第N根':>10} {'单数':>5} {'占比':>6} {'后续MFE中位':>12} {'后续均R':>8} {'胜率':>6} {'≥6%概率':>8} {'≥10%概率':>9}")
    print("  " + "-" * 70)
    time_bins = [(1,1),(2,2),(3,3),(4,5),(6,8),(9,12),(13,18),(19,999)]
    time_labels = ["第1根","第2根","第3根","第4-5根","第6-8根","第9-12根","第13-18根","第19根+"]
    for (lo,hi),lbl in zip(time_bins, time_labels):
        sub = hit[(hit[col]>=lo)&(hit[col]<=hi)]
        if sub.empty: continue
        # 后续涨幅 = MFE - thresh
        remaining = sub["mfe"] - thresh
        ge6 = (remaining >= 0.06 - thresh).mean()  # 后续再涨(6%-thresh)以上
        ge10 = (remaining >= 0.10 - thresh).mean()
        print(f"  {lbl:>10} {len(sub):>5} {len(sub)/len(hit):>5.0%} {remaining.median():>11.1%} {sub['r_value'].mean():>8.2f} {(sub['r_value']>0).mean():>5.0%} {ge6:>7.0%} {ge10:>8.0%}")

    # 核心：首次突破后继续大涨的概率
    print(f"\n  首次突破{thresh:.1%}后，后续继续涨的概率:")
    remaining_all = hit["mfe"] - thresh
    for target in [0.02, 0.04, 0.06, 0.08, 0.10]:
        prob = (remaining_all >= target).mean()
        print(f"    后续再涨≥{target:.0%}: {prob:.0%} ({(remaining_all>=target).sum()}/{len(hit)}单)")

    # 对比：达到vs未达到
    print(f"\n  对比: 达到{thresh:.1%} vs 未达到{thresh:.1%}")
    print(f"    达到:   {len(hit)}单, 均R={hit['r_value'].mean():.2f}, 累计R={hit['r_value'].sum():.1f}, 胜率={(hit['r_value']>0).mean():.0%}, MFE中位={hit['mfe'].median():.1%}")
    print(f"    未达到: {len(no_hit)}单, 均R={no_hit['r_value'].mean():.2f}, 累计R={no_hit['r_value'].sum():.1f}, 胜率={(no_hit['r_value']>0).mean():.0%}, MFE中位={no_hit['mfe'].median():.1%}")

# 关键对比：首次突破2%的时间 vs 后续大涨概率
print(f"\n{'='*110}")
print("汇总：首次突破各阈值的时间 vs 后续≥10%概率")
print(f"{'='*110}")
print(f"\n{'阈值':>6} {'达到率':>6} {'第1根':>8} {'第2根':>8} {'第3根':>8} {'第4-5根':>8} {'第6-8根':>8} {'第9+根':>8} {'从未':>8}")
print("-" * 70)
for thresh in [0.01, 0.02, 0.023, 0.03, 0.04, 0.06]:
    col = f"first_hit_{thresh:.3f}"
    hit = rdf[rdf[col].notna()]
    no_hit_cnt = rdf[col].isna().sum()
    row = f"{thresh:>5.1%} {len(hit)/len(rdf):>5.0%}"
    for (lo,hi) in [(1,1),(2,2),(3,3),(4,5),(6,8),(9,999)]:
        sub = hit[(hit[col]>=lo)&(hit[col]<=hi)]
        if sub.empty:
            row += f" {'—':>8}"
        else:
            remaining = sub["mfe"] - thresh
            prob10 = (remaining >= 0.10 - thresh).mean()
            row += f" {f'{prob10:.0%}':>8}"
    row += f" {f'{no_hit_cnt}单':>8}"
    print(row)

print(f"\n  注：表中数字为'首次在该时间突破阈值后，后续再涨到10%+的概率'")
print(f"      例如'2%阈值/第3根/60%'= 第3根首次突破2%后，60%概率后续涨到10%+")

# 额外分析：首次突破2.3%后，按首次突破时间分档的详细R值
print(f"\n{'='*110}")
print("详细：首次突破2.3%后按时间分档的完整统计")
print(f"{'='*110}")
col = "first_hit_0.023"
hit = rdf[rdf[col].notna()]
print(f"\n{'首次在第N根':>10} {'单数':>5} {'均R':>6} {'累计R':>8} {'胜率':>6} {'MFE中位':>8} {'后续≥4%':>8} {'后续≥6%':>8} {'后续≥8%':>8} {'持仓中位':>8}")
print("-" * 85)
for (lo,hi),lbl in zip(time_bins, time_labels):
    sub = hit[(hit[col]>=lo)&(hit[col]<=hi)]
    if sub.empty: continue
    remaining = sub["mfe"] - 0.023
    p4 = (remaining >= 0.04-0.023).mean()
    p6 = (remaining >= 0.06-0.023).mean()
    p8 = (remaining >= 0.08-0.023).mean()
    print(f"{lbl:>10} {len(sub):>5} {sub['r_value'].mean():>6.2f} {sub['r_value'].sum():>8.1f} {(sub['r_value']>0).mean():>5.0%} {sub['mfe'].median():>7.1%} {p4:>7.0%} {p6:>7.0%} {p8:>7.0%} {sub['hold_bars'].median():>7.0f}")

# 多空分离
print(f"\n  多空分离（首次突破2.3%后）:")
for d, lbl in [("long", "做多"), ("short", "做空")]:
    dhit = hit[hit["direction"] == d]
    if dhit.empty: continue
    remaining = dhit["mfe"] - 0.023
    print(f"  {lbl}: {len(dhit)}单, 均R={dhit['r_value'].mean():.2f}, 后续≥6%概率={(remaining>=0.06-0.023).mean():.0%}, 后续≥10%概率={(remaining>=0.10-0.023).mean():.0%}")

print(f"\n{'='*110}")
