/* ═══════════════════════════════════════════════════════════════
   Shadow Trading System - P&L Comparison Chart Module
   Shadow vs Live cumulative P&L comparison with hover tooltips
   ═══════════════════════════════════════════════════════════════ */

const CompareChart = {
    canvas: null,
    ctx: null,
    data: null,
    tooltip: null,
    hoverIndex: -1,
    accountId: null,
    padding: { top: 30, right: 70, bottom: 35, left: 55 },

    init(canvasId, tooltipId) {
        this.canvas = document.getElementById(canvasId);
        this.tooltip = document.getElementById(tooltipId);
        if (!this.canvas) return;
        this.ctx = this.canvas.getContext('2d');
        this.canvas.addEventListener('mousemove', (e) => this.handleMouseMove(e));
        this.canvas.addEventListener('mouseleave', () => this.hideTooltip());
        this.canvas.addEventListener('touchmove', (e) => {
            e.preventDefault();
            const touch = e.touches[0];
            const rect = this.canvas.getBoundingClientRect();
            this._handleHoverX(touch.clientX - rect.left);
        });
        this.canvas.addEventListener('touchend', () => this.hideTooltip());
        window.addEventListener('resize', () => { if (this.data) this.draw(); });
        this.fetchData();
        // 每 60s 拉一次对比数据，但只在 tab 可见时；
        // 隐藏 tab / 锁屏 / 切到后台浏览器都不再请求，省后端遍历 trades 的成本。
        setInterval(() => {
            if (document.visibilityState === 'visible') {
                this.fetchData();
            }
        }, 60000);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible' && this.data == null) {
                // 切回来时，如果上次失败 / 没数据，立刻补一次
                this.fetchData();
            }
        });
    },

    setAccount(accountId) {
        this.accountId = accountId || null;
        this.fetchData();
    },

    async fetchData() {
        try {
            const qs = this.accountId ? ('?account_id=' + encodeURIComponent(this.accountId)) : '';
            const resp = await fetch('/api/pnl/compare' + qs);
            if (!resp.ok) return;
            this.data = await resp.json();
            this.updateSummary();
            this.draw();
        } catch (e) {
            console.warn('[CompareChart] fetch failed:', e);
        }
    },

    updateSummary() {
        const el = document.getElementById('compare-summary');
        if (!el || !this.data) return;
        const s = this.data.shadow;
        const l = this.data.live;
        const diff = (l.total_pnl - s.total_pnl).toFixed(2);
        const diffColor = diff >= 0 ? 'green' : 'red';
        el.innerHTML = `
            <span style="color:var(--yellow);">🌑 影子: ${s.total_pnl.toFixed(2)}U (${s.trade_count}笔)</span> &nbsp;|&nbsp;
            <span style="color:var(--green);">⚡ 实盘: ${l.total_pnl.toFixed(2)}U (${l.trade_count}笔)</span> &nbsp;|&nbsp;
            <span class="${diffColor}">差异: ${diff >= 0 ? '+' : ''}${diff}U</span>
        `;
    },

    draw() {
        if (!this.canvas || !this.data || !this.data.dates || !this.data.dates.length) return;
        const { dates, shadow, live } = this.data;
        const n = dates.length;

        const w = this.canvas.parentElement.clientWidth;
        const h = 240;
        this.canvas.width = w;
        this.canvas.height = h;

        const ctx = this.ctx;
        const { top, right, bottom, left } = this.padding;
        const chartW = w - left - right;
        const chartH = h - top - bottom;

        const style = getComputedStyle(document.documentElement);
        const bgColor = style.getPropertyValue('--chart-bg').trim() || '#161b22';
        const gridColor = style.getPropertyValue('--chart-grid').trim() || '#30363d';
        const textColor = style.getPropertyValue('--text-secondary').trim() || '#8b949e';
        const yellowColor = style.getPropertyValue('--yellow').trim() || '#d29922';
        const greenColor = style.getPropertyValue('--green').trim() || '#3fb950';

        ctx.fillStyle = bgColor;
        ctx.fillRect(0, 0, w, h);

        if (n === 0) {
            ctx.fillStyle = textColor;
            ctx.font = '12px sans-serif';
            ctx.fillText('暂无对比数据', w / 2 - 40, h / 2);
            return;
        }

        const allVals = [...shadow.cumulative, ...live.cumulative];
        const minVal = Math.min(...allVals, 0);
        const maxVal = Math.max(...allVals, 0);
        const range = (maxVal - minVal) || 1;

        const toY = (val) => top + chartH - ((val - minVal) / range) * chartH;
        const toX = (i) => n === 1 ? left + chartW / 2 : left + (i / (n - 1)) * chartW;

        // Grid
        ctx.strokeStyle = gridColor;
        ctx.setLineDash([2, 4]);
        ctx.lineWidth = 0.5;
        for (let i = 0; i <= 4; i++) {
            const y = top + (chartH / 4) * i;
            ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w - right, y); ctx.stroke();
        }
        ctx.setLineDash([]);

        // Zero line
        const zeroY = toY(0);
        if (zeroY >= top && zeroY <= top + chartH) {
            ctx.strokeStyle = textColor;
            ctx.setLineDash([4, 4]);
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(left, zeroY); ctx.lineTo(w - right, zeroY); ctx.stroke();
            ctx.setLineDash([]);
        }

        // Shadow curve (yellow/orange)
        ctx.strokeStyle = yellowColor;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        for (let i = 0; i < n; i++) {
            const x = toX(i), y = toY(shadow.cumulative[i]);
            i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        }
        ctx.stroke();

        // Live curve (green)
        ctx.strokeStyle = greenColor;
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        for (let i = 0; i < n; i++) {
            const x = toX(i), y = toY(live.cumulative[i]);
            i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        }
        ctx.stroke();

        // Axis labels
        ctx.fillStyle = textColor;
        ctx.font = '10px sans-serif';
        ctx.fillText(`${maxVal.toFixed(0)}U`, 4, top + 10);
        ctx.fillText(`${minVal.toFixed(0)}U`, 4, h - bottom - 4);
        if (n > 1) {
            ctx.fillText(dates[0], left, h - 6);
            const lastLabel = dates[n - 1];
            ctx.fillText(lastLabel, w - right - ctx.measureText(lastLabel).width, h - 6);
        }

        // Final value labels on right side
        const shadowFinal = shadow.cumulative[n - 1];
        const liveFinal = live.cumulative[n - 1];
        ctx.fillStyle = yellowColor;
        ctx.font = 'bold 11px sans-serif';
        ctx.fillText(`影:${shadowFinal >= 0 ? '+' : ''}${shadowFinal.toFixed(1)}U`, w - right + 4, toY(shadowFinal) + 4);
        ctx.fillStyle = greenColor;
        ctx.fillText(`盘:${liveFinal >= 0 ? '+' : ''}${liveFinal.toFixed(1)}U`, w - right + 4, toY(liveFinal) + 16);

        // Hover crosshair
        if (this.hoverIndex >= 0 && this.hoverIndex < n) {
            const hx = toX(this.hoverIndex);
            ctx.strokeStyle = textColor;
            ctx.setLineDash([2, 2]);
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(hx, top); ctx.lineTo(hx, top + chartH); ctx.stroke();
            ctx.setLineDash([]);

            // Dots
            ctx.beginPath();
            ctx.arc(hx, toY(shadow.cumulative[this.hoverIndex]), 4, 0, Math.PI * 2);
            ctx.fillStyle = yellowColor; ctx.fill();
            ctx.beginPath();
            ctx.arc(hx, toY(live.cumulative[this.hoverIndex]), 4, 0, Math.PI * 2);
            ctx.fillStyle = greenColor; ctx.fill();
        }
    },

    handleMouseMove(e) {
        const rect = this.canvas.getBoundingClientRect();
        this._handleHoverX(e.clientX - rect.left);
    },

    _handleHoverX(mouseX) {
        if (!this.data || !this.data.dates.length) return;
        const n = this.data.dates.length;
        const { left, right } = this.padding;
        const chartW = this.canvas.width - left - right;
        const relX = mouseX - left;
        if (relX < 0 || relX > chartW) { this.hideTooltip(); return; }
        const idx = Math.round((relX / chartW) * (n - 1));
        if (idx < 0 || idx >= n) { this.hideTooltip(); return; }
        this.hoverIndex = idx;
        this.draw();
        this.showTooltip(idx, mouseX);
    },

    showTooltip(idx, mouseX) {
        if (!this.tooltip || !this.data) return;
        const { dates, shadow, live } = this.data;
        const sv = shadow.cumulative[idx], lv = live.cumulative[idx];
        const diff = (lv - sv).toFixed(2);
        this.tooltip.innerHTML = `
            <div><strong>${dates[idx]}</strong></div>
            <div style="color:var(--yellow);">影子: ${sv >= 0 ? '+' : ''}${sv.toFixed(2)}U</div>
            <div style="color:var(--green);">实盘: ${lv >= 0 ? '+' : ''}${lv.toFixed(2)}U</div>
            <div>差: ${diff >= 0 ? '+' : ''}${diff}U</div>
        `;
        this.tooltip.style.display = 'block';
        const tooltipW = this.tooltip.offsetWidth;
        let tooltipX = mouseX + 10;
        if (tooltipX + tooltipW > this.canvas.width) tooltipX = mouseX - tooltipW - 10;
        this.tooltip.style.left = tooltipX + 'px';
        this.tooltip.style.top = '10px';
    },

    hideTooltip() {
        this.hoverIndex = -1;
        if (this.tooltip) this.tooltip.style.display = 'none';
        if (this.data) this.draw();
    }
};
