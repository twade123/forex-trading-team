"""Kronos-specific guardian exit logic.

Built for Kronos Hunter trades that live in parallel-stable / compressed EMA
regimes — where scout's threat-scoring misreads "fan contracting" as reversal
and closes winners early.

Two sub-modes auto-detected at spawn:

  CONTINUATION — price within N×ATR of E21, direction aligned with fan. Trust
    the trend; exit only when structure breaks (body closes wrong side of
    E21, slope flips, separation collapses).

  REVERSAL — price extended >M×ATR from E21 OR direction opposes fan. Trade
    is a counter-trend / extension play; exit if fresh trend-direction
    candle resumes past the extreme.

Pure functions — no side effects, fully unit-testable. The live guardian
integrates this by calling `detect_mode` at watcher spawn, then consulting
`should_exit_*` on each M15 bar alongside ratchet/trailing/SL/TP.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Sequence, Tuple


class KronosMode(str, Enum):
    CONTINUATION = "continuation"
    REVERSAL = "reversal"


@dataclass
class BarContext:
    """One M15 bar + trailing indicator values needed for exit decisions."""
    open: float
    high: float
    low: float
    close: float
    ema21: float
    ema55: float
    ema100: float
    bb_width_pips: float  # distance upper-lower in pips
    atr_pips: float


@dataclass
class KronosExitDecision:
    should_exit: bool
    reason: str


# ---------------------------------------------------------------------------
# Default tunables — sweep these with candle_walk_backtest to find optima
# ---------------------------------------------------------------------------
DEFAULT_PARAMS: Dict[str, float] = {
    # Mode detection
    "continuation_dist_atr": 1.0,     # price within 1×ATR of E21 AND direction aligned = continuation
    "reversal_dist_atr":     1.5,     # price >1.5×ATR from E21 = reversal candidate

    # Continuation exits
    "e21_slope_flip_bars":    2,      # E21 slope flipped sign for N consecutive bars -> exit
    "separation_collapse_atr": 1.0,   # E21-E55 separation drops by >1×ATR in window -> exit
    "separation_window_bars":  3,
    "e100_body_proximity_atr": 0.3,   # body midpoint within 0.3×ATR of E100 -> exit
    "min_body_cross_frac":     0.6,   # "body crosses E21" = body straddles E21 with at least 60% on wrong side

    # Reversal exits
    "reversal_resume_body_atr": 1.0,  # fresh trend-dir body closes this far past E21 -> exit
}


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------
def detect_mode(
    *,
    entry_price: float,
    ema21: float,
    ema55: float,
    ema100: float,
    atr_pips: float,
    pip_size: float,
    direction: str,
    params: Dict[str, float] = DEFAULT_PARAMS,
) -> KronosMode:
    """At trade spawn, pick CONTINUATION or REVERSAL mode."""
    if atr_pips <= 0:
        return KronosMode.CONTINUATION  # fallback

    dist_from_e21_pips = abs(entry_price - ema21) / pip_size
    dist_frac = dist_from_e21_pips / atr_pips

    # Fan direction: bullish if e21 > e55 > e100, bearish if reversed
    bullish = ema21 > ema55 > ema100
    bearish = ema21 < ema55 < ema100
    fan_dir = "bullish" if bullish else "bearish" if bearish else "mixed"

    aligned = (direction == "buy" and fan_dir == "bullish") or \
              (direction == "sell" and fan_dir == "bearish")

    cont_dist = params.get("continuation_dist_atr", 1.0)
    rev_dist = params.get("reversal_dist_atr", 1.5)

    if aligned and dist_frac <= cont_dist:
        return KronosMode.CONTINUATION
    if dist_frac >= rev_dist:
        return KronosMode.REVERSAL
    # Middle zone: default to CONTINUATION if aligned, REVERSAL if opposing
    return KronosMode.CONTINUATION if aligned else KronosMode.REVERSAL


# ---------------------------------------------------------------------------
# Continuation-mode exits
# ---------------------------------------------------------------------------
def _body_on_wrong_side(bar: BarContext, direction: str, min_frac: float) -> bool:
    """True if the candle BODY is materially on the wrong side of E21.

    'Materially' = at least `min_frac` of the body is past E21 against trade dir.
    For buy: body below E21. For sell: body above E21.
    """
    body_low = min(bar.open, bar.close)
    body_high = max(bar.open, bar.close)
    body_size = body_high - body_low
    if body_size <= 0:
        return False
    if direction == "buy":
        # Portion of body below E21
        wrong = max(0.0, bar.ema21 - body_low)
        return (wrong / body_size) >= min_frac
    else:
        wrong = max(0.0, body_high - bar.ema21)
        return (wrong / body_size) >= min_frac


def _e21_slope_flipped(
    recent_e21: Sequence[float],
    direction: str,
    bars: int,
) -> bool:
    """E21 slope against trade direction for `bars` consecutive samples."""
    if len(recent_e21) < bars + 1:
        return False
    tail = recent_e21[-(bars + 1):]
    for i in range(1, len(tail)):
        slope = tail[i] - tail[i - 1]
        if direction == "buy" and slope > 0:
            return False
        if direction == "sell" and slope < 0:
            return False
    return True


def _separation_collapsed(
    recent_e21: Sequence[float],
    recent_e55: Sequence[float],
    window: int,
    threshold_price: float,
) -> bool:
    """E21-E55 separation shrank by more than `threshold_price` over window."""
    if len(recent_e21) < window + 1 or len(recent_e55) < window + 1:
        return False
    sep_old = abs(recent_e21[-(window + 1)] - recent_e55[-(window + 1)])
    sep_new = abs(recent_e21[-1] - recent_e55[-1])
    return (sep_old - sep_new) > threshold_price


def should_exit_continuation(
    *,
    bar: BarContext,
    recent_e21: Sequence[float],
    recent_e55: Sequence[float],
    direction: str,
    pip_size: float,
    params: Dict[str, float] = DEFAULT_PARAMS,
) -> KronosExitDecision:
    """Evaluate a continuation-mode trade against the latest M15 bar."""
    # Gate 1: candle body closed on wrong side of E21
    if _body_on_wrong_side(bar, direction, params["min_body_cross_frac"]):
        return KronosExitDecision(True, "body closed through E21 against trade")

    # Gate 2: E21 slope flipped against trade for N bars
    if _e21_slope_flipped(recent_e21, direction, int(params["e21_slope_flip_bars"])):
        return KronosExitDecision(True, f"E21 slope against trade for {int(params['e21_slope_flip_bars'])} bars")

    # Gate 3: E21-E55 separation collapsed by > X×ATR
    threshold_price = params["separation_collapse_atr"] * bar.atr_pips * pip_size
    if _separation_collapsed(
        recent_e21, recent_e55,
        int(params["separation_window_bars"]),
        threshold_price,
    ):
        return KronosExitDecision(True, "E21-E55 separation collapsed — trend weakening")

    # Gate 4: candle body midpoint near E100 (trend dying, testing major structure)
    body_mid = (bar.open + bar.close) / 2.0
    if abs(body_mid - bar.ema100) <= params["e100_body_proximity_atr"] * bar.atr_pips * pip_size:
        # Only against-trade direction (for buy, body near/below E100 = bad)
        if (direction == "buy" and body_mid <= bar.ema100) or \
           (direction == "sell" and body_mid >= bar.ema100):
            return KronosExitDecision(True, "body reached E100 against trade")

    return KronosExitDecision(False, "continuation intact")


# ---------------------------------------------------------------------------
# Reversal-mode exits
# ---------------------------------------------------------------------------
def should_exit_reversal(
    *,
    bar: BarContext,
    entry_price: float,
    extreme_price: float,          # the high (for sell) / low (for buy) the reversal was shorting against
    direction: str,
    pip_size: float,
    params: Dict[str, float] = DEFAULT_PARAMS,
) -> KronosExitDecision:
    """Evaluate a reversal-mode trade against the latest M15 bar.

    Reversal-mode trade is a counter-trend extension play. Exit when the
    prior trend resumes: a full-body candle in the OLD trend direction that
    closes materially past E21.
    """
    threshold_price = params["reversal_resume_body_atr"] * bar.atr_pips * pip_size

    if direction == "sell":
        # Reversal sell = we shorted a top.
        # Extreme breached first — clean signal, takes precedence.
        if bar.close > extreme_price:
            return KronosExitDecision(True, "price back above reversal-entry extreme")
        # Bullish candle closed materially past E21 → prior uptrend resumed
        if bar.close > bar.open and bar.close > bar.ema21 + threshold_price:
            return KronosExitDecision(True, "bullish body past E21 — reversal failed")
    else:
        # Reversal buy = we bought a bottom.
        if bar.close < extreme_price:
            return KronosExitDecision(True, "price back below reversal-entry extreme")
        if bar.close < bar.open and bar.close < bar.ema21 - threshold_price:
            return KronosExitDecision(True, "bearish body past E21 — reversal failed")

    return KronosExitDecision(False, "reversal thesis intact")
