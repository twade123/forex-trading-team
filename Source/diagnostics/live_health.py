"""Live health checks — is anything broken right now?

Answers "is anything broken right now?" in one call. Checks daemon heartbeats,
pipeline stage freshness, spread health, and watches near trigger.

Pool-managed connections (db_pool.get_trading_forex / get_flight_recorder)
are thread-local and cached; we do NOT close them. Lifecycle is owned by
the pool. Matches the pattern established in diagnostics.context (A1).
"""
from __future__ import annotations

import json as _json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex, get_flight_recorder

_UTC = timezone.utc

CRITICAL_STALENESS_S = {
    "kronos_hunter": 1800,        # 30 min (15-min cadence × 2)
    "scout_scan": 600,            # 10 min (5-min cadence × 2)
    "guardian_tick": 180,         # 3 min (60s cadence × 3)
    "tuning_measurement": 25200,  # 7h (6h cadence + 1h grace)
}

PIPELINE_STAGES = [
    "scout_scan", "scout_alert", "validator_verdict",
    "trade_phase", "guardian_threat", "guardian_action", "trade_close",
    "kronos_hunter_scan_start", "kronos_hunter_signal", "kronos_filter_check",
]


@dataclass
class HealthStatus:
    """One component's liveness snapshot."""
    component: str
    severity: str                  # "ok" | "warn" | "critical"
    alive: bool
    last_heartbeat: Optional[datetime]
    staleness_seconds: Optional[float]
    details: Dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component": self.component,
            "severity": self.severity,
            "alive": self.alive,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "staleness_seconds": self.staleness_seconds,
            "details": self.details,
            "message": self.message,
        }


def _now() -> datetime:
    return datetime.now(_UTC)


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Ensure timezone-aware (assume UTC if naive) so arithmetic with _now() works.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt


def _severity_from_staleness(staleness_s: Optional[float], critical_s: float) -> str:
    if staleness_s is None:
        return "critical"
    if staleness_s > critical_s:
        return "critical"
    if staleness_s > critical_s * 0.6:
        return "warn"
    return "ok"


def check_kronos() -> HealthStatus:
    """Kronos hunter heartbeat = most-recent kronos_signals.anchor_time."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT anchor_time FROM kronos_signals ORDER BY anchor_time DESC LIMIT 1"
    ).fetchone()
    last = _parse_iso(row["anchor_time"]) if row else None
    staleness: Optional[float] = (_now() - last).total_seconds() if last else None
    sev = _severity_from_staleness(staleness, CRITICAL_STALENESS_S["kronos_hunter"])
    return HealthStatus(
        component="kronos_hunter",
        severity=sev,
        alive=last is not None and (staleness or 0) < CRITICAL_STALENESS_S["kronos_hunter"],
        last_heartbeat=last,
        staleness_seconds=staleness,
        message=(
            f"Last kronos signal {staleness:.0f}s ago"
            if staleness is not None
            else "No kronos signals found"
        ),
    )


def check_guardian() -> HealthStatus:
    """Guardian = any guardian_threat entry in last 3 minutes (only if trades are open)."""
    conn_ft = get_trading_forex()
    conn_fl = get_flight_recorder()
    conn_ft.row_factory = sqlite3.Row
    conn_fl.row_factory = sqlite3.Row
    open_trades = conn_ft.execute(
        "SELECT COUNT(*) AS n FROM live_trades WHERE exit_time IS NULL"
    ).fetchone()["n"]
    if open_trades == 0:
        return HealthStatus(
            component="guardian",
            severity="ok",
            alive=True,
            last_heartbeat=None,
            staleness_seconds=None,
            details={"open_trades": 0},
            message="No open trades — guardian idle (expected)",
        )
    row = conn_fl.execute(
        "SELECT timestamp FROM flight_log WHERE stage = 'guardian_threat' "
        "ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    last = _parse_iso(row["timestamp"]) if row else None
    staleness: Optional[float] = (_now() - last).total_seconds() if last else None
    sev = _severity_from_staleness(staleness, CRITICAL_STALENESS_S["guardian_tick"])
    return HealthStatus(
        component="guardian",
        severity=sev,
        alive=staleness is not None and staleness < CRITICAL_STALENESS_S["guardian_tick"],
        last_heartbeat=last,
        staleness_seconds=staleness,
        details={"open_trades": open_trades},
        message=(
            f"Guardian last scored {staleness:.0f}s ago across {open_trades} open trades"
            if staleness is not None
            else f"No guardian_threat events despite {open_trades} open trades"
        ),
    )


def check_pipeline_stages(hours_back: int = 2) -> Dict[str, HealthStatus]:
    """For each expected stage, report last-seen + staleness.

    If a stage has never been seen, severity is "critical", last_heartbeat=None,
    staleness_seconds=None.
    """
    conn = get_flight_recorder()
    conn.row_factory = sqlite3.Row
    out: Dict[str, HealthStatus] = {}
    for stage in PIPELINE_STAGES:
        row = conn.execute(
            "SELECT timestamp FROM flight_log WHERE stage = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (stage,),
        ).fetchone()
        last = _parse_iso(row["timestamp"]) if row else None
        staleness: Optional[float] = (_now() - last).total_seconds() if last else None
        if staleness is None:
            sev = "critical"
        elif staleness < hours_back * 3600:
            sev = "ok"
        else:
            sev = "warn"
        out[stage] = HealthStatus(
            component=f"stage:{stage}",
            severity=sev,
            alive=last is not None,
            last_heartbeat=last,
            staleness_seconds=staleness,
            message=(
                f"Last {stage}: {staleness:.0f}s ago"
                if staleness is not None
                else f"No {stage} events"
            ),
        )
    return out


def check_daemons() -> Dict[str, HealthStatus]:
    """Aggregate daemon health: kronos + guardian + scheduler heartbeat.

    Scheduler heartbeat uses tuning_performance_snapshots.measured_at as a proxy
    (the tuning_measurement job runs every 6h).
    """
    out: Dict[str, HealthStatus] = {
        "kronos_hunter": check_kronos(),
        "guardian": check_guardian(),
    }
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT MAX(measured_at) AS last FROM tuning_performance_snapshots"
    ).fetchone()
    last = _parse_iso(row["last"]) if row and row["last"] else None
    staleness: Optional[float] = (_now() - last).total_seconds() if last else None
    out["scheduler"] = HealthStatus(
        component="scheduler",
        severity=_severity_from_staleness(staleness, CRITICAL_STALENESS_S["tuning_measurement"]),
        alive=staleness is not None and staleness < CRITICAL_STALENESS_S["tuning_measurement"],
        last_heartbeat=last,
        staleness_seconds=staleness,
        message=(
            f"Last tuning snapshot {staleness:.0f}s ago"
            if staleness is not None
            else "No tuning snapshots"
        ),
    )
    return out


def check_spreads() -> Dict[str, HealthStatus]:
    """Spread-health check via PRICING_SNAPSHOT flight_log entries in last 5 minutes.

    Returns {} gracefully if no PRICING_SNAPSHOT entries exist (the stage is not
    guaranteed to be emitted by the current pipeline).

    NOTE: As of 2026-04, no PRICING_SNAPSHOT stage is emitted by the pipeline —
    this check will return {} until such stage is logged. Kept for forward
    compatibility; no blocking issue.
    """
    conn = get_flight_recorder()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT timestamp, data FROM flight_log
        WHERE stage = 'PRICING_SNAPSHOT'
          AND timestamp >= datetime('now', '-5 minutes')
        ORDER BY timestamp DESC
        """
    ).fetchall()
    out: Dict[str, HealthStatus] = {}
    for r in rows:
        try:
            d = _json.loads(r["data"]) if r["data"] else {}
        except (_json.JSONDecodeError, TypeError):
            continue
        pair = d.get("pair")
        spread = d.get("spread_pips")
        normal = d.get("normal_spread_pips")
        if not pair or pair in out or spread is None or normal is None:
            continue
        ratio = spread / normal if normal > 0 else float("inf")
        sev = "critical" if ratio > 5 else "warn" if ratio > 2.5 else "ok"
        out[pair] = HealthStatus(
            component=f"spread:{pair}",
            severity=sev,
            alive=True,
            last_heartbeat=_parse_iso(r["timestamp"]),
            staleness_seconds=0,
            details={"spread_pips": spread, "normal_pips": normal, "ratio": ratio},
            message=f"{pair} spread {spread:.1f}p ({ratio:.1f}x normal)",
        )
    return out


def check_watches_near_trigger(progress_min: float = 0.80) -> List[Dict[str, Any]]:
    """Watches with status='watching' and peak_progress >= progress_min.

    Returns raw dict rows (not HealthStatus) — these are operational handoffs
    for a human / higher-level aggregator, not daemon liveness.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id AS watch_id, instrument AS pair, direction, origin_type,
               suggestion_type, peak_progress, conditions_met_count,
               conditions_total_count, created_at, last_checked_at
        FROM watch_suggestions
        WHERE status = 'watching'
          AND peak_progress >= ?
        ORDER BY peak_progress DESC
        """,
        (progress_min,),
    ).fetchall()
    return [dict(r) for r in rows]


def check_all() -> List[HealthStatus]:
    """Every check, sorted critical -> warn -> ok."""
    statuses: List[HealthStatus] = []
    statuses.extend(check_daemons().values())
    statuses.extend(check_pipeline_stages(hours_back=2).values())
    statuses.extend(check_spreads().values())
    order = {"critical": 0, "warn": 1, "ok": 2}
    statuses.sort(key=lambda s: order.get(s.severity, 9))
    return statuses
