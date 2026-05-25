#!/usr/bin/env python3
"""
宏观信号采集器
从 multi-signal 和 Crypto-Market-Monitor 读取宏观市场环境信号。

数据源：
  1. multi-signal: 读取 signal_latest.json（6维加权评分 -100~+100）
  2. Crypto-Market-Monitor: 可选直接调用 DecisionEngine 或读取输出文件

输出：统一的 MacroSignal 对象，供 macro/filter.py 消费。

部署方式：
  - multi-signal 通过 cron 独立运行（每4小时），输出 JSON 到指定路径
  - CMM 可选同机部署或跳过（降级为仅用 MS 信号）
  - 本模块只做「读取 + 解析 + 缓存」，不做决策
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("macro.collector")


# ══════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class MSSignal:
    """multi-signal 评分结果"""
    score: int = 0                      # -100 ~ +100
    signal: str = '观望'                 # '做多' | '做空' | '观望'
    timestamp: int = 0                  # unix timestamp
    age_minutes: float = 0.0            # 数据年龄（分钟）
    is_stale: bool = True               # 是否过期（>2小时视为过期）

    # 6维度分数
    smart_money_score: int = 0          # Smart Money 信号 (-3~+3)
    momentum_score: int = 0             # 动量 (-4~+4)
    trend_score: int = 0                # 趋势 (-4~+4)
    sentiment_score: int = 0            # 情绪 (-3~+3)
    structure_score: int = 0            # 盘口结构 (-2~+2)
    technical_score: int = 0            # TradingView TA (-3~+3)

    # 关键指标
    fear_greed: int = 50                # 恐惧贪婪指数
    funding_rate: float = 0.0           # OKX funding rate
    btc_24h_change: float = 0.0         # BTC 24h 涨跌幅 %
    order_book_ratio: float = 1.0       # 订单簿多空比
    tv_alignment: int = 0               # TradingView 多周期对齐 (-2~+2)
    tv_rsi_1h: float = 50.0             # TradingView 1H RSI

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class CMMDecision:
    """Crypto-Market-Monitor 决策结果"""
    action: str = 'HOLD'                # STRONG_BUY/BUY/HOLD/REDUCE/SELL/SHORT
    confidence: float = 0.0             # 0~100
    risk_level: int = 3                 # 1~5
    position_pct: int = 30              # 0~100 建议仓位
    phase: str = 'NEUTRAL'              # 市场阶段
    timestamp: int = 0
    age_minutes: float = 0.0
    is_stale: bool = True

    # 各层信号
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

    # 综合判断
    environment: str = 'neutral'        # 'bullish' | 'bearish' | 'neutral' | 'extreme_bullish' | 'extreme_bearish'
    short_friendly: bool = True         # 当前环境是否适合做空
    stake_multiplier: float = 1.0       # 仓位调节系数 (0.0~1.5)
    score_bonus: int = 0                # 信号评分加减分 (-15~+15)
    reason: str = ''                    # 判断原因

    collected_at: str = ''
    data_quality: str = 'good'          # 'good' | 'partial' | 'stale' | 'unavailable'

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
    # multi-signal JSON 文件路径
    ms_signal_path: str = ''            # 留空则自动搜索
    ms_search_paths: tuple = (
        os.path.expanduser('~/.openclaw/skills/multi-signal/output/signal_latest.json'),
        '/projects/sandbox/multi-signal/output/signal_latest.json',
        '../multi-signal/output/signal_latest.json',
        './multi-signal_output/signal_latest.json',
    )
    ms_stale_minutes: float = 120.0     # MS 数据超过2小时视为过期

    # CMM 输出文件路径（可选）
    cmm_output_path: str = ''           # 留空则尝试直接调用
    cmm_enabled: bool = True            # 是否启用 CMM
    cmm_stale_minutes: float = 30.0     # CMM 数据超过30分钟视为过期

    # 缓存
    cache_ttl_sec: int = 60             # 采集结果缓存60秒


# ══════════════════════════════════════════════════════════════════
#  采集器
# ══════════════════════════════════════════════════════════════════

class MacroCollector:
    """
    宏观信号采集器。

    用法:
      collector = MacroCollector(config)
      macro = collector.collect()
      if not macro.short_friendly:
          logger.warning("宏观环境不利做空，暂停")
    """

    def __init__(self, config: Optional[MacroCollectorConfig] = None):
        self.config = config or MacroCollectorConfig()
        self._cache: Optional[MacroSignal] = None
        self._cache_ts: float = 0.0

    def collect(self) -> MacroSignal:
        """
        采集并整合宏观信号。带缓存。
        """
        now = time.time()
        if self._cache and (now - self._cache_ts) < self.config.cache_ttl_sec:
            return self._cache

        ms = self._collect_ms()
        cmm = self._collect_cmm()
        macro = self._synthesize(ms, cmm)

        self._cache = macro
        self._cache_ts = now
        return macro

    def invalidate_cache(self):
        """强制下次重新采集"""
        self._cache = None

    # ── multi-signal 采集 ─────────────────────────────────────────

    def _collect_ms(self) -> MSSignal:
        """读取 multi-signal 的 signal_latest.json"""
        signal = MSSignal()

        filepath = self._find_ms_file()
        if not filepath:
            logger.debug("multi-signal 文件未找到")
            signal.is_stale = True
            return signal

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"multi-signal JSON 读取失败: {e}")
            return signal

        # 解析基础字段
        signal.score = int(data.get('score', 0))
        signal.signal = data.get('signal', '观望')
        signal.timestamp = int(data.get('timestamp', 0))

        # 计算数据年龄
        if signal.timestamp > 0:
            age_sec = time.time() - signal.timestamp
            signal.age_minutes = round(age_sec / 60, 1)
            signal.is_stale = signal.age_minutes > self.config.ms_stale_minutes

        # 解析6维度
        components = data.get('components', {})

        sm = components.get('smart_money', {})
        signal.smart_money_score = int(sm.get('score', 0))

        mom = components.get('momentum', {})
        signal.momentum_score = int(mom.get('score', 0))

        trend = components.get('trend', {})
        signal.trend_score = int(trend.get('score', 0))

        sent = components.get('sentiment', {})
        signal.sentiment_score = int(sent.get('score', 0))
        # 从 sentiment 中提取 fear_greed
        fg_str = str(sent.get('fear_greed', '50'))
        try:
            # 格式可能是 "14 (2)" 或纯数字
            signal.fear_greed = int(fg_str.split()[0].strip('('))
        except (ValueError, IndexError):
            signal.fear_greed = 50

        # funding rate
        fr_str = str(sent.get('funding_rate', '0'))
        try:
            signal.funding_rate = float(fr_str.rstrip('%'))
        except ValueError:
            signal.funding_rate = 0.0

        struct = components.get('structure', {})
        signal.structure_score = int(struct.get('score', 0))
        # order book ratio
        obr_str = str(struct.get('order_book_ratio', '1'))
        try:
            signal.order_book_ratio = float(obr_str)
        except ValueError:
            signal.order_book_ratio = 1.0

        tech = components.get('technical', {})
        signal.technical_score = int(tech.get('score', 0))
        signal.tv_alignment = int(tech.get('alignment', 0))
        signal.tv_rsi_1h = float(tech.get('rsi', 50))

        # BTC 24h change from momentum
        change_str = str(mom.get('change_24h', '0')).rstrip('%')
        try:
            signal.btc_24h_change = float(change_str)
        except ValueError:
            signal.btc_24h_change = 0.0

        logger.info(
            f"[MS] score={signal.score} signal={signal.signal} "
            f"smart={signal.smart_money_score} age={signal.age_minutes:.0f}min"
            f"{' (STALE)' if signal.is_stale else ''}"
        )
        return signal

    def _find_ms_file(self) -> Optional[str]:
        """查找 multi-signal 输出文件"""
        if self.config.ms_signal_path and os.path.exists(self.config.ms_signal_path):
            return self.config.ms_signal_path

        for path in self.config.ms_search_paths:
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                return expanded

        return None

    # ── CMM 采集 ──────────────────────────────────────────────────

    def _collect_cmm(self) -> CMMDecision:
        """采集 Crypto-Market-Monitor 的决策"""
        decision = CMMDecision()

        if not self.config.cmm_enabled:
            decision.is_stale = True
            return decision

        # 方式1: 读取输出文件
        if self.config.cmm_output_path and os.path.exists(self.config.cmm_output_path):
            return self._read_cmm_file(self.config.cmm_output_path)

        # 方式2: 尝试直接调用 CMM DecisionEngine（同机部署时）
        try:
            return self._call_cmm_directly()
        except Exception as e:
            logger.debug(f"CMM 直接调用失败（非致命）: {e}")

        decision.is_stale = True
        return decision

    def _read_cmm_file(self, filepath: str) -> CMMDecision:
        """从文件读取 CMM 决策"""
        decision = CMMDecision()
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            decision.action = data.get('action', 'HOLD')
            decision.confidence = float(data.get('confidence', 0))
            decision.risk_level = int(data.get('risk_level', 3))
            decision.position_pct = int(data.get('position_pct', 30))
            decision.phase = data.get('phase', 'NEUTRAL')
            decision.timestamp = int(data.get('timestamp', 0))
            decision.macro_signal = data.get('macro_signal', '')
            decision.trend_signal = data.get('trend_signal', '')
            decision.extreme_signal = data.get('extreme_signal', '')

            if decision.timestamp > 0:
                age_sec = time.time() - decision.timestamp
                decision.age_minutes = round(age_sec / 60, 1)
                decision.is_stale = decision.age_minutes > self.config.cmm_stale_minutes

        except Exception as e:
            logger.warning(f"CMM 文件读取失败: {e}")
            decision.is_stale = True

        return decision

    def _call_cmm_directly(self) -> CMMDecision:
        """直接调用 CMM 的 DecisionEngine（需要同机部署 + 可 import）"""
        import sys
        # 尝试找到 CMM 路径
        cmm_paths = [
            '/projects/sandbox/Crypto-Market-Monitor',
            os.path.expanduser('~/Crypto-Market-Monitor'),
            '../Crypto-Market-Monitor',
        ]

        cmm_path = None
        for p in cmm_paths:
            if os.path.exists(os.path.join(p, 'decision', 'engine.py')):
                cmm_path = p
                break

        if not cmm_path:
            raise ImportError("CMM 项目路径未找到")

        # 临时加入 sys.path
        if cmm_path not in sys.path:
            sys.path.insert(0, cmm_path)

        try:
            from decision.engine import DecisionEngine, Action
            from data_collector.sentiment import SentimentCollector
            import asyncio

            # 简化调用：只获取情绪 + 基础决策
            engine = DecisionEngine()

            # 尝试获取 fear_greed
            async def _get_fg():
                sc = SentimentCollector()
                try:
                    fg = await sc.get_fear_greed_index()
                    return fg.get('value', 50) if fg else 50
                finally:
                    await sc.close()

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    fg = 50  # 在已有 event loop 中无法嵌套 run
                else:
                    fg = loop.run_until_complete(_get_fg())
            except RuntimeError:
                fg = 50

            # 做一个轻量决策（只用 fear_greed）
            result = engine.decide(fear_greed=fg)

            decision = CMMDecision(
                action=result.action.value,
                confidence=result.confidence,
                risk_level=result.risk_level,
                position_pct=result.position_pct,
                phase=result.phase.value,
                timestamp=int(time.time()),
                age_minutes=0.0,
                is_stale=False,
                macro_signal=result.macro_signal,
                trend_signal=result.trend_signal,
                extreme_signal=result.extreme_signal,
            )
            logger.info(f"[CMM] action={decision.action} confidence={decision.confidence:.0f}%")
            return decision

        finally:
            # 清理 sys.path
            if cmm_path in sys.path:
                sys.path.remove(cmm_path)

    # ── 综合判断 ──────────────────────────────────────────────────

    def _synthesize(self, ms: MSSignal, cmm: CMMDecision) -> MacroSignal:
        """综合 MS + CMM 信号，输出统一的宏观判断"""
        macro = MacroSignal(ms=ms, cmm=cmm)
        macro.collected_at = datetime.now(timezone.utc).isoformat()

        # 数据质量评估
        if ms.is_stale and cmm.is_stale:
            macro.data_quality = 'unavailable'
        elif ms.is_stale or cmm.is_stale:
            macro.data_quality = 'partial'
        else:
            macro.data_quality = 'good'

        # ── 环境判断（核心逻辑）──
        reasons = []

        # MS 分数判断
        if not ms.is_stale:
            if ms.score >= 60:
                macro.environment = 'extreme_bullish'
                reasons.append(f"MS极度看多({ms.score})")
            elif ms.score >= 30:
                macro.environment = 'bullish'
                reasons.append(f"MS看多({ms.score})")
            elif ms.score <= -60:
                macro.environment = 'extreme_bearish'
                reasons.append(f"MS极度看空({ms.score})")
            elif ms.score <= -30:
                macro.environment = 'bearish'
                reasons.append(f"MS看空({ms.score})")
            else:
                macro.environment = 'neutral'
                reasons.append(f"MS中性({ms.score})")

        # CMM 决策叠加
        if not cmm.is_stale:
            if cmm.action in ('SELL', 'SHORT'):
                if macro.environment in ('neutral', 'bearish'):
                    macro.environment = 'bearish'
                reasons.append(f"CMM={cmm.action}")
            elif cmm.action in ('STRONG_BUY', 'BUY'):
                if macro.environment in ('neutral', 'bullish'):
                    macro.environment = 'bullish'
                reasons.append(f"CMM={cmm.action}")

        # ── 做空友好度判断 ──
        # 核心逻辑：做空策略在牛市环境下暂停
        if macro.environment in ('extreme_bullish',):
            macro.short_friendly = False
            reasons.append("极度看多环境，暂停做空")
        elif macro.environment == 'bullish':
            # 温和看多：不暂停但缩减仓位
            macro.short_friendly = True
            macro.stake_multiplier = 0.5
            reasons.append("看多环境，仓位减半")
        elif macro.environment in ('bearish', 'extreme_bearish'):
            # 看空环境：做空友好，增强信号
            macro.short_friendly = True
            macro.stake_multiplier = 1.2 if macro.environment == 'extreme_bearish' else 1.0
            macro.score_bonus = 10 if macro.environment == 'extreme_bearish' else 5
            reasons.append("看空环境，信号增强")
        else:
            # 中性：正常做空
            macro.short_friendly = True
            macro.stake_multiplier = 1.0
            macro.score_bonus = 0

        # ── Smart Money 特别信号 ──
        if not ms.is_stale and ms.smart_money_score <= -2:
            # 大户集中卖出 → 做空加分
            macro.score_bonus += 5
            reasons.append(f"SmartMoney看空({ms.smart_money_score})")
        elif not ms.is_stale and ms.smart_money_score >= 2:
            # 大户集中买入 → 做空减分
            macro.score_bonus -= 5
            macro.stake_multiplier = min(macro.stake_multiplier, 0.7)
            reasons.append(f"SmartMoney看多({ms.smart_money_score})")

        # ── CMM 风险等级影响 ──
        if not cmm.is_stale and cmm.risk_level >= 4:
            macro.stake_multiplier = min(macro.stake_multiplier, 0.6)
            reasons.append(f"CMM高风险({cmm.risk_level}/5)")

        # ── TradingView 多周期对齐 ──
        if not ms.is_stale and ms.tv_alignment >= 2:
            # 多周期看多对齐 → 做空不利
            macro.stake_multiplier = min(macro.stake_multiplier, 0.7)
            reasons.append("TV多周期看多对齐")
        elif not ms.is_stale and ms.tv_alignment <= -2:
            # 多周期看空对齐 → 做空有利
            macro.score_bonus += 3
            reasons.append("TV多周期看空对齐")

        # 安全边界
        macro.stake_multiplier = max(0.0, min(1.5, macro.stake_multiplier))
        macro.score_bonus = max(-15, min(15, macro.score_bonus))

        macro.reason = ' | '.join(reasons) if reasons else '数据不足'
        return macro
