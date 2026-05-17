"""
Core technical indicator suite for the trading bot.

Computes EMA crossovers, RSI with divergence detection, MACD with
signal crossovers, Bollinger Bands with squeeze detection, and ATR
from Oanda candle data using the ``ta`` library.

The primary entry point is :meth:`Indicators.compute_all`, which
returns a consolidated dict of all indicator values and signals
for downstream consumers (confluence scoring, alignment engine).

Usage:
    from trading_bot.source.candle_pipeline import CandlePipeline
    from trading_bot.source.indicators import Indicators

    candles = pipeline.fetch_candles("EUR_USD", "H1", count=250)
    ind = Indicators(candles)
    results = ind.compute_all()
"""

from typing import Any, Dict, List, Optional

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange, BollingerBands


class Indicators:
    """Technical indicator suite operating on Oanda candle data.

    Accepts raw candle dicts (as returned by :class:`CandlePipeline`)
    and converts them to a pandas DataFrame internally.  Indicator
    methods compute lazily on first call and cache results on the
    instance.

    Args:
        candles: List of Oanda candle dicts.  Each dict must contain
            ``time``, ``mid`` (with o/h/l/c strings), ``volume``,
            and ``complete`` fields.
    """

    # EMA period sets used for crossover detection
    EMA_SET_1 = (9, 21, 50)
    EMA_SET_2 = (21, 55, 100)
    ALL_EMA_PERIODS = (9, 21, 50, 55, 100, 200)

    def __init__(self, candles: List[Dict[str, Any]]):
        self.df = self.candles_to_dataframe(candles)
        self._emas_computed = False

    # ------------------------------------------------------------------
    # Candle conversion
    # ------------------------------------------------------------------

    @staticmethod
    def candles_to_dataframe(
        candles: List[Dict[str, Any]],
    ) -> pd.DataFrame:
        """Convert Oanda candle dicts to a pandas DataFrame.

        Filters to complete candles only, casts price strings to
        float, and sorts by time ascending.

        Args:
            candles: Raw candle list from :class:`CandlePipeline`.

        Returns:
            DataFrame with float columns ``open``, ``high``, ``low``,
            ``close``, ``volume`` and a datetime index ``time``.

        Raises:
            ValueError: If no complete candles are present.
        """
        complete = [c for c in candles if c.get("complete", False)]
        if not complete:
            raise ValueError(
                "No complete candles in input data "
                f"(total candles: {len(candles)})"
            )

        rows = []
        for idx, c in enumerate(complete):
            mid = c["mid"]
            try:
                ts = pd.Timestamp(c["time"])
            except (ValueError, TypeError):
                # Fallback for non-standard timestamps: use sequential
                # minutes from a fixed epoch so ordering is preserved.
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

        df = pd.DataFrame(rows)
        df = df.sort_values("time").reset_index(drop=True)
        df = df.set_index("time")
        return df

    # ------------------------------------------------------------------
    # EMA suite
    # ------------------------------------------------------------------

    def compute_emas(self) -> Dict[int, pd.Series]:
        """Compute EMAs for all configured periods.

        Uses ``ta.trend.EMAIndicator`` for each period in
        :attr:`ALL_EMA_PERIODS` (9, 21, 50, 55, 100, 200).

        Returns:
            Dict mapping period (int) to the EMA pd.Series.
        """
        result: Dict[int, pd.Series] = {}
        for period in self.ALL_EMA_PERIODS:
            col_name = f"ema_{period}"
            if col_name not in self.df.columns:
                ema = EMAIndicator(
                    close=self.df["close"], window=period
                )
                self.df[col_name] = ema.ema_indicator()
            result[period] = self.df[col_name]
        self._emas_computed = True
        return result

    def get_ema_crossovers(self) -> Dict[str, Dict[str, Optional[str]]]:
        """Detect EMA crossovers for both period sets.

        A bullish crossover occurs when the fast EMA crosses above
        the slow EMA (current bar fast > slow AND previous bar
        fast <= slow).  Bearish is the inverse.

        Returns:
            Dict with ``set_1`` (9/21/50 crossovers) and ``set_2``
            (21/55/100 crossovers).  Each crossover entry is
            ``'bullish'``, ``'bearish'``, or ``None``.  Each set
            also includes ``trend`` entries showing current
            fast-vs-slow orientation.
        """
        if not self._emas_computed:
            self.compute_emas()

        def _detect(fast_p: int, slow_p: int) -> Dict[str, Optional[str]]:
            fast = self.df[f"ema_{fast_p}"]
            slow = self.df[f"ema_{slow_p}"]

            # Need at least 2 rows for crossover detection
            if len(fast.dropna()) < 2 or len(slow.dropna()) < 2:
                return {"crossover": None, "trend": None}

            curr_fast = fast.iloc[-1]
            prev_fast = fast.iloc[-2]
            curr_slow = slow.iloc[-1]
            prev_slow = slow.iloc[-2]

            # Skip if any are NaN (not enough data for the period)
            if pd.isna(curr_fast) or pd.isna(prev_fast):
                return {"crossover": None, "trend": None}
            if pd.isna(curr_slow) or pd.isna(prev_slow):
                return {"crossover": None, "trend": None}

            crossover: Optional[str] = None
            if curr_fast > curr_slow and prev_fast <= prev_slow:
                crossover = "bullish"
            elif curr_fast < curr_slow and prev_fast >= prev_slow:
                crossover = "bearish"

            trend = "bullish" if curr_fast > curr_slow else "bearish"

            return {"crossover": crossover, "trend": trend}

        set_1_pairs = [(9, 21), (9, 50), (21, 50)]
        set_2_pairs = [(21, 55), (21, 100), (55, 100)]

        set_1: Dict[str, Any] = {}
        for fast_p, slow_p in set_1_pairs:
            key = f"{fast_p}_{slow_p}"
            set_1[key] = _detect(fast_p, slow_p)

        set_2: Dict[str, Any] = {}
        for fast_p, slow_p in set_2_pairs:
            key = f"{fast_p}_{slow_p}"
            set_2[key] = _detect(fast_p, slow_p)

        return {"set_1": set_1, "set_2": set_2}

    def get_ema200_trend(self) -> Dict[str, Any]:
        """Classify trend direction relative to the 200-period EMA.

        - Price above EMA 200 = bullish bias
        - Price below EMA 200 = bearish bias
        - Price within 0.1% of EMA 200 = neutral

        Returns:
            Dict with ``direction`` (``'bullish'``/``'bearish'``/
            ``'neutral'``) and ``distance_pct`` (float, percentage
            distance from EMA 200).
        """
        if not self._emas_computed:
            self.compute_emas()

        ema_200 = self.df["ema_200"].iloc[-1]
        price = self.df["close"].iloc[-1]

        if pd.isna(ema_200):
            return {"direction": "neutral", "distance_pct": 0.0}

        distance_pct = ((price - ema_200) / ema_200) * 100.0

        if abs(distance_pct) <= 0.1:
            direction = "neutral"
        elif distance_pct > 0:
            direction = "bullish"
        else:
            direction = "bearish"

        return {"direction": direction, "distance_pct": round(distance_pct, 4)}

    # ------------------------------------------------------------------
    # RSI
    # ------------------------------------------------------------------

    def compute_rsi(self, period: int = 14) -> Dict[str, Any]:
        """Compute RSI and classify overbought/oversold conditions.

        Uses ``ta.momentum.RSIIndicator``.

        Args:
            period: RSI lookback window (default 14).

        Returns:
            Dict with ``value`` (latest RSI float), ``overbought``
            (bool, >70), ``oversold`` (bool, <30), and ``series``
            (full pd.Series).
        """
        col = f"rsi_{period}"
        if col not in self.df.columns:
            rsi = RSIIndicator(close=self.df["close"], window=period)
            self.df[col] = rsi.rsi()

        series = self.df[col]
        latest = series.iloc[-1]

        if pd.isna(latest):
            return {
                "value": None,
                "overbought": False,
                "oversold": False,
                "series": series,
            }

        return {
            "value": round(float(latest), 4),
            "overbought": float(latest) > 70,
            "oversold": float(latest) < 30,
            "series": series,
        }

    # ------------------------------------------------------------------
    # RSI Divergence
    # ------------------------------------------------------------------

    def get_rsi_divergence(
        self, lookback: int = 20
    ) -> Dict[str, Any]:
        """Detect RSI divergence (price/RSI disagreement).

        Bullish divergence: price makes lower low but RSI makes
        higher low within the lookback window.

        Bearish divergence: price makes higher high but RSI makes
        lower high within the lookback window.

        Uses simple local extremum detection: a bar is a local low
        if its close is less than both neighbors; a local high if
        its close is greater than both neighbors.

        Args:
            lookback: Number of bars to scan for divergence (default 20).

        Returns:
            Dict with ``bullish_divergence`` (bool),
            ``bearish_divergence`` (bool), and ``details`` (str or None).
        """
        rsi_col = "rsi_14"
        if rsi_col not in self.df.columns:
            self.compute_rsi(14)

        close = self.df["close"]
        rsi = self.df[rsi_col]

        window = min(lookback, len(close) - 2)
        if window < 4:
            return {
                "bullish_divergence": False,
                "bearish_divergence": False,
                "details": "Insufficient data for divergence detection",
            }

        # Use the last `window` bars (excluding the very last bar
        # since we need i+1 for neighbor comparison)
        start = len(close) - window - 1
        end = len(close) - 1  # exclusive of the last bar (needs right neighbor)

        # Find local lows and highs in the window
        local_lows: List[int] = []
        local_highs: List[int] = []

        for i in range(max(start, 1), end):
            c_prev = close.iloc[i - 1]
            c_curr = close.iloc[i]
            c_next = close.iloc[i + 1]

            if pd.isna(rsi.iloc[i]):
                continue

            if c_curr < c_prev and c_curr < c_next:
                local_lows.append(i)
            if c_curr > c_prev and c_curr > c_next:
                local_highs.append(i)

        bullish = False
        bearish = False
        details = None

        # Bullish divergence: compare two most recent lows
        if len(local_lows) >= 2:
            prev_low_idx = local_lows[-2]
            curr_low_idx = local_lows[-1]

            price_lower = close.iloc[curr_low_idx] < close.iloc[prev_low_idx]
            rsi_higher = rsi.iloc[curr_low_idx] > rsi.iloc[prev_low_idx]

            if price_lower and rsi_higher:
                bullish = True
                details = (
                    f"Bullish: price low {close.iloc[curr_low_idx]:.5f} < "
                    f"{close.iloc[prev_low_idx]:.5f} but RSI "
                    f"{rsi.iloc[curr_low_idx]:.1f} > "
                    f"{rsi.iloc[prev_low_idx]:.1f}"
                )

        # Bearish divergence: compare two most recent highs
        if len(local_highs) >= 2:
            prev_high_idx = local_highs[-2]
            curr_high_idx = local_highs[-1]

            price_higher = close.iloc[curr_high_idx] > close.iloc[prev_high_idx]
            rsi_lower = rsi.iloc[curr_high_idx] < rsi.iloc[prev_high_idx]

            if price_higher and rsi_lower:
                bearish = True
                detail_str = (
                    f"Bearish: price high {close.iloc[curr_high_idx]:.5f} > "
                    f"{close.iloc[prev_high_idx]:.5f} but RSI "
                    f"{rsi.iloc[curr_high_idx]:.1f} < "
                    f"{rsi.iloc[prev_high_idx]:.1f}"
                )
                details = detail_str if details is None else f"{details}; {detail_str}"

        return {
            "bullish_divergence": bullish,
            "bearish_divergence": bearish,
            "details": details,
        }

    # ------------------------------------------------------------------
    # MACD
    # ------------------------------------------------------------------

    def compute_macd(
        self, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> Dict[str, Any]:
        """Compute MACD with signal line crossover detection.

        Uses ``ta.trend.MACD``.

        Args:
            fast: Fast EMA period (default 12).
            slow: Slow EMA period (default 26).
            signal: Signal line period (default 9).

        Returns:
            Dict with ``macd`` (float), ``signal`` (float),
            ``histogram`` (float), ``crossover`` (``'bullish'``/
            ``'bearish'``/``None``), and ``momentum``
            (``'positive'``/``'negative'``).
        """
        prefix = f"macd_{fast}_{slow}_{signal}"
        macd_col = f"{prefix}_line"
        sig_col = f"{prefix}_signal"
        hist_col = f"{prefix}_hist"

        if macd_col not in self.df.columns:
            macd_obj = MACD(
                close=self.df["close"],
                window_slow=slow,
                window_fast=fast,
                window_sign=signal,
            )
            self.df[macd_col] = macd_obj.macd()
            self.df[sig_col] = macd_obj.macd_signal()
            self.df[hist_col] = macd_obj.macd_diff()

        macd_val = self.df[macd_col].iloc[-1]
        sig_val = self.df[sig_col].iloc[-1]
        hist_val = self.df[hist_col].iloc[-1]

        if pd.isna(macd_val) or pd.isna(sig_val):
            return {
                "macd": None,
                "signal": None,
                "histogram": None,
                "crossover": None,
                "momentum": None,
            }

        # Crossover detection
        crossover: Optional[str] = None
        if len(self.df) >= 2:
            prev_macd = self.df[macd_col].iloc[-2]
            prev_sig = self.df[sig_col].iloc[-2]
            if not (pd.isna(prev_macd) or pd.isna(prev_sig)):
                if macd_val > sig_val and prev_macd <= prev_sig:
                    crossover = "bullish"
                elif macd_val < sig_val and prev_macd >= prev_sig:
                    crossover = "bearish"

        momentum = "positive" if hist_val >= 0 else "negative"

        return {
            "macd": round(float(macd_val), 6),
            "signal": round(float(sig_val), 6),
            "histogram": round(float(hist_val), 6),
            "crossover": crossover,
            "momentum": momentum,
        }

    def get_macd_divergence(
        self, lookback: int = 20, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> Dict[str, Any]:
        """Detect MACD histogram divergence (price/MACD disagreement).

        Bullish divergence: price makes lower low but MACD histogram
        makes higher low (momentum shifting up despite price dropping).

        Bearish divergence: price makes higher high but MACD histogram
        makes lower high (momentum fading despite price rising).

        Args:
            lookback: Number of bars to scan (default 20).
            fast: MACD fast period.
            slow: MACD slow period.
            signal: MACD signal period.

        Returns:
            Dict with ``bullish_divergence`` (bool),
            ``bearish_divergence`` (bool), and ``details`` (str or None).
        """
        hist_col = f"macd_{fast}_{slow}_{signal}_hist"
        if hist_col not in self.df.columns:
            self.compute_macd(fast, slow, signal)

        close = self.df["close"]
        hist = self.df[hist_col]

        window = min(lookback, len(close) - 2)
        if window < 4:
            return {"bullish_divergence": False, "bearish_divergence": False, "details": None}

        start = len(close) - window - 1
        end = len(close) - 1

        local_lows: List[int] = []
        local_highs: List[int] = []

        for i in range(max(start, 1), end):
            if pd.isna(hist.iloc[i]):
                continue
            c_prev, c_curr, c_next = close.iloc[i-1], close.iloc[i], close.iloc[i+1]
            if c_curr < c_prev and c_curr < c_next:
                local_lows.append(i)
            if c_curr > c_prev and c_curr > c_next:
                local_highs.append(i)

        bullish = False
        bearish = False
        details = None

        if len(local_lows) >= 2:
            pl, cl = local_lows[-2], local_lows[-1]
            if close.iloc[cl] < close.iloc[pl] and hist.iloc[cl] > hist.iloc[pl]:
                bullish = True
                details = (
                    f"MACD Bullish: price low {close.iloc[cl]:.5f} < "
                    f"{close.iloc[pl]:.5f} but MACD hist "
                    f"{hist.iloc[cl]:.6f} > {hist.iloc[pl]:.6f}"
                )

        if len(local_highs) >= 2:
            ph, ch = local_highs[-2], local_highs[-1]
            if close.iloc[ch] > close.iloc[ph] and hist.iloc[ch] < hist.iloc[ph]:
                bearish = True
                d = (
                    f"MACD Bearish: price high {close.iloc[ch]:.5f} > "
                    f"{close.iloc[ph]:.5f} but MACD hist "
                    f"{hist.iloc[ch]:.6f} < {hist.iloc[ph]:.6f}"
                )
                details = f"{details}; {d}" if details else d

        return {"bullish_divergence": bullish, "bearish_divergence": bearish, "details": details}

    # ------------------------------------------------------------------
    # Bollinger Bands
    # ------------------------------------------------------------------

    def compute_bollinger(
        self,
        period: int = 20,
        std_dev: int = 2,
        squeeze_threshold: float = 0.02,
    ) -> Dict[str, Any]:
        """Compute Bollinger Bands with squeeze detection.

        Uses ``ta.volatility.BollingerBands``.

        Squeeze is detected when bandwidth (upper - lower) / middle
        drops below *squeeze_threshold*.

        Position classification:
        - ``'upper'``: price in upper 25% of band range
        - ``'lower'``: price in lower 25% of band range
        - ``'middle'``: price in central 50%

        Args:
            period: Band period (default 20).
            std_dev: Standard deviation multiplier (default 2).
            squeeze_threshold: Bandwidth ratio below which a squeeze
                is flagged (default 0.02).

        Returns:
            Dict with ``upper``, ``middle``, ``lower`` (floats),
            ``bandwidth`` (float), ``squeeze`` (bool), and
            ``position`` (``'upper'``/``'middle'``/``'lower'``).
        """
        prefix = f"bb_{period}_{std_dev}"
        upper_col = f"{prefix}_upper"
        mid_col = f"{prefix}_mid"
        lower_col = f"{prefix}_lower"

        if upper_col not in self.df.columns:
            bb = BollingerBands(
                close=self.df["close"],
                window=period,
                window_dev=std_dev,
            )
            self.df[upper_col] = bb.bollinger_hband()
            self.df[mid_col] = bb.bollinger_mavg()
            self.df[lower_col] = bb.bollinger_lband()

        upper = self.df[upper_col].iloc[-1]
        middle = self.df[mid_col].iloc[-1]
        lower = self.df[lower_col].iloc[-1]

        if pd.isna(upper) or pd.isna(middle) or pd.isna(lower):
            return {
                "upper": None,
                "middle": None,
                "lower": None,
                "bandwidth": None,
                "squeeze": False,
                "position": "middle",
            }

        bandwidth = (upper - lower) / middle if middle != 0 else 0.0
        squeeze = bandwidth < squeeze_threshold

        # Position classification
        band_range = upper - lower
        price = self.df["close"].iloc[-1]
        if band_range > 0:
            relative_pos = (price - lower) / band_range
            if relative_pos >= 0.75:
                position = "upper"
            elif relative_pos <= 0.25:
                position = "lower"
            else:
                position = "middle"
        else:
            position = "middle"

        return {
            "upper": round(float(upper), 6),
            "middle": round(float(middle), 6),
            "lower": round(float(lower), 6),
            "bandwidth": round(float(bandwidth), 6),
            "squeeze": squeeze,
            "position": position,
        }

    # ------------------------------------------------------------------
    # ATR
    # ------------------------------------------------------------------

    def compute_atr(self, period: int = 14) -> Dict[str, Any]:
        """Compute Average True Range.

        Uses ``ta.volatility.AverageTrueRange``.

        Args:
            period: ATR lookback window (default 14).

        Returns:
            Dict with ``value`` (latest ATR float) and ``series``
            (full pd.Series).
        """
        col = f"atr_{period}"
        if col not in self.df.columns:
            atr = AverageTrueRange(
                high=self.df["high"],
                low=self.df["low"],
                close=self.df["close"],
                window=period,
            )
            self.df[col] = atr.average_true_range()

        series = self.df[col]
        latest = series.iloc[-1]

        if pd.isna(latest):
            return {"value": None, "series": series}

        return {"value": round(float(latest), 6), "series": series}

    # ------------------------------------------------------------------
    # Convenience: compute all
    # ------------------------------------------------------------------

    def compute_all(self) -> Dict[str, Any]:
        """Compute all indicators and return consolidated results.

        This is the primary method for downstream consumers
        (confluence scoring, alignment engine).

        Returns:
            Dict with keys: ``emas``, ``ema_crossovers``,
            ``ema200_trend``, ``rsi``, ``rsi_divergence``,
            ``macd``, ``bollinger``, ``atr``.
        """
        return {
            "emas": self.compute_emas(),
            "ema_crossovers": self.get_ema_crossovers(),
            "ema200_trend": self.get_ema200_trend(),
            "rsi": self.compute_rsi(),
            "rsi_divergence": self.get_rsi_divergence(),
            "macd": self.compute_macd(),
            "macd_divergence": self.get_macd_divergence(),
            "bollinger": self.compute_bollinger(),
            "atr": self.compute_atr(),
        }
