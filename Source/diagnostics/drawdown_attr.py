"""Drawdown attribution + losing-streak detection.

Pool-managed connections (db_pool.get_trading_forex) are thread-local and
cached; we do NOT close them. Lifecycle is owned by the pool. Matches the
pattern established in diagnostics.context / diagnostics.aggregation /
diagnostics.profit_zone.
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex

from diagnostics.context import Window, load_trades


@dataclass
class DrawdownEvent:
    start: str
    end: str
    depth_pips: float
    duration_minutes: float
    trades_involved: List[int]
    common_features: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "depth_pips": round(self.depth_pips, 1),
            "duration_minutes": round(self.duration_minutes, 1),
            "trades_involved": self.trades_involved,
            "common_features": self.common_features,
        }


_FRAC_SECONDS_RE = re.compile(r"\.(\d+)")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Robust ISO-8601 parser: handles Z suffix and nanosecond precision.

    Real exit_time values can carry 9-digit fractional seconds (e.g.
    "2026-03-25T00:14:35.675178586+00:00"), which datetime.fromisoformat
    rejects pre-3.11. We truncate fractional seconds to 6 digits (microseconds).
    """
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    s = _FRAC_SECONDS_RE.sub(lambda m: "." + m.group(1)[:6], s)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _find_common(trades: List[Any], attr: str) -> Any:
    """Return the modal value of `attr` across `trades` if it dominates (>=60%)."""
    vals = [getattr(t, attr) for t in trades if getattr(t, attr) is not None]
    if not vals:
        return None
    counter: Dict[Any, int] = defaultdict(int)
    for v in vals:
        counter[v] += 1
    top, n = max(counter.items(), key=lambda kv: kv[1])
    if n / len(vals) >= 0.6:
        return top
    return None


def worst_drawdowns(window: Window, top_n: int = 5) -> List[DrawdownEvent]:
    """Walk equity curve chronologically; identify peak->trough segments; rank by depth."""
    trades = load_trades(window)
    trades_sorted = sorted(trades, key=lambda t: t.exit_time or "")
    if not trades_sorted:
        return []

    equity = 0.0
    peak = 0.0
    peak_i = 0
    segments: List[Dict[str, Any]] = []
    current_seg: Optional[Dict[str, Any]] = None
    for i, t in enumerate(trades_sorted):
        equity += t.pnl_pips or 0.0
        if equity >= peak:
            if current_seg and current_seg["depth"] < 0:
                current_seg["end_i"] = i
                current_seg["end_time"] = t.exit_time
                segments.append(current_seg)
            peak = equity
            peak_i = i
            current_seg = None
        else:
            depth = equity - peak
            if current_seg is None:
                current_seg = {
                    "start_i": peak_i,
                    "start_time": trades_sorted[peak_i].exit_time,
                    "end_i": i,
                    "end_time": t.exit_time,
                    "depth": depth,
                    "peak": peak,
                }
            elif depth < current_seg["depth"]:
                current_seg["depth"] = depth
                current_seg["end_i"] = i
                current_seg["end_time"] = t.exit_time
    if current_seg and current_seg["depth"] < 0:
        segments.append(current_seg)

    segments.sort(key=lambda s: s["depth"])
    out: List[DrawdownEvent] = []
    for s in segments[:top_n]:
        trade_slice = trades_sorted[s["start_i"]:s["end_i"] + 1]
        start_dt = _parse_iso(s["start_time"])
        end_dt = _parse_iso(s["end_time"])
        duration = (end_dt - start_dt).total_seconds() / 60 if start_dt and end_dt else 0
        common = {
            "pair": _find_common(trade_slice, "pair"),
            "source": _find_common(trade_slice, "source"),
            "session": _find_common(trade_slice, "session"),
            "direction": _find_common(trade_slice, "direction"),
            "setup_code": _find_common(trade_slice, "setup_code"),
        }
        out.append(DrawdownEvent(
            start=s["start_time"] or "",
            end=s["end_time"] or "",
            depth_pips=s["depth"],
            duration_minutes=duration,
            trades_involved=[t.id for t in trade_slice],
            common_features={k: v for k, v in common.items() if v is not None},
        ))
    return out


def attribute(window: Window) -> Dict[str, Dict[str, Any]]:
    """Break down drawdown contribution by pair / source / setup / session.

    Pool-managed connection: we do NOT close it.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    out: Dict[str, Dict[str, Any]] = {}
    for axis, col in [("by_pair", "pair"), ("by_source", "source"),
                      ("by_setup", "setup_code"), ("by_session", "session")]:
        rows = conn.execute(f"""
            SELECT {col} AS k, COUNT(*) AS n,
                   SUM(CASE WHEN pnl_pips < 0 THEN pnl_pips ELSE 0 END) AS dd_contrib,
                   SUM(pnl_pips) AS net_pips
            FROM live_trades
            WHERE {window.to_sql_clause('exit_time')}
              AND exit_time IS NOT NULL
            GROUP BY {col}
            ORDER BY dd_contrib ASC
        """).fetchall()
        out[axis] = {
            (r["k"] or "unknown"): {
                "n": r["n"],
                "drawdown_contribution_pips": r["dd_contrib"] or 0.0,
                "net_pips": r["net_pips"] or 0.0,
            }
            for r in rows
        }
    return out


def losing_streaks(window: Window, min_length: int = 3) -> List[Dict[str, Any]]:
    """Detect consecutive-loss runs with length >= min_length, sorted descending."""
    trades = sorted(load_trades(window), key=lambda t: t.exit_time or "")
    streaks: List[Dict[str, Any]] = []
    run: List[Any] = []
    for t in trades:
        if t.outcome == "loss":
            run.append(t)
        else:
            if len(run) >= min_length:
                streaks.append(_streak_dict(run))
            run = []
    if len(run) >= min_length:
        streaks.append(_streak_dict(run))
    return sorted(streaks, key=lambda s: s["length"], reverse=True)


def _streak_dict(run: List[Any]) -> Dict[str, Any]:
    return {
        "length": len(run),
        "start": run[0].exit_time,
        "end": run[-1].exit_time,
        "total_pips": sum(t.pnl_pips or 0 for t in run),
        "trade_ids": [t.id for t in run],
        "common_pair": _find_common(run, "pair"),
        "common_source": _find_common(run, "source"),
        "common_session": _find_common(run, "session"),
    }
