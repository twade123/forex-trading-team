"""Kronos Thesis — Kronos-specific market read + threat score.

Parallel to scout's ``market_story.read_market_story()`` but reads the SAME
indicators (EMA21/55/100, BB(20,2), candle structure, RSI, Stoch, MACD) through
Kronos's interpretive lens:

  Scout asks:   "Is this a tradeable setup RIGHT NOW?" (entries in expansion /
                 peak-turn / compression breakout)
  Kronos asks:  "Is this trade's structure still intact?" (continuation of
                 parallel-stable trends, reversal on extensions)

All thresholds in DEFAULT_PARAMS so the tuner can sweep them with candle_walk.
Pure Python — no LLM, no DB, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

import numpy as np


# ---------------------------------------------------------------------------
# Default tunables (Optuna sweeps these)
# ---------------------------------------------------------------------------
DEFAULT_PARAMS: Dict[str, float] = {
    # --- Sub-mode detection ---
    "continuation_dist_e21_atr": 1.0,    # entry within this many ATRs of E21 + aligned fan -> continuation
    "reversal_dist_e21_atr":     1.5,    # entry >this many ATRs from E21 -> reversal candidate

    # --- Parallel-quality (how stable are EMA separations) ---
    "parallel_window_bars":      8,      # how many bars to measure parallel quality over
    "parallel_quality_min":      60.0,   # 0-100, below this = not parallel -> structure weakening

    # --- Separation stability ---
    "sep_collapse_atr":          1.0,    # E21-E55 separation drops by >X×ATR in window -> fan_collapsing
    "sep_window_bars":           3,
    "sep_55_100_collapse_atr":   1.2,    # longer-term structure collapse

    # --- Just-crossed handling ---
    "recent_cross_bars":         5,      # crossing in last N bars
    "post_cross_grace_bars":     3,      # after fresh cross in our favor, give trend room

    # --- E100 proximity danger ---
    "e100_danger_atr":           0.3,    # body midpoint within 0.3×ATR of E100 against trade = danger

    # --- Candle structure ---
    "body_wrong_side_frac":      0.6,    # >60% of body on wrong side of E21 -> structure broken
    "bodies_on_trend_window":    6,      # look back N closed candles for body-position stats
    "bodies_on_trend_min_pct":   50.0,   # fewer than X% of bodies on trend side of E21 -> weakening

    # --- Extension / exhaustion (for continuation mode only) ---
    "extension_warning_atr":     2.5,    # price >2.5×ATR from E21 = potentially exhausted

    # --- BB width ---
    "bb_width_expanding_pct":    30.0,   # BB width grew >30% in window -> regime-change warning
    "bb_width_window_bars":      5,

    # --- Momentum (RSI extremes) ---
    "rsi_divergence_bars":       5,      # RSI divergence over this window
    "rsi_extreme_high":          72.0,
    "rsi_extreme_low":           28.0,

    # --- Continuation score weights (sum to 1.0) ---
    "w_fan_posture":             0.30,
    "w_separation_stability":    0.25,
    "w_candle_alignment":        0.20,
    "w_e100_safety":             0.15,
    "w_momentum_sanity":         0.10,

    # --- Exit thresholds on continuation_score ---
    "score_hard_exit":           25.0,   # score below this = hard exit
    "score_soft_exit":           45.0,   # score below this = tighten SL, arm exit
}


class KronosThesisState(str, Enum):
    CONTINUATION_HEALTHY = "continuation_healthy"
    CONTINUATION_WEAKENING = "continuation_weakening"
    REVERSAL_BREWING = "reversal_brewing"
    COMPRESSION_UNSTABLE = "compression_unstable"
    FAN_COLLAPSING = "fan_collapsing"
    STRUCTURE_BROKEN = "structure_broken"
    EXHAUSTION = "exhaustion"


class KronosExitSignal(str, Enum):
    NONE = "none"
    WATCH = "watch"
    SOFT_EXIT = "soft_exit"
    HARD_EXIT = "hard_exit"


@dataclass
class KronosThesisRead:
    state: KronosThesisState
    continuation_score: float             # 0-100
    exit_signal: KronosExitSignal
    reasons: List[str] = field(default_factory=list)

    # Raw metrics (for logging / tuning / flight_log)
    fan_ordering: str = "mixed"           # aligned_bullish / aligned_bearish / mixed
    parallel_quality: float = 0.0         # 0-100
    sep_21_55_pips: float = 0.0
    sep_55_100_pips: float = 0.0
    delta_sep_21_55_atr: float = 0.0      # Δ in ATRs over window
    e21_slope_sign: int = 0               # +1 up, -1 down, 0 flat
    body_pct_on_trend: float = 0.0        # 0-100
    e100_distance_atr: float = 0.0
    extension_atr: float = 0.0            # current price distance from E21 in ATRs
    bb_width_delta_pct: float = 0.0
    recent_cross: bool = False
    rsi_last: float = 50.0
    sub_mode: str = "continuation"        # continuation | reversal


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------
def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(arr, np.nan, dtype=float)
    if len(arr) < period:
        return out
    mult = 2.0 / (period + 1)
    out[period - 1] = arr[:period].mean()
    for i in range(period, len(arr)):
        out[i] = (arr[i] - out[i - 1]) * mult + out[i - 1]
    return out


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    tr = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - closes[:-1]),
        np.abs(lows[1:] - closes[:-1]),
    ])
    return float(np.mean(tr[-period:]))


def _bb_width(closes: np.ndarray, period: int = 20, n_std: float = 2.0) -> float:
    if len(closes) < period:
        return 0.0
    w = closes[-period:]
    return (w.std() * n_std * 2.0)  # width from upper to lower


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    d = np.diff(closes)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag = g[-period:].mean()
    al = l[-period:].mean()
    if al == 0:
        return 100.0
    rs = ag / al
    return float(100 - 100 / (1 + rs))


def _parallel_quality(
    e21: np.ndarray, e55: np.ndarray, e100: np.ndarray, window: int,
) -> float:
    """Score 0-100 — how parallel the three EMAs stay over the window.

    100 = separations held perfectly constant (EMAs move in lockstep).
    Low = separations changed a lot (fan expanding/collapsing/flapping).
    """
    if len(e21) < window or len(e55) < window or len(e100) < window:
        return 0.0
    sep_21_55 = np.abs(e21[-window:] - e55[-window:])
    sep_55_100 = np.abs(e55[-window:] - e100[-window:])
    # Coefficient of variation per pair — lower cv = more parallel
    def _cv(x):
        m = float(np.mean(x))
        if m <= 0:
            return 1.0
        return float(np.std(x) / m)
    cv = (_cv(sep_21_55) + _cv(sep_55_100)) / 2.0
    # Squash: cv=0 -> 100, cv=1 -> ~37, cv=2 -> ~13
    return float(max(0.0, min(100.0, 100.0 * np.exp(-cv))))


def _pct_bodies_on_trend_side(
    opens: np.ndarray, closes: np.ndarray, e21: np.ndarray,
    direction: str, window: int,
) -> float:
    """% of last `window` closed candles whose body midpoint is on the
    trade-direction side of E21."""
    if len(closes) < window:
        return 0.0
    o = opens[-window:]
    c = closes[-window:]
    e = e21[-window:]
    mids = (o + c) / 2.0
    if direction == "buy":
        hits = (mids > e).sum()
    else:
        hits = (mids < e).sum()
    return float(100.0 * hits / window)


def _recent_cross(e21: np.ndarray, e55: np.ndarray, bars: int) -> bool:
    if len(e21) < bars + 1 or len(e55) < bars + 1:
        return False
    for i in range(len(e21) - bars, len(e21)):
        if np.isnan(e21[i - 1]) or np.isnan(e55[i - 1]):
            continue
        if (e21[i - 1] > e55[i - 1]) != (e21[i] > e55[i]):
            return True
    return False


# ---------------------------------------------------------------------------
# Main reader
# ---------------------------------------------------------------------------
def read_kronos_thesis(
    *,
    candles: Sequence[Dict[str, float]],
    pair: str,
    direction: str,
    entry_price: Optional[float] = None,
    params: Dict[str, float] = DEFAULT_PARAMS,
) -> KronosThesisRead:
    """Read the market through Kronos's lens.

    Args:
        candles: list of dicts with open/high/low/close (oldest first).
        pair: instrument (e.g. "EUR_USD") — used for pip size.
        direction: "buy" or "sell" — trade direction being evaluated.
        entry_price: original trade entry price. If None, uses last close
            (useful for pre-spawn signal evaluation).
        params: DEFAULT_PARAMS or a sweep override.

    Returns:
        KronosThesisRead with synthesized state + continuation_score +
        exit_signal + raw metrics.
    """
    pip = 0.01 if "JPY" in pair.upper() else 0.0001
    n = len(candles)
    if n < 100:
        return KronosThesisRead(
            state=KronosThesisState.COMPRESSION_UNSTABLE,
            continuation_score=0.0,
            exit_signal=KronosExitSignal.HARD_EXIT,
            reasons=[f"insufficient candles ({n} < 100)"],
        )

    opens = np.array([float(c["open"]) for c in candles])
    highs = np.array([float(c["high"]) for c in candles])
    lows = np.array([float(c["low"]) for c in candles])
    closes = np.array([float(c["close"]) for c in candles])

    e21 = _ema(closes, 21)
    e55 = _ema(closes, 55)
    e100 = _ema(closes, 100)
    atr_raw = _atr(highs, lows, closes)
    atr_pips = atr_raw / pip
    bb_width_now = _bb_width(closes)
    bb_width_then = _bb_width(closes[: -int(params["bb_width_window_bars"])]) \
        if len(closes) > int(params["bb_width_window_bars"]) + 20 else bb_width_now

    current_price = float(entry_price) if entry_price else float(closes[-1])

    # Fan ordering
    if e21[-1] > e55[-1] > e100[-1]:
        fan_ordering = "aligned_bullish"
    elif e21[-1] < e55[-1] < e100[-1]:
        fan_ordering = "aligned_bearish"
    else:
        fan_ordering = "mixed"

    # Sub-mode (continuation vs reversal)
    dist_e21_pips = abs(current_price - e21[-1]) / pip
    extension_atr = dist_e21_pips / atr_pips if atr_pips > 0 else 0.0

    aligned = (direction == "buy" and fan_ordering == "aligned_bullish") or \
              (direction == "sell" and fan_ordering == "aligned_bearish")
    # Sub-mode: aligned with fan = continuation (extension is natural in
    # strong trends). Opposing fan = reversal. Extension alone never forces
    # reversal — exhaustion state checks that separately.
    sub_mode = "continuation" if aligned else "reversal"

    # Parallel quality
    pq = _parallel_quality(e21, e55, e100, int(params["parallel_window_bars"]))

    # Separations
    sep_21_55_now = abs(e21[-1] - e55[-1]) / pip
    sep_55_100_now = abs(e55[-1] - e100[-1]) / pip
    window = int(params["sep_window_bars"])
    sep_21_55_then = abs(e21[-window - 1] - e55[-window - 1]) / pip if len(e21) > window else sep_21_55_now
    sep_55_100_then = abs(e55[-window - 1] - e100[-window - 1]) / pip if len(e55) > window else sep_55_100_now
    delta_sep_21_55_atr = (sep_21_55_then - sep_21_55_now) / atr_pips if atr_pips > 0 else 0.0
    delta_sep_55_100_atr = (sep_55_100_then - sep_55_100_now) / atr_pips if atr_pips > 0 else 0.0

    # E21 slope sign over recent bars
    e21_slope_raw = e21[-1] - e21[-min(5, len(e21))]
    e21_slope_sign = 1 if e21_slope_raw > 0 else -1 if e21_slope_raw < 0 else 0

    # Body alignment
    body_pct = _pct_bodies_on_trend_side(opens, closes, e21, direction,
                                          int(params["bodies_on_trend_window"]))

    # E100 proximity
    e100_dist_pips = abs(current_price - e100[-1]) / pip
    e100_dist_atr = e100_dist_pips / atr_pips if atr_pips > 0 else 999
    # Is E100 against the trade (price crossing/near E100 in trade-wrong direction)?
    e100_against = (
        (direction == "buy" and current_price <= e100[-1] + params["e100_danger_atr"] * atr_pips * pip) or
        (direction == "sell" and current_price >= e100[-1] - params["e100_danger_atr"] * atr_pips * pip)
    )

    # Recent cross
    rc = _recent_cross(e21, e55, int(params["recent_cross_bars"]))

    # BB width behavior
    bb_width_pips_now = bb_width_now / pip
    bb_width_delta_pct = 100 * (bb_width_now - bb_width_then) / bb_width_then if bb_width_then > 0 else 0.0

    # RSI
    rsi_last = _rsi(closes)

    # ------------------------------------------------------------------
    # Synthesis — weighted continuation score + state classification
    # ------------------------------------------------------------------
    reasons: List[str] = []

    # (1) Fan posture score (0-100)
    if aligned:
        fan_score = 100.0 if fan_ordering != "mixed" else 40.0
    elif fan_ordering == "mixed":
        fan_score = 20.0
        reasons.append("fan ordering mixed")
    else:
        fan_score = 10.0
        reasons.append("trade direction opposes fan ordering")

    # (2) Separation stability score (0-100)
    sep_collapse = delta_sep_21_55_atr >= params["sep_collapse_atr"] or \
                   delta_sep_55_100_atr >= params["sep_55_100_collapse_atr"]
    if sep_collapse:
        sep_score = 15.0
        reasons.append(f"separation collapsed (Δ21-55={delta_sep_21_55_atr:.2f}×ATR)")
    elif pq >= params["parallel_quality_min"]:
        sep_score = 95.0
    else:
        # Linearly interpolate between 20 (cv=high) and 90 (at threshold)
        sep_score = 20.0 + 70.0 * (pq / params["parallel_quality_min"])
        if pq < params["parallel_quality_min"] * 0.6:
            reasons.append(f"parallel quality low ({pq:.0f})")

    # (3) Candle alignment score
    if body_pct >= 70:
        candle_score = 95.0
    elif body_pct >= params["bodies_on_trend_min_pct"]:
        candle_score = 65.0
    elif body_pct >= 30:
        candle_score = 35.0
        reasons.append(f"only {body_pct:.0f}% of bodies on trend side of E21")
    else:
        candle_score = 10.0
        reasons.append(f"bodies mostly against trend ({body_pct:.0f}%)")

    # (4) E100 safety
    if e100_against:
        e100_score = 10.0
        reasons.append(f"price testing E100 against trade ({e100_dist_atr:.2f}×ATR)")
    elif e100_dist_atr >= 1.5:
        e100_score = 95.0
    elif e100_dist_atr >= 0.8:
        e100_score = 70.0
    else:
        e100_score = 40.0

    # (5) Momentum sanity
    if direction == "buy":
        if rsi_last >= params["rsi_extreme_high"] and sub_mode == "continuation":
            mom_score = 50.0  # overbought but in continuation — ok, common in strong trends
            reasons.append(f"RSI overbought ({rsi_last:.0f}) but continuation mode")
        elif rsi_last <= params["rsi_extreme_low"]:
            mom_score = 20.0
            reasons.append(f"RSI oversold against buy ({rsi_last:.0f})")
        else:
            mom_score = 85.0
    else:
        if rsi_last <= params["rsi_extreme_low"] and sub_mode == "continuation":
            mom_score = 50.0
            reasons.append(f"RSI oversold ({rsi_last:.0f}) but continuation mode")
        elif rsi_last >= params["rsi_extreme_high"]:
            mom_score = 20.0
            reasons.append(f"RSI overbought against sell ({rsi_last:.0f})")
        else:
            mom_score = 85.0

    # Weighted score
    cont_score = (
        fan_score * params["w_fan_posture"]
        + sep_score * params["w_separation_stability"]
        + candle_score * params["w_candle_alignment"]
        + e100_score * params["w_e100_safety"]
        + mom_score * params["w_momentum_sanity"]
    )

    # --- Classify state ---
    # Hard-exit conditions override score
    body_cross_wrong = False
    last_o, last_c = float(opens[-1]), float(closes[-1])
    body_lo = min(last_o, last_c)
    body_hi = max(last_o, last_c)
    body_size = body_hi - body_lo
    if body_size > 0:
        if direction == "buy":
            wrong = max(0.0, e21[-1] - body_lo)
            if (wrong / body_size) >= params["body_wrong_side_frac"]:
                body_cross_wrong = True
        else:
            wrong = max(0.0, body_hi - e21[-1])
            if (wrong / body_size) >= params["body_wrong_side_frac"]:
                body_cross_wrong = True

    # Fan-ordering-agnostic compression: if ALL three EMAs sit within
    # ~0.5×ATR of each other, we're in a coil/noise regime regardless of
    # what the ordering happens to be. Common trap on quiet pairs.
    total_sep_pips = sep_21_55_now + sep_55_100_now
    _total_compressed = atr_pips > 0 and total_sep_pips < 0.5 * atr_pips

    if body_cross_wrong:
        state = KronosThesisState.STRUCTURE_BROKEN
        reasons.append("body closed on wrong side of E21")
    elif _total_compressed:
        state = KronosThesisState.COMPRESSION_UNSTABLE
        reasons.append(f"EMAs stacked (total sep {total_sep_pips:.2f}p < 0.5×ATR)")
    elif sep_collapse and pq < params["parallel_quality_min"]:
        state = KronosThesisState.FAN_COLLAPSING
    elif fan_ordering == "mixed" and sep_21_55_now < 0.5 * atr_pips:
        state = KronosThesisState.COMPRESSION_UNSTABLE
        reasons.append("EMAs compressed/mixed")
    elif sub_mode == "reversal" and extension_atr >= params["extension_warning_atr"]:
        # Could be valid reversal OR exhaustion — call it brewing
        state = KronosThesisState.REVERSAL_BREWING
    elif cont_score >= 75:
        state = KronosThesisState.CONTINUATION_HEALTHY
    elif cont_score >= 50:
        state = KronosThesisState.CONTINUATION_WEAKENING
    elif cont_score >= 30 and sub_mode == "continuation":
        state = KronosThesisState.EXHAUSTION
    else:
        state = KronosThesisState.STRUCTURE_BROKEN

    # --- Exit signal mapping ---
    if state in (KronosThesisState.STRUCTURE_BROKEN, KronosThesisState.FAN_COLLAPSING,
                 KronosThesisState.COMPRESSION_UNSTABLE) or cont_score <= params["score_hard_exit"]:
        exit_sig = KronosExitSignal.HARD_EXIT
    elif state == KronosThesisState.CONTINUATION_WEAKENING or cont_score <= params["score_soft_exit"]:
        exit_sig = KronosExitSignal.SOFT_EXIT
    elif state == KronosThesisState.REVERSAL_BREWING:
        exit_sig = KronosExitSignal.WATCH
    else:
        exit_sig = KronosExitSignal.NONE

    return KronosThesisRead(
        state=state,
        continuation_score=round(cont_score, 1),
        exit_signal=exit_sig,
        reasons=reasons,
        fan_ordering=fan_ordering,
        parallel_quality=round(pq, 1),
        sep_21_55_pips=round(sep_21_55_now, 2),
        sep_55_100_pips=round(sep_55_100_now, 2),
        delta_sep_21_55_atr=round(delta_sep_21_55_atr, 3),
        e21_slope_sign=e21_slope_sign,
        body_pct_on_trend=round(body_pct, 1),
        e100_distance_atr=round(e100_dist_atr, 3),
        extension_atr=round(extension_atr, 3),
        bb_width_delta_pct=round(bb_width_delta_pct, 1),
        recent_cross=rc,
        rsi_last=round(rsi_last, 1),
        sub_mode=sub_mode,
    )
