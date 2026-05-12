#!/usr/bin/env python3
"""
小币种影子空单追踪器 v4.0
功能：
  - 追踪持仓盈亏（杠杆仓位）
  - 硬止损（价格反弹 3% 无条件平仓）
  - 分批止盈（TP1 -5% 锁50%仓位，TP2 -10% 全仓平）
  - 移动止损（最高盈利回撤10%触发）
  - 时间止损（24小时且盈利<3%强制平）
  - 风控集成（平仓时记录盈亏）
  - 统一逻辑：--check-only 和日报共用同一套评估函数
"""

import sys
from dataclasses import dataclass
from typing import Optional

import ccxt

import config
from common import (
    TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    utcnow_iso, hold_days, hold_hours,
)
from models import Trade
from risk_control import record_trade_closed, get_risk_summary

logger = setup_logger("altcoin_tracker")


# ══════════════════════════════════════════════════════════════════
#  核心评估逻辑（唯一真相源）
# ══════════════════════════════════════════════════════════════════

@dataclass
class EvalResult:
    """交易评估结果"""
    pnl_pct: float              # 价格变动百分比（做空：正=盈利）
    pnl_usd: float              # 当前盈亏金额（杠杆后）
    current_price: float
    updated: bool = False       # 是否有字段更新
    closed: bool = False        # 是否触发平仓
    close_reason: Optional[str] = None
    alert_msg: Optional[str] = None


def evaluate_trade(trade: Trade, current_price: float) -> EvalResult:
    """
    评估单笔交易状态，统一处理：
    - 杠杆盈亏计算（名义仓位）
    - 硬止损（最高优先级）
    - 移动止损更新
    - TP1 / TP2 分批止盈
    - 移动止损触发
    - 时间止损触发

    盈亏公式（做空）：
      pnl_pct = (entry - current) / entry * 100
      pnl_usd = notional_remaining * pnl_pct / 100
      其中 notional_remaining = stake_remaining * leverage
    """
    entry = trade.entry_price
    leverage = trade.leverage

    # 方向盈亏
    if trade.direction == 'LONG':
        pnl_pct = (current_price - entry) / entry * 100  # 做多：涨=赚
    else:
        pnl_pct = (entry - current_price) / entry * 100  # 做空：跌=赚

    # 杠杆后的名义仓位盈亏
    notional_remaining = trade.stake_remaining * leverage
    pnl_usd = notional_remaining * pnl_pct / 100

    result = EvalResult(
        pnl_pct=pnl_pct,
        pnl_usd=round(pnl_usd, 2),
        current_price=current_price,
    )

    # ══ 硬止损（最高优先级） ══
    hard_stop_hit = False
    if trade.hard_stop_price:
        if trade.direction == 'LONG' and current_price <= trade.hard_stop_price:
            hard_stop_hit = True
        elif trade.direction == 'SHORT' and current_price >= trade.hard_stop_price:
            hard_stop_hit = True

    if hard_stop_hit:
        remaining_pnl = notional_remaining * pnl_pct / 100
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        trade.pnl = round(total_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        trade.close_reason = f"硬止损（价格反弹{-pnl_pct:.1f}%触发）"
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.close_reason = f"🛑 硬止损触发（+{config.HARD_STOP_LOSS_PCT}%，亏损{total_pnl:.1f}U）"
        result.alert_msg = (
            f"🛑 <b>硬止损触发</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"价格反弹：{-pnl_pct:.2f}% > 止损线{config.HARD_STOP_LOSS_PCT}%\n"
            f"亏损：<b>{total_pnl:.2f}U</b>（保证金{trade.stake}U×{leverage}x）\n"
            f"已自动平仓，严格执行纪律 ✅"
        )
        logger.info(f"[硬止损] {trade.symbol} @ {current_price}, 亏损 {total_pnl:.2f}U")
        trade.current_price = current_price
        return result

    # ── 更新移动止损最高盈利 ──
    if pnl_pct > trade.best_pnl_pct:
        trade.best_pnl_pct = round(pnl_pct, 2)
        trail_pct = config.TRAIL_STOP_DRAWDOWN_PCT
        if trade.direction == 'LONG':
            # 做多：止损价在下方
            trade.trail_stop_price = round(entry * (1 + pnl_pct / 100 - trail_pct), 6)
        else:
            # 做空：止损价在上方
            trade.trail_stop_price = round(entry * (1 - (pnl_pct / 100 - trail_pct)), 6)
        result.updated = True

    # ── 分批止盈 / 止损判断 ──
    hours_held = hold_hours(trade.opened_at)

    # TP1：第一档止盈
    tp1_hit = False
    if not trade.tp1_triggered and trade.take_profit_1:
        if trade.direction == 'LONG' and current_price >= trade.take_profit_1:
            tp1_hit = True
        elif trade.direction == 'SHORT' and current_price <= trade.take_profit_1:
            tp1_hit = True

    if tp1_hit:
        trade.tp1_triggered = True
        # 锁定 50% 仓位的利润（杠杆后）
        locked_notional = trade.stake * config.TP1_CLOSE_RATIO * leverage
        locked_pnl = locked_notional * pnl_pct / 100
        trade.tp1_locked_pnl = round(locked_pnl, 2)
        trade.stake_remaining = trade.stake * (1 - config.TP1_CLOSE_RATIO)
        # 剩余仓位浮盈
        new_notional = trade.stake_remaining * leverage
        result.pnl_usd = round(new_notional * pnl_pct / 100, 2)
        result.updated = True
        result.alert_msg = (
            f"🎯 <b>第一档止盈触发（-{(1-config.TP1_MULTIPLIER)*100:.0f}%）</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"锁定盈利：<b>{locked_pnl:+.2f}U</b>（{int(config.TP1_CLOSE_RATIO*100)}%仓位）\n"
            f"名义仓位：{trade.stake}×{leverage}x → 剩余{trade.stake_remaining}×{leverage}x\n"
            f"剩余等待TP2（-{(1-config.TP2_MULTIPLIER)*100:.0f}%）✅"
        )
        logger.info(f"[TP1] {trade.symbol} @ {current_price}, 锁定 {locked_pnl:+.2f}U")

    # TP2：第二档止盈
    tp2_hit = False
    if trade.tp1_triggered and trade.take_profit_2:
        if trade.direction == 'LONG' and current_price >= trade.take_profit_2:
            tp2_hit = True
        elif trade.direction == 'SHORT' and current_price <= trade.take_profit_2:
            tp2_hit = True

    if tp2_hit:
        remaining_notional = trade.stake_remaining * leverage
        remaining_pnl = remaining_notional * pnl_pct / 100
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        trade.pnl = round(total_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        trade.close_reason = f"TP2止盈-{(1-config.TP2_MULTIPLIER)*100:.0f}%全仓平仓"
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.close_reason = f"✅ 第二档止盈（-{(1-config.TP2_MULTIPLIER)*100:.0f}%，全仓平仓）"
        result.alert_msg = (
            f"🎯 <b>第二档止盈触发（-{(1-config.TP2_MULTIPLIER)*100:.0f}%全仓平仓）</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"TP1锁定：{trade.tp1_locked_pnl:+.2f}U\n"
            f"TP2剩余：{remaining_pnl:+.2f}U\n"
            f"<b>总盈亏：{total_pnl:+.2f}U（{pnl_pct:+.1f}%×{leverage}x）</b>\n"
            f"完美止盈 🎉"
        )
        logger.info(f"[TP2] {trade.symbol} @ {current_price}, 总盈亏 {total_pnl:+.2f}U")

    # 移动止损
    trail_triggered = False
    if (trade.trail_stop_price
          and trade.best_pnl_pct >= config.TRAIL_STOP_ACTIVATE_PCT):
        if trade.direction == 'LONG' and current_price <= trade.trail_stop_price:
            trail_triggered = True
        elif trade.direction == 'SHORT' and current_price >= trade.trail_stop_price:
            trail_triggered = True

    if trail_triggered:
        remaining_notional = trade.stake_remaining * leverage
        remaining_pnl = remaining_notional * pnl_pct / 100
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        trade.pnl = round(total_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        trade.close_reason = f"移动止损（最高{trade.best_pnl_pct:.1f}%→{pnl_pct:.1f}%）"
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.close_reason = f"🛑 移动止损（最高{trade.best_pnl_pct:.1f}%→{pnl_pct:.1f}%）"
        result.alert_msg = (
            f"🛑 <b>移动止损触发</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"历史最高：{trade.best_pnl_pct:.1f}% | 当前：{pnl_pct:.1f}%\n"
            f"盈亏：<b>{total_pnl:+.2f}U</b>（{leverage}x杠杆）\n"
            f"已自动平仓 ✅"
        )
        logger.info(f"[移动止损] {trade.symbol} @ {current_price}")

    # 时间止损
    elif hours_held >= trade.max_hold_days * 24 and pnl_pct < config.TIME_STOP_MIN_PROFIT_PCT:
        remaining_notional = trade.stake_remaining * leverage
        remaining_pnl = remaining_notional * pnl_pct / 100
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        trade.pnl = round(total_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        trade.close_reason = f"时间止损（{hours_held:.1f}h，{pnl_pct:.1f}%）"
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.close_reason = f"⏰ 时间止损（持仓{hours_held:.1f}h，{pnl_pct:.1f}%）"
        result.alert_msg = (
            f"⏰ <b>时间止损触发</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"持仓{hours_held:.1f}小时，盈利仅{pnl_pct:.1f}%，强制平仓\n"
            f"盈亏：<b>{total_pnl:+.2f}U</b> ✅"
        )
        logger.info(f"[时间止损] {trade.symbol} 持仓{hours_held:.1f}h")

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
        "📊 <b>影子空单日报</b>（杠杆模式）",
        f"⏰ {utcnow_iso()[:16].replace('T', ' ')} UTC",
        f"📐 杠杆：{config.LEVERAGE}x | 保证金/单：{config.DEFAULT_STAKE}U",
        "",
    ]

    for trade in open_trades:
        try:
            current = binance.fetch_ticker(trade.symbol)['last']
        except Exception as e:
            logger.warning(f"获取价格失败 ({trade.symbol}): {e}")
            current = trade.entry_price

        result = evaluate_trade(trade, current)

        if result.updated:
            any_updated = True

        # 平仓时记录风控
        if result.closed:
            record_trade_closed(result.pnl_usd, trade.stake_remaining)

        # 触发推送
        if result.alert_msg:
            send_tg(result.alert_msg)

        # 日报内容
        if not check_only:
            if result.close_reason:
                lines.append(f"{result.close_reason} <b>{trade.symbol}</b>")
                lines.append(f"   入场: {trade.entry_price:.5f} → 现价: {current:.5f}")
                lines.append(f"   盈亏: <b>{result.pnl_usd:+.2f}U ({result.pnl_pct:+.1f}%×{trade.leverage}x)</b>")
            else:
                emoji = "🟢" if result.pnl_pct > 0 else "🔴"
                lines.append(f"{emoji} <b>{trade.symbol}</b> 做空持仓中")
                lines.append(f"   入场: {trade.entry_price:.5f} | 现价: {current:.5f}")
                lines.append(
                    f"   浮盈: <b>{result.pnl_pct:+.1f}%×{trade.leverage}x = {result.pnl_usd:+.2f}U</b>"
                )
                trail_str = f" | 移动止损: {trade.trail_stop_price:.5f}" if trade.trail_stop_price else ""
                hard_str = f" | 硬止损: {trade.hard_stop_price:.5f}" if trade.hard_stop_price else ""
                if trade.tp1_triggered:
                    lines.append(
                        f"   ✅TP1已锁定{trade.tp1_locked_pnl:+.2f}U | "
                        f"TP2: {trade.take_profit_2:.5f}(-{(1-config.TP2_MULTIPLIER)*100:.0f}%){trail_str}"
                    )
                else:
                    lines.append(
                        f"   TP1: {trade.take_profit_1:.5f}(-{(1-config.TP1_MULTIPLIER)*100:.0f}%) | "
                        f"TP2: {trade.take_profit_2:.5f}(-{(1-config.TP2_MULTIPLIER)*100:.0f}%){hard_str}{trail_str}"
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
        lines.append("")
        # 风控状态
        lines.append(get_risk_summary())

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
