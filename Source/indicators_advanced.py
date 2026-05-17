"""
Advanced technical indicator suite for the trading bot.

Provides ADX regime detection, Stochastic oscillator for entry timing,
Fibonacci retracement/extension level calculator, Volume SMA confirmation,
and VWAP intraday bias -- built on the ``ta`` library and custom Fibonacci
logic.

These indicators complement the core suite in :mod:`indicators`:
- ADX tells the bot *whether* to trend-follow or mean-revert.
- Stochastic refines entry timing in ranging markets.
- Fibonacci marks key support/resistance levels for confluence.
- Volume SMA confirms whether a move has conviction.
- VWAP provides intraday directional bias.

Usage:
    from trading_bot.source.candle_pipeline import CandlePipeline
    from trading_bot.source.indicators_advanced import AdvancedIndicators

    candles = pipeline.fetch_candles("EUR_USD", "H1", count=250)
    adv = AdvancedIndicators(candles)
    result = adv.compute_all()
"""

from typing import Any, Dict, List, Optional

import pandas as pd
import ta.momentum
import ta.trend
import ta.volume


# ---------------------------------------------------------------------------
# Candle-to-DataFrame conversion
# ---------------------------------------------------------------------------
# The canonical ``candles_to_dataframe`` lives in indicators.py (03-01).
# Since both plans execute in parallel, we include a local copy so this
# module is self-contained.  If indicators.py is available we prefer its
# version for consistency; otherwise we fall back to the local one.
# ---------------------------------------------------------------------------

def _candles_to_dataframe(candles: List[Dict[str, Any]]) -> pd.DataFrame:
    """Convert Oanda candle dicts to a pandas DataFrame.

    Filters to ``complete=True`` candles, casts price strings to float,
    sorts by time ascending, and sets time as the index.

    Args:
        candles: List of Oanda candle dicts, each containing
            ``time``, ``mid`` (with o/h/l/c strings), ``volume``,
            and ``complete``.

    Returns:
        DataFrame with columns: open, high, low, close, volume
        and a DatetimeIndex named ``time``.
    """
    complete = [c for c in candles if c.get("complete", False)]
    if not complete:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    rows = []
    for idx, c in enumerate(complete):
        mid = c["mid"]
        try:
            ts = pd.to_datetime(c["time"], utc=True)
        except Exception:
            # Fallback for malformed timestamps (e.g. synthetic test data):
            # assign sequential UTC timestamps to preserve ordering.
            ts = pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(minutes=idx)
        rows.append(
            {
                "time": ts,
                "open": float(mid["o"]),
                "high": float(mid["h"]),
                "low": float(mid["l"]),
                "close": float(mid["c"]),
                "volume": int(c["volume"]),
            }
        )
    df = pd.DataFrame(rows)
    df.sort_values("time", inplace=True)
    df.set_index("time", inplace=True)
    return df


# Prefer the canonical version when available.
try:
    from .indicators import candles_to_dataframe  # type: ignore[import-untyped]
except ImportError:
    candles_to_dataframe = _candles_to_dataframe  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AdvancedIndicators class
# ---------------------------------------------------------------------------

class AdvancedIndicators:
    """Advanced technical indicator suite.

    Accepts raw Oanda candle dicts (as returned by
    :class:`CandlePipeline`), converts to a DataFrame internally, and
    provides methods for ADX, Stochastic, Volume SMA, Fibonacci, and
    VWAP computation.

    Args:
        candles: List of Oanda candle dicts (mid price component).
    """

    def __init__(self, candles: List[Dict[str, Any]]) -> None:
        self.df: pd.DataFrame = candles_to_dataframe(candles)

    # ------------------------------------------------------------------
    # ADX regime detection
    # ------------------------------------------------------------------

    def compute_adx(self, period: int = 14) -> Dict[str, Any]:
        """Compute ADX and classify the market regime.

        Args:
            period: ADX look-back window (default 14).

        Returns:
            Dict with keys:
            - adx: Latest ADX value.
            - plus_di: Latest +DI value.
            - minus_di: Latest -DI value.
            - regime: ``'trending'`` (ADX > 25), ``'ranging'`` (< 20),
              or ``'mixed'`` (20-25).
            - trend_direction: ``'bullish'`` (+DI > -DI),
              ``'bearish'`` (-DI > +DI), or ``'neutral'`` (equal).
        """
        adx_indicator = ta.trend.ADXIndicator(
            high=self.df["high"],
            low=self.df["low"],
            close=self.df["close"],
            window=period,
        )

        adx_val = adx_indicator.adx().iloc[-1]
        plus_di = adx_indicator.adx_pos().iloc[-1]
        minus_di = adx_indicator.adx_neg().iloc[-1]

        # Classify regime
        if adx_val > 25:
            regime = "trending"
        elif adx_val < 20:
            regime = "ranging"
        else:
            regime = "mixed"

        # Trend direction from DI comparison
        if plus_di > minus_di:
            trend_direction = "bullish"
        elif minus_di > plus_di:
            trend_direction = "bearish"
        else:
            trend_direction = "neutral"

        return {
            "adx": float(adx_val),
            "plus_di": float(plus_di),
            "minus_di": float(minus_di),
            "regime": regime,
            "trend_direction": trend_direction,
        }

    # ------------------------------------------------------------------
    # Stochastic oscillator
    # ------------------------------------------------------------------

    def compute_stochastic(
        self,
        k_period: int = 14,
        d_period: int = 3,
        smooth_k: int = 3,
    ) -> Dict[str, Any]:
        """Compute Stochastic Oscillator with crossover detection.

        Args:
            k_period: %K look-back window (default 14).
            d_period: %D smoothing period (default 3).
            smooth_k: %K smoothing window (default 3).

        Returns:
            Dict with keys:
            - k: Latest %K value (0-100).
            - d: Latest %D value (0-100).
            - overbought: True if %K > 80.
            - oversold: True if %K < 20.
            - crossover: ``'bullish'`` if %K crossed above %D,
              ``'bearish'`` if %K crossed below %D, else ``None``.
        """
        stoch = ta.momentum.StochasticOscillator(
            high=self.df["high"],
            low=self.df["low"],
            close=self.df["close"],
            window=k_period,
            smooth_window=smooth_k,
            fillna=False,
        )

        k_series = stoch.stoch()
        d_series = stoch.stoch_signal()

        k_val = float(k_series.iloc[-1])
        d_val = float(d_series.iloc[-1])

        # Crossover detection: compare last two bars
        crossover = None
        if len(k_series) >= 2 and len(d_series) >= 2:
            prev_k = k_series.iloc[-2]
            prev_d = d_series.iloc[-2]
            if not (pd.isna(prev_k) or pd.isna(prev_d)):
                if k_val > d_val and prev_k <= prev_d:
                    crossover = "bullish"
                elif k_val < d_val and prev_k >= prev_d:
                    crossover = "bearish"

        return {
            "k": k_val,
            "d": d_val,
            "overbought": k_val > 80,
            "oversold": k_val < 20,
            "crossover": crossover,
        }

    # ------------------------------------------------------------------
    # Volume SMA confirmation
    # ------------------------------------------------------------------

    def compute_volume_sma(self, period: int = 20) -> Dict[str, Any]:
        """Compute Volume Simple Moving Average and confirmation signal.

        Note: Oanda volume is *tick volume* (number of price changes),
        not true exchange volume.  This is standard for forex and still
        useful for confirming move strength.

        Args:
            period: SMA look-back window (default 20).

        Returns:
            Dict with keys:
            - current_volume: Most recent bar's volume.
            - sma: Volume SMA value.
            - ratio: current_volume / sma.
            - confirmation: ``'high'`` if ratio >= 1, ``'low'`` otherwise.
        """
        vol = self.df["volume"].astype(float)
        sma = vol.rolling(window=period).mean().iloc[-1]
        current = float(vol.iloc[-1])

        ratio = current / sma if sma > 0 else 0.0

        return {
            "current_volume": int(current),
            "sma": float(sma),
            "ratio": float(ratio),
            "confirmation": "high" if ratio >= 1.0 else "low",
        }

    # ------------------------------------------------------------------
    # Fibonacci retracement / extension
    # ------------------------------------------------------------------

    def compute_fibonacci(self, lookback: int = 100) -> Dict[str, Any]:
        """Compute Fibonacci retracement and extension levels.

        Detects swing high and swing low within the *lookback* window,
        determines trend direction from temporal ordering, and calculates
        retracement (0-100%) and extension (127.2%, 161.8%) levels.

        Args:
            lookback: Number of bars to scan for swing points (default 100).

        Returns:
            Dict with keys:
            - swing_high: Swing high price.
            - swing_low: Swing low price.
            - trend: ``'up'`` or ``'down'``.
            - retracement_levels: Dict mapping level (e.g. 0.236) to price.
            - extension_levels: Dict mapping level (1.272, 1.618) to price.
            - nearest_level: Dict with ``level``, ``price``, ``distance_pct``.
        """
        window = self.df.tail(lookback)
        highs = window["high"]
        lows = window["low"]

        swing_high = float(highs.max())
        swing_low = float(lows.min())
        swing_high_idx = highs.idxmax()
        swing_low_idx = lows.idxmin()

        # Determine trend: if swing low came first (earlier), trend is up
        trend = "up" if swing_low_idx < swing_high_idx else "down"

        price_range = swing_high - swing_low
        if price_range == 0:
            price_range = 1e-10  # avoid division by zero

        # Retracement levels
        ret_levels = [0.0, 0.236, 0.328, 0.382, 0.5, 0.618, 0.786, 1.0]
        retracement: Dict[float, float] = {}
        for level in ret_levels:
            if trend == "up":
                # Uptrend: levels descend from swing_high
                retracement[level] = swing_high - price_range * level
            else:
                # Downtrend: levels ascend from swing_low
                retracement[level] = swing_low + price_range * level

        # Extension levels
        ext_levels = [1.272, 1.618]
        extensions: Dict[float, float] = {}
        for level in ext_levels:
            if trend == "up":
                extensions[level] = swing_high + price_range * (level - 1.0)
            else:
                extensions[level] = swing_low - price_range * (level - 1.0)

        # Find nearest level to current price
        current_price = float(self.df["close"].iloc[-1])
        all_levels = {**retracement, **extensions}
        nearest_level = None
        nearest_price = None
        nearest_dist = float("inf")
        for lvl, px in all_levels.items():
            dist = abs(current_price - px)
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_level = lvl
                nearest_price = px

        distance_pct = (nearest_dist / current_price) * 100 if current_price != 0 else 0.0

        return {
            "swing_high": swing_high,
            "swing_low": swing_low,
            "trend": trend,
            "retracement_levels": retracement,
            "extension_levels": extensions,
            "nearest_level": {
                "level": nearest_level,
                "price": nearest_price,
                "distance_pct": distance_pct,
            },
        }

    # ------------------------------------------------------------------
    # VWAP (Volume-Weighted Average Price)
    # ------------------------------------------------------------------

    def compute_vwap(self) -> Dict[str, Any]:
        """Compute VWAP and intraday directional bias.

        The ``ta`` library's VWAP implementation resets daily by default,
        which is the desired behaviour for intraday bias assessment.

        Returns:
            Dict with keys:
            - vwap: Latest VWAP value.
            - price_vs_vwap: ``'above'`` or ``'below'``.
            - distance_pct: Percentage distance from VWAP.
        """
        vwap_indicator = ta.volume.VolumeWeightedAveragePrice(
            high=self.df["high"],
            low=self.df["low"],
            close=self.df["close"],
            volume=self.df["volume"].astype(float),
        )

        vwap_series = vwap_indicator.volume_weighted_average_price()
        vwap_val = float(vwap_series.iloc[-1])
        current_price = float(self.df["close"].iloc[-1])

        position = "above" if current_price >= vwap_val else "below"
        distance_pct = (
            abs(current_price - vwap_val) / vwap_val * 100
            if vwap_val != 0
            else 0.0
        )

        return {
            "vwap": vwap_val,
            "price_vs_vwap": position,
            "distance_pct": distance_pct,
        }

    # ------------------------------------------------------------------
    # Convenience: compute everything
    # ------------------------------------------------------------------

    def compute_all(self) -> Dict[str, Any]:
        """Compute all advanced indicators and return a consolidated dict.

        Returns:
            Dict with keys: ``adx``, ``stochastic``, ``volume_sma``,
            ``fibonacci``, ``vwap``.
        """
        return {
            "adx": self.compute_adx(),
            "stochastic": self.compute_stochastic(),
            "volume_sma": self.compute_volume_sma(),
            "fibonacci": self.compute_fibonacci(),
            "vwap": self.compute_vwap(),
        }
