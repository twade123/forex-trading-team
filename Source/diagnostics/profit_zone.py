"""Profit-zone clustering + MFE/peak efficiency analysis.

Pool-managed connections (db_pool.get_trading_forex) are thread-local and
cached; we do NOT close them. Lifecycle is owned by the pool. Matches the
pattern established in diagnostics.context / diagnostics.aggregation.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex

from diagnostics.aggregation import _dim_expr, VALID_DIMENSIONS
from diagnostics.context import Window


@dataclass
class ProfitZoneCluster:
    key: Dict[str, Any]
    n: int
    total_pips: float
    avg_pips: float
    win_rate: float
    mfe_capture_ratio: float
    rank: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "n": self.n,
            "total_pips": round(self.total_pips, 1),
            "avg_pips": round(self.avg_pips, 2),
            "win_rate": round(self.win_rate, 4),
            "mfe_capture_ratio": round(self.mfe_capture_ratio, 3),
            "rank": self.rank,
        }


def top_clusters(
    window: Window,
    dimensions: List[str],
    top_n: int = 10,
    min_trades: int = 3,
) -> List[ProfitZoneCluster]:
    """Rank dimension-combinations by total_pips desc. Returns top_n clusters."""
    bad = set(dimensions) - VALID_DIMENSIONS
    if bad:
        raise ValueError(f"Invalid dimensions: {bad}. Valid: {VALID_DIMENSIONS}")
    dim_exprs = ", ".join(f"{_dim_expr(d)} AS {d}" for d in dimensions)
    group_by = ", ".join(_dim_expr(d) for d in dimensions)
    sql = f"""
        SELECT {dim_exprs},
               COUNT(*) AS n,
               SUM(pnl_pips) AS total_pips,
               AVG(pnl_pips) AS avg_pips,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS wr,
               AVG(CASE
                    WHEN max_favorable_excursion_pips > 0 AND pnl_pips > 0
                    THEN pnl_pips * 1.0 / max_favorable_excursion_pips
                    ELSE NULL END) AS mfe_capture
        FROM live_trades
        WHERE {window.to_sql_clause('exit_time')}
          AND exit_time IS NOT NULL
          AND pnl_pips IS NOT NULL
        GROUP BY {group_by}
        HAVING n >= ?
        ORDER BY total_pips DESC
        LIMIT ?
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, (min_trades, top_n)).fetchall()
    return [
        ProfitZoneCluster(
            key={d: r[d] for d in dimensions},
            n=r["n"],
            total_pips=r["total_pips"] or 0.0,
            avg_pips=r["avg_pips"] or 0.0,
            win_rate=r["wr"] or 0.0,
            mfe_capture_ratio=r["mfe_capture"] or 0.0,
            rank=i + 1,
        )
        for i, r in enumerate(rows)
    ]


def mfe_capture(
    window: Window,
    groupby: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """pnl_pips / MFE — how much of the available move we kept."""
    if groupby:
        bad = set(groupby) - VALID_DIMENSIONS
        if bad:
            raise ValueError(f"Invalid groupby dimensions: {bad}. Valid: {VALID_DIMENSIONS}")
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    base = f"""
        WHERE {window.to_sql_clause('exit_time')}
          AND exit_time IS NOT NULL
          AND max_favorable_excursion_pips IS NOT NULL
          AND max_favorable_excursion_pips > 0
    """
    if groupby:
        dims = ", ".join(_dim_expr(d) for d in groupby)
        selects = ", ".join(f"{_dim_expr(d)} AS {d}" for d in groupby)
        rows = conn.execute(f"""
            SELECT {selects},
                   COUNT(*) AS n,
                   AVG(pnl_pips * 1.0 / max_favorable_excursion_pips) AS avg_capture,
                   AVG(max_favorable_excursion_pips) AS avg_mfe,
                   AVG(pnl_pips) AS avg_pnl
            FROM live_trades
            {base}
            GROUP BY {dims}
        """).fetchall()
        return {
            str(tuple(r[d] for d in groupby)): {
                "n": r["n"],
                "avg_capture_ratio": r["avg_capture"] or 0.0,
                "avg_mfe_pips": r["avg_mfe"] or 0.0,
                "avg_pnl_pips": r["avg_pnl"] or 0.0,
            }
            for r in rows
        }
    row = conn.execute(f"""
        SELECT COUNT(*) AS n,
               AVG(pnl_pips * 1.0 / max_favorable_excursion_pips) AS avg_capture,
               AVG(max_favorable_excursion_pips) AS avg_mfe,
               AVG(pnl_pips) AS avg_pnl
        FROM live_trades
        {base}
    """).fetchone()
    return {
        "overall": {
            "n": row["n"] or 0,
            "avg_capture_ratio": row["avg_capture"] or 0.0,
            "avg_mfe_pips": row["avg_mfe"] or 0.0,
            "avg_pnl_pips": row["avg_pnl"] or 0.0,
        }
    }


def peak_efficiency(
    window: Window,
    groupby: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """How close to the peak did we exit? pnl / max(pnl reached) per trade.

    Peak is MFE in pips (positive = favorable). For winners: efficiency = pnl / MFE.
    Thin wrapper over mfe_capture that exposes the ratio as `avg_efficiency` for
    call-site readability. `avg_capture_ratio` is preserved for back-compat.
    """
    raw = mfe_capture(window, groupby)
    out: Dict[str, Any] = {}
    for k, stats in raw.items():
        enriched = dict(stats)
        enriched["avg_efficiency"] = stats.get("avg_capture_ratio", 0.0)
        out[k] = enriched
    return out
