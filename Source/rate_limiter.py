"""
Token bucket rate limiter for Oanda API requests.

Enforces a configurable maximum requests-per-second ceiling to avoid
API rate limiting and potential bans. Thread-safe via threading.Lock.

Usage:
    limiter = RateLimiter(max_requests_per_second=10)
    limiter.acquire()  # blocks until a token is available
"""

import threading
import time
from collections import deque


class RateLimiter:
    """Token bucket rate limiter with blocking and non-blocking acquire.

    Args:
        max_requests_per_second: Maximum allowed requests per second.
            Defaults to 10 (conservative; Oanda allows 100 on established
            connections but project policy specifies 10).
    """

    def __init__(self, max_requests_per_second: int = 10):
        self.max_requests_per_second = max_requests_per_second
        self._interval = 1.0 / max_requests_per_second
        self._lock = threading.Lock()
        self._last_request_time = 0.0
        self.recent_requests: deque = deque(maxlen=100)

    def acquire(self) -> None:
        """Block until a request token is available.

        Sleeps if the minimum interval between requests has not elapsed.
        Records the request timestamp for monitoring.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
            self._last_request_time = time.monotonic()
            self.recent_requests.append(time.time())

    def try_acquire(self) -> bool:
        """Non-blocking attempt to acquire a request token.

        Returns:
            True if a token was acquired, False if rate limit would
            be exceeded.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._interval:
                return False
            self._last_request_time = now
            self.recent_requests.append(time.time())
            return True

    @property
    def current_rate(self) -> float:
        """Approximate requests per second over the last second.

        Returns:
            Float representing the recent request rate.
        """
        now = time.time()
        cutoff = now - 1.0
        count = sum(1 for t in self.recent_requests if t >= cutoff)
        return float(count)
