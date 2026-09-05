"""保本参数对比脚本：扫描不同时间锁K数 + 利润锁组合，输出关键指标对比。

用法：python strategies_backtest/compare_be.py
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


def run(be_bars, profit_lock, min_abs=0.10):
    """跑一组配置，返回关键指标。"""
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
    # 最优止盈
    best_x, best_mean = 0, ret.mean()
    for x in [0.08, 0.10, 0.12, 0.15, 0.18, 0.20]:
        gain = np.where(mfe >= x, x, ret)
        if gain.mean() > best_mean:
            best_x, best_mean = x, gain.mean()
    be_count = (filtered["exit_type"] == "breakeven").sum()
    worst = ret.min()
    return {
        "bands": len(results), "ge10": len(filtered),
        "mean": ret.mean(), "median": ret.median(), "win": (ret > 0).mean(),
        "worst": worst, "be_exit": be_count, "best_x": best_x, "best_mean": best_mean,
    }


# 需求1：时间锁 K 数对比（2/3/4/5/6K）
print("=" * 90)
print("需求1：保本时间锁 K 数对比（利润锁关闭）")
print("=" * 90)
print(f"{'配置':>10} {'总波段':>6} {'≥10%':>5} {'均值':>8} {'中位':>8} {'胜率':>7} {'最差':>8} {'保本平仓':>8} {'最优止盈':>8} {'最优均值':>8}")
for be in [2, 3, 4, 5, 6]:
    r = run(be, 0.0)
    if r:
        print(f"{'第'+str(be)+'K锁':>10} {r['bands']:>6} {r['ge10']:>5} {r['mean']:>8.2%} {r['median']:>8.2%} "
              f"{r['win']:>7.1%} {r['worst']:>8.2%} {r['be_exit']:>8d} {r['best_x']:>8.0%} {r['best_mean']:>8.2%}")

# 需求3：利润锁 2% 补充（时间锁3K + 利润锁2%）
print("\n" + "=" * 90)
print("需求3：时间锁3K + 利润锁X% 组合对比")
print("=" * 90)
print(f"{'配置':>16} {'总波段':>6} {'≥10%':>5} {'均值':>8} {'中位':>8} {'胜率':>7} {'最差':>8} {'保本平仓':>8} {'最优止盈':>8} {'最优均值':>8}")
configs = [
    (3, 0.0, "3K锁"),
    (3, 0.02, "3K锁+2%利锁"),
    (3, 0.03, "3K锁+3%利锁"),
    (99, 0.02, "仅2%利锁"),
    (99, 0.03, "仅3%利锁"),
]
for be, pl, label in configs:
    r = run(be, pl)
    if r:
        print(f"{label:>16} {r['bands']:>6} {r['ge10']:>5} {r['mean']:>8.2%} {r['median']:>8.2%} "
              f"{r['win']:>7.1%} {r['worst']:>8.2%} {r['be_exit']:>8d} {r['best_x']:>8.0%} {r['best_mean']:>8.2%}")
print("=" * 90)
