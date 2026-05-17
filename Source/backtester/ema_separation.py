#!/usr/bin/env python3
"""
EMA Fan/Separation Detection Module

Implements Tim's EMA 21/55/100 trading strategy:
1. EMA 21 & EMA 55 CROSS → signals direction change
2. EMA 100 stays on one side → resistance (above) or support (below)
3. After cross, SEPARATION between EMA 21 & 55 widens → momentum confirmation
4. ENTER as separation grows
5. EXIT at PEAK SEPARATION — when 21 & 55 are furthest apart = exhaustion
6. If they CONVERGE but DON'T TOUCH → trend still alive, watch for re-entry
7. If they CROSS AGAIN with EMA 100 still on same side → new leg, repeat trade

Pure Python implementation - no external dependencies.
"""

import math
from typing import List, Dict, Any, Optional, Tuple


def calculate_ema(data: List[float], period: int) -> List[float]:
    """Calculate Exponential Moving Average using pure Python."""
    if len(data) < period:
        return [float('nan')] * len(data)
    
    ema = [float('nan')] * len(data)
    
    # Start with SMA for the first EMA value
    ema[period - 1] = sum(data[:period]) / period
    multiplier = 2.0 / (period + 1)
    
    for i in range(period, len(data)):
        ema[i] = data[i] * multiplier + ema[i - 1] * (1 - multiplier)
    
    return ema


def is_nan(value: float) -> bool:
    """Check if a value is NaN."""
    return value != value or value == float('nan')


def detect_ema_crossovers(candles: List[Dict]) -> List[Dict]:
    """
    Detect EMA 21/55 crossover events.
    
    Args:
        candles: List of dicts with 'time', 'open', 'high', 'low', 'close'
    
    Returns:
        List of crossover events: {index, direction, timestamp, ema21, ema55}
    """
    if len(candles) < 55:
        return []
    
    closes = [float(c['close']) for c in candles]
    ema21 = calculate_ema(closes, 21)
    ema55 = calculate_ema(closes, 55)
    
    crossovers = []
    for i in range(22, len(ema21)):  # Start after both EMAs are valid
        if (is_nan(ema21[i]) or is_nan(ema55[i]) or 
            is_nan(ema21[i-1]) or is_nan(ema55[i-1])):
            continue
            
        # Bullish crossover: EMA21 crosses above EMA55
        if ema21[i] > ema55[i] and ema21[i-1] <= ema55[i-1]:
            crossovers.append({
                'index': i,
                'direction': 'bullish',
                'timestamp': candles[i]['time'],
                'ema21': round(ema21[i], 6),
                'ema55': round(ema55[i], 6)
            })
        
        # Bearish crossover: EMA21 crosses below EMA55
        elif ema21[i] < ema55[i] and ema21[i-1] >= ema55[i-1]:
            crossovers.append({
                'index': i,
                'direction': 'bearish',
                'timestamp': candles[i]['time'],
                'ema21': round(ema21[i], 6),
                'ema55': round(ema55[i], 6)
            })
    
    # Filter out fake crosses: if a cross reverses within 5 candles, remove BOTH
    if len(crossovers) >= 2:
        filtered = []
        skip_next = False
        for j in range(len(crossovers)):
            if skip_next:
                skip_next = False
                continue
            if j + 1 < len(crossovers):
                gap = crossovers[j+1]['index'] - crossovers[j]['index']
                if gap <= 5:
                    # Too close — fake cross, skip both
                    skip_next = True
                    continue
            filtered.append(crossovers[j])
        crossovers = filtered
    
    return crossovers


def measure_separation(ema21: List[float], ema55: List[float], prices: List[float]) -> List[float]:
    """
    Measure separation between EMA21 and EMA55 as percentage of price.
    
    Args:
        ema21: EMA 21 values
        ema55: EMA 55 values
        prices: Close prices for normalization
    
    Returns:
        List of separation percentages
    """
    separation = []
    
    for i in range(len(ema21)):
        if (is_nan(ema21[i]) or is_nan(ema55[i]) or 
            is_nan(prices[i]) or prices[i] == 0):
            separation.append(float('nan'))
        else:
            sep_pct = abs(ema21[i] - ema55[i]) / prices[i] * 100
            separation.append(sep_pct)
    
    return separation


def detect_peak_separation(separations: List[float], window: int = 3) -> List[int]:
    """
    Detect peak separation points where derivative changes sign.
    
    Args:
        separations: List of separation values
        window: Lookback window for peak detection
    
    Returns:
        List of indices where separation peaks
    """
    if len(separations) < window * 2 + 1:
        return []
    
    peaks = []
    for i in range(window, len(separations) - window):
        if is_nan(separations[i]):
            continue
            
        # Check if current point is higher than surrounding points
        left_window = separations[i-window:i]
        right_window = separations[i+1:i+window+1]
        
        valid_left = [x for x in left_window if not is_nan(x)]
        valid_right = [x for x in right_window if not is_nan(x)]
        
        if len(valid_left) > 0 and len(valid_right) > 0:
            max_left = max(valid_left)
            max_right = max(valid_right)
            if separations[i] > max_left and separations[i] > max_right:
                peaks.append(i)
    
    return peaks


def detect_deceleration(separations: List[float], lookback: int = 5) -> List[int]:
    """
    Detect where separation growth is DECELERATING — the early exit signal.
    
    Only fires when:
    1. Separation is LARGE (top 20% of the range on this chart — real moves, not noise)
    2. Growth was strong then clearly slowing (sustained deceleration, not one-candle wobble)
    3. Only returns the FIRST deceleration point before each peak (one signal per move)
    
    Returns indices 2-3 candles before actual peaks.
    """
    if len(separations) < lookback + 5:
        return []
    
    # Calculate separation threshold — must be in the upper range to matter
    valid_seps = [s for s in separations if not is_nan(s) and s > 0]
    if len(valid_seps) < 10:
        return []
    valid_seps_sorted = sorted(valid_seps)
    # Top 30% threshold — only care about significant separations
    threshold = valid_seps_sorted[int(len(valid_seps_sorted) * 0.70)]
    threshold = max(threshold, 0.10)  # Data-driven: significant separation only (winners avg 16%)
    
    decel_points = []
    last_decel = -10  # Prevent clustering — minimum 8 candles between signals
    
    for i in range(lookback + 2, len(separations)):
        if is_nan(separations[i]) or is_nan(separations[i-1]) or is_nan(separations[i-2]) or is_nan(separations[i-3]):
            continue
        
        # Must be above the significance threshold
        if separations[i] < threshold:
            continue
        
        # Check sustained growth over last few candles before this point
        growth_count = 0
        for j in range(1, min(lookback, i)):
            if not is_nan(separations[i-j]) and not is_nan(separations[i-j-1]):
                if separations[i-j] > separations[i-j-1]:
                    growth_count += 1
        if growth_count < 3:  # Need at least 3 candles of prior growth
            continue
        
        # First derivatives over last 3 candles
        d1_0 = separations[i] - separations[i-1]
        d1_1 = separations[i-1] - separations[i-2]
        d1_2 = separations[i-2] - separations[i-3]
        
        # Sustained deceleration: growth was positive but slowing for 2+ candles
        if d1_2 > d1_1 > d1_0 and d1_0 >= 0:
            # Growth rate dropped at least 2 candles in a row — momentum dying
            if i - last_decel >= 8:  # No clustering
                decel_points.append(i)
                last_decel = i
    
    return decel_points


def detect_convergence_no_touch(ema21: List[float], ema55: List[float], 
                               threshold_pips: float = 2.0) -> List[int]:
    """
    Detect points where EMAs converge close but don't cross.
    
    Args:
        ema21: EMA 21 values
        ema55: EMA 55 values
        threshold_pips: Threshold in pips for "close" convergence
    
    Returns:
        List of indices where convergence occurs
    """
    if len(ema21) < 3:
        return []
    
    convergences = []
    pip_value = threshold_pips * 0.0001  # Assuming 4-decimal pairs
    
    for i in range(2, len(ema21)):
        if is_nan(ema21[i]) or is_nan(ema55[i]):
            continue
            
        current_diff = abs(ema21[i] - ema55[i])
        
        if not is_nan(ema21[i-1]) and not is_nan(ema55[i-1]):
            prev_diff = abs(ema21[i-1] - ema55[i-1])
        else:
            prev_diff = float('inf')
        
        # Check if they're converging (getting closer) and within threshold
        if current_diff < prev_diff and current_diff <= pip_value:
            # Ensure they maintain the same relative position (no cross)
            if ((ema21[i] > ema55[i]) == (ema21[i-1] > ema55[i-1])):
                convergences.append(i)
    
    return convergences


def classify_ema100_position(ema100: List[float], ema21: List[float], ema55: List[float]) -> List[str]:
    """
    Classify EMA 100 position relative to faster EMAs.
    
    Args:
        ema100: EMA 100 values
        ema21: EMA 21 values  
        ema55: EMA 55 values
    
    Returns:
        List of classifications: 'resistance', 'support', or 'neutral'
    """
    classifications = []
    
    for i in range(len(ema100)):
        if is_nan(ema100[i]) or is_nan(ema21[i]) or is_nan(ema55[i]):
            classifications.append('neutral')
            continue
        
        avg_fast = (ema21[i] + ema55[i]) / 2
        
        if ema100[i] > avg_fast * 1.0001:  # EMA100 above → resistance
            classifications.append('resistance')
        elif ema100[i] < avg_fast * 0.9999:  # EMA100 below → support  
            classifications.append('support')
        else:
            classifications.append('neutral')
    
    return classifications


def _compute_velocity_trend(separations: List[float], current_idx: int, lookback: int = 8) -> str:
    """
    Determine if velocity itself is accelerating, steady, or decelerating.
    Compares recent velocity to earlier velocity within the lookback window.
    """
    if current_idx < lookback + 4:
        return 'unknown'
    
    # Velocity in the recent half vs the earlier half
    mid = lookback // 2
    recent_deltas = []
    earlier_deltas = []
    
    for i in range(1, mid + 1):
        idx = current_idx - i
        if idx >= 1 and not is_nan(separations[idx]) and not is_nan(separations[idx - 1]):
            recent_deltas.append(separations[idx] - separations[idx - 1])
    
    for i in range(mid + 1, lookback + 1):
        idx = current_idx - i
        if idx >= 1 and not is_nan(separations[idx]) and not is_nan(separations[idx - 1]):
            earlier_deltas.append(separations[idx] - separations[idx - 1])
    
    if not recent_deltas or not earlier_deltas:
        return 'unknown'
    
    recent_avg = sum(recent_deltas) / len(recent_deltas)
    earlier_avg = sum(earlier_deltas) / len(earlier_deltas)
    
    diff = recent_avg - earlier_avg
    if diff > 0.002:
        return 'accelerating'
    elif diff < -0.002:
        return 'decelerating'
    else:
        return 'steady'


def _detect_e100_candle_pattern(candles: List[Dict], ema100: List[float], idx: int) -> Optional[Dict]:
    """
    Check if there's a reversal candle pattern at E100 at the given index.
    Returns pattern info or None.
    """
    if idx < 1 or idx >= len(candles) or is_nan(ema100[idx]):
        return None
    
    c = candles[idx]
    close = float(c['close'])
    open_ = float(c['open'])
    high = float(c['high'])
    low = float(c['low'])
    
    body_dist = abs(close - ema100[idx]) / close * 100
    wick_dist = min(abs(high - ema100[idx]), abs(low - ema100[idx])) / close * 100
    near_e100 = body_dist < 0.08 or wick_dist < 0.05
    
    if not near_e100:
        return None
    
    body = abs(close - open_)
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low
    candle_range = high - low
    
    if candle_range == 0:
        return None
    
    patterns = []
    if lower_wick > body * 2 and lower_wick > candle_range * 0.55:
        patterns.append(('hammer', 'buy'))
    if upper_wick > body * 2 and upper_wick > candle_range * 0.55:
        patterns.append(('shooting_star', 'sell'))
    if close > ema100[idx] and lower_wick > candle_range * 0.5 and low <= ema100[idx] * 1.0005:
        patterns.append(('rejection_bounce_up', 'buy'))
    if close < ema100[idx] and upper_wick > candle_range * 0.5 and high >= ema100[idx] * 0.9995:
        patterns.append(('rejection_bounce_down', 'sell'))
    
    # Check engulfing
    if idx > 0:
        prev_close = float(candles[idx - 1]['close'])
        prev_open = float(candles[idx - 1]['open'])
        prev_body = abs(prev_close - prev_open)
        bullish = close > open_
        if bullish and prev_close < prev_open and body > prev_body * 1.1:
            if open_ <= prev_close and close >= prev_open:
                patterns.append(('bullish_engulfing', 'buy'))
        if not bullish and prev_close > prev_open and body > prev_body * 1.1:
            if open_ >= prev_close and close <= prev_open:
                patterns.append(('bearish_engulfing', 'sell'))
    
    if patterns:
        return {'name': patterns[0][0], 'direction': patterns[0][1], 'proximity_pct': round(min(body_dist, wick_dist), 4)}
    return None


def scan_ema_signals(candles: List[Dict]) -> Dict[str, Any]:
    """
    Main function to scan for EMA Fan/Separation signals.
    
    Returns a comprehensive EMA context dict including:
    - Basic signal/phase/strength (legacy)
    - Three gap measurements (21-55, 55-100, price-to-100)
    - Fan state and velocity trend
    - Trend health score (0-100)
    - Reversal risk assessment
    - E100 candle pattern detection
    - Recommended directional bias
    - Plain English narrative for agents
    """
    _empty = {
        'signal': 'neutral',
        'phase': 'insufficient_data',
        'separation_pct': 0.0,
        'ema100_role': 'neutral',
        'strength': 0,
        'entry_suggested': False,
        'exit_suggested': False,
        'crossovers': [],
        'current_emas': {},
        'separation_velocity': 0.0,
        'bars_since_crossover': None,
        'bars_since_cross2': None,
        'bars_since_cross3': None,
        'cross3_direction': None,
        'candles_below_e100': 0,
        'candles_above_e100': 0,
        'last_close_vs_e100': 'unknown',
        'e100_rejections_from_below': 0,
        'e100_rejections_from_above': 0,
        'cascade_phase': 0,
        'recent_peaks': 0,
        'recent_convergences': 0,
        # New narrative fields
        'gap_21_55': 0.0,
        'gap_55_100': 0.0,
        'gap_price_100': 0.0,
        'fan_state': 'no_data',
        'fan_direction': 'mixed',
        'fan_ordered': False,
        'fan_velocity_trend': 'unknown',
        'trend_health': 0,
        'reversal_risk': 'unknown',
        'e100_candle_pattern': None,
        'recommended_bias': 'neutral',
        'narrative': 'Insufficient candle data for EMA analysis.',
    }
    
    if len(candles) < 100:
        return _empty
    
    # Calculate EMAs
    closes = [float(c['close']) for c in candles]
    ema21 = calculate_ema(closes, 21)
    ema55 = calculate_ema(closes, 55)
    ema100 = calculate_ema(closes, 100)
    
    # Get current values
    ci = len(candles) - 1  # current index
    e21 = ema21[ci] if not is_nan(ema21[ci]) else None
    e55 = ema55[ci] if not is_nan(ema55[ci]) else None
    e100v = ema100[ci] if not is_nan(ema100[ci]) else None
    price = closes[ci]
    
    if e21 is None or e55 is None or e100v is None:
        return _empty
    
    # ── Core detections ──────────────────────────────────────────────
    crossovers = detect_ema_crossovers(candles)
    recent_cross = crossovers[-1] if crossovers else None

    # Cross 2: E21 × E100 — fan fully ordered, the confirmation cross
    _cross2_list = []
    for _i in range(101, len(ema21)):
        if is_nan(ema21[_i]) or is_nan(ema100[_i]) or is_nan(ema21[_i-1]) or is_nan(ema100[_i-1]):
            continue
        if ema21[_i] > ema100[_i] and ema21[_i-1] <= ema100[_i-1]:
            _cross2_list.append({'index': _i, 'direction': 'bullish'})
        elif ema21[_i] < ema100[_i] and ema21[_i-1] >= ema100[_i-1]:
            _cross2_list.append({'index': _i, 'direction': 'bearish'})
    _recent_cross2 = _cross2_list[-1] if _cross2_list else None
    bars_since_cross2 = (ci - _recent_cross2['index']) if _recent_cross2 else None

    # 2026-04-27: Cross 3 (E55 × E100) — the cascade-completion cross.
    # When this fires after Cross 1 + Cross 2, the fan is fully ordered.
    # Without this signal the TA agent reads "tangled" when E55 and E100 are
    # close together; with it the structural cascade is explicit.
    _cross3_list = []
    for _i in range(101, len(ema55)):
        if is_nan(ema55[_i]) or is_nan(ema100[_i]) or is_nan(ema55[_i-1]) or is_nan(ema100[_i-1]):
            continue
        if ema55[_i] > ema100[_i] and ema55[_i-1] <= ema100[_i-1]:
            _cross3_list.append({'index': _i, 'direction': 'bullish'})
        elif ema55[_i] < ema100[_i] and ema55[_i-1] >= ema100[_i-1]:
            _cross3_list.append({'index': _i, 'direction': 'bearish'})
    _recent_cross3 = _cross3_list[-1] if _cross3_list else None
    bars_since_cross3 = (ci - _recent_cross3['index']) if _recent_cross3 else None
    cross3_direction = _recent_cross3['direction'] if _recent_cross3 else None

    # 2026-04-27: Candle position vs E100 — count how many of the last N
    # closes are above/below E100. Tells the TA whether price is decisively
    # on one side of the trend structure, complementing the cross signals.
    _vs_e100_lookback = 10
    candles_below_e100 = 0
    candles_above_e100 = 0
    for _i in range(max(0, ci - _vs_e100_lookback + 1), ci + 1):
        if is_nan(ema100[_i]):
            continue
        if closes[_i] < ema100[_i]:
            candles_below_e100 += 1
        elif closes[_i] > ema100[_i]:
            candles_above_e100 += 1
    last_close_vs_e100 = (
        'below' if (not is_nan(ema100[ci]) and closes[ci] < ema100[ci]) else
        'above' if (not is_nan(ema100[ci]) and closes[ci] > ema100[ci]) else
        'unknown'
    )

    # 2026-04-27: E100 rejection counter — wicks that touched E100 but bodies
    # closed away. A test-and-reject signals E100 acting as resistance/support.
    _rej_lookback = 20
    e100_rej_from_below = 0  # body below E100, wick touched up; E100 = resistance
    e100_rej_from_above = 0  # body above E100, wick touched down; E100 = support
    for _i in range(max(0, ci - _rej_lookback + 1), ci + 1):
        if is_nan(ema100[_i]):
            continue
        h = float(candles[_i].get('high', 0))
        l = float(candles[_i].get('low', 0))
        o = float(candles[_i].get('open', 0))
        c_ = float(candles[_i].get('close', 0))
        body_low, body_high = min(o, c_), max(o, c_)
        if h >= ema100[_i] and body_high < ema100[_i]:
            e100_rej_from_below += 1
        if l <= ema100[_i] and body_low > ema100[_i]:
            e100_rej_from_above += 1

    # 2026-04-27: Cascade phase aggregate — single integer summarizing where
    # we are in the trend-formation sequence. The TA prompt uses this to
    # narrate cleanly instead of falling back to "tangled" when EMAs cluster.
    #   Phase 0: no recent activity
    #   Phase 1: cross1 (E21/E55) within 30 bars
    #   Phase 2: + cross2 (E21/E100) within 30 bars
    #   Phase 3: + cross3 (E55/E100) → fan fully ordered
    #   Phase 4: phase 3 confirmed by price action (>=7 of last 10 closes
    #            on the trend-correct side of E100)
    _fan_bull_local = e21 > e55 > e100v
    _fan_bear_local = e100v > e55 > e21
    _c1_bars_for_phase = (ci - recent_cross['index']) if recent_cross else None
    cascade_phase = 0
    if _c1_bars_for_phase is not None and _c1_bars_for_phase <= 50:
        cascade_phase = 1
        if bars_since_cross2 is not None and bars_since_cross2 <= 50:
            cascade_phase = 2
            if (bars_since_cross3 is not None and bars_since_cross3 <= 50
                and (_fan_bull_local or _fan_bear_local)):
                cascade_phase = 3
                if ((_fan_bull_local and candles_above_e100 >= 7)
                    or (_fan_bear_local and candles_below_e100 >= 7)):
                    cascade_phase = 4
    
    separations = measure_separation(ema21, ema55, closes)
    cur_sep = separations[ci] if not is_nan(separations[ci]) else 0.0
    
    peaks = detect_peak_separation(separations)
    convergences = detect_convergence_no_touch(ema21, ema55)
    decel_points = detect_deceleration(separations)
    
    ema100_positions = classify_ema100_position(ema100, ema21, ema55)
    e100_role = ema100_positions[ci]
    
    # ── Three gap measurements ───────────────────────────────────────
    gap_21_55 = abs(e21 - e55) / price * 100          # crossover signal
    gap_55_100 = abs(e55 - e100v) / price * 100       # trend structure depth
    gap_price_100 = (price - e100v) / price * 100      # signed: +above, -below
    
    # ── Fan ordering ─────────────────────────────────────────────────
    fan_bullish = e21 > e55 > e100v     # perfect bull fan
    fan_bearish = e100v > e55 > e21     # perfect bear fan
    fan_ordered = fan_bullish or fan_bearish
    fan_direction = 'bullish' if fan_bullish else ('bearish' if fan_bearish else 'mixed')
    
    # ── Velocity ─────────────────────────────────────────────────────
    bars_since = (ci - recent_cross['index']) if recent_cross else None
    if bars_since and bars_since > 0:
        sep_velocity = cur_sep / bars_since
    else:
        sep_velocity = 0.0
    
    vel_trend = _compute_velocity_trend(separations, ci)
    
    # ── Fan state (expanding / stable / contracting / crossed / converging) ──
    # Look at last 5 bars of separation trend
    recent_seps = [separations[ci - j] for j in range(min(5, ci)) if not is_nan(separations[ci - j])]
    if len(recent_seps) >= 3:
        growth = recent_seps[0] - recent_seps[-1]  # positive = expanding
        if growth > 0.005:
            fan_state = 'expanding'
        elif growth < -0.005:
            fan_state = 'contracting'
        else:
            fan_state = 'stable'
    elif len(recent_seps) >= 2:
        # Fallback with fewer bars - still useful data
        growth = recent_seps[0] - recent_seps[-1]
        if growth > 0.008:  # Higher threshold for less data
            fan_state = 'expanding'
        elif growth < -0.008:
            fan_state = 'contracting'
        else:
            fan_state = 'stable'
    else:
        # No separation data - determine state from EMA positions
        if fan_ordered and cur_sep > 0.01:
            fan_state = 'stable'  # Ordered fan with separation = stable trend
        elif recent_cross and bars_since is not None and bars_since <= 2:
            fan_state = 'just_crossed'  # Very recent cross
        else:
            fan_state = 'forming'  # Fan is forming but not yet clear
    
    # Override for special conditions
    if recent_cross and bars_since is not None and bars_since <= 3:
        fan_state = 'just_crossed'
    
    # Check deceleration / peak
    recent_decel = [d for d in decel_points if ci - d <= 3]
    nearby_peaks = [p for p in peaks if abs(ci - p) <= 3]
    if recent_decel and cur_sep > 0.03:
        fan_state = 'decelerating'
    if nearby_peaks and cur_sep > 0.05:
        fan_state = 'peaked'
    
    # ── Legacy phase/signal (keep backward compat) ───────────────────
    signal = 'neutral'
    phase = 'ranging'
    strength = 0
    entry_suggested = False
    exit_suggested = False
    
    if recent_cross and bars_since is not None and bars_since <= 50:
        cross_dir = recent_cross['direction']
        cross_sep = separations[recent_cross['index']] if not is_nan(separations[recent_cross['index']]) else 0
        sep_trend = cur_sep - cross_sep
        
        if sep_trend > 0.01:
            phase = 'separating'
            strength = min(int(sep_trend * 1000), 100)
            if e100_role in ('support', 'resistance'):
                signal = 'buy' if cross_dir == 'bullish' else 'sell'
                if cur_sep >= 0.10 and sep_velocity >= 0.005:
                    entry_suggested = True
        elif sep_trend < -0.01:
            phase = 'converging'
            strength = max(int(-sep_trend * 1000), 20)
        else:
            phase = 'stable'
            strength = 40
        
        if fan_state == 'decelerating':
            phase = 'decelerating'
            exit_suggested = True
            strength = max(strength, 85)
        if fan_state == 'peaked':
            phase = 'peak'
            exit_suggested = True
            strength = max(strength, 90)
    
    if (not recent_cross or (bars_since is not None and bars_since > 50)):
        recent_convs = [c for c in convergences if ci - c <= 10]
        if recent_convs and cur_sep > 0.08:
            phase = 're-separating'
            strength = min(int(cur_sep * 2000), 100)
            if e21 > e55:
                signal = 'buy'
                if e100_role == 'support' and cur_sep >= 0.10 and sep_velocity >= 0.005:
                    entry_suggested = True
            else:
                signal = 'sell'
                if e100_role == 'resistance' and cur_sep >= 0.10 and sep_velocity >= 0.005:
                    entry_suggested = True
    
    # ── E100 candle pattern ──────────────────────────────────────────
    e100_pattern = _detect_e100_candle_pattern(candles, ema100, ci)
    # Also check previous bar (pattern may have just completed)
    if not e100_pattern:
        e100_pattern = _detect_e100_candle_pattern(candles, ema100, ci - 1)
    
    # ── Trend health (0-100) ─────────────────────────────────────────
    # Composite: fan ordered? + velocity + separation level + velocity trend
    health = 0
    if fan_ordered:
        health += 30
    if sep_velocity >= 0.007:
        health += 25
    elif sep_velocity >= 0.005:
        health += 15
    elif sep_velocity >= 0.003:
        health += 8
    if cur_sep >= 0.20:
        health += 20
    elif cur_sep >= 0.10:
        health += 12
    elif cur_sep >= 0.05:
        health += 6
    if vel_trend == 'accelerating':
        health += 15
    elif vel_trend == 'steady':
        health += 8
    elif vel_trend == 'decelerating':
        health += 0
    if gap_55_100 > 0.10:
        health += 10  # Deep trend structure
    elif gap_55_100 > 0.05:
        health += 5
    trend_health = min(health, 100)
    
    # ── Reversal risk ────────────────────────────────────────────────
    risk_score = 0
    if fan_state == 'contracting':
        risk_score += 3
    if fan_state in ('decelerating', 'peaked'):
        risk_score += 4
    if vel_trend == 'decelerating':
        risk_score += 2
    if not fan_ordered:
        risk_score += 2
    if nearby_peaks:
        risk_score += 3
    recent_conv_count = len([c for c in convergences if ci - c <= 10])
    if recent_conv_count > 0:
        risk_score += 2
    
    if risk_score >= 6:
        reversal_risk = 'high'
    elif risk_score >= 3:
        reversal_risk = 'moderate'
    else:
        reversal_risk = 'low'
    
    # ── Recommended bias ─────────────────────────────────────────────
    if fan_bullish and trend_health >= 60:
        recommended_bias = 'strong_bull'
    elif fan_bullish or (signal == 'buy' and trend_health >= 30):
        recommended_bias = 'bull'
    elif fan_bearish and trend_health >= 60:
        recommended_bias = 'strong_bear'
    elif fan_bearish or (signal == 'sell' and trend_health >= 30):
        recommended_bias = 'bear'
    else:
        recommended_bias = 'neutral'
    
    # ── Generate narrative ───────────────────────────────────────────
    narrative = generate_ema_narrative(
        fan_direction=fan_direction,
        fan_state=fan_state,
        cur_sep=cur_sep,
        sep_velocity=sep_velocity,
        vel_trend=vel_trend,
        e100_role=e100_role,
        gap_price_100=gap_price_100,
        gap_55_100=gap_55_100,
        bars_since=bars_since,
        trend_health=trend_health,
        reversal_risk=reversal_risk,
        e100_pattern=e100_pattern,
        recommended_bias=recommended_bias,
        cross_dir=recent_cross['direction'] if recent_cross else None,
    )
    
    return {
        'signal': signal,
        'phase': phase,
        'separation_pct': round(cur_sep, 4),
        'ema100_role': e100_role,
        'strength': strength,
        'entry_suggested': entry_suggested,
        'exit_suggested': exit_suggested,
        'crossovers': crossovers,
        'current_emas': {
            'ema21': round(e21, 6),
            'ema55': round(e55, 6),
            'ema100': round(e100v, 6),
        },
        'separation_velocity': round(sep_velocity, 6),
        'bars_since_crossover': bars_since,
        'bars_since_cross2': bars_since_cross2,  # Cross 2: E21×E100 — fan fully ordered
        # 2026-04-27: Cascade fields — explicit cross sequence + price-vs-E100
        # for the TA agent. Without these the model reads "tangled" when the
        # EMAs cluster on top of each other.
        'bars_since_cross3': bars_since_cross3,  # Cross 3: E55×E100 — cascade complete
        'cross3_direction': cross3_direction,    # bullish | bearish | None
        'candles_below_e100': candles_below_e100,  # of last 10
        'candles_above_e100': candles_above_e100,
        'last_close_vs_e100': last_close_vs_e100,
        'e100_rejections_from_below': e100_rej_from_below,  # E100 = resistance
        'e100_rejections_from_above': e100_rej_from_above,  # E100 = support
        'cascade_phase': cascade_phase,          # 0..4
        'recent_peaks': len([p for p in peaks if ci - p <= 10]),
        'recent_convergences': len([c for c in convergences if ci - c <= 10]),
        # New narrative fields
        'gap_21_55': round(gap_21_55, 4),
        'gap_55_100': round(gap_55_100, 4),
        'gap_price_100': round(gap_price_100, 4),
        'fan_state': fan_state,
        'fan_direction': fan_direction,
        'fan_ordered': fan_ordered,
        'fan_velocity_trend': vel_trend,
        'trend_health': trend_health,
        'reversal_risk': reversal_risk,
        'e100_candle_pattern': e100_pattern,
        'recommended_bias': recommended_bias,
        'narrative': narrative,
    }


def generate_ema_narrative(
    fan_direction: str,
    fan_state: str,
    cur_sep: float,
    sep_velocity: float,
    vel_trend: str,
    e100_role: str,
    gap_price_100: float,
    gap_55_100: float,
    bars_since: Optional[int],
    trend_health: int,
    reversal_risk: str,
    e100_pattern: Optional[Dict],
    recommended_bias: str,
    cross_dir: Optional[str],
) -> str:
    """
    Generate a plain-English narrative of the EMA market state.
    This is what agents read to understand the directional picture.
    """
    parts = []
    
    # ── Opening: current direction ───────────────────────────────────
    if fan_direction == 'bullish':
        dir_text = "Bullish"
    elif fan_direction == 'bearish':
        dir_text = "Bearish"
    else:
        dir_text = "Mixed/no clear"
    
    if cross_dir and bars_since is not None:
        parts.append(f"{dir_text} bias. {cross_dir.title()} cross {bars_since} bars ago.")
    else:
        parts.append(f"{dir_text} bias. No recent crossover.")
    
    # ── Fan state + velocity ─────────────────────────────────────────
    vel_label = 'fast' if sep_velocity >= 0.007 else ('moderate' if sep_velocity >= 0.005 else 'slow')
    
    state_text = {
        'expanding': f"Fan expanding ({vel_label} velocity {sep_velocity:.4f}%/bar), separation {cur_sep:.3f}%.",
        'stable': f"Fan stable, separation {cur_sep:.3f}%, velocity {vel_label} ({sep_velocity:.4f}%/bar).",
        'contracting': f"Fan CONTRACTING — separation {cur_sep:.3f}% and shrinking. Potential direction change forming.",
        'just_crossed': f"FRESH CROSS — fan just starting to form, separation {cur_sep:.3f}%. Watch velocity.",
        'decelerating': f"Fan DECELERATING — separation {cur_sep:.3f}% but growth is slowing. Momentum fading.",
        'peaked': f"Fan at or near PEAK separation ({cur_sep:.3f}%). Move is exhausting — exit territory.",
        'unknown': f"Fan state unclear, separation {cur_sep:.3f}%.",
    }.get(fan_state, f"Fan {fan_state}, separation {cur_sep:.3f}%.")
    parts.append(state_text)
    
    # ── Velocity trend (acceleration of the acceleration) ────────────
    if vel_trend == 'accelerating':
        parts.append("Velocity ACCELERATING — trend gaining strength.")
    elif vel_trend == 'decelerating':
        parts.append("Velocity decelerating — momentum waning.")
    # 'steady' — not worth mentioning, already covered
    
    # ── E100 role + price distance ───────────────────────────────────
    e100_word = 'support' if e100_role == 'support' else ('resistance' if e100_role == 'resistance' else 'neutral')
    price_side = 'above' if gap_price_100 > 0 else 'below'
    parts.append(f"E100 acting as {e100_word}. Price {abs(gap_price_100):.3f}% {price_side} E100.")
    
    # ── Trend structure depth ────────────────────────────────────────
    if gap_55_100 > 0.15:
        parts.append(f"Deep trend structure (55-100 gap {gap_55_100:.3f}%).")
    elif gap_55_100 < 0.03:
        parts.append(f"Shallow structure — E55 and E100 are close ({gap_55_100:.3f}%), trend is young or weak.")
    
    # ── E100 candle pattern ──────────────────────────────────────────
    if e100_pattern:
        pn = e100_pattern['name'].replace('_', ' ')
        pd = e100_pattern['direction']
        parts.append(f"⚡ Candle pattern at E100: {pn} ({pd}) — {e100_pattern['proximity_pct']:.3f}% from E100.")
    
    # ── Summary assessment ───────────────────────────────────────────
    health_word = 'strong' if trend_health >= 65 else ('moderate' if trend_health >= 35 else 'weak')
    parts.append(f"Trend health: {health_word} ({trend_health}/100). Reversal risk: {reversal_risk}.")
    
    # ── Actionable guidance ──────────────────────────────────────────
    if reversal_risk == 'high' and fan_state in ('contracting', 'peaked', 'decelerating'):
        if fan_direction == 'bullish':
            parts.append("ACTION: Tighten stops on longs. Watch for bearish reversal patterns at E100.")
        elif fan_direction == 'bearish':
            parts.append("ACTION: Tighten stops on shorts. Watch for bullish reversal patterns at E100.")
        else:
            parts.append("ACTION: No clear direction — avoid new positions, wait for cross.")
    elif fan_state == 'expanding' and trend_health >= 50:
        if fan_direction == 'bullish':
            parts.append("ACTION: Trend healthy. Good window for counter-trend SHORT snipes on overextension. Hold existing longs.")
        elif fan_direction == 'bearish':
            parts.append("ACTION: Trend healthy. Good window for counter-trend LONG snipes on overextension. Hold existing shorts.")
    elif fan_state == 'just_crossed':
        parts.append("ACTION: New cross — wait for fan to confirm with velocity before entering. Don't chase.")
    elif vel_trend == 'accelerating' and fan_state == 'expanding':
        parts.append("ACTION: Trend STRENGTHENING. Not ideal for counter-trend entries yet — wait for deceleration.")
    
    return " ".join(parts)


def generate_market_picture(pair: str, candles_primary: List[Dict], candles_secondary: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """
    Build a unified market picture for a pair combining EMA context with
    RSI, Stochastic, and Bollinger Band readings.
    
    This is the SINGLE data structure that agents, scout, and confluence
    scoring all consume. One call = complete market state.
    
    Args:
        pair: e.g. 'EUR_USD'
        candles_primary: M15 candles — EMAs, BBs, RSI, Stoch computed on this (trading timeframe)
        candles_secondary: Optional higher timeframe candles (H1) for velocity cross-check
    
    Returns:
        Unified market picture dict
    """
    picture = {
        'pair': pair,
        'timestamp': candles_primary[-1]['time'] if candles_primary else None,
        'ema': {},
        'ema_narrative': '',
        'rsi': {},
        'stochastic': {},
        'bollinger': {},
        'candle_pattern_at_e100': None,
        'confluence_narrative': '',
        'trend_health': 0,
        'reversal_risk': 'unknown',
        'recommended_bias': 'neutral',
    }
    
    if len(candles_primary) < 100:
        picture['ema_narrative'] = 'Insufficient data.'
        picture['confluence_narrative'] = 'Insufficient data for market analysis.'
        return picture
    
    # ── EMA context (M15 primary) ─────────────────────────────────────
    ema_data = scan_ema_signals(candles_primary)
    # scan_ema_signals already computes fan_direction from actual EMA ordering
    # (line 487: bullish if e21>e55>e100, bearish if e100>e55>e21, mixed).
    # DO NOT override it — the previous code re-derived from recommended_bias
    # which missed 'strong_bull'/'strong_bear' and collapsed them to 'neutral'.
    # Just normalise 'mixed' → 'neutral' for downstream gate compatibility.
    if ema_data.get('fan_direction') == 'mixed':
        ema_data['fan_direction'] = 'neutral'
    picture['ema'] = ema_data
    picture['ema_narrative'] = ema_data.get('narrative', '')
    picture['trend_health'] = ema_data.get('trend_health', 0)
    picture['reversal_risk'] = ema_data.get('reversal_risk', 'unknown')
    picture['recommended_bias'] = ema_data.get('recommended_bias', 'neutral')
    picture['candle_pattern_at_e100'] = ema_data.get('e100_candle_pattern')
    
    # If M15 candles available, get higher-resolution velocity
    if candles_secondary and len(candles_secondary) >= 100:
        secondary_ema = scan_ema_signals(candles_secondary)
        secondary_vel = secondary_ema.get('separation_velocity', 0)
        primary_vel = ema_data.get('separation_velocity', 0)
        # Use whichever velocity is higher (M15 detects between H1 bars)
        picture['ema']['secondary_velocity'] = round(secondary_vel, 6)
        if secondary_vel > primary_vel:
            picture['ema']['velocity_source'] = 'secondary'
        else:
            picture['ema']['velocity_source'] = 'primary'
    
    # ── RSI (14-period) ──────────────────────────────────────────────
    closes = [float(c['close']) for c in candles_primary]
    rsi_val = _compute_rsi(closes, 14)
    if rsi_val is not None:
        if rsi_val <= 30:
            rsi_zone = 'oversold'
        elif rsi_val >= 70:
            rsi_zone = 'overbought'
        elif rsi_val <= 40:
            rsi_zone = 'approaching_oversold'
        elif rsi_val >= 60:
            rsi_zone = 'approaching_overbought'
        else:
            rsi_zone = 'neutral'
        picture['rsi'] = {'value': round(rsi_val, 2), 'zone': rsi_zone}
    
    # ── Stochastic (14,3,3) ──────────────────────────────────────────
    stoch = _compute_stochastic(candles_primary, 14, 3, 3)
    if stoch:
        if stoch['k'] <= 20:
            stoch_zone = 'oversold'
        elif stoch['k'] >= 80:
            stoch_zone = 'overbought'
        else:
            stoch_zone = 'neutral'
        picture['stochastic'] = {'k': round(stoch['k'], 2), 'd': round(stoch['d'], 2), 'zone': stoch_zone}
    
    # ── Bollinger Bands (20,2) ───────────────────────────────────────
    bb = _compute_bollinger(closes, 20, 2)
    if bb:
        price = closes[-1]
        if price <= bb['lower']:
            bb_pos = 'below_lower'
        elif price >= bb['upper']:
            bb_pos = 'above_upper'
        elif price < bb['middle']:
            bb_pos = 'lower_half'
        else:
            bb_pos = 'upper_half'
        bb_width = (bb['upper'] - bb['lower']) / bb['middle'] * 100
        squeeze = bb_width < 1.0  # tight bands = low volatility

        # ── BB bandwidth rate-of-change (acceleration) ───────────────
        # Compute bandwidth over last N bars to detect expansion/contraction
        bb_widths = _compute_bollinger_bandwidth_series(closes, 20, 2)
        bb_expanding = False
        bb_contracting = False
        bb_acceleration = 0.0  # positive = expanding faster, negative = contracting
        bb_width_trend = 'stable'
        if len(bb_widths) >= 8:
            recent_bw = bb_widths[-4:]   # last 4 bars
            earlier_bw = bb_widths[-8:-4] # previous 4 bars
            avg_recent = sum(recent_bw) / len(recent_bw)
            avg_earlier = sum(earlier_bw) / len(earlier_bw)
            bb_acceleration = avg_recent - avg_earlier
            if bb_acceleration > 0.02:
                bb_expanding = True
                bb_width_trend = 'expanding'
            elif bb_acceleration < -0.02:
                bb_contracting = True
                bb_width_trend = 'contracting'

        picture['bollinger'] = {
            'upper': round(bb['upper'], 6),
            'middle': round(bb['middle'], 6),
            'lower': round(bb['lower'], 6),
            'position': bb_pos,
            'width_pct': round(bb_width, 3),
            'squeeze': squeeze,
            'bb_expanding': bb_expanding,
            'bb_contracting': bb_contracting,
            'bb_acceleration': round(bb_acceleration, 4),
            'bb_width_trend': bb_width_trend,
        }
    
    # ── Confluence narrative (the full story) ────────────────────────
    picture['confluence_narrative'] = _build_confluence_narrative(picture)
    
    return picture


def _compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Compute RSI from close prices."""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    
    if len(gains) < period:
        return None
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _compute_stochastic(candles: List[Dict], k_period: int = 14, k_smooth: int = 3, d_smooth: int = 3) -> Optional[Dict]:
    """Compute Stochastic K and D."""
    if len(candles) < k_period + k_smooth + d_smooth:
        return None
    
    highs = [float(c['high']) for c in candles]
    lows = [float(c['low']) for c in candles]
    closes = [float(c['close']) for c in candles]
    
    # Raw %K
    raw_k = []
    for i in range(k_period - 1, len(candles)):
        h = max(highs[i - k_period + 1:i + 1])
        l = min(lows[i - k_period + 1:i + 1])
        if h == l:
            raw_k.append(50.0)
        else:
            raw_k.append((closes[i] - l) / (h - l) * 100)
    
    if len(raw_k) < k_smooth:
        return None
    
    # Smoothed %K
    smooth_k = []
    for i in range(k_smooth - 1, len(raw_k)):
        smooth_k.append(sum(raw_k[i - k_smooth + 1:i + 1]) / k_smooth)
    
    if len(smooth_k) < d_smooth:
        return None
    
    # %D
    d_vals = []
    for i in range(d_smooth - 1, len(smooth_k)):
        d_vals.append(sum(smooth_k[i - d_smooth + 1:i + 1]) / d_smooth)
    
    return {'k': smooth_k[-1], 'd': d_vals[-1]}


def _compute_bollinger(closes: List[float], period: int = 20, std_mult: float = 2.0) -> Optional[Dict]:
    """Compute Bollinger Bands."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = variance ** 0.5
    return {
        'upper': middle + std_mult * std,
        'middle': middle,
        'lower': middle - std_mult * std,
    }


def _compute_bollinger_bandwidth_series(closes: List[float], period: int = 20, std_mult: float = 2.0) -> List[float]:
    """Compute Bollinger bandwidth (upper-lower)/middle as % for each bar where we have enough data."""
    if len(closes) < period:
        return []
    widths = []
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        middle = sum(window) / period
        if middle == 0:
            widths.append(0.0)
            continue
        variance = sum((x - middle) ** 2 for x in window) / period
        std = variance ** 0.5
        upper = middle + std_mult * std
        lower = middle - std_mult * std
        widths.append((upper - lower) / middle * 100)
    return widths


def _build_confluence_narrative(picture: Dict) -> str:
    """
    Combine EMA narrative with RSI/Stoch/BB into one unified market story.
    This is what agents read to understand the COMPLETE picture.
    """
    parts = []
    
    # Start with EMA direction
    ema = picture.get('ema', {})
    bias = picture.get('recommended_bias', 'neutral')
    parts.append(f"[EMA] {picture.get('ema_narrative', 'No EMA data.')}")
    
    # RSI context
    rsi = picture.get('rsi', {})
    if rsi:
        rsi_val = rsi.get('value', 50)
        rsi_zone = rsi.get('zone', 'neutral')
        parts.append(f"[RSI] {rsi_val} ({rsi_zone}).")
    
    # Stochastic context
    stoch = picture.get('stochastic', {})
    if stoch:
        parts.append(f"[Stoch] K={stoch.get('k', 50)} D={stoch.get('d', 50)} ({stoch.get('zone', 'neutral')}).")
    
    # Bollinger context
    bb = picture.get('bollinger', {})
    if bb:
        pos = bb.get('position', 'unknown')
        squeeze = bb.get('squeeze', False)
        squeeze_text = " SQUEEZE (low volatility, breakout pending)." if squeeze else ""
        bw_trend = bb.get('bb_width_trend', 'stable')
        bw_extra = ""
        if bw_trend == 'expanding':
            bw_extra = f" Bands EXPANDING (accel {bb.get('bb_acceleration', 0):+.3f}) — volatility confirming the move."
        elif bw_trend == 'contracting':
            bw_extra = f" Bands CONTRACTING (accel {bb.get('bb_acceleration', 0):+.3f}) — move losing steam."
        parts.append(f"[BB] Price {pos.replace('_', ' ')}, width {bb.get('width_pct', 0):.2f}%.{squeeze_text}{bw_extra}")
    
    # ── The synthesis: what does it all mean together? ────────────────
    ema_dir = ema.get('fan_direction', 'mixed')
    fan_st = ema.get('fan_state', 'unknown')
    rsi_zone = rsi.get('zone', 'neutral') if rsi else 'neutral'
    stoch_zone = stoch.get('zone', 'neutral') if stoch else 'neutral'
    bb_pos = bb.get('position', 'neutral') if bb else 'neutral'
    e100_pat = picture.get('candle_pattern_at_e100')
    
    # Counter-trend reversal setup
    if ema_dir == 'bearish' and rsi_zone in ('oversold', 'approaching_oversold') and stoch_zone == 'oversold':
        if fan_st in ('decelerating', 'peaked', 'contracting'):
            parts.append("⚡ STRONG counter-trend LONG setup: bearish trend exhausting + RSI/Stoch oversold + fan weakening.")
            if e100_pat and e100_pat['direction'] == 'buy':
                parts.append(f"CONFIRMED: {e100_pat['name'].replace('_', ' ')} reversal candle at E100.")
        elif fan_st == 'expanding':
            parts.append("⚠ Counter-trend LONG possible (RSI/Stoch oversold) but fan still expanding — wait for deceleration for safer entry.")
    
    elif ema_dir == 'bullish' and rsi_zone in ('overbought', 'approaching_overbought') and stoch_zone == 'overbought':
        if fan_st in ('decelerating', 'peaked', 'contracting'):
            parts.append("⚡ STRONG counter-trend SHORT setup: bullish trend exhausting + RSI/Stoch overbought + fan weakening.")
            if e100_pat and e100_pat['direction'] == 'sell':
                parts.append(f"CONFIRMED: {e100_pat['name'].replace('_', ' ')} reversal candle at E100.")
        elif fan_st == 'expanding':
            parts.append("⚠ Counter-trend SHORT possible (RSI/Stoch overbought) but fan still expanding — wait for deceleration.")
    
    # EMA expanding + BB expanding = high conviction move
    elif fan_st in ('expanding', 'accelerating') and bb.get('bb_expanding'):
        dir_word = 'LONG' if ema_dir == 'bullish' else ('SHORT' if ema_dir == 'bearish' else 'directional')
        parts.append(f"⚡ EMA fan expanding + BB bands widening — DOUBLE CONFIRMATION {dir_word} move. High conviction.")

    # BB squeeze + EMA cross = potential breakout
    elif bb.get('squeeze') and fan_st == 'just_crossed':
        parts.append("🔔 BB squeeze + fresh EMA cross — breakout forming. Watch direction of fan for confirmation.")
    
    # Ranging market
    elif ema_dir == 'mixed' and rsi_zone == 'neutral' and not bb.get('squeeze'):
        parts.append("Market ranging — no clear directional edge. Wait for cross + fan formation or extreme RSI/Stoch.")
    
    # Default: just note the alignment
    else:
        # How many indicators agree on direction?
        bull_signals = 0
        bear_signals = 0
        if ema_dir == 'bullish':
            bull_signals += 1
        elif ema_dir == 'bearish':
            bear_signals += 1
        if rsi_zone in ('oversold', 'approaching_oversold'):
            bull_signals += 1
        elif rsi_zone in ('overbought', 'approaching_overbought'):
            bear_signals += 1
        if bb_pos in ('below_lower', 'lower_half'):
            bull_signals += 1
        elif bb_pos in ('above_upper', 'upper_half'):
            bear_signals += 1
        
        if bull_signals >= 2:
            parts.append(f"Leaning bullish ({bull_signals} indicators aligned).")
        elif bear_signals >= 2:
            parts.append(f"Leaning bearish ({bear_signals} indicators aligned).")
        else:
            parts.append("Mixed signals — no strong confluence yet.")
    
    return " ".join(parts)


def format_chart_signals(candles: List[Dict]) -> List[Dict]:
    """
    Format EMA signals for frontend chart markers.
    
    Returns:
        List of {time, type, direction, label} for chart display
    """
    if len(candles) < 100:
        return []
    
    markers = []
    
    # Add crossover markers
    crossovers = detect_ema_crossovers(candles)
    for cross in crossovers:
        markers.append({
            'time': cross['timestamp'],
            'type': 'crossover',
            'direction': cross['direction'],
            'label': f"EMA Cross {cross['direction'].title()}"
        })
    
    # Compute EMAs and separations (used by multiple marker types below)
    closes = [float(c['close']) for c in candles]
    ema21 = calculate_ema(closes, 21)
    ema55 = calculate_ema(closes, 55)
    separations = measure_separation(ema21, ema55, closes)
    
    # ===================================================================
    # CANDLE PATTERN + EMA 100 ENTRY SYSTEM
    #
    # The entry is a reversal candle pattern AT the EMA 100 line.
    # When price touches E100 and shows a rejection pattern, that's
    # the entry — BEFORE the cross, BEFORE the fan opens.
    #
    # The sequence a trader sees:
    # 1. Fan closes → EMAs converge → price drifts toward E100
    # 2. Candle touches E100 + shows reversal pattern → ENTRY
    # 3. Cross happens, fan opens → confirmation
    # 4. Ride the separation → exit at peak/deceleration
    #
    # Grade A: Strong reversal pattern + E100 touch + fan context supports
    # Grade B: Weaker pattern or less ideal context
    # ===================================================================
    ema100 = calculate_ema(closes, 100)
    opens = [float(c['open']) for c in candles]
    highs = [float(c['high']) for c in candles]
    lows = [float(c['low']) for c in candles]
    
    # Suppress entries within N bars of a previous entry to avoid clusters
    last_entry_bar = -20
    
    for i in range(100, len(candles)):
        if is_nan(ema100[i]):
            continue
        
        # Skip if too close to last entry
        if i - last_entry_bar < 10:
            continue
        
        # Is price near EMA 100? Check body AND wicks
        body_dist = abs(closes[i] - ema100[i]) / closes[i] * 100
        wick_dist = min(abs(highs[i] - ema100[i]), abs(lows[i] - ema100[i])) / closes[i] * 100
        near_e100 = body_dist < 0.08 or wick_dist < 0.05
        
        if not near_e100:
            continue
        
        # Detect candle patterns at this bar
        body = abs(closes[i] - opens[i])
        upper_wick = highs[i] - max(opens[i], closes[i])
        lower_wick = min(opens[i], closes[i]) - lows[i]
        candle_range = highs[i] - lows[i]
        bullish_candle = closes[i] > opens[i]
        
        if candle_range == 0:
            continue
        
        # Pattern detection
        patterns = []
        
        # Hammer (long lower wick, small body at top) — bullish reversal
        if lower_wick > body * 2 and lower_wick > candle_range * 0.55:
            patterns.append(('hammer', 'buy'))
        
        # Shooting star (long upper wick, small body at bottom) — bearish reversal
        if upper_wick > body * 2 and upper_wick > candle_range * 0.55:
            patterns.append(('shooting_star', 'sell'))
        
        # Engulfing patterns (need previous bar)
        if i > 0:
            prev_body = abs(closes[i-1] - opens[i-1])
            if bullish_candle and closes[i-1] < opens[i-1] and body > prev_body * 1.1:
                if opens[i] <= closes[i-1] and closes[i] >= opens[i-1]:
                    patterns.append(('bullish_engulfing', 'buy'))
            if not bullish_candle and closes[i-1] > opens[i-1] and body > prev_body * 1.1:
                if opens[i] >= closes[i-1] and closes[i] <= opens[i-1]:
                    patterns.append(('bearish_engulfing', 'sell'))
        
        # Rejection wicks — price touched E100 and got pushed away
        if closes[i] > ema100[i] and lower_wick > candle_range * 0.5 and lows[i] <= ema100[i] * 1.0005:
            patterns.append(('rejection_bounce_up', 'buy'))
        if closes[i] < ema100[i] and upper_wick > candle_range * 0.5 and highs[i] >= ema100[i] * 0.9995:
            patterns.append(('rejection_bounce_down', 'sell'))
        
        if not patterns:
            continue
        
        # Determine E100 role and fan context
        e100_is_support = closes[i] >= ema100[i]
        fan_bullish = ema21[i] > ema55[i] > ema100[i]
        fan_bearish = ema100[i] > ema55[i] > ema21[i]
        fan_converging = not fan_bullish and not fan_bearish
        
        # Score each pattern based on context alignment
        for pattern_name, pattern_dir in patterns:
            score = 0
            
            # Pattern direction matches E100 role?
            # Buy pattern + E100 as support = strong (bouncing up off support)
            # Sell pattern + E100 as resistance = strong (bouncing down off resistance)
            if pattern_dir == 'buy' and e100_is_support:
                score += 3
            elif pattern_dir == 'sell' and not e100_is_support:
                score += 3
            elif pattern_dir == 'buy' and not e100_is_support:
                score += 1  # Counter — buying at resistance (breakout attempt)
            elif pattern_dir == 'sell' and e100_is_support:
                score += 1  # Counter — selling at support (breakdown attempt)
            
            # Fan context — if fan is ordered in the pattern direction, bonus
            if pattern_dir == 'buy' and fan_bullish:
                score += 2  # With-trend bounce
            elif pattern_dir == 'sell' and fan_bearish:
                score += 2  # With-trend bounce
            elif fan_converging:
                score += 1  # Converging = could go either way, still valid
            
            # How close to E100? Closer = better
            if wick_dist < 0.02:
                score += 2  # Wick practically touched E100
            elif body_dist < 0.04:
                score += 1
            
            # Strong pattern types get bonus
            if pattern_name in ('bullish_engulfing', 'bearish_engulfing'):
                score += 1
            if pattern_name in ('rejection_bounce_up', 'rejection_bounce_down'):
                score += 1
            
            # Grade: A >= 6, B >= 4
            if score >= 6:
                grade = 'A'
            elif score >= 4:
                grade = 'B'
            else:
                continue  # Too weak, skip
            
            pattern_short = pattern_name.replace('_', ' ').replace('rejection bounce up', 'bounce↑').replace('rejection bounce down', 'bounce↓').replace('bullish engulfing', 'engulf↑').replace('bearish engulfing', 'engulf↓')
            
            markers.append({
                'time': candles[i]['time'],
                'type': 'entry',
                'direction': pattern_dir,
                'label': f"▶ {grade} {pattern_short}"
            })
            last_entry_bar = i
            break  # One entry per bar max
    
    # ===================================================================
    # EMA 100 LIVE TESTING (scout alert — setup forming NOW)
    # When price is currently sitting on EMA100 in the last few bars
    # ===================================================================
    recent_touches = 0
    touch_dir = None
    for i in range(max(100, len(candles) - 5), len(candles)):
        if i < len(ema100) and not is_nan(ema100[i]):
            dist = abs(closes[i] - ema100[i]) / closes[i] * 100
            if dist < 0.05:
                recent_touches += 1
                touch_dir = 'support' if closes[i] >= ema100[i] else 'resistance'
    
    if recent_touches >= 2 and touch_dir:
        entry_dir = 'buy' if touch_dir == 'support' else 'sell'
        markers.append({
            'time': candles[-1]['time'],
            'type': 'ema100_test',
            'direction': entry_dir,
            'label': f"🛡 E100 {touch_dir.title()} NOW ({recent_touches}x)"
        })
    
    # ===================================================================
    # DIRECTIONAL EXITS
    #
    # Exits are bidirectional — they tell you WHAT to close:
    # - "Close Long" = if you're in a buy, get out (fan closing from bull side)
    # - "Close Short" = if you're in a sell, get out (fan closing from bear side)
    #
    # Exit signals:
    # 1. Deceleration — separation growth slowing (early warning)
    # 2. Peak separation — fan at max width (get out NOW)
    # 3. Price returns to E100 — the move is over, back to the pivot
    # 4. Reverse candle pattern at E100 — the opposite entry is forming
    # ===================================================================
    
    # Deceleration exits — directional based on which side of E100
    decel_points = detect_deceleration(separations)
    for d_idx in decel_points:
        if d_idx < len(candles) and not is_nan(separations[d_idx]) and not is_nan(ema100[d_idx]):
            if ema21[d_idx] > ema55[d_idx]:
                exit_dir = 'sell'  # Bull fan decelerating → close your longs
                label = "⚡ Close Long"
            else:
                exit_dir = 'buy'  # Bear fan decelerating → close your shorts
                label = "⚡ Close Short"
            markers.append({
                'time': candles[d_idx]['time'],
                'type': 'decel',
                'direction': exit_dir,
                'label': label
            })
    
    # Peak separation exits — shifted 3 candles early, directional
    peaks = detect_peak_separation(separations)
    for peak_idx in peaks:
        early_idx = max(peak_idx - 3, 0)
        if early_idx < len(candles) and not is_nan(separations[peak_idx]) and not is_nan(ema100[early_idx]):
            if ema21[early_idx] > ema55[early_idx]:
                exit_dir = 'sell'  # Bull peak → close longs
                label = "⚠ Exit Long"
            else:
                exit_dir = 'buy'  # Bear peak → close shorts
                label = "⚠ Exit Short"
            markers.append({
                'time': candles[early_idx]['time'],
                'type': 'peak_sep',
                'direction': exit_dir,
                'label': label
            })
    
    # Price-returns-to-E100 exits — when price was away and comes back
    was_away = False
    away_dir = None  # 'above' or 'below'
    for i in range(101, len(candles)):
        if is_nan(ema100[i]) or is_nan(ema100[i-1]):
            continue
        dist = (closes[i] - ema100[i]) / closes[i] * 100
        prev_dist = (closes[i-1] - ema100[i-1]) / closes[i-1] * 100
        
        if not was_away:
            if abs(dist) > 0.12:
                was_away = True
                away_dir = 'above' if dist > 0 else 'below'
        else:
            # Check if price returned to E100
            if abs(dist) < 0.04:
                if away_dir == 'above':
                    markers.append({
                        'time': candles[i]['time'],
                        'type': 'return_exit',
                        'direction': 'sell',  # Was above → close long
                        'label': "◼ Back to E100 (close long)"
                    })
                else:
                    markers.append({
                        'time': candles[i]['time'],
                        'type': 'return_exit',
                        'direction': 'buy',  # Was below → close short
                        'label': "◼ Back to E100 (close short)"
                    })
                was_away = False
                away_dir = None
    
    return markers


if __name__ == "__main__":
    # Simple test with sample data
    print("EMA Separation Module - Test Mode")
    
    # Generate sample candle data  
    sample_candles = []
    base_price = 1.1000
    for i in range(200):
        # Simple trending price with noise
        trend = i * 0.00005 if i < 100 else (200 - i) * 0.00005
        noise = (i % 17 - 8) * 0.00001  # Deterministic "noise"
        price = base_price + trend + noise
        
        sample_candles.append({
            'time': f"2024-01-01T{i:02d}:00:00Z",
            'open': price,
            'high': price + abs(noise) * 0.5,
            'low': price - abs(noise) * 0.5,
            'close': price
        })
    
    # Test the scan
    result = scan_ema_signals(sample_candles)
    print(f"Signal: {result['signal']}")
    print(f"Phase: {result['phase']}")
    print(f"Separation: {result['separation_pct']}%")
    print(f"EMA100 Role: {result['ema100_role']}")
    print(f"Strength: {result['strength']}")
    print(f"Entry Suggested: {result['entry_suggested']}")
    print(f"Exit Suggested: {result['exit_suggested']}")
    print(f"Crossovers Detected: {len(result['crossovers'])}")