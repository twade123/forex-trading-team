"""Scout scanning-pipeline health.

Four analytics tracking the scout's scan cadence and downstream funnel:
scan cadence (expected 12/hour per the 5-minute cycle), findings-per-scan
distribution (scout_alert events between scans), watch-creation health by
origin + session, and setup-type quality via watch_suggestions → outcomes.

Pool-managed connections (db_pool.get_trading_forex / get_flight_recorder) are
thread-local and cached; we do NOT close them. Lifecycle is owned by the pool.
Matches pattern established in diagnostics.context (A1) and all sibling
diagnostics modules (A2-A11).

Flight-log stage names are LOWERCASE in this schema — `scout_scan` (17k+ rows/7d)
and `scout_alert` (~300 rows/7d). There is no `scout_finding` stage in flight_log;
`scout_findings` is a separate table read by scout_quality.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, List

from db_pool import get_trading_forex, get_flight_recorder

from diagnostics.context import Window


def scan_cadence(window: Window) -> Dict[str, Any]:
    """Scout scan frequency vs expected (12/hour = every 5 minutes).

    Counts `scout_scan` events in flight_log, computes per-hour rate, and
    flags gaps exceeding 15 minutes (3x the expected 5-minute cadence) as
    likely scheduler hiccups.
    """
    conn = get_flight_recorder()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"""
        SELECT timestamp FROM flight_log
        WHERE stage = 'scout_scan'
          AND {window.to_sql_clause('timestamp')}
        ORDER BY timestamp
    """).fetchall()
    if not rows:
        return {
            "n_scans": 0,
            "scans_per_hour": 0.0,
            "expected_per_hour": 12.0,
            "gaps_over_15min": 0,
            "max_gap_minutes": 0.0,
        }
    times = [datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) for r in rows]
    hours = (window.end - window.start).total_seconds() / 3600
    gaps = [(times[i + 1] - times[i]).total_seconds() / 60 for i in range(len(times) - 1)]
    return {
        "n_scans": len(times),
        "scans_per_hour": len(times) / hours if hours else 0.0,
        "expected_per_hour": 12.0,
        "gaps_over_15min": sum(1 for g in gaps if g > 15),
        "max_gap_minutes": max(gaps) if gaps else 0.0,
    }


def findings_per_scan(window: Window) -> Dict[str, Any]:
    """Count `scout_alert` events between each adjacent pair of `scout_scan` events.

    Buckets scans into {0 findings, 1-3, 4+} to show the distribution shape.
    A healthy scout produces alerts sporadically — mostly 0, occasional 1-3.
    A flood of 4+ per interval suggests threshold calibration issues.

    (Plan originally listed `SCOUT_FINDING`/`SCOUT_ALERT`; only `scout_alert`
    exists as a flight_log stage — `scout_findings` is a separate table.)
    """
    conn = get_flight_recorder()
    conn.row_factory = sqlite3.Row
    scans = conn.execute(f"""
        SELECT timestamp FROM flight_log
        WHERE stage = 'scout_scan'
          AND {window.to_sql_clause('timestamp')}
        ORDER BY timestamp
    """).fetchall()
    findings = conn.execute(f"""
        SELECT timestamp FROM flight_log
        WHERE stage = 'scout_alert'
          AND {window.to_sql_clause('timestamp')}
        ORDER BY timestamp
    """).fetchall()
    f_times = [f["timestamp"] for f in findings]
    counts: List[int] = []
    for i in range(len(scans) - 1):
        start = scans[i]["timestamp"]
        end = scans[i + 1]["timestamp"]
        n = sum(1 for t in f_times if start <= t < end)
        counts.append(n)
    dist = {"0": 0, "1-3": 0, "4+": 0}
    for c in counts:
        bucket = "0" if c == 0 else "1-3" if c <= 3 else "4+"
        dist[bucket] += 1
    return {
        "n_scan_intervals": len(counts),
        "distribution": dist,
        "avg_findings_per_scan": sum(counts) / len(counts) if counts else 0.0,
    }


def watch_creation_health(window: Window) -> Dict[str, Any]:
    """Break down watch_suggestions creation by origin_type and trading session.

    origin_type values seen in the DB include `scout`, `user_requested`,
    `validator_structured`, `replay_unknown`, `kronos_path_snipe`, plus NULL.
    Session buckets use UTC hour ranges (rough proxy — London/NY overlap is
    12-17 UTC but split arbitrarily here for single-bucket attribution).
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    per_origin = conn.execute(f"""
        SELECT origin_type, COUNT(*) AS n
        FROM watch_suggestions
        WHERE {window.to_sql_clause('created_at')}
        GROUP BY origin_type
        ORDER BY n DESC
    """).fetchall()
    per_session = conn.execute(f"""
        SELECT
            CASE
                WHEN CAST(strftime('%H', created_at) AS INT) BETWEEN 8 AND 12 THEN 'London'
                WHEN CAST(strftime('%H', created_at) AS INT) BETWEEN 13 AND 17 THEN 'NY'
                WHEN CAST(strftime('%H', created_at) AS INT) BETWEEN 0 AND 7 THEN 'Asian'
                ELSE 'Sydney'
            END AS session,
            COUNT(*) AS n
        FROM watch_suggestions
        WHERE {window.to_sql_clause('created_at')}
        GROUP BY session
    """).fetchall()
    return {
        "per_origin": {(r["origin_type"] or "unspecified"): r["n"] for r in per_origin},
        "per_session": {r["session"]: r["n"] for r in per_session},
    }


def setup_type_quality(window: Window, min_watches: int = 5) -> List[Dict[str, Any]]:
    """Per watch_suggestions.suggestion_type: creation count, trigger rate,
    win rate (when trade_outcome populated), and avg pips.

    Ordered by win_rate descending (NULLs last via NULLIF on triggered=0).
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"""
        SELECT suggestion_type AS setup_type,
               COUNT(*) AS watches_created,
               SUM(CASE WHEN triggered_at IS NOT NULL THEN 1 ELSE 0 END) AS triggered,
               SUM(CASE WHEN trade_outcome='win' THEN 1 ELSE 0 END) AS wins,
               AVG(pips_result) AS avg_pips
        FROM watch_suggestions
        WHERE {window.to_sql_clause('created_at')}
          AND suggestion_type IS NOT NULL
        GROUP BY suggestion_type
        HAVING watches_created >= ?
        ORDER BY wins * 1.0 / NULLIF(triggered, 0) DESC
    """, (min_watches,)).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        watches = r["watches_created"] or 0
        trig = r["triggered"] or 0
        wins = r["wins"] or 0
        out.append({
            "setup_type": r["setup_type"],
            "watches_created": watches,
            "triggered": trig,
            "trigger_rate": trig / watches if watches else 0.0,
            "wins": wins,
            "win_rate": wins / trig if trig else 0.0,
            "avg_pips": r["avg_pips"] or 0.0,
        })
    return out
