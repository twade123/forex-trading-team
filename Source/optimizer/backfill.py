#!/usr/bin/env python3
"""
Indicator Backfill Script — Stage 1 of the parameter optimizer.

Fetches M15 candles from OANDA for each trade's entry time, computes all
indicators at that candle, and writes them back to live_trades.  Also computes
MFE/MAE (Maximum Favorable/Adverse Excursion) for every trade by walking
candles from entry to exit.

Usage:
    python -m optimizer.backfill            # live run
    python -m optimizer.backfill --dry-run  # preview without DB writes
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OANDA candle → DataFrame helper
# ---------------------------------------------------------------------------

def _oanda_candles_to_df(candles: list) -> pd.DataFrame:
    """Convert a list of OANDA candle dicts to a tidy OHLCV DataFrame."""
    rows = []
    for c in candles:
        mid = c.get("mid", {})
        rows.append({
            "time": pd.Timestamp(c["time"]),
            "open": float(mid.get("o", 0)),
            "high": float(mid.get("h", 0)),
            "low": float(mid.get("l", 0)),
            "close": float(mid.get("c", 0)),
            "volume": float(c.get("volume", 0)),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("time").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Pair normalisation helpers
# ---------------------------------------------------------------------------

def _normalise_pair(pair: str) -> str:
    """Convert 'EUR/USD' → 'EUR_USD'; pass through 'EUR_USD' unchanged."""
    return pair.replace("/", "_").upper()


def _pip_size(pair: str) -> float:
    """Return pip size (in price units) for a given pair."""
    normalised = _normalise_pair(pair)
    if "JPY" in normalised:
        return 0.01
    return 0.0001


# ---------------------------------------------------------------------------
# Fan-state classifier
# ---------------------------------------------------------------------------

def classify_fan_state(
    ema8: float, ema21: float, ema55: float, ema100: float,
    prev_ema8: float, prev_ema21: float, prev_ema55: float, prev_ema100: float,
) -> tuple:
    """
    Classify the EMA fan state from four EMA values at the current and
    previous candle.

    Returns:
        (fan_state, fan_direction, fan_ordered, fan_width_pct)

    fan_state values:
        expanding, just_crossed, contracting, decelerating, peaked, stable, unknown
    fan_direction: 'bull', 'bear', or 'neutral'
    fan_ordered: True if EMAs are in strict monotonic order (bull or bear)
    fan_width_pct: abs(ema8 - ema100) / midpoint * 100
    """
    if any(v is None or (isinstance(v, float) and v != v) for v in
           [ema8, ema21, ema55, ema100, prev_ema8, prev_ema21, prev_ema55, prev_ema100]):
        return "unknown", "neutral", False, 0.0

    bull_ordered = ema8 > ema21 > ema55 > ema100
    bear_ordered = ema8 < ema21 < ema55 < ema100

    if bull_ordered:
        fan_direction = "bull"
        fan_ordered = True
    elif bear_ordered:
        fan_direction = "bear"
        fan_ordered = True
    else:
        fan_direction = "neutral"
        fan_ordered = False

    midpoint = (ema8 + ema100) / 2
    fan_width_pct = abs(ema8 - ema100) / midpoint * 100 if midpoint != 0 else 0.0

    prev_midpoint = (prev_ema8 + prev_ema100) / 2
    prev_width_pct = abs(prev_ema8 - prev_ema100) / prev_midpoint * 100 if prev_midpoint != 0 else 0.0

    width_change = fan_width_pct - prev_width_pct

    if fan_width_pct < 0.15:
        fan_state = "just_crossed"
    elif width_change > 0.02:
        fan_state = "expanding"
    elif width_change < -0.02:
        if fan_ordered:
            fan_state = "contracting"   # valid retrace territory
        else:
            fan_state = "decelerating"
    elif abs(width_change) <= 0.02:
        # Minimal change — check if we're near a peak
        if prev_width_pct > fan_width_pct and fan_width_pct > 0.3:
            fan_state = "peaked"
        else:
            fan_state = "stable"
    else:
        fan_state = "unknown"

    return fan_state, fan_direction, fan_ordered, round(fan_width_pct, 4)


# ---------------------------------------------------------------------------
# Indicator computation
# ---------------------------------------------------------------------------

def compute_indicators_at_time(df: pd.DataFrame, target_time, pair: str) -> dict:
    """
    Compute all trading indicators at the candle nearest to *target_time*.

    Args:
        df:          M15 OHLCV DataFrame (columns: time, open, high, low, close, volume)
        target_time: pd.Timestamp or str for the target candle
        pair:        Instrument string (used only for context; indicators are price-based)

    Returns:
        dict with keys: rsi, stoch_k, stoch_d, bb_width, bb_upper, bb_lower,
        bb_mid, bb_expanding, atr, adx, fan_state, fan_direction, fan_ordered,
        fan_width_pct, trend_health, ema_8, ema_21, ema_55, ema_100,
        momentum_state
    """
    from backtester.indicators import (
        rsi as compute_rsi,
        stochastic,
        bollinger_bands,
        atr as compute_atr,
        ema,
        adx as compute_adx,
    )

    if not isinstance(target_time, pd.Timestamp):
        target_time = pd.Timestamp(target_time)
    # Make timezone-naive for comparison
    if target_time.tzinfo is not None:
        target_time = target_time.tz_localize(None)

    df = df.copy()
    if not df.empty and df["time"].iloc[0].tzinfo is not None:
        df["time"] = df["time"].dt.tz_localize(None)

    # Find the index of the candle at-or-just-before target_time
    mask = df["time"] <= target_time
    if not mask.any():
        logger.warning("No candle found at or before %s", target_time)
        return {}

    idx = df[mask].index[-1]

    # Compute indicators on the slice up to (and including) target candle
    sub = df.loc[: idx].copy()

    rsi_series = compute_rsi(sub)
    stoch_df = stochastic(sub)
    bb_df = bollinger_bands(sub)
    atr_series = compute_atr(sub)
    adx_df = compute_adx(sub)

    ema8_series = ema(sub, 8)
    ema21_series = ema(sub, 21)
    ema55_series = ema(sub, 55)
    ema100_series = ema(sub, 100)

    def _last(series):
        val = series.iloc[-1] if len(series) > 0 else float("nan")
        return float(val) if pd.notna(val) else None

    def _prev(series):
        val = series.iloc[-2] if len(series) > 1 else float("nan")
        return float(val) if pd.notna(val) else None

    e8 = _last(ema8_series)
    e21 = _last(ema21_series)
    e55 = _last(ema55_series)
    e100 = _last(ema100_series)

    pe8 = _prev(ema8_series)
    pe21 = _prev(ema21_series)
    pe55 = _prev(ema55_series)
    pe100 = _prev(ema100_series)

    fan_state, fan_direction, fan_ordered, fan_width_pct = classify_fan_state(
        e8, e21, e55, e100, pe8, pe21, pe55, pe100
    )

    # Bollinger expanding: current width > previous width
    bb_width_cur = _last(bb_df["bb_width"]) if "bb_width" in bb_df else None
    bb_width_prev = _prev(bb_df["bb_width"]) if "bb_width" in bb_df else None
    bb_expanding = None
    if bb_width_cur is not None and bb_width_prev is not None:
        bb_expanding = int(bb_width_cur > bb_width_prev)

    # Trend health: composite of fan_ordered + adx strength
    adx_val = _last(adx_df["adx"]) if "adx" in adx_df else None
    if fan_ordered and adx_val is not None:
        trend_health = min(1.0, adx_val / 50.0)
    elif adx_val is not None:
        trend_health = min(0.5, adx_val / 100.0)
    else:
        trend_health = None

    # Momentum state from RSI
    rsi_val = _last(rsi_series)
    if rsi_val is None:
        momentum_state = "unknown"
    elif rsi_val >= 70:
        momentum_state = "overbought"
    elif rsi_val <= 30:
        momentum_state = "oversold"
    elif rsi_val >= 55:
        momentum_state = "bullish"
    elif rsi_val <= 45:
        momentum_state = "bearish"
    else:
        momentum_state = "neutral"

    # bb_width stored as raw price diff (upper - lower) for live_trades column
    bb_upper = _last(bb_df["bb_upper"]) if "bb_upper" in bb_df else None
    bb_lower = _last(bb_df["bb_lower"]) if "bb_lower" in bb_df else None
    bb_mid = _last(bb_df["bb_middle"]) if "bb_middle" in bb_df else None
    bb_width_raw = (bb_upper - bb_lower) if (bb_upper is not None and bb_lower is not None) else None

    return {
        "rsi": rsi_val,
        "stoch_k": _last(stoch_df["stoch_k"]) if "stoch_k" in stoch_df else None,
        "stoch_d": _last(stoch_df["stoch_d"]) if "stoch_d" in stoch_df else None,
        "bb_width": bb_width_raw,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "bb_mid": bb_mid,
        "bb_expanding": bb_expanding,
        "atr": _last(atr_series),
        "adx": adx_val,
        "fan_state": fan_state,
        "fan_direction": fan_direction,
        "fan_ordered": int(fan_ordered),
        "fan_width_pct": fan_width_pct,
        "trend_health": trend_health,
        "ema_8": e8,
        "ema_21": e21,
        "ema_55": e55,
        "ema_100": e100,
        "momentum_state": momentum_state,
    }


# ---------------------------------------------------------------------------
# MFE / MAE computation
# ---------------------------------------------------------------------------

def compute_mfe_mae(
    df: pd.DataFrame,
    entry_idx: int,
    direction: str,
    entry_price: float,
    pair: str,
) -> tuple:
    """
    Walk candles from *entry_idx* to end of DataFrame, computing:
      - MFE (Maximum Favorable Excursion) in pips
      - MAE (Maximum Adverse Excursion) in pips

    Both values are returned as positive numbers.

    Args:
        df:          OHLCV DataFrame
        entry_idx:   Integer row index of the entry candle
        direction:   'buy' or 'sell'
        entry_price: Entry price
        pair:        Instrument (used for pip size)

    Returns:
        (mfe_pips, mae_pips) — both positive floats
    """
    pip = _pip_size(pair)
    direction = direction.lower()

    walk = df.iloc[entry_idx:]
    max_favorable = 0.0
    max_adverse = 0.0

    for _, row in walk.iterrows():
        if direction == "buy":
            favorable = row["high"] - entry_price
            adverse = entry_price - row["low"]
        else:  # sell
            favorable = entry_price - row["low"]
            adverse = row["high"] - entry_price

        if favorable > max_favorable:
            max_favorable = favorable
        if adverse > max_adverse:
            max_adverse = adverse

    mfe_pips = round(max_favorable / pip, 1)
    mae_pips = round(max_adverse / pip, 1)
    return mfe_pips, mae_pips


# ---------------------------------------------------------------------------
# Candle fetch
# ---------------------------------------------------------------------------

def fetch_candles_for_trade(
    pair: str,
    entry_time,
    exit_time,
    pre_candles: int = 120,
    post_candles: int = 10,
) -> pd.DataFrame:
    """
    Fetch M15 candles for a trade, including warmup candles before entry.

    Args:
        pair:          Instrument (e.g. 'EUR/USD' or 'EUR_USD')
        entry_time:    Entry time as string or datetime
        exit_time:     Exit time as string or datetime
        pre_candles:   Number of M15 candles before entry for indicator warmup
        post_candles:  Extra candles after exit for final MFE/MAE check

    Returns:
        DataFrame with columns: time, open, high, low, close, volume
    """
    from backtester.data_fetcher import fetch_candles

    instrument = _normalise_pair(pair)

    # Parse times — use pd.Timestamp which handles nanosecond precision
    entry_dt = pd.Timestamp(entry_time)
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.tz_localize("UTC")
    entry_dt = entry_dt.to_pydatetime()

    exit_dt = pd.Timestamp(exit_time)
    if exit_dt.tzinfo is None:
        exit_dt = exit_dt.tz_localize("UTC")
    exit_dt = exit_dt.to_pydatetime()

    # M15 = 15 minutes per candle
    from_dt = entry_dt - timedelta(minutes=15 * pre_candles)
    to_dt = exit_dt + timedelta(minutes=15 * post_candles)

    from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_str = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    candles = fetch_candles(instrument=instrument, granularity="M15", from_time=from_str, to_time=to_str)
    return _oanda_candles_to_df(candles)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_trades_needing_backfill() -> list:
    """
    Return closed trades that are missing at least one key indicator.

    A trade qualifies if:
      - exit_time IS NOT NULL
      - entry_price > 0
      - any of (fan_state, bb_width, rsi, atr, max_favorable_excursion_pips) is NULL
    """
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from db_pool import get_trading_forex

    conn = get_trading_forex()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, pair, direction, entry_price, entry_time, exit_time,
               fan_state, bb_width, rsi, atr, max_favorable_excursion_pips
        FROM live_trades
        WHERE exit_time IS NOT NULL
          AND entry_price > 0
          AND (
              fan_state IS NULL
              OR bb_width IS NULL
              OR rsi IS NULL
              OR atr IS NULL
              OR max_favorable_excursion_pips IS NULL
          )
        ORDER BY entry_time
    """)
    cols = [desc[0] for desc in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def backfill_trade(trade: dict, dry_run: bool = False) -> dict:
    """
    Backfill indicator data for a single trade.

    Only NULL columns are updated; existing non-NULL values are left untouched.

    Args:
        trade:   Row dict from get_trades_needing_backfill()
        dry_run: If True, compute but do not write to DB

    Returns:
        Result dict with keys: trade_id, status, updates, error
    """
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from db_pool import get_trading_forex

    trade_id = trade["id"]
    result = {"trade_id": trade_id, "status": "skipped", "updates": {}, "error": None}

    try:
        df = fetch_candles_for_trade(
            pair=trade["pair"],
            entry_time=trade["entry_time"],
            exit_time=trade["exit_time"],
        )
    except Exception as exc:
        logger.error("Failed to fetch candles for trade %s: %s", trade_id, exc)
        result["status"] = "error"
        result["error"] = str(exc)
        return result

    if df.empty:
        logger.warning("No candles returned for trade %s", trade_id)
        result["status"] = "error"
        result["error"] = "empty candle response"
        return result

    # Locate entry candle index
    entry_time = pd.Timestamp(trade["entry_time"])
    if entry_time.tzinfo is not None:
        entry_time = entry_time.tz_localize(None)
    df_times = df["time"].dt.tz_localize(None) if df["time"].iloc[0].tzinfo is not None else df["time"]

    mask = df_times <= entry_time
    if not mask.any():
        result["status"] = "error"
        result["error"] = "entry time before all fetched candles"
        return result

    entry_idx = df[mask].index[-1]

    # Compute indicators
    indicators = compute_indicators_at_time(df, entry_time, trade["pair"])

    # Compute MFE/MAE
    mfe_pips, mae_pips = compute_mfe_mae(
        df, entry_idx, trade["direction"], trade["entry_price"], trade["pair"]
    )

    # Build updates: only set columns that are currently NULL
    updates = {}

    _indicator_col_map = {
        "fan_state": "fan_state",
        "fan_direction": "fan_direction",
        "fan_ordered": "fan_ordered",
        "fan_width_pct": "fan_width_pct",
        "bb_width": "bb_width",
        "bb_upper": "bb_upper",
        "bb_lower": "bb_lower",
        "bb_mid": "bb_mid",
        "bb_expanding": "bb_expanding",
        "rsi": "rsi",
        "stoch_k": "stoch_k",
        "stoch_d": "stoch_d",
        "atr": "atr",
        "adx": "adx",
        "trend_health": "trend_health",
        "momentum_state": "momentum_state",
    }

    for ind_key, db_col in _indicator_col_map.items():
        if trade.get(db_col) is None and ind_key in indicators and indicators[ind_key] is not None:
            updates[db_col] = indicators[ind_key]

    if trade.get("max_favorable_excursion_pips") is None:
        updates["max_favorable_excursion_pips"] = mfe_pips
        updates["max_adverse_excursion_pips"] = mae_pips

    if not updates:
        result["status"] = "skipped"
        return result

    result["updates"] = updates

    if not dry_run:
        conn = get_trading_forex()
        cursor = conn.cursor()
        set_clause = ", ".join(f"{col} = ?" for col in updates)
        values = list(updates.values()) + [trade_id]
        cursor.execute(f"UPDATE live_trades SET {set_clause} WHERE id = ?", values)
        conn.commit()

    result["status"] = "updated"
    return result


# ---------------------------------------------------------------------------
# Main run loop
# ---------------------------------------------------------------------------

def run_backfill(dry_run: bool = False, rate_limit: float = 0.6) -> dict:
    """
    Run the full backfill across all trades needing data.

    Trades are grouped by pair to reduce context-switching; a short sleep
    between API calls avoids hitting OANDA rate limits.

    Args:
        dry_run:    If True, no DB writes are made.
        rate_limit: Seconds to sleep between OANDA API calls.

    Returns:
        Summary dict: {total, updated, failed, skipped}
    """
    trades = get_trades_needing_backfill()
    logger.info("Found %d trades needing backfill", len(trades))

    summary = {"total": len(trades), "updated": 0, "failed": 0, "skipped": 0}

    # Group by pair
    from collections import defaultdict
    by_pair: dict = defaultdict(list)
    for t in trades:
        by_pair[t["pair"]].append(t)

    for pair, pair_trades in by_pair.items():
        logger.info("Processing pair %s (%d trades)", pair, len(pair_trades))
        for trade in pair_trades:
            result = backfill_trade(trade, dry_run=dry_run)
            if result["status"] == "updated":
                summary["updated"] += 1
                logger.info("  [OK] trade %s — updated %s", result["trade_id"], list(result["updates"].keys()))
            elif result["status"] == "error":
                summary["failed"] += 1
                logger.warning("  [FAIL] trade %s — %s", result["trade_id"], result["error"])
            else:
                summary["skipped"] += 1
                logger.debug("  [SKIP] trade %s", result["trade_id"])

            time.sleep(rate_limit)

    logger.info(
        "Backfill complete: total=%d updated=%d failed=%d skipped=%d",
        summary["total"], summary["updated"], summary["failed"], summary["skipped"],
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(description="Backfill indicator data for live_trades.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute indicators but do not write to the database.",
    )
    args = parser.parse_args()

    summary = run_backfill(dry_run=args.dry_run)
    print(summary)
