"""
Optuna 参数优化器
贝叶斯优化策略参数，自动搜索最优 Sharpe/收益/胜率。

特性:
  - TPE (Tree-structured Parzen Estimator) 采样器
  - Hyperband 剪枝（早停低效试验）
  - 多目标优化支持（Sharpe + 最大回撤）
  - Walk-forward 验证防过拟合
  - 结果持久化到 SQLite
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("optimization")


@dataclass
class OptimizationConfig:
    """优化配置"""
    n_trials: int = 200             # 试验次数
    timeout_sec: int = 600          # 总超时（秒）
    objective: str = 'sharpe'       # 优化目标: sharpe | sortino | pnl | win_rate
    direction: str = 'maximize'     # maximize | minimize
    min_trades: int = 10            # 最少交易数（少于则剪枝）
    seed: int = 42
    n_jobs: int = 1                 # 并行数（1=串行）
    study_name: str = 'strategy_opt'
    storage: Optional[str] = None   # Optuna 存储 URL（None=内存）

    # Walk-forward 配置
    walk_forward: bool = False      # 是否启用 walk-forward 验证
    wf_train_ratio: float = 0.7     # 训练集比例
    wf_n_splits: int = 3            # 滚动窗口数

    # 惩罚项
    drawdown_penalty_weight: float = 0.3  # 回撤惩罚权重


@dataclass
class OptimizationResult:
    """优化结果"""
    best_params: Dict[str, Any] = field(default_factory=dict)
    best_value: float = 0.0
    n_trials_completed: int = 0
    elapsed_sec: float = 0.0
    all_trials: List[Dict] = field(default_factory=list)
    param_importances: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'best_params': self.best_params,
            'best_value': self.best_value,
            'n_trials': self.n_trials_completed,
            'elapsed_sec': self.elapsed_sec,
            'param_importances': self.param_importances,
        }


class StrategyOptimizer:
    """
    策略参数优化器。

    用法:
      from optimization.optimizer import StrategyOptimizer, OptimizationConfig
      from strategies.short_overbought import ShortOverboughtStrategy
      from backtesting.engine import VectorizedBacktester, BacktestConfig

      optimizer = StrategyOptimizer(
          strategy=ShortOverboughtStrategy(),
          ohlcv_data=df,
          config=OptimizationConfig(n_trials=300),
      )
      result = optimizer.optimize()
      print(f"最优参数: {result.best_params}")
      print(f"最优 Sharpe: {result.best_value}")
    """

    def __init__(
        self,
        strategy,
        ohlcv_data: pd.DataFrame,
        config: Optional[OptimizationConfig] = None,
        backtest_config=None,
        symbol: str = '',
    ):
        self.strategy = strategy
        self.ohlcv_data = ohlcv_data
        self.config = config or OptimizationConfig()
        self.backtest_config = backtest_config
        self.symbol = symbol

    def optimize(self) -> OptimizationResult:
        """运行优化"""
        try:
            import optuna
            optuna.logging.set_verbosity(optuna.logging.WARNING)
        except ImportError:
            logger.error("optuna 未安装，请 pip install optuna")
            return OptimizationResult()

        cfg = self.config
        t0 = time.monotonic()

        # 创建 study
        sampler = optuna.samplers.TPESampler(seed=cfg.seed)
        pruner = optuna.pruners.HyperbandPruner(
            min_resource=5, max_resource=cfg.n_trials, reduction_factor=3
        )

        study = optuna.create_study(
            study_name=cfg.study_name,
            direction=cfg.direction,
            sampler=sampler,
            pruner=pruner,
            storage=cfg.storage,
            load_if_exists=True,
        )

        # 获取参数空间
        param_space = self.strategy.get_param_space()
        if not param_space:
            logger.warning("策略未定义参数空间 (get_param_space 返回空)")
            return OptimizationResult()

        def objective(trial: optuna.Trial) -> float:
            # 从参数空间采样
            params = {}
            for name, spec in param_space.items():
                ptype = spec.get('type', 'float')
                if ptype == 'int':
                    params[name] = trial.suggest_int(
                        name, spec['low'], spec['high'],
                        step=spec.get('step', 1),
                    )
                elif ptype == 'float':
                    params[name] = trial.suggest_float(
                        name, spec['low'], spec['high'],
                        step=spec.get('step'),
                    )
                elif ptype == 'categorical':
                    params[name] = trial.suggest_categorical(
                        name, spec['choices']
                    )

            # 运行回测
            from backtesting.engine import VectorizedBacktester
            bt = VectorizedBacktester(
                strategy=self.strategy,
                ohlcv_data=self.ohlcv_data,
                config=self.backtest_config,
                symbol=self.symbol,
            )
            result = bt.run(params)
            metrics = result.metrics

            # 交易数不足 → 剪枝
            if metrics.total_trades < cfg.min_trades:
                raise optuna.TrialPruned()

            # 计算目标值
            if cfg.objective == 'sharpe':
                value = metrics.sharpe_ratio
            elif cfg.objective == 'sortino':
                value = metrics.sortino_ratio
            elif cfg.objective == 'pnl':
                value = metrics.total_pnl
            elif cfg.objective == 'win_rate':
                value = metrics.win_rate
            elif cfg.objective == 'calmar':
                value = metrics.calmar_ratio
            else:
                value = metrics.sharpe_ratio

            # 回撤惩罚
            if cfg.drawdown_penalty_weight > 0:
                dd_penalty = metrics.max_drawdown_pct * cfg.drawdown_penalty_weight / 100
                value -= dd_penalty

            # 记录中间指标
            trial.set_user_attr('total_trades', metrics.total_trades)
            trial.set_user_attr('win_rate', metrics.win_rate)
            trial.set_user_attr('total_pnl', metrics.total_pnl)
            trial.set_user_attr('max_drawdown', metrics.max_drawdown_pct)
            trial.set_user_attr('profit_factor', metrics.profit_factor)

            return value

        # 执行优化
        study.optimize(
            objective,
            n_trials=cfg.n_trials,
            timeout=cfg.timeout_sec,
            n_jobs=cfg.n_jobs,
            show_progress_bar=False,
        )

        elapsed = time.monotonic() - t0

        # 构建结果
        opt_result = OptimizationResult(
            best_params=study.best_params if study.best_trial else {},
            best_value=study.best_value if study.best_trial else 0.0,
            n_trials_completed=len(study.trials),
            elapsed_sec=round(elapsed, 2),
        )

        # 参数重要性
        try:
            importances = optuna.importance.get_param_importances(study)
            opt_result.param_importances = {
                k: round(v, 3) for k, v in importances.items()
            }
        except Exception:
            pass

        # 所有试验摘要
        for trial in study.trials:
            if trial.state == optuna.trial.TrialState.COMPLETE:
                opt_result.all_trials.append({
                    'number': trial.number,
                    'value': round(trial.value, 4) if trial.value else 0,
                    'params': trial.params,
                    'user_attrs': trial.user_attrs,
                })

        logger.info(
            f"优化完成: {opt_result.n_trials_completed} 次试验 | "
            f"最优 {cfg.objective}={opt_result.best_value:.3f} | "
            f"耗时 {elapsed:.1f}s"
        )

        return opt_result

    def walk_forward_optimize(self) -> OptimizationResult:
        """
        Walk-forward 优化：滚动窗口训练+验证，防止过拟合。

        流程:
          1. 数据分为 N 个窗口
          2. 每个窗口前 70% 训练（Optuna 优化）
          3. 后 30% 用最优参数验证
          4. 汇总所有验证期的表现
        """
        cfg = self.config
        df = self.ohlcv_data
        n = len(df)
        n_splits = cfg.wf_n_splits
        train_ratio = cfg.wf_train_ratio

        window_size = n // n_splits
        if window_size < 100:
            logger.warning("数据量不足以做 walk-forward（每窗口<100 bars）")
            return self.optimize()

        all_oos_pnls = []
        best_params_per_fold = []

        for fold in range(n_splits):
            start = fold * window_size
            end = min(start + window_size, n)
            fold_df = df.iloc[start:end].reset_index(drop=True)

            split_point = int(len(fold_df) * train_ratio)
            train_df = fold_df.iloc[:split_point].reset_index(drop=True)
            test_df = fold_df.iloc[split_point:].reset_index(drop=True)

            # 训练期优化
            train_opt = StrategyOptimizer(
                strategy=self.strategy,
                ohlcv_data=train_df,
                config=OptimizationConfig(
                    n_trials=cfg.n_trials // n_splits,
                    timeout_sec=cfg.timeout_sec // n_splits,
                    objective=cfg.objective,
                    seed=cfg.seed + fold,
                    min_trades=max(3, cfg.min_trades // 2),
                ),
                backtest_config=self.backtest_config,
                symbol=self.symbol,
            )
            fold_result = train_opt.optimize()

            if not fold_result.best_params:
                continue

            best_params_per_fold.append(fold_result.best_params)

            # 验证期回测
            from backtesting.engine import VectorizedBacktester
            bt = VectorizedBacktester(
                strategy=self.strategy,
                ohlcv_data=test_df,
                config=self.backtest_config,
                symbol=self.symbol,
            )
            oos_result = bt.run(fold_result.best_params)
            all_oos_pnls.extend([t.total_pnl for t in oos_result.trades])

            logger.info(
                f"WF Fold {fold+1}/{n_splits}: "
                f"训练 {cfg.objective}={fold_result.best_value:.3f} | "
                f"验证 trades={oos_result.metrics.total_trades} "
                f"PnL={oos_result.metrics.total_pnl:.1f}U"
            )

        # 汇总结果
        if not best_params_per_fold:
            return OptimizationResult()

        # 用最后一个 fold 的最优参数作为最终推荐
        final_params = best_params_per_fold[-1]

        from backtesting.metrics import calculate_metrics
        oos_metrics = calculate_metrics(
            all_oos_pnls,
            initial_capital=self.backtest_config.initial_capital if self.backtest_config else 1000,
        )

        return OptimizationResult(
            best_params=final_params,
            best_value=oos_metrics.sharpe_ratio,
            n_trials_completed=sum(1 for _ in best_params_per_fold),
        )
