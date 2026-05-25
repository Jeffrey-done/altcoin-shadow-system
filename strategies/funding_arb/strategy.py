#!/usr/bin/env python3
"""
Funding Rate 套利策略 v1.0

核心逻辑：
  永续合约每 8h 结算一次资金费率。当费率极端偏离时（如 >0.1%/8h），
  意味着市场单边拥挤，费率有强烈的均值回归倾向。

  策略利用两个 alpha：
    1. 费率收入 alpha：持仓方向与费率方向相反，直接吃费率
    2. 价格回归 alpha：极端费率往往伴随价格过冲，方向反转概率高

做空费率套利（正费率极高时）：
  - 条件：funding_rate > 0.08%/8h（年化 >350%）
  - 动作：做空（空头收费率）
  - 预期：吃 1~3 次 0.08%+ 费率 = 0.24%+ 收入，+价格回落加成
  - 持仓：8~24h（跨 1~3 次费率结算）

做多费率套利（负费率极端时）：
  - 条件：funding_rate < -0.05%/8h
  - 动作：做多（多头收费率）
  - 预期：吃负费率 + 空头踩踏反弹
  - 持仓：8~24h

风险控制：
  - 硬止损 2%（费率套利是小利润策略，不能扛大亏损）
  - 时间止损 24h（费率可能持续极端，不能无限等待）
  - 最大持仓 2 笔（市场情绪同方向时避免集中暴露）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from strategies.base import (
    BaseStrategy, DataFeed, MarketSnapshot,
    Candidate, Signal, SignalDirection,
    ExitSignal, ExitReason, TradeContext,
)

logger = logging.getLogger("strategy.funding_arb")


@dataclass
class FundingArbParams:
    """策略参数"""
    # 扫描阈值
    positive_rate_threshold: float = 0.08   # 正费率入场阈值 (%/8h)
    negative_rate_threshold: float = -0.05  # 负费率入场阈值 (%/8h)

    # 过滤条件
    min_volume_24h: float = 2_000_000       # 最低 24h 成交量 (USDT)
    min_oi_usdt: float = 5_000_000          # 最低 OI (USDT) — 确保流动性
    max_spread_pct: float = 0.1             # 最大买卖价差 %

    # 仓位
    default_stake: float = 30.0             # 默认保证金 (USDT)
    leverage: int = 5                       # 杠杆（低于做空策略，风险小利润）
    max_open_positions: int = 2             # 最大同时持仓数

    # 止盈止损
    hard_stop_pct: float = 2.0              # 硬止损 %（小利润策略必须严格止损）
    take_profit_pct: float = 1.5            # 止盈 %（费率收入 + 价格回落）
    time_stop_hours: int = 24               # 时间止损（跨 3 次结算后退出）
    min_profit_for_hold: float = 0.3        # 超时但已盈利 >0.3% 则继续持有

    # 评分加权
    rate_magnitude_weight: float = 40.0     # 费率绝对值越大越好
    oi_change_weight: float = 20.0          # OI 涨幅越大（投机越热）越好
    volume_spike_weight: float = 20.0       # 成交量突增 bonus
    cross_exchange_weight: float = 20.0     # 多所费率一致 bonus

    # 交叉验证
    cross_validate: bool = True             # 是否检查 OKX 费率一致性
    cross_rate_agreement_pct: float = 0.03  # 两所费率偏差阈值


class FundingArbStrategy(BaseStrategy):
    """Funding Rate 套利策略"""

    def __init__(self, params: Optional[FundingArbParams] = None):
        self._params = params or FundingArbParams()

    @property
    def name(self) -> str:
        return 'funding_arb'

    @property
    def version(self) -> str:
        return '1.0.0'

    @property
    def description(self) -> str:
        return '资金费率极端偏离时的均值回归套利（市场中性）'

    @property
    def direction(self) -> SignalDirection:
        # 本策略可做多也可做空，默认标记为 SHORT（主要场景）
        return SignalDirection.SHORT

    # ── 核心方法 ──────────────────────────────────────────────────

    def scan(self, data_feed: DataFeed, market: MarketSnapshot) -> List[Candidate]:
        """
        扫描全市场，找到资金费率极端的品种。
        """
        candidates = []
        tickers = market.tickers or data_feed.get_tickers()

        for symbol, ticker in tickers.items():
            if not symbol.endswith('/USDT'):
                continue

            # 成交量过滤
            vol_24h = ticker.get('quoteVolume', 0)
            if vol_24h < self._params.min_volume_24h:
                continue

            # 获取资金费率
            try:
                funding_rate = data_feed.get_funding_rate(symbol)
            except Exception:
                continue

            if funding_rate == 0:
                continue

            # 判断是否超过阈值
            is_positive_extreme = funding_rate >= self._params.positive_rate_threshold
            is_negative_extreme = funding_rate <= self._params.negative_rate_threshold

            if not (is_positive_extreme or is_negative_extreme):
                continue

            # 构造候选
            direction = 'SHORT' if is_positive_extreme else 'LONG'
            score = self._calculate_scan_score(funding_rate, vol_24h, ticker)

            candidates.append(Candidate(
                symbol=symbol,
                price=ticker.get('last', 0),
                score=score,
                timeframe='funding',
                metadata={
                    'funding_rate': funding_rate,
                    'direction': direction,
                    'vol_24h': vol_24h,
                    'rate_type': 'positive_extreme' if is_positive_extreme else 'negative_extreme',
                },
            ))

        # 按评分排序，取 top N
        candidates.sort(key=lambda c: c.score, reverse=True)
        if candidates:
            logger.info(
                f"📡 Funding Arb 扫描: 发现 {len(candidates)} 个极端费率品种"
            )
        return candidates[:10]

    def confirm(self, candidate: Candidate, data_feed: DataFeed) -> Optional[Signal]:
        """
        确认候选：检查费率持续性 + 流动性 + 价差。
        """
        symbol = candidate.symbol
        meta = candidate.metadata
        direction_str = meta.get('direction', 'SHORT')

        # 再次获取最新费率（可能已回落）
        try:
            current_rate = data_feed.get_funding_rate(symbol)
        except Exception:
            logger.debug(f"[FundingArb] {symbol} 获取费率失败，跳过")
            return None

        # 费率已回落到阈值内 → 信号消失
        if direction_str == 'SHORT' and current_rate < self._params.positive_rate_threshold * 0.8:
            logger.debug(f"[FundingArb] {symbol} 正费率已回落 ({current_rate:.4f}%)，跳过")
            return None
        if direction_str == 'LONG' and current_rate > self._params.negative_rate_threshold * 0.8:
            logger.debug(f"[FundingArb] {symbol} 负费率已回落 ({current_rate:.4f}%)，跳过")
            return None

        # 检查订单簿流动性
        try:
            orderbook = data_feed.get_orderbook(symbol, depth=10)
            if not orderbook.get('bids') or not orderbook.get('asks'):
                return None

            best_bid = orderbook['bids'][0][0]
            best_ask = orderbook['asks'][0][0]
            spread_pct = (best_ask - best_bid) / best_bid * 100

            if spread_pct > self._params.max_spread_pct:
                logger.debug(
                    f"[FundingArb] {symbol} 价差过大 ({spread_pct:.3f}% > {self._params.max_spread_pct}%)"
                )
                return None
        except Exception:
            pass  # 订单簿不可用时不阻塞

        # 交叉验证（OKX 费率一致性）
        cross_bonus = 0
        if self._params.cross_validate:
            cross_bonus = self._check_cross_exchange(symbol, current_rate, data_feed)

        # 最终评分
        score = self._calculate_confirm_score(
            current_rate, meta.get('vol_24h', 0), cross_bonus
        )

        if score < 40:
            return None

        # 构造信号
        direction = SignalDirection.SHORT if direction_str == 'SHORT' else SignalDirection.LONG

        signal = Signal(
            symbol=symbol,
            direction=direction,
            score=score,
            stake=self._params.default_stake,
            leverage=self._params.leverage,
            hard_stop_pct=self._params.hard_stop_pct,
            tp1_pct=self._params.take_profit_pct * 0.6,
            tp2_pct=self._params.take_profit_pct,
            trail_retrace_ratio=0.5,
            max_hold_hours=self._params.time_stop_hours,
            strategy_name=self.name,
            strategy_version=self.version,
            trigger_type='funding_extreme',
            reason=self._build_reason(symbol, direction_str, current_rate, score),
            metadata={
                'funding_rate': current_rate,
                'cross_bonus': cross_bonus,
                'rate_type': meta.get('rate_type', ''),
            },
        )

        logger.info(
            f"📊 [FundingArb] 信号确认: {symbol} {direction_str} "
            f"rate={current_rate:.4f}%/8h score={score}"
        )
        return signal

    def evaluate_exit(self, trade: TradeContext, data_feed: DataFeed) -> Optional[ExitSignal]:
        """
        退出评估：硬止损 / 止盈 / 时间止损 / 费率回归退出。
        """
        params = self._params

        # 计算盈亏 %
        if trade.direction == 'SHORT':
            pnl_pct = (trade.entry_price - trade.current_price) / trade.entry_price * 100
        else:
            pnl_pct = (trade.current_price - trade.entry_price) / trade.entry_price * 100

        # 1. 硬止损
        if pnl_pct <= -params.hard_stop_pct:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.HARD_STOP,
                pnl_estimate=pnl_pct,
                strategy_name=self.name,
                description=f"费率套利硬止损: {pnl_pct:.2f}% < -{params.hard_stop_pct}%",
            )

        # 2. 止盈
        if pnl_pct >= params.take_profit_pct:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.TP2,
                pnl_estimate=pnl_pct,
                strategy_name=self.name,
                description=f"费率套利止盈: {pnl_pct:.2f}% >= {params.take_profit_pct}%",
            )

        # 3. 时间止损
        if trade.hold_hours >= params.time_stop_hours:
            if pnl_pct < params.min_profit_for_hold:
                return ExitSignal(
                    trade_id=trade.trade_id,
                    reason=ExitReason.TIME_STOP,
                    pnl_estimate=pnl_pct,
                    strategy_name=self.name,
                    description=f"费率套利超时 {trade.hold_hours:.0f}h, pnl={pnl_pct:.2f}%",
                )

        # 4. 费率回归退出（费率已正常化，持仓意义降低）
        try:
            current_rate = data_feed.get_funding_rate(trade.symbol)
            rate_normalized = (
                (trade.direction == 'SHORT' and current_rate < 0.02)
                or (trade.direction == 'LONG' and current_rate > -0.02)
            )
            if rate_normalized and pnl_pct > 0.3 and trade.hold_hours >= 8:
                return ExitSignal(
                    trade_id=trade.trade_id,
                    reason=ExitReason.TP2,
                    pnl_estimate=pnl_pct,
                    strategy_name=self.name,
                    description=f"费率回归正常 ({current_rate:.4f}%), 盈利退出 {pnl_pct:.2f}%",
                )
        except Exception:
            pass

        return None

    # ── 参数管理 ──────────────────────────────────────────────────

    def get_params(self) -> Dict[str, Any]:
        return {
            'positive_rate_threshold': self._params.positive_rate_threshold,
            'negative_rate_threshold': self._params.negative_rate_threshold,
            'min_volume_24h': self._params.min_volume_24h,
            'default_stake': self._params.default_stake,
            'leverage': self._params.leverage,
            'hard_stop_pct': self._params.hard_stop_pct,
            'take_profit_pct': self._params.take_profit_pct,
            'time_stop_hours': self._params.time_stop_hours,
            'max_open_positions': self._params.max_open_positions,
            'cross_validate': self._params.cross_validate,
        }

    def set_params(self, params: Dict[str, Any]) -> None:
        for key, value in params.items():
            if hasattr(self._params, key):
                setattr(self._params, key, value)

    def get_param_space(self) -> Dict[str, Dict[str, Any]]:
        return {
            'positive_rate_threshold': {
                'type': 'float', 'low': 0.05, 'high': 0.20, 'step': 0.01
            },
            'negative_rate_threshold': {
                'type': 'float', 'low': -0.15, 'high': -0.03, 'step': 0.01
            },
            'hard_stop_pct': {
                'type': 'float', 'low': 1.0, 'high': 4.0, 'step': 0.5
            },
            'take_profit_pct': {
                'type': 'float', 'low': 0.5, 'high': 3.0, 'step': 0.25
            },
            'leverage': {
                'type': 'int', 'low': 3, 'high': 10, 'step': 1
            },
        }

    # ── 私有方法 ──────────────────────────────────────────────────

    def _calculate_scan_score(self, funding_rate: float, vol_24h: float,
                              ticker: dict) -> float:
        """扫描阶段粗筛评分 (0~100)"""
        score = 0.0

        # 费率绝对值越大越好 (0~40)
        abs_rate = abs(funding_rate)
        if abs_rate >= 0.20:
            score += 40
        elif abs_rate >= 0.12:
            score += 30
        elif abs_rate >= 0.08:
            score += 20
        elif abs_rate >= 0.05:
            score += 10

        # 成交量 (0~30)
        if vol_24h >= 50_000_000:
            score += 30
        elif vol_24h >= 20_000_000:
            score += 20
        elif vol_24h >= 5_000_000:
            score += 10

        # 24h 涨跌幅（价格过冲越大，回归概率越高）(0~30)
        pct_24h = abs(ticker.get('percentage', 0) or 0)
        if pct_24h >= 20:
            score += 30
        elif pct_24h >= 10:
            score += 20
        elif pct_24h >= 5:
            score += 10

        return min(100, score)

    def _calculate_confirm_score(self, funding_rate: float, vol_24h: float,
                                 cross_bonus: int) -> float:
        """确认阶段精确评分 (0~100)"""
        params = self._params
        score = 0.0

        # 费率强度 (0~40)
        abs_rate = abs(funding_rate)
        rate_score = min(40, abs_rate / 0.20 * params.rate_magnitude_weight)
        score += rate_score

        # 成交量 (0~20)
        vol_score = min(20, (vol_24h / 50_000_000) * params.volume_spike_weight)
        score += vol_score

        # 交叉验证 bonus (0~20)
        score += min(20, cross_bonus)

        # 基础分 (保底 20)
        score += 20

        return min(100, round(score))

    def _check_cross_exchange(self, symbol: str, binance_rate: float,
                              data_feed: DataFeed) -> int:
        """交叉验证 OKX 费率"""
        try:
            # 尝试从 exchange_manager 获取 OKX 费率
            from exchange_manager import get_okx_funding_rate
            okx_rate = get_okx_funding_rate(symbol)

            if okx_rate is None:
                return 0

            # 两所费率方向一致且幅度接近
            same_direction = (binance_rate > 0 and okx_rate > 0) or \
                             (binance_rate < 0 and okx_rate < 0)

            if same_direction:
                diff = abs(abs(binance_rate) - abs(okx_rate))
                if diff < self._params.cross_rate_agreement_pct:
                    return 15  # 两所一致，高置信度
                return 8       # 方向一致但幅度有差异
            return 0
        except Exception:
            return 0

    def _build_reason(self, symbol: str, direction: str, rate: float,
                      score: int) -> str:
        """构建人类可读的触发原因"""
        if direction == 'SHORT':
            return (
                f"正费率极端 ({rate:.4f}%/8h, 年化{rate*3*365:.0f}%)，"
                f"多头过度拥挤，做空吃费率+回落"
            )
        else:
            return (
                f"负费率极端 ({rate:.4f}%/8h)，"
                f"空头过度拥挤，做多吃费率+反弹"
            )
