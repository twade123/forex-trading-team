#!/usr/bin/env python3
"""Chart pattern detection for trading bot.

Detects major chart patterns from OHLC data using swing point analysis.
Returns pattern names, directions, confidence levels, and key price levels.
"""

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema
from typing import List, Dict, Tuple, Optional


def find_swing_points(data: np.ndarray, order: int = 3) -> Tuple[np.ndarray, np.ndarray]:
    """Find local minima and maxima using scipy.signal.argrelextrema."""
    highs = argrelextrema(data, np.greater, order=order)[0]
    lows = argrelextrema(data, np.less, order=order)[0]
    return highs, lows


def calculate_trendline(x_points: np.ndarray, y_points: np.ndarray) -> Tuple[float, float]:
    """Calculate trendline slope and intercept using linear regression."""
    if len(x_points) < 2:
        return 0.0, 0.0
    
    # Use numpy polyfit for linear regression
    slope, intercept = np.polyfit(x_points, y_points, 1)
    return slope, intercept


def distance_to_line(x: float, y: float, slope: float, intercept: float) -> float:
    """Calculate perpendicular distance from point to line."""
    # Line equation: y = mx + b -> mx - y + b = 0
    # Distance = |mx - y + b| / sqrt(m² + 1)
    return abs(slope * x - y + intercept) / np.sqrt(slope ** 2 + 1)


def lines_intersect_x(slope1: float, intercept1: float, slope2: float, intercept2: float) -> float:
    """Find x-coordinate where two lines intersect."""
    if abs(slope1 - slope2) < 1e-10:  # Parallel lines
        return np.inf
    return (intercept2 - intercept1) / (slope1 - slope2)


def _score_double_pattern(price1: float, price2: float, pattern_height: float,
                          std: float, idx_distance: int, lookback: int,
                          tolerance_pct: float = 0.003) -> int:
    """Continuous 0-100 quality score for Double Top / Double Bottom.

    Replaces the legacy step-jump formula (70 base + 15 + 10 capped at 95) which
    only had 4 reachable values. This composes three normalized factors:

      A. peak_similarity (0-1): how closely the two peaks/troughs match in price.
         Maps the relative gap [0%..tolerance_pct] to [1..0]. Identical peaks = 1.
      B. height_significance (0-1): pattern height as multiple of recent std.
         Scales [0..3*std] to [0..1]. Bigger relative height = stronger pattern.
      C. spacing_quality (0-1): bars between peaks as fraction of lookback.
         Patterns too close together are weaker; ones spanning ~half the window
         are ideal. Caps at 1.0.

    Weights: peak_similarity 0.45, height 0.35, spacing 0.20.
    """
    if not (price1 and price2 and max(price1, price2) > 0):
        return 0
    peak_gap_pct = abs(price1 - price2) / max(price1, price2)
    peak_similarity = max(0.0, 1.0 - (peak_gap_pct / max(tolerance_pct, 1e-9)))
    height_significance = min((pattern_height / max(std * 3, 1e-9)), 1.0) if std > 0 else 0.0
    spacing_quality = min((idx_distance / max(lookback, 1)), 1.0)
    score = (0.45 * peak_similarity) + (0.35 * height_significance) + (0.20 * spacing_quality)
    return int(round(max(0.0, min(score, 1.0)) * 100))


def _score_hs_pattern(left_shoulder: float, head: float, right_shoulder: float,
                     pattern_height: float, std: float,
                     left_dist: int, right_dist: int, lookback: int) -> int:
    """Continuous 0-100 quality score for Head and Shoulders.

      A. shoulder_symmetry (0-1): how similar the two shoulders are. The closer
         shoulder heights match, the cleaner the pattern. Caps mismatches >5%.
      B. head_dominance (0-1): how much higher the head is vs the average shoulder.
         Strong dominance = clearer pattern. Scales [0..30%] to [0..1].
      C. height_significance (0-1): pattern height vs recent std. Same as double.
      D. spacing_balance (0-1): the two shoulder-to-head distances balanced.
         A symmetric H&S has equal spacing; very asymmetric is weaker.

    Weights: symmetry 0.30, dominance 0.25, height 0.25, spacing_balance 0.20.
    """
    avg_shoulder = (left_shoulder + right_shoulder) / 2.0 if (left_shoulder and right_shoulder) else 0.0
    if avg_shoulder <= 0 or head <= 0:
        return 0
    shoulder_gap_pct = abs(left_shoulder - right_shoulder) / max(left_shoulder, right_shoulder)
    shoulder_symmetry = max(0.0, 1.0 - (shoulder_gap_pct / 0.05))  # 0% gap = 1.0; 5% gap = 0.0
    head_dominance = min((head - avg_shoulder) / max(avg_shoulder * 0.30, 1e-9), 1.0) if head > avg_shoulder else 0.0
    height_significance = min((pattern_height / max(std * 3, 1e-9)), 1.0) if std > 0 else 0.0
    # Spacing balance: 1.0 if left_dist == right_dist, falls off as ratio diverges
    if left_dist > 0 and right_dist > 0:
        spacing_balance = min(left_dist, right_dist) / max(left_dist, right_dist)
    else:
        spacing_balance = 0.0
    score = (0.30 * shoulder_symmetry) + (0.25 * head_dominance) + (0.25 * height_significance) + (0.20 * spacing_balance)
    return int(round(max(0.0, min(score, 1.0)) * 100))


def _score_triangle_pattern(flat_range_pct: float, slope: float, std: float,
                            num_touches: int, duration_bars: int, lookback: int) -> int:
    """Continuous 0-100 quality score for triangles.

      A. flatness (0-1): how flat the flat side is. Range_pct of 0 = perfect flat = 1.0;
         1% range = the threshold = 0.0.
      B. slope_strength (0-1): magnitude of the converging slope vs price std (per bar).
      C. touches_factor (0-1): more swing-point touches = stronger pattern. 4+ = 1.0.
      D. duration_factor (0-1): pattern duration as fraction of lookback. ~50% lookback = 1.0.

    Weights: flatness 0.35, slope_strength 0.25, touches 0.20, duration 0.20.
    """
    flatness = max(0.0, 1.0 - (flat_range_pct / 0.01))
    slope_strength = min(abs(slope) / max(std / max(lookback, 1), 1e-9), 1.0) if std > 0 else 0.0
    touches_factor = min(num_touches / 4.0, 1.0)
    duration_factor = min(duration_bars / max(lookback * 0.5, 1), 1.0)
    score = (0.35 * flatness) + (0.25 * slope_strength) + (0.20 * touches_factor) + (0.20 * duration_factor)
    return int(round(max(0.0, min(score, 1.0)) * 100))


def _score_symmetrical_triangle(upper_slope: float, lower_slope: float, std: float,
                                num_swing_points: int, duration_bars: int, lookback: int) -> int:
    """Continuous 0-100 score for symmetrical triangle. Quality factors:

      A. convergence (0-1): both slopes pointing toward apex with similar magnitude.
         Perfect symmetry = 1.0.
      B. slope_strength (0-1): combined slope magnitude.
      C. swing_density (0-1): more swing points = stronger pattern.
      D. duration (0-1): similar to other triangles.
    """
    if upper_slope >= 0 or lower_slope <= 0:
        return 0  # not actually symmetrical
    abs_upper = abs(upper_slope)
    abs_lower = abs(lower_slope)
    convergence = min(abs_upper, abs_lower) / max(abs_upper, abs_lower) if max(abs_upper, abs_lower) > 0 else 0.0
    avg_slope = (abs_upper + abs_lower) / 2.0
    slope_strength = min(avg_slope / max(std / max(lookback, 1), 1e-9), 1.0) if std > 0 else 0.0
    swing_density = min(num_swing_points / 6.0, 1.0)
    duration_factor = min(duration_bars / max(lookback * 0.5, 1), 1.0)
    score = (0.35 * convergence) + (0.25 * slope_strength) + (0.20 * swing_density) + (0.20 * duration_factor)
    return int(round(max(0.0, min(score, 1.0)) * 100))


def _score_flag_pattern(pole_height: float, flag_range: float, flag_slope: float,
                        std: float, flag_duration: int, direction: str) -> int:
    """Continuous 0-100 score for bull/bear flag.

      A. pole_dominance (0-1): pole_height vs flag_range. Bigger pole vs flag = stronger.
         Aim: pole >= 4× flag range = 1.0.
      B. height_significance (0-1): pole_height vs recent std. Bigger move = more meaningful.
      C. flag_slope_correctness (0-1): bull flag should have slight DOWNWARD slope (counter-trend
         pullback); bear flag slight UPWARD. Pure flat = 0.5; correct mild = 1.0; wrong direction = 0.
      D. duration_quality (0-1): 10-12 bars is sweet spot; very short or very long penalized.
    """
    if pole_height <= 0 or flag_range <= 0:
        return 0
    pole_dominance = min((pole_height / flag_range) / 4.0, 1.0)
    height_significance = min(pole_height / max(std * 5, 1e-9), 1.0) if std > 0 else 0.0
    if direction == 'bullish':
        # Want slope <= 0 (down or flat); penalize positive
        flag_slope_correctness = 1.0 if flag_slope < -0.0001 else (0.5 if flag_slope <= 0 else 0.0)
    else:
        flag_slope_correctness = 1.0 if flag_slope > 0.0001 else (0.5 if flag_slope >= 0 else 0.0)
    # Sweet spot 10-12 bars; penalize <=8 (min) or >14
    if 10 <= flag_duration <= 12:
        duration_quality = 1.0
    elif 8 <= flag_duration <= 14:
        duration_quality = 0.7
    else:
        duration_quality = 0.4
    score = (0.30 * pole_dominance) + (0.25 * height_significance) + (0.25 * flag_slope_correctness) + (0.20 * duration_quality)
    return int(round(max(0.0, min(score, 1.0)) * 100))


def _score_cup_and_handle(left_high: float, right_high: float, cup_bottom: float,
                          cup_depth_pct: float, handle_depth_pct: float = 0.0,
                          duration_bars: int = 60, lookback: int = 60) -> int:
    """Continuous 0-100 score for cup and handle.

      A. rim_symmetry (0-1): how similar left and right rims are. Perfect = 1.0; 3% diff = 0.
      B. cup_depth (0-1): depth as fraction of left_high. Sweet spot 12-30%.
      C. handle_quality (0-1): handle pullback depth. 5-15% of cup depth = ideal; deeper = bad.
      D. duration_factor (0-1): pattern duration.
    """
    if left_high <= 0 or right_high <= 0 or cup_bottom <= 0:
        return 0
    rim_gap = abs(left_high - right_high) / max(left_high, right_high)
    rim_symmetry = max(0.0, 1.0 - (rim_gap / 0.03))
    if cup_depth_pct < 0.12:
        cup_depth_score = cup_depth_pct / 0.12  # ramp up to 12%
    elif cup_depth_pct <= 0.30:
        cup_depth_score = 1.0
    else:
        cup_depth_score = max(0.0, 1.0 - (cup_depth_pct - 0.30) / 0.20)  # falloff past 30%
    if handle_depth_pct == 0.0:
        handle_quality = 0.5  # unknown handle
    elif 0.05 <= handle_depth_pct <= 0.15:
        handle_quality = 1.0
    elif handle_depth_pct <= 0.25:
        handle_quality = 0.7
    else:
        handle_quality = 0.3  # too-deep handle is bad
    duration_factor = min(duration_bars / max(lookback, 1), 1.0)
    score = (0.30 * rim_symmetry) + (0.30 * cup_depth_score) + (0.25 * handle_quality) + (0.15 * duration_factor)
    return int(round(max(0.0, min(score, 1.0)) * 100))


def _dedupe_and_consolidate(patterns: List[Dict]) -> List[Dict]:
    """Post-process double top/bottom patterns:
      A. Dedupe overlapping same-direction instances — when multiple swing-point pairs
         describe the same setup (end_idx within 10 bars of each other), keep only the
         highest-confidence instance.
      B. Range detection — if BOTH Double Top and Double Bottom fired in the same recent
         window (within 30 bars of each other), the chart is range-bound, not reversal-
         pending. Replace the contradictory pair with a single 'Range Bound' signal.
    Other pattern types (head & shoulders, triangles, etc.) pass through untouched.
    """
    if not patterns:
        return patterns

    tops = [p for p in patterns if p.get('pattern') == 'Double Top']
    bottoms = [p for p in patterns if p.get('pattern') == 'Double Bottom']
    others = [p for p in patterns if p.get('pattern') not in ('Double Top', 'Double Bottom')]

    def _dedupe(group: List[Dict]) -> List[Dict]:
        if not group:
            return group
        # Sort by confidence desc so first-kept is best in any cluster
        group_sorted = sorted(group, key=lambda p: -p.get('confidence', 0))
        kept: List[Dict] = []
        for p in group_sorted:
            p_end = p.get('end_idx', 0)
            # Keep if no already-kept instance is within 10 bars of this end_idx
            if not any(abs(k.get('end_idx', 0) - p_end) < 10 for k in kept):
                kept.append(p)
        return kept

    tops = _dedupe(tops)
    bottoms = _dedupe(bottoms)

    # Range detection — if both directions present and their most-recent instances
    # are within 30 bars of each other, emit single Range Bound signal.
    if tops and bottoms:
        max_top_idx = max(t.get('end_idx', 0) for t in tops)
        max_bot_idx = max(b.get('end_idx', 0) for b in bottoms)
        if abs(max_top_idx - max_bot_idx) < 30:
            top_conf = max(t.get('confidence', 0) for t in tops)
            bot_conf = max(b.get('confidence', 0) for b in bottoms)
            resistance = max(t.get('key_levels', {}).get('peak1', 0) for t in tops)
            support = min(b.get('key_levels', {}).get('bottom1', 0) for b in bottoms)
            range_pattern = {
                'pattern': 'Range Bound',
                'direction': 'neutral',
                'confidence': round((top_conf + bot_conf) / 2),
                'start_idx': min(
                    min(t.get('start_idx', 0) for t in tops),
                    min(b.get('start_idx', 0) for b in bottoms),
                ),
                'end_idx': max(max_top_idx, max_bot_idx),
                'key_levels': {
                    'resistance': resistance,
                    'support': support,
                },
                'note': (
                    'Both double_top and double_bottom fired in the same window — '
                    'chart is range-bound between support and resistance, not reversal-pending'
                ),
            }
            return others + [range_pattern]

    return others + tops + bottoms


def detect_double_top_bottom(df: pd.DataFrame, lookback: int = 50) -> List[Dict]:
    """Detect double top and double bottom patterns.

    Post-processed by `_dedupe_and_consolidate` to (A) collapse overlapping same-
    direction instances and (B) replace contradictory top+bottom pairs with a
    single Range Bound signal when both fire in the same window.
    """
    patterns = []
    highs, lows = find_swing_points(df['high'].values), find_swing_points(df['low'].values)
    
    # Double Top Detection
    high_indices = highs[0]
    if len(high_indices) >= 2:
        for i in range(len(high_indices) - 1):
            for j in range(i + 1, len(high_indices)):
                idx1, idx2 = high_indices[i], high_indices[j]
                if idx2 - idx1 > lookback // 4:  # Minimum distance between peaks
                    
                    price1 = df.iloc[idx1]['high']
                    price2 = df.iloc[idx2]['high']
                    
                    # Check if prices are similar (within 0.3% tolerance)
                    if abs(price1 - price2) / max(price1, price2) < 0.003:
                        
                        # Find the trough between peaks
                        trough_start = max(0, idx1 - 5)
                        trough_end = min(len(df), idx2 + 5)
                        trough_idx = df.iloc[trough_start:trough_end]['low'].idxmin()
                        trough_price = df.iloc[trough_idx]['low']
                        
                        # Neckline (support level between the peaks)
                        neckline = trough_price
                        pattern_height = max(price1, price2) - neckline

                        # 2026-04-27: Continuous 0-100 quality score (was 70/85/95 step jumps).
                        confidence = _score_double_pattern(
                            price1, price2, pattern_height,
                            float(df['close'].iloc[-20:].std() or 0.0),
                            idx2 - idx1, lookback,
                        )

                        patterns.append({
                            'pattern': 'Double Top',
                            'direction': 'bearish',
                            'confidence': confidence,
                            'start_idx': idx1,
                            'end_idx': idx2,
                            'key_levels': {
                                'peak1': price1,
                                'peak2': price2,
                                'neckline': neckline,
                                'target': neckline - pattern_height
                            },
                            'pattern_height': pattern_height
                        })
    
    # Double Bottom Detection
    low_indices = lows[0]
    if len(low_indices) >= 2:
        for i in range(len(low_indices) - 1):
            for j in range(i + 1, len(low_indices)):
                idx1, idx2 = low_indices[i], low_indices[j]
                if idx2 - idx1 > lookback // 4:
                    
                    price1 = df.iloc[idx1]['low']
                    price2 = df.iloc[idx2]['low']
                    
                    if abs(price1 - price2) / max(price1, price2) < 0.003:
                        
                        # Find the peak between lows
                        peak_start = max(0, idx1 - 5)
                        peak_end = min(len(df), idx2 + 5)
                        peak_idx = df.iloc[peak_start:peak_end]['high'].idxmax()
                        peak_price = df.iloc[peak_idx]['high']
                        
                        neckline = peak_price
                        pattern_height = neckline - min(price1, price2)

                        # 2026-04-27: Continuous 0-100 quality score (was 70/85/95 step jumps).
                        confidence = _score_double_pattern(
                            price1, price2, pattern_height,
                            float(df['close'].iloc[-20:].std() or 0.0),
                            idx2 - idx1, lookback,
                        )

                        patterns.append({
                            'pattern': 'Double Bottom',
                            'direction': 'bullish',
                            'confidence': confidence,
                            'start_idx': idx1,
                            'end_idx': idx2,
                            'key_levels': {
                                'bottom1': price1,
                                'bottom2': price2,
                                'neckline': neckline,
                                'target': neckline + pattern_height
                            },
                            'pattern_height': pattern_height
                        })

    return _dedupe_and_consolidate(patterns)


def detect_head_and_shoulders(df: pd.DataFrame, lookback: int = 60) -> List[Dict]:
    """Detect head and shoulders patterns."""
    patterns = []
    highs, lows = find_swing_points(df['high'].values), find_swing_points(df['low'].values)
    
    # Head and Shoulders (bearish)
    high_indices = highs[0]
    if len(high_indices) >= 3:
        for i in range(len(high_indices) - 2):
            left_shoulder_idx = high_indices[i]
            head_idx = high_indices[i + 1] 
            right_shoulder_idx = high_indices[i + 2]
            
            if (head_idx - left_shoulder_idx > lookback // 6 and 
                right_shoulder_idx - head_idx > lookback // 6):
                
                left_shoulder = df.iloc[left_shoulder_idx]['high']
                head = df.iloc[head_idx]['high']
                right_shoulder = df.iloc[right_shoulder_idx]['high']
                
                # Head must be highest, shoulders similar height
                if (head > left_shoulder and head > right_shoulder and
                    abs(left_shoulder - right_shoulder) / max(left_shoulder, right_shoulder) < 0.05):
                    
                    # Find neckline (connect the lows between shoulders and head)
                    left_low_idx = df.iloc[left_shoulder_idx:head_idx]['low'].idxmin()
                    right_low_idx = df.iloc[head_idx:right_shoulder_idx]['low'].idxmin()
                    left_low = df.iloc[left_low_idx]['low']
                    right_low = df.iloc[right_low_idx]['low']
                    neckline = min(left_low, right_low)
                    
                    pattern_height = head - neckline
                    # 2026-04-27: Continuous 0-100 quality score (was 75/90/95 step jumps).
                    confidence = _score_hs_pattern(
                        left_shoulder, head, right_shoulder, pattern_height,
                        float(df['close'].iloc[-20:].std() or 0.0),
                        head_idx - left_shoulder_idx, right_shoulder_idx - head_idx, lookback,
                    )

                    patterns.append({
                        'pattern': 'Head and Shoulders',
                        'direction': 'bearish',
                        'confidence': confidence,
                        'start_idx': left_shoulder_idx,
                        'end_idx': right_shoulder_idx,
                        'key_levels': {
                            'left_shoulder': left_shoulder,
                            'head': head,
                            'right_shoulder': right_shoulder,
                            'neckline': neckline,
                            'target': neckline - pattern_height
                        },
                        'pattern_height': pattern_height
                    })
    
    # Inverse Head and Shoulders (bullish)
    low_indices = lows[0]
    if len(low_indices) >= 3:
        for i in range(len(low_indices) - 2):
            left_shoulder_idx = low_indices[i]
            head_idx = low_indices[i + 1]
            right_shoulder_idx = low_indices[i + 2]
            
            if (head_idx - left_shoulder_idx > lookback // 6 and
                right_shoulder_idx - head_idx > lookback // 6):
                
                left_shoulder = df.iloc[left_shoulder_idx]['low']
                head = df.iloc[head_idx]['low']
                right_shoulder = df.iloc[right_shoulder_idx]['low']
                
                if (head < left_shoulder and head < right_shoulder and
                    abs(left_shoulder - right_shoulder) / max(left_shoulder, right_shoulder) < 0.05):
                    
                    left_high_idx = df.iloc[left_shoulder_idx:head_idx]['high'].idxmax()
                    right_high_idx = df.iloc[head_idx:right_shoulder_idx]['high'].idxmax()
                    left_high = df.iloc[left_high_idx]['high']
                    right_high = df.iloc[right_high_idx]['high']
                    neckline = max(left_high, right_high)
                    
                    pattern_height = neckline - head
                    # 2026-04-27: Continuous 0-100 quality score (was 75/90/95 step jumps).
                    confidence = _score_hs_pattern(
                        left_shoulder, head, right_shoulder, pattern_height,
                        float(df['close'].iloc[-20:].std() or 0.0),
                        head_idx - left_shoulder_idx, right_shoulder_idx - head_idx, lookback,
                    )

                    patterns.append({
                        'pattern': 'Inverse Head and Shoulders',
                        'direction': 'bullish',
                        'confidence': confidence,
                        'start_idx': left_shoulder_idx,
                        'end_idx': right_shoulder_idx,
                        'key_levels': {
                            'left_shoulder': left_shoulder,
                            'head': head,
                            'right_shoulder': right_shoulder,
                            'neckline': neckline,
                            'target': neckline + pattern_height
                        },
                        'pattern_height': pattern_height
                    })
    
    return patterns


def detect_triangles(df: pd.DataFrame, lookback: int = 40) -> List[Dict]:
    """Detect triangle patterns (ascending, descending, symmetrical)."""
    patterns = []
    highs, lows = find_swing_points(df['high'].values), find_swing_points(df['low'].values)
    
    high_indices = highs[0]
    low_indices = lows[0]
    
    # Need at least 4 swing points total for triangle
    if len(high_indices) < 2 or len(low_indices) < 2:
        return patterns
    
    # Ascending Triangle: flat resistance + rising lows
    if len(high_indices) >= 2:
        recent_highs = high_indices[high_indices >= len(df) - lookback]
        if len(recent_highs) >= 2:
            # Check if highs are relatively flat (resistance level)
            high_prices = [df.iloc[idx]['high'] for idx in recent_highs[-3:]]
            if len(high_prices) >= 2:
                high_range = max(high_prices) - min(high_prices)
                avg_high = np.mean(high_prices)
                
                if high_range / avg_high < 0.01:  # Flat resistance within 1%
                    # Check if lows are rising
                    recent_lows = low_indices[low_indices >= len(df) - lookback]
                    if len(recent_lows) >= 2:
                        low_prices = [(idx, df.iloc[idx]['low']) for idx in recent_lows[-3:]]
                        if len(low_prices) >= 2:
                            # Calculate trendline slope for lows
                            x_coords = [p[0] for p in low_prices]
                            y_coords = [p[1] for p in low_prices]
                            slope, _ = calculate_trendline(np.array(x_coords), np.array(y_coords))
                            
                            if slope > 0:  # Rising lows
                                resistance_level = avg_high
                                pattern_height = resistance_level - min(y_coords)
                                # 2026-04-27: Continuous score (was flat 80).
                                _ascending_score = _score_triangle_pattern(
                                    high_range / max(avg_high, 1e-9), slope,
                                    float(df['close'].iloc[-20:].std() or 0.0),
                                    len(recent_lows[-3:]) + len(recent_highs[-3:]),
                                    len(df) - 1 - int(min(recent_lows[0], recent_highs[0])), lookback,
                                )
                                patterns.append({
                                    'pattern': 'Ascending Triangle',
                                    'direction': 'bullish',
                                    'confidence': _ascending_score,
                                    'start_idx': min(recent_lows[0], recent_highs[0]),
                                    'end_idx': len(df) - 1,
                                    'key_levels': {
                                        'resistance': resistance_level,
                                        'support_slope': slope,
                                        'target': resistance_level + pattern_height,
                                        'pattern_height': pattern_height
                                    }
                                })
    
    # Descending Triangle: flat support + falling highs  
    if len(low_indices) >= 2:
        recent_lows = low_indices[low_indices >= len(df) - lookback]
        if len(recent_lows) >= 2:
            low_prices = [df.iloc[idx]['low'] for idx in recent_lows[-3:]]
            if len(low_prices) >= 2:
                low_range = max(low_prices) - min(low_prices)
                avg_low = np.mean(low_prices)
                
                if low_range / avg_low < 0.01:  # Flat support
                    recent_highs = high_indices[high_indices >= len(df) - lookback]
                    if len(recent_highs) >= 2:
                        high_prices = [(idx, df.iloc[idx]['high']) for idx in recent_highs[-3:]]
                        if len(high_prices) >= 2:
                            x_coords = [p[0] for p in high_prices]
                            y_coords = [p[1] for p in high_prices]
                            slope, _ = calculate_trendline(np.array(x_coords), np.array(y_coords))
                            
                            if slope < 0:  # Falling highs
                                support_level = avg_low
                                pattern_height = max([p[1] for p in high_prices]) - support_level
                                # 2026-04-27: Continuous score (was flat 80).
                                _descending_score = _score_triangle_pattern(
                                    low_range / max(avg_low, 1e-9), slope,
                                    float(df['close'].iloc[-20:].std() or 0.0),
                                    len(recent_lows[-3:]) + len(recent_highs[-3:]),
                                    len(df) - 1 - int(min(recent_lows[0], recent_highs[0])), lookback,
                                )
                                patterns.append({
                                    'pattern': 'Descending Triangle',
                                    'direction': 'bearish',
                                    'confidence': _descending_score,
                                    'start_idx': min(recent_lows[0], recent_highs[0]),
                                    'end_idx': len(df) - 1,
                                    'key_levels': {
                                        'support': support_level,
                                        'resistance_slope': slope,
                                        'target': support_level - pattern_height,
                                        'pattern_height': pattern_height
                                    }
                                })
    
    # Symmetrical Triangle: converging trendlines
    if len(high_indices) >= 2 and len(low_indices) >= 2:
        recent_highs = high_indices[high_indices >= len(df) - lookback]
        recent_lows = low_indices[low_indices >= len(df) - lookback]
        
        if len(recent_highs) >= 2 and len(recent_lows) >= 2:
            high_coords = [(idx, df.iloc[idx]['high']) for idx in recent_highs[-3:]]
            low_coords = [(idx, df.iloc[idx]['low']) for idx in recent_lows[-3:]]
            
            if len(high_coords) >= 2 and len(low_coords) >= 2:
                # Calculate both trendlines
                high_x = [p[0] for p in high_coords]
                high_y = [p[1] for p in high_coords]
                low_x = [p[0] for p in low_coords] 
                low_y = [p[1] for p in low_coords]
                
                high_slope, high_intercept = calculate_trendline(np.array(high_x), np.array(high_y))
                low_slope, low_intercept = calculate_trendline(np.array(low_x), np.array(low_y))
                
                # Check if lines are converging
                if high_slope < 0 and low_slope > 0:  # High falling, low rising
                    apex_x = lines_intersect_x(high_slope, high_intercept, low_slope, low_intercept)
                    if apex_x > len(df) - 1 and apex_x < len(df) + 20:  # Reasonable apex
                        
                        # 2026-04-27: Continuous score (was flat 75).
                        _sym_score = _score_symmetrical_triangle(
                            high_slope, low_slope,
                            float(df['close'].iloc[-20:].std() or 0.0),
                            len(recent_highs[-3:]) + len(recent_lows[-3:]),
                            len(df) - 1 - int(min(recent_lows[0], recent_highs[0])), lookback,
                        )
                        patterns.append({
                            'pattern': 'Symmetrical Triangle',
                            'direction': 'neutral',
                            'confidence': _sym_score,
                            'start_idx': min(recent_lows[0], recent_highs[0]),
                            'end_idx': len(df) - 1,
                            'key_levels': {
                                'upper_trendline': (high_slope, high_intercept),
                                'lower_trendline': (low_slope, low_intercept),
                                'apex_x': apex_x
                            }
                        })
    
    return patterns


def detect_flags(df: pd.DataFrame, lookback: int = 30) -> List[Dict]:
    """Detect bull and bear flag patterns."""
    patterns = []
    
    # Look for sharp moves followed by consolidation channels
    price_changes = df['close'].pct_change(5)  # 5-period price change
    
    for i in range(lookback, len(df) - 10):
        # Check for sharp upward move (potential bull flag pole)
        if price_changes.iloc[i] > 0.02:  # 2% move in 5 periods
            pole_start = max(0, i - 10)
            pole_end = i
            pole_height = df['high'].iloc[pole_start:pole_end+1].max() - df['low'].iloc[pole_start:pole_end+1].min()
            
            # Look for consolidation after the pole (flag)
            flag_start = i + 1
            flag_end = min(len(df), i + 15)
            
            if flag_end - flag_start >= 8:  # Minimum flag length
                flag_data = df.iloc[flag_start:flag_end]
                flag_high = flag_data['high'].max()
                flag_low = flag_data['low'].min()
                flag_range = flag_high - flag_low
                
                # Flag should be smaller than pole and trend slightly down
                if flag_range < pole_height * 0.5:
                    # Check if flag trends slightly downward (consolidation)
                    flag_slope = calculate_trendline(
                        np.arange(len(flag_data)), 
                        flag_data['close'].values
                    )[0]
                    
                    if flag_slope <= 0:  # Slight downward or sideways
                        # 2026-04-27: Continuous score (was flat 70).
                        _bull_flag_score = _score_flag_pattern(
                            pole_height, flag_range, flag_slope,
                            float(df['close'].iloc[-20:].std() or 0.0),
                            flag_end - flag_start, 'bullish',
                        )
                        patterns.append({
                            'pattern': 'Bull Flag',
                            'direction': 'bullish',
                            'confidence': _bull_flag_score,
                            'start_idx': pole_start,
                            'end_idx': flag_end - 1,
                            'key_levels': {
                                'pole_height': pole_height,
                                'flag_high': flag_high,
                                'flag_low': flag_low,
                                'target': flag_high + pole_height
                            }
                        })
        
        # Check for sharp downward move (potential bear flag pole)
        elif price_changes.iloc[i] < -0.02:  # -2% move
            pole_start = max(0, i - 10)
            pole_end = i
            pole_height = df['high'].iloc[pole_start:pole_end+1].max() - df['low'].iloc[pole_start:pole_end+1].min()
            
            flag_start = i + 1
            flag_end = min(len(df), i + 15)
            
            if flag_end - flag_start >= 8:
                flag_data = df.iloc[flag_start:flag_end]
                flag_high = flag_data['high'].max()
                flag_low = flag_data['low'].min()
                flag_range = flag_high - flag_low
                
                if flag_range < pole_height * 0.5:
                    flag_slope = calculate_trendline(
                        np.arange(len(flag_data)),
                        flag_data['close'].values
                    )[0]
                    
                    if flag_slope >= 0:  # Slight upward or sideways
                        # 2026-04-27: Continuous score (was flat 70).
                        _bear_flag_score = _score_flag_pattern(
                            pole_height, flag_range, flag_slope,
                            float(df['close'].iloc[-20:].std() or 0.0),
                            flag_end - flag_start, 'bearish',
                        )
                        patterns.append({
                            'pattern': 'Bear Flag',
                            'direction': 'bearish',
                            'confidence': _bear_flag_score,
                            'start_idx': pole_start,
                            'end_idx': flag_end - 1,
                            'key_levels': {
                                'pole_height': pole_height,
                                'flag_high': flag_high,
                                'flag_low': flag_low,
                                'target': flag_low - pole_height
                            }
                        })
    
    return patterns


def detect_cup_and_handle(df: pd.DataFrame, lookback: int = 60) -> List[Dict]:
    """Detect cup and handle patterns."""
    patterns = []
    
    if len(df) < lookback:
        return patterns
    
    # Look for U-shaped recovery (cup) followed by small pullback (handle)
    for i in range(lookback, len(df) - 15):
        cup_start = i - lookback
        cup_end = i
        
        # Find the left rim, bottom, and right rim of potential cup
        left_section = df.iloc[cup_start:cup_start + lookback//3]
        middle_section = df.iloc[cup_start + lookback//3:cup_start + 2*lookback//3]
        right_section = df.iloc[cup_start + 2*lookback//3:cup_end]
        
        left_high = left_section['high'].max()
        cup_bottom = middle_section['low'].min()
        right_high = right_section['high'].max()
        
        # Cup criteria: similar rim heights, significant depth
        if (abs(left_high - right_high) / max(left_high, right_high) < 0.03 and
            (left_high - cup_bottom) / left_high > 0.12):  # At least 12% depth
            
            # Look for handle formation
            handle_start = cup_end
            handle_end = min(len(df), cup_end + 15)
            
            if handle_end - handle_start >= 5:
                handle_data = df.iloc[handle_start:handle_end]
                handle_low = handle_data['low'].min()
                handle_high = handle_data['high'].max()
                
                # Handle should be smaller pullback from right rim
                pullback_depth = (right_high - handle_low) / right_high
                if 0.05 < pullback_depth < 0.25:  # 5-25% pullback for handle
                    
                    cup_depth = left_high - cup_bottom
                    # 2026-04-27: Continuous score (was flat 75).
                    _cup_score = _score_cup_and_handle(
                        left_high, right_high, cup_bottom,
                        cup_depth / max(left_high, 1e-9),
                        pullback_depth, handle_end - cup_start, lookback,
                    )
                    patterns.append({
                        'pattern': 'Cup and Handle',
                        'direction': 'bullish',
                        'confidence': _cup_score,
                        'start_idx': cup_start,
                        'end_idx': handle_end - 1,
                        'key_levels': {
                            'left_rim': left_high,
                            'right_rim': right_high,
                            'cup_bottom': cup_bottom,
                            'handle_low': handle_low,
                            'target': right_high + cup_depth,
                            'cup_depth': cup_depth
                        }
                    })
    
    return patterns


def calculate_fibonacci_levels(swing_high: float, swing_low: float) -> Dict[str, float]:
    """Calculate Fibonacci retracement levels."""
    diff = swing_high - swing_low
    
    return {
        '0.0%': swing_high,
        '23.6%': swing_high - (diff * 0.236),
        '38.2%': swing_high - (diff * 0.382), 
        '50.0%': swing_high - (diff * 0.500),
        '61.8%': swing_high - (diff * 0.618),
        '78.6%': swing_high - (diff * 0.786),
        '100.0%': swing_low
    }


def find_fibonacci_reactions(df: pd.DataFrame, lookback: int = 100) -> List[Dict]:
    """Find price reactions at Fibonacci levels."""
    if len(df) < lookback:
        return []
    
    reactions = []
    
    # Find significant swings in the lookback period
    highs, lows = find_swing_points(df['high'].values[-lookback:]), find_swing_points(df['low'].values[-lookback:])
    high_indices = highs[0] + (len(df) - lookback)
    low_indices = lows[0] + (len(df) - lookback)
    
    # For each significant swing, check if current price is near a fib level
    current_price = df['close'].iloc[-1]
    
    for high_idx in high_indices[-5:]:  # Check last 5 significant highs
        for low_idx in low_indices[-5:]:   # Check last 5 significant lows
            if abs(high_idx - low_idx) > 10:  # Must be separated
                swing_high = df.iloc[high_idx]['high']
                swing_low = df.iloc[low_idx]['low']
                
                fib_levels = calculate_fibonacci_levels(swing_high, swing_low)
                
                # Check if current price is near any fib level (within 0.1%)
                for level_name, level_price in fib_levels.items():
                    distance_pct = abs(current_price - level_price) / current_price
                    if distance_pct < 0.001:  # Within 0.1%
                        reactions.append({
                            'fib_level': level_name,
                            'fib_price': level_price,
                            'current_price': current_price,
                            'swing_high': swing_high,
                            'swing_low': swing_low,
                            'distance_pct': distance_pct,
                            'is_reacting': True
                        })
    
    return reactions


def detect_all_chart_patterns(df: pd.DataFrame, lookback: int = 100) -> List[Dict]:
    """Detect all chart patterns and return consolidated results."""
    if len(df) < lookback:
        return []
    
    all_patterns = []
    
    # Detect each pattern type
    all_patterns.extend(detect_double_top_bottom(df, lookback))
    all_patterns.extend(detect_head_and_shoulders(df, lookback))
    all_patterns.extend(detect_triangles(df, lookback))
    all_patterns.extend(detect_flags(df, lookback))
    all_patterns.extend(detect_cup_and_handle(df, lookback))
    
    # Add fibonacci analysis
    fib_reactions = find_fibonacci_reactions(df, lookback)
    for reaction in fib_reactions:
        all_patterns.append({
            'pattern': f'Fibonacci {reaction["fib_level"]} Reaction',
            'direction': 'neutral',
            'confidence': 60,
            'start_idx': len(df) - lookback,
            'end_idx': len(df) - 1,
            'key_levels': {
                'fib_level': reaction['fib_level'],
                'fib_price': reaction['fib_price'],
                'swing_high': reaction['swing_high'],
                'swing_low': reaction['swing_low']
            }
        })
    
    # Sort by confidence and recency (end_idx)
    all_patterns.sort(key=lambda x: (x['confidence'], x['end_idx']), reverse=True)
    
    # Remove overlapping patterns (keep highest confidence)
    filtered_patterns = []
    for pattern in all_patterns:
        overlap = False
        for existing in filtered_patterns:
            # Check if patterns overlap significantly
            if (abs(pattern['start_idx'] - existing['start_idx']) < 10 and
                abs(pattern['end_idx'] - existing['end_idx']) < 10):
                overlap = True
                break
        
        if not overlap:
            filtered_patterns.append(pattern)
    
    return filtered_patterns[:5]  # Return top 5 patterns


if __name__ == "__main__":
    # Test with sample data
    np.random.seed(42)
    dates = pd.date_range('2023-01-01', periods=200, freq='H')
    
    # Generate sample OHLC data with some pattern-like behavior
    close_prices = 100 + np.cumsum(np.random.randn(200) * 0.5)
    
    sample_data = pd.DataFrame({
        'datetime': dates,
        'open': close_prices + np.random.randn(200) * 0.1,
        'high': close_prices + abs(np.random.randn(200)) * 0.3,
        'low': close_prices - abs(np.random.randn(200)) * 0.3,
        'close': close_prices
    })
    
    patterns = detect_all_chart_patterns(sample_data)
    print(f"Detected {len(patterns)} patterns:")
    for pattern in patterns:
        print(f"- {pattern['pattern']}: {pattern['direction']} (confidence: {pattern['confidence']}%)")