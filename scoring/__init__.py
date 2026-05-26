#!/usr/bin/env python3
"""
统一评分入口（S3 修复 — 2026-05）

历史背景
========
在 v5.x 之前，本仓库存在三套并存的信号评分实现：

  1. ``signal_score.calculate_signal_score()``        — 硬编码 4×25 + bonus（最早版本）
  2. ``signals.factor_scorer.score_signal_multifactor()`` — IC 加权多因子（2026-Q2）
  3. ``ml.scorer.ml_signal_score()``                   — XGBoost 概率（实验阶段）

altcoin_scanner.py 用 (2) 主路径 + (1) fallback；ml/scorer.py 用 (3) 主路径 +
(1) fallback；测试与 backtest 直接调 (1)。三者之间没有统一调度，导致：

  * 改 grade 阈值要在三处同时改，容易漏
  * Scanner 要写 try/except 双层 fallback，可读性差
  * 新策略想接入只能"再写一份"，scorer 数量随时间继续膨胀

S3 修复
=======
本包提供 **唯一的对外评分入口** ``score_signal()``。内部按以下优先级
自动选择实现，调用方完全无感：

  ml > multifactor > linear

参数：
  - 始终接受最宽的特征集（OHLCV + RSI + 链上 + 情绪）
  - 内部按需投影到具体 scorer 的输入字段
  - 单一返回结构 :class:`ScoreResult`

向后兼容：
  - 旧的三个底层函数依旧可用，未做删除（test_audit_fixes、test_fixes_hm、
    ml/scorer 等仍然引用），只是不再是 "入口"。
  - 新代码（scanner / backtest / engine_adapter）应只 import 本包。

详细文档见 ``docs/UNIFIED_ARCHITECTURE.md``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("scoring")


@dataclass
class ScoreResult:
    """统一评分结果。等价于旧 dict 形态 + 新增 ``source`` 字段。"""
    score: int = 0
    grade: str = "SKIP"
    stake: float = 0
    details: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    source: str = "linear"   # 'linear' | 'multifactor' | 'ml'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'score': self.score,
            'grade': self.grade,
            'stake': self.stake,
            'details': self.details,
            'reason': self.reason,
            'source': self.source,
        }


def _from_dict(d: Dict[str, Any], source: str) -> ScoreResult:
    return ScoreResult(
        score=int(d.get('score', 0)),
        grade=str(d.get('grade', 'SKIP')),
        stake=float(d.get('stake', 0)),
        details=dict(d.get('details', {})),
        reason=str(d.get('reason', '')),
        source=source,
    )


def score_signal(
    *,
    # ── 通用必选 ──
    rsi_1d: float,
    rsi_4h: float,
    rsi_4h_peak: float,
    pct_24h: float,
    oi_change: float,        # 百分比（30.0 表示 +30%）— 与 calculate_signal_score 单位一致
    funding_rate: float,     # %/8h
    yao_score: int,
    trigger_type: str,       # 'abandon' | '4h_rsi'
    # ── 可选 ──
    abandon_oi_declining: bool = False,
    btc_24h_pct: float = 0.0,
    cross_validate_bonus: int = 0,
    vol_divergence_bonus: int = 0,
    whale_bonus: int = 0,
    sentiment_bonus: int = 0,
    # ── 多因子 / ML 用 ──
    ohlcv_df: Optional[Any] = None,   # pd.DataFrame，列 [timestamp,open,high,low,close,volume]
    symbol: str = "",
    direction: str = "SHORT",
    # ── 调度参数 ──
    prefer: str = "auto",    # 'auto' | 'ml' | 'multifactor' | 'linear'
) -> ScoreResult:
    """
    统一信号评分入口。按 ml > multifactor > linear 优先级自动 fallback。

    ``prefer='auto'`` 时遵循 config 中的 ``SCORING_BACKEND`` 偏好（默认 auto）。
    任何评分实现失败都会自动降级到下一档，永远返回有效 ``ScoreResult``。
    """
    import config

    backend_pref = prefer
    if backend_pref == "auto":
        backend_pref = str(getattr(config, 'SCORING_BACKEND', 'auto')).lower()

    tried: list[str] = []

    # ── 1. ML（如果偏好 auto/ml 且模型可用） ──
    if backend_pref in ('auto', 'ml'):
        try:
            from ml.scorer import ml_signal_score
            tried.append('ml')
            d = ml_signal_score(
                rsi_1d=rsi_1d,
                rsi_4h=rsi_4h,
                rsi_4h_peak=rsi_4h_peak,
                pct_24h=pct_24h,
                oi_change=oi_change / 100.0,   # ml 期望小数比例
                funding_rate=funding_rate,
                yao_score=yao_score,
                trigger_type=trigger_type,
                btc_24h_pct=btc_24h_pct,
                whale_bonus=whale_bonus,
                sentiment_bonus=sentiment_bonus,
                default_stake=float(getattr(config, 'DEFAULT_STAKE', 30)),
                use_ml=True,
            )
            # 当 ml.scorer 内部 fallback 到线性时，source 会是 'linear_fallback'。
            # 保留信息但归为 linear 路径，避免误以为模型在跑。
            if d.get('source') == 'ml':
                return _from_dict(d, 'ml')
        except Exception as e:
            logger.debug(f"score_signal: ml 路径失败 → 继续 fallback: {e}")

    # ── 2. Multi-factor（如果有 OHLCV df 可用） ──
    if backend_pref in ('auto', 'multifactor') and ohlcv_df is not None:
        try:
            from signals.factor_scorer import score_signal_multifactor
            tried.append('multifactor')
            mf = score_signal_multifactor(ohlcv_df, symbol=symbol, direction=direction)
            score = round(mf.score)
            # bonus 与旧 scanner 行为一致：whale + sentiment 加到总分上
            score = max(0, min(100, score + whale_bonus + sentiment_bonus))
            full_th = float(getattr(config, 'SCORE_FULL_THRESHOLD', 70))
            half_th = float(getattr(config, 'SCORE_HALF_THRESHOLD', 50))
            base_stake = float(getattr(config, 'DEFAULT_STAKE', 30))
            if score >= full_th:
                grade, stake = 'A', base_stake
            elif score >= half_th:
                grade, stake = 'B', round(base_stake * 0.5)
            else:
                grade, stake = 'SKIP', 0
            top = mf.top_factors[0][0] if mf.top_factors else 'N/A'
            return ScoreResult(
                score=score,
                grade=grade,
                stake=stake,
                details={
                    'multifactor': True,
                    'confidence': mf.confidence,
                    'agreement': mf.factor_agreement,
                    'n_factors': mf.n_factors_used,
                    'whale': whale_bonus,
                    'sentiment': sentiment_bonus,
                },
                reason=f"MF score={mf.score:.0f} conf={mf.confidence:.2f} top={top}",
                source='multifactor',
            )
        except Exception as e:
            logger.debug(f"score_signal: multifactor 路径失败 → 继续 fallback: {e}")

    # ── 3. Linear（最后兜底，永不抛错） ──
    try:
        from signal_score import calculate_signal_score
        tried.append('linear')
        d = calculate_signal_score(
            rsi_1d=rsi_1d,
            rsi_4h=rsi_4h,
            rsi_4h_peak=rsi_4h_peak,
            pct_24h=pct_24h,
            oi_change=oi_change,
            funding_rate=funding_rate,
            yao_score=yao_score,
            trigger_type=trigger_type,
            abandon_oi_declining=abandon_oi_declining,
            btc_24h_pct=btc_24h_pct,
            cross_validate_bonus=cross_validate_bonus,
            vol_divergence_bonus=vol_divergence_bonus,
            whale_bonus=whale_bonus,
            sentiment_bonus=sentiment_bonus,
        )
        return _from_dict(d, 'linear')
    except Exception as e:
        logger.error(f"score_signal: 全部路径失败 (尝试={tried}): {e}", exc_info=True)
        return ScoreResult(
            score=0, grade='SKIP', stake=0,
            details={'error': str(e), 'tried': tried},
            reason='all backends failed',
            source='error',
        )


__all__ = ['ScoreResult', 'score_signal']
