"""
Dashboard 数据读取层（M1 拆分自 dashboard.py）

提供：
  * ``get_long_candidates()`` — 做多策略候选
  * ``load_risk_v1_view(account_id)`` — 把 v2 多账户风控 → v1 平铺视图
  * ``get_dashboard_data(account_id=None)`` — 主面板汇总
  * ``read_tail_lines(path, max_lines)`` — 安全读尾部行
  * ``build_execution_metrics(...)`` — 执行/对账指标
  * ``get_risk_history(pnl_history)`` — 7 日风控 sparkline

所有函数都是**纯读**，无副作用；可单独单测。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import config
from common import (
    CANDIDATES_FILE,
    EXECUTION_EVENTS_FILE,
    RISK_FILE,
    TRADES_FILE,
    account_param,
    filter_trades_by_account,
    get_compound_stake,
    get_current_account_id,
    get_dynamic_balance,
    load_json,
    today_str,
    utcnow_iso,
)

logger = logging.getLogger("dashboard_app.data")


# ══════════════════════════════════════════════════════════════════
#  做多候选
# ══════════════════════════════════════════════════════════════════

def get_long_candidates() -> list:
    """
    获取做多策略（Pre-Pump Sniffer + Long Oversold）的候选列表。
    优先 DB；DB 不可用时从 candidates JSON 过滤；都失败返回 []。
    """
    try:
        from db.compat import _is_db_ready
        if _is_db_ready():
            from db.repositories import CandidateRepo
            return CandidateRepo.get_active(
                exclude_triggered=True, direction='LONG')
    except Exception:
        pass

    # Fallback: 从全量 candidates JSON 按 direction/strategy 过滤
    try:
        candidates = load_json(CANDIDATES_FILE, [])
        return [c for c in candidates
                if not c.get('triggered', False)
                and c.get('direction', '').upper() == 'LONG']
    except Exception:
        pass

    return []


# ══════════════════════════════════════════════════════════════════
#  风控视图
# ══════════════════════════════════════════════════════════════════

def load_risk_v1_view(account_id: Optional[str]) -> dict:
    """
    把 risk_state.json (v2 多账户结构) 转成单账户 v1 平铺视图。
    详细背景见原 dashboard.py 中本函数注释。
    """
    try:
        from risk_control import load_risk_state
        state = load_risk_state(account_id)
        return state.to_dict()
    except Exception as e:
        logger.warning(f"读取风控状态失败: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════
#  主仪表盘数据
# ══════════════════════════════════════════════════════════════════

def get_dashboard_data(account_id: Optional[str] = None) -> dict:
    """
    汇总所有数据供前端展示。``account_id=None`` 时使用当前活跃账户。
    保留与原 dashboard.py 完全一致的输出 schema。
    """
    if account_id is None:
        account_id = get_current_account_id()

    try:
        from db.compat import load_all_trades
        trades = load_all_trades(account_id=account_id)
    except Exception:
        trades = load_json(TRADES_FILE, [])
        trades = filter_trades_by_account(trades, account_id)

    candidates = load_json(CANDIDATES_FILE, [])
    risk_state = load_risk_v1_view(account_id)

    short_trades = [t for t in trades if t.get('direction', 'SHORT') == 'SHORT']
    long_trades = [t for t in trades if t.get('direction') == 'LONG']

    open_short = [t for t in short_trades if t.get('status') == 'open']
    closed_short = [t for t in short_trades if t.get('status') == 'closed']
    open_long = [t for t in long_trades if t.get('status') == 'open']
    closed_long = [t for t in long_trades if t.get('status') == 'closed']

    today = today_str()
    today_closed_short = [t for t in closed_short if t.get('closed_at', '').startswith(today)]
    today_closed_long = [t for t in closed_long if t.get('closed_at', '').startswith(today)]

    today_pnl_short = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in today_closed_short
    )
    today_pnl_long = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in today_closed_long
    )

    today_tp1_locked_short = sum(
        t.get('tp1_locked_pnl', 0) for t in open_short
        if t.get('tp1_triggered') and t.get('opened_at', '').startswith(today)
    )
    today_tp1_locked_long = sum(
        t.get('tp1_locked_pnl', 0) for t in open_long
        if t.get('tp1_triggered') and t.get('opened_at', '').startswith(today)
    )
    today_pnl_short += today_tp1_locked_short
    today_pnl_long += today_tp1_locked_long

    total_pnl_short = sum(t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in closed_short)
    total_pnl_long = sum(t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in closed_long)
    total_pnl_short += sum(t.get('tp1_locked_pnl', 0) for t in open_short if t.get('tp1_triggered'))
    total_pnl_long += sum(t.get('tp1_locked_pnl', 0) for t in open_long if t.get('tp1_triggered'))

    all_closed = closed_short + closed_long
    tp1_triggered_trades = [t for t in open_short + open_long if t.get('tp1_triggered')]
    all_for_winrate = all_closed + tp1_triggered_trades
    wins = sum(
        1 for t in all_for_winrate
        if (t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)) > 0
    )
    win_rate = (wins / len(all_for_winrate) * 100) if all_for_winrate else 0

    pnl_history: dict = {}
    for t in closed_short + closed_long:
        closed_at = t.get('closed_at', '')
        if not closed_at:
            continue
        day = closed_at[:10]
        pnl = t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        pnl_history[day] = pnl_history.get(day, 0) + pnl

    sorted_days = sorted(pnl_history.keys())
    pnl_chart_data = {
        'dates': sorted_days,
        'daily_pnl': [round(pnl_history[d], 2) for d in sorted_days],
        'cumulative': [],
    }
    cum = 0
    for d in sorted_days:
        cum += pnl_history[d]
        pnl_chart_data['cumulative'].append(round(cum, 2))

    dynamic_balance = get_dynamic_balance(account_id)
    compound_stake = get_compound_stake(account_id)

    short_used = sum(t.get('stake_remaining', t.get('stake', 0)) for t in open_short)
    long_used = sum(t.get('stake_remaining', t.get('stake', 0)) for t in open_long)
    total_used = short_used + long_used
    risk_max_pos_pct = float(account_param(account_id, 'RISK_MAX_POSITION_PCT',
                                           config.RISK_MAX_POSITION_PCT))
    max_position = dynamic_balance * risk_max_pos_pct
    available = max(0, max_position - total_used)

    pool_allocation = {
        'total': round(dynamic_balance, 2),
        'max_position': round(max_position, 2),
        'compound_stake': round(compound_stake, 2),
        'short_used': round(short_used, 2),
        'long_used': round(long_used, 2),
        'total_used': round(total_used, 2),
        'available': round(available, 2),
        'used_pct': round(total_used / max_position * 100, 1) if max_position > 0 else 0,
    }

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
    yesterday_pnl = pnl_history.get(yesterday, 0)
    risk_history = get_risk_history(pnl_history)
    risk_last_pause = risk_state.get('last_paused_at',
                                     risk_state.get('paused_until', None))

    return {
        'account': {
            'balance': round(dynamic_balance, 2),
            'initial_balance': float(account_param(account_id, 'ACCOUNT_BALANCE', config.ACCOUNT_BALANCE)),
            'leverage': int(account_param(account_id, 'LEVERAGE', config.LEVERAGE)),
            'today_pnl': round(today_pnl_short + today_pnl_long, 2),
            'total_pnl': round(total_pnl_short + total_pnl_long, 2),
            'win_rate': round(win_rate, 1),
            'total_trades': len(all_for_winrate),
        },
        'short_trades': {
            'open': open_short,
            'closed': closed_short[-20:],
            'today_pnl': round(today_pnl_short, 2),
            'total_pnl': round(total_pnl_short, 2),
        },
        'long_trades': {
            'open': open_long,
            'closed': closed_long[-20:],
            'today_pnl': round(today_pnl_long, 2),
            'total_pnl': round(total_pnl_long, 2),
        },
        'candidates': candidates,
        'long_candidates': get_long_candidates(),
        'risk': risk_state,
        'pnl_chart': pnl_chart_data,
        'pool': pool_allocation,
        'config': {
            'tp1_pct': round((1 - float(account_param(account_id, 'TP1_MULTIPLIER', config.TP1_MULTIPLIER))) * 100, 1),
            'tp2_pct': round((1 - float(account_param(account_id, 'TP2_MULTIPLIER', config.TP2_MULTIPLIER))) * 100, 1),
            'hard_stop_pct': float(account_param(account_id, 'HARD_STOP_LOSS_PCT', config.HARD_STOP_LOSS_PCT)),
            'trail_activate_pct': config.TRAIL_STOP_ACTIVATE_PCT,
            'max_hold_days': config.MAX_HOLD_DAYS,
            'max_daily_loss': float(account_param(account_id, 'RISK_MAX_DAILY_LOSS', config.RISK_MAX_DAILY_LOSS)),
            'max_daily_trades': int(account_param(account_id, 'RISK_MAX_DAILY_TRADES', config.RISK_MAX_DAILY_TRADES)),
        },
        'yesterday_pnl': round(yesterday_pnl, 2),
        'risk_history': risk_history,
        'risk_last_pause': risk_last_pause,
        'account_id': account_id or '',
        'timestamp': utcnow_iso(),
    }


# ══════════════════════════════════════════════════════════════════
#  Tail 读 + 执行指标
# ══════════════════════════════════════════════════════════════════

def read_tail_lines(path: str, max_lines: int = 1500) -> List[str]:
    """安全地读取文件尾部 ``max_lines`` 行，文件不存在或异常返回 []。"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        if len(lines) <= max_lines:
            return lines
        return lines[-max_lines:]
    except Exception:
        return []


def build_execution_metrics(account_id: Optional[str] = None,
                            minutes: int = 0) -> Dict[str, Any]:
    """
    从 ``execution_events.jsonl`` 构造执行/对账轻量指标。
    与原 dashboard.py 内 _build_execution_metrics 行为完全一致。
    """
    lines = read_tail_lines(EXECUTION_EVENTS_FILE, max_lines=2000)
    cutoff_dt = None
    if minutes and minutes > 0:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    total_created = total_filled = total_failed = 0
    close_created = close_filled = close_failed = 0
    reconcile_diffs = 0
    recent_errors: List[Dict[str, Any]] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue

        if cutoff_dt is not None:
            ts = ev.get('ts', '')
            try:
                ev_dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                if ev_dt.tzinfo is None:
                    ev_dt = ev_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if ev_dt < cutoff_dt:
                continue

        if account_id is not None:
            ev_acc = (ev.get('account_id') or '').strip()
            if ev_acc != (account_id or '').strip():
                continue

        et = ev.get('event_type', '')
        if et == 'order_created':
            total_created += 1
        elif et == 'order_filled':
            total_filled += 1
        elif et == 'order_failed':
            total_failed += 1
            if len(recent_errors) < 5:
                recent_errors.append({
                    'ts': ev.get('ts', ''),
                    'symbol': ev.get('symbol', ''),
                    'exchange': ev.get('exchange', ''),
                    'error': ev.get('error', ''),
                })
        elif et == 'close_created':
            close_created += 1
        elif et == 'close_filled':
            close_filled += 1
        elif et == 'close_failed':
            close_failed += 1
            if len(recent_errors) < 5:
                recent_errors.append({
                    'ts': ev.get('ts', ''),
                    'symbol': ev.get('symbol', ''),
                    'exchange': ev.get('exchange', ''),
                    'error': ev.get('error', ''),
                })
        elif et == 'position_reconcile_diff':
            reconcile_diffs += 1

    open_success_rate = round((total_filled / total_created) * 100, 1) if total_created > 0 else 0.0
    close_success_rate = round((close_filled / close_created) * 100, 1) if close_created > 0 else 0.0

    return {
        'open_orders': {
            'created': total_created,
            'filled': total_filled,
            'failed': total_failed,
            'success_rate': open_success_rate,
        },
        'close_orders': {
            'created': close_created,
            'filled': close_filled,
            'failed': close_failed,
            'success_rate': close_success_rate,
        },
        'reconcile': {'diff_events': reconcile_diffs},
        'recent_errors': recent_errors,
        'window_minutes': int(minutes or 0),
        'updated_at': utcnow_iso(),
    }


# ══════════════════════════════════════════════════════════════════
#  Risk history (7-day sparkline)
# ══════════════════════════════════════════════════════════════════

def get_risk_history(pnl_history: dict) -> List[float]:
    """最近 7 天的 daily loss（用于前端 sparkline）。负 PnL = 亏损。"""
    today_dt = datetime.now(timezone.utc).date()
    history = []
    for i in range(7, 0, -1):
        day = (today_dt - timedelta(days=i)).strftime('%Y-%m-%d')
        daily = pnl_history.get(day, 0)
        history.append(round(-daily if daily < 0 else 0, 2))
    return history


__all__ = [
    'get_long_candidates',
    'load_risk_v1_view',
    'get_dashboard_data',
    'read_tail_lines',
    'build_execution_metrics',
    'get_risk_history',
]
