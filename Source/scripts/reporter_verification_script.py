#!/usr/bin/env python3
"""
Verification script for reporter/logging agent fixes.

Shows the current state of:
- trade_decisions outcome fill rate  
- scout_findings with trade_id set
- trade_log.db exists and has tables
- health check direction normalization

VERIFICATION: Check all fixes are working
"""

import sqlite3
import os
import logging
from datetime import datetime
from db_connection import get_db, DB_PATH

logger = logging.getLogger(__name__)

def check_trade_decisions_outcomes():
    """Check trade_decisions outcome fill rate."""
    print("=" * 60)
    print("TRADE DECISIONS OUTCOME FILL RATE")
    print("=" * 60)
    
    with get_db() as conn:
        # Total decisions with live_trade_id
        total_with_trade_id = conn.execute("""
            SELECT COUNT(*) as count FROM trade_decisions 
            WHERE live_trade_id IS NOT NULL
        """).fetchone()['count']
        
        # Decisions with outcomes
        with_outcomes = conn.execute("""
            SELECT COUNT(*) as count FROM trade_decisions 
            WHERE live_trade_id IS NOT NULL AND outcome IS NOT NULL
        """).fetchone()['count']
        
        fill_rate = (with_outcomes / total_with_trade_id * 100) if total_with_trade_id > 0 else 0
        
        print(f"Trade decisions with live_trade_id: {total_with_trade_id}")
        print(f"Trade decisions with outcomes: {with_outcomes}")
        print(f"Fill rate: {fill_rate:.1f}%")
        
        if fill_rate < 50:
            print("❌ LOW FILL RATE - Problem 2 may not be fully fixed")
        else:
            print("✅ Good fill rate")
            
        # Show recent examples
        print("\nRecent examples:")
        examples = conn.execute("""
            SELECT decision_id, pair, live_trade_id, outcome, outcome_pips, created_at
            FROM trade_decisions 
            WHERE live_trade_id IS NOT NULL
            ORDER BY created_at DESC LIMIT 5
        """).fetchall()
        
        for ex in examples:
            outcome_str = f"{ex['outcome']} ({ex['outcome_pips']} pips)" if ex['outcome'] else "No outcome"
            print(f"  #{ex['decision_id']}: {ex['pair']} trade_id={ex['live_trade_id']} -> {outcome_str}")


def check_scout_findings_linkage():
    """Check scout_findings with trade_id set."""
    print("=" * 60)
    print("SCOUT FINDINGS TRADE LINKAGE")
    print("=" * 60)
    
    with get_db() as conn:
        # Total scout findings
        total_findings = conn.execute("""
            SELECT COUNT(*) as count FROM scout_findings
        """).fetchone()['count']
        
        # Findings with trade_id
        with_trade_id = conn.execute("""
            SELECT COUNT(*) as count FROM scout_findings 
            WHERE trade_id IS NOT NULL
        """).fetchone()['count']
        
        # Findings with outcomes
        with_outcomes = conn.execute("""
            SELECT COUNT(*) as count FROM scout_findings 
            WHERE outcome IS NOT NULL
        """).fetchone()['count']
        
        linkage_rate = (with_trade_id / total_findings * 100) if total_findings > 0 else 0
        outcome_rate = (with_outcomes / total_findings * 100) if total_findings > 0 else 0
        
        print(f"Total scout findings: {total_findings}")
        print(f"Scout findings with trade_id: {with_trade_id}")
        print(f"Scout findings with outcomes: {with_outcomes}")
        print(f"Trade linkage rate: {linkage_rate:.1f}%")
        print(f"Outcome fill rate: {outcome_rate:.1f}%")
        
        if linkage_rate == 0:
            print("❌ NO TRADE LINKAGE - Problem 1 may not be fully fixed")
        else:
            print("✅ Scout findings are being linked to trades")
            
        # Show recent examples
        print("\nRecent examples:")
        examples = conn.execute("""
            SELECT id, pair, setup_name, direction, trade_id, outcome, pips_result, timestamp
            FROM scout_findings 
            ORDER BY timestamp DESC LIMIT 5
        """).fetchall()
        
        for ex in examples:
            trade_str = f"trade_id={ex['trade_id']}" if ex['trade_id'] else "No trade link"
            outcome_str = f" -> {ex['outcome']} ({ex['pips_result']} pips)" if ex['outcome'] else ""
            print(f"  #{ex['id']}: {ex['pair']} {ex['setup_name']} {ex['direction']} | {trade_str}{outcome_str}")


def check_trade_log_db():
    """Check if trade_log.db exists and has required tables."""
    print("=" * 60)
    print("TRADE_LOG.DB STATUS")
    print("=" * 60)
    
    # Check if TradeLogger can initialize
    try:
        from trade_logger import TradeLogger
        logger_instance = TradeLogger()
        db_path = logger_instance._db_path
        
        print(f"TradeLogger initialized successfully")
        print(f"DB path: {db_path}")
        print(f"DB exists: {os.path.exists(db_path)}")
        
        if os.path.exists(db_path):
            # Check tables
            conn = sqlite3.connect(db_path)
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            table_names = [table[0] for table in tables]
            conn.close()
            
            required_tables = ['signal_log', 'validation_log', 'mcp_query_log']
            missing_tables = [t for t in required_tables if t not in table_names]
            
            print(f"Available tables: {', '.join(table_names)}")
            
            if missing_tables:
                print(f"❌ MISSING TABLES: {', '.join(missing_tables)}")
            else:
                print("✅ All required tables present")
        else:
            print("❌ DATABASE FILE MISSING")
            
    except Exception as e:
        print(f"❌ TRADE LOGGER ERROR: {e}")


def check_direction_normalization():
    """Test the health check direction normalization."""
    print("=" * 60)
    print("HEALTH CHECK DIRECTION NORMALIZATION")  
    print("=" * 60)
    
    try:
        from cycle_health_check import _normalize_direction
        
        # Test cases
        test_cases = [
            ("BEAR", "bearish"),
            ("bear", "bearish"),
            ("bearish", "bearish"),
            ("BULL", "bullish"), 
            ("bull", "bullish"),
            ("bullish", "bullish"),
            ("neutral", "neutral"),
            ("", ""),
            (None, "")
        ]
        
        all_passed = True
        
        for input_val, expected in test_cases:
            result = _normalize_direction(input_val)
            status = "✅" if result == expected else "❌"
            if result != expected:
                all_passed = False
            print(f"  {status} '{input_val}' -> '{result}' (expected '{expected}')")
        
        if all_passed:
            print("\n✅ Direction normalization working correctly")
        else:
            print("\n❌ Direction normalization has issues")
            
    except Exception as e:
        print(f"❌ DIRECTION NORMALIZATION ERROR: {e}")


def main():
    """Run all verification checks."""
    print("Reporter/Logging Agent Verification")
    print(f"Database: {DB_PATH}")
    print(f"Timestamp: {datetime.now().isoformat()}")
    print()
    
    try:
        check_trade_decisions_outcomes()
        print()
        check_scout_findings_linkage()
        print()
        check_trade_log_db()
        print()
        check_direction_normalization()
        print()
        print("Verification completed!")
        
    except Exception as e:
        print(f"Verification failed: {e}")
        logger.error(f"Verification error: {e}")
        raise


if __name__ == "__main__":
    main()