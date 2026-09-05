"""方案A：最新价 vs 标记价 对比。同时跑两个数据源，输出标记价CSV + 对比报告。
方案A过滤：[-2%,6%)全要；[6%,15%)要前3K<0且ATR%<1.8。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from pathlib import Path
from config import BacktestConfig
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
cache = cfg.base_dir / "cache"
TP_LONG = 0.22; TP_SHORT = 0.12; STOP_MULT = 1.0; WINDOW = 3

def load_data(source):
    """source='latest'或'mark'。返回(df_4h, df_1m, daily_df)。"""
    if source == "latest":
        from data_loader import load_klines
        df_4h = load_klines(cfg)
        df_1m = pd.read_parquet(cache / "BTCUSDTUSDT_1m.parquet")
        daily_df = pd.read_parquet(cache / "BTCUSDT_1d.parquet")
    else:
        df_4h = pd.read_parquet(cache / "BTCUSDTUSDT_4h_mark.parquet")
        df_1m = pd.read_parquet(cache / "BTCUSDT_1m_mark.parquet")
        daily_df = pd.read_parquet(cache / "BTCUSDT_1d_mark.parquet")
        for df in (df_4h, df_1m, daily_df):
            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns: df[col] = df[col].astype(float)
    if "open_time" not in df_4h.columns:
        df_4h["open_time"] = pd.to_datetime(df_4h["open_time_ms"], unit="ms", utc=True)
    if "open_time" not in df_1m.columns:
        df_1m["open_time"] = pd.to_datetime(df_1m["open_time_ms"], unit="ms", utc=True)
    if "open_time" not in daily_df.columns:
        daily_df["open_time"] = pd.to_datetime(daily_df["open_time_ms"], unit="ms", utc=True)
    return df_4h, df_1m, daily_df

def run_scheme_a(df_4h, df_1m, daily_df):
    kama_vals = kama(df_4h["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
    highs = df_4h["high"].to_numpy(); lows = df_4h["low"].to_numpy()
    closes = df_4h["close"].to_numpy(); opens = df_4h["open"].to_numpy()
    n4h = len(df_4h)
    years = df_4h["open_time"].dt.year.to_numpy()
    times = df_4h["open_time"].tolist()

    k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
    for i in range(1, len(daily_df)):
        de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
    date_to_ema = dict(zip(daily_df["open_time"].dt.date, de70))

    def get_dev(i):
        if i - 1 < 0: return np.nan
        prev_date = pd.to_datetime(times[i-1], utc=True).date()
        ema_prev = date_to_ema.get(prev_date, np.nan)
        if np.isnan(ema_prev): return np.nan
        return (closes[i-1] - ema_prev) / ema_prev * 100

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
        cb_extreme, _ = find_callback_extreme(i, d, WINDOW)
        entry_base = cb_extreme if cb_extreme is not None else bt_nk
        filled = (lows[i] <= entry_base) if is_long else (highs[i] >= entry_base)
        confirmed = (closes[i] >= entry_base + 0.5*av) if is_long else (closes[i] <= entry_base - 0.5*av)
        fill_price = float(entry_base) if filled else 0.0
        dev = get_dev(i)
        pre_3k_pct = np.nan
        if i - 4 >= 0 and closes[i-4] > 0:
            raw = (closes[i-1] - closes[i-4]) / closes[i-4] * 100
            pre_3k_pct = raw if is_long else -raw
        atr_pct = av / closes[i-1] * 100 if i-1 >= 0 else np.nan
        if not filled: return None
        if not confirmed:
            ep = float(closes[i])
            r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
            return {"dir": d, "exit_idx": i, "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                    "confirmed": False, "fill_price": fill_price, "exit_price": ep,
                    "year": int(years[i]), "sig_idx": i, "dev": dev, "atr": av, "atr_pct": atr_pct,
                    "pre_3k_pct": pre_3k_pct}
        if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
        else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
        addon_done = False; addon_price = 0.0
        exit_idx = exit_price = exit_reason = None
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
                "confirmed": True, "fill_price": fill_price, "exit_price": exit_price,
                "year": int(years[i]), "sig_idx": i, "dev": dev, "atr": av, "atr_pct": atr_pct,
                "pre_3k_pct": pre_3k_pct}

    def passes(r):
        if np.isnan(r["dev"]): return False
        if -2 <= r["dev"] < 6: return True
        if 6 <= r["dev"] < 15 and r["pre_3k_pct"] < 0 and r["atr_pct"] < 1.8: return True
        return False

    signals = detect_breakout(df_4h, WINDOW)
    results = []; last_exit = -1
    for s in signals:
        if s.idx <= last_exit: continue
        r = simulate(s)
        if r and passes(r):
            results.append(r)
            last_exit = r["exit_idx"]
    return pd.DataFrame(results), times

def calc_stats(rdf):
    if rdf.empty: return {}
    sr = rdf["r"]
    w = sr[sr > 0]; l = sr[sr <= 0]
    pf = w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else float("inf")
    rdf_s = rdf.sort_values("sig_idx"); cum = rdf_s["r"].cumsum()
    return {"单数": len(rdf), "累计R": sr.sum(), "均R": sr.mean(),
            "胜率": (sr>0).mean(), "PF": pf, "最大回撤R": (cum - cum.cummax()).min(),
            "做多R": rdf[rdf["dir"]=="long"]["r"].sum(),
            "做空R": rdf[rdf["dir"]=="short"]["r"].sum()}

print("=" * 110)
print("方案A：最新价 vs 标记价 对比")
print("=" * 110)

rdf_latest, times_latest = run_scheme_a(*load_data("latest"))
rdf_mark, times_mark = run_scheme_a(*load_data("mark"))

st_l = calc_stats(rdf_latest); st_m = calc_stats(rdf_mark)

print(f"\n{'━'*110}")
print("一、总览对比")
print(f"{'━'*110}")
print(f"{'数据源':>8} {'单数':>6} {'累计R':>9} {'均R':>6} {'胜率':>6} {'PF':>7} {'最大回撤':>8} {'做多R':>8} {'做空R':>8}")
print("-" * 75)
for name, st in [("最新价", st_l), ("标记价", st_m)]:
    pf = f"{st['PF']:.1f}" if st['PF'] < 1000 else "inf"
    print(f"{name:>8} {st['单数']:>6} {st['累计R']:>9.1f} {st['均R']:>6.2f} {st['胜率']:>5.0%} {pf:>7} {st['最大回撤R']:>8.1f} {st['做多R']:>8.1f} {st['做空R']:>8.1f}")
print(f"{'差异':>8} {st_m['单数']-st_l['单数']:>+6} {st_m['累计R']-st_l['累计R']:>+9.1f} {st_m['均R']-st_l['均R']:>+6.2f} {st_m['胜率']-st_l['胜率']:>+5.1%} {'':>7} {st_m['最大回撤R']-st_l['最大回撤R']:>+8.1f}")

print(f"\n{'━'*110}")
print("二、年度分解对比")
print(f"{'━'*110}")
all_years = sorted(set(rdf_latest["year"].unique()) | set(rdf_mark["year"].unique()))
print(f"{'年份':>6} | {'最新价':>22} | {'标记价':>22}")
print("-" * 60)
for yr in all_years:
    sl = rdf_latest[rdf_latest["year"] == yr]
    sm = rdf_mark[rdf_mark["year"] == yr]
    def fmt(sub):
        if sub.empty: return f"{'—':>22}"
        sr2 = sub["r"]; cum = sub.sort_values("sig_idx")["r"].cumsum()
        return f"{len(sub):>4}/{sr2.sum():>6.1f}R/{(cum-cum.cummax()).min():>5.1f}"
    print(f"{yr:>6} | {fmt(sl):>22} | {fmt(sm):>22}")

print(f"\n{'━'*110}")
print("三、出场方式对比")
print(f"{'━'*110}")
all_reasons = sorted(set(rdf_latest["exit_reason"].unique()) | set(rdf_mark["exit_reason"].unique()))
print(f"{'出场原因':>14} | {'最新价':>22} | {'标记价':>22}")
print("-" * 65)
for reason in all_reasons:
    sl = rdf_latest[rdf_latest["exit_reason"] == reason]
    sm = rdf_mark[rdf_mark["exit_reason"] == reason]
    def fmt(sub):
        if sub.empty: return f"{'—':>22}"
        return f"{len(sub):>4}/{sub['r'].sum():>6.1f}R/{sub['r'].mean():>5.2f}"
    print(f"{reason:>14} | {fmt(sl):>22} | {fmt(sm):>22}")

# 逐单对比（按sig_idx对齐）
print(f"\n{'━'*110}")
print("四、逐单对比（按信号时间对齐）")
print(f"{'━'*110}")
l_by_idx = rdf_latest.set_index("sig_idx")
m_by_idx = rdf_mark.set_index("sig_idx")
common = set(l_by_idx.index) & set(m_by_idx.index)
only_l = set(l_by_idx.index) - set(m_by_idx.index)
only_m = set(m_by_idx.index) - set(l_by_idx.index)
print(f"  共同信号: {len(common)}，仅最新价: {len(only_l)}，仅标记价: {len(only_m)}")
if common:
    diffs_r = []
    diffs_exit = 0
    for idx in common:
        rl = l_by_idx.loc[idx]; rm = m_by_idx.loc[idx]
        diffs_r.append(rm["r"] - rl["r"])
        if rl["exit_reason"] != rm["exit_reason"]: diffs_exit += 1
    diffs_r = np.array(diffs_r)
    print(f"  R值差异: 均值{diffs_r.mean():+.4f} 绝对均值{np.abs(diffs_r).mean():.4f} 最大绝对{np.abs(diffs_r).max():.3f} std{diffs_r.std():.4f}")
    print(f"  出场原因不同: {diffs_exit}/{len(common)} ({diffs_exit/len(common):.1%})")
    print(f"  R值差异>0.1的单: {(np.abs(diffs_r)>0.1).sum()} ({(np.abs(diffs_r)>0.1).mean():.1%})")
    print(f"  R值差异>1的单: {(np.abs(diffs_r)>1).sum()} ({(np.abs(diffs_r)>1).mean():.1%})")

# 导出标记价CSV
rows = []
for r in rdf_mark.to_dict("records"):
    i = r["sig_idx"]
    sig_t = pd.Timestamp(times_mark[i]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    exit_t = pd.Timestamp(times_mark[r["exit_idx"]]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    rows.append({"信号时间": sig_t, "方向": "做多" if r["dir"]=="long" else "做空",
        "成交价": round(r["fill_price"],2), "ATR": round(r["atr"],2), "ATR%": round(r["atr_pct"],3),
        "EMA70偏离度%": round(r["dev"],2),
        "前3K涨幅%": round(r["pre_3k_pct"],2) if not np.isnan(r["pre_3k_pct"]) else "",
        "是否确认": "确认" if r["confirmed"] else "弱突破",
        "出场时间": exit_t, "出场价": round(r["exit_price"],2),
        "持仓K数": r["hold"], "R值": round(r["r"],3),
        "出场原因": r["exit_reason"], "年份": r["year"]})
out_df = pd.DataFrame(rows).sort_values("信号时间", ascending=False).reset_index(drop=True)
out_df.insert(0, "序号", range(1, len(out_df)+1))
csv_path = cfg.results_path / "方案A_标记价_完整报告.csv"
out_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
print(f"\n标记价CSV已导出: {csv_path}（{len(out_df)}行×{len(out_df.columns)}列）")
