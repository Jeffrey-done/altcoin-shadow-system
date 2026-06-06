#!/usr/bin/env python3
"""
影子做空系统实时仪表盘 v4.0 - Short-Only Architecture
Flask + SocketIO + Jinja2 Templates + Modular Static Files

Features:
- Jinja2 template-based rendering (templates/)
- Modular CSS/JS in static/ directory
- Token-based API authentication (optional)
- ETag/Last-Modified for static data endpoints
- New API endpoints: /api/events, yesterday_pnl, risk_history
- Mobile responsive with bottom tab bar
- Dark/Light theme with CSS variables
- Real-time SocketIO + Binance WebSocket

启动：python3 dashboard.py [--port 8080]
访问：http://localhost:8080

M1 修复（2026-05）— 模块拆分
================================
原本本文件 1535 行职责糅合，现在仅保留 Flask / SocketIO 路由层 + 启动入口；
所有数据读取、事件抽取、实时价格、token 认证逻辑已迁移到 ``dashboard_app/``
子包，详见 ``docs/UNIFIED_ARCHITECTURE.md``。
"""

# ══════════════════════════════════════════════════════════════════
#  eventlet monkey_patch 必须在 stdlib(socket/threading/time/...) 之前!
# ══════════════════════════════════════════════════════════════════
# requirements.txt 装了 eventlet，python-socketio 会自动选 eventlet 后端跑
# socketio.run()。但如果不 monkey_patch，stdlib 的阻塞 IO（requests 走的
# urllib3、threading.Lock、time.sleep）就还是真阻塞，会把 eventlet hub 卡死，
# 表现就是：前端 SocketIO 心跳超时 → WiFi 图标变红 → 几分钟后才恢复。
# 关闭 thread=False：业务代码里 ThreadPoolExecutor 仍要用真 OS 线程跑
# ccxt 同步调用，不要把 threading 协程化（否则 ccxt 内部 socket 会和 eventlet
# 的 greenlet hub 互锁）。
import eventlet
eventlet.monkey_patch(thread=False)

import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, jsonify, request, make_response, redirect
from flask_socketio import SocketIO, emit as socketio_emit

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from common import (
    TRADES_FILE, CANDIDATES_FILE, RISK_FILE,
    WEEKLY_REPORT_FILE, EXECUTION_EVENTS_FILE,
    load_json, utcnow_iso, today_str, get_dynamic_balance, get_compound_stake,
    get_current_account_id, filter_trades_by_account, account_param,
)

# ── M1: 拆分后的子模块 ─────────────────────────────────────────────
from dashboard_app.auth import (
    check_api_token,
    require_auth as _require_auth,
    Unauthorized,
)
from dashboard_app.data import (
    get_dashboard_data,
    build_execution_metrics as _build_execution_metrics,
    get_long_candidates as _get_long_candidates,
    load_risk_v1_view as _load_risk_v1_view,
    read_tail_lines as _read_tail_lines,
    get_risk_history as _get_risk_history,
)
from dashboard_app.events import extract_events as _extract_events
from dashboard_app.live_prices import (
    inject_live_prices as _inject_live_prices,
    fetch_live_prices as _fetch_live_prices,
    make_background_push,
)
from dashboard_app.etag import make_etag_response as _make_etag_response

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__,
            template_folder=os.path.join(SCRIPT_DIR, 'templates'),
            static_folder=os.path.join(SCRIPT_DIR, 'static'))
# SECRET_KEY 从环境变量读取；未设置时用 token_urlsafe 生成临时随机值（重启后会变，
# 导致已登录 session 失效，但本系统 Dashboard 主要是只读接口，影响可忽略）。
_secret = os.environ.get('DASHBOARD_SECRET_KEY', '')
if not _secret:
    import secrets as _secrets
    _secret = _secrets.token_urlsafe(32)
app.config['SECRET_KEY'] = _secret
# Session cookie 安全加固（admin panel 依赖 session 存登录态）
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
# 只在请求本身是 https 时才把 cookie 标 Secure，免得本地 http 测试拿不到 cookie
if os.environ.get('DASHBOARD_FORCE_HTTPS_COOKIE', '').lower() in ('1', 'true', 'yes'):
    app.config['SESSION_COOKIE_SECURE'] = True
# CORS 配置：从环境变量读取允许的源，默认只允许同源（不暴露到公网）
_cors_origins = os.environ.get('DASHBOARD_CORS_ORIGINS', '').strip()
if _cors_origins:
    # 支持逗号分隔多个源，如 "http://localhost:3000,https://mydomain.com"
    _cors_list = [o.strip() for o in _cors_origins.split(',') if o.strip()]
else:
    _cors_list = []  # 空列表 = 仅同源
socketio = SocketIO(
    app,
    cors_allowed_origins=_cors_list if _cors_list else None,
    # 显式声明 eventlet：避免 python-socketio 在多个候选后端中自动选择时
    # 因为 import 顺序选成 threading（threading 模式下 emit 跨线程是 unsafe 的）
    async_mode='eventlet',
)


# ══════════════════════════════════════════════════════════════════
#  静态资源缓存（B12）
# ══════════════════════════════════════════════════════════════════
# 之前 /static/ 没有任何 Cache-Control，每次刷新都重新下载
# lucide.min.js (~340KB) / chart.js / app.js 等。给一天缓存，刷新前端时
# 用 hard reload (Ctrl+Shift+R) 跳过缓存即可。
@app.after_request
def _add_static_cache(resp):
    if request.path.startswith('/static/'):
        # public：允许 CDN/反代缓存；max-age=86400：一天
        resp.headers.setdefault('Cache-Control', 'public, max-age=86400')
    return resp

# ══════════════════════════════════════════════════════════════════
#  Admin Panel (挂载在 /<ADMIN_URL_SECRET>/ 下)
# ══════════════════════════════════════════════════════════════════
# 关键安全设计：如果 ADMIN_URL_SECRET 环境变量未设置，整个 blueprint 不加载。
# 这意味着:
#   - 任何 /admin /login /config 之类的 GET 都会走 Flask 默认的 404 处理
#   - 互联网扫描器扫不到任何 admin 相关路径
#   - 要访问面板必须：① 知道精确的 secret 前缀 ② 知道完整 URL
# 生成 secret: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
#
# 安全脱敏（2026-05 修复）：启动日志中**不打印** secret 的任何相关信息
#   （包括长度、是否短、提示路径前缀等），避免在共享终端 / 日志聚合系统
#   / 截图分享中无意泄露线索。强度告警转为 logger 级别（写文件 + stderr 但
#   不带数值），运维需要时可在审计日志里查。
import logging as _admin_log
_admin_url_secret = os.environ.get('ADMIN_URL_SECRET', '').strip()
if _admin_url_secret:
    if len(_admin_url_secret) < 16:
        # 仅记录到 logger，不 print 到 stdout，且不暴露具体长度数值
        _admin_log.getLogger("dashboard").warning(
            "ADMIN_URL_SECRET 强度不足，建议至少 32 字节随机串："
            "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    try:
        from admin_panel import create_blueprint as _create_admin_bp
        app.register_blueprint(_create_admin_bp(_admin_url_secret))
        print("🔐 Admin Panel 已启用 ✓")
    except Exception as _e:
        # 加载失败信息可能含路径，谨慎处理：仅打印异常类型，不打印 _e 全文
        print(f"⚠️  Admin Panel 加载失败: {type(_e).__name__}")
        _admin_log.getLogger("dashboard").error(
            "Admin Panel 加载失败", exc_info=True
        )
else:
    print("🔐 Admin Panel 未启用（ADMIN_URL_SECRET 未设置）")

# ── Prometheus 指标端点 /metrics ──────────────────────────────────
try:
    from monitoring.prometheus import metrics_bp
    if metrics_bp is not None:
        app.register_blueprint(metrics_bp)
        print("📊 Prometheus /metrics 端点已启用 ✓")
except Exception as _prom_e:
    print(f"⚠️  Prometheus metrics 加载跳过: {type(_prom_e).__name__}")

BATCH_BACKTEST_RESULTS_FILE = os.path.join(SCRIPT_DIR, 'batch_backtest_results.json')


# ══════════════════════════════════════════════════════════════════
#  ETag Helper
# ══════════════════════════════════════════════════════════════════
# (M1: 实现已迁移到 dashboard_app/etag.py — 这里仅保留导入别名 _make_etag_response)


# ══════════════════════════════════════════════════════════════════
#  Routes - Pages (Jinja2 templates)
# ══════════════════════════════════════════════════════════════════
# 本节及以下：所有 @app.route / @socketio.on 处理函数。
# 数据读取、事件抽取、实时价格、ETag 都已迁移到 dashboard_app/ 子包。

@app.route('/')
def index():
    _require_auth()
    return render_template('index.html')


@app.route('/weekly-report')
def weekly_report_page():
    _require_auth()
    return render_template('weekly_report.html')


@app.route('/batch-backtest')
def batch_backtest_page():
    _require_auth()
    return render_template('batch_backtest.html')


@app.route('/backtest')
def backtest_page():
    _require_auth()
    return render_template('backtest.html')


@app.route('/signal-scores')
def signal_scores_page():
    _require_auth()
    return render_template('signal_scores.html')


@app.route('/admin')
def admin_redirect():
    """统一入口：从主面板跳转到 Admin Panel（保留安全机制）"""
    _require_auth()
    secret = os.environ.get('ADMIN_URL_SECRET', '').strip()
    if not secret:
        return render_template('admin_unavailable.html'), 503
    return redirect(f'/{secret}/')



# ══════════════════════════════════════════════════════════════════
#  Routes - API Endpoints
# ══════════════════════════════════════════════════════════════════

@app.route('/api/data')
@check_api_token
def api_data():
    # Admin panel 改了配置后，下一次 /api/data 就能反映最新值
    try:
        from runtime_config import apply_overrides as _apply_rc
        _apply_rc()
    except Exception:
        pass
    # 允许前端通过 ?account_id=xxx 切换查看的账户（纯视图，不影响后台活跃账户）
    account_id = request.args.get('account_id', '').strip() or None
    data = get_dashboard_data(account_id)
    data = _inject_live_prices(data)
    return jsonify(data)


@app.route('/api/events')
@check_api_token
def api_events():
    """Return last 50 events extracted from trade files."""
    events = _extract_events()
    return jsonify({'events': events})


@app.route('/api/accounts/overview')
@check_api_token
def api_accounts_overview():
    """
    返回所有账户的概览数据（全局视图，不受 active_account 影响）。
    用于前端多账户看板，管理员切换账户不改变此视图。
    """
    try:
        from admin_secrets import list_accounts, get_active_account_id, SHADOW_ACCOUNT_ID
    except ImportError:
        return jsonify({'accounts': [], 'active_account': ''})

    # 确保每次请求都读到最新的 runtime_config(Admin Panel 切换
    # LIVE_MODE 后立即生效,不用重启进程)
    try:
        from runtime_config import apply_overrides as _apply_rc
        _apply_rc()
    except Exception:
        pass

    all_accounts = list_accounts()
    active_id = get_active_account_id()
    try:
        from db.compat import load_all_trades
        all_trades = load_all_trades()
    except Exception:
        all_trades = load_json(TRADES_FILE, [])

    # B9 修复：之前对每个账户都遍历整个 all_trades 一遍 (O(账户数 × 交易数))，
    # 现在单遍把 trades 按 account_id 分桶到 dict (O(交易数 + 账户数))。
    # 注：filter_trades_by_account 把"无 account_id 的旧交易"归属影子账户，
    #     这里也要保持同样的兼容语义。
    buckets: dict = {}
    legacy_unmarked: list = []  # account_id == '' 的旧交易
    for t in all_trades:
        acc = t.get('account_id', '')
        if not acc:
            legacy_unmarked.append(t)
        else:
            buckets.setdefault(acc, []).append(t)

    today = today_str()

    accounts_data = []
    for acc in all_accounts:
        acc_id = acc['id']
        # 按账户取桶；影子账户额外接收所有无标记的旧交易。
        # 非影子账户不展示 shadow 模拟仓（RN 只看实盘）。
        acc_trades = list(buckets.get(acc_id, []))
        if acc_id == SHADOW_ACCOUNT_ID:
            if legacy_unmarked:
                acc_trades.extend(legacy_unmarked)
        else:
            acc_trades = [t for t in acc_trades if t.get('exchange', 'shadow') != 'shadow']

        open_trades = [t for t in acc_trades if t.get('status') == 'open']
        closed_trades = [t for t in acc_trades if t.get('status') == 'closed']

        total_pnl = sum(
            t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
            for t in closed_trades
        )
        today_pnl = sum(
            t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
            for t in closed_trades
            if (t.get('closed_at') or '')[:10] == today
        )

        # 判断账户是否为实盘模式
        # 规则(v5.3+):
        #   1. 系统影子账户永远是"影子"
        #   2. 若账户已有任何真实交易所交易(历史或持仓)→ 实盘
        #      (即便现在 LIVE_MODE 关了,残留持仓仍需按实盘显示)
        #   3. 否则按"下一笔信号会不会实盘"判断:
        #      配了对应交易所凭证 + 对应 LIVE_MODE 开 + 交易开关 ON
        if acc_id == SHADOW_ACCOUNT_ID:
            is_live = False
        elif any(t.get('exchange', 'shadow') != 'shadow' for t in acc_trades):
            is_live = True
        else:
            bn_ready = acc.get('has_binance', False) and bool(getattr(config, 'LIVE_MODE', False))
            okx_ready = acc.get('has_okx', False) and bool(getattr(config, 'OKX_LIVE_MODE', False))
            is_live = acc.get('trading_enabled', True) and (bn_ready or okx_ready)

        accounts_data.append({
            'id': acc_id,
            'name': acc['name'],
            'is_active': acc_id == active_id,
            'is_system': acc_id == SHADOW_ACCOUNT_ID,
            'is_live': is_live,
            'has_binance': acc.get('has_binance', False),
            'has_okx': acc.get('has_okx', False),
            'trading_enabled': acc.get('trading_enabled', True),
            'open_count': len(open_trades),
            'closed_count': len(closed_trades),
            'total_pnl': round(total_pnl, 2),
            'today_pnl': round(today_pnl, 2),
        })

    return jsonify({
        'accounts': accounts_data,
        'active_account': active_id,
        'total_accounts': len(accounts_data),
    })


@app.route('/api/data/all')
@check_api_token
def api_data_all_accounts():
    """
    返回所有账户的合并交易数据（全局视图）。
    前端可以同时展示所有账户的持仓，不受 active_account 限制。
    """
    try:
        from runtime_config import apply_overrides as _apply_rc
        _apply_rc()
    except Exception:
        pass

    try:
        from db.compat import load_all_trades
        all_trades = load_all_trades()
    except Exception:
        all_trades = load_json(TRADES_FILE, [])
    # 不按账户过滤 - 返回全部
    open_trades = [t for t in all_trades if t.get('status') == 'open']
    closed_trades = [t for t in all_trades if t.get('status') == 'closed']

    return jsonify({
        'open_trades': open_trades,
        'closed_trades': closed_trades[-50:],
        'total_open': len(open_trades),
        'total_closed': len(closed_trades),
    })


@app.route('/api/trades/filtered')
@check_api_token
def api_trades_filtered():
    """
    Return trades filtered by mode: shadow or live.
    Query params:
      - mode: 'shadow' | 'live' | 'all' (default: 'all')
      - status: 'open' | 'closed' | 'all' (default: 'all')
    """
    mode = request.args.get('mode', 'all').lower()
    status_filter = request.args.get('status', 'all').lower()

    trades = load_json(TRADES_FILE, [])
    account_id = get_current_account_id()
    trades = filter_trades_by_account(trades, account_id)

    # Filter by mode (shadow vs live)
    if mode == 'shadow':
        trades = [t for t in trades if t.get('exchange', 'shadow') == 'shadow']
    elif mode == 'live':
        trades = [t for t in trades if t.get('exchange', 'shadow') != 'shadow']

    # Filter by status
    if status_filter == 'open':
        trades = [t for t in trades if t.get('status') == 'open']
    elif status_filter == 'closed':
        trades = [t for t in trades if t.get('status') == 'closed']

    # Compute summary
    open_trades = [t for t in trades if t.get('status') == 'open']
    closed_trades = [t for t in trades if t.get('status') == 'closed']
    total_pnl = sum(t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in closed_trades)
    total_pnl += sum(t.get('tp1_locked_pnl', 0) for t in open_trades if t.get('tp1_triggered'))

    today = today_str()
    today_closed = [t for t in closed_trades if t.get('closed_at', '').startswith(today)]
    today_pnl = sum(t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in today_closed)

    wins = sum(1 for t in closed_trades if (t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)) > 0)
    win_rate = round(wins / len(closed_trades) * 100, 1) if closed_trades else 0

    return jsonify({
        'mode': mode,
        'open': open_trades,
        'closed': closed_trades[-30:],
        'summary': {
            'total_pnl': round(total_pnl, 2),
            'today_pnl': round(today_pnl, 2),
            'open_count': len(open_trades),
            'closed_count': len(closed_trades),
            'win_rate': win_rate,
        }
    })


@app.route('/api/metrics/execution')
@check_api_token
def api_metrics_execution():
    """Return execution/reconcile metrics derived from execution events."""
    account_id = request.args.get('account_id', '').strip() or None
    try:
        minutes = int(request.args.get('minutes', '0').strip() or 0)
    except Exception:
        minutes = 0
    if minutes < 0:
        minutes = 0
    if minutes > 7 * 24 * 60:
        minutes = 7 * 24 * 60
    data = _build_execution_metrics(account_id, minutes=minutes)
    return _make_etag_response(data)


@app.route('/api/pnl/compare')
@check_api_token
def api_pnl_compare():
    """
    Return PnL comparison data: shadow vs live cumulative curves.
    Used for the comparison chart on the dashboard.
    Optional ?account_id=xxx to view a specific account.
    """
    trades = load_json(TRADES_FILE, [])
    account_id = request.args.get('account_id', '').strip() or get_current_account_id()
    trades = filter_trades_by_account(trades, account_id)

    shadow_trades = [t for t in trades if t.get('exchange', 'shadow') == 'shadow' and t.get('status') == 'closed']
    live_trades = [t for t in trades if t.get('exchange', 'shadow') != 'shadow' and t.get('status') == 'closed']

    def build_pnl_curve(trade_list):
        daily = {}
        for t in trade_list:
            closed_at = t.get('closed_at', '')
            if not closed_at:
                continue
            day = closed_at[:10]
            pnl = t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
            daily[day] = daily.get(day, 0) + pnl

        sorted_days = sorted(daily.keys())
        cum = 0
        cumulative = []
        for d in sorted_days:
            cum += daily[d]
            cumulative.append(round(cum, 2))
        return {
            'dates': sorted_days,
            'daily_pnl': [round(daily[d], 2) for d in sorted_days],
            'cumulative': cumulative,
        }

    shadow_curve = build_pnl_curve(shadow_trades)
    live_curve = build_pnl_curve(live_trades)

    # Merge dates for aligned comparison
    all_dates = sorted(set(shadow_curve['dates'] + live_curve['dates']))

    # Build aligned cumulative arrays
    def align_cumulative(curve, all_dates):
        date_cum_map = {}
        cum = 0
        for i, d in enumerate(curve['dates']):
            cum = curve['cumulative'][i]
            date_cum_map[d] = cum
        aligned = []
        last_val = 0
        for d in all_dates:
            if d in date_cum_map:
                last_val = date_cum_map[d]
            aligned.append(last_val)
        return aligned

    shadow_aligned = align_cumulative(shadow_curve, all_dates)
    live_aligned = align_cumulative(live_curve, all_dates)

    return jsonify({
        'dates': all_dates,
        'shadow': {
            'cumulative': shadow_aligned,
            'total_pnl': shadow_aligned[-1] if shadow_aligned else 0,
            'trade_count': len(shadow_trades),
        },
        'live': {
            'cumulative': live_aligned,
            'total_pnl': live_aligned[-1] if live_aligned else 0,
            'trade_count': len(live_trades),
        },
    })


@app.route('/api/backtest')
@check_api_token
def api_backtest():
    """返回最近一次单币回测结果，附带当前config参数用于对比"""
    bt_file = os.path.join(SCRIPT_DIR, 'backtest_results.json')
    data = load_json(bt_file, {})
    # 阶段 2（2026-05）：按当前活跃账号取 ALLOWED 字段（含 proportional 缩放）；
    # 非 ALLOWED 字段（DAILY_RSI_MIN / TRAIL_STOP_ACTIVATE_PCT / MAX_HOLD_DAYS）
    # 是常量直接从 config 读
    _active = get_current_account_id()
    data['current_config'] = {
        'daily_rsi_min': config.DAILY_RSI_MIN,
        'tp1_pct': round((1 - float(account_param(_active, 'TP1_MULTIPLIER', config.TP1_MULTIPLIER))) * 100, 2),
        'tp2_pct': round((1 - float(account_param(_active, 'TP2_MULTIPLIER', config.TP2_MULTIPLIER))) * 100, 2),
        'hard_stop_pct': float(account_param(_active, 'HARD_STOP_LOSS_PCT', config.HARD_STOP_LOSS_PCT)),
        'h4_rsi_drop': config.H4_RSI_DROP,
        'trail_activate_pct': config.TRAIL_STOP_ACTIVATE_PCT,
        'leverage': int(account_param(_active, 'LEVERAGE', config.LEVERAGE)),
        'stake': float(account_param(_active, 'DEFAULT_STAKE', config.DEFAULT_STAKE)),
    }
    return _make_etag_response(data)


@app.route('/api/batch-backtest')
@check_api_token
def api_batch_backtest():
    """返回批量回测结果，附带当前config参数用于对比"""
    data = load_json(BATCH_BACKTEST_RESULTS_FILE, {})
    _active = get_current_account_id()
    data['current_config'] = {
        'daily_rsi_min': config.DAILY_RSI_MIN,
        'tp1_pct': round((1 - float(account_param(_active, 'TP1_MULTIPLIER', config.TP1_MULTIPLIER))) * 100, 2),
        'tp2_pct': round((1 - float(account_param(_active, 'TP2_MULTIPLIER', config.TP2_MULTIPLIER))) * 100, 2),
        'hard_stop_pct': float(account_param(_active, 'HARD_STOP_LOSS_PCT', config.HARD_STOP_LOSS_PCT)),
        'batch_symbols': config.BATCH_BACKTEST_SYMBOLS,
        'batch_days': config.BATCH_BACKTEST_DAYS,
    }
    return _make_etag_response(data)


@app.route('/api/weekly-report')
@check_api_token
def api_weekly_report():
    """返回周报数据 (with ETag caching)"""
    data = load_json(WEEKLY_REPORT_FILE, {})
    return _make_etag_response(data)


@app.route('/api/signal-scores')
@check_api_token
def api_signal_scores():
    """返回最近交易的策略评分详情"""
    trades = load_json(TRADES_FILE, [])
    account_id = get_current_account_id()
    trades = filter_trades_by_account(trades, account_id)
    scored_trades = []
    for t in trades[-50:]:
        entry = {
            'symbol': t.get('symbol', ''),
            'direction': t.get('direction', 'SHORT'),
            'strategy': t.get('strategy', ''),
            'status': t.get('status', ''),
            'opened_at': t.get('opened_at', ''),
            'reason': t.get('reason', ''),
            'score': t.get('score', None),
            'score_grade': t.get('score_grade', None),
            'score_details': t.get('score_details', None),
            'pnl': (t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)) if t.get('status') == 'closed' else None,
        }
        scored_trades.append(entry)
    scored_trades.reverse()
    return jsonify({
        'trades': scored_trades,
        'config': {
            'score_full_threshold': config.SCORE_FULL_THRESHOLD,
            'score_half_threshold': config.SCORE_HALF_THRESHOLD,
            'signal_score_enabled': config.SIGNAL_SCORE_ENABLED,
        },
    })


# ══════════════════════════════════════════════════════════════════
#  System Status APIs (策略/信号/ML/事件总线/执行引擎)
# ══════════════════════════════════════════════════════════════════

@app.route('/system-status')
def system_status_page():
    _require_auth()
    return render_template('system_status.html')


@app.route('/api/strategies')
@check_api_token
def api_strategies():
    """返回策略引擎状态：注册的策略、启用状态、各策略独立 PnL"""
    result = {'strategies': [], 'engine_status': 'unknown'}
    try:
        from strategies.registry import StrategyRegistry
        registry = StrategyRegistry()
        for name, strategy in registry._strategies.items():
            enabled = registry._enabled.get(name, False)
            # 计算该策略的独立 PnL
            trades = load_json(TRADES_FILE, [])
            account_id = get_current_account_id()
            trades = filter_trades_by_account(trades, account_id)
            strat_trades = [t for t in trades if t.get('strategy', '') == name]
            closed = [t for t in strat_trades if t.get('status') == 'closed']
            open_trades = [t for t in strat_trades if t.get('status') == 'open']
            total_pnl = sum(t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in closed)
            wins = sum(1 for t in closed if (t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)) > 0)
            win_rate = round(wins / len(closed) * 100, 1) if closed else 0

            result['strategies'].append({
                'name': name,
                'version': getattr(strategy, 'version', '?'),
                'description': getattr(strategy, 'description', ''),
                'direction': getattr(strategy, 'direction', '?'),
                'enabled': enabled,
                'open_count': len(open_trades),
                'closed_count': len(closed),
                'total_pnl': round(total_pnl, 2),
                'win_rate': win_rate,
            })
        result['engine_status'] = 'running'
    except Exception as e:
        result['engine_status'] = f'error: {e}'
    result['timestamp'] = utcnow_iso()
    return jsonify(result)


@app.route('/api/signals')
@check_api_token
def api_signals():
    """返回市场信号系统状态：regime、sentiment、whale alerts"""
    result = {'regime': {}, 'sentiment': {}, 'whale_alerts': [], 'timestamp': utcnow_iso()}
    # Regime
    try:
        from signals.regime import get_current_regime
        state = get_current_regime()
        result['regime'] = {
            'regime': state.regime.value if hasattr(state.regime, 'value') else str(state.regime),
            'description': state.regime.description if hasattr(state.regime, 'description') else '',
            'confidence': round(state.confidence, 2),
            'short_bias': state.regime.short_bias if hasattr(state.regime, 'short_bias') else 1.0,
            'long_bias': state.regime.long_bias if hasattr(state.regime, 'long_bias') else 1.0,
            'updated_at': state.updated_at,
            'indicators': state.indicators,
            'reason': state.reason,
        }
    except Exception as e:
        result['regime'] = {'status': 'unavailable', 'error': str(e)}
    # Sentiment
    try:
        from signals.sentiment import get_sentiment_state
        sent = get_sentiment_state()
        result['sentiment'] = sent if isinstance(sent, dict) else {'status': 'unavailable'}
    except Exception as e:
        result['sentiment'] = {'status': 'unavailable', 'error': str(e)}
    # Whale alerts
    try:
        from signals.whale_alert import get_recent_alerts
        alerts = get_recent_alerts()
        result['whale_alerts'] = alerts[:20] if isinstance(alerts, list) else []
    except Exception as e:
        result['whale_alerts'] = []
    return jsonify(result)


@app.route('/api/ml')
@check_api_token
def api_ml():
    """返回 ML 模型状态：版本、最近预测统计、A/B 测试"""
    result = {'status': 'unavailable', 'model': {}, 'predictions': {}, 'ab_test': {}}
    try:
        from ml.scorer import get_scorer_status
        status = get_scorer_status()
        result.update(status if isinstance(status, dict) else {})
        result['status'] = 'active'
    except Exception as e:
        result['status'] = f'not_loaded: {e}'
    # 尝试获取最近预测分布
    try:
        from ml.scorer import get_recent_predictions
        preds = get_recent_predictions()
        result['predictions'] = preds if isinstance(preds, dict) else {}
    except Exception:
        pass
    # A/B 测试
    try:
        from ml.ab_test import get_ab_results
        result['ab_test'] = get_ab_results() or {}
    except Exception:
        pass
    result['timestamp'] = utcnow_iso()
    return jsonify(result)


@app.route('/api/event-bus')
@check_api_token
def api_event_bus():
    """返回事件总线状态：后端类型、订阅者数、最近事件统计"""
    result = {'backend': 'unknown', 'subscribers': 0, 'channels': [],
              'recent_events': [], 'stats': {}}
    try:
        from event_bus import get_event_bus
        bus = get_event_bus()
        result['backend'] = getattr(bus, '_backend_name', type(bus).__name__)
        # 获取订阅信息
        if hasattr(bus, '_backend') and hasattr(bus._backend, '_subscriptions'):
            subs = bus._backend._subscriptions
            result['subscribers'] = len(subs)
            result['channels'] = list(set(s.pattern for s in subs.values()))
        elif hasattr(bus, '_subscriptions'):
            result['subscribers'] = len(bus._subscriptions)
            result['channels'] = list(set(s.pattern for s in bus._subscriptions.values()))
        # 事件统计
        if hasattr(bus, 'get_stats'):
            result['stats'] = bus.get_stats()
    except Exception as e:
        result['backend'] = f'error: {e}'
    result['timestamp'] = utcnow_iso()
    return jsonify(result)


@app.route('/api/execution-engine')
@check_api_token
def api_execution_engine():
    """返回执行引擎状态：WS连接池、延迟、Smart Order"""
    result = {'ws_engine': {}, 'smart_order': {}, 'orderbook': {}, 'status': 'unknown'}
    # WS Order Engine
    try:
        from execution.ws_order import get_ws_engine_status
        result['ws_engine'] = get_ws_engine_status() or {}
    except Exception as e:
        result['ws_engine'] = {'status': 'unavailable', 'error': str(e)}
    # Smart Order
    try:
        from execution.smart_order import get_smart_order_status
        result['smart_order'] = get_smart_order_status() or {}
    except Exception as e:
        result['smart_order'] = {'status': 'unavailable', 'error': str(e)}
    # Orderbook Monitor
    try:
        from execution.orderbook_monitor import get_monitor_status
        result['orderbook'] = get_monitor_status() or {}
    except Exception as e:
        result['orderbook'] = {'status': 'unavailable', 'error': str(e)}
    result['status'] = 'active'
    result['timestamp'] = utcnow_iso()
    return jsonify(result)


@app.route('/api/gate-account')
@check_api_token
def api_gate_account():
    """Gate.io 账号端点 — v5.x 已废弃,保留以兼容前端 polling。

    始终返回 status='unavailable',让前端隐藏 Gate.io 卡片。
    """
    return jsonify({
        'status': 'unavailable',
        'balance': {},
        'positions': [],
        'credentials': False,
        'message': 'Gate.io 已在 v5.x 移除',
        'timestamp': utcnow_iso(),
    })


# ══════════════════════════════════════════════════════════════════
#  SocketIO Events
# ══════════════════════════════════════════════════════════════════

@socketio.on('connect')
def handle_connect():
    from flask import request as _ws_req
    token = _ws_req.args.get('token', '')
    expected_token = os.environ.get('DASHBOARD_TOKEN', '')
    if expected_token and not hmac.compare_digest(token, expected_token):
        return False
    token = request.args.get('token', '') or (request.headers.get('X-Dashboard-Token', '') if hasattr(request, 'headers') else '')
    expected_token = os.environ.get('DASHBOARD_TOKEN', '')
    if expected_token and not hmac.compare_digest(token, expected_token):
        return False
    """新连接时立即推送一次数据（仅给当前 sid，不广播全员）。

    修复 B4：之前用 socketio.emit 不带 to=，每个新连接会广播给所有现有客户端，
    多人开页面时互相打扰，前端被迫整页 re-render。
    用 flask_socketio.emit（在 socket 上下文里默认只发给当前连接）即可。
    """
    data = get_dashboard_data()
    data = _inject_live_prices(data)
    socketio_emit('update', data)  # 默认只发给 request.sid，不广播


# ══════════════════════════════════════════════════════════════════
#  Startup
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    port = 8080
    if '--port' in sys.argv:
        idx = sys.argv.index('--port')
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    # 初始化事件系统（EventBus + YAML 配置注入）
    try:
        from event_integration import init_event_system
        init_event_system('dashboard')

        # 订阅事件 → SocketIO 实时推送（减少 10s JSON 轮询依赖）
        from event_bus import get_event_bus, Event

        def _event_to_socketio(event: Event):
            """将 EventBus 事件转发为 SocketIO emit"""
            try:
                socketio.emit('event_bus', {
                    'channel': event.channel,
                    'data': event.data,
                    'timestamp': event.timestamp,
                }, namespace='/')
            except Exception:
                pass

        bus = get_event_bus()
        bus.subscribe('trade.*', _event_to_socketio, subscriber_id='dashboard_trade')
        bus.subscribe('risk.*', _event_to_socketio, subscriber_id='dashboard_risk')
        bus.subscribe('signal.*', _event_to_socketio, subscriber_id='dashboard_signal')
        bus.subscribe('market.*', _event_to_socketio, subscriber_id='dashboard_market')
    except Exception as _e:
        import logging as _dlog
        _dlog.getLogger("dashboard").warning(f"事件系统初始化失败: {_e}")

    print(f"🚀 Dashboard v4.1 启动: http://localhost:{port}")
    print("   架构: Flask + Jinja2 Templates + Modular Static Files")
    print("   页面: 主面板 | 周报 | 批量回测 | 单币回测 | 策略评分")
    _token = os.environ.get('DASHBOARD_TOKEN', '')
    if _token:
        if len(_token) < 16:
            # 安全脱敏：不打印具体长度，仅 logger 警告
            import logging as _dlog
            _dlog.getLogger("dashboard").warning(
                "DASHBOARD_TOKEN 强度不足，建议 ≥32 字节随机串："
                "python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )
        print("   API认证: 已启用（X-Dashboard-Token，常量时间比较）")
    else:
        print("   API认证: ⚠️  未设置 DASHBOARD_TOKEN（开放访问，仅限局域网）")
    _allow_insecure = os.environ.get('ALLOW_INSECURE_DASHBOARD', '0') == '1'
    _bind_host = os.environ.get('DASHBOARD_BIND', '0.0.0.0')
    _is_loopback_only = _bind_host in ('127.0.0.1', 'localhost')
    if not _token and not _allow_insecure and not _is_loopback_only:
        print('❌ 安全检查失败: 未设置 DASHBOARD_TOKEN 且绑定非本机地址。')
        print('   请设置 DASHBOARD_TOKEN，或改 DASHBOARD_BIND=127.0.0.1，')
        print('   或临时设置 ALLOW_INSECURE_DASHBOARD=1（不推荐）。')
        raise SystemExit(2)
    print("   实时推送间隔: 10秒")
    print("   按 Ctrl+C 停止")

    # 后台推送任务：用 socketio.start_background_task 而不是 threading.Thread
    # 才能跑在 eventlet 协程里，与 SocketIO 心跳协同；用真线程 + socketio.emit
    # 是 unsafe 的（会偶尔与 hub 写出竞争）。
    # M1 修复：实现已迁移到 dashboard_app.live_prices.make_background_push()
    socketio.start_background_task(
        make_background_push(socketio, get_dashboard_data)
    )

    socketio.run(app, host=_bind_host, port=port, debug=False)
