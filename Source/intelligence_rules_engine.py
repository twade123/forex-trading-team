"""
Programmatic decision rules that complement the LLM validator.

These rules run BEFORE the LLM call to pre-compute confidence adjustments and
flags from cached intelligence data.  The LLM receives the summary as
pre-computed context — it does not re-interpret the raw rules.

All rules operate on the dict returned by
``get_cached_intelligence_for_validator()`` plus the proposed trade direction.

Design principles
-----------------
- Rules are PASSIVE: they read from cached DB data, never fetch live data.
- Rules are ORDERED: evaluate_all_rules() runs them in rule-number order.
- Rules are COMPOSABLE: each returns a RuleResult; summarize_rules() merges.
- Rules DEGRADE gracefully: missing data → triggered=False, detail explains why.
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class RuleResult:
    """Outcome of a single rule evaluation."""

    rule_name: str
    triggered: bool
    confidence_adjustment: int = 0    # Positive = boost, negative = reduce
    flag: Optional[str] = None
    position_size_factor: float = 1.0  # 1.0 = full size, 0.5 = half size
    detail: str = ""


def evaluate_all_rules(
    pair: str,
    trade_direction: str,   # "buy" or "sell"
    package: dict,          # from get_cached_intelligence_for_validator()
    ta_confluence: float,   # 0-100 confluence score from TA system
    **kwargs,               # Accept and ignore legacy params (swarm_weight, etc.)
) -> List[RuleResult]:
    """
    Evaluate all intelligence rules and return the list of RuleResult objects.

    Called by the validator orchestrator right before the LLM prompt is built.
    Results are summarised via summarize_rules() and injected into the prompt.

    Args:
        pair:           OANDA instrument string, e.g. "EUR_USD".
        trade_direction: "buy" or "sell".
        package:        Intelligence package dict from DB lookup.
        ta_confluence:  TA confluence score (0-100) for the proposed trade.

    Returns:
        List of RuleResult, one per evaluated rule.
    """
    results: List[RuleResult] = []

    # Rule 1 — Calendar veto
    results.append(_rule_calendar_veto(pair, package))

    # Rule 2 — VIX regime (position size)
    results.append(_rule_vix_regime(package))

    # Rule 3 — COT squeeze risk
    results.append(_rule_cot_squeeze(pair, trade_direction, package))

    # Rule 4 — Recent performance context
    results.append(_rule_recent_performance(pair, package))

    # Rule 5 — News sentiment alignment
    results.append(_rule_news_sentiment(pair, trade_direction, package))

    # Rule 6 — User thesis / annotation check
    results.append(_rule_user_thesis(pair, trade_direction, package))

    # Rule 7 — Cross-asset confirmation
    results.append(_rule_cross_asset(pair, trade_direction, package))

    return results


def summarize_rules(results: List[RuleResult]) -> dict:
    """
    Collapse a list of RuleResult objects into a single summary dict for the
    LLM prompt and for recording in validator_decisions.

    Returns:
        Dict with keys:
        - total_confidence_adjustment (int)
        - flags (list of str)
        - position_size_factor (float, lowest factor wins)
        - position_size_label (str)
        - rules_triggered (list of dicts)
        - rules_clear (list of str)
    """
    total_confidence_adj = sum(r.confidence_adjustment for r in results)
    flags = [r.flag for r in results if r.flag]
    min_position_factor = min(
        (r.position_size_factor for r in results), default=1.0
    )

    return {
        "total_confidence_adjustment": total_confidence_adj,
        "flags": flags,
        "position_size_factor": min_position_factor,
        "position_size_label": _factor_to_label(min_position_factor),
        "rules_triggered": [
            {
                "rule": r.rule_name,
                "adjustment": r.confidence_adjustment,
                "detail": r.detail,
                "flag": r.flag,
            }
            for r in results
            if r.triggered
        ],
        "rules_clear": [r.rule_name for r in results if not r.triggered],
    }


# ---------------------------------------------------------------------------
# Individual rule implementations
# ---------------------------------------------------------------------------


def _rule_calendar_veto(pair: str, package: dict) -> RuleResult:
    """Rule 1: High-impact economic event within expected trade duration."""
    calendar_data = package.get("calendar", {})
    events = calendar_data.get("events", [])
    pair_currencies = set(pair.split("_"))

    # Flag any high-impact events for this pair's currencies within 4 hours
    imminent_high = [
        e for e in events
        if e.get("impact") == "high"
        and e.get("currency") in pair_currencies
        and isinstance(e.get("hours_away"), (int, float))
        and e["hours_away"] < 4
    ]

    if imminent_high:
        evt = imminent_high[0]
        return RuleResult(
            rule_name="calendar_veto",
            triggered=True,
            confidence_adjustment=-20,
            flag=(
                f"HIGH_IMPACT_EVENT: {evt.get('event_name', 'Unknown')} "
                f"in {evt.get('hours_away', '?')}h "
                f"({evt.get('currency', '?')})"
            ),
            detail=(
                f"High-impact event '{evt.get('event_name')}' for "
                f"{evt.get('currency')} within ~{evt.get('hours_away')}h "
                f"— consider HOLD unless trade plays the outcome directly."
            ),
        )

    return RuleResult(
        rule_name="calendar_veto",
        triggered=False,
        detail="Calendar clear for expected trade duration (no high-impact < 4h)",
    )


def _rule_vix_regime(package: dict) -> RuleResult:
    """Rule 2: VIX level determines position size cap and entry threshold."""
    cross_asset = package.get("cross_asset", {})
    vix_data = cross_asset.get("vix", {})
    vix_level = vix_data.get("current_price", 0) or 0

    if vix_level > 30:
        return RuleResult(
            rule_name="vix_regime",
            triggered=True,
            confidence_adjustment=-10,
            flag=f"VIX_EXTREME: {vix_level:.1f}",
            position_size_factor=0.25,
            detail=(
                f"VIX at {vix_level:.1f} (extreme volatility). "
                "75% position reduction recommended. "
                "Only accept trades with confluence >90."
            ),
        )

    if vix_level > 25:
        return RuleResult(
            rule_name="vix_regime",
            triggered=True,
            flag=f"VIX_HIGH: {vix_level:.1f}",
            position_size_factor=0.50,
            detail=(
                f"VIX at {vix_level:.1f} (high volatility). "
                "50% position reduction. Require confluence >80."
            ),
        )

    return RuleResult(
        rule_name="vix_regime",
        triggered=False,
        detail=f"VIX at {vix_level:.1f} (normal regime)",
    )


def _rule_cot_squeeze(pair: str, direction: str, package: dict) -> RuleResult:
    """Rule 3: COT extreme positioning and squeeze risk assessment."""
    base_ccy = pair.split("_")[0]
    quote_ccy = pair.split("_")[1]
    # For USD pairs, we care about the non-USD currency's COT
    target_ccy = base_ccy if base_ccy != "USD" else quote_ccy

    cot_data = package.get("cot", {})
    cot = cot_data.get(target_ccy, {})

    if not cot or cot.get("positioning_signal", "neutral") == "neutral":
        return RuleResult(
            rule_name="cot_squeeze",
            triggered=False,
            detail=f"COT positioning neutral for {target_ccy}",
        )

    signal = cot.get("positioning_signal", "neutral")
    percentile = cot.get("percentile", "?")
    trade_is_bullish_base = direction == "buy"

    # Going AGAINST extreme crowd: e.g., buying where specs are max-short
    if signal == "extreme_short" and trade_is_bullish_base:
        return RuleResult(
            rule_name="cot_squeeze",
            triggered=True,
            confidence_adjustment=-10,
            flag=f"COT_COUNTER_EXTREME: {target_ccy} specs extremely short, trade is long",
            detail=(
                f"{target_ccy} specs at {percentile}th percentile (extreme short). "
                "Buying against a crowded short — high squeeze risk. "
                "Requires strong breakout catalyst, not pattern speculation."
            ),
        )

    # Trading WITH the crowded position: squeezable if catalyst fires
    fighting_squeeze = (
        (signal == "extreme_short" and not trade_is_bullish_base)
        or (signal == "extreme_long" and trade_is_bullish_base)
    )

    if fighting_squeeze:
        return RuleResult(
            rule_name="cot_squeeze",
            triggered=True,
            confidence_adjustment=-5,
            flag=f"COT_CROWDED: {target_ccy} specs {signal}",
            position_size_factor=0.75,
            detail=(
                f"{target_ccy} crowded ({signal}, {percentile}th percentile). "
                "Position size reduced 25%. "
                "If unwind triggers, move could be violent against us."
            ),
        )

    return RuleResult(
        rule_name="cot_squeeze",
        triggered=False,
        detail=f"COT: {target_ccy} {signal} ({percentile}th pct) — no squeeze risk for this direction",
    )


def _rule_recent_performance(pair: str, package: dict) -> RuleResult:
    """Rule 4: Consecutive loss streak on this pair penalises confidence."""
    pair_data = package.get("pair_data", {})
    recent = pair_data.get("recent_trades", {})

    if not recent:
        return RuleResult(
            rule_name="recent_performance",
            triggered=False,
            detail="No recent trade data for this pair",
        )

    trades = recent.get("recent_trades", [])
    if not trades:
        return RuleResult(
            rule_name="recent_performance",
            triggered=False,
            detail="No recent trades recorded",
        )

    summary = recent.get("summary", {})
    streak = str(summary.get("streak", ""))

    # Losing streak of 3+
    if streak.startswith("L"):
        try:
            count = int(streak[1:])
        except ValueError:
            count = 0

        if count >= 3:
            return RuleResult(
                rule_name="recent_performance",
                triggered=True,
                confidence_adjustment=-5,
                flag=f"COLD_STREAK: {streak} on {pair}",
                detail=f"Losing streak ({streak}) on {pair} — exercise caution",
            )

    # Check if last trade used same setup type and lost
    last_trade = trades[0] if trades else {}
    if last_trade.get("result") == "L" and last_trade.get("setup_type"):
        # Check if proposed trade is same setup type (not available here — skip penalty)
        pass

    return RuleResult(
        rule_name="recent_performance",
        triggered=False,
        detail=f"Recent streak: {streak or 'N/A'} — no cold streak penalty",
    )


def _rule_news_sentiment(pair: str, direction: str, package: dict) -> RuleResult:
    """Rule 5: Strong opposing news sentiment adds a narrative headwind flag."""
    pair_data = package.get("pair_data", {})
    news_data = pair_data.get("news", {})
    agg_sentiment = news_data.get("aggregate_sentiment", {})

    score = agg_sentiment.get("score", 0)
    label = agg_sentiment.get("label", "neutral")

    trade_is_bullish = direction == "buy"
    sentiment_is_bearish = score < -0.3
    sentiment_is_bullish = score > 0.3

    opposing = (
        (trade_is_bullish and sentiment_is_bearish)
        or (not trade_is_bullish and sentiment_is_bullish)
    )

    if opposing and abs(score) > 0.5:
        return RuleResult(
            rule_name="news_sentiment",
            triggered=True,
            flag=(
                f"NEWS_HEADWIND: sentiment {label} (score {score:.2f}) "
                f"vs {'buy' if trade_is_bullish else 'sell'} trade"
            ),
            detail=(
                f"Narrative headwind — news sentiment ({label}, {score:.2f}) "
                "opposes trade direction. Not an auto-reject but requires "
                "strong TA justification."
            ),
        )

    sentiment_dir = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    aligned = (trade_is_bullish and sentiment_is_bullish) or (
        not trade_is_bullish and sentiment_is_bearish
    )
    align_str = "ALIGNED" if aligned else "NEUTRAL"

    return RuleResult(
        rule_name="news_sentiment",
        triggered=False,
        detail=f"News sentiment: {label} (score {score:.2f}) — {align_str} with trade direction",
    )


def _rule_user_thesis(pair: str, direction: str, package: dict) -> RuleResult:
    """Rule 6: User chart annotations / active watches conflict check."""
    pair_data = package.get("pair_data", {})
    annotations = pair_data.get("user_chart_annotations", [])
    active_watches = pair_data.get("active_watches", [])

    flags = []
    detail_parts = []

    # Check if trade contradicts user's annotated thesis
    for ann in annotations:
        ann_direction = ann.get("direction", "").lower()
        if ann_direction and ann_direction != direction:
            flags.append(
                f"USER_THESIS_CONFLICT: annotation is '{ann_direction}', "
                f"trade is '{direction}'"
            )
            detail_parts.append(
                f"User annotation '{ann.get('label', 'N/A')}' points "
                f"{ann_direction} — trade goes {direction}. Highlight conflict."
            )

    # Check active watches
    for watch in active_watches:
        watch_dir = watch.get("direction", "").lower()
        watch_triggered = watch.get("triggered", False)
        if watch_triggered:
            detail_parts.append(
                f"Active watch has triggered: {watch.get('condition', 'N/A')}"
            )
        elif watch_dir and watch_dir != direction:
            detail_parts.append(
                f"Active watch is for {watch_dir} direction "
                f"(trade is {direction}) — note the disagreement."
            )

    if flags:
        return RuleResult(
            rule_name="user_thesis",
            triggered=True,
            flag=flags[0],
            detail=" | ".join(detail_parts) if detail_parts else flags[0],
        )

    return RuleResult(
        rule_name="user_thesis",
        triggered=False,
        detail=(
            "No user annotation conflicts"
            + (f" — {len(active_watches)} active watch(es) noted" if active_watches else "")
        ),
    )


def _rule_cross_asset(pair: str, direction: str, package: dict) -> RuleResult:
    """Rule 7: Cross-asset dashboard confirmation / contradiction scan."""
    cross_asset = package.get("cross_asset", {})
    if not cross_asset:
        return RuleResult(
            rule_name="cross_asset",
            triggered=False,
            detail="Cross-asset data not available",
        )

    trade_is_bullish = direction == "buy"
    base_ccy = pair.split("_")[0]
    quote_ccy = pair.split("_")[1]
    contradictions = []

    # DXY direction check for USD pairs
    dxy = cross_asset.get("dxy", {})
    dxy_change = dxy.get("change_pct", 0) or 0

    usd_in_pair = "USD" in (base_ccy, quote_ccy)
    if usd_in_pair:
        usd_is_base = base_ccy == "USD"
        trade_usd_bullish = (trade_is_bullish and usd_is_base) or (
            not trade_is_bullish and not usd_is_base
        )
        dxy_bullish = dxy_change > 0.1

        if trade_usd_bullish and not dxy_bullish and abs(dxy_change) > 0.2:
            contradictions.append(
                f"DXY declining ({dxy_change:+.2f}%) but trade implies USD strength"
            )
        elif not trade_usd_bullish and dxy_bullish and abs(dxy_change) > 0.2:
            contradictions.append(
                f"DXY rising ({dxy_change:+.2f}%) but trade implies USD weakness"
            )

    # Gold / risk-off check
    gold = cross_asset.get("gold", {})
    gold_change = gold.get("change_pct", 0) or 0
    if gold_change > 0.5:  # Gold rising = risk-off
        risk_off_pairs = {"JPY", "CHF"}  # Strengthen in risk-off
        risk_on_pairs = {"AUD", "NZD"}   # Weaken in risk-off
        if base_ccy in risk_on_pairs and trade_is_bullish:
            contradictions.append(
                f"Gold rising (risk-off) but trade is long {base_ccy} (risk-on ccy)"
            )
        elif base_ccy in risk_off_pairs and not trade_is_bullish:
            contradictions.append(
                f"Gold rising (risk-off) but trade is short {base_ccy} (safe-haven ccy)"
            )

    # Equity risk-on signal (S&P)
    spx = cross_asset.get("spx", {})
    spx_change = spx.get("change_pct", 0) or 0
    if abs(spx_change) > 0.5:
        spx_risk_on = spx_change > 0
        risk_on_pairs = {"AUD", "NZD", "GBP"}
        if base_ccy in risk_on_pairs:
            if spx_risk_on and not trade_is_bullish:
                contradictions.append(
                    f"Equities up (risk-on) but trade is short {base_ccy} (risk-on ccy)"
                )
            elif not spx_risk_on and trade_is_bullish:
                contradictions.append(
                    f"Equities down (risk-off) but trade is long {base_ccy} (risk-on ccy)"
                )

    # Oil / CAD check
    oil = cross_asset.get("oil", {})
    oil_change = oil.get("change_pct", 0) or 0
    if base_ccy == "CAD" and abs(oil_change) > 0.5:
        if oil_change < -0.5 and trade_is_bullish:
            contradictions.append(
                f"Oil falling ({oil_change:+.2f}%) but trade is long CAD"
            )
        elif oil_change > 0.5 and not trade_is_bullish:
            contradictions.append(
                f"Oil rising ({oil_change:+.2f}%) but trade is short CAD"
            )

    if len(contradictions) >= 3:
        return RuleResult(
            rule_name="cross_asset",
            triggered=True,
            flag=f"CROSS_ASSET_DIVERGENCE: {len(contradictions)} signals contradict trade",
            detail=(
                "Cross-asset divergence — multiple signals oppose trade direction: "
                + " | ".join(contradictions)
            ),
        )

    if contradictions:
        return RuleResult(
            rule_name="cross_asset",
            triggered=False,
            detail=(
                f"Minor cross-asset divergence ({len(contradictions)} signal(s)): "
                + " | ".join(contradictions)
            ),
        )

    return RuleResult(
        rule_name="cross_asset",
        triggered=False,
        detail="Cross-asset signals consistent with trade direction",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _factor_to_label(factor: float) -> str:
    """Convert a position size factor to a human-readable label."""
    if factor >= 1.0:
        return "standard"
    if factor >= 0.75:
        return "reduced_25pct"
    if factor >= 0.50:
        return "reduced_50pct"
    return "reduced_75pct"
