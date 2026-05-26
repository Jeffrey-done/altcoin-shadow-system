"""
Repository 层 — 封装所有数据库 CRUD 操作
提供统一的数据访问接口，策略/风控/执行模块通过 Repository 读写数据。

设计原则：
  - 每个方法对应一个明确的业务操作
  - 自动管理事务（单次调用 = 单个事务）
  - 向后兼容：返回 dict 格式与原 JSON 模式对齐
  - 支持批量操作（减少事务开销）
"""

import json
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from sqlalchemy import and_, or_, desc, func
from sqlalchemy.orm import Session

from db.connection import get_session
from db.models import (
    TradeModel, CandidateModel, RiskStateModel,
    ExecutionEventModel, TaskMetricModel, InFlightJournalModel,
    SignalLogModel,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today_str() -> str:
    return _utcnow().strftime('%Y-%m-%d')


# ══════════════════════════════════════════════════════════════════
#  TradeRepo — 交易记录
# ══════════════════════════════════════════════════════════════════

class TradeRepo:
    """交易记录 Repository"""

    @staticmethod
    def create(trade_data: dict) -> dict:
        """创建新交易"""
        with get_session() as session:
            # 处理时间字段
            if 'opened_at' in trade_data and isinstance(trade_data['opened_at'], str):
                from common import parse_iso
                trade_data['opened_at'] = parse_iso(trade_data['opened_at'])

            trade = TradeModel(**trade_data)
            session.add(trade)
            session.flush()
            return trade.to_dict()

    @staticmethod
    def get_by_id(trade_id: str) -> Optional[dict]:
        """按 ID 获取交易"""
        with get_session() as session:
            trade = session.query(TradeModel).filter(TradeModel.id == trade_id).first()
            return trade.to_dict() if trade else None

    @staticmethod
    def get_open(account_id: Optional[str] = None,
                 exchange: Optional[str] = None) -> List[dict]:
        """获取所有未平仓交易"""
        with get_session() as session:
            query = session.query(TradeModel).filter(TradeModel.status == 'open')
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)
            if exchange:
                query = query.filter(TradeModel.exchange == exchange)
            return [t.to_dict() for t in query.all()]

    @staticmethod
    def get_open_symbols(account_id: Optional[str] = None) -> set:
        """获取当前所有持仓的 symbol 集合"""
        with get_session() as session:
            query = session.query(TradeModel.symbol).filter(TradeModel.status == 'open')
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)
            return {row[0] for row in query.all()}

    @staticmethod
    def get_closed(account_id: Optional[str] = None,
                   since: Optional[datetime] = None,
                   limit: int = 100) -> List[dict]:
        """获取已平仓交易"""
        with get_session() as session:
            query = session.query(TradeModel).filter(TradeModel.status == 'closed')
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)
            if since:
                query = query.filter(TradeModel.closed_at >= since)
            query = query.order_by(desc(TradeModel.closed_at)).limit(limit)
            return [t.to_dict() for t in query.all()]

    @staticmethod
    def get_by_symbol(symbol: str, status: Optional[str] = None,
                      account_id: Optional[str] = None) -> List[dict]:
        """按币种查询交易"""
        with get_session() as session:
            query = session.query(TradeModel).filter(TradeModel.symbol == symbol)
            if status:
                query = query.filter(TradeModel.status == status)
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)
            return [t.to_dict() for t in query.order_by(desc(TradeModel.opened_at)).all()]

    @staticmethod
    def update(trade_id: str, updates: dict) -> bool:
        """更新交易字段"""
        with get_session() as session:
            trade = session.query(TradeModel).filter(TradeModel.id == trade_id).first()
            if not trade:
                return False
            for key, value in updates.items():
                if hasattr(trade, key):
                    setattr(trade, key, value)
            trade.updated_at = _utcnow()
            return True

    @staticmethod
    def close_trade(trade_id: str, pnl: float, close_price: float,
                    close_reason: str, close_type: str,
                    close_order_id: Optional[str] = None,
                    exit_ref_price: float = 0.0,
                    exit_slippage_pct: float = 0.0) -> bool:
        """平仓"""
        with get_session() as session:
            trade = session.query(TradeModel).filter(TradeModel.id == trade_id).first()
            if not trade:
                return False
            trade.status = 'closed'
            trade.pnl = pnl
            trade.current_price = close_price
            trade.close_reason = close_reason
            trade.close_type = close_type
            trade.close_order_id = close_order_id
            trade.exit_ref_price = exit_ref_price
            trade.exit_slippage_pct = exit_slippage_pct
            trade.closed_at = _utcnow()
            trade.updated_at = _utcnow()
            return True

    @staticmethod
    def get_total_open_stake(account_id: Optional[str] = None) -> float:
        """计算指定账户的持仓总保证金"""
        with get_session() as session:
            query = session.query(func.coalesce(func.sum(TradeModel.stake_remaining), 0.0))
            query = query.filter(TradeModel.status == 'open')
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)
            return float(query.scalar() or 0.0)

    @staticmethod
    def get_today_trades_count(account_id: Optional[str] = None) -> int:
        """今日开仓数"""
        today_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with get_session() as session:
            query = session.query(func.count(TradeModel.id))
            query = query.filter(TradeModel.opened_at >= today_start)
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)
            return int(query.scalar() or 0)

    @staticmethod
    def get_today_realized_loss(account_id: Optional[str] = None) -> float:
        """今日已实现亏损（绝对值）"""
        today_start = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        with get_session() as session:
            query = session.query(TradeModel).filter(
                and_(
                    TradeModel.status == 'closed',
                    TradeModel.closed_at >= today_start,
                )
            )
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)

            total_loss = 0.0
            for t in query.all():
                realized = (t.tp1_locked_pnl or 0) + (t.pnl or 0)
                if realized < 0:
                    total_loss += abs(realized)
            return round(total_loss, 2)

    @staticmethod
    def get_consecutive_losses(account_id: Optional[str] = None) -> int:
        """从最近已平仓交易反推当前连续亏损次数"""
        with get_session() as session:
            query = session.query(TradeModel).filter(TradeModel.status == 'closed')
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)
            closed = query.order_by(desc(TradeModel.closed_at)).limit(50).all()

            cnt = 0
            for t in closed:
                realized = (t.tp1_locked_pnl or 0) + (t.pnl or 0)
                if realized < 0:
                    cnt += 1
                elif realized > 0:
                    break
            return cnt

    @staticmethod
    def get_realized_pnl(account_id: Optional[str] = None) -> float:
        """累计已实现盈亏"""
        with get_session() as session:
            query = session.query(TradeModel).filter(TradeModel.status == 'closed')
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)

            total = 0.0
            for t in query.all():
                total += (t.tp1_locked_pnl or 0) + (t.pnl or 0)
            return round(total, 2)

    @staticmethod
    def get_all(status: Optional[str] = None, account_id: Optional[str] = None) -> List[dict]:
        """获取所有交易（可选过滤）"""
        with get_session() as session:
            query = session.query(TradeModel)
            if status:
                query = query.filter(TradeModel.status == status)
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)
            return [t.to_dict() for t in query.order_by(desc(TradeModel.opened_at)).all()]

    @staticmethod
    def get_dynamic_pnl(account_id: Optional[str] = None) -> float:
        """Cumulative PnL including TP1 locked from open trades"""
        with get_session() as session:
            query = session.query(TradeModel)
            if account_id:
                query = query.filter(TradeModel.account_id == account_id)
            total = 0.0
            for t in query.all():
                if t.status == 'closed':
                    total += (t.tp1_locked_pnl or 0) + (t.pnl or 0)
                elif t.status == 'open' and (t.tp1_locked_pnl or 0) > 0:
                    total += (t.tp1_locked_pnl or 0)
            return total

    @staticmethod
    def archive_old_trades(days: int = 30) -> int:
        """归档超过 N 天的已平仓交易（标记 archived，不删除）"""
        cutoff = _utcnow() - timedelta(days=days)
        with get_session() as session:
            count = session.query(TradeModel).filter(
                and_(
                    TradeModel.status == 'closed',
                    TradeModel.closed_at < cutoff,
                )
            ).count()
            # 数据库模式下不需要物理移动，查询时自动按时间过滤
            return count


# ══════════════════════════════════════════════════════════════════
#  CandidateRepo — 候选池
# ══════════════════════════════════════════════════════════════════

class CandidateRepo:
    """候选池 Repository — 多策略支持"""

    @staticmethod
    def upsert(candidate_data: dict) -> dict:
        """创建或更新候选（按 symbol + strategy 复合唯一键）"""
        with get_session() as session:
            strategy = candidate_data.get('strategy', 'short_overbought')
            symbol = candidate_data['symbol']

            existing = session.query(CandidateModel).filter(
                and_(
                    CandidateModel.symbol == symbol,
                    CandidateModel.strategy == strategy,
                )
            ).first()

            # 序列化 metadata dict → metadata_json
            if 'metadata' in candidate_data and isinstance(candidate_data['metadata'], dict):
                import json as _json
                candidate_data['metadata_json'] = _json.dumps(
                    candidate_data['metadata'], ensure_ascii=False)
                del candidate_data['metadata']

            if existing:
                for key, value in candidate_data.items():
                    if hasattr(existing, key) and key != 'id':
                        setattr(existing, key, value)
                existing.updated_at = _utcnow()
                session.flush()
                return existing.to_dict()
            else:
                # Ensure strategy and direction are set
                candidate_data.setdefault('strategy', 'short_overbought')
                candidate_data.setdefault('direction', 'SHORT')
                candidate = CandidateModel(**{
                    k: v for k, v in candidate_data.items()
                    if hasattr(CandidateModel, k)
                })
                session.add(candidate)
                session.flush()
                return candidate.to_dict()

    @staticmethod
    def get_active(exclude_triggered: bool = True,
                   strategy: Optional[str] = None,
                   direction: Optional[str] = None) -> List[dict]:
        """获取活跃候选（未触发 + 未过期），支持按策略/方向过滤"""
        with get_session() as session:
            query = session.query(CandidateModel)
            if exclude_triggered:
                query = query.filter(CandidateModel.triggered == False)
            if strategy:
                query = query.filter(CandidateModel.strategy == strategy)
            if direction:
                query = query.filter(CandidateModel.direction == direction)
            # 排除过期候选
            now = _utcnow()
            query = query.filter(
                or_(
                    CandidateModel.expires_at == None,
                    CandidateModel.expires_at > now,
                )
            )
            return [c.to_dict() for c in query.order_by(CandidateModel.added_at).all()]

    @staticmethod
    def get_by_strategy(strategy_name: str, exclude_triggered: bool = True) -> List[dict]:
        """获取指定策略的候选列表"""
        return CandidateRepo.get_active(
            exclude_triggered=exclude_triggered, strategy=strategy_name)

    @staticmethod
    def mark_triggered(symbol: str, trigger_type: str, trigger_reason: str,
                       strategy: str = 'short_overbought') -> bool:
        """标记候选已触发（按 symbol + strategy 定位）"""
        with get_session() as session:
            candidate = session.query(CandidateModel).filter(
                and_(
                    CandidateModel.symbol == symbol,
                    CandidateModel.strategy == strategy,
                )
            ).first()
            if not candidate:
                return False
            candidate.triggered = True
            candidate.trigger_type = trigger_type
            candidate.trigger_reason = trigger_reason
            candidate.updated_at = _utcnow()
            return True

    @staticmethod
    def remove_expired(expire_hours: int = 12) -> int:
        """清理过期候选"""
        cutoff = _utcnow() - timedelta(hours=expire_hours)
        with get_session() as session:
            count = session.query(CandidateModel).filter(
                or_(
                    CandidateModel.triggered == True,
                    CandidateModel.added_at < cutoff,
                )
            ).delete(synchronize_session=False)
            return count

    @staticmethod
    def remove_by_symbol(symbol: str, strategy: Optional[str] = None) -> bool:
        """删除指定 symbol 的候选（可选限定策略）"""
        with get_session() as session:
            query = session.query(CandidateModel).filter(
                CandidateModel.symbol == symbol
            )
            if strategy:
                query = query.filter(CandidateModel.strategy == strategy)
            count = query.delete(synchronize_session=False)
            return count > 0

    @staticmethod
    def count(strategy: Optional[str] = None) -> int:
        """当前候选池大小（可选按策略过滤）"""
        with get_session() as session:
            query = session.query(func.count(CandidateModel.id))
            if strategy:
                query = query.filter(CandidateModel.strategy == strategy)
            return query.scalar() or 0


# ══════════════════════════════════════════════════════════════════
#  RiskRepo — 风控状态
# ══════════════════════════════════════════════════════════════════

class RiskRepo:
    """风控状态 Repository"""

    @staticmethod
    def get_or_create(account_id: str) -> dict:
        """获取今日风控状态，不存在则创建"""
        today = _today_str()
        with get_session() as session:
            state = session.query(RiskStateModel).filter(
                and_(
                    RiskStateModel.account_id == account_id,
                    RiskStateModel.date == today,
                )
            ).first()

            if state:
                return state.to_dict()

            # 创建新的今日状态
            new_state = RiskStateModel(
                account_id=account_id,
                date=today,
            )
            session.add(new_state)
            session.flush()
            return new_state.to_dict()

    @staticmethod
    def update(account_id: str, updates: dict) -> bool:
        """更新风控状态"""
        today = _today_str()
        with get_session() as session:
            state = session.query(RiskStateModel).filter(
                and_(
                    RiskStateModel.account_id == account_id,
                    RiskStateModel.date == today,
                )
            ).first()

            if not state:
                state = RiskStateModel(account_id=account_id, date=today)
                session.add(state)

            for key, value in updates.items():
                if hasattr(state, key):
                    setattr(state, key, value)
            state.updated_at = _utcnow()
            return True

    @staticmethod
    def increment_daily_trades(account_id: str) -> int:
        """递增今日开仓次数，返回新值"""
        today = _today_str()
        with get_session() as session:
            state = session.query(RiskStateModel).filter(
                and_(
                    RiskStateModel.account_id == account_id,
                    RiskStateModel.date == today,
                )
            ).first()

            if not state:
                state = RiskStateModel(account_id=account_id, date=today)
                session.add(state)
                session.flush()

            state.daily_trades_opened += 1
            state.updated_at = _utcnow()
            return state.daily_trades_opened

    @staticmethod
    def add_daily_loss(account_id: str, loss: float) -> float:
        """累加今日亏损，返回累计值"""
        today = _today_str()
        with get_session() as session:
            state = session.query(RiskStateModel).filter(
                and_(
                    RiskStateModel.account_id == account_id,
                    RiskStateModel.date == today,
                )
            ).first()

            if not state:
                state = RiskStateModel(account_id=account_id, date=today)
                session.add(state)
                session.flush()

            state.daily_loss += abs(loss)
            state.updated_at = _utcnow()
            return state.daily_loss


# ══════════════════════════════════════════════════════════════════
#  EventRepo — 执行事件
# ══════════════════════════════════════════════════════════════════

class EventRepo:
    """执行事件 Repository"""

    @staticmethod
    def log(event_type: str, **kwargs) -> int:
        """记录一条执行事件"""
        with get_session() as session:
            # 分离已知字段和额外字段
            known_fields = {
                'exchange', 'symbol', 'direction', 'account_id',
                'client_order_id', 'order_id', 'stake', 'leverage',
                'fill_price', 'fill_amount', 'error', 'error_code',
            }
            model_data = {'event_type': event_type}
            extra = {}

            for key, value in kwargs.items():
                if key in known_fields:
                    model_data[key] = value
                else:
                    extra[key] = value

            if extra:
                model_data['extra_data'] = json.dumps(extra, ensure_ascii=False)

            event = ExecutionEventModel(**model_data)
            session.add(event)
            session.flush()
            return event.id

    @staticmethod
    def get_recent(event_type: Optional[str] = None,
                   symbol: Optional[str] = None,
                   limit: int = 50) -> List[dict]:
        """查询最近的执行事件"""
        with get_session() as session:
            query = session.query(ExecutionEventModel)
            if event_type:
                query = query.filter(ExecutionEventModel.event_type == event_type)
            if symbol:
                query = query.filter(ExecutionEventModel.symbol == symbol)
            query = query.order_by(desc(ExecutionEventModel.timestamp)).limit(limit)
            return [e.to_dict() for e in query.all()]


# ══════════════════════════════════════════════════════════════════
#  TaskMetricRepo — 任务指标
# ══════════════════════════════════════════════════════════════════

class TaskMetricRepo:
    """任务指标 Repository"""

    @staticmethod
    def record(metric_data: dict) -> int:
        """记录任务执行指标"""
        with get_session() as session:
            metric = TaskMetricModel(**metric_data)
            session.add(metric)
            session.flush()
            return metric.id

    @staticmethod
    def get_recent(name: Optional[str] = None, limit: int = 20) -> List[dict]:
        """查询最近的任务指标"""
        with get_session() as session:
            query = session.query(TaskMetricModel)
            if name:
                query = query.filter(TaskMetricModel.name == name)
            query = query.order_by(desc(TaskMetricModel.timestamp)).limit(limit)
            return [m.to_dict() for m in query.all()]

    @staticmethod
    def get_failure_streak(name: str) -> int:
        """获取指定任务的连续失败次数"""
        with get_session() as session:
            recent = session.query(TaskMetricModel).filter(
                TaskMetricModel.name == name
            ).order_by(desc(TaskMetricModel.timestamp)).limit(20).all()

            streak = 0
            for m in recent:
                if m.status in ('error', 'timeout', 'killed'):
                    streak += 1
                else:
                    break
            return streak


# ══════════════════════════════════════════════════════════════════
#  JournalRepo — In-flight Journal
# ══════════════════════════════════════════════════════════════════

class JournalRepo:
    """In-flight Journal Repository"""

    @staticmethod
    def add_pending(client_order_id: str, exchange: str, account_id: str,
                    symbol: str, direction: str, stake: float, leverage: int) -> None:
        """下单前写 pending"""
        with get_session() as session:
            existing = session.query(InFlightJournalModel).filter(
                InFlightJournalModel.client_order_id == client_order_id
            ).first()

            if existing:
                existing.status = 'pending'
                existing.updated_at = _utcnow()
            else:
                entry = InFlightJournalModel(
                    client_order_id=client_order_id,
                    exchange=exchange,
                    account_id=account_id or '',
                    symbol=symbol,
                    direction=direction,
                    stake=stake,
                    leverage=leverage,
                    status='pending',
                )
                session.add(entry)

    @staticmethod
    def mark_confirmed(client_order_id: str) -> None:
        """确认成功后删除"""
        with get_session() as session:
            session.query(InFlightJournalModel).filter(
                InFlightJournalModel.client_order_id == client_order_id
            ).delete(synchronize_session=False)

    @staticmethod
    def mark_failed(client_order_id: str, error: str) -> None:
        """标记失败"""
        with get_session() as session:
            entry = session.query(InFlightJournalModel).filter(
                InFlightJournalModel.client_order_id == client_order_id
            ).first()
            if entry:
                entry.status = 'failed'
                entry.last_error = str(error)[:500]
                entry.updated_at = _utcnow()

    @staticmethod
    def get_pending() -> List[dict]:
        """获取所有 pending 条目"""
        with get_session() as session:
            entries = session.query(InFlightJournalModel).filter(
                InFlightJournalModel.status == 'pending'
            ).all()
            return [e.to_dict() for e in entries]

    @staticmethod
    def cleanup_failed(retain_hours: int = 72) -> int:
        """清理过期 failed 条目"""
        cutoff = _utcnow() - timedelta(hours=retain_hours)
        with get_session() as session:
            count = session.query(InFlightJournalModel).filter(
                and_(
                    InFlightJournalModel.status == 'failed',
                    InFlightJournalModel.updated_at < cutoff,
                )
            ).delete(synchronize_session=False)
            return count


# ══════════════════════════════════════════════════════════════════
#  SignalLogRepo — 信号评分日志
# ══════════════════════════════════════════════════════════════════

class SignalLogRepo:
    """信号评分日志 Repository"""

    @staticmethod
    def log(signal_data: dict) -> int:
        """记录一条信号评分"""
        with get_session() as session:
            log_entry = SignalLogModel(**signal_data)
            session.add(log_entry)
            session.flush()
            return log_entry.id

    @staticmethod
    def get_recent(symbol: Optional[str] = None,
                   strategy: Optional[str] = None,
                   limit: int = 50) -> List[dict]:
        """查询最近的信号"""
        with get_session() as session:
            query = session.query(SignalLogModel)
            if symbol:
                query = query.filter(SignalLogModel.symbol == symbol)
            if strategy:
                query = query.filter(SignalLogModel.strategy == strategy)
            query = query.order_by(desc(SignalLogModel.timestamp)).limit(limit)
            return [s.to_dict() for s in query.all()]

    @staticmethod
    def get_trigger_rate(strategy: str, days: int = 30) -> dict:
        """统计信号触发率"""
        since = _utcnow() - timedelta(days=days)
        with get_session() as session:
            total = session.query(func.count(SignalLogModel.id)).filter(
                and_(
                    SignalLogModel.strategy == strategy,
                    SignalLogModel.timestamp >= since,
                )
            ).scalar() or 0

            triggered = session.query(func.count(SignalLogModel.id)).filter(
                and_(
                    SignalLogModel.strategy == strategy,
                    SignalLogModel.timestamp >= since,
                    SignalLogModel.triggered_open == True,
                )
            ).scalar() or 0

            return {
                'total_signals': total,
                'triggered': triggered,
                'trigger_rate': round(triggered / max(total, 1) * 100, 1),
            }
