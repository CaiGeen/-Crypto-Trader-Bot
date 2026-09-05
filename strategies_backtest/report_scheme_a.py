"""方案A完整实盘报告：3K突破 + 复合过滤（[-2%,6%)全要；[6%,15%)要前3K<0且ATR%<1.8）。
输出：终端完整统计 + 明细CSV（中文列名，时间降序）。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
df_4h = load_klines(cfg)
kama_vals = kama(df_4h["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy()
n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy()
times = df_4h["open_time"].tolist()

daily_df = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1d.parquet")
k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
daily_df["date"] = daily_df["open_time"].dt.date
date_to_ema = dict(zip(daily_df["date"], de70))
ema_slope5 = np.zeros(len(daily_df))
for i in range(5, len(daily_df)):
    ema_slope5[i] = (de70[i] - de70[i-5]) / de70[i-5] * 100
date_to_slope = dict(zip(daily_df["date"], ema_slope5))

def get_dev_slope(i):
    if i - 1 < 0: return np.nan, np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    slope_prev = date_to_slope.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan, np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100, slope_prev

df_1m = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1m.parquet")
ms_1m = df_1m["open_time_ms"].to_numpy()
h1m = df_1m["high"].to_numpy(); l1m = df_1m["low"].to_numpy()
ms_4h = df_4h["open_time_ms"].to_numpy()
four_h_ms = 4 * 60 * 60 * 1000
min_start_idx = np.searchsorted(ms_1m, ms_4h)
min_end_idx = np.searchsorted(ms_1m, ms_4h + four_h_ms)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, 20)
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3

def disambiguate_1m(j, sl, tp_price, is_long):
    s = min_start_idx[j]; e = min_end_idx[j]
    if s >= e: return None
    for k in range(s, e):
        if is_long:
            if l1m[k] < sl: return ("stop", float(sl))
            if h1m[k] >= tp_price: return ("tp", float(tp_price))
        else:
            if h1m[k] > sl: return ("stop", float(sl))
            if l1m[k] <= tp_price: return ("tp", float(tp_price))
    return None

def find_callback_extreme(i, direction, window):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n4h: continue
        if direction == "long":
            if highs[j+1] <= highs[j] and highs[j+2] <= highs[j]: return highs[j], j
        else:
            if lows[j+1] >= lows[j] and lows[j+2] >= lows[j]: return lows[j], j
    return None, None

def simulate(signal):
    i = signal.idx
    if i + 1 >= n4h: return None
    bt_nk = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None
    cb_extreme, cb_idx = find_callback_extreme(i, d, WINDOW)
    has_cb = cb_extreme is not None
    entry_base = cb_extreme if has_cb else bt_nk
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)
    fill_price = float(entry_base) if filled else 0.0
    dev, ema_slope = get_dev_slope(i)
    pre_3k_pct = np.nan
    if i - 4 >= 0 and closes[i-4] > 0:
        raw = (closes[i-1] - closes[i-4]) / closes[i-4] * 100
        pre_3k_pct = raw if is_long else -raw
    atr_pct = av / closes[i-1] * 100 if i-1 >= 0 else np.nan
    body_atr = np.nan
    if av > 0:
        raw = (closes[i] - opens[i]) / av
        body_atr = raw if is_long else -raw
    if not filled: return None
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {"dir": d, "exit_idx": i, "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                "confirmed": False, "addon": False, "fill_price": fill_price, "exit_price": ep,
                "year": int(years[i]), "sig_idx": i, "dev": dev, "atr": av, "atr_pct": atr_pct,
                "ema_slope": ema_slope, "pre_3k_pct": pre_3k_pct, "body_atr": body_atr,
                "has_cb": has_cb, "init_stop": 0, "tp_price": 0}
    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None; ambig = False
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = init_stop if bh <= 3 else (max(ks, fill_price) if is_long else min(ks, fill_price))
        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j])
        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)
        if hit_stop and hit_tp:
            ambig = True
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep = result
                if reason_1m == "stop":
                    exit_idx = j; exit_price = ep
                    exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
                else:
                    exit_idx = j; exit_price = ep; exit_reason = "止盈"
                break
            ep = float(sl); exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_stop:
            ep = float(sl); exit_idx = j; exit_price = ep
            exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); exit_reason = "止盈"; break
    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1]); exit_reason = "未平仓"
    r1 = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "r": r1+r2, "hold": exit_idx-i, "exit_reason": exit_reason,
            "confirmed": True, "addon": addon_done, "fill_price": fill_price, "exit_price": exit_price,
            "year": int(years[i]), "sig_idx": i, "dev": dev, "atr": av, "atr_pct": atr_pct,
            "ema_slope": ema_slope, "pre_3k_pct": pre_3k_pct, "body_atr": body_atr,
            "has_cb": has_cb, "init_stop": init_stop, "tp_price": tp_price, "ambig": ambig}

# 方案A过滤：[-2%,6%)全要；[6%,15%)要前3K<0且ATR%<1.8
def passes(r):
    if np.isnan(r["dev"]): return False
    if -2 <= r["dev"] < 6: return True
    if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.8: return True
    return False

signals = detect_breakout(df_4h, WINDOW)
results = []; last_exit = -1; skipped = 0
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r:
        if passes(r):
            results.append(r)
            last_exit = r["exit_idx"]
        else:
            skipped += 1

rdf = pd.DataFrame(results)
sr = rdf["r"]
w = sr[sr > 0]; l = sr[sr <= 0]
pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
pf_str = f"{pf:.1f}" if pf < 1000 else "inf"

print("=" * 110)
print("方案A完整实盘报告：3K突破 + 复合过滤")
print("=" * 110)
print(f"过滤规则：[-2%,6%)偏离度全要；[6%,15%)要前3K方向涨幅<0 且 ATR%<1.8")
print(f"配置：3K突破+回调基准+0.5ATR确认+1ATR止损+1.5ATR加仓+做多22%/做空12%止盈+1min消歧")
print(f"数据：{df_4h['open_time'].iloc[0]} ~ {df_4h['open_time'].iloc[-1]}，{n4h}根4H K线")
print(f"原始信号：{len(signals)}，成交并过滤通过：{len(rdf)}单，被过滤跳过：{skipped}单（不占last_exit）")
print()

# 1. 总览
print(f"{'━'*110}")
print("一、总览")
print(f"{'━'*110}")
rdf_s = rdf.sort_values("sig_idx"); cum = rdf_s["r"].cumsum()
max_dd = (cum - cum.cummax()).min()
print(f"  累计R={sr.sum():.1f}  均R={sr.mean():.2f}  胜率={(sr>0).mean():.0%}  PF={pf_str}  最大回撤={max_dd:.1f}R")
print(f"  歧义波段: {rdf['ambig'].sum()} 个  加仓单: {rdf['addon'].sum()} 个")

# 2. 多空分开
print(f"\n{'━'*110}")
print("二、多空分开")
print(f"{'━'*110}")
print(f"{'方向':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'大赚(≥5R)':>10} {'大亏(≤-1R)':>10}")
print("-" * 60)
for d, lbl in [("long", "做多"), ("short", "做空")]:
    sub = rdf[rdf["dir"] == d]
    if sub.empty: continue
    sr2 = sub["r"]
    big_win = (sr2 >= 5).sum()
    big_loss = (sr2 <= -1).sum()
    print(f"{lbl:>6} {len(sub):>6} {sr2.sum():>8.1f} {sr2.mean():>6.2f} {(sr2>0).mean():>5.0%} {big_win:>10} {big_loss:>10}")

# 3. 出场方式
print(f"\n{'━'*110}")
print("三、出场方式分布")
print(f"{'━'*110}")
vc = rdf["exit_reason"].value_counts()
for reason, cnt in vc.items():
    sub = rdf[rdf["exit_reason"] == reason]
    print(f"  {reason:>10}: {cnt:>5}单 ({cnt/len(rdf):.0%})  累计R={sub['r'].sum():>7.1f}  均R={sub['r'].mean():>6.2f}")

# 4. 过滤贡献分析
print(f"\n{'━'*110}")
print("四、过滤条件贡献分析")
print(f"{'━'*110}")
core = rdf[(rdf["dev"] >= -2) & (rdf["dev"] < 6)]
ext = rdf[(rdf["dev"] >= 6) & (rdf["dev"] < 15)]
print(f"  核心[-2%,6%): {len(core)}单  累计R={core['r'].sum():.1f}  均R={core['r'].mean():.2f}  胜率={(core['r']>0).mean():.0%}")
if not ext.empty:
    print(f"  扩展[6%,15%): {len(ext)}单  累计R={ext['r'].sum():.1f}  均R={ext['r'].mean():.2f}  胜率={(ext['r']>0).mean():.0%}")
    print(f"  扩展贡献: +{ext['r'].sum():.1f}R ({len(ext)}单)")

# 5. 年度分解
print(f"\n{'━'*110}")
print("五、年度分解")
print(f"{'━'*110}")
print(f"{'年份':>6} {'单数':>6} {'累计R':>8} {'均R':>6} {'胜率':>6} {'做多R':>8} {'做空R':>8} {'最大回撤R':>10}")
print("-" * 65)
for yr in sorted(rdf["year"].unique()):
    sub = rdf[rdf["year"] == yr]
    sr2 = sub["r"]
    lr = sub[sub["dir"]=="long"]["r"].sum()
    srr = sub[sub["dir"]=="short"]["r"].sum()
    cum_y = sub.sort_values("sig_idx")["r"].cumsum()
    mdd = (cum_y - cum_y.cummax()).min()
    print(f"{yr:>6} {len(sub):>6} {sr2.sum():>8.1f} {sr2.mean():>6.2f} {(sr2>0).mean():>5.0%} {lr:>8.1f} {srr:>8.1f} {mdd:>10.1f}")
print(f"{'合计':>6} {len(rdf):>6} {sr.sum():>8.1f} {sr.mean():>6.2f} {(sr>0).mean():>5.0%} {rdf[rdf['dir']=='long']['r'].sum():>8.1f} {rdf[rdf['dir']=='short']['r'].sum():>8.1f} {max_dd:>10.1f}")

# 6. R值分布
print(f"\n{'━'*110}")
print("六、R值分布")
print(f"{'━'*110}")
print(f"{'R区间':>12} {'单数':>6} {'占比':>6} {'累计R':>8} {'均R':>6}")
print("-" * 45)
for lo, hi in [(-3,-2),(-2,-1),(-1,0),(0,1),(1,2),(2,5),(5,100)]:
    sub = rdf[(rdf["r"] >= lo) & (rdf["r"] < hi)]
    if sub.empty: continue
    print(f"[{lo}, {hi}):{'':>4} {len(sub):>6} {len(sub)/len(rdf):>5.0%} {sub['r'].sum():>8.1f} {sub['r'].mean():>6.2f}")

# 7. 导出CSV（时间降序）
rows = []
for idx, r in rdf.reset_index(drop=True).iterrows():
    i = int(r["sig_idx"])
    sig_t = pd.Timestamp(times[i]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    exit_t = pd.Timestamp(times[int(r["exit_idx"])]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    rows.append({"序号": idx+1, "信号时间": sig_t, "方向": "做多" if r["dir"]=="long" else "做空",
        "成交4H开盘价": round(opens[i],2), "成交价": round(r["fill_price"],2),
        "ATR": round(r["atr"],2), "ATR%": round(r["atr_pct"],3),
        "EMA70偏离度%": round(r["dev"],2),
        "前3K涨幅%": round(r["pre_3k_pct"],2) if not np.isnan(r["pre_3k_pct"]) else "",
        "EMA斜率%": round(r["ema_slope"],2) if not np.isnan(r["ema_slope"]) else "",
        "突破实体/ATR": round(r["body_atr"],2) if not np.isnan(r["body_atr"]) else "",
        "入场基准": "回调极值" if r["has_cb"] else "突破K极值",
        "是否确认": "确认" if r["confirmed"] else "弱突破",
        "初始止损": round(r["init_stop"],2) if r["init_stop"] else "",
        "止盈价": round(r["tp_price"],2) if r["tp_price"] else "",
        "出场时间": exit_t, "出场价": round(r["exit_price"],2),
        "持仓K数": int(r["hold"]), "R值": round(r["r"],3),
        "是否加仓": "加仓" if r["addon"] else "",
        "出场原因": r["exit_reason"], "年份": int(r["year"])})

out_df = pd.DataFrame(rows)
out_df = out_df.sort_values("信号时间", ascending=False).reset_index(drop=True)
out_df["序号"] = range(1, len(out_df) + 1)
csv_path = cfg.results_path / "方案A_3K复合过滤_完整报告.csv"
out_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
print(f"\n{'━'*110}")
print(f"CSV已导出: {csv_path}")
print(f"  {len(out_df)}行 × {len(out_df.columns)}列 (时间降序)")
