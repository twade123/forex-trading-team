#!/usr/bin/env python3
"""
Candle Structure Analyzer — reads what candles are PHYSICALLY doing.

Not pattern names (hammer, engulfing) — those are handled by candle_patterns.py.
This module answers: what is the PRICE ACTION telling us?

- Wick rejection analysis (where is price getting pushed back?)
- Body progression (growing conviction or shrinking indecision?)
- EMA interaction (bouncing off E100? wrapping around E55? breaking through?)
- Consecutive structure (exhaustion runs, compression before breakout)
- Support/resistance formation from repeated wick rejections

Pure Python, no external dependencies.
"""

from typing import List, Dict, Any, Optional, Tuple
import math


def analyze_candle_structure(
    candles: List[Dict],
    ema_21: List[float],
    ema_55: List[float],
    ema_100: List[float],
    lookback: int = 20,
) -> Dict[str, Any]:
    """
    Full candle structure read over the last `lookback` candles.

    Args:
        candles: list of {'open','high','low','close','volume'} dicts
        ema_21/55/100: EMA value lists aligned to candles (same length)
        lookback: how many recent candles to analyze

    Returns:
        Dict with wick_analysis, body_progression, ema_interaction,
        consecutive_structure, and an overall structure_narrative.
    """
    if len(candles) < lookback or len(ema_100) < len(candles):
        return _empty_result("Insufficient data")

    n = len(candles)
    recent = candles[n - lookback:]
    e21 = ema_21[n - lookback:]
    e55 = ema_55[n - lookback:]
    e100 = ema_100[n - lookback:]

    # Skip if EMAs aren't computed yet
    if any(_is_nan(v) for v in [e21[-1], e55[-1], e100[-1]]):
        return _empty_result("EMA values not ready")

    wick = _analyze_wicks(recent)
    body = _analyze_body_progression(recent)
    ema_int = _analyze_ema_interaction(recent, e21, e55, e100)
    consec = _analyze_consecutive_structure(recent)
    narrative = _build_structure_narrative(wick, body, ema_int, consec)

    return {
        'wick_analysis': wick,
        'body_progression': body,
        'ema_interaction': ema_int,
        'consecutive_structure': consec,
        'structure_narrative': narrative,
    }


# ═══════════════════════════════════════════════════════════════════
# WICK ANALYSIS — Where is price getting rejected?
# ═══════════════════════════════════════════════════════════════════

def _analyze_wicks(candles: List[Dict]) -> Dict[str, Any]:
    """
    Read wick behavior: rejection pressure, wick-to-body ratios,
    repeated rejections at the same level.
    """
    n = len(candles)
    upper_wicks = []
    lower_wicks = []
    bodies = []
    upper_rejection_levels = []
    lower_rejection_levels = []

    for c in candles:
        o, h, l, cl = c['open'], c['high'], c['low'], c['close']
        body = abs(cl - o)
        total = h - l
        if total == 0:
            total = 0.00001  # avoid div/0

        upper_wick = h - max(o, cl)
        lower_wick = min(o, cl) - l

        upper_wicks.append(upper_wick)
        lower_wicks.append(lower_wick)
        bodies.append(body)

        # Track rejection levels (the wick tip where price got pushed back)
        if upper_wick > body * 0.5:
            upper_rejection_levels.append(h)
        if lower_wick > body * 0.5:
            lower_rejection_levels.append(l)

    # Recent emphasis (last 5 candles)
    last5 = candles[-5:]
    recent_upper_ratio = []
    recent_lower_ratio = []
    for c in last5:
        o, h, l, cl = c['open'], c['high'], c['low'], c['close']
        total = h - l if h - l > 0 else 0.00001
        body = abs(cl - o)
        recent_upper_ratio.append((h - max(o, cl)) / total)
        recent_lower_ratio.append((min(o, cl) - l) / total)

    avg_upper_ratio = sum(recent_upper_ratio) / len(recent_upper_ratio)
    avg_lower_ratio = sum(recent_lower_ratio) / len(recent_lower_ratio)

    # Detect repeated rejection at same price zone
    upper_cluster = _find_rejection_cluster(upper_rejection_levels)
    lower_cluster = _find_rejection_cluster(lower_rejection_levels)

    # Determine dominant pressure
    if avg_lower_ratio > 0.35 and avg_lower_ratio > avg_upper_ratio * 1.5:
        pressure = 'buying'  # Long lower wicks = buyers stepping in
        pressure_strength = 'strong' if avg_lower_ratio > 0.50 else 'moderate'
    elif avg_upper_ratio > 0.35 and avg_upper_ratio > avg_lower_ratio * 1.5:
        pressure = 'selling'  # Long upper wicks = sellers pushing down
        pressure_strength = 'strong' if avg_upper_ratio > 0.50 else 'moderate'
    else:
        pressure = 'balanced'
        pressure_strength = 'neutral'

    return {
        'dominant_pressure': pressure,
        'pressure_strength': pressure_strength,
        'avg_upper_wick_ratio': round(avg_upper_ratio, 3),
        'avg_lower_wick_ratio': round(avg_lower_ratio, 3),
        'upper_rejection_cluster': upper_cluster,  # price level repeatedly rejected
        'lower_rejection_cluster': lower_cluster,
        'last_candle_upper_wick_pct': round(recent_upper_ratio[-1], 3),
        'last_candle_lower_wick_pct': round(recent_lower_ratio[-1], 3),
    }


def _find_rejection_cluster(levels: List[float], tolerance_pct: float = 0.05) -> Optional[Dict]:
    """
    Find if multiple wick rejections cluster at the same price level.
    If 3+ wicks get rejected within tolerance_pct of each other, that's a level.
    """
    if len(levels) < 3:
        return None

    # Sort and look for clusters
    sorted_levels = sorted(levels)
    best_cluster = None
    best_count = 0

    for i in range(len(sorted_levels)):
        ref = sorted_levels[i]
        tol = ref * tolerance_pct / 100
        cluster = [lv for lv in sorted_levels if abs(lv - ref) <= tol]
        if len(cluster) > best_count:
            best_count = len(cluster)
            best_cluster = {
                'level': round(sum(cluster) / len(cluster), 6),
                'touches': len(cluster),
                'strength': 'strong' if len(cluster) >= 5 else 'moderate' if len(cluster) >= 3 else 'weak',
            }

    if best_cluster and best_cluster['touches'] >= 3:
        return best_cluster
    return None


# ═══════════════════════════════════════════════════════════════════
# BODY PROGRESSION — Growing conviction or shrinking indecision?
# ═══════════════════════════════════════════════════════════════════

def _analyze_body_progression(candles: List[Dict]) -> Dict[str, Any]:
    """
    Track how candle bodies are changing over the last N candles.
    Growing bodies = conviction. Shrinking bodies = indecision/exhaustion.
    """
    last8 = candles[-8:]
    body_sizes = []
    directions = []  # 1 = bull, -1 = bear, 0 = doji

    for c in last8:
        body = c['close'] - c['open']
        body_sizes.append(abs(body))
        if abs(body) < (c['high'] - c['low']) * 0.05:
            directions.append(0)
        elif body > 0:
            directions.append(1)
        else:
            directions.append(-1)

    # Body size trend (are bodies getting bigger or smaller?)
    if len(body_sizes) >= 4:
        first_half = sum(body_sizes[:4]) / 4
        second_half = sum(body_sizes[4:]) / 4
        if first_half > 0:
            change_ratio = second_half / first_half
        else:
            change_ratio = 1.0

        if change_ratio > 1.4:
            body_trend = 'growing'  # Conviction increasing
        elif change_ratio < 0.6:
            body_trend = 'shrinking'  # Indecision / exhaustion
        else:
            body_trend = 'steady'
    else:
        body_trend = 'unknown'
        change_ratio = 1.0

    # Direction consistency
    recent_dirs = directions[-5:]
    bull_count = sum(1 for d in recent_dirs if d == 1)
    bear_count = sum(1 for d in recent_dirs if d == -1)
    doji_count = sum(1 for d in recent_dirs if d == 0)

    if bull_count >= 4:
        direction_bias = 'strong_bull'
    elif bull_count >= 3:
        direction_bias = 'bull'
    elif bear_count >= 4:
        direction_bias = 'strong_bear'
    elif bear_count >= 3:
        direction_bias = 'bear'
    elif doji_count >= 3:
        direction_bias = 'indecisive'
    else:
        direction_bias = 'mixed'

    # Last candle character
    last = candles[-1]
    last_body = abs(last['close'] - last['open'])
    last_range = last['high'] - last['low'] if last['high'] != last['low'] else 0.00001
    last_body_pct = last_body / last_range

    if last_body_pct < 0.15:
        last_character = 'doji'
    elif last_body_pct > 0.70:
        last_character = 'marubozu'  # Strong conviction candle
    elif last_body_pct > 0.45:
        last_character = 'normal'
    else:
        last_character = 'spinning_top'

    return {
        'body_trend': body_trend,
        'body_change_ratio': round(change_ratio, 2),
        'direction_bias': direction_bias,
        'bull_count_5': bull_count,
        'bear_count_5': bear_count,
        'doji_count_5': doji_count,
        'last_candle_character': last_character,
        'last_body_pct': round(last_body_pct, 3),
    }


# ═══════════════════════════════════════════════════════════════════
# EMA INTERACTION — How is price relating to the EMAs physically?
# ═══════════════════════════════════════════════════════════════════

def _analyze_ema_interaction(
    candles: List[Dict],
    e21: List[float],
    e55: List[float],
    e100: List[float],
) -> Dict[str, Any]:
    """
    Read how price physically interacts with each EMA line:
    - Bouncing off (wick touches, body stays away)
    - Wrapping around (bodies crossing back and forth)
    - Breaking through (closed decisively through with momentum)
    - Riding (price hugging along the line)
    """
    n = len(candles)
    price = candles[-1]['close']

    # Analyze each EMA
    e100_read = _read_ema_interaction(candles, e100, 'E100', lookback=10)
    e55_read = _read_ema_interaction(candles, e55, 'E55', lookback=8)
    e21_read = _read_ema_interaction(candles, e21, 'E21', lookback=5)

    # Price position relative to EMAs
    above_21 = price > e21[-1]
    above_55 = price > e55[-1]
    above_100 = price > e100[-1]

    if above_21 and above_55 and above_100:
        price_position = 'above_all'
    elif not above_21 and not above_55 and not above_100:
        price_position = 'below_all'
    elif above_100 and not above_21:
        price_position = 'between_emas_bearish_near_term'
    elif not above_100 and above_21:
        price_position = 'between_emas_bullish_near_term'
    else:
        price_position = 'mixed'

    # Distance from E100 as percentage
    e100_dist_pct = (price - e100[-1]) / e100[-1] * 100 if e100[-1] > 0 else 0

    return {
        'e100': e100_read,
        'e55': e55_read,
        'e21': e21_read,
        'price_position': price_position,
        'e100_distance_pct': round(e100_dist_pct, 4),
    }


def _read_ema_interaction(
    candles: List[Dict],
    ema_vals: List[float],
    label: str,
    lookback: int = 10,
) -> Dict[str, Any]:
    """
    Determine how price is interacting with a single EMA over recent candles.
    """
    n = len(candles)
    start = max(0, n - lookback)
    recent_candles = candles[start:]
    recent_ema = ema_vals[start:]

    touches = 0  # wick touched the EMA
    bounces = 0  # touched and closed away (rejection)
    breaks = 0   # closed through the EMA
    wraps = 0    # body straddles the EMA

    for i, (c, ev) in enumerate(zip(recent_candles, recent_ema)):
        if _is_nan(ev):
            continue

        o, h, l, cl = c['open'], c['high'], c['low'], c['close']
        body_top = max(o, cl)
        body_bot = min(o, cl)

        # Did price touch this EMA? (wick reached it)
        touched = l <= ev <= h

        if touched:
            touches += 1

            # Bounce: wick touched but body stayed on one side
            if ev <= body_bot or ev >= body_top:
                bounces += 1
            # Break: body closed through the EMA
            elif i > 0:
                prev_close = recent_candles[i - 1]['close']
                prev_ema = recent_ema[i - 1] if not _is_nan(recent_ema[i - 1]) else ev
                if (prev_close > prev_ema and cl < ev) or (prev_close < prev_ema and cl > ev):
                    breaks += 1
                else:
                    wraps += 1
            else:
                wraps += 1

    # Classify the interaction
    if bounces >= 3 and breaks == 0:
        interaction = 'strong_support' if candles[-1]['close'] > recent_ema[-1] else 'strong_resistance'
    elif bounces >= 2 and breaks == 0:
        interaction = 'support' if candles[-1]['close'] > recent_ema[-1] else 'resistance'
    elif breaks >= 2:
        interaction = 'broken'
    elif wraps >= 3:
        interaction = 'wrapping'  # Price can't decide — consolidation around this level
    elif touches == 0:
        # Price isn't even near this EMA
        dist = abs(candles[-1]['close'] - recent_ema[-1]) / recent_ema[-1] * 100
        interaction = 'distant' if dist > 0.3 else 'approaching'
    else:
        interaction = 'testing'

    return {
        'interaction': interaction,
        'touches': touches,
        'bounces': bounces,
        'breaks': breaks,
        'wraps': wraps,
        'label': label,
    }


# ═══════════════════════════════════════════════════════════════════
# CONSECUTIVE STRUCTURE — Exhaustion runs and compression
# ═══════════════════════════════════════════════════════════════════

def _analyze_consecutive_structure(candles: List[Dict]) -> Dict[str, Any]:
    """
    Read consecutive candle patterns:
    - Exhaustion runs (5+ candles same direction = potential reversal)
    - Compression (shrinking ranges = breakout building)
    - Expansion (growing ranges = momentum confirmed)
    """
    last12 = candles[-12:]

    # Count current consecutive run
    consec_bull = 0
    consec_bear = 0
    for c in reversed(last12):
        if c['close'] > c['open']:
            if consec_bear > 0:
                break
            consec_bull += 1
        elif c['close'] < c['open']:
            if consec_bull > 0:
                break
            consec_bear += 1
        else:
            break  # Doji breaks the run

    # Range compression/expansion
    ranges = [c['high'] - c['low'] for c in last12]
    if len(ranges) >= 6:
        first_half_avg = sum(ranges[:6]) / 6
        second_half_avg = sum(ranges[6:]) / 6
        if first_half_avg > 0:
            range_change = second_half_avg / first_half_avg
        else:
            range_change = 1.0
    else:
        range_change = 1.0

    if range_change < 0.6:
        range_trend = 'compressing'  # Ranges shrinking → breakout building
    elif range_change > 1.5:
        range_trend = 'expanding'  # Ranges growing → momentum
    else:
        range_trend = 'steady'

    # Classify
    if consec_bull >= 5:
        run_state = 'bull_exhaustion_risk'
    elif consec_bear >= 5:
        run_state = 'bear_exhaustion_risk'
    elif consec_bull >= 3:
        run_state = 'bull_momentum'
    elif consec_bear >= 3:
        run_state = 'bear_momentum'
    else:
        run_state = 'neutral'

    return {
        'consec_bull': consec_bull,
        'consec_bear': consec_bear,
        'run_state': run_state,
        'range_trend': range_trend,
        'range_change_ratio': round(range_change, 2),
    }


# ═══════════════════════════════════════════════════════════════════
# NARRATIVE BUILDER
# ═══════════════════════════════════════════════════════════════════

def _build_structure_narrative(
    wick: Dict, body: Dict, ema_int: Dict, consec: Dict
) -> str:
    """Build a human-readable narrative from candle structure analysis."""
    parts = []

    # Wick story
    pressure = wick['dominant_pressure']
    if pressure == 'buying':
        parts.append(f"Wicks show {wick['pressure_strength']} buying pressure (avg lower wick {wick['avg_lower_wick_ratio']:.0%}).")
    elif pressure == 'selling':
        parts.append(f"Wicks show {wick['pressure_strength']} selling pressure (avg upper wick {wick['avg_upper_wick_ratio']:.0%}).")
    else:
        parts.append("Wicks balanced — no clear rejection pressure.")

    # Rejection clusters
    if wick['upper_rejection_cluster']:
        cl = wick['upper_rejection_cluster']
        parts.append(f"Resistance forming at {cl['level']:.5f} ({cl['touches']} rejections).")
    if wick['lower_rejection_cluster']:
        cl = wick['lower_rejection_cluster']
        parts.append(f"Support forming at {cl['level']:.5f} ({cl['touches']} rejections).")

    # Body progression
    if body['body_trend'] == 'growing':
        parts.append(f"Bodies growing ({body['body_change_ratio']:.1f}x) — conviction increasing.")
    elif body['body_trend'] == 'shrinking':
        parts.append(f"Bodies shrinking ({body['body_change_ratio']:.1f}x) — indecision/exhaustion.")

    if body['direction_bias'] in ('strong_bull', 'strong_bear'):
        parts.append(f"Strong {body['direction_bias'].replace('strong_', '')} bias in recent candles.")

    # EMA interaction (E100 is most important)
    e100 = ema_int['e100']
    if e100['interaction'] == 'strong_support':
        parts.append(f"E100 acting as STRONG support ({e100['bounces']} bounces, 0 breaks).")
    elif e100['interaction'] == 'strong_resistance':
        parts.append(f"E100 acting as STRONG resistance ({e100['bounces']} bounces, 0 breaks).")
    elif e100['interaction'] == 'support':
        parts.append(f"E100 providing support ({e100['bounces']} bounces).")
    elif e100['interaction'] == 'resistance':
        parts.append(f"E100 providing resistance ({e100['bounces']} bounces).")
    elif e100['interaction'] == 'broken':
        parts.append(f"E100 BROKEN — structural level lost ({e100['breaks']} breaks).")
    elif e100['interaction'] == 'wrapping':
        parts.append(f"Price wrapping around E100 — consolidation/indecision at key level.")
    elif e100['interaction'] == 'testing':
        parts.append(f"Price testing E100 — watching for bounce or break.")

    # E55 interaction (secondary)
    e55 = ema_int['e55']
    if e55['interaction'] in ('wrapping', 'testing'):
        parts.append(f"Price interacting with E55 ({e55['interaction']}).")

    # Consecutive structure
    if consec['run_state'] == 'bull_exhaustion_risk':
        parts.append(f"{consec['consec_bull']} consecutive bull candles — exhaustion risk.")
    elif consec['run_state'] == 'bear_exhaustion_risk':
        parts.append(f"{consec['consec_bear']} consecutive bear candles — exhaustion risk.")

    if consec['range_trend'] == 'compressing':
        parts.append("Ranges compressing — breakout building.")
    elif consec['range_trend'] == 'expanding':
        parts.append("Ranges expanding — momentum confirmed.")

    # Price position summary
    pos = ema_int['price_position']
    if pos == 'above_all':
        parts.append(f"Price above all EMAs (E100 dist: {ema_int['e100_distance_pct']:+.3f}%).")
    elif pos == 'below_all':
        parts.append(f"Price below all EMAs (E100 dist: {ema_int['e100_distance_pct']:+.3f}%).")

    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def _is_nan(v) -> bool:
    """Check for NaN."""
    try:
        return v != v or math.isnan(v)
    except (TypeError, ValueError):
        return True


def _empty_result(reason: str) -> Dict[str, Any]:
    return {
        'wick_analysis': {},
        'body_progression': {},
        'ema_interaction': {},
        'consecutive_structure': {},
        'structure_narrative': reason,
    }
