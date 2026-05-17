#!/usr/bin/env python3
"""SNIPER V4 — Enhanced scoring using ALL research findings.

Adds what was missing:
1. Fibonacci level proximity (pattern at fib level = high confluence)
2. Stochastic %K/%D crossover (not just extremes)
3. MACD histogram zero-line cross event
4. Parabolic SAR direction
5. CCI extremes
6. Regime-specific scoring (trending vs ranging via ADX)
7. EMA crossover events
8. Pivot point proximity
9. EMA Fan/Separation detection (up to 15 points)
"""

from typing import Dict, List, Any, Optional

# Handle pandas and numpy imports gracefully
try:
    import pandas as pd
    import numpy as np
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    # Minimal fallback for environments without pandas/numpy
    class pd:
        @staticmethod
        def isna(x):
            return x is None or (isinstance(x, float) and x != x)
    
    class np:
        @staticmethod
        def isnan(x):
            if x is None:
                return True
            try:
                return x != x
            except Exception:
                return True
        
        nan = float('nan')

# Timeframe-specific parameters
TF_PARAMS = {
    "M5": {
        "rsi_ob": [30, 35, 40], "rsi_os": [60, 65, 70],
        "stoch_ob": [20, 25], "stoch_os": [75, 80],
        "bb_pen": [0.3, 0.0], "consec": [7, 5, 4],
        "candle_w": 1.5, "h4_w": 1.5, "rsi_slope": 2,
    },
    "M10": {
        "rsi_ob": [28, 33, 38], "rsi_os": [62, 67, 72],
        "stoch_ob": [18, 23], "stoch_os": [77, 82],
        "bb_pen": [0.35, 0.0], "consec": [6, 5, 4],
        "candle_w": 1.4, "h4_w": 1.4, "rsi_slope": 2.5,
    },
    "M15": {
        "rsi_ob": [28, 32, 37], "rsi_os": [63, 68, 72],
        "stoch_ob": [18, 22], "stoch_os": [78, 82],
        "bb_pen": [0.4, 0.0], "consec": [6, 4, 3],
        "candle_w": 1.3, "h4_w": 1.3, "rsi_slope": 2.5,
    },
    "H1": {
        "rsi_ob": [25, 30, 35], "rsi_os": [65, 70, 75],
        "stoch_ob": [15, 20], "stoch_os": [80, 85],
        "bb_pen": [0.5, 0.0], "consec": [5, 4, 3],
        "candle_w": 1.0, "h4_w": 1.0, "rsi_slope": 3,
    },
}


def add_enhanced_indicators(df):
    """Add the missing signal columns."""
    if not PANDAS_AVAILABLE:
        # Return the dataframe unchanged if pandas is not available
        # The EMA separation module will handle EMA signals independently
        return df
    # Ensure core indicator columns exist (may be missing when called on live candles)
    _needed = {"stoch_k", "stoch_d", "macd_histogram", "rsi", "adx", "bb_upper", "bb_lower"}
    if not _needed.issubset(df.columns):
        try:
            from backtester.indicators import compute_all as _compute_all
            df = _compute_all(df)
        except Exception:
            pass

    if "stoch_k" not in df.columns or "stoch_d" not in df.columns:
        try:
            from backtester.indicators import stochastic as _stoch_fn
            _stoch = _stoch_fn(df)
            df["stoch_k"] = _stoch["stoch_k"]
            df["stoch_d"] = _stoch["stoch_d"]
        except Exception:
            df["stoch_k"] = 50.0
            df["stoch_d"] = 50.0

    # Stochastic crossover
    df["stoch_k_cross_up"] = (df["stoch_k"] > df["stoch_d"]) & (df["stoch_k"].shift(1) <= df["stoch_d"].shift(1))
    df["stoch_k_cross_down"] = (df["stoch_k"] < df["stoch_d"]) & (df["stoch_k"].shift(1) >= df["stoch_d"].shift(1))

    # MACD zero-line cross
    mh = df["macd_histogram"]
    df["macd_cross_bull"] = (mh > 0) & (mh.shift(1) <= 0)
    df["macd_cross_bear"] = (mh < 0) & (mh.shift(1) >= 0)

    # Parabolic SAR direction (already computed as sar)
    if "sar" in df.columns:
        df["sar_bullish"] = df["close"] > df["sar"]
        df["sar_bearish"] = df["close"] < df["sar"]
    else:
        df["sar_bullish"] = False
        df["sar_bearish"] = False

    # CCI extremes
    if "cci" in df.columns:
        df["cci_oversold"] = df["cci"] < -100
        df["cci_overbought"] = df["cci"] > 100
        df["cci_extreme_os"] = df["cci"] < -200
        df["cci_extreme_ob"] = df["cci"] > 200
    else:
        df["cci_oversold"] = df["cci_overbought"] = df["cci_extreme_os"] = df["cci_extreme_ob"] = False

    # Fibonacci proximity (using fib levels already in df)
    atr = df["atr"]
    for fib_col in ["fib_236", "fib_382", "fib_500", "fib_618", "fib_786"]:
        if fib_col in df.columns:
            df[f"near_{fib_col}"] = (abs(df["close"] - df[fib_col]) / atr) < 0.5
        else:
            df[f"near_{fib_col}"] = False

    # Near any key fib (382, 500, 618 are the big ones)
    df["at_key_fib"] = df["near_fib_382"] | df["near_fib_500"] | df["near_fib_618"]
    df["at_golden_fib"] = df["near_fib_618"]  # The most important one

    # EMA crossover events — Cross 1: E21 × E55, Cross 2: E21 × E100
    # Cross 2 (E21×E100) is the confirmation cross — fan fully ordered, trend committed
    df["ema_21_cross_55_up"]   = (df["ema_21"] > df["ema_55"])  & (df["ema_21"].shift(1) <= df["ema_55"].shift(1))
    df["ema_21_cross_55_down"] = (df["ema_21"] < df["ema_55"])  & (df["ema_21"].shift(1) >= df["ema_55"].shift(1))
    df["ema_21_cross_100_up"]  = (df["ema_21"] > df["ema_100"]) & (df["ema_21"].shift(1) <= df["ema_100"].shift(1))
    df["ema_21_cross_100_down"]= (df["ema_21"] < df["ema_100"]) & (df["ema_21"].shift(1) >= df["ema_100"].shift(1))

    # Recent cross flags: did Cross 2 happen within the last 10 bars?
    _window = min(10, len(df))
    df["e21_crossed_100_recently_bull"] = df["ema_21_cross_100_up"].rolling(_window).max().fillna(0).astype(bool)
    df["e21_crossed_100_recently_bear"] = df["ema_21_cross_100_down"].rolling(_window).max().fillna(0).astype(bool)

    # Pivot point proximity
    if "pivot" in df.columns:
        df["near_pivot"] = (abs(df["close"] - df["pivot"]) / atr) < 0.5
        if "pivot_s1" in df.columns:
            df["near_s1"] = (abs(df["close"] - df["pivot_s1"]) / atr) < 0.5
        else:
            df["near_s1"] = False
        if "pivot_r1" in df.columns:
            df["near_r1"] = (abs(df["close"] - df["pivot_r1"]) / atr) < 0.5
        else:
            df["near_r1"] = False
    else:
        df["near_pivot"] = df["near_s1"] = df["near_r1"] = False

    return df


def compute_ema_separation_score(row) -> Dict[str, float]:
    """
    Compute EMA Fan/Separation score — NARRATIVE-AWARE.
    
    Reads the full EMA market picture (if available from market_picture)
    and scores based on the STORY: direction, fan state, velocity,
    velocity trend, E100 role, and how they work together.
    
    Falls back to raw EMA values if no market picture is available
    (e.g., during backtesting where we only have indicator columns).
    
    Returns:
        Dict with 'bull_score', 'bear_score', 'bias', 'fan_state',
        'trend_health', 'reversal_risk'
    """
    bull_score = bear_score = 0.0
    bias = 'neutral'
    fan_state = 'unknown'
    trend_health = 0
    reversal_risk = 'unknown'
    
    # ── Try to use pre-computed market picture (live trading) ────────
    mp_ema = row.get('_market_picture_ema')
    if mp_ema and isinstance(mp_ema, dict):
        return _score_from_market_picture(mp_ema)
    
    # ── Fallback: compute from raw indicator columns (backtesting) ──
    ema21 = row.get('ema_21', np.nan)
    ema55 = row.get('ema_55', np.nan) 
    ema100 = row.get('ema_100', np.nan)
    close = row.get('close', np.nan)
    
    if np.isnan(ema21) or np.isnan(ema55) or np.isnan(ema100) or np.isnan(close):
        return {'bull_score': 0, 'bear_score': 0, 'bias': 'neutral',
                'fan_state': 'unknown', 'trend_health': 0, 'reversal_risk': 'unknown'}
    
    ema21_above_55 = ema21 > ema55
    separation_pct = abs(ema21 - ema55) / close * 100 if close != 0 else 0
    sep_velocity = row.get('ema_separation_velocity', 0)
    
    # Fan ordering check
    fan_bullish = ema21 > ema55 > ema100
    fan_bearish = ema100 > ema55 > ema21
    fan_ordered = fan_bullish or fan_bearish
    
    if fan_bullish:
        bias = 'bullish'
    elif fan_bearish:
        bias = 'bearish'
    
    # ── 1. EMA Direction vs Trade Direction (0-5 pts) ────────────────
    # Points go to the TREND direction side — counter-trend scoring
    # happens at the trade-level when we compare buy_score vs sell_score
    if row.get('ema_21_cross_55_up', False):
        bull_score += 5  # Fresh cross = strong directional signal
    elif row.get('ema_21_cross_55_down', False):
        bear_score += 5
    elif fan_bullish:
        bull_score += 3  # Ordered fan = clear direction
    elif fan_bearish:
        bear_score += 3
    elif ema21_above_55:
        bull_score += 1  # Unordered but 21>55
    else:
        bear_score += 1
    
    # ── 2. Fan Health (0-4 pts) ──────────────────────────────────────
    # Velocity component
    if sep_velocity >= 0.007:
        vel_pts = 2  # Fast = real momentum
    elif sep_velocity >= 0.005:
        vel_pts = 1  # Moderate
    else:
        vel_pts = 0  # Slow = fakeout risk
    
    if ema21_above_55:
        bull_score += vel_pts
    else:
        bear_score += vel_pts
    
    # Separation level component
    if separation_pct > 0.10:
        sep_pts = 2  # Significant separation = real trend
    elif separation_pct > 0.05:
        sep_pts = 1
    else:
        sep_pts = 0
    
    if ema21_above_55:
        bull_score += sep_pts
    else:
        bear_score += sep_pts
    
    # ── 3. E100 Confirmation (0-3 pts) ───────────────────────────────
    avg_fast = (ema21 + ema55) / 2
    price_dist_e100 = abs(close - ema100) / close * 100
    
    if ema100 < avg_fast * 0.999:  # E100 as support
        bull_score += 2
        # Candle pattern at E100 bonus (if available)
        if price_dist_e100 < 0.08:
            bull_score += 1  # Price near E100 = potential bounce
    elif ema100 > avg_fast * 1.001:  # E100 as resistance
        bear_score += 2
        if price_dist_e100 < 0.08:
            bear_score += 1
    else:
        bull_score += 1
        bear_score += 1
    
    # ── 4. Trend Structure (0-3 pts) ─────────────────────────────────
    gap_55_100 = abs(ema55 - ema100) / close * 100
    
    if fan_ordered:
        if ema21_above_55:
            bull_score += 2
        else:
            bear_score += 2
    
    if gap_55_100 > 0.10:  # Deep trend structure
        if ema21_above_55:
            bull_score += 1
        else:
            bear_score += 1
    
    return {
        'bull_score': bull_score,
        'bear_score': bear_score, 
        'bias': bias,
        'fan_state': fan_state,
        'trend_health': trend_health,
        'reversal_risk': reversal_risk,
    }


def _score_from_market_picture(ema: Dict) -> Dict[str, float]:
    """
    Score EMA from a pre-computed market picture (live trading path).
    Reads the NARRATIVE — fan state, velocity trend, reversal risk —
    and scores based on the story, not isolated values.
    
    Max 15 points (same allocation as backtester path).
    """
    bull_score = bear_score = 0.0
    
    fan_dir = ema.get('fan_direction', 'mixed')
    fan_state = ema.get('fan_state', 'unknown')
    fan_ordered = ema.get('fan_ordered', False)
    velocity = ema.get('separation_velocity', 0)
    vel_trend = ema.get('fan_velocity_trend', 'unknown')
    cur_sep = ema.get('separation_pct', 0)
    e100_role = ema.get('ema100_role', 'neutral')
    gap_55_100 = ema.get('gap_55_100', 0)
    trend_health = ema.get('trend_health', 0)
    reversal_risk = ema.get('reversal_risk', 'unknown')
    e100_pattern = ema.get('e100_candle_pattern')
    
    is_bull = fan_dir == 'bullish'
    is_bear = fan_dir == 'bearish'
    
    bias = 'bullish' if is_bull else ('bearish' if is_bear else 'neutral')
    
    # ── 1. Direction + Fan State (0-5 pts) ───────────────────────────
    if fan_state == 'just_crossed':
        # Fresh cross = strong directional signal
        if is_bull: bull_score += 5
        elif is_bear: bear_score += 5
    elif fan_ordered and fan_state == 'expanding':
        # Ordered fan expanding = clear healthy trend
        if is_bull: bull_score += 4
        elif is_bear: bear_score += 4
    elif fan_ordered and fan_state in ('stable', 'decelerating'):
        if is_bull: bull_score += 3
        elif is_bear: bear_score += 3
    elif fan_ordered and fan_state in ('contracting', 'peaked'):
        # Trend exhausting — direction still counts but less weight
        if is_bull: bull_score += 1
        elif is_bear: bear_score += 1
    elif not fan_ordered:
        # Mixed — no directional edge
        bull_score += 0
        bear_score += 0
    
    # ── 2. Fan Health — Velocity + Trend (0-4 pts) ───────────────────
    vel_pts = 0
    if velocity >= 0.007:
        vel_pts = 2
    elif velocity >= 0.005:
        vel_pts = 1
    
    # Velocity trend bonus
    if vel_trend == 'accelerating':
        vel_pts = min(vel_pts + 1, 2)  # cap at 2
    elif vel_trend == 'decelerating' and vel_pts > 0:
        vel_pts = max(vel_pts - 1, 0)  # reduce for fading momentum
    
    # Separation level
    sep_pts = 0
    if cur_sep >= 0.20:
        sep_pts = 2
    elif cur_sep >= 0.10:
        sep_pts = 1
    
    health_pts = vel_pts + sep_pts  # 0-4
    if is_bull:
        bull_score += health_pts
    elif is_bear:
        bear_score += health_pts
    else:
        # Mixed: split
        bull_score += health_pts // 2
        bear_score += health_pts // 2
    
    # ── 3. E100 Confirmation (0-3 pts) ───────────────────────────────
    if e100_role == 'support':
        bull_score += 2
    elif e100_role == 'resistance':
        bear_score += 2
    else:
        bull_score += 1
        bear_score += 1
    
    # E100 candle pattern = strong directional confirmation
    if e100_pattern:
        pat_dir = e100_pattern.get('direction')
        if pat_dir == 'buy':
            bull_score += 1
        elif pat_dir == 'sell':
            bear_score += 1
    
    # ── 4. Trend Structure (0-3 pts) ─────────────────────────────────
    if fan_ordered:
        if is_bull: bull_score += 2
        elif is_bear: bear_score += 2
    
    if gap_55_100 > 0.10:
        if is_bull: bull_score += 1
        elif is_bear: bear_score += 1
    
    return {
        'bull_score': bull_score,
        'bear_score': bear_score, 
        'bias': bias,
        'fan_state': fan_state,
        'trend_health': trend_health,
        'reversal_risk': reversal_risk,
    }


def score_v4(row, p):
    """Enhanced scoring with all research findings."""
    if not PANDAS_AVAILABLE and hasattr(row, 'get'):
        # Handle dict-like row objects when pandas is not available
        c = row.get("close", 0)
        rsi = row.get("rsi", 50)
        sk = row.get("stoch_k", 50)
        adx = row.get("adx", 25)
        h4b = row.get("h4_bias", "none")
        h4r = row.get("h4_rsi", 50)
    else:
        # Handle pandas Series objects
        c = row["close"] if "close" in row else row.get("close", 0)
        rsi = row.get("rsi", 50)
        sk = row.get("stoch_k", 50)
        adx = row.get("adx", 25)
        h4b = row.get("h4_bias", "none")
        h4r = row.get("h4_rsi", 50)
    
    cw = p["candle_w"]
    hw = p["h4_w"]
    is_trending = adx > 25
    is_ranging = adx < 20

    sb = ss = 0

    # ===== EMA FAN/SEPARATION SCORING (up to 15 points) =====
    try:
        ema_scores = compute_ema_separation_score(row)
        sb += ema_scores['bull_score']
        ss += ema_scores['bear_score']
        ema_bias = ema_scores['bias']
    except Exception:
        # Fallback if EMA scoring fails
        ema_bias = 'neutral'

    # ===== CATEGORY 1: MOMENTUM (RSI + Stochastic + CCI) =====

    # RSI extremes (adaptive)
    rb = p["rsi_ob"]
    if rsi < rb[0]: sb += 3
    elif rsi < rb[1]: sb += 2
    elif rsi < rb[2]: sb += 1
    rs = p["rsi_os"]
    if rsi > rs[2]: ss += 3
    elif rsi > rs[1]: ss += 2
    elif rsi > rs[0]: ss += 1

    # Stochastic CROSSOVER (the actual signal, not just extremes)
    skb = p["stoch_ob"]
    if row.get("stoch_k_cross_up", False) and sk < skb[1]:
        sb += 3  # Bullish crossover in oversold = strong
    elif sk < skb[0]:
        sb += 1
    sks = p["stoch_os"]
    if row.get("stoch_k_cross_down", False) and sk > sks[0]:
        ss += 3  # Bearish crossover in overbought = strong
    elif sk > sks[1]:
        ss += 1

    # CCI extremes (bonus confirmation)
    if row.get("cci_extreme_os", False): sb += 2
    elif row.get("cci_oversold", False): sb += 1
    if row.get("cci_extreme_ob", False): ss += 2
    elif row.get("cci_overbought", False): ss += 1

    # RSI slope (turning)
    rsl = row.get("rsi_slope", 0)
    if rsl > p["rsi_slope"] and rsi < rb[2]+5: sb += 2
    if rsl < -p["rsi_slope"] and rsi > rs[0]-5: ss += 2

    # ===== CATEGORY 2: TREND (ADX + EMA + MACD + SAR) =====

    # MACD zero-line cross (EVENT, not just sign)
    if row.get("macd_cross_bull", False): sb += 3
    elif row.get("macd_histogram", 0) > 0: sb += 1
    if row.get("macd_cross_bear", False): ss += 3
    elif row.get("macd_histogram", 0) < 0: ss += 1

    # Parabolic SAR direction
    if row.get("sar_bullish", False): sb += 1
    if row.get("sar_bearish", False): ss += 1

    # EMA crossover events (strong trend signal)
    if row.get("ema_21_cross_55_up", False): sb += 2
    if row.get("ema_21_cross_55_down", False): ss += 2
    # Cross 2: E21 × E100 — fan fully ordered, stronger confirmation (3 pts)
    if row.get("ema_21_cross_100_up", False): sb += 3
    if row.get("ema_21_cross_100_down", False): ss += 3
    # Recent Cross 2 (within 10 bars) — still fresh signal (2 pts)
    if row.get("e21_crossed_100_recently_bull", False) and not row.get("ema_21_cross_100_up", False): sb += 2
    if row.get("e21_crossed_100_recently_bear", False) and not row.get("ema_21_cross_100_down", False): ss += 2

    # ===== CATEGORY 3: VOLATILITY (BB + Support/Resistance) =====

    # BB penetration (adaptive)
    blp = row.get("bb_lower_pen", 0)
    if blp > p["bb_pen"][0]: sb += 3
    elif blp > p["bb_pen"][1]: sb += 2
    elif c < row.get("bb_lower", c) * 1.002: sb += 1
    bup = row.get("bb_upper_pen", 0)
    if bup > p["bb_pen"][0]: ss += 3
    elif bup > p["bb_pen"][1]: ss += 2
    elif c > row.get("bb_upper", c) * 0.998: ss += 1

    # Swing proximity (support/resistance)
    if row.get("near_swing_low", False): sb += 2
    if row.get("near_swing_high", False): ss += 2

    # ===== FIBONACCI CONFLUENCE (BONUS — pattern at fib = high prob) =====
    at_fib = row.get("at_key_fib", False)
    at_golden = row.get("at_golden_fib", False)
    if at_golden:
        sb += 3  # At 61.8% fib = strongest level
        ss += 3
    elif at_fib:
        sb += 2  # At 38.2/50/61.8
        ss += 2

    # Pivot point proximity
    if row.get("near_s1", False): sb += 2  # Near support
    if row.get("near_r1", False): ss += 2  # Near resistance
    if row.get("near_pivot", False):
        sb += 1
        ss += 1

    # ===== CANDLESTICK PATTERNS (weighted by TF) =====
    if row.get("hammer", False): sb += int(3 * cw)
    if row.get("bullish_engulfing", False): sb += int(4 * cw)
    if row.get("morning_star", False): sb += int(4 * cw)
    if row.get("dragonfly_doji", False): sb += int(2 * cw)
    if row.get("three_white_soldiers", False): sb += int(3 * cw)
    bc = row.get("candle_bull_signal", 0)
    if bc >= 2: sb += int(2 * cw)
    elif bc >= 1: sb += int(1 * cw)

    if row.get("shooting_star", False): ss += int(3 * cw)
    if row.get("bearish_engulfing", False): ss += int(4 * cw)
    if row.get("evening_star", False): ss += int(4 * cw)
    if row.get("gravestone_doji", False): ss += int(2 * cw)
    if row.get("three_black_crows", False): ss += int(3 * cw)
    brc = row.get("candle_bear_signal", 0)
    if brc >= 2: ss += int(2 * cw)
    elif brc >= 1: ss += int(1 * cw)

    # Consecutive candle exhaustion (adaptive)
    cb = row.get("consec_bear", 0)
    cs = p["consec"]
    if cb >= cs[0]: sb += 3
    elif cb >= cs[1]: sb += 2
    elif cb >= cs[2]: sb += 1
    cbl = row.get("consec_bull", 0)
    if cbl >= cs[0]: ss += 3
    elif cbl >= cs[1]: ss += 2
    elif cbl >= cs[2]: ss += 1

    # ===== H4 ALIGNMENT (weighted more on lower TFs) =====
    if h4b == "bull":
        sb += int(2 * hw)
    elif h4b == "range":
        sb += 1
    elif h4b == "bear":
        sb -= int(2 * hw)
    if h4b == "bear":
        ss += int(2 * hw)
    elif h4b == "range":
        ss += 1
    elif h4b == "bull":
        ss -= int(2 * hw)

    if not pd.isna(h4r):
        if h4r < 35: sb += 2
        if h4r > 65: ss += 2

    # ===== REGIME BONUS (use the right strategy for the market) =====
    # In trending: reward trend-following signals more
    if is_trending:
        if row.get("sar_bullish", False) and row.get("macd_histogram", 0) > 0:
            sb += 2  # SAR + MACD agree in trend = bonus
        if row.get("sar_bearish", False) and row.get("macd_histogram", 0) < 0:
            ss += 2
    # In ranging: reward mean reversion more
    if is_ranging:
        if blp > 0 and rsi < rb[1]:  # Below BB + oversold in range
            sb += 2
        if bup > 0 and rsi > rs[1]:
            ss += 2

    # ===== EMA NARRATIVE ADJUSTMENT =====
    # Uses fan state + velocity trend to modulate scores.
    # Key insight: our sniper is MEAN REVERSION — counter-trend entries
    # into a DECELERATING fan are the best setups.
    
    ema_fan_state = ema_scores.get('fan_state', 'unknown') if isinstance(ema_scores, dict) else 'unknown'
    ema_reversal_risk = ema_scores.get('reversal_risk', 'unknown') if isinstance(ema_scores, dict) else 'unknown'
    
    if ema_bias == 'bullish':
        # Bull trend: reward bullish, but also reward counter-trend sells
        # when the trend is exhausting
        if ema_fan_state in ('decelerating', 'peaked', 'contracting'):
            # Trend weakening — counter-trend SELL setups get a boost
            ss += 3  # Reversal setup reward
            sb += 1  # Reduced with-trend bonus (trend fading)
        elif ema_fan_state in ('expanding', 'just_crossed'):
            # Trend healthy — with-trend buys rewarded, counter-trend penalized
            sb += 3
            ss -= 2  # Don't sell into a healthy bull trend
        else:
            sb += 2
        
    elif ema_bias == 'bearish':
        if ema_fan_state in ('decelerating', 'peaked', 'contracting'):
            sb += 3  # Counter-trend BUY boost (bear trend exhausting)
            ss += 1
        elif ema_fan_state in ('expanding', 'just_crossed'):
            ss += 3
            sb -= 2  # Don't buy into a healthy bear trend
        else:
            ss += 2
    
    # Indicator confluence with EMA direction
    if ema_bias == 'bullish' and rsi < rb[1] and row.get("macd_histogram", 0) > 0:
        sb += 2  # RSI + MACD confirm bullish
    elif ema_bias == 'bearish' and rsi > rs[1] and row.get("macd_histogram", 0) < 0:
        ss += 2  # RSI + MACD confirm bearish

    # ===== DIVERGENCE SCORING (setup-type aware) =====
    # Backtested: divergence boosts reversals +8-24% WR, hurts trends -6-25% WR
    # It's a REVERSAL signal — confirms mean reversion, contradicts trend following
    
    # Detect divergence from row data (columns added by divergence.add_divergence_signals)
    rsi_bull_div = bool(row.get("rsi_bull_div", False))
    rsi_bear_div = bool(row.get("rsi_bear_div", False))
    rsi_hid_bull = bool(row.get("rsi_hidden_bull_div", False))
    rsi_hid_bear = bool(row.get("rsi_hidden_bear_div", False))
    macd_bull_div = bool(row.get("macd_bull_div", False))
    macd_bear_div = bool(row.get("macd_bear_div", False))
    
    has_bull_div = rsi_bull_div or macd_bull_div  # reversal UP signals
    has_bear_div = rsi_bear_div or macd_bear_div  # reversal DOWN signals
    has_hidden_bull = rsi_hid_bull  # continuation UP
    has_hidden_bear = rsi_hid_bear  # continuation DOWN
    
    # Regular divergence: BOOST the reversal direction, PENALIZE the trend direction
    # RSI divergence is strongest (+5/-4), MACD is confirmation (+3/-2)
    if rsi_bull_div:
        sb += 5  # Strong reversal UP signal
        ss -= 4  # Penalize selling into bullish divergence
    if rsi_bear_div:
        ss += 5  # Strong reversal DOWN signal
        sb -= 4  # Penalize buying into bearish divergence
    if macd_bull_div:
        sb += 3  # MACD confirms bullish reversal
        ss -= 2
    if macd_bear_div:
        ss += 3  # MACD confirms bearish reversal
        sb -= 2
    
    # Hidden divergence: BOOST the continuation direction
    # (price HL + RSI LL = trend still has legs)
    if has_hidden_bull:
        sb += 2  # Continuation UP
    if has_hidden_bear:
        ss += 2  # Continuation DOWN

    # Ensure scores don't go negative
    sb = max(sb, 0)
    ss = max(ss, 0)

    return sb, ss
