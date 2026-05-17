"""snipe_retrace_gate.py — snipe-trigger-time gate that blocks entries when a
peak separation exit marker has just appeared AND the fan is actively compressing
(retrace in progress).

The thesis: if the ⚠ Exit↓/↑ marker appears within the last few bars of the live
candle AND the fan is compressing, the impulse has just peaked. Entering a snipe
at this moment means entering INTO the retrace — guaranteed underwater.

Detection logic:
  1. Run format_chart_signals → find peak_sep markers
  2. Most recent marker must be within last NEAR_LIVE_BARS bars
  3. Fan velocity (separation_velocity_pct_per_bar) must be NEGATIVE (compressing)
  4. If both conditions: BLOCK

Public API:
  check_snipe_retrace_gate(candles, direction) -> dict
"""
import pandas as pd

from backtester.ema_separation import format_chart_signals
from scripts.build_cohort_indicators import derive_fan_state
from indicators import Indicators

# Marker must be within the last N candles of the live bar to count as "fresh"
NEAR_LIVE_BARS = 5

# Fan velocity must be at least this negative to count as actively retracing
RETRACE_VELOCITY_MAX = -0.001  # pct/bar — anything below = compressing


def _canon_time(t):
    if isinstance(t, str):
        return t
    return t.isoformat() if hasattr(t, "isoformat") else str(t)


def check_snipe_retrace_gate(candles: list, direction: str) -> dict:
    """Block snipe if peak_sep marker fired within last NEAR_LIVE_BARS bars
    AND fan is actively compressing (retrace in progress).

    Returns dict: {'block': bool, 'reason': str, 'data': {...}}.
    Fails open on any error — never raises.
    """
    if not candles or len(candles) < 100:
        return {"block": False, "reason": "insufficient_candles", "data": {}}

    try:
        # 1. Get the most recent peak_sep marker from format_chart_signals
        # format_chart_signals expects flat candle shape — flatten if nested mid format
        flat_candles = []
        for c in candles:
            if "mid" in c and isinstance(c["mid"], dict):
                flat_candles.append({
                    "time": c["time"],
                    "open": float(c["mid"]["o"]),
                    "high": float(c["mid"]["h"]),
                    "low":  float(c["mid"]["l"]),
                    "close": float(c["mid"]["c"]),
                })
            else:
                flat_candles.append({
                    "time": c["time"],
                    "open": float(c.get("open", c.get("o", 0))),
                    "high": float(c.get("high", c.get("h", 0))),
                    "low":  float(c.get("low", c.get("l", 0))),
                    "close": float(c.get("close", c.get("c", 0))),
                })

        signals = format_chart_signals(flat_candles) or []
        peak_seps = [s for s in signals if s.get("type") == "peak_sep"]
        if not peak_seps:
            return {"block": False, "reason": "no_peak_sep_marker", "data": {}}

        # 2. Locate the most recent peak_sep by candle index
        time_to_idx = {_canon_time(c["time"]): i for i, c in enumerate(flat_candles)}
        peak_seps_with_idx = []
        for s in peak_seps:
            idx = time_to_idx.get(_canon_time(s.get("time")))
            if idx is not None:
                peak_seps_with_idx.append((idx, s))
        if not peak_seps_with_idx:
            return {"block": False, "reason": "marker_time_unmatched", "data": {}}

        peak_seps_with_idx.sort(key=lambda x: x[0])
        latest_idx, latest_marker = peak_seps_with_idx[-1]
        live_idx = len(flat_candles) - 1
        bars_back = live_idx - latest_idx
        marker_dir = latest_marker.get("direction", "?")

        # 3. Marker must be within last NEAR_LIVE_BARS to count as "fresh"
        if bars_back > NEAR_LIVE_BARS:
            return {
                "block": False,
                "reason": f"marker_too_old(bars_back={bars_back})",
                "data": {"latest_marker_bars_back": bars_back, "marker_direction": marker_dir},
            }

        # 4. Fan velocity check — must be actively compressing (negative)
        engine = Indicators(candles)
        engine.compute_emas()
        fan = derive_fan_state(engine.df)
        velocity = float(fan.get("separation_velocity_pct_per_bar", 0))

        is_compressing = velocity <= RETRACE_VELOCITY_MAX

        if is_compressing:
            return {
                "block": True,
                "reason": f"recent_exit_marker(bars_back={bars_back},dir={marker_dir})+retrace(vel={velocity:.5f})",
                "data": {
                    "latest_marker_bars_back": bars_back,
                    "marker_direction": marker_dir,
                    "fan_velocity": velocity,
                    "fan_state": fan.get("fan_state"),
                },
            }
        else:
            return {
                "block": False,
                "reason": f"marker_fresh_but_no_retrace(vel={velocity:.5f})",
                "data": {
                    "latest_marker_bars_back": bars_back,
                    "marker_direction": marker_dir,
                    "fan_velocity": velocity,
                },
            }
    except Exception as e:
        return {"block": False, "reason": f"gate_error: {type(e).__name__}: {e}", "data": {}}
