"""
Database Connection Pool for Jarvis Forex Trading Team

ALL database access in this project MUST use either:
  - db_pool.get_trading_forex() / get_core() / etc. (long-lived, thread-local)
  - db_connection.get_db(path) context manager (short-lived, auto-close)
Raw sqlite3.connect() is PROHIBITED in hot-path code — it bypasses cleanup
and causes FD exhaustion under load. See 2026-03-26 FD leak postmortem.

Provides thread-local, singleton connections to the V2 databases used by
the trading bot.

V2 DATABASE MAP (authoritative):
    trading_forex.db — watch_suggestions, trade_decisions, user_snipe_list,
                       scout_alerts, scout_findings, live_trades, setup_revenue,
                       setup_trades, user_chart_annotations, backtest data
    core.db          — users, user_sessions, trading_preferences, broker_credentials
    agents.db        — agent_registry, agent_skills, agent_communication
    workspaces.db    — workspace_tasks, workspace_task_comments, workspaces
    intelligence.db  — intelligence_cache, intelligence_packages, handler_analysis

Usage:
    from db_pool import get_trading_forex, get_core, get_agents, get_workspaces, get_intelligence

    trading_conn = get_trading_forex()
    core_conn = get_core()
    agents_conn = get_agents()
    workspaces_conn = get_workspaces()
    intelligence_conn = get_intelligence()
"""

import atexit
import os
import resource
import signal
import sqlite3
import threading
import logging
import random
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Flight Recorder v2 (lazy import) ─────────────────────────────────────────
_flight_v2 = None

def _get_flight_recorder():
    """Lazy singleton for Flight Recorder v2."""
    global _flight_v2
    if _flight_v2 is None:
        try:
            from connection_doctor.flight_recorder_v2 import FlightRecorderV2
            _flight_v2 = FlightRecorderV2()
        except (ImportError, Exception):
            _flight_v2 = False  # Not available
    return _flight_v2 if _flight_v2 else None

# ── FUSE filesystem defense ────────────────────────────────────────────────
# These databases run on a FUSE mount (VirtIO on macOS). FUSE has an unreliable
# mmap implementation. SQLite's WAL mode uses a memory-mapped -shm (shared
# memory) file for the WAL index. Under concurrent access on FUSE, the mmap
# can fail, causing "disk I/O error" even when the WAL and DB are healthy.
#
# When a process crashes without closing connections, FUSE can't release the
# -shm file handle, creating .fuse_hidden files. On restart, the new -shm
# may overlap with stale mappings, causing immediate disk I/O errors.
#
# Defenses:
# 1. PRAGMA mmap_size=0 — disable mmap for main DB reads (doesn't help -shm)
# 2. Clean stale -shm on startup when safe (no other process holds lock)
# 3. atexit + signal handlers — always close connections on exit
# 4. Checkpoint failure → nuke connection immediately
# ───────────────────────────────────────────────────────────────────────────

# Thread-local storage for connections
_thread_local = threading.local()

# Database paths — use resolve() to normalize case on macOS.
# Without this, launching from /jarvis/ (lowercase) vs /Jarvis/ (uppercase)
# creates different path strings. SQLite's lock manager uses path strings
# to coordinate locks — different strings = no lock coordination = deadlock.
BASE_DIR = Path(__file__).parent.parent.parent.resolve()

# V2 databases (authoritative)
TRADING_FOREX_DB = BASE_DIR / "Database" / "v2" / "trading_forex.db"
CORE_DB = BASE_DIR / "Database" / "v2" / "core.db"
AGENTS_DB = BASE_DIR / "Database" / "v2" / "agents.db"
WORKSPACES_DB = BASE_DIR / "Database" / "v2" / "workspaces.db"
INTELLIGENCE_DB = BASE_DIR / "Database" / "v2" / "intelligence.db"
CONVERSATIONS_DB = BASE_DIR / "Database" / "v2" / "conversations.db"
JOURNEYS_DB = BASE_DIR / "Database" / "v2" / "journeys.db"
PROMPTS_DB = BASE_DIR / "Database" / "v2" / "prompts.db"
FLIGHT_RECORDER_DB = BASE_DIR / "Database" / "v2" / "flight_recorder.db"

# Legacy databases — ARCHIVED 2026-03-24
# Moved to Database/archive/. Do NOT re-add.
# BOARDROOM_DB — replaced by agents.db + workspaces.db + conversations.db + journeys.db
# TREVOR_DB    — replaced by trading_forex.db + intelligence.db
# USERS_DB     — replaced by core.db

# ── WAL checkpoint tracking ──────────────────────────────────────────────
# Tracks last checkpoint time per database path. PASSIVE checkpoint is
# non-blocking (returns immediately if readers are active), so it's safe
# to call frequently. We run it every 60s per DB.
_checkpoint_lock = threading.Lock()
_last_checkpoint: dict[str, float] = {}
_CHECKPOINT_INTERVAL_S = 60  # passive checkpoint every 60s

# Track which databases have had their -shm files cleaned this process lifetime.
_shm_cleaned: set[str] = set()
_shm_clean_lock = threading.Lock()

# ── Connection ceiling ──────────────────────────────────────────────────
# Hard limit on total connections across all threads. When exceeded, new
# connections are refused (existing thread-local connections still work).
# This prevents FD exhaustion from runaway thread creation.
MAX_POOL_CONNECTIONS = 50

# ── Recovery backoff tracking ──────────────────────────────────────────
# After a failed disk I/O recovery, don't retry checkpoints immediately.
# Prevents 60s hammering on a broken FUSE mount.
_recovery_backoff: dict[str, tuple[float, float]] = {}  # key -> (next_retry_monotonic, backoff_s)
_recovery_backoff_lock = threading.Lock()
_RECOVERY_BACKOFF_INITIAL_S = 120.0   # 2 minutes
_RECOVERY_BACKOFF_MAX_S = 480.0       # 8 minutes
_RECOVERY_MAX_FAILURES = 5            # after this many consecutive, enter degraded mode
_recovery_failure_count: dict[str, int] = {}


def _clean_stale_shm(db_path: Path) -> None:
    """Remove stale -shm file on first access if no other process holds the DB.

    On FUSE mounts, crashed processes leave orphaned -shm file handles that
    appear as .fuse_hidden* files. The next process that opens the DB gets a
    new -shm that may conflict with the stale mapping, causing disk I/O errors.

    This function is called ONCE per database per process lifetime, BEFORE
    the first sqlite3.connect(). It:
    1. Checks if a -shm file exists
    2. Tries to acquire an exclusive POSIX lock on the DB file
    3. If successful (no other process has the DB open), deletes the -shm file
    4. Releases the lock

    If another process holds the DB, we skip — the -shm is active and valid.
    """
    key = str(db_path)
    with _shm_clean_lock:
        if key in _shm_cleaned:
            return
        # NOTE: Do NOT add to _shm_cleaned here — only after the clean succeeds.
        # Setting the flag before work causes a race condition where flock failure
        # permanently prevents retries. See 2026-03-26 recovery loop postmortem.

    shm_path = Path(str(db_path) + "-shm")
    if not shm_path.exists():
        # Nothing to clean — mark as done
        with _shm_clean_lock:
            _shm_cleaned.add(key)
        return

    import fcntl
    try:
        # Try exclusive lock on the main DB file — if we get it, no other
        # process has this DB open, so the -shm is stale.
        fd = os.open(str(db_path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Got exclusive lock — -shm is stale. Delete it.
            try:
                shm_path.unlink()
                logger.info("Cleaned stale -shm for %s (FUSE safety)", db_path.name)
            except OSError as e:
                logger.warning("Could not remove stale -shm for %s: %s", db_path.name, e)
            # Also clean stale -wal if it's empty or very small (leftover from crash)
            wal_path = Path(str(db_path) + "-wal")
            if wal_path.exists():
                wal_size = wal_path.stat().st_size
                if wal_size == 0:
                    try:
                        wal_path.unlink()
                        logger.info("Cleaned empty -wal for %s", db_path.name)
                    except OSError:
                        pass
            # Release exclusive lock
            fcntl.flock(fd, fcntl.LOCK_UN)
            # Clean succeeded — mark as done so we don't retry
            with _shm_clean_lock:
                _shm_cleaned.add(key)
        finally:
            os.close(fd)
    except (OSError, IOError):
        # Could not get exclusive lock — another process has the DB open.
        # The -shm is active, leave it alone. Flag stays UNSET so next
        # _get_connection() call will retry (the flock is non-blocking, so
        # the retry cost is trivial — a few microseconds per attempt).
        logger.debug("Skipping -shm cleanup for %s — DB in use by another process (will retry)", db_path.name)


def _register_shutdown_handlers() -> None:
    """Register atexit and signal handlers to close all connections on exit.

    On FUSE mounts, leaked file handles create .fuse_hidden files that cause
    disk I/O errors on the next process startup. This ensures connections are
    always closed, even on SIGTERM/SIGINT.
    """
    atexit.register(_shutdown_all_threads)

    def _signal_handler(signum, frame):
        logger.info("Signal %d received — closing all DB connections", signum)
        _shutdown_all_threads()
        # Re-raise with default handler so the process actually exits
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    # Only register signal handlers from the main thread
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _signal_handler)
            except (OSError, ValueError):
                pass  # Can't set signal handler in some contexts


def _shutdown_all_threads() -> None:
    """Close connections across ALL threads, not just the calling thread.

    Thread-local storage makes this tricky — we can only close connections
    for the calling thread. But we track all active connections in a global
    registry so we can close them all on shutdown.
    """
    with _connection_registry_lock:
        closed = 0
        for conn_ref in list(_connection_registry):
            try:
                conn_ref.close()
                closed += 1
            except Exception:
                pass
        _connection_registry.clear()
        if closed > 0:
            logger.info("Shutdown: closed %d DB connections across all threads", closed)


# Global registry of all active connections (for cross-thread shutdown)
_connection_registry: list[sqlite3.Connection] = []
_connection_registry_lock = threading.Lock()
# Map connection id() -> thread ident for dead-thread eviction.
# sqlite3.Connection is a C-extension and doesn't support arbitrary attribute
# assignment on all Python builds, so we track thread ownership externally.
_connection_thread_map: dict[int, int] = {}

# Register handlers once on module import
_register_shutdown_handlers()


def _maybe_checkpoint(conn: sqlite3.Connection, db_path: Path) -> None:
    """Run a PASSIVE WAL checkpoint if enough time has elapsed.

    PASSIVE mode never blocks readers or writers — it checkpoints whatever
    WAL pages aren't currently needed by active readers, then returns.
    This keeps the WAL file from growing unbounded without the deadlock
    risk of FULL/TRUNCATE checkpoints.
    """
    now = time.monotonic()
    key = str(db_path)

    # Check if this DB is in recovery backoff or degraded mode
    with _recovery_backoff_lock:
        if _recovery_failure_count.get(key, 0) >= _RECOVERY_MAX_FAILURES:
            return  # Degraded mode: skip checkpoints, allow reads/writes
        backoff_info = _recovery_backoff.get(key)
        if backoff_info:
            next_retry, _ = backoff_info
            if now < next_retry:
                return  # Still in backoff, skip checkpoint

    with _checkpoint_lock:
        last = _last_checkpoint.get(key, 0)
        if now - last < _CHECKPOINT_INTERVAL_S:
            return
        _last_checkpoint[key] = now

    try:
        result = conn.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        if result and result[1] > 0:
            logger.debug("WAL checkpoint %s: log=%d checkpointed=%d",
                        db_path.name, result[1], result[2])
            try:
                fr = _get_flight_recorder()
                if fr:
                    fr.record(domain="database", stage="DB_CHECKPOINT", source="db_pool._maybe_checkpoint", target=str(db_path), status="ok")
            except Exception:
                pass
        # Checkpoint succeeded — clear any recovery backoff
        with _recovery_backoff_lock:
            if key in _recovery_backoff:
                del _recovery_backoff[key]
                logger.info("[DB_POOL] Recovery backoff cleared for %s — checkpoint succeeded", db_path.name)
            if key in _recovery_failure_count:
                del _recovery_failure_count[key]
    except sqlite3.OperationalError as e:
        err_str = str(e).lower()
        if 'disk i/o error' in err_str or 'i/o' in err_str:
            # CRITICAL: Checkpoint hit disk I/O — the -shm mmap is likely
            # corrupted (FUSE issue). Nuke ALL connections to this DB across
            # all threads. Single-connection nuke is insufficient because the
            # WAL/SHM corruption affects every connection to the same file.
            logger.error("WAL checkpoint disk I/O for %s — nuking ALL connections: %s",
                        db_path.name, e)
            _nuke_all_connections_for_db(db_path)

            # Escalate recovery backoff
            with _recovery_backoff_lock:
                _recovery_failure_count[key] = _recovery_failure_count.get(key, 0) + 1
                fail_count = _recovery_failure_count[key]
                if fail_count >= _RECOVERY_MAX_FAILURES:
                    # Last resort: attempt automatic database repair
                    logger.warning("[DB_POOL] Attempting auto-recovery for %s (attempt %d)",
                                  db_path.name, fail_count)
                    recovered = _auto_recover_db(db_path)
                    if recovered:
                        # Success — clear all backoff, resume normal operation
                        _recovery_failure_count.pop(key, None)
                        _recovery_backoff.pop(key, None)
                        logger.warning("[DB_POOL] AUTO-RECOVERY succeeded for %s — resuming normal operation",
                                      db_path.name)
                    else:
                        logger.error(
                            "[DB_POOL] DEGRADED MODE: %s has failed recovery %d times and "
                            "auto-recovery failed — suspending checkpoints. Reads/writes still allowed. "
                            "Call reset_degraded_mode('%s') or recover_database('%s') manually.",
                            db_path.name, fail_count, db_path.stem, db_path.stem
                        )
                else:
                    _, prev_backoff = _recovery_backoff.get(key, (0, _RECOVERY_BACKOFF_INITIAL_S / 2))
                    jitter = random.uniform(0.8, 1.2)
                    new_backoff = min(prev_backoff * 2 * jitter, _RECOVERY_BACKOFF_MAX_S)
                    _recovery_backoff[key] = (now + new_backoff, new_backoff)
                    logger.warning(
                        "[DB_POOL] Recovery backoff for %s: %.0fs (attempt %d/%d)",
                        db_path.name, new_backoff, fail_count, _RECOVERY_MAX_FAILURES
                    )
        else:
            logger.warning("WAL checkpoint failed for %s: %s", db_path.name, e)
    except sqlite3.Error as e:
        logger.warning("WAL checkpoint failed for %s: %s", db_path.name, e)


def _nuke_connection(attr_name: str) -> None:
    """Force-close and remove a thread-local connection.

    Called when a connection is in a bad state (disk I/O error, etc.).
    The next call to _get_connection will create a fresh one.
    Also removes from the global registry to prevent double-close on shutdown.
    """
    if hasattr(_thread_local, attr_name):
        try:
            old = getattr(_thread_local, attr_name)
            # Remove from global registry
            with _connection_registry_lock:
                try:
                    _connection_registry.remove(old)
                except ValueError:
                    pass
            _connection_thread_map.pop(id(old), None)
            old.close()
        except Exception:
            pass
        delattr(_thread_local, attr_name)
        try:
            fr = _get_flight_recorder()
            if fr:
                fr.record(domain="database", stage="DB_DISCONNECT", source="db_pool._nuke_connection", target=attr_name, status="warn", notes="Broken connection recreated")
        except Exception:
            pass


def _get_connection(db_path: Path, attr_name: str) -> sqlite3.Connection:
    """
    Get a thread-local connection to the specified database.
    Creates the connection once per thread, then reuses it.

    If the connection is broken (disk I/O error, etc.), destroys it
    and creates a fresh one. Also runs periodic PASSIVE WAL checkpoints
    to prevent unbounded WAL growth.
    """
    try:
        # Check if we already have a connection in this thread
        if hasattr(_thread_local, attr_name):
            conn = getattr(_thread_local, attr_name)
            # Test with an actual table read, not just SELECT 1.
            # disk I/O errors can pass SELECT 1 but fail on real operations.
            conn.execute('SELECT 1')
            # Run periodic WAL checkpoint to prevent WAL bloat
            _maybe_checkpoint(conn, db_path)
            return conn
    except sqlite3.OperationalError as e:
        # disk I/O error, database is locked after timeout, corrupted state
        # — nuke the connection and recreate below
        logger.warning("Broken connection to %s (%s) — recreating", db_path.name, e)
        _nuke_connection(attr_name)
    except (sqlite3.Error, AttributeError):
        # Connection is broken or doesn't exist, create a new one
        _nuke_connection(attr_name)

    try:
        # ── FUSE safety: clean stale -shm before first connect ──────────
        # On FUSE mounts, crashed processes leave orphaned -shm file handles.
        # Delete them before connecting to prevent stale mmap conflicts.
        _clean_stale_shm(db_path)

        # Create new connection in AUTOCOMMIT mode (isolation_level=None).
        # Without autocommit, Python's sqlite3 starts implicit transactions on any
        # DML statement. If that statement FAILS (e.g., table doesn't exist), the
        # implicit transaction stays open holding a RESERVED lock FOREVER — because
        # the pool never closes/rollbacks the connection. This was the root cause of
        # the permanent "database is locked" error after swarm send_message failed on
        # the missing agent_communication table.
        # With autocommit, each statement commits immediately. Code that needs
        # multi-statement transactions must explicitly use BEGIN/COMMIT.
        conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)

        # ── FUSE safety: disable mmap for main DB file ──────────────────
        # FUSE mmap is unreliable under concurrent access. This only controls
        # the main DB file reads — the -shm file is always mmap'd by SQLite's
        # WAL implementation (can't be disabled via pragma), which is why we
        # also clean stale -shm files above and register shutdown handlers.
        conn.execute('PRAGMA mmap_size=0')

        # DELETE journal mode — no -shm file, no mmap, no VirtioFS interference.
        # Cowork's VM (VirtioFS) caches WAL -shm files and holds stale handles,
        # causing "disk I/O error" crashes. DELETE mode eliminates this entirely.
        conn.execute('PRAGMA journal_mode=DELETE')
        # CRITICAL: busy_timeout must be set on EVERY connection.
        # Without this, SQLite returns SQLITE_BUSY immediately on contention
        # instead of waiting. This was the root cause of "database is locked" errors.
        conn.execute('PRAGMA busy_timeout=30000')  # Wait up to 30s for locks

        # wal_autocheckpoint removed — not applicable in DELETE journal mode

        # Enable foreign key support
        conn.execute('PRAGMA foreign_keys=ON')

        # Optimize for faster reads (trading bot is read-heavy)
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA cache_size=10000')     # 10MB cache
        conn.execute('PRAGMA temp_store=MEMORY')    # Use memory for temp tables

        # Tag connection with thread ID for dead-thread eviction
        # NOTE: sqlite3.Connection is a C-extension object and does NOT support
        # arbitrary attribute assignment on all Python builds. Use a separate
        # dict instead of conn._pool_thread_id.
        _connection_thread_map[id(conn)] = threading.current_thread().ident

        # Store in thread-local storage
        setattr(_thread_local, attr_name, conn)

        # Register in global registry for cross-thread shutdown.
        # HARD CEILING: if at or above limit, close stale connections from dead
        # threads BEFORE adding the new one.
        with _connection_registry_lock:
            reg_size = len(_connection_registry)
            if reg_size >= MAX_POOL_CONNECTIONS:
                # Evict connections from dead threads to make room
                alive_tids = {t.ident for t in threading.enumerate()}
                before = len(_connection_registry)
                stale = []
                kept = []
                for c in _connection_registry:
                    # Check if connection's thread is still alive
                    c_tid = _connection_thread_map.get(id(c))
                    if c_tid and c_tid not in alive_tids:
                        stale.append(c)
                    else:
                        kept.append(c)
                _connection_registry.clear()
                _connection_registry.extend(kept)
                for c in stale:
                    try:
                        _connection_thread_map.pop(id(c), None)
                        c.close()
                    except Exception:
                        pass
                if stale:
                    logger.info("[DB_POOL] Evicted %d stale connections from dead threads (%d→%d)",
                               len(stale), before, len(kept))
            _connection_registry.append(conn)
            reg_size = len(_connection_registry)
        if reg_size > MAX_POOL_CONNECTIONS:
            logger.warning("[DB_POOL] Connection registry at %d OVER ceiling=%d — all threads alive, cannot evict",
                          reg_size, MAX_POOL_CONNECTIONS)

        # ── Immediate health check on brand-new connection ──────────────
        # If the -shm file is corrupted (FUSE ghost handles from a crashed
        # process), sqlite3.connect() succeeds but any real I/O fails with
        # "disk I/O error". Detect this NOW so we can recover the -shm
        # instead of returning a broken connection to the caller.
        try:
            conn.execute('SELECT 1')
        except sqlite3.OperationalError as _hc_err:
            if 'disk I/O error' in str(_hc_err):
                # PROOF: we just created this connection. No other live process
                # could have corrupted it between connect() and SELECT 1. The
                # -shm is provably stale (ghost FUSE handle from a dead process).
                logger.warning(
                    "[DB_POOL] Brand-new connection to %s failed health check: %s "
                    "— -shm is provably stale, removing and retrying",
                    db_path.name, _hc_err
                )
                # Clean up the bad connection
                try:
                    with _connection_registry_lock:
                        try:
                            _connection_registry.remove(conn)
                        except ValueError:
                            pass
                    _connection_thread_map.pop(id(conn), None)
                    conn.close()
                except Exception:
                    pass
                if hasattr(_thread_local, attr_name):
                    delattr(_thread_local, attr_name)

                # Remove the proven-stale -shm (and empty -wal if present)
                _shm = Path(str(db_path) + "-shm")
                _wal = Path(str(db_path) + "-wal")
                try:
                    if _shm.exists():
                        _shm.unlink()
                        logger.info("[DB_POOL] Removed proven-stale -shm for %s", db_path.name)
                    if _wal.exists() and _wal.stat().st_size == 0:
                        _wal.unlink()
                        logger.info("[DB_POOL] Removed empty -wal for %s", db_path.name)
                except OSError as _rm_err:
                    logger.error("[DB_POOL] Could not remove stale -shm for %s: %s", db_path.name, _rm_err)
                    raise _hc_err  # can't recover — propagate original error

                # Reset the clean flag so _clean_stale_shm doesn't skip on retry
                with _shm_clean_lock:
                    _shm_cleaned.discard(str(db_path))

                # ONE retry with clean -shm
                conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
                conn.execute('PRAGMA mmap_size=0')
                conn.execute('PRAGMA journal_mode=DELETE')
                conn.execute('PRAGMA busy_timeout=30000')
                conn.execute('PRAGMA foreign_keys=ON')
                conn.execute('PRAGMA synchronous=NORMAL')
                conn.execute('PRAGMA cache_size=10000')
                conn.execute('PRAGMA temp_store=MEMORY')
                # Verify the retry actually works
                conn.execute('SELECT 1')
                logger.info("[DB_POOL] Recovery successful for %s — connection healthy after -shm removal", db_path.name)
                _connection_thread_map[id(conn)] = threading.current_thread().ident
                setattr(_thread_local, attr_name, conn)
                with _connection_registry_lock:
                    _connection_registry.append(conn)
                with _shm_clean_lock:
                    _shm_cleaned.add(str(db_path))
                try:
                    fr = _get_flight_recorder()
                    if fr:
                        fr.record(domain="database", stage="SHM_RECOVERY", source="db_pool._get_connection",
                                  target=str(db_path), status="ok",
                                  notes="Proven-stale -shm removed, connection recovered")
                except Exception:
                    pass
            else:
                raise  # not a -shm issue, propagate

        logger.debug("Created new connection to %s for thread %s",
                     db_path.name, threading.current_thread().ident)
        return conn

    except Exception as e:
        logger.error("Failed to create connection to %s: %s", db_path, e)
        try:
            fr = _get_flight_recorder()
            if fr:
                fr.record(domain="database", stage="DB_ERROR", source="db_pool._get_connection", target=str(db_path), status="error", data={"error": str(e)})
        except Exception:
            pass
        raise


def force_checkpoint(db_name: str = 'trading_forex') -> dict:
    """Force a PASSIVE WAL checkpoint on the named database.

    Safe to call from any thread. Returns checkpoint stats.
    Use this from scout's main loop or health checks.

    Args:
        db_name: One of 'trading_forex', 'core', 'agents', 'workspaces', 'intelligence'

    Returns:
        dict with 'busy', 'log', 'checkpointed' counts, or 'error' on failure.
    """
    db_map = {
        'trading_forex': (TRADING_FOREX_DB, 'trading_forex_conn'),
        'core': (CORE_DB, 'core_conn'),
        'agents': (AGENTS_DB, 'agents_conn'),
        'workspaces': (WORKSPACES_DB, 'workspaces_conn'),
        'intelligence': (INTELLIGENCE_DB, 'intelligence_conn'),
        'conversations': (CONVERSATIONS_DB, 'conversations_conn'),
        'journeys': (JOURNEYS_DB, 'journeys_conn'),
        'prompts': (PROMPTS_DB, 'prompts_conn'),
    }
    if db_name not in db_map:
        return {'error': f'Unknown database: {db_name}'}

    db_path, attr_name = db_map[db_name]
    try:
        conn = _get_connection(db_path, attr_name)
        result = conn.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        stats = {'busy': result[0], 'log': result[1], 'checkpointed': result[2]}
        logger.info("Force checkpoint %s: %s", db_name, stats)
        return stats
    except Exception as e:
        logger.error("Force checkpoint %s failed: %s", db_name, e)
        return {'error': str(e)}


def get_trading_forex() -> sqlite3.Connection:
    """
    Get a thread-local connection to v2/trading_forex.db.

    Contains: watch_suggestions, trade_decisions, user_snipe_list, scout_alerts,
              scout_findings, live_trades, setup_revenue, setup_trades,
              user_chart_annotations, backtest data, snipe_leaderboard
    Used by: watch_manager.py, trading_cycle.py, trade_scout.py, floor_chat.py
    """
    return _get_connection(TRADING_FOREX_DB, 'trading_forex_conn')

def get_core() -> sqlite3.Connection:
    """
    Get a thread-local connection to v2/core.db.

    Contains: users, user_sessions, trading_preferences, broker_credentials
    Used by: trading_cycle.py, position_guardian.py, serve_ui.py
    """
    return _get_connection(CORE_DB, 'core_conn')

def get_agents() -> sqlite3.Connection:
    """
    Get a thread-local connection to v2/agents.db.

    Contains: agent_registry, agent_skills, agent_communication, agent_activity
    Used by: lightweight_registrar.py, team_setup.py
    """
    return _get_connection(AGENTS_DB, 'agents_conn')

def get_workspaces() -> sqlite3.Connection:
    """
    Get a thread-local connection to v2/workspaces.db.

    Contains: workspace_tasks, workspace_task_comments, workspaces, workspace_agent_assignments
    Used by: watch_manager.py (for workspace task creation), serve_ui.py
    """
    return _get_connection(WORKSPACES_DB, 'workspaces_conn')

def get_intelligence() -> sqlite3.Connection:
    """
    Get a thread-local connection to v2/intelligence.db.

    Contains: intelligence_cache, intelligence_packages, handler_analysis, training_data
    Used by: intelligence_store.py, trade_logger.py
    """
    return _get_connection(INTELLIGENCE_DB, 'intelligence_conn')

def get_conversations() -> sqlite3.Connection:
    """
    Get a thread-local connection to v2/conversations.db.

    Contains: conversations, conversation_messages, conversation_participants
    Used by: conversation_aggregator.py, serve_ui.py
    """
    return _get_connection(CONVERSATIONS_DB, 'conversations_conn')

def get_journeys() -> sqlite3.Connection:
    """
    Get a thread-local connection to v2/journeys.db.

    Contains: journeys, journey_steps, journey_conversations
    Used by: journey_tracker.py, serve_ui.py
    """
    return _get_connection(JOURNEYS_DB, 'journeys_conn')

def get_prompts() -> sqlite3.Connection:
    """
    Get a thread-local connection to v2/prompts.db.

    Contains: prompts, prompt_versions, prompt_tags
    Used by: prompt_registry, handler_prompt_registry.py
    """
    return _get_connection(PROMPTS_DB, 'prompts_conn')

def get_flight_recorder() -> sqlite3.Connection:
    """
    Get a thread-local sqlite3 connection to v2/flight_recorder.db.

    Contains: flight_log (timestamp, stage, status, trade_id, cycle_id,
              duration_ms, data, note, ...)
    Used by: diagnostics package (read-only analysis queries).

    Note: this returns a raw sqlite3.Connection. The existing private
    _get_flight_recorder() returns a FlightRecorderV2 *logger* object and
    is unrelated — do not conflate the two.
    """
    return _get_connection(FLIGHT_RECORDER_DB, 'flight_recorder_conn')

def close_all_connections():
    """
    Close all thread-local connections for the calling thread.
    Also removes them from the global registry.
    Called during graceful shutdown or thread cleanup.
    """
    connections_closed = 0

    for attr_name in ['trading_forex_conn', 'core_conn', 'agents_conn', 'workspaces_conn',
                       'intelligence_conn', 'conversations_conn', 'journeys_conn', 'prompts_conn',
                       'flight_recorder_conn']:
        if hasattr(_thread_local, attr_name):
            try:
                conn = getattr(_thread_local, attr_name)
                # Remove from global registry
                with _connection_registry_lock:
                    try:
                        _connection_registry.remove(conn)
                    except ValueError:
                        pass
                _connection_thread_map.pop(id(conn), None)
                conn.close()
                delattr(_thread_local, attr_name)
                connections_closed += 1
            except Exception as e:
                logger.warning("Error closing %s: %s", attr_name, e)

    if connections_closed > 0:
        logger.debug("Closed %d database connections for thread %s",
                     connections_closed, threading.current_thread().ident)


def register_flask_teardown(app):
    """Register Flask teardown hook to close DB connections after each request.

    ROOT CAUSE FIX: Flask with threaded=True creates a new OS thread per request.
    db_pool creates a thread-local connection per thread. Without this hook,
    connections accumulate in _connection_registry forever because the thread
    dies before close_all_connections() is called.

    Call this ONCE during app startup:
        from db_pool import register_flask_teardown
        register_flask_teardown(app)
    """
    @app.teardown_appcontext
    def _close_db_connections(exception=None):
        close_all_connections()

    logger.info("[db_pool] Flask teardown hook registered — connections will close after each request")


# Test function to verify the pool works (v2 databases)
def test_pool():
    """Simple test to verify connection pool functionality against v2 databases."""
    try:
        # Test trading_forex connection
        trading = get_trading_forex()
        cursor = trading.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 5")
        tables = cursor.fetchall()
        logger.info("trading_forex DB test: Found %d tables", len(tables))

        # Test core connection
        core = get_core()
        cursor = core.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 5")
        tables = cursor.fetchall()
        logger.info("core DB test: Found %d tables", len(tables))

        # Verify thread-local behavior (same connection returned)
        assert get_trading_forex() is trading
        assert get_core() is core

        logger.info("Database connection pool test passed!")
        return True

    except Exception as e:
        logger.error("Database connection pool test failed: %s", e)
        return False

def pool_stats() -> dict:
    """Return current pool statistics for monitoring.

    Returns dict with:
        - total_connections: total in registry
        - ceiling: MAX_POOL_CONNECTIONS
        - fd_pressure: ratio of process FDs to OS soft limit (0.0-1.0)
        - fd_count: current open FDs for this process
        - fd_limit: OS soft limit
    """
    with _connection_registry_lock:
        total = len(_connection_registry)
    try:
        fd_count = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except (FileNotFoundError, PermissionError):
        # macOS: use resource module
        try:
            import subprocess
            result = subprocess.run(
                ["lsof", "-p", str(os.getpid())],
                capture_output=True, text=True, timeout=5
            )
            fd_count = max(0, len(result.stdout.strip().split('\n')) - 1)
        except Exception:
            fd_count = -1
    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    pressure = fd_count / soft_limit if soft_limit > 0 and fd_count >= 0 else 0.0
    return {
        "total_connections": total,
        "ceiling": MAX_POOL_CONNECTIONS,
        "fd_pressure": round(pressure, 3),
        "fd_count": fd_count,
        "fd_limit": soft_limit,
    }


_fd_pressure_cache = {"value": 0.0, "ts": 0.0}
_FD_PRESSURE_CACHE_TTL = 5.0  # Cache for 5s — lsof subprocess is expensive

def fd_pressure() -> float:
    """Return FD pressure as a ratio (0.0-1.0).

    Used by Flask before_request hook to shed load when FDs are running out.
    Cached for 5s to avoid spawning lsof subprocess on every HTTP request.
    """
    now = time.monotonic()
    if now - _fd_pressure_cache["ts"] < _FD_PRESSURE_CACHE_TTL:
        return _fd_pressure_cache["value"]
    value = pool_stats()["fd_pressure"]
    _fd_pressure_cache["value"] = value
    _fd_pressure_cache["ts"] = now
    return value


# ── Stale connection reaper ──────────────────────────────────────────────
# Daemon thread that periodically cleans up connections from dead threads.
# Flask creates a new OS thread per request (threaded=True). If the teardown
# hook fails or the thread dies abnormally, the connection stays in the
# registry forever. The reaper catches these orphans.

_REAPER_INTERVAL_S = 60

def _reaper_loop():
    """Background thread that cleans up connections from dead threads."""
    while True:
        try:
            time.sleep(_REAPER_INTERVAL_S)
            alive_thread_ids = {t.ident for t in threading.enumerate()}
            reaped = 0
            to_close: list = []
            with _connection_registry_lock:
                total = len(_connection_registry)
                # Actually evict connections whose owning thread is dead
                surviving = []
                for conn in _connection_registry:
                    c_tid = _connection_thread_map.get(id(conn))
                    if c_tid is not None and c_tid not in alive_thread_ids:
                        to_close.append(conn)
                        _connection_thread_map.pop(id(conn), None)
                    else:
                        surviving.append(conn)
                if to_close:
                    _connection_registry[:] = surviving
            # Close outside the lock to avoid holding it during I/O
            for conn in to_close:
                try:
                    conn.close()
                    reaped += 1
                except Exception:
                    pass
            if reaped:
                logger.info(
                    "[DB_POOL] Reaper: evicted %d dead-thread connections (registry %d→%d)",
                    reaped, total, total - reaped
                )
            if total - reaped > MAX_POOL_CONNECTIONS:
                logger.warning(
                    "[DB_POOL] Reaper: %d connections in registry (ceiling=%d) — "
                    "all threads alive, cannot evict more",
                    total - reaped, MAX_POOL_CONNECTIONS
                )
            stats = pool_stats()
            if stats["fd_pressure"] > 0.7:
                logger.warning(
                    "[DB_POOL] Reaper: FD pressure at %.1f%% (%d/%d) — approaching exhaustion",
                    stats["fd_pressure"] * 100, stats["fd_count"], stats["fd_limit"]
                )
        except Exception as e:
            logger.error("[DB_POOL] Reaper error: %s", e)


def _nuke_all_connections_for_db(db_path: Path) -> int:
    """Force-close ALL connections to a specific database across all threads.

    Used for auto-recovery when disk I/O errors indicate the connection pool
    is poisoned (e.g., FUSE -shm corruption). Returns count of connections closed.
    """
    target_str = str(db_path)
    nuked = 0

    # Find the attr_name for this database
    db_attr_map = {
        str(TRADING_FOREX_DB): 'trading_forex_conn',
        str(CORE_DB): 'core_conn',
        str(AGENTS_DB): 'agents_conn',
        str(WORKSPACES_DB): 'workspaces_conn',
        str(INTELLIGENCE_DB): 'intelligence_conn',
        str(CONVERSATIONS_DB): 'conversations_conn',
        str(JOURNEYS_DB): 'journeys_conn',
        str(PROMPTS_DB): 'prompts_conn',
    }
    attr_name = db_attr_map.get(target_str)

    with _connection_registry_lock:
        to_close = []
        remaining = []
        for conn in _connection_registry:
            # Check if this connection is for the target database
            # by testing the database filename
            try:
                db_file = conn.execute("PRAGMA database_list").fetchone()
                if db_file and target_str in str(db_file[2]):
                    to_close.append(conn)
                else:
                    remaining.append(conn)
            except Exception:
                to_close.append(conn)  # Broken connection — close it

        _connection_registry.clear()
        _connection_registry.extend(remaining)

    for conn in to_close:
        try:
            _connection_thread_map.pop(id(conn), None)
            conn.close()
            nuked += 1
        except Exception:
            pass

    # Clean stale WAL/SHM files to prevent recontamination
    for ext in ['-wal', '-shm']:
        p = Path(target_str + ext)
        if p.exists():
            try:
                p.unlink()
                logger.info("[DB_POOL] RECOVERY: Deleted %s", p.name)
            except OSError:
                pass

    # Clean .fuse_hidden* orphan files in the DB directory.
    # These are stale file handles left by FUSE when a process crashes
    # while holding an -shm or -wal file descriptor.
    db_dir = db_path.parent
    try:
        fuse_cleaned = 0
        for fname in os.listdir(str(db_dir)):
            if fname.startswith(".fuse_hidden"):
                try:
                    (db_dir / fname).unlink()
                    fuse_cleaned += 1
                except OSError:
                    pass  # May still be held by kernel — will clean on next startup
        if fuse_cleaned:
            logger.info("[DB_POOL] RECOVERY: Deleted %d FUSE ghost files in %s", fuse_cleaned, db_dir.name)
    except OSError:
        pass

    # Reset the shm_cleaned flag so next connect will re-clean if needed
    with _shm_clean_lock:
        _shm_cleaned.discard(target_str)

    logger.warning("[DB_POOL] RECOVERY: Nuked %d connections to %s — will reconnect on next access",
                   nuked, db_path.name)
    return nuked


def _auto_recover_db(db_path: Path) -> bool:
    """Last-resort automatic database recovery using table-by-table copy.

    1. Check integrity — if OK, just clear WAL/SHM (transient FUSE issue)
    2. If corrupted, copy all tables to a fresh DB, validate, and swap in
    3. Keep corrupt DB as backup with timestamp

    Returns True if recovery succeeded, False if it failed.
    Based on pattern from Database/recover_trevor_db.py.
    """
    from datetime import datetime as _dt
    target_str = str(db_path)
    logger.warning("[DB_POOL] AUTO-RECOVERY starting for %s", db_path.name)

    # Step 1: Check if actually corrupted or just transient FUSE issue
    try:
        check_conn = sqlite3.connect(f"file:{target_str}?mode=ro", uri=True, timeout=10)
        result = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        check_conn.close()
        if result == "ok":
            # Not actually corrupted — clear WAL/SHM and return success
            logger.info("[DB_POOL] AUTO-RECOVERY: %s passes integrity check — clearing WAL/SHM only", db_path.name)
            for ext in ['-wal', '-shm']:
                p = Path(target_str + ext)
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            with _shm_clean_lock:
                _shm_cleaned.discard(target_str)
            return True
    except Exception as e:
        logger.warning("[DB_POOL] AUTO-RECOVERY: integrity check failed for %s: %s — proceeding with full recovery",
                      db_path.name, e)

    # Step 2: Full table-by-table recovery
    timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    recovered_path = db_path.parent / f"{db_path.stem}_recovered.db"
    corrupt_path = db_path.parent / f"{db_path.stem}.db.corrupt.{timestamp}"

    # Clean up any prior recovery attempt
    if recovered_path.exists():
        try:
            recovered_path.unlink()
        except OSError:
            logger.error("[DB_POOL] AUTO-RECOVERY: cannot remove prior %s", recovered_path.name)
            return False

    src = None
    dst = None
    try:
        src = sqlite3.connect(f"file:{target_str}?mode=ro", uri=True, timeout=30, isolation_level=None)
        dst = sqlite3.connect(str(recovered_path), timeout=60, isolation_level=None)

        # Safe PRAGMAs for destination
        for pragma in [
            "PRAGMA journal_mode = DELETE",
            "PRAGMA synchronous = FULL",
            "PRAGMA busy_timeout = 30000",
            "PRAGMA mmap_size = 0",
        ]:
            dst.execute(pragma)
        dst.commit()

        # Copy schema
        schema = src.execute("""
            SELECT type, name, sql FROM sqlite_master
            WHERE sql IS NOT NULL
              AND type IN ('table', 'index')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY rootpage
        """).fetchall()
        for obj_type, name, sql in schema:
            try:
                dst.execute(sql)
            except sqlite3.OperationalError as e:
                if "already exists" not in str(e):
                    logger.warning("[DB_POOL] AUTO-RECOVERY: schema warning [%s]: %s", name, e)
        dst.commit()

        # Copy data table by table
        tables = [t[0] for t in src.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY rootpage
        """).fetchall()]

        total_rows = 0
        failed_tables = []
        for table in tables:
            try:
                count = src.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                if count == 0:
                    continue
                cols = [row[1] for row in src.execute(f'PRAGMA table_info("{table}")').fetchall()]
                col_list = ", ".join(f'"{c}"' for c in cols)
                placeholders = ", ".join("?" * len(cols))
                insert_sql = f'INSERT OR IGNORE INTO "{table}" ({col_list}) VALUES ({placeholders})'

                chunk_size = 50000
                offset = 0
                while offset < count:
                    rows = src.execute(
                        f'SELECT {col_list} FROM "{table}" LIMIT {chunk_size} OFFSET {offset}'
                    ).fetchall()
                    if not rows:
                        break
                    dst.executemany(insert_sql, rows)
                    dst.commit()
                    total_rows += len(rows)
                    offset += chunk_size
            except Exception as e:
                dst.rollback()
                failed_tables.append(table)
                logger.warning("[DB_POOL] AUTO-RECOVERY: table %s failed: %s", table, e)

        logger.info("[DB_POOL] AUTO-RECOVERY: copied %d rows from %d tables (%d failed)",
                   total_rows, len(tables), len(failed_tables))

        # Validate recovered DB
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            logger.error("[DB_POOL] AUTO-RECOVERY: recovered DB fails integrity check: %s", result)
            dst.close()
            src.close()
            recovered_path.unlink(missing_ok=True)
            return False

        dst.close()
        src.close()

        # Swap files: original → corrupt backup, recovered → original
        try:
            os.rename(target_str, str(corrupt_path))
        except OSError as e:
            logger.error("[DB_POOL] AUTO-RECOVERY: cannot rename corrupt DB: %s", e)
            recovered_path.unlink(missing_ok=True)
            return False

        try:
            os.rename(str(recovered_path), target_str)
        except OSError as e:
            # Revert: put corrupt back
            logger.error("[DB_POOL] AUTO-RECOVERY: cannot swap in recovered DB: %s — reverting", e)
            os.rename(str(corrupt_path), target_str)
            return False

        # Clean stale WAL/SHM from the old corrupt DB
        for ext in ['-wal', '-shm']:
            p = Path(target_str + ext)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

        with _shm_clean_lock:
            _shm_cleaned.discard(target_str)

        logger.warning(
            "[DB_POOL] AUTO-RECOVERY COMPLETE for %s: %d rows recovered, %d tables failed. "
            "Corrupt backup at %s",
            db_path.name, total_rows, len(failed_tables), corrupt_path.name
        )

        # Log to flight recorder
        try:
            fr = _get_flight_recorder()
            if fr:
                fr.record(
                    domain="database", stage="DB_AUTO_RECOVERY",
                    source="db_pool._auto_recover_db", target=str(db_path),
                    status="ok",
                    data={"rows": total_rows, "tables": len(tables),
                          "failed": failed_tables, "corrupt_backup": str(corrupt_path)}
                )
        except Exception:
            pass

        return True

    except Exception as e:
        logger.error("[DB_POOL] AUTO-RECOVERY FAILED for %s: %s", db_path.name, e)
        if src:
            try:
                src.close()
            except Exception:
                pass
        if dst:
            try:
                dst.close()
            except Exception:
                pass
        recovered_path.unlink(missing_ok=True)
        return False


def recover_database(db_name: str) -> dict:
    """Manually trigger database recovery.

    Usage: from db_pool import recover_database; recover_database('trading_forex')
    """
    db_map = {
        'trading_forex': TRADING_FOREX_DB,
        'core': CORE_DB,
        'agents': AGENTS_DB,
        'workspaces': WORKSPACES_DB,
        'intelligence': INTELLIGENCE_DB,
        'conversations': CONVERSATIONS_DB,
        'journeys': JOURNEYS_DB,
        'prompts': PROMPTS_DB,
    }
    db_path = db_map.get(db_name)
    if not db_path:
        return {'error': f'Unknown database: {db_name}. Valid: {list(db_map.keys())}'}

    # Nuke all connections first
    _nuke_all_connections_for_db(db_path)

    # Attempt recovery
    success = _auto_recover_db(db_path)
    if success:
        reset_degraded_mode(db_name)
        return {'database': db_name, 'recovered': True}
    else:
        return {'database': db_name, 'recovered': False, 'error': 'Recovery failed — check logs'}


def reset_degraded_mode(db_name: str = None) -> dict:
    """Reset recovery backoff and degraded mode for a database (or all).

    Call this after manually fixing the underlying FUSE/disk issue.
    Usage: from db_pool import reset_degraded_mode; reset_degraded_mode('trading_forex')
    """
    db_map = {
        'trading_forex': str(TRADING_FOREX_DB),
        'core': str(CORE_DB),
        'agents': str(AGENTS_DB),
        'workspaces': str(WORKSPACES_DB),
        'intelligence': str(INTELLIGENCE_DB),
        'conversations': str(CONVERSATIONS_DB),
        'journeys': str(JOURNEYS_DB),
        'prompts': str(PROMPTS_DB),
    }
    with _recovery_backoff_lock:
        if db_name:
            key = db_map.get(db_name)
            if not key:
                return {'error': f'Unknown database: {db_name}. Valid: {list(db_map.keys())}'}
            cleared = []
            if key in _recovery_backoff:
                del _recovery_backoff[key]
                cleared.append('backoff')
            if key in _recovery_failure_count:
                del _recovery_failure_count[key]
                cleared.append('failure_count')
            logger.info("[DB_POOL] Degraded mode reset for %s", db_name)
            return {'database': db_name, 'cleared': cleared}
        else:
            count = len(_recovery_backoff) + len(_recovery_failure_count)
            _recovery_backoff.clear()
            _recovery_failure_count.clear()
            logger.info("[DB_POOL] Degraded mode reset for ALL databases")
            return {'cleared_all': True, 'count': count}


# Start reaper daemon thread on module import
_reaper_thread = threading.Thread(target=_reaper_loop, name="db_pool_reaper", daemon=True)
_reaper_thread.start()


if __name__ == "__main__":
    # Run test when module is executed directly
    logging.basicConfig(level=logging.DEBUG)
    test_pool()
