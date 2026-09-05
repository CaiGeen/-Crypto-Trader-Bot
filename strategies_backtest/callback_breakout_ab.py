"""回调突破线 A/B 对比：把策略基准从 6K极值突破线 整体替换为 回调突破线。

回调突破线定义（6K窗口内，从左到右找最早的确认点）：
  做多：highs[j+1] <= highs[j] 且 highs[j+2] <= highs[j] → highs[j] 为回调突破线
  做空：lows[j+1] >= lows[j] 且 lows[j+2] >= lows[j] → lows[j] 为回调突破线
  找不到则退回 6K极值。

入场/初始止损/止盈/保本/R值 全部以该基准计算，其余规则不变：
  1×ATR初始止损、第3K保本+KAMA跟踪、止盈多22%/空12%、开盘价出场、信号合并。
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
TP_LONG, TP_SHORT = 0.22, 0.12

def find_callback_line(i, direction, window=6):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]:
                return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] >= lows[j]:
                return lows[j], j
    return None, None

def simulate(signal, use_callback):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    if use_callback:
        cb, _ = find_callback_line(i, d, cfg.window)
        base = cb if cb is not None else bt
    else:
        base = bt

    if is_long:
        init_stop = base - av; tp_price = base * (1 + TP_LONG)
    else:
        init_stop = base + av; tp_price = base * (1 - TP_SHORT)

    reason = None; exit_idx = None; ep = None
    mfe = -np.inf
    for j in range(i + 1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        if is_long: mfe = max(mfe, (highs[j] - base) / base)
        else: mfe = max(mfe, (base - lows[j]) / base)
        bh = j - i
        if bh <= 3: sl = init_stop; st = "atr_stop"
        else:
            if is_long: sl = max(ks, base); st = "breakeven" if ks < base else "kama_trail"
            else: sl = min(ks, base); st = "breakeven" if ks > base else "kama_trail"
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl); reason = st; exit_idx = j; break
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl); reason = st; exit_idx = j; break
        if is_long and highs[j] >= tp_price:
            ep = float(tp_price); reason = "take_profit"; exit_idx = j; break
        if not is_long and lows[j] <= tp_price:
            ep = float(tp_price); reason = "take_profit"; exit_idx = j; break
    if exit_idx is None:
        exit_idx = n-1; ep = float(closes[-1]); reason = "end"
    if mfe == -np.inf: mfe = 0.0

    r = (ep - base)/av if is_long else (base - ep)/av
    ret = (ep - base)/base if is_long else (base - ep)/base
    return {"r": r, "ret": ret, "exit_idx": exit_idx, "hold": exit_idx - i,
            "year": int(dates[exit_idx]), "dir": d, "exit_reason": reason,
            "base": float(base), "bt": float(bt), "signal_idx": i,
            "atr": float(av), "mfe": float(mfe), "exit_price": float(ep)}

def run(use_callback):
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s, use_callback)
        if r: results.append(r); last_exit = r["exit_idx"]
    return pd.DataFrame(results)

base_df = run(False)
cb_df = run(True)

print("=" * 100)
print("A/B 对比：6K极值突破线 vs 回调突破线（其余规则完全一致，开盘价出场）")
print("=" * 100)

def show(label, rdf):
    r = rdf["r"]
    print(f"\n【{label}】 单数={len(rdf)} 累计R={r.sum():.1f} 均R={r.mean():.2f} "
          f"胜率={(r>0).mean():.0%} 累计收益={rdf['ret'].sum():.1%} 持仓中位={rdf['hold'].median():.0f}根")
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = rdf[rdf["dir"] == d]
        if sub.empty: continue
        sr = sub["r"]
        print(f"  {lbl}: {len(sub)}单 累计R={sr.sum():.1f} 均R={sr.mean():.2f} "
              f"胜率={(sr>0).mean():.0%} 收益={sub['ret'].sum():.1%}")
    vc = rdf["exit_reason"].value_counts()
    print("  出场:", "  ".join(f"{k}={v}({v/len(rdf):.0%})" for k, v in vc.items()))

show("A. 原策略（6K极值突破线）", base_df)
show("B. 新策略（回调突破线）", cb_df)

# 回调线覆盖率与偏移
cov = 0; offs = []
for _, row in cb_df.iterrows():
    pass
sig_map = {}
for s in signals:
    cb, _ = find_callback_line(s.idx, s.direction, cfg.window)
    sig_map[s.idx] = cb
found = sum(1 for v in sig_map.values() if v is not None)
offs = []
for s in signals:
    cb = sig_map[s.idx]
    if cb is None: continue
    if s.direction == "long": offs.append((s.breakthrough - cb)/s.breakthrough*100)
    else: offs.append((cb - s.breakthrough)/s.breakthrough*100)
offs = np.array(offs)
print(f"\n回调突破线覆盖: {found}/{len(signals)} ({found/len(signals):.0%})，无回调线时退回6K极值")
print(f"偏移(极值-回调线)/极值: 均值={offs.mean():.3f}% 中位={np.median(offs):.3f}% 范围=[{offs.min():.3f}%, {offs.max():.3f}%]")

# 年度对比
print(f"\n{'年份':>6} {'A累计R':>10} {'B累计R':>10} {'B-A':>8}")
print("-" * 40)
for yr in sorted(set(base_df["year"]) | set(cb_df["year"])):
    a = base_df[base_df["year"] == yr]["r"].sum()
    b = cb_df[cb_df["year"] == yr]["r"].sum()
    print(f"{yr:>6} {a:>10.1f} {b:>10.1f} {b-a:>+8.1f}")
print(f"{'合计':>6} {base_df['r'].sum():>10.1f} {cb_df['r'].sum():>10.1f} {cb_df['r'].sum()-base_df['r'].sum():>+8.1f}")

# R分布对比
print(f"\n{'R区间':>8} {'A单数':>8} {'B单数':>8}")
for lo, hi, tag in [(-99,-1,"<-1"),(-1,0,"-1~0"),(0,1,"0~1"),(1,2,"1~2"),(2,5,"2~5"),(5,99,"5+")]:
    a = ((base_df["r"]>=lo)&(base_df["r"]<hi)).sum()
    b = ((cb_df["r"]>=lo)&(cb_df["r"]<hi)).sum()
    print(f"{tag:>8} {a:>8} {b:>8}")

# ---- 导出自查明细CSV ----
def to_cn(reason):
    return {"atr_stop": "ATR止损", "breakeven": "保本平仓", "kama_trail": "KAMA跟踪",
            "take_profit": "固定止盈", "end": "数据末尾"}.get(reason, reason)

times = df["open_time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M").to_numpy()
a_r_by_signal = dict(zip(base_df["signal_idx"], base_df["r"]))
a_exit_by_signal = dict(zip(base_df["signal_idx"], base_df["exit_idx"]))
a_hold_by_signal = dict(zip(base_df["signal_idx"], base_df["hold"]))

rows = []
for label, rdf, other_r in [("A_原策略_6K极值", base_df, None), ("B_新策略_回调突破线", cb_df, a_r_by_signal)]:
    for k, (_, t) in enumerate(rdf.iterrows(), start=1):
        i = t["signal_idx"]; d = t["dir"]; is_long = (d == "long")
        bt = t["bt"]; cb_line = t["base"]
        cb_raw, _ = find_callback_line(i, d, cfg.window)
        has_cb = "是" if cb_raw is not None else "否"
        if cb_raw is None:
            off = 0.0
        elif is_long:
            off = (bt - cb_raw) / bt * 100
        else:
            off = (cb_raw - bt) / bt * 100
        if is_long:
            init_stop = t["base"] - t["atr"]; tp_price = t["base"] * (1 + TP_LONG)
        else:
            init_stop = t["base"] + t["atr"]; tp_price = t["base"] * (1 - TP_SHORT)
        paired_r = ""
        if other_r is not None and i in other_r:
            paired_r = round(other_r[i], 4)
        rows.append({
            "序号": k, "组别": label,
            "信号时间": times[i], "方向": "做多" if is_long else "做空",
            "6K极值": round(bt, 2), "入场基准": round(t["base"], 2),
            "是否有回调突破线": has_cb, "回调偏移%": round(off, 3),
            "ATR": round(t["atr"], 2), "ATR%": round(t["atr"]/bt*100, 3),
            "突破K收盘价": round(float(closes[i]), 2),
            "入场价": round(t["base"], 2), "初始止损": round(init_stop, 2), "止盈目标": round(tp_price, 2),
            "出场时间": times[t["exit_idx"]], "出场价": round(t["exit_price"], 2),
            "持仓K数": t["hold"], "R值": round(t["r"], 4), "收益率%": round(t["ret"]*100, 3),
            "MFE%": round(t["mfe"]*100, 3), "出场原因": to_cn(t["exit_reason"]),
            "同信号对方R值": paired_r, "年份": t["year"],
        })

out = pd.DataFrame(rows)
out = out.sort_values(["组别", "信号时间"], ascending=[True, False]).reset_index(drop=True)
out["序号"] = range(1, len(out) + 1)
out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "回调突破线_AB自查明细.csv")
out.to_csv(out_path, index=False, encoding="utf-8-sig")
print(f"\n自查明细已导出: {out_path}（{len(out)}行 = A组{len(base_df)} + B组{len(cb_df)}）")
