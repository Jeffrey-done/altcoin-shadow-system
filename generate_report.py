"""
Generate the Altcoin Shadow System evaluation report as a Word document.
"""
from docx import Document
from docx.shared import Inches, Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def set_cell_shading(cell, color):
    """Set cell background color."""
    shading_elm = OxmlElement('w:shd')
    shading_elm.set(qn('w:fill'), color)
    shading_elm.set(qn('w:val'), 'clear')
    cell._tc.get_or_add_tcPr().append(shading_elm)

def set_table_style(table):
    """Apply basic formatting to table."""
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_before = Pt(2)
                paragraph.paragraph_format.space_after = Pt(2)
                for run in paragraph.runs:
                    run.font.size = Pt(9)

def add_header_row(table, headers, color="1F4E79"):
    """Format the first row as header."""
    for i, cell in enumerate(table.rows[0].cells):
        set_cell_shading(cell, color)
        for paragraph in cell.paragraphs:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in paragraph.runs:
                run.font.color.rgb = RGBColor(255, 255, 255)
                run.font.bold = True
                run.font.size = Pt(9)

def add_table(doc, headers, rows, header_color="1F4E79"):
    """Add a formatted table to the document."""
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.style = 'Table Grid'
    
    # Header
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = h
    
    # Data rows
    for r_idx, row_data in enumerate(rows):
        for c_idx, val in enumerate(row_data):
            table.rows[r_idx + 1].cells[c_idx].text = str(val)
    
    add_header_row(table, headers, header_color)
    set_table_style(table)
    doc.add_paragraph()  # spacing
    return table


def main():
    doc = Document()
    
    # Set default font
    style = doc.styles['Normal']
    font = style.font
    font.name = 'Microsoft YaHei'
    font.size = Pt(10.5)
    
    # ============ TITLE ============
    title = doc.add_heading('Altcoin Shadow System — 深度量化评估报告', level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    doc.add_paragraph()
    
    # Metadata
    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = meta.add_run('系统类型：基于 Python 的中频均值回归做空量化交易系统\n')
    run.font.size = Pt(10)
    run = meta.add_run('评估日期：2026年5月26日\n')
    run.font.size = Pt(10)
    run = meta.add_run('评估维度：市场定位 | 技术代差 | SWOT | 改进路径')
    run.font.size = Pt(10)
    
    doc.add_paragraph()
    
    # ============ SECTION 1 ============
    doc.add_heading('一、市场定位与赛道分析', level=1)
    
    doc.add_heading('1.1 梯队定位：进阶个人开发者级 → 小型私募级之间（2.5/5 梯队）', level=2)
    
    add_table(doc,
        ['维度', '评估', '理由'],
        [
            ['资金管理规模', '个人/小团队', '默认本金 100U，最大复利 300U stake，适配 $1K–$50K AUM'],
            ['工程成熟度', '小型私募水平', 'Docker 化部署、多账户隔离、TOTP 安全面板、JSON 文件锁并发控制'],
            ['策略复杂度', '个人开发者中上', '单因子（RSI）为核心，辅以 OI/Funding/量价背离等过滤层'],
            ['运维能力', '准机构级', '健康检查、任务超时诊断、TG 告警、审计日志、In-flight Journal 幽灵仓位防护'],
        ]
    )
    
    doc.add_heading('1.2 策略赛道：中频 Mean-Reversion Short（均值回归做空）', level=2)
    
    p = doc.add_paragraph()
    p.add_run('• 标的宇宙：').bold = True
    p.add_run('Binance USDT 永续合约中的小盘/中盘 altcoin（PRICE_MAX ≤ $50，VOL_MIN ≥ 50万U）\n')
    p.add_run('• 持仓周期：').bold = True
    p.add_run('1h–24h（中频偏短线）\n')
    p.add_run('• 核心逻辑：').bold = True
    p.add_run('超买（日线 RSI≥75）→ 回落确认（4h RSI drop≥10）→ 做空 → 分批止盈\n')
    p.add_run('• 适合赛道：').bold = True
    p.add_run('CTA 类趋势反转策略，而非高频做市或跨所套利')
    
    # ============ SECTION 2 ============
    doc.add_heading('二、技术代差与顶级系统差距对比', level=1)
    
    doc.add_heading('2.1 执行与延迟（Execution & Latency）', level=2)
    
    add_table(doc,
        ['维度', '本系统现状', '顶级量化机构目标', '差距评估'],
        [
            ['下单延迟', 'REST API 市价单（~100–300ms）', 'Co-located C++/FPGA 直连撮合引擎（<1ms）', '巨大差距（但策略不依赖极低延迟）'],
            ['滑点控制', '简单市价单 + 事后滑点告警（1%）', 'TWAP/VWAP/Adaptive Algo + Order Book 深度实时分析', '中等差距'],
            ['并发执行', 'asyncio + ThreadPoolExecutor（4线程）', '多进程微服务 + 内存总线通信（ZeroMQ/Aeron）', '中等差距'],
            ['网络拓扑', '普通 VPS Docker 部署', 'AWS Tokyo/Singapore co-location + 专线', '显著差距'],
        ]
    )
    
    p = doc.add_paragraph()
    p.add_run('结论：').bold = True
    p.add_run('中频策略（持仓 1–24h）对延迟容忍度高，当前架构基本够用。100ms 级延迟对 RSI 回落信号（小时级窗口）不构成致命劣势。')
    
    doc.add_heading('2.2 数据与回测（Data & Backtesting）', level=2)
    
    add_table(doc,
        ['维度', '本系统现状', '顶级量化机构目标', '差距评估'],
        [
            ['数据精度', '1h K 线为主，REST API 拉取', 'Tick-level L2 Order Book 全量重播', '显著差距'],
            ['Look-ahead Bias 防护', '✅ 丢弃最后未收盘 K 线、entry_idx+1 开盘价入场', '✅ Point-in-time 数据库 + 存活偏差校正', '基本达标'],
            ['回测引擎', '向量化引擎（numpy/pandas）+ 事件驱动引擎', '分布式回测集群 + Monte Carlo + Walk-forward', '中等差距（已有 Monte Carlo 模块）'],
            ['滑点建模', 'Volume-Based 滑点模型', '真实 Order Book 重播 + Almgren-Chriss 市场冲击模型', '中等差距'],
            ['资金费率', '简化为固定 0.01%/8h', '逐 8h 历史费率精确累计', '轻微差距'],
            ['数据完整性', '✅ 时间戳断点校验（≥4 根缺失丢弃数据）', '多源交叉验证 + 缺失填补', '基本达标'],
        ]
    )
    
    doc.add_heading('2.3 Alpha 来源（Signal Generation）', level=2)
    
    add_table(doc,
        ['维度', '本系统现状', '顶级量化机构目标', '差距评估'],
        [
            ['核心因子', 'RSI（Wilder 14期）+ OI 变化 + 资金费率', '多因子合成（100+因子、因子正交化、IC 衰减分析）', '巨大差距'],
            ['信号评分', '简单加权评分（满分100，4维各25）', 'ML 模型（XGBoost/Transformer）+ 非线性特征交互', '显著差距'],
            ['另类数据', '❌ 无链上数据 / 社交情绪 / 鲸鱼追踪', 'On-chain flow、Whale Alert、社交 NLP、DEX 流动性', '显著差距'],
            ['策略多样性', '3 个策略（做空超买、做多超卖、Funding 套利）但核心仍是 RSI 变体', '30–100+ 独立策略，低相关性组合', '巨大差距'],
            ['自适应能力', '参数固定（需人工 admin 面板调整）', '在线学习 + Regime Detection + 自动参数衰减', '显著差距'],
        ]
    )
    
    # ============ SECTION 3 ============
    doc.add_heading('三、系统核心剖析（SWOT）', level=1)
    
    doc.add_heading('3.1 Strengths（优势）', level=2)
    
    add_table(doc,
        ['#', '优势', '具体表现'],
        [
            ['S1', '工程完整度极高', '从扫描→开仓→持仓管理→平仓→风控→报告→诊断→归档，全生命周期闭环；45个测试用例'],
            ['S2', '多层风控体系', '8 重防护（日亏损/笔数/连亏暂停/冷却期/持仓占比/BTC 过滤/深度分析/幂等键）'],
            ['S3', '多交易所架构', 'Binance/OKX/Gate 三所路由 + 影子并行模式；支持 both 模式分散对手方风险'],
            ['S4', '多账户隔离', 'v3 架构每账户独立本金/杠杆/风控/复利曲线；per-account 冷却期'],
            ['S5', '部署运维成熟', 'Docker Compose 编排、Redis 事件总线、健康检查、任务超时诊断、WebSocket 断线告警'],
            ['S6', '安全设计优秀', 'PBKDF2-SHA256(600k iter) + TOTP + Secret URL + IP 锁定 + CSRF + 审计日志'],
            ['S7', '回测框架对齐实盘', '事件驱动回测引擎同步了复利/BTC 过滤/风控限频/冷却期，解决回测-实盘口径偏差'],
            ['S8', '优雅降级与容错', 'In-flight Journal 防幽灵仓位、平仓重试队列、多源价格 fallback'],
        ],
        header_color="2E7D32"
    )
    
    doc.add_heading('3.2 Weaknesses（劣势）', level=2)
    
    add_table(doc,
        ['#', '劣势', '影响'],
        [
            ['W1', 'Alpha 单薄', '核心仍是 RSI 一个因子，在越来越拥挤的均值回归赛道中容易被"信号衰减（Alpha Decay）"吞噬'],
            ['W2', 'JSON 文件存储', 'altcoin_shadow_trades.json + fcntl 锁作为主数据库，在高并发/大量历史数据下是瓶颈'],
            ['W3', '无真实 Order Book 模拟', '回测滑点模型是参数化公式，未基于真实深度重播；小币种流动性薄场景下可能严重低估滑点'],
            ['W4', '策略同质性高', '3 个策略本质都是 RSI 变体 + 方向翻转，真正的非相关 Alpha 源极少'],
            ['W5', '无实时 P&L / Greeks 监控', '没有 portfolio-level 的实时风险敞口计算（Delta/Vega/Funding exposure）'],
            ['W6', '缺乏统计检验', '无 Walk-forward validation、无 Out-of-sample 测试框架、无因子 IC/IR 分析'],
            ['W7', 'Python GIL 限制', 'ThreadPoolExecutor 4 线程受 GIL 制约；asyncio 层虽有但策略逻辑仍在线程池同步执行'],
        ],
        header_color="C62828"
    )
    
    doc.add_heading('3.3 Opportunities（机会）', level=2)
    
    add_table(doc,
        ['#', '机会', '可行性'],
        [
            ['O1', '链上数据整合', 'Whale Alert / DEX Net Flow / Exchange Inflow → 做空前确认"聪明钱出逃"'],
            ['O2', 'ML 评分模型', '用历史 SignalLog 表训练 XGBoost/LightGBM，替代硬编码加权评分'],
            ['O3', 'DB 全迁移', 'SQLAlchemy models 已定义，迁移后支持复杂查询、历史回溯、JOIN 分析'],
            ['O4', '多策略低相关组合', '引入 Funding Rate 套利（已有框架）、跨交易所基差套利、波动率卖方策略'],
            ['O5', 'Regime Detection', 'BTC 趋势过滤已做了简单版；可升级为 Hidden Markov / 聚类模型，动态调节策略权重'],
        ],
        header_color="1565C0"
    )
    
    doc.add_heading('3.4 Threats（威胁）', level=2)
    
    add_table(doc,
        ['#', '威胁', '严重程度'],
        [
            ['T1', '信号拥挤 — RSI 是最古老的技术指标，大量交易者和机器人都在用 → 信号边际收益递减', '🔴 高'],
            ['T2', '交易所风控升级 — Binance 动态调整杠杆上限/限速/保证金要求，可能导致策略参数突然失效', '🟡 中'],
            ['T3', '流动性抽取 — 小币种在极端行情下 Order Book 瞬间清空，5% 硬止损可能产生 10%+ 实际亏损', '🔴 高'],
            ['T4', '单一方向风险 — 纯做空策略在牛市中系统性亏损（BTC 过滤是唯一防线）', '🟡 中'],
        ],
        header_color="E65100"
    )
    
    # ============ SECTION 4 ============
    doc.add_heading('四、进阶改进建议', level=1)
    
    doc.add_heading('4.1 架构层面', level=2)
    
    add_table(doc,
        ['优先级', '改进项', '具体方案', '预期效果'],
        [
            ['P0', '数据层迁移', '全面切换到 PostgreSQL（models 已定义），消除 JSON + fcntl 瓶颈', '支持复杂查询、并发安全、数据完整性约束'],
            ['P1', 'WebSocket 全量化', '将 hot_scanner 的 WS 扩展到所有活跃持仓的 Order Book Depth Stream', '实时深度分析 + <100ms 止损触发'],
            ['P2', '微服务拆分', 'Scanner / Executor / RiskEngine / Dashboard 拆为独立进程，用 Redis Pub/Sub 通信', '水平扩展 + 故障隔离'],
            ['P3', 'Co-location 部署', '将执行引擎部署到 AWS Tokyo (ap-northeast-1)，靠近 Binance 撮合引擎', '下单延迟 ~50ms → ~5ms'],
        ]
    )
    
    doc.add_heading('4.2 风控层面', level=2)
    
    add_table(doc,
        ['优先级', '改进项', '具体方案', '预期效果'],
        [
            ['P0', 'Portfolio VaR', '实现 Historical VaR / CVaR 计算，基于持仓相关性矩阵估算尾部风险', '防止"所有持仓同时止损"的极端场景'],
            ['P1', '动态止损', '基于 ATR（Average True Range）动态调整硬止损百分比，替代固定 5%', '减少牛市假突破止损，增加熊市保护力度'],
            ['P2', 'Liquidation 预警', '接入 Binance @forceOrder WebSocket，监控全市场爆仓流 → 极端行情提前减仓', '黑天鹅保护'],
            ['P3', '压力测试模块', '用 Monte Carlo 模拟 3σ 事件（flash crash / exchange downtime / API 限速风暴）', '量化最大可承受冲击'],
        ]
    )
    
    doc.add_heading('4.3 策略层面', level=2)
    
    add_table(doc,
        ['优先级', '改进项', '具体方案', '预期效果'],
        [
            ['P0', 'ML 信号评分', '收集 signal_logs 表历史数据 → LightGBM 二分类（开仓后24h是否盈利）→ 替代硬编码评分', '预期 Sharpe +0.3–0.5'],
            ['P1', '链上因子', '集成 Glassnode/Nansen API：Exchange Inflow Spike + Whale Sell 信号 → 加入评分体系', '非相关 Alpha 源，对冲 RSI 拥挤风险'],
            ['P2', '多时间框架合成', '将 1D/4H/1H RSI 做向量正交化（PCA），生成"综合超买因子"', '减少假信号 20–30%'],
            ['P3', 'Regime-Aware 仓位', 'HMM 检测牛/熊/震荡市 → 牛市缩减做空仓位至 25%，熊市放大至 150%', '降低系统性方向风险'],
        ]
    )
    
    # ============ SECTION 5 ============
    doc.add_heading('五、最迫切的 3 条性能优化清单', level=1)
    
    add_table(doc,
        ['排名', '优化项', '投入产出比', '实施复杂度', '预期 Sharpe 改善'],
        [
            ['🥇 1', 'ML 信号评分替代硬编码 — 用已有 SignalLogModel 历史数据训练 LightGBM，按概率分级仓位', '⭐⭐⭐⭐⭐', '中（2–3周）', '+0.3–0.5'],
            ['🥈 2', '数据层迁移 PostgreSQL — 消除 JSON 文件锁瓶颈 + 支持 SQL 分析 + 为 ML 管道提供数据基础', '⭐⭐⭐⭐', '中（1–2周）', '间接（稳定性 +↑）'],
            ['🥉 3', '动态 ATR 止损 + Portfolio VaR — 止损按各币种波动率自适应；加 portfolio-level 尾部风险约束', '⭐⭐⭐⭐', '中（2周）', '+0.2'],
        ]
    )
    
    # ============ SECTION 6 ============
    doc.add_heading('六、总结评语', level=1)
    
    p = doc.add_paragraph()
    p.add_run('这是一套在工程完整度和运维成熟度方面远超同级别个人项目的量化系统。').bold = True
    p.add_run(' 它的风控设计（8 层防护 + 幂等键 + In-flight Journal + 多账户隔离）堪比小型对冲基金的合规要求。然而，其 ')
    p.add_run('Alpha 生成能力仍停留在"技术分析指标 + 规则引擎"阶段').bold = True
    p.add_run('，这是限制其长期竞争力的最大瓶颈。')
    
    doc.add_paragraph()
    
    doc.add_heading('向顶级量化机构跨越的关键路径：', level=2)
    
    add_table(doc,
        ['当前位置', '→', '目标位置'],
        [
            ['RSI + 规则引擎', '→', 'ML 多因子 + 链上数据'],
            ['JSON 文件存储', '→', 'TimescaleDB + 流处理'],
            ['固定 5% 止损', '→', 'ATR 动态 + VaR 约束'],
            ['单策略', '→', '低相关策略组合（30+）'],
            ['VPS 部署', '→', 'Co-location + 专线'],
        ]
    )
    
    # Final summary
    p = doc.add_paragraph()
    p.add_run('\n一句话总结：').bold = True
    p.add_run('工程层已达"能打实盘"的水准，但 Alpha 层仍在"能赚小钱"的阶段。突破口在于用 ML 替代规则引擎 + 引入非相关数据源 + 构建真正的 Portfolio Risk Management。')
    
    # Save
    output_path = '/projects/sandbox/altcoin-shadow-system/Altcoin_Shadow_System_评估报告.docx'
    doc.save(output_path)
    print(f"Report saved to: {output_path}")
    return output_path


if __name__ == '__main__':
    main()
