"""Initial schema - all tables

Revision ID: 001
Revises: None
Create Date: 2026-05-25

所有表初始创建：
  - trades: 交易记录
  - candidates: 候选池
  - risk_states: 风控状态
  - execution_events: 执行事件
  - task_metrics: 任务指标
  - inflight_journal: In-flight Journal
  - signal_logs: 信号评分日志
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── trades ──
    op.create_table(
        'trades',
        sa.Column('id', sa.String(128), primary_key=True),
        sa.Column('symbol', sa.String(32), nullable=False),
        sa.Column('direction', sa.String(8), nullable=False, server_default='SHORT'),
        sa.Column('strategy', sa.String(64), nullable=False, server_default='short_overbought'),
        sa.Column('status', sa.String(16), nullable=False, server_default='open'),
        sa.Column('entry_price', sa.Float, nullable=False),
        sa.Column('stake', sa.Float, nullable=False),
        sa.Column('leverage', sa.Integer, nullable=False, server_default='10'),
        sa.Column('notional', sa.Float, nullable=False, server_default='0'),
        sa.Column('shares', sa.Float, nullable=False, server_default='0'),
        sa.Column('take_profit_1', sa.Float, server_default='0'),
        sa.Column('take_profit_2', sa.Float, server_default='0'),
        sa.Column('tp1_triggered', sa.Boolean, server_default='0'),
        sa.Column('tp1_locked_pnl', sa.Float, server_default='0'),
        sa.Column('stake_remaining', sa.Float, server_default='0'),
        sa.Column('hard_stop_price', sa.Float, nullable=True),
        sa.Column('best_pnl_pct', sa.Float, server_default='0'),
        sa.Column('trail_stop_price', sa.Float, nullable=True),
        sa.Column('stop_loss', sa.Float, nullable=True),
        sa.Column('max_hold_days', sa.Integer, server_default='1'),
        sa.Column('pnl', sa.Float, server_default='0'),
        sa.Column('current_price', sa.Float, nullable=True),
        sa.Column('close_reason', sa.Text, nullable=True),
        sa.Column('close_type', sa.String(32), nullable=True),
        sa.Column('exchange', sa.String(16), nullable=False, server_default='shadow'),
        sa.Column('live_order_id', sa.String(128), nullable=True),
        sa.Column('close_order_id', sa.String(128), nullable=True),
        sa.Column('tp1_close_order_id', sa.String(128), nullable=True),
        sa.Column('client_order_id', sa.String(128), server_default=''),
        sa.Column('account_id', sa.String(64), nullable=False, server_default=''),
        sa.Column('ref_price_at_order', sa.Float, server_default='0'),
        sa.Column('slippage_pct', sa.Float, server_default='0'),
        sa.Column('tp1_closed_shares', sa.Float, server_default='0'),
        sa.Column('tp1_exit_price', sa.Float, server_default='0'),
        sa.Column('tp1_exit_ref_price', sa.Float, server_default='0'),
        sa.Column('tp1_slippage_pct', sa.Float, server_default='0'),
        sa.Column('exit_ref_price', sa.Float, server_default='0'),
        sa.Column('exit_slippage_pct', sa.Float, server_default='0'),
        sa.Column('protect_stop_algo_id', sa.String(128), nullable=True),
        sa.Column('protect_tp_algo_id', sa.String(128), nullable=True),
        sa.Column('protect_stage', sa.String(16), server_default=''),
        sa.Column('source', sa.String(128), server_default=''),
        sa.Column('reason', sa.Text, server_default=''),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_trades_symbol', 'trades', ['symbol'])
    op.create_index('ix_trades_status', 'trades', ['status'])
    op.create_index('ix_trades_strategy', 'trades', ['strategy'])
    op.create_index('ix_trades_account_id', 'trades', ['account_id'])
    op.create_index('ix_trades_status_account', 'trades', ['status', 'account_id'])
    op.create_index('ix_trades_symbol_status', 'trades', ['symbol', 'status'])
    op.create_index('ix_trades_strategy_status', 'trades', ['strategy', 'status'])
    op.create_index('ix_trades_opened_at', 'trades', ['opened_at'])
    op.create_index('ix_trades_closed_at', 'trades', ['closed_at'])

    # ── candidates ──
    op.create_table(
        'candidates',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(32), nullable=False, unique=True),
        sa.Column('price', sa.Float, nullable=False),
        sa.Column('vol24h', sa.Float, server_default='0'),
        sa.Column('pct24h', sa.Float, server_default='0'),
        sa.Column('rsi_1d', sa.Float, server_default='50'),
        sa.Column('rsi_4h', sa.Float, nullable=True),
        sa.Column('rsi_4h_peak', sa.Float, nullable=True),
        sa.Column('oi_change', sa.Float, server_default='0'),
        sa.Column('funding_rate', sa.Float, server_default='0'),
        sa.Column('yao_score', sa.Integer, server_default='0'),
        sa.Column('triggered', sa.Boolean, server_default='0'),
        sa.Column('trigger_type', sa.String(16), nullable=True),
        sa.Column('trigger_reason', sa.Text, nullable=True),
        sa.Column('pending_open', sa.Boolean, server_default='0'),
        sa.Column('pending_opened_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('pending_open_retries', sa.Integer, server_default='0'),
        sa.Column('added_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_candidates_symbol', 'candidates', ['symbol'])
    op.create_index('ix_candidates_triggered', 'candidates', ['triggered'])
    op.create_index('ix_candidates_added_at', 'candidates', ['added_at'])

    # ── risk_states ──
    op.create_table(
        'risk_states',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('account_id', sa.String(64), nullable=False),
        sa.Column('date', sa.String(10), nullable=False),
        sa.Column('daily_loss', sa.Float, server_default='0'),
        sa.Column('daily_trades_opened', sa.Integer, server_default='0'),
        sa.Column('consecutive_losses', sa.Integer, server_default='0'),
        sa.Column('paused_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('total_open_stake', sa.Float, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_risk_account_id', 'risk_states', ['account_id'])
    op.create_index('ix_risk_account_date', 'risk_states', ['account_id', 'date'], unique=True)

    # ── execution_events ──
    op.create_table(
        'execution_events',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('exchange', sa.String(16), nullable=True),
        sa.Column('symbol', sa.String(32), nullable=True),
        sa.Column('direction', sa.String(8), nullable=True),
        sa.Column('account_id', sa.String(64), nullable=True),
        sa.Column('client_order_id', sa.String(128), nullable=True),
        sa.Column('order_id', sa.String(128), nullable=True),
        sa.Column('stake', sa.Float, nullable=True),
        sa.Column('leverage', sa.Integer, nullable=True),
        sa.Column('fill_price', sa.Float, nullable=True),
        sa.Column('fill_amount', sa.Float, nullable=True),
        sa.Column('error', sa.Text, nullable=True),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('extra_data', sa.Text, nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_events_event_type', 'execution_events', ['event_type'])
    op.create_index('ix_events_symbol', 'execution_events', ['symbol'])
    op.create_index('ix_events_account_id', 'execution_events', ['account_id'])
    op.create_index('ix_events_timestamp', 'execution_events', ['timestamp'])
    op.create_index('ix_events_type_ts', 'execution_events', ['event_type', 'timestamp'])
    op.create_index('ix_events_symbol_ts', 'execution_events', ['symbol', 'timestamp'])

    # ── task_metrics ──
    op.create_table(
        'task_metrics',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('mode', sa.String(16), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('duration_sec', sa.Float, nullable=True),
        sa.Column('timeout_sec', sa.Float, nullable=True),
        sa.Column('exitcode', sa.Integer, nullable=True),
        sa.Column('pid', sa.Integer, nullable=True),
        sa.Column('spawn_dt', sa.Float, nullable=True),
        sa.Column('error', sa.Text, nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_task_metrics_name', 'task_metrics', ['name'])
    op.create_index('ix_task_metrics_status', 'task_metrics', ['status'])
    op.create_index('ix_task_metrics_timestamp', 'task_metrics', ['timestamp'])
    op.create_index('ix_task_metrics_name_ts', 'task_metrics', ['name', 'timestamp'])

    # ── inflight_journal ──
    op.create_table(
        'inflight_journal',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('client_order_id', sa.String(128), nullable=False, unique=True),
        sa.Column('exchange', sa.String(16), nullable=False),
        sa.Column('account_id', sa.String(64), nullable=False, server_default=''),
        sa.Column('symbol', sa.String(32), nullable=False),
        sa.Column('direction', sa.String(8), nullable=False),
        sa.Column('stake', sa.Float, nullable=False),
        sa.Column('leverage', sa.Integer, nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('order_id', sa.String(128), server_default=''),
        sa.Column('last_error', sa.Text, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_journal_client_order_id', 'inflight_journal', ['client_order_id'])
    op.create_index('ix_journal_status', 'inflight_journal', ['status'])

    # ── signal_logs ──
    op.create_table(
        'signal_logs',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(32), nullable=False),
        sa.Column('strategy', sa.String(64), nullable=False, server_default='short_overbought'),
        sa.Column('score', sa.Integer, nullable=False),
        sa.Column('grade', sa.String(4), nullable=False),
        sa.Column('rsi_score', sa.Float, server_default='0'),
        sa.Column('yao_score', sa.Float, server_default='0'),
        sa.Column('trigger_score', sa.Float, server_default='0'),
        sa.Column('heat_score', sa.Float, server_default='0'),
        sa.Column('cross_validate_bonus', sa.Integer, server_default='0'),
        sa.Column('vol_divergence_bonus', sa.Integer, server_default='0'),
        sa.Column('rsi_1d', sa.Float, nullable=True),
        sa.Column('rsi_4h', sa.Float, nullable=True),
        sa.Column('pct_24h', sa.Float, nullable=True),
        sa.Column('oi_change', sa.Float, nullable=True),
        sa.Column('funding_rate', sa.Float, nullable=True),
        sa.Column('btc_24h_pct', sa.Float, nullable=True),
        sa.Column('trigger_type', sa.String(16), nullable=True),
        sa.Column('triggered_open', sa.Boolean, server_default='0'),
        sa.Column('trade_id', sa.String(128), nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_signal_logs_symbol', 'signal_logs', ['symbol'])
    op.create_index('ix_signal_logs_timestamp', 'signal_logs', ['timestamp'])
    op.create_index('ix_signal_logs_symbol_ts', 'signal_logs', ['symbol', 'timestamp'])
    op.create_index('ix_signal_logs_strategy_ts', 'signal_logs', ['strategy', 'timestamp'])


def downgrade() -> None:
    op.drop_table('signal_logs')
    op.drop_table('inflight_journal')
    op.drop_table('task_metrics')
    op.drop_table('execution_events')
    op.drop_table('risk_states')
    op.drop_table('candidates')
    op.drop_table('trades')
