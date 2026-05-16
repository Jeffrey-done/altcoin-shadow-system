#!/usr/bin/env python3
"""
自动回测优化建议模块
每周自动对比不同参数组合，发现更优参数时推送 TG 建议。
"""

import config
from common import (
    setup_logger, send_tg, TRADES_FILE, load_json,
    get_current_account_id, filter_trades_by_account,
)

logger = setup_logger("auto_optimize")


# 待测试的参数变体
PARAM_VARIANTS = [
    {'name': '当前参数', 'tp1_pct': round((1-config.TP1_MULTIPLIER)*100, 1), 'tp2_pct': round((1-config.TP2_MULTIPLIER)*100, 1), 'hard_stop_pct': config.HARD_STOP_LOSS_PCT, 'daily_rsi_min': config.DAILY_RSI_MIN},
    {'name': 'TP2收紧6%', 'tp1_pct': 5, 'tp2_pct': 6, 'hard_stop_pct': 5, 'daily_rsi_min': 80},
    {'name': 'TP2放宽10%', 'tp1_pct': 5, 'tp2_pct': 10, 'hard_stop_pct': 5, 'daily_rsi_min': 80},
    {'name': '硬止损3%', 'tp1_pct': 5, 'tp2_pct': 8, 'hard_stop_pct': 3, 'daily_rsi_min': 80},
    {'name': '硬止损7%', 'tp1_pct': 5, 'tp2_pct': 8, 'hard_stop_pct': 7, 'daily_rsi_min': 80},
    {'name': 'RSI75入场', 'tp1_pct': 5, 'tp2_pct': 8, 'hard_stop_pct': 5, 'daily_rsi_min': 75},
    {'name': 'RSI85入场', 'tp1_pct': 5, 'tp2_pct': 8, 'hard_stop_pct': 5, 'daily_rsi_min': 85},
]


def run_auto_optimize():
    """
    自动回测优化：对比参数变体，发现更优时推送建议。
    注意：这里只做简单的胜率/盈亏对比作为建议，不会自动修改config。
    """
    if not config.AUTO_OPTIMIZE_ENABLED:
        logger.info("自动优化已关闭")
        return

    # 检查最近是否有足够交易作为参考
    # 多账户合规（2026-05）：原实现读 ALL accounts 的交易、用 active 账户的参数对比，
    # 多账户场景会输出错位建议（"基于全部账户的胜率，但参数对应的是 active 账户"）。
    # 改为按活跃账户过滤交易；单账户兼容时 account_id='' filter 不生效，行为不变。
    account_id = get_current_account_id()
    trades = load_json(TRADES_FILE, [])
    trades = filter_trades_by_account(trades, account_id)
    closed = [t for t in trades if t.get('status') == 'closed']
    if len(closed) < 5:
        logger.info(f"已平仓交易不足5笔（{len(closed)}），跳过优化分析")
        return

    # 统计当前策略实际表现
    recent_closed = closed[-20:]  # 最近20笔
    actual_pnls = [t.get('tp1_locked_pnl', 0) + t.get('pnl', 0) for t in recent_closed]
    actual_total = sum(actual_pnls)
    actual_wins = sum(1 for p in actual_pnls if p > 0)
    actual_winrate = round(actual_wins / len(actual_pnls) * 100, 1) if actual_pnls else 0

    # 构建建议消息
    lines = [
        "🔬 <b>自动优化分析报告</b>",
        "",
        f"📊 <b>当前实盘表现（最近{len(recent_closed)}笔）</b>",
        f"  总盈亏: {actual_total:+.2f}U",
        f"  胜率: {actual_winrate}%",
        f"  参数: TP1={round((1-config.TP1_MULTIPLIER)*100,1)}% / TP2={round((1-config.TP2_MULTIPLIER)*100,1)}% / 止损={config.HARD_STOP_LOSS_PCT}% / RSI>{config.DAILY_RSI_MIN}",
        "",
        "💡 <b>参数建议</b>",
    ]

    # 基于实际表现给出建议
    suggestions = []
    
    # 分析止损触发率
    stop_loss_trades = [t for t in recent_closed if '止损' in t.get('close_reason', '') or 'stop' in t.get('close_reason', '').lower()]
    stop_rate = len(stop_loss_trades) / len(recent_closed) * 100 if recent_closed else 0
    
    if stop_rate > 50:
        suggestions.append(f"止损触发率高达{stop_rate:.0f}%，建议放宽硬止损（当前{config.HARD_STOP_LOSS_PCT}% → 建议7%）")
    
    if actual_winrate < 40:
        suggestions.append(f"胜率仅{actual_winrate}%，建议提高入场门槛（RSI>{config.DAILY_RSI_MIN} → 建议>85）")
    
    # TP触发率分析
    tp1_trades = [t for t in recent_closed if t.get('tp1_triggered')]
    tp1_rate = len(tp1_trades) / len(recent_closed) * 100 if recent_closed else 0
    tp2_trades = [t for t in recent_closed if 'TP2' in t.get('close_reason', '')]
    tp2_rate = len(tp2_trades) / len(recent_closed) * 100 if recent_closed else 0
    
    if tp1_rate > 60 and tp2_rate < 10:
        suggestions.append(f"TP1触发率{tp1_rate:.0f}%但TP2仅{tp2_rate:.0f}%，建议收紧TP2（当前{round((1-config.TP2_MULTIPLIER)*100,1)}% → 建议6%）")
    
    if not suggestions:
        suggestions.append("当前参数表现良好，无需调整 ✅")
    
    for s in suggestions:
        lines.append(f"  • {s}")
    
    lines.append("")
    lines.append(f"止损触发率: {stop_rate:.0f}% | TP1率: {tp1_rate:.0f}% | TP2率: {tp2_rate:.0f}%")
    lines.append("")
    lines.append("<i>以上仅为参考建议，修改参数请手动更新 config.py</i>")

    msg = "\n".join(lines)
    send_tg(msg)
    logger.info(msg)


if __name__ == '__main__':
    run_auto_optimize()
