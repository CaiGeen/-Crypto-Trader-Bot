"""拉取币安 BTCUSDT 永续【标记价】K线（/fapi/v1/markPriceKlines），存 parquet 缓存。

【增量更新】重复执行同一条命令即可：已缓存部分自动跳过，按 open_time_ms 断点续传。
    python fetch_markprice.py 4h --csv --xlsx

【落盘产物】cache/ 下 BTCUSDT_<周期>_mark.{parquet,csv,xlsx}
    - 路径统一由 config.BacktestConfig.mark_cache_path() 解析，其他脚本请用该方法取，勿硬编码文件名
    - parquet：规范缓存，open_time 为 UTC（与 data_loader 各缓存同 schema，仅存已收盘 K）
    - csv/xlsx：--csv/--xlsx 按需导出；时间列注明时区，open_time_ms 保留原始 epoch 毫秒
    - 落盘只保留已收盘K（与 data_loader 规则一致），未收盘K下次续传时补上

【周期】缺省 1d + 1m（历史行为）；可传 1m/3m/5m/15m/1h/4h/1d 任意组合。
【依赖】复用项目根目录 .env 的 BINANCE_PROXY；公开接口无需 API Key。
"""
import argparse
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ccxt, numpy as np, pandas as pd
from pathlib import Path
from dotenv import load_dotenv
from config import BacktestConfig
from data_loader import TF_MS, drop_unclosed_bars

load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # 与 data_loader 同法：按包路径定位 .env，不硬编码绝对路径
proxy = os.getenv("BINANCE_PROXY", "").strip()
opts = {"enableRateLimit": True, "timeout": 30000, "options": {"defaultType": "future"}}
if proxy: opts["proxies"] = {"http": proxy, "https": proxy}
ex = ccxt.binanceusdm(opts)

CFG = BacktestConfig()
cache = CFG.base_dir / CFG.cache_dir  # 与 data_loader/config 同一处解析，不硬编码绝对路径
SINCE = ex.parse8601("2019-12-23T00:00:00Z")  # 标记价功能上线（4h 最早可用 K 即此日 08:00 UTC）
DEFAULT_INTERVALS = ("1d", "1m")
XLSX_MAX_ROWS = 1_048_575  # Excel 单表上限 1048576 行（含表头）
CN_TZ = "Asia/Shanghai"  # 北京时间，固定 UTC+8（1991 后无夏令时，本数据区间适用）

def _save_mark(existing, rows, path, interval):
    """existing + rows 按 open_time_ms upsert 去重排序，只保留已收盘K，原子写盘。"""
    new_df = pd.DataFrame([r[:6] for r in rows], columns=["open_time_ms", "open", "high", "low", "close", "volume"])
    new_df["open_time_ms"] = new_df["open_time_ms"].astype("int64")
    all_df = pd.concat([existing[["open_time_ms","open","high","low","close","volume"]], new_df], ignore_index=True) if not existing.empty else new_df
    all_df = all_df.drop_duplicates(subset="open_time_ms", keep="last").sort_values("open_time_ms").reset_index(drop=True)
    all_df["open_time"] = pd.to_datetime(all_df["open_time_ms"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):  # API 返回字符串，落盘统一为 float64（与 data_loader 缓存同 schema）
        all_df[col] = all_df[col].astype("float64")
    all_df, unclosed = drop_unclosed_bars(all_df, interval)
    if len(unclosed): print(f"  丢弃未收盘 {len(unclosed)} 根（{interval}）", flush=True)
    tmp = path.with_name(path.name + ".tmp")
    all_df.to_parquet(tmp, index=False)
    tmp.replace(path)
    return all_df

def fetch_markprice(symbol, interval, since_ms, limit=1000, save_path=None, save_every=100_000):
    """拉取标记价K线，支持断点续传+分批保存+超时重试。循环结束后补写尾部（否则尾部丢失）。"""
    existing = pd.DataFrame()
    if save_path and save_path.exists():
        existing = pd.read_parquet(save_path)
        if not existing.empty:
            since_ms = int(existing["open_time_ms"].iloc[-1]) + 1
            print(f"  续传自 {existing['open_time'].iloc[-1]}（已有{len(existing)}根）")
    rows = []
    total = len(existing)
    while True:
        for attempt in range(5):
            try:
                data = ex.fapiPublicGetMarkPriceKlines({"symbol": symbol, "interval": interval, "startTime": since_ms, "limit": limit})
                break
            except Exception as e:
                print(f"  重试 {attempt+1}/5: {type(e).__name__}", flush=True)
                time.sleep(3 * (attempt + 1))
        else:
            print(f"  重试5次失败，保存已拉取数据")
            break
        if not data: break
        rows.extend(data)
        since_ms = data[-1][0] + 1
        total += len(data)
        if total % 100_000 < limit:
            print(f"  {interval} 已拉取 {total} 根...", flush=True)
        if save_path and len(rows) >= save_every:
            existing = _save_mark(existing, rows, save_path, interval)
            rows = []
            print(f"  分批保存: {len(existing)}根", flush=True)
        if len(data) < limit: break
        time.sleep(ex.rateLimit / 1000)
    if save_path and rows:
        existing = _save_mark(existing, rows, save_path, interval)
        print(f"  尾部保存: {len(existing)}根", flush=True)
    return rows, existing

def export_table(df, interval, formats):
    """导出 csv/xlsx。

    行情列名与 parquet 一致；open_time_ms 保留原始 epoch 毫秒（UTC 基准，格式不变）。
    时间列一律注明时区：open_time_UTC+0 与 open_time_北京UTC+8，值内也带 +00:00 / +08:00 偏移。
    """
    if not formats:
        return
    out = df.copy()
    out["open_time_UTC+0"] = out["open_time"].dt.strftime("%Y-%m-%d %H:%M:%S+00:00")
    out["open_time_北京UTC+8"] = out["open_time"].dt.tz_convert(CN_TZ).dt.strftime("%Y-%m-%d %H:%M:%S+08:00")
    out = out.drop(columns=["open_time"])
    for fmt in formats:
        path = CFG.mark_cache_path(interval).with_suffix(f".{fmt}")
        if fmt == "xlsx" and len(out) > XLSX_MAX_ROWS:
            print(f"  跳过 {path.name}: {len(out)} 根超过 Excel 单表 {XLSX_MAX_ROWS + 1} 行上限")
            continue
        if fmt == "xlsx":
            out.to_excel(path, index=False)
        else:
            out.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"  导出 {path.name}: {len(out)}行  北京时间 {out['open_time_北京UTC+8'].iloc[0]} ~ {out['open_time_北京UTC+8'].iloc[-1]}")

def main():
    ap = argparse.ArgumentParser(description="拉取币安 BTCUSDT 永续标记价K线")
    ap.add_argument("intervals", nargs="*", default=list(DEFAULT_INTERVALS),
                    help=f"周期，可选 {', '.join(TF_MS)}；缺省 {' '.join(DEFAULT_INTERVALS)}")
    ap.add_argument("--csv", action="store_true", help=f"同时导出 csv（{CFG.mark_cache_path('4h').with_suffix('.csv')}）")
    ap.add_argument("--xlsx", action="store_true", help="同时导出 xlsx（超过 Excel 行上限则跳过）")
    args = ap.parse_args()
    formats = [f for f, on in (("csv", args.csv), ("xlsx", args.xlsx)) if on]
    for interval in args.intervals:
        if interval not in TF_MS:
            raise SystemExit(f"未知周期: {interval}（可选 {', '.join(TF_MS)}）")
        print(f"拉取标记价 {interval}...")
        _, df = fetch_markprice("BTCUSDT", interval, SINCE,
                                save_path=CFG.mark_cache_path(interval),
                                save_every=10_000 if interval in ("1d", "4h") else 100_000)
        if df is not None and not df.empty:
            print(f"  parquet {CFG.mark_cache_path(interval)}: {len(df)}根  UTC {df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]}")
            export_table(df, interval, formats)
    print("\n完成")

if __name__ == "__main__":
    main()
