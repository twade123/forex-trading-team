"""
Calendar-based risk reduction: pre-event flattening, weekend closure,
and overnight position reduction.

Provides :class:`EventFlattener` which integrates with
:class:`NewsIntelligence` for event detection and
:class:`RiskProfileManager` for profile-specific timing thresholds.

The EventFlattener does **not** directly close positions -- it returns
recommendations for the orchestrator to act on.  All thresholds are
profile-driven so that Aggressive profiles can skip weekend close
while Conservative profiles flatten earlier.

Usage::

    from Source.event_flattener import EventFlattener

    ef = EventFlattener(risk_profile_manager, account_manager)
    result = ef.check_event_risk(news_data, instrument="EUR_USD")
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("trading_bot.event_flattener")

_ET = ZoneInfo("America/New_York")


class EventFlattener:
    """Manages calendar-based risk reduction: pre-event flattening,
    weekend closure, and overnight position reduction.

    Integrates with NewsIntelligence for event detection and
    RiskProfileManager for profile-specific timing thresholds.
    """

    def __init__(self, risk_profile_manager, account_manager) -> None:
        self._risk_profile_manager = risk_profile_manager
        self._account_manager = account_manager

    # ------------------------------------------------------------------
    # Pre-event risk (RMGT-07, RMGT-11)
    # ------------------------------------------------------------------

    def check_event_risk(
        self,
        news_data: dict,
        instrument: Optional[str] = None,
    ) -> dict:
        """Check upcoming event risk and recommend action.

        Args:
            news_data: Output from ``NewsIntelligence.monitor()`` or
                ``detect_events()`` -- must contain ``events``,
                ``high_impact_within_30min``, and
                ``high_impact_within_4h`` keys (either at top level
                or nested under ``events`` key from ``monitor()``).
            instrument: Optional instrument for relevance filtering.

        Returns:
            Dict with ``event_detected``, ``event_type``,
            ``minutes_until``, ``action``, ``for_open_trades``,
            ``for_new_trades``, ``spread_check``, and ``reason``.
        """
        profile = self._risk_profile_manager.get_active_profile()
        flatten_minutes = profile.event_flatten_minutes

        # Support both monitor() output and detect_events() output
        events_data = news_data.get("events", news_data)
        if isinstance(events_data, dict):
            events_list = events_data.get("events", [])
            within_30min = events_data.get("high_impact_within_30min", False)
            within_4h = events_data.get("high_impact_within_4h", False)
        else:
            events_list = []
            within_30min = news_data.get("high_impact_within_30min", False)
            within_4h = news_data.get("high_impact_within_4h", False)

        # Find the nearest high-impact event
        nearest_event = None
        for event in events_list:
            impact = event.get("impact", "medium")
            if impact in ("extreme", "high"):
                hours = event.get("hours_until", 999)
                minutes = hours * 60.0
                if minutes <= flatten_minutes:
                    if nearest_event is None or minutes < nearest_event["_min"]:
                        nearest_event = {
                            "name": event.get("name", "unknown"),
                            "_min": minutes,
                        }

        # Check for event within profile's flatten window
        if nearest_event is not None:
            minutes_until = nearest_event["_min"]
            event_type = nearest_event["name"]

            # Get open trade count
            open_count = self._open_trade_count()

            if open_count > 0:
                if within_30min:
                    action = "flatten"
                    for_open = "flatten"
                else:
                    action = "tighten"
                    for_open = "tighten_to_lock_profit"
            else:
                action = "block_entry"
                for_open = "normal"

            logger.warning(
                "Event risk: %s in %.0f min — action=%s",
                event_type, minutes_until, action,
            )

            return {
                "event_detected": True,
                "event_type": event_type,
                "minutes_until": int(minutes_until),
                "action": action,
                "for_open_trades": for_open,
                "for_new_trades": "block",
                "spread_check": False,
                "reason": (
                    f"{event_type} in {int(minutes_until)} min — "
                    f"{action} recommended"
                ),
            }

        # Also check within_30min flag even if no specific event in list
        if within_30min:
            open_count = self._open_trade_count()
            action = "flatten" if open_count > 0 else "block_entry"
            return {
                "event_detected": True,
                "event_type": "high_impact",
                "minutes_until": 30,
                "action": action,
                "for_open_trades": "flatten" if open_count > 0 else "normal",
                "for_new_trades": "block",
                "spread_check": False,
                "reason": "High-impact event within 30 min",
            }

        if within_4h:
            open_count = self._open_trade_count()
            action = "tighten" if open_count > 0 else "block_entry"
            return {
                "event_detected": True,
                "event_type": "high_impact",
                "minutes_until": 240,
                "action": action,
                "for_open_trades": (
                    "tighten_to_lock_profit" if open_count > 0 else "normal"
                ),
                "for_new_trades": "block",
                "spread_check": False,
                "reason": "High-impact event within 4 hours",
            }

        return {
            "event_detected": False,
            "event_type": None,
            "minutes_until": None,
            "action": "normal",
            "for_open_trades": "normal",
            "for_new_trades": "normal",
            "spread_check": False,
            "reason": "No imminent high-impact events",
        }

    # ------------------------------------------------------------------
    # Weekend risk (RMGT-12)
    # ------------------------------------------------------------------

    def check_weekend_risk(
        self, current_time: Optional[datetime] = None
    ) -> dict:
        """Check if weekend closure rules apply.

        Args:
            current_time: Optional ET datetime for testing. Defaults to now.

        Returns:
            Dict with ``action``, ``is_friday``,
            ``market_close_minutes``, and ``reason``.
        """
        profile = self._risk_profile_manager.get_active_profile()

        now = self._to_et(current_time)
        is_friday = now.weekday() == 4  # Monday=0, Friday=4

        if not profile.weekend_close:
            return {
                "action": "normal",
                "is_friday": is_friday,
                "market_close_minutes": None,
                "reason": "Weekend close disabled for this profile",
            }

        if not is_friday:
            return {
                "action": "normal",
                "is_friday": False,
                "market_close_minutes": None,
                "reason": "Not Friday — no weekend risk",
            }

        # Calculate minutes until 17:00 ET close
        close_hour = 17
        close_minute = 0
        now_minutes = now.hour * 60 + now.minute
        close_minutes = close_hour * 60 + close_minute
        minutes_until_close = close_minutes - now_minutes

        hour = now.hour
        minute = now.minute
        current_minutes = hour * 60 + minute

        # Friday 16:30+ ET -> close_all
        if current_minutes >= 16 * 60 + 30:
            logger.warning(
                "Friday %02d:%02d ET — past 16:30, close all positions",
                hour, minute,
            )
            return {
                "action": "close_all",
                "is_friday": True,
                "market_close_minutes": max(minutes_until_close, 0),
                "reason": "Friday after 16:30 ET — close all before weekend",
            }

        # Friday 16:00-16:30 ET -> no_new_trades
        if current_minutes >= 16 * 60:
            logger.info(
                "Friday %02d:%02d ET — past 16:00, no new trades",
                hour, minute,
            )
            return {
                "action": "no_new_trades",
                "is_friday": True,
                "market_close_minutes": max(minutes_until_close, 0),
                "reason": "Friday after 16:00 ET — no new trades before weekend",
            }

        # Friday before 16:00 -> normal
        return {
            "action": "normal",
            "is_friday": True,
            "market_close_minutes": max(minutes_until_close, 0),
            "reason": "Friday before 16:00 ET — normal trading",
        }

    # ------------------------------------------------------------------
    # Overnight risk (RMGT-12)
    # ------------------------------------------------------------------

    def check_overnight_risk(
        self, current_time: Optional[datetime] = None
    ) -> dict:
        """Check if current time is during lower-liquidity hours.

        Peak liquidity is during the London-NY overlap (8:00-12:00 ET).
        Outside this window, positions are reduced per profile setting.

        Args:
            current_time: Optional ET datetime for testing. Defaults to now.

        Returns:
            Dict with ``is_overnight``, ``size_multiplier``,
            ``session``, and ``reason``.
        """
        profile = self._risk_profile_manager.get_active_profile()
        overnight_mult = profile.overnight_size_mult

        now = self._to_et(current_time)
        hour = now.hour

        # Session classification (all times ET):
        # Prime overlap:  8:00 - 12:00  (London + New York)
        # New York:      12:00 - 17:00  (NY only)
        # London:         3:00 -  8:00  (London pre-overlap)
        # Asian:         19:00 -  3:00  (Tokyo/Sydney, crosses midnight)
        # Off hours:     17:00 - 19:00  (between NY close and Asian open)

        if 8 <= hour < 12:
            session = "prime_overlap"
            size_mult = 1.0
            is_overnight = False
            reason = "London-NY overlap — prime liquidity"
        elif 12 <= hour < 17:
            session = "new_york"
            size_mult = 1.0
            is_overnight = False
            reason = "New York session — good liquidity"
        elif 3 <= hour < 8:
            session = "london"
            size_mult = 1.0
            is_overnight = False
            reason = "London session — good liquidity"
        elif hour >= 19 or hour < 3:
            session = "asian"
            size_mult = overnight_mult
            is_overnight = True
            reason = (
                f"Asian session — reduced liquidity, "
                f"size mult {overnight_mult:.2f}"
            )
        else:
            # 17:00 - 19:00
            session = "off_hours"
            size_mult = overnight_mult
            is_overnight = True
            reason = (
                f"Off hours (17-19 ET) — lowest liquidity, "
                f"size mult {overnight_mult:.2f}"
            )

        return {
            "is_overnight": is_overnight,
            "size_multiplier": size_mult,
            "session": session,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # Combined check
    # ------------------------------------------------------------------

    def get_combined_event_adjustment(
        self,
        news_data: dict,
        current_time: Optional[datetime] = None,
    ) -> dict:
        """Combine event, weekend, and overnight checks.

        Returns the most restrictive action and lowest size_multiplier.

        Args:
            news_data: Output from ``NewsIntelligence.monitor()`` or
                ``detect_events()``.
            current_time: Optional datetime for testing.

        Returns:
            Dict with ``trading_allowed``, ``size_multiplier``,
            ``actions``, ``reasons``, and ``should_flatten``.
        """
        event = self.check_event_risk(news_data)
        weekend = self.check_weekend_risk(current_time)
        overnight = self.check_overnight_risk(current_time)

        actions: List[str] = []
        reasons: List[str] = []

        # Collect actions
        if event["action"] != "normal":
            actions.append(event["action"])
            reasons.append(event["reason"])
        if weekend["action"] != "normal":
            actions.append(weekend["action"])
            reasons.append(weekend["reason"])
        if overnight["is_overnight"]:
            actions.append(f"overnight_reduction({overnight['size_multiplier']})")
            reasons.append(overnight["reason"])

        # Determine should_flatten
        flatten_actions = {"flatten", "close_all"}
        should_flatten = any(a in flatten_actions for a in actions)

        # Determine trading_allowed
        blocking_actions = {"flatten", "close_all", "block_entry"}
        trading_blocked = any(a in blocking_actions for a in actions)
        # Weekend blocks
        if weekend["action"] in ("close_all", "no_new_trades"):
            trading_blocked = True

        # Minimum size multiplier across all checks
        multipliers = [1.0]
        if event["action"] in ("flatten", "block_entry", "close_all"):
            multipliers.append(0.0)
        if weekend["action"] in ("close_all", "no_new_trades"):
            multipliers.append(0.0)
        multipliers.append(overnight["size_multiplier"])

        size_mult = min(multipliers)

        if not actions:
            actions.append("normal")
            reasons.append("No event, weekend, or overnight restrictions")

        return {
            "trading_allowed": not trading_blocked,
            "size_multiplier": size_mult,
            "actions": actions,
            "reasons": reasons,
            "should_flatten": should_flatten,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_trade_count(self) -> int:
        """Get the current number of open trades."""
        count = self._account_manager.open_trade_count
        if count is not None:
            return int(count)
        return 0

    @staticmethod
    def _to_et(dt: Optional[datetime] = None) -> datetime:
        """Convert datetime to ET or return current ET time."""
        if dt is None:
            return datetime.now(_ET)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_ET)
        return dt.astimezone(_ET)
