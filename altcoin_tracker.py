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
    setup_logger, send_tg, load_json, tg_escape,
    utcnow_iso, hold_hours, LockedJsonFile,
    account_param,
)
from models import Trade, CloseType
from risk_control import record_trade_closed, release_partial_stake

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

    # M-1 修复：TP1 触发时的"半仓平仓"风控记账事件。
    # 不为 None 表示上层应在出锁后调用 record_trade_closed(pnl, stake) 一次，
    # 这样 risk_state.total_open_stake 在 TP1 时就同步减半，避免 TP1+TP2 流程
    # 结束后留下"+50% 虚高"持仓。
    # 元组语义：(pnl_usd, stake_to_release)
    pending_risk_partial: Optional[tuple] = None


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

    # 阶段 2（2026-05）：所有展示文本用 ALLOWED 字段按该 trade 自己的
    # account_id 取，避免显示成"活跃账号阈值"造成误导
    _t_acc = getattr(trade, 'account_id', None) or None
    _hard_stop_pct = float(account_param(_t_acc, 'HARD_STOP_LOSS_PCT',
                                         config.HARD_STOP_LOSS_PCT))
    _tp1_mult = float(account_param(_t_acc, 'TP1_MULTIPLIER', config.TP1_MULTIPLIER))
    _tp2_mult = float(account_param(_t_acc, 'TP2_MULTIPLIER', config.TP2_MULTIPLIER))
    _tp1_ratio = float(account_param(_t_acc, 'TP1_CLOSE_RATIO', config.TP1_CLOSE_RATIO))

    # L-7 修复：异常价格守卫，防止 entry=0 / 数据迁移残留造成 ZeroDivisionError
    if entry <= 0 or current_price <= 0:
        logger.error(
            f"⚠️ Trade {trade.id} 价格异常 entry={entry} current={current_price}，"
            f"本轮跳过评估（不更新字段、不平仓）"
        )
        return EvalResult(pnl_pct=0.0, pnl_usd=0.0, current_price=current_price)

    # 异常数据守卫：shares==0（历史迁移可能产生）会让 remaining_shares 计算
    # 全部失效，且交易所平仓数量为 0 必然报错。直接跳过本轮，由下次评估或人工介入处理。
    if trade.shares <= 0:
        logger.error(
            f"⚠️ Trade {trade.id} shares={trade.shares} 异常，本轮跳过评估"
        )
        return EvalResult(pnl_pct=0.0, pnl_usd=0.0, current_price=current_price)

    # 方向盈亏
    if trade.direction == 'LONG':
        pnl_pct = (current_price - entry) / entry * 100  # 做多：涨=赚
    else:
        pnl_pct = (entry - current_price) / entry * 100  # 做空：跌=赚

    # 杠杆后的名义仓位盈亏
    notional_remaining = trade.stake_remaining * leverage
    pnl_usd = notional_remaining * pnl_pct / 100

    # 剩余数量（用于真实平仓）
    # H7: TP1 后优先用交易所实际返回的 tp1_closed_shares 反推剩余仓位，
    # 避免因 TP1 平仓滑点导致系统记账数量和交易所实际持仓不符（小零头残留）。
    # 如果没有 tp1_closed_shares（影子交易 / 老数据），回退到按 stake 比例估算。
    if not trade.tp1_triggered:
        remaining_shares = trade.shares
    elif trade.tp1_closed_shares and trade.tp1_closed_shares > 0:
        remaining_shares = max(0.0, trade.shares - trade.tp1_closed_shares)
    elif trade.stake > 0:
        # M1: stake==0 已通过上面 elif 排除，此处是正常回退路径
        remaining_shares = trade.shares * trade.stake_remaining / trade.stake
    else:
        # M1: stake 为 0 是异常数据（历史迁移可能产生）→ 显式 warn
        logger.warning(
            f"⚠️ Trade {trade.id} stake==0 且 tp1_closed_shares==0，"
            f"remaining_shares 回退到全量 trade.shares，建议检查数据迁移"
        )
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
        # 平仓滑点：记录触发时的参考价；真实 fill 价由 _perform_exchange_close 的
        # 后台线程回填，用于事后分析止损滑点成本（硬止损通常是滑点最大的场景）
        trade.exit_ref_price = current_price
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.pending_exchange_action = 'full_close'
        result.pending_close_amount = remaining_shares
        result.close_reason = f"🛑 硬止损触发（+{_hard_stop_pct}%，合计{total_pnl:+.1f}U）"
        result.alert_msg = (
            f"🛑 <b>硬止损触发</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"价格反弹：{-pnl_pct:.2f}% > 止损线{_hard_stop_pct}%\n"
            f"合计盈亏：<b>{total_pnl:+.2f}U</b>（TP1锁定{trade.tp1_locked_pnl:+.2f} + 剩余{remaining_pnl:+.2f}，保证金{trade.stake}U×{leverage}x）\n"
            f"已自动平仓，严格执行纪律 ✅"
        )
        logger.info(f"[硬止损] {trade.symbol} @ {current_price}, 合计 {total_pnl:+.2f}U")
        trade.current_price = current_price
        return result

    # ── 更新移动止损最高盈利 ──
    if pnl_pct > trade.best_pnl_pct:
        trade.best_pnl_pct = round(pnl_pct, 2)
        # M5: 使用相对回撤比例（从最高盈利回撤 RATIO × best_pnl_pct 即触发）
        # 例如 best=5%, ratio=0.4 → trigger_pct = 5% * (1-0.4) = 3%
        # 做空 trail_stop = entry * (1 - trigger_pct/100)
        retrace_ratio = getattr(config, 'TRAIL_STOP_RETRACE_RATIO', 0.4)
        trigger_pnl_pct = trade.best_pnl_pct * (1 - retrace_ratio)
        if trade.direction == 'LONG':
            # 做多：止损价 = entry * (1 + trigger_pnl_pct/100)（价格上方一点）
            trail_price = round(entry * (1 + trigger_pnl_pct / 100), 6)
            # TP1已触发后：保本止损升级，止损不低于入场价
            if trade.tp1_triggered:
                trail_price = max(trail_price, entry)
            trade.trail_stop_price = trail_price
        else:
            # 做空：止损价 = entry * (1 - trigger_pnl_pct/100)（价格下方一点）
            trail_price = round(entry * (1 - trigger_pnl_pct / 100), 6)
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
        # 阶段 2：用 _tp1_ratio（per-account），不再读全局 config.TP1_CLOSE_RATIO
        locked_notional = trade.stake * _tp1_ratio * leverage
        locked_pnl = locked_notional * pnl_pct / 100
        trade.tp1_locked_pnl = round(locked_pnl, 2)
        # M-1 修复：TP1 平仓后，被释放的保证金 = stake × TP1_CLOSE_RATIO
        # 上层在出锁后会用它调 record_trade_closed，更新 total_open_stake
        tp1_released_stake = trade.stake * _tp1_ratio
        trade.stake_remaining = trade.stake * (1 - _tp1_ratio)
        # 真实平仓：TP1 半仓（发单前的全量 shares × TP1_CLOSE_RATIO）
        tp1_close_amount = trade.shares * _tp1_ratio
        # H7: 先按计划值记录 tp1_closed_shares（影子交易路径不会被后续回填覆盖）
        # 真实实盘路径由 _perform_exchange_close 的 _backfill_order_id 用 filled 实际值覆盖
        trade.tp1_closed_shares = round(tp1_close_amount, 6)
        trade.tp1_exit_price = current_price
        # 平仓滑点：TP1 触发时记录参考价；shadow 交易保持 fill==ref → 滑点 0
        trade.tp1_exit_ref_price = current_price
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
        # M-1 修复：让上层在出锁后通过 record_trade_closed 把这部分 stake 从
        # total_open_stake 中扣减，避免 TP1+TP2 流程结束后 +50% 虚高漂移。
        # 注意 pnl 是 TP1 实际锁定的盈利（正值），不会触发连亏计数。
        result.pending_risk_partial = (round(locked_pnl, 2), round(tp1_released_stake, 4))
        result.alert_msg = (
            f"🎯 <b>第一档止盈触发（-{(1-_tp1_mult)*100:.0f}%）</b>\n\n"
            f"币种：<b>{trade.symbol}</b>\n"
            f"入场价：{entry:.5f} → 现价：{current_price:.5f}\n"
            f"锁定盈利：<b>{locked_pnl:+.2f}U</b>（{int(_tp1_ratio*100)}%仓位）\n"
            f"名义仓位：{trade.stake}×{leverage}x → 剩余{trade.stake_remaining}×{leverage}x\n"
            f"保本止损已激活：剩余仓位止损 = 入场价\n"
            f"剩余等待TP2（-{(1-_tp2_mult)*100:.0f}%）✅"
        )
        logger.info(f"[TP1] {trade.symbol} @ {current_price}, 锁定 {locked_pnl:+.2f}U, 保本止损已激活")

    # TP2 跳空穿越保护：如果价格一次穿越 TP1 和 TP2（跳空行情），
    # 直接全平 TP1 + TP2 两档，避免只平 TP1 后剩余仓位开回错过 TP2
    if tp1_hit and trade.take_profit_2:
        if (trade.direction == 'SHORT' and current_price <= trade.take_profit_2) or            (trade.direction == 'LONG' and current_price >= trade.take_profit_2):
            trade.tp1_triggered = True
            trade.current_price = current_price
            # 合并 TP1 和 TP2：TP1 半仓已设，修改 close_type 让后续全平剩余 50%
            result.pending_exchange_action = 'tp2'
            result.pending_close_amount = trade.shares  # 全平
            result.alert_msg = (
                f"🎯 <b>两档止盈同时触发（TP2跳空穿越）</b>\n\n"
                f"币种：<b>{trade.symbol}</b>\n"
                f"入场价：{trade.entry_price:.5f} → 现价：{current_price:.5f}\n"
                f"价格一次穿越 TP1 和 TP2，全仓平仓 ✅"
            )
            logger.info(f"[TP2-GAP] {trade.symbol} @ {current_price}, 跳空穿越 TP1+TP2 全平")
            return result

    # TP1 刚触发时不再继续检查 TP2（避免同 tick 双触发导致平仓数量错误）
    if tp1_hit:
        trade.current_price = current_price
        return result

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
        trade.close_reason = f"TP2止盈-{(1-_tp2_mult)*100:.0f}%全仓平仓"
        trade.close_type = CloseType.TP2
        trade.exit_ref_price = current_price
        total_pnl = trade.tp1_locked_pnl + remaining_pnl
        result.closed = True
        result.updated = True
        result.pnl_usd = round(total_pnl, 2)
        result.pending_exchange_action = 'full_close'
        result.pending_close_amount = remaining_shares
        result.close_reason = f"✅ 第二档止盈（-{(1-_tp2_mult)*100:.0f}%，全仓平仓）"
        result.alert_msg = (
            f"🎯 <b>第二档止盈触发（-{(1-_tp2_mult)*100:.0f}%全仓平仓）</b>\n\n"
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
        trade.exit_ref_price = current_price
        total_pnl = trade.tp1_locked_pnl + remaining_pnl

        # 区分保本止损和普通移动止损
        is_breakeven_stop = trade.tp1_triggered and pnl_pct <= 0.5
        if is_breakeven_stop:
            trade.close_reason = "保本止损（TP1后价格回到入场价附近）"
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
        trade.exit_ref_price = current_price
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
#  价格获取（多源 fallback）
# ══════════════════════════════════════════════════════════════════

def _fetch_price_multi_source(binance_exchange, symbol: str) -> Optional[float]:
    """
    多源获取最新价：优先 Binance，失败回退 OKX。
    两个都失败返回 None — 上层决定如何处理（此时应跳过本轮而非用 entry_price）。
    """
    # 尝试 Binance
    try:
        price = binance_exchange.fetch_ticker(symbol)['last']
        if price and price > 0:
            return float(price)
    except Exception as e:
        logger.debug(f"Binance fetch_ticker 失败 ({symbol}): {e}")

    # Fallback 到 OKX
    try:
        from exchange_manager import get_okx
        okx = get_okx()
        if okx is not None:
            price = okx.fetch_ticker(symbol)['last']
            if price and price > 0:
                logger.info(f"价格 fallback 到 OKX: {symbol} @ {price}")
                return float(price)
    except Exception as e:
        logger.debug(f"OKX fetch_ticker 失败 ({symbol}): {e}")

    return None


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

    # ── 幂等防重：如果另一个进程/线程已经成功发过平仓单，跳过 ──
    # 场景：realtime_monitor(WS 触发) 和 altcoin_tracker(10分钟定时) 可能在
    # 短窗口内对同一笔 trade 同时调用 _perform_exchange_close。虽然交易所端
    # 通过 client_order_id 幂等保护不会重复成交，但多余的 API 调用仍浪费资源
    # 并产生多余的错误日志。这里通过检查 JSON 文件中的最新状态做前置去重。
    try:
        _latest_trades = load_json(TRADES_FILE, [])
        for _t in _latest_trades:
            if _t.get('id') != trade.id:
                continue
            # full_close: 如果已有 close_order_id，说明平仓单已成功发送
            if action == 'full_close' and _t.get('close_order_id'):
                logger.info(
                    f"⏩ 跳过重复平仓 {trade.symbol}（已有 close_order_id="
                    f"{_t['close_order_id']}，另一进程已处理）"
                )
                return
            # tp1_partial: 仅当已有 TP1 真实订单 ID 时才视为已处理
            if action == 'tp1_partial' and _t.get('tp1_close_order_id'):
                logger.info(
                    f"⏩ 跳过重复 TP1 平仓 {trade.symbol}（已有 tp1_close_order_id="
                    f"{_t['tp1_close_order_id']}，另一进程已处理）"
                )
                return
            break
    except Exception as _dedup_err:
        # 去重检查失败不应阻塞平仓主流程（交易所端幂等键仍是最后防线）
        logger.debug(f"平仓去重检查异常（非致命）: {_dedup_err}")

    from live_executor import execute_close, make_client_order_id, cancel_binance_open_orders, place_binance_stage2_after_tp1, get_binance_position_amount

    # 去重保护：在锁内写入 close_in_progress，防止 tracker 和 realtime_monitor 同时发单
    # 两个进程都看到 close_in_progress==null → 都发市价单 → 仓位过度平仓
    from common import LockedJsonFile, TRADES_FILE
    _dedup_signal = None
    try:
        with LockedJsonFile(TRADES_FILE, default=[], lock_timeout_sec=2) as (_td, _tsv):
            for _tt in _td:
                if _tt.get('id') == trade.id:
                    _existing = _tt.get('close_in_progress')
                    if _existing:
                        _dedup_signal = _existing
                        break
                    _tt['close_in_progress'] = action
                    _tsv(_td)
                    break
    except Exception:
        pass
    if _dedup_signal:
        logger.info(f"⏩ 跳过重复平仓（close_in_progress={_dedup_signal}）: {trade.symbol}")
        return

    # 幂等键：同一笔交易的同一次动作（tp1 vs close）用稳定 ID
    prefix = 'tp1' if action == 'tp1_partial' else 'cls'
    coid = make_client_order_id(prefix, f"{trade.id[:10]}{trade.symbol}",
                                 exchange_name=trade.exchange)

    # 使用开仓时记录的 account_id，确保平仓用正确账户的凭证
    acc_id = trade.account_id if trade.account_id else None

    # 交易所托管保护单 guard：先看实时持仓，避免重复平仓
    if trade.exchange == 'binance':
        live_pos_before = get_binance_position_amount(trade.symbol, trade.direction, account_id=acc_id)
        if action == 'tp1_partial':
            # 若交易所已先成交部分/全部，说明 TP1 可能已由条件单完成；
            # 不再重复发单，但仍继续后续清理/回写阶段。
            if live_pos_before <= 0:
                logger.info(f"⏩ TP1 guard: {trade.symbol} 持仓已为0，跳过发单，继续清理挂单")
                close_amount = 0.0
            else:
                close_amount = min(close_amount, live_pos_before)
        elif action == 'full_close':
            if live_pos_before <= 0:
                logger.info(f"⏩ full_close guard: {trade.symbol} 持仓已为0，跳过发单，继续清理挂单")
                close_amount = 0.0
            else:
                close_amount = min(close_amount, live_pos_before)

    # 若 guard 判定交易所已先成交导致可平数量为 0：不再发单，走后续清理分支
    if close_amount <= 0:
        result = {"success": True, "order_id": "ALREADY_CLOSED", "price": 0, "amount": 0, "error": ""}
        last_error = None
        max_retries = 0
    else:
        # 最多重试 3 次（指数退避：0.5s → 1s → 2s），覆盖网络抖动和限速
        import time as _time
        max_retries = 3
        result = None
        last_error = None

    for attempt in range(max_retries):
        try:
            result = execute_close(
                trade.symbol, trade.direction, close_amount,
                exchange_name=trade.exchange, client_order_id=coid,
                account_id=acc_id,
            )
            if result.get("success"):
                break  # 成功，跳出重试
            last_error = result.get('error', '未知错误')
            # 幂等键保证不会重复成交，安全重试
            logger.warning(
                f"[{trade.exchange}] 平仓尝试 {attempt+1}/{max_retries} 失败 "
                f"{trade.symbol}: {last_error}"
            )
        except Exception as e:
            last_error = str(e)
            logger.warning(
                f"[{trade.exchange}] 平仓尝试 {attempt+1}/{max_retries} 异常 "
                f"{trade.symbol}: {e}"
            )
            result = None

        if attempt < max_retries - 1:
            _time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s

    # 所有重试都失败
    if result is None or not result.get("success"):
        error_msg = last_error or "未知错误"
        logger.error(
            f"❌ [{trade.exchange}] 平仓失败（{max_retries}次重试后）{trade.symbol}: {error_msg}"
        )
        # 标记 trade 需要在下轮 tracker/monitor 重试平仓（避免悬仓无人管）
        try:
            with LockedJsonFile(TRADES_FILE, default=[]) as (trades_raw, save):
                for t in trades_raw:
                    if t.get('id') == trade.id:
                        t['close_retry_pending'] = True
                        t['close_retry_action'] = action
                        t['close_retry_amount'] = close_amount
                        t['close_retry_last_error'] = error_msg
                        save(trades_raw)
                        break
        except Exception as _e:
            logger.debug(f"标记 close_retry_pending 失败（非致命）: {_e}")
        send_tg(
            f"🚨 <b>[{tg_escape(trade.exchange.upper())}] 自动平仓失败（已重试{max_retries}次，已排队下轮重试）</b>\n\n"
            f"币种：{tg_escape(trade.symbol)}\n"
            f"动作：{tg_escape(action)}\n"
            f"原因：{tg_escape(error_msg)}\n\n"
            f"⚠️ 系统会在下一轮 tracker/monitor 继续尝试平仓；\n"
            f"如果持续失败请立即手动到交易所平仓！"
        )
        return

    # 成功：把交易所订单 ID 回填到 trade（后台线程，避免阻塞 WebSocket 消息处理线程）
    # 这里用 best-effort 模式：失败不重试（订单已成交，ID 只是审计信息）
    # H7: TP1 部分平仓成功时，把交易所返回的 filled 数量写回 trade.tp1_closed_shares
    #     后续评估 remaining_shares 时优先使用它，避免小零头残留
    # 平仓滑点：同时用返回的 average 成交价回填 tp1_slippage_pct / exit_slippage_pct
    def _backfill_order_id():
        try:
            with LockedJsonFile(TRADES_FILE, default=[]) as (trades_raw, save):
                for t in trades_raw:
                    if t.get('id') == trade.id:
                        if action == 'tp1_partial':
                            t['tp1_close_order_id'] = result.get('order_id', '')
                        else:
                            t['close_order_id'] = result.get('order_id', '')
                        fill_price = float(result.get('price') or 0)
                        if action == 'tp1_partial':
                            # H7: TP1 实际成交数量回填（交易所滑点 → 可能和预期略有差异）
                            filled = float(result.get('amount') or 0) or close_amount
                            t['tp1_closed_shares'] = round(filled, 6)
                            if fill_price > 0:
                                t['tp1_exit_price'] = round(fill_price, 6)
                                # 平仓滑点：用 evaluate 时写入的 ref 价算 abs(fill-ref)/ref*100
                                ref = float(t.get('tp1_exit_ref_price') or 0)
                                if ref > 0:
                                    t['tp1_slippage_pct'] = round(
                                        abs(fill_price - ref) / ref * 100, 4
                                    )
                        else:
                            # full_close：tp2 / hard_stop / trail_stop / time_stop 等
                            if fill_price > 0:
                                ref = float(t.get('exit_ref_price') or 0)
                                if ref > 0:
                                    t['exit_slippage_pct'] = round(
                                        abs(fill_price - ref) / ref * 100, 4
                                    )
                        # 成功平仓后清除所有 retry 标记
                        t.pop('close_retry_pending', None)
                        t.pop('close_retry_action', None)
                        t.pop('close_retry_amount', None)
                        t.pop('close_retry_last_error', None)
                        save(trades_raw)
                        break
        except Exception as e:
            logger.debug(f"回填 close_order_id 失败（非致命）: {e}")

    import threading as _threading
    _backfill_order_id()

    # 清理 close_in_progress 标记
    try:
        with LockedJsonFile(TRADES_FILE) as (_td_c, _sv_c):
            for _t_c in _td_c:
                if _t_c.get('id') == trade.id:
                    _t_c.pop('close_in_progress', None)
                    _sv_c(_td_c)
                    break
    except Exception:
        pass

    logger.info(
        f"✅ [{trade.exchange}] 实盘平仓成功: {trade.symbol} | "
        f"动作={action} | 数量={close_amount:.4f} | 订单={result.get('order_id')}"
    )

    # TP1 成功后，切换为阶段二保护单：取消旧单 -> 挂保本/移动止损 + TP2
    if action == 'tp1_partial' and trade.exchange == 'binance':
        try:
            _cancel = cancel_binance_open_orders(trade.symbol, account_id=acc_id)
            if not _cancel.get('success'):
                logger.warning(f"TP1后撤旧保护单失败 {trade.symbol}: {_cancel.get('error')}")

            # 剩余仓位数量：优先取交易所回填 filled
            remain_amount = max(0.0, float(trade.shares or 0) - float(result.get('amount') or close_amount or 0))
            if remain_amount <= 0:
                logger.info(f"TP1 后剩余仓位为 0，跳过阶段二挂单 {trade.symbol}")
                return

            # 做空：保本/移动止损价取 trail_stop_price（已在 evaluate 后置为保本或更优）
            stop_price = float(trade.trail_stop_price or trade.entry_price or 0)
            tp2_price = float(trade.take_profit_2 or 0)
            _p2 = place_binance_stage2_after_tp1(
                symbol=trade.symbol,
                remain_amount=remain_amount,
                stop_price=stop_price,
                tp2_price=tp2_price,
                account_id=acc_id,
                stop_client_order_id=make_client_order_id('st2', trade.symbol, 'binance'),
                tp2_client_order_id=make_client_order_id('tp2', trade.symbol, 'binance'),
            )
            if not _p2.get('success'):
                send_tg(f"[BINANCE] TP1 stage2 protection failed | symbol={tg_escape(trade.symbol)} | error={tg_escape(_p2.get('error','unknown'))}")
            else:
                try:
                    with LockedJsonFile(TRADES_FILE, default=[]) as (_trs, _save):
                        for _t in _trs:
                            if _t.get('id') == trade.id:
                                _t['protect_stop_algo_id'] = _p2.get('stop_order_id')
                                _t['protect_tp_algo_id'] = _p2.get('tp_order_id')
                                _t['protect_stage'] = 'stage2'
                                _save(_trs)
                                break
                except Exception as _we:
                    logger.debug(f"stage2 algo id backfill failed {trade.symbol}: {_we}")
        except Exception as _p2e:
            logger.error(f"TP1 后阶段二挂单异常 {trade.symbol}: {_p2e}")

    # 全平成功后清理残留条件单
    if action == 'full_close' and trade.exchange == 'binance':
        try:
            cancel_binance_open_orders(trade.symbol, account_id=acc_id)
        except Exception as _ce:
            logger.debug(f"full_close 后清理残单异常 {trade.symbol}: {_ce}")


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
    has_open = any(t.get('status') == 'open' for t in trades_raw)
    has_retry = any(t.get('close_retry_pending') for t in trades_raw)
    if not has_open and not has_retry:
        logger.info("无持仓中的空单")
        return

    # H10: 走 exchange_manager 单例，强制带 timeout
    from exchange_manager import get_binance
    binance = get_binance()

    # 日报行
    # 阶段 2：日报标题里展示用全局阈值（取活跃账号；多账号场景应改为按账号分组日报，
    # 但 run() 当前是单账号视图，按当前活跃账号取已经是最贴近原意的语义）
    from common import get_current_account_id as _gcid
    _run_acc = _gcid() or None
    _run_lev = int(account_param(_run_acc, 'LEVERAGE', config.LEVERAGE))
    _run_stake = float(account_param(_run_acc, 'DEFAULT_STAKE', config.DEFAULT_STAKE))
    lines = [
        "📊 <b>影子空单日报</b>（杠杆模式）",
        f"⏰ {utcnow_iso()[:16].replace('T', ' ')} UTC",
        f"📐 杠杆：{_run_lev}x | 保证金/单：{_run_stake:.0f}U",
        "",
    ]

    # 持锁 RMW：整段读-改-写在同一把锁里
    pending_risk_updates = []   # [(pnl_usd, stake_remaining), ...]
    pending_risk_partials = []  # M-1: TP1 部分平仓的 risk 记账 [(pnl, stake, account_id), ...]
    pending_alerts = []         # [alert_msg, ...]
    pending_exchange_closes = []  # [(trade_ref, action, amount), ...] 出锁后下真单
    pending_retry_closes = []    # [(trade_ref, action, amount), ...] 上轮失败的重试

    # 先处理上一轮标记了 close_retry_pending 的死状态 trade（不抢 trades 锁，仅读）
    trades_snapshot_for_retry = load_json(TRADES_FILE, [])
    for t in trades_snapshot_for_retry:
        if not t.get('close_retry_pending'):
            continue
        if t.get('exchange') == 'shadow':
            continue  # 影子交易无需真实平仓
        try:
            trade_obj = Trade.from_dict(t)
            pending_retry_closes.append((
                trade_obj,
                t.get('close_retry_action', 'full_close'),
                float(t.get('close_retry_amount', 0) or 0),
            ))
        except Exception as _e:
            logger.debug(f"跳过损坏的 retry trade: {_e}")

    with LockedJsonFile(TRADES_FILE, default=[]) as (trades_raw, save):
        trades = [Trade.from_dict(t) for t in trades_raw]
        open_trades = [t for t in trades if t.status == 'open']

        if not open_trades:
            # 进锁后发现状态变了（别的进程刚平完）
            logger.info("获锁后无持仓中的空单")
            return

        any_updated = False

        # ── 自动修复：TP1已触发但保本止损未正确设置的交易 ──
        # 兼容旧版本遗留数据：
        #   做空：trail_stop_price 应 <= entry（价格涨回入场价时保本平仓）
        #   做多：trail_stop_price 应 >= entry（价格跌回入场价时保本平仓）
        for trade in open_trades:
            if not trade.tp1_triggered:
                continue
            entry = trade.entry_price
            needs_fix = False
            if trade.trail_stop_price is None:
                needs_fix = True
            elif trade.direction == 'SHORT' and trade.trail_stop_price > entry:
                # 做空：止损在上方，保本 = 入场价，当前值比入场价高说明还是硬止损
                needs_fix = True
            elif trade.direction == 'LONG' and trade.trail_stop_price < entry:
                # 做多：止损在下方，保本 = 入场价，当前值比入场价低说明还是硬止损
                needs_fix = True

            if needs_fix:
                old_val = trade.trail_stop_price
                trade.trail_stop_price = entry
                any_updated = True
                logger.warning(f"🔧 修复保本止损: {trade.symbol} trail_stop {old_val} → {entry}")

        # ── 自动补挂：实盘持仓缺保护单的补挂 ──
        from live_executor import place_binance_short_protection_split
        for trade in open_trades:
            if trade.exchange == 'shadow':
                continue
            if trade.protect_stop_algo_id and trade.protect_tp_algo_id:
                continue
            logger.warning(f"🔧 检测到缺保护单: {trade.symbol} stop={trade.protect_stop_algo_id} tp={trade.protect_tp_algo_id}")
            tp1_ratio = float(account_param(trade.account_id, 'TP1_CLOSE_RATIO', config.TP1_CLOSE_RATIO))
            tp1_amount = trade.shares * tp1_ratio
            from live_executor import make_client_order_id
            prot = place_binance_short_protection_split(
                symbol=trade.symbol,
                stop_amount=trade.shares,
                tp_amount=tp1_amount,
                hard_stop_price=trade.hard_stop_price or 0.0,
                tp_trigger_price=trade.take_profit_1,
                account_id=trade.account_id or None,
                stop_client_order_id=make_client_order_id('st1-repair', trade.symbol, 'binance'),
                tp_client_order_id=make_client_order_id('tp1-repair', trade.symbol, 'binance'),
            )
            if prot.get('success'):
                trade.protect_stop_algo_id = prot.get('stop_order_id')
                trade.protect_tp_algo_id = prot.get('tp_order_id')
                trade.protect_stage = 'stage1'
                any_updated = True
                logger.info(f"✅ 补挂保护单成功: {trade.symbol} stop={prot['stop_order_id']} tp={prot['tp_order_id']}")

        for trade in open_trades:
            current = _fetch_price_multi_source(binance, trade.symbol)
            if current is None:
                logger.warning(f"获取价格失败（Binance+OKX 都不可用）: {trade.symbol}")
                send_tg(
                    f"⚠️ <b>价格获取失败</b>\n\n"
                    f"币种：{trade.symbol}\n"
                    f"Binance 和 OKX 两个数据源都不可用\n"
                    f"本轮跳过该仓位评估，等待下一轮或 WebSocket 覆盖"
                )
                continue

            result = evaluate_trade(trade, current)

            if result.updated:
                any_updated = True

            # 平仓时：把 risk 更新和推送推迟到 save 之后
            if result.closed:
                pending_risk_updates.append((result.pnl_usd, trade.stake_remaining, trade.account_id))
            # M-1: TP1 半仓平仓也要排队 risk 记账，避免 total_open_stake 漂移
            if result.pending_risk_partial:
                _ppnl, _pstake = result.pending_risk_partial
                pending_risk_partials.append((_ppnl, _pstake, trade.account_id))
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
                    # 阶段 2：按 trade.account_id 取 TP1/TP2（per-account 缩放感知）
                    _row_acc = getattr(trade, 'account_id', None) or None
                    _row_tp1m = float(account_param(_row_acc, 'TP1_MULTIPLIER', config.TP1_MULTIPLIER))
                    _row_tp2m = float(account_param(_row_acc, 'TP2_MULTIPLIER', config.TP2_MULTIPLIER))
                    if trade.tp1_triggered:
                        lines.append(
                            f"   ✅TP1已锁定{trade.tp1_locked_pnl:+.2f}U | "
                            f"TP2: {trade.take_profit_2:.5f}(-{(1-_row_tp2m)*100:.0f}%){trail_str}"
                        )
                    else:
                        lines.append(
                            f"   TP1: {trade.take_profit_1:.5f}(-{(1-_row_tp1m)*100:.0f}%) | "
                            f"TP2: {trade.take_profit_2:.5f}(-{(1-_row_tp2m)*100:.0f}%){hard_str}{trail_str}"
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
            # Dual-write closed/updated trades to DB
            try:
                from db.compat import save_trade, close_trade_compat
                for t in trades:
                    if t.status == 'closed' and t.close_type:
                        close_trade_compat(
                            trade_id=t.id, pnl=t.pnl,
                            close_price=t.current_price or 0,
                            close_reason=t.close_reason or '',
                            close_type=t.close_type,
                        )
                    else:
                        save_trade(t.to_dict())
            except Exception as _db_err:
                logger.debug(f"DB dual-write (tracker) failed (non-fatal): {_db_err}")
            # 立即刷新 realtime_monitor 的内存快照：
            # TP1 触发后 trail_stop_price 已变成保本止损，
            # 必须立刻反映到内存里，否则 realtime_monitor 的 30s 快照窗口内
            # 保本止损价格反弹不会触发
            try:
                from realtime_monitor import _refresh_snapshot_from_trades
                _refresh_snapshot_from_trades(trades)
            except Exception as _e:
                logger.debug(f"刷新 realtime_monitor 快照失败（非致命）: {_e}")

    # ══ 出锁后才执行副作用 ══
    # 0) 先重试上一轮失败的悬仓（不在本轮 evaluate 里，所以单独处理）
    for trade, action, close_amount in pending_retry_closes:
        if close_amount <= 0:
            continue
        logger.info(f"🔁 重试上轮失败的平仓: {trade.symbol} 动作={action} 数量={close_amount:.4f}")
        _perform_exchange_close(trade, action, close_amount)

    # 1) 实盘平仓（发真实订单）
    #    顺序很重要：save 已经落盘，此时发单失败也不会让 JSON 和交易所状态不一致
    #    （JSON 已经标记 closed，交易所还挂着 → 会在 TG 推送里告警，让用户手动处理）
    for trade, action, close_amount in pending_exchange_closes:
        _perform_exchange_close(trade, action, close_amount)

    # 2) 改 risk_state（在 trades 已经持久化之后）
    for pnl_usd, stake_remaining, acc_id in pending_risk_updates:
        # NF-4: 用 trade_account_id 让函数能区分"调用方未指定账户"与
        # "trade 自带账户标记（含老数据的空字符串）"，避免回退到当前活跃账户错记账
        record_trade_closed(pnl_usd, stake_remaining, trade_account_id=acc_id)

    # M-1: TP1 半仓的 stake 释放（不影响 daily_loss / consecutive_losses）
    for _ppnl, _pstake, _pacc in pending_risk_partials:
        # NF-4: 同上，用 trade_account_id 而不是 ``account_id=_pacc or None``
        release_partial_stake(_pstake, trade_account_id=_pacc)

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
