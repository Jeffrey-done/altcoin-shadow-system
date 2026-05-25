"""
Pre-Pump Sniffer 策略实现 — BaseStrategy 接口。
检测妖币起飞前的吸筹异常信号，突破确认后做多。
"""

from __future__ import annotations

import logging
from typing import List, Optional, Dict, Any

from strategies.base import (
    BaseStrategy, DataFeed, MarketSnapshot, Signal, ExitSignal,
    Candidate, TradeContext, SignalDirection, ExitReason,
)
from strategies.prepump_sniffer.params import PrePumpParams
from strategies.prepump_sniffer.indicators import calculate_anomaly_score

logger = logging.getLogger("strategy.prepump_sniffer")


class PrePumpSnifferStrategy(BaseStrategy):
    """
    妖币起飞前嗅探器 v1.0
    在价格横盘时检测异常资金流入，突破确认后做多。
    """

    def __init__(self, params: Optional[PrePumpParams] = None):
        self._params = params or PrePumpParams()

    @property
    def name(self) -> str:
        return 'prepump_sniffer'

    @property
    def version(self) -> str:
        return '1.0.0'

    @property
    def description(self) -> str:
        return '妖币起飞前嗅探：7维度异常检测 + 突破确认做多'

    @property
    def direction(self) -> SignalDirection:
        return SignalDirection.LONG

    # ── 核心方法：扫描 ────────────────────────────────────────────

    def scan(self, data_feed: DataFeed, market: MarketSnapshot) -> List[Candidate]:
        """扫描全市场，找横盘中出现异常资金流入的币"""
        p = self._params
        candidates = []

        for symbol, ticker in market.tickers.items():
            if not symbol.endswith('/USDT'):
                continue

            price = ticker.get('last', 0) or 0
            vol24h = ticker.get('quoteVolume', 0) or 0
            pct24h = ticker.get('percentage', 0) or 0

            # 基础过滤：横盘中的小币
            if price <= 0 or price > p.price_max:
                continue
            if vol24h < p.vol_min:
                continue
            if abs(pct24h) > p.price_change_max:
                continue  # 已经在动的不要

            candidates.append(Candidate(
                symbol=symbol,
                price=price,
                score=0,
                metadata={
                    'vol24h': round(vol24h),
                    'pct24h': round(pct24h, 1),
                },
            ))

        logger.info(f"扫描完成：{len(candidates)} 个横盘小币（待异常检测）")
        return candidates

    # ── 核心方法：确认 ────────────────────────────────────────────

    def confirm(self, candidate: Candidate, data_feed: DataFeed) -> Optional[Signal]:
        """对候选币执行7维度异常检测 + 突破确认"""
        p = self._params
        symbol = candidate.symbol

        # 获取数据
        try:
            ohlcv_1h = data_feed.get_ohlcv(symbol, '1h', limit=30)
            if len(ohlcv_1h) < 10:
                return None
        except Exception:
            return None

        closes = [c[4] for c in ohlcv_1h]
        volumes = [c[5] for c in ohlcv_1h]
        highs = [c[2] for c in ohlcv_1h]

        if len(closes) < 8:
            return None

        # 计算指标
        vol_current = volumes[-1]
        vol_24h_avg = sum(volumes[-24:]) / min(24, len(volumes)) if len(volumes) >= 6 else 0

        # 价格变化
        price_change_4h = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
        price_change_1h = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        price_change_3h = (closes[-1] - closes[-4]) / closes[-4] * 100 if len(closes) >= 4 else 0

        # OI 变化
        oi_change = data_feed.get_oi_change(symbol) * 100  # 转为百分比

        # Funding
        funding_now = data_feed.get_funding_rate(symbol)
        funding_4h_ago = funding_now * 0.5  # 简化：假设4H前是当前的一半

        # 成交量序列
        vol_3h = volumes[-3:] if len(volumes) >= 3 else [0, 0, 0]

        # BB width percentile (简化计算)
        import numpy as np
        if len(closes) >= 20:
            bb_std = np.std(closes[-20:])
            bb_width = bb_std / closes[-1] * 100
            recent_widths = []
            for j in range(min(7*24, len(closes) - 20)):
                start = max(0, len(closes) - 20 - j - 1)
                end = start + 20
                if end <= len(closes):
                    w = np.std(closes[start:end]) / closes[end-1] * 100 if closes[end-1] > 0 else 0
                    recent_widths.append(w)
            if recent_widths:
                bb_pctile = sum(1 for w in recent_widths if bb_width <= w) / len(recent_widths) * 100
            else:
                bb_pctile = 50.0
        else:
            bb_pctile = 50.0

        # ── 7维度异常评分 ──
        score, details = calculate_anomaly_score(
            vol_current=vol_current,
            vol_24h_avg=vol_24h_avg,
            price_change_4h_pct=price_change_4h,
            price_change_1h_pct=price_change_1h,
            price_change_3h_pct=price_change_3h,
            oi_4h_change_pct=oi_change,
            funding_now=funding_now,
            funding_4h_ago=funding_4h_ago,
            vol_series_3h=vol_3h,
            bb_width_percentile=bb_pctile,
            params=p.to_dict(),
        )

        if score < p.signal_threshold:
            return None

        # ── 等待突破确认 ──
        # 检查是否已经突破近期高点
        recent_high = max(highs[-p.breakout_lookback:]) if len(highs) >= p.breakout_lookback else max(highs)
        current_price = closes[-1]
        breakout_pct = (current_price - recent_high) / recent_high * 100

        if breakout_pct < p.breakout_pct:
            # 还没突破，标记为候选但不触发
            return None

        # 突破确认 + 放量
        if vol_current < vol_24h_avg * p.breakout_vol_mult:
            return None  # 突破但没放量，假突破

        # ── 生成做多信号 ──
        signal_score = min(100, score * 15 + int(breakout_pct * 5))
        reason = f"Pre-Pump嗅探 score={score}/7 [{' '.join(details)}] 突破{breakout_pct:.1f}%"

        logger.info(f"  🔮 {symbol}: {reason}")

        return Signal(
            symbol=symbol,
            direction=SignalDirection.LONG,
            score=signal_score,
            stake=p.default_stake,
            leverage=p.leverage,
            hard_stop_pct=p.hard_stop_pct,
            tp1_pct=p.tp1_pct,
            tp2_pct=p.tp2_pct,
            trail_retrace_ratio=p.trail_retrace_ratio,
            max_hold_hours=p.max_hold_hours,
            strategy_name=self.name,
            strategy_version=self.version,
            trigger_type='prepump_breakout',
            reason=reason,
            metadata={
                'anomaly_score': score,
                'anomaly_details': details,
                'breakout_pct': breakout_pct,
                'vol_ratio': vol_current / vol_24h_avg if vol_24h_avg > 0 else 0,
                'oi_change_pct': oi_change,
            },
        )

    # ── 核心方法：退出评估 ─────────────────────────────────────────

    def evaluate_exit(self, trade: TradeContext, data_feed: DataFeed) -> Optional[ExitSignal]:
        """评估持仓是否需要平仓"""
        p = self._params
        entry = trade.entry_price
        current = trade.current_price

        if entry <= 0 or current <= 0:
            return None

        # 做多 PnL%
        pnl_pct = (current - entry) / entry * 100

        # 1. 硬止损
        if pnl_pct <= -p.hard_stop_pct:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.HARD_STOP,
                close_ratio=1.0,
                pnl_estimate=pnl_pct,
                description=f"硬止损: {pnl_pct:.1f}% <= -{p.hard_stop_pct}%",
            )

        # 2. TP1 半仓止盈
        if not trade.tp1_triggered and pnl_pct >= p.tp1_pct:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.TP1,
                close_ratio=p.tp1_close_ratio,
                pnl_estimate=pnl_pct,
                description=f"TP1: +{pnl_pct:.1f}%",
            )

        # 3. TP2 全仓
        if pnl_pct >= p.tp2_pct:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.TP2,
                close_ratio=1.0,
                pnl_estimate=pnl_pct,
                description=f"TP2: +{pnl_pct:.1f}%",
            )

        # 4. 移动止损
        if trade.best_pnl_pct >= p.trail_activate_pct:
            trail_trigger = trade.best_pnl_pct * (1 - p.trail_retrace_ratio)
            if pnl_pct <= trail_trigger:
                return ExitSignal(
                    trade_id=trade.trade_id,
                    reason=ExitReason.TRAIL_STOP,
                    close_ratio=1.0,
                    pnl_estimate=pnl_pct,
                    description=f"移动止损: best={trade.best_pnl_pct:.1f}% now={pnl_pct:.1f}%",
                )

        # 5. 时间止损
        if trade.hold_hours >= p.max_hold_hours:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.TIME_STOP,
                close_ratio=1.0,
                pnl_estimate=pnl_pct,
                description=f"时间止损: {trade.hold_hours:.0f}h >= {p.max_hold_hours}h",
            )

        return None

    # ── 参数管理 ─────────────────────────────────────────────────

    def get_params(self) -> Dict[str, Any]:
        return self._params.to_dict()

    def set_params(self, params: Dict[str, Any]) -> None:
        for key, value in params.items():
            if hasattr(self._params, key):
                setattr(self._params, key, value)

    def get_param_space(self) -> Dict[str, Dict[str, Any]]:
        return self._params.get_optimization_space()

    # ── 生命周期回调 ─────────────────────────────────────────────

    def on_trade_opened(self, trade_id: str, signal: Signal) -> None:
        logger.info(f"🔮 开仓: {signal.symbol} | score={signal.score} | {signal.reason}")

    def on_trade_closed(self, trade_id: str, pnl: float, reason: ExitReason) -> None:
        emoji = "🚀" if pnl > 0 else "💥"
        logger.info(f"{emoji} 平仓: {trade_id} | PnL={pnl:+.2f}U | {reason.value}")
