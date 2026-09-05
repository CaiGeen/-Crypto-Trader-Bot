"""查看2026-08-19 20:00 UTC突破信号的trailing stop执行过程"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout

cfg = BacktestConfig()
df = load_klines(cfg)
signals = detect_breakout(df, cfg.window)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
highs = df["high"].to_numpy(); lows = df["low"].to_numpy()
closes = df["close"].to_numpy(); opens = df["open"].to_numpy(); n = len(df)

def calc_atr(h, l, c, length=20):
    m = len(c); tr = np.zeros(m); tr[0] = h[0] - l[0]
    for i in range(1, m):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    av = np.zeros(m); av[0] = tr[0]; a = 1.0/length
    for i in range(1, m): av[i] = a*tr[i] + (1-a)*av[i-1]
    return av

atr_vals = calc_atr(highs, lows, closes, 20)

# 找2026-08-19 20:00 UTC的信号
found = False
for s in signals:
    ot = df['open_time'].iloc[s.idx]
    if str(ot).startswith('2026-08-19 12:00'):
        found = True
        i = s.idx
        bt = s.breakthrough
        av = atr_vals[i-1]
        is_long = (s.direction == "long")
        init_stop = bt - av if is_long else bt + av
        tp_price = bt * 1.18 if is_long else bt * 0.88

        print(f"突破K: idx={i}, 北京时间={ot + pd.Timedelta(hours=8)}")
        print(f"方向={s.direction}, 突破线={bt:.1f}, ATR[i-1]={av:.1f}, ATR%={av/bt*100:.2f}%")
        print(f"初始止损={init_stop:.1f}, 止盈={tp_price:.1f}")
        print()

        peak = bt
        print(f"{'bh':>3} {'北京时间':>20} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'KAMA':>8} {'peak':>8} {'trail_stop':>10} {'止损线':>8} {'动作':>10}")
        print("-" * 120)

        for j in range(i, min(i+15, n)):
            otj = df['open_time'].iloc[j]
            bh = j - i
            ks = kama_vals[j-1] if j-1 >= 0 else np.nan

            if bh == 0:
                print(f"{bh:>3} {str(otj + pd.Timedelta(hours=8)):>20} {opens[j]:>8.1f} {highs[j]:>8.1f} {lows[j]:>8.1f} {closes[j]:>8.1f} {ks:>8.1f} {'—':>8} {'—':>10} {'—':>8} {'突破K':>10}")
                continue

            # 用前一根peak算止损
            if bh <= 3:
                sl = init_stop
                trail_str = "初始止损"
            else:
                if is_long:
                    trail_stop = peak - 0.5 * av
                    sl = max(trail_stop, bt, ks)
                else:
                    trail_stop = peak + 0.5 * av
                    sl = min(trail_stop, bt, ks)
                trail_str = f"{trail_stop:.1f}"

            # 检查出场
            action = ""
            if is_long and lows[j] < sl:
                action = f"止损出场@{sl:.1f}"
            elif not is_long and highs[j] > sl:
                action = f"止损出场@{sl:.1f}"
            elif is_long and highs[j] >= tp_price:
                action = f"止盈出场@{tp_price:.1f}"
            elif not is_long and lows[j] <= tp_price:
                action = f"止盈出场@{tp_price:.1f}"
            else:
                action = "持有"

            print(f"{bh:>3} {str(otj + pd.Timedelta(hours=8)):>20} {opens[j]:>8.1f} {highs[j]:>8.1f} {lows[j]:>8.1f} {closes[j]:>8.1f} {ks:>8.1f} {peak:>8.1f} {trail_str:>10} {sl:>8.1f} {action:>10}")

            if "出场" in action:
                r = (sl - bt) / av if is_long else (bt - sl) / av
                print(f"\nR值 = {r:.2f}")
                break

            # 收盘后更新peak
            if is_long:
                peak = max(peak, highs[j])
            else:
                peak = min(peak, lows[j])

if not found:
    print("未找到2026-08-19 20:00 UTC的突破信号")
    # 找附近的信号
    for s in signals:
        ot = df['open_time'].iloc[s.idx]
        if '2026-08-19' in str(ot) or '2026-08-20' in str(ot):
            print(f"  信号: idx={s.idx}, UTC={ot}, 北京={ot+pd.Timedelta(hours=8)}, 方向={s.direction}, 突破线={s.breakthrough:.1f}")
