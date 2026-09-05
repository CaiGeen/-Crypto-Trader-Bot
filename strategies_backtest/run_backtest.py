"""回测 CLI 入口。

用法（从项目根目录运行）：
    python strategies_backtest/run_backtest.py
    python strategies_backtest/run_backtest.py --min-abs 0.10
    python strategies_backtest/run_backtest.py --min-abs 0.15 --refresh-data
    python strategies_backtest/run_backtest.py --max-rows 50   # 快速验证只取最近50根
"""
import argparse
import os
import sys
from datetime import datetime

# 确保本包目录在 sys.path，支持 `python strategies_backtest/run_backtest.py` 直接运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import BacktestConfig
from data_loader import load_klines
from indicators import kama
from signal_detector import detect_breakout
from backtest import simulate_leg
from report import build_report, filter_by_abs_excursion, save_report, print_summary, print_analysis, print_distribution


def main():
    parser = argparse.ArgumentParser(description="BTC 永续 4H 6K 突破 + KAMA 止损回测")
    parser.add_argument("--min-abs", type=float, default=0.0,
                        help="按 abs(max_excursion) 过滤的最小阈值，如 0.10 表示 10%%")
    parser.add_argument("--refresh-data", action="store_true", help="忽略缓存全量重拉 K 线")
    parser.add_argument("--max-rows", type=int, default=0, help="只取最近 N 根 K 线（快速验证，0=全部）")
    parser.add_argument("--output-tag", default=None, help="输出文件名标签，默认时间戳")
    parser.add_argument("--be-bars", type=int, default=3, help="保本时间锁：买入后第N K收盘后切换保本止损（默认3）")
    parser.add_argument("--profit-lock", type=float, default=0.0, help="保本利润锁：累计涨幅达X即保本（如0.02，0=不启用）")
    args = parser.parse_args()

    cfg = BacktestConfig()

    print(f"[1/5] 加载 K 线数据 ({cfg.symbol}, {cfg.timeframe})...")
    df = load_klines(cfg, refresh=args.refresh_data)
    if df.empty:
        print("未获取到任何 K 线数据，退出。")
        return
    if args.max_rows > 0:
        df = df.tail(args.max_rows).reset_index(drop=True)
        print(f"按 --max-rows 截取最近 {len(df)} 根用于验证")
    print(f"      共 {len(df)} 根，范围 {df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]}")

    print("[2/5] 计算 KAMA...")
    kama_vals = kama(df["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)

    print(f"[3/5] 检测 {cfg.window}K 突破信号...")
    signals = detect_breakout(df, cfg.window)
    print(f"      共 {len(signals)} 个信号（多+空）")

    print("[4/5] 回测各波段（合并持仓期间的重叠信号，每波段取最早突破点）...")
    results = []
    skipped_overlap = 0
    last_exit_time = None
    for s in signals:  # detect_breakout 已按时间升序
        if last_exit_time is not None and s.time <= last_exit_time:
            skipped_overlap += 1  # 落在上一波段持仓区间内 → 子信号，跳过
            continue
        r = simulate_leg(s, df, kama_vals,
                         breakeven_after_bars=args.be_bars,
                         profit_lock_threshold=args.profit_lock)
        if r is not None:
            results.append(r)
            last_exit_time = r.exit_time
    print(f"      原始信号 {len(signals)} 个 → 合并后 {len(results)} 个波段（跳过 {skipped_overlap} 个重叠子信号）")

    print("[5/5] 生成报告...")
    report = build_report(results)
    filtered = filter_by_abs_excursion(report, args.min_abs)

    tag = args.output_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path, json_path = save_report(filtered, cfg.results_path, tag)
    print_summary(filtered, args.min_abs, total_signals=len(signals))
    if not filtered.empty:
        print_analysis(filtered)
        print_distribution(filtered, f"≥{args.min_abs:.0%} 波段")
    if not report.empty and args.min_abs > 0:
        print_distribution(report, "全独立波段（不限阈值）")
    print(f"\n结果已保存:\n  CSV  -> {csv_path}\n  JSON -> {json_path}")


if __name__ == "__main__":
    main()
