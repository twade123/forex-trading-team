#!/usr/bin/env python3
"""MASTER SWEEP V2 — Visual Pattern Analysis + Divergence-Gated Sells.

Incorporates findings from 55 chart screenshots + research text:
1. Divergence-gated sells (bearish RSI/Stoch divergence required)
2. ADX >25 mandatory for trend-following sells
3. MACD 5-bar recency enforcement
4. "No Man's Land" filter (skip when between SMA 50 and SMA 100)
5. Resistance zone sells (price at swing high + bearish pattern)
6. Category-based scoring: 1 Momentum + 1 Trend + 1 Volatility
7. Separate buy/sell tracking with full breakdown

Usage:
    cd ~/jarvis/Trading\ Bot
    source ~/myenv/bin/activate
    python -u -m Source.backtester.master_sweep_v2
    
    # Quick test one pair:
    python -u -m Source.backtester.master_sweep_v2 --pair EUR_USD --tf H1
    
    # Use cached data only:
    python -u -m Source.backtester.master_sweep_v2 --no-fetch
"""

import argparse
import csv
import gc
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
os.environ['PYTHONUNBUFFERED'] = '1'

import numpy as np
import pandas as pd

TRADING_BOT = Path(__file__).resolve().parent.parent.parent
JARVIS_ROOT = TRADING_BOT.parent
sys.path.insert(0, str(JARVIS_ROOT))
sys.path.insert(0, str(TRADING_BOT))

from Source.backtester import indicators, divergence
from Source.backtester.candle_patterns import detect_all_patterns

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
logger = logging.getLogger("sweep_v2")
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

ALL_TIMEFRAMES = ["H4", "H1", "M15", "M5"]

JPY_PAIRS = {"USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"}

DATA_DIR = TRADING_BOT / "Data"
RESULTS_DIR = TRADING_BOT / "Results"

# ============================================================================
# V2 STRATEGY ENGINES (from visual pattern analysis)
# ============================================================================

# Engine A: Category-Based (RSI + ADX + BB) — the "Golden Rule" from research
# Engine B: SMA/MACD Trend-Following (Investopedia strategy)
# Engine C: BB + Stochastic Range Trading
# Engine D: Divergence-Primary (divergence as gate, not bonus)

ENGINES = ["category", "sma_macd", "bb_stoch", "divergence"]

PARAM_GRID = {
    # Sell filters (from image analysis)
    "sell_filter": [
        "none",           # No filter
        "candle_gate",    # Require bearish candle pattern
        "divergence_gate", # Require bearish divergence
        "adx_gate",       # Require ADX >25
        "full_gate",      # All: divergence + candle + ADX
    ],
    # Risk:Reward
    "risk_reward": [1.5, 2.0, 2.5, 3.0],
    # Stop loss ATR mult
    "sl_atr_mult": [1.0, 1.5, 2.0, 2.5],
    # No Man's Land filter
    "no_mans_land": [False, True],
}


# ============================================================================
# DATA PREPARATION (same as V1 + extra columns)
# ============================================================================

def load_and_prepare(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # Core indicators
    df = indicators.compute_all(df)
    df = divergence.add_divergence_signals(df)
    df = detect_all_patterns(df)

    # Derived
    df["prev_macd_histogram"] = df["macd_histogram"].shift(1)
    df["prev_close"] = df["close"].shift(1)
    df["prev_high"] = df["high"].shift(1)
    df["prev_low"] = df["low"].shift(1)
    df["prev_stoch_k"] = df["stoch_k"].shift(1)
    df["prev_stoch_d"] = df["stoch_d"].shift(1)
    df["avg_volume"] = df["volume"].rolling(20).mean()

    # MACD crossover recency
    mh = df["macd_histogram"]
    pmh = df["prev_macd_histogram"]
    macd_cross_bull = (mh > 0) & (pmh <= 0)
    macd_cross_bear = (mh < 0) & (pmh >= 0)
    df["macd_cross_bull"] = macd_cross_bull
    df["macd_cross_bear"] = macd_cross_bear

    bars_since_bull = pd.Series(999, index=df.index, dtype=int)
    bars_since_bear = pd.Series(999, index=df.index, dtype=int)
    last_bull = -999
    last_bear = -999
    for i in range(len(df)):
        if macd_cross_bull.iloc[i]:
            last_bull = i
        if macd_cross_bear.iloc[i]:
            last_bear = i
        bars_since_bull.iloc[i] = i - last_bull
        bars_since_bear.iloc[i] = i - last_bear
    df["macd_bull_bars_ago"] = bars_since_bull
    df["macd_bear_bars_ago"] = bars_since_bear

    # Stochastic crossovers
    df["stoch_cross_up"] = (df["stoch_k"] > df["stoch_d"]) & (df["prev_stoch_k"] <= df["prev_stoch_d"])
    df["stoch_cross_down"] = (df["stoch_k"] < df["stoch_d"]) & (df["prev_stoch_k"] >= df["prev_stoch_d"])

    # Consecutive candles
    bull_run = (df["close"] > df["open"]).astype(int)
    bear_run = (df["close"] < df["open"]).astype(int)
    consec_bull = pd.Series(0, index=df.index)
    consec_bear = pd.Series(0, index=df.index)
    for i in range(1, len(df)):
        if bull_run.iloc[i]:
            consec_bull.iloc[i] = consec_bull.iloc[i-1] + 1
        if bear_run.iloc[i]:
            consec_bear.iloc[i] = consec_bear.iloc[i-1] + 1
    df["consec_bull"] = consec_bull
    df["consec_bear"] = consec_bear

    # Swing highs/lows (for divergence detection + S/R)
    lookback = 50
    df["swing_high"] = df["high"].rolling(lookback).max()
    df["swing_low"] = df["low"].rolling(lookback).min()

    # BB penetration
    atr_safe = df["atr"].replace(0, np.nan)
    df["bb_lower_pen"] = np.where(df["close"] < df["bb_lower"],
        (df["bb_lower"] - df["close"]) / atr_safe, 0)
    df["bb_upper_pen"] = np.where(df["close"] > df["bb_upper"],
        (df["close"] - df["bb_upper"]) / atr_safe, 0)

    # RSI slope
    df["rsi_slope"] = df["rsi"].diff(3)

    # Time
    df["hour"] = df["timestamp"].dt.hour

    # "Near resistance" — within 0.5 ATR of swing high
    df["near_resistance"] = (df["swing_high"] - df["close"]) / atr_safe < 0.5
    df["near_support"] = (df["close"] - df["swing_low"]) / atr_safe < 0.5

    return df


# ============================================================================
# SIGNAL PRE-COMPUTATION (V2 — category-based)
# ============================================================================

def precompute_signals_v2(df: pd.DataFrame) -> dict:
    """Pre-compute all V2 signals as numpy arrays."""
    n = len(df)

    # Extract arrays
    close = df["close"].values.astype(np.float64)
    high = df["high"].values.astype(np.float64)
    low = df["low"].values.astype(np.float64)
    atr = df["atr"].values.astype(np.float64).copy()
    atr[atr == 0] = 0.001
    atr[np.isnan(atr)] = 0.001

    sma50 = df["sma_50"].values.astype(np.float64)
    sma100 = df["sma_100"].values.astype(np.float64)
    ema50 = df["ema_50"].values.astype(np.float64)
    ema200 = df["ema_200"].values.astype(np.float64)

    rsi = df["rsi"].values.astype(np.float64)
    stoch_k = df["stoch_k"].values.astype(np.float64)
    stoch_d = df["stoch_d"].values.astype(np.float64)
    adx = df["adx"].values.astype(np.float64)
    macd_hist = df["macd_histogram"].values.astype(np.float64)
    cci_vals = df["cci"].values.astype(np.float64) if "cci" in df.columns else np.zeros(n)

    bb_upper = df["bb_upper"].values.astype(np.float64)
    bb_lower = df["bb_lower"].values.astype(np.float64)
    bb_middle = df["bb_middle"].values.astype(np.float64)

    # Boolean arrays
    macd_bull_bars = df["macd_bull_bars_ago"].values
    macd_bear_bars = df["macd_bear_bars_ago"].values
    stoch_cross_up = df["stoch_cross_up"].values.astype(bool)
    stoch_cross_down = df["stoch_cross_down"].values.astype(bool)

    # Divergence signals (from divergence.py)
    rsi_bull_div = df.get("rsi_bull_div", pd.Series(False, index=df.index)).values.astype(bool)
    rsi_bear_div = df.get("rsi_bear_div", pd.Series(False, index=df.index)).values.astype(bool)
    stoch_bull_div = np.zeros(n, dtype=bool)  # We'll compute below
    stoch_bear_div = np.zeros(n, dtype=bool)

    # Candle patterns
    candle_bull = df.get("candle_bull_signal", pd.Series(0, index=df.index)).values
    candle_bear = df.get("candle_bear_signal", pd.Series(0, index=df.index)).values

    has_bull_pattern = (
        (candle_bull >= 2) |
        df.get("bullish_engulfing", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("hammer", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("morning_star", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("dragonfly_doji", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("three_white_soldiers", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("tweezer_bottom", pd.Series(False, index=df.index)).values.astype(bool)
    )
    has_bear_pattern = (
        (candle_bear >= 2) |
        df.get("bearish_engulfing", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("shooting_star", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("evening_star", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("dark_cloud", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("three_black_crows", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("gravestone_doji", pd.Series(False, index=df.index)).values.astype(bool) |
        df.get("tweezer_top", pd.Series(False, index=df.index)).values.astype(bool)
    )

    near_resistance = df["near_resistance"].values.astype(bool)
    near_support = df["near_support"].values.astype(bool)

    # ---- Pre-compute engine signals per bar ----

    # Arrays for each engine: buy_score, sell_score
    # Engine A: Category-Based (RSI momentum + ADX trend + BB volatility)
    cat_buy = np.zeros(n, dtype=np.float32)
    cat_sell = np.zeros(n, dtype=np.float32)

    # Engine B: SMA/MACD Trend Following
    sma_buy = np.zeros(n, dtype=np.float32)
    sma_sell = np.zeros(n, dtype=np.float32)

    # Engine C: BB + Stochastic Range
    bbst_buy = np.zeros(n, dtype=np.float32)
    bbst_sell = np.zeros(n, dtype=np.float32)

    # Engine D: Divergence Primary
    div_buy = np.zeros(n, dtype=np.float32)
    div_sell = np.zeros(n, dtype=np.float32)

    warmup = 200
    for i in range(warmup, n):
        c = close[i]
        r = rsi[i]
        sk = stoch_k[i]
        sd = stoch_d[i]
        a = adx[i]
        bbu = bb_upper[i]
        bbl = bb_lower[i]
        bbm = bb_middle[i]
        mh = macd_hist[i]
        s50 = sma50[i]
        s100 = sma100[i]
        e50 = ema50[i]
        e200 = ema200[i]
        at = atr[i]
        cc = cci_vals[i]

        if np.isnan(r) or np.isnan(a) or np.isnan(s50):
            continue

        # ========== ENGINE A: Category-Based ==========
        # MOMENTUM score (0-10): RSI + Stochastic + CCI
        mom_buy = mom_sell = 0
        if r < 30: mom_buy += 3
        elif r < 40: mom_buy += 1
        if r > 70: mom_sell += 3
        elif r > 60: mom_sell += 1

        if stoch_cross_up[i] and sk < 25: mom_buy += 3
        elif sk < 20: mom_buy += 1
        if stoch_cross_down[i] and sk > 75: mom_sell += 3
        elif sk > 80: mom_sell += 1

        if cc < -100: mom_buy += 2
        if cc > 100: mom_sell += 2

        # RSI divergence (from images: this is KEY for sells)
        if rsi_bull_div[i]: mom_buy += 3
        if rsi_bear_div[i]: mom_sell += 3

        # TREND score (0-10): ADX + EMA direction
        trend_buy = trend_sell = 0
        if a > 25:
            if e50 > e200: trend_buy += 3
            else: trend_sell += 3
            if a > 35:  # Strong trend
                if e50 > e200: trend_buy += 2
                else: trend_sell += 2
        # MACD confirmation (within 5 bars — from research)
        if macd_bull_bars[i] <= 5: trend_buy += 2
        if macd_bear_bars[i] <= 5: trend_sell += 2
        # MACD momentum direction
        if mh > 0: trend_buy += 1
        if mh < 0: trend_sell += 1

        # VOLATILITY score (0-10): BB position
        vol_buy = vol_sell = 0
        if c <= bbl: vol_buy += 3
        elif c < bbl + 0.3 * (bbm - bbl): vol_buy += 1
        if c >= bbu: vol_sell += 3
        elif c > bbu - 0.3 * (bbu - bbm): vol_sell += 1

        # Near S/R zones (from images: critical for context)
        if near_support[i]: vol_buy += 2
        if near_resistance[i]: vol_sell += 2

        # Candle pattern bonus
        if has_bull_pattern[i]: vol_buy += 2
        if has_bear_pattern[i]: vol_sell += 2

        cat_buy[i] = mom_buy + trend_buy + vol_buy
        cat_sell[i] = mom_sell + trend_sell + vol_sell

        # ========== ENGINE B: SMA/MACD Trend Following ==========
        # From Investopedia: price above/below both SMAs + MACD cross within 5 bars
        above_both = c > s50 and c > s100
        below_both = c < s50 and c < s100
        between_sma = not above_both and not below_both  # No Man's Land

        pip_10 = 0.100 if False else 0.0010  # Will be set per pair
        sma_break_above = (c - min(s50, s100)) > pip_10 * 10
        sma_break_below = (max(s50, s100) - c) > pip_10 * 10

        if above_both and sma_break_above and macd_bull_bars[i] <= 5 and a > 25:
            sma_buy[i] = 10
        if below_both and sma_break_below and macd_bear_bars[i] <= 5 and a > 25:
            sma_sell[i] = 10

        # ========== ENGINE C: BB + Stochastic Range ==========
        # Only active when ADX < 25 (ranging market)
        if a < 25:
            if c <= bbl and stoch_cross_up[i] and sk < 25:
                bbst_buy[i] = 10
            elif c <= bbl and sk < 20:
                bbst_buy[i] = 6
            if c >= bbu and stoch_cross_down[i] and sk > 75:
                bbst_sell[i] = 10
            elif c >= bbu and sk > 80:
                bbst_sell[i] = 6

        # ========== ENGINE D: Divergence Primary ==========
        # Divergence is THE signal, everything else is confirmation
        if rsi_bull_div[i]:
            score = 6
            if has_bull_pattern[i]: score += 3
            if sk < 30: score += 2
            if near_support[i]: score += 2
            div_buy[i] = score
        if rsi_bear_div[i]:
            score = 6
            if has_bear_pattern[i]: score += 3
            if sk > 70: score += 2
            if near_resistance[i]: score += 2
            div_sell[i] = score

    return {
        "close": close, "high": high, "low": low, "atr": atr,
        "sma50": sma50, "sma100": sma100,
        "adx": adx, "rsi": rsi,
        "macd_bear_bars": macd_bear_bars,
        "rsi_bear_div": rsi_bear_div, "rsi_bull_div": rsi_bull_div,
        "has_bear_pattern": has_bear_pattern, "has_bull_pattern": has_bull_pattern,
        "near_resistance": near_resistance, "near_support": near_support,
        "cat_buy": cat_buy, "cat_sell": cat_sell,
        "sma_buy": sma_buy, "sma_sell": sma_sell,
        "bbst_buy": bbst_buy, "bbst_sell": bbst_sell,
        "div_buy": div_buy, "div_sell": div_sell,
        "n": n,
    }


# ============================================================================
# POSITION + BACKTEST
# ============================================================================

class Position:
    __slots__ = ['direction', 'entry_price', 'stop_loss', 'take_profit',
                 'risk_pips', 'half_exited', 'pips']
    def __init__(self, direction, entry_price, stop_loss, take_profit, risk_pips):
        self.direction = direction
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.risk_pips = risk_pips
        self.half_exited = False
        self.pips = 0.0


def run_backtest_v2(signals: dict, engine: str, params: dict, is_jpy: bool) -> dict:
    """Run V2 backtest with sell filters from visual pattern analysis."""

    sell_filter = params["sell_filter"]
    rr = params["risk_reward"]
    sl_mult = params["sl_atr_mult"]
    no_mans_land = params["no_mans_land"]

    pip_mult = 100.0 if is_jpy else 10000.0
    threshold_pip = 0.100 if is_jpy else 0.0010
    warmup = 200
    max_positions = 2

    # Pick engine signal arrays
    if engine == "category":
        buy_scores = signals["cat_buy"]
        sell_scores = signals["cat_sell"]
        threshold = 8  # Needs momentum + trend + volatility
    elif engine == "sma_macd":
        buy_scores = signals["sma_buy"]
        sell_scores = signals["sma_sell"]
        threshold = 8
    elif engine == "bb_stoch":
        buy_scores = signals["bbst_buy"]
        sell_scores = signals["bbst_sell"]
        threshold = 5
    elif engine == "divergence":
        buy_scores = signals["div_buy"]
        sell_scores = signals["div_sell"]
        threshold = 6
    else:
        return {"total_trades": 0}

    close = signals["close"]
    high = signals["high"]
    low = signals["low"]
    atr = signals["atr"]
    sma50 = signals["sma50"]
    sma100 = signals["sma100"]
    adx = signals["adx"]
    n = signals["n"]

    positions = []
    trades = []

    for i in range(warmup, n):
        c = close[i]
        h = high[i]
        l = low[i]
        a = atr[i]

        # --- Check exits ---
        to_close = []
        for j, pos in enumerate(positions):
            if pos.direction == "buy":
                if l <= pos.stop_loss:
                    pips = (pos.stop_loss - pos.entry_price) * pip_mult
                    if pos.half_exited: pips = pos.pips + pips * 0.5
                    trades.append({"direction": "buy", "pips": pips, "exit": "sl"})
                    to_close.append(j); continue
                if not pos.half_exited and h >= pos.take_profit:
                    pos.pips += (pos.take_profit - pos.entry_price) * pip_mult * 0.5
                    pos.half_exited = True
                    pos.stop_loss = pos.entry_price
                    continue
                if pos.half_exited and c < sma50[i] - threshold_pip:
                    pips = (c - pos.entry_price) * pip_mult * 0.5
                    trades.append({"direction": "buy", "pips": pos.pips + pips, "exit": "trail"})
                    to_close.append(j); continue
            else:  # sell
                if h >= pos.stop_loss:
                    pips = (pos.entry_price - pos.stop_loss) * pip_mult
                    if pos.half_exited: pips = pos.pips + pips * 0.5
                    trades.append({"direction": "sell", "pips": pips, "exit": "sl"})
                    to_close.append(j); continue
                if not pos.half_exited and l <= pos.take_profit:
                    pos.pips += (pos.entry_price - pos.take_profit) * pip_mult * 0.5
                    pos.half_exited = True
                    pos.stop_loss = pos.entry_price
                    continue
                if pos.half_exited and c > sma50[i] + threshold_pip:
                    pips = (pos.entry_price - c) * pip_mult * 0.5
                    trades.append({"direction": "sell", "pips": pos.pips + pips, "exit": "trail"})
                    to_close.append(j); continue

        for j in sorted(to_close, reverse=True):
            positions.pop(j)

        if len(positions) >= max_positions:
            continue

        # --- No Man's Land filter ---
        if no_mans_land:
            s50 = sma50[i]
            s100 = sma100[i]
            if not np.isnan(s50) and not np.isnan(s100):
                if min(s50, s100) < c < max(s50, s100):
                    continue

        # --- Signal ---
        bs = buy_scores[i]
        ss = sell_scores[i]
        direction = None

        if bs >= threshold and bs > ss + 2:
            direction = "buy"
        elif ss >= threshold and ss > bs + 2:
            direction = "sell"
        else:
            continue

        # --- Sell filters (from visual pattern analysis) ---
        if direction == "sell":
            if sell_filter == "candle_gate":
                if not signals["has_bear_pattern"][i]:
                    continue
            elif sell_filter == "divergence_gate":
                if not signals["rsi_bear_div"][i]:
                    continue
            elif sell_filter == "adx_gate":
                if adx[i] < 25:
                    continue
            elif sell_filter == "full_gate":
                # Need at least 2 of 3: divergence, candle, ADX
                checks = 0
                if signals["rsi_bear_div"][i]: checks += 1
                if signals["has_bear_pattern"][i]: checks += 1
                if adx[i] >= 25: checks += 1
                if checks < 2:
                    continue

        # --- SL/TP ---
        sl_dist = a * sl_mult
        if direction == "buy":
            sl = c - sl_dist
            tp = c + sl_dist * rr
        else:
            sl = c + sl_dist
            tp = c - sl_dist * rr

        positions.append(Position(direction, c, sl, tp, sl_dist * pip_mult))

    # Close remaining
    if positions:
        last_c = close[-1]
        for pos in positions:
            if pos.direction == "buy":
                pips = (last_c - pos.entry_price) * pip_mult
            else:
                pips = (pos.entry_price - last_c) * pip_mult
            if pos.half_exited: pips = pos.pips + pips * 0.5
            trades.append({"direction": pos.direction, "pips": pips, "exit": "eod"})

    return _compute_stats(trades, engine, params)


def _compute_stats(trades, engine, params):
    if not trades:
        return {"total_trades": 0}

    buy_trades = [t for t in trades if t["direction"] == "buy"]
    sell_trades = [t for t in trades if t["direction"] == "sell"]

    def _s(tl, lbl):
        if not tl:
            return {f"{lbl}_trades": 0, f"{lbl}_wins": 0, f"{lbl}_wr": 0,
                    f"{lbl}_pips": 0, f"{lbl}_pf": 0}
        w = [t for t in tl if t["pips"] > 0]
        lo = [t for t in tl if t["pips"] <= 0]
        gp = sum(t["pips"] for t in w) if w else 0
        gl = abs(sum(t["pips"] for t in lo)) if lo else 0.01
        return {
            f"{lbl}_trades": len(tl), f"{lbl}_wins": len(w),
            f"{lbl}_wr": round(len(w)/len(tl)*100, 1),
            f"{lbl}_pips": round(sum(t["pips"] for t in tl), 1),
            f"{lbl}_pf": round(gp/gl, 2),
        }

    all_w = [t for t in trades if t["pips"] > 0]
    all_l = [t for t in trades if t["pips"] <= 0]
    gp = sum(t["pips"] for t in all_w) if all_w else 0
    gl = abs(sum(t["pips"] for t in all_l)) if all_l else 0.01
    tp = sum(t["pips"] for t in trades)

    run = 0
    peak = 0
    dd = 0
    for t in trades:
        run += t["pips"]
        peak = max(peak, run)
        dd = max(dd, peak - run)

    return {
        "engine": engine,
        "total_trades": len(trades), "wins": len(all_w),
        "win_rate": round(len(all_w)/len(trades)*100, 1),
        "total_pips": round(tp, 1),
        "profit_factor": round(gp/gl, 2),
        "max_dd": round(dd, 1),
        **_s(buy_trades, "buy"), **_s(sell_trades, "sell"),
        **params,
    }


# ============================================================================
# SWEEP
# ============================================================================

def generate_v2_configs():
    configs = []
    for sf, rr, sl, nml in product(
        PARAM_GRID["sell_filter"], PARAM_GRID["risk_reward"],
        PARAM_GRID["sl_atr_mult"], PARAM_GRID["no_mans_land"]
    ):
        configs.append({
            "sell_filter": sf, "risk_reward": rr,
            "sl_atr_mult": sl, "no_mans_land": nml,
        })
    return configs


def get_data_path(pair, tf):
    return DATA_DIR / f"{pair.lower()}_{tf.lower()}_3yr.csv"


def main():
    parser = argparse.ArgumentParser(description="Master Sweep V2 — Visual Pattern Analysis")
    parser.add_argument("--pair", type=str)
    parser.add_argument("--tf", type=str)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else ALL_PAIRS
    timeframes = [args.tf] if args.tf else ALL_TIMEFRAMES

    configs = generate_v2_configs()
    if args.quick:
        configs = [c for c in configs if c["risk_reward"] == 2.0 and c["sl_atr_mult"] in [1.5, 2.0]]

    # Find available data
    pair_tf_combos = []
    for pair in pairs:
        for tf in timeframes:
            if get_data_path(pair, tf).exists():
                pair_tf_combos.append((pair, tf))

    total_runs = len(pair_tf_combos) * len(ENGINES) * len(configs)
    print(f"{'='*70}")
    print(f"MASTER SWEEP V2 — Visual Pattern Analysis Edition")
    print(f"  Pairs: {len(pairs)} | TFs: {len(timeframes)} | Combos: {len(pair_tf_combos)}")
    print(f"  Engines: {len(ENGINES)} ({', '.join(ENGINES)})")
    print(f"  Param configs: {len(configs)}")
    print(f"  Total backtests: {total_runs}")
    print(f"{'='*70}")

    all_results = []
    completed = 0
    start_time = time.time()

    for ci, (pair, tf) in enumerate(pair_tf_combos):
        csv_path = get_data_path(pair, tf)
        is_jpy = pair in JPY_PAIRS

        print(f"\n[{ci+1}/{len(pair_tf_combos)}] {pair}/{tf} ...", end=" ", flush=True)
        t0 = time.time()

        try:
            df = load_and_prepare(str(csv_path))
        except Exception as e:
            print(f"FAILED: {e}")
            completed += len(ENGINES) * len(configs)
            continue

        prep_time = time.time() - t0
        print(f"{len(df)} candles, prep={prep_time:.1f}s", end=" ", flush=True)

        t1 = time.time()
        try:
            signals = precompute_signals_v2(df)
        except Exception as e:
            print(f"SIG FAILED: {e}")
            completed += len(ENGINES) * len(configs)
            del df; gc.collect()
            continue

        sig_time = time.time() - t1
        print(f"sig={sig_time:.1f}s", end=" ", flush=True)

        combo_results = []
        best_pf = 0
        best_r = None

        for engine in ENGINES:
            for cfg in configs:
                try:
                    stats = run_backtest_v2(signals, engine, cfg, is_jpy)
                except Exception:
                    completed += 1
                    continue
                completed += 1
                t = stats.get("total_trades", 0)
                if t > 0:
                    stats["pair"] = pair
                    stats["timeframe"] = tf
                    combo_results.append(stats)
                    if t >= 10 and stats["profit_factor"] > best_pf:
                        best_pf = stats["profit_factor"]
                        best_r = stats

        combo_time = time.time() - t0
        elapsed = time.time() - start_time
        rate = completed / elapsed if elapsed > 0 else 1
        eta = (total_runs - completed) / rate if rate > 0 else 0

        if best_r:
            print(f"done={combo_time:.0f}s ETA={eta/60:.0f}m | BEST PF={best_r['profit_factor']:.2f} "
                  f"e={best_r['engine']} sf={best_r['sell_filter']} "
                  f"BUY:{best_r['buy_wr']:.0f}%/{best_r['buy_trades']}t "
                  f"SELL:{best_r['sell_wr']:.0f}%/{best_r['sell_trades']}t", flush=True)
        else:
            print(f"done={combo_time:.0f}s — no trades", flush=True)

        all_results.extend(combo_results)
        del df, signals; gc.collect()

    total_time = time.time() - start_time

    if not all_results:
        print("\n❌ No trades!")
        return

    # ========================================================================
    # RESULTS
    # ========================================================================

    viable = [r for r in all_results if r["total_trades"] >= 15 and r["profit_factor"] > 1.0]
    viable.sort(key=lambda x: x["profit_factor"], reverse=True)

    sell_viable = [r for r in all_results if r["sell_trades"] >= 10 and r["sell_pf"] > 1.0]
    sell_viable.sort(key=lambda x: x["sell_pf"], reverse=True)

    print(f"\n{'='*160}")
    print(f"V2 SWEEP COMPLETE — {total_time/60:.1f}min, {len(all_results)} with trades, {len(viable)} viable")
    print(f"{'='*160}")

    hdr = (f"{'PAIR':<10} {'TF':<4} {'ENGINE':<11} {'FILT':<16} {'R:R':>4} {'SL':>4} {'NML':>4} "
           f"{'TRD':>5} {'W%':>5} {'PIPS':>8} {'PF':>6} {'DD':>6} "
           f"{'B_T':>4} {'B%':>5} {'B_P':>7} {'BPF':>5} "
           f"{'S_T':>4} {'S%':>5} {'S_P':>7} {'SPF':>5}")

    # Top 50 overall
    print(f"\nTOP 50 OVERALL:")
    print(hdr)
    print("-" * 160)
    for r in viable[:50]:
        nml = "Y" if r["no_mans_land"] else "N"
        print(f"{r['pair']:<10} {r['timeframe']:<4} {r['engine']:<11} {r['sell_filter']:<16} "
              f"{r['risk_reward']:>4.1f} {r['sl_atr_mult']:>4.1f} {nml:>4} "
              f"{r['total_trades']:>5} {r['win_rate']:>4.1f}% {r['total_pips']:>7.0f} "
              f"{r['profit_factor']:>6.2f} {r['max_dd']:>5.0f} "
              f"{r['buy_trades']:>4} {r['buy_wr']:>4.1f}% {r['buy_pips']:>6.0f} {r['buy_pf']:>5.2f} "
              f"{r['sell_trades']:>4} {r['sell_wr']:>4.1f}% {r['sell_pips']:>6.0f} {r['sell_pf']:>5.2f}")

    # Best SELL performers
    print(f"\nTOP 30 BEST SELL PERFORMANCE (≥10 sell trades, sell PF>1.0):")
    print(hdr)
    print("-" * 160)
    for r in sell_viable[:30]:
        nml = "Y" if r["no_mans_land"] else "N"
        print(f"{r['pair']:<10} {r['timeframe']:<4} {r['engine']:<11} {r['sell_filter']:<16} "
              f"{r['risk_reward']:>4.1f} {r['sl_atr_mult']:>4.1f} {nml:>4} "
              f"{r['total_trades']:>5} {r['win_rate']:>4.1f}% {r['total_pips']:>7.0f} "
              f"{r['profit_factor']:>6.2f} {r['max_dd']:>5.0f} "
              f"{r['buy_trades']:>4} {r['buy_wr']:>4.1f}% {r['buy_pips']:>6.0f} {r['buy_pf']:>5.2f} "
              f"{r['sell_trades']:>4} {r['sell_wr']:>4.1f}% {r['sell_pips']:>6.0f} {r['sell_pf']:>5.2f}")

    # Engine comparison
    print(f"\n{'='*100}")
    print("ENGINE COMPARISON (≥10 trades):")
    for eng in ENGINES:
        er = [r for r in all_results if r["engine"] == eng and r["total_trades"] >= 10]
        if er:
            avg_pf = np.mean([r["profit_factor"] for r in er])
            avg_wr = np.mean([r["win_rate"] for r in er])
            sell_ers = [r for r in er if r["sell_trades"] > 0]
            avg_swr = np.mean([r["sell_wr"] for r in sell_ers]) if sell_ers else 0
            viable_count = len([r for r in er if r["profit_factor"] > 1.0])
            print(f"  {eng:<12}: {len(er):>5} configs, avg PF={avg_pf:.2f}, win%={avg_wr:.1f}%, "
                  f"sell win%={avg_swr:.1f}%, {viable_count} profitable")

    # Sell filter comparison
    print(f"\n{'='*100}")
    print("SELL FILTER COMPARISON (≥5 sell trades):")
    for sf in PARAM_GRID["sell_filter"]:
        sr = [r for r in all_results if r["sell_filter"] == sf and r["sell_trades"] >= 5]
        if sr:
            avg_swr = np.mean([r["sell_wr"] for r in sr])
            avg_spf = np.mean([r["sell_pf"] for r in sr])
            avg_st = np.mean([r["sell_trades"] for r in sr])
            print(f"  {sf:<18}: {len(sr):>5} configs, avg sell win%={avg_swr:.1f}%, "
                  f"avg sell PF={avg_spf:.2f}, avg sell trades={avg_st:.0f}")

    # No Man's Land comparison
    print(f"\n{'='*100}")
    print("NO MAN'S LAND FILTER:")
    for nml in [False, True]:
        nr = [r for r in all_results if r["no_mans_land"] == nml and r["total_trades"] >= 10]
        if nr:
            avg_pf = np.mean([r["profit_factor"] for r in nr])
            avg_wr = np.mean([r["win_rate"] for r in nr])
            print(f"  NML={'ON' if nml else 'OFF':<4}: {len(nr):>5} configs, "
                  f"avg PF={avg_pf:.2f}, avg win%={avg_wr:.1f}%")

    # Portfolio
    print(f"\n{'='*80}")
    print("OPTIMAL PORTFOLIO (best per pair):")
    best_per_pair = {}
    for r in sorted(viable, key=lambda x: -x["total_pips"]):
        if r["pair"] not in best_per_pair:
            best_per_pair[r["pair"]] = r
    total_dp = 0
    for pair, r in sorted(best_per_pair.items()):
        cpd = {"M5": 288, "M15": 96, "H1": 24, "H4": 6}.get(r["timeframe"], 24)
        days = r.get("total_trades", 1) / max(r.get("total_trades", 1) / 1000, 0.1)  # rough
        dp = r["total_pips"] / 1095  # 3 years
        total_dp += dp
        print(f"  {pair:<10} {r['timeframe']:<4} e={r['engine']:<10} f={r['sell_filter']:<15} "
              f"rr={r['risk_reward']} sl={r['sl_atr_mult']} nml={r['no_mans_land']} → "
              f"PF={r['profit_factor']:.2f} {r['win_rate']:.0f}% "
              f"BUY:{r['buy_wr']:.0f}%/{r['buy_trades']}t SELL:{r['sell_wr']:.0f}%/{r['sell_trades']}t")
    print(f"\n  ~{total_dp:.0f} pips/day combined")

    # Save
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / "master_sweep_v2_results.json"
    with open(json_path, "w") as f:
        json.dump({
            "all_results": all_results,
            "viable": viable[:50],
            "sell_viable": sell_viable[:30],
            "best_per_pair": best_per_pair,
            "metadata": {
                "total_tested": completed,
                "with_trades": len(all_results),
                "viable": len(viable),
                "elapsed_seconds": round(total_time),
                "run_time": datetime.now(timezone.utc).isoformat(),
            }
        }, f, indent=2, default=str)

    csv_path = RESULTS_DIR / "master_sweep_v2_results.csv"
    if all_results:
        rows = [{k: v for k, v in r.items() if not isinstance(v, dict)} for r in all_results]
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    print(f"\n✅ V2 Done! Results at:\n  {json_path}\n  {csv_path}")


if __name__ == "__main__":
    main()
