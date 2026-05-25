"""
智能订单引擎测试
覆盖: 算法选择、TWAP 拆分、Adaptive 深度检查、结果汇总
"""

import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault('ccxt', MagicMock())

import pytest


class TestSmartOrderConfig:
    """配置测试"""

    def test_default_config(self):
        from execution.smart_order import SmartOrderConfig
        cfg = SmartOrderConfig()
        assert cfg.auto_threshold_usdt == 1000.0
        assert cfg.twap_slices == 3
        assert cfg.adaptive_depth_ratio == 0.3

    def test_custom_config(self):
        from execution.smart_order import SmartOrderConfig
        cfg = SmartOrderConfig(twap_slices=5, auto_threshold_usdt=500)
        assert cfg.twap_slices == 5
        assert cfg.auto_threshold_usdt == 500


class TestSmartOrderResult:
    """结果对象测试"""

    def test_fill_rate(self):
        from execution.smart_order import SmartOrderResult
        result = SmartOrderResult(target_amount=1000, filled_amount=750)
        assert result.fill_rate == 0.75

    def test_fill_rate_zero_target(self):
        from execution.smart_order import SmartOrderResult
        result = SmartOrderResult(target_amount=0, filled_amount=0)
        assert result.fill_rate == 0.0

    def test_is_complete(self):
        from execution.smart_order import SmartOrderResult, SmartOrderStatus
        result = SmartOrderResult(status=SmartOrderStatus.FILLED)
        assert result.is_complete is True

        result2 = SmartOrderResult(status=SmartOrderStatus.EXECUTING)
        assert result2.is_complete is False

    def test_improvement_vs_market(self):
        from execution.smart_order import SmartOrderResult
        # Sell at higher price than mid = positive improvement
        result = SmartOrderResult(
            side='sell',
            pre_trade_mid_price=100.0,
            avg_price=100.05,
            filled_amount=100,
        )
        assert result.improvement_vs_market_bps > 0


class TestAlgoSelection:
    """算法选择逻辑测试"""

    def test_small_notional_uses_market(self):
        from execution.smart_order import SmartOrderEngine, SmartOrderConfig, AlgoType
        cfg = SmartOrderConfig(auto_threshold_usdt=1000)
        engine = SmartOrderEngine(cfg)
        algo = engine._select_algo('PEPE/USDT', 500, 'binance')
        assert algo == AlgoType.MARKET

    def test_medium_notional_uses_twap(self):
        from execution.smart_order import SmartOrderEngine, SmartOrderConfig, AlgoType

        cfg = SmartOrderConfig(auto_threshold_usdt=1000)
        engine = SmartOrderEngine(cfg)

        # Mock orderbook to return None (fallback to TWAP)
        with patch.object(engine, '_get_orderbook', return_value=None):
            algo = engine._select_algo('PEPE/USDT', 5000, 'binance')
            assert algo == AlgoType.TWAP


class TestSliceResult:
    """单片结果测试"""

    def test_slice_result_defaults(self):
        from execution.smart_order import SliceResult
        sr = SliceResult(slice_index=0, amount=100)
        assert sr.success is False
        assert sr.filled_amount == 0
        assert sr.slippage_bps == 0


class TestResultFinalization:
    """结果汇总测试"""

    def test_finalize_weighted_average(self):
        from execution.smart_order import SmartOrderEngine, SmartOrderResult, SliceResult

        engine = SmartOrderEngine()
        result = SmartOrderResult()

        # Two successful slices at different prices
        result.slices = [
            SliceResult(slice_index=0, amount=100, filled_amount=100, avg_price=1.0, success=True),
            SliceResult(slice_index=1, amount=100, filled_amount=100, avg_price=1.02, success=True),
        ]

        engine._finalize_result(result)
        assert result.filled_amount == 200
        assert abs(result.avg_price - 1.01) < 0.001  # Weighted average

    def test_finalize_with_failed_slice(self):
        from execution.smart_order import SmartOrderEngine, SmartOrderResult, SliceResult

        engine = SmartOrderEngine()
        result = SmartOrderResult()

        result.slices = [
            SliceResult(slice_index=0, amount=100, filled_amount=100, avg_price=1.0, success=True),
            SliceResult(slice_index=1, amount=100, filled_amount=0, avg_price=0, success=False, error='timeout'),
        ]

        engine._finalize_result(result)
        assert result.filled_amount == 100
        assert result.avg_price == 1.0

    def test_finalize_all_failed(self):
        from execution.smart_order import SmartOrderEngine, SmartOrderResult, SliceResult

        engine = SmartOrderEngine()
        result = SmartOrderResult()
        result.slices = [
            SliceResult(slice_index=0, amount=100, success=False, error='err'),
        ]

        engine._finalize_result(result)
        assert result.filled_amount == 0
        assert result.avg_price == 0
