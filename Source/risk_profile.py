"""
Risk profile management with named presets and auto-adjustment.

Provides :class:`RiskProfile` (immutable parameter snapshot) and
:class:`RiskProfileManager` (preset selection, persistence, and
consecutive-loss/win auto-adjustment).

Named presets are loaded from ``risk_profiles.yaml``.  Tim switches
profiles via :meth:`set_profile` and the bot auto-reduces risk after
consecutive losses (RISK-06).  Auto-adjustment **never** escalates
beyond the profile Tim selected -- only he can increase risk (RISK-07).

Usage:
    from trading_bot.source.risk_profile import RiskProfileManager

    rpm = RiskProfileManager()
    profile = rpm.get_active_profile()    # RiskProfile(name="normal", ...)
    limits = rpm.get_risk_limits()        # dict for TradeValidator
    rpm.set_profile("conservative")
    rpm.record_trade_result("loss")
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger("trading_bot.risk_profile")

_ET = ZoneInfo("America/New_York")

# Fields that define a RiskProfile (order must match dataclass)
_PROFILE_FIELDS = [
    "risk_pct", "min_confluence", "max_concurrent_trades",
    "max_daily_loss_pct", "max_portfolio_heat_pct", "atr_multiplier",
    "min_rr_ratio", "overnight_size_mult", "weekend_close",
    "event_flatten_minutes", "max_correlation_group_positions",
    "description",
]


@dataclass(frozen=True)
class RiskProfile:
    """Immutable snapshot of risk parameters for a single profile level."""

    name: str
    risk_pct: float
    min_confluence: int
    max_concurrent_trades: int
    max_daily_loss_pct: float
    max_portfolio_heat_pct: float
    atr_multiplier: float
    min_rr_ratio: float
    overnight_size_mult: float
    weekend_close: bool
    event_flatten_minutes: int
    max_correlation_group_positions: int
    description: str

    @classmethod
    def from_profile_name(cls, name: str) -> "RiskProfile":
        """Load a named profile from the default risk_profiles.yaml.

        Convenience constructor that creates a temporary
        :class:`RiskProfileManager`, switches to *name*, and returns
        the :class:`RiskProfile` snapshot.

        Args:
            name: Profile name (e.g. ``"conservative"``).

        Returns:
            A :class:`RiskProfile` for the requested preset.

        Raises:
            KeyError: If *name* is not in the YAML config.
        """
        manager = RiskProfileManager()
        manager.set_profile(name)
        return manager.get_active_profile()


class RiskProfileManager:
    """Manages risk profile selection, persistence, and auto-adjustment.

    Loads named presets from YAML config.  Provides a command interface
    for Tim to switch profiles.  Persists the active profile across
    restarts.  Auto-reduces risk after consecutive losses; auto-restores
    after consecutive wins.

    Auto-adjustment **never** escalates risk -- only Tim can increase it.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        state_file: Optional[str] = None,
    ) -> None:
        if config_path is None:
            config_path = self._default_config_path()
        if state_file is None:
            state_file = self._default_state_path()

        self._config_path = config_path
        self._state_file = state_file

        # Parse YAML
        self._raw: Dict[str, Any] = {}
        self._profiles: Dict[str, RiskProfile] = {}
        self._absolute_limits: Dict[str, Any] = {}
        self._custom_bounds: Dict[str, List] = {}
        self._auto_adjustment: Dict[str, Any] = {}
        self._default_name: str = "normal"

        self._load_config(config_path)

        # Runtime state
        self._active_name: str = self._default_name
        self._auto_reduced: bool = False
        self._auto_reduced_from: Optional[str] = None
        self._consecutive_wins: int = 0
        self._consecutive_losses: int = 0
        self._custom_profile: Optional[RiskProfile] = None

        # Restore persisted state (overrides defaults above)
        self._load_state()

    # ------------------------------------------------------------------
    # Config / state paths
    # ------------------------------------------------------------------

    @staticmethod
    def _default_config_path() -> str:
        source_dir = Path(__file__).resolve().parent
        return str(source_dir.parent / "Config" / "risk_profiles.yaml")

    @staticmethod
    def _default_state_path() -> str:
        source_dir = Path(__file__).resolve().parent
        return str(source_dir / ".risk_state.json")

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_config(self, config_path: str) -> None:
        """Parse YAML and build RiskProfile objects for each named preset."""
        try:
            with open(config_path, "r") as f:
                self._raw = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.error("Risk profiles config not found: %s", config_path)
            return
        except yaml.YAMLError as exc:
            logger.error("YAML parse error in %s: %s", config_path, exc)
            return

        self._default_name = self._raw.get("default_profile", "normal")
        self._absolute_limits = self._raw.get("absolute_limits", {})
        self._custom_bounds = self._raw.get("custom_bounds", {})
        self._auto_adjustment = self._raw.get("auto_adjustment", {})

        for name, params in self._raw.get("profiles", {}).items():
            self._profiles[name] = RiskProfile(
                name=name,
                risk_pct=float(params.get("risk_pct", 0.01)),
                min_confluence=int(params.get("min_confluence", 30)),
                max_concurrent_trades=int(params.get("max_concurrent_trades", 3)),
                max_daily_loss_pct=float(params.get("max_daily_loss_pct", 5.0)),
                max_portfolio_heat_pct=float(params.get("max_portfolio_heat_pct", 5.0)),
                atr_multiplier=float(params.get("atr_multiplier", 1.5)),
                min_rr_ratio=float(params.get("min_rr_ratio", 2.0)),
                overnight_size_mult=float(params.get("overnight_size_mult", 0.75)),
                weekend_close=bool(params.get("weekend_close", True)),
                event_flatten_minutes=int(params.get("event_flatten_minutes", 30)),
                max_correlation_group_positions=int(
                    params.get("max_correlation_group_positions", 1)
                ),
                description=str(params.get("description", "")),
            )

        logger.info(
            "Loaded %d risk profiles from %s (default: %s)",
            len(self._profiles), config_path, self._default_name,
        )

    # ------------------------------------------------------------------
    # State persistence (mirrors AccountManager._save_state/_load_state)
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Persist active profile and adjustment counters to JSON."""
        try:
            state = {
                "active_profile": self._active_name,
                "auto_reduced": self._auto_reduced,
                "auto_reduced_from": self._auto_reduced_from,
                "consecutive_wins": self._consecutive_wins,
                "consecutive_losses": self._consecutive_losses,
                "custom_params": (
                    asdict(self._custom_profile)
                    if self._custom_profile is not None
                    else None
                ),
                "saved_at": datetime.now(_ET).isoformat(),
            }
            with open(self._state_file, "w") as f:
                json.dump(state, f, indent=2)
        except OSError as e:
            logger.warning("Failed to save risk state: %s", e)

    def _load_state(self) -> None:
        """Restore persisted state from JSON file."""
        try:
            with open(self._state_file, "r") as f:
                state = json.load(f)
        except FileNotFoundError:
            return  # Normal on first run
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load risk state: %s", e)
            return

        restored_name = state.get("active_profile", self._default_name)

        # Restore custom profile if it was active
        custom_params = state.get("custom_params")
        if restored_name == "custom" and custom_params is not None:
            self._custom_profile = RiskProfile(**custom_params)
            self._profiles["custom"] = self._custom_profile

        if restored_name in self._profiles:
            self._active_name = restored_name
        else:
            logger.warning(
                "Persisted profile '%s' not found, using default '%s'",
                restored_name, self._default_name,
            )
            self._active_name = self._default_name

        self._auto_reduced = state.get("auto_reduced", False)
        self._auto_reduced_from = state.get("auto_reduced_from")
        self._consecutive_wins = state.get("consecutive_wins", 0)
        self._consecutive_losses = state.get("consecutive_losses", 0)

        logger.info(
            "Restored risk state: profile=%s, auto_reduced=%s",
            self._active_name, self._auto_reduced,
        )

    # ------------------------------------------------------------------
    # Profile access
    # ------------------------------------------------------------------

    def get_active_profile(self) -> RiskProfile:
        """Return the currently active :class:`RiskProfile`."""
        return self._profiles[self._active_name]

    def set_profile(self, name: str) -> dict:
        """Switch to the named profile.

        Resets consecutive counters (fresh start at new level) and
        persists the change.

        Args:
            name: One of the named presets (e.g. ``"conservative"``).

        Returns:
            ``{success, profile, parameters, message}`` on success or
            ``{success: False, error: ...}`` on failure.
        """
        if name not in self._profiles:
            available = list(self._profiles.keys())
            return {
                "success": False,
                "error": f"Unknown profile: '{name}'. Available: {available}",
            }

        self._active_name = name
        self._auto_reduced = False
        self._auto_reduced_from = None
        self._consecutive_wins = 0
        self._consecutive_losses = 0
        self._save_state()

        profile = self._profiles[name]
        logger.info("Switched to risk profile '%s'", name)
        return {
            "success": True,
            "profile": name,
            "parameters": asdict(profile),
            "message": f"Risk profile set to '{name}': {profile.description}",
        }

    def set_custom_profile(self, params: dict) -> dict:
        """Create and activate a custom :class:`RiskProfile`.

        All fields are validated against ``custom_bounds`` and
        ``absolute_limits`` from the YAML config.

        Args:
            params: Dict of profile parameters to set.

        Returns:
            ``{success, profile, parameters, message}`` or
            ``{success: False, errors: [...]}`` with validation issues.
        """
        errors = []

        # Fill defaults from normal profile
        normal = self._profiles.get("normal") or self._profiles.get(
            self._default_name
        )
        if normal is None:
            return {"success": False, "errors": ["No base profile found"]}

        base = asdict(normal)
        base.update(params)
        base["name"] = "custom"

        # Validate against custom_bounds
        for field, bounds in self._custom_bounds.items():
            if field in base and len(bounds) == 2:
                lo, hi = bounds
                val = base[field]
                if isinstance(val, (int, float)) and not (lo <= val <= hi):
                    errors.append(
                        f"{field}={val} out of bounds [{lo}, {hi}]"
                    )

        # Validate against absolute_limits
        abs_lim = self._absolute_limits
        if base.get("risk_pct", 0) > abs_lim.get("max_risk_pct", 1.0):
            errors.append(
                f"risk_pct={base['risk_pct']} exceeds absolute max "
                f"{abs_lim['max_risk_pct']}"
            )
        if base.get("risk_pct", 1) < abs_lim.get("min_risk_pct", 0):
            errors.append(
                f"risk_pct={base['risk_pct']} below absolute min "
                f"{abs_lim['min_risk_pct']}"
            )
        if base.get("max_concurrent_trades", 0) > abs_lim.get(
            "max_concurrent_trades", 999
        ):
            errors.append(
                f"max_concurrent_trades={base['max_concurrent_trades']} "
                f"exceeds absolute max {abs_lim['max_concurrent_trades']}"
            )
        if base.get("min_confluence", 100) < abs_lim.get("min_confluence", 0):
            errors.append(
                f"min_confluence={base['min_confluence']} below absolute min "
                f"{abs_lim['min_confluence']}"
            )

        if errors:
            return {"success": False, "errors": errors}

        # Build custom profile
        try:
            custom = RiskProfile(
                name="custom",
                risk_pct=float(base["risk_pct"]),
                min_confluence=int(base["min_confluence"]),
                max_concurrent_trades=int(base["max_concurrent_trades"]),
                max_daily_loss_pct=float(base["max_daily_loss_pct"]),
                max_portfolio_heat_pct=float(base["max_portfolio_heat_pct"]),
                atr_multiplier=float(base["atr_multiplier"]),
                min_rr_ratio=float(base["min_rr_ratio"]),
                overnight_size_mult=float(base["overnight_size_mult"]),
                weekend_close=bool(base["weekend_close"]),
                event_flatten_minutes=int(base["event_flatten_minutes"]),
                max_correlation_group_positions=int(
                    base["max_correlation_group_positions"]
                ),
                description=str(base.get("description", "Custom profile")),
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "errors": [str(exc)]}

        self._custom_profile = custom
        self._profiles["custom"] = custom
        self._active_name = "custom"
        self._auto_reduced = False
        self._auto_reduced_from = None
        self._consecutive_wins = 0
        self._consecutive_losses = 0
        self._save_state()

        logger.info("Activated custom risk profile: %s", asdict(custom))
        return {
            "success": True,
            "profile": "custom",
            "parameters": asdict(custom),
            "message": "Custom risk profile activated",
        }

    # ------------------------------------------------------------------
    # Auto-adjustment (RISK-06, RISK-07)
    # ------------------------------------------------------------------

    def record_trade_result(self, result: str) -> None:
        """Record a trade outcome and apply auto-adjustment if needed.

        Args:
            result: ``"win"`` or ``"loss"``.
        """
        reduction_order = self._auto_adjustment.get(
            "reduction_order", ["aggressive", "normal", "conservative"]
        )
        losses_threshold = self._auto_adjustment.get(
            "consecutive_losses_to_reduce", 3
        )
        wins_threshold = self._auto_adjustment.get(
            "consecutive_wins_to_restore", 3
        )

        if result == "loss":
            self._consecutive_losses += 1
            self._consecutive_wins = 0

            # Auto-reduce if at threshold and not already at lowest
            if self._consecutive_losses >= losses_threshold:
                self._try_auto_reduce(reduction_order)

        elif result == "win":
            self._consecutive_wins += 1
            self._consecutive_losses = 0

            # Auto-restore if at threshold and currently auto-reduced
            if (
                self._auto_reduced
                and self._consecutive_wins >= wins_threshold
            ):
                self._auto_restore()

        self._save_state()

    def _try_auto_reduce(self, reduction_order: List[str]) -> None:
        """Attempt to reduce risk one level in the reduction order."""
        if self._active_name not in reduction_order:
            return

        current_idx = reduction_order.index(self._active_name)
        next_idx = current_idx + 1

        if next_idx >= len(reduction_order):
            # Already at lowest level
            logger.info(
                "Already at lowest risk level '%s'; no further reduction",
                self._active_name,
            )
            return

        target = reduction_order[next_idx]
        if target not in self._profiles:
            logger.warning(
                "Auto-reduce target '%s' not found in profiles", target
            )
            return

        if not self._auto_reduced:
            self._auto_reduced_from = self._active_name
        self._auto_reduced = True
        self._active_name = target
        self._consecutive_losses = 0  # Reset counter after reduction

        logger.warning(
            "Auto-reduced risk: '%s' -> '%s' after consecutive losses "
            "(original: '%s')",
            self._auto_reduced_from, target, self._auto_reduced_from,
        )

    def _auto_restore(self) -> None:
        """Restore risk level to pre-reduction level after consecutive wins."""
        if self._auto_reduced_from is None:
            return
        if self._auto_reduced_from not in self._profiles:
            logger.warning(
                "Restore target '%s' not found", self._auto_reduced_from
            )
            return

        restored = self._auto_reduced_from
        self._active_name = restored
        self._auto_reduced = False
        self._auto_reduced_from = None
        self._consecutive_wins = 0
        self._consecutive_losses = 0

        logger.info("Auto-restored risk level to '%s' after consecutive wins", restored)

    # ------------------------------------------------------------------
    # Bridge to TradeValidator
    # ------------------------------------------------------------------

    def get_risk_limits(self) -> dict:
        """Return dict compatible with TradeValidator.validate_pre_trade().

        Maps active profile parameters to the risk_limits dict that
        :class:`TradeValidator` expects.
        """
        profile = self.get_active_profile()
        return {
            "min_confluence": profile.min_confluence,
            "min_rr_ratio": profile.min_rr_ratio,
            "max_daily_loss_pct": profile.max_daily_loss_pct,
            "max_concurrent_trades": profile.max_concurrent_trades,
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return comprehensive status dict."""
        profile = self.get_active_profile()
        available = [
            n for n in self._profiles if n != "custom"
        ]
        return {
            "active_profile": self._active_name,
            "parameters": asdict(profile),
            "auto_reduced": self._auto_reduced,
            "auto_reduced_from": self._auto_reduced_from,
            "consecutive_wins": self._consecutive_wins,
            "consecutive_losses": self._consecutive_losses,
            "available_profiles": available,
        }
