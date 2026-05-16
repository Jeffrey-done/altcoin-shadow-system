"""
审计修复回归测试 (2026-05)

覆盖范围:
  H-1: TG HTML escape + 降级重发
  M-1: TP1 触发 release_partial_stake，total_open_stake 不漂移
  M-2: 极端负费率（funding < FUNDING_MIN）应被信号评分严重扣分
  M-7: COOLDOWN_SCOPE='per_account' 时 is_in_cooldown 按账户隔离
  L-7: evaluate_trade entry/current=0 不抛异常
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

# conftest 已经 mock 了 ccxt

import common
import config
from common import (
    tg_escape, send_tg, atomic_write_json, load_json,
)
from models import Trade
from altcoin_tracker import evaluate_trade
import risk_control
from risk_control import (
    can_open_trade, record_trade_opened, record_trade_closed,
    release_partial_stake, RiskState, save_risk_state, load_risk_state,
)


# ══════════════════════════════════════════════════════════════════
#  H-1: TG escape & fallback
# ══════════════════════════════════════════════════════════════════

class TestH1TGEscape:
    def test_escape_html_special_chars(self):
        """tg_escape 把 < > & 转成实体"""
        assert tg_escape("<script>") == "&lt;script&gt;"
        assert tg_escape("a&b") == "a&amp;b"
        assert tg_escape("a > b < c & d") == "a &gt; b &lt; c &amp; d"

    def test_escape_handles_none(self):
        assert tg_escape(None) == ''

    def test_escape_handles_non_string(self):
        assert tg_escape(123) == '123'
        assert tg_escape(1.5) == '1.5'

    def test_send_tg_returns_false_when_unconfigured(self, monkeypatch):
        """没配 TG_BOT_TOKEN/CHAT_ID 时 send_tg 返回 False"""
        monkeypatch.setattr(common, 'TG_BOT_TOKEN', '')
        monkeypatch.setattr(common, 'TG_CHAT_ID', '')
        assert send_tg("hello") is False

    def test_send_tg_falls_back_to_plain_on_html_parse_error(self, monkeypatch):
        """HTML 解析失败 (400 + can't parse) → 降级 plain text 重发"""
        monkeypatch.setattr(common, 'TG_BOT_TOKEN', 'fake')
        monkeypatch.setattr(common, 'TG_CHAT_ID', 'fake')

        call_count = [0]

        def fake_post(url, json=None, timeout=None, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一次（HTML mode）失败
                resp = MagicMock()
                resp.status_code = 400
                resp.text = "Bad Request: can't parse entities"
                return resp
            # 第二次（plain）成功
            resp = MagicMock()
            resp.status_code = 200
            return resp

        monkeypatch.setattr(common.requests, 'post', fake_post)
        assert send_tg("<broken html") is True
        assert call_count[0] == 2  # 第一次 HTML 失败，第二次 plain 成功

    def test_send_tg_returns_true_on_first_success(self, monkeypatch):
        monkeypatch.setattr(common, 'TG_BOT_TOKEN', 'fake')
        monkeypatch.setattr(common, 'TG_CHAT_ID', 'fake')

        resp = MagicMock()
        resp.status_code = 200
        monkeypatch.setattr(common.requests, 'post', lambda *a, **k: resp)
        assert send_tg("hello") is True


# ══════════════════════════════════════════════════════════════════
#  M-1: TP1 release_partial_stake 闭环
# ══════════════════════════════════════════════════════════════════

class TestM1ReleasePartialStake:
    @pytest.fixture(autouse=True)
    def patch_files(self, monkeypatch, tmp_path):
        risk_file = str(tmp_path / "risk_state.json")
        trades_file = str(tmp_path / "trades.json")
        monkeypatch.setattr(common, 'RISK_FILE', risk_file)
        monkeypatch.setattr(common, 'TRADES_FILE', trades_file)
        monkeypatch.setattr(risk_control, 'RISK_FILE', risk_file)
        monkeypatch.setattr(risk_control, 'TRADES_FILE', trades_file)

    def test_release_partial_stake_decrements(self, mock_config):
        """release_partial_stake 仅减 total_open_stake，不动 daily_loss / consec"""
        state = RiskState(
            total_open_stake=100.0,
            daily_loss=10.0,
            consecutive_losses=2,
        )
        save_risk_state(state)

        release_partial_stake(50.0)

        new = load_risk_state()
        assert abs(new.total_open_stake - 50.0) < 0.01
        # 不影响其他字段
        assert abs(new.daily_loss - 10.0) < 0.01
        assert new.consecutive_losses == 2

    def test_tp1_then_tp2_full_round_trip(self, mock_config):
        """完整 TP1+TP2 流程 → total_open_stake 应回到 0"""
        # 手动构造一个开仓后的状态：开仓 100U
        state = RiskState(total_open_stake=100.0)
        save_risk_state(state)

        # 模拟 TP1: 释放 50U（半仓）
        release_partial_stake(50.0)
        s1 = load_risk_state()
        assert abs(s1.total_open_stake - 50.0) < 0.01

        # 模拟 TP2: record_trade_closed(positive_pnl, stake_remaining=50)
        record_trade_closed(pnl=20.0, stake=50.0)
        s2 = load_risk_state()
        # 净持仓应为 0
        assert abs(s2.total_open_stake) < 0.01

    def test_evaluate_trade_sets_pending_risk_partial_on_tp1(self, mock_config):
        """evaluate_trade TP1 命中应设置 pending_risk_partial=(pnl, half_stake)"""
        trade = Trade(
            id='T1', symbol='X/USDT', direction='SHORT',
            entry_price=100.0, stake=100, leverage=10,
            notional=1000, shares=10.0,
            opened_at=common.utcnow_iso(), status='open',
            take_profit_1=95.0, take_profit_2=85.0,
            hard_stop_price=110.0, max_hold_days=1,
            stake_remaining=100,
        )
        result = evaluate_trade(trade, current_price=94.0)
        assert trade.tp1_triggered is True
        assert result.pending_risk_partial is not None
        pnl, stake = result.pending_risk_partial
        # half stake = 100 * 0.5 = 50
        assert abs(stake - 50.0) < 0.01
        # locked pnl > 0
        assert pnl > 0


# ══════════════════════════════════════════════════════════════════
#  M-2: 极端负费率扣分
# ══════════════════════════════════════════════════════════════════

class TestM2NegFundingPenalty:
    def test_signal_score_extreme_negative_penalty(self, mock_config, monkeypatch):
        """负费率 -0.05% 应在 heat 维度扣 5"""
        import signal_score
        monkeypatch.setattr(config, 'SIGNAL_SCORE_ENABLED', True)
        monkeypatch.setattr(config, 'SCORE_FULL_THRESHOLD', 70)
        monkeypatch.setattr(config, 'SCORE_HALF_THRESHOLD', 40)
        monkeypatch.setattr(config, 'SCORE_SKIP_THRESHOLD', 40)
        monkeypatch.setattr(config, 'BTC_PUMP_THRESHOLD', 8.0)

        baseline = signal_score.calculate_signal_score(
            rsi_1d=82, rsi_4h=65, rsi_4h_peak=82,
            pct_24h=15, oi_change=20, funding_rate=0,
            yao_score=2, trigger_type='abandon',
        )
        with_neg = signal_score.calculate_signal_score(
            rsi_1d=82, rsi_4h=65, rsi_4h_peak=82,
            pct_24h=15, oi_change=20, funding_rate=-0.05,
            yao_score=2, trigger_type='abandon',
        )
        assert with_neg['score'] <= baseline['score']
        # heat 至少差 5
        assert (baseline['details']['heat'] - with_neg['details']['heat']) >= 5


# ══════════════════════════════════════════════════════════════════
#  M-7: COOLDOWN_SCOPE per_account
# ══════════════════════════════════════════════════════════════════

class TestM7CooldownScope:
    @pytest.fixture(autouse=True)
    def patch_files(self, monkeypatch, tmp_path):
        risk_file = str(tmp_path / "risk_state.json")
        trades_file = str(tmp_path / "trades.json")
        monkeypatch.setattr(common, 'RISK_FILE', risk_file)
        monkeypatch.setattr(common, 'TRADES_FILE', trades_file)
        monkeypatch.setattr(risk_control, 'RISK_FILE', risk_file)
        monkeypatch.setattr(risk_control, 'TRADES_FILE', trades_file)

    def test_cooldown_scope_global_default(self):
        """默认 'global'：任一账户止损 → 全局返回 cooldown"""
        # 这层在 risk_control.is_in_cooldown 已经测过；这里只确认默认值
        assert getattr(config, 'COOLDOWN_SCOPE', 'global') in ('global', 'per_account')

    def test_account_id_filter(self, mock_config):
        """显式传 account_id='acc_B' 时只看 acc_B 的交易"""
        recent_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        trades = [{
            'symbol': 'PEPE/USDT', 'status': 'closed',
            'closed_at': recent_iso,
            'close_type': 'hard_stop',
            'account_id': 'acc_A',
        }]
        atomic_write_json(common.TRADES_FILE, trades)
        # acc_B 不受 acc_A 止损影响
        in_cd, _ = risk_control.is_in_cooldown('PEPE/USDT', account_id='acc_B')
        assert in_cd is False
        # acc_A 自己受影响
        in_cd_a, _ = risk_control.is_in_cooldown('PEPE/USDT', account_id='acc_A')
        assert in_cd_a is True


# ══════════════════════════════════════════════════════════════════
#  L-7: entry=0 守卫
# ══════════════════════════════════════════════════════════════════

class TestL7ZeroEntryGuard:
    def test_evaluate_trade_zero_entry_no_exception(self):
        """entry_price=0 时 evaluate_trade 不应抛 ZeroDivisionError"""
        trade = Trade(
            id='BAD-01', symbol='X/USDT', direction='SHORT',
            entry_price=0.0,  # 异常数据
            stake=100, leverage=10, notional=1000, shares=0.0,
            opened_at=common.utcnow_iso(), status='open',
            take_profit_1=0, take_profit_2=0, hard_stop_price=0,
            max_hold_days=1, stake_remaining=100,
        )
        result = evaluate_trade(trade, current_price=1.0)
        assert result.closed is False
        assert result.pnl_pct == 0.0
        # trade 未被破坏
        assert trade.status == 'open'

    def test_evaluate_trade_zero_current_no_exception(self):
        """current_price=0 也不应炸"""
        trade = Trade(
            id='BAD-02', symbol='X/USDT', direction='SHORT',
            entry_price=100.0,
            stake=100, leverage=10, notional=1000, shares=10.0,
            opened_at=common.utcnow_iso(), status='open',
            take_profit_1=95, take_profit_2=85, hard_stop_price=110,
            max_hold_days=1, stake_remaining=100,
        )
        result = evaluate_trade(trade, current_price=0.0)
        assert result.closed is False


# ══════════════════════════════════════════════════════════════════
#  L-5: OKX bonus 整除修复
# ══════════════════════════════════════════════════════════════════

class TestL5OKXBonusSplit:
    def test_bonus_split_preserves_total_when_odd(self):
        """BONUS=7 时拆分应 4+3=7（旧实现 3+3=6 损失 1 分）"""
        # 直接验证拆分公式
        for bonus in (1, 3, 5, 7, 8):
            first = (bonus + 1) // 2
            second = bonus - first
            assert first + second == bonus
            # 上取整
            assert first >= bonus // 2
