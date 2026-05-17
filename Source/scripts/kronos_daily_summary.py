"""Print daily Kronos summary: scans, signals, trades, WR, pnl, filter rejects.
Usage: python kronos_daily_summary.py [--date YYYY-MM-DD]
"""
import argparse
import sqlite3
import sys
from datetime import date as _date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db_pool import get_trading_forex

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=_date.today().isoformat())
    args = parser.parse_args()

    con = get_trading_forex()
    con.row_factory = sqlite3.Row

    day_start = f"{args.date}T00:00:00+00:00"
    day_end = f"{args.date}T23:59:59+00:00"

    total = con.execute(
        "SELECT COUNT(*) FROM kronos_signals "
        "WHERE anchor_time BETWEEN ? AND ?",
        (day_start, day_end),
    ).fetchone()[0]

    by_action = con.execute(
        "SELECT action_taken, COUNT(*) AS n FROM kronos_signals "
        "WHERE anchor_time BETWEEN ? AND ? GROUP BY action_taken "
        "ORDER BY n DESC",
        (day_start, day_end),
    ).fetchall()

    trades = con.execute(
        "SELECT lt.id, lt.pair, lt.direction, lt.pnl_pips, lt.outcome, lt.status "
        "FROM live_trades lt WHERE lt.source='kronos_hunter' "
        "AND lt.entry_time BETWEEN ? AND ? ORDER BY lt.entry_time",
        (day_start, day_end),
    ).fetchall()

    closed = [t for t in trades if t["status"] == "closed" and t["outcome"] in ("win", "loss")]
    wins = sum(1 for t in closed if t["outcome"] == "win")
    pnl = sum((t["pnl_pips"] or 0.0) for t in closed)

    print(f"=== Kronos summary for {args.date} ===")
    print(f"  signals total:  {total}")
    for row in by_action:
        print(f"    {row['action_taken']:28s}: {row['n']:4d}")
    print(f"  hunter trades opened: {len(trades)}")
    if closed:
        print(f"    closed: {len(closed)}, wins: {wins}, "
              f"WR: {100*wins/len(closed):.1f}%")
    else:
        print(f"    closed: 0 (no closed trades)")
    print(f"    total pnl (pips): {pnl:+.1f}")

if __name__ == "__main__":
    main()
