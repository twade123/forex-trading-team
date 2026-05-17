"""Live stale-watch detection using OANDA current price + watch conditions.

For every active watch (status='watching'), fetch current OANDA mid price,
compare against the watch's creation-time entry_price, and produce a staleness
verdict (valid / drifted / invalidated / unknown) with a recommendation
(keep / delete / modify_entry).

Pool-managed connections (db_pool.get_trading_forex) are thread-local and
cached; we do NOT close them. Lifecycle is owned by the pool. Matches
pattern established in diagnostics.context (A1), diagnostics.live_health (A2),
and diagnostics.snipe_analysis (A9).

`scan_active_watches()` and `near_trigger_watches()` are read-only library
functions — safe to call from any diagnostics flow. `flag_stale_in_db()` is
the single CLI-gated write path and must never be invoked from library code.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex

_UTC = timezone.utc


@dataclass
class StaleVerdict:
    """Staleness verdict for one active watch."""
    watch_id: int
    pair: str
    direction: str
    origin_type: Optional[str]
    suggestion_type: Optional[str]
    age_hours: float
    distance_pips_from_entry: Optional[float]
    thesis_validity: str      # valid | drifted | invalidated | unknown
    recommendation: str        # keep | delete | modify_entry
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in [
            "watch_id", "pair", "direction", "origin_type", "suggestion_type",
            "age_hours", "distance_pips_from_entry", "thesis_validity",
            "recommendation", "reason",
        ]}


def _pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair.upper() else 0.0001


_ENTRY_PRICE_FIELDS = {
    "entry_price", "entry", "price", "price_above", "price_below",
    "suggested_entry", "limit_price",
}


def _extract_entry_price(cond: Any) -> Optional[float]:
    """Best-effort extraction of the watch's intended entry price.

    The `conditions` column is heterogeneous across watch origins:
     - dict shape: `{"entry_price": 1.2345, ...}` (legacy / chart-annotation)
     - list shape: `[{"field": "price_below", "value": 109.5}, ...]`
       (validator_structured — current dominant format)
     - list with price_zone strings: `[{"field": "price_zone",
       "value": "109.38-109.45"}, ...]` (use midpoint)

    Returns None if no numeric entry reference can be parsed.
    """
    if cond is None:
        return None
    if isinstance(cond, dict):
        for k in _ENTRY_PRICE_FIELDS:
            if k in cond:
                try:
                    v = float(cond[k])
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    continue
        return None
    if isinstance(cond, list):
        # Prefer explicit price fields; fall back to price_zone midpoint.
        zone_mid: Optional[float] = None
        for item in cond:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field", "")).lower()
            value = item.get("value")
            if field in _ENTRY_PRICE_FIELDS and field != "price_zone":
                try:
                    v = float(value)
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    continue
            if field == "price_zone" and isinstance(value, str) and "-" in value:
                try:
                    lo, hi = value.split("-", 1)
                    zone_mid = (float(lo) + float(hi)) / 2
                except (ValueError, TypeError):
                    continue
        return zone_mid
    return None


def _current_price(pair: str) -> Optional[float]:
    """Fetch current mid from OANDA if available; else return None.

    Any import error (OandaClient unavailable) or runtime error (bad credentials,
    network failure, malformed response) returns None — callers treat a missing
    price as thesis='unknown'.
    """
    try:
        from oanda_client import OandaClient
        client = OandaClient()
        pricing = client.get_pricing(pair)
        bid = float(pricing["bids"][0]["price"])
        ask = float(pricing["asks"][0]["price"])
        return (bid + ask) / 2
    except Exception:
        return None


def scan_active_watches() -> List[StaleVerdict]:
    """Evaluate every status='watching' row against current OANDA price.

    Returns one StaleVerdict per active watch. Read-only — safe to call from
    any diagnostics flow.

    Rules:
     - distance > 10 pips past entry  → invalidated → delete
     - distance > 3 pips past entry   → drifted     → modify_entry
     - distance <= 3 pips             → valid       → keep
     - age > 72h                      → delete (overrides "valid")
     - no current price or no entry   → unknown     → keep
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, instrument AS pair, direction, origin_type, suggestion_type,
               conditions, created_at, peak_progress
        FROM watch_suggestions
        WHERE status = 'watching'
    """).fetchall()

    out: List[StaleVerdict] = []
    now = datetime.now(_UTC)
    for r in rows:
        created_raw = r["created_at"] or ""
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=_UTC)
            age_hours = (now - created).total_seconds() / 3600
        except (ValueError, AttributeError):
            age_hours = 0.0

        entry_price: Optional[float] = None
        try:
            cond = json.loads(r["conditions"]) if r["conditions"] else None
            entry_price = _extract_entry_price(cond)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        current = _current_price(r["pair"])
        distance_pips: Optional[float] = None
        thesis = "unknown"
        reason = "no current price available"
        if current is not None and entry_price is not None:
            diff = current - entry_price
            if (r["direction"] or "").lower().startswith("s"):
                diff = -diff  # sell: price below entry is favorable
            distance_pips = diff / _pip_size(r["pair"])
            if distance_pips > 10:
                thesis = "invalidated"
                reason = f"price moved {distance_pips:+.1f}p past entry — thesis gone"
            elif distance_pips > 3:
                thesis = "drifted"
                reason = f"price {distance_pips:+.1f}p past entry zone"
            else:
                thesis = "valid"
                reason = f"price {distance_pips:+.1f}p from entry"

        if thesis == "invalidated" or age_hours > 72:
            rec = "delete"
            if age_hours > 72 and thesis != "invalidated":
                reason = f"watch age {age_hours:.0f}h > 72h threshold"
        elif thesis == "drifted":
            rec = "modify_entry"
        else:
            rec = "keep"

        out.append(StaleVerdict(
            watch_id=r["id"],
            pair=r["pair"],
            direction=r["direction"] or "",
            origin_type=r["origin_type"],
            suggestion_type=r["suggestion_type"],
            age_hours=age_hours,
            distance_pips_from_entry=distance_pips,
            thesis_validity=thesis,
            recommendation=rec,
            reason=reason,
        ))
    return out


def near_trigger_watches(progress_min: float = 0.80) -> List[Dict[str, Any]]:
    """Watches with peak_progress >= progress_min. Delegates to live_health."""
    from diagnostics.live_health import check_watches_near_trigger
    return check_watches_near_trigger(progress_min=progress_min)


def flag_stale_in_db(watch_ids: List[int]) -> int:
    """CLI-gated only — never call from diagnostics library flows.

    Writes stale_flagged_at = now on the given watch rows (only where it's
    currently NULL, so repeated calls are idempotent). Returns count updated.

    Library callers should use scan_active_watches() for read-only reporting.
    """
    if not watch_ids:
        return 0
    conn = get_trading_forex()
    placeholders = ",".join("?" * len(watch_ids))
    cur = conn.execute(
        f"UPDATE watch_suggestions SET stale_flagged_at = datetime('now') "
        f"WHERE id IN ({placeholders}) AND stale_flagged_at IS NULL",
        watch_ids,
    )
    conn.commit()
    return cur.rowcount
