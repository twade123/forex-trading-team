"""Flight Recorder — Trading System Observability

A ring-buffer audit system that tracks every stage of the trading pipeline.
Keeps the last N cycles per pair, auto-purges older ones. Think airplane
black box: always recording, never growing.

STAGES (in pipeline order):
─────────────────────────────────────────────────────────────────────
 1. SCOUT_SCAN         Scout scans a pair (market story, v4 score)
 2. SCOUT_ALERT        Scout generates an alert (story thesis found)
 3. SCOUT_SNIPE_CHECK  Scout checks active snipes for this pair
 4. QUEUE_ENTER        Alert enters the cycle queue
 5. QUEUE_DEQUEUE      Cycle dequeued and started
 6. CYCLE_START        Trading cycle begins for instrument
 7. DATA_OANDA         OANDA candles + account + pricing fetched
 8. DATA_INTELLIGENCE   Intelligence agent synthesis (news/macro/weather)
 9. TA_COMPUTE         Sniper V4 + EMA market picture computed (Python)
10. TA_LLM             TA agent interprets indicators (LLM)
11. VALIDATOR_CALL     Validator agent called with full data package
12. VALIDATOR_DB       Validator queries backtest DB (tool calls)
13. VALIDATOR_VERDICT  Validator returns verdict + confidence
14. CONFLUENCE_SCORE   Full confluence computed (0-100)
15. ORCHESTRATOR_LLM   Orchestrator LLM makes trade/hold decision
16. ORCHESTRATOR_MATH  make_trade_decision() computes SL/TP/size
17. EXECUTION          Order placed via OANDA (or hold)
18. WATCH_CREATE       Snipe/watch created from HOLD verdict
19. GUARDIAN_SPAWN     Guardian spawns watcher for new trade
20. GUARDIAN_THREAT    Guardian threat assessment update
21. GUARDIAN_ACTION    Guardian escalation or emergency close
22. DASHBOARD_PUSH     cycle_data.json written for dashboard
23. DASHBOARD_WS       WebSocket alert broadcast to clients
24. CYCLE_END          Cycle complete, timing + result logged
25. TRADE_CLOSE        Trade closed (win/loss), revenue recorded
26. WIN_SNIPE          Winning trade → snipe created
─────────────────────────────────────────────────────────────────────

Usage:
    from flight_recorder import flight, FlightStage

    # Record a stage
    flight.record(
        stage=FlightStage.SCOUT_SCAN,
        pair="EUR_USD",
        cycle_id="cycle_1_2026-02-22T...",
        data={"v4_score": 14, "story_score": 72},
        status="ok",  # ok | warn | error | skip
        duration_ms=234,
        note="Counter-trend reversal thesis found",
    )

    # Check data flow between stages
    flight.check_flow(cycle_id="cycle_1_...")
    # Returns: {missing_stages: [...], data_gaps: [...], bottlenecks: [...]}

    # Get recent cycles for a pair
    flight.get_cycles("EUR_USD", limit=4)
"""

import sqlite3
import json
import time
import logging
import os
import functools
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from contextlib import contextmanager

logger = logging.getLogger("trading_bot.flight_recorder")

# ── Configuration ──
RING_SIZE = 4          # Keep last N complete cycles per pair
MAX_ROWS = 50000       # Hard ceiling — raised 2026-04-07, 2000 was purging trade audit history
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DB_DIR, "flight_recorder.db")


class FlightStage(str, Enum):
    """Every discrete stage in the trading pipeline."""
    # Scout
    SCOUT_SCAN = "scout_scan"
    SCOUT_ALERT = "scout_alert"
    SCOUT_SNIPE_CHECK = "scout_snipe_check"
    SNIPE_MONITOR = "snipe_monitor"          # 5-min independent snipe check loop
    SNIPE_M1_FAST = "snipe_m1_fast"          # 60s M1 price/EMA fast-check loop
    # Queue
    QUEUE_ENTER = "queue_enter"
    QUEUE_DEQUEUE = "queue_dequeue"
    # Trading Cycle — data collection
    CYCLE_START = "cycle_start"
    DATA_OANDA = "data_oanda"
    DATA_INTELLIGENCE = "data_intelligence"
    # Trading Cycle — analysis
    TA_COMPUTE = "ta_compute"
    TA_LLM = "ta_llm"
    # Vault knowledge access
    VAULT_LOAD = "vault_load"          # Agent loaded prompt/learnings/education from vault
    VAULT_QUERY = "vault_query"        # Agent searched vault FTS for specific content
    VAULT_IMAGES = "vault_images"      # Agent loaded teaching images from image catalog
    # Trading Cycle — validation
    VALIDATOR_CALL = "validator_call"
    VALIDATOR_DB = "validator_db"
    VALIDATOR_VERDICT = "validator_verdict"
    CONFLUENCE_SCORE = "confluence_score"
    # Trading Cycle — decision & execution
    ORCHESTRATOR_LLM = "orchestrator_llm"
    ORCHESTRATOR_MATH = "orchestrator_math"
    EXECUTION = "execution"
    WATCH_CREATE = "watch_create"
    # Watch_manager visibility (2026-04-23): gates and exceptions that used to
    # log-only, now flight-recorded so dashboard/audit can see why a snipe that
    # hit 100% didn't fire a trade.
    WATCH_GATE_BLOCKED = "watch_gate_blocked"    # A gate (sanity / ema_ordering / overlap / cooldown / market_picture) blocked a triggering watch
    WATCH_EXCEPTION    = "watch_exception"        # Outer check_active_watches catch — watch silently skipped due to code error
    # Guardian
    GUARDIAN_SPAWN = "guardian_spawn"
    GUARDIAN_THREAT = "guardian_threat"
    GUARDIAN_ACTION = "guardian_action"
    # Dashboard
    DASHBOARD_PUSH = "dashboard_push"
    DASHBOARD_WS = "dashboard_ws"
    # Cycle lifecycle
    CYCLE_END = "cycle_end"
    # Trade lifecycle
    TRADE_CLOSE = "trade_close"
    # Cascade phase transitions — one record per state change per trade
    TRADE_PHASE = "trade_phase"
    WIN_SNIPE = "win_snipe"

    # ── Learning Loop (post-trade improvement pipeline) ────────────────
    LEARNING_AUDIT = "learning_audit"          # Trade audit → learning extraction started
    LEARNING_SCOUT = "learning_scout"          # Scout learnings written to vault
    LEARNING_VALIDATOR = "learning_validator"   # Validator learnings written to vault
    LEARNING_GUARDIAN = "learning_guardian"     # Guardian learnings written to vault
    LEARNING_KNOWLEDGE = "learning_knowledge"  # Per-pair knowledge.json updated
    LEARNING_RETRO = "learning_retro"          # Full-session retrospective completed
    LEARNING_DRIFT = "learning_drift"          # Rolling audit drift → vault learnings
    LEARNING_THESIS = "learning_thesis"        # Thesis audit → vault structural learnings
    LEARNING_TUNING = "learning_tuning"        # Risk auto-tuner → vault parameter log
    LEARNING_DASHBOARD = "learning_dashboard"  # learning_events.json written for UI
    LEARNING_COMPLETE = "learning_complete"     # Full learning loop finished for this trade

    # ── Kronos Pipeline ───────────────────────────────────────────────────
    KRONOS_HUNTER_SCAN_START    = "kronos_hunter_scan_start"    # Hunter began pair scan
    KRONOS_HUNTER_SIGNAL        = "kronos_hunter_signal"        # Hunter identified a signal
    KRONOS_HUNTER_TRADE_OPEN    = "kronos_hunter_trade_open"    # Hunter opened a trade
    KRONOS_HUNTER_SCAN_COMPLETE = "kronos_hunter_scan_complete" # Hunter scan finished
    KRONOS_SNIPE_CREATED        = "kronos_snipe_created"        # Path snipe created in watch_suggestions
    KRONOS_SNIPE_TRIGGERED      = "kronos_snipe_triggered"      # Path snipe hit entry price, trade opening
    KRONOS_SNIPE_EXPIRED        = "kronos_snipe_expired"        # Path snipe expired (stale), auto-deleted
    KRONOS_SNIPE_REPLACED       = "kronos_snipe_replaced"       # Path snipe replaced (direction changed)
    KRONOS_FILTER_CHECK         = "kronos_filter_check"         # Filter evaluating signal
    KRONOS_FILTER_REJECT        = "kronos_filter_reject"        # Filter rejected signal
    KRONOS_FILTER_PASS          = "kronos_filter_pass"          # Filter approved signal
    KRONOS_ERROR                = "kronos_error"                # Kronos pipeline error
    # Kronos Guardian (2026-04-15) — Kronos-specific threat scorer tuned from
    # indicator_profile_{backtest,live}.csv. Fires on every M1 tick for any
    # live_trades.source='kronos_hunter' trade. Captures raw indicator
    # readings + threat score so we can retrospectively compare the Kronos
    # scorer's kills/keeps against scout's scoring on the same trade.
    KRONOS_GUARDIAN_THREAT      = "kronos_guardian_threat"      # Kronos threat scorer tick (score, zone, reasons, indicators)
    KRONOS_GUARDIAN_EXIT        = "kronos_guardian_exit"        # Kronos guardian triggered exit (reason, score, pnl)
    KRONOS_GUARDIAN_SHADOW      = "kronos_guardian_shadow"      # threat_black suppressed by kill-switch
    KRONOS_RECONCILE_ORPHAN     = "kronos_reconcile_orphan"     # zombie DB row closed by reconciler
    KRONOS_AUTO_ROLLBACK        = "kronos_auto_rollback"        # tripwire fired — kronos disabled

    # ── Universal stages (non-trading) ──────────────────────────────────
    # Trevor interaction
    TREVOR_INTENT = "trevor_intent"        # what Trevor classified the request as
    TREVOR_ROUTE = "trevor_route"          # which handler/skill was dispatched
    TREVOR_RESPONSE = "trevor_response"    # response delivered + quality signal
    TREVOR_TRAINING = "trevor_training"    # training pair captured (yes/no, domain)

    # Boardroom
    BOARDROOM_START = "boardroom_start"    # deliberation begins (topic, seats)
    BOARDROOM_SEAT = "boardroom_seat"      # each seat's contribution (seat, duration)
    BOARDROOM_OPUS = "boardroom_opus"      # Opus QC called (cost, verdict)
    BOARDROOM_END = "boardroom_end"        # deliberation complete (outcome, duration)

    # Skills
    SKILL_START = "skill_start"            # skill execution begins (skill_name)
    SKILL_END = "skill_end"               # skill complete (quality_score, domain)

    # MCP
    MCP_CALL = "mcp_call"                 # MCP tool invoked (server, tool, latency)

    # ── Service health (uptime monitoring) ──────────────────────────────
    SERVICE_UP      = "service_up"        # service came online (service, pid)
    SERVICE_DOWN    = "service_down"      # service detected as down (service, last_seen)
    SERVICE_RESTART = "service_restart"   # watchdog restarted a service (service, reason)
    SERVICE_BEAT    = "service_beat"      # periodic heartbeat — service still alive (service)


# Expected stage order for a full cycle (used by check_flow)
# ORCHESTRATOR_MATH and WATCH_CREATE are conditional (trade vs hold)
CYCLE_STAGE_ORDER = [
    FlightStage.CYCLE_START,
    FlightStage.DATA_OANDA,
    FlightStage.DATA_INTELLIGENCE,
    FlightStage.TA_COMPUTE,
    FlightStage.TA_LLM,
    FlightStage.VAULT_LOAD,
    FlightStage.VAULT_QUERY,
    FlightStage.VAULT_IMAGES,
    FlightStage.VALIDATOR_CALL,
    FlightStage.VALIDATOR_DB,
    FlightStage.VALIDATOR_VERDICT,
    FlightStage.CONFLUENCE_SCORE,
    FlightStage.ORCHESTRATOR_LLM,
    FlightStage.EXECUTION,
    FlightStage.DASHBOARD_PUSH,
    FlightStage.CYCLE_END,
    # Post-trade learning loop (fires after TRADE_CLOSE)
    FlightStage.LEARNING_AUDIT,
    FlightStage.LEARNING_SCOUT,
    FlightStage.LEARNING_VALIDATOR,
    FlightStage.LEARNING_GUARDIAN,
    FlightStage.LEARNING_KNOWLEDGE,
    FlightStage.LEARNING_RETRO,
    FlightStage.LEARNING_DASHBOARD,
    FlightStage.LEARNING_COMPLETE,
]

# Stages that are conditional (don't flag as missing)
OPTIONAL_STAGES = {
    FlightStage.ORCHESTRATOR_MATH.value,  # Only on trade (not hold)
    FlightStage.WATCH_CREATE.value,       # Only on hold
    FlightStage.GUARDIAN_SPAWN.value,     # Only when trade fills
    FlightStage.DASHBOARD_WS.value,      # Only if WS clients connected
    # Learning loop — only fires after TRADE_CLOSE, not every cycle
    FlightStage.LEARNING_AUDIT.value,
    FlightStage.LEARNING_SCOUT.value,
    FlightStage.LEARNING_VALIDATOR.value,
    FlightStage.LEARNING_GUARDIAN.value,
    FlightStage.LEARNING_KNOWLEDGE.value,
    FlightStage.LEARNING_RETRO.value,
    FlightStage.LEARNING_DRIFT.value,    # Only on rolling audit (every 5th)
    FlightStage.LEARNING_THESIS.value,   # Only on weekly thesis audit
    FlightStage.LEARNING_TUNING.value,   # Only when risk tuner runs
    FlightStage.LEARNING_DASHBOARD.value,
    FlightStage.LEARNING_COMPLETE.value,
    # Universal stages — always optional (non-trading pipelines)
    FlightStage.TREVOR_INTENT.value,
    FlightStage.TREVOR_ROUTE.value,
    FlightStage.TREVOR_RESPONSE.value,
    FlightStage.TREVOR_TRAINING.value,
    FlightStage.BOARDROOM_START.value,
    FlightStage.BOARDROOM_SEAT.value,
    FlightStage.BOARDROOM_OPUS.value,
    FlightStage.BOARDROOM_END.value,
    FlightStage.SKILL_START.value,
    FlightStage.SKILL_END.value,
    FlightStage.MCP_CALL.value,
}

# Per-category row ceilings (ring buffer — enforced on record())
# Trading ceiling managed by _maybe_purge (per-pair, per-cycle)
# Universal ceilings enforced by _purge_category()
CATEGORY_CEILINGS = {
    "trevor":    500,   # ~1MB max
    "boardroom": 200,   # ~500KB max
    "skill":     200,   # ~200KB max
    "mcp":       200,   # ~200KB max
}

UNIVERSAL_STAGE_CATEGORY = {
    FlightStage.TREVOR_INTENT.value:    "trevor",
    FlightStage.TREVOR_ROUTE.value:     "trevor",
    FlightStage.TREVOR_RESPONSE.value:  "trevor",
    FlightStage.TREVOR_TRAINING.value:  "trevor",
    FlightStage.BOARDROOM_START.value:  "boardroom",
    FlightStage.BOARDROOM_SEAT.value:   "boardroom",
    FlightStage.BOARDROOM_OPUS.value:   "boardroom",
    FlightStage.BOARDROOM_END.value:    "boardroom",
    FlightStage.SKILL_START.value:      "skill",
    FlightStage.SKILL_END.value:        "skill",
    FlightStage.MCP_CALL.value:         "mcp",
}

# Fields each stage MUST include in data (for data-loss detection)
REQUIRED_FIELDS = {
    FlightStage.SCOUT_SCAN: ["story_score", "entry_type", "fan_state"],
    FlightStage.SCOUT_ALERT: ["pair", "direction", "entry_type", "opportunity_score"],
    FlightStage.DATA_OANDA: ["m15_candles", "h1_candles", "h4_candles", "balance"],
    FlightStage.DATA_INTELLIGENCE: ["verdict", "bias"],
    FlightStage.TA_COMPUTE: ["buy_score", "sell_score", "fan_state", "trend_health"],
    FlightStage.TA_LLM: ["clarity", "steps_confirmed"],
    FlightStage.VALIDATOR_VERDICT: ["verdict", "confidence"],
    FlightStage.CONFLUENCE_SCORE: ["total_score", "tradeable"],
    FlightStage.ORCHESTRATOR_LLM: ["action", "allowed"],
    FlightStage.EXECUTION: ["status"],  # filled | hold | rejected
    FlightStage.DASHBOARD_PUSH: ["wrote_file"],
    FlightStage.CYCLE_END: ["total_time_s", "steps_completed"],
    FlightStage.GUARDIAN_THREAT: ["threat_score", "zone"],
    # Learning loop stages
    FlightStage.LEARNING_AUDIT: ["audit_id", "pair", "outcome"],
    FlightStage.LEARNING_SCOUT: ["learnings_count", "learnings"],
    FlightStage.LEARNING_VALIDATOR: ["learnings_count", "learnings"],
    FlightStage.LEARNING_GUARDIAN: ["learnings_count", "learnings"],
    FlightStage.LEARNING_KNOWLEDGE: ["pair", "setup", "live_win_rate"],
    FlightStage.LEARNING_RETRO: ["capture_rate", "entry_gap_pips"],
    FlightStage.LEARNING_DRIFT: ["flags_count", "recommendations_count"],
    FlightStage.LEARNING_THESIS: ["learnings_count"],
    FlightStage.LEARNING_TUNING: ["changes_count"],
    FlightStage.LEARNING_DASHBOARD: ["events_written"],
    FlightStage.LEARNING_COMPLETE: ["total_learnings", "duration_ms"],
}


class FlightRecorder:
    """Ring-buffer flight recorder for the trading pipeline."""

    def __init__(self, db_path: str = DB_PATH, ring_size: int = RING_SIZE, user_id: int = None):
        self._db_path = db_path
        self._ring_size = ring_size
        # Resolve user_id from arg or TRADING_USER_ID env (set by serve_ui.py)
        if user_id is not None:
            self._user_id = user_id
        else:
            _env = os.environ.get("TRADING_USER_ID")
            self._user_id = int(_env) if _env else None
        self._initialized = False

    def _ensure_db(self):
        if self._initialized:
            return
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        try:  # conn.close() in finally below
            conn.execute("PRAGMA mmap_size=0")  # FUSE safety: disable mmap
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS flight_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    pair TEXT DEFAULT '',
                    cycle_id TEXT DEFAULT '',
                    trade_id TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ok',
                    duration_ms REAL DEFAULT 0,
                    data TEXT DEFAULT '{}',
                    note TEXT DEFAULT '',
                    missing_fields TEXT DEFAULT '',
                    user_id INTEGER
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_flight_cycle
                ON flight_log(cycle_id, stage)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_flight_pair_time
                ON flight_log(pair, timestamp DESC)
            """)
            # Partial index for warn/error status filter — only indexes non-ok rows
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_flight_status
                ON flight_log(status) WHERE status IN ('warn', 'error')
            """)
            # Migration: add user_id column if missing (for pre-existing DBs)
            try:
                conn.execute("ALTER TABLE flight_log ADD COLUMN user_id INTEGER")
            except sqlite3.OperationalError:
                pass  # Column already exists
            # ── session_metrics: one row per trading session for cross-session comparison ──
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_metrics (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_date TEXT NOT NULL UNIQUE,  -- YYYY-MM-DD
                    trades      INTEGER DEFAULT 0,
                    wins        INTEGER DEFAULT 0,
                    losses      INTEGER DEFAULT 0,
                    win_rate    REAL DEFAULT 0,
                    total_usd   REAL DEFAULT 0,
                    avg_pips    REAL DEFAULT 0,
                    scout_alerts INTEGER DEFAULT 0,
                    cycles_run  INTEGER DEFAULT 0,
                    exec_failures INTEGER DEFAULT 0,
                    phase3_count INTEGER DEFAULT 0,   -- retrace phase transitions
                    phase5_count INTEGER DEFAULT 0,   -- exhaustion exits
                    phase3_survival_rate REAL DEFAULT 0,  -- % that resumed after retrace
                    created_at  TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_session_metrics_date
                ON session_metrics(session_date)
            """)

            # ── trade_phases: one row per cascade phase transition per trade ──
            # Enables post-session analysis: did Phase 3 retraces survive?
            # Did Phase 5 exits fire at the right time? How many second legs ran?
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_phases (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT NOT NULL,
                    trade_id    TEXT NOT NULL,
                    pair        TEXT NOT NULL,
                    direction   TEXT NOT NULL,
                    phase       TEXT NOT NULL,    -- trending|retracing|continuing|peak|exhaustion
                    from_phase  TEXT,             -- previous phase
                    pnl_pips    REAL DEFAULT 0,   -- P&L at transition moment
                    bb_width    REAL DEFAULT 0,   -- BB% at transition
                    fan_sep_pips REAL DEFAULT 0,  -- E21-E100 separation in pips
                    retrace_depth REAL DEFAULT 0, -- % of peak BB compressed (retracing only)
                    e100_tests  INTEGER DEFAULT 0, -- E100 tests so far in retrace
                    reexpansion_count INTEGER DEFAULT 0, -- re-expansion bars (continuing only)
                    action_taken TEXT DEFAULT '',  -- what guardian did (lock_profit|trail_sl|take_50pct|etc)
                    note        TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_phases_trade
                ON trade_phases(trade_id, timestamp DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trade_phases_pair_phase
                ON trade_phases(pair, phase, timestamp DESC)
            """)
            conn.commit()
        finally:
            conn.close()
        self._initialized = True

    @contextmanager
    def _conn(self):
        self._ensure_db()
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA mmap_size=0")  # FUSE safety
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _user_filter(user_id=None, alias=""):
        """Build SQL WHERE clause fragment for user_id filtering.
        Returns (sql_fragment, params_list).
        Usage:  uf, up = self._user_filter(user_id, alias='f.')
                query = f"... WHERE {uf}" ; cursor.execute(query, up)
        """
        prefix = f"{alias}" if alias else ""
        if user_id is not None:
            return f"({prefix}user_id = ? OR {prefix}user_id IS NULL)", [user_id]
        return "1=1", []

    def record(
        self,
        stage: FlightStage,
        pair: str = "",
        cycle_id: str = "",
        trade_id: str = "",
        data: Optional[Dict[str, Any]] = None,
        status: str = "ok",
        duration_ms: float = 0,
        note: str = "",
    ):
        """Record a pipeline stage event.

        Args:
            stage: Which pipeline stage
            pair: Instrument (e.g. EUR_USD)
            cycle_id: Links events within one trading cycle
            trade_id: OANDA trade ID (for guardian/close events)
            data: Key metrics for this stage (checked for completeness)
            status: ok | warn | error | skip
            duration_ms: How long this stage took
            note: Human-readable summary
        """
        data = data or {}
        stage_str = stage.value if hasattr(stage, 'value') else str(stage)

        # Check for missing required fields
        required = REQUIRED_FIELDS.get(stage, [])
        missing = [f for f in required if f not in data]
        if missing:
            logger.warning(
                "Flight %s/%s missing fields: %s",
                stage_str, pair, missing,
            )

        # Truncate data to prevent bloat (keep it lean)
        data_str = json.dumps(data, default=str)
        if len(data_str) > 4000:
            # Keep only required fields + first 3000 chars
            slim = {k: data[k] for k in required if k in data}
            slim["_truncated"] = True
            data_str = json.dumps(slim, default=str)[:4000]

        now = datetime.now(timezone.utc).isoformat()

        with self._conn() as conn:
            conn.execute("""
                INSERT INTO flight_log
                (timestamp, stage, pair, cycle_id, trade_id, status,
                 duration_ms, data, note, missing_fields, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now, stage_str, pair, cycle_id, trade_id, status,
                duration_ms, data_str, note,
                json.dumps(missing) if missing else "",
                self._user_id,
            ))

        # 2026-04-07: Purging DISABLED — all flight data kept permanently for backtesting.
        # Data has timestamp + cycle_id for querying by date range.

    def record_event(
        self,
        stage: FlightStage,
        data: Dict[str, Any] = None,
        status: str = "ok",
        duration_ms: float = 0,
        note: str = "",
    ) -> None:
        """Record a universal (non-trading) event. No pair/cycle_id required.

        Usage:
            fr.record_event(FlightStage.TREVOR_INTENT, {"intent": "trading", "confidence": 0.9})
            fr.record_event(FlightStage.SKILL_END, {"skill": "canvas-design", "quality": 0.95})
        """
        data = data or {}
        data_str = json.dumps(data, default=str)[:3000]
        now = datetime.now(timezone.utc).isoformat()

        with self._conn() as conn:
            conn.execute("""
                INSERT INTO flight_log
                (timestamp, stage, pair, cycle_id, trade_id, status,
                 duration_ms, data, note, missing_fields, user_id)
                VALUES (?, ?, '', '', '', ?, ?, ?, ?, '', ?)
            """, (now, stage.value, status, duration_ms, data_str, note, self._user_id))

        # Enforce category ceiling
        category = UNIVERSAL_STAGE_CATEGORY.get(stage.value)
        if category:
            self._purge_category(category)

    def _purge_category(self, category: str) -> None:
        """Enforce per-category row ceiling (ring buffer for universal stages)."""
        ceiling = CATEGORY_CEILINGS.get(category, 500)
        stage_values = [
            s for s, c in UNIVERSAL_STAGE_CATEGORY.items() if c == category
        ]
        if not stage_values:
            return
        placeholders = ",".join("?" * len(stage_values))
        with self._conn() as conn:
            count = conn.execute(
                f"SELECT COUNT(*) FROM flight_log WHERE stage IN ({placeholders})",
                stage_values,
            ).fetchone()[0]
            if count > ceiling:
                trim = count - ceiling
                conn.execute(f"""
                    DELETE FROM flight_log WHERE id IN (
                        SELECT id FROM flight_log
                        WHERE stage IN ({placeholders})
                        ORDER BY timestamp ASC LIMIT ?
                    )
                """, stage_values + [trim])

    def get_recent_universal(self, category: str = None, limit: int = 20) -> List[Dict]:
        """Get recent non-trading events for context injection.

        Args:
            category: 'trevor', 'boardroom', 'skill', 'mcp' — or None for all
            limit: max rows to return
        """
        if category:
            stage_values = [
                s for s, c in UNIVERSAL_STAGE_CATEGORY.items() if c == category
            ]
        else:
            stage_values = list(UNIVERSAL_STAGE_CATEGORY.keys())

        if not stage_values:
            return []

        placeholders = ",".join("?" * len(stage_values))
        with self._conn() as conn:
            rows = conn.execute(f"""
                SELECT timestamp, stage, status, duration_ms, data, note
                FROM flight_log
                WHERE stage IN ({placeholders})
                ORDER BY timestamp DESC
                LIMIT ?
            """, stage_values + [limit]).fetchall()
        return [dict(r) for r in rows]

    def check_flow(self, cycle_id: str, user_id=None) -> Dict[str, Any]:
        """Audit a cycle's data flow. Returns issues found.

        Returns:
            {
                "cycle_id": str,
                "stages_found": [str],
                "missing_stages": [str],
                "data_gaps": [{"stage": str, "missing": [str]}],
                "bottlenecks": [{"stage": str, "duration_ms": float}],
                "errors": [{"stage": str, "note": str}],
                "out_of_order": [str],
                "total_time_ms": float,
                "healthy": bool,
            }
        """
        uf, up = self._user_filter(user_id)
        with self._conn() as conn:
            rows = conn.execute(f"""
                SELECT stage, status, duration_ms, data, note, missing_fields, timestamp
                FROM flight_log
                WHERE cycle_id = ? AND {uf}
                ORDER BY timestamp ASC
            """, [cycle_id] + up).fetchall()

        if not rows:
            return {"cycle_id": cycle_id, "healthy": False, "error": "No data found"}

        stages_found = [r["stage"] for r in rows]
        expected = [s.value for s in CYCLE_STAGE_ORDER]

        # Missing stages (excluding optional ones)
        missing_stages = [s for s in expected if s not in stages_found and s not in OPTIONAL_STAGES]

        # Data gaps (missing required fields)
        data_gaps = []
        for r in rows:
            if r["missing_fields"]:
                try:
                    fields = json.loads(r["missing_fields"])
                    if fields:
                        data_gaps.append({"stage": r["stage"], "missing": fields})
                except json.JSONDecodeError:
                    pass

        # Bottlenecks (> 10s)
        bottlenecks = [
            {"stage": r["stage"], "duration_ms": r["duration_ms"]}
            for r in rows if r["duration_ms"] > 10000
        ]

        # Errors
        errors = [
            {"stage": r["stage"], "note": r["note"]}
            for r in rows if r["status"] == "error"
        ]

        # Out of order (stages that appear before their expected predecessor)
        out_of_order = []
        seen_indices = {}
        for s in stages_found:
            if s in expected:
                idx = expected.index(s)
                for prev_stage, prev_idx in seen_indices.items():
                    if prev_idx > idx:
                        out_of_order.append(f"{s} before {prev_stage}")
                seen_indices[s] = idx

        # Total time
        total_time_ms = sum(r["duration_ms"] for r in rows)

        healthy = (
            len(missing_stages) == 0
            and len(data_gaps) == 0
            and len(errors) == 0
            and len(out_of_order) == 0
        )

        return {
            "cycle_id": cycle_id,
            "stages_found": stages_found,
            "stage_count": len(stages_found),
            "missing_stages": missing_stages,
            "data_gaps": data_gaps,
            "bottlenecks": bottlenecks,
            "errors": errors,
            "out_of_order": out_of_order,
            "total_time_ms": total_time_ms,
            "healthy": healthy,
        }

    def check_learning_flow(self, hours_back: int = 24) -> Dict[str, Any]:
        """Audit learning loop health across recent trade closures.

        Designed for sentry agents: checks that every TRADE_CLOSE has a
        corresponding LEARNING_COMPLETE, and that the intermediate stages
        fired correctly.

        Returns:
            {
                "period_hours": int,
                "trade_closes": int,           -- how many trades closed
                "learning_loops_complete": int, -- how many have LEARNING_COMPLETE
                "learning_loops_missing": int,  -- closed trades with NO learning stages
                "learning_loops_partial": int,  -- started but didn't complete
                "errors": [{trade_id, pair, stage, note}],
                "stage_coverage": {stage: count},  -- how often each learning stage fired
                "avg_learnings_per_trade": float,
                "avg_duration_ms": float,
                "healthy": bool,
                "issues": [str],
            }
        """
        with self._conn() as conn:
            # Get all trade closures in the window
            closes = conn.execute("""
                SELECT trade_id, pair, cycle_id, timestamp, data
                FROM flight_log
                WHERE stage = 'trade_close'
                  AND timestamp >= datetime('now', ? || ' hours')
                ORDER BY timestamp DESC
            """, (f"-{hours_back}",)).fetchall()

            if not closes:
                return {
                    "period_hours": hours_back,
                    "trade_closes": 0,
                    "healthy": True,
                    "issues": ["No trade closures in period"],
                }

            # Get ALL learning stages in the window
            learning_rows = conn.execute("""
                SELECT trade_id, stage, status, duration_ms, data, note
                FROM flight_log
                WHERE stage LIKE 'learning_%'
                  AND timestamp >= datetime('now', ? || ' hours')
                ORDER BY timestamp ASC
            """, (f"-{hours_back}",)).fetchall()

        # Group learning stages by trade_id
        learning_by_trade: Dict[str, List[Dict]] = {}
        for r in learning_rows:
            tid = r["trade_id"]
            learning_by_trade.setdefault(tid, []).append(dict(r))

        issues = []
        errors = []
        complete_count = 0
        missing_count = 0
        partial_count = 0
        total_learnings = 0
        total_duration = 0
        stage_coverage: Dict[str, int] = {}

        learning_stages = [
            "learning_audit", "learning_scout", "learning_validator",
            "learning_guardian", "learning_knowledge", "learning_retro",
            "learning_dashboard", "learning_complete",
        ]

        for close in closes:
            tid = close["trade_id"]
            pair = close["pair"]
            trade_learnings = learning_by_trade.get(tid, [])

            if not trade_learnings:
                missing_count += 1
                issues.append(
                    f"Trade {tid} ({pair}) closed with NO learning loop"
                )
                continue

            stages_fired = [r["stage"] for r in trade_learnings]

            # Count stage coverage
            for s in stages_fired:
                stage_coverage[s] = stage_coverage.get(s, 0) + 1

            # Check for completion
            if "learning_complete" in stages_fired:
                complete_count += 1
                # Extract metrics from LEARNING_COMPLETE
                complete_row = next(
                    (r for r in trade_learnings if r["stage"] == "learning_complete"),
                    None
                )
                if complete_row:
                    try:
                        d = json.loads(complete_row["data"])
                        total_learnings += d.get("total_learnings", 0)
                        total_duration += d.get("duration_ms", 0)
                    except (json.JSONDecodeError, TypeError):
                        pass
            else:
                partial_count += 1
                missing = [s for s in learning_stages if s not in stages_fired]
                issues.append(
                    f"Trade {tid} ({pair}) learning loop incomplete — "
                    f"missing: {', '.join(missing)}"
                )

            # Check for errors
            for r in trade_learnings:
                if r["status"] == "error":
                    errors.append({
                        "trade_id": tid,
                        "pair": pair,
                        "stage": r["stage"],
                        "note": r["note"],
                    })

        trade_count = len(closes)
        healthy = (
            missing_count == 0
            and len(errors) == 0
            and (complete_count / max(trade_count, 1)) >= 0.8
        )

        return {
            "period_hours": hours_back,
            "trade_closes": trade_count,
            "learning_loops_complete": complete_count,
            "learning_loops_missing": missing_count,
            "learning_loops_partial": partial_count,
            "completion_rate": round(complete_count / max(trade_count, 1), 2),
            "errors": errors,
            "stage_coverage": stage_coverage,
            "avg_learnings_per_trade": (
                round(total_learnings / max(complete_count, 1), 1)
            ),
            "avg_duration_ms": (
                round(total_duration / max(complete_count, 1), 0)
            ),
            "healthy": healthy,
            "issues": issues[:20],  # cap to prevent bloat
        }

    def get_cycles(self, pair: str, limit: int = 4, user_id=None) -> List[Dict]:
        """Get summary of recent cycles for a pair."""
        uf, up = self._user_filter(user_id)
        with self._conn() as conn:
            # Get distinct cycle_ids
            cycle_rows = conn.execute(f"""
                SELECT DISTINCT cycle_id, MIN(timestamp) as started, MAX(timestamp) as ended
                FROM flight_log
                WHERE pair = ? AND cycle_id != '' AND {uf}
                GROUP BY cycle_id
                ORDER BY started DESC
                LIMIT ?
            """, [pair] + up + [limit]).fetchall()

            cycles = []
            for cr in cycle_rows:
                cid = cr["cycle_id"]
                flow = self.check_flow(cid, user_id=user_id)
                # Get the decision — scoped to this user
                dec_uf, dec_up = self._user_filter(user_id)
                dec_row = conn.execute(f"""
                    SELECT data, note FROM flight_log
                    WHERE cycle_id = ? AND stage IN ('orchestrator_llm', 'execution')
                      AND {dec_uf}
                    ORDER BY timestamp DESC LIMIT 1
                """, [cid] + dec_up).fetchone()
                decision = ""
                if dec_row:
                    try:
                        d = json.loads(dec_row["data"])
                        decision = d.get("action", dec_row["note"])
                    except Exception:
                        decision = dec_row["note"]

                cycles.append({
                    "cycle_id": cid,
                    "pair": pair,
                    "started": cr["started"],
                    "ended": cr["ended"],
                    "healthy": flow["healthy"],
                    "stages": flow["stage_count"],
                    "missing": flow["missing_stages"],
                    "errors": flow["errors"],
                    "bottlenecks": flow["bottlenecks"],
                    "data_gaps": flow["data_gaps"],
                    "total_time_ms": flow["total_time_ms"],
                    "decision": decision,
                })
            return cycles

    def get_latest_issues(self, limit: int = 20, user_id=None) -> List[Dict]:
        """Get recent warnings/errors across all pairs."""
        uf, up = self._user_filter(user_id)
        with self._conn() as conn:
            rows = conn.execute(f"""
                SELECT timestamp, stage, pair, cycle_id, status, note, missing_fields
                FROM flight_log
                WHERE (status IN ('warn', 'error') OR missing_fields != '') AND {uf}
                ORDER BY timestamp DESC
                LIMIT ?
            """, up + [limit]).fetchall()
            return [dict(r) for r in rows]

    def get_stage_timings(self, pair: str = None, limit: int = 20, user_id=None) -> Dict[str, Dict]:
        """Get average/max timing per stage (for bottleneck analysis)."""
        uf, up = self._user_filter(user_id)
        with self._conn() as conn:
            if pair:
                where = f"WHERE pair = ? AND duration_ms > 0 AND {uf}"
                params = [pair] + up
            else:
                where = f"WHERE duration_ms > 0 AND {uf}"
                params = up
            rows = conn.execute(f"""
                SELECT stage,
                       AVG(duration_ms) as avg_ms,
                       MAX(duration_ms) as max_ms,
                       COUNT(*) as count
                FROM flight_log
                {where}
                GROUP BY stage
                ORDER BY avg_ms DESC
            """, params).fetchall()
            return {r["stage"]: {"avg_ms": r["avg_ms"], "max_ms": r["max_ms"], "count": r["count"]} for r in rows}

    def get_nightly_digest(self, hours_back: int = 24, user_id=None) -> Dict:
        """Return a structured summary of the last N hours for nightly review.

        Args:
            hours_back: Window size in hours (default 24).
            user_id: If provided, restrict results to this tenant.

        Returns:
            Dict with period, total_cycles, errors, warnings, missing_stages,
            slowest_stages, pairs_active, error_count, warning_count.
        """
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
        uf, up = self._user_filter(user_id)

        with self._conn() as conn:
            # Errors
            error_rows = conn.execute(f"""
                SELECT pair, stage, note, COUNT(*) as count,
                       MIN(timestamp) as first_seen, MAX(timestamp) as last_seen
                FROM flight_log
                WHERE status = 'error' AND timestamp >= ? AND {uf}
                GROUP BY pair, stage, note
                ORDER BY count DESC
            """, [cutoff] + up).fetchall()

            # Warnings
            warn_rows = conn.execute(f"""
                SELECT pair, stage, note, COUNT(*) as count,
                       MIN(timestamp) as first_seen, MAX(timestamp) as last_seen
                FROM flight_log
                WHERE status = 'warn' AND timestamp >= ? AND {uf}
                GROUP BY pair, stage, note
                ORDER BY count DESC
            """, [cutoff] + up).fetchall()

            # Cycle count
            cycle_count = conn.execute(f"""
                SELECT COUNT(DISTINCT cycle_id) FROM flight_log
                WHERE stage = ? AND timestamp >= ? AND cycle_id != '' AND {uf}
            """, [FlightStage.CYCLE_START.value, cutoff] + up).fetchone()[0]

            # Active pairs
            pair_rows = conn.execute(f"""
                SELECT DISTINCT pair FROM flight_log
                WHERE timestamp >= ? AND pair != '' AND {uf}
            """, [cutoff] + up).fetchall()

            # Missing stages: cycles that started but lack CYCLE_END
            incomplete = conn.execute(f"""
                SELECT cycle_id, pair,
                    GROUP_CONCAT(stage) as stages_present
                FROM flight_log
                WHERE timestamp >= ? AND cycle_id != '' AND {uf}
                GROUP BY cycle_id
                HAVING stages_present NOT LIKE '%cycle_end%'
            """, [cutoff] + up).fetchall()

            # Slowest stages
            slow_rows = conn.execute(f"""
                SELECT stage, AVG(duration_ms) as avg_ms, MAX(duration_ms) as max_ms
                FROM flight_log
                WHERE duration_ms > 0 AND timestamp >= ? AND {uf}
                GROUP BY stage
                ORDER BY avg_ms DESC
                LIMIT 10
            """, [cutoff] + up).fetchall()

        now = datetime.now(timezone.utc)
        period_start = (now - timedelta(hours=hours_back)).strftime("%Y-%m-%d %H:%M")
        period_end = now.strftime("%Y-%m-%d %H:%M")

        return {
            "period": f"{period_start} → {period_end}",
            "total_cycles": cycle_count,
            "errors": [
                {"pair": r["pair"], "stage": r["stage"], "note": r["note"],
                 "count": r["count"], "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
                for r in error_rows
            ],
            "warnings": [
                {"pair": r["pair"], "stage": r["stage"], "note": r["note"],
                 "count": r["count"], "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
                for r in warn_rows
            ],
            "missing_stages": [
                {"cycle_id": r["cycle_id"], "pair": r["pair"],
                 "missing": list(
                     set(s.value for s in CYCLE_STAGE_ORDER) -
                     set((r["stages_present"] or "").split(","))
                 )}
                for r in incomplete
            ],
            "slowest_stages": [
                {"stage": r["stage"], "avg_ms": round(r["avg_ms"], 1), "max_ms": round(r["max_ms"], 1)}
                for r in slow_rows
            ],
            "pairs_active": [r["pair"] for r in pair_rows],
            "error_count": len(error_rows),
            "warning_count": len(warn_rows),
        }

    def get_today_summary(self, since_iso: str = None, user_id=None) -> Dict:
        """Compact summary of today's trading activity, filtered at SQL level.

        Args:
            since_iso: ISO timestamp cutoff (default: midnight local time today).
                       Pass e.g. "2026-03-19T12:00:00" to start from noon.
            user_id: If provided, restrict results to this tenant.

        Returns a lean dict suitable for LLM consumption — no raw row dumps.
        """
        from datetime import datetime, timezone, timedelta

        if since_iso:
            cutoff = since_iso
        else:
            # Midnight local time today, converted to UTC ISO
            now_local = datetime.now().astimezone()
            midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff = midnight_local.astimezone(timezone.utc).isoformat()

        uf, up = self._user_filter(user_id)

        with self._conn() as conn:
            # Cycle count
            total_cycles = conn.execute(f"""
                SELECT COUNT(DISTINCT cycle_id) FROM flight_log
                WHERE stage = ? AND timestamp >= ? AND cycle_id != '' AND {uf}
            """, [FlightStage.CYCLE_START.value, cutoff] + up).fetchone()[0]

            # Execution outcomes: trade vs hold
            exec_rows = conn.execute(f"""
                SELECT pair, note, data, timestamp
                FROM flight_log
                WHERE stage = 'execution' AND timestamp >= ? AND {uf}
                ORDER BY timestamp ASC
            """, [cutoff] + up).fetchall()

            executions = []
            for r in exec_rows:
                try:
                    d = json.loads(r["data"])
                    action = d.get("action", "unknown")
                except Exception:
                    action = "unknown"
                executions.append({
                    "pair": r["pair"],
                    "action": action,
                    "note": r["note"][:120] if r["note"] else "",
                    "time": r["timestamp"][11:16],  # HH:MM only
                })

            # Trade closes with P&L
            close_rows = conn.execute(f"""
                SELECT pair, note, data, timestamp
                FROM flight_log
                WHERE stage = 'trade_close' AND timestamp >= ? AND {uf}
                ORDER BY timestamp ASC
            """, [cutoff] + up).fetchall()

            closures = []
            for r in close_rows:
                try:
                    d = json.loads(r["data"])
                except Exception:
                    d = {}
                closures.append({
                    "pair": r["pair"],
                    "pnl_usd": d.get("pnl_usd"),
                    "pnl_pips": d.get("pnl_pips"),
                    "outcome": d.get("outcome", "unknown"),
                    "note": r["note"][:120] if r["note"] else "",
                    "time": r["timestamp"][11:16],
                })

            # Errors and warnings (compact)
            issues = conn.execute(f"""
                SELECT pair, stage, status, note, timestamp
                FROM flight_log
                WHERE status IN ('error', 'warn') AND timestamp >= ? AND {uf}
                ORDER BY timestamp DESC
                LIMIT 30
            """, [cutoff] + up).fetchall()

            # Active pairs
            pairs = conn.execute(f"""
                SELECT DISTINCT pair FROM flight_log
                WHERE timestamp >= ? AND pair != '' AND {uf}
            """, [cutoff] + up).fetchall()

            # Guardian actions
            guardian_rows = conn.execute(f"""
                SELECT pair, note, data, timestamp
                FROM flight_log
                WHERE stage = 'guardian_action' AND timestamp >= ? AND {uf}
                ORDER BY timestamp ASC
            """, [cutoff] + up).fetchall()

            guardian_actions = []
            for r in guardian_rows:
                try:
                    d = json.loads(r["data"])
                except Exception:
                    d = {}
                guardian_actions.append({
                    "pair": r["pair"],
                    "action": d.get("action", r["note"][:80] if r["note"] else ""),
                    "time": r["timestamp"][11:16],
                })

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        wins = sum(1 for c in closures if c["outcome"] in ("win", "profit"))
        losses = sum(1 for c in closures if c["outcome"] in ("loss", "loss"))
        total_pnl = sum(c["pnl_usd"] for c in closures if c["pnl_usd"] is not None)

        return {
            "period": f"{cutoff[11:16]} → {now_str[11:]}",
            "date": now_str[:10],
            "total_cycles": total_cycles,
            "pairs_active": [r["pair"] for r in pairs],
            "executions": executions,
            "trade_closures": closures,
            "wins": wins,
            "losses": losses,
            "total_pnl_usd": round(total_pnl, 2),
            "guardian_actions": guardian_actions,
            "issues": [
                {
                    "pair": r["pair"], "stage": r["stage"],
                    "status": r["status"], "note": r["note"][:120] if r["note"] else "",
                    "time": r["timestamp"][11:16],
                }
                for r in issues
            ],
            "issue_count": len(issues),
        }

    def summary(self, user_id=None) -> Dict:
        """Quick health check: total rows, issues, oldest/newest."""
        uf, up = self._user_filter(user_id)
        with self._conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM flight_log WHERE {uf}", up).fetchone()[0]
            errors = conn.execute(f"SELECT COUNT(*) FROM flight_log WHERE status='error' AND {uf}", up).fetchone()[0]
            warns = conn.execute(f"SELECT COUNT(*) FROM flight_log WHERE status='warn' AND {uf}", up).fetchone()[0]
            gaps = conn.execute(f"SELECT COUNT(*) FROM flight_log WHERE missing_fields != '' AND {uf}", up).fetchone()[0]
            oldest = conn.execute(f"SELECT MIN(timestamp) FROM flight_log WHERE {uf}", up).fetchone()[0]
            newest = conn.execute(f"SELECT MAX(timestamp) FROM flight_log WHERE {uf}", up).fetchone()[0]
            pairs = conn.execute(f"SELECT DISTINCT pair FROM flight_log WHERE pair != '' AND {uf}", up).fetchall()
            return {
                "total_rows": total,
                "errors": errors,
                "warnings": warns,
                "data_gaps": gaps,
                "oldest": oldest,
                "newest": newest,
                "pairs": [r[0] for r in pairs],
                "ring_size": self._ring_size,
                "max_rows": MAX_ROWS,
            }


# ── Timer context manager for easy instrumentation ──

@contextmanager
def flight_timer(stage: FlightStage, pair: str = "", cycle_id: str = "",
                 trade_id: str = "", note: str = "", data: Optional[Dict] = None):
    """Context manager that times a stage and records it.

    Usage:
        with flight_timer(FlightStage.TA_COMPUTE, pair="EUR_USD", cycle_id=cid,
                          data={"buy_score": 14}) as ft:
            # ... do work ...
            ft["data"]["sell_score"] = 8  # add data during execution

    On exit, automatically records timing and status (error if exception).
    """
    record_data = data or {}
    result = {"data": record_data, "status": "ok", "note": note}
    start = time.time()
    try:
        yield result
    except Exception as e:
        result["status"] = "error"
        result["note"] = f"{note} | ERROR: {str(e)[:200]}" if note else str(e)[:200]
        raise
    finally:
        elapsed_ms = (time.time() - start) * 1000
        flight.record(
            stage=stage,
            pair=pair,
            cycle_id=cycle_id,
            trade_id=trade_id,
            data=result["data"],
            status=result["status"],
            duration_ms=elapsed_ms,
            note=result["note"],
        )


# ── Singleton ──
flight = FlightRecorder()
