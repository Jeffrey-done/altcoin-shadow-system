#!/usr/bin/env python3
"""
信号评分 & BTC 趋势过滤 v1.0
功能：
  - 综合评分 0~100：RSI强度 + 妖币特征 + 触发方式 + OI/费率
  - 根据评分决定仓位大小（全仓/半仓/跳过）
  - BTC 24h 暴跌时暂停做空山寨（防止被反弹打止损）

被 altcoin_scanner.py 调用，在触发开仓前执行评分和过滤。
"""

import requests

import config
from common import setup_logger, to_binance_symbol

logger = setup_logger("signal_score")


# ══════════════════════════════════════════════════════════════════
#  BTC 趋势过滤
# ══════════════════════════════════════════════════════════════════

def get_btc_24h_change() -> float:
    """获取 BTC/USDT 24h 涨跌幅（%）"""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": "BTCUSDT"},
            timeout=5,
        )
        if r.status_code != 200:
            return 0.0
        return float(r.json().get('priceChangePercent', 0))
    except Exception as e:
        logger.warning(f"获取 BTC 涨跌幅失败: {e}")
        return 0.0


def check_btc_filter() -> tuple:
    """
    检查 BTC 趋势过滤。

    返回: (allowed: bool, btc_pct: float, reason: str)
      - allowed=True: 可以做空山寨
      - allowed=False: BTC 暴跌中，暂停做空
    """
    if not config.BTC_FILTER_ENABLED:
        return True, 0.0, "BTC过滤已关闭"

    btc_pct = get_btc_24h_change()

    if btc_pct <= config.BTC_CRASH_THRESHOLD:
        reason = (
            f"BTC 24h 跌{btc_pct:.1f}%（阈值{config.BTC_CRASH_THRESHOLD}%），"
            f"暂停做空山寨（防反弹打止损）"
        )
        logger.warning(f"🚫 BTC过滤: {reason}")
        return False, btc_pct, reason

    return True, btc_pct, "OK"


# ══════════════════════════════════════════════════════════════════
#  改良版2560均线趋势确认（4h EMA6/EMA15 系统）
# ══════════════════════════════════════════════════════════════════

def calc_ema(closes: list, period: int) -> list:
    """
    计算 EMA 序列。
    multiplier = 2 / (period + 1)
    第一个值用 SMA 初始化。
    """
    if len(closes) < period:
        return []

    multiplier = 2.0 / (period + 1)
    ema_values = []

    # SMA 初始化
    sma = sum(closes[:period]) / period
    ema_values.append(sma)

    # EMA 递推
    for i in range(period, len(closes)):
        ema = (closes[i] - ema_values[-1]) * multiplier + ema_values[-1]
        ema_values.append(ema)

    return ema_values


def get_ma2560_trend(exchange, symbol: str) -> dict:
    """
    获取4h级别 EMA6/EMA15 趋势状态（改良版2560系统）。

    返回:
      {
        "available": bool,       # 数据是否可用
        "ema_fast": float,       # 当前 EMA6 值
        "ema_slow": float,       # 当前 EMA15 值
        "gap_pct": float,        # 快慢线距离百分比（正=多头，负=空头）
        "alignment": str,        # 'bullish' | 'bearish' | 'convergence'
        "cross_signal": str,     # 'golden_cross' | 'death_cross' | 'none'
        "trend_strength": float, # 趋势强度 0~1（基于gap_pct与发散阈值比值）
      }
    """
    result = {
        "available": False,
        "ema_fast": 0.0,
        "ema_slow": 0.0,
        "gap_pct": 0.0,
        "alignment": "convergence",
        "cross_signal": "none",
        "trend_strength": 0.0,
    }

    if not config.MA2560_ENABLED:
        return result

    try:
        ohlcv = exchange.fetch_ohlcv(
            symbol,
            config.MA2560_TIMEFRAME,
            limit=config.MA2560_KLINE_LIMIT,
        )
        closes = [c[4] for c in ohlcv]

        if len(closes) < config.MA2560_SLOW_PERIOD + 2:
            logger.debug(f"MA2560: {symbol} K线数据不足({len(closes)}根)")
            return result

        # 计算 EMA 序列
        ema_fast_series = calc_ema(closes, config.MA2560_FAST_PERIOD)
        ema_slow_series = calc_ema(closes, config.MA2560_SLOW_PERIOD)

        if len(ema_fast_series) < 2 or len(ema_slow_series) < 2:
            return result

        # 当前值（取各自序列最后一个值）
        ema_fast_now = ema_fast_series[-1]
        ema_slow_now = ema_slow_series[-1]

        # 前一根值（用于交叉判断）
        ema_fast_prev = ema_fast_series[-2]
        ema_slow_prev = ema_slow_series[-2]

        # 快慢线距离百分比（正=快线在上=多头，负=快线在下=空头）
        if ema_slow_now > 0:
            gap_pct = (ema_fast_now - ema_slow_now) / ema_slow_now * 100
        else:
            gap_pct = 0.0

        # 趋势排列判断
        abs_gap = abs(gap_pct)
        if abs_gap < config.MA2560_CONVERGENCE_PCT:
            alignment = "convergence"  # 粘合
        elif gap_pct > 0:
            alignment = "bullish"      # 多头排列
        else:
            alignment = "bearish"      # 空头排列

        # 交叉信号判断（当前根发生交叉）
        cross_signal = "none"
        if ema_fast_prev <= ema_slow_prev and ema_fast_now > ema_slow_now:
            cross_signal = "golden_cross"  # 金叉
        elif ema_fast_prev >= ema_slow_prev and ema_fast_now < ema_slow_now:
            cross_signal = "death_cross"   # 死叉

        # 趋势强度（0~1）：gap越大趋势越强，以发散阈值为满分参考
        trend_strength = min(1.0, abs_gap / config.MA2560_DIVERGENCE_PCT)

        result = {
            "available": True,
            "ema_fast": round(ema_fast_now, 8),
            "ema_slow": round(ema_slow_now, 8),
            "gap_pct": round(gap_pct, 3),
            "alignment": alignment,
            "cross_signal": cross_signal,
            "trend_strength": round(trend_strength, 3),
        }

        logger.debug(
            f"MA2560: {symbol} | EMA{config.MA2560_FAST_PERIOD}={ema_fast_now:.6f} "
            f"EMA{config.MA2560_SLOW_PERIOD}={ema_slow_now:.6f} | "
            f"间距={gap_pct:+.3f}% | 排列={alignment} | "
            f"交叉={cross_signal} | 强度={trend_strength:.2f}"
        )

        return result

    except Exception as e:
        logger.warning(f"MA2560: 获取趋势失败 ({symbol}): {e}")
        return result


def calc_ma2560_score(ma_trend: dict, direction: str = 'short') -> dict:
    """
    根据均线趋势状态计算加减分。

    参数:
      ma_trend: get_ma2560_trend() 的返回值
      direction: 'short'（做空）或 'long'（做多）

    返回:
      {
        "bonus": int,            # 加减分值（正=加分，负=减分）
        "reason": str,           # 说明文字
        "alignment": str,        # 趋势排列
        "cross_signal": str,     # 交叉信号
      }
    """
    if not ma_trend.get("available", False):
        return {"bonus": 0, "reason": "均线数据不可用", "alignment": "unknown", "cross_signal": "none"}

    alignment = ma_trend["alignment"]
    cross_signal = ma_trend["cross_signal"]
    trend_strength = ma_trend["trend_strength"]
    gap_pct = ma_trend["gap_pct"]

    bonus = 0
    reasons = []

    if direction == 'short':
        # ── 做空评分逻辑 ──
        if alignment == "bearish":
            # 空头排列：趋势一致，加分
            bonus = round(config.MA2560_BONUS_MAX * trend_strength)
            reasons.append(f"空头排列({gap_pct:+.2f}%)")
        elif alignment == "bullish":
            # 多头排列：趋势矛盾，减分
            bonus = round(config.MA2560_PENALTY_MAX * trend_strength)
            reasons.append(f"多头排列({gap_pct:+.2f}%)⚠️逆势")
        else:
            # 粘合：中性，微加分（即将选方向，波动率可能扩大）
            bonus = 2
            reasons.append(f"均线粘合({gap_pct:+.2f}%)")

        # 交叉信号额外调整
        if cross_signal == "death_cross":
            bonus += 3
            reasons.append("死叉确认↓")
        elif cross_signal == "golden_cross":
            bonus -= 3
            reasons.append("金叉警告↑")

    else:
        # ── 做多评分逻辑 ──
        if alignment == "bullish":
            bonus = round(config.MA2560_BONUS_MAX * trend_strength)
            reasons.append(f"多头排列({gap_pct:+.2f}%)")
        elif alignment == "bearish":
            bonus = round(config.MA2560_PENALTY_MAX * trend_strength)
            reasons.append(f"空头排列({gap_pct:+.2f}%)⚠️逆势")
        else:
            bonus = 2
            reasons.append(f"均线粘合({gap_pct:+.2f}%)")

        if cross_signal == "golden_cross":
            bonus += 3
            reasons.append("金叉确认↑")
        elif cross_signal == "death_cross":
            bonus -= 3
            reasons.append("死叉警告↓")

    # 限幅
    bonus = max(config.MA2560_PENALTY_MAX, min(config.MA2560_BONUS_MAX, bonus))

    reason = " | ".join(reasons) if reasons else "均线中性"

    return {
        "bonus": bonus,
        "reason": reason,
        "alignment": alignment,
        "cross_signal": cross_signal,
    }


# ══════════════════════════════════════════════════════════════════
#  信号评分
# ══════════════════════════════════════════════════════════════════

def calculate_signal_score(
    rsi_1d: float,
    rsi_4h: float,
    rsi_4h_peak: float,
    pct_24h: float,
    oi_change: float,         # 百分比，如 30.0 表示 +30%
    funding_rate: float,      # %/8h
    yao_score: int,           # 0~3
    trigger_type: str,        # 'abandon' | '4h_rsi'
    abandon_oi_declining: bool = False,
    btc_24h_pct: float = 0.0,
    ma2560_trend: dict = None,  # get_ma2560_trend() 返回值（均线趋势确认）
) -> dict:
    """
    综合评分 0~100（+均线加减分±10）。

    评分维度：
      1. RSI 强度（0~25）：日线 RSI 越高、4h 回落越深，信号越强
      2. 妖币特征（0~25）：yao_score 映射
      3. 触发方式（0~25）：弃盘点 > 4h RSI 回落
      4. 市场热度（0~25）：OI 变化 + 资金费率 + BTC 趋势加分
      5. 均线趋势（-10~+10）：改良2560系统，趋势一致加分/矛盾减分

    返回:
      {
        "score": 0~110,
        "grade": "A" / "B" / "C" / "SKIP",
        "stake": 实际保证金,
        "details": {各维度分数},
        "reason": 评分说明
      }
    """
    if not config.SIGNAL_SCORE_ENABLED:
        # 未启用评分，全部使用默认仓位
        return {
            "score": 100,
            "grade": "A",
            "stake": config.DEFAULT_STAKE,
            "details": {},
            "reason": "评分系统未启用，使用默认仓位",
        }

    # ── 维度1：RSI 强度（0~25）──
    # 日线RSI: 78→10, 85→18, 90+→25
    rsi_score = min(25, max(0, (rsi_1d - 75) * 1.5))

    # 4h 回落深度加分：回落越多越好
    drop = rsi_4h_peak - rsi_4h
    rsi_score += min(5, drop * 0.3)  # 最多加5分
    rsi_score = min(25, rsi_score)

    # ── 维度2：妖币特征（0~25）──
    yao_map = {0: 0, 1: 8, 2: 16, 3: 25}
    yao_dim_score = yao_map.get(yao_score, 0)

    # ── 维度3：触发方式（0~25）──
    if trigger_type == 'abandon':
        trigger_score = 20
        if abandon_oi_declining:
            trigger_score = 25  # 弃盘点 + OI 同步下降 = 满分
    else:
        # 4h RSI 回落：基础15分
        trigger_score = 15

    # ── 维度4：市场热度（0~25）──
    heat_score = 0.0

    # OI 变化（0~10）：OI 涨得越多，说明主力建仓越积极
    if oi_change >= 50:
        heat_score += 10
    elif oi_change >= 30:
        heat_score += 7
    elif oi_change >= 15:
        heat_score += 4

    # 资金费率（0~8）：费率越高，多头越拥挤
    if funding_rate >= 0.05:
        heat_score += 8
    elif funding_rate >= 0.03:
        heat_score += 5
    elif funding_rate >= 0.01:
        heat_score += 2

    # BTC 趋势加分（0~7）：BTC 涨时山寨冲高回落概率更大
    if btc_24h_pct >= config.BTC_PUMP_THRESHOLD:
        heat_score += 7
    elif btc_24h_pct >= 3:
        heat_score += 4
    elif btc_24h_pct >= 0:
        heat_score += 2

    heat_score = min(25, heat_score)

    # ── 维度5：均线趋势确认（-10~+10，改良2560系统）──
    ma2560_result = calc_ma2560_score(ma2560_trend or {}, direction='short')
    ma_bonus = ma2560_result["bonus"]

    # ── 总分 ──
    base_score = round(rsi_score + yao_dim_score + trigger_score + heat_score)
    total_score = base_score + ma_bonus
    total_score = max(0, min(110, total_score))

    # ── 评级 & 仓位 ──
    if total_score >= config.SCORE_FULL_THRESHOLD:
        grade = "A"
        stake = config.DEFAULT_STAKE
    elif total_score >= config.SCORE_HALF_THRESHOLD:
        grade = "B"
        stake = round(config.DEFAULT_STAKE * 0.5)
    else:
        grade = "SKIP"
        stake = 0

    # 构建说明
    details = {
        "rsi": round(rsi_score, 1),
        "yao": round(yao_dim_score, 1),
        "trigger": round(trigger_score, 1),
        "heat": round(heat_score, 1),
        "ma2560": ma_bonus,
    }

    reason_parts = []
    if rsi_score >= 18:
        reason_parts.append(f"RSI极强({rsi_1d})")
    if yao_dim_score >= 16:
        reason_parts.append(f"妖币特征明显")
    if trigger_score >= 20:
        reason_parts.append(f"弃盘点触发")
    if heat_score >= 15:
        reason_parts.append(f"市场热度高")
    if ma_bonus >= 5:
        reason_parts.append(f"均线顺势({ma2560_result['reason']})")
    elif ma_bonus <= -5:
        reason_parts.append(f"均线逆势({ma2560_result['reason']})")

    reason = " + ".join(reason_parts) if reason_parts else "信号一般"

    result = {
        "score": total_score,
        "grade": grade,
        "stake": stake,
        "details": details,
        "reason": reason,
    }

    logger.info(
        f"  📊 评分={total_score} [{grade}] | "
        f"RSI={rsi_score:.0f} 妖={yao_dim_score:.0f} "
        f"触发={trigger_score:.0f} 热度={heat_score:.0f} "
        f"MA={ma_bonus:+d} | "
        f"仓位={stake}U | {reason}"
    )

    return result


# ══════════════════════════════════════════════════════════════════
#  做多信号评分
# ══════════════════════════════════════════════════════════════════

def calculate_long_signal_score(
    rsi: float,
    strategy_type: str,  # 'breakout_pullback' | 'pin_bar_bottom'
    pullback_pct: float = 0.0,
    shadow_pct: float = 0.0,
    oi_increasing: bool = False,
    vol_ratio: float = 1.0,  # current_vol / avg_vol
    btc_24h_pct: float = 0.0,
    ma2560_trend: dict = None,  # get_ma2560_trend() 返回值（均线趋势确认）
) -> dict:
    """
    做多信号评分 0~100（+均线加减分±10）。

    评分维度：
      1. RSI 强度（0~25）：插针时 RSI 越低越强，突破时 RSI 40~50 健康回调
      2. 形态质量（0~25）：插针下影线长度 / 突破回踩深度
      3. OI/成交量确认（0~25）：OI 增加 + 成交量放大
      4. 市场环境（0~25）：BTC 趋势对做多的支持度
      5. 均线趋势（-10~+10）：改良2560系统，趋势一致加分/矛盾减分

    返回:
      {
        "score": 0~110,
        "grade": "A" / "B" / "SKIP",
        "stake": 实际保证金,
        "details": {各维度分数},
        "reason": 评分说明
      }
    """
    # ── 维度1：RSI 强度（0~25）──
    rsi_score = 0.0
    if strategy_type == 'pin_bar_bottom':
        # 插针抄底：RSI 越低越好
        if rsi < 15:
            rsi_score = 25
        elif rsi < 20:
            rsi_score = 18
        elif rsi < 25:
            rsi_score = 12
        else:
            rsi_score = 5
    else:
        # 突破回踩：RSI 40~50 最健康
        if 40 <= rsi <= 50:
            rsi_score = 20
        elif 35 <= rsi < 40 or 50 < rsi <= 55:
            rsi_score = 15
        elif 30 <= rsi < 35 or 55 < rsi <= 60:
            rsi_score = 10
        else:
            rsi_score = 5

    # ── 维度2：形态质量（0~25）──
    pattern_score = 0.0
    if strategy_type == 'pin_bar_bottom':
        # 下影线越长越好
        if shadow_pct > 5:
            pattern_score = 25
        elif shadow_pct > 3:
            pattern_score = 18
        elif shadow_pct > 2:
            pattern_score = 12
        else:
            pattern_score = 5
    else:
        # 突破回踩：浅回踩（1~3%）最佳
        if 1 <= pullback_pct <= 3:
            pattern_score = 25
        elif 3 < pullback_pct <= 5:
            pattern_score = 15
        elif pullback_pct < 1:
            pattern_score = 8
        else:
            pattern_score = 5

    # ── 维度3：OI/成交量确认（0~25）──
    oi_vol_score = 0.0
    if oi_increasing:
        oi_vol_score += 15
    if vol_ratio > 1.5:
        oi_vol_score += 10
    elif vol_ratio > 1.0:
        oi_vol_score += 5
    oi_vol_score = min(25, oi_vol_score)

    # ── 维度4：市场环境（0~25）──
    market_score = 0.0
    if 3 <= btc_24h_pct <= 8:
        # BTC 温和上涨，做多环境好
        market_score = 25
    elif 0 <= btc_24h_pct < 3:
        # BTC 稳定
        market_score = 15
    elif btc_24h_pct > 8:
        # BTC 暴涨，山寨可能跟涨
        market_score = 20
    elif -5 <= btc_24h_pct < 0:
        # BTC 小跌
        market_score = 10
    else:
        # BTC 暴跌 >5%，做多风险大
        market_score = 5

    # ── 维度5：均线趋势确认（-10~+10，改良2560系统）──
    ma2560_result = calc_ma2560_score(ma2560_trend or {}, direction='long')
    ma_bonus = ma2560_result["bonus"]

    # ── 总分 ──
    base_score = round(rsi_score + pattern_score + oi_vol_score + market_score)
    total_score = base_score + ma_bonus
    total_score = max(0, min(110, total_score))

    # ── 评级 & 仓位 ──
    if total_score >= config.SCORE_FULL_THRESHOLD:
        grade = "A"
        stake = config.LONG_STAKE
    elif total_score >= config.SCORE_HALF_THRESHOLD:
        grade = "B"
        stake = round(config.LONG_STAKE * 0.5)
    else:
        grade = "SKIP"
        stake = 0

    # 构建说明
    details = {
        "rsi": round(rsi_score, 1),
        "pattern": round(pattern_score, 1),
        "oi_vol": round(oi_vol_score, 1),
        "market": round(market_score, 1),
        "ma2560": ma_bonus,
    }

    reason_parts = []
    if rsi_score >= 18:
        reason_parts.append(f"RSI极端({rsi:.0f})")
    if pattern_score >= 18:
        reason_parts.append("形态优秀")
    if oi_vol_score >= 15:
        reason_parts.append("OI/量确认")
    if market_score >= 20:
        reason_parts.append("市场环境好")
    if ma_bonus >= 5:
        reason_parts.append(f"均线顺势({ma2560_result['reason']})")
    elif ma_bonus <= -5:
        reason_parts.append(f"均线逆势({ma2560_result['reason']})")

    reason = " + ".join(reason_parts) if reason_parts else "信号一般"

    result = {
        "score": total_score,
        "grade": grade,
        "stake": stake,
        "details": details,
        "reason": reason,
    }

    logger.info(
        f"  📊 做多评分={total_score} [{grade}] | "
        f"RSI={rsi_score:.0f} 形态={pattern_score:.0f} "
        f"OI量={oi_vol_score:.0f} 市场={market_score:.0f} "
        f"MA={ma_bonus:+d} | "
        f"仓位={stake}U | {reason}"
    )

    return result
