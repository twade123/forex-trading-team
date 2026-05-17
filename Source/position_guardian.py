"""
Position Guardian - Parallel trade monitoring with threat scoring.

Pure Python watchdog that monitors ALL open trades simultaneously.
Each trade gets its own async task that evaluates every M1 candle.

Architecture:
  Guardian (this) ──→ Trade Monitor Agent (LLM) ──→ Orchestrator Agent (LLM) ──→ Execution Agent

  GREEN:  Guardian handles silently. Normal trailing via PositionMonitor.
  YELLOW: Guardian tightens + sends status to Trade Monitor.
  RED:    Guardian sends URGENT to Trade Monitor → Orchestrator reasons → Execution acts.
  BLACK:  Guardian kills the trade IMMEDIATELY (safety). Notifies after.

Each open trade gets its own coroutine running in parallel. Trades can open
and close at any time - the guardian spawns/reaps watchers dynamically.

The guardian is NOT an agent. It's infrastructure. The Trade Monitor agent
reads the guardian's threat assessments and communicates with the Orchestrator
using natural language reasoning.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

import pandas as pd
from db_connection import get_db, DB_PATH
from db_pool import get_trading_forex

_JARVIS_ROOT = Path(__file__).resolve().parent.parent.parent
_TRADING_FOREX_DB = str(_JARVIS_ROOT / "Database" / "v2" / "trading_forex.db")

try:
    from flight_recorder import flight, FlightStage
except ImportError:
    flight = None
    FlightStage = None

try:
    from tuning_config import get as tc_get
except ImportError:
    tc_get = lambda param, fallback=None: fallback

logger = logging.getLogger("trading_bot.position_guardian")

# ---------------------------------------------------------------------------
# Threat zone thresholds
# ---------------------------------------------------------------------------
ZONE_YELLOW = tc_get("guardian.zone_yellow", 31)
ZONE_RED = tc_get("guardian.zone_red", 61)
ZONE_BLACK = tc_get("guardian.zone_black", 81)

# How often each trade watcher evaluates (seconds)
EVAL_INTERVAL_S = 60  # Every M1 candle

# M15 refresh interval
M15_REFRESH_S = 900

# How many M1 candles to keep per trade
M1_BUFFER_SIZE = 60

# Spread spike = close everything
SPREAD_SPIKE_MULTIPLIER = tc_get("guardian.spread_spike_multiplier", 8.0)  # 4x was too sensitive — session transitions routinely hit 5x

# Margin danger threshold
MARGIN_DANGER_PCT = tc_get("guardian.margin_danger_pct", 80.0)

# After an LLM escalation, don't re-escalate for N seconds
ESCALATION_COOLDOWN_S = 300

# How often to poll OANDA for new/closed trades (seconds)
RECONCILE_INTERVAL_S = 15

# Normal spreads by pair (pips) - learned dynamically, these are fallbacks
DEFAULT_SPREADS = {
    'EUR_USD': 0.00012, 'GBP_USD': 0.00015, 'USD_JPY': 0.015,
    'AUD_USD': 0.00015, 'NZD_USD': 0.00018, 'USD_CAD': 0.00018,
    'EUR_GBP': 0.00020, 'EUR_JPY': 0.020, 'GBP_JPY': 0.025,
    'AUD_JPY': 0.020, 'CAD_JPY': 0.025, 'AUD_NZD': 0.00020,
    'EUR_AUD': 0.00025,
}

# ---------------------------------------------------------------------------
# Candle normalization (OANDA nested mid.o/h/l/c → flat)
# ---------------------------------------------------------------------------

def _norm(c: Dict) -> Dict:
    """Normalize one OANDA candle to flat OHLC."""
    if 'open' in c and isinstance(c['open'], (int, float)):
        return c
    mid = c.get('mid', c.get('bid', {}))
    return {
        'time': c.get('time', ''),
        'open': float(mid.get('o', 0)),
        'high': float(mid.get('h', 0)),
        'low': float(mid.get('l', 0)),
        'close': float(mid.get('c', 0)),
        'volume': int(c.get('volume', 0)),
        'complete': c.get('complete', True),
    }


def _norm_list(candles: List[Dict]) -> List[Dict]:
    return [_norm(c) for c in candles]


# ---------------------------------------------------------------------------
# Lightweight candle pattern detection (<1ms, no pandas)
# ---------------------------------------------------------------------------

def _detect_pattern(candles: List[Dict]) -> Optional[str]:
    """Detect reversal pattern from last 2-3 candles. Returns name or None."""
    if len(candles) < 2:
        return None

    c = candles[-1]
    p = candles[-2]
    o, h, l, cl = c['open'], c['high'], c['low'], c['close']
    po, ph, pl, pcl = p['open'], p['high'], p['low'], p['close']
    body = abs(cl - o)
    rng = h - l
    p_body = abs(pcl - po)
    if rng == 0:
        return None
    upper_wick = h - max(cl, o)
    lower_wick = min(cl, o) - l

    if pcl < po and cl > o and cl >= po and o <= pcl and body > p_body:
        return 'bullish_engulfing'
    if pcl > po and cl < o and cl <= po and o >= pcl and body > p_body:
        return 'bearish_engulfing'
    if lower_wick >= 2 * body and upper_wick < rng * 0.3 and body > 0:
        return 'hammer'
    if upper_wick >= 2 * body and lower_wick < rng * 0.3 and body > 0:
        return 'shooting_star'
    if body < rng * 0.1:
        return 'doji'
    p_mid = (po + pcl) / 2
    if pcl < po and cl > o and o < pl and cl > p_mid and cl < po:
        return 'piercing_line'
    if pcl > po and cl < o and o > ph and cl < p_mid and cl > po:
        return 'dark_cloud'

    if len(candles) >= 3:
        pp = candles[-3]
        ppo, ppcl = pp['open'], pp['close']
        pp_body = abs(ppcl - ppo)
        if ppcl < ppo and pp_body > body * 2 and p_body < pp_body * 0.3 and cl > o and body > p_body:
            return 'morning_star'
        if ppcl > ppo and pp_body > body * 2 and p_body < pp_body * 0.3 and cl < o and body > p_body:
            return 'evening_star'
    return None


# ---------------------------------------------------------------------------
# Threat scorer - pure function, stateless, <5ms
# ---------------------------------------------------------------------------

def score_threat(
    trade: Dict[str, Any],
    market: Dict[str, Any],
    candles_m1: List[Dict],
    spread_normal: float,
    margin_pct: float = 0.0,
) -> Dict[str, Any]:
    """Score threat by reading the chart as a whole picture, not stacking indicators.

    Philosophy: A trader looks at the chart and sees a STORY. The EMA fan tells the
    trend narrative. Candles confirm or deny it. Momentum indicators (RSI, Stoch, MACD)
    are all measuring the SAME underlying momentum - counting them separately is
    triple-counting. Instead, we synthesize them into a single momentum read.

    The threat score comes from STRUCTURAL changes to the trade thesis:
      - Is the trend story still intact? (EMA fan + velocity)
      - Is price respecting structure? (E100, candle patterns)
      - Is momentum confirming or diverging? (single momentum read)
      - Are there emergency conditions? (spread, margin)

    A strong trend with overbought RSI is NORMAL - that's what a trend looks like.
    RSI at 78 in an expanding bullish fan is not a threat. RSI at 78 in a peaked
    fan with a shooting star at E100 IS a threat - because the structure changed.
    """
    reasons = []

    direction = trade.get('direction', 'buy')
    is_long = direction == 'buy'
    r_mult = trade.get('r_multiple', 0)
    candles_in = trade.get('candles_in_trade', 0)

    ema = market.get('ema', {})
    fan = ema.get('fan_state', 'mixed')
    fan_dir = ema.get('fan_direction', 'neutral')
    velocity = ema.get('separation_velocity', 0)
    trend_health = ema.get('trend_health', 50)
    e100 = ema.get('current_emas', {}).get('ema100', 0)

    price = candles_m1[-1]['close'] if candles_m1 else trade.get('entry_price', 0)

    rsi_val = market.get('rsi', {}).get('value', 50)
    stoch_k = market.get('stochastic', {}).get('k', 50)
    stoch_d = market.get('stochastic', {}).get('d', 50)
    macd_hist = market.get('macd', {}).get('histogram', 0) or 0
    adx_val = market.get('adx', {}).get('value', 25)

    fan_favorable = ((is_long and fan_dir == 'bullish') or (not is_long and fan_dir == 'bearish'))
    fan_against = ((is_long and fan_dir == 'bearish') or (not is_long and fan_dir == 'bullish'))
    
    # Thesis awareness: counter-trend trades EXPECT the fan to be against them
    thesis = trade.get('thesis', {})
    is_mean_reversion = trade.get('is_mean_reversion', False)

    # ── Retrace awareness (2026-04-02) ──
    # During retrace, EMAs compress naturally. E55 and E100 converge. Price sitting
    # at E55 should NOT be scored as "E100 broken" or "E100 proximity danger."
    # The candle behavior relative to the EMAs (especially E55) is the PRIMARY signal.
    retrace_state = trade.get('retrace_state', 'trending')
    retrace_depth = trade.get('retrace_depth', 0.0)
    e100_tests_in_retrace = trade.get('e100_tests_in_retrace', 0)
    peak_fan_width = trade.get('peak_fan_width', 0.0)
    reexpansion_count = trade.get('reexpansion_count', 0)
    in_retrace = retrace_state in ('retracing', 'continuing')
    _ema_convergence = 999.0  # will be computed in structure layer if EMAs available

    # ══════════════════════════════════════════════════════════════════
    # CONTINUOUS PROXIMITY MODEL — how far are candles from E100?
    # The further candles stay from E100, the healthier the trend.
    # Fan width (E21-to-E100) and BB width move in parallel — both
    # expanding = strong trend, both contracting = retrace/danger zone.
    # When candles approach E100: volatility zone, support/resistance test.
    # ══════════════════════════════════════════════════════════════════

    e21 = ema.get('current_emas', {}).get('ema21', 0)
    e55 = ema.get('current_emas', {}).get('ema55', 0)

    # E100 distance as continuous gradient (0 = at E100, higher = safer)
    e100_dist_pct = abs(price - e100) / e100 * 100 if e100 > 0 and price > 0 else 0
    # Price on the WRONG side of E100? (long below E100, short above)
    e100_wrong_side = (is_long and price < e100) or (not is_long and price > e100) if e100 > 0 else False

    # Fan width: distance from outermost EMA (E21) to E100 — measures cascade spread
    fan_width_pct = abs(e21 - e100) / e100 * 100 if e100 > 0 and e21 > 0 else 0

    # BB width from market state
    bb = market.get('bollinger', {})
    bb_upper = bb.get('upper', 0)
    bb_lower = bb.get('lower', 0)
    bb_width_pct = (bb_upper - bb_lower) / price * 100 if price > 0 and bb_upper > 0 else 0

    # Proximity risk score: 0 (safe) to 100 (at E100 on wrong side with reversal confirmation)
    # This feeds into the final threat as a continuous modifier
    proximity_risk = 0
    if e100 > 0 and price > 0:
        if e100_wrong_side:
            # Price THROUGH E100 — structural break, high danger
            proximity_risk = 70
        elif e100_dist_pct < 0.02:
            # Touching E100 — volatility zone
            proximity_risk = 50
        elif e100_dist_pct < 0.05:
            # Very close — testing zone
            proximity_risk = 35
        elif e100_dist_pct < 0.10:
            # Approaching — early warning
            proximity_risk = 15
        elif e100_dist_pct < 0.20:
            # Within range but not testing
            proximity_risk = 5
        else:
            # Comfortably away — trend healthy
            proximity_risk = 0

        # ── RETRACE DISCOUNT on proximity risk (2026-04-02) ──
        # During retrace, EMAs converge toward each other. E55 and E100 get close.
        # Price sitting at E55 level looks "near E100" numerically but is NOT a
        # structural E100 test. Discount proximity_risk when:
        #   1. Trade is in retrace state
        #   2. Fan has compressed significantly from peak (EMAs converging)
        #   3. Price is between E55 and E100 (at E55, not at E100)
        if in_retrace and proximity_risk > 0 and e55 > 0 and e100 > 0:
            e55_dist_pct = abs(price - e55) / e55 * 100 if e55 > 0 else 999
            ema_convergence = abs(e55 - e100) / e100 * 100 if e100 > 0 else 999
            # If price is closer to E55 than to E100, the "E100 proximity" is an
            # artifact of EMA compression, not a real structural test
            price_to_e55 = abs(price - e55)
            price_to_e100 = abs(price - e100)
            if price_to_e55 < price_to_e100:
                # Price is at E55 level, not E100 — discount heavily
                # The tighter the EMAs, the bigger the discount (max 80% reduction)
                if ema_convergence < 0.05:
                    # EMAs nearly merged — proximity to E100 is meaningless
                    discount = 0.80
                elif ema_convergence < 0.10:
                    discount = 0.60
                elif ema_convergence < 0.15:
                    discount = 0.40
                else:
                    discount = 0.20
                old_prox = proximity_risk
                proximity_risk = int(proximity_risk * (1.0 - discount))
                reasons.append(f'Retrace discount: E100 prox {old_prox}→{proximity_risk} '
                               f'(price at E55, EMA gap {ema_convergence:.3f}%, discount {discount:.0%})')

        # RSI extreme at E100 confirms reversal (Tim: "reversals line up with RSI OB/OS")
        rsi_extreme = (is_long and rsi_val > 70) or (not is_long and rsi_val < 30)
        rsi_deep_extreme = (is_long and rsi_val > 80) or (not is_long and rsi_val < 20)
        if proximity_risk >= 35 and rsi_deep_extreme:
            proximity_risk = min(100, proximity_risk + 25)
            reasons.append(f'E100 proximity ({e100_dist_pct:.3f}%) + RSI extreme ({rsi_val:.0f}) = reversal confirmation')
        elif proximity_risk >= 35 and rsi_extreme:
            proximity_risk = min(100, proximity_risk + 15)

        # Fan width collapsing toward E100 — but ONLY flag as danger if fan ORDER is broken.
        # During a normal M15 retracement, the fan compresses as EMAs converge — that's healthy.
        # The fan is still working if E21/E55/E100 are still in the right order for the trade.
        # Only flag "trend structure gone" if the fan has actually INVERTED (order broken).
        # fan_favorable already checks whether the fan direction matches trade direction.
        if fan_width_pct < 0.03 and e100_dist_pct < 0.10:
            if in_retrace:
                # 2026-04-02: During retrace, fan compression is EXPECTED. Don't add threat.
                # The retrace state machine already tracks whether this is healthy or not.
                reasons.append(f'Fan compressing in retrace ({fan_width_pct:.3f}%) — expected, no threat added')
            elif not fan_favorable:
                # Fan inverted or neutral AND compressed near E100 — real structural break
                proximity_risk = min(100, proximity_risk + 20)
                reasons.append(f'Fan width collapsed ({fan_width_pct:.3f}%) near E100 — trend structure gone')
            else:
                # Fan still in correct order — this is a normal retracement compression
                # Add a small note but no threat increase — let the trade breathe
                reasons.append(f'Fan compressing ({fan_width_pct:.3f}%) — retracement, order intact')

    # ══════════════════════════════════════════════════════════════════
    # LAYER 1: TREND STRUCTURE (0-50 points)
    # This is THE most important read. Is the trend still working for us?
    # ══════════════════════════════════════════════════════════════════

    trend_threat = 0

    if fan == 'expanding' and fan_favorable:
        # Best case: trend accelerating in our favor
        trend_threat = -15  # Bonus - actively reduces threat
        reasons.append(f'Trend intact: {fan_dir} fan expanding, health {trend_health}')

    elif fan == 'expanding' and fan_against:
        if is_mean_reversion:
            # THESIS INVALIDATED: we bet on reversal but trend is accelerating against us
            trend_threat = 50
            reasons.append(f'THESIS BROKEN: {fan_dir} fan EXPANDING against mean reversion {direction} — trend NOT dying')
        else:
            # Worst case: trend accelerating AGAINST us
            trend_threat = 50
            reasons.append(f'TREND AGAINST: {fan_dir} fan expanding against {direction}')

    elif fan == 'peaked':
        # Trend exhaustion - this is the KEY inflection point
        if fan_favorable:
            trend_threat = 20
            reasons.append(f'Trend peaked - {fan_dir} momentum maxed out')
        elif is_mean_reversion:
            # Peaked AGAINST us in a mean reversion = THESIS INTACT
            # The trend is exhausting — exactly what we need
            trend_threat = 5
            reasons.append(f'Mean reversion thesis intact: {fan_dir} trend peaked (exhausting as expected)')
        else:
            trend_threat = 35
            reasons.append(f'Trend peaked against trade - reversal risk HIGH')

    elif fan == 'contracting':
        if fan_against and is_mean_reversion:
            # Contracting AGAINST us in a mean reversion = THESIS PLAYING OUT
            # The adverse trend is DYING — this is the best signal for our trade
            trend_threat = 0
            reasons.append(f'Mean reversion thesis playing out: {fan_dir} trend contracting/dying')
        elif fan_against:
            trend_threat = 45
            reasons.append(f'Trend collapsing against trade - fan contracting {fan_dir}')
        elif fan_favorable:
            # Trend fading but still in our direction - moderate concern
            trend_threat = 15
            reasons.append(f'Trend fading - fan contracting but still {fan_dir}')
        else:
            trend_threat = 20
            reasons.append(f'Trend structure unclear - fan contracting')

    else:  # mixed/unknown
        if trend_health < 30:
            trend_threat = 20
            reasons.append(f'No clear trend - health {trend_health}/100')
        else:
            trend_threat = 5

    # Velocity modifier - only matters if trend is questionable
    if trend_threat > 0 and velocity is not None and velocity < 0.003:
        if is_mean_reversion and fan_against:
            # Fading velocity on the adverse trend = GOOD for mean reversion
            pass  # Don't penalize
        else:
            trend_threat += 5
            reasons.append(f'Velocity fading: {velocity:.4f}%/bar')

    # ══════════════════════════════════════════════════════════════════
    # LAYER 2: PRICE STRUCTURE (0-40 points)
    # What is price doing at key levels? Candles tell the real story.
    # Uses candle_structure module for full wick/body/EMA interaction read
    # when available, falls back to basic pattern detection otherwise.
    # ══════════════════════════════════════════════════════════════════
    
    structure_threat = 0
    cs = market.get('candle_structure', {})
    cs_ema = cs.get('ema_interaction', {})
    cs_wick = cs.get('wick_analysis', {})
    cs_body = cs.get('body_progression', {})
    cs_consec = cs.get('consecutive_structure', {})
    e100_int = cs_ema.get('e100', {})
    has_cs = bool(e100_int)  # candle_structure data available?

    # ── E100 structural analysis ──
    if e100 > 0 and price > 0:
        dist_pct = e100_dist_pct  # Already computed in proximity model

        # ── Retrace EMA convergence check (2026-04-02) ──
        # When in retrace, EMAs compress. E55 and E100 converge. If price is at E55
        # level and EMAs are tight, "E100 broken" is a false signal — price broke
        # through the E55/E100 cluster zone, not a genuine E100 structural level.
        # We track whether E55-E100 gap has compressed vs the peak fan width.
        _ema_convergence = abs(e55 - e100) / e100 * 100 if e100 > 0 and e55 > 0 else 999
        _retrace_ema_tight = in_retrace and _ema_convergence < 0.15  # EMAs within 0.15%

        # E100 BROKEN (candle_structure tracks this precisely)
        if has_cs and e100_int.get('interaction') == 'broken':
            # Confirm: broken AGAINST the trade?
            if (is_long and price < e100) or (not is_long and price > e100):
                if _retrace_ema_tight:
                    # 2026-04-02: EMAs converged during retrace. Price crossing the
                    # E55/E100 cluster is NOT the same as breaking a well-separated E100.
                    # Check candle conviction: only score high if candles show real momentum
                    # through the level (large bodies, consecutive directional candles).
                    _recent_bodies = []
                    for c in candles_m1[-5:]:
                        _recent_bodies.append(abs(c['close'] - c['open']))
                    _atr_val = market.get('atr', {}).get('value', 0) or 1e-10
                    _avg_body = sum(_recent_bodies) / max(len(_recent_bodies), 1)
                    _body_ratio = _avg_body / _atr_val

                    if _body_ratio > 0.6:
                        # Large bodies pushing through — this IS conviction, real break
                        structure_threat = 30
                        reasons.append(f'E100 broken in retrace BUT with conviction (body/ATR={_body_ratio:.2f}) — discounted 40→30')
                    else:
                        # Small bodies / wicking at E55/E100 cluster — retrace noise
                        structure_threat = 10
                        reasons.append(f'E100 "broken" in retrace — EMAs converged ({_ema_convergence:.3f}%), '
                                       f'small bodies (body/ATR={_body_ratio:.2f}) — retrace noise, 40→10')
                else:
                    structure_threat = 40
                    reasons.append(f'E100 BROKEN against trade ({e100_int.get("breaks",0)} breaks) — structural level lost')
            else:
                # Broken in our favor = not a threat
                pass
        elif not has_cs and len(candles_m1) >= 2:
            # Fallback: basic break detection
            prev = candles_m1[-2]['close']
            atr_val = market.get('atr', {}).get('value', 0)
            candle_body = abs(price - candles_m1[-1]['open'])
            broke_through = False
            if is_long and prev > e100 and price < e100 and atr_val > 0 and candle_body > atr_val * 0.5:
                broke_through = True
            elif not is_long and prev < e100 and price > e100 and atr_val > 0 and candle_body > atr_val * 0.5:
                broke_through = True
            if broke_through:
                if _retrace_ema_tight:
                    structure_threat = 10
                    reasons.append(f'Price crossed E100 in retrace — EMAs tight ({_ema_convergence:.3f}%), likely noise')
                else:
                    structure_threat = 40
                    reasons.append('Price BROKE E100 with momentum — structural break')

        # ── E100 interaction (rich analysis when candle_structure available) ──
        if structure_threat == 0 and has_cs:
            interaction = e100_int.get('interaction', 'distant')

            if _retrace_ema_tight:
                # 2026-04-02: EMAs converged during retrace. E100 interaction signals
                # are unreliable because candle_structure can't distinguish "at E100"
                # from "at E55 where E100 happens to be nearby." Discount heavily.
                if interaction in ('strong_resistance', 'strong_support', 'broken'):
                    structure_threat = max(structure_threat, 5)
                    reasons.append(f'E100 {interaction} in retrace — EMAs tight ({_ema_convergence:.3f}%), discounted to 5')
                elif interaction in ('wrapping', 'testing'):
                    # Wrapping/testing at converged EMAs is just retrace consolidation
                    reasons.append(f'E100 {interaction} in retrace — EMAs converged, treated as retrace consolidation')
            else:
                if interaction in ('strong_resistance',) and is_long:
                    # E100 is blocking our long — wicks rejected repeatedly
                    structure_threat = 30
                    reasons.append(f'E100 strong resistance ({e100_int.get("bounces",0)} rejections) — ceiling for long')
                elif interaction in ('strong_support',) and not is_long:
                    structure_threat = 30
                    reasons.append(f'E100 strong support ({e100_int.get("bounces",0)} bounces) — floor for short')
                elif interaction == 'wrapping':
                    # Price can't decide — consolidating around E100
                    structure_threat = 15
                    reasons.append('Price wrapping around E100 — indecision at structural level')
                elif interaction == 'testing':
                    structure_threat = 10
                    reasons.append(f'Price testing E100 ({dist_pct:.3f}%) — watching for resolution')

        # ── Fallback E100 test detection (no candle_structure) ──
        if structure_threat == 0 and not has_cs and dist_pct < 0.05:
            if _retrace_ema_tight:
                # 2026-04-02: Near E100 because EMAs converged, not because price moved there
                reasons.append(f'Near E100 in retrace (EMAs tight {_ema_convergence:.3f}%) — fallback test suppressed')
            else:
                pat = None
                windows = [candles_m1[-3:], candles_m1[-2:]]
                if len(candles_m1) >= 3:
                    windows.append(candles_m1[-3:-1])
                for window in windows:
                    if len(window) >= 2:
                        pat = _detect_pattern(window)
                        if pat:
                            break
                bearish_reversals = ['bearish_engulfing', 'evening_star', 'shooting_star', 'dark_cloud']
                bullish_reversals = ['bullish_engulfing', 'morning_star', 'hammer', 'piercing_line']
                against_patterns = bearish_reversals if is_long else bullish_reversals
                if pat and pat in against_patterns:
                    structure_threat = 35
                    reasons.append(f'Reversal pattern ({pat}) at E100 — high-conviction exit signal')
                elif pat == 'doji':
                    structure_threat = 15
                    reasons.append('Indecision at E100 — watching closely')
                else:
                    structure_threat = 10
                    reasons.append(f'Price testing E100 ({dist_pct:.3f}%) — no rejection yet')

    # ══════════════════════════════════════════════════════════════════
    # RETRACE-SPECIFIC: Candle-EMA interaction at E55 (2026-04-02)
    # Tim: "When it's in retrace the candles are the most important part
    # and in relation to the EMAs."
    # During retrace, E55 is the KEY level. E100 proximity signals are
    # discounted above. Instead, evaluate HOW candles interact with E55:
    #   - Small bodies / dojis / wicks bouncing off E55 = healthy retrace
    #   - Large directional bodies THROUGH E55 with follow-through = real reversal
    # This section can REDUCE structure_threat if candles show healthy retrace,
    # or ADD to it if candles show genuine reversal conviction through E55.
    # ══════════════════════════════════════════════════════════════════
    if in_retrace and e55 > 0 and len(candles_m1) >= 5:
        _e55_dist_pct = abs(price - e55) / e55 * 100 if e55 > 0 else 999

        # Only evaluate when price is near or has crossed E55
        if _e55_dist_pct < 0.15:
            _last5 = candles_m1[-5:]
            _atr = market.get('atr', {}).get('value', 0) or 1e-10
            _bodies = [abs(c['close'] - c['open']) for c in _last5]
            _avg_body = sum(_bodies) / len(_bodies)
            _body_atr_ratio = _avg_body / _atr

            # Count candles that pushed THROUGH E55 against the trade
            _against_through_e55 = 0
            _bounce_wicks = 0
            for c in _last5:
                _c_open, _c_close, _c_high, _c_low = c['open'], c['close'], c['high'], c['low']
                _c_body = abs(_c_close - _c_open)
                _c_range = _c_high - _c_low if _c_high > _c_low else 1e-10

                if is_long:
                    # Long trade: check if candle closed below E55 (adverse)
                    if _c_close < e55 and _c_body > _atr * 0.3:
                        _against_through_e55 += 1
                    # Wick bounced off E55 from below (supportive)
                    elif _c_low < e55 < _c_close and (_c_close - _c_low) / _c_range > 0.5:
                        _bounce_wicks += 1
                else:
                    # Short trade: check if candle closed above E55 (adverse)
                    if _c_close > e55 and _c_body > _atr * 0.3:
                        _against_through_e55 += 1
                    # Wick bounced off E55 from above (supportive)
                    elif _c_high > e55 > _c_close and (_c_high - _c_close) / _c_range > 0.5:
                        _bounce_wicks += 1

            if _against_through_e55 >= 3 and _body_atr_ratio > 0.5:
                # 3+ candles with real bodies pushing through E55 = genuine reversal
                _retrace_struct_add = 20
                structure_threat = max(structure_threat, structure_threat + _retrace_struct_add)
                reasons.append(f'RETRACE REVERSAL: {_against_through_e55}/5 candles through E55 '
                               f'with conviction (body/ATR={_body_atr_ratio:.2f}) — real move, not noise')
            elif _bounce_wicks >= 2:
                # Wicks bouncing off E55 = healthy retrace, reduce threat
                _old_st = structure_threat
                structure_threat = max(0, structure_threat - 10)
                reasons.append(f'Retrace healthy: {_bounce_wicks} E55 bounces in last 5 candles — '
                               f'structure threat {_old_st}→{structure_threat}')
            elif _body_atr_ratio < 0.3:
                # Tiny bodies near E55 = consolidation, retrace pausing, not reversing
                _old_st = structure_threat
                structure_threat = max(0, structure_threat - 5)
                reasons.append(f'Retrace consolidation at E55: small bodies (body/ATR={_body_atr_ratio:.2f}) '
                               f'— structure threat {_old_st}→{structure_threat}')

    # ── Wick pressure analysis (candle_structure provides this) ──
    if has_cs and structure_threat < 25:
        wick_pressure = cs_wick.get('dominant_pressure', 'balanced')
        wick_strength = cs_wick.get('pressure_strength', 'neutral')

        # Wicks pushing against our trade direction
        if is_long and wick_pressure == 'selling' and wick_strength == 'strong':
            structure_threat = max(structure_threat, 20)
            reasons.append(f'Strong selling wick pressure (upper avg {cs_wick.get("avg_upper_wick_ratio",0):.0%}) — sellers active')
        elif not is_long and wick_pressure == 'buying' and wick_strength == 'strong':
            structure_threat = max(structure_threat, 20)
            reasons.append(f'Strong buying wick pressure (lower avg {cs_wick.get("avg_lower_wick_ratio",0):.0%}) — buyers active')

        # Rejection cluster forming against us
        if is_long and cs_wick.get('upper_rejection_cluster'):
            cl = cs_wick['upper_rejection_cluster']
            if cl['strength'] in ('strong', 'moderate') and price > cl['level'] * 0.999:
                structure_threat = max(structure_threat, 20)
                reasons.append(f'Resistance cluster at {cl["level"]:.5f} ({cl["touches"]} rejections)')
        elif not is_long and cs_wick.get('lower_rejection_cluster'):
            cl = cs_wick['lower_rejection_cluster']
            if cl['strength'] in ('strong', 'moderate') and price < cl['level'] * 1.001:
                structure_threat = max(structure_threat, 20)
                reasons.append(f'Support cluster at {cl["level"]:.5f} ({cl["touches"]} rejections)')

    # ── Body progression (conviction shifting?) ──
    if has_cs and structure_threat < 25:
        body_trend = cs_body.get('body_trend', 'steady')
        body_bias = cs_body.get('direction_bias', 'mixed')

        # Bodies growing AGAINST our trade
        if body_trend == 'growing':
            if (is_long and 'bear' in body_bias) or (not is_long and 'bull' in body_bias):
                bump = 10 if 'strong' in body_bias else 5
                structure_threat = max(structure_threat, structure_threat + bump)
                reasons.append(f'Bodies growing against trade ({body_bias}, {cs_body.get("body_change_ratio",0):.1f}x)')

    # ── Consecutive exhaustion runs ──
    if has_cs and structure_threat < 20:
        run_state = cs_consec.get('run_state', 'neutral')
        if run_state == 'bull_exhaustion_risk' and not is_long:
            # Bears should worry about 5+ bull candles
            structure_threat = max(structure_threat, 15)
            reasons.append(f'{cs_consec.get("consec_bull",0)} consecutive bull candles against short')
        elif run_state == 'bear_exhaustion_risk' and is_long:
            structure_threat = max(structure_threat, 15)
            reasons.append(f'{cs_consec.get("consec_bear",0)} consecutive bear candles against long')

    # ── Candle patterns AWAY from E100 (basic fallback) ──
    if structure_threat == 0 and len(candles_m1) >= 3 and not has_cs:
        pat = _detect_pattern(candles_m1[-3:])
        bearish_reversals = ['bearish_engulfing', 'evening_star']
        bullish_reversals = ['bullish_engulfing', 'morning_star']
        against_patterns = bearish_reversals if is_long else bullish_reversals
        if pat and pat in against_patterns:
            structure_threat = 15
            reasons.append(f'Reversal pattern: {pat} (no key level — watch for confirmation)')

    # ══════════════════════════════════════════════════════════════════
    # LAYER 3: MOMENTUM READ (0-15 points)
    # RSI, Stoch, MACD are all measuring the same thing: momentum.
    # Synthesize into ONE read. In a trend, overbought is NORMAL.
    # Only flag when momentum DIVERGES from the trend story.
    # ══════════════════════════════════════════════════════════════════

    momentum_threat = 0

    # Count how many momentum indicators are extreme against our trade
    momentum_against = 0
    if is_long:
        if rsi_val > 80: momentum_against += 1
        if stoch_k > 80 and stoch_k < stoch_d: momentum_against += 1  # bearish cross in OB
        if macd_hist < 0: momentum_against += 1
    else:
        if rsi_val < 20: momentum_against += 1
        if stoch_k < 20 and stoch_k > stoch_d: momentum_against += 1  # bullish cross in OS
        if macd_hist > 0: momentum_against += 1

    # Context matters: in a strong trend, overbought is fine
    if fan == 'expanding' and fan_favorable:
        # Strong trend - momentum extremes are expected, not threatening
        if momentum_against >= 3:
            momentum_threat = 5  # Barely a blip - all indicators OB in a trend is normal
            reasons.append(f'Momentum stretched but trend strong (RSI {rsi_val:.0f})')
        # else: 0 - overbought in a strong trend is just... a strong trend
    else:
        # Trend NOT strong - momentum extremes carry more weight
        if momentum_against >= 3:
            momentum_threat = 15
            reasons.append(f'Momentum exhaustion: RSI {rsi_val:.0f}, Stoch K {stoch_k:.0f}, MACD against - trend weak')
        elif momentum_against >= 2:
            momentum_threat = 10
            reasons.append(f'Momentum fading: {momentum_against}/3 indicators against trade')
        elif momentum_against == 1:
            momentum_threat = 3  # One indicator diverging - barely notable

    # ══════════════════════════════════════════════════════════════════
    # LAYER 4: EMERGENCY CONDITIONS (override everything)
    # These are binary: either you're in danger or you're not.
    # ══════════════════════════════════════════════════════════════════

    emergency_threat = 0

    # Spread spike - liquidity evaporated
    # Only BLACK if spread is extreme AND trade is losing money
    current_spread = trade.get('current_spread', 0)
    unrealized_pl = trade.get('unrealizedPL', 0)
    if spread_normal > 0 and current_spread > spread_normal * SPREAD_SPIKE_MULTIPLIER:
        # Emergency threshold scales with candles_in — early in trade, spread noise looks like a spike.
        # On large positions (100K units) even 1-2 pips adverse = -$10 to -$20 from entry spread alone.
        # Use $50 minimum loss before spread spike becomes emergency, and require >5 candles (5 min).
        _spread_pl_threshold = -50.0  # raised from -10 to avoid triggering on entry spread noise
        _spread_candle_min   = 5      # must be at least 5 min into trade
        if unrealized_pl < _spread_pl_threshold and candles_in >= _spread_candle_min:
            emergency_threat = 85  # Instant BLACK
            reasons.append(f'SPREAD SPIKE + LOSING: {current_spread:.5f} (normal {spread_normal:.5f}), PL=${unrealized_pl:.2f}')
        else:
            # Spread is wide but we're not in real danger yet — flag it, don't kill
            reasons.append(f'SPREAD WIDE: {current_spread:.5f} (normal {spread_normal:.5f}) — monitoring (PL=${unrealized_pl:.2f})')

    # Margin danger
    # Snipe trades (thesis-driven entries) get a higher margin threshold —
    # the validator already confirmed the setup; don't kill it on size alone.
    # Warn at 80%, only emergency-close at 95% for snipe/thesis trades.
    is_snipe = trade.get('is_snipe', False) or bool(trade.get('thesis'))
    snipe_margin_threshold = 95.0  # snipes get 95% before kill
    effective_margin_threshold = snipe_margin_threshold if is_snipe else MARGIN_DANGER_PCT
    if margin_pct > effective_margin_threshold:
        emergency_threat = max(emergency_threat, 85)
        reasons.append(f'MARGIN CRITICAL: {margin_pct:.1f}%')
    elif margin_pct > MARGIN_DANGER_PCT and is_snipe:
        # Warn but don't kill — snipe thesis overrides margin caution below 95%
        reasons.append(f'MARGIN HIGH (snipe grace): {margin_pct:.1f}% — monitoring')

    # ══════════════════════════════════════════════════════════════════
    # COMBINE: Weighted synthesis, not additive stacking
    # ══════════════════════════════════════════════════════════════════

    # Mean reversion thesis awareness
    # Counter-trend trades EXPECT initial adverse movement — the trend hasn't
    # reversed yet when we enter. Give grace period for the thesis to play out.
    is_mean_reversion = trade.get('is_mean_reversion', False)
    # ALL trades get development grace — price may waffle negative before thesis plays out
    # Mean reversion gets more (15 min) since counter-trend by nature
    # Regular trades get 8 min to develop
    trade_development_grace = candles_in <= 8  # First 8 M1 candles for all trades
    thesis_grace = is_mean_reversion and candles_in <= 15  # Mean reversion: 15 min

    # Manual trades get grace too — user placed it deliberately
    is_manual = trade.get('is_manual', False)
    if is_manual and candles_in <= tc_get("guardian.manual_grace_candles", 90):  # ~6 M15 candles (90 M1 ticks) grace for manual trades
        thesis_grace = True

    # Development grace: don't tighten/exit on young trades unless emergency
    if trade_development_grace and not thesis_grace:
        thesis_grace = True

    if emergency_threat > 0 and not (is_manual and candles_in <= tc_get("guardian.manual_grace_candles", 90)):
        # Emergency (spread spike, margin) overrides grace — EXCEPT for manual trades.
        # 2026-04-01: Trade #3433 GBP_JPY manual killed in 1 second because margin >80%
        # triggered emergency_threat=85 which bypassed the grace cap entirely.
        # The user placed the trade deliberately knowing their account state.
        # Manual trades get grace period protection even against margin warnings.
        # True SL hits and spread spikes during losing trades still apply.
        threat = emergency_threat
    elif emergency_threat > 0 and is_manual and candles_in <= 90:
        # Manual trade in grace — log the margin/spread concern but don't override.
        # 2026-04-01: MUST zero emergency_threat so the return dict reports
        # emergency=False. Otherwise BLACK zone + emergency=True triggers
        # instant close even though grace capped the score. Trade #3623 EUR_GBP
        # was killed in 2 minutes because emergency flag leaked through.
        reasons.append(f'Manual grace overrides emergency (margin/spread): emergency_threat={emergency_threat}, capped by grace')
        emergency_threat = 0  # Clear so line 683 returns emergency: False
        threat = trend_threat + structure_threat + momentum_threat
    else:
        # Trend is primary (60% weight), structure (30%), momentum (10%)
        # But they interact: momentum only matters when trend is weak
        threat = trend_threat + structure_threat + momentum_threat

        # PROXIMITY MODEL: E100 distance modifies threat continuously
        # Close to E100 = amplify threat. Far from E100 = dampen threat.
        if proximity_risk > 0:
            # Add proximity risk scaled by 0.3 (it's a modifier, not a standalone layer)
            proximity_add = int(proximity_risk * 0.3)
            # 2026-04-02: During retrace, proximity_add already discounted at source.
            # Apply additional halving of the modifier during retrace — let the candle-EMA
            # interaction (structure layer) be the primary signal, not E100 distance.
            if in_retrace:
                _old_pa = proximity_add
                proximity_add = proximity_add // 2
                if _old_pa != proximity_add:
                    reasons.append(f'Retrace proximity dampened: +{_old_pa}→+{proximity_add}')
            threat += proximity_add
            if proximity_risk >= 50:
                reasons.append(f'E100 proximity danger: {e100_dist_pct:.3f}% away, risk={proximity_risk} (+{proximity_add})')

        # If trend is strong in our favor, price far from E100, and nothing structural —
        # clamp threat LOW regardless of momentum noise
        if trend_threat <= 0 and structure_threat <= 10 and proximity_risk <= 15:
            threat = max(0, min(threat, 20))  # Can't go above GREEN in a healthy trend
            if not reasons:
                reasons.append(f'Trend healthy: {fan_dir} {fan}, health {trend_health}, E100 dist {e100_dist_pct:.2f}%')

        # E100 DISTANCE PATIENCE: far from E100 + favorable fan = reduce momentum noise
        if fan_favorable and e100_dist_pct > 0.15 and proximity_risk == 0:
            if momentum_threat > 0 and structure_threat < 15:
                old = threat
                threat = max(0, threat - min(momentum_threat, 10))
                if threat < old:
                    reasons.append(f'E100 patience: {e100_dist_pct:.2f}% from E100, fan {fan} — momentum noise reduced')

        # Mean reversion / manual trade grace: dampen threat during grace period.
        # Previously only fired when trend_threat > 0, which let structure +
        # momentum stack past BLACK unchecked.  Now ALWAYS caps during grace.
        if thesis_grace:
            old_threat = threat
            if trend_threat > 0:
                # Reduce trend contribution by 60% during grace
                damped_trend = int(trend_threat * 0.4)
                threat = damped_trend + structure_threat + momentum_threat
            # Hard cap during grace: can't go above RED (no BLACK kills)
            # This applies regardless of which layers contributed the threat.
            threat = min(threat, ZONE_RED - 1)  # Cap at 60 (high YELLOW)
            if threat < old_threat:
                _grace_label = 'Manual/Scout trade' if is_manual else 'Mean reversion'
                reasons.append(f'{_grace_label} grace ({candles_in} candles): threat {old_threat}→{threat}')

        # Profit protection - be more cautious when we have something to lose
        if r_mult > 1.5 and threat > 25:
            threat = min(100, int(threat * 1.1))
            reasons.append(f'Protecting {r_mult:.1f}R profit')

        # Time decay - only a nudge, not a driver
        if candles_in > 120 and threat > 20:
            threat = min(100, threat + 5)

    threat = max(0, min(100, threat))

    # TIME ESCALATION — REMOVED 2026-03-30
    # Was: +25 threat if trade in YELLOW for 10+ min and never profitable.
    # Root cause of trades 2785 (-3.8p), 2717 (-5.6p): trade was in normal
    # retracement (fan ordered, support holding, BB re-expanding) but the
    # +25 penalty pushed threat past kill threshold the instant retracement
    # protection lifted. Retracements ARE expected to be negative for a while.
    # The stop loss exists for true failures — time alone is not a threat signal.

    if threat >= ZONE_BLACK:   zone = 'BLACK'
    elif threat >= ZONE_RED:   zone = 'RED'
    elif threat >= ZONE_YELLOW: zone = 'YELLOW'
    else:                       zone = 'GREEN'

    return {
        'threat_level': threat,
        'zone': zone,
        'reasons': reasons,
        'escalate': zone == 'RED',
        'emergency': emergency_threat >= 85,  # Only true for spread spike + margin — NOT trend scoring
        'breakdown': {
            'trend': trend_threat,
            'structure': structure_threat,
            'momentum': momentum_threat,
            'emergency': emergency_threat,
            'proximity': proximity_risk,
        },
        'proximity': {
            'e100_dist_pct': round(e100_dist_pct, 4),
            'e100_wrong_side': e100_wrong_side,
            'fan_width_pct': round(fan_width_pct, 4),
            'bb_width_pct': round(bb_width_pct, 4),
            'proximity_risk': proximity_risk,
        },
        'retrace_context': {
            'retrace_state': retrace_state,
            'retrace_depth': round(retrace_depth, 3),
            'e100_tests_in_retrace': e100_tests_in_retrace,
            'in_retrace': in_retrace,
            'ema_convergence_pct': round(_ema_convergence, 4),
        },
    }


# ---------------------------------------------------------------------------
# Market state builder from M1 candles
# ---------------------------------------------------------------------------

def build_market_state(candles_m1: List[Dict], candles_m15: Optional[List[Dict]] = None) -> Dict:
    """Compute indicators from M1 candles. Returns market state dict for score_threat."""
    from backtester.indicators import rsi, stochastic, bollinger_bands, macd, adx, atr, ema

    if not candles_m1 or len(candles_m1) < 30:
        return _empty_state()

    df = pd.DataFrame(candles_m1)
    for col in ('open', 'high', 'low', 'close'):
        df[col] = df[col].astype(float)

    df['rsi'] = rsi(df, 14)
    stoch = stochastic(df, 14, 3)
    df['stoch_k'] = stoch['stoch_k']
    df['stoch_d'] = stoch['stoch_d']
    bb = bollinger_bands(df, 20, 2)
    macd_df = macd(df, 12, 26, 9)
    adx_df = adx(df, 14)
    df['atr'] = atr(df, 14)
    df['ema21'] = ema(df, 21)
    df['ema55'] = ema(df, 55)
    df['ema100'] = ema(df, 100)

    last = df.iloc[-1]
    rsi_val = float(last.get('rsi', 50))
    sk = float(last.get('stoch_k', 50))
    sd = float(last.get('stoch_d', 50))
    e21 = float(last.get('ema21', 0))
    e55 = float(last.get('ema55', 0))
    e100 = float(last.get('ema100', 0))

    # Fan direction
    if e21 > e55 > e100:     fan_dir = 'bullish'
    elif e21 < e55 < e100:   fan_dir = 'bearish'
    else:                     fan_dir = 'mixed'

    # Velocity
    if len(df) >= 6 and e100 > 0:
        sep_now = abs(e21 - e100) / e100 * 100
        e21_5 = float(df.iloc[-6].get('ema21', 0))
        e100_5 = float(df.iloc[-6].get('ema100', 0))
        sep_5 = abs(e21_5 - e100_5) / e100_5 * 100 if e100_5 else 0
        velocity = (sep_now - sep_5) / 5
    else:
        velocity = 0

    if velocity > 0.005:     fan_state = 'expanding'
    elif velocity > 0:       fan_state = 'peaked'
    else:                     fan_state = 'contracting'

    try: adx_val = float(adx_df['adx'].iloc[-1])
    except Exception as e:
        logger.warning("[GUARDIAN] ADX read failed, using default 25: %s", e)
        adx_val = 25

    trend_health = min(100, max(0, int(adx_val * 1.5 + max(0, velocity * 5000))))

    # Override EMA with M15 market_picture if available
    ema_state = {
        'fan_state': fan_state, 'fan_direction': fan_dir,
        'separation_velocity': max(0, velocity), 'trend_health': trend_health,
        'current_emas': {'ema21': e21, 'ema55': e55, 'ema100': e100},
    }

    if candles_m15 and len(candles_m15) >= 100:
        try:
            from backtester.ema_separation import generate_market_picture
            pair = candles_m1[0].get('instrument', 'UNKNOWN')
            mkt = generate_market_picture(pair, candles_m15)
            if mkt:
                # 2026-04-20 BUGFIX: generate_market_picture nests EMA signals inside
                # mkt['ema'], NOT at the top level. Previous code read mkt.get('fan_state'),
                # mkt.get('current_emas'), etc. — all None. Result: M15 override silently
                # fell back to M1-computed values. Scorer has been using M1 EMAs, which
                # always show fan_width < 0.03% on tiny timeframe compression, triggering
                # constant "fan collapsed — trend structure gone" false positives.
                # Fix: read from mkt['ema']['...'] with top-level as fallback.
                _mkt_ema = mkt.get('ema', {}) if isinstance(mkt.get('ema'), dict) else {}
                ema_state['fan_state']           = _mkt_ema.get('fan_state', mkt.get('fan_state', fan_state))
                ema_state['fan_direction']       = _mkt_ema.get('fan_direction', mkt.get('fan_direction', fan_dir))
                ema_state['separation_velocity'] = _mkt_ema.get('separation_velocity',
                                                     mkt.get('separation_velocity', velocity))
                ema_state['trend_health']        = mkt.get('trend_health', trend_health)
                _m15_emas = _mkt_ema.get('current_emas') or mkt.get('current_emas')
                if _m15_emas:
                    ema_state['current_emas'].update(_m15_emas)
        except Exception as e:
            logger.debug("M15 market picture fallback: %s", e)

    # ── Candle structure analysis (wick rejection, body progression, EMA interaction) ──
    # 2026-04-02: CRITICAL FIX — use M15 EMA values for structural level evaluation.
    # Previously used M1 EMAs which are ~15x shorter periods (M1 EMA100 ≈ M15 EMA7).
    # candle_structure was detecting "E100 broken" against a meaningless M1 level.
    # Now: compute M15 EMAs, broadcast to M1 alignment, so candle_structure evaluates
    # M1 candle interaction with the REAL M15 structural levels Tim sees on his chart.
    candle_struct = {}
    try:
        from backtester.candle_structure import analyze_candle_structure

        # Default: M1 EMA lists (fallback if M15 not available)
        e21_for_cs = df['ema21'].tolist()
        e55_for_cs = df['ema55'].tolist()
        e100_for_cs = df['ema100'].tolist()

        # Override with M15 EMAs broadcast to M1 alignment if M15 data available
        if candles_m15 and len(candles_m15) >= 100:
            try:
                _m15_df = pd.DataFrame(candles_m15)
                _m15_df['close'] = _m15_df['close'].astype(float)
                _m15_e21 = ema(_m15_df, 21)
                _m15_e55 = ema(_m15_df, 55)
                _m15_e100 = ema(_m15_df, 100)

                # Broadcast M15 EMA values to M1 candle count.
                # Each M15 EMA value applies to 15 M1 candles.
                # For the last N M1 candles, use the corresponding M15 EMA value.
                _m1_len = len(df)
                _m15_len = len(_m15_df)
                _e21_broadcast = [0.0] * _m1_len
                _e55_broadcast = [0.0] * _m1_len
                _e100_broadcast = [0.0] * _m1_len

                # Map M1 candles to their M15 bar index (each M15 bar = 15 M1 bars)
                # Work backwards from the end (most recent data aligned)
                for i in range(_m1_len):
                    _m15_idx = _m15_len - 1 - ((_m1_len - 1 - i) // 15)
                    _m15_idx = max(0, min(_m15_idx, _m15_len - 1))
                    _e21_broadcast[i] = float(_m15_e21.iloc[_m15_idx]) if pd.notna(_m15_e21.iloc[_m15_idx]) else 0.0
                    _e55_broadcast[i] = float(_m15_e55.iloc[_m15_idx]) if pd.notna(_m15_e55.iloc[_m15_idx]) else 0.0
                    _e100_broadcast[i] = float(_m15_e100.iloc[_m15_idx]) if pd.notna(_m15_e100.iloc[_m15_idx]) else 0.0

                e21_for_cs = _e21_broadcast
                e55_for_cs = _e55_broadcast
                e100_for_cs = _e100_broadcast
                logger.debug("[GUARDIAN] candle_structure using M15 EMAs (broadcast %d M15 → %d M1)", _m15_len, _m1_len)
            except Exception as _m15_cs_err:
                logger.debug("[GUARDIAN] M15 EMA broadcast failed, using M1 fallback: %s", _m15_cs_err)

        candle_list_for_struct = [
            {'open': float(r['open']), 'high': float(r['high']),
             'low': float(r['low']), 'close': float(r['close'])}
            for _, r in df.iterrows()
        ]
        candle_struct = analyze_candle_structure(
            candle_list_for_struct, e21_for_cs, e55_for_cs, e100_for_cs, lookback=20
        )
    except Exception as cs_err:
        logger.debug("Candle structure analysis skipped: %s", cs_err)

    # ── BB override with M15 (2026-04-02) ──
    # M1 BB is micro-noise. M15 BB shows the actual volatility envelope Tim sees.
    # Used for retrace depth calculations and proximity model bb_width.
    bb_upper_val = float(bb['bb_upper'].iloc[-1]) if 'bb_upper' in bb else 0
    bb_lower_val = float(bb['bb_lower'].iloc[-1]) if 'bb_lower' in bb else 0
    if candles_m15 and len(candles_m15) >= 30:
        try:
            _m15_bb_df = pd.DataFrame(candles_m15) if not isinstance(candles_m15, pd.DataFrame) else candles_m15
            for _col in ('open', 'high', 'low', 'close'):
                _m15_bb_df[_col] = _m15_bb_df[_col].astype(float)
            _m15_bb = bollinger_bands(_m15_bb_df, 20, 2)
            if 'bb_upper' in _m15_bb and 'bb_lower' in _m15_bb:
                bb_upper_val = float(_m15_bb['bb_upper'].iloc[-1])
                bb_lower_val = float(_m15_bb['bb_lower'].iloc[-1])
                logger.debug("[GUARDIAN] BB using M15 values: upper=%.5f lower=%.5f", bb_upper_val, bb_lower_val)
        except Exception as _bb_err:
            logger.debug("[GUARDIAN] M15 BB failed, using M1 fallback: %s", _bb_err)

    return {
        'ema': ema_state,
        'rsi': {'value': rsi_val},
        'stochastic': {'k': sk, 'd': sd},
        'bollinger': {
            'upper': bb_upper_val,
            'lower': bb_lower_val,
        },
        'macd': {'histogram': float(macd_df['macd_histogram'].iloc[-1]) if 'macd_histogram' in macd_df else 0},
        'adx': {'value': adx_val},
        'atr': {'value': float(df['atr'].iloc[-1]) if 'atr' in df.columns else 0},
        'candle_structure': candle_struct,
    }


def _empty_state():
    return {
        'ema': {'fan_state': 'mixed', 'fan_direction': 'neutral', 'separation_velocity': 0,
                'trend_health': 50, 'current_emas': {}},
        'rsi': {'value': 50}, 'stochastic': {'k': 50, 'd': 50},
        'bollinger': {'upper': 0, 'lower': 0}, 'macd': {'histogram': 0},
        'adx': {'value': 25}, 'atr': {'value': 0},
    }


# ---------------------------------------------------------------------------
# LLM escalation prompt builder
# ---------------------------------------------------------------------------

def build_escalation_report(trade: Dict, threat: Dict, market: Dict, candles_m1: List[Dict]) -> Dict:
    """Build structured report for Trade Monitor → Orchestrator escalation.

    Returns a dict (not a prompt string) so the Trade Monitor agent can
    incorporate it into its communication with the Orchestrator.
    """
    ema = market.get('ema', {})
    price = candles_m1[-1]['close'] if candles_m1 else trade.get('entry_price', 0)
    e100 = ema.get('current_emas', {}).get('ema100', 0)

    # Candle descriptions
    candle_desc = []
    for c in candles_m1[-3:]:
        body = abs(c['close'] - c['open'])
        rng = c['high'] - c['low']
        ratio = body / rng if rng > 0 else 0
        if ratio < 0.2:    candle_desc.append('doji')
        elif ratio < 0.5:  candle_desc.append('small')
        else:               candle_desc.append('bullish' if c['close'] > c['open'] else 'bearish')

    return {
        'trade_id': trade.get('trade_id', ''),
        'pair': trade.get('instrument', trade.get('pair', '')),
        'direction': trade.get('direction', ''),
        'entry_price': trade.get('entry_price', 0),
        'stop_loss': trade.get('stop_loss', 0),
        'candles_in_trade': trade.get('candles_in_trade', 0),
        'current_pnl_pips': trade.get('current_pnl_pips', 0),
        'r_multiple': trade.get('r_multiple', 0),
        'threat_level': threat['threat_level'],
        'zone': threat['zone'],
        'reasons': threat['reasons'],
        'market_picture': {
            'fan_state': ema.get('fan_state'),
            'fan_direction': ema.get('fan_direction'),
            'velocity': ema.get('separation_velocity', 0),
            'trend_health': ema.get('trend_health', 0),
            'e100_distance_pct': abs(price - e100) / e100 * 100 if e100 > 0 else None,
            'e100_acting_as': 'resistance' if price < e100 else 'support',
            'rsi': market.get('rsi', {}).get('value', 50),
            'stoch_k': market.get('stochastic', {}).get('k', 50),
            'stoch_d': market.get('stochastic', {}).get('d', 50),
            'adx': market.get('adx', {}).get('value', 25),
            'last_3_candles': ' → '.join(candle_desc),
        },
        'recommended_actions': _suggest_actions(threat),
        'urgency': 'CRITICAL' if threat['zone'] == 'RED' else 'HIGH',
    }


def _suggest_actions(threat: Dict) -> List[str]:
    """Generate suggested actions for the Trade Monitor to relay."""
    actions = []
    level = threat['threat_level']
    reasons = threat.get('reasons', [])

    has_reversal = any('reversal' in r.lower() or 'engulfing' in r.lower() for r in reasons)
    has_e100 = any('e100' in r.lower() for r in reasons)
    has_rsi = any('rsi' in r.lower() for r in reasons)
    has_fan_against = any('against' in r.lower() and 'fan' in r.lower() for r in reasons)

    if has_fan_against:
        actions.append('CLOSE - EMA structure has turned against trade')
    if has_reversal and has_e100:
        actions.append('CLOSE - reversal pattern at key EMA level')
    elif has_reversal:
        actions.append('TIGHTEN - reversal pattern forming, lock profits')
    if has_rsi and level >= 60:
        actions.append('TIGHTEN - momentum exhaustion signals')
    if not actions:
        actions.append('TIGHTEN - multiple warning signals accumulating')

    return actions


# ---------------------------------------------------------------------------
# Per-trade watcher coroutine
# ---------------------------------------------------------------------------

class TradeWatcher:
    """Async coroutine that monitors a single open trade.

    One TradeWatcher per open trade, all running in parallel.
    """

    def __init__(
        self,
        trade_id: str,
        instrument: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        units: float,
        pip_size: float,
        display_precision: int,
        oanda_client,
        on_status_update: Optional[Callable] = None,  # async fn(trade_id, threat_dict)
        on_escalation: Optional[Callable] = None,      # async fn(trade_id, report_dict)
        on_emergency: Optional[Callable] = None,        # async fn(trade_id, reason)
        trade_thesis: Optional[Dict] = None,            # thesis context from scout/validator
        user_id: Optional[int] = None,                  # user ID for preference loading
    ):
        self.trade_id = trade_id
        self.instrument = instrument
        self.pair = instrument            # alias — some close paths use watcher.pair
        self.direction = direction
        self.entry_price = entry_price
        self.entry_time = datetime.now(timezone.utc)  # set at watcher spawn; updated by reconcile if available
        self.stop_loss = stop_loss
        self._original_sl = stop_loss  # 2026-04-02: preserve original SL for min-distance safeguard
        self.take_profit = take_profit
        self.units = abs(units)
        self.pip_size = pip_size
        self.display_precision = display_precision
        self._client = oanda_client
        self._on_status = on_status_update
        self._on_escalation = on_escalation
        self._on_emergency = on_emergency

        # Trade thesis context — what story justified this trade?
        # Includes entry_type, thesis text, fan state at entry, expected behavior
        self.trade_thesis = trade_thesis or {}
        self._thesis_entry_type = (trade_thesis or {}).get('entry_type', 'unknown')
        self._thesis_text = (trade_thesis or {}).get('thesis', '')
        # Mean reversion trades expect initial adverse movement — give more room
        self._is_mean_reversion = self._thesis_entry_type in (
            'counter_trend_reversal', 'e100_bounce', 'exhaustion_reversal'
        )
        # Retracement continuation entries — trade entered DURING fan contraction/retracement.
        # Strategy: ordered EMA fan peaks → BBs contract → price pulls back toward E55/E100
        # → enter when candles stall near E55 (mid-retrace) or E100 (deep retrace)
        # → BBs re-expand, fan re-accelerates, trend continues.
        # Key guard behaviors for this entry type:
        #   - Initial BB contraction is EXPECTED (we entered during it) — do NOT penalize
        #   - E100 tests at entry are the SIGNAL, not danger — suppress E100 test exit rule
        #   - EXIT only when: E21 crosses below E55 (bull) / above E55 (bear) = fan structure failed
        #   - CONTINUATION confirmed by: price holds above E55 + BB starts re-expanding
        # 2026-04-17: REVERTED snipe_direct/snipe from this list. Blanket retracement label
        # activated retrace SL trail from candle 1, tightening SL 17-25p→9-12p in minutes.
        # Trades 6883/7068/7349 were +3-5p profitable but trail killed them. Retrace is
        # detected by the scorer-mirrored fast-path when market actually shows retrace.
        self._is_retracement_entry = (
            self._thesis_entry_type in (
                'retracement_continuation', 'fan_retracement', 'e100_retest',
                'e100_retracement', 'e100_bounce', 'retracement',
            ) or
            (trade_thesis or {}).get('fan_state_at_entry') in ('peaked', 'contracting', 'compressed', 'just_crossed') or
            (trade_thesis or {}).get('entry_zone') in ('e100_retest', 'e55_retest', 'deep_retracement') or
            # snipe_direct trades that entered during a retracement — thesis now carries this flag
            bool((trade_thesis or {}).get('is_retracement_entry'))
        )
        # Store invalidation level from watch context — used as max SL for retracement entries
        self._invalidation_level: Optional[float] = (trade_thesis or {}).get('invalidation_level')
        # Track fan structure integrity — the definitive exit for retracement entries
        self._e21_crossed_e55_against: bool = False   # E21 broke through E55 against trade
        self._e55_held_count: int = 0                 # candles where price stayed above E55 (bull) / below E55 (bear)
        self._fan_reaccelerating: bool = False         # True once BB re-expands after retrace
        # Source: 'auto' = placed by trading team, 'manual' = placed by Tim directly,
        # 'kronos_hunter' = kronos-hunter spawned trade, 'snipe_direct' = direct snipe.
        # Prefer explicit source from thesis (set by _reconcile from live_trades.source).
        _thesis_src = (trade_thesis or {}).get('source')
        if _thesis_src:
            self.source = _thesis_src
        else:
            self.source = 'manual' if not trade_thesis else 'auto'

        # Kronos Task 11: Source-aware TUNING resolution. kronos_hunter trades
        # read from the kronos.* namespace; every other source reads global
        # TUNING unchanged (zero risk to existing trades).
        from tuning_config import tc_get_for_trade as _tc_s
        _src = self.source
        self._params = {
            "gate.sl_atr_mult":                _tc_s("gate.sl_atr_mult", _src),
            "gate.tp_atr_mult":                _tc_s("gate.tp_atr_mult", _src),
            "guardian.profit_floor_5p":        _tc_s("guardian.profit_floor_5p", _src),
            "guardian.profit_floor_8p":        _tc_s("guardian.profit_floor_8p", _src),
            "guardian.profit_floor_12p":       _tc_s("guardian.profit_floor_12p", _src),
            "guardian.profit_floor_20p":       _tc_s("guardian.profit_floor_20p", _src),
            "guardian.ratchet_step_pips":      _tc_s("guardian.ratchet_step_pips", _src),
            "guardian.trailing_activation_rr": _tc_s("guardian.trailing_activation_rr", _src),
            "guardian.trailing_atr_mult":      _tc_s("guardian.trailing_atr_mult", _src),
            "guardian.sl_buffer_pips":         _tc_s("guardian.sl_buffer_pips", _src),
            "guardian.sl_min_gap_atr_mult":    _tc_s("guardian.sl_min_gap_atr_mult", _src),
        }
        # Snipe direct trades use ratcheting TP instead of partial-close scaling
        self._is_snipe_direct = (trade_thesis or {}).get('entry_type') == 'snipe_direct'
        
        # User preferences for auto profit controls
        self.user_id = user_id
        self._auto_profit_enabled = True  # Default: ON, will be loaded from DB

        self._m1_buffer: List[Dict] = []
        self._m15_buffer: List[Dict] = []
        self._m15_last_fetch: float = 0
        # Phase 1: cache M15 structural signals so we don't recompute every M1 tick
        # Refreshed only when a new M15 bar arrives (tracked by latest-bar TIMESTAMP,
        # not buffer length — _m15_buffer is a rolling fixed-size buffer so len() never changes)
        self._m15_last_bar_count: int = 0  # deprecated
        self._m15_signal_cache: dict = {
            'decel_bars': set(),
            'peak_bars': set(),
            'return_exit_bars': set(),
            'last_computed_time': '',  # timestamp of latest bar when cache was built
        }
        self._spread_history: List[float] = []
        self._last_escalation_time: float = 0
        self._last_threat: Optional[Dict] = None
        # Per-watcher account summary cache — avoids redundant OANDA calls
        self._acct_cache: Optional[dict] = None
        self._acct_cache_ts: float = 0.0
        self._candles_in_trade: int = 0
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Trade projection - computed each tick
        self._projection: Optional[Dict] = None

        # Profit protection tracking
        self._peak_pnl_pips: float = 0.0
        self._fan_at_peak: str = ''  # fan state when peak PnL was reached
        self._retrace_zone: str = 'trending'  # trending|e21_retrace|e55_retrace|e100_broken
        self._retrace_zone_threat: int = 0    # 0/10/30/55 based on candle-to-EMA position
        self._max_adverse_pips: float = 0.0   # Max drawdown from entry (always positive)
        self._peak_price: float = entry_price  # Track high water mark for ATR trailing
        self._partial_taken: bool = False      # Track if partial TP already taken
        self._peak_unrealized_pl: float = 0.0
        self._sl_moved_to_be: bool = False
        self._last_trail_sl: float = 0.0

        # Failed-rally lock state machine (2026-05-08, claude-code, Tim approved).
        # Catches the "negative-then-brief-positive-then-collapse" pattern. State:
        #   normal -> earned (after N consecutive negative M15 closes)
        #          -> pos_seen (on first positive M15 close after earned)
        #          -> locked   (on first negative M15 close after pos_seen)
        # Once locked, exit at entry + lock_pips when adverse extreme touches the lock.
        # Defaults: N=1 (any prior neg), lock_pips=0 (breakeven), enabled=True.
        # Backtest: post-tune 21d catches 6 of 6 brief-positive big losses (+139p saved,
        # -62p winners killed, net +76.8p). 90d net +134p.
        self._fr_state: str = 'normal'      # normal | earned | pos_seen | locked
        self._fr_consec_neg: int = 0
        self._fr_last_m15_time: str = ''    # to detect new M15 close events
        self._fr_lock_price: Optional[float] = None
        self._fr_lock_bar_time: str = ''

        # ── Exit-marker event-driven dual-mode state (2026-05-14 v2) ──
        # Listens for NEW opposing peak_sep markers during first N M15 bars.
        # Baseline = snapshot of marker set at entry. Any marker outside baseline
        # is a "new appearance" → trigger event.
        # Dual mode:
        #   pnl > 0  → take profit at current close
        #   pnl <= 0 → tighten SL to current_close - buffer (let recovery happen)
        # Backtest 30d watch=15: NET +373p (snipe +245p clean, scout +132p).
        self._em_baseline_marker_times: Optional[set] = None  # snapshot at trade open
        self._em_baseline_m15_count: int = 0                  # # bars when baseline taken
        self._em_be_armed: bool = False                        # SL-tighten armed
        self._em_be_lock_price: Optional[float] = None         # tightened SL price
        self._em_fire_bar: Optional[int] = None                # bar index when rule fired
        self._em_last_eval_m15_time: str = ''                  # last bar we evaluated

        # ── Real-time loser-pattern detector state (2026-05-15) ──
        # Behavioral counterpart to exit_marker. Fires when bar 2-3 from entry
        # shows MFE=0 + 3 adverse closes + RSI counter-direction ≥5.
        # Action: SL→break-even (entry price). Watch M1 for breach.
        # 30d backtest: NET +298p (snipe_direct +275.8p clean).
        self._rt_loser_armed: bool = False
        self._rt_loser_last_eval_m15_time: str = ''
        self._rt_loser_fire_bar: Optional[int] = None

        # ── Entry-time fresh-marker detector state (2026-05-15 FRONT-HALF) ──
        # Checks at first M15 close after entry: fresh opposing peak_sep
        # (within last K bars) + reversal candle + price retraced from extreme.
        # Action: SL→entry±buffer. 30d backtest: NET +101.3p.
        # One-shot: evaluated once on first qualifying bar, then locked in or skipped.
        self._em_fresh_evaluated: bool = False
        self._em_fresh_armed: bool = False
        self._em_fresh_lock_price: Optional[float] = None

        # Ratcheting TP tracking (snipe direct trades)
        # Starts at entry+10pips, ratchets up in 5-pip steps as price advances.
        # When price exceeds current TP by one step, TP is bumped up.
        # Never decreases — locks in each increment. Fills on retrace.
        # Hybrid TP active for ALL trades — snipe-direct, system-generated, and manual entries.
        # Tim always trades with a thesis; the momentum-aware extension applies universally.
        self._ratchet_tp_active: bool = True
        # Anchor initial TP to the set TP price if one exists — otherwise default 10 pips
        _tp_pips_from_entry = 0.0
        if take_profit and entry_price and pip_size:
            _tp_pips_from_entry = abs(take_profit - entry_price) / pip_size
        _initial = max(10.0, round(_tp_pips_from_entry / 5.0) * 5.0) if _tp_pips_from_entry > 5 else 10.0
        self._ratchet_tp_pips: float = _initial
        self._ratchet_step_pips: float = self._params["guardian.ratchet_step_pips"]
        self._ratchet_initial_pips: float = _initial
        self._ratchet_last_update_pips: float = 0.0

        # ── Failsafe profit floor ─────────────────────────────────────────────────
        # Once trade peaks at FAILSAFE_THRESH_PIPS (8 pips), lock in a minimum
        # profit floor on the SL side.  Floor = max($1, peak_$ × 30%).
        # Grows with the trade — can only move up, never down.
        # Sits beneath the hybrid BB/EMA floor as the last line of defense.
        self._failsafe_active: bool = False        # Activated once 8-pip threshold hit
        self._failsafe_sl_pips: float = 0.0        # Current floor in pips above entry
        self._failsafe_peak_usd: float = 0.0       # Peak $ at time of last floor update

        # ── Hybrid TP state (momentum-aware extension) ──────────────────────────
        # Once the original TP is reached, we check BB+EMA momentum before extending.
        # If still running → extend 5 pips and keep watching.
        # If momentum fades → lock the TP price; exit on 2-pip retrace.
        # Big runners (peak ≥ 30 pips extended) → hard cap at 8-pip retrace from peak.
        self._hybrid_tp_extended: bool = False    # TP has been extended past original
        self._hybrid_tp_locked: bool = False      # Momentum faded — TP locked, watching retrace
        self._hybrid_lock_pips: float = 0.0       # Pips at moment of lock
        self._hybrid_original_tp_pips: float = 0.0  # Original TP pips (recorded at first extend)

        # Dynamic EMA/BB exit tracking (pipeline model)
        self._ema_sep_history: List[float] = []  # EMA 21/55 separation each tick
        self._bb_width_history: List[float] = []  # BB width each tick
        self._ema_sep_velocity_negative_count: int = 0  # consecutive negative velocity candles
        self._bb_contracting_count: int = 0  # consecutive BB contracting candles
        # Peak-decel detection: last 3 velocity values so we can match the chart signal's
        # d1_2 > d1_1 > d1_0 pattern (growth slowing before it turns negative)
        self._ema_sep_vel_history: List[float] = []  # last N velocity values
        self._peak_decel_close_fired: bool = False  # only fire once per trade

        # Retrace state machine — tracks contraction → re-expansion cycle
        # States: 'trending' → 'retracing' → 'continuing' or 'reversing'
        self._retrace_state: str = 'trending'  # trending | retracing | continuing
        self._last_retrace_exit_time: Optional[float] = None  # timestamp when retrace→continuing/trending
        self._retrace_depth: float = 0.0       # how much BB/EMA contracted from peak
        self._peak_bb_width: float = 0.0       # BB width at peak before contraction
        self._peak_ema_sep: float = 0.0        # EMA separation at peak before contraction
        self._peak_fan_width: float = 0.0      # full fan width (E21-E100) at peak
        self._retrace_candle_count: int = 0    # candles spent in retrace
        self._e100_tests_in_retrace: int = 0   # times price tested E100 during retrace
        self._reexpansion_count: int = 0       # consecutive re-expansion ticks after retrace
        self._e100_dist_history: List[float] = []  # continuous E100 distance tracking
        self._retrace_m15_bar_count: int = 0   # M15 bars seen — retrace SM only advances on new bars (DEPRECATED: buffer is rolling, use _last_bar_time)
        self._retrace_m15_last_bar_time: str = ""  # timestamp of latest M15 bar — detects new bars in rolling buffer

        # Smart exit tracking — real-time indicator exhaustion detection
        self._rsi_history: List[float] = []          # RSI values each check
        self._stoch_history: List[float] = []        # Stochastic K values
        self._rsi_crossed_extreme: bool = False      # RSI entered OB/OS zone during trade
        self._exit_signals: int = 0                  # Count of concurrent exit signals

    @property
    def last_threat(self) -> Optional[Dict]:
        return self._last_threat

    async def _close_with_reason(self, trigger: str, method: str = "guardian",
                                  pnl_pips: float = 0.0, peak_pips: float = 0.0):
        """Write exit reason to DB BEFORE closing on OANDA.

        Prevents the inline reconciliation from overwriting with
        'oanda_auto_close / reconcile_inline' — the race condition that
        was hiding all real exit reasons (2026-04-20 discovery).
        Also pre-writes pnl_pips/pips from last known values so 404
        reconciliation doesn't zero them out (2026-04-21 fix).
        """
        _pnl = round(getattr(self, '_last_pnl_pips', 0.0) or 0.0, 1)
        _peak = round(self._peak_pnl_pips, 1) if hasattr(self, '_peak_pnl_pips') else 0.0
        # 2026-04-24: Persist MFE/MAE at close so retro-analysis can answer
        # "did this loser ever go green" and "did this winner give back profit".
        # Guardian tracks both in-memory but never wrote them — 0% MFE capture
        # ratio across all diagnostic modules (profit_zone.mfe_capture).
        # Both stored as positive magnitudes — MFE = best favorable, MAE = worst drawdown.
        # profit_zone.mfe_capture expects MFE > 0 (ratio = pnl_pips / mfe).
        _mfe = round(max(0.0, self._peak_pnl_pips), 1) if hasattr(self, '_peak_pnl_pips') else 0.0
        _mae = round(abs(self._max_adverse_pips), 1) if hasattr(self, '_max_adverse_pips') else 0.0
        try:
            from db_pool import get_trading_forex
            conn = get_trading_forex()
            conn.execute(
                "UPDATE live_trades SET exit_trigger=?, exit_method=?, "
                "pnl_pips=?, pips=?, "
                "max_favorable_excursion_pips=?, max_adverse_excursion_pips=?, "
                "max_favorable_pips=?, max_adverse_pips=? "
                "WHERE oanda_trade_id=?",
                (trigger, method, _pnl, _pnl, _mfe, _mae, _mfe, _mae, self.trade_id))
            conn.commit()
        except Exception as e:
            # 2026-04-24: upgraded from silent debug — pre-close DB write
            # carries exit_trigger, MFE/MAE. Silent failure = reconciler
            # may overwrite with 'reconcile_inline' losing real exit reason.
            logger.warning("_close_with_reason DB write FAILED for %s: %s: %s (reconciler may overwrite exit reason)",
                           self.trade_id, type(e).__name__, e)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._client.close_trade(self.trade_id))
        # Shadow outcome backfill — only for Kronos trades
        if getattr(self, 'source', '') == 'kronos_hunter':
            try:
                from kronos_shadow import update_outcome as _shadow_outcome
                _outcome = "win" if pnl_pips > 0 else "loss"
                _shadow_outcome(
                    trade_id=self.trade_id,
                    outcome=_outcome,
                    final_pnl=float(pnl_pips),
                    final_exit_trigger=trigger,
                )
            except Exception as _sho_err:
                logger.warning("[GUARDIAN] shadow outcome backfill failed %s: %s",
                               self.trade_id, _sho_err)

    async def _load_user_preferences(self):
        """Load user preferences for auto profit controls."""
        if not self.user_id:
            return
        
        try:
            from db_pool import get_core
            conn = get_core()
            row = conn.execute(
                "SELECT pref_value FROM trading_preferences WHERE user_id = ? AND pref_key = ?",
                (self.user_id, "risk_auto_profit")
            ).fetchone()

            if row:
                self._auto_profit_enabled = row[0].lower() in ('on', 'true', '1')
                logger.debug("TradeWatcher %s: Auto profit = %s", self.trade_id, self._auto_profit_enabled)
            else:
                self._auto_profit_enabled = True  # Default ON
        except Exception as e:
            logger.warning("Failed to load user preferences for trade %s: %s", self.trade_id, e)
            self._auto_profit_enabled = True  # Default ON

    async def start(self):
        """Start watching this trade."""
        if self._running:
            return
        
        # Load user preferences before starting
        await self._load_user_preferences()
        
        self._running = True
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("Watcher started: trade %s %s %s @ %.5f",
                     self.trade_id, self.direction, self.instrument, self.entry_price)

    async def stop(self):
        """Stop watching."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Watcher stopped: trade %s (%d candles monitored)",
                     self.trade_id, self._candles_in_trade)

    async def _watch_loop(self):
        """Main loop - evaluate every M1 candle."""
        # Always reset escalation timer at loop start — defensive against watcher recycling
        self._last_escalation_time = 0
        # Initial data fetch
        await self._fetch_m15()

        # ── WIDEN OANDA HARD SL → catastrophic safety net only ──────────────
        # The guardian's threat system (EMA fan, BB width, retrace state machine,
        # candle structure) is the real exit manager — it evaluates every M1 bar.
        # OANDA's hard SL fires instantly on price touch, BEFORE the guardian can
        # evaluate.  Trades 2143 & 2153 proved this: guardian was YELLOW (would hold)
        # but OANDA's 1.5×ATR SL killed them mid-retracement.
        # Fix: widen the OANDA SL to 3×ATR for guardian-managed trades so the
        # guardian has room to work.  Applies to snipe, scout, manual.
        # The guardian's dynamic SL trailing (E55/E100 anchored) still tightens the
        # *internal* stop as the trade progresses — OANDA SL is just the disaster cap.
        #
        # 2026-04-16: SKIP for kronos_hunter trades. Backtest (2,834 trades, 83.9% WR)
        # used mechanical SL at sl_atr_mult×ATR+buffer (~22p). Wins last 1 bar median —
        # no retracement to protect. Widening turns 22p bounded losses into 30-70p
        # blowouts (trade 6506: -31.3p, 5699: -71.3p, 6865: -38.4p). Let OANDA's
        # original SL do its job — it matches the backtest exit profile.
        _skip_sl_widen = (getattr(self, 'source', '') == 'kronos_hunter')
        if _skip_sl_widen:
            logger.info(
                "🔒 [GUARDIAN] %s #%s (kronos): keeping original OANDA SL at %.5f "
                "(%.1fp) — no widening for kronos trades",
                self.instrument, self.trade_id, self.stop_loss,
                abs(self.entry_price - self.stop_loss) / self.pip_size if self.stop_loss else 0
            )
        # 2026-05-15 (Tim approved): kill switch for the widening. Default False.
        # Trades 15910/15972/16116 reached planned SL distance but OANDA's
        # widened SL was 2-5x further out — trades kept bleeding. 30d backtest:
        # widening provided NET +6p edge (noise) over not-widening; risk profile
        # vastly cleaner without. Other guardian rules can still TIGHTEN SL
        # (profit_floor, dynamic_sl_trail) — only the disaster-cap widening
        # on spawn is disabled.
        try:
            from tuning_config import tc_get_for_trade as _tc_widen
            _widen_enabled = bool(_tc_widen("guardian.widen_oanda_sl_enabled", self.source, False))
        except Exception:
            _widen_enabled = False
        if not _skip_sl_widen and not _widen_enabled and not getattr(self, '_sl_widened', False):
            self._sl_widened = True  # mark evaluated so we don't keep logging
            _orig_dist = abs(self.entry_price - self.stop_loss) / self.pip_size if self.stop_loss else 0
            logger.info(
                "🔒 [GUARDIAN] %s #%s: keeping original OANDA SL at %.5f (%.1fp) — "
                "widening disabled (guardian.widen_oanda_sl_enabled=False)",
                self.instrument, self.trade_id, self.stop_loss, _orig_dist,
            )
        if _widen_enabled and not getattr(self, '_sl_widened', False) and not _skip_sl_widen:
            self._sl_widened = True
            try:
                loop = asyncio.get_event_loop()
                is_long = self.direction == 'buy'
                _orig_sl = self.stop_loss
                _new_sl = None

                # ── Compute ATR and E100 structural level ──────────────────
                _atr = 0
                _atr_pips = 0
                _e100_val = None
                if self._m15_buffer and len(self._m15_buffer) >= 14:
                    _highs = [c.get('high', 0) for c in self._m15_buffer[-14:]]
                    _lows = [c.get('low', 0) for c in self._m15_buffer[-14:]]
                    if _highs and _lows:
                        _ranges = [h - l for h, l in zip(_highs, _lows)]
                        _atr = sum(_ranges) / len(_ranges) if _ranges else 0
                        _atr_pips = _atr / self.pip_size

                    # Compute E100 from M15 closes (inline — no import needed)
                    _closes = [c.get('close', 0) for c in self._m15_buffer if c.get('close')]
                    if len(_closes) >= 100:
                        _ema = _closes[0]
                        _k = 2.0 / (100 + 1)
                        for _c in _closes[1:]:
                            _ema = _c * _k + _ema * (1 - _k)
                        _e100_val = _ema

                # Structural SL: clear E100 + 0.5×ATR buffer so retrace wicks
                # to E55/E100 don't hit the hard SL while guardian is managing.
                # Works for ALL pairs — ATR-scaled buffer adapts to volatility.
                _structural_sl = None
                if _e100_val is not None and _atr > 0:
                    _buffer = _atr * 0.5
                    if is_long:
                        _structural_sl = _e100_val - _buffer
                    else:
                        _structural_sl = _e100_val + _buffer

                # ATR-based floor (3×ATR from entry — previous default)
                _atr_sl = None
                if _atr > 0:
                    _atr_dist = _atr * 3.0
                    if is_long:
                        _atr_sl = self.entry_price - _atr_dist
                    else:
                        _atr_sl = self.entry_price + _atr_dist

                # Pick the WIDER of structural vs ATR-based SL
                def _pick_wider(a, b, long):
                    """Return whichever SL is farther from entry (wider)."""
                    if a is None:
                        return b
                    if b is None:
                        return a
                    if long:
                        return min(a, b)   # lower = wider for longs
                    else:
                        return max(a, b)   # higher = wider for shorts

                _wide_sl = _pick_wider(_structural_sl, _atr_sl, is_long)

                # Option A: use invalidation level from the watch
                if self._invalidation_level and self._invalidation_level > 0:
                    _inv = self._invalidation_level
                    # Sanity check: invalidation must be on the correct side
                    if (is_long and _inv < self.entry_price) or (not is_long and _inv > self.entry_price):
                        # Cap invalidation at our wide SL distance
                        _max_sl_dist = abs(self.entry_price - _wide_sl) if _wide_sl else (abs(self.entry_price - _orig_sl) * 2.5)
                        _inv_dist = abs(_inv - self.entry_price)
                        if _inv_dist <= _max_sl_dist:
                            # Still ensure invalidation clears E100 + buffer
                            _new_sl = _pick_wider(_inv, _wide_sl, is_long)
                        else:
                            # Invalidation too far — use our computed wide SL
                            _new_sl = _wide_sl
                else:
                    # Option B: no invalidation — use EMA-aware catastrophic SL
                    _new_sl = _wide_sl

                # Log the structural computation for audit trail
                if _e100_val is not None:
                    _e100_dist = abs(self.entry_price - _e100_val) / self.pip_size
                    _struct_dist = abs(self.entry_price - _structural_sl) / self.pip_size if _structural_sl else 0
                    _atr_dist_p = abs(self.entry_price - _atr_sl) / self.pip_size if _atr_sl else 0
                    logger.info(
                        "📐 [GUARDIAN] %s #%s: E100=%.5f (%.1fp from entry), "
                        "structural SL=%.5f (%.1fp), ATR SL=%.5f (%.1fp) → picked %s",
                        self.instrument, self.trade_id, _e100_val, _e100_dist,
                        _structural_sl or 0, _struct_dist,
                        _atr_sl or 0, _atr_dist_p,
                        "structural" if _new_sl == _structural_sl else "ATR-based"
                    )

                # Only widen — never tighten
                if _new_sl is not None:
                    _sl_is_wider = (is_long and _new_sl < _orig_sl) or (not is_long and _new_sl > _orig_sl)
                    if _sl_is_wider:
                        _new_sl = round(_new_sl, self.display_precision)
                        await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                            trade_id=str(self.trade_id),
                            stop_loss={"price": str(_new_sl), "timeInForce": "GTC"}
                        ))
                        _old_dist = abs(self.entry_price - _orig_sl) / self.pip_size
                        _new_dist = abs(self.entry_price - _new_sl) / self.pip_size
                        self.stop_loss = _new_sl
                        _entry_label = "RETRACE" if self._is_retracement_entry else self._thesis_entry_type or self.source
                        logger.info(
                            "🔄 [GUARDIAN] %s #%s (%s): widened OANDA SL %.5f→%.5f "
                            "(%.1fp→%.1fp) — guardian manages exit, OANDA SL is safety net",
                            self.instrument, self.trade_id, _entry_label,
                            _orig_sl, _new_sl, _old_dist, _new_dist
                        )
                    else:
                        logger.debug(
                            "[GUARDIAN] %s #%s: computed SL %.5f is not wider than current %.5f — keeping",
                            self.instrument, self.trade_id, _new_sl, _orig_sl
                        )
            except Exception as _widen_err:
                logger.warning(
                    "[GUARDIAN] %s #%s: failed to widen OANDA SL: %s",
                    self.instrument, self.trade_id, _widen_err
                )

        while self._running:
            try:
                t0 = time.time()
                await self._evaluate_once()
                self._candles_in_trade += 1
                elapsed = time.time() - t0
                await asyncio.sleep(max(1, EVAL_INTERVAL_S - elapsed))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Watcher %s error: %s", self.trade_id, e, exc_info=True)
                await asyncio.sleep(10)

    async def _evaluate_once(self):
        """One evaluation cycle for this trade."""
        loop = asyncio.get_event_loop()

        # Fetch M1 candles
        try:
            raw = await loop.run_in_executor(None, lambda: self._client.get_candles(
                self.instrument, granularity='M1', count=M1_BUFFER_SIZE,
            ))
            self._m1_buffer = _norm_list(raw) if raw else self._m1_buffer
        except Exception as e:
            logger.debug("M1 fetch failed for %s: %s", self.instrument, e)

        if not self._m1_buffer:
            return

        # Refresh M15 every 15 min
        if time.time() - self._m15_last_fetch > M15_REFRESH_S:
            await self._fetch_m15()

        # Get current spread
        current_spread = 0
        try:
            pricing = await loop.run_in_executor(None, lambda: self._client.get_pricing([self.instrument]))
            prices = pricing.get('prices', [])
            if prices:
                ask = float(prices[0].get('asks', [{}])[0].get('price', 0))
                bid = float(prices[0].get('bids', [{}])[0].get('price', 0))
                current_spread = ask - bid
                self._spread_history.append(current_spread)
                if len(self._spread_history) > 100:
                    self._spread_history = self._spread_history[-100:]
        except Exception as e:
            logger.warning("[GUARDIAN] Spread fetch failed for %s: %s", self.instrument, e)

        normal_spread = self._get_normal_spread()

        # Compute P&L
        price = self._m1_buffer[-1]['close']
        risk_pips = abs(self.entry_price - self.stop_loss) / self.pip_size
        pnl_pips = ((price - self.entry_price) / self.pip_size) if self.direction == 'buy' else ((self.entry_price - price) / self.pip_size)
        self._last_pnl_pips = pnl_pips  # Track for _close_with_reason
        r_mult = pnl_pips / risk_pips if risk_pips > 0 else 0

        trade_info = {
            'trade_id': self.trade_id,
            'instrument': self.instrument,
            'pair': self.instrument,
            'direction': self.direction,
            'entry_price': self.entry_price,
            'stop_loss': self.stop_loss,
            'pip_size': self.pip_size,
            'display_precision': self.display_precision,
            'current_pnl_pips': pnl_pips,
            'r_multiple': r_mult,
            'candles_in_trade': self._candles_in_trade,
            'current_spread': current_spread,
        }

        # Get margin — cached at 30s TTL to avoid per-watcher OANDA round-trips.
        # 2026-04-14: BUGFIX. Was using marginUsed/balance * 100 which is NOT a
        # margin-call indicator — it's a position-sizing ratio. With 2 normal 100k
        # positions on a $9k account, marginUsed/balance sits at ~67% even with
        # healthy equity. That tripped MARGIN_DANGER_PCT=60 on every tick, and when
        # manual-grace expired (90 candles), trades got emergency-closed spuriously.
        # Killed trades 5484 (-26.3p) and 5709 (-16.4p) at minimum.
        # Correct: use OANDA's marginCloseoutPercent — the actual distance-to-liquidation
        # metric (100% = OANDA auto-closes positions). Fall back to marginUsed/NAV if
        # missing (uses equity, accounting for unrealized P&L).
        margin_pct = 0
        try:
            _now = time.time()
            if self._acct_cache is None or (_now - self._acct_cache_ts) > 30:
                _raw = await loop.run_in_executor(None, lambda: self._client._request(
                    "GET", f"/v3/accounts/{self._client.account_id}/summary"))
                if _raw and 'account' in _raw:
                    self._acct_cache = _raw['account']
                    self._acct_cache_ts = _now
            if self._acct_cache:
                # Primary: OANDA's own margin-call metric (0-100 scale, 100 = force-close)
                _mco = self._acct_cache.get('marginCloseoutPercent')
                if _mco is not None and _mco != '':
                    # OANDA returns decimal (0.0-1.0+) — convert to percent
                    margin_pct = float(_mco) * 100.0
                else:
                    # Fallback: marginUsed / NAV (equity) — includes unrealized P&L
                    mu = float(self._acct_cache.get('marginUsed', 0))
                    nav = float(self._acct_cache.get('NAV', 0) or self._acct_cache.get('balance', 1))
                    margin_pct = (mu / nav * 100) if nav > 0 else 0
        except Exception as e:
            logger.warning("[GUARDIAN] Margin fetch failed for %s: %s", self.instrument, e)

        # Build market state (CPU-bound, run in executor)
        market = await loop.run_in_executor(None, lambda: build_market_state(
            self._m1_buffer, self._m15_buffer or None))

        # Score threat — pass thesis context for mean-reversion grace
        trade_info['thesis'] = self.trade_thesis
        trade_info['is_mean_reversion'] = self._is_mean_reversion
        trade_info['is_manual'] = (self.source in ('manual', 'scout') or
                                   (self.trade_thesis or {}).get('is_manual', False))
        # Snipe flag: any trade with a validator thesis OR snipe-type entry gets margin grace (95% vs 60%).
        # 2026-04-14: Trade #5709 USD_CHF killed at margin 67.1% because is_snipe evaluated False —
        # self.source is only 'manual'|'auto' (never 'snipe'), and 'snipe_direct' wasn't in the
        # entry_type tuple. Added self._is_snipe_direct (set from trade_thesis.entry_type=='snipe_direct')
        # and 'snipe_direct' to the entry_type list.
        trade_info['is_snipe'] = (
            bool(self.trade_thesis) or
            getattr(self, '_is_snipe_direct', False) or
            getattr(self, 'source', '') in ('snipe', 'snipe_direct') or
            self._thesis_entry_type in ('e100_retest', 'e100_retracement', 'fan_retracement',
                                        'retracement_continuation', 'snipe', 'snipe_direct')
        )
        # ── Retrace context for score_threat ──
        # 2026-04-02: AUD/USD #4271 killed because score_threat had zero retrace
        # awareness. During retrace EMAs compress naturally — E55 and E100 converge.
        # Price sitting at E55 got scored as "E100 BROKEN" because the distance was
        # tiny. Pass retrace state so score_threat can discount these false signals.
        trade_info['retrace_state'] = self._retrace_state          # trending|retracing|continuing
        trade_info['retrace_depth'] = self._retrace_depth          # 0.0-1.0 position-based depth
        trade_info['retrace_zone'] = getattr(self, '_retrace_zone', 'trending')  # trending|e21_retrace|e55_retrace|e100_broken
        trade_info['retrace_zone_threat'] = getattr(self, '_retrace_zone_threat', 0)  # 0/10/30/55
        trade_info['e100_tests_in_retrace'] = self._e100_tests_in_retrace
        trade_info['peak_fan_width'] = self._peak_fan_width        # fan width at peak (before contraction)
        trade_info['reexpansion_count'] = self._reexpansion_count  # consecutive re-expansion bars
        # Kronos trades use a dedicated threat scorer tuned from indicator profiling.
        # Scout's score_threat reads parallel-stable EMAs (Kronos's ideal setup) as
        # "fan collapsing" and kills winners. Kronos scorer only exits when the fan
        # actually flips against direction, price breaks E100 against, or sep collapses.
        trade_info['pair'] = self.instrument
        if getattr(self, 'source', '') == 'kronos_hunter':
            from kronos_threat import score_threat_kronos
            threat = score_threat_kronos(trade_info, market, self._m1_buffer, normal_spread, margin_pct)
            # Flight recorder: capture every tick (raw indicators + threat score) so
            # the Kronos scorer can be retrospectively validated against actual outcomes.
            if flight:
                _ema = market.get('ema', {})
                _emas = _ema.get('current_emas', {})
                _bb = market.get('bollinger', {})
                _atr = market.get('atr', {}).get('value', 0) or 0
                _pip = 0.01 if 'JPY' in self.instrument else 0.0001
                _price = self._m1_buffer[-1]['close'] if self._m1_buffer else entry_price
                _sign = 1.0 if self.direction == 'buy' else -1.0
                flight.record(FlightStage.KRONOS_GUARDIAN_THREAT, pair=self.instrument,
                              trade_id=self.trade_id, data={
                    "score": threat['score'],
                    "zone": threat['zone'],
                    "reasons": threat.get('reasons', []),
                    "layer_scores": threat.get('layer_scores', {}),
                    "pnl_pips": pnl_pips,
                    "r_multiple": r_mult,
                    "candles_in_trade": self._candles_in_trade,
                    # Raw indicator snapshot (what drove the score)
                    "fan_direction": _ema.get('fan_direction', 'neutral'),
                    "fan_state": _ema.get('fan_state', 'unknown'),
                    "sep_velocity": _ema.get('separation_velocity', 0),
                    "dist_e21_pips": round(_sign * (_price - (_emas.get('ema21') or _price)) / _pip, 2) if _emas.get('ema21') else 0,
                    "dist_e55_pips": round(_sign * (_price - (_emas.get('ema55') or _price)) / _pip, 2) if _emas.get('ema55') else 0,
                    "dist_e100_pips": round(_sign * (_price - (_emas.get('ema100') or _price)) / _pip, 2) if _emas.get('ema100') else 0,
                    "bb_width_pips": round((_bb.get('upper', 0) - _bb.get('lower', 0)) / _pip, 2) if _bb.get('upper') else 0,
                    "atr_pips": round(_atr / _pip, 2),
                    "rsi": market.get('rsi', {}).get('value'),
                }, note=f"kronos {threat['zone']} score={threat['score']} pnl={pnl_pips:+.1f}p")
            # Shadow logging: persistent per-tick scores for Phase 5 analysis
            if self._params.get("guardian.shadow_logging_enabled", True):
                try:
                    from kronos_shadow import write_score as _shadow_write
                    _shadow_write(
                        trade_id=self.trade_id,
                        pair=self.instrument,
                        direction=self.direction,
                        tick_time=datetime.now(timezone.utc),
                        candles_in=self._candles_in_trade,
                        threat=threat,
                        pnl_pips=pnl_pips,
                        r_multiple=r_mult,
                        peak_pnl_pips=self._peak_pnl_pips,
                        market=market,
                    )
                except Exception as _sh_err:
                    logger.warning("[GUARDIAN] shadow write failed %s: %s",
                                   self.trade_id, _sh_err)
        else:
            threat = score_threat(trade_info, market, self._m1_buffer, normal_spread, margin_pct)
        threat['pair'] = self.instrument
        threat['trade_id'] = self.trade_id
        threat['pnl_pips'] = pnl_pips
        threat['r_multiple'] = r_mult
        threat['timestamp'] = datetime.now(timezone.utc).isoformat()

        # ── Trade Projection ──
        # Get real $ P&L from OANDA (accounts for pip value in account currency)
        unrealized_pl = 0
        try:
            oanda_trade = await loop.run_in_executor(
                None, lambda: self._client.get_trade(self.trade_id))
            if oanda_trade:
                unrealized_pl = float(oanda_trade.get('unrealizedPL', 0))
                # Update units in case of partial close
                cur_units = float(oanda_trade.get('currentUnits', self.units))
                if cur_units:
                    self.units = abs(cur_units)
        except Exception as e:
            logger.warning("[GUARDIAN] Trade fetch for P&L failed %s: %s", self.trade_id, e)

        atr_val = market.get('atr', {}).get('value', 0)
        projection = self._compute_projection(price, pnl_pips, unrealized_pl, atr_val,
                                                market.get('ema', {}))
        threat['projection'] = projection
        threat['unrealized_pl'] = unrealized_pl
        
        # ── Kronos mechanical trailing (matches candle_walk_replay) ──────────
        # 2026-04-16: The backtest (83.9% WR) used a simple trailing stop:
        #   activate at peak >= trailing_activation_rr × SL_pips (~2.9p)
        #   trail at trailing_atr_mult × ATR (~2.8p from peak)
        # 76.6% of backtest wins exited via this trailing at avg +4.6p.
        # The guardian's own profit protection / smart exit needs pnl>=3p and
        # uses different mechanics — live Kronos trades were floating for hours
        # instead of capturing the 1-3 bar wins the backtest showed.
        if getattr(self, 'source', '') == 'kronos_hunter' and pnl_pips > 0:
            _kr_sl_pips = abs(self.entry_price - self._original_sl) / self.pip_size if self._original_sl else abs(self.entry_price - self.stop_loss) / self.pip_size
            _kr_activation = self._params.get("guardian.trailing_activation_rr", 0.13) * _kr_sl_pips
            _kr_trail_dist = self._params.get("guardian.trailing_atr_mult", 0.28) * (atr_val / self.pip_size if atr_val else _kr_sl_pips * 0.5)
            if not hasattr(self, '_kronos_trail_active'):
                self._kronos_trail_active = False
            if self._peak_pnl_pips >= _kr_activation:
                self._kronos_trail_active = True
            if self._kronos_trail_active and _kr_trail_dist > 0:
                _kr_trail_level = self._peak_pnl_pips - _kr_trail_dist
                if _kr_trail_level > 0 and pnl_pips <= _kr_trail_level:
                    logger.info(
                        "📈 [GUARDIAN] %s #%s (kronos): trailing exit — peak=+%.1fp, "
                        "trail=%.1fp from peak, closing at +%.1fp",
                        self.instrument, self.trade_id, self._peak_pnl_pips,
                        _kr_trail_dist, pnl_pips)
                    try:
                        await self._close_with_reason("kronos_mechanical_trailing")
                        if flight:
                            flight.record(FlightStage.KRONOS_GUARDIAN_EXIT, pair=self.instrument,
                                          trade_id=self.trade_id, data={
                                "score": threat.get('score', 0),
                                "pnl_pips": pnl_pips,
                                "peak_pips": self._peak_pnl_pips,
                                "trail_dist": _kr_trail_dist,
                                "trail_level": _kr_trail_level,
                                "exit_path": "kronos_mechanical_trailing",
                            }, note=f"kronos trailing exit peak={self._peak_pnl_pips:+.1f}p pnl={pnl_pips:+.1f}p")
                    except Exception as _ktr_e:
                        logger.error("Kronos trailing close FAILED for %s: %s", self.trade_id, _ktr_e)
                    return  # Closed — skip further processing

        # ── Profit protection (after we have unrealized_pl) ──
        await self._check_profit_protection(pnl_pips, r_mult, unrealized_pl, threat, price, market)

        # ── Dynamic EMA/BB exit (pipeline model) ──
        await self._check_dynamic_exit(pnl_pips, market, threat)

        # ── Smart profit exit — "the move is done" detection ──
        await self._check_smart_exit(pnl_pips, r_mult, unrealized_pl, market, threat, price)
        
        self._last_threat = threat

        # ── Dispatch by zone ──

        # Always send status update (dashboard + Trade Monitor can read)
        if self._on_status:
            try:
                await self._on_status(self.trade_id, threat)
            except Exception as e:
                logger.warning("[GUARDIAN] Status callback failed for %s: %s", self.trade_id, e)

        zone = threat['zone']

        # Flight: threat assessment (only log YELLOW+ to avoid noise)
        if flight and zone in ('YELLOW', 'RED', 'BLACK'):
            flight.record(FlightStage.GUARDIAN_THREAT, pair=self.instrument,
                          trade_id=self.trade_id, data={
                "threat_score": threat['threat_level'],
                "zone": zone,
                "reasons": threat.get('reasons', [])[:3],
                "unrealized_pl": threat.get('unrealized_pl'),
            }, note=f"{zone} threat={threat['threat_level']}")

        if zone == 'BLACK' and threat.get('emergency', False):
            # SAFETY KILL - ONLY for true emergencies (spread spike + losing, margin danger)
            # Trend-based BLACK gets downgraded to RED and goes through Trade Monitor LLM
            logger.critical("BLACK ZONE trade %s %s - EMERGENCY CLOSE", self.trade_id, self.instrument)
            if self._on_emergency:
                try:
                    await self._on_emergency(self.trade_id, '; '.join(threat['reasons']))
                except Exception as e:
                    logger.warning("[GUARDIAN] Emergency callback failed for %s: %s", self.trade_id, e)
            # Direct close - don't wait for agent chain
            try:
                await self._close_with_reason("emergency_close")
                logger.critical("EMERGENCY CLOSE executed: trade %s", self.trade_id)
                if flight:
                    flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                  trade_id=self.trade_id, data={
                        "action": "emergency_close", "zone": "BLACK",
                        "threat_score": threat['threat_level'],
                        "reasons": threat.get('reasons', [])[:3],
                        "scorer": threat.get('scorer', 'scout'),
                    }, status="warn", note=f"BLACK ZONE emergency close")
                    # Kronos-specific exit log (paired with the KRONOS_GUARDIAN_THREAT
                    # stream so we can retrospectively validate which scorer readings
                    # drove which exits).
                    if getattr(self, 'source', '') == 'kronos_hunter':
                        flight.record(FlightStage.KRONOS_GUARDIAN_EXIT, pair=self.instrument,
                                      trade_id=self.trade_id, data={
                            "score": threat['threat_level'],
                            "zone": "BLACK",
                            "reasons": threat.get('reasons', []),
                            "layer_scores": threat.get('layer_scores', {}),
                            "pnl_pips": pnl_pips,
                            "r_multiple": r_mult,
                            "candles_in_trade": self._candles_in_trade,
                            "emergency": threat.get('emergency', False),
                            "exit_path": "black_emergency_close",
                        }, status="warn",
                           note=f"kronos exit score={threat['threat_level']} pnl={pnl_pips:+.1f}p")
            except Exception as e:
                logger.error("EMERGENCY CLOSE FAILED for %s: %s", self.trade_id, e)
                if flight:
                    flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                  trade_id=self.trade_id, status="error",
                                  note=f"EMERGENCY CLOSE FAILED: {e}")

        elif zone in ('RED', 'BLACK'):
            # 2026-04-16: Kronos BLACK (non-emergency) → close immediately.
            # Kronos trades win in 1-3 bars. If the dedicated Kronos scorer says
            # BLACK (fan flipped + E100 broken + contracting = ~85), the direction
            # call was definitively wrong. No retracement to wait for.
            # Scout/snipe trades still get retracement protection below.
            _is_kronos_trade = (getattr(self, 'source', '') == 'kronos_hunter')
            if _is_kronos_trade and zone == 'BLACK':
                _close_enabled = self._params.get("guardian.threat_black_close_enabled", False)
                if _close_enabled:
                    # Original close path — preserved for rollback via tuning override
                    logger.warning(
                        "⚫ [GUARDIAN] %s #%s (kronos): BLACK %d — closing (no retrace protection for kronos)",
                        self.instrument, self.trade_id, threat['threat_level'])
                    try:
                        await self._close_with_reason("kronos_threat_black")
                        if flight:
                            flight.record(FlightStage.KRONOS_GUARDIAN_EXIT, pair=self.instrument,
                                          trade_id=self.trade_id, data={
                                "score": threat['threat_level'],
                                "zone": "BLACK",
                                "reasons": threat.get('reasons', []),
                                "pnl_pips": pnl_pips,
                                "exit_path": "kronos_black_non_emergency_close",
                            }, status="warn",
                               note=f"kronos BLACK close score={threat['threat_level']} pnl={pnl_pips:+.1f}p")
                    except Exception as e:
                        logger.error("Kronos BLACK close FAILED for %s: %s", self.trade_id, e)
                    return  # Already closed — skip further processing
                else:
                    # SHADOW MODE: log would-have-closed, do NOT close
                    logger.info(
                        "⚫ [GUARDIAN] %s #%s (kronos): BLACK %d — SHADOW (close suppressed)",
                        self.instrument, self.trade_id, threat['threat_level'])
                    if flight:
                        flight.record(FlightStage.KRONOS_GUARDIAN_SHADOW, pair=self.instrument,
                                      trade_id=self.trade_id, data={
                            "score": threat['threat_level'],
                            "zone": "BLACK",
                            "reasons": threat.get('reasons', []),
                            "pnl_pips": pnl_pips,
                            "would_close_at_pips": pnl_pips,
                            "shadow_action": "suppressed_close",
                        }, note=f"SHADOW close suppressed score={threat['threat_level']} pnl={pnl_pips:+.1f}p")
                    # Fall through — standard RED/BLACK retrace-suppression path below.
                    # For Kronos trades that path is effectively a no-op (no escalation callback
                    # closes trades since 2026-04-06). Mechanical SL/TP/trailing handle exits.

            # Non-emergency BLACK falls through here (trend-based, or manual grace
            # overrode emergency). Treat same as RED — use retracement suppression.
            # 2026-04-01: Trade #3623 EUR_GBP manual was killed in 2 min because
            # trend scoring alone hit 90 (BLACK) and non-emergency BLACK had no
            # handler, so it fell through to nothing but the emergency_close still
            # fired due to the leaked emergency flag. Now with emergency flag fixed
            # AND this fallthrough, manual trades get retracement protection.
            # ── RETRACEMENT SUPPRESSION ──
            # 2026-04-06: Trade Monitor LLM close authority DISABLED.
            # Guardian is now sole trade manager. Escalation callback only
            # broadcasts to dashboard (UI notification) — no close decisions.
            # Suppression still useful to reduce dashboard noise during normal
            # retraces. Guardian's own exit rules handle all close conditions.
            #
            # Trade 2165: held through -6.6p retrace → closed +12.7p ($125).
            # This trade: killed at -12.7p by Trade Monitor during retracement
            # when candles hadn't even reached EMA 55.
            # Suppress RED escalation during retracement AND for a cooldown
            # period after exiting retracement.  Trades 2717 and 2785 were killed
            # on the exact tick _retrace_state flipped from 'retracing' to
            # 'continuing' — the trade was recovering but the instant suppression
            # dropped, the structural threat (which was high DURING retrace, as
            # expected) auto-closed it.  5-minute cooldown lets the trend confirm.
            _in_retrace = (self._retrace_state == 'retracing')
            _in_continuing = (self._retrace_state == 'continuing')

            # Track when we last exited retracement state
            if _in_retrace:
                self._last_retrace_exit_time = None  # still in retrace
            elif not hasattr(self, '_last_retrace_exit_time') or self._last_retrace_exit_time is None:
                # Just transitioned OUT of retrace — start cooldown
                self._last_retrace_exit_time = time.time()

            _POST_RETRACE_COOLDOWN_S = 300  # 5 minutes grace after retrace ends
            _in_post_retrace_cooldown = False
            if hasattr(self, '_last_retrace_exit_time') and self._last_retrace_exit_time is not None:
                _elapsed = time.time() - self._last_retrace_exit_time
                if _elapsed < _POST_RETRACE_COOLDOWN_S:
                    _in_post_retrace_cooldown = True

            # 2026-04-02: NEVER suppress RED if trade has NEVER been in profit.
            # EUR_JPY #4319 was -8p to -17p its entire life. Guardian correctly
            # saw RED 73 ("fan collapsed, trend peaked against trade") but
            # post_retrace_cooldown suppressed it. The trade was never retracing
            # from profit — it was simply losing from entry. No profit = no retrace
            # = no reason to suppress the kill signal.
            #
            # 2026-04-16: NEVER suppress for kronos_hunter trades. Backtest shows
            # wins last 1 bar median — there is no retracement to protect. When
            # Kronos scorer fires RED, the direction call was wrong. Trade 6506:
            # RED 72-76 suppressed for 16 min while bleeding -17→-31p. Trade 6362:
            # RED 70 suppressed, lost -25.5p.
            _is_kronos = (getattr(self, 'source', '') == 'kronos_hunter')
            _ever_in_profit = self._peak_pnl_pips > 1.0  # at least 1 pip of real profit
            _suppress_escalation = (_in_retrace or _in_continuing or _in_post_retrace_cooldown) and _ever_in_profit and not _is_kronos

            if _suppress_escalation:
                _suppress_reason = (
                    "retracement_active" if _in_retrace else
                    "continuing_after_retrace" if _in_continuing else
                    "post_retrace_cooldown"
                )
                logger.info(
                    "RED ZONE trade %s %s (threat=%d) — SUPPRESSING escalation: "
                    "%s (depth=%.1f%%, candles=%d, E100 tests=%d). "
                    "Guardian dynamic exit rules manage retracement exits.",
                    self.trade_id, self.instrument, threat['threat_level'],
                    _suppress_reason,
                    self._retrace_depth * 100, self._retrace_candle_count,
                    self._e100_tests_in_retrace,
                )
                if flight:
                    flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                  trade_id=self.trade_id, data={
                        "action": "red_escalation_suppressed",
                        "reason": _suppress_reason,
                        "threat_level": threat['threat_level'],
                        "retrace_state": self._retrace_state,
                        "retrace_depth_pct": round(self._retrace_depth * 100, 1),
                        "retrace_candles": self._retrace_candle_count,
                    }, note=f"RED {threat['threat_level']} suppressed — {_suppress_reason}")
                return  # Do NOT escalate

            # Broadcast RED zone status to dashboard (no close authority)
            # 2026-04-06: Trade Monitor LLM close disabled. This callback now
            # only pushes threat data to the UI for user awareness.
            now = time.time()
            if now - self._last_escalation_time < ESCALATION_COOLDOWN_S:
                return
            self._last_escalation_time = now

            logger.warning("RED ZONE trade %s %s (threat=%d) — broadcasting to dashboard (guardian manages)",
                          self.trade_id, self.instrument, threat['threat_level'])

            if self._on_escalation:
                report = build_escalation_report(trade_info, threat, market, self._m1_buffer)
                # Include retrace context so Trade Monitor can factor it in.
                # peak_pips_cached is consumed by trading_api_routes.py retrace
                # suppression to gate the _ever_in_profit check (never-in-profit
                # losers should NOT be suppressed from auto-close).
                report['retrace_context'] = {
                    'retrace_state': self._retrace_state,
                    'retrace_depth_pct': round(self._retrace_depth * 100, 1),
                    'retrace_candles': self._retrace_candle_count,
                    'e100_tests_in_retrace': self._e100_tests_in_retrace,
                    'reexpansion_count': self._reexpansion_count,
                    'peak_pips_cached': self._peak_pnl_pips,
                }
                # Propagate scorer identity so auto-close path can route correctly
                # (Kronos scorer has its own retrace awareness, bypasses scout's
                # retrace-suppression — see trading_api_routes.py around line 3945).
                report['scorer'] = threat.get('scorer', 'scout')
                report['source'] = getattr(self, 'source', '')
                # 2026-05-13 (Tim approved): Propagate fan-intact state so
                # auto_close_threat90 can refuse to close while fan is still
                # ordered (per Tim: "fan is there until the EMAs cross").
                report['fan_intact'] = not getattr(self, "_e21_crossed_e55_against", False)
                try:
                    await self._on_escalation(self.trade_id, report)
                except Exception as e:
                    logger.error("Escalation callback failed for %s: %s", self.trade_id, e)

        # GREEN/YELLOW: status already sent. Trade Monitor reads it on its
        # regular 5-min check. PositionMonitor handles normal trailing.

    async def _check_profit_protection(
        self,
        pnl_pips: float,
        r_mult: float,
        unrealized_pl: float,
        threat: Dict,
        current_price: float = 0,
        market: Dict = None,
    ):
        """Check and implement profit protection measures.
        
        Tracks peak P&L and implements:
        1. Move SL to breakeven at 1.0R profit
        2. Trailing stop at 1.5R+ (50% of peak profit)
        3. Profit giveback protection (close if >40% giveback + YELLOW+ threat)
        """
        try:
            loop = asyncio.get_event_loop()
            is_long = self.direction == 'buy'
            risk_pips = abs(self.entry_price - self.stop_loss) / self.pip_size
            
            # Update peak tracking for P&L + fan state at peak moment
            if pnl_pips > self._peak_pnl_pips:
                self._peak_pnl_pips = pnl_pips
                # Snapshot fan state at the peak for dynamic floor tiers
                _ema_st = (market or {}).get('ema', {})
                _fan_st = _ema_st.get('fan_state', '') if isinstance(_ema_st, dict) else ''
                self._fan_at_peak = _fan_st or getattr(self, '_fan_at_peak', '')
            if pnl_pips < 0 and abs(pnl_pips) > self._max_adverse_pips:
                self._max_adverse_pips = abs(pnl_pips)  # MAE: worst drawdown from entry
            if unrealized_pl > self._peak_unrealized_pl:
                self._peak_unrealized_pl = unrealized_pl
            
            # Update peak price tracking (high water mark)
            if market is None:
                market = {}
            if current_price > 0:
                if is_long and current_price > self._peak_price:
                    self._peak_price = current_price
                elif not is_long and current_price < self._peak_price:
                    self._peak_price = current_price
            
            # Calculate giveback percentage
            giveback_pct = 0.0
            if self._peak_pnl_pips > 0:
                giveback_pct = max(0, (self._peak_pnl_pips - pnl_pips) / self._peak_pnl_pips * 100)

            # ── RATCHETING PROFIT FLOOR ────────────────────────────────────────
            # 2026-04-01: Replaced old failsafe (8p threshold, 30% lock, reactive).
            # 48hr audit: 22/42 trades left profit on table (~$548). 4 losers were
            # previously in profit (~$230 unnecessary losses). Root cause: old failsafe
            # waited for retrace to hit the floor before moving SL, threshold too high.
            #
            # NEW: Proactive ratchet — as peak profit grows, SL is IMMEDIATELY moved
            # to lock a progressive % of the peak. SL only ever tightens, never widens.
            # Trade keeps running with room, but if it retraces hard the floor catches it.
            #
            # Ratchet schedule (peak pips → lock %):
            #   5-8p  → 30% locked  (5p peak → floor at ~1.5p, trade has 3.5p room)
            #   8-12p → 50% locked  (10p peak → floor at ~5p)
            #   12-20p→ 60% locked  (15p peak → floor at ~9p)
            #   20p+  → 70% locked  (20p peak → floor at ~14p, 25p → ~17.5p)
            #
            # Example: trade runs to 20p, ratchet locks floor at 14p.
            # Trade retraces to 15p — still has room, SL at 14p.
            # Trade bounces back to 25p — floor rises to 17.5p.
            # Trade retraces hard — caught at 17.5p instead of giving it all back.
            _RATCHET_THRESH_PIPS = 4.5  # 2026-05-13 (Tim approved): lowered 5.0→4.5 after audit showed 11 trades peaked 3-5p with no protection became -10p avg losses. Threat gate also removed below.
            if pnl_pips > 0 or self._failsafe_active:
                # Pip value in USD (approx)
                try:
                    _pip_val_usd = (self.units * self.pip_size) / self.entry_price \
                                   if not self.instrument.endswith("USD") \
                                   else self.units * self.pip_size
                except Exception as e:
                    logger.warning("[GUARDIAN] Pip value calc failed for %s: %s", self.instrument, e)
                    _pip_val_usd = self.units * self.pip_size

                # ── Threat-gated profit floor (2026-04-17) ──────────────────
                # 203-trade candle-walk tested 8 strategies. Threat-gated 70%
                # was the clear winner: +1022p total (3x fixed 70%), preserves
                # 43 big runners (4x fixed 70%), avg win +12.6p (vs +7.3p fixed).
                #
                # Logic: floor ONLY engages when threat >= 50 (YELLOW+).
                # GREEN threat = trade healthy, let it run freely.
                # YELLOW+ = something deteriorating, lock 70% of peak.
                # This lets runners run in healthy trends and protects when
                # structure/momentum/proximity starts failing.
                _peak = self._peak_pnl_pips
                _threat_level = threat.get('threat_level', 0) if isinstance(threat, dict) else 0
                # 2026-04-17: Use retrace zone threat for floor gating.
                # Zone threat: trending=0, E21=10, E55=30, E100=55.
                # Gate at 30 = floor engages when price reaches E55+ zone.
                _zone_threat = getattr(self, '_retrace_zone_threat', 0)
                _threat_for_floor = max(_threat_level, _zone_threat)
                _THREAT_FLOOR_GATE = 30  # lock when price at E55+ or threat >= 30

                _threat_elevated = _threat_for_floor >= _THREAT_FLOOR_GATE

                # 2026-05-13 (Tim approved): threat gate REMOVED from activation.
                # Audit of 47 post-tune trades peaking 3-5p showed 11 had no protection
                # at all (no ratchet, no BE-move) — 8 of 11 became losses averaging -10p.
                # USD_CHF 14932 today peaked +5p ($30) but stayed in 'trending' zone
                # (zone_threat=0), so ratchet never engaged, retraced to +1.7p close.
                # Floor must engage on PIP threshold alone, independent of threat zone.
                # Per Tim: "lets first fix the live trading with the 4.5 and make sure
                # the sl is ratcheting as its suppose to, only threat level was wrong".
                # 2026-04-17: 80% lock across tiers (backtest: +1038p at 80% vs +955p at 70%)
                if _peak >= 20.0:
                    _lock_ratio = 0.95
                elif _peak >= 12.0:
                    _lock_ratio = 0.90
                elif _peak >= 8.0:
                    _lock_ratio = 0.85
                elif _peak >= _RATCHET_THRESH_PIPS:
                    _lock_ratio = 0.80
                else:
                    _lock_ratio = 0.0

                if not self._failsafe_active and _peak >= _RATCHET_THRESH_PIPS:
                    # First activation
                    self._failsafe_active = True
                    self._failsafe_sl_pips = max(0.5, _peak * _lock_ratio)
                    self._failsafe_peak_usd = self._peak_unrealized_pl
                    logger.info("🛡️ [RATCHET] %s #%s: activated at peak %.1fp — floor +%.1fp (%.0f%% lock)",
                                self.instrument, self.trade_id,
                                _peak, self._failsafe_sl_pips, _lock_ratio * 100)

                if self._failsafe_active and _lock_ratio > 0:
                    # Grow floor as peak grows — ratchet only UP, never down
                    _new_floor_pips = max(0.5, _peak * _lock_ratio)
                    if _new_floor_pips > self._failsafe_sl_pips + 0.3:
                        logger.info("🛡️ [RATCHET] %s #%s: floor raised +%.1fp→+%.1fp (peak %.1fp, %.0f%% lock)",
                                    self.instrument, self.trade_id,
                                    self._failsafe_sl_pips, _new_floor_pips,
                                    _peak, _lock_ratio * 100)
                        self._failsafe_sl_pips = _new_floor_pips
                        self._failsafe_peak_usd = self._peak_unrealized_pl

                    # PROACTIVE: Move SL to the floor NOW — don't wait for retrace.
                    # This is the key difference from old failsafe. As peak grows,
                    # the SL moves up immediately to lock the profit.
                    _floor_price = (self.entry_price + self._failsafe_sl_pips * self.pip_size) \
                                   if is_long \
                                   else (self.entry_price - self._failsafe_sl_pips * self.pip_size)
                    _floor_price = round(_floor_price, self.display_precision)

                    # Only move SL if it improves on current stop (never widen)
                    _sl_improves = (is_long and _floor_price > self.stop_loss) or \
                                   (not is_long and _floor_price < self.stop_loss)
                    # Must also be on the right side of current price (don't set SL beyond price)
                    _sl_valid = (is_long and _floor_price < current_price) or \
                                (not is_long and _floor_price > current_price)

                    if _sl_improves and _sl_valid:
                        try:
                            loop = asyncio.get_event_loop()
                            await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                                self.trade_id,
                                stop_loss={"price": str(_floor_price), "timeInForce": "GTC"}
                            ))
                            _old_sl_ratchet = self.stop_loss
                            self.stop_loss = _floor_price
                            logger.info("🛡️ [RATCHET] %s #%s: SL moved %.5f→%.5f (locking +%.1fp, peak %.1fp, %.0f%%)",
                                        self.instrument, self.trade_id,
                                        _old_sl_ratchet, _floor_price,
                                        self._failsafe_sl_pips, _peak, _lock_ratio * 100)
                            if flight:
                                flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                              trade_id=self.trade_id, data={
                                    "action": "ratchet_profit_floor",
                                    "old_sl": _old_sl_ratchet,
                                    "new_sl": _floor_price,
                                    "floor_pips": round(self._failsafe_sl_pips, 1),
                                    "peak_pips": round(_peak, 1),
                                    "lock_ratio": _lock_ratio,
                                    "pnl_pips": round(pnl_pips, 1),
                                }, note=f"Ratchet floor: SL {_old_sl_ratchet:.5f}→{_floor_price:.5f} (lock +{self._failsafe_sl_pips:.1f}p of {_peak:.1f}p peak)")
                        except Exception as _fe:
                            logger.error("🛡️ [RATCHET] %s #%s: SL update failed: %s",
                                         self.instrument, self.trade_id, _fe)
            # ── END RATCHETING PROFIT FLOOR ────────────────────────────────────

            # Apply profit protection only if auto_profit is enabled
            if self._auto_profit_enabled:
                # 2026-04-21: snipe_direct reads from tuning_config (snipe.* namespace
                # via tc_get_for_trade) — bypasses risk_config.json. Lets us tighten
                # snipe trailing without touching scout/manual. See tuning_config.py
                # snipe.guardian.trailing_* entries.
                _is_snipe = getattr(self, 'source', '') == 'snipe_direct'
                if _is_snipe:
                    trailing_activation_rr = self._params["guardian.trailing_activation_rr"]
                    trailing_atr_mult = self._params["guardian.trailing_atr_mult"]
                    partial_exit_rr = 1.0
                    partial_exit_ratio = 0.5
                else:
                    # Scout/manual/live: read risk_config.json (unchanged behavior)
                    try:
                        import json
                        import os
                        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Config", "risk_config.json")
                        with open(config_path, 'r') as f:
                            config = json.load(f)
                        position_mgmt = config.get("position_management", {})
                        trailing_activation_rr = float(position_mgmt.get("trailing_stop_activation_rr", 0.5))
                        trailing_atr_mult = float(position_mgmt.get("trailing_stop_atr_multiplier", 1.0))
                        partial_exit_rr = float(position_mgmt.get("partial_exit_rr", 1.0))
                        partial_exit_ratio = float(position_mgmt.get("partial_exit_ratio", 0.5))
                    except Exception as e:
                        logger.warning("Failed to load risk config for profit protection: %s", e)
                        trailing_activation_rr = self._params["guardian.trailing_activation_rr"]  # lock in after half SL-distance of profit (~6p)
                        trailing_atr_mult = self._params["guardian.trailing_atr_mult"]        # trail at 1×ATR (was 1.5) — tighter lock
                        partial_exit_rr = 1.0
                        partial_exit_ratio = 0.5
                
                # ── SNIPE DIRECT: Hybrid Momentum-Aware TP ──────────────────────
                # Phase 1 (below original TP): ratchet works as before.
                # Phase 2 (at/past original TP): check BB+EMA momentum before extending.
                #   - Still running (fan expanding + BB open) → extend TP 5 pips, keep going.
                #   - Momentum fading → lock current pips, exit on 2-pip retrace.
                # Big runner safety: if extended and peak ≥ 30 pips, cap at 8-pip retrace.
                if self._ratchet_tp_active:
                    try:
                        _ratchet_pip_size = self.pip_size

                        # ── Read momentum state from market ────────────────────
                        _ema_st  = (market or {}).get('ema', {})
                        _fan_state = _ema_st.get('fan_state', 'mixed')     # expanding/peaked/contracting
                        _fan_dir   = _ema_st.get('fan_direction', 'mixed')  # bullish/bearish/mixed
                        # BB: prefer M15 buffer (same as _check_dynamic_exit)
                        _bb_expanding = False
                        try:
                            if self._m15_buffer and len(self._m15_buffer) >= 22:
                                import pandas as _hpd
                                from backtester.indicators import bollinger_bands as _hbb
                                _hdf = _hpd.DataFrame(self._m15_buffer[-40:])
                                for _hc in ('open','high','low','close'):
                                    _hdf[_hc] = _hdf[_hc].astype(float)
                                _hbands = _hbb(_hdf, 20, 2)
                                if 'bb_upper' in _hbands and len(_hbands) >= 2:
                                    _hw_now  = float(_hbands['bb_upper'].iloc[-1]) - float(_hbands['bb_lower'].iloc[-1])
                                    _hw_prev = float(_hbands['bb_upper'].iloc[-2]) - float(_hbands['bb_lower'].iloc[-2])
                                    _bb_expanding = _hw_now >= _hw_prev
                            else:
                                _bb = (market or {}).get('bollinger', {})
                                _bb_expanding = bool(_bb.get('upper', 0) and _bb.get('lower', 0))
                        except Exception as e:
                            logger.warning("[GUARDIAN] BB expansion check failed for %s: %s", self.instrument, e)
                            _bb_expanding = True  # assume open on error — conservative

                        # Running: fan expanding + direction aligns + BB open
                        _dir_ok = (is_long and _fan_dir == 'bullish') or \
                                  (not is_long and _fan_dir == 'bearish')
                        _is_running = (_fan_state == 'expanding') and _bb_expanding and _dir_ok

                        # ── Big-runner hard cap (works regardless of lock state) ─
                        if self._hybrid_tp_extended and self._peak_pnl_pips >= 30.0:
                            _big_giveback = self._peak_pnl_pips - pnl_pips
                            if _big_giveback >= 8.0 and pnl_pips > 0:
                                logger.info("🏆 [HYBRID TP] %s #%s: big-runner cap — peak %.1fp, retrace %.1fp → closing at +%.1fp",
                                            self.instrument, self.trade_id,
                                            self._peak_pnl_pips, _big_giveback, pnl_pips)
                                try:
                                    await self._close_with_reason("profit_giveback")
                                except Exception as _brc_e:
                                    logger.warning("Big-runner close failed %s: %s", self.trade_id, _brc_e)

                        # ── Locked TP: watch for 2-pip retrace ─────────────────
                        elif self._hybrid_tp_locked:
                            _retrace = self._hybrid_lock_pips - pnl_pips
                            if _retrace >= 3.5 and pnl_pips > 0:
                                logger.info("🎯 [HYBRID TP] %s #%s: 2-pip retrace from lock (%.1fp → %.1fp) → closing",
                                            self.instrument, self.trade_id,
                                            self._hybrid_lock_pips, pnl_pips)
                                try:
                                    await self._close_with_reason("floor_breach")
                                except Exception as _htc_e:
                                    logger.warning("Hybrid TP close failed %s: %s", self.trade_id, _htc_e)
                            elif _is_running:
                                # Momentum returned — unlock and let it run again
                                self._hybrid_tp_locked = False
                                logger.info("🔓 [HYBRID TP] %s #%s: momentum returned — unlocking TP",
                                            self.instrument, self.trade_id)

                        # ── Standard ratchet / extension logic ─────────────────
                        elif pnl_pips >= self._ratchet_initial_pips:
                            _excess_pips = pnl_pips - self._ratchet_initial_pips
                            _increments  = int(_excess_pips / self._ratchet_step_pips)
                            _new_ratchet = self._ratchet_initial_pips + (_increments * self._ratchet_step_pips)

                            if _new_ratchet > self._ratchet_tp_pips and \
                               abs(_new_ratchet - self._ratchet_last_update_pips) >= self._ratchet_step_pips:

                                # Record original TP on first extension
                                if not self._hybrid_original_tp_pips:
                                    self._hybrid_original_tp_pips = self._ratchet_tp_pips

                                if _is_running or not self._hybrid_tp_extended:
                                    # Momentum running (or first ratchet) → extend TP
                                    if is_long:
                                        _new_tp_price = self.entry_price + (_new_ratchet * _ratchet_pip_size)
                                    else:
                                        _new_tp_price = self.entry_price - (_new_ratchet * _ratchet_pip_size)
                                    _new_tp_price = round(_new_tp_price, self.display_precision)
                                    try:
                                        _tp_str = f"{_new_tp_price:.{self.display_precision}f}"
                                        await loop.run_in_executor(None, lambda tp=_tp_str:
                                            self._client.set_trade_orders(
                                                self.trade_id,
                                                take_profit={"price": tp, "timeInForce": "GTC"}
                                            )
                                        )
                                        _mode = "EXTEND" if self._hybrid_tp_extended else "RATCHET"
                                        logger.info("⚡ [HYBRID TP/%s] %s #%s: +%.1fp → TP → +%.0fp @ %.5f | fan:%s bb:%s",
                                                    _mode, self.instrument, self.trade_id,
                                                    pnl_pips, _new_ratchet, _new_tp_price,
                                                    _fan_state, "open" if _bb_expanding else "closing")
                                        _old_tp_ratchet = self.take_profit
                                        self._ratchet_tp_pips = _new_ratchet
                                        self._ratchet_last_update_pips = _new_ratchet
                                        self.take_profit = _new_tp_price
                                        self._hybrid_tp_extended = True
                                        if flight:
                                            flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                                          trade_id=self.trade_id, data={
                                                "action": f"ratchet_tp_{_mode.lower()}",
                                                "old_tp": _old_tp_ratchet,
                                                "new_tp": _new_tp_price,
                                                "ratchet_pips": _new_ratchet,
                                                "pnl_pips": round(pnl_pips, 1),
                                                "fan_state": _fan_state,
                                            }, note=f"Hybrid TP {_mode}: +{_new_ratchet:.0f}p @ {_new_tp_price:.5f}")
                                    except Exception as _rtp_e:
                                        logger.warning("Hybrid TP update failed %s: %s", self.trade_id, _rtp_e)
                                else:
                                    # Extended but momentum fading → lock here
                                    if not self._hybrid_tp_locked:
                                        self._hybrid_tp_locked = True
                                        self._hybrid_lock_pips = pnl_pips
                                        logger.info("🔒 [HYBRID TP] %s #%s: momentum fading — locking at +%.1fp | fan:%s bb:%s",
                                                    self.instrument, self.trade_id, pnl_pips,
                                                    _fan_state, "open" if _bb_expanding else "closing")

                    except Exception as _ratch_err:
                        # 2026-04-24: upgraded — hybrid TP ratchet locks profit on snipes.
                        # Silent failure = no profit lock, trade runs against peak.
                        logger.warning("Hybrid TP ratchet error %s: %s: %s (profit lock inactive)",
                                       self.trade_id, type(_ratch_err).__name__, _ratch_err)
                    # Skip partial-close and ATR-trailing — hybrid TP handles exit for snipe trades

                # 1. Partial take-profit at 1.0R (or configured level) — non-snipe trades only
                if not self._ratchet_tp_active and not self._partial_taken and r_mult >= partial_exit_rr:
                    # Close partial position
                    partial_units = int(abs(self.units) * partial_exit_ratio)
                    if partial_units > 0:
                        try:
                            await loop.run_in_executor(None, lambda: self._client.close_trade(
                                self.trade_id, units=str(partial_units)
                            ))
                            
                            self._partial_taken = True
                            self.units = int(abs(self.units) * (1.0 - partial_exit_ratio))
                            
                            if flight:
                                flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                              trade_id=self.trade_id, data={
                                    "action": "partial_take_profit",
                                    "partial_units": partial_units,
                                    "remaining_units": self.units,
                                    "r_multiple": r_mult,
                                    "partial_ratio": partial_exit_ratio,
                                }, note=f"Partial TP at {r_mult:.1f}R - closed {partial_exit_ratio:.0%}")
                            
                            logger.info("Trade %s: Partial TP at %.1fR - closed %d units (%.0f%%), %d remaining",
                                       self.trade_id, r_mult, partial_units, partial_exit_ratio * 100, self.units)
                        except Exception as e:
                            logger.error("Failed to execute partial TP for trade %s: %s", self.trade_id, e)
                
                # 2. Move SL to breakeven — ALL trades (ratchet TP manages the top end, SL-to-BE manages floor)
                if not self._sl_moved_to_be and r_mult >= trailing_activation_rr:
                    buffer_pips = self._params["guardian.sl_buffer_pips"]  # 3 pip buffer (was 1 — too tight, gets stopped on spread noise)
                    if is_long:
                        new_sl = self.entry_price + (buffer_pips * self.pip_size)
                    else:
                        new_sl = self.entry_price - (buffer_pips * self.pip_size)

                    # Enforce minimum ATR gap from current price even at breakeven.
                    # 2026-04-22: min_gap multiplier is now tunable. Default 1.0 preserves
                    # pre-existing behavior. Snipes override to 0.3 so the tight trail
                    # (snipe.guardian.trailing_atr_mult=0.1) actually activates instead
                    # of being neutralized by the 1.0 hardcoded floor.
                    _mkt_atr_be = market.get("atr", {})
                    _atr_be = _mkt_atr_be.get("value", _mkt_atr_be) if isinstance(_mkt_atr_be, dict) else _mkt_atr_be
                    if _atr_be and _atr_be > 0:
                        _cur_price = current_price if current_price > 0 else self.entry_price
                        _min_gap_mult_be = float(self._params.get("guardian.sl_min_gap_atr_mult", 1.0))
                        _min_be_gap = float(_atr_be) * _min_gap_mult_be
                        if is_long:
                            new_sl = min(new_sl, _cur_price - _min_be_gap)
                        else:
                            new_sl = max(new_sl, _cur_price + _min_be_gap)

                    new_sl = round(new_sl, self.display_precision)
                    
                    # Modify the trade's stop loss
                    await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                        self.trade_id, 
                        stop_loss={"price": str(new_sl), "timeInForce": "GTC"}
                    ))
                    
                    self.stop_loss = new_sl
                    self._sl_moved_to_be = True
                    
                    if flight:
                        flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                      trade_id=self.trade_id, data={
                            "action": "moved_sl_to_breakeven",
                            "new_sl": new_sl,
                            "pnl_pips": pnl_pips,
                            "r_multiple": r_mult,
                        }, note=f"Moved SL to breakeven at {r_mult:.1f}R")
                        
                    logger.info("Trade %s: Moved SL to breakeven at %.5f (%.1fR protection)",
                               self.trade_id, new_sl, r_mult)
                
                # 3. ATR-based trailing stop — non-snipe trades only
                elif not self._ratchet_tp_active and self._sl_moved_to_be and r_mult >= trailing_activation_rr:
                    # Get ATR value from market data
                    _mkt_atr = market.get("atr", {})
                    atr = _mkt_atr.get("value", _mkt_atr) if isinstance(_mkt_atr, dict) else _mkt_atr
                    if atr and atr > 0:
                        # Calculate ATR-based trailing stop from high water mark
                        atr_distance = float(atr) * trailing_atr_mult
                        
                        if is_long:
                            new_trail_sl = self._peak_price - atr_distance
                        else:
                            new_trail_sl = self._peak_price + atr_distance
                        
                        new_trail_sl = round(new_trail_sl, self.display_precision)

                        # Minimum breathing room: SL must be at least N× ATR from current price.
                        # 2026-04-22: N is now tunable via guardian.sl_min_gap_atr_mult (default 1.0).
                        # Snipes override to 0.3 so their tight trail (atr_mult=0.1) can actually
                        # activate instead of being neutralized by the 1.0 floor.
                        # 14d backtest: 1.0 → -149.7p, 0.3 → -32.5p (+117p improvement).
                        current_price = current_price if current_price > 0 else self.entry_price
                        _min_gap_mult = float(self._params.get("guardian.sl_min_gap_atr_mult", 1.0))
                        min_gap = float(atr) * _min_gap_mult
                        if is_long:
                            sl_floor = round(current_price - min_gap, self.display_precision)
                            new_trail_sl = min(new_trail_sl, sl_floor)
                        else:
                            sl_ceiling = round(current_price + min_gap, self.display_precision)
                            new_trail_sl = max(new_trail_sl, sl_ceiling)

                        # Only tighten - never widen
                        sl_is_better = False
                        if is_long:
                            sl_is_better = new_trail_sl > self.stop_loss
                        else:
                            sl_is_better = new_trail_sl < self.stop_loss
                            
                        if sl_is_better and new_trail_sl != self._last_trail_sl:
                            await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                                self.trade_id,
                                stop_loss={"price": str(new_trail_sl), "timeInForce": "GTC"}
                            ))
                            
                            self.stop_loss = new_trail_sl
                            self._last_trail_sl = new_trail_sl
                            
                            if flight:
                                flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                              trade_id=self.trade_id, data={
                                    "action": "atr_trailing_stop",
                                    "new_sl": new_trail_sl,
                                    "peak_price": self._peak_price,
                                    "atr": atr,
                                    "atr_multiplier": trailing_atr_mult,
                                }, note=f"ATR trailing stop: {trailing_atr_mult}×ATR from peak")
                                
                            logger.info("Trade %s: ATR trailing stop to %.5f (%.1f×ATR=%.5f from peak %.5f)",
                                       self.trade_id, new_trail_sl, trailing_atr_mult, atr_distance, self._peak_price)

            # 4. Profit giveback protection (always active regardless of auto_profit setting)
            # If you were solidly in profit and gave most of it back, close before it becomes a loss.
            # Scales by threat: GREEN needs 70% giveback, YELLOW 60%, RED/BLACK 50%.
            threat_zone = threat.get('zone', 'GREEN')
            _giveback_thresholds = {'GREEN': 70.0, 'YELLOW': 60.0, 'RED': 50.0, 'BLACK': 40.0}
            _giveback_threshold = _giveback_thresholds.get(threat_zone, 70.0)
            # Minimum peak: at least 3 pips AND $1.50 — don't trigger on noise
            _min_peak_pips = 3.0
            _min_peak_usd = 1.50
            # Don't fire giveback if we're within 2 pips of TP — just let it hit
            _tp_price = float(self.take_profit) if self.take_profit else None
            _pips_to_tp = abs(_tp_price - current_price) / self.pip_size if _tp_price and current_price > 0 else 999
            _near_tp = _pips_to_tp <= 2.0

            if (not _near_tp and
                giveback_pct > _giveback_threshold and pnl_pips >= 4.0 and
                self._peak_pnl_pips >= _min_peak_pips and
                self._peak_unrealized_pl >= _min_peak_usd):
                
                logger.warning("Trade %s: Profit giveback >50%% with %s threat - CLOSING",
                              self.trade_id, threat_zone)
                
                # Close the trade immediately
                await self._close_with_reason("profit_giveback")
                
                if flight:
                    flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                  trade_id=self.trade_id, data={
                        "action": "profit_giveback_close",
                        "giveback_pct": giveback_pct,
                        "peak_pips": self._peak_pnl_pips,
                        "current_pips": pnl_pips,
                        "threat_zone": threat_zone,
                    }, status="warn", note=f"Profit giveback {giveback_pct:.1f}% — closing")

            # ══════════════════════════════════════════════════════════════════════
            # EARLY ADVERSE-EXCURSION CUT (2026-04-30)
            # ══════════════════════════════════════════════════════════════════════
            #
            # Catches fast-failing snipe/scout trades within first ~60 minutes.
            # Complementary to structural_fan_failure below (which fires after
            # 4+ hrs / ≥16 M15 bars).
            #
            # Rule: if max_adverse_pips >= 10 by M15 bar 4 of trade life, exit now.
            # Excluded pairs: EUR_AUD, AUD_USD — these have deep-retrace recovery
            # archetypes where the rule actively hurts (per 90d × 14pair backtest).
            #
            # Validation (scripts/loss_signature_finder.py, 90d × 14pair × 8 folds):
            #   - 83.5% precision (when fires, mostly real losses)
            #   - 7.2% winkill rate (preserves edge)
            #   - Mean +14p/fold (sd 17p), 7 of 8 folds positive
            #   - +112p NetSwing over 90 days
            #
            # Tunables (tuning_config.py):
            #   guardian.adv_cut_enabled         - master switch
            #   guardian.adv_cut_pips            - threshold (default 10p)
            #   guardian.adv_cut_by_bar          - bar window (default 4 = 60min)
            #   guardian.adv_cut_excluded_pairs  - per-pair exclusion list
            #   guardian.adv_cut_require_e55_break - structural guard (default True, 2026-05-06)
            #
            # Structural guard (added 2026-05-06 after 30-day audit by claude-code):
            # When True, adv_cut requires ALL of:
            #   • self._retrace_zone in ('e55_retrace','e100_broken')  ← M15 candle is past E55
            #   • self._e21_crossed_e55_against == False               ← fan still ordered
            # 30-day replay vs winners+losers: drops winner-kill from 10.4%→1.5%,
            # still saves ~30% of large losses (the structural-break archetype).
            # Reuses existing retrace_zone tracking from candle-to-EMA system (line 3145+).
            try:
                _adv_cut_enabled = self._params.get("guardian.adv_cut_enabled", True)
                _adv_cut_excluded = self._params.get(
                    "guardian.adv_cut_excluded_pairs", ["EUR_AUD", "AUD_USD"])
                _adv_cut_pips = float(self._params.get("guardian.adv_cut_pips", 10.0))
                _adv_cut_by_bar = int(self._params.get("guardian.adv_cut_by_bar", 4))
                _adv_cut_require_struct = bool(self._params.get(
                    "guardian.adv_cut_require_e55_break", True))
                # 2026-05-07 BUGFIX (claude-code): _candles_in_trade resets to 0
                # on every watcher respawn (reconcile recreates watcher when
                # OANDA briefly omits the trade or task dies). Respawns happen
                # every 15-60 min, so a 5-hour-old trade was being re-tested as
                # "bar 1" repeatedly — adv_cut fired on EUR_USD #13705 + NZD_USD
                # #13713 today (5h alive each, both kept by structural guard for
                # hours, then killed when one respawn aligned with e55_retrace).
                # Use trade-age-from-entry_time so the bar window is tied to
                # actual trade lifetime, not watcher lifetime.
                try:
                    _trade_age_s = (datetime.now(timezone.utc) - self.entry_time).total_seconds()
                    _adv_m15_bars = max(0, int(_trade_age_s // 900))  # 900s = 15 min
                except Exception:
                    _adv_m15_bars = max(0, self._candles_in_trade // 15)  # fallback

                # Existing PnL-based gate
                _pnl_gate = (_adv_cut_enabled
                             and self.source in ("snipe_direct", "scout")
                             and self.instrument not in _adv_cut_excluded
                             and 1 <= _adv_m15_bars <= _adv_cut_by_bar
                             and self._max_adverse_pips >= _adv_cut_pips
                             and pnl_pips < 0)

                # Structural gate — reuses existing retrace_zone + fan-cross tracking
                _retrace_zone = getattr(self, "_retrace_zone", "trending")
                _fan_intact = not getattr(self, "_e21_crossed_e55_against", False)
                _past_e55 = _retrace_zone in ("e55_retrace", "e100_broken")
                # 2026-05-13 (Tim approved): NEVER fire adv_cut while fan is intact.
                # 9/9 historical adv-cuts were losers (-$882) — all fired during normal
                # retraces in healthy bearish fans. Today: GBP_JPY 15179 cut at -24.9p
                # with fan_intact=True (logged), trend never reversed. Per Tim:
                # "the fan is there until the EMAs cross, if they don't ever cross
                # the trend will cont at some point". Require explicit fan FAILURE.
                _struct_gate = (not _adv_cut_require_struct) or (
                    (not _fan_intact) and _past_e55)

                if _pnl_gate and _struct_gate:
                    logger.warning(
                        "⚡ [GUARDIAN] %s #%s: adv-cut — MAE=%.1fp by M15 bar %d "
                        "(>=%.1fp threshold), zone=%s fan_intact=%s, exiting (pnl=%.1fp)",
                        self.instrument, self.trade_id,
                        self._max_adverse_pips, _adv_m15_bars, _adv_cut_pips,
                        _retrace_zone, _fan_intact, pnl_pips,
                    )
                    if flight:
                        flight.record(
                            FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                            trade_id=self.trade_id, status="warn", data={
                                "action": "adverse_excursion_cut",
                                "max_adverse_pips": round(self._max_adverse_pips, 1),
                                "m15_bars": _adv_m15_bars,
                                "threshold_pips": _adv_cut_pips,
                                "pnl_pips": round(pnl_pips, 1),
                                "source": self.source,
                                "retrace_zone": _retrace_zone,
                                "fan_intact": _fan_intact,
                            },
                            note=(f"adv-cut MAE={self._max_adverse_pips:.1f}p "
                                  f"bar={_adv_m15_bars} zone={_retrace_zone} "
                                  f"fan_intact={_fan_intact} pnl={pnl_pips:+.1f}p"),
                        )
                    await self._close_with_reason("adverse_excursion_cut")
                    return  # short-circuit further guardian checks this tick

                # Observability: log when PnL gate would have fired but structural
                # guard spared the trade. Once per trade (track via flag) to avoid spam.
                if _pnl_gate and not _struct_gate and not getattr(self, "_adv_cut_struct_skip_logged", False):
                    self._adv_cut_struct_skip_logged = True
                    logger.info(
                        "🛡️ [GUARDIAN] %s #%s: adv-cut SKIPPED by structural guard — "
                        "MAE=%.1fp bar=%d zone=%s fan_intact=%s — let SL handle",
                        self.instrument, self.trade_id,
                        self._max_adverse_pips, _adv_m15_bars, _retrace_zone, _fan_intact,
                    )
                    if flight:
                        flight.record(
                            FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                            trade_id=self.trade_id, status="ok", data={
                                "action": "adv_cut_skipped_structural",
                                "max_adverse_pips": round(self._max_adverse_pips, 1),
                                "m15_bars": _adv_m15_bars,
                                "pnl_pips": round(pnl_pips, 1),
                                "retrace_zone": _retrace_zone,
                                "fan_intact": _fan_intact,
                                "reason": ("fan_disordered" if not _fan_intact
                                           else "price_not_past_e55"),
                            },
                            note=(f"adv-cut spared: zone={_retrace_zone} "
                                  f"fan_intact={_fan_intact} MAE={self._max_adverse_pips:.1f}p"),
                        )
            except Exception as _adv_exc:
                logger.warning("Adv-cut check failed for %s: %s", self.trade_id, _adv_exc)

            # ══════════════════════════════════════════════════════════════════════
            # 4b. FAILED-RALLY BREAKEVEN LOCK (added 2026-05-08, claude-code, Tim approved)
            # ══════════════════════════════════════════════════════════════════════
            # Catches "negative-then-brief-positive-then-collapse" pattern that hit
            # Tim hard 05-07/05-08 (13705 EUR_USD -10.2p, 13713 NZD_USD -16p,
            # 13727 AUD_USD -30.4p, 13743 AUD_JPY -26.7p). All 4 had a brief
            # positive M15 close that immediately reversed. The existing exit logic
            # had nothing for sub-+5p peaks; profit floor only engages at +5p+.
            #
            # State machine (per watcher, advances ONLY on new M15 closes):
            #   normal -> earned   on N consecutive negative M15 closes
            #   earned -> pos_seen on first positive M15 close
            #   pos_seen -> locked on first negative M15 close after pos_seen
            #   locked: exit at entry+lock_pips when adverse extreme touches lock
            #
            # Tunables (defaults catch all 4 today + 6 of 6 brief-positive big losses
            # post-tune; expected -1 large winner per 3 weeks):
            #   guardian.failed_rally_lock_enabled (True)
            #   guardian.failed_rally_min_neg_bars (1)   N=1: ANY prior negative arms
            #   guardian.failed_rally_lock_pips    (0.0) lock at breakeven
            #
            # Backtest (post-tune 21d): saves 9 losses (+152.9p), kills 10 winners
            # (-62.5p), NET +76.8p. 90d: +134.3p. See scripts/failed_rally_test.py.
            try:
                # 2026-05-11: Read failed_rally_* tunables directly from tuning_config
                # because self._params is a hand-built dict at line ~1258 that does
                # NOT include these params — making the tuning_overrides #307/#308
                # for `guardian.failed_rally_lock_enabled` no-op. Default flipped
                # True → False per Tim's directive ("turn it off for now") until
                # the rewrite (V_clf65 classifier) lands live.
                from tuning_config import tc_get_for_trade as _tc_fr
                _fr_enabled = bool(_tc_fr("guardian.failed_rally_lock_enabled", self.source, False))
                _fr_min_neg = int(_tc_fr("guardian.failed_rally_min_neg_bars", self.source, 1))
                _fr_lock_pips = float(_tc_fr("guardian.failed_rally_lock_pips", self.source, 0.0))

                # Compute lock price once (can change if tunable changes mid-trade,
                # but we set on first lock arming below)
                if self._fr_lock_price is None:
                    self._fr_lock_price = self.entry_price + (_fr_lock_pips * self.pip_size if is_long else -_fr_lock_pips * self.pip_size)

                # Detect new M15 close: when latest M15 bar time differs from last
                # we processed, the previous bar JUST closed.
                _fr_latest_m15 = ""
                if self._m15_buffer:
                    _fr_latest_m15 = self._m15_buffer[-1].get('time', '') or ""
                _fr_new_m15 = bool(_fr_latest_m15) and _fr_latest_m15 != self._fr_last_m15_time

                # State machine update on new M15 close (uses pnl_pips at moment of bar close)
                if _fr_enabled and _fr_new_m15 and self._fr_state != 'locked':
                    self._fr_last_m15_time = _fr_latest_m15
                    if pnl_pips < 0:
                        self._fr_consec_neg += 1
                        if self._fr_state == 'normal' and self._fr_consec_neg >= _fr_min_neg:
                            self._fr_state = 'earned'
                        elif self._fr_state == 'pos_seen':
                            # Failed rally — lock now at entry + lock_pips
                            self._fr_state = 'locked'
                            self._fr_lock_bar_time = _fr_latest_m15
                            logger.warning(
                                "🔒 [GUARDIAN] %s #%s: FAILED-RALLY LOCK armed at +%.1fp "
                                "after %d-bar neg → pos → neg pattern (pnl=%.1fp)",
                                self.instrument, self.trade_id, _fr_lock_pips,
                                self._fr_consec_neg, pnl_pips,
                            )
                            if flight:
                                flight.record(
                                    FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                    trade_id=self.trade_id, status="warn", data={
                                        "action": "failed_rally_lock_armed",
                                        "lock_pips": _fr_lock_pips,
                                        "consec_neg_at_arm": self._fr_consec_neg,
                                        "pnl_pips": round(pnl_pips, 1),
                                    },
                                    note=(f"failed-rally lock armed at "
                                          f"+{_fr_lock_pips:.1f}p (pnl={pnl_pips:+.1f}p)"),
                                )
                    elif pnl_pips > 0:
                        self._fr_consec_neg = 0
                        if self._fr_state == 'earned':
                            self._fr_state = 'pos_seen'

                # Lock-hit check on every tick (not just M15 closes): if locked, see
                # if current price has touched the lock. Use last M1 bar's adverse
                # extreme as the touch test.
                if _fr_enabled and self._fr_state == 'locked' and self._m1_buffer:
                    _fr_m1 = self._m1_buffer[-1]
                    _fr_h = float(_fr_m1.get('high', 0))
                    _fr_l = float(_fr_m1.get('low', 0))
                    _fr_hit = (_fr_l <= self._fr_lock_price) if is_long else (_fr_h >= self._fr_lock_price)
                    if _fr_hit:
                        logger.warning(
                            "🔒 [GUARDIAN] %s #%s: FAILED-RALLY LOCK HIT — exit at +%.1fp "
                            "(price touched %s)",
                            self.instrument, self.trade_id, _fr_lock_pips,
                            self._fr_lock_price,
                        )
                        if flight:
                            flight.record(
                                FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                trade_id=self.trade_id, status="warn", data={
                                    "action": "failed_rally_lock_hit",
                                    "lock_price": self._fr_lock_price,
                                    "lock_pips": _fr_lock_pips,
                                    "pnl_pips": round(pnl_pips, 1),
                                },
                                note=(f"failed-rally lock hit at +{_fr_lock_pips:.1f}p"),
                            )
                        await self._close_with_reason("failed_rally_lock")
                        return  # short-circuit further guardian checks
            except Exception as _fr_exc:
                logger.warning("Failed-rally lock check error for %s: %s",
                               self.trade_id, _fr_exc)

            # ══════════════════════════════════════════════════════════════════════
            # 4c. EXIT-MARKER EVENT-DRIVEN DUAL-MODE (2026-05-14 v2, claude-code, Tim approved)
            # ══════════════════════════════════════════════════════════════════════
            # Listens for NEW opposing ⚠ Exit (peak_sep) markers that APPEAR during
            # the live trade. Baseline = set of opposing markers at trade open.
            # On each new M15 bar close, recompute marker set and diff vs baseline.
            #
            # When a NEW opposing marker appears:
            #   • pnl > 0  → take profit NOW at current price (book the top)
            #   • pnl <= 0 → tighten SL to current_close - 1p adverse buffer
            #                  (let recovery happen; SL hit only if it drifts more)
            #
            # Listening window: first N M15 bars (default 15). Past that, other
            # guardian rules (profit floor / structural exit) own the lifecycle.
            #
            # 30d backtest (200 non-kronos trades, watch=15):
            #   snipe_direct: 30 fires, 24 helped +245p / 0 hurt — clean
            #   scout:        28 fires, 19 SL-helped + 5 TP +29p / 12 hurt -28p
            #   TOTAL:        60 fires, NET +373.1p
            # See scripts/backtest_marker_appears_during_trade.py.
            try:
                from tuning_config import tc_get_for_trade as _tc_em
                _em_enabled = bool(_tc_em("guardian.exit_marker_be_enabled", self.source, True))
                _em_excluded = _tc_em("guardian.exit_marker_be_excluded_sources", self.source, ["kronos_hunter"])

                if (
                    _em_enabled
                    and self.source not in (_em_excluded or [])
                    and self._m15_buffer
                    and len(self._m15_buffer) >= 110  # E100 needs valid runway
                    and not self._em_be_armed         # if SL already tightened, stop re-evaluating
                ):
                    _em_window = int(_tc_em("guardian.exit_marker_be_window_bars", self.source, 15))
                    _em_neg_buffer = float(_tc_em("guardian.exit_marker_neg_lock_buffer_pips", self.source, 1.0))

                    from backtester.ema_separation import format_chart_signals

                    # Detect new M15 close: latest bar time differs from last we evaluated
                    _em_latest = self._m15_buffer[-1].get("time", "") or ""
                    _em_new_close = bool(_em_latest) and _em_latest != self._em_last_eval_m15_time

                    if _em_new_close:
                        self._em_last_eval_m15_time = _em_latest

                        # Locate entry bar index
                        _em_entry_str = (
                            self.entry_time.isoformat()
                            if hasattr(self.entry_time, "isoformat") else str(self.entry_time)
                        )
                        _em_entry_idx = None
                        for _ei, _ec in enumerate(self._m15_buffer):
                            _et = _ec.get("time", "")
                            if _et and _et >= _em_entry_str:
                                _em_entry_idx = _ei
                                break
                        if _em_entry_idx is None:
                            _em_entry_idx = len(self._m15_buffer) - 1

                        _em_oppose = "sell" if is_long else "buy"

                        # Baseline snapshot at trade open (first eligible tick)
                        if self._em_baseline_marker_times is None:
                            _em_baseline_sub = self._m15_buffer[: _em_entry_idx + 1]
                            _em_b_signals = format_chart_signals(_em_baseline_sub) or []
                            self._em_baseline_marker_times = {
                                s.get("time") for s in _em_b_signals
                                if s.get("type") == "peak_sep" and s.get("direction") == _em_oppose
                            }
                            self._em_baseline_m15_count = len(_em_baseline_sub)
                            logger.info(
                                "🎯 [GUARDIAN] %s #%s: exit-marker baseline = %d opposing peak_sep markers",
                                self.instrument, self.trade_id,
                                len(self._em_baseline_marker_times),
                            )

                        # Window check: how many M15 bars since entry?
                        _em_bars_in_trade = max(0, len(self._m15_buffer) - 1 - _em_entry_idx)
                        if _em_bars_in_trade <= _em_window:
                            # Recompute markers on current buffer and diff vs baseline
                            _em_current_signals = format_chart_signals(self._m15_buffer) or []
                            _em_current_oppose = {
                                s.get("time") for s in _em_current_signals
                                if s.get("type") == "peak_sep" and s.get("direction") == _em_oppose
                            }
                            _em_new_markers = _em_current_oppose - self._em_baseline_marker_times

                            if _em_new_markers:
                                # FIRE — dual mode based on current pnl
                                self._em_fire_bar = _em_bars_in_trade
                                _em_bar_close = float(self._m15_buffer[-1].get("close", 0))

                                if pnl_pips > 0:
                                    # TAKE PROFIT mode — book the top
                                    logger.warning(
                                        "🎯 [GUARDIAN] %s #%s: EXIT-MARKER TAKE-PROFIT — "
                                        "new opposing peak_sep at bar +%d, pnl=%+.1fp → close",
                                        self.instrument, self.trade_id,
                                        _em_bars_in_trade, pnl_pips,
                                    )
                                    if flight:
                                        flight.record(
                                            FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                            trade_id=self.trade_id, status="warn", data={
                                                "action": "exit_marker_take_profit",
                                                "fire_bar": _em_bars_in_trade,
                                                "pnl_pips": round(pnl_pips, 1),
                                                "new_marker_count": len(_em_new_markers),
                                                "source": self.source,
                                            },
                                            note=(f"exit-marker TP at +{pnl_pips:.1f}p "
                                                  f"(bar +{_em_bars_in_trade})"),
                                        )
                                    await self._close_with_reason("exit_marker_tp")
                                    return
                                else:
                                    # IN-LOSS branch — gated by guardian.exit_marker_in_loss_action
                                    # 'tighten' (legacy 2026-05-14): SL→current_close±buffer, let recovery happen
                                    # 'kill'    (audit    2026-05-17): close at market — +63p/30d net vs tighten
                                    _em_in_loss_action = str(_tc_em(
                                        "guardian.exit_marker_in_loss_action", self.source, "tighten"
                                    )).lower()

                                    if _em_in_loss_action == "kill":
                                        # KILL-AT-MARKET mode
                                        logger.warning(
                                            "🎯 [GUARDIAN] %s #%s: EXIT-MARKER IN-LOSS KILL — "
                                            "new opposing peak_sep at bar +%d, pnl=%+.1fp → close at market",
                                            self.instrument, self.trade_id,
                                            _em_bars_in_trade, pnl_pips,
                                        )
                                        if flight:
                                            flight.record(
                                                FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                                trade_id=self.trade_id, status="warn", data={
                                                    "action": "exit_marker_in_loss_kill",
                                                    "fire_bar": _em_bars_in_trade,
                                                    "pnl_pips": round(pnl_pips, 1),
                                                    "new_marker_count": len(_em_new_markers),
                                                    "source": self.source,
                                                },
                                                note=(f"exit-marker in-loss kill at pnl={pnl_pips:+.1f}p "
                                                      f"(bar +{_em_bars_in_trade})"),
                                            )
                                        await self._close_with_reason("exit_marker_in_loss_kill")
                                        return
                                    else:
                                        # SL-TIGHTEN mode (legacy) — set SL near current, let recovery happen
                                        self._em_be_armed = True
                                        self._em_be_lock_price = _em_bar_close - (
                                            _em_neg_buffer * self.pip_size if is_long
                                            else -_em_neg_buffer * self.pip_size
                                        )
                                        logger.warning(
                                            "🎯 [GUARDIAN] %s #%s: EXIT-MARKER SL-TIGHTEN — "
                                            "new opposing peak_sep at bar +%d, pnl=%+.1fp → "
                                            "SL → %.5f (current_close - %.1fp)",
                                            self.instrument, self.trade_id,
                                            _em_bars_in_trade, pnl_pips,
                                            self._em_be_lock_price, _em_neg_buffer,
                                        )
                                        if flight:
                                            flight.record(
                                                FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                                trade_id=self.trade_id, status="warn", data={
                                                    "action": "exit_marker_sl_tighten",
                                                    "fire_bar": _em_bars_in_trade,
                                                    "pnl_pips": round(pnl_pips, 1),
                                                    "lock_price": self._em_be_lock_price,
                                                    "neg_buffer_pips": _em_neg_buffer,
                                                    "new_marker_count": len(_em_new_markers),
                                                    "source": self.source,
                                                },
                                                note=(f"exit-marker SL-tighten at pnl={pnl_pips:+.1f}p "
                                                      f"→ {self._em_be_lock_price:.5f}"),
                                            )

                # Lock-hit check (only fires after SL-tighten armed via NEG path)
                if self._em_be_armed and self._em_be_lock_price is not None and self._m1_buffer:
                    _em_m1 = self._m1_buffer[-1]
                    _em_h = float(_em_m1.get("high", 0))
                    _em_l = float(_em_m1.get("low", 0))
                    _em_hit = (_em_l <= self._em_be_lock_price) if is_long else (_em_h >= self._em_be_lock_price)
                    if _em_hit:
                        logger.warning(
                            "🎯 [GUARDIAN] %s #%s: EXIT-MARKER SL-HIT — exit at tightened SL "
                            "(pnl=%+.1fp, fire_bar=+%d)",
                            self.instrument, self.trade_id, pnl_pips, self._em_fire_bar or 0,
                        )
                        if flight:
                            flight.record(
                                FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                trade_id=self.trade_id, status="warn", data={
                                    "action": "exit_marker_sl_hit",
                                    "lock_price": self._em_be_lock_price,
                                    "fire_bar": self._em_fire_bar,
                                    "pnl_pips": round(pnl_pips, 1),
                                    "source": self.source,
                                },
                                note=(f"exit-marker SL hit at pnl={pnl_pips:+.1f}p"),
                            )
                        await self._close_with_reason("exit_marker_sl")
                        return
            except Exception as _em_exc:
                logger.warning("Exit-marker event check error for %s: %s",
                               self.trade_id, _em_exc)

            # ══════════════════════════════════════════════════════════════════════
            # 4d. REAL-TIME LOSER-PATTERN DETECTOR (2026-05-15, claude-code, Tim approved)
            # ══════════════════════════════════════════════════════════════════════
            # Catches "entered late into exhaustion, riding the retrace" pattern —
            # behavioral counterpart to exit_marker which is chart-structural.
            #
            # Fires on M15 bar close when ALL true on bars after entry:
            #   • MFE so far ≤ 2p (trade essentially never went positive)
            #   • adv_streak ≥ 3 (3 consecutive M15 closes adverse from entry)
            #   • RSI moved ≥5 pts AGAINST trade direction over last 3 bars
            #   • bar pnl_close in [-20, -1] (early-warning zone, not catastrophe)
            #
            # Action: SL → break-even (entry price). Watch M1 for breach.
            #   • Price retraces to entry → trade exits flat (saves the loss)
            #   • Price recovers to TP → trade keeps the win
            #
            # 30d backtest (259 trades, scripts/audit_30d_constrained_rule.py):
            #   snipe_direct: 34/58 losers caught, 13/109 winners flagged → +275.8p NET
            #   scout:        10/37 losers, 7/50 winners → +26.4p NET (marginal)
            # Initially restricted to snipe_direct via guardian.rt_loser_pattern_sources.
            try:
                from tuning_config import tc_get_for_trade as _tc_rt
                _rt_enabled = bool(_tc_rt("guardian.rt_loser_pattern_enabled", self.source, False))
                _rt_sources = _tc_rt("guardian.rt_loser_pattern_sources", self.source, ["snipe_direct"]) or []

                if (
                    _rt_enabled
                    and self.source in _rt_sources
                    and self._m15_buffer
                    and len(self._m15_buffer) >= 20  # need lookback for RSI(14)+3-bar dir
                    and not getattr(self, '_rt_loser_armed', False)
                ):
                    _rt_mfe_max = float(_tc_rt("guardian.rt_loser_pattern_mfe_max_pips", self.source, 2.0))
                    _rt_adv_min = int(_tc_rt("guardian.rt_loser_pattern_adv_streak", self.source, 3))
                    _rt_rsi_min = float(_tc_rt("guardian.rt_loser_pattern_rsi_dir_min", self.source, 5.0))
                    _rt_pnl_low = float(_tc_rt("guardian.rt_loser_pattern_pnl_low", self.source, -20.0))
                    _rt_pnl_high = float(_tc_rt("guardian.rt_loser_pattern_pnl_high", self.source, -1.0))

                    _rt_latest = self._m15_buffer[-1].get("time", "") or ""
                    _rt_new_close = bool(_rt_latest) and _rt_latest != self._rt_loser_last_eval_m15_time

                    if _rt_new_close:
                        self._rt_loser_last_eval_m15_time = _rt_latest

                        # Locate entry bar index in buffer
                        _rt_entry_str = (
                            self.entry_time.isoformat()
                            if hasattr(self.entry_time, "isoformat") else str(self.entry_time)
                        )
                        _rt_entry_idx = None
                        for _ei, _ec in enumerate(self._m15_buffer):
                            _et = _ec.get("time", "")
                            if _et and _et >= _rt_entry_str:
                                _rt_entry_idx = _ei
                                break

                        if _rt_entry_idx is not None:
                            _rt_bars_after_entry = len(self._m15_buffer) - 1 - _rt_entry_idx

                            # Need ≥3 fully closed bars AFTER entry to compute adv_streak
                            if _rt_bars_after_entry >= 3:
                                _ep = self.entry_price
                                _pip = self.pip_size

                                # Compute MFE + adv_streak across bars from entry onward
                                _rt_mfe = 0.0
                                _rt_adv = 0
                                _rt_last_close_pnl = 0.0
                                for _i in range(_rt_entry_idx, len(self._m15_buffer)):
                                    _b = self._m15_buffer[_i]
                                    try:
                                        _bh = float(_b.get("high", 0))
                                        _bl = float(_b.get("low", 0))
                                        _bc = float(_b.get("close", 0))
                                    except (TypeError, ValueError):
                                        continue
                                    if not _bc: continue
                                    _hi_pnl = ((_bh - _ep) if is_long else (_ep - _bl)) / _pip
                                    _cl_pnl = ((_bc - _ep) if is_long else (_ep - _bc)) / _pip
                                    _rt_mfe = max(_rt_mfe, _hi_pnl)
                                    if _cl_pnl < 0:
                                        _rt_adv += 1
                                    else:
                                        _rt_adv = 0
                                    _rt_last_close_pnl = _cl_pnl

                                # Compute RSI direction over last 3 closes
                                _rt_closes = [
                                    float(_b.get("close", 0)) for _b in self._m15_buffer
                                    if _b.get("close")
                                ]
                                _rt_rsi_dir = None
                                if len(_rt_closes) >= 18:
                                    try:
                                        from backtester.ema_separation import _compute_rsi as _rsi_fn
                                        _rsi_now = _rsi_fn(_rt_closes, 14)
                                        _rsi_3ago = _rsi_fn(_rt_closes[:-3], 14)
                                        if _rsi_now is not None and _rsi_3ago is not None:
                                            _raw_dir = _rsi_now - _rsi_3ago
                                            # adverse = RSI moving against trade direction
                                            # for LONG adverse = RSI falling (negative raw_dir → positive adverse)
                                            # for SHORT adverse = RSI rising (positive raw_dir → positive adverse)
                                            _rt_rsi_dir = (-_raw_dir) if is_long else _raw_dir
                                    except Exception:
                                        _rt_rsi_dir = None

                                _rt_sig_fires = (
                                    _rt_mfe <= _rt_mfe_max
                                    and _rt_adv >= _rt_adv_min
                                    and _rt_rsi_dir is not None and _rt_rsi_dir >= _rt_rsi_min
                                    and _rt_pnl_low <= _rt_last_close_pnl <= _rt_pnl_high
                                )

                                if _rt_sig_fires:
                                    self._rt_loser_armed = True
                                    self._rt_loser_fire_bar = _rt_bars_after_entry
                                    logger.warning(
                                        "🚨 [GUARDIAN] %s #%s: RT-LOSER-PATTERN FIRED — "
                                        "MFE=%.1fp adv_streak=%d rsi_adv_dir=%+.1f bar=+%d "
                                        "pnl=%+.1fp → SL → BE (%.5f)",
                                        self.instrument, self.trade_id,
                                        _rt_mfe, _rt_adv, _rt_rsi_dir, _rt_bars_after_entry,
                                        pnl_pips, self.entry_price,
                                    )
                                    if flight:
                                        flight.record(
                                            FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                            trade_id=self.trade_id, status="warn", data={
                                                "action": "rt_loser_pattern_sl_be",
                                                "fire_bar": _rt_bars_after_entry,
                                                "pnl_pips": round(pnl_pips, 1),
                                                "mfe_pips": round(_rt_mfe, 1),
                                                "adv_streak": _rt_adv,
                                                "rsi_adverse_dir": round(_rt_rsi_dir, 1),
                                                "be_price": self.entry_price,
                                                "source": self.source,
                                            },
                                            note=(f"rt_loser_pattern bar+{_rt_bars_after_entry} "
                                                  f"mfe={_rt_mfe:.1f}p adv={_rt_adv} "
                                                  f"rsi_adv={_rt_rsi_dir:+.1f} → SL→BE"),
                                        )

                # BE-hit check: armed and M1 has crossed entry → exit at break-even
                if getattr(self, '_rt_loser_armed', False) and self._m1_buffer:
                    try:
                        _rt_m1 = self._m1_buffer[-1]
                        _rt_h = float(_rt_m1.get("high", 0))
                        _rt_l = float(_rt_m1.get("low", 0))
                    except (TypeError, ValueError):
                        _rt_h = _rt_l = 0.0
                    _rt_be_hit = (_rt_l <= self.entry_price) if is_long else (_rt_h >= self.entry_price)
                    if _rt_be_hit and _rt_h > 0 and _rt_l > 0:
                        logger.warning(
                            "🚨 [GUARDIAN] %s #%s: RT-LOSER BE HIT — exit at break-even "
                            "(pnl=%+.1fp, fire_bar=+%d)",
                            self.instrument, self.trade_id, pnl_pips,
                            getattr(self, '_rt_loser_fire_bar', None) or 0,
                        )
                        if flight:
                            flight.record(
                                FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                trade_id=self.trade_id, status="warn", data={
                                    "action": "rt_loser_pattern_be_hit",
                                    "fire_bar": getattr(self, '_rt_loser_fire_bar', None),
                                    "pnl_pips": round(pnl_pips, 1),
                                    "source": self.source,
                                },
                                note=f"rt_loser_pattern BE hit at pnl={pnl_pips:+.1f}p",
                            )
                        await self._close_with_reason("rt_loser_pattern_be")
                        return
            except Exception as _rt_exc:
                logger.warning("RT loser-pattern check error for %s: %s",
                               self.trade_id, _rt_exc)

            # ══════════════════════════════════════════════════════════════════════
            # 5+6. RETRACE-AWARE STRUCTURAL EXIT SYSTEM
            # ══════════════════════════════════════════════════════════════════════
            #
            # REPLACES: never-positive (fixed pip threshold) + safety net (fixed trigger)
            # BACKTEST (49 trades, 2 weeks): actual -81.7 pips → new logic +64.7 pips
            #           30 improved / 16 worsened / 3 unchanged
            #
            # PHILOSOPHY:
            #   Tim's trades almost always enter during retracement — they go negative
            #   first then positive. Fixed pip thresholds fire during the normal dip.
            #   The market structure tells us whether the dip is a retrace or a reversal:
            #
            #   RETRACE (HOLD):   BB + EMA both contracting = normal pullback → stay in
            #   REVERSAL (EXIT):  Fan failure outside retracing state = trend broke → exit
            #
            # EXIT SIGNALS (M15-based, structural not pip-based):
            #   A. Fan failure — E21 crosses E55 against trade while NOT in retracing state
            #      Min hold: 8 M15 bars (2 hours) before this fires
            #   B. Deceleration — M15 separation growth slowing, outside retracement
            #   C. Peak separation — 3 M15 bars before peak (pre-emptive)
            #
            # SAFETY FLOOR: SL/TP set by Tim on entry are honoured as hard floors.
            # Emergency BLACK zone (≥75) still fires unconditionally.

            try:
                from backtester.ema_separation import (
                    calculate_ema as _calc_ema,
                    measure_separation as _measure_sep,
                )
                import pandas as _pd_exit

                # ── Use M15 buffer for all structural calculations ────────────────
                _m15 = self._m15_buffer or []
                _min_m15_hold = 8  # must have 8 M15 bars (2 hrs) before structural exit fires

                if len(_m15) >= 30:
                    _m15_closes = [float(c['close']) for c in _m15]
                    _e21_m15 = _calc_ema(_m15_closes, 21)
                    _e55_m15 = _calc_ema(_m15_closes, 55)

                    # ── Track how many M15 bars since watcher spawned ─────────────
                    _m15_bars_elapsed = max(0, self._candles_in_trade // 15)

                    # ── Current retrace state (from guardian state machine) ────────
                    _in_retrace = (self._retrace_state == 'retracing')

                    # ── Fan ordering on M15 ───────────────────────────────────────
                    _e21_cur = float(_e21_m15[-1]) if _e21_m15[-1] else 0
                    _e55_cur = float(_e55_m15[-1]) if _e55_m15[-1] else 0
                    _fan_ordered_m15 = (
                        (is_long  and _e21_cur > _e55_cur > 0) or
                        (not is_long and _e21_cur < _e55_cur and _e55_cur > 0)
                    ) if _e21_cur and _e55_cur else True

                    # ── Use cached M15 signals (recomputed only on new M15 bar) ───
                    # Cache is refreshed in _fetch_m15() when buffer length changes.
                    _cache     = self._m15_signal_cache
                    _last_bar  = len(_m15) - 1
                    _decel_now  = _last_bar in _cache.get('decel_bars', set())
                    _peak_now   = _last_bar in _cache.get('peak_bars', set())
                    _return_now = _last_bar in _cache.get('return_exit_bars', set())

                    # ── SIGNAL A: Fan failure (outside retracement, min hold met) ─
                    _fan_failed = (
                        not _fan_ordered_m15
                        and not _in_retrace               # NOT during expected pullback
                        and _m15_bars_elapsed >= _min_m15_hold  # trade old enough
                        and not self._sl_moved_to_be      # SL not already at BE
                    )

                    # ── SIGNAL B: Deceleration outside retracement ────────────────
                    _decel_exit = (
                        _decel_now
                        and not _in_retrace
                        and _m15_bars_elapsed >= _min_m15_hold
                    )

                    # ── SIGNAL C: Peak separation (always fires when detected) ─────
                    _peak_exit = (
                        _peak_now
                        and _m15_bars_elapsed >= _min_m15_hold
                    )

                    # ── SIGNAL D: Price returned to E100 (◼ Back to E100) ────────
                    # Price was away from E100 (>0.12%) and came back (<0.04%).
                    # The move is complete — close if profitable, watch if not.
                    _return_exit = (
                        _return_now
                        and _m15_bars_elapsed >= _min_m15_hold
                    )

                    # ── ACT on signals (priority order) ──────────────────────────
                    _structural_signal = None
                    if _peak_exit:
                        _structural_signal = 'peak_separation'
                    elif _return_exit:
                        _structural_signal = 'return_to_e100'
                    elif _decel_exit:
                        _structural_signal = 'deceleration'
                    elif _fan_failed:
                        _structural_signal = 'fan_failure'

                    if _structural_signal:
                        _atr_val_str = market.get('atr', {})
                        _atr_str = float(_atr_val_str.get('value', _atr_val_str)) if isinstance(_atr_val_str, dict) else float(_atr_val_str or 0)
                        _atr_pips_str = (_atr_str / self.pip_size) if _atr_str > 0 else 5.0
                        _buf = max(3.0, min(8.0, round(_atr_pips_str * 0.5, 1)))

                        if pnl_pips >= 2.0 and not self._sl_moved_to_be:
                            # In profit — tighten SL to lock in gains (ATR buffer)
                            _new_sl = (self.entry_price + _buf * self.pip_size) if is_long else (self.entry_price - _buf * self.pip_size)
                            _new_sl = round(_new_sl, self.display_precision)
                            _should_move = (is_long and _new_sl > self.stop_loss) or (not is_long and _new_sl < self.stop_loss)
                            if _should_move:
                                try:
                                    await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                                        self.trade_id, stop_loss={"price": str(_new_sl), "timeInForce": "GTC"}
                                    ))
                                    self.stop_loss = _new_sl
                                    self._sl_moved_to_be = True
                                    logger.info("📐 [STRUCTURAL EXIT] %s #%s: %s signal → SL tightened to %.5f (+%.1f pips profit locked, M15 bars=%d)",
                                                self.instrument, self.trade_id, _structural_signal, _new_sl, pnl_pips, _m15_bars_elapsed)
                                    if flight:
                                        flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                                      trade_id=self.trade_id, data={
                                            "action": "structural_exit_tighten",
                                            "signal": _structural_signal,
                                            "new_sl": _new_sl,
                                            "pnl_pips": pnl_pips,
                                            "retrace_state": self._retrace_state,
                                            "m15_bars": _m15_bars_elapsed,
                                        }, note=f"Structural exit ({_structural_signal}) → SL tightened at +{pnl_pips:.1f}p")
                                except Exception as _se:
                                    logger.warning("Structural exit SL tighten failed %s: %s", self.trade_id, _se)

                        elif pnl_pips < 0 and _structural_signal == 'fan_failure' and _m15_bars_elapsed >= _min_m15_hold * 2:
                            # In loss AND fan failed AND we've waited 2× minimum — close, trend is done
                            logger.warning("🔴 [STRUCTURAL EXIT] %s #%s: fan failed, in loss (%.1f pips), %d M15 bars — closing",
                                           self.instrument, self.trade_id, pnl_pips, _m15_bars_elapsed)
                            try:
                                await self._close_with_reason("structural_fan_failure")
                                if flight:
                                    flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                                  trade_id=self.trade_id, data={
                                        "action": "structural_exit_close",
                                        "signal": _structural_signal,
                                        "pnl_pips": pnl_pips,
                                        "retrace_state": self._retrace_state,
                                        "m15_bars": _m15_bars_elapsed,
                                    }, status="warn", note=f"Structural exit ({_structural_signal}) → closed at {pnl_pips:.1f}p")
                            except Exception as _sc:
                                logger.warning("Structural exit close failed %s: %s", self.trade_id, _sc)
                        else:
                            # Signal fired but not yet actionable — log and watch
                            logger.info("📊 [STRUCTURAL WATCH] %s #%s: %s signal (pnl=%.1f pips, retrace=%s, M15 bars=%d) — watching",
                                        self.instrument, self.trade_id, _structural_signal, pnl_pips, self._retrace_state, _m15_bars_elapsed)
                    else:
                        # No structural signal — log retrace state so it's visible in dashboard
                        if _in_retrace:
                            logger.debug("⏳ [RETRACE HOLD] %s #%s: %.1f pips, in retracement — holding (M15 bars=%d)",
                                         self.instrument, self.trade_id, pnl_pips, _m15_bars_elapsed)

            except Exception as _structural_err:
                logger.warning("Structural exit check failed for %s: %s", self.trade_id, _structural_err)

            # Update threat dict with profit protection info
            threat['profit_protection'] = {
                'peak_pnl_pips': round(self._peak_pnl_pips, 1),
                'peak_pl_usd': round(self._peak_unrealized_pl, 2),
                'peak_price': round(self._peak_price, self.display_precision),
                'sl_at_breakeven': self._sl_moved_to_be,
                'trailing_sl': self._last_trail_sl,
                'giveback_pct': round(giveback_pct, 1),
                'auto_profit_enabled': self._auto_profit_enabled,
                'partial_taken': self._partial_taken,
            }
            
        except Exception as e:
            logger.error("Profit protection error for trade %s: %s", self.trade_id, e)

    async def _check_dynamic_exit(
        self,
        pnl_pips: float,
        market: Dict,
        threat: Dict,
    ):
        """Dynamic EMA/BB exit logic with retrace state machine.

        Core insight: BB contraction and EMA contraction happen together — that's a
        normal retrace, NOT an exit signal. The question is what happens AFTER:
        - Bands + EMAs re-expand → trend continues → STAY IN
        - Candles test E100 during contraction → reversal risk → EXIT

        Retrace state machine:
          TRENDING  → both contracting → RETRACING
          RETRACING → both re-expanding → CONTINUING (reset counters, ride the trend)
          RETRACING → candles test E100  → reversal risk → TIGHTEN/CLOSE
          CONTINUING → normal trending again

        Only exit on contraction when candle structure confirms the trend is dying
        (E100 tests, reversal candle patterns, loss of EMA structure).
        """
        try:
            loop = asyncio.get_event_loop()
            is_long = self.direction == 'buy'

            # ── Extract EMA 21/55 separation ──
            ema_state = market.get('ema', {})
            emas = ema_state.get('current_emas', {})
            e21 = emas.get('ema21', 0)
            e55 = emas.get('ema55', 0)
            e100 = emas.get('ema100', 0)
            price = market.get('price', 0) or (self._m1_buffer[-1]['close'] if self._m1_buffer else 0)

            # Compute _new_m15_bar EARLY so we can gate counter updates to M15 boundaries.
            # 2026-04-14 v1: Fix for M1-tick jitter destroying counters. Counters now only
            # advance on new M15 bar boundaries.
            # 2026-04-14 v2: BUGFIX — _m15_buffer is a ROLLING FIXED-SIZE buffer (count=200,
            # replaced on each fetch). len() never changes, so original detector was broken.
            # Use the timestamp of the latest bar instead. Trades 5699/5709 sat in trending
            # for 75+ min post-restart because of this bug.
            _latest_m15_time = ""
            if self._m15_buffer:
                _latest_m15_time = self._m15_buffer[-1].get('time', '') or ""
            _new_m15_bar = bool(_latest_m15_time) and _latest_m15_time != self._retrace_m15_last_bar_time

            ema_contracting = False
            if e21 and e55:
                separation = abs(e21 - e55)

                # Track peak separation for retrace depth measurement (every tick is fine —
                # we want the true intrabar peak)
                if separation > self._peak_ema_sep:
                    self._peak_ema_sep = separation

                # Only append to structural history + update velocity counter on new M15 bars
                if _new_m15_bar:
                    self._ema_sep_history.append(separation)
                    if len(self._ema_sep_history) > 60:
                        self._ema_sep_history = self._ema_sep_history[-60:]

                    # Compute separation velocity (M15-close to M15-close)
                    if len(self._ema_sep_history) >= 2:
                        sep_vel = self._ema_sep_history[-1] - self._ema_sep_history[-2]
                        self._ema_sep_vel_history.append(sep_vel)
                        if len(self._ema_sep_vel_history) > 6:
                            self._ema_sep_vel_history = self._ema_sep_vel_history[-6:]
                        if sep_vel < 0:
                            self._ema_sep_velocity_negative_count += 1
                        else:
                            self._ema_sep_velocity_negative_count = 0

                # ema_contracting for current tick: derived from latest M15-grain velocity
                # (sticky across M1 ticks within same M15 bar, which is the correct behavior)
                if self._ema_sep_vel_history and self._ema_sep_vel_history[-1] < 0:
                    ema_contracting = True

            # ── Extract BB width from M15 (not M1 — too noisy) ──
            bb_upper = 0
            bb_lower = 0
            if self._m15_buffer and len(self._m15_buffer) >= 20:
                try:
                    import pandas as _pd
                    from backtester.indicators import bollinger_bands as _bb_fn
                    _m15_df = _pd.DataFrame(self._m15_buffer)
                    for _col in ('open', 'high', 'low', 'close'):
                        _m15_df[_col] = _m15_df[_col].astype(float)
                    _bb15 = _bb_fn(_m15_df, 20, 2)
                    bb_upper = float(_bb15['bb_upper'].iloc[-1]) if 'bb_upper' in _bb15 else 0
                    bb_lower = float(_bb15['bb_lower'].iloc[-1]) if 'bb_lower' in _bb15 else 0
                except Exception as e:
                    logger.warning("[GUARDIAN] M15 BB calc failed for %s: %s", self.instrument, e)
                    bb = market.get('bollinger', {})
                    bb_upper = bb.get('upper', 0)
                    bb_lower = bb.get('lower', 0)
            else:
                bb = market.get('bollinger', {})
                bb_upper = bb.get('upper', 0)
                bb_lower = bb.get('lower', 0)

            bb_contracting = False
            bb_width = 0
            if bb_upper and bb_lower:
                bb_width = bb_upper - bb_lower

                # Track peak BB width (every tick — capture true intrabar peak)
                if bb_width > self._peak_bb_width:
                    self._peak_bb_width = bb_width

                # Gate structural history + contracting counter to new M15 bars only.
                # 2026-04-14: Previously appended same M15 value every M1 tick, so the
                # "is last < prev?" check was always False and counter never incremented.
                if _new_m15_bar:
                    self._bb_width_history.append(bb_width)
                    if len(self._bb_width_history) > 60:
                        self._bb_width_history = self._bb_width_history[-60:]

                    if len(self._bb_width_history) >= 2:
                        if self._bb_width_history[-1] < self._bb_width_history[-2]:
                            self._bb_contracting_count += 1
                        else:
                            self._bb_contracting_count = 0

                # bb_contracting for current tick: sticky based on latest M15-grain signal
                if self._bb_contracting_count > 0:
                    bb_contracting = True

            # ── E100 proximity — continuous tracking ──
            e100_test = False
            e100_dist_pct = 999
            fan_width_pct = 0
            if e100 > 0 and price > 0:
                e100_dist_pct = abs(price - e100) / e100 * 100
                self._e100_dist_history.append(e100_dist_pct)
                if len(self._e100_dist_history) > 60:
                    self._e100_dist_history = self._e100_dist_history[-60:]

                # "Testing" E100 = price within 0.05% (about 5 pips on EUR_USD)
                if e100_dist_pct < 0.05:
                    e100_test = True

            # Fan width: E21 to E100 spread (parallel with BB width)
            if e21 and e100:
                fan_width_pct = abs(e21 - e100) / e100 * 100
                if fan_width_pct > self._peak_fan_width:
                    self._peak_fan_width = fan_width_pct

            # Are candles APPROACHING E100? (distance shrinking over last 5 ticks)
            e100_approaching = False
            if len(self._e100_dist_history) >= 5:
                recent = self._e100_dist_history[-5:]
                if all(recent[i] >= recent[i+1] for i in range(len(recent)-1)):
                    e100_approaching = True

            # ── Candle pattern at E100 (reversal signals) ──
            e100_reversal_candle = False
            if e100_test and len(self._m1_buffer) >= 3:
                # Check last few candles for reversal patterns near E100
                pat = _detect_pattern(self._m1_buffer[-3:])
                if pat in ('hammer', 'shooting_star', 'engulfing_bearish', 'engulfing_bullish',
                           'evening_star', 'morning_star', 'doji', 'piercing_line', 'dark_cloud'):
                    # Is this pattern AGAINST our trade?
                    bearish_patterns = ('shooting_star', 'engulfing_bearish', 'evening_star', 'dark_cloud')
                    bullish_patterns = ('hammer', 'engulfing_bullish', 'morning_star', 'piercing_line')
                    if (is_long and pat in bearish_patterns) or (not is_long and pat in bullish_patterns):
                        e100_reversal_candle = True

            # ══════════════════════════════════════════════════════════════
            # RETRACE STATE MACHINE
            # BB contraction + EMA contraction = parallel events (retrace)
            # What happens AFTER determines if we exit
            #
            # 2026-04-07: Gate to M15 bars only. Previously ran every M1 tick
            # (~60s), sampling M15 indicators 15x per bar. Tiny floating-point
            # jitter caused ema_contracting/bb_contracting to flip true/false
            # rapidly, oscillating retrace state (trending↔retracing↔continuing)
            # and disabling retrace protection at random moments. Trades #4792
            # and #4796 auto-closed during retrace because of this oscillation.
            # Now: only advance the state machine when a new M15 bar arrives.
            # ══════════════════════════════════════════════════════════════

            both_contracting = bb_contracting and ema_contracting
            both_expanding = (self._bb_contracting_count == 0 and
                             self._ema_sep_velocity_negative_count == 0)

            # _new_m15_bar already computed above (before EMA/BB counter updates).
            # Commit the bar-time advance here so all downstream M15-gated logic
            # (retrace state transitions, E21/E55 cross detection) sees the same flag.
            if _new_m15_bar:
                self._retrace_m15_last_bar_time = _latest_m15_time
                self._retrace_m15_bar_count += 1  # still tracked for legacy log fields

            # ── EMA fan structure check (primary exit signal for retracement entries) ──
            _e21  = ema_state.get('current_emas', {}).get('ema21', 0)
            _e55  = ema_state.get('current_emas', {}).get('ema55', 0)
            _e100 = ema_state.get('current_emas', {}).get('ema100', 0)
            _fan_still_ordered = (
                (is_long  and _e21 > _e55 > 0) or   # bullish ordered: E21 above E55
                (not is_long and _e21 < _e55 and _e55 > 0)  # bearish ordered: E21 below E55
            ) if (_e21 > 0 and _e55 > 0) else True  # default to ordered if EMAs not available

            # Detect E21/E55 cross against trade — the definitive fan failure signal
            # 2026-04-07: Gated to M15 bars. Previously ran every M1 tick — M15 EMAs
            # jitter between ticks causing false crosses. Trade #4856 USD_CAD had
            # "E21 CROSSED BELOW E55 → tightening SL aggressively" every minute for
            # 13 minutes while in retrace, tightening SL until OANDA hit it.
            if _new_m15_bar and not _fan_still_ordered and not self._e21_crossed_e55_against:
                self._e21_crossed_e55_against = True
                logger.warning("Trade %s: ⚠️ E21 CROSSED E55 AGAINST TRADE — fan structure failing "
                               "(E21=%.5f E55=%.5f is_long=%s)",
                               self.trade_id, _e21, _e55, is_long)
            elif _new_m15_bar and _fan_still_ordered and self._e21_crossed_e55_against:
                # Fan recovered — E21 crossed back through E55 in our favor
                self._e21_crossed_e55_against = False
                logger.info("Trade %s: E21/E55 recovered — fan structure restored", self.trade_id)

            # Track E55 holding for retracement entries (price above E55 = retrace still alive)
            if self._is_retracement_entry and _e55 > 0:
                if (is_long and price > _e55) or (not is_long and price < _e55):
                    self._e55_held_count += 1
                else:
                    self._e55_held_count = 0  # reset if price breaks E55

            # ══════════════════════════════════════════════════════════════
            # CANDLE-TO-EMA RETRACE DETECTION (2026-04-17)
            # Replaces BB-width/EMA-separation contraction approach.
            # Retrace = where PRICE is relative to E21/E55/E100.
            # Detects immediately on every tick. No waiting for M15 bars.
            #
            # 203-trade backtest: +1038p total (vs +113p baseline).
            # 98% of trades retrace to at least E21. Retrace is the norm.
            # E21 zone: 68% WR (safe). E55: 51% (watch). E100: 58% (risk).
            #
            # For SELL: trending = below E21, retrace = pulling back above
            # For BUY:  trending = above E21, retrace = pulling back below
            # ══════════════════════════════════════════════════════════════
            _prev_retrace_zone = getattr(self, '_retrace_zone', 'trending')

            if is_long:
                if price > _e21:
                    self._retrace_zone = 'trending'
                elif price > _e55:
                    self._retrace_zone = 'e21_retrace'
                elif price > _e100:
                    self._retrace_zone = 'e55_retrace'
                else:
                    self._retrace_zone = 'e100_broken'
            else:
                if price < _e21:
                    self._retrace_zone = 'trending'
                elif price < _e55:
                    self._retrace_zone = 'e21_retrace'
                elif price < _e100:
                    self._retrace_zone = 'e55_retrace'
                else:
                    self._retrace_zone = 'e100_broken'

            # Map zone to retrace_state for downstream consumers
            if self._retrace_zone == 'trending':
                self._retrace_state = 'trending'
                self._retrace_depth = 0.0
            elif self._retrace_zone == 'e21_retrace':
                self._retrace_state = 'retracing'
                self._retrace_depth = 0.3  # shallow
            elif self._retrace_zone == 'e55_retrace':
                self._retrace_state = 'retracing'
                self._retrace_depth = 0.6  # deep
            elif self._retrace_zone == 'e100_broken':
                self._retrace_state = 'retracing'
                self._retrace_depth = 0.9  # thesis at risk but 58% still win

            # Zone-based retrace threat (feeds into score_threat via trade_info)
            _zone_threats = {'trending': 0, 'e21_retrace': 10, 'e55_retrace': 30, 'e100_broken': 55}
            self._retrace_zone_threat = _zone_threats.get(self._retrace_zone, 0)

            # Log zone transitions
            if self._retrace_zone != _prev_retrace_zone:
                logger.info("Trade %s: retrace zone %s → %s (price=%.5f E21=%.5f E55=%.5f E100=%.5f)",
                            self.trade_id, _prev_retrace_zone, self._retrace_zone,
                            price, _e21, _e55, _e100)

            # Track retrace candle count + E100 tests (still used by downstream rules)
            if self._retrace_state == 'retracing':
                if _new_m15_bar:
                    self._retrace_candle_count += 1
                if e100_test:
                    self._e100_tests_in_retrace += 1
                if both_expanding:
                    self._reexpansion_count += 1
                else:
                    self._reexpansion_count = 0
                # Continuing: re-expansion sustained = trend resuming
                if self._reexpansion_count >= 3:
                    self._retrace_state = 'continuing'
                    logger.info("Trade %s: RETRACE → CONTINUING (re-expanding)", self.trade_id)
            elif self._retrace_state == 'continuing' and self._retrace_zone == 'trending':
                self._retrace_state = 'trending'

            elif _new_m15_bar and self._retrace_state == 'continuing':
                # Back to normal trending or new retrace
                if both_contracting:
                    self._retrace_state = 'retracing'
                    self._retrace_candle_count = 0
                    self._e100_tests_in_retrace = 0
                    self._reexpansion_count = 0
                else:
                    # Update peaks while trending
                    self._retrace_state = 'trending'

            # ══════════════════════════════════════════════════════════════
            # FIVE-PHASE CASCADE TRACKING
            # Logs every state transition to trade_phases table so we can
            # measure: did retraces survive? Did Phase 5 fire at the peak?
            # How many second legs ran? What was pips at each transition?
            # ══════════════════════════════════════════════════════════════
            _prev_retrace_state = getattr(self, '_last_logged_phase', None)
            _curr_phase = self._retrace_state

            # ── Phase 2: PEAK SIGNAL detection ───────────────────────────
            # Fan velocity → 0 or negative while BBs still near peak width.
            # This is 1-2 bars before BOTH CONTRACT fires — the top of the leg.
            # Detect it and log it separately from the retrace phase.
            _peak_signal_fired = False
            if _curr_phase == 'trending' and self._peak_bb_width > 0:
                _bb_near_peak = (bb_width >= self._peak_bb_width * 0.85)  # still ≥85% of peak
                _fan_vel_zero = (self._ema_sep_velocity_negative_count >= 1)  # velocity turned
                if _bb_near_peak and _fan_vel_zero:
                    _peak_signal_fired = True
                    _curr_phase = 'peak'  # ephemeral — transitions to retracing next bar

            # ── Phase 5: EXHAUSTION detection ────────────────────────────
            # Fan velocity negative + RSI extreme + BB at/near session peak.
            # Fire ONCE — take 50% profit and set SL floor at 70% of peak.
            _exhaustion_fired = False
            _exhaustion_ever_fired = getattr(self, '_exhaustion_fired', False)
            if not _exhaustion_ever_fired and pnl_pips >= 5.0:
                _atr_market = market.get('atr', {})
                _atr_ex = float(_atr_market.get('value', _atr_market)) if isinstance(_atr_market, dict) else float(_atr_market or 0)
                _atr_pips_ex = (_atr_ex / self.pip_size) if _atr_ex > 0 else 8.0
                _rsi_market = market.get('rsi', {})
                _rsi_ex = float(_rsi_market.get('value', _rsi_market)) if isinstance(_rsi_market, dict) else float(_rsi_market or 50)
                _rsi_extreme = (is_long and _rsi_ex > 70) or (not is_long and _rsi_ex < 30)
                _fan_neg = self._ema_sep_velocity_negative_count >= 2
                _bb_at_peak = self._peak_bb_width > 0 and bb_width >= self._peak_bb_width * 0.88

                if _rsi_extreme and _fan_neg and _bb_at_peak and pnl_pips >= 8.0:
                    _exhaustion_fired = True
                    self._exhaustion_fired = True
                    _curr_phase = 'exhaustion'
                    logger.info("[GUARDIAN] PHASE 5 EXHAUSTION %s %s: RSI=%.0f fan_neg=%d bb=%.3f/%.3f pnl=%.1fp",
                                self.trade_id, self.instrument, _rsi_ex,
                                self._ema_sep_velocity_negative_count, bb_width, self._peak_bb_width, pnl_pips)
                    # Take 50% of position at exhaustion
                    try:
                        _half_units = int(abs(self.units) / 2)
                        if _half_units >= 1000:
                            _partial_dir = 'buy' if not is_long else 'sell'  # close half = opposite direction
                            from agents.wrappers import place_market_order as _pmo_ex
                            _partial_fill = _pmo_ex(
                                instrument=self.instrument,
                                units=_half_units,
                                direction=_partial_dir,
                                cycle_id=f"exhaustion_partial_{self.trade_id}"
                            )
                            if _partial_fill.get('status') != 'error':
                                logger.info("[GUARDIAN] Phase 5: took 50%% (%d units) at exhaustion | pnl=%.1fp",
                                            _half_units, pnl_pips)
                                # Set SL floor at 70% of peak pips
                                _floor_pips = self._peak_pnl_pips * 0.70
                                _floor_sl = round(
                                    self.entry_price + _floor_pips * self.pip_size if is_long
                                    else self.entry_price - _floor_pips * self.pip_size,
                                    self.display_precision
                                )
                                _floor_valid = (is_long and _floor_sl > self.stop_loss) or \
                                               (not is_long and _floor_sl < self.stop_loss)
                                if _floor_valid:
                                    await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                                        self.trade_id,
                                        stop_loss={"price": str(_floor_sl), "timeInForce": "GTC"}
                                    ))
                                    _old_sl_p5 = self.stop_loss
                                    self.stop_loss = _floor_sl
                                    logger.info("[GUARDIAN] Phase 5: SL floor set to %.5f (70%% of %.1fp peak)",
                                                _floor_sl, self._peak_pnl_pips)
                                    if flight:
                                        flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                                      trade_id=self.trade_id, data={
                                            "action": "exhaustion_partial_floor",
                                            "partial_units": _half_units,
                                            "old_sl": _old_sl_p5,
                                            "new_sl": _floor_sl,
                                            "peak_pnl_pips": round(self._peak_pnl_pips, 1),
                                            "pnl_pips": round(pnl_pips, 1),
                                        }, note=f"Phase 5 exhaustion: 50% closed + SL floor {_floor_sl:.5f}")
                    except Exception as _ex5_err:
                        logger.warning("[GUARDIAN] Phase 5 partial exit failed %s: %s", self.trade_id, _ex5_err)

            # ── Phase 3: Gradual SL trail toward E100 during retrace ─────
            # When state = retracing, move SL 30% of remaining distance to E100 each tick.
            # This is gentler than jumping to E100+buffer immediately.
            # GUARD: Only trail once the trade has seen positive P&L (peak >= 3p).
            # E100 retest entries that never moved positive get clipped by this trail
            # during the normal initial oscillation at the entry zone.
            # KILL SWITCH (2026-04-20): retrace SL trail disabled. Same root cause
            # as the auto_close_threat90 false positives — tightening SL toward
            # E100/E55 during EMA compression walks SL into price noise during
            # normal retrace oscillation. Planned SL + emergency margin still protect.
            try:
                _retrace_trail_enabled = bool(tc_get("guardian.retrace_sl_trail_enabled", False))
            except Exception:
                _retrace_trail_enabled = False
            _p3_peak_ok = self._peak_pnl_pips >= 3.0
            if (_retrace_trail_enabled and self._retrace_state == 'retracing'
                    and _e55 > 0 and _e100 > 0 and self.stop_loss and _p3_peak_ok):
                try:
                    _atr_p3 = market.get('atr', {})
                    _atr_p3v = float(_atr_p3.get('value', _atr_p3)) if isinstance(_atr_p3, dict) else float(_atr_p3 or 0)
                    _atr_pips_p3 = (_atr_p3v / self.pip_size) if _atr_p3v > 0 else 8.0

                    # 2026-04-02: When EMAs converged during retrace, E100 is not a
                    # meaningful separate level. Trail toward E55 with bigger buffer instead.
                    _ema_gap_pct_p3 = abs(_e55 - _e100) / _e100 * 100 if _e100 > 0 else 999
                    if _ema_gap_pct_p3 < 0.15:
                        _p3_anchor = _e55
                        _e100_buf = max(8.0, min(12.0, _atr_pips_p3 * 0.7)) * self.pip_size
                    else:
                        _p3_anchor = _e100
                        _e100_buf = max(3.0, min(8.0, _atr_pips_p3 * 0.5)) * self.pip_size

                    # Target: anchor + buffer (the final danger zone anchor)
                    _p3_target = round(
                        _p3_anchor + _e100_buf if not is_long else _p3_anchor - _e100_buf,
                        self.display_precision
                    )
                    # Current SL — move toward target each tick.
                    # 2026-04-01: Manual trades get 15% per tick (was 30% for all).
                    # Trade #3689 USD_CHF manual SELL had SL ratcheted from 15.3p to 5.1p
                    # in 7 minutes at 30%, then price retraced into the tightened SL.
                    # Manual trades need more room — user picked the entry deliberately.
                    _is_manual_p3 = (self.source in ('manual', 'scout') or
                                     (self.trade_thesis or {}).get('is_manual', False))
                    _p3_rate = 0.15 if _is_manual_p3 else 0.30
                    _p3_gap = _p3_target - self.stop_loss
                    _p3_move = round(_p3_gap * _p3_rate, self.display_precision)
                    _p3_new_sl = round(self.stop_loss + _p3_move, self.display_precision)

                    # Only tighten, never widen
                    _p3_tightens = (is_long and _p3_new_sl > self.stop_loss) or \
                                   (not is_long and _p3_new_sl < self.stop_loss)
                    _p3_valid = (is_long and _p3_new_sl < price) or \
                                (not is_long and _p3_new_sl > price)

                    if _p3_tightens and _p3_valid and abs(_p3_move) > self.pip_size * 0.5:
                        await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                            self.trade_id,
                            stop_loss={"price": str(_p3_new_sl), "timeInForce": "GTC"}
                        ))
                        _old_sl_p3 = self.stop_loss
                        self.stop_loss = _p3_new_sl
                        logger.debug("[GUARDIAN] Phase 3 SL trail %s: %.5f → %.5f (%.0f%% toward E100 target %.5f)%s",
                                     self.trade_id, _old_sl_p3, _p3_new_sl, _p3_rate*100, _p3_target,
                                     " [manual=slow]" if _is_manual_p3 else "")
                        if flight:
                            flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                          trade_id=self.trade_id, data={
                                "action": "retrace_trail_e100",
                                "old_sl": _old_sl_p3,
                                "new_sl": _p3_new_sl,
                                "e100_target": _p3_target,
                                "pnl_pips": round(pnl_pips, 1),
                                "retrace_depth": round(self._retrace_depth, 3),
                            }, note=f"Phase 3 retrace trail: SL {_old_sl_p3:.5f}→{_p3_new_sl:.5f}")
                except Exception as _p3_err:
                    # 2026-04-24: upgraded — Phase 3 retrace trail moves SL during retrace.
                    # Silent failure = SL not trailed, giving back retrace profit.
                    logger.warning("[GUARDIAN] Phase 3 trail FAILED %s: %s: %s (SL not trailed)",
                                   self.trade_id, type(_p3_err).__name__, _p3_err)

            # ── Log phase transition to trade_phases table ────────────────
            if _curr_phase != _prev_retrace_state:
                try:
                    import sqlite3 as _p_sq
                    from flight_recorder import DB_PATH as _FLIGHT_DB_PATH_
                    _p_db = str(_FLIGHT_DB_PATH_)
                    _action = ""
                    if _curr_phase == 'exhaustion':    _action = "take_50pct_set_floor_70pct"
                    elif _curr_phase == 'peak':        _action = "lock_profit_70pct"
                    elif _curr_phase == 'retracing':   _action = "trail_sl_toward_e100"
                    elif _curr_phase == 'continuing':  _action = "resume_e55_anchor"

                    _fan_sep_pips = abs(_e21 - _e100) * (1/self.pip_size) if _e21 and _e100 else 0

                    with _p_sq.connect(_p_db, timeout=3) as _pc:
                        _pc.execute("""
                            INSERT OR IGNORE INTO trade_phases
                            (timestamp, trade_id, pair, direction, phase, from_phase,
                             pnl_pips, bb_width, fan_sep_pips, retrace_depth,
                             e100_tests, reexpansion_count, action_taken, note)
                            VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?,?)
                        """, (
                            datetime.now(timezone.utc).isoformat(),
                            str(self.trade_id), self.instrument,
                            'buy' if is_long else 'sell',
                            _curr_phase, _prev_retrace_state,
                            round(pnl_pips, 2), round(bb_width, 4),
                            round(_fan_sep_pips, 1), round(self._retrace_depth, 3),
                            self._e100_tests_in_retrace, self._reexpansion_count,
                            _action,
                            f"bb={bb_width:.3f} peak={self._peak_bb_width:.3f} retrace={self._retrace_depth:.1%}"
                        ))
                        _pc.commit()
                    self._last_logged_phase = _curr_phase
                    logger.info("[GUARDIAN] PHASE %s→%s %s %s | pnl=%.1fp bb=%.3f fan=%.1fp retrace=%.0f%%",
                                _prev_retrace_state, _curr_phase,
                                self.trade_id, self.instrument,
                                pnl_pips, bb_width, _fan_sep_pips,
                                self._retrace_depth * 100)
                    # Also record in flight_log for dashboard visibility
                    if flight:
                        flight.record(FlightStage.TRADE_PHASE, pair=self.instrument,
                                      trade_id=self.trade_id, data={
                            "phase": _curr_phase, "from_phase": _prev_retrace_state,
                            "pnl_pips": round(pnl_pips, 2),
                            "bb_width": round(bb_width, 4),
                            "fan_sep_pips": round(_fan_sep_pips, 1),
                            "retrace_depth": round(self._retrace_depth, 3),
                            "e100_tests": self._e100_tests_in_retrace,
                            "reexpansion_count": self._reexpansion_count,
                            "action": _action,
                        }, note=f"Phase {_prev_retrace_state}→{_curr_phase} | pnl={pnl_pips:+.1f}p")
                except Exception as _log_err:
                    # 2026-04-24: upgraded — trade_phases table tracks cascade state.
                    # Silent failure = phase history gaps → retro analysis broken.
                    logger.warning("[GUARDIAN] Phase log FAILED %s: %s: %s (trade_phases table gap)",
                                   self.trade_id, type(_log_err).__name__, _log_err)

            # ══════════════════════════════════════════════════════════════
            # EXIT RULES — only act on structural confirmation, not just contraction
            # ══════════════════════════════════════════════════════════════

            # ── DYNAMIC E55-ANCHORED SL ──────────────────────────────────────────
            # Core insight from trade analysis (2026-03-18):
            #   11 of 19 losses were SL-too-tight — static ATR stop killed trades
            #   during normal retracements that tested E55 but never E100.
            #   The composite picture (EMA position + BB state + RSI) tells us
            #   whether a retrace is healthy or structural.
            #
            # Rule: SL trails E55 with a buffer.
            #   TRENDING state  → SL = E55 ± (1×ATR buffer, min 5p)
            #                     Gives the trade full room to retrace to E55
            #   RETRACING state → SL = E100 ± (0.5×ATR buffer, min 3p)
            #                     Once retracing, real danger is E100 test
            #   E21/E55 crossed → SL = entry ± buffer (existing Rule 0 handles this)
            #
            # Only moves SL TIGHTER than current position — never widens it.
            # Only activates after trade has breathed (≥5 M1 candles).
            # GUARD: Do not tighten SL while trade P&L is negative (peak < 3p).
            # E100 retest entries have normal initial oscillation below/at entry —
            # tightening SL during that phase clips valid trades before they breathe.
            # ────────────────────────────────────────────────────────────────────
            # 2026-04-02: Added pnl_pips > 0 guard. EUR_AUD #4305 had SL tightened at
            # pnl=-6.4p because peak was 5.4p (>3.0). Dynamic SL should ONLY tighten
            # when the trade is currently profitable — never while losing money.
            if _e55 > 0 and _e100 > 0 and self._candles_in_trade >= 5 and self._peak_pnl_pips >= 3.0 and pnl_pips > 0:
                try:
                    _atr_val_dsl = market.get('atr', {})
                    _atr_dsl = float(_atr_val_dsl.get('value', _atr_val_dsl)) if isinstance(_atr_val_dsl, dict) else float(_atr_val_dsl or 0)
                    _atr_pips_dsl = (_atr_dsl / self.pip_size) if _atr_dsl > 0 else 8.0

                    if self._retrace_state in ('trending', 'continuing'):
                        # ── Profit-tiered anchor: ≥8p profit → switch to E21 (tighter) ──
                        # Normal: SL = E55 ± up to 12p — lets trade breathe through retracements
                        # ≥8 pips in profit (~0.5R): switch to E21 ± 3p — lock in gained profit
                        # Prevents giving back large profits (e.g. trade #1930: peaked +13.8p, closed -0.6p)
                        if pnl_pips >= 8.0 and _e21 > 0:
                            # 2026-03-31: Raised from 3p to 5p. Profit lock still
                            # tighter than E55 anchor but gives breathing room.
                            _buffer_dsl = 5.0  # profit-lock anchor to E21
                            if is_long:
                                _dsl_price = round(_e21 - (_buffer_dsl * self.pip_size), self.display_precision)
                                _sl_valid = _dsl_price < price
                            else:
                                _dsl_price = round(_e21 + (_buffer_dsl * self.pip_size), self.display_precision)
                                _sl_valid = _dsl_price > price
                        else:
                            # SL anchored to E55 — gives trade room to retrace naturally
                            # 2026-03-31: Raised floor from 5p to 8p, ceiling 12→15p.
                            _buffer_dsl = max(8.0, min(15.0, _atr_pips_dsl * 0.8))
                            if is_long:
                                _dsl_price = round(_e55 - (_buffer_dsl * self.pip_size), self.display_precision)
                                _sl_valid = _dsl_price < price  # SL must be below current price
                            else:
                                _dsl_price = round(_e55 + (_buffer_dsl * self.pip_size), self.display_precision)
                                _sl_valid = _dsl_price > price  # SL must be above current price

                    elif self._retrace_state == 'retracing':
                        # In retrace — anchor SL to E100 + buffer (real danger zone)
                        # 2026-03-31: Raised floor from 3p to 8p. Old 3p floor was
                        # clipping trades on normal noise (trade #3165 GBP_JPY stopped
                        # out at -2.9p, then system opened same direction and profited).
                        # 2026-04-02: EUR_AUD #4293 killed at +2.7p because E100 anchor
                        # tightened SL from 39p to 1.7p from entry in ONE move when EMAs
                        # compressed during retrace. Two safeguards added:
                        #   1. Minimum distance from entry: SL can't go closer than 50% of
                        #      original SL distance (preserve half the breathing room)
                        #   2. When EMAs converged (E55-E100 gap < 0.15%), use E55 anchor
                        #      instead of E100 — E100 is not a meaningful level when it's
                        #      sitting right on top of E55
                        _ema_gap_pct_dsl = abs(_e55 - _e100) / _e100 * 100 if _e100 > 0 and _e55 > 0 else 999
                        if _ema_gap_pct_dsl < 0.15:
                            # EMAs converged — E100 is NOT a distinct level, use E55 instead
                            _anchor_ema = _e55
                            _buffer_dsl = max(10.0, min(15.0, _atr_pips_dsl * 0.8))
                        else:
                            _anchor_ema = _e100
                            _buffer_dsl = max(8.0, min(15.0, _atr_pips_dsl * 0.7))
                        if is_long:
                            _dsl_price = round(_anchor_ema - (_buffer_dsl * self.pip_size), self.display_precision)
                            _sl_valid = _dsl_price < price
                        else:
                            _dsl_price = round(_anchor_ema + (_buffer_dsl * self.pip_size), self.display_precision)
                            _sl_valid = _dsl_price > price
                        # (50% original SL safeguard now applied universally below)
                    else:
                        _dsl_price = None
                        _sl_valid  = False

                    # ── Safeguard: never tighten past 50% of original SL distance ──
                    # 2026-04-03: GBP_JPY #4507 — Dynamic SL in trending/continuing
                    # jumped from 211.097→210.774 (32.3p tighter) in ONE tick because
                    # this safeguard only existed on the retracing branch. The trade
                    # had 42p of room, Dynamic SL crushed it to 9.5p, retrace killed it.
                    # Now applies to ALL branches — trending, continuing, AND retracing.
                    if _dsl_price and _sl_valid and self._original_sl:
                        _orig_dist_dsl = abs(self._original_sl - self.entry_price)
                        _min_dist_dsl = _orig_dist_dsl * 0.50
                        _new_dist_dsl = abs(_dsl_price - self.entry_price)
                        if _new_dist_dsl < _min_dist_dsl:
                            if is_long:
                                _dsl_price = round(self.entry_price - _min_dist_dsl, self.display_precision)
                            else:
                                _dsl_price = round(self.entry_price + _min_dist_dsl, self.display_precision)
                            logger.info("[GUARDIAN] Dynamic SL %s: anchor would tighten to %.1fp from entry "
                                        "(min=%.1fp) — clamped to 50%% of original SL distance (state=%s)",
                                        self.trade_id, _new_dist_dsl / self.pip_size,
                                        _min_dist_dsl / self.pip_size, self._retrace_state)

                    # ── Max-tighten-per-tick: cap how much SL can move in one cycle ──
                    # 2026-04-03: GBP_JPY #4507 — SL jumped 32.3p in one 15s cycle.
                    # Phase 3 retrace trail already has a per-tick rate (15-30%), but
                    # the main Dynamic SL had no limit. Cap at 30% of remaining gap
                    # per tick so the SL converges gradually, not in a single leap.
                    if _dsl_price and _sl_valid and self.stop_loss:
                        _dsl_gap = abs(_dsl_price - self.stop_loss)
                        _max_move_per_tick = _dsl_gap * 0.30
                        # Only apply rate limit if the move is large (>5p) — small
                        # tightening moves (normal E55 drift) should go through immediately
                        if _dsl_gap / self.pip_size > 5.0:
                            if is_long:
                                _dsl_price = round(self.stop_loss + _max_move_per_tick, self.display_precision)
                            else:
                                _dsl_price = round(self.stop_loss - _max_move_per_tick, self.display_precision)
                            logger.info("[GUARDIAN] Dynamic SL %s: rate-limited move to 30%% of gap "
                                        "(gap=%.1fp, moving %.1fp this tick)",
                                        self.trade_id, _dsl_gap / self.pip_size,
                                        _max_move_per_tick / self.pip_size)

                    if _dsl_price and _sl_valid:
                        # Only move SL if it TIGHTENS (never widen)
                        _current_sl = self.stop_loss or 0
                        _should_move = (
                            (is_long  and _dsl_price > _current_sl) or
                            (not is_long and _dsl_price < _current_sl)
                        ) if _current_sl else False

                        if _should_move:
                            try:
                                await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                                    self.trade_id,
                                    stop_loss={"price": str(_dsl_price), "timeInForce": "GTC"}
                                ))
                                _old_sl = self.stop_loss
                                self.stop_loss = _dsl_price
                                _anchor = ("E21" if (self._retrace_state in ('trending', 'continuing') and pnl_pips >= 8.0 and _e21 > 0)
                                           else "E55" if self._retrace_state in ('trending', 'continuing')
                                           else "E100")
                                logger.info("[GUARDIAN] DYNAMIC SL %s %s: %s anchor | state=%s | "
                                            "SL %.5f → %.5f (buffer=%.1fp) | pnl=%.1fp",
                                            self.trade_id, self.instrument,
                                            _anchor, self._retrace_state,
                                            _old_sl, _dsl_price, _buffer_dsl, pnl_pips)
                                if flight:
                                    flight.record(FlightStage.GUARDIAN_ACTION,
                                                  pair=self.instrument, trade_id=self.trade_id, data={
                                        "action": "dynamic_sl_trail",
                                        "anchor": _anchor,
                                        "retrace_state": self._retrace_state,
                                        "old_sl": _old_sl,
                                        "new_sl": _dsl_price,
                                        "buffer_pips": _buffer_dsl,
                                        "e55": _e55,
                                        "e100": _e100,
                                        "pnl_pips": pnl_pips,
                                    }, note=f"Dynamic SL: {_anchor}±{_buffer_dsl:.1f}p → {_dsl_price}")
                            except Exception as _dsl_err:
                                # 2026-04-24: upgraded — dynamic SL move operation failed.
                                # Silent = SL stays at stale level, no protection update.
                                logger.warning("[GUARDIAN] Dynamic SL move FAILED %s: %s: %s (SL not updated)",
                                               self.trade_id, type(_dsl_err).__name__, _dsl_err)
                except Exception as _dsl_outer:
                    # 2026-04-24: upgraded — dynamic SL calc threw. Silent =
                    # dynamic SL disabled for this trade's remaining life.
                    logger.warning("[GUARDIAN] Dynamic SL calc FAILED %s: %s: %s (dynamic SL disabled rest of trade)",
                                   self.trade_id, type(_dsl_outer).__name__, _dsl_outer)
            # ────────────────────────────────────────────────────────────────────

            # RULE 0: E21/E55 CROSS AGAINST TRADE — definitive fan structure failure
            # The fan has failed when E21 crosses below E55 (bull) / above E55 (bear).
            #
            # CRITICAL GUARD: Do NOT fire while in RETRACING state.
            # An E21/E55 cross during a known retracement is EXPECTED pullback behavior —
            # it is NOT structural failure. The market was retracing and the next candle
            # often resumes in the trade direction. Firing here clips profitable trades
            # on normal retracements (exactly what happened on USD_JPY 2026-03-16).
            #
            # Only fire when: (a) cross confirmed AND
            #                 (b) NOT currently in a retracement (retracing state)
            #                 (c) trade has had time to breathe (≥15 M1 candles = ~1 M15 bar)
            #                 (d) not already deep in loss
            # Applies to ALL trade types (manual, snipe, auto) — retracement is
            # a market condition, not an entry-type label.
            _rule0_retrace_suppressed = (self._retrace_state == 'retracing')
            if (self._e21_crossed_e55_against and
                    not _rule0_retrace_suppressed and    # NOT during a recognized retracement
                    self._candles_in_trade >= 15 and     # at least ~1 M15 bar of breathing room
                    pnl_pips > -20.0):                   # not already stopped out further
                risk_pips = abs(self.entry_price - self.stop_loss) / self.pip_size if self.stop_loss else 0
                logger.warning("Trade %s: E21 CROSSED BELOW E55 (fan structure failed) → "
                               "tightening SL aggressively (pnl=%.1f pips, retrace_state=%s)",
                               self.trade_id, pnl_pips, self._retrace_state)
                # Move SL to near-breakeven — use ATR-based buffer (not fixed 1 pip)
                # Fixed 1-pip buffer gets clipped by spread on JPY/CHF pairs
                if pnl_pips >= 3.0 and not self._sl_moved_to_be:
                    _atr_val = market.get('atr', {})
                    _atr = float(_atr_val.get('value', _atr_val)) if isinstance(_atr_val, dict) else float(_atr_val or 0)
                    # Buffer = 0.5×ATR, min 3 pips, max 8 pips — survives spread + 1 candle noise
                    _atr_pips = (_atr / self.pip_size) if _atr > 0 else 5.0
                    buffer_pips = max(3.0, min(8.0, round(_atr_pips * 0.5, 1)))
                    if is_long:
                        new_sl = self.entry_price - (buffer_pips * self.pip_size)
                    else:
                        new_sl = self.entry_price + (buffer_pips * self.pip_size)
                    new_sl = round(new_sl, self.display_precision)
                    try:
                        await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                            self.trade_id,
                            stop_loss={"price": str(new_sl), "timeInForce": "GTC"}
                        ))
                        _old_sl_fan = self.stop_loss
                        self.stop_loss = new_sl
                        self._sl_moved_to_be = True
                        logger.info("Trade %s: Fan failed → SL to near-entry %.5f (buffer=%.1f pips, ATR=%.1f pips)",
                                    self.trade_id, new_sl, buffer_pips, _atr_pips)
                        if flight:
                            flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                          trade_id=self.trade_id, data={
                                "action": "fan_failure_sl_tighten",
                                "old_sl": _old_sl_fan,
                                "new_sl": new_sl,
                                "buffer_pips": buffer_pips,
                                "atr_pips": round(_atr_pips, 1),
                                "pnl_pips": round(pnl_pips, 1),
                            }, note=f"Fan failed: SL {_old_sl_fan:.5f}→{new_sl:.5f}")
                    except Exception as _fe:
                        logger.warning("Trade %s: Failed to tighten SL on fan failure: %s", self.trade_id, _fe)
            elif self._e21_crossed_e55_against and _rule0_retrace_suppressed:
                logger.info("Trade %s: E21/E55 cross SUPPRESSED — in retracement state (pnl=%.1f pips). "
                            "Watching for continuation.", self.trade_id, pnl_pips)

            # RULE 1: E100 TEST DURING RETRACE — real reversal danger
            # For retracement entries: E100 at entry is EXPECTED — suppress until we have
            # profit and E100 is tested AGAIN (after price moved away from entry first).
            # For expansion entries: any 2 E100 tests in retrace = danger, tighten.
            _e100_danger = (self._e100_tests_in_retrace >= 2 or
                           (e100_approaching and e100_dist_pct < 0.08))
            # Retracement entry: don't fire on first E100 test (it was the entry signal).
            # Only fire if price has already moved away (e55_held > 3 candles) then comes back.
            _e100_rule1_armed = (
                not self._is_retracement_entry or
                self._e55_held_count >= 3  # price held above E55 before coming back to E100
            )
            if (self._retrace_state == 'retracing' and
                    _e100_danger and
                    _e100_rule1_armed and
                    pnl_pips >= 5.0 and
                    not self._sl_moved_to_be):

                _atr_val_r1 = market.get('atr', {})
                _atr_r1 = float(_atr_val_r1.get('value', _atr_val_r1)) if isinstance(_atr_val_r1, dict) else float(_atr_val_r1 or 0)
                _atr_pips_r1 = (_atr_r1 / self.pip_size) if _atr_r1 > 0 else 5.0
                buffer_pips = max(3.0, min(8.0, round(_atr_pips_r1 * 0.5, 1)))
                if is_long:
                    new_sl = self.entry_price + (buffer_pips * self.pip_size)
                else:
                    new_sl = self.entry_price - (buffer_pips * self.pip_size)

                # ── MFE minimum floor ────────────────────────────────────────────
                # If MFE has already exceeded 60% of TP, don't set SL below MFE-2p.
                # Prevents tightening SL so aggressively that profit is given back
                # on a normal retracement that would have continued to TP.
                # Example: #1461 EUR_AUD — MFE 7.3p on 11p TP → floor = 5.3p, 
                # but guardian set SL at entry+3p, trade closed at 2.7p.
                _risk_pips_r1 = abs(self.entry_price - self.stop_loss) / self.pip_size
                _tp_pips_r1 = abs(self.take_profit - self.entry_price) / self.pip_size if self.take_profit else _risk_pips_r1 * 3
                if self._peak_pnl_pips > _tp_pips_r1 * 0.6:
                    _mfe_floor_pips = max(buffer_pips, self._peak_pnl_pips - 2.0)
                    if is_long:
                        _mfe_floor_sl = self.entry_price + (_mfe_floor_pips * self.pip_size)
                        if _mfe_floor_sl > new_sl:
                            logger.info("[GUARDIAN] %s: MFE floor raised SL from +%.1fp to +%.1fp (MFE=%.1fp, TP=%.1fp)",
                                        self.instrument, buffer_pips, _mfe_floor_pips, self._peak_pnl_pips, _tp_pips_r1)
                            new_sl = _mfe_floor_sl
                    else:
                        _mfe_floor_sl = self.entry_price - (_mfe_floor_pips * self.pip_size)
                        if _mfe_floor_sl < new_sl:
                            logger.info("[GUARDIAN] %s: MFE floor raised SL from +%.1fp to +%.1fp (MFE=%.1fp, TP=%.1fp)",
                                        self.instrument, buffer_pips, _mfe_floor_pips, self._peak_pnl_pips, _tp_pips_r1)
                            new_sl = _mfe_floor_sl

                new_sl = round(new_sl, self.display_precision)

                await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                    self.trade_id,
                    stop_loss={"price": str(new_sl), "timeInForce": "GTC"}
                ))
                self.stop_loss = new_sl
                self._sl_moved_to_be = True

                logger.info("Trade %s: E100 tested %dx during retrace → SL to breakeven %.5f "
                           "(retrace depth %.1f%%, %d candles in retrace)",
                           self.trade_id, self._e100_tests_in_retrace, new_sl,
                           self._retrace_depth * 100, self._retrace_candle_count)

                if flight:
                    flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                  trade_id=self.trade_id, data={
                        "action": "dynamic_exit_tighten",
                        "reason": "e100_test_during_retrace",
                        "new_sl": new_sl,
                        "e100_tests": self._e100_tests_in_retrace,
                        "retrace_depth": self._retrace_depth,
                        "retrace_candles": self._retrace_candle_count,
                    }, note=f"E100 tested {self._e100_tests_in_retrace}x in retrace → tighten")

            # RULE 2: E100 REVERSAL CANDLE — strongest exit signal
            # Price at E100 + reversal candle pattern against trade = close
            if (e100_reversal_candle and
                    pnl_pips >= 3.0):

                risk_pips = abs(self.entry_price - self.stop_loss) / self.pip_size
                min_profit = max(5.0, risk_pips * 0.5)  # At least 5 pips or 0.5R

                if pnl_pips >= min_profit:
                    logger.warning("Trade %s: REVERSAL CANDLE at E100 (dist %.3f%%) → CLOSING at %.1f pips profit",
                                  self.trade_id, e100_dist_pct, pnl_pips)

                    await self._close_with_reason("reversal_candle_e100")

                    if flight:
                        flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                      trade_id=self.trade_id, data={
                            "action": "dynamic_exit_close",
                            "reason": "e100_reversal_candle",
                            "e100_dist_pct": e100_dist_pct,
                            "pnl_pips": pnl_pips,
                            "retrace_state": self._retrace_state,
                        }, status="warn", note=f"Reversal candle at E100 → close at {pnl_pips:.1f} pips")

            # RULE 3: DEEP RETRACE with no recovery — contraction persists, no re-expansion
            # Only fires when: deep contraction (>40% from peak) + long time (20+ candles) +
            # NO re-expansion signs + in profit.
            #
            # RETRACEMENT ENTRIES: trade was entered DURING the contraction.
            # The retrace_depth is measured from the BB peak BEFORE entry, so this position
            # was always 40%+ contracted from entry — we need an adjusted rule:
            # For retracement entries, only fire if ALSO:
            #   (a) E21 has crossed below E55 (fan structure failed), OR
            #   (b) contraction has deepened further since entry (not recovering), AND
            #       fan re-expansion never started after initial E55/E100 bounce
            _deep_retrace_ok_to_fire = (
                not self._is_retracement_entry or
                self._e21_crossed_e55_against or                   # fan structure failed
                (self._e55_held_count == 0 and                     # never held above E55
                 self._retrace_candle_count >= 15)                  # been stuck here 15+ candles
            )
            if (self._retrace_state == 'retracing' and
                    self._retrace_depth > 0.40 and
                    self._retrace_candle_count >= tc_get("guardian.retrace_candle_count", 10) and  # V4: was 20, faster retrace detection
                    self._reexpansion_count == 0 and
                    self._candles_in_trade >= tc_get("guardian.min_candles_retrace_exit", 15) and  # V4: was 30, reduced to match V3 optimizer max_hold=15
                    _deep_retrace_ok_to_fire and
                    pnl_pips >= 8.0):

                risk_pips = abs(self.entry_price - self.stop_loss) / self.pip_size
                min_profit_for_close = max(10.0, risk_pips * 1.0)

                if pnl_pips >= min_profit_for_close:
                    logger.warning("Trade %s: Deep retrace (%.0f%% contraction, %d candles, no recovery) → CLOSING at %.1f pips",
                                  self.trade_id, self._retrace_depth * 100,
                                  self._retrace_candle_count, pnl_pips)

                    await self._close_with_reason("deep_retrace")

                    if flight:
                        flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                      trade_id=self.trade_id, data={
                            "action": "dynamic_exit_close",
                            "reason": "deep_retrace_no_recovery",
                            "retrace_depth": self._retrace_depth,
                            "retrace_candles": self._retrace_candle_count,
                            "e100_tests": self._e100_tests_in_retrace,
                            "pnl_pips": pnl_pips,
                        }, status="warn", note=f"Deep retrace {self._retrace_depth:.0%} / {self._retrace_candle_count} candles → close")

            # RULE 4: EMA contraction WITHOUT BB contraction = just EMA noise, tighten only
            # (EMA sep negative 10+ AND not in retrace state AND profit)
            if (self._ema_sep_velocity_negative_count >= 4 and  # V4: was 10, tighten faster per V3 optimizer
                    not both_contracting and
                    self._retrace_state == 'trending' and
                    pnl_pips >= 8.0 and
                    not self._sl_moved_to_be):

                _atr_val_r4 = market.get('atr', {})
                _atr_r4 = float(_atr_val_r4.get('value', _atr_val_r4)) if isinstance(_atr_val_r4, dict) else float(_atr_val_r4 or 0)
                _atr_pips_r4 = (_atr_r4 / self.pip_size) if _atr_r4 > 0 else 5.0
                buffer_pips = max(3.0, min(8.0, round(_atr_pips_r4 * 0.5, 1)))
                if is_long:
                    new_sl = self.entry_price + (buffer_pips * self.pip_size)
                else:
                    new_sl = self.entry_price - (buffer_pips * self.pip_size)
                new_sl = round(new_sl, self.display_precision)

                await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                    self.trade_id,
                    stop_loss={"price": str(new_sl), "timeInForce": "GTC"}
                ))
                self.stop_loss = new_sl
                self._sl_moved_to_be = True

                logger.info("Trade %s: EMA sep negative %dx (BB not contracting) → SL to breakeven %.5f",
                           self.trade_id, self._ema_sep_velocity_negative_count, new_sl)

                if flight:
                    flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                  trade_id=self.trade_id, data={
                        "action": "dynamic_exit_tighten",
                        "reason": "ema_only_deceleration",
                        "new_sl": new_sl,
                        "ema_sep_neg_count": self._ema_sep_velocity_negative_count,
                    }, note=f"EMA-only deceleration {self._ema_sep_velocity_negative_count}x → tighten")

            # ══════════════════════════════════════════════════════════════
            # RULE 5: PEAK SEPARATION APPROACH — close 1-2 bars before the peak
            #
            # Mirror of format_chart_signals() detect_deceleration():
            # fires when EMA separation GROWTH is slowing (d1_2 > d1_1 > d1_0, still >= 0)
            # while above the significance threshold (top 30% of range).
            # This is 1-2 bars BEFORE the peak — the guardian's existing rules only fire
            # AFTER velocity goes negative (after the peak has already passed).
            #
            # Only closes if: significant separation + decel pattern + profit + not already fired.
            # ══════════════════════════════════════════════════════════════
            if (not self._peak_decel_close_fired
                    and len(self._ema_sep_vel_history) >= 3
                    and len(self._ema_sep_history) >= 10
                    and self._candles_in_trade >= 5
                    and pnl_pips >= 5.0):

                _d1_0 = self._ema_sep_vel_history[-1]
                _d1_1 = self._ema_sep_vel_history[-2]
                _d1_2 = self._ema_sep_vel_history[-3]

                # Pattern: three consecutive decelerations, growth still positive (not yet negative)
                _decel_pattern = (_d1_2 > _d1_1 > _d1_0 >= 0)

                # Significance: current separation is in top 30% of what we've seen
                _valid_seps = sorted([s for s in self._ema_sep_history if s > 0])
                _sep_threshold = _valid_seps[int(len(_valid_seps) * 0.70)] if len(_valid_seps) >= 5 else 0
                _above_threshold = (_sep_threshold > 0 and
                                    (self._ema_sep_history[-1] if self._ema_sep_history else 0) > _sep_threshold)

                # Also confirm BBs are still wide (not already contracting — that's Rule 3's job)
                _bb_still_wide = self._bb_contracting_count <= 1

                if _decel_pattern and _above_threshold and _bb_still_wide and pnl_pips >= 8.0:
                    logger.warning(
                        "🎯 [PEAK-DECEL] %s #%s: separation decelerating (%.5f→%.5f→%.5f) "
                        "above threshold %.4f — closing at %.1f pips (1-2 bars before peak)",
                        self.instrument, self.trade_id, _d1_2, _d1_1, _d1_0,
                        _sep_threshold, pnl_pips
                    )
                    try:
                        await self._close_with_reason("fan_separation_lost")
                        self._peak_decel_close_fired = True
                        if flight:
                            flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                          trade_id=self.trade_id, data={
                                "action": "peak_decel_close",
                                "d1_2": round(_d1_2, 6),
                                "d1_1": round(_d1_1, 6),
                                "d1_0": round(_d1_0, 6),
                                "sep_threshold": round(_sep_threshold, 6),
                                "current_sep": round(self._ema_sep_history[-1], 6),
                                "pnl_pips": round(pnl_pips, 1),
                                "candles_in_trade": self._candles_in_trade,
                            }, status="ok", note=f"Peak-decel close at {pnl_pips:.1f} pips (separation slowing)")
                    except Exception as _pd_err:
                        logger.error("[PEAK-DECEL] Close failed for trade %s: %s", self.trade_id, _pd_err)

            # ══════════════════════════════════════════════════════════════
            # RULE 6: BAD ENTRY — trade was never profitable, cut early
            #
            # Catches trades that entered at the wrong part of the cycle
            # (e.g. SELL entered while price was retracing UP into E100).
            # These trades were never working — no retrace-from-profit to
            # manage, just a wrong entry bleeding to SL.
            #
            # Conditions (ALL must be true):
            #   - Open >= 10 M1 candles (gave it a fair chance)
            #   - Peak P&L never exceeded +3 pips (never really worked)
            #   - Current P&L worse than -10 pips (meaningfully underwater)
            #   - E100 tested at least once (confirms price at wrong level)
            #
            # This is NOT Rule 3 (which protects profits during pullback).
            # This asks: "was this entry fundamentally wrong?"
            # ══════════════════════════════════════════════════════════════
            # 2026-04-01: Rule 6 (Bad Entry) DISABLED.
            # It was closing trades that were in valid retracements or about to
            # recover. The -10p early close saved ~3p vs full SL but killed
            # trades that would have turned profitable. Losses today from Rule 6:
            #   #3389 EUR_AUD -$89.65, #3457 USD_CAD (in retrace territory).
            # Let the original SL do its job instead.
            if False:  # Rule 6 disabled
                logger.warning(
                    "⛔ [BAD ENTRY] %s #%s: never profitable (peak=%.1fp), now %.1fp after %d candles, "
                    "E100 tested %d times (dist=%.1f%%) → CLOSING bad entry",
                    self.instrument, self.trade_id, self._peak_pnl_pips, pnl_pips,
                    self._candles_in_trade, self._e100_tests_in_retrace,
                    e100_dist_pct * 100
                )
                try:
                    await self._close_with_reason("e100_proximity_exit")
                    self._rule6_fired = True
                    if flight:
                        flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                      trade_id=self.trade_id, data={
                            "action": "bad_entry_close",
                            "reason": "never_profitable_e100_test",
                            "peak_pnl_pips": round(self._peak_pnl_pips, 1),
                            "pnl_pips": round(pnl_pips, 1),
                            "candles_in_trade": self._candles_in_trade,
                            "e100_tests": self._e100_tests_in_retrace,
                            "e100_dist_pct": round(e100_dist_pct, 3),
                            "retrace_state": self._retrace_state,
                        }, status="warn", note=f"Bad entry close at {pnl_pips:.1f}p (peak was {self._peak_pnl_pips:.1f}p, never worked)")
                except Exception as _r6_err:
                    logger.error("[BAD ENTRY] Close failed for trade %s: %s", self.trade_id, _r6_err)

            # Add dynamic exit info to threat dict
            threat['dynamic_exit'] = {
                'retrace_state': self._retrace_state,
                'retrace_depth_pct': round(self._retrace_depth * 100, 1),
                'retrace_candles': self._retrace_candle_count,
                'e100_tests_in_retrace': self._e100_tests_in_retrace,
                'e100_approaching': e100_approaching,
                'reexpansion_count': self._reexpansion_count,
                'ema_sep_velocity_negative_count': self._ema_sep_velocity_negative_count,
                'bb_contracting_count': self._bb_contracting_count,
                'candles_in_trade': self._candles_in_trade,
                'ema_separation': self._ema_sep_history[-1] if self._ema_sep_history else 0,
                'bb_width': self._bb_width_history[-1] if self._bb_width_history else 0,
                'fan_width_pct': round(fan_width_pct, 4),
                'e100_dist_pct': round(e100_dist_pct, 3) if e100_dist_pct < 999 else None,
                'e100_reversal_candle': e100_reversal_candle,
                # Parallel tracking: BB and fan moving together?
                'bb_ema_parallel': both_contracting or both_expanding,
                # Rule 5 diagnostics
                'peak_decel_d1': [round(v, 6) for v in self._ema_sep_vel_history[-3:]] if len(self._ema_sep_vel_history) >= 3 else [],
                'peak_decel_fired': self._peak_decel_close_fired,
            }

        except Exception as e:
            logger.error("Dynamic exit check error for trade %s: %s", self.trade_id, e)

    async def _check_smart_exit(
        self,
        pnl_pips: float,
        r_mult: float,
        unrealized_pl: float,
        market: Dict,
        threat: Dict,
        current_price: float,
    ):
        """Smart profit-taking: detect when the move is exhausting via RSI, Stoch, BB, EMA.
        
        This is NOT about danger — it's about recognizing "the move is done, take profit."
        Only acts when we're in profit. Watches for:
        1. RSI reversing from extreme (was OB/OS, now crossing back)
        2. Stochastic crossing back from extreme
        3. BB bands contracting (squeeze starting = momentum dying)
        4. EMA separation velocity going negative (trend decelerating)
        
        When 2+ signals agree AND we're in profit → tighten SL aggressively or close.
        """
        # Only care about smart exits when we have meaningful profit
        if pnl_pips < 3.0 or unrealized_pl < 1.50:
            return

        try:
            loop = asyncio.get_event_loop()
            is_long = self.direction == 'buy'

            # ── PROFIT-LOCK: threat reasons override ──────────────────────────────
            # If the guardian is already seeing "peaked against trade" or
            # "collapsing against trade" these are the SAME signals that caused
            # EUR_USD #1644 to give back $5.60 profit and end at -$16.
            # When we're in profit AND these specific reasons appear for 2+
            # consecutive minutes → move SL to lock in at least breakeven immediately.
            # This fires BEFORE the slower RSI/Stoch signal logic below.
            _threat_reasons = threat.get('reasons', [])
            _adverse_fan = any(
                p in r for r in _threat_reasons
                for p in ('peaked against trade', 'collapsing against trade',
                          'TREND AGAINST')
            )
            # Track peak pnl seen so far (used for lock calculation)
            self._profit_lock_peak_pips = max(
                getattr(self, '_profit_lock_peak_pips', 0.0), pnl_pips
            )

            # Only count consecutive adverse minutes when we have REAL profit (3+ pips)
            # and the fan signal is adverse. Below 3 pips it's M1 noise — don't fire.
            if _adverse_fan and pnl_pips >= 3.0:
                self._adverse_fan_consecutive = getattr(self, '_adverse_fan_consecutive', 0) + 1
            else:
                self._adverse_fan_consecutive = 0

            # Fire after 3 consecutive adverse minutes (not 2) — gives trade room to breathe
            # Lock based on 50% of PEAK profit seen, not current (avoids locking near zero on dips)
            if self._adverse_fan_consecutive >= 3 and pnl_pips >= 3.0:
                peak_pips = self._profit_lock_peak_pips
                lock_pips = max(1.0, peak_pips * 0.50)  # lock 50% of best profit seen
                new_sl = round(
                    self.entry_price + lock_pips * self.pip_size if is_long
                    else self.entry_price - lock_pips * self.pip_size,
                    self.display_precision
                )
                _should_move = (is_long and new_sl > self.stop_loss) or \
                               (not is_long and new_sl < self.stop_loss)

                if _should_move:
                    try:
                        await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                            self.trade_id,
                            stop_loss={"price": str(new_sl), "timeInForce": "GTC"}
                        ))
                        self.stop_loss = new_sl
                        self._sl_moved_to_be = True
                        logger.info(
                            "[GUARDIAN] PROFIT-LOCK %s %s: 'peaked/collapsing against trade' "
                            "×%d consecutive | current=%.1fp peak=%.1fp | SL → %.5f (locking %.1fp = 50%% of peak)",
                            self.trade_id, self.instrument,
                            self._adverse_fan_consecutive, pnl_pips, peak_pips, new_sl, lock_pips
                        )
                        if flight:
                            flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                          trade_id=self.trade_id, data={
                                "action": "profit_lock_adverse_fan",
                                "consecutive_adverse_minutes": self._adverse_fan_consecutive,
                                "pnl_pips": pnl_pips,
                                "peak_pips": peak_pips,
                                "lock_pips": lock_pips,
                                "new_sl": new_sl,
                                "reasons": _threat_reasons[:3],
                            }, note=f"Profit-lock: adverse fan ×{self._adverse_fan_consecutive}min → SL {new_sl} (50% of {peak_pips:.1f}p peak)")
                    except Exception as _pl_err:
                        logger.error("[GUARDIAN] Profit-lock SL move failed %s: %s",
                                     self.trade_id, _pl_err)
            # ─────────────────────────────────────────────────────────────────────
            
            # --- Gather current indicator values ---
            # RSI from M15 buffer (more reliable than M1)
            rsi = 50.0
            stoch_k = 50.0
            
            if self._m15_buffer and len(self._m15_buffer) >= 14:
                try:
                    import pandas as _pd
                    _m15_df = _pd.DataFrame(self._m15_buffer)
                    for _col in ('open', 'high', 'low', 'close'):
                        _m15_df[_col] = _m15_df[_col].astype(float)
                    
                    # Compute RSI
                    _close = _m15_df['close']
                    _delta = _close.diff()
                    _gain = _delta.where(_delta > 0, 0.0).rolling(14).mean()
                    _loss = (-_delta.where(_delta < 0, 0.0)).rolling(14).mean()
                    _rs = _gain / _loss.replace(0, float('nan'))
                    _rsi_series = 100 - (100 / (1 + _rs))
                    rsi = float(_rsi_series.iloc[-1]) if not _rsi_series.empty else 50.0
                    
                    # Compute Stochastic K (14-period)
                    _low14 = _m15_df['low'].astype(float).rolling(14).min()
                    _high14 = _m15_df['high'].astype(float).rolling(14).max()
                    _stoch = ((_close - _low14) / (_high14 - _low14).replace(0, float('nan'))) * 100
                    stoch_k = float(_stoch.iloc[-1]) if not _stoch.empty else 50.0
                except Exception as e:
                    logger.warning("[GUARDIAN] RSI/Stoch calc failed for %s: %s", self.instrument, e)
            
            # Fallback to market state if M15 computation failed
            if rsi == 50.0:
                _m_rsi = market.get('rsi', {})
                rsi = _m_rsi.get('value', _m_rsi) if isinstance(_m_rsi, dict) else float(_m_rsi or 50)
            if stoch_k == 50.0:
                _m_stoch = market.get('stochastic', {})
                stoch_k = _m_stoch.get('k', _m_stoch) if isinstance(_m_stoch, dict) else float(_m_stoch or 50)
            
            self._rsi_history.append(rsi)
            self._stoch_history.append(stoch_k)
            if len(self._rsi_history) > 60:
                self._rsi_history = self._rsi_history[-60:]
            if len(self._stoch_history) > 60:
                self._stoch_history = self._stoch_history[-60:]
            
            # --- Signal 1: RSI exhaustion reversal ---
            # For a SELL: RSI was oversold (<30), now crossing back above 30 = buyers returning
            # For a BUY: RSI was overbought (>70), now crossing back below 70 = sellers returning
            rsi_exit = False
            if is_long:
                if any(r > 70 for r in self._rsi_history[-10:]):
                    self._rsi_crossed_extreme = True
                if self._rsi_crossed_extreme and rsi < 65:
                    rsi_exit = True  # Was OB, now falling back
            else:
                if any(r < 30 for r in self._rsi_history[-10:]):
                    self._rsi_crossed_extreme = True
                if self._rsi_crossed_extreme and rsi > 35:
                    rsi_exit = True  # Was OS, now rising back
            
            # --- Signal 2: Stochastic reversal from extreme ---
            stoch_exit = False
            if len(self._stoch_history) >= 3:
                prev_stoch = self._stoch_history[-3]
                if is_long and prev_stoch > 80 and stoch_k < 75:
                    stoch_exit = True
                elif not is_long and prev_stoch < 20 and stoch_k > 25:
                    stoch_exit = True
            
            # --- Signal 3: BB contracting (from dynamic exit tracking) ---
            bb_squeeze = self._bb_contracting_count >= 3
            
            # --- Signal 4: EMA separation decelerating ---
            ema_fading = self._ema_sep_velocity_negative_count >= 3
            
            # --- Count concurrent exit signals ---
            self._exit_signals = sum([rsi_exit, stoch_exit, bb_squeeze, ema_fading])
            
            # Add to threat dict for dashboard/monitor visibility
            threat['smart_exit'] = {
                'rsi': round(rsi, 1),
                'stoch_k': round(stoch_k, 1),
                'rsi_exit': rsi_exit,
                'stoch_exit': stoch_exit,
                'bb_squeeze': bb_squeeze,
                'ema_fading': ema_fading,
                'signal_count': self._exit_signals,
                'rsi_was_extreme': self._rsi_crossed_extreme,
            }
            
            # --- ACT: signals agree while in profit → tighten aggressively ---
            # Fan-aware thresholds: if trend is healthy (expanding in our favor), require MORE signals
            # A healthy trend consolidating will trip RSI + BB squeeze — that's normal, not exhaustion
            _fan_state = market.get('ema', {}).get('fan_state', 'mixed')
            _fan_dir = market.get('ema', {}).get('fan_direction', 'neutral')
            _fan_favorable = ((is_long and _fan_dir == 'bullish') or (not is_long and _fan_dir == 'bearish'))
            _fan_healthy = _fan_favorable and _fan_state in ('expanding', 'peaked')

            # Healthy trend = need 3+ signals (not 2). Also need more profit to lock.
            _signal_threshold = 3 if _fan_healthy else 2
            _profit_threshold = 8.0 if _fan_healthy else 5.0
            _close_signals = 4 if _fan_healthy else 3
            _close_profit = 12.0 if _fan_healthy else 8.0
            _close_profit = max(_close_profit, 5.0)

            if self._exit_signals >= _signal_threshold and pnl_pips >= _profit_threshold:
                # Move SL to lock in most of the profit (entry + 60% of current profit)
                lock_pips = pnl_pips * 0.60
                if is_long:
                    new_sl = self.entry_price + (lock_pips * self.pip_size)
                else:
                    new_sl = self.entry_price - (lock_pips * self.pip_size)
                new_sl = round(new_sl, self.display_precision)
                
                # Only tighten, never widen
                _move = False
                if is_long and new_sl > self.stop_loss:
                    _move = True
                elif not is_long and new_sl < self.stop_loss:
                    _move = True
                
                if _move:
                    try:
                        await loop.run_in_executor(None, lambda: self._client.set_trade_orders(
                            self.trade_id,
                            stop_loss={"price": str(new_sl), "timeInForce": "GTC"}
                        ))
                        self.stop_loss = new_sl
                        self._sl_moved_to_be = True
                        
                        signals = []
                        if rsi_exit: signals.append(f'RSI reversal({rsi:.0f})')
                        if stoch_exit: signals.append(f'Stoch reversal({stoch_k:.0f})')
                        if bb_squeeze: signals.append('BB squeeze')
                        if ema_fading: signals.append('EMA fading')
                        
                        logger.info("Trade %s: SMART EXIT — %d signals [%s] | locking %.1f pips profit (SL→%.5f) | P&L: $%.2f",
                                   self.trade_id, self._exit_signals, ', '.join(signals),
                                   lock_pips, new_sl, unrealized_pl)
                        
                        if flight:
                            flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                          trade_id=self.trade_id, data={
                                "action": "smart_exit_tighten",
                                "signals": signals,
                                "signal_count": self._exit_signals,
                                "lock_pips": lock_pips,
                                "new_sl": new_sl,
                                "rsi": rsi,
                                "stoch_k": stoch_k,
                                "pnl_pips": pnl_pips,
                            }, note=f"Smart exit: {self._exit_signals} signals → lock {lock_pips:.1f} pips")
                    except Exception as e:
                        logger.error("Smart exit SL move failed for trade %s: %s", self.trade_id, e)
            
            # --- Close threshold: fan-aware (healthy trend needs more conviction) ---
            if self._exit_signals >= _close_signals and pnl_pips >= _close_profit:
                logger.warning("Trade %s: SMART EXIT CLOSE — %d signals, move exhausted at %.1f pips / $%.2f",
                              self.trade_id, self._exit_signals, pnl_pips, unrealized_pl)
                try:
                    await self._close_with_reason("smart_exit_signals")
                    if flight:
                        flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                      trade_id=self.trade_id, data={
                            "action": "smart_exit_close",
                            "signals": self._exit_signals,
                            "pnl_pips": pnl_pips,
                            "unrealized_pl": unrealized_pl,
                            "rsi": rsi,
                            "stoch_k": stoch_k,
                        }, status="warn", note=f"Smart exit: {self._exit_signals} signals → CLOSE at {pnl_pips:.1f} pips")
                except Exception as e:
                    logger.error("Smart exit close failed for trade %s: %s", self.trade_id, e)
                    
        except Exception as e:
            # 2026-04-24: upgraded — smart_exit runs on every tick, early-exits
            # on degrading setups. Silent failure = smart-exit disabled.
            logger.warning("Smart exit check FAILED for trade %s: %s: %s (smart-exit inactive this tick)",
                           self.trade_id, type(e).__name__, e)

    async def _fetch_m15(self):
        try:
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(None, lambda: self._client.get_candles(
                self.instrument, granularity='M15', count=200))
            self._m15_buffer = _norm_list(raw) if raw else self._m15_buffer
            self._m15_last_fetch = time.time()

            # Recompute structural signals only when a new M15 bar arrives
            # 2026-04-14: BUGFIX — was comparing len(buffer) but buffer is rolling fixed-size
            # (count=200). Length never changed, so cache was computed ONCE at spawn and never
            # refreshed. decel_bars / peak_bars / return_exit_bars became stale indices into
            # an ever-shifting buffer. Fix: compare latest-bar timestamp.
            _new_len = len(self._m15_buffer)
            _latest_time = self._m15_buffer[-1].get('time', '') if self._m15_buffer else ''
            _cached_time = self._m15_signal_cache.get('last_computed_time', '')
            if _latest_time and _latest_time != _cached_time and _new_len >= 30:
                try:
                    from backtester.ema_separation import (
                        calculate_ema as _ce, measure_separation as _ms,
                        detect_deceleration as _dd, detect_peak_separation as _dp,
                    )
                    _closes = [float(c['close']) for c in self._m15_buffer]
                    _e21 = _ce(_closes, 21)
                    _e55 = _ce(_closes, 55)
                    _seps = _ms(_e21, _e55, _closes)

                    # Deceleration and peak bars
                    _decel = set(_dd(_seps))
                    _peaks = set(max(p - 3, 0) for p in _dp(_seps))

                    # Return-to-E100 bars (price was away, came back)
                    _e100 = _ce(_closes, 100)
                    _return_bars = set()
                    _was_away = False
                    _away_dir = None
                    for _i in range(101, _new_len):
                        if not _e100[_i] or not _e100[_i-1]: continue
                        _dist = (_closes[_i] - _e100[_i]) / _closes[_i] * 100
                        if not _was_away:
                            if abs(_dist) > 0.12:
                                _was_away = True
                                _away_dir = 'above' if _dist > 0 else 'below'
                        else:
                            if abs(_dist) < 0.04:
                                _return_bars.add(_i)
                                _was_away = False
                                _away_dir = None

                    self._m15_signal_cache = {
                        'decel_bars': _decel,
                        'peak_bars': _peaks,
                        'return_exit_bars': _return_bars,
                        'last_computed_time': _latest_time,
                    }
                    logger.debug("Trade %s: M15 signals recomputed (len=%d, decel=%d, peaks=%d, returns=%d)",
                                 self.trade_id, _new_len, len(_decel), len(_peaks), len(_return_bars))
                except Exception as _sig_err:
                    logger.debug("M15 signal cache failed for %s: %s", self.trade_id, _sig_err)

        except Exception as e:
            logger.debug("M15 fetch failed for %s: %s", self.instrument, e)

    def _compute_projection(
        self, price: float, pnl_pips: float, unrealized_pl: float,
        atr_m1: float, ema_state: Dict,
    ) -> Dict[str, Any]:
        """Compute trade P&L projection based on current momentum.

        Returns:
            pip_value_usd: $ per pip for this position
            current_pl_usd: current unrealized P&L in $
            tp_pips_remaining: pips to take profit
            sl_pips_remaining: pips to stop loss
            tp_pl_usd: projected $ if TP hit
            sl_pl_usd: projected $ if SL hit
            est_candles_to_tp: estimated M1 candles to reach TP (based on ATR)
            est_time_to_tp: human-readable time estimate
            momentum: 'accelerating', 'steady', 'fading'
            rr_live: live reward:risk ratio from current price
        """
        # $ per pip calculation
        # For USD-quoted pairs (XXX_USD), pip value = units × pip_size exactly.
        # The unrealized_pl / pnl_pips ratio is WRONG for display because OANDA
        # computes unrealized_pl at bid/ask (the price to close) while pnl_pips
        # is mid-price based — the spread gap creates a ~15-20% error on small
        # moves (e.g. $0.82/pip instead of $1.00/pip for 10K EUR_USD).
        _quote_ccy = self.instrument.split('_')[-1] if '_' in self.instrument else ''
        if _quote_ccy == 'USD':
            # Direct: units × pip_size is exact for USD-denominated accounts
            pip_value = self.units * self.pip_size
        elif abs(pnl_pips) > 0.5 and abs(unrealized_pl) > 0.01:
            # Cross pairs / USD-base pairs: derive from OANDA P&L ratio
            # Require larger thresholds to reduce spread noise
            pip_value = abs(unrealized_pl / pnl_pips)
        else:
            # Fallback estimate for cross pairs with tiny moves
            pip_value = self.units * self.pip_size

        is_long = self.direction == 'buy'

        # Distance to TP and SL from current price
        if self.take_profit > 0:
            tp_pips = ((self.take_profit - price) / self.pip_size) if is_long else ((price - self.take_profit) / self.pip_size)
        else:
            tp_pips = 0

        sl_pips = ((price - self.stop_loss) / self.pip_size) if is_long else ((self.stop_loss - price) / self.pip_size)

        # Projected P&L at TP and SL
        tp_dist_from_entry = ((self.take_profit - self.entry_price) / self.pip_size) if is_long else ((self.entry_price - self.take_profit) / self.pip_size) if self.take_profit > 0 else 0
        sl_dist_from_entry = ((self.entry_price - self.stop_loss) / self.pip_size) if is_long else ((self.stop_loss - self.entry_price) / self.pip_size)

        tp_pl = tp_dist_from_entry * pip_value if self.take_profit > 0 else 0
        sl_pl = -sl_dist_from_entry * pip_value

        # Live R:R from current price
        rr_live = tp_pips / sl_pips if sl_pips > 0.1 else 0

        # Estimate candles to TP using M1 ATR
        # ATR = average range per candle. Price needs to cover tp_pips.
        # Not all movement is directional, so use ~40% of ATR as directional progress
        if atr_m1 > 0 and tp_pips > 0:
            directional_progress = (atr_m1 / self.pip_size) * 0.4  # pips of directional movement per M1
            if directional_progress > 0:
                est_candles = tp_pips / directional_progress
            else:
                est_candles = 999
        else:
            est_candles = 0

        # Adjust for momentum
        velocity = ema_state.get('separation_velocity', 0)
        fan = ema_state.get('fan_state', 'mixed')
        fan_dir = ema_state.get('fan_direction', 'neutral')

        # Momentum assessment
        favorable_fan = ((is_long and fan_dir == 'bullish') or (not is_long and fan_dir == 'bearish'))
        if fan == 'expanding' and favorable_fan:
            momentum = 'accelerating'
            est_candles *= 0.7  # faster arrival
        elif fan in ('peaked', 'contracting') or (velocity is not None and velocity < 0.003):
            momentum = 'fading'
            est_candles *= 1.5  # slower arrival
        else:
            momentum = 'steady'

        # Time estimate
        est_minutes = int(est_candles)  # M1 candles = minutes
        if est_minutes <= 0:
            time_str = '-'
        elif est_minutes < 60:
            time_str = f'~{est_minutes}m'
        elif est_minutes < 1440:
            hours = est_minutes / 60
            time_str = f'~{hours:.1f}h'
        else:
            days = est_minutes / 1440
            time_str = f'~{days:.1f}d'

        return {
            'pip_value_usd': round(pip_value, 4),
            'current_pl_usd': round(unrealized_pl, 2),
            'pnl_pips': round(pnl_pips, 1),
            'peak_pnl_pips': round(self._peak_pnl_pips, 1),    # MFE: highest profit reached
            'max_adverse_pips': round(self._max_adverse_pips, 1), # MAE: worst drawdown reached
            'tp_pips_remaining': round(tp_pips, 1),
            'sl_pips_remaining': round(sl_pips, 1),
            'tp_pl_usd': round(tp_pl, 2),
            'sl_pl_usd': round(sl_pl, 2),
            'est_candles_to_tp': int(est_candles),
            'est_time_to_tp': time_str,
            'momentum': momentum,
            'rr_live': round(rr_live, 2),
            'units': int(self.units),
            'entry_price': self.entry_price,
            'take_profit': self.take_profit,
            'stop_loss': self.stop_loss,
        }

    def _get_normal_spread(self) -> float:
        if len(self._spread_history) >= 10:
            s = sorted(self._spread_history)
            return s[len(s) // 2]
        return DEFAULT_SPREADS.get(self.instrument, 0.00015)


# ---------------------------------------------------------------------------
# Position Guardian - manages all TradeWatchers
# ---------------------------------------------------------------------------

class PositionGuardian:
    """Top-level manager that spawns/reaps TradeWatchers for all open trades.

    Polls OANDA every RECONCILE_INTERVAL_S for new/closed trades and
    creates/destroys watchers accordingly. All watchers run in parallel.

    Callbacks:
        on_status_update(trade_id, threat_dict)  - every M1, every trade
        on_escalation(trade_id, report_dict)     - RED zone → Trade Monitor
        on_emergency(trade_id, reason_str)        - BLACK zone (after kill)
    """

    def __init__(
        self,
        oanda_client,
        on_status_update: Optional[Callable] = None,
        on_escalation: Optional[Callable] = None,
        on_emergency: Optional[Callable] = None,
        user_id: Optional[int] = None,
    ):
        self._client = oanda_client
        self._on_status = on_status_update
        self._on_escalation = on_escalation
        self._on_emergency = on_emergency
        self._user_id = user_id  # Must be provided by caller — no hardcoded default

        self._watchers: Dict[str, TradeWatcher] = {}  # trade_id → watcher
        self._trade_theses: Dict[str, Dict] = {}      # instrument → thesis context from scout
        self._running = False
        self._reconcile_task: Optional[asyncio.Task] = None

        # Account summary cache — shared across all watchers, refreshed every 30s
        # Prevents N identical OANDA account calls per evaluation round (one per watcher)
        self._account_cache: Optional[dict] = None
        self._account_cache_ts: float = 0.0
        _ACCOUNT_CACHE_TTL = 30  # seconds
        self._account_cache_ttl = _ACCOUNT_CACHE_TTL

        # Candle cache — shared across watchers on the same instrument
        # Prevents duplicate OANDA candle calls when multiple trades on same pair
        self._candle_cache: dict = {}  # (instrument, granularity) → (timestamp, data)
        _CANDLE_CACHE_TTL = 45  # seconds
        self._candle_cache_ttl = _CANDLE_CACHE_TTL

        self._stats = {
            'trades_watched': 0,
            'escalations': 0,
            'emergency_closes': 0,
            'profit_protection_actions': 0,
        }

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def active_watchers(self) -> int:
        return len(self._watchers)

    def get_all_threats(self) -> Dict[str, Dict]:
        """Get latest threat assessment for every watched trade."""
        return {tid: w.last_threat for tid, w in self._watchers.items() if w.last_threat}

    async def _get_account_summary_cached(self) -> dict:
        """Return cached account summary (refreshes every 30s).

        Without this, each TradeWatcher fetches account summary independently
        every evaluation cycle. With 5 open trades that's 5 identical OANDA
        calls per minute — this cuts it to 1 per 30s regardless of watcher count.
        """
        now = time.time()
        if self._account_cache is None or (now - self._account_cache_ts) > self._account_cache_ttl:
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: self._client._request("GET",
                        f"/v3/accounts/{self._client.account_id}/summary")
                )
                if result and "account" in result:
                    self._account_cache = result["account"]
                    self._account_cache_ts = now
            except Exception as e:
                logger.debug("[GUARDIAN] Account summary cache refresh failed: %s", e)
        return self._account_cache or {}

    async def _get_candles_cached(self, instrument: str, granularity: str, count: int) -> list:
        """Return cached candles for (instrument, granularity), refreshed every 45s.

        Prevents duplicate OANDA candle fetches when multiple watchers are
        monitoring the same instrument simultaneously.
        """
        key = (instrument, granularity)
        now = time.time()
        if key in self._candle_cache:
            ts, data = self._candle_cache[key]
            if now - ts < self._candle_cache_ttl:
                return data
        try:
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(
                None,
                lambda: self._client.get_candles(instrument, granularity=granularity, count=count)
            )
            if raw:
                self._candle_cache[key] = (now, raw)
                return raw
        except Exception as e:
            logger.debug("[GUARDIAN] Candle cache refresh failed %s %s: %s", instrument, granularity, e)
        # Return stale data if available, empty list otherwise
        if key in self._candle_cache:
            return self._candle_cache[key][1]
        return []

    def get_threat(self, trade_id: str) -> Optional[Dict]:
        w = self._watchers.get(trade_id)
        return w.last_threat if w else None

    def get_stats(self) -> Dict:
        return {
            **self._stats,
            'active_watchers': len(self._watchers),
            'watched_instruments': list(set(w.instrument for w in self._watchers.values())),
        }

    async def start(self):
        """Start the guardian. Begins reconcile loop that spawns watchers."""
        if self._running:
            return
        self._running = True
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        logger.info("Position Guardian started - reconciling every %ds", RECONCILE_INTERVAL_S)

    async def stop(self):
        """Stop the guardian and all watchers."""
        self._running = False
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try: await self._reconcile_task
            except asyncio.CancelledError: pass

        # Stop all watchers in parallel
        if self._watchers:
            await asyncio.gather(*(w.stop() for w in self._watchers.values()), return_exceptions=True)
            self._watchers.clear()

        logger.info("Position Guardian stopped. Stats: %s", json.dumps(self._stats))

    def register_thesis(self, instrument: str, thesis: Dict):
        """Register trade thesis context for guardian to use when spawning watcher.
        Called by trading_cycle after a trade is placed."""
        self._trade_theses[instrument] = thesis
        logger.info("Registered thesis for %s: %s", instrument, 
                     thesis.get('entry_type', 'unknown'))

    async def _reconcile_loop(self):
        """Periodically check OANDA for new/closed trades, spawn/reap watchers."""
        while self._running:
            try:
                await self._reconcile()
                await asyncio.sleep(RECONCILE_INTERVAL_S)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Reconcile error: %s", e, exc_info=True)
                await asyncio.sleep(30)

    async def _reconcile(self):
        """Sync watchers with OANDA's open trades."""
        loop = asyncio.get_event_loop()

        try:
            open_trades = await loop.run_in_executor(None, self._client.get_open_trades)
        except Exception as e:
            logger.warning("Failed to get open trades: %s", e)
            return

        open_trade_ids: Set[str] = set()

        for t in open_trades:
            trade_id = str(t.get('id', ''))
            if not trade_id:
                continue
            open_trade_ids.add(trade_id)

            # Already watching?
            if trade_id in self._watchers:
                continue

            # New trade - spawn watcher
            instrument = t.get('instrument', '')
            units = float(t.get('currentUnits', t.get('initialUnits', 0)))
            direction = 'buy' if units > 0 else 'sell'
            entry_price = float(t.get('price', 0))

            # Determine pip size and precision
            if 'JPY' in instrument:
                pip_size = 0.01
                precision = 3
            else:
                pip_size = 0.0001
                precision = 5

            # Get stop loss and take profit
            sl_order = t.get('stopLossOrder', {})
            stop_loss = float(sl_order.get('price', 0)) if sl_order else 0
            tp_order = t.get('takeProfitOrder', {})
            take_profit = float(tp_order.get('price', 0)) if tp_order else 0

            # ── AUTO-ATTACH SL/TP for trades missing them (e.g. manual trades) ──
            # Instead of skipping the trade, compute SL/TP from ATR and attach
            # via OANDA API. Manual trades deserve the same guardian protection.
            if not stop_loss:
                try:
                    _candles = await self._get_candles_cached(instrument, "M15", 30)
                    if len(_candles) >= 15:
                        import numpy as np
                        _highs = np.array([float(c['mid']['h']) for c in _candles if c.get('complete', True)])
                        _lows  = np.array([float(c['mid']['l']) for c in _candles if c.get('complete', True)])
                        _closes = np.array([float(c['mid']['c']) for c in _candles if c.get('complete', True)])
                        _tr = np.maximum(
                            _highs[1:] - _lows[1:],
                            np.maximum(np.abs(_highs[1:] - _closes[:-1]), np.abs(_lows[1:] - _closes[:-1]))
                        )
                        _atr = float(np.mean(_tr[-14:])) if len(_tr) >= 14 else float(np.mean(_tr))

                        # 2026-04-01: SL raised to 2.5×ATR (was 1.5×) — matches snipe direct path
                        # Old 1.5× was too tight; retracements hitting SL before guardian could act
                        _sl_mult = 2.5
                        _tp_mult = 2.0
                        if direction == 'buy':
                            stop_loss = round(entry_price - (_atr * _sl_mult), precision)
                            if not take_profit:
                                take_profit = round(entry_price + (_atr * _tp_mult), precision)
                        else:
                            stop_loss = round(entry_price + (_atr * _sl_mult), precision)
                            if not take_profit:
                                take_profit = round(entry_price - (_atr * _tp_mult), precision)

                        # Attach SL/TP to the OANDA trade
                        _orders = {"stop_loss": {"price": str(stop_loss), "timeInForce": "GTC"}}
                        if take_profit:
                            _orders["take_profit"] = {"price": str(take_profit), "timeInForce": "GTC"}
                        await loop.run_in_executor(
                            None, lambda: self._client.set_trade_orders(trade_id, **_orders)
                        )
                        _sl_pips = abs(entry_price - stop_loss) / pip_size
                        _tp_pips = abs(entry_price - take_profit) / pip_size if take_profit else 0
                        logger.info(
                            "🛡️ [GUARDIAN] Auto-attached SL/TP to trade %s (%s %s): "
                            "SL=%s (%.1fp), TP=%s (%.1fp) [1.5×ATR=%.1fp]",
                            trade_id, instrument, direction,
                            stop_loss, _sl_pips, take_profit, _tp_pips, _atr / pip_size
                        )
                    else:
                        logger.warning(
                            "🛡️ [GUARDIAN] Trade %s (%s) has no SL and candle fetch returned only %d bars "
                            "— cannot compute ATR. Skipping guardian for safety.",
                            trade_id, instrument, len(_candles)
                        )
                        continue
                except Exception as _sl_err:
                    logger.error(
                        "🛡️ [GUARDIAN] Failed to auto-attach SL to trade %s (%s): %s — skipping",
                        trade_id, instrument, _sl_err
                    )
                    continue

            # Get thesis context if available (registered by trading_cycle)
            thesis = self._trade_theses.get(instrument, {})

            # ── MANUAL TRADE DETECTION ──
            # _trade_theses is keyed by instrument, so a stale thesis from a
            # previous cycle can make a manual trade look like 'auto'.
            # Explicitly check live_trades DB for source='manual'.
            _is_manual_trade = False
            _db_source: Optional[str] = None
            try:
                import sqlite3 as _sq3
                _lt_conn = _sq3.connect('~/Jarvis/Database/v2/trading_forex.db')
                _lt_row = _lt_conn.execute(
                    "SELECT source, entry_type FROM live_trades WHERE id = ? LIMIT 1",
                    (trade_id,)
                ).fetchone()
                _lt_conn.close()
                if _lt_row:
                    _db_source = _lt_row[0]
                if _lt_row and (_lt_row[0] in ('manual', 'scout') or _lt_row[1] in ('manual', 'scout')):
                    _is_manual_trade = True
                    _src_label = _lt_row[0] or _lt_row[1] or 'unknown'
                    logger.info(
                        "\U0001f6e1\ufe0f [GUARDIAN] Trade %s (%s) detected as %s — grace period active",
                        trade_id, instrument, _src_label.upper())
            except Exception as _mt_err:
                logger.debug("[GUARDIAN] Manual trade check failed for %s: %s", trade_id, _mt_err)
            if _is_manual_trade:
                if not thesis:
                    thesis = {}
                thesis['is_manual'] = True
            # Kronos Task 11: carry live_trades.source into thesis so the
            # watcher can resolve kronos.* TUNING for kronos_hunter trades.
            if _db_source:
                if not thesis:
                    thesis = {}
                thesis['source'] = _db_source

            watcher = TradeWatcher(
                trade_id=trade_id,
                instrument=instrument,
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                units=units,
                pip_size=pip_size,
                display_precision=precision,
                oanda_client=self._client,
                on_status_update=self._on_status,
                on_escalation=self._on_escalation,
                on_emergency=self._on_emergency,
                trade_thesis=thesis,
            )
            # Stamp actual open time from OANDA so close paths have correct entry_time
            # 2026-04-23: OANDA returns 9-digit fractional seconds (e.g. "...14.178476000Z")
            # which Python 3.10 fromisoformat() can't parse — silently fails, entry_time
            # stays at datetime.now() from __init__, which makes _trade_age_s ≈ 0 and
            # breaks BOTH the phase-restore AND peak-reconstruction gates downstream.
            # Fix: truncate fractional seconds to 6 digits before fromisoformat.
            # Fallback: DB live_trades.entry_time if OANDA parse fails.
            _oanda_open_time = t.get('openTime', '')
            _entry_parsed = False
            if _oanda_open_time:
                try:
                    _ot = _oanda_open_time.replace('Z', '+00:00')
                    # Truncate fractional seconds to 6 digits (microseconds)
                    if '.' in _ot:
                        _base, _rest = _ot.split('.', 1)
                        for _sep in ('+', '-'):
                            if _sep in _rest[1:]:
                                _i = _rest.index(_sep, 1)
                                _off = _rest[_i:]
                                _rest = _rest[:_i]
                                break
                        else:
                            _off = '+00:00'
                        _ot = f"{_base}.{_rest[:6]}{_off}"
                    watcher.entry_time = datetime.fromisoformat(_ot)
                    _entry_parsed = True
                except Exception as _ot_err:
                    logger.debug("[GUARDIAN] OANDA openTime parse failed for %s: %s (raw=%s)",
                                 trade_id, _ot_err, _oanda_open_time)
            # DB fallback so _trade_age_s is correct for restore gates
            if not _entry_parsed:
                try:
                    import sqlite3 as _sq3_et
                    _et_conn = _sq3_et.connect('~/Jarvis/Database/v2/trading_forex.db', timeout=3)
                    _et_row = _et_conn.execute(
                        "SELECT entry_time FROM live_trades WHERE id=? OR oanda_trade_id=? LIMIT 1",
                        (trade_id, trade_id)
                    ).fetchone()
                    _et_conn.close()
                    if _et_row and _et_row[0]:
                        _db_et = _et_row[0]
                        if _db_et.endswith('Z'):
                            _db_et = _db_et.replace('Z', '+00:00')
                        if '.' in _db_et:
                            _base2, _rest2 = _db_et.split('.', 1)
                            for _sep2 in ('+', '-'):
                                if _sep2 in _rest2[1:]:
                                    _i2 = _rest2.index(_sep2, 1)
                                    _off2 = _rest2[_i2:]
                                    _rest2 = _rest2[:_i2]
                                    break
                            else:
                                _off2 = '+00:00'
                            _db_et = f"{_base2}.{_rest2[:6]}{_off2}"
                        watcher.entry_time = datetime.fromisoformat(_db_et)
                        _entry_parsed = True
                        logger.debug("[GUARDIAN] Used DB entry_time fallback for %s: %s",
                                     trade_id, watcher.entry_time)
                except Exception as _db_et_err:
                    logger.debug("[GUARDIAN] DB entry_time fallback failed for %s: %s",
                                 trade_id, _db_et_err)

            self._watchers[trade_id] = watcher

            # ── Retrace state restoration on guardian respawn ─────────────────
            # 2026-04-15 (trade 5780 EUR_AUD -7.1p): serve_ui process restarts
            # every ~14 min wipe TradeWatcher in-memory state. Each fresh spawn
            # resets _retrace_state='trending' and _bb/_ema counters to 0, so
            # the 3-M15-bar threshold to flip to 'retracing' is never met and
            # guardian treats normal pullbacks as terminal. For trades open
            # >5 min, restore last-known phase from trade_phases table so the
            # state machine continues where it left off.
            try:
                _trade_age_s = 0.0
                if hasattr(watcher, 'entry_time'):
                    _trade_age_s = (datetime.now(timezone.utc) - watcher.entry_time).total_seconds()
                if _trade_age_s > 300:  # >5 min = pre-existing trade, not fresh entry
                    import sqlite3 as _sq3_ph
                    _ph_path = '<repo_root>/Source/flight_recorder.db'
                    _ph_conn = _sq3_ph.connect(_ph_path, timeout=5.0)
                    _ph_row = _ph_conn.execute(
                        "SELECT phase, retrace_depth, e100_tests, reexpansion_count, timestamp "
                        "FROM trade_phases WHERE trade_id = ? "
                        "ORDER BY timestamp DESC LIMIT 1",
                        (trade_id,)
                    ).fetchone()
                    if _ph_row and _ph_row[0] in ('retracing', 'continuing', 'peak', 'exhaustion'):
                        watcher._retrace_state = _ph_row[0]
                        # Bump counters past the 3-bar trigger so the state
                        # machine sees us as already in retrace, not rebuilding
                        # from scratch.
                        watcher._bb_contracting_count = 3
                        watcher._ema_sep_velocity_negative_count = 3
                        # Restore retrace-cycle state if we have it
                        if _ph_row[1]:
                            watcher._retrace_depth = float(_ph_row[1])
                        if _ph_row[2]:
                            watcher._e100_tests_in_retrace = int(_ph_row[2])
                        if _ph_row[3]:
                            watcher._reexpansion_count = int(_ph_row[3])
                        logger.warning(
                            "[GUARDIAN] Trade %s respawn: restored phase=%s "
                            "from trade_phases (age=%.0fs) — preserves M15 state "
                            "across serve_ui restart",
                            trade_id, _ph_row[0], _trade_age_s,
                        )
                        if flight:
                            try:
                                flight.record(
                                    "guardian_state_restore",
                                    pair=instrument, trade_id=trade_id,
                                    data={
                                        "phase": _ph_row[0],
                                        "trade_age_s": _trade_age_s,
                                        "source": "trade_phases",
                                    },
                                    note=f"Restored phase {_ph_row[0]} on respawn",
                                )
                            except Exception:
                                pass
                    _ph_conn.close()

                    # ── Peak PnL reconstruction from OANDA M15 (2026-04-23) ──────
                    # The prior approach (flight_log unrealized_pl) fails when guardian
                    # was DEAD during the profit window — flight_log has no data to read.
                    # Trade 9421 AUD_USD 2026-04-23: opened 06:20 ET, peaked +7.2p at
                    # 06:43, guardian died in server restart, peak was never logged, floor
                    # never engaged, trade swung back to -12p. New approach: fetch M15
                    # candles covering entry_time → now, compute peak favorable excursion
                    # from each candle's high/low vs direction. This reconstructs the peak
                    # that guardian would have observed if it had been alive.
                    # M15 is the trading timeframe — its candle high/low captures the
                    # relevant peak for profit-floor ratchet purposes.
                    try:
                        # Fetch enough M15 candles to cover the gap (+ buffer)
                        # 1 M15 bar = 15 min, so 30 bars = 7.5h of history
                        _candles_peak = await self._get_candles_cached(instrument, "M15", 30)
                        if _candles_peak and len(_candles_peak) >= 2:
                            _entry_ts = watcher.entry_time
                            if _entry_ts.tzinfo is None:
                                _entry_ts = _entry_ts.replace(tzinfo=timezone.utc)
                            _peak_pips_reconstructed = 0.0
                            _peak_bar_time = None
                            for _c in _candles_peak:
                                if not _c.get('complete', True):
                                    continue
                                _ct = _c.get('time', '')
                                try:
                                    _cdt = datetime.fromisoformat(_ct.replace('Z', '+00:00').split('.')[0] + '+00:00')
                                except Exception:
                                    continue
                                # Only consider candles at/after entry
                                if _cdt < _entry_ts:
                                    continue
                                _mid = _c.get('mid', {})
                                _hi = float(_mid.get('h', 0))
                                _lo = float(_mid.get('l', 0))
                                if _hi <= 0 or _lo <= 0:
                                    continue
                                # Peak favorable excursion per direction
                                if direction == 'buy':
                                    _excursion_pips = (_hi - entry_price) / pip_size
                                else:  # sell
                                    _excursion_pips = (entry_price - _lo) / pip_size
                                if _excursion_pips > _peak_pips_reconstructed:
                                    _peak_pips_reconstructed = _excursion_pips
                                    _peak_bar_time = _ct
                            # Only activate if we found meaningful profit history
                            # (>1p — below that, ratchet wouldn't engage anyway)
                            if _peak_pips_reconstructed > 1.0 and _peak_pips_reconstructed > watcher._peak_pnl_pips:
                                watcher._peak_pnl_pips = round(_peak_pips_reconstructed, 1)
                                # Mirror the floor rules from the live ratchet logic —
                                # conservative 70% lock (keep 70% of peak as the floor
                                # for tighter retracement protection).
                                watcher._failsafe_active = True
                                watcher._failsafe_sl_pips = max(0.5, _peak_pips_reconstructed * 0.70)
                                logger.warning(
                                    "[GUARDIAN] Trade %s respawn: reconstructed peak=+%.1fp "
                                    "from M15 candles (peak_bar=%s) → profit floor active at +%.1fp",
                                    trade_id, _peak_pips_reconstructed, _peak_bar_time,
                                    watcher._failsafe_sl_pips,
                                )
                                if flight:
                                    try:
                                        flight.record(
                                            "guardian_state_restore",
                                            pair=instrument, trade_id=trade_id,
                                            data={
                                                "peak_pips_reconstructed": round(_peak_pips_reconstructed, 1),
                                                "floor_activated_at": round(watcher._failsafe_sl_pips, 1),
                                                "peak_bar_time": _peak_bar_time,
                                                "source": "m15_candles",
                                                "trade_age_s": round(_trade_age_s, 0),
                                            },
                                            note=f"Restored peak +{_peak_pips_reconstructed:.1f}p from M15",
                                        )
                                    except Exception:
                                        pass
                    except Exception as _peak_err:
                        logger.debug("[GUARDIAN] M15 peak reconstruction failed for %s: %s",
                                     trade_id, _peak_err)

            except Exception as _restore_err:
                logger.debug("[GUARDIAN] Phase restore skipped for %s: %s",
                             trade_id, _restore_err)

            await watcher.start()
            self._stats['trades_watched'] += 1
            
            # Get historical exit learning context for this trade
            setup_name = 'unknown'
            regime = 'unknown'
            if thesis:
                setup_name = thesis.get('setup_name', 'unknown')
                regime = thesis.get('regime', 'unknown')
            
            learning_context = self._get_exit_learning_context(
                setup_name=setup_name,
                pair=instrument,
                regime=regime,
                user_id=self._user_id
            )
            
            # Store learning context in watcher for monitoring decisions
            if hasattr(watcher, '_learning_context'):
                watcher._learning_context = learning_context
            
            # Log learning insights if available
            if learning_context['trades'] > 0:
                partial_tp = learning_context.get('suggested_partial_tp_pips')
                strategy = learning_context.get('recommended_strategy', 'unknown')
                hit_10_rate = learning_context.get('hit_10_pips_rate', 0) * 100
                re_entry_rate = learning_context.get('re_entry_success_rate', 0) * 100
                
                logger.info("Trade %s learning: %d trades, partial TP @%.1f pips, 10pip hit %.0f%%, re-entry %.0f%%, strategy: %s",
                           trade_id, learning_context['trades'], partial_tp or 8, 
                           hit_10_rate, re_entry_rate, strategy)
            
            logger.info("Spawned watcher for trade %s: %s %s @ %.5f SL %.5f",
                        trade_id, direction, instrument, entry_price, stop_loss)
            if flight:
                flight.record(FlightStage.GUARDIAN_SPAWN, pair=instrument,
                              trade_id=trade_id, data={
                    "direction": direction, "entry_price": entry_price,
                    "stop_loss": stop_loss, "take_profit": take_profit,
                    "units": abs(units),
                    "learning_context": learning_context,
                }, note=f"Watching {direction} {instrument}")

        # ── Dead watcher task detection ──────────────────────────────────────
        # If a TradeWatcher._watch_loop() task crashes outside its inner
        # try/except, the asyncio task silently becomes done(). Without this
        # check the trade would go unmonitored indefinitely — no exit rules,
        # no guardian stops — until the whole process restarts.
        # On next reconcile the trade is still open in OANDA → fresh watcher
        # spawned automatically.
        _dead_watcher_ids = []
        for _tw_id, _tw in list(self._watchers.items()):
            _task = getattr(_tw, '_task', None)
            if _task is not None and _task.done():
                _cancelled = _task.cancelled()
                _exc = None
                if not _cancelled:
                    try:
                        _exc = _task.exception()
                    except Exception:
                        pass
                logger.error(
                    "[GUARDIAN] Watcher task for trade %s is DEAD "
                    "(cancelled=%s exc=%s) — removing from tracking; "
                    "fresh watcher spawns on next reconcile",
                    _tw_id, _cancelled, _exc,
                )
                try:
                    if flight:
                        flight.record(
                            "watcher_task_death",
                            pair=getattr(_tw, 'instrument', _tw_id),
                            trade_id=_tw_id,
                            data={"cancelled": _cancelled, "exception": str(_exc)},
                            status="error",
                            note=f"Watcher task died: {_exc}",
                        )
                except Exception:
                    pass
                _dead_watcher_ids.append(_tw_id)

        for _tw_id in _dead_watcher_ids:
            self._watchers.pop(_tw_id, None)
            self._stats['trades_watched'] = max(0, self._stats.get('trades_watched', 1) - 1)

        # Reap watchers for closed trades - record revenue
        closed = set(self._watchers.keys()) - open_trade_ids
        for trade_id in closed:
            watcher = self._watchers.pop(trade_id)
            await watcher.stop()

            # Look up closed trade from OANDA for final P&L.
            # 2026-04-14: Rewritten after trade 5699 recorded 0p / -$285 instead of the real
            # -71.3p / -$512.72. Race: reconcile ran at the exact millisecond of the OANDA SL
            # fill, get_trade() returned empty/transitional data, code fell to the
            # reconstruct-from-watcher path, which used entry_price as close_price → 0 pips.
            # New strategy:
            #   (1) Try get_trade with up to 3 retries + 500ms backoff (handles the close-moment
            #       race).
            #   (2) Only accept the response if state=CLOSED with a non-empty averageClosePrice.
            #   (3) If still no valid data, reconstruct — but NEVER fall back to entry_price for
            #       close_price (produces fake 0-pip rows). Use last watcher market price; if
            #       that's missing, mark outcome='needs_backfill' and flag for later repair.
            try:
                closed_trade = None
                _gt_err_last = None
                for _attempt in range(3):
                    try:
                        _resp = await loop.run_in_executor(
                            None, lambda tid=trade_id: self._client.get_trade(tid))
                        if _resp and _resp.get('state') == 'CLOSED' and _resp.get('averageClosePrice'):
                            closed_trade = _resp
                            break
                    except Exception as _gt_err:
                        _gt_err_last = _gt_err
                    if _attempt < 2:
                        await asyncio.sleep(0.5)
                # 2026-04-23: fall back to OANDA transactions API when get_trade fails
                # or returns stale data. Trade 9967 case: get_trade 404'd within seconds
                # of SL fill, guardian then used watcher pre-fill cached state for pnl_pips
                # (stale +4.2p) while OANDA actual was -3.4p loss. Transactions API still
                # had the correct ORDER_FILL. Try that before reconstructing from watcher.
                if not closed_trade:
                    try:
                        _tx_data = await loop.run_in_executor(
                            None,
                            lambda tid=trade_id: self._client.get_trade_close_from_transactions(tid)
                        )
                        if _tx_data and _tx_data.get('close_price') and _tx_data.get('close_time'):
                            closed_trade = {
                                'state': 'CLOSED',
                                'realizedPL': str(_tx_data.get('realized_pl', 0)),
                                'averageClosePrice': str(_tx_data.get('close_price', 0)),
                                'price': str(_tx_data.get('open_price', watcher.entry_price)),
                                'openTime': _tx_data.get('open_time', ''),
                                'closeTime': _tx_data.get('close_time', ''),
                                '_source': 'transactions_api',
                            }
                            logger.info(
                                "Trade %s: get_trade failed, recovered close via transactions API "
                                "(close_price=%s, realizedPL=%s, reason=%s)",
                                trade_id,
                                _tx_data.get('close_price'), _tx_data.get('realized_pl'),
                                _tx_data.get('reason', ''),
                            )
                    except Exception as _tx_err:
                        logger.debug("Trade %s: transactions-API fallback failed: %s",
                                     trade_id, _tx_err)
                if not closed_trade and _gt_err_last:
                    logger.info("get_trade(%s) retried 3x (%s) — falling back to watcher state",
                                trade_id, _gt_err_last)

                # Reconstruct only when OANDA genuinely can't give us the close data.
                # Prefer last market price from buffer over entry_price (entry_price → 0 pips bug).
                if not closed_trade:
                    _unrealized = 0.0
                    _last_price = 0.0
                    if watcher.last_threat:
                        _unrealized = float(watcher.last_threat.get('unrealized_pl', 0) or 0)
                        _last_price = float(watcher.last_threat.get('current_price', 0) or 0)
                    # Fallback chain for close price: last_threat → m1_buffer last close → None
                    if not _last_price and getattr(watcher, '_m1_buffer', None):
                        try:
                            _last_price = float(watcher._m1_buffer[-1].get('close', 0) or 0)
                        except Exception:
                            pass
                    if _last_price:
                        closed_trade = {
                            'realizedPL': str(_unrealized),
                            'averageClosePrice': str(_last_price),
                            'openTime': watcher.entry_time.isoformat() if hasattr(watcher.entry_time, 'isoformat') else '',
                            'closeTime': datetime.now(timezone.utc).isoformat(),
                            '_reconstructed': True,
                        }
                        logger.warning("Trade %s: OANDA data unavailable at close moment — "
                                       "reconstructed from watcher state (close_price=%.5f, "
                                       "unrealized=%.2f). Will need backfill from OANDA once settled.",
                                       trade_id, _last_price, _unrealized)
                    else:
                        # No recoverable close price — flag for explicit backfill instead of writing 0.
                        logger.error("Trade %s: cannot reconstruct close (no OANDA data, no market price "
                                     "in watcher). Marking for backfill.", trade_id)
                        closed_trade = {
                            'realizedPL': str(_unrealized),
                            'averageClosePrice': '',
                            'openTime': watcher.entry_time.isoformat() if hasattr(watcher.entry_time, 'isoformat') else '',
                            'closeTime': datetime.now(timezone.utc).isoformat(),
                            '_reconstructed': True,
                            '_needs_backfill': True,
                        }

                if closed_trade:
                    realized_pl = float(closed_trade.get('realizedPL', 0))
                    close_price = float(closed_trade.get('averageClosePrice', closed_trade.get('price', 0)))
                    open_time = closed_trade.get('openTime', '')
                    close_time = closed_trade.get('closeTime', '')

                    # Calculate pips and duration - ensure pnl_pips is always properly defined
                    pnl_pips = 0.0
                    if watcher.pip_size > 0 and close_price > 0:
                        if watcher.direction == 'buy':
                            pnl_pips = (close_price - watcher.entry_price) / watcher.pip_size
                        else:
                            pnl_pips = (watcher.entry_price - close_price) / watcher.pip_size
                    else:
                        # Fallback calculation for edge cases where pip_size is invalid or close_price is 0
                        if close_price > 0:
                            price_diff = close_price - watcher.entry_price if watcher.direction == 'buy' else watcher.entry_price - close_price
                            # Use default pip size based on currency pair
                            default_pip_size = 0.01 if 'JPY' in watcher.instrument else 0.0001
                            pnl_pips = price_diff / default_pip_size
                        else:
                            pnl_pips = 0.0

                    duration = watcher._candles_in_trade  # minutes (M1 candles)
                    r_mult = 0
                    risk_pips = abs(watcher.entry_price - watcher.stop_loss) / watcher.pip_size
                    if risk_pips > 0:
                        r_mult = pnl_pips / risk_pips

                    # Get setup name from live_trades (the entry-time classification scout/snipe wrote).
                    # 2026-05-10: switched from trade_decisions which has 100% empty setup column —
                    # see .planning/v1.2-audit/LOOP-BREAK-FINDINGS.md. The wrong table lookup forced
                    # the exit-bar reclassify fallback to run on every close, mis-attributing every
                    # winning S16/V4/C trade to S15/S5/S1 bleeders and starving auto-promote.
                    setup_name = 'unknown'
                    threat_zone = watcher.last_threat.get('zone', '') if watcher.last_threat else ''
                    close_reason_str = ''
                    story_kwargs = {}  # Story fields for revenue tracker → win snipe
                    try:
                        with get_db() as _conn:
                            row = _conn.execute(
                                "SELECT setup, market_story FROM live_trades WHERE id = ?",
                                (trade_id,)
                            ).fetchone()
                            if row:
                                setup_name = row[0] or 'unknown'
                                if row[1]:
                                    try:
                                        story_kwargs = json.loads(row[1])
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                    except Exception as e:
                        logger.warning("[GUARDIAN] live_trades setup lookup failed for %s: %s", trade_id, e)

                    # 2026-05-10: removed exit-bar reclassify fallback. It was running on every
                    # close (because trade_decisions.setup was always empty) and writing the
                    # exit-bar's S15/S5/S1 classification into setup_trades — mis-attributing
                    # every winning S16/V4/C trade. After switching to live_trades.setup above,
                    # the fallback would only fire for genuinely unclassified entries (rare); but
                    # exit-bar classification is structurally wrong (entry context != exit context),
                    # so leaving setup_name='unknown' is more honest than re-tagging.
                    # See .planning/v1.2-audit/LOOP-BREAK-FINDINGS.md.
                    if setup_name == 'unknown':
                        logger.warning(
                            "[GUARDIAN] Trade %s closing with setup='unknown' — live_trades.setup was empty. "
                            "Recording as 'unknown' rather than reclassifying on exit bar.",
                            trade_id,
                        )

                    # Record in setup revenue tracker
                    try:
                        from setup_revenue import SetupRevenueTracker
                        tracker = SetupRevenueTracker()
                        _thesis_watch_id = (watcher.trade_thesis or {}).get('watch_id')
                        result = tracker.record_trade(
                            trade_id=trade_id,
                            setup_name=setup_name,
                            pair=watcher.instrument,
                            direction=watcher.direction,
                            pnl_pips=pnl_pips,
                            pnl_usd=realized_pl,
                            entry_price=watcher.entry_price,
                            exit_price=close_price,
                            stop_loss=watcher.stop_loss,
                            take_profit=watcher.take_profit,
                            units=watcher.units,
                            r_multiple=r_mult,
                            duration_minutes=duration,
                            source='scout',
                            threat_zone_at_close=threat_zone,
                            opened_at=open_time,
                            user_id=self._user_id,
                            watch_id=_thesis_watch_id,
                            **story_kwargs,
                        )
                        logger.info(
                            "Trade %s closed: %s %s %.1f pips ($%.2f) - %s. Lifetime: %d trades, $%.2f%s",
                            trade_id, watcher.instrument, result['outcome'],
                            pnl_pips, realized_pl, setup_name,
                            result['total_trades'], result['total_usd'],
                            ' 🎯 PROMOTED!' if result.get('promotion_action') == 'promoted' else '',
                        )

                        # Flight: trade closed
                        if flight:
                            flight.record(FlightStage.TRADE_CLOSE, pair=watcher.instrument,
                                          trade_id=trade_id, data={
                                "outcome": result['outcome'],
                                "pnl_pips": pnl_pips,
                                "pnl_usd": realized_pl,
                                "r_multiple": r_mult,
                                "duration_min": duration,
                                "setup": setup_name,
                                "threat_zone": threat_zone,
                            }, note=f"{result['outcome']} {pnl_pips:+.1f} pips (${realized_pl:+.2f})")

                        # 2026-04-29: close the live_trades row directly. setup_revenue
                        # tracker only writes setup_trades + setup_revenue tables; without
                        # this UPDATE the live_trades row stays at status='open' forever
                        # and the daily P&L aggregator never sees this win/loss.
                        # Populates ALL redundant P&L columns (pips/pnl_pips, pnl_usd/realized_pl,
                        # result/outcome, outcome_pips/outcome_usd) — different consumers read
                        # different subsets, mirrors backfill_oanda_trades.py:177-202.
                        try:
                            _lt_conn = get_trading_forex()
                            _close_iso = close_time or datetime.now(timezone.utc).isoformat()
                            _outcome_str = result.get('outcome') if isinstance(result, dict) else ('win' if pnl_pips > 0 else 'loss')
                            # Round at write — pip computation produces float precision
                            # artifacts (2.29999999999952 instead of 2.3) which surface
                            # raw on the dashboard.
                            _pp = round(float(pnl_pips), 1)
                            _pl = round(float(realized_pl), 2)
                            _lt_conn.execute("""
                                UPDATE live_trades
                                SET status = 'closed',
                                    exit_price = ?,
                                    exit_time = ?,
                                    pnl_pips = ?,
                                    pips = ?,
                                    pnl_usd = ?,
                                    realized_pl = ?,
                                    result = ?,
                                    outcome = ?,
                                    outcome_pips = ?,
                                    outcome_usd = ?
                                WHERE id = ? OR oanda_trade_id = ?
                            """, (close_price, _close_iso, _pp, _pp,
                                  _pl, _pl,
                                  _outcome_str, _outcome_str, _pp, _pl,
                                  str(trade_id), str(trade_id)))
                            _lt_conn.commit()
                            logger.info(
                                "[GUARDIAN] live_trades closed: id=%s outcome=%s pnl=%+.1fp / $%+.2f",
                                trade_id, _outcome_str, _pp, _pl
                            )
                        except Exception as _lt_err:
                            logger.warning(
                                "[GUARDIAN] live_trades UPDATE failed for %s: %s",
                                trade_id, _lt_err
                            )
                    except Exception as e:
                        logger.warning("Revenue tracking failed for %s: %s", trade_id, e)

                    # RE-ENTRY: Stamp the watch with close time so the watch timer
                    # enforces a 15-min post-trade cooldown before re-firing.
                    # (reentry_signals.json removed — watch system is the single re-entry path)
                    try:
                        import json as _wjson2
                        _wc2 = get_trading_forex()
                        try:
                            _wrow2 = _wc2.execute(
                                "SELECT id, context FROM watch_suggestions "
                                "WHERE json_extract(context, '$._snipe_fill_trade_id')=? "
                                "AND status IN ('triggered','watching') LIMIT 1",
                                (str(trade_id),)
                            ).fetchone()
                            if _wrow2:
                                _wid2, _wctx_raw2 = _wrow2
                                _wctx2 = {}
                                try: _wctx2 = _wjson2.loads(_wctx_raw2 or '{}')
                                except Exception as e: logger.warning("[GUARDIAN] Failed to parse watch context: %s", e)
                                _wctx2["_last_fill_close_time"] = time.time()
                                _wctx2["_last_fill_outcome"] = "win" if pnl_pips > 0 else "loss"
                                # FIX 2026-03-27: Clear trade_cycle_id so watch can re-trigger
                                _wc2.execute(
                                    "UPDATE watch_suggestions SET status='watching', "
                                    "trade_cycle_id=NULL, triggered_at=NULL, context=? WHERE id=?",
                                    (_wjson2.dumps(_wctx2), _wid2)
                                )
                                _wc2.commit()
                                logger.info("[GUARDIAN] Watch #%s: stamped close time for %s (outcome=%s, cycle_id cleared)",
                                           _wid2, watcher.instrument, _wctx2["_last_fill_outcome"])
                                # ── Origin-tracking: upsert snipe_leaderboard (2026-04-22) ──
                                # Enables per-watch performance tracking. Previously 0 rows.
                                try:
                                    from Source.agents.watch_manager import _upsert_leaderboard as _upsert_lb
                                    _wrow_full = _wc2.execute(
                                        "SELECT conditions, suggestion_type, direction FROM watch_suggestions WHERE id=?",
                                        (_wid2,)
                                    ).fetchone()
                                    if _wrow_full:
                                        _conds_raw, _stype, _wdir = _wrow_full
                                        _conds = _wjson2.loads(_conds_raw or "[]")
                                        _direction_for_lb = (
                                            _wdir
                                            or _wctx2.get("re_entry_direction")
                                            or _wctx2.get("direction")
                                            or "unknown"
                                        )
                                        _upsert_lb(_wc2, _wid2, watcher.instrument, _conds,
                                                   _stype or "unknown", _direction_for_lb,
                                                   _wctx2["_last_fill_outcome"], pnl_pips)
                                except Exception as _lb_err:
                                    logger.debug("[GUARDIAN] leaderboard upsert failed for watch #%s: %s",
                                                 _wid2, _lb_err)
                                # ── Stamp module-level pair cooldown (shared with _fire_snipe_cycle) ──
                                try:
                                    import trading_api_routes as _tar2
                                    _tar2.pair_last_close[(self._user_id, watcher.instrument)] = time.time()
                                    logger.info("[GUARDIAN] Pair cooldown stamped for %s user_id=%s (30 min)", watcher.instrument, self._user_id)
                                except Exception as _tare2:
                                    logger.debug("Could not stamp pair cooldown: %s", _tare2)
                        finally:
                            _wc2.close()
                    except Exception as _re:
                        logger.debug("Watch close-stamp failed: %s", _re)

                    # ── Fallback watch reset: also match by trade_cycle_id ────
                    # The context-based lookup above may miss watches where context
                    # was never stamped with _snipe_fill_trade_id (e.g. before that
                    # field was added, or on OANDA 404 paths). Reset any triggered
                    # watch whose trade_cycle_id matches the closing trade.
                    try:
                        import json as _wjson3
                        _wc3 = get_trading_forex()
                        try:
                            _rows3 = _wc3.execute(
                                "SELECT id, context FROM watch_suggestions "
                                "WHERE trade_cycle_id=? AND status='triggered'",
                                (str(trade_id),)
                            ).fetchall()
                            for _wid3, _wctx_raw3 in _rows3:
                                _wctx3 = {}
                                try: _wctx3 = _wjson3.loads(_wctx_raw3 or '{}')
                                except Exception as e: logger.warning("[GUARDIAN] Failed to parse watch context: %s", e)
                                _wctx3["_last_fill_close_time"] = time.time()
                                _wctx3["_last_fill_outcome"] = "win" if pnl_pips > 0 else "loss"
                                # FIX 2026-03-27: Also clear trade_cycle_id on close.
                                # Without this, the watch re-enters check_active_watches
                                # (status='watching' is always included) but if the re-triggered
                                # cycle gets gate-blocked, the stale cycle_id causes a permanent
                                # dead state where status='triggered' + cycle_id != NULL.
                                _wc3.execute(
                                    "UPDATE watch_suggestions SET status='watching', "
                                    "trade_cycle_id=NULL, triggered_at=NULL, context=? WHERE id=?",
                                    (_wjson3.dumps(_wctx3), _wid3)
                                )
                                logger.info("[GUARDIAN] Watch #%s: fallback reset to watching (trade_cycle_id cleared, outcome=%s)",
                                           _wid3, _wctx3["_last_fill_outcome"])
                                # ── Stamp module-level pair cooldown ──
                                try:
                                    import trading_api_routes as _tar3
                                    _tar3.pair_last_close[(self._user_id, watcher.instrument)] = time.time()
                                except Exception:
                                    pass
                            _wc3.commit()
                        finally:
                            _wc3.close()
                    except Exception as _re3:
                        logger.debug("Watch fallback-reset failed: %s", _re3)

                    # PROBLEM 1 FIX: Backfill scout_findings with trade outcome
                    try:
                        outcome = 'win' if pnl_pips > 0 else 'loss'
                        exit_reason = 'tp' if (watcher.direction == 'buy' and close_price >= watcher.take_profit) or \
                                             (watcher.direction == 'sell' and close_price <= watcher.take_profit) else \
                                     'sl' if (watcher.direction == 'buy' and close_price <= watcher.stop_loss) or \
                                             (watcher.direction == 'sell' and close_price >= watcher.stop_loss) else \
                                     f'guardian_{threat_zone}' if threat_zone else 'broker'
                        
                        # Calculate hold time in hours
                        hold_hours = duration / 60.0 if duration > 0 else 0
                        resolution_time = datetime.now(timezone.utc).isoformat()
                        
                        # Backfill scout_findings with outcome
                        with get_db() as conn:
                            rows_updated = conn.execute("""
                                UPDATE scout_findings 
                                SET outcome=?, pips_result=?, hold_time_hours=?, exit_reason=?, 
                                    resolution_timestamp=?, updated_at=CURRENT_TIMESTAMP
                                WHERE trade_id=? AND outcome IS NULL
                            """, (outcome, pnl_pips, hold_hours, exit_reason, resolution_time, trade_id)).rowcount
                            
                        if rows_updated > 0:
                            logger.info("🎯 Backfilled %d scout_findings for trade %s: %s %.1f pips in %.1fh (%s)",
                                       rows_updated, trade_id, outcome, pnl_pips, hold_hours, exit_reason)
                    except Exception as e:
                        logger.warning("Scout findings backfill failed for %s: %s", trade_id, e)

                    # Capture exit for manual trades (SL/TP hit, guardian close, etc.)
                    try:
                        from manual_trade_store import ManualTradeStore
                        _mt_store = ManualTradeStore()
                        _mt = _mt_store.get_trade_by_oanda_id(str(trade_id))
                        if _mt and _mt.get('result') is None:
                            # Determine exit reason
                            _exit_reason = 'unknown'
                            if threat_zone == 'BLACK':
                                _exit_reason = 'guardian_emergency'
                            elif watcher.take_profit and close_price:
                                tp_hit = (watcher.direction == 'buy' and close_price >= watcher.take_profit) or \
                                         (watcher.direction == 'sell' and close_price <= watcher.take_profit)
                                sl_hit = (watcher.direction == 'buy' and close_price <= watcher.stop_loss) or \
                                         (watcher.direction == 'sell' and close_price >= watcher.stop_loss)
                                if tp_hit:
                                    _exit_reason = 'tp'
                                elif sl_hit:
                                    _exit_reason = 'sl'
                                else:
                                    _exit_reason = 'guardian' if threat_zone else 'broker_close'
                            _mt_store.record_exit(
                                trade_id=str(trade_id),
                                exit_price=close_price or 0,
                                realized_pl=realized_pl,
                                exit_reason=_exit_reason,
                                hold_bars=duration,  # M1 candle count
                                user_id=self._user_id,
                            )
                            logger.info("📝 Manual trade %s exit captured via guardian: %s $%.2f",
                                       trade_id, _exit_reason, realized_pl)
                            # Update live_trades so status reflects closure
                            # 2026-04-14: If close was reconstructed without a valid close_price,
                            # tag exit_trigger='needs_backfill' so a scheduled repair can fix it
                            # from OANDA's transaction API later. DO NOT silently write 0 pips.
                            try:
                                import sqlite3 as _lt_sq
                                _lt_db2 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                       "Database", "v2", "trading_forex.db")
                                _needs_backfill = bool(closed_trade.get('_needs_backfill'))
                                _was_reconstructed = bool(closed_trade.get('_reconstructed'))
                                _exit_trigger_value = 'needs_backfill' if _needs_backfill else \
                                                      (f"{_exit_reason}_reconstructed" if _was_reconstructed else _exit_reason)
                                with _lt_sq.connect(_lt_db2, timeout=10) as _lt_c2:
                                    _lt_c2.execute("PRAGMA journal_mode=DELETE")
                                    _outcome_lt = 'win' if pnl_pips > 0 else ('loss' if pnl_pips < 0 or realized_pl < 0 else 'unknown')
                                    # 2026-04-14: write BOTH column pairs — dashboard reads pips/realized_pl
                                    # while audit path reads pnl_pips/pnl_usd. Keeping them in sync avoids the
                                    # "UI shows 0p" bug even when the row has correct pnl_pips.
                                    _lt_c2.execute("""
                                        UPDATE live_trades SET
                                            status='closed', exit_time=?, exit_price=?,
                                            pnl_pips=?, pnl_usd=?, pips=?, realized_pl=?,
                                            outcome=?, result=?, exit_trigger=?
                                        WHERE id=?
                                    """, (
                                        datetime.now(timezone.utc).isoformat(),
                                        close_price if close_price > 0 else None,
                                        round(pnl_pips, 1) if close_price > 0 else None,
                                        round(realized_pl, 4) if realized_pl else None,
                                        round(pnl_pips, 1) if close_price > 0 else None,
                                        round(realized_pl, 4) if realized_pl else None,
                                        _outcome_lt,
                                        _outcome_lt,
                                        _exit_trigger_value,
                                        str(trade_id),
                                    ))
                            except Exception as _lt_e2:
                                # 2026-04-24: upgraded — manual-close DB write failure
                                # means dashboard shows 0p for manual exits.
                                logger.warning("live_trades manual-close update FAILED for %s: %s: %s (dashboard will show 0p)",
                                               trade_id, type(_lt_e2).__name__, _lt_e2)
                    except Exception as _mte:
                        # 2026-04-24: upgraded — outer manual-trade exit block.
                        # Silent = manual exits go unrecorded.
                        logger.warning("Manual trade exit capture FAILED: %s: %s",
                                       type(_mte).__name__, _mte)

                    # Also update trade_decisions with outcome + get cycle context for audit
                    _decision_row = None
                    try:
                        with get_db() as _conn:
                            # PROBLEM 2 FIX: Try multiple formats for trade_id matching
                            outcome = 'win' if pnl_pips > 0 else 'loss'
                            pips_rounded = round(pnl_pips, 1)
                            
                            # Try exact match first
                            rows_updated = _conn.execute(
                                "UPDATE trade_decisions SET outcome = ?, outcome_pips = ? WHERE live_trade_id = ?",
                                (outcome, pips_rounded, str(trade_id))
                            ).rowcount
                            
                            # If no match, try as integer (for numeric trade_ids)
                            if rows_updated == 0 and str(trade_id).isdigit():
                                rows_updated = _conn.execute(
                                    "UPDATE trade_decisions SET outcome = ?, outcome_pips = ? WHERE live_trade_id = ?",
                                    (outcome, pips_rounded, int(trade_id))
                                ).rowcount
                                
                            # If still no match, try string conversion the other way
                            if rows_updated == 0:
                                try:
                                    _conn.execute(
                                        "UPDATE trade_decisions SET outcome = ?, outcome_pips = ? WHERE CAST(live_trade_id AS TEXT) = ?",
                                        (outcome, pips_rounded, str(trade_id))
                                    )
                                except Exception as e:
                                    logger.warning("[GUARDIAN] trade_decisions cast-update failed for %s: %s", trade_id, e)
                            
                            _decision_row = _conn.execute(
                                "SELECT decision_id, validator_verdict, market_story_snapshot "
                                "FROM trade_decisions WHERE live_trade_id = ? OR CAST(live_trade_id AS TEXT) = ? ORDER BY created_at DESC LIMIT 1",
                                (str(trade_id), str(trade_id))
                            ).fetchone()
                    except Exception as e:
                        # 2026-04-24: upgraded — trade_decisions outcome write failure.
                        # Silent = validator audit/learning sees no outcome for this trade.
                        logger.warning(f"Trade decision outcome update FAILED: {type(e).__name__}: {e} (validator learning DB drift)")

                    # Post-trade audit (async, non-blocking)
                    try:
                        from trade_auditor import audit_trade_async
                        _entry_type = story_kwargs.get('entry_type', '')
                        _validator_v = _decision_row['validator_verdict'] if _decision_row else ''
                        # Use cycle_id from flight recorder if available
                        _audit_cycle_id = ""
                        if flight:
                            try:
                                with flight._conn() as _fc:
                                    _cid_row = _fc.execute(
                                        "SELECT cycle_id FROM flight_log WHERE trade_id = ? AND cycle_id != '' LIMIT 1",
                                        (trade_id,)
                                    ).fetchone()
                                    if _cid_row:
                                        _audit_cycle_id = _cid_row["cycle_id"]
                            except Exception as e:
                                logger.info("[GUARDIAN] Flight cycle_id lookup failed for %s: %s", trade_id, e)

                        # ── Stamp training outcome on validator pair ──────────────
                        # The 35B learns from every trade result, not just verdicts.
                        # CONFIRM→LOSS pairs are flagged as negative examples.
                        # Lookup chain: trade_id → watch.cycle_id → training pair
                        # Falls back to _audit_cycle_id if watch lookup fails.
                        try:
                            from Source.validator_training_extractor import stamp_outcome
                            _stamp_cycle_id = _audit_cycle_id
                            # Primary: find cycle_id via watch (most reliable for snipe trades)
                            if not _stamp_cycle_id:
                                try:
                                    import sqlite3 as _wdb_s
                                    with _wdb_s.connect(_TRADING_FOREX_DB, timeout=3) as _wc_s:
                                        _wr_s = _wc_s.execute(
                                            "SELECT cycle_id FROM watch_suggestions WHERE trade_cycle_id=? LIMIT 1",
                                            (str(trade_id),)
                                        ).fetchone()
                                        if _wr_s and _wr_s[0]:
                                            _stamp_cycle_id = _wr_s[0]
                                except Exception:
                                    pass
                            if _stamp_cycle_id:
                                stamp_outcome(
                                    cycle_id=_stamp_cycle_id,
                                    outcome='win' if pnl_pips > 0 else 'loss',
                                    pnl_pips=pnl_pips,
                                )
                                logger.debug("[TRAINING] Stamped %s outcome on cycle %s (trade %s)",
                                            'win' if pnl_pips > 0 else 'loss', _stamp_cycle_id, trade_id)
                        except Exception as _ste:
                            # 2026-04-24: upgraded — training-data outcome stamp failure.
                            # Silent = training pairs remain unlabeled, distillation quality degrades.
                            logger.warning("[TRAINING] stamp_outcome FAILED: %s: %s (training pair unlabeled)",
                                           type(_ste).__name__, _ste)

                        asyncio.create_task(audit_trade_async(
                            cycle_id=_audit_cycle_id,
                            trade_id=trade_id,
                            pair=watcher.instrument,
                            direction=watcher.direction,
                            entry_price=watcher.entry_price,
                            exit_price=close_price,
                            stop_loss=watcher.stop_loss,
                            take_profit=watcher.take_profit,
                            pnl_pips=pnl_pips,
                            pnl_usd=realized_pl,
                            setup_name=setup_name,
                            entry_type=_entry_type,
                            entry_time=open_time,
                            close_time=close_time,
                            outcome='win' if pnl_pips > 0 else 'loss',
                            validator_verdict=_validator_v,
                        ))
                        logger.info("Post-trade audit scheduled for %s", trade_id)
                    except Exception as e:
                        logger.warning("Audit skipped for %s: %s", trade_id, e)

                    # Record exit learning data for guardian learning
                    self._record_exit_learning(
                        trade_id=trade_id,
                        watcher=watcher,
                        closed_trade=closed_trade,
                        setup_name=setup_name,
                        pnl_pips=pnl_pips,
                        realized_pl=realized_pl,
                        r_mult=r_mult,
                        duration=duration,
                        threat_zone=threat_zone,
                        user_id=self._user_id
                    )

                    # Write trade outcome to knowledge vault
                    try:
                        import sys as _sys
                        _vault_path = os.path.join(os.path.dirname(__file__), '..', '..', '..')
                        _sys.path.insert(0, _vault_path)
                        from knowledge.vault_writer import VaultWriter
                        _vw = VaultWriter()
                        _outcome = "win" if pnl_pips > 0 else "loss"
                        _emoji = "💰" if pnl_pips > 0 else "❌"
                        _pair = watcher.instrument if hasattr(watcher, 'instrument') else pair if 'pair' in dir() else "?"
                        _summary = f"{_emoji} {_pair} {watcher.direction.upper() if hasattr(watcher,'direction') else '?'} {_outcome}: {pnl_pips:+.1f}p / ${realized_pl:+.2f} ({duration}min)"
                        _context = (
                            f"**Pair:** {_pair} | **Direction:** {getattr(watcher,'direction','?').upper()}\n"
                            f"**PnL:** {pnl_pips:+.1f} pips / ${realized_pl:+.2f} | **R:** {r_mult:.2f}\n"
                            f"**Duration:** {duration} min | **Close reason:** {threat_zone}\n"
                            f"**Peak pips:** {getattr(watcher,'_peak_pnl_pips',0):.1f}\n"
                            f"**Setup:** {setup_name}"
                        )
                        _vw.record_agent_learning("guardian", {
                            "type": "discovery" if _outcome == "win" else "failure",
                            "summary": _summary,
                            "context": _context,
                            "tags": [_outcome, _pair.lower().replace("_",""), "guardian", setup_name or "unknown"],
                        })

                        # Write to v2/agents.db agent_performance for team tracking
                        try:
                            import sqlite3 as _sq2, uuid as _uuid2
                            from datetime import datetime as _dt2
                            _reg_db = os.path.normpath(os.path.join(
                                os.path.dirname(os.path.abspath(__file__)),
                                '..', '..', '..', 'Database', 'v2', 'agents.db'
                            ))
                            _conn2 = _sq2.connect(_reg_db)
                            _cur2  = _conn2.cursor()
                            _cur2.execute("SELECT id FROM agent_registry WHERE agent_name='guardian'")
                            _greg = _cur2.fetchone()
                            if not _greg:
                                # guardian not in registry yet — insert it
                                import uuid as _u3
                                _greg_id = str(_u3.uuid4())
                                _cur2.execute("""INSERT INTO agent_registry (id, agent_name, agent_type, capabilities, status, created_at, updated_at, vault_path)
                                    VALUES (?,?,?,?,?,?,?,?)""",
                                    (_greg_id, 'guardian', 'monitoring', '["position_monitoring","exit_management"]',
                                     'active', _dt2.now().isoformat(), _dt2.now().isoformat(),
                                     'agents/guardian/prompt.md'))
                            else:
                                _greg_id = _greg[0]
                            _cur2.execute("""
                                INSERT INTO agent_performance
                                    (id, agent_id, workspace_id, task_id, success,
                                     completion_time_ms, quality_score, error_count, timestamp, metadata)
                                VALUES (?,?,?,?,?,?,?,?,?,?)
                            """, (
                                str(_uuid2.uuid4()), _greg_id, "forex-v4-prod",
                                str(trade_id),
                                1 if pnl_pips > 0 else 0,
                                duration * 60000,   # duration is minutes
                                min(1.0, max(0.0, (r_mult + 1) / 3.0)),  # R-mult → quality 0-1
                                0,
                                _dt2.now().isoformat(timespec='seconds'),
                                _sq2.Binary(__import__('json').dumps({
                                    "pair": _pair, "direction": watcher.direction,
                                    "pnl_pips": round(pnl_pips, 1),
                                    "r_multiple": round(r_mult, 2),
                                    "duration_min": duration,
                                    "close_reason": threat_zone,
                                    "setup": setup_name,
                                }).encode()),
                            ))
                            _conn2.commit()
                            _conn2.close()
                        except Exception as _re:
                            logger.debug("Registry perf write for guardian failed (non-critical): %s", _re)

                    except Exception as _ve:
                        logger.debug("Vault write for trade close failed (non-critical): %s", _ve)

                    # ── V4: Label chart image with trade outcome for training ──
                    try:
                        import shutil
                        _v4_train_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                      "Data", "charts", "training")
                        _v4_labeled_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                        "Data", "charts", "labeled")
                        os.makedirs(_v4_labeled_dir, exist_ok=True)

                        # Find the entry chart for this trade (search training dir for pair + recent timestamp)
                        _outcome = "WIN" if pnl_pips > 0 else "LOSS"
                        _pips_str = f"{pnl_pips:+.0f}p"

                        # Look for charts matching this pair in training dir
                        if os.path.isdir(_v4_train_dir):
                            for _cf in sorted(os.listdir(_v4_train_dir), reverse=True):
                                if _cf.startswith(watcher.pair) and _cf.endswith('.png'):
                                    _src = os.path.join(_v4_train_dir, _cf)
                                    _dst_name = f"{watcher.pair}_{watcher.direction}_{_outcome}_{_pips_str}_{_cf.split('_')[-1]}"
                                    _dst = os.path.join(_v4_labeled_dir, _dst_name)
                                    shutil.copy2(_src, _dst)
                                    logger.info("[V4] Chart labeled: %s → %s", _cf, _dst_name)

                                    # Update vision_training_data DB with outcome
                                    try:
                                        _v4c = get_trading_forex()
                                        _v4c.execute("""
                                            UPDATE vision_training_data
                                            SET output_response = json_set(output_response, '$.outcome', ?, '$.pnl_pips', ?)
                                            WHERE chart_path = ?
                                        """, (_outcome, pnl_pips, _src))
                                        _v4c.commit()
                                    except Exception as _db_err:
                                        logger.debug("[V4] DB outcome update: %s", _db_err)
                                    break  # Only label the most recent chart for this pair
                    except Exception as _v4_label_err:
                        logger.debug("[V4] Chart labeling: %s", _v4_label_err)

                    # ── Update live_trades with exit data so learning pipeline has real results ──
                    # CRITICAL: Must set ALL columns the dashboard reads:
                    #   result, pips, realized_pl → session P&L and win rate
                    #   status → 'closed' so it's counted in completed trades
                    #   pnl_pips, pnl_usd, outcome, outcome_pips, outcome_usd → trade detail view
                    # 2026-04-22: Hardened close UPDATE against silent failures that
                    # produced zombie trades (status='open' in DB despite close on OANDA).
                    # Root cause: sqlite3.OperationalError on DB lock contention was caught
                    # and logged once, never retried. If other writer held the lock, UPDATE
                    # dropped and the trade stayed 'open' forever — blocking future fires
                    # on that pair via dedup gate.
                    # Fix: 3-attempt retry with exponential backoff. On all retries failing,
                    # emit LIVE_TRADES_UPDATE_FAILED to flight_log so zombies are surfaced
                    # immediately instead of silently accumulating.
                    _lt_update_ok = False
                    _lt_last_err = None
                    _lt_attempts = 0
                    for _lt_attempt in range(3):
                        _lt_attempts = _lt_attempt + 1
                        try:
                            _lt_conn = get_trading_forex()
                            # Gather MFE/MAE from watcher memory
                            _mfe = round(getattr(watcher, '_peak_pnl_pips', 0.0), 1)
                            _mae = round(getattr(watcher, '_max_adverse_pips', abs(min(pnl_pips, 0.0))), 1)
                            _pnl_rounded = round(pnl_pips, 1)
                            _outcome_str = 'win' if pnl_pips > 0 else 'loss'
                            _realized = round(realized_pl, 2) if realized_pl else 0.0
                            _lt_conn.execute("""
                                UPDATE live_trades SET
                                    exit_time = ?,
                                    exit_price = ?,
                                    pips = ?,
                                    pnl_pips = ?,
                                    pnl_usd = ?,
                                    result = ?,
                                    outcome = ?,
                                    outcome_pips = ?,
                                    outcome_usd = ?,
                                    realized_pl = ?,
                                    status = 'closed',
                                    exit_trigger = ?,
                                    exit_method = 'guardian',
                                    max_favorable_pips = ?,
                                    max_favorable_excursion_pips = ?,
                                    max_adverse_pips = ?,
                                    max_adverse_excursion_pips = ?,
                                    updated_at = ?
                                WHERE oanda_trade_id = ?
                            """, (
                                datetime.now(timezone.utc).isoformat(),
                                close_price,
                                _pnl_rounded,
                                _pnl_rounded,
                                _realized,
                                _outcome_str,
                                _outcome_str,
                                _pnl_rounded,
                                _realized,
                                _realized,
                                # 2026-04-23: removed exit_reason = ? — column doesn't exist
                                # in live_trades schema (confirmed via PRAGMA table_info).
                                # exit_trigger below captures the same info. Prior behavior
                                # was all UPDATEs failing → ZOMBIE RISK errors for every close →
                                # trades staying 'open' in DB after OANDA closed them.
                                threat_zone or 'guardian',
                                _mfe,
                                _mfe,
                                _mae,
                                _mae,
                                datetime.now(timezone.utc).isoformat(),
                                str(trade_id),
                            ))
                            _rows_updated = _lt_conn.execute("SELECT changes()").fetchone()[0]
                            _lt_conn.commit()
                            _lt_update_ok = True
                            if _rows_updated > 0:
                                if _lt_attempts > 1:
                                    logger.info("[LT] Updated live_trades exit for %s: %+.1fp $%.2f %s "
                                                "(retry %d/3)", trade_id, pnl_pips, _realized,
                                                _outcome_str.upper(), _lt_attempts)
                                else:
                                    logger.info("[LT] Updated live_trades exit for %s: %+.1fp $%.2f %s",
                                                trade_id, pnl_pips, _realized, _outcome_str.upper())
                            else:
                                logger.warning("[LT] live_trades UPDATE matched 0 rows for oanda_trade_id=%s "
                                               "— trade was never inserted (snipe direct path may not have "
                                               "created the row)", trade_id)
                            break
                        except Exception as _lt_err:
                            _lt_last_err = _lt_err
                            if _lt_attempt < 2:
                                import time as _lt_time
                                _lt_time.sleep(0.5 * (2 ** _lt_attempt))  # 0.5s, 1s backoff
                                logger.debug("[LT] UPDATE retry %d/3 for %s after %s",
                                             _lt_attempt + 1, trade_id, _lt_err)
                    if not _lt_update_ok:
                        # All retries failed — emit flight_log event so zombie is surfaced
                        logger.error("[LT] ZOMBIE RISK — live_trades UPDATE failed %d/3 for %s: %s. "
                                     "Trade closed on OANDA but DB row still 'open'. Reconciler required.",
                                     _lt_attempts, trade_id, _lt_last_err)
                        if flight:
                            try:
                                flight.record(FlightStage.GUARDIAN_ACTION, pair=self.instrument,
                                              trade_id=str(trade_id), data={
                                    "action": "live_trades_update_failed_ZOMBIE_RISK",
                                    "attempts": _lt_attempts,
                                    "error": str(_lt_last_err),
                                    "pnl_pips": round(pnl_pips, 1),
                                    "realized_pl": round(realized_pl, 2) if realized_pl else 0.0,
                                    "outcome": 'win' if pnl_pips > 0 else 'loss',
                                }, note=f"ZOMBIE RISK: live_trades UPDATE failed for {trade_id} — run reconcile_kronos_zombies.py")
                            except Exception:
                                pass

                    # ── Reset watch_suggestions so snipes re-arm after trade closes ────
                    # Snipes are multi-use: reset to 'watching' so they can fire again
                    # if conditions realign. Matches both direct OANDA trade_id (from
                    # trading_cycle.py snipe direct) AND manual_<trade_id> (from manual
                    # trade linking in trading_api_routes.py).
                    # Also clears ALL progress fields so the snipe starts fresh —
                    # Reset trade link on close — but PRESERVE condition progress.
                    # The conditions reflect current market state, not trade state.
                    # criteria_hit_rate/scan_count took hundreds of scans to build — don't nuke them.
                    # The watch checker will naturally re-evaluate on the next scan.
                    try:
                        _bm_conn = get_trading_forex()
                        try:
                            _outcome = 'win' if pnl_pips > 0 else 'loss'
                            _now_iso = datetime.now(timezone.utc).isoformat()
                            _tid_str = str(trade_id)
                            # Match both 'trade_id' and 'manual_trade_id' patterns
                            _bm_conn.execute("""
                                UPDATE watch_suggestions
                                SET status = 'watching',
                                    triggered_at = NULL,
                                    trade_cycle_id = NULL,
                                    trade_outcome = ?,
                                    pips_result = ?,
                                    last_checked_at = ?,
                                    stale_flagged_at = NULL
                                WHERE (trade_cycle_id = ? OR trade_cycle_id = ?)
                                AND status = 'triggered'
                            """, (_outcome, round(pnl_pips, 1), _now_iso,
                                  _tid_str, f"manual_{_tid_str}"))
                            _rows = _bm_conn.execute(
                                "SELECT changes()").fetchone()[0]
                            _bm_conn.commit()
                            if _rows:
                                logger.info("🔄 [SNIPE RESET] %s #%s: %d snipe(s) reset to watching + progress cleared (%s %+.1fp)",
                                            watcher.instrument, trade_id, _rows, _outcome.upper(), pnl_pips)
                            else:
                                logger.debug("[SNIPE RESET] No triggered watches found for trade %s (tried both direct and manual_ prefix)", trade_id)
                        finally:
                            _bm_conn.close()
                    except Exception as _ws_err:
                        logger.warning("[SNIPE RESET] Failed to reset watch_suggestions for %s: %s", trade_id, _ws_err)

                    # ── Telegram: trade closed notification ──────────────────
                    try:
                        import sys as _sys
                        _src_dir = os.path.dirname(os.path.abspath(__file__))
                        if _src_dir not in _sys.path:
                            _sys.path.insert(0, _src_dir)
                        from trade_notify import notify_trade_closed
                        _dur_min = None
                        try:
                            _dur_min = int((datetime.now(timezone.utc) -
                                datetime.fromisoformat(watcher.entry_time.replace('Z', '+00:00')
                                    if '+' not in watcher.entry_time else watcher.entry_time)
                                ).total_seconds() / 60)
                        except Exception as e:
                            logger.warning("[GUARDIAN] Duration calc failed for trade %s: %s", trade_id, e)
                        notify_trade_closed(
                            trade_id=str(trade_id),
                            pair=watcher.instrument,
                            direction=watcher.direction,
                            pnl_pips=pnl_pips,
                            pnl_usd=float(realized_pl or 0),
                            exit_reason=threat_zone or exit_reason or 'guardian',
                            exit_price=float(close_price or 0),
                            units=int(watcher.units or 0),
                            duration_min=_dur_min,
                            from_snipe=bool(getattr(watcher, 'trade_cycle_id', None)),
                        )
                    except Exception as _ntf_e:
                        logger.debug("Trade close notification failed: %s", _ntf_e)

                    # Update scout_performance_analytics after each trade close
                    try:
                        from scout_learning_system import update_scout_performance_analytics
                        update_scout_performance_analytics()
                    except Exception as _spa_err:
                        logger.debug("scout_performance_analytics update skipped: %s", _spa_err)

                    # ── Tier 1 catalog Live 30d updater (2026-04-29) ────────────
                    # If the closing trade originated from a Tier 1 detector
                    # (C1/C3/C4/C5/C8/C9/C11), refresh that setup's `Live 30d:`
                    # line in tier1_setup_catalog.md so the next validator
                    # agent registration sees fresh stats. Cheap (one DB query
                    # + one file rewrite) and only fires when the closing
                    # trade matches a Tier 1 setup. Wrapped to never affect
                    # the trade-close path itself.
                    try:
                        import sqlite3 as _t1_sqlite
                        from setup_perf_updater import update_catalog_perf, TIER1_SETUPS
                        _t1_conn = _t1_sqlite.connect(
                            "~/Jarvis/Database/v2/trading_forex.db"
                        )
                        try:
                            _t1_row = _t1_conn.execute(
                                "SELECT setup_code, setup, "
                                "json_extract(metadata, '$.alert_type') "
                                "FROM live_trades WHERE id = ?",
                                (str(trade_id),),
                            ).fetchone()
                        finally:
                            _t1_conn.close()
                        _t1_match = next(
                            (c for c in (_t1_row or ()) if c and c in TIER1_SETUPS),
                            None,
                        )
                        if _t1_match:
                            update_catalog_perf(_t1_match)
                            logger.info(
                                "[TIER1] Catalog Live 30d refreshed for %s after trade %s close",
                                _t1_match, trade_id,
                            )
                    except Exception as _t1_err:
                        logger.debug("Tier1 catalog update skipped: %s", _t1_err)

                    # Manual trade learning — analyze trades scout missed
                    if watcher.source == 'manual':
                        try:
                            from manual_trade_learner import process_closed_trade
                            _learn_result = process_closed_trade({
                                'pair': watcher.pair,
                                'direction': watcher.direction,
                                'entry_time': watcher.entry_time.isoformat() if hasattr(watcher.entry_time, 'isoformat') else str(watcher.entry_time),
                                'exit_time': datetime.now(timezone.utc).isoformat(),
                                'pips': pnl_pips,
                                'result': 'win' if pnl_pips > 0 else 'loss',
                                'entry_price': watcher.entry_price,
                            })
                            if _learn_result.get('recommendation') in ('NEW_PATTERN', 'SCOUT_GAP'):
                                logger.info(
                                    f"📚 Manual trade learning: {watcher.pair} {watcher.direction} "
                                    f"{pnl_pips:+.1f}p → {_learn_result['recommendation']} "
                                    f"pattern={_learn_result.get('pattern_id', 'N/A')}"
                                )
                        except Exception as _learn_err:
                            logger.debug(f"Manual trade learning skipped: {_learn_err}")

                    # Collect training pairs for MLX fine-tuning (wins + losses)
                    try:
                        from training_collector import collect_cycle_pairs
                        _outcome = 'win' if pnl_pips > 0 else ('loss' if pnl_pips < 0 else 'breakeven')
                        collect_cycle_pairs(trade_id, watcher.instrument, _outcome)
                    except Exception as _tc_err:
                        logger.debug("Training collect failed (non-critical): %s", _tc_err)

                    # Auto-trigger LoRA training if enough new pairs accumulated
                    try:
                        from lora_trainer import should_train, run_lora_training
                        for _mk in ("ta_9b", "trade_monitor_35b"):
                            if should_train(_mk):
                                logger.info("[CYCLE] Auto-triggering LoRA for %s", _mk)
                                run_lora_training(_mk)  # Non-blocking subprocess
                    except Exception as _le:
                        logger.debug("[CYCLE] LoRA auto-trigger check failed: %s", _le)

            except Exception as e:
                logger.warning("Failed to record closed trade %s: %s", trade_id, e)

            logger.info("Reaped watcher for trade %s", trade_id)

    async def force_close(self, trade_id: str, reason: str = "manual") -> bool:
        """Force-close a trade from external call (dashboard button, etc.)."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: self._client.close_trade(trade_id))
            logger.warning("Force-closed trade %s: %s", trade_id, reason)
            self._stats['emergency_closes'] += 1
            # Best-effort audit for force-closed trades
            try:
                asyncio.create_task(audit_trade_async(
                    cycle_id=None,
                    trade_id=trade_id,
                    pair=None,
                    direction=None,
                    entry_price=None,
                    exit_price=None,
                    stop_loss=None,
                    take_profit=None,
                    pnl_pips=None,
                    pnl_usd=None,
                    setup_name=None,
                    entry_type=None,
                    entry_time=None,
                    close_time=None,
                    outcome='force_close',
                    validator_verdict=None,
                ))
                logger.info("Post-trade audit scheduled for force-closed %s", trade_id)
            except Exception as _audit_err:
                logger.warning("Force-close audit skipped for %s: %s", trade_id, _audit_err)
            return True
        except Exception as e:
            logger.error("Force-close failed for %s: %s", trade_id, e)
            return False

    def _record_exit_learning(self, trade_id: str, watcher: 'TradeWatcher', closed_trade: Dict, 
                            setup_name: str, pnl_pips: float, realized_pl: float, r_mult: float,
                            duration: int, threat_zone: str, user_id: int = None) -> None:
        """Record comprehensive exit data for guardian learning.
        
        This is supplementary learning data - NEVER crash the guardian.
        All errors are captured and logged safely.
        """
        try:
            # Basic trade data
            exit_price = float(closed_trade.get('averageClosePrice', closed_trade.get('price', 0)))
            open_time = closed_trade.get('openTime', '')
            close_time = closed_trade.get('closeTime', '')
            
            # Determine exit reason
            exit_reason = 'unknown'
            primary_exit_signal = None
            
            if threat_zone == 'BLACK':
                exit_reason = 'guardian_emergency'
                primary_exit_signal = 'black_zone_exit'
            elif watcher.take_profit and exit_price:
                tp_hit = (watcher.direction == 'buy' and exit_price >= watcher.take_profit) or \
                         (watcher.direction == 'sell' and exit_price <= watcher.take_profit)
                sl_hit = (watcher.direction == 'buy' and exit_price <= watcher.stop_loss) or \
                         (watcher.direction == 'sell' and exit_price >= watcher.stop_loss)
                if tp_hit:
                    exit_reason = 'take_profit'
                    primary_exit_signal = 'tp_hit'
                elif sl_hit:
                    exit_reason = 'stop_loss'
                    primary_exit_signal = 'sl_hit'
                elif threat_zone in ['RED', 'YELLOW']:
                    exit_reason = 'guardian_threat'
                    primary_exit_signal = f"{threat_zone.lower()}_zone_exit"
                else:
                    exit_reason = 'manual_close'
                    primary_exit_signal = 'manual_close'
            
            # Extract market state from latest threat assessment
            threat_level = None
            rsi_at_exit = None
            stoch_at_exit = None
            bb_width_at_exit = None
            fan_state_at_exit = None
            fan_direction_at_exit = None
            velocity_at_exit = None
            trend_health_at_exit = None
            ema_sep_at_exit = None
            
            if watcher.last_threat:
                threat_level = watcher.last_threat.get('threat_level')
                market_state = watcher.last_threat.get('market_state', {})
                
                # Extract indicators from market state
                rsi_at_exit = market_state.get('rsi')
                stoch_at_exit = market_state.get('stoch_k')  # Use stochastic K
                bb_width_at_exit = market_state.get('bb_width')
                
                # Extract fan and trend data
                fan_data = market_state.get('fan', {})
                if fan_data:
                    fan_state_at_exit = fan_data.get('state')
                    fan_direction_at_exit = fan_data.get('direction')
                
                velocity_at_exit = market_state.get('velocity')
                trend_health_at_exit = market_state.get('trend_health')
                
                # Calculate EMA separation from EMA data
                ema_21 = market_state.get('ema_21')
                ema_55 = market_state.get('ema_55')
                if ema_21 and ema_55:
                    ema_sep_at_exit = abs(ema_21 - ema_55) / ema_21 * 100  # Percentage separation
            
            # Calculate max favorable/adverse excursion from watcher data
            max_favorable_excursion_pips = getattr(watcher, '_peak_pnl_pips', 0.0)
            max_adverse_excursion_pips = 0.0  # TODO: Track this in future watcher enhancement
            mfe_time_minutes = None  # TODO: Track when MFE occurred
            
            # Get entry type and regime from trade thesis
            entry_type = None
            regime = None
            if hasattr(watcher, 'trade_thesis') and watcher.trade_thesis:
                entry_type = watcher.trade_thesis.get('entry_type')
                # Try to extract regime from thesis or market state
                regime = watcher.trade_thesis.get('regime') or \
                        (watcher.last_threat.get('market_state', {}).get('regime') if watcher.last_threat else None)
            
            # Calculate initial R:R target
            initial_rr_target = None
            if watcher.stop_loss and watcher.take_profit:
                risk_pips = abs(watcher.entry_price - watcher.stop_loss) / watcher.pip_size
                reward_pips = abs(watcher.take_profit - watcher.entry_price) / watcher.pip_size
                if risk_pips > 0:
                    initial_rr_target = reward_pips / risk_pips
            
            # Calculate partial TP and re-entry analysis
            partial_tp_pips = None
            partial_tp_taken = 0
            remaining_position_pips = pnl_pips
            re_entry_available = 0
            re_entry_window_pips = None
            optimal_exit_level = max_favorable_excursion_pips  # Default to MFE
            reversal_signal = None
            position_sizing_usd = abs(float(watcher.units)) * abs(watcher.entry_price) if hasattr(watcher, 'units') else None
            
            # Check if this was a partial TP situation (trade closed with profit but MFE was higher)
            if pnl_pips > 0 and max_favorable_excursion_pips > pnl_pips + 2:  # MFE was >2 pips higher than exit
                # This could have been a partial TP situation
                # For Tim's philosophy: optimal partial TP is around 5-12 pips
                if max_favorable_excursion_pips >= 5:
                    partial_tp_pips = min(max_favorable_excursion_pips * 0.6, 12)  # 60% of MFE or 12 pips max
                    partial_tp_taken = 0  # Wasn't actually taken, but could have been
                    
                    # Simulate what remaining position would have done
                    remaining_position_pips = pnl_pips  # Would have been same since full position held
                    
                    # Analyze if re-entry after partial TP would have been profitable
                    # For now, use heuristics based on market state at exit
                    try:
                        # If trend was strong and no reversal signals, likely re-entry opportunity
                        strong_trend = (
                            velocity_at_exit and abs(velocity_at_exit) > 0.5 and
                            fan_state_at_exit in ['expanding', 'strong'] and
                            threat_level and threat_level < 40  # Not too much threat
                        )
                        
                        if strong_trend and exit_reason in ['take_profit', 'manual_close']:
                            re_entry_available = 1
                            # Estimate potential re-entry gain based on trend strength
                            if velocity_at_exit:
                                re_entry_window_pips = min(abs(velocity_at_exit) * 10, 15)  # Conservative estimate
                    except Exception as e:
                        logger.warning("[GUARDIAN] Re-entry analysis failed: %s", e)
                        # If analysis fails, assume no re-entry opportunity
            
            # Determine reversal signal from threat assessment
            if threat_zone in ['RED', 'BLACK']:
                reversal_signal = f"threat_zone_{threat_zone.lower()}"
            elif rsi_at_exit and (rsi_at_exit > 70 or rsi_at_exit < 30):
                reversal_signal = "rsi_extreme"
            elif bb_width_at_exit and bb_width_at_exit < 0.0005:  # Very tight BB
                reversal_signal = "bb_squeeze"
            elif fan_state_at_exit == 'converging':
                reversal_signal = "ema_fan_converging"
            
            # Insert into exit_learning table
            with get_db() as conn:
                conn.execute("""
                INSERT OR REPLACE INTO exit_learning (
                    trade_id, user_id, setup_name, pair, direction, regime, entry_type,
                    entry_price, initial_sl, initial_tp, initial_rr_target,
                    exit_price, exit_reason, pnl_pips, pnl_usd, actual_rr,
                    duration_minutes, max_favorable_excursion_pips, max_adverse_excursion_pips,
                    mfe_time_minutes, primary_exit_signal, threat_level_at_exit, threat_zone_at_exit,
                    rsi_at_exit, stoch_at_exit, bb_width_at_exit, fan_state_at_exit,
                    fan_direction_at_exit, velocity_at_exit, trend_health_at_exit, ema_sep_at_exit,
                    partial_tp_pips, partial_tp_taken, remaining_position_pips, re_entry_available,
                    re_entry_window_pips, optimal_exit_level, reversal_signal, position_sizing_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id, user_id, setup_name, watcher.instrument, watcher.direction,
                    regime, entry_type, watcher.entry_price, watcher.stop_loss, watcher.take_profit,
                    initial_rr_target, exit_price, exit_reason, pnl_pips, realized_pl, r_mult,
                    duration, max_favorable_excursion_pips, max_adverse_excursion_pips,
                    mfe_time_minutes, primary_exit_signal, threat_level, threat_zone,
                    rsi_at_exit, stoch_at_exit, bb_width_at_exit, fan_state_at_exit,
                    fan_direction_at_exit, velocity_at_exit, trend_health_at_exit, ema_sep_at_exit,
                    partial_tp_pips, partial_tp_taken, remaining_position_pips, re_entry_available,
                    re_entry_window_pips, optimal_exit_level, reversal_signal, position_sizing_usd
                ))
                
            logger.debug("Exit learning recorded for trade %s: %s %s %.1f pips", 
                        trade_id, setup_name, exit_reason, pnl_pips)
                        
        except Exception as e:
            # NEVER crash the guardian - this is supplementary learning data
            logger.warning("Exit learning recording failed for trade %s: %s", trade_id, e)

    def _get_exit_learning_context(self, setup_name: str, pair: str, regime: str, user_id: int) -> Dict:
        """Query historical exit data to inform monitoring strategy.
        
        Focuses on Tim's trading philosophy: 5-20 pips target, partial TP, re-entry.
        """
        try:
            with get_db(readonly=True) as conn:
                # MFE profile for this setup
                mfe_row = conn.execute("""
                    SELECT COUNT(*) as trades,
                           AVG(max_favorable_excursion_pips) as avg_mfe,
                           AVG(CASE WHEN pnl_pips > 0 THEN max_favorable_excursion_pips END) as avg_mfe_wins,
                           AVG(duration_minutes) as avg_duration,
                           AVG(CASE WHEN pnl_pips > 0 THEN actual_rr END) as avg_win_rr,
                           AVG(optimal_exit_level) as avg_optimal_exit
                    FROM exit_learning
                    WHERE setup_name=? AND pair=? AND user_id=?
                    AND created_at > datetime('now', '-90 days')
                """, (setup_name, pair, user_id)).fetchone()
                
                # Partial TP analysis - what's the sweet spot for this setup?
                partial_tp_row = conn.execute("""
                    SELECT AVG(CASE WHEN max_favorable_excursion_pips >= 5 
                                   THEN max_favorable_excursion_pips * 0.6 
                                   ELSE NULL END) as suggested_partial_tp_pips,
                           COUNT(CASE WHEN re_entry_available = 1 THEN 1 END) * 1.0 / COUNT(*) as re_entry_success_rate,
                           AVG(re_entry_window_pips) as avg_re_entry_potential
                    FROM exit_learning
                    WHERE setup_name=? AND pair=? AND user_id=?
                    AND pnl_pips > 0 
                    AND created_at > datetime('now', '-90 days')
                """, (setup_name, pair, user_id)).fetchone()
                
                # Best exit signals for this setup
                signals = conn.execute("""
                    SELECT primary_exit_signal,
                           COUNT(*) as times_used,
                           AVG(CASE WHEN pnl_pips > 0 THEN 1.0 ELSE 0 END) as preserved_profit_rate,
                           AVG(pnl_pips) as avg_pnl_pips
                    FROM exit_learning
                    WHERE setup_name=? AND pair=? AND user_id=?
                    AND primary_exit_signal IS NOT NULL
                    GROUP BY primary_exit_signal
                    HAVING COUNT(*) >= 3
                    ORDER BY preserved_profit_rate DESC
                """, (setup_name, pair, user_id)).fetchall()
                
                # Tim's specific targets: what percentage of trades hit 5-20 pip targets?
                target_analysis = conn.execute("""
                    SELECT 
                        COUNT(CASE WHEN max_favorable_excursion_pips >= 5 THEN 1 END) * 1.0 / COUNT(*) as hit_5_pips_rate,
                        COUNT(CASE WHEN max_favorable_excursion_pips >= 10 THEN 1 END) * 1.0 / COUNT(*) as hit_10_pips_rate,
                        COUNT(CASE WHEN max_favorable_excursion_pips >= 20 THEN 1 END) * 1.0 / COUNT(*) as hit_20_pips_rate,
                        AVG(CASE WHEN pnl_pips BETWEEN 5 AND 20 THEN pnl_pips END) as avg_target_zone_exit
                    FROM exit_learning
                    WHERE setup_name=? AND pair=? AND user_id=?
                    AND created_at > datetime('now', '-90 days')
                """, (setup_name, pair, user_id)).fetchone()
                
                # Calculate suggested partial TP level (Tim's sweet spot)
                suggested_partial_tp = None
                if partial_tp_row and partial_tp_row['suggested_partial_tp_pips']:
                    raw_suggestion = partial_tp_row['suggested_partial_tp_pips']
                    # Clamp to Tim's preferred range: 5-12 pips
                    suggested_partial_tp = max(5, min(12, raw_suggestion))
                elif mfe_row and mfe_row['avg_mfe_wins']:
                    # Fallback: 50% of average winning MFE, clamped to 5-12 pips
                    suggested_partial_tp = max(5, min(12, mfe_row['avg_mfe_wins'] * 0.5))
                
                return {
                    'trades': mfe_row['trades'] if mfe_row else 0,
                    'avg_mfe_pips': mfe_row['avg_mfe'] if mfe_row else None,
                    'avg_win_rr': mfe_row['avg_win_rr'] if mfe_row else None,
                    'avg_duration_min': mfe_row['avg_duration'] if mfe_row else None,
                    'avg_optimal_exit': mfe_row['avg_optimal_exit'] if mfe_row else None,
                    'best_exit_signals': [dict(s) for s in signals] if signals else [],
                    
                    # Tim's trading philosophy metrics
                    'suggested_partial_tp_pips': suggested_partial_tp,
                    're_entry_success_rate': partial_tp_row['re_entry_success_rate'] if partial_tp_row else 0,
                    'avg_re_entry_potential_pips': partial_tp_row['avg_re_entry_potential'] if partial_tp_row else None,
                    
                    # Target hit rates for Tim's 5-20 pip strategy
                    'hit_5_pips_rate': target_analysis['hit_5_pips_rate'] if target_analysis else 0,
                    'hit_10_pips_rate': target_analysis['hit_10_pips_rate'] if target_analysis else 0,
                    'hit_20_pips_rate': target_analysis['hit_20_pips_rate'] if target_analysis else 0,
                    'avg_target_zone_exit': target_analysis['avg_target_zone_exit'] if target_analysis else None,
                    
                    # Strategy recommendation
                    'recommended_strategy': self._generate_strategy_recommendation(
                        mfe_row, partial_tp_row, target_analysis, suggested_partial_tp
                    )
                }
        except Exception as e:
            logger.warning("Exit learning context query failed for %s %s: %s", setup_name, pair, e)
            return {
                'trades': 0,
                'avg_mfe_pips': None,
                'avg_win_rr': None,
                'avg_duration_min': None,
                'best_exit_signals': [],
                'suggested_partial_tp_pips': 8,  # Default to 8 pips for Tim's strategy
                're_entry_success_rate': 0,
                'avg_re_entry_potential_pips': None,
                'hit_5_pips_rate': 0,
                'hit_10_pips_rate': 0,
                'hit_20_pips_rate': 0,
                'avg_target_zone_exit': None,
                'recommended_strategy': 'insufficient_data'
            }

    def _generate_strategy_recommendation(self, mfe_row, partial_tp_row, target_analysis, suggested_partial_tp):
        """Generate strategy recommendation based on historical data."""
        try:
            if not mfe_row or mfe_row['trades'] < 5:
                return 'insufficient_data'
            
            hit_10_rate = target_analysis['hit_10_pips_rate'] if target_analysis else 0
            re_entry_rate = partial_tp_row['re_entry_success_rate'] if partial_tp_row else 0
            
            if hit_10_rate > 0.7 and re_entry_rate > 0.4:
                return 'partial_tp_aggressive'  # Take profit at suggested level, re-enter on continuation
            elif hit_10_rate > 0.5:
                return 'partial_tp_conservative'  # Take partial profit, trail remainder
            elif hit_10_rate < 0.3:
                return 'quick_scalp'  # Take profit fast, this setup doesn't run far
            else:
                return 'standard_trail'  # Standard trailing stop approach
                
        except Exception as e:
            logger.warning("[GUARDIAN] Exit strategy selection failed: %s", e)
            return 'standard_trail'
