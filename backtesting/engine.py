"""
向量化回测引擎 v2.0
用 pandas/numpy 替代逐 bar 循环，性能提升 10~50x。

设计:
  - 策略通过 BaseStrategy 接口接入，引擎只负责撮合模拟
  - 支持多档止盈（TP1 半仓 + TP2 全仓）
  - 支持移动止损、时间止损
  - 滑点 & 手续费通过 SlippageModel 和 fee_pct 参数注入
  - 输出标准 BacktestMetrics，可直接送入 Optuna 优化

用法:
  from backtesting.engine import VectorizedBacktester
  from strategies.short_overbought import ShortOverboughtStrategy

  bt = VectorizedBacktester(
      strategy=ShortOverboughtStrategy(),
      ohlcv_data=df,  # columns: [timestamp, open, high, low, close, volume]
      initial_capital=1000,
  )
  result = bt.run()
  print(result.metrics.sharpe_ratio)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

import numpy as np
import pandas as pd

from backtesting.metrics import BacktestMetrics, calculate_metrics
from backtesting.slippage import SlippageModel, SlippageConfig
from strategies.base import BaseStrategy, SignalDirection


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 1000.0
    fee_pct: float = 0.04          # 每边手续费 %
    slippage_config: Optional[SlippageConfig] = None
    max_open_trades: int = 3       # 最大同时持仓数
    compound: bool = True          # 是否复利（用当前权益计算仓位）
    timeframe: str = '1h'          # 主回测时间框架


@dataclass
class BacktestTrade:
    """单笔回测交易"""
    entry_bar: int
    exit_bar: int = -1
    symbol: str = ''
    direction: str = 'SHORT'
    entry_price: float = 0.0
    exit_price: float = 0.0
    stake: float = 0.0
    leverage: int = 10
    notional: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    fee_total: float = 0.0
    slippage_total: float = 0.0
    hold_bars: int = 0
    hold_hours: float = 0.0
    exit_reason: str = ''
    score: float = 0.0
    tp1_triggered: bool = False
    tp1_pnl: float = 0.0

    @property
    def total_pnl(self) -> float:
        return self.tp1_pnl + self.pnl


@dataclass
class BacktestResult:
    """回测完整结果"""
    metrics: BacktestMetrics = field(default_factory=BacktestMetrics)
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    elapsed_sec: float = 0.0
    symbol: str = ''
    timeframe: str = '1h'
    bars_processed: int = 0


class VectorizedBacktester:
    """
    向量化回测引擎。

    工作流程:
      1. 向量化计算技术指标（RSI / 均线等）
      2. 向量化生成信号（满足条件的 bar 标记为候选）
      3. 逐交易模拟持仓管理（止盈/止损/移动止损）
         注: 持仓管理无法完全向量化（状态依赖），但单笔交易内的
         高低价遍历用 numpy 加速

    性能:
      - 90天 1h 数据 (~2160 bars): < 0.5s
      - 向量化指标计算: O(n) 单遍扫描
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        ohlcv_data: pd.DataFrame,
        config: Optional[BacktestConfig] = None,
        symbol: str = '',
    ):
        self.strategy = strategy
        self.config = config or BacktestConfig()
        self.symbol = symbol
        self.slippage = SlippageModel(self.config.slippage_config or SlippageConfig())

        # 标准化 DataFrame
        if isinstance(ohlcv_data, pd.DataFrame):
            self.df = ohlcv_data.copy()
        else:
            self.df = pd.DataFrame(
                ohlcv_data,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )

        # 确保数值类型
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in self.df.columns:
                self.df[col] = pd.to_numeric(self.df[col], errors='coerce')

    def run(self, params: Optional[Dict[str, Any]] = None) -> BacktestResult:
        """
        执行回测。

        参数:
          params: 策略参数覆盖（用于 Optuna 优化）

        返回:
          BacktestResult 包含 metrics + trades + equity_curve
        """
        t0 = time.monotonic()

        # 应用参数覆盖
        if params:
            self.strategy.set_params(params)

        strategy_params = self.strategy.get_params()
        cfg = self.config
        df = self.df

        if len(df) < 50:
            return BacktestResult(
                params=strategy_params,
                symbol=self.symbol,
                timeframe=cfg.timeframe,
                bars_processed=len(df),
            )

        # ── 阶段1: 向量化指标计算 ──
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        volumes = df['volume'].values

        rsi = self._calc_rsi_vectorized(closes, strategy_params.get('rsi_period', 14))
        df_internal = df.copy()
        df_internal['rsi'] = rsi

        # ── 阶段2: 向量化信号生成 ──
        signals = self._generate_signals(df_internal, strategy_params)

        # ── 阶段3: 交易模拟 ──
        trades = self._simulate_trades(
            df_internal, signals, strategy_params, cfg
        )

        # ── 阶段4: 计算指标 ──
        trade_pnls = [t.total_pnl for t in trades]
        hold_hours = [t.hold_hours for t in trades]
        backtest_days = len(df) / 24.0 if cfg.timeframe == '1h' else len(df)

        metrics = calculate_metrics(
            trade_pnls=trade_pnls,
            initial_capital=cfg.initial_capital,
            hold_hours=hold_hours,
            backtest_days=backtest_days,
        )

        elapsed = time.monotonic() - t0

        return BacktestResult(
            metrics=metrics,
            trades=trades,
            equity_curve=metrics.equity_curve,
            params=strategy_params,
            elapsed_sec=round(elapsed, 3),
            symbol=self.symbol,
            timeframe=cfg.timeframe,
            bars_processed=len(df),
        )

    # ══════════════════════════════════════════════════════════════
    #  向量化指标计算
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _calc_rsi_vectorized(closes: np.ndarray, period: int = 14) -> np.ndarray:
        """向量化 Wilder RSI 计算"""
        n = len(closes)
        rsi = np.full(n, 50.0)

        if n < period + 1:
            return rsi

        deltas = np.diff(closes)
        gains = np.maximum(deltas, 0)
        losses = np.maximum(-deltas, 0)

        # SMA 初始化
        avg_gain = gains[:period].mean()
        avg_loss = losses[:period].mean()

        # Wilder 递推
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

            if avg_loss == 0:
                rsi[i + 1] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))

        return rsi

    # ══════════════════════════════════════════════════════════════
    #  信号生成
    # ══════════════════════════════════════════════════════════════

    def _generate_signals(self, df: pd.DataFrame, params: dict) -> np.ndarray:
        """
        向量化信号生成：标记满足入场条件的 bar。

        返回: 布尔数组，True = 该 bar 触发做空信号
        """
        n = len(df)
        signals = np.zeros(n, dtype=bool)

        rsi = df['rsi'].values
        closes = df['close'].values
        highs = df['high'].values
        volumes = df['volume'].values

        # 参数
        rsi_enter = params.get('h4_rsi_enter', 70)
        rsi_drop = params.get('h4_rsi_drop', 10)
        rsi_min_daily = params.get('daily_rsi_min', 75)
        peak_lookback = params.get('h4_rsi_peak_lookback', 10)

        # RSI 峰值（回溯窗口内最大值）
        rsi_peak = pd.Series(rsi).rolling(window=peak_lookback, min_periods=1).max().values

        # 信号条件（向量化）:
        #   1. RSI 曾经达到超买（peak >= daily_rsi_min）
        #   2. 当前 RSI < h4_rsi_enter（回落）
        #   3. 回落幅度 >= h4_rsi_drop
        cond_peak_overbought = rsi_peak >= rsi_min_daily
        cond_rsi_below_enter = rsi < rsi_enter
        cond_drop_enough = (rsi_peak - rsi) >= rsi_drop

        # 额外: 价格在局部高点附近（近 20 bar 内的 90% 以上）
        price_peak_20 = pd.Series(highs).rolling(window=20, min_periods=1).max().values
        cond_price_near_peak = closes >= price_peak_20 * 0.9

        signals = (
            cond_peak_overbought
            & cond_rsi_below_enter
            & cond_drop_enough
            & cond_price_near_peak
        )

        # 去重: 信号触发后 N bar 内不重复触发（冷却）
        cooldown_bars = params.get('max_hold_hours', 24)
        cleaned = np.zeros(n, dtype=bool)
        last_signal_bar = -cooldown_bars - 1
        for i in range(n):
            if signals[i] and (i - last_signal_bar) > cooldown_bars:
                cleaned[i] = True
                last_signal_bar = i

        return cleaned

    # ══════════════════════════════════════════════════════════════
    #  交易模拟
    # ══════════════════════════════════════════════════════════════

    def _simulate_trades(
        self,
        df: pd.DataFrame,
        signals: np.ndarray,
        params: dict,
        cfg: BacktestConfig,
    ) -> List[BacktestTrade]:
        """
        模拟交易执行：逐信号处理持仓生命周期。
        """
        trades: List[BacktestTrade] = []
        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        volumes = df['volume'].values
        n = len(closes)

        # 策略参数
        tp1_pct = params.get('tp1_pct', 5.0)
        tp2_pct = params.get('tp2_pct', 8.0)
        hard_stop_pct = params.get('hard_stop_pct', 5.0)
        trail_activate_pct = params.get('trail_activate_pct', 3.0)
        trail_retrace_ratio = params.get('trail_retrace_ratio', 0.4)
        max_hold_hours = params.get('max_hold_hours', 24)
        tp1_close_ratio = params.get('tp1_close_ratio', 0.5)
        stake = params.get('default_stake', 30.0)
        leverage = params.get('leverage', 10)

        # 时间框架 → bar 转换
        hours_per_bar = 1.0 if cfg.timeframe == '1h' else 4.0
        max_hold_bars = int(max_hold_hours / hours_per_bar)

        equity = cfg.initial_capital
        open_count = 0
        signal_indices = np.where(signals)[0]

        for entry_bar in signal_indices:
            if open_count >= cfg.max_open_trades:
                continue
            if entry_bar >= n - 2:
                continue

            # 动态仓位（复利）
            current_stake = stake
            if cfg.compound and equity > cfg.initial_capital:
                ratio = equity / cfg.initial_capital
                current_stake = min(stake * ratio, stake * 3)

            entry_price = closes[entry_bar]
            if entry_price <= 0:
                continue

            # 应用开仓滑点
            vol_24h = float(volumes[max(0, entry_bar-24):entry_bar+1].sum()) if entry_bar > 0 else 0
            notional = current_stake * leverage
            entry_price_slipped = self.slippage.apply(
                entry_price, notional, vol_24h, 'SHORT'
            )

            # 手续费
            entry_fee = notional * cfg.fee_pct / 100

            # 止盈止损价格（做空）
            tp1_price = entry_price_slipped * (1 - tp1_pct / 100)
            tp2_price = entry_price_slipped * (1 - tp2_pct / 100)
            hard_stop_price = entry_price_slipped * (1 + hard_stop_pct / 100)

            # 模拟持仓
            trade = BacktestTrade(
                entry_bar=entry_bar,
                symbol=self.symbol,
                direction='SHORT',
                entry_price=entry_price_slipped,
                stake=current_stake,
                leverage=leverage,
                notional=notional,
            )

            best_pnl_pct = 0.0
            tp1_done = False
            remaining_ratio = 1.0
            tp1_pnl_locked = 0.0
            exit_bar = -1
            exit_price = 0.0
            exit_reason = ''

            for bar in range(entry_bar + 1, min(entry_bar + max_hold_bars + 1, n)):
                low = lows[bar]
                high = highs[bar]
                close = closes[bar]

                # 做空 PnL %
                pnl_pct_at_low = (entry_price_slipped - low) / entry_price_slipped * 100
                pnl_pct_at_high = (entry_price_slipped - high) / entry_price_slipped * 100
                pnl_pct_at_close = (entry_price_slipped - close) / entry_price_slipped * 100

                best_pnl_pct = max(best_pnl_pct, pnl_pct_at_low)

                # 1. 硬止损（高点触及止损价）
                if high >= hard_stop_price:
                    exit_bar = bar
                    exit_price = hard_stop_price
                    exit_reason = 'hard_stop'
                    break

                # 2. TP1（低点触及 TP1）
                if not tp1_done and low <= tp1_price:
                    tp1_done = True
                    tp1_notional = notional * tp1_close_ratio
                    tp1_pnl_raw = tp1_notional * tp1_pct / 100
                    tp1_fee = tp1_notional * cfg.fee_pct / 100
                    tp1_pnl_locked = tp1_pnl_raw - tp1_fee
                    remaining_ratio = 1 - tp1_close_ratio
                    trade.tp1_triggered = True

                # 3. TP2（低点触及 TP2）
                if low <= tp2_price:
                    exit_bar = bar
                    exit_price = tp2_price
                    exit_reason = 'tp2'
                    break

                # 4. 移动止损
                if best_pnl_pct >= trail_activate_pct:
                    trail_trigger = best_pnl_pct * (1 - trail_retrace_ratio)
                    if pnl_pct_at_close <= trail_trigger:
                        exit_bar = bar
                        exit_price = close
                        exit_reason = 'trail_stop'
                        break

            # 时间止损
            if exit_bar < 0:
                exit_bar = min(entry_bar + max_hold_bars, n - 1)
                exit_price = closes[exit_bar]
                exit_reason = 'time_stop'

            # 平仓滑点
            exit_price_slipped = self.slippage.apply_close(
                exit_price, notional * remaining_ratio, vol_24h, 'SHORT'
            )

            # 剩余仓位 PnL
            remaining_pnl_pct = (entry_price_slipped - exit_price_slipped) / entry_price_slipped * 100
            remaining_notional = notional * remaining_ratio
            remaining_pnl = remaining_notional * remaining_pnl_pct / 100
            exit_fee = remaining_notional * cfg.fee_pct / 100
            remaining_pnl -= exit_fee

            # 汇总
            total_fee = entry_fee + exit_fee + (notional * tp1_close_ratio * cfg.fee_pct / 100 if tp1_done else 0)
            total_pnl = tp1_pnl_locked + remaining_pnl

            trade.exit_bar = exit_bar
            trade.exit_price = exit_price_slipped
            trade.pnl = remaining_pnl
            trade.tp1_pnl = tp1_pnl_locked
            trade.pnl_pct = round((trade.total_pnl / current_stake) * 100, 2)
            trade.fee_total = round(total_fee, 4)
            trade.hold_bars = exit_bar - entry_bar
            trade.hold_hours = trade.hold_bars * hours_per_bar
            trade.exit_reason = exit_reason

            trades.append(trade)
            equity += trade.total_pnl

            # 破产保护
            if equity <= cfg.initial_capital * 0.1:
                break

        return trades

    # ══════════════════════════════════════════════════════════════
    #  批量回测
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def batch_run(
        strategy: BaseStrategy,
        datasets: Dict[str, pd.DataFrame],
        config: Optional[BacktestConfig] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, BacktestResult]:
        """
        批量回测多个币种。

        参数:
          datasets: {symbol: ohlcv_dataframe}

        返回:
          {symbol: BacktestResult}
        """
        results = {}
        for symbol, df in datasets.items():
            bt = VectorizedBacktester(
                strategy=strategy,
                ohlcv_data=df,
                config=config,
                symbol=symbol,
            )
            results[symbol] = bt.run(params)
        return results

    @staticmethod
    def aggregate_results(results: Dict[str, BacktestResult]) -> BacktestMetrics:
        """汇总多币种回测结果"""
        all_pnls = []
        all_hours = []
        total_days = 0.0

        for result in results.values():
            all_pnls.extend([t.total_pnl for t in result.trades])
            all_hours.extend([t.hold_hours for t in result.trades])
            total_days = max(total_days, result.metrics.backtest_days)

        if not all_pnls:
            return BacktestMetrics()

        initial_capital = next(iter(results.values())).metrics.equity_curve[0] if results else 1000
        return calculate_metrics(
            trade_pnls=all_pnls,
            initial_capital=initial_capital,
            hold_hours=all_hours,
            backtest_days=total_days,
        )
