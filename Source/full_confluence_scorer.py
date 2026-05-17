"""Full Confluence Scorer — V4 THESIS-ALIGNED MODEL.

Gates mirror the trade thesis in chronological order:

  Gate 1: Did the cross happen?        (pass/fail)
          E21×E55 cross within 30 bars OR fan actively expanding OR fan ordered +
          story >= 40 OR retracement continuation (fan peaked/contracting to E100)

  Gate 2: Is the fan ordering?         (0-20 pts)
          EMAs properly stacked + E100 on correct side + fan state

  Gate 3: Is expansion confirmed?      (0-30 pts)
          Price away from E100 + BB expanding + velocity + momentum candles

  Gate 4: Supporting evidence          (0-20 pts)
          Validator vision confidence + session quality + Tim's historical wins

  Gate 5: Risk modifiers               (-10 to +5)
          News events, account state, daily limits

Total max = 75. Tradeable = Gate1 PASS + total >= min_confluence (default 30).

Sniper is early warning trigger ONLY — not scored, not gated.
"""

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from db_pool import get_trading_forex

logger = logging.getLogger(__name__)


def _query_historical_wins(pair: str, direction: str, entry_type: str = "") -> Dict:
    """Query Tim's manual trade history for this pair + direction."""
    try:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        _dir = "buy" if direction == "bullish" else "sell"

        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins
            FROM manual_trade_analysis
            WHERE pair=? AND direction=?
        """, [pair, _dir]).fetchone()

        total = row["total"] or 0
        wins  = row["wins"]  or 0
        wr    = (wins / total * 100) if total > 0 else 0
        return {"total": total, "wins": wins, "win_rate": wr}
    except Exception as e:
        logger.debug("Historical wins query failed: %s", e)
        return {"total": 0, "wins": 0, "win_rate": 0}


def compute_full_confluence(
    sniper_result: Dict[str, Any],
    intelligence_data: Dict[str, Any],
    db_evidence: Optional[Dict[str, Any]] = None,
    session: str = "unknown",
    account_state: Optional[Dict[str, Any]] = None,
    min_confluence: int = 30,
    market_picture: Optional[Dict[str, Any]] = None,
    profile_engine: Optional[Any] = None,
    scout_context: Optional[Dict[str, Any]] = None,
    pair: str = "",
) -> Dict[str, Any]:
    """
    Thesis-aligned confluence scorer.
    Gates 2-4 score the thesis sequentially — cross → fan → expansion → evidence → risk.
    """
    breakdown = {}

    # ── Extract from sniper (indicators only — scores ignored) ──
    sniper = sniper_result or {}
    ind     = sniper.get("indicators", {})
    direction = sniper.get("direction", "neutral")
    h4_bias   = sniper.get("h4_bias", "none")

    mp = market_picture or {}
    sc = scout_context  or {}

    # Common fields used across gates.
    # generate_market_picture() nests fan_direction under mp["ema"] — check both levels.
    _mp_ema = mp.get("ema", {}) if isinstance(mp, dict) else {}
    fan_direction   = (mp.get("fan_direction") or _mp_ema.get("fan_direction") or sc.get("fan_direction") or "neutral")
    fan_state       = (mp.get("fan_state")     or _mp_ema.get("fan_state")     or sc.get("fan_state")     or "unknown")
    velocity        = float(mp.get("separation_velocity", 0) or _mp_ema.get("separation_velocity", 0) or 0)
    trend_health    = float(mp.get("trend_health", 0) or 0)
    reversal_risk   = str(mp.get("reversal_risk", "unknown")).lower()
    separation_pct  = float(mp.get("separation_pct", 0) or 0)
    bars_since_cross = (mp.get("bars_since_crossover") if mp.get("bars_since_crossover") is not None
                        else _mp_ema.get("bars_since_crossover"))
    # Cross 2: E21 × E100 — fan fully ordered, the confirmation cross
    bars_since_cross2 = (mp.get("bars_since_cross2") if mp.get("bars_since_cross2") is not None
                         else _mp_ema.get("bars_since_cross2")
                         or sniper.get("e21_crossed_100_recently") and 5  # fallback if <10 bars
                         or None)

    story_score     = int(sc.get("opportunity_score", sc.get("story_score", sc.get("score", 0))) or 0)  # scout sends 'story_score'
    story_entry     = sc.get("story_entry_type", sc.get("entry_type", ""))
    is_thesis_confirmed = sc.get("opportunity_source", "") in ("thesis_confirmed", "thesis_elite")
    is_ema_cross        = sc.get("opportunity_source", "") == "ema_cross_trend"

    fan_ordered_bull = fan_direction in ("bullish", "bull")
    fan_ordered_bear = fan_direction in ("bearish", "bear")
    fan_ordered      = fan_ordered_bull or fan_ordered_bear

    # If no explicit trade direction, default to fan direction (with-trend assumption).
    # This covers cycles where sniper_data has no 'direction' field set.
    if direction in ("neutral", "none", "", None) and fan_ordered:
        direction = fan_direction

    trade_is_buy  = direction in ("bullish", "bull")
    trade_is_sell = direction in ("bearish", "bear")
    is_with_trend    = (trade_is_buy and fan_ordered_bull) or (trade_is_sell and fan_ordered_bear)
    is_counter_trend = (trade_is_buy and fan_ordered_bear) or (trade_is_sell and fan_ordered_bull)

    bb_expanding  = mp.get("bb_expanding",  ind.get("bb_expanding",  False))
    bb_contracting = mp.get("bb_contracting", ind.get("bb_contracting", False))
    bb_squeeze    = mp.get("bb_squeeze", False)

    # ── Sanity check: override false "expanding" flags when deltas are flat ──
    # ema_separation.py uses a loose threshold (0.005 raw separation) which can
    # report "expanding" when the fan is actually stalled. Cross-check with the
    # scout's delta-based values which use percentage-of-price measurement.
    _sc_fan_delta = float(sc.get("fan_delta_5bar", 0) or 0)
    _sc_bb_delta  = float(sc.get("bb_delta_5bar", 0) or 0)
    if fan_state == "expanding" and _sc_fan_delta <= 0:
        fan_state = "stable"  # Fan ordered but NOT actively expanding
    if bb_expanding and _sc_bb_delta <= 0:
        bb_expanding = False  # BB not actually expanding

    # ═══════════════════════════════════════════════════════════════
    # GATE 1: DID THE CROSS HAPPEN? (pass/fail)
    # The starting gun. E21×E55 cross within 30 bars OR fan is already
    # ordered (cross happened earlier) OR story says something is forming.
    # ═══════════════════════════════════════════════════════════════
    _recent_cross = (
        bars_since_cross is not None and
        isinstance(bars_since_cross, (int, float)) and
        bars_since_cross <= 30   # 30 M15 bars = 7.5h window (widened from 20 to catch Phase 2.5 entries)
    )
    # Fan is actively expanding RIGHT NOW (not peaked/contracting)
    _fan_active = fan_state in ("just_crossed", "expanding", "accelerating", "bullish_expanding", "bearish_expanding")

    _gate1_reason = ""
    if _recent_cross and _fan_active:
        gate1_pass = True
        _gate1_reason = f"EMA cross {int(bars_since_cross)} bars ago + fan actively {fan_state}"
    elif _recent_cross:
        gate1_pass = True
        _gate1_reason = f"EMA cross {int(bars_since_cross)} bars ago (fan={fan_state})"
    elif _fan_active and fan_ordered:
        # Fan is actively expanding AND ordered — live opportunity
        gate1_pass = True
        _gate1_reason = f"Fan actively {fan_state} {fan_direction} — live expansion"
    elif fan_ordered and fan_state not in ("peaked", "contracting") and story_score >= 40:
        # Ordered fan (not peaked/contracting) with strong story confirmation
        # Peaked/contracting = move is done, validator will always reject. Block here.
        gate1_pass = True
        _gate1_reason = f"Fan ordered {fan_direction} ({fan_state}) + story {story_score}/100"
    elif story_score >= 40 and fan_state not in ("peaked", "contracting"):
        # Story confirms opportunity AND fan is not a completed move
        gate1_pass = True
        _gate1_reason = f"Story score {story_score}/100 — opportunity forming ({fan_state})"
    elif (sc.get("is_retracement") or sc.get("alert_type") == "RETRACEMENT") and fan_ordered and story_score >= 40 and story_entry not in ("none", ""):
        # Retracement continuation — fan peaked/contracting back to E100 = Tim's primary setup.
        # Gate 1 normally hard-blocks "contracting" fan_state, but that's wrong for retracements:
        # the contraction IS the setup (price pulling back to E100/E55 before continuation).
        # Gate 2 awards +15pts for this exact scenario (_e100_retest bonus). Let it get there.
        # Guard: fan must be ordered (EMAs stacked), story_score >= 40, and entry_type must be named.
        gate1_pass = True
        _gate1_reason = f"Retracement continuation: {fan_direction} fan {fan_state}, entry={story_entry}, story={story_score}/100"
    elif is_thesis_confirmed:
        gate1_pass = True
        _gate1_reason = "Thesis confirmed — full structure detected"
    else:
        gate1_pass = False
        _gate1_reason = f"No active setup: fan={fan_state} {fan_direction}, cross={bars_since_cross}bars, story={story_score}/100"

    breakdown["gate1_sniper"] = {   # key kept for legacy compat
        "pass": gate1_pass,
        "reason": _gate1_reason,
        "bars_since_cross": bars_since_cross,
        "fan_direction": fan_direction,
        "fan_ordered": fan_ordered,
        "story_score": story_score,
        "signal_source": (
            "ema_cross"          if _recent_cross       else
            "retracement"        if (sc.get("is_retracement") or sc.get("alert_type") == "RETRACEMENT") and fan_ordered and story_score >= 40 and story_entry not in ("none", "") else
            "fan_ordered"        if fan_ordered          else
            "thesis_confirmed"   if is_thesis_confirmed  else
            "story_score"
        ),
    }

    if not gate1_pass:
        return {
            "total_score": 0,
            "max_possible": 75,
            "tradeable": False,
            "direction": direction,
            "breakdown": breakdown,
            "min_confluence": min_confluence,
            "exit_strategy": None,
            "summary": f"Gate1 FAIL: {_gate1_reason}. No trade.",
        }

    # ═══════════════════════════════════════════════════════════════
    # GATE 2: IS THE FAN ORDERING CORRECTLY? (0-20 pts)
    # Steps 1-2 of the thesis: EMAs stacking, E100 on correct side.
    # ═══════════════════════════════════════════════════════════════
    gate2_points = 0
    gate2_components = []

    # ── E100 retest detection ──
    # Price at or just through E100 in an ordered fan = the ideal entry zone.
    # Bull fan + price at/below E100 = BUY zone. Bear fan + price at/above E100 = SELL zone.
    gap_price_100 = float(mp.get("gap_price_100", 0) or sc.get("gap_price_100", 0) or 0)
    e100_dist = float(
        sc.get("e100_distance_pips", 0) or
        ind.get("e100_dist_pips", 0) or
        mp.get("e100_distance_pips", 0) or 0
    )
    _e100_retest = (
        fan_ordered and
        fan_state in ("peaked", "contracting", "decelerating", "stable") and
        (
            (fan_ordered_bull and gap_price_100 <= 2) or   # bull fan, price at/below E100
            (fan_ordered_bear and gap_price_100 >= -2)     # bear fan, price at/above E100
        ) and
        e100_dist <= 20   # within 20 pips of E100 (not far away)
    )

    # ── Fan stack (0-10) ──
    if fan_ordered and is_with_trend:
        if _e100_retest:
            # E100 retest in established fan = Tim's primary setup
            # Fan peak → retracement → price at E100 → enter with fan direction
            gate2_points += 10
            gate2_components.append(f"E100 RETEST: {fan_direction} fan {fan_state}, price at E100 retest zone — best entry +10")
        elif fan_state in ("expanding", "accelerating", "just_crossed"):
            gate2_points += 10
            gate2_components.append(f"Fan {fan_direction} {fan_state} — stack forming +10")
        elif fan_state in ("stable", "peaked"):
            gate2_points += 7
            gate2_components.append(f"Fan {fan_direction} {fan_state} — ordered but slowing +7")
        elif fan_state in ("decelerating", "contracting"):
            gate2_points += 5
            gate2_components.append(f"Fan {fan_direction} {fan_state} — fading but ordered +5")
        else:
            gate2_points += 5
            gate2_components.append(f"Fan {fan_direction} {fan_state} — ordering +5")
    elif fan_ordered and is_counter_trend:
        if fan_state in ("decelerating", "peaked", "contracting"):
            gate2_points += 8
            gate2_components.append(f"Counter-trend: {fan_direction} fan {fan_state} — exhausting +8")
        elif fan_state in ("expanding", "accelerating"):
            gate2_points += 0
            gate2_components.append(f"⛔ Counter-trend into healthy {fan_direction} fan — 0pts")
        else:
            gate2_points += 3
            gate2_components.append(f"Counter-trend: fan {fan_state} (unclear) +3")
    elif not fan_ordered:
        # Mixed fan — Phase 2.5 or early cross, fan not yet ordered
        if _recent_cross and bars_since_cross <= 10:
            gate2_points += 5
            gate2_components.append(f"Fresh cross {int(bars_since_cross)} bars ago — fan forming +5")
        else:
            gate2_points += 2
            gate2_components.append(f"Fan mixed — thesis early stage +2")

    # ── E100 proximity (0-5) ──
    e100_correct_side = (
        (trade_is_buy  and gap_price_100 > 0) or   # price above E100 = bullish
        (trade_is_sell and gap_price_100 < 0)       # price below E100 = bearish
    )
    if _e100_retest:
        # E100 retest already scored above — give bonus for being in the zone
        gate2_points += 5
        gate2_components.append(f"E100 retest zone ({e100_dist:.1f}p from E100) +5")
    elif e100_correct_side and e100_dist >= 5:
        gate2_points += 5
        gate2_components.append(f"E100 correct side, {e100_dist:.1f}p distance +5")
    elif e100_correct_side:
        gate2_points += 3
        gate2_components.append(f"E100 correct side, {e100_dist:.1f}p (close) +3")
    elif e100_dist < 5:
        gate2_points += 0
        gate2_components.append(f"Price near/wrong side of E100 ({e100_dist:.1f}p) — Phase 2.5 entry")

    # ── Two-cross confirmation bonus (0-10) ──
    # Cross 1 (E21×E55): fan starts forming — scout trigger
    # Cross 2 (E21×E100): fan fully ordered — confirmation signal
    # Both crosses present = highest quality setup
    _c1_fresh = _recent_cross and bars_since_cross is not None
    _c2_fresh = bars_since_cross2 is not None

    if _c1_fresh and bars_since_cross <= 5:
        gate2_points += 3
        gate2_components.append(f"C1 very fresh (E21×E55 {int(bars_since_cross)} bars ago) +3")
    elif _c1_fresh and bars_since_cross <= 15:
        gate2_points += 2
        gate2_components.append(f"C1 recent (E21×E55 {int(bars_since_cross)} bars ago) +2")

    if _c2_fresh and bars_since_cross2 <= 5:
        gate2_points += 5
        gate2_components.append(f"C2 confirmed (E21×E100 {int(bars_since_cross2)} bars ago) — fan fully ordered +5")
    elif _c2_fresh and bars_since_cross2 <= 15:
        gate2_points += 4
        gate2_components.append(f"C2 recent (E21×E100 {int(bars_since_cross2)} bars ago) +4")
    elif _c2_fresh and bars_since_cross2 <= 30:
        gate2_points += 2
        gate2_components.append(f"C2 present (E21×E100 {int(bars_since_cross2)} bars ago) +2")
    elif not _c2_fresh and fan_ordered:
        # Fan is ordered so C2 exists — just outside our window
        gate2_points += 1
        gate2_components.append("C2 outside window (fan ordered — C2 implied) +1")

    gate2_points = min(20, max(0, gate2_points))

    breakdown["gate2_fan"] = {
        "points": gate2_points,
        "max": 20,
        "fan_direction": fan_direction,
        "fan_state": fan_state,
        "fan_ordered": fan_ordered,
        "e100_correct_side": e100_correct_side,
        "e100_dist_pips": e100_dist,
        "bars_since_cross": bars_since_cross,
        "bars_since_cross2": bars_since_cross2,  # E21×E100 confirmation cross
        "is_with_trend": is_with_trend,
        "is_counter_trend": is_counter_trend,
        "components": gate2_components,
    }

    # ═══════════════════════════════════════════════════════════════
    # GATE 3: IS EXPANSION CONFIRMED? (0-30 pts)
    # Steps 3-6 of the thesis: candles away from E100, BB expanding,
    # velocity healthy, momentum candles present.
    # ═══════════════════════════════════════════════════════════════
    gate3_points = 0
    gate3_components = []

    # ── Candles away from E100 (0-10) ──
    if e100_dist >= 15:
        gate3_points += 10
        gate3_components.append(f"Candles {e100_dist:.1f}p from E100 — full expansion +10")
    elif e100_dist >= 8:
        gate3_points += 7
        gate3_components.append(f"Candles {e100_dist:.1f}p from E100 — expanding +7")
    elif e100_dist >= 5:
        gate3_points += 4
        gate3_components.append(f"Candles {e100_dist:.1f}p from E100 — just separating +4")
    else:
        gate3_points += 0
        gate3_components.append(f"Candles {e100_dist:.1f}p from E100 — no separation yet +0")

    # ── BB expanding (0-10) ──
    if bb_squeeze and bb_expanding:
        gate3_points += 10
        gate3_components.append("BB squeeze releasing — explosive move starting +10")
    elif bb_expanding and is_with_trend:
        gate3_points += 8
        gate3_components.append("BB expanding with trend — energy confirmed +8")
    elif bb_expanding:
        gate3_points += 5
        gate3_components.append("BB expanding +5")
    elif bb_contracting and is_with_trend:
        gate3_points += 1
        gate3_components.append("BB contracting — move losing energy +1")
    else:
        gate3_points += 3
        gate3_components.append("BB neutral +3")

    # ── Fan velocity (0-5) ──
    if velocity >= 0.007:
        gate3_points += 5
        gate3_components.append(f"Velocity {velocity:.4f}%/bar — FAST +5")
    elif velocity >= 0.005:
        gate3_points += 3
        gate3_components.append(f"Velocity {velocity:.4f}%/bar — healthy +3")
    elif velocity >= 0.003:
        gate3_points += 1
        gate3_components.append(f"Velocity {velocity:.4f}%/bar — slow +1")
    else:
        gate3_points += 0
        gate3_components.append(f"Velocity {velocity:.4f}%/bar — stalling +0")

    # ── Momentum candles (0-5) ──
    has_momentum = (
        ind.get("has_momentum_candles") or
        ind.get("momentum_candles") or
        mp.get("momentum_candles") or
        (trend_health >= 50 and velocity >= 0.005)
    )
    if has_momentum:
        gate3_points += 5
        gate3_components.append("Momentum candles present — conviction +5")
    else:
        gate3_points += 0
        gate3_components.append("No momentum candles — indecisive +0")

    gate3_points = min(30, max(0, gate3_points))

    breakdown["gate3_expansion"] = {
        "points": gate3_points,
        "max": 30,
        "e100_dist_pips": e100_dist,
        "bb_expanding": bb_expanding,
        "bb_squeeze": bb_squeeze,
        "velocity": velocity,
        "trend_health": trend_health,
        "has_momentum_candles": bool(has_momentum),
        "components": gate3_components,
    }

    # ═══════════════════════════════════════════════════════════════
    # GATE 4: SUPPORTING EVIDENCE (0-20 pts)
    # Validator vision + session quality + Tim's historical wins
    # ═══════════════════════════════════════════════════════════════
    gate4_points = 0
    gate4_components = []

    # ── Validator vision confidence (0-10) ──
    db = db_evidence or {}
    vision_conf = float(db.get("v4_confidence", db.get("confidence", 0)) or 0)

    if vision_conf >= 0.85:
        vision_pts = 10
        gate4_components.append(f"Validator: very high confidence ({vision_conf:.0%}) +10")
    elif vision_conf >= 0.70:
        vision_pts = 8
        gate4_components.append(f"Validator: high confidence ({vision_conf:.0%}) +8")
    elif vision_conf >= 0.55:
        vision_pts = 5
        gate4_components.append(f"Validator: moderate confidence ({vision_conf:.0%}) +5")
    elif vision_conf > 0:
        vision_pts = 3
        gate4_components.append(f"Validator: low confidence ({vision_conf:.0%}) +3")
    else:
        vision_pts = 4  # Pre-validator: neutral assumption
        gate4_components.append("Validator: pending (pre-call neutral) +4")

    gate4_points += vision_pts

    # ── Session quality (0-5) ──
    session_lower = (session or "").lower().replace(" ", "_")
    if "overlap" in session_lower or "london_ny" in session_lower:
        sess_pts = 5
        gate4_components.append("Session: London-NY overlap (best liquidity) +5")
    elif "london" in session_lower:
        sess_pts = 4
        gate4_components.append("Session: London +4")
    elif "new_york" in session_lower or "ny" in session_lower:
        sess_pts = 4
        gate4_components.append("Session: New York +4")
    elif "tokyo" in session_lower or "sydney" in session_lower:
        sess_pts = 1
        gate4_components.append(f"Session: {session} (low liquidity) +1")
    else:
        from datetime import datetime, timezone
        try:
            import zoneinfo
            hour = datetime.now(zoneinfo.ZoneInfo("America/New_York")).hour
        except Exception:
            hour = datetime.now(timezone.utc).hour - 5
        if 8 <= hour <= 12:
            sess_pts = 5
            gate4_components.append("Session: 8-12AM ET (London-NY overlap) +5")
        elif 13 <= hour <= 17:
            sess_pts = 4
            gate4_components.append("Session: NY afternoon +4")
        elif 3 <= hour <= 8:
            sess_pts = 3
            gate4_components.append("Session: London open +3")
        else:
            sess_pts = 1
            gate4_components.append("Session: off-hours +1")

    gate4_points += sess_pts

    # ── Tim's historical wins on this setup (0-5) ──
    history_pts = 0
    if pair and direction:
        hist = _query_historical_wins(pair, direction, story_entry)
        h_total = hist["total"]
        h_wins  = hist["wins"]
        h_wr    = hist["win_rate"]
        if h_total >= 5 and h_wr >= 70:
            history_pts = 5
            gate4_components.append(
                f"Tim's history: {h_wins}/{h_total} wins ({h_wr:.0f}%) on {pair} {direction} +5"
            )
        elif h_total >= 3 and h_wr >= 60:
            history_pts = 3
            gate4_components.append(
                f"Tim's history: {h_wins}/{h_total} wins ({h_wr:.0f}%) on {pair} {direction} +3"
            )
        elif h_total >= 2:
            history_pts = 2
            gate4_components.append(
                f"Tim's history: {h_wins}/{h_total} trades on {pair} (building sample) +2"
            )
        elif h_total == 1 and h_wr == 100:
            history_pts = 1
            gate4_components.append(
                f"Tim's history: 1 win on {pair} {direction} +1"
            )
        # 0 trades = no points (no data, neutral)

    gate4_points += history_pts

    # ── H4 alignment bonus (inline) ──
    h4_agrees = (h4_bias == "bull" and trade_is_buy) or (h4_bias == "bear" and trade_is_sell)
    h4_opposes = (h4_bias == "bull" and trade_is_sell) or (h4_bias == "bear" and trade_is_buy)
    if h4_agrees:
        gate4_points += 1
        gate4_components.append(f"H4 aligned ({h4_bias}) +1")
    elif h4_opposes:
        gate4_points -= 1
        gate4_components.append(f"H4 opposes ({h4_bias}) -1")

    gate4_points = min(20, max(0, gate4_points))

    breakdown["gate4_evidence"] = {
        "points": gate4_points,
        "max": 20,
        "vision_confidence": vision_conf,
        "vision_pts": vision_pts,
        "session_pts": sess_pts,
        "history_pts": history_pts,
        "history_total": hist["total"] if pair else 0,
        "history_wr": hist["win_rate"] if pair else 0,
        "h4_bias": h4_bias,
        "components": gate4_components,
    }

    # ═══════════════════════════════════════════════════════════════
    # GATE 5: RISK MODIFIERS (-10 to +5)
    # News, account state, daily limits
    # ═══════════════════════════════════════════════════════════════
    gate5_mod = 0
    gate5_components = []

    intel = intelligence_data or {}
    acct  = account_state or {}

    risk_events = intel.get("risk_events_upcoming", [])
    if len(risk_events) >= 3:
        gate5_mod -= 5
        gate5_components.append(f"{len(risk_events)} risk events upcoming -5")
    elif risk_events:
        gate5_mod -= 2
        gate5_components.append(f"{len(risk_events)} risk event(s) -2")

    # Sentiment alignment
    try:
        sentiment = float(intel.get("overall_sentiment", 0) or 0)
    except (TypeError, ValueError):
        sentiment = 0

    if (trade_is_buy and sentiment > 0.3) or (trade_is_sell and sentiment < -0.3):
        gate5_mod += 2
        gate5_components.append(f"Sentiment aligned +2")
    elif (trade_is_buy and sentiment < -0.5) or (trade_is_sell and sentiment > 0.5):
        gate5_mod -= 2
        gate5_components.append(f"Sentiment opposed -2")

    # Account limits
    open_trades = acct.get("open_trade_count", 0)
    daily_loss_pct = acct.get("daily_loss_pct", 0)
    if open_trades >= 3:
        gate5_mod -= 5
        gate5_components.append(f"Max trades reached ({open_trades}) -5")
    if daily_loss_pct >= 2.0:
        gate5_mod -= 5
        gate5_components.append(f"Daily loss {daily_loss_pct:.1f}% at limit -5")

    gate5_mod = max(-10, min(5, gate5_mod))

    breakdown["gate5_risk"] = {
        "modifier": gate5_mod,
        "risk_events": len(risk_events),
        "sentiment": sentiment,
        "open_trades": open_trades,
        "daily_loss_pct": daily_loss_pct,
        "components": gate5_components,
    }

    # ═══════════════════════════════════════════════════════════════
    # TOTAL SCORE
    # ═══════════════════════════════════════════════════════════════
    total = gate2_points + gate3_points + gate4_points + gate5_mod
    max_possible = 75   # Gate2(20) + Gate3(30) + Gate4(20) + Gate5(+5)

    # Hard block: counter-trend into healthy expanding fan
    direction_blocked = (
        is_counter_trend and
        fan_state in ("expanding", "accelerating") and
        gate2_points == 0
    )

    tradeable = gate1_pass and total >= min_confluence and not direction_blocked

    # ═══════════════════════════════════════════════════════════════
    # EXIT STRATEGY
    # ═══════════════════════════════════════════════════════════════
    exit_strategy = None
    if tradeable:
        exit_strategy = {
            "exit_mode": "dynamic_ema",
            "estimated_hold_candles": 18 if is_counter_trend else 22,
            "exit_signals": [
                "ema_sep_velocity_negative_2bars",
                "bb_contracting_past_candle10",
                "mfe_giveback_40pct",
            ],
            "notes": (
                "Track EMA 21/55 separation velocity. When velocity goes negative 2+ bars "
                "AND in profit → tighten stop. When BB contracting past candle 10 → close. "
                "No fixed TP — let the fan tell you when to exit."
            ),
        }

    # ── Legacy compat keys (downstream code uses these) ──
    breakdown["gate1_structure"]  = breakdown["gate1_sniper"]
    breakdown["gate2_thesis"]     = breakdown["gate2_fan"]
    breakdown["gate3_evidence"]   = breakdown["gate3_expansion"]
    breakdown["gate4_risk"]       = {"modifier": gate5_mod, **breakdown["gate5_risk"]}
    breakdown["ema_narrative"]    = {
        "points": gate2_points, "max": 20,
        "fan_state": fan_state, "fan_direction": fan_direction,
        "velocity": velocity, "trend_health": trend_health,
        "reversal_risk": reversal_risk, "separation_pct": separation_pct,
        "components": gate2_components,
    }
    breakdown["db_evidence"]      = {
        "points": vision_pts, "max": 10,
        "v4_vision_confidence": vision_conf, "source": "v4_vision",
    }
    breakdown["session_regime"]   = {
        "points": sess_pts, "max": 5,
        "session": session, "h4_bias": h4_bias,
    }
    breakdown["intelligence"]     = {
        "points": max(0, gate5_mod), "max": 5,
        "sentiment": sentiment, "risk_events": len(risk_events),
        "components": gate5_components,
    }
    breakdown["combined_playbook"] = {
        "points": history_pts, "max": 5,
        "pair": pair, "direction": direction,
        "history_total": hist["total"] if pair else 0,
        "history_wr": hist["win_rate"] if pair else 0,
    }

    return {
        "total_score": total,
        "max_possible": max_possible,
        "tradeable": tradeable,
        "direction": direction,
        "signal": "TRADE" if tradeable else "HOLD",
        "breakdown": breakdown,
        "min_confluence": min_confluence,
        "exit_strategy": exit_strategy,
        "pipeline": {
            "gate1_pass":      gate1_pass,
            "gate2_fan":       gate2_points,
            "gate3_expansion": gate3_points,
            "gate4_evidence":  gate4_points,
            "gate5_risk":      gate5_mod,
            "direction_blocked": direction_blocked,
        },
        "summary": (
            f"Gate1 {'PASS' if gate1_pass else 'FAIL'} [{breakdown['gate1_sniper']['signal_source']}] | "
            f"G2(fan)={gate2_points}/20 | G3(expansion)={gate3_points}/30 | "
            f"G4(evidence)={gate4_points}/20 | G5(risk)={gate5_mod:+d} | "
            f"Total {total}/75 (min={min_confluence}) {'→ TRADE' if tradeable else '→ HOLD'}"
        ),
    }


def _score_db_raw(win_rate: float, profit_factor: float, trade_count: int) -> int:
    """Legacy helper — kept for any callers that reference it."""
    pts = 0
    if win_rate >= 85 and trade_count >= 500:
        pts += 16
    elif win_rate >= 80 and trade_count >= 200:
        pts += 13
    elif win_rate >= 75 and trade_count >= 100:
        pts += 10
    elif win_rate >= 70 and trade_count >= 50:
        pts += 7
    elif win_rate >= 60 and trade_count >= 30:
        pts += 4
    elif trade_count >= 15:
        pts += 1
    if profit_factor >= 1.5:
        pts += 7
    elif profit_factor >= 1.2:
        pts += 4
    elif profit_factor >= 1.0:
        pts += 2
    return min(25, pts)
