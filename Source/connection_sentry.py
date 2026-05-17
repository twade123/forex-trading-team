"""Connection Sentry — Health Monitoring for Every Trading System Connection

Runs periodic heartbeat checks on all connections: OANDA API, databases,
WebSocket, SSE, Scout process, Guardian process, and dashboard JSON files.

Three layers:
  Layer 1 — Heartbeat contracts (per-connection liveness checks)
  Layer 2 — Connection matrix (cascade detection)
  Layer 3 — Cross-workspace topology (future)

Usage:
    from connection_sentry import sentry

    # Start monitoring (call once at system boot)
    sentry.start()

    # Get current health snapshot
    report = sentry.get_report()

    # Check specific connection
    status = sentry.check("forex.oanda.api")

    # Stop monitoring
    sentry.stop()
"""

import json
import logging
import os
import sqlite3
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from db_pool import get_trading_forex

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
_SOURCE_DIR = Path(__file__).parent.resolve()
_PROJECT_DIR = _SOURCE_DIR.parent.resolve()
_JARVIS_DIR = _SOURCE_DIR.parent.parent.resolve()

_DB_DIR = _JARVIS_DIR / "Database"
_DATA_DIR = _PROJECT_DIR / "Data"
_DASHBOARD_DIR = _PROJECT_DIR / "dashboard"

TREVOR_DB = _DB_DIR / "v2" / "trading_forex.db"
BOARDROOM_DB = _DB_DIR / "v2" / "workspaces.db"
USERS_DB = _DB_DIR / "v2" / "core.db"
FLIGHT_DB = _SOURCE_DIR / "flight_recorder.db"

SENTRY_REPORT_PATH = _DASHBOARD_DIR / "sentry_report.json"
SCOUT_HEARTBEAT = Path("/tmp/scout_last_scan")
SCOUT_PAUSE_FILE = Path("/tmp/scout_paused")
WATCHDOG_PAUSE_FILE = Path("/tmp/watchdog.pause")


# ── Enums ────────────────────────────────────────────────────────────────

class ConnStatus(str, Enum):
    UP = "UP"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class OverallHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


# ── Data classes ─────────────────────────────────────────────────────────

@dataclass
class HeartbeatResult:
    connection_id: str
    status: ConnStatus
    latency_ms: float = 0.0
    last_success: str = ""
    last_failure: str = ""
    consecutive_failures: int = 0
    uptime_pct_24h: float = 100.0
    error: str = ""
    detail: str = ""

    def to_dict(self) -> Dict:
        return {
            "id": self.connection_id,
            "status": self.status.value,
            "latency_ms": round(self.latency_ms, 1),
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "consecutive_failures": self.consecutive_failures,
            "uptime_24h": f"{self.uptime_pct_24h:.1f}%",
            "error": self.error,
            "detail": self.detail,
        }


@dataclass
class CascadeAlert:
    failed_connection: str
    impacted: List[str]
    severity: Severity
    message: str
    timestamp: str = ""

    def to_dict(self) -> Dict:
        return {
            "failed_connection": self.failed_connection,
            "impacted": self.impacted,
            "severity": self.severity.value,
            "message": self.message,
            "timestamp": self.timestamp or _now_iso(),
        }


@dataclass
class HeartbeatContract:
    """Definition of how to check a connection."""
    connection_id: str
    check_fn: Callable[[], HeartbeatResult]
    interval_s: float = 60.0
    timeout_s: float = 5.0
    failure_threshold: int = 3
    severity: Severity = Severity.HIGH
    recovery_action: str = "alert"
    cascade_impacts: List[str] = field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_db(db_path: Path, query: str = "SELECT 1") -> HeartbeatResult:
    """Check if a SQLite database is accessible and responsive."""
    conn_id = f"db.{db_path.stem}"
    t0 = time.monotonic()
    conn = None
    try:
        if not db_path.exists():
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.DOWN,
                error=f"File not found: {db_path}",
            )
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA busy_timeout=5000")
        result = conn.execute(query).fetchone()
        latency = (time.monotonic() - t0) * 1000
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.UP,
            latency_ms=latency,
            last_success=_now_iso(),
            detail=f"query returned {result}",
        )
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.DOWN,
            latency_ms=latency,
            last_failure=_now_iso(),
            error=str(e),
        )
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _check_trade_log() -> HeartbeatResult:
    """Check if trade_log (setup_revenue) is accessible via pooled connection.

    Uses pooled connection from db_pool.get_trading_forex() — does NOT call close()
    on pooled connections.
    """
    conn_id = "db.trade_log"
    t0 = time.monotonic()
    try:
        conn = get_trading_forex()
        result = conn.execute("SELECT count(*) FROM setup_revenue").fetchone()
        latency = (time.monotonic() - t0) * 1000
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.UP,
            latency_ms=latency,
            last_success=_now_iso(),
            detail=f"query returned {result}",
        )
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.DOWN,
            latency_ms=latency,
            last_failure=_now_iso(),
            error=str(e),
        )


def _check_port(host: str, port: int, conn_id: str) -> HeartbeatResult:
    """Check if a TCP port is accepting connections."""
    t0 = time.monotonic()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex((host, port))
        sock.close()
        latency = (time.monotonic() - t0) * 1000
        if result == 0:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.UP,
                latency_ms=latency,
                last_success=_now_iso(),
                detail=f"port {port} open",
            )
        else:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.DOWN,
                latency_ms=latency,
                last_failure=_now_iso(),
                error=f"port {port} refused (errno={result})",
            )
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.DOWN,
            latency_ms=latency,
            last_failure=_now_iso(),
            error=str(e),
        )


def _check_http(url: str, conn_id: str, timeout: float = 5.0) -> HeartbeatResult:
    """Check if an HTTP endpoint responds."""
    t0 = time.monotonic()
    try:
        import requests as req
        resp = req.get(url, timeout=timeout)
        latency = (time.monotonic() - t0) * 1000
        if resp.status_code < 500:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.UP,
                latency_ms=latency,
                last_success=_now_iso(),
                detail=f"HTTP {resp.status_code}",
            )
        else:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.DEGRADED,
                latency_ms=latency,
                last_failure=_now_iso(),
                error=f"HTTP {resp.status_code}",
            )
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.DOWN,
            latency_ms=latency,
            last_failure=_now_iso(),
            error=str(e),
        )


def _check_file_freshness(path: Path, max_age_s: float, conn_id: str) -> HeartbeatResult:
    """Check if a file exists and was modified within max_age_s seconds."""
    try:
        if not path.exists():
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.DOWN,
                error=f"File not found: {path.name}",
            )
        age = time.time() - path.stat().st_mtime
        if age <= max_age_s:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.UP,
                last_success=_now_iso(),
                detail=f"age={age:.0f}s (limit={max_age_s:.0f}s)",
            )
        else:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.DEGRADED,
                last_failure=_now_iso(),
                error=f"stale: age={age:.0f}s > limit={max_age_s:.0f}s",
            )
    except Exception as e:
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.DOWN,
            error=str(e),
        )


# ── OANDA Check (special: uses existing client if available) ────────────

def _check_oanda() -> HeartbeatResult:
    """Check OANDA API connectivity via a lightweight account ping."""
    conn_id = "forex.oanda.api"
    t0 = time.monotonic()
    try:
        # Try to use the existing OandaClient
        try:
            from config import get_oanda_credentials
            creds = get_oanda_credentials()
            if not creds or not creds.get("token"):
                return HeartbeatResult(
                    connection_id=conn_id,
                    status=ConnStatus.DOWN,
                    error="No OANDA credentials configured",
                )
        except Exception:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.UNKNOWN,
                error="Cannot load OANDA credentials",
            )

        import requests as req
        api_url = creds.get("api_url", "https://api-fxpractice.oanda.com")
        account_id = creds.get("account_id", "")
        headers = {
            "Authorization": f"Bearer {creds['token']}",
            "Content-Type": "application/json",
        }
        resp = req.get(
            f"{api_url}/v3/accounts/{account_id}/summary",
            headers=headers,
            timeout=5,
        )
        latency = (time.monotonic() - t0) * 1000

        if resp.status_code == 200:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.UP,
                latency_ms=latency,
                last_success=_now_iso(),
                detail=f"account {account_id[:8]}... OK",
            )
        elif resp.status_code == 429:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.DEGRADED,
                latency_ms=latency,
                last_failure=_now_iso(),
                error=f"Rate limited (429)",
            )
        else:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.DOWN,
                latency_ms=latency,
                last_failure=_now_iso(),
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.DOWN,
            latency_ms=latency,
            last_failure=_now_iso(),
            error=str(e),
        )


# ── Scout Process Check ─────────────────────────────────────────────────

def _check_scout_process() -> HeartbeatResult:
    """Check Scout liveness via its heartbeat file and health port."""
    conn_id = "forex.scout.process"

    # Check 1: Heartbeat file freshness (Scout writes /tmp/scout_last_scan every cycle)
    if SCOUT_PAUSE_FILE.exists():
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.DEGRADED,
            detail="Scout is paused (user request)",
        )

    if SCOUT_HEARTBEAT.exists():
        age = time.time() - SCOUT_HEARTBEAT.stat().st_mtime
        # Scout runs every 5 min (300s). Allow 2x buffer = 600s.
        if age > 600:
            # Also check HTTP health
            http_result = _check_http("http://127.0.0.1:8768/", conn_id, timeout=3)
            if http_result.status == ConnStatus.UP:
                return HeartbeatResult(
                    connection_id=conn_id,
                    status=ConnStatus.DEGRADED,
                    detail=f"Process alive but heartbeat stale ({age:.0f}s old). May be stuck.",
                )
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.DOWN,
                error=f"Heartbeat stale ({age:.0f}s) and health port unresponsive",
            )
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.UP,
            last_success=_now_iso(),
            detail=f"Last scan {age:.0f}s ago",
        )

    # No heartbeat file — check health port as fallback
    http_result = _check_http("http://127.0.0.1:8768/", conn_id, timeout=3)
    if http_result.status == ConnStatus.UP:
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.DEGRADED,
            detail="Health port responding but no heartbeat file",
        )
    return HeartbeatResult(
        connection_id=conn_id,
        status=ConnStatus.DOWN,
        error="No heartbeat file and health port unresponsive",
    )


# ── Guardian Process Check ──────────────────────────────────────────────

def _check_guardian_process() -> HeartbeatResult:
    """Check Guardian via its module-level instance reference."""
    conn_id = "forex.guardian.process"
    try:
        # The guardian is instantiated in trading_api_routes.py as _guardian_instance
        from trading_api_routes import _guardian_instance, _guardian_loop
        if _guardian_instance is None:
            return HeartbeatResult(
                connection_id=conn_id,
                status=ConnStatus.DOWN,
                error="Guardian not instantiated",
            )

        stats = _guardian_instance.get_stats() if hasattr(_guardian_instance, 'get_stats') else {}
        watchers = getattr(_guardian_instance, '_watchers', {})
        watcher_count = len(watchers)

        # Check if there are open trades but no watchers
        # (We can't query OANDA here without a credential, so just report state)
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.UP,
            last_success=_now_iso(),
            detail=f"active_watchers={watcher_count}, stats={json.dumps(stats)[:200]}",
        )
    except ImportError:
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.UNKNOWN,
            error="Cannot import trading_api_routes",
        )
    except Exception as e:
        return HeartbeatResult(
            connection_id=conn_id,
            status=ConnStatus.DOWN,
            error=str(e),
        )


# ── In-Memory State Snapshot ────────────────────────────────────────────

def _get_in_memory_state() -> Dict[str, Any]:
    """Snapshot of volatile in-memory state that dies on restart."""
    state = {
        "scout_heartbeat_age_s": None,
        "guardian_active_watchers": 0,
        "scout_paused": SCOUT_PAUSE_FILE.exists(),
        "watchdog_paused": WATCHDOG_PAUSE_FILE.exists(),
    }

    # Scout heartbeat age
    if SCOUT_HEARTBEAT.exists():
        state["scout_heartbeat_age_s"] = round(time.time() - SCOUT_HEARTBEAT.stat().st_mtime, 0)

    # Guardian watchers
    try:
        from trading_api_routes import _guardian_instance
        if _guardian_instance and hasattr(_guardian_instance, '_watchers'):
            state["guardian_active_watchers"] = len(_guardian_instance._watchers)
    except Exception:
        pass

    # Running cycles (ThreadPoolExecutor tasks)
    try:
        from trading_api_routes import _BACKGROUND_EXECUTOR
        # ThreadPoolExecutor._work_queue.qsize() is an approximation
        if _BACKGROUND_EXECUTOR:
            state["background_worker_threads"] = _BACKGROUND_EXECUTOR._max_workers
    except Exception:
        pass

    return state


# ── CASCADE DETECTION (Layer 2) ─────────────────────────────────────────

# Connection matrix: who reads/writes what
_CASCADE_RULES: List[Tuple[str, List[str], Severity, str]] = [
    ("forex.oanda.api",
     ["Scout", "Guardian", "Trading Cycles", "UI balance"],
     Severity.CRITICAL,
     "OANDA API down — Scout can't scan, Guardian can't monitor, no new trades"),

    ("db.trading_forex",
     ["Scout", "Trading Cycles", "Guardian", "UI dashboard"],
     Severity.CRITICAL,
     "Primary trading database down — all trading operations blocked"),

    ("db.workspaces",
     ["Trading Cycles (watch dedup)", "UI (agent comms)"],
     Severity.HIGH,
     "Workspaces DB down — watch deduplication and agent communication impaired"),

    ("db.trade_log",
     ["Revenue feedback loop"],
     Severity.MEDIUM,
     "Trade log DB down — setup revenue data stale, not fatal"),

    ("forex.scout.process",
     ["New alerts", "Cycle triggers"],
     Severity.HIGH,
     "Scout down — no new market alerts, existing watches still run"),

    ("forex.guardian.process",
     ["Open trade monitoring"],
     Severity.CRITICAL,
     "Guardian down — open trades UNMONITORED"),

    ("forex.ws.8767",
     ["UI alert stream"],
     Severity.MEDIUM,
     "WebSocket down — UI won't show live alerts (data still in DB)"),

    ("platform.serve_ui",
     ["Entire UI", "All API endpoints"],
     Severity.CRITICAL,
     "Flask server down — entire platform inaccessible"),
]


def _detect_cascades(results: Dict[str, HeartbeatResult]) -> List[CascadeAlert]:
    """Check for cascading failures based on the connection matrix."""
    cascades = []
    for conn_id, impacted, severity, message in _CASCADE_RULES:
        result = results.get(conn_id)
        if result and result.status == ConnStatus.DOWN:
            cascades.append(CascadeAlert(
                failed_connection=conn_id,
                impacted=impacted,
                severity=severity,
                message=message,
                timestamp=_now_iso(),
            ))
    return cascades


# ═══════════════════════════════════════════════════════════════════════════
# CONNECTION SENTRY (Main Class)
# ═══════════════════════════════════════════════════════════════════════════

class ConnectionSentry:
    """Centralized connection health monitor.

    Runs a background thread that periodically executes heartbeat checks
    on all registered connections. Maintains history for uptime calculation
    and detects cascade failures.
    """

    def __init__(self):
        self._contracts: Dict[str, HeartbeatContract] = {}
        self._results: Dict[str, HeartbeatResult] = {}
        self._history: Dict[str, deque] = {}  # conn_id → deque of (timestamp, up/down)
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._check_interval = 15  # Base interval for the monitoring loop
        self._last_check: Dict[str, float] = {}  # conn_id → last check time

        # Register default heartbeat contracts
        self._register_defaults()

    def _register_defaults(self):
        """Register all heartbeat contracts from the architecture spec."""

        # ── External API ──
        self.register(HeartbeatContract(
            connection_id="forex.oanda.api",
            check_fn=_check_oanda,
            interval_s=30,
            timeout_s=5,
            failure_threshold=3,
            severity=Severity.CRITICAL,
            recovery_action="alert + pause Scout after 3 failures",
            cascade_impacts=["Scout", "Guardian", "Trading Cycles", "UI balance"],
        ))

        # ── Databases ──
        self.register(HeartbeatContract(
            connection_id="db.trading_forex",
            check_fn=lambda: _check_db(TREVOR_DB),
            interval_s=60,
            failure_threshold=2,
            severity=Severity.CRITICAL,
            recovery_action="alert + block cycles",
            cascade_impacts=["Scout", "Cycles", "Guardian", "UI dashboard"],
        ))

        self.register(HeartbeatContract(
            connection_id="db.workspaces",
            check_fn=lambda: _check_db(BOARDROOM_DB),
            interval_s=60,
            failure_threshold=2,
            severity=Severity.HIGH,
            recovery_action="alert",
            cascade_impacts=["Cycles (watch dedup)", "UI (agent comms)"],
        ))

        self.register(HeartbeatContract(
            connection_id="db.trade_log",
            check_fn=_check_trade_log,
            interval_s=60,
            failure_threshold=3,
            severity=Severity.MEDIUM,
            recovery_action="alert (data may be stale)",
            cascade_impacts=["Revenue feedback loop"],
        ))

        self.register(HeartbeatContract(
            connection_id="db.flight_recorder",
            check_fn=lambda: _check_db(FLIGHT_DB),
            interval_s=120,
            failure_threshold=5,
            severity=Severity.LOW,
            recovery_action="alert (non-critical)",
            cascade_impacts=[],
        ))

        self.register(HeartbeatContract(
            connection_id="db.users",
            check_fn=lambda: _check_db(USERS_DB),
            interval_s=120,
            failure_threshold=3,
            severity=Severity.HIGH,
            recovery_action="alert",
            cascade_impacts=["Auth", "Credential decryption"],
        ))

        # ── Real-time channels ──
        self.register(HeartbeatContract(
            connection_id="forex.ws.8767",
            check_fn=lambda: _check_port("127.0.0.1", 8767, "forex.ws.8767"),
            interval_s=15,
            failure_threshold=3,
            severity=Severity.MEDIUM,
            recovery_action="restart WebSocket server",
            cascade_impacts=["UI alert stream"],
        ))

        # ── Processes ──
        self.register(HeartbeatContract(
            connection_id="forex.scout.process",
            check_fn=_check_scout_process,
            interval_s=60,
            failure_threshold=2,
            severity=Severity.HIGH,
            recovery_action="restart Scout",
            cascade_impacts=["New alerts", "Cycle triggers"],
        ))

        self.register(HeartbeatContract(
            connection_id="forex.guardian.process",
            check_fn=_check_guardian_process,
            interval_s=60,
            failure_threshold=1,  # Guardian down is immediately critical
            severity=Severity.CRITICAL,
            recovery_action="respawn watchers",
            cascade_impacts=["Open trade monitoring"],
        ))

        # ── Platform services ──
        self.register(HeartbeatContract(
            connection_id="platform.serve_ui",
            check_fn=lambda: _check_http("http://127.0.0.1:8766/", "platform.serve_ui", timeout=5),
            interval_s=15,
            failure_threshold=2,
            severity=Severity.CRITICAL,
            recovery_action="auto-restart serve_ui.py",
            cascade_impacts=["Entire UI", "All API endpoints"],
        ))

        self.register(HeartbeatContract(
            connection_id="platform.scout_health",
            check_fn=lambda: _check_http("http://127.0.0.1:8768/", "platform.scout_health", timeout=3),
            interval_s=30,
            failure_threshold=3,
            severity=Severity.HIGH,
            recovery_action="restart Scout process",
            cascade_impacts=["Scout HTTP health endpoint"],
        ))

        # ── Dashboard JSON freshness ──
        for json_file, max_age in [
            ("cycle_data.json", 900),
            ("intelligence_status.json", 900),
            ("sentry_report.json", 900),
        ]:
            fpath = _DASHBOARD_DIR / json_file
            cid = f"forex.dashboard.{json_file.replace('.json', '')}"
            self.register(HeartbeatContract(
                connection_id=cid,
                check_fn=lambda p=fpath, a=max_age, c=cid: _check_file_freshness(p, a, c),
                interval_s=120,
                failure_threshold=2,
                severity=Severity.LOW,
                recovery_action="alert",
                cascade_impacts=[],
            ))

    def register(self, contract: HeartbeatContract):
        """Register a new heartbeat contract."""
        with self._lock:
            self._contracts[contract.connection_id] = contract
            if contract.connection_id not in self._history:
                self._history[contract.connection_id] = deque(maxlen=1440)  # 24h at 1/min
            if contract.connection_id not in self._results:
                self._results[contract.connection_id] = HeartbeatResult(
                    connection_id=contract.connection_id,
                    status=ConnStatus.UNKNOWN,
                )

    def start(self):
        """Start the background monitoring thread."""
        if self._running:
            logger.warning("[SENTRY] Already running")
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="ConnectionSentry")
        self._thread.start()
        logger.info(f"[SENTRY] Started with {len(self._contracts)} heartbeat contracts")

    def stop(self):
        """Stop the background monitoring thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("[SENTRY] Stopped")

    def _monitor_loop(self):
        """Main monitoring loop — checks each connection on its own schedule."""
        # Run all checks immediately on startup
        for conn_id in self._contracts:
            self._last_check[conn_id] = 0

        while self._running:
            try:
                now = time.monotonic()
                for conn_id, contract in list(self._contracts.items()):
                    elapsed = now - self._last_check.get(conn_id, 0)
                    if elapsed >= contract.interval_s:
                        try:
                            result = contract.check_fn()
                            self._process_result(conn_id, result, contract)
                        except Exception as e:
                            logger.error(f"[SENTRY] Check failed for {conn_id}: {e}")
                            self._process_result(conn_id, HeartbeatResult(
                                connection_id=conn_id,
                                status=ConnStatus.DOWN,
                                error=f"Check function raised: {e}",
                            ), contract)
                        self._last_check[conn_id] = time.monotonic()

                # Write sentry report to disk
                self._write_report()

            except Exception as e:
                logger.error(f"[SENTRY] Monitor loop error: {e}")

            # Sleep in small increments so we can stop quickly
            for _ in range(self._check_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _process_result(self, conn_id: str, result: HeartbeatResult,
                        contract: HeartbeatContract):
        """Process a heartbeat result: update state, track history, log transitions."""
        with self._lock:
            prev = self._results.get(conn_id)
            is_up = result.status in (ConnStatus.UP, ConnStatus.DEGRADED)

            # Track consecutive failures
            if is_up:
                result.consecutive_failures = 0
                result.last_success = result.last_success or _now_iso()
            else:
                prev_failures = prev.consecutive_failures if prev else 0
                result.consecutive_failures = prev_failures + 1
                result.last_failure = result.last_failure or _now_iso()
                # Preserve last_success from previous
                if prev and prev.last_success:
                    result.last_success = prev.last_success

            # Record in history
            self._history.setdefault(conn_id, deque(maxlen=1440)).append(
                (time.time(), is_up)
            )

            # Calculate 24h uptime
            result.uptime_pct_24h = self._calc_uptime(conn_id)

            # Log state transitions
            if prev and prev.status != result.status:
                if result.status == ConnStatus.DOWN:
                    logger.warning(
                        f"[SENTRY] {conn_id} went DOWN: {result.error} "
                        f"(failures={result.consecutive_failures}, "
                        f"threshold={contract.failure_threshold})"
                    )
                elif result.status == ConnStatus.UP and prev.status == ConnStatus.DOWN:
                    logger.info(
                        f"[SENTRY] {conn_id} recovered (was down, now UP)"
                    )

            # Log threshold breach
            if (result.consecutive_failures == contract.failure_threshold
                    and result.status == ConnStatus.DOWN):
                logger.error(
                    f"[SENTRY] THRESHOLD BREACH: {conn_id} has failed "
                    f"{contract.failure_threshold} times. "
                    f"Severity={contract.severity.value}. "
                    f"Action: {contract.recovery_action}. "
                    f"Cascades: {contract.cascade_impacts}"
                )

            self._results[conn_id] = result

    def _calc_uptime(self, conn_id: str) -> float:
        """Calculate uptime percentage from history."""
        history = self._history.get(conn_id, deque())
        if not history:
            return 100.0
        cutoff = time.time() - 86400  # 24 hours
        recent = [(ts, up) for ts, up in history if ts >= cutoff]
        if not recent:
            return 100.0
        up_count = sum(1 for _, up in recent if up)
        return round((up_count / len(recent)) * 100, 1)

    def check(self, connection_id: str) -> Optional[HeartbeatResult]:
        """Run an immediate check for a specific connection."""
        contract = self._contracts.get(connection_id)
        if not contract:
            return None
        try:
            result = contract.check_fn()
            self._process_result(connection_id, result, contract)
            return result
        except Exception as e:
            return HeartbeatResult(
                connection_id=connection_id,
                status=ConnStatus.DOWN,
                error=str(e),
            )

    def get_report(self) -> Dict[str, Any]:
        """Generate the full sentry report."""
        with self._lock:
            results_copy = dict(self._results)

        cascades = _detect_cascades(results_copy)
        in_memory = _get_in_memory_state()

        # Determine overall health
        down_critical = [
            r for r in results_copy.values()
            if r.status == ConnStatus.DOWN
            and self._contracts.get(r.connection_id, HeartbeatContract(
                connection_id="", check_fn=lambda: HeartbeatResult(connection_id="", status=ConnStatus.UNKNOWN)
            )).severity == Severity.CRITICAL
        ]
        down_any = [r for r in results_copy.values() if r.status == ConnStatus.DOWN]
        degraded = [r for r in results_copy.values() if r.status == ConnStatus.DEGRADED]

        if down_critical:
            overall = OverallHealth.CRITICAL
        elif down_any or len(degraded) > 2:
            overall = OverallHealth.DEGRADED
        else:
            overall = OverallHealth.HEALTHY

        return {
            "timestamp": _now_iso(),
            "workspace": "forex-trading-team",
            "overall_health": overall.value,
            "connections": [r.to_dict() for r in sorted(
                results_copy.values(),
                key=lambda r: (
                    0 if r.status == ConnStatus.DOWN else
                    1 if r.status == ConnStatus.DEGRADED else
                    2 if r.status == ConnStatus.UP else 3
                )
            )],
            "cascades_detected": [c.to_dict() for c in cascades],
            "in_memory_state": in_memory,
            "summary": {
                "total_connections": len(results_copy),
                "up": sum(1 for r in results_copy.values() if r.status == ConnStatus.UP),
                "degraded": len(degraded),
                "down": len(down_any),
                "unknown": sum(1 for r in results_copy.values() if r.status == ConnStatus.UNKNOWN),
                "critical_cascades": len([c for c in cascades if c.severity == Severity.CRITICAL]),
            },
        }

    def _write_report(self):
        """Write the sentry report to dashboard/sentry_report.json atomically."""
        try:
            report = self.get_report()

            # Merge with existing learning metrics (don't overwrite them)
            if SENTRY_REPORT_PATH.exists():
                try:
                    with open(SENTRY_REPORT_PATH, 'r') as f:
                        existing = json.load(f)
                    # Preserve learning metrics from health_checker.py
                    for key in ("trade_closes", "learning_loops_complete",
                                "learning_loops_missing", "avg_learnings_per_trade",
                                "scout_learnings", "validator_learnings",
                                "guardian_learnings", "drift_checks", "recommendation"):
                        if key in existing and key not in report:
                            report[key] = existing[key]
                except (json.JSONDecodeError, KeyError):
                    pass

            # Add connection pool health stats
            try:
                from db_pool import pool_stats
                report["pool"] = pool_stats()
            except Exception:
                pass

            # Atomic write: write to temp, then rename
            tmp_path = SENTRY_REPORT_PATH.with_suffix('.tmp')
            with open(tmp_path, 'w') as f:
                json.dump(report, f, indent=2)
            os.replace(str(tmp_path), str(SENTRY_REPORT_PATH))

        except Exception as e:
            logger.error(f"[SENTRY] Failed to write report: {e}")

    def get_connection_ids(self) -> List[str]:
        """List all registered connection IDs."""
        return list(self._contracts.keys())

    def get_status_summary(self) -> str:
        """One-line status summary for logging."""
        report = self.get_report()
        s = report["summary"]
        return (
            f"[SENTRY] {s['up']}↑ {s['degraded']}~ {s['down']}↓ "
            f"{s['unknown']}? | cascades={s['critical_cascades']}"
        )


# ── Module-level singleton ──────────────────────────────────────────────
sentry = ConnectionSentry()


# ── Circuit Breaker (used by oanda_client.py) ───────────────────────────

class CircuitBreaker:
    """Circuit breaker for external API calls.

    States:
      CLOSED   — normal operation, requests pass through
      OPEN     — too many failures, requests blocked for cooldown period
      HALF_OPEN — cooldown expired, next request is a test

    Usage:
        breaker = CircuitBreaker(failure_threshold=3, cooldown_s=60)

        if not breaker.allow_request():
            raise ConnectionError("OANDA circuit breaker is OPEN")

        try:
            response = make_api_call()
            breaker.record_success()
        except Exception:
            breaker.record_failure()
            raise
    """

    class State(Enum):
        CLOSED = "CLOSED"
        OPEN = "OPEN"
        HALF_OPEN = "HALF_OPEN"

    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 60,
                 name: str = "default"):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._state = self.State.CLOSED
        self._consecutive_failures = 0
        self._last_failure_time = 0.0
        self._last_state_change = time.monotonic()
        self._lock = threading.Lock()
        self._total_trips = 0
        self._total_blocked = 0

    @property
    def state(self) -> 'CircuitBreaker.State':
        with self._lock:
            return self._state

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        with self._lock:
            if self._state == self.State.CLOSED:
                return True

            if self._state == self.State.OPEN:
                # Check if cooldown has elapsed
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.cooldown_s:
                    self._state = self.State.HALF_OPEN
                    self._last_state_change = time.monotonic()
                    logger.info(
                        f"[CIRCUIT_BREAKER:{self.name}] OPEN → HALF_OPEN "
                        f"(cooldown={self.cooldown_s}s elapsed)"
                    )
                    return True  # Allow test request
                else:
                    self._total_blocked += 1
                    return False

            if self._state == self.State.HALF_OPEN:
                return True  # Allow test request

            return True

    def record_success(self):
        """Record a successful request."""
        with self._lock:
            if self._state in (self.State.HALF_OPEN, self.State.OPEN):
                logger.info(
                    f"[CIRCUIT_BREAKER:{self.name}] {self._state.value} → CLOSED "
                    f"(success recorded)"
                )
            self._state = self.State.CLOSED
            self._consecutive_failures = 0
            self._last_state_change = time.monotonic()

    def record_failure(self):
        """Record a failed request."""
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()

            if self._state == self.State.HALF_OPEN:
                # Test request failed — go back to OPEN
                self._state = self.State.OPEN
                self._total_trips += 1
                self._last_state_change = time.monotonic()
                logger.warning(
                    f"[CIRCUIT_BREAKER:{self.name}] HALF_OPEN → OPEN "
                    f"(test request failed, cooldown={self.cooldown_s}s)"
                )
            elif (self._state == self.State.CLOSED
                  and self._consecutive_failures >= self.failure_threshold):
                self._state = self.State.OPEN
                self._total_trips += 1
                self._last_state_change = time.monotonic()
                logger.error(
                    f"[CIRCUIT_BREAKER:{self.name}] CLOSED → OPEN "
                    f"(failures={self._consecutive_failures} >= "
                    f"threshold={self.failure_threshold}, "
                    f"cooldown={self.cooldown_s}s)"
                )

    def get_status(self) -> Dict[str, Any]:
        """Get circuit breaker status for monitoring."""
        with self._lock:
            return {
                "name": self.name,
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self.failure_threshold,
                "cooldown_s": self.cooldown_s,
                "total_trips": self._total_trips,
                "total_blocked": self._total_blocked,
                "time_in_state_s": round(time.monotonic() - self._last_state_change, 1),
            }


# Module-level circuit breaker for OANDA
oanda_breaker = CircuitBreaker(
    failure_threshold=3,
    cooldown_s=60,
    name="oanda",
)
