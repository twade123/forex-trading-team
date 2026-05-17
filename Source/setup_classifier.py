#!/usr/bin/env python3
"""Setup Classifier — identifies which S1-S20 setups match current conditions.

Derived from 55 pages of chart education and research. Each setup has:
- Exact indicator conditions required
- Regime requirements (when it works / fails)
- Direction (buy/sell/both)
- Confidence based on how many conditions align

Used by Scout (pre-identification) and TA agent (verification).
"""

import json
import logging
import os
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# Path to custom setups (auto-discovered S21+)
_CUSTOM_SETUPS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'custom_setups.json')
_custom_setups_cache = None
_custom_setups_mtime = 0

# Valid regimes (must match DB)
VALID_REGIMES = {'exhaustion', 'high_volatility', 'ranging', 'squeeze', 'strong_trend'}

# Regime → valid setups mapping (from complete_visual_knowledge_base.md)
REGIME_SETUP_MAP = {
    'strong_trend':    ['S3', 'S4', 'S5', 'S6', 'S7', 'S8', 'S11', 'S12', 'S16'],
    # NOTE: 'mixed' removed — doesn't exist in backtest DB. ADX 20-25 maps to 'ranging'.
    'ranging':         ['S1', 'S4', 'S5', 'S11', 'S13', 'S14', 'S15', 'S16', 'S17'],
    'exhaustion':      ['S9', 'S10', 'S15', 'S18'],
    'squeeze':         ['S12', 'S17'],
    'high_volatility': ['S3', 'S4', 'S16', 'S19'],
}


def _load_custom_setups() -> List[Dict]:
    """Load custom setups from JSON, with file-mtime caching for hot reload."""
    global _custom_setups_cache, _custom_setups_mtime
    try:
        if not os.path.exists(_CUSTOM_SETUPS_PATH):
            return []
        mtime = os.path.getmtime(_CUSTOM_SETUPS_PATH)
        if _custom_setups_cache is not None and mtime == _custom_setups_mtime:
            return _custom_setups_cache
        with open(_CUSTOM_SETUPS_PATH, 'r') as f:
            data = json.load(f)
        _custom_setups_cache = [s for s in data.get('setups', []) if s.get('status') == 'active']
        _custom_setups_mtime = mtime
        logger.debug("Loaded %d custom setups from %s", len(_custom_setups_cache), _CUSTOM_SETUPS_PATH)
        return _custom_setups_cache
    except Exception as e:
        logger.warning("Failed to load custom setups: %s", e)
        return _custom_setups_cache or []


def _evaluate_custom_setup(setup_def: Dict, indicators: Dict, close: float, bb_mid: float, bb_lower: float, bb_upper: float) -> bool:
    """Evaluate if current indicators match a custom setup's conditions."""
    conditions = setup_def.get('conditions', {})
    for key, spec in conditions.items():
        if key == 'bb_position':
            if spec == 'below_mid' and close >= bb_mid:
                return False
            elif spec == 'above_mid' and close <= bb_mid:
                return False
            elif spec == 'at_lower' and close > bb_lower:
                return False
            elif spec == 'at_upper' and close < bb_upper:
                return False
            continue
        if key == 'candle':
            continue  # Candle patterns checked separately
        val = indicators.get(key)
        if val is None:
            return False
        if isinstance(spec, dict):
            if 'min' in spec and val < spec['min']:
                return False
            if 'max' in spec and val > spec['max']:
                return False
        # string equality
        elif val != spec:
            return False
    return True


def classify_setups(
    indicators: Dict[str, Any],
    candle_patterns: Dict[str, bool],
    chart_patterns: List[Dict] = None,
    regime: str = 'ranging',
    fib_data: Dict = None,
    h4_bias: str = None,
) -> List[Dict]:
    """Classify which S1-S20 setups match current market conditions.
    
    Args:
        indicators: Dict with keys like rsi, stoch_k, stoch_d, adx, macd_value,
                    macd_signal, macd_hist, bb_upper, bb_lower, bb_mid, bb_width,
                    ema_21, ema_55, ema_100, sma50, sma100, sar, cci, atr, close
        candle_patterns: Dict from detect_all_patterns — boolean columns
        chart_patterns: List from detect_all_chart_patterns
        regime: Current regime string
        fib_data: Dict with fib levels and reactions
        h4_bias: 'bullish', 'bearish', or None
        
    Returns:
        List of dicts: [{setup, name, direction, confidence, conditions_met, conditions_total, regime_valid}]
    """
    if regime not in VALID_REGIMES:
        regime = 'ranging'  # Default fallback — 'mixed' doesn't exist in backtest DB
    
    valid_for_regime = set(REGIME_SETUP_MAP.get(regime, []))
    active = []
    
    # Extract indicators with safe defaults
    rsi = indicators.get('rsi', 50)
    stoch_k = indicators.get('stoch_k', 50)
    stoch_d = indicators.get('stoch_d', 50)
    adx = indicators.get('adx', 25)
    macd_val = indicators.get('macd_value', 0)
    macd_sig = indicators.get('macd_signal', 0)
    macd_hist = indicators.get('macd_hist', 0)
    bb_upper = indicators.get('bb_upper', 0)
    bb_lower = indicators.get('bb_lower', 0)
    bb_mid = indicators.get('bb_mid', 0)
    bb_width = indicators.get('bb_width', 0)
    close = indicators.get('close', 0)
    sma50 = indicators.get('sma50', 0)
    sma100 = indicators.get('sma100', 0)
    sar = indicators.get('sar', 0)
    cci = indicators.get('cci', 0)
    ema_21 = indicators.get('ema_21', indicators.get('ema21', 0))
    ema_55 = indicators.get('ema_55', indicators.get('ema55', 0))
    ema_100 = indicators.get('ema_100', indicators.get('ema100', 0))
    atr = indicators.get('atr', 0)
    
    # Pre-compute derived conditions
    price_above_bb_upper = close > bb_upper if bb_upper else False
    price_below_bb_lower = close < bb_lower if bb_lower else False
    price_near_bb_upper = abs(close - bb_upper) < atr * 0.3 if bb_upper and atr else False
    price_near_bb_lower = abs(close - bb_lower) < atr * 0.3 if bb_lower and atr else False
    stoch_overbought = stoch_k > 80
    stoch_oversold = stoch_k < 20
    stoch_cross_down = stoch_k < stoch_d and stoch_k > 70  # recent cross from overbought
    stoch_cross_up = stoch_k > stoch_d and stoch_k < 30  # recent cross from oversold
    rsi_overbought = rsi > 70
    rsi_oversold = rsi < 30
    macd_bearish_cross = macd_hist < 0 and macd_val < macd_sig
    macd_bullish_cross = macd_hist > 0 and macd_val > macd_sig
    sar_bearish = sar > close if sar else False
    sar_bullish = sar < close if sar else False
    ema_bearish_cross = ema_21 < ema_55 if ema_21 and ema_55 else False
    ema_bullish_cross = ema_21 > ema_55 if ema_21 and ema_55 else False
    price_below_sma50 = close < sma50 if sma50 else False
    price_below_sma100 = close < sma100 if sma100 else False
    price_above_sma50 = close > sma50 if sma50 else False
    price_above_sma100 = close > sma100 if sma100 else False
    bb_squeeze = bb_width < 0.003 if bb_width else False
    
    # Extract candle pattern booleans  
    cp = candle_patterns if isinstance(candle_patterns, dict) else {}
    has_hammer = cp.get('hammer', False)
    has_inv_hammer = cp.get('inverted_hammer', False)
    has_shooting_star = cp.get('shooting_star', False)
    has_bull_engulfing = cp.get('bullish_engulfing', False)
    has_bear_engulfing = cp.get('bearish_engulfing', False)
    has_morning_star = cp.get('morning_star', False)
    has_evening_star = cp.get('evening_star', False)
    has_doji = cp.get('doji', False)
    has_dragonfly_doji = cp.get('dragonfly_doji', False)
    has_gravestone_doji = cp.get('gravestone_doji', False)
    has_three_white = cp.get('three_white_soldiers', False)
    has_three_black = cp.get('three_black_crows', False)
    has_tweezer_bottom = cp.get('tweezer_bottom', False)
    has_tweezer_top = cp.get('tweezer_top', False)
    
    # Extract chart patterns
    chart_pats = chart_patterns or []
    has_double_top = any(p['pattern'] == 'Double Top' for p in chart_pats)
    has_double_bottom = any(p['pattern'] == 'Double Bottom' for p in chart_pats)
    has_head_shoulders = any(p['pattern'] == 'Head and Shoulders' for p in chart_pats)
    has_inv_head_shoulders = any(p['pattern'] == 'Inverse Head and Shoulders' for p in chart_pats)
    has_asc_triangle = any(p['pattern'] == 'Ascending Triangle' for p in chart_pats)
    has_desc_triangle = any(p['pattern'] == 'Descending Triangle' for p in chart_pats)
    has_sym_triangle = any(p['pattern'] == 'Symmetrical Triangle' for p in chart_pats)
    has_bull_flag = any(p['pattern'] == 'Bull Flag' for p in chart_pats)
    has_bear_flag = any(p['pattern'] == 'Bear Flag' for p in chart_pats)
    has_cup_handle = any(p['pattern'] == 'Cup and Handle' for p in chart_pats)
    
    # Fib data
    at_fib_level = False
    fib_level_name = None
    if fib_data and fib_data.get('reactions'):
        at_fib_level = True
        fib_level_name = fib_data['reactions'][0].get('level', '50%')
    
    # ================================================================
    # S1: BB + Stochastic Overbought/Oversold
    # ================================================================
    s1_sell_conds = [price_near_bb_upper or price_above_bb_upper, stoch_overbought, stoch_cross_down]
    s1_buy_conds = [price_near_bb_lower or price_below_bb_lower, stoch_oversold, stoch_cross_up]
    s1_sell_met = sum(bool(c) for c in s1_sell_conds)
    s1_buy_met = sum(bool(c) for c in s1_buy_conds)
    if s1_sell_met >= 2:
        active.append(_make(1, 'BB + Stoch Overbought', 'sell', s1_sell_met, 3, regime, valid_for_regime))
    if s1_buy_met >= 2:
        active.append(_make(1, 'BB + Stoch Oversold', 'buy', s1_buy_met, 3, regime, valid_for_regime))
    
    # ================================================================
    # S2: MACD Crossover + RSI Extreme
    # ================================================================
    s2_sell_conds = [macd_bearish_cross, rsi_overbought or rsi > 65]
    s2_buy_conds = [macd_bullish_cross, rsi_oversold or rsi < 35]
    if sum(bool(c) for c in s2_sell_conds) >= 2:
        active.append(_make(2, 'MACD Cross + RSI Overbought', 'sell', 2, 2, regime, valid_for_regime))
    if sum(bool(c) for c in s2_buy_conds) >= 2:
        active.append(_make(2, 'MACD Cross + RSI Oversold', 'buy', 2, 2, regime, valid_for_regime))
    
    # ================================================================
    # S3: SAR Flip + EMA Crossover
    # ================================================================
    s3_sell_conds = [sar_bearish, ema_bearish_cross]
    s3_buy_conds = [sar_bullish, ema_bullish_cross]
    if all(s3_sell_conds):
        active.append(_make(3, 'SAR Flip + EMA Cross Down', 'sell', 2, 2, regime, valid_for_regime))
    if all(s3_buy_conds):
        active.append(_make(3, 'SAR Flip + EMA Cross Up', 'buy', 2, 2, regime, valid_for_regime))
    
    # ================================================================
    # S4: SAR + Stochastic Extreme
    # ================================================================
    s4_sell_conds = [sar_bearish, stoch_overbought or stoch_cross_down]
    s4_buy_conds = [sar_bullish, stoch_oversold or stoch_cross_up]
    if all(s4_sell_conds):
        active.append(_make(4, 'SAR + Stoch Overbought', 'sell', 2, 2, regime, valid_for_regime))
    if all(s4_buy_conds):
        active.append(_make(4, 'SAR + Stoch Oversold', 'buy', 2, 2, regime, valid_for_regime))
    
    # ================================================================
    # S5: EMA Trend + MACD (skip when EMAs tangled)
    # ================================================================
    ema_spread = abs(ema_21 - ema_55) / close * 100 if close and ema_21 and ema_55 else 0
    ema_tangled = ema_spread < 0.05  # EMAs too close = "No Man's Land"
    if not ema_tangled:
        s5_sell = [ema_bearish_cross, macd_hist < 0, adx > 20]
        s5_buy = [ema_bullish_cross, macd_hist > 0, adx > 20]
        s5s = sum(bool(c) for c in s5_sell)
        s5b = sum(bool(c) for c in s5_buy)
        if s5s >= 2:
            active.append(_make(5, 'EMA Trend + MACD Down', 'sell', s5s, 3, regime, valid_for_regime))
        if s5b >= 2:
            active.append(_make(5, 'EMA Trend + MACD Up', 'buy', s5b, 3, regime, valid_for_regime))
    
    # ================================================================
    # S7: Fibonacci Retracement (sell at resistance / buy at support)
    # ================================================================
    if at_fib_level and fib_level_name in ['38.2%', '50.0%', '61.8%']:
        # Direction depends on the trend context
        if h4_bias == 'bearish' or (ema_bearish_cross and adx > 20):
            active.append(_make(7, f'Fib {fib_level_name} Retracement Sell', 'sell', 2, 3, regime, valid_for_regime))
        elif h4_bias == 'bullish' or (ema_bullish_cross and adx > 20):
            active.append(_make(7, f'Fib {fib_level_name} Retracement Buy', 'buy', 2, 3, regime, valid_for_regime))
    
    # ================================================================
    # S8: Fibonacci + MACD Confirmation
    # ================================================================
    if at_fib_level:
        if macd_bearish_cross:
            active.append(_make(8, 'Fib + MACD Sell', 'sell', 3, 3, regime, valid_for_regime))
        if macd_bullish_cross:
            active.append(_make(8, 'Fib + MACD Buy', 'buy', 3, 3, regime, valid_for_regime))
    
    # ================================================================
    # S9: Bearish/Bullish Divergence (Stochastic)
    # Detected via sniper divergence flags — check indicators
    # ================================================================
    rsi_divergence_bear = indicators.get('rsi_divergence_bear', False) or indicators.get('divergence_bearish', False)
    rsi_divergence_bull = indicators.get('rsi_divergence_bull', False) or indicators.get('divergence_bullish', False)
    stoch_divergence_bear = indicators.get('stoch_divergence_bear', False)
    stoch_divergence_bull = indicators.get('stoch_divergence_bull', False)
    
    if stoch_divergence_bear or (rsi_overbought and adx > 25 and indicators.get('adx_slope', 0) < 0):
        conds = sum([bool(stoch_divergence_bear), rsi_overbought, adx > 25])
        active.append(_make(9, 'Stoch Divergence Sell', 'sell', conds, 3, regime, valid_for_regime))
    if stoch_divergence_bull or (rsi_oversold and adx > 25 and indicators.get('adx_slope', 0) < 0):
        conds = sum([bool(stoch_divergence_bull), rsi_oversold, adx > 25])
        active.append(_make(9, 'Stoch Divergence Buy', 'buy', conds, 3, regime, valid_for_regime))
    
    # ================================================================
    # S10: RSI Divergence + BB Context + ADX Declining
    # ================================================================
    adx_declining = indicators.get('adx_slope', 0) < 0
    if rsi_divergence_bear and adx_declining:
        s10_conds = [True, price_near_bb_upper, adx_declining]
        active.append(_make(10, 'RSI Div + BB + ADX Decline', 'sell', sum(bool(c) for c in s10_conds), 3, regime, valid_for_regime))
    if rsi_divergence_bull and adx_declining:
        s10_conds = [True, price_near_bb_lower, adx_declining]
        active.append(_make(10, 'RSI Div + BB + ADX Decline', 'buy', sum(bool(c) for c in s10_conds), 3, regime, valid_for_regime))
    
    # ================================================================
    # S11: SMA 50/100 Breakdown/Breakout + MACD — REQUIRES ADX > 25
    # ================================================================
    if adx > 25:
        s11_sell = [price_below_sma50, price_below_sma100, macd_bearish_cross]
        s11_buy = [price_above_sma50, price_above_sma100, macd_bullish_cross]
        s11s = sum(bool(c) for c in s11_sell)
        s11b = sum(bool(c) for c in s11_buy)
        if s11s >= 2:
            active.append(_make(11, 'SMA Breakdown + MACD', 'sell', s11s, 3, regime, valid_for_regime))
        if s11b >= 2:
            active.append(_make(11, 'SMA Breakout + MACD', 'buy', s11b, 3, regime, valid_for_regime))
    
    # ================================================================
    # S12: Bollinger Band Squeeze → Breakout
    # Requires ACTUAL squeeze (bb_width < 0.003) — not just narrow bands
    # Also requires ADX > 20 to confirm trending context (squeeze → breakout)
    # ================================================================
    real_squeeze = bb_width > 0 and bb_width < 0.003
    if real_squeeze and adx > 20:
        s12_conds_sell = [real_squeeze, price_below_bb_lower or close < bb_mid, adx > 20]
        s12_conds_buy = [real_squeeze, price_above_bb_upper or close > bb_mid, adx > 20]
        if sum(bool(c) for c in s12_conds_sell) >= 2:
            active.append(_make(12, 'BB Squeeze Breakdown', 'sell', sum(bool(c) for c in s12_conds_sell), 3, regime, valid_for_regime))
        if sum(bool(c) for c in s12_conds_buy) >= 2:
            active.append(_make(12, 'BB Squeeze Breakout', 'buy', sum(bool(c) for c in s12_conds_buy), 3, regime, valid_for_regime))
    
    # ================================================================
    # S13: Slow Stochastic Oscillator — RANGING ONLY (ADX < 25)
    # ================================================================
    if adx < 25:
        if stoch_cross_down or (stoch_k < stoch_d and stoch_overbought):
            active.append(_make(13, 'Stochastic Overbought Cross', 'sell', 2, 2, regime, valid_for_regime))
        if stoch_cross_up or (stoch_k > stoch_d and stoch_oversold):
            active.append(_make(13, 'Stochastic Oversold Cross', 'buy', 2, 2, regime, valid_for_regime))
    
    # ================================================================
    # S14: CCI Extreme Reversal
    # ================================================================
    if cci > 100:
        active.append(_make(14, 'CCI Overbought Reversal', 'sell', 1, 2, regime, valid_for_regime))
    if cci < -100:
        active.append(_make(14, 'CCI Oversold Reversal', 'buy', 1, 2, regime, valid_for_regime))
    
    # ================================================================
    # S15: Mean Reversion from Stochastic Extremes (was Hidden Divergence)
    # Backtest: 93.1% WR, 7,125 trades in ranging. 98% WR in squeeze.
    # The edge is Stoch extreme + BB extreme → snap back to mean.
    # Original required hidden_divergence which we don't compute live.
    # Broadened to match what actually wins: Stoch OB/OS + price at BB band.
    # ================================================================
    hidden_div_bull = indicators.get('hidden_divergence_bull', False)
    hidden_div_bear = indicators.get('hidden_divergence_bear', False)
    # Original trigger (keep if available)
    if hidden_div_bull and ema_bullish_cross:
        active.append(_make(15, 'Hidden Div Continuation Buy', 'buy', 2, 2, regime, valid_for_regime))
    if hidden_div_bear and ema_bearish_cross:
        active.append(_make(15, 'Hidden Div Continuation Sell', 'sell', 2, 2, regime, valid_for_regime))
    # Broadened trigger: Stochastic extreme + BB band extreme (mean reversion)
    # This captures the actual S15 edge without needing divergence detection
    if not hidden_div_bull and not hidden_div_bear:
        s15_sell_conds = [stoch_k > 75, price_near_bb_upper, adx < 30]
        s15_buy_conds = [stoch_k < 25, price_near_bb_lower, adx < 30]
        s15s = sum(bool(c) for c in s15_sell_conds)
        s15b = sum(bool(c) for c in s15_buy_conds)
        if s15s >= 2:
            active.append(_make(15, 'Stoch OB + BB Upper Reversal', 'sell', s15s, 3, regime, valid_for_regime))
        if s15b >= 2:
            active.append(_make(15, 'Stoch OS + BB Lower Reversal', 'buy', s15b, 3, regime, valid_for_regime))
    
    # ================================================================
    # S16: SAR Stop-and-Reverse — TRENDING ONLY (ADX > 25)
    # ================================================================
    if adx > 25:
        if sar_bearish:
            active.append(_make(16, 'SAR Stop-and-Reverse Short', 'sell', 1, 1, regime, valid_for_regime))
        if sar_bullish:
            active.append(_make(16, 'SAR Stop-and-Reverse Long', 'buy', 1, 1, regime, valid_for_regime))
    
    # ================================================================
    # S17: Triangle Breakout
    # ================================================================
    if has_asc_triangle:
        active.append(_make(17, 'Ascending Triangle Breakout', 'buy', 2, 2, regime, valid_for_regime))
    if has_desc_triangle:
        active.append(_make(17, 'Descending Triangle Breakdown', 'sell', 2, 2, regime, valid_for_regime))
    if has_sym_triangle:
        # Direction from trend context
        d = 'buy' if ema_bullish_cross else 'sell'
        active.append(_make(17, 'Symmetrical Triangle Breakout', d, 1, 2, regime, valid_for_regime))
    
    # ================================================================
    # S18: RSI Divergence + BB + ADX — The Golden Combination
    # ================================================================
    if adx_declining and adx > 20:
        s18_sell = [rsi_divergence_bear or rsi_overbought, price_near_bb_upper, adx_declining]
        s18_buy = [rsi_divergence_bull or rsi_oversold, price_near_bb_lower, adx_declining]
        s18s = sum(bool(c) for c in s18_sell)
        s18b = sum(bool(c) for c in s18_buy)
        if s18s >= 2:
            active.append(_make(18, 'RSI Div + BB + ADX Golden Sell', 'sell', s18s, 3, regime, valid_for_regime))
        if s18b >= 2:
            active.append(_make(18, 'RSI Div + BB + ADX Golden Buy', 'buy', s18b, 3, regime, valid_for_regime))
    
    # ================================================================
    # S19: High Volatility — ATR Spike (advisory, not directional)
    # ================================================================
    avg_atr = indicators.get('avg_atr', atr)
    if atr and avg_atr and atr > avg_atr * 1.5:
        active.append(_make(19, 'High Volatility Warning', 'neutral', 1, 1, regime, valid_for_regime))
    
    # ================================================================
    # S20: Chart Pattern Setups (from chart_patterns.py)
    # ================================================================
    if has_double_top:
        active.append(_make(20, 'Double Top', 'sell', 2, 2, regime, valid_for_regime))
    if has_double_bottom:
        active.append(_make(20, 'Double Bottom', 'buy', 2, 2, regime, valid_for_regime))
    if has_head_shoulders:
        active.append(_make(20, 'Head & Shoulders', 'sell', 3, 3, regime, valid_for_regime))
    if has_inv_head_shoulders:
        active.append(_make(20, 'Inv Head & Shoulders', 'buy', 3, 3, regime, valid_for_regime))
    if has_bull_flag:
        active.append(_make(20, 'Bull Flag', 'buy', 2, 2, regime, valid_for_regime))
    if has_bear_flag:
        active.append(_make(20, 'Bear Flag', 'sell', 2, 2, regime, valid_for_regime))
    if has_cup_handle:
        active.append(_make(20, 'Cup & Handle', 'buy', 2, 2, regime, valid_for_regime))
    
    # ================================================================
    # Custom Setups (S21+) — loaded dynamically from custom_setups.json
    # ================================================================
    custom_setups = _load_custom_setups()
    # Build indicator dict for evaluation
    _ind_for_custom = {
        'rsi': rsi, 'stoch_k': stoch_k, 'stoch_d': stoch_d, 'adx': adx,
        'macd_value': macd_val, 'macd_signal': macd_sig, 'macd_hist': macd_hist,
        'bb_width': bb_width, 'cci': cci, 'atr': atr,
    }
    for cs in custom_setups:
        cs_id = cs.get('setup_id', 'S?')
        cs_num = int(cs_id[1:]) if cs_id[1:].isdigit() else 99
        cs_direction = cs.get('direction', 'both')
        cs_regimes = cs.get('regimes', [])
        
        # Add custom setup to valid_for_regime dynamically
        if regime in cs_regimes:
            valid_for_regime.add(cs_id)
        
        if _evaluate_custom_setup(cs, _ind_for_custom, close, bb_mid, bb_lower, bb_upper):
            n_conditions = len(cs.get('conditions', {}))
            if cs_direction == 'both':
                for d in ['buy', 'sell']:
                    active.append(_make(cs_num, cs.get('name', cs_id), d, n_conditions, n_conditions, regime, valid_for_regime))
            else:
                active.append(_make(cs_num, cs.get('name', cs_id), cs_direction, n_conditions, n_conditions, regime, valid_for_regime))
    
    # ================================================================
    # Candlestick-enhanced versions (boost confidence when candle confirms)
    # ================================================================
    for setup in active:
        if setup['direction'] == 'sell' and (has_bear_engulfing or has_evening_star or has_shooting_star or has_three_black):
            setup['confidence'] = min(0.99, setup['confidence'] + 0.10)
            setup['candle_confirmation'] = True
        elif setup['direction'] == 'buy' and (has_bull_engulfing or has_morning_star or has_hammer or has_three_white):
            setup['confidence'] = min(0.99, setup['confidence'] + 0.10)
            setup['candle_confirmation'] = True
    
    # Sort by confidence descending
    active.sort(key=lambda x: x['confidence'], reverse=True)
    
    return active


def _make(num: int, name: str, direction: str, met: int, total: int, regime: str, valid_set: set) -> Dict:
    """Create a setup classification result."""
    setup_id = f'S{num}'
    regime_valid = setup_id in valid_set
    
    # Base confidence from conditions met
    base_conf = met / total if total > 0 else 0
    
    # Regime bonus/penalty
    if regime_valid:
        confidence = min(0.95, base_conf * 0.85 + 0.15)  # Boost when regime matches
    else:
        confidence = base_conf * 0.60  # Heavy penalty when regime doesn't match
    
    return {
        'setup': setup_id,
        'name': name,
        'direction': direction,
        'confidence': round(confidence, 3),
        'conditions_met': met,
        'conditions_total': total,
        'regime_valid': regime_valid,
        'candle_confirmation': False,
    }


def get_best_setups(classified: List[Dict], min_confidence: float = 0.50, max_results: int = 5) -> List[Dict]:
    """Filter to best regime-valid setups above confidence threshold."""
    valid = [s for s in classified if s['regime_valid'] and s['confidence'] >= min_confidence]
    return valid[:max_results]


def format_for_prompt(classified: List[Dict], max_items: int = 5) -> str:
    """Format classified setups as text for agent prompts."""
    if not classified:
        return "No S1-S20 setups detected in current conditions."
    
    lines = ["**Active S1-S20 Setups:**"]
    for s in classified[:max_items]:
        regime_tag = "✅" if s['regime_valid'] else "⚠️"
        candle_tag = " +candle" if s.get('candle_confirmation') else ""
        lines.append(
            f"- {regime_tag} **{s['setup']}** {s['name']} → {s['direction'].upper()} "
            f"(conf={s['confidence']:.0%}, {s['conditions_met']}/{s['conditions_total']} conditions{candle_tag})"
        )
    return "\n".join(lines)
