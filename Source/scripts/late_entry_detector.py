"""late_entry_detector.py — Structural composite for "is this a late entry?"

The validator on iter 20d/e was over-confirming Phase 3 continuation trades
on extended fans (EUR_CHF / EUR_AUD / AUD_JPY losses 2026-05-11/12 — all
entered at the top of an already-stretched move and never recovered).

This detector measures the STRUCTURAL signals Tim identified, NOT the
oscillator signals (RSI 70+ is often the START of a cascade, not the end):

  1. EMA21 / EMA55 physical slope bend — did the moving average itself
     turn against the proposed trade direction?
  2. BB upper/lower band bend — has the band stopped expanding and
     started rolling over?
  3. Candle-vs-E21 relationship over last 5 bars — touches, breaks,
     close distance from E21 (normalized by ATR).
  4. Fan expansion state — gap E21–E55 and E55–E100 still widening
     (expanding), holding (parallel), or shrinking (late).
  5. Color shift in last 5 bars — opposite-color candles appearing in
     the trend (multiple shifts = momentum decay; one = noise).
  6. Bars since fan first ordered for the proposed direction —
     how many bars deep into the move are we.

Returns a composite score 0–100 and a verdict:
  EARLY  : <30  — structure intact, normal continuation, safe to trade
  MID    : 30–60 — some bending, light retrace coming, downgrade to WATCH
  LATE   : >60  — multiple structural signals broken, skip or regime risk

Pure function — does not import live trading code. Standalone for testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd


# ── CONFIG ───────────────────────────────────────────────────────────

# Thresholds tuned conservatively — calibrate after winners/losers cohort run.
# All "pips" thresholds assume non-JPY pair pip = 0.0001. JPY pairs pip = 0.01
# Caller must pass pair_pip correctly.
CONFIG = {
    # Slope bend detection
    "slope_lookback_bars": 5,         # compare slope now vs N bars ago
    "slope_pip_min_rising": 0.10,     # bar must have moved ≥0.1 pips/bar to count as "rising"
    "slope_pip_max_flat": 0.05,       # bar moving ≤0.05 pips/bar = flat
    # Candle-vs-E21 (look at last N bars)
    "candle_e21_lookback_bars": 5,
    "touch_wick_pad_pips": 0.5,       # low/high within X pips of E21 counts as touch
    # Fan expansion
    "fan_expansion_min_ratio": 1.05,  # gap grew by ≥5% over 5 bars = expanding
    "fan_shrinking_max_ratio": 0.95,  # gap shrank by ≥5% over 5 bars = shrinking
    # Color shift
    "color_shift_lookback": 5,
    "color_shift_count_threshold": 2,  # 2+ opposite-color bars = decay
    # Bars-since-cross
    "bars_late_threshold": 8,
    "bars_very_late_threshold": 13,
    # Composite score weights
    "weights": {
        "ema21_bend":           15,
        "ema55_bend":           10,
        "bb_band_bend":         10,
        "candle_e21":           20,
        "fan_shrinking":        10,
        "color_shift":          10,
        "bars_late":            10,
        "extension_exhaustion": 25,  # NEW: stretched + no pullback
    },
    # Continuation bonus (negative — reduces late score) when fan EXPANDING
    # AND price NOT yet extended (close to E21 ≤1× ATR).
    "expansion_bonus_when_close": -10,
    # NEW: extension exhaustion thresholds
    "extension_atr_threshold_strong": 1.5,   # ≥1.5× ATR from E21 = stretched
    "extension_atr_threshold_extreme": 2.5,  # ≥2.5× ATR = very stretched
    "extension_bars_no_touch_min": 6,        # need N+ bars without pullback to confirm
    # Verdict cutoffs
    "early_max": 30,
    "mid_max": 60,
}


# ── RESULT STRUCTURE ─────────────────────────────────────────────────

@dataclass
class SignalDetail:
    fired: bool
    score: float
    detail: dict = field(default_factory=dict)


@dataclass
class LateEntryResult:
    score: float                     # 0-100 composite
    verdict: Literal["EARLY", "MID", "LATE"]
    direction: str                   # "bullish" or "bearish"
    signals: dict[str, SignalDetail] # per-signal breakdown
    reasons: list[str]               # human-readable
    bars_since_fan_cross: int
    atr_pips: float

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "verdict": self.verdict,
            "direction": self.direction,
            "bars_since_fan_cross": self.bars_since_fan_cross,
            "atr_pips": round(self.atr_pips, 2),
            "signals": {k: {"fired": s.fired, "score": round(s.score, 1),
                            "detail": s.detail} for k, s in self.signals.items()},
            "reasons": self.reasons,
        }


# ── HELPERS ──────────────────────────────────────────────────────────

def _atr_pips(df: pd.DataFrame, pair_pip: float, period: int = 14) -> float:
    """ATR in pips over `period` bars on M15."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    if not np.isfinite(atr) or atr <= 0:
        return 5.0  # safety fallback
    return float(atr / pair_pip)


def _slope_pips_per_bar(series: pd.Series, end_idx: int, span: int, pair_pip: float) -> float:
    """Average pips/bar movement from end_idx-span to end_idx."""
    if end_idx - span < 0:
        return 0.0
    delta = series.iloc[end_idx] - series.iloc[end_idx - span]
    return float(delta / pair_pip / span)


def _bars_since_fan_cross(ema21: pd.Series, ema55: pd.Series, ema100: pd.Series,
                          direction: str) -> int:
    """Walk back to find last bar where fan was NOT ordered for direction.
    Returns bars since the most recent re-ordering (the start of the current cascade)."""
    n = len(ema21)
    for i in range(n - 1, -1, -1):
        e21, e55, e100 = ema21.iloc[i], ema55.iloc[i], ema100.iloc[i]
        if direction == "bullish":
            ordered = (e21 > e55) and (e55 > e100)
        else:
            ordered = (e21 < e55) and (e55 < e100)
        if not ordered:
            return (n - 1) - i
    return n  # fan was ordered for entire window


# ── INDIVIDUAL SIGNAL CHECKS ─────────────────────────────────────────

def _check_ema_bend(ema: pd.Series, direction: str, pair_pip: float,
                    weight: float, label: str) -> SignalDetail:
    """Did this EMA's slope reverse against the proposed trade direction?"""
    n = len(ema)
    lookback = CONFIG["slope_lookback_bars"]
    if n < lookback * 2:
        return SignalDetail(False, 0.0, {"reason": "insufficient data"})

    slope_now = _slope_pips_per_bar(ema, n - 1, 3, pair_pip)
    slope_prev = _slope_pips_per_bar(ema, n - 1 - lookback, 3, pair_pip)

    detail = {
        "slope_now_pips_per_bar": round(slope_now, 3),
        "slope_prev_pips_per_bar": round(slope_prev, 3),
        "lookback_bars": lookback,
    }

    # For bullish trade: want EMA to be RISING (slope > 0). Bending = slope decreased toward 0 or negative
    # For bearish trade: want EMA to be FALLING (slope < 0). Bending = slope increased toward 0 or positive
    rise_min = CONFIG["slope_pip_min_rising"]
    flat_max = CONFIG["slope_pip_max_flat"]

    if direction == "bullish":
        was_strong_up = slope_prev > rise_min
        now_flat_or_down = slope_now < flat_max
        bend = was_strong_up and now_flat_or_down
        # Strength: how much slope dropped
        if bend:
            drop_ratio = max(0.0, (slope_prev - slope_now) / max(slope_prev, 0.1))
            score = weight * min(1.0, drop_ratio)
        else:
            score = 0.0
    else:  # bearish
        was_strong_down = slope_prev < -rise_min
        now_flat_or_up = slope_now > -flat_max
        bend = was_strong_down and now_flat_or_up
        if bend:
            rise_ratio = max(0.0, (slope_now - slope_prev) / max(-slope_prev, 0.1))
            score = weight * min(1.0, rise_ratio)
        else:
            score = 0.0

    detail["bend_detected"] = bend
    detail["label"] = label
    return SignalDetail(bend, score, detail)


def _check_bb_bend(bb_upper: pd.Series, bb_lower: pd.Series, direction: str,
                   pair_pip: float, weight: float) -> SignalDetail:
    """For bullish: upper BB stopped rising AND lower BB stopped falling = both bending in = late.
    For bearish: lower BB stopped falling AND upper BB stopped rising = both bending in = late."""
    n = len(bb_upper)
    lookback = CONFIG["slope_lookback_bars"]
    if n < lookback * 2:
        return SignalDetail(False, 0.0, {"reason": "insufficient data"})

    up_slope_now = _slope_pips_per_bar(bb_upper, n - 1, 3, pair_pip)
    up_slope_prev = _slope_pips_per_bar(bb_upper, n - 1 - lookback, 3, pair_pip)
    lo_slope_now = _slope_pips_per_bar(bb_lower, n - 1, 3, pair_pip)
    lo_slope_prev = _slope_pips_per_bar(bb_lower, n - 1 - lookback, 3, pair_pip)

    flat_max = CONFIG["slope_pip_max_flat"]
    rise_min = CONFIG["slope_pip_min_rising"]

    if direction == "bullish":
        # Upper band was rising and now flat/down
        up_bent = (up_slope_prev > rise_min) and (up_slope_now < flat_max)
        # Lower band was falling and now flat/up
        lo_bent = (lo_slope_prev < -rise_min) and (lo_slope_now > -flat_max)
    else:
        up_bent = (up_slope_prev > rise_min) and (up_slope_now < flat_max)
        lo_bent = (lo_slope_prev < -rise_min) and (lo_slope_now > -flat_max)

    detail = {
        "upper_slope_now": round(up_slope_now, 3),
        "upper_slope_prev": round(up_slope_prev, 3),
        "lower_slope_now": round(lo_slope_now, 3),
        "lower_slope_prev": round(lo_slope_prev, 3),
        "upper_bent": up_bent,
        "lower_bent": lo_bent,
    }

    # Both bands bending in = full weight. One bending = half weight.
    if up_bent and lo_bent:
        score = weight
        fired = True
    elif up_bent or lo_bent:
        score = weight * 0.5
        fired = True
    else:
        score = 0.0
        fired = False

    return SignalDetail(fired, score, detail)


def _check_candle_vs_e21(df: pd.DataFrame, ema21: pd.Series, direction: str,
                         pair_pip: float, atr_pips: float, weight: float) -> SignalDetail:
    """Tim's #1 indicator. Last N bars: are candles testing/breaking E21, or riding away?"""
    n = len(df)
    lookback = CONFIG["candle_e21_lookback_bars"]
    if n < lookback:
        return SignalDetail(False, 0.0, {"reason": "insufficient data"})

    touch_pad = CONFIG["touch_wick_pad_pips"] * pair_pip
    touches = 0   # wick within pad of E21
    breaks = 0    # close crossed wrong side of E21
    close_dist_pips = []  # distance of each close from E21, signed in trade-direction units

    for i in range(n - lookback, n):
        e21 = ema21.iloc[i]
        bar = df.iloc[i]
        high, low, close = bar["high"], bar["low"], bar["close"]
        if direction == "bullish":
            # Touch: low within touch_pad of e21 (price wicked down to it)
            if low <= e21 + touch_pad:
                touches += 1
            # Break: close ended below e21
            if close < e21:
                breaks += 1
            # Signed distance in trade direction (positive = above e21 for bull)
            close_dist_pips.append((close - e21) / pair_pip)
        else:
            if high >= e21 - touch_pad:
                touches += 1
            if close > e21:
                breaks += 1
            close_dist_pips.append((e21 - close) / pair_pip)

    last_dist = close_dist_pips[-1]
    last_dist_atr = last_dist / max(atr_pips, 0.1)

    detail = {
        "lookback_bars": lookback,
        "touches": touches,
        "breaks": breaks,
        "last_close_distance_pips": round(last_dist, 2),
        "last_close_distance_atr": round(last_dist_atr, 2),
        "close_distances_pips_window": [round(d, 1) for d in close_dist_pips],
    }

    # Scoring:
    #   breaks ≥ 1 → 100% weight (closed wrong side of E21 = regime risk)
    #   touches ≥ 3 → 80% (constant retest)
    #   touches 1-2 → 40-60% (some retracement)
    #   touches 0 AND last_dist ≥ 1.0 ATR away → 0% (NOT late, expanding)
    if breaks >= 1:
        score = weight
        fired = True
    elif touches >= 3:
        score = weight * 0.8
        fired = True
    elif touches == 2:
        score = weight * 0.6
        fired = True
    elif touches == 1:
        score = weight * 0.4
        fired = True
    else:
        score = 0.0
        fired = False
        # If candles riding well above (1 ATR+), this gives a NEGATIVE contribution later
        if last_dist_atr >= 1.0:
            detail["riding_away"] = True

    return SignalDetail(fired, score, detail)


def _check_fan_expansion(ema21: pd.Series, ema55: pd.Series, ema100: pd.Series,
                         direction: str, weight: float) -> SignalDetail:
    """Is the fan still expanding (continuation OK), parallel (mid), or shrinking (late)?"""
    n = len(ema21)
    lookback = CONFIG["slope_lookback_bars"]
    if n < lookback + 1:
        return SignalDetail(False, 0.0, {"reason": "insufficient data"})

    gap_21_55_now = abs(ema21.iloc[-1] - ema55.iloc[-1])
    gap_21_55_prev = abs(ema21.iloc[-1 - lookback] - ema55.iloc[-1 - lookback])
    gap_55_100_now = abs(ema55.iloc[-1] - ema100.iloc[-1])
    gap_55_100_prev = abs(ema55.iloc[-1 - lookback] - ema100.iloc[-1 - lookback])

    ratio_21_55 = gap_21_55_now / max(gap_21_55_prev, 1e-9)
    ratio_55_100 = gap_55_100_now / max(gap_55_100_prev, 1e-9)

    expand_ratio = CONFIG["fan_expansion_min_ratio"]
    shrink_ratio = CONFIG["fan_shrinking_max_ratio"]

    expanding_21_55 = ratio_21_55 >= expand_ratio
    expanding_55_100 = ratio_55_100 >= expand_ratio
    shrinking_21_55 = ratio_21_55 <= shrink_ratio
    shrinking_55_100 = ratio_55_100 <= shrink_ratio

    detail = {
        "gap_21_55_now": round(gap_21_55_now, 5),
        "gap_21_55_prev": round(gap_21_55_prev, 5),
        "ratio_21_55": round(ratio_21_55, 3),
        "ratio_55_100": round(ratio_55_100, 3),
        "expanding_21_55": expanding_21_55,
        "expanding_55_100": expanding_55_100,
        "shrinking_21_55": shrinking_21_55,
        "shrinking_55_100": shrinking_55_100,
    }

    # Shrinking = late. Expanding = NOT late (negative contribution).
    if shrinking_21_55 and shrinking_55_100:
        score = weight
        fired = True
        detail["state"] = "BOTH_SHRINKING"
    elif shrinking_21_55 or shrinking_55_100:
        score = weight * 0.5
        fired = True
        detail["state"] = "ONE_SHRINKING"
    elif expanding_21_55 and expanding_55_100:
        # State recorded for the conditional bonus in detect_late_entry
        score = 0.0
        fired = False
        detail["state"] = "BOTH_EXPANDING"
    elif expanding_21_55 or expanding_55_100:
        score = 0.0
        fired = False
        detail["state"] = "ONE_EXPANDING"
    else:
        score = 0.0
        fired = False
        detail["state"] = "PARALLEL"

    return SignalDetail(fired, score, detail)


def _check_color_shift(df: pd.DataFrame, direction: str, weight: float) -> SignalDetail:
    """Count opposite-color candles in last N bars. Multiple = momentum decay."""
    lookback = CONFIG["color_shift_lookback"]
    n = len(df)
    if n < lookback:
        return SignalDetail(False, 0.0, {"reason": "insufficient data"})

    opposite = 0
    last_bars = df.iloc[-lookback:]
    for _, bar in last_bars.iterrows():
        is_green = bar["close"] > bar["open"]
        if direction == "bullish" and not is_green:
            opposite += 1
        elif direction == "bearish" and is_green:
            opposite += 1

    threshold = CONFIG["color_shift_count_threshold"]
    detail = {
        "lookback_bars": lookback,
        "opposite_color_count": opposite,
        "threshold": threshold,
    }

    if opposite >= threshold:
        # More opposite = stronger signal
        score = weight * min(1.0, opposite / lookback)
        fired = True
    else:
        score = 0.0
        fired = False

    return SignalDetail(fired, score, detail)


def _bars_since_e21_touch(df: pd.DataFrame, ema21: pd.Series, direction: str,
                          pair_pip: float, max_lookback: int = 40) -> int:
    """Walk back to find last bar where price wicked to E21 (pullback)."""
    n = len(df)
    touch_pad = CONFIG["touch_wick_pad_pips"] * pair_pip
    end = max(0, n - max_lookback)
    for i in range(n - 1, end - 1, -1):
        bar = df.iloc[i]
        e21 = ema21.iloc[i]
        if direction == "bullish":
            if bar["low"] <= e21 + touch_pad:
                return (n - 1) - i
        else:
            if bar["high"] >= e21 - touch_pad:
                return (n - 1) - i
    return max_lookback  # no touch in window


def _check_extension_exhaustion(df: pd.DataFrame, ema21: pd.Series, direction: str,
                                pair_pip: float, atr_pips: float,
                                weight: float) -> SignalDetail:
    """NEW: Detect stretched-from-E21 entries with no recent pullback.
    This is the dominant failure mode in the loss cohort — far from E21,
    fan still expanding, but mean reversion overdue."""
    bars_since_touch = _bars_since_e21_touch(df, ema21, direction, pair_pip)
    last_close = df["close"].iloc[-1]
    last_e21 = ema21.iloc[-1]
    if direction == "bullish":
        dist_pips = (last_close - last_e21) / pair_pip
    else:
        dist_pips = (last_e21 - last_close) / pair_pip
    dist_atr = dist_pips / max(atr_pips, 0.1)

    strong = CONFIG["extension_atr_threshold_strong"]
    extreme = CONFIG["extension_atr_threshold_extreme"]
    bars_min = CONFIG["extension_bars_no_touch_min"]

    detail = {
        "bars_since_e21_touch": bars_since_touch,
        "distance_pips": round(dist_pips, 2),
        "distance_atr": round(dist_atr, 2),
        "strong_threshold_atr": strong,
        "extreme_threshold_atr": extreme,
        "bars_threshold": bars_min,
    }

    if dist_atr >= extreme and bars_since_touch >= bars_min:
        score = weight
        fired = True
        detail["state"] = "EXTREME_EXTENSION"
    elif dist_atr >= strong and bars_since_touch >= bars_min:
        score = weight * 0.7
        fired = True
        detail["state"] = "STRONG_EXTENSION"
    elif dist_atr >= strong and bars_since_touch >= 4:
        score = weight * 0.4
        fired = True
        detail["state"] = "MODERATE_EXTENSION"
    else:
        score = 0.0
        fired = False
        detail["state"] = "OK"

    return SignalDetail(fired, score, detail)


def _check_bars_since_cross(bars_since: int, weight: float) -> SignalDetail:
    """8+ bars deep = mid-late, 13+ = very late."""
    very_late = CONFIG["bars_very_late_threshold"]
    late = CONFIG["bars_late_threshold"]

    detail = {"bars_since_fan_cross": bars_since,
              "late_threshold": late, "very_late_threshold": very_late}

    if bars_since >= very_late:
        score = weight
        fired = True
        detail["state"] = "VERY_LATE"
    elif bars_since >= late:
        score = weight * 0.6
        fired = True
        detail["state"] = "LATE"
    else:
        score = 0.0
        fired = False
        detail["state"] = "EARLY" if bars_since < 4 else "MID"

    return SignalDetail(fired, score, detail)


# ── MAIN ENTRY ───────────────────────────────────────────────────────

def detect_late_entry(
    df: pd.DataFrame,
    direction: str,
    pair_pip: float = 0.0001,
    ema21: pd.Series | None = None,
    ema55: pd.Series | None = None,
    ema100: pd.Series | None = None,
    bb_upper: pd.Series | None = None,
    bb_lower: pd.Series | None = None,
) -> LateEntryResult:
    """Run all structural signals and produce composite late-entry verdict.

    Args:
        df: M15 OHLC dataframe with columns: open, high, low, close. Most recent bar last.
        direction: "bullish" (proposing a BUY) or "bearish" (proposing a SELL).
        pair_pip: 0.0001 for non-JPY, 0.01 for JPY pairs.
        ema21/ema55/ema100/bb_upper/bb_lower: optional pre-computed indicators.
            If None, computed from df.close.
    """
    if direction not in ("bullish", "bearish"):
        raise ValueError(f"direction must be bullish or bearish, got {direction!r}")

    close = df["close"]
    if ema21 is None:
        ema21 = close.ewm(span=21, adjust=False).mean()
    if ema55 is None:
        ema55 = close.ewm(span=55, adjust=False).mean()
    if ema100 is None:
        ema100 = close.ewm(span=100, adjust=False).mean()
    if bb_upper is None or bb_lower is None:
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        bb_upper = bb_mid + 2.0 * bb_std
        bb_lower = bb_mid - 2.0 * bb_std

    atr_pips = _atr_pips(df, pair_pip)
    bars_since = _bars_since_fan_cross(ema21, ema55, ema100, direction)

    w = CONFIG["weights"]
    signals = {
        "ema21_bend":           _check_ema_bend(ema21, direction, pair_pip, w["ema21_bend"], "E21"),
        "ema55_bend":           _check_ema_bend(ema55, direction, pair_pip, w["ema55_bend"], "E55"),
        "bb_band_bend":         _check_bb_bend(bb_upper, bb_lower, direction, pair_pip, w["bb_band_bend"]),
        "candle_e21":           _check_candle_vs_e21(df, ema21, direction, pair_pip, atr_pips, w["candle_e21"]),
        "fan_expansion":        _check_fan_expansion(ema21, ema55, ema100, direction, w["fan_shrinking"]),
        "color_shift":          _check_color_shift(df, direction, w["color_shift"]),
        "bars_late":            _check_bars_since_cross(bars_since, w["bars_late"]),
        "extension_exhaustion": _check_extension_exhaustion(df, ema21, direction, pair_pip, atr_pips, w["extension_exhaustion"]),
    }

    # Composite score
    raw_score = sum(s.score for s in signals.values())

    # Continuation bonus ONLY when fan expanding AND price still close to E21.
    # If price is already stretched far from E21, expansion doesn't help — it's
    # an extension trade not a continuation trade.
    fan_state = signals["fan_expansion"].detail.get("state", "")
    ext_state = signals["extension_exhaustion"].detail.get("state", "OK")
    if "EXPANDING" in fan_state and ext_state == "OK":
        raw_score += CONFIG["expansion_bonus_when_close"]

    # Clamp to 0-100
    score = max(0.0, min(100.0, raw_score))

    # Verdict
    if score < CONFIG["early_max"]:
        verdict = "EARLY"
    elif score < CONFIG["mid_max"]:
        verdict = "MID"
    else:
        verdict = "LATE"

    # Build human-readable reasons
    reasons = []
    for name, sig in signals.items():
        if sig.fired:
            d = sig.detail
            if name == "ema21_bend":
                reasons.append(f"E21 slope bent: {d.get('slope_prev_pips_per_bar')}→{d.get('slope_now_pips_per_bar')} pips/bar")
            elif name == "ema55_bend":
                reasons.append(f"E55 slope bent: {d.get('slope_prev_pips_per_bar')}→{d.get('slope_now_pips_per_bar')} pips/bar")
            elif name == "bb_band_bend":
                if d.get('upper_bent') and d.get('lower_bent'):
                    reasons.append("BOTH BB bands bending in (squeeze starting)")
                else:
                    reasons.append(f"BB {'upper' if d.get('upper_bent') else 'lower'} band bent")
            elif name == "candle_e21":
                if d.get('breaks', 0) > 0:
                    reasons.append(f"{d['breaks']} candle(s) closed wrong side of E21 — regime risk")
                else:
                    reasons.append(f"{d.get('touches')} candle(s) touched E21 in last {d.get('lookback_bars')} bars")
            elif name == "fan_expansion":
                reasons.append(f"Fan {d.get('state','?')}")
            elif name == "color_shift":
                reasons.append(f"{d.get('opposite_color_count')} opposite-color candles in last {d.get('lookback_bars')}")
            elif name == "bars_late":
                reasons.append(f"{d.get('bars_since_fan_cross')} bars since fan cross ({d.get('state')})")
            elif name == "extension_exhaustion":
                reasons.append(f"{d.get('state')}: {d.get('distance_atr')}× ATR from E21, {d.get('bars_since_e21_touch')} bars without pullback")

    # Continuation indicators (negative contributors)
    if "EXPANDING" in fan_state and ext_state == "OK":
        reasons.append(f"Fan {fan_state} + price close to E21 (continuation OK — bonus applied)")

    return LateEntryResult(
        score=score,
        verdict=verdict,
        direction=direction,
        signals=signals,
        reasons=reasons,
        bars_since_fan_cross=bars_since,
        atr_pips=atr_pips,
    )
