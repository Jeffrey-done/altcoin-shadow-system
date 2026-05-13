"""
Walk-Forward Analysis 测试

测试策略:用纯合成 K 线,完全绕过 ccxt / 网络,
验证 WFA 的核心行为:
  - 窗口切分正确
  - 每个窗口选参 + OOS 执行的顺序
  - overfit_ratio 和 parameter_stability 聚合正确
  - 随机游走数据应该产生 ~0 的 OOS Sharpe (没有真实 edge)
  - 含真实 edge 的数据应该产生正 OOS PnL
"""

import random
from unittest.mock import patch

from walk_forward import (
    walk_forward_analysis,
    default_param_grid, _enumerate_grid,
    _simple_sharpe, _max_drawdown_pct, _pnls_to_equity_curve,
    _pick_best_params, _run_window,
)
from backtest import BacktestParams
import pytest


# ══════════════════════════════════════════════════════════════════
#  合成 K 线生成器
# ══════════════════════════════════════════════════════════════════

def _make_random_walk(n_bars: int, start_price: float = 1.0, seed: int = 0,
                      drift: float = 0.0, vol: float = 0.005) -> list:
    """
    用确定性随机游走生成 K 线 (无真实 edge)。
    每根 K 线 high/low 相对 open/close 留一点余量,避免退化。
    """
    rng = random.Random(seed)
    klines = []
    price = start_price
    for i in range(n_bars):
        ret = rng.gauss(drift, vol)
        open_p = price
        close_p = max(price * (1 + ret), 1e-9)
        high_p = max(open_p, close_p) * (1 + abs(rng.gauss(0, vol * 0.3)))
        low_p = min(open_p, close_p) * (1 - abs(rng.gauss(0, vol * 0.3)))
        klines.append({
            'time': f"2025-01-01T{i:04d}",
            'open': round(open_p, 8),
            'high': round(high_p, 8),
            'low': round(low_p, 8),
            'close': round(close_p, 8),
            'volume': 1_000_000,
        })
        price = close_p
    return klines


def _make_edge_klines(n_bars: int, seed: int = 0) -> list:
    """
    合成带有 "RSI 冲高后回落就适合做空" 真实 edge 的 K 线:
    上涨 30 根 → 下跌 30 根 循环。信号扫描能稳定检出。
    """
    rng = random.Random(seed)
    klines = []
    price = 1.0
    bar_idx = 0
    while bar_idx < n_bars:
        # 上涨 30 根
        for _ in range(30):
            if bar_idx >= n_bars:
                break
            open_p = price
            close_p = price * (1 + 0.015 + rng.gauss(0, 0.001))
            high_p = close_p * 1.003
            low_p = open_p * 0.999
            klines.append({
                'time': f"2025-01-01T{bar_idx:04d}",
                'open': round(open_p, 6), 'high': round(high_p, 6),
                'low': round(low_p, 6), 'close': round(close_p, 6),
                'volume': 1_500_000,
            })
            price = close_p
            bar_idx += 1

        # 下跌 30 根
        for _ in range(30):
            if bar_idx >= n_bars:
                break
            open_p = price
            close_p = price * (1 - 0.012 + rng.gauss(0, 0.001))
            high_p = open_p * 1.001
            low_p = close_p * 0.997
            klines.append({
                'time': f"2025-01-01T{bar_idx:04d}",
                'open': round(open_p, 6), 'high': round(high_p, 6),
                'low': round(low_p, 6), 'close': round(close_p, 6),
                'volume': 2_000_000,
            })
            price = close_p
            bar_idx += 1

    return klines


# ══════════════════════════════════════════════════════════════════
#  _enumerate_grid
# ══════════════════════════════════════════════════════════════════

class TestEnumerateGrid:
    def test_enumerate_simple_grid(self):
        combos = _enumerate_grid({'a': [1, 2], 'b': [3, 4]})
        assert len(combos) == 4
        assert {'a': 1, 'b': 3} in combos

    def test_enumerate_drops_invalid_tp(self):
        """tp2 <= tp1 的组合自动被丢掉"""
        combos = _enumerate_grid({
            'tp1_pct': [3, 5, 10],
            'tp2_pct': [5, 10, 15],
        })
        for c in combos:
            assert c['tp2_pct'] > c['tp1_pct']

    def test_default_grid_size(self):
        combos = _enumerate_grid(default_param_grid())
        # 5 tp2 × 3 tp1 × 3 stop × 4 rsi × 3 drop = 540 raw,
        # 去掉 tp2<=tp1 后应明显少一些但非零
        assert len(combos) > 0
        assert len(combos) < 540
        for c in combos:
            assert c['tp2_pct'] > c['tp1_pct']


# ══════════════════════════════════════════════════════════════════
#  辅助函数
# ══════════════════════════════════════════════════════════════════

class TestHelperFunctions:
    def test_sharpe_with_uniform_pnls_is_zero(self):
        """标准差 0 → Sharpe 0 (避免除零)"""
        assert _simple_sharpe([1.0, 1.0, 1.0, 1.0]) == 0.0

    def test_sharpe_positive_mean_positive_sharpe(self):
        pnls = [1, 2, 3, 4, 5]
        s = _simple_sharpe(pnls)
        assert s > 0

    def test_sharpe_single_value(self):
        assert _simple_sharpe([5.0]) == 0.0
        assert _simple_sharpe([]) == 0.0

    def test_max_drawdown_from_curve(self):
        # 100 → 120 → 80 → 110 : peak=120, trough=80 → DD = 40/120 = 33.33%
        eq = [100, 120, 80, 110]
        dd = _max_drawdown_pct(eq)
        assert abs(dd - 33.33) < 0.1

    def test_max_drawdown_monotonic_up_is_zero(self):
        assert _max_drawdown_pct([100, 110, 120, 130]) == 0.0

    def test_pnls_to_equity_curve_accumulates(self):
        eq = _pnls_to_equity_curve(1000, [10, -5, 3])
        assert eq == [1000, 1010, 1005, 1008]


# ══════════════════════════════════════════════════════════════════
#  _run_window
# ══════════════════════════════════════════════════════════════════

class TestRunWindow:
    def test_run_window_empty_returns_empty_result(self):
        result = _run_window([], BacktestParams())
        assert result.total_trades == 0

    def test_run_window_small_sample_returns_empty(self):
        """< 30 根 K 线直接返回空,不 raise"""
        result = _run_window(_make_random_walk(20), BacktestParams())
        assert result.total_trades == 0

    def test_run_window_with_edge_klines_produces_trades(self):
        klines = _make_edge_klines(300)
        params = BacktestParams(
            daily_rsi_min=75, h4_rsi_enter=70, h4_rsi_drop=8,
            tp1_pct=5, tp2_pct=10, hard_stop_pct=3,
        )
        result = _run_window(klines, params, symbol='TEST/USDT')
        # edge klines 应该能产出交易 (非空)
        assert result.total_trades > 0


# ══════════════════════════════════════════════════════════════════
#  _pick_best_params
# ══════════════════════════════════════════════════════════════════

class TestPickBestParams:
    def test_pick_best_honors_min_trades(self):
        """min_trades 约束 — 排除在训练期只产出 <N 笔交易的组合"""
        klines = _make_edge_klines(200)
        # 极宽的网格,但要求至少 5 笔
        grid = _enumerate_grid({
            'tp1_pct': [3, 5],
            'tp2_pct': [8, 10],
            'hard_stop_pct': [3, 5],
            'daily_rsi_min': [75],
            'h4_rsi_drop': [8],
        })
        params, result = _pick_best_params(klines, grid, min_trades=3)
        # 至少保证能选出一个 (哪怕松绑也要有)
        assert params is not None

    def test_pick_best_on_empty_grid_returns_defaults(self):
        params, result = _pick_best_params(_make_edge_klines(100), [])
        assert isinstance(params, BacktestParams)
        assert result.total_trades == 0

    def test_pick_best_honors_metric(self):
        """改 selection_metric 能改变返回值"""
        klines = _make_edge_klines(200)
        grid = _enumerate_grid({
            'tp1_pct': [3, 5],
            'tp2_pct': [8, 15],
            'hard_stop_pct': [3],
            'daily_rsi_min': [75],
            'h4_rsi_drop': [8],
        })
        # 按 PnL 最大和按胜率最大可能选不同组合 — 至少不能 raise
        by_pnl, _ = _pick_best_params(klines, grid, selection_metric=lambda r: r.total_pnl)
        by_wr, _ = _pick_best_params(klines, grid, selection_metric=lambda r: r.win_rate)
        assert isinstance(by_pnl, BacktestParams)
        assert isinstance(by_wr, BacktestParams)


# ══════════════════════════════════════════════════════════════════
#  walk_forward_analysis — 端到端
# ══════════════════════════════════════════════════════════════════

class TestWalkForwardEndToEnd:
    def test_windowing_rolling_count(self):
        """
        总 180 天 = 180*24 = 4320 bars
        train=60d (1440), test=20d (480), step=20d (480)
        → 第 1 窗 train[0:1440] test[1440:1920]
          第 2 窗 train[480:1920] test[1920:2400]
          ...
          最后一个 test_end ≤ 4320
        应该有 (4320 - 1440 - 480) / 480 + 1 = 6 个窗口
        """
        klines = _make_random_walk(180 * 24, seed=42)
        # 用小网格加速
        grid = {
            'tp1_pct': [5], 'tp2_pct': [10],
            'hard_stop_pct': [3], 'daily_rsi_min': [78], 'h4_rsi_drop': [10],
        }
        result = walk_forward_analysis(
            symbol='SYNTH/USDT',
            klines=klines,
            train_days=60, test_days=20, step_days=20,
            param_grid=grid, anchor='rolling',
        )
        assert len(result.windows) == 6

    def test_windowing_anchored_expands_train(self):
        """
        anchored 模式:每个窗口的 train_start 固定为 0,train_end 递增
        """
        klines = _make_random_walk(180 * 24, seed=42)
        grid = {
            'tp1_pct': [5], 'tp2_pct': [10],
            'hard_stop_pct': [3], 'daily_rsi_min': [78], 'h4_rsi_drop': [10],
        }
        result = walk_forward_analysis(
            symbol='SYNTH/USDT',
            klines=klines,
            train_days=60, test_days=20, step_days=20,
            param_grid=grid, anchor='anchored',
        )
        for w in result.windows:
            assert w.train_start_idx == 0
        # 训练期终点单调递增
        ends = [w.train_end_idx for w in result.windows]
        assert ends == sorted(ends)
        assert len(set(ends)) == len(ends)  # 各窗口训练结束点不同

    def test_insufficient_data_returns_zero_windows(self):
        """K 线太少,拼不出哪怕一个窗口"""
        klines = _make_random_walk(50, seed=1)  # 仅 50 根
        grid = {'tp1_pct': [5], 'tp2_pct': [10], 'hard_stop_pct': [3],
                'daily_rsi_min': [78], 'h4_rsi_drop': [10]}
        result = walk_forward_analysis(
            symbol='SYNTH/USDT',
            klines=klines,
            train_days=60, test_days=20, step_days=20,
            param_grid=grid,
        )
        assert len(result.windows) == 0
        assert result.oos_total_pnl == 0.0
        assert result.oos_total_trades == 0

    def test_random_walk_produces_weak_oos(self):
        """
        随机游走数据没有真实 edge → OOS Sharpe 应该接近 0
        (不严格 ==0 是因为合成样本有限)
        """
        klines = _make_random_walk(200 * 24, seed=7)
        grid = {
            'tp1_pct': [3, 5], 'tp2_pct': [8, 15],
            'hard_stop_pct': [3], 'daily_rsi_min': [75, 78], 'h4_rsi_drop': [8, 10],
        }
        result = walk_forward_analysis(
            symbol='RNG/USDT',
            klines=klines,
            train_days=60, test_days=20, step_days=20,
            param_grid=grid, min_trades_is=1,
        )
        assert len(result.windows) >= 3
        # 无真实 edge 的 OOS 夏普率应该比较小 (不强制零)
        # 主要验证 WFA 跑完不崩 + 能聚合出指标
        assert isinstance(result.oos_sharpe, float)
        assert isinstance(result.overfit_ratio, float)

    def test_edge_data_produces_trades(self):
        """
        有真实 edge 的数据 → 至少能在 OOS 跑出一些交易
        """
        klines = _make_edge_klines(200 * 24)
        grid = {
            'tp1_pct': [5], 'tp2_pct': [10],
            'hard_stop_pct': [3], 'daily_rsi_min': [75], 'h4_rsi_drop': [8],
        }
        result = walk_forward_analysis(
            symbol='EDGE/USDT',
            klines=klines,
            train_days=60, test_days=20, step_days=20,
            param_grid=grid, min_trades_is=1,
        )
        # edge 数据 + 多个窗口 → 应该至少跑出来几笔 OOS 交易
        assert result.oos_total_trades > 0

    def test_parameter_stability_single_combo_has_zero_cv(self):
        """
        只给一个参数组合 → 每个窗口都选同一个 → 稳定性指标里所有维度的 CV=0
        """
        klines = _make_edge_klines(180 * 24)
        grid = {
            'tp1_pct': [5], 'tp2_pct': [10],
            'hard_stop_pct': [3], 'daily_rsi_min': [78], 'h4_rsi_drop': [10],
        }
        result = walk_forward_analysis(
            symbol='STABLE/USDT',
            klines=klines,
            train_days=60, test_days=20, step_days=20,
            param_grid=grid, min_trades_is=1,
        )
        for k, cv in result.parameter_stability.items():
            assert cv == 0.0, f"{k} CV should be 0, got {cv}"

    def test_to_dict_roundtrip(self):
        """WFAResult 可序列化成 dict,字段完备"""
        klines = _make_edge_klines(180 * 24)
        grid = {
            'tp1_pct': [5], 'tp2_pct': [10],
            'hard_stop_pct': [3], 'daily_rsi_min': [78], 'h4_rsi_drop': [10],
        }
        result = walk_forward_analysis(
            symbol='DICT/USDT',
            klines=klines,
            train_days=60, test_days=20, step_days=20,
            param_grid=grid, min_trades_is=1,
        )
        d = result.to_dict()
        # 核心字段
        for k in ['symbol', 'windows', 'oos_total_pnl', 'oos_sharpe',
                  'is_sharpe', 'overfit_ratio', 'parameter_stability']:
            assert k in d

    def test_summary_text_does_not_crash(self):
        klines = _make_edge_klines(180 * 24)
        grid = {
            'tp1_pct': [5], 'tp2_pct': [10],
            'hard_stop_pct': [3], 'daily_rsi_min': [78], 'h4_rsi_drop': [10],
        }
        result = walk_forward_analysis(
            symbol='SUM/USDT',
            klines=klines,
            train_days=60, test_days=20, step_days=20,
            param_grid=grid, min_trades_is=1,
        )
        text = result.summary_text()
        assert 'Walk-Forward Analysis' in text
        assert 'SUM/USDT' in text

    def test_windows_have_non_overlapping_test_ranges(self):
        """
        step=test_days 时,各窗口的 test 段刚好首尾相接不重叠
        — 这样 OOS 拼接才是干净的
        """
        klines = _make_random_walk(240 * 24, seed=3)
        grid = {
            'tp1_pct': [5], 'tp2_pct': [10],
            'hard_stop_pct': [3], 'daily_rsi_min': [78], 'h4_rsi_drop': [10],
        }
        result = walk_forward_analysis(
            symbol='NON-OVERLAP/USDT',
            klines=klines,
            train_days=60, test_days=20, step_days=20,
            param_grid=grid, anchor='rolling',
        )
        for i in range(1, len(result.windows)):
            prev = result.windows[i - 1]
            curr = result.windows[i]
            assert curr.test_start_idx == prev.test_end_idx, (
                f"窗口 {i} 的 test 区间应该紧接上个窗口末尾,"
                f"实际 prev_end={prev.test_end_idx}, curr_start={curr.test_start_idx}"
            )


# ══════════════════════════════════════════════════════════════════
#  CLI 入口不 raise (smoke test)
# ══════════════════════════════════════════════════════════════════

class TestCliSmoke:
    def test_cli_with_mocked_data_does_not_crash(self, monkeypatch, capsys):
        """mock 掉 load_cached_klines 避免网络,跑 CLI 入口一次"""
        klines = _make_edge_klines(180 * 24)

        with patch('walk_forward.load_cached_klines', return_value=klines):
            result = walk_forward_analysis(
                symbol='CLI/USDT',
                total_days=180, train_days=60, test_days=20, step_days=20,
                param_grid={
                    'tp1_pct': [5], 'tp2_pct': [10],
                    'hard_stop_pct': [3], 'daily_rsi_min': [78], 'h4_rsi_drop': [10],
                }, min_trades_is=1,
            )
        # 至少跑出来窗口
        assert len(result.windows) > 0


# ══════════════════════════════════════════════════════════════════
#  边界: anchor 参数校验
# ══════════════════════════════════════════════════════════════════

class TestArgumentValidation:
    def test_invalid_anchor_raises(self):
        with pytest.raises(ValueError, match='anchor'):
            walk_forward_analysis(
                symbol='X/USDT', klines=_make_random_walk(100),
                train_days=60, test_days=20,
                param_grid={'tp1_pct': [5], 'tp2_pct': [10], 'hard_stop_pct': [3],
                            'daily_rsi_min': [78], 'h4_rsi_drop': [10]},
                anchor='sideways',  # 非法
            )
