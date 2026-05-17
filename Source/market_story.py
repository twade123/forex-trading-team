#!/usr/bin/env python3
"""
Market Story Reader — reads the market like a trader, not a checklist.

This is the scout's brain. Instead of stacking individual indicator scores,
it reads the market in contextual layers, just like the Position Guardian
does for threat assessment — but inverted for ENTRY opportunities.

Layer 1: Trend Narrative (EMA fan) — "What's the story?"
Layer 2: Price Structure (E100 + candles + wicks) — "What are candles doing?"
Layer 3: Momentum Confirmation (RSI+Stoch+MACD as ONE read) — "Does momentum agree?"

After these 3 layers form a thesis, Layer 4 (historical validation) runs
in the scout to overlay backtest data on top.

Philosophy:
  A trader doesn't check RSI, then Stochastic, then BB separately.
  They look at the chart, see the trend, see what candles are doing at key
  levels, and THEN glance at momentum to confirm. This module does that.
"""

from typing import List, Dict, Any, Optional, Tuple
import math

# Import the candle structure analyzer
try:
    from backtester.candle_structure import analyze_candle_structure
    from backtester.ema_separation import generate_market_picture, calculate_ema
    from backtester.candle_patterns import detect_all_patterns
except ImportError:
    from Source.backtester.candle_structure import analyze_candle_structure
    from Source.backtester.ema_separation import generate_market_picture, calculate_ema
    from Source.backtester.candle_patterns import detect_all_patterns


def read_market_story(
    pair: str,
    candles: List[Dict],
    mkt_picture: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Read the complete market story for a pair.

    Args:
        pair: instrument name e.g. 'EUR_USD'
        candles: list of completed candle dicts with OHLCV
        mkt_picture: pre-computed generate_market_picture() result (optional,
                     will be computed if not provided)

    Returns:
        {
          'has_opportunity': bool,        # Is there a tradeable thesis?
          'direction': str,               # 'buy' | 'sell' | 'none'
          'opportunity_score': float,     # 0-100, how strong the setup is
          'confidence': float,            # 0-1, how confident we are
          'thesis': str,                  # One-line thesis ("Counter-trend long: fan peaked, E100 hammer, momentum exhausted")
          'narrative': str,               # Full multi-line narrative
          'layers': {
              'trend': {...},             # Layer 1 output
              'structure': {...},         # Layer 2 output
              'momentum': {...},          # Layer 3 output
          },
          'entry_type': str,              # 'counter_trend_reversal' | 'trend_continuation' | 'breakout' | 'e100_bounce' | 'early_expansion' | 'ema_fan_expansion' | 'none'
          'warnings': [str],             # Things that could invalidate the thesis
        }
    """
    result = {
        'has_opportunity': False,
        'direction': 'none',
        'opportunity_score': 0,
        'confidence': 0.0,
        'thesis': 'No clear opportunity',
        'narrative': '',
        'layers': {'trend': {}, 'structure': {}, 'momentum': {}},
        'entry_type': 'none',
        'warnings': [],
    }

    if len(candles) < 200:
        result['narrative'] = 'Insufficient data for market read.'
        return result

    # ── Compute market picture if not provided ──
    if mkt_picture is None:
        candle_dicts = [{'time': c.get('time', ''), 'open': c['open'], 'high': c['high'],
                         'low': c['low'], 'close': c['close']} for c in candles]
        mkt_picture = generate_market_picture(pair, candle_dicts)

    # ── Compute EMAs for candle structure analysis ──
    closes = [float(c['close']) for c in candles]
    ema_21 = calculate_ema(closes, 21)
    ema_55 = calculate_ema(closes, 55)
    ema_100 = calculate_ema(closes, 100)

    # ══════════════════════════════════════════════════════════════════
    # LAYER 1: TREND NARRATIVE — "What's the story?"
    # The EMA fan tells us the trend direction, strength, and phase.
    # This sets the CONTEXT for everything else.
    # ══════════════════════════════════════════════════════════════════

    ema = mkt_picture.get('ema', {})
    fan_state = ema.get('fan_state', 'unknown')
    fan_dir = ema.get('fan_direction', 'mixed')
    velocity = ema.get('separation_velocity', 0)
    trend_health = ema.get('trend_health', 0)
    reversal_risk = ema.get('reversal_risk', 'unknown')
    recommended_bias = ema.get('recommended_bias', 'neutral')
    e100_candle_pat = ema.get('e100_candle_pattern')

    trend_layer = {
        'fan_state': fan_state,
        'fan_direction': fan_dir,
        'velocity': velocity,
        'trend_health': trend_health,
        'reversal_risk': reversal_risk,
        'recommended_bias': recommended_bias,
        'e100_candle_pattern': e100_candle_pat,
        'narrative': mkt_picture.get('ema_narrative', ''),
    }
    result['layers']['trend'] = trend_layer

    # ══════════════════════════════════════════════════════════════════
    # LAYER 2: PRICE STRUCTURE — "What are candles doing at key levels?"
    # Wicks, body progression, EMA interaction, rejection patterns.
    # This tells us WHERE the fight is and who's winning.
    # ══════════════════════════════════════════════════════════════════

    candle_structure = analyze_candle_structure(candles, ema_21, ema_55, ema_100)

    structure_layer = {
        'candle_structure': candle_structure,
        'e100_interaction': candle_structure.get('ema_interaction', {}).get('e100', {}),
        'wick_pressure': candle_structure.get('wick_analysis', {}),
        'body_trend': candle_structure.get('body_progression', {}),
        'consecutive': candle_structure.get('consecutive_structure', {}),
        'narrative': candle_structure.get('structure_narrative', ''),
    }
    result['layers']['structure'] = structure_layer

    # ══════════════════════════════════════════════════════════════════
    # LAYER 3: MOMENTUM CONFIRMATION — ONE read, not three
    # RSI, Stoch, MACD all measure the same underlying momentum.
    # Synthesize into a single state. Context from Layer 1 determines
    # whether "overbought" is normal (strong trend) or alarming (peaked fan).
    # ══════════════════════════════════════════════════════════════════

    rsi_data = mkt_picture.get('rsi', {})
    stoch_data = mkt_picture.get('stochastic', {})
    rsi_val = rsi_data.get('value', 50)
    stoch_k = stoch_data.get('k', 50)
    stoch_d = stoch_data.get('d', 50)

    # MACD from candles (compute if not in picture)
    macd_hist = _get_macd_histogram(closes)

    momentum = _synthesize_momentum(
        rsi_val, stoch_k, stoch_d, macd_hist,
        fan_state, fan_dir, trend_health
    )
    result['layers']['momentum'] = momentum

    # ══════════════════════════════════════════════════════════════════
    # THESIS FORMATION — Combine layers into a trading thesis
    # ══════════════════════════════════════════════════════════════════

    thesis = _form_thesis(trend_layer, structure_layer, momentum, mkt_picture, pair)
    result.update(thesis)

    return result


# ═══════════════════════════════════════════════════════════════════
# MOMENTUM SYNTHESIS
# ═══════════════════════════════════════════════════════════════════

def _synthesize_momentum(
    rsi: float, stoch_k: float, stoch_d: float, macd_hist: float,
    fan_state: str, fan_dir: str, trend_health: float,
) -> Dict[str, Any]:
    """
    Synthesize RSI + Stochastic + MACD into a single momentum read.

    In a strong expanding trend, overbought RSI/Stoch is NORMAL.
    Momentum only matters when it DIVERGES from the trend story.
    """

    # Count extreme readings
    bullish_extremes = 0
    bearish_extremes = 0

    if rsi <= 30: bullish_extremes += 1    # Oversold = potential bullish
    elif rsi >= 70: bearish_extremes += 1  # Overbought = potential bearish

    if stoch_k <= 20: bullish_extremes += 1
    elif stoch_k >= 80: bearish_extremes += 1

    # Stoch crossover adds weight
    stoch_bull_cross = stoch_k > stoch_d and stoch_k < 30
    stoch_bear_cross = stoch_k < stoch_d and stoch_k > 70
    if stoch_bull_cross: bullish_extremes += 1
    if stoch_bear_cross: bearish_extremes += 1

    if macd_hist > 0: bullish_extremes += 0.5
    elif macd_hist < 0: bearish_extremes += 0.5

    # ── Contextual interpretation ──
    # In a strong trend, extreme readings are expected
    strong_trend = fan_state in ('expanding', 'accelerating') and trend_health > 60

    if strong_trend and fan_dir == 'bullish':
        # Overbought in a strong bull trend = normal, not a sell signal
        if bearish_extremes >= 2:
            state = 'stretched_with_trend'
            significance = 'low'  # Expected, not alarming
            note = 'Momentum stretched but trend strong — overbought is normal here'
        elif bullish_extremes >= 2:
            state = 'diverging_from_trend'
            significance = 'high'  # Oversold in a bull trend = something wrong
            note = 'Momentum diverging: oversold readings in a bullish trend — structural shift?'
        else:
            state = 'confirming'
            significance = 'low'
            note = 'Momentum neutral, trend driving'
    elif strong_trend and fan_dir == 'bearish':
        if bullish_extremes >= 2:
            state = 'stretched_with_trend'
            significance = 'low'
            note = 'Momentum stretched but trend strong — oversold is normal here'
        elif bearish_extremes >= 2:
            state = 'diverging_from_trend'
            significance = 'high'
            note = 'Momentum diverging: overbought readings in a bearish trend — structural shift?'
        else:
            state = 'confirming'
            significance = 'low'
            note = 'Momentum neutral, trend driving'
    else:
        # Trend NOT strong — momentum readings carry real weight
        if bullish_extremes >= 2 and bearish_extremes == 0:
            state = 'oversold'
            significance = 'high'
            note = f'Momentum oversold (RSI {rsi:.0f}, Stoch {stoch_k:.0f}) — bounce potential'
        elif bearish_extremes >= 2 and bullish_extremes == 0:
            state = 'overbought'
            significance = 'high'
            note = f'Momentum overbought (RSI {rsi:.0f}, Stoch {stoch_k:.0f}) — pullback potential'
        elif bullish_extremes >= 1 and bearish_extremes >= 1:
            state = 'conflicting'
            significance = 'medium'
            note = 'Momentum indicators conflicting — no clear read'
        else:
            state = 'neutral'
            significance = 'low'
            note = 'Momentum neutral — no extreme readings'

    # Exhaustion detection: when fan is peaked/decelerating AND momentum is extreme
    exhausted = False
    if fan_state in ('peaked', 'decelerating', 'contracting'):
        if fan_dir == 'bullish' and bearish_extremes >= 2:
            exhausted = True
            state = 'exhausted_bull'
            significance = 'critical'
            note = f'Bull trend exhausting: fan {fan_state} + RSI {rsi:.0f}/Stoch {stoch_k:.0f} overbought'
        elif fan_dir == 'bearish' and bullish_extremes >= 2:
            exhausted = True
            state = 'exhausted_bear'
            significance = 'critical'
            note = f'Bear trend exhausting: fan {fan_state} + RSI {rsi:.0f}/Stoch {stoch_k:.0f} oversold'

    return {
        'state': state,
        'significance': significance,
        'exhausted': exhausted,
        'rsi': round(rsi, 1),
        'stoch_k': round(stoch_k, 1),
        'stoch_d': round(stoch_d, 1),
        'macd_histogram': round(macd_hist, 6),
        'bullish_extremes': bullish_extremes,
        'bearish_extremes': bearish_extremes,
        'stoch_bull_cross': stoch_k > stoch_d and stoch_k < 30,
        'stoch_bear_cross': stoch_k < stoch_d and stoch_k > 70,
        'narrative': note,
    }


# ═══════════════════════════════════════════════════════════════════
# THESIS FORMATION — The actual trading decision
# ═══════════════════════════════════════════════════════════════════

def _form_thesis(
    trend: Dict, structure: Dict, momentum: Dict, mkt_picture: Dict, pair: str = "?",
) -> Dict[str, Any]:
    """
    Combine all three layers into a trading thesis.

    Entry types:
    1. COUNTER_TREND_REVERSAL — Fan peaked/exhausting + E100 bounce + momentum exhausted
    2. TREND_CONTINUATION — Fan expanding + pullback to E21/E55 + momentum confirming
    3. E100_BOUNCE — Price testing E100 with reversal candle + wick rejection
    5. EMA_FAN_EXPANSION — Fresh EMA cross + accelerating separation (the money move)
    4. BREAKOUT — BB squeeze + ranges compressing + fresh EMA cross
    5. NONE — No clear thesis
    """
    warnings = []
    thesis_parts = []

    fan_state = trend['fan_state']
    fan_dir = trend['fan_direction']
    trend_health = trend['trend_health']

    e100_int = structure.get('e100_interaction', {})
    wick = structure.get('wick_pressure', {})
    body = structure.get('body_trend', {})
    consec = structure.get('consecutive', {})

    mom_state = momentum['state']
    mom_significance = momentum['significance']

    # Score each potential entry type
    scores = {
        'counter_trend_reversal': 0,
        'trend_continuation': 0,
        'e100_bounce': 0,
        'breakout': 0,
        'early_expansion': 0,
        'ema_fan_expansion': 0,
    }
    directions = {
        'counter_trend_reversal': 'none',
        'trend_continuation': 'none',
        'e100_bounce': 'none',
        'breakout': 'none',
        'early_expansion': 'none',
        'ema_fan_expansion': 'none',
    }

    # ── 1. COUNTER-TREND REVERSAL ──────────────────────────────────
    # Best setup: Fan peaked/decelerating + oscillators exhausted + structure confirming
    if fan_state in ('peaked', 'decelerating', 'contracting'):
        ctr_dir = 'buy' if fan_dir == 'bearish' else 'sell'
        directions['counter_trend_reversal'] = ctr_dir

        # Fan exhaustion (0-35)
        if fan_state == 'peaked':
            scores['counter_trend_reversal'] += 35
            thesis_parts.append(f'Fan peaked ({fan_dir})')
        elif fan_state == 'decelerating':
            scores['counter_trend_reversal'] += 25
            thesis_parts.append(f'Fan decelerating ({fan_dir})')
        elif fan_state == 'contracting':
            scores['counter_trend_reversal'] += 30
            thesis_parts.append(f'Fan contracting ({fan_dir})')

        # Momentum exhaustion (0-30)
        if momentum['exhausted']:
            scores['counter_trend_reversal'] += 30
        elif mom_state in ('overbought', 'oversold') and mom_significance == 'high':
            scores['counter_trend_reversal'] += 20
        elif mom_significance == 'medium':
            scores['counter_trend_reversal'] += 10

        # Structure confirmation (0-25)
        pressure = wick.get('dominant_pressure', 'balanced')
        if ctr_dir == 'buy' and pressure == 'buying':
            scores['counter_trend_reversal'] += 20
        elif ctr_dir == 'sell' and pressure == 'selling':
            scores['counter_trend_reversal'] += 20

        # Exhaustion run adds to reversal case
        run = consec.get('run_state', 'neutral')
        if run == 'bear_exhaustion_risk' and ctr_dir == 'buy':
            scores['counter_trend_reversal'] += 5
        elif run == 'bull_exhaustion_risk' and ctr_dir == 'sell':
            scores['counter_trend_reversal'] += 5

        # Body shrinking = conviction fading in the trend = good for reversal
        if body.get('body_trend') == 'shrinking':
            scores['counter_trend_reversal'] += 5

        # E100 candle pattern confirmation
        e100_pat = trend.get('e100_candle_pattern')
        if e100_pat:
            pat_dir = e100_pat.get('direction', '')
            if pat_dir == ctr_dir:
                scores['counter_trend_reversal'] += 10
                thesis_parts.append(f"E100 {e100_pat.get('name', 'pattern')} confirming")

        # Warnings
        if fan_state == 'expanding':
            warnings.append('Fan still expanding — counter-trend is high risk')
            scores['counter_trend_reversal'] -= 20

    # ── 2. TREND CONTINUATION ──────────────────────────────────────
    # Fan expanding/stable + pullback to E21/E55 with bounce
    if fan_state in ('expanding', 'accelerating', 'stable') and fan_dir in ('bullish', 'bearish'):
        cont_dir = 'buy' if fan_dir == 'bullish' else 'sell'
        directions['trend_continuation'] = cont_dir

        # Fan strength (0-30)
        if fan_state == 'expanding':
            scores['trend_continuation'] += 25
        elif fan_state == 'accelerating':
            scores['trend_continuation'] += 30
        elif fan_state == 'stable':
            scores['trend_continuation'] += 15

        # Health bonus
        if trend_health > 70:
            scores['trend_continuation'] += 10
        elif trend_health > 50:
            scores['trend_continuation'] += 5

        # Pullback to EMA with bounce (0-30)
        e21_int = structure.get('candle_structure', {}).get('ema_interaction', {}).get('e21', {})
        e55_int = structure.get('candle_structure', {}).get('ema_interaction', {}).get('e55', {})

        pulled_back = False
        if e21_int.get('interaction') in ('support', 'strong_support') and cont_dir == 'buy':
            scores['trend_continuation'] += 25
            pulled_back = True
            thesis_parts.append('Pullback bouncing off E21')
        elif e21_int.get('interaction') in ('resistance', 'strong_resistance') and cont_dir == 'sell':
            scores['trend_continuation'] += 25
            pulled_back = True
            thesis_parts.append('Pullback rejected at E21')
        elif e55_int.get('interaction') in ('support', 'strong_support') and cont_dir == 'buy':
            scores['trend_continuation'] += 20
            pulled_back = True
            thesis_parts.append('Deeper pullback bouncing off E55')
        elif e55_int.get('interaction') in ('resistance', 'strong_resistance') and cont_dir == 'sell':
            scores['trend_continuation'] += 20
            pulled_back = True
            thesis_parts.append('Deeper pullback rejected at E55')

        # Momentum confirming the continuation (0-15)
        if mom_state == 'confirming':
            scores['trend_continuation'] += 10
        elif mom_state == 'stretched_with_trend':
            # Still with trend but stretched — less room to run
            scores['trend_continuation'] += 5
            warnings.append('Momentum stretched — reduced upside potential')

        # Wick confirmation
        if cont_dir == 'buy' and wick.get('dominant_pressure') == 'buying':
            scores['trend_continuation'] += 10
        elif cont_dir == 'sell' and wick.get('dominant_pressure') == 'selling':
            scores['trend_continuation'] += 10

        # Body growing in trend direction = conviction
        if body.get('body_trend') == 'growing':
            bias = body.get('direction_bias', '')
            if (cont_dir == 'buy' and 'bull' in bias) or (cont_dir == 'sell' and 'bear' in bias):
                scores['trend_continuation'] += 5

        # Penalty if no pullback detected (just chasing)
        if not pulled_back:
            scores['trend_continuation'] -= 15
            warnings.append('No pullback to EMA — chasing risk')

    # ── 3. E100 BOUNCE ─────────────────────────────────────────────
    # Price at E100 with reversal candle — works in any fan state
    e100_interaction = e100_int.get('interaction', 'distant')
    if e100_interaction in ('testing', 'support', 'strong_support', 'resistance', 'strong_resistance'):
        # Determine direction from E100 role
        price_above = structure.get('candle_structure', {}).get('ema_interaction', {}).get('e100_distance_pct', 0) > 0
        bounce_dir = 'buy' if e100_interaction in ('support', 'strong_support', 'testing') and price_above else 'sell'
        if e100_interaction in ('resistance', 'strong_resistance'):
            bounce_dir = 'sell' if not price_above else 'buy'
        directions['e100_bounce'] = bounce_dir

        # Interaction quality (0-35)
        if e100_interaction == 'strong_support' and bounce_dir == 'buy':
            scores['e100_bounce'] += 35
        elif e100_interaction == 'strong_resistance' and bounce_dir == 'sell':
            scores['e100_bounce'] += 35
        elif e100_interaction in ('support', 'resistance'):
            scores['e100_bounce'] += 25
        elif e100_interaction == 'testing':
            scores['e100_bounce'] += 15

        # Wick rejection at E100 (0-25)
        if bounce_dir == 'buy' and wick.get('dominant_pressure') == 'buying':
            scores['e100_bounce'] += 20
            thesis_parts.append('Wicks showing buying at E100')
        elif bounce_dir == 'sell' and wick.get('dominant_pressure') == 'selling':
            scores['e100_bounce'] += 20
            thesis_parts.append('Wicks showing selling at E100')

        # E100 candle pattern
        e100_pat = trend.get('e100_candle_pattern')
        if e100_pat and e100_pat.get('direction') == bounce_dir:
            scores['e100_bounce'] += 20
            thesis_parts.append(f"{e100_pat.get('name', 'Reversal pattern')} at E100")

        # Momentum agreement (0-10)
        if bounce_dir == 'buy' and momentum.get('state') in ('oversold', 'exhausted_bear'):
            scores['e100_bounce'] += 10
        elif bounce_dir == 'sell' and momentum.get('state') in ('overbought', 'exhausted_bull'):
            scores['e100_bounce'] += 10

        # E100 broken = invalidates the bounce
        if e100_int.get('breaks', 0) >= 2:
            scores['e100_bounce'] = 0
            warnings.append('E100 already broken — bounce thesis invalid')

    # ── 4. BREAKOUT ────────────────────────────────────────────────
    # BB squeeze + compressing ranges + fresh EMA cross
    bb = mkt_picture.get('bollinger', {})
    if bb.get('squeeze', False) or consec.get('range_trend') == 'compressing':
        if bb.get('squeeze'):
            scores['breakout'] += 20
        if consec.get('range_trend') == 'compressing':
            scores['breakout'] += 15

        # Fresh cross gives direction
        if fan_state == 'just_crossed':
            scores['breakout'] += 25
            directions['breakout'] = 'buy' if fan_dir == 'bullish' else 'sell'
        elif fan_state in ('expanding', 'accelerating'):
            scores['breakout'] += 15
            directions['breakout'] = 'buy' if fan_dir == 'bullish' else 'sell'

        # Body growing = breakout conviction
        if body.get('body_trend') == 'growing':
            scores['breakout'] += 10

        if directions['breakout'] == 'none':
            # No direction signal — can't trade a breakout without direction
            scores['breakout'] = 0

    # ── 5. EMA FAN EXPANSION ──────────────────────────────────────
    # THE FULL THESIS (see thesis_definition.md):
    #   1. TRIGGER: E21 crosses E55
    #   2. FAN ORDERING: BUY = price > E21 > E55 > E100 / SELL = reverse
    #   3. E100 POSITIONING: E100 outside fan as support (buy) / resistance (sell)
    #   4. FAN SEPARATION: abs(E21 - E100) = TOTAL fan width, must be GROWING
    #   5. BB EXPANSION: Bollinger Bands expanding simultaneously
    #
    # This uses GATE logic — conditions 2-5 are required, not additive points.
    # Score reflects HOW STRONG the confirmed thesis is, not whether it exists.

    velocity = trend.get('separation_velocity', 0)
    velocity_trend = trend.get('fan_velocity_trend', 'unknown')
    candles_since = trend.get('candles_since_cross', 999)

    # BB bandwidth data
    bb = mkt_picture.get('bollinger', {})
    bb_expanding = bb.get('bb_expanding', False)
    bb_contracting = bb.get('bb_contracting', False)
    bb_acceleration = bb.get('bb_acceleration', 0)
    bb_squeeze = bb.get('squeeze', False)

    # Get raw EMA values for FULL fan width and ordering check
    _ema_data = mkt_picture.get('ema', {})
    _emas = _ema_data.get('current_emas', {})
    _e21 = _emas.get('ema_21') or _emas.get('ema21', 0)
    _e55 = _emas.get('ema_55') or _emas.get('ema55', 0)
    _e100 = _emas.get('ema_100') or _emas.get('ema100', 0)
    # Get close price — not passed directly, derive from EMA data or bollinger mid
    _close = bb.get('bb_mid', 0) or _e21 or 0

    # Resolve direction from cross / EMA positions
    _exp_fan_dir = fan_dir
    if _exp_fan_dir == 'mixed':
        _crossovers = _ema_data.get('crossovers', [])
        if _crossovers:
            _exp_fan_dir = _crossovers[-1].get('direction', 'mixed')
        if _exp_fan_dir == 'mixed' and _e21 and _e55:
            _exp_fan_dir = 'bullish' if _e21 > _e55 else 'bearish'

    # ── EARLY EXPANSION THESIS ────────────────────────────────────
    # Fresh EMA cross with direction still "mixed" but BB expanding — Tim's pattern
    if (fan_state in ('expanding', 'just_crossed') 
            and _e21 and _e55 and _e100 and _close
            and bb_expanding):

        # Resolve direction from cross or EMA positions
        _early_dir = fan_dir
        if _early_dir == 'mixed':
            _crossovers = _ema_data.get('crossovers', [])
            if _crossovers:
                _early_dir = _crossovers[-1].get('direction', 'mixed')
            if _early_dir == 'mixed' and _e21 and _e55:
                _early_dir = 'bullish' if _e21 > _e55 else 'bearish'

        # Only proceed if we resolved a direction
        if _early_dir in ('bullish', 'bearish'):
            early_dir = 'buy' if _early_dir == 'bullish' else 'sell'
            directions['early_expansion'] = early_dir

            # Base score: BB expanding + fan expanding/just_crossed + direction resolved = 45
            scores['early_expansion'] = 45
            thesis_parts.append(f'Early expansion: {_early_dir} fan developing + BB expanding')

            # E100 as S/R confirming direction = +15
            if early_dir == 'buy' and _e100 < _close:
                scores['early_expansion'] += 15
                thesis_parts.append('E100 support')
            elif early_dir == 'sell' and _e100 > _close:
                scores['early_expansion'] += 15
                thesis_parts.append('E100 resistance')

            # Momentum neutral or confirming = +10
            if mom_state == 'neutral':
                scores['early_expansion'] += 10
                thesis_parts.append('Momentum neutral')
            elif early_dir == 'buy' and mom_state in ('bullish', 'approaching_overbought', 'confirming'):
                scores['early_expansion'] += 10
                thesis_parts.append('Momentum confirming')
            elif early_dir == 'sell' and mom_state in ('bearish', 'approaching_oversold', 'confirming'):
                scores['early_expansion'] += 10
                thesis_parts.append('Momentum confirming')

            # Fan ordering starting to form = +10
            if early_dir == 'buy' and _close > _e21 > _e55:
                scores['early_expansion'] += 10
                thesis_parts.append('Fan ordering forming')
            elif early_dir == 'sell' and _close < _e21 < _e55:
                scores['early_expansion'] += 10
                thesis_parts.append('Fan ordering forming')

            # Recent cross bonus (bars since cross < 15)
            _crossovers = _ema_data.get('crossovers', [])
            if _crossovers and candles_since < 15:
                scores['early_expansion'] += 5
                thesis_parts.append(f'Fresh cross ({candles_since} bars)')

    # Only consider if we have a cross context (fan developing)
    if (fan_state in ('just_crossed', 'expanding', 'accelerating')
            and _exp_fan_dir in ('bullish', 'bearish')
            and _e21 and _e55 and _e100 and _close):

        exp_dir = 'buy' if _exp_fan_dir == 'bullish' else 'sell'
        directions['ema_fan_expansion'] = exp_dir

        # ── GATE 1: Fan ordering (price > E21 > E55 > E100 for buy) ──
        if exp_dir == 'buy':
            fan_ordered = _close > _e21 > _e55 > _e100
        else:
            fan_ordered = _close < _e21 < _e55 < _e100

        # ── GATE 2: E100 on correct side ──
        if exp_dir == 'buy':
            e100_correct = _e100 < _close  # E100 below = support
        else:
            e100_correct = _e100 > _close  # E100 above = resistance

        # ── GATE 3: Fan width (E21 to E100 TOTAL) is meaningful and growing ──
        fan_width = abs(_e21 - _e100)
        fan_width_pct = (fan_width / _close * 100) if _close > 0 else 0
        fan_width_ok = fan_width_pct >= 0.10  # Backtested: <0.10% is noise (PF 1.04)

        # Check if fan width is growing via velocity
        fan_growing = velocity > 0 and velocity_trend in ('accelerating', 'steady')

        # ── GATE 4: BB expanding ──
        bb_confirms = bb_expanding or (bb_squeeze and fan_state == 'just_crossed')

        # ── Count gates passed ──
        gates_passed = sum([fan_ordered, e100_correct, fan_width_ok and fan_growing, bb_confirms])
        all_gates = fan_ordered and e100_correct and (fan_width_ok and fan_growing) and bb_confirms

        # ── Build thesis status description ──
        gate_status = []
        gate_status.append(f"fan_ordered={'YES' if fan_ordered else 'NO'}")
        gate_status.append(f"E100={'support' if exp_dir=='buy' else 'resistance'}={'YES' if e100_correct else 'NO'}")
        gate_status.append(f"fan_width={fan_width_pct:.3f}%({'YES' if fan_width_ok and fan_growing else 'NO'})")
        gate_status.append(f"BB={'YES' if bb_confirms else 'NO'}")

        if all_gates:
            # ── ALL GATES PASSED — score reflects STRENGTH ──
            # Base: 50 for confirmed thesis
            scores['ema_fan_expansion'] = 50
            thesis_parts.append(f'THESIS CONFIRMED: {" | ".join(gate_status)}')

            # Velocity strength bonus (0-20)
            if velocity_trend == 'accelerating':
                if velocity >= 0.005:
                    scores['ema_fan_expansion'] += 20
                    thesis_parts.append(f'FAST acceleration ({velocity:.4f}%/bar)')
                elif velocity >= 0.001:
                    scores['ema_fan_expansion'] += 15
                    thesis_parts.append(f'Accelerating ({velocity:.4f}%/bar)')
                else:
                    scores['ema_fan_expansion'] += 10
                    thesis_parts.append(f'Early acceleration ({velocity:.4f}%/bar)')
            elif velocity_trend == 'steady' and velocity >= 0.002:
                scores['ema_fan_expansion'] += 10

            # Fan width magnitude bonus (0-15) — wider = more committed
            if fan_width_pct >= 0.20:
                scores['ema_fan_expansion'] += 15
                thesis_parts.append(f'Wide fan ({fan_width_pct:.3f}%)')
            elif fan_width_pct >= 0.15:
                scores['ema_fan_expansion'] += 10
            elif fan_width_pct >= 0.10:
                scores['ema_fan_expansion'] += 5

            # Momentum alignment bonus (0-10)
            if exp_dir == 'buy' and mom_state in ('bullish', 'approaching_overbought', 'confirming', 'stretched_with_trend'):
                scores['ema_fan_expansion'] += 10
            elif exp_dir == 'sell' and mom_state in ('bearish', 'approaching_oversold', 'confirming', 'stretched_with_trend'):
                scores['ema_fan_expansion'] += 10
            elif mom_state == 'neutral':
                scores['ema_fan_expansion'] += 5

            # BB acceleration bonus (0-5)
            if bb_acceleration > 0.01:
                scores['ema_fan_expansion'] += 5

        elif gates_passed >= 3:
            # ── DEVELOPING — 3 of 4 gates, thesis forming ──
            scores['ema_fan_expansion'] = 35
            missing = []
            if not fan_ordered: missing.append('fan not ordered')
            if not e100_correct: missing.append('E100 wrong side')
            if not (fan_width_ok and fan_growing): missing.append(f'fan width {fan_width_pct:.3f}% {"too narrow" if not fan_width_ok else "not growing"}')
            if not bb_confirms: missing.append('BB not expanding')
            thesis_parts.append(f'THESIS DEVELOPING (3/4): waiting on {", ".join(missing)}')
            thesis_parts.append(f'Gates: {" | ".join(gate_status)}')

        elif gates_passed >= 2 and fan_state == 'just_crossed':
            # ── EARLY — fresh cross with some alignment, worth watching ──
            scores['ema_fan_expansion'] = 20
            thesis_parts.append(f'THESIS EARLY (fresh cross, {gates_passed}/4 gates): {" | ".join(gate_status)}')

        else:
            # ── NOT READY — too few conditions met ──
            scores['ema_fan_expansion'] = 0
            if fan_state in ('just_crossed', 'expanding'):
                thesis_parts.append(f'THESIS NOT READY ({gates_passed}/4 gates): {" | ".join(gate_status)}')

        # Warnings for specific failures
        if not fan_ordered and fan_state != 'just_crossed':
            warnings.append(f'Fan NOT ordered ({"price > E21 > E55 > E100" if exp_dir == "buy" else "price < E21 < E55 < E100"} required)')
        if not e100_correct:
            warnings.append(f'E100 on WRONG side (need {"below" if exp_dir == "buy" else "above"} price)')
        if bb_contracting and not bb_squeeze:
            warnings.append(f'BB contracting ({bb_acceleration:+.3f}) — volatility not confirming')
        if velocity_trend == 'decelerating':
            warnings.append('Fan velocity decelerating — momentum fading')

    # ══════════════════════════════════════════════════════════════════
    # PICK THE BEST THESIS
    # ══════════════════════════════════════════════════════════════════

    best_type = max(scores, key=scores.get)
    best_score = scores[best_type]
    best_direction = directions[best_type]

    # Minimum threshold — need at least 40/100 to have a real thesis
    THESIS_THRESHOLD = 40

    if best_score < THESIS_THRESHOLD or best_direction == 'none':
        # No clear thesis — build a "waiting" narrative
        narrative_parts = [
            f"No clear entry thesis for {mkt_picture.get('pair', '?')}.",
            f"Trend: {fan_dir} fan {fan_state} (health {trend_health}/100).",
            f"Structure: {structure.get('narrative', 'N/A')[:200]}",
            f"Momentum: {momentum['narrative']}",
            f"Scores: CTR={scores['counter_trend_reversal']}, "
            f"CONT={scores['trend_continuation']}, "
            f"E100={scores['e100_bounce']}, "
            f"BRK={scores['breakout']}, "
            f"EFX={scores['ema_fan_expansion']}",
        ]
        return {
            'has_opportunity': False,
            'direction': 'none',
            'opportunity_score': best_score,
            'confidence': best_score / 100.0,
            'thesis': 'No clear opportunity — waiting for setup',
            'narrative': '\n'.join(narrative_parts),
            'entry_type': 'none',
            'warnings': warnings,
            'layers': {'trend': trend, 'structure': structure, 'momentum': momentum},
        }

    # Build the thesis
    confidence = min(best_score / 100.0, 0.95)

    type_labels = {
        'counter_trend_reversal': 'Counter-trend reversal',
        'trend_continuation': 'Trend continuation',
        'e100_bounce': 'E100 bounce',
        'breakout': 'Breakout',
        'early_expansion': 'Early expansion',
        'ema_fan_expansion': 'EMA fan expansion',
    }
    dir_label = 'LONG' if best_direction == 'buy' else 'SHORT'
    thesis_line = f"{type_labels[best_type]} {dir_label}: {' + '.join(thesis_parts[:3])}"

    # Full narrative
    narrative_parts = [
        f"=== {pair} — {type_labels[best_type]} {dir_label} (score: {best_score}/100) ===",
        "",
        f"[TREND] {trend['narrative']}",
        f"[STRUCTURE] {structure.get('narrative', 'N/A')}",
        f"[MOMENTUM] {momentum['narrative']}",
        "",
        f"Thesis: {thesis_line}",
        f"Confidence: {confidence:.0%}",
    ]

    if warnings:
        narrative_parts.append(f"Warnings: {'; '.join(warnings)}")

    # Secondary opportunities
    secondary = [(k, v) for k, v in scores.items() if k != best_type and v >= 30]
    if secondary:
        sec_str = ', '.join(f"{type_labels[k]}({v})" for k, v in secondary)
        narrative_parts.append(f"Secondary: {sec_str}")

    narrative_parts.append(
        f"\nAll scores: CTR={scores['counter_trend_reversal']}, "
        f"CONT={scores['trend_continuation']}, "
        f"E100={scores['e100_bounce']}, BRK={scores['breakout']}, "
        f"EXP={scores['early_expansion']}, EFX={scores['ema_fan_expansion']}"
    )

    return {
        'has_opportunity': True,
        'direction': best_direction,
        'opportunity_score': best_score,
        'confidence': confidence,
        'thesis': thesis_line,
        'narrative': '\n'.join(narrative_parts),
        'entry_type': best_type,
        'warnings': warnings,
        'layers': {'trend': trend, 'structure': structure, 'momentum': momentum},
    }


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def _get_macd_histogram(closes: List[float]) -> float:
    """Compute latest MACD histogram value from closes."""
    if len(closes) < 35:
        return 0.0
    try:
        ema12 = calculate_ema(closes, 12)
        ema26 = calculate_ema(closes, 26)
        macd_line = [a - b if not (_is_nan(a) or _is_nan(b)) else 0
                     for a, b in zip(ema12, ema26)]
        signal = calculate_ema(macd_line, 9)
        if _is_nan(macd_line[-1]) or _is_nan(signal[-1]):
            return 0.0
        return macd_line[-1] - signal[-1]
    except Exception:
        return 0.0


def _is_nan(v) -> bool:
    try:
        return v != v or math.isnan(v)
    except (TypeError, ValueError):
        return True
