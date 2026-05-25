"""
多交易所价格对比
移植自 multi-signal/scripts/bitget/fetch.sh

比较 OKX 和 CoinGecko 的 BTC 价格，检测跨平台价差信号。
"""

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger("macro.sources.multi_exchange")

_TIMEOUT = 10


@dataclass
class MultiExchangeResult:
    okx_price: float = 0.0
    cg_price: float = 0.0
    cg_24h_change: float = 0.0      # %
    price_diff_pct: float = 0.0     # OKX vs CG 偏差 %
    price_signal: int = 0           # +1 OKX溢价 / -1 OKX折价 / 0 正常
    error: str = ''


def fetch_multi_exchange(timeout: int = _TIMEOUT) -> MultiExchangeResult:
    """获取多交易所价格对比。"""
    r = MultiExchangeResult()

    # 1. OKX 价格
    try:
        resp = requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": "BTC-USDT"}, timeout=timeout,
        )
        if resp.status_code == 200:
            r.okx_price = float(resp.json().get("data", [{}])[0].get("last", 0))
    except Exception as e:
        logger.debug(f"OKX price 失败: {e}")

    # 2. CoinGecko 价格
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            btc = resp.json().get("bitcoin", {})
            r.cg_price = float(btc.get("usd", 0))
            r.cg_24h_change = float(btc.get("usd_24h_change", 0))
    except Exception as e:
        logger.debug(f"CoinGecko price 失败: {e}")

    # 3. 计算价差
    if r.okx_price > 0 and r.cg_price > 0:
        r.price_diff_pct = round((r.okx_price - r.cg_price) / r.cg_price * 100, 4)

    if r.price_diff_pct > 0.1:
        r.price_signal = 1
    elif r.price_diff_pct < -0.1:
        r.price_signal = -1

    logger.info(f"[MultiEx] OKX=${r.okx_price:.0f} CG=${r.cg_price:.0f} diff={r.price_diff_pct:.3f}%")
    return r
