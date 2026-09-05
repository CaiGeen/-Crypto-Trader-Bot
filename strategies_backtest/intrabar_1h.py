"""用1小时K线消解4H成交当根的"先触发还是先破止损线"路径歧义。

背景：入场挂单在触发线（回调线±1ATR），止损挂在回调线。若成交当根4H K线
的最高价≥触发线且最低价≤止损线，仅凭4H OHLC无法判定成交前后的顺序。
本模块加载全量1H K线，按1H顺序定位成交时刻，判定止损/止盈是否在成交之后发生。

判定规则（1H粒度，剩余歧义按开盘价位置裁决）：
  - 依1H顺序找第一根 high≥trig（做多）的K = 成交所在1H
  - 成交那根1H内：开盘<trig且开盘≥cb（介于两线之间）→ 无法判定，保守算止损
                  开盘≥trig（开盘即已越过触发线）→ 其后的低点属成交后 → 算止损
                  开盘<cb（开盘已在止损线外）→ 低点大概率在成交前 → 不算止损
  - 成交之后的1H：low<cb → 止损成立；high≥tp → 止盈成立
"""
import time
from pathlib import Path

import numpy as np
import pandas as pd

from config import BacktestConfig

_H1 = None  # {"ms": dict[int,int], "open": arr, "high": arr, "low": arr}

def load_h1(config: BacktestConfig = None, refresh: bool = False):
    """加载/缓存全量1H K线，返回 (ms_index_dict, open, high, low)。"""
    global _H1
    if _H1 is not None and not refresh:
        return _H1
    cfg = config or BacktestConfig()
    from data_loader import load_klines
    df1 = load_klines(cfg, refresh=refresh, timeframe="1h")
    ms = df1["open_time_ms"].to_numpy()
    _H1 = (dict(zip(ms.tolist(), range(len(ms)))),
           df1["open"].to_numpy(), df1["high"].to_numpy(), df1["low"].to_numpy())
    return _H1

def resolve_fill_bar(bar_ms, is_long, trig, cb, tp):
    """对4H成交根（open_time毫秒）做1H粒度判定。返回 (当根止损, 当根止盈)。"""
    return resolve_fill_bar_tf(bar_ms, is_long, trig, cb, tp, tf="1h")

_TF_MS = {"1h": 3_600_000, "3m": 180_000, "5m": 300_000, "1m": 60_000}
_TF_CACHE = {}

def load_tf(tf: str, page_limit: int = 1000):
    """加载任意周期的全量K线（缓存parquet，增量续传），返回 (ms索引dict, open, high, low)。"""
    if tf in _TF_CACHE:
        return _TF_CACHE[tf]
    cfg = BacktestConfig()
    cfg.page_limit = page_limit
    from data_loader import load_klines
    df1 = load_klines(cfg, timeframe=tf)
    ms = df1["open_time_ms"].to_numpy()
    out = (dict(zip(ms.tolist(), range(len(ms)))),
           df1["open"].to_numpy(), df1["high"].to_numpy(), df1["low"].to_numpy())
    _TF_CACHE[tf] = out
    return out

def resolve_fill_bar_tf(bar_ms, is_long, trig, cb, tp, tf="1h"):
    """对4H成交根做任意周期粒度判定（3m/5m/1h），逻辑与1H版一致。"""
    idx_map, o1, h1, l1 = load_tf(tf)
    step = _TF_MS[tf]
    per_4h = 14_400_000 // step
    base = int(bar_ms)
    stopped = False
    filled = False
    for k in range(per_4h):
        idx = idx_map.get(base + k * step)
        if idx is None:
            continue
        if not filled:
            trig_hit = (h1[idx] >= trig) if is_long else (l1[idx] <= trig)
            if not trig_hit:
                continue
            filled = True
            o = o1[idx]
            if is_long:
                open_below_cb = o < cb
                open_above_trig = o >= trig
                stop_hit = l1[idx] < cb
            else:
                open_below_cb = o > cb
                open_above_trig = o <= trig
                stop_hit = l1[idx] > cb
            if stop_hit and not open_below_cb:
                # 开盘在止损线内侧（cb≤o<trig或开盘越过触发线）：触发后的回落 → 止损成立
                stopped = True
                break
            tp_hit = (h1[idx] >= tp) if is_long else (l1[idx] <= tp)
            if tp_hit:
                return False, True
        else:
            if is_long:
                if l1[idx] < cb:
                    stopped = True; break
                if h1[idx] >= tp:
                    return False, True
            else:
                if l1[idx] > cb:
                    stopped = True; break
                if h1[idx] <= tp:
                    return False, True
    return stopped, False
