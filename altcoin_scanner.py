#!/usr/bin/env python3
"""
小币种超买做空扫描器 v5.0
多时间框架策略：
  - 每4小时扫描全市场，筛选日线 RSI>78 的候选池
  - 每1小时检查候选池，确认 4h RSI 回落 或 1H 弃盘点 触发信号
  - 过滤条件：成交量>50万U、价格<1U、24h涨幅>10%
新增：
  - 杠杆仓位计算（10x）
  - 硬止损价格
  - 风控检查（开仓前必须通过）
  - v5.0: 多账户同步开仓（所有配置了凭证的账户毫秒级并行下单）
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict

import ccxt
import requests

import config
from common import (
    CANDIDATES_FILE, TRADES_FILE,
    setup_logger, send_tg, atomic_write_json, load_json,
    to_binance_symbol, utcnow, utcnow_iso, parse_iso,
    LockedJsonFile, get_compound_stake,
)
from models import Candidate, Trade
from risk_control import can_open_trade, record_trade_opened, is_in_cooldown
from signal_score import calculate_signal_score, check_btc_filter
from exchange_manager import (
    get_binance, get_okx,
    cross_validate_funding, cross_validate_oi,
    okx_has_swap, cross_validate_price,
)

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
    """
    获取指定时间框架的 RSI。

    丢弃最后一根 K 线：Binance 返回的最后一根是"正在形成"的，
    用它算 RSI 会在收盘前闪烁，产生假信号。
    """
    try:
        # 多取一根，扔掉未收盘的那根
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit + 1)
        if len(ohlcv) >= 2:
            ohlcv = ohlcv[:-1]
        closes = [c[4] for c in ohlcv]
        return calc_rsi_wilder(closes)
    except Exception as e:
        logger.warning(f"获取 RSI 失败 ({symbol} {timeframe}): {e}")
        return 50.0


def get_rsi_peak(exchange, symbol: str, timeframe: str = '4h',
                 lookback: int = config.H4_RSI_PEAK_LOOKBACK) -> float:
    """获取近期 RSI 峰值（O(n) Wilder 递推，丢弃未收盘 K 线）"""
    try:
        # 多取一根丢弃未收盘
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=lookback + config.RSI_PERIOD + 6)
        if len(ohlcv) >= 2:
            ohlcv = ohlcv[:-1]
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

    丢弃最后一根未收盘 K 线（避免 intra-bar 跳动触发假信号）。
    """
    try:
        # 多取一根，丢弃最后的未收盘 K
        h1 = exchange.fetch_ohlcv(symbol, '1h', limit=7)
        if len(h1) >= 2:
            h1 = h1[:-1]
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
#  量价背离检测（Volume-Price Divergence）
# ══════════════════════════════════════════════════════════════════

def detect_volume_divergence(exchange, symbol: str) -> dict:
    """
    检测量价背离：价格冲高但成交量递减 → 顶部信号更强。

    逻辑：
      1. 获取近24根1H K线
      2. 找到最近两次"价格局部高点"（高于前后2根）
      3. 如果第二个高点的价格 >= 第一个高点（新高或持平）
         但第二个高点的成交量 < 第一个高点的成交量 × 0.7（缩量30%+）
         → 量价背离成立

    返回:
      {
        "divergence": bool,        # 是否存在量价背离
        "shrink_ratio": float,     # 成交量缩减比例（0~1，越小越强）
        "score_bonus": int,        # 建议加分值（0~8）
        "reason": str,             # 描述
      }
    """
    result = {"divergence": False, "shrink_ratio": 1.0, "score_bonus": 0, "reason": ""}
    try:
        # 多取一根丢弃未收盘
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=25)
        if len(ohlcv) >= 2:
            ohlcv = ohlcv[:-1]
        if len(ohlcv) < 10:
            return result

        # 提取高点和成交量
        highs = [c[2] for c in ohlcv]   # high price
        volumes = [c[5] for c in ohlcv]  # volume

        # 找局部高点（高于前后2根K线的high）
        peaks = []  # [(index, high_price, volume)]
        for i in range(2, len(highs) - 2):
            if highs[i] >= highs[i-1] and highs[i] >= highs[i-2] \
               and highs[i] >= highs[i+1] and highs[i] >= highs[i+2]:
                peaks.append((i, highs[i], volumes[i]))

        if len(peaks) < 2:
            return result

        # 比较最近两个高点
        prev_peak = peaks[-2]
        last_peak = peaks[-1]

        prev_price, prev_vol = prev_peak[1], prev_peak[2]
        last_price, last_vol = last_peak[1], last_peak[2]

        # 价格创新高或持平（差距<1%）
        price_higher = last_price >= prev_price * 0.99

        if not price_higher:
            return result

        # 成交量缩减检查
        if prev_vol <= 0:
            return result

        vol_ratio = last_vol / prev_vol  # <1 表示缩量

        if vol_ratio < 0.70:
            # 量价背离成立：价格新高但量缩30%+
            shrink_pct = round((1 - vol_ratio) * 100, 0)
            result["divergence"] = True
            result["shrink_ratio"] = round(vol_ratio, 2)

            # 缩量越多，加分越高
            if vol_ratio < 0.40:
                result["score_bonus"] = 8   # 缩量60%+ → 强烈背离
            elif vol_ratio < 0.55:
                result["score_bonus"] = 5   # 缩量45%+ → 中等背离
            else:
                result["score_bonus"] = 3   # 缩量30%+ → 轻微背离

            result["reason"] = f"量价背离：价格新高但成交量缩{shrink_pct:.0f}%（顶部信号）"
            logger.info(f"  📉 {symbol}: {result['reason']}")

    except Exception as e:
        logger.debug(f"量价背离检测失败 ({symbol}): {e}")

    return result


# ══════════════════════════════════════════════════════════════════
#  第一阶段：日线扫描（每4小时）
# ══════════════════════════════════════════════════════════════════

def scan_daily():
    """扫描全市场，找日线 RSI > DAILY_RSI_MIN 的候选币"""
    logger.info("=== 开始日线扫描 ===")
    exchange = ccxt.binance({'enableRateLimit': True})

    # 获取快速预筛标记的热门币（优先处理）
    try:
        from hot_scanner import get_hot_symbol_list
        hot_list = get_hot_symbol_list()
        if hot_list:
            logger.info(f"  🔥 快速预筛标记了 {len(hot_list)} 个热门币，优先处理")
    except ImportError:
        hot_list = []

    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        logger.error(f"获取行情失败: {e}")
        return

    candidates: Dict[str, Candidate] = {}
    checked = 0

    # 排序：热门币优先（减少 API 调用浪费在冷门币上）
    sorted_symbols = list(tickers.keys())
    if hot_list:
        hot_set = set(hot_list)
        sorted_symbols.sort(key=lambda s: (0 if s in hot_set else 1))

    for symbol in sorted_symbols:
        ticker = tickers[symbol]
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

            # ── OKX 交叉验证 ──
            okx_cross_info = ""
            if config.OKX_CROSS_VALIDATE_ENABLED and okx_has_swap(symbol):
                funding_cv = cross_validate_funding(symbol, funding)
                oi_cv = cross_validate_oi(symbol, oi_change)

                if funding_cv["available"]:
                    okx_cross_info += f" | OKX费率={funding_cv['okx_rate']:.4f}%"
                    # 两所费率都高 → 妖币评分+1
                    if funding_cv["signal_boost"]:
                        yao_score = min(3, yao_score + 1)
                        okx_cross_info += "(✓双验证)"

                if oi_cv["available"] and oi_cv["signal_boost"]:
                    okx_cross_info += f" | OKX_OI={oi_cv['okx_oi_change']*100:.0f}%(✓双验证)"

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
                f"FR={funding:.4f}% | 评分={yao_score}{okx_cross_info}"
            )

        time.sleep(0.1)

    # 合并已有候选池（持锁 RMW 防止与 check_candidates 并发覆盖）
    with LockedJsonFile(CANDIDATES_FILE, default=[]) as (existing_list, save_candidates):
        existing = {c['symbol']: Candidate.from_dict(c) for c in existing_list}

        for sym, cand in candidates.items():
            if sym in existing:
                existing[sym].rsi_1d = cand.rsi_1d
                existing[sym].price = cand.price
                existing[sym].vol24h = cand.vol24h
                existing[sym].pct24h = cand.pct24h
            else:
                existing[sym] = cand

        # 清理过期候选（已触发的立即删除 + 超时未触发的也删除）
        now = utcnow()
        to_remove = []
        for sym, c in existing.items():
            added = parse_iso(c.added_at)
            age_hours = (now - added).total_seconds() / 3600

            if c.triggered:
                # 已触发开仓的：直接移除，不占位
                to_remove.append(sym)
            elif age_hours > config.CANDIDATE_EXPIRE_HOURS:
                # 超时未触发：超买窗口已过，信号失效
                to_remove.append(sym)
                logger.info(f"  🗑️ 移除过期候选: {sym}（已等待{age_hours:.0f}h未触发）")

        for sym in to_remove:
            del existing[sym]

        save_candidates([c.to_dict() for c in existing.values()])
        final_count = len(existing)

    logger.info(f"日线扫描完成，共检查 {checked} 个标的，候选池 {final_count} 个")


# ══════════════════════════════════════════════════════════════════
#  第二阶段：4h 确认（每1小时）
# ══════════════════════════════════════════════════════════════════
#  实盘路由辅助
# ══════════════════════════════════════════════════════════════════

def _resolve_exchange_routes(symbol: str, stake: float) -> list:
    """
    根据 config.LIVE_MODE / OKX_LIVE_MODE / PRIMARY_EXCHANGE 决定一笔信号
    要在哪些交易所开仓。

    返回 [(exchange_name, stake_portion), ...]：
      - 两个 LIVE_MODE 都关 → 纯纸上：[('shadow', stake)]
      - 只开 Binance       → [('binance', stake)]
      - 只开 OKX           → [('okx', stake)]
      - 两个都开 + PRIMARY_EXCHANGE='binance' → [('binance', stake)]
      - 两个都开 + PRIMARY_EXCHANGE='okx'     → [('okx', stake)]
      - 两个都开 + PRIMARY_EXCHANGE='both'    → [('binance', stake/2), ('okx', stake/2)]
      - 两个都开 + PRIMARY_EXCHANGE='auto'    → 按该币种在哪家有合约决定

    SHADOW_PARALLEL 模式：实盘路由前面额外插入一条 ('shadow', stake)，
    用于同步产生影子对照交易（不占风控额度）。
    """
    binance_on = config.LIVE_MODE
    okx_on = config.OKX_LIVE_MODE

    # 纯纸上
    if not binance_on and not okx_on:
        return [('shadow', stake)]

    # 实盘路由
    live_routes = []

    # 单所实盘
    if binance_on and not okx_on:
        live_routes = [('binance', stake)]
    elif okx_on and not binance_on:
        live_routes = [('okx', stake)]
    else:
        # 两所都打开 → 看 PRIMARY_EXCHANGE
        mode = getattr(config, 'PRIMARY_EXCHANGE', 'binance').lower()

        if mode == 'binance':
            live_routes = [('binance', stake)]
        elif mode == 'okx':
            live_routes = [('okx', stake)]
        elif mode == 'both':
            # 保证金各一半，向下取整到 1U（避免浮点尾数导致交易所拒单）
            half = max(1.0, round(stake / 2, 2))
            live_routes = [('binance', half), ('okx', half)]
        elif mode == 'auto':
            # 按品种覆盖决定
            try:
                has_okx = okx_has_swap(symbol)
            except Exception:
                has_okx = False
            # Binance 默认全覆盖（系统主数据源就是 Binance）
            if has_okx:
                fallback = getattr(config, 'PRIMARY_EXCHANGE_FALLBACK', 'binance').lower()
                live_routes = [(fallback if fallback in ('binance', 'okx') else 'binance', stake)]
            else:
                live_routes = [('binance', stake)]
        else:
            live_routes = [('binance', stake)]

    # 影子并行模式：在实盘路由前插入一条 shadow 路由
    if getattr(config, 'SHADOW_PARALLEL', False):
        return [('shadow', stake)] + live_routes

    return live_routes


def trigger_reason_for_create(trigger_abandon, abandon, rsi_4h_peak, rsi_4h) -> str:
    """统一生成开仓的 reason 字段，避免内联条件重复"""
    if trigger_abandon:
        return f"弃盘点: {abandon['reason']}"
    return f"4h RSI从{rsi_4h_peak:.0f}回落至{rsi_4h}"


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

    # 仅用于"是否已有持仓"的预过滤（真实开仓时会在锁内再校验一次，防竞态）
    trades_snapshot = load_json(TRADES_FILE, [])
    open_symbols = {
        t['symbol'] for t in trades_snapshot if t.get('status') == 'open'
    }

    triggered_any = False

    for c in candidates:
        if c.triggered or c.symbol in open_symbols:
            continue

        # ── 冷却期检查 ──
        in_cooldown, cooldown_reason = is_in_cooldown(c.symbol)
        if in_cooldown:
            logger.info(f"  ❄️ 冷却中 {c.symbol}: {cooldown_reason}")
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

        # ── 量价背离检查（Volume-Price Divergence）──
        vol_divergence = detect_volume_divergence(exchange, c.symbol)

        # ── 信号评分 ──
        abandon_oi = abandon.get("oi_declining", False) if trigger_abandon else False

        # OKX 交叉验证加分
        cross_validate_bonus = 0
        okx_cv_info = ""
        if config.OKX_CROSS_VALIDATE_ENABLED and okx_has_swap(c.symbol):
            funding_cv = cross_validate_funding(c.symbol, c.funding_rate)
            oi_cv = cross_validate_oi(c.symbol, c.oi_change / 100)  # oi_change 在 candidate 是百分比

            if funding_cv.get("signal_boost"):
                cross_validate_bonus += config.OKX_CROSS_VALIDATE_BONUS // 2
                okx_cv_info += "费率✓ "
            if oi_cv.get("signal_boost"):
                cross_validate_bonus += config.OKX_CROSS_VALIDATE_BONUS // 2
                okx_cv_info += "OI✓"

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
            cross_validate_bonus=cross_validate_bonus,
            vol_divergence_bonus=vol_divergence.get("score_bonus", 0),
        )

        # 评分太低跳过
        if score_result["grade"] == "SKIP":
            logger.info(f"  ⏭️ 跳过 {c.symbol}: 评分{score_result['score']}分 < {config.SCORE_SKIP_THRESHOLD}分")
            time.sleep(0.1)
            continue

        # 根据评分决定仓位（结合自动复利）
        base_stake = get_compound_stake()
        if score_result["grade"] == "A":
            actual_stake = base_stake
        else:  # grade B
            actual_stake = round(base_stake * 0.5)

        # ── 风控检查（全局预检，任一账户允许即继续；精确检查在并行循环内）──
        # 这里用默认账户做快速预筛，防止所有账户都满额时浪费 API 调用
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

        # ── 多交易所价格确认 ──
        if config.OKX_CROSS_VALIDATE_ENABLED:
            price_cv = cross_validate_price(c.symbol, price)
            if price_cv['available'] and not price_cv['pass']:
                logger.warning(f"  ⚠️ 价格偏差过大 {c.symbol}: {price_cv['reason']}")
                c.triggered = False
                continue

        # ── 实盘路由：决定这次信号要在哪些交易所开仓 ──
        # 返回 [(exchange_name, stake_portion), ...]
        # - 'shadow' 表示纯纸上模拟（两个 LIVE_MODE 都关）
        # - 单所实盘：一个条目，stake_portion = actual_stake
        # - both 模式：两个条目，stake 各半，分散交易对手风险
        routes = _resolve_exchange_routes(c.symbol, actual_stake)
        if not routes:
            logger.warning(f"  ⚠️ 无可用交易所路由 {c.symbol}，跳过")
            continue

        # ── 多账户同步开仓 v5.0 ──
        # 获取所有配置了凭证的交易账户，为每个账户并行执行开仓
        from admin_secrets import get_all_trading_accounts
        from common import get_all_trading_account_ids
        all_accounts = get_all_trading_accounts()

        # 如果没有配置任何交易账户，使用影子模式（兼容旧单账户部署）
        if not all_accounts:
            all_accounts = [{'id': '', 'name': '默认', 'exchanges': {}}]

        # ── 锁内二次校验准备 ──
        # 已开仓判重改为按 (symbol, exchange, account_id) 粒度：
        # 同币在不同账户各开一次是允许的（多账户同步模式）
        with LockedJsonFile(TRADES_FILE, default=[]) as (trades_raw, save):
            already_open_triples = {
                (t.get('symbol'), t.get('exchange', 'shadow'), t.get('account_id', ''))
                for t in trades_raw if t.get('status') == 'open'
            }

            opened_trades = []     # [(trade, entry_price, route_exchange, route_stake)]
            opened_any = False

            # 构建所有需要执行的任务：(account_id, route_exchange, route_stake)
            # 每个账户独立检查风控额度
            execution_tasks = []
            for account in all_accounts:
                acc_id = account['id']

                # ── 每账户独立风控检查 ──
                acc_allowed, acc_risk_reason = can_open_trade(
                    actual_stake, account_id=acc_id if acc_id else None
                )
                if not acc_allowed:
                    logger.info(
                        f"  ⏩ 跳过账户 {account['name']}({acc_id}): {acc_risk_reason}"
                    )
                    continue

                for route_exchange, route_stake in routes:
                    # 同币同所同账户已有持仓 → 跳过
                    if (c.symbol, route_exchange, acc_id) in already_open_triples:
                        logger.warning(
                            f"  ⏩ 跳过 {c.symbol}@{route_exchange}@{acc_id}：锁内发现已有持仓"
                        )
                        continue
                    execution_tasks.append((acc_id, account['name'], route_exchange, route_stake))

            if not execution_tasks:
                c.triggered = False
                continue

            # ── 并行执行所有账户的下单（毫秒级同步）──
            from live_executor import execute_open, make_client_order_id

            def _execute_one(task_args):
                """单个账户单个路由的下单任务"""
                acc_id, acc_name, r_exchange, r_stake = task_args
                coid = make_client_order_id('sho', c.symbol, r_exchange)

                if r_exchange == 'shadow':
                    return (acc_id, acc_name, r_exchange, r_stake,
                            {"success": True, "order_id": "", "price": 0, "amount": 0, "error": ""})

                result = execute_open(
                    c.symbol, 'SHORT', r_stake,
                    exchange_name=r_exchange,
                    leverage=(config.OKX_DEFAULT_LEVERAGE if r_exchange == 'okx' else config.LEVERAGE),
                    client_order_id=coid,
                    account_id=acc_id if acc_id else None,
                )
                return (acc_id, acc_name, r_exchange, r_stake, result)

            # 使用 ThreadPoolExecutor 实现毫秒级并行
            # max_workers = 账户数 × 路由数，确保所有下单同时发出
            with ThreadPoolExecutor(max_workers=max(len(execution_tasks), 4)) as executor:
                futures = [executor.submit(_execute_one, task) for task in execution_tasks]
                results = [f.result() for f in as_completed(futures)]

            # 处理执行结果
            for acc_id, acc_name, route_exchange, route_stake, live_result in results:
                if not live_result["success"]:
                    logger.error(
                        f"  ❌ [{acc_name}] {route_exchange} 下单失败 {c.symbol}: {live_result['error']}"
                    )
                    send_tg(
                        f"❌ <b>[{route_exchange.upper()}][{acc_name}] 实盘下单失败</b>\n\n"
                        f"币种：{c.symbol}\n"
                        f"原因：{live_result['error']}\n"
                        f"本次跳过，不记录风控扣账。"
                    )
                    continue

                # 成交均价优先，否则回退到 ticker 价格（影子模式总是走这条）
                entry_price = live_result["price"] if live_result["price"] > 0 else price

                trade = Trade.create_short(
                    c.symbol, entry_price, reason=trigger_reason_for_create(trigger_abandon, abandon, rsi_4h_peak, rsi_4h),
                    stake=route_stake,
                    leverage=(config.OKX_DEFAULT_LEVERAGE if route_exchange == 'okx' else config.LEVERAGE),
                    exchange=route_exchange,
                    live_order_id=live_result.get("order_id") or None,
                )
                # 覆盖 trade 的 account_id（create_short 默认用 active_account）
                trade.account_id = acc_id
                opened_trades.append((trade, entry_price, route_exchange, route_stake))
                trades_raw.append(trade.to_dict())
                opened_any = True

            if not opened_any:
                # 所有路由都失败/重复 → 跳过这个信号
                c.triggered = False
                continue

            save(trades_raw)

        # ══ 交易写盘成功后，才改风控状态 + 发推送 ══
        c.triggered = True
        triggered_any = True

        if trigger_abandon:
            trigger_reason = f"弃盘点: {abandon['reason']}"
            c.trigger_type = 'abandon'
        else:
            trigger_reason = f"4h RSI从{rsi_4h_peak:.0f}回落至{rsi_4h}"
            c.trigger_type = '4h_rsi'
        c.trigger_reason = trigger_reason

        logger.info(
            f"  🚨 触发信号: {c.symbol} 开仓 {len(opened_trades)} 笔 "
            f"[{', '.join(t[2] for t in opened_trades)}] [{trigger_reason}]"
        )

        # 每一条 Trade 都单独记录风控 + 推送（因为每笔都是独立的风控事件）
        for trade, entry_price, route_exchange, route_stake in opened_trades:
            # 影子并行模式下的 shadow 交易不计入风控
            if route_exchange == 'shadow' and getattr(config, 'SHADOW_PARALLEL', False):
                continue
            record_trade_opened(route_stake, account_id=trade.account_id or None)

            logger.info(
                f"  ✅ 已开空单 [{route_exchange}]: {c.symbol} @ {entry_price} | "
                f"评分={score_result['score']}[{score_result['grade']}] | "
                f"保证金={trade.stake}U × {trade.leverage}x = {trade.notional}U | "
                f"硬止损={trade.hard_stop_price} | 订单={trade.live_order_id or 'SHADOW'}"
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

        tp1_pct = round((1 - config.TP1_MULTIPLIER) * 100, 1)
        tp2_pct = round((1 - config.TP2_MULTIPLIER) * 100, 1)
        hard_stop_pct = config.HARD_STOP_LOSS_PCT

        # 多路由开仓时，用第一笔作为主展示，额外列出其他路由摘要
        primary_trade, primary_price, primary_ex, _ = opened_trades[0]

        # 标题：区分影子 / 币安实盘 / OKX实盘 / 两所同开
        route_tags = [t[2] for t in opened_trades]
        if set(route_tags) == {'shadow'}:
            title_prefix = "🔴 <b>影子空单已开仓</b>"
        elif len(set(route_tags)) == 1:
            title_prefix = f"🔴 <b>[{route_tags[0].upper()}] 实盘空单已开仓</b>"
        else:
            title_prefix = f"🔴 <b>[双所对冲] 实盘空单已开仓</b>"

        extra_routes_line = ""
        if len(opened_trades) > 1:
            extra_routes_line = "\n📮 路由：" + " + ".join(
                f"{t[2].upper()}({t[0].stake}U×{t[0].leverage}x)"
                for t in opened_trades
            )

        msg = (
            f"{title_prefix} {yao_tag}\n\n"
            f"📌 <b>{c.symbol}</b>{extra_routes_line}\n"
            f"📊 信号评分：<b>{score_result['score']}分 [{score_result['grade']}]</b>\n"
            f"评分详情：RSI={score_result['details'].get('rsi',0):.0f} "
            f"妖={score_result['details'].get('yao',0):.0f} "
            f"触发={score_result['details'].get('trigger',0):.0f} "
            f"热度={score_result['details'].get('heat',0):.0f}"
            f"{(' OKX=' + okx_cv_info) if okx_cv_info else ''}"
            f"{(' 量价背离=+' + str(vol_divergence.get('score_bonus', 0))) if vol_divergence.get('divergence') else ''}\n\n"
            f"入场价：{primary_price:.6f} U ({primary_ex})\n"
            f"保证金：{primary_trade.stake}U × {primary_trade.leverage}x = <b>{primary_trade.notional}U</b>\n"
            f"止盈一档：{primary_trade.take_profit_1:.6f}（-{tp1_pct}%，+{primary_trade.notional*tp1_pct/100*config.TP1_CLOSE_RATIO:.1f}U）\n"
            f"止盈二档：{primary_trade.take_profit_2:.6f}（-{tp2_pct}%，+{primary_trade.notional*tp2_pct/100*(1-config.TP1_CLOSE_RATIO):.1f}U）\n"
            f"硬止损：{primary_trade.hard_stop_price:.6f}（+{hard_stop_pct}%，-{primary_trade.notional*hard_stop_pct/100:.1f}U）\n"
            f"移动止损：最高盈利回撤{config.TRAIL_STOP_DRAWDOWN_PCT*100:.0f}%触发\n\n"
            f"{trigger_desc}\n\n"
            f"日线RSI：{c.rsi_1d}（超买）\n"
            f"24h涨幅：{c.pct24h:+.1f}% | 成交量：{c.vol24h:,}U\n"
            f"OI变化：{c.oi_change:+.0f}% | 资金费率：{c.funding_rate:.4f}%/8h\n"
            f"妖币评分：{c.yao_score}/3 | BTC 24h：{btc_pct:+.1f}%"
        )
        send_tg(msg)

        time.sleep(0.1)

    # 保存候选池更新（持锁 RMW，避免与 scan_daily 并发覆盖）
    with LockedJsonFile(CANDIDATES_FILE, default=[]) as (existing_raw, save_candidates):
        # 用 symbol 映射合并：保留 scan_daily 期间新加入的候选
        by_symbol = {c.get('symbol'): c for c in existing_raw if c.get('symbol')}
        for c in candidates:
            by_symbol[c.symbol] = c.to_dict()
        save_candidates(list(by_symbol.values()))

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
