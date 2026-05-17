#!/usr/bin/env python3
"""Load V3 backtest results into v2/trading_forex.db for the data validator.

Creates trading tables in the existing jarvis database:
  - backtest_trades: every trade with full forensic data (~8.5M rows)
  - backtest_setup_performance: pre-aggregated setup × pair × timeframe × regime stats
  - backtest_metadata: sweep metadata (run date, params, etc.)

Indexes optimized for data validator queries:
  - Lookup by pair + regime + setup (primary query path)
  - Filter by h4_agrees, session, result
  - Aggregate by setup + regime

Usage:
    cd ~/jarvis/Trading\ Bot
    source ~/myenv/bin/activate
    python -u -m Source.backtester.load_to_db
"""

import csv
import gc
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

TRADING_BOT = Path(__file__).resolve().parent.parent.parent
JARVIS_ROOT = TRADING_BOT.parent
DB_PATH = JARVIS_ROOT / "Database" / "v2" / "trading_forex.db"
RESULTS_DIR = TRADING_BOT / "Results"
TRADES_CSV = RESULTS_DIR / "v3_all_trades.csv"
SETUP_SUMMARY_CSV = RESULTS_DIR / "v3_setup_summary.csv"

BATCH_SIZE = 50_000


def get_column_types():
    """Define column types for the trades table. Everything else is TEXT by default."""
    return {
        'trade_id': 'TEXT',
        'pips': 'REAL',
        'risk_reward_actual': 'REAL',
        'combined_pips': 'REAL',
        'partial_exit_pips': 'REAL',
        'second_half_pips': 'REAL',
        'entry_price': 'REAL',
        'exit_price': 'REAL',
        'sl_price': 'REAL',
        'tp_price': 'REAL',
        'adx': 'REAL',
        'adx_slope': 'REAL',
        'rsi': 'REAL',
        'macd_value': 'REAL',
        'macd_signal': 'REAL',
        'macd_hist': 'REAL',
        'stoch_k': 'REAL',
        'stoch_d': 'REAL',
        'cci': 'REAL',
        'bb_upper': 'REAL',
        'bb_mid': 'REAL',
        'bb_lower': 'REAL',
        'bb_width': 'REAL',
        'sma50': 'REAL',
        'sma100': 'REAL',
        'atr': 'REAL',
        'sar': 'REAL',
        'nearest_support': 'REAL',
        'nearest_resistance': 'REAL',
        'pivot_pp': 'REAL',
        'pivot_r1': 'REAL',
        'pivot_s1': 'REAL',
        'dist_to_daily_pivot_atr': 'REAL',
        'max_favorable_pips': 'REAL',
        'max_adverse_pips': 'REAL',
        'candles_to_exit': 'INTEGER',
        'loss_streak_at_entry': 'INTEGER',
        'max_loss_streak': 'INTEGER',
        'be_candle': 'REAL',
        'rr_mult': 'REAL',
        'sl_mult': 'REAL',
        'confidence': 'REAL',
        'risk_reward_actual': 'REAL',
    }


def create_tables(conn):
    """Create trading tables (drop if exist to reload fresh)."""
    cursor = conn.cursor()

    # Read CSV header to get all columns
    with open(TRADES_CSV, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)

    col_types = get_column_types()

    # Build column definitions
    col_defs = []
    for col in header:
        ctype = col_types.get(col, 'TEXT')
        col_defs.append(f'    "{col}" {ctype}')

    cols_sql = ',\n'.join(col_defs)

    print("🗄️  Creating tables...", flush=True)

    cursor.execute("DROP TABLE IF EXISTS backtest_trades")
    cursor.execute("DROP TABLE IF EXISTS backtest_setup_performance")
    cursor.execute("DROP TABLE IF EXISTS backtest_metadata")

    cursor.execute(f"""
        CREATE TABLE backtest_trades (
        {cols_sql}
        )
    """)

    # Pre-aggregated performance table for fast validator lookups
    cursor.execute("""
        CREATE TABLE backtest_setup_performance (
            setup TEXT NOT NULL,
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            regime TEXT NOT NULL,
            trade_count INTEGER,
            win_count INTEGER,
            win_rate REAL,
            total_pips REAL,
            avg_pips REAL,
            profit_factor REAL,
            avg_risk_reward REAL,
            max_favorable REAL,
            max_adverse REAL,
            avg_hold_time REAL,
            h4_agrees_count INTEGER,
            h4_agrees_win_rate REAL,
            best_session TEXT,
            best_session_win_rate REAL,
            PRIMARY KEY (setup, pair, timeframe, regime)
        )
    """)

    cursor.execute("""
        CREATE TABLE backtest_metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()
    return header


def load_trades(conn, header):
    """Stream CSV into backtest_trades table in batches."""
    cursor = conn.cursor()
    col_types = get_column_types()

    placeholders = ','.join(['?' for _ in header])
    col_names = ",".join(['"' + c + '"' for c in header])
    insert_sql = f"INSERT INTO backtest_trades ({col_names}) VALUES ({placeholders})"

    print("\n📥 Loading trades...", flush=True)
    t0 = time.time()
    total = 0
    batch = []

    with open(TRADES_CSV, 'r') as f:
        reader = csv.reader(f)
        next(reader)  # skip header

        for row in reader:
            # Convert types
            converted = []
            for i, val in enumerate(row):
                col = header[i]
                if val == '' or val == 'None' or val == 'nan':
                    converted.append(None)
                elif col in col_types:
                    try:
                        fv = float(val)
                        if fv != fv or abs(fv) == float('inf') or abs(fv) > 1e15:
                            converted.append(None)
                        elif col_types[col] == 'INTEGER':
                            converted.append(int(fv))
                        else:  # REAL
                            converted.append(fv)
                    except (ValueError, TypeError, OverflowError):
                        converted.append(None)
                else:
                    converted.append(val)

            batch.append(converted)
            total += 1

            if len(batch) >= BATCH_SIZE:
                cursor.executemany(insert_sql, batch)
                conn.commit()
                elapsed = time.time() - t0
                rate = total / elapsed
                print(f"  {total:,} rows loaded ({rate:,.0f} rows/sec)", flush=True)
                batch = []

    if batch:
        cursor.executemany(insert_sql, batch)
        conn.commit()

    elapsed = time.time() - t0
    print(f"\n✅ {total:,} trades loaded in {elapsed:.1f}s ({total/elapsed:,.0f} rows/sec)", flush=True)
    return total


def create_indexes(conn):
    """Create indexes for data validator query patterns."""
    print("\n🔑 Creating indexes...", flush=True)
    cursor = conn.cursor()

    indexes = [
        # Primary validator lookup: "how does this setup perform on this pair in this regime?"
        ("idx_bt_pair_regime_setup", "backtest_trades(pair, regime, setup)"),
        # Filter by result for win/loss analysis
        ("idx_bt_pair_setup_result", "backtest_trades(pair, setup, result)"),
        # H4 filter analysis
        ("idx_bt_pair_h4_agrees", "backtest_trades(pair, h4_agrees, result)"),
        # Session timing analysis
        ("idx_bt_pair_session", "backtest_trades(pair, session, result)"),
        # Setup performance by timeframe
        ("idx_bt_setup_tf", "backtest_trades(setup, timeframe, result)"),
        # Regime analysis
        ("idx_bt_regime_result", "backtest_trades(regime, result)"),
        # Concurrent setup analysis
        ("idx_bt_concurrent", "backtest_trades(pair, timeframe, entry_time)"),
        # Loss streak analysis
        ("idx_bt_loss_streak", "backtest_trades(pair, setup, loss_streak_at_entry)"),
    ]

    for name, definition in indexes:
        t0 = time.time()
        print(f"  {name}...", end=" ", flush=True)
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {definition}")
        conn.commit()
        print(f"{time.time()-t0:.1f}s", flush=True)


def build_performance_table(conn):
    """Pre-aggregate setup performance for fast validator lookups."""
    print("\n📊 Building setup performance table...", flush=True)
    cursor = conn.cursor()

    # Main aggregation
    cursor.execute("""
        INSERT INTO backtest_setup_performance 
            (setup, pair, timeframe, regime, trade_count, win_count, win_rate,
             total_pips, avg_pips, profit_factor, avg_risk_reward,
             max_favorable, max_adverse, avg_hold_time,
             h4_agrees_count, h4_agrees_win_rate, best_session, best_session_win_rate)
        SELECT 
            setup, pair, timeframe, regime,
            COUNT(*) as trade_count,
            SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as win_count,
            ROUND(100.0 * SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
            ROUND(SUM(COALESCE(combined_pips, pips)), 1) as total_pips,
            ROUND(AVG(COALESCE(combined_pips, pips)), 2) as avg_pips,
            ROUND(
                CASE WHEN SUM(CASE WHEN COALESCE(combined_pips, pips) <= 0 
                    THEN ABS(COALESCE(combined_pips, pips)) ELSE 0 END) > 0
                THEN SUM(CASE WHEN COALESCE(combined_pips, pips) > 0 
                    THEN COALESCE(combined_pips, pips) ELSE 0 END) /
                    SUM(CASE WHEN COALESCE(combined_pips, pips) <= 0 
                    THEN ABS(COALESCE(combined_pips, pips)) ELSE 0 END)
                ELSE 9999.0 END, 2) as profit_factor,
            ROUND(AVG(risk_reward_actual), 2) as avg_risk_reward,
            ROUND(AVG(max_favorable_pips), 1) as max_favorable,
            ROUND(AVG(max_adverse_pips), 1) as max_adverse,
            ROUND(AVG(candles_to_exit), 1) as avg_hold_time,
            SUM(CASE WHEN h4_agrees IN ('True', '1', 'true') THEN 1 ELSE 0 END) as h4_agrees_count,
            ROUND(100.0 * SUM(CASE WHEN h4_agrees IN ('True', '1', 'true') AND result='win' THEN 1 ELSE 0 END) /
                NULLIF(SUM(CASE WHEN h4_agrees IN ('True', '1', 'true') THEN 1 ELSE 0 END), 0), 1) as h4_agrees_win_rate,
            NULL, NULL  -- best_session filled separately
        FROM backtest_trades
        GROUP BY setup, pair, timeframe, regime
        HAVING COUNT(*) >= 5
    """)
    conn.commit()

    rows = cursor.execute("SELECT COUNT(*) FROM backtest_setup_performance").fetchone()[0]
    print(f"✅ {rows:,} setup performance rows created", flush=True)

    # Index the performance table
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_perf_pair_regime 
        ON backtest_setup_performance(pair, regime)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_perf_pair_setup
        ON backtest_setup_performance(pair, setup)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_perf_profitable
        ON backtest_setup_performance(pair, regime, profit_factor)
    """)
    conn.commit()


def save_metadata(conn, total_trades):
    """Save sweep metadata."""
    cursor = conn.cursor()
    metadata = {
        'total_trades': str(total_trades),
        'loaded_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source_file': str(TRADES_CSV),
        'data_range': '2023-02 to 2026-02 (3 years)',
        'pairs': '13 (EUR_USD,GBP_USD,USD_JPY,USD_CHF,AUD_USD,NZD_USD,USD_CAD,EUR_GBP,EUR_JPY,GBP_JPY,EUR_AUD,EUR_CHF,AUD_JPY)',
        'timeframes': 'H4,H1,M15',
        'setups': '20 (S1-S20)',
        'param_variants': '16 per setup (RR=[1.5,2,2.5,3] × SL=[1,1.5,2,2.5])',
        'features': 'regime,h4_filter,trailing_stop,partial_exits,session_timing,daily_pivots,loss_streak,concurrent_setups',
    }
    cursor.executemany(
        "INSERT OR REPLACE INTO backtest_metadata (key, value) VALUES (?, ?)",
        metadata.items()
    )
    conn.commit()


def print_validator_examples(conn):
    """Show example queries the data validator would run."""
    cursor = conn.cursor()

    print("\n" + "=" * 70)
    print("EXAMPLE DATA VALIDATOR QUERIES")
    print("=" * 70)

    # Query 1: What's the best setup for EUR_USD in a ranging market?
    print("\n📊 Best setups for EUR_USD in ranging market:")
    rows = cursor.execute("""
        SELECT setup, timeframe, trade_count, win_rate, profit_factor, total_pips
        FROM backtest_setup_performance
        WHERE pair='EUR_USD' AND regime='ranging' AND trade_count >= 10 AND profit_factor > 1.0
        ORDER BY profit_factor DESC
        LIMIT 5
    """).fetchall()
    print(f"  {'SETUP':<25} {'TF':<4} {'TRADES':>6} {'WR%':>6} {'PF':>8} {'PIPS':>8}")
    for r in rows:
        print(f"  {r[0]:<25} {r[1]:<4} {r[2]:>6} {r[3]:>5.1f}% {r[4]:>8.2f} {r[5]:>7.0f}")

    # Query 2: Should I take this S15 divergence sell on GBP_JPY?
    print("\n📊 S15 divergence on GBP_JPY — by regime:")
    rows = cursor.execute("""
        SELECT regime, trade_count, win_rate, profit_factor, total_pips, h4_agrees_win_rate
        FROM backtest_setup_performance
        WHERE pair='GBP_JPY' AND setup LIKE 'S15%' AND trade_count >= 10
        ORDER BY profit_factor DESC
        LIMIT 5
    """).fetchall()
    print(f"  {'REGIME':<16} {'TRADES':>6} {'WR%':>6} {'PF':>8} {'PIPS':>8} {'H4 WR%':>7}")
    for r in rows:
        h4 = f"{r[5]:.1f}%" if r[5] else "N/A"
        print(f"  {r[0]:<16} {r[1]:>6} {r[2]:>5.1f}% {r[3]:>8.2f} {r[4]:>7.0f} {h4:>7}")

    # Query 3: What's the overall win rate when H4 agrees vs disagrees?
    print("\n📊 H4 agreement impact (all pairs):")
    for h4 in [('True', '1', 'true'), ('False', '0', 'false')]:
        label = 'H4 agrees' if 'True' in h4 else 'H4 disagrees'
        placeholders = ','.join(['?' for _ in h4])
        row = cursor.execute(f"""
            SELECT COUNT(*), 
                   ROUND(100.0 * SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) / COUNT(*), 1),
                   ROUND(SUM(COALESCE(combined_pips, pips)), 0)
            FROM backtest_trades
            WHERE h4_agrees IN ({placeholders})
        """, h4).fetchone()
        print(f"  {label:<15}: {row[0]:>10,} trades, {row[1]}% win rate, {row[2]:>12,.0f} pips")

    # DB size
    db_size = os.path.getsize(DB_PATH) / (1024**3)
    print(f"\n💾 Database size: {db_size:.2f} GB")


def main():
    t0 = time.time()

    print("=" * 70)
    print("LOADING V3 BACKTEST RESULTS INTO TREVOR DATABASE")
    print("=" * 70)
    print(f"Database: {DB_PATH}")
    print(f"Source: {TRADES_CSV}")

    if not TRADES_CSV.exists():
        print(f"❌ {TRADES_CSV} not found!")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-2000000")  # 2GB cache
    conn.execute("PRAGMA temp_store=MEMORY")

    try:
        header = create_tables(conn)
        total = load_trades(conn, header)
        create_indexes(conn)
        build_performance_table(conn)
        save_metadata(conn, total)
        print_validator_examples(conn)

        elapsed = time.time() - t0
        print(f"\n{'='*70}")
        print(f"✅ COMPLETE in {elapsed/60:.1f} minutes")
        print(f"{'='*70}")
        print(f"Tables created:")
        print(f"  backtest_trades          — {total:,} rows (full forensic data)")
        print(f"  backtest_setup_performance — pre-aggregated for fast lookups")
        print(f"  backtest_metadata        — sweep parameters")
        print(f"\nThe data validator can now query:")
        print(f"  SELECT * FROM backtest_setup_performance")
        print(f"    WHERE pair=? AND regime=? AND profit_factor > 1.0")
        print(f"    ORDER BY profit_factor DESC")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
