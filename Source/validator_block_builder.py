"""Validator indicator block builder — SINGLE SOURCE OF TRUTH.

Used by BOTH:
  1. LIVE  — agents/trading_cycle.py (validator call during live trading cycle)
  2. TEST  — scripts/build_cohort_indicators.py (ghost replay cohort blocks)

Both paths must build the SAME structured inputs and call build_validator_indicator_block()
to produce the markdown block fed to the validator (35B / Anthropic).

Adding a new field = ONE edit here. Never duplicate the formatting logic in
either caller — pass the value via the input dicts and reference it in the
template below.

Inputs are intentionally explicit (keyword-only) so callers cannot silently
omit a section. Each section accepts a dict; missing keys fall back to a
defensible default and a "?" in the rendered block (visible to the validator
as "this data was not provided").

The 2026-05-17 refactor extracted this from trading_cycle.py:6668-6715
(the inline _raw_indicator_content builder) into this canonical module.
History: prior to this refactor, scripts/build_cohort_indicators.py had a
separate format_block() that omitted Stoch K/D, RSI slope, ADX, BB squeeze
state, patterns, divergence, scout deltas — so every ghost replay tested the
35B on POORER input than production sent it. That divergence is now eliminated.
"""
from __future__ import annotations

import json
from typing import Iterable, Mapping, Sequence


# ─── Candle helpers (M15-only; OANDA mid-format aware) ──────────────────
def _flatten_oanda_candles(candles: Iterable[Mapping]) -> list[dict]:
    """OANDA returns {'mid': {'o','h','l','c'}}. Flatten to {open,high,low,close,time}.
    Accepts already-flat candles too — checks for 'mid' key first.
    """
    out: list[dict] = []
    for c in candles:
        if "mid" in c:
            m = c["mid"]
            out.append({
                "time": c.get("time"),
                "open": float(m["o"]), "high": float(m["h"]),
                "low": float(m["l"]), "close": float(m["c"]),
            })
        else:
            out.append({
                "time": c.get("time"),
                "open": float(c["open"]), "high": float(c["high"]),
                "low": float(c["low"]), "close": float(c["close"]),
            })
    return out


def compute_range_position_pct(candles: Iterable[Mapping], lookback: int = 24) -> float | None:
    """Where the entry close sits in the last-N-bar range. 0=session low, 100=session high.
    M15 only. Returns None if insufficient bars."""
    flat = _flatten_oanda_candles(candles)
    if len(flat) < lookback:
        return None
    window = flat[-lookback:]
    hi = max(c["high"] for c in window)
    lo = min(c["low"] for c in window)
    rng = hi - lo
    if rng <= 0:
        return None
    return round((flat[-1]["close"] - lo) / rng * 100, 1)


def compute_prior_session_hl_pips(
    candles: Iterable[Mapping],
    pair: str,
    session_bars: int = 32,
) -> dict[str, float | None]:
    """Distance from entry close to prior 32-bar (8h M15) session H/L in pips.
    Returns {'pips_to_prev_hi': float|None, 'pips_to_prev_lo': float|None}.
    """
    flat = _flatten_oanda_candles(candles)
    if len(flat) < session_bars * 2:
        return {"pips_to_prev_hi": None, "pips_to_prev_lo": None}
    pip_factor = 100.0 if "JPY" in pair.upper() else 10000.0
    prev = flat[-session_bars * 2 : -session_bars]
    hi = max(c["high"] for c in prev)
    lo = min(c["low"] for c in prev)
    cur = flat[-1]["close"]
    return {
        "pips_to_prev_hi": round((hi - cur) * pip_factor, 1),
        "pips_to_prev_lo": round((cur - lo) * pip_factor, 1),
    }


# ─── Delta line helpers ─────────────────────────────────────────────────
def format_fan_delta_line(
    *,
    fan_delta_5bar: float | None = None,
    fan_delta_20bar: float | None = None,
) -> str:
    """Format the 'Fan width delta' line under EMA Structure.
    Returns a string ending with '\n' or empty string if both inputs are None.
    """
    if fan_delta_5bar is None and fan_delta_20bar is None:
        return ""
    parts = []
    if fan_delta_5bar is not None:
        parts.append(f"Δ5={fan_delta_5bar:+.5f}")
    if fan_delta_20bar is not None:
        parts.append(f"Δ20={fan_delta_20bar:+.5f}")
    return "Fan-Δ: " + " | ".join(parts) + "\n"


def format_bb_delta_line(
    *,
    bb_delta_5bar: float | None = None,
    bb_delta_20bar: float | None = None,
    bb_bandwidth: float | None = None,
) -> str:
    """Format the BB width / delta line under Bollinger Bands."""
    parts = []
    if bb_bandwidth is not None:
        parts.append(f"width={bb_bandwidth:.5f}")
    if bb_delta_5bar is not None:
        parts.append(f"Δ5={bb_delta_5bar:+.5f}")
    if bb_delta_20bar is not None:
        parts.append(f"Δ20={bb_delta_20bar:+.5f}")
    if not parts:
        return ""
    return "- BB-Δ: " + " | ".join(parts) + "\n"


# ─── Canonical block builder ────────────────────────────────────────────
def build_validator_indicator_block(
    *,
    # Mandatory context
    pair: str,
    direction: str,
    # EMA / fan state (mirrors ema_result from generate_market_picture)
    ema: Mapping,
    # Bollinger Band state (subset of ema_result + indicators)
    bollinger: Mapping,
    # Momentum (rsi, rsi_slope, stoch_k, stoch_d, macd_histogram, adx, regime, rsi_recovery_flag)
    momentum: Mapping,
    # Cross sequence (e21_e55, e21_e100, e55_e100 — current_orientation, bars_since_last_flip, cross_direction)
    crosses: Mapping,
    # E100 context (role: support/resistance/neutral, dist_pips, candle_pattern_text,
    #               candles_below_e100, candles_above_e100, last_close_vs_e100,
    #               rejections_from_below, rejections_from_above)
    e100: Mapping,
    # Location (range_position_24bar_pct, pips_to_prev_hi, pips_to_prev_lo)
    location: Mapping | None = None,
    # Patterns + divergence
    patterns: Sequence[str] | None = None,
    divergence: Mapping | None = None,
    # Scout context (alert_type, e100_dist_pips, fan_delta_5bar, fan_delta_20bar,
    #                 bb_delta_5bar, bb_delta_20bar)
    scout: Mapping | None = None,
    # Session gate (blocked: bool, reason: str)
    session: tuple[bool, str] = (False, ""),
) -> str:
    """Build the canonical 'Indicator Data — Raw' markdown block.

    Live path: trading_cycle.py builds these dicts from sniper_result, ema_result,
    indicators, _v4_* deltas, etc., and calls this function.

    Test path: scripts/build_cohort_indicators.py computes the same dicts from
    candles + market_picture and calls this function.

    Format is intentionally stable across callers — the validator's prompt
    references specific section headings ('**Momentum:**', '**Location:**')
    and the rule wording depends on these existing.
    """
    patterns = patterns or []
    divergence = divergence or {}
    scout = scout or {}
    location = location or {}

    # ── EMA / Fan ───────────────────────────────────────────────────────
    fan_dir = ema.get("fan_direction", "?")
    fan_state = ema.get("fan_state", "?")
    fan_ordered = ema.get("fan_ordered", "?")
    sep_pct = ema.get("separation_pct", 0) or 0
    sep_vel = ema.get("separation_velocity", 0) or 0
    fan_vel_trend = ema.get("fan_velocity_trend", "?")
    gap_100 = ema.get("gap_price_100", 0) or 0
    cascade_phase = ema.get("cascade_phase", 0) or 0
    trend_health = ema.get("trend_health", 0) or 0
    reversal_risk = ema.get("reversal_risk", "?")
    fan_delta_line = format_fan_delta_line(
        fan_delta_5bar=scout.get("fan_delta_5bar"),
        fan_delta_20bar=scout.get("fan_delta_20bar"),
    )

    # ── Cross sequence ──────────────────────────────────────────────────
    def _cross_line(label: str, key: str) -> str:
        c = crosses.get(key, {}) or {}
        bars_since = c.get("bars_since_last_flip")
        bars_str = f"{int(bars_since)} bars ago" if bars_since is not None else "never or >100 bars"
        orient = c.get("current_orientation", "?")
        cdir = c.get("cross_direction")
        cdir_str = f" ({cdir})" if cdir else ""
        return f"- {label}: {orient}, last flip {bars_str}{cdir_str}\n"

    cross_lines = (
        _cross_line("Cross 1 (E21 vs E55)", "e21_e55")
        + _cross_line("Cross 2 (E21 vs E100)", "e21_e100")
        + _cross_line("Cross 3 (E55 vs E100)", "e55_e100")
    )

    # ── E100 context ────────────────────────────────────────────────────
    e100_role = e100.get("role", "?")
    e100_dist = e100.get("dist_pips", 0) or 0
    e100_text = e100.get("candle_pattern_text", "none")
    below = e100.get("candles_below_e100", 0)
    above = e100.get("candles_above_e100", 0)
    last_close_vs = e100.get("last_close_vs_e100", "?")
    rej_below = e100.get("rejections_from_below", 0)
    rej_above = e100.get("rejections_from_above", 0)

    # ── Bollinger ───────────────────────────────────────────────────────
    bb_squeeze = bollinger.get("bb_squeeze", False)
    bb_expanding = bollinger.get("bb_expanding", False)
    bb_contracting = bollinger.get("bb_contracting", False)
    bb_lower_pen = bollinger.get("bb_lower_pen", 0) or 0
    bb_upper_pen = bollinger.get("bb_upper_pen", 0) or 0
    bb_delta_line = format_bb_delta_line(
        bb_delta_5bar=scout.get("bb_delta_5bar"),
        bb_delta_20bar=scout.get("bb_delta_20bar"),
        bb_bandwidth=bollinger.get("bb_bandwidth"),
    )

    # ── Momentum ────────────────────────────────────────────────────────
    rsi = momentum.get("rsi", 50) or 50
    rsi_slope = momentum.get("rsi_slope", 0) or 0
    rsi_recovery = momentum.get("rsi_recovery", True)
    rsi_warn = " ⚠️ STUCK AT EXTREME" if not rsi_recovery else ""
    stoch_k = momentum.get("stoch_k", 50)
    stoch_d = momentum.get("stoch_d", 50)
    macd_hist = momentum.get("macd_histogram", 0) or 0
    adx_val = momentum.get("adx", 0) or 0
    regime = momentum.get("regime", "?")

    # ── Location (NEW for ENTRY-COMMITMENT VETO 2026-05-17) ─────────────
    range_pos = location.get("range_position_24bar_pct")
    prev_hi = location.get("pips_to_prev_hi")
    prev_lo = location.get("pips_to_prev_lo")
    location_lines = ""
    if range_pos is not None:
        location_lines += (
            f"- Range position (last 24 bars): {range_pos}% "
            f"(0=session low, 100=session high)\n"
        )
    if prev_hi is not None and prev_lo is not None:
        location_lines += (
            f"- Prior 32-bar session: {prev_hi:+.1f}p to prior high, "
            f"{prev_lo:+.1f}p to prior low\n"
        )

    # ── Patterns & Divergence ───────────────────────────────────────────
    pat_str = ", ".join(patterns) if patterns else "None"
    if divergence and any(divergence.values()):
        div_str = json.dumps({k: v for k, v in divergence.items() if v}, default=str)
    else:
        div_str = "None"

    # ── Scout context line ──────────────────────────────────────────────
    scout_alert = scout.get("alert_type", "?")
    scout_e100 = scout.get("e100_dist_pips", e100_dist)
    fan_5 = scout.get("fan_delta_5bar", 0) or 0
    fan_20 = scout.get("fan_delta_20bar", 0) or 0
    bb_5 = scout.get("bb_delta_5bar", 0) or 0
    bb_20 = scout.get("bb_delta_20bar", 0) or 0
    scout_line = (
        f"- {scout_alert} | E100 dist={scout_e100:.1f}p | "
        f"fan_Δ5={fan_5:+.5f} | fan_Δ20={fan_20:+.5f} | "
        f"bb_Δ5={bb_5:+.5f} | bb_Δ20={bb_20:+.5f}"
    )

    # ── Session ─────────────────────────────────────────────────────────
    sess_block, sess_reason = session
    session_line = f"BLOCKED — {sess_reason}" if sess_block else "OPEN"

    # ── Assemble ────────────────────────────────────────────────────────
    location_section = location_lines if location_lines else "- (range/prior-session H-L not provided)\n"
    return (
        f"## Indicator Data — Raw (computed from candles ending at entry)\n\n"
        f"**EMA Structure:**\n"
        f"- Fan: {fan_dir} {fan_state} (ordered: {fan_ordered})\n"
        f"- Fan width (E21→E100): {sep_pct:.4f}% | {fan_delta_line}"
        f"- Velocity: {sep_vel:.6f}%/bar ({fan_vel_trend})\n"
        f"- E100 role: {e100_role} | distance: {e100_dist:.1f} pips | "
        f"gap_price_100: {gap_100:.4f}%\n"
        f"- E100 candle pattern: {e100_text}\n"
        f"{cross_lines}"
        f"- Cascade phase: {cascade_phase}/4 "
        f"(0=none, 1=cross1 only, 2=+cross2, 3=+cross3 fully ordered, 4=phase 3 confirmed by price)\n"
        f"- Last 10 closes vs E100: {below} below / {above} above. "
        f"Last close: {last_close_vs} E100\n"
        f"- E100 rejections (last 20 bars): {rej_below} from below (E100=resistance), "
        f"{rej_above} from above (E100=support)\n"
        f"- Trend Health: {trend_health}/100 | Reversal Risk: {reversal_risk}\n\n"
        f"**Bollinger Bands:**\n"
        f"{bb_delta_line}"
        f"- BB squeeze: {bb_squeeze} | BB expanding: {bb_expanding} | "
        f"BB contracting: {bb_contracting}\n"
        f"- BB position: lower_pen={bb_lower_pen:.4f}, upper_pen={bb_upper_pen:.4f}\n\n"
        f"**Momentum:**\n"
        f"- RSI: {rsi:.1f} (slope: {rsi_slope:.2f}){rsi_warn}\n"
        f"- Stoch K/D: {(stoch_k if stoch_k is not None else 50):.1f}/"
        f"{(stoch_d if stoch_d is not None else 50):.1f}\n"
        f"- MACD hist: {macd_hist:.5f}\n"
        f"- ADX: {adx_val:.1f} → regime={regime}\n\n"
        f"**Location:**\n"
        f"{location_section}"
        f"\n"
        f"**Patterns & Divergence:**\n"
        f"- Candlestick: {pat_str}\n"
        f"- Divergence: {div_str}\n\n"
        f"**Session gate:** {session_line}\n\n"
        f"**Scout:**\n"
        f"{scout_line}\n"
    )
