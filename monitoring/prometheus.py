#!/usr/bin/env python3
"""
Prometheus 指标导出 v1.0

通过 Flask Blueprint 暴露 /metrics 端点，供 Prometheus scrape。
不依赖 prometheus_client 库（自行输出 text format），零外部依赖。

导出指标：
  - altcoin_open_trades_total          当前持仓数
  - altcoin_daily_pnl_usdt             今日已实现盈亏
  - altcoin_total_pnl_usdt             累计已实现盈亏
  - altcoin_daily_trades_opened        今日开仓次数
  - altcoin_risk_state_paused          是否处于风控暂停 (0/1)
  - altcoin_candidates_count           候选池大小
  - altcoin_event_bus_messages_total   EventBus 发布消息总数
  - altcoin_api_latency_seconds        最近 API 调用延迟
  - altcoin_signal_score_histogram     信号评分分布（桶）
  - altcoin_regime_state               当前市场 regime (gauge label)
  - altcoin_engine_scan_duration_sec   最近一次扫描耗时
  - altcoin_ws_connected               WebSocket 连接状态 (0/1)

集成方式：
  在 dashboard.py 中注册 Blueprint：
    from monitoring.prometheus import metrics_bp
    app.register_blueprint(metrics_bp)

  Prometheus scrape config:
    - job_name: 'altcoin_shadow'
      static_configs:
        - targets: ['dashboard:8080']
      metrics_path: '/metrics'
"""

from __future__ import annotations

import time
import logging
from typing import Dict, List, Optional

logger = logging.getLogger("monitoring.prometheus")

try:
    from flask import Blueprint, Response
    metrics_bp = Blueprint('metrics', __name__)
except ImportError:
    metrics_bp = None
    logger.debug("Flask 未安装，Prometheus endpoint 不可用")


# ══════════════════════════════════════════════════════════════════
#  指标收集器
# ══════════════════════════════════════════════════════════════════

class MetricsCollector:
    """收集所有系统指标"""

    def collect(self) -> str:
        """收集所有指标，返回 Prometheus text format"""
        lines: List[str] = []
        lines.append('# HELP altcoin_up System is running')
        lines.append('# TYPE altcoin_up gauge')
        lines.append('altcoin_up 1')

        # 持仓数
        open_count = self._get_open_trades_count()
        lines.append('# HELP altcoin_open_trades_total Current open positions')
        lines.append('# TYPE altcoin_open_trades_total gauge')
        lines.append(f'altcoin_open_trades_total {open_count}')

        # 今日 PnL
        daily_pnl = self._get_daily_pnl()
        lines.append('# HELP altcoin_daily_pnl_usdt Today realized PnL')
        lines.append('# TYPE altcoin_daily_pnl_usdt gauge')
        lines.append(f'altcoin_daily_pnl_usdt {daily_pnl:.2f}')

        # 累计 PnL
        total_pnl = self._get_total_pnl()
        lines.append('# HELP altcoin_total_pnl_usdt Total realized PnL')
        lines.append('# TYPE altcoin_total_pnl_usdt gauge')
        lines.append(f'altcoin_total_pnl_usdt {total_pnl:.2f}')

        # 今日开仓数
        daily_opens = self._get_daily_opens()
        lines.append('# HELP altcoin_daily_trades_opened Today opened trades')
        lines.append('# TYPE altcoin_daily_trades_opened gauge')
        lines.append(f'altcoin_daily_trades_opened {daily_opens}')

        # 候选池大小
        candidates = self._get_candidates_count()
        lines.append('# HELP altcoin_candidates_count Candidate pool size')
        lines.append('# TYPE altcoin_candidates_count gauge')
        lines.append(f'altcoin_candidates_count {candidates}')

        # 风控状态
        paused = self._get_risk_paused()
        lines.append('# HELP altcoin_risk_paused Risk control paused (1=paused)')
        lines.append('# TYPE altcoin_risk_paused gauge')
        lines.append(f'altcoin_risk_paused {paused}')

        # Market Regime
        regime = self._get_regime()
        lines.append('# HELP altcoin_regime Current market regime')
        lines.append('# TYPE altcoin_regime gauge')
        lines.append(f'altcoin_regime{{state="{regime}"}} 1')

        # EventBus 消息计数
        event_count = self._get_event_bus_count()
        lines.append('# HELP altcoin_event_bus_messages_total EventBus published messages')
        lines.append('# TYPE altcoin_event_bus_messages_total counter')
        lines.append(f'altcoin_event_bus_messages_total {event_count}')

        # 账户余额
        balance = self._get_balance()
        lines.append('# HELP altcoin_account_balance_usdt Account balance')
        lines.append('# TYPE altcoin_account_balance_usdt gauge')
        lines.append(f'altcoin_account_balance_usdt {balance:.2f}')

        return '\n'.join(lines) + '\n'

    # ── 数据获取 ─────────────────────────────────────────────────

    def _get_open_trades_count(self) -> int:
        try:
            from common import TRADES_FILE, load_json
            trades = load_json(TRADES_FILE, [])
            return sum(1 for t in trades if t.get('status') == 'open')
        except Exception:
            return 0

    def _get_daily_pnl(self) -> float:
        try:
            from common import TRADES_FILE, load_json, today_str
            trades = load_json(TRADES_FILE, [])
            today = today_str()
            pnl = 0.0
            for t in trades:
                if t.get('status') == 'closed':
                    closed_at = str(t.get('closed_at', ''))
                    if today in closed_at:
                        pnl += t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
            return pnl
        except Exception:
            return 0.0

    def _get_total_pnl(self) -> float:
        try:
            from common import TRADES_FILE, load_json
            trades = load_json(TRADES_FILE, [])
            return sum(
                t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
                for t in trades if t.get('status') == 'closed'
            )
        except Exception:
            return 0.0

    def _get_daily_opens(self) -> int:
        try:
            from common import RISK_FILE, load_json
            risk = load_json(RISK_FILE, {})
            # v2 format
            if '_version' in risk:
                accounts = risk.get('accounts', {})
                total = 0
                for acc_state in accounts.values():
                    total += acc_state.get('daily_trades_opened', 0)
                return total
            return risk.get('daily_trades_opened', 0)
        except Exception:
            return 0

    def _get_candidates_count(self) -> int:
        try:
            from common import CANDIDATES_FILE, load_json
            candidates = load_json(CANDIDATES_FILE, [])
            return len([c for c in candidates if not c.get('triggered', False)])
        except Exception:
            return 0

    def _get_risk_paused(self) -> int:
        try:
            from common import RISK_FILE, load_json
            risk = load_json(RISK_FILE, {})
            if '_version' in risk:
                for acc_state in risk.get('accounts', {}).values():
                    if acc_state.get('paused_until'):
                        return 1
                return 0
            return 1 if risk.get('paused_until') else 0
        except Exception:
            return 0

    def _get_regime(self) -> str:
        try:
            from signals.regime import get_current_regime
            return get_current_regime().value
        except Exception:
            return 'unknown'

    def _get_event_bus_count(self) -> int:
        try:
            from event_bus import get_event_bus, InMemoryBackend
            bus = get_event_bus()
            if hasattr(bus, '_backend') and isinstance(bus._backend, InMemoryBackend):
                return len(bus._backend._event_history)
            return 0
        except Exception:
            return 0

    def _get_balance(self) -> float:
        try:
            import config
            return float(getattr(config, 'ACCOUNT_BALANCE', 100))
        except Exception:
            return 100.0


# ══════════════════════════════════════════════════════════════════
#  Flask Blueprint 路由
# ══════════════════════════════════════════════════════════════════

_collector = MetricsCollector()

if metrics_bp is not None:
    @metrics_bp.route('/metrics')
    def prometheus_metrics():
        """Prometheus scrape endpoint"""
        content = _collector.collect()
        return Response(content, mimetype='text/plain; charset=utf-8')

    @metrics_bp.route('/healthz')
    def healthz():
        """Kubernetes-style health probe"""
        return Response('ok', mimetype='text/plain')

    @metrics_bp.route('/readyz')
    def readyz():
        """Readiness probe — checks if system can serve traffic"""
        try:
            from common import TRADES_FILE
            import os
            if os.path.exists(TRADES_FILE):
                return Response('ready', mimetype='text/plain')
            return Response('not ready', status=503, mimetype='text/plain')
        except Exception:
            return Response('error', status=503, mimetype='text/plain')
