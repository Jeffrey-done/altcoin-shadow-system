"""
技术指标计算 — 从 altcoin_scanner.py 抽取的纯函数。
无副作用、无 IO、可向量化。
"""

from typing import List


def calc_rsi_wilder(closes: List[float], period: int = 14) -> float:
    """
    Wilder 平滑 RSI（与 TradingView 一致）。
    第一段用 SMA 初始化，后续用 EMA 递推。

    参数:
      closes: 收盘价序列（从旧到新）
      period: RSI 计算周期

    返回:
      RSI 值 (0~100)
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


def calc_rsi_series(closes: List[float], period: int = 14) -> List[float]:
    """
    计算完整 RSI 序列（用于找峰值）。

    返回:
      RSI 序列（长度 = len(closes) - period）
    """
    if len(closes) < period + 1:
        return []

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

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

    return rsi_series


def find_rsi_peak(closes: List[float], period: int = 14, lookback: int = 10) -> float:
    """
    找近 N 根 K 线的 RSI 峰值。

    参数:
      closes: 收盘价序列（从旧到新，不含未收盘 K 线）
      period: RSI 周期
      lookback: 回溯 K 线数

    返回:
      RSI 峰值
    """
    rsi_list = calc_rsi_series(closes, period)
    if not rsi_list:
        return 50.0
    return round(max(rsi_list[-lookback:]), 1)


def detect_abandon_signal(candles_1h: List[List[float]],
                          body_drop_pct: float = 3.0,
                          consecutive: int = 2) -> dict:
    """
    检测 1H K 线弃盘点信号。

    参数:
      candles_1h: OHLCV 数据 [[ts, open, high, low, close, vol], ...]
                  已丢弃最后一根未收盘 K 线
      body_drop_pct: 实体下跌阈值 (%)
      consecutive: 连续满足的 K 线数

    返回:
      {'signal': bool, 'reason': str, 'drop_pct': float, 'drops': list}
    """
    if len(candles_1h) < 3:
        return {"signal": False, "reason": "数据不足"}

    drops = []
    for candle in candles_1h[-3:]:
        open_p, close_p = candle[1], candle[4]
        if open_p <= 0:
            continue
        body_drop = (open_p - close_p) / open_p * 100
        drops.append(round(body_drop, 2))

    matching = sum(1 for d in drops[-consecutive:] if d > body_drop_pct)

    if matching < consecutive:
        return {"signal": False, "reason": f"1H跌幅不足({drops[-2:]})", "drops": drops}

    total_drop = sum(d for d in drops[-consecutive:] if d > 0)
    reason = f"连续{matching}根1H实体下跌{total_drop:.1f}%"

    return {
        "signal": True,
        "reason": reason,
        "drop_pct": total_drop,
        "drops": drops[-consecutive:],
    }


def detect_volume_divergence(ohlcv_1h: List[List[float]]) -> dict:
    """
    检测量价背离：价格冲高但成交量递减 → 顶部信号。

    参数:
      ohlcv_1h: 1小时 OHLCV 数据（已丢弃未收盘 K 线）

    返回:
      {'divergence': bool, 'shrink_ratio': float, 'score_bonus': int, 'reason': str}
    """
    result = {"divergence": False, "shrink_ratio": 1.0, "score_bonus": 0, "reason": ""}

    if len(ohlcv_1h) < 10:
        return result

    highs = [c[2] for c in ohlcv_1h]
    volumes = [c[5] for c in ohlcv_1h]

    # 找局部高点（高于前后2根K线的high）
    peaks = []
    for i in range(2, len(highs) - 2):
        if (highs[i] >= highs[i-1] and highs[i] >= highs[i-2]
                and highs[i] >= highs[i+1] and highs[i] >= highs[i+2]):
            peaks.append((i, highs[i], volumes[i]))

    if len(peaks) < 2:
        return result

    prev_peak = peaks[-2]
    last_peak = peaks[-1]
    prev_price, prev_vol = prev_peak[1], prev_peak[2]
    last_price, last_vol = last_peak[1], last_peak[2]

    # 价格创新高或持平（差距<1%）
    if last_price < prev_price * 0.99:
        return result

    if prev_vol <= 0:
        return result

    vol_ratio = last_vol / prev_vol

    if vol_ratio < 0.70:
        shrink_pct = round((1 - vol_ratio) * 100, 0)
        result["divergence"] = True
        result["shrink_ratio"] = round(vol_ratio, 2)

        if vol_ratio < 0.40:
            result["score_bonus"] = 8
        elif vol_ratio < 0.55:
            result["score_bonus"] = 5
        else:
            result["score_bonus"] = 3

        result["reason"] = f"量价背离：价格新高但成交量缩{shrink_pct:.0f}%（顶部信号）"

    return result


def calculate_signal_score(
    rsi_1d: float,
    rsi_4h: float,
    rsi_4h_peak: float,
    pct_24h: float,
    oi_change: float,
    funding_rate: float,
    yao_score: int,
    trigger_type: str,
    abandon_oi_declining: bool = False,
    btc_24h_pct: float = 0.0,
    cross_validate_bonus: int = 0,
    vol_divergence_bonus: int = 0,
    btc_pump_threshold: float = 8.0,
) -> dict:
    """
    综合信号评分 0~100。

    评分维度：
      1. RSI 强度（0~25）
      2. 妖币特征（0~25）
      3. 触发方式（0~25）
      4. 市场热度（0~25）
      5. 交叉验证加分（0~8）
      6. 量价背离加分（0~8）

    返回:
      {'score': int, 'grade': str, 'details': dict, 'reason': str}
    """
    # 维度1：RSI 强度（0~25）
    rsi_score = min(25, max(0, (rsi_1d - 75) * 1.5))
    drop = rsi_4h_peak - rsi_4h
    rsi_score += min(5, drop * 0.3)
    rsi_score = min(25, rsi_score)

    # 维度2：妖币特征（0~25）
    yao_map = {0: 0, 1: 8, 2: 16, 3: 25}
    yao_dim_score = yao_map.get(yao_score, 0)

    # 维度3：触发方式（0~25）
    if trigger_type == 'abandon':
        trigger_score = 25 if abandon_oi_declining else 20
    else:
        trigger_score = 15

    # 维度4：市场热度（0~25）
    heat_score = 0.0
    if oi_change >= 50:
        heat_score += 10
    elif oi_change >= 30:
        heat_score += 7
    elif oi_change >= 15:
        heat_score += 4

    if funding_rate >= 0.05:
        heat_score += 8
    elif funding_rate >= 0.03:
        heat_score += 5
    elif funding_rate >= 0.01:
        heat_score += 2
    elif funding_rate <= -0.03:
        heat_score -= 5
    elif funding_rate <= -0.01:
        heat_score -= 2

    if btc_24h_pct >= btc_pump_threshold:
        heat_score += 7
    elif btc_24h_pct >= 3:
        heat_score += 4
    elif btc_24h_pct >= 0:
        heat_score += 2

    heat_score = max(0, min(25, heat_score))

    # 总分
    total_score = round(rsi_score + yao_dim_score + trigger_score + heat_score
                        + cross_validate_bonus + vol_divergence_bonus)
    total_score = max(0, min(100, total_score))

    # 评级
    if total_score >= 70:
        grade = "A"
    elif total_score >= 40:
        grade = "B"
    else:
        grade = "SKIP"

    details = {
        "rsi": round(rsi_score, 1),
        "yao": round(yao_dim_score, 1),
        "trigger": round(trigger_score, 1),
        "heat": round(heat_score, 1),
        "cross_validate": cross_validate_bonus,
        "vol_divergence": vol_divergence_bonus,
    }

    reason_parts = []
    if rsi_score >= 18:
        reason_parts.append(f"RSI极强({rsi_1d})")
    if yao_dim_score >= 16:
        reason_parts.append("妖币特征明显")
    if trigger_score >= 20:
        reason_parts.append("弃盘点触发")
    if heat_score >= 15:
        reason_parts.append("市场热度高")
    if cross_validate_bonus > 0:
        reason_parts.append(f"OKX交叉验证(+{cross_validate_bonus})")
    if vol_divergence_bonus > 0:
        reason_parts.append(f"量价背离(+{vol_divergence_bonus})")

    reason = " + ".join(reason_parts) if reason_parts else "信号一般"

    return {
        "score": total_score,
        "grade": grade,
        "details": details,
        "reason": reason,
    }
