"""
风控模块测试
测试 risk_control 模块的开仓检查、亏损记录、连亏暂停等
"""

import pytest
import json
from datetime import datetime, timezone, timedelta

import config
import common
import risk_control
from risk_control import (
    can_open_trade, record_trade_closed, record_trade_opened,
    load_risk_state, save_risk_state, RiskState,
)


@pytest.fixture(autouse=True)
def patch_risk_files(monkeypatch, tmp_path):
    """为每个测试重定向 RISK_FILE 和 TRADES_FILE 到临时目录"""
    risk_file = str(tmp_path / "risk_state.json")
    trades_file = str(tmp_path / "trades.json")
    funding_trades_file = str(tmp_path / "funding_trades.json")
    low_risk_trades_file = str(tmp_path / "low_risk_trades.json")

    monkeypatch.setattr(common, 'RISK_FILE', risk_file)
    monkeypatch.setattr(common, 'TRADES_FILE', trades_file)
    monkeypatch.setattr(common, 'FUNDING_TRADES_FILE', funding_trades_file)
    monkeypatch.setattr(common, 'LOW_RISK_TRADES_FILE', low_risk_trades_file)
    monkeypatch.setattr(risk_control, 'RISK_FILE', risk_file)
    monkeypatch.setattr(risk_control, 'TRADES_FILE', trades_file)


class TestCanOpenTrade:
    """can_open_trade 检查测试"""

    def test_can_open_trade_allowed(self, mock_config):
        """新状态、小仓位 -> 允许开仓"""
        allowed, reason = can_open_trade(50, strategy='short')
        assert allowed is True
        assert reason == "OK"

    def test_daily_loss_blocks(self, mock_config, monkeypatch):
        """当日亏损达上限 -> 拒绝开仓"""
        # 先创建一个状态文件，daily_loss 达到上限
        state = RiskState(
            date=common.today_str(),
            daily_loss=30.0,  # = RISK_MAX_DAILY_LOSS
        )
        save_risk_state(state)

        allowed, reason = can_open_trade(50, strategy='short')
        assert allowed is False
        assert '亏损' in reason

    def test_daily_trades_blocks(self, mock_config):
        """当日开仓次数达上限 -> 拒绝开仓"""
        state = RiskState(
            date=common.today_str(),
            daily_trades_opened=2,  # = RISK_MAX_DAILY_TRADES
        )
        save_risk_state(state)

        allowed, reason = can_open_trade(50, strategy='short')
        assert allowed is False
        assert '次数' in reason or '开仓' in reason

    def test_consecutive_loss_pause(self, mock_config):
        """连亏暂停中（paused_until 在未来）-> 拒绝开仓"""
        future_time = (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat()
        state = RiskState(
            date=common.today_str(),
            consecutive_losses=3,
            paused_until=future_time,
        )
        save_risk_state(state)

        allowed, reason = can_open_trade(50, strategy='short')
        assert allowed is False
        assert '暂停' in reason

    def test_pause_expires(self, mock_config):
        """暂停已过期 -> 允许开仓"""
        past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state = RiskState(
            date=common.today_str(),
            consecutive_losses=3,
            paused_until=past_time,
        )
        save_risk_state(state)

        allowed, reason = can_open_trade(50, strategy='short')
        assert allowed is True
        assert reason == "OK"

    def test_position_limit_blocks(self, mock_config, monkeypatch):
        """持仓占比超限 -> 拒绝开仓"""
        # ACCOUNT_BALANCE=1000, SHORT_STRATEGY_POOL_PCT=60, RISK_MAX_POSITION_PCT=0.5
        # max_position = 1000 * 0.6 * 0.5 = 300
        state = RiskState(
            date=common.today_str(),
            total_open_stake=280.0,
        )
        save_risk_state(state)

        # Mock _calc_actual_open_stake to return the state value
        # (in production it reads trade files, which are empty in tests)
        monkeypatch.setattr(risk_control, '_calc_actual_open_stake', lambda: 280.0)

        # 280 + 50 = 330 > 300 -> blocked
        allowed, reason = can_open_trade(50, strategy='short')
        assert allowed is False
        assert '持仓' in reason or '超限' in reason

    def test_strategy_pool_isolation(self, mock_config):
        """
        资金池隔离：funding_arb 使用 FUNDING_ARB_POOL_PCT。
        ACCOUNT_BALANCE=1000, FUNDING_ARB_POOL_PCT=20, RISK_MAX_POSITION_PCT=0.5
        max = 1000 * 0.2 * 0.5 = 100
        stake=101 应被拒绝
        """
        state = RiskState(date=common.today_str(), total_open_stake=0)
        save_risk_state(state)

        # 101 > 100 -> blocked
        allowed, reason = can_open_trade(101, strategy='funding_arb')
        assert allowed is False

        # 99 < 100 -> allowed
        allowed2, reason2 = can_open_trade(99, strategy='funding_arb')
        assert allowed2 is True


class TestRecordTradeClosed:
    """record_trade_closed 测试"""

    def test_record_loss_increments_consecutive(self, mock_config):
        """亏损交易 -> consecutive_losses 增加"""
        # 初始状态
        state = RiskState(date=common.today_str(), consecutive_losses=1)
        save_risk_state(state)

        record_trade_closed(pnl=-10.0, stake=100)

        state = load_risk_state()
        assert state.consecutive_losses == 2

    def test_record_profit_resets_consecutive(self, mock_config):
        """盈利交易 -> consecutive_losses 重置为 0"""
        state = RiskState(date=common.today_str(), consecutive_losses=2)
        save_risk_state(state)

        record_trade_closed(pnl=15.0, stake=100)

        state = load_risk_state()
        assert state.consecutive_losses == 0

    def test_record_loss_accumulates_daily(self, mock_config):
        """亏损累计到 daily_loss"""
        state = RiskState(date=common.today_str(), daily_loss=10.0)
        save_risk_state(state)

        record_trade_closed(pnl=-5.0, stake=100)

        state = load_risk_state()
        assert state.daily_loss == 15.0

    def test_record_reduces_open_stake(self, mock_config):
        """平仓减少 total_open_stake"""
        state = RiskState(date=common.today_str(), total_open_stake=200.0)
        save_risk_state(state)

        record_trade_closed(pnl=10.0, stake=100)

        state = load_risk_state()
        assert state.total_open_stake == 100.0
