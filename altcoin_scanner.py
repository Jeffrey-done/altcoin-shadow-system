#!/usr/bin/env python3
"""
小币种超买做空扫描器 v4.0
多时间框架策略：
  - 每4小时扫描全市场，筛选日线 RSI>78 的候选池
  - 每1小时检查候选池，确认 4h RSI 回落 或 1H 弃盘点 触发信号
  - 过滤条件：成交量>50万U、价格<1U、24h涨幅>10%
新增：
  - 杠杆仓位计算（10x）
  - 硬止损价格
  - 风控检查（开仓前必须通过）
"""

import time
import ccxt
import requests

import config
from common import (
    CANDIDATES_FILE, TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    to_binance_symbol, utcnow, utcnow_iso, parse_iso,
)
from models import Candidate, Trade
from risk_control import can_open_trade, record_trade_opened
from signal_score import calculate_signal_score, check_btc_filter

logger = setup_logger("altcoin_scanner")


# ══════════════════════════════════════════════════════════════════
#  技术指标
# ══════════════════════════════════════════════════════════════════

def calc_rsi_wilder(closes: list, period: int = config.RSI_PERIOD) -> float:
    """
    Wilder 平滑 RSI（与 TradingView 一致）。
    第一段用 SMA 初始化，后续用 EMA 递推。
    """
    if len(closes) < period + 1:
        return 50.0

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    # SMA 初始化（前 period 个）
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder 递推
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def get_rsi(exchange, symbol: str, timeframe: str, limit: int = 50) -> float:
    """获取指定时间框架的 RSI"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        closes = [c[4] for c in ohlcv]
        return calc_rsi_wilder(closes)
    except Exception as e:
        logger.warning(f"获取 RSI 失败 ({symbol} {timeframe}): {e}")
        return 50.0


def get_rsi_peak(exchange, symbol: str, timeframe: str = '4h',
                 lookback: int = config.H4_RSI_PEAK_LOOKBACK) -> float:
    """获取近期 RSI 峰值（O(n) Wilder 递推）"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=lookback + config.RSI_PERIOD + 5)
        closes = [c[4] for c in ohlcv]
        if len(closes) < config.RSI_PERIOD + 1:
            return 50.0

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]

        period = config.RSI_PERIOD
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        rsi_series = []
        if avg_loss == 0:
            rsi_series.append(100.0)
        else:
            rsi_series.append(100 - (100 / (1 + avg_gain / avg_loss)))

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            if avg_loss == 0:
                rsi_series.append(100.0)
            else:
                rsi_series.append(100 - (100 / (1 + avg_gain / avg_loss)))

        return round(max(rsi_series[-lookback:]), 1) if rsi_series else 50.0
    except Exception as e:
        logger.warning(f"获取 RSI 峰值失败 ({symbol}): {e}")
        return 50.0


# ══════════════════════════════════════════════════════════════════
#  合约数据（OI / 资金费率）
# ══════════════════════════════════════════════════════════════════

def get_oi_change(symbol: str) -> float:
    """获取合约 OI 24h 变化率，无合约返回 0"""
    try:
        sym = to_binance_symbol(symbol)
        r = requests.get(
            "https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol": sym, "period": "1h", "limit": 25},
            timeout=5,
        )
        if r.status_code != 200 or not r.json():
            return 0.0
        hist = r.json()
        if len(hist) < 2:
            return 0.0
        oi_now = float(hist[-1].get('sumOpenInterest', 0))
        oi_24h_ago = float(hist[0].get('sumOpenInterest', 0))
        if oi_24h_ago <= 0:
            return 0.0
        return (oi_now - oi_24h_ago) / oi_24h_ago
    except Exception as e:
        logger.debug(f"获取 OI 变化失败 ({symbol}): {e}")
        return 0.0


def get_funding_rate(symbol: str) -> float:
    """获取当前资金费率（%/8h），无合约返回 0"""
    try:
        sym = to_binance_symbol(symbol)
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": sym},
            timeout=5,
        )
        if r.status_code != 200:
            return 0.0
        return float(r.json().get('lastFundingRate', 0)) * 100
    except Exception as e:
        logger.debug(f"获取资金费率失败 ({symbol}): {e}")
        return 0.0


# ══════════════════════════════════════════════════════════════════
#  弃盘点检测
# ══════════════════════════════════════════════════════════════════

def detect_abandon_signal(exchange, symbol: str) -> dict:
    """
    检测 1H K 线弃盘点信号：
    连续 N 根 1H K 线实体下跌 > ABANDON_BODY_DROP_PCT% + OI 同步下降
    """
    try:
        h1 = exchange.fetch_ohlcv(symbol, '1h', limit=6)
        if len(h1) < 3:
            return {"signal": False, "reason": "数据不足"}

        drops = []
        for candle in h1[-3:]:
            open_p, close_p = candle[1], candle[4]
            if open_p <= 0:
                continue
            body_drop = (open_p - close_p) / open_p * 100
            drops.append(round(body_drop, 2))

        consecutive = sum(
            1 for d in drops[-config.ABANDON_CONSECUTIVE:]
            if d > config.ABANDON_BODY_DROP_PCT
        )

        if consecutive < config.ABANDON_CONSECUTIVE:
            return {"signal": False, "reason": f"1H跌幅不足({drops[-2:]})"}

        total_drop = sum(d for d in drops[-config.ABANDON_CONSECUTIVE:] if d > 0)

        oi_declining = False
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
                    oi_declining = oi_recent < oi_prev * (1 - config.ABANDON_OI_DROP_PCT)
        except Exception as e:
            logger.debug(f"弃盘 OI 检测异常: {e}")

        reason = f"连续{consecutive}根1H实体下跌{total_drop:.1f}%"
        if oi_declining:
            reason += " + OI同步下降（主力撤退信号）"

        return {
            "signal": True,
            "reason": reason,
            "drop_pct": total_drop,
            "oi_declining": oi_declining,
            "drops": drops[-config.ABANDON_CONSECUTIVE:],
        }
    except Exception as e:
        logger.warning(f"弃盘检测失败 ({symbol}): {e}")
        return {"signal": False, "reason": f"检测失败:{e}"}


# ══════════════════════════════════════════════════════════════════
#  第一阶段：日线扫描（每4小时）
# ══════════════════════════════════════════════════════════════════

def scan_daily():
    """扫描全市场，找日线 RSI > DAILY_RSI_MIN 的候选币"""
    logger.info("=== 开始日线扫描 ===")
    exchange = ccxt.binance({'enableRateLimit': True})

    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        logger.error(f"获取行情失败: {e}")
        return

    candidates: dict[str, Candidate] = {}
    checked = 0

    for symbol, ticker in tickers.items():
        if not symbol.endswith('/USDT'):
            continue

        price = ticker.get('last', 0) or 0
        vol24h = ticker.get('quoteVolume', 0) or 0
        pct24h = ticker.get('percentage', 0) or 0

        if price <= 0 or price > config.PRICE_MAX:
            continue
        if vol24h < config.VOL_MIN:
            continue
        if pct24h < config.PCT_24H_MIN:
            continue

        checked += 1

        rsi_1d = get_rsi(exchange, symbol, '1d', limit=50)

        if rsi_1d >= config.DAILY_RSI_MIN:
            oi_change = get_oi_change(symbol)
            funding = get_funding_rate(symbol)

            yao_score = 0
            if oi_change >= config.OI_CHANGE_MIN:
                yao_score += 1
            if funding >= config.FUNDING_HOT:
                yao_score += 1
            if pct24h >= 30:
                yao_score += 1

            if funding > config.FUNDING_MAX:
                logger.info(f"  ⛔ 跳过: {symbol} 资金费率过高({funding:.4f}%)")
                continue

            candidates[symbol] = Candidate(
                symbol=symbol,
                price=price,
                vol24h=round(vol24h),
                pct24h=round(pct24h, 1),
                rsi_1d=rsi_1d,
                oi_change=round(oi_change * 100, 1),
                funding_rate=round(funding, 4),
                yao_score=yao_score,
            )

            yao_tag = "🔥妖币" if yao_score >= 2 else ("⚡候选" if yao_score == 1 else "📌普通")
            logger.info(
                f"  ✅ {yao_tag}: {symbol} | 日线RSI={rsi_1d} | "
                f"24h={pct24h:.1f}% | OI={oi_change*100:.0f}% | "
                f"FR={funding:.4f}% | 评分={yao_score}"
            )

        time.sleep(0.1)

    # 合并已有候选池
    existing_list = load_json(CANDIDATES_FILE, [])
    existing = {c['symbol']: Candidate.from_dict(c) for c in existing_list}

    for sym, cand in candidates.items():
        if sym in existing:
            existing[sym].rsi_1d = cand.rsi_1d
            existing[sym].price = cand.price
            existing[sym].vol24h = cand.vol24h
            existing[sym].pct24h = cand.pct24h
        else:
            existing[sym] = cand

    # 清理过期候选
    now = utcnow()
    to_remove = []
    for sym, c in existing.items():
        if c.triggered:
            added = parse_iso(c.added_at)
            if (now - added).days > config.CANDIDATE_EXPIRE_DAYS:
                to_remove.append(sym)
    for sym in to_remove:
        del existing[sym]

    atomic_write_json(CANDIDATES_FILE, [c.to_dict() for c in existing.values()])
    logger.info(f"日线扫描完成，共检查 {checked} 个标的，候选池 {len(existing)} 个")


# ══════════════════════════════════════════════════════════════════
#  第二阶段：4h 确认（每1小时）
# ══════════════════════════════════════════════════════════════════

def check_candidates():
    """检查候选池，4h RSI 回落 或 弃盘点 触发时自动开仓"""
    candidates_list = load_json(CANDIDATES_FILE, [])
    if not candidates_list:
        logger.info("候选池为空，跳过")
        return

    # ── BTC 趋势过滤（全局开关）──
    btc_allowed, btc_pct, btc_reason = check_btc_filter()
    if not btc_allowed:
        logger.warning(f"  🚫 {btc_reason}")
        send_tg(f"🚫 <b>BTC过滤：暂停做空</b>\n\n{btc_reason}")
        return

    candidates = [Candidate.from_dict(c) for c in candidates_list]
    exchange = ccxt.binance({'enableRateLimit': True})
    logger.info(f"=== 检查候选池（{len(candidates)}个）| BTC 24h={btc_pct:+.1f}% ===")

    # 已有持仓的币不重复开仓
    trades_list = load_json(TRADES_FILE, [])
    open_symbols = {
        t['symbol'] for t in trades_list if t.get('status') == 'open'
    }

    triggered_any = False

    for c in candidates:
        if c.triggered or c.symbol in open_symbols:
            continue

        # 获取 4h RSI
        rsi_4h = get_rsi(exchange, c.symbol, '4h', limit=50)
        rsi_4h_peak = get_rsi_peak(exchange, c.symbol, '4h')
        c.rsi_4h = rsi_4h
        c.rsi_4h_peak = rsi_4h_peak

        drop = rsi_4h_peak - rsi_4h
        logger.info(
            f"  {c.symbol}: 日线RSI={c.rsi_1d} | 4h RSI={rsi_4h} | "
            f"峰值={rsi_4h_peak:.1f} | 回落={drop:.1f}"
        )

        trigger_4h = rsi_4h < config.H4_RSI_ENTER and drop >= config.H4_RSI_DROP
        abandon = detect_abandon_signal(exchange, c.symbol)
        trigger_abandon = abandon.get("signal", False)

        if not (trigger_4h or trigger_abandon):
            time.sleep(0.1)
            continue

        # ── 信号评分 ──
        abandon_oi = abandon.get("oi_declining", False) if trigger_abandon else False
        score_result = calculate_signal_score(
            rsi_1d=c.rsi_1d,
            rsi_4h=rsi_4h,
            rsi_4h_peak=rsi_4h_peak,
            pct_24h=c.pct24h,
            oi_change=c.oi_change,
            funding_rate=c.funding_rate,
            yao_score=c.yao_score,
            trigger_type='abandon' if trigger_abandon else '4h_rsi',
            abandon_oi_declining=abandon_oi,
            btc_24h_pct=btc_pct,
        )

        # 评分太低跳过
        if score_result["grade"] == "SKIP":
            logger.info(f"  ⏭️ 跳过 {c.symbol}: 评分{score_result['score']}分 < {config.SCORE_SKIP_THRESHOLD}分")
            time.sleep(0.1)
            continue

        # 根据评分决定仓位（结合自动复利）
        from common import get_compound_stake
        base_stake = get_compound_stake()
        if score_result["grade"] == "A":
            actual_stake = base_stake
        else:  # grade B
            actual_stake = round(base_stake * 0.5)

        # ── 风控检查 ──
        allowed, risk_reason = can_open_trade(actual_stake)
        if not allowed:
            logger.warning(f"  🚫 风控拒绝 {c.symbol}: {risk_reason}")
            continue

        # ── 触发开仓 ──
        try:
            price = exchange.fetch_ticker(c.symbol)['last']
        except Exception as e:
            logger.error(f"获取最新价失败 ({c.symbol}): {e}")
            continue

        c.triggered = True
        triggered_any = True

        if trigger_abandon:
            trigger_reason = f"弃盘点: {abandon['reason']}"
            c.trigger_type = 'abandon'
        else:
            trigger_reason = f"4h RSI从{rsi_4h_peak:.0f}回落至{rsi_4h}"
            c.trigger_type = '4h_rsi'
        c.trigger_reason = trigger_reason

        logger.info(f"  🚨 触发信号: {c.symbol} @ {price} [{trigger_reason}]")

        # 创建影子空单（带杠杆 + 硬止损 + 评分仓位）
        trade = Trade.create_short(c.symbol, price, reason=trigger_reason, stake=actual_stake)
        trades_list.append(trade.to_dict())
        atomic_write_json(TRADES_FILE, trades_list)

        # 记录风控
        record_trade_opened(actual_stake)

        logger.info(
            f"  ✅ 已开空单: {c.symbol} @ {price} | "
            f"评分={score_result['score']}[{score_result['grade']}] | "
            f"保证金={trade.stake}U × {trade.leverage}x = {trade.notional}U | "
            f"硬止损={trade.hard_stop_price}"
        )

        # 推送通知
        yao_tag = "🔥妖币" if c.yao_score >= 2 else "📌普通超买"
        if c.trigger_type == 'abandon':
            trigger_desc = f"📊 触发方式：<b>弃盘点信号</b>\n{abandon['reason']}"
        else:
            trigger_desc = (
                f"📊 触发方式：<b>4h RSI 回落</b>\n"
                f"4h RSI：{rsi_4h}（峰值 {rsi_4h_peak:.0f}，回落 {drop:.0f} 点）"
            )

        msg = (
            f"🔴 <b>影子空单已开仓</b> {yao_tag}\n\n"
            f"📌 <b>{c.symbol}</b>\n"
            f"📊 信号评分：<b>{score_result['score']}分 [{score_result['grade']}]</b>\n"
            f"评分详情：RSI={score_result['details'].get('rsi',0):.0f} "
            f"妖={score_result['details'].get('yao',0):.0f} "
            f"触发={score_result['details'].get('trigger',0):.0f} "
            f"热度={score_result['details'].get('heat',0):.0f}\n\n"
            f"入场价：{price:.6f} U\n"
            f"保证金：{trade.stake}U × {trade.leverage}x = <b>{trade.notional}U</b>\n"
            f"止盈一档：{trade.take_profit_1:.6f}（-5%，+{trade.notional*0.05*0.5:.1f}U）\n"
            f"止盈二档：{trade.take_profit_2:.6f}（-10%，+{trade.notional*0.10*0.5:.1f}U）\n"
            f"硬止损：{trade.hard_stop_price:.6f}（+3%，-{trade.notional*0.03:.1f}U）\n"
            f"移动止损：最高盈利回撤10%触发\n\n"
            f"{trigger_desc}\n\n"
            f"日线RSI：{c.rsi_1d}（超买）\n"
            f"24h涨幅：{c.pct24h:+.1f}% | 成交量：{c.vol24h:,}U\n"
            f"OI变化：{c.oi_change:+.0f}% | 资金费率：{c.funding_rate:.4f}%/8h\n"
            f"妖币评分：{c.yao_score}/3 | BTC 24h：{btc_pct:+.1f}%"
        )
        send_tg(msg)

        time.sleep(0.1)

    # 保存候选池更新
    atomic_write_json(CANDIDATES_FILE, [c.to_dict() for c in candidates])

    if not triggered_any:
        logger.info("候选池无触发信号")


# ══════════════════════════════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else 'check'

    if mode == 'scan':
        scan_daily()
    elif mode == 'check':
        check_candidates()
    elif mode == 'both':
        scan_daily()
        check_candidates()
    else:
        print(f"用法: {sys.argv[0]} [scan|check|both]")
        sys.exit(1)
