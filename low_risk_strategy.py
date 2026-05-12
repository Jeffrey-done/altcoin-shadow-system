#!/usr/bin/env python3
"""
低风险日收策略 v1.0
目标：每日稳定收益 1-3%，最大回撤控制在 1% 以内

三大子策略：
  1. 网格交易（Grid）：在震荡币种上设置价格网格，低买高卖
  2. 均值回归（Mean Reversion）：低波动率币偏离均值时反向开仓
  3. 多币种费率收割（Multi-Funding）：同时在多个币上收负费率

风控特点：
  - Kelly 公式动态仓位管理
  - 日度止盈止损（达到目标或回撤上限自动停工）
  - 小仓位 + 低杠杆 + 分散投资

用法：
  python3 low_risk_strategy.py scan                    # 执行所有策略扫描
  python3 low_risk_strategy.py scan --mode grid        # 仅网格扫描
  python3 low_risk_strategy.py scan --mode mean        # 仅均值回归
  python3 low_risk_strategy.py scan --mode funding     # 仅多币费率
  python3 low_risk_strategy.py check                   # 检查持仓（止盈/止损/超时）
  python3 low_risk_strategy.py status                  # 查看今日状态
"""

import time
import math
import statistics
import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional, List

try:
    import ccxt
except ImportError:
    ccxt = None

try:
    import requests
except ImportError:
    requests = None

import config
from common import (
    LOW_RISK_TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    to_binance_symbol, utcnow_iso, utcnow, today_str, hold_hours,
)


logger = setup_logger("low_risk")
# ══════════════════════════════════════════════════════════════════
#  数据模型（从 models.py 导入）
# ══════════════════════════════════════════════════════════════════

from models import LowRiskTrade
# ══════════════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════════════

def calculate_kelly_size(win_rate: float, avg_win: float, avg_loss: float,
                         fraction: float = config.LOW_RISK_KELLY_FRACTION) -> float:
    """
    Kelly 公式计算仓位大小。
    f = (win_rate * avg_win - (1-win_rate) * abs(avg_loss)) / avg_win
    返回建议的仓位金额（USDT）。
    """
    if avg_win <= 0:
        return 0.0

    loss_rate = 1 - win_rate
    kelly_pct = (win_rate * avg_win - loss_rate * abs(avg_loss)) / avg_win

    # 负期望值不开仓
    if kelly_pct <= 0:
        return 0.0

    # 限制最大仓位为账户余额的25%
    size_pct = max(0.0, min(kelly_pct * fraction, 0.25))
    return size_pct * config.ACCOUNT_BALANCE


def calculate_volatility(closes: list) -> float:
    """
    计算年化波动率（百分比）。
    输入：收盘价列表（1h K线）
    返回：年化波动率 %
    """
    if len(closes) < 3:
        return 0.0

    # 计算收益率
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1]
               for i in range(1, len(closes))
               if closes[i - 1] != 0]

    if len(returns) < 2:
        return 0.0

    # 标准差
    stdev = statistics.stdev(returns)

    # 年化（假设1h数据）：stdev * sqrt(365 * 24)
    annualized = stdev * math.sqrt(365 * 24)

    return annualized * 100  # 转为百分比


def check_daily_limits() -> tuple:
    """
    检查今日低风险策略的盈亏限制。
    返回: (can_trade: bool, daily_pnl: float, daily_drawdown: float)
    """
    trades_data = load_json(LOW_RISK_TRADES_FILE, [])
    today = today_str()

    # 筛选今日已平仓交易
    today_closed = [
        t for t in trades_data
        if t.get('status') == 'closed'
        and t.get('closed_at', '').startswith(today)
    ]

    # 计算今日总盈亏
    daily_pnl = sum(t.get('pnl', 0) for t in today_closed)

    # 计算今日最大回撤（按顺序累积）
    running_pnl = 0.0
    peak_pnl = 0.0
    max_drawdown = 0.0
    for t in today_closed:
        running_pnl += t.get('pnl', 0)
        if running_pnl > peak_pnl:
            peak_pnl = running_pnl
        dd = peak_pnl - running_pnl
        if dd > max_drawdown:
            max_drawdown = dd

    # 目标和回撤上限（基于账户余额）
    target = config.ACCOUNT_BALANCE * config.LOW_RISK_DAILY_TARGET_PCT / 100
    max_dd_limit = config.ACCOUNT_BALANCE * config.LOW_RISK_MAX_DAILY_DRAWDOWN_PCT / 100

    can_trade = True
    if daily_pnl >= target:
        logger.info(f"🎯 今日已达目标：{daily_pnl:.2f}U >= {target:.2f}U，停止交易")
        can_trade = False
    elif max_drawdown >= max_dd_limit:
        logger.warning(f"⚠️ 今日回撤超限：{max_drawdown:.2f}U >= {max_dd_limit:.2f}U，停止交易")
        can_trade = False

    return can_trade, daily_pnl, max_drawdown
# ══════════════════════════════════════════════════════════════════
#  扫描：网格交易机会
# ══════════════════════════════════════════════════════════════════

def scan_grid_opportunities(exchange) -> list:
    """
    扫描适合网格交易的币种。
    条件：24h 振幅在 1%~3% 之间（窄幅震荡 = 网格友好）
    返回机会列表。
    """
    opportunities = []

    for symbol in config.LOW_RISK_SYMBOLS:
        try:
            ticker = exchange.fetch_ticker(symbol)
            high = ticker.get('high', 0)
            low = ticker.get('low', 0)
            last = ticker.get('last', 0)

            if not high or not low or not last or low == 0:
                continue

            range_pct = (high - low) / low * 100

            # 只选振幅在 1%~3% 的币（太小没利润，太大风险高）
            if range_pct < 1.0 or range_pct > 3.0:
                continue

            # 计算网格价格（从低到高均匀分布）
            grid_prices = []
            step = (high - low) / (config.LOW_RISK_GRID_LEVELS + 1)
            for i in range(1, config.LOW_RISK_GRID_LEVELS + 1):
                grid_prices.append(round(low + step * i, 6))

            # 预期每次网格利润
            profit_per_trip = config.LOW_RISK_GRID_STAKE * config.LOW_RISK_GRID_LEVERAGE * (config.LOW_RISK_GRID_SPACING_PCT / 100)

            opportunities.append({
                'symbol': symbol,
                'high': high,
                'low': low,
                'last': last,
                'range_pct': round(range_pct, 2),
                'grid_prices': grid_prices,
                'profit_per_trip': round(profit_per_trip, 4),
            })

            logger.info(f"  网格候选: {symbol} | 振幅={range_pct:.2f}% | 价格={last:.6f}")
            time.sleep(0.1)

        except Exception as e:
            logger.debug(f"获取 {symbol} ticker 失败: {e}")
            continue

    return opportunities


# ══════════════════════════════════════════════════════════════════
#  扫描：均值回归机会
# ══════════════════════════════════════════════════════════════════

def scan_mean_reversion(exchange) -> list:
    """
    扫描均值回归机会。
    条件：当前价格偏离近N小时均值超过 threshold 个标准差。
    """
    opportunities = []
    lookback = config.LOW_RISK_MEAN_REVERSION_LOOKBACK
    threshold = config.LOW_RISK_MEAN_REVERSION_THRESHOLD

    for symbol in config.LOW_RISK_SYMBOLS:
        try:
            # 获取1h K线
            ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=lookback)
            if not ohlcv or len(ohlcv) < lookback // 2:
                continue

            closes = [candle[4] for candle in ohlcv]
            current = closes[-1]

            if len(closes) < 3:
                continue

            # 计算均值和标准差
            mean_price = statistics.mean(closes)
            stdev_price = statistics.stdev(closes)

            if stdev_price == 0:
                continue

            # 偏离程度（以标准差为单位）
            deviation = (current - mean_price) / stdev_price

            if abs(deviation) < threshold:
                continue

            # 方向判断
            if deviation < -threshold:
                direction = 'LONG'
                target_price = round(mean_price, 6)
                stop_price = round(current - 2 * threshold * stdev_price, 6)
            else:
                direction = 'SHORT'
                target_price = round(mean_price, 6)
                stop_price = round(current + 2 * threshold * stdev_price, 6)

            opportunities.append({
                'symbol': symbol,
                'direction': direction,
                'current_price': current,
                'mean_price': round(mean_price, 6),
                'stdev': round(stdev_price, 6),
                'deviation': round(deviation, 2),
                'target_price': target_price,
                'stop_price': stop_price,
                'volatility': calculate_volatility(closes),
            })

            logger.info(
                f"  均值回归候选: {symbol} | 方向={direction} | "
                f"偏离={deviation:.2f}σ | 价格={current:.6f} | 均值={mean_price:.6f}"
            )
            time.sleep(0.1)

        except Exception as e:
            logger.debug(f"获取 {symbol} K线失败: {e}")
            continue

    return opportunities


# ══════════════════════════════════════════════════════════════════
#  扫描：多币种费率收割
# ══════════════════════════════════════════════════════════════════

def scan_multi_funding(exchange) -> list:
    """
    多币种费率收割扫描。
    找负费率币（阈值比主策略宽松：-0.03%），同时做多多个币分散风险。
    """
    opportunities = []

    try:
        # 获取所有费率
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            timeout=10,
        )
        if r.status_code != 200:
            logger.error(f"获取费率列表失败: HTTP {r.status_code}")
            return []
        all_rates = r.json()
    except Exception as e:
        logger.error(f"获取费率列表异常: {e}")
        return []

    # 筛选目标币种的负费率
    target_symbols = {to_binance_symbol(s): s for s in config.LOW_RISK_FUNDING_SYMBOLS}

    for item in all_rates:
        binance_sym = item.get('symbol', '')
        if binance_sym not in target_symbols:
            continue

        rate = float(item.get('lastFundingRate', 0)) * 100  # 转为百分比
        mark_price = float(item.get('markPrice', 0))

        # 宽松阈值：-0.03%
        if rate >= -0.03:
            continue

        ccxt_symbol = target_symbols[binance_sym]
        opportunities.append({
            'symbol': ccxt_symbol,
            'funding_rate': round(rate, 4),
            'mark_price': mark_price,
            'expected_income': round(
                config.LOW_RISK_GRID_STAKE * config.LOW_RISK_GRID_LEVERAGE * abs(rate) / 100, 4
            ),
        })

    # 按费率排序（最负的在前）
    opportunities.sort(key=lambda x: x['funding_rate'])

    # 取前N个
    opportunities = opportunities[:config.LOW_RISK_FUNDING_MAX_COINS]

    for opp in opportunities:
        logger.info(
            f"  费率候选: {opp['symbol']} | 费率={opp['funding_rate']:.4f}%/8h | "
            f"预期收入={opp['expected_income']:.4f}U"
        )

    return opportunities
# ══════════════════════════════════════════════════════════════════
#  执行：综合扫描并开仓
# ══════════════════════════════════════════════════════════════════

def execute_low_risk_scan(mode: str = 'all'):
    """
    低风险策略主扫描入口。
    mode: 'all' | 'grid' | 'mean' | 'funding'
    """
    if not config.LOW_RISK_ENABLED:
        logger.info("低风险策略已关闭")
        return

    logger.info(f"=== 低风险策略扫描开始 (mode={mode}) ===")

    # 1. 检查日度限制
    can_trade, daily_pnl, daily_dd = check_daily_limits()
    if not can_trade:
        return

    # 2. 检查当前持仓数
    trades_data = load_json(LOW_RISK_TRADES_FILE, [])
    trades = [LowRiskTrade.from_dict(t) for t in trades_data]
    open_count = sum(1 for t in trades if t.status == 'open')
    if open_count >= config.LOW_RISK_MAX_POSITIONS:
        logger.info(f"持仓已满（{open_count}/{config.LOW_RISK_MAX_POSITIONS}），跳过扫描")
        return

    # 3. 创建交易所连接
    exchange = ccxt.binance({'enableRateLimit': True})

    new_trades = []

    # 4. 执行对应模式的扫描
    if mode in ('all', 'grid'):
        logger.info("--- 网格交易扫描 ---")
        grid_opps = scan_grid_opportunities(exchange)
        if grid_opps and open_count < config.LOW_RISK_MAX_POSITIONS:
            # 选最佳（振幅居中的）
            best = sorted(grid_opps, key=lambda x: abs(x['range_pct'] - 2.0))[0]
            # 在当前价下方买入（做多）
            price = best['last']
            grid_level = len(best['grid_prices']) // 2
            target = round(price * (1 + config.LOW_RISK_GRID_SPACING_PCT / 100), 6)
            stop = round(price * (1 - config.LOW_RISK_GRID_SPACING_PCT * 2 / 100), 6)

            trade = LowRiskTrade.create_grid(
                symbol=best['symbol'],
                price=price,
                direction='LONG',
                grid_level=grid_level,
                target_price=target,
                stop_price=stop,
            )
            new_trades.append(trade)
            open_count += 1

    if mode in ('all', 'mean') and open_count < config.LOW_RISK_MAX_POSITIONS:
        logger.info("--- 均值回归扫描 ---")
        mean_opps = scan_mean_reversion(exchange)
        if mean_opps:
            # 选偏离最大的
            best = sorted(mean_opps, key=lambda x: abs(x['deviation']), reverse=True)[0]
            trade = LowRiskTrade.create_mean_reversion(
                symbol=best['symbol'],
                price=best['current_price'],
                direction=best['direction'],
                target_price=best['target_price'],
                stop_price=best['stop_price'],
            )
            new_trades.append(trade)
            open_count += 1

    if mode in ('all', 'funding') and open_count < config.LOW_RISK_MAX_POSITIONS:
        logger.info("--- 多币费率扫描 ---")
        funding_opps = scan_multi_funding(exchange)
        for opp in funding_opps:
            if open_count >= config.LOW_RISK_MAX_POSITIONS:
                break
            trade = LowRiskTrade.create_funding(
                symbol=opp['symbol'],
                price=opp['mark_price'],
                funding_rate=opp['funding_rate'],
            )
            new_trades.append(trade)
            open_count += 1

    # 5. 保存新交易
    if new_trades:
        for trade in new_trades:
            trades.append(trade)
            logger.info(
                f"  ✅ 开仓: {trade.symbol} | 策略={trade.strategy} | "
                f"方向={trade.direction} | 入场={trade.entry_price:.6f} | "
                f"仓位={trade.stake}U x{trade.leverage}"
            )

        atomic_write_json(LOW_RISK_TRADES_FILE, [t.to_dict() for t in trades])

        # TG 推送
        msg_lines = [f"📊 <b>低风险策略开仓 ({len(new_trades)} 笔)</b>\n"]
        for t in new_trades:
            msg_lines.append(
                f"  {t.symbol} | {t.strategy} | {t.direction}\n"
                f"  入场: {t.entry_price:.6f} | 目标: {t.target_price:.6f}\n"
                f"  仓位: {t.stake}U x{t.leverage} = {t.notional}U\n"
            )
        msg_lines.append(f"\n今日盈亏: {daily_pnl:+.2f}U")
        send_tg("\n".join(msg_lines))
    else:
        logger.info("本次扫描未发现合适机会")

    logger.info("=== 低风险策略扫描结束 ===")


# ══════════════════════════════════════════════════════════════════
#  检查持仓：止盈/止损/超时
# ══════════════════════════════════════════════════════════════════

def check_low_risk_positions():
    """
    检查低风险持仓：目标达成、止损触发、时间到期。
    """
    trades_data = load_json(LOW_RISK_TRADES_FILE, [])
    if not trades_data:
        logger.info("无低风险持仓")
        return

    trades = [LowRiskTrade.from_dict(t) for t in trades_data]
    open_trades = [t for t in trades if t.status == 'open']

    if not open_trades:
        logger.info("无低风险持仓需要检查")
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

        # 计算盈亏
        if trade.direction == 'LONG':
            pnl_pct = (current - trade.entry_price) / trade.entry_price * 100
        else:
            pnl_pct = (trade.entry_price - current) / trade.entry_price * 100
        pnl_usd = trade.notional * pnl_pct / 100

        close_reason = None

        # 1. 止盈检查
        if trade.direction == 'LONG' and current >= trade.target_price:
            close_reason = f"止盈触发（价格达目标 {trade.target_price:.6f}）"
        elif trade.direction == 'SHORT' and current <= trade.target_price:
            close_reason = f"止盈触发（价格达目标 {trade.target_price:.6f}）"

        # 2. 止损检查
        elif trade.direction == 'LONG' and current <= trade.stop_price:
            close_reason = f"止损触发（价格跌破 {trade.stop_price:.6f}）"
        elif trade.direction == 'SHORT' and current >= trade.stop_price:
            close_reason = f"止损触发（价格突破 {trade.stop_price:.6f}）"

        # 3. 超时检查
        elif hours >= trade.max_hold_hours:
            close_reason = f"超时平仓（持仓 {hours:.1f}h >= {trade.max_hold_hours}h）"

        if close_reason:
            trade.status = 'closed'
            trade.closed_at = utcnow_iso()
            trade.close_reason = close_reason
            trade.pnl = round(pnl_usd, 4)
            any_updated = True

            emoji = "✅" if trade.pnl >= 0 else "❌"
            logger.info(
                f"  {emoji} 平仓 {trade.symbol}: {close_reason} | "
                f"PnL={trade.pnl:+.4f}U | 策略={trade.strategy}"
            )

            send_tg(
                f"📊 <b>低风险平仓</b> {emoji}\n\n"
                f"币种：<b>{trade.symbol}</b>\n"
                f"策略：{trade.strategy}\n"
                f"原因：{close_reason}\n"
                f"入场：{trade.entry_price:.6f} -> 平仓：{current:.6f}\n"
                f"<b>盈亏：{trade.pnl:+.4f}U</b>"
            )
        else:
            trade.pnl = round(pnl_usd, 4)
            any_updated = True

    if any_updated:
        atomic_write_json(LOW_RISK_TRADES_FILE, [t.to_dict() for t in trades])


# ══════════════════════════════════════════════════════════════════
#  状态查看
# ══════════════════════════════════════════════════════════════════

def get_low_risk_status():
    """打印低风险策略今日状态"""
    trades_data = load_json(LOW_RISK_TRADES_FILE, [])
    trades = [LowRiskTrade.from_dict(t) for t in trades_data]

    open_trades = [t for t in trades if t.status == 'open']
    today = today_str()
    today_closed = [
        t for t in trades
        if t.status == 'closed' and t.closed_at and t.closed_at.startswith(today)
    ]

    # 今日盈亏
    daily_pnl = sum(t.pnl for t in today_closed)
    target = config.ACCOUNT_BALANCE * config.LOW_RISK_DAILY_TARGET_PCT / 100

    # 进度条
    progress = min(daily_pnl / target * 100, 100) if target > 0 else 0
    bar_len = 20
    filled = int(bar_len * max(0, progress) / 100)
    bar = '█' * filled + '░' * (bar_len - filled)

    # 策略分布
    strategy_pnl = {}
    for t in today_closed:
        strategy_pnl.setdefault(t.strategy, 0.0)
        strategy_pnl[t.strategy] += t.pnl

    print("=== 低风险日收策略状态 ===")
    print(f"今日目标：{target:.2f}U ({config.LOW_RISK_DAILY_TARGET_PCT}%)")
    print(f"今日盈亏：{daily_pnl:+.2f}U")
    print(f"进度：[{bar}] {progress:.1f}%")
    print(f"\n持仓中：{len(open_trades)} 笔")
    for t in open_trades:
        hours = hold_hours(t.opened_at)
        print(
            f"  {t.symbol} | {t.strategy} | {t.direction} | "
            f"浮盈={t.pnl:+.4f}U | 持仓{hours:.1f}h"
        )

    print(f"\n今日已平仓：{len(today_closed)} 笔")
    if strategy_pnl:
        print("策略分布：")
        for strat, pnl in strategy_pnl.items():
            print(f"  {strat}: {pnl:+.4f}U")


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='低风险日收策略 - 网格/均值回归/多币费率'
    )
    subparsers = parser.add_subparsers(dest='command', help='子命令')

    # scan 子命令
    scan_parser = subparsers.add_parser('scan', help='执行策略扫描并开仓')
    scan_parser.add_argument(
        '--mode', choices=['all', 'grid', 'mean', 'funding'],
        default='all', help='扫描模式（默认 all）'
    )

    # check 子命令
    subparsers.add_parser('check', help='检查持仓（止盈/止损/超时）')

    # status 子命令
    subparsers.add_parser('status', help='查看今日状态')

    args = parser.parse_args()

    if args.command == 'scan':
        execute_low_risk_scan(mode=args.mode)
    elif args.command == 'check':
        check_low_risk_positions()
    elif args.command == 'status':
        get_low_risk_status()
    else:
        parser.print_help()
