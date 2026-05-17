"""Setup signal backtest — evaluate candidate scout setup detectors.

For each pair, fetches last N days of M15 candles, computes indicators (same
as live scout via add_enhanced_indicators), then walks bar-by-bar asking each
candidate detector "would you fire here?". On a fire, simulates a synthetic
trade with fixed SL/TP (default 1.5x ATR / 2.5x ATR, 50-bar max hold) and
walks forward until SL, TP, or timeout.

Usage:
    python -m scripts.setup_signal_backtest [--days N] [--pairs P1 P2 ...]
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtester.data_fetcher import fetch_candles
from backtester.indicators import compute_all
from backtester.sniper_v4 import add_enhanced_indicators
from optimizer.replay import candle_walk_replay, TradeSnapshot

import numpy as np


# ── Production snipe gate models (from agents/trading_cycle.py) ──
def gate_fan_exhaustion(df: pd.DataFrame, i: int, direction: str) -> Optional[str]:
    """Block if fan state NOT in (expanding, accelerating, just_crossed)."""
    if i < 25:
        return None
    look = df.iloc[max(0, i-25):i+1]
    e21 = look.get("ema_21")
    e55 = look.get("ema_55")
    if e21 is None or e55 is None or e21.isna().any() or e55.isna().any():
        return None
    sep = abs(e21 - e55).values
    if len(sep) < 6:
        return None
    sep_now = sep[-1]
    sep_3b = sep[-4]
    sep_5b = sep[-6]
    # Approximate fan_state:
    # expanding: sep_now > sep_3b > sep_5b (monotonic widening)
    # accelerating: sep_now > sep_3b * 1.1 (rapid widening)
    # just_crossed: e21 just changed side vs e55 within last 3 bars
    s55 = look["ema_55"].values
    s21 = look["ema_21"].values
    just_crossed = False
    for k in range(1, min(4, len(s21))):
        if (s21[-k-1] - s55[-k-1]) * (s21[-1] - s55[-1]) < 0:
            just_crossed = True
            break
    expanding = sep_now > sep_3b > sep_5b
    accelerating = sep_now > sep_3b * 1.1
    is_active = expanding or accelerating or just_crossed
    if not is_active:
        return "fan_exhaustion"
    return None


def gate_ema_ordering_conflict(df: pd.DataFrame, i: int, direction: str) -> Optional[str]:
    """Block if EMA21/55/100 ordering doesn't match snipe direction."""
    r = df.iloc[i]
    e21, e55, e100 = r.get("ema_21"), r.get("ema_55"), r.get("ema_100")
    if any(pd.isna(x) for x in (e21, e55, e100)):
        return None
    bullish = e21 > e55 > e100
    bearish = e21 < e55 < e100
    if direction == "buy" and bearish:
        return "ema_ordering_conflict"
    if direction == "sell" and bullish:
        return "ema_ordering_conflict"
    return None


def gate_validator_fan_alignment(df: pd.DataFrame, i: int, direction: str,
                                 lookback: int = 12, rise_n: int = 3, rev_k: int = 6) -> Optional[str]:
    """Block at_peak/post_peak/fan_reversed + entry candle reversed direction."""
    if i < 60:
        return None
    look = df.iloc[i-60:i+1]
    e21_arr = look["ema_21"].values
    e55_arr = look["ema_55"].values
    if np.isnan(e21_arr[-1]) or np.isnan(e55_arr[-1]):
        return None
    fan_signed = (e21_arr - e55_arr)
    fan_sep = np.abs(fan_signed)
    if len(fan_sep) < lookback or np.any(np.isnan(fan_sep[-lookback:])):
        return None
    window = fan_sep[-lookback:]
    peak_idx = int(np.argmax(window))
    peak_val = float(window[peak_idx])
    cur_val = float(window[-1])
    bars_since_peak = (lookback - 1) - peak_idx
    rising = len(window) > rise_n and window[-1] > window[-1 - rise_n]
    at_peak = bars_since_peak == 0 and rising
    post_peak = (1 <= bars_since_peak <= lookback - 2) and (cur_val < peak_val)
    reversed_ = False
    if len(fan_signed) >= rev_k + 1:
        signs = np.sign(fan_signed[-rev_k - 1:])
        reversed_ = bool(np.any(signs[:-1] * signs[-1] < 0))
    structural = at_peak or post_peak or reversed_
    if not structural:
        return None
    r = df.iloc[i]
    entry_green = r["close"] > r["open"]
    entry_red = r["close"] < r["open"]
    candle_warns = (direction == "sell" and entry_green) or (direction == "buy" and entry_red)
    if candle_warns:
        return "validator_fan_alignment"
    return None


def gate_counter_momentum(df: pd.DataFrame, i: int, direction: str, pair: str = "EUR_USD", min_score: int = 2) -> Optional[str]:
    """Multi-indicator pre-entry score 0-5; block if < min_score (default 2)."""
    if i < 24:
        return None
    look = df.iloc[i-23:i+1]
    closes = look["close"].values
    r = df.iloc[i]
    is_long = direction == "buy"
    # C1: candle color aligned
    c_open, c_close = r["open"], r["close"]
    if c_close > c_open:
        c_color = "GREEN"
    elif c_close < c_open:
        c_color = "RED"
    else:
        c_color = "DOJI"
    c1 = (is_long and c_color == "GREEN") or (not is_long and c_color == "RED")
    # C2: 3-bar price extension aligned
    if len(closes) >= 4:
        ext = closes[-1] - closes[-4]
        c2 = (is_long and ext > 0) or (not is_long and ext < 0)
    else:
        c2 = False
    # C3: stoch aligned + trending further
    stoch_now = r.get("stoch_k", 50)
    stoch_prev = df.iloc[i-1].get("stoch_k", 50) if i >= 1 else 50
    if is_long:
        c3 = stoch_now >= 55 and stoch_now >= stoch_prev
    else:
        c3 = stoch_now <= 45 and stoch_now <= stoch_prev
    # C4: BB width expanding 3 bars
    bb_w_now = r.get("bb_width", 0)
    bb_w_p1 = df.iloc[i-1].get("bb_width", 0) if i >= 1 else 0
    bb_w_p2 = df.iloc[i-2].get("bb_width", 0) if i >= 2 else 0
    c4 = bool(bb_w_now > bb_w_p1 and bb_w_p1 > bb_w_p2 * 0.98)
    # C5: price ≥5 pips from E21 in direction
    e21_now = r.get("ema_21")
    pair_pip = 0.01 if "JPY" in pair.upper() else 0.0001
    if e21_now and not pd.isna(e21_now):
        pos_e21 = (r["close"] - e21_now) / pair_pip
        c5 = (is_long and pos_e21 >= 5.0) or (not is_long and pos_e21 <= -5.0)
    else:
        c5 = False
    score = int(c1) + int(c2) + int(c3) + int(c4) + int(c5)
    if score < min_score:
        return f"counter_momentum_{score}of5"
    return None


def apply_snipe_gates(df: pd.DataFrame, i: int, direction: str, pair: str) -> Optional[str]:
    """Run the deterministic snipe gates in production order. Return blocking
    gate name on first block, or None if all pass.

    Gates modeled (deterministic only — LLM validator not modeled):
      - ema_ordering_conflict
      - validator_fan_alignment (newest, 2026-04-28)
      - fan_exhaustion (most blocking)
      - counter_momentum
    """
    blk = gate_ema_ordering_conflict(df, i, direction)
    if blk:
        return blk
    blk = gate_validator_fan_alignment(df, i, direction)
    if blk:
        return blk
    blk = gate_fan_exhaustion(df, i, direction)
    if blk:
        return blk
    blk = gate_counter_momentum(df, i, direction, pair=pair, min_score=2)
    if blk:
        return blk
    return None


# Production guardian params (from active tuning_overrides as of 2026-04-29).
# Used by candle_walk_replay to simulate exact live exit behavior.
PRODUCTION_GUARDIAN_PARAMS = {
    "gate.sl_atr_mult": 2.5,            # snipe.sl_atr_mult overrides gate
    "gate.tp_atr_mult": 1.5,            # gate.tp_atr_mult active override
    "guardian.profit_floor_5p": 0.70,
    "guardian.profit_floor_8p": 0.80,
    "guardian.profit_floor_12p": 0.90,
    "guardian.profit_floor_20p": 0.95,
    "guardian.ratchet_step_pips": 3.67,
    "guardian.sl_buffer_pips": 1.0,
    "guardian.trailing_activation_rr": 0.20,
    "guardian.trailing_atr_mult": 0.30,
}

logging.basicConfig(level=logging.WARNING, format='%(message)s')
logger = logging.getLogger(__name__)

ALL_PAIRS = [
    "AUD_JPY", "AUD_USD", "EUR_AUD", "EUR_CHF", "EUR_GBP", "EUR_JPY",
    "EUR_USD", "GBP_AUD", "GBP_JPY", "GBP_USD", "NZD_USD", "USD_CAD",
    "USD_CHF", "USD_JPY",
]


def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def candles_to_df(candles: list) -> pd.DataFrame:
    rows = []
    for c in candles:
        if not c.get("complete", True):
            continue
        m = c["mid"]
        rows.append({
            "time": c["time"],
            "open": float(m["o"]),
            "high": float(m["h"]),
            "low": float(m["l"]),
            "close": float(m["c"]),
            "volume": int(c.get("volume", 0)),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Candidate detectors. Each returns ('buy'|'sell'|None) for the given bar.
# A detector inspects df.iloc[i] and prior bars; it must NOT look forward.
# --------------------------------------------------------------------------

def C0_TRADE_NOW_SIGNATURE(df: pd.DataFrame, i: int) -> Optional[str]:
    """Control: tight reproduction of the 4 TRADE_NOW winners.

    Required: prior 3 bars ALSO in bearish-fan-expanding state (durable expansion),
    bearish entry candle, RSI 20-50, Stoch_K < 25, ATR meaningful (real volatility),
    fan separation accelerating vs 5 bars ago (>1.15x).
    """
    if i < 8:
        return None
    r = df.iloc[i]
    e21, e55, e100 = r.get("ema_21"), r.get("ema_55"), r.get("ema_100")
    if any(pd.isna(x) for x in (e21, e55, e100)):
        return None
    if not (e21 < e55 < e100):
        return None
    sep_now = (e100 - e21) / r["close"]
    r5 = df.iloc[i-5]
    sep_5b = (r5.get("ema_100", e100) - r5.get("ema_21", e21)) / r5["close"]
    if sep_5b <= 0 or sep_now / sep_5b < 1.15:
        return None
    # Prior 3 bars must already be in ordered bearish fan
    for k in (1, 2, 3):
        rk = df.iloc[i-k]
        if not (rk.get("ema_21", 0) < rk.get("ema_55", 0) < rk.get("ema_100", 0)):
            return None
    rsi = r.get("rsi", 50)
    stoch = r.get("stoch_k", 50)
    bearish_candle = r["close"] < r["open"]
    if not (bearish_candle and 20 <= rsi <= 50 and stoch < 25):
        return None
    return "sell"


def C1_STOCH_EXTREME_BB(df: pd.DataFrame, i: int) -> Optional[str]:
    """Stoch crossing back from extreme (not just at extreme) + BB band touch + ranging.

    Requires: Stoch was at extreme on prior bar AND now turning back (cross),
    price at BB band, ADX < 22 (truly ranging), BB width meaningful.
    """
    if i < 2:
        return None
    r = df.iloc[i]
    p1 = df.iloc[i-1]
    stoch_now = r.get("stoch_k", 50)
    stoch_prev = p1.get("stoch_k", 50)
    adx = r.get("adx", 25)
    bb_u, bb_l = r.get("bb_upper"), r.get("bb_lower")
    bb_w = r.get("bb_width", 0)
    if pd.isna(bb_u) or pd.isna(bb_l) or adx >= 22 or not bb_w:
        return None
    atr = r.get("atr", 0)
    if not atr or bb_w < 3 * atr:
        return None
    close = r["close"]
    high_prev = p1["high"]
    low_prev = p1["low"]
    # Sell: prior bar had stoch>=80 AND poked upper BB, now stoch turning down
    if stoch_prev >= 80 and high_prev >= bb_u * 0.999 and stoch_now < stoch_prev and r["close"] < r["open"]:
        return "sell"
    # Buy: mirror
    if stoch_prev <= 20 and low_prev <= bb_l * 1.001 and stoch_now > stoch_prev and r["close"] > r["open"]:
        return "buy"
    return None


def C2_FRESH_CROSS(df: pd.DataFrame, i: int) -> Optional[str]:
    """E21×E55 cross 1-2 bars ago + E100 alignment + ADX rising + same-dir candle.

    Stronger: require E21 also crossing/crossed E100 within 5 bars (full fan flip),
    ADX > 20 and rising, same-direction candle on entry bar.
    """
    if i < 6:
        return None
    r = df.iloc[i]
    p1 = df.iloc[i-1]
    cross_up = bool(r.get("ema_21_cross_55_up") or p1.get("ema_21_cross_55_up"))
    cross_dn = bool(r.get("ema_21_cross_55_down") or p1.get("ema_21_cross_55_down"))
    e100_recent_up = bool(r.get("e21_crossed_100_recently_bull"))
    e100_recent_dn = bool(r.get("e21_crossed_100_recently_bear"))
    adx = r.get("adx", 0)
    adx_prev = df.iloc[i-3].get("adx", 0)
    if adx <= 20 or adx <= adx_prev:
        return None
    bullish_candle = r["close"] > r["open"] and (r["close"] - r["open"]) > 0.3 * (r["high"] - r["low"])
    bearish_candle = r["close"] < r["open"] and (r["open"] - r["close"]) > 0.3 * (r["high"] - r["low"])
    if cross_up and e100_recent_up and bullish_candle:
        return "buy"
    if cross_dn and e100_recent_dn and bearish_candle:
        return "sell"
    return None


def C3_RSI_DIV_GOLDEN(df: pd.DataFrame, i: int) -> Optional[str]:
    """RSI divergence proxy: price extreme vs RSI relaxed, at BB band, ADX declining from >25.

    Live scout has a precomputed divergence flag we don't recreate here. Instead use
    a 10-bar rolling proxy: price makes new HH/LL but RSI doesn't confirm.
    """
    if i < 12:
        return None
    r = df.iloc[i]
    look = df.iloc[i-10:i+1]
    bb_u, bb_l = r.get("bb_upper"), r.get("bb_lower")
    adx = r.get("adx", 25)
    adx_prev = df.iloc[i-3].get("adx", 25)
    if pd.isna(bb_u) or pd.isna(bb_l):
        return None
    declining = adx < adx_prev and adx_prev > 25
    if not declining:
        return None
    # Bearish div: new HH but RSI lower than prior HH
    is_new_hh = r["high"] >= look["high"].max()
    prior_high_idx = look["high"].iloc[:-1].idxmax()
    rsi_now, rsi_prior = r.get("rsi", 50), df.iloc[prior_high_idx].get("rsi", 50)
    near_upper = r["close"] >= bb_u * 0.998
    if is_new_hh and rsi_now < rsi_prior and near_upper:
        return "sell"
    # Bullish div: new LL but RSI higher than prior LL
    is_new_ll = r["low"] <= look["low"].min()
    prior_low_idx = look["low"].iloc[:-1].idxmin()
    rsi_prior_l = df.iloc[prior_low_idx].get("rsi", 50)
    near_lower = r["close"] <= bb_l * 1.002
    if is_new_ll and rsi_now > rsi_prior_l and near_lower:
        return "buy"
    return None


def C4_CHART_PATTERN_BREAK(df: pd.DataFrame, i: int) -> Optional[str]:
    """Light-weight double-top/bottom break.

    Look back 30 bars: if there are 2 prior peaks within 0.2*ATR of each other
    and current bar closes below the trough between them, sell. Mirror for buy.
    """
    if i < 30:
        return None
    r = df.iloc[i]
    atr = r.get("atr", 0)
    if not atr or atr <= 0:
        return None
    look = df.iloc[i-30:i]
    highs = look["high"].values
    lows = look["low"].values
    closes = look["close"].values
    # Double top
    top_idx = highs.argmax()
    masked_h = highs.copy()
    # Window-out 5 bars around top to find second peak
    lo, hi = max(0, top_idx-2), min(len(highs), top_idx+3)
    masked_h[lo:hi] = -1e9
    second_top_idx = masked_h.argmax()
    if abs(highs[top_idx] - highs[second_top_idx]) < 0.3 * atr and abs(top_idx - second_top_idx) >= 5:
        trough = lows[min(top_idx, second_top_idx):max(top_idx, second_top_idx)].min()
        if r["close"] < trough:
            return "sell"
    # Double bottom
    bot_idx = lows.argmin()
    masked_l = lows.copy()
    lo, hi = max(0, bot_idx-2), min(len(lows), bot_idx+3)
    masked_l[lo:hi] = 1e9
    second_bot_idx = masked_l.argmin()
    if abs(lows[bot_idx] - lows[second_bot_idx]) < 0.3 * atr and abs(bot_idx - second_bot_idx) >= 5:
        peak = highs[min(bot_idx, second_bot_idx):max(bot_idx, second_bot_idx)].max()
        if r["close"] > peak:
            return "buy"
    return None


def C5_FIB_REACTION(df: pd.DataFrame, i: int) -> Optional[str]:
    """Price at 38.2/50/61.8 fib of last swing + reversal candle + EMA-21 alignment.

    Direction comes from EMA21 vs EMA100 (more responsive than sma50 slope).
    """
    if i < 31:
        return None
    r = df.iloc[i]
    p1 = df.iloc[i-1]
    look = df.iloc[i-30:i]
    swing_high = look["high"].max()
    swing_low = look["low"].min()
    diff = swing_high - swing_low
    atr = r.get("atr", 0)
    if diff <= 0 or not atr:
        return None
    fib_382 = swing_high - 0.382 * diff
    fib_500 = swing_high - 0.500 * diff
    fib_618 = swing_high - 0.618 * diff
    proximity = 0.5 * atr
    at_fib = (
        abs(r["close"] - fib_382) < proximity
        or abs(r["close"] - fib_500) < proximity
        or abs(r["close"] - fib_618) < proximity
    )
    if not at_fib:
        return None
    bullish_reversal = r["close"] > r["open"] and r["close"] > p1["high"]
    bearish_reversal = r["close"] < r["open"] and r["close"] < p1["low"]
    e21, e100 = r.get("ema_21"), r.get("ema_100")
    if pd.isna(e21) or pd.isna(e100):
        return None
    uptrend = e21 > e100
    downtrend = e21 < e100
    if uptrend and bullish_reversal:
        return "buy"
    if downtrend and bearish_reversal:
        return "sell"
    return None


def C6_SMA_BREAKOUT(df: pd.DataFrame, i: int) -> Optional[str]:
    """Price clears SMA50 AND SMA100 + MACD aligned + ADX>25 (S11)."""
    if i < 1:
        return None
    r = df.iloc[i]
    p1 = df.iloc[i-1]
    sma50, sma100 = r.get("sma_50"), r.get("sma_100")
    if pd.isna(sma50) or pd.isna(sma100):
        return None
    macd_h = r.get("macd_histogram", 0)
    adx = r.get("adx", 0)
    if adx <= 25:
        return None
    above_now = r["close"] > sma50 and r["close"] > sma100
    above_prev = p1["close"] > p1.get("sma_50", 1e9) and p1["close"] > p1.get("sma_100", 1e9)
    below_now = r["close"] < sma50 and r["close"] < sma100
    below_prev = p1["close"] < p1.get("sma_50", -1e9) and p1["close"] < p1.get("sma_100", -1e9)
    # Just-cleared = above now but not above_prev
    if above_now and not above_prev and macd_h > 0:
        return "buy"
    if below_now and not below_prev and macd_h < 0:
        return "sell"
    return None


def C7_MACD_RSI_EXTREME(df: pd.DataFrame, i: int) -> Optional[str]:
    """MACD histogram zero-cross + RSI extreme (S2)."""
    if i < 1:
        return None
    r = df.iloc[i]
    cross_bull = bool(r.get("macd_cross_bull"))
    cross_bear = bool(r.get("macd_cross_bear"))
    rsi = r.get("rsi", 50)
    if cross_bull and rsi < 35:
        return "buy"
    if cross_bear and rsi > 65:
        return "sell"
    return None


def C8_TRIANGLE_BREAKOUT(df: pd.DataFrame, i: int) -> Optional[str]:
    """Tightening range (last 20 bars) + breakout close beyond range bounds."""
    if i < 21:
        return None
    r = df.iloc[i]
    look = df.iloc[i-20:i]
    high_max = look["high"].max()
    low_min = look["low"].min()
    rng = high_max - low_min
    atr = r.get("atr", 0)
    if not atr or rng > 6 * atr:
        return None  # not consolidating
    # Earlier half range vs later half — must be tightening
    early = df.iloc[i-20:i-10]
    late = df.iloc[i-10:i]
    early_rng = early["high"].max() - early["low"].min()
    late_rng = late["high"].max() - late["low"].min()
    if late_rng >= early_rng * 0.85:
        return None  # not tightening
    if r["close"] > high_max:
        return "buy"
    if r["close"] < low_min:
        return "sell"
    return None


def C9_BEARISH_EXPANSION_PULLBACK(df: pd.DataFrame, i: int) -> Optional[str]:
    """The dominant live-winner pattern: durable bearish fan + small bullish pullback bar
    + entry on next bearish bar that closes back below E21.
    """
    if i < 8:
        return None
    r = df.iloc[i]
    p1 = df.iloc[i-1]
    e21, e55, e100 = r.get("ema_21"), r.get("ema_55"), r.get("ema_100")
    if any(pd.isna(x) for x in (e21, e55, e100)):
        return None
    # Sell side
    if e21 < e55 < e100:
        # Prior was a small green pullback NOT taking out e21 from below
        prev_bull = p1["close"] > p1["open"] and (p1["close"] - p1["open"]) < 0.6 * (p1["high"] - p1["low"])
        if prev_bull and p1["high"] <= e21 * 1.0010 and r["close"] < r["open"] and r["close"] < e21:
            return "sell"
    # Buy side mirror (rare but symmetrical)
    if e21 > e55 > e100:
        prev_bear = p1["close"] < p1["open"] and (p1["open"] - p1["close"]) < 0.6 * (p1["high"] - p1["low"])
        if prev_bear and p1["low"] >= e21 * 0.9990 and r["close"] > r["open"] and r["close"] > e21:
            return "buy"
    return None


def C10_RSI_DIV_TIGHT(df: pd.DataFrame, i: int) -> Optional[str]:
    """Tight RSI divergence: at least 8 bars between extremes, ADX falling from >25,
    price still in fan direction but RSI weakening.
    """
    if i < 20:
        return None
    r = df.iloc[i]
    look = df.iloc[i-20:i+1]
    adx = r.get("adx", 25)
    adx_prev = df.iloc[i-5].get("adx", 25)
    if adx >= adx_prev or adx_prev <= 25:
        return None
    rsi_now = r.get("rsi", 50)
    # Bearish divergence — recent local high in look that's >= 8 bars back
    high_idx = look["high"].iloc[:-3].idxmax()
    bars_back = i - high_idx
    if bars_back >= 8 and r["high"] > look.iloc[:-1]["high"].max() * 0.9995:
        rsi_at_prev_high = df.iloc[high_idx].get("rsi", 50)
        if rsi_now < rsi_at_prev_high - 3 and r["close"] < r["open"]:
            return "sell"
    low_idx = look["low"].iloc[:-3].idxmin()
    bars_back = i - low_idx
    if bars_back >= 8 and r["low"] < look.iloc[:-1]["low"].min() * 1.0005:
        rsi_at_prev_low = df.iloc[low_idx].get("rsi", 50)
        if rsi_now > rsi_at_prev_low + 3 and r["close"] > r["open"]:
            return "buy"
    return None


def C11_JPY_BIG_MOVE(df: pd.DataFrame, i: int) -> Optional[str]:
    """JPY-specific: ordered fan + ADX > 28 + MACD aligned + entry candle closes
    in fan direction. JPY pairs had biggest live winners (+27p, +17p, +14p)."""
    if i < 5:
        return None
    r = df.iloc[i]
    e21, e55, e100 = r.get("ema_21"), r.get("ema_55"), r.get("ema_100")
    adx = r.get("adx", 0)
    macd_h = r.get("macd_histogram", 0)
    if any(pd.isna(x) for x in (e21, e55, e100)) or adx < 28:
        return None
    if e21 > e55 > e100 and macd_h > 0 and r["close"] > r["open"] and r["close"] > e21:
        return "buy"
    if e21 < e55 < e100 and macd_h < 0 and r["close"] < r["open"] and r["close"] < e21:
        return "sell"
    return None


DETECTORS = {
    "C0_TRADE_NOW_SIG": C0_TRADE_NOW_SIGNATURE,
    "C1_STOCH_EXTREME_BB": C1_STOCH_EXTREME_BB,
    "C2_FRESH_CROSS": C2_FRESH_CROSS,
    "C3_RSI_DIV_GOLDEN": C3_RSI_DIV_GOLDEN,
    "C4_CHART_PATTERN_BREAK": C4_CHART_PATTERN_BREAK,
    "C5_FIB_REACTION": C5_FIB_REACTION,
    "C6_SMA_BREAKOUT": C6_SMA_BREAKOUT,
    "C7_MACD_RSI_EXTREME": C7_MACD_RSI_EXTREME,
    "C8_TRIANGLE_BREAKOUT": C8_TRIANGLE_BREAKOUT,
    "C9_BEAR_EXP_PULLBACK": C9_BEARISH_EXPANSION_PULLBACK,
    "C10_RSI_DIV_TIGHT": C10_RSI_DIV_TIGHT,
    "C11_JPY_BIG_MOVE": C11_JPY_BIG_MOVE,
}


def simulate_trade(df: pd.DataFrame, entry_idx: int, direction: str, pair: str,
                   sl_atr_mult: float = 1.5, tp_atr_mult: float = 2.5,
                   max_hold_bars: int = 50) -> Optional[dict]:
    """Walk forward from entry_idx and mark win/loss/timeout (static SL/TP)."""
    if entry_idx >= len(df) - 1:
        return None
    entry = df.iloc[entry_idx]
    atr = entry.get("atr", 0)
    if not atr or atr <= 0:
        return None
    pip = pip_size(pair)
    entry_price = float(entry["close"])
    is_buy = direction == "buy"
    if is_buy:
        sl = entry_price - sl_atr_mult * atr
        tp = entry_price + tp_atr_mult * atr
    else:
        sl = entry_price + sl_atr_mult * atr
        tp = entry_price - tp_atr_mult * atr

    end_idx = min(entry_idx + 1 + max_hold_bars, len(df))
    for j in range(entry_idx + 1, end_idx):
        bar = df.iloc[j]
        high, low = float(bar["high"]), float(bar["low"])
        if is_buy:
            if low <= sl:
                return {"outcome": "loss", "pips": (sl - entry_price) / pip, "bars": j - entry_idx}
            if high >= tp:
                return {"outcome": "win", "pips": (tp - entry_price) / pip, "bars": j - entry_idx}
        else:
            if high >= sl:
                return {"outcome": "loss", "pips": (entry_price - sl) / pip, "bars": j - entry_idx}
            if low <= tp:
                return {"outcome": "win", "pips": (entry_price - tp) / pip, "bars": j - entry_idx}
    final = df.iloc[end_idx - 1]
    pnl = (float(final["close"]) - entry_price) / pip * (1 if is_buy else -1)
    return {"outcome": "timeout", "pips": pnl, "bars": end_idx - 1 - entry_idx}


def simulate_trade_guardian(df: pd.DataFrame, entry_idx: int, direction: str, pair: str,
                            max_hold_bars: int = 50,
                            params: dict = PRODUCTION_GUARDIAN_PARAMS) -> Optional[dict]:
    """Use the actual production candle_walk_replay with live guardian params.

    Builds a TradeSnapshot for the synthetic entry and feeds forward bars to
    the same replay engine the optimizer uses. Output mapped to our schema.
    """
    if entry_idx >= len(df) - 1:
        return None
    entry = df.iloc[entry_idx]
    atr_raw = entry.get("atr", 0)
    if not atr_raw or atr_raw <= 0:
        return None
    entry_price = float(entry["close"])
    pip = pip_size(pair)

    # Build minimal TradeSnapshot. Most fields unused by candle_walk_replay
    # except: pair, direction, entry_price, atr, pnl_pips (used for pips_saved
    # comparison only — set to 0 since this is a synthetic entry).
    snap = TradeSnapshot(
        id=f"synth_{entry_idx}",
        pair=pair,
        direction=direction,
        outcome="",
        pnl_pips=0.0,
        realized_pl=0.0,
        fan_state=str(entry.get("fan_state") or ""),
        bb_width=float(entry.get("bb_width") or 0) or None,
        rsi=float(entry.get("rsi") or 50),
        stoch_k=float(entry.get("stoch_k") or 50),
        story_score=None,
        atr=float(atr_raw),
        confidence=None,
        entry_price=entry_price,
        sl_price=None,
        tp_price=None,
        mfe=None,
        mae=None,
        session=None,
    )

    end_idx = min(entry_idx + 1 + max_hold_bars, len(df))
    forward = df.iloc[entry_idx + 1:end_idx][["time", "open", "high", "low", "close"]].copy()
    if len(forward) < 2:
        return None

    result = candle_walk_replay(snap, forward, params, reaction_delay_bars=1)

    pnl = result.get("simulated_pnl", 0.0)
    out = result.get("simulated_outcome", "")
    if out not in ("win", "loss"):
        out = "win" if pnl > 0 else "loss" if pnl < 0 else "timeout"
    return {
        "outcome": out,
        "pips": float(pnl),
        "bars": int(result.get("bars_held", 0)),
        "peak_pips": float(result.get("peak_pips", 0)),
        "exit_reason": result.get("exit_reason", ""),
    }


def simulate_trade_dynamic(df: pd.DataFrame, entry_idx: int, direction: str, pair: str,
                           sl_atr_mult: float = 1.5, max_hold_bars: int = 50) -> Optional[dict]:
    """Guardian-style dynamic exit: profit-floor ratchet + trail.

    - Initial hard SL = entry ± 1.5×ATR
    - Track peak favorable pips
    - Once peak >= 5p:  lock 30% of peak (move SL to entry ± 0.30*peak)
    - Once peak >= 8p:  lock 50%
    - Once peak >= 12p: lock 60%
    - Once peak >= 20p: lock 70%
    - Trail: SL never widens, only tightens
    """
    if entry_idx >= len(df) - 1:
        return None
    entry = df.iloc[entry_idx]
    atr = entry.get("atr", 0)
    if not atr or atr <= 0:
        return None
    pip = pip_size(pair)
    entry_price = float(entry["close"])
    is_buy = direction == "buy"
    sl = entry_price - sl_atr_mult * atr if is_buy else entry_price + sl_atr_mult * atr
    peak_pips = 0.0
    end_idx = min(entry_idx + 1 + max_hold_bars, len(df))

    for j in range(entry_idx + 1, end_idx):
        bar = df.iloc[j]
        high, low = float(bar["high"]), float(bar["low"])
        # Update peak favorable
        if is_buy:
            cur_high_pips = (high - entry_price) / pip
            cur_low_pips = (low - entry_price) / pip
            peak_pips = max(peak_pips, cur_high_pips)
        else:
            cur_high_pips = (entry_price - low) / pip
            cur_low_pips = (entry_price - high) / pip
            peak_pips = max(peak_pips, cur_high_pips)
        # Apply ratchet — compute lock pct from current peak
        if peak_pips >= 20:
            lock_pct = 0.70
        elif peak_pips >= 12:
            lock_pct = 0.60
        elif peak_pips >= 8:
            lock_pct = 0.50
        elif peak_pips >= 5:
            lock_pct = 0.30
        else:
            lock_pct = 0.0
        if lock_pct > 0:
            lock_pips = peak_pips * lock_pct
            new_sl = entry_price + lock_pips * pip if is_buy else entry_price - lock_pips * pip
            if is_buy:
                sl = max(sl, new_sl)
            else:
                sl = min(sl, new_sl)
        # Check SL hit
        if is_buy:
            if low <= sl:
                exit_pips = (sl - entry_price) / pip
                outcome = "win" if exit_pips > 0 else "loss"
                return {"outcome": outcome, "pips": exit_pips, "bars": j - entry_idx, "peak_pips": peak_pips}
        else:
            if high >= sl:
                exit_pips = (entry_price - sl) / pip
                outcome = "win" if exit_pips > 0 else "loss"
                return {"outcome": outcome, "pips": exit_pips, "bars": j - entry_idx, "peak_pips": peak_pips}
    # Timeout
    final = df.iloc[end_idx - 1]
    pnl = (float(final["close"]) - entry_price) / pip * (1 if is_buy else -1)
    outcome = "win" if pnl > 0 else "loss" if pnl < 0 else "timeout"
    return {"outcome": outcome, "pips": pnl, "bars": end_idx - 1 - entry_idx, "peak_pips": peak_pips}


def run_pair_with_gates(pair: str, days: int, apply_gates: bool = True) -> Dict[str, dict]:
    """Run all detectors AND apply production snipe gates. Records gate
    block reason per fire so we see funnel: detected → gate_pass → simulated.
    """
    to_t = datetime.now(timezone.utc)
    from_t = to_t - timedelta(days=days)
    candles = fetch_candles(pair, "M15", from_t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            to_t.strftime("%Y-%m-%dT%H:%M:%SZ"))
    df = candles_to_df(candles)
    if df.empty or len(df) < 100:
        print(f"  {pair}: insufficient data ({len(df)} bars)")
        return {}
    df = compute_all(df)
    df = add_enhanced_indicators(df)

    cooldown = defaultdict(int)
    results = defaultdict(lambda: {
        "trades_passed": [], "fires_total": 0, "blocked_by": defaultdict(int),
    })

    for i in range(35, len(df) - 5):
        for name, fn in DETECTORS.items():
            if i < cooldown[name]:
                continue
            try:
                direction = fn(df, i)
            except Exception:
                direction = None
            if not direction:
                continue
            results[name]["fires_total"] += 1
            cooldown[name] = i + 8

            if apply_gates:
                gate_block = apply_snipe_gates(df, i, direction, pair)
                if gate_block:
                    # Strip param values from gate name for grouping
                    gate_key = gate_block.split("_")[0] if gate_block.startswith("counter_momentum") else gate_block
                    results[name]["blocked_by"][gate_block] += 1
                    continue

            trade = simulate_trade_guardian(df, i, direction, pair)
            if trade:
                trade["pair"] = pair
                trade["direction"] = direction
                trade["entry_bar_idx"] = i
                results[name]["trades_passed"].append(trade)
    return results


def run_pair(pair: str, days: int, exit_mode: str = "guardian") -> Dict[str, dict]:
    """Run all detectors over historical M15 candles for one pair.

    exit_mode:
        'static'   = fixed 1.5/2.5 ATR (debug)
        'dynamic'  = simplified ratchet (debug)
        'guardian' = real candle_walk_replay with production params (USE THIS)
    """
    if exit_mode == "guardian":
        sim_fn = simulate_trade_guardian
    elif exit_mode == "dynamic":
        sim_fn = simulate_trade_dynamic
    else:
        sim_fn = simulate_trade
    to_t = datetime.now(timezone.utc)
    from_t = to_t - timedelta(days=days)
    candles = fetch_candles(pair, "M15", from_t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            to_t.strftime("%Y-%m-%dT%H:%M:%SZ"))
    df = candles_to_df(candles)
    if df.empty or len(df) < 100:
        print(f"  {pair}: insufficient data ({len(df)} bars)")
        return {}
    df = compute_all(df)
    df = add_enhanced_indicators(df)

    cooldown = defaultdict(int)
    results = defaultdict(lambda: {"trades": [], "fires": 0})

    for i in range(35, len(df) - 5):
        for name, fn in DETECTORS.items():
            if i < cooldown[name]:
                continue
            try:
                direction = fn(df, i)
            except Exception:
                direction = None
            if direction:
                results[name]["fires"] += 1
                trade = sim_fn(df, i, direction, pair)
                if trade:
                    trade["pair"] = pair
                    trade["direction"] = direction
                    trade["entry_bar_idx"] = i
                    results[name]["trades"].append(trade)
                cooldown[name] = i + 8
    return results


def aggregate(per_pair_results: Dict[str, Dict[str, dict]]) -> Dict[str, dict]:
    agg = defaultdict(lambda: {"trades": []})
    for pair, by_setup in per_pair_results.items():
        for name, info in by_setup.items():
            agg[name]["trades"].extend(info["trades"])
    return agg


def overlap_report(per_pair_results: Dict[str, Dict[str, dict]], tier1: List[str]):
    """Measure how often Tier 1 detectors fire on same bar / overlapping windows.

    For each pair, build a per-bar timeline of which detectors fired (and dir).
    Reports:
      1. Bar-level overlap matrix: pairwise fires on same bar (same dir)
      2. Cluster-size distribution: how many detectors share a bar (1, 2, 3+)
      3. C0 (V4 proxy) overlap: how many Tier 1 fires are NEW vs duplicating V4
      4. 8-bar window dedup impact: per pair, how many unique events after cooldown
    """
    # Build (pair, bar) -> set of (detector, direction) firing on that bar
    bar_fires = defaultdict(lambda: defaultdict(set))  # pair -> bar_idx -> {(name, dir)}

    for pair, by_setup in per_pair_results.items():
        for name, info in by_setup.items():
            for t in info.get("trades", []):
                bar = t.get("entry_bar_idx")
                if bar is None:
                    continue
                bar_fires[pair][bar].add((name, t["direction"]))

    print()
    print("=" * 95)
    print("OVERLAP ANALYSIS — Tier 1 detectors")
    print("=" * 95)

    # 1. Bar-level cluster size distribution (Tier 1 only, count distinct same-bar fires)
    cluster_sizes_t1 = defaultdict(int)
    cluster_sizes_all = defaultdict(int)
    total_t1_fires = 0
    total_all_fires = 0
    for pair, bars in bar_fires.items():
        for bar, fires in bars.items():
            t1_fires = {(n, d) for n, d in fires if n in tier1}
            if t1_fires:
                cluster_sizes_t1[len(t1_fires)] += 1
                total_t1_fires += len(t1_fires)
            cluster_sizes_all[len(fires)] += 1
            total_all_fires += len(fires)

    print("\nBar-level cluster size — how many Tier 1 detectors fire on the same bar:")
    print(f"  {'Cluster size':<14} {'# bars':>8} {'% of bars':>10}  {'fires/bar':>10}")
    total_t1_bars = sum(cluster_sizes_t1.values())
    for size in sorted(cluster_sizes_t1.keys()):
        n_bars = cluster_sizes_t1[size]
        pct = 100 * n_bars / total_t1_bars if total_t1_bars else 0
        print(f"  {size:>13}  {n_bars:>8} {pct:>9.1f}%   {size:>10}")
    print(f"  {'TOTAL':<13}  {total_t1_bars:>8}    raw Tier-1 fires={total_t1_fires}")

    # 2. Pairwise co-fire matrix
    print("\nPairwise co-fire — when detector X fires, how often does Y fire same bar?")
    print(f"  {'X':<22}", end="")
    for n in tier1:
        print(f" {n[:8]:>9}", end="")
    print()
    co_fire = defaultdict(lambda: defaultdict(int))
    fire_count = defaultdict(int)
    for pair, bars in bar_fires.items():
        for bar, fires in bars.items():
            t1_names = {n for n, d in fires if n in tier1}
            for n in t1_names:
                fire_count[n] += 1
                for m in t1_names:
                    if m != n:
                        co_fire[n][m] += 1
    for n in tier1:
        print(f"  {n:<22}", end="")
        for m in tier1:
            if n == m:
                print(f" {'-':>9}", end="")
            else:
                cnt = co_fire[n].get(m, 0)
                pct = 100 * cnt / fire_count[n] if fire_count[n] else 0
                print(f" {pct:>7.0f}%", end="")
        print()

    # 3. C0 (V4 proxy) overlap — how often is each Tier 1 fire ALSO a C0 fire?
    print("\nC0_TRADE_NOW_SIG (V4 proxy) overlap — % of detector's fires that ALSO fire C0:")
    c0_bars = set()
    for pair, bars in bar_fires.items():
        for bar, fires in bars.items():
            if any(n == "C0_TRADE_NOW_SIG" for n, _ in fires):
                c0_bars.add((pair, bar))
    print(f"  {'Detector':<22} {'Fires':>7} {'Also C0':>9} {'% dup-V4':>10} {'% NEW':>9}")
    for n in tier1:
        fires = 0
        also_c0 = 0
        for pair, bars in bar_fires.items():
            for bar, by_pair_fires in bars.items():
                names = {nm for nm, _ in by_pair_fires}
                if n in names:
                    fires += 1
                    if (pair, bar) in c0_bars:
                        also_c0 += 1
        new_pct = 100 * (fires - also_c0) / fires if fires else 0
        dup_pct = 100 * also_c0 / fires if fires else 0
        print(f"  {n:<22} {fires:>7} {also_c0:>9} {dup_pct:>9.1f}% {new_pct:>8.1f}%")

    # 4. 8-bar window dedup — count distinct "events" after collapsing fires within ±4 bars
    print("\n8-bar window dedup — how many distinct events remain after collapsing close fires:")
    print("  (Treats any Tier-1 fire within 8 bars of another as a single event)")
    print(f"  {'Pair':<10} {'Raw fires':>10} {'Dedup events':>14} {'Reduction':>11}")
    total_raw, total_dedup = 0, 0
    for pair in sorted(bar_fires.keys()):
        bars_with_t1 = sorted([b for b, fires in bar_fires[pair].items()
                              if any(n in tier1 for n, _ in fires)])
        if not bars_with_t1:
            continue
        raw = sum(len({(n, d) for n, d in bar_fires[pair][b] if n in tier1}) for b in bars_with_t1)
        events = 0
        last_bar = -1000
        for b in bars_with_t1:
            if b - last_bar >= 8:
                events += 1
            last_bar = b
        reduction = 100 * (1 - events / raw) if raw else 0
        total_raw += raw
        total_dedup += events
        print(f"  {pair:<10} {raw:>10} {events:>14} {reduction:>10.1f}%")
    if total_raw:
        red = 100 * (1 - total_dedup / total_raw)
        print(f"  {'TOTAL':<10} {total_raw:>10} {total_dedup:>14} {red:>10.1f}%")
        print(f"\n  After dedup: ~{total_dedup / 90:.1f} events/day across {len(bar_fires)} pairs")
    print("=" * 95)


def walk_forward_report(per_pair_results: Dict[str, Dict[str, dict]], n_folds: int):
    """Group trades by their entry_bar_idx into N contiguous folds, per pair.

    Since each pair has its own bar count, fold split happens per-pair then
    aggregated by fold number. Reports each detector's per-fold stats.
    """
    # Build fold index: trade has 'fold' key based on its entry_bar_idx within its pair
    fold_stats = defaultdict(lambda: defaultdict(list))  # detector -> fold_n -> [trades]

    for pair, by_setup in per_pair_results.items():
        # Determine pair's bar count from any detector's trades (use max bar idx + 1 ~ total bars)
        max_bar = 0
        for info in by_setup.values():
            for t in info.get("trades", []):
                if t.get("entry_bar_idx", 0) > max_bar:
                    max_bar = t["entry_bar_idx"]
        if max_bar < n_folds:
            continue
        fold_size = (max_bar + 1) / n_folds
        for name, info in by_setup.items():
            for t in info.get("trades", []):
                fold = min(int(t.get("entry_bar_idx", 0) / fold_size), n_folds - 1)
                fold_stats[name][fold].append(t)

    print()
    print("=" * 110)
    print(f"WALK-FORWARD ANALYSIS — {n_folds} folds (each ~{60//n_folds} days OOS slice)")
    print("=" * 110)
    header = f"{'Setup':<22}"
    for f in range(n_folds):
        header += f" F{f}_WR  F{f}_avg"
    header += f"  {'mean_WR':>8} {'sd_WR':>6} {'mean_avg':>9} {'min_avg':>8}  Verdict"
    print(header)
    print("-" * 110)

    verdicts = {}
    for name in sorted(fold_stats.keys()):
        row = f"{name:<22}"
        wrs = []
        avgs = []
        for f in range(n_folds):
            trades = fold_stats[name].get(f, [])
            n = len(trades)
            if n == 0:
                row += f"   --     --   "
                continue
            wins = sum(1 for t in trades if t["outcome"] == "win")
            wr = 100 * wins / n
            avg = sum(t["pips"] for t in trades) / n
            wrs.append(wr)
            avgs.append(avg)
            row += f" {wr:>4.0f}% {avg:>+5.1f}p"
        if not wrs:
            print(row + "  no data")
            continue
        mean_wr = sum(wrs) / len(wrs)
        sd_wr = (sum((w - mean_wr)**2 for w in wrs) / len(wrs)) ** 0.5
        mean_avg = sum(avgs) / len(avgs)
        min_avg = min(avgs)
        # Stability gates
        stable = (
            mean_wr >= 70
            and sd_wr <= 10
            and mean_avg > 0
            and sum(1 for a in avgs if a < 0) <= 1
            and len(wrs) >= n_folds - 1  # had data in at least n-1 folds
        )
        verdict = "STABLE" if stable else "weak"
        verdicts[name] = {"stable": stable, "mean_wr": mean_wr, "sd_wr": sd_wr,
                          "mean_avg": mean_avg, "min_avg": min_avg, "n_folds_active": len(wrs)}
        row += f"  {mean_wr:>6.1f}% {sd_wr:>5.1f}  {mean_avg:>+7.2f}p {min_avg:>+6.2f}p  {verdict}"
        print(row)
    print("=" * 110)
    print("Stability gates: mean_WR>=70%, sd_WR<=10pp, mean_avg>0, max 1 negative fold, active in n-1 folds\n")
    print("Verdict summary:")
    stable = [n for n, v in verdicts.items() if v["stable"]]
    weak = [n for n, v in verdicts.items() if not v["stable"]]
    print(f"  STABLE ({len(stable)}): {', '.join(sorted(stable)) or '(none)'}")
    print(f"  weak   ({len(weak)}): {', '.join(sorted(weak)) or '(none)'}")
    return verdicts


def report(agg: Dict[str, dict]):
    print()
    print("=" * 100)
    print(f"{'Setup':<24} {'N':>5} {'Wins':>5} {'WR%':>6} {'AvgPip':>8} {'TotPip':>9} {'PF':>6} {'Expect':>8} {'TO%':>6}")
    print("-" * 100)
    rows = []
    for name in sorted(agg.keys()):
        trades = agg[name]["trades"]
        n = len(trades)
        if n == 0:
            print(f"{name:<24} {0:>5}")
            continue
        wins = sum(1 for t in trades if t["outcome"] == "win")
        losses = sum(1 for t in trades if t["outcome"] == "loss")
        timeouts = sum(1 for t in trades if t["outcome"] == "timeout")
        win_pips = sum(t["pips"] for t in trades if t["outcome"] == "win")
        loss_pips_abs = abs(sum(t["pips"] for t in trades if t["outcome"] == "loss"))
        timeout_pips = sum(t["pips"] for t in trades if t["outcome"] == "timeout")
        total_pips = win_pips - loss_pips_abs + timeout_pips
        wr = 100 * wins / n
        avg_pips = total_pips / n
        pf = (win_pips + max(timeout_pips, 0)) / max(loss_pips_abs - min(timeout_pips, 0), 0.0001)
        expectancy = avg_pips
        to_pct = 100 * timeouts / n
        rows.append((name, n, wins, wr, avg_pips, total_pips, pf, expectancy, to_pct))
        print(f"{name:<24} {n:>5} {wins:>5} {wr:>5.1f}% {avg_pips:>+7.2f} {total_pips:>+8.1f} {pf:>5.2f} {expectancy:>+7.2f} {to_pct:>5.1f}%")
    print("=" * 100)
    print()
    # Pass/fail gate
    print("Acceptance gates: n>=30, WR>=55%, PF>=1.5, AvgPip>0  (passing setups)")
    print("-" * 60)
    for row in rows:
        name, n, wins, wr, avg_pips, total_pips, pf, expectancy, _ = row
        passes = n >= 30 and wr >= 55 and pf >= 1.5 and avg_pips > 0
        flag = "PASS" if passes else "fail"
        print(f"  [{flag}] {name}  (n={n}, WR={wr:.1f}%, PF={pf:.2f}, avg={avg_pips:+.2f})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--pairs", nargs="+", default=ALL_PAIRS)
    parser.add_argument("--exit-mode", choices=["static", "dynamic", "guardian"], default="guardian",
                        help="static = 1.5/2.5 ATR | dynamic = simplified ratchet | guardian = REAL production candle_walk_replay")
    parser.add_argument("--walk-forward", type=int, default=0,
                        help="If >0, split window into N folds and emit per-fold stability report")
    parser.add_argument("--overlap", action="store_true",
                        help="Run overlap analysis on Tier 1 detectors")
    parser.add_argument("--gates", action="store_true",
                        help="Apply production snipe gates (fan_exhaustion, validator_fan_alignment, ema_ordering, counter_momentum)")
    args = parser.parse_args()

    print(f"Setup backtest: {len(args.pairs)} pairs × {args.days} days × {len(DETECTORS)} detectors | exit={args.exit_mode} | gates={args.gates}")
    print()
    per_pair = {}
    gate_results = {}  # detector -> {fires_total, trades_passed, blocked_by}
    for i, pair in enumerate(args.pairs, 1):
        print(f"[{i}/{len(args.pairs)}] {pair}...", flush=True)
        try:
            if args.gates:
                gp_res = run_pair_with_gates(pair, args.days, apply_gates=True)
                # Build per_pair format for downstream aggregation: trades = trades_passed
                per_pair[pair] = {n: {"trades": v["trades_passed"]} for n, v in gp_res.items()}
                # Also track gate funnel
                for n, v in gp_res.items():
                    if n not in gate_results:
                        gate_results[n] = {"fires_total": 0, "trades_passed": 0, "blocked_by": defaultdict(int)}
                    gate_results[n]["fires_total"] += v["fires_total"]
                    gate_results[n]["trades_passed"] += len(v["trades_passed"])
                    for blk, cnt in v["blocked_by"].items():
                        gate_results[n]["blocked_by"][blk] += cnt
            else:
                per_pair[pair] = run_pair(pair, args.days, exit_mode=args.exit_mode)
        except Exception as e:
            print(f"  {pair}: FAILED — {e}")
            per_pair[pair] = {}

    if args.gates and gate_results:
        print()
        print("=" * 105)
        print("PRODUCTION SNIPE GATE FUNNEL — fires that survive deterministic gates")
        print("=" * 105)
        print(f"{'Setup':<22} {'Fires':>7} {'Pass':>6} {'Pass%':>6} {'fan_ex':>7} {'val_fan':>8} {'ema_ord':>8} {'cnt_mom':>8}")
        print("-" * 105)
        for name in sorted(gate_results.keys()):
            v = gate_results[name]
            n = v["fires_total"]
            p = v["trades_passed"]
            pct = 100 * p / n if n else 0
            fan_ex = v["blocked_by"].get("fan_exhaustion", 0)
            val_fan = v["blocked_by"].get("validator_fan_alignment", 0)
            ema_ord = v["blocked_by"].get("ema_ordering_conflict", 0)
            cnt_mom = sum(c for b, c in v["blocked_by"].items() if b.startswith("counter_momentum"))
            print(f"{name:<22} {n:>7} {p:>6} {pct:>5.1f}% {fan_ex:>7} {val_fan:>8} {ema_ord:>8} {cnt_mom:>8}")
        print("=" * 105)

    agg = aggregate(per_pair)
    report(agg)

    if args.walk_forward and args.walk_forward >= 2:
        walk_forward_report(per_pair, args.walk_forward)

    if args.overlap:
        TIER1 = ["C1_STOCH_EXTREME_BB", "C3_RSI_DIV_GOLDEN", "C4_CHART_PATTERN_BREAK",
                 "C5_FIB_REACTION", "C8_TRIANGLE_BREAKOUT", "C9_BEAR_EXP_PULLBACK",
                 "C11_JPY_BIG_MOVE"]
        overlap_report(per_pair, TIER1)

    # Per-pair detail for top performers
    print("\nPer-pair fire counts:")
    for setup_name in sorted(DETECTORS.keys()):
        line = f"  {setup_name:<24}"
        for pair in args.pairs:
            n = len(per_pair.get(pair, {}).get(setup_name, {}).get("trades", []))
            line += f" {pair[:6]}={n:>3}"
        print(line)


if __name__ == "__main__":
    main()
