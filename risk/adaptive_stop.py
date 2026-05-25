#!/usr/bin/env python3
"""
ATR 自适应止损 v1.0

解决问题：
  固定 5% 硬止损在高波动期被假突破洗出（日内震荡 6%+ 常见），
  在低波动期又太宽导致亏损过大。

方案：
  hard_stop = max(MIN_STOP_PCT, ATR_MULTIPLIER * ATR_14 / entry_price * 100)

逻辑：
  - 高波动（ATR 大）→ 止损自动加宽 → 避免被日内震荡洗出
  - 低波动（ATR 小）→ 止损收紧 → 保护浮盈
  - 始终不低于 MIN_STOP_PCT（2%）→ 兜底安全网
  - 始终不高于 MAX_STOP_PCT（10%）→ 防止无限放大亏损

ATR 计算：
  使用 14 根 1H K 线的 True Range 均值（Wilder 平滑）。
  1H ATR 比日线 ATR 更敏感，适合 24h 持仓周期。

集成方式：
  在 altcoin_tracker.evaluate_trade() 或策略的 evaluate_exit() 中：
    from risk.adaptive_stop import calculate_adaptive_stop
    stop_pct = calculate_adaptive_stop(symbol, entry_price)
    if pnl_pct <= -stop_pct:
        trigger_stop_loss()

用法：
  from risk.adaptive_stop import AdaptiveStopEngine, calculate_adaptive_stop

  # 简单用法
  stop_pct = calculate_adaptive_stop('PEPE/USDT', entry_price=0.00001)

  # 完整用法
  engine = AdaptiveStopEngine()
  result = engine.compute(symbol='PEPE/USDT', entry_price=0.00001)
  print(f"止损: {result.stop_pct:.1f}% (ATR={result.atr_value:.6f})")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("risk.adaptive_stop")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class AdaptiveStopConfig:
    """自适应止损配置"""
    enabled: bool = True

    # ATR 参数
    atr_period: int = 14               # ATR 计算周期（1H K 线数）
    atr_timeframe: str = '1h'          # ATR 计算时间框架
    atr_multiplier: float = 2.0        # ATR 乘数（止损 = N * ATR）

    # 止损范围限制
    min_stop_pct: float = 2.0          # 最小止损 %（兜底）
    max_stop_pct: float = 10.0         # 最大止损 %（封顶）
    default_stop_pct: float = 5.0      # ATR 不可用时的默认值

    # 方向调整
    short_multiplier: float = 1.0      # 做空时的乘数（做空对上涨更敏感）
    long_multiplier: float = 1.1       # 做多时的乘数（做多允许稍宽）

    # 缓存
    cache_ttl_sec: int = 300           # ATR 缓存 5 分钟

    # Re-entry 逻辑（止损后快速反转 = 假突破）
    reentry_enabled: bool = True
    reentry_window_bars: int = 3       # 止损后 N 根 bar 内价格回到入场价 = 假突破
    reentry_cooldown_override_min: int = 30  # 假突破时缩短冷却期到 30 分钟


# ══════════════════════════════════════════════════════════════════
#  结果
# ══════════════════════════════════════════════════════════════════

@dataclass
class AdaptiveStopResult:
    """自适应止损计算结果"""
    stop_pct: float = 5.0              # 最终止损百分比
    stop_price: float = 0.0            # 止损价格
    atr_value: float = 0.0             # ATR 绝对值
    atr_pct: float = 0.0              # ATR 占价格的百分比
    volatility_regime: str = 'normal'  # 'low' / 'normal' / 'high' / 'extreme'
    source: str = 'adaptive'           # 'adaptive' / 'default' / 'min_clamp' / 'max_clamp'

    @property
    def is_widened(self) -> bool:
        """止损是否被加宽（相对默认 5%）"""
        return self.stop_pct > 5.0

    @property
    def is_tightened(self) -> bool:
        """止损是否被收紧"""
        return self.stop_pct < 5.0


# ══════════════════════════════════════════════════════════════════
#  自适应止损引擎
# ══════════════════════════════════════════════════════════════════

class AdaptiveStopEngine:
    """
    ATR 自适应止损引擎。

    用法:
      engine = AdaptiveStopEngine()
      result = engine.compute('PEPE/USDT', entry_price=0.00001, direction='SHORT')
      print(f"止损: {result.stop_pct:.1f}%")
    """

    def __init__(self, config: Optional[AdaptiveStopConfig] = None):
        self.config = config or AdaptiveStopConfig()
        self._atr_cache: Dict[str, Tuple[float, float]] = {}  # symbol → (atr, timestamp)

    def compute(
        self,
        symbol: str,
        entry_price: float,
        direction: str = 'SHORT',
        klines: Optional[List[List[float]]] = None,
    ) -> AdaptiveStopResult:
        """
        计算自适应止损。

        参数:
          symbol: 交易对
          entry_price: 入场价
          direction: 'SHORT' / 'LONG'
          klines: 可选的 K 线数据 [[ts, o, h, l, c, v], ...]
                  如果不传，会尝试从 exchange 获取

        返回:
          AdaptiveStopResult
        """
        cfg = self.config
        result = AdaptiveStopResult()

        if not cfg.enabled:
            result.stop_pct = cfg.default_stop_pct
            result.source = 'default'
            if entry_price > 0:
                if direction == 'SHORT':
                    result.stop_price = entry_price * (1 + result.stop_pct / 100)
                else:
                    result.stop_price = entry_price * (1 - result.stop_pct / 100)
            return result

        # 获取 ATR
        atr = self._get_atr(symbol, klines)

        if atr is None or atr <= 0 or entry_price <= 0:
            result.stop_pct = cfg.default_stop_pct
            result.source = 'default'
        else:
            result.atr_value = atr
            result.atr_pct = atr / entry_price * 100

            # 计算自适应止损
            raw_stop_pct = cfg.atr_multiplier * result.atr_pct

            # 方向调整
            if direction == 'SHORT':
                raw_stop_pct *= cfg.short_multiplier
            else:
                raw_stop_pct *= cfg.long_multiplier

            # Clamp 到合理范围
            if raw_stop_pct < cfg.min_stop_pct:
                result.stop_pct = cfg.min_stop_pct
                result.source = 'min_clamp'
            elif raw_stop_pct > cfg.max_stop_pct:
                result.stop_pct = cfg.max_stop_pct
                result.source = 'max_clamp'
            else:
                result.stop_pct = round(raw_stop_pct, 2)
                result.source = 'adaptive'

            # 波动率 regime 分类
            if result.atr_pct < 1.5:
                result.volatility_regime = 'low'
            elif result.atr_pct < 3.0:
                result.volatility_regime = 'normal'
            elif result.atr_pct < 6.0:
                result.volatility_regime = 'high'
            else:
                result.volatility_regime = 'extreme'

        # 计算止损价
        if entry_price > 0:
            if direction == 'SHORT':
                result.stop_price = round(entry_price * (1 + result.stop_pct / 100), 8)
            else:
                result.stop_price = round(entry_price * (1 - result.stop_pct / 100), 8)

        logger.debug(
            f"[AdaptiveStop] {symbol} {direction}: "
            f"stop={result.stop_pct:.1f}% (ATR={result.atr_pct:.2f}%, "
            f"regime={result.volatility_regime}, source={result.source})"
        )
        return result

    def _get_atr(self, symbol: str, klines: Optional[List] = None) -> Optional[float]:
        """获取 ATR（带缓存）"""
        cfg = self.config

        # 检查缓存
        if symbol in self._atr_cache:
            cached_atr, cached_ts = self._atr_cache[symbol]
            if time.time() - cached_ts < cfg.cache_ttl_sec:
                return cached_atr

        # 从 klines 计算
        if klines and len(klines) >= cfg.atr_period + 1:
            atr = self._calculate_atr(klines, cfg.atr_period)
            self._atr_cache[symbol] = (atr, time.time())
            return atr

        # 尝试从交易所获取
        try:
            from exchange_manager import get_binance
            exchange = get_binance()
            fetched = exchange.fetch_ohlcv(symbol, cfg.atr_timeframe, limit=cfg.atr_period + 5)
            if fetched and len(fetched) >= cfg.atr_period + 1:
                atr = self._calculate_atr(fetched, cfg.atr_period)
                self._atr_cache[symbol] = (atr, time.time())
                return atr
        except Exception as e:
            logger.debug(f"ATR 获取失败 ({symbol}): {e}")

        return None

    @staticmethod
    def _calculate_atr(klines: List[List[float]], period: int = 14) -> float:
        """
        计算 ATR (Average True Range)。

        True Range = max(
          high - low,
          abs(high - prev_close),
          abs(low - prev_close)
        )

        ATR = Wilder 平滑均值
        """
        if len(klines) < period + 1:
            return 0.0

        true_ranges = []
        for i in range(1, len(klines)):
            high = float(klines[i][2])
            low = float(klines[i][3])
            prev_close = float(klines[i - 1][4])

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            true_ranges.append(tr)

        if len(true_ranges) < period:
            return float(np.mean(true_ranges)) if true_ranges else 0.0

        # Wilder 平滑（与 RSI 同样的平滑方法）
        atr = sum(true_ranges[:period]) / period
        for tr in true_ranges[period:]:
            atr = (atr * (period - 1) + tr) / period

        return atr

    def clear_cache(self):
        """清除 ATR 缓存"""
        self._atr_cache.clear()


# ══════════════════════════════════════════════════════════════════
#  便捷函数
# ══════════════════════════════════════════════════════════════════

_engine: Optional[AdaptiveStopEngine] = None


def get_adaptive_stop_engine() -> AdaptiveStopEngine:
    """获取全局单例"""
    global _engine
    if _engine is None:
        _engine = AdaptiveStopEngine()
    return _engine


def calculate_adaptive_stop(
    symbol: str,
    entry_price: float,
    direction: str = 'SHORT',
    klines: Optional[List] = None,
) -> float:
    """
    便捷函数：计算自适应止损百分比。

    返回: 止损百分比 (如 5.0 表示 5%)
    """
    engine = get_adaptive_stop_engine()
    result = engine.compute(symbol, entry_price, direction, klines)
    return result.stop_pct


def calculate_adaptive_stop_price(
    symbol: str,
    entry_price: float,
    direction: str = 'SHORT',
    klines: Optional[List] = None,
) -> float:
    """
    便捷函数：直接返回止损价格。
    """
    engine = get_adaptive_stop_engine()
    result = engine.compute(symbol, entry_price, direction, klines)
    return result.stop_price
