"""pattern_library_quotes.py — Verbatim text snippets from
<repo_root>/Skills/pattern_library.md,
keyed by pattern_id. Used to dynamically inject pattern entries into the
validator prompt when a detector fires on a given trade.

Source of truth: pattern_library.md. When that file changes, update here.
"""

PATTERN_QUOTES = {
    "pattern_01": (
        "**pattern_01 Hammer / Pin Bar**\n"
        "Type: single candle reversal (bullish or bearish mirror).\n"
        "Detection: wick ≥ 2× body on one side; opposite wick < body; "
        "at swing extreme or near E55/E100.\n"
        "Bias: bullish (hammer at support) / bearish (star at resistance).\n"
        "Entry: next candle close past the body in reversal direction.\n"
        "Invalidation: close back through the wick extreme.\n"
        "Reliability: HIGH at key S/R, MEDIUM random."
    ),
    "pattern_02": (
        "**pattern_02 Bullish Engulfing**\n"
        "Type: 2-candle reversal.\n"
        "Structure: small red candle → large green candle whose body wraps "
        "the red body entirely.\n"
        "Detection: open_now ≤ close_prev AND close_now ≥ open_prev. "
        "Second candle green, first red.\n"
        "Entry: close of engulfing candle. Invalidation: below engulfing low.\n"
        "Target: prior swing high or BB upper.\n"
        "Reliability: HIGH — one of the most reliable reversal patterns."
    ),
    "pattern_03": (
        "**pattern_03 Bearish Engulfing**\n"
        "Type: 2-candle reversal (mirror).\n"
        "Structure: small green → large red engulfing the green body.\n"
        "Detection: open_now ≥ close_prev AND close_now ≤ open_prev. "
        "Second candle red, first green.\n"
        "Entry: close of engulfing candle. Invalidation: above engulfing high."
    ),
    "pattern_04": (
        "**pattern_04 Morning / Evening Star**\n"
        "Type: 3-candle reversal.\n"
        "Morning star (bullish): large red → small body/doji → large green "
        "closing above midpoint of first.\n"
        "Evening star (bearish): large green → small body/doji → large red "
        "closing below midpoint of first.\n"
        "Entry: close of third candle. Invalidation: beyond the star's extreme.\n"
        "Reliability: HIGH — strongest 3-candle reversal."
    ),
    "pattern_05": (
        "**pattern_05 Doji at Extreme**\n"
        "Type: indecision / reversal pending.\n"
        "Structure: body ≤ 10% of total range.\n"
        "Variants: Dragonfly (long lower wick, no upper) = bullish at support; "
        "Gravestone (long upper wick, no lower) = bearish at resistance; "
        "Long-legged = extreme indecision, big move coming; "
        "Standard = pure indecision, needs confirmation.\n"
        "Context matters: doji at BB extreme / RSI extreme / swing high-low "
        "= real signal. Doji mid-range = noise."
    ),
    "pattern_06": (
        "**pattern_06 Ascending Triangle**\n"
        "Type: continuation pattern (bullish).\n"
        "Structure: flat horizontal top (resistance), higher lows compressing into it.\n"
        "Detection: flat resistance (3+ tests), higher lows compressing into it.\n"
        "Confirmation: decisive close above the flat top.\n"
        "Target: triangle height projected up from breakout.\n"
        "Bias: bullish continuation (or breakout after range)."
    ),
    "pattern_07": (
        "**pattern_07 Descending Triangle**\n"
        "Type: continuation pattern (bearish, mirror of ascending).\n"
        "Structure: flat horizontal bottom (support), lower highs compressing down to it.\n"
        "Confirmation: decisive close below the flat bottom.\n"
        "Target: triangle height projected down.\n"
        "Bias: bearish continuation."
    ),
    "pattern_08": (
        "**pattern_08 Channel Trading**\n"
        "Type: range / parallel channel.\n"
        "Structure: price oscillating between parallel support + resistance lines.\n"
        "Trade: fade extremes with confirmation candle; avoid middle; "
        "take profit near opposite band.\n"
        "Invalidation: decisive break of either bound (channel breakout).\n"
        "Best use: M15 channel within larger H1/H4 trend — fade counter-trend "
        "side, trade with-trend side heavier."
    ),
    "pattern_10": (
        "**pattern_10 BB Squeeze Breakout (Tim's #1 setup)**\n"
        "Type: volatility expansion trade.\n"
        "Structure: Bollinger Bands compress to tight bandwidth for ≥10 M15 bars, "
        "then price decisively pierces one band.\n"
        "Detection: bandwidth < 50% of 20-bar average sustained ≥10 bars; "
        "price closes beyond band by ≥ 0.5 × current bandwidth; "
        "EMA fan aligned with break direction.\n"
        "Bias: directional (follows the break direction).\n"
        "Entry: on break confirmation close. Invalidation: close back inside bands within 3 bars.\n"
        "Target: 2-3× bandwidth projected from break.\n"
        "Tim's note: bearish version often preceded by double top at E100 + E21 crossing below E55."
    ),
    "pattern_11": (
        "**pattern_11 RSI / MACD Divergence**\n"
        "Type: leading reversal (regular) or continuation (hidden) signal.\n"
        "Regular bearish: price HIGHER high, indicator LOWER high → reversal down.\n"
        "Regular bullish: price LOWER low, indicator HIGHER low → reversal up.\n"
        "Hidden bullish: price HIGHER low, indicator LOWER low → uptrend continues.\n"
        "Hidden bearish: price LOWER high, indicator HIGHER high → downtrend continues.\n"
        "Detection: need 2 swing points on price + matching 2 points on indicator.\n"
        "Trade: wait for price-level confirmation before entering.\n"
        "Reliability: HIGH — #1 leading reversal signal per validator encyclopedia."
    ),
}


def _render_context(ctx: dict) -> str:
    """Render the per-fire indicator context into a clean bullet block."""
    if not ctx:
        return ""
    nearest = ctx.get("nearest_ema_within_8pips")
    loc_line = f"near {nearest}" if nearest else "mid-range (not at any EMA)"
    bb_pos = ctx.get("bb_position", "unknown").replace("_", " ")
    rsi_v = ctx.get("rsi_at_fire")
    rsi_z = ctx.get("rsi_zone", "unknown")
    rsi_line = f"{rsi_v} ({rsi_z})" if rsi_v is not None else "unknown"
    trend = ctx.get("trend_alignment", "unknown")
    conf = ctx.get("confirmation_status", "unknown")
    invld = ctx.get("invalidation_status", "unknown")
    swing = "✓" if ctx.get("at_swing_extreme") else "✗"
    return (
        f"  - Location: {loc_line} | BB: {bb_pos} | at swing extreme: {swing}\n"
        f"  - RSI at fire: {rsi_line}\n"
        f"  - Trend alignment: **{trend}**\n"
        f"  - Confirmation candle: **{conf}**\n"
        f"  - Invalidation status: **{invld}**"
    )


def _interpretation(trend: str, conf: str, invld: str, swing_extreme: bool,
                    nearest_ema: str | None, bb_pos: str) -> str:
    """One-line evidence summary. Tells the model the pattern's strength but does
    NOT prescribe a verdict — that's the model's job once it knows the trade direction.

    Counter-trend reversal patterns at the right location ARE warnings against the
    in-trend trade — they are NOT noise to ignore. The model decides whether to
    apply them as support (matching direction) or warning (opposite direction).
    """
    if invld == "invalidated":
        return "⚠️ PATTERN INVALIDATED — price has closed through trigger level, ignore."
    location_strong = (nearest_ema is not None) or bb_pos in ("at_upper_band", "at_lower_band") or swing_extreme
    has_confirmation = conf == "confirmed"
    in_trend = "IN_TREND" in trend
    counter_trend = "COUNTER_TREND" in trend

    # Build evidence summary
    parts = []
    if in_trend:
        parts.append("in-trend (HIGH reliability per Bulkowski)")
    if counter_trend:
        parts.append("counter-trend reversal pattern (potential trend-flip signal — treat as WARNING against the in-trend direction)")
    if location_strong:
        parts.append("at structural level (S/R, EMA, or BB band)")
    else:
        parts.append("mid-range location (less reliable)")
    if has_confirmation:
        parts.append("confirmation candle present")
    elif conf == "not_confirmed":
        parts.append("no confirmation candle yet")

    summary = " · ".join(parts)
    # Final tag
    if in_trend and location_strong and has_confirmation:
        tag = "✓ STRONG — apply pattern rule with confidence."
    elif counter_trend and location_strong and has_confirmation:
        tag = "⚠️ CREDIBLE REVERSAL WARNING — pattern has the location and confirmation a real reversal needs. Weight as warning against the in-trend direction."
    elif counter_trend and not location_strong:
        tag = "✗ WEAK — counter-trend pattern without location/confirmation = noise."
    elif not location_strong and not has_confirmation:
        tag = "✗ WEAK — incomplete evidence."
    else:
        tag = "MIXED — partial evidence, one input among several."
    return f"{summary} → {tag}"


def build_pattern_section(fires: list, body_only: bool = False) -> str:
    """Given a list of detector-fire dicts (with enrichment context), build a
    prompt section combining pattern_library.md quotes with the per-fire
    evidence so the validator can apply the rules with full context.

    When body_only=True, omits the leading "## DETECTED PATTERNS ON THIS CHART"
    heading — for callers (live validator wiring) that supply their own section
    heading via the wrapping framework.
    """
    if not fires:
        return ""
    blocks = []
    seen = set()
    for f in fires:
        pid = f.get("pattern_id")
        if not pid:
            continue
        quote = PATTERN_QUOTES.get(pid, "")
        ctx = f.get("context", {})
        ctx_lines = _render_context(ctx)
        interp = _interpretation(
            ctx.get("trend_alignment", ""),
            ctx.get("confirmation_status", ""),
            ctx.get("invalidation_status", ""),
            ctx.get("at_swing_extreme", False),
            ctx.get("nearest_ema_within_8pips"),
            ctx.get("bb_position", "unknown"),
        )
        header = f"### {f.get('name', pid)} (bar -{abs(f.get('bar_idx', 0) - 249)})"
        # Quote pattern_library entry once per pattern_id (rule is the same)
        if pid in seen:
            quote_part = ""
        else:
            seen.add(pid)
            quote_part = f"\n\n> {quote}" if quote else ""
        blocks.append(
            f"{header}\n\n**Evidence at fire:**\n{ctx_lines}\n\n"
            f"**Interpretation:** {interp}{quote_part}"
        )
    intro = (
        "Programmatic detectors fired the patterns below. Each is labeled on "
        "the chart at the relevant bar. Use the **Evidence at fire** to judge "
        "if the pattern's rule applies cleanly to THIS chart — patterns alone "
        "are not sufficient; they need location, trend alignment, confirmation, "
        "and no invalidation. Apply the pattern_library rule (quoted under each "
        "pattern) only where the evidence supports it.\n\n"
    )
    footer = (
        "\n\n*Pattern bias CONFIRMING the scout's trade direction with strong evidence = "
        "support the verdict. Pattern bias CONFLICTING (counter-trend) is a heads-up, NOT "
        "a direction-flip — multi-bar structure (cascade phase, fan) dominates single-bar "
        "patterns per established trading rules.*\n"
    )
    body = intro + "\n\n---\n\n".join(blocks) + footer
    if body_only:
        return body
    return "## DETECTED PATTERNS ON THIS CHART (with indicator-context evidence)\n\n" + body
