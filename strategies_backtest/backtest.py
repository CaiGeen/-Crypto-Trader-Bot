"""单波段回测引擎：入场 → 逐根 KAMA 止损 → 统计 MFE/回撤/时长。"""
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from signal_detector import Signal
from stats import top3_drawdowns


@dataclass
class LegResult:
    signal_time: object
    direction: str
    breakthrough: float
    entry_kama: float
    exit_time: object
    exit_price: float
    final_return: float
    max_excursion: float
    duration_bars: int
    duration_hours: float
    dd1: float
    dd1_time: object
    dd2: float
    dd2_time: object
    dd3: float
    dd3_time: object
    exited_by_stop: bool  # True=跌破KAMA平仓，False=持有至数据末端未止损
    dd_peak_to_exit: float  # 从 MFE 最高点到平仓的最大回撤（可能与前3大重合，单独列出）
    breakeven_active: bool  # 第3K收盘时仍持仓 → 触发保本切换
    exit_type: str  # 'kama_stop'=第1-3K内KAMA止损 / 'breakeven'=保本平仓 / 'end'=持有至末端


def simulate_leg(signal: Signal, df: pd.DataFrame, kama_vals: np.ndarray,
                 breakeven_after_bars: int = 3,
                 profit_lock_threshold: float = 0.0) -> Optional[LegResult]:
    """模拟单个信号波段。

    逻辑：
      - 信号在 i 根收盘确认，从 i+1 根起持仓检查
      - 每根 j 的止损线 = KAMA[j-1]（前一根收盘确定的 KAMA）
      - 做多：low[j] < KAMA[j-1] → 盘中跌破，出场价 = KAMA[j-1]
      - 做空：high[j] > KAMA[j-1] → 盘中涨破，出场价 = KAMA[j-1]
      - MFE 用 high/low，累计收益用 close
    """
    n = len(df)
    i = signal.idx
    if i + 1 >= n:
        return None

    breakthrough = signal.breakthrough
    direction = signal.direction
    closes = df["close"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    times = df["open_time"].tolist()

    entry_kama = float(kama_vals[i - 1]) if i - 1 >= 0 else float("nan")

    returns: list = []
    r_times: list = []
    max_exc = -np.inf
    peak_idx = i  # MFE 最大点对应的 K 线索引
    exit_idx = None
    exit_price = None
    exited_by_stop = False
    breakeven_active = False
    exit_type = "end"

    for j in range(i + 1, n):
        kama_stop = kama_vals[j - 1]
        if np.isnan(kama_stop):
            continue

        # 最大有利偏移（MFE，盘中）
        if direction == "long":
            exc = (highs[j] - breakthrough) / breakthrough
        else:
            exc = (breakthrough - lows[j]) / breakthrough
        if exc > max_exc:
            max_exc = exc
            peak_idx = j  # 记录 MFE 最大点

        # 累计收益（收盘）
        if direction == "long":
            r = (closes[j] - breakthrough) / breakthrough
        else:
            r = (breakthrough - closes[j]) / breakthrough
        returns.append(r)
        r_times.append(times[j])

        # 保本触发：时间锁（第N K收盘后）OR 利润锁（累计涨幅达阈值）
        # 任一触发 → 止损线 = KAMA 与保本价(突破线)取更紧者
        time_lock = (j - i) > breakeven_after_bars
        profit_lock = profit_lock_threshold > 0 and max_exc >= profit_lock_threshold
        use_breakeven = time_lock or profit_lock
        if use_breakeven:
            if direction == "long":
                stop_line = max(kama_stop, breakthrough)
                is_breakeven_exit = kama_stop < breakthrough
            else:
                stop_line = min(kama_stop, breakthrough)
                is_breakeven_exit = kama_stop > breakthrough
            breakeven_active = True
        else:
            stop_line = kama_stop
            is_breakeven_exit = False

        # 止损（盘中触及即出场）
        if direction == "long" and lows[j] < stop_line:
            exit_idx, exit_price, exited_by_stop = j, float(stop_line), True
            exit_type = ("breakeven" if is_breakeven_exit else "kama_trail") if use_breakeven else "kama_stop"
            break
        if direction == "short" and highs[j] > stop_line:
            exit_idx, exit_price, exited_by_stop = j, float(stop_line), True
            exit_type = ("breakeven" if is_breakeven_exit else "kama_trail") if use_breakeven else "kama_stop"
            break

    # 未止损 → 持有至数据末端，用最后一根收盘
    if exit_idx is None:
        exit_idx = n - 1
        exit_price = float(closes[-1])
        exit_type = "end"

    if direction == "long":
        final_return = (exit_price - breakthrough) / breakthrough
    else:
        final_return = (breakthrough - exit_price) / breakthrough

    duration_bars = exit_idx - i
    duration_hours = duration_bars * 4.0

    dds = top3_drawdowns(np.array(returns, dtype=float), r_times) if returns else []
    dd1, dd1_t = (dds[0] if len(dds) > 0 else (0.0, None))
    dd2, dd2_t = (dds[1] if len(dds) > 1 else (0.0, None))
    dd3, dd3_t = (dds[2] if len(dds) > 2 else (0.0, None))

    # 从 MFE 最高点到平仓的最大回撤（用收盘价序列）
    # 做多：peak_close = 区间最高收盘，回撤 = (peak_close - exit_price)/peak_close
    # 做空：trough_close = 区间最低收盘，回撤 = (exit_price - trough_close)/exit_price
    if peak_idx < exit_idx:
        seg = closes[peak_idx:exit_idx + 1]
        if direction == "long":
            peak_close = float(seg.max())
            dd_peak_to_exit = (peak_close - exit_price) / peak_close if peak_close > 0 else 0.0
        else:
            trough_close = float(seg.min())
            dd_peak_to_exit = (exit_price - trough_close) / exit_price if exit_price > 0 else 0.0
    else:
        dd_peak_to_exit = 0.0

    return LegResult(
        signal_time=signal.time,
        direction=direction,
        breakthrough=breakthrough,
        entry_kama=entry_kama,
        exit_time=times[exit_idx],
        exit_price=exit_price,
        final_return=final_return,
        max_excursion=float(max_exc) if max_exc != -np.inf else 0.0,
        duration_bars=duration_bars,
        duration_hours=duration_hours,
        dd1=dd1, dd1_time=dd1_t,
        dd2=dd2, dd2_time=dd2_t,
        dd3=dd3, dd3_time=dd3_t,
        exited_by_stop=exited_by_stop,
        dd_peak_to_exit=dd_peak_to_exit,
        breakeven_active=breakeven_active,
        exit_type=exit_type,
    )
