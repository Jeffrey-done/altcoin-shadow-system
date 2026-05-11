#!/usr/bin/env python3
"""
小币种影子空单追踪器 v3.0
功能：
  - 追踪持仓盈亏
  - 分批止盈（TP1 -20% 锁50%仓位，TP2 -35% 全仓平）
  - 移动止损（最高盈利回撤10%触发）
  - 时间止损（7天且盈利<5%强制平）
  - 统一逻辑：--check-only 和日报共用同一套评估函数
"""

import sys
from dataclasses import dataclass
from typing import Optional, Tuple

import ccxt

import config
from common import (
    TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    utcnow_iso, hold_days,
)
from models import Trade

logger = setup_logger("altcoin_tracker")


# ══════════════════════════════════════════════════════════════════
#  核心评估逻辑（唯一真相源）
# ══════════════════════════════════════════════════════════════════

@dataclass
class EvalResult:
    """交易评估结果"""
    pnl_pct: float              # 当前盈亏百分比
    pnl_usd: float              # 当前盈亏金额（剩余仓位）
    current_price: float
    updated: bool = False       # 是否有字段更新
    closed: bool = False        # 是否触发平仓
    close_reason: Optional[str] = None
    alert_msg: Optional[str] = None


def evaluate_trade(trade: Trade, current_price: float) -> EvalResult:
    """
    评估单笔交易状态，统一处理：
    - 浮盈计算
    - 移动止损更新
    - TP1 / TP2 分批止盈
    - 移动止损触发
    - 时间止损触发

    返回 EvalResult，调用方根据结果决定是否推送 / 写入。
    """
    entry = trade.entry_price
    stake = trade.stake

    # 做空盈亏
    pnl_pct = (entry - current_price) / entry * 100
    effective_stake = trade.stake_remaining
    pnl_usd = effective_stake * pnl_pct / 100

    result = EvalResult(
        pnl_pct=pnl_pct,
        pnl_usd=round(pnl_usd, 2),
        current_price=current_price,
    )

    # ── 更新移动止损最高盈利 ──
    if pnl_pct > trade.best_pnl_pct:
        trade.best_pnl_pct = round(pnl_pct, 2)
        trail_pct = config.TRAIL_STOP_DRAWDOWN_PCT
        # 移动止损触发价 = 入场价 × (1 - (当前最高盈利% - 回撤阈值))
        trade.trail_stop_price = round(entry * (1 - (pnl_pct / 100 - trail_pct)), 6)
        result.updated = True

    # ── 分批止盈 / 止损判断 ──
    days = hold_days(trade.opened_at)

    # TP1：第一档止盈 -20%（价格跌到 TP1 价位）
    if not trade.tp1_triggered and trade.take_profit_1 and current_price <= trade.take_profit_1:
        trade.tp1_triggered = True
        # 锁定 50% 仓位的利润
        locked_pnl = (stake * config.TP1_CLOSE_RATIO) * pnl_pct / 100
        trade.tp1_locked_pnl = round(locked_pnl, 2)
        trade.stake_remaining = stake * (1 - config.TP1_CLOSE_RATIO)
        # 剩余仓位的浮盈
        result.pnl_usd = round(trade.stake_remaining * pnl_pct / 100, 2)
        result.updated = True
        result.alert_msg = (
            f"🎯 <b>第一档止盈触发（-20%）</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"锁定盈利：<b>{locked_pnl:+.2f}U（{pnl_pct:+.1f}%）</b>\n"
            f"剩余50%等待第二档-35%止盈 ✅"
        )
        logger.info(f"[TP1] {trade.symbol} @ {current_price}, 锁定 {locked_pnl:+.2f}U")

    # TP2：第二档止盈 -35%
    elif trade.tp1_triggered and trade.take_profit_2 and current_price <= trade.take_profit_2:
        # 剩余仓位盈亏
        remaining_pnl = trade.stake_remaining * pnl_pct / 100
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        trade.pnl = round(total_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        trade.close_reason = "TP2止盈-35%全仓平仓"
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.close_reason = "✅ 第二档止盈（-35%，全仓平仓）"
        result.alert_msg = (
            f"🎯 <b>第二档止盈触发（-35%全仓平仓）</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"TP1锁定：{trade.tp1_locked_pnl:+.2f}U\n"
            f"TP2剩余：{remaining_pnl:+.2f}U\n"
            f"最终盈亏：<b>{total_pnl:+.2f}U（{pnl_pct:+.1f}%）</b>\n"
            f"影子空单已全部平仓 ✅"
        )
        logger.info(f"[TP2] {trade.symbol} @ {current_price}, 总盈亏 {total_pnl:+.2f}U")

    # 移动止损：最高盈利 >= 5% 后激活，价格反弹到 trail_stop_price 触发
    elif (trade.trail_stop_price
          and trade.best_pnl_pct >= config.TRAIL_STOP_ACTIVATE_PCT
          and current_price >= trade.trail_stop_price):
        remaining_pnl = trade.stake_remaining * pnl_pct / 100
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        trade.pnl = round(total_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        trade.close_reason = f"移动止损（最高{trade.best_pnl_pct:.1f}%→{pnl_pct:.1f}%）"
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.close_reason = f"🛑 移动止损触发（最高盈利{trade.best_pnl_pct:.1f}%，回撤至{pnl_pct:.1f}%）"
        result.alert_msg = (
            f"🛑 <b>移动止损触发</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"历史最高：{trade.best_pnl_pct:.1f}% | 当前：{pnl_pct:.1f}%\n"
            f"盈亏：<b>{total_pnl:+.2f}U</b> | 已自动平仓 ✅"
        )
        logger.info(f"[移动止损] {trade.symbol} @ {current_price}")

    # 时间止损：超过 MAX_HOLD_DAYS 且盈利不足
    elif days >= trade.max_hold_days and pnl_pct < config.TIME_STOP_MIN_PROFIT_PCT:
        remaining_pnl = trade.stake_remaining * pnl_pct / 100
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        trade.pnl = round(total_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        trade.close_reason = f"时间止损（{days}天，{pnl_pct:.1f}%）"
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.close_reason = f"⏰ 时间止损（持仓{days}天，盈利仅{pnl_pct:.1f}%）"
        result.alert_msg = (
            f"⏰ <b>时间止损触发</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"持仓{days}天，盈利仅{pnl_pct:.1f}%，强制平仓\n"
            f"盈亏：<b>{total_pnl:+.2f}U</b> ✅"
        )
        logger.info(f"[时间止损] {trade.symbol} 持仓{days}天")

    # 无触发：更新浮盈
    else:
        trade.pnl = round(pnl_usd, 2)

    trade.current_price = current_price
    return result


# ══════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════

def run(check_only: bool = False):
    """
    统一入口：
    - check_only=True：只检查止盈 / 止损触发，触发时单独推送
    - check_only=False：检查 + 推送日报汇总
    """
    trades_raw = load_json(TRADES_FILE, [])
    if not trades_raw:
        logger.info("无交易记录")
        return

    trades = [Trade.from_dict(t) for t in trades_raw]
    open_trades = [t for t in trades if t.status == 'open']

    if not open_trades:
        logger.info("无持仓中的空单")
        return

    binance = ccxt.binance({'enableRateLimit': True})
    any_updated = False

    # 日报行
    lines = [
        "📊 <b>小币种影子空单日报</b>",
        f"⏰ {utcnow_iso()[:16].replace('T', ' ')} UTC",
        "",
    ]

    for trade in open_trades:
        # 获取最新价
        try:
            current = binance.fetch_ticker(trade.symbol)['last']
        except Exception as e:
            logger.warning(f"获取价格失败 ({trade.symbol}): {e}")
            current = trade.entry_price

        result = evaluate_trade(trade, current)

        if result.updated:
            any_updated = True

        # 触发推送
        if result.alert_msg:
            send_tg(result.alert_msg)

        # 日报内容
        if not check_only:
            if result.close_reason:
                lines.append(f"{result.close_reason} <b>{trade.symbol}</b>")
                lines.append(f"   入场: {trade.entry_price:.5f} → 现价: {current:.5f}")
                lines.append(f"   盈亏: <b>{result.pnl_usd:+.2f}U ({result.pnl_pct:+.1f}%)</b>")
            else:
                emoji = "🟢" if result.pnl_pct > 0 else "🔴"
                lines.append(f"{emoji} <b>{trade.symbol}</b> 做空持仓中")
                lines.append(f"   入场: {trade.entry_price:.5f} | 现价: {current:.5f}")
                lines.append(
                    f"   浮动盈亏: <b>{result.pnl_pct:+.1f}% ({result.pnl_usd:+.2f}U)</b>"
                )
                trail_str = f" | 移动止损: {trade.trail_stop_price:.5f}" if trade.trail_stop_price else ""
                if trade.tp1_triggered:
                    lines.append(
                        f"   ✅TP1已锁定{trade.tp1_locked_pnl:+.2f}U | "
                        f"TP2: {trade.take_profit_2:.5f}(-35%){trail_str}"
                    )
                else:
                    lines.append(
                        f"   TP1: {trade.take_profit_1:.5f}(-20%) | "
                        f"TP2: {trade.take_profit_2:.5f}(-35%){trail_str}"
                    )
            lines.append("")

    # 汇总
    if not check_only:
        total_open_pnl = sum(
            t.tp1_locked_pnl + t.pnl
            for t in trades if t.status == 'open'
        )
        total_closed_pnl = sum(
            t.tp1_locked_pnl + t.pnl
            for t in trades if t.status == 'closed'
        )
        lines.append(f"💰 持仓浮盈: <b>{total_open_pnl:+.2f}U</b>")
        lines.append(f"💰 已实现盈亏: <b>{total_closed_pnl:+.2f}U</b>")

        msg = "\n".join(lines)
        send_tg(msg)
        logger.info(msg)

    # 持久化
    if any_updated:
        atomic_write_json(TRADES_FILE, [t.to_dict() for t in trades])
        logger.info("交易数据已更新并保存")


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    check_only = '--check-only' in sys.argv
    run(check_only=check_only)
