"""
Trade replay simulator for the forex parameter optimizer.

Given a set of parameter values, answers: would this trade have been taken,
and what would the outcome be?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Session-aware spread model (pips)
# ---------------------------------------------------------------------------

SPREAD_MODEL: Dict[str, Dict[str, float]] = {
    "London_NY":  {"EUR_USD": 0.6, "GBP_USD": 1.0, "USD_JPY": 0.8, "EUR_GBP": 0.9, "default": 1.3},
    "London":     {"EUR_USD": 0.8, "GBP_USD": 1.2, "USD_JPY": 0.9, "EUR_GBP": 0.8, "default": 1.5},
    "New_York":   {"EUR_USD": 0.9, "GBP_USD": 1.3, "USD_JPY": 1.0, "EUR_GBP": 1.2, "default": 1.6},
    "Tokyo":      {"EUR_USD": 1.5, "GBP_USD": 2.5, "USD_JPY": 1.0, "AUD_JPY": 1.5, "default": 2.0},
    "Sydney":     {"EUR_USD": 2.0, "GBP_USD": 3.0, "USD_JPY": 1.5, "AUD_NZD": 2.0, "default": 2.5},
}
_DEFAULT_SPREAD = 1.5


def get_spread_cost(pair: Optional[str], session: Optional[str]) -> float:
    """Return estimated spread cost in pips for pair/session combo."""
    if not session:
        return _DEFAULT_SPREAD
    session_spreads = SPREAD_MODEL.get(session, SPREAD_MODEL.get("London", {}))
    return session_spreads.get(pair or "", session_spreads.get("default", _DEFAULT_SPREAD))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TradeSnapshot:
    """All data needed to replay a single trade."""

    id: str
    pair: str
    direction: str          # "buy"/"long" or "sell"/"short"
    outcome: str            # "win" or "loss"
    pnl_pips: float
    realized_pl: float      # USD
    fan_state: str           # expanding/contracting/just_crossed/stable/peaked/decelerating
    bb_width: Optional[float]   # Raw price difference (not pips)
    rsi: Optional[float]
    stoch_k: Optional[float]
    story_score: Optional[float]
    atr: Optional[float]        # Raw price ATR
    confidence: Optional[float]
    entry_price: float
    sl_price: Optional[float]
    tp_price: Optional[float]
    mfe: Optional[float]        # Max Favorable Excursion in pips
    mae: Optional[float]        # Max Adverse Excursion in pips
    session: Optional[str]
    # Optional extended fields
    adx: Optional[float] = None
    trend_health: Optional[float] = None
    fan_direction: Optional[str] = None
    fan_ordered: Optional[bool] = None
    momentum_state: Optional[str] = None
    stoch_d: Optional[float] = None
    source: Optional[str] = None
    entry_time: Optional[str] = None


# ---------------------------------------------------------------------------
# Unit conversion helpers
# ---------------------------------------------------------------------------

def _pip_size(pair: Optional[str]) -> float:
    """Return pip size for *pair*."""
    return 0.01 if "JPY" in (pair or "").upper() else 0.0001


def _bb_width_to_pips(raw_width: Optional[float], pair: Optional[str]) -> Optional[float]:
    """Convert stored BB width (price difference) to pips."""
    if raw_width is None:
        return None
    return raw_width / _pip_size(pair)


def _atr_to_pips(atr_raw: Optional[float], pair: Optional[str]) -> Optional[float]:
    """Convert raw ATR (price units) to pips."""
    if atr_raw is None:
        return None
    return atr_raw / _pip_size(pair)


# ---------------------------------------------------------------------------
# Gate replay
# ---------------------------------------------------------------------------

def gate_replay(trade: TradeSnapshot, params: dict) -> dict:
    """Check whether a trade would be blocked by the current gate parameters.

    Only checks gates whose keys are present in *params*.

    Returns
    -------
    dict with keys:
        blocked      : bool
        blocked_by   : list[str]  — human-readable gate names
        trade_id     : str
        outcome      : str
        pnl_pips     : float
    """
    blocked_by: list[str] = []
    is_buy = trade.direction.lower() in ("buy", "long")
    is_sell = trade.direction.lower() in ("sell", "short")

    # Gate: minimum BB width in pips
    if "gate.bb_width_min_pips" in params:
        bb_pips = _bb_width_to_pips(trade.bb_width, trade.pair)
        if bb_pips is not None and bb_pips < params["gate.bb_width_min_pips"]:
            blocked_by.append("bb_width")
        elif bb_pips is None:
            # No data — block conservatively only if the gate is set
            pass  # No data, skip gate (cannot evaluate)

    # Gate: stoch BUY filter — don't buy above threshold
    if "gate.stoch_dont_buy_above" in params and is_buy:
        if trade.stoch_k is not None and trade.stoch_k > params["gate.stoch_dont_buy_above"]:
            blocked_by.append("stoch_buy")

    # Gate: stoch SELL filter — don't sell below threshold
    if "gate.stoch_dont_sell_below" in params and is_sell:
        if trade.stoch_k is not None and trade.stoch_k < params["gate.stoch_dont_sell_below"]:
            blocked_by.append("stoch_sell")

    # Gate: minimum story score
    if "gate.story_score_min" in params:
        if trade.story_score is not None and trade.story_score < params["gate.story_score_min"]:
            blocked_by.append("story_score")

    # Gate: minimum confidence
    if "gate.confidence_base" in params:
        if trade.confidence is not None and trade.confidence < params["gate.confidence_base"]:
            blocked_by.append("confidence")

    # Gate: minimum risk/reward ratio
    if "gate.min_rr_ratio" in params:
        if trade.sl_price is not None and trade.tp_price is not None:
            sl_dist = abs(trade.entry_price - trade.sl_price)
            tp_dist = abs(trade.entry_price - trade.tp_price)
            if sl_dist > 0:
                rr = tp_dist / sl_dist
                if rr < params["gate.min_rr_ratio"]:
                    blocked_by.append("rr_ratio")

    return {
        "blocked": len(blocked_by) > 0,
        "blocked_by": blocked_by,
        "trade_id": trade.id,
        "outcome": trade.outcome,
        "pnl_pips": trade.pnl_pips,
    }


# ---------------------------------------------------------------------------
# SL/TP replay
# ---------------------------------------------------------------------------

def sltp_replay(trade: TradeSnapshot, params: dict) -> dict:
    """Simulate different SL/TP distances using ATR multipliers + MFE/MAE.

    Logic
    -----
    new_sl_pips = sl_atr_mult * atr_in_pips
    new_tp_pips = tp_atr_mult * atr_in_pips

    If MAE >= new_sl_pips  → loss at -new_sl_pips
    If MFE >= new_tp_pips  → win  at +new_tp_pips
    Otherwise              → use original result

    Returns
    -------
    dict with keys:
        trade_id, status, original_pnl, simulated_pnl, simulated_outcome,
        new_sl_pips, new_tp_pips, mfe, mae
    """
    atr_pips = _atr_to_pips(trade.atr, trade.pair)
    sl_mult = params.get("gate.sl_atr_mult")
    tp_mult = params.get("gate.tp_atr_mult")

    new_sl_pips: Optional[float] = None
    new_tp_pips: Optional[float] = None

    if atr_pips is not None and sl_mult is not None:
        new_sl_pips = sl_mult * atr_pips
    if atr_pips is not None and tp_mult is not None:
        new_tp_pips = tp_mult * atr_pips

    mae = trade.mae  # already in pips
    mfe = trade.mfe  # already in pips

    # Determine simulated outcome
    simulated_pnl = trade.pnl_pips
    simulated_outcome = trade.outcome
    status = "original"

    if new_sl_pips is not None and mae is not None and mae >= new_sl_pips:
        simulated_pnl = -new_sl_pips
        simulated_outcome = "loss"
        status = "sl_hit"
    elif new_tp_pips is not None and mfe is not None and mfe >= new_tp_pips:
        simulated_pnl = new_tp_pips
        simulated_outcome = "win"
        status = "tp_hit"

    return {
        "trade_id": trade.id,
        "status": status,
        "original_pnl": trade.pnl_pips,
        "simulated_pnl": simulated_pnl,
        "simulated_outcome": simulated_outcome,
        "new_sl_pips": new_sl_pips,
        "new_tp_pips": new_tp_pips,
        "mfe": mfe,
        "mae": mae,
    }


# ---------------------------------------------------------------------------
# Candle-walk replay (high-fidelity guardian simulation)
# ---------------------------------------------------------------------------

def candle_walk_replay(
    trade: TradeSnapshot,
    candles,  # pd.DataFrame with time, open, high, low, close
    params: dict,
    reaction_delay_bars: int = 1,
) -> dict:
    """Walk candles from entry to exit, simulating guardian behavior each bar.

    Works with any granularity (M1, M5, M15). At each candle the guardian:
      1. Computes unrealized P&L from entry_price
      2. Tracks peak profit (running MFE)
      3. When peak crosses a floor tier, locks that floor
      4. If unrealized P&L drops below locked floor → exit (with reaction delay)
      5. Checks SL/TP hit (if ATR-based SL/TP params present)
      6. Simulates trailing stop (activates after trailing_activation_rr, trails at ATR mult)

    The reaction_delay_bars parameter controls how many bars the guardian waits
    after detecting a floor breach before executing. Default 1 bar.
    For M1 candles: 1 bar = 1 minute (matches real guardian).
    For M15 candles: 1 bar = 15 minutes (overly conservative).

    Args:
        trade: The trade being replayed.
        candles: M15 OHLCV DataFrame covering entry through exit.
                 Must have columns: time, open, high, low, close.
        params: Tuning parameters to simulate.

    Returns dict with:
        simulated_pnl, simulated_outcome, exit_reason, exit_bar,
        peak_pips, floor_set_pips, bars_held, pips_saved
    """
    import numpy as np
    import pandas as pd

    pip = _pip_size(trade.pair)
    is_long = trade.direction.lower() in ("buy", "long")
    entry = trade.entry_price

    if candles is None or len(candles) < 2 or entry <= 0:
        return {
            "simulated_pnl": trade.pnl_pips,
            "simulated_outcome": trade.outcome,
            "exit_reason": "no_candles",
            "exit_bar": 0,
            "peak_pips": 0.0,
            "floor_set_pips": 0.0,
            "bars_held": 0,
            "pips_saved": 0.0,
        }

    # ── ATR-based SL/TP ──
    atr_pips = _atr_to_pips(trade.atr, trade.pair)
    sl_mult = params.get("gate.sl_atr_mult")
    tp_mult = params.get("gate.tp_atr_mult")
    sl_pips = sl_mult * atr_pips if (atr_pips and sl_mult) else None
    tp_pips = tp_mult * atr_pips if (atr_pips and tp_mult) else None

    # Use original SL/TP if no ATR params
    if sl_pips is None and trade.sl_price:
        sl_pips = abs(entry - trade.sl_price) / pip
    if tp_pips is None and trade.tp_price:
        tp_pips = abs(trade.tp_price - entry) / pip

    # ── Trailing stop params ──
    trail_activation_rr = params.get("guardian.trailing_activation_rr")
    trail_atr_mult = params.get("guardian.trailing_atr_mult")
    trail_dist_pips = trail_atr_mult * atr_pips if (atr_pips and trail_atr_mult) else None

    # ── Profit floor tiers ──
    floor_tiers = []
    for threshold, param_name in [
        (5.0, "guardian.profit_floor_5p"),
        (8.0, "guardian.profit_floor_8p"),
        (12.0, "guardian.profit_floor_12p"),
        (20.0, "guardian.profit_floor_20p"),
    ]:
        if param_name in params:
            floor_tiers.append((threshold, params[param_name]))
    floor_tiers.sort(key=lambda x: x[0])  # ascending by threshold

    ratchet = params.get("guardian.ratchet_step_pips", 5.0)

    # ── SL buffer ──
    sl_buffer = params.get("guardian.sl_buffer_pips", 0)

    # ── Walk candles ──
    peak_pips = 0.0
    locked_floor = 0.0
    trailing_active = False
    trailing_sl_pips = None  # distance from peak where trailing SL sits
    breach_bar = None  # bar where floor was breached (1-bar delay)

    for i in range(len(candles)):
        row = candles.iloc[i]

        # Unrealized P&L at candle extremes
        if is_long:
            best_pips = (row["high"] - entry) / pip
            worst_pips = (row["low"] - entry) / pip
            close_pips = (row["close"] - entry) / pip
        else:
            best_pips = (entry - row["low"]) / pip
            worst_pips = (entry - row["high"]) / pip
            close_pips = (entry - row["close"]) / pip

        # Update peak
        peak_pips = max(peak_pips, best_pips)

        # ── Check SL hit (worst of candle) ──
        effective_sl = sl_pips + sl_buffer if (sl_pips and sl_buffer) else sl_pips
        if effective_sl and worst_pips <= -effective_sl:
            return {
                "simulated_pnl": round(-effective_sl, 1),
                "simulated_outcome": "loss",
                "exit_reason": "sl_hit",
                "exit_bar": i,
                "peak_pips": round(peak_pips, 1),
                "floor_set_pips": round(locked_floor, 1),
                "bars_held": i,
                "pips_saved": round(max(0, -effective_sl - trade.pnl_pips), 1) if trade.pnl_pips < -effective_sl else 0.0,
            }

        # ── Check TP hit (best of candle) ──
        if tp_pips and best_pips >= tp_pips:
            return {
                "simulated_pnl": round(tp_pips, 1),
                "simulated_outcome": "win",
                "exit_reason": "tp_hit",
                "exit_bar": i,
                "peak_pips": round(peak_pips, 1),
                "floor_set_pips": round(locked_floor, 1),
                "bars_held": i,
                "pips_saved": round(tp_pips - trade.pnl_pips, 1) if trade.pnl_pips < tp_pips else 0.0,
            }

        # ── Update profit floor locks ──
        for threshold, pct in reversed(floor_tiers):  # check highest first
            if peak_pips >= threshold:
                raw = peak_pips * pct
                snapped = int(raw / ratchet) * ratchet if ratchet > 0 else raw
                locked_floor = max(locked_floor, snapped)
                break

        # ── Activate trailing stop ──
        if trail_activation_rr and sl_pips and not trailing_active:
            activation_pips = trail_activation_rr * sl_pips
            if peak_pips >= activation_pips:
                trailing_active = True

        # ── Trailing SL check ──
        if trailing_active and trail_dist_pips:
            trail_sl_level = peak_pips - trail_dist_pips
            if trail_sl_level > 0 and close_pips <= trail_sl_level:
                exit_pnl = max(trail_sl_level, locked_floor)
                return {
                    "simulated_pnl": round(exit_pnl, 1),
                    "simulated_outcome": "win" if exit_pnl > 0 else "loss",
                    "exit_reason": "trailing_sl",
                    "exit_bar": i,
                    "peak_pips": round(peak_pips, 1),
                    "floor_set_pips": round(locked_floor, 1),
                    "bars_held": i,
                    "pips_saved": round(exit_pnl - trade.pnl_pips, 1) if trade.pnl_pips < exit_pnl else 0.0,
                }

        # ── Floor breach check (configurable reaction delay) ──
        if locked_floor > 0 and close_pips < locked_floor:
            if breach_bar is not None and (i - breach_bar) >= reaction_delay_bars:
                # Breach held for reaction_delay_bars → guardian exits
                exit_pnl = close_pips  # exit at close (slippage from floor)
                exit_pnl = max(exit_pnl, locked_floor * 0.90)  # max ~10% slippage past floor
                return {
                    "simulated_pnl": round(exit_pnl, 1),
                    "simulated_outcome": "win" if exit_pnl > 0 else "loss",
                    "exit_reason": "floor_breach",
                    "exit_bar": i,
                    "peak_pips": round(peak_pips, 1),
                    "floor_set_pips": round(locked_floor, 1),
                    "bars_held": i,
                    "pips_saved": round(exit_pnl - trade.pnl_pips, 1) if trade.pnl_pips < exit_pnl else 0.0,
                }
            elif breach_bar is None:
                breach_bar = i  # first breach — start counting
        else:
            breach_bar = None  # reset if price recovers above floor

    # ── Trade ran to completion (no early exit triggered) ──
    return {
        "simulated_pnl": trade.pnl_pips,
        "simulated_outcome": trade.outcome,
        "exit_reason": "natural",
        "exit_bar": len(candles) - 1,
        "peak_pips": round(peak_pips, 1),
        "floor_set_pips": round(locked_floor, 1),
        "bars_held": len(candles) - 1,
        "pips_saved": 0.0,
    }


# ---------------------------------------------------------------------------
# Profit floor replay (guardian exit behavior — MFE-based approximation)
# ---------------------------------------------------------------------------

def profit_floor_replay(trade: TradeSnapshot, params: dict, current_pnl: float) -> dict:
    """Simulate guardian profit floor locks using MFE data.

    The guardian locks a percentage of peak profit at various thresholds:
      - guardian.profit_floor_5p:  lock X% when MFE >= 5 pips
      - guardian.profit_floor_8p:  lock X% when MFE >= 8 pips
      - guardian.profit_floor_12p: lock X% when MFE >= 12 pips
      - guardian.profit_floor_20p: lock X% when MFE >= 20 pips

    If the trade's actual pnl fell BELOW the locked floor, the guardian
    SHOULD have closed at the floor. Simulated pnl = max(actual, floor).

    Also simulates ratchet_step_pips: the profit floor ratchets up in steps,
    so the floor is rounded down to the nearest ratchet step.

    Args:
        trade: The trade snapshot.
        params: Dict with guardian.profit_floor_* and guardian.ratchet_step_pips.
        current_pnl: The pnl after gate + SL/TP simulation (may differ from original).

    Returns dict with floor_applied, floor_pips, pnl_after_floor, pips_saved.
    """
    mfe = trade.mfe
    if mfe is None or mfe <= 0:
        return {
            "floor_applied": False,
            "floor_pips": 0.0,
            "pnl_after_floor": current_pnl,
            "pips_saved": 0.0,
        }

    # Determine which floor tier applies (highest MFE tier wins)
    floor_pct = 0.0
    tiers = [
        (20.0, "guardian.profit_floor_20p"),
        (12.0, "guardian.profit_floor_12p"),
        (8.0, "guardian.profit_floor_8p"),
        (5.0, "guardian.profit_floor_5p"),
    ]
    for threshold, param_name in tiers:
        if mfe >= threshold and param_name in params:
            floor_pct = params[param_name]
            break  # Highest tier that applies

    if floor_pct <= 0:
        return {
            "floor_applied": False,
            "floor_pips": 0.0,
            "pnl_after_floor": current_pnl,
            "pips_saved": 0.0,
        }

    # Calculate floor in pips
    raw_floor = mfe * floor_pct

    # Apply ratchet step: floor snaps to ratchet increments
    ratchet = params.get("guardian.ratchet_step_pips", 5.0)
    if ratchet > 0:
        floor_pips = int(raw_floor / ratchet) * ratchet
    else:
        floor_pips = raw_floor

    # Floor must be positive to matter
    if floor_pips <= 0:
        return {
            "floor_applied": False,
            "floor_pips": 0.0,
            "pnl_after_floor": current_pnl,
            "pips_saved": 0.0,
        }

    # If actual pnl is below the floor, guardian would have closed at floor
    if current_pnl < floor_pips:
        pips_saved = floor_pips - current_pnl
        return {
            "floor_applied": True,
            "floor_pips": round(floor_pips, 1),
            "pnl_after_floor": round(floor_pips, 1),
            "pips_saved": round(pips_saved, 1),
        }

    return {
        "floor_applied": False,
        "floor_pips": round(floor_pips, 1),
        "pnl_after_floor": current_pnl,
        "pips_saved": 0.0,
    }


# ---------------------------------------------------------------------------
# Combined single-trade replay
# ---------------------------------------------------------------------------

def replay_trade(trade: TradeSnapshot, params: dict, candles=None,
                  apply_spread: bool = False) -> dict:
    """Combine gate + exit simulation for a single trade.

    Pipeline:
      1. Gate check — would this trade have been taken?
      2. If candles provided: candle-walk replay (high fidelity)
         Else: MFE-based SL/TP + profit floor approximation (fast)
      3. Optionally deduct session-aware spread cost.

    Args:
        trade: The trade snapshot.
        params: Tuning parameters to simulate.
        candles: Optional M15 DataFrame for candle-walk replay.
        apply_spread: If True, deduct session-aware spread from simulated_pnl.

    Returns dict with:
        trade_id, blocked, blocked_by,
        original_outcome, original_pnl, original_usd,
        simulated_outcome, simulated_pnl, simulated_usd,
        exit_reason, pips_saved
    """
    gate_result = gate_replay(trade, params)

    if gate_result["blocked"]:
        return {
            "trade_id": trade.id,
            "blocked": True,
            "blocked_by": gate_result["blocked_by"],
            "original_outcome": trade.outcome,
            "original_pnl": trade.pnl_pips,
            "original_usd": trade.realized_pl,
            "simulated_outcome": "blocked",
            "simulated_pnl": 0.0,
            "simulated_usd": 0.0,
            "exit_reason": "blocked",
            "pips_saved": 0.0,
        }

    # Spread cost (deducted at end if enabled)
    spread = get_spread_cost(trade.pair, trade.session) if apply_spread else 0.0

    # ── High-fidelity path: candle walk ──
    if candles is not None and len(candles) > 1:
        cw = candle_walk_replay(trade, candles, params)
        pnl = cw["simulated_pnl"] - spread
        outcome = cw["simulated_outcome"]
        if apply_spread and outcome == "win" and pnl <= 0:
            outcome = "loss"
        return {
            "trade_id": trade.id,
            "blocked": False,
            "blocked_by": [],
            "original_outcome": trade.outcome,
            "original_pnl": trade.pnl_pips,
            "original_usd": trade.realized_pl,
            "simulated_outcome": outcome,
            "simulated_pnl": pnl,
            "simulated_usd": trade.realized_pl,
            "exit_reason": cw["exit_reason"],
            "pips_saved": cw["pips_saved"],
        }

    # ── Fallback: MFE-based approximation ──
    sltp_result = sltp_replay(trade, params)
    sim_pnl = sltp_result["simulated_pnl"]
    sim_outcome = sltp_result["simulated_outcome"]

    floor_result = profit_floor_replay(trade, params, sim_pnl)
    final_pnl = floor_result["pnl_after_floor"] - spread

    if floor_result["floor_applied"] and final_pnl > 0 and sim_pnl <= 0:
        sim_outcome = "win"
    elif final_pnl > 0:
        sim_outcome = "win"
    elif apply_spread and sim_outcome == "win" and final_pnl <= 0:
        sim_outcome = "loss"

    return {
        "trade_id": trade.id,
        "blocked": False,
        "blocked_by": [],
        "original_outcome": trade.outcome,
        "original_pnl": trade.pnl_pips,
        "original_usd": trade.realized_pl,
        "simulated_outcome": sim_outcome,
        "simulated_pnl": round(final_pnl, 1),
        "simulated_usd": trade.realized_pl,
        "exit_reason": "mfe_approx",
        "pips_saved": floor_result["pips_saved"],
    }


# ---------------------------------------------------------------------------
# Portfolio replay
# ---------------------------------------------------------------------------

def replay_all_trades(
    trades: List[TradeSnapshot],
    params: dict,
    candles_by_trade: Optional[dict] = None,
    apply_spread: bool = False,
    trade_weights: Optional[Dict[str, float]] = None,
) -> dict:
    """Replay every trade and return portfolio-level metrics.

    Args:
        trades: List of TradeSnapshot objects.
        params: Tuning parameters to simulate.
        candles_by_trade: Optional dict of {trade_id: DataFrame}.
            When provided, uses candle-walk replay (high fidelity).
            When None, falls back to MFE-based approximation (fast).
        apply_spread: If True, deduct session-aware spread costs.
        trade_weights: Optional dict of {trade_id: weight} for time-decay
            weighted WR calculation. If None, all trades weighted equally.

    Returns dict with:
        total, blocked, remaining, wins, losses, win_rate,
        total_pips, avg_pips, blocked_wins, blocked_losses, blocked_pnl,
        net_pips_improvement, trades
    """
    results = []
    blocked_wins = 0
    blocked_losses = 0
    blocked_pnl = 0.0
    active_wins = 0
    active_losses = 0
    total_pips = 0.0
    original_total_pips = 0.0

    for trade in trades:
        trade_candles = candles_by_trade.get(trade.id) if candles_by_trade else None
        result = replay_trade(trade, params, candles=trade_candles,
                              apply_spread=apply_spread)
        results.append(result)
        original_total_pips += trade.pnl_pips

        if result["blocked"]:
            if trade.outcome == "win":
                blocked_wins += 1
            else:
                blocked_losses += 1
            blocked_pnl += trade.pnl_pips
        else:
            total_pips += result["simulated_pnl"]
            if result["simulated_outcome"] == "win":
                active_wins += 1
            else:
                active_losses += 1

    total = len(trades)
    blocked = blocked_wins + blocked_losses
    remaining = total - blocked
    wins = active_wins
    losses = active_losses

    # Win rate: weighted if trade_weights provided, else simple
    if trade_weights and remaining > 0:
        weighted_wins = 0.0
        weighted_total = 0.0
        for trade, r in zip(trades, results):
            if not r["blocked"]:
                w = trade_weights.get(trade.id, 1.0)
                weighted_total += w
                if r["simulated_outcome"] == "win":
                    weighted_wins += w
        win_rate = (weighted_wins / weighted_total * 100) if weighted_total > 0 else 0.0
    else:
        win_rate = (wins / remaining * 100) if remaining > 0 else 0.0

    avg_pips = total_pips / remaining if remaining > 0 else 0.0
    net_pips_improvement = total_pips - (original_total_pips - blocked_pnl)

    # Gross win/loss pips for Profit Factor
    gross_win_pips = sum(
        r["simulated_pnl"] for r in results
        if not r["blocked"] and r["simulated_pnl"] > 0
    )
    gross_loss_pips = sum(
        r["simulated_pnl"] for r in results
        if not r["blocked"] and r["simulated_pnl"] <= 0
    )

    # Max Drawdown (peak-to-trough of running equity curve)
    equity = 0.0
    peak_equity = 0.0
    max_dd = 0.0
    for r in results:
        if not r["blocked"]:
            equity += r["simulated_pnl"]
            peak_equity = max(peak_equity, equity)
            dd = peak_equity - equity
            max_dd = max(max_dd, dd)

    # Profit Factor
    if gross_loss_pips == 0:
        profit_factor = 99.9
    else:
        profit_factor = gross_win_pips / abs(gross_loss_pips)

    # Calmar Ratio
    if max_dd == 0:
        calmar_ratio = 99.9
    elif total_pips <= 0:
        calmar_ratio = 0.0
    else:
        calmar_ratio = total_pips / max_dd

    return {
        "total": total,
        "blocked": blocked,
        "remaining": remaining,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pips": total_pips,
        "avg_pips": avg_pips,
        "blocked_wins": blocked_wins,
        "blocked_losses": blocked_losses,
        "blocked_pnl": blocked_pnl,
        "net_pips_improvement": net_pips_improvement,
        "gross_win_pips": gross_win_pips,
        "gross_loss_pips": gross_loss_pips,
        "max_drawdown_pips": max_dd,
        "profit_factor": profit_factor,
        "calmar_ratio": calmar_ratio,
        "trades": results,
    }


# ---------------------------------------------------------------------------
# Candle cache loader
# ---------------------------------------------------------------------------

def load_candles_for_trades(
    trades: List[TradeSnapshot],
    granularity: str = "M15",
) -> dict:
    """Fetch and cache candles for all trades at the specified granularity.

    Args:
        trades: List of TradeSnapshot objects.
        granularity: OANDA granularity — 'M1', 'M5', 'M10', 'M15'.
            M1 matches real guardian behavior (60s evaluation cycle).
            M15 is faster to fetch but 15x less reactive.

    Returns dict of {trade_id: DataFrame} ready for candle_walk_replay.
    """
    import logging
    import sqlite3
    import time

    import pandas as pd

    from backtester.data_fetcher import fetch_candles
    from optimizer.backfill import _oanda_candles_to_df

    logger = logging.getLogger("optimizer.replay")

    # Minutes per candle for this granularity
    gran_minutes = {"M1": 1, "M5": 5, "M10": 10, "M15": 15}.get(granularity, 15)

    # Get entry/exit times from DB
    from db_pool import get_trading_forex
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    time_map = {}
    for row in conn.execute(
        "SELECT id, entry_time, exit_time FROM live_trades WHERE exit_time IS NOT NULL"
    ).fetchall():
        time_map[str(row["id"])] = (row["entry_time"], row["exit_time"])

    candles_by_trade = {}
    trade_ids_by_pair = {}
    for t in trades:
        if t.entry_price <= 0 or t.id not in time_map:
            continue
        trade_ids_by_pair.setdefault(t.pair, []).append(t)

    for pair, pair_trades in trade_ids_by_pair.items():
        logger.info("Fetching %s candles for %s (%d trades)", granularity, pair, len(pair_trades))
        oanda_pair = pair.replace("/", "_").upper()

        for t in pair_trades:
            entry_time, exit_time = time_map.get(t.id, (None, None))
            if not entry_time or not exit_time:
                continue
            try:
                entry_dt = pd.Timestamp(entry_time)
                exit_dt = pd.Timestamp(exit_time)

                # Fetch window: 5 bars before entry through exit + 2 bars after
                from_dt = entry_dt - pd.Timedelta(minutes=gran_minutes * 5)
                to_dt = exit_dt + pd.Timedelta(minutes=gran_minutes * 2)
                from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                to_str = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

                raw = fetch_candles(oanda_pair, granularity, from_str, to_str)
                if raw:
                    df = _oanda_candles_to_df(raw)
                    # Trim to entry - 1 bar through exit + 1 bar
                    mask = (
                        (df["time"] >= entry_dt - pd.Timedelta(minutes=gran_minutes))
                        & (df["time"] <= exit_dt + pd.Timedelta(minutes=gran_minutes))
                    )
                    trade_df = df[mask].reset_index(drop=True)
                    if len(trade_df) > 1:
                        candles_by_trade[t.id] = trade_df

                time.sleep(0.3)  # rate limit
            except Exception as e:
                logger.warning("Failed to fetch candles for trade %s: %s", t.id, e)

    logger.info("Loaded %s candles for %d / %d trades", granularity, len(candles_by_trade), len(trades))
    return candles_by_trade
