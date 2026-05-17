#!/usr/bin/env python3
"""
One-time backfill script for reporter/logging agent issues.

After fixing the pipeline, backfills historical data:
1. Matches live_trades to trade_decisions by pair + time proximity (within 60 seconds)
2. Matches live_trades to scout_findings by pair + time proximity
3. Fills outcome + outcome_pips in trade_decisions  
4. Fills trade_id, outcome, pips_result in scout_findings

PROBLEM 2 & 5 FIX: Historical backfill for existing data
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any
from db_connection import get_db, DB_PATH

logger = logging.getLogger(__name__)

def run_trade_decisions_backfill():
    """Match live_trades to trade_decisions by pair + time proximity."""
    print("=" * 60)
    print("TRADE DECISIONS BACKFILL")
    print("=" * 60)
    
    with get_db() as conn:
        # Get unmatched trade_decisions (those with live_trade_id but no outcome)
        unmatched_decisions = conn.execute("""
            SELECT decision_id, pair, created_at, live_trade_id, direction
            FROM trade_decisions 
            WHERE live_trade_id IS NOT NULL AND outcome IS NULL
            ORDER BY created_at DESC
        """).fetchall()
        
        print(f"Found {len(unmatched_decisions)} trade decisions with missing outcomes")
        
        if not unmatched_decisions:
            print("No unmatched decisions found.")
            return
        
        # Get all live_trades with outcomes
        live_trades = conn.execute("""
            SELECT trade_id, pair, entry_time, exit_time, pips, direction
            FROM live_trades 
            WHERE exit_time IS NOT NULL AND pips IS NOT NULL
            ORDER BY entry_time DESC
        """).fetchall()
        
        print(f"Found {len(live_trades)} completed live trades")
        
        matches = 0
        
        for decision in unmatched_decisions:
            # Parse decision time, removing timezone info for comparison
            decision_time_str = decision['created_at'].replace('Z', '').replace('+00:00', '')
            decision_time = datetime.fromisoformat(decision_time_str)
            decision_pair = decision['pair']
            
            best_match = None
            best_time_diff = float('inf')
            
            # Find the closest live_trade by pair and time
            for trade in live_trades:
                if trade['pair'] != decision_pair:
                    continue
                    
                # Parse trade time, removing timezone info for comparison
                trade_time_str = trade['entry_time'].replace('Z', '').replace('+00:00', '')
                trade_time = datetime.fromisoformat(trade_time_str)
                time_diff = abs((trade_time - decision_time).total_seconds())
                
                # Only consider trades within 60 seconds
                if time_diff <= 60 and time_diff < best_time_diff:
                    best_match = trade
                    best_time_diff = time_diff
            
            if best_match:
                # Update the decision with outcome
                outcome = 'win' if best_match['pips'] > 0 else 'loss'
                conn.execute("""
                    UPDATE trade_decisions 
                    SET outcome = ?, outcome_pips = ?
                    WHERE decision_id = ?
                """, (outcome, round(best_match['pips'], 1), decision['decision_id']))
                
                matches += 1
                print(f"Matched decision #{decision['decision_id']} ({decision_pair}) to trade #{best_match['trade_id']} - {outcome} ({best_match['pips']:.1f} pips, {best_time_diff:.0f}s apart)")
        
        conn.commit()
        print(f"Updated {matches} trade decisions with outcomes")


def run_scout_findings_backfill():
    """Match live_trades to scout_findings by pair + time proximity."""
    print("=" * 60)
    print("SCOUT FINDINGS BACKFILL")
    print("=" * 60)
    
    with get_db() as conn:
        # Get scout findings without trade linkage
        unlinked_findings = conn.execute("""
            SELECT id, pair, timestamp, direction, setup_name
            FROM scout_findings 
            WHERE trade_id IS NULL
            ORDER BY timestamp DESC
        """).fetchall()
        
        print(f"Found {len(unlinked_findings)} scout findings without trade linkage")
        
        if not unlinked_findings:
            print("No unlinked scout findings found.")
            return
        
        # Get all live_trades with outcomes
        live_trades = conn.execute("""
            SELECT trade_id, pair, entry_time, exit_time, pips, direction
            FROM live_trades 
            WHERE exit_time IS NOT NULL AND pips IS NOT NULL
            ORDER BY entry_time DESC
        """).fetchall()
        
        print(f"Found {len(live_trades)} completed live trades")
        
        matches = 0

        # Build an index of live_trades by pair for faster lookup
        trades_by_pair = {}
        for trade in live_trades:
            p = trade['pair']
            if p not in trades_by_pair:
                trades_by_pair[p] = []
            trades_by_pair[p].append(trade)

        # For each live_trade, find the most recent scout_finding (same pair+direction)
        # that fired within 4 hours BEFORE the trade's entry_time.
        # Scouts fire on a schedule (~15 min), so a 60-second window never matches.
        MAX_LOOKBACK_SECONDS = 4 * 3600  # 4 hours

        already_linked = set()  # prevent one scout matching multiple trades

        for trade in live_trades:
            trade_pair = trade['pair']
            trade_direction = trade['direction'].lower() if trade['direction'] else ''
            trade_time_str = trade['entry_time'].replace('Z', '').replace('+00:00', '')
            trade_time = datetime.fromisoformat(trade_time_str)

            best_finding = None
            best_time_diff = float('inf')

            for finding in unlinked_findings:
                if finding['id'] in already_linked:
                    continue
                if finding['pair'] != trade_pair:
                    continue

                # Normalize direction
                finding_direction = finding['direction'].lower() if finding['direction'] else ''
                direction_match = False
                if finding_direction.startswith('bull') and trade_direction in ['buy', 'long', 'bullish']:
                    direction_match = True
                elif finding_direction.startswith('bear') and trade_direction in ['sell', 'short', 'bearish']:
                    direction_match = True
                elif finding_direction == trade_direction:
                    direction_match = True

                if not direction_match:
                    continue

                finding_time_str = finding['timestamp'].replace('Z', '').replace('+00:00', '')
                finding_time = datetime.fromisoformat(finding_time_str)

                # Scout must have fired BEFORE or at trade entry (within 4h lookback)
                diff = (trade_time - finding_time).total_seconds()
                if 0 <= diff <= MAX_LOOKBACK_SECONDS and diff < best_time_diff:
                    best_finding = finding
                    best_time_diff = diff

            if best_finding:
                outcome = 'win' if trade['pips'] > 0 else 'loss'
                conn.execute("""
                    UPDATE scout_findings
                    SET trade_id = ?, outcome = ?, pips_result = ?
                    WHERE id = ?
                """, (str(trade['trade_id']), outcome, round(trade['pips'], 1), best_finding['id']))
                already_linked.add(best_finding['id'])
                matches += 1
                print(f"Matched finding #{best_finding['id']} ({trade_pair} {best_finding['direction']}) to trade #{trade['trade_id']} - {outcome} ({trade['pips']:.1f} pips, {best_time_diff/60:.0f}min before entry)")
        
        conn.commit()
        print(f"Updated {matches} scout findings with trade linkage")


def main():
    """Run all backfill operations."""
    print("Starting reporter/logging agent backfill...")
    print(f"Database: {DB_PATH}")
    print(f"Timestamp: {datetime.now().isoformat()}")
    
    try:
        run_trade_decisions_backfill()
        print()
        run_scout_findings_backfill()
        print()
        print("Backfill completed successfully!")
        
    except Exception as e:
        print(f"Backfill failed: {e}")
        logger.error(f"Backfill error: {e}")
        raise


if __name__ == "__main__":
    main()