"""开仓限制规则对比：B(回调突破线·原合并) vs C(回调突破线·新开仓限制)。

新规则：触发买入后，必须满足以下任一条件才能再开新仓：
  1) 该仓位打损出场（ATR止损）→ 出场后立即解锁；
  2) 买入后第3根K线收盘后（signal bar i 的 i+3 收盘，即 i+4 起可开新仓）。
与原合并逻辑（持仓期间全部跳过、出场即解锁）的差异：
  - 持仓超过3K的仓位不再阻挡新信号（3K收盘后允许并发仓位）；
  - 3K内非打损出场（提前止盈等罕见情形）需等到第3K收盘才解锁。
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

def simulate(signal):
    """回调突破线策略单笔回测（与 callback_breakout_ab.py 的B组完全一致）。"""
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb, _ = find_callback_line(i, d, cfg.window)
    base = cb if cb is not None else bt

    if is_long: init_stop = base - av; tp_price = base * (1 + TP_LONG)
    else: init_stop = base + av; tp_price = base * (1 - TP_SHORT)

    mfe = -np.inf; exit_idx = None; ep = None; reason = None
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
    if exit_idx is None: exit_idx = n-1; ep = float(closes[-1]); reason = "end"
    if mfe == -np.inf: mfe = 0.0

    r = (ep - base)/av if is_long else (base - ep)/av
    ret = (ep - base)/base if is_long else (base - ep)/base
    return {"r": r, "ret": ret, "signal_idx": i, "exit_idx": exit_idx, "hold": exit_idx - i,
            "year": int(dates[exit_idx]), "dir": d, "exit_reason": reason,
            "base": float(base), "bt": float(bt), "atr": float(av), "mfe": float(mfe),
            "exit_price": float(ep)}

def run_original_merge(require_callback=False):
    """原合并：持仓期间跳过，出场后解锁。require_callback=True时无回调线信号不成交。"""
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        if require_callback:
            cb, _ = find_callback_line(s.idx, s.direction, cfg.window)
            if cb is None: continue
        r = simulate(s)
        if r: results.append(r); last_exit = r["exit_idx"]
    return results

def run_gated(require_callback=False):
    """新规则：打损即解锁；否则等 signal bar+3 收盘（i+4起）解锁——与是否已出场无关。"""
    results = []; gate = 0  # idx >= gate 才允许开仓
    for s in signals:
        if s.idx < gate: continue
        if require_callback:
            cb, _ = find_callback_line(s.idx, s.direction, cfg.window)
            if cb is None: continue
        r = simulate(s)
        if r is None: continue
        results.append(r)
        i = r["signal_idx"]; j = r["exit_idx"]
        # 解锁 = min(第3K收盘后 i+4, 打损出场后 j+1)
        if r["exit_reason"] == "atr_stop" and j + 1 < i + 4:
            gate = j + 1
        else:
            gate = i + 4
    return results

res_b = run_original_merge()
res_c = run_gated()
res_bs = run_original_merge(require_callback=True)
res_cs = run_gated(require_callback=True)
bdf = pd.DataFrame(res_b); cdf = pd.DataFrame(res_c)
bsdf = pd.DataFrame(res_bs); csdf = pd.DataFrame(res_cs)

def show(label, rdf):
    r = rdf["r"]
    print(f"\n【{label}】 单数={len(rdf)} 累计R={r.sum():.1f} 均R={r.mean():.2f} "
          f"胜率={(r>0).mean():.0%} 累计收益={rdf['ret'].sum():.1%} 持仓中位={rdf['hold'].median():.0f}根")
    for d, lbl in [("long", "做多"), ("short", "做空")]:
        sub = rdf[rdf["dir"] == d]
        if sub.empty: continue
        sr = sub["r"]
        print(f"  {lbl}: {len(sub)}单 累计R={sr.sum():.1f} 均R={sr.mean():.2f} 胜率={(sr>0).mean():.0%}")
    vc = rdf["exit_reason"].value_counts()
    print("  出场:", "  ".join(f"{k}={v}({v/len(rdf):.0%})" for k, v in vc.items()))

print("=" * 100)
print("开仓限制对比：B=回调突破线·原合并 vs C=回调突破线·新规则(打损或3K收盘后才可再开仓)")
print("严格模式：无回调线的信号不成交（不退回6K极值）")
print("=" * 100)
show("B. 原合并（持仓期间全跳过）", bdf)
show("C. 新规则（打损即解锁 / 3K收盘后解锁）", cdf)
show("B严格. 原合并 + 无回调线不开仓", bsdf)
show("C严格. 新规则 + 无回调线不开仓", csdf)

# 无回调线成交单的影响（宽松模式下退回6K极值成交的单子）
no_cb_b = bdf[bdf["base"] == bdf["bt"]]
# base==bt 可能是退回，也可能是回调线恰好等于极值；用逐信号查证
def count_no_cb(rdf):
    cnt = 0; rsum = 0.0
    for _, t in rdf.iterrows():
        cb, _ = find_callback_line(int(t["signal_idx"]), t["dir"], cfg.window)
        if cb is None: cnt += 1; rsum += t["r"]
    return cnt, rsum
cnt_b, r_b = count_no_cb(bdf); cnt_c, r_c = count_no_cb(cdf)
print(f"\n无回调线但成交的单（宽松模式退回6K极值）: B组{cnt_b}单/{r_b:+.1f}R, C组{cnt_c}单/{r_c:+.1f}R")

# 并发仓位统计（C组：开仓时上一仓仍未出场）
open_exits = []
for k in range(1, len(res_c)):
    if res_c[k]["signal_idx"] <= res_c[k-1]["exit_idx"]:
        open_exits.append(res_c[k])
conc = len(open_exits)
print(f"C组并发开仓（上一仓仍持有时开新仓）: {conc}单 ({conc/len(res_c):.0%})，其累计R={sum(x['r'] for x in open_exits):.1f}")

# 年度对比
print(f"\n{'年份':>6} {'B累计R':>10} {'C累计R':>10} {'B严R':>10} {'C严R':>10}")
print("-" * 52)
for yr in sorted(set(bdf["year"]) | set(cdf["year"]) | set(csdf["year"])):
    b = bdf[bdf["year"] == yr]["r"].sum(); c = cdf[cdf["year"] == yr]["r"].sum()
    bs = bsdf[bsdf["year"] == yr]["r"].sum() if yr in bsdf["year"].values else 0.0
    cs = csdf[csdf["year"] == yr]["r"].sum() if yr in csdf["year"].values else 0.0
    print(f"{yr:>6} {b:>10.1f} {c:>10.1f} {bs:>10.1f} {cs:>10.1f}")
print(f"{'合计':>6} {bdf['r'].sum():>10.1f} {cdf['r'].sum():>10.1f} {bsdf['r'].sum():>10.1f} {csdf['r'].sum():>10.1f}")

# 导出自查明细
times = df["open_time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M").to_numpy()
def to_cn(reason):
    return {"atr_stop": "ATR止损", "breakeven": "保本平仓", "kama_trail": "KAMA跟踪",
            "take_profit": "固定止盈", "end": "数据末尾"}.get(reason, reason)
rows = []
for label, rdf in [("B_原合并", bdf), ("C_开仓限制", cdf),
                   ("B严格_无回调线不开仓", bsdf), ("C严格_无回调线不开仓", csdf)]:
    for k, (_, t) in enumerate(rdf.iterrows(), start=1):
        i = int(t["signal_idx"])
        cb_raw, _ = find_callback_line(i, t["dir"], cfg.window)
        bt = t["bt"]; base = t["base"]; is_long = (t["dir"] == "long")
        off = 0.0 if cb_raw is None else ((bt - cb_raw)/bt*100 if is_long else (cb_raw - bt)/bt*100)
        rows.append({
            "序号": k, "组别": label,
            "信号时间": times[i], "方向": "做多" if is_long else "做空",
            "6K极值": round(bt, 2), "回调突破线": round(base, 2),
            "是否有回调线": "是" if cb_raw is not None else "否（退回6K极值成交）",
            "回调偏移%": round(off, 3),
            "ATR": round(t["atr"], 2),
            "入场价": round(base, 2),
            "初始止损": round(base - t["atr"] if is_long else base + t["atr"], 2),
            "止盈目标": round(base*(1+TP_LONG) if is_long else base*(1-TP_SHORT), 2),
            "出场时间": times[int(t["exit_idx"])], "出场价": round(t["exit_price"], 2),
            "持仓K数": t["hold"], "R值": round(t["r"], 4), "收益率%": round(t["ret"]*100, 3),
            "MFE%": round(t["mfe"]*100, 3), "出场原因": to_cn(t["exit_reason"]), "年份": t["year"],
        })
out = pd.DataFrame(rows)
out = out.sort_values(["组别", "信号时间"], ascending=[True, False]).reset_index(drop=True)
out["序号"] = range(1, len(out) + 1)
out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "回调突破线_开仓限制对比.csv")
out.to_csv(out_path, index=False, encoding="utf-8-sig")
print(f"\n自查明细已导出: {out_path}（{len(out)}行 = B{len(bdf)} + C{len(cdf)} + B严格{len(bsdf)} + C严格{len(csdf)}）")
