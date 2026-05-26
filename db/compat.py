#!/usr/bin/env python3
"""
数据兼容层 — 旧模块渐进迁移到 DB 的桥接接口。

S4 修复（2026-05）— 双写模式可配置
======================================

历史背景：原本所有写操作"双写"（DB + JSON），保证旧模块仍能读 JSON。
代价是每次交易要做 2 次 IO，且 DB 与 JSON 之间偶尔不一致（崩溃在两次写
之间）。读路径用"DB 优先 → JSON fallback"也意味着两份数据并存，难以判定
真源。

新增配置开关 ``DB_WRITE_MODE``（环境变量 / config / runtime_config）：

  * ``dual``        — 双写（**默认**，向后兼容；旧 dashboard 仍能读 JSON）
  * ``db-canonical`` — DB 写，JSON 仅作只读快照（每分钟由 dashboard refresh）
  * ``json-only``   — DB 关闭（无 SQLAlchemy 时的兜底）

所有读路径通过统一函数（load_open_trades / load_candidates 等）走，调用方
不需关心模式。

被 engine_adapter.py / scheduler.py / dashboard.py 等调用。
"""

import logging
import os
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any

logger = logging.getLogger("db.compat")

# DB 是否可用的缓存（避免每次调用都 try import）
_db_available: Optional[bool] = None


def get_write_mode() -> str:
    """
    返回当前写模式：'dual' / 'db-canonical' / 'json-only'。
    优先级：env var > config 模块属性 > 默认 'dual'。
    """
    env = os.environ.get('DB_WRITE_MODE', '').strip().lower()
    if env in ('dual', 'db-canonical', 'json-only'):
        return env
    try:
        import config as _cfg
        attr = str(getattr(_cfg, 'DB_WRITE_MODE', 'dual')).lower()
        if attr in ('dual', 'db-canonical', 'json-only'):
            return attr
    except Exception:
        pass
    return 'dual'


def _should_write_db() -> bool:
    return get_write_mode() in ('dual', 'db-canonical') and _is_db_ready()


def _should_write_json() -> bool:
    return get_write_mode() in ('dual', 'json-only')


def _check_db() -> bool:
    """检测 DB 层是否可用（VF-3 简化：直接尝试 init_db）"""
    global _db_available
    if _db_available is not None:
        return _db_available
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
    """
    保存候选池。
    写模式由 ``get_write_mode()`` 决定（dual / db-canonical / json-only）。
    """
    if _should_write_db():
        try:
            from db.repositories import CandidateRepo
            for c in candidates:
                CandidateRepo.upsert(c)
        except Exception as e:
            logger.warning(f"候选写入 DB 失败: {e}")

    if _should_write_json():
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
    """
    保存单笔交易（新建或更新）。
    写模式由 ``get_write_mode()`` 决定。
    JSON 路径由调用方通过 LockedJsonFile 管理（保留旧约定，避免双层加锁）。
    """
    trade_id = trade_dict.get('id', '')

    if _should_write_db():
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
    """平仓 — 写 DB（json-only 模式跳过）"""
    if _should_write_db():
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
    """
    记录执行事件。
    - dual / db-canonical: 写 DB
    - dual / json-only: 写 JSONL
    """
    if _should_write_db():
        try:
            from db.repositories import EventRepo
            EventRepo.log(event_type, **kwargs)
        except Exception as e:
            logger.debug(f"事件写入 DB 失败: {e}")

    if _should_write_json():
        from common import log_execution_event
        log_execution_event(event_type, **kwargs)


# ══════════════════════════════════════════════════════════════════
#  信号日志
# ══════════════════════════════════════════════════════════════════

def log_signal(signal_data: Dict):
    """记录信号评分到 DB（json-only 模式跳过）"""
    if _should_write_db():
        try:
            from db.repositories import SignalLogRepo
            SignalLogRepo.log(signal_data)
        except Exception as e:
            logger.debug(f"信号日志写入 DB 失败: {e}")



# ══════════════════════════════════════════════════════════════════
#  新增方法 — 支持 common.py / risk_control.py 迁移
# ══════════════════════════════════════════════════════════════════

def load_all_trades_for_account(account_id: Optional[str] = None) -> List[Dict]:
    """Load all trades (open + closed) filtered by account. Used by common.py calculations."""
    return load_all_trades(account_id=account_id)


def get_dynamic_pnl(account_id: Optional[str] = None) -> float:
    """Cumulative realized PnL + TP1 locked PnL from open trades (for dynamic balance)"""
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            return TradeRepo.get_dynamic_pnl(account_id=account_id)
        except Exception:
            pass
    # Fallback: compute from JSON
    from common import TRADES_FILE, load_json, filter_trades_by_account
    trades = load_json(TRADES_FILE, [])
    if account_id:
        trades = filter_trades_by_account(trades, account_id)
    total = 0.0
    for t in trades:
        if t.get('status') == 'closed':
            total += t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        elif t.get('status') == 'open' and t.get('tp1_locked_pnl', 0) > 0:
            total += t.get('tp1_locked_pnl', 0)
    return total


def get_open_symbols(account_id: Optional[str] = None) -> set:
    """Get set of symbols with open positions (for dedup pre-filtering)"""
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            return TradeRepo.get_open_symbols(account_id=account_id)
        except Exception:
            pass
    from common import TRADES_FILE, load_json
    trades = load_json(TRADES_FILE, [])
    return {t['symbol'] for t in trades if t.get('status') == 'open' or t.get('close_retry_pending')}


def get_consecutive_losses(account_id: Optional[str] = None) -> int:
    """Get consecutive loss count from most recent closed trades"""
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            return TradeRepo.get_consecutive_losses(account_id=account_id)
        except Exception:
            pass
    return 0


def get_today_realized_loss(account_id: Optional[str] = None) -> float:
    """Today's realized loss (absolute value)"""
    if _is_db_ready():
        try:
            from db.repositories import TradeRepo
            return TradeRepo.get_today_realized_loss(account_id=account_id)
        except Exception:
            pass
    return 0.0
