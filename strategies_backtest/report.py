"""回测报告：汇总、过滤、输出、摘要、统计分析。"""
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import LegResult

# 英文字段 → 中文专业名称
CHINESE_COLUMNS = {
    "signal_time": "信号时间",
    "direction": "方向",
    "breakthrough": "突破基准价",
    "entry_kama": "入场KAMA",
    "exit_time": "出场时间",
    "exit_price": "出场价",
    "final_return": "实际涨跌幅",
    "max_excursion": "盘中最大涨跌幅",
    "duration_bars": "持仓K线数",
    "duration_hours": "持仓小时",
    "dd1": "最大回撤1",
    "dd1_time": "最大回撤1时间",
    "dd2": "最大回撤2",
    "dd2_time": "最大回撤2时间",
    "dd3": "最大回撤3",
    "dd3_time": "最大回撤3时间",
    "exited_by_stop": "KAMA止损出场",
    "dd_peak_to_exit": "峰值到平仓回撤",
    "breakeven_active": "触发保本切换",
    "exit_type": "出场类型",
}

TIME_COLS = ("signal_time", "exit_time", "dd1_time", "dd2_time", "dd3_time")


def build_report(results: list) -> pd.DataFrame:
    if not results:
        return pd.DataFrame()
    return pd.DataFrame([r.__dict__ for r in results])


def filter_by_abs_excursion(df: pd.DataFrame, min_abs: float) -> pd.DataFrame:
    """按 abs(盘中最大涨跌幅) >= min_abs 过滤波段。"""
    if df.empty or min_abs <= 0:
        return df
    return df[df["max_excursion"].abs() >= min_abs].reset_index(drop=True)


def to_beijing_time(df: pd.DataFrame) -> pd.DataFrame:
    """将所有时间列转为北京时间（UTC+8），格式 YYYY-MM-DD HH:MM。"""
    df = df.copy()
    for c in TIME_COLS:
        if c not in df.columns:
            continue
        s = pd.to_datetime(df[c], utc=True, errors="coerce")
        s = s.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M")
        df[c] = s
    return df


def apply_chinese_columns(df: pd.DataFrame) -> pd.DataFrame:
    """重命名为中文列名。"""
    return df.rename(columns={k: v for k, v in CHINESE_COLUMNS.items() if k in df.columns})


def save_report(df: pd.DataFrame, out_dir: Path, tag: str) -> tuple:
    """保存为中文列名 + 北京时间的 CSV/JSON。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    df_out = to_beijing_time(df)
    df_out = apply_chinese_columns(df_out)
    csv_path = out_dir / f"legs_{tag}.csv"
    json_path = out_dir / f"legs_{tag}.json"
    df_out.to_csv(csv_path, index=False, encoding="utf-8-sig")  # BOM 便于 Excel 中文
    df_out.to_json(json_path, orient="records", indent=2, force_ascii=False)
    return csv_path, json_path


def print_summary(df: pd.DataFrame, min_abs: float, total_signals: int) -> None:
    print("\n" + "=" * 64)
    print("回测结果摘要")
    print("=" * 64)
    print(f"信号总数:       {total_signals}")
    print(f"过滤阈值:       |盘中最大涨跌幅| >= {min_abs:.2%}")
    print(f"过滤后波段数:   {len(df)}")
    if df.empty:
        print("无符合条件的波段。")
        return

    long_n = (df["direction"] == "long").sum()
    short_n = (df["direction"] == "short").sum()
    stopped = df["exited_by_stop"].sum()
    print(f"做多/做空:      {long_n} / {short_n}")
    print(f"5KAMA 止损出场:  {stopped} ({stopped / len(df):.1%})")
    print(f"持有至末端:     {len(df) - stopped}")

    # 出场类型分布（保本机制）
    if "exit_type" in df.columns:
        print("\n—— 出场类型分布 ——")
        for t, cnt in df["exit_type"].value_counts().items():
            label = {"kama_stop": "第1-3K内KAMA止损", "kama_trail": "第3K后KAMA跟踪锁利", "breakeven": "保本平仓(收益0)", "end": "持有至末端"}.get(t, t)
            print(f"  {label}: {cnt} ({cnt / len(df):.1%})")
        be = df["breakeven_active"].sum()
        print(f"  触发保本切换(第3K仍持仓): {be} ({be / len(df):.1%})")

    print("\n—— 实际涨跌幅（突破线→出场）——")
    print(df["final_return"].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).to_string())
    print("\n—— 盘中最大涨跌幅 MFE ——")
    print(df["max_excursion"].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).to_string())
    print("\n—— 持仓小时 ——")
    print(df["duration_hours"].describe(percentiles=[0.1, 0.5, 0.9]).to_string())

    print("\n—— 回撤均值 ——")
    for k in ("dd1", "dd2", "dd3", "dd_peak_to_exit"):
        print(f"  {CHINESE_COLUMNS.get(k, k)}: mean={df[k].mean():.4f}, max={df[k].max():.4f}")
    print("=" * 64)


def print_analysis(df: pd.DataFrame) -> None:
    """需求1：MFE vs 实际涨跌幅 关联分析 + 提前止盈建议。

    假设每次在突破线买入，若盘中达到止盈阈值 X% 则提前平仓（收益=X%），
    否则持有到 KAMA 止损（收益=实际涨跌幅）。扫描不同 X 找最优。
    """
    if df.empty:
        return
    print("\n" + "=" * 64)
    print("止盈空间统计分析（假设突破线买入）")
    print("=" * 64)

    mfe = df["max_excursion"]
    ret = df["final_return"]
    n = len(df)

    # —— 中位数 / 众数（分桶）——
    print(f"\n样本数: {n}")
    print(f"盘中最大涨跌幅 MFE:  中位数={mfe.median():.2%}  均值={mfe.mean():.2%}")
    print(f"实际涨跌幅:          中位数={ret.median():.2%}  均值={ret.mean():.2%}")

    # 众数：按 2% 宽度分桶
    def bucket_mode(series, width=0.02):
        bins = np.arange(0, series.max() + width, width)
        counts, edges = np.histogram(series, bins=bins)
        idx = counts.argmax()
        return edges[idx], edges[idx + 1], counts[idx]

    lo, hi, cnt = bucket_mode(mfe)
    print(f"MFE 众数区间:        [{lo:.2%}, {hi:.2%})  出现 {cnt} 次")
    lo2, hi2, cnt2 = bucket_mode(ret)
    print(f"实际涨跌幅众数区间:  [{lo2:.2%}, {hi2:.2%})  出现 {cnt2} 次")

    # —— 关联：捕获率与回吐 ——
    valid = mfe > 0
    capture = (ret[valid] / mfe[valid])  # 实际捕获了 MFE 的多少
    giveback = mfe[valid] - ret[valid]   # 回吐幅度
    print(f"\n捕获率 (实际/MFE):    中位数={capture.median():.1%}  均值={capture.mean():.1%}")
    print(f"回吐幅度 (MFE-实际):  中位数={giveback.median():.2%}  均值={giveback.mean():.2%}")
    print(f"  → 典型波段只捕获了 MFE 的 {capture.median():.0%}，回吐 {giveback.median():.2%}")

    # —— 提前止盈扫描 ——
    # 对每个阈值 X：若 MFE>=X 则收益=X，否则收益=final_return
    print("\n—— 提前止盈阈值扫描（达到 X% 即平仓，否则等 KAMA）——")
    print(f"{'止盈阈值':>10} {'平均收益':>10} {'中位收益':>10} {'胜率':>8} {'触发次数':>10} {'vs纯KAMA':>10}")
    thresholds = [0.03, 0.05, 0.07, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20]
    base_mean = ret.mean()
    best = None
    for x in thresholds:
        hit = mfe >= x
        gain = np.where(hit, x, ret)
        mean_g = gain.mean()
        med_g = np.median(gain)
        win = (gain > 0).mean()
        hit_n = hit.sum()
        vs = mean_g - base_mean
        marker = " *" if vs > 0 else ""
        print(f"{x:>10.0%} {mean_g:>10.2%} {med_g:>10.2%} {win:>8.1%} {hit_n:>10d} {vs:>+10.2%}{marker}")
        if best is None or mean_g > best[1]:
            best = (x, mean_g, med_g, win, hit_n)

    print(f"\n基准（纯 KAMA 止损）: 平均收益={base_mean:.2%}, 胜率={(ret>0).mean():.1%}")
    print(f"最优止盈阈值: {best[0]:.0%}  → 平均收益={best[1]:.2%}, 中位收益={best[2]:.2%}, "
          f"胜率={best[3]:.1%}, 触发{best[4]}次/{n}")

    # —— 多空分开：最优止盈阈值对比 ——
    print("\n—— 多空分开：最优止盈阈值对比 ——")
    print(f"{'方向':>6} {'样本':>6} {'纯KAMA均值':>12} {'MFE中位':>10} {'最优阈值':>10} "
          f"{'最优均值':>10} {'最优中位':>10} {'胜率':>8}")
    for d, label in [("long", "做多"), ("short", "做空")]:
        sub = df[df["direction"] == d]
        if sub.empty:
            continue
        sub_ret = sub["final_return"]
        sub_mfe = sub["max_excursion"]
        sub_base = sub_ret.mean()
        sub_best = None
        for x in thresholds:
            hit = sub_mfe >= x
            gain = np.where(hit, x, sub_ret)
            mg = gain.mean()
            if sub_best is None or mg > sub_best[1]:
                sub_best = (x, mg, np.median(gain), (gain > 0).mean())
        print(f"{label:>6} {len(sub):>6} {sub_base:>12.2%} {sub_mfe.median():>10.2%} "
              f"{sub_best[0]:>10.0%} {sub_best[1]:>10.2%} {sub_best[2]:>10.2%} {sub_best[3]:>8.1%}")
    print("=" * 64)


def print_distribution(df: pd.DataFrame, title: str = "全波段") -> None:
    """需求3：盘中最大涨跌幅 MFE 箱体分布，建立对下次突破空间的预期。"""
    if df.empty:
        return
    mfe = df["max_excursion"]
    print("\n" + "=" * 64)
    print(f"{title} · 盘中最大涨跌幅 MFE 箱体分布")
    print("=" * 64)
    print(f"样本数: {len(df)}")
    print(f"最小={mfe.min():.2%}  Q1={mfe.quantile(0.25):.2%}  中位={mfe.median():.2%}  "
          f"Q3={mfe.quantile(0.75):.2%}  最大={mfe.max():.2%}  均值={mfe.mean():.2%}")

    width = 0.02
    bins = np.arange(0, mfe.max() + width, width)
    counts, edges = np.histogram(mfe, bins=bins)
    total = len(df)
    print(f"\n{'区间':>18} {'次数':>6} {'占比':>8} {'累计占比':>10}")
    cum = 0
    for k in range(len(counts)):
        if counts[k] == 0:
            continue
        cum += counts[k]
        print(f"[{edges[k]:>6.1%}, {edges[k+1]:>6.1%}) {counts[k]:>6} "
              f"{counts[k]/total:>8.1%} {cum/total:>10.1%}")
    print("=" * 64)
