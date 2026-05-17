#!/usr/bin/env python3
"""Candlestick pattern detection — the missing piece.

Detects patterns from OHLC data mathematically. No image processing needed.
Each pattern returns a signal strength: positive = bullish, negative = bearish.
"""

import numpy as np
import pandas as pd


def body_size(row):
    """Absolute body size."""
    return abs(row["close"] - row["open"])


def upper_wick(row):
    """Upper shadow length."""
    return row["high"] - max(row["close"], row["open"])


def lower_wick(row):
    """Lower shadow length."""
    return min(row["close"], row["open"]) - row["low"]


def candle_range(row):
    """Full candle range."""
    return row["high"] - row["low"]


def is_bullish(row):
    return row["close"] > row["open"]


def is_bearish(row):
    return row["close"] < row["open"]


def detect_all_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Add candlestick pattern columns to the DataFrame."""
    df = df.copy()
    n = len(df)

    # Pre-compute candle components
    bodies = (df["close"] - df["open"]).abs()
    ranges = df["high"] - df["low"]
    upper_wicks = df["high"] - df[["close", "open"]].max(axis=1)
    lower_wicks = df[["close", "open"]].min(axis=1) - df["low"]
    bullish = df["close"] > df["open"]
    bearish = df["close"] < df["open"]
    avg_body = bodies.rolling(20).mean()
    avg_range = ranges.rolling(20).mean()

    # === SINGLE CANDLE PATTERNS ===

    # HAMMER (bullish reversal): small body at top, long lower wick
    # lower_wick >= 2× body, upper_wick < 0.3× range, body in upper 1/3
    df["hammer"] = (
        (lower_wicks >= 2 * bodies) &
        (upper_wicks < ranges * 0.3) &
        (bodies > 0) &
        (ranges > avg_range * 0.5)  # Not too tiny
    )

    # INVERTED HAMMER (bullish reversal after downtrend): small body at bottom, long upper wick
    df["inverted_hammer"] = (
        (upper_wicks >= 2 * bodies) &
        (lower_wicks < ranges * 0.3) &
        (bodies > 0) &
        (ranges > avg_range * 0.5)
    )

    # SHOOTING STAR (bearish reversal): small body at bottom, long upper wick
    # Same shape as inverted hammer but at top of move
    df["shooting_star"] = (
        (upper_wicks >= 2 * bodies) &
        (lower_wicks < ranges * 0.3) &
        (bodies > 0) &
        (ranges > avg_range * 0.5)
    )

    # DOJI: very small body relative to range
    df["doji"] = (
        (bodies < ranges * 0.1) &
        (ranges > avg_range * 0.3)
    )

    # DRAGONFLY DOJI: doji with long lower wick (bullish)
    df["dragonfly_doji"] = (
        df["doji"] &
        (lower_wicks >= ranges * 0.6)
    )

    # GRAVESTONE DOJI: doji with long upper wick (bearish)
    df["gravestone_doji"] = (
        df["doji"] &
        (upper_wicks >= ranges * 0.6)
    )

    # MARUBOZU: full body, almost no wicks (strong conviction)
    df["marubozu_bull"] = (
        bullish &
        (bodies >= ranges * 0.85) &
        (bodies > avg_body * 1.5)
    )
    df["marubozu_bear"] = (
        bearish &
        (bodies >= ranges * 0.85) &
        (bodies > avg_body * 1.5)
    )

    # SPINNING TOP: small body, long wicks on both sides (indecision)
    df["spinning_top"] = (
        (bodies < ranges * 0.3) &
        (upper_wicks > ranges * 0.25) &
        (lower_wicks > ranges * 0.25)
    )

    # === TWO CANDLE PATTERNS ===

    prev_bullish = bullish.shift(1)
    prev_bearish = bearish.shift(1)
    prev_bodies = bodies.shift(1)
    prev_open = df["open"].shift(1)
    prev_close = df["close"].shift(1)
    prev_high = df["high"].shift(1)
    prev_low = df["low"].shift(1)

    # BULLISH ENGULFING: bearish candle followed by larger bullish candle that engulfs it
    df["bullish_engulfing"] = (
        prev_bearish.fillna(False) &
        bullish &
        (df["open"] <= prev_close) &
        (df["close"] >= prev_open) &
        (bodies > prev_bodies)
    )

    # BEARISH ENGULFING: bullish candle followed by larger bearish candle
    df["bearish_engulfing"] = (
        prev_bullish.fillna(False) &
        bearish &
        (df["open"] >= prev_close) &
        (df["close"] <= prev_open) &
        (bodies > prev_bodies)
    )

    # PIERCING LINE (bullish): bearish candle, then bullish that opens below prev low
    # and closes above midpoint of prev body
    prev_midpoint = (prev_open + prev_close) / 2
    df["piercing_line"] = (
        prev_bearish.fillna(False) &
        bullish &
        (df["open"] < prev_low) &
        (df["close"] > prev_midpoint) &
        (df["close"] < prev_open)
    )

    # DARK CLOUD COVER (bearish): bullish candle, then bearish that opens above prev high
    df["dark_cloud"] = (
        prev_bullish.fillna(False) &
        bearish &
        (df["open"] > prev_high) &
        (df["close"] < prev_midpoint) &
        (df["close"] > prev_open)
    )

    # TWEEZER BOTTOM (bullish): two candles with same/similar lows
    df["tweezer_bottom"] = (
        prev_bearish.fillna(False) &
        bullish &
        (abs(df["low"] - prev_low) < avg_range * 0.05)
    )

    # TWEEZER TOP (bearish): two candles with same/similar highs
    df["tweezer_top"] = (
        prev_bullish.fillna(False) &
        bearish &
        (abs(df["high"] - prev_high) < avg_range * 0.05)
    )

    # === THREE CANDLE PATTERNS ===

    prev2_bullish = bullish.shift(2)
    prev2_bearish = bearish.shift(2)
    prev2_close = df["close"].shift(2)
    prev2_open = df["open"].shift(2)
    prev2_bodies = bodies.shift(2)

    # MORNING STAR (bullish reversal): bearish, small body, bullish
    df["morning_star"] = (
        prev2_bearish.fillna(False) &
        (bodies.shift(1) < avg_body * 0.5) &  # Small middle candle
        bullish &
        (df["close"] > (prev2_open + prev2_close) / 2) &  # Closes above midpoint of first
        (prev2_bodies > avg_body)  # First candle is significant
    )

    # EVENING STAR (bearish reversal): bullish, small body, bearish
    df["evening_star"] = (
        prev2_bullish.fillna(False) &
        (bodies.shift(1) < avg_body * 0.5) &
        bearish &
        (df["close"] < (prev2_open + prev2_close) / 2) &
        (prev2_bodies > avg_body)
    )

    # THREE WHITE SOLDIERS (strong bullish): three consecutive bullish candles, each closing higher
    df["three_white_soldiers"] = (
        bullish &
        prev_bullish.fillna(False) &
        prev2_bullish.fillna(False) &
        (df["close"] > prev_close) &
        (prev_close > prev2_close) &
        (bodies > avg_body * 0.7) &
        (prev_bodies > avg_body * 0.7)
    )

    # THREE BLACK CROWS (strong bearish): three consecutive bearish candles
    df["three_black_crows"] = (
        bearish &
        prev_bearish.fillna(False) &
        prev2_bearish.fillna(False) &
        (df["close"] < prev_close) &
        (prev_close < prev2_close) &
        (bodies > avg_body * 0.7) &
        (prev_bodies > avg_body * 0.7)
    )

    # === COMPOSITE SIGNALS ===

    # Bullish candle signal: sum of bullish patterns (0 = none, higher = stronger)
    df["candle_bull_signal"] = (
        df["hammer"].astype(int) * 2 +
        df["bullish_engulfing"].astype(int) * 3 +
        df["morning_star"].astype(int) * 3 +
        df["piercing_line"].astype(int) * 2 +
        df["three_white_soldiers"].astype(int) * 3 +
        df["dragonfly_doji"].astype(int) * 1 +
        df["tweezer_bottom"].astype(int) * 2 +
        df["inverted_hammer"].astype(int) * 1 +
        df["marubozu_bull"].astype(int) * 2
    )

    # Bearish candle signal
    df["candle_bear_signal"] = (
        df["shooting_star"].astype(int) * 2 +
        df["bearish_engulfing"].astype(int) * 3 +
        df["evening_star"].astype(int) * 3 +
        df["dark_cloud"].astype(int) * 2 +
        df["three_black_crows"].astype(int) * 3 +
        df["gravestone_doji"].astype(int) * 1 +
        df["tweezer_top"].astype(int) * 2 +
        df["marubozu_bear"].astype(int) * 2
    )

    # Indecision signal
    df["candle_indecision"] = (
        df["doji"].astype(int) +
        df["spinning_top"].astype(int)
    )

    return df


def detect_support_resistance(df: pd.DataFrame, lookback: int = 100, tolerance_pct: float = 0.001) -> pd.DataFrame:
    """Detect dynamic support and resistance levels from price action.
    
    Looks for price levels where highs/lows cluster (multiple touches).
    """
    df = df.copy()
    df["support"] = np.nan
    df["resistance"] = np.nan
    df["at_support"] = False
    df["at_resistance"] = False

    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values

    for i in range(lookback, len(df)):
        window_highs = highs[i - lookback:i]
        window_lows = lows[i - lookback:i]
        close = closes[i]
        tol = close * tolerance_pct

        # Find resistance: levels where multiple highs cluster
        # Sort highs and find clusters
        sorted_highs = np.sort(window_highs)[::-1]
        best_res = None
        best_res_touches = 0
        for h in sorted_highs[:20]:  # Check top 20 highs
            if h < close:
                continue
            touches = np.sum(np.abs(window_highs - h) < tol)
            if touches >= 3 and touches > best_res_touches:
                best_res = h
                best_res_touches = touches

        # Find support: levels where multiple lows cluster
        sorted_lows = np.sort(window_lows)
        best_sup = None
        best_sup_touches = 0
        for l in sorted_lows[:20]:
            if l > close:
                continue
            touches = np.sum(np.abs(window_lows - l) < tol)
            if touches >= 3 and touches > best_sup_touches:
                best_sup = l
                best_sup_touches = touches

        if best_res is not None:
            df.iloc[i, df.columns.get_loc("resistance")] = best_res
            df.iloc[i, df.columns.get_loc("at_resistance")] = abs(close - best_res) < tol * 2

        if best_sup is not None:
            df.iloc[i, df.columns.get_loc("support")] = best_sup
            df.iloc[i, df.columns.get_loc("at_support")] = abs(close - best_sup) < tol * 2

    return df
