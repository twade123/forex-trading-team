"""Shared primitives: Window, loaders, regime classifier, pair list."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex, get_flight_recorder

PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "USD_CAD",
    "AUD_USD", "NZD_USD", "EUR_JPY", "GBP_JPY", "EUR_GBP",
    "AUD_JPY", "EUR_CHF", "CAD_JPY",
]

_UTC = timezone.utc


@dataclass
class Window:
    start: datetime
    end: datetime
    label: str

    @classmethod
    def last_days(cls, days: int) -> "Window":
        end = datetime.now(_UTC)
        start = end - timedelta(days=days)
        return cls(start=start, end=end, label=f"{days}d")

    @classmethod
    def last_hours(cls, hours: int) -> "Window":
        end = datetime.now(_UTC)
        start = end - timedelta(hours=hours)
        return cls(start=start, end=end, label=f"{hours}h")

    @classmethod
    def since(cls, iso_date: str) -> "Window":
        start = datetime.fromisoformat(iso_date).replace(tzinfo=_UTC)
        end = datetime.now(_UTC)
        return cls(start=start, end=end, label=f"since_{iso_date}")

    def to_sql_clause(self, ts_col: str = "exit_time") -> str:
        return f"{ts_col} >= '{self.start.isoformat()}' AND {ts_col} <= '{self.end.isoformat()}'"


@dataclass
class TradeRow:
    id: int
    pair: str
    direction: str
    source: str
    entry_time: Optional[str]
    exit_time: Optional[str]
    entry_price: Optional[float]
    exit_price: Optional[float]
    sl_price: Optional[float]
    tp_price: Optional[float]
    pnl_pips: Optional[float]
    realized_pl: Optional[float]
    outcome: Optional[str]
    exit_trigger: Optional[str]
    exit_method: Optional[str]
    setup_code: Optional[str]
    entry_setup_type: Optional[str]
    confluence_score: Optional[float]
    scout_confidence: Optional[float]
    story_score: Optional[float]
    fan_state: Optional[str]
    session: Optional[str]
    adx: Optional[float]
    bb_width: Optional[float]
    mfe: Optional[float]
    mae: Optional[float]
    finding_id: Optional[str]  # DB stores as TEXT, not INT
    cycle_id: Optional[str]


@dataclass
class KronosSignalRow:
    id: int
    anchor_time: str
    pair: str
    direction: Optional[str]
    drift_pips: Optional[float]
    confidence: Optional[float]
    action_taken: str
    trade_id: Optional[str]
    matches_thesis: Optional[str]


@dataclass
class FlightLogRow:
    timestamp: str
    stage: str
    status: str
    trade_id: Optional[str]
    cycle_id: Optional[str]
    duration_ms: Optional[int]
    data: Dict[str, Any]
    note: Optional[str]


_TRADE_FILTER_KEYS = {
    "pair", "source", "direction", "outcome", "setup_code", "fan_state", "session",
}


def load_trades(window: Window, filters: Optional[Dict[str, Any]] = None) -> List[TradeRow]:
    """Load closed trades in window. Filters applied with AND logic.

    Pool-managed connection: we do NOT close it. db_pool.get_trading_forex()
    returns a thread-local cached connection whose lifecycle is owned by the pool.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    where = [window.to_sql_clause("exit_time"), "exit_time IS NOT NULL"]
    params: List[Any] = []
    if filters:
        for k, v in filters.items():
            if k not in _TRADE_FILTER_KEYS:
                raise ValueError(
                    f"Invalid filter key for load_trades: {k!r}. "
                    f"Allowed: {sorted(_TRADE_FILTER_KEYS)}"
                )
            where.append(f"{k} = ?")
            params.append(v)
    # NOTE: live_trades stores scout's confidence in the `confidence` column
    # (there is no `scout_confidence` column). We alias it here so the
    # TradeRow.scout_confidence field name stays semantically meaningful
    # for downstream scout_quality analysis (A11).
    sql = f"""
        SELECT id, pair, direction, source, entry_time, exit_time,
               entry_price, exit_price, sl_price, tp_price,
               pnl_pips, realized_pl, outcome, exit_trigger, exit_method,
               setup_code, entry_setup_type, confluence_score,
               confidence AS scout_confidence,
               story_score, fan_state, session, adx, bb_width,
               max_favorable_excursion_pips AS mfe,
               max_adverse_excursion_pips AS mae,
               finding_id, cycle_id
        FROM live_trades
        WHERE {' AND '.join(where)}
        ORDER BY exit_time DESC
    """
    rows = conn.execute(sql, params).fetchall()
    return [TradeRow(**dict(r)) for r in rows]


_SIGNAL_FILTER_KEYS = {"pair", "direction", "action_taken", "matches_thesis"}


def load_signals(window: Window, filters: Optional[Dict[str, Any]] = None) -> List[KronosSignalRow]:
    """Load kronos signals in window. Pool-managed connection (do not close)."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    where = [window.to_sql_clause("anchor_time")]
    params: List[Any] = []
    if filters:
        for k, v in filters.items():
            if k not in _SIGNAL_FILTER_KEYS:
                raise ValueError(
                    f"Invalid filter key for load_signals: {k!r}. "
                    f"Allowed: {sorted(_SIGNAL_FILTER_KEYS)}"
                )
            where.append(f"{k} = ?")
            params.append(v)
    sql = f"""
        SELECT id, anchor_time, pair, direction, drift_pips, confidence,
               action_taken, trade_id, matches_thesis
        FROM kronos_signals
        WHERE {' AND '.join(where)}
        ORDER BY anchor_time DESC
    """
    rows = conn.execute(sql, params).fetchall()
    return [KronosSignalRow(**dict(r)) for r in rows]


def load_flight_log(
    window: Optional[Window] = None,
    trade_id: Optional[str] = None,
    cycle_id: Optional[str] = None,
    stages: Optional[List[str]] = None,
) -> List[FlightLogRow]:
    """Load flight_log rows. Pool-managed connection (do not close)."""
    conn = get_flight_recorder()
    conn.row_factory = sqlite3.Row
    where: List[str] = []
    params: List[Any] = []
    if window:
        where.append(window.to_sql_clause("timestamp"))
    if trade_id:
        where.append("trade_id = ?")
        params.append(trade_id)
    if cycle_id:
        where.append("cycle_id = ?")
        params.append(cycle_id)
    if stages:
        placeholders = ",".join("?" * len(stages))
        where.append(f"stage IN ({placeholders})")
        params.extend(stages)
    sql = f"""
        SELECT timestamp, stage, status, trade_id, cycle_id,
               duration_ms, data, note
        FROM flight_log
        {f"WHERE {' AND '.join(where)}" if where else ''}
        ORDER BY timestamp
    """
    rows = conn.execute(sql, params).fetchall()
    out: List[FlightLogRow] = []
    for r in rows:
        d = dict(r)
        try:
            d["data"] = json.loads(d["data"]) if d["data"] else {}
        except (json.JSONDecodeError, TypeError):
            d["data"] = {}
        out.append(FlightLogRow(**d))
    return out


def classify_regime(adx: float, bb_width_pips: Optional[float] = None) -> str:
    """Map ADX + BB width to regime label (matches vault regime_playbook)."""
    if adx is None:
        return "unknown"
    if adx < 15:
        if bb_width_pips is not None and bb_width_pips < 8:
            return "compression"
        return "ranging"
    if adx < 25:
        return "weak_trend"
    if adx < 35:
        return "strong_trend"
    return "exhaustion"
