"""
Funding Rate 套利策略测试
覆盖: scan, confirm, evaluate_exit, params
"""

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault('ccxt', MagicMock())

import pytest


class MockDataFeed:
    """Mock DataFeed for testing"""

    def __init__(self, funding_rates=None, tickers=None, orderbook=None):
        self._funding = funding_rates or {}
        self._tickers = tickers or {}
        self._orderbook = orderbook or {'bids': [[1.0, 1000]], 'asks': [[1.001, 1000]]}

    def get_ohlcv(self, symbol, timeframe, limit=50):
        return []

    def get_ticker(self, symbol):
        return self._tickers.get(symbol, {'last': 1.0, 'quoteVolume': 5000000, 'percentage': 15})

    def get_tickers(self):
        return self._tickers

    def get_funding_rate(self, symbol):
        return self._funding.get(symbol, 0.0)

    def get_oi_change(self, symbol):
        return 0.1

    def get_orderbook(self, symbol, depth=20):
        return self._orderbook


class TestFundingArbScan:
    """扫描阶段测试"""

    def test_scan_finds_extreme_positive_rate(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        from strategies.base import MarketSnapshot

        strategy = FundingArbStrategy()

        tickers = {
            'PEPE/USDT': {'last': 0.00001, 'quoteVolume': 5000000, 'percentage': 20},
            'BTC/USDT': {'last': 70000, 'quoteVolume': 500000000, 'percentage': 2},
        }
        feed = MockDataFeed(
            funding_rates={'PEPE/USDT': 0.15, 'BTC/USDT': 0.01},
            tickers=tickers,
        )
        market = MarketSnapshot(tickers=tickers)

        candidates = strategy.scan(feed, market)
        assert len(candidates) == 1
        assert candidates[0].symbol == 'PEPE/USDT'
        assert candidates[0].metadata['direction'] == 'SHORT'

    def test_scan_finds_extreme_negative_rate(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        from strategies.base import MarketSnapshot

        strategy = FundingArbStrategy()

        tickers = {
            'DOGE/USDT': {'last': 0.15, 'quoteVolume': 10000000, 'percentage': -5},
        }
        feed = MockDataFeed(
            funding_rates={'DOGE/USDT': -0.08},
            tickers=tickers,
        )
        market = MarketSnapshot(tickers=tickers)

        candidates = strategy.scan(feed, market)
        assert len(candidates) == 1
        assert candidates[0].metadata['direction'] == 'LONG'

    def test_scan_filters_low_volume(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        from strategies.base import MarketSnapshot

        strategy = FundingArbStrategy()
        tickers = {
            'LOW/USDT': {'last': 0.1, 'quoteVolume': 100000, 'percentage': 10},
        }
        feed = MockDataFeed(funding_rates={'LOW/USDT': 0.20}, tickers=tickers)
        market = MarketSnapshot(tickers=tickers)

        candidates = strategy.scan(feed, market)
        assert len(candidates) == 0

    def test_scan_ignores_normal_rate(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        from strategies.base import MarketSnapshot

        strategy = FundingArbStrategy()
        tickers = {
            'ETH/USDT': {'last': 3000, 'quoteVolume': 100000000, 'percentage': 3},
        }
        feed = MockDataFeed(funding_rates={'ETH/USDT': 0.02}, tickers=tickers)
        market = MarketSnapshot(tickers=tickers)

        candidates = strategy.scan(feed, market)
        assert len(candidates) == 0


class TestFundingArbConfirm:
    """确认阶段测试"""

    def test_confirm_with_persistent_rate(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        from strategies.base import Candidate

        strategy = FundingArbStrategy()
        feed = MockDataFeed(funding_rates={'PEPE/USDT': 0.12})

        candidate = Candidate(
            symbol='PEPE/USDT',
            price=0.00001,
            score=60,
            metadata={
                'funding_rate': 0.15,
                'direction': 'SHORT',
                'vol_24h': 5000000,
                'rate_type': 'positive_extreme',
            },
        )

        signal = strategy.confirm(candidate, feed)
        assert signal is not None
        assert signal.direction.value == 'SHORT'
        assert signal.score >= 40

    def test_confirm_rejects_when_rate_normalized(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        from strategies.base import Candidate

        strategy = FundingArbStrategy()
        feed = MockDataFeed(funding_rates={'PEPE/USDT': 0.02})  # Below threshold

        candidate = Candidate(
            symbol='PEPE/USDT',
            price=0.00001,
            score=60,
            metadata={
                'funding_rate': 0.15,
                'direction': 'SHORT',
                'vol_24h': 5000000,
                'rate_type': 'positive_extreme',
            },
        )

        signal = strategy.confirm(candidate, feed)
        assert signal is None


class TestFundingArbExit:
    """退出评估测试"""

    def test_hard_stop_triggers(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        from strategies.base import TradeContext

        strategy = FundingArbStrategy()
        feed = MockDataFeed(funding_rates={'PEPE/USDT': 0.10})

        # SHORT direction: price went UP (losing money)
        trade = TradeContext(
            trade_id='t1', symbol='PEPE/USDT', direction='SHORT',
            entry_price=100.0, current_price=103.0,  # +3% = losing for short
            stake=30, stake_remaining=30, leverage=5, shares=1.5,
            opened_at='2026-01-01T00:00:00Z', pnl_pct=-3.0,
            best_pnl_pct=0.5, hold_hours=2,
            tp1_triggered=False, tp1_locked_pnl=0,
            hard_stop_price=102.0, trail_stop_price=None,
            exchange='shadow', account_id='',
        )

        exit_signal = strategy.evaluate_exit(trade, feed)
        assert exit_signal is not None
        assert exit_signal.reason.value == 'hard_stop'

    def test_take_profit_triggers(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        from strategies.base import TradeContext

        strategy = FundingArbStrategy()
        feed = MockDataFeed(funding_rates={'PEPE/USDT': 0.10})

        # SHORT direction: price went DOWN (making money)
        trade = TradeContext(
            trade_id='t1', symbol='PEPE/USDT', direction='SHORT',
            entry_price=100.0, current_price=98.0,  # -2% = profit for short
            stake=30, stake_remaining=30, leverage=5, shares=1.5,
            opened_at='2026-01-01T00:00:00Z', pnl_pct=2.0,
            best_pnl_pct=2.0, hold_hours=10,
            tp1_triggered=False, tp1_locked_pnl=0,
            hard_stop_price=105.0, trail_stop_price=None,
            exchange='shadow', account_id='',
        )

        exit_signal = strategy.evaluate_exit(trade, feed)
        assert exit_signal is not None
        assert exit_signal.reason.value == 'tp2'

    def test_no_exit_when_in_profit_range(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        from strategies.base import TradeContext

        strategy = FundingArbStrategy()
        feed = MockDataFeed(funding_rates={'PEPE/USDT': 0.10})

        trade = TradeContext(
            trade_id='t1', symbol='PEPE/USDT', direction='SHORT',
            entry_price=100.0, current_price=99.5,  # -0.5% = small profit
            stake=30, stake_remaining=30, leverage=5, shares=1.5,
            opened_at='2026-01-01T00:00:00Z', pnl_pct=0.5,
            best_pnl_pct=0.5, hold_hours=4,
            tp1_triggered=False, tp1_locked_pnl=0,
            hard_stop_price=105.0, trail_stop_price=None,
            exchange='shadow', account_id='',
        )

        exit_signal = strategy.evaluate_exit(trade, feed)
        assert exit_signal is None


class TestFundingArbParams:
    """参数管理测试"""

    def test_get_params(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        strategy = FundingArbStrategy()
        params = strategy.get_params()
        assert 'positive_rate_threshold' in params
        assert 'hard_stop_pct' in params
        assert params['leverage'] == 5

    def test_set_params(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        strategy = FundingArbStrategy()
        strategy.set_params({'leverage': 8, 'hard_stop_pct': 3.0})
        params = strategy.get_params()
        assert params['leverage'] == 8
        assert params['hard_stop_pct'] == 3.0

    def test_param_space(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        strategy = FundingArbStrategy()
        space = strategy.get_param_space()
        assert 'positive_rate_threshold' in space
        assert space['positive_rate_threshold']['type'] == 'float'

    def test_strategy_metadata(self):
        from strategies.funding_arb.strategy import FundingArbStrategy
        strategy = FundingArbStrategy()
        assert strategy.name == 'funding_arb'
        assert strategy.version == '1.0.0'
        assert 'funding' in strategy.description.lower() or '费率' in strategy.description
