"""
回测引擎层（M2 修复 — 2026-05）

历史背景
========
项目并存两套回测实现：

  * ``backtest.py``               — 旧版（2300+ 行）：CLI / 网格搜索主战场
                                     - 支持 OKX 交叉验证、量价背离、ML 评分等"全特性"
                                     - ``run_backtest`` 既能跑事件驱动主循环，也能跑 _legacy
                                     - 调用方：CLI、grid_search、batch_backtest
  * ``backtesting.engine``        — 新版（500 行）：``VectorizedBacktester``
                                     - 向量化指标计算，10~50× 性能
                                     - 调用方：walk_forward、optimization、parallel

两套引擎的输出 schema 不同（``BacktestResult`` 在两个文件里各有一套），
新人想跑回测要先猜该用哪个；优化器和 CLI 之间无法对照结果。

M2 修复
=======
本模块作为**唯一的对外回测入口**，提供 ``run()`` / ``run_legacy()`` /
``run_vectorized()`` 三个高层函数：

  * ``run(symbol, days, params=None, engine='auto')``
      - engine='auto'        — 默认按场景智能选择（grid → legacy；single → vectorized）
      - engine='legacy'      — 强制走 ``backtest.run_backtest`` (含全部 bonus / OKX CV)
      - engine='vectorized'  — 强制走 ``VectorizedBacktester`` (高性能)
      - 返回统一的 :class:`UnifiedBacktestResult`
  * 旧 API 不变（backtest.py / VectorizedBacktester 的接口都保留），
    本模块仅作"统一入口 + 结果归一化"。

调用方迁移：
  - CLI / grid_search → 继续用 ``backtest.run_backtest`` 没问题
  - 新代码请只 ``from backtesting import run``
  - 长期目标：legacy 引擎仅保留必要特性后并入 ``VectorizedBacktester``

详见 ``docs/UNIFIED_ARCHITECTURE.md``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backtesting")


@dataclass
class UnifiedBacktestResult:
    """两个引擎结果的统一外壳"""
    engine: str = ''                 # 'legacy' | 'vectorized'
    symbol: str = ''
    days: int = 0
    total_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    trades: List[Any] = field(default_factory=list)   # 原始 trade 对象（引擎特定）
    metrics: Dict[str, Any] = field(default_factory=dict)  # 引擎特定附加指标
    raw: Any = None                  # 引擎原生 result（不丢信息）

    def to_dict(self) -> Dict[str, Any]:
        return {
            'engine': self.engine,
            'symbol': self.symbol,
            'days': self.days,
            'total_trades': self.total_trades,
            'win_rate': self.win_rate,
            'total_pnl': self.total_pnl,
            'total_pnl_pct': self.total_pnl_pct,
            'max_drawdown': self.max_drawdown,
            'sharpe_ratio': self.sharpe_ratio,
            'profit_factor': self.profit_factor,
            'metrics': self.metrics,
        }


def _wrap_legacy_result(r: Any, symbol: str, days: int) -> UnifiedBacktestResult:
    """把 backtest.BacktestResult 投影到 UnifiedBacktestResult"""
    out = UnifiedBacktestResult(engine='legacy', symbol=symbol, days=days, raw=r)
    if r is None:
        return out
    out.total_trades = int(getattr(r, 'total_trades', 0) or 0)
    out.win_rate = float(getattr(r, 'win_rate', 0.0) or 0.0)
    out.total_pnl = float(getattr(r, 'total_pnl', 0.0) or 0.0)
    out.total_pnl_pct = float(getattr(r, 'total_pnl_pct', 0.0) or 0.0)
    out.max_drawdown = float(getattr(r, 'max_drawdown_pct', 0.0) or 0.0)
    out.sharpe_ratio = float(getattr(r, 'sharpe_ratio', 0.0) or 0.0)
    out.profit_factor = float(getattr(r, 'profit_factor', 0.0) or 0.0)
    out.trades = list(getattr(r, 'trades', []) or [])
    return out


def _wrap_vectorized_result(r: Any, symbol: str, days: int) -> UnifiedBacktestResult:
    """把 backtesting.engine.BacktestResult → UnifiedBacktestResult"""
    out = UnifiedBacktestResult(engine='vectorized', symbol=symbol, days=days, raw=r)
    if r is None:
        return out
    m = getattr(r, 'metrics', None)
    if m is not None:
        out.total_trades = int(getattr(m, 'total_trades', 0) or 0)
        out.win_rate = float(getattr(m, 'win_rate', 0.0) or 0.0)
        out.total_pnl = float(getattr(m, 'total_pnl', 0.0) or 0.0)
        out.total_pnl_pct = float(getattr(m, 'total_return_pct', 0.0) or 0.0)
        out.max_drawdown = float(getattr(m, 'max_drawdown_pct', 0.0) or 0.0)
        out.sharpe_ratio = float(getattr(m, 'sharpe_ratio', 0.0) or 0.0)
        out.profit_factor = float(getattr(m, 'profit_factor', 0.0) or 0.0)
    out.trades = list(getattr(r, 'trades', []) or [])
    out.metrics = {
        'elapsed_sec': getattr(r, 'elapsed_sec', 0.0),
        'bars_processed': getattr(r, 'bars_processed', 0),
    }
    return out


def run(
    symbol: str,
    days: int = 90,
    params: Optional[Any] = None,
    *,
    engine: str = 'auto',
    **kwargs,
) -> UnifiedBacktestResult:
    """
    统一回测入口（M2 修复）。

    Args:
        symbol: 'PEPE/USDT' 等
        days:   回测天数
        params: 引擎特定参数。legacy → ``backtest.BacktestParams``；
                vectorized → 字典，会被映射到 ``BacktestConfig``。
                None 时使用默认值。
        engine: 'auto' | 'legacy' | 'vectorized'
                - 'auto': 调用方未传 ``ohlcv_df`` 等向量化专用参数 → 走 legacy；
                          否则走 vectorized。当前实现简单偏向 legacy
                          以保留全部业务特性。
        **kwargs: 透传给底层引擎

    Returns:
        :class:`UnifiedBacktestResult`，含归一化字段 + 原始 raw 对象。
    """
    eff_engine = _choose_engine(engine, kwargs)
    if eff_engine == 'legacy':
        return run_legacy(symbol, days=days, params=params, **kwargs)
    return run_vectorized(symbol, days=days, params=params, **kwargs)


def run_legacy(symbol: str, days: int = 90,
               params: Optional[Any] = None, **kwargs) -> UnifiedBacktestResult:
    """显式调用旧 ``backtest.run_backtest``（保留全特性）"""
    from backtest import run_backtest
    raw = run_backtest(symbol, days=days, params=params, **kwargs)
    return _wrap_legacy_result(raw, symbol, days)


def run_vectorized(symbol: str, days: int = 90,
                   params: Optional[Any] = None,
                   ohlcv_df=None, strategy=None,
                   **kwargs) -> UnifiedBacktestResult:
    """显式调用 ``VectorizedBacktester``（高性能向量化）"""
    from backtesting.engine import VectorizedBacktester, BacktestConfig

    if strategy is None:
        from strategies.short_overbought import ShortOverboughtStrategy
        strategy = ShortOverboughtStrategy()

    # 把 dict params 映射到 BacktestConfig
    cfg = BacktestConfig()
    if isinstance(params, dict):
        for k, v in params.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)

    if ohlcv_df is None:
        raise ValueError(
            'run_vectorized 需要预加载 OHLCV DataFrame；'
            '通过 ohlcv_df= 参数传入，或改用 run_legacy（自动取数）'
        )

    bt = VectorizedBacktester(
        strategy=strategy,
        ohlcv_data=ohlcv_df,
        config=cfg,
        symbol=symbol,
        **kwargs,
    )
    raw = bt.run()
    return _wrap_vectorized_result(raw, symbol, days)


def _choose_engine(engine: str, kwargs: Dict[str, Any]) -> str:
    """auto 模式选择策略"""
    if engine in ('legacy', 'vectorized'):
        return engine
    if engine != 'auto':
        raise ValueError(f"未知 engine: {engine!r}")
    # ohlcv_df 已经准备好 → vectorized；否则 legacy 自动取数更省事
    if kwargs.get('ohlcv_df') is not None:
        return 'vectorized'
    return 'legacy'


__all__ = [
    'UnifiedBacktestResult',
    'run',
    'run_legacy',
    'run_vectorized',
]
