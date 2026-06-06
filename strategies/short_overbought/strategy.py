"""
做空超买策略实现 — BaseStrategy 接口的完整实现。
从 altcoin_scanner.py / altcoin_tracker.py / signal_score.py 重构。
"""

from __future__ import annotations

import logging
from typing import List, Optional, Dict, Any

from strategies.base import (
    BaseStrategy, DataFeed, MarketSnapshot, Signal, ExitSignal,
    Candidate, TradeContext, SignalDirection, ExitReason,
)
from strategies.short_overbought.params import ShortOverboughtParams
from strategies.short_overbought.indicators import (
    calc_rsi_wilder, find_rsi_peak, detect_abandon_signal,
    detect_volume_divergence, calculate_signal_score,
)

logger = logging.getLogger("strategy.short_overbought")



class ShortOverboughtStrategy(BaseStrategy):
    """
    做空超买策略 v5.0
    扫描日线超买小币种，4h RSI 回落或弃盘点确认后做空。
    """

    def __init__(self, params: Optional[ShortOverboughtParams] = None):
        self._params = params or ShortOverboughtParams()

    @property
    def name(self) -> str:
        return 'short_overbought'

    @property
    def version(self) -> str:
        return '5.0.0'

    @property
    def description(self) -> str:
        return '小币种超买做空策略：日线RSI超买 + 4h回落/弃盘点确认'

    @property
    def direction(self) -> SignalDirection:
        return SignalDirection.SHORT


    # ── 核心方法：扫描 ────────────────────────────────────────────

    def scan(self, data_feed: DataFeed, market: MarketSnapshot) -> List[Candidate]:
        """扫描全市场，找日线 RSI 超买的候选币"""
        p = self._params
        candidates = []

        for symbol, ticker in market.tickers.items():
            if not symbol.endswith('/USDT'):
                continue

            price = ticker.get('last', 0) or 0
            vol24h = ticker.get('quoteVolume', 0) or 0
            pct24h = ticker.get('percentage', 0) or 0

            # 基础过滤
            if price <= 0 or price > p.price_max:
                continue
            if vol24h < p.vol_min:
                continue
            if pct24h < p.pct_24h_min:
                continue

            # 日线 RSI
            try:
                ohlcv = data_feed.get_ohlcv(symbol, '1d', limit=51)
                if len(ohlcv) >= 2:
                    ohlcv = ohlcv[:-1]  # 丢弃未收盘
                closes = [c[4] for c in ohlcv]
                rsi_1d = calc_rsi_wilder(closes, p.rsi_period)
            except Exception:
                continue

            if rsi_1d < p.daily_rsi_min:
                continue

            # 合约数据
            oi_change = data_feed.get_oi_change(symbol)
            funding = data_feed.get_funding_rate(symbol)

            # 费率过滤
            if funding > p.funding_max or funding < p.funding_min:
                continue

            # 妖币评分
            yao_score = 0
            if oi_change >= p.oi_change_min:
                yao_score += 1
            if funding >= p.funding_hot:
                yao_score += 1
            if pct24h >= 30:
                yao_score += 1

            candidates.append(Candidate(
                symbol=symbol,
                price=price,
                score=rsi_1d,
                metadata={
                    'vol24h': round(vol24h),
                    'pct24h': round(pct24h, 1),
                    'rsi_1d': rsi_1d,
                    'oi_change': round(oi_change * 100, 1),
                    'funding_rate': round(funding, 4),
                    'yao_score': yao_score,
                },
            ))

        logger.info(f"扫描完成：{len(candidates)} 个候选")
        return candidates


    # ── 核心方法：确认 ────────────────────────────────────────────

    def confirm(self, candidate: Candidate, data_feed: DataFeed) -> Optional[Signal]:
        """确认候选：4h RSI 回落或弃盘点触发"""
        p = self._params
        symbol = candidate.symbol
        meta = candidate.metadata

        # 获取 4h RSI
        try:
            ohlcv_4h = data_feed.get_ohlcv(symbol, '4h', limit=51)
            if len(ohlcv_4h) >= 2:
                ohlcv_4h = ohlcv_4h[:-1]
            closes_4h = [c[4] for c in ohlcv_4h]
            rsi_4h = calc_rsi_wilder(closes_4h, p.rsi_period)
        except Exception:
            return None

        # 早退：4h RSI 仍超买
        if rsi_4h >= p.h4_rsi_enter:
            return None

        rsi_4h_peak = find_rsi_peak(closes_4h, p.rsi_period, p.h4_rsi_peak_lookback)
        drop = rsi_4h_peak - rsi_4h
        trigger_4h = drop >= p.h4_rsi_drop

        # 弃盘检测
        try:
            ohlcv_1h = data_feed.get_ohlcv(symbol, '1h', limit=7)
            if len(ohlcv_1h) >= 2:
                ohlcv_1h = ohlcv_1h[:-1]
            abandon = detect_abandon_signal(
                ohlcv_1h, p.abandon_body_drop_pct, p.abandon_consecutive
            )
        except Exception:
            abandon = {"signal": False, "reason": "检测失败"}

        trigger_abandon = abandon.get("signal", False)

        if not (trigger_4h or trigger_abandon):
            return None

        # 量价背离
        try:
            ohlcv_div = data_feed.get_ohlcv(symbol, '1h', limit=25)
            if len(ohlcv_div) >= 2:
                ohlcv_div = ohlcv_div[:-1]
            vol_div = detect_volume_divergence(ohlcv_div)
        except Exception:
            vol_div = {"divergence": False, "score_bonus": 0}

        # BTC 过滤
        btc_pct = 0.0
        if p.btc_filter_enabled:
            try:
                btc_ticker = data_feed.get_ticker('BTC/USDT')
                btc_pct = btc_ticker.get('percentage', 0) or 0
                if btc_pct <= p.btc_crash_threshold:
                    return None
            except Exception:
                pass


        # 信号评分
        score_result = calculate_signal_score(
            rsi_1d=meta.get('rsi_1d', 75),
            rsi_4h=rsi_4h,
            rsi_4h_peak=rsi_4h_peak,
            pct_24h=meta.get('pct24h', 0),
            oi_change=meta.get('oi_change', 0),
            funding_rate=meta.get('funding_rate', 0),
            yao_score=meta.get('yao_score', 0),
            trigger_type='abandon' if trigger_abandon else '4h_rsi',
            abandon_oi_declining=abandon.get("oi_declining", False),
            btc_24h_pct=btc_pct,
            cross_validate_bonus=0,
            vol_divergence_bonus=vol_div.get("score_bonus", 0),
            btc_pump_threshold=p.btc_pump_threshold,
        )

        score = score_result['score']
        grade = score_result['grade']

        if grade == 'SKIP':
            return None

        # 计算仓位
        if score >= p.score_full_threshold:
            stake = p.default_stake
        else:
            stake = round(p.default_stake * 0.5)

        trigger_type = 'abandon' if trigger_abandon else '4h_rsi'
        if trigger_abandon:
            reason = f"弃盘点: {abandon.get('reason', '')}"
        else:
            reason = f"4h RSI从{rsi_4h_peak:.0f}回落至{rsi_4h:.0f}"

        return Signal(
            symbol=symbol,
            direction=SignalDirection.SHORT,
            score=score,
            stake=stake,
            leverage=p.leverage,
            hard_stop_pct=p.hard_stop_pct,
            tp1_pct=p.tp1_pct,
            tp2_pct=p.tp2_pct,
            trail_retrace_ratio=p.trail_retrace_ratio,
            max_hold_hours=p.max_hold_hours,
            strategy_name=self.name,
            strategy_version=self.version,
            trigger_type=trigger_type,
            reason=reason,
            metadata={
                'entry_ref_price': candidate.price,
                'rsi_4h': rsi_4h,
                'rsi_4h_peak': rsi_4h_peak,
                'score_details': score_result['details'],
                'vol_divergence': vol_div.get("divergence", False),
                'grade': grade,
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

        # 盈亏百分比（做空：价格下跌 = 盈利）
        pnl_pct = (entry - current) / entry * 100

        # 1. 硬止损
        hard_stop_price = entry * (1 + p.hard_stop_pct / 100)
        if current >= hard_stop_price:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.HARD_STOP,
                close_ratio=1.0,
                pnl_estimate=pnl_pct,
                description=f"硬止损触发：价格 {current:.6f} >= {hard_stop_price:.6f}",
            )

        # 2. TP1（半仓止盈）
        if not trade.tp1_triggered:
            tp1_price = entry * (1 - p.tp1_pct / 100)
            if current <= tp1_price:
                return ExitSignal(
                    trade_id=trade.trade_id,
                    reason=ExitReason.TP1,
                    close_ratio=p.tp1_close_ratio,
                    pnl_estimate=pnl_pct,
                    description=f"TP1触发：价格跌至 {current:.6f}（-{p.tp1_pct}%）",
                )

        # 3. TP2（全部止盈）
        tp2_price = entry * (1 - p.tp2_pct / 100)
        if current <= tp2_price:
            return ExitSignal(
                trade_id=trade.trade_id,
                reason=ExitReason.TP2,
                close_ratio=1.0,
                pnl_estimate=pnl_pct,
                description=f"TP2触发：价格跌至 {current:.6f}（-{p.tp2_pct}%）",
            )

        # 4. 移动止损
        if trade.best_pnl_pct >= p.trail_activate_pct:
            trail_trigger_pnl = trade.best_pnl_pct * (1 - p.trail_retrace_ratio)
            if pnl_pct <= trail_trigger_pnl:
                return ExitSignal(
                    trade_id=trade.trade_id,
                    reason=ExitReason.TRAIL_STOP,
                    close_ratio=1.0,
                    pnl_estimate=pnl_pct,
                    description=(
                        f"移动止损：最高盈利{trade.best_pnl_pct:.1f}%回撤到{pnl_pct:.1f}%"
                    ),
                )

        # 5. 时间止损
        if trade.hold_hours >= p.max_hold_hours:
            # 超时但盈利超过阈值则不平
            if pnl_pct < p.trail_activate_pct:
                return ExitSignal(
                    trade_id=trade.trade_id,
                    reason=ExitReason.TIME_STOP,
                    close_ratio=1.0,
                    pnl_estimate=pnl_pct,
                    description=f"时间止损：持仓{trade.hold_hours:.1f}h超过{p.max_hold_hours}h",
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
        logger.info(
            f"✅ 开仓: {signal.symbol} | score={signal.score} | "
            f"stake={signal.stake}U | {signal.reason}"
        )

    def on_trade_closed(self, trade_id: str, pnl: float, reason: ExitReason) -> None:
        emoji = "📈" if pnl > 0 else "📉"
        logger.info(f"{emoji} 平仓: {trade_id} | PnL={pnl:+.2f}U | {reason.value}")
