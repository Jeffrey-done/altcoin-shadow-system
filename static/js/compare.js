// compare.js — 影子 vs 实盘对比图
window.CompareChart = {
  canvas: null,
  ctx: null,
  tooltip: null,
  data: null,
  padding: 40,
  currentAccountId: 'acc_main',

  init: function(canvasId, tooltipId) {
    this.canvas = document.getElementById(canvasId);
    this.tooltip = document.getElementById(tooltipId);
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext('2d');

    // Register listeners
    this.canvas.addEventListener('mousemove', (e) => this.handleMouseMove(e));
    this.canvas.addEventListener('mouseleave', () => this.handleMouseLeave());

    // Fetch initial comparative dataset
    this.fetchData();
  },

  setAccount: function(accountId) {
    this.currentAccountId = accountId;
    this.fetchData();
  },

  fetchData: function() {
    fetch(`/api/pnl/compare?account_id=${this.currentAccountId}`)
      .then(res => res.json())
      .then(data => {
        this.data = data;
        this.draw();
      })
      .catch(err => {
        console.error("Error loading comparison data:", err);
        // Fallback or empty draw
        this.draw();
      });
  },

  draw: function() {
    const ctx = this.ctx;
    const canvas = this.canvas;
    const data = this.data;
    const padding = this.padding;

    // High resolution display logic
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
      ctx.fillText('暂无对照曲线数据', w/2, h/2);
      return;
    }

    const dates = data.dates;
    const shadow = data.shadow_pnl || (data.shadow && data.shadow.cumulative) || [];
    const real = data.real_pnl || (data.live && data.live.cumulative) || [];

    const xRange = w - padding * 2;
    const yRange = h - padding * 2;

    // Find scale
    const allVal = shadow.concat(real);
    const maxVal = Math.max(...allVal, 10);
    const minVal = Math.min(...allVal, -10);
    const range = (maxVal - minVal) || 10;

    this.getX = (index) => padding + (xRange / (dates.length - 1)) * index;
    this.getY = (val) => h - padding - ((val - minVal) / range) * yRange;

    // 1. Gridlines and labels
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.04)';
    ctx.lineWidth = 1;
    ctx.fillStyle = 'var(--text-muted)';
    ctx.font = '9px JetBrains Mono';
    ctx.textAlign = 'right';

    const numTicks = 5;
    for (let i = 0; i < numTicks; i++) {
      const val = maxVal - (range * i / (numTicks - 1));
      const y = this.getY(val);
      
      ctx.beginPath();
      ctx.moveTo(padding, y);
      ctx.lineTo(w - padding, y);
      ctx.stroke();
      
      ctx.fillText(val.toFixed(1) + 'U', padding - 10, y + 3);
    }

    // Baseline (Y=0)
    const yZero = this.getY(0);
    ctx.strokeStyle = 'rgba(255,255,255,0.15)';
    ctx.beginPath();
    ctx.moveTo(padding, yZero);
    ctx.lineTo(w - padding, yZero);
    ctx.stroke();

    // 2. Dates X-axis text
    ctx.textAlign = 'center';
    ctx.font = '9px Inter';
    const labelStep = Math.max(1, Math.floor(dates.length / 5));
    dates.forEach((date, i) => {
      if (i % labelStep === 0 || i === dates.length - 1) {
        ctx.fillText(date, this.getX(i), h - 12);
      }
    });

    // 3. Draw Shadow PnL Curve (Purple)
    ctx.beginPath();
    dates.forEach((date, i) => {
      const x = this.getX(i);
      const y = this.getY(shadow[i] || 0);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = '#a855f7'; // Purple --purple
    ctx.lineWidth = 2;
    ctx.stroke();

    // 4. Draw Real PnL Curve (Green)
    ctx.beginPath();
    dates.forEach((date, i) => {
      const x = this.getX(i);
      const y = this.getY(real[i] || 0);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = '#10b981'; // Success green --success
    ctx.lineWidth = 2;
    ctx.stroke();

    // 5. Draw small indicators circles
    dates.forEach((date, i) => {
      const x = this.getX(i);
      
      // Shadow dot
      ctx.beginPath();
      ctx.arc(x, this.getY(shadow[i] || 0), 2.5, 0, Math.PI * 2);
      ctx.fillStyle = '#a855f7';
      ctx.fill();

      // Real dot
      ctx.beginPath();
      ctx.arc(x, this.getY(real[i] || 0), 2.5, 0, Math.PI * 2);
      ctx.fillStyle = '#10b981';
      ctx.fill();
    });
  },

  handleMouseMove: function(e) {
    if (!this.data || !this.data.dates || this.data.dates.length === 0) return;

    const rect = this.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;

    const dates = this.data.dates;
    const xRange = rect.width - this.padding * 2;

    let minDiff = Infinity;
    let index = 0;

    dates.forEach((date, i) => {
      const px = this.getX(i);
      const diff = Math.abs(px - mx);
      if (diff < minDiff) {
        minDiff = diff;
        index = i;
      }
    });

    const px = this.getX(index);
    const shadowVal = (this.data.shadow_pnl || (this.data.shadow && this.data.shadow.cumulative) || [])[index] || 0;
    const realVal = (this.data.real_pnl || (this.data.live && this.data.live.cumulative) || [])[index] || 0;

    this.draw();

    const ctx = this.ctx;
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = 'rgba(255,255,255,0.15)';
    ctx.beginPath();
    ctx.moveTo(px, this.padding);
    ctx.lineTo(px, rect.height - this.padding);
    ctx.stroke();
    ctx.setLineDash([]);

    // Hover dots
    ctx.beginPath();
    ctx.arc(px, this.getY(shadowVal), 4, 0, Math.PI * 2);
    ctx.fillStyle = '#a855f7';
    ctx.stroke();
    ctx.fill();

    ctx.beginPath();
    ctx.arc(px, this.getY(realVal), 4, 0, Math.PI * 2);
    ctx.fillStyle = '#10b981';
    ctx.stroke();
    ctx.fill();

    if (this.tooltip) {
      this.tooltip.style.opacity = '1';
      this.tooltip.style.left = `${px + 15}px`;
      this.tooltip.style.top = `${this.getY(shadowVal) - 20}px`;

      this.tooltip.innerHTML = `
        <div style="font-weight:700; color:var(--text-muted); margin-bottom:4px;">Date: ${dates[index]}</div>
        <div>影子(做空): <span style="font-weight:bold; color:var(--purple);">${shadowVal >=0 ? '+' : ''}${shadowVal.toFixed(2)} U</span></div>
        <div>实盘(做多): <span style="font-weight:bold; color:var(--green);">${realVal >=0 ? '+' : ''}${realVal.toFixed(2)} U</span></div>
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
