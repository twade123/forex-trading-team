"""
Chart pattern recognition engine for the trading bot.

Detects reversal and continuation chart patterns (double top/bottom,
head & shoulders, flags, triangles, cup & handle) using scipy for
geometric swing detection, with breakout confirmation and price-target
projection.

The primary entry point is :meth:`ChartPatterns.scan_all`, which
returns a categorised dict of all detected patterns with confirmation
status for downstream consumers (confluence scoring, trade setup
generation).

Usage:
    from trading_bot.source.candle_pipeline import CandlePipeline
    from trading_bot.source.chart_patterns import ChartPatterns

    candles = pipeline.fetch_candles("EUR_USD", "H1", count=250)
    cp = ChartPatterns(candles)
    results = cp.scan_all(volume_sma_ratio=1.2)
"""

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from scipy.signal import argrelextrema

try:
    from .indicators import Indicators

    def _candles_to_dataframe(candles):
        return Indicators.candles_to_dataframe(candles)

except (ImportError, SystemError):
    # Fallback for direct execution or parallel runs
    import pandas as pd

    def _candles_to_dataframe(candles):
        """Minimal candle-to-DataFrame converter (fallback)."""
        complete = [c for c in candles if c.get("complete", False)]
        if not complete:
            raise ValueError(
                f"No complete candles in input data (total: {len(candles)})"
            )
        rows = []
        for idx, c in enumerate(complete):
            mid = c["mid"]
            try:
                ts = pd.Timestamp(c["time"])
            except (ValueError, TypeError):
                ts = pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(
                    minutes=idx
                )
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
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        df = df.set_index("time")
        return df


class ChartPatterns:
    """Chart-pattern recognition engine operating on Oanda candle data.

    Converts raw candle dicts to a pandas DataFrame, extracts numpy
    arrays for close, high, low and volume, and provides methods for
    detecting reversal patterns, continuation patterns, breakout
    confirmation and price-target projection.

    Args:
        candles: List of Oanda candle dicts.  Each dict must contain
            ``time``, ``mid`` (with o/h/l/c strings), ``volume``,
            and ``complete`` fields.
        lookback: Maximum number of recent bars to consider for
            pattern detection (default 200).
    """

    def __init__(
        self, candles: List[Dict[str, Any]], lookback: int = 200
    ):
        self.df = _candles_to_dataframe(candles)
        # Trim to lookback window
        if len(self.df) > lookback:
            self.df = self.df.iloc[-lookback:]
        self.close = self.df["close"].values.astype(float)
        self.high = self.df["high"].values.astype(float)
        self.low = self.df["low"].values.astype(float)
        self.volume = self.df["volume"].values.astype(float)

    # ------------------------------------------------------------------
    # Swing-point detection
    # ------------------------------------------------------------------

    def _find_swing_points(
        self, order: int = 5
    ) -> Dict[str, List[Tuple[int, float]]]:
        """Detect local swing highs and lows using ``scipy.signal.argrelextrema``.

        Args:
            order: Number of bars on each side required for a point to
                qualify as a local extremum (default 5).

        Returns:
            Dict with ``highs`` and ``lows`` keys, each containing a
            list of ``(index, price)`` tuples sorted by index.
        """
        high_indices = argrelextrema(self.high, np.greater, order=order)[0]
        low_indices = argrelextrema(self.low, np.less, order=order)[0]

        highs = sorted(
            [(int(i), float(self.high[i])) for i in high_indices],
            key=lambda x: x[0],
        )
        lows = sorted(
            [(int(i), float(self.low[i])) for i in low_indices],
            key=lambda x: x[0],
        )
        return {"highs": highs, "lows": lows}

    # ------------------------------------------------------------------
    # Reversal patterns
    # ------------------------------------------------------------------

    def detect_double_bottom(
        self, tolerance: float = 0.02
    ) -> Optional[Dict[str, Any]]:
        """Detect a double-bottom reversal pattern.

        Two swing lows within *tolerance* percent of each other,
        separated by at least 10 bars.  Neckline is the highest
        high between the two lows.

        Returns:
            Pattern dict or ``None``.
        """
        swings = self._find_swing_points()
        lows = swings["lows"]
        if len(lows) < 2:
            return None

        # Search from most recent pair backwards
        for i in range(len(lows) - 1, 0, -1):
            idx2, price2 = lows[i]
            idx1, price1 = lows[i - 1]
            if abs(idx2 - idx1) < 10:
                continue
            if abs(price1 - price2) / price1 > tolerance:
                continue

            # Neckline = highest high between the two lows
            between = self.high[idx1:idx2 + 1]
            neckline = float(np.max(between))
            avg_low = (price1 + price2) / 2.0
            target = neckline + (neckline - avg_low)

            return {
                "type": "double_bottom",
                "direction": "bullish",
                "low1": (idx1, price1),
                "low2": (idx2, price2),
                "neckline": neckline,
                "target": target,
                "confirmed": False,
                "confidence": 0.0,
                "key_levels": {
                    "support": avg_low,
                    "neckline": neckline,
                    "target": target,
                },
            }
        return None

    def detect_double_top(
        self, tolerance: float = 0.02
    ) -> Optional[Dict[str, Any]]:
        """Detect a double-top reversal pattern.

        Mirror of double bottom using swing highs.

        Returns:
            Pattern dict or ``None``.
        """
        swings = self._find_swing_points()
        highs = swings["highs"]
        if len(highs) < 2:
            return None

        for i in range(len(highs) - 1, 0, -1):
            idx2, price2 = highs[i]
            idx1, price1 = highs[i - 1]
            if abs(idx2 - idx1) < 10:
                continue
            if abs(price1 - price2) / price1 > tolerance:
                continue

            between = self.low[idx1:idx2 + 1]
            neckline = float(np.min(between))
            avg_high = (price1 + price2) / 2.0
            target = neckline - (avg_high - neckline)

            return {
                "type": "double_top",
                "direction": "bearish",
                "high1": (idx1, price1),
                "high2": (idx2, price2),
                "neckline": neckline,
                "target": target,
                "confirmed": False,
                "confidence": 0.0,
                "key_levels": {
                    "resistance": avg_high,
                    "neckline": neckline,
                    "target": target,
                },
            }
        return None

    def detect_triple_bottom(
        self, tolerance: float = 0.02
    ) -> Optional[Dict[str, Any]]:
        """Detect a triple-bottom reversal pattern.

        Three swing lows within *tolerance* percent, each separated
        by at least 8 bars.

        Returns:
            Pattern dict or ``None``.
        """
        swings = self._find_swing_points()
        lows = swings["lows"]
        if len(lows) < 3:
            return None

        for i in range(len(lows) - 1, 1, -1):
            idx3, price3 = lows[i]
            idx2, price2 = lows[i - 1]
            idx1, price1 = lows[i - 2]

            if abs(idx2 - idx1) < 8 or abs(idx3 - idx2) < 8:
                continue

            avg_price = (price1 + price2 + price3) / 3.0
            if any(
                abs(p - avg_price) / avg_price > tolerance
                for p in (price1, price2, price3)
            ):
                continue

            between = self.high[idx1:idx3 + 1]
            neckline = float(np.max(between))
            target = neckline + (neckline - avg_price)

            return {
                "type": "triple_bottom",
                "direction": "bullish",
                "low1": (idx1, price1),
                "low2": (idx2, price2),
                "low3": (idx3, price3),
                "neckline": neckline,
                "target": target,
                "confirmed": False,
                "confidence": 0.0,
                "key_levels": {
                    "support": avg_price,
                    "neckline": neckline,
                    "target": target,
                },
            }
        return None

    def detect_triple_top(
        self, tolerance: float = 0.02
    ) -> Optional[Dict[str, Any]]:
        """Detect a triple-top reversal pattern.

        Mirror of triple bottom using swing highs.

        Returns:
            Pattern dict or ``None``.
        """
        swings = self._find_swing_points()
        highs = swings["highs"]
        if len(highs) < 3:
            return None

        for i in range(len(highs) - 1, 1, -1):
            idx3, price3 = highs[i]
            idx2, price2 = highs[i - 1]
            idx1, price1 = highs[i - 2]

            if abs(idx2 - idx1) < 8 or abs(idx3 - idx2) < 8:
                continue

            avg_price = (price1 + price2 + price3) / 3.0
            if any(
                abs(p - avg_price) / avg_price > tolerance
                for p in (price1, price2, price3)
            ):
                continue

            between = self.low[idx1:idx3 + 1]
            neckline = float(np.min(between))
            target = neckline - (avg_price - neckline)

            return {
                "type": "triple_top",
                "direction": "bearish",
                "high1": (idx1, price1),
                "high2": (idx2, price2),
                "high3": (idx3, price3),
                "neckline": neckline,
                "target": target,
                "confirmed": False,
                "confidence": 0.0,
                "key_levels": {
                    "resistance": avg_price,
                    "neckline": neckline,
                    "target": target,
                },
            }
        return None

    def detect_head_and_shoulders(
        self, tolerance: float = 0.02
    ) -> Optional[Dict[str, Any]]:
        """Detect a head-and-shoulders reversal pattern (bearish).

        Three consecutive swing highs where the middle (head) is
        higher than both sides (shoulders), and the shoulders are
        within *tolerance* of each other.

        Returns:
            Pattern dict or ``None``.
        """
        swings = self._find_swing_points()
        highs = swings["highs"]
        if len(highs) < 3:
            return None

        for i in range(len(highs) - 1, 1, -1):
            idx3, right_shoulder = highs[i]
            idx2, head = highs[i - 1]
            idx1, left_shoulder = highs[i - 2]

            # Head must be higher than both shoulders
            if head <= left_shoulder or head <= right_shoulder:
                continue

            # Shoulders within tolerance
            if abs(left_shoulder - right_shoulder) / left_shoulder > tolerance:
                continue

            # Neckline = average of lows between the three highs
            low_between_1_2 = float(np.min(self.low[idx1:idx2 + 1]))
            low_between_2_3 = float(np.min(self.low[idx2:idx3 + 1]))
            neckline = (low_between_1_2 + low_between_2_3) / 2.0
            target = neckline - (head - neckline)

            return {
                "type": "head_and_shoulders",
                "direction": "bearish",
                "left_shoulder": (idx1, left_shoulder),
                "head": (idx2, head),
                "right_shoulder": (idx3, right_shoulder),
                "neckline": neckline,
                "target": target,
                "confirmed": False,
                "confidence": 0.0,
                "key_levels": {
                    "resistance": head,
                    "neckline": neckline,
                    "target": target,
                },
            }
        return None

    def detect_inverse_head_and_shoulders(
        self, tolerance: float = 0.02
    ) -> Optional[Dict[str, Any]]:
        """Detect an inverse head-and-shoulders reversal pattern (bullish).

        Mirror of H&S using swing lows.

        Returns:
            Pattern dict or ``None``.
        """
        swings = self._find_swing_points()
        lows = swings["lows"]
        if len(lows) < 3:
            return None

        for i in range(len(lows) - 1, 1, -1):
            idx3, right_shoulder = lows[i]
            idx2, head = lows[i - 1]
            idx1, left_shoulder = lows[i - 2]

            # Head must be lower than both shoulders
            if head >= left_shoulder or head >= right_shoulder:
                continue

            # Shoulders within tolerance
            if abs(left_shoulder - right_shoulder) / left_shoulder > tolerance:
                continue

            # Neckline = average of highs between the three lows
            high_between_1_2 = float(np.max(self.high[idx1:idx2 + 1]))
            high_between_2_3 = float(np.max(self.high[idx2:idx3 + 1]))
            neckline = (high_between_1_2 + high_between_2_3) / 2.0
            target = neckline + (neckline - head)

            return {
                "type": "inverse_head_and_shoulders",
                "direction": "bullish",
                "left_shoulder": (idx1, left_shoulder),
                "head": (idx2, head),
                "right_shoulder": (idx3, right_shoulder),
                "neckline": neckline,
                "target": target,
                "confirmed": False,
                "confidence": 0.0,
                "key_levels": {
                    "support": head,
                    "neckline": neckline,
                    "target": target,
                },
            }
        return None

    def detect_all_reversals(
        self, tolerance: float = 0.02
    ) -> List[Dict[str, Any]]:
        """Run all reversal pattern detectors and return found patterns.

        Returns:
            List of pattern dicts (may be empty).
        """
        patterns: List[Dict[str, Any]] = []
        detectors = [
            self.detect_double_bottom,
            self.detect_double_top,
            self.detect_triple_bottom,
            self.detect_triple_top,
            self.detect_head_and_shoulders,
            self.detect_inverse_head_and_shoulders,
        ]
        for detector in detectors:
            result = detector(tolerance=tolerance)
            if result is not None:
                patterns.append(result)
        return patterns

    # ------------------------------------------------------------------
    # Continuation patterns
    # ------------------------------------------------------------------

    def detect_bull_flag(
        self, min_bars: int = 10, max_bars: int = 40
    ) -> Optional[Dict[str, Any]]:
        """Detect a bull-flag continuation pattern.

        A strong upward move (pole) followed by a slight downward
        consolidation channel (flag) that is narrow relative to the
        pole height.

        Returns:
            Pattern dict or ``None``.
        """
        n = len(self.close)
        if n < min_bars + 5:
            return None

        # Scan backwards for a pole: rapid >2% rise in <10 bars
        for pole_end in range(n - min_bars, max(9, n - max_bars), -1):
            for pole_start in range(
                max(0, pole_end - 10), pole_end - 2
            ):
                rise = (
                    self.close[pole_end] - self.close[pole_start]
                ) / self.close[pole_start]
                if rise < 0.02:
                    continue

                pole_height = self.close[pole_end] - self.close[pole_start]

                # Flag region: from pole_end to the latest bar
                flag_region = self.close[pole_end:]
                if len(flag_region) < 3:
                    continue

                # Flag should drift slightly down and be narrow
                flag_range = float(np.max(flag_region) - np.min(flag_region))
                flag_drift = flag_region[-1] - flag_region[0]

                if flag_range > 0.5 * pole_height:
                    continue  # Flag too wide
                if flag_drift > 0:
                    continue  # Flag should drift downward

                # Compute simple slopes for the channel
                x = np.arange(len(flag_region), dtype=float)
                upper_slope = 0.0
                lower_slope = 0.0
                if len(x) >= 2:
                    upper_fit = np.polyfit(
                        x,
                        self.high[pole_end: pole_end + len(flag_region)],
                        1,
                    )
                    lower_fit = np.polyfit(
                        x,
                        self.low[pole_end: pole_end + len(flag_region)],
                        1,
                    )
                    upper_slope = float(upper_fit[0])
                    lower_slope = float(lower_fit[0])

                target = self.close[pole_end] + pole_height
                flag_support = float(np.min(
                    self.low[pole_end: pole_end + len(flag_region)]
                ))

                return {
                    "type": "bull_flag",
                    "direction": "bullish",
                    "pole_start": (pole_start, float(self.close[pole_start])),
                    "pole_end": (pole_end, float(self.close[pole_end])),
                    "flag_channel": (upper_slope, lower_slope),
                    "target": target,
                    "confirmed": False,
                    "confidence": 0.0,
                    "key_levels": {
                        "pole_base": float(self.close[pole_start]),
                        "flag_support": flag_support,
                        "target": target,
                    },
                }
        return None

    def detect_bear_flag(
        self, min_bars: int = 10, max_bars: int = 40
    ) -> Optional[Dict[str, Any]]:
        """Detect a bear-flag continuation pattern.

        Mirror of bull flag: strong downward pole followed by slight
        upward consolidation.

        Returns:
            Pattern dict or ``None``.
        """
        n = len(self.close)
        if n < min_bars + 5:
            return None

        for pole_end in range(n - min_bars, max(9, n - max_bars), -1):
            for pole_start in range(
                max(0, pole_end - 10), pole_end - 2
            ):
                drop = (
                    self.close[pole_start] - self.close[pole_end]
                ) / self.close[pole_start]
                if drop < 0.02:
                    continue

                pole_height = self.close[pole_start] - self.close[pole_end]

                flag_region = self.close[pole_end:]
                if len(flag_region) < 3:
                    continue

                flag_range = float(np.max(flag_region) - np.min(flag_region))
                flag_drift = flag_region[-1] - flag_region[0]

                if flag_range > 0.5 * pole_height:
                    continue
                if flag_drift < 0:
                    continue  # Flag should drift upward (counter-trend)

                x = np.arange(len(flag_region), dtype=float)
                upper_slope = 0.0
                lower_slope = 0.0
                if len(x) >= 2:
                    upper_fit = np.polyfit(
                        x,
                        self.high[pole_end: pole_end + len(flag_region)],
                        1,
                    )
                    lower_fit = np.polyfit(
                        x,
                        self.low[pole_end: pole_end + len(flag_region)],
                        1,
                    )
                    upper_slope = float(upper_fit[0])
                    lower_slope = float(lower_fit[0])

                target = self.close[pole_end] - pole_height
                flag_resistance = float(np.max(
                    self.high[pole_end: pole_end + len(flag_region)]
                ))

                return {
                    "type": "bear_flag",
                    "direction": "bearish",
                    "pole_start": (pole_start, float(self.close[pole_start])),
                    "pole_end": (pole_end, float(self.close[pole_end])),
                    "flag_channel": (upper_slope, lower_slope),
                    "target": target,
                    "confirmed": False,
                    "confidence": 0.0,
                    "key_levels": {
                        "pole_base": float(self.close[pole_start]),
                        "flag_resistance": flag_resistance,
                        "target": target,
                    },
                }
        return None

    def detect_ascending_triangle(
        self, tolerance: float = 0.02
    ) -> Optional[Dict[str, Any]]:
        """Detect an ascending-triangle continuation pattern.

        Flat resistance (swing highs at approximately the same level)
        with ascending support (swing lows trending upward).

        Returns:
            Pattern dict or ``None``.
        """
        swings = self._find_swing_points()
        highs = swings["highs"]
        lows = swings["lows"]

        if len(highs) < 2 or len(lows) < 2:
            return None

        # Check flat resistance: last N highs within tolerance
        recent_highs = highs[-3:] if len(highs) >= 3 else highs[-2:]
        high_prices = [p for _, p in recent_highs]
        avg_resistance = np.mean(high_prices)

        if any(
            abs(p - avg_resistance) / avg_resistance > tolerance
            for p in high_prices
        ):
            return None

        # Check ascending support: lows trending upward
        recent_lows = lows[-3:] if len(lows) >= 3 else lows[-2:]
        low_prices = [p for _, p in recent_lows]

        if not all(
            low_prices[i] < low_prices[i + 1]
            for i in range(len(low_prices) - 1)
        ):
            return None

        resistance = float(avg_resistance)
        lowest_support = float(min(low_prices))
        target = resistance + (resistance - lowest_support)

        return {
            "type": "ascending_triangle",
            "direction": "bullish",
            "resistance": resistance,
            "support_lows": [(idx, p) for idx, p in recent_lows],
            "target": target,
            "confirmed": False,
            "confidence": 0.0,
            "key_levels": {
                "resistance": resistance,
                "support": float(low_prices[-1]),
                "target": target,
            },
        }

    def detect_descending_triangle(
        self, tolerance: float = 0.02
    ) -> Optional[Dict[str, Any]]:
        """Detect a descending-triangle continuation pattern.

        Flat support with descending resistance.

        Returns:
            Pattern dict or ``None``.
        """
        swings = self._find_swing_points()
        highs = swings["highs"]
        lows = swings["lows"]

        if len(highs) < 2 or len(lows) < 2:
            return None

        # Check flat support
        recent_lows = lows[-3:] if len(lows) >= 3 else lows[-2:]
        low_prices = [p for _, p in recent_lows]
        avg_support = np.mean(low_prices)

        if any(
            abs(p - avg_support) / avg_support > tolerance
            for p in low_prices
        ):
            return None

        # Check descending resistance
        recent_highs = highs[-3:] if len(highs) >= 3 else highs[-2:]
        high_prices = [p for _, p in recent_highs]

        if not all(
            high_prices[i] > high_prices[i + 1]
            for i in range(len(high_prices) - 1)
        ):
            return None

        support = float(avg_support)
        highest_resistance = float(max(high_prices))
        target = support - (highest_resistance - support)

        return {
            "type": "descending_triangle",
            "direction": "bearish",
            "support": support,
            "resistance_highs": [(idx, p) for idx, p in recent_highs],
            "target": target,
            "confirmed": False,
            "confidence": 0.0,
            "key_levels": {
                "support": support,
                "resistance": float(high_prices[-1]),
                "target": target,
            },
        }

    def detect_symmetrical_triangle(
        self, tolerance: float = 0.02
    ) -> Optional[Dict[str, Any]]:
        """Detect a symmetrical-triangle continuation pattern.

        Both support ascending and resistance descending, converging.

        Returns:
            Pattern dict or ``None``.
        """
        swings = self._find_swing_points()
        highs = swings["highs"]
        lows = swings["lows"]

        if len(highs) < 2 or len(lows) < 2:
            return None

        recent_highs = highs[-3:] if len(highs) >= 3 else highs[-2:]
        recent_lows = lows[-3:] if len(lows) >= 3 else lows[-2:]

        high_prices = [p for _, p in recent_highs]
        low_prices = [p for _, p in recent_lows]

        # Resistance descending
        resistance_descending = all(
            high_prices[i] > high_prices[i + 1]
            for i in range(len(high_prices) - 1)
        )

        # Support ascending
        support_ascending = all(
            low_prices[i] < low_prices[i + 1]
            for i in range(len(low_prices) - 1)
        )

        if not (resistance_descending and support_ascending):
            return None

        # Widest part of triangle
        widest = float(max(high_prices) - min(low_prices))
        # Breakout point approximated as current price
        breakout_point = float(self.close[-1])
        target = breakout_point + widest

        return {
            "type": "symmetrical_triangle",
            "direction": "neutral",
            "resistance_highs": [(idx, p) for idx, p in recent_highs],
            "support_lows": [(idx, p) for idx, p in recent_lows],
            "target": target,
            "confirmed": False,
            "confidence": 0.0,
            "key_levels": {
                "resistance": float(high_prices[-1]),
                "support": float(low_prices[-1]),
                "target": target,
            },
        }

    def detect_cup_and_handle(self) -> Optional[Dict[str, Any]]:
        """Detect a cup-and-handle continuation pattern.

        A rounded bottom (U-shape) where prices decline, flatten,
        then rise back to the prior level (cup), followed by a
        shallow pullback (handle).

        Returns:
            Pattern dict or ``None``.
        """
        n = len(self.close)
        if n < 30:
            return None

        # Look for a U-shape in the last 60-80% of the data
        search_start = max(0, n // 5)
        search_end = n - 5  # Leave room for handle

        best_cup = None
        best_score = 0.0

        for cup_start in range(search_start, min(search_start + n // 3, search_end - 20)):
            start_price = float(self.close[cup_start])

            # Find the cup bottom
            cup_search_end = min(cup_start + int(n * 0.6), search_end)
            if cup_search_end - cup_start < 15:
                continue

            segment = self.close[cup_start:cup_search_end]
            bottom_offset = int(np.argmin(segment))
            bottom_idx = cup_start + bottom_offset
            bottom_price = float(self.close[bottom_idx])

            # Cup depth: 12-33% of start price
            depth_pct = (start_price - bottom_price) / start_price
            if depth_pct < 0.005 or depth_pct > 0.33:
                continue

            # Find where price returns to cup rim level after bottom
            rim_found = False
            rim_idx = bottom_idx
            for j in range(bottom_idx + 1, min(cup_search_end, n)):
                if self.close[j] >= start_price * 0.98:
                    rim_found = True
                    rim_idx = j
                    break

            if not rim_found:
                continue

            # Handle: slight pullback after rim
            handle_region = self.close[rim_idx:]
            if len(handle_region) < 2:
                continue

            handle_depth = float(
                np.max(handle_region) - np.min(handle_region)
            )
            cup_depth = start_price - bottom_price

            if handle_depth > 0.5 * cup_depth:
                continue  # Handle too deep

            score = depth_pct * (rim_idx - cup_start)
            if score > best_score:
                best_score = score
                rim_level = float(start_price)
                target = rim_level + cup_depth

                best_cup = {
                    "type": "cup_and_handle",
                    "direction": "bullish",
                    "cup_start": (cup_start, float(self.close[cup_start])),
                    "cup_bottom": (bottom_idx, bottom_price),
                    "rim": (rim_idx, float(self.close[rim_idx])),
                    "target": target,
                    "confirmed": False,
                    "confidence": 0.0,
                    "key_levels": {
                        "rim": rim_level,
                        "cup_bottom": bottom_price,
                        "target": target,
                    },
                }

        return best_cup

    def detect_all_continuations(self) -> List[Dict[str, Any]]:
        """Run all continuation pattern detectors and return found patterns.

        Returns:
            List of pattern dicts (may be empty).
        """
        patterns: List[Dict[str, Any]] = []
        detectors = [
            lambda: self.detect_bull_flag(),
            lambda: self.detect_bear_flag(),
            lambda: self.detect_ascending_triangle(),
            lambda: self.detect_descending_triangle(),
            lambda: self.detect_symmetrical_triangle(),
            lambda: self.detect_cup_and_handle(),
        ]
        for detector in detectors:
            result = detector()
            if result is not None:
                patterns.append(result)
        return patterns

    # ------------------------------------------------------------------
    # Breakout confirmation
    # ------------------------------------------------------------------

    def confirm_patterns(
        self,
        patterns: List[Dict[str, Any]],
        volume_sma_ratio: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Check breakout confirmation for a list of patterns.

        For each pattern, verifies whether the current price has broken
        beyond the relevant key level (neckline for reversals,
        resistance/support for triangles, flag boundary for flags).
        If *volume_sma_ratio* is provided and > 1.0, volume
        confirmation is included.

        Args:
            patterns: List of pattern dicts from detection methods.
            volume_sma_ratio: Ratio of current volume to SMA volume.
                If > 1.0, adds volume confirmation.

        Returns:
            List of confirmed pattern dicts.
        """
        current_price = float(self.close[-1])
        confirmed: List[Dict[str, Any]] = []

        for pattern in patterns:
            levels = pattern.get("key_levels", {})
            ptype = pattern.get("type", "")
            direction = pattern.get("direction", "")
            breakout = False

            # Determine the key breakout level
            if "neckline" in levels:
                neckline = levels["neckline"]
                if direction == "bullish" and current_price > neckline:
                    breakout = True
                elif direction == "bearish" and current_price < neckline:
                    breakout = True
            elif "resistance" in levels and direction == "bullish":
                if current_price > levels["resistance"]:
                    breakout = True
            elif "support" in levels and direction == "bearish":
                if current_price < levels["support"]:
                    breakout = True
            elif "flag_support" in levels and direction == "bullish":
                # Bull flag: breakout above flag resistance
                flag_high = float(np.max(self.high[-10:])) if len(self.high) > 10 else float(np.max(self.high))
                if current_price > flag_high * 0.998:
                    breakout = True
            elif "flag_resistance" in levels and direction == "bearish":
                flag_low = float(np.min(self.low[-10:])) if len(self.low) > 10 else float(np.min(self.low))
                if current_price < flag_low * 1.002:
                    breakout = True

            if breakout:
                p = dict(pattern)
                p["confirmed"] = True
                p["breakout_bar"] = len(self.close) - 1

                # Calculate confidence score (0.0 - 1.0)
                conf_score = 0.5  # Base confidence for confirmed breakout
                if volume_sma_ratio is not None and volume_sma_ratio > 1.0:
                    p["volume_confirmed"] = True
                    conf_score += 0.2  # Volume confirmation boost
                    if volume_sma_ratio > 1.5:
                        conf_score += 0.1  # Strong volume
                else:
                    p["volume_confirmed"] = False
                    p["note"] = "no_volume_data"

                # Proximity boost: how close price is to breakout level
                levels = p.get("key_levels", {})
                neckline = levels.get("neckline", levels.get("resistance", levels.get("support")))
                if neckline and neckline > 0:
                    distance_pct = abs(current_price - neckline) / neckline
                    if distance_pct < 0.002:  # Very close to breakout
                        conf_score += 0.1
                    elif distance_pct < 0.005:
                        conf_score += 0.05

                # Pattern type boost for high-reliability patterns
                high_reliability = {"head_and_shoulders", "inverse_head_and_shoulders", 
                                   "double_bottom", "double_top", "cup_and_handle"}
                if p.get("type") in high_reliability:
                    conf_score += 0.1

                p["confidence"] = min(conf_score, 1.0)
                confirmed.append(p)

        return confirmed

    # ------------------------------------------------------------------
    # Full scan
    # ------------------------------------------------------------------

    def scan_all(
        self, volume_sma_ratio: Optional[float] = None
    ) -> Dict[str, Any]:
        """Run all pattern detection and return categorised results.

        Detects all reversal and continuation patterns, then checks
        breakout confirmation for each.

        Args:
            volume_sma_ratio: Ratio of current volume to SMA.

        Returns:
            Dict with ``reversals``, ``continuations``, ``confirmed``,
            and ``unconfirmed`` lists.
        """
        reversals = self.detect_all_reversals()
        continuations = self.detect_all_continuations()

        all_patterns = reversals + continuations
        confirmed = self.confirm_patterns(all_patterns, volume_sma_ratio)

        confirmed_types = {p["type"] for p in confirmed}
        unconfirmed = [
            p for p in all_patterns if p["type"] not in confirmed_types
        ]

        return {
            "reversals": reversals,
            "continuations": continuations,
            "confirmed": confirmed,
            "unconfirmed": unconfirmed,
        }


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

def detect_patterns(
    candles: List[Dict[str, Any]],
    volume_sma_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    """Detect chart patterns on raw Oanda candle data.

    Convenience wrapper around :class:`ChartPatterns` that returns the
    full ``scan_all()`` output.

    Args:
        candles: List of Oanda candle dicts.
        volume_sma_ratio: Ratio of current volume to SMA for breakout
            confirmation (optional).

    Returns:
        Dict with ``reversals``, ``continuations``, ``confirmed``,
        and ``unconfirmed`` lists.
    """
    cp = ChartPatterns(candles)
    return cp.scan_all(volume_sma_ratio=volume_sma_ratio)
