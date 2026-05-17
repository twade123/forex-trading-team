"""
Six-layer circuit breaker system for the trading bot.

Provides :class:`CircuitBreaker` which monitors account health and
prevents catastrophic losses.  If any circuit breaker fires, trading
is blocked regardless of confluence score.

Layers:
    1. **Daily loss limit** -- tiered response (RMGT-10)
    2. **Overall drawdown** -- high-water mark tracking (RMGT-09)
    3. **Consecutive loss** -- 3-tier circuit breaker
    4. **Cooldown** -- time-based trading pause
    5. **Volatility** -- z-score of current move vs ATR history
    6. **Portfolio heat** -- total open risk limit

All thresholds are driven by the active risk profile.  State
(HWM, consecutive counts, cooldowns) persists across restarts.

Usage::

    from Source.circuit_breaker import CircuitBreaker

    cb = CircuitBreaker(account_manager, risk_profile_manager, risk_registry)
    result = cb.check_all()
    if not result["trading_allowed"]:
        print("BLOCKED:", result["reasons"])
"""

import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("trading_bot.circuit_breaker")

_ET = ZoneInfo("America/New_York")


class CircuitBreaker:
    """Six-layer protection system that overrides all trading signals.

    Monitors account health and prevents catastrophic losses. If any
    circuit breaker fires, trading is blocked regardless of confluence.

    Layers:
    1. Daily loss limit -- tiered response (RMGT-10)
    2. Overall drawdown -- high-water mark tracking (RMGT-09)
    3. Consecutive loss -- 3-tier circuit breaker
    4. Cooldown -- time-based trading pause
    5. Volatility -- z-score of current move vs ATR history
    6. Portfolio heat -- total open risk limit
    """

    # Maximum ATR history entries for volatility z-score
    _MAX_ATR_HISTORY = 50

    def __init__(
        self,
        account_manager,
        risk_profile_manager,
        risk_registry,
        state_file: Optional[str] = None,
    ) -> None:
        self._account_manager = account_manager
        self._risk_profile_manager = risk_profile_manager
        self._risk_registry = risk_registry

        if state_file is None:
            source_dir = Path(__file__).resolve().parent
            state_file = str(source_dir / ".circuit_breaker_state.json")
        self._state_file = state_file

        # State -- initialised from account or restored from file
        self._high_water_mark: float = 0.0
        self._start_of_day_equity: float = 0.0
        self._consecutive_wins: int = 0
        self._consecutive_losses: int = 0
        self._active_cooldowns: List[Dict[str, Any]] = []
        self._atr_history: List[float] = []
        self._halted: bool = False

        # Try to restore persisted state
        loaded = self._load_state()

        if not loaded:
            # First run -- seed from account
            equity = self._current_equity()
            self._high_water_mark = equity
            self._start_of_day_equity = equity
            self._save_state()

    # ------------------------------------------------------------------
    # Composite check
    # ------------------------------------------------------------------

    def check_all(self, proposed_trade: Optional[dict] = None) -> dict:
        """Run ALL circuit breaker checks and return composite result.

        Args:
            proposed_trade: Optional dict with ``instrument`` and
                ``direction`` keys for correlation checking.

        Returns:
            Composite result dict with ``trading_allowed``,
            ``size_multiplier``, ``reasons``, ``checks``, and ``action``.
        """
        daily = self.check_daily_loss()
        drawdown = self.check_overall_drawdown()
        consec = self.check_consecutive_losses()
        cooldown = self.check_cooldown()
        vol = self.check_volatility()
        heat = self.check_portfolio_heat()

        checks = {
            "daily_loss": daily,
            "drawdown": drawdown,
            "consecutive_loss": consec,
            "cooldown": cooldown,
            "volatility": vol,
            "portfolio_heat": heat,
        }

        # Determine worst action across all checks
        action_priority = {
            "halt": 5,
            "close_all": 4,
            "no_new_trades": 3,
            "emergency": 3,
            "reduce_and_cooldown": 2,
            "reduce": 1,
            "critical": 1,
            "defensive": 1,
            "caution": 0,
            "normal": -1,
        }

        worst_action = "normal"
        worst_priority = -1
        reasons: List[str] = []

        # Collect size multipliers from checks that have them
        size_multipliers: List[float] = []

        for name, result in checks.items():
            action = result.get("action", "normal")
            prio = action_priority.get(action, -1)
            if prio > worst_priority:
                worst_priority = prio
                worst_action = action

            # Collect size multiplier
            sm = result.get("size_mult")
            if sm is not None and sm < 1.0:
                size_multipliers.append(sm)

            # Collect reasons
            if action not in ("normal",):
                if name == "daily_loss" and action != "normal":
                    reasons.append(
                        f"Daily loss {result.get('tier', '')}: "
                        f"{result.get('daily_loss_pct', 0):.1f}% of "
                        f"{result.get('limit_pct', 0):.1f}% limit"
                    )
                elif name == "drawdown" and action != "normal":
                    reasons.append(
                        f"Drawdown {result.get('tier', '')}: "
                        f"{result.get('drawdown_pct', 0):.1f}%"
                    )
                elif name == "consecutive_loss" and action != "normal":
                    reasons.append(
                        f"Consecutive losses: {result.get('consecutive_losses', 0)}"
                    )
                elif name == "cooldown" and not result.get("allowed", True):
                    reasons.append(
                        f"Cooldown active: {result.get('reason', 'unknown')} "
                        f"({result.get('remaining_minutes', 0):.0f} min remaining)"
                    )
                elif name == "volatility" and result.get("triggered", False):
                    reasons.append(
                        f"Volatility z-score {result.get('z_score', 0):.1f}: "
                        f"{result.get('level', '')}"
                    )
                elif name == "portfolio_heat" and not result.get("within_limit", True):
                    reasons.append(
                        f"Portfolio heat {result.get('total_heat_pct', 0):.1f}% "
                        f"exceeds {result.get('limit_pct', 0):.1f}% limit"
                    )

        # Composite size_multiplier is the product of all reductions
        if size_multipliers:
            composite_mult = 1.0
            for m in size_multipliers:
                composite_mult *= m
            composite_mult = max(composite_mult, 0.0)
        else:
            composite_mult = 1.0

        # Determine trading_allowed
        blocking_actions = {"halt", "close_all", "no_new_trades", "emergency"}
        trading_allowed = worst_action not in blocking_actions

        # Cooldown overrides
        if not cooldown.get("allowed", True):
            trading_allowed = False
            composite_mult = 0.0

        # Halted state overrides everything
        if self._halted:
            trading_allowed = False
            composite_mult = 0.0
            worst_action = "halt"
            if "System halted — manual reset required" not in reasons:
                reasons.append("System halted — manual reset required")

        return {
            "trading_allowed": trading_allowed,
            "size_multiplier": round(composite_mult, 4),
            "reasons": reasons,
            "checks": checks,
            "action": worst_action,
        }

    # ------------------------------------------------------------------
    # Layer 1: Daily loss limit (RMGT-10)
    # ------------------------------------------------------------------

    def check_daily_loss(self) -> dict:
        """Check daily P&L against tiered loss limits.

        Returns:
            Dict with ``daily_pnl``, ``daily_loss_pct``, ``limit_pct``,
            ``tier``, ``action``, and ``size_mult``.
        """
        equity = self._current_equity()
        daily_pnl = equity - self._start_of_day_equity

        profile = self._risk_profile_manager.get_active_profile()
        limit_pct = profile.max_daily_loss_pct

        if daily_pnl >= 0 or self._start_of_day_equity <= 0:
            return {
                "daily_pnl": round(daily_pnl, 2),
                "daily_loss_pct": 0.0,
                "limit_pct": limit_pct,
                "tier": "normal",
                "action": "normal",
                "size_mult": 1.0,
            }

        daily_loss_pct = abs(daily_pnl) / self._start_of_day_equity * 100.0
        usage = daily_loss_pct / limit_pct * 100.0 if limit_pct > 0 else 100.0

        if usage > 120:
            tier, action, size_mult = "close_all", "close_all", 0.0
            logger.critical(
                "Daily loss %.1f%% exceeds 120%% of limit (%.1f%%) — CLOSE ALL",
                daily_loss_pct, limit_pct,
            )
        elif usage >= 100:
            tier, action, size_mult = "no_new_trades", "no_new_trades", 0.0
            logger.critical(
                "Daily loss %.1f%% exceeds limit (%.1f%%) — no new trades",
                daily_loss_pct, limit_pct,
            )
        elif usage >= 80:
            tier, action, size_mult = "critical", "reduce", 0.25
            logger.warning(
                "Daily loss %.1f%% at 80%%+ of limit (%.1f%%) — critical",
                daily_loss_pct, limit_pct,
            )
        elif usage >= 60:
            tier, action, size_mult = "defensive", "reduce", 0.50
            logger.warning(
                "Daily loss %.1f%% at 60%%+ of limit (%.1f%%) — defensive",
                daily_loss_pct, limit_pct,
            )
        elif usage >= 40:
            tier, action, size_mult = "caution", "caution", 0.75
            logger.info(
                "Daily loss %.1f%% at 40%%+ of limit (%.1f%%) — caution",
                daily_loss_pct, limit_pct,
            )
        else:
            tier, action, size_mult = "normal", "normal", 1.0

        return {
            "daily_pnl": round(daily_pnl, 2),
            "daily_loss_pct": round(daily_loss_pct, 4),
            "limit_pct": limit_pct,
            "tier": tier,
            "action": action,
            "size_mult": size_mult,
        }

    # ------------------------------------------------------------------
    # Layer 2: Overall drawdown (RMGT-09)
    # ------------------------------------------------------------------

    def check_overall_drawdown(self) -> dict:
        """Check drawdown from high-water mark.

        Returns:
            Dict with ``equity``, ``hwm``, ``drawdown_pct``, ``tier``,
            ``action``, and ``size_mult``.
        """
        equity = self._current_equity()

        # Update HWM (only up — never reduce)
        if equity > self._high_water_mark:
            self._high_water_mark = equity
            self._save_state()  # Persist every HWM update

        hwm = self._high_water_mark

        if hwm <= 0:
            return {
                "equity": equity,
                "hwm": hwm,
                "drawdown_pct": 0.0,
                "tier": "normal",
                "action": "normal",
                "size_mult": 1.0,
            }

        drawdown_pct = (hwm - equity) / hwm * 100.0
        drawdown_pct = max(drawdown_pct, 0.0)

        if drawdown_pct >= 10.0:
            tier, action, size_mult = "halt", "halt", 0.0
            self._halted = True
            self._save_state()
            logger.critical(
                "Overall drawdown %.1f%% >= 10%% — HALT TRADING",
                drawdown_pct,
            )
        elif drawdown_pct >= 8.0:
            tier, action, size_mult = "emergency", "emergency", 0.0
            logger.critical(
                "Overall drawdown %.1f%% — emergency, no new trades",
                drawdown_pct,
            )
        elif drawdown_pct >= 7.0:
            tier, action, size_mult = "critical", "reduce", 0.25
            logger.warning("Drawdown %.1f%% — critical", drawdown_pct)
        elif drawdown_pct >= 5.0:
            tier, action, size_mult = "defensive", "reduce", 0.50
            logger.warning("Drawdown %.1f%% — defensive", drawdown_pct)
        elif drawdown_pct >= 3.0:
            tier, action, size_mult = "caution", "caution", 0.75
            logger.info("Drawdown %.1f%% — caution", drawdown_pct)
        else:
            tier, action, size_mult = "normal", "normal", 1.0

        return {
            "equity": round(equity, 2),
            "hwm": round(hwm, 2),
            "drawdown_pct": round(drawdown_pct, 4),
            "tier": tier,
            "action": action,
            "size_mult": size_mult,
        }

    # ------------------------------------------------------------------
    # Layer 3: Consecutive losses
    # ------------------------------------------------------------------

    def check_consecutive_losses(self) -> dict:
        """Check consecutive loss count against tiered thresholds.

        Returns:
            Dict with ``consecutive_losses``, ``tier``, ``action``,
            ``size_mult``, and ``cooldown_minutes``.
        """
        losses = self._consecutive_losses

        if losses >= 7:
            tier = "halt"
            action = "halt"
            size_mult = 0.0
            cooldown_min = 0  # Manual reset required
            self._halted = True
            self._save_state()
            logger.critical(
                "%d consecutive losses — HALT until manual reset", losses
            )
        elif losses >= 5:
            tier = "reduce_and_cooldown"
            action = "reduce_and_cooldown"
            size_mult = 0.50
            # Profile-configurable cooldown (default 120 min / 2 hours)
            cooldown_min = 120
            self._set_cooldown(
                f"Consecutive losses ({losses})",
                cooldown_min,
            )
            logger.warning(
                "%d consecutive losses — reduce + %d min cooldown",
                losses, cooldown_min,
            )
        elif losses >= 3:
            tier = "reduce"
            action = "reduce"
            size_mult = 0.50
            cooldown_min = 0
            logger.warning("%d consecutive losses — reduce to 50%%", losses)
        else:
            tier = "normal"
            action = "normal"
            size_mult = 1.0
            cooldown_min = 0

        return {
            "consecutive_losses": losses,
            "tier": tier,
            "action": action,
            "size_mult": size_mult,
            "cooldown_minutes": cooldown_min,
        }

    # ------------------------------------------------------------------
    # Layer 4: Cooldown
    # ------------------------------------------------------------------

    def check_cooldown(self) -> dict:
        """Check if an active cooldown is in effect.

        Returns:
            Dict with ``allowed``, and optionally ``reason`` and
            ``remaining_minutes``.
        """
        now = datetime.now(_ET)
        self._expire_cooldowns(now)

        if not self._active_cooldowns:
            return {"allowed": True}

        # Return the first (most relevant) active cooldown
        cd = self._active_cooldowns[0]
        expires = datetime.fromisoformat(cd["expires_at"])
        remaining = (expires - now).total_seconds() / 60.0

        return {
            "allowed": False,
            "reason": cd.get("reason", "unknown"),
            "remaining_minutes": round(max(remaining, 0), 1),
        }

    # ------------------------------------------------------------------
    # Layer 5: Volatility z-score
    # ------------------------------------------------------------------

    def check_volatility(
        self,
        current_candle_range: float = 0.0,
        current_move: float = 0.0,
    ) -> dict:
        """Check if current volatility is abnormal relative to ATR history.

        Args:
            current_candle_range: Range (high - low) of the current candle.
            current_move: Absolute price move being evaluated.

        Returns:
            Dict with ``triggered``, ``z_score``, ``level``, ``action``,
            and optionally ``size_mult``.
        """
        move = max(current_candle_range, current_move)

        if len(self._atr_history) < 5 or move == 0:
            return {"triggered": False, "z_score": 0.0}

        mean_atr = sum(self._atr_history) / len(self._atr_history)
        if mean_atr <= 0:
            return {"triggered": False, "z_score": 0.0}

        variance = sum(
            (v - mean_atr) ** 2 for v in self._atr_history
        ) / len(self._atr_history)
        std_atr = math.sqrt(variance) if variance > 0 else mean_atr * 0.1

        z_score = (move - mean_atr) / std_atr if std_atr > 0 else 0.0

        if z_score > 3.0:
            logger.critical(
                "Volatility z-score %.1f > 3.0 — CRITICAL, close all", z_score
            )
            return {
                "triggered": True,
                "z_score": round(z_score, 2),
                "level": "critical",
                "action": "close_all",
                "size_mult": 0.0,
            }
        elif z_score > 2.0:
            logger.warning(
                "Volatility z-score %.1f > 2.0 — warning, reduce", z_score
            )
            return {
                "triggered": True,
                "z_score": round(z_score, 2),
                "level": "warning",
                "action": "reduce",
                "size_mult": 0.5,
            }
        else:
            return {
                "triggered": False,
                "z_score": round(z_score, 2),
            }

    # ------------------------------------------------------------------
    # Layer 6: Portfolio heat
    # ------------------------------------------------------------------

    def check_portfolio_heat(self) -> dict:
        """Check total open risk against portfolio heat limit.

        Returns:
            Dict with ``total_heat_pct``, ``limit_pct``,
            ``within_limit``, ``positions_by_group``, and
            ``correlation_violations``.
        """
        profile = self._risk_profile_manager.get_active_profile()
        limit_pct = profile.max_portfolio_heat_pct
        max_group_positions = profile.max_correlation_group_positions

        balance = self._parse_float(
            self._account_manager.balance, fallback=1.0
        )

        # Get open trades via the client
        try:
            open_trades = self._account_manager._client.get_open_trades()
        except Exception as exc:
            logger.warning("Failed to get open trades: %s", exc)
            open_trades = []

        total_risk = 0.0
        positions_by_group: Dict[str, int] = {}

        for trade in open_trades:
            instrument = trade.get("instrument", "UNKNOWN")
            units = abs(float(trade.get("currentUnits", 0)))
            price = float(trade.get("price", 0))

            # Estimate stop distance from stopLossOrder if present
            stop_order = trade.get("stopLossOrder", {})
            stop_price = float(stop_order.get("price", 0)) if stop_order else 0
            if stop_price > 0 and price > 0:
                stop_distance = abs(price - stop_price)
            else:
                # Fallback: approximate 1% of price as stop distance
                stop_distance = price * 0.01

            # Approximate risk amount (units * stop_distance)
            risk_amount = units * stop_distance
            total_risk += risk_amount

            # Track correlation groups
            group = self._risk_registry.get_correlation_group(instrument)
            if group:
                positions_by_group[group] = (
                    positions_by_group.get(group, 0) + 1
                )

        total_heat_pct = (total_risk / balance * 100.0) if balance > 0 else 0.0

        # Check correlation group violations
        correlation_violations: List[str] = []
        for group, count in positions_by_group.items():
            if count > max_group_positions:
                correlation_violations.append(
                    f"{group}: {count} positions (max {max_group_positions})"
                )

        within_limit = (
            total_heat_pct <= limit_pct and len(correlation_violations) == 0
        )

        if not within_limit:
            action = "no_new_trades"
        else:
            action = "normal"

        return {
            "total_heat_pct": round(total_heat_pct, 4),
            "limit_pct": limit_pct,
            "within_limit": within_limit,
            "positions_by_group": positions_by_group,
            "correlation_violations": correlation_violations,
            "action": action,
        }

    # ------------------------------------------------------------------
    # Trade result recording
    # ------------------------------------------------------------------

    def record_trade_result(self, result: str) -> None:
        """Record a trade outcome and update consecutive counters.

        Args:
            result: ``"win"`` or ``"loss"``.
        """
        if result == "loss":
            self._consecutive_losses += 1
            self._consecutive_wins = 0
        elif result == "win":
            self._consecutive_wins += 1
            self._consecutive_losses = 0

        # Forward to risk profile manager for auto-adjustment
        self._risk_profile_manager.record_trade_result(result)

        self._save_state()
        logger.info(
            "Trade result: %s (consecutive W:%d L:%d)",
            result, self._consecutive_wins, self._consecutive_losses,
        )

    # ------------------------------------------------------------------
    # ATR history management
    # ------------------------------------------------------------------

    def update_atr(self, atr_value: float) -> None:
        """Add a new ATR value to the rolling history.

        Args:
            atr_value: Current ATR value.
        """
        if atr_value > 0:
            self._atr_history.append(atr_value)
            if len(self._atr_history) > self._MAX_ATR_HISTORY:
                self._atr_history = self._atr_history[-self._MAX_ATR_HISTORY:]
            self._save_state()

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """Reset daily tracking at 5 PM ET forex day rollover."""
        equity = self._current_equity()
        self._start_of_day_equity = equity
        self._save_state()
        logger.info(
            "Daily reset: start_of_day_equity set to %.2f", equity,
        )

    # ------------------------------------------------------------------
    # Manual reset (Tim's override)
    # ------------------------------------------------------------------

    def manual_reset(self) -> None:
        """Clear halt state, cooldowns, and consecutive counters.

        This is Tim's operator override to resume trading after a
        circuit breaker halt.
        """
        self._halted = False
        self._active_cooldowns = []
        self._consecutive_losses = 0
        self._consecutive_wins = 0
        self._save_state()
        logger.warning(
            "Manual reset by operator — all circuit breakers cleared"
        )

    # ------------------------------------------------------------------
    # Cooldown helpers
    # ------------------------------------------------------------------

    def _set_cooldown(self, reason: str, minutes: int) -> None:
        """Add a cooldown period."""
        now = datetime.now(_ET)
        expires = now + timedelta(minutes=minutes)

        # Don't add duplicate cooldowns
        for cd in self._active_cooldowns:
            if cd.get("reason") == reason:
                return

        self._active_cooldowns.append({
            "reason": reason,
            "expires_at": expires.isoformat(),
        })
        self._save_state()

    def _expire_cooldowns(self, now: datetime) -> None:
        """Remove expired cooldowns."""
        before = len(self._active_cooldowns)
        self._active_cooldowns = [
            cd for cd in self._active_cooldowns
            if datetime.fromisoformat(cd["expires_at"]) > now
        ]
        if len(self._active_cooldowns) < before:
            self._save_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Persist circuit breaker state to JSON file."""
        try:
            state = {
                "high_water_mark": self._high_water_mark,
                "start_of_day_equity": self._start_of_day_equity,
                "consecutive_wins": self._consecutive_wins,
                "consecutive_losses": self._consecutive_losses,
                "active_cooldowns": self._active_cooldowns,
                "atr_history": self._atr_history,
                "halted": self._halted,
                "saved_at": datetime.now(_ET).isoformat(),
            }
            with open(self._state_file, "w") as f:
                json.dump(state, f, indent=2)
        except OSError as exc:
            logger.warning("Failed to save circuit breaker state: %s", exc)

    def _load_state(self) -> bool:
        """Restore circuit breaker state from JSON file.

        Returns:
            ``True`` if state was successfully loaded.
        """
        try:
            with open(self._state_file, "r") as f:
                state = json.load(f)
        except FileNotFoundError:
            return False
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load circuit breaker state: %s", exc)
            return False

        self._high_water_mark = float(state.get("high_water_mark", 0))
        self._start_of_day_equity = float(
            state.get("start_of_day_equity", 0)
        )
        self._consecutive_wins = int(state.get("consecutive_wins", 0))
        self._consecutive_losses = int(state.get("consecutive_losses", 0))
        self._active_cooldowns = state.get("active_cooldowns", [])
        self._atr_history = state.get("atr_history", [])
        self._halted = bool(state.get("halted", False))

        logger.info(
            "Restored circuit breaker state: HWM=%.2f, losses=%d, halted=%s",
            self._high_water_mark, self._consecutive_losses, self._halted,
        )
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_equity(self) -> float:
        """Get current equity (balance + unrealized P&L) from account manager."""
        nav = self._account_manager.nav
        if nav is not None:
            return float(nav)

        balance = self._account_manager.balance
        unrealized = self._account_manager.unrealized_pl
        if balance is not None:
            b = float(balance)
            u = float(unrealized) if unrealized is not None else 0.0
            return b + u

        return 0.0

    @staticmethod
    def _parse_float(value, fallback: float = 0.0) -> float:
        """Safely parse a string or numeric value to float."""
        if value is None:
            return fallback
        try:
            return float(value)
        except (ValueError, TypeError):
            return fallback
