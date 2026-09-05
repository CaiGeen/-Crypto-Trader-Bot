"""拉取 BTCUSDT 永续 资金费率全历史 + 持仓量OI（data.binance.vision metrics）。

- 资金费率：ccxt fetch_funding_rate_history（/fapi/v1/fundingRate，公开，8h结算一次）
  → cache/funding_BTCUSDT.csv（ts_ms, rate 十进制）
- 持仓量：data.binance.vision 月度 metrics 包（sum_open_interest, BTC单位）。
  币安 API 的 openInterestHist 仅保留30天，全历史只能用官方公开数据转储。
  覆盖范围以实际探测为准（约2021年起），早期无数据。
"""
import io
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import requests
from dotenv import load_dotenv

from config import BacktestConfig

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(HERE.parent / ".env")
proxy = os.getenv("BINANCE_PROXY", "").strip()
proxies = {"http": proxy, "https": proxy} if proxy else None

cfg = BacktestConfig()

# ============ 资金费率（每次全量重拉：仅~9页请求，保证最新结算入缓存） ============
from data_loader import _build_exchange
ex = _build_exchange(cfg)
since = ex.parse8601("2019-09-01T00:00:00Z")
rows = []
while True:
    batch = ex.fetch_funding_rate_history(cfg.symbol, since=since, limit=1000)
    if not batch:
        break
    for f in batch:
        rate = f.get("info", {}).get("fundingRate")
        rows.append((int(f["timestamp"]), float(rate)))
    since = batch[-1]["timestamp"] + 1
    if len(batch) < 1000:
        break
    time.sleep(ex.rateLimit / 1000)
fd = (pd.DataFrame(rows, columns=["ts", "rate"])
      .drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
fd.to_csv(HERE / "cache" / "funding_BTCUSDT.csv", index=False)
print(f"资金费率: {len(fd)} 条  {datetime.fromtimestamp(fd['ts'].iloc[0]/1000, tz=timezone.utc):%Y-%m-%d}"
      f" ~ {datetime.fromtimestamp(fd['ts'].iloc[-1]/1000, tz=timezone.utc):%Y-%m-%d}")
print(f"  rate 中位={fd['rate'].median():.6f}  P5={fd['rate'].quantile(.05):.6f}  "
      f"P95={fd['rate'].quantile(.95):.6f}  min={fd['rate'].min():.6f}  max={fd['rate'].max():.6f}")

# ============ 持仓量 OI（data.binance.vision daily metrics，2020-09-01 起，5分钟粒度） ============
from concurrent.futures import ThreadPoolExecutor

base_d = "https://data.binance.vision/data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-{d}.zip"
days = pd.date_range(pd.Timestamp("2020-09-01", tz="UTC"),
                     pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1), freq="D")
sess = requests.Session()
if proxies:
    sess.proxies.update(proxies)


def grab(day):
    url = base_d.format(d=day.strftime("%Y-%m-%d"))
    try:
        r = sess.get(url, proxies=proxies, timeout=60)
        if r.status_code != 200:
            return None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            csvb = z.read(z.namelist()[0])
        d = pd.read_csv(io.BytesIO(csvb), usecols=["create_time", "sum_open_interest"])
        d["ts"] = pd.to_datetime(d["create_time"], utc=True).to_numpy(dtype="datetime64[ms]").astype("int64")
        d = d[d["sum_open_interest"] > 0]
        return d.rename(columns={"sum_open_interest": "oi"})[["ts", "oi"]]
    except Exception:
        return None


with ThreadPoolExecutor(max_workers=8) as pool:
    frames = [f for f in pool.map(grab, days) if f is not None]
if frames:
    oi = (pd.concat(frames).drop_duplicates("ts").sort_values("ts").reset_index(drop=True))
    oi.to_parquet(HERE / "cache" / "metrics_oi_BTCUSDT.parquet", index=False)
    print(f"持仓量OI: {len(oi)} 条（5分钟粒度）  "
          f"{datetime.fromtimestamp(oi['ts'].iloc[0]/1000, tz=timezone.utc):%Y-%m-%d %H:%M}"
          f" ~ {datetime.fromtimestamp(oi['ts'].iloc[-1]/1000, tz=timezone.utc):%Y-%m-%d %H:%M}  "
          f"成功 {len(frames)}/{len(days)} 天")
    print(f"  oi(BTC) 中位={oi['oi'].median():.0f}  min={oi['oi'].min():.0f}  max={oi['oi'].max():.0f}")
else:
    print("OI 下载全部失败")
