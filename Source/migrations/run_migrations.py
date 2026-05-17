"""Apply hand-written SQL migrations to the appropriate DB.

Usage:
  python3 migrations/run_migrations.py --db core --file 2026_05_09_add_is_founder.sql
  python3 migrations/run_migrations.py --db flight --file 2026_05_09_add_user_id_to_flight_log.sql
"""
import argparse
import sqlite3
import sys
from pathlib import Path

DBS = {
    "core": Path("~/Jarvis/Database/v2/core.db"),
    "flight": Path("<repo_root>/Source/flight_recorder.db"),
    "trading": Path("~/Jarvis/Database/v2/trading_forex.db"),
}

MIGRATIONS_DIR_BY_DB = {
    "core": Path("~/Jarvis/Database/v2/migrations"),
    "flight": Path("<repo_root>/Source/migrations"),
    "trading": Path("<repo_root>/Source/migrations"),
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", choices=list(DBS), required=True)
    p.add_argument("--file", required=True, help="SQL filename inside the db's migrations dir")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    db_path = DBS[args.db]
    sql_path = MIGRATIONS_DIR_BY_DB[args.db] / args.file
    if not sql_path.exists():
        print(f"ERR: {sql_path} not found")
        return 2

    sql = sql_path.read_text()
    print(f"Migration: {sql_path}")
    print(f"Target DB: {db_path}")
    print("--- SQL ---")
    print(sql)
    print("-----------")

    if args.dry_run:
        print("Dry run, not applying.")
        return 0

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(sql)
        conn.commit()
        print("Applied.")
    except Exception as e:
        print(f"FAILED: {e}")
        conn.rollback()
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
