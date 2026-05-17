#!/usr/bin/env python3
"""MASTER SWEEP V3 — Comprehensive 20-Setup Analysis with Regime Detection.

This is the ultimate forex backtesting sweep that runs ALL 20 trading setups 
simultaneously on every candle across 3 years of cached OANDA data.

Features:
- 5 regime detector (strong_trend, ranging, exhaustion, squeeze, high_volatility) 
- 20 comprehensive setups (candlestick, chart patterns, indicators, structure, volatility)
- Full trade recording with ALL indicator values at entry
- Walk-forward simulation with proper exit logic
- Concurrent setup detection and confluence analysis
- Multiple output files for deep analysis
- Memory efficient processing (one pair/timeframe at a time)

Usage:
    cd ~/jarvis/Trading\ Bot
    source ~/myenv/bin/activate
    python -u -m Source.backtester.master_sweep_v3 --no-fetch
    
    # Test specific combinations:
    python -u -m Source.backtester.master_sweep_v3 --pairs EUR_USD --timeframes H1 --setups S11,S13
"""

import argparse
import csv
import gc
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Force unbuffered output for real-time progress
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
os.environ['PYTHONUNBUFFERED'] = '1'

import numpy as np
import pandas as pd
from scipy import stats

# Setup paths
TRADING_BOT = Path(__file__).resolve().parent.parent.parent
JARVIS_ROOT = TRADING_BOT.parent
sys.path.insert(0, str(JARVIS_ROOT))
sys.path.insert(0, str(TRADING_BOT))

# Import our modules
from Source.backtester import indicators, divergence
from Source.backtester.candle_patterns import detect_all_patterns

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
logger = logging.getLogger("sweep_v3")
logger.setLevel(logging.INFO)

# ============================================================================
# CONFIGURATION
# ============================================================================

ALL_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", 
    "AUD_USD", "NZD_USD", "USD_CAD",
    "EUR_GBP", "EUR_JPY", "GBP_JPY", 
    "EUR_AUD", "EUR_CHF", "AUD_JPY"
]

ALL_TIMEFRAMES = ["H4", "H1", "M15"]  # Skip M5 for performance

JPY_PAIRS = {"USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"}

# Bug 3: Add spreads per pair
SPREADS = {
    "EUR_USD": 0.00012, "GBP_USD": 0.00015, "USD_JPY": 0.015,
    "USD_CHF": 0.00015, "AUD_USD": 0.00015, "NZD_USD": 0.00018,
    "USD_CAD": 0.00018, "EUR_GBP": 0.00020, "EUR_JPY": 0.020,
    "GBP_JPY": 0.025, "EUR_AUD": 0.00025, "EUR_CHF": 0.00020,
    "AUD_JPY": 0.020
}

DATA_DIR = TRADING_BOT / "Data"
RESULTS_DIR = TRADING_BOT / "Results"

# All 20 setups
ALL_SETUPS = [
    "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10",
    "S11", "S12", "S13", "S14", "S15", "S16", "S17", "S18", "S19", "S20"
]

WARMUP_PERIOD = 200
MAX_HOLD_CANDLES = 50
MAX_POSITIONS_PER_SETUP = 1  # Conservative per param variant

# Parameter sweep: override each setup's hardcoded SL/TP with ATR-based variants
PARAM_SWEEP = True  # Set False to use setup's own SL/TP
RR_MULTIPLIERS = [1.5, 2.0, 2.5, 3.0]  # TP = entry ± RR × ATR
SL_MULTIPLIERS = [1.0, 1.5, 2.0, 2.5]  # SL = entry ∓ SL × ATR

# ============================================================================
# REGIME DETECTOR
# ============================================================================

def detect_regime(df: pd.DataFrame, i: int) -> str:
    """Classify current candle into one of 5 market regimes."""
    if i < WARMUP_PERIOD:
        return "unknown"
    
    try:
        adx = df.iloc[i]["adx"]
        adx_prev = df.iloc[i-5:i]["adx"].mean() if i >= 5 else adx
        price = df.iloc[i]["close"]
        sma50 = df.iloc[i]["sma_50"]
        sma100 = df.iloc[i]["sma_100"]
        bb_width = df.iloc[i]["bb_width"]
        bb_width_avg = df.iloc[i-20:i]["bb_width"].mean() if i >= 20 else bb_width
        atr = df.iloc[i]["atr"]
        atr_avg = df.iloc[i-20:i]["atr"].mean() if i >= 20 else atr
        
        # Check for valid values
        if pd.isna(adx) or pd.isna(price) or pd.isna(sma50):
            return "unknown"
            
        adx_rising = adx > adx_prev
        price_above_smas = price > sma50 and price > sma100
        price_below_smas = price < sma50 and price < sma100
        
        # Strong trend: ADX > 30 and rising, price clearly above/below SMAs
        if adx > 30 and adx_rising and (price_above_smas or price_below_smas):
            return "strong_trend"
            
        # Ranging: ADX < 20, price oscillating, BB flat
        if adx < 20 and abs(price - sma50) < atr * 0.5:
            return "ranging"
            
        # Exhaustion: ADX declining from > 30, divergence possible
        if adx > 25 and not adx_rising and adx_prev > 30:
            return "exhaustion"
            
        # Squeeze: BB width narrowing, ADX < 15
        if bb_width < bb_width_avg * 0.8 and adx < 15:
            return "squeeze"
            
        # High volatility: ATR > 1.5x average
        if atr > atr_avg * 1.5:
            return "high_volatility"
            
        return "ranging"  # Default
        
    except Exception:
        return "unknown"


# ============================================================================
# POSITION CLASS
# ============================================================================

class Position:
    """Holds all position data for walk-forward simulation."""
    def __init__(self, setup_name: str, direction: str, entry_price: float, 
                 entry_time: str, sl_price: float, tp_price: float, 
                 entry_index: int, confidence: float, trigger_reason: str):
        self.trade_id = str(uuid.uuid4())[:8]
        self.setup_name = setup_name
        self.direction = direction
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.sl_price = sl_price
        self.original_sl_price = sl_price  # Store original SL for breakeven calculation
        self.tp_price = tp_price
        self.entry_index = entry_index
        self.confidence = confidence
        self.trigger_reason = trigger_reason
        self.exit_time = None
        self.exit_price = None
        self.result = None
        self.pips = 0.0
        self.risk_reward_actual = 0.0
        self.max_favorable_pips = 0.0
        self.max_adverse_pips = 0.0
        self.candles_to_exit = 0
        self.exit_reason = None
        
        # Feature 2: Trailing Stop (Breakeven)
        self.be_triggered = False
        self.be_candle = None
        
        # Feature 3: Partial Exit Simulation  
        self.partial_exit_hit = False
        self.partial_exit_pips = 0.0
        self.partial_exit_candle = None
        self.tp1_price = None  # Will be calculated as 1:1 R:R
        self.second_half_result = None
        self.second_half_pips = 0.0


# ============================================================================
# SETUP FUNCTIONS (All 20)
# ============================================================================

def setup_s1_hammer_pinbar(df: pd.DataFrame, i: int) -> dict:
    """S1: Hammer/Pin Bar at support + reversal indicators."""
    try:
        if i < WARMUP_PERIOD:
            return None
            
        row = df.iloc[i]
        
        # Check for hammer/pin bar patterns
        is_hammer = row.get("hammer", False)
        is_shooting_star = row.get("shooting_star", False)
        
        # Support/resistance levels
        bb_lower = row["bb_lower"]
        bb_upper = row["bb_upper"] 
        bb_mid = row["bb_middle"]
        close = row["close"]
        rsi = row["rsi"]
        stoch_k = row["stoch_k"]
        stoch_d = row["stoch_d"]
        atr = row["atr"]
        
        # Buy signal: Hammer at support
        if (is_hammer and close <= bb_lower * 1.01 and 
            rsi < 35 and stoch_k < 25):
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": close - atr * 1.5,
                "tp_price": min(bb_mid, close + atr * 2.0),
                "confidence": 7.5,
                "trigger_reason": f"Hammer at BB lower band, RSI={rsi:.1f}, Stoch={stoch_k:.1f}"
            }
            
        # Sell signal: Shooting star at resistance  
        if (is_shooting_star and close >= bb_upper * 0.99 and 
            rsi > 65 and stoch_k > 75):
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": close + atr * 1.5,
                "tp_price": max(bb_mid, close - atr * 2.0),
                "confidence": 7.5,
                "trigger_reason": f"Shooting star at BB upper band, RSI={rsi:.1f}, Stoch={stoch_k:.1f}"
            }
            
    except Exception:
        pass
    return None


def setup_s2_engulfing(df: pd.DataFrame, i: int) -> dict:
    """S2: Engulfing pattern at key levels."""
    try:
        if i < WARMUP_PERIOD:
            return None
            
        row = df.iloc[i]
        
        bullish_eng = row.get("bullish_engulfing", False)
        bearish_eng = row.get("bearish_engulfing", False)
        
        close = row["close"]
        atr = row["atr"]
        prev_close = df.iloc[i-1]["close"] if i > 0 else close
        
        if bullish_eng:
            return {
                "direction": "buy", 
                "entry_price": close,
                "sl_price": min(row["low"], df.iloc[i-1]["low"]) - atr * 0.2,
                "tp_price": close + abs(close - prev_close) * 1.5,
                "confidence": 8.0,
                "trigger_reason": "Bullish engulfing pattern"
            }
            
        if bearish_eng:
            return {
                "direction": "sell",
                "entry_price": close, 
                "sl_price": max(row["high"], df.iloc[i-1]["high"]) + atr * 0.2,
                "tp_price": close - abs(close - prev_close) * 1.5,
                "confidence": 8.0,
                "trigger_reason": "Bearish engulfing pattern"
            }
            
    except Exception:
        pass
    return None


def setup_s3_morning_evening_star(df: pd.DataFrame, i: int) -> dict:
    """S3: Morning/Evening Star patterns."""
    try:
        if i < WARMUP_PERIOD:
            return None
            
        row = df.iloc[i]
        morning_star = row.get("morning_star", False)
        evening_star = row.get("evening_star", False)
        
        close = row["close"]
        atr = row["atr"]
        
        if morning_star:
            star_low = min(df.iloc[i-2:i+1]["low"])
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": star_low - atr * 0.3,
                "tp_price": close + (close - star_low) * 2,
                "confidence": 9.0,
                "trigger_reason": "Morning star reversal pattern"
            }
            
        if evening_star:
            star_high = max(df.iloc[i-2:i+1]["high"])
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": star_high + atr * 0.3,
                "tp_price": close - (star_high - close) * 2,
                "confidence": 9.0,
                "trigger_reason": "Evening star reversal pattern"
            }
            
    except Exception:
        pass
    return None


def setup_s4_doji_extremes(df: pd.DataFrame, i: int) -> dict:
    """S4: Doji at RSI/Stoch extremes with confirmation."""
    try:
        if i < WARMUP_PERIOD + 1:
            return None
            
        row = df.iloc[i-1]  # Look at previous candle
        current = df.iloc[i]  # Current for confirmation
        
        dragonfly = row.get("dragonfly_doji", False)
        gravestone = row.get("gravestone_doji", False)
        rsi = row["rsi"]
        stoch_k = row["stoch_k"]
        close = current["close"]
        prev_close = row["close"]
        atr = current["atr"]
        
        # Dragonfly doji + confirmation candle
        # Bug 6: Loosen thresholds - RSI 25->30, Stoch 20->25
        if (dragonfly and rsi < 30 and stoch_k < 25 and 
            close > prev_close):  # Confirmation
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": row["low"] - atr * 0.5,
                "tp_price": close + atr * 1.5,
                "confidence": 7.0,
                "trigger_reason": f"Dragonfly doji confirmed, RSI={rsi:.1f}"
            }
            
        # Gravestone doji + confirmation
        # Bug 6: Loosen thresholds - RSI 75->70, Stoch 80->75
        if (gravestone and rsi > 70 and stoch_k > 75 and
            close < prev_close):  # Confirmation
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": row["high"] + atr * 0.5,
                "tp_price": close - atr * 1.5,
                "confidence": 7.0,
                "trigger_reason": f"Gravestone doji confirmed, RSI={rsi:.1f}"
            }
            
    except Exception:
        pass
    return None


def setup_s5_ascending_triangle(df: pd.DataFrame, i: int) -> dict:
    """S5: Ascending triangle breakout."""
    try:
        if i < WARMUP_PERIOD + 10:
            return None
            
        # Look for flat resistance over last 10-20 periods
        lookback = min(15, i - WARMUP_PERIOD)
        recent_highs = df.iloc[i-lookback:i]["high"]
        current = df.iloc[i]
        close = current["close"]
        high = current["high"]
        atr = current["atr"]
        
        # Find resistance level (mode of recent highs)
        resistance = recent_highs.quantile(0.9)  # Top 10% as resistance proxy
        touches = sum(abs(recent_highs - resistance) < atr * 0.1)
        
        # Rising lows check
        recent_lows = df.iloc[i-lookback:i]["low"]
        if len(recent_lows) < 5:
            return None
            
        # Linear regression on lows to check if rising
        x = np.arange(len(recent_lows))
        slope, _, _, p_value, _ = stats.linregress(x, recent_lows)
        
        # Breakout above resistance with rising lows
        if (touches >= 3 and slope > 0 and p_value < 0.1 and
            close > resistance and high > resistance):
            
            # Find last higher low for SL
            last_hl = recent_lows.iloc[-5:].min()  # Conservative
            triangle_height = resistance - last_hl
            
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": last_hl - atr * 0.3,
                "tp_price": close + triangle_height,
                "confidence": 8.5,
                "trigger_reason": f"Ascending triangle breakout, {touches} touches at {resistance:.5f}"
            }
            
    except Exception:
        pass
    return None


def setup_s6_descending_triangle(df: pd.DataFrame, i: int) -> dict:
    """S6: Descending triangle breakdown."""
    try:
        if i < WARMUP_PERIOD + 10:
            return None
            
        lookback = min(15, i - WARMUP_PERIOD)
        recent_lows = df.iloc[i-lookback:i]["low"]
        current = df.iloc[i]
        close = current["close"]
        low = current["low"]
        atr = current["atr"]
        
        # Find support level
        support = recent_lows.quantile(0.1)  # Bottom 10% as support
        touches = sum(abs(recent_lows - support) < atr * 0.1)
        
        # Descending highs check
        recent_highs = df.iloc[i-lookback:i]["high"]
        if len(recent_highs) < 5:
            return None
            
        x = np.arange(len(recent_highs))
        slope, _, _, p_value, _ = stats.linregress(x, recent_highs)
        
        # Breakdown below support with descending highs
        if (touches >= 3 and slope < 0 and p_value < 0.1 and
            close < support and low < support):
            
            last_lh = recent_highs.iloc[-5:].max()  # Conservative
            triangle_height = last_lh - support
            
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": last_lh + atr * 0.3,
                "tp_price": close - triangle_height,
                "confidence": 8.5,
                "trigger_reason": f"Descending triangle breakdown, {touches} touches at {support:.5f}"
            }
            
    except Exception:
        pass
    return None


def setup_s7_channel_trading(df: pd.DataFrame, i: int) -> dict:
    """S7: Channel trading with linear regression."""
    try:
        if i < WARMUP_PERIOD + 20:
            return None
            
        lookback = min(50, i - WARMUP_PERIOD)
        recent_closes = df.iloc[i-lookback:i]["close"]
        x = np.arange(len(recent_closes))
        
        # Linear regression to find trend
        slope, intercept, r_value, _, _ = stats.linregress(x, recent_closes)
        
        # Need reasonable correlation for channel
        if abs(r_value) < 0.3:
            return None
            
        # Calculate channel lines
        predicted = slope * x + intercept
        residuals = recent_closes.values - predicted
        std_dev = np.std(residuals)
        
        upper_channel = predicted + 2 * std_dev
        lower_channel = predicted - 2 * std_dev
        
        current = df.iloc[i]
        close = current["close"]
        atr = current["atr"]
        
        # Current position relative to channel
        current_predicted = slope * len(recent_closes) + intercept
        current_upper = current_predicted + 2 * std_dev
        current_lower = current_predicted - 2 * std_dev
        
        # Buy at lower channel with bullish candle
        if (close <= current_lower * 1.02 and 
            current["close"] > current["open"]):  # Bullish candle
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": current_lower - atr,
                "tp_price": current_upper,
                "confidence": 6.5,
                "trigger_reason": f"Buy at lower channel line, R²={r_value**2:.2f}"
            }
            
        # Sell at upper channel with bearish candle  
        if (close >= current_upper * 0.98 and
            current["close"] < current["open"]):  # Bearish candle
            return {
                "direction": "sell", 
                "entry_price": close,
                "sl_price": current_upper + atr,
                "tp_price": current_lower,
                "confidence": 6.5,
                "trigger_reason": f"Sell at upper channel line, R²={r_value**2:.2f}"
            }
            
    except Exception:
        pass
    return None


def setup_s8_sr_break(df: pd.DataFrame, i: int) -> dict:
    """S8: Support/Resistance break with confirmation."""
    try:
        if i < WARMUP_PERIOD + 20:
            return None
            
        current = df.iloc[i]
        close = current["close"]
        high = current["high"]
        low = current["low"]
        atr = current["atr"]
        volume = current.get("volume", 1)
        avg_volume = df.iloc[i-20:i]["volume"].mean() if "volume" in df.columns else 1
        
        # Look for S/R levels from swing highs/lows
        lookback = min(50, i - WARMUP_PERIOD)
        recent = df.iloc[i-lookback:i]
        
        # Find significant swing highs (resistance)
        swing_highs = []
        for j in range(5, len(recent) - 5):
            if all(recent.iloc[j]["high"] >= recent.iloc[j+k]["high"] for k in range(-5, 6) if k != 0):
                swing_highs.append(recent.iloc[j]["high"])
                
        # Find significant swing lows (support) 
        swing_lows = []
        for j in range(5, len(recent) - 5):
            if all(recent.iloc[j]["low"] <= recent.iloc[j+k]["low"] for k in range(-5, 6) if k != 0):
                swing_lows.append(recent.iloc[j]["low"])
        
        # Check for resistance break
        for resistance in swing_highs:
            if (close > resistance and high > resistance * 1.001 and 
                df.iloc[i-1]["close"] <= resistance and
                volume > avg_volume * 1.2):  # Volume confirmation
                return {
                    "direction": "buy",
                    "entry_price": close,
                    "sl_price": resistance - atr * 0.5,
                    "tp_price": close + (close - resistance) * 2,
                    "confidence": 7.5,
                    "trigger_reason": f"Resistance break at {resistance:.5f} with volume"
                }
        
        # Check for support break
        for support in swing_lows:
            if (close < support and low < support * 0.999 and
                df.iloc[i-1]["close"] >= support and
                volume > avg_volume * 1.2):
                return {
                    "direction": "sell",
                    "entry_price": close,
                    "sl_price": support + atr * 0.5,
                    "tp_price": close - (support - close) * 2,
                    "confidence": 7.5,
                    "trigger_reason": f"Support break at {support:.5f} with volume"
                }
                
    except Exception:
        pass
    return None


def setup_s9_head_shoulders(df: pd.DataFrame, i: int) -> dict:
    """S9: Head & shoulders (simplified detection)."""
    try:
        if i < WARMUP_PERIOD + 30:
            return None
            
        # Look for 3 peaks pattern over reasonable timeframe
        lookback = min(30, i - WARMUP_PERIOD)
        recent = df.iloc[i-lookback:i]
        
        # Find local peaks
        # Bug 7: Reduce neighbor comparison window from ±5 to ±3
        peaks = []
        for j in range(3, len(recent) - 3):
            if all(recent.iloc[j]["high"] >= recent.iloc[j+k]["high"] for k in range(-3, 4) if k != 0):
                peaks.append((j, recent.iloc[j]["high"]))
        
        if len(peaks) < 3:
            return None
            
        # Take last 3 peaks
        peaks = peaks[-3:]
        left_shoulder = peaks[0][1]
        head = peaks[1][1] 
        right_shoulder = peaks[2][1]
        
        # Head should be highest
        if not (head > left_shoulder and head > right_shoulder):
            return None
            
        # Shoulders should be roughly equal
        shoulder_diff = abs(left_shoulder - right_shoulder)
        if shoulder_diff > (head - min(left_shoulder, right_shoulder)) * 0.3:
            return None
            
        # Find neckline (lows between peaks)
        neckline_points = []
        for j in range(peaks[0][0], peaks[2][0]):
            if j < len(recent) - 1:
                neckline_points.append(recent.iloc[j]["low"])
        
        if not neckline_points:
            return None
            
        neckline = min(neckline_points)
        current = df.iloc[i]
        close = current["close"]
        atr = current["atr"]
        
        # Break below neckline = sell signal
        if close < neckline * 0.995:  # Small buffer
            target_distance = head - neckline
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": neckline + atr,
                "tp_price": close - target_distance,
                "confidence": 8.0,
                "trigger_reason": f"Head & shoulders neckline break at {neckline:.5f}"
            }
            
    except Exception:
        pass
    return None


def setup_s10_double_top_bottom(df: pd.DataFrame, i: int) -> dict:
    """S10: Double top/bottom pattern."""
    try:
        if i < WARMUP_PERIOD + 20:
            return None
            
        lookback = min(40, i - WARMUP_PERIOD)
        recent = df.iloc[i-lookback:i]
        current = df.iloc[i]
        close = current["close"]
        atr = current["atr"]
        
        # Find significant peaks for double top
        peaks = []
        for j in range(5, len(recent) - 5):
            if all(recent.iloc[j]["high"] >= recent.iloc[j+k]["high"] for k in range(-5, 6) if k != 0):
                peaks.append((j, recent.iloc[j]["high"]))
        
        # Check for double top (two similar peaks)
        for k in range(len(peaks) - 1):
            for l in range(k + 1, len(peaks)):
                peak1_price = peaks[k][1]
                peak2_price = peaks[l][1]
                
                # Similar heights (within 0.2 * ATR)
                if abs(peak1_price - peak2_price) < atr * 0.2:
                    # Find valley between peaks
                    start_idx = peaks[k][0]
                    end_idx = peaks[l][0]
                    valley = min(recent.iloc[start_idx:end_idx]["low"])
                    
                    # Break below valley = sell signal
                    if close < valley * 0.995:
                        tp_price = valley - (max(peak1_price, peak2_price) - valley)
                        # Bug 4: Ensure sell TP is always below entry
                        tp_price = min(tp_price, close - atr * 0.5)
                        return {
                            "direction": "sell",
                            "entry_price": close,
                            "sl_price": max(peak1_price, peak2_price) + atr * 0.5,
                            "tp_price": tp_price,
                            "confidence": 7.5,
                            "trigger_reason": f"Double top break, peaks at {peak1_price:.5f} and {peak2_price:.5f}"
                        }
        
        # Find troughs for double bottom
        troughs = []
        for j in range(5, len(recent) - 5):
            if all(recent.iloc[j]["low"] <= recent.iloc[j+k]["low"] for k in range(-5, 6) if k != 0):
                troughs.append((j, recent.iloc[j]["low"]))
        
        # Check for double bottom
        for k in range(len(troughs) - 1):
            for l in range(k + 1, len(troughs)):
                trough1_price = troughs[k][1]
                trough2_price = troughs[l][1]
                
                if abs(trough1_price - trough2_price) < atr * 0.2:
                    # Find peak between troughs
                    start_idx = troughs[k][0]
                    end_idx = troughs[l][0]
                    peak = max(recent.iloc[start_idx:end_idx]["high"])
                    
                    # Break above peak = buy signal
                    if close > peak * 1.005:
                        tp_price = peak + (peak - min(trough1_price, trough2_price))
                        # Bug 4: Ensure buy TP is always above entry
                        tp_price = max(tp_price, close + atr * 0.5)
                        return {
                            "direction": "buy",
                            "entry_price": close,
                            "sl_price": min(trough1_price, trough2_price) - atr * 0.5,
                            "tp_price": tp_price,
                            "confidence": 7.5,
                            "trigger_reason": f"Double bottom break, troughs at {trough1_price:.5f} and {trough2_price:.5f}"
                        }
                        
    except Exception:
        pass
    return None


def setup_s11_sma_macd(df: pd.DataFrame, i: int) -> dict:
    """S11: SMA50/100 + MACD crossover."""
    try:
        if i < WARMUP_PERIOD + 1:
            return None
            
        current = df.iloc[i]
        prev = df.iloc[i-1]
        
        close = current["close"]
        sma50 = current["sma_50"]
        sma100 = current["sma_100"]
        macd_hist = current["macd_histogram"]
        prev_macd_hist = prev["macd_histogram"]
        atr = current["atr"]
        
        if pd.isna(sma50) or pd.isna(sma100) or pd.isna(macd_hist):
            return None
            
        # Buy: price crosses above both SMAs AND MACD crosses positive
        if (close > sma50 and close > sma100 and
            prev["close"] <= min(prev["sma_50"], prev["sma_100"]) and
            macd_hist > 0 and prev_macd_hist <= 0):
            
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": close - atr * 1.5,
                "tp_price": close + atr * 2.0,
                "confidence": 8.5,
                "trigger_reason": f"Price above SMA50/100 + MACD bullish cross"
            }
            
        # Sell: price crosses below both SMAs AND MACD crosses negative
        if (close < sma50 and close < sma100 and
            prev["close"] >= max(prev["sma_50"], prev["sma_100"]) and
            macd_hist < 0 and prev_macd_hist >= 0):
            
            return {
                "direction": "sell", 
                "entry_price": close,
                "sl_price": close + atr * 1.5,
                "tp_price": close - atr * 2.0,
                "confidence": 8.5,
                "trigger_reason": f"Price below SMA50/100 + MACD bearish cross"
            }
            
    except Exception:
        pass
    return None


def setup_s12_bb_squeeze_breakout(df: pd.DataFrame, i: int) -> dict:
    """S12: Bollinger Band squeeze breakout."""
    try:
        if i < WARMUP_PERIOD + 20:
            return None
            
        current = df.iloc[i]
        close = current["close"]
        bb_width = current["bb_width"]
        bb_upper = current["bb_upper"]
        bb_lower = current["bb_lower"]
        sma50 = current["sma_50"]
        atr = current["atr"]
        
        # Calculate BB width average over last 20 periods
        bb_width_avg = df.iloc[i-20:i]["bb_width"].mean()
        
        # Check for squeeze (BB width below average)
        if bb_width >= bb_width_avg * 0.8:
            return None
            
        # SMA50 slope for trend direction
        sma50_prev = df.iloc[i-5]["sma_50"] if i >= 5 else sma50
        trend_up = sma50 > sma50_prev
        
        # Breakout above upper band
        if close > bb_upper and trend_up:
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": bb_lower,
                "tp_price": close + atr * 2.0,
                "confidence": 7.0,
                "trigger_reason": f"BB squeeze breakout upward, width={bb_width:.4f} vs avg={bb_width_avg:.4f}"
            }
            
        # Breakout below lower band
        if close < bb_lower and not trend_up:
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": bb_upper,
                "tp_price": close - atr * 2.0,
                "confidence": 7.0,
                "trigger_reason": f"BB squeeze breakout downward, width={bb_width:.4f} vs avg={bb_width_avg:.4f}"
            }
            
    except Exception:
        pass
    return None


def setup_s13_stoch_crossover(df: pd.DataFrame, i: int) -> dict:
    """S13: Slow Stochastic crossover."""
    try:
        if i < WARMUP_PERIOD + 1:
            return None
            
        current = df.iloc[i]
        prev = df.iloc[i-1]
        
        stoch_k = current["stoch_k"]
        stoch_d = current["stoch_d"]
        prev_stoch_k = prev["stoch_k"]
        prev_stoch_d = prev["stoch_d"]
        close = current["close"]
        atr = current["atr"]
        
        if pd.isna(stoch_k) or pd.isna(stoch_d):
            return None
            
        # Buy: %K crosses above %D below 20
        if (stoch_k > stoch_d and prev_stoch_k <= prev_stoch_d and
            stoch_k < 25 and stoch_d < 25):
            
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": close - atr * 1.0,
                "tp_price": close + atr * 1.5,
                "confidence": 6.5,
                "trigger_reason": f"Stoch bullish cross at oversold, K={stoch_k:.1f} D={stoch_d:.1f}"
            }
            
        # Sell: %K crosses below %D above 80
        if (stoch_k < stoch_d and prev_stoch_k >= prev_stoch_d and
            stoch_k > 75 and stoch_d > 75):
            
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": close + atr * 1.0,
                "tp_price": close - atr * 1.5,
                "confidence": 6.5,
                "trigger_reason": f"Stoch bearish cross at overbought, K={stoch_k:.1f} D={stoch_d:.1f}"
            }
            
    except Exception:
        pass
    return None


def setup_s14_cci_extremes(df: pd.DataFrame, i: int) -> dict:
    """S14: CCI extremes reversal."""
    try:
        if i < WARMUP_PERIOD + 1:
            return None
            
        current = df.iloc[i]
        prev = df.iloc[i-1]
        
        cci = current.get("cci", 0)
        prev_cci = prev.get("cci", 0)
        close = current["close"]
        atr = current["atr"]
        
        if pd.isna(cci) or pd.isna(prev_cci):
            return None
            
        # Buy: CCI crosses back above -100 (was oversold)
        if cci > -100 and prev_cci <= -100:
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": close - atr * 1.5,
                "tp_price": close + atr * 2.0,
                "confidence": 6.0,
                "trigger_reason": f"CCI reversal from oversold, CCI={cci:.1f}"
            }
            
        # Sell: CCI crosses back below +100
        if cci < 100 and prev_cci >= 100:
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": close + atr * 1.5,
                "tp_price": close - atr * 2.0,
                "confidence": 6.0,
                "trigger_reason": f"CCI reversal from overbought, CCI={cci:.1f}"
            }
            
    except Exception:
        pass
    return None


def setup_s15_rsi_divergence(df: pd.DataFrame, i: int) -> dict:
    """S15: RSI divergence signals."""
    try:
        if i < WARMUP_PERIOD:
            return None
            
        current = df.iloc[i]
        
        rsi_bull_div = current.get("rsi_bull_div", False)
        rsi_bear_div = current.get("rsi_bear_div", False)
        close = current["close"]
        atr = current["atr"]
        rsi = current["rsi"]
        
        if rsi_bull_div:
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": close - atr * 1.5,
                "tp_price": close + atr * 2.5,
                "confidence": 8.0,
                "trigger_reason": f"Bullish RSI divergence, RSI={rsi:.1f}"
            }
            
        if rsi_bear_div:
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": close + atr * 1.5,
                "tp_price": close - atr * 2.5,
                "confidence": 8.0,
                "trigger_reason": f"Bearish RSI divergence, RSI={rsi:.1f}"
            }
            
    except Exception:
        pass
    return None


def setup_s16_sar_flip(df: pd.DataFrame, i: int) -> dict:
    """S16: Parabolic SAR flip with ADX filter."""
    try:
        if i < WARMUP_PERIOD + 1:
            return None
            
        current = df.iloc[i]
        prev = df.iloc[i-1]
        
        close = current["close"]
        sar = current.get("parabolic_sar", close)
        prev_sar = prev.get("parabolic_sar", close)
        prev_close = prev["close"]
        adx = current["adx"]
        atr = current["atr"]
        
        if pd.isna(sar) or pd.isna(adx) or adx < 25:
            return None
            
        # SAR flips from above to below price = buy
        if prev_sar > prev_close and sar < close:
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": sar,
                "tp_price": close + atr * 2.0,
                "confidence": 7.0,
                "trigger_reason": f"SAR flip bullish with ADX={adx:.1f}"
            }
            
        # SAR flips from below to above price = sell  
        if prev_sar < prev_close and sar > close:
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": sar,
                "tp_price": close - atr * 2.0,
                "confidence": 7.0,
                "trigger_reason": f"SAR flip bearish with ADX={adx:.1f}"
            }
            
    except Exception:
        pass
    return None


def setup_s17_pivot_bounce(df: pd.DataFrame, i: int) -> dict:
    """S17: Pivot point bounce/break."""
    try:
        if i < WARMUP_PERIOD + 1:
            return None
            
        # Calculate daily pivot points from previous day's high/low/close
        # For simplicity, use last 24 candles as "daily" data
        lookback = min(24, i)
        recent = df.iloc[i-lookback:i]
        
        if len(recent) == 0:
            return None
            
        prev_high = recent["high"].max()
        prev_low = recent["low"].min()
        prev_close = recent.iloc[-1]["close"]
        
        # Calculate pivot points
        pivot = (prev_high + prev_low + prev_close) / 3
        r1 = 2 * pivot - prev_low
        r2 = pivot + (prev_high - prev_low)
        s1 = 2 * pivot - prev_high
        s2 = pivot - (prev_high - prev_low)
        
        current = df.iloc[i]
        close = current["close"]
        atr = current["atr"]
        is_bullish_candle = current["close"] > current["open"]
        is_bearish_candle = current["close"] < current["open"]
        
        # Buy at S1/S2 with bullish candle confirmation
        # Bug 8: Loosen proximity threshold from 0.3*ATR to 0.5*ATR
        if is_bullish_candle:
            if abs(close - s1) < atr * 0.5:
                return {
                    "direction": "buy",
                    "entry_price": close,
                    "sl_price": s2 - atr * 0.5,
                    "tp_price": pivot,
                    "confidence": 6.5,
                    "trigger_reason": f"Bounce at S1 pivot level {s1:.5f}"
                }
            if abs(close - s2) < atr * 0.5:
                return {
                    "direction": "buy",
                    "entry_price": close,
                    "sl_price": s2 - atr * 0.8,
                    "tp_price": s1,
                    "confidence": 6.0,
                    "trigger_reason": f"Bounce at S2 pivot level {s2:.5f}"
                }
                
        # Sell at R1/R2 with bearish candle confirmation
        if is_bearish_candle:
            if abs(close - r1) < atr * 0.5:
                return {
                    "direction": "sell",
                    "entry_price": close,
                    "sl_price": r2 + atr * 0.5,
                    "tp_price": pivot,
                    "confidence": 6.5,
                    "trigger_reason": f"Bounce at R1 pivot level {r1:.5f}"
                }
            if abs(close - r2) < atr * 0.5:
                return {
                    "direction": "sell",
                    "entry_price": close,
                    "sl_price": r2 + atr * 0.8,
                    "tp_price": r1,
                    "confidence": 6.0,
                    "trigger_reason": f"Bounce at R2 pivot level {r2:.5f}"
                }
                
    except Exception:
        pass
    return None


def setup_s18_fib_retracement(df: pd.DataFrame, i: int) -> dict:
    """S18: Fibonacci 50% retracement."""
    try:
        if i < WARMUP_PERIOD + 20:
            return None
            
        # Look for significant swing moves over recent period
        lookback = min(50, i - WARMUP_PERIOD)
        recent = df.iloc[i-lookback:i]
        current = df.iloc[i]
        close = current["close"]
        atr = current["atr"]
        
        # Find last significant move (>2*ATR)
        swing_high = recent["high"].max()
        swing_low = recent["low"].min()
        swing_range = swing_high - swing_low
        
        if swing_range < atr * 2:
            return None
            
        # Calculate Fibonacci levels
        fib_236 = swing_high - 0.236 * swing_range
        fib_382 = swing_high - 0.382 * swing_range
        fib_500 = swing_high - 0.500 * swing_range
        fib_618 = swing_high - 0.618 * swing_range
        
        # Check for retracement to 50% level
        if abs(close - fib_500) < atr * 0.2:
            
            # Determine original trend direction
            recent_trend = recent.iloc[-10:]["close"].iloc[-1] - recent.iloc[-10:]["close"].iloc[0]
            is_bullish_candle = current["close"] > current["open"]
            is_bearish_candle = current["close"] < current["open"]
            
            # Bullish retracement (was going up, now bouncing off 50%)
            if recent_trend > 0 and is_bullish_candle:
                return {
                    "direction": "buy",
                    "entry_price": close,
                    "sl_price": fib_618 - atr * 0.3,
                    "tp_price": swing_high,
                    "confidence": 7.5,
                    "trigger_reason": f"50% Fib retracement bounce from {fib_500:.5f} in uptrend"
                }
                
            # Bearish retracement (was going down, now bouncing off 50%)
            if recent_trend < 0 and is_bearish_candle:
                return {
                    "direction": "sell",
                    "entry_price": close,
                    "sl_price": fib_382 + atr * 0.3,
                    "tp_price": swing_low,
                    "confidence": 7.5,
                    "trigger_reason": f"50% Fib retracement bounce from {fib_500:.5f} in downtrend"
                }
                
    except Exception:
        pass
    return None


def setup_s19_atr_expansion(df: pd.DataFrame, i: int) -> dict:
    """S19: ATR expansion volatility trade."""
    try:
        if i < WARMUP_PERIOD + 20:
            return None
            
        current = df.iloc[i]
        atr = current["atr"]
        atr_avg = df.iloc[i-20:i]["atr"].mean()
        close = current["close"]
        open_price = current["open"]
        
        # ATR spike detection
        if atr <= atr_avg * 1.5:
            return None
            
        # Direction of the volatility spike
        candle_direction = 1 if close > open_price else -1
        candle_size = abs(close - open_price)
        
        # Must be a significant candle
        if candle_size < atr * 0.8:
            return None
            
        if candle_direction > 0:  # Bullish volatility spike
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": close - atr * 2.0,
                "tp_price": close + atr * 3.0,
                "confidence": 6.0,
                "trigger_reason": f"ATR expansion trade, ATR={atr:.5f} vs avg={atr_avg:.5f}"
            }
        else:  # Bearish volatility spike
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": close + atr * 2.0,
                "tp_price": close - atr * 3.0,
                "confidence": 6.0,
                "trigger_reason": f"ATR expansion trade, ATR={atr:.5f} vs avg={atr_avg:.5f}"
            }
            
    except Exception:
        pass
    return None


def setup_s20_multi_timeframe(df: pd.DataFrame, i: int) -> dict:
    """S20: Multi-timeframe confirmation (simplified)."""
    try:
        if i < WARMUP_PERIOD + 10:
            return None
            
        # For this sweep, we'll simulate higher timeframe by checking
        # SMA50 slope over longer period as proxy for H4 trend
        current = df.iloc[i]
        close = current["close"]
        sma50 = current["sma_50"]
        atr = current["atr"]
        
        # "Higher timeframe" trend: SMA50 slope over 10 periods
        if i < 10:
            return None
            
        sma50_10_ago = df.iloc[i-10]["sma_50"]
        htf_trend_up = sma50 > sma50_10_ago
        
        # Current timeframe signal (simple RSI + price position)
        rsi = current["rsi"]
        bb_upper = current["bb_upper"]
        bb_lower = current["bb_lower"]
        
        # Buy signal: HTF uptrend + LTF oversold + bullish candle
        if (htf_trend_up and rsi < 35 and close < bb_lower * 1.02 and
            current["close"] > current["open"]):
            
            return {
                "direction": "buy",
                "entry_price": close,
                "sl_price": close - atr * 1.5,
                "tp_price": close + atr * 2.5,
                "confidence": 7.0,
                "trigger_reason": f"MTF: HTF uptrend + LTF oversold, RSI={rsi:.1f}"
            }
            
        # Sell signal: HTF downtrend + LTF overbought + bearish candle
        if (not htf_trend_up and rsi > 65 and close > bb_upper * 0.98 and
            current["close"] < current["open"]):
            
            return {
                "direction": "sell",
                "entry_price": close,
                "sl_price": close + atr * 1.5,
                "tp_price": close - atr * 2.5,
                "confidence": 7.0,
                "trigger_reason": f"MTF: HTF downtrend + LTF overbought, RSI={rsi:.1f}"
            }
            
    except Exception:
        pass
    return None


# Setup function mapping
SETUP_FUNCTIONS = {
    "S1": setup_s1_hammer_pinbar,
    "S2": setup_s2_engulfing,
    "S3": setup_s3_morning_evening_star,
    "S4": setup_s4_doji_extremes,
    "S5": setup_s5_ascending_triangle,
    "S6": setup_s6_descending_triangle,
    "S7": setup_s7_channel_trading,
    "S8": setup_s8_sr_break,
    "S9": setup_s9_head_shoulders,
    "S10": setup_s10_double_top_bottom,
    "S11": setup_s11_sma_macd,
    "S12": setup_s12_bb_squeeze_breakout,
    "S13": setup_s13_stoch_crossover,
    "S14": setup_s14_cci_extremes,
    "S15": setup_s15_rsi_divergence,
    "S16": setup_s16_sar_flip,
    "S17": setup_s17_pivot_bounce,
    "S18": setup_s18_fib_retracement,
    "S19": setup_s19_atr_expansion,
    "S20": setup_s20_multi_timeframe,
}


# ============================================================================
# DATA PREPARATION
# ============================================================================

def load_and_prepare_h4_data(pair: str) -> pd.DataFrame:
    """Load and prepare H4 data for multi-timeframe analysis."""
    try:
        h4_path = DATA_DIR / f"{pair}_h4_3yr.csv"
        if not h4_path.exists():
            logger.warning(f"H4 data not found for {pair}: {h4_path}")
            return pd.DataFrame()
            
        df_h4 = pd.read_csv(h4_path)
        if "timestamp" not in df_h4.columns and "time" in df_h4.columns:
            df_h4["timestamp"] = df_h4["time"]
            
        df_h4["timestamp"] = pd.to_datetime(df_h4["timestamp"])
        
        # Calculate H4 SMAs
        df_h4["sma_50"] = df_h4["close"].rolling(50).mean()
        df_h4["sma_100"] = df_h4["close"].rolling(100).mean()
        
        return df_h4.sort_values("timestamp").reset_index(drop=True)
        
    except Exception as e:
        logger.error(f"Failed to load H4 data for {pair}: {e}")
        return pd.DataFrame()


def get_h4_trend(h4_df: pd.DataFrame, current_time: pd.Timestamp) -> tuple:
    """Get H4 trend direction for current timestamp."""
    if h4_df.empty:
        return "neutral", False, ""
        
    try:
        # Ensure timezone compatibility
        if current_time.tzinfo is None and h4_df["timestamp"].dt.tz is not None:
            current_time = current_time.tz_localize("UTC")
        elif current_time.tzinfo is not None and h4_df["timestamp"].dt.tz is None:
            current_time = current_time.tz_localize(None)
        # Find most recent H4 candle <= current time
        h4_candles = h4_df[h4_df["timestamp"] <= current_time]
        if h4_candles.empty:
            return "neutral", False, "no_h4_data"
            
        latest_h4 = h4_candles.iloc[-1]
        close = latest_h4["close"]
        sma50 = latest_h4["sma_50"]
        sma100 = latest_h4["sma_100"]
        
        if pd.isna(sma50) or pd.isna(sma100):
            return "neutral", False, "insufficient_h4_sma"
            
        # H4 trend logic
        if close > sma50 and sma50 > sma100:
            return "bullish", True, f"H4_close>{sma50:.5f}>sma100_{sma100:.5f}"
        elif close < sma50 and sma50 < sma100:
            return "bearish", True, f"H4_close<{sma50:.5f}<sma100_{sma100:.5f}"
        else:
            return "neutral", False, f"H4_mixed_sma50_{sma50:.5f}_sma100_{sma100:.5f}"
            
    except Exception as e:
        return "neutral", False, f"error_{str(e)[:20]}"


def get_daily_pivots(df: pd.DataFrame, current_idx: int) -> dict:
    """Calculate daily pivot levels from previous day's data."""
    try:
        # Use last 24 candles as "previous day" approximation
        lookback = min(24, current_idx)
        if lookback <= 0:
            return {}
            
        prev_day_data = df.iloc[current_idx - lookback:current_idx]
        if prev_day_data.empty:
            return {}
            
        prev_high = prev_day_data["high"].max()
        prev_low = prev_day_data["low"].min()
        prev_close = prev_day_data.iloc[-1]["close"]
        
        # Calculate pivot points
        pp = (prev_high + prev_low + prev_close) / 3
        r1 = 2 * pp - prev_low
        r2 = pp + (prev_high - prev_low) 
        r3 = prev_high + 2 * (pp - prev_low)
        s1 = 2 * pp - prev_high
        s2 = pp - (prev_high - prev_low)
        s3 = prev_low - 2 * (prev_high - pp)
        
        return {
            "PP": pp, "R1": r1, "R2": r2, "R3": r3,
            "S1": s1, "S2": s2, "S3": s3
        }
        
    except Exception:
        return {}


def get_nearest_pivot_info(entry_price: float, pivots: dict, atr: float) -> dict:
    """Find nearest pivot level and calculate distances."""
    if not pivots:
        return {
            "nearest_daily_pivot": "none",
            "dist_to_daily_pivot_atr": 999.0,
            "near_daily_resistance": False,
            "near_daily_support": False
        }
    
    try:
        # Find nearest pivot
        distances = {}
        for level, price in pivots.items():
            distances[level] = abs(entry_price - price)
            
        nearest_level = min(distances, key=distances.get)
        nearest_distance = distances[nearest_level]
        dist_in_atr = nearest_distance / max(atr, 0.00001)
        
        # Check if near resistance/support levels
        resistance_levels = [pivots.get("R1", 0), pivots.get("R2", 0), pivots.get("R3", 0)]
        support_levels = [pivots.get("S1", 0), pivots.get("S2", 0), pivots.get("S3", 0)]
        
        near_resistance = any(abs(entry_price - r) < atr * 0.5 for r in resistance_levels if r > 0)
        near_support = any(abs(entry_price - s) < atr * 0.5 for s in support_levels if s > 0)
        
        return {
            "nearest_daily_pivot": nearest_level,
            "dist_to_daily_pivot_atr": round(dist_in_atr, 2),
            "near_daily_resistance": near_resistance,
            "near_daily_support": near_support
        }
        
    except Exception:
        return {
            "nearest_daily_pivot": "error",
            "dist_to_daily_pivot_atr": 999.0,
            "near_daily_resistance": False,
            "near_daily_support": False
        }


def get_trading_session(timestamp_str: str) -> str:
    """Determine trading session based on UTC time."""
    try:
        # Parse timestamp and get UTC hour
        dt = pd.to_datetime(timestamp_str)
        if dt.tz is not None:
            dt = dt.tz_convert('UTC')
        else:
            # Assume UTC if no timezone info
            dt = dt.tz_localize('UTC')
            
        hour = dt.hour
        
        # Session definitions (UTC)
        if 0 <= hour < 8:
            return "Asian"
        elif 8 <= hour < 13:
            return "London"
        elif 13 <= hour < 17:
            return "NY_Overlap"
        elif 17 <= hour < 22:
            return "NY"
        else:  # 22-24
            return "Off_Hours"
            
    except Exception:
        return "Unknown"


def load_and_prepare_data(csv_path: str) -> pd.DataFrame:
    """Load CSV and compute all indicators + patterns."""
    try:
        # Read data
        df = pd.read_csv(csv_path)
        if "timestamp" not in df.columns and "time" in df.columns:
            df["timestamp"] = df["time"]
        
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        
        # Ensure we have OHLCV columns
        required_cols = ["open", "high", "low", "close", "volume"]
        for col in required_cols:
            if col not in df.columns:
                if col == "volume":
                    df[col] = 1000  # Default volume
                else:
                    raise ValueError(f"Missing required column: {col}")
        
        # Compute all indicators
        df = indicators.compute_all(df)
        
        # Add divergence signals
        df = divergence.add_divergence_signals(df)
        
        # Add candlestick patterns
        df = detect_all_patterns(df)
        
        # Add some additional derived columns
        df["prev_close"] = df["close"].shift(1)
        df["prev_high"] = df["high"].shift(1)  
        df["prev_low"] = df["low"].shift(1)
        
        return df
        
    except Exception as e:
        logger.error(f"Failed to load/prepare data from {csv_path}: {e}")
        raise


# ============================================================================
# TRADE SIMULATION ENGINE
# ============================================================================

def simulate_trades(df: pd.DataFrame, pair: str, timeframe: str, setups_to_run: list) -> list:
    """Run all setups on every candle and simulate walk-forward."""
    
    pip_multiplier = 100 if pair in JPY_PAIRS else 10000
    all_trades = []
    active_positions = {setup: [] for setup in setups_to_run}
    
    # Feature 1: Load H4 data for multi-timeframe analysis
    # For H4 timeframe testing itself, we'll use Daily concepts (SMA slope over 20 candles)
    h4_data = load_and_prepare_h4_data(pair) if timeframe in ["H1", "M15", "M5", "M10", "M30"] else pd.DataFrame()
    use_daily_concept = timeframe == "H4"
    
    # Feature 6: Loss streak tracking per setup
    loss_streaks = {setup: 0 for setup in setups_to_run}
    max_loss_streaks = {setup: 0 for setup in setups_to_run}
    
    total_candles = len(df)
    last_progress = 0
    
    for i in range(WARMUP_PERIOD, total_candles):
        
        # Progress reporting
        progress = int((i - WARMUP_PERIOD) / (total_candles - WARMUP_PERIOD) * 100)
        if progress - last_progress >= 5:  # Every 5%
            active_count = sum(len(positions) for positions in active_positions.values())
            print(f"\r[{pair} {timeframe}] Candle {i}/{total_candles} | Active: {active_count} | Trades: {len(all_trades)} | {progress}%", 
                  end="", flush=True)
            last_progress = progress
        
        current_time = df.iloc[i]["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        regime = detect_regime(df, i)
        
        # === CHECK EXITS FOR ACTIVE POSITIONS ===
        for setup_name in list(active_positions.keys()):
            positions_to_remove = []
            
            for pos_idx, pos in enumerate(active_positions[setup_name]):
                exit_info = check_position_exit(df, i, pos, pip_multiplier)
                
                if exit_info:
                    pos.exit_time = current_time
                    pos.exit_price = exit_info["exit_price"]
                    pos.result = exit_info["result"]
                    pos.pips = exit_info["pips"]
                    pos.candles_to_exit = i - pos.entry_index
                    pos.exit_reason = exit_info["reason"]
                    
                    # Calculate actual risk:reward
                    if pos.result == "win":
                        risk_pips = abs(pos.entry_price - pos.sl_price) * pip_multiplier
                        pos.risk_reward_actual = abs(pos.pips) / max(risk_pips, 0.1)
                    else:
                        pos.risk_reward_actual = -1.0
                    
                    # Feature 6: Update loss streaks
                    if pos.result == "loss":
                        loss_streaks[pos.setup_name] += 1
                        max_loss_streaks[pos.setup_name] = max(max_loss_streaks[pos.setup_name], loss_streaks[pos.setup_name])
                    else:  # win or breakeven
                        loss_streaks[pos.setup_name] = 0
                    
                    # Create full trade record with all new features
                    trade_record = create_trade_record(df, i, pos, pair, timeframe, regime, h4_data, max_loss_streaks)
                    
                    # Bug 9: Pip Calculation Sanity Check
                    if trade_record["result"] == "win" and trade_record["pips"] < 0:
                        trade_record["result"] = "loss"
                        logger.debug(f"Fixed trade {trade_record['trade_id']}: win with negative pips -> loss")
                    elif trade_record["result"] == "loss" and trade_record["pips"] > 0:
                        trade_record["result"] = "win"
                        logger.debug(f"Fixed trade {trade_record['trade_id']}: loss with positive pips -> win")
                    
                    all_trades.append(trade_record)
                    
                    positions_to_remove.append(pos_idx)
            
            # Remove closed positions
            for idx in reversed(positions_to_remove):
                active_positions[setup_name].pop(idx)
        
        # === RUN ALL SETUPS TO FIND NEW SIGNALS ===
        candle_signals = {}
        
        for setup_name in setups_to_run:
            if len(active_positions[setup_name]) >= MAX_POSITIONS_PER_SETUP:
                continue
                
            try:
                signal = SETUP_FUNCTIONS[setup_name](df, i)
                if signal:
                    candle_signals[setup_name] = signal
            except Exception as e:
                logger.debug(f"Error in {setup_name} at {i}: {e}")
                continue
        
        # === PROCESS NEW SIGNALS ===
        for setup_name, signal in candle_signals.items():
            direction = signal["direction"]
            base_entry = signal["entry_price"]
            atr = df.iloc[i]["atr"] if "atr" in df.columns else 0.001
            
            # Generate parameter variants
            if PARAM_SWEEP and atr > 0:
                param_variants = []
                for rr in RR_MULTIPLIERS:
                    for sl in SL_MULTIPLIERS:
                        variant_name = f"{setup_name}_rr{rr}_sl{sl}"
                        if direction == "buy":
                            v_sl = base_entry - sl * atr
                            v_tp = base_entry + rr * atr
                        else:
                            v_sl = base_entry + sl * atr
                            v_tp = base_entry - rr * atr
                        param_variants.append((variant_name, v_sl, v_tp, rr, sl))
            else:
                # Use setup's own SL/TP
                param_variants = [(setup_name, signal["sl_price"], signal["tp_price"], 0, 0)]
            
            for variant_name, sl_price, tp_price, rr_mult, sl_mult in param_variants:
                entry_price = base_entry
                
                # Bug 1: SL/TP Direction Validation (CRITICAL)
                if direction == "buy":
                    if tp_price <= entry_price or sl_price >= entry_price:
                        continue
                else:
                    if tp_price >= entry_price or sl_price <= entry_price:
                        continue
                
                # Check if this variant already has an active position
                if variant_name not in active_positions:
                    active_positions[variant_name] = []
                if variant_name not in loss_streaks:
                    loss_streaks[variant_name] = 0
                    max_loss_streaks[variant_name] = 0
                if len(active_positions[variant_name]) >= MAX_POSITIONS_PER_SETUP:
                    continue
                
                # Bug 3: Apply spread at entry
                spread = SPREADS.get(pair, 0.00015)
                if direction == "buy":
                    entry_price += spread / 2
                else:
                    entry_price -= spread / 2
                
                # Create position
                pos = Position(
                    setup_name=variant_name,
                    direction=direction,
                    entry_price=entry_price,
                    entry_time=current_time,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    entry_index=i,
                    confidence=signal.get("confidence", 5.0),
                    trigger_reason=signal.get("trigger_reason", "") + f" [rr={rr_mult},sl={sl_mult}]"
                )
                
                # Feature 3: Calculate TP1 for partial exits (1:1 Risk:Reward)
                risk = abs(entry_price - sl_price)
                if direction == "buy":
                    pos.tp1_price = entry_price + risk
                else:
                    pos.tp1_price = entry_price - risk
                
                # Feature 6: Add current loss streak to position
                pos.loss_streak_at_entry = loss_streaks.get(variant_name, 0)
                
                active_positions[variant_name].append(pos)
    
    # === CLOSE REMAINING POSITIONS AT END ===
    final_time = df.iloc[-1]["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
    final_price = df.iloc[-1]["close"]
    final_regime = detect_regime(df, len(df) - 1)
    
    for setup_name in active_positions:
        for pos in active_positions[setup_name]:
            pos.exit_time = final_time
            pos.exit_price = final_price
            pos.candles_to_exit = len(df) - 1 - pos.entry_index
            pos.exit_reason = "end_of_data"
            
            if pos.direction == "buy":
                pos.pips = (final_price - pos.entry_price) * pip_multiplier
            else:
                pos.pips = (pos.entry_price - final_price) * pip_multiplier
                
            pos.result = "win" if pos.pips > 0 else "loss"
            
            trade_record = create_trade_record(df, len(df) - 1, pos, pair, timeframe, final_regime, h4_data, max_loss_streaks)
            
            # Bug 9: Pip Calculation Sanity Check
            if trade_record["result"] == "win" and trade_record["pips"] < 0:
                trade_record["result"] = "loss"
                logger.debug(f"Fixed end-of-data trade {trade_record['trade_id']}: win with negative pips -> loss")
            elif trade_record["result"] == "loss" and trade_record["pips"] > 0:
                trade_record["result"] = "win"
                logger.debug(f"Fixed end-of-data trade {trade_record['trade_id']}: loss with positive pips -> win")
            
            all_trades.append(trade_record)
    
    print()  # New line after progress
    return all_trades


def check_position_exit(df: pd.DataFrame, current_idx: int, pos: Position, pip_multiplier: float) -> dict:
    """Check if position should be closed based on SL/TP or max hold time."""
    
    current = df.iloc[current_idx]
    high = current["high"]
    low = current["low"] 
    close = current["close"]
    
    # Track max favorable/adverse excursion
    if pos.direction == "buy":
        favorable = (high - pos.entry_price) * pip_multiplier
        adverse = (pos.entry_price - low) * pip_multiplier
    else:
        favorable = (pos.entry_price - low) * pip_multiplier
        adverse = (high - pos.entry_price) * pip_multiplier
        
    pos.max_favorable_pips = max(pos.max_favorable_pips, favorable)
    pos.max_adverse_pips = max(pos.max_adverse_pips, adverse)
    
    # Feature 2: Trailing Stop (Move SL to Breakeven)
    if not pos.be_triggered:
        risk = abs(pos.entry_price - pos.original_sl_price) * pip_multiplier
        if pos.max_favorable_pips >= 1.0 * risk:  # 1x risk in our favor
            pos.be_triggered = True
            pos.be_candle = current_idx
            pos.sl_price = pos.entry_price  # Move SL to breakeven
    
    # Feature 3: Partial Exit Simulation - Check for TP1 hit
    if not pos.partial_exit_hit and pos.tp1_price:
        tp1_hit = False
        if pos.direction == "buy" and high >= pos.tp1_price:
            tp1_hit = True
        elif pos.direction == "sell" and low <= pos.tp1_price:
            tp1_hit = True
            
        if tp1_hit:
            pos.partial_exit_hit = True
            pos.partial_exit_candle = current_idx
            pos.partial_exit_pips = abs(pos.entry_price - pos.original_sl_price) * pip_multiplier  # 1R pips
            
            # For second half, move SL to breakeven
            pos.sl_price = pos.entry_price
    
    # Bug 2: Check "both SL and TP hit in same candle" FIRST
    if pos.direction == "buy":
        if low <= pos.sl_price and high >= pos.tp_price:
            second_half_pips = (pos.sl_price - pos.entry_price) * pip_multiplier
            if pos.partial_exit_hit:
                pos.second_half_pips = second_half_pips
                pos.second_half_result = "loss"
                combined_pips = (pos.partial_exit_pips + second_half_pips) / 2
            else:
                combined_pips = second_half_pips
            return {
                "exit_price": pos.sl_price,
                "result": "win" if combined_pips > 0 else "loss",
                "pips": combined_pips,
                "reason": "both_hit_sl_first"
            }
    else:
        if high >= pos.sl_price and low <= pos.tp_price:
            second_half_pips = (pos.entry_price - pos.sl_price) * pip_multiplier
            if pos.partial_exit_hit:
                pos.second_half_pips = second_half_pips
                pos.second_half_result = "loss"
                combined_pips = (pos.partial_exit_pips + second_half_pips) / 2
            else:
                combined_pips = second_half_pips
            return {
                "exit_price": pos.sl_price,
                "result": "win" if combined_pips > 0 else "loss",
                "pips": combined_pips,
                "reason": "both_hit_sl_first"
            }
    
    # Stop loss hit
    if pos.direction == "buy" and low <= pos.sl_price:
        second_half_pips = (pos.sl_price - pos.entry_price) * pip_multiplier
        if pos.partial_exit_hit:
            pos.second_half_pips = second_half_pips
            pos.second_half_result = "breakeven" if pos.be_triggered and abs(second_half_pips) < 2 else "loss"
            combined_pips = (pos.partial_exit_pips + second_half_pips) / 2
        else:
            combined_pips = second_half_pips
        result = "breakeven" if pos.be_triggered and abs(combined_pips) < 2 else ("win" if combined_pips > 0 else "loss")
        return {
            "exit_price": pos.sl_price,
            "result": result,
            "pips": combined_pips,
            "reason": "stop_loss" if result == "loss" else "breakeven_sl"
        }
        
    if pos.direction == "sell" and high >= pos.sl_price:
        second_half_pips = (pos.entry_price - pos.sl_price) * pip_multiplier
        if pos.partial_exit_hit:
            pos.second_half_pips = second_half_pips
            pos.second_half_result = "breakeven" if pos.be_triggered and abs(second_half_pips) < 2 else "loss"
            combined_pips = (pos.partial_exit_pips + second_half_pips) / 2
        else:
            combined_pips = second_half_pips
        result = "breakeven" if pos.be_triggered and abs(combined_pips) < 2 else ("win" if combined_pips > 0 else "loss")
        return {
            "exit_price": pos.sl_price,
            "result": result, 
            "pips": combined_pips,
            "reason": "stop_loss" if result == "loss" else "breakeven_sl"
        }
    
    # Take profit hit
    if pos.direction == "buy" and high >= pos.tp_price:
        second_half_pips = (pos.tp_price - pos.entry_price) * pip_multiplier
        
        # Feature 3: Calculate combined pips if partial exit occurred
        if pos.partial_exit_hit:
            pos.second_half_pips = second_half_pips
            pos.second_half_result = "win"
            combined_pips = (pos.partial_exit_pips + second_half_pips) / 2
        else:
            combined_pips = second_half_pips
            
        return {
            "exit_price": pos.tp_price,
            "result": "win",
            "pips": combined_pips,
            "reason": "take_profit"
        }
        
    if pos.direction == "sell" and low <= pos.tp_price:
        second_half_pips = (pos.entry_price - pos.tp_price) * pip_multiplier
        
        # Feature 3: Calculate combined pips if partial exit occurred
        if pos.partial_exit_hit:
            pos.second_half_pips = second_half_pips
            pos.second_half_result = "win"
            combined_pips = (pos.partial_exit_pips + second_half_pips) / 2
        else:
            combined_pips = second_half_pips
            
        return {
            "exit_price": pos.tp_price,
            "result": "win",
            "pips": combined_pips,
            "reason": "take_profit"
        }
    
    # Max hold time
    if current_idx - pos.entry_index >= MAX_HOLD_CANDLES:
        if pos.direction == "buy":
            second_half_pips = (close - pos.entry_price) * pip_multiplier
        else:
            second_half_pips = (pos.entry_price - close) * pip_multiplier
            
        # Feature 3: Calculate combined pips if partial exit occurred
        if pos.partial_exit_hit:
            pos.second_half_pips = second_half_pips
            pos.second_half_result = "win" if second_half_pips > 0 else ("breakeven" if abs(second_half_pips) < 2 else "loss")
            combined_pips = (pos.partial_exit_pips + second_half_pips) / 2
        else:
            combined_pips = second_half_pips
            
        return {
            "exit_price": close,
            "result": "win" if combined_pips > 0 else ("breakeven" if abs(combined_pips) < 2 else "loss"),
            "pips": combined_pips,
            "reason": "max_hold_time"
        }
    
    return None


def create_trade_record(df: pd.DataFrame, exit_idx: int, pos: Position, pair: str, timeframe: str, regime: str, h4_data: pd.DataFrame = None, max_loss_streaks: dict = None) -> dict:
    """Create comprehensive trade record with all required fields."""
    
    entry_row = df.iloc[pos.entry_index]
    
    # Get all indicator values at entry
    indicators_at_entry = {
        "adx": entry_row.get("adx", 0),
        "adx_slope": 0,  # Would need calculation
        "rsi": entry_row.get("rsi", 0),
        "macd_value": entry_row.get("macd_line", 0),
        "macd_signal": entry_row.get("macd_signal", 0),
        "macd_hist": entry_row.get("macd_histogram", 0),
        "stoch_k": entry_row.get("stoch_k", 0),
        "stoch_d": entry_row.get("stoch_d", 0),
        "cci": entry_row.get("cci", 0),
        "bb_upper": entry_row.get("bb_upper", 0),
        "bb_mid": entry_row.get("bb_middle", 0),
        "bb_lower": entry_row.get("bb_lower", 0),
        "bb_width": entry_row.get("bb_width", 0),
        "sma50": entry_row.get("sma_50", 0),
        "sma100": entry_row.get("sma_100", 0),
        "atr": entry_row.get("atr", 0),
        "sar": entry_row.get("parabolic_sar", 0),
    }
    
    # Calculate ADX slope
    if pos.entry_index >= 5:
        adx_prev = df.iloc[pos.entry_index - 5].get("adx", 0)
        indicators_at_entry["adx_slope"] = indicators_at_entry["adx"] - adx_prev
    
    # Price vs SMAs
    price_vs_sma50 = "above" if entry_row["close"] > indicators_at_entry["sma50"] else "below"
    price_vs_sma100 = "above" if entry_row["close"] > indicators_at_entry["sma100"] else "below"
    
    # Entry candle pattern
    entry_candle_pattern = "none"
    for pattern in ["hammer", "shooting_star", "doji", "bullish_engulfing", "bearish_engulfing"]:
        if entry_row.get(pattern, False):
            entry_candle_pattern = pattern
            break
    
    # Previous 3 candle patterns (simplified)
    prev_patterns = []
    for j in range(max(0, pos.entry_index - 3), pos.entry_index):
        if j < len(df):
            for pattern in ["hammer", "shooting_star", "doji"]:
                if df.iloc[j].get(pattern, False):
                    prev_patterns.append(pattern)
                    break
            else:
                prev_patterns.append("none")
    
    # Support/Resistance levels (simplified)
    lookback = min(50, pos.entry_index)
    if lookback > 0:
        recent_data = df.iloc[pos.entry_index - lookback:pos.entry_index]
        nearest_support = recent_data["low"].min()
        nearest_resistance = recent_data["high"].max()
    else:
        nearest_support = entry_row["low"]
        nearest_resistance = entry_row["high"]
    
    # Pivot points (simplified daily calculation)
    prev_high = df.iloc[max(0, pos.entry_index - 24):pos.entry_index]["high"].max()
    prev_low = df.iloc[max(0, pos.entry_index - 24):pos.entry_index]["low"].min()
    prev_close = df.iloc[pos.entry_index - 1]["close"] if pos.entry_index > 0 else entry_row["close"]
    
    pivot_pp = (prev_high + prev_low + prev_close) / 3
    pivot_r1 = 2 * pivot_pp - prev_low
    pivot_s1 = 2 * pivot_pp - prev_high
    
    # Feature 1: H4 Multi-timeframe analysis
    h4_trend = "neutral"
    h4_agrees = False
    h4_info = ""
    
    if timeframe == "H4":
        # For H4 timeframe testing itself, use Daily concepts (SMA slope over last 20 candles)
        if pos.entry_index >= 20:
            sma50_20ago = df.iloc[pos.entry_index - 20].get("sma_50", 0)
            current_sma50 = indicators_at_entry["sma50"]
            
            if pd.notna(sma50_20ago) and pd.notna(current_sma50) and sma50_20ago > 0:
                sma_slope = (current_sma50 - sma50_20ago) / sma50_20ago * 100  # Percentage change
                
                if sma_slope > 0.5:  # Rising trend  
                    h4_trend = "bullish"
                    h4_agrees = pos.direction == "buy"
                    h4_info = f"Daily_SMA50_slope_{sma_slope:.2f}%"
                elif sma_slope < -0.5:  # Falling trend
                    h4_trend = "bearish"  
                    h4_agrees = pos.direction == "sell"
                    h4_info = f"Daily_SMA50_slope_{sma_slope:.2f}%"
                else:
                    h4_trend = "neutral"
                    h4_agrees = False
                    h4_info = f"Daily_SMA50_flat_{sma_slope:.2f}%"
    elif h4_data is not None and not h4_data.empty:
        entry_timestamp = pd.to_datetime(pos.entry_time)
        h4_trend, h4_trend_valid, h4_info = get_h4_trend(h4_data, entry_timestamp)
        
        if h4_trend_valid:
            if pos.direction == "buy" and h4_trend == "bullish":
                h4_agrees = True
            elif pos.direction == "sell" and h4_trend == "bearish":
                h4_agrees = True
    
    # Feature 4: Trading Session
    session = get_trading_session(pos.entry_time)
    
    # Feature 5: Daily Pivot Analysis
    daily_pivots = get_daily_pivots(df, pos.entry_index)
    pivot_info = get_nearest_pivot_info(pos.entry_price, daily_pivots, indicators_at_entry["atr"])
    
    return {
        "trade_id": pos.trade_id,
        "pair": pair,
        "timeframe": timeframe,
        "setup": pos.setup_name,
        "base_setup": pos.setup_name.split("_rr")[0] if "_rr" in pos.setup_name else pos.setup_name,
        "rr_mult": float(pos.setup_name.split("_rr")[1].split("_sl")[0]) if "_rr" in pos.setup_name else 0,
        "sl_mult": float(pos.setup_name.split("_sl")[1]) if "_sl" in pos.setup_name else 0,
        "direction": pos.direction,
        "entry_time": pos.entry_time,
        "exit_time": pos.exit_time,
        "entry_price": pos.entry_price,
        "exit_price": pos.exit_price,
        "sl_price": pos.sl_price,
        "tp_price": pos.tp_price,
        "result": pos.result,
        "pips": round(pos.pips, 1),
        "risk_reward_actual": round(pos.risk_reward_actual, 2),
        "regime": regime,
        
        # Feature 1: H4 Multi-timeframe
        "h4_trend": h4_trend,
        "h4_agrees": h4_agrees,
        "h4_info": h4_info,
        
        # Feature 2: Trailing Stop (Breakeven)  
        "sl_moved_to_be": pos.be_triggered,
        "be_candle": pos.be_candle if pos.be_triggered else None,
        
        # Feature 3: Partial Exit Simulation
        "partial_exit_hit": pos.partial_exit_hit,
        "partial_exit_pips": round(pos.partial_exit_pips, 1) if pos.partial_exit_hit else 0.0,
        "second_half_result": pos.second_half_result if pos.partial_exit_hit else "n/a",
        "second_half_pips": round(pos.second_half_pips, 1) if pos.partial_exit_hit else 0.0,
        "combined_pips": round(pos.pips, 1),  # This is already the combined pips from exit logic
        
        # Feature 4: Session Time
        "session": session,
        
        # Feature 5: Daily Pivot Levels
        **pivot_info,
        
        # Feature 6: Loss Streak Tracking
        "loss_streak_at_entry": getattr(pos, 'loss_streak_at_entry', 0),
        "max_loss_streak": max_loss_streaks.get(pos.setup_name, 0) if max_loss_streaks else 0,
        
        # Existing fields
        **indicators_at_entry,
        "price_vs_sma50": price_vs_sma50,
        "price_vs_sma100": price_vs_sma100,
        "entry_candle_pattern": entry_candle_pattern,
        "prev_3_candle_patterns": prev_patterns,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "pivot_pp": pivot_pp,
        "pivot_r1": pivot_r1,
        "pivot_s1": pivot_s1,
        "max_favorable_pips": round(pos.max_favorable_pips, 1),
        "max_adverse_pips": round(pos.max_adverse_pips, 1),
        "candles_to_exit": pos.candles_to_exit,
        "trigger_reason": pos.trigger_reason,
        "confidence": pos.confidence,
        "exit_reason": pos.exit_reason,
    }


# ============================================================================
# CONCURRENT SETUP DETECTION
# ============================================================================

def add_concurrent_setup_info(all_trades: list) -> list:
    """Add concurrent setup information to all trades."""
    
    # Group trades by pair/timeframe/entry_time
    time_groups = {}
    for trade in all_trades:
        key = (trade["pair"], trade["timeframe"], trade["entry_time"])
        if key not in time_groups:
            time_groups[key] = []
        time_groups[key].append(trade)
    
    # Add concurrent info
    for trades_at_same_time in time_groups.values():
        if len(trades_at_same_time) > 1:
            setups = [t["setup"] for t in trades_at_same_time]
            directions = [t["direction"] for t in trades_at_same_time]
            
            for trade in trades_at_same_time:
                trade["concurrent_setups"] = [s for s in setups if s != trade["setup"]]
                trade["concurrent_directions"] = [d for i, d in enumerate(directions) if setups[i] != trade["setup"]]
        else:
            trades_at_same_time[0]["concurrent_setups"] = []
            trades_at_same_time[0]["concurrent_directions"] = []
    
    return all_trades


# ============================================================================
# ANALYSIS AND OUTPUT
# ============================================================================

def generate_setup_summary(all_trades: list) -> list:
    """Generate per-setup summary statistics."""
    summary = []
    
    # Group by setup, pair, timeframe, regime
    groups = {}
    for trade in all_trades:
        key = (trade["setup"], trade["pair"], trade["timeframe"], trade["regime"])
        if key not in groups:
            groups[key] = []
        groups[key].append(trade)
    
    for (setup, pair, timeframe, regime), trades in groups.items():
        if not trades:
            continue
            
        wins = [t for t in trades if t["result"] == "win"]
        total_pips = sum(t["pips"] for t in trades)
        
        win_pips = sum(t["pips"] for t in wins) if wins else 0
        loss_pips = abs(sum(t["pips"] for t in trades if t["result"] == "loss"))
        
        summary.append({
            "setup": setup,
            "pair": pair,
            "timeframe": timeframe,
            "regime": regime,
            "trade_count": len(trades),
            "win_count": len(wins),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "total_pips": round(total_pips, 1),
            "avg_pips": round(total_pips / len(trades), 1) if trades else 0,
            "profit_factor": round(win_pips / max(loss_pips, 0.1), 2),
            "avg_risk_reward": round(np.mean([t["risk_reward_actual"] for t in wins]), 2) if wins else 0,
            "max_favorable": round(np.mean([t["max_favorable_pips"] for t in trades]), 1),
            "max_adverse": round(np.mean([t["max_adverse_pips"] for t in trades]), 1),
            "avg_hold_time": round(np.mean([t["candles_to_exit"] for t in trades]), 1),
        })
    
    return summary


def generate_confluence_summary(all_trades: list) -> list:
    """Generate confluence analysis."""
    confluence_summary = []
    
    # Find trades with concurrent setups
    concurrent_trades = [t for t in all_trades if t["concurrent_setups"]]
    
    # Group by confluence combinations
    confluence_groups = {}
    for trade in concurrent_trades:
        # Sort setups to create consistent key
        all_setups = sorted([trade["setup"]] + trade["concurrent_setups"])
        key = "+".join(all_setups)
        
        if key not in confluence_groups:
            confluence_groups[key] = []
        confluence_groups[key].append(trade)
    
    for combo, trades in confluence_groups.items():
        if len(trades) < 3:  # Skip rare combinations
            continue
            
        wins = [t for t in trades if t["result"] == "win"]
        total_pips = sum(t["pips"] for t in trades)
        win_pips = sum(t["pips"] for t in wins) if wins else 0
        loss_pips = abs(sum(t["pips"] for t in trades if t["result"] == "loss"))
        
        confluence_summary.append({
            "combo": combo,
            "trade_count": len(trades),
            "win_count": len(wins),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_pips": round(total_pips, 1),
            "profit_factor": round(win_pips / max(loss_pips, 0.1), 2),
            "avg_pips": round(total_pips / len(trades), 1),
        })
    
    return sorted(confluence_summary, key=lambda x: x["profit_factor"], reverse=True)


def generate_regime_summary(all_trades: list) -> list:
    """Generate performance by regime."""
    regime_summary = []
    
    # Group by regime
    regime_groups = {}
    for trade in all_trades:
        regime = trade["regime"]
        if regime not in regime_groups:
            regime_groups[regime] = []
        regime_groups[regime].append(trade)
    
    for regime, trades in regime_groups.items():
        if not trades:
            continue
            
        # Overall regime stats
        wins = [t for t in trades if t["result"] == "win"]
        total_pips = sum(t["pips"] for t in trades)
        
        # Best setups in this regime
        setup_performance = {}
        for trade in trades:
            setup = trade["setup"]
            if setup not in setup_performance:
                setup_performance[setup] = []
            setup_performance[setup].append(trade)
        
        best_setups = []
        for setup, setup_trades in setup_performance.items():
            if len(setup_trades) >= 5:  # Minimum sample
                setup_wins = len([t for t in setup_trades if t["result"] == "win"])
                setup_wr = setup_wins / len(setup_trades) * 100
                setup_pips = sum(t["pips"] for t in setup_trades)
                
                if setup_wr > 50 and setup_pips > 0:
                    best_setups.append(f"{setup}({setup_wr:.0f}%/{len(setup_trades)}t)")
        
        regime_summary.append({
            "regime": regime,
            "trade_count": len(trades),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "total_pips": round(total_pips, 1),
            "avg_pips": round(total_pips / len(trades), 1) if trades else 0,
            "best_setups": ", ".join(best_setups[:5])  # Top 5
        })
    
    return regime_summary


def generate_session_summary(all_trades: list) -> list:
    """Generate performance by trading session."""
    session_summary = []
    
    # Group by session
    session_groups = {}
    for trade in all_trades:
        session = trade.get("session", "Unknown")
        if session not in session_groups:
            session_groups[session] = {}
        
        # Group by setup within session
        setup = trade["setup"]
        regime = trade["regime"]
        key = (setup, regime)
        
        if key not in session_groups[session]:
            session_groups[session][key] = []
        session_groups[session][key].append(trade)
    
    # Generate summary for each session-setup-regime combination
    for session, setup_groups in session_groups.items():
        for (setup, regime), trades in setup_groups.items():
            if len(trades) < 3:  # Minimum sample size
                continue
                
            wins = [t for t in trades if t["result"] == "win"]
            total_pips = sum(t["pips"] for t in trades)
            win_pips = sum(t["pips"] for t in wins) if wins else 0
            loss_pips = abs(sum(t["pips"] for t in trades if t["result"] == "loss"))
            
            session_summary.append({
                "session": session,
                "setup": setup,
                "regime": regime,
                "trade_count": len(trades),
                "win_count": len(wins),
                "win_rate": round(len(wins) / len(trades) * 100, 1),
                "profit_factor": round(win_pips / max(loss_pips, 0.1), 2),
                "total_pips": round(total_pips, 1)
            })
    
    return sorted(session_summary, key=lambda x: x["profit_factor"], reverse=True)


def generate_streak_analysis(all_trades: list) -> list:
    """Generate loss streak analysis."""
    streak_analysis = []
    
    # Group by setup and pair/timeframe
    setup_groups = {}
    for trade in all_trades:
        key = (trade["setup"], trade["pair"], trade["timeframe"])
        if key not in setup_groups:
            setup_groups[key] = []
        setup_groups[key].append(trade)
    
    for (setup, pair, timeframe), trades in setup_groups.items():
        if len(trades) < 10:  # Need sufficient sample
            continue
            
        # Find max streak for this setup/pair/timeframe
        max_streak = max(t.get("max_loss_streak", 0) for t in trades)
        
        # Find trades after 3+ loss streak
        trades_after_3plus_streak = [t for t in trades if t.get("loss_streak_at_entry", 0) >= 3]
        
        if trades_after_3plus_streak:
            avg_pips_after_streak = sum(t["pips"] for t in trades_after_3plus_streak) / len(trades_after_3plus_streak)
        else:
            avg_pips_after_streak = 0.0
            
        streak_analysis.append({
            "setup": setup,
            "pair": pair,
            "timeframe": timeframe,
            "max_streak": max_streak,
            "trades_after_3plus_streak": len(trades_after_3plus_streak),
            "avg_pips_after_streak_3plus": round(avg_pips_after_streak, 1)
        })
    
    return sorted(streak_analysis, key=lambda x: x["max_streak"], reverse=True)


def generate_loss_analysis(all_trades: list) -> list:
    """Analyze common patterns in losing trades."""
    losing_trades = [t for t in all_trades if t["result"] == "loss"]
    
    if not losing_trades:
        return []
    
    analysis = []
    
    # Group by setup
    setup_losses = {}
    for trade in losing_trades:
        setup = trade["setup"]
        if setup not in setup_losses:
            setup_losses[setup] = []
        setup_losses[setup].append(trade)
    
    for setup, losses in setup_losses.items():
        if len(losses) < 10:  # Need reasonable sample
            continue
        
        # Common indicators at loss entries
        avg_rsi = np.mean([t["rsi"] for t in losses])
        avg_adx = np.mean([t["adx"] for t in losses])
        avg_stoch = np.mean([t["stoch_k"] for t in losses])
        
        # Exit reasons
        exit_reasons = {}
        for trade in losses:
            reason = trade.get("exit_reason", "unknown")
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        
        most_common_exit = max(exit_reasons, key=exit_reasons.get)
        
        # Regimes where losses occur
        regime_count = {}
        for trade in losses:
            regime = trade["regime"]
            regime_count[regime] = regime_count.get(regime, 0) + 1
        
        worst_regime = max(regime_count, key=regime_count.get) if regime_count else "unknown"
        
        analysis.append({
            "setup": setup,
            "loss_count": len(losses),
            "avg_loss_pips": round(np.mean([abs(t["pips"]) for t in losses]), 1),
            "avg_rsi": round(avg_rsi, 1),
            "avg_adx": round(avg_adx, 1),
            "avg_stoch": round(avg_stoch, 1),
            "common_exit": most_common_exit,
            "exit_count": exit_reasons[most_common_exit],
            "worst_regime": worst_regime,
            "regime_count": regime_count[worst_regime],
        })
    
    return sorted(analysis, key=lambda x: x["loss_count"], reverse=True)


def save_results(all_trades: list, setup_summary: list, confluence_summary: list, 
                regime_summary: list, loss_analysis: list, session_summary: list, streak_analysis: list):
    """Save all results to files."""
    
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    
    # 1. All trades JSON
    json_path = RESULTS_DIR / "v3_all_trades.json"
    with open(json_path, 'w') as f:
        json.dump({
            "trades": all_trades,
            "summary": {
                "total_trades": len(all_trades),
                "winners": len([t for t in all_trades if t["result"] == "win"]),
                "total_pips": round(sum(t["pips"] for t in all_trades), 1),
                "generated": datetime.now(timezone.utc).isoformat(),
            }
        }, f, indent=2, default=str)
    
    # 2. All trades CSV
    csv_path = RESULTS_DIR / "v3_all_trades.csv"
    if all_trades:
        # Flatten lists in concurrent_setups and concurrent_directions
        csv_trades = []
        for trade in all_trades:
            csv_trade = trade.copy()
            csv_trade["concurrent_setups"] = "|".join(trade.get("concurrent_setups", []))
            csv_trade["concurrent_directions"] = "|".join(trade.get("concurrent_directions", []))
            csv_trade["prev_3_candle_patterns"] = "|".join(trade.get("prev_3_candle_patterns", []))
            csv_trades.append(csv_trade)
            
        df_trades = pd.DataFrame(csv_trades)
        df_trades.to_csv(csv_path, index=False)
    
    # 3. Setup summary CSV
    summary_path = RESULTS_DIR / "v3_setup_summary.csv"
    if setup_summary:
        pd.DataFrame(setup_summary).to_csv(summary_path, index=False)
    
    # 4. Confluence summary CSV
    confluence_path = RESULTS_DIR / "v3_confluence_summary.csv"
    if confluence_summary:
        pd.DataFrame(confluence_summary).to_csv(confluence_path, index=False)
    
    # 5. Regime summary CSV
    regime_path = RESULTS_DIR / "v3_regime_summary.csv"  
    if regime_summary:
        pd.DataFrame(regime_summary).to_csv(regime_path, index=False)
    
    # 6. Loss analysis CSV
    loss_path = RESULTS_DIR / "v3_loss_analysis.csv"
    if loss_analysis:
        pd.DataFrame(loss_analysis).to_csv(loss_path, index=False)
    
    # 7. Session summary CSV (Feature 4)
    session_path = RESULTS_DIR / "v3_session_summary.csv"
    if session_summary:
        pd.DataFrame(session_summary).to_csv(session_path, index=False)
    
    # 8. Streak analysis CSV (Feature 6)
    streak_path = RESULTS_DIR / "v3_streak_analysis.csv" 
    if streak_analysis:
        pd.DataFrame(streak_analysis).to_csv(streak_path, index=False)
    
    return {
        "json": json_path,
        "csv": csv_path,
        "summary": summary_path,
        "confluence": confluence_path,
        "regime": regime_path,
        "loss": loss_path,
        "session": session_path,
        "streak": streak_path,
    }


# ============================================================================
# MAIN EXECUTION
# ============================================================================

def get_data_path(pair: str, timeframe: str) -> Path:
    """Get path to data file."""
    return DATA_DIR / f"{pair.lower()}_{timeframe.lower()}_3yr.csv"


def main():
    parser = argparse.ArgumentParser(description="Master Sweep V3 - Comprehensive 20-Setup Analysis")
    parser.add_argument("--no-fetch", action="store_true", help="Use cached data only")
    parser.add_argument("--pairs", type=str, help="Comma-separated pairs (e.g., EUR_USD,GBP_USD)")
    parser.add_argument("--timeframes", type=str, help="Comma-separated timeframes (e.g., H1,H4)")
    parser.add_argument("--setups", type=str, help="Comma-separated setups (e.g., S11,S13)")
    parser.add_argument("--quick", action="store_true", help="Run on single pair/timeframe for testing")
    args = parser.parse_args()

    # Parse arguments
    pairs = args.pairs.split(",") if args.pairs else (["EUR_USD"] if args.quick else ALL_PAIRS)
    timeframes = args.timeframes.split(",") if args.timeframes else (["H1"] if args.quick else ALL_TIMEFRAMES)  
    setups = args.setups.split(",") if args.setups else ALL_SETUPS

    # Validate setups
    invalid_setups = [s for s in setups if s not in ALL_SETUPS]
    if invalid_setups:
        print(f"❌ Invalid setups: {invalid_setups}")
        print(f"Available: {ALL_SETUPS}")
        return

    # Find available data files
    available_combos = []
    for pair in pairs:
        for tf in timeframes:
            data_path = get_data_path(pair, tf)
            if data_path.exists():
                available_combos.append((pair, tf, data_path))
            elif not args.no_fetch:
                print(f"⚠️  Missing data file: {data_path}")

    if not available_combos:
        print("❌ No data files found!")
        return

    # Print configuration
    print("=" * 80)
    print("MASTER SWEEP V3 — Comprehensive Setup Analysis")
    print("=" * 80)
    print(f"📊 Pairs: {len(pairs)} ({', '.join(pairs)})")
    print(f"⏰ Timeframes: {len(timeframes)} ({', '.join(timeframes)})")
    print(f"🎯 Setups: {len(setups)} ({', '.join(setups)})")
    print(f"📁 Available data combinations: {len(available_combos)}")
    print(f"🧠 Warmup period: {WARMUP_PERIOD} candles")
    print(f"⏳ Max hold time: {MAX_HOLD_CANDLES} candles")
    print("=" * 80)

    start_time = time.time()
    all_trades = []
    
    # Process each pair/timeframe combination
    for combo_idx, (pair, timeframe, data_path) in enumerate(available_combos):
        print(f"\n[{combo_idx + 1}/{len(available_combos)}] Processing {pair} {timeframe}...")
        
        try:
            # Load and prepare data
            df = load_and_prepare_data(str(data_path))
            print(f"  📈 Loaded {len(df)} candles")
            
            # Run simulation
            pair_trades = simulate_trades(df, pair, timeframe, setups)
            print(f"  ✅ Generated {len(pair_trades)} trades")
            
            all_trades.extend(pair_trades)
            
            # Memory cleanup
            del df
            gc.collect()
            
        except Exception as e:
            print(f"  ❌ Failed: {e}")
            continue

    total_time = time.time() - start_time
    
    if not all_trades:
        print("\n❌ No trades generated!")
        return

    print(f"\n{'='*80}")
    print("ANALYSIS PHASE")
    print(f"{'='*80}")
    print(f"📊 Total trades generated: {len(all_trades)}")
    
    # Add concurrent setup information
    print("🔗 Adding concurrent setup information...")
    all_trades = add_concurrent_setup_info(all_trades)
    
    concurrent_count = len([t for t in all_trades if t["concurrent_setups"]])
    print(f"  ✅ {concurrent_count} trades have concurrent signals")
    
    # Generate summaries
    print("📋 Generating setup summary...")
    setup_summary = generate_setup_summary(all_trades)
    
    print("🎯 Generating confluence summary...")
    confluence_summary = generate_confluence_summary(all_trades)
    
    print("🌊 Generating regime summary...")
    regime_summary = generate_regime_summary(all_trades)
    
    print("📉 Generating loss analysis...")
    loss_analysis = generate_loss_analysis(all_trades)
    
    print("🕐 Generating session analysis...")
    session_summary = generate_session_summary(all_trades)
    
    print("📈 Generating streak analysis...")
    streak_analysis = generate_streak_analysis(all_trades)
    
    # Save results
    print("💾 Saving results...")
    file_paths = save_results(all_trades, setup_summary, confluence_summary, regime_summary, loss_analysis, session_summary, streak_analysis)
    
    # Display key insights
    print(f"\n{'='*80}")
    print("KEY INSIGHTS")
    print(f"{'='*80}")
    
    winners = [t for t in all_trades if t["result"] == "win"]
    total_pips = sum(t["pips"] for t in all_trades)
    
    print(f"📈 Overall Performance:")
    print(f"   Total Trades: {len(all_trades)}")
    print(f"   Winners: {len(winners)} ({len(winners)/len(all_trades)*100:.1f}%)")
    print(f"   Total Pips: {total_pips:.1f}")
    
    if winners:
        avg_win = np.mean([t["pips"] for t in winners])
        print(f"   Average Win: {avg_win:.1f} pips")
    
    losers = [t for t in all_trades if t["result"] == "loss"]
    if losers:
        avg_loss = np.mean([abs(t["pips"]) for t in losers])
        print(f"   Average Loss: {avg_loss:.1f} pips")
    
    # Best setup
    if setup_summary:
        best_setup = max([s for s in setup_summary if s["trade_count"] >= 10], 
                        key=lambda x: x["profit_factor"], default=None)
        if best_setup:
            print(f"\n🏆 Best Setup (≥10 trades):")
            print(f"   {best_setup['setup']}: {best_setup['win_rate']}% win rate, "
                  f"PF={best_setup['profit_factor']}, {best_setup['total_pips']} pips "
                  f"({best_setup['pair']}/{best_setup['timeframe']}/{best_setup['regime']})")
    
    # Best regime
    if regime_summary:
        best_regime = max(regime_summary, key=lambda x: x["total_pips"])
        print(f"\n🌊 Best Regime:")
        print(f"   {best_regime['regime']}: {best_regime['win_rate']}% win rate, "
              f"{best_regime['total_pips']} pips from {best_regime['trade_count']} trades")
        if best_regime['best_setups']:
            print(f"   Top setups: {best_regime['best_setups']}")
    
    # Top confluence
    if confluence_summary:
        top_confluence = confluence_summary[0]
        print(f"\n🎯 Best Confluence:")
        print(f"   {top_confluence['combo']}: {top_confluence['win_rate']}% win rate, "
              f"PF={top_confluence['profit_factor']}, {top_confluence['total_pips']} pips "
              f"from {top_confluence['trade_count']} trades")
    
    # NEW FEATURE INSIGHTS
    print(f"\n{'='*50} NEW FEATURES {'='*50}")
    
    # Feature 4: Best Session Timing
    if session_summary:
        session_totals = {}
        for s in session_summary:
            session = s['session']
            if session not in session_totals:
                session_totals[session] = {'trades': 0, 'pips': 0, 'wins': 0}
            session_totals[session]['trades'] += s['trade_count']
            session_totals[session]['pips'] += s['total_pips']
            session_totals[session]['wins'] += s['win_count']
        
        best_session = max(session_totals, key=lambda x: session_totals[x]['pips'])
        best_session_data = session_totals[best_session]
        best_wr = round(best_session_data['wins'] / max(best_session_data['trades'], 1) * 100, 1)
        
        print(f"🕐 Best Session Timing:")
        print(f"   {best_session}: {best_wr}% win rate, {best_session_data['pips']} pips "
              f"from {best_session_data['trades']} trades")
    
    # Feature 1: H4 Filter Impact
    h4_agrees_trades = [t for t in all_trades if t.get('h4_agrees', False)]
    h4_disagrees_trades = [t for t in all_trades if t.get('h4_agrees') == False and t.get('h4_trend') != 'neutral']
    
    if h4_agrees_trades and h4_disagrees_trades:
        h4_agree_wins = len([t for t in h4_agrees_trades if t['result'] == 'win'])
        h4_disagree_wins = len([t for t in h4_disagrees_trades if t['result'] == 'win'])
        h4_agree_wr = round(h4_agree_wins / len(h4_agrees_trades) * 100, 1)
        h4_disagree_wr = round(h4_disagree_wins / len(h4_disagrees_trades) * 100, 1)
        
        print(f"📊 H4 Filter Impact:")
        print(f"   H4 Agrees: {h4_agree_wr}% win rate from {len(h4_agrees_trades)} trades")  
        print(f"   H4 Disagrees: {h4_disagree_wr}% win rate from {len(h4_disagrees_trades)} trades")
        print(f"   Impact: {h4_agree_wr - h4_disagree_wr:.1f} percentage points better when H4 agrees")
    
    # Feature 2: Trailing Stop Impact
    be_moved_trades = [t for t in all_trades if t.get('sl_moved_to_be', False)]
    if be_moved_trades:
        be_saved_trades = [t for t in be_moved_trades if t.get('exit_reason') == 'breakeven_sl']
        print(f"🛡️  Trailing Stop Impact:")
        print(f"   {len(be_moved_trades)} trades had SL moved to breakeven")
        print(f"   {len(be_saved_trades)} trades saved by breakeven move (would have been losses)")
    
    print(f"\n{'='*80}")
    print(f"COMPLETE! Runtime: {total_time/60:.1f} minutes")
    print(f"{'='*80}")
    print("📁 Results saved to:")
    for desc, path in file_paths.items():
        print(f"   {desc}: {path}")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()