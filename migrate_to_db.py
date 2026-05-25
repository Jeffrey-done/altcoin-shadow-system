#!/usr/bin/env python3
"""
JSON → 数据库迁移工具
将旧的 JSON 文件数据一次性导入到新的 SQLAlchemy 数据库。

用法:
  python3 migrate_to_db.py              # 迁移所有数据
  python3 migrate_to_db.py --dry-run    # 只统计，不写入
  python3 migrate_to_db.py --trades     # 只迁移交易记录
  python3 migrate_to_db.py --all        # 迁移所有（默认）

数据源:
  - altcoin_shadow_trades.json → trades 表
  - altcoin_trades_archive.json → trades 表（标记为已归档）
  - altcoin_candidates.json → candidates 表
  - risk_state.json → risk_states 表
  - altcoin_trades_inflight.json → inflight_journal 表
  - execution_events.jsonl → execution_events 表

注意:
  - 迁移是幂等的：重复运行不会产生重复数据（按 trade.id / candidate.symbol 去重）
  - 迁移前建议先备份 JSON 文件
  - 迁移完成后旧 JSON 文件保留不删除（作为回退保险）
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# 项目根目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from db.connection import get_session, init_db
from db.models import (
    TradeModel, CandidateModel, RiskStateModel,
    ExecutionEventModel, InFlightJournalModel,
)


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_iso(dt_str: str) -> datetime:
    """解析 ISO 时间字符串"""
    if not dt_str:
        return _utcnow()
    dt_str = dt_str.strip()
    try:
        dt = datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(str(dt_str)[:19])
        except (ValueError, TypeError):
            return _utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_json(filepath: str, default=None):
    """安全加载 JSON 文件"""
    if not os.path.exists(filepath):
        return default if default is not None else []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"  ⚠️ 读取失败 {filepath}: {e}")
        return default if default is not None else []


def _load_jsonl(filepath: str) -> list:
    """加载 JSONL 文件"""
    if not os.path.exists(filepath):
        return []
    records = []
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except IOError as e:
        print(f"  ⚠️ 读取失败 {filepath}: {e}")
    return records


# ══════════════════════════════════════════════════════════════════
#  迁移函数
# ══════════════════════════════════════════════════════════════════

def migrate_trades(dry_run: bool = False) -> dict:
    """迁移交易记录"""
    stats = {'total': 0, 'imported': 0, 'skipped': 0, 'errors': 0}

    # 主交易文件 + 归档文件
    trades_file = os.path.join(SCRIPT_DIR, 'altcoin_shadow_trades.json')
    archive_file = os.path.join(SCRIPT_DIR, 'altcoin_trades_archive.json')

    all_trades = []
    main_trades = _load_json(trades_file, [])
    archive_trades = _load_json(archive_file, [])
    all_trades.extend(main_trades)
    all_trades.extend(archive_trades)

    stats['total'] = len(all_trades)
    print(f"  📋 发现 {len(main_trades)} 笔活跃交易 + {len(archive_trades)} 笔归档交易")

    if dry_run:
        return stats

    with get_session() as session:
        # 获取已存在的 trade IDs
        existing_ids = {row[0] for row in session.query(TradeModel.id).all()}

        for t in all_trades:
            trade_id = t.get('id', '')
            if not trade_id:
                stats['errors'] += 1
                continue
            if trade_id in existing_ids:
                stats['skipped'] += 1
                continue

            try:
                # 构建 TradeModel 字段
                trade_data = {
                    'id': trade_id,
                    'symbol': t.get('symbol', ''),
                    'direction': t.get('direction', 'SHORT'),
                    'strategy': t.get('strategy', 'short_overbought'),
                    'status': t.get('status', 'open'),
                    'entry_price': float(t.get('entry_price', 0)),
                    'stake': float(t.get('stake', 0)),
                    'leverage': int(t.get('leverage', 10)),
                    'notional': float(t.get('notional', 0)),
                    'shares': float(t.get('shares', 0)),
                    'take_profit_1': float(t.get('take_profit_1', 0)),
                    'take_profit_2': float(t.get('take_profit_2', t.get('take_profit', 0))),
                    'tp1_triggered': bool(t.get('tp1_triggered', False)),
                    'tp1_locked_pnl': float(t.get('tp1_locked_pnl', 0)),
                    'stake_remaining': float(t.get('stake_remaining', t.get('stake', 0))),
                    'hard_stop_price': t.get('hard_stop_price'),
                    'best_pnl_pct': float(t.get('best_pnl_pct', 0)),
                    'trail_stop_price': t.get('trail_stop_price'),
                    'stop_loss': t.get('stop_loss'),
                    'max_hold_days': int(t.get('max_hold_days', 1)),
                    'pnl': float(t.get('pnl', 0)),
                    'current_price': t.get('current_price'),
                    'close_reason': t.get('close_reason'),
                    'close_type': t.get('close_type'),
                    'exchange': t.get('exchange', 'shadow'),
                    'live_order_id': t.get('live_order_id'),
                    'close_order_id': t.get('close_order_id'),
                    'tp1_close_order_id': t.get('tp1_close_order_id'),
                    'client_order_id': t.get('client_order_id', ''),
                    'account_id': t.get('account_id', ''),
                    'ref_price_at_order': float(t.get('ref_price_at_order', 0)),
                    'slippage_pct': float(t.get('slippage_pct', 0)),
                    'tp1_closed_shares': float(t.get('tp1_closed_shares', 0)),
                    'tp1_exit_price': float(t.get('tp1_exit_price', 0)),
                    'tp1_exit_ref_price': float(t.get('tp1_exit_ref_price', 0)),
                    'tp1_slippage_pct': float(t.get('tp1_slippage_pct', 0)),
                    'exit_ref_price': float(t.get('exit_ref_price', 0)),
                    'exit_slippage_pct': float(t.get('exit_slippage_pct', 0)),
                    'protect_stop_algo_id': t.get('protect_stop_algo_id'),
                    'protect_tp_algo_id': t.get('protect_tp_algo_id'),
                    'protect_stage': t.get('protect_stage', ''),
                    'source': t.get('source', ''),
                    'reason': t.get('reason', ''),
                    'opened_at': _parse_iso(t.get('opened_at', '')),
                    'closed_at': _parse_iso(t['closed_at']) if t.get('closed_at') else None,
                    'created_at': _utcnow(),
                    'updated_at': _utcnow(),
                }

                trade = TradeModel(**trade_data)
                session.add(trade)
                stats['imported'] += 1
            except Exception as e:
                stats['errors'] += 1
                print(f"  ❌ 导入失败 {trade_id}: {e}")

    return stats


def migrate_candidates(dry_run: bool = False) -> dict:
    """迁移候选池"""
    stats = {'total': 0, 'imported': 0, 'skipped': 0, 'errors': 0}

    candidates_file = os.path.join(SCRIPT_DIR, 'altcoin_candidates.json')
    candidates = _load_json(candidates_file, [])
    stats['total'] = len(candidates)
    print(f"  📋 发现 {len(candidates)} 个候选")

    if dry_run:
        return stats

    with get_session() as session:
        existing_symbols = {row[0] for row in session.query(CandidateModel.symbol).all()}

        for c in candidates:
            symbol = c.get('symbol', '')
            if not symbol:
                stats['errors'] += 1
                continue
            if symbol in existing_symbols:
                stats['skipped'] += 1
                continue

            try:
                candidate = CandidateModel(
                    symbol=symbol,
                    price=float(c.get('price', 0)),
                    vol24h=float(c.get('vol24h', 0)),
                    pct24h=float(c.get('pct24h', 0)),
                    rsi_1d=float(c.get('rsi_1d', 50)),
                    rsi_4h=c.get('rsi_4h'),
                    rsi_4h_peak=c.get('rsi_4h_peak'),
                    oi_change=float(c.get('oi_change', 0)),
                    funding_rate=float(c.get('funding_rate', 0)),
                    yao_score=int(c.get('yao_score', 0)),
                    triggered=bool(c.get('triggered', False)),
                    trigger_type=c.get('trigger_type'),
                    trigger_reason=c.get('trigger_reason'),
                    pending_open=bool(c.get('pending_open', False)),
                    pending_opened_at=_parse_iso(c['pending_opened_at']) if c.get('pending_opened_at') else None,
                    pending_open_retries=int(c.get('pending_open_retries', 0)),
                    added_at=_parse_iso(c.get('added_at', '')),
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
                session.add(candidate)
                stats['imported'] += 1
            except Exception as e:
                stats['errors'] += 1
                print(f"  ❌ 导入候选失败 {symbol}: {e}")

    return stats


def migrate_risk_state(dry_run: bool = False) -> dict:
    """迁移风控状态"""
    stats = {'total': 0, 'imported': 0, 'skipped': 0, 'errors': 0}

    risk_file = os.path.join(SCRIPT_DIR, 'risk_state.json')
    data = _load_json(risk_file, {})

    if not data:
        print("  📋 risk_state.json 为空，跳过")
        return stats

    # v1 格式检测
    if '_version' not in data and 'date' in data:
        data = {'_version': 2, 'accounts': {'_default': data}}

    accounts = data.get('accounts', {})
    stats['total'] = len(accounts)
    print(f"  📋 发现 {len(accounts)} 个账户的风控状态")

    if dry_run:
        return stats

    with get_session() as session:
        for acc_id, state_data in accounts.items():
            try:
                date_str = state_data.get('date', _utcnow().strftime('%Y-%m-%d'))

                existing = session.query(RiskStateModel).filter(
                    RiskStateModel.account_id == acc_id,
                    RiskStateModel.date == date_str,
                ).first()

                if existing:
                    stats['skipped'] += 1
                    continue

                paused_until = None
                if state_data.get('paused_until'):
                    paused_until = _parse_iso(state_data['paused_until'])

                risk_state = RiskStateModel(
                    account_id=acc_id,
                    date=date_str,
                    daily_loss=float(state_data.get('daily_loss', 0)),
                    daily_trades_opened=int(state_data.get('daily_trades_opened', 0)),
                    consecutive_losses=int(state_data.get('consecutive_losses', 0)),
                    paused_until=paused_until,
                    total_open_stake=float(state_data.get('total_open_stake', 0)),
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
                session.add(risk_state)
                stats['imported'] += 1
            except Exception as e:
                stats['errors'] += 1
                print(f"  ❌ 导入风控状态失败 [{acc_id}]: {e}")

    return stats


def migrate_inflight_journal(dry_run: bool = False) -> dict:
    """迁移 In-flight Journal"""
    stats = {'total': 0, 'imported': 0, 'skipped': 0, 'errors': 0}

    journal_file = os.path.join(SCRIPT_DIR, 'altcoin_trades_inflight.json')
    entries = _load_json(journal_file, [])
    stats['total'] = len(entries)
    print(f"  📋 发现 {len(entries)} 条 journal 记录")

    if dry_run:
        return stats

    with get_session() as session:
        existing_coids = {
            row[0] for row in session.query(InFlightJournalModel.client_order_id).all()
        }

        for entry in entries:
            coid = entry.get('client_order_id', '')
            if not coid:
                stats['errors'] += 1
                continue
            if coid in existing_coids:
                stats['skipped'] += 1
                continue

            try:
                journal = InFlightJournalModel(
                    client_order_id=coid,
                    exchange=entry.get('exchange', 'binance'),
                    account_id=entry.get('account_id', ''),
                    symbol=entry.get('symbol', ''),
                    direction=entry.get('direction', 'SHORT'),
                    stake=float(entry.get('stake', 0)),
                    leverage=int(entry.get('leverage', 10)),
                    status=entry.get('status', 'pending'),
                    order_id=entry.get('order_id', ''),
                    last_error=entry.get('last_error', ''),
                    created_at=_parse_iso(entry.get('created_at', '')),
                    updated_at=_parse_iso(entry.get('updated_at', '')),
                )
                session.add(journal)
                stats['imported'] += 1
            except Exception as e:
                stats['errors'] += 1
                print(f"  ❌ 导入 journal 失败 {coid}: {e}")

    return stats


def migrate_execution_events(dry_run: bool = False) -> dict:
    """迁移执行事件"""
    stats = {'total': 0, 'imported': 0, 'skipped': 0, 'errors': 0}

    events_file = os.path.join(SCRIPT_DIR, 'execution_events.jsonl')
    records = _load_jsonl(events_file)
    stats['total'] = len(records)
    print(f"  📋 发现 {len(records)} 条执行事件")

    if dry_run:
        return stats

    known_fields = {
        'exchange', 'symbol', 'direction', 'account_id',
        'client_order_id', 'order_id', 'stake', 'leverage',
        'fill_price', 'fill_amount', 'error', 'error_code',
    }

    with get_session() as session:
        batch = []
        for record in records:
            try:
                event_type = record.get('event_type', '')
                if not event_type:
                    stats['errors'] += 1
                    continue

                model_data = {'event_type': event_type}
                extra = {}

                for key, value in record.items():
                    if key in ('ts', 'event_type'):
                        continue
                    if key in known_fields:
                        model_data[key] = value
                    else:
                        extra[key] = value

                if extra:
                    model_data['extra_data'] = json.dumps(extra, ensure_ascii=False)

                model_data['timestamp'] = _parse_iso(record.get('ts', ''))

                batch.append(ExecutionEventModel(**model_data))
                stats['imported'] += 1

                # 批量写入（每 500 条 flush 一次）
                if len(batch) >= 500:
                    session.add_all(batch)
                    session.flush()
                    batch = []
            except Exception as e:
                stats['errors'] += 1

        if batch:
            session.add_all(batch)

    return stats


# ══════════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='JSON → DB 迁移工具')
    parser.add_argument('--dry-run', action='store_true', help='只统计，不写入')
    parser.add_argument('--trades', action='store_true', help='只迁移交易记录')
    parser.add_argument('--candidates', action='store_true', help='只迁移候选池')
    parser.add_argument('--risk', action='store_true', help='只迁移风控状态')
    parser.add_argument('--journal', action='store_true', help='只迁移 journal')
    parser.add_argument('--events', action='store_true', help='只迁移执行事件')
    parser.add_argument('--all', action='store_true', help='迁移所有数据（默认）')
    args = parser.parse_args()

    # 如果没有指定任何具体项，默认 --all
    migrate_all = args.all or not any([
        args.trades, args.candidates, args.risk, args.journal, args.events
    ])

    mode = "🔍 DRY RUN（不写入）" if args.dry_run else "🚀 正式迁移"
    print(f"\n{'='*60}")
    print(f"  JSON → DB 迁移工具 | {mode}")
    print(f"{'='*60}\n")

    # 初始化数据库（创建表）
    if not args.dry_run:
        print("📦 初始化数据库...")
        init_db()
        print("  ✅ 数据库表已创建\n")

    results = {}

    if migrate_all or args.trades:
        print("📊 迁移交易记录...")
        results['trades'] = migrate_trades(args.dry_run)
        _print_stats('trades', results['trades'])

    if migrate_all or args.candidates:
        print("📊 迁移候选池...")
        results['candidates'] = migrate_candidates(args.dry_run)
        _print_stats('candidates', results['candidates'])

    if migrate_all or args.risk:
        print("📊 迁移风控状态...")
        results['risk'] = migrate_risk_state(args.dry_run)
        _print_stats('risk', results['risk'])

    if migrate_all or args.journal:
        print("📊 迁移 In-flight Journal...")
        results['journal'] = migrate_inflight_journal(args.dry_run)
        _print_stats('journal', results['journal'])

    if migrate_all or args.events:
        print("📊 迁移执行事件...")
        results['events'] = migrate_execution_events(args.dry_run)
        _print_stats('events', results['events'])

    # 汇总
    print(f"\n{'='*60}")
    print("  📊 迁移汇总")
    print(f"{'='*60}")
    total_imported = sum(r.get('imported', 0) for r in results.values())
    total_skipped = sum(r.get('skipped', 0) for r in results.values())
    total_errors = sum(r.get('errors', 0) for r in results.values())
    print(f"  ✅ 导入: {total_imported}")
    print(f"  ⏭️  跳过: {total_skipped}（已存在）")
    print(f"  ❌ 错误: {total_errors}")

    if not args.dry_run:
        print(f"\n  💾 数据库已更新")
        print(f"  📝 旧 JSON 文件保留（作为回退备份）")
        print(f"  🔄 后续可删除 JSON 文件完成迁移")
    print()


def _print_stats(name: str, stats: dict):
    print(f"  → {name}: 总计={stats['total']} 导入={stats['imported']} "
          f"跳过={stats['skipped']} 错误={stats['errors']}\n")


if __name__ == '__main__':
    main()
