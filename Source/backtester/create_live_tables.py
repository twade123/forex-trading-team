#!/usr/bin/env python3
"""Phase 1: Create all 6 live pipeline tables in v2/trading_forex.db.

Tables created:
  1. live_trades — same schema as backtest_trades + live-specific fields
  2. trade_decisions — full audit trail of every trading decision
  3. news_events — news agent's event log
  4. weather_events — extreme weather events (sparse)
  5. wolfram_analyses — statistical validation log
  6. market_snapshots — periodic market state captures

Usage:
    cd ~/jarvis/Trading\ Bot
    source ~/myenv/bin/activate
    python -u -m Source.backtester.create_live_tables
"""

import os
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

JARVIS_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DB_PATH = JARVIS_ROOT / "Database" / "v2/trading_forex.db"


def create_tables(conn):
    cursor = conn.cursor()

    # ================================================================
    # TABLE 1: live_trades
    # Mirror of backtest_trades + live-specific fields
    # ================================================================
    print("📊 Creating live_trades...", end=" ", flush=True)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS live_trades (
            -- Identity
            trade_id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT 'paper',  -- paper / live
            account_id TEXT,
            oanda_trade_id TEXT,
            decision_id TEXT,  -- FK to trade_decisions

            -- Same fields as backtest_trades
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            setup TEXT NOT NULL,
            base_setup TEXT,
            rr_mult REAL,
            sl_mult REAL,
            direction TEXT NOT NULL,  -- buy / sell

            -- Timing
            entry_time TEXT NOT NULL,
            exit_time TEXT,
            session TEXT,  -- Asian / London / NY_Overlap / NY / Off_Hours

            -- Prices
            entry_price REAL NOT NULL,
            exit_price REAL,
            sl_price REAL NOT NULL,
            tp_price REAL NOT NULL,
            spread_at_entry REAL,  -- NEW: spread in pips when entered

            -- Result
            result TEXT,  -- win / loss / breakeven / open
            pips REAL,
            combined_pips REAL,
            risk_reward_actual REAL,
            exit_reason TEXT,  -- take_profit / stop_loss / breakeven / timeout / manual

            -- Market context at entry
            regime TEXT,
            h4_trend TEXT,
            h4_agrees TEXT,
            h4_info TEXT,

            -- Trade management
            sl_moved_to_be TEXT,
            be_candle REAL,
            partial_exit_hit TEXT,
            partial_exit_pips REAL,
            second_half_result TEXT,
            second_half_pips REAL,

            -- Daily pivot context
            nearest_daily_pivot TEXT,
            dist_to_daily_pivot_atr REAL,
            near_daily_resistance TEXT,
            near_daily_support TEXT,

            -- Loss streak
            loss_streak_at_entry INTEGER DEFAULT 0,
            max_loss_streak INTEGER DEFAULT 0,

            -- Indicator snapshot at entry
            adx REAL,
            adx_slope REAL,
            rsi REAL,
            macd_value REAL,
            macd_signal REAL,
            macd_hist REAL,
            stoch_k REAL,
            stoch_d REAL,
            cci REAL,
            bb_upper REAL,
            bb_mid REAL,
            bb_lower REAL,
            bb_width REAL,
            sma50 REAL,
            sma100 REAL,
            atr REAL,
            sar REAL,
            price_vs_sma50 TEXT,
            price_vs_sma100 TEXT,

            -- Candle context
            entry_candle_pattern TEXT,
            prev_3_candle_patterns TEXT,
            nearest_support REAL,
            nearest_resistance REAL,
            pivot_pp REAL,
            pivot_r1 REAL,
            pivot_s1 REAL,

            -- Excursion tracking
            max_favorable_pips REAL,
            max_adverse_pips REAL,
            candles_to_exit INTEGER,

            -- Signal info
            trigger_reason TEXT,
            confidence REAL,

            -- Confluence
            concurrent_setups TEXT,
            concurrent_directions TEXT,

            -- Timestamps
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    print("✅", flush=True)

    # ================================================================
    # TABLE 2: trade_decisions
    # Full audit trail — what each agent said, what validator decided
    # ================================================================
    print("📋 Creating trade_decisions...", end=" ", flush=True)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trade_decisions (
            decision_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),

            -- What's being evaluated
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            setup TEXT NOT NULL,
            direction TEXT NOT NULL,
            regime TEXT,

            -- Agent recommendations (JSON blobs)
            market_agent_data TEXT,      -- indicators, regime, setups firing
            news_agent_data TEXT,        -- headlines, sentiment, upcoming events
            weather_agent_data TEXT,     -- clear or warning + details
            wolfram_agent_data TEXT,     -- statistical validation, position size

            -- Data validator's analysis
            validator_verdict TEXT NOT NULL,  -- APPROVE / REJECT / REDUCE_SIZE / WAIT
            validator_confidence REAL,
            validator_reasoning TEXT,         -- human-readable explanation
            validator_db_evidence TEXT,       -- JSON: backtest stats looked up
            validator_loss_patterns TEXT,     -- JSON: what losses look like for this setup
            validator_confluence TEXT,        -- JSON: concurrent setup analysis

            -- Recommended trade parameters
            recommended_rr REAL,
            recommended_sl REAL,
            recommended_size REAL,       -- position size (lots)
            recommended_size_reason TEXT, -- why this size (Kelly, news reduction, etc.)

            -- Final action taken
            final_action TEXT NOT NULL,   -- EXECUTE / SKIP / REDUCE
            final_action_reason TEXT,     -- if different from validator (e.g., manual override)

            -- Outcome (filled after trade closes)
            live_trade_id TEXT,           -- FK to live_trades (NULL if skipped)
            outcome TEXT,                 -- win / loss / breakeven / NULL if skipped
            outcome_pips REAL,
            outcome_matched_prediction INTEGER,  -- 1 = validator was right, 0 = wrong

            -- Meta
            execution_time_ms INTEGER,   -- how long the full decision pipeline took
            created_at TEXT DEFAULT (datetime('now')),

            -- Market story at entry (JSON snapshot for revenue tracking + win snipes)
            market_story_snapshot TEXT,

            -- Multi-user isolation
            user_id INTEGER
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trade_decisions_user_id ON trade_decisions(user_id)")
    print("✅", flush=True)

    # ================================================================
    # TABLE 3: news_events
    # Everything the news agent finds
    # ================================================================
    print("📰 Creating news_events...", end=" ", flush=True)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS news_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),

            -- Classification
            category TEXT NOT NULL,       -- central_bank / employment / inflation / gdp / geopolitical / sentiment / other
            impact_level TEXT NOT NULL,    -- high / medium / low
            currencies_affected TEXT,      -- CSV: "EUR,USD"
            pairs_affected TEXT,           -- CSV: "EUR_USD,GBP_USD"

            -- Content
            headline TEXT NOT NULL,
            summary TEXT,
            source TEXT,
            url TEXT,

            -- Analysis
            sentiment_score REAL,          -- -1.0 (bearish) to +1.0 (bullish)
            direction_bias TEXT,           -- e.g., "USD_bearish", "EUR_bullish"

            -- Calendar events
            event_time TEXT,               -- when the actual event occurs (for scheduled events)
            is_upcoming INTEGER DEFAULT 0, -- 1 = future event from economic calendar
            is_active INTEGER DEFAULT 1,   -- 0 = event has passed / no longer relevant

            -- Link to decisions
            decision_ids TEXT              -- CSV of decision_ids where this event was considered
        )
    """)
    print("✅", flush=True)

    # ================================================================
    # TABLE 4: weather_events
    # Only extreme events that could affect markets
    # ================================================================
    print("🌪️  Creating weather_events...", end=" ", flush=True)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weather_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),

            -- Location
            region TEXT NOT NULL,
            country TEXT NOT NULL,

            -- Event details
            event_type TEXT NOT NULL,      -- hurricane / drought / flood / earthquake / extreme_temp / wildfire
            severity INTEGER NOT NULL,     -- 1-5 (5 = catastrophic)
            description TEXT,

            -- Market impact assessment
            currencies_affected TEXT,       -- CSV: "AUD,NZD"
            pairs_affected TEXT,            -- CSV: "AUD_USD,NZD_USD"
            estimated_impact TEXT,          -- text description of expected market impact
            impact_direction TEXT,          -- e.g., "AUD_bearish"

            -- Status
            is_active INTEGER DEFAULT 1,   -- 0 = resolved
            resolved_at TEXT,

            -- Link to decisions
            decision_ids TEXT
        )
    """)
    print("✅", flush=True)

    # ================================================================
    # TABLE 5: wolfram_analyses
    # Statistical validations and calculations
    # ================================================================
    print("🔬 Creating wolfram_analyses...", end=" ", flush=True)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS wolfram_analyses (
            analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),

            -- Query details
            query_type TEXT NOT NULL,       -- significance_test / correlation / position_size / seasonal / regression / kelly
            query_text TEXT NOT NULL,        -- what was sent to Wolfram
            pair TEXT,                       -- if pair-specific

            -- Results
            result_summary TEXT NOT NULL,    -- human-readable result
            result_data TEXT,               -- JSON: full parsed response
            result_value REAL,              -- key numeric result (e.g., p-value, correlation, kelly fraction)

            -- Link to decisions
            decision_id TEXT                -- FK to trade_decisions
        )
    """)
    print("✅", flush=True)

    # ================================================================
    # TABLE 6: market_snapshots
    # Periodic market state captures (every candle close or on-demand)
    # ================================================================
    print("📸 Creating market_snapshots...", end=" ", flush=True)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            candle_time TEXT NOT NULL,      -- the candle close time this snapshot represents

            -- Pair info
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,

            -- Current price
            bid REAL,
            ask REAL,
            spread_pips REAL,

            -- Regime
            regime TEXT,

            -- Indicators
            adx REAL,
            adx_slope REAL,
            rsi REAL,
            macd_value REAL,
            macd_signal REAL,
            macd_hist REAL,
            stoch_k REAL,
            stoch_d REAL,
            cci REAL,
            bb_upper REAL,
            bb_mid REAL,
            bb_lower REAL,
            bb_width REAL,
            sma50 REAL,
            sma100 REAL,
            atr REAL,
            sar REAL,

            -- Higher TF context
            h4_trend TEXT,
            h4_agrees TEXT,

            -- Active signals
            active_setups_firing TEXT,      -- CSV: "S13,S15"
            active_setup_directions TEXT,   -- CSV: "sell,buy"

            -- Portfolio state
            open_positions INTEGER DEFAULT 0,
            daily_pnl REAL DEFAULT 0,
            daily_trades INTEGER DEFAULT 0,
            current_loss_streak INTEGER DEFAULT 0,

            -- Unique constraint: one snapshot per pair/tf/candle
            UNIQUE(pair, timeframe, candle_time)
        )
    """)
    print("✅", flush=True)

    conn.commit()


def create_indexes(conn):
    cursor = conn.cursor()

    indexes = [
        # live_trades
        ("idx_lt_pair_setup", "live_trades(pair, setup, result)"),
        ("idx_lt_pair_regime", "live_trades(pair, regime, result)"),
        ("idx_lt_decision", "live_trades(decision_id)"),
        ("idx_lt_oanda", "live_trades(oanda_trade_id)"),
        ("idx_lt_source", "live_trades(source, result)"),
        ("idx_lt_entry_time", "live_trades(entry_time)"),

        # trade_decisions
        ("idx_td_pair_setup", "trade_decisions(pair, setup)"),
        ("idx_td_verdict", "trade_decisions(validator_verdict)"),
        ("idx_td_outcome", "trade_decisions(outcome)"),
        ("idx_td_timestamp", "trade_decisions(timestamp)"),
        ("idx_td_matched", "trade_decisions(outcome_matched_prediction)"),

        # news_events
        ("idx_ne_category", "news_events(category, impact_level)"),
        ("idx_ne_currencies", "news_events(currencies_affected)"),
        ("idx_ne_upcoming", "news_events(is_upcoming, is_active)"),
        ("idx_ne_timestamp", "news_events(timestamp)"),
        ("idx_ne_event_time", "news_events(event_time)"),

        # weather_events
        ("idx_we_active", "weather_events(is_active, severity)"),
        ("idx_we_currencies", "weather_events(currencies_affected)"),

        # wolfram_analyses
        ("idx_wa_type", "wolfram_analyses(query_type)"),
        ("idx_wa_decision", "wolfram_analyses(decision_id)"),
        ("idx_wa_pair", "wolfram_analyses(pair)"),

        # market_snapshots
        ("idx_ms_pair_time", "market_snapshots(pair, timeframe, candle_time)"),
        ("idx_ms_regime", "market_snapshots(pair, regime)"),
        ("idx_ms_timestamp", "market_snapshots(timestamp)"),
    ]

    print(f"\n🔑 Creating {len(indexes)} indexes...", flush=True)
    for name, definition in indexes:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {definition}")
    conn.commit()
    print("✅ All indexes created", flush=True)


def verify_tables(conn):
    cursor = conn.cursor()

    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)

    # Check all tables exist
    tables = cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name IN (
            'backtest_trades', 'backtest_setup_performance', 'backtest_metadata',
            'live_trades', 'trade_decisions', 'news_events',
            'weather_events', 'wolfram_analyses', 'market_snapshots'
        )
        ORDER BY name
    """).fetchall()

    print("\n📋 Trading tables in database:")
    for t in tables:
        count = cursor.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
        cols = len(cursor.execute(f"PRAGMA table_info({t[0]})").fetchall())
        print(f"  ✅ {t[0]:<30} {count:>10,} rows, {cols} columns")

    # Check indexes
    idx_count = cursor.execute("""
        SELECT COUNT(*) FROM sqlite_master 
        WHERE type='index' AND name LIKE 'idx_%'
    """).fetchone()[0]
    print(f"\n  🔑 {idx_count} indexes total")

    # Show example queries the system will use
    print("\n" + "=" * 60)
    print("EXAMPLE QUERIES FOR THE LIVE PIPELINE")
    print("=" * 60)

    print("""
  -- Data Validator: Should I take this trade?
  SELECT win_rate, profit_factor, trade_count, total_pips
  FROM backtest_setup_performance
  WHERE pair='EUR_USD' AND regime='ranging' AND setup='S15_rr2.0_sl2.5'
    AND profit_factor > 1.0;

  -- Decision Logger: Record the full decision
  INSERT INTO trade_decisions (decision_id, pair, timeframe, setup, direction,
    regime, market_agent_data, news_agent_data, validator_verdict, ...)
  VALUES (...);

  -- Trade Logger: Record the live trade
  INSERT INTO live_trades (trade_id, source, pair, ...) VALUES (...);

  -- Performance Drift: Compare live vs backtest
  SELECT 
    'backtest' as source, win_rate, profit_factor
  FROM backtest_setup_performance
  WHERE pair='EUR_USD' AND setup='S15_rr2.0_sl2.5' AND regime='ranging'
  UNION ALL
  SELECT 
    'live' as source,
    ROUND(100.0 * SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) / COUNT(*), 1),
    ROUND(SUM(CASE WHEN pips>0 THEN pips ELSE 0 END) / 
          NULLIF(ABS(SUM(CASE WHEN pips<=0 THEN pips ELSE 0 END)), 0), 2)
  FROM live_trades
  WHERE pair='EUR_USD' AND setup='S15_rr2.0_sl2.5' AND regime='ranging';

  -- News Check: Any high-impact events upcoming?
  SELECT headline, event_time, currencies_affected, impact_level
  FROM news_events
  WHERE is_upcoming=1 AND is_active=1 AND impact_level='high'
    AND currencies_affected LIKE '%USD%'
  ORDER BY event_time;

  -- Audit: Why did we lose on this trade?
  SELECT td.validator_verdict, td.validator_reasoning, td.validator_db_evidence,
         td.news_agent_data, lt.result, lt.pips, lt.exit_reason
  FROM trade_decisions td
  LEFT JOIN live_trades lt ON lt.decision_id = td.decision_id
  WHERE td.outcome = 'loss'
  ORDER BY td.timestamp DESC LIMIT 10;
""")

    db_size = os.path.getsize(DB_PATH) / (1024**3)
    print(f"💾 Database size: {db_size:.2f} GB")


def main():
    t0 = time.time()

    print("=" * 60)
    print("PHASE 1: CREATE LIVE PIPELINE TABLES")
    print("=" * 60)
    print(f"Database: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=DELETE")

    try:
        create_tables(conn)
        create_indexes(conn)
        verify_tables(conn)

        elapsed = time.time() - t0
        print(f"\n✅ Phase 1 complete in {elapsed:.1f}s")
        print(f"📁 All tables ready for Phase 2 (data validator wiring)")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
