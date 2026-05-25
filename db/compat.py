#!/usr/bin/env python3
"""
数据兼容层 — 旧模块渐进迁移到 DB 的桥接接口。

策略：DB 优先，JSON fallback。
  - 写操作：双写（DB + JSON），保证旧模块仍能读 JSON
  - 读操作：优先读 DB，DB 为空时 fallback 到 JSON
  - 当所有模块迁移完成后，删除 JSON 写入路径即可

被 engine_adapter.py / scheduler.py / dashboard.py 等调用。
"""

import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any

logger = logging.getLogger("db.compat")

# DB 是否可用的缓存（避免每次调用都 try import）
_db_available: Optional[bool] = None


def _check_db() -> bool:
    """检测 DB 层是否可用"""
    global _db_available
    if _db_available is not None:
        return _db_available
    try:
        from db.connection import get_engine
        engine = get_engine()
        # 尝试连接
        with engine.connect() as conn:
            conn.execute(engine.dialect.statement_compiler(engine.dialect, None).__class__.__module__ and conn.execute.__func__ and True)
        _db_available = True
    except Exception:
        # 简化检测：只要能 import 就认为可用（init_db 会自动建表）
        try:
            from db.connection import init_db
            init_db()
            _db_available = True
        except Exception as e:
            logger.warning(f"DB 层不可用，将只使用 JSON: {e}")
            _db_available = False
    return _db_available


def _is_db_ready() -> bool:
    """快速检查 DB 是否就绪"""
    global _db_available
    if _db_available is None:
        return _check_db()
    return _db_available


# ══════════════════════════════════════════════════════════════════
#  候选池
# ══════════════════════════════════════════════════════════════════

def load_candidates() -> List[Dict]:
    """
    加载候选池。
    优先从 DB 读，DB 为空时 fallback 到 JSON。
    """
    if _is_db_ready():
        try:
            from db.repositories import CandidateRepo
            candidates = CandidateRepo.get_active()
            if candidates:
                return candidates
        except Exception as e:
            logger.debug(f"DB 读取候选失败，fallback JSON: {e}")

    # Fallback: JSON
    from common import CANDIDATES_FILE, load_json
    raw = load_json(CANDIDATES_FILE, [])
    # 过滤已触发的
    return [c for c in raw if not c.get('triggered', False)]


def save_candidates(candidates: List[Dict]):
    """双写候选池"""
    # DB
    if _is_db_ready():
        try:
            from db.repositories import CandidateRepo
            for c in candidates:
                CandidateRepo.upsert(c)
        except Exception as e:
            logger.warning(f"候选写入 DB 失败: {e}")

    # JSON（兼容）
    from common import CANDIDATES_FILE, LockedJsonFile
    try:
        with LockedJsonFile(CANDIDATES_FILE, default=[]) as (_, save):
            save(candidates)
    except Exception as e:
        logger.warning(f"候选写入 JSON 失败: {e}")


# ══════════════════════════════════════════════════════════════════
#  交易记录
# ══════════════════════════════════════════════════════════════════

def load_open_trades(account_id: Optional[str] = None) -> List[Dict]:
    """
    加载未平仓交易。
    优先 DB，fallback JSON。
    """
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            trades = TradeRepo.get_open(account_id=account_id)
            if trades:
                return trades
        except Exception as e:
            logger.debug(f"DB 读取 open trades 失败，fallback JSON: {e}")

    # Fallback: JSON
    from common import TRADES_FILE, load_json, filter_trades_by_account
    raw = load_json(TRADES_FILE, [])
    open_trades = [t for t in raw if t.get('status') == 'open']
    if account_id:
        open_trades = filter_trades_by_account(open_trades, account_id)
    return open_trades


def load_all_trades(status: Optional[str] = None,
                    account_id: Optional[str] = None) -> List[Dict]:
    """加载交易（可选状态过滤）"""
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            return TradeRepo.get_all(status=status, account_id=account_id)
        except Exception as e:
            logger.debug(f"DB 读取 trades 失败，fallback JSON: {e}")

    from common import TRADES_FILE, load_json, filter_trades_by_account
    raw = load_json(TRADES_FILE, [])
    if status:
        raw = [t for t in raw if t.get('status') == status]
    if account_id:
        raw = filter_trades_by_account(raw, account_id)
    return raw


def save_trade(trade_dict: Dict):
    """双写单笔交易（新建或更新）"""
    trade_id = trade_dict.get('id', '')

    # DB
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            existing = TradeRepo.get_by_id(trade_id)
            if existing:
                TradeRepo.update(trade_id, trade_dict)
            else:
                TradeRepo.create(trade_dict)
        except Exception as e:
            logger.warning(f"交易写入 DB 失败 ({trade_id}): {e}")

    # JSON（兼容）— 由调用方通过 LockedJsonFile 自行管理
    # 这里不重复写 JSON，因为旧代码已经在写了


def close_trade_compat(trade_id: str, pnl: float, close_price: float,
                       close_reason: str, close_type: str, **kwargs):
    """平仓 — 双写 DB"""
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            TradeRepo.close_trade(
                trade_id=trade_id,
                pnl=pnl,
                close_price=close_price,
                close_reason=close_reason,
                close_type=close_type,
                **kwargs,
            )
        except Exception as e:
            logger.warning(f"平仓写入 DB 失败 ({trade_id}): {e}")


# ══════════════════════════════════════════════════════════════════
#  风控状态
# ══════════════════════════════════════════════════════════════════

def get_open_stake(account_id: Optional[str] = None) -> float:
    """获取当前持仓总保证金"""
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            return TradeRepo.get_total_open_stake(account_id=account_id)
        except Exception:
            pass

    # Fallback
    trades = load_open_trades(account_id)
    return sum(t.get('stake_remaining', t.get('stake', 0)) for t in trades)


def get_today_trades_count(account_id: Optional[str] = None) -> int:
    """今日开仓数"""
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            return TradeRepo.get_today_trades_count(account_id=account_id)
        except Exception:
            pass
    return 0


def get_realized_pnl(account_id: Optional[str] = None) -> float:
    """累计已实现盈亏"""
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            return TradeRepo.get_realized_pnl(account_id=account_id)
        except Exception:
            pass

    # Fallback
    from common import TRADES_FILE, load_json, filter_trades_by_account
    trades = load_json(TRADES_FILE, [])
    if account_id:
        trades = filter_trades_by_account(trades, account_id)
    return sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        for t in trades if t.get('status') == 'closed'
    )


# ══════════════════════════════════════════════════════════════════
#  执行事件
# ══════════════════════════════════════════════════════════════════

def log_event(event_type: str, **kwargs):
    """记录执行事件（双写 DB + JSONL）"""
    # DB
    if _is_db_ready():
        try:
            from db.repositories import EventRepo
            EventRepo.log(event_type, **kwargs)
        except Exception as e:
            logger.debug(f"事件写入 DB 失败: {e}")

    # JSONL（兼容）
    from common import log_execution_event
    log_execution_event(event_type, **kwargs)


# ══════════════════════════════════════════════════════════════════
#  信号日志
# ══════════════════════════════════════════════════════════════════

def log_signal(signal_data: Dict):
    """记录信号评分到 DB"""
    if _is_db_ready():
        try:
            from db.repositories import SignalLogRepo
            SignalLogRepo.log(signal_data)
        except Exception as e:
            logger.debug(f"信号日志写入 DB 失败: {e}")
