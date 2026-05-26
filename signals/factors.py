"""
Multi-Factor Quantitative Signal Library
=========================================

Production-grade factor computation engine for the altcoin short-selling system.
Provides 30+ alpha factors organized into 5 categories:

  1. Momentum   — RSI, ROC, Williams%R, CCI, MACD, Stochastic
  2. Volume     — OBV slope, volume ratios, VWAP deviation, MFI
  3. Volatility — ATR, Bollinger bandwidth, realized vol, Keltner
  4. Trend      — ADX, Aroon, EMA cross, linear regression, Supertrend
  5. Microstructure — funding rate, OI changes, liquidation pressure

Usage:
    from signals.factors import FactorRegistry

    registry = FactorRegistry()
    factor_df = registry.compute_all(ohlcv_df)
    # factor_df columns = factor names, values = z-scores

All factor functions accept a pandas DataFrame with columns:
    [timestamp, open, high, low, close, volume]
and return a pandas Series aligned to the input index.

Dependencies: numpy, pandas (no sklearn required).
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  Factor Registry Infrastructure
# ══════════════════════════════════════════════════════════════════

_GLOBAL_FACTOR_REGISTRY: Dict[str, "FactorMeta"] = {}



@dataclass
class FactorMeta:
    """Metadata for a registered factor."""
    name: str
    category: str
    func: Callable
    description: str = ""


def register_factor(name: str, category: str, description: str = ""):
    """
    Decorator to register a factor function into the global registry.

    Usage:
        @register_factor("rsi_14", "momentum", "Wilder RSI period 14")
        def rsi_14(df: pd.DataFrame) -> pd.Series:
            ...
    """
    def decorator(func: Callable) -> Callable:
        meta = FactorMeta(
            name=name,
            category=category,
            func=func,
            description=description,
        )
        _GLOBAL_FACTOR_REGISTRY[name] = meta
        return func
    return decorator



class FactorRegistry:
    """
    Central registry managing all factor computations.

    Provides batch computation, z-score normalization, and
    category-based filtering.
    """

    def __init__(self, normalize: bool = True, zscore_window: int = 100):
        """
        Args:
            normalize: If True, output z-scores instead of raw values.
            zscore_window: Rolling window for z-score normalization.
        """
        self.normalize = normalize
        self.zscore_window = zscore_window
        self._registry: Dict[str, FactorMeta] = dict(_GLOBAL_FACTOR_REGISTRY)

    @property
    def factor_names(self) -> List[str]:
        """All registered factor names."""
        return list(self._registry.keys())

    @property
    def categories(self) -> Dict[str, List[str]]:
        """Factor names grouped by category."""
        cats: Dict[str, List[str]] = {}
        for name, meta in self._registry.items():
            cats.setdefault(meta.category, []).append(name)
        return cats

    def compute_all(
        self,
        df: pd.DataFrame,
        categories: Optional[List[str]] = None,
        external_data: Optional[Dict[str, pd.Series]] = None,
    ) -> pd.DataFrame:
        """
        Compute all registered factors for the given OHLCV data.

        Args:
            df: DataFrame with columns [timestamp, open, high, low, close, volume].
            categories: If provided, only compute factors in these categories.
            external_data: Optional dict of external Series (e.g., funding_rate)
                           passed to microstructure factors.

        Returns:
            DataFrame with one column per factor, z-score normalized by default.
        """
        results: Dict[str, pd.Series] = {}
        for name, meta in self._registry.items():
            if categories and meta.category not in categories:
                continue
            try:
                if meta.category == "microstructure" and external_data is not None:
                    series = meta.func(df, external_data=external_data)
                else:
                    series = meta.func(df)
                results[name] = series
            except Exception as e:
                logger.warning(f"Factor '{name}' computation failed: {e}")
                results[name] = pd.Series(np.nan, index=df.index)

        factor_df = pd.DataFrame(results, index=df.index)

        if self.normalize:
            factor_df = self._zscore_normalize(factor_df)

        return factor_df


    def _zscore_normalize(self, factor_df: pd.DataFrame) -> pd.DataFrame:
        """Apply rolling z-score normalization to all factors."""
        rolling_mean = factor_df.rolling(
            window=self.zscore_window, min_periods=20
        ).mean()
        rolling_std = factor_df.rolling(
            window=self.zscore_window, min_periods=20
        ).std()
        # Avoid division by zero
        rolling_std = rolling_std.replace(0, np.nan)
        normalized = (factor_df - rolling_mean) / rolling_std
        return normalized.fillna(0.0)

    def compute_single(self, name: str, df: pd.DataFrame) -> pd.Series:
        """Compute a single factor by name."""
        if name not in self._registry:
            raise KeyError(f"Factor '{name}' not registered")
        return self._registry[name].func(df)

    def get_factor_info(self, name: str) -> FactorMeta:
        """Get metadata for a registered factor."""
        return self._registry[name]


# ══════════════════════════════════════════════════════════════════
#  Helper Functions (vectorized)
# ══════════════════════════════════════════════════════════════════

def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (equivalent to EMA with alpha=1/period)."""
    return series.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    """Compute True Range from OHLCV data."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)



# ══════════════════════════════════════════════════════════════════
#  Category 1: MOMENTUM FACTORS (8)
# ══════════════════════════════════════════════════════════════════

@register_factor("rsi_14", "momentum", "Wilder RSI with 14-period lookback")
def rsi_14(df: pd.DataFrame) -> pd.Series:
    """RSI(14) — standard overbought/oversold momentum oscillator."""
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = _wilder_smooth(gain, 14)
    avg_loss = _wilder_smooth(loss, 14)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


@register_factor("rsi_7", "momentum", "Fast RSI with 7-period lookback")
def rsi_7(df: pd.DataFrame) -> pd.Series:
    """RSI(7) — fast momentum for short-term reversals."""
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = _wilder_smooth(gain, 7)
    avg_loss = _wilder_smooth(loss, 7)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


@register_factor("roc_5", "momentum", "Rate of Change 5 periods")
def roc_5(df: pd.DataFrame) -> pd.Series:
    """ROC(5) — percentage price change over 5 bars."""
    return df["close"].pct_change(periods=5) * 100.0


@register_factor("roc_10", "momentum", "Rate of Change 10 periods")
def roc_10(df: pd.DataFrame) -> pd.Series:
    """ROC(10) — percentage price change over 10 bars."""
    return df["close"].pct_change(periods=10) * 100.0



@register_factor("williams_r_14", "momentum", "Williams %R 14 periods")
def williams_r_14(df: pd.DataFrame) -> pd.Series:
    """Williams %R(14) — momentum oscillator, -100 to 0 range."""
    period = 14
    highest_high = df["high"].rolling(window=period, min_periods=period).max()
    lowest_low = df["low"].rolling(window=period, min_periods=period).min()
    wr = -100.0 * (highest_high - df["close"]) / (highest_high - lowest_low).replace(0, np.nan)
    return wr.fillna(-50.0)


@register_factor("cci_20", "momentum", "Commodity Channel Index 20 periods")
def cci_20(df: pd.DataFrame) -> pd.Series:
    """CCI(20) — measures price deviation from statistical mean."""
    period = 20
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    sma_tp = typical_price.rolling(window=period, min_periods=period).mean()
    mean_dev = typical_price.rolling(window=period, min_periods=period).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    cci = (typical_price - sma_tp) / (0.015 * mean_dev).replace(0, np.nan)
    return cci.fillna(0.0)


@register_factor("macd_hist", "momentum", "MACD Histogram (12,26,9)")
def macd_hist(df: pd.DataFrame) -> pd.Series:
    """MACD Histogram — momentum divergence from signal line."""
    close = df["close"]
    ema_12 = close.ewm(span=12, min_periods=12, adjust=False).mean()
    ema_26 = close.ewm(span=26, min_periods=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    signal_line = macd_line.ewm(span=9, min_periods=9, adjust=False).mean()
    return macd_line - signal_line


@register_factor("stochastic_k", "momentum", "Stochastic %K (14,3)")
def stochastic_k(df: pd.DataFrame) -> pd.Series:
    """Stochastic %K(14,3) — position within recent price range."""
    period = 14
    smooth = 3
    lowest_low = df["low"].rolling(window=period, min_periods=period).min()
    highest_high = df["high"].rolling(window=period, min_periods=period).max()
    raw_k = 100.0 * (df["close"] - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    stoch_k = raw_k.rolling(window=smooth, min_periods=1).mean()
    return stoch_k.fillna(50.0)



# ══════════════════════════════════════════════════════════════════
#  Category 2: VOLUME FACTORS (6)
# ══════════════════════════════════════════════════════════════════

@register_factor("obv_slope", "volume", "OBV linear regression slope")
def obv_slope(df: pd.DataFrame) -> pd.Series:
    """OBV slope — trend direction of On-Balance Volume over 20 bars."""
    period = 20
    sign = np.sign(df["close"].diff())
    obv = (sign * df["volume"]).cumsum()
    # Compute rolling slope via linear regression
    slopes = obv.rolling(window=period, min_periods=period).apply(
        lambda y: np.polyfit(np.arange(len(y)), y, 1)[0], raw=True
    )
    return slopes


@register_factor("volume_ratio_20", "volume", "Current volume / 20-bar SMA volume")
def volume_ratio_20(df: pd.DataFrame) -> pd.Series:
    """Volume ratio — relative volume compared to 20-bar average."""
    avg_vol = df["volume"].rolling(window=20, min_periods=10).mean()
    ratio = df["volume"] / avg_vol.replace(0, np.nan)
    return ratio.fillna(1.0)


@register_factor("vwap_deviation", "volume", "Price deviation from VWAP")
def vwap_deviation(df: pd.DataFrame) -> pd.Series:
    """VWAP deviation — percentage distance of close from session VWAP."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum()
    cum_tp_vol = (typical_price * df["volume"]).cumsum()
    vwap = cum_tp_vol / cum_vol.replace(0, np.nan)
    deviation_pct = (df["close"] - vwap) / vwap.replace(0, np.nan) * 100.0
    return deviation_pct.fillna(0.0)



@register_factor("volume_price_divergence", "volume", "Volume-price divergence score")
def volume_price_divergence(df: pd.DataFrame) -> pd.Series:
    """
    Volume-price divergence — detects price moves on declining volume.
    Positive = price up on declining volume (bearish divergence).
    """
    period = 10
    price_change = df["close"].pct_change(periods=period)
    vol_change = df["volume"].pct_change(periods=period)
    # Divergence: price rising but volume falling, or vice versa
    divergence = price_change * (-vol_change)
    return divergence.fillna(0.0)


@register_factor("accumulation_distribution", "volume", "Accumulation/Distribution line slope")
def accumulation_distribution(df: pd.DataFrame) -> pd.Series:
    """A/D line slope — money flow based on close position within bar range."""
    high_low = (df["high"] - df["low"]).replace(0, np.nan)
    clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / high_low
    ad = (clv * df["volume"]).cumsum()
    # Return 10-bar slope
    slope = ad.rolling(window=10, min_periods=5).apply(
        lambda y: np.polyfit(np.arange(len(y)), y, 1)[0], raw=True
    )
    return slope


@register_factor("money_flow_index", "volume", "Money Flow Index (14)")
def money_flow_index(df: pd.DataFrame) -> pd.Series:
    """MFI(14) — volume-weighted RSI, 0-100 range."""
    period = 14
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    raw_money_flow = typical_price * df["volume"]
    positive_flow = pd.Series(0.0, index=df.index)
    negative_flow = pd.Series(0.0, index=df.index)

    tp_diff = typical_price.diff()
    positive_flow = raw_money_flow.where(tp_diff > 0, 0.0)
    negative_flow = raw_money_flow.where(tp_diff < 0, 0.0)

    pos_sum = positive_flow.rolling(window=period, min_periods=period).sum()
    neg_sum = negative_flow.rolling(window=period, min_periods=period).sum()

    money_ratio = pos_sum / neg_sum.replace(0, np.nan)
    mfi = 100.0 - (100.0 / (1.0 + money_ratio))
    return mfi.fillna(50.0)



# ══════════════════════════════════════════════════════════════════
#  Category 3: VOLATILITY FACTORS (6)
# ══════════════════════════════════════════════════════════════════

@register_factor("atr_14", "volatility", "Average True Range 14 periods")
def atr_14(df: pd.DataFrame) -> pd.Series:
    """ATR(14) — average true range as volatility measure."""
    tr = _true_range(df)
    atr = _wilder_smooth(tr, 14)
    # Normalize by close price for cross-asset comparability
    return (atr / df["close"].replace(0, np.nan) * 100.0).fillna(0.0)


@register_factor("bollinger_bandwidth", "volatility", "Bollinger Bands bandwidth (20,2)")
def bollinger_bandwidth(df: pd.DataFrame) -> pd.Series:
    """Bollinger bandwidth — band width as % of middle band."""
    period = 20
    sma = df["close"].rolling(window=period, min_periods=period).mean()
    std = df["close"].rolling(window=period, min_periods=period).std()
    upper = sma + 2.0 * std
    lower = sma - 2.0 * std
    bandwidth = (upper - lower) / sma.replace(0, np.nan) * 100.0
    return bandwidth.fillna(0.0)


@register_factor("historical_vol_20", "volatility", "20-bar realized volatility (annualized)")
def historical_vol_20(df: pd.DataFrame) -> pd.Series:
    """Historical volatility — 20-bar annualized standard deviation of returns."""
    log_returns = np.log(df["close"] / df["close"].shift(1))
    hvol = log_returns.rolling(window=20, min_periods=10).std() * np.sqrt(365 * 24)
    return hvol.fillna(0.0)


@register_factor("realized_vol_ratio", "volatility", "Short-term vol / long-term vol ratio")
def realized_vol_ratio(df: pd.DataFrame) -> pd.Series:
    """Realized vol ratio — 5-bar vol / 20-bar vol (vol regime detection)."""
    log_returns = np.log(df["close"] / df["close"].shift(1))
    vol_short = log_returns.rolling(window=5, min_periods=3).std()
    vol_long = log_returns.rolling(window=20, min_periods=10).std()
    ratio = vol_short / vol_long.replace(0, np.nan)
    return ratio.fillna(1.0)



@register_factor("keltner_width", "volatility", "Keltner Channel width (20, 1.5 ATR)")
def keltner_width(df: pd.DataFrame) -> pd.Series:
    """Keltner width — channel width normalized by middle line."""
    period = 20
    multiplier = 1.5
    ema_close = df["close"].ewm(span=period, min_periods=period, adjust=False).mean()
    tr = _true_range(df)
    atr = tr.ewm(span=period, min_periods=period, adjust=False).mean()
    upper = ema_close + multiplier * atr
    lower = ema_close - multiplier * atr
    width = (upper - lower) / ema_close.replace(0, np.nan) * 100.0
    return width.fillna(0.0)


@register_factor("price_range_ratio", "volatility", "Intrabar range / ATR ratio")
def price_range_ratio(df: pd.DataFrame) -> pd.Series:
    """Price range ratio — current bar range vs average (expansion detection)."""
    bar_range = df["high"] - df["low"]
    avg_range = bar_range.rolling(window=20, min_periods=10).mean()
    ratio = bar_range / avg_range.replace(0, np.nan)
    return ratio.fillna(1.0)


# ══════════════════════════════════════════════════════════════════
#  Category 4: TREND FACTORS (5)
# ══════════════════════════════════════════════════════════════════

@register_factor("adx_14", "trend", "Average Directional Index 14 periods")
def adx_14(df: pd.DataFrame) -> pd.Series:
    """ADX(14) — trend strength regardless of direction."""
    period = 14
    high = df["high"]
    low = df["low"]
    close = df["close"]

    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    # Only keep the larger directional movement
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)

    tr = _true_range(df)
    atr = _wilder_smooth(tr, period)

    plus_di = 100.0 * _wilder_smooth(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100.0 * _wilder_smooth(minus_dm, period) / atr.replace(0, np.nan)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _wilder_smooth(dx, period)
    return adx.fillna(0.0)



@register_factor("aroon_oscillator", "trend", "Aroon Oscillator (25)")
def aroon_oscillator(df: pd.DataFrame) -> pd.Series:
    """Aroon Oscillator — difference between Aroon Up and Aroon Down."""
    period = 25
    aroon_up = df["high"].rolling(window=period + 1, min_periods=period + 1).apply(
        lambda x: x.argmax() / period * 100.0, raw=True
    )
    aroon_down = df["low"].rolling(window=period + 1, min_periods=period + 1).apply(
        lambda x: x.argmin() / period * 100.0, raw=True
    )
    return (aroon_up - aroon_down).fillna(0.0)


@register_factor("ema_cross_signal", "trend", "EMA(9)/EMA(21) cross signal")
def ema_cross_signal(df: pd.DataFrame) -> pd.Series:
    """EMA cross — normalized distance between EMA(9) and EMA(21)."""
    ema_fast = df["close"].ewm(span=9, min_periods=9, adjust=False).mean()
    ema_slow = df["close"].ewm(span=21, min_periods=21, adjust=False).mean()
    # Normalize by price for cross-asset comparability
    signal = (ema_fast - ema_slow) / df["close"].replace(0, np.nan) * 100.0
    return signal.fillna(0.0)


@register_factor("linear_regression_slope", "trend", "20-bar linear regression slope")
def linear_regression_slope(df: pd.DataFrame) -> pd.Series:
    """Linear regression slope — normalized price trend strength."""
    period = 20
    slopes = df["close"].rolling(window=period, min_periods=period).apply(
        lambda y: np.polyfit(np.arange(len(y)), y, 1)[0], raw=True
    )
    # Normalize by price
    normalized = slopes / df["close"].replace(0, np.nan) * 100.0
    return normalized.fillna(0.0)


@register_factor("supertrend_signal", "trend", "Supertrend direction (10, 3.0)")
def supertrend_signal(df: pd.DataFrame) -> pd.Series:
    """
    Supertrend signal — +1 (uptrend) or -1 (downtrend).
    Uses ATR(10) with multiplier 3.0.
    """
    period = 10
    multiplier = 3.0

    hl2 = (df["high"] + df["low"]) / 2.0
    tr = _true_range(df)
    atr = tr.ewm(span=period, min_periods=period, adjust=False).mean()

    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    close = df["close"]
    direction = pd.Series(1, index=df.index, dtype=float)

    for i in range(1, len(df)):
        if close.iloc[i] > upper_band.iloc[i - 1]:
            direction.iloc[i] = 1
        elif close.iloc[i] < lower_band.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]

    return direction



# ══════════════════════════════════════════════════════════════════
#  Category 5: MICROSTRUCTURE FACTORS (5)
# ══════════════════════════════════════════════════════════════════

@register_factor("funding_rate_zscore", "microstructure", "Funding rate z-score")
def funding_rate_zscore(df: pd.DataFrame, external_data: Optional[Dict[str, pd.Series]] = None) -> pd.Series:
    """
    Funding rate z-score — standardized funding rate.
    High positive = market overleveraged long (short signal).
    """
    if external_data and "funding_rate" in external_data:
        funding = external_data["funding_rate"].reindex(df.index, method="ffill")
    else:
        # Fallback: synthetic funding proxy from price momentum
        funding = df["close"].pct_change(8).rolling(window=24, min_periods=12).mean()

    mean = funding.rolling(window=168, min_periods=48).mean()  # 7-day mean
    std = funding.rolling(window=168, min_periods=48).std()
    zscore = (funding - mean) / std.replace(0, np.nan)
    return zscore.fillna(0.0)


@register_factor("oi_change_rate", "microstructure", "Open interest change rate")
def oi_change_rate(df: pd.DataFrame, external_data: Optional[Dict[str, pd.Series]] = None) -> pd.Series:
    """
    OI change rate — rapid OI increases signal speculative buildup.
    """
    if external_data and "open_interest" in external_data:
        oi = external_data["open_interest"].reindex(df.index, method="ffill")
    else:
        # Fallback: proxy from volume pattern
        oi = df["volume"].rolling(window=24, min_periods=12).sum()

    oi_pct_change = oi.pct_change(periods=4) * 100.0
    return oi_pct_change.fillna(0.0)


@register_factor("oi_price_divergence", "microstructure", "OI-price divergence")
def oi_price_divergence(df: pd.DataFrame, external_data: Optional[Dict[str, pd.Series]] = None) -> pd.Series:
    """
    OI-price divergence — price up + OI down = weak rally (short signal).
    """
    if external_data and "open_interest" in external_data:
        oi = external_data["open_interest"].reindex(df.index, method="ffill")
    else:
        oi = df["volume"].rolling(window=24, min_periods=12).sum()

    price_change = df["close"].pct_change(periods=8)
    oi_change = oi.pct_change(periods=8)
    # Divergence: opposite signs
    divergence = price_change * (-oi_change)
    return divergence.fillna(0.0)



@register_factor("large_trade_imbalance", "microstructure", "Large trade buy/sell imbalance")
def large_trade_imbalance(df: pd.DataFrame, external_data: Optional[Dict[str, pd.Series]] = None) -> pd.Series:
    """
    Large trade imbalance — whale buying vs selling pressure.
    Positive = net buying (bearish for shorts).
    """
    if external_data and "large_buy_volume" in external_data and "large_sell_volume" in external_data:
        buy_vol = external_data["large_buy_volume"].reindex(df.index, method="ffill")
        sell_vol = external_data["large_sell_volume"].reindex(df.index, method="ffill")
        total = buy_vol + sell_vol
        imbalance = (buy_vol - sell_vol) / total.replace(0, np.nan)
    else:
        # Proxy: detect large volume bars and their direction
        vol_threshold = df["volume"].rolling(window=50, min_periods=20).quantile(0.9)
        is_large = (df["volume"] > vol_threshold).astype(float)
        direction = np.sign(df["close"] - df["open"])
        imbalance = (is_large * direction).rolling(window=12, min_periods=4).mean()

    return imbalance.fillna(0.0)


@register_factor("liquidation_pressure", "microstructure", "Estimated liquidation pressure")
def liquidation_pressure(df: pd.DataFrame, external_data: Optional[Dict[str, pd.Series]] = None) -> pd.Series:
    """
    Liquidation pressure — proxy for cascading liquidation risk.
    High values indicate price near likely liquidation clusters.
    """
    if external_data and "liquidation_volume" in external_data:
        liq_vol = external_data["liquidation_volume"].reindex(df.index, method="ffill")
        # Normalize by average volume
        avg_vol = df["volume"].rolling(window=24, min_periods=12).mean()
        pressure = liq_vol / avg_vol.replace(0, np.nan)
    else:
        # Proxy: sharp moves on high volume suggest liquidation cascades
        returns_abs = df["close"].pct_change().abs()
        vol_spike = df["volume"] / df["volume"].rolling(window=20, min_periods=10).mean().replace(0, np.nan)
        # Combined metric: sharp moves * volume spikes
        pressure = (returns_abs * vol_spike).rolling(window=6, min_periods=3).mean()

    return pressure.fillna(0.0)
