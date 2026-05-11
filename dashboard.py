#!/usr/bin/env python3
"""
影子做空系统实时仪表盘 v1.0
Flask + WebSocket 实现实时价格推送和状态监控。

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
    load_json, utcnow_iso, today_str,
)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'shadow-system-dashboard'
socketio = SocketIO(app, cors_allowed_origins="*")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ══════════════════════════════════════════════════════════════════
#  数据读取
# ══════════════════════════════════════════════════════════════════

def get_dashboard_data() -> dict:
    """汇总所有数据供前端展示"""
    trades = load_json(TRADES_FILE, [])
    candidates = load_json(CANDIDATES_FILE, [])
    funding_trades = load_json(FUNDING_TRADES_FILE, [])
    risk_state = load_json(RISK_FILE, {})

    open_trades = [t for t in trades if t.get('status') == 'open']
    closed_trades = [t for t in trades if t.get('status') == 'closed']

    # 今日盈亏
    today = today_str()
    today_closed = [
        t for t in closed_trades
        if t.get('closed_at', '').startswith(today)
    ]
    today_pnl = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        for t in today_closed
    )

    # 累计盈亏
    total_pnl = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        for t in closed_trades
    )

    # 持仓浮盈
    open_pnl = sum(
        t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        for t in open_trades
    )

    # 费率套利统计
    funding_open = [t for t in funding_trades if t.get('status') == 'open']
    funding_closed = [t for t in funding_trades if t.get('status') == 'closed']
    funding_today_pnl = sum(
        t.get('total_pnl', 0)
        for t in funding_closed
        if t.get('closed_at', '').startswith(today)
    )
    funding_total_pnl = sum(t.get('total_pnl', 0) for t in funding_closed)

    # 胜率
    wins = sum(1 for t in closed_trades if (t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)) > 0)
    win_rate = (wins / len(closed_trades) * 100) if closed_trades else 0

    # PnL 历史（按日汇总）
    pnl_history = {}
    for t in closed_trades:
        closed_at = t.get('closed_at', '')
        if not closed_at:
            continue
        day = closed_at[:10]  # YYYY-MM-DD
        pnl = t.get('tp1_locked_pnl', 0) + t.get('pnl', 0)
        pnl_history[day] = pnl_history.get(day, 0) + pnl
    # 加入费率套利
    for t in funding_closed:
        closed_at = t.get('closed_at', '')
        if not closed_at:
            continue
        day = closed_at[:10]
        pnl_history[day] = pnl_history.get(day, 0) + t.get('total_pnl', 0)
    # 排序
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

    return {
        'account': {
            'balance': round(config.ACCOUNT_BALANCE + total_pnl + funding_total_pnl, 2),
            'initial_balance': config.ACCOUNT_BALANCE,
            'leverage': config.LEVERAGE,
            'today_pnl': round(today_pnl, 2),
            'total_pnl': round(total_pnl, 2),
            'open_pnl': round(open_pnl, 2),
            'win_rate': round(win_rate, 1),
            'total_trades': len(closed_trades),
        },
        'open_trades': open_trades,
        'closed_trades': closed_trades[-20:],
        'candidates': candidates,
        'funding': {
            'open': funding_open,
            'closed': funding_closed[-10:],
            'today_pnl': round(funding_today_pnl, 4),
            'total_pnl': round(funding_total_pnl, 4),
        },
        'risk': risk_state,
        'pnl_chart': pnl_chart_data,
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
    """返回最近一次回测结果"""
    bt_file = os.path.join(SCRIPT_DIR, 'backtest_results.json')
    data = load_json(bt_file, {})
    return jsonify(data)


@app.route('/backtest')
def backtest_page():
    return render_template_string(BACKTEST_HTML)


@socketio.on('connect')
def handle_connect():
    """新连接时立即推送一次数据"""
    data = get_dashboard_data()
    socketio.emit('update', data)


# ══════════════════════════════════════════════════════════════════
#  HTML 模板
# ══════════════════════════════════════════════════════════════════

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Shadow Trading Dashboard</title>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    min-height: 100vh;
    padding: 16px;
}
.container { max-width: 1400px; margin: 0 auto; }
h1 {
    font-size: 1.5rem;
    color: #58a6ff;
    margin-bottom: 4px;
}
.subtitle { color: #8b949e; font-size: 0.85rem; margin-bottom: 16px; }

/* Grid */
.grid { display: grid; gap: 12px; margin-bottom: 12px; }
.grid-4 { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.grid-2 { grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); }

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
.card-value {
    font-size: 1.8rem;
    font-weight: 700;
}
.card-sub { font-size: 0.75rem; color: #8b949e; margin-top: 4px; }

/* Colors */
.green { color: #3fb950; }
.red { color: #f85149; }
.yellow { color: #d29922; }
.blue { color: #58a6ff; }

/* Table */
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th {
    text-align: left;
    padding: 8px 6px;
    border-bottom: 1px solid #30363d;
    color: #8b949e;
    font-weight: 500;
}
td {
    padding: 8px 6px;
    border-bottom: 1px solid #21262d;
}
tr:hover td { background: #1c2128; }

/* Status badge */
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.7rem;
    font-weight: 600;
}
.badge-open { background: #1f3d2b; color: #3fb950; }
.badge-closed { background: #3d1f1f; color: #f85149; }
.badge-ok { background: #1f3d2b; color: #3fb950; }
.badge-warn { background: #3d2f1f; color: #d29922; }
.badge-stop { background: #3d1f1f; color: #f85149; }

/* Risk bar */
.risk-bar {
    height: 6px;
    background: #21262d;
    border-radius: 3px;
    margin-top: 6px;
    overflow: hidden;
}
.risk-bar-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.5s ease;
}

/* Live indicator */
.live-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    background: #3fb950;
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 2s infinite;
}
@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}

/* Section title */
.section-title {
    font-size: 1rem;
    font-weight: 600;
    margin: 16px 0 8px;
    color: #c9d1d9;
}

/* Responsive */
@media (max-width: 768px) {
    .grid-4 { grid-template-columns: repeat(2, 1fr); }
    .grid-2 { grid-template-columns: 1fr; }
    .card-value { font-size: 1.4rem; }
}
</style>
</head>
<body>
<div class="container">
    <h1><span class="live-dot"></span>Shadow Trading System</h1>
    <div class="subtitle"><span id="timestamp">连接中...</span> | <a href="/backtest" style="color:#58a6ff;text-decoration:none;">📈 回测结果</a></div>

    <!-- Summary Cards -->
    <div class="grid grid-4">
        <div class="card">
            <div class="card-header">今日盈亏</div>
            <div class="card-value" id="today-pnl">--</div>
            <div class="card-sub">做空 + 费率套利</div>
        </div>
        <div class="card">
            <div class="card-header">累计盈亏</div>
            <div class="card-value" id="total-pnl">--</div>
            <div class="card-sub">总交易 <span id="total-trades">0</span> 单</div>
        </div>
        <div class="card">
            <div class="card-header">账户余额</div>
            <div class="card-value" id="open-pnl">--</div>
            <div class="card-sub" id="leverage-info">--</div>
        </div>
        <div class="card">
            <div class="card-header">胜率</div>
            <div class="card-value" id="win-rate">--</div>
            <div class="card-sub">盈利单/总单</div>
        </div>
    </div>

    <!-- Main content -->
    <div class="grid grid-2">
        <!-- Open Positions -->
        <div class="card">
            <div class="section-title">🔴 持仓中</div>
            <table>
                <thead><tr>
                    <th>币种</th><th>入场</th><th>现价</th><th>盈亏%</th><th>盈亏U</th><th>状态</th>
                </tr></thead>
                <tbody id="open-trades"></tbody>
            </table>
            <div class="card-sub" id="open-empty" style="padding:12px;text-align:center;display:none;">暂无持仓</div>
        </div>

        <!-- Risk Control -->
        <div class="card">
            <div class="section-title">🛡️ 风控状态</div>
            <div id="risk-content"></div>
        </div>
    </div>

    <div class="grid grid-2">
        <!-- Funding Arbitrage -->
        <div class="card">
            <div class="section-title">💰 费率套利</div>
            <div style="margin-bottom:8px;">
                <span class="card-sub">今日：</span>
                <span id="funding-today" class="green">--</span>
                <span class="card-sub" style="margin-left:12px;">累计：</span>
                <span id="funding-total" class="green">--</span>
            </div>
            <table>
                <thead><tr><th>币种</th><th>费率</th><th>方向PnL</th><th>费率收入</th><th>总计</th></tr></thead>
                <tbody id="funding-trades"></tbody>
            </table>
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
        <div style="height:220px;position:relative;">
            <canvas id="pnl-chart"></canvas>
        </div>
        <div style="margin-top:8px;display:flex;gap:16px;font-size:0.75rem;color:#8b949e;">
            <span>🟢 累计盈亏</span>
            <span>🔵 每日盈亏</span>
        </div>
    </div>

    <!-- Closed Trades -->
    <div class="card" style="margin-top:12px;">
        <div class="section-title">✅ 最近平仓记录</div>
        <table>
            <thead><tr>
                <th>币种</th><th>方向</th><th>入场</th><th>平仓价</th><th>盈亏</th><th>原因</th><th>时间</th>
            </tr></thead>
            <tbody id="closed-trades"></tbody>
        </table>
    </div>
</div>

<script>
const socket = io();

function pnlColor(val) {
    if (val > 0) return 'green';
    if (val < 0) return 'red';
    return '';
}

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

    document.getElementById('open-pnl').innerHTML = fmtPnl(a.balance, 'U');
    document.getElementById('open-pnl').className = 'card-value ' + (a.balance >= a.initial_balance ? 'green' : 'red');

    document.getElementById('total-trades').textContent = a.total_trades;
    document.getElementById('leverage-info').textContent = `余额 ${a.balance}U（初始${a.initial_balance}U）`;
    document.getElementById('win-rate').textContent = a.win_rate + '%';
    document.getElementById('win-rate').className = 'card-value ' + (a.win_rate >= 50 ? 'green' : 'yellow');

    // Open trades
    const openTbody = document.getElementById('open-trades');
    const openEmpty = document.getElementById('open-empty');
    if (!data.open_trades || data.open_trades.length === 0) {
        openTbody.innerHTML = '';
        openEmpty.style.display = 'block';
    } else {
        openEmpty.style.display = 'none';
        openTbody.innerHTML = data.open_trades.map(t => {
            const entry = t.entry_price || 0;
            const cur = t.current_price || entry;
            const pnlPct = entry > 0 ? ((entry - cur) / entry * 100) : 0;
            const leverage = t.leverage || 10;
            const pnlU = (t.stake_remaining || t.stake || 100) * leverage * pnlPct / 100;
            const tp1 = t.tp1_triggered ? '<span class="badge badge-ok">TP1✓</span>' : '';
            return `<tr>
                <td><b>${t.symbol}</b></td>
                <td>${entry.toFixed(6)}</td>
                <td>${cur.toFixed(6)}</td>
                <td>${fmtPct(pnlPct)}</td>
                <td>${fmtPnl(pnlU)}</td>
                <td>${tp1}</td>
            </tr>`;
        }).join('');
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
        <div style="margin-bottom:8px;">
            <span>今日亏损：${dailyLoss.toFixed(1)} / ${dailyMax}U</span>
            <div class="risk-bar"><div class="risk-bar-fill" style="width:${dailyPct}%;background:${barColor}"></div></div>
        </div>
        <div style="margin-bottom:8px;">今日开仓：${trades_opened} / ${maxTrades} 次</div>
        <div style="margin-bottom:8px;">连续亏损：${consLoss} / 3 次</div>
        <div style="margin-bottom:8px;">持仓占用：${(risk.total_open_stake||0).toFixed(0)}U</div>
        ${paused ? '<div class="red" style="margin-top:8px;">暂停至：' + (risk.paused_until||'').slice(0,16) + ' UTC</div>' : ''}
    `;

    // Funding
    document.getElementById('funding-today').innerHTML = fmtPnl(data.funding?.today_pnl || 0);
    document.getElementById('funding-total').innerHTML = fmtPnl(data.funding?.total_pnl || 0);

    const fundingTbody = document.getElementById('funding-trades');
    const allFunding = [...(data.funding?.open || []), ...(data.funding?.closed || [])].slice(-8);
    fundingTbody.innerHTML = allFunding.map(t => {
        const badge = t.status === 'open'
            ? '<span class="badge badge-open">持仓</span>'
            : '<span class="badge badge-closed">已平</span>';
        return `<tr>
            <td>${t.symbol} ${badge}</td>
            <td>${(t.funding_rate||0).toFixed(4)}%</td>
            <td>${fmtPnl(t.pnl||0)}</td>
            <td>${fmtPnl(t.funding_income||0)}</td>
            <td><b>${fmtPnl(t.total_pnl||0)}</b></td>
        </tr>`;
    }).join('');

    // Candidates
    const candTbody = document.getElementById('candidates');
    const candEmpty = document.getElementById('cand-empty');
    if (!data.candidates || data.candidates.length === 0) {
        candTbody.innerHTML = '';
        candEmpty.style.display = 'block';
    } else {
        candEmpty.style.display = 'none';
        candTbody.innerHTML = data.candidates.slice(0, 15).map(c => {
            const badge = c.triggered
                ? '<span class="badge badge-closed">已触发</span>'
                : '<span class="badge badge-open">等待中</span>';
            const yao = c.yao_score >= 2 ? '🔥' : (c.yao_score === 1 ? '⚡' : '📌');
            return `<tr>
                <td>${c.symbol}</td>
                <td><b>${c.rsi_1d}</b></td>
                <td>${fmtPct(c.pct24h)}</td>
                <td>${(c.oi_change||0).toFixed(0)}%</td>
                <td>${yao} ${c.yao_score}/3</td>
                <td>${badge}</td>
            </tr>`;
        }).join('');
    }

    // Closed trades
    const closedTbody = document.getElementById('closed-trades');
    const closedList = (data.closed_trades || []).slice().reverse().slice(0, 15);
    closedTbody.innerHTML = closedList.map(t => {
        const pnl = (t.tp1_locked_pnl || 0) + (t.pnl || 0);
        const closedAt = (t.closed_at || '').slice(0, 16).replace('T', ' ');
        return `<tr>
            <td><b>${t.symbol}</b></td>
            <td>${t.direction}</td>
            <td>${(t.entry_price||0).toFixed(6)}</td>
            <td>${(t.current_price||0).toFixed(6)}</td>
            <td><b>${fmtPnl(pnl)}</b></td>
            <td>${t.close_reason || '--'}</td>
            <td>${closedAt}</td>
        </tr>`;
    }).join('');
}

socket.on('update', function(data) {
    updateDashboard(data);
    drawPnlChart(data.pnl_chart);
});
socket.on('connect', () => {
    document.getElementById('timestamp').textContent = '已连接，等待数据...';
});
socket.on('disconnect', () => {
    document.getElementById('timestamp').textContent = '⚠️ 连接断开，重连中...';
});

function drawPnlChart(chartData) {
    if (!chartData || !chartData.dates || chartData.dates.length < 2) return;
    const canvas = document.getElementById('pnl-chart');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const w = canvas.parentElement.clientWidth;
    const h = 220;
    canvas.width = w;
    canvas.height = h;

    const dates = chartData.dates;
    const daily = chartData.daily_pnl;
    const cum = chartData.cumulative;
    const n = dates.length;

    const padding = { top: 20, right: 20, bottom: 30, left: 50 };
    const chartW = w - padding.left - padding.right;
    const chartH = h - padding.top - padding.bottom;

    // 背景
    ctx.fillStyle = '#161b22';
    ctx.fillRect(0, 0, w, h);

    // 累计曲线范围
    const allVals = [...cum, ...daily];
    const minVal = Math.min(...allVals, 0);
    const maxVal = Math.max(...allVals, 0);
    const range = (maxVal - minVal) || 1;

    // 零线
    const zeroY = padding.top + chartH - ((0 - minVal) / range) * chartH;
    ctx.strokeStyle = '#30363d';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padding.left, zeroY);
    ctx.lineTo(w - padding.right, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);

    // 每日盈亏柱状
    const barWidth = Math.max(2, (chartW / n) * 0.6);
    for (let i = 0; i < n; i++) {
        const x = padding.left + (i / (n - 1)) * chartW;
        const val = daily[i];
        const barH = Math.abs(val / range) * chartH;
        const y = val >= 0 ? zeroY - barH : zeroY;
        ctx.fillStyle = val >= 0 ? 'rgba(63,185,80,0.4)' : 'rgba(248,81,73,0.4)';
        ctx.fillRect(x - barWidth/2, y, barWidth, barH);
    }

    // 累计曲线
    ctx.strokeStyle = '#3fb950';
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
        const x = padding.left + (i / (n - 1)) * chartW;
        const y = padding.top + chartH - ((cum[i] - minVal) / range) * chartH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // 标注
    ctx.fillStyle = '#8b949e';
    ctx.font = '10px sans-serif';
    ctx.fillText(maxVal.toFixed(0) + 'U', 4, padding.top + 10);
    ctx.fillText(minVal.toFixed(0) + 'U', 4, h - padding.bottom - 4);
    if (dates.length > 0) {
        ctx.fillText(dates[0], padding.left, h - 6);
        ctx.fillText(dates[dates.length-1], w - padding.right - 60, h - 6);
    }

    // 最终累计值
    const finalCum = cum[cum.length - 1];
    ctx.fillStyle = finalCum >= 0 ? '#3fb950' : '#f85149';
    ctx.font = 'bold 12px sans-serif';
    ctx.fillText(`${finalCum >= 0 ? '+' : ''}${finalCum.toFixed(1)}U`, w - padding.right - 55, padding.top + 12);
}
</script>
</body>
</html>'''


# ══════════════════════════════════════════════════════════════════
#  回测结果页面 HTML
# ══════════════════════════════════════════════════════════════════

BACKTEST_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backtest Results</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    min-height: 100vh;
    padding: 16px;
}
.container { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 1.5rem; color: #58a6ff; margin-bottom: 4px; }
.subtitle { color: #8b949e; font-size: 0.85rem; margin-bottom: 16px; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }

.grid { display: grid; gap: 12px; margin-bottom: 12px; }
.grid-5 { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
.grid-2 { grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); }

.card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 16px;
}
.card-header { font-size: 0.8rem; color: #8b949e; margin-bottom: 6px; }
.card-value { font-size: 1.6rem; font-weight: 700; }

.green { color: #3fb950; }
.red { color: #f85149; }
.yellow { color: #d29922; }
.blue { color: #58a6ff; }

table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th { text-align: left; padding: 8px 6px; border-bottom: 1px solid #30363d; color: #8b949e; }
td { padding: 8px 6px; border-bottom: 1px solid #21262d; }
tr:hover td { background: #1c2128; }

.section-title { font-size: 1rem; font-weight: 600; margin: 16px 0 8px; }

.chart-container {
    width: 100%;
    height: 200px;
    position: relative;
    margin-top: 12px;
}
canvas { width: 100% !important; height: 100% !important; }

.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.7rem;
    font-weight: 600;
}
.badge-pass { background: #1f3d2b; color: #3fb950; }
.badge-fail { background: #3d1f1f; color: #f85149; }
.badge-warn { background: #3d2f1f; color: #d29922; }

.empty-state { text-align: center; padding: 40px; color: #8b949e; }

@media (max-width: 768px) {
    .grid-5 { grid-template-columns: repeat(2, 1fr); }
    .grid-2 { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<div class="container">
    <h1>📈 回测结果</h1>
    <div class="subtitle">
        <a href="/">← 返回主面板</a> |
        <span id="bt-meta">加载中...</span>
    </div>

    <div id="content">
        <div class="empty-state">
            <p>正在加载回测数据...</p>
            <p style="margin-top:8px;font-size:0.8rem;">如果没有数据，请先运行：<code>python3 backtest.py --symbol PEPE/USDT --days 90</code></p>
        </div>
    </div>
</div>

<script>
function pnlColor(val) { return val > 0 ? 'green' : val < 0 ? 'red' : ''; }

function renderResult(data) {
    const content = document.getElementById('content');

    if (!data || (!data.results && !data.top_results)) {
        content.innerHTML = `<div class="empty-state">
            <p>暂无回测数据</p>
            <p style="margin-top:8px;font-size:0.8rem;">运行: <code>python3 backtest.py --symbol PEPE/USDT</code></p>
        </div>`;
        return;
    }

    const meta = document.getElementById('bt-meta');
    meta.textContent = `${data.symbol || (data.symbols||[]).join(', ')} | ${data.days}天 | ${(data.timestamp||'').slice(0,16)}`;

    // Grid search results
    if (data.top_results) {
        renderGridResults(content, data.top_results);
        return;
    }

    // Single/multi backtest
    const results = data.results || [];
    if (results.length === 0) {
        content.innerHTML = '<div class="empty-state">无结果</div>';
        return;
    }

    let html = '';
    for (const r of results) {
        html += renderSingleResult(r);
    }
    content.innerHTML = html;
}

function renderSingleResult(r) {
    const p = r.params || {};
    const passBadge = (r.win_rate >= 50 && r.profit_loss_ratio >= 1.5)
        ? '<span class="badge badge-pass">达标</span>'
        : (r.win_rate >= 40 ? '<span class="badge badge-warn">需优化</span>' : '<span class="badge badge-fail">不佳</span>');

    let html = `
    <div class="grid grid-5">
        <div class="card">
            <div class="card-header">总盈亏</div>
            <div class="card-value ${pnlColor(r.total_pnl)}">${r.total_pnl >= 0 ? '+' : ''}${r.total_pnl.toFixed(1)}U</div>
            <div style="font-size:0.75rem;color:#8b949e;margin-top:4px;">${r.total_trades} 笔交易</div>
        </div>
        <div class="card">
            <div class="card-header">胜率</div>
            <div class="card-value ${r.win_rate >= 50 ? 'green' : 'yellow'}">${r.win_rate}%</div>
            <div style="font-size:0.75rem;color:#8b949e;margin-top:4px;">${r.wins}胜 / ${r.losses}负</div>
        </div>
        <div class="card">
            <div class="card-header">盈亏比</div>
            <div class="card-value ${r.profit_loss_ratio >= 1.5 ? 'green' : 'yellow'}">${r.profit_loss_ratio.toFixed(2)}x</div>
            <div style="font-size:0.75rem;color:#8b949e;margin-top:4px;">赢${r.avg_win.toFixed(1)} / 亏${r.avg_loss.toFixed(1)}</div>
        </div>
        <div class="card">
            <div class="card-header">最大回撤</div>
            <div class="card-value red">${r.max_drawdown.toFixed(1)}%</div>
            <div style="font-size:0.75rem;color:#8b949e;margin-top:4px;">连亏${r.max_consecutive_losses}次</div>
        </div>
        <div class="card">
            <div class="card-header">评级 ${passBadge}</div>
            <div class="card-value blue">${r.sharpe_ratio.toFixed(2)}</div>
            <div style="font-size:0.75rem;color:#8b949e;margin-top:4px;">夏普率</div>
        </div>
    </div>

    <div class="card" style="margin-top:12px;">
        <div class="section-title">📊 权益曲线</div>
        <div class="chart-container"><canvas id="equity-chart"></canvas></div>
    </div>

    <div class="grid grid-2" style="margin-top:12px;">
        <div class="card">
            <div class="section-title">📝 交易明细</div>
            <table>
                <thead><tr><th>入场时间</th><th>盈亏</th><th>原因</th><th>持仓</th><th>TP1</th></tr></thead>
                <tbody>`;

    const trades = (r.trades || []).slice(-20);
    for (const t of trades) {
        const cls = t.pnl_usd > 0 ? 'green' : 'red';
        const tp1 = t.tp1_hit ? '✓' : '';
        html += `<tr>
            <td>${(t.entry_time||'').slice(0,16)}</td>
            <td class="${cls}">${t.pnl_usd >= 0 ? '+' : ''}${t.pnl_usd.toFixed(2)}U</td>
            <td>${t.exit_reason}</td>
            <td>${t.hold_bars}h</td>
            <td>${tp1}</td>
        </tr>`;
    }

    html += `</tbody></table></div>
        <div class="card">
            <div class="section-title">⚙️ 参数</div>
            <table>
                <tbody>
                    <tr><td>RSI 阈值</td><td><b>${p.daily_rsi_min || '--'}</b></td></tr>
                    <tr><td>RSI 回落</td><td><b>${p.h4_rsi_drop || '--'} 点</b></td></tr>
                    <tr><td>TP1</td><td><b>-${p.tp1_pct || '--'}%</b></td></tr>
                    <tr><td>TP2</td><td><b>-${p.tp2_pct || '--'}%</b></td></tr>
                    <tr><td>硬止损</td><td><b>+${p.hard_stop_pct || '--'}%</b></td></tr>
                    <tr><td>移动止损激活</td><td><b>${p.trail_activate_pct || '--'}%</b></td></tr>
                    <tr><td>移动止损回撤</td><td><b>${((p.trail_drawdown_pct||0)*100).toFixed(0)}%</b></td></tr>
                    <tr><td>最大持仓</td><td><b>${p.max_hold_bars || '--'}h</b></td></tr>
                    <tr><td>杠杆</td><td><b>${p.leverage || '--'}x</b></td></tr>
                </tbody>
            </table>
        </div>
    </div>`;

    return html;
}

function renderGridResults(container, topResults) {
    let html = `<div class="card"><div class="section-title">🏆 参数网格搜索 Top ${topResults.length}</div>
    <table>
        <thead><tr>
            <th>#</th><th>PnL</th><th>胜率</th><th>盈亏比</th><th>回撤</th><th>连亏</th>
            <th>TP1</th><th>TP2</th><th>止损</th><th>RSI</th><th>Drop</th><th>单数</th>
        </tr></thead><tbody>`;

    topResults.slice(0, 20).forEach((r, i) => {
        const p = r.params || {};
        const cls = r.total_pnl > 0 ? 'green' : 'red';
        html += `<tr>
            <td>${i+1}</td>
            <td class="${cls}"><b>${r.total_pnl >= 0 ? '+' : ''}${r.total_pnl.toFixed(1)}</b></td>
            <td>${r.win_rate}%</td>
            <td>${r.profit_loss_ratio.toFixed(2)}x</td>
            <td>${r.max_drawdown.toFixed(1)}%</td>
            <td>${r.max_consecutive_losses}</td>
            <td>${p.tp1_pct}%</td>
            <td>${p.tp2_pct}%</td>
            <td>${p.hard_stop_pct}%</td>
            <td>${p.daily_rsi_min}</td>
            <td>${p.h4_rsi_drop}</td>
            <td>${r.total_trades}</td>
        </tr>`;
    });

    html += '</tbody></table></div>';

    // 最优参数详情
    if (topResults.length > 0) {
        html += renderSingleResult(topResults[0]);
    }

    container.innerHTML = html;
    drawEquityChart(topResults[0]);
}

function drawEquityChart(result) {
    if (!result || !result.equity_curve || result.equity_curve.length < 2) return;
    const canvas = document.getElementById('equity-chart');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const data = result.equity_curve;
    const w = canvas.parentElement.clientWidth;
    const h = 200;
    canvas.width = w;
    canvas.height = h;

    const padding = { top: 20, right: 20, bottom: 30, left: 50 };
    const chartW = w - padding.left - padding.right;
    const chartH = h - padding.top - padding.bottom;

    const minVal = Math.min(...data);
    const maxVal = Math.max(...data);
    const range = maxVal - minVal || 1;

    // 背景
    ctx.fillStyle = '#161b22';
    ctx.fillRect(0, 0, w, h);

    // 基线（初始资金）
    const baseY = padding.top + chartH - ((data[0] - minVal) / range) * chartH;
    ctx.strokeStyle = '#30363d';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padding.left, baseY);
    ctx.lineTo(w - padding.right, baseY);
    ctx.stroke();
    ctx.setLineDash([]);

    // 权益曲线
    ctx.strokeStyle = data[data.length - 1] >= data[0] ? '#3fb950' : '#f85149';
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < data.length; i++) {
        const x = padding.left + (i / (data.length - 1)) * chartW;
        const y = padding.top + chartH - ((data[i] - minVal) / range) * chartH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // 标注
    ctx.fillStyle = '#8b949e';
    ctx.font = '11px sans-serif';
    ctx.fillText(maxVal.toFixed(0) + 'U', 4, padding.top + 10);
    ctx.fillText(minVal.toFixed(0) + 'U', 4, h - padding.bottom - 4);
    ctx.fillText('Start', padding.left, h - 8);
    ctx.fillText('End', w - padding.right - 20, h - 8);

    // 最终值
    const finalVal = data[data.length - 1];
    const finalColor = finalVal >= data[0] ? '#3fb950' : '#f85149';
    ctx.fillStyle = finalColor;
    ctx.font = 'bold 12px sans-serif';
    ctx.fillText(`${finalVal.toFixed(1)}U`, w - padding.right - 50, padding.top + 10);
}

// 加载数据
fetch('/api/backtest')
    .then(r => r.json())
    .then(data => {
        renderResult(data);
        // 绘制图表（延迟以确保 DOM 就绪）
        setTimeout(() => {
            const results = data.results || data.top_results;
            if (results && results.length > 0) {
                drawEquityChart(results[0]);
            }
        }, 100);
    })
    .catch(err => {
        document.getElementById('content').innerHTML =
            `<div class="empty-state"><p>加载失败: ${err.message}</p></div>`;
    });
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

    print(f"🚀 Dashboard 启动: http://localhost:{port}")
    print(f"   实时推送间隔: 10秒")
    print(f"   按 Ctrl+C 停止")

    # 后台推送线程
    push_thread = threading.Thread(target=background_push, daemon=True)
    push_thread.start()

    socketio.run(app, host='0.0.0.0', port=port, debug=False)
