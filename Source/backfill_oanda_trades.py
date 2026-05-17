#!/usr/bin/env python3
"""
Backfill closed trade data from OANDA into live_trades.

Queries OANDA for the actual state of trades that are stuck as 'open'
in our DB (because guardian couldn't run during server crashes).
Updates exit_price, exit_time, pips, pnl_usd, realized_pl, result, status.

Usage:
    source ~/myenv/bin/activate && python backfill_oanda_trades.py

    # Dry-run (show what would change, don't write):
    source ~/myenv/bin/activate && python backfill_oanda_trades.py --dry-run
"""

import os
import sys
import json
import sqlite3
import argparse
import logging
import requests
from datetime import datetime, timezone

# --- paths ---------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from broker_credentials import BrokerCredentials

# DB that holds live_trades — same path resolution as db_pool.py
# Source/ -> Forex Trading Team/ -> Jarvis/
_JARVIS_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_TRADING_DB = os.path.join(_JARVIS_ROOT, "Database", "v2", "trading_forex.db")
if not os.path.exists(_TRADING_DB):
    # Fallback: try Source/Database/
    _TRADING_DB = os.path.join(_SCRIPT_DIR, "Database", "trading_forex.db")
if not os.path.exists(_TRADING_DB):
    # Last resort: look in Data/
    _TRADING_DB = os.path.join(_SCRIPT_DIR, "Data", "trading_forex.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BACKFILL] %(message)s",
)
log = logging.getLogger(__name__)


def _calc_pips(pair: str, entry: float, exit_: float, direction: str) -> float:
    """Calculate pips — matches outcome_reconciler logic."""
    pip_size = 0.01 if "JPY" in pair else 0.0001
    if direction and direction.lower() in ("sell", "short"):
        return round((entry - exit_) / pip_size, 1)
    return round((exit_ - entry) / pip_size, 1)


def backfill(dry_run: bool = False):
    # 1. Get OANDA credentials
    bc = BrokerCredentials()
    creds = bc.get_connection(user_id=2, broker="oanda")
    if not creds.get("configured"):
        log.error("OANDA credentials not configured or decryption failed")
        return

    api_key = creds["api_key"]
    account_id = creds["account_id"]
    base_url = creds["base_url"]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 2. Get trades stuck as 'open' in our DB
    _using_pool = False
    try:
        from db_pool import get_trading_forex
        conn = get_trading_forex()
        _using_pool = True
        log.info(f"Using db_pool connection")
    except ImportError:
        conn = sqlite3.connect(_TRADING_DB)
        log.info(f"Using direct connection: {_TRADING_DB}")
    conn.row_factory = sqlite3.Row
    stale_rows = conn.execute("""
        SELECT id, pair, direction, entry_price, oanda_trade_id, entry_time, units
        FROM live_trades
        WHERE status = 'open'
          AND oanda_trade_id IS NOT NULL
        ORDER BY entry_time
    """).fetchall()

    if not stale_rows:
        log.info("No stale open trades found — nothing to backfill.")
        if not _using_pool:
            conn.close()
        return

    log.info(f"Found {len(stale_rows)} open trades in DB to check against OANDA")

    # 3. Query OANDA for each trade's actual state
    updated = 0
    still_open = 0
    errors = 0

    for row in stale_rows:
        oanda_id = row["oanda_trade_id"]
        pair = row["pair"]
        direction = row["direction"]
        entry_price = row["entry_price"]
        db_id = row["id"]

        try:
            resp = requests.get(
                f"{base_url}/v3/accounts/{account_id}/trades/{oanda_id}",
                headers=headers,
                timeout=10,
            )

            if resp.status_code == 404:
                log.warning(f"  Trade {oanda_id} ({pair}) not found on OANDA — skipping")
                errors += 1
                continue

            if resp.status_code != 200:
                log.error(f"  OANDA error for trade {oanda_id}: {resp.status_code} {resp.text[:200]}")
                errors += 1
                continue

            trade = resp.json().get("trade", {})
            state = trade.get("state", "OPEN")

            if state == "OPEN":
                # Still genuinely open on OANDA — update unrealizedPL if available
                unrealized = float(trade.get("unrealizedPL", 0))
                current_price = float(trade.get("price", entry_price))
                log.info(f"  {oanda_id} ({pair}) still OPEN on OANDA | unrealPL=${unrealized:.2f}")
                still_open += 1
                continue

            # Trade is CLOSED on OANDA
            realized_pl = float(trade.get("realizedPL", 0))
            close_time = trade.get("closeTime", "")
            avg_close_price = float(trade.get("averageClosePrice", 0))
            financing = float(trade.get("financing", 0))

            # Calculate pips
            pips = _calc_pips(pair, entry_price, avg_close_price, direction)

            # Determine result
            if realized_pl > 0:
                result = "win"
            elif realized_pl < 0:
                result = "loss"
            else:
                result = "breakeven"

            # Parse close time
            if close_time:
                # OANDA format: 2026-03-26T12:34:56.789012345Z
                exit_time = close_time[:19].replace("T", "T")
            else:
                exit_time = None

            log.info(
                f"  {oanda_id} ({pair} {direction}) CLOSED | "
                f"exit={avg_close_price} | pips={pips:+.1f} | "
                f"PL=${realized_pl:+.2f} | result={result}"
            )

            if dry_run:
                log.info(f"    [DRY-RUN] Would update DB id={db_id}")
                updated += 1
                continue

            # 4. Update live_trades
            conn.execute("""
                UPDATE live_trades
                SET status = 'closed',
                    exit_price = ?,
                    exit_time = ?,
                    pips = ?,
                    pnl_pips = ?,
                    pnl_usd = ?,
                    realized_pl = ?,
                    result = ?,
                    outcome = ?,
                    outcome_pips = ?,
                    outcome_usd = ?
                WHERE id = ?
            """, (
                avg_close_price,
                exit_time,
                pips,
                pips,
                realized_pl,
                realized_pl,
                result,
                result,
                pips,
                realized_pl,
                db_id,
            ))
            conn.commit()
            updated += 1
            log.info(f"    ✓ Updated DB id={db_id}")

        except requests.RequestException as e:
            log.error(f"  Network error for trade {oanda_id}: {e}")
            errors += 1
        except Exception as e:
            log.error(f"  Unexpected error for trade {oanda_id}: {e}")
            errors += 1

    if not _using_pool:
        conn.close()

    log.info("=" * 60)
    log.info(f"BACKFILL COMPLETE")
    log.info(f"  Checked:    {len(stale_rows)} trades")
    log.info(f"  Updated:    {updated} (closed on OANDA)")
    log.info(f"  Still open: {still_open}")
    log.info(f"  Errors:     {errors}")
    if dry_run:
        log.info("  MODE: DRY-RUN (no DB changes made)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill OANDA trade data into live_trades")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
