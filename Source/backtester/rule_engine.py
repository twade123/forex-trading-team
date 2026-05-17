#!/usr/bin/env python3
"""Rule engine: loads trading_rules.json and evaluates against indicator data."""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


def load_rules(path: str = None) -> dict:
    if path is None:
        path = Path(__file__).resolve().parent.parent.parent / "Knowledge" / "trading_rules.json"
    with open(path) as f:
        return json.load(f)


def detect_regime(row: dict) -> str:
    adx_val = row.get("adx", 0)
    if isinstance(adx_val, float) and np.isnan(adx_val):
        return "unknown"
    if adx_val >= 25:
        return "trending"
    elif adx_val <= 20:
        return "ranging"
    else:
        return "transitional"


def _check_condition(row: dict, condition: dict) -> bool:
    """Check a single condition against a row of indicator data.
    
    Uses broad keyword matching against condition strings from trading_rules.json.
    """
    indicator = condition.get("indicator", "").lower()
    cond = condition.get("condition", "").lower()

    try:
        close = row.get("close", 0)

        # === BOLLINGER BANDS ===
        if indicator == "bollinger_bands":
            bb_lower = row.get("bb_lower", 0)
            bb_upper = row.get("bb_upper", float("inf"))
            if "lower" in cond:
                return close <= bb_lower
            if "upper" in cond:
                return close >= bb_upper
            if "outer" in cond or "at_band" in cond:
                return close <= bb_lower or close >= bb_upper
            if "squeeze" in cond:
                return row.get("bb_width", 1) < 0.01

        # === RSI ===
        if indicator == "rsi":
            rsi_val = row.get("rsi", 50)
            if "extreme" in cond and "divergence" in cond:
                # Either extreme OR divergence
                return (rsi_val < 30 or rsi_val > 70 or
                        bool(row.get("rsi_bull_div", False)) or
                        bool(row.get("rsi_bear_div", False)))
            if "extreme" in cond:
                return rsi_val < 30 or rsi_val > 70
            if "divergence" in cond or "higher_low" in cond or "lower_high" in cond:
                return (bool(row.get("rsi_bull_div", False)) or
                        bool(row.get("rsi_bear_div", False)) or
                        bool(row.get("rsi_hidden_bull_div", False)) or
                        bool(row.get("rsi_hidden_bear_div", False)))
            if "higher_high" in cond:
                return bool(row.get("rsi_hidden_bear_div", False))
            if "lower_low" in cond:
                return bool(row.get("rsi_hidden_bull_div", False))

        # === MACD ===
        if indicator == "macd":
            hist = row.get("macd_histogram", 0)
            prev_hist = row.get("prev_macd_histogram", 0)
            bars_ago = row.get("macd_cross_bars_ago", 999)

            if "within" in cond and "5" in cond:
                return bars_ago <= 5
            if "older_than_5" in cond or "stale" in cond:
                return bars_ago > 5
            if "crosses_zero" in cond or "crossed_positive" in cond or "crossed_negative" in cond:
                return (hist > 0 and prev_hist <= 0) or (hist < 0 and prev_hist >= 0)
            if "confirm" in cond or "momentum" in cond:
                return abs(hist) > abs(prev_hist)  # Increasing momentum
            if "turns_bullish" in cond:
                return hist > 0 and prev_hist <= 0
            if "turns_bearish" in cond:
                return hist < 0 and prev_hist >= 0

        # === ADX ===
        if indicator == "adx":
            adx_val = row.get("adx", 0)
            if isinstance(adx_val, float) and np.isnan(adx_val):
                adx_val = 0
            if "> 25" in cond or "above_25" in cond:
                if "rising" in cond:
                    return adx_val > 25 and adx_val > row.get("prev_adx", 0)
                return adx_val > 25
            if "> 30" in cond:
                return adx_val > 30
            if "> 20" in cond:
                return adx_val > 20
            if "< 20" in cond:
                return adx_val < 20
            if "< 25" in cond:
                return adx_val < 25

        # === SMA ===
        if indicator == "sma":
            sma50 = row.get("sma_50", 0)
            sma100 = row.get("sma_100", 0)
            if "between" in cond:
                return min(sma50, sma100) < close < max(sma50, sma100)
            if "above_both" in cond or ("above" in cond and "50" in cond and "100" in cond):
                return close > sma50 and close > sma100
            if "below_both" in cond or ("below" in cond and "50" in cond and "100" in cond):
                return close < sma50 and close < sma100
            if "breaks" in cond and "10_pips" in cond:
                return abs(close - sma50) > 0.0010 or abs(close - sma100) > 0.0010

        # === EMA ===
        if indicator == "ema":
            ema50 = row.get("ema_50", 0)
            ema200 = row.get("ema_200", 0)
            prev_ema50 = row.get("prev_ema_50", ema50)
            if "above_200" in cond or "crosses_above" in cond:
                return ema50 > ema200
            if "below_200" in cond or "crosses_below" in cond:
                return ema50 < ema200
            if "trending" in cond or "direction" in cond:
                return abs(ema50 - ema200) / ema200 > 0.002  # 20 pip spread

        # === STOCHASTIC ===
        if indicator == "stochastic":
            k = row.get("stoch_k", 50)
            d = row.get("stoch_d", 50)
            prev_k = row.get("prev_stoch_k", 50)
            prev_d = row.get("prev_stoch_d", 50)
            if "extreme" in cond or "crossover_at_extreme" in cond:
                oversold_cross = k < 20 and k > d and prev_k <= prev_d
                overbought_cross = k > 80 and k < d and prev_k >= prev_d
                return oversold_cross or overbought_cross
            if "confirm" in cond:
                return (k > d and k < 30) or (k < d and k > 70)

        # === CCI ===
        if indicator == "cci":
            cci_val = row.get("cci", 0)
            if "overbought" in cond or "> 100" in cond:
                return cci_val > 100
            if "oversold" in cond or "< -100" in cond:
                return cci_val < -100
            return abs(cci_val) > 100

        # === FIBONACCI ===
        if indicator == "fibonacci":
            # Check if price is near common Fib levels based on recent swing
            sma50 = row.get("sma_50", close)
            sma200 = row.get("sma_200", close)
            swing_range = abs(sma50 - sma200)
            if swing_range < 0.001:
                return False
            if "breaks_61.8" in cond:
                fib_618 = max(sma50, sma200) - 0.618 * swing_range
                return abs(close - fib_618) < swing_range * 0.05
            # General retracement check
            for ratio in [0.382, 0.5, 0.618]:
                level = max(sma50, sma200) - ratio * swing_range
                if abs(close - level) < swing_range * 0.03:
                    return True
            return False

        # === VOLUME ===
        if indicator == "volume":
            vol = row.get("volume", 0)
            avg_vol = row.get("avg_volume", 1)
            if "spike" in cond or "above" in cond:
                return vol > avg_vol * 1.5
            if "below" in cond:
                return vol < avg_vol * 0.5

        # === PARABOLIC SAR ===
        if indicator == "parabolic_sar":
            sar = row.get("parabolic_sar", 0)
            if "flip" in cond or "dots" in cond:
                prev_sar_below = row.get("prev_parabolic_sar", 0) < row.get("prev_close", close)
                curr_sar_above = sar > close
                return prev_sar_below != (sar < close)  # Direction changed
            if "bullish" in cond or "below" in cond:
                return sar < close
            if "bearish" in cond or "above" in cond:
                return sar > close

        # === TIME/SESSION ===
        if indicator == "time":
            hour = row.get("hour", 12)
            if "london_ny" in cond or "overlap" in cond:
                return 8 <= hour <= 12
            if "outside" in cond:
                return hour < 8 or hour > 17
            if "remaining" in cond:
                return hour < 16

        # === VWAP ===
        if indicator == "vwap":
            vwap_val = row.get("vwap", close)
            return abs(close - vwap_val) / close < 0.001

        # === ADR ===
        if indicator == "adr":
            adr_val = row.get("adr", 0.01)
            daily_range = row.get("high", close) - row.get("low", close)
            if "80" in cond or "exhaust" in cond:
                return daily_range > adr_val * 0.8

        # === MOVING AVERAGE (generic) ===
        if indicator == "moving_average" or indicator == "trend_indicator":
            sma50 = row.get("sma_50", close)
            if "above" in cond:
                return close > sma50
            if "below" in cond:
                return close < sma50
            return abs(close - sma50) / close > 0.002

        # === PRICE ACTION ===
        if indicator == "price_action":
            if "consolidat" in cond or "sideways" in cond:
                return row.get("adx", 25) < 20
            if "higher_high" in cond:
                return close > row.get("prev_close", close)
            if "lower_low" in cond:
                return close < row.get("prev_close", close)
            if "higher_low" in cond:
                return row.get("low", close) > row.get("prev_low", 0)
            if "lower_high" in cond:
                return row.get("high", close) < row.get("prev_high", float("inf"))
            if "break" in cond:
                return abs(close - row.get("sma_50", close)) > 0.002

        # === DIVERGENCE (standalone) ===
        if indicator == "divergence":
            if "miss" in cond or "already" in cond:
                return False  # Can't detect "missed" in backtest

        # === CURRENCY CORRELATION (can't backtest without second pair) ===
        if indicator in ("gbp_usd", "usd_chf"):
            return False  # No data for second pair — skip this condition
        if indicator == "eur_usd":
            return True  # We have EUR/USD data

        # === ATR ===
        if indicator == "atr":
            return True  # ATR is always used for sizing, not entry signal

        # === GENERIC STRATEGY REFERENCES ===
        if indicator in ("any_strategy",):
            return True  # Meta-rule, always passes

        # Unknown indicator — log and skip
        if indicator and indicator != "?":
            pass  # Don't spam logs

    except (TypeError, ValueError, ZeroDivisionError):
        return False

    return False


def evaluate_entry_rules(row: dict, rules: dict, regime: str) -> List[dict]:
    """Evaluate all entry rules, return list of fired rules."""
    fired = []
    for rule in rules.get("rules", []):
        rule_regime = rule.get("regime", "all")
        if rule_regime != "all" and rule_regime != regime:
            continue

        conditions = rule.get("conditions", {})
        all_conditions = conditions.get("all", [])
        any_conditions = conditions.get("any", [])

        all_met = all(_check_condition(row, c) for c in all_conditions) if all_conditions else False
        any_met = any(_check_condition(row, c) for c in any_conditions) if any_conditions else True

        if all_met and any_met:
            direction = rule.get("direction", "neutral")
            signal = _resolve_direction(direction, row)
            fired.append({
                "rule_id": rule["id"],
                "name": rule["name"],
                "signal": signal,
                "confidence": rule.get("confidence", "medium"),
                "category": rule.get("category", "unknown"),
            })

    return fired


def _resolve_direction(direction: str, row: dict) -> str:
    """Convert rule direction to buy/sell based on current market state."""
    d = direction.lower()

    if d in ("bullish", "buy", "long"):
        return "buy"
    if d in ("bearish", "sell", "short"):
        return "sell"

    # Trend-following: follow the EMA direction
    if d in ("trend_following", "with_trend", "trend_continuation", "as_per_strategy", "as_per_eur_usd"):
        if row.get("ema_50", 0) > row.get("ema_200", 0):
            return "buy"
        return "sell"

    # Counter-trend: fade the current direction
    if d in ("contra_trend", "reversal", "counter_momentum"):
        rsi = row.get("rsi", 50)
        close = row.get("close", 0)
        bb_lower = row.get("bb_lower", close)
        bb_upper = row.get("bb_upper", close)
        # Use RSI + BB position to determine direction
        if rsi < 40 or close <= bb_lower:
            return "buy"   # Oversold → buy the reversal
        if rsi > 60 or close >= bb_upper:
            return "sell"  # Overbought → sell the reversal
        # Ambiguous — use SMA
        if close < row.get("sma_50", close):
            return "buy"
        return "sell"

    # Breakout: follow the breakout direction
    if d in ("with_breakout",):
        close = row.get("close", 0)
        if close > row.get("bb_upper", close):
            return "buy"
        if close < row.get("bb_lower", close):
            return "sell"
        if row.get("macd_histogram", 0) > 0:
            return "buy"
        return "sell"

    # VWAP-based
    if d in ("toward_vwap",):
        close = row.get("close", 0)
        vwap = row.get("vwap", close)
        return "buy" if close < vwap else "sell"

    # No trade signal
    if d in ("no_trade",):
        return "hold"

    # Fallback: use trend
    if row.get("ema_50", 0) > row.get("ema_200", 0):
        return "buy"
    return "sell"


def evaluate_skip_rules(row: dict, rules: dict) -> List[dict]:
    """Evaluate skip/no-trade rules. Only hard NO_TRADE rules actually block.
    
    SKIP_TREND_TRADES/SKIP_ENTRY are soft — they reduce confidence but don't block.
    """
    fired = []
    for rule in rules.get("skip_rules", []):
        action = rule.get("action", "NO_TRADE")

        # Only hard NO_TRADE rules block entry. Soft skips are informational.
        if action != "NO_TRADE":
            continue

        conditions = rule.get("conditions", {})

        if "all" in conditions:
            met = all(_check_condition(row, c) for c in conditions["all"])
        elif "indicator" in conditions:
            met = _check_condition(row, conditions)
        else:
            met = False

        if met:
            fired.append({
                "rule_id": rule["id"],
                "name": rule["name"],
                "action": action,
                "confidence": rule.get("confidence", "medium"),
            })

    return fired


def score_confluence(fired_rules: List[dict]) -> dict:
    """Compute confluence score from fired entry rules."""
    if not fired_rules:
        return {"score": 0, "direction": "hold", "buy_count": 0, "sell_count": 0}

    conf_weights = {"high": 15, "medium": 10, "low": 5, "medium_boost": 8}

    buy_score = 0
    sell_score = 0
    buy_count = 0
    sell_count = 0

    for rule in fired_rules:
        weight = conf_weights.get(rule["confidence"], 10)
        if rule["signal"] == "buy":
            buy_score += weight
            buy_count += 1
        elif rule["signal"] == "sell":
            sell_score += weight
            sell_count += 1

    if buy_score > sell_score:
        direction = "buy"
        score = buy_score
    elif sell_score > buy_score:
        direction = "sell"
        score = sell_score
    else:
        direction = "hold"
        score = 0

    return {
        "score": score,
        "direction": direction,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "rules_fired": [r["rule_id"] for r in fired_rules],
    }
