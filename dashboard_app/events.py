"""
事件抽取 + mtime 缓存（M1 拆分自 dashboard.py）

提供 :func:`extract_events` ：从 ``trades.json`` + ``risk_state.json`` 派生
最近 50 条用户可见事件（开仓 / 平仓 / 风控暂停）。

mtime 缓存
==========
``trades.json`` + ``risk_state.json`` 任一变化才重建结果，否则直接复用上次。
线程安全（``threading.Lock`` 保护）。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import List

from common import (
    RISK_FILE,
    TRADES_FILE,
    filter_trades_by_account,
    get_current_account_id,
    load_json,
)
from dashboard_app.data import load_risk_v1_view


_events_cache_lock = threading.Lock()
_events_cache_trades_mtime: float = 0.0
_events_cache_risk_mtime: float = 0.0
_events_cache_account: str = ''
_events_cache_data: List[dict] = []


def extract_events() -> List[dict]:
    """
    从交易文件 + 风控状态派生最近 50 条事件（按时间倒序）。
    与原 dashboard.py ``_extract_events`` 行为完全一致，包括 7 天窗口、
    mtime 缓存、风控暂停事件等价语义。
    """
    global _events_cache_trades_mtime, _events_cache_risk_mtime
    global _events_cache_account, _events_cache_data

    EVENT_WINDOW_DAYS = 7
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=EVENT_WINDOW_DAYS)
    cutoff_iso = cutoff_dt.isoformat()

    account_id = get_current_account_id()

    try:
        current_trades_mtime = os.path.getmtime(TRADES_FILE)
    except OSError:
        current_trades_mtime = 0.0
    try:
        current_risk_mtime = os.path.getmtime(RISK_FILE)
    except OSError:
        current_risk_mtime = 0.0

    with _events_cache_lock:
        if (
            _events_cache_trades_mtime == current_trades_mtime
            and _events_cache_risk_mtime == current_risk_mtime
            and _events_cache_account == (account_id or '')
            and _events_cache_data
        ):
            return list(_events_cache_data)

    events: List[dict] = []
    trades = load_json(TRADES_FILE, [])
    trades = filter_trades_by_account(trades, account_id)
    risk_state = load_risk_v1_view(account_id)

    for t in trades:
        symbol = t.get('symbol', '?')
        direction = t.get('direction', 'SHORT')

        opened_at = t.get('opened_at') or ''
        if opened_at and opened_at >= cutoff_iso:
            events.append({
                'time': opened_at,
                'type': 'open',
                'level': 'info',
                'message': f"📈 开仓 {direction} {symbol} @ {t.get('entry_price', 0):.6f}",
            })

        closed_at = t.get('closed_at') or ''
        if closed_at and closed_at >= cutoff_iso:
            pnl = t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
            reason = t.get('close_reason', '')
            level = 'success' if pnl > 0 else 'warning'

            ct = str(t.get('close_type') or '').lower()
            if ct in ('hard_stop', 'trail_stop', 'time_stop', 'breakeven_stop'):
                level = 'critical'

            events.append({
                'time': closed_at,
                'type': 'close',
                'level': level,
                'message': f"{'✅' if pnl > 0 else '❌'} 平仓 {direction} {symbol} | {pnl:+.2f}U | {reason}",
            })

    if risk_state.get('paused_until'):
        events.append({
            'time': risk_state.get('last_paused_at', risk_state.get('paused_until', '')),
            'type': 'risk_pause',
            'level': 'critical',
            'message': f"🚨 风控暂停 | 暂停至 {risk_state['paused_until'][:16]}",
        })

    events.sort(key=lambda e: e.get('time', ''), reverse=True)
    result = events[:50]

    with _events_cache_lock:
        _events_cache_trades_mtime = current_trades_mtime
        _events_cache_risk_mtime = current_risk_mtime
        _events_cache_account = account_id or ''
        _events_cache_data = list(result)

    return result


__all__ = ['extract_events']
