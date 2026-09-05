"""利润锁阈值扫描：找保本效果与不错失大波段的平衡点。

用法：python strategies_backtest/scan_profit_lock.py
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
from backtest import simulate_leg
from report import filter_by_abs_excursion

cfg = BacktestConfig()
print("加载数据...")
df = load_klines(cfg)
kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
signals = detect_breakout(df, cfg.window)
print(f"共 {len(df)} 根K线，{len(signals)} 个原始信号\n")


def run(profit_lock, be_bars=3, min_abs=0.10):
    results = []
    last_exit = None
    for s in signals:
        if last_exit is not None and s.time <= last_exit:
            continue
        r = simulate_leg(s, df, kama_vals, breakeven_after_bars=be_bars,
                         profit_lock_threshold=profit_lock)
        if r is not None:
            results.append(r)
            last_exit = r.exit_time
    report = pd.DataFrame([r.__dict__ for r in results])
    filtered = filter_by_abs_excursion(report, min_abs)
    if filtered.empty:
        return None
    ret = filtered["final_return"]
    mfe = filtered["max_excursion"]
    best_x, best_mean = 0, ret.mean()
    for x in [0.10, 0.12, 0.15, 0.18, 0.20]:
        gain = np.where(mfe >= x, x, ret)
        if gain.mean() > best_mean:
            best_x, best_mean = x, gain.mean()
    return {
        "ge10": len(filtered), "mean": ret.mean(), "median": ret.median(),
        "win": (ret > 0).mean(), "worst": ret.min(), "best_x": best_x, "best_mean": best_mean,
    }


print("=" * 95)
print("利润锁阈值扫描（时间锁固定3K，看错失大波段 vs 保本效果的权衡）")
print("=" * 95)
print(f"{'利润锁':>8} {'≥10%波段':>8} {'错失数':>6} {'均值':>8} {'中位':>8} {'胜率':>7} "
      f"{'最差收益':>9} {'最优止盈':>8} {'最优均值':>8}")
base_ge10 = 94  # 无利润锁的≥10%波段数（3K锁）
for pl in [0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08]:
    r = run(pl)
    if r:
        lost = base_ge10 - r["ge10"] if pl > 0 else 0
        label = f"{pl:.0%}" if pl > 0 else "关闭"
        print(f"{label:>8} {r['ge10']:>8} {lost:>6} {r['mean']:>8.2%} {r['median']:>8.2%} "
              f"{r['win']:>7.1%} {r['worst']:>9.2%} {r['best_x']:>8.0%} {r['best_mean']:>8.2%}")
print("=" * 95)
print("注：错失数 = 无利润锁时≥10%波段数(94) - 当前≥10%波段数")
