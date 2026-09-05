"""技术指标实现。"""
import numpy as np


def kama(close, er_len: int = 10, fast: int = 2, slow: int = 30) -> np.ndarray:
    """考夫曼自适应移动平均线（严格对齐 TradingView ta.kama）。

    TradingView 标准实现：
        fastSC = 2 / (fast + 1)
        slowSC = 2 / (slow + 1)
        change     = |source - source[length]|
        volatility = sum(|source - source[1]|, length)   # 含当前根，共 length 项
        er  = volatility == 0 ? 0 : change / volatility
        sc  = er * (fastSC - slowSC) + slowSC
        kama[i] = na(kama[i-1]) ? source[i] : kama[i-1] + sc^2 * (source[i] - kama[i-1])

    参数对应 TradingView 实测：erLen=10, fast=2, slow=30。
    返回与 close 等长的 ndarray，前 er_len 个为 NaN（数据不足）。
    """
    close = np.asarray(close, dtype=float)
    n = len(close)
    out = np.full(n, np.nan)
    if n < er_len + 1:
        return out

    diff = np.abs(np.diff(close))  # diff[j] = close[j+1] - close[j]
    fast_sc = 2.0 / (fast + 1)
    slow_sc = 2.0 / (slow + 1)

    # 首值：i = er_len 时 kama[i-1] 为 na → kama[i] = close[i]
    out[er_len] = close[er_len]
    for i in range(er_len + 1, n):
        direction = abs(close[i] - close[i - er_len])
        # volatility = Σ|close[j]-close[j-1]|, j ∈ [i-er_len+1, i]
        #            = diff[i-er_len : i]  （共 er_len 项）
        volatility = diff[i - er_len:i].sum()
        er = 0.0 if volatility == 0 else direction / volatility
        sc = er * (fast_sc - slow_sc) + slow_sc
        out[i] = out[i - 1] + sc * sc * (close[i] - out[i - 1])
    return out
