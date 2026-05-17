"""
Core Oanda REST API client.

Provides authenticated access to the Oanda v20 API for account data,
instrument discovery, candlestick retrieval, order management, and
trade operations. Built on requests.Session for persistent HTTP
connections (keep-alive) per Oanda best practices.

Usage:
    from trading_bot.source.oanda_client import OandaClient

    with OandaClient() as client:
        summary = client.get_account_summary()
        candles = client.get_candles("EUR_USD", granularity="H1", count=100)
        result = client.place_market_order("EUR_USD", 100, stop_loss="1.0800")
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

try:
    from . import config
    from .rate_limiter import RateLimiter
except ImportError:
    import config
    from rate_limiter import RateLimiter


def _parse_oanda_time(time_str: str) -> datetime:
    """Parse an Oanda RFC3339 timestamp to a UTC datetime.

    Handles nanosecond precision (e.g. ``2024-01-01T00:00:00.000000000Z``)
    by truncating to microsecond precision before parsing.
    """
    s = time_str.replace("Z", "")
    if "." in s:
        integer_part, frac = s.split(".", 1)
        offset = ""
        for sep in ("+", "-"):
            if sep in frac:
                idx = frac.index(sep)
                offset = frac[idx:]
                frac = frac[:idx]
                break
        frac = frac[:6].ljust(6, "0")
        s = f"{integer_part}.{frac}"
        if offset:
            s += offset
        else:
            s += "+00:00"
    else:
        s += "+00:00"
    return datetime.fromisoformat(s)


class OandaAPIError(Exception):
    """Exception raised for non-2xx responses from the Oanda API.

    Attributes:
        status_code: HTTP status code from the response.
        error_body: Parsed JSON error body, or raw text if not JSON.
        url: The request URL that produced the error.
        method: The HTTP method used.
    """

    def __init__(
        self,
        status_code: int,
        error_body: Any,
        url: str,
        method: str = "GET",
    ):
        self.status_code = status_code
        self.error_body = error_body
        self.url = url
        self.method = method

        # Build human-readable message
        if isinstance(error_body, dict):
            msg = error_body.get("errorMessage", str(error_body))
        else:
            msg = str(error_body)

        super().__init__(
            f"Oanda API {method} {url} returned {status_code}: {msg}"
        )


class OandaClient:
    """Authenticated client for the Oanda v20 REST API.

    Uses requests.Session for persistent HTTP connections (keep-alive).
    Supports context manager protocol for clean resource management.

    Args:
        api_key: Oanda API bearer token. Defaults to config.API_KEY.
        account_id: Oanda account identifier. Defaults to config.ACCOUNT_ID.
        practice: If True, use practice API. If False, use live API.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        account_id: Optional[str] = None,
        practice: bool = True,
    ):
        self.api_key = api_key or config.API_KEY
        self.account_id = account_id or config.ACCOUNT_ID
        # 2026-04-27: assertion moved here from config.py module-load time.
        # Lets unrelated code import Source/* without OANDA env vars; only
        # actual OANDA users see the failure (and at a useful point).
        if not self.api_key:
            raise RuntimeError(
                "OANDA API key missing. Set OANDA_API_KEY env var or place key at "
                f"{config.__file__.replace('config.py', '../API/OANDA_API_KEY.txt')}"
            )
        if not self.account_id:
            raise RuntimeError(
                "OANDA_ACCOUNT_ID env var required to instantiate OandaClient — "
                "set it before launching the trading system."
            )
        self.base_url = config.PRACTICE_URL if practice else config.LIVE_URL

        # Rate limiter: 10 req/s conservative ceiling (Oanda allows 100)
        self.rate_limiter = RateLimiter(max_requests_per_second=10)

        # Create persistent session (keep-alive by default with Session)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        })

    def __enter__(self):
        """Enter context manager."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager and close the session."""
        self.close()
        return False

    def close(self):
        """Close the underlying HTTP session."""
        self.session.close()

    @property
    def broker_name(self) -> str:
        """Broker identifier for cache partitioning."""
        return "oanda"

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send an authenticated request to the Oanda API.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE).
            path: API path (e.g. /v3/accounts).
            params: Optional query parameters.
            json_body: Optional JSON request body.

        Returns:
            Parsed JSON response as a dictionary.

        Raises:
            OandaAPIError: If the response status code is not 2xx.
        """
        # ── Circuit Breaker: block requests when OANDA is consistently failing ──
        try:
            from connection_sentry import oanda_breaker
            if not oanda_breaker.allow_request():
                _cb_status = oanda_breaker.get_status()
                raise OandaAPIError(
                    status_code=0,
                    error_body=(
                        f"OANDA circuit breaker is OPEN — "
                        f"{_cb_status['consecutive_failures']} consecutive failures, "
                        f"cooldown {_cb_status['cooldown_s']}s. "
                        f"Next retry in {_cb_status['cooldown_s'] - _cb_status['time_in_state_s']:.0f}s"
                    ),
                    url=f"{self.base_url}{path}",
                    method=method,
                )
        except ImportError:
            pass  # Sentry not installed — operate without circuit breaker

        self.rate_limiter.acquire()
        url = f"{self.base_url}{path}"

        # Retry up to 3 times on timeout or 503 Service Unavailable.
        # 429 Rate Limited gets a longer backoff. All other errors raise immediately.
        _max_attempts = 3
        _last_exc: Optional[Exception] = None
        for _attempt in range(_max_attempts):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    timeout=30,  # 30s hard timeout — prevents indefinite hangs
                )
            except requests.exceptions.Timeout as _te:
                _last_exc = _te
                self._record_breaker_failure()
                if _attempt < _max_attempts - 1:
                    _backoff = 2 ** _attempt
                    logger.warning("OANDA %s %s timed out (attempt %d/%d) — retrying in %ds",
                                   method, path, _attempt + 1, _max_attempts, _backoff)
                    time.sleep(_backoff)
                    continue
                raise OandaAPIError(
                    status_code=0,
                    error_body=f"OANDA timeout after {_max_attempts} attempts: {_te}",
                    url=url,
                    method=method,
                )
            except requests.exceptions.RequestException as _re:
                # Network-level errors (DNS, connection refused) — fail fast
                self._record_breaker_failure()
                raise OandaAPIError(
                    status_code=0,
                    error_body=f"Network error: {_re}",
                    url=url,
                    method=method,
                )

            if response.status_code == 503:
                self._record_breaker_failure()
                if _attempt < _max_attempts - 1:
                    _backoff = 2 ** _attempt
                    logger.warning("OANDA 503 on %s %s (attempt %d/%d) — retrying in %ds",
                                   method, path, _attempt + 1, _max_attempts, _backoff)
                    time.sleep(_backoff)
                    continue
            elif response.status_code == 429:
                _retry_after = int(response.headers.get("Retry-After", 5))
                logger.warning("OANDA 429 rate limit on %s %s — backing off %ds",
                               method, path, _retry_after)
                time.sleep(_retry_after)
                continue

            if not response.ok:
                try:
                    error_body = response.json()
                except (ValueError, requests.exceptions.JSONDecodeError):
                    error_body = response.text
                # 4xx client errors are NOT circuit breaker failures (bad request, not server down)
                if response.status_code >= 500:
                    self._record_breaker_failure()
                raise OandaAPIError(
                    status_code=response.status_code,
                    error_body=error_body,
                    url=url,
                    method=method,
                )

            # Success — record it so half-open → closed
            self._record_breaker_success()
            return response.json()

        # Should never reach here but satisfies type checker
        raise OandaAPIError(
            status_code=0,
            error_body=f"OANDA request failed after {_max_attempts} attempts",
            url=url,
            method=method,
        )

    @staticmethod
    def _record_breaker_success():
        """Record a successful request with the circuit breaker."""
        try:
            from connection_sentry import oanda_breaker
            oanda_breaker.record_success()
        except ImportError:
            pass

    @staticmethod
    def _record_breaker_failure():
        """Record a failed request with the circuit breaker."""
        try:
            from connection_sentry import oanda_breaker
            oanda_breaker.record_failure()
        except ImportError:
            pass

    @staticmethod
    def _to_rfc3339(dt: datetime) -> str:
        """Convert a datetime object to Oanda RFC3339 format.

        Args:
            dt: A datetime object. If naive (no tzinfo), assumed UTC.

        Returns:
            RFC3339 string like '2024-01-15T12:00:00.000000000Z'.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Oanda expects nanosecond precision with Z suffix
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}000Z"

    # ------------------------------------------------------------------
    # Account endpoints
    # ------------------------------------------------------------------

    def get_accounts(self) -> List[Dict[str, Any]]:
        """List all accounts authorized for the current token.

        Returns:
            List of account property dicts with 'id' and 'tags' fields.

        Endpoint: GET /v3/accounts
        """
        data = self._request("GET", "/v3/accounts")
        return data.get("accounts", [])

    def get_account_details(self) -> Dict[str, Any]:
        """Get full details for the configured account.

        Returns full pending orders, open trades, and open positions.

        Returns:
            Account details dictionary with all fields.

        Endpoint: GET /v3/accounts/{accountID}
        """
        data = self._request("GET", f"/v3/accounts/{self.account_id}")
        return data.get("account", {})

    def get_account_summary(self) -> Dict[str, Any]:
        """Get a summary for the configured account.

        Returns balance, NAV, margin, P&L, and trade counts
        without the full position/order/trade lists.

        Returns:
            Account summary dictionary with fields like:
            balance, NAV, currency, marginAvailable, unrealizedPL, etc.

        Endpoint: GET /v3/accounts/{accountID}/summary
        """
        data = self._request("GET", f"/v3/accounts/{self.account_id}/summary")
        return data.get("account", {})

    # ------------------------------------------------------------------
    # Instrument endpoints
    # ------------------------------------------------------------------

    def get_instruments(
        self,
        instruments: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get tradeable instruments for the configured account.

        Args:
            instruments: Optional list of instrument names to filter
                (e.g. ['EUR_USD', 'GBP_USD']). If None, returns all.

        Returns:
            List of instrument dicts with fields: name, type,
            displayName, pipLocation, displayPrecision,
            tradeUnitsPrecision, marginRate, etc.

        Endpoint: GET /v3/accounts/{accountID}/instruments
        """
        params = {}
        if instruments:
            params["instruments"] = ",".join(instruments)

        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/instruments",
            params=params or None,
        )
        return data.get("instruments", [])

    def get_candles(
        self,
        instrument: str,
        granularity: str = "H1",
        count: int = 500,
        price: str = "M",
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        include_first: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch candlestick data for an instrument.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').
            granularity: Candle granularity (S5, M1, M5, M15, H1, H4, D, W, M).
            count: Number of candles to return (max 5000). Ignored if both
                from_time and to_time are provided.
            price: Price component(s): 'M' (mid), 'B' (bid), 'A' (ask),
                or combinations like 'MBA'.
            from_time: Start of time range (datetime object).
            to_time: End of time range (datetime object).
            include_first: Whether to include the candle covered by from_time.

        Returns:
            List of candle dicts, each with 'time', 'volume', 'complete',
            and price dicts (mid/bid/ask) containing o, h, l, c values.

        Endpoint: GET /v3/instruments/{instrument}/candles
        """
        params: Dict[str, Any] = {
            "granularity": granularity,
            "price": price,
        }

        if from_time is not None and to_time is not None:
            # When both from and to are specified, don't send count
            params["from"] = self._to_rfc3339(from_time)
            params["to"] = self._to_rfc3339(to_time)
        elif from_time is not None:
            params["from"] = self._to_rfc3339(from_time)
            params["count"] = min(count, config.MAX_CANDLE_COUNT)
        elif to_time is not None:
            params["to"] = self._to_rfc3339(to_time)
            params["count"] = min(count, config.MAX_CANDLE_COUNT)
        else:
            params["count"] = min(count, config.MAX_CANDLE_COUNT)

        if include_first is not None:
            params["includeFirst"] = str(include_first).lower()

        data = self._request(
            "GET",
            f"/v3/instruments/{instrument}/candles",
            params=params,
        )
        return data.get("candles", [])

    def fetch_candles_range(
        self,
        instrument: str,
        granularity: str,
        from_time: datetime,
        to_time: datetime,
        price: str = "M",
        chunk_size: int = config.MAX_CANDLE_COUNT,
    ) -> List[Dict[str, Any]]:
        """Fetch ALL candles between *from_time* and *to_time*.

        Chains multiple ``get_candles`` requests (each up to *chunk_size*
        candles) using the last candle's timestamp as the cursor for the
        next page.  On the first request ``include_first=True`` to include
        the candle at *from_time*; subsequent requests use
        ``include_first=False`` to avoid duplicates.

        Rate limiting is handled by the underlying ``get_candles`` call
        (no double throttling).

        Args:
            instrument: Instrument name (e.g. ``'EUR_USD'``).
            granularity: Candle granularity (e.g. ``'H1'``).
            from_time: Start of the desired range (inclusive).
            to_time: End of the desired range (inclusive).
            price: Price component(s): ``'M'``, ``'B'``, ``'A'``,
                or combinations like ``'MBA'``.
            chunk_size: Max candles per API request (default
                :pydata:`config.MAX_CANDLE_COUNT`).

        Returns:
            List of candle dicts covering the entire requested range,
            sorted chronologically.
        """
        all_candles: List[Dict[str, Any]] = []
        cursor = from_time

        while True:
            candles = self.get_candles(
                instrument=instrument,
                granularity=granularity,
                count=chunk_size,
                price=price,
                from_time=cursor,
                include_first=(cursor == from_time),
            )

            if not candles:
                break

            all_candles.extend(candles)

            # Advance cursor to the last candle's time
            last_time_str = candles[-1]["time"]
            cursor = _parse_oanda_time(last_time_str)

            logger.info(
                "Fetched %d candles so far, cursor at %s",
                len(all_candles),
                cursor,
            )

            if cursor >= to_time:
                break

            if len(candles) < chunk_size:
                break

        # Filter out any candles beyond the requested range
        all_candles = [
            c
            for c in all_candles
            if _parse_oanda_time(c["time"]) <= to_time
        ]

        return all_candles

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def verify_connection(self) -> Dict[str, Any]:
        """Verify API connectivity by fetching account summary and instrument count.

        Returns a dict with connection status and basic account info.
        Catches OandaAPIError and returns connected=False with error details.

        Returns:
            Dict with keys: connected, account_id, balance, currency,
            nav, instruments_count. On failure: connected=False, error, error_details.
        """
        try:
            summary = self.get_account_summary()
            instruments = self.get_instruments()
            return {
                "connected": True,
                "account_id": summary.get("id", self.account_id),
                "balance": summary.get("balance"),
                "currency": summary.get("currency"),
                "nav": summary.get("NAV"),
                "unrealized_pl": summary.get("unrealizedPL"),
                "margin_available": summary.get("marginAvailable"),
                "instruments_count": len(instruments),
            }
        except OandaAPIError as e:
            return {
                "connected": False,
                "error": str(e),
                "error_details": {
                    "status_code": e.status_code,
                    "error_body": e.error_body,
                    "url": e.url,
                },
            }
        except Exception as e:
            return {
                "connected": False,
                "error": str(e),
                "error_details": {"exception_type": type(e).__name__},
            }

    # ------------------------------------------------------------------
    # Account changes (incremental sync per Best Practices)
    # ------------------------------------------------------------------

    def get_account_changes(
        self, since_transaction_id: str
    ) -> Dict[str, Any]:
        """Get account changes since a given transaction ID.

        Used for incremental sync per Oanda Best Practices: after an
        initial full snapshot via get_account_details(), repeatedly poll
        this endpoint with the last known transaction ID to receive only
        what changed.

        Args:
            since_transaction_id: The last known transaction ID. The
                response will include changes that occurred after this ID.

        Returns:
            Dict with keys: 'changes' (orders/trades/positions that changed),
            'state' (current price-dependent account state), and
            'lastTransactionID'.

        Endpoint: GET /v3/accounts/{accountID}/changes?sinceTransactionID={id}
        """
        return self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/changes",
            params={"sinceTransactionID": since_transaction_id},
        )

    # ------------------------------------------------------------------
    # Trade endpoints
    # ------------------------------------------------------------------

    def get_open_trades(self) -> List[Dict[str, Any]]:
        """Get all currently open trades for the configured account.

        Returns:
            List of open trade dicts with fields like id, instrument,
            currentUnits, price, unrealizedPL, etc.

        Endpoint: GET /v3/accounts/{accountID}/openTrades
        """
        data = self._request(
            "GET", f"/v3/accounts/{self.account_id}/openTrades"
        )
        return data.get("trades", [])

    def get_trade(self, trade_id: str) -> Dict[str, Any]:
        """Get details of a specific trade.

        Args:
            trade_id: The trade identifier.

        Returns:
            Trade details dictionary.

        Endpoint: GET /v3/accounts/{accountID}/trades/{tradeID}
        """
        data = self._request(
            "GET", f"/v3/accounts/{self.account_id}/trades/{trade_id}"
        )
        return data.get("trade", {})

    def get_trade_close_from_transactions(self, trade_id: str) -> Dict[str, Any]:
        """Find the ORDER_FILL transaction that closed a trade, via transactions API.

        Use as a fallback when get_trade() returns 404 (trade purged from active list).
        OANDA keeps transaction history longer than the trade record itself.

        Returns a dict with: close_price, realized_pl, close_time, open_price, open_time,
        instrument, units, reason. Empty dict if no close fill found.

        2026-04-23: needed for close reconciliation when OANDA returns 404 on recently-
        closed trades — saw trade 9967 where dashboard showed 0p/$0/unknown because
        get_trade 404'd within seconds of close. Transactions API still had the fill.
        """
        try:
            # Get recent transactions (last ~100) and find the fill for this trade
            summary = self._request("GET", f"/v3/accounts/{self.account_id}/summary")
            last_id = int(summary.get("account", {}).get("lastTransactionID", 0))
            if not last_id:
                return {}
            data = self._request(
                "GET", f"/v3/accounts/{self.account_id}/transactions/sinceid",
                params={"id": last_id - 200},
            )
            txns = data.get("transactions", []) or []
            result = {}
            tid_str = str(trade_id)
            for tx in txns:
                # Is this the open fill?
                to = tx.get("tradeOpened")
                if isinstance(to, dict) and str(to.get("tradeID", "")) == tid_str:
                    result["open_price"] = float(to.get("price") or tx.get("price") or 0)
                    result["open_time"] = tx.get("time", "")
                    result["instrument"] = tx.get("instrument", "")
                    result["open_units"] = int(to.get("units", 0) or 0)
                # Is this a close fill?
                for tc in tx.get("tradesClosed") or []:
                    if str(tc.get("tradeID", "")) == tid_str:
                        result["close_price"] = float(tc.get("price") or tx.get("price") or 0)
                        result["realized_pl"] = float(tc.get("realizedPL") or 0)
                        result["close_time"] = tx.get("time", "")
                        result["close_units"] = int(tc.get("units", 0) or 0)
                        result["reason"] = tx.get("reason", "")
                        if not result.get("instrument"):
                            result["instrument"] = tx.get("instrument", "")
            return result
        except Exception:
            return {}

    def get_trades(
        self,
        instrument: Optional[str] = None,
        state: str = "OPEN",
        count: int = 50,
    ) -> List[Dict[str, Any]]:
        """Get a list of trades for the configured account.

        Args:
            instrument: Optional instrument name to filter by.
            state: Trade state filter: OPEN, CLOSED, CLOSE_WHEN_TRADEABLE, ALL.
            count: Maximum number of trades to return (default 50, max 500).

        Returns:
            List of trade dicts.

        Endpoint: GET /v3/accounts/{accountID}/trades
        """
        params: Dict[str, Any] = {"state": state, "count": min(count, 500)}
        if instrument:
            params["instrument"] = instrument

        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/trades",
            params=params,
        )
        return data.get("trades", [])

    # ------------------------------------------------------------------
    # Position endpoints
    # ------------------------------------------------------------------

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Get all open positions for the configured account.

        Returns:
            List of position dicts with instrument, long/short units,
            unrealizedPL, etc.

        Endpoint: GET /v3/accounts/{accountID}/openPositions
        """
        data = self._request(
            "GET", f"/v3/accounts/{self.account_id}/openPositions"
        )
        return data.get("positions", [])

    def get_positions(self) -> List[Dict[str, Any]]:
        """Get ALL positions for the configured account.

        Returns all positions including those with zero units, unlike
        :meth:`get_open_positions` which only returns non-zero positions.

        Returns:
            List of position dicts.

        Endpoint: GET /v3/accounts/{accountID}/positions
        """
        data = self._request(
            "GET", f"/v3/accounts/{self.account_id}/positions"
        )
        return data.get("positions", [])

    def get_position(self, instrument: str) -> Dict[str, Any]:
        """Get the position for a specific instrument.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').

        Returns:
            Position details dictionary.

        Endpoint: GET /v3/accounts/{accountID}/positions/{instrument}
        """
        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/positions/{instrument}",
        )
        return data.get("position", {})

    def close_position(
        self,
        instrument: str,
        long_units: Optional[str] = None,
        short_units: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Close the long or short side of a position for an instrument.

        At least one of *long_units* or *short_units* must be provided.
        Use ``"ALL"`` to close the entire side, or a specific unit count
        for a partial close.

        Args:
            instrument: Instrument name (e.g. ``'EUR_USD'``).
            long_units: Units to close on the long side
                (``"ALL"`` or numeric string).
            short_units: Units to close on the short side
                (``"ALL"`` or numeric string).

        Returns:
            Response dict with position close transactions.

        Raises:
            ValueError: If neither long_units nor short_units is provided.

        Endpoint: PUT /v3/accounts/{accountID}/positions/{instrument}/close
        """
        if long_units is None and short_units is None:
            raise ValueError(
                "At least one of long_units or short_units must be provided"
            )

        body: Dict[str, Any] = {}
        if long_units is not None:
            body["longUnits"] = str(long_units)
        if short_units is not None:
            body["shortUnits"] = str(short_units)

        return self._request(
            "PUT",
            f"/v3/accounts/{self.account_id}/positions/{instrument}/close",
            json_body=body,
        )

    # ------------------------------------------------------------------
    # Transaction endpoints
    # ------------------------------------------------------------------

    def get_transactions_since(
        self, since_id: str, transaction_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get transactions since a given transaction ID.

        Args:
            since_id: Transaction ID to start from (exclusive).
            transaction_type: Optional CSV of transaction types to filter
                (e.g. 'ORDER_FILL,TRADE_CLOSE').

        Returns:
            List of transaction dicts.

        Endpoint: GET /v3/accounts/{accountID}/transactions/sinceid?id={id}
        """
        params: Dict[str, Any] = {"id": since_id}
        if transaction_type:
            params["type"] = transaction_type

        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/transactions/sinceid",
            params=params,
        )
        return data.get("transactions", [])

    def get_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """Get details of a single transaction.

        Args:
            transaction_id: The transaction identifier.

        Returns:
            Transaction details dictionary.

        Endpoint: GET /v3/accounts/{accountID}/transactions/{transactionID}
        """
        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/transactions/{transaction_id}",
        )
        return data.get("transaction", {})

    def get_transactions_by_time(
        self,
        from_time: datetime,
        to_time: Optional[datetime] = None,
        page_size: int = 100,
        transaction_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List transactions by time range (paginated).

        Returns page URLs rather than transaction bodies directly.
        The caller iterates the returned page URLs to fetch full
        transaction details.

        Args:
            from_time: Start of time range.
            to_time: End of time range. Defaults to current time.
            page_size: Number of transactions per page (max 1000).
            transaction_type: Optional CSV of transaction types to filter
                (e.g. ``'ORDER_FILL,TRADE_CLOSE'``).

        Returns:
            Dict with ``count``, ``from``, ``to``, ``pageSize``,
            and ``pages`` (list of page URLs).

        Endpoint: GET /v3/accounts/{accountID}/transactions
        """
        params: Dict[str, Any] = {
            "from": self._to_rfc3339(from_time),
            "pageSize": min(page_size, 1000),
        }
        if to_time is not None:
            params["to"] = self._to_rfc3339(to_time)
        if transaction_type:
            params["type"] = transaction_type

        return self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/transactions",
            params=params,
        )

    def get_transactions_idrange(
        self,
        from_id: str,
        to_id: str,
        transaction_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get transactions within a specific ID range.

        Args:
            from_id: Starting transaction ID (inclusive).
            to_id: Ending transaction ID (inclusive).
            transaction_type: Optional CSV of transaction types to filter.

        Returns:
            List of transaction dicts.

        Endpoint: GET /v3/accounts/{accountID}/transactions/idrange
        """
        params: Dict[str, Any] = {"from": from_id, "to": to_id}
        if transaction_type:
            params["type"] = transaction_type

        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/transactions/idrange",
            params=params,
        )
        return data.get("transactions", [])

    # ------------------------------------------------------------------
    # Trade close
    # ------------------------------------------------------------------

    def close_trade(
        self,
        trade_id: str,
        units: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Close (fully or partially) an open trade.

        Args:
            trade_id: The trade identifier to close.
            units: Optional number of units to close (as string).
                If None, closes the entire trade.

        Returns:
            Full API response dict with orderCreateTransaction,
            orderFillTransaction, etc.

        Endpoint: PUT /v3/accounts/{accountID}/trades/{tradeID}/close
        """
        body: Optional[Dict[str, Any]] = None
        if units is not None:
            body = {"units": str(units)}
        return self._request(
            "PUT",
            f"/v3/accounts/{self.account_id}/trades/{trade_id}/close",
            json_body=body,
        )

    def set_trade_orders(
        self,
        trade_id: str,
        take_profit: Optional[Dict[str, Any]] = None,
        stop_loss: Optional[Dict[str, Any]] = None,
        trailing_stop_loss: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Modify TP, SL, and/or trailing SL on an existing trade.

        Any parameter can be None (not sent) or set to ``{"price": "0"}``
        to cancel the existing dependent order.

        Args:
            trade_id: The trade identifier.
            take_profit: Dict with at least ``price`` key
                (e.g. ``{"price": "1.1100"}``).
            stop_loss: Dict with ``price`` and optional ``timeInForce``
                (e.g. ``{"price": "1.0900", "timeInForce": "GTC"}``).
            trailing_stop_loss: Dict with ``distance`` key
                (e.g. ``{"distance": "0.0050"}``).

        Returns:
            Response dict with trade modification transactions.

        Endpoint: PUT /v3/accounts/{accountID}/trades/{tradeSpecifier}/orders
        """
        body: Dict[str, Any] = {}
        if take_profit is not None:
            body["takeProfit"] = take_profit
        if stop_loss is not None:
            body["stopLoss"] = stop_loss
        if trailing_stop_loss is not None:
            body["trailingStopLoss"] = trailing_stop_loss

        return self._request(
            "PUT",
            f"/v3/accounts/{self.account_id}/trades/{trade_id}/orders",
            json_body=body,
        )

    # ------------------------------------------------------------------
    # Pricing endpoints
    # ------------------------------------------------------------------

    def get_latest_candles(
        self,
        candle_specifications: List[str],
        units: Optional[str] = None,
        smooth: bool = False,
        daily_alignment: int = 17,
        alignment_timezone: str = "America/New_York",
    ) -> List[Dict[str, Any]]:
        """Get the latest candles for multiple instrument/granularity combos.

        Also known as "dancing bears". Fetches the most recently completed
        candle for each specification in a single API call.

        Args:
            candle_specifications: List of specs in the format
                ``{instrument}:{granularity}:{price_component}``
                e.g. ``["EUR_USD:H1:M", "USD_JPY:M15:M"]``.
            units: Decimal number of units for volume-weighted average
                bid/ask prices. Default ``"1"``.
            smooth: If True, use previous candle close as open price.
            daily_alignment: Hour (0-23) for daily candle alignment.
            alignment_timezone: Timezone for daily alignment.

        Returns:
            List of latest candle response dicts (one per specification).

        Endpoint: GET /v3/accounts/{accountID}/candles/latest
        """
        params: Dict[str, Any] = {
            "candleSpecifications": ",".join(candle_specifications),
            "smooth": str(smooth).lower(),
            "dailyAlignment": daily_alignment,
            "alignmentTimezone": alignment_timezone,
        }
        if units is not None:
            params["units"] = str(units)

        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/candles/latest",
            params=params,
        )
        return data.get("latestCandles", [])

    def get_pricing(
        self,
        instruments: List[str],
        since: Optional[str] = None,
        include_units_available: bool = True,
        include_home_conversions: bool = False,
    ) -> Dict[str, Any]:
        """Get live bid/ask pricing with liquidity depth for instruments.

        Args:
            instruments: List of instrument names
                (e.g. ``["EUR_USD", "USD_JPY"]``).
            since: Optional RFC3339 timestamp. Only prices changed after
                this time are returned.
            include_units_available: Include available units in response.
            include_home_conversions: Include home currency conversion
                factors.

        Returns:
            Full response dict with ``prices`` list (each containing
            bid/ask arrays with liquidity buckets) and ``time``.

        Endpoint: GET /v3/accounts/{accountID}/pricing
        """
        params: Dict[str, Any] = {
            "instruments": ",".join(instruments),
            "includeUnitsAvailable": str(include_units_available).lower(),
            "includeHomeConversions": str(include_home_conversions).lower(),
        }
        if since is not None:
            params["since"] = since

        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/pricing",
            params=params,
        )
        return data

    def get_account_candles(
        self,
        instrument: str,
        granularity: str = "H1",
        count: int = 500,
        price: str = "M",
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        smooth: bool = False,
        daily_alignment: int = 17,
        alignment_timezone: str = "America/New_York",
    ) -> List[Dict[str, Any]]:
        """Fetch candlestick data for an instrument scoped to the account.

        Same parameter pattern as :meth:`get_candles` but uses the
        account-scoped URL which supports additional alignment options.

        Args:
            instrument: Instrument name (e.g. ``'EUR_USD'``).
            granularity: Candle granularity (S5 .. M).
            count: Number of candles (max 5000). Ignored when both
                *from_time* and *to_time* are provided.
            price: Price component(s): ``'M'``, ``'B'``, ``'A'``,
                or combinations like ``'MBA'``.
            from_time: Start of time range.
            to_time: End of time range.
            smooth: Use previous close as open if True.
            daily_alignment: Hour (0-23) for daily candle alignment.
            alignment_timezone: Timezone for daily alignment.

        Returns:
            List of candle dicts with time, volume, complete, and
            price component dicts.

        Endpoint: GET /v3/accounts/{accountID}/instruments/{instrument}/candles
        """
        params: Dict[str, Any] = {
            "granularity": granularity,
            "price": price,
            "smooth": str(smooth).lower(),
            "dailyAlignment": daily_alignment,
            "alignmentTimezone": alignment_timezone,
        }

        if from_time is not None and to_time is not None:
            params["from"] = self._to_rfc3339(from_time)
            params["to"] = self._to_rfc3339(to_time)
        elif from_time is not None:
            params["from"] = self._to_rfc3339(from_time)
            params["count"] = min(count, config.MAX_CANDLE_COUNT)
        elif to_time is not None:
            params["to"] = self._to_rfc3339(to_time)
            params["count"] = min(count, config.MAX_CANDLE_COUNT)
        else:
            params["count"] = min(count, config.MAX_CANDLE_COUNT)

        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/instruments/{instrument}/candles",
            params=params,
        )
        return data.get("candles", [])

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_order_body(
        order_type: str,
        instrument: Optional[str] = None,
        units: Optional[int] = None,
        price: Optional[str] = None,
        trade_id: Optional[str] = None,
        distance: Optional[str] = None,
        time_in_force: str = "GTC",
        gtd_time: Optional[str] = None,
        stop_loss: Optional[str] = None,
        take_profit: Optional[str] = None,
        trailing_stop_distance: Optional[str] = None,
        client_extensions: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Build a complete order request body for the Oanda API.

        All price and unit values are converted to strings as required
        by the Oanda v20 API.

        Args:
            order_type: Order type (MARKET, LIMIT, STOP, MARKET_IF_TOUCHED,
                TAKE_PROFIT, STOP_LOSS, TRAILING_STOP_LOSS).
            instrument: Instrument name (required for entry orders).
            units: Number of units (positive=buy, negative=sell).
            price: Trigger/limit price as string.
            trade_id: Trade ID for dependent orders (TP/SL/TSL).
            distance: Trailing distance for TRAILING_STOP_LOSS.
            time_in_force: Time in force (FOK, GTC, GTD, GFD).
            gtd_time: GTD expiry time (RFC3339) when time_in_force is GTD.
            stop_loss: Stop loss price to attach via stopLossOnFill.
            take_profit: Take profit price to attach via takeProfitOnFill.
            trailing_stop_distance: Distance for trailingStopLossOnFill.
            client_extensions: Dict with optional keys id, tag, comment.

        Returns:
            Dict in the form {"order": {...}} ready for JSON POST.
        """
        order: Dict[str, Any] = {"type": order_type}

        if instrument is not None:
            order["instrument"] = instrument
        if units is not None:
            order["units"] = str(units)
        if price is not None:
            order["price"] = str(price)
        if trade_id is not None:
            order["tradeID"] = str(trade_id)
        if distance is not None:
            order["distance"] = str(distance)

        order["timeInForce"] = time_in_force
        if time_in_force == "GTD" and gtd_time is not None:
            order["gtdTime"] = gtd_time

        # positionFill default for entry orders
        if order_type in ("MARKET", "LIMIT", "STOP", "MARKET_IF_TOUCHED"):
            order["positionFill"] = "DEFAULT"

        # Attach on-fill protective orders
        if stop_loss is not None:
            order["stopLossOnFill"] = {
                "price": str(stop_loss),
                "timeInForce": "GTC",
            }
        if take_profit is not None:
            order["takeProfitOnFill"] = {"price": str(take_profit)}
        if trailing_stop_distance is not None:
            order["trailingStopLossOnFill"] = {
                "distance": str(trailing_stop_distance),
            }

        # Client extensions
        if client_extensions is not None:
            order["clientExtensions"] = client_extensions

        return {"order": order}

    # ------------------------------------------------------------------
    # Order creation — 7 order types
    # ------------------------------------------------------------------

    def place_market_order(
        self,
        instrument: str,
        units: int,
        stop_loss: Optional[str] = None,
        take_profit: Optional[str] = None,
        trailing_stop_distance: Optional[str] = None,
        client_extensions: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Place a Market order (Fill Or Kill).

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').
            units: Number of units. Positive=buy, negative=sell/short.
            stop_loss: Optional stop loss price (string).
            take_profit: Optional take profit price (string).
            trailing_stop_distance: Optional trailing stop distance (string).
            client_extensions: Optional dict with id, tag, comment.

        Returns:
            Full API response dict with orderCreateTransaction,
            orderFillTransaction, etc.

        Endpoint: POST /v3/accounts/{accountID}/orders
        """
        body = self._build_order_body(
            order_type="MARKET",
            instrument=instrument,
            units=units,
            time_in_force="FOK",
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop_distance=trailing_stop_distance,
            client_extensions=client_extensions,
        )
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            json_body=body,
        )

    def place_limit_order(
        self,
        instrument: str,
        units: int,
        price: str,
        stop_loss: Optional[str] = None,
        take_profit: Optional[str] = None,
        trailing_stop_distance: Optional[str] = None,
        client_extensions: Optional[Dict[str, str]] = None,
        time_in_force: str = "GTC",
        gtd_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place a Limit order at a specified price.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').
            units: Number of units. Positive=buy, negative=sell.
            price: The price threshold for the Limit order (string).
            stop_loss: Optional stop loss price.
            take_profit: Optional take profit price.
            trailing_stop_distance: Optional trailing stop distance.
            client_extensions: Optional dict with id, tag, comment.
            time_in_force: GTC (Good Til Cancelled) or GTD (Good Til Date).
            gtd_time: Required when time_in_force is GTD (RFC3339).

        Returns:
            Full API response dict.

        Endpoint: POST /v3/accounts/{accountID}/orders
        """
        body = self._build_order_body(
            order_type="LIMIT",
            instrument=instrument,
            units=units,
            price=price,
            time_in_force=time_in_force,
            gtd_time=gtd_time,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop_distance=trailing_stop_distance,
            client_extensions=client_extensions,
        )
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            json_body=body,
        )

    def place_stop_order(
        self,
        instrument: str,
        units: int,
        price: str,
        stop_loss: Optional[str] = None,
        take_profit: Optional[str] = None,
        trailing_stop_distance: Optional[str] = None,
        client_extensions: Optional[Dict[str, str]] = None,
        time_in_force: str = "GTC",
        gtd_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place a Stop order at a specified trigger price.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').
            units: Number of units. Positive=buy, negative=sell.
            price: The trigger price for the Stop order (string).
            stop_loss: Optional stop loss price.
            take_profit: Optional take profit price.
            trailing_stop_distance: Optional trailing stop distance.
            client_extensions: Optional dict with id, tag, comment.
            time_in_force: GTC or GTD.
            gtd_time: Required when time_in_force is GTD (RFC3339).

        Returns:
            Full API response dict.

        Endpoint: POST /v3/accounts/{accountID}/orders
        """
        body = self._build_order_body(
            order_type="STOP",
            instrument=instrument,
            units=units,
            price=price,
            time_in_force=time_in_force,
            gtd_time=gtd_time,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop_distance=trailing_stop_distance,
            client_extensions=client_extensions,
        )
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            json_body=body,
        )

    def place_market_if_touched_order(
        self,
        instrument: str,
        units: int,
        price: str,
        stop_loss: Optional[str] = None,
        take_profit: Optional[str] = None,
        trailing_stop_distance: Optional[str] = None,
        client_extensions: Optional[Dict[str, str]] = None,
        time_in_force: str = "GTC",
        gtd_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place a Market-If-Touched order.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').
            units: Number of units. Positive=buy, negative=sell.
            price: The trigger price (string).
            stop_loss: Optional stop loss price.
            take_profit: Optional take profit price.
            trailing_stop_distance: Optional trailing stop distance.
            client_extensions: Optional dict with id, tag, comment.
            time_in_force: GTC or GTD.
            gtd_time: Required when time_in_force is GTD (RFC3339).

        Returns:
            Full API response dict.

        Endpoint: POST /v3/accounts/{accountID}/orders
        """
        body = self._build_order_body(
            order_type="MARKET_IF_TOUCHED",
            instrument=instrument,
            units=units,
            price=price,
            time_in_force=time_in_force,
            gtd_time=gtd_time,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop_distance=trailing_stop_distance,
            client_extensions=client_extensions,
        )
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            json_body=body,
        )

    def place_take_profit_order(
        self,
        trade_id: str,
        price: str,
        time_in_force: str = "GTC",
        gtd_time: Optional[str] = None,
        client_extensions: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Place a Take-Profit dependent order on an existing trade.

        Args:
            trade_id: The trade to attach this order to.
            price: The take profit price (string).
            time_in_force: GTC or GTD.
            gtd_time: Required when time_in_force is GTD (RFC3339).
            client_extensions: Optional dict with id, tag, comment.

        Returns:
            Full API response dict.

        Endpoint: POST /v3/accounts/{accountID}/orders
        """
        body = self._build_order_body(
            order_type="TAKE_PROFIT",
            trade_id=trade_id,
            price=price,
            time_in_force=time_in_force,
            gtd_time=gtd_time,
            client_extensions=client_extensions,
        )
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            json_body=body,
        )

    def place_stop_loss_order(
        self,
        trade_id: str,
        price: str,
        time_in_force: str = "GTC",
        gtd_time: Optional[str] = None,
        client_extensions: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Place a Stop-Loss dependent order on an existing trade.

        Args:
            trade_id: The trade to attach this order to.
            price: The stop loss price (string).
            time_in_force: GTC or GTD.
            gtd_time: Required when time_in_force is GTD (RFC3339).
            client_extensions: Optional dict with id, tag, comment.

        Returns:
            Full API response dict.

        Endpoint: POST /v3/accounts/{accountID}/orders
        """
        body = self._build_order_body(
            order_type="STOP_LOSS",
            trade_id=trade_id,
            price=price,
            time_in_force=time_in_force,
            gtd_time=gtd_time,
            client_extensions=client_extensions,
        )
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            json_body=body,
        )

    def place_trailing_stop_loss_order(
        self,
        trade_id: str,
        distance: str,
        time_in_force: str = "GTC",
        gtd_time: Optional[str] = None,
        client_extensions: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Place a Trailing-Stop-Loss dependent order on an existing trade.

        Args:
            trade_id: The trade to attach this order to.
            distance: The trailing stop distance in price units (string).
            time_in_force: GTC or GTD.
            gtd_time: Required when time_in_force is GTD (RFC3339).
            client_extensions: Optional dict with id, tag, comment.

        Returns:
            Full API response dict.

        Endpoint: POST /v3/accounts/{accountID}/orders
        """
        body = self._build_order_body(
            order_type="TRAILING_STOP_LOSS",
            trade_id=trade_id,
            distance=distance,
            time_in_force=time_in_force,
            gtd_time=gtd_time,
            client_extensions=client_extensions,
        )
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            json_body=body,
        )

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def get_orders(
        self,
        instrument: Optional[str] = None,
        state: str = "PENDING",
        count: int = 50,
    ) -> List[Dict[str, Any]]:
        """Get a list of orders for the configured account.

        Args:
            instrument: Optional instrument name to filter by.
            state: Order state filter (PENDING, FILLED, TRIGGERED,
                CANCELLED, ALL). Default PENDING.
            count: Maximum number of orders to return (max 500).

        Returns:
            List of order dicts.

        Endpoint: GET /v3/accounts/{accountID}/orders
        """
        params: Dict[str, Any] = {
            "state": state,
            "count": min(count, 500),
        }
        if instrument:
            params["instrument"] = instrument

        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/orders",
            params=params,
        )
        return data.get("orders", [])

    def get_pending_orders(self) -> List[Dict[str, Any]]:
        """Get all pending orders for the configured account.

        Returns:
            List of pending order dicts.

        Endpoint: GET /v3/accounts/{accountID}/pendingOrders
        """
        data = self._request(
            "GET", f"/v3/accounts/{self.account_id}/pendingOrders"
        )
        return data.get("orders", [])

    def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get details of a specific order.

        Args:
            order_id: The order identifier (numeric ID or @client_id).

        Returns:
            Order details dictionary.

        Endpoint: GET /v3/accounts/{accountID}/orders/{orderSpecifier}
        """
        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/orders/{order_id}",
        )
        return data.get("order", {})

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel a pending order.

        Args:
            order_id: The order identifier to cancel.

        Returns:
            Full API response dict with orderCancelTransaction.

        Endpoint: PUT /v3/accounts/{accountID}/orders/{orderSpecifier}/cancel
        """
        return self._request(
            "PUT",
            f"/v3/accounts/{self.account_id}/orders/{order_id}/cancel",
        )

    def replace_order(
        self,
        order_id: str,
        new_order_body: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Replace (cancel + recreate) a pending order.

        Args:
            order_id: The order identifier to replace.
            new_order_body: Complete order request body in the form
                {"order": {...}} with the replacement order spec.

        Returns:
            Full API response dict with orderCancelTransaction and
            orderCreateTransaction.

        Endpoint: PUT /v3/accounts/{accountID}/orders/{orderSpecifier}
        """
        return self._request(
            "PUT",
            f"/v3/accounts/{self.account_id}/orders/{order_id}",
            json_body=new_order_body,
        )

    def set_order_client_extensions(
        self,
        order_id: str,
        client_extensions: Dict[str, str],
    ) -> Dict[str, Any]:
        """Update client extensions on a pending order.

        Args:
            order_id: The order identifier.
            client_extensions: Dict with optional keys id, tag, comment.

        Returns:
            Full API response dict with
            orderClientExtensionsModifyTransaction.

        Endpoint: PUT /v3/accounts/{accountID}/orders/{orderSpecifier}/clientExtensions
        """
        return self._request(
            "PUT",
            f"/v3/accounts/{self.account_id}/orders/{order_id}/clientExtensions",
            json_body={"clientExtensions": client_extensions},
        )
