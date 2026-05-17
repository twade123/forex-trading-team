#!/usr/bin/env python3
"""
Setup Discovery Pipeline — Automated discovery of high-edge indicator combinations.

Scans the backtest_trades table (8.4M+ trades) for indicator combinations that
show consistent edge across multiple pairs and regimes.

Usage:
    # Standalone scan for new setups:
    python -m setup_discovery
    
    # On-demand from trade_scout:
    from setup_discovery import discover_from_conditions
    discover_from_conditions(indicators, regime, direction)
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

from db_pool import get_trading_forex

logger = logging.getLogger(__name__)

# ── Configuration ──
MIN_WIN_RATE = 70.0
MIN_TRADES = 1000
MIN_PAIRS_PASSING = 8      # out of 13
MIN_PAIR_WIN_RATE = 65.0
MAX_OVERLAP_WITH_EXISTING = 0.80

ALL_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_JPY", "EUR_AUD", "GBP_JPY",
    "USD_CHF", "NZD_USD", "EUR_GBP", "EUR_JPY", "AUD_USD", "USD_CAD", "EUR_CHF"
]
VALID_REGIMES = ['exhaustion', 'high_volatility', 'ranging', 'squeeze', 'strong_trend']

# Paths
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
CUSTOM_SETUPS_PATH = os.path.join(SOURCE_DIR, 'custom_setups.json')


def _get_db() -> sqlite3.Connection:
    """Get a pooled connection to the backtest database (trading_forex.db)."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    return conn  # pooled — do NOT close


def _load_custom_setups() -> Dict:
    """Load custom_setups.json."""
    if os.path.exists(CUSTOM_SETUPS_PATH):
        with open(CUSTOM_SETUPS_PATH, 'r') as f:
            return json.load(f)
    return {"metadata": {"version": 1}, "setups": []}


def _save_custom_setups(data: Dict):
    """Save custom_setups.json atomically."""
    data['metadata']['last_updated'] = datetime.now().isoformat()
    tmp = CUSTOM_SETUPS_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CUSTOM_SETUPS_PATH)
    logger.info("Saved custom_setups.json with %d setups", len(data['setups']))


def _next_setup_id(data: Dict) -> str:
    """Get next available S-number (S21+)."""
    existing = set()
    # S1-S20 are hardcoded
    existing.update(range(1, 21))
    for s in data.get('setups', []):
        sid = s.get('setup_id', '')
        if sid.startswith('S'):
            try:
                existing.add(int(sid[1:]))
            except ValueError:
                pass
    n = 21
    while n in existing:
        n += 1
    return f"S{n}"


# ── Candidate Definitions ──
# Each candidate is a dict with:
#   name, conditions (SQL WHERE fragments + param dict), direction, regimes_to_test

def _build_candidates() -> List[Dict]:
    """Build a focused list of indicator combination candidates to test.
    
    Strategy: test meaningful extremes and known-good patterns, not brute force.
    Each candidate maps to SQL WHERE conditions on backtest_trades columns.
    """
    candidates = []
    
    # ── Deep oversold buys (like S23) ──
    for rsi_max in [20, 25, 30]:
        for stoch_max in [15, 20, 25]:
            candidates.append({
                'name': f'Deep Oversold Buy (RSI<={rsi_max}, Stoch<={stoch_max})',
                'where': f'rsi <= ? AND stoch_k <= ? AND direction = ?',
                'params': [rsi_max, stoch_max, 'buy'],
                'direction': 'buy',
                'conditions': {'rsi': {'max': rsi_max}, 'stoch_k': {'max': stoch_max}},
            })
    
    # ── Deep overbought sells ──
    for rsi_min in [70, 75, 80]:
        for stoch_min in [75, 80, 85]:
            candidates.append({
                'name': f'Deep Overbought Sell (RSI>={rsi_min}, Stoch>={stoch_min})',
                'where': f'rsi >= ? AND stoch_k >= ? AND direction = ?',
                'params': [rsi_min, stoch_min, 'sell'],
                'direction': 'sell',
                'conditions': {'rsi': {'min': rsi_min}, 'stoch_k': {'min': stoch_min}},
            })
    
    # ── BB extremes + RSI ──
    for rsi_max in [30, 35]:
        candidates.append({
            'name': f'BB Lower + RSI<={rsi_max} Buy',
            'where': f'rsi <= ? AND bb_lower > 0 AND entry_price <= bb_lower AND direction = ?',
            'params': [rsi_max, 'buy'],
            'direction': 'buy',
            'conditions': {'rsi': {'max': rsi_max}, 'bb_position': 'at_lower'},
        })
    for rsi_min in [65, 70]:
        candidates.append({
            'name': f'BB Upper + RSI>={rsi_min} Sell',
            'where': f'rsi >= ? AND bb_upper > 0 AND entry_price >= bb_upper AND direction = ?',
            'params': [rsi_min, 'sell'],
            'direction': 'sell',
            'conditions': {'rsi': {'min': rsi_min}, 'bb_position': 'at_upper'},
        })
    
    # ── ADX trending + MACD confirmation ──
    for adx_min in [25, 30, 35]:
        candidates.append({
            'name': f'Strong Trend Buy (ADX>={adx_min}, MACD bull)',
            'where': f'adx >= ? AND macd_hist > 0 AND direction = ?',
            'params': [adx_min, 'buy'],
            'direction': 'buy',
            'conditions': {'adx': {'min': adx_min}, 'macd_hist': {'min': 0}},
        })
        candidates.append({
            'name': f'Strong Trend Sell (ADX>={adx_min}, MACD bear)',
            'where': f'adx >= ? AND macd_hist < 0 AND direction = ?',
            'params': [adx_min, 'sell'],
            'direction': 'sell',
            'conditions': {'adx': {'min': adx_min}, 'macd_hist': {'max': 0}},
        })
    
    # ── Stoch + BB squeeze ──
    for stoch_max in [20, 25]:
        candidates.append({
            'name': f'Squeeze + Stoch OS Buy (Stoch<={stoch_max}, BB narrow)',
            'where': f'stoch_k <= ? AND bb_width > 0 AND bb_width < 0.003 AND direction = ?',
            'params': [stoch_max, 'buy'],
            'direction': 'buy',
            'conditions': {'stoch_k': {'max': stoch_max}, 'bb_width': {'max': 0.003}},
        })
    for stoch_min in [75, 80]:
        candidates.append({
            'name': f'Squeeze + Stoch OB Sell (Stoch>={stoch_min}, BB narrow)',
            'where': f'stoch_k >= ? AND bb_width > 0 AND bb_width < 0.003 AND direction = ?',
            'params': [stoch_min, 'sell'],
            'direction': 'sell',
            'conditions': {'stoch_k': {'min': stoch_min}, 'bb_width': {'max': 0.003}},
        })
    
    # ── CCI extremes + RSI confirmation ──
    candidates.append({
        'name': 'CCI Deep Oversold Buy (CCI<-150, RSI<=35)',
        'where': 'cci < -150 AND rsi <= 35 AND direction = ?',
        'params': ['buy'],
        'direction': 'buy',
        'conditions': {'cci': {'max': -150}, 'rsi': {'max': 35}},
    })
    candidates.append({
        'name': 'CCI Deep Overbought Sell (CCI>150, RSI>=65)',
        'where': 'cci > 150 AND rsi >= 65 AND direction = ?',
        'params': ['sell'],
        'direction': 'sell',
        'conditions': {'cci': {'min': 150}, 'rsi': {'min': 65}},
    })
    
    # ── Candle pattern + indicator confluence ──
    for pattern in ['hammer', 'bullish_engulfing']:
        candidates.append({
            'name': f'{pattern.replace("_"," ").title()} + RSI<=35 Buy',
            'where': f"entry_candle_pattern = ? AND rsi <= 35 AND direction = ?",
            'params': [pattern, 'buy'],
            'direction': 'buy',
            'conditions': {'candle': pattern, 'rsi': {'max': 35}},
        })
    for pattern in ['shooting_star', 'bearish_engulfing']:
        candidates.append({
            'name': f'{pattern.replace("_"," ").title()} + RSI>=65 Sell',
            'where': f"entry_candle_pattern = ? AND rsi >= 65 AND direction = ?",
            'params': [pattern, 'sell'],
            'direction': 'sell',
            'conditions': {'candle': pattern, 'rsi': {'min': 65}},
        })
    
    # ── Triple confluence: RSI + Stoch + ADX ──
    for rsi_max in [25, 30]:
        for adx_range in [(15, 25), (10, 20)]:
            candidates.append({
                'name': f'Triple OS Buy (RSI<={rsi_max}, Stoch<=25, ADX {adx_range[0]}-{adx_range[1]})',
                'where': f'rsi <= ? AND stoch_k <= 25 AND adx >= ? AND adx <= ? AND direction = ?',
                'params': [rsi_max, adx_range[0], adx_range[1], 'buy'],
                'direction': 'buy',
                'conditions': {'rsi': {'max': rsi_max}, 'stoch_k': {'max': 25}, 'adx': {'min': adx_range[0], 'max': adx_range[1]}},
            })

    return candidates


def _test_candidate(conn: sqlite3.Connection, candidate: Dict) -> Optional[Dict]:
    """Test a candidate per-regime across all pairs. Returns stats or None if fails filters.
    
    The edge is regime-specific (e.g. S23 is 70% in ranging but 50% overall).
    So we test each regime separately and return the best regime combination.
    """
    where = candidate['where']
    params = candidate['params']
    
    # Test each regime separately — the edge is regime-specific
    best_result = None
    
    for regime in VALID_REGIMES:
        regime_where = f"{where} AND regime = ?"
        regime_params = list(params) + [regime]
        
        # Overall stats for this regime
        query = f"""
            SELECT COUNT(*) as trades,
                   SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins
            FROM backtest_trades
            WHERE {regime_where}
        """
        cur = conn.execute(query, regime_params)
        row = cur.fetchone()
        if not row or row['trades'] < MIN_TRADES:
            continue
        
        total_trades = row['trades']
        total_wins = row['wins']
        overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
        
        if overall_wr < MIN_WIN_RATE:
            continue
        
        # Per-pair validation for this regime
        pair_query = f"""
            SELECT pair,
                   COUNT(*) as trades,
                   SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as wins
            FROM backtest_trades
            WHERE {regime_where}
            GROUP BY pair
        """
        cur = conn.execute(pair_query, regime_params)
        pair_results = {}
        pairs_passing = 0
        top_pairs = []
        
        for prow in cur.fetchall():
            p = prow['pair']
            t = prow['trades']
            w = prow['wins']
            wr = (w / t * 100) if t > 0 else 0
            pair_results[p] = {'trades': t, 'wins': w, 'win_rate': round(wr, 1)}
            if wr >= MIN_PAIR_WIN_RATE and t >= 50:
                pairs_passing += 1
            top_pairs.append((p, wr, t))
        
        if pairs_passing < MIN_PAIRS_PASSING:
            continue
        
        top_pairs.sort(key=lambda x: x[1], reverse=True)
        
        # This regime passes! Track it
        if best_result is None or overall_wr > best_result['win_rate']:
            best_result = {
                'name': candidate['name'],
                'direction': candidate['direction'],
                'conditions': candidate['conditions'],
                'total_trades': total_trades,
                'total_wins': total_wins,
                'win_rate': round(overall_wr, 1),
                'pairs_passing': pairs_passing,
                'pair_results': pair_results,
                'top_pairs': [p[0] for p in top_pairs[:3]],
                'regime_results': {},
                'good_regimes': [],
            }
    
    if best_result is None:
        return None
    
    # Now collect ALL regimes that pass for the winning candidate
    for regime in VALID_REGIMES:
        regime_where = f"{where} AND regime = ?"
        regime_params = list(params) + [regime]
        cur = conn.execute(f"""
            SELECT COUNT(*) as t, SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) as w
            FROM backtest_trades WHERE {regime_where}
        """, regime_params)
        r = cur.fetchone()
        if r and r['t'] > 0:
            wr = r['w'] / r['t'] * 100
            best_result['regime_results'][regime] = {'trades': r['t'], 'win_rate': round(wr, 1)}
            if wr >= MIN_WIN_RATE and r['t'] >= 100:
                best_result['good_regimes'].append(regime)
    
    if not best_result['good_regimes']:
        best_result['good_regimes'] = ['ranging']
    
    return best_result


def _conditions_overlap(conds_a: Dict, conds_b: Dict) -> float:
    """Compute overlap ratio between two condition sets (0.0 - 1.0)."""
    keys_a = set(conds_a.keys())
    keys_b = set(conds_b.keys())
    if not keys_a or not keys_b:
        return 0.0
    
    shared = keys_a & keys_b
    total = keys_a | keys_b
    if not total:
        return 0.0
    
    overlap_count = 0
    for k in shared:
        va = conds_a[k]
        vb = conds_b[k]
        if isinstance(va, dict) and isinstance(vb, dict):
            # Range overlap check
            a_min = va.get('min', float('-inf'))
            a_max = va.get('max', float('inf'))
            b_min = vb.get('min', float('-inf'))
            b_max = vb.get('max', float('inf'))
            # Check if ranges overlap significantly
            overlap_start = max(a_min, b_min)
            overlap_end = min(a_max, b_max)
            if overlap_start <= overlap_end:
                overlap_count += 1
        elif va == vb:
            overlap_count += 1
    
    return overlap_count / len(total)


def _get_existing_conditions() -> List[Dict]:
    """Get conditions from hardcoded S1-S20 + custom setups for dedup."""
    existing = []
    
    # Hardcoded S1-S20 approximate conditions (for dedup purposes)
    hardcoded = {
        'S1': {'stoch_k': {'min': 80}, 'bb_position': 'at_upper'},
        'S2': {'rsi': {'min': 65}, 'macd_hist': {'max': 0}},
        'S4': {'stoch_k': {'min': 80}},
        'S9': {'rsi': {'min': 70}, 'adx': {'min': 25}},
        'S13': {'stoch_k': {'min': 80}, 'adx': {'max': 25}},
        'S14': {'cci': {'min': 100}},
        'S15': {'stoch_k': {'min': 75}, 'bb_position': 'at_upper'},
    }
    for sid, conds in hardcoded.items():
        existing.append({'setup_id': sid, 'conditions': conds})
    
    # Custom setups
    data = _load_custom_setups()
    for s in data.get('setups', []):
        if s.get('status') == 'active':
            existing.append({'setup_id': s['setup_id'], 'conditions': s['conditions']})
    
    return existing


def _is_duplicate(candidate_conditions: Dict, existing: List[Dict]) -> Tuple[bool, Optional[str]]:
    """Check if candidate overlaps too much with any existing setup."""
    for ex in existing:
        overlap = _conditions_overlap(candidate_conditions, ex['conditions'])
        if overlap >= MAX_OVERLAP_WITH_EXISTING:
            return True, ex['setup_id']
    return False, None


def scan_for_setups(verbose: bool = True) -> List[Dict]:
    """Run the full discovery pipeline. Returns list of newly registered setups."""
    logger.info("Starting setup discovery scan...")
    conn = _get_db()
    candidates = _build_candidates()
    logger.info("Testing %d candidate combinations...", len(candidates))
    
    existing = _get_existing_conditions()
    new_setups = []
    data = _load_custom_setups()
    
    passed = []
    for i, cand in enumerate(candidates):
        result = _test_candidate(conn, cand)
        if result:
            passed.append(result)
            if verbose:
                logger.info(
                    "  ✅ PASS: %s — %.1f%% WR, %d trades, %d/%d pairs",
                    result['name'], result['win_rate'], result['total_trades'],
                    result['pairs_passing'], len(ALL_PAIRS)
                )
    logger.info("%d candidates passed filters out of %d tested", len(passed), len(candidates))
    
    # Deduplicate and register
    for result in passed:
        is_dup, dup_of = _is_duplicate(result['conditions'], existing)
        if is_dup:
            if verbose:
                logger.info("  ⏭️  SKIP (duplicate of %s): %s", dup_of, result['name'])
            continue
        
        setup_id = _next_setup_id(data)
        new_setup = {
            'setup_id': setup_id,
            'name': result['name'],
            'conditions': result['conditions'],
            'regimes': result['good_regimes'],
            'direction': result['direction'],
            'backtest_stats': {
                'trades': result['total_trades'],
                'win_rate': result['win_rate'],
                'pairs_passing': result['pairs_passing'],
                'top_pairs': result['top_pairs'],
            },
            'description': f"Auto-discovered: {result['name']}. {result['win_rate']}% WR across {result['total_trades']} trades.",
            'discovered_date': datetime.now().strftime('%Y-%m-%d'),
            'status': 'active',
        }
        
        data['setups'].append(new_setup)
        existing.append({'setup_id': setup_id, 'conditions': result['conditions']})
        new_setups.append(new_setup)
        
        logger.info(
            "[SETUP DISCOVERED] %s \"%s\" added to playbook — %.1f%% WR across %d trades | Regimes: %s | Top pairs: %s",
            setup_id, result['name'], result['win_rate'], result['total_trades'],
            ', '.join(result['good_regimes']), ', '.join(result['top_pairs'])
        )
    
    if new_setups:
        _save_custom_setups(data)
        # Write notification file for OpenClaw pickup
        try:
            notif_dir = os.path.join(SOURCE_DIR, 'notifications')
            os.makedirs(notif_dir, exist_ok=True)
            lines = [f"🎯 {s['setup_id']} \"{s['name']}\" — {s['backtest_stats']['win_rate']}% WR, "
                     f"{s['backtest_stats']['trades']:,} trades, regimes: {', '.join(s.get('regimes', []))}"
                     for s in new_setups]
            msg = f"NEW SETUPS DISCOVERED ({len(new_setups)}):\n" + "\n".join(lines)
            with open(os.path.join(notif_dir, 'setup_discovered.txt'), 'w') as f:
                f.write(msg)
        except Exception:
            pass
    
    logger.info("Discovery complete: %d new setups registered", len(new_setups))
    return new_setups


def discover_from_conditions(indicators: Dict[str, Any], regime: str, direction: str) -> Optional[Dict]:
    """On-demand discovery triggered by trade_scout when no setup matches.
    
    Builds a candidate from current indicator values and tests it against backtest DB.
    Returns the new setup dict if registered, or None.
    """
    rsi = indicators.get('rsi', 50)
    stoch_k = indicators.get('stoch_k', 50)
    adx = indicators.get('adx', 25)
    bb_width = indicators.get('bb_width', 0)
    cci = indicators.get('cci', 0)
    macd_hist = indicators.get('macd_hist', 0)
    
    # Build conditions from current indicator extremes
    conditions = {}
    where_parts = [f'direction = ?']
    params = [direction]
    
    if direction == 'buy':
        if rsi <= 35:
            conditions['rsi'] = {'max': int(rsi) + 5}
            where_parts.append(f'rsi <= ?')
            params.append(int(rsi) + 5)
        if stoch_k <= 30:
            conditions['stoch_k'] = {'max': int(stoch_k) + 5}
            where_parts.append(f'stoch_k <= ?')
            params.append(int(stoch_k) + 5)
    else:
        if rsi >= 65:
            conditions['rsi'] = {'min': int(rsi) - 5}
            where_parts.append(f'rsi >= ?')
            params.append(int(rsi) - 5)
        if stoch_k >= 70:
            conditions['stoch_k'] = {'min': int(stoch_k) - 5}
            where_parts.append(f'stoch_k >= ?')
            params.append(int(stoch_k) - 5)
    
    if adx >= 25:
        conditions['adx'] = {'min': 25}
        where_parts.append('adx >= 25')
    elif adx <= 20:
        conditions['adx'] = {'max': 20}
        where_parts.append('adx <= 20')
    
    if cci < -100:
        conditions['cci'] = {'max': -100}
        where_parts.append('cci < -100')
    elif cci > 100:
        conditions['cci'] = {'min': 100}
        where_parts.append('cci > 100')
    
    # Need at least 2 indicator conditions
    if len(conditions) < 2:
        logger.debug("discover_from_conditions: insufficient extreme indicators (%d)", len(conditions))
        return None
    
    name = f"Auto {direction.title()} ({', '.join(f'{k}' for k in conditions)})"
    candidate = {
        'name': name,
        'where': ' AND '.join(where_parts),
        'params': params,
        'direction': direction,
        'conditions': conditions,
    }
    
    try:
        conn = _get_db()
        result = _test_candidate(conn, candidate)
    except Exception as e:
        logger.error("discover_from_conditions DB error: %s", e)
        return None
    
    if not result:
        logger.debug("discover_from_conditions: candidate didn't pass filters")
        return None
    
    # Dedup check
    existing = _get_existing_conditions()
    is_dup, dup_of = _is_duplicate(result['conditions'], existing)
    if is_dup:
        logger.debug("discover_from_conditions: duplicate of %s", dup_of)
        return None
    
    # Register
    data = _load_custom_setups()
    setup_id = _next_setup_id(data)
    new_setup = {
        'setup_id': setup_id,
        'name': result['name'],
        'conditions': result['conditions'],
        'regimes': result['good_regimes'],
        'direction': result['direction'],
        'backtest_stats': {
            'trades': result['total_trades'],
            'win_rate': result['win_rate'],
            'pairs_passing': result['pairs_passing'],
            'top_pairs': result['top_pairs'],
        },
        'description': f"On-demand discovery: {result['name']}. {result['win_rate']}% WR across {result['total_trades']} trades.",
        'discovered_date': datetime.now().strftime('%Y-%m-%d'),
        'status': 'active',
    }
    data['setups'].append(new_setup)
    _save_custom_setups(data)
    
    logger.info(
        "[SETUP DISCOVERED] %s \"%s\" added to playbook — %.1f%% WR across %d trades",
        setup_id, result['name'], result['win_rate'], result['total_trades']
    )
    return new_setup


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    results = scan_for_setups(verbose=True)
    if results:
        print(f"\n{'='*60}")
        print(f"DISCOVERY COMPLETE: {len(results)} new setups added")
        for s in results:
            print(f"  {s['setup_id']}: {s['name']} — {s['backtest_stats']['win_rate']}% WR, {s['backtest_stats']['trades']} trades")
    else:
        print("\nNo new setups discovered (all candidates either failed filters or were duplicates)")
