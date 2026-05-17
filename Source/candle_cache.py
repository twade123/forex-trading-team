"""Process-wide M15/H1/H4 candle cache for OANDA data.

Why this exists:
  Watch evaluator, kronos hunter, and trading_cycle each fetch candles via
  OandaClient. With many active watches/snipes, the per-cycle burst (~174
  calls / 5 min) was overwhelming OANDA's 4-second read timeout, triggering
  the OandaClient circuit breaker and silently returning empty results — which
  then blocked every snipe at the no_market_picture gate. See incident
  2026-05-01.

Design:
  - Process-wide cache. Each user runs their own workspace = own process =
    own cache. No cross-user state.
  - Key:   (pair, granularity, count)   — candle data is identical
                                          regardless of API key.
  - Value: (fetched_unix_ts, candles_list)
  - TTL:   5 min (matches watch evaluator cycle)
  - Thread-safe (RLock).
  - Caches ONLY successful, non-degenerate responses (>= 100 candles).
  - On fetch failure: returns stale entry if available
    (allow_stale_on_error=True), else re-raises.
  - Memory bound: ~13 pairs × 5 TFs × ~50KB = ~3MB ceiling. Hard cap 500
    entries with full clear() if exceeded (defensive, never expected to hit).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

DEFAULT_TTL_SEC = 300
MIN_VALID_CANDLES = 100
MAX_CACHE_ENTRIES = 500

_CACHE: Dict[Tuple[str, str, int], Tuple[float, list]] = {}
_LOCK = threading.RLock()
_STATS = {"hits": 0, "misses": 0, "errors": 0, "stale_serves": 0, "panics": 0}


def get_cached_candles(
    fetch_fn: Callable[[], list],
    pair: str,
    granularity: str,
    count: int = 250,
    ttl_sec: int = DEFAULT_TTL_SEC,
    allow_stale_on_error: bool = True,
) -> List[dict]:
    """Return cached candles if fresh; otherwise call fetch_fn() and cache.

    Args:
        fetch_fn: Zero-arg callable returning a candle list. Lets callers wrap
            any source (OandaClient, swarm tool, mock).
        pair: Instrument symbol, e.g. "EUR_USD".
        granularity: OANDA TF, e.g. "M15", "H1", "H4".
        count: Number of candles requested (must match across calls for the
            same key — different counts = different cache entries).
        ttl_sec: Freshness window. Default 5 min.
        allow_stale_on_error: On fetch_fn exception, return stale cache entry
            if one exists, else re-raise.

    Returns:
        List of candle dicts. May be the cached copy (no defensive copy — do
        NOT mutate; treat as read-only).
    """
    key = (pair, granularity, count)
    now = time.time()

    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and (now - cached[0]) < ttl_sec:
            _STATS["hits"] += 1
            return cached[1]

    # Cache miss / expired — fetch fresh.
    try:
        fresh = fetch_fn()
    except Exception:
        with _LOCK:
            _STATS["errors"] += 1
            cached = _CACHE.get(key)
            if cached is not None and allow_stale_on_error:
                _STATS["stale_serves"] += 1
                return cached[1]
        raise

    candles = fresh if isinstance(fresh, list) else (fresh or {}).get("candles", [])

    if isinstance(candles, list) and len(candles) >= MIN_VALID_CANDLES:
        with _LOCK:
            _CACHE[key] = (now, candles)
            _STATS["misses"] += 1
            if len(_CACHE) > MAX_CACHE_ENTRIES:
                _CACHE.clear()
                _STATS["panics"] += 1
        return candles

    with _LOCK:
        _STATS["misses"] += 1
    return candles or []


def get_stats() -> dict:
    """Return cache stats snapshot."""
    with _LOCK:
        total = _STATS["hits"] + _STATS["misses"]
        hit_rate = (_STATS["hits"] / total * 100) if total else 0.0
        return {
            **_STATS,
            "size": len(_CACHE),
            "hit_rate_pct": round(hit_rate, 1),
        }


def clear_cache() -> None:
    """Test/debug only — drop all cached entries and reset stats."""
    with _LOCK:
        _CACHE.clear()
        for k in _STATS:
            _STATS[k] = 0
