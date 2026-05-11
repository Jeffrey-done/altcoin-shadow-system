"""
回测引擎测试
测试 backtest 模块的 RSI 序列、信号检测、交易模拟、统计计算
"""

import pytest

from backtest import (
    calc_rsi_series, detect_entry_signals, simulate_trade,
    calculate_stats, BacktestParams, BacktestTrade,
)


def _make_params(**kwargs):
    """构造测试用回测参数"""
    defaults = dict(
        rsi_period=14,
        daily_rsi_min=78,
        h4_rsi_enter=70,
        h4_rsi_drop=10,
        tp1_pct=5.0,
        tp2_pct=10.0,
        tp1_close_ratio=0.5,
        hard_stop_pct=3.0,
        trail_activate_pct=3.0,
        trail_drawdown_pct=0.10,
        max_hold_bars=24,
        leverage=10,
        stake=100.0,
        slippage_pct=0.1,
        fee_pct=0.04,
    )
    defaults.update(kwargs)
    return BacktestParams(**defaults)


def _make_trending_klines(n=60, start_price=1.0, trend='up'):
    """生成趋势性K线数据"""
    klines = []
    price = start_price
    step = 0.02 if trend == 'up' else -0.02
    for i in range(n):
        open_p = price
        close_p = price + step
        high_p = max(open_p, close_p) + 0.005
        low_p = min(open_p, close_p) - 0.005
        klines.append({
            "time": f"2025-01-01T{i:02d}:00:00+00:00" if i < 24 else f"2025-01-02T{i-24:02d}:00:00+00:00",
            "open": round(open_p, 6),
            "high": round(high_p, 6),
            "low": round(low_p, 6),
            "close": round(close_p, 6),
            "volume": 1000000,
        })
        price = close_p
    return klines


def _make_signal_klines():
    """
    生成含有明确信号的K线：
    前30根上涨（RSI高），后30根下跌（RSI回落）。
    共60根。
    """
    klines = []
    price = 0.50
    # 前30根强劲上涨 -> RSI冲高
    for i in range(30):
        open_p = price
        close_p = price + 0.015
        high_p = close_p + 0.003
        low_p = open_p - 0.001
        klines.append({
            "time": f"2025-01-01T{i:02d}:00:00+00:00" if i < 24 else f"2025-01-02T{i-24:02d}:00:00+00:00",
            "open": round(open_p, 6),
            "high": round(high_p, 6),
            "low": round(low_p, 6),
            "close": round(close_p, 6),
            "volume": 1500000,
        })
        price = close_p
    # 后30根快速下跌 -> RSI 回落
    for i in range(30):
        open_p = price
        close_p = price - 0.012
        high_p = open_p + 0.001
        low_p = close_p - 0.003
        klines.append({
            "time": f"2025-01-03T{i:02d}:00:00+00:00" if i < 24 else f"2025-01-04T{i-24:02d}:00:00+00:00",
            "open": round(open_p, 6),
            "high": round(high_p, 6),
            "low": round(low_p, 6),
            "close": round(close_p, 6),
            "volume": 2000000,
        })
        price = close_p
    return klines


class TestCalcRsiSeries:
    """calc_rsi_series 测试"""

    def test_calc_rsi_series_length(self):
        """输出长度等于输入长度"""
        prices = [float(i) for i in range(50, 100)]
        result = calc_rsi_series(prices, period=14)
        assert len(result) == len(prices)

    def test_calc_rsi_series_values_range(self):
        """RSI 值应在 0~100 范围"""
        prices = [float(i) for i in range(50, 100)]
        result = calc_rsi_series(prices, period=14)
        for v in result:
            assert 0 <= v <= 100


class TestDetectEntrySignals:
    """detect_entry_signals 信号检测测试"""

    def test_detect_entry_signals_finds_signals(self):
        """含明确 RSI 冲高后回落的K线 -> 应检测到信号"""
        klines = _make_signal_klines()
        params = _make_params(daily_rsi_min=78, h4_rsi_enter=70, h4_rsi_drop=10)
        signals = detect_entry_signals(klines, params)
        assert len(signals) >= 1, "应检测到至少1个信号"

    def test_detect_entry_signals_no_signal(self):
        """平稳波动K线（RSI~50）-> 无信号"""
        klines = []
        price = 1.0
        for i in range(60):
            # 小幅波动（涨跌交替）
            delta = 0.001 if i % 2 == 0 else -0.001
            open_p = price
            close_p = price + delta
            klines.append({
                "time": f"2025-01-01T{i:02d}:00:00+00:00" if i < 24 else f"2025-01-02T{i-24:02d}:00:00+00:00",
                "open": round(open_p, 6),
                "high": round(open_p + 0.002, 6),
                "low": round(open_p - 0.002, 6),
                "close": round(close_p, 6),
                "volume": 1000000,
            })
            price = close_p
        params = _make_params(daily_rsi_min=78, h4_rsi_enter=70, h4_rsi_drop=10)
        signals = detect_entry_signals(klines, params)
        assert len(signals) == 0, "平稳行情不应产生信号"

    def test_detect_signals_not_on_last_bar(self):
        """信号不应出现在最后一根K线（需要下一根bar入场）"""
        klines = _make_signal_klines()
        params = _make_params()
        signals = detect_entry_signals(klines, params)
        for sig in signals:
            assert sig + 1 < len(klines), "信号不应在最后一根K线"


class TestSimulateTrade:
    """simulate_trade 交易模拟测试"""

    def test_simulate_trade_uses_next_bar_open(self):
        """入场价应为 entry_idx+1 的开盘价（含滑点调整）"""
        klines = _make_signal_klines()
        params = _make_params(slippage_pct=0.0)  # 无滑点时应精确等于 next bar open
        entry_idx = 35  # 确保 entry_idx+1 存在
        trade = simulate_trade(klines, entry_idx, params)
        expected_entry = klines[entry_idx + 1]['open']
        assert abs(trade.entry_price - expected_entry) < 0.0001

    def test_simulate_trade_hard_stop(self):
        """构造价格上涨K线 -> 硬止损触发"""
        # 创建 entry_idx+1 之后价格持续上涨的K线
        klines = []
        price = 1.0
        for i in range(50):
            open_p = price
            if i < 20:
                close_p = price + 0.001  # 小幅上涨
            else:
                close_p = price + 0.05  # 大幅上涨
            high_p = close_p + 0.01
            low_p = open_p - 0.001
            klines.append({
                "time": f"2025-01-01T{i:02d}:00:00+00:00",
                "open": round(open_p, 6),
                "high": round(high_p, 6),
                "low": round(low_p, 6),
                "close": round(close_p, 6),
                "volume": 1000000,
            })
            price = close_p

        params = _make_params(hard_stop_pct=3.0, slippage_pct=0.0)
        # entry_idx=18, entry_price = klines[19]['open'] ~ 1.019
        # hard_stop = entry_price * 1.03
        # 之后 K 线大幅上涨，高点会超过 hard_stop
        trade = simulate_trade(klines, 18, params)
        assert trade.exit_reason == 'hard_stop'
        assert trade.pnl_usd < 0  # 做空硬止损应亏损

    def test_simulate_trade_tp1_tp2(self):
        """构造价格持续下跌K线 -> TP1+TP2 触发"""
        klines = []
        price = 1.0
        for i in range(50):
            open_p = price
            close_p = price - 0.015  # 每根跌1.5%
            high_p = open_p + 0.001
            low_p = close_p - 0.005
            klines.append({
                "time": f"2025-01-01T{i:02d}:00:00+00:00",
                "open": round(open_p, 6),
                "high": round(high_p, 6),
                "low": round(low_p, 6),
                "close": round(close_p, 6),
                "volume": 1000000,
            })
            price = close_p

        params = _make_params(tp1_pct=5.0, tp2_pct=10.0, hard_stop_pct=3.0,
                              slippage_pct=0.0, max_hold_bars=40)
        trade = simulate_trade(klines, 0, params)
        assert trade.tp1_hit is True
        assert trade.exit_reason == 'tp2'
        assert trade.pnl_usd > 0

    def test_simulate_trade_time_stop(self):
        """价格平稳持续 max_hold_bars 根 -> 时间止损"""
        klines = []
        price = 1.0
        for i in range(50):
            # 几乎不动
            open_p = price
            close_p = price + 0.0001
            high_p = price + 0.0005
            low_p = price - 0.0005
            klines.append({
                "time": f"2025-01-01T{i:02d}:00:00+00:00",
                "open": round(open_p, 6),
                "high": round(high_p, 6),
                "low": round(low_p, 6),
                "close": round(close_p, 6),
                "volume": 1000000,
            })
            price = close_p

        params = _make_params(max_hold_bars=10, hard_stop_pct=50.0,
                              tp1_pct=50.0, tp2_pct=80.0, slippage_pct=0.0)
        trade = simulate_trade(klines, 0, params)
        assert trade.exit_reason == 'time_stop'
        assert trade.hold_bars == 10

    def test_simulate_trade_slippage_fees(self):
        """滑点和手续费应减少盈利"""
        klines = []
        price = 1.0
        for i in range(50):
            open_p = price
            close_p = price - 0.015
            high_p = open_p + 0.001
            low_p = close_p - 0.005
            klines.append({
                "time": f"2025-01-01T{i:02d}:00:00+00:00",
                "open": round(open_p, 6),
                "high": round(high_p, 6),
                "low": round(low_p, 6),
                "close": round(close_p, 6),
                "volume": 1000000,
            })
            price = close_p

        # 无滑点无手续费
        params_no_fees = _make_params(slippage_pct=0.0, fee_pct=0.0,
                                       tp1_pct=5.0, tp2_pct=10.0, max_hold_bars=40)
        trade_no_fees = simulate_trade(klines, 0, params_no_fees)

        # 有滑点有手续费
        params_with_fees = _make_params(slippage_pct=0.1, fee_pct=0.04,
                                         tp1_pct=5.0, tp2_pct=10.0, max_hold_bars=40)
        trade_with_fees = simulate_trade(klines, 0, params_with_fees)

        # 有费用版本的盈利应更低
        assert trade_with_fees.pnl_usd < trade_no_fees.pnl_usd


class TestCalculateStats:
    """calculate_stats 统计计算测试"""

    def test_calculate_stats_correctness(self):
        """已知交易列表 -> 验证统计指标"""
        trades = [
            BacktestTrade(symbol='TEST', entry_price=1.0, entry_time='2025-01-01',
                          exit_price=0.9, exit_time='2025-01-02', pnl_pct=10, pnl_usd=10.0,
                          exit_reason='tp2', hold_bars=5, tp1_hit=True),
            BacktestTrade(symbol='TEST', entry_price=1.0, entry_time='2025-01-03',
                          exit_price=0.9, exit_time='2025-01-04', pnl_pct=10, pnl_usd=10.0,
                          exit_reason='tp2', hold_bars=6, tp1_hit=True),
            BacktestTrade(symbol='TEST', entry_price=1.0, entry_time='2025-01-05',
                          exit_price=1.03, exit_time='2025-01-06', pnl_pct=-3, pnl_usd=-5.0,
                          exit_reason='hard_stop', hold_bars=2, tp1_hit=False),
            BacktestTrade(symbol='TEST', entry_price=1.0, entry_time='2025-01-07',
                          exit_price=1.03, exit_time='2025-01-08', pnl_pct=-3, pnl_usd=-5.0,
                          exit_reason='hard_stop', hold_bars=3, tp1_hit=False),
        ]
        params = _make_params()
        result = calculate_stats(trades, params)

        # 4 笔交易
        assert result.total_trades == 4
        # 2 胜 2 负
        assert result.wins == 2
        assert result.losses == 2
        # 胜率 50%
        assert result.win_rate == 50.0
        # 平均盈利 10, 平均亏损 -5
        assert result.avg_win == 10.0
        assert result.avg_loss == -5.0
        # 盈亏比 10/5 = 2.0
        assert result.profit_loss_ratio == 2.0
        # 总盈亏 10+10-5-5 = 10
        assert result.total_pnl == 10.0

    def test_calculate_stats_empty(self):
        """空交易列表 -> 安全返回"""
        params = _make_params()
        result = calculate_stats([], params)
        assert result.total_trades == 0
        assert result.win_rate == 0
        assert result.total_pnl == 0

    def test_calculate_stats_max_drawdown(self):
        """验证最大回撤计算"""
        trades = [
            BacktestTrade(symbol='TEST', entry_price=1.0, entry_time='t1',
                          exit_price=0.0, exit_time='t2', pnl_pct=5, pnl_usd=20.0,
                          exit_reason='tp1', hold_bars=5, tp1_hit=True),
            BacktestTrade(symbol='TEST', entry_price=1.0, entry_time='t3',
                          exit_price=0.0, exit_time='t4', pnl_pct=-3, pnl_usd=-15.0,
                          exit_reason='hard_stop', hold_bars=2, tp1_hit=False),
            BacktestTrade(symbol='TEST', entry_price=1.0, entry_time='t5',
                          exit_price=0.0, exit_time='t6', pnl_pct=-3, pnl_usd=-15.0,
                          exit_reason='hard_stop', hold_bars=2, tp1_hit=False),
        ]
        params = _make_params()
        result = calculate_stats(trades, params)
        # 从峰值 120 跌到 90, dd = 30/120 = 25%
        assert result.max_drawdown > 0
        assert result.max_consecutive_losses == 2
