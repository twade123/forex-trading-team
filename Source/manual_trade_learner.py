#!/usr/bin/env python3
"""
Manual Trade Learner — Learn from user trades that scout missed.

When a user closes a manual trade and scout had no matching finding:
1. Fetch M15 candles at entry time
2. Compute indicators + market picture at that moment
3. Check if it matches any existing thesis/setup/sniper pattern
4. If no match → record as a NEW pattern candidate
5. Track outcomes over time → when pattern has enough wins, add to playbook

This is how the system gets smarter from every trade the user makes.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
LEARNER_DB_PATH = os.path.join(SOURCE_DIR, '..', 'Data', 'manual_trade_patterns.json')

try:
    from db_connection import get_db, DB_PATH
except ImportError:
    get_db = None

try:
    from oanda_client import OandaClient
    _oanda = OandaClient()
except Exception:
    _oanda = None

try:
    from backtester.indicators import compute_all
    from backtester.ema_separation import generate_market_picture
    from market_story import read_market_story
    from trade_scout import score_v4, TF_PARAMS
    from setup_classifier import classify_setups
except ImportError as e:
    logger.warning(f"Import error in manual_trade_learner: {e}")


def _load_patterns() -> Dict:
    """Load recorded manual trade patterns."""
    if os.path.exists(LEARNER_DB_PATH):
        try:
            with open(LEARNER_DB_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"patterns": [], "version": 1, "last_updated": None}


def _save_patterns(data: Dict):
    """Save patterns to disk."""
    data['last_updated'] = datetime.now().isoformat()
    os.makedirs(os.path.dirname(LEARNER_DB_PATH), exist_ok=True)
    with open(LEARNER_DB_PATH, 'w') as f:
        json.dump(data, f, indent=2)


def analyze_manual_trade(trade: Dict) -> Dict:
    """
    Analyze a closed manual trade against all detection systems.
    
    Args:
        trade: dict with keys: pair, direction (buy/sell), entry_time (ISO), 
               exit_time, pips, result (win/loss), entry_price
    
    Returns:
        dict with analysis results: matched_systems, market_state, recommendation
    """
    pair = trade['pair']
    direction = trade.get('direction', 'buy').lower()
    entry_time = trade.get('entry_time', '')
    pips = trade.get('pips', 0)
    result = trade.get('result', 'unknown')
    
    analysis = {
        'pair': pair,
        'direction': direction,
        'entry_time': entry_time,
        'pips': pips,
        'result': result,
        'matched_systems': [],
        'market_state': {},
        'scout_had_finding': False,
        'recommendation': None,
    }
    
    # ── Step 1: Check if scout had a finding near this trade ──
    scout_dir = 'BULL' if direction in ('buy', 'bull', 'bullish') else 'BEAR'
    
    if get_db:
        try:
            with get_db() as conn:
                conn.row_factory = sqlite3.Row
                findings = conn.execute('''
                    SELECT direction, setup_type, setup_name, sniper_score,
                           ema_fan_state, ema_fan_direction, timestamp
                    FROM scout_findings
                    WHERE pair = ? AND direction = ?
                    AND timestamp BETWEEN datetime(?, "-60 minutes") AND ?
                    ORDER BY timestamp DESC LIMIT 1
                ''', (pair, scout_dir, entry_time, entry_time)).fetchall()
                
                if findings:
                    analysis['scout_had_finding'] = True
                    f = findings[0]
                    analysis['matched_systems'].append({
                        'system': 'scout',
                        'setup_type': f['setup_type'],
                        'setup_name': f['setup_name'],
                        'sniper_score': f['sniper_score'],
                        'fan_state': f['ema_fan_state'],
                    })
        except Exception as e:
            logger.warning(f"Scout check failed: {e}")
    
    # ── Step 2: Fetch candles and compute indicators at entry time ──
    candles = None
    indicators = None
    mkt_picture = None
    story = None
    
    if _oanda:
        try:
            _to_dt = datetime.fromisoformat(entry_time.replace('Z', '+00:00')) if isinstance(entry_time, str) else entry_time
            raw_candles = _oanda.get_candles(pair, granularity="M15", count=200, 
                                              to_time=_to_dt)
            if raw_candles:
                candles = [{
                    'time': c['time'],
                    'open': float(c['mid']['o']),
                    'high': float(c['mid']['h']),
                    'low': float(c['mid']['l']),
                    'close': float(c['mid']['c']),
                    'volume': int(c.get('volume', 0)),
                } for c in raw_candles if c.get('complete', True)]
        except Exception as e:
            logger.warning(f"OANDA candle fetch failed: {e}")
    
    if candles and len(candles) >= 100:
        try:
            # Compute all indicators
            import pandas as pd
            df = pd.DataFrame(candles)
            df = compute_all(df)
            latest = df.iloc[-1]
            
            # Market picture
            mkt_picture = generate_market_picture(pair, candles)
            
            # Market story (thesis)
            story = read_market_story(pair, candles, mkt_picture)
            
            # Sniper score
            params = TF_PARAMS.get("M15", TF_PARAMS.get("H1", {}))
            bull_score, bear_score = score_v4(latest, params)
            
            # Setup classifier
            try:
                _candle_patterns = latest.get('candle_pattern', latest.get('entry_candle_pattern', ''))
                classified = classify_setups(latest, _candle_patterns if _candle_patterns else '')
            except Exception:
                classified = []
            
            # EMA state
            ema = mkt_picture.get('ema', {})
            
            analysis['market_state'] = {
                'rsi': float(latest.get('rsi', latest.get('RSI', 0))),
                'stoch_k': float(latest.get('stoch_k', latest.get('STOCH_k', 0))),
                'adx': float(latest.get('adx', latest.get('ADX', 0))),
                'bb_width': float(latest.get('bb_width', 0)) if 'bb_width' in latest else None,
                'fan_state': ema.get('fan_state'),
                'fan_direction': ema.get('fan_direction'),
                'ema_separation': ema.get('separation_pct'),
                'velocity': ema.get('velocity'),
                'trend_health': mkt_picture.get('trend_health'),
                'v4_bull': bull_score,
                'v4_bear': bear_score,
                'v4_score': max(bull_score, bear_score),
                'classified_setups': [c.get('setup', '') for c in (classified or [])],
            }
            
            # ── Step 3: Check thesis match ──
            if story and story.get('has_opportunity'):
                thesis_dir = story.get('direction', 'none')
                thesis_score = story.get('opportunity_score', 0)
                thesis_type = story.get('entry_type', 'unknown')
                
                if thesis_dir == direction or (
                    (thesis_dir == 'buy' and direction in ('buy', 'bull', 'bullish')) or
                    (thesis_dir == 'sell' and direction in ('sell', 'bear', 'bearish'))
                ):
                    analysis['matched_systems'].append({
                        'system': 'thesis',
                        'score': thesis_score,
                        'type': thesis_type,
                        'direction': thesis_dir,
                    })
            
            # ── Step 4: Check sniper match ──
            sniper_dir = 'buy' if bull_score > bear_score else 'sell'
            sniper_score = max(bull_score, bear_score)
            threshold = params.get('threshold', 12)
            
            if sniper_score >= threshold:
                analysis['matched_systems'].append({
                    'system': 'sniper_v4',
                    'score': sniper_score,
                    'direction': sniper_dir,
                    'matches_trade': sniper_dir == direction,
                })
            
            # ── Step 5: Check classified setup match ──
            if classified:
                for c in classified:
                    if c.get('direction', '') == direction:
                        analysis['matched_systems'].append({
                            'system': 'classifier',
                            'setup': c.get('setup', ''),
                            'name': c.get('name', ''),
                            'direction': c.get('direction', ''),
                        })
            
            # ── Step 6: Check backtested data for similar conditions ──
            if get_db:
                try:
                    with get_db() as conn:
                        # Find similar trades in backtest data
                        rsi = analysis['market_state']['rsi']
                        stk = analysis['market_state']['stoch_k']
                        adx_val = analysis['market_state']['adx']
                        
                        similar = conn.execute('''
                            SELECT COUNT(*) as n,
                                   ROUND(SUM(CASE WHEN result='win' THEN 1.0 ELSE 0 END)/COUNT(*)*100, 1) as wr,
                                   ROUND(AVG(pips), 2) as avg_pips
                            FROM backtest_trades
                            WHERE pair = ?
                            AND direction = ?
                            AND rsi BETWEEN ? AND ?
                            AND stoch_k BETWEEN ? AND ?
                            AND adx BETWEEN ? AND ?
                        ''', (pair, direction,
                              rsi - 5, rsi + 5,
                              stk - 10, stk + 10,
                              adx_val - 5, adx_val + 5)).fetchone()
                        
                        if similar and similar[0] > 0:
                            analysis['matched_systems'].append({
                                'system': 'backtest_similar',
                                'trades': similar[0],
                                'win_rate': similar[1],
                                'avg_pips': similar[2],
                            })
                except Exception as e:
                    logger.warning(f"Backtest lookup failed: {e}")
        
        except Exception as e:
            logger.warning(f"Indicator computation failed: {e}")
            import traceback
            traceback.print_exc()
    
    # ── Step 7: Generate recommendation ──
    matched_names = [m['system'] for m in analysis['matched_systems']]
    
    if not analysis['matched_systems']:
        analysis['recommendation'] = 'NEW_PATTERN'
        analysis['recommendation_detail'] = (
            f"No existing system detected this trade. "
            f"Market state: RSI={analysis['market_state'].get('rsi', '?')}, "
            f"StochK={analysis['market_state'].get('stoch_k', '?')}, "
            f"fan={analysis['market_state'].get('fan_state', '?')} "
            f"{analysis['market_state'].get('fan_direction', '?')}. "
            f"Recording as candidate pattern."
        )
    elif 'scout' not in matched_names and ('thesis' in matched_names or 'sniper_v4' in matched_names):
        analysis['recommendation'] = 'SCOUT_GAP'
        analysis['recommendation_detail'] = (
            f"Thesis/sniper detected conditions but scout didn't fire an alert. "
            f"Check scout thresholds or scanning frequency."
        )
    else:
        analysis['recommendation'] = 'COVERED'
        analysis['recommendation_detail'] = f"Matched by: {', '.join(matched_names)}"
    
    return analysis


def record_unmatched_trade(analysis: Dict) -> Optional[str]:
    """
    Record an unmatched manual trade as a new pattern candidate.
    
    Returns pattern_id if recorded, None if already covered.
    """
    if analysis.get('recommendation') == 'COVERED':
        return None
    
    data = _load_patterns()
    
    # Create pattern entry
    pattern_id = f"USR_{len(data['patterns']) + 1:04d}"
    
    pattern = {
        'id': pattern_id,
        'pair': analysis['pair'],
        'direction': analysis['direction'],
        'entry_time': analysis['entry_time'],
        'pips': analysis['pips'],
        'result': analysis['result'],
        'market_state': analysis['market_state'],
        'matched_systems': analysis['matched_systems'],
        'recommendation': analysis['recommendation'],
        'occurrences': 1,
        'wins': 1 if analysis['result'] == 'win' else 0,
        'losses': 1 if analysis['result'] == 'loss' else 0,
        'total_pips': analysis['pips'],
        'created': datetime.now().isoformat(),
        'promoted_to_playbook': False,
    }
    
    # Check for similar existing patterns
    for existing in data['patterns']:
        if _patterns_similar(existing, pattern):
            # Update existing pattern
            existing['occurrences'] += 1
            if analysis['result'] == 'win':
                existing['wins'] += 1
            else:
                existing['losses'] += 1
            existing['total_pips'] += analysis['pips']
            
            # Check if ready for promotion
            if _ready_for_promotion(existing):
                existing['promoted_to_playbook'] = True
                _promote_to_playbook(existing)
                logger.info(f"🎓 Pattern {existing['id']} promoted to playbook! "
                           f"WR={existing['wins']/existing['occurrences']*100:.0f}% "
                           f"({existing['occurrences']} trades)")
            
            _save_patterns(data)
            return existing['id']
    
    # New pattern
    data['patterns'].append(pattern)
    _save_patterns(data)
    logger.info(f"📝 New manual trade pattern recorded: {pattern_id} "
               f"{pattern['pair']} {pattern['direction']} {pattern['result']} "
               f"{pattern['pips']:+.1f}p")
    
    return pattern_id


def _patterns_similar(existing: Dict, new: Dict) -> bool:
    """Check if two patterns are similar enough to merge."""
    if existing['pair'] != new['pair']:
        return False
    if existing['direction'] != new['direction']:
        return False
    
    # Compare market state
    e_ms = existing.get('market_state', {})
    n_ms = new.get('market_state', {})
    
    # RSI within 10 points
    e_rsi = e_ms.get('rsi', 0)
    n_rsi = n_ms.get('rsi', 0)
    if abs(e_rsi - n_rsi) > 15:
        return False
    
    # Same fan state category
    e_fan = e_ms.get('fan_state', '')
    n_fan = n_ms.get('fan_state', '')
    squeeze_states = {'contracting', 'stable', 'just_crossed'}
    expansion_states = {'expanding', 'accelerating'}
    
    e_phase = 'squeeze' if e_fan in squeeze_states else 'expansion' if e_fan in expansion_states else 'other'
    n_phase = 'squeeze' if n_fan in squeeze_states else 'expansion' if n_fan in expansion_states else 'other'
    
    if e_phase != n_phase:
        return False
    
    return True


def _ready_for_promotion(pattern: Dict) -> bool:
    """Check if a pattern has enough evidence for playbook promotion."""
    if pattern.get('promoted_to_playbook'):
        return False
    if pattern['occurrences'] < 3:
        return False
    wr = pattern['wins'] / pattern['occurrences']
    if wr < 0.65:
        return False
    if pattern['total_pips'] <= 0:
        return False
    return True


def _promote_to_playbook(pattern: Dict):
    """Add a proven pattern to the thesis elite playbook."""
    playbook_path = os.path.join(SOURCE_DIR, '..', 'Config', 'thesis_elite_playbook.json')
    
    try:
        with open(playbook_path) as f:
            playbook = json.load(f)
    except Exception:
        playbook = []
    
    ms = pattern.get('market_state', {})
    
    new_entry = {
        'pair': pattern['pair'],
        'direction': pattern['direction'],
        'source': 'manual_trade_learner',
        'pattern_id': pattern['id'],
        'trades': pattern['occurrences'],
        'win_rate': round(pattern['wins'] / pattern['occurrences'] * 100, 1),
        'total_pips': round(pattern['total_pips'], 1),
        'avg_pips': round(pattern['total_pips'] / pattern['occurrences'], 2),
        'fan_state': ms.get('fan_state', 'unknown'),
        'rsi_range': [round(ms.get('rsi', 50) - 10, 1), round(ms.get('rsi', 50) + 10, 1)],
        'adx_range': [round(ms.get('adx', 25) - 5, 1), round(ms.get('adx', 25) + 5, 1)],
        'promoted_at': datetime.now().isoformat(),
    }
    
    playbook.append(new_entry)
    
    with open(playbook_path, 'w') as f:
        json.dump(playbook, f, indent=2)
    
    logger.info(f"✅ Added {pattern['id']} to thesis_elite_playbook.json")


def process_closed_trade(trade: Dict) -> Dict:
    """
    Main entry point — called when a manual trade closes.
    Analyzes, records if unmatched, returns full analysis.
    Writes result to manual_trade_analysis table.
    """
    analysis = analyze_manual_trade(trade)

    if analysis['recommendation'] in ('NEW_PATTERN', 'SCOUT_GAP'):
        pattern_id = record_unmatched_trade(analysis)
        analysis['pattern_id'] = pattern_id

    # Always write to manual_trade_analysis DB table
    try:
        if get_db:
            with get_db() as conn:
                from datetime import datetime as _dt
                now = _dt.utcnow().isoformat()
                conn.execute('''
                    INSERT OR IGNORE INTO manual_trade_analysis
                    (trade_id, pair, direction, entry_time, exit_time, pips, result,
                     entry_price, setup_type, fan_state, fan_direction, rsi, stoch_k,
                     bb_position, pattern_matched, match_score, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    trade.get('trade_id') or trade.get('pair', '') + '_' + (trade.get('entry_time', '')[:16]),
                    trade.get('pair', ''),
                    trade.get('direction', ''),
                    trade.get('entry_time', ''),
                    trade.get('exit_time', ''),
                    trade.get('pips', 0),
                    trade.get('result', 'unknown'),
                    trade.get('entry_price'),
                    analysis.get('setup_type', ''),
                    analysis.get('fan_state', ''),
                    analysis.get('fan_direction', ''),
                    analysis.get('rsi'),
                    analysis.get('stoch_k'),
                    analysis.get('bb_position', ''),
                    analysis.get('pattern_matched', ''),
                    analysis.get('match_score', 0),
                    analysis.get('recommendation', ''),
                    now, now
                ))
                conn.commit()
    except Exception as _db_err:
        # 2026-04-24: upgraded — silent = learner rows lost, manual trade
        # analysis drifts out of sync with actual closed trades.
        logger.warning("manual_trade_analysis DB write FAILED: %s: %s (learner row lost)",
                       type(_db_err).__name__, _db_err)

    return analysis


def process_all_unmatched(hours_back: int = 72) -> List[Dict]:
    """
    Batch process: find all manual trades in last N hours that scout missed,
    analyze and record them.
    """
    if not get_db:
        logger.error("No database connection available")
        return []
    
    results = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime('%Y-%m-%dT%H:%M')
    
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        
        trades = conn.execute('''
            SELECT trade_id, pair, direction, entry_time, exit_time, result, pips,
                   entry_price, source, setup
            FROM live_trades
            WHERE entry_time > ? AND source = 'manual'
            ORDER BY entry_time
        ''', (cutoff,)).fetchall()
        
        for t in trades:
            pair = t['pair']
            direction = t['direction'] or ''
            entry_time = t['entry_time'] or ''
            scout_dir = 'BULL' if direction.lower() in ('buy', 'bull', 'bullish') else 'BEAR'
            
            # Check if scout had a finding
            findings = conn.execute('''
                SELECT id FROM scout_findings
                WHERE pair = ? AND direction = ?
                AND timestamp BETWEEN datetime(?, "-60 minutes") AND ?
                LIMIT 1
            ''', (pair, scout_dir, entry_time, entry_time)).fetchall()
            
            if findings:
                continue  # Scout covered this one
            
            trade_dict = {
                'pair': pair,
                'direction': direction.lower() if direction else 'buy',
                'entry_time': entry_time,
                'exit_time': t['exit_time'],
                'pips': t['pips'] or 0,
                'result': t['result'] or 'unknown',
                'entry_price': t['entry_price'],
            }
            
            analysis = process_closed_trade(trade_dict)
            results.append(analysis)
            
            icon = '✅' if t['result'] == 'win' else '❌'
            rec = analysis['recommendation']
            systems = [m['system'] for m in analysis['matched_systems']]
            print(f"  {icon} {pair:12s} {direction:5s} {t['pips'] or 0:+7.1f}p  "
                  f"{rec:15s} matched={systems or 'none'}")
    
    return results


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    print("=== Processing unmatched manual trades (72h) ===\n")
    results = process_all_unmatched(72)
    
    print(f"\n{'='*60}")
    print(f"Processed: {len(results)} unmatched trades")
    
    new_patterns = sum(1 for r in results if r['recommendation'] == 'NEW_PATTERN')
    scout_gaps = sum(1 for r in results if r['recommendation'] == 'SCOUT_GAP')
    print(f"  New patterns: {new_patterns}")
    print(f"  Scout gaps:   {scout_gaps}")
    
    data = _load_patterns()
    print(f"\nTotal recorded patterns: {len(data['patterns'])}")
    promoted = sum(1 for p in data['patterns'] if p.get('promoted_to_playbook'))
    print(f"Promoted to playbook:   {promoted}")
