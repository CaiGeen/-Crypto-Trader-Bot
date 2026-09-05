"""波段统计：前 3 大回撤。"""
from typing import List, Tuple

import numpy as np


def top3_drawdowns(returns: np.ndarray, times: List) -> List[Tuple[float, object]]:
    """计算持仓期间前 3 大回撤及其发生时间。

    回撤基于累计收益序列 r（从入场起每根 K 线的累计收益率）：
        peak = cummax(r)                       # 运行峰值收益
        dd   = (peak - r) / (1 + peak)         # 从峰值回落的相对幅度
    分母用 (1+peak) 等价于价格尺度 (peak_price - close)/peak_price，
    避免收益尺度下分母为负的问题（只要价格 > 0 即成立）。

    独立回撤事件：dd 创历史新高时记一次（每个回撤谷底只记一次）。
    返回 [(dd_value, time), ...]，按 dd 降序，最多 3 项。
    """
    r = np.asarray(returns, dtype=float)
    if len(r) == 0:
        return []

    peak = np.maximum.accumulate(r)
    denom = 1.0 + peak
    dd = np.where(denom > 0, (peak - r) / denom, 0.0)

    events: List[Tuple[float, object]] = []
    cur_max = -1.0
    for i, d in enumerate(dd):
        if d > cur_max:
            cur_max = d
            events.append((float(d), times[i]))
    events.sort(key=lambda x: -x[0])
    return events[:3]
