"""止盈策略分析：多空统一值扫描 + 做多20%止盈 vs KAMA 分类拆解。"""
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

results = []
last_exit = None
for s in signals:
    if last_exit is not None and s.time <= last_exit:
        continue
    r = simulate_leg(s, df, kama_vals, breakeven_after_bars=3, profit_lock_threshold=0.0)
    if r is not None:
        results.append(r)
        last_exit = r.exit_time

report = pd.DataFrame([r.__dict__ for r in results])
filtered = filter_by_abs_excursion(report, 0.10)
print(f"合并后 {len(results)} 波段，≥10%过滤后 {len(filtered)} 波段\n")

# ========== 问题1：多空统一止盈值扫描 ==========
print("=" * 95)
print("多空统一止盈值扫描（认知减负：找一个多空都适用的统一值）")
print("=" * 95)
print(f"{'统一止盈':>10} {'做多均值':>10} {'做空均值':>10} {'合并均值':>10} "
      f"{'做多胜率':>10} {'做空胜率':>10} {'合并胜率':>10}")

long_sub = filtered[filtered["direction"] == "long"]
short_sub = filtered[filtered["direction"] == "short"]
print(f"纯KAMA基准  {long_sub['final_return'].mean():>10.2%} {short_sub['final_return'].mean():>10.2%} "
      f"{filtered['final_return'].mean():>10.2%} {(long_sub['final_return']>0).mean():>10.1%} "
      f"{(short_sub['final_return']>0).mean():>10.1%} {(filtered['final_return']>0).mean():>10.1%}")
print("-" * 95)

for x in [0.10, 0.12, 0.15, 0.18, 0.20]:
    long_gain = np.where(long_sub["max_excursion"] >= x, x, long_sub["final_return"])
    short_gain = np.where(short_sub["max_excursion"] >= x, x, short_sub["final_return"])
    all_gain = np.concatenate([long_gain, short_gain])
    print(f"{x:>10.0%} {long_gain.mean():>10.2%} {short_gain.mean():>10.2%} {all_gain.mean():>10.2%} "
          f"{(long_gain>0).mean():>10.1%} {(short_gain>0).mean():>10.1%} {(all_gain>0).mean():>10.1%}")

print("\n注：均值=每个波段收益的算术平均；止盈逻辑=盘中MFE达X%则以X%平仓，否则按KAMA出场")

# ========== 问题3：做多20%止盈 vs KAMA 分类拆解 ==========
print("\n" + "=" * 95)
print("做多 20% 止盈 vs 纯KAMA 分类拆解（口径：从突破线算涨幅）")
print("=" * 95)

long_df = long_sub.copy()
long_df["tp_triggered"] = long_df["max_excursion"] >= 0.20

class_a = long_df[long_df["tp_triggered"] & (long_df["final_return"] < 0.20)]
class_b = long_df[long_df["tp_triggered"] & (long_df["final_return"] >= 0.20)]
class_c = long_df[~long_df["tp_triggered"]]

print(f"\n做多≥10%波段总数: {len(long_df)}")
print(f"  A类（止盈帮了忙，KAMA回吐<20%）: {len(class_a)} ({len(class_a)/len(long_df):.1%})")
print(f"  B类（止盈帮了倒忙，KAMA也≥20%）: {len(class_b)} ({len(class_b)/len(long_df):.1%})")
print(f"  C类（止盈未触发，MFE<20%）:      {len(class_c)} ({len(class_c)/len(long_df):.1%})")

print(f"\n—— A类：止盈帮了忙（{len(class_a)}单）——")
print(f"  MFE≥20%但KAMA回吐到<20%，20%止盈锁了更多利")
if not class_a.empty:
    print(f"  KAMA平均收益: {class_a['final_return'].mean():.2%}, 20%止盈收益: 20.00%")
    print(f"  止盈平均多赚: {0.20 - class_a['final_return'].mean():.2%}")

print(f"\n—— B类：止盈帮了倒忙（{len(class_b)}单）——")
print(f"  MFE≥20%且KAMA最终也≥20%，20%止盈反而少赚了")
if not class_b.empty:
    print(f"  KAMA平均收益: {class_b['final_return'].mean():.2%}, 20%止盈收益: 20.00%")
    print(f"  止盈平均少赚: {class_b['final_return'].mean() - 0.20:.2%}")
    print(f"\n  B类具体波段（按少赚幅度降序）:")
    print(f"  {'信号时间':>20} {'MFE':>8} {'KAMA收益':>10} {'少赚':>8} {'持仓h':>8} {'KAMA出场':>10}")
    class_b_sorted = class_b.sort_values("final_return", ascending=False)
    for _, row in class_b_sorted.iterrows():
        t = pd.to_datetime(row["signal_time"], utc=True).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M")
        print(f"  {t:>20} {row['max_excursion']:>8.1%} {row['final_return']:>10.2%} "
              f"{row['final_return']-0.20:>+8.2%} {row['duration_hours']:>8.0f} {row['exit_type']:>10}")

print(f"\n—— C类：止盈未触发（{len(class_c)}单）——")
print(f"  MFE<20%，20%止盈从未触发，按KAMA出场，两者收益相同")
if not class_c.empty:
    print(f"  KAMA平均收益: {class_c['final_return'].mean():.2%}")

print(f"\n—— 汇总：做多20%止盈 vs 纯KAMA ——")
tp_all = np.where(long_df["max_excursion"] >= 0.20, 0.20, long_df["final_return"])
print(f"  纯KAMA均值:   {long_df['final_return'].mean():.2%}")
print(f"  20%止盈均值:  {tp_all.mean():.2%}")
print(f"  差异:         {tp_all.mean() - long_df['final_return'].mean():+.2%}")
print(f"  → A类多赚 {(0.20 - class_a['final_return'].mean()) * len(class_a):.2f} 总利，"
      f" B类少赚 {(class_b['final_return'].mean() - 0.20) * len(class_b):.2f} 总利")
print("=" * 95)
