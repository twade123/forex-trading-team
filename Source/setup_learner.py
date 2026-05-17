"""
Setup Learner — Discovers and validates new trading setups from live signals.

When the scout finds a SNIPER_DIRECT (no matching S-code), this module:
1. Captures the indicator conditions at entry
2. Queries backtest data for similar conditions
3. If the conditions show 80%+ WR with 50+ trades, saves as a new setup candidate
4. Candidates can be promoted to the live playbook

This creates a growing library of validated setups.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from db_pool import get_trading_forex

logger = logging.getLogger(__name__)

CANDIDATES_PATH = os.path.join(os.path.dirname(__file__), "setup_candidates.json")


def _load_candidates() -> List[Dict]:
    """Load existing candidates from disk."""
    if os.path.exists(CANDIDATES_PATH):
        try:
            with open(CANDIDATES_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_candidates(candidates: List[Dict]):
    """Save candidates to disk."""
    try:
        with open(CANDIDATES_PATH, "w") as f:
            json.dump(candidates, f, indent=2, default=str)
    except Exception as e:
        logger.error("Failed to save candidates: %s", e)


def evaluate_conditions(pair: str, regime: str, indicators: Dict[str, Any],
                        direction: str, sniper_score: float = 0) -> Optional[Dict]:
    """
    Given a set of market conditions, query backtest data to see if similar
    conditions historically produce wins.
    
    Returns a candidate dict if conditions show 80%+ WR, else None.
    """
    rsi = indicators.get("rsi", 50)
    stoch_k = indicators.get("stoch_k", 50)
    adx = indicators.get("adx", 25)
    bb_width = indicators.get("bb_width", 0.005)

    # Define condition bands (±tolerance)
    rsi_lo = max(0, rsi - 10)
    rsi_hi = min(100, rsi + 10)
    stoch_lo = max(0, stoch_k - 15)
    stoch_hi = min(100, stoch_k + 15)
    adx_lo = max(0, adx - 5)
    adx_hi = adx + 5

    try:
        db = get_trading_forex()

        # Query: how do similar conditions perform?
        row = db.execute("""
            SELECT COUNT(*) as trades,
                   SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                   AVG(pips) as avg_pips
            FROM backtest_trades
            WHERE regime = ?
            AND direction = ?
            AND rsi BETWEEN ? AND ?
            AND stoch_k BETWEEN ? AND ?
            AND adx BETWEEN ? AND ?
            LIMIT 500000
        """, (regime, direction, rsi_lo, rsi_hi, stoch_lo, stoch_hi, adx_lo, adx_hi)).fetchone()
        
        trades = row[0] or 0
        wins = row[1] or 0
        avg_pips = row[2] or 0
        
        if trades < 30:
            logger.debug("Only %d trades for conditions — too few to validate", trades)
            return None
        
        win_rate = wins * 100.0 / trades
        
        result = {
            "pair": pair,
            "regime": regime,
            "direction": direction,
            "conditions": {
                "rsi_range": [rsi_lo, rsi_hi],
                "stoch_range": [stoch_lo, stoch_hi],
                "adx_range": [adx_lo, adx_hi],
                "bb_width": bb_width,
            },
            "actual_values": {
                "rsi": rsi, "stoch_k": stoch_k, "adx": adx, "bb_width": bb_width,
            },
            "backtest": {
                "trades": trades,
                "wins": wins,
                "win_rate": round(win_rate, 1),
                "avg_pips": round(avg_pips, 1),
            },
            "sniper_score": sniper_score,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "status": "validated" if win_rate >= 80 else "marginal" if win_rate >= 65 else "weak",
        }
        
        if win_rate >= 80 and trades >= 50:
            logger.info("[SETUP LEARNER] ✅ VALIDATED: %s %s in %s — %.1f%% WR across %d trades (+%.1f pips avg)",
                       pair, direction, regime, win_rate, trades, avg_pips)
            _add_candidate(result)
            return result
        elif win_rate >= 65:
            logger.info("[SETUP LEARNER] ⚠️ MARGINAL: %s %s in %s — %.1f%% WR across %d trades",
                       pair, direction, regime, win_rate, trades)
            return result
        else:
            logger.debug("[SETUP LEARNER] ❌ WEAK: %s %s in %s — %.1f%% WR across %d trades",
                        pair, direction, regime, win_rate, trades)
            return result
            
    except Exception as e:
        logger.error("Setup learner query failed: %s", e)
        return None


def _add_candidate(result: Dict):
    """Add a validated candidate to the candidates file."""
    candidates = _load_candidates()
    
    # Deduplicate: same pair+regime+direction+similar conditions = update, don't duplicate
    for i, c in enumerate(candidates):
        if (c.get("pair") == result["pair"] 
            and c.get("regime") == result["regime"]
            and c.get("direction") == result["direction"]):
            # Update existing
            candidates[i] = result
            _save_candidates(candidates)
            return
    
    candidates.append(result)
    _save_candidates(candidates)


def get_validated_candidates(min_wr: float = 80.0, min_trades: int = 50) -> List[Dict]:
    """Return all candidates that meet the validation threshold."""
    candidates = _load_candidates()
    return [c for c in candidates 
            if c.get("backtest", {}).get("win_rate", 0) >= min_wr
            and c.get("backtest", {}).get("trades", 0) >= min_trades]


def quick_backtest(pair: str, regime: str, direction: str, 
                   rsi: float, stoch_k: float, adx: float) -> Dict:
    """Quick backtest lookup for specific conditions. Returns stats dict."""
    indicators = {"rsi": rsi, "stoch_k": stoch_k, "adx": adx}
    result = evaluate_conditions(pair, regime, indicators, direction)
    if result:
        return result.get("backtest", {})
    return {"trades": 0, "wins": 0, "win_rate": 0, "avg_pips": 0}
