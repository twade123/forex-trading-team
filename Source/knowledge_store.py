"""
Per-instrument knowledge storage for the trading bot.

**V2 (Feb 16 2026):** Now reads from SQLite backtest_setup_performance table
as the primary data source for patterns and performance. Falls back to JSON
files for instruments not yet in the database. JSON files remain for write
operations (indicator tuning, notes, custom parameters) that don't have
a DB equivalent.

Usage:
    from Source.knowledge_store import KnowledgeStore

    ks = KnowledgeStore()
    patterns = ks.get_patterns("EUR_USD")  # reads from DB first, then JSON
    ks.save_pattern("EUR_USD", "bullish_engulfing", {"count": 5, "win_rate": 0.65})
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex

logger = logging.getLogger("trading_bot.knowledge_store")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_DEFAULT_BASE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "Data"
)


class _PooledNoClose:
    """Thin wrapper that makes .close() a no-op for pooled connections."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass  # pooled — do not close


class KnowledgeStore:
    """Per-instrument knowledge storage with SQLite + file-system hybrid.

    Reads:
      - Patterns & performance → SQLite backtest_setup_performance (8.5M trade evidence)
      - Indicator tuning, notes, custom params → JSON files

    Writes:
      - JSON files (as before) for custom data
      - Does NOT write to backtest tables (they're read-only reference data)
    """

    def __init__(self, base_dir: str = _DEFAULT_BASE_DIR, db_path: str = None):
        self._base_dir = os.path.abspath(base_dir)
        self._stores: Dict[str, Dict[str, Any]] = {}
        self._custom_db_path = db_path  # only set when caller provides non-default path

    def _get_conn(self) -> Optional[sqlite3.Connection]:
        """Return a pooled connection to trading_forex.db.

        The pool manages WAL mode, busy_timeout, and connection lifecycle.
        Do NOT close the returned connection.
        """
        if self._custom_db_path:
            # Non-default path — fall back to short-lived connection
            try:
                conn = sqlite3.connect(self._custom_db_path, timeout=30, isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("PRAGMA query_only=TRUE")
                return conn
            except Exception as e:
                logger.warning("DB connection failed: %s — using JSON only", e)
                return None
        try:
            conn = get_trading_forex()
            conn.row_factory = sqlite3.Row
            return _PooledNoClose(conn)  # .close() is safe no-op
        except Exception as e:
            logger.warning("Pool connection failed: %s — using JSON only", e)
            return None

    @property
    def db(self) -> Optional[sqlite3.Connection]:
        """Lazy SQLite connection via pool — kept for backward compatibility."""
        return self._get_conn()

    def close(self):
        pass  # pooled connections are managed by db_pool — no-op

    # ------------------------------------------------------------------
    # JSON file helpers (unchanged)
    # ------------------------------------------------------------------

    def _instrument_dir(self, instrument: str) -> str:
        path = os.path.join(self._base_dir, instrument)
        os.makedirs(path, exist_ok=True)
        return path

    def _knowledge_path(self, instrument: str) -> str:
        return os.path.join(self._instrument_dir(instrument), "knowledge.json")

    @staticmethod
    def _default_knowledge(instrument: str) -> Dict[str, Any]:
        now = _iso_now()
        return {
            "instrument": instrument, "version": 2,
            "patterns": {}, "parameters": {}, "performance": {},
            "indicator_tuning": {}, "statistics": {}, "notes": [],
            "created_at": now, "updated_at": now,
        }

    def _load_json(self, instrument: str) -> Dict[str, Any]:
        if instrument in self._stores:
            return self._stores[instrument]
        path = self._knowledge_path(instrument)
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
        else:
            data = self._default_knowledge(instrument)
        self._stores[instrument] = data
        return data

    def _save(self, instrument: str) -> None:
        data = self._stores.get(instrument)
        if data is None:
            return
        data["updated_at"] = _iso_now()
        path = self._knowledge_path(instrument)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    # ------------------------------------------------------------------
    # Public API — read (DB-first, JSON fallback)
    # ------------------------------------------------------------------

    def get_knowledge(self, instrument: str) -> Dict[str, Any]:
        """Return the full knowledge dict — DB patterns + JSON custom data."""
        json_data = self._load_json(instrument)

        # Overlay DB-backed data
        db_patterns = self._get_db_patterns(instrument)
        if db_patterns:
            # Merge: DB patterns as base, JSON patterns override
            merged = dict(db_patterns)
            merged.update(json_data.get("patterns", {}))
            json_data["patterns"] = merged

        db_perf = self._get_db_performance(instrument)
        if db_perf:
            merged_perf = dict(db_perf)
            merged_perf.update(json_data.get("performance", {}))
            json_data["performance"] = merged_perf

        return json_data

    def get_patterns(self, instrument: str) -> Dict[str, Any]:
        """Return patterns — DB setup performance as primary, JSON as supplement."""
        db_patterns = self._get_db_patterns(instrument)
        json_patterns = self._load_json(instrument).get("patterns", {})

        if db_patterns:
            merged = dict(db_patterns)
            merged.update(json_patterns)
            return merged
        return json_patterns

    def get_parameter(self, instrument: str, param_name: str, default: Any = None) -> Any:
        """Return a stored parameter or best DB params."""
        # Check JSON first
        params = self._load_json(instrument).get("parameters", {})
        entry = params.get(param_name)
        if entry is not None:
            return entry.get("value", default)

        # Check DB for best params (e.g., "best_rr", "best_sl")
        if param_name.startswith("best_"):
            _conn = self._get_conn()
            if _conn:
                try:
                    row = _conn.execute("""
                        SELECT setup, profit_factor, win_rate
                        FROM backtest_setup_performance
                        WHERE pair=? AND profit_factor > 1.0 AND trade_count >= 10
                        ORDER BY profit_factor DESC LIMIT 1
                    """, (instrument,)).fetchone()
                    if row:
                        setup = row["setup"]
                        if "_rr" in setup:
                            parts = setup.split("_")
                            for p in parts:
                                if p.startswith("rr") and param_name == "best_rr":
                                    return float(p[2:])
                                if p.startswith("sl") and param_name == "best_sl":
                                    return float(p[2:])
                except Exception:
                    pass
                finally:
                    _conn.close()
        return default

    def get_all_instruments(self) -> List[str]:
        """Return all instruments with knowledge (DB + JSON)."""
        json_instruments = set()
        if os.path.isdir(self._base_dir):
            json_instruments = {
                name for name in os.listdir(self._base_dir)
                if os.path.isdir(os.path.join(self._base_dir, name))
                and os.path.exists(os.path.join(self._base_dir, name, "knowledge.json"))
            }

        db_instruments = set()
        _conn = self._get_conn()
        if _conn:
            try:
                rows = _conn.execute(
                    "SELECT DISTINCT pair FROM backtest_setup_performance"
                ).fetchall()
                db_instruments = {r["pair"] for r in rows}
            except Exception:
                pass
            finally:
                _conn.close()

        return sorted(json_instruments | db_instruments)

    # ------------------------------------------------------------------
    # DB query helpers
    # ------------------------------------------------------------------

    def _get_db_patterns(self, instrument: str) -> Optional[Dict[str, Any]]:
        """Get setup patterns from backtest_setup_performance."""
        _conn = self._get_conn()
        if not _conn:
            return None
        try:
            rows = _conn.execute("""
                SELECT setup, trade_count, win_rate, profit_factor, total_pips,
                       avg_pips, h4_agrees_win_rate
                FROM backtest_setup_performance
                WHERE pair=? AND trade_count >= 5
                ORDER BY profit_factor DESC
            """, (instrument,)).fetchall()

            if not rows:
                return None

            patterns = {}
            for r in rows:
                key = r["setup"]
                patterns[key] = {
                    "count": r["trade_count"],
                    "win_rate": r["win_rate"] / 100 if r["win_rate"] > 1 else r["win_rate"],
                    "profit_factor": r["profit_factor"],
                    "total_pips": r["total_pips"],
                    "avg_pips": r["avg_pips"],
                    "h4_agrees_win_rate": r["h4_agrees_win_rate"],
                    "source": "backtest_db",
                }
            return patterns
        except Exception as e:
            logger.debug("DB pattern query failed: %s", e)
            return None
        finally:
            _conn.close()

    def _get_db_performance(self, instrument: str) -> Optional[Dict[str, Any]]:
        """Get aggregate performance from DB."""
        _conn = self._get_conn()
        if not _conn:
            return None
        try:
            row = _conn.execute("""
                SELECT COUNT(*) as setups,
                       SUM(trade_count) as total_trades,
                       ROUND(AVG(win_rate), 1) as avg_win_rate,
                       MAX(profit_factor) as best_pf,
                       ROUND(SUM(total_pips), 1) as total_pips
                FROM backtest_setup_performance
                WHERE pair=? AND trade_count >= 10
            """, (instrument,)).fetchone()

            if row and row["setups"]:
                return {
                    "db_setups_tested": {"value": row["setups"], "source": "backtest_db"},
                    "db_total_trades": {"value": row["total_trades"], "source": "backtest_db"},
                    "db_avg_win_rate": {"value": row["avg_win_rate"], "source": "backtest_db"},
                    "db_best_profit_factor": {"value": row["best_pf"], "source": "backtest_db"},
                    "db_total_pips": {"value": row["total_pips"], "source": "backtest_db"},
                }
            return None
        except Exception as e:
            logger.debug("DB performance query failed: %s", e)
            return None
        finally:
            _conn.close()

    # ------------------------------------------------------------------
    # Public API — write (JSON only, DB is read-only)
    # ------------------------------------------------------------------

    def save_pattern(self, instrument: str, pattern_name: str, data: Dict[str, Any]) -> None:
        knowledge = self._load_json(instrument)
        existing = knowledge["patterns"].get(pattern_name, {})
        existing.update(data)
        knowledge["patterns"][pattern_name] = existing
        self._save(instrument)

    def save_parameter(self, instrument: str, param_name: str, value: Any, source: str = "computed") -> None:
        knowledge = self._load_json(instrument)
        knowledge["parameters"][param_name] = {"value": value, "source": source, "updated_at": _iso_now()}
        self._save(instrument)

    def save_performance(self, instrument: str, metric_name: str, value: Any, period: str = "daily") -> None:
        knowledge = self._load_json(instrument)
        knowledge["performance"][metric_name] = {"value": value, "period": period, "updated_at": _iso_now()}
        self._save(instrument)

    def save_indicator_tuning(self, instrument: str, indicator_name: str, params: Dict[str, Any], notes: str = "") -> None:
        knowledge = self._load_json(instrument)
        knowledge["indicator_tuning"][indicator_name] = {"params": params, "notes": notes, "updated_at": _iso_now()}
        self._save(instrument)

    def save_statistic(self, instrument: str, stat_name: str, value: Any, sample_size: int) -> None:
        knowledge = self._load_json(instrument)
        knowledge["statistics"][stat_name] = {"value": value, "sample_size": sample_size, "updated_at": _iso_now()}
        self._save(instrument)

    def add_note(self, instrument: str, note_text: str) -> None:
        knowledge = self._load_json(instrument)
        knowledge["notes"].append({"text": note_text, "timestamp": _iso_now()})
        self._save(instrument)

    def clear_instrument(self, instrument: str) -> None:
        self._stores[instrument] = self._default_knowledge(instrument)
        self._save(instrument)
