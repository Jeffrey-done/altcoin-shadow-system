/* ═══════════════════════════════════════════════════════════════
   Shadow Trading System - Position Table Module
   Sorting, danger highlight, price animations, TP progress
   ═══════════════════════════════════════════════════════════════ */

const PositionTable = {
    sortState: {}, // { tableId: { column: '', direction: 'asc'|'desc' } }
    previousPrices: {}, // { symbol: price }

    // ── Render Position Table ────────────────────────────────────
    render(trades, tbodyId, emptyId, direction, configData) {
        const tbody = document.getElementById(tbodyId);
        const empty = document.getElementById(emptyId);
        if (!trades || trades.length === 0) {
            if (tbody) tbody.innerHTML = '';
            if (empty) empty.style.display = 'block';
            return;
        }
        if (empty) empty.style.display = 'none';

        // Apply sorting if set
        const sortInfo = this.sortState[tbodyId];
        if (sortInfo && sortInfo.column) {
            trades = this.sortTrades(trades, sortInfo.column, sortInfo.direction, direction);
        }

        const hardStopPct = configData?.hard_stop_pct || 5;

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
            const stake = t.stake_remaining || t.stake || 100;
            const pnlU = stake * leverage * pnlPct / 100;

            // Hold time
            const openedAt = t.opened_at ? new Date(t.opened_at) : null;
            const holdHours = openedAt ? ((Date.now() - openedAt.getTime()) / 3600000).toFixed(1) : '--';

            // Calculate distance to effective stop (use trail_stop_price if active, else hard stop)
            let effectiveStopPrice, distToStop, stopLabel;
            const trailStop = t.trail_stop_price || 0;
            const tp1Active = t.tp1_triggered;

            if (trailStop > 0 && (tp1Active || (t.best_pnl_pct || 0) >= (configData?.trail_activate_pct || 3))) {
                // Use trail/breakeven stop (more restrictive after TP1)
                effectiveStopPrice = trailStop;
                stopLabel = tp1Active ? '保本止损' : '移动止损';
                if (direction === 'SHORT') {
                    distToStop = entry > 0 ? ((effectiveStopPrice - cur) / cur * 100) : 999;
                } else {
                    distToStop = entry > 0 ? ((cur - effectiveStopPrice) / cur * 100) : 999;
                }
            } else {
                // Use hard stop
                if (direction === 'SHORT') {
                    effectiveStopPrice = entry * (1 + hardStopPct / 100);
                    distToStop = entry > 0 ? ((effectiveStopPrice - cur) / cur * 100) : 999;
                } else {
                    effectiveStopPrice = entry * (1 - hardStopPct / 100);
                    distToStop = entry > 0 ? ((cur - effectiveStopPrice) / cur * 100) : 999;
                }
                stopLabel = '距止损';
            }

            // Danger row if within 1% of hard stop
            const isDanger = distToStop <= 1.0;
            const dangerClass = isDanger ? 'danger-row' : '';

            // TP progress bar: show TP1 progress before trigger, TP2 progress after
            let tp1Progress = '';
            if (t.tp1_triggered) {
                // TP1 already triggered → show progress towards TP2
                const tp2Price = t.take_profit_2 || entry * 0.92;
                if (direction === 'SHORT' && entry > 0) {
                    const totalDist = entry - tp2Price;
                    const currentDist = entry - cur;
                    const pctToTP2 = Math.min(100, Math.max(0, (currentDist / totalDist) * 100));
                    const remainPct = (100 - pctToTP2).toFixed(1);
                    tp1Progress = `<div class="tp-progress"><div class="tp-progress-fill tp2" style="width:${pctToTP2}%;background:var(--success)"></div></div>
                        <span style="font-size:0.6rem;color:var(--text-secondary);">距TP2: ${remainPct}%</span>`;
                } else if (direction === 'LONG' && entry > 0) {
                    const totalDist = tp2Price - entry;
                    const currentDist = cur - entry;
                    const pctToTP2 = Math.min(100, Math.max(0, (currentDist / totalDist) * 100));
                    const remainPct = (100 - pctToTP2).toFixed(1);
                    tp1Progress = `<div class="tp-progress"><div class="tp-progress-fill tp2" style="width:${pctToTP2}%;background:var(--success)"></div></div>
                        <span style="font-size:0.6rem;color:var(--text-secondary);">距TP2: ${remainPct}%</span>`;
                }
            } else if (direction === 'SHORT' && entry > 0) {
                const tp1Price = t.take_profit_1 || entry * 0.95;
                const totalDist = entry - tp1Price;
                const currentDist = entry - cur;
                const pctToTP1 = Math.min(100, Math.max(0, (currentDist / totalDist) * 100));
                const remainPct = (100 - pctToTP1).toFixed(1);
                tp1Progress = `<div class="tp-progress"><div class="tp-progress-fill" style="width:${pctToTP1}%"></div></div>
                    <span style="font-size:0.6rem;color:var(--text-secondary);">距TP1: ${remainPct}%</span>`;
            } else if (direction === 'LONG' && entry > 0) {
                const tp1Price = t.take_profit_1 || entry * 1.05;
                const totalDist = tp1Price - entry;
                const currentDist = cur - entry;
                const pctToTP1 = Math.min(100, Math.max(0, (currentDist / totalDist) * 100));
                const remainPct = (100 - pctToTP1).toFixed(1);
                tp1Progress = `<div class="tp-progress"><div class="tp-progress-fill" style="width:${pctToTP1}%"></div></div>
                    <span style="font-size:0.6rem;color:var(--text-secondary);">距TP1: ${remainPct}%</span>`;
            }

            // Price animation class
            const prevPrice = this.previousPrices[t.symbol];
            let priceClass = '';
            let priceArrow = '';
            if (prevPrice !== undefined && prevPrice !== cur) {
                if (cur > prevPrice) {
                    priceClass = 'price-up';
                    priceArrow = '<span class="price-arrow up">↑</span>';
                } else {
                    priceClass = 'price-down';
                    priceArrow = '<span class="price-arrow down">↓</span>';
                }
            }
            // Update previous price
            this.previousPrices[t.symbol] = cur;

            const protectStage = t.protect_stage ? `<span class="badge badge-info">${t.protect_stage}</span>` : '';
            const stopPx = (t.protect_stage === 'stage2' && t.trail_stop_price) ? Number(t.trail_stop_price) : Number(t.hard_stop_price || 0);
            const tpPx = (t.protect_stage === 'stage2') ? Number(t.take_profit_2 || 0) : Number(t.take_profit_1 || 0);
            const protectStop = t.protect_stop_algo_id
                ? `<span class="badge" style="background:rgba(59,130,246,.15);color:var(--blue);border:1px solid rgba(59,130,246,.25);">SL#${String(t.protect_stop_algo_id).slice(-4)} @ ${stopPx > 0 ? stopPx.toFixed(5) : '--'}</span>`
                : '';
            const protectTp = t.protect_tp_algo_id
                ? `<span class="badge" style="background:rgba(16,185,129,.15);color:var(--success);border:1px solid rgba(16,185,129,.25);">TP#${String(t.protect_tp_algo_id).slice(-4)} @ ${tpPx > 0 ? tpPx.toFixed(5) : '--'}</span>`
                : '';
            const extra = direction === 'SHORT'
                ? (t.tp1_triggered ? '<span class="badge badge-ok">TP1✓</span>' : '')
                : (t.strategy || '');

            const stopIndicator = `<span class="stop-distance">${stopLabel} ${distToStop.toFixed(1)}%</span>`;
            const protectLine = (protectStage || protectStop || protectTp) ? `<div style="margin-top:4px;display:flex;gap:4px;flex-wrap:wrap;">${protectStage}${protectStop}${protectTp}</div>` : '';

            return `<tr class="${dangerClass}" data-symbol="${t.symbol}" data-pnl-pct="${pnlPct.toFixed(2)}" data-pnl-u="${pnlU.toFixed(2)}" data-hold="${holdHours}">
                <td><b>${t.symbol}</b>${stopIndicator}${protectLine}</td>
                <td>${entry.toFixed(6)}</td>
                <td class="${priceClass}">${cur.toFixed(6)}${priceArrow}</td>
                <td>${App.fmtPct(pnlPct)}</td>
                <td>${App.fmtPnl(pnlU)}</td>
                <td>${holdHours}h</td>
                <td>${extra}${tp1Progress}</td>
            </tr>`;
        }).join('');
    },

    // ── Sort Trades ──────────────────────────────────────────────
    sortTrades(trades, column, direction, tradeDirection) {
        const sorted = [...trades];
        sorted.sort((a, b) => {
            let valA, valB;
            switch (column) {
                case 'pnl_pct': {
                    const entryA = a.entry_price || 0;
                    const curA = a.current_price || entryA;
                    const entryB = b.entry_price || 0;
                    const curB = b.current_price || entryB;
                    if (tradeDirection === 'SHORT') {
                        valA = entryA > 0 ? ((entryA - curA) / entryA * 100) : 0;
                        valB = entryB > 0 ? ((entryB - curB) / entryB * 100) : 0;
                    } else {
                        valA = entryA > 0 ? ((curA - entryA) / entryA * 100) : 0;
                        valB = entryB > 0 ? ((curB - entryB) / entryB * 100) : 0;
                    }
                    break;
                }
                case 'pnl_u': {
                    const entryA = a.entry_price || 0;
                    const curA = a.current_price || entryA;
                    const entryB = b.entry_price || 0;
                    const curB = b.current_price || entryB;
                    const leverageA = a.leverage || 10;
                    const stakeA = a.stake_remaining || a.stake || 100;
                    const leverageB = b.leverage || 10;
                    const stakeB = b.stake_remaining || b.stake || 100;
                    let pctA, pctB;
                    if (tradeDirection === 'SHORT') {
                        pctA = entryA > 0 ? ((entryA - curA) / entryA * 100) : 0;
                        pctB = entryB > 0 ? ((entryB - curB) / entryB * 100) : 0;
                    } else {
                        pctA = entryA > 0 ? ((curA - entryA) / entryA * 100) : 0;
                        pctB = entryB > 0 ? ((curB - entryB) / entryB * 100) : 0;
                    }
                    valA = stakeA * leverageA * pctA / 100;
                    valB = stakeB * leverageB * pctB / 100;
                    break;
                }
                case 'hold_time': {
                    const openA = a.opened_at ? new Date(a.opened_at).getTime() : Date.now();
                    const openB = b.opened_at ? new Date(b.opened_at).getTime() : Date.now();
                    valA = Date.now() - openA;
                    valB = Date.now() - openB;
                    break;
                }
                default:
                    valA = 0; valB = 0;
            }
            return direction === 'asc' ? valA - valB : valB - valA;
        });
        return sorted;
    },

    // ── Init Sort Headers ────────────────────────────────────────
    initSortHeaders(tableContainerId, tbodyId, rerenderFn) {
        const container = document.getElementById(tableContainerId);
        if (!container) return;

        container.querySelectorAll('th.sortable').forEach(th => {
            th.addEventListener('click', () => {
                const col = th.dataset.sort;
                const current = this.sortState[tbodyId] || { column: '', direction: 'desc' };

                if (current.column === col) {
                    current.direction = current.direction === 'desc' ? 'asc' : 'desc';
                } else {
                    current.column = col;
                    current.direction = 'desc';
                }
                this.sortState[tbodyId] = current;

                // Update header classes
                container.querySelectorAll('th.sortable').forEach(h => {
                    h.classList.remove('sort-asc', 'sort-desc');
                });
                th.classList.add(current.direction === 'asc' ? 'sort-asc' : 'sort-desc');

                // Re-render
                if (rerenderFn) rerenderFn();
            });
        });
    },

    // ── Update Single Price (from WebSocket) ─────────────────────
    updatePrice(symbol, price, lastData) {
        // Update all position tables
        document.querySelectorAll(`tr[data-symbol="${symbol}"]`).forEach(row => {
            const cells = row.querySelectorAll('td');
            if (cells.length < 5) return;

            const entryPrice = parseFloat(cells[1].textContent);
            if (!entryPrice || entryPrice <= 0) return;

            const oldPrice = this.previousPrices[symbol];
            let priceClass = '';
            let priceArrow = '';
            if (oldPrice !== undefined && oldPrice !== price) {
                if (price > oldPrice) {
                    priceClass = 'price-up';
                    priceArrow = '<span class="price-arrow up">↑</span>';
                } else {
                    priceClass = 'price-down';
                    priceArrow = '<span class="price-arrow down">↓</span>';
                }
            }
            this.previousPrices[symbol] = price;

            // Update price cell with animation
            cells[2].className = priceClass;
            cells[2].innerHTML = `${price.toFixed(6)}${priceArrow}`;

            // Determine direction
            const isShort = row.closest('[data-direction="SHORT"]') !== null ||
                           row.closest('#short-positions-table') !== null;
            let pnlPct;
            if (isShort) {
                pnlPct = (entryPrice - price) / entryPrice * 100;
            } else {
                pnlPct = (price - entryPrice) / entryPrice * 100;
            }

            // Update PnL%
            cells[3].innerHTML = App.fmtPct(pnlPct);

            // Update PnL U
            const tradeList = isShort
                ? (lastData?.short_trades?.open || [])
                : (lastData?.long_trades?.open || []);
            const trade = tradeList.find(t => t.symbol === symbol);
            const stake = trade ? (trade.stake_remaining || trade.stake || 100) : 100;
            const leverage = trade ? (trade.leverage || 10) : 10;
            const pnlU = stake * leverage * pnlPct / 100;
            cells[4].innerHTML = App.fmtPnl(pnlU);
        });
    }
};
