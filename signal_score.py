#!/usr/bin/env python3
"""
信号评分 & BTC 趋势过滤 v1.0
功能：
  - 综合评分 0~100：RSI强度 + 妖币特征 + 触发方式 + OI/费率
  - 根据评分决定仓位大小（全仓/半仓/跳过）
  - BTC 24h 暴跌时暂停做空山寨（防止被反弹打止损）

被 altcoin_scanner.py 调用，在触发开仓前执行评分和过滤。
"""

import config
from common import setup_logger
from exchange_manager import get_btc_24h_change_multi

logger = setup_logger("signal_score")


# ══════════════════════════════════════════════════════════════════
#  BTC 趋势过滤
# ══════════════════════════════════════════════════════════════════

def get_btc_24h_change() -> float:
    """获取 BTC/USDT 24h 涨跌幅（%），多源容错（Binance → OKX）"""
    return get_btc_24h_change_multi()


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
    cross_validate_bonus: int = 0,
    vol_divergence_bonus: int = 0,
) -> dict:
    """
    综合评分 0~100。

    评分维度：
      1. RSI 强度（0~25）：日线 RSI 越高、4h 回落越深，信号越强
      2. 妖币特征（0~25）：yao_score 映射
      3. 触发方式（0~25）：弃盘点 > 4h RSI 回落
      4. 市场热度（0~25）：OI 变化 + 资金费率 + BTC 趋势加分
      5. OKX 交叉验证加分（额外 0~8）：两所数据一致时奖励
      6. 量价背离加分（额外 0~8）：价格新高但量缩，顶部确认

    返回:
      {
        "score": 0~100,
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

    # 资金费率（做空视角）：
    #   正费率（多头付空头） → 做空是双重利好（方向+费率收入）→ 加分
    #   负费率（空头付多头） → 做空需要支付持仓利息 → 扣分
    #   费率极高（>FUNDING_MAX）→ 上游 scanner 已直接跳过，这里不处理
    if funding_rate >= 0.05:
        heat_score += 8
    elif funding_rate >= 0.03:
        heat_score += 5
    elif funding_rate >= 0.01:
        heat_score += 2
    elif funding_rate <= -0.03:
        # M9: 极端负费率 → 做空严重不划算（连续 8h 支付利息）→ 明显扣分
        heat_score -= 5
    elif funding_rate <= -0.01:
        # M9: 温和负费率 → 小幅扣分
        heat_score -= 2

    # BTC 趋势加分（0~7）：BTC 涨时山寨冲高回落概率更大
    if btc_24h_pct >= config.BTC_PUMP_THRESHOLD:
        heat_score += 7
    elif btc_24h_pct >= 3:
        heat_score += 4
    elif btc_24h_pct >= 0:
        heat_score += 2

    heat_score = max(0, min(25, heat_score))

    # ── 总分（含 OKX 交叉验证加分 + 量价背离加分）──
    # 设计说明：4 个基础维度各 0~25 分（总和上限 100）。
    # cross_validate_bonus（0~8）和 vol_divergence_bonus（0~8）是"额外奖励"，
    # 用于在多所数据一致或量价背离确认时把原本 B 级信号提升到 A 级。
    # 原始总和可能超过 100（最高 116），但下面 min(100, ...) 确保最终评分
    # 永远在 [0, 100] 范围内。这是有意设计：bonus 的作用是"把接近阈值的
    # 信号推过线"，而非无限放大评分。
    total_score = round(rsi_score + yao_dim_score + trigger_score + heat_score + cross_validate_bonus + vol_divergence_bonus)
    total_score = max(0, min(100, total_score))

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
        f"触发={trigger_score:.0f} 热度={heat_score:.0f}"
        f"{f' OKX=+{cross_validate_bonus}' if cross_validate_bonus > 0 else ''}"
        f"{f' 量价背离=+{vol_divergence_bonus}' if vol_divergence_bonus > 0 else ''}"
        f" | 仓位={stake}U | {reason}"
    )

    return result
