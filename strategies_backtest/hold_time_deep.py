"""深入分析：前3根假突破特征、第6根拐点、MFE时机 vs 最终R值。"""
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
closes = df["close"].to_numpy(); opens = df["open"].to_numpy()
times = df["open_time"].tolist()
n = len(df)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

daily_df = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1d.parquet")
k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
daily_df["date"] = daily_df["open_time"].dt.date
date_to_ema = dict(zip(daily_df["date"], de70))

def get_prev_dev(i):
    if i - 1 < 0: return np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100

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
    max_exc = -np.inf; mfe_bar = 0; exit_idx = exit_price = exit_reason = None
    exc_at_3 = exc_at_5 = exc_at_8 = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        exc = (highs[j]-bt)/bt if is_long else (bt-lows[j])/bt
        if exc > max_exc: max_exc = exc; mfe_bar = j - i
        bh = j - i
        if bh == 3: exc_at_3 = exc
        if bh == 5: exc_at_5 = exc
        if bh == 8: exc_at_8 = exc
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
    hold_bars = exit_idx - i
    # 突破前3根涨幅
    if is_long:
        pre_gain = (closes[i] - closes[i-3]) / closes[i-3] if i >= 3 else np.nan
    else:
        pre_gain = (closes[i-3] - closes[i]) / closes[i-3] if i >= 3 else np.nan
    # 突破K线实体大小
    body = abs(closes[i] - opens[i]) / bt
    # 突破K线是否收阳（做多）/收阴（做空）
    favorable_candle = (closes[i] > opens[i]) if is_long else (closes[i] < opens[i])
    return {"direction": d, "r_value": r, "final_return": ret,
            "mfe": float(max_exc) if max_exc != -np.inf else 0.0,
            "exit_reason": exit_reason, "exit_idx": exit_idx,
            "hold_bars": hold_bars, "atr_pct": av / bt,
            "dev_prev": get_prev_dev(i), "mfe_bar": mfe_bar,
            "exc_at_3": exc_at_3, "exc_at_5": exc_at_5, "exc_at_8": exc_at_8,
            "pre_gain": pre_gain, "body": body, "fav_candle": favorable_candle}

all_results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r: all_results.append(r); last_exit = r["exit_idx"]
rdf = pd.DataFrame(all_results)

print("=" * 110)
print("持仓时间深入分析")
print("=" * 110)
print(f"\n全量: {len(rdf)}单, 累计R={rdf['r_value'].sum():.1f}")

# ============ A. 前3根假突破特征 ============
print(f"\n{'='*110}")
print("A. 前3根假突破特征（265单在1-3根被ATR止损，亏199R）")
print(f"{'='*110}")

early_stop = rdf[rdf["hold_bars"] <= 3]
survivors = rdf[rdf["hold_bars"] > 3]

print(f"\n  前3根止损: {len(early_stop)}单, 累计R={early_stop['r_value'].sum():.1f}, 均R={early_stop['r_value'].mean():.2f}")
print(f"  挺过3根:   {len(survivors)}单, 累计R={survivors['r_value'].sum():.1f}, 均R={survivors['r_value'].mean():.2f}")

# A1. ATR%对比
print(f"\n  A1. ATR%分布对比:")
print(f"  {'':>6}{'ATR<1%':>10}{'1-1.5%':>10}{'1.5-2%':>10}{'2-3%':>10}{'3%+':>10}")
print(f"  {'前3止损':>6}", end="")
for lo, hi in [(0,0.01),(0.01,0.015),(0.015,0.02),(0.02,0.03),(0.03,1.0)]:
    cnt = ((early_stop["atr_pct"]>=lo)&(early_stop["atr_pct"]<hi)).sum()
    print(f"{cnt:>9}({cnt/len(early_stop):>3.0%})", end="")
print()
print(f"  {'挺过3根':>6}", end="")
for lo, hi in [(0,0.01),(0.01,0.015),(0.015,0.02),(0.02,0.03),(0.03,1.0)]:
    cnt = ((survivors["atr_pct"]>=lo)&(survivors["atr_pct"]<hi)).sum()
    print(f"{cnt:>9}({cnt/len(survivors):>3.0%})", end="")
print()

# A2. 偏离度对比
print(f"\n  A2. 偏离度分布对比:")
print(f"  {'':>6}{'<-5%':>10}{'-5~0%':>10}{'0~5%':>10}{'5~10%':>10}{'10%+':>10}")
print(f"  {'前3止损':>6}", end="")
for lo, hi in [(-999,-5),(-5,0),(0,5),(5,10),(10,999)]:
    cnt = ((early_stop["dev_prev"]>=lo)&(early_stop["dev_prev"]<hi)).sum()
    print(f"{cnt:>9}({cnt/len(early_stop):>3.0%})", end="")
print()
print(f"  {'挺过3根':>6}", end="")
for lo, hi in [(-999,-5),(-5,0),(0,5),(5,10),(10,999)]:
    cnt = ((survivors["dev_prev"]>=lo)&(survivors["dev_prev"]<hi)).sum()
    print(f"{cnt:>9}({cnt/len(survivors):>3.0%})", end="")
print()

# A3. 突破前3根涨幅对比
print(f"\n  A3. 突破前3根涨幅对比:")
print(f"  {'':>6}{'<2%':>10}{'2-4%':>10}{'4-6%':>10}{'6-8%':>10}{'8%+':>10}")
print(f"  {'前3止损':>6}", end="")
for lo, hi in [(-999,0.02),(0.02,0.04),(0.04,0.06),(0.06,0.08),(0.08,999)]:
    cnt = ((early_stop["pre_gain"]>=lo)&(early_stop["pre_gain"]<hi)).sum()
    print(f"{cnt:>9}({cnt/len(early_stop):>3.0%})", end="")
print()
print(f"  {'挺过3根':>6}", end="")
for lo, hi in [(-999,0.02),(0.02,0.04),(0.04,0.06),(0.06,0.08),(0.08,999)]:
    cnt = ((survivors["pre_gain"]>=lo)&(survivors["pre_gain"]<hi)).sum()
    print(f"{cnt:>9}({cnt/len(survivors):>3.0%})", end="")
print()

# A4. 突破K线实体大小
print(f"\n  A4. 突破K线实体大小（|close-open|/突破线）:")
print(f"  前3止损: 中位{early_stop['body'].median():.2%}, 均值{early_stop['body'].mean():.2%}")
print(f"  挺过3根: 中位{survivors['body'].median():.2%}, 均值{survivors['body'].mean():.2%}")

# A5. 突破K线方向
print(f"\n  A5. 突破K线收阳/收阴（顺势K线占比）:")
print(f"  前3止损: {early_stop['fav_candle'].mean():.0%} 顺势K线")
print(f"  挺过3根: {survivors['fav_candle'].mean():.0%} 顺势K线")

# A6. 多空分离
print(f"\n  A6. 多空分离:")
for d, lbl in [("long", "做多"), ("short", "做空")]:
    es = early_stop[early_stop["direction"] == d]
    sv = survivors[survivors["direction"] == d]
    print(f"  {lbl}: 前3止损{len(es)}单(均R={es['r_value'].mean():.2f}), 挺过{len(sv)}单(均R={sv['r_value'].mean():.2f})")

# ============ B. 第6根拐点分析 ============
print(f"\n{'='*110}")
print("B. 第6根拐点分析（持仓到第6根: 胜率77%, 到第9根: 98%）")
print(f"{'='*110}")

# B1. 第3/5/8根时的涨幅 vs 最终R值
print(f"\n  B1. 第N根时涨幅 vs 最终R值:")
for exc_col, bar_num in [("exc_at_3", 3), ("exc_at_5", 5), ("exc_at_8", 8)]:
    valid = rdf[rdf[exc_col].notna()]
    if valid.empty: continue
    print(f"\n  第{bar_num}根时涨幅分组（仅持仓>{bar_num}根的单）:")
    print(f"  {'涨幅区间':>10} {'单数':>5} {'最终均R':>8} {'胜率':>6} {'最终MFE中位':>12}")
    for lo, hi in [(-0.01,0.005),(0.005,0.02),(0.02,0.04),(0.04,0.06),(0.06,0.10),(0.10,999)]:
        sub = valid[(valid[exc_col]>=lo)&(valid[exc_col]<hi)]
        if sub.empty: continue
        lbl = f"[{lo:.1%},{hi:.1%})" if hi != 999 else f"[{lo:.1%},+∞)"
        print(f"  {lbl:>10} {len(sub):>5} {sub['r_value'].mean():>8.2f} {(sub['r_value']>0).mean():>5.0%} {sub['mfe'].median():>11.1%}")

# B2. 挺过前3根后，第4-6根价格行为预测最终R
print(f"\n  B2. 挺过前3根后，第4-6根涨幅 vs 最终R:")
survived_3 = rdf[(rdf["hold_bars"] > 3) & (rdf["exc_at_5"].notna())]
print(f"  {'第5根涨幅':>10} {'单数':>5} {'最终均R':>8} {'胜率':>6} {'持仓中位':>8}")
for lo, hi in [(-0.01,0.005),(0.005,0.01),(0.01,0.02),(0.02,0.04),(0.04,0.06),(0.06,999)]:
    sub = survived_3[(survived_3["exc_at_5"]>=lo)&(survived_3["exc_at_5"]<hi)]
    if sub.empty: continue
    lbl = f"[{lo:.1%},{hi:.1%})" if hi != 999 else f"[{lo:.1%},+∞)"
    print(f"  {lbl:>10} {len(sub):>5} {sub['r_value'].mean():>8.2f} {(sub['r_value']>0).mean():>5.0%} {sub['hold_bars'].median():>7.0f}")

# ============ C. MFE时机 vs 最终R值 ============
print(f"\n{'='*110}")
print("C. MFE达到时间 vs 最终R值（MFE早达到 ≠ 最终R高）")
print(f"{'='*110}")

print(f"\n{'MFE在第N根':>10} {'单数':>5} {'最终均R':>8} {'R中位':>6} {'胜率':>6} {'MFE中位':>8} {'持仓中位':>8}")
print("-" * 60)
for i in range(1, 16):
    sub = rdf[rdf["mfe_bar"] == i]
    if sub.empty: continue
    print(f"{'第'+str(i)+'根':>10} {len(sub):>5} {sub['r_value'].mean():>8.2f} {sub['r_value'].median():>6.2f} {(sub['r_value']>0).mean():>5.0%} {sub['mfe'].median():>7.1%} {sub['hold_bars'].median():>7.0f}")
sub = rdf[rdf["mfe_bar"] >= 16]
if not sub.empty:
    print(f"{'第16根+':>10} {len(sub):>5} {sub['r_value'].mean():>8.2f} {sub['r_value'].median():>6.2f} {(sub['r_value']>0).mean():>5.0%} {sub['mfe'].median():>7.1%} {sub['hold_bars'].median():>7.0f}")

# C2. MFE达到时间 × MFE大小 联合分析
print(f"\n  C2. MFE达到时间 × MFE大小 联合分析:")
print(f"  {'MFE时机':>10} {'MFE<2%':>12} {'2-6%':>12} {'6-10%':>12} {'10%+':>12}")
print("-" * 62)
for lo_bar, hi_bar, lbl in [(1,2,"第1-2根"),(3,3,"第3根"),(4,5,"第4-5根"),(6,8,"第6-8根"),(9,999,"第9根+")]:
    row = f"{lbl:>10}"
    for lo_mfe, hi_mfe in [(-999,0.02),(0.02,0.06),(0.06,0.10),(0.10,999)]:
        sub = rdf[(rdf["mfe_bar"]>=lo_bar)&(rdf["mfe_bar"]<=hi_bar)&(rdf["mfe"]>=lo_mfe)&(rdf["mfe"]<hi_mfe)]
        if sub.empty:
            row += f" {'—':>12}"
        else:
            mean_r = sub["r_value"].mean()
            row += f" {f'{len(sub)}单/R={mean_r:.1f}':>12}"
    print(row)

# ============ D. 能否事前识别前3根止损单？ ============
print(f"\n{'='*110}")
print("D. 能否事前识别前3根止损单？（组合特征过滤）")
print(f"{'='*110}")

# D1. ATR% × 偏离度 × 前3根涨幅 联合
print(f"\n  D1. 前3根止损率 by ATR%:")
for lo, hi in [(0,0.01),(0.01,0.015),(0.015,0.02),(0.02,0.03),(0.03,1.0)]:
    sub = rdf[(rdf["atr_pct"]>=lo)&(rdf["atr_pct"]<hi)]
    if sub.empty: continue
    es = sub[sub["hold_bars"] <= 3]
    print(f"  ATR[{lo:.1%},{hi:.1%}): {len(es)}/{len(sub)} = {len(es)/len(sub):.0%} 前3止损, 这些单累计R={es['r_value'].sum():.1f}")

print(f"\n  D2. 前3根止损率 by 偏离度:")
for lo, hi in [(-999,-5),(-5,0),(0,5),(5,10),(10,999)]:
    sub = rdf[(rdf["dev_prev"]>=lo)&(rdf["dev_prev"]<hi)]
    if sub.empty: continue
    es = sub[sub["hold_bars"] <= 3]
    print(f"  偏离[{lo:.0f}%,{hi:.0f}%): {len(es)}/{len(sub)} = {len(es)/len(sub):.0%} 前3止损, 这些单累计R={es['r_value'].sum():.1f}")

print(f"\n  D3. 前3根止损率 by 突破前3根涨幅:")
for lo, hi in [(-999,0.02),(0.02,0.04),(0.04,0.06),(0.06,0.08),(0.08,999)]:
    sub = rdf[(rdf["pre_gain"]>=lo)&(rdf["pre_gain"]<hi)]
    if sub.empty: continue
    es = sub[sub["hold_bars"] <= 3]
    print(f"  前3涨幅[{lo:.0%},{hi:.0%}): {len(es)}/{len(sub)} = {len(es)/len(sub):.0%} 前3止损, 这些单累计R={es['r_value'].sum():.1f}")

# D4. 组合：ATR>2% + 偏离度[-5%,10%) → 前3止损率
print(f"\n  D4. 高风险组合的前3止损率:")
combos = [
    ("ATR>2% + 偏离[-5%,10%)", (rdf["atr_pct"]>=0.02) & (rdf["dev_prev"]>=-5) & (rdf["dev_prev"]<10)),
    ("ATR>2% + 前3涨幅<4%", (rdf["atr_pct"]>=0.02) & (rdf["pre_gain"]<0.04)),
    ("ATR>1.5% + 偏离[-5%,5%)", (rdf["atr_pct"]>=0.015) & (rdf["dev_prev"]>=-5) & (rdf["dev_prev"]<5)),
    ("ATR>2% + 偏离[-5%,10%) + 前3涨幅<4%", (rdf["atr_pct"]>=0.02) & (rdf["dev_prev"]>=-5) & (rdf["dev_prev"]<10) & (rdf["pre_gain"]<0.04)),
]
for label, mask in combos:
    sub = rdf[mask]
    if sub.empty: continue
    es = sub[sub["hold_bars"] <= 3]
    sv = sub[sub["hold_bars"] > 3]
    print(f"  {label}:")
    print(f"    总{len(sub)}单, 前3止损{len(es)}单({len(es)/len(sub):.0%}), 累计R={sub['r_value'].sum():.1f}")
    print(f"    前3止损累计R={es['r_value'].sum():.1f}, 挺过3根累计R={sv['r_value'].sum():.1f}")

print(f"\n{'='*110}")
