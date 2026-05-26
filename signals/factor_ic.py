"""
Information Coefficient (IC) Analysis Module
=============================================

Measures the predictive power of each factor for future returns using
rank-based correlation (Spearman IC). Provides:

  - Single-point IC computation
  - Rolling IC time series
  - IC decay analysis across forward horizons
  - IC stability tracking over time

Usage:
    from signals.factor_ic import compute_ic, compute_ic_series, ICReport

    ic = compute_ic(factor_values, forward_returns)
    ic_df = compute_ic_series(factor_df, returns_series, window=60)
    report = ICReport.from_data(factor_values, prices)

For the altcoin short-selling system, a factor with mean IC > 0.03
and IC_IR > 0.5 is considered useful for inclusion in the composite score.

Dependencies: numpy, pandas (no sklearn).
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)



# ══════════════════════════════════════════════════════════════════
#  Core IC Computation
# ══════════════════════════════════════════════════════════════════

def _rank_series(s: pd.Series) -> pd.Series:
    """Rank a series handling NaN values (used for Spearman correlation)."""
    return s.rank(method="average", na_option="keep")


def compute_ic(factor_values: pd.Series, forward_returns: pd.Series) -> float:
    """
    Compute rank IC (Spearman correlation) between factor values and forward returns.

    Args:
        factor_values: Factor signal values aligned to bar timestamps.
        forward_returns: Future returns over some horizon, aligned to same index.

    Returns:
        Spearman rank correlation coefficient (-1 to 1).
        Returns 0.0 if insufficient data.
    """
    # Align and drop NaN
    aligned = pd.DataFrame({
        "factor": factor_values,
        "returns": forward_returns,
    }).dropna()

    if len(aligned) < 20:
        logger.debug("Insufficient data for IC computation (n=%d)", len(aligned))
        return 0.0

    factor_rank = _rank_series(aligned["factor"])
    return_rank = _rank_series(aligned["returns"])

    # Spearman correlation = Pearson of ranks
    n = len(factor_rank)
    mean_f = factor_rank.mean()
    mean_r = return_rank.mean()

    cov = ((factor_rank - mean_f) * (return_rank - mean_r)).sum()
    std_f = np.sqrt(((factor_rank - mean_f) ** 2).sum())
    std_r = np.sqrt(((return_rank - mean_r) ** 2).sum())

    if std_f == 0 or std_r == 0:
        return 0.0

    ic = cov / (std_f * std_r)
    return float(np.clip(ic, -1.0, 1.0))



def compute_ic_series(
    factor_df: pd.DataFrame,
    returns: pd.Series,
    window: int = 60,
) -> pd.DataFrame:
    """
    Compute rolling IC for each factor over a sliding window.

    Args:
        factor_df: DataFrame with one column per factor.
        returns: Forward returns series aligned to factor_df index.
        window: Rolling window size (number of bars).

    Returns:
        DataFrame with same columns as factor_df, values = rolling IC.
    """
    ic_results: Dict[str, pd.Series] = {}

    for col in factor_df.columns:
        ic_values = []
        factor_col = factor_df[col]

        for i in range(len(factor_df)):
            if i < window - 1:
                ic_values.append(np.nan)
                continue

            start_idx = i - window + 1
            window_factor = factor_col.iloc[start_idx:i + 1]
            window_returns = returns.iloc[start_idx:i + 1]
            ic = compute_ic(window_factor, window_returns)
            ic_values.append(ic)

        ic_results[col] = pd.Series(ic_values, index=factor_df.index)

    return pd.DataFrame(ic_results)


def compute_forward_returns(
    prices: pd.Series,
    horizon: int = 1,
) -> pd.Series:
    """
    Compute forward returns at a given horizon.

    Args:
        prices: Close price series.
        horizon: Number of bars forward.

    Returns:
        Series of forward returns (negative shift so aligned to signal time).
    """
    future_price = prices.shift(-horizon)
    returns = (future_price - prices) / prices.replace(0, np.nan)
    return returns



def compute_ic_decay(
    factor_values: pd.Series,
    prices: pd.Series,
    horizons: List[int] = None,
) -> Dict[int, float]:
    """
    Compute IC at multiple forward horizons to analyze signal decay.

    Args:
        factor_values: Factor signal values.
        prices: Close price series (same index as factor_values).
        horizons: List of forward horizons in bars (default: [1, 4, 8, 24] hours).

    Returns:
        Dict mapping horizon -> IC value.
    """
    if horizons is None:
        horizons = [1, 4, 8, 24]

    decay_profile: Dict[int, float] = {}

    for h in horizons:
        fwd_ret = compute_forward_returns(prices, horizon=h)
        ic = compute_ic(factor_values, fwd_ret)
        decay_profile[h] = ic

    return decay_profile


def compute_ic_history(
    factor_values: pd.Series,
    prices: pd.Series,
    period_days: int = 30,
    horizon: int = 4,
) -> List[float]:
    """
    Compute IC by calendar period to detect factor decay over time.

    Args:
        factor_values: Factor signal values.
        prices: Close price series.
        period_days: Length of each evaluation period in days.
        horizon: Forward return horizon in bars.

    Returns:
        List of IC values, one per period (oldest first).
    """
    fwd_ret = compute_forward_returns(prices, horizon=horizon)
    total_bars = len(factor_values)

    # Assume hourly bars unless timestamps available
    bars_per_period = period_days * 24
    ic_by_period: List[float] = []

    start = 0
    while start + bars_per_period <= total_bars:
        end = start + bars_per_period
        period_factor = factor_values.iloc[start:end]
        period_returns = fwd_ret.iloc[start:end]
        ic = compute_ic(period_factor, period_returns)
        ic_by_period.append(ic)
        start = end

    return ic_by_period



def _estimate_decay_half_life(ic_decay: Dict[int, float]) -> float:
    """
    Estimate the half-life of IC decay from the decay profile.

    Returns:
        Estimated half-life in bars. Returns inf if IC is not decaying.
    """
    if not ic_decay:
        return float("inf")

    horizons = sorted(ic_decay.keys())
    ics = [abs(ic_decay[h]) for h in horizons]

    if len(ics) < 2 or ics[0] <= 0:
        return float("inf")

    # Find where IC drops below half of peak
    peak_ic = ics[0]
    half_target = peak_ic / 2.0

    for i in range(1, len(ics)):
        if ics[i] <= half_target:
            # Linear interpolation between adjacent horizons
            if ics[i - 1] == ics[i]:
                return float(horizons[i])
            ratio = (ics[i - 1] - half_target) / (ics[i - 1] - ics[i])
            half_life = horizons[i - 1] + ratio * (horizons[i] - horizons[i - 1])
            return float(half_life)

    # IC hasn't decayed below half — long half life
    return float(horizons[-1]) * 2.0


def _detect_ic_trend(ic_history: List[float], threshold: float = -0.02) -> bool:
    """
    Detect if a factor's IC is decaying over time.

    Args:
        ic_history: List of IC values by period.
        threshold: Slope threshold below which we consider it decaying.

    Returns:
        True if factor IC is declining over time.
    """
    if len(ic_history) < 3:
        return False

    x = np.arange(len(ic_history), dtype=float)
    y = np.array(ic_history, dtype=float)

    # Remove NaN
    mask = ~np.isnan(y)
    if mask.sum() < 3:
        return False

    slope = np.polyfit(x[mask], y[mask], 1)[0]
    return slope < threshold



# ══════════════════════════════════════════════════════════════════
#  ICReport Dataclass
# ══════════════════════════════════════════════════════════════════

@dataclass
class ICReport:
    """
    Summary report of a factor's predictive power.

    Attributes:
        factor_name: Name of the factor.
        mean_ic: Average IC across all observations.
        ic_std: Standard deviation of rolling IC values.
        ic_ir: Information Ratio = mean_ic / ic_std (consistency measure).
        decay_half_life: Estimated bars until IC halves.
        best_horizon: Forward horizon with highest absolute IC.
        is_decaying: Whether the factor's IC is declining over time.
        ic_by_horizon: IC at each tested horizon.
        ic_history: IC values by calendar period.
    """
    factor_name: str = ""
    mean_ic: float = 0.0
    ic_std: float = 0.0
    ic_ir: float = 0.0
    decay_half_life: float = float("inf")
    best_horizon: int = 1
    is_decaying: bool = False
    ic_by_horizon: Dict[int, float] = field(default_factory=dict)
    ic_history: List[float] = field(default_factory=list)

    @classmethod
    def from_data(
        cls,
        factor_values: pd.Series,
        prices: pd.Series,
        factor_name: str = "unknown",
        horizons: Optional[List[int]] = None,
        period_days: int = 30,
    ) -> "ICReport":
        """
        Build a complete IC report from raw data.

        Args:
            factor_values: Factor signal series.
            prices: Close price series (same index).
            factor_name: Identifier for this factor.
            horizons: Forward horizons to test (default [1, 4, 8, 24]).
            period_days: Calendar period for IC history.

        Returns:
            ICReport with all metrics computed.
        """
        if horizons is None:
            horizons = [1, 4, 8, 24]

        # IC decay profile across horizons
        ic_decay = compute_ic_decay(factor_values, prices, horizons=horizons)

        # Best horizon
        best_h = max(ic_decay, key=lambda h: abs(ic_decay[h])) if ic_decay else 1

        # Compute rolling IC at best horizon for mean/std
        fwd_ret = compute_forward_returns(prices, horizon=best_h)
        aligned = pd.DataFrame({
            "factor": factor_values,
            "returns": fwd_ret,
        }).dropna()

        # Compute IC over sliding windows for IR calculation
        window_size = min(60, len(aligned) // 4)
        if window_size < 20:
            window_size = 20

        rolling_ics: List[float] = []
        for i in range(window_size, len(aligned), max(1, window_size // 4)):
            start = max(0, i - window_size)
            w_factor = aligned["factor"].iloc[start:i]
            w_returns = aligned["returns"].iloc[start:i]
            rolling_ics.append(compute_ic(w_factor, w_returns))

        mean_ic = float(np.mean(rolling_ics)) if rolling_ics else 0.0
        ic_std = float(np.std(rolling_ics)) if rolling_ics else 1.0

        if ic_std == 0:
            ic_std = 1.0
        ic_ir = mean_ic / ic_std

        # IC history by period
        history = compute_ic_history(
            factor_values, prices,
            period_days=period_days,
            horizon=best_h,
        )

        # Decay detection
        decay_half = _estimate_decay_half_life(ic_decay)
        is_decaying = _detect_ic_trend(history)

        return cls(
            factor_name=factor_name,
            mean_ic=mean_ic,
            ic_std=ic_std,
            ic_ir=ic_ir,
            decay_half_life=decay_half,
            best_horizon=best_h,
            is_decaying=is_decaying,
            ic_by_horizon=ic_decay,
            ic_history=history,
        )

    @property
    def is_useful(self) -> bool:
        """Factor passes minimum quality threshold for inclusion."""
        return abs(self.mean_ic) > 0.03 and abs(self.ic_ir) > 0.3

    @property
    def quality_tier(self) -> str:
        """Classify factor quality: 'strong', 'moderate', 'weak', 'noise'."""
        abs_ic = abs(self.mean_ic)
        abs_ir = abs(self.ic_ir)

        if abs_ic > 0.07 and abs_ir > 1.0:
            return "strong"
        elif abs_ic > 0.05 and abs_ir > 0.5:
            return "moderate"
        elif abs_ic > 0.03 and abs_ir > 0.3:
            return "weak"
        else:
            return "noise"

    def summary(self) -> str:
        """Human-readable summary string."""
        return (
            f"ICReport({self.factor_name}): "
            f"mean_IC={self.mean_ic:.4f}, IC_IR={self.ic_ir:.2f}, "
            f"best_horizon={self.best_horizon}h, "
            f"half_life={self.decay_half_life:.1f}bars, "
            f"quality={self.quality_tier}, decaying={self.is_decaying}"
        )
