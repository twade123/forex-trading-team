"""
Config-driven market profile system for scheduling and market-hours queries.

Provides the MarketProfile class which loads market definitions from YAML
and answers questions about market hours, sessions, prime windows, and
per-timeframe scheduling. Supports forex (24/5), crypto (24/7), futures
(exchange hours with daily settlement gap), and any future market type
defined by agents via YAML.

Usage:
    from Source.market_profile import MarketProfile

    # Load by market type (looks in Config/market_profiles/)
    fx = MarketProfile.from_market_type('forex')

    # Query market state
    if fx.is_market_open():
        sessions = fx.get_current_sessions()
        if fx.is_prime_window():
            print("Prime trading window!")

    # Get scheduling config for a timeframe
    m15_sched = fx.get_schedule_for_timeframe('M15')
    # {'cron_minutes': '0,15,30,45', 'position_monitor_offset': 7}
"""

import copy
import logging
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger("trading.market_profile")

# Day name -> weekday number (Monday=0, Sunday=6)
_DAY_MAP = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Default profiles directory relative to this file
_DEFAULT_PROFILES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Config",
    "market_profiles",
)

# Required top-level keys in a profile
_REQUIRED_KEYS = {"market_type", "hours", "sessions", "scheduling"}


class MarketProfile:
    """Config-driven market definition loaded from YAML.

    Answers market-hours, session, and scheduling queries for any market
    type. Profiles can be created by agents writing YAML files -- no code
    changes required.

    Args:
        profile_path: Path to a YAML profile file.
    """

    def __init__(self, profile_path: str) -> None:
        path = Path(profile_path)
        if not path.exists():
            raise FileNotFoundError(f"Profile not found: {profile_path}")

        with open(path, "r") as f:
            config = yaml.safe_load(f)

        self._validate(config)
        self._config: Dict[str, Any] = config
        self._path: str = str(path)
        self._tz = ZoneInfo(config["hours"]["timezone"])

        logger.info(
            "MarketProfile loaded: %s (%s)",
            self._config["market_type"],
            self._path,
        )

    @classmethod
    def from_market_type(
        cls, market_type: str, profiles_dir: Optional[str] = None
    ) -> "MarketProfile":
        """Load a profile by market type name.

        Looks for ``{market_type}.yaml`` in the profiles directory.

        Args:
            market_type: Profile identifier (e.g., 'forex', 'crypto').
            profiles_dir: Optional custom directory. Defaults to
                ``Config/market_profiles/`` relative to the package.

        Returns:
            MarketProfile instance.

        Raises:
            FileNotFoundError: If the profile YAML does not exist.
        """
        directory = profiles_dir or _DEFAULT_PROFILES_DIR
        path = os.path.join(directory, f"{market_type}.yaml")
        return cls(path)

    @classmethod
    def from_dict(cls, data: dict) -> "MarketProfile":
        """Create a MarketProfile from a dict (for agent-generated profiles).

        The dict must follow the schema in ``_schema.yaml``.

        Args:
            data: Profile configuration dict.

        Returns:
            MarketProfile instance (not backed by a file).
        """
        instance = object.__new__(cls)
        cls._validate(data)
        instance._config = copy.deepcopy(data)
        instance._path = None
        instance._tz = ZoneInfo(data["hours"]["timezone"])
        logger.info(
            "MarketProfile created from dict: %s",
            data.get("market_type", "unknown"),
        )
        return instance

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def market_type(self) -> str:
        """Unique market type identifier."""
        return self._config["market_type"]

    @property
    def display_name(self) -> str:
        """Human-readable market name."""
        return self._config.get("display_name", self.market_type)

    @property
    def is_continuous(self) -> bool:
        """True if market trades 24/7 with no close (e.g., crypto)."""
        return bool(self._config["hours"].get("continuous", False))

    # ------------------------------------------------------------------
    # Market hours queries
    # ------------------------------------------------------------------

    def is_market_open(self, now: Optional[datetime] = None) -> bool:
        """Check if the market is currently open.

        - Continuous markets (crypto): always True.
        - Non-continuous: checks weekly open/close window and daily gap.

        Args:
            now: Optional datetime for deterministic testing.
                If None, uses current time in the profile's timezone.

        Returns:
            True if the market is open for trading.
        """
        if self.is_continuous:
            return True

        now = self._resolve_time(now)

        # Check weekly open/close window
        if not self._is_in_weekly_window(now):
            return False

        # Check daily settlement gap (futures)
        if self.is_in_daily_gap(now):
            return False

        return True

    def is_weekend(self, now: Optional[datetime] = None) -> bool:
        """Check if the market is in its weekend closed period.

        For continuous markets, always returns False.

        Args:
            now: Optional datetime for deterministic testing.

        Returns:
            True if between weekly_close and weekly_open.
        """
        if self.is_continuous:
            return False

        now = self._resolve_time(now)
        return not self._is_in_weekly_window(now)

    def next_open(self, now: Optional[datetime] = None) -> datetime:
        """Get the next market open time.

        If the market is currently open, returns the current time.

        Args:
            now: Optional datetime for deterministic testing.

        Returns:
            Datetime of the next market open (or now if already open).
        """
        if self.is_continuous:
            return self._resolve_time(now)

        now = self._resolve_time(now)

        if self.is_market_open(now):
            return now

        # Find next weekly open
        hours = self._config["hours"]
        open_day = _DAY_MAP[hours["weekly_open"]["day"].lower()]
        open_hour = hours["weekly_open"]["hour"]
        open_minute = hours["weekly_open"].get("minute", 0)

        # Start from current day and scan forward up to 7 days
        for offset in range(8):
            candidate = now + timedelta(days=offset)
            if candidate.weekday() == open_day:
                open_dt = candidate.replace(
                    hour=open_hour, minute=open_minute, second=0, microsecond=0
                )
                if open_dt > now:
                    return open_dt

        # Fallback: shouldn't reach here
        logger.warning("next_open fallback reached")
        return now + timedelta(days=7)

    def next_close(self, now: Optional[datetime] = None) -> datetime:
        """Get the next market close time.

        Args:
            now: Optional datetime for deterministic testing.

        Returns:
            Datetime of the next weekly close.
        """
        if self.is_continuous:
            # No close for continuous markets -- return far future
            return self._resolve_time(now) + timedelta(days=365 * 100)

        now = self._resolve_time(now)

        hours = self._config["hours"]
        close_day = _DAY_MAP[hours["weekly_close"]["day"].lower()]
        close_hour = hours["weekly_close"]["hour"]
        close_minute = hours["weekly_close"].get("minute", 0)

        # Find next close from now
        for offset in range(8):
            candidate = now + timedelta(days=offset)
            if candidate.weekday() == close_day:
                close_dt = candidate.replace(
                    hour=close_hour, minute=close_minute,
                    second=0, microsecond=0,
                )
                if close_dt > now:
                    return close_dt

        logger.warning("next_close fallback reached")
        return now + timedelta(days=7)

    def is_in_daily_gap(self, now: Optional[datetime] = None) -> bool:
        """Check if currently in a daily settlement gap (e.g., futures).

        Returns False if the profile has no daily_gap defined.

        Args:
            now: Optional datetime for deterministic testing.

        Returns:
            True if in the daily settlement gap.
        """
        gap = self._config["hours"].get("daily_gap")
        if not gap:
            return False

        now = self._resolve_time(now)

        # Check if today is a gap day
        gap_days = [_DAY_MAP[d.lower()] for d in gap.get("days", [])]
        if now.weekday() not in gap_days:
            return False

        close_time = time(gap["close_hour"], gap.get("close_minute", 0))
        reopen_time = time(gap["reopen_hour"], gap.get("reopen_minute", 0))
        current_time = now.time().replace(second=0, microsecond=0)

        # Gap is between close and reopen (same day, close < reopen)
        return close_time <= current_time < reopen_time

    # ------------------------------------------------------------------
    # Session queries
    # ------------------------------------------------------------------

    def get_current_sessions(
        self, now: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Get list of currently active trading sessions.

        Handles cross-midnight sessions (e.g., Sydney 17:00-02:00).

        Args:
            now: Optional datetime for deterministic testing.

        Returns:
            List of session dicts that are currently active.
            Empty list if no sessions are active.
        """
        now = self._resolve_time(now)
        current_hour = now.hour
        current_minute = now.minute
        current_total = current_hour * 60 + current_minute

        active = []
        for session in self._config.get("sessions", []):
            start_total = session["start_hour"] * 60 + session.get(
                "start_minute", 0
            )
            end_total = session["end_hour"] * 60 + session.get(
                "end_minute", 0
            )

            if start_total < end_total:
                # Same-day session (e.g., London 3:00-12:00)
                if start_total <= current_total < end_total:
                    active.append(session)
            else:
                # Cross-midnight session (e.g., Sydney 17:00-02:00)
                if current_total >= start_total or current_total < end_total:
                    active.append(session)

        return active

    def is_prime_window(self, now: Optional[datetime] = None) -> bool:
        """Check if currently in the prime trading window.

        Args:
            now: Optional datetime for deterministic testing.

        Returns:
            True if within the prime trading window defined in the profile.
        """
        prime = self._config.get("prime_window")
        if not prime:
            return False

        now = self._resolve_time(now)
        current_total = now.hour * 60 + now.minute

        start_total = prime["start_hour"] * 60 + prime.get("start_minute", 0)
        end_total = prime["end_hour"] * 60 + prime.get("end_minute", 0)

        if start_total < end_total:
            return start_total <= current_total < end_total
        else:
            # Cross-midnight prime window
            return current_total >= start_total or current_total < end_total

    def get_all_sessions(self) -> List[Dict[str, Any]]:
        """Return all sessions defined in the profile.

        Returns:
            List of all session dicts.
        """
        return list(self._config.get("sessions", []))

    # ------------------------------------------------------------------
    # Scheduling queries
    # ------------------------------------------------------------------

    def get_schedule_for_timeframe(self, timeframe: str) -> Dict[str, Any]:
        """Get the scheduling config for a given timeframe.

        Args:
            timeframe: Oanda granularity code (M15, H1, H4, D, etc.).

        Returns:
            Dict with cron scheduling fields for the timeframe.

        Raises:
            ValueError: If the timeframe is not configured in this profile.
        """
        scheduling = self._config.get("scheduling", {})
        if timeframe not in scheduling:
            available = list(scheduling.keys())
            raise ValueError(
                f"Timeframe '{timeframe}' not configured in {self.market_type} "
                f"profile. Available: {available}"
            )
        return dict(scheduling[timeframe])

    def get_available_timeframes(self) -> List[str]:
        """Return list of configured timeframes.

        Returns:
            List of timeframe strings (e.g., ['M15', 'H1', 'H4', 'D']).
        """
        return list(self._config.get("scheduling", {}).keys())

    def get_news_config(self) -> Dict[str, Any]:
        """Return the news monitoring config.

        Returns:
            Dict with interval_minutes and high_impact_events.
        """
        return dict(self._config.get("news_monitoring", {}))

    def get_daily_report_config(self) -> Dict[str, Any]:
        """Return the daily report config.

        Returns:
            Dict with hour, minute, timezone, and days fields.
        """
        return dict(self._config.get("daily_report", {}))

    # ------------------------------------------------------------------
    # Profile management (for agent updates)
    # ------------------------------------------------------------------

    def save(self, path: Optional[str] = None) -> None:
        """Write the current config to YAML.

        Used by orchestrator agents when updating a profile after market
        research.

        Args:
            path: Optional target path. Defaults to the original load path.

        Raises:
            ValueError: If no path available (created from dict without save).
        """
        target = path or self._path
        if not target:
            raise ValueError(
                "No save path available. Provide a path argument or use "
                "a profile loaded from file."
            )

        with open(target, "w") as f:
            yaml.dump(
                self._config, f, default_flow_style=False, sort_keys=False
            )

        self._path = target
        logger.info("MarketProfile saved: %s -> %s", self.market_type, target)

    def update(self, updates: dict) -> None:
        """Deep-merge updates into the profile config.

        Allows partial updates. For example, an agent can add a session:
        ``profile.update({"sessions": [...existing, new_session]})``

        Args:
            updates: Dict of fields to merge into the config.
        """
        self._deep_merge(self._config, updates)
        # Re-validate after update
        self._validate(self._config)
        # Refresh timezone in case hours.timezone changed
        self._tz = ZoneInfo(self._config["hours"]["timezone"])
        logger.info("MarketProfile updated: %s", self.market_type)

    def to_dict(self) -> Dict[str, Any]:
        """Return the full profile config as a dict.

        Returns:
            Deep copy of the profile configuration.
        """
        return copy.deepcopy(self._config)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_time(self, now: Optional[datetime] = None) -> datetime:
        """Convert now to the profile's timezone, defaulting to current time."""
        if now is None:
            return datetime.now(self._tz)
        return now.astimezone(self._tz)

    def _is_in_weekly_window(self, now: datetime) -> bool:
        """Check if now is within the weekly open/close window.

        For forex: Sun 17:00 ET (open) to Fri 17:00 ET (close).
        The open-to-close window wraps around the weekend.
        """
        hours = self._config["hours"]
        open_cfg = hours.get("weekly_open", {})
        close_cfg = hours.get("weekly_close", {})

        if not open_cfg or not close_cfg:
            return True  # No open/close defined = always open

        open_day = _DAY_MAP[open_cfg["day"].lower()]
        open_hour = open_cfg["hour"]
        open_minute = open_cfg.get("minute", 0)

        close_day = _DAY_MAP[close_cfg["day"].lower()]
        close_hour = close_cfg["hour"]
        close_minute = close_cfg.get("minute", 0)

        weekday = now.weekday()
        current_time = now.hour * 60 + now.minute

        open_time = open_hour * 60 + open_minute
        close_time = close_hour * 60 + close_minute

        # Convert to minutes-since-Monday-midnight for comparison
        now_mins = weekday * 1440 + current_time
        open_mins = open_day * 1440 + open_time
        close_mins = close_day * 1440 + close_time

        # The weekly window wraps around the week boundary
        # (e.g., Sun 17:00 to Fri 17:00)
        if open_mins > close_mins:
            # Wraps: open is Sun, close is Fri
            # In window if: now >= open_mins OR now < close_mins
            return now_mins >= open_mins or now_mins < close_mins
        else:
            # No wrap: open < close
            return open_mins <= now_mins < close_mins

    @staticmethod
    def _validate(config: dict) -> None:
        """Validate required fields are present in config."""
        if not isinstance(config, dict):
            raise ValueError("Profile config must be a dict")

        missing = _REQUIRED_KEYS - set(config.keys())
        if missing:
            raise ValueError(f"Profile missing required keys: {missing}")

        hours = config["hours"]
        if not isinstance(hours, dict):
            raise ValueError("'hours' must be a dict")
        if "timezone" not in hours:
            raise ValueError("'hours.timezone' is required")

    @staticmethod
    def _deep_merge(base: dict, updates: dict) -> None:
        """Recursively merge updates into base dict (in-place)."""
        for key, value in updates.items():
            if (
                key in base
                and isinstance(base[key], dict)
                and isinstance(value, dict)
            ):
                MarketProfile._deep_merge(base[key], value)
            else:
                base[key] = value
