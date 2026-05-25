#!/usr/bin/env python3
"""
ML 评分服务 — 替代线性 signal_score.calculate_signal_score()

本模块是 signal_score.py 的 ML 增强版本：
  - 有训练好的模型时 → 用 XGBoost 预测盈利概率
  - 模型不可用时 → 自动 fallback 到原始线性评分（零停机）

集成方式：
  在 altcoin_scanner.py / engine_adapter.py 中：
    from ml.scorer import ml_signal_score
    result = ml_signal_score(rsi_1d=85, pct_24h=20, ...)
    if result['grade'] != 'SKIP':
        open_position(stake=result['stake'])

向后兼容：
  返回格式与原 signal_score.calculate_signal_score() 完全一致：
  {
    "score": 0~100,
    "grade": "A" / "B" / "SKIP",
    "stake": USDT,
    "details": {...},
    "reason": str,
  }
"""

from __future__ import annotations

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("ml.scorer")


def ml_signal_score(
    rsi_1d: float = 50.0,
    rsi_4h: float = 50.0,
    rsi_4h_peak: float = 70.0,
    pct_24h: float = 0.0,
    vol_24h: float = 0.0,
    vol_7d_avg: float = 1.0,
    oi_change: float = 0.0,
    funding_rate: float = 0.0,
    yao_score: int = 0,
    trigger_type: str = '',
    btc_24h_pct: float = 0.0,
    orderbook_imbalance: float = 0.0,
    whale_bonus: int = 0,
    sentiment_bonus: int = 0,
    regime_state: int = 2,
    default_stake: float = 30.0,
    use_ml: bool = True,
) -> Dict[str, Any]:
    """
    ML 增强信号评分。

    当模型可用时，使用 XGBoost 预测盈利概率作为评分基础；
    模型不可用时，自动 fallback 到原始线性评分。

    返回格式与 signal_score.calculate_signal_score() 完全一致。
    """
    import config

    # ── 尝试 ML 路径 ──
    if use_ml:
        try:
            from ml.features import extract_features
            from ml.model import get_signal_model

            model = get_signal_model()

            features = extract_features(
                rsi_1d=rsi_1d,
                rsi_4h=rsi_4h,
                rsi_4h_peak=rsi_4h_peak,
                pct_24h=pct_24h,
                vol_24h=vol_24h,
                vol_7d_avg=vol_7d_avg,
                oi_change=oi_change,
                funding_rate=funding_rate,
                yao_score=yao_score,
                btc_24h_pct=btc_24h_pct,
                orderbook_imbalance=orderbook_imbalance,
                whale_inflow_score=whale_bonus / 15.0 if whale_bonus else 0,
                sentiment_score=sentiment_bonus / 10.0 if sentiment_bonus else 0,
                regime_state=regime_state,
            )

            prediction = model.predict(features.to_array())

            # 将盈利概率转换为 0~100 评分
            score = int(prediction.profit_probability * 100)
            score = max(0, min(100, score))

            # 评级
            if prediction.confidence == 'high':
                grade = 'A'
                stake = default_stake
            elif prediction.confidence == 'medium':
                grade = 'B'
                stake = round(default_stake * 0.5)
            else:
                grade = 'SKIP'
                stake = 0

            # 构建详情
            details = {
                'ml_probability': prediction.profit_probability,
                'ml_confidence': prediction.confidence,
                'ml_version': prediction.model_version,
                'features': features.to_dict(),
            }
            if prediction.feature_importance_top3:
                details['top_factors'] = prediction.feature_importance_top3

            reason_parts = [f"ML盈利概率={prediction.profit_probability:.0%}"]
            if prediction.feature_importance_top3:
                top_factor = prediction.feature_importance_top3[0][0]
                reason_parts.append(f"主因={top_factor}")
            reason = ' | '.join(reason_parts)

            logger.info(
                f"  🤖 ML评分={score} [{grade}] | "
                f"P(profit)={prediction.profit_probability:.1%} | "
                f"model={prediction.model_version} | stake={stake}U"
            )

            return {
                'score': score,
                'grade': grade,
                'stake': stake,
                'details': details,
                'reason': reason,
                'source': 'ml',
            }

        except Exception as e:
            logger.debug(f"ML 评分异常，fallback 到线性: {e}")

    # ── Fallback：原始线性评分 ──
    from signal_score import calculate_signal_score

    result = calculate_signal_score(
        rsi_1d=rsi_1d,
        rsi_4h=rsi_4h,
        rsi_4h_peak=rsi_4h_peak,
        pct_24h=pct_24h,
        oi_change=oi_change * 100,  # signal_score 用百分比格式
        funding_rate=funding_rate,
        yao_score=yao_score,
        trigger_type=trigger_type,
        btc_24h_pct=btc_24h_pct,
        cross_validate_bonus=0,
        vol_divergence_bonus=0,
    )
    result['source'] = 'linear_fallback'
    return result


def get_ml_scorer_status() -> Dict[str, Any]:
    """获取 ML 评分器状态（Dashboard 展示用）"""
    try:
        from ml.model import get_signal_model
        model = get_signal_model()
        return {
            'available': model.is_loaded,
            'model_version': model._model_version if model.is_loaded else 'none',
            'mode': 'ml' if model.is_loaded else 'linear_fallback',
        }
    except Exception:
        return {'available': False, 'model_version': 'none', 'mode': 'linear_fallback'}
