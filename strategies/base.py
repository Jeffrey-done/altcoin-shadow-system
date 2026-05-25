"""
策略基类定义
所有策略必须继承 BaseStrategy 并实现核心接口。

设计原则：
  - 策略只负责"产生信号"，不负责执行（执行由 Engine 层代理）
  - 每个策略有独立的参数命名空间，互不干扰
  - 策略可序列化参数（用于回测优化 + 持久化配置）
  - 支持多时间框架数据馈送
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional, Dict, Any


# ══════════════════════════════════════════════════════════════════
#  数据传输对象（策略 ↔ 引擎之间的契约）
# ══════════════════════════════════════════════════════════════════

class SignalDirection(str, Enum):
    SHORT = 'SHORT'
    LONG = 'LONG'


class ExitReason(str, Enum):
    HARD_STOP = 'hard_stop'
    TP1 = 'tp1'
    TP2 = 'tp2'
    TRAIL_STOP = 'trail_stop'
    BREAKEVEN_STOP = 'breakeven_stop'
    TIME_STOP = 'time_stop'
    MANUAL = 'manual'


@dataclass
class Signal:
    """
    策略产生的开仓信号。
    引擎收到后会做风控检查 → 执行路由 → 下单。
    """
    symbol: str
    direction: SignalDirection
    score: float                     # 信号评分 0~100
    stake: float                     # 建议保证金（风控可能调整）
    leverage: int = 10

    # 止盈止损建议（策略提供，引擎可以覆盖）
    hard_stop_pct: float = 5.0       # 硬止损 %
    tp1_pct: float = 5.0             # TP1 目标 %
    tp2_pct: float = 8.0             # TP2 目标 %
    trail_retrace_ratio: float = 0.4 # 移动止损回撤比
    max_hold_hours: int = 24         # 最大持仓时间

    # 策略元信息
    strategy_name: str = ''
    strategy_version: str = ''
    trigger_type: str = ''           # 具体触发方式（如 'abandon', '4h_rsi'）
    reason: str = ''                 # 人类可读的触发原因

    # 附加数据（策略自定义，传递给回测/统计）
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['direction'] = self.direction.value
        return d


@dataclass
class ExitSignal:
    """
    策略产生的平仓信号。
    """
    trade_id: str
    reason: ExitReason
    close_ratio: float = 1.0         # 平仓比例（0~1，TP1 一般 0.5）
    close_price: Optional[float] = None  # 建议平仓价（市价时为 None）
    pnl_estimate: float = 0.0        # 预估 PnL

    # 元信息
    strategy_name: str = ''
    description: str = ''            # 人类可读描述
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d['reason'] = self.reason.value
        return d


@dataclass
class Candidate:
    """
    策略扫描产生的候选对象。
    候选不等于信号——候选需要进一步确认后才可能产生 Signal。
    """
    symbol: str
    price: float
    score: float = 0.0               # 初步评分（扫描阶段粗筛）
    timeframe: str = '1d'            # 触发候选的时间框架
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketSnapshot:
    """
    市场快照 — Engine 传给策略的统一数据视图。
    策略不直接调用交易所 API，而是通过 DataFeed 获取快照。
    """
    tickers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    timestamp: Optional[str] = None


@dataclass
class TradeContext:
    """
    持仓上下文 — 评估退出时传给策略的当前持仓信息。
    """
    trade_id: str
    symbol: str
    direction: str
    entry_price: float
    current_price: float
    stake: float
    stake_remaining: float
    leverage: int
    shares: float
    opened_at: str
    pnl_pct: float                   # 当前浮动盈亏 %
    best_pnl_pct: float              # 历史最高浮动盈亏 %
    hold_hours: float                # 已持仓小时数
    tp1_triggered: bool
    tp1_locked_pnl: float
    hard_stop_price: Optional[float]
    trail_stop_price: Optional[float]
    exchange: str
    account_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ══════════════════════════════════════════════════════════════════
#  数据馈送接口（策略通过此接口获取市场数据）
# ══════════════════════════════════════════════════════════════════

class DataFeed(ABC):
    """
    数据馈送抽象接口。
    实盘模式由 ExchangeDataFeed 实现，回测模式由 BacktestDataFeed 实现。
    策略只依赖此接口，不直接调用 ccxt。
    """

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str, limit: int = 50) -> List[List[float]]:
        """获取 K 线数据 [[timestamp, open, high, low, close, volume], ...]"""
        ...

    @abstractmethod
    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取最新 ticker {'last': float, 'bid': float, 'ask': float, ...}"""
        ...

    @abstractmethod
    def get_tickers(self) -> Dict[str, Dict[str, Any]]:
        """获取全市场 tickers"""
        ...

    @abstractmethod
    def get_funding_rate(self, symbol: str) -> float:
        """获取当前资金费率 (%/8h)"""
        ...

    @abstractmethod
    def get_oi_change(self, symbol: str) -> float:
        """获取 OI 24h 变化率（0~1）"""
        ...

    @abstractmethod
    def get_orderbook(self, symbol: str, depth: int = 20) -> Dict[str, Any]:
        """获取订单簿 {'bids': [...], 'asks': [...]}"""
        ...


# ══════════════════════════════════════════════════════════════════
#  策略基类
# ══════════════════════════════════════════════════════════════════

class BaseStrategy(ABC):
    """
    策略基类 — 所有策略必须继承并实现以下核心方法。

    生命周期：
      1. __init__: 初始化参数
      2. scan(): 扫描市场 → 产生候选列表
      3. confirm(): 确认候选 → 产生开仓信号
      4. evaluate_exit(): 评估持仓 → 产生平仓信号
      5. on_trade_opened(): 开仓回调（可选）
      6. on_trade_closed(): 平仓回调（可选）

    策略不负责：
      - 连接交易所（通过 DataFeed 抽象）
      - 风控检查（由 RiskManager 执行）
      - 下单执行（由 OrderExecutor 执行）
      - 数据持久化（由 Repository 层负责）
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """策略唯一名称（英文标识符，如 'short_overbought'）"""
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """策略版本号（语义化版本，如 '5.0.0'）"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """策略描述（人类可读）"""
        ...

    @property
    def direction(self) -> SignalDirection:
        """策略默认方向（可被子类覆盖）"""
        return SignalDirection.SHORT

    # ── 核心方法 ──────────────────────────────────────────────────

    @abstractmethod
    def scan(self, data_feed: DataFeed, market: MarketSnapshot) -> List[Candidate]:
        """
        扫描市场，返回符合条件的候选列表。

        参数:
          data_feed: 数据馈送接口（获取 OHLCV / ticker / OI 等）
          market: 当前市场快照（全市场 tickers）

        返回:
          候选列表（空列表表示本轮无候选）

        调用频率: 由 scheduler 控制（如每小时一次）
        """
        ...

    @abstractmethod
    def confirm(self, candidate: Candidate, data_feed: DataFeed) -> Optional[Signal]:
        """
        确认候选是否触发开仓信号。

        参数:
          candidate: scan() 产生的候选对象
          data_feed: 数据馈送接口

        返回:
          Signal 对象（触发开仓）或 None（未触发，继续等待）

        调用频率: 由 scheduler 控制（如每 5~15 分钟检查一次候选池）
        """
        ...

    @abstractmethod
    def evaluate_exit(self, trade: TradeContext, data_feed: DataFeed) -> Optional[ExitSignal]:
        """
        评估持仓是否需要平仓。

        参数:
          trade: 当前持仓上下文
          data_feed: 数据馈送接口

        返回:
          ExitSignal 对象（触发平仓）或 None（继续持有）

        调用频率: 实时（WebSocket tick 触发）或定时（每 10 分钟）
        """
        ...

    # ── 参数管理 ─────────────────────────────────────────────────

    @abstractmethod
    def get_params(self) -> Dict[str, Any]:
        """
        返回当前策略所有参数。
        用于：日志记录、回测对照、参数优化、admin panel 展示。
        """
        ...

    @abstractmethod
    def set_params(self, params: Dict[str, Any]) -> None:
        """
        设置策略参数（用于参数优化时动态调整）。
        只接受 get_params() 返回的 key。
        """
        ...

    def get_param_space(self) -> Dict[str, Dict[str, Any]]:
        """
        返回参数优化空间定义（可选实现）。
        用于 Optuna / grid search 自动探索。

        返回格式:
          {
            'param_name': {
              'type': 'int' | 'float' | 'categorical',
              'low': 最小值,
              'high': 最大值,
              'step': 步长（可选）,
              'choices': [选项]（categorical 时）,
            },
          }
        """
        return {}

    # ── 生命周期回调（可选覆盖）─────────────────────────────────

    def on_trade_opened(self, trade_id: str, signal: Signal) -> None:
        """开仓成功后的回调（如清理候选池、记录日志）"""
        pass

    def on_trade_closed(self, trade_id: str, pnl: float, reason: ExitReason) -> None:
        """平仓后的回调（如更新统计、调整参数）"""
        pass

    def on_tick(self, symbol: str, price: float) -> None:
        """实时价格更新回调（可选，用于需要 tick 级别响应的策略）"""
        pass

    # ── 工具方法 ─────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} v={self.version}>"
