"""导出新入场逻辑v2的CSV明细（回调基准·1.5ATR加仓）。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout
from entry_atr_offset import simulate, find_callback_extreme, atr_vals, n, dates

cfg = BacktestConfig()
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
closes = df["close"].to_numpy(); opens = df["open"].to_numpy()

# 运行回调基准+加仓
results = []; last_exit = -1
for s in signals:
    if s.idx <= last_exit: continue
    r = simulate(s, use_callback=True, addon=True)
    if r:
        results.append((s, r))
        last_exit = r["exit_idx"]

rows = []
for idx, (s, r) in enumerate(results, 1):
    sig_time = df['open_time'].iloc[s.idx] + pd.Timedelta(hours=8)
    av = atr_vals[s.idx - 1] if s.idx - 1 >= 0 else np.nan
    base = r["base"]
    order_price = r["order_price"]
    d = r['dir']
    is_long = (d == 'long')

    if r['filled']:
        fill_time = df['open_time'].iloc[r['fill_idx']] + pd.Timedelta(hours=8)
        fill_price = r['fill_price']
        entry_price = fill_price
        init_stop = entry_price - 0.3 * av if is_long else entry_price + 0.3 * av
        confirm_threshold = entry_price + 0.5 * av if is_long else entry_price - 0.5 * av
        tp_price = base * 1.22 if is_long else base * (1 - 0.12)
        addon_threshold = base + 1.5 * av if is_long else base - 1.5 * av

        exit_idx = r['exit_idx']
        exit_time = df['open_time'].iloc[exit_idx] + pd.Timedelta(hours=8)

        # 判断出场类型
        if not r['confirmed']:
            exit_type = "弱突破平仓"
            exit_price = closes[exit_idx]
        elif exit_idx == n - 1 and r['hold'] == n - 1 - r['fill_idx']:
            # 检查是否是自然平仓
            bh = exit_idx - r['fill_idx']
            if bh <= 3:
                sl = init_stop
            else:
                ks = kama_vals[exit_idx - 1]
                sl = max(ks, base) if is_long else min(ks, base)
            if is_long:
                if lows[exit_idx] < sl:
                    exit_price = opens[exit_idx] if opens[exit_idx] < sl else sl
                    exit_type = "止损"
                elif highs[exit_idx] >= tp_price:
                    exit_price = tp_price
                    exit_type = "止盈"
                else:
                    exit_price = closes[-1]
                    exit_type = "未平仓"
            else:
                if highs[exit_idx] > sl:
                    exit_price = opens[exit_idx] if opens[exit_idx] > sl else sl
                    exit_type = "止损"
                elif lows[exit_idx] <= tp_price:
                    exit_price = tp_price
                    exit_type = "止盈"
                else:
                    exit_price = closes[-1]
                    exit_type = "未平仓"
        else:
            bh = exit_idx - r['fill_idx']
            if bh <= 3:
                sl = init_stop
            else:
                ks = kama_vals[exit_idx - 1]
                sl = max(ks, base) if is_long else min(ks, base)
            if is_long:
                if lows[exit_idx] < sl:
                    exit_price = opens[exit_idx] if opens[exit_idx] < sl else sl
                    exit_type = "止损"
                elif highs[exit_idx] >= tp_price:
                    exit_price = tp_price
                    exit_type = "止盈"
                else:
                    exit_price = closes[-1]
                    exit_type = "未平仓"
            else:
                if highs[exit_idx] > sl:
                    exit_price = opens[exit_idx] if opens[exit_idx] > sl else sl
                    exit_type = "止损"
                elif lows[exit_idx] <= tp_price:
                    exit_price = tp_price
                    exit_type = "止盈"
                else:
                    exit_price = closes[-1]
                    exit_type = "未平仓"

        rows.append({
            "序号": idx,
            "信号时间": sig_time.strftime("%Y/%m/%d %H:%M"),
            "方向": "做多" if is_long else "做空",
            "信号收盘价": closes[s.idx],
            "6K极值": s.breakthrough,
            "回调基准": base,
            "ATR": av,
            "挂单价格": order_price,
            "成交": "是",
            "取消原因": "",
            "成交时间": fill_time.strftime("%Y/%m/%d %H:%M"),
            "成交价格": fill_price,
            "入场价": entry_price,
            "紧止损价": init_stop,
            "确认阈值": confirm_threshold,
            "止盈价": tp_price,
            "加仓阈值": addon_threshold,
            "确认": "是" if r['confirmed'] else "否",
            "出场时间": exit_time.strftime("%Y/%m/%d %H:%M"),
            "出场价": exit_price,
            "出场类型": exit_type,
            "持仓K数": r['hold'],
            "R值": r['r'],
            "前3K": "是" if r['hold'] <= 3 else "否",
        })
    else:
        cancel_time = df['open_time'].iloc[r['exit_idx']] + pd.Timedelta(hours=8)
        rows.append({
            "序号": idx,
            "信号时间": sig_time.strftime("%Y/%m/%d %H:%M"),
            "方向": "做多" if is_long else "做空",
            "信号收盘价": closes[s.idx],
            "6K极值": s.breakthrough,
            "回调基准": base,
            "ATR": av,
            "挂单价格": order_price,
            "成交": "否",
            "取消原因": r['cancel'],
            "成交时间": "",
            "成交价格": "",
            "入场价": "",
            "紧止损价": "",
            "确认阈值": "",
            "止盈价": "",
            "加仓阈值": "",
            "确认": "",
            "出场时间": cancel_time.strftime("%Y/%m/%d %H:%M"),
            "出场价": "",
            "出场类型": "挂单取消",
            "持仓K数": 0,
            "R值": "",
            "前3K": "",
        })

rdf = pd.DataFrame(rows)
csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "results", "新入场逻辑v3_回调基准_ATR偏移.csv")
rdf.to_csv(csv_path, index=False, encoding="utf-8-sig")

filled = rdf[rdf['成交']=='是'].copy()
filled['R值'] = filled['R值'].astype(float)
cancelled = rdf[rdf['成交']=='否']

print(f"已导出 {len(rdf)} 行 → {csv_path}")
print(f"成交: {len(filled)}  取消: {len(cancelled)}")
print(f"累计R: {filled['R值'].sum():.1f}  均R: {filled['R值'].mean():.2f}  胜率: {(filled['R值']>0).mean():.0%}")

print(f"\n=== 出场类型统计 ===")
print(filled.groupby('出场类型').agg(单数=('R值','count'), 累计R=('R值','sum'), 均R=('R值','mean')))

print(f"\n=== 取消原因统计 ===")
print(cancelled.groupby('取消原因').agg(单数=('序号','count')))

print(f"\n=== 方向统计 ===")
print(filled.groupby('方向').agg(单数=('R值','count'), 累计R=('R值','sum'), 均R=('R值','mean'), 胜率=('R值', lambda x: f'{(x>0).mean():.0%}')))

print(f"\n=== 前3K统计 ===")
print(filled.groupby('前3K').agg(单数=('R值','count'), 累计R=('R值','sum'), 均R=('R值','mean')))

print(f"\n=== R值分布 ===")
r = filled['R值']
print(f"R <= -0.3: {(r <= -0.3).sum()} 笔, 累计R = {r[r <= -0.3].sum():.1f}")
print(f"-0.3 < R < 0: {((r > -0.3) & (r < 0)).sum()} 笔, 累计R = {r[(r > -0.3) & (r < 0)].sum():.1f}")
print(f"0 <= R < 0.5: {((r >= 0) & (r < 0.5)).sum()} 笔, 累计R = {r[(r >= 0) & (r < 0.5)].sum():.1f}")
print(f"0.5 <= R < 3: {((r >= 0.5) & (r < 3)).sum()} 笔, 累计R = {r[(r >= 0.5) & (r < 3)].sum():.1f}")
print(f"R >= 3: {(r >= 3).sum()} 笔, 累计R = {r[r >= 3].sum():.1f}")

print(f"\n=== 亏损最大10笔 ===")
print(filled.nsmallest(10, 'R值')[['序号','信号时间','方向','回调基准','挂单价格','成交价格','紧止损价','出场类型','R值','前3K']].to_string(index=False))

print(f"\n=== 盈利最大10笔 ===")
print(filled.nlargest(10, 'R值')[['序号','信号时间','方向','回调基准','挂单价格','成交价格','紧止损价','出场类型','R值','前3K']].to_string(index=False))

print(f"\n=== 前30笔明细 ===")
print(rdf[['序号','信号时间','方向','回调基准','挂单价格','成交','取消原因','成交价格','紧止损价','确认','出场类型','R值']].head(30).to_string(index=False))
