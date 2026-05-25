"""
恐惧贪婪指数采集
合并自 multi-signal/scripts/binance/fetch.sh + CMM data_collector/sentiment.py

数据源: alternative.me (完全免费，无需 API Key)
"""

import logging
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger("macro.sources.fear_greed")

_FG_URL = "https://api.alternative.me/fng/"
_TIMEOUT = 10


@dataclass
class FearGreedResult:
    value: int = 50                # 0-100
    classification: str = 'Neutral'
    yesterday: int = 50
    change: int = 0                # 今天 - 昨天
    signal_score: int = 0          # -2 ~ +2 (反向指标)
    error: str = ''


def fetch_fear_greed(timeout: int = _TIMEOUT) -> FearGreedResult:
    """
    获取恐惧贪婪指数。

    信号逻辑（反向指标）：
      ≤ 20: +2 (极度恐惧 → 反向看多)
      ≤ 35: +1 (恐惧 → 偏多)
      ≥ 75: -2 (极度贪婪 → 反向看空)
      ≥ 60: -1 (贪婪 → 偏空)
    """
    result = FearGreedResult()

    try:
        resp = requests.get(_FG_URL, params={"limit": 2, "format": "json"}, timeout=timeout)
        if resp.status_code != 200:
            result.error = f"HTTP {resp.status_code}"
            return result

        data = resp.json().get("data", [])
        if not data:
            result.error = "empty response"
            return result

        result.value = int(data[0].get("value", 50))
        result.classification = data[0].get("value_classification", "Neutral")

        if len(data) >= 2:
            result.yesterday = int(data[1].get("value", 50))
            result.change = result.value - result.yesterday

    except Exception as e:
        result.error = str(e)
        logger.warning(f"Fear & Greed 获取失败: {e}")
        return result

    # 信号评分（反向指标）
    if result.value <= 20:
        result.signal_score = 2
    elif result.value <= 35:
        result.signal_score = 1
    elif result.value >= 75:
        result.signal_score = -2
    elif result.value >= 60:
        result.signal_score = -1
    else:
        result.signal_score = 0

    logger.info(f"[FG] value={result.value} ({result.classification}) signal={result.signal_score}")
    return result
