"""Thin DAO for the kronos_signals table.

The table logs EVERY Kronos forecast call — whether a trade fired or was
skipped. Consumers: KronosHunter (insert on each pair scan), KronosFilter
(insert on each scout-cycle check), ThesisOverlay (read for analysis).

NOTE: ``anchor_time`` values MUST be ISO-8601 with T separator and a UTC
offset, e.g. ``datetime.now(timezone.utc).isoformat()`` which produces
``2026-04-15T12:00:00+00:00``.  SQLite's ``CURRENT_TIMESTAMP`` format
(``2026-04-15 12:00:00``, space-separated, no offset) is intentionally NOT
used here — mixing the two formats in comparisons would produce wrong results.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


_COLUMNS = (
    "anchor_time", "pair", "direction", "drift_pips", "drift_atr_frac",
    "confidence", "atr_pips", "forecast_terminal", "forecast_max_high",
    "forecast_min_low", "action_taken", "trade_id", "latency_ms", "error",
    "thesis_indicators_at_entry", "matches_thesis",
    "early_drift_pips", "terminal_drift_pips", "consensus",
    "forecast_sl_price", "forecast_tp_price",
    "forecast_path_json",
)


class KronosSignalsDB:
    """DAO for ``kronos_signals`` in ``trading_forex.db``.

    Production usage (no args): uses the thread-local db_pool connection —
    do NOT close it, the pool manages lifetime.

    Test usage: pass a raw ``sqlite3.Connection`` via ``conn=`` to target a
    temporary database without touching the pool.
    """

    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        """``conn`` defaults to the ``trading_forex.db`` pool connection.
        Pass an ``sqlite3.Connection`` for tests."""
        self._conn_override = conn
        try:
            con = self._connection()
            con.execute("ALTER TABLE kronos_signals ADD COLUMN forecast_path_json TEXT")
            con.commit()
        except Exception:
            pass  # column already exists

    def _connection(self) -> sqlite3.Connection:
        if self._conn_override is not None:
            return self._conn_override
        from db_pool import get_trading_forex
        return get_trading_forex()

    def insert(self, **fields: Any) -> int:
        cols = [c for c in _COLUMNS if c in fields]
        placeholders = ", ".join("?" for _ in cols)
        values = [
            json.dumps(fields[c]) if c == "thesis_indicators_at_entry"
            and isinstance(fields[c], (dict, list)) else fields[c]
            for c in cols
        ]
        sql = f"INSERT INTO kronos_signals ({', '.join(cols)}) VALUES ({placeholders})"
        con = self._connection()
        cur = con.execute(sql, values)
        con.commit()
        return int(cur.lastrowid)

    def recent_for_pair(
        self, pair: str, within_minutes: int = 15
    ) -> Optional[Dict[str, Any]]:
        """Latest kronos signal for `pair` whose anchor_time is within
        `within_minutes` of current UTC time. Returns None if none qualify."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(minutes=within_minutes)).isoformat()
        con = self._connection()
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM kronos_signals "
            "WHERE pair=? AND anchor_time >= ? "
            "ORDER BY anchor_time DESC LIMIT 1",
            (pair, cutoff),
        ).fetchone()
        return dict(row) if row else None

    def last_signal_time_for_pair(self, pair: str) -> Optional[datetime]:
        """Latest anchor_time for any action on this pair (for cooldown).

        Always returns a timezone-aware ``datetime`` (UTC).  If the stored
        string lacks a UTC offset it is assumed to be UTC and annotated
        accordingly.
        """
        con = self._connection()
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT anchor_time FROM kronos_signals "
            "WHERE pair=? ORDER BY anchor_time DESC LIMIT 1",
            (pair,),
        ).fetchone()
        if not row:
            return None
        raw = row["anchor_time"]
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def daily_hunter_pnl_pips(
        self, trade_pnl_lookup: Dict[str, float], as_of: datetime
    ) -> float:
        """Sum pnl_pips for all hunter_trade entries with anchor on `as_of` date.
        `trade_pnl_lookup` maps trade_id -> pnl_pips (passed in from live_trades
        by caller to keep this DAO independent of other tables)."""
        day_start = as_of.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        day_end = (as_of.replace(hour=0, minute=0, second=0, microsecond=0)
                   + timedelta(days=1)).isoformat()
        con = self._connection()
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT trade_id FROM kronos_signals "
            "WHERE action_taken='hunter_trade' "
            "AND anchor_time >= ? AND anchor_time < ?",
            (day_start, day_end),
        ).fetchall()
        return sum(trade_pnl_lookup.get(r["trade_id"], 0.0) for r in rows)
