"""
market_confirmation.py
Fetches live market data for a pair and compares against a user's described setup.
Returns a structured confirmation result with agreement level and supporting evidence.
"""

import logging
import sys
import os
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


def _get_candles(pair: str, granularity: str = "H1", count: int = 100) -> List[Dict]:
    """Fetch candles via OANDA. Uses the same pattern as the rest of the codebase."""
    try:
        try:
            from Source.agents.wrappers import fetch_candles
        except ImportError:
            from agents.wrappers import fetch_candles
        # fetch_candles returns {"candles": [...], "count": N} — extract the list
        result = fetch_candles(pair, timeframe=granularity, count=count)
        if isinstance(result, dict):
            candles = result.get("candles") or []
        elif isinstance(result, list):
            candles = result
        else:
            candles = []
        return candles
    except Exception as e:
        logger.warning(f"[market_confirm] fetch_candles failed for {pair}: {e}")
        return []


def _normalize_candles(candles: List[Dict]) -> List[Dict]:
    """
    Normalize OANDA raw candles ({mid: {o, h, l, c}, time, volume})
    to flat format ({open, high, low, close, time, volume}) expected
    by generate_market_picture / scan_ema_signals.
    Passes through candles that already have a 'close' key.
    """
    normalized = []
    for c in candles:
        if 'close' in c:
            normalized.append(c)
        elif 'mid' in c:
            mid = c['mid']
            normalized.append({
                'time': c.get('time', ''),
                'open': float(mid.get('o', 0)),
                'high': float(mid.get('h', 0)),
                'low':  float(mid.get('l', 0)),
                'close': float(mid.get('c', 0)),
                'volume': c.get('volume', 0),
            })
        else:
            normalized.append(c)  # unknown format, pass through
    return normalized


def _get_market_picture(pair: str, candles: List[Dict]) -> Optional[Dict]:
    """Run EMA narrative analysis on fetched candles."""
    if not candles:
        return None
    try:
        try:
            from Source.backtester.ema_separation import generate_market_picture
        except ImportError:
            from backtester.ema_separation import generate_market_picture
        return generate_market_picture(pair, _normalize_candles(candles))
    except Exception as e:
        logger.warning(f"[market_confirm] generate_market_picture failed: {e}")
        return None


def confirm_setup(
    pair: str,
    user_description: str,
    annotations: List[Dict],
    direction: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compare user's described setup against live market data.
    Returns agreement_level, evidence, and a human-readable response.
    """
    result = {
        "pair": pair,
        "agreement_level": "NO_DATA",
        "confirmed_points": [],
        "contradicting_points": [],
        "neutral_points": [],
        "market_summary": {},
        "response_text": "",
    }

    candles = _get_candles(pair, granularity="H1", count=100)
    if not candles:
        result["response_text"] = f"Can't fetch live data for {pair} right now — OANDA may be unavailable."
        return result

    picture = _get_market_picture(pair, candles)
    if not picture:
        result["response_text"] = f"Got candles for {pair} but couldn't build market picture."
        return result

    # Extract key market state fields
    # 2026-04-20 BUGFIX: generate_market_picture() nests EMA signals inside
    # picture['ema'] and BB/RSI/Stoch inside their own sub-dicts. Previous
    # top-level reads returned defaults for every field. Read from correct
    # nested level with top-level as fallback.
    _p_ema   = picture.get("ema", {}) if isinstance(picture.get("ema"), dict) else {}
    _p_bb    = picture.get("bollinger", {}) if isinstance(picture.get("bollinger"), dict) else {}
    _p_rsi   = picture.get("rsi", {}) if isinstance(picture.get("rsi"), dict) else {}
    _p_stoch = picture.get("stochastic", {}) if isinstance(picture.get("stochastic"), dict) else {}

    fan_state        = _p_ema.get("fan_state",     picture.get("fan_state", "unknown"))
    fan_direction    = _p_ema.get("fan_direction", picture.get("fan_direction", "neutral"))
    fan_ordered      = _p_ema.get("fan_ordered",   picture.get("fan_ordered", False))
    velocity         = _p_ema.get("separation_velocity", picture.get("separation_velocity", 0))
    trend_health     = picture.get("trend_health", 0)
    reversal_risk    = picture.get("reversal_risk", "unknown")
    bb_expanding     = _p_bb.get("expanding",  picture.get("bb_expanding", False))
    bb_width_pct     = _p_bb.get("width_pct",  picture.get("bb_width_pct", 0))
    rsi              = _p_rsi.get("value",     picture.get("rsi", None)) if isinstance(_p_rsi, dict) else picture.get("rsi")
    stoch_k          = _p_stoch.get("k",       picture.get("stoch_k", None)) if isinstance(_p_stoch, dict) else picture.get("stoch_k")
    recommended_bias = picture.get("recommended_bias", "neutral")
    narrative        = _p_ema.get("narrative",          picture.get("narrative", ""))
    e100_pattern     = picture.get("candle_pattern_at_e100", picture.get("e100_candle_pattern", {}))

    result["market_summary"] = {
        "fan_state": fan_state,
        "fan_direction": fan_direction,
        "fan_ordered": fan_ordered,
        "velocity": velocity,
        "trend_health": trend_health,
        "bb_expanding": bb_expanding,
        "rsi": rsi,
        "stoch_k": stoch_k,
        "recommended_bias": recommended_bias,
    }

    confirmed = []
    contradicting = []
    neutral = []

    # Check each annotation against live data
    for ann in annotations:
        ann_type = ann.get("type", "")
        note = ann.get("note", "")

        if ann_type == "bias":
            # Check if user's directional bias matches recommended bias
            user_dir = ann.get("direction") or direction
            if user_dir:
                ud = user_dir.upper()
                rb = recommended_bias.upper() if recommended_bias else ""
                fd = fan_direction.upper() if fan_direction else ""
                system_dir = "BUY" if ("bull" in rb or "bull" in fd) else ("SELL" if ("bear" in rb or "bear" in fd) else "NEUTRAL")
                if ud == system_dir:
                    confirmed.append(f"Direction {ud} — system agrees ({recommended_bias} bias, {fan_direction} fan)")
                elif system_dir == "NEUTRAL":
                    neutral.append(f"Direction {ud} — system neutral ({fan_state} fan, no clear bias)")
                else:
                    contradicting.append(f"Direction {ud} — system sees {system_dir} ({recommended_bias} bias, {fan_direction} fan)")

            # Check fan state annotations
            user_fan = ann.get("fan_state")
            if user_fan == "expanding" and fan_state in ("expanding", "peaked"):
                confirmed.append(f"Fan expanding — confirmed (fan_state={fan_state}, velocity={velocity:.4f}%/bar)")
            elif user_fan == "expanding" and fan_state in ("contracting", "mixed"):
                contradicting.append(f"Fan expanding — not confirmed (fan_state={fan_state}, velocity={velocity:.4f}%/bar)")
            elif user_fan == "contracting" and fan_state == "contracting":
                confirmed.append(f"Fan contracting — confirmed")

        elif ann_type == "indicator":
            bb_state = ann.get("bb_state")
            if bb_state == "expanding":
                if bb_expanding:
                    confirmed.append(f"BBs expanding — confirmed (width {bb_width_pct:.3f}%)")
                else:
                    contradicting.append(f"BBs expanding — not confirmed (width {bb_width_pct:.3f}%, not expanding)")

        elif ann_type == "pattern":
            ema_cross = ann.get("ema_cross")
            if ema_cross == "E21xE55":
                # Check if fan is past the initial cross
                if fan_state in ("expanding", "peaked") and fan_ordered:
                    confirmed.append(f"E21×E55 cross — fan is ordered and {fan_state}, consistent")
                elif fan_state in ("expanding",) and not fan_ordered:
                    confirmed.append(f"E21×E55 cross — fan expanding but not yet fully ordered (Phase 2.5)")
                else:
                    neutral.append(f"E21×E55 cross — fan is {fan_state}, {fan_direction}")
            elif ema_cross == "E21xE100":
                if fan_ordered:
                    confirmed.append(f"E21×E100 cross — fan fully ordered (Phase 3) ✅")
                else:
                    neutral.append(f"E21×E100 cross — fan not fully ordered yet")

        elif ann_type in ("support", "resistance"):
            price = ann.get("price")
            if price:
                neutral.append(f"User marked {'support' if ann_type == 'support' else 'resistance'} at {price} — noted")

    result["confirmed_points"] = confirmed
    result["contradicting_points"] = contradicting
    result["neutral_points"] = neutral

    # Determine overall agreement level
    n_confirmed = len(confirmed)
    n_contradicting = len(contradicting)

    if n_contradicting == 0 and n_confirmed > 0:
        agreement_level = "CONFIRMED"
    elif n_confirmed > n_contradicting:
        agreement_level = "PARTIAL"
    elif n_contradicting > n_confirmed:
        agreement_level = "DISAGREE"
    elif annotations:
        agreement_level = "NEUTRAL"
    else:
        # No annotations — just describe the market
        agreement_level = "INFO"

    result["agreement_level"] = agreement_level

    # Build human-readable response
    lines = []

    # Header
    icon = {"CONFIRMED": "✅", "PARTIAL": "⚡", "DISAGREE": "❌", "NEUTRAL": "📊", "INFO": "📊"}.get(agreement_level, "📊")
    lines.append(f"{icon} **{agreement_level}** — {pair.replace('_', '/')}")
    lines.append("")

    # Market state snapshot
    fan_icon = "✅" if fan_state == "expanding" else ("⚡" if fan_state == "peaked" else "❌")
    bb_icon = "✅" if bb_expanding else "➖"
    lines.append(f"{fan_icon} Fan: {fan_state} {fan_direction} (health {trend_health}/100, velocity {velocity:.4f}%/bar)")
    lines.append(f"{bb_icon} BBs: {'expanding' if bb_expanding else 'not expanding'} (width {bb_width_pct:.3f}%)")
    if rsi is not None:
        lines.append(f"📈 RSI: {rsi:.0f} | Stoch: {stoch_k:.0f}" if stoch_k else f"📈 RSI: {rsi:.0f}")
    lines.append(f"🎯 System bias: {recommended_bias}")
    lines.append("")

    # Confirmed points
    if confirmed:
        for c in confirmed:
            lines.append(f"✅ {c}")

    # Contradicting points
    if contradicting:
        for c in contradicting:
            lines.append(f"❌ {c}")

    # Neutral points
    if neutral:
        for n in neutral:
            lines.append(f"➖ {n}")

    # Narrative snippet
    if narrative:
        lines.append("")
        lines.append(f"_{narrative[:200]}{'...' if len(narrative) > 200 else ''}_")

    result["response_text"] = "\n".join(lines)
    return result


def get_market_snapshot(pair: str) -> Dict[str, Any]:
    """Quick market state query — no user description needed."""
    candles = _get_candles(pair, granularity="H1", count=100)
    if not candles:
        return {"error": f"No data for {pair}"}

    picture = _get_market_picture(pair, candles)
    if not picture:
        return {"error": f"Could not build market picture for {pair}"}

    fan_state = picture.get("fan_state", "unknown")
    fan_direction = picture.get("fan_direction", "neutral")
    fan_ordered = picture.get("fan_ordered", False)
    velocity = picture.get("separation_velocity", 0)
    trend_health = picture.get("trend_health", 0)
    bb_expanding = picture.get("bb_expanding", False)
    bb_width_pct = picture.get("bb_width_pct", 0)
    rsi = picture.get("rsi", None)
    stoch_k = picture.get("stoch_k", None)
    recommended_bias = picture.get("recommended_bias", "neutral")
    narrative = picture.get("narrative", "")

    fan_icon = "✅" if fan_state == "expanding" else ("⚡" if fan_state == "peaked" else "❌")
    bb_icon = "✅" if bb_expanding else "➖"
    ordered_icon = "✅" if fan_ordered else "➖"

    lines = [
        f"📊 **{pair.replace('_', '/')} Market Snapshot**",
        "",
        f"{fan_icon} Fan: {fan_state} {fan_direction} (health {trend_health}/100)",
        f"   Velocity: {velocity:.4f}%/bar | Ordered: {'YES' if fan_ordered else 'NO'}",
        f"{bb_icon} BBs: {'expanding' if bb_expanding else 'flat/contracting'} (width {bb_width_pct:.3f}%)",
    ]
    if rsi is not None:
        lines.append(f"📈 RSI: {rsi:.0f}" + (f" | Stoch K: {stoch_k:.0f}" if stoch_k else ""))
    lines.append(f"🎯 Bias: {recommended_bias}")
    if narrative:
        lines.append(f"\n_{narrative[:180]}{'...' if len(narrative) > 180 else ''}_")

    return {
        "pair": pair,
        "fan_state": fan_state,
        "fan_direction": fan_direction,
        "fan_ordered": fan_ordered,
        "velocity": velocity,
        "trend_health": trend_health,
        "bb_expanding": bb_expanding,
        "recommended_bias": recommended_bias,
        "response_text": "\n".join(lines),
    }
