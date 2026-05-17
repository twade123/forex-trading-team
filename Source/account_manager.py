"""
Account state management with incremental sync and market hours detection.

Provides the AccountManager class which maintains a local snapshot of the
Oanda account state, syncs it incrementally using the sinceTransactionID
pattern per Oanda Best Practices, caches instrument specs, and detects
forex market hours and trading sessions.

Usage:
    from trading_bot.source.oanda_client import OandaClient
    from trading_bot.source.account_manager import AccountManager

    client = OandaClient()
    am = AccountManager(client)
    am.initialize()  # full snapshot on startup

    # Periodic sync (efficient - only fetches changes)
    changes = am.sync()

    # Check market status
    if am.is_market_open() and am.is_london_ny_overlap():
        print("Prime trading window!")
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

try:
    from .oanda_client import OandaClient
except ImportError:
    from oanda_client import OandaClient

logger = logging.getLogger(__name__)

# Eastern Time zone for forex market hours
_ET = ZoneInfo("America/New_York")

# State file for persisting last_transaction_id across restarts
_STATE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_STATE_FILE = os.path.join(_STATE_DIR, ".account_state.json")


class AccountManager:
    """Manages local account state with incremental sync.

    Follows the Oanda Best Practices pattern:
    1. On startup, fetch a full account snapshot (initialize).
    2. On subsequent calls, use sinceTransactionID to get only changes (sync).
    3. Persist last_transaction_id so restarts resume efficiently.

    Also provides instrument discovery and forex market hours detection.

    Args:
        client: An authenticated OandaClient instance.
        state_file: Path to JSON file for persisting state across restarts.
            Defaults to 'Forex Trading Team/Source/.account_state.json'.
    """

    def __init__(
        self,
        client: OandaClient,
        state_file: str = _DEFAULT_STATE_FILE,
    ):
        self._client = client
        self._state_file = state_file

        # Account state (populated by initialize/sync)
        self._state: Dict[str, Any] = {}
        self._last_transaction_id: Optional[str] = None

        # Instrument cache: name -> full spec dict
        self._instruments: Dict[str, Dict[str, Any]] = {}

        # Try to restore persisted state (transaction ID only)
        self._load_state()

    # ------------------------------------------------------------------
    # Initialization and sync
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Fetch full account snapshot and cache instruments.

        This is the "startup snapshot" from Oanda Best Practices.
        Should be called once when the application starts. Sets up
        the initial account state and instruments cache.
        """
        # Full account details (includes orders, trades, positions)
        details = self._client.get_account_details()

        # Extract the full response to get lastTransactionID
        # get_account_details returns the 'account' dict; we need to
        # also get lastTransactionID from the raw response.
        # Re-fetch via raw _request to get the outer envelope.
        raw = self._client._request(
            "GET", f"/v3/accounts/{self._client.account_id}"
        )
        self._last_transaction_id = raw.get("lastTransactionID")

        # Populate state from account details
        self._state = {
            "balance": details.get("balance"),
            "nav": details.get("NAV"),
            "unrealizedPL": details.get("unrealizedPL"),
            "marginUsed": details.get("marginUsed"),
            "marginAvailable": details.get("marginAvailable"),
            "openTradeCount": details.get("openTradeCount"),
            "openPositionCount": details.get("openPositionCount"),
            "pl": details.get("pl"),
            "financing": details.get("financing"),
            "currency": details.get("currency"),
        }

        # Fetch and cache instrument specs
        instruments_list = self._client.get_instruments()
        self._instruments = {
            inst["name"]: inst for inst in instruments_list
        }

        # Persist transaction ID for restart recovery
        self._save_state()

        logger.info(
            "AccountManager initialized: balance=%s, NAV=%s, "
            "instruments=%d, lastTxnID=%s",
            self._state.get("balance"),
            self._state.get("nav"),
            len(self._instruments),
            self._last_transaction_id,
        )

    def sync(self) -> Dict[str, Any]:
        """Incrementally sync account state using sinceTransactionID.

        If no last_transaction_id is available (fresh start without
        prior state), falls back to initialize().

        Returns:
            Dict with 'changes' key containing what changed since last sync,
            and 'state' key containing the updated price-dependent state.
        """
        if not self._last_transaction_id:
            logger.info("No transaction ID available, performing full init")
            self.initialize()
            return {"changes": {}, "state": self._state}

        response = self._client.get_account_changes(
            self._last_transaction_id
        )

        # Update state from the price-dependent state object
        state_update = response.get("state", {})
        if state_update:
            for key in (
                "NAV", "unrealizedPL", "marginUsed", "marginAvailable",
            ):
                if key in state_update:
                    # Map API key names to our internal state keys
                    internal_key = {
                        "NAV": "nav",
                        "unrealizedPL": "unrealizedPL",
                        "marginUsed": "marginUsed",
                        "marginAvailable": "marginAvailable",
                    }.get(key, key)
                    self._state[internal_key] = state_update[key]

        # Update transaction ID
        new_txn_id = response.get("lastTransactionID")
        if new_txn_id:
            self._last_transaction_id = new_txn_id

        # Persist updated state
        self._save_state()

        changes = response.get("changes", {})
        logger.debug(
            "AccountManager synced: lastTxnID=%s, changes_keys=%s",
            self._last_transaction_id,
            list(changes.keys()) if isinstance(changes, dict) else "N/A",
        )

        return {"changes": changes, "state": self._state}

    # ------------------------------------------------------------------
    # Properties (read from self._state)
    # ------------------------------------------------------------------

    @property
    def balance(self) -> Optional[str]:
        """Current account balance."""
        return self._state.get("balance")

    @property
    def nav(self) -> Optional[str]:
        """Net Asset Value (balance + unrealized P&L)."""
        return self._state.get("nav")

    @property
    def unrealized_pl(self) -> Optional[str]:
        """Unrealized profit/loss on open trades."""
        return self._state.get("unrealizedPL")

    @property
    def margin_used(self) -> Optional[str]:
        """Margin currently in use."""
        return self._state.get("marginUsed")

    @property
    def margin_available(self) -> Optional[str]:
        """Margin available for new trades."""
        return self._state.get("marginAvailable")

    @property
    def open_trade_count(self) -> Optional[int]:
        """Number of currently open trades."""
        return self._state.get("openTradeCount")

    # ------------------------------------------------------------------
    # Instrument discovery
    # ------------------------------------------------------------------

    def get_instrument_spec(self, instrument: str) -> Optional[Dict[str, Any]]:
        """Get cached instrument specification.

        Args:
            instrument: Instrument name (e.g. 'EUR_USD').

        Returns:
            Instrument spec dict with pipLocation, displayPrecision,
            marginRate, etc. None if instrument not found.
        """
        return self._instruments.get(instrument)

    def get_tradeable_instruments(
        self, instrument_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get list of tradeable instruments, optionally filtered by type.

        Args:
            instrument_type: Optional filter: 'CURRENCY', 'CFD', 'METAL'.
                If None, returns all instruments.

        Returns:
            List of instrument spec dicts.
        """
        if instrument_type is None:
            return list(self._instruments.values())

        return [
            spec for spec in self._instruments.values()
            if spec.get("type") == instrument_type
        ]

    # ------------------------------------------------------------------
    # Market hours and session detection
    # ------------------------------------------------------------------

    def is_market_open(self, now: Optional[datetime] = None) -> bool:
        """Check if the forex market is currently open.

        Forex market hours: Sunday 5:00 PM ET to Friday 5:00 PM ET.
        The market is closed from Friday 5:00 PM ET to Sunday 5:00 PM ET.

        Args:
            now: Optional datetime for testing. If None, uses current time.

        Returns:
            True if the forex market is open.
        """
        if now is None:
            now = datetime.now(_ET)
        else:
            now = now.astimezone(_ET)

        weekday = now.weekday()  # Monday=0, Sunday=6
        hour = now.hour

        # Saturday: always closed
        if weekday == 5:
            return False

        # Sunday: open from 5 PM (17:00) onward
        if weekday == 6:
            return hour >= 17

        # Friday: open until 5 PM (17:00)
        if weekday == 4:
            return hour < 17

        # Monday through Thursday: always open
        return True

    def get_current_session(
        self, now: Optional[datetime] = None
    ) -> List[str]:
        """Identify which trading sessions are currently active.

        Sessions (all times in ET):
        - Sydney:   5:00 PM - 2:00 AM (Sun-Fri)
        - Tokyo:    7:00 PM - 4:00 AM
        - London:   3:00 AM - 12:00 PM
        - New_York: 8:00 AM - 5:00 PM

        Sessions can overlap. The London-New York overlap (8 AM - 12 PM ET)
        is the highest-volume trading window.

        Args:
            now: Optional datetime for testing. If None, uses current time.

        Returns:
            List of active session names (e.g. ['London', 'New_York']).
            Empty list if market is closed.
        """
        if not self.is_market_open(now):
            return []

        if now is None:
            now = datetime.now(_ET)
        else:
            now = now.astimezone(_ET)

        hour = now.hour
        sessions = []

        # Sydney: 5 PM - 2 AM ET (crosses midnight)
        if hour >= 17 or hour < 2:
            sessions.append("Sydney")

        # Tokyo: 7 PM - 4 AM ET (crosses midnight)
        if hour >= 19 or hour < 4:
            sessions.append("Tokyo")

        # London: 3 AM - 12 PM ET
        if 3 <= hour < 12:
            sessions.append("London")

        # New York: 8 AM - 5 PM ET
        if 8 <= hour < 17:
            sessions.append("New_York")

        return sessions

    def is_london_ny_overlap(
        self, now: Optional[datetime] = None
    ) -> bool:
        """Check if we are in the London-New York overlap window.

        The London-NY overlap (8:00 AM - 12:00 PM ET) is the prime
        trading window with highest volume and tightest spreads.
        This is the key session identified in PROJECT.md.

        Args:
            now: Optional datetime for testing. If None, uses current time.

        Returns:
            True if currently in the London-NY overlap window.
        """
        if not self.is_market_open(now):
            return False

        if now is None:
            now = datetime.now(_ET)
        else:
            now = now.astimezone(_ET)

        return 8 <= now.hour < 12

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Persist last_transaction_id to JSON file for restart recovery."""
        try:
            state_data = {
                "last_transaction_id": self._last_transaction_id,
                "saved_at": datetime.now(_ET).isoformat(),
            }
            with open(self._state_file, "w") as f:
                json.dump(state_data, f, indent=2)
        except OSError as e:
            logger.warning("Failed to save account state: %s", e)

    def _load_state(self) -> None:
        """Restore last_transaction_id from JSON file."""
        try:
            with open(self._state_file, "r") as f:
                state_data = json.load(f)
            self._last_transaction_id = state_data.get(
                "last_transaction_id"
            )
            if self._last_transaction_id:
                logger.info(
                    "Restored last_transaction_id=%s from %s",
                    self._last_transaction_id,
                    self._state_file,
                )
        except FileNotFoundError:
            pass  # Normal on first run
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load account state: %s", e)
