#!/usr/bin/env python3
"""
多策略做多扫描器 v1.0
两种做多策略，与做空互补，提高信号触发频率。

策略A：突破回踩做多
  - 价格突破近期高点后回踩不破（支撑确认）
  - 回踩时成交量缩量（卖压不足）
  - RSI 回落到40~60区间（健康回调，不是反转）

策略B：插针抄底做多
  - 1H K线出现长下影线（下影 > 实体×3）
  - RSI < 25（极度超卖）
  - OI 同步增加（主力在抄底建仓）

用法：
  python3 long_scanner.py scan    # 扫描做多信号
  python3 long_scanner.py status  # 查看做多持仓
"""

import time
import ccxt
import requests

import config
from common import (
    TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    to_binance_symbol, utcnow_iso,
)
from models import Trade
from risk_control import can_open_trade, record_trade_opened
from signal_score import check_btc_filter, calculate_long_signal_score, get_ma2560_trend

logger = setup_logger("long_scanner")


# ══════════════════════════════════════════════════════════════════
#  配置（做多专用参数，从 config.py 读取）
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
#  策略A：突破回踩做多
# ══════════════════════════════════════════════════════════════════

def detect_breakout_pullback(exchange, symbol: str) -> dict:
    """
    检测突破回踩信号：
    1. 近期曾突破前高（创新高）
    2. 当前价格回踩到突破位附近但未跌破
    3. 回踩时成交量缩量
    4. RSI 在健康区间（35~60）
    """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=config.LONG_BREAKOUT_LOOKBACK + 10)
        if len(ohlcv) < config.LONG_BREAKOUT_LOOKBACK:
            return {"signal": False, "reason": "数据不足"}

        highs = [c[2] for c in ohlcv]
        lows = [c[3] for c in ohlcv]
        closes = [c[4] for c in ohlcv]
        volumes = [c[5] for c in ohlcv]

        current_price = closes[-1]
        current_vol = volumes[-1]

        # 找前高（排除最近5根，找之前的最高点）
        lookback_highs = highs[:-5]
        if not lookback_highs:
            return {"signal": False, "reason": "数据不足"}

        prev_high = max(lookback_highs)
        prev_high_idx = lookback_highs.index(prev_high)

        # 检查最近5根K线是否曾突破前高
        recent_highs = highs[-5:]
        breakthrough = any(h > prev_high * 1.005 for h in recent_highs)  # 突破>0.5%算有效

        if not breakthrough:
            return {"signal": False, "reason": "未突破前高"}

        # 突破后的最高点
        breakout_high = max(recent_highs)

        # 当前价格回踩（在前高附近±3%）
        pullback_from_high = (breakout_high - current_price) / breakout_high
        if pullback_from_high < 0.01:  # 还没回踩（还在高位）
            return {"signal": False, "reason": "还未回踩"}
        if pullback_from_high > config.LONG_PULLBACK_DEPTH_MAX + 0.02:  # 回踩太深
            return {"signal": False, "reason": f"回踩过深({pullback_from_high*100:.1f}%)"}

        # 关键：价格不能跌破前高（支撑位确认）
        if current_price < prev_high * 0.99:  # 跌破前高1%以上=假突破
            return {"signal": False, "reason": "跌破前高（假突破）"}

        # 缩量回踩
        breakout_vol = max(volumes[-5:])  # 突破时的最大量
        if current_vol > breakout_vol * config.LONG_PULLBACK_VOL_SHRINK:
            return {"signal": False, "reason": "回踩未缩量"}

        # RSI 健康区间
        from altcoin_scanner import calc_rsi_wilder
        rsi = calc_rsi_wilder(closes)
        if rsi < config.LONG_PULLBACK_RSI_MIN or rsi > config.LONG_PULLBACK_RSI_MAX:
            return {"signal": False, "reason": f"RSI={rsi}不在回踩区间"}

        return {
            "signal": True,
            "reason": f"突破回踩: 前高{prev_high:.6f}→突破{breakout_high:.6f}→回踩{current_price:.6f}",
            "strategy": "breakout_pullback",
            "prev_high": prev_high,
            "breakout_high": breakout_high,
            "pullback_pct": round(pullback_from_high * 100, 2),
            "rsi": rsi,
        }
    except Exception as e:
        logger.debug(f"突破回踩检测失败 ({symbol}): {e}")
        return {"signal": False, "reason": f"检测异常: {e}"}


# ══════════════════════════════════════════════════════════════════
#  策略B：插针抄底做多
# ══════════════════════════════════════════════════════════════════

def detect_pin_bar_bottom(exchange, symbol: str) -> dict:
    """
    检测插针抄底信号：
    1. 最近1~2根1H K线出现长下影线（下影 >= 实体 × config.LONG_PIN_SHADOW_RATIO）
    2. RSI < config.LONG_PIN_RSI_MAX（极度超卖）
    3. OI 增加（主力在低位建仓，不是恐慌抛售）
    """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=20)
        if len(ohlcv) < 15:
            return {"signal": False, "reason": "数据不足"}

        # 检查最近2根K线
        pin_found = False
        pin_candle = None
        for candle in ohlcv[-2:]:
            open_p, high, low, close_p, vol = candle[1], candle[2], candle[3], candle[4], candle[5]

            body = abs(close_p - open_p)
            if body <= 0:
                body = 0.0001  # 避免除零

            lower_shadow = min(open_p, close_p) - low
            upper_shadow = high - max(open_p, close_p)

            # 长下影线：下影 >= 实体 × N 且 下影 > 上影 × 2
            if lower_shadow >= body * config.LONG_PIN_SHADOW_RATIO and lower_shadow > upper_shadow * 2:
                pin_found = True
                pin_candle = candle
                break

        if not pin_found:
            return {"signal": False, "reason": "无插针形态"}

        closes = [c[4] for c in ohlcv]

        # RSI 超卖
        from altcoin_scanner import calc_rsi_wilder
        rsi = calc_rsi_wilder(closes)
        if rsi > config.LONG_PIN_RSI_MAX:
            return {"signal": False, "reason": f"RSI={rsi}>超卖阈值{config.LONG_PIN_RSI_MAX}"}

        # 成交量门槛
        current_vol = ohlcv[-1][5] * closes[-1]  # 估算USDT成交量
        if current_vol < config.LONG_PIN_VOL_MIN:
            return {"signal": False, "reason": "成交量不足"}

        # OI 增加检查
        oi_increasing = False
        try:
            sym = to_binance_symbol(symbol)
            r = requests.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": sym, "period": "1h", "limit": 5},
                timeout=5,
            )
            if r.status_code == 200 and r.json():
                oi_hist = r.json()
                if len(oi_hist) >= 2:
                    oi_recent = float(oi_hist[-1]['sumOpenInterest'])
                    oi_prev = float(oi_hist[-2]['sumOpenInterest'])
                    if oi_prev > 0:
                        oi_change = (oi_recent - oi_prev) / oi_prev
                        oi_increasing = oi_change >= config.LONG_PIN_OI_INCREASE_MIN
        except Exception as e:
            logger.debug(f"插针OI检测异常: {e}")

        if not oi_increasing:
            return {"signal": False, "reason": "OI未增加（可能是恐慌抛售）"}

        low_price = pin_candle[3]
        close_price = pin_candle[4]
        shadow_pct = (close_price - low_price) / close_price * 100

        return {
            "signal": True,
            "reason": f"插针抄底: 下影{shadow_pct:.1f}% + RSI={rsi:.0f} + OI增加",
            "strategy": "pin_bar_bottom",
            "pin_low": low_price,
            "rsi": rsi,
            "shadow_pct": round(shadow_pct, 2),
        }
    except Exception as e:
        logger.debug(f"插针检测失败 ({symbol}): {e}")
        return {"signal": False, "reason": f"检测异常: {e}"}


# ══════════════════════════════════════════════════════════════════
#  创建做多交易
# ══════════════════════════════════════════════════════════════════

def create_long_trade(symbol: str, price: float, reason: str,
                      strategy: str) -> Trade:
    """创建做多影子交易"""
    import time as _time
    from dataclasses import asdict

    trade = Trade(
        id=f"LONG-{strategy.upper()}-{symbol.replace('/USDT','').replace('/','')}-{int(_time.time())}",
        symbol=symbol,
        direction='LONG',
        entry_price=price,
        stake=config.LONG_STAKE,
        leverage=config.LONG_LEVERAGE,
        notional=config.LONG_STAKE * config.LONG_LEVERAGE,
        shares=round((config.LONG_STAKE * config.LONG_LEVERAGE) / price, 4) if price > 0 else 0,
        reason=reason,
        strategy=strategy,
        # 做多：止盈价 = entry * (1 + tp_pct)
        take_profit_1=round(price * (1 + config.LONG_TP1_PCT), 6),
        take_profit_2=round(price * (1 + config.LONG_TP2_PCT), 6),
        # 做多：止损价 = entry * (1 - stop_pct/100)
        hard_stop_price=round(price * (1 - config.LONG_STOP_LOSS_PCT / 100), 6),
        stake_remaining=config.LONG_STAKE,
        max_hold_days=1,  # 24小时
    )
    return trade


# ══════════════════════════════════════════════════════════════════
#  主扫描流程
# ══════════════════════════════════════════════════════════════════

def scan_long_signals():
    """扫描做多信号"""
    logger.info("=== 开始做多扫描 ===")

    # BTC 过滤（做多时 BTC 暴跌不适合抄底）
    btc_allowed, btc_pct, btc_reason = check_btc_filter()
    # 做多的BTC过滤逻辑相反：BTC暴跌>8%时不抄底（可能继续跌）
    if btc_pct < -8:
        logger.warning(f"  🚫 BTC暴跌{btc_pct:.1f}%，暂停做多抄底")
        return

    exchange = ccxt.binance({'enableRateLimit': True})

    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        logger.error(f"获取行情失败: {e}")
        return

    # 已有持仓不重复
    trades_list = load_json(TRADES_FILE, [])
    open_symbols = {t['symbol'] for t in trades_list if t.get('status') == 'open'}

    # 风控检查
    allowed, risk_reason = can_open_trade(config.LONG_STAKE)
    if not allowed:
        logger.warning(f"  🚫 风控拒绝: {risk_reason}")
        return

    # 筛选候选币（24h跌幅大 + 成交量够）
    candidates = []
    for symbol, ticker in tickers.items():
        if not symbol.endswith('/USDT'):
            continue
        if symbol in open_symbols:
            continue

        price = ticker.get('last', 0) or 0
        vol24h = ticker.get('quoteVolume', 0) or 0
        pct24h = ticker.get('percentage', 0) or 0

        if price <= 0 or price > config.PRICE_MAX:
            continue
        if vol24h < config.VOL_MIN:
            continue

        # 做多候选：24h跌幅大（超卖）或刚突破
        # 策略A候选：涨幅5~30%（突破后回踩）
        # 策略B候选：跌幅>10%（超卖抄底）
        if 5 <= pct24h <= 30 or pct24h <= -10:
            candidates.append({
                'symbol': symbol,
                'price': price,
                'vol24h': vol24h,
                'pct24h': pct24h,
            })

    logger.info(f"  做多候选: {len(candidates)} 个")

    opened = 0
    max_open = 1  # 每次扫描最多开1单

    for cand in candidates[:20]:  # 最多检查20个
        if opened >= max_open:
            break

        symbol = cand['symbol']

        # 策略A：突破回踩（涨幅5~30%的币）
        if 5 <= cand['pct24h'] <= 30:
            result = detect_breakout_pullback(exchange, symbol)
            if result['signal']:
                # 获取均线趋势（改良2560系统）
                ma2560_trend = get_ma2560_trend(exchange, symbol)
                # 信号评分
                score_result = calculate_long_signal_score(
                    rsi=result.get('rsi', 50),
                    strategy_type='breakout_pullback',
                    pullback_pct=result.get('pullback_pct', 0),
                    btc_24h_pct=btc_pct,
                    ma2560_trend=ma2560_trend,
                )
                if score_result['grade'] == 'SKIP':
                    logger.info(f"  ⏭️ {symbol} 评分不足({score_result['score']}分)，跳过")
                    continue

                price = exchange.fetch_ticker(symbol)['last']
                trade = create_long_trade(symbol, price, result['reason'], 'breakout_pullback')
                # 根据评分调整仓位
                if score_result['grade'] == 'B':
                    trade.stake = round(config.LONG_STAKE * 0.5)
                    trade.notional = trade.stake * config.LONG_LEVERAGE
                trades_list.append(trade.to_dict())
                atomic_write_json(TRADES_FILE, trades_list)
                record_trade_opened(trade.stake)
                opened += 1

                logger.info(f"  ✅ 做多开仓: {symbol} @ {price} | {result['reason']} | 评分={score_result['score']}[{score_result['grade']}]")
                send_tg(
                    f"🟢 <b>做多开仓：突破回踩</b>\n\n"
                    f"📌 <b>{symbol}</b>\n"
                    f"入场价：{price:.6f}\n"
                    f"仓位：{config.LONG_STAKE}U × {config.LONG_LEVERAGE}x = {trade.notional}U\n"
                    f"止盈：+5%/{trade.take_profit_1:.6f} | +10%/{trade.take_profit_2:.6f}\n"
                    f"止损：-3%/{trade.hard_stop_price:.6f}\n\n"
                    f"📊 {result['reason']}\n"
                    f"RSI={result.get('rsi',0):.0f} | 回踩{result.get('pullback_pct',0):.1f}%\n"
                    f"评分={score_result['score']} [{score_result['grade']}] {score_result['reason']}"
                )
                continue

        # 策略B：插针抄底（跌幅>10%的币）
        if cand['pct24h'] <= -10:
            result = detect_pin_bar_bottom(exchange, symbol)
            if result['signal']:
                # 获取均线趋势（改良2560系统）
                ma2560_trend = get_ma2560_trend(exchange, symbol)
                # 信号评分
                score_result = calculate_long_signal_score(
                    rsi=result.get('rsi', 50),
                    strategy_type='pin_bar_bottom',
                    shadow_pct=result.get('shadow_pct', 0),
                    oi_increasing=True,  # 已通过OI检查
                    btc_24h_pct=btc_pct,
                    ma2560_trend=ma2560_trend,
                )
                if score_result['grade'] == 'SKIP':
                    logger.info(f"  ⏭️ {symbol} 评分不足({score_result['score']}分)，跳过")
                    continue

                price = exchange.fetch_ticker(symbol)['last']
                trade = create_long_trade(symbol, price, result['reason'], 'pin_bar_bottom')
                # 根据评分调整仓位
                if score_result['grade'] == 'B':
                    trade.stake = round(config.LONG_STAKE * 0.5)
                    trade.notional = trade.stake * config.LONG_LEVERAGE
                trades_list.append(trade.to_dict())
                atomic_write_json(TRADES_FILE, trades_list)
                record_trade_opened(trade.stake)
                opened += 1

                logger.info(f"  ✅ 做多开仓: {symbol} @ {price} | {result['reason']} | 评分={score_result['score']}[{score_result['grade']}]")
                send_tg(
                    f"🟢 <b>做多开仓：插针抄底</b>\n\n"
                    f"📌 <b>{symbol}</b>\n"
                    f"入场价：{price:.6f}\n"
                    f"仓位：{config.LONG_STAKE}U × {config.LONG_LEVERAGE}x = {trade.notional}U\n"
                    f"止盈：+5%/{trade.take_profit_1:.6f} | +10%/{trade.take_profit_2:.6f}\n"
                    f"止损：-3%/{trade.hard_stop_price:.6f}\n\n"
                    f"📊 {result['reason']}\n"
                    f"下影线={result.get('shadow_pct',0):.1f}% | RSI={result.get('rsi',0):.0f}\n"
                    f"评分={score_result['score']} [{score_result['grade']}] {score_result['reason']}"
                )
                continue

        time.sleep(0.1)

    if opened == 0:
        logger.info("  无做多信号触发")
    else:
        logger.info(f"  做多开仓 {opened} 单")


def show_status():
    """显示做多持仓"""
    trades = load_json(TRADES_FILE, [])
    long_trades = [t for t in trades if t.get('direction') == 'LONG']
    open_longs = [t for t in long_trades if t.get('status') == 'open']
    closed_longs = [t for t in long_trades if t.get('status') == 'closed']

    print("=== 做多持仓 ===")
    print(f"持仓中：{len(open_longs)} 单")
    for t in open_longs:
        print(f"  {t['symbol']} | 入场{t['entry_price']:.6f} | 策略={t.get('strategy','')}")

    total_pnl = sum(t.get('pnl', 0) + t.get('tp1_locked_pnl', 0) for t in closed_longs)
    print(f"\n已平仓：{len(closed_longs)} 单 | 总盈亏：{total_pnl:+.2f}U")


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else 'scan'

    if mode == 'scan':
        scan_long_signals()
    elif mode == 'status':
        show_status()
    else:
        print(f"用法: {sys.argv[0]} [scan|status]")
        sys.exit(1)
