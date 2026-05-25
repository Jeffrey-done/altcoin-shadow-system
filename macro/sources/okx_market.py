"""
OKX 市场数据采集
移植自 multi-signal/scripts/okx/fetch.sh

获取: BTC 价格、1H/3H K线变化、订单簿多空比、Funding Rate、日内波动
"""

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger("macro.sources.okx_market")

_BASE = "https://www.okx.com/api/v5"
_TIMEOUT = 10


@dataclass
class OKXMarketResult:
    btc_price: float = 0.0
    btc_24h_change: float = 0.0       # %
    kline_1h_change: float = 0.0      # %
    kline_3h_change: float = 0.0      # %
    inday_volatility: float = 0.0     # %
    order_book_ratio: float = 1.0     # bid_vol / ask_vol
    funding_rate: float = 0.0         # %
    momentum_score: int = 0           # -4 ~ +4
    trend_score: int = 0              # -4 ~ +4
    structure_score: int = 0          # -2 ~ +2
    error: str = ''


def fetch_okx_market(timeout: int = _TIMEOUT) -> OKXMarketResult:
    """获取 OKX BTC 多维度市场数据并计算评分。"""
    r = OKXMarketResult()

    # 1. Ticker
    try:
        resp = requests.get(f"{_BASE}/market/ticker", params={"instId": "BTC-USDT"}, timeout=timeout)
        if resp.status_code == 200:
            d = resp.json().get("data", [{}])[0]
            r.btc_price = float(d.get("last", 0))
            sod = float(d.get("sodUtc0", 0) or 0)
            high = float(d.get("high24h", 0) or 0)
            low = float(d.get("low24h", 0) or 0)
            if sod > 0:
                r.btc_24h_change = round((r.btc_price - sod) / sod * 100, 4)
            if low > 0:
                r.inday_volatility = round((high - low) / low * 100, 4)
    except Exception as e:
        r.error = str(e)
        logger.warning(f"OKX ticker 失败: {e}")
        return r

    # 2. K线 1H
    try:
        resp = requests.get(f"{_BASE}/market/history-candles",
                           params={"instId": "BTC-USDT", "bar": "1H", "limit": "3"}, timeout=timeout)
        if resp.status_code == 200:
            candles = resp.json().get("data", [])
            if len(candles) >= 2:
                last_c = float(candles[0][4])
                prev1_c = float(candles[1][4])
                if prev1_c > 0:
                    r.kline_1h_change = round((last_c - prev1_c) / prev1_c * 100, 4)
            if len(candles) >= 3:
                prev2_c = float(candles[2][4])
                last_c = float(candles[0][4])
                if prev2_c > 0:
                    r.kline_3h_change = round((last_c - prev2_c) / prev2_c * 100, 4)
    except Exception as e:
        logger.debug(f"OKX K线失败: {e}")

    # 3. 订单簿
    try:
        resp = requests.get(f"{_BASE}/market/books", params={"instId": "BTC-USDT", "sz": "20"}, timeout=timeout)
        if resp.status_code == 200:
            book = resp.json().get("data", [{}])[0]
            bids = book.get("bids", [])[:10]
            asks = book.get("asks", [])[:10]
            bid_vol = sum(float(b[1]) for b in bids) if bids else 0
            ask_vol = sum(float(a[1]) for a in asks) if asks else 1
            r.order_book_ratio = round(bid_vol / ask_vol, 4) if ask_vol > 0 else 1.0
    except Exception as e:
        logger.debug(f"OKX 订单簿失败: {e}")

    # 4. Funding Rate
    try:
        resp = requests.get(f"{_BASE}/market/funding-rate", params={"instId": "BTC-USDT-SWAP"}, timeout=timeout)
        if resp.status_code == 200:
            fr = resp.json().get("data", [{}])[0].get("fundingRate", "0")
            r.funding_rate = round(float(fr) * 100, 6)  # 转为百分比
    except Exception as e:
        logger.debug(f"OKX funding 失败: {e}")

    # 5. 评分计算
    # 动量 (-4 ~ +4)
    if r.kline_1h_change > 0.2:
        r.momentum_score += 1
    elif r.kline_1h_change < -0.2:
        r.momentum_score -= 1
    if r.kline_3h_change > 0.5:
        r.momentum_score += 1
    elif r.kline_3h_change < -0.5:
        r.momentum_score -= 1
    if r.btc_24h_change > 1:
        r.momentum_score += 1
    elif r.btc_24h_change < -1:
        r.momentum_score -= 1

    # 趋势 (-4 ~ +4)
    same_dir = (r.kline_1h_change > 0 and r.kline_3h_change > 0) or \
               (r.kline_1h_change < 0 and r.kline_3h_change < 0)
    if same_dir:
        r.trend_score = 2 if r.kline_1h_change > 0 else -2
    if r.inday_volatility > 3:
        r.trend_score *= 2

    # 结构 (-2 ~ +2)
    if r.order_book_ratio > 1.3:
        r.structure_score = 2
    elif r.order_book_ratio > 1.1:
        r.structure_score = 1
    elif r.order_book_ratio < 0.7:
        r.structure_score = -2
    elif r.order_book_ratio < 0.9:
        r.structure_score = -1

    logger.info(
        f"[OKX] BTC=${r.btc_price:.0f} 1h={r.kline_1h_change:+.2f}% "
        f"book={r.order_book_ratio:.2f} FR={r.funding_rate:.4f}%"
    )
    return r
