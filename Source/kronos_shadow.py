"""Per-tick Kronos threat-score shadow logging.

Writes every kronos_hunter guardian tick's threat evaluation to
kronos_shadow_scores. At trade close, backfills outcome columns so the
table becomes a trade-id-keyed time series pairing threat scores with
eventual outcomes. Phase 5 threat-scorer rewrite uses this data.

Fails open on any DB error — logs a warning and returns. Never raises
into the guardian tick loop.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Mapping

logger = logging.getLogger(__name__)

DEFAULT_DB = "~/Jarvis/Database/v2/trading_forex.db"


def _pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair.upper() else 0.0001


def write_score(
    *,
    trade_id: str,
    pair: str,
    direction: str,
    tick_time: datetime,
    candles_in: int,
    threat: Mapping[str, Any],
    pnl_pips: float,
    r_multiple: float,
    peak_pnl_pips: float,
    market: Mapping[str, Any],
    db_path: str = DEFAULT_DB,
) -> None:
    """Insert one row into kronos_shadow_scores. Fails open on error."""
    try:
        ema = market.get("ema", {}) or {}
        emas = ema.get("current_emas", {}) or {}
        bb = market.get("bollinger", {}) or {}
        atr = (market.get("atr", {}) or {}).get("value", 0) or 0
        rsi = (market.get("rsi", {}) or {}).get("value")

        pip = _pip_size(pair)
        sign = 1.0 if direction == "buy" else -1.0
        entry_price = 0.0  # not available here; use EMA21 as reference
        e21 = emas.get("ema21") or 0
        e55 = emas.get("ema55") or 0
        e100 = emas.get("ema100") or 0

        # Compute distances (pips) relative to current price proxy
        # We don't have price here; approximate via pnl_pips → E21
        # Better approach: pass price, but keep signature stable for now.
        # These fields are best-effort; score + reasons are the core.
        dist_e21 = None
        dist_e55 = None
        dist_e100 = None

        bb_w = ((bb.get("upper", 0) or 0) - (bb.get("lower", 0) or 0)) / pip if bb.get("upper") else 0.0
        atr_p = (atr / pip) if atr else 0.0

        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("""
            INSERT INTO kronos_shadow_scores (
                trade_id, pair, direction, tick_time, candles_in,
                score, zone, reasons, layer_scores,
                pnl_pips, r_multiple, peak_pnl_pips,
                fan_direction, fan_state,
                dist_e21_pips, dist_e55_pips, dist_e100_pips,
                bb_width_pips, atr_pips, rsi
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id, pair, direction, tick_time.isoformat(), candles_in,
            int(threat.get("score", threat.get("threat_level", 0))),
            threat.get("zone", "GREEN"),
            json.dumps(threat.get("reasons", [])),
            json.dumps(threat.get("layer_scores", {})),
            pnl_pips, r_multiple, peak_pnl_pips,
            ema.get("fan_direction"), ema.get("fan_state"),
            dist_e21, dist_e55, dist_e100,
            bb_w, atr_p, rsi,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("kronos_shadow write_score failed for %s: %s", trade_id, e)


def update_outcome(
    *,
    trade_id: str,
    outcome: str,
    final_pnl: float,
    final_exit_trigger: str,
    db_path: str = DEFAULT_DB,
) -> None:
    """Backfill trade_outcome / final_pnl_pips / final_exit_trigger on all rows
    for this trade_id. Called once at trade close. Fails open on error."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("""
            UPDATE kronos_shadow_scores
            SET trade_outcome = ?, final_pnl_pips = ?, final_exit_trigger = ?
            WHERE trade_id = ?
        """, (outcome, final_pnl, final_exit_trigger, trade_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("kronos_shadow update_outcome failed for %s: %s", trade_id, e)
