"""Kronos Hunter — independent M15 trade discovery.

Separation of concerns:
  * evaluate_signal()     : pure function, all gates in one place, easy to test
  * KronosHunter.run_cycle(): orchestrates fetch -> forecast_batch -> evaluate
                              -> log -> (optionally) execute, per M15 boundary
                              (implemented in Task 6)
  * run_forever()         : M15-aligned scheduler loop (Task 7)
"""

from __future__ import annotations

import json as _json_mod
import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Optional

import numpy as np
import pandas as pd

from kronos_inference import ForecastResult
from pathlib import Path as _Path

# Path plan extraction — reuse from walk-forward test module
import sys as _sys
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent / "research" / "kronos"))
try:
    from kronos_path_walkforward import extract_path_plan as _extract_path_plan
except ImportError:
    _extract_path_plan = None

logger = logging.getLogger("trading_bot.kronos_hunter")


def _pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def _compute_indicators(candles: pd.DataFrame, pair: str) -> dict:
    """Compute scout-equivalent indicators from candles at trade entry.

    Returns dict with: bb_width, rsi, stoch_k, stoch_d, adx, atr_pips,
    ema_21, ema_55, ema_100, close. All values are from the last bar.
    """
    if candles is None or len(candles) < 100:
        return {}
    pip = _pip_size(pair)
    c = candles["close"].values.astype(float)
    h = candles["high"].values.astype(float)
    l = candles["low"].values.astype(float)

    e21 = _ema(c, 21)
    e55 = _ema(c, 55)
    e100 = _ema(c, 100)

    # Bollinger Bands (20-period, 2 std)
    bb_period = 20
    if len(c) >= bb_period:
        bb_sma = np.mean(c[-bb_period:])
        bb_std = np.std(c[-bb_period:])
        bb_upper = bb_sma + 2 * bb_std
        bb_lower = bb_sma - 2 * bb_std
        bb_width = (bb_upper - bb_lower) / pip
    else:
        bb_width = 0.0

    # RSI (14-period)
    rsi = 50.0
    if len(c) >= 15:
        deltas = np.diff(c[-15:])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains) if len(gains) > 0 else 0
        avg_loss = np.mean(losses) if len(losses) > 0 else 0.001
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        rsi = 100 - (100 / (1 + rs))

    # Stochastic (14, 3)
    stoch_k = 50.0
    stoch_d = 50.0
    if len(c) >= 14:
        low_14 = np.min(l[-14:])
        high_14 = np.max(h[-14:])
        if high_14 != low_14:
            stoch_k = 100 * (c[-1] - low_14) / (high_14 - low_14)
        # %D = 3-bar SMA of %K (approximate with last value)
        stoch_d = stoch_k  # simplified

    # ADX (14-period, simplified)
    adx = 0.0
    if len(c) >= 15:
        tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
        atr_val = np.mean(tr[-14:]) if len(tr) >= 14 else 0
        # DI+ / DI- approximation from last 14 bars
        plus_dm = np.maximum(h[1:] - h[:-1], 0)
        minus_dm = np.maximum(l[:-1] - l[1:], 0)
        if atr_val > 0:
            plus_di = 100 * np.mean(plus_dm[-14:]) / atr_val
            minus_di = 100 * np.mean(minus_dm[-14:]) / atr_val
            di_sum = plus_di + minus_di
            if di_sum > 0:
                adx = 100 * abs(plus_di - minus_di) / di_sum

    return {
        "bb_width": round(bb_width, 2),
        "rsi": round(rsi, 1),
        "stoch_k": round(stoch_k, 1),
        "stoch_d": round(stoch_d, 1),
        "adx": round(adx, 1),
        "ema_21": round(float(e21[-1]), 6) if not np.isnan(e21[-1]) else None,
        "ema_55": round(float(e55[-1]), 6) if not np.isnan(e55[-1]) else None,
        "ema_100": round(float(e100[-1]), 6) if not np.isnan(e100[-1]) else None,
        "close": round(float(c[-1]), 6),
    }


def _ema(a: np.ndarray, period: int) -> np.ndarray:
    """Classic EMA (same math as scout + position_guardian)."""
    out = np.full(len(a), np.nan, dtype=float)
    if len(a) < period:
        return out
    m = 2.0 / (period + 1)
    out[period - 1] = a[:period].mean()
    for i in range(period, len(a)):
        out[i] = (a[i] - out[i - 1]) * m + out[i - 1]
    return out


def compute_regime(candles: pd.DataFrame, pair: str, direction: str) -> Dict[str, Any]:
    """Compute EMA fan regime at the current (last) bar for a candidate trade.

    Returns dict with:
      fan_direction:  'bullish' | 'bearish' | 'mixed'
      aligned:        True if fan matches trade direction
      total_sep_atr:  (E21↔E100 distance in pips) / ATR — compression measure
      slope_5_pips:   E21 slope over last 5 bars in pips (signed by trade direction)
      ok:             False if regime_ok cannot be computed (insufficient data)
    """
    if candles is None or len(candles) < 105:
        return {"ok": False, "reason": f"insufficient candles ({len(candles) if candles is not None else 0})"}
    pip = _pip_size(pair)
    c = candles["close"].values.astype(float)
    h = candles["high"].values.astype(float)
    l = candles["low"].values.astype(float)
    e21 = _ema(c, 21); e55 = _ema(c, 55); e100 = _ema(c, 100)
    if np.isnan(e21[-1]) or np.isnan(e55[-1]) or np.isnan(e100[-1]):
        return {"ok": False, "reason": "ema nan"}
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    atr_p = float(np.mean(tr[-14:])) / pip if len(tr) >= 14 else 0.0
    if atr_p <= 0:
        return {"ok": False, "reason": "atr zero"}

    bullish = e21[-1] > e55[-1] > e100[-1]
    bearish = e21[-1] < e55[-1] < e100[-1]
    fan_dir = "bullish" if bullish else ("bearish" if bearish else "mixed")
    aligned = (direction == "buy" and bullish) or (direction == "sell" and bearish)

    total_sep_pips = abs(e21[-1] - e100[-1]) / pip
    total_sep_atr = total_sep_pips / atr_p

    sign = 1.0 if direction == "buy" else -1.0
    slope_5_pips = sign * (e21[-1] - e21[-6]) / pip if len(e21) >= 6 else 0.0

    # 2026-04-16: Chop detection — count bars where price crosses E100
    # and check if EMAs are flat/tangled. Chart audit of 48 trades showed
    # 30/48 (65%) were in chop, producing 53% WR (-76.8p). Clean trend
    # trades were 80% WR (+16p). This is the #1 filter Kronos needs.
    e100_crosses = 0
    ema_order_consistent = 0
    for i in range(-10, 0):
        if i < -len(l) or i < -len(h):
            continue
        # Price crosses E100 when candle range straddles it
        if l[i] < e100[i] < h[i]:
            e100_crosses += 1
        # Check if EMA order is consistent (all bull or all bear)
        if e21[i] > e55[i] > e100[i] or e21[i] < e55[i] < e100[i]:
            ema_order_consistent += 1

    # E21-E55 separation stability over last 10 bars
    seps = [abs(e21[i] - e100[i]) / pip for i in range(-10, 0) if i >= -len(e21)]
    sep_std = float(np.std(seps)) if seps else 0
    sep_mean = float(np.mean(seps)) if seps else 0

    # Chop = EMAs tangled (not consistently ordered) + price crossing E100
    is_chop = (
        ema_order_consistent < 6 and  # EMAs not consistently ordered
        (e100_crosses >= 3 or total_sep_atr < 0.5)  # price weaving through E100 OR EMAs compressed
    )

    # ── Counter-momentum score (2026-04-22) ─────────────────────────────────
    # 3-condition entry-quality check identified from 7-day kronos loss analysis.
    # Losses: 64% had score 0/3. Wins: 75% had score ≥ 2/3.
    # 60d backtest: kept 48 of 164 trades, 79% WR (vs 54% actual), +$1,639 swing.
    # C1: entry candle color confirms direction (BUY=green, SELL=red)
    # C2: prior 3-bar close moved WITH direction
    # C3: stoch_k in direction zone AND turning further in direction
    o = candles["open"].values.astype(float)
    cm_c1 = (direction == "buy" and c[-1] > o[-1]) or \
            (direction == "sell" and c[-1] < o[-1])
    cm_c2 = len(c) >= 4 and (
        (direction == "buy" and c[-1] > c[-4]) or
        (direction == "sell" and c[-1] < c[-4])
    )
    # Stoch %K(14, 3) on the last bar
    cm_c3 = False
    if len(c) >= 17:
        stk_window = 14
        lowest_low = np.min(l[-stk_window:])
        highest_high = np.max(h[-stk_window:])
        if highest_high > lowest_low:
            stoch_k_now = 100 * (c[-1] - lowest_low) / (highest_high - lowest_low)
            lowest_prev = np.min(l[-stk_window-1:-1])
            highest_prev = np.max(h[-stk_window-1:-1])
            if highest_prev > lowest_prev:
                stoch_k_prev = 100 * (c[-2] - lowest_prev) / (highest_prev - lowest_prev)
                if direction == "buy":
                    cm_c3 = stoch_k_now >= 55 and stoch_k_now >= stoch_k_prev
                else:
                    cm_c3 = stoch_k_now <= 45 and stoch_k_now <= stoch_k_prev
    cm_score = int(cm_c1) + int(cm_c2) + int(cm_c3)

    # 2026-04-23: additional fields for 4-rule narrow filter (Gate 1.4)
    # stoch_k_now exported so filter can check knife pattern
    # candle_color / body_pct for "entry candle fighting direction" rule
    # pos_e21_atr for "ultra-extended position" rule
    _stoch_now = stoch_k_now if 'stoch_k_now' in dir() and len(c) >= 17 else None
    _candle_color = "GREEN" if c[-1] > o[-1] else ("RED" if c[-1] < o[-1] else "DOJI")
    _body = abs(float(c[-1]) - float(o[-1]))
    _range = float(h[-1]) - float(l[-1])
    _body_pct = (_body / _range) if _range > 0 else 0.0
    # pos_e21_atr: how far price is from E21 in ATR units (signed so BUY wants
    # positive = extended up, SELL wants negative = extended down).
    # atr_p is in pips; e21 is in raw price units, so convert price delta to pips too.
    _pos_e21_pips = (float(c[-1]) - float(e21[-1])) / pip
    _pos_e21_atr = _pos_e21_pips / atr_p if atr_p > 0 else 0.0

    return {
        "ok": True,
        "fan_direction": fan_dir,
        "aligned": aligned,
        "atr_pips": round(atr_p, 2),
        "total_sep_atr": round(total_sep_atr, 3),
        "total_sep_pips": round(total_sep_pips, 2),
        "slope_5_pips": round(slope_5_pips, 2),
        "e100_crosses": e100_crosses,
        "ema_order_consistent": ema_order_consistent,
        "is_chop": is_chop,
        "cm_score": cm_score,
        "cm_c1_color": bool(cm_c1),
        "cm_c2_ext3": bool(cm_c2),
        "cm_c3_stoch": bool(cm_c3),
        # Gate 1.4 inputs
        "stoch_k": round(_stoch_now, 1) if _stoch_now is not None else None,
        "candle_color": _candle_color,
        "body_pct": round(_body_pct, 3),
        "pos_e21_atr": round(_pos_e21_atr, 3),
    }


@dataclass
class HunterDecision:
    """Outcome of evaluate_signal() for one pair/direction forecast."""

    pair: str
    direction: Optional[str]
    action: str  # hunter_trade | skipped_low_drift | skipped_cooldown |
                 # skipped_dedup | skipped_kill_switch | skipped_max_concurrent
    reason: str


def _is_session_blocked(now_utc, params):
    """Check if current UTC time is inside a Kronos-blocked session window.

    Returns:
        (blocked: bool, reason: str)
    """
    if not params.get("hunter_session_gate_enabled", True):
        return False, ""
    weekday = now_utc.weekday()  # Mon=0 .. Sun=6
    hour = now_utc.hour

    # ── 2026-04-26: Forex weekend full blackout ──────────────────────────
    # Forex closes Friday 21:00 UTC (17:00 ET) and reopens Sunday 21:00 UTC.
    # The hour-only filter let weekend snipes get created on stale OANDA tape:
    # 27 weekend kronos snipes created across Sat/Sun 2026-04-25/04-26 because
    # weekday gates only covered Sun 21-23 UTC and Fri ≥20 UTC. Saturday was
    # entirely unguarded; Sunday hours 0-20 UTC were unguarded.
    sun_open_utc = int(params.get("hunter_sunday_open_utc", 21))
    sun_buffer_h = int(params.get("hunter_sunday_open_buffer_hours", 2))
    fri_start    = int(params.get("hunter_friday_block_start_utc", 20))

    # Friday after market close
    if weekday == 4 and hour >= fri_start:
        return True, f"Forex closed (Fri ≥{fri_start} UTC)"
    # Saturday: forex closed all day
    if weekday == 5:
        return True, "Forex closed (Saturday)"
    # Sunday before market open
    if weekday == 6 and hour < sun_open_utc:
        return True, f"Forex closed (Sunday pre-open <{sun_open_utc} UTC)"
    # Sunday open buffer: first N hours after open to let liquidity normalize
    if weekday == 6 and sun_open_utc <= hour < sun_open_utc + sun_buffer_h:
        return True, f"Sunday open buffer ({sun_buffer_h}h after {sun_open_utc} UTC)"

    # Legacy field — preserved for backward compat (overrides default Sunday
    # buffer if explicitly set with non-default end hour).
    sun_start = int(params.get("hunter_sunday_block_start_utc", sun_open_utc))
    sun_end = int(params.get("hunter_sunday_block_end_utc", sun_open_utc + sun_buffer_h))
    if weekday == 6 and sun_start <= hour < sun_end:
        return True, f"Sunday blackout {sun_start}-{sun_end} UTC"

    # ── Bleed-hour blackout (2026-04-22) ─────────────────────────────────
    # 60d session analysis showed 3 specific hour clusters bleed P&L:
    #   - UTC 4-6 (ET 00-02): Tokyo→Europe overlap, 25-35% WR
    #   - UTC 16-17 (ET 12-13): London close transition, 44% WR, -$303
    #   - UTC 20-23 (ET 16-19): NY close/Sydney open, 33-57% WR, -$252
    # Default list blocks all three. 60d backtest saved +$303 pre-candle-gate,
    # adds ~$12 on top of candle-gate combo.
    bleed_hours = params.get("hunter_session_bleed_hours_utc",
                             [4, 5, 6, 16, 17, 20, 21, 22, 23])
    if bleed_hours and hour in bleed_hours:
        return True, f"Bleed-hour blackout (UTC {hour})"

    return False, ""


def evaluate_signal(
    fr: ForecastResult,
    *,
    atr_pips: float,
    open_trade_on_pair: bool,
    recent_loss_count: int,
    recent_loss_window_hours: float,
    concurrent_open_kronos: int,
    daily_pnl: float,
    params: Dict[str, Any],
    now: datetime,
    regime: Optional[Dict[str, Any]] = None,
) -> HunterDecision:
    """Pure function: apply all Hunter gates in order. Return a decision.

    Gates are evaluated in this fixed order:
      1. Drift magnitude  — absolute pip floor + ATR-fraction floor
      2. Daily kill switch — halt if PnL <= threshold
      3. Concurrent-trades cap — no new trades if at max open
      4. Loss-based cooldown — pause this pair after N losses in window
      5. Dedup — skip if a trade is already open on the pair

    Args:
        fr: ForecastResult from the Kronos inference service.
        atr_pips: Current ATR in pips for the pair.
        open_trade_on_pair: True if any active trade (any source) exists on pair.
        recent_loss_count: How many CLOSED losing kronos_hunter trades on this
            pair occurred within the cooldown window. Used by the loss-based
            cooldown gate.
        recent_loss_window_hours: Window the loss count refers to (for messaging).
        concurrent_open_kronos: Number of currently open Kronos-sourced trades.
        daily_pnl: Realised + unrealised PnL today in pips.
        params: Tuning params dict. Required keys:
            hunter_min_drift_pips, hunter_min_drift_atr_frac,
            hunter_loss_cooldown_count, hunter_max_concurrent_trades,
            hunter_daily_kill_switch_pips.
        now: Current UTC datetime (injectable for testing).

    Returns:
        HunterDecision with action and human-readable reason.
    """
    pair = fr.pair
    min_drift = params["hunter_min_drift_pips"]
    min_drift_frac = params["hunter_min_drift_atr_frac"]
    loss_threshold = params["hunter_loss_cooldown_count"]
    max_concurrent = params["hunter_max_concurrent_trades"]
    kill_switch_pips = params["hunter_daily_kill_switch_pips"]

    # Gate 0.5: Session blackout (weekend edges)
    blocked, reason = _is_session_blocked(now, params)
    if blocked:
        return HunterDecision(
            pair, fr.direction, "skipped_session", reason,
        )

    # Gate 1: Drift magnitude vs absolute + ATR floor
    if abs(fr.drift_pips) < min_drift:
        return HunterDecision(
            pair, fr.direction, "skipped_low_drift",
            f"|drift|={abs(fr.drift_pips):.1f}p < {min_drift}p",
        )
    if atr_pips > 0 and abs(fr.drift_pips) < min_drift_frac * atr_pips:
        return HunterDecision(
            pair, fr.direction, "skipped_low_drift",
            f"|drift|={abs(fr.drift_pips):.1f}p < "
            f"{min_drift_frac}×ATR ({min_drift_frac * atr_pips:.1f}p)",
        )

    # Gate 1.04: Drift-to-ATR sanity cap (2026-04-24, A3)
    # When |drift_pips| > max_drift_atr_ratio × ATR, forecast is over-extrapolated.
    # EUR-cross blind spot: avg drift/ATR = 5.34 vs 1.96 on non-EUR. Cap at 5.0 blocks
    # the extreme outliers (3 EUR + 1 GBP_USD in 3-day sample). Combined with conf
    # [0.8, 1.1]: 92% WR / +27.5p over 3 days vs +21p without cap.
    _max_drift_atr = float(params.get("hunter_max_drift_atr_ratio", 5.0))
    if _max_drift_atr > 0 and atr_pips > 0 and abs(fr.drift_pips) / atr_pips > _max_drift_atr:
        return HunterDecision(
            pair, fr.direction, "skipped_extreme_drift",
            f"|drift|={abs(fr.drift_pips):.1f}p / ATR={atr_pips:.1f}p = "
            f"{abs(fr.drift_pips)/atr_pips:.2f}×ATR > {_max_drift_atr}×ATR — over-extrapolated",
        )

    # Gate 1.05: Signal confidence band (2026-04-24)
    # Confidence = |drift_pips| / cone_pips (kronos's self-reported conviction).
    #
    # LOWER bound (0.8 default): below this, forecast lacks conviction.
    #   conf<0.7: 47% WR / -156p. conf 0.7-1.0: 68% WR / -5p. conf ≥0.8: 76% WR.
    #
    # UPPER bound (1.1 default): above this, drift exceeds cone width —
    #   mathematically inconsistent (tight range but strong bias claimed).
    #   conf>1.0: 53% WR / -54p. conf 0.8-1.1: 81% WR / +21p.
    #
    # Grid-search optimal window on 3-day data: 0.8 ≤ conf ≤ 1.1.
    # Drops EUR-cross bleed from -125p to -0.9p (neutralizes model blind spot)
    # while keeping AUD_USD / USD_JPY / AUD_JPY edges intact.
    if hasattr(fr, 'confidence'):
        _min_conf = float(params.get("hunter_min_signal_confidence", 0.8))
        _max_conf = float(params.get("hunter_max_signal_confidence", 1.1))
        if _min_conf > 0 and fr.confidence < _min_conf:
            return HunterDecision(
                pair, fr.direction, "skipped_low_confidence",
                f"conf={fr.confidence:.2f} < {_min_conf} — forecast low-conviction",
            )
        if _max_conf > 0 and fr.confidence > _max_conf:
            return HunterDecision(
                pair, fr.direction, "skipped_inflated_confidence",
                f"conf={fr.confidence:.2f} > {_max_conf} — drift exceeds cone "
                f"(tight range, inflated conviction)",
            )

    # Gate 1.1: Consensus — early bars (0-3) and terminal must agree on direction.
    # Spike-and-reversal forecasts (early up, terminal down) become floaters.
    # 2026-04-16: Kronos docs return full 24-bar OHLCV path. Using only terminal
    # was wrong. Early bars are most accurate for our 1-3 bar win profile.
    if hasattr(fr, 'consensus') and not fr.consensus:
        return HunterDecision(
            pair, fr.direction, "skipped_no_consensus",
            f"early={getattr(fr, 'early_direction', '?')} vs "
            f"terminal={getattr(fr, 'terminal_direction', '?')} (spike-and-reversal)",
        )

    # Gate 1.2: Chop detection — EMAs tangled + price crossing E100 = no trade.
    # Chart audit of 48 Kronos trades: 30 were in chop (53% WR, -76.8p).
    # Clean trend trades: 80% WR, +16p. This gate blocks the noodle zone.
    if regime and regime.get("ok") and regime.get("is_chop", False):
        return HunterDecision(
            pair, fr.direction, "skipped_chop",
            f"EMAs tangled (ordered {regime.get('ema_order_consistent', 0)}/10 bars, "
            f"E100 crosses={regime.get('e100_crosses', 0)}, "
            f"sep={regime.get('total_sep_atr', 0):.2f}×ATR)",
        )

    # ── Gate 1.3: Counter-momentum score (2026-04-22) ────────────────────
    # 3-condition entry-quality check from 7-day loss pattern analysis:
    #   C1 entry candle color confirms direction
    #   C2 prior 3-bar price extension WITH direction
    #   C3 stoch_k in direction zone AND turning further in direction
    # Losses: 64% scored 0/3. Wins: 75% scored ≥ 2/3.
    # 60-day backtest (candle+session combo): +$1,639 net swing (-$1,378 → +$261).
    # Block if score < min_score (default 2).
    if params.get("hunter_counter_momentum_enabled", True) and regime and regime.get("ok"):
        cm_score = regime.get("cm_score")
        min_cm_score = int(params.get("hunter_counter_momentum_min_score", 2))
        if cm_score is not None and cm_score < min_cm_score:
            return HunterDecision(
                pair, fr.direction, "skipped_counter_momentum",
                f"CM score {cm_score}/3 < min {min_cm_score} "
                f"(c1_color={regime.get('cm_c1_color')} "
                f"c2_ext3={regime.get('cm_c2_ext3')} "
                f"c3_stoch={regime.get('cm_c3_stoch')})",
            )

    # ── Gate 1.4: 4-rule narrow-pattern kronos filter (2026-04-23) ──
    # Replaces the broad "clean_fan" filter that was too aggressive (4% retention).
    # Deep-dive on 13 big losers (≤-10p) across 2 days identified 4 specific
    # patterns. This filter targets those patterns, not the entire clean-fan space.
    # Backtest on 79 trades (today+yesterday): +188p saved, WR 55.7→63.3%,
    # retention 38% (vs previous broad filter's 4%). Catches 9/13 big losers (69%).
    #
    # Rule 1 — "catching knife" (stoch extreme matching direction):
    #   BUY with stoch > 70  → late entry into overbought
    #   SELL with stoch < 30 → late entry into oversold
    # Rule 2 — "entry candle fighting direction" (>30% body, opposite color):
    #   BUY with RED candle body > 30%  → bar screaming sell
    #   SELL with GREEN candle body > 30% → bar screaming buy
    # Rule 3 — "ultra-extended position" (>2 ATR from E21 in trade direction):
    #   BUY with pos_e21_atr > +2  → already extended, buying the top
    #   SELL with pos_e21_atr < -2 → already extended, shorting the bottom
    # Rule 4 — "ambiguous entry candle" (body < 10% of range — doji/tiny body):
    #   Kronos has no conviction from price action at entry moment
    #
    # All four rules universally apply to BOTH kronos_hunter direct and path-snipe
    # creation. Trigger-time re-check for path snipes lives in trading_cycle.py.
    if params.get("hunter_4rule_filter_enabled", True) and regime and regime.get("ok"):
        # Pull indicator snapshot from regime + recompute what's missing
        _stoch = regime.get("stoch_k")
        _pos_e21_atr = regime.get("pos_e21_atr")
        _candle_color = regime.get("candle_color")
        _body_pct = regime.get("body_pct")
        _dir = fr.direction.lower()

        # Thresholds — all tunable (2026-04-24)
        _knife_buy_max = float(params.get("hunter_knife_buy_stoch_max", 70.0))
        _knife_sell_min = float(params.get("hunter_knife_sell_stoch_min", 30.0))
        _fight_body_min = float(params.get("hunter_candle_fighting_body_pct_min", 0.30))
        _extended_atr = float(params.get("hunter_ultra_extended_atr_mult", 2.0))
        _ambiguous_body_max = float(params.get("hunter_ambiguous_body_pct_max", 0.10))

        # Rule 1 — knife
        if _stoch is not None and _stoch > 0:
            if _dir == "buy" and _stoch > _knife_buy_max:
                return HunterDecision(
                    pair, fr.direction, "skipped_knife_buy_overbought",
                    f"BUY with stoch={_stoch:.1f}>{_knife_buy_max} — late entry into overbought",
                )
            if _dir == "sell" and _stoch < _knife_sell_min:
                return HunterDecision(
                    pair, fr.direction, "skipped_knife_sell_oversold",
                    f"SELL with stoch={_stoch:.1f}<{_knife_sell_min} — late entry into oversold",
                )
        # Rule 2 — entry candle fighting
        if _candle_color and _body_pct is not None and _body_pct > _fight_body_min:
            if _dir == "buy" and _candle_color == "RED":
                return HunterDecision(
                    pair, fr.direction, "skipped_candle_fighting_buy",
                    f"BUY but entry candle RED with body {_body_pct*100:.0f}% > {_fight_body_min*100:.0f}%",
                )
            if _dir == "sell" and _candle_color == "GREEN":
                return HunterDecision(
                    pair, fr.direction, "skipped_candle_fighting_sell",
                    f"SELL but entry candle GREEN with body {_body_pct*100:.0f}% > {_fight_body_min*100:.0f}%",
                )
        # Rule 3 — ultra-extended position vs E21
        if _pos_e21_atr is not None:
            if _dir == "buy" and _pos_e21_atr > _extended_atr:
                return HunterDecision(
                    pair, fr.direction, "skipped_ultra_extended_buy",
                    f"BUY with price {_pos_e21_atr:+.2f}×ATR above E21 — buying top",
                )
            if _dir == "sell" and _pos_e21_atr < -_extended_atr:
                return HunterDecision(
                    pair, fr.direction, "skipped_ultra_extended_sell",
                    f"SELL with price {_pos_e21_atr:+.2f}×ATR below E21 — shorting bottom",
                )
        # Rule 4 — ambiguous entry candle (doji / tiny body)
        if _body_pct is not None and _body_pct < _ambiguous_body_max:
            return HunterDecision(
                pair, fr.direction, "skipped_ambiguous_candle",
                f"entry candle body {_body_pct*100:.0f}% of range < {_ambiguous_body_max*100:.0f}% — no conviction",
            )

    # Gate 1.3: Scout bias sanity check — Kronos must agree with scout's trend assessment.
    # Controllable via kronos.hunter_scout_bias_gate tuning param.
    # Backtest: 138 blocked trades were 80% WR, +575 pips — disable to let
    # Kronos find reversals before the fan confirms.
    scout_bias_enabled = bool(params.get("hunter_scout_bias_gate", True))
    if scout_bias_enabled and regime and regime.get("ok"):
        # Use the regime data we already computed + check flight recorder for scout's bias
        _scout_bias = None
        try:
            import sqlite3 as _sql3
            from pathlib import Path as _Path
            import os as _os
            _fr_path = _Path(__file__).resolve().parent / "flight_recorder.db"
            # Skip in test environments
            if _fr_path.exists() and not _os.environ.get("KRONOS_SKIP_SCOUT_BIAS"):
                _fr_db = _sql3.connect(str(_fr_path), timeout=2)
                _bias_row = _fr_db.execute(
                    "SELECT json_extract(data, '$.fan_direction') "
                    "FROM flight_log WHERE pair = ? AND stage = 'scout_scan' "
                    "AND json_extract(data, '$.fan_direction') IS NOT NULL "
                    "AND timestamp >= datetime('now', '-15 minutes') "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (pair,),
                ).fetchone()
                _fr_db.close()
                if _bias_row and _bias_row[0]:
                    _fd = _bias_row[0]
                    _scout_bias = 'bull' if _fd == 'bullish' else ('bear' if _fd == 'bearish' else 'neutral')
        except Exception:
            pass

        # Fall back to our own regime if scout data unavailable
        if not _scout_bias:
            _fd = regime.get("fan_direction", "mixed")
            if _fd == "bearish":
                _scout_bias = "bear"
            elif _fd == "bullish":
                _scout_bias = "bull"
            else:
                _scout_bias = "neutral"

        _buy_blocked = fr.direction == "buy" and _scout_bias in ("bear", "strong_bear")
        _sell_blocked = fr.direction == "sell" and _scout_bias in ("bull", "strong_bull")
        if _buy_blocked or _sell_blocked:
            return HunterDecision(
                pair, fr.direction, "skipped_scout_bias",
                f"{fr.direction.upper()} blocked — scout says {_scout_bias} "
                f"(dashboard trend indicator)",
            )

    # Gate 1.5: Regime — reject counter-trend / compression / noodling fan.
    # Today's losses (2026-04-15): 8 of 11 had fan misaligned, 5 were in
    # compression (total_sep < 0.8×ATR), 5 had flat E21 slope (<1p over 5
    # bars). These gates cut those entries off at the source.
    # Missing regime data fails open (backwards compatibility with tests).
    if regime and regime.get("ok"):
        require_aligned = bool(params.get("hunter_require_fan_aligned", True))
        min_fan_sep_atr = float(params.get("hunter_min_fan_sep_atr", 0.8))
        min_slope_pips = float(params.get("hunter_min_e21_slope_pips", 1.0))

        if require_aligned and not regime.get("aligned", True):
            return HunterDecision(
                pair, fr.direction, "skipped_fan_misaligned",
                f"fan {regime.get('fan_direction')} vs {fr.direction} (counter-trend)",
            )
        sep_atr = regime.get("total_sep_atr", 99.0)
        if sep_atr < min_fan_sep_atr:
            return HunterDecision(
                pair, fr.direction, "skipped_compression",
                f"fan sep {sep_atr:.2f}×ATR < {min_fan_sep_atr}×ATR (noodling)",
            )
        slope = abs(regime.get("slope_5_pips", 99.0))
        if slope < min_slope_pips:
            return HunterDecision(
                pair, fr.direction, "skipped_flat_fan",
                f"|E21 slope|={slope:.1f}p over 5 bars < {min_slope_pips}p (flat)",
            )

    # Gate 2: Daily kill switch
    if daily_pnl <= kill_switch_pips:
        return HunterDecision(
            pair, fr.direction, "skipped_kill_switch",
            f"daily_pnl={daily_pnl:.1f}p <= {kill_switch_pips}p",
        )

    # Gate 3: Concurrent-trades cap
    if concurrent_open_kronos >= max_concurrent:
        return HunterDecision(
            pair, fr.direction, "skipped_max_concurrent",
            f"open_kronos={concurrent_open_kronos} >= {max_concurrent}",
        )

    # Gate 4: Loss-based cooldown — only pause after recent losses on this pair.
    # Wins/skips never block. If pair has lost >= loss_threshold times within
    # the configured window, skip until window expires.
    if loss_threshold > 0 and recent_loss_count >= loss_threshold:
        return HunterDecision(
            pair, fr.direction, "skipped_loss_cooldown",
            f"{recent_loss_count} loss(es) in last {recent_loss_window_hours:.1f}h "
            f">= {loss_threshold} threshold",
        )

    # Gate 5: Pair already open (dedup — snipe, scout, manual, or kronos)
    if open_trade_on_pair:
        return HunterDecision(
            pair, fr.direction, "skipped_dedup",
            "existing open trade on pair",
        )

    return HunterDecision(
        pair, fr.direction, "hunter_trade",
        f"drift={fr.drift_pips:+.1f}p conf={fr.confidence:.2f}",
    )


class KronosHunter:
    """Orchestrator that fetches candles → forecast_batch → evaluate → act.

    All external collaborators are injected (inference service, signals DB,
    candle loader, open-trade checker, daily pnl source, order placer) so
    this class is trivially testable without real network / MPS calls.
    """

    def __init__(
        self,
        *,
        inference,                                # KronosInferenceService (duck-typed)
        signals_db,                               # KronosSignalsDB
        candle_loader: Callable[[str], pd.DataFrame],
        open_trade_checker: Callable[[str], bool],
        concurrent_counter: Callable[[], int],
        daily_pnl_fn: Callable[[], float],
        order_placer: Callable[..., Dict[str, Any]],
        pairs: Iterable[str],
        params_fn: Callable[[], Dict[str, Any]],
        shadow_mode_fn: Callable[[], bool],
        loss_counter: Optional[Callable[[str, float], int]] = None,
    ):
        self._inference = inference
        self._signals_db = signals_db
        self._candle_loader = candle_loader
        self._open_trade_checker = open_trade_checker
        self._concurrent_counter = concurrent_counter
        self._daily_pnl_fn = daily_pnl_fn
        self._order_placer = order_placer
        self._pairs = list(pairs)
        self._params_fn = params_fn
        self._shadow_mode_fn = shadow_mode_fn
        # Default: never block on loss history (caller should inject a real one)
        self._loss_counter = loss_counter or (lambda pair, hours: 0)

    # ------------------------------------------------------------------
    def run_cycle(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Run one full discovery cycle across all configured pairs.

        Args:
            now: Current UTC datetime (injectable for testing). Defaults to
                ``datetime.now(timezone.utc)``.

        Returns:
            Summary dict with keys: pairs_scanned, signals_emitted,
            trades_opened, started_at.
        """
        now = now or datetime.now(timezone.utc)
        summary: Dict[str, Any] = {
            "pairs_scanned": 0, "signals_emitted": 0, "trades_opened": 0,
            "started_at": now.isoformat(),
        }

        try:
            from flight_recorder import flight as _flight, FlightStage as _FS
        except Exception:
            _flight, _FS = None, None

        def _fr(stage, **kw):
            if _flight is None or _FS is None:
                return
            try:
                _flight.record(stage, **kw)
            except Exception:
                pass  # never let flight recorder errors break trading

        _fr(_FS.KRONOS_HUNTER_SCAN_START if _FS else None,
            data={"now": now.isoformat(), "pairs": self._pairs,
                  "shadow_mode": self._shadow_mode_fn()})

        # Clean up expired Kronos snipes before scanning
        try:
            from agents.watch_manager import cleanup_expired_kronos
            _n_cleaned = cleanup_expired_kronos()
            if _n_cleaned > 0:
                logger.info("kronos: cleaned %d expired snipes", _n_cleaned)
                _fr(_FS.KRONOS_SNIPE_EXPIRED if _FS else None,
                    data={"count": _n_cleaned})
        except Exception as _cleanup_exc:
            # 2026-04-24: upgraded from debug. If cleanup silently fails, expired
            # snipes accumulate in watch_suggestions and can still trigger.
            logger.warning("kronos: snipe cleanup failed (%s: %s) — expired snipes may not be purged",
                           type(_cleanup_exc).__name__, _cleanup_exc)
            _fr(_FS.KRONOS_ERROR if _FS and hasattr(_FS, 'KRONOS_ERROR') else None,
                data={"phase": "cleanup_expired", "error_type": type(_cleanup_exc).__name__,
                      "error": str(_cleanup_exc)[:200]})

        if not self._inference.is_ready():
            logger.warning("Kronos inference not ready; skipping cycle")
            _fr(_FS.KRONOS_HUNTER_SCAN_COMPLETE if _FS else None,
                data={**summary, "skipped_reason": "inference_not_ready"})
            return summary

        params = self._params_fn()
        shadow = self._shadow_mode_fn()

        # 1) Gather candles for every pair
        candles_by_pair: Dict[str, pd.DataFrame] = {}
        for pair in self._pairs:
            try:
                candles_by_pair[pair] = self._candle_loader(pair)
            except Exception as exc:
                logger.warning("candle load failed for %s: %s", pair, exc)

        if not candles_by_pair:
            return summary

        # 2) Batched forecast
        forecasts = self._inference.forecast_batch(
            candles_by_pair,
            pred_len=int(params["pred_len_bars"]),
            sample_count=int(params["sample_count"]),
        )

        # 3) Decide + log + execute per pair
        loss_window_hours = float(params.get("hunter_loss_cooldown_hours", 4))
        for pair, fr in forecasts.items():
            summary["pairs_scanned"] += 1
            candles = candles_by_pair[pair]
            atr_pips = self._inference._atr_pips(candles, pair)
            recent_losses = self._loss_counter(pair, loss_window_hours)

            # Compute regime once per pair (reads fan alignment, separation,
            # slope from the candles we already have in hand).
            regime = compute_regime(candles, pair, fr.direction)

            # ── Path-based direction: use full 24-bar forecast shape ──
            _path_plan = None
            _path_direction = fr.direction  # fallback to legacy
            _entry_bar = 0
            if _extract_path_plan is not None and hasattr(fr, 'forecast_path') and fr.forecast_path:
                try:
                    _forecast_df = pd.DataFrame(fr.forecast_path)
                    # forecast_path uses short keys {o,h,l,c} — extract_path_plan expects {open,high,low,close}
                    if "o" in _forecast_df.columns and "open" not in _forecast_df.columns:
                        _forecast_df = _forecast_df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close"})
                    _last_close = float(candles["close"].iloc[-1])
                    _path_plan = _extract_path_plan(_forecast_df, _last_close, pair)
                    _path_direction = _path_plan["direction"]
                    # Entry bar: the FIRST extreme (dip bar for buy, peak bar for sell)
                    _pp_closes = _forecast_df["close"].values
                    if _path_direction == "buy":
                        _entry_bar = int(np.argmin(_pp_closes))
                    else:
                        _entry_bar = int(np.argmax(_pp_closes))
                except Exception as _pp_exc:
                    # 2026-04-24: upgraded from debug. Path-plan override is default-off
                    # (kronos.hunter_path_direction_override_enabled=false), so this is
                    # low-impact today — but visibility matters if Tim re-enables the
                    # override and a code-path breaks silently.
                    logger.warning("kronos: path plan extraction failed for %s: %s: %s",
                                   pair, type(_pp_exc).__name__, _pp_exc)

            # Override direction with path-derived if different.
            # 2026-04-24: Gated behind tunable. Default False because 3-day
            # backtest showed path-override cohort (36 trades where path_direction
            # flipped early-bars direction) had 39% WR and -94.5p net pips even
            # at high confidence, vs aligned cohort at 70% WR. Plus the override
            # keeps old drift_pips from early bars — signal row becomes internally
            # contradictory (direction=buy with drift_pips=-18, etc).
            # Re-enable via tuning if/when path-plan extraction improves.
            _path_override_enabled = params.get(
                "kronos.hunter_path_direction_override_enabled", False)
            if _path_override_enabled and _path_direction != fr.direction:
                logger.info("kronos: %s path says %s (was %s from early-bars)",
                            pair, _path_direction, fr.direction)
                fr = ForecastResult(
                    pair=fr.pair, direction=_path_direction,
                    drift_pips=fr.drift_pips, drift_atr_frac=fr.drift_atr_frac,
                    confidence=fr.confidence,
                    forecast_terminal=fr.forecast_terminal,
                    forecast_max_high=fr.forecast_max_high,
                    forecast_min_low=fr.forecast_min_low,
                    latency_ms=fr.latency_ms,
                    early_drift_pips=fr.early_drift_pips,
                    terminal_drift_pips=fr.terminal_drift_pips,
                    early_direction=fr.early_direction,
                    terminal_direction=fr.terminal_direction,
                    consensus=fr.consensus,
                    forecast_sl_price=fr.forecast_sl_price,
                    forecast_tp_price=fr.forecast_tp_price,
                    forecast_path=getattr(fr, 'forecast_path', None),
                )
                regime = compute_regime(candles, pair, _path_direction)
            elif _path_direction != fr.direction:
                # Path disagreed but override is disabled — log it for observability.
                logger.debug("kronos: %s path_direction=%s disagrees with early=%s "
                             "(override disabled, trading early-bars)",
                             pair, _path_direction, fr.direction)

            decision = evaluate_signal(
                fr,
                atr_pips=atr_pips,
                open_trade_on_pair=self._open_trade_checker(pair),
                recent_loss_count=recent_losses,
                recent_loss_window_hours=loss_window_hours,
                concurrent_open_kronos=self._concurrent_counter(),
                daily_pnl=self._daily_pnl_fn(),
                params=params,
                now=now,
                regime=regime,
            )
            summary["signals_emitted"] += 1

            trade_id = None
            error = None
            action = decision.action
            # ── KRONOS SNIPE-ONLY (2026-04-29): every hunter_trade signal creates a quality
            # snipe via watch_manager. Direct fires removed — backtest of 2,834 historical
            # signals showed quality snipes lift WR 88%→94%, PF 2.20→4.05, MDD 214→78p.
            # When path forecast says move starts immediately (_entry_bar < 2 or no path_plan),
            # use entry_bar=0 with current close — snipe fires within current bar if structure
            # conditions match, expires shortly otherwise.
            if action == "hunter_trade" and not shadow:
                try:
                    from agents.watch_manager import create_kronos_snipe
                    _snipe_last_close = float(candles["close"].iloc[-1])
                    _snipe_pip = 0.01 if "JPY" in pair else 0.0001
                    _has_path = _entry_bar >= 2 and _path_plan is not None
                    if _has_path:
                        _snipe_entry_price = _path_plan["path_json"][_entry_bar]["c"] if _path_plan.get("path_json") else _snipe_last_close
                        _snipe_entry_bar = _entry_bar
                    else:
                        _snipe_entry_price = _snipe_last_close
                        _snipe_entry_bar = 0  # immediate-fire snipe

                    # SL/TP: reuse existing ATR-bounded forecast logic
                    _s_forecast_sl_dist = abs(_snipe_last_close - fr.forecast_sl_price) / _snipe_pip if fr.forecast_sl_price else 0
                    _s_atr_sl_min = params.get("gate.atr_sl_min_mult", 1.5) * atr_pips
                    _s_atr_sl_max = params.get("gate.atr_sl_max_mult", 3.0) * atr_pips
                    _s_sl_pips = max(_s_atr_sl_min, min(_s_forecast_sl_dist, _s_atr_sl_max)) if _s_forecast_sl_dist > 0 else params.get("sl_atr_mult", 2.0) * atr_pips

                    _s_forecast_tp_dist = abs(fr.forecast_tp_price - _snipe_last_close) / _snipe_pip if fr.forecast_tp_price else 0
                    _s_atr_tp_min = params.get("gate.atr_tp_min_mult", 1.5) * atr_pips
                    _s_atr_tp_max = params.get("gate.atr_tp_max_mult", 5.0) * atr_pips
                    _s_tp_pips = max(_s_atr_tp_min, min(_s_forecast_tp_dist, _s_atr_tp_max)) if _s_forecast_tp_dist > 0 else params.get("tp_atr_mult", 1.5) * atr_pips

                    if decision.direction == "buy":
                        _snipe_sl = _snipe_entry_price - _s_sl_pips * _snipe_pip
                        _snipe_tp = _snipe_entry_price + _s_tp_pips * _snipe_pip
                    else:
                        _snipe_sl = _snipe_entry_price + _s_sl_pips * _snipe_pip
                        _snipe_tp = _snipe_entry_price - _s_tp_pips * _snipe_pip

                    # Pass full Kronos forecast data as conditions source
                    _kronos_data = {
                        "drift_pips": fr.drift_pips,
                        "drift_atr_frac": fr.drift_atr_frac,
                        "confidence": fr.confidence,
                        "consensus": fr.consensus,
                        "early_direction": fr.early_direction,
                        "terminal_direction": fr.terminal_direction,
                        "terminal_drift_pips": fr.terminal_drift_pips,
                        "forecast_max_high": fr.forecast_max_high,
                        "forecast_min_low": fr.forecast_min_low,
                    }
                    _watch_id = create_kronos_snipe(
                        instrument=pair, direction=decision.direction,
                        entry_price=_snipe_entry_price, entry_bar=_snipe_entry_bar,
                        anchor_time=now, forecast_anchor=now.isoformat(),
                        sl_price=_snipe_sl, tp_price=_snipe_tp,
                        indicators=_kronos_data,
                        fan_direction=regime.get('fan_direction', '') if regime and regime.get('ok') else '',
                        fan_state=regime.get('fan_direction', '') + '_' + ('expanding' if regime.get('aligned') else 'mixed') if regime and regime.get('ok') else '',
                    )
                    action = "kronos_snipe_created"
                    trade_id = f"snipe_{_watch_id}"
                    logger.info("kronos: %s SNIPE created (watch %s) entry=%.5f bar=%d expiry=%d min%s",
                                pair, _watch_id, _snipe_entry_price, _snipe_entry_bar,
                                (_snipe_entry_bar + 3) * 15,
                                "" if _has_path else " [immediate-fire]")
                    _fr(_FS.KRONOS_SNIPE_CREATED if _FS else None,
                        pair=pair,
                        data={"watch_id": _watch_id, "direction": decision.direction,
                              "entry_price": _snipe_entry_price, "entry_bar": _snipe_entry_bar,
                              "sl": _snipe_sl, "tp": _snipe_tp,
                              "has_path": _has_path,
                              "expiry_min": (_snipe_entry_bar + 3) * 15})
                except Exception as _snipe_exc:
                    error = str(_snipe_exc)
                    action = "kronos_snipe_failed"
                    logger.error("kronos: snipe creation failed for %s: %s", pair, _snipe_exc)

            self._signals_db.insert(
                anchor_time=now.isoformat(),
                pair=pair,
                direction=fr.direction,
                drift_pips=fr.drift_pips,
                drift_atr_frac=fr.drift_atr_frac,
                confidence=fr.confidence,
                atr_pips=atr_pips,
                forecast_terminal=fr.forecast_terminal,
                forecast_max_high=fr.forecast_max_high,
                forecast_min_low=fr.forecast_min_low,
                action_taken=action,
                trade_id=trade_id,
                latency_ms=fr.latency_ms,
                error=error,
                early_drift_pips=fr.early_drift_pips,
                terminal_drift_pips=fr.terminal_drift_pips,
                consensus=1 if fr.consensus else 0,
                forecast_sl_price=fr.forecast_sl_price,
                forecast_tp_price=fr.forecast_tp_price,
                forecast_path_json=_json_mod.dumps(fr.forecast_path) if hasattr(fr, 'forecast_path') and fr.forecast_path else None,
            )

            _fr(_FS.KRONOS_HUNTER_SIGNAL if _FS else None,
                pair=pair,
                data={"pair": pair, "direction": fr.direction,
                      "drift_pips": fr.drift_pips, "confidence": fr.confidence,
                      "atr_pips": atr_pips, "action": action,
                      "trade_id": trade_id, "shadow": shadow,
                      # Regime snapshot for rejected/accepted signals
                      "fan_direction": regime.get("fan_direction") if regime.get("ok") else None,
                      "fan_aligned": regime.get("aligned") if regime.get("ok") else None,
                      "total_sep_atr": regime.get("total_sep_atr") if regime.get("ok") else None,
                      "slope_5_pips": regime.get("slope_5_pips") if regime.get("ok") else None,
                      "reason": decision.reason})

            if action == "hunter_trade" and trade_id:
                _fr(_FS.KRONOS_HUNTER_TRADE_OPEN if _FS else None,
                    pair=pair, trade_id=trade_id,
                    data={"pair": pair, "direction": decision.direction,
                          "entry_price": float(candles["close"].iloc[-1]),
                          "sl_pips": params["sl_atr_mult"] * atr_pips,
                          "tp_pips": params["tp_atr_mult"] * atr_pips,
                          "drift_pips": fr.drift_pips,
                          "source": "kronos_hunter"})

            if error:
                _fr(_FS.KRONOS_ERROR if _FS else None,
                    pair=pair, data={"pair": pair, "error": error,
                                     "stage": "order_placement"})

        _fr(_FS.KRONOS_HUNTER_SCAN_COMPLETE if _FS else None,
            data={**summary, "completed_at": datetime.now(timezone.utc).isoformat()})
        return summary


# ---------------------------------------------------------------------------
# Task 7: M15-aligned scheduler helpers
# ---------------------------------------------------------------------------


def next_m15_boundary(now: datetime) -> datetime:
    """Return the next :00, :15, :30, or :45 UTC boundary strictly after *now*.

    Args:
        now: A timezone-aware UTC datetime.

    Returns:
        The next quarter-hour boundary (strictly in the future relative to *now*).
    """
    minute_block = (now.minute // 15) + 1
    if minute_block >= 4:
        base = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        base = now.replace(minute=minute_block * 15, second=0, microsecond=0)
    return base


def run_forever(
    hunter: "KronosHunter",
    *,
    master_enabled_fn: Callable[[], bool],
    hunter_enabled_fn: Callable[[], bool],
    sleep_fn: Callable[[float], None] = _time.sleep,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> None:
    """Blocking loop. Sleeps until the next M15 boundary, then runs a cycle.

    Returns only when the master kill switch flips to False (caller should
    retry the outer loop periodically to pick up re-enablement).

    Args:
        hunter: A KronosHunter instance to drive each cycle.
        master_enabled_fn: Returns False to stop the loop entirely.
        hunter_enabled_fn: Returns False to pause (loop keeps polling every 30 s).
        sleep_fn: Injectable sleep; defaults to ``time.sleep``.
        now_fn: Injectable UTC clock; defaults to ``datetime.now(timezone.utc)``.
    """
    logger.warning("[KRONOS_HUNTER_LOOP] entered run_forever")
    _last_heartbeat = 0.0
    _last_fired_boundary: Optional[datetime] = None
    while master_enabled_fn():
        if not hunter_enabled_fn():
            logger.info("[KRONOS_HUNTER_LOOP] hunter disabled — sleeping 30s")
            sleep_fn(30)
            continue

        now = now_fn()
        # Find the most-recent boundary that has already passed
        # (e.g. now=06:14:59 -> 06:00; now=06:15:01 -> 06:15).
        passed_boundary = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        next_boundary = next_m15_boundary(now)

        ts = _time.time()
        if ts - _last_heartbeat > 300:
            logger.warning("[KRONOS_HUNTER_LOOP] alive — last_fired=%s next=%s (in %.0fs)",
                           _last_fired_boundary.isoformat() if _last_fired_boundary else "never",
                           next_boundary.isoformat(),
                           (next_boundary - now).total_seconds())
            _last_heartbeat = ts

        # 2026-04-16: Fire Kronos 2 min BEFORE the M15 boundary (:13, :28,
        # :43, :58). Kronos takes ~2 min for 13 pairs, finishes before scout
        # fires at :00/:15/:30/:45 — no MPS contention with 9B TA.
        _secs_to_next = (next_boundary - now).total_seconds()
        _fire_before_s = 120  # fire 2 min before boundary
        _should_fire_early = (_secs_to_next <= _fire_before_s
                              and (_last_fired_boundary is None
                                   or next_boundary > _last_fired_boundary))

        if _should_fire_early:
            logger.warning("[KRONOS_HUNTER_LOOP] firing cycle for boundary %s (2min early)",
                           next_boundary.isoformat())
            try:
                summary = hunter.run_cycle(now=next_boundary)
                logger.warning("[KRONOS_HUNTER_LOOP] cycle done: %s", summary)
            except Exception as exc:
                logger.exception("[KRONOS_HUNTER_LOOP] cycle errored: %s", exc)
            _last_fired_boundary = next_boundary
            sleep_fn(1)
            continue

        # Sleep until fire window (2 min before next boundary), cap at 60s.
        wait = max(_secs_to_next - _fire_before_s, 1.0)
        sleep_fn(min(wait, 60.0))
    logger.warning("[KRONOS_HUNTER_LOOP] exited run_forever (master disabled)")
