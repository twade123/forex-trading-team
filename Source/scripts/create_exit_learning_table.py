#!/usr/bin/env python3
"""
Create exit_learning table in v2/trading_forex.db.
Idempotent — safe to run multiple times.

Usage:
    python3 "Forex Trading Team/Source/create_exit_learning_table.py"
"""

from db_connection import get_db, DB_PATH
import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_exit_learning_table():
    """Create the exit_learning table and indexes."""
    
    if not os.path.exists(DB_PATH):
        logger.error("❌ Database not found: %s", DB_PATH)
        return False
    
    try:
        with get_db() as conn:
            # Create the exit_learning table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS exit_learning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                setup_name TEXT NOT NULL,
                pair TEXT NOT NULL,
                direction TEXT NOT NULL,
                regime TEXT,
                entry_type TEXT,
                entry_price REAL NOT NULL,
                initial_sl REAL,
                initial_tp REAL,
                initial_rr_target REAL,
                exit_price REAL NOT NULL,
                exit_reason TEXT NOT NULL,
                pnl_pips REAL,
                pnl_usd REAL,
                actual_rr REAL,
                duration_minutes INTEGER,
                max_favorable_excursion_pips REAL,
                max_adverse_excursion_pips REAL,
                mfe_time_minutes INTEGER,
                primary_exit_signal TEXT,
                threat_level_at_exit INTEGER,
                threat_zone_at_exit TEXT,
                rsi_at_exit REAL,
                stoch_at_exit REAL,
                bb_width_at_exit REAL,
                fan_state_at_exit TEXT,
                fan_direction_at_exit TEXT,
                velocity_at_exit REAL,
                trend_health_at_exit REAL,
                ema_sep_at_exit REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(trade_id, user_id)
            )
            """)
            
            # Create indexes
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_exit_learning_setup 
            ON exit_learning(setup_name, pair, regime, user_id)
            """)
            
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_exit_learning_signal 
            ON exit_learning(primary_exit_signal, user_id)
            """)
            
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_exit_learning_user_created 
            ON exit_learning(user_id, created_at)
            """)
            
            # Verify the table
            cursor = conn.execute("PRAGMA table_info(exit_learning)")
            columns = cursor.fetchall()
            
            cursor = conn.execute("SELECT COUNT(*) FROM exit_learning")
            row_count = cursor.fetchone()[0]
            
            logger.info("✅ exit_learning table: %d columns, %d rows", len(columns), row_count)
            logger.info("✅ 3 indexes created")
            logger.info("   Database: %s", DB_PATH)
            
            return True
            
    except Exception as e:
        logger.error("❌ Failed to create exit_learning table: %s", e)
        return False


if __name__ == "__main__":
    success = create_exit_learning_table()
    exit(0 if success else 1)