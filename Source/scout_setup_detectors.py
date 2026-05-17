"""Tier 1 scout setup detectors — additive triggers alongside V4.

Each detector inspects a recent window of M15 candles (with indicators already
computed via add_enhanced_indicators) and returns:
    'buy' | 'sell' | None

These were validated via 90d × 14-pair × 8-fold walk-forward backtest using
production guardian config. Stability: all 7 had zero negative folds and
sd_WR <= 5pp. Overlap with V4: 92-100% NEW signal (not duplicating).

See scripts/setup_signal_backtest.py for the test harness and full results.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple

import pandas as pd


# ── C1 — Stoch crossing back from extreme + BB band touch + ranging ──
def detect_C1_stoch_extreme_bb(df: pd.DataFrame, i: int) -> Optional[str]:
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
    if stoch_prev >= 80 and p1["high"] >= bb_u * 0.999 and stoch_now < stoch_prev and r["close"] < r["open"]:
        return "sell"
    if stoch_prev <= 20 and p1["low"] <= bb_l * 1.001 and stoch_now > stoch_prev and r["close"] > r["open"]:
        return "buy"
    return None


# ── C3 — RSI Divergence Golden (price extreme vs RSI relaxed, ADX declining) ──
def detect_C3_rsi_div_golden(df: pd.DataFrame, i: int) -> Optional[str]:
    if i < 12:
        return None
    r = df.iloc[i]
    look = df.iloc[i-10:i+1]
    bb_u, bb_l = r.get("bb_upper"), r.get("bb_lower")
    adx = r.get("adx", 25)
    adx_prev = df.iloc[i-3].get("adx", 25)
    if pd.isna(bb_u) or pd.isna(bb_l):
        return None
    if not (adx < adx_prev and adx_prev > 25):
        return None
    is_new_hh = r["high"] >= look["high"].max()
    if is_new_hh:
        prior_high_idx = look["high"].iloc[:-1].idxmax()
        rsi_now, rsi_prior = r.get("rsi", 50), df.iloc[prior_high_idx].get("rsi", 50)
        if rsi_now < rsi_prior and r["close"] >= bb_u * 0.998:
            return "sell"
    is_new_ll = r["low"] <= look["low"].min()
    if is_new_ll:
        prior_low_idx = look["low"].iloc[:-1].idxmin()
        rsi_now, rsi_prior_l = r.get("rsi", 50), df.iloc[prior_low_idx].get("rsi", 50)
        if rsi_now > rsi_prior_l and r["close"] <= bb_l * 1.002:
            return "buy"
    return None


# ── C4 — Double-top / double-bottom break (chart pattern) ──
def detect_C4_chart_pattern_break(df: pd.DataFrame, i: int) -> Optional[str]:
    if i < 30:
        return None
    r = df.iloc[i]
    atr = r.get("atr", 0)
    if not atr or atr <= 0:
        return None
    look = df.iloc[i-30:i]
    highs = look["high"].values
    lows = look["low"].values
    # Double top
    top_idx = highs.argmax()
    masked_h = highs.copy()
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


# ── C5 — Fib retracement (38.2/50/61.8) + reversal candle + EMA-21 alignment ──
def detect_C5_fib_reaction(df: pd.DataFrame, i: int) -> Optional[str]:
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
    if e21 > e100 and bullish_reversal:
        return "buy"
    if e21 < e100 and bearish_reversal:
        return "sell"
    return None


# ── C8 — Triangle breakout (consolidation tightening + breakout close) ──
def detect_C8_triangle_breakout(df: pd.DataFrame, i: int) -> Optional[str]:
    if i < 21:
        return None
    r = df.iloc[i]
    look = df.iloc[i-20:i]
    high_max = look["high"].max()
    low_min = look["low"].min()
    rng = high_max - low_min
    atr = r.get("atr", 0)
    if not atr or rng > 6 * atr:
        return None
    early = df.iloc[i-20:i-10]
    late = df.iloc[i-10:i]
    early_rng = early["high"].max() - early["low"].min()
    late_rng = late["high"].max() - late["low"].min()
    if late_rng >= early_rng * 0.85:
        return None
    if r["close"] > high_max:
        return "buy"
    if r["close"] < low_min:
        return "sell"
    return None


# ── C9 — Bearish-expansion pullback (the dominant live-winner archetype) ──
def detect_C9_bear_exp_pullback(df: pd.DataFrame, i: int) -> Optional[str]:
    if i < 8:
        return None
    r = df.iloc[i]
    p1 = df.iloc[i-1]
    e21, e55, e100 = r.get("ema_21"), r.get("ema_55"), r.get("ema_100")
    if any(pd.isna(x) for x in (e21, e55, e100)):
        return None
    if e21 < e55 < e100:
        prev_bull = p1["close"] > p1["open"] and (p1["close"] - p1["open"]) < 0.6 * (p1["high"] - p1["low"])
        if prev_bull and p1["high"] <= e21 * 1.0010 and r["close"] < r["open"] and r["close"] < e21:
            return "sell"
    if e21 > e55 > e100:
        prev_bear = p1["close"] < p1["open"] and (p1["open"] - p1["close"]) < 0.6 * (p1["high"] - p1["low"])
        if prev_bear and p1["low"] >= e21 * 0.9990 and r["close"] > r["open"] and r["close"] > e21:
            return "buy"
    return None


# ── C11 — JPY-style big-move continuation (ordered fan + ADX>28 + MACD aligned) ──
def detect_C11_big_move(df: pd.DataFrame, i: int) -> Optional[str]:
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


# ── C12 — Cascade Continuation (mid-cascade entry, momentum NOT exhausted) ──
# Discovered 2026-05-10 from hand-classifying 20 manual + scout 'unknown' winners
# (Tim's cascade catches). Distinct from C9 (specific 2-candle pullback shape) and
# S5/S16 (require ADX/MACD alignment): C12 enters during the *expansion* phase of
# an ordered fan when momentum hasn't yet rolled over and stoch is not extreme.
# Live evidence: 18W/5L (78% WR) on the 23 trades matching this profile.
def detect_C12_cascade_continuation(df: pd.DataFrame, i: int) -> Optional[str]:
    if i < 5:
        return None
    r = df.iloc[i]
    e21, e55, e100 = r.get("ema_21"), r.get("ema_55"), r.get("ema_100")
    if any(pd.isna(x) for x in (e21, e55, e100)):
        return None
    p5 = df.iloc[i - 5]
    e21_5, e55_5, e100_5 = p5.get("ema_21"), p5.get("ema_55"), p5.get("ema_100")
    if any(pd.isna(x) for x in (e21_5, e55_5, e100_5)):
        return None
    stoch_k = r.get("stoch_k", 50)
    macd_h = r.get("macd_histogram", 0)
    # ── Bearish cascade continuation (sell) ──
    # ordered bearish fan, fan NOT contracting (≥85% of width 5 bars ago),
    # stoch not overbought (≤70), MACD histogram not bullish (no counter-momentum),
    # price below E21 (still in cascade).
    if e21 < e55 < e100:
        fan_now = e100 - e21
        fan_5 = e100_5 - e21_5
        if fan_now >= fan_5 * 0.85 and stoch_k <= 70 and macd_h <= 0 and r["close"] < e21:
            return "sell"
    # ── Bullish cascade continuation (buy) — symmetric ──
    if e21 > e55 > e100:
        fan_now = e21 - e100
        fan_5 = e21_5 - e100_5
        if fan_now >= fan_5 * 0.85 and stoch_k >= 30 and macd_h >= 0 and r["close"] > e21:
            return "buy"
    return None


TIER1_DETECTORS = {
    "C1_STOCH_EXTREME_BB":   detect_C1_stoch_extreme_bb,
    "C3_RSI_DIV_GOLDEN":     detect_C3_rsi_div_golden,
    "C4_CHART_PATTERN_BREAK": detect_C4_chart_pattern_break,
    "C5_FIB_REACTION":       detect_C5_fib_reaction,
    "C8_TRIANGLE_BREAKOUT":  detect_C8_triangle_breakout,
    "C9_BEAR_EXP_PULLBACK":  detect_C9_bear_exp_pullback,
    "C11_BIG_MOVE":          detect_C11_big_move,
    "C12_CASCADE_CONTINUATION": detect_C12_cascade_continuation,
}


def run_tier1_detectors(df: pd.DataFrame, i: Optional[int] = None) -> List[Tuple[str, str]]:
    """Run all Tier 1 detectors on df at index i (default = last bar).

    Returns:
        List of (detector_name, direction) for any that fired.
        direction is 'buy' or 'sell'.
    """
    if df is None or len(df) < 35:
        return []
    if i is None:
        i = len(df) - 1
    fired = []
    for name, fn in TIER1_DETECTORS.items():
        try:
            d = fn(df, i)
            if d in ("buy", "sell"):
                fired.append((name, d))
        except Exception:
            continue
    return fired
