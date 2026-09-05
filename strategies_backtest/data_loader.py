"""K 线数据加载：ccxt 拉取 + parquet 增量缓存。

写入规则：
  - upsert：按 open_time_ms 合并，新数据覆盖旧行（keep=last）
  - 丢弃未收盘：磁盘只保存已收盘 K；增量从最后一根已收盘 K 起（含自身）重拉
  - 文件名：BTC/USDT:USDT → BTCUSDT_{tf}.parquet（不再写成 BTCUSDTUSDT_*）

复用项目根目录 .env 的 BINANCE_PROXY。公开 K 线无需 API Key。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import ccxt
import pandas as pd
from dotenv import load_dotenv

from config import BacktestConfig

TF_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

OHLCV_COLS = ["open_time_ms", "open", "high", "low", "close", "volume"]


def _build_exchange(config: BacktestConfig) -> ccxt.Exchange:
    root_env = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(dotenv_path=root_env)
    proxy = os.getenv("BINANCE_PROXY", "").strip()
    opts: dict = {
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "future"},
    }
    if proxy:
        opts["proxies"] = {"http": proxy, "https": proxy}
    return ccxt.binanceusdm(opts)


def _ohlcv_to_df(ohlcv: list) -> pd.DataFrame:
    df = pd.DataFrame(ohlcv, columns=OHLCV_COLS)
    return ensure_open_time(df)


def ensure_open_time(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=OHLCV_COLS + ["open_time"])
    out = df.copy()
    out["open_time_ms"] = out["open_time_ms"].astype("int64")
    if "open_time" not in out.columns:
        out["open_time"] = pd.to_datetime(out["open_time_ms"], unit="ms", utc=True)
    else:
        out["open_time"] = pd.to_datetime(out["open_time"], utc=True)
        if getattr(out["open_time"].dt, "tz", None) is None:
            out["open_time"] = out["open_time"].dt.tz_localize("UTC")
        else:
            out["open_time"] = out["open_time"].dt.tz_convert("UTC")
    return out


def bar_close_ms(open_time_ms, timeframe: str) -> pd.Series:
    step = TF_MS[timeframe]
    return pd.Series(open_time_ms, dtype="int64") + step


def drop_unclosed_bars(
    df: pd.DataFrame,
    timeframe: str,
    now_ms: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (已收盘, 未收盘)。空表时两者都空。"""
    if df is None or df.empty:
        empty = pd.DataFrame(columns=OHLCV_COLS + ["open_time"])
        return empty, empty
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    closed_at = bar_close_ms(df["open_time_ms"], timeframe)
    mask = closed_at <= now_ms
    return df.loc[mask].copy(), df.loc[~mask].copy()


def upsert_ohlcv(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    """按 open_time_ms upsert：新行覆盖旧行。"""
    frames = [x for x in (existing, incoming) if x is not None and not x.empty]
    if not frames:
        return pd.DataFrame(columns=OHLCV_COLS + ["open_time"])
    out = pd.concat(frames, ignore_index=True)
    out = ensure_open_time(out)
    out = out.drop_duplicates(subset="open_time_ms", keep="last")
    return out.sort_values("open_time_ms").reset_index(drop=True)


def _atomic_to_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def write_cache(
    df: pd.DataFrame,
    canonical: Path,
    legacy: Path | None = None,
) -> None:
    _atomic_to_parquet(df, canonical)
    if legacy is not None:
        try:
            if legacy.exists() and legacy.resolve() != canonical.resolve():
                legacy.unlink()
        except OSError as e:
            print(f"警告：旧缓存未删除 {legacy.name}: {e}")


def read_cache(timeframe: str, market: str = "perp") -> pd.DataFrame:
    """只读本地 parquet（规范名或旧名），不拉网络。"""
    cfg = BacktestConfig()
    path = cfg.resolve_cache_path(timeframe, market)
    if not path.exists():
        raise FileNotFoundError(f"缓存不存在: {path}")
    return ensure_open_time(pd.read_parquet(path))


def _fetch_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    since_ms: int,
    page_limit: int,
) -> list:
    rows: list = []
    while True:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=page_limit)
        if not ohlcv:
            break
        rows.extend(ohlcv)
        since_ms = ohlcv[-1][0] + 1
        if len(rows) % 100_000 < page_limit:
            print(f"  {timeframe} 已拉取 {len(rows)} 根...", flush=True)
        if len(ohlcv) < page_limit:
            break
        time.sleep(exchange.rateLimit / 1000)
    return rows


def load_klines(
    config: BacktestConfig,
    refresh: bool = False,
    timeframe: str | None = None,
    include_unclosed: bool = False,
) -> pd.DataFrame:
    """加载 K 线。默认只返回已收盘 K，并按规范文件名 upsert 写入。

    refresh=True 时忽略缓存全量重拉。
    include_unclosed=True 时内存中附带当前未收盘 K，但仍不写入磁盘。
    """
    tf = timeframe or config.timeframe
    if tf not in TF_MS:
        raise ValueError(f"未知 timeframe: {tf}")

    exchange = _build_exchange(config)
    canonical = config.canonical_cache_path(tf)
    legacy = config.legacy_cache_path(tf)
    read_path = config.resolve_cache_path(tf)

    df_existing = pd.DataFrame()
    if read_path.exists() and not refresh:
        df_existing = ensure_open_time(pd.read_parquet(read_path))

    closed, _stale_unclosed = drop_unclosed_bars(df_existing, tf)
    n_dropped = 0 if df_existing.empty else len(df_existing) - len(closed)
    if n_dropped:
        print(f"缓存丢弃未收盘 {n_dropped} 根（{read_path.name}）")

    if not closed.empty:
        since_ms = int(closed["open_time_ms"].iloc[-1])  # 含最后一根已收盘，便于覆盖
        print(f"缓存命中 {len(closed)} 根，upsert 自 {closed['open_time'].iloc[-1]}")
    else:
        since_ms = exchange.parse8601(config.since)
        print(f"无缓存，全量拉取自 {config.since}")

    all_ohlcv = _fetch_ohlcv(exchange, config.symbol, tf, since_ms, config.page_limit)
    df_new = _ohlcv_to_df(all_ohlcv) if all_ohlcv else pd.DataFrame()
    merged = upsert_ohlcv(closed, df_new)
    to_write, unclosed = drop_unclosed_bars(merged, tf)

    if to_write.empty and closed.empty:
        print("未获取到任何已收盘 K 线")
        return unclosed if include_unclosed else to_write

    write_cache(to_write, canonical, legacy)
    n_new = 0 if to_write.empty or closed.empty else max(0, len(to_write) - len(closed))
    n_upd = len(all_ohlcv)
    print(
        f"拉取 {n_upd} 根，写入已收盘 {len(to_write)} 根 → {canonical.name}"
        + (f"（未收盘 {len(unclosed)} 根未落盘）" if len(unclosed) else "")
        + (f"，净增 {n_new}" if n_new else "")
    )
    if include_unclosed and not unclosed.empty:
        return upsert_ohlcv(to_write, unclosed)
    return to_write


def migrate_cache_files(
    config: BacktestConfig | None = None,
    timeframes: tuple[str, ...] = ("1m", "3m", "1h", "4h", "1d"),
    fetch: bool = False,
) -> list[dict]:
    """把旧 BTCUSDTUSDT_* 迁到 BTCUSDT_*，丢弃未收盘 K。fetch=True 时再增量 upsert。"""
    cfg = config or BacktestConfig()
    reports = []
    for tf in timeframes:
        src = cfg.resolve_cache_path(tf)
        dst = cfg.canonical_cache_path(tf)
        legacy = cfg.legacy_cache_path(tf)
        rec = {
            "timeframe": tf,
            "src": src.name if src.exists() else None,
            "dst": dst.name,
            "ok": False,
            "rows_in": 0,
            "rows_out": 0,
            "dropped_unclosed": 0,
        }
        if not src.exists():
            rec["note"] = "源文件不存在"
            reports.append(rec)
            continue
        df = ensure_open_time(pd.read_parquet(src))
        rec["rows_in"] = len(df)
        closed, unclosed = drop_unclosed_bars(df, tf)
        rec["dropped_unclosed"] = len(unclosed)
        rec["rows_out"] = len(closed)
        write_cache(closed, dst, legacy)
        rec["ok"] = True
        rec["note"] = (
            f"{src.name} → {dst.name}，丢弃未收盘 {len(unclosed)}"
            if src.name != dst.name or len(unclosed)
            else "已是规范名且无未收盘"
        )
        print(rec["note"])
        reports.append(rec)
        if fetch:
            load_klines(cfg, timeframe=tf)
    return reports
