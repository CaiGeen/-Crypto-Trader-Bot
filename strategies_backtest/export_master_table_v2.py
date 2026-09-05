"""导出v2回测总表：用回调高点作为波段涨幅计算基准。

与v1区别：
  - 新增回调高点列（回调高点价、偏移%、回调高点K线时间）
  - MFE从回调高点计算
  - 涨跌幅从回调高点计算
  - 止盈从回调高点计算
  - R值仍用突破线和ATR计算（入场/止损逻辑不变）
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

daily_df = pd.read_parquet(cfg.base_dir / "cache" / "BTCUSDTUSDT_1d.parquet")
k_ema = 2.0 / 71; de70 = np.zeros(len(daily_df)); de70[0] = daily_df["close"].iloc[0]
for i in range(1, len(daily_df)):
    de70[i] = daily_df["close"].iloc[i]*k_ema + de70[i-1]*(1-k_ema)
daily_df["date"] = daily_df["open_time"].dt.date
date_to_ema = dict(zip(daily_df["date"], de70))


def find_callback_extreme(i, direction, window=6):
    """在6K箱体内从右往左找最新的2K不创新高/低回调的极值点。"""
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


def simulate(signal, tp=0.12):
    i = signal.idx
    if i + 1 >= n: return None
    bt = signal.breakthrough; d = signal.direction
    av = atr_vals[i-1] if i-1 >= 0 else np.nan
    if np.isnan(av) or av <= 0: return None

    # 找回调高点
    cb_extreme, cb_idx = find_callback_extreme(i, d, cfg.window)
    has_cb = cb_extreme is not None
    if not has_cb:
        cb_extreme = bt; cb_idx = i

    # 止盈用回调高点作为基准
    if d == "long":
        init_stop = bt - av
        tp_price = cb_extreme * (1 + tp) if tp > 0 else float("inf")
    else:
        init_stop = bt + av
        tp_price = cb_extreme * (1 - tp) if tp > 0 else 0.0

    max_exc_bt = -np.inf; max_exc_cb = -np.inf
    exit_idx = exit_price = exit_reason = None
    for j in range(i+1, n):
        ks = kama_vals[j-1]
        if np.isnan(ks): continue
        # 两种MFE
        if d == "long":
            exc_bt = (highs[j] - bt) / bt
            exc_cb = (highs[j] - cb_extreme) / cb_extreme
        else:
            exc_bt = (bt - lows[j]) / bt
            exc_cb = (cb_extreme - lows[j]) / cb_extreme
        if exc_bt > max_exc_bt: max_exc_bt = exc_bt
        if exc_cb > max_exc_cb: max_exc_cb = exc_cb

        bh = j - i
        if bh <= 3: stop_line = init_stop; s_type = "atr_stop"
        else:
            if d == "long": stop_line = max(ks, bt); s_type = "breakeven" if ks < bt else "kama_trail"
            else: stop_line = min(ks, bt); s_type = "breakeven" if ks > bt else "kama_trail"
        if d == "long" and lows[j] < stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if d == "short" and highs[j] > stop_line: exit_idx, exit_price, exit_reason = j, float(stop_line), s_type; break
        if tp > 0:
            if d == "long" and highs[j] >= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
            if d == "short" and lows[j] <= tp_price: exit_idx, exit_price, exit_reason = j, float(tp_price), "take_profit"; break
    if exit_idx is None: exit_idx = n-1; exit_price = float(closes[-1]); exit_reason = "end"

    # R值用突破线算（入场/止损不变）
    r = (exit_price-bt)/av if d == "long" else (bt-exit_price)/av
    mfe_r = (max_exc_bt * bt) / av if max_exc_bt != -np.inf else 0.0

    # 涨跌幅：v2用回调高点算
    ret_cb = (exit_price-cb_extreme)/cb_extreme if d == "long" else (cb_extreme-exit_price)/cb_extreme
    # 同时保留突破线涨跌幅供对比
    ret_bt = (exit_price-bt)/bt if d == "long" else (bt-exit_price)/bt

    # 偏离度
    cur_date = pd.to_datetime(times[i], utc=True).date()
    ema_cur = date_to_ema.get(cur_date, np.nan)
    dev_cur = (closes[i] - ema_cur) / ema_cur * 100 if not np.isnan(ema_cur) else np.nan
    if i - 1 >= 0:
        prev_date = pd.to_datetime(times[i-1], utc=True).date()
        ema_prev = date_to_ema.get(prev_date, np.nan)
        dev_prev = (closes[i-1] - ema_prev) / ema_prev * 100 if not np.isnan(ema_prev) else np.nan
    else:
        dev_prev = np.nan

    sig_t = pd.to_datetime(signal.time, utc=True)
    exit_t = pd.to_datetime(times[exit_idx], utc=True)
    cb_t = pd.to_datetime(times[cb_idx], utc=True) if cb_idx is not None else None
    reason_map = {"atr_stop": "ATR止损", "breakeven": "保本平仓", "kama_trail": "KAMA跟踪止盈", "take_profit": "固定止盈", "end": "持有至末端"}

    return {
        "止盈配置": "纯KAMA" if tp == 0 else f"{tp:.0%}",
        "信号时间": sig_t.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M"),
        "年份": sig_t.year, "月份": sig_t.month,
        "方向": "做多" if d == "long" else "做空",
        "突破基准价": round(bt, 2),
        "回调高点价": round(cb_extreme, 2),
        "回调高点时间": cb_t.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M") if cb_t else None,
        "是否有回调高点": "是" if has_cb else "否",
        "回调偏移(%)": round((bt - cb_extreme) / bt * 100, 2) if d == "long" else round((cb_extreme - bt) / bt * 100, 2),
        "信号K收盘价": round(closes[i], 2),
        "ATR": round(av, 2),
        "ATR%": round(av / bt * 100, 2),
        "初始止损价": round(init_stop, 2),
        "止盈目标价": round(tp_price, 2) if tp > 0 else None,
        "日线EMA70": round(ema_cur, 2) if not np.isnan(ema_cur) else None,
        "偏离度_突破K(%)": round(dev_cur, 2) if not np.isnan(dev_cur) else None,
        "偏离度_前一K(%)": round(dev_prev, 2) if not np.isnan(dev_prev) else None,
        "出场时间": exit_t.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M"),
        "出场价": round(exit_price, 2),
        "出场原因": reason_map.get(exit_reason, exit_reason),
        "涨跌幅_回调高点(%)": round(ret_cb * 100, 2),
        "涨跌幅_突破线(%)": round(ret_bt * 100, 2),
        "R值": round(r, 2),
        "MFE_回调高点(%)": round(float(max_exc_cb) * 100, 2) if max_exc_cb != -np.inf else 0.0,
        "MFE_突破线(%)": round(float(max_exc_bt) * 100, 2) if max_exc_bt != -np.inf else 0.0,
        "MFE的R值": round(mfe_r, 2),
        "MFE是否≥10%(回调高点)": "是" if abs(max_exc_cb) >= 0.10 else "否",
        "MFE是否≥10%(突破线)": "是" if abs(max_exc_bt) >= 0.10 else "否",
        "MFE是否≥8%(回调高点)": "是" if abs(max_exc_cb) >= 0.08 else "否",
        "持仓K线数": exit_idx - i,
        "持仓小时": (exit_idx - i) * 4,
    }


tp_configs = [0.0, 0.12, 0.13, 0.14, 0.15, 0.16, 0.17, 0.18]
all_rows = []

for tp in tp_configs:
    label = "纯KAMA" if tp == 0 else f"{tp:.0%}"
    results = []; last_exit_idx = None
    for s in signals:
        if last_exit_idx is not None and s.idx <= last_exit_idx: continue
        r = simulate(s, tp=tp)
        if r: results.append(r); last_exit_idx = r["持仓K线数"] + s.idx
    all_rows.extend(results)
    n_ge10_cb = sum(1 for r in results if r["MFE是否≥10%(回调高点)"] == "是")
    n_ge10_bt = sum(1 for r in results if r["MFE是否≥10%(突破线)"] == "是")
    print(f"止盈{label}: {len(results)}波段，≥10%(回调高点){n_ge10_cb}单，≥10%(突破线){n_ge10_bt}单")

total_df = pd.DataFrame(all_rows)
out_path = cfg.base_dir / "results" / "回测总表_v2.csv"
total_df.to_csv(out_path, index=False, encoding="utf-8-sig")
print(f"\nv2总表已保存: strategies_backtest/results/回测总表_v2.csv")
print(f"总行数: {len(total_df)}，列数: {len(total_df.columns)}")
print(f"\n列名:")
for c in total_df.columns:
    print(f"  {c}")
print(f"\n止盈配置分布:")
print(total_df["止盈配置"].value_counts().to_string())
print(f"\n≥10%(回调高点): {(total_df['MFE是否≥10%(回调高点)']=='是').sum()}")
print(f"≥10%(突破线): {(total_df['MFE是否≥10%(突破线)']=='是').sum()}")
print(f"有回调高点: {(total_df['是否有回调高点']=='是').sum()}/{len(total_df)} ({(total_df['是否有回调高点']=='是').mean():.0%})")
print(f"回调偏移中位: {total_df['回调偏移(%)'].median():.2f}%")
