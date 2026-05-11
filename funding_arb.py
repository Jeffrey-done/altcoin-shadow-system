#!/usr/bin/env python3
"""
资金费率套利扫描器 v1.0
策略逻辑：
  - 每8小时（结算前1小时）扫描全市场合约
  - 找资金费率 < -0.05%/8h 的币（空头付钱给多头）
  - 开多吃费率，跨过结算时间后平仓
  - 严格止损防止方向性亏损吃掉费率收入

收益预期（100U本金）：
  - 50U保证金 × 20x = 1000U名义仓位
  - 费率 -0.1%/8h → 收入 = 1000 × 0.1% = 1U/次
  - 一天最多3次结算 → 最多3U/天（低风险稳定收入）

用法：
  python3 funding_arb.py scan     # 扫描负费率币（结算前1小时运行）
  python3 funding_arb.py check    # 检查持仓（结算后运行，平仓）
  python3 funding_arb.py status   # 查看当前费率套利持仓
"""

import time
import requests
import ccxt

import config
from common import (
    FUNDING_TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    to_binance_symbol, utcnow_iso, hold_hours, today_str,
)
from models import FundingTrade
from risk_control import can_open_trade, record_trade_opened, record_trade_closed

logger = setup_logger("funding_arb")


# ══════════════════════════════════════════════════════════════════
#  数据获取
# ══════════════════════════════════════════════════════════════════

def get_all_funding_rates() -> list:
    """
    获取 Binance 合约市场所有币种的当前资金费率。
    返回: [{"symbol": "BTCUSDT", "lastFundingRate": -0.001, ...}, ...]
    """
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            timeout=10,
        )
        if r.status_code != 200:
            logger.error(f"获取费率列表失败: HTTP {r.status_code}")
            return []
        return r.json()
    except Exception as e:
        logger.error(f"获取费率列表异常: {e}")
        return []


def get_ticker_volume(symbol: str) -> float:
    """获取合约24h成交量（USDT）"""
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/24hr",
            params={"symbol": symbol},
            timeout=5,
        )
        if r.status_code != 200:
            return 0.0
        return float(r.json().get('quoteVolume', 0))
    except Exception as e:
        logger.debug(f"获取成交量失败 ({symbol}): {e}")
        return 0.0


# ══════════════════════════════════════════════════════════════════
#  扫描：找负费率币
# ══════════════════════════════════════════════════════════════════

def scan_negative_funding():
    """
    扫描负费率币种，符合条件则自动开多。
    条件：
      1. 费率 < FUNDING_ARB_MIN_RATE（如 -0.05%）
      2. 24h 成交量 > FUNDING_ARB_VOL_MIN（流动性好）
      3. 风控检查通过
      4. 今日费率套利次数未超限
    """
    if not config.FUNDING_ARB_ENABLED:
        logger.info("资金费率套利已关闭")
        return

    logger.info("=== 开始扫描负费率币 ===")

    # 检查今日已开单数
    trades = [FundingTrade.from_dict(t) for t in load_json(FUNDING_TRADES_FILE, [])]
    today = today_str()
    today_count = sum(
        1 for t in trades
        if t.opened_at.startswith(today)
    )
    if today_count >= config.FUNDING_ARB_MAX_DAILY:
        logger.info(f"今日费率套利已达上限（{today_count}/{config.FUNDING_ARB_MAX_DAILY}）")
        return

    # 风控检查
    allowed, reason = can_open_trade(config.FUNDING_ARB_STAKE)
    if not allowed:
        logger.warning(f"风控拒绝：{reason}")
        return

    # 获取所有费率
    all_rates = get_all_funding_rates()
    if not all_rates:
        return

    # 筛选负费率币
    candidates = []
    for item in all_rates:
        symbol = item.get('symbol', '')
        if not symbol.endswith('USDT'):
            continue

        rate = float(item.get('lastFundingRate', 0)) * 100  # 转为百分比
        if rate >= config.FUNDING_ARB_MIN_RATE:
            continue  # 费率不够负

        # 检查成交量
        vol = get_ticker_volume(symbol)
        if vol < config.FUNDING_ARB_VOL_MIN:
            continue

        candidates.append({
            'symbol': symbol,
            'rate': rate,
            'volume': vol,
            'mark_price': float(item.get('markPrice', 0)),
        })
        time.sleep(0.05)

    if not candidates:
        logger.info("未找到符合条件的负费率币")
        return

    # 按费率从低到高排序（越负越好）
    candidates.sort(key=lambda x: x['rate'])

    # 已持仓的币不重复开
    open_symbols = {t.symbol for t in trades if t.status == 'open'}

    logger.info(f"找到 {len(candidates)} 个负费率候选：")
    for c in candidates[:10]:
        logger.info(f"  {c['symbol']}: {c['rate']:.4f}%/8h | vol={c['volume']:,.0f}U")

    # 开仓（取费率最负的1个）
    opened = 0
    for c in candidates:
        if opened >= 1:  # 每次扫描最多开1单
            break

        ccxt_symbol = c['symbol'].replace('USDT', '/USDT')
        if ccxt_symbol in open_symbols:
            continue

        # 再次检查风控
        allowed, reason = can_open_trade(config.FUNDING_ARB_STAKE)
        if not allowed:
            logger.warning(f"风控拒绝第{opened+1}单：{reason}")
            break

        price = c['mark_price']
        if price <= 0:
            continue

        # 创建费率套利交易
        trade = FundingTrade.create_long(
            symbol=ccxt_symbol,
            price=price,
            funding_rate=c['rate'],
        )

        # 保存
        trades.append(trade)
        atomic_write_json(FUNDING_TRADES_FILE, [t.to_dict() for t in trades])
        record_trade_opened(config.FUNDING_ARB_STAKE)
        opened += 1

        logger.info(
            f"  ✅ 开多: {ccxt_symbol} @ {price:.6f} | "
            f"费率={c['rate']:.4f}% | 预期收入={trade.expected_income:.4f}U"
        )

        # TG 推送
        send_tg(
            f"💰 <b>费率套利开仓</b>\n\n"
            f"币种：<b>{ccxt_symbol}</b>\n"
            f"方向：做多（吃负费率）\n"
            f"入场价：{price:.6f}\n"
            f"保证金：{trade.stake}U × {trade.leverage}x = {trade.notional}U\n"
            f"当前费率：{c['rate']:.4f}%/8h\n"
            f"预期收入：{trade.expected_income:.4f}U\n"
            f"止损价：{trade.hard_stop_price:.6f}（-{config.FUNDING_ARB_STOP_LOSS_PCT}%）\n\n"
            f"结算后自动平仓 ⏰"
        )

    if opened == 0:
        logger.info("本次扫描未开仓（已持仓或风控限制）")


# ══════════════════════════════════════════════════════════════════
#  检查：结算后平仓 / 止损
# ══════════════════════════════════════════════════════════════════

def check_positions():
    """
    检查费率套利持仓：
    - 超过 MAX_HOLD_HOURS → 平仓（已跨过结算）
    - 价格跌破 hard_stop → 止损
    - 计算实际费率收入
    """
    trades = [FundingTrade.from_dict(t) for t in load_json(FUNDING_TRADES_FILE, [])]
    open_trades = [t for t in trades if t.status == 'open']

    if not open_trades:
        logger.info("无费率套利持仓")
        return

    exchange = ccxt.binance({'enableRateLimit': True})
    any_updated = False

    for trade in open_trades:
        try:
            ticker = exchange.fetch_ticker(trade.symbol)
            current = ticker['last']
        except Exception as e:
            logger.warning(f"获取价格失败 ({trade.symbol}): {e}")
            continue

        trade.current_price = current
        hours = hold_hours(trade.opened_at)

        # 做多盈亏
        pnl_pct = (current - trade.entry_price) / trade.entry_price * 100
        pnl_usd = trade.notional * pnl_pct / 100

        close_reason = None

        # 1. 硬止损
        if trade.hard_stop_price and current <= trade.hard_stop_price:
            close_reason = f"止损触发（价格跌{pnl_pct:.2f}%）"

        # 2. 时间到：已跨过结算
        elif hours >= trade.max_hold_hours:
            close_reason = f"结算完成（持仓{hours:.1f}h）"
            # 估算费率收入（已跨过一次结算）
            trade.funding_income = trade.expected_income

        if close_reason:
            trade.status = 'closed'
            trade.closed_at = utcnow_iso()
            trade.close_reason = close_reason
            trade.pnl = round(pnl_usd, 4)
            trade.total_pnl = round(pnl_usd + trade.funding_income, 4)
            any_updated = True

            # 风控记录
            record_trade_closed(trade.total_pnl, config.FUNDING_ARB_STAKE)

            emoji = "✅" if trade.total_pnl >= 0 else "❌"
            logger.info(
                f"  {emoji} 平仓 {trade.symbol}: {close_reason} | "
                f"方向PnL={pnl_usd:+.4f}U | 费率={trade.funding_income:+.4f}U | "
                f"总计={trade.total_pnl:+.4f}U"
            )

            send_tg(
                f"💰 <b>费率套利平仓</b> {emoji}\n\n"
                f"币种：<b>{trade.symbol}</b>\n"
                f"原因：{close_reason}\n"
                f"入场：{trade.entry_price:.6f} → 平仓：{current:.6f}\n"
                f"方向盈亏：{pnl_usd:+.4f}U\n"
                f"费率收入：{trade.funding_income:+.4f}U\n"
                f"<b>总盈亏：{trade.total_pnl:+.4f}U</b>"
            )
        else:
            trade.pnl = round(pnl_usd, 4)
            trade.total_pnl = round(pnl_usd, 4)  # 未结算，暂不算费率
            any_updated = True

    if any_updated:
        atomic_write_json(FUNDING_TRADES_FILE, [t.to_dict() for t in trades])


# ══════════════════════════════════════════════════════════════════
#  状态查看
# ══════════════════════════════════════════════════════════════════

def show_status():
    """打印费率套利状态"""
    trades = [FundingTrade.from_dict(t) for t in load_json(FUNDING_TRADES_FILE, [])]
    open_trades = [t for t in trades if t.status == 'open']
    closed_trades = [t for t in trades if t.status == 'closed']

    print("=== 资金费率套利状态 ===")
    print(f"持仓中：{len(open_trades)} 单")
    for t in open_trades:
        hours = hold_hours(t.opened_at)
        print(f"  {t.symbol} | 费率={t.funding_rate:.4f}% | 持仓{hours:.1f}h | 浮盈={t.pnl:+.4f}U")

    total_closed_pnl = sum(t.total_pnl for t in closed_trades)
    today = today_str()
    today_pnl = sum(
        t.total_pnl for t in closed_trades
        if t.closed_at and t.closed_at.startswith(today)
    )

    print(f"\n已平仓：{len(closed_trades)} 单")
    print(f"今日盈亏：{today_pnl:+.4f}U")
    print(f"总盈亏：{total_closed_pnl:+.4f}U")


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else 'scan'

    if mode == 'scan':
        scan_negative_funding()
    elif mode == 'check':
        check_positions()
    elif mode == 'status':
        show_status()
    else:
        print(f"用法: {sys.argv[0]} [scan|check|status]")
        sys.exit(1)
