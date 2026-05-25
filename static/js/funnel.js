// funnel.js — 候选池过滤漏斗模块
window.CandidateFunnel = {
  render: function(candidates) {
    const list = candidates || [];
    
    // 1. Render Table Tbody
    const tbody = document.getElementById('candidates-tbody');
    const badgeCount = document.getElementById('candidates-count');
    if (badgeCount) {
      badgeCount.textContent = list.length + ' 个品种';
    }

    if (tbody) {
      if (list.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-muted" style="text-align:center;">暂无做空预选品种</td></tr>';
      } else {
        let tbodyHtml = '';
        list.forEach(item => {
          let scoreColor = 'var(--text-muted)';
          let scoreBg = 'rgba(255,255,255,0.03)';
          if (item.yao_score >= 4.0) { scoreColor = 'var(--red)'; scoreBg = 'rgba(239,68,68,0.12)'; }
          else if (item.yao_score >= 2.0) { scoreColor = 'var(--yellow)'; scoreBg = 'rgba(234,179,8,0.12)'; }

          tbodyHtml += `
            <tr class="${item.triggered ? 'danger-row' : ''}">
              <td class="font-bold mono">${item.symbol}</td>
              <td class="mono font-bold" style="color: ${item.rsi_1d > 75 ? 'var(--red)' : 'var(--text)'}">${item.rsi_1d}</td>
              <td class="mono red font-bold">+${parseFloat(item.pct24h).toFixed(1)}%</td>
              <td class="mono text-muted">+${item.oi_change}%</td>
              <td>
                <span class="badge" style="background-color: ${scoreBg}; color: ${scoreColor}; font-weight: 700; border: 1px solid rgba(255,255,255,0.03);">
                  YAO: ${parseFloat(item.yao_score).toFixed(1)}
                </span>
              </td>
            </tr>
          `;
        });
        tbody.innerHTML = tbodyHtml;
      }
    }

    // 2. Render progressive horizontal cascade funnel
    const funnelContainer = document.getElementById('funnel-container');
    if (!funnelContainer) return;

    // Filter rules per funnel stages
    const s1_all = list.length; // Total candidates scanned
    const s2_overbought = list.filter(item => item.rsi_1d >= 70).length; // Overbought RSI
    const s3_vol_momentum = list.filter(item => item.rsi_1d >= 70 && item.oi_change >= 20).length; // RSI + Vol active
    const s4_triggered = list.filter(item => item.yao_score >= 3.0 || item.triggered).length; // Highest alerts

    const stages = [
      { name: '1. 代币高位扫描池', count: s1_all, widthPct: 100, color: 'linear-gradient(90deg, #3b82f644, #3b82f688)' },
      { name: '2. 技术面共振超买', count: s2_overbought, widthPct: 85, color: 'linear-gradient(90deg, #eab30844, #eab30888)' },
      { name: '3. 持仓金流异暴流入', count: s3_vol_momentum, widthPct: 70, color: 'linear-gradient(90deg, #a855f744, #a855f788)' },
      { name: '4. 触发对冲空刀极值点', count: s4_triggered, widthPct: 50, color: 'linear-gradient(90deg, #ef444444, #ef444488)' },
    ];

    let funnelHtml = '';
    stages.forEach((stage, index) => {
      // Limit widths nicely
      const barWidth = stage.count > 0 ? stage.widthPct : 10;
      funnelHtml += `
        <div class="funnel-stage" title="${stage.name}">
          <div class="funnel-bar" style="width: ${barWidth}%; background: ${stage.color};"></div>
          <div class="funnel-content">
            <span style="color: var(--text-secondary);">${stage.name}</span>
            <span class="mono" style="font-weight:700; color:var(--text);">${stage.count} 标的</span>
          </div>
        </div>
      `;
    });

    funnelContainer.innerHTML = funnelHtml;
  }
};
