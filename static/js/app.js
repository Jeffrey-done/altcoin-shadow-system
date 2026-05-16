/* ═══════════════════════════════════════════════════════════════
   Shadow Trading System - Core App Module
   Theme toggle, notification panel, auto-refresh, shared utilities
   ═══════════════════════════════════════════════════════════════ */

// ── Shared Utilities ─────────────────────────────────────────────
const App = {
    pnlColor(val) { return val > 0 ? 'green' : val < 0 ? 'red' : ''; },

    fmtPnl(val, suffix = 'U') {
        const cls = this.pnlColor(val);
        const sign = val >= 0 ? '+' : '';
        return `<span class="${cls}">${sign}${val.toFixed(2)}${suffix}</span>`;
    },

    fmtPct(val) {
        const cls = this.pnlColor(val);
        const sign = val >= 0 ? '+' : '';
        return `<span class="${cls}">${sign}${val.toFixed(1)}%</span>`;
    },

    formatTime(isoStr) {
        if (!isoStr) return '--';
        try {
            return new Date(isoStr).toLocaleString('zh-CN', {
                timeZone: 'Asia/Shanghai',
                month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit',
                hour12: false
            });
        } catch { return isoStr.slice(0, 16); }
    },

    // Generate trend label for summary cards
    trendLabel(todayPnl, yesterdayPnl) {
        if (yesterdayPnl === undefined || yesterdayPnl === null) return '';
        if (yesterdayPnl === 0 && todayPnl === 0) return '';
        if (yesterdayPnl === 0) {
            return todayPnl > 0
                ? '<span class="card-trend up">↑ 今日盈利中</span>'
                : '<span class="card-trend down">↓ 今日亏损中</span>';
        }
        const change = todayPnl - yesterdayPnl;
        const pct = Math.abs(change / Math.abs(yesterdayPnl) * 100).toFixed(0);
        if (change > 0) {
            return `<span class="card-trend up">↑${pct}% vs 昨日</span>`;
        } else if (change < 0) {
            return `<span class="card-trend down">▼${pct}% vs 昨日</span>`;
        }
        return '<span class="card-trend neutral">= 与昨日持平</span>';
    },

    // ROI calculation
    roiLabel(totalPnl, initialBalance) {
        if (!initialBalance) return '';
        const roi = (totalPnl / initialBalance * 100).toFixed(1);
        const cls = totalPnl >= 0 ? 'green' : 'red';
        return `<span class="${cls}" style="font-size:0.75rem;margin-left:4px;">(ROI ${roi}%)</span>`;
    }
};

// ── Theme Toggle ─────────────────────────────────────────────────
// NOTE: 主题切换的实际实现在 templates/base.html 的 inline <script> 里，
// 那边正确地切换 #theme-icon-dark / #theme-icon-light 两个 lucide svg 图标。
// 这里以前还有一个 ThemeManager，会用 textContent='☀️' 覆盖按钮内容把
// lucide svg 直接抹掉，并和 base.html 的 click handler 双重绑定（点一次切两次）。
// 已移除该实现，保留空对象避免老代码 import 时报 undefined。
const ThemeManager = {
    init() { /* deprecated: see templates/base.html */ }
};

// ── Notification Panel ───────────────────────────────────────────
const NotificationPanel = {
    events: [],
    maxEvents: 50,
    isOpen: false,

    init() {
        const toggle = document.getElementById('notification-toggle');
        const close = document.getElementById('notification-close');
        const overlay = document.getElementById('notification-overlay');

        if (toggle) toggle.addEventListener('click', () => this.toggle());
        if (close) close.addEventListener('click', () => this.close());
        if (overlay) overlay.addEventListener('click', () => this.close());

        // Load events
        this.fetchEvents();
        // 每 30s 轮询一次 /api/events，但 tab 不可见时跳过：
        // 没有用户在看的 tab 不需要持续刷事件，省后端 IO + 客户端电量
        setInterval(() => {
            if (document.visibilityState === 'visible') {
                this.fetchEvents();
            }
        }, 30000);
        // 切回前台时立刻补一次，让用户看到最新状态
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                this.fetchEvents();
            }
        });
    },

    toggle() {
        this.isOpen ? this.close() : this.open();
    },

    open() {
        this.isOpen = true;
        const panel = document.getElementById('notification-panel');
        const overlay = document.getElementById('notification-overlay');
        if (panel) panel.classList.add('open');
        if (overlay) overlay.classList.add('open');
    },

    close() {
        this.isOpen = false;
        const panel = document.getElementById('notification-panel');
        const overlay = document.getElementById('notification-overlay');
        if (panel) panel.classList.remove('open');
        if (overlay) overlay.classList.remove('open');
    },

    async fetchEvents() {
        try {
            const resp = await fetch('/api/events');
            if (!resp.ok) return;
            const data = await resp.json();
            this.events = data.events || [];
            this.render();
            this.updateBadge();
        } catch (e) {
            console.warn('[Notifications] fetch failed:', e);
        }
    },

    updateBadge() {
        const badge = document.getElementById('notification-badge');
        if (!badge) return;
        const critical = this.events.filter(e => e.level === 'critical').length;
        if (critical > 0) {
            badge.textContent = critical > 9 ? '9+' : critical;
            badge.style.display = 'flex';
        } else {
            badge.style.display = 'none';
        }
    },

    render() {
        const body = document.getElementById('notification-body');
        if (!body) return;
        if (this.events.length === 0) {
            body.innerHTML = '<div class="empty-state" style="padding:20px;">暂无事件</div>';
            return;
        }
        body.innerHTML = this.events.map(ev => {
            const levelClass = ev.level === 'critical' ? 'critical' : ev.level === 'success' ? 'success' : ev.level === 'warning' ? 'warning' : '';
            return `<div class="notification-item ${levelClass}">
                <div class="event-time">${App.formatTime(ev.time)}</div>
                <div class="event-msg">${ev.message}</div>
            </div>`;
        }).join('');
    }
};

// ── Auto Refresh ─────────────────────────────────────────────────
const AutoRefresh = {
    interval: null,
    active: false,
    callback: null,
    intervalMs: 30000,

    init(fetchCallback) {
        this.callback = fetchCallback;
        const toggle = document.getElementById('auto-refresh-toggle');
        if (!toggle) return;

        // Load saved preference
        const saved = localStorage.getItem('auto-refresh-' + window.location.pathname);
        if (saved === 'true') {
            this.start();
            toggle.classList.add('active');
        }

        toggle.addEventListener('click', () => {
            if (this.active) {
                this.stop();
                toggle.classList.remove('active');
            } else {
                this.start();
                toggle.classList.add('active');
            }
            localStorage.setItem('auto-refresh-' + window.location.pathname, this.active);
        });
    },

    start() {
        this.active = true;
        if (this.interval) clearInterval(this.interval);
        this.interval = setInterval(() => {
            if (this.callback) this.callback();
        }, this.intervalMs);
    },

    stop() {
        this.active = false;
        if (this.interval) {
            clearInterval(this.interval);
            this.interval = null;
        }
    }
};

// ── Mobile Tab Bar Active State ──────────────────────────────────
function initMobileTabBar() {
    const path = window.location.pathname;
    document.querySelectorAll('.mobile-tab-bar a').forEach(a => {
        if (a.getAttribute('href') === path) {
            a.classList.add('active');
        }
    });
}

// ── Global Init ──────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    ThemeManager.init();
    NotificationPanel.init();
    initMobileTabBar();

    // Set active nav link
    const path = window.location.pathname;
    document.querySelectorAll('.nav a').forEach(a => {
        if (a.getAttribute('href') === path) {
            a.classList.add('active');
        }
    });
});
