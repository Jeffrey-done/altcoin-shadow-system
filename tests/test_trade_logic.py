"""
交易逻辑测试
测试 altcoin_tracker.evaluate_trade() 各类止盈止损场景
"""

from datetime import datetime, timezone, timedelta

from models import Trade
from altcoin_tracker import evaluate_trade
import common


def _make_trade(direction='SHORT', entry_price=100.0, stake=100, leverage=10,
                hard_stop=None, tp1=None, tp2=None, max_hold_days=1,
                opened_at=None, tp1_triggered=False, tp1_locked_pnl=0.0,
                stake_remaining=None, best_pnl_pct=0.0, trail_stop_price=None):
    """构造测试用 Trade 对象"""
    if stake_remaining is None:
        stake_remaining = stake
    if opened_at is None:
        opened_at = common.utcnow_iso()
    return Trade(
        id='TEST-001',
        symbol='TEST/USDT',
        direction=direction,
        entry_price=entry_price,
        stake=stake,
        leverage=leverage,
        notional=stake * leverage,
        shares=round(stake * leverage / entry_price, 4),
        opened_at=opened_at,
        status='open',
        take_profit_1=tp1 if tp1 else 0.0,
        take_profit_2=tp2 if tp2 else 0.0,
        tp1_triggered=tp1_triggered,
        tp1_locked_pnl=tp1_locked_pnl,
        stake_remaining=stake_remaining,
        hard_stop_price=hard_stop,
        best_pnl_pct=best_pnl_pct,
        trail_stop_price=trail_stop_price,
        max_hold_days=max_hold_days,
    )


class TestHardStop:
    """硬止损测试"""

    def test_hard_stop_short(self):
        """做空：价格上涨超过硬止损 -> 平仓亏损"""
        trade = _make_trade(direction='SHORT', entry_price=100.0, hard_stop=103.0)
        result = evaluate_trade(trade, current_price=104.0)
        assert result.closed is True
        assert result.pnl_usd < 0
        assert trade.status == 'closed'

    def test_hard_stop_long(self):
        """做多：价格下跌低于硬止损 -> 平仓亏损"""
        trade = _make_trade(direction='LONG', entry_price=100.0, hard_stop=97.0)
        result = evaluate_trade(trade, current_price=96.0)
        assert result.closed is True
        assert result.pnl_usd < 0
        assert trade.status == 'closed'

    def test_hard_stop_not_triggered(self):
        """价格未到硬止损 -> 不平仓"""
        trade = _make_trade(direction='SHORT', entry_price=100.0, hard_stop=103.0)
        result = evaluate_trade(trade, current_price=101.0)
        assert result.closed is False
        assert trade.status == 'open'


class TestTakeProfit:
    """止盈测试"""

    def test_tp1_triggers_short(self):
        """做空 TP1 触发：价格跌到止盈一档 -> tp1_triggered=True, stake_remaining减少"""
        trade = _make_trade(direction='SHORT', entry_price=100.0, tp1=95.0, tp2=90.0,
                            stake=100, leverage=10)
        result = evaluate_trade(trade, current_price=94.0)
        assert trade.tp1_triggered is True
        assert trade.stake_remaining < 100
        assert result.closed is False  # TP1 不平仓

    def test_tp2_triggers_after_tp1(self):
        """做空 TP2 触发：先触发 TP1 再触发 TP2 -> 全仓平仓"""
        trade = _make_trade(direction='SHORT', entry_price=100.0, tp1=95.0, tp2=90.0,
                            stake=100, leverage=10)
        # 先触发 TP1
        evaluate_trade(trade, current_price=94.0)
        assert trade.tp1_triggered is True

        # 再触发 TP2
        result = evaluate_trade(trade, current_price=89.0)
        assert result.closed is True
        assert trade.status == 'closed'
        assert result.pnl_usd > 0  # 盈利

    def test_tp2_requires_tp1_first(self):
        """TP1 和 TP2 之间：先触发 TP1，TP2 等下次评估"""
        trade = _make_trade(direction='SHORT', entry_price=100.0, tp1=95.0, tp2=85.0)
        # 价格在 TP1(95) 和 TP2(85) 之间 → 只触发 TP1
        result = evaluate_trade(trade, current_price=90.0)
        assert trade.tp1_triggered is True
        assert result.closed is False
        assert result.pending_exchange_action == 'tp1_partial'

        # 第二次评估：价格继续下穿 TP2
        result2 = evaluate_trade(trade, current_price=84.0)
        assert result2.closed is True
        assert trade.status == 'closed'
        assert result2.pending_exchange_action == 'full_close'


class TestTrailingStop:
    """移动止损测试"""

    def test_trailing_stop_short(self, mock_config):
        """做空移动止损：价格回撤超过阈值 -> 平仓"""
        # 设置已经有高盈利（best_pnl_pct=5%），trail_stop_price=96
        # TRAIL_STOP_ACTIVATE_PCT=3, 所以 best_pnl_pct=5 > 3, 已激活
        trade = _make_trade(
            direction='SHORT', entry_price=100.0,
            best_pnl_pct=5.0, trail_stop_price=96.0,
            tp1=95.0, tp2=90.0,
        )
        # 价格回到97 > trail_stop_price=96 -> 触发移动止损
        result = evaluate_trade(trade, current_price=97.0)
        assert result.closed is True
        assert trade.status == 'closed'
        assert '移动止损' in trade.close_reason

    def test_trailing_stop_updates_best(self, mock_config):
        """移动止损：新高盈利更新 best_pnl_pct 和 trail_stop_price"""
        trade = _make_trade(
            direction='SHORT', entry_price=100.0,
            best_pnl_pct=0.0, trail_stop_price=None,
            tp1=90.0, tp2=85.0, hard_stop=110.0,
        )
        # 价格跌到 96 -> pnl_pct = 4% > TRAIL_STOP_ACTIVATE_PCT=3
        evaluate_trade(trade, current_price=96.0)
        assert trade.best_pnl_pct == 4.0
        assert trade.trail_stop_price is not None


class TestTimeStop:
    """时间止损测试"""

    def test_time_stop(self, monkeypatch):
        """持仓超时且盈利不足 -> 时间止损平仓"""
        # 设置 opened_at 为5天前
        past_time = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        trade = _make_trade(
            direction='SHORT', entry_price=100.0,
            max_hold_days=1, opened_at=past_time,
            hard_stop=110.0, tp1=90.0, tp2=85.0,
        )
        # 价格只跌了0.5%，低于 TIME_STOP_MIN_PROFIT_PCT=3
        result = evaluate_trade(trade, current_price=99.5)
        assert result.closed is True
        assert trade.status == 'closed'
        assert '时间' in trade.close_reason or 'time' in trade.close_reason.lower()

    def test_time_stop_not_triggered_with_profit(self, monkeypatch):
        """持仓超时但盈利足够 -> 不触发时间止损"""
        past_time = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        trade = _make_trade(
            direction='SHORT', entry_price=100.0,
            max_hold_days=1, opened_at=past_time,
            hard_stop=110.0, tp1=90.0, tp2=85.0,
        )
        # 价格跌了5% > TIME_STOP_MIN_PROFIT_PCT=3
        result = evaluate_trade(trade, current_price=95.0)
        # 应触发 TP1 而非时间止损
        assert trade.tp1_triggered is True or result.closed is False


class TestPnlCalculation:
    """盈亏计算测试"""

    def test_pnl_calculation_short(self):
        """做空盈利计算: entry=100, price=95 -> +5%"""
        trade = _make_trade(direction='SHORT', entry_price=100.0, stake=100, leverage=10,
                            hard_stop=110.0, tp1=90.0, tp2=85.0)
        result = evaluate_trade(trade, current_price=95.0)
        # pnl_pct = (100 - 95) / 100 * 100 = 5%
        assert abs(result.pnl_pct - 5.0) < 0.01
        # pnl_usd = 100 * 10 * 5 / 100 = 50
        assert abs(result.pnl_usd - 50.0) < 0.01

    def test_pnl_calculation_long(self):
        """做多盈利计算: entry=100, price=105 -> +5%"""
        trade = _make_trade(direction='LONG', entry_price=100.0, stake=100, leverage=10,
                            hard_stop=90.0, tp1=110.0, tp2=120.0)
        result = evaluate_trade(trade, current_price=105.0)
        # pnl_pct = (105 - 100) / 100 * 100 = 5%
        assert abs(result.pnl_pct - 5.0) < 0.01
        # pnl_usd = 100 * 10 * 5 / 100 = 50
        assert abs(result.pnl_usd - 50.0) < 0.01

    def test_pnl_calculation_short_loss(self):
        """做空亏损计算: entry=100, price=102 -> -2%"""
        trade = _make_trade(direction='SHORT', entry_price=100.0, stake=100, leverage=10,
                            hard_stop=110.0, tp1=90.0, tp2=85.0)
        result = evaluate_trade(trade, current_price=102.0)
        # pnl_pct = (100 - 102) / 100 * 100 = -2%
        assert abs(result.pnl_pct - (-2.0)) < 0.01
        # pnl_usd = 100 * 10 * (-2) / 100 = -20
        assert abs(result.pnl_usd - (-20.0)) < 0.01



class TestTp1DoubleCountRegression:
    """
    回归测试：v4.1 修复 TP1 双计数 bug。

    场景：TP1 触发后剩余 50% 仓位最终因硬止损平仓。
    验证：trade.pnl（剩余仓位的实现盈亏）与 tp1_locked_pnl 分开记账，
         tp1_locked_pnl + pnl 等于真实的总盈亏，不会出现 TP1 被算两次。
    """

    def test_tp1_then_hard_stop_total_pnl_correct(self, mock_config):
        """
        entry=100, stake=100, leverage=10, TP1=95, hard_stop=103
        流程：
          1) price=94 → TP1 触发：locked_pnl = 100 * 0.5 * 10 * 6/100 = 30U
             stake_remaining=50，trade.pnl 应该 = 剩余50仓位浮动盈亏 = 50*10*6/100 = 30U
          2) price=104 → 硬止损：pnl_pct = (100-104)/100 * 100 = -4%
             remaining_pnl = 50 * 10 * -4/100 = -20U
             total = tp1_locked_pnl(30) + remaining_pnl(-20) = 10U
        期望：trade.pnl（剩余仓位实现）== -20，tp1_locked_pnl == 30，合计 10。
        """
        trade = _make_trade(
            direction='SHORT', entry_price=100.0,
            stake=100, leverage=10,
            tp1=95.0, tp2=85.0, hard_stop=103.0,
        )
        # TP1 触发
        evaluate_trade(trade, current_price=94.0)
        assert trade.tp1_triggered is True
        assert abs(trade.tp1_locked_pnl - 30.0) < 0.01
        assert abs(trade.stake_remaining - 50.0) < 0.01
        # TP1 触发后 trade.pnl 应该是剩余仓位的浮动盈亏，不包含 tp1_locked
        assert abs(trade.pnl - 30.0) < 0.01

        # 硬止损触发
        result = evaluate_trade(trade, current_price=104.0)
        assert result.closed is True
        # trade.pnl 是剩余仓位的实现盈亏（约 -20U）
        assert abs(trade.pnl - (-20.0)) < 0.01
        # TP1 锁定的利润不变
        assert abs(trade.tp1_locked_pnl - 30.0) < 0.01
        # 合计盈亏 = tp1_locked + pnl = 10U
        total = trade.tp1_locked_pnl + trade.pnl
        assert abs(total - 10.0) < 0.01
        # result.pnl_usd 也应该等于合计
        assert abs(result.pnl_usd - 10.0) < 0.01
        # close_type 已设为枚举值（机器可读）
        assert trade.close_type == 'hard_stop'

    def test_tp1_tp2_close_type_is_tp2(self, mock_config):
        """TP2 全仓平仓后 close_type 应为 'tp2'"""
        trade = _make_trade(
            direction='SHORT', entry_price=100.0,
            stake=100, leverage=10,
            tp1=95.0, tp2=90.0, hard_stop=110.0,
        )
        evaluate_trade(trade, current_price=94.0)  # TP1
        result = evaluate_trade(trade, current_price=89.0)  # TP2
        assert result.closed is True
        assert trade.close_type == 'tp2'

    def test_hard_stop_without_tp1_total_pnl_correct(self, mock_config):
        """
        没 TP1 过就硬止损：tp1_locked_pnl=0, trade.pnl = 整笔亏损。
        entry=100, price=104 → pnl_pct = -4%，亏 100*10*4/100 = 40U
        """
        trade = _make_trade(
            direction='SHORT', entry_price=100.0,
            stake=100, leverage=10,
            tp1=90.0, tp2=85.0, hard_stop=103.0,
        )
        result = evaluate_trade(trade, current_price=104.0)
        assert result.closed is True
        assert trade.tp1_locked_pnl == 0.0
        assert abs(trade.pnl - (-40.0)) < 0.01
        assert abs(result.pnl_usd - (-40.0)) < 0.01
        assert trade.close_type == 'hard_stop'
