#!/usr/bin/env python3
"""
In-flight Journal 恢复器
在系统启动时扫描 trades_inflight.json 里的 pending 条目，反查交易所是否
有对应 clOrdId 的真实订单；若有但 trades.json 里没有则补录并告警。

设计原则：
  - 只做反查和告警，不自动补录仓位到 trades.json（避免自动复现严重状态错误）
  - 若发现交易所真实成交了但系统未记录，推 TG 告警要求人工处理
  - 扫描过程完全无副作用（除了清理 journal 里已确认/已失败的条目）
"""

from typing import Optional

from common import (
    TRADES_FILE, setup_logger, send_tg, load_json,
    journal_list_pending, journal_mark_confirmed,
    journal_mark_failed, journal_cleanup_failed,
)

logger = setup_logger("journal_recovery")


def _find_order_by_client_id(exchange_name: str, account_id: str,
                              symbol: str, client_order_id: str) -> Optional[dict]:
    """
    向交易所反查指定 clOrdId 是否有真实订单。
    返回成交的订单字典或 None。
    """
    try:
        if exchange_name == 'binance':
            from live_executor import get_live_exchange
            ex = get_live_exchange(account_id or None)
        elif exchange_name == 'okx':
            from live_executor import get_okx_live_exchange
            ex = get_okx_live_exchange(account_id or None)
        else:
            return None
        if ex is None:
            return None

        # Binance: 通过 origClientOrderId 查询
        if exchange_name == 'binance':
            try:
                order = ex.fetch_order(None, symbol, params={
                    'origClientOrderId': client_order_id,
                })
                return order
            except Exception as e:
                # -2013 order does not exist → 说明没成交
                if 'does not exist' in str(e).lower() or '-2013' in str(e):
                    return None
                logger.debug(f"Binance fetch_order 异常: {e}")
                return None

        # OKX: 用 get-order + clOrdId
        if exchange_name == 'okx':
            try:
                order = ex.fetch_order(None, symbol, params={
                    'clOrdId': client_order_id,
                })
                return order
            except Exception as e:
                if 'not exist' in str(e).lower() or '51603' in str(e):
                    return None
                logger.debug(f"OKX fetch_order 异常: {e}")
                return None

    except Exception as e:
        logger.warning(f"反查订单异常 [{exchange_name}] {client_order_id}: {e}")
        return None

    return None


def recover_inflight() -> dict:
    """
    启动时调用：扫描 journal 里的 pending，反查交易所。
    返回统计 dict: {'pending': int, 'recovered': int, 'ghost': int, 'cleared': int}
    """
    stats = {'pending': 0, 'recovered': 0, 'ghost': 0, 'cleared': 0}
    pendings = journal_list_pending()
    stats['pending'] = len(pendings)

    if not pendings:
        # 顺便清理过期的 failed 条目（超过 72h 的）
        stats['cleared'] = journal_cleanup_failed(retain_hours=72)
        logger.info(f"journal 检查：无 pending 条目 (清理旧 failed: {stats['cleared']})")
        return stats

    logger.warning(f"🔍 journal 发现 {len(pendings)} 条 pending，开始反查交易所...")

    trades_raw = load_json(TRADES_FILE, [])
    trades_by_coid = {
        t.get('client_order_id'): t for t in trades_raw if t.get('client_order_id')
    }

    ghost_orders = []  # 交易所有单但 trades.json 没记录的幽灵订单

    for entry in pendings:
        coid = entry.get('client_order_id', '')
        exch = entry.get('exchange', '')
        acc_id = entry.get('account_id', '')
        sym = entry.get('symbol', '')

        if not coid or not exch or not sym:
            continue

        # 1. 如果 trades.json 已经有这个 coid，说明写盘已成功，journal 忘记 confirm 了 → 清理
        if coid in trades_by_coid:
            logger.info(f"  ✅ {coid} 已在 trades.json，补 confirm")
            try:
                journal_mark_confirmed(coid, trades_by_coid[coid].get('live_order_id', ''))
                stats['recovered'] += 1
            except Exception as e:
                logger.error(f"补 confirm 失败: {e}")
            continue

        # 2. trades.json 没有 → 反查交易所
        order = _find_order_by_client_id(exch, acc_id, sym, coid)
        if order is None:
            # 交易所也没有 → 当时的下单确实失败了，清理
            logger.info(f"  ⏹️ {coid} 交易所无记录，标记为 failed")
            try:
                journal_mark_failed(coid, 'recovery: no order on exchange')
                stats['cleared'] += 1
            except Exception as e:
                logger.error(f"journal mark failed 异常: {e}")
            continue

        # 3. 交易所有真实订单但 trades.json 没有 → 幽灵仓位！
        status = order.get('status', '')
        filled = order.get('filled', 0)
        logger.critical(
            f"🚨 幽灵订单！{coid} | 交易所={exch} | 账户={acc_id} | "
            f"symbol={sym} | status={status} | filled={filled}"
        )
        ghost_orders.append({
            'coid': coid, 'exchange': exch, 'account_id': acc_id,
            'symbol': sym, 'order_id': order.get('id', ''),
            'status': status, 'filled': filled, 'avg_price': order.get('average'),
        })
        stats['ghost'] += 1

    # 幽灵订单汇总告警（让用户手工到交易所处理）
    if ghost_orders:
        summary_lines = [
            f"🚨🚨 <b>发现 {len(ghost_orders)} 个幽灵订单</b>\n",
            "交易所有真实订单但系统 trades.json 未记录！",
            "这通常发生于下单后写盘前崩溃。",
            "",
            "幽灵订单清单：",
        ]
        for g in ghost_orders:
            summary_lines.append(
                f"• {g['exchange'].upper()} {g['symbol']} "
                f"(acc={g['account_id']}) "
                f"order_id={g['order_id']} "
                f"status={g['status']} filled={g['filled']}"
            )
        summary_lines.append("")
        summary_lines.append("⚠️ 请立即到交易所手动核对并决定是平仓还是补录。")
        send_tg("\n".join(summary_lines))

    stats['cleared'] += journal_cleanup_failed(retain_hours=72)
    logger.info(
        f"journal 恢复完成: pending={stats['pending']} "
        f"recovered={stats['recovered']} ghost={stats['ghost']} cleared={stats['cleared']}"
    )
    return stats


if __name__ == '__main__':
    recover_inflight()
