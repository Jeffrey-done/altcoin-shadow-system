#!/usr/bin/env python3
"""
小币种影子空单追踪器 v4.1
功能：
  - 追踪持仓盈亏（杠杆仓位）
  - 硬止损（价格反弹 5% 无条件平仓）
  - 分批止盈（TP1 -5% 锁50%仓位，TP2 -8% 全仓平）
  - 移动止损（最高盈利回撤10%触发）
  - 时间止损（24小时且盈利<3%强制平）
  - 风控集成（平仓时记录盈亏）
  - 统一逻辑：--check-only 和日报共用同一套评估函数

关键数据语义（v4.1 修订，消除 TP1 双计数）：
  - trade.pnl 始终表示"剩余仓位"的盈亏：
      * 未触发 TP1：整笔仓位的浮动盈亏
      * TP1 已触发：剩余 50% 仓位的盈亏（浮动或平仓实现）
      * 最终关闭：剩余仓位的实现盈亏（不含 tp1_locked_pnl）
  - trade.tp1_locked_pnl 表示 TP1 已锁定的盈利（独立累加）
  - 任何地方都用 `tp1_locked_pnl + pnl` 得到该笔交易的总盈亏
  - result.pnl_usd 仍是"本次 evaluate 后该笔交易的累计盈亏"，
    供 record_trade_closed 作为单笔风控事件金额使用
"""

import sys
from dataclasses import dataclass
from typing import Optional

import ccxt

import config
from common import (
    TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    utcnow_iso, hold_days, hold_hours, LockedJsonFile,
)
from models import Trade, CloseType
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

    # v4.2：让上层知道要不要向交易所发真实平仓单
    #   'tp1_partial' → TP1 触发，需要半仓平仓（shares × TP1_CLOSE_RATIO）
    #   'full_close'  → 最终平仓（hard_stop / trail / time / tp2），需要 shares 全平
    #   None         → 只是更新字段，不用发单
    pending_exchange_action: Optional[str] = None
    pending_close_amount: float = 0.0


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

    # 剩余数量（用于真实平仓）
    # TP1 前：stake_remaining == stake，shares 就是全量
    # TP1 后：stake_remaining = stake * (1 - TP1_CLOSE_RATIO)
    # 按 stake 比例缩放 shares，避免 TP1 后 shares 字段未更新导致的偏差
    if trade.stake > 0:
        remaining_shares = trade.shares * trade.stake_remaining / trade.stake
    else:
        remaining_shares = trade.shares

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
        # 语义约定：trade.pnl = 仅"剩余仓位"的实现盈亏（不含 tp1_locked_pnl）
        remaining_pnl = notional_remaining * pnl_pct / 100
        trade.pnl = round(remaining_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        trade.close_reason = f"硬止损（价格反弹{-pnl_pct:.1f}%触发）"
        trade.close_type = CloseType.HARD_STOP
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.pending_exchange_action = 'full_close'
        result.pending_close_amount = remaining_shares
        result.close_reason = f"🛑 硬止损触发（+{config.HARD_STOP_LOSS_PCT}%，合计{total_pnl:+.1f}U）"
        result.alert_msg = (
            f"🛑 <b>硬止损触发</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"价格反弹：{-pnl_pct:.2f}% > 止损线{config.HARD_STOP_LOSS_PCT}%\n"
            f"合计盈亏：<b>{total_pnl:+.2f}U</b>（TP1锁定{trade.tp1_locked_pnl:+.2f} + 剩余{remaining_pnl:+.2f}，保证金{trade.stake}U×{leverage}x）\n"
            f"已自动平仓，严格执行纪律 ✅"
        )
        logger.info(f"[硬止损] {trade.symbol} @ {current_price}, 合计 {total_pnl:+.2f}U")
        trade.current_price = current_price
        return result

    # ── 更新移动止损最高盈利 ──
    if pnl_pct > trade.best_pnl_pct:
        trade.best_pnl_pct = round(pnl_pct, 2)
        trail_pct = config.TRAIL_STOP_DRAWDOWN_PCT
        if trade.direction == 'LONG':
            # 做多：止损价在下方
            trail_price = round(entry * (1 + pnl_pct / 100 - trail_pct), 6)
            # TP1已触发后：保本止损升级，止损不低于入场价
            if trade.tp1_triggered:
                trail_price = max(trail_price, entry)
            trade.trail_stop_price = trail_price
        else:
            # 做空：止损价在上方（越低越好）
            trail_price = round(entry * (1 - (pnl_pct / 100 - trail_pct)), 6)
            # TP1已触发后：保本止损升级，止损不高于入场价
            if trade.tp1_triggered:
                trail_price = min(trail_price, entry)
            trade.trail_stop_price = trail_price
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
        # 真实平仓：TP1 半仓（发单前的全量 shares × TP1_CLOSE_RATIO）
        tp1_close_amount = trade.shares * config.TP1_CLOSE_RATIO
        result.pending_exchange_action = 'tp1_partial'
        result.pending_close_amount = tp1_close_amount
        # TP1触发后立即启用保本止损：剩余仓位止损提升至入场价
        if trade.direction == 'LONG':
            trade.trail_stop_price = max(trade.trail_stop_price or 0, entry)
        else:
            # 做空：止损价越低越保守，入场价是最大允许值
            trade.trail_stop_price = entry if trade.trail_stop_price is None else min(trade.trail_stop_price, entry)
        # 剩余仓位浮盈（同步到 trade.pnl，保持"trade.pnl = 剩余仓位盈亏"的语义）
        new_notional = trade.stake_remaining * leverage
        remaining_pnl = new_notional * pnl_pct / 100
        trade.pnl = round(remaining_pnl, 2)
        result.pnl_usd = round(trade.tp1_locked_pnl + remaining_pnl, 2)
        result.updated = True
        result.alert_msg = (
            f"🎯 <b>第一档止盈触发（-{(1-config.TP1_MULTIPLIER)*100:.0f}%）</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"锁定盈利：<b>{locked_pnl:+.2f}U</b>（{int(config.TP1_CLOSE_RATIO*100)}%仓位）\n"
            f"名义仓位：{trade.stake}×{leverage}x → 剩余{trade.stake_remaining}×{leverage}x\n"
            f"保本止损已激活：剩余仓位止损 = 入场价\n"
            f"剩余等待TP2（-{(1-config.TP2_MULTIPLIER)*100:.0f}%）✅"
        )
        logger.info(f"[TP1] {trade.symbol} @ {current_price}, 锁定 {locked_pnl:+.2f}U, 保本止损已激活")

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
        trade.pnl = round(remaining_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        trade.close_reason = f"TP2止盈-{(1-config.TP2_MULTIPLIER)*100:.0f}%全仓平仓"
        trade.close_type = CloseType.TP2
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.pending_exchange_action = 'full_close'
        result.pending_close_amount = remaining_shares
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

    # 移动止损（含TP1后保本止损）
    trail_triggered = False
    if trade.trail_stop_price:
        # TP1已触发：保本止损无条件生效（不需要达到TRAIL_STOP_ACTIVATE_PCT）
        # TP1未触发：需要best_pnl_pct达到激活门槛
        trail_active = trade.tp1_triggered or (trade.best_pnl_pct >= config.TRAIL_STOP_ACTIVATE_PCT)
        if trail_active:
            if trade.direction == 'LONG' and current_price <= trade.trail_stop_price:
                trail_triggered = True
            elif trade.direction == 'SHORT' and current_price >= trade.trail_stop_price:
                trail_triggered = True

    if trail_triggered:
        remaining_notional = trade.stake_remaining * leverage
        remaining_pnl = remaining_notional * pnl_pct / 100
        trade.pnl = round(remaining_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        total_pnl = trade.tp1_locked_pnl + remaining_pnl

        # 区分保本止损和普通移动止损
        is_breakeven_stop = trade.tp1_triggered and pnl_pct <= 0.5
        if is_breakeven_stop:
            trade.close_reason = f"保本止损（TP1后价格回到入场价附近）"
            trade.close_type = CloseType.BREAKEVEN_STOP
            result.close_reason = f"🛡️ 保本止损（TP1已锁{trade.tp1_locked_pnl:+.2f}U，剩余保本平仓）"
            result.alert_msg = (
                f"🛡️ <b>保本止损触发</b>\n\n"
                f"币种：<b>{trade.symbol}</b>\n"
                f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
                f"TP1已锁定：<b>{trade.tp1_locked_pnl:+.2f}U</b>\n"
                f"剩余仓位：保本平仓（{remaining_pnl:+.2f}U）\n"
                f"<b>总盈亏：{total_pnl:+.2f}U</b>（保住了TP1利润）✅"
            )
        else:
            trade.close_reason = f"移动止损（最高{trade.best_pnl_pct:.1f}%→{pnl_pct:.1f}%）"
            trade.close_type = CloseType.TRAIL_STOP
            result.close_reason = f"🛑 移动止损（最高{trade.best_pnl_pct:.1f}%→{pnl_pct:.1f}%）"
            result.alert_msg = (
                f"🛑 <b>移动止损触发</b>\n\n"
                f"币种：<b>{trade.symbol}</b>\n"
                f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
                f"历史最高：{trade.best_pnl_pct:.1f}% | 当前：{pnl_pct:.1f}%\n"
                f"盈亏：<b>{total_pnl:+.2f}U</b>（{leverage}x杠杆）\n"
                f"已自动平仓 ✅"
            )

        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.pending_exchange_action = 'full_close'
        result.pending_close_amount = remaining_shares
        logger.info(f"[{'保本止损' if is_breakeven_stop else '移动止损'}] {trade.symbol} @ {current_price}")

    # 时间止损
    elif hours_held >= trade.max_hold_days * 24 and pnl_pct < config.TIME_STOP_MIN_PROFIT_PCT:
        remaining_notional = trade.stake_remaining * leverage
        remaining_pnl = remaining_notional * pnl_pct / 100
        trade.pnl = round(remaining_pnl, 2)
        trade.status = 'closed'
        trade.closed_at = utcnow_iso()
        trade.close_reason = f"时间止损（{hours_held:.1f}h，{pnl_pct:.1f}%）"
        trade.close_type = CloseType.TIME_STOP
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.pending_exchange_action = 'full_close'
        result.pending_close_amount = remaining_shares
        result.close_reason = f"⏰ 时间止损（持仓{hours_held:.1f}h，{pnl_pct:.1f}%）"
        result.alert_msg = (
            f"⏰ <b>时间止损触发</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"持仓{hours_held:.1f}小时，盈利仅{pnl_pct:.1f}%，强制平仓\n"
            f"盈亏：<b>{total_pnl:+.2f}U</b> ✅"
        )
        logger.info(f"[时间止损] {trade.symbol} 持仓{hours_held:.1f}h")

    # 无触发：更新浮盈（trade.pnl = 剩余仓位的浮动盈亏）
    else:
        remaining_notional = trade.stake_remaining * leverage
        trade.pnl = round(remaining_notional * pnl_pct / 100, 2)

    trade.current_price = current_price
    return result


# ══════════════════════════════════════════════════════════════════
#  实盘平仓辅助（tracker 和 realtime_monitor 共用）
# ══════════════════════════════════════════════════════════════════

def _perform_exchange_close(trade: Trade, action: str, close_amount: float) -> None:
    """
    根据 trade.exchange 调用对应交易所的 execute_close 发真实平仓单。

    参数:
      trade: 已经被 evaluate_trade 改好状态的 Trade 对象（status 可能已为 closed）
      action: 'tp1_partial' | 'full_close'
      close_amount: 实际平仓的币数量（shares）

    失败处理：
      - 推 TG 告警，要求用户去交易所手动平仓
      - 不回滚 JSON 状态（已落盘），避免状态机来回跳
      - 把错误信息追加到 trade.close_reason（但此时已出锁，无法再写 JSON；
        下一轮 tracker/monitor 会基于 closed 状态跳过）
    """
    if trade.exchange == 'shadow':
        return

    from live_executor import execute_close, make_client_order_id

    # 幂等键：同一笔交易的同一次动作（tp1 vs close）用稳定 ID
    # 这样即便 tracker 和 realtime_monitor 同时触发，交易所只会接受一次
    prefix = 'tp1' if action == 'tp1_partial' else 'cls'
    coid = make_client_order_id(prefix, f"{trade.id[:10]}{trade.symbol}",
                                 exchange_name=trade.exchange)

    try:
        result = execute_close(
            trade.symbol, trade.direction, close_amount,
            exchange_name=trade.exchange, client_order_id=coid,
        )
    except Exception as e:
        logger.error(f"❌ [{trade.exchange}] 平仓调用异常 {trade.symbol}: {e}")
        send_tg(
            f"🚨 <b>[{trade.exchange.upper()}] 自动平仓失败</b>\n\n"
            f"币种：{trade.symbol}\n"
            f"动作：{action}\n"
            f"异常：{e}\n\n"
            f"⚠️ JSON 已标记为平仓，但交易所可能仍有持仓，请立即手动检查！"
        )
        return

    if not result.get("success"):
        logger.error(
            f"❌ [{trade.exchange}] 平仓失败 {trade.symbol}: {result.get('error')}"
        )
        send_tg(
            f"🚨 <b>[{trade.exchange.upper()}] 自动平仓失败</b>\n\n"
            f"币种：{trade.symbol}\n"
            f"动作：{action}\n"
            f"原因：{result.get('error')}\n\n"
            f"⚠️ JSON 已标记为平仓，但交易所可能仍有持仓，请立即手动检查！"
        )
        return

    # 成功：把交易所订单 ID 回填到 trade（需要再抢一次锁写回）
    # 这里用 best-effort 模式：失败不重试（订单已成交，ID 只是审计信息）
    try:
        with LockedJsonFile(TRADES_FILE, default=[]) as (trades_raw, save):
            for t in trades_raw:
                if t.get('id') == trade.id:
                    t['close_order_id'] = result.get('order_id', '')
                    save(trades_raw)
                    break
    except Exception as e:
        logger.debug(f"回填 close_order_id 失败（非致命）: {e}")

    logger.info(
        f"✅ [{trade.exchange}] 实盘平仓成功: {trade.symbol} | "
        f"动作={action} | 数量={close_amount:.4f} | 订单={result.get('order_id')}"
    )


# ══════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════

def run(check_only: bool = False):
    """
    统一入口：
    - check_only=True：只检查止盈 / 止损触发，触发时单独推送
    - check_only=False：检查 + 推送日报汇总

    改为持锁 read-modify-write，防止与 realtime_monitor / scanner 并发覆盖。
    副作用（record_trade_closed / send_tg）一律在 save() 成功后再执行。
    """
    # 先用无锁读判断是否有持仓，避免空仓时也要抢锁
    trades_raw = load_json(TRADES_FILE, [])
    if not trades_raw:
        logger.info("无交易记录")
        return
    if not any(t.get('status') == 'open' for t in trades_raw):
        logger.info("无持仓中的空单")
        return

    binance = ccxt.binance({'enableRateLimit': True})

    # 日报行
    lines = [
        "📊 <b>影子空单日报</b>（杠杆模式）",
        f"⏰ {utcnow_iso()[:16].replace('T', ' ')} UTC",
        f"📐 杠杆：{config.LEVERAGE}x | 保证金/单：{config.DEFAULT_STAKE}U",
        "",
    ]

    # 持锁 RMW：整段读-改-写在同一把锁里
    pending_risk_updates = []   # [(pnl_usd, stake_remaining), ...]
    pending_alerts = []         # [alert_msg, ...]
    pending_exchange_closes = []  # [(trade_ref, action, amount), ...] 出锁后下真单

    with LockedJsonFile(TRADES_FILE, default=[]) as (trades_raw, save):
        trades = [Trade.from_dict(t) for t in trades_raw]
        open_trades = [t for t in trades if t.status == 'open']

        if not open_trades:
            # 进锁后发现状态变了（别的进程刚平完）
            logger.info("获锁后无持仓中的空单")
            return

        any_updated = False

        for trade in open_trades:
            try:
                current = binance.fetch_ticker(trade.symbol)['last']
            except Exception as e:
                logger.warning(f"获取价格失败 ({trade.symbol}): {e}")
                current = trade.entry_price

            result = evaluate_trade(trade, current)

            if result.updated:
                any_updated = True

            # 平仓时：把 risk 更新和推送推迟到 save 之后
            if result.closed:
                pending_risk_updates.append((result.pnl_usd, trade.stake_remaining))
            if result.alert_msg:
                pending_alerts.append(result.alert_msg)

            # 真实平仓动作：只要 evaluate 标记了 pending_exchange_action，就排队等出锁发单
            # 非 'shadow' 交易所才发单；影子交易跳过
            if result.pending_exchange_action and trade.exchange != 'shadow':
                pending_exchange_closes.append((
                    trade, result.pending_exchange_action, result.pending_close_amount,
                ))

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

        # 持仓浮盈汇总需要在写盘前、锁内、基于刚评估完的 trades 算
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

        # 先把交易数据落盘（在锁保护下）
        if any_updated:
            save([t.to_dict() for t in trades])
            logger.info("交易数据已更新并保存")

    # ══ 出锁后才执行副作用 ══
    # 1) 实盘平仓（发真实订单）
    #    顺序很重要：save 已经落盘，此时发单失败也不会让 JSON 和交易所状态不一致
    #    （JSON 已经标记 closed，交易所还挂着 → 会在 TG 推送里告警，让用户手动处理）
    for trade, action, close_amount in pending_exchange_closes:
        _perform_exchange_close(trade, action, close_amount)

    # 2) 改 risk_state（在 trades 已经持久化之后）
    for pnl_usd, stake_remaining in pending_risk_updates:
        record_trade_closed(pnl_usd, stake_remaining)

    # 3) 再推送 TG
    for msg in pending_alerts:
        send_tg(msg)

    # 3) 最后发日报（风控状态需要在 record_trade_closed 之后读，才是最新的）
    if not check_only:
        from risk_control import get_risk_summary as _get_summary
        lines.append("")
        lines.append(_get_summary())
        msg = "\n".join(lines)
        send_tg(msg)
        logger.info(msg)


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    check_only = '--check-only' in sys.argv
    run(check_only=check_only)
