#!/usr/bin/env python3
"""Compute all technical indicators from OHLCV data using pandas + numpy."""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Trend indicators
# ---------------------------------------------------------------------------

def sma(df: pd.DataFrame, period: int, col: str = "close") -> pd.Series:
    return df[col].rolling(window=period, min_periods=period).mean()


def ema(df: pd.DataFrame, period: int, col: str = "close") -> pd.Series:
    return df[col].ewm(span=period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index. Returns df with adx, plus_di, minus_di columns."""
    high, low, close = df["high"], df["low"], df["close"]

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    return pd.DataFrame({"adx": adx_val, "plus_di": plus_di, "minus_di": minus_di}, index=df.index)


def parabolic_sar(df: pd.DataFrame, af_start: float = 0.02, af_max: float = 0.2) -> pd.Series:
    """Parabolic SAR."""
    high, low = df["high"].values, df["low"].values
    n = len(df)
    sar = np.zeros(n)
    af = af_start
    trend_up = True
    ep = high[0]
    sar[0] = low[0]

    for i in range(1, n):
        if trend_up:
            sar[i] = sar[i - 1] + af * (ep - sar[i - 1])
            sar[i] = min(sar[i], low[i - 1], low[max(0, i - 2)])
            if low[i] < sar[i]:
                trend_up = False
                sar[i] = ep
                ep = low[i]
                af = af_start
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_start, af_max)
        else:
            sar[i] = sar[i - 1] + af * (ep - sar[i - 1])
            sar[i] = max(sar[i], high[i - 1], high[max(0, i - 2)])
            if high[i] > sar[i]:
                trend_up = True
                sar[i] = ep
                ep = high[i]
                af = af_start
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_start, af_max)

    return pd.Series(sar, index=df.index, name="parabolic_sar")


# ---------------------------------------------------------------------------
# Momentum indicators
# ---------------------------------------------------------------------------

def rsi(df: pd.DataFrame, period: int = 14, col: str = "close") -> pd.Series:
    delta = df[col].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9, col: str = "close") -> pd.DataFrame:
    fast_ema = df[col].ewm(span=fast, adjust=False).mean()
    slow_ema = df[col].ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({
        "macd_line": macd_line, "macd_signal": signal_line, "macd_histogram": histogram
    }, index=df.index)


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    low_min = df["low"].rolling(window=k_period).min()
    high_max = df["high"].rolling(window=k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(window=d_period).mean()
    return pd.DataFrame({"stoch_k": k, "stoch_d": d}, index=df.index)


def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = tp.rolling(window=period).mean()
    mad = tp.rolling(window=period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma_tp) / (0.015 * mad).replace(0, np.nan)


# ---------------------------------------------------------------------------
# Volatility indicators
# ---------------------------------------------------------------------------

def bollinger_bands(df: pd.DataFrame, period: int = 20, std: float = 2, col: str = "close") -> pd.DataFrame:
    mid = df[col].rolling(window=period).mean()
    std_dev = df[col].rolling(window=period).std()
    return pd.DataFrame({
        "bb_upper": mid + std * std_dev,
        "bb_middle": mid,
        "bb_lower": mid - std * std_dev,
        "bb_width": (2 * std * std_dev) / mid,  # normalized width
    }, index=df.index)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift()).abs()
    tr3 = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


# ---------------------------------------------------------------------------
# Chart studies
# ---------------------------------------------------------------------------

def fibonacci_levels(swing_high: float, swing_low: float) -> dict:
    diff = swing_high - swing_low
    return {
        "fib_0": swing_high,
        "fib_236": swing_high - 0.236 * diff,
        "fib_382": swing_high - 0.382 * diff,
        "fib_500": swing_high - 0.500 * diff,
        "fib_618": swing_high - 0.618 * diff,
        "fib_786": swing_high - 0.786 * diff,
        "fib_1": swing_low,
    }


def pivot_points(prev_high: float, prev_low: float, prev_close: float) -> dict:
    pivot = (prev_high + prev_low + prev_close) / 3
    return {
        "pivot": pivot,
        "r1": 2 * pivot - prev_low,
        "r2": pivot + (prev_high - prev_low),
        "r3": prev_high + 2 * (pivot - prev_low),
        "s1": 2 * pivot - prev_high,
        "s2": pivot - (prev_high - prev_low),
        "s3": prev_low - 2 * (prev_high - pivot),
    }


def adr(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Average Daily Range in price (not pips). Requires daily grouping."""
    daily_range = df["high"] - df["low"]
    return daily_range.rolling(window=period, min_periods=1).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Simple cumulative VWAP (resets not implemented for simplicity)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    cum_tp_vol = (tp * df["volume"]).cumsum()
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    return cum_tp_vol / cum_vol


# ---------------------------------------------------------------------------
# Composite: compute everything
# ---------------------------------------------------------------------------

def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicator columns to a copy of the DataFrame."""
    df = df.copy()

    # SMAs
    for p in [50, 100, 200]:
        df[f"sma_{p}"] = sma(df, p)

    # EMAs
    for p in [21, 50, 55, 100, 200]:
        df[f"ema_{p}"] = ema(df, p)

    # RSI
    df["rsi"] = rsi(df)

    # MACD
    macd_df = macd(df)
    df["macd_line"] = macd_df["macd_line"]
    df["macd_signal"] = macd_df["macd_signal"]
    df["macd_histogram"] = macd_df["macd_histogram"]

    # Stochastic
    stoch_df = stochastic(df)
    df["stoch_k"] = stoch_df["stoch_k"]
    df["stoch_d"] = stoch_df["stoch_d"]

    # CCI
    df["cci"] = cci(df)

    # Bollinger Bands
    bb_df = bollinger_bands(df)
    df["bb_upper"] = bb_df["bb_upper"]
    df["bb_middle"] = bb_df["bb_middle"]
    df["bb_lower"] = bb_df["bb_lower"]
    df["bb_width"] = bb_df["bb_width"]

    # ADX
    adx_df = adx(df)
    df["adx"] = adx_df["adx"]
    df["plus_di"] = adx_df["plus_di"]
    df["minus_di"] = adx_df["minus_di"]

    # ATR
    df["atr"] = atr(df)

    # Parabolic SAR
    df["parabolic_sar"] = parabolic_sar(df)

    # VWAP
    df["vwap"] = vwap(df)

    # ADR (use raw H-L as proxy for H1 candles)
    df["adr"] = adr(df, period=20)

    return df
