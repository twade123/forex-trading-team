#!/usr/bin/env python3
"""Close stale open trades that are actually closed in OANDA."""

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

# Stale trade IDs confirmed as closed by user
STALE_TRADES = [
    ("709", "NZD_USD", "sell", "2026-03-03T16:10:18.660464+00:00"),
    ("619", "EUR_AUD", "buy", "2026-03-03T13:41:19.333307+00:00"),
    ("617", "EUR_JPY", "buy", "2026-03-03T13:40:42.035039+00:00"),
    ("615", "EUR_JPY", "buy", "2026-03-03T13:39:49.899951+00:00"),
    ("613", "EUR_JPY", "buy", "2026-03-03T13:39:13.170519+00:00"),
]

DB_PATH = "~/jarvis/Database/v2/trading_forex.db"

def close_stale_trades():
    """Mark stale trades as closed with result='unknown' and pips=0."""
    # Use db_connection module for proper connection handling
    try:
        from db_connection import get_db
    except ImportError:
        # Fallback to direct connection if db_connection not available
        print("Using direct database connection...")
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.row_factory = sqlite3.Row
        use_context = False
    else:
        use_context = True

    now = datetime.now(timezone.utc).isoformat()
    closed_count = 0

    def do_close(conn):
        nonlocal closed_count
        max_retries = 5
        retry_delay = 0.5

        for trade_id, pair, direction, entry_time in STALE_TRADES:
            for attempt in range(max_retries):
                try:
                    # Check if trade exists and is still open
                    row = conn.execute("""
                        SELECT trade_id, result FROM live_trades
                        WHERE trade_id = ? AND result = 'open'
                    """, (trade_id,)).fetchone()

                    if row:
                        # Update to closed
                        conn.execute("""
                            UPDATE live_trades
                            SET result = 'unknown',
                                exit_time = ?,
                                pips = 0,
                                exit_reason = 'stale_trade_cleanup'
                            WHERE trade_id = ?
                        """, (now, trade_id))

                        closed_count += 1
                        print(f"✅ Closed stale trade {trade_id}: {pair} {direction} from {entry_time[:10]}")
                    else:
                        print(f"⚠️  Trade {trade_id} already closed or not found")
                    break  # Success, move to next trade
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < max_retries - 1:
                        print(f"⏳ Database locked, retrying in {retry_delay * (attempt + 1)}s...")
                        time.sleep(retry_delay * (attempt + 1))
                    else:
                        print(f"❌ Failed to close {trade_id} after {max_retries} attempts: {e}")
                        break

        conn.commit()

    if use_context:
        with get_db() as conn:
            do_close(conn)
    else:
        do_close(conn)
        conn.close()

    print(f"\n📊 Summary: Closed {closed_count} stale trades")
    return closed_count

if __name__ == "__main__":
    close_stale_trades()
