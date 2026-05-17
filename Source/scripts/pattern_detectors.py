"""pattern_detectors.py — Deterministic detectors for patterns in
<repo_root>/Skills/pattern_library.md.

Each detector returns either None (no fire) or a dict:
    {"pattern_id": str, "name": str, "bar_idx": int, "color": str, "details": dict}

All detectors are individually tunable via DETECTOR_ENABLED. Caller passes
in OHLC dataframe + optional BB/RSI/MACD series; detectors read what they need.

The detector logic mirrors the **Detection** lines in pattern_library.md
verbatim where possible. When the file gives a Python predicate (e.g. the
engulfing entries), the predicate is implemented as-written.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ── INDIVIDUAL DETECTOR ENABLE FLAGS ─────────────────────────────────
# Toggle these to isolate impact during iteration. All True = full pipeline.
DETECTOR_ENABLED = {
    "engulfing_bullish": True,   # pattern_02
    "engulfing_bearish": True,   # pattern_03
    "hammer":            True,   # pattern_01 bullish hammer
    "shooting_star":     True,   # pattern_01 bearish star
    "doji_extreme":      True,   # pattern_05
    "bb_squeeze":        True,   # pattern_10 — Tim's #1
    "ascending_triangle":True,   # pattern_06
    "descending_triangle":True,  # pattern_07
    "channel":           True,   # pattern_08
    "morning_star":      True,   # pattern_04 bullish
    "evening_star":      True,   # pattern_04 bearish
}

# Colors for chart annotations (matplotlib hex)
_BULL_COLOR = "#3fb950"
_BEAR_COLOR = "#ef5350"
_NEUTRAL    = "#94a3b8"


# ── HELPERS ──────────────────────────────────────────────────────────

def _body(o: float, c: float) -> float:
    return abs(c - o)


def _upper_wick(o: float, h: float, c: float) -> float:
    return h - max(o, c)


def _lower_wick(o: float, l: float, c: float) -> float:
    return min(o, c) - l


def _total_range(h: float, l: float) -> float:
    return max(h - l, 1e-12)


def _is_green(o: float, c: float) -> bool:
    return c > o


def _is_red(o: float, c: float) -> bool:
    return c < o


# ── PATTERN_01 HAMMER / PIN BAR ──────────────────────────────────────
# pattern_library.md: "wick ≥ 2× body on one side; opposite wick < body;
# at swing extreme or near E55/E100."

def detect_hammer(df: pd.DataFrame, look_back: int = 2) -> dict | None:
    """Bullish hammer in the last `look_back` bars (only most recent)."""
    if not DETECTOR_ENABLED["hammer"]:
        return None
    for i in range(len(df) - look_back, len(df)):
        if i < 0:
            continue
        r = df.iloc[i]
        body = _body(r["open"], r["close"])
        if body < 1e-9:
            continue
        lw = _lower_wick(r["open"], r["low"], r["close"])
        uw = _upper_wick(r["open"], r["high"], r["close"])
        if lw >= 2 * body and uw < body:
            return {"pattern_id": "pattern_01",
                    "name": "Hammer / Pin Bar (bullish)",
                    "bar_idx": i, "color": _BULL_COLOR,
                    "details": {"body": body, "lower_wick": lw, "upper_wick": uw}}
    return None


def detect_shooting_star(df: pd.DataFrame, look_back: int = 2) -> dict | None:
    """Bearish shooting star in the last `look_back` bars (only most recent)."""
    if not DETECTOR_ENABLED["shooting_star"]:
        return None
    for i in range(len(df) - look_back, len(df)):
        if i < 0:
            continue
        r = df.iloc[i]
        body = _body(r["open"], r["close"])
        if body < 1e-9:
            continue
        uw = _upper_wick(r["open"], r["high"], r["close"])
        lw = _lower_wick(r["open"], r["low"], r["close"])
        if uw >= 2 * body and lw < body:
            return {"pattern_id": "pattern_01",
                    "name": "Shooting Star (bearish)",
                    "bar_idx": i, "color": _BEAR_COLOR,
                    "details": {"body": body, "upper_wick": uw, "lower_wick": lw}}
    return None


# ── PATTERN_02 BULLISH ENGULFING ─────────────────────────────────────
# pattern_library.md: "open_now ≤ close_prev AND close_now ≥ open_prev.
# Second candle green, first red."

def detect_engulfing_bullish(df: pd.DataFrame, look_back: int = 2) -> dict | None:
    if not DETECTOR_ENABLED["engulfing_bullish"]:
        return None
    for i in range(max(1, len(df) - look_back), len(df)):
        p, n = df.iloc[i - 1], df.iloc[i]
        if (_is_red(p["open"], p["close"])
                and _is_green(n["open"], n["close"])
                and n["open"] <= p["close"]
                and n["close"] >= p["open"]):
            return {"pattern_id": "pattern_02",
                    "name": "Bullish Engulfing",
                    "bar_idx": i, "color": _BULL_COLOR, "details": {}}
    return None


# ── PATTERN_03 BEARISH ENGULFING ─────────────────────────────────────
# pattern_library.md: "open_now ≥ close_prev AND close_now ≤ open_prev.
# Second candle red, first green."

def detect_engulfing_bearish(df: pd.DataFrame, look_back: int = 2) -> dict | None:
    if not DETECTOR_ENABLED["engulfing_bearish"]:
        return None
    for i in range(max(1, len(df) - look_back), len(df)):
        p, n = df.iloc[i - 1], df.iloc[i]
        if (_is_green(p["open"], p["close"])
                and _is_red(n["open"], n["close"])
                and n["open"] >= p["close"]
                and n["close"] <= p["open"]):
            return {"pattern_id": "pattern_03",
                    "name": "Bearish Engulfing",
                    "bar_idx": i, "color": _BEAR_COLOR, "details": {}}
    return None


# ── PATTERN_04 MORNING / EVENING STAR ────────────────────────────────
# pattern_library.md: "3-bar window with the size pattern big-small-big.
# Morning star: large red → small body/doji → large green closing above
# midpoint of first. Evening star: mirror."

def _avg_body(df: pd.DataFrame, end_idx: int, n: int = 14) -> float:
    start = max(0, end_idx - n + 1)
    sub = df.iloc[start:end_idx + 1]
    bodies = (sub["close"] - sub["open"]).abs()
    return float(bodies.mean()) if len(bodies) else 0.0


def detect_morning_star(df: pd.DataFrame, look_back: int = 5) -> dict | None:
    if not DETECTOR_ENABLED["morning_star"]:
        return None
    for i in range(max(2, len(df) - look_back), len(df)):
        a, b, c = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        avg = _avg_body(df, i - 3) or 1e-9
        big_a = _body(a["open"], a["close"]) > avg
        small_b = _body(b["open"], b["close"]) < avg * 0.5
        big_c = _body(c["open"], c["close"]) > avg
        mid_a = (a["open"] + a["close"]) / 2
        if (_is_red(a["open"], a["close"]) and big_a and small_b
                and _is_green(c["open"], c["close"]) and big_c
                and c["close"] > mid_a):
            return {"pattern_id": "pattern_04",
                    "name": "Morning Star",
                    "bar_idx": i, "color": _BULL_COLOR, "details": {}}
    return None


def detect_evening_star(df: pd.DataFrame, look_back: int = 5) -> dict | None:
    if not DETECTOR_ENABLED["evening_star"]:
        return None
    for i in range(max(2, len(df) - look_back), len(df)):
        a, b, c = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        avg = _avg_body(df, i - 3) or 1e-9
        big_a = _body(a["open"], a["close"]) > avg
        small_b = _body(b["open"], b["close"]) < avg * 0.5
        big_c = _body(c["open"], c["close"]) > avg
        mid_a = (a["open"] + a["close"]) / 2
        if (_is_green(a["open"], a["close"]) and big_a and small_b
                and _is_red(c["open"], c["close"]) and big_c
                and c["close"] < mid_a):
            return {"pattern_id": "pattern_04",
                    "name": "Evening Star",
                    "bar_idx": i, "color": _BEAR_COLOR, "details": {}}
    return None


# ── PATTERN_05 DOJI AT EXTREME ───────────────────────────────────────
# pattern_library.md: "body ≤ 10% of total range. Variants: Dragonfly,
# Gravestone, Long-legged, Standard. Context matters: doji at BB extreme /
# RSI extreme / swing high-low = real signal."

def detect_doji_extreme(df: pd.DataFrame, bb_upper=None, bb_lower=None,
                        look_back: int = 3) -> dict | None:
    """Tightened per pattern_library.md 'Context matters: doji at BB extreme /
    RSI extreme / swing high-low = real signal. Doji mid-range = noise.'
    Now requires BOTH at BB band AND at recent swing extreme (last 10) — not
    either-or. Reduces false-positive fires on minor doji bars.
    Also color-tagged: gravestone=bearish, dragonfly=bullish (was neutral)."""
    if not DETECTOR_ENABLED["doji_extreme"]:
        return None
    for i in range(len(df) - look_back, len(df)):
        if i < 0:
            continue
        r = df.iloc[i]
        body = _body(r["open"], r["close"])
        total = _total_range(r["high"], r["low"])
        if body / total > 0.10:
            continue
        if bb_upper is None or bb_lower is None or i >= len(bb_upper):
            continue
        bu, bl = bb_upper[i], bb_lower[i]
        if np.isnan(bu) or np.isnan(bl):
            continue
        recent = df.iloc[max(0, i - 10):i + 1]
        at_swing_high = r["high"] >= recent["high"].max() * 0.9999
        at_swing_low = r["low"] <= recent["low"].min() * 1.0001
        # Require BB extreme AND swing extreme on the matching side
        if r["high"] >= bu * 0.999 and at_swing_high:
            return {"pattern_id": "pattern_05",
                    "name": "Doji at Extreme (gravestone)",
                    "bar_idx": i, "color": _BEAR_COLOR,
                    "details": {"variant": "gravestone"}}
        if r["low"] <= bl * 1.001 and at_swing_low:
            return {"pattern_id": "pattern_05",
                    "name": "Doji at Extreme (dragonfly)",
                    "bar_idx": i, "color": _BULL_COLOR,
                    "details": {"variant": "dragonfly"}}
    return None


# ── PATTERN_06 ASCENDING TRIANGLE ────────────────────────────────────
# pattern_library.md: "flat horizontal top (resistance), higher lows
# compressing into it. Detection: flat resistance (3+ tests), higher lows
# compressing into it."

def _swing_points(df: pd.DataFrame, window: int = 4, last_n: int = 50):
    """Returns (highs, lows) — each a list of (bar_idx, price) for the last `last_n` bars.
    Tighter window (50 bars = ~12.5h on M15) reduces false-positive triangle/channel firings."""
    n = len(df)
    last_n = min(n, last_n)
    start = max(window, n - last_n)
    highs, lows = [], []
    for i in range(start, n - window):
        h = df["high"].iloc[i]
        if all(h > df["high"].iloc[i - k] for k in range(1, window + 1)) and \
           all(h > df["high"].iloc[i + k] for k in range(1, window + 1)):
            highs.append((i, float(h)))
        l = df["low"].iloc[i]
        if all(l < df["low"].iloc[i - k] for k in range(1, window + 1)) and \
           all(l < df["low"].iloc[i + k] for k in range(1, window + 1)):
            lows.append((i, float(l)))
    return highs, lows


def detect_ascending_triangle(df: pd.DataFrame) -> dict | None:
    """Tightened per research: need 4+ flat-top tests within 0.15%, lows
    STRICTLY rising across last 3 (not just last 2). Reduces false positives
    that fire on any 3 high points that happen to roughly align."""
    if not DETECTOR_ENABLED["ascending_triangle"]:
        return None
    highs, lows = _swing_points(df)
    if len(highs) < 4 or len(lows) < 3:
        return None
    last_highs = highs[-4:]
    high_prices = [p for _, p in last_highs]
    mean_h = sum(high_prices) / len(high_prices)
    if not all(abs(p - mean_h) / mean_h < 0.0015 for p in high_prices):
        return None
    # Lows STRICTLY rising over last 3 swings (each higher than prior)
    last_lows = lows[-3:]
    low_prices = [p for _, p in last_lows]
    if not (low_prices[0] < low_prices[1] < low_prices[2]):
        return None
    return {"pattern_id": "pattern_06",
            "name": "Ascending Triangle",
            "bar_idx": last_highs[-1][0], "color": _BULL_COLOR,
            "details": {"flat_top": mean_h, "low_progression": low_prices}}


def detect_descending_triangle(df: pd.DataFrame) -> dict | None:
    """Mirror of ascending — tightened to 4+ flat-bottom tests within 0.15%,
    highs STRICTLY falling across last 3."""
    if not DETECTOR_ENABLED["descending_triangle"]:
        return None
    highs, lows = _swing_points(df)
    if len(lows) < 4 or len(highs) < 3:
        return None
    last_lows = lows[-4:]
    low_prices = [p for _, p in last_lows]
    mean_l = sum(low_prices) / len(low_prices)
    if not all(abs(p - mean_l) / mean_l < 0.0015 for p in low_prices):
        return None
    last_highs = highs[-3:]
    high_prices = [p for _, p in last_highs]
    if not (high_prices[0] > high_prices[1] > high_prices[2]):
        return None
    return {"pattern_id": "pattern_07",
            "name": "Descending Triangle",
            "bar_idx": last_lows[-1][0], "color": _BEAR_COLOR,
            "details": {"flat_bottom": mean_l, "high_progression": high_prices}}


# ── PATTERN_08 CHANNEL TRADING ───────────────────────────────────────
# pattern_library.md: "price oscillating between parallel support + resistance lines"

def detect_channel(df: pd.DataFrame) -> dict | None:
    if not DETECTOR_ENABLED["channel"]:
        return None
    highs, lows = _swing_points(df)
    if len(highs) < 2 or len(lows) < 2:
        return None
    # Slope of top trendline vs slope of bottom trendline
    top_slope = (highs[-1][1] - highs[0][1]) / max(highs[-1][0] - highs[0][0], 1)
    bot_slope = (lows[-1][1] - lows[0][1]) / max(lows[-1][0] - lows[0][0], 1)
    # Parallel if slopes within 20% of each other and neither is near zero
    if abs(top_slope) < 1e-7 or abs(bot_slope) < 1e-7:
        return None
    ratio = top_slope / bot_slope
    if 0.8 <= ratio <= 1.2:
        return {"pattern_id": "pattern_08",
                "name": "Channel Trading",
                "bar_idx": highs[-1][0], "color": _NEUTRAL,
                "details": {"top_slope": top_slope, "bot_slope": bot_slope}}
    return None


# ── PATTERN_10 BB SQUEEZE BREAKOUT ───────────────────────────────────
# pattern_library.md: "bandwidth < 50% of 20-bar average sustained ≥10 bars,
# then price closes beyond band by ≥ 0.5 × current bandwidth, EMA fan aligned."

def detect_bb_squeeze(df: pd.DataFrame, bb_upper, bb_lower, bb_mid,
                      ema21=None, ema55=None, ema100=None,
                      look_back: int = 5) -> dict | None:
    if not DETECTOR_ENABLED["bb_squeeze"]:
        return None
    if bb_upper is None or bb_lower is None or len(bb_upper) < 30:
        return None
    bandwidth = np.array(bb_upper) - np.array(bb_lower)
    bw_avg = pd.Series(bandwidth).rolling(20).mean().values
    # Find a squeeze: bandwidth < 50% of 20-bar avg for 10+ consecutive bars
    # then a breakout candle within the last `look_back` bars.
    for i in range(len(df) - look_back, len(df)):
        if i < 30:
            continue
        # Check sustained squeeze ending at i-1
        squeeze_window = bandwidth[i - 11:i - 1]
        avg_window = bw_avg[i - 11:i - 1]
        if any(np.isnan(avg_window)):
            continue
        if not all(sw < aw * 0.5 for sw, aw in zip(squeeze_window, avg_window)):
            continue
        # Breakout: close beyond band by ≥ 0.5 × current bandwidth
        c = df["close"].iloc[i]
        bu, bl, cur_bw = bb_upper[i], bb_lower[i], bandwidth[i]
        if c >= bu + 0.5 * cur_bw:
            # Up-break — check fan aligned bullish
            if ema21 is not None and ema21[i] > ema55[i] > ema100[i]:
                return {"pattern_id": "pattern_10",
                        "name": "BB Squeeze Breakout (UP)",
                        "bar_idx": i, "color": _BULL_COLOR,
                        "details": {"bandwidth": cur_bw}}
        if c <= bl - 0.5 * cur_bw:
            if ema21 is not None and ema21[i] < ema55[i] < ema100[i]:
                return {"pattern_id": "pattern_10",
                        "name": "BB Squeeze Breakout (DOWN)",
                        "bar_idx": i, "color": _BEAR_COLOR,
                        "details": {"bandwidth": cur_bw}}
    return None


# ── INDICATOR-CONTEXT ENRICHMENT ─────────────────────────────────────
# Per the trading research (Bulkowski + 7 sources), patterns alone are not
# sufficient — they need indicator context: location (near key EMA / S-R),
# momentum state (RSI, BB position), trend alignment (in-trend vs counter-trend),
# confirmation candle, and invalidation status. This function computes all of
# that for each fire so the validator prompt can present pattern + evidence.

def _pip_factor(pair_hint: str | None, df: pd.DataFrame) -> float:
    """Return 100 for JPY pairs, 10000 otherwise. If pair unknown, infer from price magnitude."""
    if pair_hint:
        return 100.0 if "JPY" in pair_hint.upper() else 10000.0
    sample = df["close"].iloc[-1]
    return 100.0 if sample > 10 else 10000.0


def _bb_position(price: float, bu, bl, bm) -> str:
    if np.isnan(bu) or np.isnan(bl) or np.isnan(bm):
        return "unknown"
    if price >= bu * 0.9995:
        return "at_upper_band"
    if price <= bl * 1.0005:
        return "at_lower_band"
    if price > bm:
        return "upper_half"
    if price < bm:
        return "lower_half"
    return "middle"


def _rsi_zone(rsi: float) -> str:
    if np.isnan(rsi): return "unknown"
    if rsi >= 70: return "overbought"
    if rsi <= 30: return "oversold"
    return "neutral"


def _is_at_swing_extreme(df: pd.DataFrame, bar_idx: int, window: int = 10,
                         direction: str = "either") -> bool:
    """True if bar_idx is at the 10-bar high (or low) of the window ending at bar_idx."""
    start = max(0, bar_idx - window)
    sub = df.iloc[start:bar_idx + 1]
    r = df.iloc[bar_idx]
    if direction in ("high", "either") and r["high"] >= sub["high"].max() * 0.9999:
        return True
    if direction in ("low", "either") and r["low"] <= sub["low"].min() * 1.0001:
        return True
    return False


def _trend_alignment(fire_color: str, fan_direction: str, phase: int) -> str:
    """Compare pattern bias to current trend (fan direction + phase)."""
    is_bull_pattern = fire_color == _BULL_COLOR
    is_bear_pattern = fire_color == _BEAR_COLOR
    if not is_bull_pattern and not is_bear_pattern:
        return "NEUTRAL"
    if phase < 2:
        return "EARLY_FORMATION"  # no clear trend to align against
    bull_trend = fan_direction == "bullish"
    bear_trend = fan_direction == "bearish"
    if is_bull_pattern and bull_trend:
        return "IN_TREND_BULL"
    if is_bear_pattern and bear_trend:
        return "IN_TREND_BEAR"
    if is_bull_pattern and bear_trend:
        return "COUNTER_TREND_BULL_VS_BEAR_FAN"
    if is_bear_pattern and bull_trend:
        return "COUNTER_TREND_BEAR_VS_BULL_FAN"
    return "MIXED"


def _confirmation_status(df: pd.DataFrame, bar_idx: int, pattern_bias_bullish: bool) -> str:
    """Check the bar immediately AFTER bar_idx to see if it confirmed the pattern.

    For bullish patterns: confirmation = bar+1 closes ABOVE bar_idx high.
    For bearish patterns: confirmation = bar+1 closes BELOW bar_idx low.
    """
    if bar_idx + 1 >= len(df):
        return "N/A_last_bar"
    next_bar = df.iloc[bar_idx + 1]
    fire_bar = df.iloc[bar_idx]
    if pattern_bias_bullish:
        return "confirmed" if next_bar["close"] > fire_bar["high"] else "not_confirmed"
    return "confirmed" if next_bar["close"] < fire_bar["low"] else "not_confirmed"


def _invalidation_status(df: pd.DataFrame, fire: dict, pattern_bias_bullish: bool) -> str:
    """Check if any bar AFTER the fire bar has closed through the invalidation level.

    Bullish pattern: invalidation = close below fire bar's low.
    Bearish pattern: invalidation = close above fire bar's high.
    """
    bar_idx = fire["bar_idx"]
    if bar_idx + 1 >= len(df):
        return "N/A_last_bar"
    fire_bar = df.iloc[bar_idx]
    after = df.iloc[bar_idx + 1:]
    if pattern_bias_bullish:
        if (after["close"] < fire_bar["low"]).any():
            return "invalidated"
        return "still_valid"
    else:
        if (after["close"] > fire_bar["high"]).any():
            return "invalidated"
        return "still_valid"


def enrich_with_context(fire: dict, df: pd.DataFrame,
                        bb_upper, bb_lower, bb_mid,
                        ema21, ema55, ema100,
                        rsi_series,
                        fan_direction: str,
                        phase: int,
                        pair_hint: str | None = None) -> dict:
    """Attach indicator context to a single fire dict. Returns enriched fire."""
    bi = fire["bar_idx"]
    if bi < 0 or bi >= len(df):
        return fire
    pf = _pip_factor(pair_hint, df)
    close = float(df["close"].iloc[bi])

    def _dist(ema_arr):
        if ema_arr is None or bi >= len(ema_arr): return None
        v = ema_arr[bi]
        return None if np.isnan(v) else round((close - v) * pf, 1)

    d21, d55, d100 = _dist(ema21), _dist(ema55), _dist(ema100)
    nearest_ema = None
    if d21 is not None:
        # Smallest absolute distance with name
        candidates = [(abs(d21), "E21", d21), (abs(d55), "E55", d55), (abs(d100), "E100", d100)]
        candidates = [c for c in candidates if c[2] is not None]
        if candidates:
            nearest_ema = min(candidates, key=lambda c: c[0])[1] if candidates[0][0] < 8 else None

    bb_pos = "unknown"
    if bb_upper is not None and bi < len(bb_upper):
        bb_pos = _bb_position(close, bb_upper[bi], bb_lower[bi], bb_mid[bi])

    rsi_val = float(rsi_series[bi]) if rsi_series is not None and bi < len(rsi_series) and not np.isnan(rsi_series[bi]) else None
    rsi_z = _rsi_zone(rsi_val) if rsi_val is not None else "unknown"

    pattern_bias_bullish = fire.get("color") == _BULL_COLOR
    pattern_bias_bearish = fire.get("color") == _BEAR_COLOR

    trend_align = _trend_alignment(fire.get("color"), fan_direction, phase)
    conf_status = _confirmation_status(df, bi, pattern_bias_bullish) if pattern_bias_bearish or pattern_bias_bullish else "N/A_neutral"
    invld_status = _invalidation_status(df, fire, pattern_bias_bullish) if pattern_bias_bearish or pattern_bias_bullish else "N/A_neutral"
    swing_ext = _is_at_swing_extreme(df, bi)

    fire["context"] = {
        "distance_e21_pips": d21,
        "distance_e55_pips": d55,
        "distance_e100_pips": d100,
        "nearest_ema_within_8pips": nearest_ema,
        "bb_position": bb_pos,
        "rsi_at_fire": round(rsi_val, 1) if rsi_val is not None else None,
        "rsi_zone": rsi_z,
        "trend_alignment": trend_align,
        "confirmation_status": conf_status,
        "invalidation_status": invld_status,
        "at_swing_extreme": swing_ext,
        "fire_bar_close": round(close, 5),
    }
    return fire


# ── RUN ALL ──────────────────────────────────────────────────────────

def detect_all(df: pd.DataFrame, bb_upper=None, bb_lower=None, bb_mid=None,
               ema21=None, ema55=None, ema100=None,
               rsi_series=None, fan_direction: str = "mixed",
               phase: int = 0, pair_hint: str | None = None) -> list:
    """Run every enabled detector with mutual-exclusion rules.

    Rules to suppress conflicting noise:
    1. If both bullish AND bearish engulfing fire, keep only the more recent.
    2. If both ascending AND descending triangle fire, suppress both (chart is mixed).
    3. If a triangle fires AND channel fires, prefer the triangle (more specific).
    4. If hammer AND shooting star fire, keep only the more recent.
    """
    raw = []
    for fn in (
        lambda: detect_engulfing_bullish(df),
        lambda: detect_engulfing_bearish(df),
        lambda: detect_hammer(df),
        lambda: detect_shooting_star(df),
        lambda: detect_morning_star(df),
        lambda: detect_evening_star(df),
        lambda: detect_doji_extreme(df, bb_upper, bb_lower),
        lambda: detect_ascending_triangle(df),
        lambda: detect_descending_triangle(df),
        lambda: detect_channel(df),
        lambda: detect_bb_squeeze(df, bb_upper, bb_lower, bb_mid,
                                  ema21, ema55, ema100),
    ):
        try:
            r = fn()
            if r is not None:
                raw.append(r)
        except Exception:
            continue

    by_id = {f["pattern_id"]: f for f in raw}
    # Rule 1: bull vs bear engulfing
    if "pattern_02" in by_id and "pattern_03" in by_id:
        keep = by_id["pattern_02"] if by_id["pattern_02"]["bar_idx"] > by_id["pattern_03"]["bar_idx"] else by_id["pattern_03"]
        drop = "pattern_03" if keep["pattern_id"] == "pattern_02" else "pattern_02"
        del by_id[drop]
    # Rule 2: both triangles = chart mixed, drop both
    if "pattern_06" in by_id and "pattern_07" in by_id:
        del by_id["pattern_06"]
        del by_id["pattern_07"]
    # Rule 3: triangle present + channel = drop channel
    if "pattern_08" in by_id and ("pattern_06" in by_id or "pattern_07" in by_id):
        del by_id["pattern_08"]
    # Rule 4: hammer vs shooting star (same pattern_id "pattern_01" — keep most recent)
    hammers = [f for f in raw if f.get("name", "").startswith("Hammer")]
    stars = [f for f in raw if f.get("name", "").startswith("Shooting")]
    if hammers and stars:
        most_recent = max(hammers + stars, key=lambda f: f["bar_idx"])
        by_id["pattern_01"] = most_recent

    # Rule 5 (NEW): multi-bar structure dominates single-bar when bias OPPOSES.
    # Per Bulkowski / trading research: "Multi-bar patterns are stronger
    # indicators than single-bar." When a triangle/channel fires bullish and
    # a single-bar pattern fires bearish (or vice versa), drop the single-bar
    # to avoid conflicting signals confusing the validator.
    SINGLE_BAR_IDS = {"pattern_01", "pattern_02", "pattern_03", "pattern_04", "pattern_05"}
    MULTI_BAR_IDS  = {"pattern_06", "pattern_07", "pattern_08", "pattern_10"}
    multi_bar_fires = [f for pid, f in by_id.items() if pid in MULTI_BAR_IDS]
    if multi_bar_fires:
        # Determine dominant multi-bar bias (most recent multi-bar fire)
        dominant = max(multi_bar_fires, key=lambda f: f["bar_idx"])
        dom_bias = dominant.get("color")
        if dom_bias in (_BULL_COLOR, _BEAR_COLOR):
            opposite = _BEAR_COLOR if dom_bias == _BULL_COLOR else _BULL_COLOR
            # Drop any single-bar pattern with the opposite bias
            for pid in list(by_id.keys()):
                if pid in SINGLE_BAR_IDS and by_id[pid].get("color") == opposite:
                    del by_id[pid]

    # Rule 6 (NEW): any two single-bar patterns with opposite biases —
    # keep only the most recent (more recent = more relevant to current state).
    bull_single = [f for pid, f in by_id.items() if pid in SINGLE_BAR_IDS and f.get("color") == _BULL_COLOR]
    bear_single = [f for pid, f in by_id.items() if pid in SINGLE_BAR_IDS and f.get("color") == _BEAR_COLOR]
    if bull_single and bear_single:
        all_single = bull_single + bear_single
        most_recent = max(all_single, key=lambda f: f["bar_idx"])
        # Drop opposite-bias single-bars
        for pid in list(by_id.keys()):
            f = by_id[pid]
            if pid in SINGLE_BAR_IDS and f.get("color") != most_recent.get("color") and f is not most_recent:
                # Only drop if the keep candidate is bull/bear (skip neutrals)
                if most_recent.get("color") in (_BULL_COLOR, _BEAR_COLOR):
                    del by_id[pid]

    # Enrich every surviving fire with indicator context (trend alignment,
    # location, confirmation, invalidation, RSI/BB at fire bar).
    enriched = []
    for fire in by_id.values():
        try:
            enriched.append(enrich_with_context(
                fire, df, bb_upper, bb_lower, bb_mid,
                ema21, ema55, ema100, rsi_series,
                fan_direction, phase, pair_hint,
            ))
        except Exception:
            enriched.append(fire)

    # Final filter pass — drop fires that fail the Bulkowski-style
    # confirmation / invalidation rules. These are NOISE under established
    # trading practice; firing them just confuses the validator.
    #
    # Rules applied:
    # 1. Candle-based reversal patterns (hammer, shooting star, engulfing,
    #    star) REQUIRE next-bar confirmation per pattern_library.md
    #    ("next candle close past the body in reversal direction").
    #    confirmation_status == "not_confirmed" → drop.
    #    confirmation_status == "N/A_last_bar" → keep (fire bar IS the last
    #    bar, cannot judge yet — let the model see it).
    # 2. Any pattern with invalidation_status == "invalidated" is DEAD —
    #    drop unconditionally.
    CONFIRM_REQUIRED = {"pattern_01", "pattern_02", "pattern_03", "pattern_04"}
    survivors = []
    for f in enriched:
        ctx = f.get("context", {})
        pid = f.get("pattern_id")
        if ctx.get("invalidation_status") == "invalidated":
            continue
        if pid in CONFIRM_REQUIRED and ctx.get("confirmation_status") == "not_confirmed":
            continue
        survivors.append(f)
    return survivors


def _candles_to_df(candles) -> pd.DataFrame | None:
    """Normalize OANDA-shaped M15 candles (or a DataFrame) to OHLC DataFrame."""
    if isinstance(candles, pd.DataFrame):
        return candles if len(candles) >= 20 else None
    if not candles:
        return None
    rows = []
    for c in candles:
        try:
            mid = c.get("mid", {}) if isinstance(c.get("mid"), dict) else {}
            o = mid.get("o", c.get("open"))
            h = mid.get("h", c.get("high"))
            l = mid.get("l", c.get("low"))
            cl = mid.get("c", c.get("close"))
            if o is None or cl is None:
                continue
            rows.append({
                "time": c.get("time", ""),
                "open": float(o),
                "high": float(h) if h is not None else float(o),
                "low": float(l) if l is not None else float(o),
                "close": float(cl),
            })
        except (TypeError, ValueError):
            continue
    if len(rows) < 20:
        return None
    return pd.DataFrame(rows)


def detect_patterns_for_validator(
    m15_candles,
    fan_direction: str = "mixed",
    phase: int = 0,
    pair_hint: str | None = None,
) -> list:
    """Live entry point used by trading_cycle.py at validator section build time.

    Takes raw OANDA-shaped M15 candle list (or pre-built DataFrame), computes
    BB/EMA/RSI series from the closes, runs detect_all (which already
    enriches + filters confirmation/invalidation), returns the final
    enriched fires list ready to pass to build_pattern_section.

    Returns [] on insufficient data or any error so the caller can skip
    appending an empty pattern section.
    """
    df = _candles_to_df(m15_candles)
    if df is None:
        return []
    close = df["close"]
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_mid + 2.0 * bb_std
    bb_lower = bb_mid - 2.0 * bb_std
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema55 = close.ewm(span=55, adjust=False).mean()
    ema100 = close.ewm(span=100, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi_series = (100 - 100 / (1 + rs)).fillna(50)
    return detect_all(
        df,
        bb_upper=bb_upper, bb_lower=bb_lower, bb_mid=bb_mid,
        ema21=ema21, ema55=ema55, ema100=ema100,
        rsi_series=rsi_series,
        fan_direction=fan_direction or "mixed",
        phase=phase or 0,
        pair_hint=pair_hint,
    )
