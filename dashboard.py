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

import hashlib
import hmac
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, render_template, jsonify, request, make_response
from flask_socketio import SocketIO, emit as socketio_emit

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from common import (
    TRADES_FILE, CANDIDATES_FILE, RISK_FILE,
    WEEKLY_REPORT_FILE,
    load_json, utcnow_iso, today_str, get_dynamic_balance, get_compound_stake,
    get_current_account_id, filter_trades_by_account,
)

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
_admin_url_secret = os.environ.get('ADMIN_URL_SECRET', '').strip()
if _admin_url_secret:
    if len(_admin_url_secret) < 16:
        print(f"⚠️  ADMIN_URL_SECRET 长度仅 {len(_admin_url_secret)}，强烈建议 ≥32 字节随机串")
        print("    生成: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    try:
        from admin_panel import create_blueprint as _create_admin_bp
        app.register_blueprint(_create_admin_bp(_admin_url_secret))
        print(f"🔐 Admin Panel 已挂载: /<ADMIN_URL_SECRET>/  (secret 长度={len(_admin_url_secret)})")
    except Exception as _e:
        print(f"⚠️  Admin Panel 加载失败: {_e}")
else:
    print("🔐 Admin Panel 未启用（ADMIN_URL_SECRET 未设置）")

BATCH_BACKTEST_RESULTS_FILE = os.path.join(SCRIPT_DIR, 'batch_backtest_results.json')


# ══════════════════════════════════════════════════════════════════
#  API Authentication (Optional Token-based)
# ══════════════════════════════════════════════════════════════════

def check_api_token(f):
    """
    Simple token-based auth decorator.
    Checks X-Dashboard-Token header against DASHBOARD_TOKEN env var.
    If DASHBOARD_TOKEN is not set, authentication is skipped (development mode).

    Security notes:
      - Uses hmac.compare_digest for constant-time comparison (prevents timing attacks)
      - Warns on startup if token is shorter than 16 chars
      - Recommend 32+ byte random token in production: secrets.token_urlsafe(32)
      - Requires HTTPS when exposed to public network (e.g. behind nginx/caddy)
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        expected_token = os.environ.get('DASHBOARD_TOKEN', '')
        if not expected_token:
            # No token configured, skip auth
            return f(*args, **kwargs)
        provided_token = request.headers.get('X-Dashboard-Token', '')
        # Constant-time comparison
        if not hmac.compare_digest(provided_token, expected_token):
            return jsonify({'error': 'Unauthorized', 'message': 'Invalid or missing X-Dashboard-Token'}), 401
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════════
#  Data Reading
# ══════════════════════════════════════════════════════════════════

def get_dashboard_data(account_id: str = None) -> dict:
    """汇总所有数据供前端展示（按指定账户过滤；account_id=None 时使用活跃账户）"""
    trades = load_json(TRADES_FILE, [])
    if account_id is None:
        account_id = get_current_account_id()
    trades = filter_trades_by_account(trades, account_id)
    candidates = load_json(CANDIDATES_FILE, [])
    risk_state = load_json(RISK_FILE, {})

    # 分离做空和做多（保留direction字段向后兼容）
    short_trades = [t for t in trades if t.get('direction', 'SHORT') == 'SHORT']
    long_trades = [t for t in trades if t.get('direction') == 'LONG']

    open_short = [t for t in short_trades if t.get('status') == 'open']
    closed_short = [t for t in short_trades if t.get('status') == 'closed']
    open_long = [t for t in long_trades if t.get('status') == 'open']
    closed_long = [t for t in long_trades if t.get('status') == 'closed']

    # 今日盈亏
    today = today_str()
    today_closed_short = [
        t for t in closed_short if t.get('closed_at', '').startswith(today)
    ]
    today_closed_long = [
        t for t in closed_long if t.get('closed_at', '').startswith(today)
    ]

    today_pnl_short = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in today_closed_short
    )
    today_pnl_long = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in today_closed_long
    )

    # TP1已锁定但未平仓的利润
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

    # 累计盈亏
    total_pnl_short = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in closed_short
    )
    total_pnl_long = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in closed_long
    )

    total_pnl_short += sum(
        t.get('tp1_locked_pnl', 0) for t in open_short if t.get('tp1_triggered')
    )
    total_pnl_long += sum(
        t.get('tp1_locked_pnl', 0) for t in open_long if t.get('tp1_triggered')
    )

    # 胜率
    all_closed = closed_short + closed_long
    tp1_triggered_trades = [
        t for t in open_short + open_long if t.get('tp1_triggered')
    ]
    all_for_winrate = all_closed + tp1_triggered_trades
    wins = sum(1 for t in all_for_winrate if (t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)) > 0)
    win_rate = (wins / len(all_for_winrate) * 100) if all_for_winrate else 0

    # PnL 历史
    pnl_history = {}
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

    # 动态余额
    dynamic_balance = get_dynamic_balance(account_id)
    compound_stake = get_compound_stake(account_id)

    # 持仓占用
    short_used = sum(t.get('stake_remaining', t.get('stake', 0)) for t in open_short)
    long_used = sum(t.get('stake_remaining', t.get('stake', 0)) for t in open_long)
    total_used = short_used + long_used
    max_position = dynamic_balance * config.RISK_MAX_POSITION_PCT
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

    # ── Yesterday PnL (for trend comparison) ──
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
    yesterday_pnl = pnl_history.get(yesterday, 0)

    # ── Risk History (last 7 days daily_loss) ──
    risk_history = _get_risk_history(pnl_history)

    # ── Last pause timestamp ──
    risk_last_pause = risk_state.get('last_paused_at', risk_state.get('paused_until', None))

    return {
        'account': {
            'balance': round(dynamic_balance, 2),
            'initial_balance': config.ACCOUNT_BALANCE,
            'leverage': config.LEVERAGE,
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
        'risk': risk_state,
        'pnl_chart': pnl_chart_data,
        'pool': pool_allocation,
        'config': {
            'tp1_pct': round((1 - config.TP1_MULTIPLIER) * 100, 1),
            'tp2_pct': round((1 - config.TP2_MULTIPLIER) * 100, 1),
            'hard_stop_pct': config.HARD_STOP_LOSS_PCT,
            'trail_activate_pct': config.TRAIL_STOP_ACTIVATE_PCT,
            'max_hold_days': config.MAX_HOLD_DAYS,
            'max_daily_loss': config.RISK_MAX_DAILY_LOSS,
            'max_daily_trades': config.RISK_MAX_DAILY_TRADES,
        },
        'yesterday_pnl': round(yesterday_pnl, 2),
        'risk_history': risk_history,
        'risk_last_pause': risk_last_pause,
        'account_id': account_id or '',
        'timestamp': utcnow_iso(),
    }


def _get_risk_history(pnl_history: dict) -> list:
    """Get last 7 days of daily loss values for sparkline."""
    today_dt = datetime.now(timezone.utc).date()
    history = []
    for i in range(7, 0, -1):
        day = (today_dt - timedelta(days=i)).strftime('%Y-%m-%d')
        # Negative PnL = loss
        daily = pnl_history.get(day, 0)
        # We want the loss amount (negative values mean losses)
        history.append(round(-daily if daily < 0 else 0, 2))
    return history



# ══════════════════════════════════════════════════════════════════
#  Events System
# ══════════════════════════════════════════════════════════════════

def _extract_events() -> list:
    """
    Extract events from trade files based on opened_at, closed_at timestamps.
    Returns last 50 events sorted by time (newest first).

    B8 优化：只扫最近 7 天的事件，不再每 30s 全量遍历交易历史。
    随着 trades 增长（默认归档 30 天 + 多账户）这个端点 CPU 会逐月膨胀；
    限制窗口后稳定在 O(7d × 多账户 × 多币) ≈ 几十到几百条。
    """
    EVENT_WINDOW_DAYS = 7
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=EVENT_WINDOW_DAYS)
    cutoff_iso = cutoff_dt.isoformat()  # ISO 字符串可以按字典序对比

    events = []
    trades = load_json(TRADES_FILE, [])
    account_id = get_current_account_id()
    trades = filter_trades_by_account(trades, account_id)
    risk_state = load_json(RISK_FILE, {})

    # Trade open/close events（只看 7 天内的）
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

            # Stop-loss events are critical
            if 'stop' in reason.lower() or 'hard' in reason.lower():
                level = 'critical'

            events.append({
                'time': closed_at,
                'type': 'close',
                'level': level,
                'message': f"{'✅' if pnl > 0 else '❌'} 平仓 {direction} {symbol} | {pnl:+.2f}U | {reason}",
            })

    # Risk pause events（无视窗口，只要还在 paused 状态就显示）
    if risk_state.get('paused_until'):
        events.append({
            'time': risk_state.get('last_paused_at', risk_state.get('paused_until', '')),
            'type': 'risk_pause',
            'level': 'critical',
            'message': f"🚨 风控暂停 | 暂停至 {risk_state['paused_until'][:16]}",
        })

    # Sort by time descending
    events.sort(key=lambda e: e.get('time', ''), reverse=True)
    return events[:50]


# ══════════════════════════════════════════════════════════════════
#  Background Push Thread
# ══════════════════════════════════════════════════════════════════

_live_prices = {}
_price_lock = threading.Lock()


def _fetch_live_prices(symbols: list) -> dict:
    """从 Binance 获取持仓币种的实时价格。

    重要修复（B3）：
      1. 之前用 https://api.binance.com/api/v3/ticker/price 拉**整个现货市场**（几千个币
         的 JSON，每次几 MB），即使只持仓 3 个币也是全量下载 → 5s timeout 经常被打穿。
      2. 之前查的是**现货价**，但策略做的是**永续合约**，两边价格在快速行情时会
         偏 0.1–0.3% → 前端显示的现价和实际持仓的合约市场不一致。
      3. timeout 5s 在 SSL 握手 + TCP RTT 较高时容易超时，但这个调用阻塞了
         background_push 的整个 loop，从而拖慢 SocketIO 心跳 → 前端"WiFi 图标变红"。

    现在：
      - 切到 fapi（永续合约）
      - 只拉持仓里的 symbols（用 ?symbols=[...] 参数）
      - timeout 收紧到 3s（拿不到就让前端 BinanceWS 自己直连 wss 拉，不要拖后端）
    """
    import requests as _requests
    prices = {}
    if not symbols:
        return prices

    # 把 ccxt 格式 (BTC/USDT) 转成 Binance API 格式 (BTCUSDT)
    binance_syms = [s.replace('/USDT', 'USDT').replace('/', '') for s in symbols]
    # ccxt -> binance 的反向映射，下面循环里查回 ccxt key
    rev_map = dict(zip(binance_syms, symbols))

    try:
        # fapi 的 ?symbols= 参数要 JSON-encoded array
        params = {'symbols': json.dumps(binance_syms, separators=(',', ':'))}
        r = _requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params=params,
            timeout=3,
        )
        if r.status_code == 200:
            payload = r.json()
            # fapi 返回单个 dict（symbols=1）或 list（symbols=N）。统一成 list
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                bsym = item.get('symbol')
                if bsym in rev_map:
                    try:
                        prices[rev_map[bsym]] = float(item['price'])
                    except (TypeError, ValueError):
                        continue
    except Exception as e:
        # 不打日志刷屏；fapi 偶发 5xx / 超时是常态，前端会用 wss 实时拉补齐
        pass
    return prices


def _inject_live_prices(data: dict) -> dict:
    """将实时价格注入到 dashboard 数据的持仓中"""
    with _price_lock:
        prices = _live_prices.copy()

    if not prices:
        return data

    for trade in data.get('short_trades', {}).get('open', []):
        sym = trade.get('symbol', '')
        if sym in prices:
            trade['current_price'] = prices[sym]

    for trade in data.get('long_trades', {}).get('open', []):
        sym = trade.get('symbol', '')
        if sym in prices:
            trade['current_price'] = prices[sym]

    return data


def background_push():
    """每 10 秒推送最新数据到所有连接的客户端。

    重要修复（B3）：
      - 之前用 time.sleep(10) → eventlet 不会 yield，hub 卡死，心跳延迟，前端
        SocketIO 触发 disconnect（WiFi 图标变红）。改用 socketio.sleep() 让出协程。
      - 之前用 threading.Thread 直接调 socketio.emit → 跨线程 emit 在 eventlet
        下 unsafe。改用 socketio.start_background_task 在 eventlet 协程里跑。
      - 之前每 10s 都拉 Binance 现货全市场（几 MB JSON）→ 网络抽风时一卡 5–30s
        把推送 loop 拖死。现在 _fetch_live_prices 只拉持仓 symbols 而且 3s 超时；
        即使失败前端有 wss 兜底，不影响主推送循环。
    """
    while True:
        try:
            socketio.sleep(10)  # ← 不要 time.sleep
            data = get_dashboard_data()

            # 收集所有持仓中的币种
            open_symbols = set()
            for trade in data.get('short_trades', {}).get('open', []):
                open_symbols.add(trade.get('symbol', ''))
            for trade in data.get('long_trades', {}).get('open', []):
                open_symbols.add(trade.get('symbol', ''))
            open_symbols.discard('')

            # 获取实时价格（只拉持仓里的 symbols；失败不阻塞主推送）
            if open_symbols:
                prices = _fetch_live_prices(list(open_symbols))
                if prices:
                    with _price_lock:
                        _live_prices.update(prices)

            data = _inject_live_prices(data)
            socketio.emit('update', data)
        except Exception as e:
            # 任何异常都不能让推送 loop 退出，否则前端会一直显示离线
            print(f"[Dashboard] 推送异常: {e}")



# ══════════════════════════════════════════════════════════════════
#  ETag Helper
# ══════════════════════════════════════════════════════════════════

def _make_etag_response(data):
    """Create a JSON response with ETag and Last-Modified headers for caching."""
    content = json.dumps(data, ensure_ascii=False, sort_keys=True)
    etag = hashlib.md5(content.encode()).hexdigest()

    # Check If-None-Match
    if_none_match = request.headers.get('If-None-Match', '')
    if if_none_match == etag:
        return make_response('', 304)

    resp = make_response(jsonify(data))
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'private, max-age=60'
    resp.headers['Last-Modified'] = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    return resp


# ══════════════════════════════════════════════════════════════════
#  Routes - Pages (Jinja2 templates)
# ══════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/weekly-report')
def weekly_report_page():
    return render_template('weekly_report.html')


@app.route('/batch-backtest')
def batch_backtest_page():
    return render_template('batch_backtest.html')


@app.route('/backtest')
def backtest_page():
    return render_template('backtest.html')


@app.route('/signal-scores')
def signal_scores_page():
    return render_template('signal_scores.html')


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
        # 按账户取桶；影子账户额外接收所有无标记的旧交易
        acc_trades = list(buckets.get(acc_id, []))
        if acc_id == SHADOW_ACCOUNT_ID and legacy_unmarked:
            acc_trades.extend(legacy_unmarked)

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
    data['current_config'] = {
        'daily_rsi_min': config.DAILY_RSI_MIN,
        'tp1_pct': round((1 - config.TP1_MULTIPLIER) * 100, 2),
        'tp2_pct': round((1 - config.TP2_MULTIPLIER) * 100, 2),
        'hard_stop_pct': config.HARD_STOP_LOSS_PCT,
        'h4_rsi_drop': config.H4_RSI_DROP,
        'trail_activate_pct': config.TRAIL_STOP_ACTIVATE_PCT,
        'leverage': config.LEVERAGE,
        'stake': config.DEFAULT_STAKE,
    }
    return _make_etag_response(data)


@app.route('/api/batch-backtest')
@check_api_token
def api_batch_backtest():
    """返回批量回测结果，附带当前config参数用于对比"""
    data = load_json(BATCH_BACKTEST_RESULTS_FILE, {})
    data['current_config'] = {
        'daily_rsi_min': config.DAILY_RSI_MIN,
        'tp1_pct': round((1 - config.TP1_MULTIPLIER) * 100, 2),
        'tp2_pct': round((1 - config.TP2_MULTIPLIER) * 100, 2),
        'hard_stop_pct': config.HARD_STOP_LOSS_PCT,
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
#  SocketIO Events
# ══════════════════════════════════════════════════════════════════

@socketio.on('connect')
def handle_connect():
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

    print(f"🚀 Dashboard v4.1 启动: http://localhost:{port}")
    print("   架构: Flask + Jinja2 Templates + Modular Static Files")
    print("   页面: 主面板 | 周报 | 批量回测 | 单币回测 | 策略评分")
    _token = os.environ.get('DASHBOARD_TOKEN', '')
    if _token:
        if len(_token) < 16:
            print(f"   ⚠️  DASHBOARD_TOKEN 长度仅 {len(_token)}，建议 ≥32 字节随机串")
            print("       生成: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"")
        print("   API认证: 已启用（X-Dashboard-Token，常量时间比较）")
    else:
        print("   API认证: ⚠️  未设置 DASHBOARD_TOKEN（开放访问，仅限局域网）")
    print("   实时推送间隔: 10秒")
    print("   按 Ctrl+C 停止")

    # 后台推送任务：用 socketio.start_background_task 而不是 threading.Thread
    # 才能跑在 eventlet 协程里，与 SocketIO 心跳协同；用真线程 + socketio.emit
    # 是 unsafe 的（会偶尔与 hub 写出竞争）。
    socketio.start_background_task(background_push)

    socketio.run(app, host='0.0.0.0', port=port, debug=False)
