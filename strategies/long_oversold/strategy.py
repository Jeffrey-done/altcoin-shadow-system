#!/usr/bin/env python3
"""
超卖做多策略 v1.0

信号生成逻辑：
  1. 扫描：日线 RSI < 25 + 24h 跌幅 > 15% + 成交量 > 100万U
  2. 确认：4h RSI 从谷值回升 ≥ 8 点（反弹开始确认）
  3. 退出：TP1 +5% / TP2 +10% / 硬止损 -4% / 时间止损 48h

风险特征：
  - 胜率预期 55~65%（超卖反弹概率）
  - 盈亏比 1.5~2.5（止盈 5~10% vs 止损 4%）
  - 最大持仓 48h，避免陷入趋势性下跌
  - 杠杆 5x（比做空策略保守，因为做多在下跌趋势中）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

from strategies.base import (
    BaseStrategy, DataFeed, MarketSnapshot,
    Candidate, Signal, SignalDirection,
    ExitSignal, ExitReason, TradeContext,
)

logger = logging.getLogger("strategy.long_oversold")


@dataclass
class LongOversoldParams:
    """策略参数"""
    # 扫描条件
    vol_min: float = 1_000_000
    price_max: float = 5.0
    pct_24h_max: float = -15.0          # 24h 跌幅阈值（负数）

    # RSI
    rsi_period: int = 14
    daily_rsi_max: float = 25.0         # 日线 RSI 超卖阈值
    h4_rsi_enter: float = 30.0          # 4h RSI 反弹进入阈值
    h4_rsi_rise: float = 8.0            # 4h RSI 需从谷值回升的点数

    # 仓位
    default_stake: float = 20.0
    leverage: int = 5
    max_open_positions: int = 2

    # 止盈止损
    tp1_pct: float = 5.0
    tp2_pct: float = 10.0
    tp1_close_ratio: float = 0.5
    hard_stop_pct: float = 4.0
    trail_activate_pct: float = 4.0
    trail_retrace_ratio: float = 0.4
    max_hold_hours: int = 48

    # 评分
    score_full_threshold: float = 65.0
    score_half_threshold: float = 40.0


class LongOversoldStrategy(BaseStrategy):
    """超卖做多策略"""

    def __init__(self, params: Optional[LongOversoldParams] = None):
        self._params = params or LongOversoldParams()

    @property
    def name(self) -> str:
        return 'long_oversold'

    @property
    def version(self) -> str:
        return '1.0.0'

    @property
    def description(self) -> str:
        return '山寨币超卖后均值回归做多（对冲做空策略风险）'

    @property
    def direction(self) -> SignalDirection:
        return SignalDirection.LONG

    # ── 核心方法 ──────────────────────────────────────────────────

    def scan(self, data_feed: DataFeed, market: MarketSnapshot) -> List[Candidate]:
        """扫描超卖品种"""
        candidates = []
        tickers = market.tickers or data_feed.get_tickers()
        params = self._params

        for symbol, ticker in tickers.items():
            if not symbol.endswith('/USDT'):
                continue

            # 基础过滤
            price = ticker.get('last', 0)
            vol_24h = ticker.get('quoteVolume', 0)
            pct_24h = ticker.get('percentage', 0)

            if price <= 0 or price > params.price_max:
                continue
            if vol_24h < params.vol_min:
                continue
            if pct_24h > params.pct_24h_max:  # 跌幅不够
                continue

            # RSI 检查（通过 K 线计算）
            try:
                klines = data_feed.get_ohlcv(symbol, '1d', limit=20)
                if len(klines) < params.rsi_period + 1:
                    continue
                closes = [k[4] for k in klines]
                rsi = self._calc_rsi(closes, params.rsi_period)
                if rsi > params.daily_rsi_max:
                    continue
            except Exception:
                continue

            # 评分
            score = self._scan_score(rsi, pct_24h, vol_24h)

            candidates.append(Candidate(
                symbol=symbol,
                price=price,
                score=score,
                timeframe='1d',
                metadata={
                    'rsi_1d': rsi,
                    'pct_24h': pct_24h,
                    'vol_24h': vol_24h,
                    'direction': 'LONG',
                },
            ))

        candidates.sort(key=lambda c: c.score, reverse=True)
        if candidates:
            logger.info(f"📈 LongOversold 扫描: {len(candidates)} 个超卖候选")
        return candidates[:10]

    def confirm(self, candidate: Candidate, data_feed: DataFeed) -> Optional[Signal]:
        """确认：4h RSI 反弹信号"""
        symbol = candidate.symbol
        params = self._params
        meta = candidate.metadata

        try:
            # 获取 4h K 线
            klines_4h = data_feed.get_ohlcv(symbol, '4h', limit=20)
            if len(klines_4h) < params.rsi_period + 1:
                return None

            closes_4h = [k[4] for k in klines_4h]
            current_price = closes_4h[-1] if closes_4h else candidate.price
            rsi_4h = self._calc_rsi(closes_4h, params.rsi_period)

            # 计算 4h RSI 谷值
            rsi_values = []
            for i in range(len(closes_4h) - params.rsi_period, len(closes_4h)):
                if i >= params.rsi_period:
                    subset = closes_4h[:i + 1]
                    rsi_values.append(self._calc_rsi(subset, params.rsi_period))

            if not rsi_values:
                return None

            rsi_trough = min(rsi_values[-10:]) if len(rsi_values) >= 10 else min(rsi_values)
            rsi_rise = rsi_4h - rsi_trough

            # 反弹确认条件
            if rsi_4h > params.h4_rsi_enter:
                return None  # RSI 已经太高，错过最佳入场
            if rsi_rise < params.h4_rsi_rise:
                return None  # 还没有明确反弹信号

        except Exception as e:
            logger.debug(f"[LongOversold] confirm 异常 ({symbol}): {e}")
            return None

        # 评分
        score = self._confirm_score(
            rsi_1d=meta.get('rsi_1d', 25),
            rsi_4h=rsi_4h,
            rsi_rise=rsi_rise,
            pct_24h=meta.get('pct_24h', -15),
            vol_24h=meta.get('vol_24h', 0),
        )

        if score < params.score_half_threshold:
            return None

        # 仓位
        if score >= params.score_full_threshold:
            stake = params.default_stake
        else:
            stake = round(params.default_stake * 0.5)

        signal = Signal(
            symbol=symbol,
            direction=SignalDirection.LONG,
            score=score,
            stake=stake,
            leverage=params.leverage,
            hard_stop_pct=params.hard_stop_pct,
            tp1_pct=params.tp1_pct,
            tp2_pct=params.tp2_pct,
            trail_retrace_ratio=params.trail_retrace_ratio,
            max_hold_hours=params.max_hold_hours,
            strategy_name=self.name,
            strategy_version=self.version,
            trigger_type='4h_rsi_bounce',
            reason=(
                f"超卖反弹: RSI(1d)={meta.get('rsi_1d', 0):.0f}, "
                f"4h RSI 从{rsi_trough:.0f}回升到{rsi_4h:.0f} (+{rsi_rise:.0f})"
            ),
            metadata={
                'entry_ref_price': current_price,
                'rsi_4h': rsi_4h,
                'rsi_trough': rsi_trough,
                'rsi_rise': rsi_rise,
            },
        )

        logger.info(
            f"📈 [LongOversold] 信号: {symbol} LONG score={score} "
            f"RSI回升 {rsi_trough:.0f}→{rsi_4h:.0f}"
        )
        return signal

    def evaluate_exit(self, trade: TradeContext, data_feed: DataFeed) -> Optional[ExitSignal]:
        """退出评估"""
        params = self._params

        # 做多盈亏
        pnl_pct = (trade.current_price - trade.entry_price) / trade.entry_price * 100

        # 1. 硬止损
        if pnl_pct <= -params.hard_stop_pct:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.HARD_STOP,
                pnl_estimate=pnl_pct,
                strategy_name=self.name,
                description=f"做多硬止损: {pnl_pct:.2f}%",
            )

        # 2. TP2 止盈
        if pnl_pct >= params.tp2_pct:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.TP2,
                pnl_estimate=pnl_pct,
                strategy_name=self.name,
                description=f"做多止盈 TP2: {pnl_pct:.2f}%",
            )

        # 3. TP1 半仓（如果还没触发）
        if not trade.tp1_triggered and pnl_pct >= params.tp1_pct:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.TP1,
                close_ratio=params.tp1_close_ratio,
                pnl_estimate=pnl_pct,
                strategy_name=self.name,
                description=f"做多 TP1 半仓: {pnl_pct:.2f}%",
            )

        # 4. 移动止损
        if trade.best_pnl_pct >= params.trail_activate_pct:
            trail_trigger = trade.best_pnl_pct * (1 - params.trail_retrace_ratio)
            if pnl_pct <= trail_trigger:
                return ExitSignal(
                    trade_id=trade.trade_id,
                    reason=ExitReason.TRAIL_STOP,
                    pnl_estimate=pnl_pct,
                    strategy_name=self.name,
                    description=(
                        f"做多移动止损: best={trade.best_pnl_pct:.1f}%, "
                        f"current={pnl_pct:.1f}%"
                    ),
                )

        # 5. 时间止损
        if trade.hold_hours >= params.max_hold_hours:
            if pnl_pct < params.tp1_pct * 0.5:  # 盈利不足 TP1 的一半
                return ExitSignal(
                    trade_id=trade.trade_id,
                    reason=ExitReason.TIME_STOP,
                    pnl_estimate=pnl_pct,
                    strategy_name=self.name,
                    description=f"做多超时 {trade.hold_hours:.0f}h, pnl={pnl_pct:.2f}%",
                )

        return None

    # ── 参数管理 ──────────────────────────────────────────────────

    def get_params(self) -> Dict[str, Any]:
        return {
            'vol_min': self._params.vol_min,
            'price_max': self._params.price_max,
            'pct_24h_max': self._params.pct_24h_max,
            'daily_rsi_max': self._params.daily_rsi_max,
            'h4_rsi_enter': self._params.h4_rsi_enter,
            'h4_rsi_rise': self._params.h4_rsi_rise,
            'default_stake': self._params.default_stake,
            'leverage': self._params.leverage,
            'hard_stop_pct': self._params.hard_stop_pct,
            'tp1_pct': self._params.tp1_pct,
            'tp2_pct': self._params.tp2_pct,
            'max_hold_hours': self._params.max_hold_hours,
        }

    def set_params(self, params: Dict[str, Any]) -> None:
        for key, value in params.items():
            if hasattr(self._params, key):
                setattr(self._params, key, value)

    def get_param_space(self) -> Dict[str, Dict[str, Any]]:
        return {
            'daily_rsi_max': {'type': 'float', 'low': 15, 'high': 35, 'step': 2},
            'h4_rsi_rise': {'type': 'float', 'low': 4, 'high': 15, 'step': 1},
            'hard_stop_pct': {'type': 'float', 'low': 2.0, 'high': 6.0, 'step': 0.5},
            'tp1_pct': {'type': 'float', 'low': 3.0, 'high': 8.0, 'step': 1.0},
            'tp2_pct': {'type': 'float', 'low': 6.0, 'high': 15.0, 'step': 1.0},
            'leverage': {'type': 'int', 'low': 3, 'high': 10, 'step': 1},
            'pct_24h_max': {'type': 'float', 'low': -30, 'high': -10, 'step': 2},
        }

    # ── 工具方法 ──────────────────────────────────────────────────

    def _calc_rsi(self, closes: list, period: int = 14) -> float:
        """Wilder RSI 计算"""
        if len(closes) < period + 1:
            return 50.0
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _scan_score(self, rsi: float, pct_24h: float, vol_24h: float) -> float:
        """扫描阶段评分"""
        score = 0.0
        # RSI 越低越好 (0~35)
        if rsi <= 15:
            score += 35
        elif rsi <= 20:
            score += 25
        elif rsi <= 25:
            score += 15

        # 跌幅越大越好 (0~35)
        abs_drop = abs(pct_24h)
        if abs_drop >= 30:
            score += 35
        elif abs_drop >= 20:
            score += 25
        elif abs_drop >= 15:
            score += 15

        # 成交量 (0~30)
        if vol_24h >= 20_000_000:
            score += 30
        elif vol_24h >= 5_000_000:
            score += 20
        elif vol_24h >= 1_000_000:
            score += 10

        return min(100, score)

    def _confirm_score(self, rsi_1d: float, rsi_4h: float, rsi_rise: float,
                       pct_24h: float, vol_24h: float) -> float:
        """确认阶段评分"""
        score = 0.0

        # RSI 超卖深度 (0~30)
        if rsi_1d <= 15:
            score += 30
        elif rsi_1d <= 20:
            score += 22
        elif rsi_1d <= 25:
            score += 15

        # 4h RSI 反弹力度 (0~30)
        if rsi_rise >= 15:
            score += 30
        elif rsi_rise >= 10:
            score += 22
        elif rsi_rise >= 8:
            score += 15

        # 跌幅 (0~20)
        abs_drop = abs(pct_24h)
        if abs_drop >= 25:
            score += 20
        elif abs_drop >= 18:
            score += 14
        elif abs_drop >= 12:
            score += 8

        # 成交量 (0~20)
        if vol_24h >= 10_000_000:
            score += 20
        elif vol_24h >= 3_000_000:
            score += 12
        elif vol_24h >= 1_000_000:
            score += 6

        return min(100, round(score))
