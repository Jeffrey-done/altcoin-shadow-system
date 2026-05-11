#!/usr/bin/env python3
"""
影子做空系统实时仪表盘 v2.0
Flask + WebSocket 实现实时价格推送和状态监控。
新增：低风险策略、周报、批量回测、做多分离、资金池、策略评分

启动：python3 dashboard.py [--port 8080]
访问：http://localhost:8080
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

from flask import Flask, render_template_string, jsonify
from flask_socketio import SocketIO

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from common import (
    TRADES_FILE, CANDIDATES_FILE, FUNDING_TRADES_FILE, RISK_FILE,
    LOW_RISK_TRADES_FILE, WEEKLY_REPORT_FILE,
    load_json, utcnow_iso, today_str, get_dynamic_balance, get_compound_stake,
)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'shadow-system-dashboard'
socketio = SocketIO(app, cors_allowed_origins="*")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BATCH_BACKTEST_RESULTS_FILE = os.path.join(SCRIPT_DIR, 'batch_backtest_results.json')


# ══════════════════════════════════════════════════════════════════
#  数据读取
# ══════════════════════════════════════════════════════════════════

def get_dashboard_data() -> dict:
    """汇总所有数据供前端展示"""
    trades = load_json(TRADES_FILE, [])
    candidates = load_json(CANDIDATES_FILE, [])
    funding_trades = load_json(FUNDING_TRADES_FILE, [])
    risk_state = load_json(RISK_FILE, {})
    low_risk_trades = load_json(LOW_RISK_TRADES_FILE, [])

    # 分离做空和做多
    short_trades = [t for t in trades if t.get('direction', 'SHORT') == 'SHORT']
    long_trades = [t for t in trades if t.get('direction') == 'LONG']

    open_short = [t for t in short_trades if t.get('status') == 'open']
    closed_short = [t for t in short_trades if t.get('status') == 'closed']
    open_long = [t for t in long_trades if t.get('status') == 'open']
    closed_long = [t for t in long_trades if t.get('status') == 'closed']

    # 低风险持仓
    open_low_risk = [t for t in low_risk_trades if t.get('status') == 'open']
    closed_low_risk = [t for t in low_risk_trades if t.get('status') == 'closed']

    # 今日盈亏
    today = today_str()
    today_closed_short = [
        t for t in closed_short if t.get('closed_at', '').startswith(today)
    ]
    today_closed_long = [
        t for t in closed_long if t.get('closed_at', '').startswith(today)
    ]
    today_closed_lr = [
        t for t in closed_low_risk if t.get('closed_at', '').startswith(today)
    ]

    today_pnl_short = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in today_closed_short
    )
    today_pnl_long = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in today_closed_long
    )
    today_pnl_lr = sum(t.get('pnl', 0) for t in today_closed_lr)

    # 累计盈亏
    total_pnl_short = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in closed_short
    )
    total_pnl_long = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in closed_long
    )
    total_pnl_lr = sum(t.get('pnl', 0) for t in closed_low_risk)

    # 费率套利统计
    funding_open = [t for t in funding_trades if t.get('status') == 'open']
    funding_closed = [t for t in funding_trades if t.get('status') == 'closed']
    funding_today_pnl = sum(
        t.get('total_pnl', 0) for t in funding_closed
        if t.get('closed_at', '').startswith(today)
    )
    funding_total_pnl = sum(t.get('total_pnl', 0) for t in funding_closed)

    # 胜率（所有策略）
    all_closed = closed_short + closed_long
    wins = sum(1 for t in all_closed if (t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)) > 0)
    win_rate = (wins / len(all_closed) * 100) if all_closed else 0

    # PnL 历史
    pnl_history = {}
    for t in closed_short + closed_long:
        closed_at = t.get('closed_at', '')
        if not closed_at:
            continue
        day = closed_at[:10]
        pnl = t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        pnl_history[day] = pnl_history.get(day, 0) + pnl
    for t in funding_closed:
        closed_at = t.get('closed_at', '')
        if not closed_at:
            continue
        day = closed_at[:10]
        pnl_history[day] = pnl_history.get(day, 0) + t.get('total_pnl', 0)
    for t in closed_low_risk:
        closed_at = t.get('closed_at', '')
        if not closed_at:
            continue
        day = closed_at[:10]
        pnl_history[day] = pnl_history.get(day, 0) + t.get('pnl', 0)

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

    # 动态余额和资金池
    dynamic_balance = get_dynamic_balance()
    compound_stake = get_compound_stake()
    pool_allocation = {
        'total': round(dynamic_balance, 2),
        'short': round(dynamic_balance * config.SHORT_STRATEGY_POOL_PCT / 100, 2),
        'funding_arb': round(dynamic_balance * config.FUNDING_ARB_POOL_PCT / 100, 2),
        'low_risk': round(dynamic_balance * config.LOW_RISK_POOL_PCT / 100, 2),
        'short_pct': config.SHORT_STRATEGY_POOL_PCT,
        'funding_arb_pct': config.FUNDING_ARB_POOL_PCT,
        'low_risk_pct': config.LOW_RISK_POOL_PCT,
        'compound_stake': round(compound_stake, 2),
    }

    # 做空持仓占用
    short_used = sum(t.get('stake_remaining', t.get('stake', 0)) for t in open_short)
    long_used = sum(t.get('stake_remaining', t.get('stake', 0)) for t in open_long)
    funding_used = sum(t.get('stake', 0) for t in funding_open)
    lr_used = sum(t.get('stake', 0) for t in open_low_risk)
    pool_allocation['short_used'] = round(short_used, 2)
    pool_allocation['long_used'] = round(long_used, 2)
    pool_allocation['funding_used'] = round(funding_used, 2)
    pool_allocation['low_risk_used'] = round(lr_used, 2)

    return {
        'account': {
            'balance': round(dynamic_balance, 2),
            'initial_balance': config.ACCOUNT_BALANCE,
            'leverage': config.LEVERAGE,
            'today_pnl': round(today_pnl_short + today_pnl_long + today_pnl_lr, 2),
            'total_pnl': round(total_pnl_short + total_pnl_long + total_pnl_lr, 2),
            'win_rate': round(win_rate, 1),
            'total_trades': len(all_closed),
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
        'low_risk': {
            'open': open_low_risk,
            'closed': closed_low_risk[-20:],
            'today_pnl': round(today_pnl_lr, 2),
            'total_pnl': round(total_pnl_lr, 2),
        },
        'candidates': candidates,
        'funding': {
            'open': funding_open,
            'closed': funding_closed[-10:],
            'today_pnl': round(funding_today_pnl, 4),
            'total_pnl': round(funding_total_pnl, 4),
        },
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
        'timestamp': utcnow_iso(),
    }


# ══════════════════════════════════════════════════════════════════
#  后台推送线程
# ══════════════════════════════════════════════════════════════════

def background_push():
    """每10秒推送最新数据到所有连接的客户端"""
    while True:
        time.sleep(10)
        try:
            data = get_dashboard_data()
            socketio.emit('update', data)
        except Exception as e:
            print(f"[Dashboard] 推送异常: {e}")


# ══════════════════════════════════════════════════════════════════
#  路由
# ══════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route('/api/data')
def api_data():
    return jsonify(get_dashboard_data())


@app.route('/api/backtest')
def api_backtest():
    """返回最近一次单币回测结果"""
    bt_file = os.path.join(SCRIPT_DIR, 'backtest_results.json')
    data = load_json(bt_file, {})
    return jsonify(data)


@app.route('/api/batch-backtest')
def api_batch_backtest():
    """返回批量回测结果"""
    data = load_json(BATCH_BACKTEST_RESULTS_FILE, {})
    return jsonify(data)


@app.route('/api/weekly-report')
def api_weekly_report():
    """返回周报数据"""
    data = load_json(WEEKLY_REPORT_FILE, {})
    return jsonify(data)


@app.route('/api/low-risk')
def api_low_risk():
    """返回低风险策略数据"""
    trades = load_json(LOW_RISK_TRADES_FILE, [])
    open_trades = [t for t in trades if t.get('status') == 'open']
    closed_trades = [t for t in trades if t.get('status') == 'closed']
    today = today_str()
    today_closed = [t for t in closed_trades if t.get('closed_at', '').startswith(today)]
    daily_pnl = sum(t.get('pnl', 0) for t in today_closed)
    total_pnl = sum(t.get('pnl', 0) for t in closed_trades)
    # 按策略统计
    strategy_stats = {}
    for t in closed_trades:
        strat = t.get('strategy', 'unknown')
        if strat not in strategy_stats:
            strategy_stats[strat] = {'count': 0, 'pnl': 0, 'wins': 0}
        strategy_stats[strat]['count'] += 1
        strategy_stats[strat]['pnl'] += t.get('pnl', 0)
        if t.get('pnl', 0) > 0:
            strategy_stats[strat]['wins'] += 1
    return jsonify({
        'open': open_trades,
        'closed': closed_trades[-30:],
        'today_pnl': round(daily_pnl, 4),
        'total_pnl': round(total_pnl, 4),
        'strategy_stats': strategy_stats,
        'config': {
            'daily_target': config.ACCOUNT_BALANCE * config.LOW_RISK_DAILY_TARGET_PCT / 100,
            'max_positions': config.LOW_RISK_MAX_POSITIONS,
            'symbols': config.LOW_RISK_SYMBOLS,
        },
    })


@app.route('/api/signal-scores')
def api_signal_scores():
    """返回最近交易的策略评分详情"""
    trades = load_json(TRADES_FILE, [])
    # 收集有评分信息的交易
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


@app.route('/backtest')
def backtest_page():
    return render_template_string(BACKTEST_HTML)


@app.route('/batch-backtest')
def batch_backtest_page():
    return render_template_string(BATCH_BACKTEST_HTML)


@app.route('/weekly-report')
def weekly_report_page():
    return render_template_string(WEEKLY_REPORT_HTML)


@app.route('/low-risk')
def low_risk_page():
    return render_template_string(LOW_RISK_HTML)


@app.route('/signal-scores')
def signal_scores_page():
    return render_template_string(SIGNAL_SCORES_HTML)


@socketio.on('connect')
def handle_connect():
    """新连接时立即推送一次数据"""
    data = get_dashboard_data()
    socketio.emit('update', data)


# ══════════════════════════════════════════════════════════════════
#  公共样式和导航
# ══════════════════════════════════════════════════════════════════

COMMON_STYLES = '''
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    min-height: 100vh;
    padding: 16px;
}
.container { max-width: 1400px; margin: 0 auto; }
h1 { font-size: 1.5rem; color: #58a6ff; margin-bottom: 4px; }
.subtitle { color: #8b949e; font-size: 0.85rem; margin-bottom: 16px; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }

/* Nav */
.nav { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.nav a {
    padding: 6px 12px;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    font-size: 0.8rem;
    color: #c9d1d9;
    transition: all 0.2s;
}
.nav a:hover, .nav a.active {
    background: #1f6feb;
    border-color: #1f6feb;
    color: #fff;
    text-decoration: none;
}

/* Grid */
.grid { display: grid; gap: 12px; margin-bottom: 12px; }
.grid-4 { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.grid-3 { grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); }
.grid-2 { grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); }
.grid-5 { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }

/* Cards */
.card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 16px;
}
.card-header {
    font-size: 0.8rem;
    color: #8b949e;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 8px;
}
.card-value { font-size: 1.8rem; font-weight: 700; }
.card-sub { font-size: 0.75rem; color: #8b949e; margin-top: 4px; }

/* Colors */
.green { color: #3fb950; }
.red { color: #f85149; }
.yellow { color: #d29922; }
.blue { color: #58a6ff; }
.purple { color: #a371f7; }

/* Table */
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th { text-align: left; padding: 8px 6px; border-bottom: 1px solid #30363d; color: #8b949e; font-weight: 500; }
td { padding: 8px 6px; border-bottom: 1px solid #21262d; }
tr:hover td { background: #1c2128; }

/* Status badge */
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.7rem; font-weight: 600; }
.badge-open { background: #1f3d2b; color: #3fb950; }
.badge-closed { background: #3d1f1f; color: #f85149; }
.badge-ok { background: #1f3d2b; color: #3fb950; }
.badge-warn { background: #3d2f1f; color: #d29922; }
.badge-stop { background: #3d1f1f; color: #f85149; }
.badge-a { background: #1f3d2b; color: #3fb950; }
.badge-b { background: #2d3a1f; color: #d29922; }
.badge-skip { background: #3d1f1f; color: #f85149; }

/* Risk bar */
.risk-bar { height: 6px; background: #21262d; border-radius: 3px; margin-top: 6px; overflow: hidden; }
.risk-bar-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease; }

/* Progress bar */
.progress-bar { height: 20px; background: #21262d; border-radius: 4px; overflow: hidden; position: relative; margin: 8px 0; }
.progress-bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }
.progress-bar-label { position: absolute; top: 2px; left: 50%; transform: translateX(-50%); font-size: 0.7rem; color: #fff; }

/* Pool bar */
.pool-bar { display: flex; height: 24px; border-radius: 4px; overflow: hidden; margin: 8px 0; }
.pool-segment { display: flex; align-items: center; justify-content: center; font-size: 0.7rem; font-weight: 600; color: #fff; }

/* Live indicator */
.live-dot { display: inline-block; width: 8px; height: 8px; background: #3fb950; border-radius: 50%; margin-right: 6px; animation: pulse 2s infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

/* Section title */
.section-title { font-size: 1rem; font-weight: 600; margin: 16px 0 8px; color: #c9d1d9; }

/* Empty state */
.empty-state { text-align: center; padding: 40px; color: #8b949e; }

/* Responsive */
@media (max-width: 768px) {
    .grid-4 { grid-template-columns: repeat(2, 1fr); }
    .grid-3 { grid-template-columns: repeat(2, 1fr); }
    .grid-2 { grid-template-columns: 1fr; }
    .card-value { font-size: 1.4rem; }
}
'''

NAV_HTML = '''
<div class="nav">
    <a href="/" id="nav-home">🏠 主面板</a>
    <a href="/low-risk" id="nav-lowrisk">📊 低风险策略</a>
    <a href="/weekly-report" id="nav-weekly">📋 周报</a>
    <a href="/batch-backtest" id="nav-batch">🔬 批量回测</a>
    <a href="/backtest" id="nav-bt">📈 单币回测</a>
    <a href="/signal-scores" id="nav-scores">🎯 策略评分</a>
</div>
'''



# ══════════════════════════════════════════════════════════════════
#  主面板 HTML
# ══════════════════════════════════════════════════════════════════

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shadow Trading Dashboard</title>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<style>''' + COMMON_STYLES + '''</style>
</head>
<body>
<div class="container">
    <h1><span class="live-dot"></span>Shadow Trading System</h1>
    <div class="subtitle"><span id="timestamp">连接中...</span></div>
    ''' + NAV_HTML + '''

    <!-- Summary Cards -->
    <div class="grid grid-4">
        <div class="card">
            <div class="card-header">今日盈亏</div>
            <div class="card-value" id="today-pnl">--</div>
            <div class="card-sub">全策略合计</div>
        </div>
        <div class="card">
            <div class="card-header">累计盈亏</div>
            <div class="card-value" id="total-pnl">--</div>
            <div class="card-sub">总交易 <span id="total-trades">0</span> 单</div>
        </div>
        <div class="card">
            <div class="card-header">动态余额</div>
            <div class="card-value" id="balance">--</div>
            <div class="card-sub" id="leverage-info">--</div>
        </div>
        <div class="card">
            <div class="card-header">胜率</div>
            <div class="card-value" id="win-rate">--</div>
            <div class="card-sub">盈利单/总单</div>
        </div>
    </div>

    <!-- 资金池分配 -->
    <div class="card" style="margin-bottom:12px;">
        <div class="section-title">💰 资金池分配</div>
        <div class="pool-bar" id="pool-bar"></div>
        <div class="grid grid-4" id="pool-details" style="margin-top:8px;"></div>
    </div>

    <!-- 做空持仓 + 做多持仓 -->
    <div class="grid grid-2">
        <div class="card">
            <div class="section-title">🔴 做空持仓 <span class="card-sub" id="short-pnl-info"></span></div>
            <table>
                <thead><tr><th>币种</th><th>入场</th><th>现价</th><th>盈亏%</th><th>盈亏U</th><th>状态</th></tr></thead>
                <tbody id="short-trades"></tbody>
            </table>
            <div class="card-sub" id="short-empty" style="padding:12px;text-align:center;display:none;">暂无做空持仓</div>
        </div>
        <div class="card">
            <div class="section-title">🟢 做多持仓 <span class="card-sub" id="long-pnl-info"></span></div>
            <table>
                <thead><tr><th>币种</th><th>入场</th><th>现价</th><th>盈亏%</th><th>盈亏U</th><th>策略</th></tr></thead>
                <tbody id="long-trades"></tbody>
            </table>
            <div class="card-sub" id="long-empty" style="padding:12px;text-align:center;display:none;">暂无做多持仓</div>
        </div>
    </div>

    <div class="grid grid-2">
        <!-- Risk Control -->
        <div class="card">
            <div class="section-title">🛡️ 风控状态</div>
            <div id="risk-content"></div>
        </div>
        <!-- Funding Arbitrage -->
        <div class="card">
            <div class="section-title">💰 费率套利</div>
            <div style="margin-bottom:8px;">
                <span class="card-sub">今日：</span><span id="funding-today" class="green">--</span>
                <span class="card-sub" style="margin-left:12px;">累计：</span><span id="funding-total" class="green">--</span>
            </div>
            <table>
                <thead><tr><th>币种</th><th>费率</th><th>方向PnL</th><th>费率收入</th><th>总计</th></tr></thead>
                <tbody id="funding-trades"></tbody>
            </table>
        </div>
    </div>

    <div class="grid grid-2">
        <!-- Low Risk Summary -->
        <div class="card">
            <div class="section-title">📊 低风险策略 <a href="/low-risk" style="font-size:0.75rem;margin-left:8px;">详情→</a></div>
            <div style="margin-bottom:8px;">
                <span class="card-sub">今日：</span><span id="lr-today" class="green">--</span>
                <span class="card-sub" style="margin-left:12px;">累计：</span><span id="lr-total" class="green">--</span>
                <span class="card-sub" style="margin-left:12px;">持仓：</span><span id="lr-open-count">0</span>
            </div>
            <table>
                <thead><tr><th>币种</th><th>策略</th><th>方向</th><th>入场</th><th>浮盈</th></tr></thead>
                <tbody id="lr-trades"></tbody>
            </table>
            <div class="card-sub" id="lr-empty" style="padding:12px;text-align:center;display:none;">暂无低风险持仓</div>
        </div>
        <!-- Candidates -->
        <div class="card">
            <div class="section-title">📋 候选池</div>
            <table>
                <thead><tr><th>币种</th><th>RSI(1D)</th><th>24h%</th><th>OI变化</th><th>妖币分</th><th>状态</th></tr></thead>
                <tbody id="candidates"></tbody>
            </table>
            <div class="card-sub" id="cand-empty" style="padding:12px;text-align:center;display:none;">候选池为空</div>
        </div>
    </div>

    <!-- PnL Chart -->
    <div class="card" style="margin-top:12px;">
        <div class="section-title">📈 历史盈亏曲线</div>
        <div style="height:220px;position:relative;"><canvas id="pnl-chart"></canvas></div>
        <div style="margin-top:8px;display:flex;gap:16px;font-size:0.75rem;color:#8b949e;">
            <span>🟢 累计盈亏</span><span>🔵 每日盈亏</span>
        </div>
    </div>

    <!-- Closed Trades -->
    <div class="card" style="margin-top:12px;">
        <div class="section-title">✅ 最近平仓记录</div>
        <table>
            <thead><tr><th>币种</th><th>方向</th><th>入场</th><th>平仓价</th><th>盈亏</th><th>原因</th><th>时间</th></tr></thead>
            <tbody id="closed-trades"></tbody>
        </table>
    </div>
</div>

<script>
const socket = io();
function pnlColor(val) { return val > 0 ? 'green' : val < 0 ? 'red' : ''; }
function fmtPnl(val, suffix='U') {
    const cls = pnlColor(val);
    const sign = val >= 0 ? '+' : '';
    return `<span class="${cls}">${sign}${val.toFixed(2)}${suffix}</span>`;
}
function fmtPct(val) {
    const cls = pnlColor(val);
    const sign = val >= 0 ? '+' : '';
    return `<span class="${cls}">${sign}${val.toFixed(1)}%</span>`;
}

function renderPositionTable(trades, tbodyId, emptyId, direction) {
    const tbody = document.getElementById(tbodyId);
    const empty = document.getElementById(emptyId);
    if (!trades || trades.length === 0) {
        tbody.innerHTML = '';
        empty.style.display = 'block';
        return;
    }
    empty.style.display = 'none';
    tbody.innerHTML = trades.map(t => {
        const entry = t.entry_price || 0;
        const cur = t.current_price || entry;
        let pnlPct;
        if (direction === 'SHORT') {
            pnlPct = entry > 0 ? ((entry - cur) / entry * 100) : 0;
        } else {
            pnlPct = entry > 0 ? ((cur - entry) / entry * 100) : 0;
        }
        const leverage = t.leverage || 10;
        const pnlU = (t.stake_remaining || t.stake || 100) * leverage * pnlPct / 100;
        const extra = direction === 'SHORT'
            ? (t.tp1_triggered ? '<span class="badge badge-ok">TP1✓</span>' : '')
            : (t.strategy || '');
        return `<tr>
            <td><b>${t.symbol}</b></td>
            <td>${entry.toFixed(6)}</td>
            <td>${cur.toFixed(6)}</td>
            <td>${fmtPct(pnlPct)}</td>
            <td>${fmtPnl(pnlU)}</td>
            <td>${extra}</td>
        </tr>`;
    }).join('');
}

function updateDashboard(data) {
    // Timestamp
    const ts = data.timestamp ? data.timestamp.slice(0, 19).replace('T', ' ') + ' UTC' : '--';
    document.getElementById('timestamp').textContent = `最后更新: ${ts}`;

    // Account summary
    const a = data.account;
    const todayTotal = a.today_pnl + (data.funding?.today_pnl || 0);
    document.getElementById('today-pnl').innerHTML = fmtPnl(todayTotal);
    document.getElementById('today-pnl').className = 'card-value ' + pnlColor(todayTotal);

    const totalAll = a.total_pnl + (data.funding?.total_pnl || 0);
    document.getElementById('total-pnl').innerHTML = fmtPnl(totalAll);
    document.getElementById('total-pnl').className = 'card-value ' + pnlColor(totalAll);

    document.getElementById('balance').innerHTML = `${a.balance}U`;
    document.getElementById('balance').className = 'card-value ' + (a.balance >= a.initial_balance ? 'green' : 'red');
    document.getElementById('total-trades').textContent = a.total_trades;
    document.getElementById('leverage-info').textContent = `初始${a.initial_balance}U | 复利仓位${data.pool?.compound_stake || '--'}U`;
    document.getElementById('win-rate').textContent = a.win_rate + '%';
    document.getElementById('win-rate').className = 'card-value ' + (a.win_rate >= 50 ? 'green' : 'yellow');

    // Pool allocation
    const pool = data.pool || {};
    document.getElementById('pool-bar').innerHTML = `
        <div class="pool-segment" style="width:${pool.short_pct||60}%;background:#f85149;">做空${pool.short_pct||60}%</div>
        <div class="pool-segment" style="width:${pool.funding_arb_pct||20}%;background:#d29922;">费率${pool.funding_arb_pct||20}%</div>
        <div class="pool-segment" style="width:${pool.low_risk_pct||20}%;background:#3fb950;">低风险${pool.low_risk_pct||20}%</div>
    `;
    document.getElementById('pool-details').innerHTML = `
        <div><span class="card-sub">做空池：</span><b>${pool.short||0}U</b> <span class="card-sub">占用 ${pool.short_used||0}U</span></div>
        <div><span class="card-sub">费率池：</span><b>${pool.funding_arb||0}U</b> <span class="card-sub">占用 ${pool.funding_used||0}U</span></div>
        <div><span class="card-sub">低风险池：</span><b>${pool.low_risk||0}U</b> <span class="card-sub">占用 ${pool.low_risk_used||0}U</span></div>
        <div><span class="card-sub">做多占用：</span><b>${pool.long_used||0}U</b></div>
    `;

    // Short positions
    const st = data.short_trades || {};
    document.getElementById('short-pnl-info').innerHTML = `今日 ${fmtPnl(st.today_pnl||0)} | 累计 ${fmtPnl(st.total_pnl||0)}`;
    renderPositionTable(st.open, 'short-trades', 'short-empty', 'SHORT');

    // Long positions
    const lt = data.long_trades || {};
    document.getElementById('long-pnl-info').innerHTML = `今日 ${fmtPnl(lt.today_pnl||0)} | 累计 ${fmtPnl(lt.total_pnl||0)}`;
    renderPositionTable(lt.open, 'long-trades', 'long-empty', 'LONG');

    // Low risk summary
    const lr = data.low_risk || {};
    document.getElementById('lr-today').innerHTML = fmtPnl(lr.today_pnl || 0);
    document.getElementById('lr-total').innerHTML = fmtPnl(lr.total_pnl || 0);
    document.getElementById('lr-open-count').textContent = (lr.open||[]).length;
    const lrTbody = document.getElementById('lr-trades');
    const lrEmpty = document.getElementById('lr-empty');
    if (!lr.open || lr.open.length === 0) {
        lrTbody.innerHTML = '';
        lrEmpty.style.display = 'block';
    } else {
        lrEmpty.style.display = 'none';
        lrTbody.innerHTML = lr.open.map(t => `<tr>
            <td><b>${t.symbol}</b></td>
            <td>${t.strategy}</td>
            <td>${t.direction}</td>
            <td>${(t.entry_price||0).toFixed(6)}</td>
            <td>${fmtPnl(t.pnl||0)}</td>
        </tr>`).join('');
    }

    // Risk control
    const risk = data.risk || {};
    const cfg = data.config || {};
    const dailyLoss = risk.daily_loss || 0;
    const dailyMax = cfg.max_daily_loss || 30;
    const dailyPct = Math.min(dailyLoss / dailyMax * 100, 100);
    const trades_opened = risk.daily_trades_opened || 0;
    const maxTrades = cfg.max_daily_trades || 2;
    const consLoss = risk.consecutive_losses || 0;
    const paused = risk.paused_until ? true : false;
    let statusBadge = '<span class="badge badge-ok">正常</span>';
    if (paused) statusBadge = '<span class="badge badge-stop">暂停中</span>';
    else if (dailyLoss >= dailyMax) statusBadge = '<span class="badge badge-stop">今日停止</span>';
    else if (dailyLoss >= dailyMax * 0.7) statusBadge = '<span class="badge badge-warn">接近限额</span>';
    const barColor = dailyPct > 80 ? '#f85149' : dailyPct > 50 ? '#d29922' : '#3fb950';
    document.getElementById('risk-content').innerHTML = `
        <div style="margin-bottom:12px;">状态：${statusBadge}</div>
        <div style="margin-bottom:8px;"><span>今日亏损：${dailyLoss.toFixed(1)} / ${dailyMax}U</span>
            <div class="risk-bar"><div class="risk-bar-fill" style="width:${dailyPct}%;background:${barColor}"></div></div></div>
        <div style="margin-bottom:8px;">今日开仓：${trades_opened} / ${maxTrades} 次</div>
        <div style="margin-bottom:8px;">连续亏损：${consLoss} / 3 次</div>
        <div style="margin-bottom:8px;">持仓占用：${(risk.total_open_stake||0).toFixed(0)}U</div>
        ${paused ? '<div class="red" style="margin-top:8px;">暂停至：' + (risk.paused_until||'').slice(0,16) + ' UTC</div>' : ''}`;

    // Funding
    document.getElementById('funding-today').innerHTML = fmtPnl(data.funding?.today_pnl || 0);
    document.getElementById('funding-total').innerHTML = fmtPnl(data.funding?.total_pnl || 0);
    const fundingTbody = document.getElementById('funding-trades');
    const allFunding = [...(data.funding?.open || []), ...(data.funding?.closed || [])].slice(-8);
    fundingTbody.innerHTML = allFunding.map(t => {
        const badge = t.status === 'open' ? '<span class="badge badge-open">持仓</span>' : '<span class="badge badge-closed">已平</span>';
        return `<tr><td>${t.symbol} ${badge}</td><td>${(t.funding_rate||0).toFixed(4)}%</td>
            <td>${fmtPnl(t.pnl||0)}</td><td>${fmtPnl(t.funding_income||0)}</td><td><b>${fmtPnl(t.total_pnl||0)}</b></td></tr>`;
    }).join('');

    // Candidates
    const candTbody = document.getElementById('candidates');
    const candEmpty = document.getElementById('cand-empty');
    if (!data.candidates || data.candidates.length === 0) {
        candTbody.innerHTML = ''; candEmpty.style.display = 'block';
    } else {
        candEmpty.style.display = 'none';
        candTbody.innerHTML = data.candidates.slice(0, 15).map(c => {
            const badge = c.triggered ? '<span class="badge badge-closed">已触发</span>' : '<span class="badge badge-open">等待中</span>';
            const yao = c.yao_score >= 2 ? '🔥' : (c.yao_score === 1 ? '⚡' : '📌');
            return `<tr><td>${c.symbol}</td><td><b>${c.rsi_1d}</b></td><td>${fmtPct(c.pct24h)}</td>
                <td>${(c.oi_change||0).toFixed(0)}%</td><td>${yao} ${c.yao_score}/3</td><td>${badge}</td></tr>`;
        }).join('');
    }

    // Closed trades (combined short + long)
    const closedTbody = document.getElementById('closed-trades');
    const allClosed = [...(st.closed||[]), ...(lt.closed||[])].sort((a,b) => (b.closed_at||'').localeCompare(a.closed_at||'')).slice(0,15);
    closedTbody.innerHTML = allClosed.map(t => {
        const pnl = (t.tp1_locked_pnl || 0) + (t.pnl || 0);
        const closedAt = (t.closed_at || '').slice(0, 16).replace('T', ' ');
        return `<tr><td><b>${t.symbol}</b></td><td>${t.direction||'SHORT'}</td><td>${(t.entry_price||0).toFixed(6)}</td>
            <td>${(t.current_price||0).toFixed(6)}</td><td><b>${fmtPnl(pnl)}</b></td><td>${t.close_reason || '--'}</td><td>${closedAt}</td></tr>`;
    }).join('');
}

socket.on('update', function(data) { updateDashboard(data); drawPnlChart(data.pnl_chart); });
socket.on('connect', () => { document.getElementById('timestamp').textContent = '已连接，等待数据...'; });
socket.on('disconnect', () => { document.getElementById('timestamp').textContent = '⚠️ 连接断开，重连中...'; });

function drawPnlChart(chartData) {
    if (!chartData || !chartData.dates || chartData.dates.length < 2) return;
    const canvas = document.getElementById('pnl-chart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const w = canvas.parentElement.clientWidth;
    const h = 220;
    canvas.width = w; canvas.height = h;
    const dates = chartData.dates, daily = chartData.daily_pnl, cum = chartData.cumulative, n = dates.length;
    const padding = { top: 20, right: 20, bottom: 30, left: 50 };
    const chartW = w - padding.left - padding.right, chartH = h - padding.top - padding.bottom;
    ctx.fillStyle = '#161b22'; ctx.fillRect(0, 0, w, h);
    const allVals = [...cum, ...daily];
    const minVal = Math.min(...allVals, 0), maxVal = Math.max(...allVals, 0);
    const range = (maxVal - minVal) || 1;
    const zeroY = padding.top + chartH - ((0 - minVal) / range) * chartH;
    ctx.strokeStyle = '#30363d'; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(padding.left, zeroY); ctx.lineTo(w - padding.right, zeroY); ctx.stroke(); ctx.setLineDash([]);
    const barWidth = Math.max(2, (chartW / n) * 0.6);
    for (let i = 0; i < n; i++) {
        const x = padding.left + (i / (n - 1)) * chartW;
        const val = daily[i];
        const barH = Math.abs(val / range) * chartH;
        const y = val >= 0 ? zeroY - barH : zeroY;
        ctx.fillStyle = val >= 0 ? 'rgba(63,185,80,0.4)' : 'rgba(248,81,73,0.4)';
        ctx.fillRect(x - barWidth/2, y, barWidth, barH);
    }
    ctx.strokeStyle = '#3fb950'; ctx.lineWidth = 2; ctx.beginPath();
    for (let i = 0; i < n; i++) {
        const x = padding.left + (i / (n - 1)) * chartW;
        const y = padding.top + chartH - ((cum[i] - minVal) / range) * chartH;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.fillStyle = '#8b949e'; ctx.font = '10px sans-serif';
    ctx.fillText(maxVal.toFixed(0) + 'U', 4, padding.top + 10);
    ctx.fillText(minVal.toFixed(0) + 'U', 4, h - padding.bottom - 4);
    if (dates.length > 0) { ctx.fillText(dates[0], padding.left, h - 6); ctx.fillText(dates[dates.length-1], w - padding.right - 60, h - 6); }
    const finalCum = cum[cum.length - 1];
    ctx.fillStyle = finalCum >= 0 ? '#3fb950' : '#f85149'; ctx.font = 'bold 12px sans-serif';
    ctx.fillText(`${finalCum >= 0 ? '+' : ''}${finalCum.toFixed(1)}U`, w - padding.right - 55, padding.top + 12);
}
document.getElementById('nav-home').classList.add('active');
</script>
</body>
</html>'''



# ══════════════════════════════════════════════════════════════════
#  低风险策略页面 HTML
# ══════════════════════════════════════════════════════════════════

LOW_RISK_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>低风险策略 - Shadow Trading</title>
<style>''' + COMMON_STYLES + '''</style>
</head>
<body>
<div class="container">
    <h1>📊 低风险日收策略</h1>
    <div class="subtitle">网格交易 | 均值回归 | 多币费率收割</div>
    ''' + NAV_HTML + '''
    <div id="content"><div class="empty-state">加载中...</div></div>
</div>
<script>
function pnlColor(val) { return val > 0 ? 'green' : val < 0 ? 'red' : ''; }
function fmtPnl(val) { const cls = pnlColor(val); return `<span class="${cls}">${val >= 0 ? '+' : ''}${val.toFixed(4)}U</span>`; }

fetch('/api/low-risk').then(r => r.json()).then(data => {
    const content = document.getElementById('content');
    const target = data.config?.daily_target || 2;
    const progress = target > 0 ? Math.min((data.today_pnl / target) * 100, 100) : 0;
    const barColor = progress >= 100 ? '#3fb950' : progress >= 50 ? '#d29922' : '#58a6ff';

    let html = `
    <div class="grid grid-4">
        <div class="card">
            <div class="card-header">今日盈亏</div>
            <div class="card-value ${pnlColor(data.today_pnl)}">${data.today_pnl >= 0 ? '+' : ''}${data.today_pnl.toFixed(4)}U</div>
            <div class="card-sub">目标: ${target.toFixed(2)}U</div>
        </div>
        <div class="card">
            <div class="card-header">累计盈亏</div>
            <div class="card-value ${pnlColor(data.total_pnl)}">${data.total_pnl >= 0 ? '+' : ''}${data.total_pnl.toFixed(4)}U</div>
        </div>
        <div class="card">
            <div class="card-header">当前持仓</div>
            <div class="card-value blue">${(data.open||[]).length}</div>
            <div class="card-sub">上限: ${data.config?.max_positions || 5}</div>
        </div>
        <div class="card">
            <div class="card-header">今日进度</div>
            <div class="progress-bar"><div class="progress-bar-fill" style="width:${Math.max(0,progress)}%;background:${barColor}"></div>
                <span class="progress-bar-label">${progress.toFixed(1)}%</span></div>
        </div>
    </div>`;

    // 策略统计
    if (data.strategy_stats && Object.keys(data.strategy_stats).length > 0) {
        html += `<div class="card" style="margin-bottom:12px;"><div class="section-title">📊 策略统计</div><table>
            <thead><tr><th>策略</th><th>交易数</th><th>盈亏</th><th>胜率</th></tr></thead><tbody>`;
        for (const [strat, s] of Object.entries(data.strategy_stats)) {
            const wr = s.count > 0 ? (s.wins / s.count * 100).toFixed(0) : 0;
            html += `<tr><td><b>${strat}</b></td><td>${s.count}</td><td>${fmtPnl(s.pnl)}</td><td>${wr}%</td></tr>`;
        }
        html += `</tbody></table></div>`;
    }

    // 持仓中
    html += `<div class="card" style="margin-bottom:12px;"><div class="section-title">🔄 持仓中 (${(data.open||[]).length})</div>`;
    if (data.open && data.open.length > 0) {
        html += `<table><thead><tr><th>币种</th><th>策略</th><th>方向</th><th>入场</th><th>目标</th><th>止损</th><th>浮盈</th></tr></thead><tbody>`;
        data.open.forEach(t => {
            html += `<tr><td><b>${t.symbol}</b></td><td>${t.strategy}</td><td>${t.direction}</td>
                <td>${(t.entry_price||0).toFixed(6)}</td><td>${(t.target_price||0).toFixed(6)}</td>
                <td>${(t.stop_price||0).toFixed(6)}</td><td>${fmtPnl(t.pnl||0)}</td></tr>`;
        });
        html += `</tbody></table>`;
    } else {
        html += `<div class="card-sub" style="padding:12px;text-align:center;">暂无持仓</div>`;
    }
    html += `</div>`;

    // 最近平仓
    html += `<div class="card"><div class="section-title">✅ 最近平仓</div><table>
        <thead><tr><th>币种</th><th>策略</th><th>方向</th><th>盈亏</th><th>原因</th><th>时间</th></tr></thead><tbody>`;
    (data.closed||[]).slice().reverse().slice(0,20).forEach(t => {
        const closedAt = (t.closed_at||'').slice(0,16).replace('T',' ');
        html += `<tr><td><b>${t.symbol}</b></td><td>${t.strategy}</td><td>${t.direction}</td>
            <td><b>${fmtPnl(t.pnl||0)}</b></td><td>${t.close_reason||'--'}</td><td>${closedAt}</td></tr>`;
    });
    html += `</tbody></table></div>`;

    content.innerHTML = html;
}).catch(e => { document.getElementById('content').innerHTML = `<div class="empty-state">加载失败: ${e.message}</div>`; });
document.getElementById('nav-lowrisk').classList.add('active');
</script>
</body>
</html>'''



# ══════════════════════════════════════════════════════════════════
#  周报页面 HTML
# ══════════════════════════════════════════════════════════════════

WEEKLY_REPORT_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>策略周报 - Shadow Trading</title>
<style>''' + COMMON_STYLES + '''</style>
</head>
<body>
<div class="container">
    <h1>📋 策略周报</h1>
    <div class="subtitle">自动生成的周度策略表现报告</div>
    ''' + NAV_HTML + '''
    <div id="content"><div class="empty-state">加载中...</div></div>
</div>
<script>
function pnlColor(val) { return val > 0 ? 'green' : val < 0 ? 'red' : ''; }
function fmtPnl(val) { const cls = pnlColor(val); return `<span class="${cls}">${val >= 0 ? '+' : ''}${val.toFixed(2)}U</span>`; }

fetch('/api/weekly-report').then(r => r.json()).then(data => {
    const content = document.getElementById('content');
    if (!data || !data.stats) {
        content.innerHTML = `<div class="empty-state"><p>暂无周报数据</p><p style="margin-top:8px;font-size:0.8rem;">运行: <code>python3 weekly_report.py</code></p></div>`;
        return;
    }
    const meta = data.metadata || {};
    const stats = data.stats;
    const suggestions = data.suggestions || [];
    const gradeEmoji = {'A':'🏆','B':'✅','C':'⚠️','F':'❌'}[meta.grade] || '❓';

    let html = `
    <div class="grid grid-5">
        <div class="card">
            <div class="card-header">评级</div>
            <div class="card-value">${gradeEmoji} ${meta.grade||'-'}</div>
            <div class="card-sub">ROI: ${(meta.roi_pct||0).toFixed(1)}%</div>
        </div>
        <div class="card">
            <div class="card-header">总盈亏</div>
            <div class="card-value ${pnlColor(stats.total_pnl)}">${fmtPnl(stats.total_pnl||0)}</div>
        </div>
        <div class="card">
            <div class="card-header">交易数</div>
            <div class="card-value blue">${stats.total_trades||0}</div>
            <div class="card-sub">${stats.win_count||0}胜 / ${stats.loss_count||0}负</div>
        </div>
        <div class="card">
            <div class="card-header">胜率</div>
            <div class="card-value ${(stats.win_rate||0) >= 50 ? 'green' : 'yellow'}">${stats.win_rate||0}%</div>
        </div>
        <div class="card">
            <div class="card-header">平均持仓</div>
            <div class="card-value blue">${(stats.avg_hold_hours||0).toFixed(1)}h</div>
        </div>
    </div>

    <div class="grid grid-2">
        <div class="card">
            <div class="section-title">💰 盈亏分布</div>
            <table><tbody>
                <tr><td>做空策略</td><td><b>${fmtPnl(stats.short_pnl||0)}</b></td></tr>
                <tr><td>费率套利</td><td><b>${fmtPnl(stats.funding_pnl||0)}</b></td></tr>
                <tr><td>低风险策略</td><td><b>${fmtPnl(stats.low_risk_pnl||0)}</b></td></tr>
                <tr><td>最佳交易</td><td><b>${stats.best_trade?.symbol||'-'}</b> ${fmtPnl(stats.best_trade?.pnl||0)}</td></tr>
                <tr><td>最差交易</td><td><b>${stats.worst_trade?.symbol||'-'}</b> ${fmtPnl(stats.worst_trade?.pnl||0)}</td></tr>
                <tr><td>最大单日亏损</td><td>${fmtPnl(stats.max_drawdown_day||0)}</td></tr>
            </tbody></table>
        </div>
        <div class="card">
            <div class="section-title">📊 策略明细</div>
            <table><thead><tr><th>策略</th><th>交易数</th><th>盈亏</th><th>胜率</th></tr></thead><tbody>`;

    if (stats.strategy_breakdown) {
        for (const [strat, s] of Object.entries(stats.strategy_breakdown)) {
            const wr = s.count > 0 ? (s.wins / s.count * 100).toFixed(0) : 0;
            html += `<tr><td><b>${strat}</b></td><td>${s.count}</td><td>${fmtPnl(s.pnl)}</td><td>${wr}%</td></tr>`;
        }
    }
    html += `</tbody></table></div></div>`;

    // 每日明细
    if (stats.daily_breakdown && Object.keys(stats.daily_breakdown).length > 0) {
        html += `<div class="card" style="margin-top:12px;"><div class="section-title">📅 每日盈亏</div><table>
            <thead><tr><th>日期</th><th>盈亏</th><th>可视化</th></tr></thead><tbody>`;
        const maxAbs = Math.max(...Object.values(stats.daily_breakdown).map(v => Math.abs(v)), 1);
        for (const [day, pnl] of Object.entries(stats.daily_breakdown)) {
            const barW = Math.abs(pnl) / maxAbs * 100;
            const color = pnl >= 0 ? '#3fb950' : '#f85149';
            html += `<tr><td>${day}</td><td>${fmtPnl(pnl)}</td>
                <td><div style="height:12px;width:${barW}%;background:${color};border-radius:2px;"></div></td></tr>`;
        }
        html += `</tbody></table></div>`;
    }

    // 建议
    if (suggestions.length > 0) {
        html += `<div class="card" style="margin-top:12px;"><div class="section-title">💡 策略建议</div><ul style="padding-left:20px;">`;
        suggestions.forEach(s => { html += `<li style="margin-bottom:6px;font-size:0.85rem;">${s}</li>`; });
        html += `</ul></div>`;
    }

    // 元数据
    html += `<div class="card-sub" style="margin-top:12px;text-align:center;">
        报告范围: ${(meta.week_start||'').slice(0,10)} ~ ${(meta.week_end||'').slice(0,10)} |
        生成时间: ${(meta.generated_at||'').slice(0,16).replace('T',' ')} UTC</div>`;

    content.innerHTML = html;
}).catch(e => { document.getElementById('content').innerHTML = `<div class="empty-state">加载失败: ${e.message}</div>`; });
document.getElementById('nav-weekly').classList.add('active');
</script>
</body>
</html>'''



# ══════════════════════════════════════════════════════════════════
#  批量回测页面 HTML
# ══════════════════════════════════════════════════════════════════

BATCH_BACKTEST_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>批量回测 - Shadow Trading</title>
<style>''' + COMMON_STYLES + '''</style>
</head>
<body>
<div class="container">
    <h1>🔬 批量回测结果</h1>
    <div class="subtitle">多币种对比 | 相关性分析 | 推荐组合</div>
    ''' + NAV_HTML + '''
    <div id="content"><div class="empty-state">加载中...</div></div>
</div>
<script>
function pnlColor(val) { return val > 0 ? 'green' : val < 0 ? 'red' : ''; }
function fmtPnl(val) { const cls = pnlColor(val); return `<span class="${cls}">${val >= 0 ? '+' : ''}${val.toFixed(2)}U</span>`; }

fetch('/api/batch-backtest').then(r => r.json()).then(data => {
    const content = document.getElementById('content');
    if (!data || !data.report) {
        content.innerHTML = `<div class="empty-state"><p>暂无批量回测数据</p>
            <p style="margin-top:8px;font-size:0.8rem;">运行: <code>python3 backtest.py --batch</code></p></div>`;
        return;
    }
    const report = data.report;
    const summary = report.summary || {};
    const perCoin = report.per_coin_results || [];
    const rankings = report.rankings || [];
    const recommended = report.recommended_portfolio || [];
    const highCorr = report.high_correlation_pairs || {};

    let html = `
    <div class="grid grid-4">
        <div class="card"><div class="card-header">币种数量</div><div class="card-value blue">${summary.total_coins||0}</div></div>
        <div class="card"><div class="card-header">总交易数</div><div class="card-value">${summary.total_trades||0}</div></div>
        <div class="card"><div class="card-header">整体胜率</div><div class="card-value ${(summary.overall_win_rate||0) >= 50 ? 'green' : 'yellow'}">${summary.overall_win_rate||0}%</div></div>
        <div class="card"><div class="card-header">总盈亏</div><div class="card-value ${pnlColor(summary.total_pnl)}">${fmtPnl(summary.total_pnl||0)}</div></div>
    </div>`;

    // 各币种表现
    html += `<div class="card" style="margin-bottom:12px;"><div class="section-title">📋 各币种表现</div><table>
        <thead><tr><th>排名</th><th>币种</th><th>评分</th><th>交易数</th><th>胜率</th><th>盈亏比</th><th>盈亏</th><th>最大回撤</th><th>夏普率</th></tr></thead><tbody>`;
    rankings.forEach((coin, i) => {
        const cls = coin.total_pnl > 0 ? 'green' : 'red';
        html += `<tr><td><b>${i+1}</b></td><td><b>${coin.symbol}</b></td><td>${coin.score.toFixed(4)}</td>
            <td>${coin.total_trades}</td><td>${coin.win_rate}%</td><td>${coin.profit_loss_ratio.toFixed(2)}x</td>
            <td class="${cls}"><b>${coin.total_pnl >= 0 ? '+' : ''}${coin.total_pnl.toFixed(2)}U</b></td>
            <td>${coin.max_drawdown.toFixed(1)}%</td><td>${coin.sharpe_ratio.toFixed(2)}</td></tr>`;
    });
    html += `</tbody></table></div>`;

    // 推荐组合
    if (recommended.length > 0) {
        html += `<div class="card" style="margin-bottom:12px;"><div class="section-title">🏆 推荐组合（低相关性 Top 币种）</div>
            <div class="grid grid-5">`;
        recommended.forEach((coin, i) => {
            html += `<div class="card" style="border-color:#3fb950;">
                <div class="card-header">#${i+1} ${coin.symbol}</div>
                <div style="font-size:0.8rem;">评分: <b>${coin.score.toFixed(4)}</b></div>
                <div style="font-size:0.8rem;">胜率: ${coin.win_rate}%</div>
                <div style="font-size:0.8rem;">盈亏比: ${coin.profit_loss_ratio.toFixed(2)}x</div>
                <div style="font-size:0.8rem;" class="${pnlColor(coin.total_pnl)}">PnL: ${coin.total_pnl >= 0?'+':''}${coin.total_pnl.toFixed(1)}U</div>
            </div>`;
        });
        html += `</div></div>`;
    }

    // 高相关性警告
    if (Object.keys(highCorr).length > 0) {
        html += `<div class="card" style="margin-bottom:12px;"><div class="section-title">⚠️ 高相关性币对</div><table>
            <thead><tr><th>币对</th><th>相关系数</th><th>建议</th></tr></thead><tbody>`;
        for (const [pair, val] of Object.entries(highCorr)) {
            const [a, b] = pair.split('|');
            html += `<tr><td>${a} ↔ ${b}</td><td><b>${val.toFixed(4)}</b></td><td class="yellow">避免同时开仓</td></tr>`;
        }
        html += `</tbody></table></div>`;
    }

    html += `<div class="card-sub" style="margin-top:12px;text-align:center;">
        回测天数: ${data.days||90}天 | 生成时间: ${(data.timestamp||'').slice(0,16).replace('T',' ')} UTC</div>`;

    content.innerHTML = html;
}).catch(e => { document.getElementById('content').innerHTML = `<div class="empty-state">加载失败: ${e.message}</div>`; });
document.getElementById('nav-batch').classList.add('active');
</script>
</body>
</html>'''



# ══════════════════════════════════════════════════════════════════
#  策略评分页面 HTML
# ══════════════════════════════════════════════════════════════════

SIGNAL_SCORES_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>策略评分 - Shadow Trading</title>
<style>''' + COMMON_STYLES + '''</style>
</head>
<body>
<div class="container">
    <h1>🎯 策略评分详情</h1>
    <div class="subtitle">信号评分系统 | 动态仓位管理</div>
    ''' + NAV_HTML + '''
    <div id="content"><div class="empty-state">加载中...</div></div>
</div>
<script>
function pnlColor(val) { return val > 0 ? 'green' : val < 0 ? 'red' : ''; }
function fmtPnl(val) { if (val === null || val === undefined) return '--'; const cls = pnlColor(val); return `<span class="${cls}">${val >= 0 ? '+' : ''}${val.toFixed(2)}U</span>`; }

fetch('/api/signal-scores').then(r => r.json()).then(data => {
    const content = document.getElementById('content');
    const cfg = data.config || {};
    const trades = data.trades || [];

    let html = `
    <div class="grid grid-3">
        <div class="card">
            <div class="card-header">评分系统</div>
            <div class="card-value ${cfg.signal_score_enabled ? 'green' : 'red'}">${cfg.signal_score_enabled ? '已启用' : '已关闭'}</div>
        </div>
        <div class="card">
            <div class="card-header">A级阈值（全仓）</div>
            <div class="card-value green">≥ ${cfg.score_full_threshold||70}分</div>
        </div>
        <div class="card">
            <div class="card-header">B级阈值（半仓）</div>
            <div class="card-value yellow">≥ ${cfg.score_half_threshold||40}分</div>
            <div class="card-sub">&lt; ${cfg.score_half_threshold||40}分: 跳过</div>
        </div>
    </div>

    <div class="card" style="margin-bottom:12px;">
        <div class="section-title">📊 评分维度说明</div>
        <div class="grid grid-4" style="margin-top:8px;">
            <div><b>RSI强度</b> (0~25)<br><span class="card-sub">日线RSI越高越强</span></div>
            <div><b>妖币特征</b> (0~25)<br><span class="card-sub">妖币评分0/1/2/3</span></div>
            <div><b>触发方式</b> (0~25)<br><span class="card-sub">弃盘点>RSI回落</span></div>
            <div><b>市场热度</b> (0~25)<br><span class="card-sub">OI+费率+BTC趋势</span></div>
        </div>
    </div>

    <div class="card"><div class="section-title">📝 最近交易评分记录</div><table>
        <thead><tr><th>币种</th><th>方向</th><th>策略</th><th>评分</th><th>等级</th><th>维度</th><th>结果</th><th>状态</th></tr></thead><tbody>`;

    trades.forEach(t => {
        const gradeBadge = t.score_grade === 'A' ? '<span class="badge badge-a">A</span>'
            : t.score_grade === 'B' ? '<span class="badge badge-b">B</span>'
            : t.score_grade === 'SKIP' ? '<span class="badge badge-skip">SKIP</span>'
            : '<span class="card-sub">--</span>';
        const scoreBar = t.score !== null ? `<div class="risk-bar" style="width:80px;display:inline-block;vertical-align:middle;">
            <div class="risk-bar-fill" style="width:${t.score}%;background:${t.score>=70?'#3fb950':t.score>=40?'#d29922':'#f85149'}"></div></div>
            <span style="font-size:0.75rem;margin-left:4px;">${t.score}</span>` : '--';
        let dims = '--';
        if (t.score_details) {
            const d = t.score_details;
            dims = Object.entries(d).map(([k,v]) => `${k}:${v}`).join(' ');
        }
        const resultStr = t.pnl !== null ? fmtPnl(t.pnl) : '<span class="badge badge-open">持仓中</span>';
        const statusBadge = t.status === 'open' ? '<span class="badge badge-open">open</span>' : '<span class="badge badge-closed">closed</span>';
        html += `<tr><td><b>${t.symbol}</b></td><td>${t.direction}</td><td>${t.strategy||'--'}</td>
            <td>${scoreBar}</td><td>${gradeBadge}</td><td style="font-size:0.7rem;">${dims}</td>
            <td>${resultStr}</td><td>${statusBadge}</td></tr>`;
    });

    html += `</tbody></table></div>`;

    // 评分分布统计
    const scored = trades.filter(t => t.score !== null);
    if (scored.length > 0) {
        const aCount = scored.filter(t => t.score >= (cfg.score_full_threshold||70)).length;
        const bCount = scored.filter(t => t.score >= (cfg.score_half_threshold||40) && t.score < (cfg.score_full_threshold||70)).length;
        const skipCount = scored.filter(t => t.score < (cfg.score_half_threshold||40)).length;
        const aPnl = scored.filter(t => t.score >= (cfg.score_full_threshold||70) && t.pnl !== null).reduce((s,t) => s + t.pnl, 0);
        const bPnl = scored.filter(t => t.score >= (cfg.score_half_threshold||40) && t.score < (cfg.score_full_threshold||70) && t.pnl !== null).reduce((s,t) => s + t.pnl, 0);

        html += `<div class="card" style="margin-top:12px;"><div class="section-title">📈 评分分布统计</div>
            <div class="grid grid-3">
                <div><span class="badge badge-a">A级</span> ${aCount}笔 | PnL: ${fmtPnl(aPnl)}</div>
                <div><span class="badge badge-b">B级</span> ${bCount}笔 | PnL: ${fmtPnl(bPnl)}</div>
                <div><span class="badge badge-skip">跳过</span> ${skipCount}笔</div>
            </div></div>`;
    }

    content.innerHTML = html;
}).catch(e => { document.getElementById('content').innerHTML = `<div class="empty-state">加载失败: ${e.message}</div>`; });
document.getElementById('nav-scores').classList.add('active');
</script>
</body>
</html>'''



# ══════════════════════════════════════════════════════════════════
#  单币回测页面 HTML
# ══════════════════════════════════════════════════════════════════

BACKTEST_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>单币回测 - Shadow Trading</title>
<style>''' + COMMON_STYLES + '''
.chart-container { width: 100%; height: 200px; position: relative; margin-top: 12px; }
canvas { width: 100% !important; height: 100% !important; }
</style>
</head>
<body>
<div class="container">
    <h1>📈 单币回测结果</h1>
    <div class="subtitle" id="bt-meta">加载中...</div>
    ''' + NAV_HTML + '''
    <div id="content"><div class="empty-state"><p>正在加载回测数据...</p>
        <p style="margin-top:8px;font-size:0.8rem;">运行: <code>python3 backtest.py --symbol PEPE/USDT --days 90</code></p></div></div>
</div>
<script>
function pnlColor(val) { return val > 0 ? 'green' : val < 0 ? 'red' : ''; }
function fmtPnl(val) { const cls = pnlColor(val); return `<span class="${cls}">${val >= 0 ? '+' : ''}${val.toFixed(2)}U</span>`; }

function renderResult(data) {
    const content = document.getElementById('content');
    if (!data || (!data.results && !data.top_results)) {
        content.innerHTML = `<div class="empty-state"><p>暂无回测数据</p>
            <p style="margin-top:8px;font-size:0.8rem;">运行: <code>python3 backtest.py --symbol PEPE/USDT</code></p></div>`;
        return;
    }
    const meta = document.getElementById('bt-meta');
    meta.textContent = `${data.symbol || (data.symbols||[]).join(', ')} | ${data.days}天 | ${(data.timestamp||'').slice(0,16)}`;

    if (data.top_results) { renderGridResults(content, data.top_results); return; }
    const results = data.results || [];
    if (results.length === 0) { content.innerHTML = '<div class="empty-state">无结果</div>'; return; }
    let html = '';
    for (const r of results) { html += renderSingleResult(r); }
    content.innerHTML = html;
}

function renderSingleResult(r) {
    const p = r.params || {};
    const passBadge = (r.win_rate >= 50 && r.profit_loss_ratio >= 1.5)
        ? '<span class="badge badge-ok">达标</span>'
        : (r.win_rate >= 40 ? '<span class="badge badge-warn">需优化</span>' : '<span class="badge badge-stop">不佳</span>');

    let html = `<div class="grid grid-5">
        <div class="card"><div class="card-header">总盈亏</div><div class="card-value ${pnlColor(r.total_pnl)}">${r.total_pnl >= 0 ? '+' : ''}${r.total_pnl.toFixed(1)}U</div><div class="card-sub">${r.total_trades} 笔交易</div></div>
        <div class="card"><div class="card-header">胜率</div><div class="card-value ${r.win_rate >= 50 ? 'green' : 'yellow'}">${r.win_rate}%</div><div class="card-sub">${r.wins}胜 / ${r.losses}负</div></div>
        <div class="card"><div class="card-header">盈亏比</div><div class="card-value ${r.profit_loss_ratio >= 1.5 ? 'green' : 'yellow'}">${r.profit_loss_ratio.toFixed(2)}x</div><div class="card-sub">赢${r.avg_win.toFixed(1)} / 亏${r.avg_loss.toFixed(1)}</div></div>
        <div class="card"><div class="card-header">最大回撤</div><div class="card-value red">${r.max_drawdown.toFixed(1)}%</div><div class="card-sub">连亏${r.max_consecutive_losses}次</div></div>
        <div class="card"><div class="card-header">评级 ${passBadge}</div><div class="card-value blue">${r.sharpe_ratio.toFixed(2)}</div><div class="card-sub">夏普率</div></div>
    </div>
    <div class="card" style="margin-top:12px;"><div class="section-title">📊 权益曲线</div><div class="chart-container"><canvas id="equity-chart"></canvas></div></div>
    <div class="grid grid-2" style="margin-top:12px;">
        <div class="card"><div class="section-title">📝 交易明细</div><table>
            <thead><tr><th>入场时间</th><th>盈亏</th><th>原因</th><th>持仓</th><th>TP1</th></tr></thead><tbody>`;
    const trades = (r.trades || []).slice(-20);
    for (const t of trades) {
        const cls = t.pnl_usd > 0 ? 'green' : 'red';
        html += `<tr><td>${(t.entry_time||'').slice(0,16)}</td><td class="${cls}">${t.pnl_usd >= 0 ? '+' : ''}${t.pnl_usd.toFixed(2)}U</td><td>${t.exit_reason}</td><td>${t.hold_bars}h</td><td>${t.tp1_hit ? '✓' : ''}</td></tr>`;
    }
    html += `</tbody></table></div><div class="card"><div class="section-title">⚙️ 参数</div><table><tbody>
        <tr><td>RSI 阈值</td><td><b>${p.daily_rsi_min || '--'}</b></td></tr>
        <tr><td>RSI 回落</td><td><b>${p.h4_rsi_drop || '--'} 点</b></td></tr>
        <tr><td>TP1</td><td><b>-${p.tp1_pct || '--'}%</b></td></tr>
        <tr><td>TP2</td><td><b>-${p.tp2_pct || '--'}%</b></td></tr>
        <tr><td>硬止损</td><td><b>+${p.hard_stop_pct || '--'}%</b></td></tr>
        <tr><td>移动止损激活</td><td><b>${p.trail_activate_pct || '--'}%</b></td></tr>
        <tr><td>最大持仓</td><td><b>${p.max_hold_bars || '--'}h</b></td></tr>
        <tr><td>杠杆</td><td><b>${p.leverage || '--'}x</b></td></tr>
    </tbody></table></div></div>`;
    return html;
}

function renderGridResults(container, topResults) {
    let html = `<div class="card"><div class="section-title">🏆 参数网格搜索 Top ${topResults.length}</div><table>
        <thead><tr><th>#</th><th>PnL</th><th>胜率</th><th>盈亏比</th><th>回撤</th><th>连亏</th><th>TP1</th><th>TP2</th><th>止损</th><th>RSI</th><th>Drop</th><th>单数</th></tr></thead><tbody>`;
    topResults.slice(0, 20).forEach((r, i) => {
        const p = r.params || {};
        const cls = r.total_pnl > 0 ? 'green' : 'red';
        html += `<tr><td>${i+1}</td><td class="${cls}"><b>${r.total_pnl >= 0 ? '+' : ''}${r.total_pnl.toFixed(1)}</b></td><td>${r.win_rate}%</td><td>${r.profit_loss_ratio.toFixed(2)}x</td><td>${r.max_drawdown.toFixed(1)}%</td><td>${r.max_consecutive_losses}</td><td>${p.tp1_pct}%</td><td>${p.tp2_pct}%</td><td>${p.hard_stop_pct}%</td><td>${p.daily_rsi_min}</td><td>${p.h4_rsi_drop}</td><td>${r.total_trades}</td></tr>`;
    });
    html += '</tbody></table></div>';
    if (topResults.length > 0) { html += renderSingleResult(topResults[0]); }
    container.innerHTML = html;
    drawEquityChart(topResults[0]);
}

function drawEquityChart(result) {
    if (!result || !result.equity_curve || result.equity_curve.length < 2) return;
    const canvas = document.getElementById('equity-chart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const data = result.equity_curve;
    const w = canvas.parentElement.clientWidth, h = 200;
    canvas.width = w; canvas.height = h;
    const padding = { top: 20, right: 20, bottom: 30, left: 50 };
    const chartW = w - padding.left - padding.right, chartH = h - padding.top - padding.bottom;
    const minVal = Math.min(...data), maxVal = Math.max(...data);
    const range = maxVal - minVal || 1;
    ctx.fillStyle = '#161b22'; ctx.fillRect(0, 0, w, h);
    const baseY = padding.top + chartH - ((data[0] - minVal) / range) * chartH;
    ctx.strokeStyle = '#30363d'; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(padding.left, baseY); ctx.lineTo(w - padding.right, baseY); ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = data[data.length - 1] >= data[0] ? '#3fb950' : '#f85149';
    ctx.lineWidth = 2; ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
        const x = padding.left + (i / (data.length - 1)) * chartW;
        const y = padding.top + chartH - ((data[i] - minVal) / range) * chartH;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.fillStyle = '#8b949e'; ctx.font = '11px sans-serif';
    ctx.fillText(maxVal.toFixed(0) + 'U', 4, padding.top + 10);
    ctx.fillText(minVal.toFixed(0) + 'U', 4, h - padding.bottom - 4);
    const finalVal = data[data.length - 1];
    ctx.fillStyle = finalVal >= data[0] ? '#3fb950' : '#f85149';
    ctx.font = 'bold 12px sans-serif';
    ctx.fillText(`${finalVal.toFixed(1)}U`, w - padding.right - 50, padding.top + 10);
}

fetch('/api/backtest').then(r => r.json()).then(data => {
    renderResult(data);
    setTimeout(() => {
        const results = data.results || data.top_results;
        if (results && results.length > 0) drawEquityChart(results[0]);
    }, 100);
}).catch(err => { document.getElementById('content').innerHTML = `<div class="empty-state">加载失败: ${err.message}</div>`; });
document.getElementById('nav-bt').classList.add('active');
</script>
</body>
</html>'''


# ══════════════════════════════════════════════════════════════════
#  启动
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    port = 8080
    if '--port' in sys.argv:
        idx = sys.argv.index('--port')
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    print(f"🚀 Dashboard v2.0 启动: http://localhost:{port}")
    print(f"   页面: 主面板 | 低风险策略 | 周报 | 批量回测 | 单币回测 | 策略评分")
    print(f"   实时推送间隔: 10秒")
    print(f"   按 Ctrl+C 停止")

    # 后台推送线程
    push_thread = threading.Thread(target=background_push, daemon=True)
    push_thread.start()

    socketio.run(app, host='0.0.0.0', port=port, debug=False)
