// positions.js — 持仓表格渲染模块
window.PositionTable = {
  // Store sorting criteria
  sortField: 'symbol',
  sortAsc: true,
  lastTrades: [],
  config: {},

  render: function(trades, tbodyId, emptyId, direction, configData) {
    const tbody = document.getElementById(tbodyId);
    const emptyEl = document.getElementById(emptyId);
    if (!tbody) return;

    this.config = configData || {};
    let filteredTrades = (trades || []).filter(t => (t.direction || 'SHORT') === direction);
    
    // Cache for WebSocket real-time updates
    this.lastTrades = (this.lastTrades || []).filter(t => (t.direction || 'SHORT') !== direction).concat(filteredTrades);

    if (filteredTrades.length === 0) {
      tbody.innerHTML = '';
      if (emptyEl) emptyEl.style.display = 'flex';
      return;
    }

    if (emptyEl) emptyEl.style.display = 'none';

    // Apply sorting
    filteredTrades.sort((a, b) => {
      let valA = this.getSortValue(a, this.sortField);
      let valB = this.getSortValue(b, this.sortField);
      
      if (typeof valA === 'string') {
        return this.sortAsc ? valA.localeCompare(valB) : valB.localeCompare(valA);
      } else {
        return this.sortAsc ? (valA - valB) : (valB - valA);
      }
    });

    let html = '';
    filteredTrades.forEach(trade => {
      const rowData = this.calculateRowStats(trade);
      const isDanger = rowData.slDistance !== null && rowData.slDistance < 1.0;
      const dangerClass = isDanger ? 'danger-row' : '';

      html += `
        <tr id="pos-row-${trade.symbol.replace('/', '-')}" class="${dangerClass}" data-symbol="${trade.symbol}">
          <td class="font-bold mono" style="font-size:13px;">
            <div style="display: flex; align-items: center; gap: 6px;">
              <i data-lucide="${trade.direction === 'LONG' ? 'arrow-up-right' : 'arrow-down-left'}" class="${trade.direction === 'LONG' ? 'green' : 'red'}" style="width:14px; height:14px;"></i>
              <div>
                <span>${trade.symbol}</span>
                <div style="font-size:9px; font-weight:normal;" class="text-muted">Exchange: ${trade.exchange || 'binance'}</div>
              </div>
            </div>
          </td>
          <td class="mono">
            <div style="font-size:13px; font-weight:800;" class="pnl-u-cell">${window.App.fmtPnl(rowData.pnl_u)}</div>
            <div style="font-size:10px;" class="pnl-pct-cell">${window.App.fmtPct(rowData.pnl_pct)} (${trade.leverage}x)</div>
          </td>
          <td class="mono">
            <div class="text-muted">Entry: ${parseFloat(trade.entry_price || 0).toFixed(6)}</div>
            <div style="font-weight:700; font-size:13px;" class="price-cell shadow-sm-indicator">${parseFloat(trade.current_price || 0).toFixed(6)}</div>
          </td>
          <td class="mono">
            <div>Stake: ${trade.stake_remaining} U</div>
            <div style="font-size:10px;" class="text-muted">Total: ${trade.stake} U</div>
          </td>
          <td>
            <div class="tp-progress-wrapper">
              <div class="tp-progress-labels">
                <span class="tp-stage-name">${rowData.tpLabel}</span>
                <span class="tp-pct-value mono" style="font-weight:600;">${rowData.tpProgress.toFixed(0)}%</span>
              </div>
              <div class="progress-container" style="height: 6px; border-radius: 3px;">
                <div class="progress-bar tp-progress-bar" style="width: ${rowData.tpProgress}%; background-color: ${rowData.tpColor};"></div>
              </div>
              <div style="font-size:9px; margin-top:2px;" class="text-muted mono tp-targets-lbl">Target: ${rowData.tpTarget.toFixed(6)}</div>
            </div>
          </td>
          <td class="mono">
            <div style="font-weight: 600;" class="sl-price-lbl">${rowData.slPrice ? rowData.slPrice.toFixed(6) : '--'}</div>
            <div style="font-size:10px; font-weight: 700;" class="sl-dist-cell ${isDanger ? 'red' : 'text-secondary'}">
              ${rowData.slDistance !== null ? `距止损: ${rowData.slDistance.toFixed(2)}%` : '--'}
            </div>
          </td>
          <td>
            <div style="display:flex; flex-direction:column; gap:2px;">
              <span class="badge ${trade.protect_stage === 'stage2' ? 'badge-purple' : 'badge-blue'}">${trade.protect_stage || 'stage1'}</span>
              <div style="font-size:9px; font-family:var(--font-mono); color: var(--text-muted);" class="algo-ids-lbl">
                SL:${trade.protect_stop_algo_id ? '...' + trade.protect_stop_algo_id.slice(-4) : '--'}
                TP:${trade.protect_tp_algo_id ? '...' + trade.protect_tp_algo_id.slice(-4) : '--'}
              </div>
            </div>
          </td>
        </tr>
      `;
    });

    tbody.innerHTML = html;
    if (typeof lucide !== 'undefined') {
      lucide.createIcons();
    }
  },

  getSortValue: function(trade, field) {
    if (field === 'symbol') return trade.symbol;
    if (field === 'current_price') return trade.current_price;
    if (field === 'pnl_pct') {
      const stats = this.calculateRowStats(trade);
      return stats.pnl_pct;
    }
    return 0;
  },

  calculateRowStats: function(trade) {
    const entry = parseFloat(trade.entry_price || 0);
    const current = parseFloat(trade.current_price || 0);
    const stake_remaining = parseFloat(trade.stake_remaining || 0);
    const leverage = parseFloat(trade.leverage || 1);

    // 1. PnL calculation
    let pnl_pct = 0;
    if (trade.direction === 'SHORT') {
      pnl_pct = (entry - current) / entry * 100;
    } else {
      pnl_pct = (current - entry) / entry * 100;
    }
    const pnl_u = stake_remaining * leverage * pnl_pct / 100;

    // 2. TP Progress bar calculation
    const tp1_pct = parseFloat(this.config.tp1_pct || 5.0);
    const tp2_pct = parseFloat(this.config.tp2_pct || 8.0);
    
    // Explicit take profits from data, otherwise compute from percentages
    const tp1 = parseFloat(trade.take_profit_1) || (trade.direction === 'SHORT' ? entry * (1 - tp1_pct/100) : entry * (1 + tp1_pct/100));
    const tp2 = parseFloat(trade.take_profit_2) || (trade.direction === 'SHORT' ? entry * (1 - tp2_pct/100) : entry * (1 + tp2_pct/100));

    let tpProgress = 0;
    let tpLabel = '距 TP1';
    let tpTarget = tp1;
    let tpColor = 'var(--blue)';

    if (trade.tp1_triggered || (trade.direction === 'SHORT' ? current <= tp1 : current >= tp1)) {
      tpLabel = '距 TP2';
      tpTarget = tp2;
      tpColor = 'var(--success)';
      
      const denominator = trade.direction === 'SHORT' ? (tp1 - tp2) : (tp2 - tp1);
      if (denominator !== 0) {
        tpProgress = (trade.direction === 'SHORT' ? (tp1 - current) : (current - tp1)) / denominator * 100;
      }
    } else {
      const denominator = trade.direction === 'SHORT' ? (entry - tp1) : (tp1 - entry);
      if (denominator !== 0) {
        tpProgress = (trade.direction === 'SHORT' ? (entry - current) : (current - entry)) / denominator * 100;
      }
    }
    // Clamp progress
    tpProgress = Math.max(0, Math.min(100, tpProgress));

    // 3. Stop loss distance
    let slPrice = null;
    if (trade.tp1_triggered || (trade.direction === 'SHORT' ? current <= tp1 : current >= tp1)) {
      slPrice = parseFloat(trade.trail_stop_price) || entry; // default breakeven
    } else {
      slPrice = parseFloat(trade.hard_stop_price) || (trade.direction === 'SHORT' ? entry * 1.05 : entry * 0.95);
    }

    let slDistance = null;
    if (slPrice > 0) {
      if (trade.direction === 'SHORT') {
        slDistance = ((slPrice - current) / current) * 100;
      } else {
        slDistance = ((current - slPrice) / current) * 100;
      }
    }

    return {
      pnl_pct: pnl_pct,
      pnl_u: pnl_u,
      tpProgress: tpProgress,
      tpLabel: tpLabel,
      tpTarget: tpTarget,
      tpColor: tpColor,
      slPrice: slPrice,
      slDistance: slDistance
    };
  },

  initSortHeaders: function(tableContainerId, tbodyId, rerenderFn) {
    const container = document.getElementById(tableContainerId);
    if (!container) return;
    const headers = container.querySelectorAll('th.sortable');
    
    headers.forEach(header => {
      // Draw icon holder
      header.style.position = 'relative';
      header.innerHTML = header.textContent + ' <i class="sort-icon desc" style="border: solid var(--text-muted); border-width: 0 1.5px 1.5px 0; display: inline-block; padding: 2px; transform: rotate(45deg); vertical-align: middle; margin-left: 4px; opacity: 0.3;"></i>';
      
      header.addEventListener('click', () => {
        const field = header.getAttribute('data-sort');
        if (this.sortField === field) {
          this.sortAsc = !this.sortAsc;
        } else {
          this.sortField = field;
          this.sortAsc = true;
        }

        // Active header style sync
        headers.forEach(h => {
          const sIcon = h.querySelector('.sort-icon');
          if (sIcon) sIcon.style.opacity = '0.3';
        });

        const activeIcon = header.querySelector('.sort-icon');
        if (activeIcon) {
          activeIcon.style.opacity = '1.0';
          if (this.sortAsc) {
            activeIcon.style.transform = 'rotate(-135deg)'; // up arrow
          } else {
            activeIcon.style.transform = 'rotate(45deg)'; // down arrow
          }
        }
        
        if (rerenderFn) rerenderFn();
      });
    });
  },

  // Binance WebSocket real-time price tick animation & updater
  updatePrice: function(symbol, price, lastData) {
    const row = document.getElementById(`pos-row-${symbol.replace('/', '-')}`);
    if (!row) return;

    // Retrieve trade original fields from cached array
    const trade = (this.lastTrades || []).find(t => t.symbol === symbol);
    if (!trade) return;

    const oldPrice = parseFloat(trade.current_price || 0);
    const newPrice = parseFloat(price);
    
    // Set updated price
    trade.current_price = newPrice;
    if (lastData && lastData[symbol]) {
      lastData[symbol].current_price = newPrice;
    }

    // Identify flare status class
    const priceCell = row.querySelector('.price-cell');
    if (priceCell) {
      priceCell.textContent = newPrice.toFixed(6);
      
      // Flash animations
      priceCell.classList.remove('price-up', 'price-down');
      void priceCell.offsetWidth; // Reflow trigger
      
      if (newPrice > oldPrice) {
        priceCell.classList.add('price-up');
      } else if (newPrice < oldPrice) {
        priceCell.classList.add('price-down');
      }
    }

    // Core stats recalculation
    const stats = this.calculateRowStats(trade);

    const pnlUCell = row.querySelector('.pnl-u-cell');
    if (pnlUCell) pnlUCell.innerHTML = window.App.fmtPnl(stats.pnl_u);

    const pnlPctCell = row.querySelector('.pnl-pct-cell');
    if (pnlPctCell) pnlPctCell.innerHTML = `${window.App.fmtPct(stats.pnl_pct)} (${trade.leverage}x)`;

    // TP Progress update
    const tpStage = row.querySelector('.tp-stage-name');
    if (tpStage) tpStage.textContent = stats.tpLabel;

    const tpPct = row.querySelector('.tp-pct-value');
    if (tpPct) tpPct.textContent = `${stats.tpProgress.toFixed(0)}%`;

    const tpBar = row.querySelector('.tp-progress-bar');
    if (tpBar) {
      tpBar.style.width = `${stats.tpProgress}%`;
      tpBar.style.backgroundColor = stats.tpColor;
    }

    const tpTargetLabel = row.querySelector('.tp-targets-lbl');
    if (tpTargetLabel) tpTargetLabel.textContent = `Target: ${stats.tpTarget.toFixed(6)}`;

    // Stop loss recalculation
    const slPriceLbl = row.querySelector('.sl-price-lbl');
    if (slPriceLbl) slPriceLbl.textContent = stats.slPrice ? stats.slPrice.toFixed(6) : '--';

    const slDistCell = row.querySelector('.sl-dist-cell');
    if (slDistCell) {
      if (stats.slDistance !== null) {
        slDistCell.textContent = `距止损: ${stats.slDistance.toFixed(2)}%`;
        if (stats.slDistance < 1.0) {
          row.classList.add('danger-row');
          slDistCell.className = 'sl-dist-cell red';
        } else {
          row.classList.remove('danger-row');
          slDistCell.className = 'sl-dist-cell text-secondary';
        }
      } else {
        slDistCell.textContent = '--';
      }
    }
  }
};
