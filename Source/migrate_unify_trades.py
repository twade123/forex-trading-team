"""
Phase 1+2: Unify manual_trades and live_trades into a single canonical table.

This script:
1. Adds all manual_trades-specific columns to live_trades (ALTER TABLE)
2. Fixes the direction CHECK constraint to accept buy/sell/long/short
3. Backfills historical manual_trades data into live_trades
4. Creates indexes for the new columns
5. Verifies data integrity

Run with: python migrate_unify_trades.py [--dry-run]
"""
import sys, sqlite3, json, os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DRY_RUN = '--dry-run' in sys.argv

def get_db_path():
    """Get the trading_forex.db path."""
    try:
        from db_pool import get_trading_forex
        conn = get_trading_forex()
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        conn.close()
        return path
    except:
        # Fallback
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, "Database", "v2", "trading_forex.db")

def main():
    db_path = get_db_path()
    print(f"Database: {db_path}")
    print(f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    print()

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=30000")

    # ── Step 1: Get current schemas ──
    lt_cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(live_trades)").fetchall()}
    mt_cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(manual_trades)").fetchall()}

    print(f"live_trades columns: {len(lt_cols)}")
    print(f"manual_trades columns: {len(mt_cols)}")

    # ── Step 2: Identify columns to add ──
    # Columns in manual_trades but NOT in live_trades
    COLUMNS_TO_ADD = {
        # JSON market snapshots
        'market_picture': 'JSON',
        'market_story': 'JSON',
        'sniper_scores': 'JSON',
        'candle_structure': 'JSON',
        'indicators': 'JSON',
        'exit_market_picture': 'JSON',
        # Derived fast-query fields
        'fan_state': 'TEXT',
        'fan_direction': 'TEXT',
        'fan_ordered': 'INTEGER',
        'e100_role': 'TEXT',
        'fan_width_pct': 'REAL',
        'momentum_state': 'TEXT',
        # Cascade/retracement
        'dual_cross_cascade': 'INTEGER DEFAULT 0',
        'cascade_direction': 'TEXT',
        'retracement_type': 'TEXT',
        'bb_re_expanding': 'INTEGER DEFAULT 0',
        'tested_e55': 'INTEGER DEFAULT 0',
        'tested_e100': 'INTEGER DEFAULT 0',
        'entry_setup_type': 'TEXT',
        # Learning/tracking
        'pattern_fingerprint': 'TEXT',
        'promoted_to_snipe': 'INTEGER DEFAULT 0',
        'classified_setup': 'TEXT',
        # Execution (if not already present)
        'units': 'INTEGER',
        'hold_bars': 'INTEGER',
        'realized_pl': 'REAL',
    }

    # Also check for columns we already added in previous sessions
    already_have = set(lt_cols.keys())

    added = 0
    for col, col_type in COLUMNS_TO_ADD.items():
        if col not in already_have:
            sql = f"ALTER TABLE live_trades ADD COLUMN {col} {col_type}"
            print(f"  + {col} ({col_type})")
            if not DRY_RUN:
                try:
                    conn.execute(sql)
                    added += 1
                except sqlite3.OperationalError as e:
                    if 'duplicate' in str(e).lower():
                        print(f"    (already exists)")
                    else:
                        print(f"    ERROR: {e}")
        else:
            pass  # Already exists

    if not DRY_RUN:
        conn.commit()
    print(f"\nAdded {added} new columns to live_trades")

    # ── Step 3: Fix direction CHECK constraint ──
    # SQLite doesn't support ALTER CONSTRAINT, so we need to recreate the table
    # First check if the constraint is actually blocking buy/sell
    print("\n── Fixing direction CHECK constraint ──")
    try:
        conn.execute("INSERT INTO live_trades (id, pair, direction, entry_price, entry_time, status) VALUES ('__test__', 'TEST', 'buy', 0, '2000-01-01', 'open')")
        conn.execute("DELETE FROM live_trades WHERE id = '__test__'")
        print("  CHECK constraint already accepts 'buy' — no change needed")
    except sqlite3.IntegrityError:
        print("  CHECK constraint blocks 'buy/sell' — rebuilding table...")
        if not DRY_RUN:
            _rebuild_table_with_new_constraint(conn)
        else:
            print("  (would rebuild table in LIVE mode)")

    if not DRY_RUN:
        conn.commit()

    # ── Step 4: Add indexes for new columns ──
    print("\n── Adding indexes ──")
    indexes = [
        ("idx_lt_fan_state", "live_trades(fan_state)"),
        ("idx_lt_pattern_fp", "live_trades(pattern_fingerprint)"),
        ("idx_lt_classified", "live_trades(classified_setup)"),
        ("idx_lt_promoted", "live_trades(promoted_to_snipe)"),
        ("idx_lt_result", "live_trades(result)"),
        ("idx_lt_source", "live_trades(source)"),
    ]
    for idx_name, idx_def in indexes:
        try:
            if not DRY_RUN:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}")
            print(f"  + {idx_name}")
        except Exception as e:
            print(f"  {idx_name}: {e}")

    if not DRY_RUN:
        conn.commit()

    # ── Step 5: Backfill from manual_trades ──
    print("\n── Backfilling manual_trades → live_trades ──")

    # Get all manual_trades
    mt_rows = conn.execute("SELECT * FROM manual_trades ORDER BY created_at").fetchall()
    mt_cols_list = [desc[0] for desc in conn.execute("SELECT * FROM manual_trades LIMIT 0").description]
    print(f"  manual_trades rows: {len(mt_rows)}")

    # Get existing live_trades IDs (by trade_id and oanda_trade_id)
    existing = set()
    for r in conn.execute("SELECT id, oanda_trade_id FROM live_trades"):
        existing.add(str(r[0]))
        if r[1]:
            existing.add(str(r[1]))

    # Map manual_trades direction to live_trades convention
    # After constraint fix, we can use buy/sell directly

    inserted = 0
    updated = 0
    skipped = 0

    for mt in mt_rows:
        mt_dict = dict(zip(mt_cols_list, mt))
        tid = str(mt_dict.get('trade_id', ''))

        if not tid or tid == 'None':
            skipped += 1
            continue

        if tid in existing:
            # Trade exists in live_trades — UPDATE with manual_trades rich data
            update_fields = {}
            for col in ['market_picture', 'market_story', 'sniper_scores',
                        'candle_structure', 'indicators', 'exit_market_picture',
                        'fan_state', 'fan_direction', 'fan_ordered', 'e100_role',
                        'fan_width_pct', 'momentum_state',
                        'dual_cross_cascade', 'cascade_direction', 'retracement_type',
                        'bb_re_expanding', 'tested_e55', 'tested_e100',
                        'entry_setup_type', 'pattern_fingerprint', 'promoted_to_snipe',
                        'classified_setup', 'units', 'hold_bars', 'realized_pl']:
                val = mt_dict.get(col)
                if val is not None:
                    update_fields[col] = val

            # Also fill in result/pips/exit data if live_trades is missing it
            if mt_dict.get('result') and mt_dict['result'] in ('win', 'loss'):
                update_fields['result'] = mt_dict['result']
                if mt_dict.get('pips') is not None:
                    update_fields['pips'] = mt_dict['pips']
                if mt_dict.get('exit_price') is not None:
                    update_fields['exit_price'] = mt_dict['exit_price']
                if mt_dict.get('exit_time'):
                    update_fields['exit_time'] = mt_dict['exit_time']
                if mt_dict.get('exit_reason'):
                    update_fields['exit_reason'] = mt_dict['exit_reason']
                if mt_dict.get('mfe_pips') is not None:
                    update_fields['mfe_pips'] = mt_dict['mfe_pips']
                if mt_dict.get('mae_pips') is not None:
                    update_fields['mae_pips'] = mt_dict['mae_pips']
                if mt_dict.get('realized_pl') is not None:
                    update_fields['realized_pl'] = mt_dict['realized_pl']
                    update_fields['pnl_usd'] = mt_dict['realized_pl']
                # Map result to outcome
                update_fields['outcome'] = mt_dict['result']
                if mt_dict.get('pips') is not None:
                    update_fields['outcome_pips'] = mt_dict['pips']

            # Also backfill bb_expanding and other derived fields
            for col in ['bb_expanding', 'rsi', 'stoch_k', 'trend_health',
                        'story_score', 'story_entry_type']:
                val = mt_dict.get(col)
                if val is not None:
                    # Some of these exist with different names in live_trades
                    update_fields[col] = val

            if update_fields and not DRY_RUN:
                set_clause = ", ".join(f"{k} = ?" for k in update_fields.keys())
                vals = list(update_fields.values()) + [tid, tid]
                try:
                    conn.execute(
                        f"UPDATE live_trades SET {set_clause} WHERE id = ? OR oanda_trade_id = ?",
                        vals
                    )
                    updated += 1
                except Exception as e:
                    print(f"  UPDATE {tid} failed: {e}")
            elif update_fields:
                updated += 1  # dry run count
        else:
            # Trade NOT in live_trades — INSERT new row
            direction = mt_dict.get('direction', 'buy')
            result = mt_dict.get('result')
            outcome = result if result in ('win', 'loss') else None
            status = 'closed' if result else 'open'

            insert_data = {
                'id': tid,
                'source': 'manual',
                'oanda_trade_id': tid,
                'pair': mt_dict.get('pair', ''),
                'timeframe': 'M15',
                'setup': mt_dict.get('classified_setup') or mt_dict.get('entry_setup_type') or 'unknown',
                'base_setup': mt_dict.get('entry_setup_type') or 'unknown',
                'direction': direction,
                'entry_time': mt_dict.get('entry_time') or mt_dict.get('created_at', ''),
                'entry_price': mt_dict.get('entry_price', 0),
                'sl_price': 0,
                'tp_price': 0,
                'status': status,
                'user_id': mt_dict.get('user_id', 2),
                'units': mt_dict.get('units'),
                'exit_price': mt_dict.get('exit_price'),
                'exit_time': mt_dict.get('exit_time'),
                'result': result,
                'pips': mt_dict.get('pips'),
                'realized_pl': mt_dict.get('realized_pl'),
                'pnl_usd': mt_dict.get('realized_pl'),
                'outcome': outcome,
                'outcome_pips': mt_dict.get('pips'),
                'exit_reason': mt_dict.get('exit_reason'),
                'hold_bars': mt_dict.get('hold_bars'),
                'mfe_pips': mt_dict.get('mfe_pips'),
                'mae_pips': mt_dict.get('mae_pips'),
                # Rich market data
                'market_picture': mt_dict.get('market_picture'),
                'market_story': mt_dict.get('market_story'),
                'sniper_scores': mt_dict.get('sniper_scores'),
                'candle_structure': mt_dict.get('candle_structure'),
                'indicators': mt_dict.get('indicators'),
                'exit_market_picture': mt_dict.get('exit_market_picture'),
                # Derived fields
                'fan_state': mt_dict.get('fan_state'),
                'fan_direction': mt_dict.get('fan_direction'),
                'fan_ordered': mt_dict.get('fan_ordered'),
                'e100_role': mt_dict.get('e100_role'),
                'fan_width_pct': mt_dict.get('fan_width_pct'),
                'bb_expanding': mt_dict.get('bb_expanding'),
                'momentum_state': mt_dict.get('momentum_state'),
                'trend_health': mt_dict.get('trend_health'),
                'story_score': mt_dict.get('story_score'),
                'story_entry_type': mt_dict.get('story_entry_type'),
                # Cascade/retracement
                'dual_cross_cascade': mt_dict.get('dual_cross_cascade', 0),
                'cascade_direction': mt_dict.get('cascade_direction'),
                'retracement_type': mt_dict.get('retracement_type'),
                'bb_re_expanding': mt_dict.get('bb_re_expanding', 0),
                'tested_e55': mt_dict.get('tested_e55', 0),
                'tested_e100': mt_dict.get('tested_e100', 0),
                'entry_setup_type': mt_dict.get('entry_setup_type'),
                # Learning
                'pattern_fingerprint': mt_dict.get('pattern_fingerprint'),
                'promoted_to_snipe': mt_dict.get('promoted_to_snipe', 0),
                'classified_setup': mt_dict.get('classified_setup'),
            }

            # Filter out None values and get valid columns
            valid_cols = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)").fetchall()}
            insert_data = {k: v for k, v in insert_data.items() if k in valid_cols and v is not None}

            if not DRY_RUN:
                cols = list(insert_data.keys())
                placeholders = ",".join(["?" for _ in cols])
                col_names = ",".join(cols)
                try:
                    conn.execute(
                        f"INSERT OR IGNORE INTO live_trades ({col_names}) VALUES ({placeholders})",
                        list(insert_data.values())
                    )
                    inserted += 1
                except Exception as e:
                    print(f"  INSERT {tid} {mt_dict.get('pair','?')} failed: {e}")
            else:
                inserted += 1

    if not DRY_RUN:
        conn.commit()

    print(f"\n  Inserted: {inserted} new trades")
    print(f"  Updated: {updated} existing trades with rich data")
    print(f"  Skipped: {skipped}")

    # ── Step 6: Verify ──
    print("\n── Verification ──")
    lt_total = conn.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0]
    mt_total = conn.execute("SELECT COUNT(*) FROM manual_trades").fetchone()[0]
    lt_with_fp = conn.execute("SELECT COUNT(*) FROM live_trades WHERE pattern_fingerprint IS NOT NULL").fetchone()[0]
    lt_with_mp = conn.execute("SELECT COUNT(*) FROM live_trades WHERE market_picture IS NOT NULL").fetchone()[0]
    lt_with_outcome = conn.execute("SELECT COUNT(*) FROM live_trades WHERE outcome IS NOT NULL AND outcome != ''").fetchone()[0]
    lt_manual = conn.execute("SELECT COUNT(*) FROM live_trades WHERE source = 'manual'").fetchone()[0]

    print(f"  live_trades total: {lt_total}")
    print(f"  manual_trades total: {mt_total}")
    print(f"  live_trades with pattern_fingerprint: {lt_with_fp}")
    print(f"  live_trades with market_picture: {lt_with_mp}")
    print(f"  live_trades with outcome: {lt_with_outcome}")
    print(f"  live_trades source=manual: {lt_manual}")

    # Check direction values
    dirs = conn.execute("SELECT direction, COUNT(*) FROM live_trades GROUP BY direction").fetchall()
    print(f"\n  Direction distribution: {dict(dirs)}")

    # Sample unified row
    sample = conn.execute("""
        SELECT id, pair, direction, outcome, pnl_pips, fan_state, pattern_fingerprint, source
        FROM live_trades
        WHERE pattern_fingerprint IS NOT NULL AND outcome IS NOT NULL
        LIMIT 3
    """).fetchall()
    print(f"\n  Sample unified rows:")
    for s in sample:
        print(f"    {s[0]} {s[1]} {s[2]} | {s[3]} | fp={s[6][:30] if s[6] else '?'}...")

    conn.close()
    print("\nDone!")


def _rebuild_table_with_new_constraint(conn):
    """Rebuild live_trades with direction CHECK accepting buy/sell/long/short."""
    print("  Creating backup...")
    conn.execute("ALTER TABLE live_trades RENAME TO live_trades_backup")

    # Get full DDL and modify the CHECK constraint
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'live_trades_backup'"
    ).fetchone()[0]

    # Replace constraint
    new_ddl = ddl.replace("live_trades_backup", "live_trades")
    new_ddl = new_ddl.replace(
        "CHECK (direction IN ('long', 'short'))",
        "CHECK (direction IN ('long', 'short', 'buy', 'sell'))"
    )

    # If the constraint is in a different format, try broader replacement
    if "buy" not in new_ddl:
        # Try without quotes variation
        import re
        new_ddl = re.sub(
            r"CHECK\s*\(\s*direction\s+IN\s*\([^)]+\)\s*\)",
            "CHECK (direction IN ('long', 'short', 'buy', 'sell'))",
            new_ddl,
            flags=re.IGNORECASE
        )

    print("  Creating new table with updated constraint...")
    conn.execute(new_ddl)

    # Copy data
    print("  Copying data...")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(live_trades_backup)").fetchall()]
    col_list = ", ".join(cols)
    conn.execute(f"INSERT INTO live_trades ({col_list}) SELECT {col_list} FROM live_trades_backup")

    # Verify counts
    old_count = conn.execute("SELECT COUNT(*) FROM live_trades_backup").fetchone()[0]
    new_count = conn.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0]
    print(f"  Copied {new_count}/{old_count} rows")
    assert new_count == old_count, f"Row count mismatch! {new_count} != {old_count}"

    # Recreate indexes
    print("  Recreating indexes...")
    indexes = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='live_trades_backup' AND sql IS NOT NULL"
    ).fetchall()
    for idx in indexes:
        idx_sql = idx[0].replace("live_trades_backup", "live_trades")
        try:
            conn.execute(idx_sql)
        except Exception as e:
            print(f"    Index recreation: {e}")

    # Drop backup
    conn.execute("DROP TABLE live_trades_backup")
    conn.commit()
    print("  Table rebuilt successfully!")


if __name__ == "__main__":
    main()
