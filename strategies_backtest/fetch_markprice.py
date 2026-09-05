"""拉取标记价K线（1d + 1m），存为parquet缓存。
币安 /fapi/v1/markPriceKlines，ccxt方法 fapiPublicGetMarkPriceKlines。
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ccxt, numpy as np, pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("G:/my-crypto-bot/.env")
proxy = os.getenv("BINANCE_PROXY", "").strip()
opts = {"enableRateLimit": True, "timeout": 30000, "options": {"defaultType": "future"}}
if proxy: opts["proxies"] = {"http": proxy, "https": proxy}
ex = ccxt.binanceusdm(opts)

cache = Path("G:/my-crypto-bot/strategies_backtest/cache")
SINCE = ex.parse8601("2019-12-23T00:00:00Z")  # 标记价功能上线
TF_MS = {"1m": 60_000, "1d": 86_400_000}

def fetch_markprice(symbol, interval, since_ms, limit=1000, save_path=None, save_every=100_000):
    """拉取标记价K线，支持断点续传+分批保存+超时重试。"""
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
            new_df = pd.DataFrame([r[:6] for r in rows], columns=["open_time_ms", "open", "high", "low", "close", "volume"])
            new_df["open_time_ms"] = new_df["open_time_ms"].astype("int64")
            all_df = pd.concat([existing[["open_time_ms","open","high","low","close","volume"]], new_df], ignore_index=True) if not existing.empty else new_df
            all_df = all_df.drop_duplicates(subset="open_time_ms", keep="last").sort_values("open_time_ms").reset_index(drop=True)
            all_df["open_time"] = pd.to_datetime(all_df["open_time_ms"], unit="ms", utc=True)
            tmp = save_path.with_name(save_path.name + ".tmp")
            all_df.to_parquet(tmp, index=False)
            tmp.replace(save_path)
            existing = all_df; rows = []
            print(f"  分批保存: {len(all_df)}根", flush=True)
        if len(data) < limit: break
        time.sleep(ex.rateLimit / 1000)
    return rows, existing

def save_parquet(rows, path):
    df = pd.DataFrame([r[:6] for r in rows], columns=["open_time_ms", "open", "high", "low", "close", "volume"])
    df["open_time_ms"] = df["open_time_ms"].astype("int64")
    df["open_time"] = pd.to_datetime(df["open_time_ms"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="open_time_ms", keep="last").sort_values("open_time_ms").reset_index(drop=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)
    print(f"  写入 {path.name}: {len(df)}根  {df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]}")

# 1d标记价（快）
print("拉取标记价 1d...")
_, df_1d = fetch_markprice("BTCUSDT", "1d", SINCE, save_path=cache / "BTCUSDT_1d_mark.parquet", save_every=10_000)
if df_1d is not None and not df_1d.empty:
    print(f"  1d标记价: {len(df_1d)}根  {df_1d['open_time'].iloc[0]} ~ {df_1d['open_time'].iloc[-1]}")

# 1m标记价（慢，~350万根）
print("\n拉取标记价 1m（耗时较长）...")
_, df_1m = fetch_markprice("BTCUSDT", "1m", SINCE, save_path=cache / "BTCUSDT_1m_mark.parquet")
if df_1m is not None and not df_1m.empty:
    print(f"  1m标记价: {len(df_1m)}根  {df_1m['open_time'].iloc[0]} ~ {df_1m['open_time'].iloc[-1]}")

print("\n完成")
