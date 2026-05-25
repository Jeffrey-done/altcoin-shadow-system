"""
TradingView 多周期技术分析
移植自 multi-signal/scripts/tradingview/fetch.sh 的 Python 内核

获取 BTC/USDT 在 1H/4H/1D 三个周期的：
  - RSI / MACD / ADX / Supertrend / Bollinger Bands
  - 多周期趋势对齐评分 (-2 ~ +2)
  - 综合 TA 评分 (-3 ~ +3)

依赖: tradingview-ta（可选，未安装时返回空结果）
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("macro.sources.tradingview_ta")


@dataclass
class TVAnalysisResult:
    score: int = 0                    # -3 ~ +3 综合 TA 评分
    alignment: int = 0                # -2 ~ +2 多周期对齐
    rsi_1h: float = 50.0
    rsi_4h: float = 50.0
    rsi_1d: float = 50.0
    supertrend_1h: int = 0            # +1 多 / -1 空
    supertrend_4h: int = 0
    supertrend_1d: int = 0
    macd_1h: int = 0
    bb_position: float = 0.5          # 0=下轨 1=上轨
    bb_zone: str = 'neutral'          # oversold / overbought / neutral
    recommendation_1h: str = 'NEUTRAL'
    available: bool = False
    error: str = ''


def fetch_tradingview_analysis(symbol: str = "BTCUSDT",
                               exchange: str = "BINANCE",
                               timeout: int = 30) -> TVAnalysisResult:
    """
    获取 TradingView 多周期技术分析。

    如果 tradingview-ta 未安装，返回 available=False 的空结果（不阻塞主流程）。
    """
    result = TVAnalysisResult()

    try:
        from tradingview_ta import TA_Handler, Interval
    except ImportError:
        result.error = "tradingview-ta not installed"
        logger.debug("tradingview-ta 未安装，跳过 TV 分析")
        return result

    intervals = [
        ('1h', Interval.INTERVAL_1_HOUR),
        ('4h', Interval.INTERVAL_4_HOURS),
        ('1d', Interval.INTERVAL_1_DAY),
    ]

    multi_data = {}
    try:
        for name, interval in intervals:
            handler = TA_Handler(
                symbol=symbol, exchange=exchange,
                screener="crypto", interval=interval,
            )
            analysis = handler.get_analysis()
            ind = analysis.indicators
            summary = analysis.summary

            price = ind.get('close', 0)
            sar = ind.get('P.SAR', 0)
            rsi = ind.get('RSI', 50)
            macd = ind.get('MACD.macd', 0)
            macd_signal = ind.get('MACD.signal', 0)
            adx = ind.get('ADX', 0)
            adx_plus = ind.get('ADX+DI', 0)
            adx_minus = ind.get('ADX-DI', 0)
            bb_upper = ind.get('BB.upper', 0)
            bb_lower = ind.get('BB.lower', 0)

            bb_range = bb_upper - bb_lower if bb_upper != bb_lower else 1
            bb_pos = (price - bb_lower) / bb_range if bb_range > 0 else 0.5

            supertrend = 1 if price > sar else -1
            macd_trend = 1 if macd > macd_signal else -1
            adx_trend = 0
            if adx > 25:
                adx_trend = 1 if adx_plus > adx_minus else -1

            multi_data[name] = {
                'rsi': rsi,
                'supertrend': supertrend,
                'macd_trend': macd_trend,
                'adx_trend': adx_trend,
                'adx': adx,
                'bb_position': round(bb_pos, 3),
                'recommendation': summary.get('RECOMMENDATION', 'NEUTRAL'),
            }
    except Exception as e:
        result.error = str(e)
        logger.warning(f"TradingView 分析失败: {e}")
        return result

    if not multi_data or '1h' not in multi_data:
        result.error = "incomplete data"
        return result

    # 填充结果
    d1h = multi_data['1h']
    d4h = multi_data.get('4h', d1h)
    d1d = multi_data.get('1d', d1h)

    result.rsi_1h = round(d1h['rsi'], 1)
    result.rsi_4h = round(d4h['rsi'], 1)
    result.rsi_1d = round(d1d['rsi'], 1)
    result.supertrend_1h = d1h['supertrend']
    result.supertrend_4h = d4h['supertrend']
    result.supertrend_1d = d1d['supertrend']
    result.macd_1h = d1h['macd_trend']
    result.bb_position = d1h['bb_position']
    result.recommendation_1h = d1h['recommendation']

    if d1h['bb_position'] < 0.2:
        result.bb_zone = 'oversold'
    elif d1h['bb_position'] > 0.8:
        result.bb_zone = 'overbought'
    else:
        result.bb_zone = 'neutral'

    # 多周期对齐
    st_list = [d1h['supertrend'], d4h['supertrend'], d1d['supertrend']]
    macd_list = [d1h['macd_trend'], d4h['macd_trend'], d1d['macd_trend']]
    adx_list = [d['adx_trend'] for d in [d1h, d4h, d1d] if d['adx_trend'] != 0]

    st_align = sum(st_list) / len(st_list)
    macd_align = sum(macd_list) / len(macd_list)
    adx_align = sum(adx_list) / len(adx_list) if adx_list else 0

    overall = st_align * 0.4 + macd_align * 0.3 + adx_align * 0.3
    result.alignment = max(-2, min(2, round(overall * 2)))

    # 综合 TA 评分
    ta_score = 0.0
    ta_score += d1h['supertrend'] * 0.5
    rsi_zone = 1 if d1h['rsi'] > 70 else -1 if d1h['rsi'] < 30 else 0
    if rsi_zone == -1:
        ta_score += 0.5
    elif rsi_zone == 1:
        ta_score -= 0.5
    ta_score += d1h['macd_trend'] * 0.3
    if d1h['adx'] > 25:
        ta_score += d1h['adx_trend'] * 0.3
    if d1h['bb_position'] < 0.2:
        ta_score += 0.5
    elif d1h['bb_position'] > 0.8:
        ta_score -= 0.5
    ta_score += result.alignment * 0.4

    result.score = max(-3, min(3, round(ta_score)))
    result.available = True

    logger.info(
        f"[TV] score={result.score} align={result.alignment} "
        f"RSI={result.rsi_1h} ST={result.supertrend_1h}/{result.supertrend_4h}/{result.supertrend_1d}"
    )
    return result
