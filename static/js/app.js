// app.js — 全局工具与通用逻辑
window.App = {
  // 格式化盈亏金额
  fmtPnl: function(value) {
    if (value === null || value === undefined) return '<span class="text-muted">--</span>';
    const num = parseFloat(value);
    const cls = num > 0 ? 'green' : (num < 0 ? 'red' : 'text-muted');
    const sign = num > 0 ? '+' : '';
    return `<span class="${cls} font-bold mono">${sign}${num.toFixed(2)} USDT</span>`;
  },

  // 格式化百分比
  fmtPct: function(value) {
    if (value === null || value === undefined) return '--%';
    const num = parseFloat(value);
    const cls = num > 0 ? 'green' : (num < 0 ? 'red' : 'text-muted');
    const sign = num > 0 ? '+' : '';
    return `<span class="${cls} font-bold mono">${sign}${num.toFixed(2)}%</span>`;
  },

  // 返回颜色名
  pnlColor: function(value) {
    if (value === null || value === undefined) return '';
    const num = parseFloat(value);
    return num > 0 ? 'green' : (num < 0 ? 'red' : '');
  },

  // 格式化 ISO 日期时间 "2026-05-25T10:30:00Z" -> "05-25 10:30"
  formatTime: function(isoStr) {
    if (!isoStr) return '--:--';
    try {
      const date = new Date(isoStr);
      const m = String(date.getMonth() + 1).padStart(2, '0');
      const d = String(date.getDate()).padStart(2, '0');
      const h = String(date.getHours()).padStart(2, '0');
      const min = String(date.getMinutes()).padStart(2, '0');
      return `${m}-${d} ${h}:${min}`;
    } catch (e) {
      return '--:--';
    }
  },

  // 获利趋势标识
  trendLabel: function(today, yesterday) {
    const t = parseFloat(today || 0);
    const y = parseFloat(yesterday || 0);
    const diff = t - y;
    const sign = diff > 0 ? '↑' : (diff < 0 ? '↓' : '→');
    const cls = diff > 0 ? 'green' : (diff < 0 ? 'red' : 'text-muted');
    return `<span class="${cls} font-bold mono" style="font-size:10px;">${sign} vs 昨日 (${y > 0 ? '+' : ''}${y.toFixed(2)}U)</span>`;
  },

  // ROI百分比标识
  roiLabel: function(pnl, capital) {
    const p = parseFloat(pnl || 0);
    const c = parseFloat(capital || 1);
    const roi = (p / c) * 100;
    const sign = roi > 0 ? '+' : '';
    const cls = roi > 0 ? 'green' : (roi < 0 ? 'red' : 'text-muted');
    return `<span class="${cls} mono" style="font-size:10px; font-weight:600;">(${sign}${roi.toFixed(2)}%)</span>`;
  }
};

// 绘制迷你折线图函数
window.drawSparkline = function(canvasId, data, width, height) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  
  // Set resolution multiplier
  const dpr = window.devicePixelRatio || 1;
  canvas.width = (width || canvas.clientWidth || 200) * dpr;
  canvas.height = (height || canvas.clientHeight || 40) * dpr;
  ctx.scale(dpr, dpr);
  
  const w = canvas.width / dpr;
  const h = canvas.height / dpr;
  
  ctx.clearRect(0, 0, w, h);
  
  if (!data || data.length < 2) return;
  
  // Find min/max
  let min = Math.min(...data);
  let max = Math.max(...data);
  if (max === min) {
    max += 1;
    min -= 1;
  }
  const range = max - min;
  
  // Draw stroke path
  ctx.beginPath();
  data.forEach((val, index) => {
    const x = (w / (data.length - 1)) * index;
    const y = h - 4 - ((val - min) / range) * (h - 8);
    if (index === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  
  ctx.strokeStyle = '#eab308'; // Sparkline default yellow
  ctx.lineWidth = 1.8;
  ctx.stroke();
  
  // Fill gradient area below path
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  const gradient = ctx.createLinearGradient(0, 0, 0, h);
  gradient.addColorStop(0, 'rgba(234, 179, 8, 0.12)');
  gradient.addColorStop(1, 'rgba(234, 179, 8, 0.0)');
  ctx.fillStyle = gradient;
  ctx.fill();
};

// Universal elements handler (Drawer log, theme setup)
document.addEventListener("DOMContentLoaded", () => {
  
  // Theme Switching Management
  const themeToggle = document.getElementById("theme-toggle");
  const themeIcon = document.getElementById("theme-icon");
  
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("dashboard_theme", theme);
    if (themeIcon) {
      if (theme === "dark") {
        themeIcon.setAttribute("data-lucide", "sun");
      } else {
        themeIcon.setAttribute("data-lucide", "moon");
      }
      if (typeof lucide !== "undefined") {
        lucide.createIcons();
      }
    }
  }

  // Load saved theme
  const savedTheme = localStorage.getItem("dashboard_theme") || "dark";
  applyTheme(savedTheme);

  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") || "dark";
      const next = current === "dark" ? "light" : "dark";
      applyTheme(next);
    });
  }

  // Sliding Drawer Drawer panel triggers
  const notificationToggle = document.getElementById("notification-toggle");
  const notificationClose = document.getElementById("notification-close");
  const notificationPanel = document.getElementById("notification-panel");
  const eventsListContainer = document.getElementById("events-list-container");
  const eventsCountBadge = document.getElementById("events-count-badge");

  if (notificationToggle && notificationPanel) {
    notificationToggle.addEventListener("click", () => {
      notificationPanel.classList.toggle("open");
      loadEventsLog();
    });
  }

  if (notificationClose && notificationPanel) {
    notificationClose.addEventListener("click", () => {
      notificationPanel.classList.remove("open");
    });
  }

  // Close drawer if click outside
  document.addEventListener("click", (e) => {
    if (notificationPanel && notificationPanel.classList.contains("open")) {
      if (!notificationPanel.contains(e.target) && !notificationToggle.contains(e.target)) {
        notificationPanel.classList.remove("open");
      }
    }
  });

  // Fetch /api/events list log
  function loadEventsLog() {
    fetch('/api/events')
      .then(res => res.json())
      .then(data => {
        // Backend returns {events: [...]} wrapper
        const events = data.events || data || [];
        renderEventsList(events);
      })
      .catch(err => console.error("Error loading events logs:", err));
  }

  function renderEventsList(events) {
    if (!eventsListContainer) return;
    
    if (!events || events.length === 0) {
      eventsListContainer.innerHTML = `
        <div class="empty-state">
          <i data-lucide="info" style="width: 24px; height: 24px;" class="empty-state-icon"></i>
          <span>暂无系统实时日志</span>
        </div>`;
      if (eventsCountBadge) eventsCountBadge.textContent = '0';
      if (typeof lucide !== "undefined") lucide.createIcons();
      return;
    }
    
    if (eventsCountBadge) {
      eventsCountBadge.textContent = events.length;
    }

    let html = '';
    events.forEach(ev => {
      let icon = 'info';
      let badgeCls = 'badge-blue';
      if (ev.type === 'open') { icon = 'play-circle'; badgeCls = 'badge-purple'; }
      else if (ev.type === 'close') { icon = 'check-circle2'; badgeCls = 'badge-green'; }
      else if (ev.type === 'pause' || ev.type === 'risk_pause') { icon = 'pause-circle'; badgeCls = 'badge-red'; }
      else if (ev.type === 'error') { icon = 'alert-triangle'; badgeCls = 'badge-red'; }

      html += `
        <div class="notification-item">
          <div class="flex-between">
            <span class="badge ${badgeCls}" style="font-size:9px;">${ev.type.toUpperCase()}</span>
            <span class="mono text-muted" style="font-size:10px;">${window.App.formatTime(ev.time || ev.timestamp)}</span>
          </div>
          <p style="margin-top:6px; font-weight:500;">${ev.message}</p>
          ${ev.symbol ? `<div style="margin-top:4px; font-weight:700; font-family:var(--font-mono); font-size:10px;">${ev.symbol}</div>` : ''}
        </div>`;
    });

    eventsListContainer.innerHTML = html;
    if (typeof lucide !== "undefined") {
      lucide.createIcons();
    }
  }

  // Load events once on init
  setTimeout(loadEventsLog, 500);
});
