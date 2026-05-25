"""
回测指标计算器
提供策略评估所需的所有量化指标。

支持指标:
  - 基础: 总交易数、胜率、盈亏比
  - 收益: 总 PnL、年化收益率、月均收益
  - 风险: 最大回撤、Sharpe、Sortino、Calmar
  - 稳定性: 利润因子、恢复因子、连续亏损
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np


@dataclass
class BacktestMetrics:
    """回测结果指标集"""

    # 基础统计
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0              # %

    # 收益
    total_pnl: float = 0.0             # USDT
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    profit_factor: float = 0.0         # gross_profit / |gross_loss|

    # 风险
    max_drawdown_pct: float = 0.0      # %
    max_drawdown_usd: float = 0.0      # USDT
    max_drawdown_duration_hours: float = 0.0
    sharpe_ratio: float = 0.0          # 年化
    sortino_ratio: float = 0.0         # 年化
    calmar_ratio: float = 0.0          # 年化收益 / 最大回撤

    # 收益率
    total_return_pct: float = 0.0      # %
    annualized_return_pct: float = 0.0 # %
    monthly_return_pct: float = 0.0    # 平均月收益 %

    # 稳定性
    avg_hold_hours: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    recovery_factor: float = 0.0       # total_pnl / max_drawdown
    payoff_ratio: float = 0.0          # avg_win / |avg_loss|

    # 时间
    backtest_days: float = 0.0
    trades_per_day: float = 0.0

    # 原始数据
    equity_curve: List[float] = field(default_factory=list)
    trade_pnls: List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()
             if k not in ('equity_curve', 'trade_pnls')}
        d['equity_curve_len'] = len(self.equity_curve)
        d['trade_pnls_len'] = len(self.trade_pnls)
        return d

    @property
    def grade(self) -> str:
        """综合评级 A/B/C/D/F"""
        score = 0
        if self.sharpe_ratio >= 2.0:
            score += 3
        elif self.sharpe_ratio >= 1.0:
            score += 2
        elif self.sharpe_ratio >= 0.5:
            score += 1

        if self.win_rate >= 55:
            score += 2
        elif self.win_rate >= 45:
            score += 1

        if self.profit_factor >= 2.0:
            score += 2
        elif self.profit_factor >= 1.5:
            score += 1

        if self.max_drawdown_pct <= 15:
            score += 2
        elif self.max_drawdown_pct <= 25:
            score += 1

        if score >= 8:
            return 'A'
        elif score >= 6:
            return 'B'
        elif score >= 4:
            return 'C'
        elif score >= 2:
            return 'D'
        return 'F'


def calculate_metrics(
    trade_pnls: List[float],
    initial_capital: float = 1000.0,
    hold_hours: Optional[List[float]] = None,
    backtest_days: float = 90.0,
    risk_free_rate: float = 0.0,
) -> BacktestMetrics:
    """
    从交易 PnL 序列计算完整指标集。

    参数:
      trade_pnls: 每笔交易的已实现 PnL (USDT)
      initial_capital: 初始本金
      hold_hours: 每笔交易的持仓时长 (小时)
      backtest_days: 回测总天数
      risk_free_rate: 无风险利率 (年化 %)
    """
    m = BacktestMetrics()
    m.trade_pnls = list(trade_pnls)
    m.backtest_days = backtest_days

    if not trade_pnls:
        return m

    pnls = np.array(trade_pnls, dtype=np.float64)
    n = len(pnls)
    m.total_trades = n

    # 基础统计
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    m.winning_trades = len(wins)
    m.losing_trades = len(losses)
    m.win_rate = round(m.winning_trades / n * 100, 1) if n > 0 else 0.0

    # 收益
    m.total_pnl = round(float(pnls.sum()), 2)
    m.gross_profit = round(float(wins.sum()), 2) if len(wins) > 0 else 0.0
    m.gross_loss = round(float(losses.sum()), 2) if len(losses) > 0 else 0.0
    m.avg_win = round(float(wins.mean()), 2) if len(wins) > 0 else 0.0
    m.avg_loss = round(float(losses.mean()), 2) if len(losses) > 0 else 0.0
    m.largest_win = round(float(pnls.max()), 2)
    m.largest_loss = round(float(pnls.min()), 2)

    # 利润因子
    if m.gross_loss != 0:
        m.profit_factor = round(abs(m.gross_profit / m.gross_loss), 2)
    else:
        m.profit_factor = float('inf') if m.gross_profit > 0 else 0.0

    # 盈亏比
    if m.avg_loss != 0:
        m.payoff_ratio = round(abs(m.avg_win / m.avg_loss), 2)
    else:
        m.payoff_ratio = float('inf') if m.avg_win > 0 else 0.0

    # 权益曲线 & 最大回撤
    equity = np.cumsum(pnls) + initial_capital
    m.equity_curve = equity.tolist()
    peak = np.maximum.accumulate(equity)
    drawdown = (peak - equity) / peak * 100
    m.max_drawdown_pct = round(float(drawdown.max()), 2)
    m.max_drawdown_usd = round(float((peak - equity).max()), 2)

    # 回撤持续时间
    in_drawdown = equity < peak
    if in_drawdown.any():
        dd_starts = np.where(np.diff(in_drawdown.astype(int)) == 1)[0]
        dd_ends = np.where(np.diff(in_drawdown.astype(int)) == -1)[0]
        if len(dd_starts) > 0:
            if len(dd_ends) == 0 or dd_ends[-1] < dd_starts[-1]:
                dd_ends = np.append(dd_ends, n - 1)
            durations = dd_ends[:len(dd_starts)] - dd_starts[:len(dd_ends)]
            if len(durations) > 0:
                avg_hours_per_trade = backtest_days * 24 / n if n > 0 else 24
                m.max_drawdown_duration_hours = round(
                    float(durations.max()) * avg_hours_per_trade, 1
                )

    # 收益率
    m.total_return_pct = round(m.total_pnl / initial_capital * 100, 2)
    years = backtest_days / 365.25
    if years > 0 and initial_capital > 0:
        final_equity = initial_capital + m.total_pnl
        if final_equity > 0:
            m.annualized_return_pct = round(
                ((final_equity / initial_capital) ** (1 / years) - 1) * 100, 2
            )
    months = backtest_days / 30.44
    m.monthly_return_pct = round(m.total_return_pct / max(months, 1), 2)

    # Sharpe Ratio (年化)
    if n >= 2:
        daily_returns = pnls / initial_capital  # 简化：每笔交易当作一个"周期"
        trades_per_year = n / years if years > 0 else n * 4
        mean_return = float(daily_returns.mean())
        std_return = float(daily_returns.std(ddof=1))
        if std_return > 0:
            m.sharpe_ratio = round(
                (mean_return - risk_free_rate / 100 / trades_per_year)
                / std_return * math.sqrt(trades_per_year), 2
            )

    # Sortino Ratio (年化，只用下行波动率)
    if n >= 2:
        downside = pnls[pnls < 0] / initial_capital
        if len(downside) >= 2:
            downside_std = float(np.std(downside, ddof=1))
            if downside_std > 0:
                trades_per_year = n / years if years > 0 else n * 4
                m.sortino_ratio = round(
                    (mean_return - risk_free_rate / 100 / trades_per_year)
                    / downside_std * math.sqrt(trades_per_year), 2
                )

    # Calmar Ratio
    if m.max_drawdown_pct > 0:
        m.calmar_ratio = round(m.annualized_return_pct / m.max_drawdown_pct, 2)

    # 恢复因子
    if m.max_drawdown_usd > 0:
        m.recovery_factor = round(m.total_pnl / m.max_drawdown_usd, 2)

    # 连续胜/亏
    streak_win, streak_loss = 0, 0
    max_win_streak, max_loss_streak = 0, 0
    for p in pnls:
        if p > 0:
            streak_win += 1
            streak_loss = 0
            max_win_streak = max(max_win_streak, streak_win)
        elif p < 0:
            streak_loss += 1
            streak_win = 0
            max_loss_streak = max(max_loss_streak, streak_loss)
        else:
            streak_win = 0
            streak_loss = 0
    m.max_consecutive_wins = max_win_streak
    m.max_consecutive_losses = max_loss_streak

    # 持仓时间
    if hold_hours:
        m.avg_hold_hours = round(float(np.mean(hold_hours)), 1)

    # 交易频率
    m.trades_per_day = round(n / max(backtest_days, 1), 2)

    return m
