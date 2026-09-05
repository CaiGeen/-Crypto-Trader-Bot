"""回测参数配置。"""
from dataclasses import dataclass
from pathlib import Path


def symbol_stem(symbol: str, market: str = "perp") -> str:
    """缓存文件名前缀。BTC/USDT:USDT → BTCUSDT（不重复结算币）。"""
    s = str(symbol or "").upper()
    if "/" in s:
        base, rest = s.split("/", 1)
        quote = rest.split(":", 1)[0]
        stem = (base + quote).replace("_", "")
    else:
        stem = s.replace("/", "").replace(":", "").replace("_", "")
    if market == "spot":
        return f"{stem}_spot"
    return stem


@dataclass
class BacktestConfig:
    # —— 数据源 ——
    # ccxt binanceusdm 永续合约统一符号
    symbol: str = "BTC/USDT:USDT"
    timeframe: str = "4h"
    # 永续上线约 2019-09，全历史起点
    since: str = "2019-09-01T00:00:00Z"
    # 币安 fapi klines 单页上限（永续 4h 实测单页返回 1000）
    page_limit: int = 1000

    # —— 信号参数 ——
    # 突破窗口：close[i] 突破前 window 根的极值（不含当前根）
    window: int = 6

    # —— KAMA 参数（TradingView 实测：erLen=10, fast=2, slow=30）——
    kama_er_len: int = 10
    kama_fast: int = 2
    kama_slow: int = 30

    # —— 路径（相对本包目录）——
    cache_dir: str = "cache"
    results_dir: str = "results"

    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent

    def cache_stem(self, market: str = "perp") -> str:
        return symbol_stem(self.symbol, market)

    def canonical_cache_path(self, timeframe: str | None = None, market: str = "perp") -> Path:
        """规范文件名：BTCUSDT_4h.parquet（永续）/ BTCUSDT_spot_4h.parquet（现货）。"""
        tf = timeframe or self.timeframe
        return self.base_dir / self.cache_dir / f"{self.cache_stem(market)}_{tf}.parquet"

    def legacy_cache_path(self, timeframe: str | None = None) -> Path:
        """旧错误命名：BTC/USDT:USDT 去分隔符 → BTCUSDTUSDT_4h.parquet。"""
        tf = timeframe or self.timeframe
        safe_sym = self.symbol.replace("/", "").replace(":", "")
        return self.base_dir / self.cache_dir / f"{safe_sym}_{tf}.parquet"

    def resolve_cache_path(self, timeframe: str | None = None, market: str = "perp") -> Path:
        """读缓存：优先规范名，其次旧名。写缓存请用 canonical_cache_path。"""
        canon = self.canonical_cache_path(timeframe, market)
        if canon.exists():
            return canon
        if market == "perp":
            legacy = self.legacy_cache_path(timeframe)
            if legacy.exists():
                return legacy
        return canon

    def cache_path_for(self, timeframe: str, market: str = "perp") -> Path:
        return self.resolve_cache_path(timeframe, market)

    @property
    def cache_path(self) -> Path:
        return self.resolve_cache_path(self.timeframe)

    @property
    def results_path(self) -> Path:
        return self.base_dir / self.results_dir
