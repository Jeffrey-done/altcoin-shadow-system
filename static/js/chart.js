// chart.js — PnL 走势 Canvas 图表模块
window.PnlChart = {
  canvas: null,
  ctx: null,
  tooltip: null,
  data: null,
  padding: 40,

  init: function(canvasId, tooltipId) {
    this.canvas = document.getElementById(canvasId);
    this.tooltip = document.getElementById(tooltipId);
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext('2d');
    
    // Mouse hover trackers
    this.canvas.addEventListener('mousemove', (e) => this.handleMouseMove(e));
    this.canvas.addEventListener('mouseleave', () => this.handleMouseLeave());
  },

  update: function(pnlChartData) {
    if (!this.canvas || !pnlChartData) return;
    this.data = pnlChartData;
    this.draw();
  },

  draw: function() {
    const ctx = this.ctx;
    const canvas = this.canvas;
    const data = this.data;
    const padding = this.padding;

    // HD scale handling
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = rect.height;

    ctx.clearRect(0, 0, w, h);

    if (!data || !data.dates || data.dates.length === 0) {
      ctx.fillStyle = 'var(--text-muted)';
      ctx.font = '12px Inter';
      ctx.textAlign = 'center';
      ctx.fillText('暂无 PnL 数据', w/2, h/2);
      return;
    }

    const dates = data.dates;
    const daily = data.daily_pnl || [];
    const cumulative = data.cumulative || [];

    const xRange = w - padding * 2;
    const yRange = h - padding * 2;

    // Scale calculation
    const maxCum = Math.max(...cumulative, 10);
    const minCum = Math.min(...cumulative, -10);
    const maxDaily = Math.max(...daily, 5);
    const minDaily = Math.min(...daily, -5);

    // Common boundaries
    const totalMax = Math.max(maxCum, maxDaily);
    const totalMin = Math.min(minCum, minDaily);
    const range = (totalMax - totalMin) || 10;

    // Helper functions for positions
    this.getX = (index) => padding + (xRange / (dates.length - 1)) * index;
    this.getY = (val) => h - padding - ((val - totalMin) / range) * yRange;

    // 1. Gridlines and horizontal axes labels
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.04)';
    ctx.lineWidth = 1;
    ctx.fillStyle = 'var(--text-muted)';
    ctx.font = '9px JetBrains Mono';
    ctx.textAlign = 'right';

    const numTicks = 5;
    for (let i = 0; i < numTicks; i++) {
      const val = totalMax - (range * i / (numTicks - 1));
      const y = this.getY(val);
      
      ctx.beginPath();
      ctx.moveTo(padding, y);
      ctx.lineTo(w - padding, y);
      ctx.stroke();
      
      ctx.fillText(val.toFixed(1) + 'U', padding - 10, y + 3);
    }

    // Baseline (Y=0)
    const yZero = this.getY(0);
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)';
    ctx.beginPath();
    ctx.moveTo(padding, yZero);
    ctx.lineTo(w - padding, yZero);
    ctx.stroke();

    // 2. Dates x-coordinates labels
    ctx.textAlign = 'center';
    ctx.font = '9px Inter';
    const labelStep = Math.max(1, Math.floor(dates.length / 5));
    dates.forEach((date, i) => {
      if (i % labelStep === 0 || i === dates.length - 1) {
        ctx.fillText(date, this.getX(i), h - 12);
      }
    });

    // 3. Draw Daily PnL bars
    const barWidth = Math.max(2, (xRange / dates.length) * 0.4);
    dates.forEach((date, i) => {
      const val = daily[i] || 0;
      const x = this.getX(i);
      const y = this.getY(val);
      
      ctx.fillStyle = val >= 0 ? 'rgba(34, 197, 94, 0.25)' : 'rgba(239, 68, 68, 0.25)';
      ctx.fillRect(x - barWidth/2, Math.min(yZero, y), barWidth, Math.abs(yZero - y));
      ctx.strokeStyle = val >= 0 ? 'rgba(34, 197, 94, 0.4)' : 'rgba(239, 68, 68, 0.4)';
      ctx.lineWidth = 0.5;
      ctx.strokeRect(x - barWidth/2, Math.min(yZero, y), barWidth, Math.abs(yZero - y));
    });

    // 4. Draw Cumulative Area
    ctx.beginPath();
    ctx.moveTo(padding, yZero);
    dates.forEach((date, i) => {
      ctx.lineTo(this.getX(i), this.getY(cumulative[i] || 0));
    });
    ctx.lineTo(this.getX(dates.length - 1), yZero);
    ctx.closePath();

    const gradient = ctx.createLinearGradient(0, padding, 0, h - padding);
    gradient.addColorStop(0, 'rgba(168, 85, 247, 0.18)');
    gradient.addColorStop(1, 'rgba(168, 85, 247, 0.00)');
    ctx.fillStyle = gradient;
    ctx.fill();

    // 5. Draw Cumulative Line
    ctx.beginPath();
    dates.forEach((date, i) => {
      const x = this.getX(i);
      const y = this.getY(cumulative[i] || 0);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = '#a855f7'; // Purple line
    ctx.lineWidth = 2;
    ctx.stroke();

    // 6. Draw dots
    dates.forEach((date, i) => {
      const x = this.getX(i);
      const y = this.getY(cumulative[i] || 0);
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fillStyle = 'var(--bg-card)';
      ctx.fill();
      ctx.strokeStyle = '#a855f7';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    });
  },

  handleMouseMove: function(e) {
    if (!this.data || !this.data.dates || this.data.dates.length === 0) return;
    
    // Get mouse coordinate
    const rect = this.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    const dates = this.data.dates;
    const width = rect.width;
    const xRange = width - this.padding * 2;

    // Find nearest point
    let minDiff = Infinity;
    let nearestIndex = 0;
    
    dates.forEach((date, i) => {
      const px = this.getX(i);
      const diff = Math.abs(px - mx);
      if (diff < minDiff) {
        minDiff = diff;
        nearestIndex = i;
      }
    });

    const nearestX = this.getX(nearestIndex);
    const nearestCum = this.data.cumulative[nearestIndex] || 0;
    const nearestDaily = this.data.daily_pnl[nearestIndex] || 0;
    const nearestY = this.getY(nearestCum);

    // Re-draw chart + guideline
    this.draw();
    
    const ctx = this.ctx;
    ctx.strokeStyle = 'rgba(168, 85, 247, 0.4)';
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(nearestX, this.padding);
    ctx.lineTo(nearestX, rect.height - this.padding);
    ctx.stroke();
    ctx.setLineDash([]); // clear dash

    // Draw localized hover dot
    ctx.beginPath();
    ctx.arc(nearestX, nearestY, 5, 0, Math.PI * 2);
    ctx.fillStyle = '#a855f7';
    ctx.fill();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Show tooltip
    if (this.tooltip) {
      this.tooltip.style.opacity = '1';
      this.tooltip.style.left = `${nearestX + 15}px`;
      this.tooltip.style.top = `${nearestY - 30}px`;
      
      const dailySign = nearestDaily >= 0 ? '+' : '';
      const cumSign = nearestCum >= 0 ? '+' : '';
      
      this.tooltip.innerHTML = `
        <div style="font-weight: 700; color:var(--text-muted); margin-bottom:4px;">Date: ${dates[nearestIndex]}</div>
        <div>日利润: <span style="font-weight:bold; color:${nearestDaily >=0 ? 'var(--green)' : 'var(--red)'}">${dailySign}${nearestDaily.toFixed(2)} U</span></div>
        <div>累计利盈: <span style="font-weight:bold; color:${nearestCum >=0 ? 'var(--green)' : 'var(--red)'}">${cumSign}${nearestCum.toFixed(2)} U</span></div>
      `;
    }
  },

  handleMouseLeave: function() {
    this.draw();
    if (this.tooltip) {
      this.tooltip.style.opacity = '0';
    }
  }
};
