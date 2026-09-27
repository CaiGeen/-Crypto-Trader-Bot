# 行情缓存目录（已剥离大二进制）

本目录原含 Binance 行情缓存 `*.parquet`（1m/3m/4h/1d 等 K 线、mark、OI、spot 等），
以及回测图表 `*.png`。这些文件体积大（单文件最大 167MB，合计约 330MB），
且**完全可由回测脚本重新下载/生成**，不属于「测试资产/需求资料」。

因此在 `frozen-archive` 归档中已剥离上述 `*.parquet` 与 `*.png`，
仅保留研究脚本（`.py`）与结果产物（`.csv`/`.json` 等文本）。

如需恢复缓存：运行对应回测数据拉取脚本即可重建本目录内容。

## 标记价数据（增量更新）

`BTCUSDT_<周期>_mark.parquet` 由 `../fetch_markprice.py` 写入（币安 `/fapi/v1/markPriceKlines` 口径，
**不是**最新成交价——`BTCUSDT_4h.parquet` 才是成交价）。断点续传，重复执行同一条命令即可增量更新：

```powershell
cd ..\strategies_backtest
python fetch_markprice.py 4h --csv --xlsx
```

其他脚本请用 `config.BacktestConfig().mark_cache_path(tf)` 取路径，勿硬编码文件名。
详见 `../PROJECT_OVERVIEW.md` §11.7。

