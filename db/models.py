"""
SQLAlchemy ORM 模型定义
完整覆盖系统所有数据实体：Trade, Candidate, RiskState, ExecutionEvent, TaskMetric

设计原则：
  - 字段与原 dataclass 模型一一对应（保证迁移无损）
  - 所有时间字段统一 UTC timezone-aware
  - 索引覆盖所有常用查询模式
  - 支持多账户隔离（account_id 贯穿所有表）
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Float, Integer, Boolean, Text, DateTime,
    Index, Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase
import enum


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ══════════════════════════════════════════════════════════════════
#  枚举
# ══════════════════════════════════════════════════════════════════

class TradeStatus(str, enum.Enum):
    OPEN = 'open'
    CLOSED = 'closed'


class TradeDirection(str, enum.Enum):
    SHORT = 'SHORT'
    LONG = 'LONG'


class CloseTypeEnum(str, enum.Enum):
    HARD_STOP = 'hard_stop'
    TP1 = 'tp1'
    TP2 = 'tp2'
    TRAIL_STOP = 'trail_stop'
    BREAKEVEN_STOP = 'breakeven_stop'
    TIME_STOP = 'time_stop'
    MANUAL = 'manual'

    @classmethod
    def is_stop_loss(cls, value) -> bool:
        if value is None:
            return False
        v = value.value if isinstance(value, cls) else str(value)
        return v in (cls.HARD_STOP.value, cls.TRAIL_STOP.value, cls.TIME_STOP.value)


class ExchangeName(str, enum.Enum):
    SHADOW = 'shadow'
    BINANCE = 'binance'
    OKX = 'okx'


# ══════════════════════════════════════════════════════════════════
#  Trade 表
# ══════════════════════════════════════════════════════════════════

class TradeModel(Base):
    """交易记录表 — 核心表"""
    __tablename__ = 'trades'

    id = Column(String(128), primary_key=True)
    symbol = Column(String(32), nullable=False, index=True)
    direction = Column(String(8), nullable=False, default='SHORT')
    strategy = Column(String(64), nullable=False, default='short_overbought', index=True)
    status = Column(String(16), nullable=False, default='open', index=True)

    # 价格 & 仓位
    entry_price = Column(Float, nullable=False)
    stake = Column(Float, nullable=False)
    leverage = Column(Integer, nullable=False, default=10)
    notional = Column(Float, nullable=False, default=0.0)
    shares = Column(Float, nullable=False, default=0.0)

    # 止盈档位
    take_profit_1 = Column(Float, default=0.0)
    take_profit_2 = Column(Float, default=0.0)
    tp1_triggered = Column(Boolean, default=False)
    tp1_locked_pnl = Column(Float, default=0.0)
    stake_remaining = Column(Float, default=0.0)

    # 硬止损
    hard_stop_price = Column(Float, nullable=True)

    # 移动止损
    best_pnl_pct = Column(Float, default=0.0)
    trail_stop_price = Column(Float, nullable=True)

    # 其他止损
    stop_loss = Column(Float, nullable=True)
    max_hold_days = Column(Integer, default=1)

    # 结算
    pnl = Column(Float, default=0.0)
    current_price = Column(Float, nullable=True)
    close_reason = Column(Text, nullable=True)
    close_type = Column(String(32), nullable=True)

    # 实盘路由
    exchange = Column(String(16), nullable=False, default='shadow')
    live_order_id = Column(String(128), nullable=True)
    close_order_id = Column(String(128), nullable=True)
    tp1_close_order_id = Column(String(128), nullable=True)
    client_order_id = Column(String(128), default='')

    # 多账户隔离
    account_id = Column(String(64), nullable=False, default='', index=True)

    # 滑点追踪
    ref_price_at_order = Column(Float, default=0.0)
    slippage_pct = Column(Float, default=0.0)
    tp1_closed_shares = Column(Float, default=0.0)
    tp1_exit_price = Column(Float, default=0.0)
    tp1_exit_ref_price = Column(Float, default=0.0)
    tp1_slippage_pct = Column(Float, default=0.0)
    exit_ref_price = Column(Float, default=0.0)
    exit_slippage_pct = Column(Float, default=0.0)

    # 保护单
    protect_stop_algo_id = Column(String(128), nullable=True)
    protect_tp_algo_id = Column(String(128), nullable=True)
    protect_stage = Column(String(16), default='')

    # 元信息
    source = Column(String(128), default='扫描器自动开仓')
    reason = Column(Text, default='')
    opened_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index('ix_trades_status_account', 'status', 'account_id'),
        Index('ix_trades_symbol_status', 'symbol', 'status'),
        Index('ix_trades_strategy_status', 'strategy', 'status'),
        Index('ix_trades_opened_at', 'opened_at'),
        Index('ix_trades_closed_at', 'closed_at'),
    )

    def to_dict(self) -> dict:
        """兼容旧代码的 dict 导出"""
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


# ══════════════════════════════════════════════════════════════════
#  Candidate 表
# ══════════════════════════════════════════════════════════════════

class CandidateModel(Base):
    """候选币表 — 多策略支持，同一 symbol 可被不同策略各自持有"""
    __tablename__ = 'candidates'

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    strategy = Column(String(64), nullable=False, default='short_overbought', index=True)
    direction = Column(String(8), nullable=False, default='SHORT')  # SHORT | LONG

    price = Column(Float, nullable=False)
    vol24h = Column(Float, default=0.0)
    pct24h = Column(Float, default=0.0)
    score = Column(Float, default=0.0)  # 策略扫描阶段的初步评分

    # 通用指标
    rsi_1d = Column(Float, default=50.0)
    rsi_4h = Column(Float, nullable=True)
    rsi_4h_peak = Column(Float, nullable=True)
    oi_change = Column(Float, default=0.0)
    funding_rate = Column(Float, default=0.0)

    # short_overbought 专用（其他策略可为 NULL）
    yao_score = Column(Integer, nullable=True, default=0)

    # 策略自定义元数据（JSON 序列化，存放策略独有指标）
    metadata_json = Column(Text, nullable=True)

    triggered = Column(Boolean, default=False)
    trigger_type = Column(String(16), nullable=True)
    trigger_reason = Column(Text, nullable=True)

    # 待开仓状态
    pending_open = Column(Boolean, default=False)
    pending_opened_at = Column(DateTime(timezone=True), nullable=True)
    pending_open_retries = Column(Integer, default=0)

    # 时间
    added_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index('ix_candidates_symbol_strategy', 'symbol', 'strategy', unique=True),
        Index('ix_candidates_triggered', 'triggered'),
        Index('ix_candidates_added_at', 'added_at'),
        Index('ix_candidates_strategy', 'strategy'),
        Index('ix_candidates_direction', 'direction'),
    )

    def to_dict(self) -> dict:
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        # 解析 metadata_json 为 dict 方便消费端使用
        if d.get('metadata_json'):
            try:
                import json
                d['metadata'] = json.loads(d['metadata_json'])
            except (ValueError, TypeError):
                d['metadata'] = {}
        else:
            d['metadata'] = {}
        return d


# ══════════════════════════════════════════════════════════════════
#  RiskState 表
# ══════════════════════════════════════════════════════════════════

class RiskStateModel(Base):
    """风控状态表 — 每个账户每天一行"""
    __tablename__ = 'risk_states'

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String(64), nullable=False, index=True)
    date = Column(String(10), nullable=False)  # YYYY-MM-DD

    daily_loss = Column(Float, default=0.0)
    daily_trades_opened = Column(Integer, default=0)
    consecutive_losses = Column(Integer, default=0)
    paused_until = Column(DateTime(timezone=True), nullable=True)
    total_open_stake = Column(Float, default=0.0)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index('ix_risk_account_date', 'account_id', 'date', unique=True),
    )

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


# ══════════════════════════════════════════════════════════════════
#  ExecutionEvent 表
# ══════════════════════════════════════════════════════════════════

class ExecutionEventModel(Base):
    """执行事件表 — 替代 execution_events.jsonl"""
    __tablename__ = 'execution_events'

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(64), nullable=False, index=True)
    exchange = Column(String(16), nullable=True)
    symbol = Column(String(32), nullable=True, index=True)
    direction = Column(String(8), nullable=True)
    account_id = Column(String(64), nullable=True, index=True)
    client_order_id = Column(String(128), nullable=True)
    order_id = Column(String(128), nullable=True)

    # 灵活数据字段
    stake = Column(Float, nullable=True)
    leverage = Column(Integer, nullable=True)
    fill_price = Column(Float, nullable=True)
    fill_amount = Column(Float, nullable=True)
    error = Column(Text, nullable=True)
    error_code = Column(String(64), nullable=True)
    extra_data = Column(Text, nullable=True)  # JSON 序列化的额外字段

    timestamp = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)

    __table_args__ = (
        Index('ix_events_type_ts', 'event_type', 'timestamp'),
        Index('ix_events_symbol_ts', 'symbol', 'timestamp'),
    )

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


# ══════════════════════════════════════════════════════════════════
#  TaskMetric 表
# ══════════════════════════════════════════════════════════════════

class TaskMetricModel(Base):
    """任务执行指标表 — 替代 task_metrics.py"""
    __tablename__ = 'task_metrics'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    mode = Column(String(16), nullable=True)   # 'process' | 'thread'
    status = Column(String(16), nullable=False, index=True)  # 'ok' | 'error' | 'timeout' | 'killed'
    duration_sec = Column(Float, nullable=True)
    timeout_sec = Column(Float, nullable=True)
    exitcode = Column(Integer, nullable=True)
    pid = Column(Integer, nullable=True)
    spawn_dt = Column(Float, nullable=True)
    error = Column(Text, nullable=True)

    timestamp = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)

    __table_args__ = (
        Index('ix_task_metrics_name_ts', 'name', 'timestamp'),
    )

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


# ══════════════════════════════════════════════════════════════════
#  InFlightJournal 表
# ══════════════════════════════════════════════════════════════════

class InFlightJournalModel(Base):
    """In-flight Journal 表 — 替代 altcoin_trades_inflight.json"""
    __tablename__ = 'inflight_journal'

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_order_id = Column(String(128), nullable=False, unique=True, index=True)
    exchange = Column(String(16), nullable=False)
    account_id = Column(String(64), nullable=False, default='')
    symbol = Column(String(32), nullable=False)
    direction = Column(String(8), nullable=False)
    stake = Column(Float, nullable=False)
    leverage = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default='pending')  # pending | confirmed | failed
    order_id = Column(String(128), default='')
    last_error = Column(Text, default='')

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index('ix_journal_status', 'status'),
    )

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


# ══════════════════════════════════════════════════════════════════
#  SignalLog 表（新增：信号评分历史，用于策略优化回溯）
# ══════════════════════════════════════════════════════════════════

class SignalLogModel(Base):
    """信号评分日志 — 每次评分都记录，无论是否触发开仓"""
    __tablename__ = 'signal_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    strategy = Column(String(64), nullable=False, default='short_overbought')

    # 评分详情
    score = Column(Integer, nullable=False)
    grade = Column(String(4), nullable=False)  # A / B / SKIP
    rsi_score = Column(Float, default=0.0)
    yao_score = Column(Float, default=0.0)
    trigger_score = Column(Float, default=0.0)
    heat_score = Column(Float, default=0.0)
    cross_validate_bonus = Column(Integer, default=0)
    vol_divergence_bonus = Column(Integer, default=0)

    # 原始指标
    rsi_1d = Column(Float, nullable=True)
    rsi_4h = Column(Float, nullable=True)
    pct_24h = Column(Float, nullable=True)
    oi_change = Column(Float, nullable=True)
    funding_rate = Column(Float, nullable=True)
    btc_24h_pct = Column(Float, nullable=True)
    trigger_type = Column(String(16), nullable=True)  # 'abandon' | '4h_rsi'

    # 结果
    triggered_open = Column(Boolean, default=False)  # 是否最终开仓
    trade_id = Column(String(128), nullable=True)    # 关联的 trade ID

    timestamp = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)

    __table_args__ = (
        Index('ix_signal_logs_symbol_ts', 'symbol', 'timestamp'),
        Index('ix_signal_logs_strategy_ts', 'strategy', 'timestamp'),
    )

    def to_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}
