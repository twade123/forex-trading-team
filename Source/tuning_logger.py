"""
Tuning Logger — records parameter changes to the tuning_overrides table.

Usage from anywhere in the trading system:

    from tuning_logger import log_tuning_change

    log_tuning_change(
        param="snipe.sl_atr_mult",
        value="2.5",
        previous_value="1.5",
        reason="Backtest showed 3 trades saved at wider SL",
        backtest_result={"dollars_saved": 53.95, "trades_saved": 3},
        approved_by="Tim (user)"
    )

The tuning dashboard in the admin Performance panel reads from this table
and shows before/after trade performance for each change.
"""

import json
import logging
import os
import sqlite3
import datetime

logger = logging.getLogger(__name__)

_TRADING_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JARVIS_ROOT = os.path.dirname(_TRADING_BOT_DIR)
_TRADING_FOREX_DB = os.path.join(_JARVIS_ROOT, "Database", "v2", "trading_forex.db")


def log_tuning_change(
    param: str,
    value: str,
    previous_value: str = "",
    reason: str = "",
    backtest_result: dict = None,
    approved_by: str = "system",
    db_path: str = None,
) -> int:
    """Record a tuning parameter change to the tuning_overrides table.

    Args:
        param: Dotted parameter name (e.g. 'snipe.sl_atr_mult')
        value: New value as string
        previous_value: Old value as string
        reason: Human-readable reason for the change
        backtest_result: Optional dict with backtest evidence
        approved_by: Who approved ('Tim (user)', 'system', 'claude-code', etc.)
        db_path: Override DB path (defaults to trading_forex.db)

    Returns:
        The inserted row ID, or -1 on error.
    """
    db = db_path or _TRADING_FOREX_DB
    now = datetime.datetime.utcnow().isoformat()
    bt_json = json.dumps(backtest_result) if backtest_result else None

    # Store values as valid JSON so tuning_config._load_overrides() can
    # round-trip them. Python's str(True)='True' is NOT valid JSON — it has
    # to be 'true'. Accept raw Python values OR pre-encoded strings.
    def _json_encode_value(v):
        if isinstance(v, str):
            # Already a string — try to parse as JSON, if it works use as-is,
            # otherwise convert Python-style literals (True/False/None)
            try:
                json.loads(v)
                return v
            except (json.JSONDecodeError, TypeError):
                lower = v.strip().lower()
                if lower == "true":  return "true"
                if lower == "false": return "false"
                if lower == "none" or lower == "null": return "null"
                # Try numeric
                try:
                    float(v); return v
                except ValueError:
                    return json.dumps(v)  # wrap as JSON string
        return json.dumps(v)

    value_json = _json_encode_value(value)
    prev_json = _json_encode_value(previous_value) if previous_value else ""

    try:
        # Deactivate prior active override for this param so there's never
        # ambiguity about which row _load_overrides() should see.
        conn = sqlite3.connect(db, timeout=10)
        conn.execute(
            "UPDATE tuning_overrides SET active = 0 WHERE param = ? AND active = 1",
            (param,),
        )
        cursor = conn.execute(
            """INSERT INTO tuning_overrides
               (param, value, previous_value, reason, backtest_result,
                approved_by, approved_at, active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (param, value_json, prev_json, reason,
             bt_json, approved_by, now, now),
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        logger.info("[TUNING] Logged: %s = %s (was %s) — %s",
                     param, value, previous_value, reason[:80])
        return row_id
    except Exception as e:
        logger.error("[TUNING] Failed to log change %s: %s", param, e)
        return -1


def deactivate_tuning(param: str, reason: str = "", db_path: str = None) -> bool:
    """Mark a tuning override as inactive (rolled back)."""
    db = db_path or _TRADING_FOREX_DB
    try:
        conn = sqlite3.connect(db, timeout=10)
        conn.execute(
            "UPDATE tuning_overrides SET active = 0 WHERE param = ? AND active = 1",
            (param,),
        )
        conn.commit()
        conn.close()
        logger.info("[TUNING] Deactivated: %s — %s", param, reason)
        return True
    except Exception as e:
        logger.error("[TUNING] Failed to deactivate %s: %s", param, e)
        return False


def get_active_tuning(db_path: str = None) -> dict:
    """Return all active tuning overrides as {param: value}."""
    db = db_path or _TRADING_FOREX_DB
    try:
        conn = sqlite3.connect(db, timeout=10)
        rows = conn.execute(
            "SELECT param, value FROM tuning_overrides WHERE active = 1"
        ).fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception as e:
        logger.error("[TUNING] Failed to read active tuning: %s", e)
        return {}
