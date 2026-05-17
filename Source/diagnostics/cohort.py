"""Cohort comparison: pre/post tuning change impact.

For a given tuning_overrides param, compute win_rate and avg_pips in
[cutover - window, cutover) vs [cutover, cutover + window) and classify
the delta as positive / negative / insignificant / insufficient_data.

Pool-managed connections (db_pool.get_trading_forex) are thread-local and
cached; we do NOT close them. Lifecycle is owned by the pool. Matches the
pattern established across diagnostics.* modules.

tuning_overrides.created_at uses 6-digit microsecond precision (safe for
datetime.fromisoformat). No nanosecond-truncation needed for this table.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex

_UTC = timezone.utc


@dataclass
class CohortComparison:
    param: str
    tuning_change: str
    cutover: str
    before_n: int
    after_n: int
    before_wr: float
    after_wr: float
    before_avg_pips: float
    after_avg_pips: float
    wr_delta_pp: float
    avg_pips_delta: float
    verdict: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "param": self.param,
            "tuning_change": self.tuning_change,
            "cutover": self.cutover,
            "before_n": self.before_n,
            "after_n": self.after_n,
            "before_wr": round(self.before_wr, 4),
            "after_wr": round(self.after_wr, 4),
            "before_avg_pips": round(self.before_avg_pips, 2),
            "after_avg_pips": round(self.after_avg_pips, 2),
            "wr_delta_pp": round(self.wr_delta_pp, 2),
            "avg_pips_delta": round(self.avg_pips_delta, 2),
            "verdict": self.verdict,
        }


def _verdict(wr_delta_pp: float, n_min: int, before_n: int, after_n: int) -> str:
    if before_n < n_min or after_n < n_min:
        return "insufficient_data"
    if wr_delta_pp >= 5:
        return "positive"
    if wr_delta_pp <= -5:
        return "negative"
    return "insignificant"


def compare_around(
    param: str,
    window_hours: int = 168,
    min_trades: int = 10,
) -> Optional[CohortComparison]:
    """Compare trade performance before/after the most recent active cutover for `param`.

    Returns None when no active tuning_overrides row exists for `param`.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row

    ov = conn.execute(
        """
        SELECT param, value, previous_value, created_at
        FROM tuning_overrides
        WHERE param = ? AND active = 1
        ORDER BY created_at DESC LIMIT 1
        """,
        (param,),
    ).fetchone()
    if not ov:
        return None

    cutover = datetime.fromisoformat(ov["created_at"].replace("Z", "+00:00"))
    before_start = cutover - timedelta(hours=window_hours)
    after_end = cutover + timedelta(hours=window_hours)

    def _stats(start: datetime, end: datetime) -> Dict[str, Any]:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
                   AVG(pnl_pips) AS avg_pips
            FROM live_trades
            WHERE exit_time >= ? AND exit_time < ? AND exit_time IS NOT NULL
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        n = row["n"] or 0
        return {
            "n": n,
            "wr": ((row["wins"] or 0) / n) if n else 0.0,
            "avg_pips": row["avg_pips"] or 0.0,
        }

    before = _stats(before_start, cutover)
    after = _stats(cutover, after_end)
    wr_delta = (after["wr"] - before["wr"]) * 100

    return CohortComparison(
        param=param,
        tuning_change=f"{ov['previous_value']}→{ov['value']}",
        cutover=ov["created_at"],
        before_n=before["n"],
        after_n=after["n"],
        before_wr=before["wr"],
        after_wr=after["wr"],
        before_avg_pips=before["avg_pips"],
        after_avg_pips=after["avg_pips"],
        wr_delta_pp=wr_delta,
        avg_pips_delta=after["avg_pips"] - before["avg_pips"],
        verdict=_verdict(wr_delta, min_trades, before["n"], after["n"]),
    )


def all_recent_tuning_impacts(days: int = 14) -> List[CohortComparison]:
    """Every active tuning change in the last N days → CohortComparison."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT DISTINCT param FROM tuning_overrides
        WHERE active = 1
          AND created_at >= datetime('now', '-' || ? || ' days')
        """,
        (days,),
    ).fetchall()

    out: List[CohortComparison] = []
    for r in rows:
        comp = compare_around(r["param"], window_hours=days * 12)
        if comp:
            out.append(comp)
    return out
