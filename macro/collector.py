#!/usr/bin/env python3
"""
宏观信号采集器 v2 — 内嵌采集，无外部依赖
直接调用 macro/sources/ 下的 5 个 Python 模块获取数据，
不再依赖 multi-signal 或 CMM 的外部仓库。

数据源（全部内嵌）：
  1. Smart Money — Binance Web3 大户买卖
  2. OKX Market — K线/订单簿/Funding
  3. Fear & Greed — alternative.me 情绪指数
  4. TradingView — 多周期技术分析（可选依赖 tradingview-ta）
  5. Multi Exchange — 多交易所价差

输出：统一的 MacroSignal 对象，供 macro/filter.py 消费。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("macro.collector")


# ══════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class MSSignal:
    """多维度综合信号（替代原 multi-signal 的 JSON 输出）"""
    score: int = 0                      # -100 ~ +100
    signal: str = '观望'                 # '做多' | '做空' | '观望'
    timestamp: int = 0
    age_minutes: float = 0.0
    is_stale: bool = False

    # 6维度分数
    smart_money_score: int = 0
    momentum_score: int = 0
    trend_score: int = 0
    sentiment_score: int = 0
    structure_score: int = 0
    technical_score: int = 0

    # 关键指标
    fear_greed: int = 50
    funding_rate: float = 0.0
    btc_24h_change: float = 0.0
    order_book_ratio: float = 1.0
    tv_alignment: int = 0
    tv_rsi_1h: float = 50.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class CMMDecision:
    """CMM 风格决策（基于内嵌数据直接计算）"""
    action: str = 'HOLD'
    confidence: float = 0.0
    risk_level: int = 3
    position_pct: int = 30
    phase: str = 'NEUTRAL'
    timestamp: int = 0
    age_minutes: float = 0.0
    is_stale: bool = False
    macro_signal: str = ''
    trend_signal: str = ''
    extreme_signal: str = ''

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class MacroSignal:
    """整合后的宏观信号（最终输出）"""
    ms: MSSignal = field(default_factory=MSSignal)
    cmm: CMMDecision = field(default_factory=CMMDecision)

    environment: str = 'neutral'
    short_friendly: bool = True
    stake_multiplier: float = 1.0
    score_bonus: int = 0
    reason: str = ''
    collected_at: str = ''
    data_quality: str = 'good'

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d['ms'] = self.ms.to_dict()
        d['cmm'] = self.cmm.to_dict()
        return d


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class MacroCollectorConfig:
    """采集器配置"""
    # 权重（与 multi-signal 保持一致）
    w_smart_money: float = 0.30
    w_momentum: float = 0.20
    w_technical: float = 0.15
    w_trend: float = 0.15
    w_sentiment: float = 0.10
    w_structure: float = 0.10

    # 阈值
    long_threshold: int = 30
    short_threshold: int = -30

    # 超时
    source_timeout: int = 15

    # 缓存
    cache_ttl_sec: int = 60

    # 各源开关
    smart_money_enabled: bool = True
    tradingview_enabled: bool = True
    okx_enabled: bool = True
    fear_greed_enabled: bool = True
    multi_exchange_enabled: bool = True

    # 保留旧字段兼容（不再使用）
    ms_signal_path: str = ''
    ms_search_paths: tuple = ()
    ms_stale_minutes: float = 120.0
    cmm_output_path: str = ''
    cmm_enabled: bool = True
    cmm_stale_minutes: float = 30.0


# ══════════════════════════════════════════════════════════════════
#  采集器
# ══════════════════════════════════════════════════════════════════

class MacroCollector:
    """
    宏观信号采集器 v2 — 内嵌所有数据源。

    用法:
      collector = MacroCollector()
      macro = collector.collect()
      if not macro.short_friendly:
          logger.warning("宏观环境不利做空")
    """

    def __init__(self, config: Optional[MacroCollectorConfig] = None):
        self.config = config or MacroCollectorConfig()
        self._cache: Optional[MacroSignal] = None
        self._cache_ts: float = 0.0

    def collect(self) -> MacroSignal:
        """采集并整合宏观信号。带缓存。"""
        now = time.time()
        if self._cache and (now - self._cache_ts) < self.config.cache_ttl_sec:
            return self._cache

        ms = self._collect_all_sources()
        macro = self._synthesize(ms)

        self._cache = macro
        self._cache_ts = now
        return macro

    def invalidate_cache(self):
        """强制下次重新采集"""
        self._cache = None

    # ── 采集所有数据源 ────────────────────────────────────────────

    def _collect_all_sources(self) -> MSSignal:
        """并行采集所有内嵌数据源，计算6维评分。"""
        cfg = self.config
        signal = MSSignal(timestamp=int(time.time()))

        # 1. Smart Money
        if cfg.smart_money_enabled:
            try:
                from macro.sources.smart_money import fetch_smart_money
                sm = fetch_smart_money(timeout=cfg.source_timeout)
                signal.smart_money_score = sm.signal_score
            except Exception as e:
                logger.warning(f"Smart Money 采集失败: {e}")

        # 2. OKX Market (动量 + 趋势 + 结构)
        if cfg.okx_enabled:
            try:
                from macro.sources.okx_market import fetch_okx_market
                okx = fetch_okx_market(timeout=cfg.source_timeout)
                signal.momentum_score = okx.momentum_score
                signal.trend_score = okx.trend_score
                signal.structure_score = okx.structure_score
                signal.btc_24h_change = okx.btc_24h_change
                signal.funding_rate = okx.funding_rate
                signal.order_book_ratio = okx.order_book_ratio
            except Exception as e:
                logger.warning(f"OKX 采集失败: {e}")

        # 3. Fear & Greed (情绪)
        if cfg.fear_greed_enabled:
            try:
                from macro.sources.fear_greed import fetch_fear_greed
                fg = fetch_fear_greed(timeout=cfg.source_timeout)
                signal.sentiment_score = fg.signal_score
                signal.fear_greed = fg.value
            except Exception as e:
                logger.warning(f"Fear & Greed 采集失败: {e}")

        # 4. TradingView (技术分析)
        if cfg.tradingview_enabled:
            try:
                from macro.sources.tradingview_ta import fetch_tradingview_analysis
                tv = fetch_tradingview_analysis()
                if tv.available:
                    signal.technical_score = tv.score
                    signal.tv_alignment = tv.alignment
                    signal.tv_rsi_1h = tv.rsi_1h
            except Exception as e:
                logger.debug(f"TradingView 采集失败（非致命）: {e}")

        # 5. Multi Exchange (补充动量)
        if cfg.multi_exchange_enabled:
            try:
                from macro.sources.multi_exchange import fetch_multi_exchange
                me = fetch_multi_exchange(timeout=cfg.source_timeout)
                if me.price_signal != 0:
                    signal.momentum_score += me.price_signal
            except Exception as e:
                logger.debug(f"Multi Exchange 采集失败: {e}")

        # 6. Funding Rate 情绪补充
        if signal.funding_rate > 0.005:
            signal.sentiment_score -= 1
        elif signal.funding_rate < -0.005:
            signal.sentiment_score += 1

        # 计算综合评分
        max_per_dim = 3
        raw = (
            signal.momentum_score * cfg.w_momentum
            + signal.trend_score * cfg.w_trend
            + signal.sentiment_score * cfg.w_sentiment
            + signal.structure_score * cfg.w_structure
            + signal.smart_money_score * cfg.w_smart_money
            + signal.technical_score * cfg.w_technical
        ) / max_per_dim * 100

        signal.score = max(-100, min(100, round(raw)))

        if signal.score >= cfg.long_threshold:
            signal.signal = '做多'
        elif signal.score <= cfg.short_threshold:
            signal.signal = '做空'
        else:
            signal.signal = '观望'

        logger.info(
            f"[Macro] score={signal.score} signal={signal.signal} | "
            f"SM={signal.smart_money_score} Mom={signal.momentum_score} "
            f"Trend={signal.trend_score} Sent={signal.sentiment_score} "
            f"Struct={signal.structure_score} Tech={signal.technical_score}"
        )
        return signal

    # ── 综合判断 ──────────────────────────────────────────────────

    def _synthesize(self, ms: MSSignal) -> MacroSignal:
        """综合信号 → 环境判断 + 做空友好度。"""
        macro = MacroSignal(ms=ms)
        macro.collected_at = datetime.now(timezone.utc).isoformat()
        macro.data_quality = 'good'

        reasons = []

        # 环境判断
        if ms.score >= 60:
            macro.environment = 'extreme_bullish'
            reasons.append(f"极度看多({ms.score})")
        elif ms.score >= 30:
            macro.environment = 'bullish'
            reasons.append(f"看多({ms.score})")
        elif ms.score <= -60:
            macro.environment = 'extreme_bearish'
            reasons.append(f"极度看空({ms.score})")
        elif ms.score <= -30:
            macro.environment = 'bearish'
            reasons.append(f"看空({ms.score})")
        else:
            macro.environment = 'neutral'
            reasons.append(f"中性({ms.score})")

        # 做空友好度
        if macro.environment == 'extreme_bullish':
            macro.short_friendly = False
            reasons.append("极度看多，暂停做空")
        elif macro.environment == 'bullish':
            macro.short_friendly = True
            macro.stake_multiplier = 0.5
            reasons.append("看多环境，仓位减半")
        elif macro.environment in ('bearish', 'extreme_bearish'):
            macro.short_friendly = True
            macro.stake_multiplier = 1.2 if macro.environment == 'extreme_bearish' else 1.0
            macro.score_bonus = 10 if macro.environment == 'extreme_bearish' else 5
            reasons.append("看空环境，信号增强")
        else:
            macro.short_friendly = True

        # Smart Money 特别加权
        if ms.smart_money_score <= -2:
            macro.score_bonus += 5
            reasons.append(f"SmartMoney看空({ms.smart_money_score})")
        elif ms.smart_money_score >= 2:
            macro.score_bonus -= 5
            macro.stake_multiplier = min(macro.stake_multiplier, 0.7)
            reasons.append(f"SmartMoney看多({ms.smart_money_score})")

        # TV 多周期对齐
        if ms.tv_alignment >= 2:
            macro.stake_multiplier = min(macro.stake_multiplier, 0.7)
            reasons.append("TV多周期看多对齐")
        elif ms.tv_alignment <= -2:
            macro.score_bonus += 3
            reasons.append("TV多周期看空对齐")

        # 安全边界
        macro.stake_multiplier = max(0.0, min(1.5, macro.stake_multiplier))
        macro.score_bonus = max(-15, min(15, macro.score_bonus))
        macro.reason = ' | '.join(reasons) if reasons else '数据不足'

        return macro
