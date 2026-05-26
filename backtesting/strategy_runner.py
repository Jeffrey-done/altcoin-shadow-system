#!/usr/bin/env python3
"""
策略无关回测运行器 v1.0

解决问题：
  原 backtest.py 和 backtesting/engine.py 的信号逻辑硬编码为 RSI 做空。
  本模块允许任何 BaseStrategy 插件直接接入回测，无需修改引擎代码。

设计：
  - 逐 bar 驱动策略的 scan() → confirm() → evaluate_exit()
  - 使用 BacktestDataFeed 模拟实时数据馈送
  - 撮合模型复用 backtesting/slippage.py
  - 输出标准 BacktestMetrics，可送入 Optuna 优化

用法:
  from backtesting.strategy_runner import StrategyBacktester
  from your_strategy_module import YourStrategy

  bt = StrategyBacktester(
      strategy=YourStrategy(),
      datasets={'PEPE/USDT': df_pepe, 'DOGE/USDT': df_doge},
      initial_capital=1000,
  )
  result = bt.run()
  print(result.metrics.sharpe_ratio)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

import numpy as np
import pandas as pd

from strategies.base import (
    BaseStrategy, DataFeed, MarketSnapshot,
    Candidate, Signal, SignalDirection,
    ExitSignal, ExitReason, TradeContext,
)
from backtesting.metrics import BacktestMetrics, calculate_metrics
from backtesting.slippage import SlippageModel, SlippageConfig
from data.feeds import BacktestDataFeed

logger = logging.getLogger("backtesting.strategy_runner")


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

@dataclass
class StrategyBacktestConfig:
    """策略回测配置"""
    initial_capital: float = 1000.0
    fee_pct: float = 0.04               # 每边手续费 %
    slippage_config: Optional[SlippageConfig] = None
    max_open_trades: int = 3            # 最大同时持仓数
    compound: bool = True               # 是否复利
    scan_interval_bars: int = 24        # 每 N 根 bar 做一次 scan（模拟调度）
    confirm_interval_bars: int = 1      # 每 N 根 bar 做一次 confirm
    funding_rate_sim: float = 0.01      # 模拟资金费率 (%/8h)


# ══════════════════════════════════════════════════════════════════
#  回测交易记录
# ══════════════════════════════════════════════════════════════════

@dataclass
class BacktestPosition:
    """回测持仓"""
    trade_id: str
    symbol: str
    direction: str                     # 'SHORT' / 'LONG'
    entry_bar: int
    entry_price: float
    stake: float
    leverage: int
    shares: float                      # 持仓数量
    hard_stop_pct: float = 5.0
    tp1_pct: float = 5.0
    tp2_pct: float = 8.0
    trail_retrace_ratio: float = 0.4
    max_hold_bars: int = 24
    trail_activate_pct: float = 3.0

    # 运行时状态
    best_pnl_pct: float = 0.0
    tp1_triggered: bool = False
    tp1_locked_pnl: float = 0.0
    stake_remaining: float = 0.0
    exit_bar: int = -1
    exit_price: float = 0.0
    exit_reason: str = ''
    pnl: float = 0.0

    def __post_init__(self):
        if self.stake_remaining == 0:
            self.stake_remaining = self.stake


@dataclass
class StrategyBacktestResult:
    """策略回测结果"""
    strategy_name: str = ''
    strategy_version: str = ''
    metrics: Optional[BacktestMetrics] = None
    trades: List[BacktestPosition] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    symbols_tested: List[str] = field(default_factory=list)
    total_bars: int = 0
    elapsed_sec: float = 0.0
    config: Optional[StrategyBacktestConfig] = None


# ══════════════════════════════════════════════════════════════════
#  策略回测器
# ══════════════════════════════════════════════════════════════════

class StrategyBacktester:
    """
    策略无关回测器 — 任何 BaseStrategy 都可以接入。

    用法:
      bt = StrategyBacktester(
          strategy=MyStrategy(),
          datasets={'PEPE/USDT': df},
          initial_capital=1000,
      )
      result = bt.run()
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        datasets: Dict[str, pd.DataFrame],
        initial_capital: float = 1000.0,
        config: Optional[StrategyBacktestConfig] = None,
        funding_rates: Optional[Dict[str, float]] = None,
        oi_changes: Optional[Dict[str, float]] = None,
    ):
        self.strategy = strategy
        self.datasets = datasets
        self.config = config or StrategyBacktestConfig(initial_capital=initial_capital)
        self._slippage_model = SlippageModel(
            self.config.slippage_config or SlippageConfig()
        )

        # 数据馈送
        self._feed = BacktestDataFeed(
            datasets=datasets,
            funding_rates=funding_rates or {s: self.config.funding_rate_sim for s in datasets},
            oi_changes=oi_changes or {s: 0.1 for s in datasets},
        )

        # 状态
        self._equity = self.config.initial_capital
        self._equity_curve: List[float] = []
        self._open_positions: List[BacktestPosition] = []
        self._closed_positions: List[BacktestPosition] = []
        self._candidates: List[Candidate] = []
        self._trade_counter = 0
        self._total_bars = 0

    def run(self) -> StrategyBacktestResult:
        """运行完整回测"""
        t0 = time.monotonic()
        cfg = self.config

        # 确定总 bar 数（取所有 dataset 中最长的）
        max_bars = max(len(df) for df in self.datasets.values())
        warmup = 30  # 前 30 根 bar 用于指标预热

        for bar in range(warmup, max_bars):
            self._total_bars = bar
            self._feed.set_bar(bar)

            # 1. 定期扫描
            if bar % cfg.scan_interval_bars == 0:
                self._run_scan(bar)

            # 2. 确认候选
            if bar % cfg.confirm_interval_bars == 0:
                self._run_confirm(bar)

            # 3. 评估退出
            self._run_exit_check(bar)

            # 4. 更新浮动盈亏 & 权益曲线
            self._update_equity(bar)

        # 强制平仓所有剩余持仓
        self._close_all_remaining(max_bars - 1)

        # 计算指标
        pnl_list = [p.pnl for p in self._closed_positions]
        metrics = calculate_metrics(
            pnl_list=pnl_list,
            equity_curve=self._equity_curve,
            initial_capital=cfg.initial_capital,
        )

        elapsed = time.monotonic() - t0

        return StrategyBacktestResult(
            strategy_name=self.strategy.name,
            strategy_version=self.strategy.version,
            metrics=metrics,
            trades=self._closed_positions,
            equity_curve=self._equity_curve,
            symbols_tested=list(self.datasets.keys()),
            total_bars=self._total_bars,
            elapsed_sec=round(elapsed, 3),
            config=cfg,
        )

    # ── 回测循环子步骤 ───────────────────────────────────────────

    def _run_scan(self, bar: int):
        """运行策略 scan"""
        try:
            market = MarketSnapshot(tickers=self._feed.get_tickers())
            new_candidates = self.strategy.scan(self._feed, market)
            if new_candidates:
                # 去重
                existing_symbols = {c.symbol for c in self._candidates}
                for c in new_candidates:
                    if c.symbol not in existing_symbols:
                        self._candidates.append(c)
                        existing_symbols.add(c.symbol)
        except Exception as e:
            logger.debug(f"scan 异常 (bar={bar}): {e}")

    def _run_confirm(self, bar: int):
        """运行策略 confirm"""
        if not self._candidates:
            return

        # 限制同时持仓数
        if len(self._open_positions) >= self.config.max_open_trades:
            return

        confirmed = []
        for candidate in self._candidates[:]:
            try:
                signal = self.strategy.confirm(candidate, self._feed)
                if signal:
                    confirmed.append(signal)
                    self._candidates.remove(candidate)
                    if len(self._open_positions) + len(confirmed) >= self.config.max_open_trades:
                        break
            except Exception as e:
                logger.debug(f"confirm 异常 ({candidate.symbol}): {e}")

        # 开仓
        for signal in confirmed:
            self._open_position(signal, bar)

    def _run_exit_check(self, bar: int):
        """评估所有持仓的退出条件"""
        for pos in self._open_positions[:]:
            symbol = pos.symbol
            ticker = self._feed.get_ticker(symbol)
            current_price = ticker.get('last', 0)
            if current_price <= 0:
                continue

            # 构建 TradeContext
            hold_bars = bar - pos.entry_bar
            if pos.direction == 'SHORT':
                pnl_pct = (pos.entry_price - current_price) / pos.entry_price * 100
            else:
                pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100

            # 更新 best_pnl
            if pnl_pct > pos.best_pnl_pct:
                pos.best_pnl_pct = pnl_pct

            ctx = TradeContext(
                trade_id=pos.trade_id,
                symbol=symbol,
                direction=pos.direction,
                entry_price=pos.entry_price,
                current_price=current_price,
                stake=pos.stake,
                stake_remaining=pos.stake_remaining,
                leverage=pos.leverage,
                shares=pos.shares,
                opened_at='',
                pnl_pct=pnl_pct,
                best_pnl_pct=pos.best_pnl_pct,
                hold_hours=hold_bars,  # 1 bar ≈ 1 hour
                tp1_triggered=pos.tp1_triggered,
                tp1_locked_pnl=pos.tp1_locked_pnl,
                hard_stop_price=None,
                trail_stop_price=None,
                exchange='backtest',
                account_id='',
            )

            try:
                exit_signal = self.strategy.evaluate_exit(ctx, self._feed)
                if exit_signal:
                    self._close_position(pos, bar, current_price, exit_signal.reason.value)
            except Exception as e:
                logger.debug(f"evaluate_exit 异常 ({symbol}): {e}")

    def _open_position(self, signal: Signal, bar: int):
        """开仓"""
        symbol = signal.symbol
        ticker = self._feed.get_ticker(symbol)
        entry_price = ticker.get('last', 0)
        if entry_price <= 0:
            return

        # 应用滑点
        if signal.direction == SignalDirection.SHORT:
            entry_price *= (1 - self.config.fee_pct / 100)
        else:
            entry_price *= (1 + self.config.fee_pct / 100)

        # 仓位计算
        stake = min(signal.stake, self._equity * 0.5)
        if stake <= 0:
            return

        notional = stake * signal.leverage
        shares = notional / entry_price

        self._trade_counter += 1
        pos = BacktestPosition(
            trade_id=f"bt_{self.strategy.name}_{self._trade_counter}",
            symbol=symbol,
            direction=signal.direction.value,
            entry_bar=bar,
            entry_price=entry_price,
            stake=stake,
            leverage=signal.leverage,
            shares=shares,
            hard_stop_pct=signal.hard_stop_pct,
            tp1_pct=signal.tp1_pct,
            tp2_pct=signal.tp2_pct,
            trail_retrace_ratio=signal.trail_retrace_ratio,
            max_hold_bars=signal.max_hold_hours,
            stake_remaining=stake,
        )
        self._open_positions.append(pos)

    def _close_position(self, pos: BacktestPosition, bar: int,
                        exit_price: float, reason: str):
        """平仓"""
        # 应用滑点/手续费
        if pos.direction == 'SHORT':
            exit_price *= (1 + self.config.fee_pct / 100)
            pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100
        else:
            exit_price *= (1 - self.config.fee_pct / 100)
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100

        notional_remaining = pos.stake_remaining * pos.leverage
        pnl = notional_remaining * pnl_pct / 100

        pos.exit_bar = bar
        pos.exit_price = exit_price
        pos.exit_reason = reason
        pos.pnl = pnl + pos.tp1_locked_pnl

        self._equity += pnl
        self._open_positions.remove(pos)
        self._closed_positions.append(pos)

    def _close_all_remaining(self, bar: int):
        """回测结束时强制平仓"""
        for pos in self._open_positions[:]:
            ticker = self._feed.get_ticker(pos.symbol)
            price = ticker.get('last', pos.entry_price)
            self._close_position(pos, bar, price, 'backtest_end')

    def _update_equity(self, bar: int):
        """更新权益曲线"""
        floating_pnl = 0.0
        for pos in self._open_positions:
            ticker = self._feed.get_ticker(pos.symbol)
            price = ticker.get('last', pos.entry_price)
            if pos.direction == 'SHORT':
                pnl_pct = (pos.entry_price - price) / pos.entry_price * 100
            else:
                pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
            notional = pos.stake_remaining * pos.leverage
            floating_pnl += notional * pnl_pct / 100

        self._equity_curve.append(self._equity + floating_pnl)
