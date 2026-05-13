#!/usr/bin/env python3
"""
Walk-Forward Analysis (WFA) v1.0

问题:
  传统 "全样本网格搜索" (backtest.grid_search) 把所有历史数据参与参数选择,
  选出来的 "最佳参数" 通常是过拟合历史噪声的结果。实盘业绩会大幅低于回测。

解决:
  滚动切片 — 用前 N 天 (in-sample, IS) 选参数,然后在紧接着的 M 天 (out-of-sample, OOS)
  上应用该参数,记录 OOS 业绩。窗口滚动重复,最后把所有 OOS 段拼接起来
  得到 "从未参与过参数选择的数据" 的业绩。这才是对未来业绩的诚实估计。

术语:
  train_days:  IS 训练窗口长度
  test_days:   OOS 测试窗口长度
  step_days:   窗口每次滚动的天数 (通常 = test_days)
  anchor:      滚动模式 —
               'rolling'  : 每次 IS 起点和终点都向前推 step_days (固定窗口宽度)
               'anchored' : IS 起点固定,终点向前推 (样本随时间变多)

关键指标:
  OOS Sharpe           真实可预期的业绩
  IS/OOS Sharpe ratio  过拟合指标 — 越接近 1 越稳, << 1 意味着严重过拟合
  参数稳定性           各窗口 best_params 的离散度 — 高度漂移意味着策略对参数敏感

用法:
  程序调用:
    from walk_forward import walk_forward_analysis, default_param_grid
    result = walk_forward_analysis(
        symbol='PEPE/USDT', total_days=180,
        train_days=60, test_days=20, step_days=20,
        param_grid=default_param_grid(),
    )
    print(result.summary_text())

  CLI:
    python3 walk_forward.py --symbol PEPE/USDT --total-days 180 \\
                            --train-days 60 --test-days 20

作用域限制 (本 PR):
  - 不改 auto_optimize.py (避免 blast radius)
  - 不引入新依赖 (只用 ccxt + 标准库)
  - 生成的参数选择结果 不 自动被 scanner 使用,仅作为观察诊断
"""

from __future__ import annotations

import argparse
import itertools
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import setup_logger, atomic_write_json
from backtest import (
    BacktestParams, BacktestResult,
    calculate_stats, detect_entry_signals, simulate_trade,
    load_cached_klines,
)

logger = setup_logger("walk_forward")


# ══════════════════════════════════════════════════════════════════
#  数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class WindowResult:
    """单个 train/test 窗口的结果"""
    window_index: int
    train_start_idx: int
    train_end_idx: int
    test_start_idx: int
    test_end_idx: int
    train_period_start_time: str
    train_period_end_time: str
    test_period_start_time: str
    test_period_end_time: str
    best_params: dict
    # IS: 训练期回测结果 (全样本搜索的最佳参数在训练期上的业绩)
    is_pnl: float
    is_win_rate: float
    is_trades: int
    is_sharpe: float
    is_max_dd: float
    # OOS: 测试期用同一套参数跑的业绩
    oos_pnl: float
    oos_win_rate: float
    oos_trades: int
    oos_sharpe: float
    oos_max_dd: float
    # OOS 每笔交易明细(保留给 equity curve 聚合)
    oos_trade_pnls: List[float] = field(default_factory=list)


@dataclass
class WFAResult:
    """完整的 WFA 分析结果"""
    symbol: str
    total_bars: int
    train_days: int
    test_days: int
    step_days: int
    anchor: str
    param_grid_size: int
    windows: List[WindowResult] = field(default_factory=list)
    # 汇总 (基于拼接后的 OOS 段)
    oos_total_pnl: float = 0.0
    oos_total_trades: int = 0
    oos_win_rate: float = 0.0
    oos_sharpe: float = 0.0
    oos_max_drawdown: float = 0.0
    is_total_pnl: float = 0.0
    is_total_trades: int = 0
    is_sharpe: float = 0.0
    # 过拟合度量: OOS sharpe / IS sharpe (越接近 1 越好)
    overfit_ratio: float = 0.0
    # 参数稳定性 — 按每个参数维度算 CV (std/mean)
    parameter_stability: Dict[str, float] = field(default_factory=dict)
    # 权益曲线 (拼接每个 OOS 段的交易 pnl)
    oos_equity_curve: List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    # ══════════════════════════════════════════════════════════════
    #  人类可读的总结
    # ══════════════════════════════════════════════════════════════

    def summary_text(self) -> str:
        lines = []
        lines.append("=" * 72)
        lines.append(f"  Walk-Forward Analysis: {self.symbol}")
        lines.append("=" * 72)
        lines.append(
            f"  窗口配置: train={self.train_days}d / test={self.test_days}d / "
            f"step={self.step_days}d | anchor={self.anchor} | 参数组合={self.param_grid_size}"
        )
        lines.append(f"  完成窗口: {len(self.windows)} 个")
        lines.append("")
        lines.append("  OOS (样本外,诚实指标):")
        lines.append(f"    总盈亏:       {self.oos_total_pnl:+.2f}U")
        lines.append(f"    总交易数:     {self.oos_total_trades}")
        lines.append(f"    胜率:         {self.oos_win_rate:.1f}%")
        lines.append(f"    夏普率:       {self.oos_sharpe:.2f}")
        lines.append(f"    最大回撤:     {self.oos_max_drawdown:.1f}%")
        lines.append("")
        lines.append("  IS (样本内,参考):")
        lines.append(f"    总盈亏:       {self.is_total_pnl:+.2f}U")
        lines.append(f"    总交易数:     {self.is_total_trades}")
        lines.append(f"    夏普率:       {self.is_sharpe:.2f}")
        lines.append("")
        lines.append("  过拟合指标:")
        lines.append(f"    OOS/IS Sharpe: {self.overfit_ratio:.2f}")
        if self.overfit_ratio >= 0.7:
            lines.append("    判定:         ✅ 稳健 (OOS 保留了 IS 业绩的大部分)")
        elif self.overfit_ratio >= 0.3:
            lines.append("    判定:         ⚠️ 中等过拟合 (OOS 业绩明显逊色于 IS)")
        elif self.overfit_ratio > 0:
            lines.append("    判定:         ❌ 严重过拟合 (策略主要靠拟合历史噪声)")
        else:
            lines.append("    判定:         ❌ OOS 负收益 — 无真实边缘")
        lines.append("")
        if self.parameter_stability:
            lines.append("  参数稳定性 (CV = std/|mean|, 越低越稳):")
            for k, v in sorted(self.parameter_stability.items(),
                               key=lambda x: x[1], reverse=True):
                flag = "⚠️" if v > 0.3 else "✓"
                lines.append(f"    {flag} {k:<25} {v:.2f}")
        lines.append("")
        lines.append("  每个窗口 OOS 明细:")
        for w in self.windows:
            emoji = "📈" if w.oos_pnl >= 0 else "📉"
            lines.append(
                f"    #{w.window_index:<2} "
                f"{w.test_period_start_time[:10]} → {w.test_period_end_time[:10]} "
                f"{emoji} PnL {w.oos_pnl:+7.2f}U  胜率{w.oos_win_rate:4.1f}%  "
                f"交易{w.oos_trades}笔"
            )
        lines.append("=" * 72)
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  默认参数网格 (与 backtest.grid_search 保持一致)
# ══════════════════════════════════════════════════════════════════

def default_param_grid() -> Dict[str, List]:
    """
    默认搜索网格。与 backtest.grid_search 对齐但留足裕度:
      5 * 5 * 3 * 4 * 3 = 300 个组合
    """
    return {
        'tp1_pct': [3, 5, 7],
        'tp2_pct': [8, 10, 15],
        'hard_stop_pct': [2, 3, 5],
        'daily_rsi_min': [72, 75, 78, 82],
        'h4_rsi_drop': [8, 10, 12],
    }


def _enumerate_grid(grid: Dict[str, List]) -> List[dict]:
    """
    把维度字典展开成 [{'tp1_pct':3, 'tp2_pct':8, ...}, ...]
    自动丢弃 tp2 <= tp1 的无效组合。
    """
    keys = list(grid.keys())
    out = []
    for vals in itertools.product(*[grid[k] for k in keys]):
        combo = dict(zip(keys, vals))
        if 'tp1_pct' in combo and 'tp2_pct' in combo:
            if combo['tp2_pct'] <= combo['tp1_pct']:
                continue
        out.append(combo)
    return out


# ══════════════════════════════════════════════════════════════════
#  单个窗口回测 (IS or OOS)
# ══════════════════════════════════════════════════════════════════

def _run_window(
    klines_window: List[dict],
    params: BacktestParams,
    symbol: str = '',
) -> BacktestResult:
    """
    在给定 K 线窗口上跑一次信号检测 + 交易模拟 + 统计。
    纯函数,不调 API,不读写磁盘 — WFA 的热路径。
    """
    if len(klines_window) < 30:
        # 数据太少,返回空结果
        return calculate_stats([], params)

    signals = detect_entry_signals(klines_window, params)
    # 确保 entry_idx+1 有效(信号模拟函数依赖下一根 bar)
    signals = [s for s in signals if s + 1 < len(klines_window)]

    trades = []
    for sig_idx in signals:
        t = simulate_trade(klines_window, sig_idx, params)
        t.symbol = symbol
        trades.append(t)

    return calculate_stats(trades, params)


def _pick_best_params(
    klines_train: List[dict],
    grid_combos: List[dict],
    selection_metric: Callable[[BacktestResult], float] = lambda r: r.total_pnl,
    min_trades: int = 3,
) -> tuple[BacktestParams, BacktestResult]:
    """
    在训练窗口上遍历参数网格,返回 (best_params, best_result)。

    selection_metric: 用于选最佳参数的打分函数
      默认按 total_pnl 最大化 (与 backtest.grid_search 一致)

    min_trades: 训练期内必须至少产出多少笔交易,否则该参数组合被视为无效。
      防止选中一个在训练期几乎不触发的组合 (样本太少,业绩高度偶然)。
    """
    best_result: Optional[BacktestResult] = None
    best_params: Optional[BacktestParams] = None
    best_score = float('-inf')

    for combo in grid_combos:
        params = BacktestParams(**combo)
        result = _run_window(klines_train, params)
        # 样本过少的组合被排除
        if result.total_trades < min_trades:
            continue
        score = selection_metric(result)
        if score > best_score:
            best_score = score
            best_result = result
            best_params = params

    # 如果整个 grid 都没有足够交易的组合,放宽 min_trades 再选一次
    if best_result is None:
        for combo in grid_combos:
            params = BacktestParams(**combo)
            result = _run_window(klines_train, params)
            score = selection_metric(result)
            if score > best_score:
                best_score = score
                best_result = result
                best_params = params

    # 最终仍空 — 空网格场景 — 返回默认参数 + 空结果
    if best_params is None:
        best_params = BacktestParams()
        best_result = calculate_stats([], best_params)

    return best_params, best_result


# ══════════════════════════════════════════════════════════════════
#  权益曲线 / 最大回撤 辅助
# ══════════════════════════════════════════════════════════════════

def _pnls_to_equity_curve(start_balance: float, pnls: Sequence[float]) -> List[float]:
    eq = [start_balance]
    for p in pnls:
        eq.append(eq[-1] + p)
    return eq


def _max_drawdown_pct(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def _simple_sharpe(pnls: Sequence[float]) -> float:
    """与 backtest.calculate_stats 的夏普近似公式一致"""
    if len(pnls) <= 1:
        return 0.0
    mean = statistics.mean(pnls)
    std = statistics.stdev(pnls)
    if std == 0:
        return 0.0
    # 与 backtest 一致:乘以 sqrt(365)/10 年化近似
    return round((mean / std) * (365 ** 0.5 / 10), 2)


# ══════════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════════

def walk_forward_analysis(
    symbol: str,
    total_days: int = 180,
    train_days: int = 60,
    test_days: int = 20,
    step_days: Optional[int] = None,
    param_grid: Optional[Dict[str, List]] = None,
    anchor: str = 'rolling',
    klines: Optional[List[dict]] = None,
    selection_metric: Optional[Callable[[BacktestResult], float]] = None,
    min_trades_is: int = 3,
    start_balance: float = 1000.0,
) -> WFAResult:
    """
    执行 Walk-Forward Analysis。

    参数:
      symbol:       交易对(仅用于日志/报告标记,和 klines 参数二选一)
      total_days:   总回测天数 (klines=None 时从 Binance 拉取)
      train_days:   每个 IS 窗口宽度 (天)
      test_days:    每个 OOS 窗口宽度 (天)
      step_days:    窗口滚动步长 (默认 = test_days)
      param_grid:   参数网格 dict,None 用 default_param_grid()
      anchor:       'rolling' 或 'anchored'
      klines:       可选,直接传入预加载的 1h K 线 (测试用)
      selection_metric: 选参函数,默认 total_pnl
      min_trades_is:    训练期有效参数组合需要的最少交易数
      start_balance:    权益曲线起点

    返回: WFAResult
    """
    if step_days is None:
        step_days = test_days
    if param_grid is None:
        param_grid = default_param_grid()
    if selection_metric is None:
        selection_metric = lambda r: r.total_pnl  # noqa: E731

    if anchor not in ('rolling', 'anchored'):
        raise ValueError(f"anchor 必须是 'rolling' 或 'anchored', 得到 {anchor!r}")

    # 加载 K 线
    if klines is None:
        klines = load_cached_klines(symbol, '1h', total_days)
    if not klines:
        logger.error(f"无可用 K 线数据: {symbol}")
        return WFAResult(
            symbol=symbol, total_bars=0, train_days=train_days,
            test_days=test_days, step_days=step_days, anchor=anchor,
            param_grid_size=0,
        )

    bars_per_day = 24  # 1h K 线
    train_bars = train_days * bars_per_day
    test_bars = test_days * bars_per_day
    step_bars = step_days * bars_per_day

    grid_combos = _enumerate_grid(param_grid)

    logger.info(
        f"WFA 启动: {symbol} | 总 K 线={len(klines)} | "
        f"train={train_bars} bars ({train_days}d) | "
        f"test={test_bars} bars ({test_days}d) | step={step_bars} bars | "
        f"param grid={len(grid_combos)} combos | anchor={anchor}"
    )

    # 窗口切分
    wfa = WFAResult(
        symbol=symbol,
        total_bars=len(klines),
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        anchor=anchor,
        param_grid_size=len(grid_combos),
    )

    window_idx = 0
    train_start = 0
    oos_pnls_all: List[float] = []
    is_pnls_all: List[float] = []
    oos_trades_count = 0
    is_trades_count = 0
    oos_wins = 0

    while True:
        if anchor == 'rolling':
            tr_start = train_start
            tr_end = tr_start + train_bars
        else:  # anchored
            tr_start = 0
            tr_end = train_bars + window_idx * step_bars

        te_start = tr_end
        te_end = te_start + test_bars

        # 数据不够就停
        if te_end > len(klines):
            break

        klines_train = klines[tr_start:tr_end]
        klines_test = klines[te_start:te_end]

        # 1) 在训练期选参
        t0 = time.time()
        best_params, is_result = _pick_best_params(
            klines_train, grid_combos,
            selection_metric=selection_metric,
            min_trades=min_trades_is,
        )
        pick_dt = time.time() - t0

        # 2) 用选出的参数在 OOS 跑
        oos_result = _run_window(klines_test, best_params, symbol=symbol)

        wr = WindowResult(
            window_index=window_idx,
            train_start_idx=tr_start,
            train_end_idx=tr_end,
            test_start_idx=te_start,
            test_end_idx=te_end,
            train_period_start_time=klines_train[0]['time'] if klines_train else '',
            train_period_end_time=klines_train[-1]['time'] if klines_train else '',
            test_period_start_time=klines_test[0]['time'] if klines_test else '',
            test_period_end_time=klines_test[-1]['time'] if klines_test else '',
            best_params=asdict(best_params),
            is_pnl=is_result.total_pnl,
            is_win_rate=is_result.win_rate,
            is_trades=is_result.total_trades,
            is_sharpe=is_result.sharpe_ratio,
            is_max_dd=is_result.max_drawdown,
            oos_pnl=oos_result.total_pnl,
            oos_win_rate=oos_result.win_rate,
            oos_trades=oos_result.total_trades,
            oos_sharpe=oos_result.sharpe_ratio,
            oos_max_dd=oos_result.max_drawdown,
            oos_trade_pnls=[t.pnl_usd for t in oos_result.trades],
        )
        wfa.windows.append(wr)

        # 聚合
        is_pnls_all.extend([t.pnl_usd for t in is_result.trades])
        oos_pnls_all.extend(wr.oos_trade_pnls)
        oos_trades_count += wr.oos_trades
        is_trades_count += wr.is_trades
        oos_wins += sum(1 for p in wr.oos_trade_pnls if p > 0)

        logger.info(
            f"  窗口 #{window_idx}: train=[{tr_start}:{tr_end}] "
            f"test=[{te_start}:{te_end}] | IS PnL={is_result.total_pnl:+.2f}U "
            f"OOS PnL={oos_result.total_pnl:+.2f}U (选参用时 {pick_dt:.1f}s)"
        )

        window_idx += 1
        train_start += step_bars

    # ══ 汇总 ══
    wfa.oos_total_pnl = round(sum(oos_pnls_all), 2)
    wfa.oos_total_trades = oos_trades_count
    wfa.oos_win_rate = round(oos_wins / oos_trades_count * 100, 1) if oos_trades_count else 0.0
    wfa.oos_sharpe = _simple_sharpe(oos_pnls_all)
    wfa.is_total_pnl = round(sum(is_pnls_all), 2)
    wfa.is_total_trades = is_trades_count
    wfa.is_sharpe = _simple_sharpe(is_pnls_all)
    wfa.oos_equity_curve = _pnls_to_equity_curve(start_balance, oos_pnls_all)
    wfa.oos_max_drawdown = _max_drawdown_pct(wfa.oos_equity_curve)

    # 过拟合比:OOS sharpe / IS sharpe
    # IS=0 时无法归一化 — 退到 OOS 符号本身
    if wfa.is_sharpe > 0:
        wfa.overfit_ratio = round(wfa.oos_sharpe / wfa.is_sharpe, 3)
    elif wfa.is_sharpe == 0 and wfa.oos_sharpe == 0:
        wfa.overfit_ratio = 0.0
    else:
        # IS 非正(IS 都赚不到钱)+ OOS 有值 → 比率没有实际含义
        wfa.overfit_ratio = 0.0

    # 参数稳定性 (对每个数值维度计算 CV = std/|mean|)
    if wfa.windows:
        for k in wfa.windows[0].best_params.keys():
            values = [w.best_params.get(k) for w in wfa.windows]
            numeric_values = [float(v) for v in values
                              if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if len(numeric_values) <= 1:
                continue
            m = statistics.mean(numeric_values)
            if m == 0:
                continue
            sd = statistics.stdev(numeric_values)
            wfa.parameter_stability[k] = round(sd / abs(m), 3)

    return wfa


def save_wfa_result(result: WFAResult, path: str) -> None:
    """落盘 WFAResult 为 JSON (方便 dashboard 或 CLI 后续读)"""
    atomic_write_json(path, result.to_dict())
    logger.info(f"WFA 结果已保存: {path}")


# ══════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════

def _main():
    parser = argparse.ArgumentParser(description='Walk-Forward Analysis')
    parser.add_argument('--symbol', default='PEPE/USDT', help='交易对')
    parser.add_argument('--total-days', type=int, default=180)
    parser.add_argument('--train-days', type=int, default=60)
    parser.add_argument('--test-days', type=int, default=20)
    parser.add_argument('--step-days', type=int, default=None)
    parser.add_argument('--anchor', choices=['rolling', 'anchored'], default='rolling')
    parser.add_argument('--output', default='',
                        help='把 WFA 结果落到 JSON 文件(可选)')
    args = parser.parse_args()

    result = walk_forward_analysis(
        symbol=args.symbol,
        total_days=args.total_days,
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
        anchor=args.anchor,
    )
    print(result.summary_text())

    if args.output:
        save_wfa_result(result, args.output)


if __name__ == '__main__':
    _main()
