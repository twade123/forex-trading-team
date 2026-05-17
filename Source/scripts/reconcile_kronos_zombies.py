"""Kronos zombie trade reconciler.

Queries live_trades (all sources) for rows where exit_time IS NULL, compares
against OANDA's current open trade list, and marks orphans as closed.

Dry-run by default. --execute flag required to modify DB. Always writes a
snapshot JSON to /tmp before any modification so rollback is UPDATE-from-snapshot.

Scope: modifies live_trades rows only (source-agnostic since any zombie blocks
Kronos dedup). Does NOT execute scout/snipe/manual code paths.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_DB = "~/Jarvis/Database/v2/trading_forex.db"


def build_snapshot(
    db_path: str,
    oanda_open_trade_ids: Iterable[str],
    now: datetime | None = None,
) -> dict:
    """Build pre-execution snapshot. No DB writes."""
    now = now or datetime.now(timezone.utc)
    oanda_set = {str(x) for x in oanda_open_trade_ids}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("""
        SELECT id, pair, source, direction, entry_time, oanda_trade_id, status
        FROM live_trades
        WHERE exit_time IS NULL AND COALESCE(status, '') != 'cancelled'
    """)
    rows = []
    keep = 0
    close = 0
    for r in cur:
        is_real = r["oanda_trade_id"] is not None and str(r["oanda_trade_id"]) in oanda_set
        action = "keep" if is_real else "close_orphan"
        if action == "keep":
            keep += 1
        else:
            close += 1
        rows.append({
            "id": r["id"],
            "pair": r["pair"],
            "source": r["source"],
            "direction": r["direction"],
            "entry_time": r["entry_time"],
            "oanda_trade_id": r["oanda_trade_id"],
            "status": r["status"],
            "action": action,
        })
    conn.close()
    return {
        "timestamp": now.isoformat(),
        "mode": "dry_run",
        "oanda_open_trade_ids": sorted(oanda_set),
        "db_open_rows": rows,
        "summary": {"keep": keep, "close_orphan": close},
    }


def apply_snapshot(db_path: str, snapshot: dict, now: datetime | None = None) -> int:
    """Close all rows marked close_orphan in the snapshot. Returns count closed."""
    now = now or datetime.now(timezone.utc)
    ts = now.isoformat()
    ids_to_close = [r["id"] for r in snapshot["db_open_rows"] if r["action"] == "close_orphan"]
    if not ids_to_close:
        return 0
    conn = sqlite3.connect(db_path)
    placeholders = ",".join("?" for _ in ids_to_close)
    conn.execute(f"""
        UPDATE live_trades SET
            exit_time = ?,
            status = 'closed',
            exit_trigger = 'reconcile_orphan',
            exit_method = 'reconcile_orphan',
            pnl_pips = 0,
            pnl_usd = 0,
            outcome = 'orphan_closed'
        WHERE id IN ({placeholders})
          AND exit_time IS NULL
    """, [ts, *ids_to_close])
    changed = conn.total_changes
    conn.commit()
    conn.close()
    return changed


def fetch_oanda_open_trade_ids() -> set[str]:
    """Query live OANDA for currently open trade IDs."""
    # Path follows existing kronos_hunter.py pattern for OANDA client access
    SRC = Path(__file__).resolve().parents[1]
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from oanda_client import OandaClient  # type: ignore
    client = OandaClient()
    trades = client.get_open_trades()  # existing method
    return {str(t["id"]) for t in trades}


def write_snapshot_file(snapshot: dict, mode: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = f"/tmp/kronos_reconcile_{ts}.json"
    snapshot["mode"] = mode
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)
    return path


def print_summary(snapshot: dict) -> None:
    print(f"\n=== Kronos Zombie Reconciler — {snapshot['mode'].upper()} ===")
    print(f"OANDA open trades: {len(snapshot['oanda_open_trade_ids'])}")
    print(f"DB open rows:      {len(snapshot['db_open_rows'])}")
    print(f"\n{'id':<8} {'pair':<10} {'source':<16} {'entry_time':<28} {'action':<14}")
    print("-" * 80)
    for r in sorted(snapshot["db_open_rows"], key=lambda x: x["entry_time"] or ""):
        print(f"{r['id']:<8} {r['pair']:<10} {(r['source'] or '-'):<16} "
              f"{(r['entry_time'] or '-'):<28} {r['action']:<14}")
    print(f"\nSummary: keep={snapshot['summary']['keep']} "
          f"close_orphan={snapshot['summary']['close_orphan']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reconcile Kronos zombie trades against OANDA.")
    ap.add_argument("--execute", action="store_true", help="Actually close orphans (default: dry-run)")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"live_trades DB path (default: {DEFAULT_DB})")
    args = ap.parse_args(argv)

    print("Querying OANDA for open trades...")
    oanda_ids = fetch_oanda_open_trade_ids()

    print(f"Found {len(oanda_ids)} OANDA open trades. Building snapshot...")
    snap = build_snapshot(args.db, oanda_ids)

    mode = "execute" if args.execute else "dry_run"
    path = write_snapshot_file(snap, mode)
    print(f"Snapshot written: {path}")
    print_summary(snap)

    if args.execute:
        if snap["summary"]["close_orphan"] == 0:
            print("\nNo orphans to close. Exiting.")
            return 0
        print(f"\nClosing {snap['summary']['close_orphan']} orphan rows...")
        n = apply_snapshot(args.db, snap)
        print(f"Closed {n} rows.")
        # Flight-log each close
        _write_flight_log_entries(snap)
    else:
        print("\nDRY-RUN. Re-run with --execute to modify DB.")
    return 0


def _write_flight_log_entries(snapshot: dict) -> None:
    """Write one flight_log entry per closed orphan."""
    SRC = Path(__file__).resolve().parents[1]
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    try:
        from flight_recorder import FlightRecorder, FlightStage  # type: ignore
        fr = FlightRecorder()
        for row in snapshot["db_open_rows"]:
            if row["action"] != "close_orphan":
                continue
            fr.record(FlightStage.KRONOS_RECONCILE_ORPHAN, pair=row["pair"],
                      trade_id=row["id"], data={
                "source": row["source"],
                "direction": row["direction"],
                "entry_time": row["entry_time"],
                "oanda_trade_id": row["oanda_trade_id"],
            }, note=f"orphan closed by reconciler")
    except Exception as e:
        print(f"WARN: flight_log write failed: {e}")


if __name__ == "__main__":
    sys.exit(main())
