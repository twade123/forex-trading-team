#!/usr/bin/env python3
"""
Create intelligence_snapshots and intelligence_cache tables in v2/trading_forex.db.
Idempotent — safe to run multiple times.

Usage:
    source ~/myenv/bin/activate
    python3 "Forex Trading Team/Source/create_intelligence_tables.py"
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("~/jarvis/Database/v2/trading_forex.db")


def create_tables(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # ── intelligence_snapshots ──────────────────────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS intelligence_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        cycle_id TEXT,
        decision_id TEXT,
        trade_id TEXT,
        instrument TEXT NOT NULL,

        -- Macro data (Wolfram)
        base_currency TEXT,
        quote_currency TEXT,
        base_currency_rate REAL,
        quote_currency_rate REAL,
        rate_differential REAL,
        base_inflation REAL,
        quote_inflation REAL,
        base_unemployment REAL,
        quote_unemployment REAL,
        oil_price REAL,
        gold_price REAL,
        pair_1yr_min REAL,
        pair_1yr_max REAL,
        pair_1yr_avg REAL,
        pair_1yr_volatility REAL,
        pair_current_price REAL,
        pair_range_position TEXT,
        base_gdp_growth REAL,
        quote_gdp_growth REAL,
        base_trade_balance REAL,
        quote_trade_balance REAL,
        macro_bias TEXT,

        -- News data
        base_sentiment REAL,
        quote_sentiment REAL,
        net_sentiment REAL,
        high_impact_events TEXT,
        articles_analyzed INTEGER,
        news_key_finding TEXT,
        block_trading INTEGER DEFAULT 0,

        -- Weather data
        weather_checked INTEGER DEFAULT 0,
        weather_severity INTEGER DEFAULT 1,
        weather_locations TEXT,
        weather_summary TEXT,

        -- Statistics
        kelly_fraction REAL,
        half_kelly REAL,
        recommended_size_pct REAL,
        correlation_alerts TEXT,
        correlation_breakdown_detected INTEGER DEFAULT 0,
        drift_detected INTEGER DEFAULT 0,
        drift_z_score REAL,
        drift_setup TEXT,

        -- Verdict
        verdict TEXT NOT NULL,
        bias TEXT,
        confidence REAL,
        summary TEXT,

        -- Outcome (filled after trade closes)
        outcome TEXT,
        pips_result REAL,
        outcome_notes TEXT,

        -- Raw data
        full_report TEXT,
        wolfram_queries_used TEXT,
        news_queries_used TEXT,

        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    # ── intelligence_cache ──────────────────────────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS intelligence_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cache_key TEXT UNIQUE NOT NULL,
        category TEXT NOT NULL,
        instrument TEXT,
        data TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        query_used TEXT
    )
    """)

    # ── Indexes ─────────────────────────────────────────────────────
    indexes = [
        ("idx_intel_instrument",         "intelligence_snapshots", "instrument"),
        ("idx_intel_decision",           "intelligence_snapshots", "decision_id"),
        ("idx_intel_trade",              "intelligence_snapshots", "trade_id"),
        ("idx_intel_outcome",            "intelligence_snapshots", "outcome"),
        ("idx_intel_verdict",            "intelligence_snapshots", "verdict"),
        ("idx_intel_timestamp",          "intelligence_snapshots", "timestamp"),
        ("idx_intel_instrument_outcome", "intelligence_snapshots", "instrument, outcome"),
        ("idx_cache_key",               "intelligence_cache",     "cache_key"),
        ("idx_cache_expires",           "intelligence_cache",     "expires_at"),
        ("idx_cache_category",          "intelligence_cache",     "category"),
    ]

    for idx_name, table, cols in indexes:
        cur.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({cols})")

    conn.commit()

    # ── Verify ──────────────────────────────────────────────────────
    cur.execute("SELECT COUNT(*) FROM intelligence_snapshots")
    snap_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM intelligence_cache")
    cache_count = cur.fetchone()[0]

    cur.execute("PRAGMA table_info(intelligence_snapshots)")
    snap_cols = cur.fetchall()
    cur.execute("PRAGMA table_info(intelligence_cache)")
    cache_cols = cur.fetchall()

    print(f"✅ intelligence_snapshots: {len(snap_cols)} columns, {snap_count} rows")
    print(f"✅ intelligence_cache:     {len(cache_cols)} columns, {cache_count} rows")
    print(f"✅ 10 indexes created")
    print(f"   Database: {db_path}")

    conn.close()


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"❌ Database not found: {DB_PATH}")
        sys.exit(1)
    create_tables(DB_PATH)
