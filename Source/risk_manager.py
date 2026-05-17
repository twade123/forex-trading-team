"""
Central risk authority composing all Phase 8 sub-components.

The RiskManager is the **single entry point** for all risk decisions.
It composes PositionSizer, StopManager, RiskRegistry, RiskProfileManager,
CircuitBreaker, EventFlattener, and PositionMonitor into a unified
interface.  The strategy engine calls ``pre_trade_check()`` before any
order -- if RiskManager says no, no order is placed regardless of
confluence score.

Defence-in-depth ordering:
    1. Circuit breaker check (6 layers)
    2. Event / weekend / overnight check
    3. Position sizing
    4. Stop / TP calculation
    5. Structure-based stop validation
    6. Partial take-profit plan

Size multipliers from different sources are **multiplied** (not added)
for compounding reduction.

Usage::

    from trading_bot.source.risk_manager import RiskManager

    risk_mgr = RiskManager(oanda_client, account_manager)
    check = risk_mgr.pre_trade_check(
        instrument="EUR_USD", direction="buy",
        atr_value=0.00185, regime="mixed",
        current_price=1.08500, pip_size=0.0001,
        display_precision=5, spread_pips=1.8,
    )
    if check["allowed"]:
        size = check["position_size"]
        stops = check["stop_levels"]
        # Place order with size and stops
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .position_sizer import PositionSizer
    from .stop_manager import StopManager
    from .risk_registry import RiskRegistry
    from .risk_profile import RiskProfileManager
    from .circuit_breaker import CircuitBreaker
    from .event_flattener import EventFlattener
    from .position_monitor import PositionMonitor
except ImportError:
    from position_sizer import PositionSizer
    from stop_manager import StopManager
    from risk_registry import RiskRegistry
    from risk_profile import RiskProfileManager
    from circuit_breaker import CircuitBreaker
    from event_flattener import EventFlattener
    from position_monitor import PositionMonitor

logger = logging.getLogger("trading_bot.risk_manager")


class RiskManager:
    """Central risk authority.  Composes all Phase 8 sub-components.

    Called BEFORE any order is placed.  If RiskManager says no,
    no order is placed regardless of confluence score.

    Components:
        - PositionSizer: calculates position size
        - StopManager: calculates stop/TP levels
        - RiskRegistry: routes to asset-class risk config
        - RiskProfileManager: named presets and auto-adjustment
        - CircuitBreaker: 6-layer protection
        - EventFlattener: calendar-based risk reduction
        - PositionMonitor: active trade management

    Args:
        oanda_client: Authenticated :class:`OandaClient` instance.
        account_manager: Initialised :class:`AccountManager` instance.
        config_dir: Path to the ``Config/`` directory.  Defaults to
            ``Forex Trading Team/Config/`` relative to the Source package.
    """

    def __init__(
        self,
        oanda_client,
        account_manager,
        config_dir: Optional[str] = None,
    ) -> None:
        self._client = oanda_client
        self._account_manager = account_manager

        if config_dir is None:
            source_dir = Path(__file__).resolve().parent
            config_dir = str(source_dir.parent / "Config")

        config_path = Path(config_dir)

        # Initialise all sub-components
        self.registry = RiskRegistry(
            str(config_path / "risk_asset_classes.yaml")
        )
        self.profile_manager = RiskProfileManager(
            str(config_path / "risk_profiles.yaml")
        )
        self.position_sizer = PositionSizer()
        self.stop_manager = StopManager()
        self.circuit_breaker = CircuitBreaker(
            account_manager, self.profile_manager, self.registry,
        )
        self.event_flattener = EventFlattener(
            self.profile_manager, account_manager,
        )
        self.position_monitor = PositionMonitor(
            oanda_client, self.stop_manager, account_manager,
        )

        logger.info(
            "RiskManager initialised: profile=%s, asset_classes=%s",
            self.profile_manager.get_active_profile().name,
            self.registry.list_asset_classes(),
        )

    # ------------------------------------------------------------------
    # Main entry point: pre-trade check
    # ------------------------------------------------------------------

    def pre_trade_check(
        self,
        instrument: str,
        direction: str,
        atr_value: float,
        regime: str,
        current_price: float,
        pip_size: float,
        display_precision: int,
        spread_pips: float,
        news_data: Optional[Dict[str, Any]] = None,
        candles: Optional[List[Dict]] = None,
        atr_50: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run all risk checks and return a composite pre-trade decision.

        Defence-in-depth ordering:

        1. Circuit breakers (if any layer fires, return immediately)
        2. Event / weekend / overnight (if blocked, return immediately)
        3. Position sizing (with compounded size multipliers)
        4. Stop and take-profit calculation
        5. Structure-based stop validation (if candles provided)
        6. Partial take-profit plan
        7. Risk limits for TradeValidator bridge

        Args:
            instrument: Oanda instrument name (e.g. ``"EUR_USD"``).
            direction: ``"buy"`` or ``"sell"``.
            atr_value: Current ATR(14) in price units.
            regime: Market regime (``"trending"``/``"ranging"``/``"mixed"``).
            current_price: Current mid price.
            pip_size: Size of one pip (e.g. 0.0001 for EUR/USD).
            display_precision: Decimal places for Oanda price strings.
            spread_pips: Current spread in pips.
            news_data: Output from ``NewsIntelligence`` (optional).
            candles: Recent candle dicts for structure validation (optional).
            atr_50: ATR(50) for volatility-regime adjustment (optional).

        Returns:
            Dict with ``allowed`` (bool), and when allowed: ``position_size``,
            ``stop_levels``, ``partial_tp_plan``, ``risk_limits``,
            ``adjustments``, ``profile``, ``asset_class``.
            When not allowed: ``reason`` and ``details``.
        """
        # ---- Step 1: Circuit breaker check ----
        cb_result = self.circuit_breaker.check_all({
            "instrument": instrument,
            "direction": direction,
        })
        if not cb_result["trading_allowed"]:
            logger.warning(
                "Pre-trade BLOCKED by circuit breaker: %s (%s)",
                cb_result["action"], cb_result["reasons"],
            )
            return {
                "allowed": False,
                "reason": cb_result["action"],
                "details": cb_result,
            }

        # ---- Step 2: Event / weekend / overnight check ----
        event_result = self.event_flattener.get_combined_event_adjustment(
            news_data or {},
        )
        if not event_result["trading_allowed"]:
            logger.warning(
                "Pre-trade BLOCKED by event risk: %s",
                event_result["reasons"],
            )
            return {
                "allowed": False,
                "reason": "event_risk",
                "details": event_result,
            }

        # ---- Step 3: Get risk config and profile ----
        profile = self.profile_manager.get_active_profile()

        # ---- Step 4: Calculate position size ----
        # Combine size multipliers from circuit breaker + event flattener
        combined_size_mult = (
            cb_result["size_multiplier"] * event_result["size_multiplier"]
        )

        news_active = False
        if news_data:
            news_active = bool(news_data.get("high_impact_within_30min", False))

        size_result = self.position_sizer.calculate_position_size(
            balance=float(self._account_manager.balance or 0),
            risk_pct=profile.risk_pct * combined_size_mult,
            atr_value=atr_value,
            pip_size=pip_size,
            current_price=current_price,
            instrument=instrument,
            regime=regime,
            spread_pips=spread_pips,
            atr_50=atr_50,
            news_active=news_active,
            margin_available=float(
                self._account_manager.margin_available or 0
            ),
            margin_rate=float(
                (self._account_manager.get_instrument_spec(instrument) or {})
                .get("marginRate", 0.02)
            ),
        )

        # ---- Step 5: Calculate stop levels ----
        stop_result = self.stop_manager.calculate_stops(
            entry_price=current_price,
            direction=direction,
            atr_value=atr_value,
            regime=regime,
            min_rr_ratio=profile.min_rr_ratio,
            pip_size=pip_size,
            display_precision=display_precision,
            spread_pips=spread_pips,
            news_active=news_active,
            atr_50=atr_50,
        )

        # ---- Step 6: Structure-based stop validation ----
        if candles:
            structure_result = self.stop_manager.validate_with_structure(
                atr_stop_price=float(stop_result["stop_loss"]),
                entry_price=current_price,
                direction=direction,
                candles=candles,
                atr_value=atr_value,
            )
            if structure_result["used_structure"]:
                stop_result["stop_loss"] = format(
                    structure_result["final_stop"],
                    f".{display_precision}f",
                )
                # Recalculate TP based on wider stop
                wider_distance = abs(
                    current_price - structure_result["final_stop"]
                )
                if direction == "buy":
                    new_tp = current_price + wider_distance * profile.min_rr_ratio
                else:
                    new_tp = current_price - wider_distance * profile.min_rr_ratio
                stop_result["take_profit"] = format(
                    new_tp, f".{display_precision}f"
                )
                stop_result["structure_validation"] = structure_result

        # ---- Step 7: Partial TP plan ----
        tp_plan = self.stop_manager.calculate_partial_tp_plan(
            entry_price=current_price,
            stop_loss_price=float(stop_result["stop_loss"]),
            direction=direction,
            display_precision=display_precision,
        )

        # ---- Step 8: Risk limits for TradeValidator bridge ----
        risk_limits = self.profile_manager.get_risk_limits()

        logger.info(
            "Pre-trade ALLOWED: %s %s %s | size=%d | stop=%s | tp=%s | "
            "profile=%s | size_mult=%.2f",
            direction, instrument, regime,
            size_result["units"], stop_result["stop_loss"],
            stop_result["take_profit"], profile.name, combined_size_mult,
        )

        return {
            "allowed": True,
            "position_size": size_result,
            "stop_levels": stop_result,
            "partial_tp_plan": tp_plan,
            "risk_limits": risk_limits,
            "adjustments": {
                "circuit_breaker": cb_result,
                "event": event_result,
                "combined_size_multiplier": combined_size_mult,
            },
            "profile": profile.name,
            "asset_class": self.registry.classify(instrument),
        }

    # ------------------------------------------------------------------
    # Trade result recording
    # ------------------------------------------------------------------

    def record_trade_result(self, result: str) -> None:
        """Record a trade outcome (``"win"`` or ``"loss"``).

        Forwards to CircuitBreaker (which also forwards to
        RiskProfileManager for auto-adjustment).

        Args:
            result: ``"win"`` or ``"loss"``.
        """
        self.circuit_breaker.record_trade_result(result)

    # ------------------------------------------------------------------
    # Trade registration and monitoring
    # ------------------------------------------------------------------

    def register_new_trade(
        self,
        trade_id: str,
        instrument: str,
        direction: str,
        entry_price: float,
        initial_stop: float,
        units: int,
        pip_size: float,
        display_precision: int,
        atr_value: float = 0.0,
    ) -> Dict[str, Any]:
        """Register a new trade for active position monitoring.

        Args:
            trade_id: Oanda trade ID.
            instrument: Instrument name.
            direction: ``"buy"`` or ``"sell"``.
            entry_price: Trade entry price.
            initial_stop: Initial stop-loss price.
            units: Position size in units.
            pip_size: Size of one pip.
            display_precision: Decimal places for price strings.
            atr_value: Current ATR for safety trailing stop.

        Returns:
            Dict with ``trade_state`` and ``tp_plan``.
        """
        return self.position_monitor.register_trade(
            trade_id=trade_id,
            instrument=instrument,
            direction=direction,
            entry_price=entry_price,
            initial_stop=initial_stop,
            units=units,
            pip_size=pip_size,
            display_precision=display_precision,
            atr_value=atr_value,
        )

    def update_open_trades(
        self,
        candles_by_instrument: Dict[str, List[Dict]],
        current_prices: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """Run the position monitor update loop for all open trades.

        Args:
            candles_by_instrument: ``{instrument: [candle_dicts]}``.
            current_prices: ``{instrument: current_mid_price}``.

        Returns:
            List of action result dicts.
        """
        return self.position_monitor.update_all(
            candles_by_instrument, current_prices,
        )

    # ------------------------------------------------------------------
    # Status and convenience
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Aggregate status from all sub-components.

        Returns:
            Dict with ``profile``, ``circuit_breaker``,
            ``monitored_trades``, and ``asset_classes``.
        """
        return {
            "profile": self.profile_manager.get_status(),
            "circuit_breaker": self.circuit_breaker.check_all(),
            "monitored_trades": self.position_monitor.get_all_statuses(),
            "asset_classes": self.registry.list_asset_classes(),
        }

    def set_profile(self, name: str) -> Dict[str, Any]:
        """Switch to a named risk profile.

        Args:
            name: Profile name (e.g. ``"conservative"``).

        Returns:
            Result dict from :meth:`RiskProfileManager.set_profile`.
        """
        return self.profile_manager.set_profile(name)

    def reset_circuit_breakers(self) -> None:
        """Clear all circuit breaker states (operator override)."""
        self.circuit_breaker.manual_reset()
