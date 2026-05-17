"""Kronos performance tripwire.

Independent daemon. Every 60s, computes rolling 4h Kronos PnL
(realized + unrealized). If breach:
  - Flip kronos.enabled=False via tuning_overrides
  - Flight-record KRONOS_AUTO_ROLLBACK
  - Exit (one-shot — manual re-enable required)

NOT a process/crash watchdog. Existing 3-layer watchdog handles crashes.
This catches value bleed and pulls the master kill-switch on Kronos trading.

Scope: reads only WHERE source='kronos_hunter'. Writes only
tuning_overrides.param='kronos.enabled'. Cannot disable scout/snipe.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("kronos_rollback_tripwire")

DEFAULT_DB = "~/Jarvis/Database/v2/trading_forex.db"
TICK_INTERVAL_SEC = 60


def fetch_unrealized_kronos_pnl(db_path: str = DEFAULT_DB) -> float:
    """Query OANDA for open Kronos trades' unrealized PnL in pips.
    Fails open (returns 0) on any error — prefer missing signal over false tripwire.
    """
    try:
        conn = sqlite3.connect(db_path)
        open_trades = conn.execute("""
            SELECT id, pair, oanda_trade_id, entry_price, direction
            FROM live_trades
            WHERE source='kronos_hunter' AND exit_time IS NULL
        """).fetchall()
        conn.close()
        if not open_trades:
            return 0.0
        from oanda_client import OandaClient  # type: ignore
        client = OandaClient()
        total_pips = 0.0
        for row in open_trades:
            oid = row[2]
            if not oid:
                continue
            t = client.get_trade(oid)
            if not t:
                continue
            pair = row[1]
            pip = 0.01 if "JPY" in pair else 0.0001
            entry = float(row[3])
            direction = row[4]
            price = float(t.get("price") or entry)
            if direction == "buy":
                pips = (price - entry) / pip
            else:
                pips = (entry - price) / pip
            total_pips += pips
        return total_pips
    except Exception as e:
        logger.warning("unrealized PnL fetch failed — treating as 0: %s", e)
        return 0.0


def compute_kronos_pnl(window_hours: int, db_path: str = DEFAULT_DB) -> float:
    """Rolling window Kronos PnL (realized + unrealized) in pips."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    conn = sqlite3.connect(db_path)
    realized = conn.execute("""
        SELECT COALESCE(SUM(pnl_pips), 0) FROM live_trades
        WHERE source='kronos_hunter' AND exit_time IS NOT NULL
          AND exit_time >= ?
    """, [cutoff.isoformat()]).fetchone()[0] or 0.0
    conn.close()
    unrealized = fetch_unrealized_kronos_pnl(db_path)
    return float(realized) + float(unrealized)


def _set_tuning_override(db_path: str, param: str, value: str,
                         previous_value: str, reason: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE tuning_overrides SET active=0 WHERE param=? AND active=1",
        [param],
    )
    conn.execute("""
        INSERT INTO tuning_overrides (param, value, previous_value, reason, active, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
    """, [param, value, previous_value, reason, datetime.now(timezone.utc).isoformat()])
    conn.commit()
    conn.close()


def _write_flight_log(db_path: str, pnl: float, threshold: float, window_h: int) -> None:
    try:
        from flight_recorder import FlightRecorder, FlightStage  # type: ignore
        fr = FlightRecorder()
        fr.record(FlightStage.KRONOS_AUTO_ROLLBACK, data={
            "pnl_window_pips": pnl,
            "threshold_pips": threshold,
            "window_hours": window_h,
        }, status="error",
           note=f"KRONOS AUTO-ROLLBACK {pnl:+.1f}p in {window_h}h")
    except Exception as e:
        logger.warning("flight_log write failed: %s", e)


def check_and_act(
    *,
    enabled: bool,
    threshold: float,
    window_hours: int,
    db_path: str = DEFAULT_DB,
) -> bool:
    """Single check. Returns True if tripwire fired, False otherwise."""
    if not enabled:
        return False
    pnl = compute_kronos_pnl(window_hours=window_hours, db_path=db_path)
    if pnl > threshold:
        return False
    reason = f"auto-rollback: {window_hours}h pnl {pnl:+.1f}p <= {threshold}p"
    _set_tuning_override(db_path, "kronos.enabled", "false", "true", reason)
    _write_flight_log(db_path, pnl, threshold, window_hours)
    logger.critical("KRONOS AUTO-ROLLBACK: %.1fp in %dh <= %dp. "
                    "kronos.enabled=False. Manual re-enable required.",
                    pnl, window_hours, threshold)
    return True


def _resolve_params() -> tuple[bool, float, int]:
    """Read current tuning values. Falls back to defaults on error."""
    try:
        from tuning_config import tc_get_for_trade  # type: ignore
        enabled = bool(tc_get_for_trade("guardian.auto_rollback_enabled", "kronos_hunter"))
        threshold = float(tc_get_for_trade("guardian.auto_rollback_pnl_threshold", "kronos_hunter"))
        window_h = int(tc_get_for_trade("guardian.auto_rollback_window_hours", "kronos_hunter"))
        return enabled, threshold, window_h
    except Exception as e:
        logger.warning("tuning param resolution failed, using defaults: %s", e)
        return True, -50.0, 4


def main():
    logger.info("kronos_rollback_tripwire starting — interval=%ds", TICK_INTERVAL_SEC)
    while True:
        try:
            enabled, threshold, window_h = _resolve_params()
            fired = check_and_act(
                enabled=enabled, threshold=threshold, window_hours=window_h,
            )
            if fired:
                logger.info("Tripwire fired — exiting (one-shot).")
                return 0
        except Exception as e:
            logger.error("tripwire tick error: %s", e)
        time.sleep(TICK_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
