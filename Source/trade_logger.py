"""
Trade Logger -- Consolidated audit trail for trading operations.

**V2 (Feb 16 2026):** Now a thin wrapper around TradingDB for trade/decision logging.
TradingDB is the SINGLE SOURCE OF TRUTH for all trading data in v2/trading_forex.db.
The old trade_log.db (Data/trade_log.db) is preserved for signal_log, validation_log,
and mcp_query_log which remain useful for cycle-level audit trail.

Usage::

    from Source.trade_logger import TradeLogger

    logger = TradeLogger()
    # NEW: logs to trevor_database.db live_trades table
    logger.log_trade_unified(cycle_id="c1", instrument="EUR_USD", ...)
    # OLD: still works for backward compat, logs to trade_log.db
    logger.log_signal(cycle_id="c1", instrument="EUR_USD", ...)
    logger.close()
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex

logger = logging.getLogger("trading_bot.trade_logger")

_DEFAULT_DB_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "Data"
)
_DEFAULT_DB_PATH = os.path.join(_DEFAULT_DB_DIR, "trade_log.db")


class TradeLogger:
    """Persistent structured logging for trading operations.

    Trade logging is delegated to TradingDB (v2/trading_forex.db) for the
    canonical 76-column live_trades table and trade_decisions audit trail.
    Signal, validation, and MCP query logs remain in the local trade_log.db.
    """

    def __init__(self, db_path: Optional[str] = None, user_id: Optional[int] = None):
        self._db_path = db_path or _DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        # User isolation: every log entry stamped with the owning user
        self._user_id = user_id or self._resolve_user_id()
        self._create_tables()

        # Lazy-load TradingDB for unified trade logging
        self._trading_db = None

        logger.info("TradeLogger initialised: %s (user_id=%s)", self._db_path, self._user_id)

    @staticmethod
    def _resolve_user_id() -> Optional[int]:
        """Resolve user_id from environment (set by serve_ui.py at login)."""
        uid = os.environ.get('TRADING_USER_ID')
        return int(uid) if uid else None

    def _get_conn(self) -> sqlite3.Connection:
        """Get persistent pooled connection to trade_log.db.

        Returns a connection from db_pool with isolation_level=None (autocommit).
        Do NOT close this connection — it's managed by the pool.
        """
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        # Pool connection already has WAL + foreign_keys enabled
        return conn

    @contextmanager
    def _db(self):
        """Context manager for pooled connections.

        Connection is persistent and managed by db_pool. Since isolation_level=None
        (autocommit), we wrap multi-statement writes in BEGIN/COMMIT.
        Do NOT close the connection.
        """
        conn = self._get_conn()
        try:
            yield conn
            # Connection is autocommit; no manual commit needed for single statements
        except Exception:
            # Rollback is automatic on error with isolation_level=None
            raise
        # Do NOT close — connection is managed by pool

    @property
    def trading_db(self):
        """Lazy-load TradingDB for canonical trade/decision logging."""
        if self._trading_db is None:
            try:
                import sys
                from pathlib import Path
                bot_dir = Path(__file__).parent.parent
                if str(bot_dir) not in sys.path:
                    sys.path.insert(0, str(bot_dir))
                from Source.backtester.trading_db import TradingDB
                self._trading_db = TradingDB()
                logger.info("TradingDB connected for unified logging")
            except Exception as e:
                logger.warning("TradingDB not available, falling back to local: %s", e)
        return self._trading_db

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        """Close all connections."""
        if self._trading_db:
            self._trading_db.close()
            self._trading_db = None

    # ------------------------------------------------------------------
    # Schema (local tables only — signal, validation, mcp)
    # ------------------------------------------------------------------

    def _create_tables(self):
        """Ensure local log tables exist with user_id columns for multi-user isolation.

        Uses pooled connection with isolation_level=None. Wraps multi-statement
        operations in explicit BEGIN/COMMIT.
        """
        conn = self._get_conn()
        try:
            # executescript is not supported with isolation_level=None
            # so we wrap CREATE TABLE statements in explicit transaction
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_log (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id            TEXT NOT NULL,
                    instrument          TEXT NOT NULL,
                    timeframe           TEXT NOT NULL,
                    timestamp           TEXT NOT NULL,
                    confluence_score    REAL,
                    direction           TEXT,
                    action              TEXT,
                    indicator_values    TEXT,
                    patterns_detected   TEXT,
                    news_sentiment      REAL,
                    intelligence_summary TEXT,
                    decision_reasoning  TEXT,
                    gate_results        TEXT,
                    ema_snapshot        TEXT,
                    user_id             INTEGER
                )
                """)
                conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_log (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id            TEXT NOT NULL,
                    trade_id            TEXT NOT NULL,
                    instrument          TEXT NOT NULL,
                    direction           TEXT NOT NULL,
                    entry_price         REAL,
                    exit_price          REAL,
                    units               INTEGER,
                    stop_loss           TEXT,
                    take_profit         TEXT,
                    realized_pl         REAL,
                    risk_profile        TEXT,
                    confluence_score    REAL,
                    patterns_triggered  TEXT,
                    mcp_data_used       TEXT,
                    client_extensions   TEXT,
                    entry_time          TEXT,
                    exit_time           TEXT,
                    exit_reason         TEXT,
                    user_id             INTEGER
                )
                """)
                conn.execute("""
                CREATE TABLE IF NOT EXISTS validation_log (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id            TEXT NOT NULL,
                    instrument          TEXT NOT NULL,
                    timestamp           TEXT NOT NULL,
                    gate1_passed        INTEGER,
                    gate1_confidence    REAL,
                    gate1_issues        TEXT,
                    gate2_passed        INTEGER,
                    gate2_issues        TEXT,
                    contradictions      TEXT,
                    needs_llm_escalation INTEGER,
                    recommendation      TEXT,
                    overall_passed      INTEGER,
                    user_id             INTEGER
                )
                """)
                conn.execute("""
                CREATE TABLE IF NOT EXISTS mcp_query_log (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id            TEXT NOT NULL,
                    instrument          TEXT NOT NULL,
                    timestamp           TEXT NOT NULL,
                    source              TEXT NOT NULL,
                    query_type          TEXT,
                    response_summary    TEXT,
                    impact_on_decision  TEXT,
                    error               TEXT
                )
                """)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            # ── Migrate existing tables: add user_id if missing ──────────────
            for table in ('signal_log', 'validation_log', 'trade_log'):
                try:
                    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                    if 'user_id' not in cols:
                        conn.execute("BEGIN IMMEDIATE")
                        try:
                            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
                            conn.commit()
                            logger.info("Migrated %s: added user_id column", table)
                        except Exception:
                            conn.rollback()
                            raise
                except Exception as e:
                    logger.debug("user_id migration for %s: %s", table, e)

            # Index for fast per-user queries
            for table in ('signal_log', 'validation_log', 'trade_log'):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        conn.execute(f"""
                            CREATE INDEX IF NOT EXISTS idx_{table}_user_id
                            ON {table}(user_id)
                        """)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                except Exception:
                    pass
        finally:
            # Do NOT close — connection is managed by pool
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_json(data: Any) -> Optional[str]:
        if data is None:
            return None
        return json.dumps(data, default=str)

    def _rows_to_dicts(self, rows: list) -> List[Dict[str, Any]]:
        return [dict(row) for row in rows]

    # ==================================================================
    # UNIFIED LOGGING — delegates to TradingDB (trevor_database.db)
    # ==================================================================

    def log_trade_unified(
        self,
        trade_data: Dict[str, Any],
        cycle_id: str = None,
    ) -> str:
        """Log a trade to the canonical live_trades table in v2/trading_forex.db.

        Also writes a legacy entry to local trade_log for backward compat.

        Args:
            trade_data: Dict matching live_trades schema (76 columns).
            cycle_id: Optional cycle ID for local trade_log entry.

        Returns:
            trade_id from TradingDB.
        """
        db = self.trading_db
        if db is None:
            logger.warning("TradingDB unavailable — logging to local trade_log only")
            return self._log_trade_local_fallback(trade_data, cycle_id)

        trade_id = db.log_live_trade(trade_data)

        # Capture intelligence snapshot with the trade for pattern analysis
        try:
            self._capture_intelligence_snapshot(trade_id, trade_data.get("pair", ""))
        except Exception as e:
            logger.debug(f"Intelligence snapshot capture failed (non-critical): {e}")

        # Also write to local trade_log for backward compat
        if cycle_id:
            try:
                self.log_trade(
                    cycle_id=cycle_id,
                    trade_id=trade_id,
                    instrument=trade_data.get("pair", ""),
                    direction=trade_data.get("direction", ""),
                    entry_price=trade_data.get("entry_price", 0),
                    units=trade_data.get("units", 0),
                    stop_loss=str(trade_data.get("sl_price", "")),
                    take_profit=str(trade_data.get("tp_price", "")),
                    risk_profile=trade_data.get("source", "paper"),
                    confluence_score=trade_data.get("confidence", 0),
                    patterns_triggered=trade_data.get("entry_candle_pattern"),
                    mcp_data_used=None,
                    client_extensions=None,
                )
            except Exception as e:
                logger.debug("Local trade_log write failed (non-critical): %s", e)

        return trade_id

    def _capture_intelligence_snapshot(self, trade_id: str, instrument: str) -> None:
        """Capture current intelligence briefing + raw macro data with a trade.
        
        Stored in trade_intelligence table for post-trade pattern analysis:
        - Which macro conditions led to wins vs losses?
        - Do commodity moves correlate with trade outcomes?
        - Does news sentiment predict success?
        """
        import json as _json
        try:
            from intelligence_store import IntelligenceStore
            store = IntelligenceStore()
            
            # Grab the AI briefing
            briefing_row = store.get_cached(f"briefing:ai:{instrument}")
            briefing = ""
            if briefing_row:
                try:
                    d = _json.loads(briefing_row) if isinstance(briefing_row, str) else briefing_row
                    if isinstance(d, dict):
                        briefing = d.get("briefing", str(d))
                    else:
                        briefing = str(d)
                except Exception:
                    briefing = str(briefing_row)
            
            # Grab all cached data for this instrument
            raw_cache = {}
            cursor = store.conn.execute(
                "SELECT cache_key, category, data FROM intelligence_cache WHERE instrument = ?",
                [instrument]
            )
            for row in cursor:
                raw_cache[row[0]] = {"category": row[1], "data": row[2][:500]}  # Truncate per entry
            
            store.close()
            
            # Store in trade_intelligence table
            from datetime import datetime, timezone
            db = self.trading_db
            if db and db.conn:
                db.conn.execute("""
                    CREATE TABLE IF NOT EXISTS trade_intelligence (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id TEXT NOT NULL,
                        instrument TEXT NOT NULL,
                        briefing TEXT,
                        raw_data TEXT,
                        captured_at TEXT NOT NULL,
                        outcome TEXT DEFAULT NULL,
                        pips_result REAL DEFAULT NULL
                    )
                """)
                db.conn.execute(
                    """INSERT INTO trade_intelligence 
                       (trade_id, instrument, briefing, raw_data, captured_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    [trade_id, instrument, briefing,
                     _json.dumps(raw_cache, default=str),
                     datetime.now(timezone.utc).isoformat()]
                )
                db.conn.commit()
                logger.info(f"Captured intelligence snapshot for trade {trade_id} ({instrument})")
        except Exception as e:
            logger.warning(f"Intelligence snapshot failed: {e}")

    def log_decision_unified(self, **kwargs) -> str:
        """Log a trade decision to the canonical trade_decisions table.

        Accepts either TradingDB.log_decision() param names or common aliases.
        Maps: instrument→pair, action→final_action, score→confidence, etc.

        Returns:
            decision_id
        """
        db = self.trading_db
        if db is None:
            logger.warning("TradingDB unavailable — decision not logged")
            return "no_db"

        # Map common aliases to TradingDB.log_decision() param names
        mapped = dict(kwargs)
        if "instrument" in mapped and "pair" not in mapped:
            mapped["pair"] = mapped.pop("instrument")
        if "action" in mapped and "final_action" not in mapped:
            mapped["final_action"] = mapped.pop("action")
        if "confluence_score" in mapped and "confidence" not in mapped:
            mapped["confidence"] = mapped.pop("confluence_score")
        if "setup_id" in mapped and "setup" not in mapped:
            mapped["setup"] = mapped.pop("setup_id")
        if "validator_verdict" in mapped and "verdict" not in mapped:
            mapped["verdict"] = mapped.pop("validator_verdict")
        if "validator_evidence" in mapped and "db_evidence" not in mapped:
            mapped["db_evidence"] = mapped.pop("validator_evidence")
        if "intelligence_summary" in mapped:
            # Split into news/wolfram if possible, otherwise drop
            intel = mapped.pop("intelligence_summary")
            if isinstance(intel, dict):
                if "news_sentiment" in intel and "news_data" not in mapped:
                    mapped["news_data"] = intel
        # Ensure required params have defaults
        mapped.setdefault("pair", "")
        mapped.setdefault("timeframe", "H1")
        mapped.setdefault("setup", "unknown")
        mapped.setdefault("direction", "neutral")
        mapped.setdefault("regime", "unknown")

        return db.log_decision(**mapped)

    def update_trade_outcome_unified(
        self,
        trade_id: str = None,
        decision_id: str = None,
        outcome: str = None,
        pips: float = None,
    ):
        """Update trade outcome in both TradingDB and local trade_log."""
        db = self.trading_db
        if db:
            db.update_trade_outcome(trade_id, decision_id, outcome, pips)

        # Also update local trade_log via pooled connection
        if trade_id:
            conn = self._get_conn()
            try:
                conn.execute(
                    """UPDATE trade_log SET exit_reason=?, realized_pl=?
                       WHERE trade_id=?""",
                    (outcome, pips, trade_id),
                )
                # Connection is autocommit; no explicit commit needed
            except Exception:
                pass
            # Do NOT close — connection is managed by pool

    def _log_trade_local_fallback(self, trade_data: Dict, cycle_id: str = None) -> str:
        """Fallback: log trade to local trade_log when TradingDB is unavailable.

        Uses pooled connection with autocommit.
        """
        import uuid
        trade_id = trade_data.get("trade_id", str(uuid.uuid4())[:8])
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute(
            """INSERT INTO trade_log
               (cycle_id, trade_id, instrument, direction,
                entry_price, units, stop_loss, take_profit,
                risk_profile, confluence_score, entry_time, user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cycle_id or "unknown",
                trade_id,
                trade_data.get("pair", ""),
                trade_data.get("direction", ""),
                trade_data.get("entry_price", 0),
                trade_data.get("units", 0),
                str(trade_data.get("sl_price", "")),
                str(trade_data.get("tp_price", "")),
                trade_data.get("source", "paper"),
                trade_data.get("confidence", 0),
                now,
                self._user_id,
            ),
            )
            # Connection is autocommit; no explicit commit needed
        finally:
            # Do NOT close — connection is managed by pool
            pass
        return trade_id

    # ==================================================================
    # ORIGINAL LOGGING METHODS (preserved for backward compat)
    # ==================================================================

    def log_signal(
        self,
        cycle_id: str,
        instrument: str,
        timeframe: str,
        analysis_results: dict,
        decision: dict,
        intelligence_data: dict,
        ema_snapshot: dict = None,
    ) -> int:
        """Log a trading signal after decision step (LOGS-01).
        
        ema_snapshot: EMA market narrative state at signal time.
        Saved for every cycle (trade AND hold) so we can learn from
        both taken and missed opportunities.
        """
        now = datetime.now(timezone.utc).isoformat()
        confluence = analysis_results.get("confluence", {})
        confluence_score = confluence.get("total_score", confluence.get("score", 0.0))
        direction = confluence.get("direction", decision.get("direction", "neutral"))
        action = decision.get("action", "hold")

        indicator_values = {
            "core": analysis_results.get("core_indicators", {}),
            "advanced": analysis_results.get("advanced_indicators", {}),
        }
        patterns_detected = {
            "candlestick": analysis_results.get("candlestick_patterns", {}),
            "chart": analysis_results.get("chart_patterns", {}),
        }
        news = intelligence_data.get("news", {})
        news_sentiment = news.get("sentiment") if isinstance(news, dict) else None
        decision_reasoning = {
            "reasons": decision.get("reasons", []),
            "blocking_reasons": decision.get("blocking_reasons", []),
            "allowed": decision.get("allowed", False),
        }
        gate_results = decision.get("gate_results", {})

        with self._db() as conn:
            # Ensure ema_snapshot column exists
            if ema_snapshot:
                try:
                    conn.execute("SELECT ema_snapshot FROM signal_log LIMIT 0")
                except Exception:
                    try:
                        conn.execute("ALTER TABLE signal_log ADD COLUMN ema_snapshot TEXT")
                        conn.commit()
                    except Exception:
                        pass

            cursor = conn.execute(
                """INSERT INTO signal_log
                   (cycle_id, instrument, timeframe, timestamp,
                    confluence_score, direction, action,
                    indicator_values, patterns_detected, news_sentiment,
                    intelligence_summary, decision_reasoning, gate_results,
                    ema_snapshot, user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cycle_id, instrument, timeframe, now, confluence_score, direction,
                 action, self._to_json(indicator_values), self._to_json(patterns_detected),
                 news_sentiment, self._to_json(intelligence_data),
                 self._to_json(decision_reasoning), self._to_json(gate_results),
                 self._to_json(ema_snapshot), self._user_id),
            )
            return cursor.lastrowid

    def log_trade(
        self, cycle_id, trade_id, instrument, direction, entry_price, units,
        stop_loss, take_profit, risk_profile, confluence_score,
        patterns_triggered, mcp_data_used, client_extensions,
        ema_snapshot=None, market_picture_snapshot=None,
    ) -> int:
        """Log a trade placement (LOGS-02) — local only.
        
        ema_snapshot: dict with fan_direction, fan_state, velocity, trend_health, etc.
        market_picture_snapshot: dict with RSI, Stoch, BB, confluence_narrative at entry time.
        """
        now = datetime.now(timezone.utc).isoformat()
        
        with self._db() as conn:
            # Ensure ema_snapshot and market_picture columns exist
            try:
                conn.execute("SELECT ema_snapshot FROM trade_log LIMIT 0")
            except Exception:
                try:
                    conn.execute("ALTER TABLE trade_log ADD COLUMN ema_snapshot TEXT")
                    conn.execute("ALTER TABLE trade_log ADD COLUMN market_picture_snapshot TEXT")
                    conn.commit()
                except Exception:
                    pass  # Columns may already exist
            
            cursor = conn.execute(
                """INSERT INTO trade_log
                   (cycle_id, trade_id, instrument, direction,
                    entry_price, units, stop_loss, take_profit,
                    risk_profile, confluence_score,
                    patterns_triggered, mcp_data_used, client_extensions,
                    entry_time, ema_snapshot, market_picture_snapshot, user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cycle_id, trade_id, instrument, direction, entry_price, units,
                 stop_loss, take_profit, risk_profile, confluence_score,
                 self._to_json(patterns_triggered), self._to_json(mcp_data_used),
                 self._to_json(client_extensions), now,
                 self._to_json(ema_snapshot), self._to_json(market_picture_snapshot),
                 self._user_id),
            )
            return cursor.lastrowid

    def update_trade_exit(self, trade_id, exit_price, realized_pl, exit_time, exit_reason,
                          exit_ema_snapshot=None):
        """Update a trade record with exit data — also updates TradingDB.
        
        exit_ema_snapshot: EMA state at exit time for learning loop comparison.
        """
        with self._db() as conn:
            # Ensure exit_ema_snapshot column exists
            if exit_ema_snapshot:
                try:
                    conn.execute("SELECT exit_ema_snapshot FROM trade_log LIMIT 0")
                except Exception:
                    try:
                        conn.execute("ALTER TABLE trade_log ADD COLUMN exit_ema_snapshot TEXT")
                        conn.commit()
                    except Exception:
                        pass
            
            if exit_ema_snapshot:
                conn.execute(
                    """UPDATE trade_log SET exit_price=?, realized_pl=?, exit_time=?, exit_reason=?,
                       exit_ema_snapshot=?
                       WHERE trade_id=?""",
                    (exit_price, realized_pl, exit_time, exit_reason,
                     self._to_json(exit_ema_snapshot), trade_id),
                )
            else:
                conn.execute(
                    """UPDATE trade_log SET exit_price=?, realized_pl=?, exit_time=?, exit_reason=?
                       WHERE trade_id=?""",
                    (exit_price, realized_pl, exit_time, exit_reason, trade_id),
                )

        # Also update canonical DB
        db = self.trading_db
        if db:
            outcome = "win" if realized_pl and realized_pl > 0 else "loss"
            try:
                db.update_trade_outcome(trade_id=trade_id, outcome=outcome, pips=realized_pl)
            except Exception as e:
                # 2026-04-24: upgraded from debug — canonical outcome DB write failure
                # is critical (learning systems drift, stats diverge). Needs visibility.
                logger.warning("TradingDB outcome update FAILED for trade %s (pips=%s): %s: %s",
                               trade_id, realized_pl, type(e).__name__, e)
            
            # Update intelligence snapshot with outcome
            try:
                db.conn.execute(
                    "UPDATE trade_intelligence SET outcome=?, pips_result=? WHERE trade_id=?",
                    [outcome, realized_pl, trade_id]
                )
                db.conn.commit()
            except Exception:
                pass  # Table may not exist yet

    def log_validation(self, cycle_id, instrument, validation_results) -> int:
        """Log a validation run (LOGS-03)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            cursor = conn.execute(
                """INSERT INTO validation_log
                   (cycle_id, instrument, timestamp,
                    gate1_passed, gate1_confidence, gate1_issues,
                    gate2_passed, gate2_issues,
                    contradictions, needs_llm_escalation,
                    recommendation, overall_passed, user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cycle_id, instrument, now,
                 1 if validation_results.get("gate1_passed") else 0 if validation_results.get("gate1_passed") is not None else None,
                 validation_results.get("gate1_confidence"),
                 self._to_json(validation_results.get("gate1_issues")),
                 1 if validation_results.get("gate2_passed") else 0 if validation_results.get("gate2_passed") is not None else None,
                 self._to_json(validation_results.get("gate2_issues")),
                 self._to_json(validation_results.get("contradictions")),
                 1 if validation_results.get("needs_llm_escalation") else 0,
                 validation_results.get("recommendation"),
                 1 if validation_results.get("overall_passed") else 0,
                 self._user_id),
            )
            return cursor.lastrowid

    def log_mcp_query(self, cycle_id, instrument, source, query_type,
                      response_summary, impact_on_decision, error=None) -> int:
        """Log an intelligence query (LOGS-04)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as conn:
            cursor = conn.execute(
                """INSERT INTO mcp_query_log
                   (cycle_id, instrument, timestamp, source, query_type,
                    response_summary, impact_on_decision, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (cycle_id, instrument, now, source, query_type,
                 self._to_json(response_summary), impact_on_decision, error),
            )
            return cursor.lastrowid

    # ------------------------------------------------------------------
    # Query methods (unchanged — read from local trade_log.db)
    # ------------------------------------------------------------------

    def get_signals(self, instrument=None, from_date=None, to_date=None, limit=100):
        query = "SELECT * FROM signal_log WHERE 1=1"
        params = []
        if instrument: query += " AND instrument = ?"; params.append(instrument)
        if from_date: query += " AND timestamp >= ?"; params.append(from_date)
        if to_date: query += " AND timestamp <= ?"; params.append(to_date)
        query += " ORDER BY timestamp DESC LIMIT ?"; params.append(limit)
        with self._db() as conn:
            return self._rows_to_dicts(conn.execute(query, params).fetchall())

    def get_trades(self, instrument=None, from_date=None, to_date=None, limit=100):
        query = "SELECT * FROM trade_log WHERE 1=1"
        params = []
        if instrument: query += " AND instrument = ?"; params.append(instrument)
        if from_date: query += " AND entry_time >= ?"; params.append(from_date)
        if to_date: query += " AND entry_time <= ?"; params.append(to_date)
        query += " ORDER BY entry_time DESC LIMIT ?"; params.append(limit)
        with self._db() as conn:
            return self._rows_to_dicts(conn.execute(query, params).fetchall())

    def get_validations(self, instrument=None, from_date=None, to_date=None, limit=100):
        query = "SELECT * FROM validation_log WHERE 1=1"
        params = []
        if instrument: query += " AND instrument = ?"; params.append(instrument)
        if from_date: query += " AND timestamp >= ?"; params.append(from_date)
        if to_date: query += " AND timestamp <= ?"; params.append(to_date)
        query += " ORDER BY timestamp DESC LIMIT ?"; params.append(limit)
        with self._db() as conn:
            return self._rows_to_dicts(conn.execute(query, params).fetchall())

    def get_mcp_queries(self, instrument=None, source=None, from_date=None, to_date=None, limit=100):
        query = "SELECT * FROM mcp_query_log WHERE 1=1"
        params = []
        if instrument: query += " AND instrument = ?"; params.append(instrument)
        if source: query += " AND source = ?"; params.append(source)
        if from_date: query += " AND timestamp >= ?"; params.append(from_date)
        if to_date: query += " AND timestamp <= ?"; params.append(to_date)
        query += " ORDER BY timestamp DESC LIMIT ?"; params.append(limit)
        with self._db() as conn:
            return self._rows_to_dicts(conn.execute(query, params).fetchall())

    def get_daily_summary(self, date=None, instrument=None):
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prefix = date

        with self._db() as conn:
            sig_q = "SELECT COUNT(*) FROM signal_log WHERE timestamp LIKE ?"
            sig_p = [f"{prefix}%"]
            if instrument: sig_q += " AND instrument = ?"; sig_p.append(instrument)
            signals_count = conn.execute(sig_q, sig_p).fetchone()[0]

            tq = "SELECT COUNT(*) as cnt, COALESCE(SUM(realized_pl), 0) as net_pl FROM trade_log WHERE entry_time LIKE ?"
            tp = [f"{prefix}%"]
            if instrument: tq += " AND instrument = ?"; tp.append(instrument)
            tr = conn.execute(tq, tp).fetchone()

            wq = "SELECT COUNT(*) FROM trade_log WHERE entry_time LIKE ? AND realized_pl >= 0 AND realized_pl IS NOT NULL"
            wp = [f"{prefix}%"]
            if instrument: wq += " AND instrument = ?"; wp.append(instrument)
            wins = conn.execute(wq, wp).fetchone()[0]

            lq = "SELECT COUNT(*) FROM trade_log WHERE entry_time LIKE ? AND realized_pl < 0"
            lp = [f"{prefix}%"]
            if instrument: lq += " AND instrument = ?"; lp.append(instrument)
            losses = conn.execute(lq, lp).fetchone()[0]

            vq = "SELECT COUNT(*) FROM validation_log WHERE timestamp LIKE ?"
            vp = [f"{prefix}%"]
            if instrument: vq += " AND instrument = ?"; vp.append(instrument)
            validations = conn.execute(vq, vp).fetchone()[0]

            return {
                "date": date, "total_trades": tr[0], "wins": wins, "losses": losses,
                "net_pl": tr[1], "signals_generated": signals_count,
                "trades_executed": tr[0], "validations_run": validations,
            }

    def get_trade_by_id(self, trade_id):
        with self._db() as conn:
            row = conn.execute("SELECT * FROM trade_log WHERE trade_id = ?", (trade_id,)).fetchone()
            return dict(row) if row else None
