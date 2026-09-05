"""导出最优配置CSV：包含成交单和未成交单，标记可能错过的涨跌幅度/R值"""
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
open_times = df['open_time'].tolist()
dates = df['open_time'].dt.year.to_numpy()

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

def find_callback_extreme(i, direction, window=6):
    start = i - window; end = i - 1
    for j in range(start, end - 1):
        if j < 0 or j + 2 >= n: continue
        if direction == "long":
            if highs[j + 1] <= highs[j] and highs[j + 2] <= highs[j]:
                return highs[j], j
        else:
            if lows[j + 1] >= lows[j] and lows[j + 2] >= lows[j]:
                return lows[j], j
    return None, None

def simulate_full(signal):
    """完整模拟，返回所有信息包括是否成交、可能错过的R值"""
    i = signal.idx
    if i + 1 >= n: return None
    bt_6k = signal.breakthrough; d = signal.direction; is_long = (d == "long")
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    # 回调高点
    cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
    has_cb = cb_extreme is not None
    if has_cb:
        entry_base = cb_extreme
    else:
        entry_base = bt_6k

    # 是否成交：突破K盘中low是否<=回调高点（做多）/ high是否>=回调高点（做空）
    if is_long:
        filled = lows[i] <= entry_base
    else:
        filled = highs[i] >= entry_base

    # 确认阈值
    if is_long:
        confirmed = closes[i] >= entry_base + 0.5 * av
    else:
        confirmed = closes[i] <= entry_base - 0.5 * av

    entry_price = entry_base
    if is_long:
        init_stop = entry_price - 0.3 * av
        tp_price = entry_base * (1 + 0.22)
    else:
        init_stop = entry_price + 0.3 * av
        tp_price = entry_base * (1 - 0.12)

    # 弱突破：入场后在收盘价平仓
    if not confirmed:
        ep = float(closes[i])
        r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
        return {
            "signal_idx": i, "dir": d, "bt_6k": bt_6k, "entry_base": entry_base,
            "has_cb": has_cb, "cb_idx": cb_idx, "atr": av, "confirmed": False,
            "filled": filled, "entry_price": entry_price, "init_stop": init_stop,
            "tp_price": tp_price, "exit_idx": i, "exit_price": ep, "hold": 0,
            "r": r1, "exit_reason": "弱突破平仓", "addon_done": False, "addon_price": 0,
            "pre3k": True, "peak_price": ep, "mfe_pct": 0, "mae_pct": 0,
        }

    # 确认通过：正常持有
    peak = entry_price; addon_done = False; addon_price = 0.0
    mfe = -np.inf if is_long else np.inf
    mae = np.inf if is_long else -np.inf
    for j in range(i + 1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        bh = j - i
        if bh <= 3:
            sl = init_stop
        else:
            sl = max(ks, entry_base) if is_long else min(ks, entry_base)
        # 加仓
        if not addon_done:
            if is_long:
                cg_abs = closes[j] - entry_base
            else:
                cg_abs = entry_base - closes[j]
            if cg_abs >= 1.5 * av:
                addon_done = True; addon_price = float(closes[j])
        # MFE/MAE
        if is_long:
            mfe = max(mfe, highs[j])
            mae = min(mae, lows[j])
        else:
            mfe = min(mfe, lows[j])
            mae = max(mae, highs[j])
        # 止损
        if is_long and lows[j] < sl:
            ep = float(opens[j]) if opens[j] < sl else float(sl)
            r1 = (ep-entry_price)/av
            r2 = ((ep-addon_price)/av if addon_done else 0.0)
            reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
            return _result(i, d, bt_6k, entry_base, has_cb, cb_idx, av, True, filled,
                          entry_price, init_stop, tp_price, j, ep, bh, r1+r2, reason,
                          addon_done, addon_price, bh<=3, peak, mfe, mae, is_long)
        if not is_long and highs[j] > sl:
            ep = float(opens[j]) if opens[j] > sl else float(sl)
            r1 = (entry_price-ep)/av
            r2 = ((addon_price-ep)/av if addon_done else 0.0)
            reason = "前3K止损" if bh <= 3 else "KAMA跟踪止损"
            return _result(i, d, bt_6k, entry_base, has_cb, cb_idx, av, True, filled,
                          entry_price, init_stop, tp_price, j, ep, bh, r1+r2, reason,
                          addon_done, addon_price, bh<=3, peak, mfe, mae, is_long)
        # 止盈
        if is_long and highs[j] >= tp_price:
            r1 = (tp_price-entry_price)/av
            r2 = ((tp_price-addon_price)/av if addon_done else 0.0)
            return _result(i, d, bt_6k, entry_base, has_cb, cb_idx, av, True, filled,
                          entry_price, init_stop, tp_price, j, tp_price, bh, r1+r2, "止盈",
                          addon_done, addon_price, bh<=3, peak, mfe, mae, is_long)
        if not is_long and lows[j] <= tp_price:
            r1 = (entry_price-tp_price)/av
            r2 = ((addon_price-tp_price)/av if addon_done else 0.0)
            return _result(i, d, bt_6k, entry_base, has_cb, cb_idx, av, True, filled,
                          entry_price, init_stop, tp_price, j, tp_price, bh, r1+r2, "止盈",
                          addon_done, addon_price, bh<=3, peak, mfe, mae, is_long)
        if is_long: peak = max(peak, highs[j])
        else: peak = min(peak, lows[j])
    # 未平仓
    ep = float(closes[-1])
    r1 = (ep-entry_price)/av if is_long else (entry_price-ep)/av
    r2 = ((ep-addon_price)/av if is_long else (addon_price-ep)/av) if addon_done else 0.0
    return _result(i, d, bt_6k, entry_base, has_cb, cb_idx, av, True, filled,
                  entry_price, init_stop, tp_price, n-1, ep, n-1-i, r1+r2, "未平仓",
                  addon_done, addon_price, False, peak, mfe, mae, is_long)

def _result(i, d, bt_6k, entry_base, has_cb, cb_idx, av, confirmed, filled,
           entry_price, init_stop, tp_price, exit_idx, exit_price, hold, r, reason,
           addon_done, addon_price, pre3k, peak, mfe, mae, is_long):
    mfe_pct = (mfe - entry_base) / entry_base * 100 if is_long else (entry_base - mfe) / entry_base * 100
    mae_pct = (mae - entry_base) / entry_base * 100 if is_long else (entry_base - mae) / entry_base * 100
    return {
        "signal_idx": i, "dir": d, "bt_6k": bt_6k, "entry_base": entry_base,
        "has_cb": has_cb, "cb_idx": cb_idx, "atr": av, "confirmed": confirmed,
        "filled": filled, "entry_price": entry_price, "init_stop": init_stop,
        "tp_price": tp_price, "exit_idx": exit_idx, "exit_price": exit_price, "hold": hold,
        "r": r, "exit_reason": reason, "addon_done": addon_done, "addon_price": addon_price,
        "pre3k": pre3k, "peak_price": peak, "mfe_pct": mfe_pct, "mae_pct": mae_pct,
    }

# 运行
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate_full(s)
    if r:
        results.append(r)
        last_exit = r["exit_idx"]

# 构建DataFrame
rows = []
for idx, r in enumerate(results):
    i = r["signal_idx"]
    sig_time = pd.Timestamp(open_times[i]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
    exit_time = pd.Timestamp(open_times[r["exit_idx"]]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M") if r["exit_idx"] != r["signal_idx"] else sig_time

    cb_offset_pct = (r["bt_6k"] - r["entry_base"]) / r["bt_6k"] * 100 if r["dir"] == "long" else (r["entry_base"] - r["bt_6k"]) / r["bt_6k"] * 100
    atr_pct = r["atr"] / r["bt_6k"] * 100

    # 可能错过的涨幅和R值（对于未成交单）
    if not r["filled"]:
        missed_pct = r["mfe_pct"]  # 如果做上，最大可能涨幅%
        missed_r = r["r"]  # 如果做上，可能获得的R值
    else:
        missed_pct = 0
        missed_r = 0

    rows.append({
        "序号": idx + 1,
        "信号时间": sig_time,
        "方向": "做多" if r["dir"] == "long" else "做空",
        "6K极值": round(r["bt_6k"], 2),
        "回调基准": round(r["entry_base"], 2) if r["has_cb"] else "",
        "是否有回调基准": "是" if r["has_cb"] else "否",
        "回调基准偏移%": round(cb_offset_pct, 3) if r["has_cb"] else "",
        "ATR": round(r["atr"], 2),
        "ATR%": round(atr_pct, 3),
        "突破K收盘价": round(closes[i], 2),
        "突破K最低价": round(lows[i], 2),
        "突破K最高价": round(highs[i], 2),
        "是否成交": "成交" if r["filled"] else "未成交",
        "是否确认": "确认" if r["confirmed"] else "弱突破",
        "入场价": round(r["entry_price"], 2),
        "初始止损": round(r["init_stop"], 2),
        "止盈目标": round(r["tp_price"], 2),
        "出场时间": exit_time,
        "出场价": round(r["exit_price"], 2),
        "持仓K数": r["hold"],
        "R值": round(r["r"], 3),
        "出场原因": r["exit_reason"],
        "是否加仓": "是" if r["addon_done"] else "否",
        "加仓价": round(r["addon_price"], 2) if r["addon_done"] else "",
        "前3K止损": "是" if r["pre3k"] else "否",
        "MFE%": round(r["mfe_pct"], 2),
        "MAE%": round(r["mae_pct"], 2),
        "可能错过涨幅%": round(missed_pct, 2) if not r["filled"] else "",
        "可能错过R值": round(missed_r, 3) if not r["filled"] else "",
        "年份": int(dates[i]),
    })

out_df = pd.DataFrame(rows)

# 统计
print("=" * 100)
print("CSV导出统计")
print("=" * 100)
filled = out_df[out_df["是否成交"] == "成交"]
unfilled = out_df[out_df["是否成交"] == "未成交"]
print(f"\n总单数: {len(out_df)}")
print(f"  成交: {len(filled)} ({len(filled)/len(out_df)*100:.0f}%), 累计R={filled['R值'].sum():.1f}")
print(f"  未成交: {len(unfilled)} ({len(unfilled)/len(out_df)*100:.0f}%), 可能错过R={unfilled['可能错过R值'].sum():.1f}")

print(f"\n  成交单按出场原因:")
for reason in filled["出场原因"].unique():
    sub = filled[filled["出场原因"] == reason]
    print(f"    {reason}: {len(sub)}单, R={sub['R值'].sum():.1f}")

print(f"\n  未成交单按出场原因:")
for reason in unfilled["出场原因"].unique():
    sub = unfilled[unfilled["出场原因"] == reason]
    print(f"    {reason}: {len(sub)}单, 可能错过R={sub['可能错过R值'].sum():.1f}")

# 导出CSV
csv_path = os.path.join(os.path.dirname(__file__), "results", "最优配置_回调基准_ATR动态.csv")
out_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
print(f"\nCSV已导出: {csv_path}")
print(f"共 {len(out_df)} 行 × {len(out_df.columns)} 列")
print(f"列名: {list(out_df.columns)}")
