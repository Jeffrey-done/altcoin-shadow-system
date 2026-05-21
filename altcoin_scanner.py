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
    setup_logger, send_tg, load_json, tg_escape,
    to_binance_symbol, utcnow, utcnow_iso, parse_iso,
    LockedJsonFile, get_compound_stake, account_param,
)
from models import Candidate, Trade
from risk_control import can_open_trade, record_trade_opened, is_in_cooldown
from signal_score import calculate_signal_score, check_btc_filter
from exchange_manager import (
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
    try:
        from runtime_config import apply_overrides as _apply_rc
        _apply_rc(force=True)
    except Exception as _e:
        logger.debug(f"scan_daily apply_overrides 失败（非致命）: {_e}")
    logger.info("=== 开始日线扫描 ===")
    # H10: 走 exchange_manager 单例，强制带 timeout，避免 fetch_tickers 卡死
    from exchange_manager import get_binance
    exchange = get_binance()

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
            # M-2 修复：极端负费率 → 做空持仓成本过高，直接跳过
            if funding < config.FUNDING_MIN:
                logger.info(
                    f"  ⛔ 跳过: {symbol} 资金费率极端负({funding:.4f}% < {config.FUNDING_MIN}%)，"
                    f"做空持仓成本过高"
                )
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
    with LockedJsonFile(CANDIDATES_FILE, default=[], lock_timeout_sec=5, lock_name='CANDIDATES_FILE') as (existing_list, save_candidates):
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

    # 清空快速预筛标记：本轮扫描已经处理了所有热门币，
    # 清空后下一轮 hot_scanner WS 会重新标记（避免旧标记累积干扰排序优先级）
    try:
        from hot_scanner import clear_hot_symbols
        clear_hot_symbols()
    except ImportError:
        pass


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
            # H13: auto 模式不再同步探测 OKX 合约，避免单次路由把整轮候选确认拖死。
            fallback = getattr(config, 'PRIMARY_EXCHANGE_FALLBACK', 'binance').lower()
            live_routes = [(fallback if fallback in ('binance', 'okx') else 'binance', stake)]
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
#  H11: 单候选评估（并发友好；纯只读，不改 candidates / trades）
# ══════════════════════════════════════════════════════════════════

def _evaluate_candidate(exchange, c, btc_pct: float, open_symbols: set,
                       per_candidate_timeout_sec: float):
    """
    评估单个候选：拉 4h RSI / 4h RSI 峰值 / 弃盘检测 / 量价背离 / OKX 交叉验证 / 评分。

    设计：
      - 纯只读（不改 c.triggered / 不写 trades）
      - 出错 / 超时返回 ('skip', reason)，由上层串行阶段决定是否记录
      - 早退优化：4h RSI 还在超买阈值（>= H4_RSI_ENTER）就直接跳，
        因为做空策略只关心"从超买回落"

    Returns:
      ('skip', reason: str)         — 不触发，跳过
      ('cooldown', reason: str)     — 冷却中
      ('trigger', payload: dict)    — 命中触发，payload 含开仓所需所有字段

    payload 字段:
      {
        'rsi_4h': float, 'rsi_4h_peak': float, 'drop': float,
        'trigger_4h': bool, 'trigger_abandon': bool,
        'abandon': dict, 'vol_divergence': dict,
        'cross_validate_bonus': int, 'okx_cv_info': str,
        'score_result': dict, 'eval_elapsed_sec': float,
      }
    """
    import time as _time
    t0 = _time.monotonic()

    # 已触发 / 已持仓
    if c.triggered or c.symbol in open_symbols:
        return ('skip', 'already_triggered_or_held')

    # 冷却期
    # M-7：按 config.COOLDOWN_SCOPE 决定全局/按账户作用域
    #   'global'      → 这里全局检查（任一账户止损都会 block 整笔信号）
    #   'per_account' → 跳过全局检查，由 _open_position_for_candidate 按账户单独检查
    if getattr(config, 'COOLDOWN_SCOPE', 'global') == 'global':
        in_cooldown, cooldown_reason = is_in_cooldown(c.symbol)
        if in_cooldown:
            return ('cooldown', cooldown_reason)

    def _expired() -> bool:
        return (_time.monotonic() - t0) > per_candidate_timeout_sec

    # ── 4h RSI ──
    rsi_4h = get_rsi(exchange, c.symbol, '4h', limit=50)
    if _expired():
        return ('skip', f'timeout_after_rsi (>{per_candidate_timeout_sec:.0f}s)')

    # H11 早退：4h RSI 还在超买阈值之上，做空触发条件
    # （rsi_4h < H4_RSI_ENTER 且 drop >= H4_RSI_DROP）一定不满足，
    # 而 abandon 仅在 1H 跌幅大时触发——4h RSI 仍超买的概率不高，
    # 所以这里早退极大概率是对的，只省一次 fetch_ohlcv（peak） + abandon 的两次调用
    if rsi_4h >= config.H4_RSI_ENTER:
        return ('skip', f'4h RSI {rsi_4h:.1f} 仍 >= {config.H4_RSI_ENTER}（未回落）')

    rsi_4h_peak = get_rsi_peak(exchange, c.symbol, '4h')
    if _expired():
        return ('skip', f'timeout_after_peak (>{per_candidate_timeout_sec:.0f}s)')

    drop = rsi_4h_peak - rsi_4h
    trigger_4h = drop >= config.H4_RSI_DROP

    # 弃盘检测（始终跑，因为可能 4h RSI 早退条件下还是被 abandon 触发——
    # 但因为上面已经 early-return rsi_4h >= H4_RSI_ENTER 了，这里只需考虑
    # rsi_4h < H4_RSI_ENTER 但 drop 不够的情况）
    abandon = detect_abandon_signal(exchange, c.symbol)
    if _expired():
        return ('skip', f'timeout_after_abandon (>{per_candidate_timeout_sec:.0f}s)')
    trigger_abandon = abandon.get("signal", False)

    if not (trigger_4h or trigger_abandon):
        # 把评估结果挂回 c（线程安全：每个 c 由自己线程独占处理）
        c.rsi_4h = rsi_4h
        c.rsi_4h_peak = rsi_4h_peak
        return ('skip', f'no_trigger (drop={drop:.1f}, abandon={abandon.get("reason","")})')

    # ── 量价背离 + OKX 交叉验证 + 评分 ──
    vol_divergence = detect_volume_divergence(exchange, c.symbol)
    if _expired():
        return ('skip', f'timeout_after_voldiv (>{per_candidate_timeout_sec:.0f}s)')

    abandon_oi = abandon.get("oi_declining", False) if trigger_abandon else False

    cross_validate_bonus = 0
    okx_cv_info = ""
    logger.info(f"  [DBG] cross-validate gate {c.symbol}")
    if config.OKX_CROSS_VALIDATE_ENABLED and okx_has_swap(c.symbol):
        funding_cv = cross_validate_funding(c.symbol, c.funding_rate)
        oi_cv = cross_validate_oi(c.symbol, c.oi_change / 100)
        # L-5 修复：把总 BONUS 拆给两个维度时用上取整 + 下取整组合，保证两者之和 = BONUS
        # 例如 BONUS=7 → funding 拿 4、oi 拿 3，总和 7（旧实现 3+3=6 损失 1 分）
        _bonus_total = config.OKX_CROSS_VALIDATE_BONUS
        _bonus_first = (_bonus_total + 1) // 2   # 上取整
        _bonus_second = _bonus_total - _bonus_first  # 余下
        if funding_cv.get("signal_boost"):
            cross_validate_bonus += _bonus_first
            okx_cv_info += "费率✓ "
        if oi_cv.get("signal_boost"):
            cross_validate_bonus += _bonus_second
            okx_cv_info += "OI✓"
        if _expired():
            return ('skip', f'timeout_after_okx_cv (>{per_candidate_timeout_sec:.0f}s)')

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

    if score_result["grade"] == "SKIP":
        c.rsi_4h = rsi_4h
        c.rsi_4h_peak = rsi_4h_peak
        return ('skip', f'low_score {score_result["score"]} < {config.SCORE_SKIP_THRESHOLD}')

    elapsed = _time.monotonic() - t0
    return ('trigger', {
        'rsi_4h': rsi_4h,
        'rsi_4h_peak': rsi_4h_peak,
        'drop': drop,
        'trigger_4h': trigger_4h,
        'trigger_abandon': trigger_abandon,
        'abandon': abandon,
        'vol_divergence': vol_divergence,
        'cross_validate_bonus': cross_validate_bonus,
        'okx_cv_info': okx_cv_info,
        'score_result': score_result,
        'eval_elapsed_sec': round(elapsed, 2),
    })


# ══════════════════════════════════════════════════════════════════
#  第二阶段：4h 确认（每1小时）
# ══════════════════════════════════════════════════════════════════

def check_candidates():
    """检查候选池，4h RSI 回落 或 弃盘点 触发时自动开仓

    H11 改造（2025-11 修复 600s 超时）：
      - 评估阶段（拉 RSI / abandon / OKX 交叉）改并发，N 路线程池
      - 整轮预算 CHECK_CANDIDATES_BUDGET_SEC（默认 480s），到点优雅退出
      - 单候选硬超时 CHECK_CANDIDATES_PER_CANDIDATE_SEC（默认 30s），超时跳过
      - 早退优化：4h RSI 仍超买（>= H4_RSI_ENTER）直接跳，省 3 次 API
      - 命中触发后才进入串行的开仓阶段（保留原有锁/路由/journal 逻辑）
    """
    try:
        from runtime_config import apply_overrides as _apply_rc
        _apply_rc(force=True)
    except Exception as _e:
        logger.debug(f"check_candidates apply_overrides 失败（非致命）: {_e}")

    import time as _time
    from concurrent.futures import (
        ThreadPoolExecutor, as_completed,
        TimeoutError as _futures_TimeoutError,
    )

    _round_t0 = _time.monotonic()
    _budget = config.CHECK_CANDIDATES_BUDGET_SEC
    _per_cand_timeout = config.CHECK_CANDIDATES_PER_CANDIDATE_SEC
    _max_workers = max(1, config.CHECK_CANDIDATES_PARALLELISM)

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
    # pending_open 守护：重试次数上限 + 过期淘汰（避免无限重试）
    _pending_max_retries = int(getattr(config, 'PENDING_OPEN_MAX_RETRIES', 3))
    _pending_expire_hours = int(getattr(config, 'PENDING_OPEN_EXPIRE_HOURS', 6))
    _now = utcnow()
    _cleaned = 0
    for _c in candidates:
        if not getattr(_c, 'pending_open', False):
            continue
        _exp = False
        if (_c.pending_open_retries or 0) >= _pending_max_retries:
            _exp = True
        elif _c.pending_opened_at:
            try:
                _age_h = (_now - parse_iso(_c.pending_opened_at)).total_seconds() / 3600
                if _age_h >= _pending_expire_hours:
                    _exp = True
            except Exception:
                pass
        if _exp:
            _c.pending_open = False
            _c.pending_opened_at = None
            _c.pending_open_retries = 0
            _cleaned += 1
    if _cleaned:
        logger.info(f"pending_open 清理 {_cleaned} 个候选（重试/超时）")
    # 预算溢出保护：优先处理上轮已触发但未执行开仓的候选
    candidates.sort(key=lambda x: (0 if getattr(x, 'pending_open', False) else 1, x.added_at))
    # H10: 走 exchange_manager 单例，强制带 timeout
    from exchange_manager import get_binance
    exchange = get_binance()
    logger.info(
        f"=== 检查候选池（{len(candidates)}个）| BTC 24h={btc_pct:+.1f}% | "
        f"预算={_budget}s 单候选超时={_per_cand_timeout}s 并发={_max_workers} ==="
    )

    # 仅用于"是否已有持仓"的预过滤（真实开仓时会在锁内再校验一次，防竞态）
    # M3: 把 close_retry_pending=True 的 closed 交易视同 open
    trades_snapshot = load_json(TRADES_FILE, [])
    open_symbols = {
        t['symbol'] for t in trades_snapshot
        if t.get('status') == 'open' or t.get('close_retry_pending')
    }

    triggered_any = False

    # ══════════════════════════════════════════════════════════════════
    #  H11 阶段一：分批并发评估候选（纯只读，可多线程）
    # ══════════════════════════════════════════════════════════════════
    #  分批的目的：ThreadPoolExecutor.__exit__ 默认 wait=True，预算到期
    #  没法立刻 break。分批后每批之间检查预算，最多浪费一批 chunk 的时间。
    #  另外 fut.result(timeout=N) 给每个 future 设硬上限，超时即视为失败
    #  跳到下一个；shutdown(wait=False) 让卡住的 ccxt 线程作为 daemon
    #  在后台被父进程统一清理（multiprocessing.Process daemon=True）。
    #
    #  关键：worker 线程必须是 daemon，否则子进程主线程退出时 Python
    #  会等所有非 daemon 线程结束 → 子进程根本不会退出，反而被父进程
    #  600s 强杀。下面 _DaemonThreadPoolExecutor 子类化 ThreadPoolExecutor
    #  在 _adjust_thread_count 后把所有 worker 标 daemon，作用域只限实例。
    # ══════════════════════════════════════════════════════════════════

    triggered_payloads = []  # [(c, payload), ...]
    skipped_count = 0
    timeout_break = False
    chunk_size = max(_max_workers * 4, 8)   # 每批 N 个候选

    def _check_budget() -> bool:
        return (_time.monotonic() - _round_t0) < _budget

    # H11-fix: 用自定义 DaemonThreadPoolExecutor 代替全局 monkey-patch threading.Thread.__init__。
    # 旧做法在 CLI 模式（主进程直接调用 check_candidates）下有竞态风险：
    # 如果 hot_scanner / tg_bot 后台线程恰好在 patch 期间创建新线程，
    # 这些无关线程会被错误标记为 daemon 而意外退出。
    # 新做法：覆盖 ThreadPoolExecutor._adjust_thread_count 在 worker 启动后设 daemon，
    # 作用域严格限制在 executor 实例内部。
    import threading as _threading

    class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
        """ThreadPoolExecutor whose worker threads are daemon threads.
        Safe: only affects this executor instance, no global side effects."""
        def _adjust_thread_count(self):
            super()._adjust_thread_count()
            for t in list(self._threads):
                if not t.daemon:
                    try:
                        t.daemon = True
                    except RuntimeError:
                        pass  # 已启动的线程无法改 daemon，忽略

    try:
        for chunk_start in range(0, len(candidates), chunk_size):
            if not _check_budget():
                elapsed = _time.monotonic() - _round_t0
                remaining = len(candidates) - chunk_start
                logger.warning(
                    f"⏰ 评估阶段预算耗尽 {elapsed:.0f}s/{_budget}s，"
                    f"剩余 {remaining} 个候选推迟到下一轮（已评估 "
                    f"{len(triggered_payloads) + skipped_count}/{len(candidates)}）"
                )
                timeout_break = True
                break

            chunk = candidates[chunk_start:chunk_start + chunk_size]
            # 单批的本地预算：min(剩余整轮预算, 单候选超时 × chunk/并发 + 余量)
            chunk_budget = min(
                _budget - (_time.monotonic() - _round_t0),
                _per_cand_timeout * (len(chunk) / _max_workers + 1),
            )
            if chunk_budget <= 0:
                timeout_break = True
                break

            executor = _DaemonThreadPoolExecutor(
                max_workers=_max_workers, thread_name_prefix='cand-eval',
            )
            try:
                future_to_c = {
                    executor.submit(
                        _evaluate_candidate, exchange, c, btc_pct, open_symbols, _per_cand_timeout,
                    ): c
                    for c in chunk
                }
                try:
                    for fut in as_completed(future_to_c, timeout=chunk_budget):
                        c = future_to_c[fut]
                        try:
                            result_kind, payload = fut.result(timeout=1)
                        except Exception as e:
                            logger.warning(f"评估候选 {c.symbol} 异常: {e}")
                            skipped_count += 1
                            continue

                        if result_kind == 'trigger':
                            logger.info(
                                f"  ✅ 触发候选 {c.symbol}: 4h RSI={payload['rsi_4h']} "
                                f"峰值={payload['rsi_4h_peak']:.1f} 回落={payload['drop']:.1f} "
                                f"评分={payload['score_result']['score']}[{payload['score_result']['grade']}] "
                                f"耗时={payload['eval_elapsed_sec']}s"
                            )
                            c.rsi_4h = payload['rsi_4h']
                            c.rsi_4h_peak = payload['rsi_4h_peak']
                            triggered_payloads.append((c, payload))
                        elif result_kind == 'cooldown':
                            logger.info(f"  ❄️ 冷却中 {c.symbol}: {payload}")
                            skipped_count += 1
                        else:
                            skipped_count += 1
                            logger.debug(f"  跳过 {c.symbol}: {payload}")
                except _futures_TimeoutError:
                    unfinished = [f for f in future_to_c if not f.done()]
                    for f in unfinished:
                        f.cancel()
                    logger.warning(
                        f"⏰ 第 {chunk_start//chunk_size + 1} 批 chunk_budget={chunk_budget:.0f}s 超时，"
                        f"放弃 {len(unfinished)} 个候选（ccxt 还在跑的会被父进程清理）"
                    )
                    skipped_count += len(unfinished)
            finally:
                # wait=False：不等已经在跑的 worker；线程是 daemon，进程退出时被 OS 清理
                executor.shutdown(wait=False)
    finally:
        pass  # No global state to restore (DaemonThreadPoolExecutor is instance-scoped)

    eval_elapsed = _time.monotonic() - _round_t0
    logger.info(
        f"评估阶段完成 {eval_elapsed:.0f}s | 命中触发={len(triggered_payloads)} "
        f"跳过={skipped_count} 超时退出={timeout_break}"
    )

    if not triggered_payloads:
        if not _check_budget():
            send_tg(
                f"⏰ <b>候选确认评估阶段超预算</b>\n\n"
                f"耗时 {eval_elapsed:.0f}s / 预算 {_budget}s\n"
                f"已评估 {skipped_count}/{len(candidates)} 个候选未命中触发\n"
                f"建议：减小候选池（提高 DAILY_RSI_MIN）或调整 BUDGET/并发数"
            )
        else:
            logger.info("候选池无触发信号")
        # 仍然要保存候选池（rsi_4h 等评估结果的回写）
        _save_candidates_back(candidates)
        return

    # ══════════════════════════════════════════════════════════════════
    #  H11 阶段二：串行开仓（保留原有锁 + 路由 + journal 逻辑）
    # ══════════════════════════════════════════════════════════════════

    for i, (c, payload) in enumerate(triggered_payloads):
        # 阶段二的剩余预算：原预算 - 已用 - 给收尾留的 60s
        if (_time.monotonic() - _round_t0) > (_budget + 60):
            remain = len(triggered_payloads) - i
            logger.warning(
                f"⏰ 开仓阶段已超 {_budget + 60}s，剩余 {remain} 笔开仓推迟到下一轮"
            )
            for c2, p2 in triggered_payloads[i:]:
                c2.pending_open = True
                c2.pending_opened_at = utcnow_iso()
                c2.pending_open_retries = int(getattr(c2, 'pending_open_retries', 0) or 0) + 1
                # 保存触发语义，下一轮优先执行
                if p2.get('trigger_abandon'):
                    c2.trigger_type = 'abandon'
                    c2.trigger_reason = f"弃盘点: {p2.get('abandon', {}).get('reason', '')}".strip()
                else:
                    c2.trigger_type = '4h_rsi'
                    c2.trigger_reason = f"4h RSI从{p2.get('rsi_4h_peak', 0):.0f}回落至{p2.get('rsi_4h', 0)}"
            break
        ok = _open_position_for_candidate(c, payload, btc_pct, exchange)
        # 不论开仓成功与否，本轮已尝试执行，清理 pending 标记
        c.pending_open = False
        c.pending_opened_at = None
        c.pending_open_retries = 0
        if ok:
            triggered_any = True

    # 保存候选池更新（rsi_4h_peak / triggered 状态等）
    _save_candidates_back(candidates)

    if not triggered_any:
        logger.info("候选池无触发信号")


# ══════════════════════════════════════════════════════════════════
#  H11 辅助：保存候选池 + 单候选开仓
# ══════════════════════════════════════════════════════════════════

def _save_candidates_back(candidates):
    """把 candidates 列表写回 CANDIDATES_FILE，保留 scan_daily 期间新加入的"""
    with LockedJsonFile(CANDIDATES_FILE, default=[], lock_timeout_sec=5, lock_name='CANDIDATES_FILE') as (existing_raw, save_candidates):
        by_symbol = {c.get('symbol'): c for c in existing_raw if c.get('symbol')}
        for c in candidates:
            by_symbol[c.symbol] = c.to_dict()
        save_candidates(list(by_symbol.values()))


def _open_position_for_candidate(c, payload: dict, btc_pct: float, exchange) -> bool:
    """
    H11 串行开仓阶段：
      - 风控预检（单/多账户）
      - 价格 + 多交易所价格确认
      - 路由计算
      - 多账户并行下单（journal + 锁）
      - 部分失败回滚（H5 对冲语义保证）
      - 成功后写交易、记风控、推 TG

    Returns True 表示至少有一笔成功开仓；False 表示完全跳过/失败。

    本函数 = 旧 check_candidates for-loop 体的开仓段落，行为与之前完全一致；
    只是抽出来便于阶段二串行调用，且能用 budget 守护剩余预算。
    """
    rsi_4h = payload['rsi_4h']
    rsi_4h_peak = payload['rsi_4h_peak']
    drop = payload['drop']
    trigger_4h = payload['trigger_4h']
    trigger_abandon = payload['trigger_abandon']
    abandon = payload['abandon']
    vol_divergence = payload['vol_divergence']
    cross_validate_bonus = payload['cross_validate_bonus']
    okx_cv_info = payload['okx_cv_info']
    score_result = payload['score_result']

    # 根据评分决定仓位（结合自动复利）
    # 注意：这是 active 账号的 base_stake，用于"展示+回退"。每个账号实际开仓 stake
    # 在下方 _trading_accts 循环里独立计算（P2-1 修复，2026-05）。
    base_stake = get_compound_stake()
    if score_result["grade"] == "A":
        actual_stake = base_stake
    else:  # grade B
        actual_stake = round(base_stake * 0.5)

    # ── 风控检查（全局预检）──
    # H8: 用每个账户"实际将占用的保证金总额"去做 can_open_trade 检查，
    # 避免 PRIMARY_EXCHANGE='both' 模式下用原始 stake 误判为超额度。
    # P2-1 修复（2026-05）：之前用 active 账号算的 stake 给所有账号做风控检查，
    # 多账号 manual 模式下不同账号有不同 DEFAULT_STAKE 时会用错值（active=A
    # 但检查 B 用的是 A 的 stake）。改为每个账号独立 get_compound_stake(acc_id)。
    try:
        from admin_secrets import (
            get_all_trading_accounts as _get_trading_accts,
            SHADOW_ACCOUNT_ID as _SHADOW_ID,
            list_accounts as _list_all,
            is_account_trading_enabled as _is_enabled,
        )
        _trading_accts = _get_trading_accts() or []
        if any(a.get('id') == _SHADOW_ID for a in _list_all()) and _is_enabled(_SHADOW_ID):
            _trading_accts = [{'id': _SHADOW_ID, 'is_shadow': True}] + _trading_accts
        if not _trading_accts:
            _trading_accts = [{'id': ''}]
    except Exception:
        _trading_accts = [{'id': ''}]

    any_allowed = False
    first_reason = ""
    acc_actual_stake_map = {}  # 避免在 TRADES_FILE 锁内再次读取 trades（会自锁阻塞）
    acc_allowed_map = {}
    for _acc in _trading_accts:
        _acc_id = _acc.get('id') or None
        _is_shadow_acc = _acc.get('is_shadow', False)

        # 该账号的独立 stake（含其复利曲线 + proportional 缩放）
        try:
            _acc_base_stake = get_compound_stake(_acc_id)
        except Exception:
            _acc_base_stake = base_stake  # fallback：用 active 算的，兼容老路径
        _acc_actual_stake = (
            _acc_base_stake if score_result["grade"] == "A"
            else round(_acc_base_stake * 0.5)
        )
        acc_actual_stake_map[_acc_id or ''] = _acc_actual_stake

        # 该账号的路由分配（PRIMARY_EXCHANGE='both' 时拆分到两所）
        _acc_routes_preview = _resolve_exchange_routes(c.symbol, _acc_actual_stake)
        _acc_live_route_sum = sum(
            s for ex, s in _acc_routes_preview if ex != 'shadow'
        ) or _acc_actual_stake
        _acc_shadow_route = next(
            (s for ex, s in _acc_routes_preview if ex == 'shadow'),
            _acc_actual_stake,
        )
        _acc_check_stake = (
            _acc_shadow_route if _is_shadow_acc else _acc_live_route_sum
        )

        _ok, _rsn = can_open_trade(_acc_check_stake, account_id=_acc_id)
        acc_allowed_map[_acc_id or ''] = (_ok, _rsn)
        if _ok:
            any_allowed = True
            break
        if not first_reason:
            first_reason = _rsn
    logger.info(f"  [DBG] precheck pass {c.symbol}: at least one account allowed")
    if not any_allowed:
        logger.warning(f"  🚫 所有账户都拒绝 {c.symbol}: {first_reason}")
        return False

    # ── 触发开仓 ──
    logger.info(f"  [DBG] fetching price {c.symbol}")
    try:
        price = exchange.fetch_ticker(c.symbol)['last']
    except Exception as e:
        logger.error(f"获取最新价失败 ({c.symbol}): {e}")
        return False

    # ── 多交易所价格确认 ──
    # H10: 先用 okx_has_swap 门禁，避免对 OKX 上不存在的币发起 fetch_ticker
    if config.OKX_CROSS_VALIDATE_ENABLED and okx_has_swap(c.symbol):
        price_cv = cross_validate_price(c.symbol, price)
        if price_cv['available'] and not price_cv['pass']:
            logger.warning(f"  ⚠️ 价格偏差过大 {c.symbol}: {price_cv['reason']}")
            c.triggered = False
            return False

    # ── 实盘路由 ──
    logger.info(f"  [DBG] resolving routes {c.symbol}")
    routes = _resolve_exchange_routes(c.symbol, actual_stake)
    if not routes:
        logger.warning(f"  ⚠️ 无可用交易所路由 {c.symbol}，跳过")
        return False

    # ── 多账户同步开仓 v5.0 ──
    logger.info(f"  [DBG] loading accounts {c.symbol}")
    from admin_secrets import get_all_trading_accounts, SHADOW_ACCOUNT_ID, list_accounts
    trading_accounts = get_all_trading_accounts()

    shadow_account = None
    try:
        from admin_secrets import is_account_trading_enabled
        for _acc in list_accounts():
            if _acc.get('id') == SHADOW_ACCOUNT_ID:
                if not is_account_trading_enabled(SHADOW_ACCOUNT_ID):
                    logger.info("  ⏩ 影子账户交易开关已关闭，跳过同步")
                    break
                shadow_account = {
                    'id': SHADOW_ACCOUNT_ID,
                    'name': _acc.get('name', '影子账户'),
                    'exchanges': {},
                    'force_shadow_route': True,
                }
                break
    except Exception as _e:
        logger.debug(f"读取影子账户失败，跳过同步: {_e}")

    all_accounts = []
    if shadow_account is not None:
        all_accounts.append(shadow_account)
    all_accounts.extend(trading_accounts)

    if not all_accounts:
        all_accounts = [{'id': '', 'name': '默认', 'exchanges': {}, 'force_shadow_route': True}]

    # ── 锁内第一阶段：去重检查 + 每账户风控检查 ──
    logger.info(f"  [DBG] acquire trades lock (check) {c.symbol}")
    with LockedJsonFile(TRADES_FILE, default=[], lock_timeout_sec=5, lock_name='TRADES_FILE') as (trades_raw_check, save_check):
        already_open_per_account = {
            (t.get('symbol'), t.get('account_id') or '')
            for t in trades_raw_check
            if t.get('status') == 'open' or t.get('close_retry_pending')
        }

        logger.info(f"  [DBG] inside trades lock, start build tasks {c.symbol}")
        execution_tasks = []
        for account in all_accounts:
            acc_id = account['id']
            acc_key = (c.symbol, acc_id or '')
            if acc_key in already_open_per_account:
                logger.info(
                    f"  ⏩ 跳过账户 {account['name']}({acc_id})：已持有 {c.symbol}"
                )
                continue

            # M-7: 按账户冷却（仅当 COOLDOWN_SCOPE='per_account' 时）
            # 'global' 模式已在 _evaluate_candidate 全局检查过，这里跳过避免重复
            # 注意：is_in_cooldown 新语义下 None/'' 都是"全账户扫描"，
            # 所以 acc_id 为空（旧单账户兼容）时 per-account 等价于 global，安全跳过避免重复检查。
            if getattr(config, 'COOLDOWN_SCOPE', 'global') == 'per_account' and acc_id:
                acc_in_cd, acc_cd_reason = is_in_cooldown(
                    c.symbol, account_id=acc_id
                )
                if acc_in_cd:
                    logger.info(
                        f"  ❄️ 跳过账户 {account['name']}({acc_id})：{acc_cd_reason}"
                    )
                    continue

            # H15: 这里已在锁外预计算过每账号 actual stake，
            # 不要在 TRADES_FILE 锁内再调用 get_compound_stake（它会读 TRADES_FILE，可能自锁阻塞）。
            _acc_actual = acc_actual_stake_map.get(acc_id or '', actual_stake)

            if account.get('force_shadow_route'):
                acc_routes = [('shadow', _acc_actual)]
            else:
                acc_routes = _resolve_exchange_routes(c.symbol, _acc_actual)
                if not acc_routes:
                    logger.info(
                        f"  ⏩ 跳过账户 {account['name']}({acc_id}): 无可用交易所路由"
                    )
                    continue

            logger.info(f"  [DBG] account loop {acc_name if 'acc_name' in locals() else account.get('name','?')} ({acc_id}) routes={acc_routes}")
            acc_stake_total = sum(s for _ex, s in acc_routes)
            # H15: 不在 TRADES_FILE 锁内再次跑 can_open_trade（该函数会读 trades，可能自锁阻塞）。
            acc_allowed, acc_risk_reason = acc_allowed_map.get(acc_id or '', (True, ''))
            logger.info(f"  [DBG] prechecked can_open_trade acc={acc_id} allowed={acc_allowed} reason={acc_risk_reason}")
            if not acc_allowed:
                logger.info(
                    f"  ⏩ 跳过账户 {account['name']}({acc_id}): {acc_risk_reason}"
                )
                continue

            for route_exchange, route_stake in acc_routes:
                execution_tasks.append((acc_id, account['name'], route_exchange, route_stake))

    logger.info(f"  [DBG] execution_tasks={len(execution_tasks)} for {c.symbol}")
    logger.info(f"  [DBG] left trades lock, execution_tasks={len(execution_tasks)}")
    if not execution_tasks:
        c.triggered = False
        return False

    # ── 锁外第二阶段：并行执行下单 ──
    from live_executor import execute_open, make_client_order_id, place_binance_short_protection_split
    from common import (
        journal_add_pending, journal_mark_confirmed, journal_mark_failed,
    )

    def _prep_task(task_args):
        acc_id, acc_name, r_exchange, r_stake = task_args
        coid = make_client_order_id('sho', c.symbol, r_exchange)
        return (acc_id, acc_name, r_exchange, r_stake, coid)

    prepared_tasks = [_prep_task(t) for t in execution_tasks]

    for acc_id, _acc_name, r_exchange, r_stake, coid in prepared_tasks:
        if r_exchange == 'shadow':
            continue
        # 阶段 4：按账号取 leverage
        if r_exchange == 'okx':
            lev = int(account_param(acc_id, 'OKX_DEFAULT_LEVERAGE', config.OKX_DEFAULT_LEVERAGE))
        else:
            lev = int(account_param(acc_id, 'LEVERAGE', config.LEVERAGE))
        try:
            journal_add_pending(
                client_order_id=coid, exchange=r_exchange,
                account_id=acc_id or '', symbol=c.symbol, direction='SHORT',
                stake=r_stake, leverage=lev,
            )
        except Exception as _je:
            logger.error(f"journal pending 写入失败（非致命）: {_je}")

    def _execute_one(task_args):
        acc_id, acc_name, r_exchange, r_stake, coid = task_args
        if r_exchange == 'shadow':
            return (acc_id, acc_name, r_exchange, r_stake, coid,
                    {"success": True, "order_id": "", "price": 0, "amount": 0, "error": ""})
        result = execute_open(
            c.symbol, 'SHORT', r_stake,
            exchange_name=r_exchange,
            leverage=(int(account_param(acc_id, 'OKX_DEFAULT_LEVERAGE', config.OKX_DEFAULT_LEVERAGE))
                      if r_exchange == 'okx'
                      else int(account_param(acc_id, 'LEVERAGE', config.LEVERAGE))),
            client_order_id=coid,
            account_id=acc_id if acc_id else None,
        )
        return (acc_id, acc_name, r_exchange, r_stake, coid, result)

    logger.info(f"  [DBG] entering execute phase {c.symbol}")
    # H12: 给开仓执行阶段增加硬超时，防止某个交易所请求卡死把整轮拖到 scheduler 600s 被强杀
    # 关键点：不能用 `with ThreadPoolExecutor(...)`，否则 __exit__ 会 wait=True 等待挂死线程。
    exec_timeout = int(getattr(config, 'CHECK_CANDIDATES_OPEN_EXEC_TIMEOUT_SEC', 45))
    executor = ThreadPoolExecutor(max_workers=max(len(prepared_tasks), 4))
    futures = [executor.submit(_execute_one, task) for task in prepared_tasks]
    results = []
    timed_out = False
    try:
        for f in as_completed(futures, timeout=exec_timeout):
            try:
                results.append(f.result(timeout=1))
            except Exception as e:
                logger.error(f"  ❌ 开仓任务执行异常: {e}")
    except TimeoutError:
        timed_out = True
        logger.error(
            f"  ⏰ 开仓执行阶段超时（>{exec_timeout}s），强制结束剩余路由任务"
        )
    finally:
        if timed_out:
            for f in futures:
                if not f.done():
                    f.cancel()
            # Python 3.9+：不等待正在执行的线程，直接返回，避免拖到 600s
            executor.shutdown(wait=False, cancel_futures=True)
        else:
            executor.shutdown(wait=True)

    # 如果有任务超时，后续按已完成结果处理；没有任何成功结果则本轮跳过并等待下一轮

    # ── 锁内第三阶段：把成功的下单结果写入 TRADES_FILE ──
    opened_trades = []
    routes_by_key: dict = {}

    with LockedJsonFile(TRADES_FILE, default=[], lock_timeout_sec=5, lock_name='TRADES_FILE') as (trades_raw, save):
        opened_any = False
        for acc_id, acc_name, route_exchange, route_stake, coid, live_result in results:
            key = (c.symbol, acc_id or '')
            bucket = routes_by_key.setdefault(key, {'success': [], 'failed': []})

            if not live_result["success"]:
                logger.error(
                    f"  ❌ [{acc_name}] {route_exchange} 下单失败 {c.symbol}: {live_result['error']}"
                )
                if route_exchange != 'shadow':
                    try:
                        journal_mark_failed(coid, live_result['error'])
                    except Exception as _je:
                        logger.debug(f"journal mark_failed 失败: {_je}")
                send_tg(
                    f"❌ <b>[{tg_escape(route_exchange.upper())}][{tg_escape(acc_name)}] 实盘下单失败</b>\n\n"
                    f"币种：{tg_escape(c.symbol)}\n"
                    f"原因：{tg_escape(live_result['error'])}\n"
                    f"本次跳过，不记录风控扣账。"
                )
                bucket['failed'].append({
                    'exchange': route_exchange, 'account_id': acc_id,
                    'account_name': acc_name, 'error': live_result['error'],
                })
                continue

            entry_price = live_result["price"] if live_result["price"] > 0 else price
            if price > 0 and entry_price > 0:
                slippage_pct = abs(entry_price - price) / price * 100
            else:
                slippage_pct = 0.0

            trade = Trade.create_short(
                c.symbol, entry_price,
                reason=trigger_reason_for_create(trigger_abandon, abandon, rsi_4h_peak, rsi_4h),
                stake=route_stake,
                leverage=(int(account_param(acc_id, 'OKX_DEFAULT_LEVERAGE', config.OKX_DEFAULT_LEVERAGE))
                          if route_exchange == 'okx'
                          else int(account_param(acc_id, 'LEVERAGE', config.LEVERAGE))),
                exchange=route_exchange,
                live_order_id=live_result.get("order_id") or None,
                client_order_id=coid,
                ref_price_at_order=price,
                slippage_pct=slippage_pct,
                account_id=acc_id,
                tp1_multiplier=float(account_param(acc_id, 'TP1_MULTIPLIER', config.TP1_MULTIPLIER)),
                tp2_multiplier=float(account_param(acc_id, 'TP2_MULTIPLIER', config.TP2_MULTIPLIER)),
                hard_stop_loss_pct=float(account_param(acc_id, 'HARD_STOP_LOSS_PCT', config.HARD_STOP_LOSS_PCT)),
                max_hold_days=int(config.MAX_HOLD_DAYS),
            )
            opened_trades.append((trade, entry_price, route_exchange, route_stake, coid))
            trades_raw.append(trade.to_dict())
            opened_any = True
            bucket['success'].append({
                'exchange': route_exchange, 'account_id': acc_id,
                'trade_id': trade.id, 'order_id': live_result.get('order_id', ''),
                'shares': trade.shares, 'direction': 'SHORT',
                'client_order_id': coid,
            })

        if not opened_any:
            c.triggered = False
            return False

        try:
            save(trades_raw)
        except Exception as _se:
            order_ids_summary = ", ".join(
                f"{s['exchange']}:{s.get('order_id') or s.get('client_order_id')}"
                for bucket in routes_by_key.values()
                for s in bucket.get('success', [])
            )
            logger.critical(
                f"🚨 trades.json 写盘失败但交易所已下单！"
                f"错误={_se} | 已成交订单={order_ids_summary}"
            )
            send_tg(
                f"🚨🚨 <b>紧急：持久化失败，交易所已成交订单</b>\n\n"
                f"币种：{tg_escape(c.symbol)}\n"
                f"错误：{tg_escape(_se)}\n\n"
                f"已成交订单（需人工到交易所核对）：\n<code>{tg_escape(order_ids_summary)}</code>\n\n"
                f"日志和 journal 文件包含完整信息；请立即检查交易所持仓。"
            )
            raise

    for _trade, _ep, _ex, _rs, _coid in opened_trades:
        if _ex == 'shadow':
            continue
        try:
            journal_mark_confirmed(_coid, _trade.live_order_id or '')
        except Exception as _je:
            logger.debug(f"journal mark_confirmed 失败（非致命）: {_je}")

    # ── H5: both 模式部分失败回滚 ──
    rollbacks = []
    for key, bucket in routes_by_key.items():
        if bucket['success'] and bucket['failed']:
            for s in bucket['success']:
                if s['exchange'] == 'shadow':
                    continue
                rollbacks.append((
                    s['trade_id'], s['exchange'], s['account_id'],
                    s['shares'], s['direction'], key[0], s['client_order_id'],
                ))

    if rollbacks:
        from live_executor import execute_close as _exec_close, make_client_order_id as _mk_coid
        for trade_id, rb_ex, rb_acc_id, rb_shares, rb_dir, rb_sym, rb_coid in rollbacks:
            rollback_coid = _mk_coid('rb', f"{rb_sym}{trade_id[-8:]}", exchange_name=rb_ex)
            logger.warning(
                f"🔁 [H5 对冲回滚] 部分失败→平掉成功那所的仓位: "
                f"{rb_sym} {rb_ex}/{rb_acc_id} {rb_shares:.4f}"
            )
            try:
                rb_result = _exec_close(
                    rb_sym, rb_dir, rb_shares,
                    exchange_name=rb_ex,
                    client_order_id=rollback_coid,
                    account_id=rb_acc_id or None,
                )
            except Exception as _re:
                rb_result = {"success": False, "error": str(_re)}

            if rb_result.get('success'):
                try:
                    with LockedJsonFile(TRADES_FILE, default=[], lock_timeout_sec=5, lock_name='TRADES_FILE') as (trs, savetr):
                        for t in trs:
                            if t.get('id') == trade_id:
                                t['status'] = 'closed'
                                t['closed_at'] = utcnow_iso()
                                t['close_reason'] = '对冲回滚（配对路由下单失败）'
                                t['close_type'] = 'manual'
                                t['close_order_id'] = rb_result.get('order_id', '')
                                t['pnl'] = 0.0
                                savetr(trs)
                                break
                except Exception as _we:
                    logger.error(f"回滚后更新 trade 状态失败: {_we}")

                send_tg(
                    f"🔁 <b>对冲回滚成功</b>\n\n"
                    f"币种：{rb_sym}\n"
                    f"回滚交易所：{rb_ex.upper()}\n"
                    f"原因：配对路由下单失败，保持对冲语义\n"
                    f"仓位已平，下一轮扫描可重新触发。"
                )
                opened_trades = [x for x in opened_trades if x[0].id != trade_id]
            else:
                send_tg(
                    f"🚨 <b>对冲回滚失败，需人工处理</b>\n\n"
                    f"币种：{tg_escape(rb_sym)}\n"
                    f"交易所：{tg_escape(rb_ex.upper())}\n"
                    f"数量：{rb_shares:.4f}\n"
                    f"错误：{tg_escape(rb_result.get('error', '未知'))}\n\n"
                    f"⚠️ 请立即手动到交易所核对并平仓。"
                )

    if not opened_trades:
        c.triggered = False
        return False

    # 交易所托管保护单：开仓后立即挂 STOP(全仓) + TP1(半仓)
    for trade, _entry_price, route_exchange, _route_stake, _coid in opened_trades:
        if route_exchange != 'binance':
            continue
        try:
            tp1_ratio = float(account_param(trade.account_id, 'TP1_CLOSE_RATIO', config.TP1_CLOSE_RATIO))
            tp1_amount = trade.shares * tp1_ratio
            stop_amount = trade.shares
            prot = place_binance_short_protection_split(
                symbol=trade.symbol,
                stop_amount=stop_amount,
                tp_amount=tp1_amount,
                hard_stop_price=trade.hard_stop_price or 0.0,
                tp_trigger_price=trade.take_profit_1,
                account_id=trade.account_id or None,
                stop_client_order_id=make_client_order_id('st1', trade.symbol, 'binance'),
                tp_client_order_id=make_client_order_id('tp1', trade.symbol, 'binance'),
            )
            if not prot.get('success'):
                send_tg(f"[BINANCE] protection order placement failed | symbol={tg_escape(trade.symbol)} | error={tg_escape(prot.get('error','unknown'))}")
            else:
                try:
                    with LockedJsonFile(TRADES_FILE, default=[], lock_timeout_sec=5, lock_name='TRADES_FILE') as (_trs, _save):
                        for _t in _trs:
                            if _t.get('id') == trade.id:
                                _t['protect_stop_algo_id'] = prot.get('stop_order_id')
                                _t['protect_tp_algo_id'] = prot.get('tp_order_id')
                                _t['protect_stage'] = 'stage1'
                                _save(_trs)
                                break
                except Exception as _we:
                    logger.debug(f"stage1 algo id backfill failed {trade.symbol}: {_we}")
        except Exception as _pe:
            logger.error(f"保护单挂单异常 {trade.symbol}: {_pe}")

    # ══ 交易写盘成功后，才改风控状态 + 发推送 ══
    c.triggered = True

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

    for trade, entry_price, route_exchange, route_stake, _coid in opened_trades:
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

    # 阶段 4：按第一个 trade 的 account_id 取阈值（per-account 缩放感知）
    _msg_acc = (opened_trades[0][0].account_id if opened_trades else None) or None
    tp1_pct = round((1 - float(account_param(_msg_acc, 'TP1_MULTIPLIER', config.TP1_MULTIPLIER))) * 100, 1)
    tp2_pct = round((1 - float(account_param(_msg_acc, 'TP2_MULTIPLIER', config.TP2_MULTIPLIER))) * 100, 1)
    hard_stop_pct = float(account_param(_msg_acc, 'HARD_STOP_LOSS_PCT', config.HARD_STOP_LOSS_PCT))

    primary_trade, primary_price, primary_ex, _, _ = opened_trades[0]

    route_tags = [t[2] for t in opened_trades]
    if set(route_tags) == {'shadow'}:
        title_prefix = "🔴 <b>影子空单已开仓</b>"
    elif len(set(route_tags)) == 1:
        title_prefix = f"🔴 <b>[{route_tags[0].upper()}] 实盘空单已开仓</b>"
    else:
        title_prefix = "🔴 <b>[双所对冲] 实盘空单已开仓</b>"

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
        f"移动止损：最高盈利回撤 {getattr(config, 'TRAIL_STOP_RETRACE_RATIO', 0.4)*100:.0f}% 触发\n\n"
        f"{trigger_desc}\n\n"
        f"日线RSI：{c.rsi_1d}（超买）\n"
        f"24h涨幅：{c.pct24h:+.1f}% | 成交量：{c.vol24h:,}U\n"
        f"OI变化：{c.oi_change:+.0f}% | 资金费率：{c.funding_rate:.4f}%/8h\n"
        f"妖币评分：{c.yao_score}/3 | BTC 24h：{btc_pct:+.1f}%"
    )
    send_tg(msg)

    return True


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
