"""标记价 vs 最新价 核对：确认缓存口径 + 量化对信号/波段的实际影响。

事实背景：
  - 实盘 trader.py 挂单 workingType='MARK_PRICE'（触发判定用标记价，触发后市价成交≈最新价）。
  - data_loader.fetch_ohlcv 未带 price 参数 → /fapi/v1/klines 默认最新成交价。
本脚本：
  1. 拉全历史 4H 标记价K线（/fapi/v1/markPriceKlines）存 cache/BTCUSDTUSDT_4h_mark.parquet
  2. 逐K对比 high/low/close 差异（bps），并实测证明缓存=最新价而非标记价
  3. 在标记价上完整重跑 6K信号+KAMA终结波段逻辑，与现数据集逐段对比
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd

from config import BacktestConfig
from data_loader import _build_exchange
from indicators import kama
from signal_detector import detect_breakout

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = BacktestConfig()
mark_path = os.path.join(HERE, "cache", "BTCUSDTUSDT_4h_mark.parquet")

if os.path.exists(mark_path):
    mk = pd.read_parquet(mark_path)
    print(f"标记价K线: 缓存命中 {len(mk)} 根")
else:
    ex = _build_exchange(cfg)
    since = ex.parse8601("2019-09-01T00:00:00Z")
    rows = []
    while True:
        batch = ex.fapiPublicGetMarkPriceKlines({
            "symbol": "BTCUSDT", "interval": "4h", "startTime": since, "limit": 1000})
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(ex.rateLimit / 1000)
    mk = pd.DataFrame([r[:5] for r in rows], columns=["open_time_ms", "open", "high", "low", "close"])
    for k_ in ("open", "high", "low", "close"):
        mk[k_] = mk[k_].astype(float)
    mk = mk.drop_duplicates("open_time_ms").sort_values("open_time_ms").reset_index(drop=True)
    mk.to_parquet(mark_path, index=False)
    print(f"标记价K线: 新拉取 {len(mk)} 根")

last = pd.read_parquet(os.path.join(HERE, "cache", "BTCUSDTUSDT_4h.parquet"))
m_ = mk.merge(last, on="open_time_ms", suffixes=("_mk", "_ls"))
print(f"对齐后 {len(m_)} 根: 标记价 {pd.to_datetime(mk['open_time_ms'].iloc[0], unit='ms')} 起，"
      f"最新价 {pd.to_datetime(last['open_time_ms'].iloc[0], unit='ms')} 起")

# ============ 1. 逐K差异（bps） ============
print("\n===== 标记价 vs 最新价 逐K差异（bps = 差/最新价×1e4） =====")
for col in ("high", "low", "close"):
    d = (m_[f"{col}_mk"] - m_[f"{col}_ls"]) / m_[f"{col}_ls"] * 1e4
    print(f"{col:>5}: |diff| 中位={d.abs().median():.2f}  P99={d.abs().quantile(.99):.2f}  "
          f"max={d.abs().max():.2f}  >5bps占比={(d.abs() > 5).mean():.2%}")

mk["open_time"] = pd.to_datetime(mk["open_time_ms"], unit="ms", utc=True)

# 实证1：最近30根中已收盘的bar，缓存close vs 最新价端点 逐一复检（暴露冻结半根K）
ex = _build_exchange(cfg)
now_ms = int(time.time() * 1000)
fresh = {r[0]: r for r in ex.fetch_ohlcv(cfg.symbol, "4h", limit=30)}
fresh_mk = {r[0]: r for r in ex.fapiPublicGetMarkPriceKlines(
    {"symbol": "BTCUSDT", "interval": "4h", "limit": 30})}
mism, same_as_last, same_as_mk = [], 0, 0
checked = 0
for k_ in range(-30, 0):
    row = last.iloc[k_]
    bt = int(row["open_time_ms"])
    if bt + 4 * 3600 * 1000 > now_ms or bt not in fresh:
        continue
    checked += 1
    cl_c, cl_l, cl_m = float(row["close"]), float(fresh[bt][4]), float(fresh_mk[bt][4])
    if abs(cl_c - cl_l) > 1e-6:
        mism.append((str(row["open_time"]), cl_c, cl_l))
    else:
        same_as_last += 1
        if abs(cl_c - cl_m) > 1e-6:
            same_as_mk += 1
print(f"\n实证（复检最近已收盘 {checked} 根）:")
print(f"  缓存 == 最新价端点 且 ≠ 标记价端点: {same_as_mk} 根 → 缓存确为最新价口径")
print(f"  缓存 ≠ 最新价端点（冻结的半根K污染）: {len(mism)} 根")
for mm in mism[:5]:
    print(f"    {mm[0]}  缓存close={mm[1]}  最终close={mm[2]}")

# ============ 2. 信号/波段级影响 ============
print("\n===== 在标记价上重跑整套波段逻辑 vs 现数据集 =====")
W = cfg.window


def build_legs(df_ohlc):
    kama_v = kama(df_ohlc["close"].to_numpy(), cfg.kama_er_len, cfg.kama_fast, cfg.kama_slow)
    hs = df_ohlc["high"].to_numpy()
    ls_ = df_ohlc["low"].to_numpy()
    cs = df_ohlc["close"].to_numpy()
    n = len(df_ohlc)
    legs = []
    last_exit = {"long": -1, "short": -1}
    for s in detect_breakout(df_ohlc, W):
        d = s.direction
        if s.idx <= last_exit[d] or np.isnan(kama_v[s.idx]):
            continue
        i = s.idx
        entry = s.breakthrough
        ex_ = None
        for j in range(i + 1, n):
            if np.isnan(kama_v[j - 1]):
                continue
            if (d == "long" and cs[j] < kama_v[j - 1]) or (d == "short" and cs[j] > kama_v[j - 1]):
                ex_ = j
                break
        end = ex_ if ex_ is not None else n - 1
        seg = hs[i:end + 1] if d == "long" else ls_[i:end + 1]
        peak = float(seg.max() if d == "long" else seg.min())
        amp = (peak - entry) / entry * 100 if d == "long" else (entry - peak) / entry * 100
        pnl = np.nan
        if ex_ is not None:
            line = float(kama_v[ex_ - 1])
            pnl = (line - entry) / entry * 100 if d == "long" else (entry - line) / entry * 100
        legs.append({"i": i, "d": d, "ex": ex_, "amp": amp, "pnl": pnl})
        last_exit[d] = ex_ if ex_ is not None else n - 1
    return legs


legs_mk = build_legs(mk)
mk_ms = mk["open_time_ms"].to_numpy()
for lg in legs_mk:  # 位置索引 → 时间戳键（两序列起点不同，整数位置不可比）
    lg["anchor_ms"] = int(mk_ms[lg["i"]])
    lg["exit_ms"] = int(mk_ms[lg["ex"]]) if lg["ex"] is not None else None
ref = pd.read_csv(os.path.join(HERE, "results", "单段涨跌幅_6K突破线到KAMA线.csv"))


def to_ms(col):  # UTC+8字符串 → UTC毫秒（本pandas版本astype(int64)单位为µs，须显式转ms）
    return (pd.to_datetime(ref[col]).dt.tz_localize("Asia/Shanghai")
            .dt.tz_convert("UTC").to_numpy(dtype="datetime64[ms]").astype("int64"))


ref["anchor_ms"] = to_ms("突破K时间")
ref["exit_ms"] = to_ms("KAMA终结K时间")
print(f"段数: 现数据集(最新价)={len(ref)}  标记价重跑={len(legs_mk)}")

mk_df = pd.DataFrame(legs_mk).set_index("anchor_ms")
ref_df = ref.set_index("anchor_ms")
common = mk_df.index.intersection(ref_df.index)
print(f"锚点(按时间戳)一致的段: {len(common)} ({len(common)/len(ref_df):.1%})  "
      f"仅标记价有: {len(mk_df.index.difference(ref_df.index))}  "
      f"仅最新价有: {len(ref_df.index.difference(mk_df.index))}")
cm = mk_df.loc[common].join(ref_df.loc[common], rsuffix="_ref")
d_ex = (cm["exit_ms"] != cm["exit_ms_ref"]).sum()
d_amp = (cm["amp"] - cm["涨跌幅%(突破线→波峰)"]).abs()
d_pnl = (cm["pnl"] - cm["盈亏%(突破线→KAMA线)"]).abs()
print(f"终结K不同: {d_ex}/{len(common)} 段")
print(f"涨跌幅差: 中位={d_amp.median():.3f}pp  P99={d_amp.quantile(.99):.3f}pp  max={d_amp.max():.3f}pp  >0.5pp占比={(d_amp > 0.5).mean():.1%}")
ok = cm.dropna(subset=["pnl"])
print(f"盈亏差:   中位={(ok['pnl'] - ok['盈亏%(突破线→KAMA线)']).abs().median():.3f}pp  "
      f"max={(ok['pnl'] - ok['盈亏%(突破线→KAMA线)']).abs().max():.3f}pp")
print(f"标记价口径 涨跌幅中位={mk_df['amp'].median():.2f}% vs 现数据集 {ref_df['涨跌幅%(突破线→波峰)'].median():.2f}%")
