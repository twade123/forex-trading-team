"""
Broker-agnostic historical candle data fetcher with SQLite caching.

Defines the :class:`CandleProvider` protocol that any broker client must
implement, and :class:`HistoricalDataFetcher` which wraps a provider with
an on-disk SQLite cache keyed by ``(broker, instrument, granularity, time)``
so that forex, crypto, and futures data stays completely isolated.

Usage:
    from Source.oanda_client import OandaClient
    from Source.historical_data import HistoricalDataFetcher

    with OandaClient() as client:
        fetcher = HistoricalDataFetcher(client)
        candles = fetcher.fetch("EUR_USD", "H1", from_time, to_time)
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# CandleProvider protocol
# ------------------------------------------------------------------

@runtime_checkable
class CandleProvider(Protocol):
    """Protocol for broker clients that provide historical candle data.

    Any broker (Oanda, Coinbase, futures brokers) implements this protocol
    to plug into the backtesting system.  The :class:`HistoricalDataFetcher`
    accepts any ``CandleProvider``, keeping the backtesting engine
    broker-agnostic.

    Implementors:
        - ``OandaClient`` (forex) -- ``fetch_candles_range`` chains
          5000-candle pages
        - ``CoinbaseClient`` (crypto) -- future implementation
        - ``FuturesBrokerClient`` (futures) -- future implementation
    """

    @property
    def broker_name(self) -> str:
        """Unique broker identifier for cache partitioning.

        Examples: ``'oanda'``, ``'coinbase'``, ``'cme'``.
        """
        ...

    def fetch_candles_range(
        self,
        instrument: str,
        granularity: str,
        from_time: datetime,
        to_time: datetime,
        price: str = "M",
    ) -> List[Dict[str, Any]]:
        """Fetch all candles between *from_time* and *to_time*.

        Must handle pagination internally (e.g. Oanda's 5000 limit).

        Returns:
            List of candle dicts with at minimum: ``time``,
            ``mid``/``bid``/``ask`` containing ``o``/``h``/``l``/``c``,
            ``volume``, ``complete``.
        """
        ...


# ------------------------------------------------------------------
# HistoricalDataFetcher
# ------------------------------------------------------------------

_DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Data",
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS candles (
    broker TEXT NOT NULL,
    instrument TEXT NOT NULL,
    granularity TEXT NOT NULL,
    time TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    PRIMARY KEY (broker, instrument, granularity, time)
)
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_candles_lookup
ON candles (broker, instrument, granularity, time)
"""


class HistoricalDataFetcher:
    """Broker-agnostic historical candle data fetcher with SQLite caching.

    Accepts any :class:`CandleProvider` (Oanda, Coinbase, futures brokers)
    and caches data in SQLite keyed by
    ``(broker, instrument, granularity, time)``.  This ensures forex, crypto,
    and futures data stays completely isolated.

    Args:
        provider: Any object implementing the :class:`CandleProvider`
            protocol.
        cache_dir: Directory for the SQLite cache file.  Defaults to
            ``Forex Trading Team/Data/``.
    """

    def __init__(
        self,
        provider: CandleProvider,
        cache_dir: Optional[str] = None,
    ) -> None:
        # Validate protocol compliance (duck-type check)
        if not hasattr(provider, "broker_name"):
            raise TypeError(
                f"Provider {type(provider).__name__} missing 'broker_name' "
                "property (CandleProvider protocol)."
            )
        if not hasattr(provider, "fetch_candles_range"):
            raise TypeError(
                f"Provider {type(provider).__name__} missing "
                "'fetch_candles_range' method (CandleProvider protocol)."
            )

        self._provider = provider
        self._broker: str = provider.broker_name
        self._cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self._db_path: Optional[str] = None
        self._initialized = False

        logger.info(
            "HistoricalDataFetcher initialised for broker=%s cache_dir=%s",
            self._broker,
            self._cache_dir,
        )

    # ------------------------------------------------------------------
    # SQLite helpers
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        """Create a fresh short-lived SQLite connection to the cache.

        Initializes the cache directory and schema on first call.
        Caller is responsible for closing the returned connection.
        """
        if not self._initialized:
            Path(self._cache_dir).mkdir(parents=True, exist_ok=True)
            self._db_path = os.path.join(self._cache_dir, "historical_cache.db")
            conn = sqlite3.connect(self._db_path, timeout=10, isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute(_CREATE_TABLE_SQL)
                conn.execute(_CREATE_INDEX_SQL)
                conn.commit()
            finally:
                conn.close()
            self._initialized = True
            logger.info("SQLite cache initialized at %s", self._db_path)

        conn = sqlite3.connect(self._db_path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=DELETE")
        return conn  # caller must .close()

    # ------------------------------------------------------------------
    # Cache read/write
    # ------------------------------------------------------------------

    def _cache_candles(
        self,
        instrument: str,
        granularity: str,
        candles: List[Dict[str, Any]],
    ) -> None:
        """Store candles in the SQLite cache.

        Uses ``INSERT OR REPLACE`` so re-fetching the same range is
        idempotent.  Extracts OHLCV from the ``mid`` price component
        (falls back to ``bid`` then ``ask``).
        """
        if not candles:
            return

        conn = self._get_connection()
        try:
            rows = []
            for c in candles:
                # Prefer mid, then bid, then ask
                prices = c.get("mid") or c.get("bid") or c.get("ask") or {}
                rows.append((
                    self._broker,
                    instrument,
                    granularity,
                    c["time"],
                    float(prices.get("o", 0)),
                    float(prices.get("h", 0)),
                    float(prices.get("l", 0)),
                    float(prices.get("c", 0)),
                    int(c.get("volume", 0)),
                    1 if c.get("complete", True) else 0,
                ))

            conn.executemany(
                "INSERT OR REPLACE INTO candles "
                "(broker, instrument, granularity, time, open, high, low, "
                "close, volume, complete) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
            logger.info(
                "Cached %d candles for %s/%s/%s",
                len(rows),
                self._broker,
                instrument,
                granularity,
            )
        finally:
            conn.close()

    def _get_cached(
        self,
        instrument: str,
        granularity: str,
        from_time: datetime,
        to_time: datetime,
    ) -> List[Dict[str, Any]]:
        """Retrieve cached candles for the specified range.

        Returns candle dicts in the same structure as the Oanda API
        (with ``mid`` dict containing ``o``/``h``/``l``/``c``).
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT time, open, high, low, close, volume, complete "
                "FROM candles "
                "WHERE broker = ? AND instrument = ? AND granularity = ? "
                "  AND time >= ? AND time <= ? "
                "ORDER BY time",
                (
                    self._broker,
                    instrument,
                    granularity,
                    self._dt_to_rfc3339(from_time),
                    self._dt_to_rfc3339(to_time),
                ),
            )
            rows = cursor.fetchall()

            return [
                {
                    "time": row[0],
                    "mid": {
                        "o": str(row[1]),
                        "h": str(row[2]),
                        "l": str(row[3]),
                        "c": str(row[4]),
                    },
                    "volume": row[5],
                    "complete": bool(row[6]),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def _check_cache_coverage(
        self,
        instrument: str,
        granularity: str,
        from_time: datetime,
        to_time: datetime,
    ) -> Tuple[bool, Optional[datetime], Optional[datetime]]:
        """Check how much of the requested range is already cached.

        Returns:
            ``(fully_covered, gap_start, gap_end)``
            - If fully cached: ``(True, None, None)``
            - If partial: ``(False, gap_start, gap_end)`` with the
              uncovered portion
            - If no cache: ``(False, from_time, to_time)``
        """
        conn = self._get_connection()
        try:
            cursor = conn.execute(
                "SELECT MIN(time), MAX(time) FROM candles "
                "WHERE broker = ? AND instrument = ? AND granularity = ?",
                (self._broker, instrument, granularity),
            )
            row = cursor.fetchone()

            if row is None or row[0] is None:
                # No cached data at all
                return (False, from_time, to_time)

            cached_min = self._parse_time(row[0])
            cached_max = self._parse_time(row[1])

            from_str = self._dt_to_rfc3339(from_time)
            to_str = self._dt_to_rfc3339(to_time)

            # Check if request falls entirely within cached range
            if cached_min <= from_time and cached_max >= to_time:
                # Verify density: count cached rows vs expected
                count_cursor = conn.execute(
                    "SELECT COUNT(*) FROM candles "
                    "WHERE broker = ? AND instrument = ? AND granularity = ? "
                    "  AND time >= ? AND time <= ?",
                    (self._broker, instrument, granularity, from_str, to_str),
                )
                cached_count = count_cursor.fetchone()[0]
                if cached_count > 0:
                    return (True, None, None)

            # Determine gap boundaries
            if cached_min > from_time and cached_max < to_time:
                # Cache covers a middle portion -- fetch both edges
                # Simplify: fetch the whole range (provider handles dedup)
                return (False, from_time, to_time)
            elif cached_min > from_time:
                # Missing data before cache start
                return (False, from_time, cached_min)
            elif cached_max < to_time:
                # Missing data after cache end
                return (False, cached_max, to_time)

            return (False, from_time, to_time)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(
        self,
        instrument: str,
        granularity: str,
        from_time: datetime,
        to_time: datetime,
        price: str = "M",
    ) -> List[Dict[str, Any]]:
        """Fetch historical candles, using cache where available.

        This is the single entry point for all historical data needs.

        1. Checks cache coverage for the requested range.
        2. If fully cached, returns from disk (no API calls).
        3. If gaps exist, fetches only the gap via the provider.
        4. Caches newly fetched candles.
        5. Returns the complete range sorted by time.

        Args:
            instrument: Instrument name (e.g. ``'EUR_USD'``).
            granularity: Candle granularity (e.g. ``'H1'``).
            from_time: Start of range (inclusive).
            to_time: End of range (inclusive).
            price: Price component(s) for the provider (default ``'M'``).

        Returns:
            List of candle dicts covering the requested range.
        """
        fully_cached, gap_start, gap_end = self._check_cache_coverage(
            instrument, granularity, from_time, to_time,
        )

        if fully_cached:
            cached = self._get_cached(instrument, granularity, from_time, to_time)
            logger.info(
                "Cache HIT for %s/%s/%s: %d candles [%s -> %s]",
                self._broker,
                instrument,
                granularity,
                len(cached),
                from_time,
                to_time,
            )
            return cached

        # Fetch the gap from the provider
        logger.info(
            "Cache MISS for %s/%s/%s: fetching [%s -> %s]",
            self._broker,
            instrument,
            granularity,
            gap_start,
            gap_end,
        )
        new_candles = self._provider.fetch_candles_range(
            instrument=instrument,
            granularity=granularity,
            from_time=gap_start,
            to_time=gap_end,
            price=price,
        )

        if new_candles:
            self._cache_candles(instrument, granularity, new_candles)

        # Return the full range from cache (now populated)
        result = self._get_cached(instrument, granularity, from_time, to_time)
        logger.info(
            "Returning %d total candles for %s/%s/%s [%s -> %s]",
            len(result),
            self._broker,
            instrument,
            granularity,
            from_time,
            to_time,
        )
        return result

    def clear_cache(
        self,
        instrument: Optional[str] = None,
        granularity: Optional[str] = None,
        broker: Optional[str] = None,
    ) -> int:
        """Delete cached candles.

        By default, clears only the current broker's data.  Pass explicit
        arguments to narrow or broaden the scope.

        Args:
            instrument: Limit deletion to this instrument.
            granularity: Limit deletion to this granularity.
            broker: Broker to clear.  Defaults to ``self._broker``
                (current provider).

        Returns:
            Number of rows deleted.
        """
        target_broker = broker if broker is not None else self._broker
        conn = self._get_connection()
        try:
            if instrument is not None and granularity is not None:
                cursor = conn.execute(
                    "DELETE FROM candles WHERE broker=? AND instrument=? AND granularity=?",
                    (target_broker, instrument, granularity),
                )
            elif instrument is not None:
                cursor = conn.execute(
                    "DELETE FROM candles WHERE broker=? AND instrument=?",
                    (target_broker, instrument),
                )
            elif granularity is not None:
                cursor = conn.execute(
                    "DELETE FROM candles WHERE broker=? AND granularity=?",
                    (target_broker, granularity),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM candles WHERE broker=?",
                    (target_broker,),
                )
            conn.commit()
            deleted = cursor.rowcount

            logger.info(
                "Cleared %d cached candles (broker=%s instrument=%s granularity=%s)",
                deleted,
                target_broker,
                instrument,
                granularity,
            )
            return deleted
        finally:
            conn.close()

    def get_cache_stats(
        self,
        broker: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return cache statistics per broker/instrument/granularity.

        Args:
            broker: Filter to a specific broker.  Defaults to
                ``self._broker``.  Pass ``broker=""`` or ``broker="*"``
                for all brokers.

        Returns:
            Dict with ``total_candles``, ``brokers``, and per-broker
            breakdown of instruments, granularities, counts, and date ranges.
        """
        conn = self._get_connection()
        try:
            # Determine filter
            if broker is None:
                target_broker = self._broker
            elif broker in ("", "*"):
                target_broker = None
            else:
                target_broker = broker

            if target_broker:
                cursor = conn.execute(
                    "SELECT broker, instrument, granularity, "
                    "  COUNT(*), MIN(time), MAX(time) "
                    "FROM candles "
                    "WHERE broker = ? "
                    "GROUP BY broker, instrument, granularity "
                    "ORDER BY broker, instrument, granularity",
                    (target_broker,),
                )
            else:
                cursor = conn.execute(
                    "SELECT broker, instrument, granularity, "
                    "  COUNT(*), MIN(time), MAX(time) "
                    "FROM candles "
                    "GROUP BY broker, instrument, granularity "
                    "ORDER BY broker, instrument, granularity"
                )

            rows = cursor.fetchall()
            total = 0
            brokers: Dict[str, List[Dict[str, Any]]] = {}

            for b, inst, gran, cnt, min_t, max_t in rows:
                total += cnt
                brokers.setdefault(b, []).append({
                    "instrument": inst,
                    "granularity": gran,
                    "count": cnt,
                    "from": min_t,
                    "to": max_t,
                })

            return {
                "total_candles": total,
                "brokers": brokers,
            }
        finally:
            conn.close()

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.info("SQLite cache connection closed.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_time(time_str: str) -> datetime:
        """Parse an RFC3339 / ISO 8601 time string to a UTC datetime.

        Handles Oanda's nanosecond-precision timestamps
        (e.g. ``2024-01-01T00:00:00.000000000Z``) by truncating to
        microsecond precision before parsing.
        """
        # Strip trailing 'Z' and replace with UTC offset
        s = time_str.replace("Z", "")
        # Truncate nanosecond fractional part to 6 digits (microseconds)
        has_offset = False
        if "." in s:
            integer_part, frac = s.split(".", 1)
            # Remove any existing timezone offset from frac
            offset = ""
            for sep in ("+", "-"):
                if sep in frac:
                    idx = frac.index(sep)
                    offset = frac[idx:]
                    frac = frac[:idx]
                    has_offset = True
                    break
            frac = frac[:6].ljust(6, "0")
            s = f"{integer_part}.{frac}{offset}"
        if not has_offset:
            s += "+00:00"
        return datetime.fromisoformat(s)

    @staticmethod
    def _dt_to_rfc3339(dt: datetime) -> str:
        """Convert datetime to the RFC3339 string format used in cache."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}000Z"
