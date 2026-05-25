#!/usr/bin/env python3
"""
市场 Regime Detection（市场状态分类）v1.0

将市场环境分为几种状态，策略根据不同状态调整行为：
  - TRENDING_UP:   单边上涨（做空策略减仓 / 做多策略加仓）
  - TRENDING_DOWN: 单边下跌（做空策略加仓 / 做多策略减仓）
  - RANGING:       震荡盘整（双向策略正常运行）
  - HIGH_VOL:      极高波动（缩减仓位 / 加宽止损）
  - CRASH:         崩盘模式（暂停所有开仓）

检测方法（多指标融合）：
  1. BTC 趋势方向（EMA20 vs EMA50 交叉）
  2. 波动率水平（ATR / 历史波动率分位数）
  3. 市场宽度（涨跌比、新高新低比）
  4. VIX-like 指标（隐含波动率代理：funding rate 离散度）

集成方式：
  - 被 event_integration.py 定期调用（每 30 分钟）
  - 通过 event_bus 发布 'market.regime_changed' 事件
  - 策略通过 get_current_regime() 查询当前状态
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple

import numpy as np

logger = logging.getLogger("signals.regime")


# ══════════════════════════════════════════════════════════════════
#  市场状态枚举
# ══════════════════════════════════════════════════════════════════

class MarketRegime(str, Enum):
    TRENDING_UP = 'trending_up'        # 牛市趋势
    TRENDING_DOWN = 'trending_down'    # 熊市趋势
    RANGING = 'ranging'                # 震荡
    HIGH_VOL = 'high_volatility'       # 高波动
    CRASH = 'crash'                    # 崩盘

    @property
    def short_bias(self) -> float:
        """做空策略的仓位乘数"""
        return {
            self.TRENDING_UP: 0.3,     # 牛市大幅减仓
            self.TRENDING_DOWN: 1.5,   # 熊市加仓
            self.RANGING: 1.0,         # 震荡正常
            self.HIGH_VOL: 0.5,        # 高波动减仓
            self.CRASH: 0.0,           # 崩盘暂停
        }[self]

    @property
    def long_bias(self) -> float:
        """做多策略的仓位乘数"""
        return {
            self.TRENDING_UP: 1.3,     # 牛市加仓
            self.TRENDING_DOWN: 0.3,   # 熊市大幅减仓
            self.RANGING: 1.0,
            self.HIGH_VOL: 0.5,
            self.CRASH: 0.0,
        }[self]

    @property
    def description(self) -> str:
        return {
            self.TRENDING_UP: '🟢 牛市趋势',
            self.TRENDING_DOWN: '🔴 熊市趋势',
            self.RANGING: '🟡 震荡盘整',
            self.HIGH_VOL: '🟠 高波动',
            self.CRASH: '💥 崩盘模式',
        }[self]


# ══════════════════════════════════════════════════════════════════
#  检测结果
# ══════════════════════════════════════════════════════════════════

@dataclass
class RegimeState:
    """当前市场状态"""
    regime: MarketRegime = MarketRegime.RANGING
    confidence: float = 0.5            # 置信度 (0~1)
    updated_at: float = 0.0            # timestamp
    indicators: Dict[str, float] = field(default_factory=dict)
    reason: str = ''

    @property
    def is_stale(self) -> bool:
        """数据是否过期（>30 分钟）"""
        return time.time() - self.updated_at > 1800


@dataclass
class RegimeConfig:
    """Regime Detection 配置"""
    enabled: bool = True

    # EMA 参数
    ema_fast: int = 20
    ema_slow: int = 50

    # 波动率分位数阈值
    vol_high_percentile: float = 80     # ATR > 80 分位数 = 高波动
    vol_crash_percentile: float = 95    # ATR > 95 分位数 = 崩盘级

    # 趋势强度阈值
    trend_threshold_pct: float = 2.0    # EMA fast/slow 偏离 > 2% = 趋势
    strong_trend_pct: float = 5.0       # > 5% = 强趋势

    # BTC 特殊规则
    btc_crash_24h_pct: float = -8.0     # BTC 24h < -8% = 触发 CRASH
    btc_pump_24h_pct: float = 10.0      # BTC 24h > 10% = 强牛

    # 市场宽度
    breadth_threshold: float = 0.7      # 70%+ 涨/跌 = 趋势确认

    # 更新频率
    update_interval_sec: float = 1800   # 30 分钟更新一次


# ══════════════════════════════════════════════════════════════════
#  Regime Detection 引擎
# ══════════════════════════════════════════════════════════════════

class RegimeDetector:
    """
    市场 Regime 检测器。

    用法:
      detector = RegimeDetector()
      detector.start()

      # 获取当前状态
      state = detector.get_state()
      print(f"当前: {state.regime.description}, 置信度={state.confidence:.0%}")

      # 策略用
      multiplier = state.regime.short_bias  # 做空仓位乘数
    """

    def __init__(self, config: Optional[RegimeConfig] = None):
        self.config = config or RegimeConfig()
        self._state = RegimeState()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._btc_history: List[float] = []     # BTC 1h closes
        self._market_data_cache: Dict[str, Any] = {}

    def start(self) -> None:
        """启动后台检测线程"""
        if not self.config.enabled:
            logger.info("📊 Regime Detection 已禁用")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._detection_loop,
            name='regime_detector',
            daemon=True,
        )
        self._thread.start()
        logger.info("📊 Regime Detection 已启动")

    def stop(self) -> None:
        self._running = False

    def get_state(self) -> RegimeState:
        """获取当前市场状态"""
        with self._lock:
            return self._state

    def get_regime(self) -> MarketRegime:
        """快捷获取当前 regime"""
        return self._state.regime

    def get_position_multiplier(self, direction: str = 'SHORT') -> float:
        """获取当前 regime 下的仓位乘数"""
        regime = self._state.regime
        if direction.upper() == 'SHORT':
            return regime.short_bias
        return regime.long_bias

    def force_update(self) -> RegimeState:
        """强制立即更新（同步）"""
        self._detect()
        return self._state

    # ── 检测逻辑 ─────────────────────────────────────────────────

    def _detection_loop(self):
        """后台检测循环"""
        while self._running:
            try:
                self._detect()
            except Exception as e:
                logger.warning(f"Regime 检测异常: {e}")
            time.sleep(self.config.update_interval_sec)

    def _detect(self):
        """执行一次 regime 检测"""
        cfg = self.config
        indicators = {}

        # 1. 获取 BTC 价格数据
        btc_closes = self._fetch_btc_closes()
        if not btc_closes or len(btc_closes) < cfg.ema_slow + 10:
            return

        self._btc_history = btc_closes

        # 2. 计算 EMA
        ema_fast = self._ema(btc_closes, cfg.ema_fast)
        ema_slow = self._ema(btc_closes, cfg.ema_slow)
        ema_divergence = (ema_fast - ema_slow) / ema_slow * 100
        indicators['ema_divergence_pct'] = round(ema_divergence, 2)

        # 3. 计算 ATR / 波动率
        atr = self._calculate_atr(btc_closes, period=14)
        atr_pct = atr / btc_closes[-1] * 100 if btc_closes[-1] > 0 else 0
        indicators['atr_pct'] = round(atr_pct, 3)

        # 历史 ATR 分位数
        historical_atrs = self._calculate_historical_atrs(btc_closes, period=14)
        if historical_atrs:
            vol_percentile = np.percentile(
                historical_atrs, [cfg.vol_high_percentile, cfg.vol_crash_percentile]
            )
            indicators['vol_high_threshold'] = round(float(vol_percentile[0]), 4)
            indicators['vol_crash_threshold'] = round(float(vol_percentile[1]), 4)
        else:
            vol_percentile = [atr_pct * 1.5, atr_pct * 2.5]

        # 4. BTC 24h 变化
        btc_24h_pct = (btc_closes[-1] - btc_closes[-24]) / btc_closes[-24] * 100 if len(btc_closes) >= 24 else 0
        indicators['btc_24h_pct'] = round(btc_24h_pct, 2)

        # 5. 市场宽度（简化：用 BTC 趋势一致性代理）
        recent_trend = (btc_closes[-1] - btc_closes[-6]) / btc_closes[-6] * 100 if len(btc_closes) >= 6 else 0
        indicators['btc_6h_pct'] = round(recent_trend, 2)

        # ── 状态判定 ─────────────────────────────────────────────

        regime = MarketRegime.RANGING
        confidence = 0.5
        reason_parts = []

        # CRASH 检测（最高优先级）
        if btc_24h_pct <= cfg.btc_crash_24h_pct:
            regime = MarketRegime.CRASH
            confidence = 0.95
            reason_parts.append(f"BTC 24h {btc_24h_pct:.1f}% (崩盘)")
        elif atr_pct > float(vol_percentile[1] if len(vol_percentile) > 1 else atr_pct * 2):
            regime = MarketRegime.CRASH
            confidence = 0.85
            reason_parts.append(f"ATR={atr_pct:.2f}% 超过 95 分位数")

        # HIGH_VOL 检测
        elif atr_pct > float(vol_percentile[0] if vol_percentile else atr_pct * 1.5):
            regime = MarketRegime.HIGH_VOL
            confidence = 0.7
            reason_parts.append(f"ATR={atr_pct:.2f}% 超过 80 分位数")

        # 趋势检测
        elif abs(ema_divergence) >= cfg.strong_trend_pct:
            if ema_divergence > 0:
                regime = MarketRegime.TRENDING_UP
                confidence = 0.85
                reason_parts.append(f"EMA20/50 偏离 +{ema_divergence:.1f}% (强牛)")
            else:
                regime = MarketRegime.TRENDING_DOWN
                confidence = 0.85
                reason_parts.append(f"EMA20/50 偏离 {ema_divergence:.1f}% (强熊)")

        elif abs(ema_divergence) >= cfg.trend_threshold_pct:
            if ema_divergence > 0:
                regime = MarketRegime.TRENDING_UP
                confidence = 0.6
                reason_parts.append(f"EMA20/50 偏离 +{ema_divergence:.1f}%")
            else:
                regime = MarketRegime.TRENDING_DOWN
                confidence = 0.6
                reason_parts.append(f"EMA20/50 偏离 {ema_divergence:.1f}%")

        # BTC 强涨加分
        if btc_24h_pct >= cfg.btc_pump_24h_pct and regime != MarketRegime.CRASH:
            regime = MarketRegime.TRENDING_UP
            confidence = max(confidence, 0.8)
            reason_parts.append(f"BTC 24h +{btc_24h_pct:.1f}%")

        # 默认 RANGING
        if not reason_parts:
            reason_parts.append(f"EMA 偏离 {ema_divergence:.1f}% 在阈值内")

        # 更新状态
        new_state = RegimeState(
            regime=regime,
            confidence=confidence,
            updated_at=time.time(),
            indicators=indicators,
            reason=' | '.join(reason_parts),
        )

        old_regime = self._state.regime
        with self._lock:
            self._state = new_state

        # 如果 regime 变化了，发布事件
        if regime != old_regime:
            logger.info(
                f"📊 Regime 变更: {old_regime.description} → {regime.description} "
                f"(confidence={confidence:.0%})"
            )
            try:
                from event_bus import get_event_bus
                get_event_bus().publish('market.regime_changed', {
                    'old_regime': old_regime.value,
                    'new_regime': regime.value,
                    'confidence': confidence,
                    'reason': new_state.reason,
                    'indicators': indicators,
                })
            except Exception:
                pass

    # ── 数据获取 ─────────────────────────────────────────────────

    def _fetch_btc_closes(self) -> List[float]:
        """获取 BTC 1h 收盘价序列"""
        try:
            from exchange_manager import get_binance
            exchange = get_binance()
            klines = exchange.fetch_ohlcv('BTC/USDT', '1h', limit=100)
            if klines:
                return [k[4] for k in klines]
        except Exception as e:
            logger.debug(f"BTC 数据获取失败: {e}")

        # Fallback: 用缓存
        return self._btc_history

    # ── 技术指标 ─────────────────────────────────────────────────

    @staticmethod
    def _ema(data: List[float], period: int) -> float:
        """计算 EMA 当前值"""
        if len(data) < period:
            return data[-1] if data else 0
        multiplier = 2 / (period + 1)
        ema = sum(data[:period]) / period  # SMA 初始化
        for price in data[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    @staticmethod
    def _calculate_atr(closes: List[float], period: int = 14) -> float:
        """计算 ATR（简化：用收盘价差代替真实 TR）"""
        if len(closes) < period + 1:
            return 0
        trs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
        return sum(trs[-period:]) / period

    @staticmethod
    def _calculate_historical_atrs(closes: List[float], period: int = 14) -> List[float]:
        """计算历史 ATR 序列（用于分位数）"""
        if len(closes) < period + 10:
            return []
        trs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
        atrs = []
        for i in range(period, len(trs)):
            atr = sum(trs[i - period:i]) / period
            atrs.append(atr / closes[i] * 100 if closes[i] > 0 else 0)
        return atrs


# ══════════════════════════════════════════════════════════════════
#  全局单例
# ══════════════════════════════════════════════════════════════════

_detector: Optional[RegimeDetector] = None


def get_regime_detector(config: Optional[RegimeConfig] = None) -> RegimeDetector:
    """获取 Regime Detector 单例"""
    global _detector
    if _detector is None:
        _detector = RegimeDetector(config)
    return _detector


def get_current_regime() -> MarketRegime:
    """快捷函数：获取当前市场 regime"""
    return get_regime_detector().get_regime()


def get_position_multiplier(direction: str = 'SHORT') -> float:
    """快捷函数：获取当前 regime 的仓位乘数"""
    return get_regime_detector().get_position_multiplier(direction)
