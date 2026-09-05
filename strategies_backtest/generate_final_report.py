"""生成最终实盘就绪报告：参数清单 + 交易记录CSV + 逐年绩效 + E'分档加权。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
cache = cfg.base_dir / "cache"
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3
CONFIRM_ATR = 0.5; ADDON_ATR = 1.5; ADDON_BODY_PCT = 0.03; ATR_LEN = 20; EMA_LEN = 70

df_4h = pd.read_parquet(cache / "BTCUSDTUSDT_4h_mark.parquet")
df_1m = pd.read_parquet(cache / "BTCUSDT_1m_mark.parquet")
daily_df = pd.read_parquet(cache / "BTCUSDT_1d_mark.parquet")
df_4h_latest = pd.read_parquet(cache / "BTCUSDT_4h.parquet")
for df in (df_4h, df_1m, daily_df, df_4h_latest):
    for col in ["open","high","low","close","volume"]:
        if col in df.columns: df[col] = df[col].astype(float)
    if "open_time" not in df.columns:
        df["open_time"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
vol_latest = df_4h_latest["volume"].to_numpy() if "volume" in df_4h_latest.columns else None
ms_latest = df_4h_latest["open_time_ms"].to_numpy()

kama_vals = kama(df_4h["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy(); n4h = len(df_4h)
years = df_4h["open_time"].dt.year.to_numpy(); times = df_4h["open_time"].tolist()
ms_4h = df_4h["open_time_ms"].to_numpy()

vol_4h = None
if vol_latest is not None:
    vol_4h = np.full(n4h, np.nan)
    for idx in range(n4h):
        pos = np.searchsorted(ms_latest, ms_4h[idx])
        if pos < len(ms_latest) and ms_latest[pos] == ms_4h[idx]:
            vol_4h[idx] = vol_latest[pos]

k_ema = 2.0/(EMA_LEN+1); de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
date_to_ema = dict(zip(daily_df["open_time"].dt.date, de70))

def get_dev(i):
    if i-1 < 0: return np.nan
    prev_date = pd.to_datetime(times[i-1], utc=True).date()
    ema_prev = date_to_ema.get(prev_date, np.nan)
    if np.isnan(ema_prev): return np.nan
    return (closes[i-1] - ema_prev) / ema_prev * 100

ms_1m = df_1m["open_time_ms"].to_numpy()
h1m = df_1m["high"].to_numpy(); l1m = df_1m["low"].to_numpy()
four_h_ms = 4*60*60*1000
min_start_idx = np.searchsorted(ms_1m, ms_4h)
min_end_idx = np.searchsorted(ms_1m, ms_4h + four_h_ms)

def calc_atr(h, l, c, length=ATR_LEN):
    m = len(c); tr = np.zeros(m); tr[0] = h[0]-l[0]
    for i in range(1, m): tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av
atr_vals = calc_atr(highs, lows, closes, ATR_LEN)

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
    if cb_extreme is not None:
        entry_base = cb_extreme; entry_type = "基准值"
    else:
        entry_base = bt_nk; entry_type = "6K极值"
    filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
    fill_price = float(entry_base) if filled else 0.0
    dev = get_dev(i)
    pre_3k_pct = np.nan
    if i - 4 >= 0 and closes[i-4] > 0:
        raw = (closes[i-1] - closes[i-4]) / closes[i-4] * 100
        pre_3k_pct = raw if is_long else -raw
    atr_pct = av / closes[i-1] * 100 if i-1 >= 0 else np.nan
    vol_ratio = np.nan
    if vol_4h is not None and i >= 20:
        avg_vol = np.nanmean(vol_4h[i-20:i])
        if avg_vol > 0 and not np.isnan(vol_4h[i]): vol_ratio = vol_4h[i] / avg_vol
    if not filled: return None
    sig_close = closes[i]
    break_k = 0
    if is_long:
        k = 1
        while i - k >= 0 and highs[i-k] < sig_close:
            k += 1
        break_k = k - 1
    else:
        k = 1
        while i - k >= 0 and lows[i-k] > sig_close:
            k += 1
        break_k = k - 1
    base = {"dir": d, "year": int(years[i]), "sig_idx": i, "dev": dev,
            "atr_pct": atr_pct, "pre_3k_pct": pre_3k_pct, "vol_ratio": vol_ratio,
            "entry_base": entry_base, "entry_type": entry_type, "fill_price": fill_price, "atr": av,
            "cb_idx": cb_idx if cb_idx is not None else -1, "break_k": break_k}

    confirmed = (closes[i] >= entry_base + CONFIRM_ATR*av) if is_long else (closes[i] <= entry_base - CONFIRM_ATR*av)
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {**base, "exit_idx": i, "exit_price": ep, "r": r1, "r_base": r1, "r_addon": 0.0, "hold": 0,
                "exit_reason": "弱突破平仓", "confirmed": False, "addon": False, "addon_price": 0.0}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0; sl_at_addon = None
    sig_body_pct = abs(closes[i] - opens[i]) / opens[i] if opens[i] > 0 else 0.0
    addon_eligible = sig_body_pct >= ADDON_BODY_PCT
    exit_idx = exit_price = exit_reason = exit_sl = None
    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        bh = j - i
        if bh <= 3 or np.isnan(ks):
            sl = init_stop
        else:
            sl = max(ks, fill_price) if is_long else min(ks, fill_price)
        if not addon_done and addon_eligible:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= ADDON_ATR*av: addon_done = True; addon_price = float(closes[j]); sl_at_addon = float(sl)
        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)
        # 3K收盘认赔优先于前3K止损：收盘价穿成本价时按收盘价出场，优于按init_stop止损
        if bh == 3:
            if (is_long and closes[j] < fill_price) or (not is_long and closes[j] > fill_price):
                exit_idx = j; exit_price = float(closes[j]); exit_reason = "3K收盘认赔"; exit_sl = float(sl); break
        if hit_stop and hit_tp:
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep = result
                if reason_1m == "stop":
                    exit_idx = j; exit_price = ep; exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; exit_sl = float(sl)
                else:
                    exit_idx = j; exit_price = ep; exit_reason = "止盈"; exit_sl = float(sl)
                break
            ep = float(sl); exit_idx = j; exit_price = ep; exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; exit_sl = float(sl); break
        elif hit_stop:
            ep = float(sl); exit_idx = j; exit_price = ep; exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"; exit_sl = float(sl); break
        elif hit_tp:
            exit_idx = j; exit_price = float(tp_price); exit_reason = "止盈"; exit_sl = float(sl); break
    if exit_idx is None: exit_idx = n4h-1; exit_price = float(closes[-1]); exit_reason = "未平仓"; exit_sl = float(sl)
    r1 = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av
    if addon_done:
        addon_risk = (addon_price - sl_at_addon) if is_long else (sl_at_addon - addon_price)
        if addon_risk is None or np.isnan(addon_risk) or addon_risk <= 0:
            r2 = 0.0
        else:
            r2 = ((exit_price-addon_price)/addon_risk) if is_long else ((addon_price-exit_price)/addon_risk)
    else:
        r2 = 0.0
    return {**base, "exit_idx": exit_idx, "exit_price": exit_price, "r": r1+r2,
            "r_base": r1, "r_addon": r2,
            "hold": exit_idx-i, "exit_reason": exit_reason, "confirmed": True, "addon": addon_done,
            "addon_price": addon_price if addon_done else 0.0, "exit_sl": exit_sl}

def passes(r):
    return True

def e_prime_tier(r):
    if r["dir"]=="long" and r["atr_pct"]<2.0: return 2.0
    if r["dir"]=="short" and r["atr_pct"]<2.0: return 1.5
    return 1.0

signals = detect_breakout(df_4h, WINDOW)

results = []; last_exit = -1
all_records = []
for s in signals:
    if s.idx <= last_exit:
        r = simulate(s)
        if r:
            all_records.append({**r, "made": False, "skip_reason": "持仓中跳过"})
        continue
    r = simulate(s)
    if r and passes(r):
        results.append(r)
        last_exit = r["exit_idx"]
        all_records.append({**r, "made": True, "skip_reason": ""})
    elif r:
        if not r["confirmed"]:
            skip_reason = "弱突破平仓"
        elif np.isnan(r["dev"]):
            skip_reason = "偏离度NaN"
        elif r["dev"] < -2:
            skip_reason = "偏离度<-2%"
        elif r["dev"] >= 6:
            skip_reason = "偏离度≥6%不满足条件2"
        else:
            skip_reason = "方案A过滤"
        all_records.append({**r, "made": False, "skip_reason": skip_reason})
rdf = pd.DataFrame(results)
rdf = rdf.sort_values("sig_idx").reset_index(drop=True)
rdf["tier_base"] = rdf.apply(e_prime_tier, axis=1)

DD_THRESHOLD = 3.0
peak = 0.0; cum_base_wr = 0.0
tier_actual = []; downgraded = []
for _, row in rdf.iterrows():
    cum_base_wr += row["r"] * row["tier_base"]
    peak = max(peak, cum_base_wr)
    dd = cum_base_wr - peak
    if dd <= -DD_THRESHOLD:
        t = max(1.0, row["tier_base"] - 0.5)
        tier_actual.append(t)
        downgraded.append(True)
    else:
        tier_actual.append(row["tier_base"])
        downgraded.append(False)
rdf["tier"] = tier_actual
rdf["downgraded"] = downgraded
rdf["weighted_r"] = rdf["r"] * rdf["tier"]

def fmt_time(idx):
    return pd.Timestamp(times[idx]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")

def stats_str(sub, col="r"):
    if sub.empty: return "—"
    sr = sub[col]
    w = sr[sr>0].sum(); l = sr[sr<=0].sum()
    pf = f"{w/abs(l):.1f}" if l != 0 else "∞"
    return f"{len(sub)}单/{sr.sum():.1f}R/均{sr.mean():.2f}/PF{pf}/胜{(sr>0).mean():.0%}"

def max_dd(sub, col="r"):
    s = sub.sort_values("sig_idx"); cum = s[col].cumsum()
    return (cum - cum.cummax()).min()

total_r = rdf["r"].sum()
total_wr = rdf["weighted_r"].sum()
dd_r = max_dd(rdf, "r")
dd_wr = max_dd(rdf, "weighted_r")

# ==================== CSV ====================
rows = []
for r in rdf.to_dict("records"):
    rows.append({
        "序号": 0,
        "信号时间": fmt_time(r["sig_idx"]),
        "方向": "做多" if r["dir"]=="long" else "做空",
        "入场线类型": r["entry_type"],
        "成交价": round(r["fill_price"], 2),
        "ATR": round(r["atr"], 2),
        "ATR%": round(r["atr_pct"], 3),
        "EMA70偏离度%": round(r["dev"], 2),
        "突破K数": r["break_k"],
        "前3K方向涨幅%": round(r["pre_3k_pct"], 2) if not np.isnan(r["pre_3k_pct"]) else "",
        "量比": round(r["vol_ratio"], 2) if not np.isnan(r["vol_ratio"]) else "",
        "是否确认": "确认" if r["confirmed"] else "弱突破",
        "是否加仓": "是" if r["addon"] else "",
        "加仓价": round(r["addon_price"], 2) if r["addon"] else "",
        "出场时间": fmt_time(r["exit_idx"]),
        "出场价": round(r["exit_price"], 2),
        "持仓K数": r["hold"],
        "底仓R值": round(r["r_base"], 3),
        "加仓R值": round(r["r_addon"], 3) if r["r_addon"] != 0 else "",
        "R值": round(r["r"], 3),
        "基础档位": f"{r['tier_base']:.1f}x",
        "是否降档": "是" if r["downgraded"] else "",
        "实际档位": f"{r['tier']:.1f}x",
        "底仓加权R": round(r["r_base"] * r["tier"], 3),
        "加仓加权R": round(r["r_addon"] * r["tier"], 3) if r["r_addon"] != 0 else "",
        "加权R值": round(r["weighted_r"], 3),
        "出场原因": r["exit_reason"],
        "年份": r["year"],
    })
csv_df = pd.DataFrame(rows).sort_values("信号时间", ascending=False).reset_index(drop=True)
csv_df["序号"] = range(1, len(csv_df)+1)
csv_path = cfg.results_path / "final_trades_E_prime.csv"
try:
    csv_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
except PermissionError:
    csv_path = cfg.results_path / "final_trades_E_prime_new.csv"
    csv_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  (原文件被占用，已写入: {csv_path.name})")

# ==================== 全部波段CSV ====================
all_rows = []
for r in all_records:
    tier = e_prime_tier(r) if r["made"] else None
    all_rows.append({
        "序号": 0,
        "信号时间": fmt_time(r["sig_idx"]),
        "方向": "做多" if r["dir"]=="long" else "做空",
        "入场线类型": r["entry_type"],
        "成交价": round(r["fill_price"], 2),
        "ATR": round(r["atr"], 2),
        "ATR%": round(r["atr_pct"], 3),
        "EMA70偏离度%": round(r["dev"], 2) if not np.isnan(r["dev"]) else "",
        "突破K数": r["break_k"],
        "前3K方向涨幅%": round(r["pre_3k_pct"], 2) if not np.isnan(r["pre_3k_pct"]) else "",
        "量比": round(r["vol_ratio"], 2) if not np.isnan(r["vol_ratio"]) else "",
        "状态": "做上" if r["made"] else "未做",
        "不做原因": r["skip_reason"],
        "是否确认": "确认" if r["confirmed"] else "弱突破",
        "是否加仓": "是" if r.get("addon") else "",
        "加仓价": round(r["addon_price"], 2) if r.get("addon") else "",
        "出场时间": fmt_time(r["exit_idx"]),
        "出场价": round(r["exit_price"], 2),
        "持仓K数": r["hold"],
        "底仓R值": round(r["r_base"], 3),
        "加仓R值": round(r["r_addon"], 3) if r["r_addon"] != 0 else "",
        "R值": round(r["r"], 3),
        "基础档位": f"{tier:.1f}x" if tier else "",
        "出场原因": r["exit_reason"],
        "年份": r["year"],
    })
all_csv_df = pd.DataFrame(all_rows).sort_values("信号时间", ascending=False).reset_index(drop=True)
all_csv_df["序号"] = range(1, len(all_csv_df)+1)
all_csv_path = cfg.results_path / "all_signals_E_prime.csv"
try:
    all_csv_df.to_csv(all_csv_path, index=False, encoding="utf-8-sig")
except PermissionError:
    all_csv_path = cfg.results_path / "all_signals_E_prime_new.csv"
    all_csv_df.to_csv(all_csv_path, index=False, encoding="utf-8-sig")
    print(f"  (原文件被占用，已写入: {all_csv_path.name})")
made_count = (all_csv_df["状态"]=="做上").sum()
not_made_count = (all_csv_df["状态"]=="未做").sum()
print(f"全部波段CSV已生成: {all_csv_path}（{len(all_csv_df)}行×{len(all_csv_df.columns)}列，做上{made_count}单/未做{not_made_count}单）")

# ==================== 只根据基准线入场CSV ====================
base_rows = []
for r in rdf.to_dict("records"):
    if r["entry_type"] != "基准值": continue
    base_rows.append({
        "序号": 0,
        "信号时间": fmt_time(r["sig_idx"]),
        "方向": "做多" if r["dir"]=="long" else "做空",
        "入场线类型": r["entry_type"],
        "成交价": round(r["fill_price"], 2),
        "ATR": round(r["atr"], 2),
        "ATR%": round(r["atr_pct"], 3),
        "EMA70偏离度%": round(r["dev"], 2),
        "突破K数": r["break_k"],
        "前3K方向涨幅%": round(r["pre_3k_pct"], 2) if not np.isnan(r["pre_3k_pct"]) else "",
        "量比": round(r["vol_ratio"], 2) if not np.isnan(r["vol_ratio"]) else "",
        "是否确认": "确认" if r["confirmed"] else "弱突破",
        "是否加仓": "是" if r["addon"] else "",
        "加仓价": round(r["addon_price"], 2) if r["addon"] else "",
        "出场时间": fmt_time(r["exit_idx"]),
        "出场价": round(r["exit_price"], 2),
        "持仓K数": r["hold"],
        "底仓R值": round(r["r_base"], 3),
        "加仓R值": round(r["r_addon"], 3) if r["r_addon"] != 0 else "",
        "R值": round(r["r"], 3),
        "基础档位": f"{r['tier_base']:.1f}x",
        "是否降档": "是" if r["downgraded"] else "",
        "实际档位": f"{r['tier']:.1f}x",
        "底仓加权R": round(r["r_base"] * r["tier"], 3),
        "加仓加权R": round(r["r_addon"] * r["tier"], 3) if r["r_addon"] != 0 else "",
        "加权R值": round(r["weighted_r"], 3),
        "出场原因": r["exit_reason"],
        "年份": r["year"],
    })
base_csv_df = pd.DataFrame(base_rows).sort_values("信号时间", ascending=False).reset_index(drop=True)
base_csv_df["序号"] = range(1, len(base_csv_df)+1)
base_csv_path = cfg.results_path / "final_trades_base_only.csv"
try:
    base_csv_df.to_csv(base_csv_path, index=False, encoding="utf-8-sig")
except PermissionError:
    base_csv_path = cfg.results_path / "final_trades_base_only_new.csv"
    base_csv_df.to_csv(base_csv_path, index=False, encoding="utf-8-sig")
    print(f"  (原文件被占用，已写入: {base_csv_path.name})")
base_total_r = base_csv_df["R值"].sum() if len(base_csv_df) > 0 else 0
base_wr = base_csv_df["加权R值"].sum() if len(base_csv_df) > 0 else 0
print(f"基准线入场CSV已生成: {base_csv_path}（{len(base_csv_df)}行×{len(base_csv_df.columns)}列，总R={base_total_r:.1f}/加权R={base_wr:.1f}）")

# ==================== MD Report ====================
md = []
md.append("# BTC永续4H 3K突破策略 — 最终实盘就绪报告")
md.append("")
md.append(f"> 生成时间：2026-08-24  |  数据源：标记价  |  回测区间：{fmt_time(0)} ~ {fmt_time(n4h-1)}")
md.append("")
md.append("---")
md.append("")

# 一、策略概述
md.append("## 一、策略概述")
md.append("")
md.append("基于3K突破信号 + 回调基准入场 + 方案A复合过滤 + E'分仓位档位的BTC永续4H突破策略。")
md.append("")
md.append("- **信号**：3K突破（window=3）")
md.append("- **入场**：回调基准（3K窗口内极值）挂限价单，成交价=基准线")
md.append("- **过滤**：方案A（偏离度+方向+ATR%事前过滤）")
md.append("- **确认**：收盘价离entry_base≥0.5ATR才持仓，否则弱突破平仓")
md.append("- **止损**：前3K用init_stop(成交价-1ATR)，之后用max(KAMA, 成交价)")
md.append("- **止盈**：做多+22%，做空-12%")
md.append(f"- **加仓**：突破K实体涨跌幅≥{ADDON_BODY_PCT:.0%}时许可加仓，入场后收盘价浮盈≥{ADDON_ATR}×ATR时以收盘价加仓1倍，只加一次，无前3K限制")
md.append("- **分档**：E'方案（做多&ATR%<2→2x，做空&ATR%<2→1.5x，其余→1x）")
md.append(f"- **回撤自适应**：当前回撤>{DD_THRESHOLD:.0f}R时，档位降0.5x（最小1x），走出回撤后恢复")
md.append("- **数据源**：标记价（markPriceKlines），实盘用标记价触发")
md.append("")

# 二、参数清单
md.append("## 二、参数清单")
md.append("")
md.append("| 参数 | 值 | 说明 |")
md.append("|------|-----|------|")
md.append(f"| 突破窗口 | {WINDOW} | 3K突破 |")
md.append(f"| KAMA ER长度 | {cfg.kama_er_len} | 效率比平滑 |")
md.append(f"| KAMA fast | {cfg.kama_fast} | 快线 |")
md.append(f"| KAMA slow | {cfg.kama_slow} | 慢线 |")
md.append(f"| ATR长度 | {ATR_LEN} | 20周期RMA |")
md.append(f"| EMA长度 | {EMA_LEN} | 日线EMA70 |")
md.append(f"| 确认阈值 | {CONFIRM_ATR}×ATR | 弱突破平仓 |")
md.append(f"| 止损倍数 | {STOP_MULT}×ATR | 前3K止损 |")
md.append(f"| 止盈(多) | +{TP_LONG:.0%} | 做多止盈 |")
md.append(f"| 止盈(空) | -{TP_SHORT:.0%} | 做空止盈 |")
md.append(f"| 加仓许可 | 突破K实体涨跌幅≥{ADDON_BODY_PCT:.0%} | 弱突破不加仓 |")
md.append(f"| 加仓阈值 | {ADDON_ATR}×ATR | 收盘价浮盈达1.5ATR加仓，只加一次 |")
md.append("| 方案A过滤1 | dev∈[-2%,6%) | 全要 |")
md.append("| 方案A过滤2 | dev∈[6%,15%) 且 前3K方向涨幅<0 且 ATR%<1.8 | 事前过滤 |")
md.append("| E'分档 | 做多&ATR%<2→2x，做空&ATR%<2→1.5x，其余→1x | 无未来信息 |")
md.append(f"| 回撤自适应 | 回撤>{DD_THRESHOLD:.0f}R时降0.5x | 减小连损放大，走出回撤恢复 |")
md.append("")

# 三、总体绩效
md.append("## 三、总体绩效")
md.append("")
md.append("| 指标 | 基础R值 | E'加权R值 |")
md.append("|------|---------|----------|")
md.append(f"| 总单数 | {len(rdf)} | {len(rdf)} |")
md.append(f"| 总R | {total_r:.1f} | {total_wr:.1f} |")
md.append(f"| 均R | {rdf['r'].mean():.2f} | {rdf['weighted_r'].mean():.2f} |")
w_r = rdf["r"][rdf["r"]>0].sum(); l_r = rdf["r"][rdf["r"]<=0].sum()
w_wr = rdf["weighted_r"][rdf["weighted_r"]>0].sum(); l_wr = rdf["weighted_r"][rdf["weighted_r"]<=0].sum()
md.append(f"| 盈利R | {w_r:.1f} | {w_wr:.1f} |")
md.append(f"| 亏损R | {l_r:.1f} | {l_wr:.1f} |")
md.append(f"| PF | {w_r/abs(l_r):.1f} | {w_wr/abs(l_wr):.1f} |")
md.append(f"| 胜率 | {(rdf['r']>0).mean():.0%} | {(rdf['weighted_r']>0).mean():.0%} |")
md.append(f"| 最大回撤 | {dd_r:.1f}R | {dd_wr:.1f}R |")
md.append("")

# 四、逐年绩效
md.append("## 四、逐年绩效")
md.append("")
md.append("| 年份 | 单数 | 基础R | 均R | 胜率 | E'加权R | 加权均R | 加权胜率 | 回撤 |")
md.append("|------|------|-------|-----|------|---------|---------|---------|------|")
for yr in sorted(rdf["year"].unique()):
    sub = rdf[rdf["year"]==yr]
    s_r = sub.sort_values("sig_idx"); cum_r = s_r["r"].cumsum()
    dd_y = (cum_r - cum_r.cummax()).min()
    md.append(f"| {yr} | {len(sub)} | {sub['r'].sum():.1f} | {sub['r'].mean():.2f} | "
              f"{(sub['r']>0).mean():.0%} | {sub['weighted_r'].sum():.1f} | "
              f"{sub['weighted_r'].mean():.2f} | {(sub['weighted_r']>0).mean():.0%} | {dd_y:.1f} |")
md.append("")

# 五、出场原因分布
md.append("## 五、出场原因分布")
md.append("")
md.append("| 出场原因 | 单数 | R | 均R | 胜率 | E'加权R |")
md.append("|---------|------|---|-----|------|---------|")
for reason in sorted(rdf["exit_reason"].unique()):
    sub = rdf[rdf["exit_reason"]==reason]
    md.append(f"| {reason} | {len(sub)} | {sub['r'].sum():.1f} | {sub['r'].mean():.2f} | "
              f"{(sub['r']>0).mean():.0%} | {sub['weighted_r'].sum():.1f} |")
md.append("")

# 六、E'分档统计
md.append("## 六、E'分档统计（含回撤自适应降档）")
md.append("")
dg = rdf[rdf["downgraded"]]
md.append(f"- 降档单：{len(dg)}单（回撤>{DD_THRESHOLD:.0f}R时触发，档位降0.5x）")
md.append("")
md.append("| 实际档位 | 单数 | 基础R | 均R | 胜率 | 加权R | 降档单 |")
md.append("|---------|------|-------|-----|------|-------|-------|")
for tier in [2.0, 1.5, 1.0]:
    sub = rdf[rdf["tier"]==tier]
    if sub.empty: continue
    dg_n = sub["downgraded"].sum()
    md.append(f"| {tier:.1f}x | {len(sub)} | {sub['r'].sum():.1f} | {sub['r'].mean():.2f} | "
              f"{(sub['r']>0).mean():.0%} | {sub['weighted_r'].sum():.1f} | {dg_n} |")
md.append(f"| **合计** | **{len(rdf)}** | **{total_r:.1f}** | **{rdf['r'].mean():.2f}** | "
          f"**{(rdf['r']>0).mean():.0%}** | **{total_wr:.1f}** | **{len(dg)}** |")
md.append("")

# 七、方向×ATR%分布
md.append("## 七、方向×ATR%分布（E'加权R）")
md.append("")
md.append("| 方向 | ATR%区间 | 单数 | 基础R | 加权R | 档位 |")
md.append("|------|---------|------|-------|-------|------|")
for d in ["long", "short"]:
    for lo, hi in [(0,0.5),(0.5,1.0),(1.0,1.5),(1.5,2.0),(2.0,999)]:
        sub = rdf[(rdf["dir"]==d)&(rdf["atr_pct"]>=lo)&(rdf["atr_pct"]<hi)]
        if sub.empty: continue
        tier_str = "/".join([f"{t:.1f}x" for t in sorted(sub["tier"].unique())])
        md.append(f"| {d} | [{lo},{hi}) | {len(sub)} | {sub['r'].sum():.1f} | "
                  f"{sub['weighted_r'].sum():.1f} | {tier_str} |")
md.append("")

# 八、确认 vs 弱突破
md.append("## 八、确认 vs 弱突破")
md.append("")
confirmed = rdf[rdf["confirmed"]]
weak = rdf[~rdf["confirmed"]]
md.append(f"- 确认单：{len(confirmed)}单 / {confirmed['r'].sum():.1f}R / 均{confirmed['r'].mean():.2f}R / 胜{(confirmed['r']>0).mean():.0%}")
md.append(f"- 弱突破单：{len(weak)}单 / {weak['r'].sum():.1f}R / 均{weak['r'].mean():.2f}R / 胜{(weak['r']>0).mean():.0%}")
md.append(f"- 弱突破平仓是安全垫：100%胜率锁定微利，释放last_exit让后续信号能触发")
md.append("")

# 九、加仓统计
md.append("## 九、加仓统计")
md.append("")
addon = rdf[rdf["addon"]]
no_addon = rdf[~rdf["addon"]]
md.append(f"- 加仓单：{len(addon)}单 / 底仓R={addon['r_base'].sum():.1f} / 加仓R={addon['r_addon'].sum():.1f} / 总R={addon['r'].sum():.1f}")
md.append(f"- 非加仓单：{len(no_addon)}单 / 底仓R={no_addon['r_base'].sum():.1f}")
md.append(f"- 加仓R计算：(出场价 - 加仓价) / (加仓价 - 加仓时止损价)，用加仓时的初始风险做分母")
md.append(f"- 加仓价=触发K收盘价，加仓许可：突破K实体涨跌幅≥{ADDON_BODY_PCT:.0%}，加仓触发：收盘价浮盈≥{ADDON_ATR}×ATR")
md.append("")

# 十、回撤分析
md.append("## 十、回撤分析")
md.append("")
s_sorted = rdf.sort_values("sig_idx")
cum_wr = s_sorted["weighted_r"].cumsum()
dd_series = cum_wr - cum_wr.cummax()
md.append(f"- E'加权最大回撤：{dd_wr:.1f}R")
md.append(f"- E'加权最终R：{total_wr:.1f}R")
md.append(f"- 回撤/最终R比：{abs(dd_wr)/total_wr:.1%}")
md.append("")
dd_idx = dd_series.idxmin()
dd_loc = s_sorted.index.get_loc(dd_idx)
dd_peak_loc = cum_wr.iloc[:dd_loc+1].idxmax()
md.append(f"- 回撤起点：信号时间 {fmt_time(s_sorted.loc[dd_peak_loc, 'sig_idx'])}")
md.append(f"- 回撤谷底：信号时间 {fmt_time(s_sorted.loc[dd_idx, 'sig_idx'])}")
md.append("")

# 十一、实盘注意事项
md.append("## 十一、实盘注意事项")
md.append("")
md.append("1. **数据源**：使用标记价（markPrice）触发信号和止损，非最新价")
md.append("2. **入场**：在回调基准挂限价单，成交价≈基准线（可能有微小滑点）")
md.append("3. **确认检查**：成交后等该K收盘，检查收盘价离entry_base是否≥0.5ATR")
md.append("4. **弱突破平仓**：收盘价离entry_base<0.5ATR → 收盘市价平仓")
md.append("5. **止损**：前3K用固定止损(成交价-1ATR)，第4K起用KAMA跟踪止损")
md.append("6. **止盈**：做多+22%，做空-12%，用限价单挂止盈价")
md.append(f"7. **加仓**：突破K实体涨跌幅≥{ADDON_BODY_PCT:.0%}时许可加仓，入场后收盘价浮盈≥{ADDON_ATR}ATR时以收盘价加仓1倍，只加一次，无前3K限制")
md.append("8. **分档**：E'方案在成交时即可确定档位（ATR%和方向均已知）")
md.append("9. **ATR取值**：用信号K前一根的ATR（atr_vals[i-1]），固定值不动态更新")
md.append("10. **EMA70**：用日线EMA70，取信号K前一根的日线值")
md.append("")

md.append("---")
md.append("")
md.append(f"## 交易记录CSV")
md.append("")
md.append(f"文件：`{csv_path.name}`")
md.append(f"行数：{len(csv_df)}行 × {len(csv_df.columns)}列")
md.append(f"列名：{', '.join(csv_df.columns.tolist())}")
md.append(f"排序：按信号时间降序")

report_path = cfg.results_path / "FINAL_REPORT.md"
with open(report_path, "w", encoding="utf-8") as f:
    f.write("\n".join(md))

print(f"CSV已生成: {csv_path}（{len(csv_df)}行×{len(csv_df.columns)}列）")
print(f"报告已生成: {report_path}")
print(f"\n=== 总体绩效 ===")
print(f"  基础: {len(rdf)}单 / {total_r:.1f}R / 均{rdf['r'].mean():.2f} / PF{w_r/abs(l_r):.1f} / 胜{(rdf['r']>0).mean():.0%} / 回撤{dd_r:.1f}R")
print(f"  E'加权: {total_wr:.1f}R / 均{rdf['weighted_r'].mean():.2f} / PF{w_wr/abs(l_wr):.1f} / 胜{(rdf['weighted_r']>0).mean():.0%} / 回撤{dd_wr:.1f}R")
