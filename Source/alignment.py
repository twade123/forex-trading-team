"""
Multi-timeframe alignment engine for the trading bot.

Runs all 12 indicators (8 core + 4 advanced primary) across three
timeframes (M15, H1, H4) and produces a weighted directional alignment
score.  The alignment hierarchy follows TECH-13:

- **H4** provides directional bias (weight 0.45)
- **H1** provides trend confirmation (weight 0.35)
- **M15** provides entry timing (weight 0.20)

The primary consumer is :meth:`get_snapshot`, which returns a complete
alignment assessment including per-timeframe indicator details and a
human-readable summary for downstream strategy engine consumption.

Usage:
    from trading_bot.source.alignment import MultiTimeframeAlignment

    data = pipeline.fetch_multi_timeframe("EUR_USD", count=250)
    mta = MultiTimeframeAlignment(data)
    mta.analyze()
    snapshot = mta.get_snapshot()

Integration with CandlePipeline:
    alignment = MultiTimeframeAlignment.from_pipeline(pipeline, "EUR_USD")
    alignment.analyze()
    snapshot = alignment.get_snapshot()
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from .indicators import Indicators
    from .indicators_advanced import AdvancedIndicators
except ImportError:
    from indicators import Indicators  # type: ignore[no-redef]
    from indicators_advanced import AdvancedIndicators  # type: ignore[no-redef]


# Timeframe weights for directional bias scoring (TECH-13).
_TIMEFRAME_WEIGHTS: Dict[str, float] = {
    "H4": 0.45,
    "H1": 0.35,
    "M15": 0.20,
}


class MultiTimeframeAlignment:
    """Multi-timeframe alignment engine combining all indicators.

    For each timeframe in the provided candle data, an :class:`Indicators`
    instance and an :class:`AdvancedIndicators` instance are created.
    Calling :meth:`analyze` computes every indicator on every timeframe;
    :meth:`get_directional_bias` then derives a single weighted directional
    score from key indicator signals.

    Args:
        candle_data: Nested dict from ``CandlePipeline.fetch_multi_timeframe``
            (or equivalent).  Keys are timeframe strings (``'M15'``,
            ``'H1'``, ``'H4'``); values are lists of Oanda candle dicts.
    """

    def __init__(self, candle_data: Dict[str, List[Dict[str, Any]]]) -> None:
        self._candle_data = candle_data

        # Create indicator instances keyed by timeframe.
        self._core: Dict[str, Indicators] = {}
        self._advanced: Dict[str, AdvancedIndicators] = {}
        for tf, candles in candle_data.items():
            self._core[tf] = Indicators(candles)
            self._advanced[tf] = AdvancedIndicators(candles)

        # Populated by analyze().
        self.results: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # analyze() - compute all indicators on all timeframes
    # ------------------------------------------------------------------

    def analyze(self) -> Dict[str, Dict[str, Any]]:
        """Compute all indicators for every timeframe.

        Calls ``compute_all()`` on both the core :class:`Indicators` and
        :class:`AdvancedIndicators` instances for each timeframe.

        Stores the results as::

            {
                'M15': {'core': {...}, 'advanced': {...}},
                'H1':  {'core': {...}, 'advanced': {...}},
                'H4':  {'core': {...}, 'advanced': {...}},
            }

        Returns:
            The full results dict (also stored on ``self.results``).
        """
        self.results = {}
        for tf in self._candle_data:
            self.results[tf] = {
                "core": self._core[tf].compute_all(),
                "advanced": self._advanced[tf].compute_all(),
            }
        return self.results

    # ------------------------------------------------------------------
    # get_directional_bias() - core alignment logic
    # ------------------------------------------------------------------

    def get_directional_bias(self) -> Dict[str, Any]:
        """Derive a weighted directional alignment score from all indicators.

        For each timeframe the method combines directional signals from:

        * **EMA trend** -- price vs EMA-200, fast vs slow orientation.
          Bullish +1, bearish -1, neutral 0.
        * **RSI** -- overbought slight bearish (-0.5), oversold slight
          bullish (+0.5), neutral 0.
        * **MACD** -- positive momentum +1, negative -1.
        * **ADX regime** -- amplifies (1.5x) in trending markets,
          dampens (0.5x) in ranging, neutral (1x) in mixed.
        * **Stochastic** -- in *ranging* markets only (per ADX):
          overbought -0.5, oversold +0.5.

        Timeframe weights follow the TECH-13 hierarchy:
        H4 = 0.45, H1 = 0.35, M15 = 0.20.

        Returns:
            Dict with keys:

            * ``alignment``: One of ``'bullish_aligned'``,
              ``'bullish_leaning'``, ``'bearish_aligned'``,
              ``'bearish_leaning'``, ``'neutral'``.
            * ``score``: Float weighted directional score.
            * ``per_timeframe``: Per-timeframe breakdown with
              ``direction``, ``weight``, and ``details`` sub-dicts.
        """
        if not self.results:
            self.analyze()

        per_timeframe: Dict[str, Dict[str, Any]] = {}
        weighted_sum = 0.0

        for tf, weight in _TIMEFRAME_WEIGHTS.items():
            if tf not in self.results:
                continue

            core = self.results[tf]["core"]
            adv = self.results[tf]["advanced"]

            # --- EMA trend signal ---
            ema_trend = core.get("ema200_trend", {})
            ema_direction = ema_trend.get("direction", "neutral")
            if ema_direction == "bullish":
                ema_signal = 1.0
            elif ema_direction == "bearish":
                ema_signal = -1.0
            else:
                ema_signal = 0.0

            # --- RSI signal ---
            rsi_data = core.get("rsi", {})
            if rsi_data.get("overbought"):
                rsi_signal = -0.5
            elif rsi_data.get("oversold"):
                rsi_signal = 0.5
            else:
                rsi_signal = 0.0

            # --- MACD signal ---
            macd_data = core.get("macd", {})
            momentum = macd_data.get("momentum")
            if momentum == "positive":
                macd_signal = 1.0
            elif momentum == "negative":
                macd_signal = -1.0
            else:
                macd_signal = 0.0

            # --- ADX regime multiplier ---
            adx_data = adv.get("adx", {})
            regime = adx_data.get("regime", "mixed")
            if regime == "trending":
                regime_multiplier = 1.5
            elif regime == "ranging":
                regime_multiplier = 0.5
            else:
                regime_multiplier = 1.0

            # --- Stochastic signal (ranging markets only) ---
            stoch_signal = 0.0
            if regime == "ranging":
                stoch_data = adv.get("stochastic", {})
                if stoch_data.get("overbought"):
                    stoch_signal = -0.5
                elif stoch_data.get("oversold"):
                    stoch_signal = 0.5

            # --- Combine signals for this timeframe ---
            raw_direction = ema_signal + rsi_signal + macd_signal + stoch_signal
            direction = raw_direction * regime_multiplier

            # Normalise so each timeframe's contribution is roughly in [-1, 1]
            # before weighting.  Max theoretical raw magnitude with stochastic
            # is |1 + 0.5 + 1 + 0.5| = 3 (ranging case), amplified to 1.5
            # (trending) or 0.5 (ranging).  We normalise by max possible raw
            # contribution (3 * 1.5 = 4.5) to keep score bounded.
            max_possible = 3.0 * 1.5  # 4.5
            normalised = direction / max_possible

            per_timeframe[tf] = {
                "direction": round(normalised, 4),
                "weight": weight,
                "details": {
                    "ema_signal": ema_signal,
                    "rsi_signal": rsi_signal,
                    "macd_signal": macd_signal,
                    "stoch_signal": stoch_signal,
                    "regime": regime,
                    "regime_multiplier": regime_multiplier,
                    "raw_direction": round(raw_direction, 4),
                    "adjusted_direction": round(direction, 4),
                },
            }

            weighted_sum += normalised * weight

        score = round(weighted_sum, 4)

        # --- Classify alignment ---
        if score > 0.5:
            alignment = "bullish_aligned"
        elif score > 0.2:
            alignment = "bullish_leaning"
        elif score < -0.5:
            alignment = "bearish_aligned"
        elif score < -0.2:
            alignment = "bearish_leaning"
        else:
            alignment = "neutral"

        return {
            "alignment": alignment,
            "score": score,
            "per_timeframe": per_timeframe,
        }

    # ------------------------------------------------------------------
    # get_snapshot() - complete output for the strategy engine
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Dict[str, Any]:
        """Return a complete alignment snapshot for downstream consumers.

        Returns:
            Dict with keys:

            * ``alignment``: Full result from :meth:`get_directional_bias`.
            * ``indicators``: Per-timeframe indicator results from
              :meth:`analyze`.
            * ``summary``: Human-readable summary string.
        """
        if not self.results:
            self.analyze()

        bias = self.get_directional_bias()
        summary = self._build_summary(bias)

        return {
            "alignment": bias,
            "indicators": self.results,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # from_pipeline() - CandlePipeline integration classmethod
    # ------------------------------------------------------------------

    @classmethod
    def from_pipeline(
        cls,
        pipeline: Any,
        instrument: str,
        count: int = 250,
    ) -> "MultiTimeframeAlignment":
        """Create an alignment instance from a :class:`CandlePipeline`.

        Convenience class method providing a single-call integration path
        for the trading cycle::

            alignment = MultiTimeframeAlignment.from_pipeline(
                pipeline, "EUR_USD"
            )

        Args:
            pipeline: A :class:`CandlePipeline` instance.
            instrument: Instrument name (e.g. ``'EUR_USD'``).
            count: Number of candles to fetch per timeframe (default 250).

        Returns:
            A new :class:`MultiTimeframeAlignment` instance initialised
            with the fetched candle data.
        """
        data = pipeline.fetch_multi_timeframe(instrument, count=count)
        return cls(data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_summary(bias: Dict[str, Any]) -> str:
        """Build a human-readable summary string from bias results.

        Args:
            bias: Result dict from :meth:`get_directional_bias`.

        Returns:
            Summary string, e.g. ``"Bullish aligned: H4 bullish (EMA+MACD),
            H1 bullish (RSI 45), M15 neutral"``.
        """
        alignment = bias.get("alignment", "neutral")
        label = alignment.replace("_", " ").title()

        parts: List[str] = []
        for tf in ("H4", "H1", "M15"):
            tf_data = bias.get("per_timeframe", {}).get(tf)
            if tf_data is None:
                parts.append(f"{tf} n/a")
                continue

            direction = tf_data["direction"]
            details = tf_data.get("details", {})

            if direction > 0.05:
                tf_label = "bullish"
            elif direction < -0.05:
                tf_label = "bearish"
            else:
                tf_label = "neutral"

            # Build brief detail string
            signals: List[str] = []
            if details.get("ema_signal", 0) != 0:
                signals.append("EMA")
            if details.get("macd_signal", 0) != 0:
                signals.append("MACD")
            if details.get("rsi_signal", 0) != 0:
                signals.append("RSI")
            if details.get("stoch_signal", 0) != 0:
                signals.append("Stoch")

            regime = details.get("regime", "")
            detail_str = "+".join(signals) if signals else regime
            parts.append(f"{tf} {tf_label} ({detail_str})")

        return f"{label}: {', '.join(parts)}"
