"""
Pattern integration layer combining candlestick and chart pattern results.

Provides a unified interface for the downstream strategy engine (Phase 5)
to consume all pattern signals, with Fibonacci-level confluence flagging
to identify the highest-quality trade setups.

Primary entry points:
- :meth:`PatternIntegration.scan` -- unified pattern scan with Fibonacci
  confluence scoring
- :meth:`PatternIntegration.get_trade_signals` -- top signals sorted by
  confidence for direct consumption by the strategy engine

Usage:
    from trading_bot.source.indicators import Indicators
    from trading_bot.source.indicators_advanced import AdvancedIndicators
    from trading_bot.source.pattern_integration import PatternIntegration

    candles = pipeline.fetch_candles("EUR_USD", "H1", count=250)
    ind = Indicators(candles).compute_all()
    adv = AdvancedIndicators(candles).compute_all()
    pi = PatternIntegration(candles, indicators_result=ind, advanced_result=adv)
    result = pi.scan()
    signals = pi.get_trade_signals()
"""

from typing import Any, Dict, List, Optional

# Import pattern engines with fallback for standalone usage
try:
    from .candlestick_patterns import CandlestickPatterns
except (ImportError, SystemError):
    from candlestick_patterns import CandlestickPatterns  # type: ignore[no-redef]

try:
    from .chart_patterns import ChartPatterns
except (ImportError, SystemError):
    from chart_patterns import ChartPatterns  # type: ignore[no-redef]


class PatternIntegration:
    """Unified pattern integration with Fibonacci confluence scoring.

    Combines results from :class:`CandlestickPatterns` and
    :class:`ChartPatterns` into a single output, applying Fibonacci
    retracement-level proximity scoring to flag high-confluence setups.

    Args:
        candles: List of Oanda candle dicts (mid price component).
        indicators_result: Optional output from
            ``Indicators.compute_all()`` (core indicators).
        advanced_result: Optional output from
            ``AdvancedIndicators.compute_all()`` (ADX, Fibonacci, etc.).
    """

    # Fibonacci proximity thresholds
    CANDLESTICK_FIB_TOLERANCE_PCT = 0.3  # 0.3% for candlestick patterns
    CHART_FIB_TOLERANCE_PCT = 0.5  # 0.5% for chart pattern key levels
    FIB_CONFIDENCE_BOOST = 0.15  # confidence boost for Fibonacci confluence

    def __init__(
        self,
        candles: Optional[List[Dict[str, Any]]] = None,
        indicators_result: Optional[Dict[str, Any]] = None,
        advanced_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._candles = candles
        self._indicators_result = indicators_result
        self._advanced_result = advanced_result
        self._candlestick_engine = CandlestickPatterns(candles) if candles else None
        self._chart_engine = ChartPatterns(candles) if candles else None

    # ------------------------------------------------------------------
    # Class method constructor
    # ------------------------------------------------------------------

    @classmethod
    def from_candles(
        cls,
        candles: List[Dict[str, Any]],
        indicators_result: Optional[Dict[str, Any]] = None,
        advanced_result: Optional[Dict[str, Any]] = None,
    ) -> "PatternIntegration":
        """Convenience constructor from raw candle data.

        Allows one-liner usage::

            result = PatternIntegration.from_candles(candles).scan()

        Args:
            candles: List of Oanda candle dicts.
            indicators_result: Optional core indicator output.
            advanced_result: Optional advanced indicator output.

        Returns:
            A new :class:`PatternIntegration` instance.
        """
        return cls(
            candles,
            indicators_result=indicators_result,
            advanced_result=advanced_result,
        )

    # ------------------------------------------------------------------
    # Primary scan method
    # ------------------------------------------------------------------

    def scan(self) -> Dict[str, Any]:
        """Run unified pattern scan with Fibonacci confluence scoring.

        1. Detects candlestick patterns (context-filtered if indicators
           are available).
        2. Detects chart patterns via ``scan_all()``.
        3. Applies Fibonacci confluence scoring to both pattern sets.
        4. Collects high-confluence patterns and builds a summary.

        Returns:
            Dict with keys:

            - ``candlestick_patterns``: List of candlestick pattern dicts.
            - ``chart_patterns``: Dict with ``reversals``,
              ``continuations``, ``confirmed``, ``unconfirmed`` lists.
            - ``high_confluence``: List of all patterns near Fibonacci
              levels.
            - ``summary``: Dict with ``total_candlestick``,
              ``total_chart``, ``high_confluence_count``,
              ``dominant_direction``, ``strongest_signal``.
        """
        # 1. Get candlestick patterns
        candlestick_patterns = self._candlestick_engine.get_context_filtered(
            indicators_result=self._indicators_result,
            advanced_result=self._advanced_result,
        )

        # 2. Get chart patterns
        volume_sma_ratio = self._extract_volume_sma_ratio()
        chart_result = self._chart_engine.scan_all(
            volume_sma_ratio=volume_sma_ratio,
        )

        # 3. Extract Fibonacci levels for confluence scoring
        fib_levels = self._extract_fibonacci_levels()

        # 4. Apply Fibonacci confluence to candlestick patterns
        high_confluence: List[Dict[str, Any]] = []
        if fib_levels:
            for pattern in candlestick_patterns:
                self._apply_candlestick_fib_confluence(pattern, fib_levels)
                if pattern.get("fibonacci_confluence"):
                    high_confluence.append(pattern)

            # Apply Fibonacci confluence to chart patterns
            all_chart_patterns = (
                chart_result.get("reversals", [])
                + chart_result.get("continuations", [])
            )
            for pattern in all_chart_patterns:
                self._apply_chart_fib_confluence(pattern, fib_levels)
                if pattern.get("fibonacci_confluence"):
                    high_confluence.append(pattern)

        # 5. Build summary
        total_chart = len(
            chart_result.get("reversals", [])
        ) + len(
            chart_result.get("continuations", [])
        )
        summary = self._build_summary(
            candlestick_patterns, total_chart, high_confluence
        )

        return {
            "candlestick_patterns": candlestick_patterns,
            "chart_patterns": chart_result,
            "high_confluence": high_confluence,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Trade signals interface for Phase 5
    # ------------------------------------------------------------------

    def get_trade_signals(self) -> List[Dict[str, Any]]:
        """Get top pattern signals sorted by confidence.

        Returns the top 5 patterns across both candlestick and chart
        detection engines, normalised to a common structure for
        direct consumption by the strategy engine.

        Returns:
            List of up to 5 dicts, each with:

            - ``pattern_name``: Human-readable pattern name.
            - ``pattern_type``: ``'candlestick'`` or ``'chart'``.
            - ``direction``: ``'bullish'`` or ``'bearish'``.
            - ``confidence``: Float 0-1.
            - ``fibonacci_confluence``: Bool.
            - ``target``: Float price target or ``None``.
            - ``confirmed``: Bool.
        """
        result = self.scan()
        signals: List[Dict[str, Any]] = []

        # Collect candlestick signals
        for p in result["candlestick_patterns"]:
            signals.append(
                {
                    "pattern_name": p.get("name", "Unknown"),
                    "pattern_type": "candlestick",
                    "direction": p.get("direction", "unknown"),
                    "confidence": p.get("confidence", 0.0),
                    "fibonacci_confluence": p.get(
                        "fibonacci_confluence", False
                    ),
                    "target": None,
                    "confirmed": True,  # passed context filter
                }
            )

        # Collect chart pattern signals
        chart_patterns = result["chart_patterns"]
        all_chart = chart_patterns.get(
            "reversals", []
        ) + chart_patterns.get("continuations", [])
        for p in all_chart:
            target = None
            key_levels = p.get("key_levels", {})
            if key_levels:
                target = key_levels.get("target")
            signals.append(
                {
                    "pattern_name": p.get("type", "unknown"),
                    "pattern_type": "chart",
                    "direction": p.get("direction", "unknown"),
                    "confidence": p.get("confidence", 0.0),
                    "fibonacci_confluence": p.get(
                        "fibonacci_confluence", False
                    ),
                    "target": target,
                    "confirmed": p.get("confirmed", False),
                }
            )

        # Sort by confidence descending, fibonacci confluence first
        signals.sort(
            key=lambda s: (
                s["fibonacci_confluence"],
                s["confidence"],
            ),
            reverse=True,
        )

        return signals[:5]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_volume_sma_ratio(self) -> Optional[float]:
        """Extract volume SMA ratio from indicators result.

        Checks both core indicators and advanced indicators for volume
        SMA data.

        Returns:
            Volume SMA ratio as float, or None if not available.
        """
        # Check core indicators first
        if self._indicators_result and "volume_sma" in self._indicators_result:
            return self._indicators_result["volume_sma"].get("ratio")

        # Check advanced indicators
        if self._advanced_result and "volume_sma" in self._advanced_result:
            return self._advanced_result["volume_sma"].get("ratio")

        return None

    def _extract_fibonacci_levels(self) -> Optional[Dict[float, float]]:
        """Extract Fibonacci retracement levels from advanced result.

        Returns:
            Dict mapping level ratios (e.g. 0.382) to prices, or None
            if Fibonacci data is not available.
        """
        if not self._advanced_result:
            return None

        fib_data = self._advanced_result.get("fibonacci")
        if not fib_data:
            return None

        # Try 'levels' key first (used in some formats)
        levels = fib_data.get("levels")
        if levels and isinstance(levels, dict):
            return {float(k): float(v) for k, v in levels.items()}

        # Try 'retracement_levels' key (from AdvancedIndicators.compute_fibonacci)
        ret_levels = fib_data.get("retracement_levels")
        if ret_levels and isinstance(ret_levels, dict):
            return {float(k): float(v) for k, v in ret_levels.items()}

        return None

    def _apply_candlestick_fib_confluence(
        self,
        pattern: Dict[str, Any],
        fib_levels: Dict[float, float],
    ) -> None:
        """Apply Fibonacci confluence scoring to a candlestick pattern.

        Checks if the pattern's price is within
        :attr:`CANDLESTICK_FIB_TOLERANCE_PCT` of any Fibonacci level.
        If so, sets ``fibonacci_confluence=True``, boosts confidence by
        :attr:`FIB_CONFIDENCE_BOOST` (capped at 1.0), and adds a
        ``fibonacci_level`` field.

        Args:
            pattern: Candlestick pattern dict (mutated in place).
            fib_levels: Dict mapping Fibonacci ratios to price levels.
        """
        # Use the pattern's close price context. For candlestick patterns,
        # the relevant price is the close of the last bar in the DataFrame.
        df = self._candlestick_engine.df
        if df.empty:
            return
        current_price = float(df["close"].iloc[-1])

        for level_ratio, level_price in fib_levels.items():
            if level_price == 0:
                continue
            distance_pct = abs(current_price - level_price) / level_price * 100
            if distance_pct <= self.CANDLESTICK_FIB_TOLERANCE_PCT:
                pattern["fibonacci_confluence"] = True
                pattern["fibonacci_level"] = f"{level_ratio * 100:.1f}%"
                # Boost confidence
                old_conf = pattern.get("confidence", 0.5)
                pattern["confidence"] = min(
                    1.0, old_conf + self.FIB_CONFIDENCE_BOOST
                )
                return

        # No Fibonacci proximity found
        if "fibonacci_confluence" not in pattern:
            pattern["fibonacci_confluence"] = False

    def _apply_chart_fib_confluence(
        self,
        pattern: Dict[str, Any],
        fib_levels: Dict[float, float],
    ) -> None:
        """Apply Fibonacci confluence scoring to a chart pattern.

        Checks if any key level (neckline, support, resistance) is
        within :attr:`CHART_FIB_TOLERANCE_PCT` of a Fibonacci level.
        If so, sets ``fibonacci_confluence=True`` and adds a
        ``fibonacci_level`` field.

        Args:
            pattern: Chart pattern dict (mutated in place).
            fib_levels: Dict mapping Fibonacci ratios to price levels.
        """
        key_levels = pattern.get("key_levels", {})
        if not key_levels:
            pattern["fibonacci_confluence"] = False
            return

        for level_name, level_price_val in key_levels.items():
            if not isinstance(level_price_val, (int, float)):
                continue
            for fib_ratio, fib_price in fib_levels.items():
                if fib_price == 0:
                    continue
                distance_pct = (
                    abs(level_price_val - fib_price) / fib_price * 100
                )
                if distance_pct <= self.CHART_FIB_TOLERANCE_PCT:
                    pattern["fibonacci_confluence"] = True
                    pattern["fibonacci_level"] = f"{fib_ratio * 100:.1f}%"
                    return

        if "fibonacci_confluence" not in pattern:
            pattern["fibonacci_confluence"] = False

    # ------------------------------------------------------------------
    # Legacy convenience method for tests
    # ------------------------------------------------------------------

    @staticmethod
    def combine_patterns(
        candle_patterns: Dict[str, Any],
        chart_patterns: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Combine pre-computed candle and chart pattern dicts.

        Convenience method for downstream consumers that already have
        pattern results from separate detection calls.

        Args:
            candle_patterns: Output from candlestick pattern detection.
            chart_patterns: Output from chart pattern detection.

        Returns:
            Merged dict with ``candlestick_patterns``, ``chart_patterns``,
            and a ``summary``.
        """
        candlestick_count = sum(
            len(v) if isinstance(v, list) else 1
            for v in candle_patterns.values()
        ) if isinstance(candle_patterns, dict) else 0

        chart_count = sum(
            len(v) if isinstance(v, list) else 1
            for v in chart_patterns.values()
        ) if isinstance(chart_patterns, dict) else 0

        return {
            "candlestick_patterns": candle_patterns,
            "chart_patterns": chart_patterns,
            "summary": {
                "total_candlestick": candlestick_count,
                "total_chart": chart_count,
                "high_confluence_count": 0,
                "dominant_direction": "mixed",
                "strongest_signal": None,
            },
        }

    def _build_summary(
        self,
        candlestick_patterns: List[Dict[str, Any]],
        total_chart: int,
        high_confluence: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build a summary dict from scan results.

        Args:
            candlestick_patterns: Filtered candlestick pattern list.
            total_chart: Total number of chart patterns detected.
            high_confluence: List of patterns with Fibonacci confluence.

        Returns:
            Dict with ``total_candlestick``, ``total_chart``,
            ``high_confluence_count``, ``dominant_direction``, and
            ``strongest_signal``.
        """
        total_candlestick = len(candlestick_patterns)

        # Count directions
        bullish_count = 0
        bearish_count = 0
        strongest: Optional[Dict[str, Any]] = None
        highest_conf = -1.0

        for p in candlestick_patterns:
            direction = p.get("direction", "")
            if direction == "bullish":
                bullish_count += 1
            elif direction == "bearish":
                bearish_count += 1
            conf = p.get("confidence", 0.0)
            if conf > highest_conf:
                highest_conf = conf
                strongest = p

        # Include chart patterns in direction counts
        for p in high_confluence:
            # Skip candlestick patterns already counted
            if p.get("talib_name"):
                continue
            direction = p.get("direction", "")
            if direction == "bullish":
                bullish_count += 1
            elif direction == "bearish":
                bearish_count += 1

        if bullish_count > bearish_count:
            dominant_direction = "bullish"
        elif bearish_count > bullish_count:
            dominant_direction = "bearish"
        else:
            dominant_direction = "mixed"

        return {
            "total_candlestick": total_candlestick,
            "total_chart": total_chart,
            "high_confluence_count": len(high_confluence),
            "dominant_direction": dominant_direction,
            "strongest_signal": strongest,
        }
