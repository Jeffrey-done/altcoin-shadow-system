#!/usr/bin/env python3
"""
Walk-forward Validation 框架 v1.0

解决过拟合问题：在训练窗口优化参数，在未见过的测试窗口验证表现。
只有测试窗口的指标才代表策略的"真实"能力。

滚动窗口模式：
  |===Train 60天===|==Test 30天==|
            |===Train 60天===|==Test 30天==|
                      |===Train 60天===|==Test 30天==|

Anchored 模式：
  |=======Train（累积）=======|==Test 30天==|
  |==========Train（累积）=========|==Test 30天==|

核心指标：
  - OOS Sharpe Ratio: 样本外夏普比率
  - OOS vs IS Degradation: 样本外相对样本内的衰减比
  - Parameter Stability: 各窗口最优参数的稳定性（标准差）
  - Walk-forward Efficiency: OOS总收益 / IS总收益

用法：
  from backtesting.walk_forward import WalkForwardValidator, WalkForwardConfig

  wf = WalkForwardValidator(
      ohlcv_data=df,
      config=WalkForwardConfig(
          train_days=60,
          test_days=30,
          step_days=30,
      ),
  )
  report = wf.run()
  print(report.summary)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("backtesting.walk_forward")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class WalkForwardConfig:
    """Walk-forward 配置"""
    # 窗口
    train_days: int = 60                 # 训练窗口天数
    test_days: int = 30                  # 测试窗口天数
    step_days: int = 30                  # 滚动步长（= test_days 即无重叠）
    min_train_trades: int = 5            # 训练窗口最少交易数才视为有效

    # 模式
    anchored: bool = False               # True=累积训练窗口, False=固定窗口滚动
    timeframe: str = '1h'                # 数据时间框架

    # 优化
    optimization_target: str = 'sharpe_ratio'  # 优化目标
    n_optimization_trials: int = 50      # 每个窗口的优化次数
    use_optuna: bool = False             # True=Optuna, False=网格搜索

    # 参数搜索空间
    param_grid: Optional[Dict[str, List]] = None    # 网格搜索用
    param_ranges: Optional[Dict[str, Tuple]] = None  # Optuna 用

    # 回测
    initial_capital: float = 1000.0
    fee_pct: float = 0.04
    max_open_trades: int = 3

    # 并行
    n_workers: int = 0                   # 0=自动


# ══════════════════════════════════════════════════════════════════
#  结果
# ══════════════════════════════════════════════════════════════════

@dataclass
class WindowResult:
    """单个窗口的结果"""
    window_index: int = 0
    train_start: str = ''
    train_end: str = ''
    test_start: str = ''
    test_end: str = ''
    train_bars: int = 0
    test_bars: int = 0

    # 训练阶段（In-Sample）
    is_best_params: Dict[str, Any] = field(default_factory=dict)
    is_sharpe: float = 0.0
    is_total_pnl: float = 0.0
    is_win_rate: float = 0.0
    is_total_trades: int = 0
    is_max_drawdown: float = 0.0

    # 测试阶段（Out-of-Sample）
    oos_sharpe: float = 0.0
    oos_total_pnl: float = 0.0
    oos_win_rate: float = 0.0
    oos_total_trades: int = 0
    oos_max_drawdown: float = 0.0
    oos_profit_factor: float = 0.0

    # 衰减
    sharpe_degradation: float = 0.0      # (IS - OOS) / IS, 正数=衰减


@dataclass
class WalkForwardReport:
    """Walk-forward 验证报告"""
    # 总览
    total_windows: int = 0
    valid_windows: int = 0                # 有足够交易的窗口数
    total_elapsed_sec: float = 0.0

    # OOS 汇总
    oos_sharpe_mean: float = 0.0
    oos_sharpe_std: float = 0.0
    oos_total_pnl: float = 0.0           # 所有 OOS 窗口的累计 PnL
    oos_win_rate_mean: float = 0.0
    oos_max_drawdown_worst: float = 0.0
    oos_profit_factor_mean: float = 0.0

    # IS vs OOS 对比
    is_sharpe_mean: float = 0.0
    sharpe_degradation_mean: float = 0.0  # 平均衰减率
    walk_forward_efficiency: float = 0.0  # OOS总PnL / IS总PnL

    # 参数稳定性
    param_stability: Dict[str, float] = field(default_factory=dict)
    # 每个参数的 CV (变异系数) — 越小越稳定

    # 最终推荐参数（所有窗口 OOS 表现最好的参数的加权平均）
    recommended_params: Dict[str, Any] = field(default_factory=dict)

    # 详细窗口结果
    windows: List[WindowResult] = field(default_factory=list)

    # 配置
    config: Optional[WalkForwardConfig] = None

    @property
    def summary(self) -> str:
        """人类可读摘要"""
        grade = 'A' if self.walk_forward_efficiency > 0.7 else \
                'B' if self.walk_forward_efficiency > 0.5 else \
                'C' if self.walk_forward_efficiency > 0.3 else 'D'

        return (
            f"Walk-Forward 验证报告\n"
            f"{'═'*60}\n"
            f"窗口: {self.valid_windows}/{self.total_windows} 有效  "
            f"耗时: {self.total_elapsed_sec:.1f}s\n"
            f"{'─'*60}\n"
            f"OOS Sharpe:  均值={self.oos_sharpe_mean:.2f} ± {self.oos_sharpe_std:.2f}\n"
            f"OOS 累计PnL: {self.oos_total_pnl:.1f}U  "
            f"胜率: {self.oos_win_rate_mean:.1f}%\n"
            f"OOS 最大回撤: {self.oos_max_drawdown_worst:.1f}%\n"
            f"{'─'*60}\n"
            f"IS Sharpe:   均值={self.is_sharpe_mean:.2f}\n"
            f"Sharpe 衰减: {self.sharpe_degradation_mean*100:.1f}%\n"
            f"WF 效率:     {self.walk_forward_efficiency:.2f} (Grade {grade})\n"
            f"{'─'*60}\n"
            f"参数稳定性 (CV): {self._format_stability()}\n"
            f"推荐参数: {self.recommended_params}\n"
        )

    def _format_stability(self) -> str:
        if not self.param_stability:
            return "N/A"
        parts = [f"{k}={v:.2f}" for k, v in self.param_stability.items()]
        return ", ".join(parts)


# ══════════════════════════════════════════════════════════════════
#  Walk-forward 验证器
# ══════════════════════════════════════════════════════════════════

class WalkForwardValidator:
    """
    Walk-forward 验证器。

    核心流程：
      1. 将数据切分为多个 Train/Test 窗口
      2. 每个 Train 窗口内做参数优化
      3. 用 Train 窗口得到的最优参数在 Test 窗口回测
      4. 只收集 Test 窗口的指标作为策略真实表现
      5. 分析参数稳定性和 IS→OOS 衰减
    """

    def __init__(
        self,
        ohlcv_data: pd.DataFrame,
        symbol: str = '',
        config: Optional[WalkForwardConfig] = None,
    ):
        self.df = ohlcv_data.copy()
        self.symbol = symbol
        self.config = config or WalkForwardConfig()

        # 计算每天对应的 bar 数
        if self.config.timeframe == '1h':
            self._bars_per_day = 24
        elif self.config.timeframe == '4h':
            self._bars_per_day = 6
        else:
            self._bars_per_day = 24

    def run(self) -> WalkForwardReport:
        """执行 Walk-forward 验证"""
        t0 = time.monotonic()
        cfg = self.config
        report = WalkForwardReport(config=cfg)

        # 生成窗口
        windows = self._generate_windows()
        report.total_windows = len(windows)

        if not windows:
            logger.warning("数据不足，无法生成任何 Walk-forward 窗口")
            return report

        logger.info(
            f"[WalkForward] {self.symbol} 生成 {len(windows)} 个窗口 "
            f"(train={cfg.train_days}d, test={cfg.test_days}d, step={cfg.step_days}d)"
        )

        # 逐窗口执行
        all_params = []  # 收集各窗口最优参数

        for i, (train_slice, test_slice) in enumerate(windows):
            window_result = self._process_window(i, train_slice, test_slice)
            report.windows.append(window_result)

            if window_result.oos_total_trades >= 1:
                report.valid_windows += 1
                all_params.append(window_result.is_best_params)

            logger.info(
                f"  窗口 {i+1}/{len(windows)}: "
                f"IS_Sharpe={window_result.is_sharpe:.2f} → "
                f"OOS_Sharpe={window_result.oos_sharpe:.2f} "
                f"(trades={window_result.oos_total_trades}, "
                f"pnl={window_result.oos_total_pnl:+.1f}U)"
            )

        # 汇总计算
        self._compute_report_summary(report, all_params)
        report.total_elapsed_sec = round(time.monotonic() - t0, 2)

        return report

    # ── 窗口生成 ─────────────────────────────────────────────────

    def _generate_windows(self) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
        """
        生成 Train/Test 窗口切片。

        返回: [(train_slice, test_slice), ...]
              其中 slice = (start_bar, end_bar)
        """
        cfg = self.config
        total_bars = len(self.df)
        train_bars = cfg.train_days * self._bars_per_day
        test_bars = cfg.test_days * self._bars_per_day
        step_bars = cfg.step_days * self._bars_per_day

        windows = []
        pos = 0

        while True:
            if cfg.anchored:
                train_start = 0
            else:
                train_start = pos

            train_end = pos + train_bars
            test_start = train_end
            test_end = test_start + test_bars

            if test_end > total_bars:
                break

            windows.append(
                ((train_start, train_end), (test_start, test_end))
            )
            pos += step_bars

        return windows

    # ── 单窗口处理 ───────────────────────────────────────────────

    def _process_window(
        self, index: int,
        train_slice: Tuple[int, int],
        test_slice: Tuple[int, int],
    ) -> WindowResult:
        """处理单个 Train/Test 窗口"""
        cfg = self.config
        train_start, train_end = train_slice
        test_start, test_end = test_slice

        result = WindowResult(
            window_index=index,
            train_bars=train_end - train_start,
            test_bars=test_end - test_start,
        )

        # 提取时间标签
        if 'timestamp' in self.df.columns:
            ts_col = self.df['timestamp']
            result.train_start = str(ts_col.iloc[train_start]) if train_start < len(ts_col) else ''
            result.train_end = str(ts_col.iloc[train_end - 1]) if train_end <= len(ts_col) else ''
            result.test_start = str(ts_col.iloc[test_start]) if test_start < len(ts_col) else ''
            result.test_end = str(ts_col.iloc[test_end - 1]) if test_end <= len(ts_col) else ''

        # 切分数据
        train_df = self.df.iloc[train_start:train_end].reset_index(drop=True)
        test_df = self.df.iloc[test_start:test_end].reset_index(drop=True)

        # ── 阶段 1: 训练窗口优化 ──
        best_params, is_metrics = self._optimize_on_train(train_df)
        result.is_best_params = best_params
        result.is_sharpe = is_metrics.get('sharpe_ratio', 0)
        result.is_total_pnl = is_metrics.get('total_pnl', 0)
        result.is_win_rate = is_metrics.get('win_rate', 0)
        result.is_total_trades = is_metrics.get('total_trades', 0)
        result.is_max_drawdown = is_metrics.get('max_drawdown_pct', 0)

        # ── 阶段 2: 测试窗口验证 ──
        oos_metrics = self._validate_on_test(test_df, best_params)
        result.oos_sharpe = oos_metrics.get('sharpe_ratio', 0)
        result.oos_total_pnl = oos_metrics.get('total_pnl', 0)
        result.oos_win_rate = oos_metrics.get('win_rate', 0)
        result.oos_total_trades = oos_metrics.get('total_trades', 0)
        result.oos_max_drawdown = oos_metrics.get('max_drawdown_pct', 0)
        result.oos_profit_factor = oos_metrics.get('profit_factor', 0)

        # 衰减计算
        if result.is_sharpe > 0:
            result.sharpe_degradation = (result.is_sharpe - result.oos_sharpe) / result.is_sharpe
        else:
            result.sharpe_degradation = 0.0

        return result

    def _optimize_on_train(self, train_df: pd.DataFrame) -> Tuple[Dict, Dict]:
        """在训练窗口进行参数优化"""
        cfg = self.config

        if cfg.use_optuna:
            return self._optuna_optimize(train_df)
        else:
            return self._grid_optimize(train_df)

    def _grid_optimize(self, df: pd.DataFrame) -> Tuple[Dict, Dict]:
        """网格搜索优化"""
        import itertools

        param_grid = self.config.param_grid or {
            'daily_rsi_min': [75, 78, 80, 82],
            'h4_rsi_drop': [8, 10, 12],
            'tp1_pct': [4, 5, 6],
            'hard_stop_pct': [4, 5, 6],
        }

        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(itertools.product(*values))

        best_score = -999
        best_params = {}
        best_metrics = {}

        target = self.config.optimization_target

        for combo in combinations:
            params = dict(zip(keys, combo))
            metrics = self._run_backtest(df, params)

            score = metrics.get(target, -999)
            # 至少要有交易才有效
            if metrics.get('total_trades', 0) >= self.config.min_train_trades and score > best_score:
                best_score = score
                best_params = params
                best_metrics = metrics

        return best_params, best_metrics

    def _optuna_optimize(self, df: pd.DataFrame) -> Tuple[Dict, Dict]:
        """Optuna 优化"""
        try:
            import optuna
            optuna.logging.set_verbosity(optuna.logging.WARNING)
        except ImportError:
            logger.warning("optuna 未安装，fallback 到网格搜索")
            return self._grid_optimize(df)

        cfg = self.config
        param_ranges = cfg.param_ranges or {
            'daily_rsi_min': (72, 85),
            'h4_rsi_drop': (6, 15),
            'tp1_pct': (3, 8),
            'hard_stop_pct': (3, 8),
        }

        best_metrics_holder = [{}]

        def objective(trial):
            params = {}
            for key, (low, high) in param_ranges.items():
                if isinstance(low, int) and isinstance(high, int):
                    params[key] = trial.suggest_int(key, low, high)
                else:
                    params[key] = trial.suggest_float(key, low, high, step=0.5)

            metrics = self._run_backtest(df, params)
            if metrics.get('total_trades', 0) < cfg.min_train_trades:
                return -999

            score = metrics.get(cfg.optimization_target, -999)
            if score > trial.study.best_value if trial.study.best_trial else True:
                best_metrics_holder[0] = metrics
            return score

        study = optuna.create_study(direction='maximize')
        study.optimize(objective, n_trials=cfg.n_optimization_trials, show_progress_bar=False)

        return study.best_params, best_metrics_holder[0]

    def _validate_on_test(self, test_df: pd.DataFrame, params: Dict) -> Dict:
        """在测试窗口用固定参数回测"""
        if not params:
            return {}
        return self._run_backtest(test_df, params)

    def _run_backtest(self, df: pd.DataFrame, params: Dict) -> Dict:
        """执行单次回测，返回指标字典"""
        try:
            from backtesting.engine import VectorizedBacktester, BacktestConfig
            from strategies.short_overbought import ShortOverboughtStrategy

            config = BacktestConfig(
                initial_capital=self.config.initial_capital,
                fee_pct=self.config.fee_pct,
                max_open_trades=self.config.max_open_trades,
                timeframe=self.config.timeframe,
            )

            strategy = ShortOverboughtStrategy()
            bt = VectorizedBacktester(
                strategy=strategy,
                ohlcv_data=df,
                config=config,
                symbol=self.symbol,
            )
            result = bt.run(params=params)

            return {
                'total_trades': result.metrics.total_trades,
                'win_rate': result.metrics.win_rate,
                'total_pnl': result.metrics.total_pnl,
                'sharpe_ratio': result.metrics.sharpe_ratio,
                'max_drawdown_pct': result.metrics.max_drawdown_pct,
                'profit_factor': result.metrics.profit_factor,
                'total_return_pct': result.metrics.total_return_pct,
            }
        except Exception as e:
            logger.debug(f"回测执行失败: {e}")
            return {}

    # ── 汇总计算 ─────────────────────────────────────────────────

    def _compute_report_summary(self, report: WalkForwardReport, all_params: List[Dict]):
        """计算报告汇总"""
        valid = [w for w in report.windows if w.oos_total_trades >= 1]

        if not valid:
            return

        # OOS 汇总
        oos_sharpes = [w.oos_sharpe for w in valid]
        report.oos_sharpe_mean = round(float(np.mean(oos_sharpes)), 2)
        report.oos_sharpe_std = round(float(np.std(oos_sharpes)), 2)
        report.oos_total_pnl = round(sum(w.oos_total_pnl for w in valid), 2)
        report.oos_win_rate_mean = round(float(np.mean([w.oos_win_rate for w in valid])), 1)
        report.oos_max_drawdown_worst = round(max(w.oos_max_drawdown for w in valid), 1)
        oos_pfs = [w.oos_profit_factor for w in valid if w.oos_profit_factor > 0]
        report.oos_profit_factor_mean = round(float(np.mean(oos_pfs)), 2) if oos_pfs else 0

        # IS 汇总
        is_sharpes = [w.is_sharpe for w in valid]
        report.is_sharpe_mean = round(float(np.mean(is_sharpes)), 2)

        # 衰减
        degradations = [w.sharpe_degradation for w in valid]
        report.sharpe_degradation_mean = round(float(np.mean(degradations)), 3)

        # Walk-forward 效率
        is_total_pnl = sum(w.is_total_pnl for w in valid)
        if is_total_pnl > 0:
            report.walk_forward_efficiency = round(report.oos_total_pnl / is_total_pnl, 3)

        # 参数稳定性（变异系数 = std / mean）
        if all_params:
            for key in all_params[0].keys():
                values = [p.get(key, 0) for p in all_params if key in p]
                if values and np.mean(values) != 0:
                    cv = float(np.std(values) / abs(np.mean(values)))
                    report.param_stability[key] = round(cv, 3)

            # 推荐参数：OOS PnL 加权平均
            weights = np.array([max(0.01, w.oos_total_pnl + 10) for w in valid])
            weights = weights / weights.sum()

            for key in all_params[0].keys():
                values = np.array([p.get(key, 0) for p in all_params[:len(valid)]])
                weighted_avg = float(np.average(values, weights=weights[:len(values)]))
                # 整数参数取整
                if all(isinstance(p.get(key), int) for p in all_params if key in p):
                    report.recommended_params[key] = int(round(weighted_avg))
                else:
                    report.recommended_params[key] = round(weighted_avg, 1)
