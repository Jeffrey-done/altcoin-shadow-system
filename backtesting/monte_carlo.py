"""
蒙特卡洛模拟 — 策略稳健性评估
通过随机打乱交易顺序来评估策略在不同市场序列下的表现分布。

核心指标:
  - 破产概率: 净值跌破 N% 的概率
  - VaR (Value at Risk): 给定置信度下的最大亏损
  - 最大回撤分布: P50 / P95 / P99 分位数
  - 预期终值分布: 评估策略长期期望
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np


@dataclass
class MonteCarloResult:
    """蒙特卡洛模拟结果"""

    # 基础统计
    n_simulations: int = 0
    n_trades: int = 0
    initial_capital: float = 1000.0

    # 终值分布
    final_equity_mean: float = 0.0
    final_equity_median: float = 0.0
    final_equity_p5: float = 0.0       # 5% 分位数（悲观）
    final_equity_p95: float = 0.0      # 95% 分位数（乐观）
    final_equity_std: float = 0.0

    # 破产概率
    ruin_probability: float = 0.0      # 净值 < 50% 初始本金的概率
    ruin_threshold_pct: float = 50.0   # 破产定义阈值

    # 最大回撤分布
    max_drawdown_mean: float = 0.0     # %
    max_drawdown_median: float = 0.0
    max_drawdown_p95: float = 0.0      # 95% 分位数（最坏情况）
    max_drawdown_p99: float = 0.0

    # VaR (Value at Risk)
    var_95: float = 0.0                # 95% VaR (USDT)
    var_99: float = 0.0                # 99% VaR (USDT)
    cvar_95: float = 0.0              # 条件 VaR (Expected Shortfall)

    # Sharpe 分布
    sharpe_mean: float = 0.0
    sharpe_p5: float = 0.0
    sharpe_p95: float = 0.0

    # 原始数据（用于绘图）
    equity_paths: Optional[np.ndarray] = None  # (n_simulations, n_trades+1)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != 'equity_paths'}
        d['has_equity_paths'] = self.equity_paths is not None
        return d

    @property
    def summary(self) -> str:
        """人类可读摘要"""
        return (
            f"蒙特卡洛模拟结果 ({self.n_simulations}次 × {self.n_trades}笔交易)\n"
            f"{'─'*50}\n"
            f"终值:    均值={self.final_equity_mean:.0f}U  "
            f"中位={self.final_equity_median:.0f}U  "
            f"P5={self.final_equity_p5:.0f}U  P95={self.final_equity_p95:.0f}U\n"
            f"破产率:  {self.ruin_probability*100:.1f}% "
            f"(净值<{self.ruin_threshold_pct}%初始本金)\n"
            f"回撤:    均值={self.max_drawdown_mean:.1f}%  "
            f"P95={self.max_drawdown_p95:.1f}%  P99={self.max_drawdown_p99:.1f}%\n"
            f"VaR:     95%={self.var_95:.1f}U  99%={self.var_99:.1f}U  "
            f"CVaR95={self.cvar_95:.1f}U\n"
            f"Sharpe:  均值={self.sharpe_mean:.2f}  "
            f"P5={self.sharpe_p5:.2f}  P95={self.sharpe_p95:.2f}"
        )


def run_monte_carlo(
    trade_pnls: List[float],
    n_simulations: int = 10000,
    initial_capital: float = 1000.0,
    ruin_threshold_pct: float = 50.0,
    seed: Optional[int] = None,
    keep_paths: bool = False,
) -> MonteCarloResult:
    """
    运行蒙特卡洛模拟。

    参数:
      trade_pnls: 历史交易 PnL 序列 (USDT)
      n_simulations: 模拟次数
      initial_capital: 初始本金
      ruin_threshold_pct: 破产阈值（净值低于此比例视为破产）
      seed: 随机种子（可复现）
      keep_paths: 是否保留全部权益路径（绘图用，消耗内存）

    返回:
      MonteCarloResult
    """
    result = MonteCarloResult(
        n_simulations=n_simulations,
        n_trades=len(trade_pnls),
        initial_capital=initial_capital,
        ruin_threshold_pct=ruin_threshold_pct,
    )

    if not trade_pnls or len(trade_pnls) < 3:
        return result

    rng = np.random.default_rng(seed)
    pnls = np.array(trade_pnls, dtype=np.float64)
    n_trades = len(pnls)
    ruin_level = initial_capital * ruin_threshold_pct / 100

    # 批量生成随机排列索引
    # shape: (n_simulations, n_trades)
    indices = np.array([rng.permutation(n_trades) for _ in range(n_simulations)])

    # 批量重排 PnL
    shuffled_pnls = pnls[indices]  # (n_simulations, n_trades)

    # 批量计算权益曲线
    equity_curves = np.cumsum(shuffled_pnls, axis=1) + initial_capital
    # 加上初始值列
    equity_full = np.column_stack([
        np.full(n_simulations, initial_capital),
        equity_curves
    ])  # (n_simulations, n_trades+1)

    # ── 终值分布 ──
    final_equities = equity_full[:, -1]
    result.final_equity_mean = round(float(final_equities.mean()), 2)
    result.final_equity_median = round(float(np.median(final_equities)), 2)
    result.final_equity_p5 = round(float(np.percentile(final_equities, 5)), 2)
    result.final_equity_p95 = round(float(np.percentile(final_equities, 95)), 2)
    result.final_equity_std = round(float(final_equities.std()), 2)

    # ── 破产概率 ──
    # 任何时刻净值低于 ruin_level 即视为破产
    min_equity_per_sim = equity_full.min(axis=1)
    ruin_count = (min_equity_per_sim < ruin_level).sum()
    result.ruin_probability = round(float(ruin_count / n_simulations), 4)

    # ── 最大回撤分布 ──
    peaks = np.maximum.accumulate(equity_full, axis=1)
    drawdowns_pct = (peaks - equity_full) / peaks * 100
    max_drawdowns = drawdowns_pct.max(axis=1)
    result.max_drawdown_mean = round(float(max_drawdowns.mean()), 2)
    result.max_drawdown_median = round(float(np.median(max_drawdowns)), 2)
    result.max_drawdown_p95 = round(float(np.percentile(max_drawdowns, 95)), 2)
    result.max_drawdown_p99 = round(float(np.percentile(max_drawdowns, 99)), 2)

    # ── VaR ──
    # 基于终值的亏损分布
    losses = initial_capital - final_equities  # 正数=亏损
    result.var_95 = round(float(np.percentile(losses, 95)), 2)
    result.var_99 = round(float(np.percentile(losses, 99)), 2)
    # CVaR: 超过 VaR 的平均亏损
    tail_losses = losses[losses >= np.percentile(losses, 95)]
    result.cvar_95 = round(float(tail_losses.mean()), 2) if len(tail_losses) > 0 else result.var_95

    # ── Sharpe 分布 ──
    # 每条路径算一个"伪 Sharpe"
    mean_returns = shuffled_pnls.mean(axis=1) / initial_capital
    std_returns = shuffled_pnls.std(axis=1, ddof=1) / initial_capital
    # 避免除零
    valid_mask = std_returns > 1e-10
    sharpes = np.zeros(n_simulations)
    if valid_mask.any():
        sharpes[valid_mask] = mean_returns[valid_mask] / std_returns[valid_mask] * np.sqrt(n_trades)
    result.sharpe_mean = round(float(sharpes.mean()), 2)
    result.sharpe_p5 = round(float(np.percentile(sharpes, 5)), 2)
    result.sharpe_p95 = round(float(np.percentile(sharpes, 95)), 2)

    # 保留路径（可选）
    if keep_paths:
        result.equity_paths = equity_full

    return result
