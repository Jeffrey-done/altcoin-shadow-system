#!/usr/bin/env python3
"""
动态 ATR 止损计算器
====================

用各币种的 ATR(14) 自适应计算硬止损距离，替代全局固定 5% 硬止损。

原理：
  - 波动大的币（ATR 高）→ 止损放宽，避免正常波动被打止损
  - 波动小的币（ATR 低）→ 止损收紧，快速止血

公式：
  hard_stop_price = entry_price × (1 + atr_multiplier × ATR% / 100)
  （做空：价格上涨 = 亏损，所以止损在上方）

参数：
  - atr_period: ATR 计算周期（默认 14）
  - atr_multiplier: ATR 倍数（默认 2.0，即 2 倍 ATR 作为止损距离）
  - min_stop_pct: 最小止损百分比（默认 2%，防止极低波动币止损太紧）
  - max_stop_pct: 最大止损百分比（默认 10%，防止极高波动币止损太宽）

用法：
  from risk.atr_stop import compute_atr_stop, get_dynamic_stop_pct

  # 方式1：直接算止损价
  stop_price = compute_atr_stop(ohlcv, entry_price, direction='SHORT')

  # 方式2：只算止损百分比（给 Trade.create_short 用）
  stop_pct = get_dynamic_stop_pct(ohlcv)
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger("risk.atr_stop")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class ATRStopConfig:
    """ATR 止损配置"""
    atr_period: int = 14           # ATR 计算周期
    atr_multiplier: float = 2.0    # ATR 倍数（止损距离 = ATR × 此值）
    min_stop_pct: float = 2.0      # 最小止损 %（防止止损太紧）
    max_stop_pct: float = 10.0     # 最大止损 %（防止止损太宽）
    timeframe: str = '1h'          # ATR 计算的时间框架


# 全局默认配置
_DEFAULT_CONFIG = ATRStopConfig()


# ══════════════════════════════════════════════════════════════════
#  ATR 计算
# ══════════════════════════════════════════════════════════════════

def compute_atr(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> float:
    """
    计算 ATR (Average True Range)。

    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR = Wilder 平滑的 TR 均值

    Args:
        highs: 最高价序列
        lows: 最低价序列
        closes: 收盘价序列
        period: ATR 周期

    Returns:
        当前 ATR 值（绝对值，非百分比）
    """
    n = len(closes)
    if n < period + 1:
        # 数据不足，用简单方法估算
        if n >= 2:
            return float(np.mean([h - l for h, l in zip(highs[-5:], lows[-5:])]))
        return 0.0

    # True Range 序列
    tr_list = []
    for i in range(1, n):
        high_low = highs[i] - lows[i]
        high_prev_close = abs(highs[i] - closes[i - 1])
        low_prev_close = abs(lows[i] - closes[i - 1])
        tr = max(high_low, high_prev_close, low_prev_close)
        tr_list.append(tr)

    if len(tr_list) < period:
        return float(np.mean(tr_list))

    # Wilder 平滑（与 RSI 同款）
    atr = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period

    return float(atr)


def compute_atr_pct(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> float:
    """
    计算 ATR 占当前价格的百分比。

    Returns:
        ATR% = ATR / last_close × 100
    """
    if not closes or closes[-1] <= 0:
        return 5.0  # fallback

    atr = compute_atr(highs, lows, closes, period)
    return (atr / closes[-1]) * 100


# ══════════════════════════════════════════════════════════════════
#  动态止损计算
# ══════════════════════════════════════════════════════════════════

def get_dynamic_stop_pct(
    ohlcv: list,
    config: Optional[ATRStopConfig] = None,
) -> float:
    """
    根据 OHLCV 数据计算动态止损百分比。

    Args:
        ohlcv: ccxt 格式 [[ts, open, high, low, close, volume], ...]
        config: ATR 止损配置（None 用默认）

    Returns:
        止损百分比（如 4.5 表示 4.5%）
        已经 clamp 在 [min_stop_pct, max_stop_pct] 范围内
    """
    cfg = config or _DEFAULT_CONFIG

    if not ohlcv or len(ohlcv) < cfg.atr_period + 1:
        logger.debug("OHLCV 数据不足，使用默认 5% 止损")
        return 5.0

    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]
    closes = [c[4] for c in ohlcv]

    atr_pct = compute_atr_pct(highs, lows, closes, cfg.atr_period)

    # 止损距离 = ATR% × multiplier
    stop_pct = atr_pct * cfg.atr_multiplier

    # Clamp 到合理范围
    stop_pct = max(cfg.min_stop_pct, min(cfg.max_stop_pct, stop_pct))

    logger.debug(
        f"ATR止损: ATR%={atr_pct:.2f}% × {cfg.atr_multiplier} = {atr_pct * cfg.atr_multiplier:.2f}% "
        f"→ clamp [{cfg.min_stop_pct}, {cfg.max_stop_pct}] = {stop_pct:.2f}%"
    )

    return round(stop_pct, 2)


def compute_atr_stop(
    ohlcv: list,
    entry_price: float,
    direction: str = 'SHORT',
    config: Optional[ATRStopConfig] = None,
) -> float:
    """
    计算动态止损价格。

    Args:
        ohlcv: ccxt 格式 OHLCV 数据
        entry_price: 入场价格
        direction: 'SHORT' 或 'LONG'
        config: 配置

    Returns:
        止损触发价格
    """
    stop_pct = get_dynamic_stop_pct(ohlcv, config)

    if direction.upper() == 'SHORT':
        # 做空止损在上方：价格涨 stop_pct% 就止损
        return round(entry_price * (1 + stop_pct / 100), 8)
    else:
        # 做多止损在下方：价格跌 stop_pct% 就止损
        return round(entry_price * (1 - stop_pct / 100), 8)


def compute_atr_stop_from_exchange(
    exchange,
    symbol: str,
    entry_price: float,
    direction: str = 'SHORT',
    timeframe: str = '1h',
    limit: int = 50,
    config: Optional[ATRStopConfig] = None,
) -> Tuple[float, float]:
    """
    便捷函数：直接从交易所拉数据计算 ATR 止损。

    Args:
        exchange: ccxt exchange 实例
        symbol: 交易对（如 'PEPE/USDT'）
        entry_price: 入场价
        direction: 'SHORT' 或 'LONG'
        timeframe: K线时间框架
        limit: 拉取 K 线数量
        config: 配置

    Returns:
        (stop_price, stop_pct) 元组
    """
    cfg = config or _DEFAULT_CONFIG

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        # 丢弃最后一根未收盘 K 线
        if len(ohlcv) >= 2:
            ohlcv = ohlcv[:-1]

        stop_pct = get_dynamic_stop_pct(ohlcv, cfg)
        stop_price = compute_atr_stop(ohlcv, entry_price, direction, cfg)

        logger.info(
            f"  📐 ATR动态止损 {symbol}: ATR止损={stop_pct:.1f}% "
            f"止损价={stop_price:.6f} (入场={entry_price:.6f})"
        )
        return stop_price, stop_pct

    except Exception as e:
        logger.warning(f"ATR止损计算失败 ({symbol}): {e}，fallback 固定5%")
        fallback_pct = 5.0
        if direction.upper() == 'SHORT':
            fallback_price = entry_price * (1 + fallback_pct / 100)
        else:
            fallback_price = entry_price * (1 - fallback_pct / 100)
        return fallback_price, fallback_pct
