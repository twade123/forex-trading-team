#!/usr/bin/env python3
"""
Enhance exit_learning table with partial TP and re-entry tracking columns.
Supports Tim's trading philosophy: partial profits + re-entry on trend continuation.

Usage:
    python3 "Forex Trading Team/Source/enhance_exit_learning_table.py"
"""

from db_connection import get_db, DB_PATH
import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def enhance_exit_learning_table():
    """Add columns for partial TP tracking and re-entry analysis."""
    
    if not os.path.exists(DB_PATH):
        logger.error("❌ Database not found: %s", DB_PATH)
        return False
    
    try:
        with get_db() as conn:
            # Check existing columns
            cursor = conn.execute("PRAGMA table_info(exit_learning)")
            existing_columns = {col[1] for col in cursor.fetchall()}
            
            # Add partial TP tracking columns if not present
            new_columns = [
                ("partial_tp_pips", "REAL", "Pip level where partial TP was taken (if any)"),
                ("partial_tp_taken", "INTEGER DEFAULT 0", "Whether partial TP was executed (1=yes, 0=no)"),
                ("remaining_position_pips", "REAL", "Final P&L pips for remaining position after partial TP"),
                ("re_entry_available", "INTEGER DEFAULT 0", "Whether trend continued after exit allowing re-entry (1=yes, 0=no)"),
                ("re_entry_window_pips", "REAL", "Additional pips available if re-entered within 5 candles"),
                ("optimal_exit_level", "REAL", "Retrospective optimal exit point in pips (MFE or before reversal)"),
                ("reversal_signal", "TEXT", "What signal indicated the trend was ending"),
                ("position_sizing_usd", "REAL", "Position size in USD for calculating actual profit per pip")
            ]
            
            columns_added = 0
            for col_name, col_def, col_desc in new_columns:
                if col_name not in existing_columns:
                    conn.execute(f"ALTER TABLE exit_learning ADD COLUMN {col_name} {col_def}")
                    logger.info("✅ Added column: %s - %s", col_name, col_desc)
                    columns_added += 1
                else:
                    logger.debug("Column %s already exists", col_name)
            
            # Add index for partial TP analysis
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_exit_learning_partial_tp 
            ON exit_learning(setup_name, pair, partial_tp_taken, user_id)
            """)
            
            # Add index for re-entry analysis
            conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_exit_learning_reentry 
            ON exit_learning(setup_name, pair, re_entry_available, user_id)
            """)
            
            logger.info("✅ Enhanced exit_learning table: %d new columns added", columns_added)
            if columns_added > 0:
                logger.info("✅ 2 new indexes created for partial TP and re-entry analysis")
            
            return True
            
    except Exception as e:
        logger.error("❌ Failed to enhance exit_learning table: %s", e)
        return False


if __name__ == "__main__":
    success = enhance_exit_learning_table()
    exit(0 if success else 1)