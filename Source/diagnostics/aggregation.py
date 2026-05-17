"""Multi-dimensional performance rollups.

Pool-managed connections (db_pool.get_trading_forex) are thread-local and
cached; we do NOT close them. Lifecycle is owned by the pool. Matches the
pattern established in diagnostics.context (A1).
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from db_pool import get_trading_forex

from diagnostics.context import Window

VALID_DIMENSIONS = {
    "pair", "source", "session", "setup_code", "direction",
    "fan_state", "entry_setup_type", "regime", "hour", "day_of_week",
}


@dataclass
class RollupRow:
    key: Tuple[Any, ...]
    dim_names: Tuple[str, ...]
    n: int
    wins: int
    losses: int
    win_rate: float
    avg_pips: float
    total_pips: float
    profit_factor: float      # gross_win / |gross_loss|
    expectancy: float          # avg_win × win_rate − avg_loss × (1-win_rate)
    exit_trigger_dist: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": dict(zip(self.dim_names, self.key)),
            "n": self.n,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "avg_pips": round(self.avg_pips, 2),
            "total_pips": round(self.total_pips, 1),
            "profit_factor": (
                round(self.profit_factor, 2)
                if self.profit_factor != float("inf") else "inf"
            ),
            "expectancy": round(self.expectancy, 2),
            "exit_trigger_dist": self.exit_trigger_dist,
        }


def _dim_expr(dim: str) -> str:
    """Map dimension name to SQL expression.

    Regime thresholds match diagnostics.context.classify_regime:
    adx<15 -> ranging, adx<25 -> weak_trend, adx<35 -> strong_trend, else exhaustion.
    """
    if dim == "hour":
        return "CAST(strftime('%H', entry_time) AS INTEGER)"
    if dim == "day_of_week":
        return "CAST(strftime('%w', entry_time) AS INTEGER)"
    if dim == "regime":
        return (
            "CASE "
            "WHEN adx < 15 THEN 'ranging' "
            "WHEN adx < 25 THEN 'weak_trend' "
            "WHEN adx < 35 THEN 'strong_trend' "
            "ELSE 'exhaustion' "
            "END"
        )
    return dim


def rollup(
    window: Window,
    dimensions: List[str],
    min_trades: int = 3,
) -> List[RollupRow]:
    """Aggregate live_trades in window over dimensions. Returns sorted by total_pips desc."""
    bad = set(dimensions) - VALID_DIMENSIONS
    if bad:
        raise ValueError(f"Invalid dimensions: {bad}. Valid: {VALID_DIMENSIONS}")

    dim_exprs = [f"{_dim_expr(d)} AS {d}" for d in dimensions]
    group_by = ", ".join(_dim_expr(d) for d in dimensions)

    sql = f"""
        SELECT
            {', '.join(dim_exprs)},
            COUNT(*) AS n,
            SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
            AVG(pnl_pips) AS avg_pips,
            SUM(pnl_pips) AS total_pips,
            SUM(CASE WHEN pnl_pips > 0 THEN pnl_pips ELSE 0 END) AS gross_win,
            SUM(CASE WHEN pnl_pips < 0 THEN pnl_pips ELSE 0 END) AS gross_loss,
            exit_trigger
        FROM live_trades
        WHERE {window.to_sql_clause('exit_time')}
          AND exit_time IS NOT NULL
          AND pnl_pips IS NOT NULL
        GROUP BY {group_by}, exit_trigger
    """
    # Post-aggregate across exit_trigger values in Python to keep rollup rows clean.
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql).fetchall()

    buckets: Dict[Tuple, Dict[str, Any]] = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0, "total_pips": 0.0,
        "gross_win": 0.0, "gross_loss": 0.0,
        "exit_dist": defaultdict(int),
    })
    for r in rows:
        key = tuple(r[d] for d in dimensions)
        b = buckets[key]
        b["n"] += r["n"]
        b["wins"] += r["wins"] or 0
        b["losses"] += r["losses"] or 0
        b["total_pips"] += r["total_pips"] or 0.0
        b["gross_win"] += r["gross_win"] or 0.0
        b["gross_loss"] += r["gross_loss"] or 0.0
        if r["exit_trigger"]:
            b["exit_dist"][r["exit_trigger"]] += r["n"]

    out: List[RollupRow] = []
    for key, b in buckets.items():
        if b["n"] < min_trades:
            continue
        wr = b["wins"] / b["n"] if b["n"] else 0.0
        avg = b["total_pips"] / b["n"] if b["n"] else 0.0
        avg_win = (b["gross_win"] / b["wins"]) if b["wins"] else 0.0
        avg_loss = (abs(b["gross_loss"]) / b["losses"]) if b["losses"] else 0.0
        expectancy = avg_win * wr - avg_loss * (1 - wr)
        pf = (b["gross_win"] / abs(b["gross_loss"])) if b["gross_loss"] else float("inf")
        out.append(RollupRow(
            key=key,
            dim_names=tuple(dimensions),
            n=b["n"],
            wins=b["wins"],
            losses=b["losses"],
            win_rate=wr,
            avg_pips=avg,
            total_pips=b["total_pips"],
            profit_factor=pf,
            expectancy=expectancy,
            exit_trigger_dist=dict(b["exit_dist"]),
        ))
    out.sort(key=lambda r: r.total_pips, reverse=True)
    return out


def exit_trigger_distribution(
    window: Window,
    groupby: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Count of exit_trigger values in window, optionally grouped by dimensions."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    if groupby:
        bad = set(groupby) - VALID_DIMENSIONS
        if bad:
            raise ValueError(f"Invalid groupby dimensions: {bad}. Valid: {VALID_DIMENSIONS}")
        dims = ", ".join(_dim_expr(d) for d in groupby)
        select_dims = ", ".join(f"{_dim_expr(d)} AS {d}" for d in groupby)
        sql = f"""
            SELECT {select_dims}, exit_trigger, COUNT(*) AS n,
                   SUM(pnl_pips) AS total_pips
            FROM live_trades
            WHERE {window.to_sql_clause('exit_time')}
              AND exit_time IS NOT NULL
            GROUP BY {dims}, exit_trigger
            ORDER BY n DESC
        """
        rows = conn.execute(sql).fetchall()
        out: Dict[Tuple, Dict[str, Any]] = defaultdict(dict)
        for r in rows:
            key = tuple(r[d] for d in groupby)
            out[key][r["exit_trigger"] or "unknown"] = {
                "n": r["n"], "total_pips": r["total_pips"] or 0.0,
            }
        return {str(k): v for k, v in out.items()}
    sql = f"""
        SELECT exit_trigger, COUNT(*) AS n
        FROM live_trades
        WHERE {window.to_sql_clause('exit_time')}
          AND exit_time IS NOT NULL
        GROUP BY exit_trigger
        ORDER BY n DESC
    """
    rows = conn.execute(sql).fetchall()
    return {(r["exit_trigger"] or "unknown"): r["n"] for r in rows}


def confluence_calibration(
    window: Window,
    buckets: Optional[List[Tuple[float, float]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Bucket by confluence_score; return actual WR per bucket.

    Default buckets: [(0,40), (40,55), (55,70), (70,85), (85,101)].
    """
    buckets = buckets or [(0, 40), (40, 55), (55, 70), (70, 85), (85, 101)]
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    out: Dict[str, Dict[str, Any]] = {}
    for lo, hi in buckets:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
                   AVG(pnl_pips) AS avg_pips
            FROM live_trades
            WHERE {window.to_sql_clause('exit_time')}
              AND exit_time IS NOT NULL
              AND confluence_score >= ? AND confluence_score < ?
            """,
            (lo, hi),
        ).fetchone()
        n = row["n"] or 0
        out[f"{lo:.0f}-{hi:.0f}"] = {
            "n": n,
            "win_rate": (row["wins"] / n) if n else 0.0,
            "avg_pips": row["avg_pips"] or 0.0,
        }
    return out


def rolling_metrics(window: Window, roll_days: int = 7) -> List[Dict[str, Any]]:
    """For each day in window, compute trailing N-day WR + avg_pips."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT DATE(exit_time) AS d,
               COUNT(*) AS n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
               AVG(pnl_pips) AS avg_pips,
               SUM(pnl_pips) AS total_pips
        FROM live_trades
        WHERE {window.to_sql_clause('exit_time')}
          AND exit_time IS NOT NULL
        GROUP BY DATE(exit_time)
        ORDER BY d
        """
    ).fetchall()
    daily = [dict(r) for r in rows]
    out: List[Dict[str, Any]] = []
    for i, day in enumerate(daily):
        window_slice = daily[max(0, i - roll_days + 1): i + 1]
        n = sum(x["n"] for x in window_slice)
        wins = sum(x["wins"] for x in window_slice)
        total = sum(x["total_pips"] or 0.0 for x in window_slice)
        out.append({
            "date": day["d"],
            "trailing_days": len(window_slice),
            "n": n,
            "win_rate": (wins / n) if n else 0.0,
            "avg_pips": (total / n) if n else 0.0,
            "total_pips": total,
        })
    return out
