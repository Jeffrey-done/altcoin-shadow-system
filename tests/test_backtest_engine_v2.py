"""
M-3 完整修复回归测试：事件驱动主循环

覆盖：
  - _OpenTrade / _BacktestState / _step_open_trade 单元
  - compute_compound_stake 复利
  - get_btc_24h_change_at BTC 索引查询
  - _check_risk_gates 风控网关
  - run_backtest_event_driven 主循环（端到端）
  - 与 legacy 引擎结果对照（关闭所有动态特性时数值对齐）
  - 资金费率持仓成本仍生效
"""

from datetime import datetime, timedelta, timezone
import pytest

# conftest 已 mock 掉 ccxt

import config
from backtest import (
    BacktestParams,
    _OpenTrade,
    _BacktestState,
    _iso_to_ms,
    _open_position,
    _step_open_trade,
    _finalize_open_trade,
    compute_compound_stake,
    get_btc_24h_change_at,
    _check_risk_gates,
    run_backtest_event_driven,
)


# ══════════════════════════════════════════════════════════════════
#  夹具
# ══════════════════════════════════════════════════════════════════

def _make_params(**overrides):
    """构造 BacktestParams，关闭 BTC 过滤等避免外部依赖"""
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
        trail_retrace_ratio=0.4,
        max_hold_bars=24,
        leverage=10,
        stake=50.0,
        slippage_pct=0.0,
        fee_pct=0.0,
        funding_rate_pct=0.0,  # 测试默认关闭，避免每根 bar 自动扣
        # 事件驱动引擎默认参数
        use_event_driven_engine=True,
        account_balance=1000.0,
        compound_enabled=False,
        btc_filter_enabled=False,
        max_daily_loss=10000.0,
        max_daily_trades=999,
        consecutive_loss_pause=999,
        max_position_pct=1.0,
        cooldown_hours=0,
    )
    defaults.update(overrides)
    return BacktestParams(**defaults)


def _kline(time_str, o, h, l, c, v=1_000_000):
    return {'time': time_str, 'open': o, 'high': h, 'low': l, 'close': c, 'volume': v}


def _flat_klines(n=60, start='2025-01-01T00:00:00+00:00', price=1.0):
    """生成 n 根平稳 K 线（价格几乎不变）"""
    klines = []
    base_dt = datetime.fromisoformat(start)
    for i in range(n):
        t = (base_dt + timedelta(hours=i)).isoformat()
        klines.append(_kline(t, price, price + 0.001, price - 0.001, price))
    return klines


def _drop_klines(n=60, entry_price=1.0, drop_per_bar=0.012):
    """生成持续下跌 K 线（用于触发 TP1+TP2 做空盈利）"""
    klines = []
    base_dt = datetime.fromisoformat('2025-01-01T00:00:00+00:00')
    price = entry_price
    for i in range(n):
        t = (base_dt + timedelta(hours=i)).isoformat()
        op = price
        cl = price - drop_per_bar
        hi = op + 0.0005
        lo = cl - 0.002
        klines.append(_kline(t, op, hi, lo, cl))
        price = cl
    return klines


def _spike_up_klines(n=60, entry_price=1.0, jump_at=20, jump=0.05):
    """生成在某根 bar 突然上涨触发硬止损的 K 线"""
    klines = []
    base_dt = datetime.fromisoformat('2025-01-01T00:00:00+00:00')
    price = entry_price
    for i in range(n):
        t = (base_dt + timedelta(hours=i)).isoformat()
        op = price
        if i == jump_at:
            cl = price + jump
        else:
            cl = price + 0.001
        hi = max(op, cl) + 0.001
        lo = min(op, cl) - 0.001
        klines.append(_kline(t, op, hi, lo, cl))
        price = cl
    return klines


# ══════════════════════════════════════════════════════════════════
#  单元：基础工具
# ══════════════════════════════════════════════════════════════════

class TestIsoToMs:
    def test_round_trip(self):
        ms = _iso_to_ms('2025-01-01T00:00:00+00:00')
        assert ms == 1735689600000

    def test_naive_treated_as_utc(self):
        ms = _iso_to_ms('2025-01-01T00:00:00')
        assert ms == 1735689600000


# ══════════════════════════════════════════════════════════════════
#  单元：复利
# ══════════════════════════════════════════════════════════════════

class TestCompoundStake:
    def test_disabled_returns_base(self):
        p = _make_params(compound_enabled=False, stake=50)
        st = _BacktestState(initial_balance=100, realized_pnl=200)
        assert compute_compound_stake(st, p) == 50

    def test_loss_returns_base(self):
        p = _make_params(compound_enabled=True, stake=50)
        st = _BacktestState(initial_balance=100, realized_pnl=-50)
        assert compute_compound_stake(st, p) == 50

    def test_smooth_growth(self):
        """每多 50U 盈利 → +25U（线性平滑，非阶梯）"""
        p = _make_params(
            compound_enabled=True, stake=50,
            compound_step=50, compound_increase=25, compound_max_stake=300,
        )
        # +0 → 50；+50 → 75；+100 → 100
        for pnl, expected in [(0, 50), (50, 75), (100, 100), (200, 150)]:
            st = _BacktestState(initial_balance=100, realized_pnl=pnl)
            assert compute_compound_stake(st, p) == expected

    def test_capped_at_max(self):
        p = _make_params(
            compound_enabled=True, stake=50,
            compound_step=50, compound_increase=25, compound_max_stake=100,
        )
        st = _BacktestState(initial_balance=100, realized_pnl=10000)
        assert compute_compound_stake(st, p) == 100


# ══════════════════════════════════════════════════════════════════
#  单元：BTC 索引
# ══════════════════════════════════════════════════════════════════

class TestBtcIndex:
    def test_empty_index_returns_none(self):
        assert get_btc_24h_change_at({}, 1735689600000) is None

    def test_exact_match(self):
        idx = {1000: {'close': 100, 'pct_24h': -3.5}}
        assert get_btc_24h_change_at(idx, 1000) == -3.5

    def test_within_tolerance(self):
        idx = {1000: {'close': 100, 'pct_24h': -3.5}}
        # 30 分钟容差内
        result = get_btc_24h_change_at(idx, 1000 + 1_800_000)
        assert result == -3.5

    def test_outside_tolerance_returns_none(self):
        idx = {1000: {'close': 100, 'pct_24h': -3.5}}
        # 远超 1h 容差
        result = get_btc_24h_change_at(idx, 1000 + 10 * 3_600_000)
        assert result is None


# ══════════════════════════════════════════════════════════════════
#  单元：风控网关
# ══════════════════════════════════════════════════════════════════

class TestRiskGates:
    def test_pause_blocks(self):
        p = _make_params()
        st = _BacktestState(initial_balance=1000, paused_until_ms=10_000)
        ok, reason = _check_risk_gates(st, p, bar_time_ms=5_000, symbol='X', proposed_stake=50)
        assert ok is False
        assert reason == 'pause'

    def test_daily_loss_blocks(self):
        p = _make_params(max_daily_loss=30)
        st = _BacktestState(initial_balance=1000, daily_loss=30.0)
        ok, reason = _check_risk_gates(st, p, bar_time_ms=10_000, symbol='X', proposed_stake=50)
        assert ok is False
        assert reason == 'daily_loss_limit'

    def test_daily_trades_blocks(self):
        p = _make_params(max_daily_trades=3)
        st = _BacktestState(initial_balance=1000, daily_trades_opened=3)
        ok, reason = _check_risk_gates(st, p, bar_time_ms=10_000, symbol='X', proposed_stake=50)
        assert ok is False
        assert reason == 'daily_trades_limit'

    def test_cooldown_blocks(self):
        p = _make_params()
        st = _BacktestState(initial_balance=1000)
        st.cooldown_until_ms['X/USDT'] = 20_000
        ok, reason = _check_risk_gates(st, p, bar_time_ms=10_000, symbol='X/USDT', proposed_stake=50)
        assert ok is False
        assert reason == 'cooldown'

    def test_position_pct_blocks(self):
        # equity = 1000, max_pct = 0.5 → max_position = 500
        # 已持 400 + 新 200 > 500 → block
        p = _make_params(max_position_pct=0.5)
        st = _BacktestState(initial_balance=1000)
        st.open_trades = [
            _OpenTrade(symbol='A', entry_idx=0, entry_price=1.0, entry_time='',
                       stake=400, leverage=10, notional=4000,
                       tp1_price=0.95, tp2_price=0.9, hard_stop_price=1.03,
                       max_hold_bars=24, stake_remaining_ratio=1.0)
        ]
        ok, reason = _check_risk_gates(st, p, bar_time_ms=0, symbol='B', proposed_stake=200)
        assert ok is False
        assert reason == 'position_pct'

    def test_all_clear_allows(self):
        p = _make_params()
        st = _BacktestState(initial_balance=1000)
        ok, reason = _check_risk_gates(st, p, bar_time_ms=0, symbol='X', proposed_stake=50)
        assert ok is True


# ══════════════════════════════════════════════════════════════════
#  单元：单笔仓位演化（_step_open_trade）
# ══════════════════════════════════════════════════════════════════

class TestStepOpenTrade:
    def _open(self, entry=100, **kw):
        defaults = dict(
            symbol='X', entry_idx=0, entry_price=entry, entry_time='',
            stake=50, leverage=10, notional=500,
            tp1_price=entry * 0.95, tp2_price=entry * 0.90,
            hard_stop_price=entry * 1.03, max_hold_bars=24,
        )
        defaults.update(kw)
        return _OpenTrade(**defaults)

    def test_no_trigger_returns_none(self):
        ot = self._open(entry=100)
        bar = _kline('t', 100, 100.5, 99.5, 100)
        p = _make_params()
        assert _step_open_trade(ot, bar, p) is None
        assert ot.bars_held == 1

    def test_hard_stop_triggers(self):
        ot = self._open(entry=100)  # hard_stop=103
        bar = _kline('t', 102, 105, 101, 104)
        p = _make_params()
        result = _step_open_trade(ot, bar, p)
        assert result is not None
        assert result[1] == 'hard_stop'

    def test_tp1_then_tp2_in_two_bars(self):
        ot = self._open(entry=100)
        # bar1：探到 95（TP1 命中），收 96
        bar1 = _kline('t1', 99, 99.5, 95, 96)
        p = _make_params()
        r1 = _step_open_trade(ot, bar1, p)
        assert r1 is None
        assert ot.tp1_triggered is True
        assert ot.stake_remaining_ratio == 0.5
        # bar2：探到 89（TP2 命中）
        bar2 = _kline('t2', 96, 96.2, 89, 90)
        r2 = _step_open_trade(ot, bar2, p)
        assert r2 is not None
        assert r2[1] == 'tp2'

    def test_time_stop_after_max_bars(self):
        ot = self._open(entry=100, max_hold_bars=2)
        p = _make_params()
        # bar1 推进 → bars_held=1
        _step_open_trade(ot, _kline('t1', 100, 100.5, 99.5, 100), p)
        # bar2 推进 → bars_held=2 → 时间止损
        result = _step_open_trade(ot, _kline('t2', 100, 100.5, 99.5, 100), p)
        assert result is not None
        assert result[1] == 'time_stop'


# ══════════════════════════════════════════════════════════════════
#  端到端：事件驱动主循环
# ══════════════════════════════════════════════════════════════════

class TestEventDrivenMainLoop:
    def test_no_signals_returns_empty(self):
        klines = _flat_klines(60)
        p = _make_params()
        trades, state = run_backtest_event_driven(
            klines, 'X/USDT', p, signals=[],
        )
        assert trades == []
        assert state.realized_pnl == 0

    def test_single_signal_full_round_trip(self):
        """一个信号 → drop 行情 → TP1+TP2 盈利"""
        klines = _drop_klines(40, entry_price=1.0)
        p = _make_params(
            tp1_pct=2.0, tp2_pct=4.0, hard_stop_pct=3.0,
            max_hold_bars=10,
        )
        # 信号在 idx=0，下根 bar 开仓
        trades, state = run_backtest_event_driven(
            klines, 'X/USDT', p, signals=[0],
        )
        assert len(trades) == 1
        t = trades[0]
        assert t.symbol == 'X/USDT'
        assert t.tp1_hit is True
        assert t.exit_reason == 'tp2'
        assert t.pnl_usd > 0
        assert state.realized_pnl > 0

    def test_btc_filter_blocks_signal(self):
        """BTC 暴跌时新信号被跳过"""
        klines = _drop_klines(40)
        p = _make_params(
            btc_filter_enabled=True,
            btc_crash_threshold=-5.0,
            tp1_pct=2.0, tp2_pct=4.0,
        )
        # 构造 BTC 索引：idx=0 对应 bar_time_ms 那刻 BTC -10%
        bar0_ms = _iso_to_ms(klines[0]['time'])
        btc_index = {bar0_ms: {'close': 60000, 'pct_24h': -10.0}}
        trades, state = run_backtest_event_driven(
            klines, 'X/USDT', p, signals=[0], btc_index=btc_index,
        )
        assert trades == []
        assert state.skip_counters.get('btc_filter') == 1

    def test_btc_filter_allows_when_calm(self):
        """BTC 平稳时不阻止"""
        klines = _drop_klines(40)
        p = _make_params(
            btc_filter_enabled=True,
            btc_crash_threshold=-5.0,
            tp1_pct=2.0, tp2_pct=4.0, max_hold_bars=10,
        )
        bar0_ms = _iso_to_ms(klines[0]['time'])
        btc_index = {bar0_ms: {'close': 60000, 'pct_24h': 1.5}}  # 涨 1.5%
        trades, state = run_backtest_event_driven(
            klines, 'X/USDT', p, signals=[0], btc_index=btc_index,
        )
        assert len(trades) == 1

    def test_compound_growth_uses_higher_stake_on_second_signal(self):
        """两个信号相隔较远；第一笔盈利后第二笔的 stake 应更大（复利）"""
        # 制造两段独立的下跌：第一段 0~12 bar，第二段 14 bar 开始
        klines = []
        base_dt = datetime.fromisoformat('2025-01-01T00:00:00+00:00')
        # 第一段：从 1.0 跌到 0.85（触发 TP1 + TP2 = 大幅盈利）
        price = 1.0
        for i in range(13):
            t = (base_dt + timedelta(hours=i)).isoformat()
            klines.append(_kline(t, price, price + 0.001, price - 0.020, price - 0.013))
            price -= 0.013
        # 中间稳定一段（让第一笔交易完整出场）
        for i in range(13, 20):
            t = (base_dt + timedelta(hours=i)).isoformat()
            klines.append(_kline(t, price, price + 0.001, price - 0.001, price))
        # 第二段：从 idx=20 开始，再来一段下跌
        for i in range(20, 35):
            t = (base_dt + timedelta(hours=i)).isoformat()
            klines.append(_kline(t, price, price + 0.001, price - 0.020, price - 0.013))
            price -= 0.013

        p = _make_params(
            compound_enabled=True,
            stake=50,
            compound_step=10,    # 每盈利 10U → +25U
            compound_increase=25,
            compound_max_stake=300,
            tp1_pct=2.0, tp2_pct=4.0, hard_stop_pct=10.0,
            max_hold_bars=12,
            account_balance=1000,
            max_daily_trades=999,  # 不被风控限频干扰
        )
        # 信号 idx=0 + idx=20
        trades, state = run_backtest_event_driven(
            klines, 'X/USDT', p, signals=[0, 20],
        )
        assert len(trades) == 2
        # 第一笔盈利
        assert trades[0].pnl_usd > 0
        # 第二笔的 stake 在 _OpenTrade 创建时被复利逻辑放大了
        # （我们没法直接读 stake，但 notional 隐含 stake × leverage）
        first_notional = trades[0].pnl_usd / max(trades[0].pnl_pct / 100, 1e-6)
        # 第二笔的 notional 应当 > 第一笔（因复利）
        # （间接验证：notional ratio = pnl_usd / (pnl_pct/100)）
        if abs(trades[1].pnl_pct) > 0.5:
            second_notional = abs(trades[1].pnl_usd / (trades[1].pnl_pct / 100))
            assert second_notional > first_notional * 1.1, (
                f"第二笔 notional({second_notional}) 应明显 > 第一笔({first_notional})，"
                "复利未生效"
            )

    def test_daily_trades_limit_blocks(self):
        """daily_trades_opened 达上限后同日新信号被跳过"""
        klines = _flat_klines(30)
        # 三个相隔很近的信号都在同一天
        p = _make_params(
            max_daily_trades=2,
            tp1_pct=10.0, tp2_pct=20.0, hard_stop_pct=20.0,
            max_hold_bars=2,  # 快速结束让风控连续累计
        )
        trades, state = run_backtest_event_driven(
            klines, 'X/USDT', p, signals=[0, 1, 2],
        )
        # 只有前 2 个被开仓
        assert state.skip_counters.get('daily_trades_limit', 0) >= 1
        assert len(trades) <= 2

    def test_consecutive_loss_pause_kicks_in(self):
        """连续 N 次亏损 → paused_until 被设置 → 后续信号被跳过"""
        # 持续上涨 → 做空连续硬止损
        klines = _spike_up_klines(60, jump_at=2, jump=0.05)
        # 后面再来一个jump 让多笔交易硬止损
        klines2 = []
        base_dt = datetime.fromisoformat('2025-01-01T00:00:00+00:00')
        price = 1.0
        for i in range(60):
            t = (base_dt + timedelta(hours=i)).isoformat()
            op = price
            cl = price + 0.05  # 每根 +5% → 做空必硬止损
            klines2.append(_kline(t, op, cl + 0.001, op - 0.001, cl))
            price = cl

        p = _make_params(
            consecutive_loss_pause=2,
            pause_hours=24,
            max_daily_trades=999,
            cooldown_hours=0,
            tp1_pct=10.0, tp2_pct=20.0,
            hard_stop_pct=3.0,
            max_hold_bars=2,
        )
        # 5 个信号，间隔 2 根（满足 max_hold_bars=2 的间隔要求）
        signals = [0, 3, 6, 9, 12]
        trades, state = run_backtest_event_driven(
            klines2, 'X/USDT', p, signals=signals,
        )
        # 前两笔亏损 → 触发暂停 → 第3+信号被 pause 跳过
        assert any(t.pnl_usd < 0 for t in trades)
        assert state.skip_counters.get('pause', 0) > 0

    def test_funding_cost_still_deducted(self):
        """资金费率仍在事件驱动引擎中扣除（与旧版同算法）"""
        klines = _drop_klines(40)
        # 完全相同的环境，只改 funding_rate_pct
        p_zero = _make_params(funding_rate_pct=0.0,
                              tp1_pct=2.0, tp2_pct=4.0, max_hold_bars=10)
        p_high = _make_params(funding_rate_pct=0.5,  # 极高费率，明显扣
                              tp1_pct=2.0, tp2_pct=4.0, max_hold_bars=10)
        trades_zero, _ = run_backtest_event_driven(klines, 'X', p_zero, signals=[0])
        trades_high, _ = run_backtest_event_driven(klines, 'X', p_high, signals=[0])
        assert len(trades_zero) == len(trades_high) == 1
        # high funding 的盈利应明显更低
        assert trades_high[0].pnl_usd < trades_zero[0].pnl_usd

    def test_window_filter_applies(self):
        """window_from_idx / window_to_idx 控制信号过滤"""
        klines = _drop_klines(40)
        p = _make_params(tp1_pct=2.0, tp2_pct=4.0, max_hold_bars=10)
        # 信号在 idx=5，但窗口是 [10, 20)
        trades, state = run_backtest_event_driven(
            klines, 'X', p, signals=[5],
            window_from_idx=10, window_to_idx=20,
        )
        assert trades == []  # idx=5 不在窗口内


# ══════════════════════════════════════════════════════════════════
#  对照：legacy 引擎与事件驱动引擎在"关闭所有动态特性"时数值口径一致
# ══════════════════════════════════════════════════════════════════

class TestLegacyVsEventDrivenAlignment:
    def test_aligned_when_dynamic_features_disabled(self):
        """关闭复利/BTC过滤/风控限频/funding 时，两个引擎应得出非常接近的总盈亏"""
        from backtest import run_backtest, _legacy_run_backtest, calculate_stats

        klines = _drop_klines(80, entry_price=1.0, drop_per_bar=0.012)

        p_event = _make_params(
            use_event_driven_engine=True,
            compound_enabled=False,
            btc_filter_enabled=False,
            funding_rate_pct=0.0,
            max_daily_trades=999,
            max_daily_loss=999999,
            consecutive_loss_pause=999,
            cooldown_hours=0,
            max_position_pct=10.0,  # 不限制持仓
            tp1_pct=2.0, tp2_pct=4.0, max_hold_bars=10,
        )
        p_legacy = _make_params(
            use_event_driven_engine=False,
            funding_rate_pct=0.0,
            tp1_pct=2.0, tp2_pct=4.0, max_hold_bars=10,
        )

        trades_event, _ = run_backtest_event_driven(
            klines, 'X', p_event, signals=[0],
        )
        trades_legacy = _legacy_run_backtest(
            klines, 'X', p_legacy, [0],
        )

        assert len(trades_event) == len(trades_legacy) == 1
        # PnL 应一致（精确到分）
        assert abs(trades_event[0].pnl_usd - trades_legacy[0].pnl_usd) < 0.01
        assert trades_event[0].exit_reason == trades_legacy[0].exit_reason
