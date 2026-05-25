"""
DataFeed 实现 — 实盘 + 回测数据馈送

ExchangeDataFeed: 实盘模式，通过 ccxt 连接交易所
BacktestDataFeed: 回测模式，从历史数据 DataFrame 模拟实时馈送
CachedDataFeed:   带缓存的实盘馈送，减少 API 调用
"""

from __future__ import annotations

import time
import logging
from typing import Dict, List, Any, Optional

import numpy as np
import pandas as pd

from strategies.base import DataFeed

logger = logging.getLogger("data.feeds")


class ExchangeDataFeed(DataFeed):
    """
    实盘数据馈送 — 通过 ccxt 交易所实例获取实时数据。

    特性:
      - 自动重试（3次）
      - API 调用限速
      - 可选缓存层
    """

    def __init__(self, exchange=None, cache_ttl_sec: int = 60):
        self._exchange = exchange
        self._cache: Dict[str, tuple] = {}  # key -> (data, timestamp)
        self._cache_ttl = cache_ttl_sec

    def _get_exchange(self):
        if self._exchange is None:
            from exchange_manager import get_binance
            self._exchange = get_binance()
        return self._exchange

    def _cached(self, key: str):
        """检查缓存"""
        if key in self._cache:
            data, ts = self._cache[key]
            if time.time() - ts < self._cache_ttl:
                return data
        return None

    def _set_cache(self, key: str, data):
        self._cache[key] = (data, time.time())

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 50) -> List[List[float]]:
        cache_key = f"ohlcv:{symbol}:{timeframe}:{limit}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        exchange = self._get_exchange()
        for attempt in range(3):
            try:
                data = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                self._set_cache(cache_key, data)
                return data
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"get_ohlcv 失败 ({symbol} {timeframe}): {e}")
                    return []
                time.sleep(0.5)
        return []

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        cache_key = f"ticker:{symbol}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        exchange = self._get_exchange()
        try:
            ticker = exchange.fetch_ticker(symbol)
            self._set_cache(cache_key, ticker)
            return ticker
        except Exception as e:
            logger.warning(f"get_ticker 失败 ({symbol}): {e}")
            return {}

    def get_tickers(self) -> Dict[str, Dict[str, Any]]:
        cache_key = "tickers:all"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        exchange = self._get_exchange()
        try:
            tickers = exchange.fetch_tickers()
            self._set_cache(cache_key, tickers)
            return tickers
        except Exception as e:
            logger.warning(f"get_tickers 失败: {e}")
            return {}

    def get_funding_rate(self, symbol: str) -> float:
        cache_key = f"funding:{symbol}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        try:
            import requests
            sym = symbol.replace('/USDT', 'USDT').replace('/', '')
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                params={"symbol": sym}, timeout=5,
            )
            if r.status_code == 200:
                rate = float(r.json().get('lastFundingRate', 0)) * 100
                self._set_cache(cache_key, rate)
                return rate
        except Exception:
            pass
        return 0.0

    def get_oi_change(self, symbol: str) -> float:
        cache_key = f"oi:{symbol}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        try:
            import requests
            sym = symbol.replace('/USDT', 'USDT').replace('/', '')
            r = requests.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": sym, "period": "1h", "limit": 25},
                timeout=5,
            )
            if r.status_code == 200 and r.json():
                hist = r.json()
                if len(hist) >= 2:
                    oi_now = float(hist[-1].get('sumOpenInterest', 0))
                    oi_ago = float(hist[0].get('sumOpenInterest', 0))
                    if oi_ago > 0:
                        change = (oi_now - oi_ago) / oi_ago
                        self._set_cache(cache_key, change)
                        return change
        except Exception:
            pass
        return 0.0

    def get_orderbook(self, symbol: str, depth: int = 20) -> Dict[str, Any]:
        exchange = self._get_exchange()
        try:
            return exchange.fetch_order_book(symbol, limit=depth)
        except Exception as e:
            logger.warning(f"get_orderbook 失败 ({symbol}): {e}")
            return {'bids': [], 'asks': []}


class BacktestDataFeed(DataFeed):
    """
    回测数据馈送 — 从预加载的 DataFrame 模拟实时数据访问。

    用法:
      df = pd.read_csv('PEPE_1h.csv')
      feed = BacktestDataFeed({'PEPE/USDT': df})
      # 策略调用 feed.get_ohlcv('PEPE/USDT', '1h', limit=50) 时
      # 返回截止到当前 bar 的历史数据
    """

    def __init__(
        self,
        datasets: Dict[str, pd.DataFrame],
        current_bar: int = -1,
        funding_rates: Optional[Dict[str, float]] = None,
        oi_changes: Optional[Dict[str, float]] = None,
    ):
        self._datasets = datasets
        self._current_bar = current_bar  # -1 = 使用全部数据
        self._funding = funding_rates or {}
        self._oi = oi_changes or {}
        self._tickers_cache: Optional[Dict] = None

    def set_bar(self, bar: int):
        """设置当前 bar 位置（用于逐 bar 回测模式）"""
        self._current_bar = bar
        self._tickers_cache = None

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 50) -> List[List[float]]:
        df = self._datasets.get(symbol)
        if df is None:
            return []

        if self._current_bar >= 0:
            end = min(self._current_bar + 1, len(df))
        else:
            end = len(df)

        start = max(0, end - limit)
        subset = df.iloc[start:end]

        # 转换为 [[ts, open, high, low, close, volume], ...]
        result = []
        for _, row in subset.iterrows():
            result.append([
                row.get('timestamp', 0),
                row['open'], row['high'], row['low'], row['close'],
                row.get('volume', 0),
            ])
        return result

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        df = self._datasets.get(symbol)
        if df is None:
            return {}

        bar = self._current_bar if self._current_bar >= 0 else len(df) - 1
        bar = min(bar, len(df) - 1)
        row = df.iloc[bar]

        return {
            'last': row['close'],
            'high': row['high'],
            'low': row['low'],
            'bid': row['close'] * 0.999,
            'ask': row['close'] * 1.001,
            'quoteVolume': row.get('volume', 0) * row['close'],
            'percentage': 0.0,
        }

    def get_tickers(self) -> Dict[str, Dict[str, Any]]:
        if self._tickers_cache is not None:
            return self._tickers_cache

        tickers = {}
        for symbol in self._datasets:
            tickers[symbol] = self.get_ticker(symbol)
        self._tickers_cache = tickers
        return tickers

    def get_funding_rate(self, symbol: str) -> float:
        return self._funding.get(symbol, 0.01)

    def get_oi_change(self, symbol: str) -> float:
        return self._oi.get(symbol, 0.1)

    def get_orderbook(self, symbol: str, depth: int = 20) -> Dict[str, Any]:
        ticker = self.get_ticker(symbol)
        price = ticker.get('last', 1.0)
        # 模拟订单簿
        bids = [[price * (1 - i * 0.001), 1000] for i in range(depth)]
        asks = [[price * (1 + i * 0.001), 1000] for i in range(depth)]
        return {'bids': bids, 'asks': asks}
