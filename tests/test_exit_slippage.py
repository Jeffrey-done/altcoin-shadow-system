"""
平仓滑点进 Trade 模型 — 回归测试。

覆盖:
  - evaluate_trade 在 hard_stop / trail_stop / tp2 / time_stop 分支里
    把 exit_ref_price 写入 trade
  - TP1 分支把 tp1_exit_ref_price 写入 trade
  - _perform_exchange_close 的后台回填路径用交易所返回的 average 填充
    exit_slippage_pct / tp1_slippage_pct
  - weekly_report.calculate_weekly_stats 把三段滑点(入场 / TP1 / 最终平仓)
    分别统计到对应桶
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import common
from common import utcnow_iso, load_json, atomic_write_json
from models import Trade
from altcoin_tracker import evaluate_trade


def _make_trade(**overrides):
    base = dict(
        id='TEST-001',
        symbol='TEST/USDT',
        direction='SHORT',
        entry_price=100.0,
        stake=100,
        leverage=10,
        notional=1000,
        shares=10.0,
        opened_at=utcnow_iso(),
        status='open',
        take_profit_1=95.0,
        take_profit_2=90.0,
        hard_stop_price=105.0,
        max_hold_days=1,
        stake_remaining=100,
    )
    base.update(overrides)
    return Trade(**base)


# ══════════════════════════════════════════════════════════════════
#  evaluate_trade 写入 exit_ref_price
# ══════════════════════════════════════════════════════════════════

class TestExitRefPriceRecording:
    def test_hard_stop_records_ref_price(self, mock_config):
        trade = _make_trade()
        result = evaluate_trade(trade, current_price=106.0)
        assert result.closed is True
        assert trade.close_type == 'hard_stop'
        # evaluate 时以 current_price 作为 ref
        assert abs(trade.exit_ref_price - 106.0) < 0.001
        # 尚未 backfill 过,slippage 还是 0
        assert trade.exit_slippage_pct == 0.0

    def test_tp2_records_ref_price(self, mock_config):
        trade = _make_trade(take_profit_1=95.0, take_profit_2=90.0, hard_stop_price=110.0)
        evaluate_trade(trade, current_price=94.0)   # TP1
        result = evaluate_trade(trade, current_price=89.0)  # TP2
        assert result.closed is True
        assert trade.close_type == 'tp2'
        assert abs(trade.exit_ref_price - 89.0) < 0.001

    def test_time_stop_records_ref_price(self, mock_config):
        past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        trade = _make_trade(
            opened_at=past, max_hold_days=1,
            hard_stop_price=110.0, take_profit_1=90.0, take_profit_2=85.0,
        )
        result = evaluate_trade(trade, current_price=99.5)
        assert result.closed is True
        assert trade.close_type == 'time_stop'
        assert abs(trade.exit_ref_price - 99.5) < 0.001

    def test_trail_stop_records_ref_price(self, mock_config):
        trade = _make_trade(
            entry_price=100.0, take_profit_1=90.0, take_profit_2=85.0,
            hard_stop_price=110.0,
        )
        # 先到 95,激活移动止损,trail_stop_price 被设为 97
        evaluate_trade(trade, current_price=95.0)
        # 反弹到 98,越过 trail_stop=97 触发
        result = evaluate_trade(trade, current_price=98.0)
        assert result.closed is True
        assert trade.close_type == 'trail_stop'
        assert abs(trade.exit_ref_price - 98.0) < 0.001

    def test_tp1_records_tp1_ref_price(self, mock_config):
        trade = _make_trade(take_profit_1=95.0, take_profit_2=85.0, hard_stop_price=110.0)
        evaluate_trade(trade, current_price=94.0)
        assert trade.tp1_triggered is True
        # TP1 不关闭交易,但 ref 价要记录用于后续滑点计算
        assert abs(trade.tp1_exit_ref_price - 94.0) < 0.001
        # 并且 exit_ref_price 仍是 0(只在最终平仓时才填)
        assert trade.exit_ref_price == 0.0


# ══════════════════════════════════════════════════════════════════
#  _perform_exchange_close 的 backfill 路径
# ══════════════════════════════════════════════════════════════════

class TestBackfillSlippage:
    """
    _perform_exchange_close 的 _backfill_order_id 是后台线程;测试通过
    直接构造 trades.json,然后调 _perform_exchange_close(mock 掉 execute_close)
    并等后台线程完成,最后读文件验证字段被正确填充。
    """

    def _seed_trade(self, path, **overrides):
        t = _make_trade(**overrides).to_dict()
        atomic_write_json(path, [t])

    def _wait_backfill(self):
        """后台线程是 daemon thread,给它一点时间完成 I/O"""
        import time
        for _ in range(50):
            time.sleep(0.02)

    def test_hard_stop_backfill_populates_exit_slippage(self, tmp_path, monkeypatch, mock_config):
        """硬止损: exit_ref_price=106, fill=106.8 → 滑点 = 0.8/106*100 ≈ 0.7547%"""
        import altcoin_tracker as at

        trades_path = str(tmp_path / 'trades.json')
        monkeypatch.setattr(at, 'TRADES_FILE', trades_path)
        monkeypatch.setattr(common, 'TRADES_FILE', trades_path)

        self._seed_trade(
            trades_path,
            exchange='binance', status='closed',
            close_type='hard_stop',
            exit_ref_price=106.0, exit_slippage_pct=0.0,
        )

        # Mock live_executor.execute_close 返回成功 + 实际成交价 106.8
        fake_result = {
            'success': True, 'order_id': 'ORD-1',
            'price': 106.8, 'amount': 10.0, 'error': '',
        }
        with patch('live_executor.execute_close', return_value=fake_result), \
             patch('live_executor.make_client_order_id', return_value='coid-1'):
            trade = Trade.from_dict(load_json(trades_path)[0])
            at._perform_exchange_close(trade, 'full_close', 10.0)

        self._wait_backfill()

        # 读回 trades.json 验证 exit_slippage_pct 被正确填充
        data = load_json(trades_path)
        t = data[0]
        expected = abs(106.8 - 106.0) / 106.0 * 100
        assert abs(t['exit_slippage_pct'] - round(expected, 4)) < 0.001
        assert t['close_order_id'] == 'ORD-1'

    def test_tp1_backfill_populates_tp1_slippage(self, tmp_path, monkeypatch, mock_config):
        """TP1 半仓平仓: tp1_exit_ref_price=94, fill=94.3 → 滑点 ≈ 0.319%"""
        import altcoin_tracker as at

        trades_path = str(tmp_path / 'trades.json')
        monkeypatch.setattr(at, 'TRADES_FILE', trades_path)
        monkeypatch.setattr(common, 'TRADES_FILE', trades_path)

        self._seed_trade(
            trades_path,
            exchange='binance',
            tp1_triggered=True,
            tp1_exit_ref_price=94.0,
            tp1_closed_shares=5.0,
            tp1_exit_price=94.0,
            tp1_slippage_pct=0.0,
        )

        fake_result = {
            'success': True, 'order_id': 'ORD-TP1',
            'price': 94.3, 'amount': 4.95, 'error': '',
        }
        with patch('live_executor.execute_close', return_value=fake_result), \
             patch('live_executor.make_client_order_id', return_value='coid-tp1'):
            trade = Trade.from_dict(load_json(trades_path)[0])
            at._perform_exchange_close(trade, 'tp1_partial', 5.0)

        self._wait_backfill()

        data = load_json(trades_path)
        t = data[0]
        expected = abs(94.3 - 94.0) / 94.0 * 100
        assert abs(t['tp1_slippage_pct'] - round(expected, 4)) < 0.001
        # H7 回填也应生效
        assert abs(t['tp1_closed_shares'] - 4.95) < 0.001
        assert abs(t['tp1_exit_price'] - 94.3) < 0.001

    def test_backfill_skips_when_no_ref_price(self, tmp_path, monkeypatch, mock_config):
        """老数据没有 exit_ref_price → 不应算出离谱的滑点"""
        import altcoin_tracker as at

        trades_path = str(tmp_path / 'trades.json')
        monkeypatch.setattr(at, 'TRADES_FILE', trades_path)
        monkeypatch.setattr(common, 'TRADES_FILE', trades_path)

        self._seed_trade(
            trades_path,
            exchange='binance',
            exit_ref_price=0.0,  # 模拟老数据
        )

        fake_result = {
            'success': True, 'order_id': 'X',
            'price': 106.8, 'amount': 10.0, 'error': '',
        }
        with patch('live_executor.execute_close', return_value=fake_result), \
             patch('live_executor.make_client_order_id', return_value='c'):
            trade = Trade.from_dict(load_json(trades_path)[0])
            at._perform_exchange_close(trade, 'full_close', 10.0)

        self._wait_backfill()

        t = load_json(trades_path)[0]
        # 没 ref 价就不回填 slippage(保持 0,不乱算)
        assert t.get('exit_slippage_pct', 0.0) == 0.0


# ══════════════════════════════════════════════════════════════════
#  weekly_report 滑点聚合
# ══════════════════════════════════════════════════════════════════

class TestWeeklyReportSlippageSegments:
    def test_three_segments_aggregate_separately(self, mock_config):
        """一笔交易同时带入场 / TP1 / 平仓滑点 → 三段都被汇总"""
        from weekly_report import calculate_weekly_stats

        # 一笔做空,notional=1000,TP1 后剩 50% 仓位平仓
        # 入场滑点 0.1% → 1U
        # TP1 滑点 0.2%, 平仓 notional = 500 → 1U
        # 最终平仓滑点 0.4%, 平仓 notional = 500 → 2U
        t = _make_trade(
            entry_price=100.0, notional=1000, stake=100, leverage=10,
            closed_at=utcnow_iso(),
            status='closed',
            close_type='tp2',
            tp1_locked_pnl=30, pnl=-20,
            exchange='binance',
            slippage_pct=0.1,
            tp1_triggered=True,
            tp1_slippage_pct=0.2,
            exit_slippage_pct=0.4,
        )
        # closed_at 得在当前周范围内
        stats = calculate_weekly_stats([t])

        # 三段的金额
        assert abs(stats['slippage_entry_cost'] - 1.0) < 0.01
        assert abs(stats['slippage_tp1_cost'] - 1.0) < 0.01
        assert abs(stats['slippage_exit_cost'] - 2.0) < 0.01
        # 合计 = 入场 1 + TP1 1 + 平仓 2 = 4
        assert abs(stats['slippage_total_cost'] - 4.0) < 0.01
        # 各段 count 分别记
        assert stats['slippage_entry_count'] == 1
        assert stats['slippage_tp1_count'] == 1
        assert stats['slippage_exit_count'] == 1
        # 不同段对同一笔 trade 只数一次(按 symbol 去重)
        assert stats['slippage_trades_count'] == 1

    def test_no_slippage_data_keeps_segments_zero(self, mock_config):
        """老数据(只有入场滑点)→ 平仓段应为 0,不报错"""
        from weekly_report import calculate_weekly_stats

        t = _make_trade(
            notional=1000,
            closed_at=utcnow_iso(),
            status='closed',
            close_type='hard_stop',
            tp1_locked_pnl=0, pnl=-50,
            exchange='binance',
            slippage_pct=0.1,  # 只有入场滑点
            tp1_slippage_pct=0.0,
            exit_slippage_pct=0.0,
        )
        stats = calculate_weekly_stats([t])
        assert abs(stats['slippage_entry_cost'] - 1.0) < 0.01
        assert stats['slippage_tp1_cost'] == 0.0
        assert stats['slippage_exit_cost'] == 0.0
        assert stats['slippage_entry_count'] == 1
        assert stats['slippage_exit_count'] == 0

    def test_exit_notional_honors_tp1_triggered(self, mock_config):
        """TP1 触发的交易,平仓段的 notional 是 50%;未触发的是 100%"""
        from weekly_report import calculate_weekly_stats

        # TP1 已触发 → 剩余 500
        t1 = _make_trade(
            id='T1', notional=1000, stake=100,
            closed_at=utcnow_iso(), status='closed',
            close_type='tp2',
            tp1_triggered=True, tp1_slippage_pct=0.0,
            exit_slippage_pct=0.5,  # 500U × 0.5% = 2.5U
            exchange='binance',
        )
        # TP1 未触发 → 整笔 1000
        t2 = _make_trade(
            id='T2', symbol='OTHER/USDT', notional=1000, stake=100,
            closed_at=utcnow_iso(), status='closed',
            close_type='hard_stop',
            tp1_triggered=False, tp1_slippage_pct=0.0,
            exit_slippage_pct=0.5,  # 1000U × 0.5% = 5.0U
            exchange='binance',
        )
        stats = calculate_weekly_stats([t1, t2])
        # 2.5 + 5.0 = 7.5
        assert abs(stats['slippage_exit_cost'] - 7.5) < 0.01
