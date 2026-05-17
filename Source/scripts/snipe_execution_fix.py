#!/usr/bin/env python3
"""
CRITICAL FIX: Snipe Execution Bug

Problem: Triggered snipes only send notifications but never launch trading cycles.
Solution: Patch the watch manager to actually execute trades when snipes trigger.

This file provides the missing connection between triggered snipes and trading cycles.
"""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "agents.db"

def fix_triggered_snipes_execution():
    """
    CRITICAL FIX: Make triggered snipes actually execute trading cycles.
    
    This function should be called by the watch checking system to launch
    trading cycles for triggered snipes.
    """
    from Source.agents.trading_cycle import TradingCycle
    from Source.agents.team_setup import TradingTeamSetup
    from Source.agents.comment_protocol import CommentProtocol
    
    conn = sqlite3.connect(str(DB_PATH))
    
    # Get triggered snipes that haven't launched trading cycles
    triggered_snipes = conn.execute("""
        SELECT id, instrument, suggestion_type, raw_suggestion, 
               triggered_at, context, conditions
        FROM watch_suggestions
        WHERE status = 'triggered' 
        AND (trade_cycle_id IS NULL OR trade_cycle_id = '')
        ORDER BY triggered_at ASC
    """).fetchall()
    
    if not triggered_snipes:
        conn.close()
        return []
    
    logger.info(f"Found {len(triggered_snipes)} triggered snipes needing execution")
    
    executed_cycles = []
    
    for snipe in triggered_snipes:
        snipe_id, instrument, suggestion_type, raw_suggestion, triggered_at, context, conditions = snipe
        
        try:
            # Initialize trading system components
            team = TradingTeamSetup()
            protocol = CommentProtocol()
            cycle = TradingCycle(team, protocol)
            
            # Generate unique cycle ID
            cycle_id = f"snipe_{snipe_id}_{int(datetime.now().timestamp())}"
            
            logger.info(f"🚀 Launching trading cycle for snipe #{snipe_id} ({instrument})")
            
            # Run the trading cycle
            cycle_result = cycle.run_cycle(instrument, cycle_id=cycle_id)
            
            # Update snipe with cycle ID
            conn.execute("""
                UPDATE watch_suggestions 
                SET trade_cycle_id = ?, status = 'executing'
                WHERE id = ?
            """, (cycle_id, snipe_id))
            
            # Record in scout learning system if available
            try:
                from scout_learning_system import record_snipe_trigger, record_trade_execution
                record_snipe_trigger(snipe_id=snipe_id)
                
                if cycle_result.get('trade_executed'):
                    record_trade_execution(
                        cycle_result.get('trade_id', cycle_id),
                        cycle_result.get('entry_price', 0),
                        cycle_result.get('direction', 'unknown'),
                        snipe_id=snipe_id
                    )
            except ImportError:
                logger.debug("Scout learning system not available")
            
            executed_cycles.append({
                'snipe_id': snipe_id,
                'instrument': instrument, 
                'cycle_id': cycle_id,
                'cycle_result': cycle_result
            })
            
            logger.info(f"✅ Cycle {cycle_id} completed for {instrument}: {cycle_result.get('verdict', 'unknown')}")
            
        except Exception as e:
            logger.error(f"❌ Failed to execute trading cycle for snipe #{snipe_id}: {e}")
            
            # Mark snipe as failed
            conn.execute("""
                UPDATE watch_suggestions 
                SET status = 'execution_failed', trade_cycle_id = ?
                WHERE id = ?
            """, (f"failed_{cycle_id}", snipe_id))
    
    conn.commit()
    conn.close()
    
    return executed_cycles

def check_and_execute_triggered_snipes():
    """
    Enhanced version of check_active_watches that actually executes trades.
    
    This is the function that should be called every 5 minutes by the cron system.
    It combines the existing watch checking with the missing execution logic.
    """
    from Source.agents.watch_manager import check_active_watches
    
    # First run the existing check system
    triggered = check_active_watches()
    
    if not triggered:
        return []
    
    logger.info(f"Watch manager found {len(triggered)} newly triggered snipes")
    
    # Now execute trading cycles for the newly triggered snipes
    executed = fix_triggered_snipes_execution()
    
    # Send notifications (keep existing behavior)
    _send_snipe_notifications(triggered)
    
    return executed

def _send_snipe_notifications(triggered_snipes: List[Dict]):
    """Send notifications for triggered snipes (preserve existing behavior)."""
    try:
        # This preserves the existing notification system
        import sqlite3 as _sql3
        udb_path = Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "core.db"
        
        conn = _sql3.connect(str(udb_path))
        user_result = conn.execute("SELECT user_id FROM broker_credentials LIMIT 1").fetchone()
        conn.close()
        
        if not user_result:
            return
            
        user_id = user_result[0]
        
        # Add notifications to user state
        from Source.trading_api_routes import _get_user_team_state
        
        state = _get_user_team_state(user_id)
        
        for trigger in triggered_snipes:
            state["notifications"].append({
                "type": "snipe_triggered_and_executed",
                "instrument": trigger["instrument"],
                "watch_id": trigger.get("watch_id"),
                "conditions_met": trigger.get("conditions_met", []),
                "timestamp": datetime.now().isoformat(),
                "message": f"Snipe triggered and trading cycle launched for {trigger['instrument']}"
            })
            
        logger.info(f"Sent notifications for {len(triggered_snipes)} triggered snipes")
        
    except Exception as e:
        logger.warning(f"Failed to send snipe notifications: {e}")

def validate_snipe_execution_fix():
    """
    Validate that the execution fix is working by checking recent triggered snipes.
    """
    conn = sqlite3.connect(str(DB_PATH))
    
    # Check triggered snipes from last hour
    cutoff = (datetime.now(timezone.utc) - datetime.timedelta(hours=1)).isoformat()
    
    results = conn.execute("""
        SELECT id, instrument, status, triggered_at, trade_cycle_id
        FROM watch_suggestions
        WHERE triggered_at > ?
        ORDER BY triggered_at DESC
    """, (cutoff,)).fetchall()
    
    conn.close()
    
    status_summary = {}
    for row in results:
        snipe_id, instrument, status, triggered_at, cycle_id = row
        
        if status not in status_summary:
            status_summary[status] = []
        
        status_summary[status].append({
            'snipe_id': snipe_id,
            'instrument': instrument,
            'triggered_at': triggered_at,
            'has_cycle_id': bool(cycle_id)
        })
    
    print("=== SNIPE EXECUTION STATUS ===")
    for status, snipes in status_summary.items():
        print(f"{status}: {len(snipes)} snipes")
        for snipe in snipes[:3]:  # Show first 3
            cycle_status = "✅ Has cycle ID" if snipe['has_cycle_id'] else "❌ No cycle ID" 
            print(f"  #{snipe['snipe_id']} {snipe['instrument']} - {cycle_status}")
    
    # Check for the bug pattern
    triggered_without_cycles = sum(
        1 for snipes in status_summary.get('triggered', []) 
        if not snipes['has_cycle_id']
    )
    
    if triggered_without_cycles > 0:
        print(f"\n🚨 BUG DETECTED: {triggered_without_cycles} triggered snipes have no trading cycle!")
        print("Run fix_triggered_snipes_execution() to resolve.")
    else:
        print("\n✅ Execution fix appears to be working - all triggered snipes have cycle IDs")
    
    return status_summary

# Patch function to integrate with existing system
def patch_trading_api_routes():
    """
    Generate a patch for trading_api_routes.py to fix the execution bug.
    
    This shows the exact code changes needed in the existing file.
    """
    patch_code = '''
# PATCH FOR trading_api_routes.py around line 1679
# Replace the existing triggered snipe handling with this:

def _watch_checker_background():
    """Background thread that checks watches and EXECUTES triggered snipes."""
    import time as _time
    while True:
        try:
            cfg = _load_risk_config()
            interval = int(cfg.get("watch_check_interval_min", 5)) * 60
            _time.sleep(interval)
            
            # CRITICAL FIX: Use the enhanced checker that actually executes trades
            from Source.snipe_execution_fix import check_and_execute_triggered_snipes
            executed_cycles = check_and_execute_triggered_snipes()
            
            if executed_cycles:
                logger.info(f"Executed {len(executed_cycles)} trading cycles from triggered snipes")
                for cycle in executed_cycles:
                    logger.info(f"  {cycle['instrument']}: {cycle['cycle_id']} -> {cycle['cycle_result'].get('verdict', '?')}")
            
        except Exception as exc:
            logger.error(f"Watch checker failed: {exc}")
            _time.sleep(60)  # Wait 1 minute before retry
'''
    
    print("PATCH FOR trading_api_routes.py:")
    print(patch_code)
    
    return patch_code

if __name__ == "__main__":
    # Run validation
    print("Validating snipe execution system...")
    validate_snipe_execution_fix()
    
    # Show patch instructions
    print("\n" + "="*60)
    patch_trading_api_routes()
    
    # Initialize scout learning system
    try:
        from scout_learning_system import ensure_scout_learning_tables
        ensure_scout_learning_tables()
        print("\n✅ Scout learning system initialized")
    except Exception as e:
        print(f"\n❌ Scout learning system failed: {e}")