"""
Candlestick pattern detection engine with context-aware filtering.

Uses TA-Lib to detect all 61 candlestick patterns on Oanda candle data,
classifies them by priority (high/medium/low), and applies context
filtering based on trend direction, support/resistance proximity,
volume confirmation, and ADX regime.

The primary entry points are:
- :meth:`CandlestickPatterns.scan_all` -- run all 61 TA-Lib patterns
- :meth:`CandlestickPatterns.get_detected_patterns` -- structured results for a bar
- :meth:`CandlestickPatterns.get_context_filtered` -- patterns passing context checks

Usage:
    from trading_bot.source.candlestick_patterns import CandlestickPatterns

    candles = pipeline.fetch_candles("EUR_USD", "H1", count=250)
    cp = CandlestickPatterns(candles)

    # Raw detection
    all_patterns = cp.scan_all()

    # Structured detection for last bar
    detected = cp.get_detected_patterns()

    # Context-filtered with confidence scoring
    from trading_bot.source.indicators import Indicators
    from trading_bot.source.indicators_advanced import AdvancedIndicators
    ind = Indicators(candles).compute_all()
    adv = AdvancedIndicators(candles).compute_all()
    filtered = cp.get_context_filtered(indicators_result=ind, advanced_result=adv)
"""

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import talib


# ---------------------------------------------------------------------------
# Candle-to-DataFrame conversion — canonical copy in indicators_advanced.py
# ---------------------------------------------------------------------------
from indicators_advanced import _candles_to_dataframe  # noqa: F401


try:
    from .indicators import Indicators

    candles_to_dataframe = Indicators.candles_to_dataframe
except ImportError:
    candles_to_dataframe = _candles_to_dataframe  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CandlestickPatterns class
# ---------------------------------------------------------------------------


class CandlestickPatterns:
    """TA-Lib candlestick pattern detection with context-aware filtering.

    Accepts raw Oanda candle dicts, converts to a DataFrame, and provides
    methods for scanning all 61 TA-Lib CDL* pattern recognition functions,
    returning structured results with priority classification and optional
    context filtering.

    Args:
        candles: List of Oanda candle dicts (mid price component).
    """

    # ------------------------------------------------------------------
    # Priority classification
    # ------------------------------------------------------------------

    HIGH_PRIORITY = [
        "CDLENGULFING",
        "CDLHAMMER",
        "CDLSHOOTINGSTAR",
        "CDLMORNINGSTAR",
        "CDLEVENINGSTAR",
        "CDL3WHITESOLDIERS",
        "CDL3BLACKCROWS",
    ]

    MEDIUM_PRIORITY = [
        "CDLHIKKAKE",
        "CDLINVERTEDHAMMER",
        "CDLHANGINGMAN",
        "CDLDRAGONFLYDOJI",
        "CDLGRAVESTONEDOJI",
        "CDLTRISTAR",
    ]

    # ------------------------------------------------------------------
    # Candle count mapping
    # ------------------------------------------------------------------

    CANDLE_COUNT: Dict[str, int] = {
        # Single-candle patterns (1)
        "CDLHAMMER": 1,
        "CDLSHOOTINGSTAR": 1,
        "CDLINVERTEDHAMMER": 1,
        "CDLHANGINGMAN": 1,
        "CDLDRAGONFLYDOJI": 1,
        "CDLGRAVESTONEDOJI": 1,
        "CDLDOJI": 1,
        "CDLDOJISTAR": 1,
        "CDLLONGLEGGEDDOJI": 1,
        "CDLRICKSHAWMAN": 1,
        "CDLSPINNINGTOP": 1,
        "CDLHIGHWAVE": 1,
        "CDLMARUBOZU": 1,
        "CDLCLOSINGMARUBOZU": 1,
        "CDLBELTHOLD": 1,
        "CDLTAKURI": 1,
        "CDLHOMINGPIGEON": 1,
        # Two-candle patterns (2)
        "CDLENGULFING": 2,
        "CDLHARAMI": 2,
        "CDLHARAMICROSS": 2,
        "CDLPIERCING": 2,
        "CDLDARKCLOUDCOVER": 2,
        "CDLKICKING": 2,
        "CDLKICKINGBYLENGTH": 2,
        "CDLCOUNTERATTACK": 2,
        "CDLINNECK": 2,
        "CDLONNECK": 2,
        "CDLTHRUSTING": 2,
        "CDLSEPARATINGLINES": 2,
        "CDLMATCHINGLOW": 2,
        "CDLHIKKAKE": 2,
        "CDLHIKKAKEMOD": 2,
        "CDLGAPSIDESIDEWHITE": 2,
        "CDL2CROWS": 2,
        # Three-candle patterns (3)
        "CDLMORNINGSTAR": 3,
        "CDLEVENINGSTAR": 3,
        "CDLMORNINGDOJISTAR": 3,
        "CDLEVENINGDOJISTAR": 3,
        "CDL3WHITESOLDIERS": 3,
        "CDL3BLACKCROWS": 3,
        "CDL3INSIDE": 3,
        "CDL3OUTSIDE": 3,
        "CDL3LINESTRIKE": 3,
        "CDL3STARSINSOUTH": 3,
        "CDLTRISTAR": 3,
        "CDLABANDONEDBABY": 3,
        "CDLTASUKIGAP": 3,
        "CDLADVANCEBLOCK": 3,
        "CDLSTALLEDPATTERN": 3,
        "CDLUNIQUE3RIVER": 3,
        "CDLIDENTICAL3CROWS": 3,
        "CDLXSIDEGAP3METHODS": 3,
        "CDLCONCEALBABYSWALL": 3,
        "CDLSTICKSANDWICH": 3,
        "CDLBREAKAWAY": 3,
        "CDLLADDERBOTTOM": 3,
        "CDLRISEFALL3METHODS": 3,
        "CDLMATHOLD": 3,
    }

    # ------------------------------------------------------------------
    # Human-readable pattern names
    # ------------------------------------------------------------------

    PATTERN_NAMES: Dict[str, str] = {
        # High priority
        "CDLENGULFING": "Engulfing",
        "CDLHAMMER": "Hammer",
        "CDLSHOOTINGSTAR": "Shooting Star",
        "CDLMORNINGSTAR": "Morning Star",
        "CDLEVENINGSTAR": "Evening Star",
        "CDL3WHITESOLDIERS": "Three White Soldiers",
        "CDL3BLACKCROWS": "Three Black Crows",
        # Medium priority
        "CDLHIKKAKE": "Hikkake",
        "CDLINVERTEDHAMMER": "Inverted Hammer",
        "CDLHANGINGMAN": "Hanging Man",
        "CDLDRAGONFLYDOJI": "Dragonfly Doji",
        "CDLGRAVESTONEDOJI": "Gravestone Doji",
        "CDLTRISTAR": "Tri-Star",
        # Other patterns
        "CDL2CROWS": "Two Crows",
        "CDL3BLACKCROWS": "Three Black Crows",
        "CDL3INSIDE": "Three Inside",
        "CDL3LINESTRIKE": "Three Line Strike",
        "CDL3OUTSIDE": "Three Outside",
        "CDL3STARSINSOUTH": "Three Stars In South",
        "CDLABANDONEDBABY": "Abandoned Baby",
        "CDLADVANCEBLOCK": "Advance Block",
        "CDLBELTHOLD": "Belt Hold",
        "CDLBREAKAWAY": "Breakaway",
        "CDLCLOSINGMARUBOZU": "Closing Marubozu",
        "CDLCONCEALBABYSWALL": "Concealing Baby Swallow",
        "CDLCOUNTERATTACK": "Counterattack",
        "CDLDARKCLOUDCOVER": "Dark Cloud Cover",
        "CDLDOJI": "Doji",
        "CDLDOJISTAR": "Doji Star",
        "CDLEVENINGDOJISTAR": "Evening Doji Star",
        "CDLGAPSIDESIDEWHITE": "Gap Side-by-Side White",
        "CDLHARAMI": "Harami",
        "CDLHARAMICROSS": "Harami Cross",
        "CDLHIGHWAVE": "High Wave",
        "CDLHIKKAKEMOD": "Modified Hikkake",
        "CDLHOMINGPIGEON": "Homing Pigeon",
        "CDLIDENTICAL3CROWS": "Identical Three Crows",
        "CDLINNECK": "In-Neck",
        "CDLKICKING": "Kicking",
        "CDLKICKINGBYLENGTH": "Kicking By Length",
        "CDLLADDERBOTTOM": "Ladder Bottom",
        "CDLLONGLEGGEDDOJI": "Long-Legged Doji",
        "CDLMARUBOZU": "Marubozu",
        "CDLMATCHINGLOW": "Matching Low",
        "CDLMATHOLD": "Mat Hold",
        "CDLMORNINGDOJISTAR": "Morning Doji Star",
        "CDLONNECK": "On-Neck",
        "CDLPIERCING": "Piercing",
        "CDLRICKSHAWMAN": "Rickshaw Man",
        "CDLRISEFALL3METHODS": "Rise/Fall Three Methods",
        "CDLSEPARATINGLINES": "Separating Lines",
        "CDLSTICKSANDWICH": "Stick Sandwich",
        "CDLSTALLEDPATTERN": "Stalled Pattern",
        "CDLSPINNINGTOP": "Spinning Top",
        "CDLTAKURI": "Takuri",
        "CDLTASUKIGAP": "Tasuki Gap",
        "CDLTHRUSTING": "Thrusting",
        "CDLUNIQUE3RIVER": "Unique Three River",
        "CDLXSIDEGAP3METHODS": "Side Gap Three Methods",
    }

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self, candles: List[Dict[str, Any]]) -> None:
        self.df: pd.DataFrame = candles_to_dataframe(candles)
        self._scan_cache: Optional[Dict[str, np.ndarray]] = None

    # ------------------------------------------------------------------
    # Core scanning
    # ------------------------------------------------------------------

    def scan_all(self) -> Dict[str, np.ndarray]:
        """Run all 61 TA-Lib CDL* pattern recognition functions.

        Dynamically retrieves the pattern function list from TA-Lib and
        calls each with the open/high/low/close numpy arrays.

        Returns:
            Dict keyed by TA-Lib function name (e.g. ``'CDL2CROWS'``)
            with values being the raw output arrays.  Only patterns with
            at least one non-zero signal are included.
        """
        if self._scan_cache is not None:
            return self._scan_cache

        open_arr = self.df["open"].values.astype(np.float64)
        high_arr = self.df["high"].values.astype(np.float64)
        low_arr = self.df["low"].values.astype(np.float64)
        close_arr = self.df["close"].values.astype(np.float64)

        pattern_funcs = talib.get_function_groups()["Pattern Recognition"]
        results: Dict[str, np.ndarray] = {}

        for func_name in pattern_funcs:
            func = getattr(talib, func_name)
            output = func(open_arr, high_arr, low_arr, close_arr)
            # Only include patterns with at least one non-zero signal
            if np.any(output != 0):
                results[func_name] = output

        self._scan_cache = results
        return results

    # ------------------------------------------------------------------
    # Structured detection for a specific bar
    # ------------------------------------------------------------------

    def get_detected_patterns(
        self, bar_index: int = -1
    ) -> List[Dict[str, Any]]:
        """Get detected patterns at a specific bar index.

        Args:
            bar_index: Which bar to inspect (default -1 = last bar).

        Returns:
            List of dicts, each with: ``name`` (human-readable),
            ``talib_name``, ``signal`` (raw TA-Lib value),
            ``direction`` (``'bullish'``/``'bearish'``),
            ``priority`` (``'high'``/``'medium'``/``'low'``),
            ``candle_count`` (1/2/3).
        """
        scan = self.scan_all()
        detected: List[Dict[str, Any]] = []

        for func_name, output in scan.items():
            val = int(output[bar_index])
            if val == 0:
                continue

            direction = "bullish" if val > 0 else "bearish"

            if func_name in self.HIGH_PRIORITY:
                priority = "high"
            elif func_name in self.MEDIUM_PRIORITY:
                priority = "medium"
            else:
                priority = "low"

            name = self.PATTERN_NAMES.get(func_name, func_name.replace("CDL", ""))
            candle_count = self.CANDLE_COUNT.get(func_name, 1)

            detected.append(
                {
                    "name": name,
                    "talib_name": func_name,
                    "signal": val,
                    "direction": direction,
                    "priority": priority,
                    "candle_count": candle_count,
                }
            )

        return detected

    # ------------------------------------------------------------------
    # Context-aware filtering
    # ------------------------------------------------------------------

    def get_context_filtered(
        self,
        indicators_result: Optional[Dict[str, Any]] = None,
        advanced_result: Optional[Dict[str, Any]] = None,
        bar_index: int = -1,
    ) -> List[Dict[str, Any]]:
        """Get context-filtered patterns with confidence scoring.

        Applies trend, support/resistance proximity, volume, and ADX
        regime checks to each detected pattern and returns only those
        with a weighted confidence score >= 0.4.

        Args:
            indicators_result: Output from ``Indicators.compute_all()``.
            advanced_result: Output from ``AdvancedIndicators.compute_all()``.
            bar_index: Which bar to check (default -1 = last bar).

        Returns:
            List of dicts extending :meth:`get_detected_patterns` format
            with additional keys: ``confidence`` (float 0-1),
            ``context`` (dict with individual scores), and
            ``fibonacci_level`` (nearest level name if within 1%).
        """
        detected = self.get_detected_patterns(bar_index=bar_index)
        if not detected:
            return []

        filtered: List[Dict[str, Any]] = []

        for pattern in detected:
            scores: Dict[str, float] = {}
            weights: Dict[str, float] = {}

            # 1. Trend context (weight 0.3)
            if indicators_result and "ema200_trend" in indicators_result:
                trend_info = indicators_result["ema200_trend"]
                # Support both compute_all() format (nested dict) and
                # direct mock format
                trend_dir = trend_info.get("direction", "neutral")
                scores["trend_score"] = self._score_trend(
                    pattern["direction"], trend_dir
                )
                weights["trend_score"] = 0.3
            # Also handle flat mock format: {'ema_200': {'direction': ...}}
            elif indicators_result and "ema_200" in indicators_result:
                trend_dir = indicators_result["ema_200"].get("direction", "neutral")
                scores["trend_score"] = self._score_trend(
                    pattern["direction"], trend_dir
                )
                weights["trend_score"] = 0.3

            # 2. Support/Resistance proximity (weight 0.35)
            fib_level_name: Optional[str] = None
            if advanced_result and "fibonacci" in advanced_result:
                fib_info = advanced_result["fibonacci"]
                nearest = fib_info.get("nearest_level", {})
                dist_pct = nearest.get("distance_pct", 999.0)
                level_val = nearest.get("level")

                if dist_pct < 0.3:
                    scores["sr_score"] = 1.0
                elif dist_pct < 0.5:
                    scores["sr_score"] = 0.7
                elif dist_pct < 1.0:
                    scores["sr_score"] = 0.4
                else:
                    scores["sr_score"] = 0.1
                weights["sr_score"] = 0.35

                # Track nearest fib level name if within 1%
                if dist_pct < 1.0:
                    # Handle both real format (level=0.618) and name field
                    level_name = nearest.get("name")
                    if level_name is not None:
                        fib_level_name = str(level_name)
                    elif level_val is not None:
                        if isinstance(level_val, (int, float)):
                            fib_level_name = f"{level_val * 100:.1f}%"
                        else:
                            fib_level_name = str(level_val)

            # 3. Volume confirmation (weight 0.2)
            if indicators_result and "volume_sma" in indicators_result:
                vol_info = indicators_result["volume_sma"]
                vol_ratio = vol_info.get("ratio", 0.5)
                if vol_ratio > 1.2:
                    scores["volume_score"] = 1.0
                elif vol_ratio > 1.0:
                    scores["volume_score"] = 0.7
                elif vol_ratio > 0.8:
                    scores["volume_score"] = 0.4
                else:
                    scores["volume_score"] = 0.2
                weights["volume_score"] = 0.2

            # 4. ADX regime bonus (weight 0.15)
            if advanced_result and "adx" in advanced_result:
                adx_info = advanced_result["adx"]
                regime = adx_info.get("regime", "mixed")
                scores["regime_score"] = self._score_regime(
                    pattern["direction"], pattern["name"], regime
                )
                weights["regime_score"] = 0.15

            # Calculate final confidence
            if not weights:
                # No context available -- pass through with base confidence
                confidence = 0.5
            else:
                total_weight = sum(weights.values())
                confidence = sum(
                    scores[k] * (weights[k] / total_weight) for k in scores
                )

            if confidence >= 0.4:
                result = dict(pattern)
                result["confidence"] = round(confidence, 4)
                result["context"] = scores
                result["fibonacci_level"] = fib_level_name
                filtered.append(result)

        return filtered

    # ------------------------------------------------------------------
    # Lookback configuration
    # ------------------------------------------------------------------

    def _get_lookback_config(self) -> Dict[str, Dict[str, Any]]:
        """Return lookback window recommendations per pattern type.

        Returns:
            Dict with keys ``single_candle``, ``two_candle``,
            ``three_candle``, each mapping to ``trend_bars`` (int)
            and ``description`` (str).
        """
        return {
            "single_candle": {
                "trend_bars": 15,
                "description": "10-20 bars trend context",
            },
            "two_candle": {
                "trend_bars": 30,
                "description": "20-50 bars lookback",
            },
            "three_candle": {
                "trend_bars": 40,
                "description": "20-50 bars lookback",
            },
        }

    # ------------------------------------------------------------------
    # Internal scoring helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _score_trend(pattern_direction: str, trend_direction: str) -> float:
        """Score trend context for a pattern.

        Reversal patterns (bullish in bearish trend, bearish in bullish
        trend) score highest.  Continuation (same direction) scores
        moderately.  Contradictory (bullish pattern in strong bullish
        trend with no reversal expected) scores lowest.

        Args:
            pattern_direction: ``'bullish'`` or ``'bearish'``.
            trend_direction: ``'bullish'``, ``'bearish'``, or ``'neutral'``.

        Returns:
            Float score between 0.0 and 1.0.
        """
        if trend_direction == "neutral":
            return 0.5

        # Reversal: bullish pattern in bearish trend (or vice versa)
        if pattern_direction != trend_direction:
            return 1.0

        # Continuation: same direction (e.g., bullish pattern in bullish trend)
        # Still valid as continuation signal but lower score
        return 0.3

    @staticmethod
    def _score_regime(
        pattern_direction: str, pattern_name: str, regime: str
    ) -> float:
        """Score ADX regime context for a pattern.

        Reversal-type patterns (Hammer, Engulfing, Morning/Evening Star,
        Doji patterns) score higher in ranging markets.  Continuation
        patterns (Three Soldiers, Three Crows) score higher in trending.

        Args:
            pattern_direction: ``'bullish'`` or ``'bearish'``.
            pattern_name: Human-readable pattern name.
            regime: ``'trending'``, ``'ranging'``, or ``'mixed'``.

        Returns:
            Float score between 0.0 and 1.0.
        """
        # Classify patterns as reversal or continuation
        reversal_keywords = [
            "Hammer", "Engulfing", "Star", "Doji", "Inverted",
            "Hanging", "Shooting", "Piercing", "Dark Cloud",
            "Harami", "Abandoned", "Counterattack",
        ]
        is_reversal = any(kw in pattern_name for kw in reversal_keywords)

        if regime == "ranging":
            return 1.0 if is_reversal else 0.3
        elif regime == "trending":
            return 0.3 if is_reversal else 1.0
        else:  # mixed
            return 0.5


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

def detect_patterns(
    candles: List[Dict[str, Any]],
    bar_index: int = -1,
) -> Dict[str, Any]:
    """Detect candlestick patterns on raw Oanda candle data.

    Convenience wrapper around :class:`CandlestickPatterns` that returns
    a dict keyed by TA-Lib function name with the raw output arrays for
    patterns that fired, plus a ``filtered_patterns`` list of structured
    pattern dicts for the specified bar.

    Args:
        candles: List of Oanda candle dicts.
        bar_index: Which bar to inspect (default -1 = last bar).

    Returns:
        Dict with ``scan_results`` (raw TA-Lib arrays), ``detected``
        (structured list for *bar_index*), and ``count`` (int).
    """
    cp = CandlestickPatterns(candles)
    scan = cp.scan_all()
    detected = cp.get_detected_patterns(bar_index=bar_index)
    return {
        "scan_results": {k: v.tolist() for k, v in scan.items()},
        "detected": detected,
        "filtered_patterns": detected,
        "count": len(detected),
    }
