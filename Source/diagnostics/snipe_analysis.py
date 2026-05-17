"""Snipe + watch lifecycle analysis, origin-aware.

Answers:
 - Where do watches die? (funnel: created → triggered → executed → win)
 - Which origin is the cleanest source of snipes? (scout vs chart vs kronos_direct)
 - Which condition hashes are the worst/best performers? (leaderboard)
 - How long do watches sit before triggering? (age-at-trigger distribution)
 - Which watches time out without ever triggering? (never-armed list)

Pool-managed connections (db_pool.get_trading_forex) are thread-local and
cached; we do NOT close them. Lifecycle is owned by the pool. Matches
pattern established in diagnostics.context (A1) and diagnostics.live_health (A2).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex

from diagnostics.context import Window


@dataclass
class WatchFunnel:
    """One bucket of the watch-lifecycle funnel.

    Rates:
     - trigger_rate   = triggered / created
     - execution_rate = executed / triggered
     - win_rate       = winners / executed
    """
    created: int
    still_watching: int
    expired: int
    triggered: int
    executed: int
    winners: int
    losers: int
    trigger_rate: float
    execution_rate: float
    win_rate: float

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in [
            "created", "still_watching", "expired", "triggered", "executed",
            "winners", "losers", "trigger_rate", "execution_rate", "win_rate",
        ]}


def watch_funnel(
    window: Window,
    groupby: Optional[List[str]] = None,
) -> Dict[str, WatchFunnel]:
    """Tally created/still_watching/expired/triggered/executed/winners/losers.

    With no `groupby`, returns `{"overall": WatchFunnel}`. With `groupby`
    (e.g. ["instrument"] or ["origin_type", "suggestion_type"]), returns one
    WatchFunnel per distinct tuple keyed by `str(tuple(...))`.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    if groupby:
        select_dims = ", ".join(groupby)
        group_clause = f"GROUP BY {select_dims}"
    else:
        select_dims = "'overall' AS bucket"
        group_clause = ""
    sql = f"""
        SELECT {select_dims},
               COUNT(*) AS created,
               SUM(CASE WHEN status='watching' THEN 1 ELSE 0 END) AS still_watching,
               SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired,
               SUM(CASE WHEN triggered_at IS NOT NULL THEN 1 ELSE 0 END) AS triggered,
               SUM(CASE WHEN trade_cycle_id IS NOT NULL THEN 1 ELSE 0 END) AS executed,
               SUM(CASE WHEN trade_outcome='win' THEN 1 ELSE 0 END) AS winners,
               SUM(CASE WHEN trade_outcome='loss' THEN 1 ELSE 0 END) AS losers
        FROM watch_suggestions
        WHERE {window.to_sql_clause('created_at')}
        {group_clause}
    """
    rows = conn.execute(sql).fetchall()
    out: Dict[str, WatchFunnel] = {}
    for r in rows:
        key = "overall" if not groupby else str(tuple(r[d] for d in groupby))
        created = r["created"] or 0
        triggered = r["triggered"] or 0
        executed = r["executed"] or 0
        winners = r["winners"] or 0
        out[key] = WatchFunnel(
            created=created,
            still_watching=r["still_watching"] or 0,
            expired=r["expired"] or 0,
            triggered=triggered,
            executed=executed,
            winners=winners,
            losers=r["losers"] or 0,
            trigger_rate=triggered / created if created else 0.0,
            execution_rate=executed / triggered if triggered else 0.0,
            win_rate=winners / executed if executed else 0.0,
        )
    return out


def snipe_quality_by_origin(window: Window) -> List[Dict[str, Any]]:
    """Breakdown per origin_type (from watch_suggestions) joined with trade outcomes.

    Appends a synthetic `kronos_direct` row for kronos_hunter trades — kronos
    takes direct entries (no watch), so its quality can't be measured through
    watch_suggestions.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"""
        SELECT ws.origin_type AS origin,
               COUNT(*) AS n,
               SUM(CASE WHEN ws.trade_outcome='win' THEN 1 ELSE 0 END) AS wins,
               AVG(ws.pips_result) AS avg_pips,
               AVG(
                    CASE WHEN ws.triggered_at IS NOT NULL
                    THEN (julianday(ws.triggered_at) - julianday(ws.created_at)) * 24
                    END
               ) AS avg_age_hours_trigger,
               SUM(CASE WHEN ws.stale_flagged_at IS NOT NULL
                       AND ws.triggered_at > ws.stale_flagged_at
                       THEN 1 ELSE 0 END) AS stale_triggers
        FROM watch_suggestions ws
        WHERE {window.to_sql_clause('ws.created_at')}
          AND ws.trade_cycle_id IS NOT NULL
        GROUP BY ws.origin_type
        ORDER BY n DESC
    """).fetchall()
    # Kronos is direct (no watch) — query live_trades separately and append.
    k_row = conn.execute(f"""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
               AVG(pnl_pips) AS avg_pips
        FROM live_trades
        WHERE {window.to_sql_clause('exit_time')}
          AND source = 'kronos_hunter'
          AND exit_time IS NOT NULL
    """).fetchone()

    out: List[Dict[str, Any]] = []
    for r in rows:
        n = r["n"] or 0
        out.append({
            "origin": r["origin"] or "unspecified",
            "n": n,
            "win_rate": (r["wins"] or 0) / n if n else 0.0,
            "avg_pips": r["avg_pips"] or 0.0,
            "avg_age_hours_at_trigger": r["avg_age_hours_trigger"] or 0.0,
            "stale_triggers": r["stale_triggers"] or 0,
        })
    if k_row and (k_row["n"] or 0) > 0:
        n = k_row["n"]
        out.append({
            "origin": "kronos_direct",
            "n": n,
            "win_rate": (k_row["wins"] or 0) / n,
            "avg_pips": k_row["avg_pips"] or 0.0,
            "avg_age_hours_at_trigger": 0.0,   # direct trade — no watch phase
            "stale_triggers": 0,
        })
    return out


def condition_hash_leaderboard(
    min_triggers: int = 3,
    window: Optional[Window] = None,
) -> List[Dict[str, Any]]:
    """Worst-first leaderboard from the pre-aggregated snipe_leaderboard table.

    Plan column names corrected to match actual schema:
     - `conditions_hash` (plural, not `condition_hash`)
     - `times_won` (not `wins`)
     - `avg_pips` (not `avg_pnl_pips`)
     - `suggestion_type` (not `setup_type`)
     - `losses` is computed as `times_triggered - times_won` (no stored column)
     - `direction` does not exist on snipe_leaderboard — omitted.

    `window` is accepted for API symmetry but the aggregate table has no
    windowable timestamp columns suitable for filtering per-period; keep the
    parameter so callers can migrate if a `last_triggered_at` filter is
    added later.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT conditions_hash, instrument, suggestion_type,
               times_triggered, times_won,
               (times_triggered - times_won) AS losses,
               win_rate, avg_pips, total_pips
        FROM snipe_leaderboard
        WHERE times_triggered >= ?
        ORDER BY win_rate ASC
    """, (min_triggers,)).fetchall()
    return [dict(r) for r in rows]


def time_to_trigger_distribution(
    window: Window,
    groupby: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Avg/min/max watch-age (hours) at trigger.

    With no `groupby`, returns `{"overall": {...}}`. With `groupby`, one entry
    per bucket keyed by `str(tuple(...))`.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    if groupby:
        select_dims = ", ".join(groupby)
        group_clause = f"GROUP BY {select_dims}"
    else:
        select_dims = "'overall' AS bucket"
        group_clause = ""
    rows = conn.execute(f"""
        SELECT {select_dims},
               COUNT(*) AS n,
               AVG((julianday(triggered_at) - julianday(created_at)) * 24) AS avg_hrs,
               MIN((julianday(triggered_at) - julianday(created_at)) * 24) AS min_hrs,
               MAX((julianday(triggered_at) - julianday(created_at)) * 24) AS max_hrs
        FROM watch_suggestions
        WHERE {window.to_sql_clause('created_at')}
          AND triggered_at IS NOT NULL
        {group_clause}
    """).fetchall()
    out: Dict[str, Any] = {}
    for r in rows:
        key = "overall" if not groupby else str(tuple(r[d] for d in groupby))
        out[key] = {
            "n": r["n"] or 0,
            "avg_hours": r["avg_hrs"] or 0.0,
            "min_hours": r["min_hrs"] or 0.0,
            "max_hours": r["max_hrs"] or 0.0,
        }
    # Ensure at least an empty "overall" bucket is returned (callers expect the key).
    if not groupby and "overall" not in out:
        out["overall"] = {"n": 0, "avg_hours": 0.0, "min_hours": 0.0, "max_hours": 0.0}
    return out


def watches_that_timed_out(window: Window) -> List[Dict[str, Any]]:
    """Watches that expired without ever triggering, grouped by pair/type/origin.

    Useful for spotting systematic over-eager criteria (e.g. a scout variant
    that stamps lots of watches but never sees any trigger).
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"""
        SELECT instrument, suggestion_type, origin_type,
               COUNT(*) AS n,
               AVG(peak_progress) AS avg_peak_progress
        FROM watch_suggestions
        WHERE {window.to_sql_clause('created_at')}
          AND status = 'expired'
          AND triggered_at IS NULL
        GROUP BY instrument, suggestion_type, origin_type
        ORDER BY n DESC
    """).fetchall()
    return [dict(r) for r in rows]
