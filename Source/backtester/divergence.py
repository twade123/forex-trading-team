#!/usr/bin/env python3
"""Divergence detection: regular (reversal) and hidden (continuation)."""

import numpy as np
import pandas as pd


def _find_swing_highs(series: pd.Series, order: int = 5) -> pd.Series:
    """Return boolean Series marking local swing highs."""
    highs = pd.Series(False, index=series.index)
    vals = series.values
    for i in range(order, len(vals) - order):
        if all(vals[i] >= vals[i - j] for j in range(1, order + 1)) and \
           all(vals[i] >= vals[i + j] for j in range(1, order + 1)):
            highs.iloc[i] = True
    return highs


def _find_swing_lows(series: pd.Series, order: int = 5) -> pd.Series:
    """Return boolean Series marking local swing lows."""
    lows = pd.Series(False, index=series.index)
    vals = series.values
    for i in range(order, len(vals) - order):
        if all(vals[i] <= vals[i - j] for j in range(1, order + 1)) and \
           all(vals[i] <= vals[i + j] for j in range(1, order + 1)):
            lows.iloc[i] = True
    return lows


def detect_rsi_divergence(df: pd.DataFrame, lookback: int = 20, order: int = 5) -> pd.DataFrame:
    """Detect regular and hidden RSI divergence.

    Regular bullish: price LL, RSI HL → reversal up
    Regular bearish: price HH, RSI LH → reversal down
    Hidden bullish: price HL, RSI LL → continuation up
    Hidden bearish: price LH, RSI HH → continuation down
    """
    result = pd.DataFrame(index=df.index)
    result["rsi_bull_div"] = False
    result["rsi_bear_div"] = False
    result["rsi_hidden_bull_div"] = False
    result["rsi_hidden_bear_div"] = False

    if "rsi" not in df.columns:
        return result

    price = df["close"].values
    rsi_vals = df["rsi"].values
    price_lows = _find_swing_lows(df["close"], order)
    price_highs = _find_swing_highs(df["close"], order)

    low_indices = np.where(price_lows.values)[0]
    high_indices = np.where(price_highs.values)[0]

    # Regular bullish: price LL + RSI HL
    for i in range(1, len(low_indices)):
        idx_prev, idx_curr = low_indices[i - 1], low_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if price[idx_curr] < price[idx_prev] and rsi_vals[idx_curr] > rsi_vals[idx_prev]:
            result.iloc[idx_curr, result.columns.get_loc("rsi_bull_div")] = True

    # Regular bearish: price HH + RSI LH
    for i in range(1, len(high_indices)):
        idx_prev, idx_curr = high_indices[i - 1], high_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if price[idx_curr] > price[idx_prev] and rsi_vals[idx_curr] < rsi_vals[idx_prev]:
            result.iloc[idx_curr, result.columns.get_loc("rsi_bear_div")] = True

    # Hidden bullish: price HL + RSI LL
    for i in range(1, len(low_indices)):
        idx_prev, idx_curr = low_indices[i - 1], low_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if price[idx_curr] > price[idx_prev] and rsi_vals[idx_curr] < rsi_vals[idx_prev]:
            result.iloc[idx_curr, result.columns.get_loc("rsi_hidden_bull_div")] = True

    # Hidden bearish: price LH + RSI HH
    for i in range(1, len(high_indices)):
        idx_prev, idx_curr = high_indices[i - 1], high_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if price[idx_curr] < price[idx_prev] and rsi_vals[idx_curr] > rsi_vals[idx_prev]:
            result.iloc[idx_curr, result.columns.get_loc("rsi_hidden_bear_div")] = True

    return result


def detect_macd_divergence(df: pd.DataFrame, lookback: int = 20, order: int = 5) -> pd.DataFrame:
    """Same as RSI divergence but using MACD histogram."""
    result = pd.DataFrame(index=df.index)
    result["macd_bull_div"] = False
    result["macd_bear_div"] = False

    if "macd_histogram" not in df.columns:
        return result

    price = df["close"].values
    macd_vals = df["macd_histogram"].values
    price_lows = _find_swing_lows(df["close"], order)
    price_highs = _find_swing_highs(df["close"], order)

    low_indices = np.where(price_lows.values)[0]
    high_indices = np.where(price_highs.values)[0]

    for i in range(1, len(low_indices)):
        idx_prev, idx_curr = low_indices[i - 1], low_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if price[idx_curr] < price[idx_prev] and macd_vals[idx_curr] > macd_vals[idx_prev]:
            result.iloc[idx_curr, result.columns.get_loc("macd_bull_div")] = True

    for i in range(1, len(high_indices)):
        idx_prev, idx_curr = high_indices[i - 1], high_indices[i]
        if idx_curr - idx_prev > lookback:
            continue
        if price[idx_curr] > price[idx_prev] and macd_vals[idx_curr] < macd_vals[idx_prev]:
            result.iloc[idx_curr, result.columns.get_loc("macd_bear_div")] = True

    return result


def add_divergence_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Add all divergence columns to the DataFrame."""
    df = df.copy()
    rsi_div = detect_rsi_divergence(df)
    macd_div = detect_macd_divergence(df)
    for col in rsi_div.columns:
        df[col] = rsi_div[col]
    for col in macd_div.columns:
        df[col] = macd_div[col]
    return df
