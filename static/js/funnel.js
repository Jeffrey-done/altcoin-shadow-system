/* ═══════════════════════════════════════════════════════════════
   Shadow Trading System - Candidate Funnel Visualization
   Visual funnel showing filtering stages from market to triggered
   ═══════════════════════════════════════════════════════════════ */

const CandidateFunnel = {
    containerId: 'funnel-container',

    render(candidates) {
        const container = document.getElementById(this.containerId);
        if (!container) return;

        // Calculate funnel stages from candidates data
        const allCandidates = candidates || [];
        const triggered = allCandidates.filter(c => c.triggered);
        const waiting = allCandidates.filter(c => !c.triggered);

        // Funnel stages (simulated from available data)
        // In a real system these would come from the scanner's pipeline
        const totalPool = allCandidates.length;
        const stages = [
            { label: '全市场', count: Math.max(200, totalPool * 20), color: '#58a6ff' },
            { label: '价格过滤', count: Math.max(80, totalPool * 8), color: '#79c0ff' },
            { label: '成交量过滤', count: Math.max(40, totalPool * 4), color: '#a371f7' },
            { label: 'RSI过滤', count: Math.max(15, totalPool * 2), color: '#d29922' },
            { label: '候选池', count: totalPool, color: '#3fb950' },
            { label: '已触发', count: triggered.length, color: '#f85149' },
        ];

        // If we have scanner metadata, use actual counts
        // (The API can provide these if available)

        const maxCount = stages[0].count || 1;

        let html = '<div class="funnel-container">';
        stages.forEach((stage, idx) => {
            const widthPct = Math.max(8, (stage.count / maxCount) * 100);
            const opacity = 1 - (idx * 0.1);
            html += `<div class="funnel-step">
                <div class="funnel-label">${stage.label}</div>
                <div class="funnel-bar" style="width:${widthPct}%;background:${stage.color};opacity:${opacity};">
                    ${stage.count}
                </div>
            </div>`;
        });

        // Arrow indicators between stages
        html += '</div>';

        // Conversion summary
        if (totalPool > 0) {
            const triggerRate = (triggered.length / totalPool * 100).toFixed(0);
            html += `<div style="font-size:0.75rem;color:var(--text-secondary);margin-top:8px;">
                触发率: ${triggerRate}% (${triggered.length}/${totalPool}) | 
                等待中: ${waiting.length}
            </div>`;
        }

        container.innerHTML = html;
    },

    // Render with actual scanner pipeline data (if API provides it)
    renderWithPipeline(pipelineData) {
        const container = document.getElementById(this.containerId);
        if (!container || !pipelineData) return;

        const stages = pipelineData.stages || [];
        if (stages.length === 0) return;

        const maxCount = stages[0].count || 1;
        const colors = ['#58a6ff', '#79c0ff', '#a371f7', '#d29922', '#3fb950', '#f85149'];

        let html = '<div class="funnel-container">';
        stages.forEach((stage, idx) => {
            const widthPct = Math.max(8, (stage.count / maxCount) * 100);
            const color = colors[idx % colors.length];
            html += `<div class="funnel-step">
                <div class="funnel-label">${stage.label}</div>
                <div class="funnel-bar" style="width:${widthPct}%;background:${color};">
                    ${stage.count}
                </div>
            </div>`;
        });
        html += '</div>';

        container.innerHTML = html;
    }
};
