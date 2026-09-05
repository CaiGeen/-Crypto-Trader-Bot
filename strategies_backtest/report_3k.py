"""3K突破+3K回调基准 完整报告 + CSV导出。"""
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

    if not filled:
        return {"dir": d, "exit_idx": i, "filled": False, "has_cb": has_cb,
                "entry_base": entry_base, "bt_nk": bt_nk, "atr": av,
                "confirmed": confirmed, "r": 0.0, "hold": 0, "exit_reason": "限价未成交",
                "exit_price": 0.0, "fill_price": 0.0, "addon": False, "addon_price": 0,
                "ambig": False, "mfe_pct": 0.0, "mae_pct": 0.0, "year": int(years[i]), "sig_idx": i,
                "init_stop": 0, "tp_price": 0}

    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-fill_price)/av if is_long else (fill_price-ep)/av
        return {"dir": d, "exit_idx": i, "filled": True, "has_cb": has_cb,
                "entry_base": entry_base, "bt_nk": bt_nk, "atr": av,
                "confirmed": False, "r": r1, "hold": 0, "exit_reason": "弱突破平仓",
                "exit_price": ep, "fill_price": fill_price, "addon": False, "addon_price": 0,
                "ambig": False, "mfe_pct": 0.0, "mae_pct": 0.0, "year": int(years[i]), "sig_idx": i,
                "init_stop": 0, "tp_price": 0}

    if is_long: init_stop = fill_price - STOP_MULT*av; tp_price = fill_price*(1+TP_LONG)
    else: init_stop = fill_price + STOP_MULT*av; tp_price = fill_price*(1-TP_SHORT)
    addon_done = False; addon_price = 0.0
    exit_idx = exit_price = exit_reason = None; ambig = False
    mfe = -np.inf if is_long else np.inf; mae = np.inf if is_long else -np.inf

    for j in range(i+1, n4h):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        sl = init_stop if bh <= 3 else (max(ks, fill_price) if is_long else min(ks, fill_price))
        if not addon_done:
            cg_abs = (closes[j]-fill_price) if is_long else (fill_price-closes[j])
            if cg_abs >= 1.5*av: addon_done = True; addon_price = float(closes[j])
        if is_long: mfe = max(mfe, highs[j]); mae = min(mae, lows[j])
        else: mfe = min(mfe, lows[j]); mae = max(mae, highs[j])
        hit_stop = (lows[j] < sl) if is_long else (highs[j] > sl)
        hit_tp = (highs[j] >= tp_price) if is_long else (lows[j] <= tp_price)
        if hit_stop and hit_tp:
            ambig = True
            result = disambiguate_1m(j, sl, tp_price, is_long)
            if result is not None:
                reason_1m, ep = result
                if reason_1m == "stop":
                    exit_idx = j; exit_price = ep; exit_reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
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

    mfe_pct = (mfe-fill_price)/fill_price*100 if is_long else (fill_price-mfe)/fill_price*100
    mae_pct = (mae-fill_price)/fill_price*100 if is_long else (fill_price-mae)/fill_price*100
    r1 = (exit_price-fill_price)/av if is_long else (fill_price-exit_price)/av
    r2 = ((exit_price-addon_price)/av if is_long else (addon_price-exit_price)/av) if addon_done else 0.0
    return {"dir": d, "exit_idx": exit_idx, "filled": True, "has_cb": has_cb,
            "entry_base": entry_base, "bt_nk": bt_nk, "atr": av,
            "confirmed": True, "r": r1+r2, "hold": exit_idx-i, "exit_reason": exit_reason,
            "exit_price": exit_price, "fill_price": fill_price, "addon": addon_done, "addon_price": addon_price,
            "ambig": ambig, "mfe_pct": mfe_pct, "mae_pct": mae_pct, "year": int(years[i]), "sig_idx": i,
            "init_stop": init_stop, "tp_price": tp_price}

signals = detect_breakout(df_4h, WINDOW)
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s)
    if r:
        results.append(r)
        if r["filled"]:
            last_exit = r["exit_idx"]

rdf = pd.DataFrame(results)
filled = rdf[rdf["filled"]].copy()
sr = filled["r"]

print("=" * 110)
print(f"{WINDOW}K突破 + {WINDOW}K回调基准 完整报告")
print("=" * 110)
print(f"配置：{WINDOW}K突破+{WINDOW}K回调基准+0.5ATR确认+1ATR止损+1.5ATR加仓+做多22%/做空12%止盈+1min消歧")
print(f"数据：{df_4h['open_time'].iloc[0]} ~ {df_4h['open_time'].iloc[-1]}，{n4h}根4H K线")
print(f"原始信号：{len(signals)}，总波段：{len(rdf)}?，成交：{len(filled)}，未成交：{len(rdf)-len(filled)}（不占用last_exit）")
print()

pf = sr[sr>0].sum() / abs(sr[sr<=0].sum()) if sr[sr<=0].sum() != 0 else float("inf")
print(f"累计R={sr.sum():.1f}  均R={sr.mean():.2f}  胜率={(sr>0).mean():.0%}  PF={pf:.1f}")
print()

# 导出CSV（时间降序）
rows = []
for idx, r in rdf.reset_index(drop=True).iterrows():
    i = int(r["sig_idx"])
    sig_t = pd.Timestamp(times[i]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    is_unfilled = not r["filled"]
    if is_unfilled:
        rows.append({"序号": idx+1, "信号时间": sig_t, "方向": "做多" if r["dir"]=="long" else "做空",
            f"{WINDOW}K极值": round(r["bt_nk"],2), "入场基准": round(r["entry_base"],2), "成交价": "",
            "基准类型": "回调基准" if r["has_cb"] else f"回退{WINDOW}K极值",
            "ATR": round(r["atr"],2), "ATR%": round(r["atr"]/r["entry_base"]*100,3),
            "是否成交": "限价未成交", "是否确认": "确认" if r["confirmed"] else "弱突破",
            "初始止损": "", "止盈目标": "", "出场时间": "", "出场价": "",
            "持仓K数": "", "R值": "", "出场原因": "限价未成交",
            "是否加仓": "", "加仓价": "", "MFE%": "", "MAE%": "",
            "年份": int(r["year"])})
        continue
    exit_t = pd.Timestamp(times[int(r["exit_idx"])]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    rows.append({"序号": idx+1, "信号时间": sig_t, "方向": "做多" if r["dir"]=="long" else "做空",
        f"{WINDOW}K极值": round(r["bt_nk"],2), "入场基准": round(r["entry_base"],2),
        "成交价": round(r["fill_price"],2),
        "基准类型": "回调基准" if r["has_cb"] else f"回退{WINDOW}K极值",
        "ATR": round(r["atr"],2), "ATR%": round(r["atr"]/r["entry_base"]*100,3),
        "是否成交": "成交", "是否确认": "确认" if r["confirmed"] else "弱突破",
        "初始止损": round(r["init_stop"],2) if r["confirmed"] else "",
        "止盈目标": round(r["tp_price"],2) if r["confirmed"] else "",
        "出场时间": exit_t, "出场价": round(r["exit_price"],2),
        "持仓K数": int(r["hold"]), "R值": round(r["r"],3),
        "出场原因": r["exit_reason"], "是否加仓": "是" if r["addon"] else "否",
        "加仓价": round(r["addon_price"],2) if r["addon"] else "",
        "MFE%": round(r["mfe_pct"],2), "MAE%": round(r["mae_pct"],2),
        "年份": int(r["year"])})

out_df = pd.DataFrame(rows)
out_df = out_df.sort_values("信号时间", ascending=False).reset_index(drop=True)
out_df["序号"] = range(1, len(out_df) + 1)
csv_path = cfg.results_path / f"{WINDOW}K突破_完整报告.csv"
out_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
print(f"CSV已导出: {csv_path}")
print(f"共 {len(out_df)} 行 × {len(out_df.columns)} 列")
