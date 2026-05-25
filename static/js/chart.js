/* ═══════════════════════════════════════════════════════════════
   Shadow Trading System - PnL Chart Module
   Enhanced canvas chart with drawdown region, 回本线, time ranges,
   hover tooltips, and interactive features
   ═══════════════════════════════════════════════════════════════ */

const PnlChart = {
    canvas: null,
    ctx: null,
    chartData: null,
    filteredData: null,
    currentRange: 'all', // '7d', '30d', 'all'
    hoverIndex: -1,
    tooltip: null,
    padding: { top: 30, right: 60, bottom: 35, left: 55 },

    init(canvasId, tooltipId) {
        this.canvas = document.getElementById(canvasId);
        this.tooltip = document.getElementById(tooltipId);
        if (!this.canvas) return;
        this.ctx = this.canvas.getContext('2d');

        // Mouse events for tooltip
        this.canvas.addEventListener('mousemove', (e) => this.handleMouseMove(e));
        this.canvas.addEventListener('mouseleave', () => this.hideTooltip());

        // Touch events
        this.canvas.addEventListener('touchmove', (e) => {
            e.preventDefault();
            const touch = e.touches[0];
            const rect = this.canvas.getBoundingClientRect();
            this.handleHover(touch.clientX - rect.left);
        });
        this.canvas.addEventListener('touchend', () => this.hideTooltip());

        // Time range buttons
        document.querySelectorAll('.chart-range-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.chart-range-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.currentRange = btn.dataset.range;
                this.filterData();
                this.draw();
            });
        });

        // Window resize
        window.addEventListener('resize', () => { if (this.chartData) this.draw(); });
    },

    update(data) {
        if (!data || !data.dates || data.dates.length < 1) return;
        this.chartData = data;
        this.filterData();
        this.draw();
    },

    filterData() {
        if (!this.chartData) return;
        const { dates, daily_pnl, cumulative } = this.chartData;
        const n = dates.length;

        let startIdx = 0;
        if (this.currentRange === '7d') {
            startIdx = Math.max(0, n - 7);
        } else if (this.currentRange === '30d') {
            startIdx = Math.max(0, n - 30);
        }

        this.filteredData = {
            dates: dates.slice(startIdx),
            daily_pnl: daily_pnl.slice(startIdx),
            cumulative: cumulative.slice(startIdx),
        };
    },

    draw() {
        if (!this.canvas || !this.filteredData) return;
        const data = this.filteredData;
        const { dates, daily_pnl, cumulative } = data;
        const n = dates.length;

        const w = this.canvas.parentElement.clientWidth;
        const h = 240;
        this.canvas.width = w;
        this.canvas.height = h;

        const ctx = this.ctx;
        const { top, right, bottom, left } = this.padding;
        const chartW = w - left - right;
        const chartH = h - top - bottom;

        // Get theme colors from CSS variables
        const style = getComputedStyle(document.documentElement);
        const bgColor = style.getPropertyValue('--chart-bg').trim() || '#161b22';
        const gridColor = style.getPropertyValue('--chart-grid').trim() || '#30363d';
        const textColor = style.getPropertyValue('--text-secondary').trim() || '#8b949e';
        const greenColor = style.getPropertyValue('--green').trim() || '#3fb950';
        const redColor = style.getPropertyValue('--red').trim() || '#f85149';

        // Background
        ctx.fillStyle = bgColor;
        ctx.fillRect(0, 0, w, h);

        if (n === 0) return;

        // Single data point
        if (n === 1) {
            const val = cumulative[0] || 0;
            ctx.fillStyle = val >= 0 ? greenColor : redColor;
            ctx.font = 'bold 14px sans-serif';
            ctx.fillText(`${dates[0]}: ${val >= 0 ? '+' : ''}${val.toFixed(2)}U`, left + 20, h / 2 - 10);
            ctx.fillStyle = textColor;
            ctx.font = '11px sans-serif';
            ctx.fillText('（需要更多数据绘制曲线）', left + 20, h / 2 + 15);
            return;
        }

        // Calculate ranges
        const allVals = [...cumulative, ...daily_pnl];
        const minVal = Math.min(...allVals, 0);
        const maxVal = Math.max(...allVals, 0);
        const range = (maxVal - minVal) || 1;

        const toY = (val) => top + chartH - ((val - minVal) / range) * chartH;
        const toX = (i) => left + (i / (n - 1)) * chartW;

        // ── Grid lines ──
        ctx.strokeStyle = gridColor;
        ctx.setLineDash([2, 4]);
        ctx.lineWidth = 0.5;
        const gridLines = 4;
        for (let i = 0; i <= gridLines; i++) {
            const y = top + (chartH / gridLines) * i;
            ctx.beginPath();
            ctx.moveTo(left, y);
            ctx.lineTo(w - right, y);
            ctx.stroke();
        }
        ctx.setLineDash([]);

        // ── 回本线 (Break-even line y=0) ──
        const zeroY = toY(0);
        if (zeroY >= top && zeroY <= top + chartH) {
            ctx.strokeStyle = style.getPropertyValue('--yellow').trim() || '#d29922';
            ctx.setLineDash([6, 3]);
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(left, zeroY);
            ctx.lineTo(w - right, zeroY);
            ctx.stroke();
            ctx.setLineDash([]);

            // Label
            ctx.fillStyle = style.getPropertyValue('--yellow').trim() || '#d29922';
            ctx.font = 'bold 10px sans-serif';
            ctx.fillText('回本线', w - right + 4, zeroY + 3);
        }

        // ── Max Drawdown Region ──
        // Find max drawdown region in cumulative
        let peak = cumulative[0];
        let maxDD = 0;
        let ddStart = 0, ddEnd = 0;
        let currentDDStart = 0;
        for (let i = 0; i < n; i++) {
            if (cumulative[i] > peak) {
                peak = cumulative[i];
                currentDDStart = i;
            }
            const dd = peak - cumulative[i];
            if (dd > maxDD) {
                maxDD = dd;
                ddStart = currentDDStart;
                ddEnd = i;
            }
        }

        if (maxDD > 0 && ddEnd > ddStart) {
            ctx.fillStyle = 'rgba(248, 81, 73, 0.08)';
            ctx.beginPath();
            ctx.moveTo(toX(ddStart), toY(cumulative[ddStart]));
            for (let i = ddStart; i <= ddEnd; i++) {
                ctx.lineTo(toX(i), toY(cumulative[i]));
            }
            ctx.lineTo(toX(ddEnd), toY(cumulative[ddStart]));
            ctx.closePath();
            ctx.fill();

            // Drawdown label
            ctx.fillStyle = redColor;
            ctx.font = '9px sans-serif';
            const ddMid = Math.floor((ddStart + ddEnd) / 2);
            ctx.fillText(`最大回撤 -${maxDD.toFixed(1)}U`, toX(ddMid) - 30, toY(Math.min(...cumulative.slice(ddStart, ddEnd + 1))) + 12);
        }

        // ── Daily PnL Bars ──
        const barWidth = Math.max(2, (chartW / n) * 0.5);
        for (let i = 0; i < n; i++) {
            const x = toX(i);
            const val = daily_pnl[i];
            const barH = Math.abs(val / range) * chartH;
            const y = val >= 0 ? zeroY - barH : zeroY;
            ctx.fillStyle = val >= 0 ? 'rgba(63,185,80,0.35)' : 'rgba(248,81,73,0.35)';
            ctx.fillRect(x - barWidth / 2, y, barWidth, barH || 1);
        }

        // ── Cumulative Line ──
        ctx.strokeStyle = greenColor;
        ctx.lineWidth = 2;
        ctx.beginPath();
        for (let i = 0; i < n; i++) {
            const x = toX(i);
            const y = toY(cumulative[i]);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.stroke();

        // ── Gradient fill under cumulative ──
        const grad = ctx.createLinearGradient(0, top, 0, top + chartH);
        grad.addColorStop(0, 'rgba(63,185,80,0.15)');
        grad.addColorStop(1, 'rgba(63,185,80,0)');
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.moveTo(toX(0), toY(cumulative[0]));
        for (let i = 1; i < n; i++) {
            ctx.lineTo(toX(i), toY(cumulative[i]));
        }
        ctx.lineTo(toX(n - 1), top + chartH);
        ctx.lineTo(toX(0), top + chartH);
        ctx.closePath();
        ctx.fill();

        // ── Axis Labels ──
        ctx.fillStyle = textColor;
        ctx.font = '10px sans-serif';
        ctx.fillText(`${maxVal.toFixed(0)}U`, 4, top + 10);
        ctx.fillText(`${minVal.toFixed(0)}U`, 4, h - bottom - 4);

        // Date labels
        if (n > 1) {
            ctx.fillText(dates[0], left, h - 6);
            const lastLabel = dates[n - 1];
            ctx.fillText(lastLabel, w - right - ctx.measureText(lastLabel).width, h - 6);
            if (n > 4) {
                const midIdx = Math.floor(n / 2);
                ctx.fillText(dates[midIdx], toX(midIdx) - 20, h - 6);
            }
        }

        // Final value label
        const finalCum = cumulative[n - 1];
        ctx.fillStyle = finalCum >= 0 ? greenColor : redColor;
        ctx.font = 'bold 12px sans-serif';
        ctx.fillText(`${finalCum >= 0 ? '+' : ''}${finalCum.toFixed(1)}U`, w - right - 55, top + 12);

        // Hover highlight
        if (this.hoverIndex >= 0 && this.hoverIndex < n) {
            const hx = toX(this.hoverIndex);
            ctx.strokeStyle = textColor;
            ctx.setLineDash([2, 2]);
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(hx, top);
            ctx.lineTo(hx, top + chartH);
            ctx.stroke();
            ctx.setLineDash([]);

            // Dot on cumulative line
            const hy = toY(cumulative[this.hoverIndex]);
            ctx.beginPath();
            ctx.arc(hx, hy, 4, 0, Math.PI * 2);
            ctx.fillStyle = greenColor;
            ctx.fill();
            ctx.strokeStyle = '#fff';
            ctx.lineWidth = 1.5;
            ctx.stroke();
        }
    },

    handleMouseMove(e) {
        const rect = this.canvas.getBoundingClientRect();
        this.handleHover(e.clientX - rect.left);
    },

    handleHover(mouseX) {
        if (!this.filteredData || !this.filteredData.dates.length) return;
        const n = this.filteredData.dates.length;
        const { left, right } = this.padding;
        const chartW = this.canvas.width - left - right;

        // Find nearest index
        const relX = mouseX - left;
        if (relX < 0 || relX > chartW) {
            this.hideTooltip();
            return;
        }
        const idx = Math.round((relX / chartW) * (n - 1));
        if (idx < 0 || idx >= n) {
            this.hideTooltip();
            return;
        }

        this.hoverIndex = idx;
        this.draw();
        this.showTooltip(idx, mouseX);
    },

    showTooltip(idx, mouseX) {
        if (!this.tooltip || !this.filteredData) return;
        const data = this.filteredData;
        const date = data.dates[idx];
        const daily = data.daily_pnl[idx];
        const cum = data.cumulative[idx];

        this.tooltip.innerHTML = `
            <div><strong>${date}</strong></div>
            <div>日盈亏: <span class="${App.pnlColor(daily)}">${daily >= 0 ? '+' : ''}${daily.toFixed(2)}U</span></div>
            <div>累计: <span class="${App.pnlColor(cum)}">${cum >= 0 ? '+' : ''}${cum.toFixed(2)}U</span></div>
        `;
        this.tooltip.style.display = 'block';

        // Position
        const rect = this.canvas.getBoundingClientRect();
        const tooltipW = this.tooltip.offsetWidth;
        let tooltipX = mouseX + 10;
        if (tooltipX + tooltipW > this.canvas.width) {
            tooltipX = mouseX - tooltipW - 10;
        }
        this.tooltip.style.left = tooltipX + 'px';
        this.tooltip.style.top = '10px';
    },

    hideTooltip() {
        this.hoverIndex = -1;
        if (this.tooltip) this.tooltip.style.display = 'none';
        if (this.canvas && this.filteredData) this.draw();
    }
};

// ── Risk Sparkline ───────────────────────────────────────────────
function drawSparkline(canvasId, values, width = 100, height = 24) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || !values || values.length === 0) return;
    const ctx = canvas.getContext('2d');
    canvas.width = width;
    canvas.height = height;

    const n = values.length;
    const maxVal = Math.max(...values.map(Math.abs), 1);
    const padding = 2;
    const chartW = width - padding * 2;
    const chartH = height - padding * 2;

    // Background
    ctx.clearRect(0, 0, width, height);

    // Draw bars
    const barW = Math.max(2, chartW / n - 2);
    for (let i = 0; i < n; i++) {
        const x = padding + (i / n) * chartW;
        const barH = (Math.abs(values[i]) / maxVal) * chartH;
        const y = height - padding - barH;
        ctx.fillStyle = values[i] >= 0 ? 'rgba(248,81,73,0.7)' : 'rgba(63,185,80,0.4)';
        ctx.fillRect(x, y, barW, barH || 1);
    }

    // Zero line
    ctx.strokeStyle = 'rgba(255,255,255,0.2)';
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(0, height - padding);
    ctx.lineTo(width, height - padding);
    ctx.stroke();
}
