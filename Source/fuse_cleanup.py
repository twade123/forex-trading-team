#!/usr/bin/env python3
"""
FUSE Stale Handle Cleanup — runs ONCE at process startup, BEFORE any DB connections.

Problem:
  These databases run on a FUSE mount (VirtIO on macOS). SQLite WAL mode uses a
  memory-mapped -shm file. When a process crashes without closing connections,
  FUSE can't release the -shm handle and creates .fuse_hidden* ghost files
  (all 32KB — the exact size of an -shm file). On restart, new -shm files may
  collide with stale mmap mappings, causing "disk I/O error" and server crashes.

  db_pool.py already cleans Database/v2/ databases on first access, but 150+
  direct sqlite3.connect() calls in Source/ bypass the pool entirely. This
  module covers ALL databases in Source/ and its subdirectories.

Usage:
  Call cleanup_fuse_artifacts() at the very start of your process, before any
  sqlite3.connect() call:

    from fuse_cleanup import cleanup_fuse_artifacts
    cleanup_fuse_artifacts()   # safe, fast, idempotent
"""

import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_SOURCE_DIR = Path(__file__).parent.resolve()
_CLEANUP_DONE = False


def cleanup_fuse_artifacts(root: Path = None) -> dict:
    """Purge stale .fuse_hidden files and orphaned -shm files under *root*.

    For each -shm file found, we try an exclusive POSIX lock on the parent DB.
    If we get the lock, no other process holds the DB → -shm is stale → delete.
    If we can't lock, the DB is in use → -shm is valid → leave it.

    .fuse_hidden files are always stale (the process that created them is gone)
    so they're deleted unconditionally.

    Returns:
        dict with counts: {"fuse_hidden_removed": N, "shm_removed": N, "errors": N}
    """
    global _CLEANUP_DONE
    if _CLEANUP_DONE:
        return {"fuse_hidden_removed": 0, "shm_removed": 0, "errors": 0, "skipped": True}
    _CLEANUP_DONE = True

    if root is None:
        root = _SOURCE_DIR

    stats = {"fuse_hidden_removed": 0, "shm_removed": 0, "errors": 0}

    # ── 1. Remove .fuse_hidden ghost files ─────────────────────────────────
    # These are leaked file handles from crashed processes. They're always stale.
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for fname in filenames:
            if fname.startswith(".fuse_hidden"):
                fpath = os.path.join(dirpath, fname)
                try:
                    os.unlink(fpath)
                    stats["fuse_hidden_removed"] += 1
                except OSError:
                    stats["errors"] += 1

    # ── 2. Clean stale -shm files for all .db files ───────────────────────
    # Walk the tree once to find all databases, then check each -shm.
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for fname in filenames:
            if fname.endswith(".db") and not fname.startswith("."):
                db_path = Path(dirpath) / fname
                _clean_shm_if_stale(db_path, stats)

    # Also clean the parent Database/ directories (Database/v2/, etc.)
    # in case db_pool hasn't run yet
    db_root = root.parent / "Database"
    if db_root.exists():
        for dirpath, _dirnames, filenames in os.walk(str(db_root)):
            for fname in filenames:
                if fname.startswith(".fuse_hidden"):
                    fpath = os.path.join(dirpath, fname)
                    try:
                        os.unlink(fpath)
                        stats["fuse_hidden_removed"] += 1
                    except OSError:
                        stats["errors"] += 1
            for fname in filenames:
                if fname.endswith(".db") and not fname.startswith("."):
                    db_path = Path(dirpath) / fname
                    _clean_shm_if_stale(db_path, stats)

    if stats["fuse_hidden_removed"] or stats["shm_removed"]:
        logger.info(
            "[FUSE CLEANUP] Removed %d .fuse_hidden files, %d stale -shm files (%d errors)",
            stats["fuse_hidden_removed"], stats["shm_removed"], stats["errors"]
        )
    else:
        logger.debug("[FUSE CLEANUP] No stale artifacts found")

    return stats


def _clean_shm_if_stale(db_path: Path, stats: dict) -> None:
    """Delete -shm file for *db_path* if no other process holds the DB."""
    shm_path = Path(str(db_path) + "-shm")
    if not shm_path.exists():
        return

    try:
        fd = os.open(str(db_path), os.O_RDWR)
    except OSError:
        return  # DB file not writable or doesn't exist

    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            # Another process holds the DB — -shm is valid
            return

        # Got exclusive lock — no other process has this DB open.
        try:
            shm_path.unlink()
            stats["shm_removed"] += 1
            logger.debug("[FUSE CLEANUP] Removed stale -shm: %s", shm_path.name)
        except OSError:
            stats["errors"] += 1

        # Also clean empty -wal (crash leftover)
        wal_path = Path(str(db_path) + "-wal")
        if wal_path.exists() and wal_path.stat().st_size == 0:
            try:
                wal_path.unlink()
            except OSError:
                pass

        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
