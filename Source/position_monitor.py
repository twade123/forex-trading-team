"""
Runtime manager for open trade positions.

Runs on every candle close and actively manages open trades by executing
partial take-profits, updating trailing stops, and managing profit locks
using the calculations from StopManager (pure math, no state, no API calls).

PositionMonitor is STATEFUL -- it tracks trade lifecycle, delegates all
stop/target calculations to StopManager, and calls OandaClient for trade
modifications and partial closes.  Trade state persists to JSON for
bot-restart recovery.

Usage::

    from trading_bot.source.position_monitor import PositionMonitor
    from trading_bot.source.stop_manager import StopManager
    from trading_bot.source.oanda_client import OandaClient
    from trading_bot.source.account_manager import AccountManager

    client = OandaClient()
    sm = StopManager()
    am = AccountManager(client)
    am.initialize()

    pm = PositionMonitor(client, sm, am)
    pm.register_trade("12345", "EUR_USD", "buy", 1.08500, 1.08222, 1692,
                       pip_size=0.0001, display_precision=5)
    actions = pm.update_all(candles_by_instrument, current_prices)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trading_bot.position_monitor")

# Default state file location (next to this module)
_STATE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_STATE_FILE = os.path.join(_STATE_DIR, ".position_monitor_state.json")


class PositionMonitor:
    """Runtime manager for open trade positions.

    Runs on every candle close (or configurable interval).  For each open
    trade, evaluates whether to:

    1. Execute partial take-profit (close units via Oanda)
    2. Update trailing stop (move stop via Oanda)
    3. Lock profit at 0.25R (move stop to entry + 0.25R)
    4. Apply time-based decay (tighten trailing as trade ages)

    Uses StopManager for all calculations -- PositionMonitor handles
    state tracking and Oanda API calls.

    Trade state is persisted to survive bot restarts.

    Args:
        oanda_client: Authenticated OandaClient for API calls.
        stop_manager: StopManager instance for stop/TP calculations.
        account_manager: AccountManager for open-trade reconciliation.
        state_file: Path to JSON persistence file.
    """

    def __init__(
        self,
        oanda_client,
        stop_manager,
        account_manager,
        state_file: Optional[str] = None,
    ):
        self._client = oanda_client
        self._stop_manager = stop_manager
        self._account_manager = account_manager
        self._state_file = state_file or _DEFAULT_STATE_FILE

        # trade_id -> trade state dict
        self._trades: Dict[str, Dict[str, Any]] = {}

        # Load persisted state and reconcile with Oanda
        self._load_state()
        self._reconcile_with_oanda()

    # ------------------------------------------------------------------
    # Public: register a new trade
    # ------------------------------------------------------------------

    def register_trade(
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
        """Register a newly opened trade for monitoring.

        Called immediately after a new trade is opened.  Creates internal
        trade state, calculates partial TP plan via StopManager, and sets
        a wide Oanda native trailing stop as safety net.

        Args:
            trade_id: Oanda trade ID.
            instrument: Instrument name (e.g. ``"EUR_USD"``).
            direction: ``"buy"`` or ``"sell"``.
            entry_price: Trade entry price.
            initial_stop: Initial stop-loss price.
            units: Position size in units.
            pip_size: Size of one pip for this instrument.
            display_precision: Decimal places for Oanda price strings.
            atr_value: Current ATR(14) in price units (for safety trail).

        Returns:
            Dict with ``trade_state`` and ``tp_plan``.
        """
        now = datetime.now(timezone.utc).isoformat()

        trade_state: Dict[str, Any] = {
            "trade_id": str(trade_id),
            "instrument": instrument,
            "direction": direction,
            "entry_price": entry_price,
            "initial_stop": initial_stop,
            "initial_units": int(units),
            "current_units": int(units),
            "current_stop": initial_stop,
            "pip_size": pip_size,
            "display_precision": display_precision,
            "tp_state": {
                "tp1_hit": False,
                "tp2_hit": False,
                "profit_locked": False,
            },
            "bars_in_trade": 0,
            "opened_at": now,
            "last_updated": now,
        }

        # Calculate partial TP plan via StopManager
        tp_plan = self._stop_manager.calculate_partial_tp_plan(
            entry_price=entry_price,
            stop_loss_price=initial_stop,
            direction=direction,
            display_precision=display_precision,
            atr_value=atr_value,
        )

        # Set Oanda native trailing stop as wide safety net (4x ATR)
        safety_distance = tp_plan.get("oanda_safety_trail_distance")
        if safety_distance and float(safety_distance) > 0:
            try:
                self._client.set_trade_orders(
                    trade_id=str(trade_id),
                    trailing_stop_loss={"distance": safety_distance},
                )
                logger.info(
                    "Set safety trailing stop for trade %s: distance=%s",
                    trade_id, safety_distance,
                )
            except Exception as exc:
                logger.error(
                    "Failed to set safety trailing stop for trade %s: %s",
                    trade_id, exc,
                )

        self._trades[str(trade_id)] = trade_state
        self._persist_state()

        logger.info(
            "Registered trade %s: %s %s @ %.5f, stop=%.5f, units=%d",
            trade_id, direction, instrument, entry_price,
            initial_stop, units,
        )

        return {"trade_state": trade_state, "tp_plan": tp_plan}

    # ------------------------------------------------------------------
    # Public: main update loop
    # ------------------------------------------------------------------

    def update_all(
        self,
        candles_by_instrument: Dict[str, List[Dict]],
        current_prices: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """Run the primary update loop for all monitored trades.

        Called every candle close.  For each trade: increment bar count,
        check partial TP triggers, calculate trailing stop update, execute
        any required Oanda API calls, and update trade state.

        Args:
            candles_by_instrument: ``{instrument: [candle_dicts]}`` for
                Chandelier Exit calculation.
            current_prices: ``{instrument: current_mid_price}``.

        Returns:
            List of action result dicts (one per trade that was modified).
        """
        all_results: List[Dict[str, Any]] = []

        for trade_id in list(self._trades.keys()):
            ts = self._trades[trade_id]
            instrument = ts["instrument"]
            direction = ts["direction"]
            pip_size = ts["pip_size"]
            display_precision = ts["display_precision"]

            # Increment bars in trade
            ts["bars_in_trade"] += 1

            current_price = current_prices.get(instrument)
            if current_price is None:
                logger.debug(
                    "No current price for %s, skipping trade %s",
                    instrument, trade_id,
                )
                continue

            candles = candles_by_instrument.get(instrument, [])
            actions: List[Dict[str, Any]] = []

            # --- Check partial take-profit triggers ---
            tp_actions = self._check_partial_tp(
                ts, current_price, pip_size, display_precision,
            )
            actions.extend(tp_actions)

            # --- Calculate trailing stop update ---
            trail_action = self._calculate_trailing_update(
                ts, candles, current_price, pip_size, display_precision,
            )
            if trail_action is not None:
                actions.append(trail_action)

            # --- Execute all actions ---
            if actions:
                results = self._execute_actions(trade_id, actions)
                # Update trade state from executed actions
                for action in actions:
                    self._apply_action_to_state(ts, action, current_price)
                ts["last_updated"] = datetime.now(timezone.utc).isoformat()
                all_results.append({
                    "trade_id": trade_id,
                    "instrument": instrument,
                    "actions": actions,
                    "results": results,
                })

        self._persist_state()
        return all_results

    # ------------------------------------------------------------------
    # Public: trade management
    # ------------------------------------------------------------------

    def remove_trade(self, trade_id: str) -> None:
        """Remove a trade from monitoring (fully closed or stopped out).

        Args:
            trade_id: Oanda trade ID.
        """
        tid = str(trade_id)
        if tid in self._trades:
            logger.info("Removing trade %s from monitor", tid)
            del self._trades[tid]
            self._persist_state()
        else:
            logger.warning("remove_trade: trade %s not found in monitor", tid)

    def get_trade_status(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Get current status for a specific monitored trade.

        Returns the trade state enriched with computed fields:
        ``current_profit_r`` and ``time_in_trade_hours``.

        Args:
            trade_id: Oanda trade ID.

        Returns:
            Status dict, or None if trade not monitored.
        """
        ts = self._trades.get(str(trade_id))
        if ts is None:
            return None

        risk = abs(ts["entry_price"] - ts["initial_stop"])
        if risk > 0:
            # Profit in R requires a current price -- use last known info
            profit_r = 0.0  # placeholder unless we have a live price
        else:
            profit_r = 0.0

        # Time in trade
        try:
            opened = datetime.fromisoformat(ts["opened_at"])
            hours = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
        except (ValueError, TypeError):
            hours = 0.0

        return {
            **ts,
            "current_profit_r": profit_r,
            "time_in_trade_hours": round(hours, 2),
        }

    def get_all_statuses(self) -> List[Dict[str, Any]]:
        """Get status for all monitored trades.

        Returns:
            List of status dicts (see :meth:`get_trade_status`).
        """
        return [
            self.get_trade_status(tid)
            for tid in self._trades
        ]

    # ------------------------------------------------------------------
    # Internal: partial take-profit logic
    # ------------------------------------------------------------------

    def _check_partial_tp(
        self,
        trade_state: Dict[str, Any],
        current_price: float,
        pip_size: float,
        display_precision: int,
    ) -> List[Dict[str, Any]]:
        """Check whether partial TP levels have been reached.

        Calculates current profit in R multiples and triggers:
        - TP1 at 1.0R: close 50% + lock 0.25R profit
        - TP2 at 2.0R: close 25% + activate Chandelier trailing

        Returns:
            List of action dicts to execute.
        """
        entry = trade_state["entry_price"]
        initial_stop = trade_state["initial_stop"]
        direction = trade_state["direction"]
        tp_state = trade_state["tp_state"]
        risk = abs(entry - initial_stop)

        if risk == 0:
            return []

        # Current profit in R
        if direction == "buy":
            profit_r = (current_price - entry) / risk
        else:
            profit_r = (entry - current_price) / risk

        actions: List[Dict[str, Any]] = []

        # TP1 at 1.0R: close 50% + profit lock
        if profit_r >= 1.0 and not tp_state["tp1_hit"]:
            close_units = int(trade_state["initial_units"] * 0.50)
            actions.append({
                "type": "partial_close",
                "units": close_units,
                "reason": "TP1 at 1.0R",
            })

            # Also trigger profit lock (entry + 0.25R, NOT exact BE)
            if direction == "buy":
                lock_price = entry + 0.25 * risk
            else:
                lock_price = entry - 0.25 * risk
            lock_str = f"{lock_price:.{display_precision}f}"

            actions.append({
                "type": "move_stop",
                "new_stop": lock_str,
                "reason": "Profit lock at 0.25R (TP1 triggered)",
            })

        # TP2 at 2.0R: close 25% + activate Chandelier
        if profit_r >= 2.0 and not tp_state["tp2_hit"]:
            close_units = int(trade_state["initial_units"] * 0.25)
            actions.append({
                "type": "partial_close",
                "units": close_units,
                "reason": "TP2 at 2.0R",
            })

        return actions

    # ------------------------------------------------------------------
    # Internal: trailing stop calculation
    # ------------------------------------------------------------------

    def _calculate_trailing_update(
        self,
        trade_state: Dict[str, Any],
        candles: List[Dict],
        current_price: float,
        pip_size: float,
        display_precision: int,
    ) -> Optional[Dict[str, Any]]:
        """Delegate trailing stop calculation to StopManager.

        Computes ATR from candles and passes all context to
        ``stop_manager.calculate_trailing_update()``.  Applies an
        additional safety check: never move stop backwards.

        Returns:
            Action dict ``{type: "move_stop", ...}`` or None.
        """
        if len(candles) < 3:
            return None

        # Compute ATR from candles for StopManager
        atr_value = self._compute_atr_from_candles(candles)

        result = self._stop_manager.calculate_trailing_update(
            entry_price=trade_state["entry_price"],
            current_price=current_price,
            current_stop=trade_state["current_stop"],
            initial_stop=trade_state["initial_stop"],
            direction=trade_state["direction"],
            candles=candles,
            atr_value=atr_value,
            bars_in_trade=trade_state["bars_in_trade"],
            tp_state=trade_state["tp_state"],
            display_precision=display_precision,
        )

        if result.get("should_update") and result.get("new_stop"):
            new_stop_float = float(result["new_stop"])
            direction = trade_state["direction"]

            # Safety: never move stop backwards
            if direction == "buy" and new_stop_float <= trade_state["current_stop"]:
                return None
            if direction == "sell" and new_stop_float >= trade_state["current_stop"]:
                return None

            return {
                "type": "move_stop",
                "new_stop": result["new_stop"],
                "reason": result.get("reason", "trailing_update"),
            }

        return None

    # ------------------------------------------------------------------
    # Internal: execute actions via Oanda API
    # ------------------------------------------------------------------

    def _execute_actions(
        self,
        trade_id: str,
        actions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Execute trade modification actions via Oanda API.

        For each action:
        - ``partial_close``: call ``oanda_client.close_trade(units=...)``
        - ``move_stop``: call ``oanda_client.set_trade_orders(stop_loss=...)``

        Wraps each API call in try/except -- logs errors but never crashes.

        Returns:
            List of result dicts with success/failure status.
        """
        results: List[Dict[str, Any]] = []

        for action in actions:
            action_type = action["type"]
            try:
                if action_type == "partial_close":
                    resp = self._client.close_trade(
                        trade_id=str(trade_id),
                        units=str(action["units"]),
                    )
                    results.append({
                        "action": action_type,
                        "success": True,
                        "response": resp,
                        "reason": action.get("reason"),
                    })
                    logger.info(
                        "Partial close trade %s: %d units (%s)",
                        trade_id, action["units"], action.get("reason"),
                    )

                elif action_type == "move_stop":
                    resp = self._client.set_trade_orders(
                        trade_id=str(trade_id),
                        stop_loss={
                            "price": action["new_stop"],
                            "timeInForce": "GTC",
                        },
                    )
                    results.append({
                        "action": action_type,
                        "success": True,
                        "response": resp,
                        "reason": action.get("reason"),
                    })
                    logger.info(
                        "Moved stop for trade %s to %s (%s)",
                        trade_id, action["new_stop"], action.get("reason"),
                    )

                else:
                    logger.warning(
                        "Unknown action type '%s' for trade %s",
                        action_type, trade_id,
                    )
                    results.append({
                        "action": action_type,
                        "success": False,
                        "error": f"Unknown action type: {action_type}",
                    })

            except Exception as exc:
                logger.error(
                    "Failed to execute %s for trade %s: %s",
                    action_type, trade_id, exc,
                )
                results.append({
                    "action": action_type,
                    "success": False,
                    "error": str(exc),
                    "reason": action.get("reason"),
                })

        return results

    # ------------------------------------------------------------------
    # Internal: apply executed action to internal state
    # ------------------------------------------------------------------

    def _apply_action_to_state(
        self,
        trade_state: Dict[str, Any],
        action: Dict[str, Any],
        current_price: float,
    ) -> None:
        """Update internal trade state after an action is executed."""
        action_type = action["type"]

        if action_type == "partial_close":
            closed_units = action["units"]
            trade_state["current_units"] -= closed_units
            reason = action.get("reason", "")

            if "TP1" in reason:
                trade_state["tp_state"]["tp1_hit"] = True
            if "TP2" in reason:
                trade_state["tp_state"]["tp2_hit"] = True

        elif action_type == "move_stop":
            new_stop = float(action["new_stop"])
            trade_state["current_stop"] = new_stop
            reason = action.get("reason", "")

            if "Profit lock" in reason or "profit_lock" in reason.lower():
                trade_state["tp_state"]["profit_locked"] = True

    # ------------------------------------------------------------------
    # Internal: reconcile with Oanda on startup
    # ------------------------------------------------------------------

    def _reconcile_with_oanda(self) -> None:
        """Reconcile persisted state with actual open trades in Oanda.

        Called on startup.  Removes any monitored trade states that do not
        have a matching open trade in Oanda (trade was closed while bot
        was offline).  Logs warnings for orphaned states and info for
        untracked open trades.
        """
        try:
            open_trades = self._client.get_open_trades()
        except Exception as exc:
            logger.warning(
                "Failed to fetch open trades for reconciliation: %s", exc,
            )
            return

        open_trade_ids = {str(t.get("id", "")) for t in open_trades}

        # Remove orphaned monitor states
        orphaned = [
            tid for tid in self._trades
            if tid not in open_trade_ids
        ]
        for tid in orphaned:
            logger.warning(
                "Reconciliation: removing orphaned trade state %s "
                "(trade closed while bot was offline)",
                tid,
            )
            del self._trades[tid]

        # Log untracked open trades
        monitored_ids = set(self._trades.keys())
        for t in open_trades:
            tid = str(t.get("id", ""))
            if tid and tid not in monitored_ids:
                logger.info(
                    "Reconciliation: open trade %s (%s) found in Oanda "
                    "but not in monitor state (opened while bot was down)",
                    tid, t.get("instrument", "unknown"),
                )

        if orphaned:
            self._persist_state()

    # ------------------------------------------------------------------
    # Internal: state persistence
    # ------------------------------------------------------------------

    def _persist_state(self) -> None:
        """Save all trade states to JSON file."""
        try:
            data = {
                "trades": self._trades,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(self._state_file, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            logger.error("Failed to persist position monitor state: %s", exc)

    def _load_state(self) -> None:
        """Load trade states from JSON file."""
        try:
            with open(self._state_file, "r") as f:
                data = json.load(f)
            self._trades = data.get("trades", {})
            if self._trades:
                logger.info(
                    "Loaded %d trade states from %s",
                    len(self._trades), self._state_file,
                )
        except FileNotFoundError:
            pass  # Normal on first run
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load position monitor state: %s", exc)

    # ------------------------------------------------------------------
    # Internal: ATR helper
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_atr_from_candles(
        candles: List[Dict], period: int = 14
    ) -> float:
        """Compute ATR from candle dicts (mirrors StopManager approach).

        Returns ATR in price units, or 0.0 if insufficient data.
        """
        highs, lows, closes = [], [], []
        for c in candles:
            if isinstance(c.get("mid"), dict):
                highs.append(float(c["mid"]["h"]))
                lows.append(float(c["mid"]["l"]))
                closes.append(float(c["mid"]["c"]))
            else:
                highs.append(float(c.get("high", c.get("h", 0))))
                lows.append(float(c.get("low", c.get("l", 0))))
                closes.append(float(c.get("close", c.get("c", 0))))

        true_ranges = []
        for i in range(1, len(closes)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            true_ranges.append(max(hl, hc, lc))

        if len(true_ranges) >= period:
            return sum(true_ranges[-period:]) / period
        if true_ranges:
            return sum(true_ranges) / len(true_ranges)
        return 0.0
