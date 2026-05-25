"""
Order Book 深度监控测试
覆盖: OrderBookSnapshot, DepthAnalysis, 冲击预估, 流动性评分
"""

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault('ccxt', MagicMock())
sys.modules.setdefault('websocket', MagicMock())

import pytest


class TestOrderBookSnapshot:
    """订单簿快照测试"""

    def test_mid_price(self):
        from execution.orderbook_monitor import OrderBookSnapshot, OrderBookLevel
        book = OrderBookSnapshot(
            symbol='PEPE/USDT',
            bids=[OrderBookLevel(price=0.00001000, quantity=1000000)],
            asks=[OrderBookLevel(price=0.00001010, quantity=1000000)],
        )
        assert abs(book.mid_price - 0.00001005) < 1e-10

    def test_spread_bps(self):
        from execution.orderbook_monitor import OrderBookSnapshot, OrderBookLevel
        book = OrderBookSnapshot(
            symbol='TEST/USDT',
            bids=[OrderBookLevel(price=100.0, quantity=10)],
            asks=[OrderBookLevel(price=100.1, quantity=10)],
        )
        # spread = 0.1, mid = 100.05, spread_bps = 0.1/100.05 * 10000 ≈ 9.99
        assert 9 < book.spread_bps < 11

    def test_depth_usdt(self):
        from execution.orderbook_monitor import OrderBookSnapshot, OrderBookLevel
        book = OrderBookSnapshot(
            symbol='TEST/USDT',
            bids=[
                OrderBookLevel(price=100.0, quantity=10),
                OrderBookLevel(price=99.0, quantity=20),
            ],
            asks=[
                OrderBookLevel(price=101.0, quantity=15),
            ],
        )
        # bid depth = 100*10 + 99*20 = 1000 + 1980 = 2980
        assert abs(book.bid_depth_usdt - 2980) < 1
        # ask depth = 101*15 = 1515
        assert abs(book.ask_depth_usdt - 1515) < 1

    def test_empty_book(self):
        from execution.orderbook_monitor import OrderBookSnapshot
        book = OrderBookSnapshot(symbol='EMPTY/USDT')
        assert book.mid_price == 0.0
        assert book.spread_bps == 0.0


class TestDepthAnalysis:
    """深度分析测试"""

    def test_analysis_defaults(self):
        from execution.orderbook_monitor import DepthAnalysis
        analysis = DepthAnalysis(symbol='TEST/USDT')
        assert analysis.depth_sufficient is True
        assert analysis.recommended_slices == 1
        assert analysis.recommendation == ''


class TestImpactEstimation:
    """冲击预估测试"""

    def test_sufficient_depth_low_slippage(self):
        from execution.orderbook_monitor import DepthMonitor, OrderBookLevel

        monitor = DepthMonitor()
        levels = [
            OrderBookLevel(price=100.0, quantity=100),   # 10,000 USDT
            OrderBookLevel(price=99.9, quantity=100),    # 9,990 USDT
            OrderBookLevel(price=99.8, quantity=100),    # 9,980 USDT
        ]

        slippage, fill_price, slices = monitor._estimate_impact(
            levels, notional_usdt=5000, mid_price=100.05
        )
        # Eating only first level: fill at 100.0, mid is 100.05
        assert slippage < 10  # Less than 10 bps
        assert slices == 1

    def test_insufficient_depth_high_slippage(self):
        from execution.orderbook_monitor import DepthMonitor, OrderBookLevel

        monitor = DepthMonitor()
        levels = [
            OrderBookLevel(price=100.0, quantity=1),     # 100 USDT
            OrderBookLevel(price=99.0, quantity=1),      # 99 USDT
        ]

        slippage, fill_price, slices = monitor._estimate_impact(
            levels, notional_usdt=5000, mid_price=100.05
        )
        # Very thin depth, should have high slippage
        assert slippage > 20
        assert slices >= 3

    def test_empty_levels(self):
        from execution.orderbook_monitor import DepthMonitor

        monitor = DepthMonitor()
        slippage, fill_price, slices = monitor._estimate_impact(
            [], notional_usdt=1000, mid_price=100
        )
        assert slippage == 0.0
        assert fill_price == 100


class TestLiquidityScore:
    """流动性评分测试"""

    def test_high_liquidity_score(self):
        from execution.orderbook_monitor import DepthMonitor, OrderBookSnapshot, OrderBookLevel

        monitor = DepthMonitor()
        book = OrderBookSnapshot(
            symbol='HIGH/USDT',
            bids=[OrderBookLevel(price=100 - i * 0.01, quantity=1000) for i in range(10)],
            asks=[OrderBookLevel(price=100 + i * 0.01, quantity=1000) for i in range(10)],
        )

        score = monitor._calculate_liquidity_score(book, notional_usdt=1000, spread_bps=2)
        assert score >= 70  # High liquidity

    def test_low_liquidity_score(self):
        from execution.orderbook_monitor import DepthMonitor, OrderBookSnapshot, OrderBookLevel

        monitor = DepthMonitor()
        book = OrderBookSnapshot(
            symbol='LOW/USDT',
            bids=[OrderBookLevel(price=100, quantity=0.1)],
            asks=[OrderBookLevel(price=105, quantity=0.1)],  # 5% spread
        )

        score = monitor._calculate_liquidity_score(book, notional_usdt=10000, spread_bps=500)
        assert score < 30  # Low liquidity


class TestDepthMonitorAnalyze:
    """完整分析流程测试"""

    def test_analyze_with_no_data_returns_proceed(self):
        from execution.orderbook_monitor import DepthMonitor

        monitor = DepthMonitor()
        # No subscribed data, no REST fallback mock
        with patch.object(monitor, '_fetch_rest_orderbook', return_value=None):
            analysis = monitor.analyze('UNKNOWN/USDT', side='sell', notional_usdt=1000)
            assert analysis.recommendation == 'proceed'
            assert analysis.depth_sufficient is True

    def test_imbalance_no_data(self):
        from execution.orderbook_monitor import DepthMonitor
        monitor = DepthMonitor()
        assert monitor.get_imbalance('NODATA/USDT') == 0.0
