#!/usr/bin/env python3
"""
Strategy Comparison — EMA/BB Cascade Overlay on Existing Sniper Backtest Data

Uses proven sniper trades from the database (backtest_trades table).
For each trade, fetches M15 candles around entry and computes:
- EMA 21/55/100 separation, velocity, fan state at entry + 50 candles forward
- BB width evolution, squeeze/expansion detection
- Cascade length, MFE, optimal exit at EMA peak separation
- Thesis-only signal detection on the same candle data

Also runs a standalone thesis-only backtest across the same date range.

NO re-running of score_v4. Uses existing DB results.
"""

import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtester import indicators
from backtester.data_fetcher import fetch_candles, candles_to_rows
from backtester.ema_separation import calculate_ema as ema_calc_pure, is_nan

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════

DB_PATH = Path("~/jarvis/Database/v2/trading_forex.db")

# Best sniper setup from DB analysis
SNIPER_SETUP = "S15_rr2.0_sl2.5"
TIMEFRAME = "M15"

PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_JPY", "EUR_AUD",
    "GBP_JPY", "USD_CHF", "NZD_USD", "EUR_GBP", "EUR_JPY",
    "AUD_USD", "USD_CAD",
]

JPY_PAIRS = {"USD_JPY", "AUD_JPY", "GBP_JPY", "EUR_JPY"}

LOOKFORWARD = 50  # candles after entry to track
LOOKBACK_EMAS = 150  # candles before entry needed for EMA warmup

# Thesis-only parameters
THESIS_BB_SQUEEZE_WIDTH = 0.008
THESIS_EMA_SEP_ENTRY = 0.04

# How many trades per pair to sample for overlay (full cascade analysis)
OVERLAY_SAMPLE_PER_PAIR = 50

# Max trades per pair to process (sample evenly across time range)
MAX_TRADES_PER_PAIR = 200

# Rate limit between OANDA fetches
FETCH_DELAY = 0.3


def pip_value(pair: str) -> float:
    return 0.01 if pair in JPY_PAIRS else 0.0001


# ══════════════════════════════════════════════════════════════════
# DATABASE LOADING
# ══════════════════════════════════════════════════════════════════

def load_sniper_trades(pair: str) -> pd.DataFrame:
    """Load all sniper trades for a pair from the database."""
    conn = sqlite3.connect(DB_PATH)
    query = """
        SELECT pair, direction, entry_time, exit_time, entry_price, exit_price,
               sl_price, tp_price, result, pips, regime, atr,
               max_favorable_pips, max_adverse_pips, candles_to_exit,
               rsi, bb_width, confidence, h4_trend, h4_agrees
        FROM backtest_trades
        WHERE timeframe = ? AND setup = ? AND pair = ?
        ORDER BY entry_time
    """
    df = pd.read_sql_query(query, conn, params=[TIMEFRAME, SNIPER_SETUP, pair])
    conn.close()
    logger.info(f"  Loaded {len(df)} sniper trades for {pair}")
    return df


# ══════════════════════════════════════════════════════════════════
# CANDLE DATA FETCHING (chunked by month to avoid huge requests)
# ══════════════════════════════════════════════════════════════════

def fetch_pair_candles(pair: str, start: str, end: str) -> pd.DataFrame:
    """Fetch M15 candles for a pair over a date range, compute indicators."""
    logger.info(f"  Fetching M15 candles for {pair} from {start[:10]} to {end[:10]}...")
    raw = fetch_candles(pair, "M15", start, end)
    rows = candles_to_rows(raw)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df.rename(columns={"timestamp": "time"}, inplace=True)
    df["time"] = pd.to_datetime(df["time"])
    df = indicators.compute_all(df)

    # EMA separation
    df["ema_sep_pct"] = (df["ema_21"] - df["ema_55"]).abs() / df["close"] * 100
    df["ema_sep_velocity"] = df["ema_sep_pct"].diff()
    df["bb_width_roc"] = df["bb_width"].diff()

    # Fan state columns
    df["fan_bullish"] = (df["ema_21"] > df["ema_55"]) & (df["ema_55"] > df["ema_100"])
    df["fan_bearish"] = (df["ema_100"] > df["ema_55"]) & (df["ema_55"] > df["ema_21"])

    # EMA cross detection
    df["ema_21_above_55"] = df["ema_21"] > df["ema_55"]
    df["ema_cross_up"] = df["ema_21_above_55"] & ~df["ema_21_above_55"].shift(1, fill_value=False)
    df["ema_cross_down"] = ~df["ema_21_above_55"] & df["ema_21_above_55"].shift(1, fill_value=True)

    logger.info(f"    Got {len(df)} candles")
    return df


def fetch_candles_for_window(pair: str, center_time: str) -> pd.DataFrame:
    """Fetch ~400 M15 candles around a given time (200 before + 200 after).
    That's about 4 days of data, enough for EMA warmup + lookforward."""
    ct = pd.to_datetime(center_time, utc=True)
    # 200 candles * 15min = 50 hours before, 100 after
    start = (ct - timedelta(hours=55)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (ct + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return fetch_pair_candles(pair, start, end)


def fetch_candles_chunked(pair: str, trades_df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """
    Group trades by ~10-day windows and fetch one candle chunk per window.
    Returns dict mapping window_key -> candles DataFrame.
    Each chunk has enough data for EMA warmup + lookforward.
    """
    if trades_df.empty:
        return {}

    # Group trades into 7-day windows
    trades_df = trades_df.copy()
    trades_df["_et"] = pd.to_datetime(trades_df["entry_time"], utc=True)
    trades_df = trades_df.sort_values("_et")

    chunks = {}
    current_chunk_trades = []
    chunk_start = trades_df["_et"].iloc[0]

    for _, trade in trades_df.iterrows():
        if trade["_et"] - chunk_start > timedelta(days=7) and current_chunk_trades:
            # Fetch this chunk
            mid_time = chunk_start + (trade["_et"] - chunk_start) / 2
            chunk_key = chunk_start.strftime("%Y%m%d")
            cs = (chunk_start - timedelta(hours=55)).strftime("%Y-%m-%dT%H:%M:%SZ")
            ce = (current_chunk_trades[-1]["_et"] + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")

            logger.info(f"    Fetching chunk {chunk_key}: {cs[:10]} to {ce[:10]}")
            df = fetch_pair_candles(pair, cs, ce)
            if not df.empty:
                chunks[chunk_key] = df
            time.sleep(FETCH_DELAY)

            current_chunk_trades = []
            chunk_start = trade["_et"]

        current_chunk_trades.append(trade)

    # Last chunk
    if current_chunk_trades:
        chunk_key = chunk_start.strftime("%Y%m%d")
        cs = (chunk_start - timedelta(hours=55)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ce = (current_chunk_trades[-1]["_et"] + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(f"    Fetching chunk {chunk_key}: {cs[:10]} to {ce[:10]}")
        df = fetch_pair_candles(pair, cs, ce)
        if not df.empty:
            chunks[chunk_key] = df
        time.sleep(FETCH_DELAY)

    return chunks


def find_best_chunk(chunks: Dict[str, pd.DataFrame], entry_time: str) -> Optional[pd.DataFrame]:
    """Find the chunk that contains the given entry_time."""
    et = pd.to_datetime(entry_time, utc=True)
    for key, df in chunks.items():
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        if df["time"].iloc[0] <= et <= df["time"].iloc[-1]:
            return df
        # Also check if within reasonable range (EMA warmup may shift things)
        if df["time"].iloc[0] - timedelta(hours=2) <= et <= df["time"].iloc[-1] + timedelta(hours=2):
            return df
    return None


# ══════════════════════════════════════════════════════════════════
# CASCADE / OVERLAY ANALYSIS
# ══════════════════════════════════════════════════════════════════

def find_candle_idx(candles_df: pd.DataFrame, entry_time: str) -> Optional[int]:
    """Find the candle index closest to entry_time."""
    et = pd.to_datetime(entry_time, utc=True)
    # Ensure candles time is also tz-aware
    if candles_df["time"].dt.tz is None:
        candles_df["time"] = candles_df["time"].dt.tz_localize("UTC")
    # Find closest candle within 15 min
    diffs = (candles_df["time"] - et).abs()
    min_idx = diffs.idxmin()
    if diffs.loc[min_idx] > timedelta(minutes=20):
        return None
    return int(min_idx)


def compute_cascade(candles_df: pd.DataFrame, entry_idx: int, direction: str,
                    pair: str, entry_price: float) -> Dict:
    """Compute cascade analysis for a trade."""
    pv = pip_value(pair)
    end = min(entry_idx + LOOKFORWARD + 1, len(candles_df))

    if entry_idx >= len(candles_df) - 5:
        return _empty_cascade()

    # Consecutive candles in trade direction
    cascade_len = 0
    for j in range(entry_idx + 1, end):
        c = candles_df.iloc[j]
        if direction == "buy" and c["close"] >= c["open"]:
            cascade_len += 1
        elif direction == "sell" and c["close"] <= c["open"]:
            cascade_len += 1
        else:
            break

    # MFE / MAE (use DB values if available, but also compute from candles)
    mfe = 0.0
    mae = 0.0
    mfe_candle = 0
    for j in range(entry_idx + 1, end):
        row = candles_df.iloc[j]
        if direction == "buy":
            fav = (row["high"] - entry_price) / pv
            adv = (entry_price - row["low"]) / pv
        else:
            fav = (entry_price - row["low"]) / pv
            adv = (row["high"] - entry_price) / pv
        if fav > mfe:
            mfe = fav
            mfe_candle = j - entry_idx
        mae = max(mae, adv)

    # EMA separation at entry
    entry_row = candles_df.iloc[entry_idx]
    entry_sep = _safe_float(entry_row.get("ema_sep_pct", 0))
    entry_bb = _safe_float(entry_row.get("bb_width", 0))

    # Track EMA separation evolution
    peak_sep = entry_sep
    peak_sep_candle = 0
    peak_sep_price = entry_price

    # EMA convergence timing (velocity goes negative)
    ema_convergence_candle = None

    # BB convergence timing (width starts declining after peak)
    bb_peak = entry_bb
    bb_convergence_candle = None

    sep_values = []
    bb_values = []

    for j in range(entry_idx + 1, end):
        row = candles_df.iloc[j]
        offset = j - entry_idx

        sep = _safe_float(row.get("ema_sep_pct", 0))
        vel = _safe_float(row.get("ema_sep_velocity", 0))
        bw = _safe_float(row.get("bb_width", 0))

        sep_values.append(sep)
        bb_values.append(bw)

        if sep > peak_sep:
            peak_sep = sep
            peak_sep_candle = offset
            peak_sep_price = row["close"]

        if ema_convergence_candle is None and vel < 0 and offset > 2:
            # Check 3 consecutive negative velocities
            if len(sep_values) >= 3 and all(
                _safe_float(candles_df.iloc[j - k].get("ema_sep_velocity", 0)) < 0
                for k in range(3)
            ):
                ema_convergence_candle = offset

        if bw > bb_peak:
            bb_peak = bw
        elif bb_convergence_candle is None and bw < bb_peak * 0.97 and offset > 3:
            bb_convergence_candle = offset

    # Optimal exit revenue
    if direction == "buy":
        optimal_pips = (peak_sep_price - entry_price) / pv
    else:
        optimal_pips = (entry_price - peak_sep_price) / pv

    # Fan state at entry
    fan_state = "mixed"
    if _safe_bool(entry_row.get("fan_bullish", False)):
        fan_state = "bullish"
    elif _safe_bool(entry_row.get("fan_bearish", False)):
        fan_state = "bearish"

    # EMA direction vs trade direction
    ema_21 = _safe_float(entry_row.get("ema_21", 0))
    ema_55 = _safe_float(entry_row.get("ema_55", 0))
    ema_bias = "bullish" if ema_21 > ema_55 else "bearish"
    trade_aligns_with_ema = (
        (direction == "buy" and ema_bias == "bullish") or
        (direction == "sell" and ema_bias == "bearish")
    )

    # Velocity at entry
    entry_vel = _safe_float(entry_row.get("ema_sep_velocity", 0))

    # Fan decelerating at entry?
    fan_decelerating = entry_vel < 0

    return {
        "cascade_length": cascade_len,
        "mfe_pips": round(float(mfe), 1),
        "mae_pips": round(float(mae), 1),
        "mfe_candle": mfe_candle,
        "ema_convergence_candle": ema_convergence_candle,
        "bb_convergence_candle": bb_convergence_candle,
        "optimal_exit_candle": peak_sep_candle,
        "optimal_exit_pips": round(float(optimal_pips), 1),
        "peak_ema_sep": round(float(peak_sep), 4),
        "entry_ema_sep": round(float(entry_sep), 4),
        "entry_bb_width": round(float(entry_bb), 6),
        "entry_fan_state": fan_state,
        "entry_ema_bias": ema_bias,
        "entry_ema_velocity": round(float(entry_vel), 6),
        "trade_aligns_with_ema": trade_aligns_with_ema,
        "fan_decelerating_at_entry": fan_decelerating,
    }


def build_overlay(candles_df: pd.DataFrame, entry_idx: int, direction: str,
                  pair: str, entry_price: float) -> List[Dict]:
    """Build per-candle overlay data for visualization."""
    pv = pip_value(pair)
    end = min(entry_idx + LOOKFORWARD + 1, len(candles_df))
    overlay = []

    for j in range(entry_idx, end):
        r = candles_df.iloc[j]
        if direction == "buy":
            unrealized = (r["close"] - entry_price) / pv
        else:
            unrealized = (entry_price - r["close"]) / pv

        overlay.append({
            "offset": j - entry_idx,
            "time": str(r["time"]),
            "close": round(float(r["close"]), 6),
            "ema_21": round(_safe_float(r.get("ema_21", 0)), 6),
            "ema_55": round(_safe_float(r.get("ema_55", 0)), 6),
            "ema_100": round(_safe_float(r.get("ema_100", 0)), 6),
            "ema_sep_pct": round(_safe_float(r.get("ema_sep_pct", 0)), 4),
            "ema_sep_velocity": round(_safe_float(r.get("ema_sep_velocity", 0)), 6),
            "bb_width": round(_safe_float(r.get("bb_width", 0)), 6),
            "bb_upper": round(_safe_float(r.get("bb_upper", 0)), 6),
            "bb_lower": round(_safe_float(r.get("bb_lower", 0)), 6),
            "unrealized_pips": round(float(unrealized), 1),
        })

    return overlay


def _empty_cascade() -> Dict:
    return {
        "cascade_length": 0, "mfe_pips": 0, "mae_pips": 0, "mfe_candle": 0,
        "ema_convergence_candle": None, "bb_convergence_candle": None,
        "optimal_exit_candle": 0, "optimal_exit_pips": 0, "peak_ema_sep": 0,
        "entry_ema_sep": 0, "entry_bb_width": 0, "entry_fan_state": "unknown",
        "entry_ema_bias": "unknown", "entry_ema_velocity": 0,
        "trade_aligns_with_ema": False, "fan_decelerating_at_entry": False,
    }


def _safe_float(v) -> float:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 0.0
    return float(v)


def _safe_bool(v) -> bool:
    if v is None:
        return False
    return bool(v)


# ══════════════════════════════════════════════════════════════════
# THESIS-ONLY STRATEGY
# ══════════════════════════════════════════════════════════════════

def run_thesis_only(candles_df: pd.DataFrame, pair: str) -> List[Dict]:
    """
    Thesis-only: EMA cross + fan formation + BB expansion → entry.
    Exit when EMA separation peaks (velocity negative 3 bars) or BB contracts.
    """
    pv = pip_value(pair)
    trades = []
    in_trade = False
    entry_idx = entry_price = 0
    direction = ""
    cooldown = 0

    for i in range(150, len(candles_df) - 5):
        if cooldown > 0:
            cooldown -= 1
            continue

        row = candles_df.iloc[i]
        e21 = _safe_float(row.get("ema_21"))
        e55 = _safe_float(row.get("ema_55"))
        e100 = _safe_float(row.get("ema_100"))

        if e21 == 0 or e55 == 0 or e100 == 0:
            continue

        sep = _safe_float(row.get("ema_sep_pct", 0))
        sep_vel = _safe_float(row.get("ema_sep_velocity", 0))
        bb_w = _safe_float(row.get("bb_width", 0))
        bb_roc = _safe_float(row.get("bb_width_roc", 0))

        if in_trade:
            # Exit: 3 bars negative velocity, or BB contracting after 8+ bars, or max 40 bars
            bars_in = i - entry_idx
            vels = [_safe_float(candles_df.iloc[i - k].get("ema_sep_velocity", 0)) for k in range(min(3, bars_in))]
            sep_declining = len(vels) >= 3 and all(v < 0 for v in vels)
            bb_contracting = bb_roc < -0.0001 and bars_in > 8

            if sep_declining or bb_contracting or bars_in >= 40:
                exit_price = row["close"]
                pips = (exit_price - entry_price) / pv if direction == "buy" else (entry_price - exit_price) / pv
                trades.append({
                    "pair": pair,
                    "direction": direction,
                    "entry_time": str(candles_df.iloc[entry_idx]["time"]),
                    "exit_time": str(row["time"]),
                    "entry_price": float(entry_price),
                    "exit_price": float(exit_price),
                    "pips": round(float(pips), 1),
                    "bars_held": bars_in,
                    "win": pips > 0,
                    "exit_reason": "sep_decline" if sep_declining else ("bb_contract" if bb_contracting else "max_bars"),
                })
                in_trade = False
                cooldown = 5
            continue

        # Entry conditions
        cross_up = _safe_bool(row.get("ema_cross_up", False))
        cross_down = _safe_bool(row.get("ema_cross_down", False))
        fan_bull = _safe_bool(row.get("fan_bullish", False))
        fan_bear = _safe_bool(row.get("fan_bearish", False))
        bb_expanding = bb_roc > 0.0001

        if cross_up and bb_expanding:
            direction = "buy"
        elif cross_down and bb_expanding:
            direction = "sell"
        elif fan_bull and sep > THESIS_EMA_SEP_ENTRY and sep_vel > 0 and bb_expanding:
            direction = "buy"
        elif fan_bear and sep > THESIS_EMA_SEP_ENTRY and sep_vel > 0 and bb_expanding:
            direction = "sell"
        else:
            continue

        entry_price = row["close"]
        entry_idx = i
        in_trade = True

    return trades


# ══════════════════════════════════════════════════════════════════
# COMBINED ANALYSIS (sniper entry + thesis confirmation check)
# ══════════════════════════════════════════════════════════════════

def check_thesis_confirmation(candles_df: pd.DataFrame, entry_idx: int,
                               direction: str, window: int = 8) -> Dict:
    """
    For a sniper entry, check if thesis would have confirmed it.
    Returns confirmation info.
    """
    if entry_idx >= len(candles_df) - window:
        return {"confirmed": False, "reason": "insufficient_data"}

    entry_row = candles_df.iloc[entry_idx]
    fan_bull = _safe_bool(entry_row.get("fan_bullish", False))
    fan_bear = _safe_bool(entry_row.get("fan_bearish", False))
    sep_vel = _safe_float(entry_row.get("ema_sep_velocity", 0))
    bb_w = _safe_float(entry_row.get("bb_width", 0))

    # Check at entry and within window
    for j in range(entry_idx, min(entry_idx + window + 1, len(candles_df))):
        row = candles_df.iloc[j]
        fb = _safe_bool(row.get("fan_bullish", False))
        fbe = _safe_bool(row.get("fan_bearish", False))
        sv = _safe_float(row.get("ema_sep_velocity", 0))
        bw = _safe_float(row.get("bb_width", 0))
        not_squeeze = bw > THESIS_BB_SQUEEZE_WIDTH

        if direction == "buy":
            # Counter-trend buy: bear fan decelerating, or neutral, or bull fan
            fan_exhausting = fbe and sv < 0
            fan_neutral = not fb and not fbe
            fan_supports = fb
            if (fan_exhausting or fan_neutral or fan_supports) and not_squeeze:
                return {
                    "confirmed": True,
                    "confirm_candle": j - entry_idx,
                    "reason": "fan_exhausting" if fan_exhausting else ("fan_neutral" if fan_neutral else "fan_supports"),
                }
        else:
            fan_exhausting = fb and sv < 0
            fan_neutral = not fb and not fbe
            fan_supports = fbe
            if (fan_exhausting or fan_neutral or fan_supports) and not_squeeze:
                return {
                    "confirmed": True,
                    "confirm_candle": j - entry_idx,
                    "reason": "fan_exhausting" if fan_exhausting else ("fan_neutral" if fan_neutral else "fan_supports"),
                }

    return {"confirmed": False, "reason": "no_thesis_confirmation"}


# ══════════════════════════════════════════════════════════════════
# STATS
# ══════════════════════════════════════════════════════════════════

def compute_stats(trades: List[Dict], cascades: List[Dict] = None) -> Dict:
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0, "total_pips": 0,
                "avg_pips": 0, "profit_factor": 0, "cascade_stats": {}}

    wins = [t for t in trades if t.get("win") or t.get("result") == "win"]
    losses = [t for t in trades if not (t.get("win") or t.get("result") == "win")]

    pips_list = [t["pips"] for t in trades]
    total_pips = sum(pips_list)
    gross_profit = sum(p for p in pips_list if p > 0) or 0.001
    gross_loss = abs(sum(p for p in pips_list if p <= 0)) or 0.001

    stats = {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "total_pips": round(total_pips, 1),
        "avg_pips": round(total_pips / len(trades), 1),
        "profit_factor": round(gross_profit / gross_loss, 2),
    }

    if cascades:
        valid = [c for c in cascades if c.get("mfe_pips", 0) > 0]
        if valid:
            stats["cascade_stats"] = {
                "avg_cascade_length": round(np.mean([c["cascade_length"] for c in valid]), 1),
                "avg_mfe_pips": round(np.mean([c["mfe_pips"] for c in valid]), 1),
                "avg_mae_pips": round(np.mean([c["mae_pips"] for c in valid]), 1),
                "avg_mfe_candle": round(np.mean([c["mfe_candle"] for c in valid]), 1),
                "avg_optimal_exit_candle": round(np.mean([c["optimal_exit_candle"] for c in valid]), 1),
                "avg_optimal_exit_pips": round(np.mean([c["optimal_exit_pips"] for c in valid]), 1),
                "avg_peak_ema_sep": round(np.mean([c["peak_ema_sep"] for c in valid]), 4),
                "avg_entry_ema_sep": round(np.mean([c["entry_ema_sep"] for c in valid]), 4),
                "avg_ema_convergence_candle": round(np.mean([
                    c["ema_convergence_candle"] for c in valid
                    if c["ema_convergence_candle"] is not None
                ]) if any(c["ema_convergence_candle"] is not None for c in valid) else 0, 1),
                "avg_bb_convergence_candle": round(np.mean([
                    c["bb_convergence_candle"] for c in valid
                    if c["bb_convergence_candle"] is not None
                ]) if any(c["bb_convergence_candle"] is not None for c in valid) else 0, 1),
                "pct_aligns_with_ema": round(
                    sum(1 for c in valid if c["trade_aligns_with_ema"]) / len(valid) * 100, 1),
                "pct_fan_decelerating": round(
                    sum(1 for c in valid if c["fan_decelerating_at_entry"]) / len(valid) * 100, 1),
            }

            # Win rate when aligned vs counter-trend
            aligned = [c for i, c in enumerate(valid)]  # need to cross-ref with trades
        else:
            stats["cascade_stats"] = {}
    else:
        stats["cascade_stats"] = {}

    return stats


def compute_alignment_breakdown(trades: List[Dict], cascades: List[Dict]) -> Dict:
    """Compute win rates split by EMA alignment."""
    if not trades or not cascades or len(trades) != len(cascades):
        return {}

    aligned_wins = aligned_total = 0
    counter_wins = counter_total = 0
    decel_wins = decel_total = 0
    ndecel_wins = ndecel_total = 0

    for t, c in zip(trades, cascades):
        is_win = t.get("result") == "win" or t.get("win", False)
        if c.get("trade_aligns_with_ema"):
            aligned_total += 1
            if is_win:
                aligned_wins += 1
        else:
            counter_total += 1
            if is_win:
                counter_wins += 1

        if c.get("fan_decelerating_at_entry"):
            decel_total += 1
            if is_win:
                decel_wins += 1
        else:
            ndecel_total += 1
            if is_win:
                ndecel_wins += 1

    fan_states = defaultdict(lambda: {"wins": 0, "total": 0})
    for t, c in zip(trades, cascades):
        fs = c.get("entry_fan_state", "unknown")
        fan_states[fs]["total"] += 1
        if t.get("result") == "win" or t.get("win", False):
            fan_states[fs]["wins"] += 1

    return {
        "with_trend": {
            "trades": aligned_total,
            "wins": aligned_wins,
            "win_rate": round(aligned_wins / aligned_total * 100, 1) if aligned_total else 0,
        },
        "counter_trend": {
            "trades": counter_total,
            "wins": counter_wins,
            "win_rate": round(counter_wins / counter_total * 100, 1) if counter_total else 0,
        },
        "fan_decelerating": {
            "trades": decel_total,
            "wins": decel_wins,
            "win_rate": round(decel_wins / decel_total * 100, 1) if decel_total else 0,
        },
        "fan_not_decelerating": {
            "trades": ndecel_total,
            "wins": ndecel_wins,
            "win_rate": round(ndecel_wins / ndecel_total * 100, 1) if ndecel_total else 0,
        },
        "by_fan_state": {
            k: {"trades": v["total"], "wins": v["wins"],
                "win_rate": round(v["wins"] / v["total"] * 100, 1) if v["total"] else 0}
            for k, v in fan_states.items()
        },
    }


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    logger.info("=" * 70)
    logger.info("STRATEGY COMPARISON — DB Trades + EMA/BB Cascade Overlay")
    logger.info(f"Sniper setup: {SNIPER_SETUP} on {TIMEFRAME}")
    logger.info("=" * 70)

    all_cascades = []
    all_trades = []
    all_thesis_trades = []
    all_confirmed_trades = []
    all_confirmed_cascades = []
    all_rejected_trades = []
    pair_results = {}
    overlay_samples = []

    for pair in PAIRS:
        logger.info(f"\n{'='*50}")
        logger.info(f"Processing {pair}...")

        # Load sniper trades from DB
        trades_df = load_sniper_trades(pair)
        if trades_df.empty:
            logger.warning(f"  No trades for {pair}, skipping")
            continue

        # Sample trades if too many
        if len(trades_df) > MAX_TRADES_PER_PAIR:
            step = len(trades_df) // MAX_TRADES_PER_PAIR
            trades_sample = trades_df.iloc[::step].head(MAX_TRADES_PER_PAIR).copy()
            logger.info(f"  Sampled {len(trades_sample)} of {len(trades_df)} trades")
        else:
            trades_sample = trades_df.copy()

        # Fetch candle data in chunks
        logger.info(f"  Fetching candle chunks for {pair}...")
        chunks = fetch_candles_chunked(pair, trades_sample)
        if not chunks:
            logger.warning(f"  No candle data for {pair}, skipping")
            continue

        logger.info(f"  {pair}: {len(trades_sample)} trades, {len(chunks)} candle chunks")

        # Process each sniper trade
        pair_cascades = []
        pair_trades = []
        pair_confirmed = []
        pair_confirmed_cascades = []
        pair_rejected = []
        overlay_count = 0
        skipped = 0

        for _, trade in trades_sample.iterrows():
            chunk = find_best_chunk(chunks, trade["entry_time"])
            if chunk is None:
                skipped += 1
                continue

            idx = find_candle_idx(chunk, trade["entry_time"])
            if idx is None:
                skipped += 1
                continue

            # Compute cascade
            cascade = compute_cascade(
                chunk, idx, trade["direction"], pair, trade["entry_price"]
            )
            pair_cascades.append(cascade)

            trade_dict = trade.to_dict()
            trade_dict.pop("_et", None)
            trade_dict["pips"] = float(trade_dict.get("pips", 0))
            pair_trades.append(trade_dict)

            # Check thesis confirmation
            thesis_check = check_thesis_confirmation(chunk, idx, trade["direction"])
            if thesis_check["confirmed"]:
                pair_confirmed.append(trade_dict)
                pair_confirmed_cascades.append(cascade)
            else:
                pair_rejected.append(trade_dict)

            # Build overlay samples
            if overlay_count < OVERLAY_SAMPLE_PER_PAIR and len(overlay_samples) < 200:
                overlay_data = build_overlay(chunk, idx, trade["direction"],
                                            pair, trade["entry_price"])
                overlay_samples.append({
                    "pair": pair,
                    "direction": trade["direction"],
                    "entry_time": str(trade["entry_time"]),
                    "result": trade["result"],
                    "pips": float(trade["pips"]),
                    "cascade": cascade,
                    "candles": overlay_data,
                })
                overlay_count += 1

        if skipped:
            logger.info(f"  Skipped {skipped} trades (no matching candle data)")

        all_cascades.extend(pair_cascades)
        all_trades.extend(pair_trades)
        all_confirmed_trades.extend(pair_confirmed)
        all_confirmed_cascades.extend(pair_confirmed_cascades)
        all_rejected_trades.extend(pair_rejected)

        # Run thesis-only on largest chunk for this pair
        largest_chunk = max(chunks.values(), key=len)
        thesis_trades = run_thesis_only(largest_chunk, pair)
        all_thesis_trades.extend(thesis_trades)

        # Pair-level stats
        alignment = compute_alignment_breakdown(pair_trades, pair_cascades)
        pair_results[pair] = {
            "sniper": compute_stats(pair_trades, pair_cascades),
            "thesis": compute_stats(thesis_trades),
            "confirmed": compute_stats(pair_confirmed, pair_confirmed_cascades),
            "rejected_by_thesis": compute_stats(pair_rejected),
            "alignment": alignment,
            "trade_count": len(pair_trades),
            "confirmed_count": len(pair_confirmed),
            "rejected_count": len(pair_rejected),
        }

        logger.info(f"  {pair} done: {len(pair_trades)} trades analyzed, "
                     f"{len(pair_confirmed)} thesis-confirmed, {len(thesis_trades)} thesis-only")

    # Overall stats
    overall_alignment = compute_alignment_breakdown(all_trades, all_cascades)

    results = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "config": {
            "sniper_setup": SNIPER_SETUP,
            "timeframe": TIMEFRAME,
            "pairs": PAIRS,
            "lookforward": LOOKFORWARD,
        },
        "overall": {
            "sniper_all": compute_stats(all_trades, all_cascades),
            "thesis_only": compute_stats(all_thesis_trades),
            "sniper_thesis_confirmed": compute_stats(all_confirmed_trades, all_confirmed_cascades),
            "sniper_thesis_rejected": compute_stats(all_rejected_trades),
        },
        "alignment_analysis": overall_alignment,
        "per_pair": pair_results,
        "overlay_samples": overlay_samples[:100],
    }

    # Save
    out_path = Path(__file__).resolve().parent / "strategy_comparison_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"\nResults saved to {out_path}")

    # ── Print Summary ────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("STRATEGY COMPARISON RESULTS")
    print(f"Sniper: {SNIPER_SETUP} | Timeframe: {TIMEFRAME}")
    print("=" * 70)

    for label, key in [
        ("SNIPER (ALL TRADES)", "sniper_all"),
        ("THESIS ONLY", "thesis_only"),
        ("SNIPER + THESIS CONFIRMED", "sniper_thesis_confirmed"),
        ("SNIPER — THESIS REJECTED", "sniper_thesis_rejected"),
    ]:
        s = results["overall"][key]
        cs = s.get("cascade_stats", {})
        print(f"\n{'─'*55}")
        print(f"  {label}")
        print(f"{'─'*55}")
        print(f"  Trades: {s['trades']}  |  Wins: {s['wins']}  |  Win Rate: {s['win_rate']}%")
        print(f"  Total Pips: {s['total_pips']}  |  Avg Pips: {s['avg_pips']}  |  PF: {s['profit_factor']}")
        if cs:
            print(f"  ── Cascade ──")
            print(f"  Avg cascade length: {cs.get('avg_cascade_length', 0)} candles")
            print(f"  MFE: {cs.get('avg_mfe_pips', 0)} pips @ candle {cs.get('avg_mfe_candle', 0)}")
            print(f"  MAE: {cs.get('avg_mae_pips', 0)} pips")
            print(f"  EMA peak sep: candle {cs.get('avg_optimal_exit_candle', 0)} = {cs.get('avg_optimal_exit_pips', 0)} pips")
            print(f"  EMA convergence: {cs.get('avg_ema_convergence_candle', 0)} bars | BB convergence: {cs.get('avg_bb_convergence_candle', 0)} bars")
            print(f"  Aligns with EMA: {cs.get('pct_aligns_with_ema', 0)}% | Fan decelerating: {cs.get('pct_fan_decelerating', 0)}%")

    # Alignment breakdown
    al = results.get("alignment_analysis", {})
    if al:
        print(f"\n{'='*55}")
        print("EMA ALIGNMENT ANALYSIS")
        print(f"{'='*55}")
        wt = al.get("with_trend", {})
        ct = al.get("counter_trend", {})
        fd = al.get("fan_decelerating", {})
        nd = al.get("fan_not_decelerating", {})
        print(f"  With-trend:     {wt.get('trades',0)} trades, {wt.get('win_rate',0)}% WR")
        print(f"  Counter-trend:  {ct.get('trades',0)} trades, {ct.get('win_rate',0)}% WR")
        print(f"  Fan decel:      {fd.get('trades',0)} trades, {fd.get('win_rate',0)}% WR")
        print(f"  Fan not decel:  {nd.get('trades',0)} trades, {nd.get('win_rate',0)}% WR")
        fs = al.get("by_fan_state", {})
        if fs:
            print(f"\n  By fan state at entry:")
            for state, data in sorted(fs.items()):
                print(f"    {state:12s}: {data['trades']:5d} trades, {data['win_rate']}% WR")

    # Per-pair table
    print(f"\n{'='*70}")
    print("PER-PAIR BREAKDOWN")
    print(f"{'='*70}")
    print(f"{'Pair':<10} {'Sniper WR':>10} {'Thesis WR':>10} {'Confirmed WR':>13} {'Rejected WR':>12} {'Optimal Exit':>13}")
    for pair, pr in pair_results.items():
        sw = pr["sniper"]["win_rate"]
        tw = pr["thesis"]["win_rate"]
        cw = pr["confirmed"]["win_rate"]
        rw = pr["rejected_by_thesis"]["win_rate"]
        oe = pr["sniper"].get("cascade_stats", {}).get("avg_optimal_exit_pips", 0)
        print(f"{pair:<10} {sw:>9.1f}% {tw:>9.1f}% {cw:>12.1f}% {rw:>11.1f}% {oe:>12.1f}p")

    print(f"\n{'='*70}")
    print("KEY QUESTIONS ANSWERED:")
    print("1. Does thesis confirmation IMPROVE sniper win rate? Compare Confirmed WR vs All WR")
    print("2. Are thesis-rejected trades actually BAD? Compare Rejected WR — if still high, thesis is wrong")
    print("3. How much money left on table? Compare optimal exit pips vs actual sniper TP")
    print("4. Counter-trend vs with-trend: which sniper trades work better?")
    print("5. Does fan deceleration predict better sniper entries?")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
