#!/usr/bin/env python3
"""
Intelligence Store — Persistence layer for trading intelligence data.

Handles saving/querying intelligence_snapshots_v2 and intelligence_cache tables.
Part of Phase 4B Agent 1 (intelligence) data architecture.

Tables live in v2/intelligence.db (migrated from v2/trading_forex.db).
"""

import json
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

import os as _os

# Intelligence tables live in v2/intelligence.db (not v2/trading_forex.db)
DB_PATH = _os.path.realpath(_os.path.normpath(_os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
    "Database", "v2", "intelligence.db"
)))

logger = logging.getLogger(__name__)

# Cache TTLs (minutes)
CACHE_TTLS = {
    "wolfram_macro": 1440,   # 24 hours — rates update weekly/monthly
    "wolfram_stats": 0,      # No cache — always compute fresh
    "news": 360,             # 6 hours — news changes but not every minute during trading
    "weather": 720,          # 12 hours — weather changes slowly, perfect for pre-caching
    "ai_briefing": 420,      # 7 hours — covers overnight gap between 3AM London (~10PM ET) and 5AM ET refresh
}


class IntelligenceStore:
    """Manages intelligence_snapshots_v2 and intelligence_cache tables."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    @property
    def conn(self) -> sqlite3.Connection:
        """Return a pooled, thread-local connection to intelligence.db.

        Uses db_pool for lifecycle management — no manual close() needed.
        The pool handles WAL mode, mmap_size=0, busy_timeout, and cleanup.
        """
        from db_pool import get_intelligence
        c = get_intelligence()
        c.row_factory = sqlite3.Row
        return c

    def close(self):
        """No-op — pool manages connection lifecycle."""
        pass

    # ─── SNAPSHOTS ────────────────────────────────────────────────

    def save_snapshot(self, report: Dict[str, Any], decision_id: Optional[str] = None,
                      instrument: str = "") -> int:
        """
        Save a full intelligence report as a snapshot row.
        
        Args:
            report: Dict with keys matching intelligence_snapshots_v2 columns.
                    Extra keys go into 'full_report' as JSON.
            decision_id: Optional FK to trade_decisions table.
            instrument: e.g. "EUR_USD"
            
        Returns:
            Row id of the inserted snapshot.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Map report fields to column values
        row = {
            "timestamp": report.get("timestamp", now),
            "cycle_id": report.get("cycle_id"),
            "decision_id": decision_id or report.get("decision_id"),
            "trade_id": report.get("trade_id"),
            "instrument": instrument or report.get("instrument", ""),
            # Macro
            "base_currency": report.get("base_currency"),
            "quote_currency": report.get("quote_currency"),
            "base_currency_rate": report.get("base_currency_rate"),
            "quote_currency_rate": report.get("quote_currency_rate"),
            "rate_differential": report.get("rate_differential"),
            "base_inflation": report.get("base_inflation"),
            "quote_inflation": report.get("quote_inflation"),
            "base_unemployment": report.get("base_unemployment"),
            "quote_unemployment": report.get("quote_unemployment"),
            "oil_price": report.get("oil_price"),
            "gold_price": report.get("gold_price"),
            "pair_1yr_min": report.get("pair_1yr_min"),
            "pair_1yr_max": report.get("pair_1yr_max"),
            "pair_1yr_avg": report.get("pair_1yr_avg"),
            "pair_1yr_volatility": report.get("pair_1yr_volatility"),
            "pair_current_price": report.get("pair_current_price"),
            "pair_range_position": report.get("pair_range_position"),
            "base_gdp_growth": report.get("base_gdp_growth"),
            "quote_gdp_growth": report.get("quote_gdp_growth"),
            "base_trade_balance": report.get("base_trade_balance"),
            "quote_trade_balance": report.get("quote_trade_balance"),
            "macro_bias": report.get("macro_bias"),
            # News
            "base_sentiment": report.get("base_sentiment"),
            "quote_sentiment": report.get("quote_sentiment"),
            "net_sentiment": report.get("net_sentiment"),
            "high_impact_events": _json_or_none(report.get("high_impact_events")),
            "articles_analyzed": report.get("articles_analyzed"),
            "news_key_finding": report.get("news_key_finding"),
            "block_trading": 1 if report.get("block_trading") else 0,
            # Weather
            "weather_checked": 1 if report.get("weather_checked") else 0,
            "weather_severity": report.get("weather_severity", 1),
            "weather_locations": _json_or_none(report.get("weather_locations")),
            "weather_summary": report.get("weather_summary"),
            # Statistics
            "kelly_fraction": report.get("kelly_fraction"),
            "half_kelly": report.get("half_kelly"),
            "recommended_size_pct": report.get("recommended_size_pct"),
            "correlation_alerts": _json_or_none(report.get("correlation_alerts")),
            "correlation_breakdown_detected": 1 if report.get("correlation_breakdown_detected") else 0,
            "drift_detected": 1 if report.get("drift_detected") else 0,
            "drift_z_score": report.get("drift_z_score"),
            "drift_setup": report.get("drift_setup"),
            # Verdict
            "verdict": report.get("verdict"),
            "bias": report.get("bias"),
            "confidence": report.get("confidence"),
            "summary": report.get("summary"),
            # Full JSON backup
            "full_report": json.dumps(report, default=str),
            "wolfram_queries_used": report.get("wolfram_queries_used", 0),
            "news_queries_used": report.get("news_queries_used", 0),
            "created_at": now,
        }

        cols = list(row.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)

        cursor = self.conn.execute(
            f"INSERT INTO intelligence_snapshots_v2 ({col_names}) VALUES ({placeholders})",
            [row[c] for c in cols]
        )
        self.conn.commit()
        row_id = cursor.lastrowid
        logger.info(f"Saved intelligence snapshot #{row_id} for {instrument}")
        return row_id

    def link_trade_outcome(self, decision_id: str, trade_id: str,
                           outcome: str, pips: float, notes: str = "") -> int:
        """
        Link a trade outcome to its intelligence snapshot.
        Called by the reporter agent after a trade closes.
        
        Returns: number of rows updated.
        """
        cursor = self.conn.execute(
            """UPDATE intelligence_snapshots_v2 
               SET trade_id = ?, outcome = ?, pips_result = ?, outcome_notes = ?
               WHERE decision_id = ?""",
            [trade_id, outcome, pips, notes, decision_id]
        )
        self.conn.commit()
        updated = cursor.rowcount
        if updated:
            logger.info(f"Linked outcome '{outcome}' ({pips:+.1f} pips) to decision {decision_id}")
        else:
            logger.warning(f"No snapshot found for decision_id={decision_id}")
        return updated

    def get_snapshot(self, snapshot_id: int) -> Optional[Dict[str, Any]]:
        """Get a single snapshot by id."""
        row = self.conn.execute(
            "SELECT * FROM intelligence_snapshots_v2 WHERE id = ?", [snapshot_id]
        ).fetchone()
        return dict(row) if row else None

    def get_snapshot_by_decision(self, decision_id: str) -> Optional[Dict[str, Any]]:
        """Get snapshot linked to a specific decision."""
        row = self.conn.execute(
            "SELECT * FROM intelligence_snapshots_v2 WHERE decision_id = ?", [decision_id]
        ).fetchone()
        return dict(row) if row else None

    # ─── CACHE ────────────────────────────────────────────────────

    def get_cached(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """
        Get cached data if it hasn't expired.
        Returns parsed JSON data or None.
        """
        now = datetime.now(timezone.utc).isoformat()
        row = self.conn.execute(
            "SELECT data FROM intelligence_cache WHERE cache_key = ? AND expires_at > ?",
            [cache_key, now]
        ).fetchone()
        if row:
            try:
                return json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    def get_stale(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """
        Get cached data even if expired (stale-while-error fallback).
        Used when a live fetch fails (e.g. rate limit) so we return old data
        rather than empty. Returns parsed JSON data or None if never cached.
        """
        row = self.conn.execute(
            "SELECT data FROM intelligence_cache WHERE cache_key = ? ORDER BY fetched_at DESC LIMIT 1",
            [cache_key]
        ).fetchone()
        if row:
            try:
                return json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    def set_cached(self, cache_key: str, category: str, data: Any,
                   ttl_minutes: Optional[int] = None, instrument: str = "",
                   query_used: str = "") -> None:
        """
        Save data to cache with TTL-based expiry.
        Uses category default TTL if ttl_minutes not specified.
        """
        if ttl_minutes is None:
            ttl_minutes = CACHE_TTLS.get(category, 60)
        if ttl_minutes <= 0:
            return  # Don't cache (e.g. wolfram_stats)

        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=ttl_minutes)

        self.conn.execute(
            """INSERT OR REPLACE INTO intelligence_cache 
               (cache_key, category, instrument, data, fetched_at, expires_at, query_used)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [cache_key, category, instrument or None,
             json.dumps(data, default=str), now.isoformat(),
             expires.isoformat(), query_used]
        )
        self.conn.commit()

    def flush_cache(self, instruments: list = None) -> int:
        """Flush non-news cache entries, or only for specific instruments.
        News is NEVER flushed — NewsAPI dev plan is 100 req/24h.
        Called before scheduled refreshes so economic/briefing data is always fresh."""
        if instruments:
            placeholders = ",".join("?" for _ in instruments)
            cursor = self.conn.execute(
                f"DELETE FROM intelligence_cache WHERE instrument IN ({placeholders}) AND category != 'news'",
                instruments
            )
        else:
            cursor = self.conn.execute("DELETE FROM intelligence_cache WHERE category != 'news'")
        self.conn.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info(f"Flushed {deleted} cache entries (news preserved)" + (f" for {len(instruments)} instruments" if instruments else ""))
        return deleted

    def purge_expired_cache(self) -> int:
        """Delete expired cache entries. Returns count deleted."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            "DELETE FROM intelligence_cache WHERE expires_at < ?", [now]
        )
        self.conn.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info(f"Purged {deleted} expired cache entries")
        return deleted

    # ─── ANALYTICS / LEARNING ─────────────────────────────────────

    def query_winning_conditions(self, instrument: str,
                                 min_trades: int = 20) -> Optional[Dict[str, Any]]:
        """
        Aggregate macro conditions from winning trades for an instrument.
        Returns averaged macro data from 'win' outcomes, or None if insufficient data.
        """
        rows = self.conn.execute(
            """SELECT 
                COUNT(*) as n,
                AVG(rate_differential) as avg_rate_diff,
                AVG(base_currency_rate) as avg_base_rate,
                AVG(quote_currency_rate) as avg_quote_rate,
                AVG(base_inflation) as avg_base_inflation,
                AVG(quote_inflation) as avg_quote_inflation,
                AVG(oil_price) as avg_oil,
                AVG(gold_price) as avg_gold,
                AVG(pair_current_price) as avg_price,
                AVG(net_sentiment) as avg_sentiment,
                AVG(confidence) as avg_confidence,
                AVG(pips_result) as avg_pips
               FROM intelligence_snapshots_v2
               WHERE instrument = ? AND outcome = 'win'""",
            [instrument]
        ).fetchone()

        if not rows or rows["n"] < min_trades:
            return None

        return {
            "instrument": instrument,
            "winning_trades": rows["n"],
            "avg_rate_differential": rows["avg_rate_diff"],
            "avg_base_rate": rows["avg_base_rate"],
            "avg_quote_rate": rows["avg_quote_rate"],
            "avg_base_inflation": rows["avg_base_inflation"],
            "avg_quote_inflation": rows["avg_quote_inflation"],
            "avg_oil_price": rows["avg_oil"],
            "avg_gold_price": rows["avg_gold"],
            "avg_price": rows["avg_price"],
            "avg_sentiment": rows["avg_sentiment"],
            "avg_confidence": rows["avg_confidence"],
            "avg_pips": rows["avg_pips"],
        }

    def query_intelligence_by_outcome(self, instrument: str, outcome: str,
                                       limit: int = 50) -> List[Dict[str, Any]]:
        """Get snapshots filtered by instrument and outcome."""
        rows = self.conn.execute(
            """SELECT * FROM intelligence_snapshots_v2
               WHERE instrument = ? AND outcome = ?
               ORDER BY created_at DESC LIMIT ?""",
            [instrument, outcome, limit]
        ).fetchall()
        return [dict(r) for r in rows]

    def get_macro_pattern(self, instrument: str, setup_id: Optional[str] = None,
                          min_trades: int = 10) -> Optional[Dict[str, Any]]:
        """
        'When this instrument (+ optional setup) wins, what does the macro look like?'
        
        Compares winning vs losing macro conditions to find patterns.
        """
        def _avg_conditions(outcome: str) -> Optional[Dict]:
            sql = """SELECT COUNT(*) as n,
                        AVG(rate_differential) as rate_diff,
                        AVG(net_sentiment) as sentiment,
                        AVG(oil_price) as oil,
                        AVG(confidence) as confidence,
                        AVG(pips_result) as pips
                     FROM intelligence_snapshots_v2
                     WHERE instrument = ? AND outcome = ?"""
            params = [instrument, outcome]
            if setup_id:
                sql += " AND drift_setup = ?"
                params.append(setup_id)
            row = self.conn.execute(sql, params).fetchone()
            if not row or row["n"] < min_trades:
                return None
            return dict(row)

        wins = _avg_conditions("win")
        losses = _avg_conditions("loss")

        if not wins:
            return None

        result = {
            "instrument": instrument,
            "setup_id": setup_id,
            "winning_profile": wins,
            "losing_profile": losses,
        }

        # Add deltas if we have both
        if losses:
            result["edge_indicators"] = {
                "rate_diff_delta": (wins["rate_diff"] or 0) - (losses["rate_diff"] or 0),
                "sentiment_delta": (wins["sentiment"] or 0) - (losses["sentiment"] or 0),
                "oil_delta": (wins["oil"] or 0) - (losses["oil"] or 0),
                "confidence_delta": (wins["confidence"] or 0) - (losses["confidence"] or 0),
            }

        return result

    def get_recent_snapshots(self, instrument: str, hours: int = 24,
                             limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent snapshots for an instrument within time window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            """SELECT * FROM intelligence_snapshots_v2
               WHERE instrument = ? AND created_at > ?
               ORDER BY created_at DESC LIMIT ?""",
            [instrument, cutoff, limit]
        ).fetchall()
        return [dict(r) for r in rows]

    def get_cache_stats(self) -> Dict[str, Any]:
        """Return cache hit/miss stats by category."""
        now = datetime.now(timezone.utc).isoformat()
        rows = self.conn.execute(
            """SELECT category, 
                      COUNT(*) as total,
                      SUM(CASE WHEN expires_at > ? THEN 1 ELSE 0 END) as valid,
                      SUM(CASE WHEN expires_at <= ? THEN 1 ELSE 0 END) as expired
               FROM intelligence_cache GROUP BY category""",
            [now, now]
        ).fetchall()
        return {r["category"]: {"total": r["total"], "valid": r["valid"], 
                                "expired": r["expired"]} for r in rows}


def _json_or_none(val: Any) -> Optional[str]:
    """Convert lists/dicts to JSON string, pass strings/None through."""
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return json.dumps(val, default=str)
    return str(val)


# Module-level singleton
_store = None

def get_intelligence_store(db_path: str = DB_PATH) -> IntelligenceStore:
    """Get or create singleton IntelligenceStore."""
    global _store
    if _store is None:
        _store = IntelligenceStore(db_path)
    return _store
