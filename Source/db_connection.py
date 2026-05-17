"""
Shared database connection helper for the trading bot.

RULES:
1. Always use WAL mode + busy_timeout
2. Short-lived connections — open, use, close
3. Use `with get_db()` context manager for auto-cleanup
4. Never store a connection as a class attribute or global

Usage:
    from db_connection import get_db, DB_PATH

    with get_db() as conn:
        conn.execute("INSERT INTO ...")
        # auto-commits and closes on exit
"""

import os
import sqlite3
import logging
from contextlib import contextmanager

logger = logging.getLogger("trading_bot.db")

# Canonical path to v2/trading_forex.db (migrated from trevor_database.db)
DB_PATH = os.path.realpath(os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "Database", "v2", "trading_forex.db"
)))

BOARDROOM_PATH = os.path.realpath(os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "Database", "v2", "workspaces.db"
)))


@contextmanager
def get_db(db_path=None, timeout=30, readonly=False):
    """Context manager for short-lived DB connections.
    
    Always sets WAL mode and busy_timeout. Auto-commits on success,
    rolls back on exception, always closes.
    
    Args:
        db_path: Path to database. Defaults to v2/trading_forex.db
        timeout: Busy timeout in seconds (default 30)
        readonly: If True, opens in read-only mode (URI)
    
    Usage:
        with get_db() as conn:
            conn.execute("SELECT ...")
    """
    path = db_path or DB_PATH
    
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
    else:
        conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)

    try:
        conn.row_factory = sqlite3.Row
        # FUSE safety: disable mmap for main DB reads. FUSE mmap is unreliable
        # under concurrent access and causes "disk I/O error" on VirtIO mounts.
        conn.execute("PRAGMA mmap_size=0")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(f"PRAGMA busy_timeout={timeout * 1000}")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def quick_read(query, params=(), db_path=None):
    """One-shot read query. Returns list of Row objects."""
    with get_db(db_path=db_path, readonly=True) as conn:
        return conn.execute(query, params).fetchall()


def quick_write(query, params=(), db_path=None):
    """One-shot write query. Returns lastrowid."""
    with get_db(db_path=db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(query, params)
        conn.execute("COMMIT")
        return cursor.lastrowid


def quick_write_many(queries, db_path=None):
    """Execute multiple write queries in a single transaction.

    Args:
        queries: List of (query, params) tuples
    """
    with get_db(db_path=db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for query, params in queries:
            conn.execute(query, params)
        conn.execute("COMMIT")
