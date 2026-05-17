"""
Confluence scoring engine for the trading bot.

Converts all upstream indicator, alignment, and pattern signals into a
single 0-100 confluence score using the weighted contribution system
defined in STRT-03.  The ADX regime (from :mod:`indicators_advanced`)
determines which signal types are amplified:

- **Trending** markets amplify trend-follow signals (EMA, MACD, Multi-TF)
  and dampen mean-revert signals (Stochastic, Bollinger).
- **Ranging** markets amplify mean-revert signals and dampen trend-follow.
- **Mixed** markets apply no adjustment.

Primary entry point is :meth:`ConfluenceScorer.compute_score`, which
returns a dict with the total 0-100 score, per-source breakdown, regime
classification, and trade direction.

Usage:
    from trading_bot.source.confluence_scorer import ConfluenceScorer

    cs = ConfluenceScorer()
    result = cs.compute_score(
        indicators_result=ind.compute_all(),
        advanced_result=adv.compute_all(),
        alignment_snapshot=mta.get_snapshot(),
        pattern_results=pi.scan(),
    )
"""

from typing import Any, Dict, Optional


class ConfluenceScorer:
    """Weighted confluence scoring engine with ADX regime awareness.

    Combines signals from 10 sources into a single 0-100 score.  Each
    source has a maximum weight (summing to 100) and individual scoring
    functions that evaluate signal strength.  The ADX regime multiplies
    source weights to amplify trend-follow or mean-revert signals.

    Max weights per source (STRT-03):
        EMA: 15, RSI: 15, MACD: 10, Bollinger: 10, Volume: 5,
        Stochastic: 5, Multi-TF: 15, Candlestick: 10, Chart: 10,
        News: 5.  Total = 100.

    ADX regime thresholds (STRT-01):
        >25 = trending, <20 = ranging, 20-25 = mixed.
    """

    # STRT-03 maximum weights per signal source
    MAX_WEIGHTS: Dict[str, float] = {
        "ema": 15.0,
        "rsi": 15.0,
        "macd": 10.0,
        "bollinger": 10.0,
        "volume": 5.0,
        "stochastic": 5.0,
        "multi_tf": 15.0,
        "candlestick": 10.0,
        "chart": 10.0,
        "news": 5.0,
    }

    # Regime-specific weight multipliers
    REGIME_MULTIPLIERS: Dict[str, Dict[str, float]] = {
        "trending": {
            "ema": 1.3,
            "rsi": 1.0,
            "macd": 1.3,
            "bollinger": 0.7,
            "volume": 1.0,
            "stochastic": 0.5,
            "multi_tf": 1.2,
            "candlestick": 1.0,
            "chart": 1.0,
            "news": 1.0,
        },
        "ranging": {
            "ema": 0.6,
            "rsi": 1.2,
            "macd": 0.6,
            "bollinger": 1.4,
            "volume": 1.0,
            "stochastic": 1.5,
            "multi_tf": 1.0,
            "candlestick": 1.0,
            "chart": 1.0,
            "news": 1.0,
        },
        "mixed": {
            "ema": 1.0,
            "rsi": 1.0,
            "macd": 1.0,
            "bollinger": 1.0,
            "volume": 1.0,
            "stochastic": 1.0,
            "multi_tf": 1.0,
            "candlestick": 1.0,
            "chart": 1.0,
            "news": 1.0,
        },
    }

    # STRT-02: only trade when total score exceeds this threshold
    TRADE_THRESHOLD = 70

    # ------------------------------------------------------------------
    # ADX regime detection
    # ------------------------------------------------------------------

    @staticmethod
    def get_regime(adx_value: float) -> str:
        """Classify market regime from ADX value.

        Args:
            adx_value: Current ADX reading.

        Returns:
            ``'trending'`` if ADX > 25, ``'ranging'`` if ADX < 20,
            ``'mixed'`` if 20-25.
        """
        if adx_value > 25:
            return "trending"
        elif adx_value < 20:
            return "ranging"
        else:
            return "mixed"

    # ------------------------------------------------------------------
    # Main scoring method
    # ------------------------------------------------------------------

    def compute_score(
        self,
        indicators_result: Optional[Dict[str, Any]] = None,
        advanced_result: Optional[Dict[str, Any]] = None,
        alignment_snapshot: Optional[Dict[str, Any]] = None,
        pattern_results: Optional[Dict[str, Any]] = None,
        news_data: Optional[Dict[str, Any]] = None,
        chart_results: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Compute the 0-100 confluence score from all signal sources.

        Args:
            indicators_result: Output from ``Indicators.compute_all()``
                (EMA, RSI, MACD, Bollinger, ATR).
            advanced_result: Output from
                ``AdvancedIndicators.compute_all()`` (ADX, Stochastic,
                Volume SMA, Fibonacci, VWAP).
            alignment_snapshot: Output from
                ``MultiTimeframeAlignment.get_snapshot()``.
            pattern_results: Output from ``PatternIntegration.scan()``.
            news_data: Optional news sentiment data (Phase 6 stub).

        Returns:
            Dict with keys:

            - ``total_score``: Float 0-100.
            - ``regime``: ``'trending'``, ``'ranging'``, or ``'mixed'``.
            - ``adx_value``: Float ADX reading used for regime.
            - ``breakdown``: Per-source score dict (10 keys).
            - ``direction``: ``'bullish'``, ``'bearish'``, or ``'neutral'``.
            - ``max_possible``: 100.
            - ``threshold``: 70 (STRT-02).
        """
        indicators_result = indicators_result or {}
        advanced_result = advanced_result or {}
        alignment_snapshot = alignment_snapshot or {}
        pattern_results = pattern_results or {}

        # Extract ADX value and determine regime
        adx_data = advanced_result.get("adx", {})
        adx_value = adx_data.get("value", adx_data.get("adx", 0.0))
        if adx_value is None:
            adx_value = 0.0
        regime = self.get_regime(adx_value)

        # Get regime multipliers
        multipliers = self.REGIME_MULTIPLIERS.get(regime, self.REGIME_MULTIPLIERS["mixed"])

        # Score each signal source
        raw_scores = {
            "ema": self._score_ema(indicators_result, regime),
            "rsi": self._score_rsi(indicators_result, regime),
            "macd": self._score_macd(indicators_result, regime),
            "bollinger": self._score_bollinger(indicators_result, regime),
            "volume": self._score_volume(advanced_result, regime),
            "stochastic": self._score_stochastic(advanced_result, regime),
            "multi_tf": self._score_multi_tf(alignment_snapshot, regime),
            "candlestick": self._score_candlestick(pattern_results, regime),
            "chart": self._score_chart(chart_results or pattern_results, regime),
            "news": self._score_news(news_data, regime),
        }

        # Apply regime multipliers and cap at max weight
        breakdown: Dict[str, float] = {}
        for source, raw in raw_scores.items():
            adjusted = raw * multipliers.get(source, 1.0)
            capped = min(adjusted, self.MAX_WEIGHTS[source])
            breakdown[source] = max(0.0, round(capped, 2))

        total_score = round(sum(breakdown.values()), 2)
        total_score = min(100.0, max(0.0, total_score))

        # Determine overall direction
        direction = self._determine_direction(
            indicators_result, advanced_result, alignment_snapshot, pattern_results
        )

        return {
            "total_score": total_score,
            "regime": regime,
            "adx_value": adx_value,
            "breakdown": breakdown,
            "direction": direction,
            "max_possible": 100,
            "threshold": self.TRADE_THRESHOLD,
        }

    # ------------------------------------------------------------------
    # Individual signal scoring functions
    # ------------------------------------------------------------------

    def _score_ema(
        self, indicators: Dict[str, Any], regime: str
    ) -> float:
        """Score EMA signals: crossover alignment, trend, 200 EMA position.

        Max raw contribution: 15 points.
        - EMA 200 trend direction: up to 5 pts
        - EMA crossover (9/21): up to 5 pts
        - EMA crossover (21/55): up to 5 pts

        Args:
            indicators: Core indicators result dict.
            regime: Current ADX regime.

        Returns:
            Raw score 0-15.
        """
        score = 0.0

        # EMA 200 trend (up to 5 pts)
        ema_trend = indicators.get("ema200_trend", indicators.get("ema", {}))
        if isinstance(ema_trend, dict):
            direction = ema_trend.get("direction", ema_trend.get("trend"))
            if direction in ("bullish", "bearish"):
                distance = abs(ema_trend.get("distance_pct", 0.0))
                # Stronger signal when further from EMA 200
                if distance > 0.5:
                    score += 5.0
                elif distance > 0.2:
                    score += 3.0
                else:
                    score += 1.5

        # EMA crossovers from the crossovers dict
        crossovers = indicators.get("ema_crossovers", {})

        # Also handle the flat format from plan's verify mock data
        if not crossovers and isinstance(indicators.get("ema"), dict):
            ema_flat = indicators["ema"]
            if ema_flat.get("crossover_9_21") in ("bullish", "bearish"):
                score += 5.0
            if ema_flat.get("crossover_21_55") in ("bullish", "bearish"):
                score += 5.0
            # EMA 200 position from flat format
            if ema_flat.get("ema_200_position") in ("above", "below"):
                if score == 0.0:  # Only if not already scored from ema200_trend
                    score += 3.0
            return min(score, 15.0)

        # Set 1: 9/21 crossover (up to 5 pts)
        set_1 = crossovers.get("set_1", {})
        pair_9_21 = set_1.get("9_21", {})
        if isinstance(pair_9_21, dict):
            if pair_9_21.get("crossover") in ("bullish", "bearish"):
                score += 5.0
            elif pair_9_21.get("trend") in ("bullish", "bearish"):
                score += 2.0

        # Set 2: 21/55 crossover (up to 5 pts)
        set_2 = crossovers.get("set_2", {})
        pair_21_55 = set_2.get("21_55", {})
        if isinstance(pair_21_55, dict):
            if pair_21_55.get("crossover") in ("bullish", "bearish"):
                score += 5.0
            elif pair_21_55.get("trend") in ("bullish", "bearish"):
                score += 2.0

        return min(score, 15.0)

    def _score_rsi(
        self, indicators: Dict[str, Any], regime: str
    ) -> float:
        """Score RSI: overbought/oversold proximity, divergence detection.

        Max raw contribution: 15 points.
        - Overbought/oversold signal: up to 8 pts
        - Divergence detection: up to 7 pts

        Args:
            indicators: Core indicators result dict.
            regime: Current ADX regime.

        Returns:
            Raw score 0-15.
        """
        score = 0.0

        rsi_data = indicators.get("rsi", {})
        if not isinstance(rsi_data, dict):
            return 0.0

        rsi_value = rsi_data.get("value")
        if rsi_value is None:
            return 0.0

        # OB/OS proximity scoring (up to 8 pts)
        if rsi_value > 70 or rsi_value < 30:
            # Strong OB/OS -- high reversal signal value
            score += 8.0
        elif rsi_value > 65 or rsi_value < 35:
            # Approaching extremes
            score += 5.0
        elif rsi_value > 60 or rsi_value < 40:
            # Mild directional bias
            score += 2.0
        # 40-60 is neutral, 0 pts

        # Divergence (up to 7 pts)
        div_data = indicators.get("rsi_divergence", {})
        if isinstance(div_data, dict):
            if div_data.get("bullish_divergence") or div_data.get("bearish_divergence"):
                score += 7.0

        # Also handle the flat format divergence key
        if isinstance(rsi_data.get("divergence"), str):
            score += 7.0

        return min(score, 15.0)

    def _score_macd(
        self, indicators: Dict[str, Any], regime: str
    ) -> float:
        """Score MACD: histogram direction, signal line crossover, divergence.

        Max raw contribution: 10 points.
        - Crossover signal: up to 4 pts
        - Histogram momentum: up to 3 pts
        - MACD divergence: up to 3 pts

        Args:
            indicators: Core indicators result dict.
            regime: Current ADX regime.

        Returns:
            Raw score 0-10.
        """
        score = 0.0

        macd_data = indicators.get("macd", {})
        if not isinstance(macd_data, dict):
            return 0.0

        # Crossover signal (up to 4 pts)
        crossover = macd_data.get("crossover")
        if crossover in ("bullish", "bearish"):
            score += 4.0

        # Histogram momentum (up to 3 pts)
        histogram = macd_data.get("histogram")
        if histogram is not None:
            momentum = macd_data.get("momentum")
            if momentum in ("positive", "negative"):
                score += 3.0

        # MACD divergence (up to 3 pts)
        macd_div = indicators.get("macd_divergence", {})
        if isinstance(macd_div, dict):
            if macd_div.get("bullish_divergence") or macd_div.get("bearish_divergence"):
                score += 3.0

        return min(score, 10.0)

    def _score_bollinger(
        self, indicators: Dict[str, Any], regime: str
    ) -> float:
        """Score Bollinger Bands: position, squeeze, mean reversion.

        Max raw contribution: 10 points.
        - Band position (upper/lower): up to 4 pts
        - Squeeze status: up to 3 pts
        - Mean reversion signal: up to 3 pts

        Args:
            indicators: Core indicators result dict.
            regime: Current ADX regime.

        Returns:
            Raw score 0-10.
        """
        score = 0.0

        bb_data = indicators.get("bollinger", {})
        if not isinstance(bb_data, dict):
            return 0.0

        # Band position (up to 4 pts)
        position = bb_data.get("position")
        if position in ("upper", "lower"):
            score += 4.0
        elif position == "middle":
            score += 1.0

        # Squeeze status (up to 3 pts)
        squeeze = bb_data.get("squeeze", False)
        if squeeze:
            score += 3.0

        # Bandwidth as mean-reversion signal (up to 3 pts)
        bandwidth = bb_data.get("bandwidth")
        if bandwidth is not None:
            if bandwidth > 0.04:
                # Wide bands -- trending market, less mean-reversion value
                score += 1.0
            elif bandwidth > 0.02:
                score += 2.0
            else:
                # Tight bands -- squeeze or consolidation
                score += 3.0

        return min(score, 10.0)

    def _score_volume(
        self, advanced: Dict[str, Any], regime: str
    ) -> float:
        """Score Volume: above/below SMA confirmation.

        Max raw contribution: 5 points.

        Args:
            advanced: Advanced indicators result dict.
            regime: Current ADX regime.

        Returns:
            Raw score 0-5.
        """
        score = 0.0

        vol_data = advanced.get("volume_sma", {})
        if not isinstance(vol_data, dict):
            return 0.0

        ratio = vol_data.get("ratio")
        if ratio is None:
            return 0.0

        above_average = vol_data.get("above_average", ratio >= 1.0)

        if above_average or ratio >= 1.0:
            # Scale by how much above average
            if ratio >= 1.5:
                score += 5.0
            elif ratio >= 1.2:
                score += 4.0
            elif ratio >= 1.0:
                score += 3.0
        else:
            # Below average volume -- weak confirmation
            score += 1.0

        return min(score, 5.0)

    def _score_stochastic(
        self, advanced: Dict[str, Any], regime: str
    ) -> float:
        """Score Stochastic: overbought/oversold in ranging markets.

        Max raw contribution: 5 points.
        Stochastic is most valuable in ranging markets per STRT-01.

        Args:
            advanced: Advanced indicators result dict.
            regime: Current ADX regime.

        Returns:
            Raw score 0-5.
        """
        score = 0.0

        stoch_data = advanced.get("stochastic", {})
        if not isinstance(stoch_data, dict):
            return 0.0

        k_val = stoch_data.get("k")
        if k_val is None:
            return 0.0

        # OB/OS condition (up to 3 pts)
        condition = stoch_data.get("condition")
        overbought = stoch_data.get("overbought", False)
        oversold = stoch_data.get("oversold", False)

        if overbought or oversold or condition in ("overbought", "oversold"):
            score += 3.0
        elif k_val > 70 or k_val < 30:
            score += 2.0

        # Crossover (up to 2 pts)
        crossover = stoch_data.get("crossover")
        if crossover in ("bullish", "bearish"):
            score += 2.0

        return min(score, 5.0)

    def _score_multi_tf(
        self, alignment_snapshot: Dict[str, Any], regime: str
    ) -> float:
        """Score Multi-Timeframe alignment from MultiTimeframeAlignment.

        Max raw contribution: 15 points.
        Alignment score is in [-1, 1] range (from 03-03).

        Args:
            alignment_snapshot: Output from
                ``MultiTimeframeAlignment.get_snapshot()``.
            regime: Current ADX regime.

        Returns:
            Raw score 0-15.
        """
        if not isinstance(alignment_snapshot, dict):
            return 0.0

        # Try direct alignment_score key first (flat format)
        alignment_score = alignment_snapshot.get("alignment_score")

        # Try nested alignment dict (from get_snapshot())
        if alignment_score is None:
            alignment_data = alignment_snapshot.get("alignment", {})
            if isinstance(alignment_data, dict):
                alignment_score = alignment_data.get("score")

        if alignment_score is None:
            return 0.0

        # Alignment score is [-1, 1] -- map absolute value to 0-15
        abs_score = abs(float(alignment_score))
        return min(abs_score * 15.0, 15.0)

    def _score_candlestick(
        self, pattern_results: Any, regime: str
    ) -> float:
        """Score candlestick patterns: high-priority with context filtering.

        Max raw contribution: 10 points.
        Handles both list format (from CandlestickPatterns.get_detected_patterns())
        and dict format (from PatternIntegration.scan()).

        Args:
            pattern_results: List of pattern dicts or dict with summary/patterns.
            regime: Current ADX regime.

        Returns:
            Raw score 0-10.
        """
        score = 0.0

        # Handle list format (direct from CandlestickPatterns)
        if isinstance(pattern_results, list):
            patterns = pattern_results
            if not patterns:
                return 0.0
            # Base score: 2 pts per pattern, max 4
            score += min(len(patterns) * 2.0, 4.0)
            # Priority bonus: medium=2, high=3 pts each, max 4
            priority_score = 0.0
            for p in patterns:
                prio = p.get("priority", "low")
                if prio == "high":
                    priority_score += 3.0
                elif prio == "medium":
                    priority_score += 2.0
            score += min(priority_score, 4.0)
            # Directional agreement bonus (up to 2 pts)
            dirs = [p.get("direction") for p in patterns if p.get("direction") in ("bullish", "bearish")]
            if dirs:
                bull = dirs.count("bullish")
                bear = dirs.count("bearish")
                agreement = max(bull, bear) / len(dirs)
                score += agreement * 2.0
            return min(score, 10.0)

        if not isinstance(pattern_results, dict):
            return 0.0

        # Dict format handling (from PatternIntegration.scan())
        summary = pattern_results.get("summary", {})
        total_candlestick = 0
        if isinstance(summary, dict):
            total_candlestick = summary.get("total_candlestick", 0)

        if total_candlestick == 0:
            patterns = pattern_results.get("candlestick_patterns", [])
            total_candlestick = len(patterns) if isinstance(patterns, list) else 0

        if total_candlestick == 0:
            return 0.0

        score += min(total_candlestick * 2.0, 4.0)

        high_conf = pattern_results.get("high_confluence", [])
        if isinstance(high_conf, list) and len(high_conf) > 0:
            confs = [
                p.get("confidence", 0.0) for p in high_conf
                if isinstance(p, dict)
            ]
            if confs:
                avg_conf = sum(confs) / len(confs)
                score += avg_conf * 4.0

        patterns = pattern_results.get("candlestick_patterns", [])
        if isinstance(patterns, list):
            max_conf = max(
                (p.get("confidence", 0.0) for p in patterns if isinstance(p, dict)),
                default=0.0,
            )
            score += max_conf * 2.0

        return min(score, 10.0)

    def _score_chart(
        self, pattern_results: Dict[str, Any], regime: str
    ) -> float:
        """Score confirmed chart patterns with target.

        Max raw contribution: 10 points.

        Args:
            pattern_results: Output from ``PatternIntegration.scan()``.
            regime: Current ADX regime.

        Returns:
            Raw score 0-10.
        """
        score = 0.0

        if not isinstance(pattern_results, dict):
            return 0.0

        summary = pattern_results.get("summary", {})
        total_chart = 0
        if isinstance(summary, dict):
            total_chart = summary.get("total_chart", 0)

        chart_patterns = pattern_results.get("chart_patterns", pattern_results)
        if isinstance(chart_patterns, dict):
            # Count all patterns across sub-keys (reversals, continuations, etc.)
            all_patterns = []
            for key in ("confirmed", "reversals", "continuations", "formations"):
                pats = chart_patterns.get(key, [])
                if isinstance(pats, list):
                    all_patterns.extend(pats)
            confirmed = [p for p in all_patterns if isinstance(p, dict) and p.get("confirmed")]
            unconfirmed = [p for p in all_patterns if isinstance(p, dict) and not p.get("confirmed")]
            if confirmed:
                score += min(len(confirmed) * 4.0, 8.0)
                for p in confirmed:
                    score += p.get("confidence", 0.0) * 2.0
            if unconfirmed:
                # Unconfirmed but detected patterns have some value
                score += min(len(unconfirmed) * 1.5, 4.0)
        elif isinstance(chart_patterns, list) and len(chart_patterns) > 0:
            score += min(len(chart_patterns) * 3.0, 6.0)

        return min(score, 10.0)

    @staticmethod
    def _score_news(
        news_data: Optional[Dict[str, Any]], regime: str
    ) -> float:
        """Score news sentiment contribution.

        Max raw contribution: 5 points.
        Accepts multiple formats:
        - {"score": 0-5} — pre-computed score from trading_cycle
        - {"sentiment": {"sentiment": -1 to 1}} — raw from NewsIntelligence
        - {"overall_sentiment": -1 to 1} — from intelligence agent

        Returns 0 when a high-impact event is imminent (within 30 min).

        Args:
            news_data: News data in any supported format.
            regime: Current ADX regime.

        Returns:
            Float 0-5.
        """
        if news_data is None:
            return 0.0

        # Extract event risk -- suppress scoring during imminent events
        events = news_data.get("events", {})
        if isinstance(events, dict) and events.get("high_impact_within_30min", False):
            return 0.0

        # Format 1: raw sentiment magnitude (0-1), scale to 0-5
        if "score" in news_data:
            raw = abs(float(news_data["score"]))
            if raw <= 1.0:
                return min(raw * 5.0, 5.0)  # 0-1 range, scale up
            return min(raw, 5.0)  # Already 0-5 range

        # Format 2: overall_sentiment from intelligence agent (-1 to 1)
        overall = news_data.get("overall_sentiment")
        if overall is not None:
            return min(abs(float(overall)) * 5.0, 5.0)

        # Format 3: nested sentiment dict
        sentiment_data = news_data.get("sentiment", {})
        sentiment = sentiment_data.get("sentiment", 0.0) if isinstance(sentiment_data, dict) else 0.0

        return min(abs(float(sentiment)) * 5.0, 5.0)

    # ------------------------------------------------------------------
    # Direction determination
    # ------------------------------------------------------------------

    def _determine_direction(
        self,
        indicators: Dict[str, Any],
        advanced: Dict[str, Any],
        alignment_snapshot: Dict[str, Any],
        pattern_results: Dict[str, Any],
    ) -> str:
        """Determine overall trade direction from aggregate signals.

        Combines directional signals from EMA trend, MACD direction,
        Multi-TF alignment, and pattern direction.

        Args:
            indicators: Core indicators result dict.
            advanced: Advanced indicators result dict.
            alignment_snapshot: Alignment snapshot dict.
            pattern_results: Pattern results dict.

        Returns:
            ``'bullish'``, ``'bearish'``, or ``'neutral'``.
        """
        net_direction = 0.0

        # EMA trend signal (+/- 1.0)
        ema_trend = indicators.get("ema200_trend", indicators.get("ema", {}))
        if isinstance(ema_trend, dict):
            direction = ema_trend.get("direction", ema_trend.get("trend"))
            if direction == "bullish":
                net_direction += 1.0
            elif direction == "bearish":
                net_direction -= 1.0

        # MACD direction (+/- 1.0)
        macd_data = indicators.get("macd", {})
        if isinstance(macd_data, dict):
            crossover = macd_data.get("crossover")
            momentum = macd_data.get("momentum")
            if crossover == "bullish" or momentum == "positive":
                net_direction += 1.0
            elif crossover == "bearish" or momentum == "negative":
                net_direction -= 1.0

        # Multi-TF alignment score (+/- 1.0, scaled from [-1,1])
        if isinstance(alignment_snapshot, dict):
            alignment_score = alignment_snapshot.get("alignment_score")
            if alignment_score is None:
                alignment_data = alignment_snapshot.get("alignment", {})
                if isinstance(alignment_data, dict):
                    alignment_score = alignment_data.get("score")
            if alignment_score is not None:
                net_direction += float(alignment_score)
            else:
                # Check direction key directly
                direction = alignment_snapshot.get("direction")
                if direction == "bullish":
                    net_direction += 1.0
                elif direction == "bearish":
                    net_direction -= 1.0

        # Pattern dominant direction (+/- 0.5)
        summary = pattern_results.get("summary", {}) if isinstance(pattern_results, dict) else {}
        if isinstance(summary, dict):
            dominant = summary.get("dominant_direction")
            if dominant == "bullish":
                net_direction += 0.5
            elif dominant == "bearish":
                net_direction -= 0.5

        # Classify
        if net_direction > 0.3:
            return "bullish"
        elif net_direction < -0.3:
            return "bearish"
        else:
            return "neutral"
