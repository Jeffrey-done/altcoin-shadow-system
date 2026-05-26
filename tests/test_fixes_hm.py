"""
H 级 / M 级修复的回归测试。

覆盖范围:
  H4: realtime_monitor mtime 触发 snapshot 失效
  H7: TP1 实际成交数量回填 tp1_closed_shares → remaining_shares 精确
  M1: stake==0 异常路径 warn 而非死循环
  M2: get_realized_balance 不含浮动 TP1
  M3: close_retry_pending 参与判重（通过 common 行为而非 scanner 集成）
  M4: is_in_cooldown 用 date() 比较而非 startswith
  M5: 新 trail_retrace_ratio 语义与旧语义的触发对比
  M6: 复利平滑衰减
  M9: 负费率对做空扣分
  journal: add_pending / mark_confirmed / mark_failed / list_pending
"""

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

# conftest 已经 mock 了 ccxt

import common
import config
from common import (
    journal_add_pending, journal_mark_confirmed,
    journal_mark_failed, journal_list_pending, journal_cleanup_failed,
    get_compound_stake, get_realized_balance, get_dynamic_balance,
    load_json, atomic_write_json, utcnow_iso,
)
from models import Trade
from altcoin_tracker import evaluate_trade
import risk_control
import signal_score


# ══════════════════════════════════════════════════════════════════
#  夹具
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
def isolated_files(tmp_path, monkeypatch):
    """把所有持久化文件重定向到 tmp_path"""
    trades_file = str(tmp_path / 'trades.json')
    risk_file = str(tmp_path / 'risk.json')
    inflight_file = str(tmp_path / 'inflight.json')
    candidates_file = str(tmp_path / 'candidates.json')

    monkeypatch.setattr(common, 'TRADES_FILE', trades_file)
    monkeypatch.setattr(common, 'RISK_FILE', risk_file)
    monkeypatch.setattr(common, 'TRADES_INFLIGHT_FILE', inflight_file)
    monkeypatch.setattr(common, 'CANDIDATES_FILE', candidates_file)
    # risk_control 模块 import 时抓了 RISK_FILE / TRADES_FILE 的值，也要同步
    monkeypatch.setattr(risk_control, 'RISK_FILE', risk_file)
    monkeypatch.setattr(risk_control, 'TRADES_FILE', trades_file)

    return {
        'trades': trades_file,
        'risk': risk_file,
        'inflight': inflight_file,
        'candidates': candidates_file,
    }


def _make_trade(**overrides):
    """构造标准测试 Trade"""
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
#  H7: TP1 tp1_closed_shares 回填
# ══════════════════════════════════════════════════════════════════

class TestH7TP1ClosedShares:
    def test_tp1_hit_writes_closed_shares_and_exit_price(self, mock_config):
        """TP1 命中立即按计划值写 tp1_closed_shares 和 tp1_exit_price"""
        trade = _make_trade(take_profit_1=95.0, take_profit_2=85.0, hard_stop_price=110.0)
        evaluate_trade(trade, current_price=94.0)
        assert trade.tp1_triggered is True
        # 计划值：shares * 0.5 = 5.0
        assert abs(trade.tp1_closed_shares - 5.0) < 0.001
        # tp1_exit_price = 当前价
        assert abs(trade.tp1_exit_price - 94.0) < 0.001

    def test_remaining_shares_uses_tp1_closed_shares_when_available(self, mock_config):
        """remaining_shares 优先用 trade.shares - tp1_closed_shares
        （这里模拟交易所 filled 值略小于计划 0.5 比例，剩余应为差值）"""
        trade = _make_trade(
            stake=100, stake_remaining=50, leverage=10,
            take_profit_1=95.0, take_profit_2=85.0, hard_stop_price=105.0,
            tp1_triggered=True, tp1_locked_pnl=25.0,
            tp1_closed_shares=4.8,  # 模拟滑点导致实际只平了 4.8 而非 5.0
        )
        # 价格 106 >= hard_stop 105 → 硬止损命中，pending_close_amount 应是 10 - 4.8 = 5.2
        result = evaluate_trade(trade, current_price=106.0)
        assert result.closed is True
        assert result.pending_exchange_action == 'full_close'
        assert abs(result.pending_close_amount - 5.2) < 0.001

    def test_remaining_shares_fallback_when_no_tp1_closed_shares(self, mock_config):
        """没有 tp1_closed_shares 时按 stake 比例回退"""
        trade = _make_trade(
            stake=100, stake_remaining=50, leverage=10,
            take_profit_1=95.0, take_profit_2=85.0, hard_stop_price=105.0,
            tp1_triggered=True, tp1_locked_pnl=25.0,
            tp1_closed_shares=0.0,  # 未回填
        )
        # 回退值：shares * stake_remaining / stake = 10 * 50 / 100 = 5
        result = evaluate_trade(trade, current_price=106.0)
        assert result.closed is True
        assert abs(result.pending_close_amount - 5.0) < 0.001


# ══════════════════════════════════════════════════════════════════
#  M1: stake==0 异常路径
# ══════════════════════════════════════════════════════════════════

class TestM1StakeZero:
    def test_stake_zero_after_tp1_warns_and_falls_back(self, mock_config, caplog):
        """stake==0 + tp1_closed_shares==0 的异常数据 → warn + 回退全量 shares"""
        import logging
        trade = _make_trade(
            stake=0, stake_remaining=0, leverage=10, shares=10.0,
            take_profit_1=95.0, take_profit_2=85.0, hard_stop_price=105.0,
            tp1_triggered=True, tp1_closed_shares=0.0,
        )
        with caplog.at_level(logging.WARNING):
            result = evaluate_trade(trade, current_price=106.0)
        # 命中硬止损，remaining_shares 应回退到 trade.shares
        assert result.closed is True
        assert abs(result.pending_close_amount - 10.0) < 0.001
        # 日志里至少一条 warning
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any('stake==0' in r.message for r in warnings)


# ══════════════════════════════════════════════════════════════════
#  M2: get_realized_balance vs get_dynamic_balance
# ══════════════════════════════════════════════════════════════════

class TestM2RealizedBalance:
    def test_realized_balance_excludes_open_tp1_locked(self, isolated_files, mock_config):
        """open+tp1_locked_pnl 的浮动利润应被 get_realized_balance 排除"""
        trades = [
            # closed, 盈利 20
            {'status': 'closed', 'tp1_locked_pnl': 5, 'pnl': 15, 'account_id': ''},
            # open, TP1 已锁 10（浮动）
            {'status': 'open', 'tp1_locked_pnl': 10, 'pnl': 0, 'account_id': ''},
        ]
        atomic_write_json(isolated_files['trades'], trades)

        # dynamic 含浮动 TP1 锁定利润
        dyn = get_dynamic_balance('')
        # realized 严格只算 closed
        realized = get_realized_balance('')

        assert abs(dyn - (config.ACCOUNT_BALANCE + 20 + 10)) < 0.01
        assert abs(realized - (config.ACCOUNT_BALANCE + 20)) < 0.01
        # 差值 = 浮动 TP1
        assert abs((dyn - realized) - 10) < 0.01


# ══════════════════════════════════════════════════════════════════
#  M4: is_in_cooldown 按日期
# ══════════════════════════════════════════════════════════════════

class TestM4CooldownByDate:
    def test_stop_loss_cooldown_still_applies(self, isolated_files, mock_config):
        """止损平仓 < COOLDOWN_HOURS 小时 → in cooldown

        account_id=None(默认)现在走"全账户扫描"语义,老数据 account_id=''
        也能被正确匹配到(不再要求活跃账户 ID 一致)。
        """
        recent_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        trades = [{
            'symbol': 'PEPE/USDT', 'status': 'closed',
            'closed_at': recent_iso,
            'close_type': 'hard_stop', 'account_id': '',
        }]
        atomic_write_json(isolated_files['trades'], trades)
        in_cd, reason = risk_control.is_in_cooldown('PEPE/USDT')
        assert in_cd is True

    def test_same_day_non_stop_still_blocks(self, isolated_files, mock_config):
        """同日非止损亏损平仓 → 仍然 block(防 same-day 二次开仓扩大亏损)
        
        Fix: 只有亏损平仓才触发同日冷却。盈利平仓（TP2）不阻止同日再开。
        """
        now = datetime.now(timezone.utc)
        earlier_iso = now.replace(hour=0, minute=30, second=0, microsecond=0).isoformat()

        # Case 1: 盈利 TP2 平仓 → 不冷却（允许同日再开）
        trades_profit = [{
            'symbol': 'PEPE/USDT', 'status': 'closed',
            'closed_at': earlier_iso,
            'close_type': 'tp2', 'account_id': '',
            'tp1_locked_pnl': 5.0, 'pnl': 3.0,  # 盈利
        }]
        atomic_write_json(isolated_files['trades'], trades_profit)
        in_cd, reason = risk_control.is_in_cooldown('PEPE/USDT')
        assert in_cd is False, "盈利 TP2 不应触发同日冷却"

        # Case 2: 亏损非止损平仓（如时间止损后仍亏损）→ 仍然冷却
        trades_loss = [{
            'symbol': 'PEPE/USDT', 'status': 'closed',
            'closed_at': earlier_iso,
            'close_type': 'tp2', 'account_id': '',
            'tp1_locked_pnl': 0.0, 'pnl': -5.0,  # 亏损
        }]
        atomic_write_json(isolated_files['trades'], trades_loss)
        in_cd, reason = risk_control.is_in_cooldown('PEPE/USDT')
        assert in_cd is True
        assert '今日已亏损平仓' in reason

    def test_yesterday_non_stop_no_cooldown(self, isolated_files, mock_config):
        """昨日的非止损平仓不再 block"""
        yesterday_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        trades = [{
            'symbol': 'PEPE/USDT', 'status': 'closed',
            'closed_at': yesterday_iso,
            'close_type': 'tp2', 'account_id': '',
        }]
        atomic_write_json(isolated_files['trades'], trades)
        in_cd, _ = risk_control.is_in_cooldown('PEPE/USDT')
        assert in_cd is False

    def test_cooldown_scans_across_accounts_when_acc_id_not_given(
        self, isolated_files, mock_config,
    ):
        """
        回归测试: scanner 默认调用 is_in_cooldown(symbol) 不传 account_id 时,
        应能看到任何账户下的止损交易 — 不应只局限于当前活跃账户。

        场景: acc_A 刚硬止损了 PEPE,scanner 切到 acc_B 再次扫描 PEPE,
        如果 cooldown 只认活跃账户 → acc_B 会开仓,违反 "24h 同币冷却" 保护。
        """
        recent_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        trades = [{
            'symbol': 'PEPE/USDT', 'status': 'closed',
            'closed_at': recent_iso,
            'close_type': 'hard_stop',
            'account_id': 'acc_A',  # 某个特定账户,不是 _default
        }]
        atomic_write_json(isolated_files['trades'], trades)
        # 默认调用(不传 account_id) — 应当看到 acc_A 的止损并进入冷却
        in_cd, reason = risk_control.is_in_cooldown('PEPE/USDT')
        assert in_cd is True, (
            "is_in_cooldown(symbol) 不传 account_id 时必须跨账户扫描,"
            "防止切账户后冷却失效"
        )
        assert '冷却中' in reason

    def test_cooldown_scoped_to_account_when_explicit_id_given(
        self, isolated_files, mock_config,
    ):
        """
        显式传 account_id='acc_B' 时,应只看 acc_B 的交易 →
        acc_A 的止损不触发 acc_B 的冷却(精准账户级查询)。
        """
        recent_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        trades = [{
            'symbol': 'PEPE/USDT', 'status': 'closed',
            'closed_at': recent_iso,
            'close_type': 'hard_stop',
            'account_id': 'acc_A',
        }]
        atomic_write_json(isolated_files['trades'], trades)
        # 显式查 acc_B 视角 — acc_A 的止损不算数
        in_cd, _ = risk_control.is_in_cooldown('PEPE/USDT', account_id='acc_B')
        assert in_cd is False, (
            "显式传 account_id 时应只看该账户的交易"
        )


# ══════════════════════════════════════════════════════════════════
#  M5: 新移动止损语义
# ══════════════════════════════════════════════════════════════════

class TestM5TrailRetraceRatio:
    def test_trail_stop_price_uses_retrace_ratio(self, mock_config, monkeypatch):
        """新公式：trigger_pct = best * (1 - retrace_ratio)
        best=5, ratio=0.4 → trigger=3%, trail_stop=entry*0.97 (做空)"""
        monkeypatch.setattr(config, 'TRAIL_STOP_RETRACE_RATIO', 0.4, raising=False)
        trade = _make_trade(
            entry_price=100.0, take_profit_1=90.0, take_profit_2=85.0,
            hard_stop_price=110.0, best_pnl_pct=0.0, trail_stop_price=None,
        )
        # price=95 → pnl_pct=5% 更新 best 和 trail
        evaluate_trade(trade, current_price=95.0)
        assert abs(trade.best_pnl_pct - 5.0) < 0.01
        # trail = entry * (1 - 3%/100) = 100 * 0.97 = 97
        assert trade.trail_stop_price is not None
        assert abs(trade.trail_stop_price - 97.0) < 0.01
        # 97 < 硬止损 110，所以移动止损先于硬止损触发
        assert trade.trail_stop_price < trade.hard_stop_price

    def test_trail_triggers_before_hard_stop(self, mock_config, monkeypatch):
        """价格从高盈利回撤到 trail_stop_price 时，应该在硬止损之前平仓"""
        monkeypatch.setattr(config, 'TRAIL_STOP_RETRACE_RATIO', 0.4, raising=False)
        trade = _make_trade(
            entry_price=100.0, take_profit_1=90.0, take_profit_2=85.0,
            hard_stop_price=110.0,
        )
        # 先到 95，激活 trail
        evaluate_trade(trade, current_price=95.0)
        assert trade.trail_stop_price is not None
        # 再反弹到 98（触及 trail=97，但远未到硬止损 110）
        result = evaluate_trade(trade, current_price=98.0)
        assert result.closed is True
        assert trade.close_type == 'trail_stop'


# ══════════════════════════════════════════════════════════════════
#  M6: 复利平滑
# ══════════════════════════════════════════════════════════════════

class TestM6CompoundSmoothing:
    def test_compound_is_smooth_not_stepped(self, isolated_files, mock_config, monkeypatch):
        """旧语义：total_pnl=49 / step=50 → steps=0 → 原 stake；
        total_pnl=50 → steps=1 → stake+25 一次性跳
        新语义：49 → stake + (49/50)*25 ≈ stake+24.5
        → 两个盈利值产生的 stake 差值应 <= 1（平滑）"""
        monkeypatch.setattr(config, 'AUTO_COMPOUND_ENABLED', True)
        monkeypatch.setattr(config, 'COMPOUND_STEP', 50)
        monkeypatch.setattr(config, 'COMPOUND_INCREASE', 25)
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 150)
        monkeypatch.setattr(config, 'MAX_OPEN_TRADES', 3)
        monkeypatch.setattr(config, 'COMPOUND_MAX_STAKE', 300)

        def set_total_pnl(total):
            atomic_write_json(isolated_files['trades'], [{
                'status': 'closed', 'tp1_locked_pnl': 0, 'pnl': total, 'account_id': '',
            }])

        set_total_pnl(49)
        s_49 = get_compound_stake('')
        set_total_pnl(51)
        s_51 = get_compound_stake('')
        # 新平滑语义下 s_51 - s_49 ~ 1（对应 2/50 * 25 = 1 U）
        assert s_51 - s_49 <= 2, f"平滑应该只差约 1U，实际 {s_51 - s_49}U"

    def test_compound_no_increase_on_loss(self, isolated_files, mock_config, monkeypatch):
        """总盈亏 <= 0 → 返回 base_stake (account_balance / max_open_trades)"""
        monkeypatch.setattr(config, 'AUTO_COMPOUND_ENABLED', True)
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 150)
        monkeypatch.setattr(config, 'MAX_OPEN_TRADES', 3)

        atomic_write_json(isolated_files['trades'], [{
            'status': 'closed', 'tp1_locked_pnl': 0, 'pnl': -20, 'account_id': '',
        }])
        assert get_compound_stake('') == 50  # 150 / 3 = 50

    def test_compound_respects_cap(self, isolated_files, mock_config, monkeypatch):
        """复利上限应生效"""
        monkeypatch.setattr(config, 'AUTO_COMPOUND_ENABLED', True)
        monkeypatch.setattr(config, 'COMPOUND_STEP', 50)
        monkeypatch.setattr(config, 'COMPOUND_INCREASE', 25)
        monkeypatch.setattr(config, 'ACCOUNT_BALANCE', 150)
        monkeypatch.setattr(config, 'MAX_OPEN_TRADES', 3)
        monkeypatch.setattr(config, 'COMPOUND_MAX_STAKE', 100)

        atomic_write_json(isolated_files['trades'], [{
            'status': 'closed', 'tp1_locked_pnl': 0, 'pnl': 10000, 'account_id': '',
        }])
        assert get_compound_stake('') <= 100


# ══════════════════════════════════════════════════════════════════
#  M9: 负费率对做空扣分
# ══════════════════════════════════════════════════════════════════

class TestM9NegativeFunding:
    def test_extreme_negative_funding_penalizes(self, mock_config, monkeypatch):
        """极端负费率（<=-3%）heat 扣 5"""
        monkeypatch.setattr(config, 'SIGNAL_SCORE_ENABLED', True)
        monkeypatch.setattr(config, 'BTC_PUMP_THRESHOLD', 8.0)
        monkeypatch.setattr(config, 'SCORE_FULL_THRESHOLD', 70)
        monkeypatch.setattr(config, 'SCORE_HALF_THRESHOLD', 40)
        monkeypatch.setattr(config, 'SCORE_SKIP_THRESHOLD', 40)

        # 基准：费率 = 0（无加无减）
        baseline = signal_score.calculate_signal_score(
            rsi_1d=82, rsi_4h=65, rsi_4h_peak=82,
            pct_24h=15, oi_change=20, funding_rate=0,
            yao_score=2, trigger_type='abandon',
            abandon_oi_declining=False, btc_24h_pct=0,
        )

        # 极端负费率 -0.05
        with_neg = signal_score.calculate_signal_score(
            rsi_1d=82, rsi_4h=65, rsi_4h_peak=82,
            pct_24h=15, oi_change=20, funding_rate=-0.05,
            yao_score=2, trigger_type='abandon',
            abandon_oi_declining=False, btc_24h_pct=0,
        )

        # 极端负费率 heat 应扣 5
        assert with_neg['score'] <= baseline['score']
        assert with_neg['details']['heat'] < baseline['details']['heat']

    def test_positive_funding_still_bonus(self, mock_config, monkeypatch):
        """正费率加分没坏"""
        monkeypatch.setattr(config, 'SIGNAL_SCORE_ENABLED', True)
        baseline = signal_score.calculate_signal_score(
            rsi_1d=82, rsi_4h=65, rsi_4h_peak=82,
            pct_24h=15, oi_change=20, funding_rate=0,
            yao_score=2, trigger_type='abandon',
        )
        hot = signal_score.calculate_signal_score(
            rsi_1d=82, rsi_4h=65, rsi_4h_peak=82,
            pct_24h=15, oi_change=20, funding_rate=0.05,
            yao_score=2, trigger_type='abandon',
        )
        assert hot['score'] >= baseline['score']


# ══════════════════════════════════════════════════════════════════
#  Journal: add_pending / mark_confirmed / mark_failed / list_pending
# ══════════════════════════════════════════════════════════════════

class TestJournalAPI:
    def test_journal_add_and_confirm(self, isolated_files):
        """add_pending 后 list_pending 返回 1 条；confirm 后返回 0 条"""
        assert len(journal_list_pending()) == 0

        journal_add_pending(
            client_order_id='coid-1', exchange='binance',
            account_id='acc1', symbol='PEPE/USDT',
            direction='SHORT', stake=50, leverage=10,
        )
        assert len(journal_list_pending()) == 1

        journal_mark_confirmed('coid-1', order_id='BN-123')
        assert len(journal_list_pending()) == 0

    def test_journal_idempotent_add(self, isolated_files):
        """重复 add_pending 同 coid 不会产生双条目"""
        journal_add_pending(
            client_order_id='coid-1', exchange='binance',
            account_id='acc1', symbol='X/USDT',
            direction='SHORT', stake=50, leverage=10,
        )
        journal_add_pending(
            client_order_id='coid-1', exchange='binance',
            account_id='acc1', symbol='X/USDT',
            direction='SHORT', stake=50, leverage=10,
        )
        assert len(journal_list_pending()) == 1

    def test_journal_mark_failed(self, isolated_files):
        """mark_failed 把 pending 转 failed，pending 列表为空"""
        journal_add_pending(
            client_order_id='coid-fail', exchange='binance',
            account_id='', symbol='X/USDT',
            direction='SHORT', stake=50, leverage=10,
        )
        journal_mark_failed('coid-fail', error='network timeout')
        # pending 列表不再包含它（status=failed）
        pending = journal_list_pending()
        assert not any(e['client_order_id'] == 'coid-fail' for e in pending)
        # 但主文件里还有（failed 记录）
        data = load_json(isolated_files['inflight'], [])
        failed = [e for e in data if e.get('client_order_id') == 'coid-fail']
        assert len(failed) == 1
        assert failed[0]['status'] == 'failed'
        assert 'timeout' in failed[0]['last_error']

    def test_journal_cleanup_failed(self, isolated_files):
        """cleanup_failed 清理过期 failed，保留 pending 和未过期 failed"""
        journal_add_pending(
            client_order_id='pend', exchange='binance',
            account_id='', symbol='X/USDT',
            direction='SHORT', stake=50, leverage=10,
        )
        journal_add_pending(
            client_order_id='old-fail', exchange='binance',
            account_id='', symbol='X/USDT',
            direction='SHORT', stake=50, leverage=10,
        )
        journal_mark_failed('old-fail', 'err')

        # 手工改 updated_at 为 100 小时前
        data = load_json(isolated_files['inflight'], [])
        for e in data:
            if e['client_order_id'] == 'old-fail':
                past = datetime.now(timezone.utc) - timedelta(hours=100)
                e['updated_at'] = past.isoformat()
        atomic_write_json(isolated_files['inflight'], data)

        cleared = journal_cleanup_failed(retain_hours=72)
        assert cleared >= 1

        remaining = load_json(isolated_files['inflight'], [])
        remaining_ids = {e['client_order_id'] for e in remaining}
        assert 'pend' in remaining_ids
        assert 'old-fail' not in remaining_ids


# ══════════════════════════════════════════════════════════════════
#  H4: mtime 触发 snapshot 失效
# ══════════════════════════════════════════════════════════════════

class TestH4MtimeSnapshot:
    def test_snapshot_mtime_tracked_after_refresh(self, isolated_files, mock_config,
                                                    monkeypatch):
        """refresh_snapshot 会同步更新 _snapshot_mtime 到文件当前 mtime"""
        # realtime_monitor 在 import 时用了 common.TRADES_FILE，所以也要 monkeypatch 它
        import realtime_monitor as rm
        monkeypatch.setattr(rm, 'TRADES_FILE', isolated_files['trades'])

        # 初始状态：文件不存在 → mtime 为 0
        rm.refresh_snapshot()
        assert rm._snapshot_mtime == 0.0

        # 写入 trades.json
        atomic_write_json(isolated_files['trades'], [{
            'id': 'T1', 'symbol': 'PEPE/USDT', 'direction': 'SHORT',
            'entry_price': 100.0, 'stake': 50, 'leverage': 10, 'notional': 500,
            'shares': 5.0, 'opened_at': utcnow_iso(), 'status': 'open',
            'take_profit_1': 95.0, 'take_profit_2': 90.0, 'hard_stop_price': 105.0,
            'account_id': '',
        }])

        rm.refresh_snapshot()
        # 刷新后 mtime 应为文件实际 mtime
        expected_mtime = os.path.getmtime(isolated_files['trades'])
        assert abs(rm._snapshot_mtime - expected_mtime) < 0.01
        assert 'PEPE/USDT' in rm._trade_snapshots

    def test_maybe_refresh_triggers_on_mtime_change(self, isolated_files, mock_config,
                                                    monkeypatch):
        """文件 mtime 变化 → _maybe_refresh_on_mtime_change 会强制 refresh_snapshot"""
        import realtime_monitor as rm
        monkeypatch.setattr(rm, 'TRADES_FILE', isolated_files['trades'])

        # 初始：空 snapshot
        atomic_write_json(isolated_files['trades'], [])
        rm.refresh_snapshot()
        assert len(rm._trade_snapshots) == 0

        # 绕过节流，强制下次检查立即执行
        rm._last_mtime_check = 0

        # 外部进程模拟修改 trades 文件
        time.sleep(0.02)  # 确保 mtime 会不同
        atomic_write_json(isolated_files['trades'], [{
            'id': 'T2', 'symbol': 'DOGE/USDT', 'direction': 'SHORT',
            'entry_price': 1.0, 'stake': 50, 'leverage': 10, 'notional': 500,
            'shares': 500.0, 'opened_at': utcnow_iso(), 'status': 'open',
            'take_profit_1': 0.95, 'take_profit_2': 0.90, 'hard_stop_price': 1.05,
            'account_id': '',
        }])

        # 触发 mtime 检查
        rm._maybe_refresh_on_mtime_change()
        # 刚修改 → snapshot 应该自动更新
        assert 'DOGE/USDT' in rm._trade_snapshots
