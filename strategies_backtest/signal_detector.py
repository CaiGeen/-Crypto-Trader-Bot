"""6K 突破信号检测（双向）。"""
from dataclasses import dataclass
from typing import List

import pandas as pd


@dataclass
class Signal:
    idx: int            # 信号 K 线在 df 中的整数位置
    time: object        # 信号 K 线时间
    direction: str      # 'long' / 'short'
    breakthrough: float  # 被突破的 6K 区间极值（入场基准价）
    close: float        # 信号 K 线收盘价


def detect_breakout(df: pd.DataFrame, window: int = 6) -> List[Signal]:
    """检测收盘价突破前 window 根 K 线极值的双向信号。

    向上突破：close[i] > max(high[i-window .. i-1])  → 做多
    向下突破：close[i] < min(low[i-window .. i-1])  → 做空
    窗口不含当前根（标准突破定义）。
    """
    prev_high = df["high"].rolling(window).max().shift(1)
    prev_low = df["low"].rolling(window).min().shift(1)

    signals: List[Signal] = []
    for i in range(len(df)):
        ph = prev_high.iloc[i]
        pl = prev_low.iloc[i]
        if pd.isna(ph) or pd.isna(pl):
            continue
        c = df["close"].iloc[i]
        if c > ph:
            signals.append(Signal(idx=i, time=df["open_time"].iloc[i],
                                  direction="long", breakthrough=float(ph), close=float(c)))
        elif c < pl:
            signals.append(Signal(idx=i, time=df["open_time"].iloc[i],
                                  direction="short", breakthrough=float(pl), close=float(c)))
    return signals
