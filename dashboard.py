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

    return {
        'account': {
            'balance': config.ACCOUNT_BALANCE,
            'leverage': config.LEVERAGE,
            'today_pnl': round(today_pnl, 2),
            'total_pnl': round(total_pnl, 2),
            'open_pnl': round(open_pnl, 2),
            'win_rate': round(win_rate, 1),
            'total_trades': len(closed_trades),
        },
        'open_trades': open_trades,
        'closed_trades': closed_trades[-20:],  # 最近20条
        'candidates': candidates,
        'funding': {
            'open': funding_open,
            'closed': funding_closed[-10:],
            'today_pnl': round(funding_today_pnl, 4),
            'total_pnl': round(funding_total_pnl, 4),
        },
        'risk': risk_state,
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
    <div class="subtitle" id="timestamp">连接中...</div>

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
            <div class="card-header">持仓浮盈</div>
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

    document.getElementById('open-pnl').innerHTML = fmtPnl(a.open_pnl);
    document.getElementById('open-pnl').className = 'card-value ' + pnlColor(a.open_pnl);

    document.getElementById('total-trades').textContent = a.total_trades;
    document.getElementById('leverage-info').textContent = `${a.balance}U × ${a.leverage}x`;
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

socket.on('update', updateDashboard);
socket.on('connect', () => {
    document.getElementById('timestamp').textContent = '已连接，等待数据...';
});
socket.on('disconnect', () => {
    document.getElementById('timestamp').textContent = '⚠️ 连接断开，重连中...';
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
