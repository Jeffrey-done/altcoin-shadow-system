"""
Smart Money 信号采集（Binance Web3 API）
移植自 multi-signal/scripts/smartmoney/fetch.sh

追踪 Binance 链上智能货币地址的买入/卖出行为：
  - 大户集中买入 → 机构布局 → 看多
  - 大户集中卖出 → 机构出货 → 看空
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

import requests

logger = logging.getLogger("macro.sources.smart_money")

_SIGNAL_URL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai"
_INFLOW_URL = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query/ai"
_HEADERS = {
    "Content-Type": "application/json",
    "Accept-Encoding": "identity",
    "User-Agent": "altcoin-shadow/1.0",
}
_TIMEOUT = 15


@dataclass
class SmartMoneyResult:
    buy_count: int = 0
    sell_count: int = 0
    total_signals: int = 0
    buy_ratio: float = 0.5
    signal_score: int = 0          # -2 ~ +2
    top_buy: List[str] = None
    top_sell: List[str] = None
    top_inflow_symbol: str = ''
    error: str = ''

    def __post_init__(self):
        if self.top_buy is None:
            self.top_buy = []
        if self.top_sell is None:
            self.top_sell = []


def fetch_smart_money(chain_id: str = "56", timeout: int = _TIMEOUT) -> SmartMoneyResult:
    """
    获取 Smart Money 买卖信号。

    Returns:
      SmartMoneyResult with signal_score:
        +2: buy_ratio > 0.8 (大户集中买入)
        +1: buy_ratio > 0.6
         0: 中性
        -1: buy_ratio < 0.4
        -2: buy_ratio < 0.2 (大户集中卖出)
    """
    result = SmartMoneyResult()

    # 1. 获取买卖信号
    try:
        resp = requests.post(
            _SIGNAL_URL,
            headers=_HEADERS,
            json={"smartSignalType": "", "page": 1, "pageSize": 20, "chainId": chain_id},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            result.total_signals = len(data)
            result.buy_count = sum(1 for d in data if d.get("direction") == "buy")
            result.sell_count = sum(1 for d in data if d.get("direction") == "sell")
            result.top_buy = [d.get("ticker", "") for d in data if d.get("direction") == "buy"][:5]
            result.top_sell = [d.get("ticker", "") for d in data if d.get("direction") == "sell"][:5]
    except Exception as e:
        result.error = str(e)
        logger.warning(f"Smart Money 信号获取失败: {e}")
        return result

    # 2. 获取净流入排名
    try:
        resp2 = requests.post(
            _INFLOW_URL,
            headers=_HEADERS,
            json={"chainId": chain_id, "period": "24h", "tagType": 2},
            timeout=timeout,
        )
        if resp2.status_code == 200:
            inflow_data = resp2.json().get("data", [])
            if inflow_data:
                result.top_inflow_symbol = inflow_data[0].get("tokenName", "")
    except Exception as e:
        logger.debug(f"Smart Money 净流入获取失败: {e}")

    # 3. 计算买入比例和信号
    if result.total_signals > 0:
        result.buy_ratio = round(result.buy_count / result.total_signals, 2)
    else:
        result.buy_ratio = 0.5

    if result.buy_ratio > 0.8:
        result.signal_score = 2
    elif result.buy_ratio > 0.6:
        result.signal_score = 1
    elif result.buy_ratio < 0.2:
        result.signal_score = -2
    elif result.buy_ratio < 0.4:
        result.signal_score = -1
    else:
        result.signal_score = 0

    logger.info(
        f"[SmartMoney] buy={result.buy_count} sell={result.sell_count} "
        f"ratio={result.buy_ratio} signal={result.signal_score}"
    )
    return result
