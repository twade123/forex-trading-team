"""
Strategy engine for the trading bot.

Wraps :class:`ConfluenceScorer` with trade-decision gates that filter
out low-probability setups.  The three layers on top of confluence
scoring are:

- **RSI gate (STRT-04):**  Blocks longs when RSI > 70 and shorts when
  RSI < 30, regardless of how high the confluence score is.
- **Session awareness (STRT-05):**  Classifies the current market
  session as ``prime`` (London-NY overlap), ``active`` (any session),
  or ``quiet`` (no sessions) and applies a score adjustment.
- **Asian range (STRT-06):**  Calculates the high/low range from the
  Tokyo session for London-breakout reference.

Primary entry point is :meth:`StrategyEngine.evaluate`, which returns
a complete trade-decision dict with ``action``, gate results, session
context, and human-readable reasons.

Usage:
    from trading_bot.source.strategy_engine import StrategyEngine

    se = StrategyEngine()
    decision = se.evaluate(
        candles=candles,
        indicators_result=ind.compute_all(),
        advanced_result=adv.compute_all(),
        alignment_snapshot=mta.get_snapshot(),
        pattern_results=pi.scan(),
    )
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from .confluence_scorer import ConfluenceScorer


class StrategyEngine:
    """Trade decision engine with RSI gating and session awareness.

    Composes (not inherits) :class:`ConfluenceScorer` and optionally an
    :class:`AccountManager` to produce final buy/sell/hold decisions.
    All gates are independent -- each produces its own pass/fail with a
    human-readable reason.

    Args:
        confluence_scorer: Pre-configured scorer.  If *None* a default
            :class:`ConfluenceScorer` is created.
        account_manager: Optional :class:`AccountManager` for session
            detection.  Session features degrade gracefully without it.
        config: Optional dict overriding default thresholds.
    """

    # Default thresholds (all tuneable via config dict)
    _DEFAULT_CONFIG: Dict[str, Any] = {
        "min_score": 70,            # STRT-02: minimum confluence for trade
        "rsi_long_block": 70,       # STRT-04: RSI above this blocks longs
        "rsi_short_block": 30,      # STRT-04: RSI below this blocks shorts
        "session_bonus": 5,         # Extra score during London-NY overlap
        "quiet_penalty": 10,        # Score reduction outside active sessions
        "asian_lookback_hours": 9,  # 7 PM - 4 AM ET = 9 hours
    }

    def __init__(
        self,
        confluence_scorer: Optional[ConfluenceScorer] = None,
        account_manager: Optional[Any] = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self._scorer = confluence_scorer or ConfluenceScorer()
        self._account_manager = account_manager
        self._config = {**self._DEFAULT_CONFIG, **(config or {})}

    # ------------------------------------------------------------------
    # RSI Gate (STRT-04)
    # ------------------------------------------------------------------

    def _check_rsi_gate(
        self, rsi_value: Optional[float], direction: str
    ) -> Dict[str, Any]:
        """Check whether the RSI gate allows a trade in *direction*.

        Args:
            rsi_value: Current RSI reading, or *None* if unavailable.
            direction: ``'bullish'``, ``'bearish'``, or ``'neutral'``.

        Returns:
            Dict with ``passed`` (bool), ``rsi_value`` (float|None),
            and ``reason`` (str|None explaining a block).
        """
        # Graceful degradation -- if RSI not available, gate passes
        if rsi_value is None:
            return {"passed": True, "rsi_value": None, "reason": None}

        rsi_long_block = self._config["rsi_long_block"]
        rsi_short_block = self._config["rsi_short_block"]

        if direction == "bullish" and rsi_value > rsi_long_block:
            return {
                "passed": False,
                "rsi_value": rsi_value,
                "reason": (
                    f"RSI {rsi_value:.1f} > {rsi_long_block}: longs blocked"
                ),
            }

        if direction == "bearish" and rsi_value < rsi_short_block:
            return {
                "passed": False,
                "rsi_value": rsi_value,
                "reason": (
                    f"RSI {rsi_value:.1f} < {rsi_short_block}: shorts blocked"
                ),
            }

        # Neutral direction or RSI in acceptable range
        return {"passed": True, "rsi_value": rsi_value, "reason": None}

    # ------------------------------------------------------------------
    # Session Awareness (STRT-05)
    # ------------------------------------------------------------------

    def _get_session_context(
        self, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Classify the current session and compute score adjustment.

        Args:
            now: Optional datetime for deterministic testing.

        Returns:
            Dict with ``session_quality``, ``active_sessions``,
            ``is_overlap``, ``market_open``, and ``score_adjustment``.
        """
        # If no AccountManager, return neutral context (no adjustment)
        if self._account_manager is None:
            return {
                "session_quality": "active",
                "active_sessions": [],
                "is_overlap": False,
                "market_open": True,
                "score_adjustment": 0.0,
            }

        am = self._account_manager
        market_open = am.is_market_open(now)
        is_overlap = am.is_london_ny_overlap(now)
        active_sessions = am.get_current_session(now)

        if is_overlap:
            quality = "prime"
            adjustment = float(self._config["session_bonus"])
        elif len(active_sessions) > 0:
            quality = "active"
            adjustment = 0.0
        else:
            quality = "quiet"
            adjustment = -float(self._config["quiet_penalty"])

        return {
            "session_quality": quality,
            "active_sessions": active_sessions,
            "is_overlap": is_overlap,
            "market_open": market_open,
            "score_adjustment": adjustment,
        }

    # ------------------------------------------------------------------
    # Asian Session Range (STRT-06)
    # ------------------------------------------------------------------

    def _compute_asian_range(
        self,
        candles: List[Dict[str, Any]],
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Compute the Asian (Tokyo) session high/low range.

        Asian session: 7:00 PM - 4:00 AM ET (9 hours).  Filters candles
        within the most recent Asian window and returns the highest high
        and lowest low for London-breakout reference.

        Args:
            candles: Raw Oanda candle list (same format as upstream).
            now: Optional datetime for deterministic testing.

        Returns:
            Dict with ``high``, ``low``, ``range_pips`` (all float|None),
            and ``computed`` (bool).
        """
        if not candles:
            return {
                "high": None,
                "low": None,
                "range_pips": None,
                "computed": False,
            }

        try:
            from zoneinfo import ZoneInfo

            et = ZoneInfo("America/New_York")

            if now is None:
                now = datetime.now(et)
            elif now.tzinfo is None:
                now = now.replace(tzinfo=et)
            else:
                now = now.astimezone(et)

            # Determine the most recent Asian window boundaries
            # Asian session: 7 PM - 4 AM ET
            # If current hour >= 4 AM, the last Asian session ended today at 4 AM
            # If current hour < 4 AM, we're in the Asian session (started yesterday 7 PM)
            if now.hour >= 4:
                # Last Asian session ended today at 4 AM
                asian_end = now.replace(hour=4, minute=0, second=0, microsecond=0)
                asian_start = asian_end.replace(
                    day=asian_end.day, hour=19, minute=0, second=0, microsecond=0
                )
                # asian_start is yesterday at 7 PM
                from datetime import timedelta

                asian_start = asian_start - timedelta(days=1)
            else:
                # Currently in Asian session or before it ended
                asian_end = now.replace(hour=4, minute=0, second=0, microsecond=0)
                from datetime import timedelta

                asian_start = (
                    asian_end - timedelta(days=1)
                ).replace(hour=19, minute=0, second=0, microsecond=0)

            # Filter candles within the Asian window
            asian_highs: List[float] = []
            asian_lows: List[float] = []

            for candle in candles:
                time_str = candle.get("time", "")
                if not time_str:
                    continue

                try:
                    import pandas as pd

                    candle_time = pd.Timestamp(time_str).to_pydatetime()
                    if candle_time.tzinfo is None:
                        from zoneinfo import ZoneInfo as ZI

                        candle_time = candle_time.replace(tzinfo=ZI("UTC"))
                    candle_et = candle_time.astimezone(et)
                except (ValueError, TypeError):
                    continue

                if asian_start <= candle_et <= asian_end:
                    mid = candle.get("mid", {})
                    if mid:
                        try:
                            asian_highs.append(float(mid.get("h", 0)))
                            asian_lows.append(float(mid.get("l", 0)))
                        except (ValueError, TypeError):
                            continue

            if asian_highs and asian_lows:
                high = max(asian_highs)
                low = min(asian_lows)
                return {
                    "high": high,
                    "low": low,
                    "range_pips": high - low,
                    "computed": True,
                }

        except Exception:
            pass

        return {
            "high": None,
            "low": None,
            "range_pips": None,
            "computed": False,
        }

    # ------------------------------------------------------------------
    # Main Decision Method
    # ------------------------------------------------------------------

    def evaluate(
        self,
        candles: List[Dict[str, Any]],
        indicators_result: Optional[Dict[str, Any]] = None,
        advanced_result: Optional[Dict[str, Any]] = None,
        alignment_snapshot: Optional[Dict[str, Any]] = None,
        pattern_results: Optional[Dict[str, Any]] = None,
        news_data: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Produce a final trade decision from all available signals.

        Orchestrates the full decision pipeline:
        1. Compute confluence score via :class:`ConfluenceScorer`.
        2. Get session context and apply score adjustment.
        3. Check RSI gate against the determined direction.
        4. Compute Asian range for breakout reference.
        5. Apply the decision tree to produce buy/sell/hold.

        Args:
            candles: Raw Oanda candle list.
            indicators_result: Output from ``Indicators.compute_all()``.
            advanced_result: Output from ``AdvancedIndicators.compute_all()``.
            alignment_snapshot: Output from
                ``MultiTimeframeAlignment.get_snapshot()``.
            pattern_results: Output from ``PatternIntegration.scan()``.
            news_data: Optional news sentiment data (Phase 6 stub).
            now: Optional datetime for deterministic testing.

        Returns:
            Dict with ``action``, ``confluence``, ``rsi_gate``,
            ``session``, ``asian_range``, ``adjusted_score``,
            ``reasons``, and ``tradeable``.
        """
        reasons: List[str] = []

        # 1. Confluence score
        confluence = self._scorer.compute_score(
            indicators_result=indicators_result,
            advanced_result=advanced_result,
            alignment_snapshot=alignment_snapshot,
            pattern_results=pattern_results,
            news_data=news_data,
        )

        raw_score = confluence["total_score"]
        direction = confluence["direction"]

        # 2. Session context and score adjustment
        session = self._get_session_context(now)
        adjusted_score = raw_score + session["score_adjustment"]

        if session["score_adjustment"] > 0:
            reasons.append(
                f"Session bonus +{session['score_adjustment']:.0f} "
                f"({session['session_quality']})"
            )
        elif session["score_adjustment"] < 0:
            reasons.append(
                f"Session penalty {session['score_adjustment']:.0f} "
                f"({session['session_quality']})"
            )

        # 3. RSI gate
        rsi_data = (indicators_result or {}).get("rsi", {})
        rsi_value = rsi_data.get("value") if isinstance(rsi_data, dict) else None
        rsi_gate = self._check_rsi_gate(rsi_value, direction)

        # 4. Asian range (informational for risk management in Phase 8)
        asian_range = self._compute_asian_range(candles, now)

        # 5. Decision tree
        action = "hold"
        min_score = self._config["min_score"]

        if not session["market_open"]:
            action = "hold"
            reasons.append("Market closed")
        elif not rsi_gate["passed"]:
            action = "hold"
            reasons.append(rsi_gate["reason"])
        elif adjusted_score < min_score:
            action = "hold"
            reasons.append(
                f"Score {adjusted_score:.1f} below threshold {min_score}"
            )
        elif direction == "neutral":
            action = "hold"
            reasons.append("No directional bias")
        elif direction == "bullish":
            action = "buy"
            reasons.append(
                f"Bullish signal, score {adjusted_score:.1f} "
                f"(regime: {confluence['regime']})"
            )
        elif direction == "bearish":
            action = "sell"
            reasons.append(
                f"Bearish signal, score {adjusted_score:.1f} "
                f"(regime: {confluence['regime']})"
            )

        if asian_range["computed"]:
            reasons.append(
                f"Asian range: {asian_range['high']:.5f}-"
                f"{asian_range['low']:.5f} "
                f"({asian_range['range_pips']:.5f} pips)"
            )

        return {
            "action": action,
            "confluence": confluence,
            "rsi_gate": rsi_gate,
            "session": session,
            "asian_range": asian_range,
            "adjusted_score": adjusted_score,
            "reasons": reasons,
            "tradeable": action != "hold",
        }
