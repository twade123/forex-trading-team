#!/usr/bin/env python3
"""MASTER SWEEP — Test every combination, track buy/sell separately.

Designed to run unattended in a terminal. Memory-safe (one pair/tf at a time).
Outputs progress to terminal + final results to JSON + CSV.

Usage:
    cd ~/jarvis/Trading\ Bot
    source ~/myenv/bin/activate
    python -m Source.backtester.master_sweep
    
    # Or test a single pair quickly:
    python -m Source.backtester.master_sweep --pair EUR_USD --tf H1
    
    # Skip data fetch (use cached only):
    python -m Source.backtester.master_sweep --no-fetch
"""

import argparse
import csv
import gc
import json
import logging
import os
import sys
import time

# Force unbuffered output so terminal shows progress in real time
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
os.environ['PYTHONUNBUFFERED'] = '1'
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

TRADING_BOT = Path(__file__).resolve().parent.parent.parent
JARVIS_ROOT = TRADING_BOT.parent
sys.path.insert(0, str(JARVIS_ROOT))
sys.path.insert(0, str(TRADING_BOT))

from Source.backtester import indicators, divergence, rule_engine
from Source.backtester.candle_patterns import detect_all_patterns
from Source.backtester.sniper_v4 import add_enhanced_indicators, score_v4, TF_PARAMS

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
logger = logging.getLogger("master_sweep")
logger.setLevel(logging.INFO)

# ============================================================================
# CONFIGURATION
# ============================================================================

ALL_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF",
    "AUD_USD", "NZD_USD", "USD_CAD",
    "EUR_GBP", "EUR_JPY", "GBP_JPY",
    "EUR_AUD", "EUR_CHF", "AUD_JPY",
]

# Order: fastest first (fewer candles = faster), M5 last (huge datasets)
ALL_TIMEFRAMES = ["H4", "H1", "M15", "M5"]

JPY_PAIRS = {"USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"}

DATA_DIR = TRADING_BOT / "Data"
RESULTS_DIR = TRADING_BOT / "Results"

# Parameter grid
PARAM_GRID = {
    # Strategy engine
    "engine": ["rules", "sniper"],
    # Confluence / score thresholds
    "threshold": [10, 14, 20, 30, 40],
    # Risk:Reward ratios
    "risk_reward": [1.5, 2.0, 2.5, 3.0],
    # Stop loss ATR multiplier
    "sl_atr_mult": [1.0, 1.5, 2.0, 2.5],
    # Require candle pattern confirmation for sells?
    "sell_candle_gate": [False, True],
    # TP as fraction of ATR (sniper only, overrides RR)
    "tp_atr_frac": [None],  # None = use risk_reward
}

# Sniper-specific thresholds (scores are different scale)
SNIPER_THRESHOLDS = [8, 10, 12, 14, 16]
RULES_THRESHOLDS = [20, 30, 40, 50]

# For sniper, TP in ATR fractions
SNIPER_TP_ATR = [0.3, 0.5, 0.8, None]  # None = use risk_reward


# ============================================================================
# DATA PREPARATION
# ============================================================================

def load_and_prepare(csv_path: str, timeframe: str) -> pd.DataFrame:
    """Load CSV, compute ALL indicators + candle patterns. Returns prepared df."""
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Core indicators
    df = indicators.compute_all(df)

    # Divergence
    df = divergence.add_divergence_signals(df)

    # Candlestick patterns
    df = detect_all_patterns(df)

    # Enhanced indicators for sniper
    df = add_enhanced_indicators(df)

    # Derived columns for rule engine
    df["prev_macd_histogram"] = df["macd_histogram"].shift(1)
    df["prev_adx"] = df["adx"].shift(1)
    df["prev_sma_50"] = df["sma_50"].shift(1)
    df["prev_sma_100"] = df["sma_100"].shift(1)
    df["prev_stoch_k"] = df["stoch_k"].shift(1)
    df["prev_stoch_d"] = df["stoch_d"].shift(1)
    df["avg_volume"] = df["volume"].rolling(20).mean()
    df["atr_avg"] = df["atr"].rolling(50).mean()
    df["prev_close"] = df["close"].shift(1)
    df["prev_high"] = df["high"].shift(1)
    df["prev_low"] = df["low"].shift(1)

    # MACD crossover recency
    mh = df["macd_histogram"]
    pmh = df["prev_macd_histogram"]
    macd_cross = ((mh > 0) & (pmh <= 0)) | ((mh < 0) & (pmh >= 0))
    bars_since = pd.Series(np.nan, index=df.index)
    last_cross = -999
    for i in range(len(df)):
        if macd_cross.iloc[i]:
            last_cross = i
        bars_since.iloc[i] = i - last_cross
    df["macd_cross_bars_ago"] = bars_since

    # Time features
    df["hour"] = df["timestamp"].dt.hour
    df["day_of_week"] = df["timestamp"].dt.dayofweek

    # Consecutive candles
    bull_run = (df["close"] > df["open"]).astype(int)
    bear_run = (df["close"] < df["open"]).astype(int)
    # Count consecutive
    consec_bull = pd.Series(0, index=df.index)
    consec_bear = pd.Series(0, index=df.index)
    for i in range(1, len(df)):
        if bull_run.iloc[i]:
            consec_bull.iloc[i] = consec_bull.iloc[i-1] + 1
        if bear_run.iloc[i]:
            consec_bear.iloc[i] = consec_bear.iloc[i-1] + 1
    df["consec_bull"] = consec_bull
    df["consec_bear"] = consec_bear

    # RSI slope (for sniper)
    df["rsi_slope"] = df["rsi"].diff(3)

    # BB penetration (for sniper)
    df["bb_lower_pen"] = np.where(
        df["close"] < df["bb_lower"],
        (df["bb_lower"] - df["close"]) / df["atr"].replace(0, np.nan),
        0
    )
    df["bb_upper_pen"] = np.where(
        df["close"] > df["bb_upper"],
        (df["close"] - df["bb_upper"]) / df["atr"].replace(0, np.nan),
        0
    )

    # Swing high/low proximity (simple version)
    lookback = 50
    df["swing_high"] = df["high"].rolling(lookback).max()
    df["swing_low"] = df["low"].rolling(lookback).min()
    atr_vals = df["atr"].replace(0, np.nan)
    df["near_swing_high"] = (df["swing_high"] - df["close"]) / atr_vals < 0.5
    df["near_swing_low"] = (df["close"] - df["swing_low"]) / atr_vals < 0.5

    return df


def row_to_dict(row) -> dict:
    """Convert DataFrame row to dict, handling numpy types."""
    d = {}
    for col in row.index:
        val = row[col]
        if isinstance(val, (np.bool_, bool)):
            d[col] = bool(val)
        elif isinstance(val, (np.integer,)):
            d[col] = int(val)
        elif isinstance(val, (np.floating,)):
            d[col] = float(val) if not np.isnan(val) else 0.0
        else:
            d[col] = val
    return d


# ============================================================================
# BACKTESTING ENGINE (unified, tracks buy/sell separately)
# ============================================================================

class Position:
    __slots__ = ['direction', 'entry_price', 'entry_time', 'stop_loss',
                 'take_profit', 'risk_pips', 'confluence_score', 'half_exited', 'pips']

    def __init__(self, direction, entry_price, entry_time, stop_loss, take_profit, risk_pips, score):
        self.direction = direction
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.risk_pips = risk_pips
        self.confluence_score = score
        self.half_exited = False
        self.pips = 0.0


def precompute_signals(df: pd.DataFrame, rules: dict, timeframe: str) -> dict:
    """Pre-compute ALL signals for every candle once. Returns numpy arrays."""
    n = len(df)

    # Extract core arrays
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    atr = df["atr"].values.copy()
    atr[atr == 0] = 0.001
    atr[np.isnan(atr)] = 0.001
    sma50 = df["sma_50"].values

    # Pre-compute rule engine signals for every row
    rule_directions = np.zeros(n, dtype=np.int8)  # 1=buy, -1=sell, 0=none
    rule_scores = np.zeros(n, dtype=np.int16)
    rule_skipped = np.zeros(n, dtype=bool)

    # Pre-compute sniper signals
    tf_key = timeframe if timeframe in TF_PARAMS else "H1"
    sniper_params = TF_PARAMS[tf_key]
    sniper_buy_scores = np.zeros(n, dtype=np.int16)
    sniper_sell_scores = np.zeros(n, dtype=np.int16)

    # Pre-compute sell candle gate
    bear_signal = df.get("candle_bear_signal", pd.Series(0, index=df.index)).values
    has_bear_pattern = (
        (bear_signal >= 2) |
        df.get("bearish_engulfing", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("shooting_star", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("evening_star", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("dark_cloud", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("three_black_crows", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("gravestone_doji", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("tweezer_top", pd.Series(False, index=df.index)).values.astype(bool)
    )

    warmup = 200
    for i in range(warmup, n):
        row = row_to_dict(df.iloc[i])

        # Rules engine
        regime = rule_engine.detect_regime(row)
        skips = rule_engine.evaluate_skip_rules(row, rules)
        if skips:
            rule_skipped[i] = True
        else:
            fired = rule_engine.evaluate_entry_rules(row, rules, regime)
            if fired:
                conf = rule_engine.score_confluence(fired)
                rule_scores[i] = conf["score"]
                if conf["direction"] == "buy":
                    rule_directions[i] = 1
                elif conf["direction"] == "sell":
                    rule_directions[i] = -1

        # Sniper engine
        sb, ss = score_v4(row, sniper_params)
        sniper_buy_scores[i] = sb
        sniper_sell_scores[i] = ss

    return {
        "close": close, "high": high, "low": low, "atr": atr, "sma50": sma50,
        "rule_directions": rule_directions, "rule_scores": rule_scores,
        "rule_skipped": rule_skipped,
        "sniper_buy": sniper_buy_scores, "sniper_sell": sniper_sell_scores,
        "has_bear_pattern": has_bear_pattern,
        "n": n,
    }


def run_backtest_fast(signals: dict, params: dict, is_jpy: bool) -> dict:
    """Run backtest using pre-computed signals. MUCH faster for parameter sweeps."""

    engine = params["engine"]
    threshold = params["threshold"]
    rr = params["risk_reward"]
    sl_mult = params["sl_atr_mult"]
    sell_gate = params["sell_candle_gate"]
    tp_atr = params.get("tp_atr_frac")

    pip_mult = 100.0 if is_jpy else 10000.0
    warmup = 200
    max_positions = 2

    close = signals["close"]
    high = signals["high"]
    low = signals["low"]
    atr = signals["atr"]
    sma50 = signals["sma50"]
    n = signals["n"]

    positions = []
    trades = []

    threshold_pip = 0.100 if is_jpy else 0.0010

    for i in range(warmup, n):
        c = close[i]
        h = high[i]
        l = low[i]
        a = atr[i]

        # --- Check exits ---
        to_close = []
        for j, pos in enumerate(positions):
            # Stop loss
            if pos.direction == "buy" and l <= pos.stop_loss:
                pips = (pos.stop_loss - pos.entry_price) * pip_mult
                if pos.half_exited:
                    pips = pos.pips + pips * 0.5
                trades.append({"direction": "buy", "pips": pips, "exit_reason": "stop_loss"})
                to_close.append(j)
                continue
            if pos.direction == "sell" and h >= pos.stop_loss:
                pips = (pos.entry_price - pos.stop_loss) * pip_mult
                if pos.half_exited:
                    pips = pos.pips + pips * 0.5
                trades.append({"direction": "sell", "pips": pips, "exit_reason": "stop_loss"})
                to_close.append(j)
                continue
            # Take profit (half exit)
            if not pos.half_exited:
                if pos.direction == "buy" and h >= pos.take_profit:
                    half_pips = (pos.take_profit - pos.entry_price) * pip_mult * 0.5
                    pos.half_exited = True
                    pos.stop_loss = pos.entry_price
                    pos.pips += half_pips
                    continue
                if pos.direction == "sell" and l <= pos.take_profit:
                    half_pips = (pos.entry_price - pos.take_profit) * pip_mult * 0.5
                    pos.half_exited = True
                    pos.stop_loss = pos.entry_price
                    pos.pips += half_pips
                    continue
            # SMA trail for remaining half
            if pos.half_exited:
                s50 = sma50[i]
                if pos.direction == "buy" and c < s50 - threshold_pip:
                    pips = (c - pos.entry_price) * pip_mult * 0.5
                    trades.append({"direction": "buy", "pips": pos.pips + pips, "exit_reason": "sma_trail"})
                    to_close.append(j)
                    continue
                if pos.direction == "sell" and c > s50 + threshold_pip:
                    pips = (pos.entry_price - c) * pip_mult * 0.5
                    trades.append({"direction": "sell", "pips": pos.pips + pips, "exit_reason": "sma_trail"})
                    to_close.append(j)

        for j in sorted(to_close, reverse=True):
            positions.pop(j)

        if len(positions) >= max_positions:
            continue

        # --- Determine signal from pre-computed arrays ---
        direction = None

        if engine == "rules":
            if signals["rule_skipped"][i]:
                continue
            if signals["rule_scores"][i] < threshold:
                continue
            d = signals["rule_directions"][i]
            if d == 1:
                direction = "buy"
            elif d == -1:
                direction = "sell"
            else:
                continue

        elif engine == "sniper":
            sb = signals["sniper_buy"][i]
            ss = signals["sniper_sell"][i]
            if sb >= threshold and sb > ss + 2:
                direction = "buy"
            elif ss >= threshold and ss > sb + 2:
                direction = "sell"
            else:
                continue

        # Sell candle gate
        if sell_gate and direction == "sell":
            if not signals["has_bear_pattern"][i]:
                continue

        # --- Calculate SL/TP ---
        sl_distance = a * sl_mult
        if direction == "buy":
            stop_loss = c - sl_distance
            take_profit = c + (a * tp_atr if tp_atr is not None else sl_distance * rr)
        else:
            stop_loss = c + sl_distance
            take_profit = c - (a * tp_atr if tp_atr is not None else sl_distance * rr)

        pos = Position(direction, c, "", stop_loss, take_profit, sl_distance * pip_mult, 0)
        positions.append(pos)

    # Close remaining
    if positions:
        last_c = close[-1]
        for pos in positions:
            if pos.direction == "buy":
                pips = (last_c - pos.entry_price) * pip_mult
            else:
                pips = (pos.entry_price - last_c) * pip_mult
            if pos.half_exited:
                pips = pos.pips + pips * 0.5
            trades.append({"direction": pos.direction, "pips": pips, "exit_reason": "end_of_data"})

    return _compute_stats(trades, params)


def _check_exit(pos: Position, row: dict, pip_mult: float, is_jpy: bool) -> dict:
    """Check if position should exit."""
    close = row["close"]
    high = row["high"]
    low = row["low"]

    # Stop loss
    if pos.direction == "buy" and low <= pos.stop_loss:
        pips = (pos.stop_loss - pos.entry_price) * pip_mult
        if pos.half_exited:
            pips = pos.pips + pips * 0.5
        return {"pips": pips, "reason": "stop_loss"}

    if pos.direction == "sell" and high >= pos.stop_loss:
        pips = (pos.entry_price - pos.stop_loss) * pip_mult
        if pos.half_exited:
            pips = pos.pips + pips * 0.5
        return {"pips": pips, "reason": "stop_loss"}

    # Take profit (half exit)
    if not pos.half_exited:
        if pos.direction == "buy" and high >= pos.take_profit:
            half_pips = (pos.take_profit - pos.entry_price) * pip_mult * 0.5
            pos.half_exited = True
            pos.stop_loss = pos.entry_price  # Breakeven
            pos.pips += half_pips
            return None

        if pos.direction == "sell" and low <= pos.take_profit:
            half_pips = (pos.entry_price - pos.take_profit) * pip_mult * 0.5
            pos.half_exited = True
            pos.stop_loss = pos.entry_price
            pos.pips += half_pips
            return None

    # Trailing: SMA break for remaining half
    if pos.half_exited:
        sma50 = row.get("sma_50", 0)
        threshold = 0.100 if is_jpy else 0.0010
        if pos.direction == "buy" and close < sma50 - threshold:
            pips = (close - pos.entry_price) * pip_mult * 0.5
            return {"pips": pos.pips + pips, "reason": "sma_trail"}
        if pos.direction == "sell" and close > sma50 + threshold:
            pips = (pos.entry_price - close) * pip_mult * 0.5
            return {"pips": pos.pips + pips, "reason": "sma_trail"}

    return None


def _compute_stats(trades: list, params: dict) -> dict:
    """Compute stats with buy/sell breakdown."""
    if not trades:
        return {"total_trades": 0}

    buy_trades = [t for t in trades if t["direction"] == "buy"]
    sell_trades = [t for t in trades if t["direction"] == "sell"]

    def _stats(tlist, label):
        if not tlist:
            return {f"{label}_trades": 0, f"{label}_wins": 0, f"{label}_win_rate": 0,
                    f"{label}_pips": 0, f"{label}_pf": 0, f"{label}_avg_win": 0,
                    f"{label}_avg_loss": 0}
        wins = [t for t in tlist if t["pips"] > 0]
        losses = [t for t in tlist if t["pips"] <= 0]
        gross_profit = sum(t["pips"] for t in wins) if wins else 0
        gross_loss = abs(sum(t["pips"] for t in losses)) if losses else 0.01
        return {
            f"{label}_trades": len(tlist),
            f"{label}_wins": len(wins),
            f"{label}_win_rate": round(len(wins) / len(tlist) * 100, 1) if tlist else 0,
            f"{label}_pips": round(sum(t["pips"] for t in tlist), 1),
            f"{label}_pf": round(gross_profit / gross_loss, 2),
            f"{label}_avg_win": round(np.mean([t["pips"] for t in wins]), 1) if wins else 0,
            f"{label}_avg_loss": round(np.mean([t["pips"] for t in losses]), 1) if losses else 0,
        }

    all_wins = [t for t in trades if t["pips"] > 0]
    all_losses = [t for t in trades if t["pips"] <= 0]
    total_pips = sum(t["pips"] for t in trades)
    gross_profit = sum(t["pips"] for t in all_wins) if all_wins else 0
    gross_loss = abs(sum(t["pips"] for t in all_losses)) if all_losses else 0.01

    # Max drawdown
    running = 0
    peak = 0
    max_dd = 0
    for t in trades:
        running += t["pips"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        r = t.get("exit_reason", "unknown")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    result = {
        "total_trades": len(trades),
        "total_wins": len(all_wins),
        "win_rate": round(len(all_wins) / len(trades) * 100, 1),
        "total_pips": round(total_pips, 1),
        "profit_factor": round(gross_profit / gross_loss, 2),
        "max_drawdown": round(max_dd, 1),
        "avg_win": round(np.mean([t["pips"] for t in all_wins]), 1) if all_wins else 0,
        "avg_loss": round(np.mean([t["pips"] for t in all_losses]), 1) if all_losses else 0,
        "exit_reasons": exit_reasons,
        **_stats(buy_trades, "buy"),
        **_stats(sell_trades, "sell"),
        **params,
    }
    return result


# ============================================================================
# SWEEP ORCHESTRATION
# ============================================================================

def generate_configs() -> list:
    """Generate all parameter combinations."""
    configs = []

    # Rules engine configs
    for threshold, rr, sl, gate in product(
        RULES_THRESHOLDS, PARAM_GRID["risk_reward"],
        PARAM_GRID["sl_atr_mult"], PARAM_GRID["sell_candle_gate"]
    ):
        configs.append({
            "engine": "rules",
            "threshold": threshold,
            "risk_reward": rr,
            "sl_atr_mult": sl,
            "sell_candle_gate": gate,
            "tp_atr_frac": None,
        })

    # Sniper engine configs
    for threshold, rr, sl, gate in product(
        SNIPER_THRESHOLDS, PARAM_GRID["risk_reward"],
        PARAM_GRID["sl_atr_mult"], PARAM_GRID["sell_candle_gate"]
    ):
        configs.append({
            "engine": "sniper",
            "threshold": threshold,
            "risk_reward": rr,
            "sl_atr_mult": sl,
            "sell_candle_gate": gate,
            "tp_atr_frac": None,
        })

    # Sniper with TP as ATR fraction (skip None since covered above)
    for threshold, tp_atr, sl, gate in product(
        SNIPER_THRESHOLDS, [0.3, 0.5, 0.8],
        [1.5, 2.0, 2.5], PARAM_GRID["sell_candle_gate"]
    ):
        configs.append({
            "engine": "sniper",
            "threshold": threshold,
            "risk_reward": 0,  # Not used when tp_atr set
            "sl_atr_mult": sl,
            "sell_candle_gate": gate,
            "tp_atr_frac": tp_atr,
        })

    return configs


def get_data_path(pair: str, tf: str) -> Path:
    """Return CSV path for a pair/tf combo."""
    return DATA_DIR / f"{pair.lower()}_{tf.lower()}_3yr.csv"


def fetch_missing(pair: str, tf: str) -> bool:
    """Fetch data if missing. Returns True if available."""
    csv_path = get_data_path(pair, tf)
    if csv_path.exists():
        return True
    try:
        from Source.backtester.data_fetcher import fetch_and_save
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fetch_and_save(instrument=pair, granularity=tf,
                       from_time="2023-02-13T00:00:00Z", to_time=now, data_dir=DATA_DIR)
        return True
    except Exception as e:
        logger.warning("Could not fetch %s/%s: %s", pair, tf, e)
        return False


def main():
    parser = argparse.ArgumentParser(description="Master Backtesting Sweep")
    parser.add_argument("--pair", type=str, help="Single pair to test (e.g. EUR_USD)")
    parser.add_argument("--tf", type=str, help="Single timeframe (e.g. H1)")
    parser.add_argument("--no-fetch", action="store_true", help="Skip fetching missing data")
    parser.add_argument("--quick", action="store_true", help="Reduced param grid for fast test")
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else ALL_PAIRS
    timeframes = [args.tf] if args.tf else ALL_TIMEFRAMES

    configs = generate_configs()
    if args.quick:
        # Reduced grid: only key combos
        configs = [c for c in configs if c["threshold"] in [14, 30]
                   and c["risk_reward"] in [2.0, 0]
                   and c["sl_atr_mult"] in [1.5, 2.0]]

    rules = rule_engine.load_rules()

    # Count available data
    pair_tf_combos = []
    for pair in pairs:
        for tf in timeframes:
            csv_path = get_data_path(pair, tf)
            if csv_path.exists():
                pair_tf_combos.append((pair, tf))
            elif not args.no_fetch:
                logger.info("Fetching %s/%s...", pair, tf)
                if fetch_missing(pair, tf):
                    pair_tf_combos.append((pair, tf))

    total_runs = len(pair_tf_combos) * len(configs)
    logger.info("=" * 70)
    logger.info("MASTER SWEEP")
    logger.info("  Pairs: %d | Timeframes: %d | Pair/TF combos: %d",
                len(pairs), len(timeframes), len(pair_tf_combos))
    logger.info("  Parameter configs: %d", len(configs))
    logger.info("  Total backtests: %d", total_runs)
    logger.info("=" * 70)

    all_results = []
    completed = 0
    start_time = time.time()
    pair_tf_times = []

    for combo_idx, (pair, tf) in enumerate(pair_tf_combos):
        csv_path = get_data_path(pair, tf)
        is_jpy = pair in JPY_PAIRS

        logger.info("\n[%d/%d] Preparing %s/%s ...", combo_idx + 1, len(pair_tf_combos), pair, tf)
        t0 = time.time()

        try:
            df = load_and_prepare(str(csv_path), tf)
        except Exception as e:
            logger.error("  Failed to prepare %s/%s: %s", pair, tf, e)
            completed += len(configs)
            continue

        prep_time = time.time() - t0
        # Pre-compute signals once for this pair/tf
        logger.info("  %d candles prepared in %.1fs. Pre-computing signals...",
                     len(df), prep_time)
        t1 = time.time()
        try:
            signals = precompute_signals(df, rules, tf)
        except Exception as e:
            logger.error("  Signal precompute failed: %s", e)
            completed += len(configs)
            del df
            gc.collect()
            continue
        sig_time = time.time() - t1
        logger.info("  Signals pre-computed in %.1fs. Running %d configs...", sig_time, len(configs))

        combo_start = time.time()
        combo_results = []
        best_pf = 0
        best_config = None

        for cfg_idx, cfg in enumerate(configs):
            try:
                stats = run_backtest_fast(signals, cfg, is_jpy)
            except Exception as e:
                completed += 1
                continue

            completed += 1
            t = stats.get("total_trades", 0)

            if t > 0:
                stats["pair"] = pair
                stats["timeframe"] = tf
                stats["candles"] = len(df)
                stats["trades_per_day"] = round(t / (len(df) / ({"M5": 288, "M15": 96, "H1": 24, "H4": 6}.get(tf, 24) * 365)), 2)
                combo_results.append(stats)

                if t >= 10 and stats["profit_factor"] > best_pf:
                    best_pf = stats["profit_factor"]
                    best_config = stats

            # Progress every 100 configs
            if (cfg_idx + 1) % 100 == 0:
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (total_runs - completed) / rate if rate > 0 else 0
                print(f"\r  [{cfg_idx+1}/{len(configs)}] {completed}/{total_runs} total | "
                      f"{rate:.0f}/sec | ETA {eta/60:.0f}m | "
                      f"{len(combo_results)} with trades", end="", flush=True)

        combo_time = time.time() - combo_start
        pair_tf_times.append(combo_time)
        print()  # newline after progress

        if best_config:
            bc = best_config
            logger.info("  ✓ %s/%s done in %.0fs — %d configs with trades, best PF=%.2f "
                        "(e=%s t=%s rr=%.1f sl=%.1f gate=%s) %d trades, %.0f pips, "
                        "BUY: %.0f%% win/%d trades, SELL: %.0f%% win/%d trades",
                        pair, tf, combo_time, len(combo_results), bc["profit_factor"],
                        bc["engine"], bc["threshold"], bc["risk_reward"], bc["sl_atr_mult"],
                        bc["sell_candle_gate"], bc["total_trades"], bc["total_pips"],
                        bc["buy_win_rate"], bc["buy_trades"],
                        bc["sell_win_rate"], bc["sell_trades"])
        else:
            logger.info("  ✗ %s/%s done in %.0fs — no viable trades", pair, tf, combo_time)

        all_results.extend(combo_results)

        # Free memory
        del df, signals
        gc.collect()

    total_time = time.time() - start_time

    # ========================================================================
    # RESULTS OUTPUT
    # ========================================================================

    if not all_results:
        print("\n❌ No trades generated across any configuration!")
        return

    # Filter viable
    viable = [r for r in all_results
              if r["total_trades"] >= 15 and r["profit_factor"] > 1.0]
    viable.sort(key=lambda x: x["profit_factor"], reverse=True)

    # Also find best sell performers
    sell_viable = [r for r in all_results
                   if r["sell_trades"] >= 10 and r["sell_pf"] > 1.0]
    sell_viable.sort(key=lambda x: x["sell_pf"], reverse=True)

    # Print results
    print(f"\n{'='*140}")
    print(f"MASTER SWEEP COMPLETE — {total_time/60:.1f} minutes, {len(all_results)} configs with trades, {len(viable)} viable (PF>1, ≥15 trades)")
    print(f"{'='*140}")

    # Top 50 overall
    print(f"\n{'='*160}")
    print(f"TOP 50 OVERALL (PF>1.0, ≥15 trades)")
    print(f"{'='*160}")
    hdr = (f"{'PAIR':<10} {'TF':<4} {'ENG':<7} {'THRESH':>6} {'R:R':>5} {'SL':>4} {'GATE':>5} "
           f"{'TRADES':>7} {'WIN%':>6} {'PIPS':>9} {'PF':>6} {'DD':>7} "
           f"{'B_TRD':>6} {'B_W%':>5} {'B_PIP':>8} {'B_PF':>5} "
           f"{'S_TRD':>6} {'S_W%':>5} {'S_PIP':>8} {'S_PF':>5}")
    print(hdr)
    print("-" * 160)

    for r in viable[:50]:
        tp_str = f"{r['risk_reward']:.1f}" if r.get('tp_atr_frac') is None else f"A{r['tp_atr_frac']}"
        print(f"{r['pair']:<10} {r['timeframe']:<4} {r['engine']:<7} {r['threshold']:>6} {tp_str:>5} "
              f"{r['sl_atr_mult']:>4.1f} {'Y' if r['sell_candle_gate'] else 'N':>5} "
              f"{r['total_trades']:>7} {r['win_rate']:>5.1f}% {r['total_pips']:>8.0f} "
              f"{r['profit_factor']:>6.2f} {r['max_drawdown']:>6.0f} "
              f"{r['buy_trades']:>6} {r['buy_win_rate']:>4.1f}% {r['buy_pips']:>7.0f} {r['buy_pf']:>5.2f} "
              f"{r['sell_trades']:>6} {r['sell_win_rate']:>4.1f}% {r['sell_pips']:>7.0f} {r['sell_pf']:>5.2f}")

    # Best SELL configs
    print(f"\n{'='*160}")
    print(f"TOP 30 BEST SELL PERFORMANCE (≥10 sell trades, sell PF>1.0)")
    print(f"{'='*160}")
    print(hdr)
    print("-" * 160)

    for r in sell_viable[:30]:
        tp_str = f"{r['risk_reward']:.1f}" if r.get('tp_atr_frac') is None else f"A{r['tp_atr_frac']}"
        print(f"{r['pair']:<10} {r['timeframe']:<4} {r['engine']:<7} {r['threshold']:>6} {tp_str:>5} "
              f"{r['sl_atr_mult']:>4.1f} {'Y' if r['sell_candle_gate'] else 'N':>5} "
              f"{r['total_trades']:>7} {r['win_rate']:>5.1f}% {r['total_pips']:>8.0f} "
              f"{r['profit_factor']:>6.2f} {r['max_drawdown']:>6.0f} "
              f"{r['buy_trades']:>6} {r['buy_win_rate']:>4.1f}% {r['buy_pips']:>7.0f} {r['buy_pf']:>5.2f} "
              f"{r['sell_trades']:>6} {r['sell_win_rate']:>4.1f}% {r['sell_pips']:>7.0f} {r['sell_pf']:>5.2f}")

    # Sell candle gate comparison
    gated = [r for r in all_results if r["sell_candle_gate"] and r["sell_trades"] >= 5]
    ungated = [r for r in all_results if not r["sell_candle_gate"] and r["sell_trades"] >= 5]

    if gated and ungated:
        avg_gated_wr = np.mean([r["sell_win_rate"] for r in gated])
        avg_ungated_wr = np.mean([r["sell_win_rate"] for r in ungated])
        avg_gated_pf = np.mean([r["sell_pf"] for r in gated])
        avg_ungated_pf = np.mean([r["sell_pf"] for r in ungated])
        print(f"\n{'='*80}")
        print("SELL CANDLE GATE IMPACT:")
        print(f"  Without gate: avg sell win rate = {avg_ungated_wr:.1f}%, avg sell PF = {avg_ungated_pf:.2f} ({len(ungated)} configs)")
        print(f"  With gate:    avg sell win rate = {avg_gated_wr:.1f}%, avg sell PF = {avg_gated_pf:.2f} ({len(gated)} configs)")
        print(f"  Delta:        win rate {avg_gated_wr - avg_ungated_wr:+.1f}%, PF {avg_gated_pf - avg_ungated_pf:+.2f}")
        print(f"{'='*80}")

    # Best per pair (portfolio)
    print(f"\n{'='*80}")
    print("OPTIMAL PORTFOLIO (best config per pair across all TFs):")
    print(f"{'='*80}")

    best_per_pair = {}
    for r in sorted(viable, key=lambda x: -x["total_pips"]):
        p = r["pair"]
        if p not in best_per_pair:
            best_per_pair[p] = r

    total_daily_pips = 0
    for pair, r in sorted(best_per_pair.items()):
        # Estimate daily pips from candle count
        candles_per_day = {"M5": 288, "M15": 96, "H1": 24, "H4": 6}.get(r["timeframe"], 24)
        days = r.get("candles", 26000) / candles_per_day
        daily_pips = r["total_pips"] / max(days, 1)
        total_daily_pips += daily_pips
        print(f"  {pair:<10} {r['timeframe']:<4} e={r['engine']} t={r['threshold']} "
              f"rr={r['risk_reward']} sl={r['sl_atr_mult']} gate={r['sell_candle_gate']} → "
              f"PF={r['profit_factor']:.2f}, {r['win_rate']:.0f}% win, "
              f"~{daily_pips:.1f} pips/day, "
              f"BUY {r['buy_win_rate']:.0f}%/{r['buy_trades']}t, "
              f"SELL {r['sell_win_rate']:.0f}%/{r['sell_trades']}t")

    print(f"\n  COMBINED: ~{total_daily_pips:.0f} pips/day")
    print(f"  At mini lot ($1/pip): ~${total_daily_pips:.0f}/day")
    print(f"  At $5/pip:            ~${total_daily_pips * 5:.0f}/day")

    # Engine comparison
    rules_results = [r for r in all_results if r["engine"] == "rules" and r["total_trades"] >= 10]
    sniper_results = [r for r in all_results if r["engine"] == "sniper" and r["total_trades"] >= 10]
    if rules_results and sniper_results:
        print(f"\n{'='*80}")
        print("ENGINE COMPARISON (≥10 trades):")
        print(f"  Rules:  {len(rules_results)} configs, "
              f"avg PF={np.mean([r['profit_factor'] for r in rules_results]):.2f}, "
              f"avg win%={np.mean([r['win_rate'] for r in rules_results]):.1f}%, "
              f"avg sell win%={np.mean([r['sell_win_rate'] for r in rules_results if r['sell_trades']>0]):.1f}%")
        print(f"  Sniper: {len(sniper_results)} configs, "
              f"avg PF={np.mean([r['profit_factor'] for r in sniper_results]):.2f}, "
              f"avg win%={np.mean([r['win_rate'] for r in sniper_results]):.1f}%, "
              f"avg sell win%={np.mean([r['sell_win_rate'] for r in sniper_results if r['sell_trades']>0]):.1f}%")
        print(f"{'='*80}")

    # Save JSON
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / "master_sweep_results.json"
    save_data = {
        "all_results": all_results,
        "viable_top50": viable[:50],
        "sell_top30": sell_viable[:30],
        "best_per_pair": best_per_pair,
        "metadata": {
            "total_configs_tested": completed,
            "configs_with_trades": len(all_results),
            "viable_count": len(viable),
            "sell_viable_count": len(sell_viable),
            "elapsed_seconds": round(total_time),
            "run_time": datetime.now(timezone.utc).isoformat(),
            "pairs": pairs,
            "timeframes": timeframes,
        }
    }
    with open(json_path, "w") as f:
        json.dump(save_data, f, indent=2, default=str)

    # Save CSV (all results, easy to open in Excel)
    csv_path = RESULTS_DIR / "master_sweep_results.csv"
    if all_results:
        # Flatten - remove nested dicts
        csv_rows = []
        for r in all_results:
            row = {k: v for k, v in r.items() if not isinstance(v, dict)}
            csv_rows.append(row)
        fields = list(csv_rows[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(csv_rows)

    logger.info("\nResults saved to:")
    logger.info("  JSON: %s", json_path)
    logger.info("  CSV:  %s", csv_path)
    print(f"\n✅ Done! Results at:\n  {json_path}\n  {csv_path}")


if __name__ == "__main__":
    main()
