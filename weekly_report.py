#!/usr/bin/env python3
"""
策略周报模块 v1.0
功能：
  - 聚合做空交易和费率套利交易数据
  - 计算周度统计：总盈亏、胜率、最佳/最差交易、回撤、每日明细
  - 生成格式化报告（TG推送 + JSON数据）
  - 基于表现的策略建议
  - 可通过 cron 每周一运行

用法：
  python3 weekly_report.py              # 生成周报并推送TG
  python3 weekly_report.py --json-only  # 只生成JSON不推送
  python3 weekly_report.py --no-push    # 生成报告但不推送TG
"""

import argparse
from datetime import datetime, timedelta, timezone

import config
from common import (
    TRADES_FILE,
    FUNDING_TRADES_FILE,
    WEEKLY_REPORT_FILE,
    setup_logger,
    send_tg,
    atomic_write_json,
    load_json,
    utcnow,
    utcnow_iso,
    parse_iso,
)
from models import Trade, FundingTrade

logger = setup_logger("weekly_report")


# ══════════════════════════════════════════════════════════════════
#  时间范围
# ══════════════════════════════════════════════════════════════════

def get_week_range(reference_date=None):
    """
    返回 (week_start, week_end) 表示报告覆盖的周范围。
    - 如果 reference_date 为 None，使用 utcnow()。
    - 如果当天是周一，报告覆盖上周一至上周日。
    - 否则报告覆盖本周一至当前时间（部分周）。

    Returns:
        tuple: (week_start: datetime, week_end: datetime)
    """
    if reference_date is None:
        reference_date = utcnow()

    weekday = reference_date.weekday()  # 0=Monday

    if weekday == 0:
        # Monday: report covers previous Mon-Sun
        week_end = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = week_end - timedelta(days=7)
    else:
        # Mid-week: report covers current Mon to now
        days_since_monday = weekday
        week_start = (reference_date - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_end = reference_date

    return week_start, week_end


# ══════════════════════════════════════════════════════════════════
#  数据收集
# ══════════════════════════════════════════════════════════════════

def collect_weekly_trades(week_start, week_end):
    """
    加载交易文件，筛选在 [week_start, week_end] 内平仓的交易。

    Returns:
        tuple: (short_trades: list[Trade], funding_trades: list[FundingTrade])
    """
    # 加载做空交易
    raw_trades = load_json(TRADES_FILE, [])
    all_trades = [Trade.from_dict(t) for t in raw_trades]

    short_trades = []
    for t in all_trades:
        if t.status != 'closed' or not t.closed_at:
            continue
        closed_dt = parse_iso(t.closed_at)
        if week_start <= closed_dt <= week_end:
            short_trades.append(t)

    # 加载费率套利交易
    raw_funding = load_json(FUNDING_TRADES_FILE, [])
    all_funding = [FundingTrade.from_dict(f) for f in raw_funding]

    funding_trades = []
    for f in all_funding:
        if f.status != 'closed' or not f.closed_at:
            continue
        closed_dt = parse_iso(f.closed_at)
        if week_start <= closed_dt <= week_end:
            funding_trades.append(f)

    return short_trades, funding_trades


# ══════════════════════════════════════════════════════════════════
#  统计计算
# ══════════════════════════════════════════════════════════════════

def calculate_weekly_stats(short_trades, funding_trades):
    """
    计算周度统计数据。

    做空交易盈亏 = tp1_locked_pnl + pnl
    费率交易盈亏 = total_pnl

    Returns:
        dict: 统计数据
    """
    # 做空盈亏
    short_pnls = [(t.tp1_locked_pnl + t.pnl) for t in short_trades]
    short_pnl = sum(short_pnls)

    # 费率盈亏
    funding_pnls = [f.total_pnl for f in funding_trades]
    funding_pnl = sum(funding_pnls)

    total_pnl = short_pnl + funding_pnl
    total_trades = len(short_trades) + len(funding_trades)

    # 胜率统计
    all_pnls = short_pnls + funding_pnls
    win_count = sum(1 for p in all_pnls if p > 0)
    loss_count = sum(1 for p in all_pnls if p <= 0)
    win_rate = round(win_count / total_trades * 100, 1) if total_trades > 0 else 0.0

    # 最佳/最差交易
    best_trade = {'symbol': '-', 'pnl': 0.0}
    worst_trade = {'symbol': '-', 'pnl': 0.0}

    all_trade_entries = []
    for t in short_trades:
        all_trade_entries.append({'symbol': t.symbol, 'pnl': t.tp1_locked_pnl + t.pnl})
    for f in funding_trades:
        all_trade_entries.append({'symbol': f.symbol, 'pnl': f.total_pnl})

    if all_trade_entries:
        best_trade = max(all_trade_entries, key=lambda x: x['pnl'])
        worst_trade = min(all_trade_entries, key=lambda x: x['pnl'])

    # 每日明细
    daily_breakdown = {}
    for t in short_trades:
        if t.closed_at:
            day_str = parse_iso(t.closed_at).strftime('%Y-%m-%d')
            daily_breakdown[day_str] = daily_breakdown.get(day_str, 0) + t.tp1_locked_pnl + t.pnl
    for f in funding_trades:
        if f.closed_at:
            day_str = parse_iso(f.closed_at).strftime('%Y-%m-%d')
            daily_breakdown[day_str] = daily_breakdown.get(day_str, 0) + f.total_pnl

    # 四舍五入每日盈亏
    daily_breakdown = {k: round(v, 2) for k, v in sorted(daily_breakdown.items())}

    # 最大单日亏损
    max_drawdown_day = min(daily_breakdown.values()) if daily_breakdown else 0.0

    # 平均持仓时间（小时）
    hold_hours_list = []
    for t in short_trades:
        if t.opened_at and t.closed_at:
            opened = parse_iso(t.opened_at)
            closed = parse_iso(t.closed_at)
            hold_hours_list.append((closed - opened).total_seconds() / 3600)
    for f in funding_trades:
        if f.opened_at and f.closed_at:
            opened = parse_iso(f.opened_at)
            closed = parse_iso(f.closed_at)
            hold_hours_list.append((closed - opened).total_seconds() / 3600)

    avg_hold_hours = round(sum(hold_hours_list) / len(hold_hours_list), 1) if hold_hours_list else 0.0

    # 策略分组
    strategy_breakdown = {}
    for t in short_trades:
        key = t.strategy
        if key not in strategy_breakdown:
            strategy_breakdown[key] = {'count': 0, 'pnl': 0.0, 'wins': 0}
        strategy_breakdown[key]['count'] += 1
        pnl = t.tp1_locked_pnl + t.pnl
        strategy_breakdown[key]['pnl'] += pnl
        if pnl > 0:
            strategy_breakdown[key]['wins'] += 1
    for f in funding_trades:
        key = f.strategy
        if key not in strategy_breakdown:
            strategy_breakdown[key] = {'count': 0, 'pnl': 0.0, 'wins': 0}
        strategy_breakdown[key]['count'] += 1
        strategy_breakdown[key]['pnl'] += f.total_pnl
        if f.total_pnl > 0:
            strategy_breakdown[key]['wins'] += 1

    # 四舍五入策略盈亏
    for key in strategy_breakdown:
        strategy_breakdown[key]['pnl'] = round(strategy_breakdown[key]['pnl'], 2)

    return {
        'total_pnl': round(total_pnl, 2),
        'short_pnl': round(short_pnl, 2),
        'funding_pnl': round(funding_pnl, 2),
        'total_trades': total_trades,
        'win_count': win_count,
        'loss_count': loss_count,
        'win_rate': win_rate,
        'best_trade': best_trade,
        'worst_trade': worst_trade,
        'max_drawdown_day': round(max_drawdown_day, 2),
        'daily_breakdown': daily_breakdown,
        'avg_hold_hours': avg_hold_hours,
        'strategy_breakdown': strategy_breakdown,
    }


# ══════════════════════════════════════════════════════════════════
#  策略建议
# ══════════════════════════════════════════════════════════════════

def generate_suggestions(stats):
    """
    根据统计结果生成可操作的建议列表。

    Returns:
        list[str]: 建议列表
    """
    suggestions = []

    # 胜率过低
    if stats['total_trades'] > 0 and stats['win_rate'] < 40:
        suggestions.append("胜率低于40%，建议收紧入场条件（提高RSI阈值或增加确认信号）")

    # 最大单日亏损过大
    if stats['max_drawdown_day'] < -(config.ACCOUNT_BALANCE * 0.20):
        suggestions.append("单日最大亏损超过本金20%，建议降低杠杆或减少单笔仓位")

    # 平均持仓时间过长
    if stats['avg_hold_hours'] > 20:
        suggestions.append("平均持仓超过20小时，建议检查时间止损参数是否过于宽松")

    # 费率收入占比高
    total_pnl = stats['total_pnl']
    funding_pnl = stats['funding_pnl']
    if total_pnl > 0 and funding_pnl > 0 and (funding_pnl / total_pnl) > 0.5:
        suggestions.append("费率套利贡献超过50%总收益，建议增加费率策略资金分配")

    # 连续亏损检查（通过亏损数判断）
    if stats['loss_count'] >= 3 and stats['win_count'] == 0:
        suggestions.append("存在连续亏损，建议审查风控参数和市场环境适配性")

    # 总体亏损
    if total_pnl < 0:
        suggestions.append("本周整体亏损，建议回顾入场信号质量和止损执行情况")

    # 无交易
    if stats['total_trades'] == 0:
        suggestions.append("本周无交易，检查扫描器是否正常运行或市场是否过冷")

    return suggestions


# ══════════════════════════════════════════════════════════════════
#  报告格式化
# ══════════════════════════════════════════════════════════════════

def _get_weekly_grade(total_pnl):
    """根据ROI评级"""
    roi = total_pnl / config.ACCOUNT_BALANCE * 100
    if roi >= config.WEEKLY_ROI_GRADE_A:
        return 'A', roi
    elif roi >= config.WEEKLY_ROI_GRADE_B:
        return 'B', roi
    elif roi >= config.WEEKLY_ROI_GRADE_C:
        return 'C', roi
    else:
        return 'F', roi


def format_tg_report(stats, suggestions, week_start, week_end):
    """
    生成 HTML 格式的 TG 周报消息。

    Returns:
        str: HTML 格式消息
    """
    grade, roi = _get_weekly_grade(stats['total_pnl'])
    grade_emoji = {'A': '🏆', 'B': '✅', 'C': '⚠️', 'F': '❌'}.get(grade, '❓')

    start_str = week_start.strftime('%m/%d')
    end_str = week_end.strftime('%m/%d')

    lines = [
        f"📊 <b>策略周报 {start_str} - {end_str}</b>",
        f"评级：{grade_emoji} <b>{grade}</b>（ROI: {roi:+.1f}%）",
        "",
        "<b>--- 盈亏汇总 ---</b>",
        f"总盈亏：<code>{stats['total_pnl']:+.2f}U</code>",
        f"  做空策略：<code>{stats['short_pnl']:+.2f}U</code>",
        f"  费率套利：<code>{stats['funding_pnl']:+.2f}U</code>",
        "",
        "<b>--- 交易统计 ---</b>",
        f"总交易数：{stats['total_trades']}",
        f"胜/负：{stats['win_count']}/{stats['loss_count']}",
        f"胜率：<b>{stats['win_rate']}%</b>",
        f"平均持仓：{stats['avg_hold_hours']}h",
        "",
        "<b>--- 最佳/最差 ---</b>",
        f"最佳：{stats['best_trade']['symbol']} <code>{stats['best_trade']['pnl']:+.2f}U</code>",
        f"最差：{stats['worst_trade']['symbol']} <code>{stats['worst_trade']['pnl']:+.2f}U</code>",
    ]

    # 策略分组
    if stats['strategy_breakdown']:
        lines.append("")
        lines.append("<b>--- 策略明细 ---</b>")
        for strategy, data in stats['strategy_breakdown'].items():
            s_win_rate = round(data['wins'] / data['count'] * 100, 0) if data['count'] > 0 else 0
            lines.append(f"  {strategy}: {data['count']}笔 | {data['pnl']:+.2f}U | 胜率{s_win_rate:.0f}%")

    # 每日明细
    if stats['daily_breakdown']:
        lines.append("")
        lines.append("<b>--- 每日盈亏 ---</b>")
        for day, pnl in stats['daily_breakdown'].items():
            emoji = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"  {emoji} {day}: <code>{pnl:+.2f}U</code>")

    # 建议
    if suggestions:
        lines.append("")
        lines.append("<b>--- 策略建议 ---</b>")
        for i, s in enumerate(suggestions, 1):
            lines.append(f"  {i}. {s}")

    lines.append("")
    lines.append(f"最大单日亏损：<code>{stats['max_drawdown_day']:+.2f}U</code>")
    lines.append(f"本金：{config.ACCOUNT_BALANCE}U | 杠杆：{config.LEVERAGE}x")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  JSON 报告
# ══════════════════════════════════════════════════════════════════

def generate_json_report(stats, suggestions, week_start, week_end):
    """
    生成结构化 JSON 报告并保存到 WEEKLY_REPORT_FILE。

    Returns:
        dict: 报告数据
    """
    grade, roi = _get_weekly_grade(stats['total_pnl'])

    report = {
        'metadata': {
            'generated_at': utcnow_iso(),
            'week_start': week_start.isoformat(),
            'week_end': week_end.isoformat(),
            'grade': grade,
            'roi_pct': round(roi, 2),
        },
        'stats': stats,
        'suggestions': suggestions,
        'config_snapshot': {
            'account_balance': config.ACCOUNT_BALANCE,
            'leverage': config.LEVERAGE,
            'default_stake': config.DEFAULT_STAKE,
        },
    }

    atomic_write_json(WEEKLY_REPORT_FILE, report)
    logger.info(f"JSON 报告已保存: {WEEKLY_REPORT_FILE}")

    return report


# ══════════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='策略周报生成器')
    parser.add_argument('--json-only', action='store_true', help='只生成JSON报告，不推送TG')
    parser.add_argument('--no-push', action='store_true', help='生成报告但不推送TG')
    args = parser.parse_args()

    if not config.WEEKLY_REPORT_ENABLED:
        logger.info("周报功能已关闭 (WEEKLY_REPORT_ENABLED=False)")
        return

    # 计算周范围
    week_start, week_end = get_week_range()
    logger.info(f"周报范围: {week_start.isoformat()} ~ {week_end.isoformat()}")

    # 收集交易数据
    short_trades, funding_trades = collect_weekly_trades(week_start, week_end)
    logger.info(f"本周交易: 做空 {len(short_trades)} 笔, 费率 {len(funding_trades)} 笔")

    # 计算统计
    stats = calculate_weekly_stats(short_trades, funding_trades)

    # 生成建议
    suggestions = generate_suggestions(stats)

    # 生成 JSON 报告（始终生成）
    report = generate_json_report(stats, suggestions, week_start, week_end)

    if args.json_only:
        logger.info("--json-only 模式，跳过 TG 推送")
        return

    # 生成 TG 报告
    tg_msg = format_tg_report(stats, suggestions, week_start, week_end)

    if args.no_push:
        logger.info("--no-push 模式，报告内容：")
        logger.info(tg_msg)
        return

    # 推送 TG
    success = send_tg(tg_msg)
    if success:
        logger.info("周报已推送至 TG")
    else:
        logger.warning("TG 推送失败")


if __name__ == '__main__':
    main()
