"""
Multi-Factor Composite Scoring Engine
=======================================

Replaces the hardcoded 4×25 signal scoring with an adaptive, IC-weighted
multi-factor model. Integrates:

  - factors.py: 30+ alpha factor computations
  - factor_ic.py: Predictive power measurement
  - factor_orthogonal.py: Redundancy removal

The FactorScorer produces a 0-100 composite score with confidence metrics,
factor attribution, and grade classification (A/B/C/SKIP).

Usage:
    from signals.factor_scorer import FactorScorer, score_signal_multifactor

    # Full engine usage
    scorer = FactorScorer()
    result = scorer.score(ohlcv_df, symbol="TOKENUSDT", external_data=ext)

    # Drop-in function for altcoin_scanner.py
    score_result = score_signal_multifactor(ohlcv_df, symbol, external_data)

Grading for short-selling:
    A    — score >= 75, high confidence, strong short signal
    B    — score >= 55, moderate confidence
    C    — score >= 40, weak signal, reduced position size
    SKIP — score < 40 or low confidence, do not trade

Dependencies: numpy, pandas.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from signals.factors import FactorRegistry
from signals.factor_ic import (
    ICReport,
    compute_ic,
    compute_forward_returns,
    compute_ic_series,
)
from signals.factor_orthogonal import (
    remove_correlated_factors,
    full_orthogonalization_pipeline,
)

logger = logging.getLogger(__name__)



# ══════════════════════════════════════════════════════════════════
#  Data Structures
# ══════════════════════════════════════════════════════════════════

@dataclass
class FactorContribution:
    """Individual factor's contribution to the composite score."""
    factor_name: str
    raw_value: float
    z_score: float
    weight: float
    contribution: float  # weight * z_score component
    ic_value: float


@dataclass
class FactorScoreResult:
    """
    Complete output of the multi-factor scoring engine.

    Attributes:
        score: Composite score 0-100 (higher = stronger short signal).
        grade: Classification — A (strong), B (moderate), C (weak), SKIP.
        confidence: 0-1 reliability measure based on IC consistency.
        top_factors: Top contributing factors with attribution.
        factor_agreement: Fraction of factors agreeing on short direction.
        recommended_stake_multiplier: Position size adjustment (0.5-1.5).
        timestamp: When this score was computed.
        symbol: Symbol this score was computed for.
        n_factors_used: Number of factors in final composite.
    """
    score: float = 50.0
    grade: str = "SKIP"
    confidence: float = 0.0
    top_factors: List[Tuple[str, float, float]] = field(default_factory=list)
    factor_agreement: float = 0.5
    recommended_stake_multiplier: float = 1.0
    timestamp: float = field(default_factory=time.time)
    symbol: str = ""
    n_factors_used: int = 0

    @property
    def is_tradeable(self) -> bool:
        """Whether the signal is strong enough to act on."""
        return self.grade in ("A", "B")

    @property
    def summary(self) -> str:
        """Human-readable summary."""
        top_str = ", ".join(
            f"{name}({ic:.3f})" for name, _, ic in self.top_factors[:3]
        )
        return (
            f"[{self.symbol}] Score={self.score:.1f} Grade={self.grade} "
            f"Conf={self.confidence:.2f} Agree={self.factor_agreement:.0%} "
            f"Stake×{self.recommended_stake_multiplier:.2f} "
            f"Top: {top_str}"
        )



# ══════════════════════════════════════════════════════════════════
#  Adaptive Weighter
# ══════════════════════════════════════════════════════════════════

class AdaptiveWeighter:
    """
    Computes factor weights based on recent IC performance.

    Weights are proportional to |recent_IC| for each factor.
    Factors with IC below the minimum threshold receive zero weight.
    Weights are recalculated every `recalc_interval` hours.
    """

    def __init__(
        self,
        min_ic_threshold: float = 0.02,
        recalc_interval_hours: float = 4.0,
        ic_lookback_bars: int = 168,  # 7 days of hourly bars
        decay_factor: float = 0.94,
    ):
        """
        Args:
            min_ic_threshold: Minimum absolute IC to assign non-zero weight.
            recalc_interval_hours: Hours between weight recalculations.
            ic_lookback_bars: Number of bars for IC estimation.
            decay_factor: Exponential decay for weighting recent IC higher.
        """
        self.min_ic_threshold = min_ic_threshold
        self.recalc_interval_hours = recalc_interval_hours
        self.ic_lookback_bars = ic_lookback_bars
        self.decay_factor = decay_factor

        self._cached_weights: Dict[str, float] = {}
        self._last_recalc: float = 0.0
        self._ic_values: Dict[str, float] = {}

    @property
    def weights(self) -> Dict[str, float]:
        """Current factor weights (read-only)."""
        return dict(self._cached_weights)

    @property
    def ic_values(self) -> Dict[str, float]:
        """Most recent IC values per factor."""
        return dict(self._ic_values)

    def needs_recalculation(self) -> bool:
        """Check if weights should be recalculated."""
        elapsed_hours = (time.time() - self._last_recalc) / 3600.0
        return elapsed_hours >= self.recalc_interval_hours

    def calculate_weights(
        self,
        factor_df: pd.DataFrame,
        prices: pd.Series,
        horizon: int = 4,
    ) -> Dict[str, float]:
        """
        Calculate adaptive weights based on recent IC.

        Args:
            factor_df: Recent factor values (should have at least ic_lookback_bars rows).
            prices: Close prices aligned to factor_df.
            horizon: Forward return horizon for IC calculation.

        Returns:
            Dict of factor_name -> weight (normalized to sum to 1.0).
        """
        fwd_returns = compute_forward_returns(prices, horizon=horizon)
        lookback = min(self.ic_lookback_bars, len(factor_df))

        recent_factors = factor_df.iloc[-lookback:]
        recent_returns = fwd_returns.iloc[-lookback:]

        ic_values: Dict[str, float] = {}
        for col in factor_df.columns:
            ic = compute_ic(recent_factors[col], recent_returns)
            ic_values[col] = ic

        self._ic_values = ic_values

        # Weights = normalized |IC| above threshold
        raw_weights: Dict[str, float] = {}
        for name, ic in ic_values.items():
            abs_ic = abs(ic)
            if abs_ic >= self.min_ic_threshold:
                raw_weights[name] = abs_ic
            else:
                raw_weights[name] = 0.0

        # Normalize
        total = sum(raw_weights.values())
        if total > 0:
            weights = {k: v / total for k, v in raw_weights.items()}
        else:
            # Fallback: equal weights for all factors
            n = len(factor_df.columns)
            weights = {col: 1.0 / n for col in factor_df.columns}

        self._cached_weights = weights
        self._last_recalc = time.time()

        active_count = sum(1 for w in weights.values() if w > 0)
        logger.info(
            "Adaptive weights recalculated: %d/%d factors active",
            active_count, len(weights),
        )

        return weights



# ══════════════════════════════════════════════════════════════════
#  Main Scoring Engine
# ══════════════════════════════════════════════════════════════════

class FactorScorer:
    """
    Multi-factor composite scoring engine for short-selling signals.

    Integrates factor computation, IC-based weighting, orthogonalization,
    and produces a tradeable score with confidence metrics.
    """

    # Grade boundaries (score thresholds for short signals)
    GRADE_A_THRESHOLD = 75.0
    GRADE_B_THRESHOLD = 55.0
    GRADE_C_THRESHOLD = 40.0

    def __init__(
        self,
        normalize: bool = True,
        max_factor_correlation: float = 0.7,
        min_ic_threshold: float = 0.02,
        recalc_hours: float = 4.0,
        ic_lookback: int = 168,
        confidence_ic_ir_threshold: float = 0.5,
    ):
        """
        Args:
            normalize: Z-score normalize factors.
            max_factor_correlation: Correlation cutoff for orthogonalization.
            min_ic_threshold: Minimum IC for factor inclusion.
            recalc_hours: Weight recalculation interval.
            ic_lookback: Bars for IC estimation.
            confidence_ic_ir_threshold: IC_IR above this = high confidence.
        """
        self.registry = FactorRegistry(normalize=normalize)
        self.weighter = AdaptiveWeighter(
            min_ic_threshold=min_ic_threshold,
            recalc_interval_hours=recalc_hours,
            ic_lookback_bars=ic_lookback,
        )
        self.max_factor_correlation = max_factor_correlation
        self.confidence_ic_ir_threshold = confidence_ic_ir_threshold

        # Cache
        self._last_ortho_info = None

    def score(
        self,
        df: pd.DataFrame,
        symbol: str = "",
        external_data: Optional[Dict[str, pd.Series]] = None,
    ) -> FactorScoreResult:
        """
        Compute multi-factor composite score for a given OHLCV dataset.

        Args:
            df: OHLCV DataFrame [timestamp, open, high, low, close, volume].
            symbol: Trading symbol (for logging/attribution).
            external_data: Optional external data (funding rate, OI, etc.).

        Returns:
            FactorScoreResult with score, grade, confidence, and attribution.
        """
        if df.empty or len(df) < 30:
            logger.warning("[%s] Insufficient data for scoring (n=%d)", symbol, len(df))
            return FactorScoreResult(symbol=symbol)

        try:
            # Step 1: Compute all factors
            factor_df = self.registry.compute_all(df, external_data=external_data)

            if factor_df.empty:
                return FactorScoreResult(symbol=symbol)

            # Step 2: Orthogonalize (correlation filtering)
            ic_vals = self.weighter.ic_values if self.weighter.ic_values else None
            filtered_df, ortho_info = remove_correlated_factors(
                factor_df,
                max_corr=self.max_factor_correlation,
                ic_values=ic_vals,
            )
            self._last_ortho_info = ortho_info

            # Step 3: Compute/update weights
            prices = df["close"]
            if self.weighter.needs_recalculation():
                weights = self.weighter.calculate_weights(
                    filtered_df, prices, horizon=4
                )
            else:
                weights = self.weighter.weights
                # Ensure weights cover current factors
                if not weights or not set(weights.keys()).intersection(filtered_df.columns):
                    weights = self.weighter.calculate_weights(
                        filtered_df, prices, horizon=4
                    )

            # Step 4: Compute weighted composite
            result = self._compute_composite(
                filtered_df, weights, prices, symbol
            )

            return result

        except Exception as e:
            logger.error("[%s] Scoring failed: %s", symbol, e, exc_info=True)
            return FactorScoreResult(symbol=symbol)


    def _compute_composite(
        self,
        factor_df: pd.DataFrame,
        weights: Dict[str, float],
        prices: pd.Series,
        symbol: str,
    ) -> FactorScoreResult:
        """
        Compute the final composite score from weighted factors.

        For short-selling: higher factor z-scores (indicating overbought,
        overextension, etc.) map to higher composite scores.
        """
        # Use the most recent bar's factor values
        latest_idx = factor_df.index[-1]
        latest_values = factor_df.loc[latest_idx]

        # Compute weighted sum
        weighted_sum = 0.0
        total_weight = 0.0
        contributions: List[FactorContribution] = []

        for col in factor_df.columns:
            w = weights.get(col, 0.0)
            z = latest_values.get(col, 0.0)

            if pd.isna(z):
                z = 0.0

            contrib = w * z
            weighted_sum += contrib
            total_weight += w

            ic_val = self.weighter.ic_values.get(col, 0.0)
            contributions.append(FactorContribution(
                factor_name=col,
                raw_value=z,
                z_score=z,
                weight=w,
                contribution=contrib,
                ic_value=ic_val,
            ))

        # Normalize weighted sum to 0-100 scale
        # Z-scores typically range -3 to +3; map to 0-100
        # For shorts: positive z-score = overbought = strong signal
        raw_composite = weighted_sum if total_weight > 0 else 0.0

        # Sigmoid-like mapping: z-score composite -> 0-100
        score = self._zscore_to_score(raw_composite)

        # Factor agreement: what fraction of weighted factors agree on direction
        agreement = self._compute_agreement(factor_df, weights)

        # Confidence: based on IC reliability and agreement
        confidence = self._compute_confidence(weights, agreement)

        # Grade
        grade = self._assign_grade(score, confidence)

        # Stake multiplier
        stake_mult = self._compute_stake_multiplier(score, confidence, agreement)

        # Top factors by contribution
        contributions.sort(key=lambda c: abs(c.contribution), reverse=True)
        top_factors = [
            (c.factor_name, c.contribution, c.ic_value)
            for c in contributions[:5]
        ]

        n_active = sum(1 for c in contributions if c.weight > 0)

        result = FactorScoreResult(
            score=score,
            grade=grade,
            confidence=confidence,
            top_factors=top_factors,
            factor_agreement=agreement,
            recommended_stake_multiplier=stake_mult,
            symbol=symbol,
            n_factors_used=n_active,
        )

        logger.info("[%s] %s", symbol, result.summary)
        return result


    @staticmethod
    def _zscore_to_score(z: float) -> float:
        """
        Convert composite z-score to 0-100 scale using sigmoid mapping.

        Maps:
            z = -3 -> score ~5
            z =  0 -> score 50
            z = +3 -> score ~95
        """
        # Logistic sigmoid scaled to 0-100
        score = 100.0 / (1.0 + np.exp(-1.5 * z))
        return float(np.clip(score, 0.0, 100.0))

    @staticmethod
    def _compute_agreement(
        factor_df: pd.DataFrame,
        weights: Dict[str, float],
    ) -> float:
        """
        Compute factor agreement — fraction of weighted factors with same sign.

        For short signals, positive z-scores indicate agreement.
        """
        latest = factor_df.iloc[-1]
        positive_weight = 0.0
        total_weight = 0.0

        for col in factor_df.columns:
            w = weights.get(col, 0.0)
            if w <= 0:
                continue
            val = latest.get(col, 0.0)
            if pd.isna(val):
                continue
            total_weight += w
            if val > 0:  # Agrees with short direction (overbought)
                positive_weight += w

        if total_weight == 0:
            return 0.5

        return positive_weight / total_weight

    def _compute_confidence(
        self,
        weights: Dict[str, float],
        agreement: float,
    ) -> float:
        """
        Compute confidence score (0-1) based on IC reliability and agreement.

        High confidence requires:
          - Multiple factors with strong IC
          - High factor agreement
        """
        ic_vals = self.weighter.ic_values
        if not ic_vals:
            return 0.3

        # Average absolute IC of active factors
        active_ics = [abs(ic_vals[k]) for k, w in weights.items() if w > 0 and k in ic_vals]
        if not active_ics:
            return 0.3

        avg_ic = np.mean(active_ics)
        n_active = len(active_ics)

        # IC quality component (0-0.5)
        ic_component = min(avg_ic / 0.10, 1.0) * 0.4

        # Agreement component (0-0.3)
        agreement_component = agreement * 0.3

        # Breadth component — more active factors = more confidence (0-0.3)
        breadth = min(n_active / 10.0, 1.0) * 0.3

        confidence = ic_component + agreement_component + breadth
        return float(np.clip(confidence, 0.0, 1.0))

    def _assign_grade(self, score: float, confidence: float) -> str:
        """Assign letter grade based on score and confidence."""
        if confidence < 0.3:
            return "SKIP"
        if score >= self.GRADE_A_THRESHOLD and confidence >= 0.5:
            return "A"
        elif score >= self.GRADE_B_THRESHOLD and confidence >= 0.4:
            return "B"
        elif score >= self.GRADE_C_THRESHOLD:
            return "C"
        else:
            return "SKIP"

    @staticmethod
    def _compute_stake_multiplier(
        score: float,
        confidence: float,
        agreement: float,
    ) -> float:
        """
        Compute recommended stake multiplier (0.5-1.5).

        Higher score + confidence + agreement = larger position.
        """
        # Base from score
        score_factor = (score - 50.0) / 50.0  # -1 to +1

        # Combined
        multiplier = 1.0 + 0.3 * score_factor + 0.2 * (confidence - 0.5)

        # Agreement bonus
        if agreement > 0.75:
            multiplier += 0.1
        elif agreement < 0.4:
            multiplier -= 0.2

        return float(np.clip(multiplier, 0.5, 1.5))



# ══════════════════════════════════════════════════════════════════
#  Drop-in Enhancement Function
# ══════════════════════════════════════════════════════════════════

# Module-level singleton scorer (lazy initialization)
_global_scorer: Optional[FactorScorer] = None


def _get_scorer() -> FactorScorer:
    """Get or create the global FactorScorer instance."""
    global _global_scorer
    if _global_scorer is None:
        _global_scorer = FactorScorer()
    return _global_scorer


def score_signal_multifactor(
    df: pd.DataFrame,
    symbol: str = "",
    external_data: Optional[Dict[str, pd.Series]] = None,
    legacy_score: Optional[float] = None,
    blend_ratio: float = 0.0,
) -> FactorScoreResult:
    """
    Drop-in multi-factor scoring function for altcoin_scanner.py.

    Can be called alongside the existing calculate_signal_score() function.
    Optionally blends with the legacy score for gradual migration.

    Args:
        df: OHLCV DataFrame [timestamp, open, high, low, close, volume].
        symbol: Trading symbol identifier.
        external_data: Optional external data dict with keys like:
            - 'funding_rate': pd.Series
            - 'open_interest': pd.Series
            - 'liquidation_volume': pd.Series
            - 'large_buy_volume': pd.Series
            - 'large_sell_volume': pd.Series
        legacy_score: Optional legacy score (0-100) from calculate_signal_score().
        blend_ratio: How much to blend with legacy (0.0 = pure multifactor,
                     1.0 = pure legacy). Useful for gradual rollout.

    Returns:
        FactorScoreResult with all metrics.

    Example usage in altcoin_scanner.py:
        from signals.factor_scorer import score_signal_multifactor

        # Get OHLCV data
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=200)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])

        # New multi-factor score
        result = score_signal_multifactor(df, symbol=symbol)

        if result.grade in ('A', 'B'):
            # Proceed with trade, use result.recommended_stake_multiplier
            ...
    """
    scorer = _get_scorer()
    result = scorer.score(df, symbol=symbol, external_data=external_data)

    # Optional blending with legacy score
    if legacy_score is not None and blend_ratio > 0.0:
        blended_score = (
            (1.0 - blend_ratio) * result.score +
            blend_ratio * legacy_score
        )
        blended_score = float(np.clip(blended_score, 0.0, 100.0))

        # Re-grade with blended score
        result = FactorScoreResult(
            score=blended_score,
            grade=scorer._assign_grade(blended_score, result.confidence),
            confidence=result.confidence,
            top_factors=result.top_factors,
            factor_agreement=result.factor_agreement,
            recommended_stake_multiplier=scorer._compute_stake_multiplier(
                blended_score, result.confidence, result.factor_agreement
            ),
            symbol=result.symbol,
            n_factors_used=result.n_factors_used,
        )

    return result


def get_factor_diagnostics(
    df: pd.DataFrame,
    symbol: str = "",
    external_data: Optional[Dict[str, pd.Series]] = None,
) -> Dict:
    """
    Get detailed factor diagnostics for debugging and analysis.

    Returns a dict with:
        - factor_values: Latest values for all factors
        - ic_values: Recent IC for each factor
        - weights: Current adaptive weights
        - correlation_matrix: Factor correlation matrix
        - ortho_info: Orthogonalization summary
    """
    scorer = _get_scorer()
    registry = scorer.registry

    factor_df = registry.compute_all(df, external_data=external_data)
    prices = df["close"]

    # Compute IC for each factor
    fwd_ret = compute_forward_returns(prices, horizon=4)
    ic_values = {}
    for col in factor_df.columns:
        ic_values[col] = compute_ic(factor_df[col], fwd_ret)

    # Correlation matrix
    corr_matrix = factor_df.dropna().corr()

    # Latest values
    latest_values = factor_df.iloc[-1].to_dict() if not factor_df.empty else {}

    return {
        "symbol": symbol,
        "factor_values": latest_values,
        "ic_values": ic_values,
        "weights": scorer.weighter.weights,
        "correlation_matrix": corr_matrix,
        "n_factors": len(factor_df.columns),
        "categories": registry.categories,
    }
