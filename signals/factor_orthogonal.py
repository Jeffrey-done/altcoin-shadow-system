"""
Factor Orthogonalization Module
================================

Reduces multicollinearity in the factor set to improve composite signal
quality. Provides three approaches:

  1. Correlation filtering — drop highly correlated factors (keep higher IC)
  2. PCA orthogonalization — transform to uncorrelated principal components
  3. Market residualization — remove systematic market beta from each factor

The output is a reduced, orthogonal factor set suitable for weighted scoring.

Usage:
    from signals.factor_orthogonal import (
        remove_correlated_factors,
        pca_orthogonalize,
        residualize,
        OrthogonalFactorSet,
    )

    # Step 1: Remove redundant factors
    filtered = remove_correlated_factors(factor_df, max_corr=0.7, ic_values=ic_dict)

    # Step 2: PCA transform for remaining
    ortho_df, pca_info = pca_orthogonalize(filtered, variance_threshold=0.95)

    # Step 3: Remove market beta
    clean = residualize(factor_df, market_returns)

Dependencies: numpy, pandas. Optional: sklearn (for robust PCA fallback).
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)



# ══════════════════════════════════════════════════════════════════
#  Data Structures
# ══════════════════════════════════════════════════════════════════

@dataclass
class PCAInfo:
    """Results metadata from PCA orthogonalization."""
    n_components: int
    explained_variance_ratio: np.ndarray
    cumulative_variance: float
    loadings: np.ndarray  # shape: (n_components, n_original_factors)
    component_names: List[str] = field(default_factory=list)


@dataclass
class OrthogonalFactorSet:
    """Summary of the orthogonalization process."""
    n_original: int
    n_retained: int
    explained_variance: float
    correlation_matrix: pd.DataFrame
    dropped_factors: List[str] = field(default_factory=list)
    method: str = "correlation_filter"


# ══════════════════════════════════════════════════════════════════
#  Correlation Filtering
# ══════════════════════════════════════════════════════════════════

def remove_correlated_factors(
    factor_df: pd.DataFrame,
    max_corr: float = 0.7,
    ic_values: Optional[Dict[str, float]] = None,
) -> Tuple[pd.DataFrame, OrthogonalFactorSet]:
    """
    Remove highly correlated factors, keeping the one with higher IC.

    For each pair of factors with |correlation| > max_corr, drop the factor
    with lower absolute IC. If IC values are not provided, uses variance
    as a tiebreaker (higher variance retained).

    Args:
        factor_df: DataFrame of factor values (columns = factors).
        max_corr: Maximum allowed pairwise correlation.
        ic_values: Optional dict of factor_name -> IC value for prioritization.

    Returns:
        Tuple of (filtered DataFrame, OrthogonalFactorSet metadata).
    """
    if factor_df.empty or factor_df.shape[1] <= 1:
        info = OrthogonalFactorSet(
            n_original=factor_df.shape[1],
            n_retained=factor_df.shape[1],
            explained_variance=1.0,
            correlation_matrix=pd.DataFrame(),
        )
        return factor_df, info

    # Compute correlation matrix (use available observations)
    corr_matrix = factor_df.corr(method="spearman")
    n_original = factor_df.shape[1]

    # Priority score: |IC| if available, else variance
    if ic_values:
        priority = {col: abs(ic_values.get(col, 0.0)) for col in factor_df.columns}
    else:
        priority = {col: factor_df[col].var() for col in factor_df.columns}

    # Iteratively remove the lower-priority factor from correlated pairs
    dropped: List[str] = []
    remaining = list(factor_df.columns)

    while True:
        found_pair = False
        for i in range(len(remaining)):
            if found_pair:
                break
            for j in range(i + 1, len(remaining)):
                col_i = remaining[i]
                col_j = remaining[j]
                if abs(corr_matrix.loc[col_i, col_j]) > max_corr:
                    # Drop the one with lower priority
                    if priority.get(col_i, 0) >= priority.get(col_j, 0):
                        to_drop = col_j
                    else:
                        to_drop = col_i
                    dropped.append(to_drop)
                    remaining.remove(to_drop)
                    found_pair = True
                    break
        if not found_pair:
            break

    filtered_df = factor_df[remaining]

    info = OrthogonalFactorSet(
        n_original=n_original,
        n_retained=len(remaining),
        explained_variance=len(remaining) / max(n_original, 1),
        correlation_matrix=corr_matrix,
        dropped_factors=dropped,
        method="correlation_filter",
    )

    logger.info(
        "Correlation filter: %d -> %d factors (dropped: %s)",
        n_original, len(remaining), dropped,
    )

    return filtered_df, info



# ══════════════════════════════════════════════════════════════════
#  PCA Orthogonalization
# ══════════════════════════════════════════════════════════════════

def _numpy_pca(
    data: np.ndarray,
    n_components: Optional[int] = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Pure numpy PCA implementation (fallback when sklearn unavailable).

    Args:
        data: Centered data matrix (n_samples, n_features).
        n_components: Number of components to retain (None = all).

    Returns:
        (transformed_data, explained_variance_ratio, loadings)
    """
    n_samples, n_features = data.shape
    if n_components is None:
        n_components = min(n_samples, n_features)

    # Covariance matrix
    cov_matrix = np.cov(data, rowvar=False)

    # Eigen decomposition
    eigenvalues, eigenvectors = np.linalg.eigh(cov_matrix)

    # Sort by eigenvalue descending
    idx = np.argsort(eigenvalues)[::-1]
    eigenvalues = eigenvalues[idx]
    eigenvectors = eigenvectors[:, idx]

    # Select top components
    eigenvalues = eigenvalues[:n_components]
    eigenvectors = eigenvectors[:, :n_components]

    # Transform data
    transformed = data @ eigenvectors

    # Explained variance ratio
    total_var = eigenvalues.sum() if eigenvalues.sum() > 0 else 1.0
    explained_var_ratio = eigenvalues / total_var

    # Loadings (eigenvectors transposed)
    loadings = eigenvectors.T

    return transformed, explained_var_ratio, loadings


def pca_orthogonalize(
    factor_df: pd.DataFrame,
    n_components: Optional[int] = None,
    variance_threshold: float = 0.95,
) -> Tuple[pd.DataFrame, PCAInfo]:
    """
    Transform factors into orthogonal principal components.

    Automatically selects the number of components to explain at least
    `variance_threshold` fraction of total variance (if n_components=None).

    Args:
        factor_df: DataFrame of factor values (rows=time, cols=factors).
        n_components: Fixed number of components (overrides variance_threshold).
        variance_threshold: Minimum cumulative variance explained (default 0.95).

    Returns:
        Tuple of (orthogonal DataFrame, PCAInfo metadata).
    """
    # Drop rows with any NaN for stable PCA
    clean_df = factor_df.dropna()
    if clean_df.empty or clean_df.shape[1] < 2:
        pca_info = PCAInfo(
            n_components=factor_df.shape[1],
            explained_variance_ratio=np.array([1.0]),
            cumulative_variance=1.0,
            loadings=np.eye(factor_df.shape[1]),
        )
        return factor_df, pca_info

    # Standardize
    means = clean_df.mean()
    stds = clean_df.std().replace(0, 1.0)
    standardized = (clean_df - means) / stds
    data_matrix = standardized.values

    # Try sklearn first, fallback to numpy
    try:
        from sklearn.decomposition import PCA as SklearnPCA
        use_sklearn = True
    except ImportError:
        use_sklearn = False

    if use_sklearn:
        max_comp = min(data_matrix.shape[0], data_matrix.shape[1])
        pca_model = SklearnPCA(n_components=max_comp)
        transformed_all = pca_model.fit_transform(data_matrix)
        explained_var_ratio = pca_model.explained_variance_ratio_
        loadings_all = pca_model.components_
    else:
        transformed_all, explained_var_ratio, loadings_all = _numpy_pca(data_matrix)


    # Determine number of components
    if n_components is None:
        cumulative = np.cumsum(explained_var_ratio)
        n_components = int(np.searchsorted(cumulative, variance_threshold) + 1)
        n_components = min(n_components, len(explained_var_ratio))
    else:
        n_components = min(n_components, len(explained_var_ratio))

    # Truncate to selected components
    transformed = transformed_all[:, :n_components]
    selected_var_ratio = explained_var_ratio[:n_components]
    selected_loadings = loadings_all[:n_components]

    # Create output DataFrame
    component_names = [f"PC_{i+1}" for i in range(n_components)]
    ortho_df = pd.DataFrame(
        transformed,
        index=clean_df.index,
        columns=component_names,
    )

    # Reindex to original index (NaN where we dropped rows)
    ortho_df = ortho_df.reindex(factor_df.index)

    pca_info = PCAInfo(
        n_components=n_components,
        explained_variance_ratio=selected_var_ratio,
        cumulative_variance=float(np.sum(selected_var_ratio)),
        loadings=selected_loadings,
        component_names=component_names,
    )

    logger.info(
        "PCA: %d factors -> %d components (%.1f%% variance explained)",
        factor_df.shape[1], n_components, pca_info.cumulative_variance * 100,
    )

    return ortho_df, pca_info


# ══════════════════════════════════════════════════════════════════
#  Market Residualization
# ══════════════════════════════════════════════════════════════════

def residualize(
    factor_df: pd.DataFrame,
    market_factor: pd.Series,
) -> pd.DataFrame:
    """
    Remove market beta from each factor using OLS regression residuals.

    For each factor column, regresses it against the market factor and
    retains only the residual (idiosyncratic component).

    Args:
        factor_df: DataFrame of factor values.
        market_factor: Market return or index series (same index as factor_df).

    Returns:
        DataFrame of residualized factors (same shape as input).
    """
    residuals: Dict[str, pd.Series] = {}
    market = market_factor.reindex(factor_df.index)

    for col in factor_df.columns:
        factor_col = factor_df[col]

        # Align and drop NaN
        aligned = pd.DataFrame({"factor": factor_col, "market": market}).dropna()

        if len(aligned) < 10:
            # Not enough data, return original
            residuals[col] = factor_col
            continue

        x = aligned["market"].values
        y = aligned["factor"].values

        # OLS: y = alpha + beta * x + residual
        x_with_const = np.column_stack([np.ones(len(x)), x])
        try:
            # Normal equations: (X'X)^-1 X'y
            beta = np.linalg.lstsq(x_with_const, y, rcond=None)[0]
            predicted = x_with_const @ beta
            resid = y - predicted
        except np.linalg.LinAlgError:
            # Fallback: use original
            residuals[col] = factor_col
            continue

        resid_series = pd.Series(resid, index=aligned.index)
        # Reindex to full factor index
        residuals[col] = resid_series.reindex(factor_df.index)

    result = pd.DataFrame(residuals)
    logger.info("Residualized %d factors against market factor", len(factor_df.columns))
    return result



# ══════════════════════════════════════════════════════════════════
#  Convenience Pipeline
# ══════════════════════════════════════════════════════════════════

def full_orthogonalization_pipeline(
    factor_df: pd.DataFrame,
    market_factor: Optional[pd.Series] = None,
    ic_values: Optional[Dict[str, float]] = None,
    max_corr: float = 0.7,
    variance_threshold: float = 0.95,
    use_pca: bool = False,
) -> Tuple[pd.DataFrame, OrthogonalFactorSet]:
    """
    Run the full orthogonalization pipeline:
      1. Remove market beta (if market_factor provided)
      2. Filter correlated factors
      3. Optionally PCA transform

    Args:
        factor_df: Raw factor DataFrame.
        market_factor: Optional market return series for residualization.
        ic_values: IC values for prioritizing which factors to keep.
        max_corr: Maximum pairwise correlation allowed.
        variance_threshold: PCA variance threshold (if use_pca=True).
        use_pca: Whether to apply PCA after correlation filtering.

    Returns:
        Tuple of (processed factor DataFrame, OrthogonalFactorSet info).
    """
    current_df = factor_df.copy()
    n_original = current_df.shape[1]

    # Step 1: Residualize against market
    if market_factor is not None:
        current_df = residualize(current_df, market_factor)
        logger.info("Step 1: Residualized %d factors against market", n_original)

    # Step 2: Correlation filter
    filtered_df, filter_info = remove_correlated_factors(
        current_df, max_corr=max_corr, ic_values=ic_values
    )

    # Step 3: Optional PCA
    if use_pca and filtered_df.shape[1] > 2:
        pca_df, pca_info = pca_orthogonalize(
            filtered_df, variance_threshold=variance_threshold
        )
        explained = pca_info.cumulative_variance
        result_df = pca_df
    else:
        explained = filter_info.explained_variance
        result_df = filtered_df

    # Build summary
    corr_matrix = result_df.dropna().corr() if not result_df.empty else pd.DataFrame()

    summary = OrthogonalFactorSet(
        n_original=n_original,
        n_retained=result_df.shape[1],
        explained_variance=explained,
        correlation_matrix=corr_matrix,
        dropped_factors=filter_info.dropped_factors,
        method="pipeline_corr" + ("_pca" if use_pca else ""),
    )

    return result_df, summary
