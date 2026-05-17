"""
Comprehensive stop, target, trailing, and partial take-profit management.

Research-informed implementation combining ATR-based stops with regime-adaptive
multipliers, structure-based stop validation using swing points, Chandelier Exit
trailing, 50/25/25 partial take-profit plan, 0.25R profit lock (avoids the
break-even trap), time-based stop decay overlay, and Oanda native trailing stop
as a wide safety net.

Usage::

    from trading_bot.source.stop_manager import StopManager

    sm = StopManager()
    stops = sm.calculate_stops(
        entry_price=1.08500, direction="buy", atr_value=0.00185,
        regime="mixed", pip_size=0.0001, display_precision=5,
    )
    partial = sm.calculate_partial_tp_plan(
        entry_price=1.08500, stop_loss_price=float(stops["stop_loss"]),
        direction="buy", display_precision=5,
    )
"""

import logging
import math
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("trading_bot.stop_manager")

# ---------------------------------------------------------------------------
# Regime-adaptive ATR stop multipliers (shared with PositionSizer, RMGT-03)
# ---------------------------------------------------------------------------
REGIME_MULTIPLIERS: Dict[str, float] = {
    "trending": 2.0,   # Trends need room to breathe (ADX > 25)
    "ranging": 1.2,    # Tighter in ranges (ADX < 20)
    "mixed": 1.5,      # Default (ADX 20-25)
}


def _get_regime_multiplier(regime: str, news_active: bool = False) -> float:
    """Return the ATR stop multiplier for the given market regime."""
    mult = REGIME_MULTIPLIERS.get(regime, REGIME_MULTIPLIERS["mixed"])
    if news_active:
        mult = max(mult, 2.5)
    return mult


def _get_volatility_adjustment(atr_14: float, atr_50: Optional[float]) -> float:
    """Adjust ATR multiplier based on current-vs-historical volatility."""
    if atr_50 is None or atr_50 == 0:
        return 1.0
    ratio = atr_14 / atr_50
    if ratio >= 1.5:
        return 1.3
    if ratio >= 1.2:
        return 1.15
    if ratio <= 0.7:
        return 0.85
    if ratio <= 0.85:
        return 0.92
    return 1.0


def _fmt(price: float, precision: int) -> str:
    """Format a price to a string with *precision* decimal places."""
    return f"{price:.{precision}f}"


class StopManager:
    """Comprehensive stop, target, trailing, and partial TP management.

    Research-informed implementation combining:
    - ATR-based stops with regime-adaptive multipliers (RMGT-03)
    - Structure-based stop validation using swing points
    - Chandelier Exit trailing (primary, RMGT-06)
    - 50/25/25 partial take-profit plan (RMGT-05)
    - Lock 0.25R profit instead of exact breakeven (research: BE trap)
    - Time-based stop decay overlay
    - Oanda native trailing stop as wide safety net

    This class is pure calculation -- no state, no API calls.
    All output prices are formatted as strings with ``display_precision``
    decimal places for direct use with the Oanda REST API.
    """

    # ------------------------------------------------------------------
    # Method 1: Initial stop and take-profit levels
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_stops(
        entry_price: float,
        direction: str,
        atr_value: float,
        regime: str,
        pip_size: float,
        display_precision: int,
        min_rr_ratio: float = 2.0,
        spread_pips: float = 0.0,
        news_active: bool = False,
        atr_50: Optional[float] = None,
    ) -> Dict:
        """Calculate initial stop-loss and take-profit levels.

        Combines regime-adaptive ATR multiplier and volatility-regime
        adjustment to determine stop distance, then places TP at the
        requested minimum reward:risk ratio.

        Args:
            entry_price: Trade entry price.
            direction: ``"buy"`` or ``"sell"``.
            atr_value: Current ATR(14) in price units.
            regime: ``"trending"``/``"ranging"``/``"mixed"``.
            pip_size: Size of one pip.
            display_precision: Decimal places for Oanda price strings.
            min_rr_ratio: Minimum reward:risk ratio (default 2.0).
            spread_pips: Current spread in pips (default 0).
            news_active: Whether high-impact news is within 30 min.
            atr_50: ATR(50) for volatility-regime adjustment.

        Returns:
            Dict with ``stop_loss``, ``take_profit`` (formatted strings),
            ``stop_distance_price``, ``stop_distance_pips``,
            ``risk_pips``, ``reward_pips``, ``rr_ratio``,
            ``regime_multiplier``, ``direction``.
        """
        regime_mult = _get_regime_multiplier(regime, news_active)
        vol_adj = _get_volatility_adjustment(atr_value, atr_50)
        effective_mult = regime_mult * vol_adj

        stop_distance_price = atr_value * effective_mult

        # Enforce minimum stop distance of 2x spread in price units
        min_stop = 2.0 * spread_pips * pip_size
        if stop_distance_price < min_stop:
            stop_distance_price = min_stop

        stop_distance_pips = stop_distance_price / pip_size

        # Spread-adjusted risk in pips
        risk_pips = stop_distance_pips + spread_pips
        reward_pips = risk_pips * min_rr_ratio
        rr_ratio = min_rr_ratio  # by construction

        if direction == "buy":
            sl = entry_price - stop_distance_price
            tp = entry_price + stop_distance_price * min_rr_ratio
        else:  # sell
            sl = entry_price + stop_distance_price
            tp = entry_price - stop_distance_price * min_rr_ratio

        return {
            "stop_loss": _fmt(sl, display_precision),
            "take_profit": _fmt(tp, display_precision),
            "stop_distance_price": stop_distance_price,
            "stop_distance_pips": stop_distance_pips,
            "risk_pips": risk_pips,
            "reward_pips": reward_pips,
            "rr_ratio": rr_ratio,
            "regime_multiplier": regime_mult,
            "direction": direction,
        }

    # ------------------------------------------------------------------
    # Method 2: Structure-based stop validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_with_structure(
        atr_stop_price: float,
        entry_price: float,
        direction: str,
        candles: List[Dict],
        atr_value: float,
        lookback: int = 20,
        buffer_atr_mult: float = 0.2,
    ) -> Dict:
        """Validate ATR stop against recent swing-point structure.

        Uses ``scipy.signal.argrelextrema`` to find local swing highs/lows
        and takes the **wider** of ATR stop and structure stop (lower for
        buys, higher for sells).

        Args:
            atr_stop_price: Stop price from ATR calculation.
            entry_price: Trade entry price.
            direction: ``"buy"`` or ``"sell"``.
            candles: Recent candle dicts (need ``mid.h``, ``mid.l`` keys
                or plain ``high``/``low`` keys).
            atr_value: Current ATR for buffer calculation.
            lookback: Number of candles to scan (default 20).
            buffer_atr_mult: ATR fraction added beyond swing point
                (default 0.2).

        Returns:
            Dict with ``final_stop``, ``atr_stop``, ``structure_stop``,
            ``used_structure`` (bool), ``swing_point`` (float or None).
        """
        from scipy.signal import argrelextrema

        # Extract price arrays from candles
        recent = candles[-lookback:] if len(candles) > lookback else candles
        highs = []
        lows = []
        for c in recent:
            if isinstance(c.get("mid"), dict):
                highs.append(float(c["mid"]["h"]))
                lows.append(float(c["mid"]["l"]))
            else:
                highs.append(float(c.get("high", c.get("h", 0))))
                lows.append(float(c.get("low", c.get("l", 0))))

        highs_arr = np.array(highs)
        lows_arr = np.array(lows)

        buffer = atr_value * buffer_atr_mult

        if direction == "buy":
            # Find swing lows
            indices = argrelextrema(lows_arr, np.less, order=3)[0]
            if len(indices) > 0:
                swing_point = float(lows_arr[indices[-1]])  # nearest
                structure_stop = swing_point - buffer
            else:
                swing_point = None
                structure_stop = atr_stop_price  # fallback
            # Take the wider (lower for buy)
            final_stop = min(atr_stop_price, structure_stop)
        else:
            # Find swing highs
            indices = argrelextrema(highs_arr, np.greater, order=3)[0]
            if len(indices) > 0:
                swing_point = float(highs_arr[indices[-1]])
                structure_stop = swing_point + buffer
            else:
                swing_point = None
                structure_stop = atr_stop_price
            # Take the wider (higher for sell)
            final_stop = max(atr_stop_price, structure_stop)

        used_structure = (final_stop != atr_stop_price)

        return {
            "final_stop": final_stop,
            "atr_stop": atr_stop_price,
            "structure_stop": structure_stop,
            "used_structure": used_structure,
            "swing_point": swing_point,
        }

    # ------------------------------------------------------------------
    # Method 3: Partial TP plan (50/25/25 split)
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_partial_tp_plan(
        entry_price: float,
        stop_loss_price: float,
        direction: str,
        display_precision: int,
        atr_value: float = 0.0,
    ) -> Dict:
        """Create a 50/25/25 partial take-profit plan (RMGT-05).

        - TP1 at 1.0R: close 50% and lock 0.25R profit (not exact BE).
        - TP2 at 2.0R: close 25% and activate Chandelier trailing.
        - Runner (25%): trails with Chandelier Exit.

        Args:
            entry_price: Trade entry price.
            stop_loss_price: Initial stop-loss price.
            direction: ``"buy"`` or ``"sell"``.
            display_precision: Decimal places for Oanda price strings.
            atr_value: ATR for Oanda native trailing stop distance.

        Returns:
            Dict with ``risk_distance``, ``tp1_price``, ``tp1_pct``,
            ``tp2_price``, ``tp2_pct``, ``runner_pct``,
            ``profit_lock_trigger``, ``profit_lock_stop``,
            ``oanda_safety_trail_distance``, ``plan``.
        """
        risk_distance = abs(entry_price - stop_loss_price)

        if direction == "buy":
            tp1 = entry_price + 1.0 * risk_distance
            tp2 = entry_price + 2.0 * risk_distance
            lock_stop = entry_price + 0.25 * risk_distance
        else:
            tp1 = entry_price - 1.0 * risk_distance
            tp2 = entry_price - 2.0 * risk_distance
            lock_stop = entry_price - 0.25 * risk_distance

        return {
            "risk_distance": risk_distance,
            "tp1_price": _fmt(tp1, display_precision),
            "tp1_pct": 50,
            "tp2_price": _fmt(tp2, display_precision),
            "tp2_pct": 25,
            "runner_pct": 25,
            "profit_lock_trigger": _fmt(tp1, display_precision),
            "profit_lock_stop": _fmt(lock_stop, display_precision),
            "oanda_safety_trail_distance": _fmt(atr_value * 4.0, display_precision),
            "plan": [
                {
                    "at": "1.0R",
                    "action": "lock_profit_and_close_50pct",
                    "stop_to": "entry + 0.25R",
                },
                {
                    "at": "2.0R",
                    "action": "close_25pct_activate_chandelier",
                },
                {
                    "at": "runner",
                    "action": "trail_with_chandelier",
                },
            ],
        }

    # ------------------------------------------------------------------
    # Method 4: Chandelier Exit trailing (RMGT-06)
    # ------------------------------------------------------------------

    @staticmethod
    def chandelier_exit(
        candles: List[Dict],
        direction: str,
        atr_period: int = 14,
        lookback: int = 22,
        multiplier: float = 3.0,
        display_precision: int = 5,
    ) -> Dict:
        """Calculate the Chandelier Exit trailing stop.

        Primary trailing algorithm for the runner portion.  Uses the
        highest high (longs) or lowest low (shorts) over *lookback* bars
        and offsets by ATR * multiplier.

        Args:
            candles: Recent candle dicts (need at least
                ``max(lookback, atr_period + 1)`` candles).
            direction: ``"buy"`` or ``"sell"``.
            atr_period: ATR calculation period (default 14).
            lookback: Bars for highest-high / lowest-low (default 22).
            multiplier: ATR multiplier (default 3.0).
            display_precision: Decimal places for Oanda price strings.

        Returns:
            Dict with ``trail_stop``, ``trail_stop_float``,
            ``highest_high`` (or ``lowest_low``), ``atr_used``,
            ``multiplier``.
        """
        # Extract OHLC from candles
        highs = []
        lows = []
        closes = []
        for c in candles:
            if isinstance(c.get("mid"), dict):
                highs.append(float(c["mid"]["h"]))
                lows.append(float(c["mid"]["l"]))
                closes.append(float(c["mid"]["c"]))
            else:
                highs.append(float(c.get("high", c.get("h", 0))))
                lows.append(float(c.get("low", c.get("l", 0))))
                closes.append(float(c.get("close", c.get("c", 0))))

        # Calculate ATR (True Range method)
        true_ranges = []
        for i in range(1, len(closes)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            true_ranges.append(max(hl, hc, lc))

        if len(true_ranges) >= atr_period:
            atr = sum(true_ranges[-atr_period:]) / atr_period
        elif len(true_ranges) > 0:
            atr = sum(true_ranges) / len(true_ranges)
        else:
            atr = 0.0

        # Highest high / lowest low over lookback window
        recent_highs = highs[-lookback:]
        recent_lows = lows[-lookback:]

        if direction == "buy":
            hh = max(recent_highs)
            trail = hh - atr * multiplier
            return {
                "trail_stop": _fmt(trail, display_precision),
                "trail_stop_float": trail,
                "highest_high": hh,
                "atr_used": atr,
                "multiplier": multiplier,
            }
        else:
            ll = min(recent_lows)
            trail = ll + atr * multiplier
            return {
                "trail_stop": _fmt(trail, display_precision),
                "trail_stop_float": trail,
                "lowest_low": ll,
                "atr_used": atr,
                "multiplier": multiplier,
            }

    # ------------------------------------------------------------------
    # Method 5: Ratchet trailing for partial position management
    # ------------------------------------------------------------------

    @staticmethod
    def ratchet_trailing(
        entry_price: float,
        initial_stop_price: float,
        direction: str,
        current_price: float,
        current_trail_stop: float,
        step_size_r: float = 0.5,
        trail_offset_r: float = 1.0,
        display_precision: int = 5,
    ) -> Dict:
        """Step-based ratchet trailing stop.

        Moves the stop in discrete R-multiple steps.  Secondary to
        Chandelier Exit -- used for partial position management.

        Args:
            entry_price: Trade entry price.
            initial_stop_price: Original stop-loss price (defines 1R).
            direction: ``"buy"`` or ``"sell"``.
            current_price: Current market price.
            current_trail_stop: Current trailing stop level.
            step_size_r: R-multiple per step (default 0.5).
            trail_offset_r: Stop trails profit by this many R (default 1.0).
            display_precision: Decimal places for Oanda price strings.

        Returns:
            Dict with ``new_stop`` (str or None), ``should_update`` (bool),
            ``current_profit_r``, ``steps_achieved``, ``reason``.
        """
        risk = abs(entry_price - initial_stop_price)
        if risk == 0:
            return {
                "new_stop": None,
                "should_update": False,
                "current_profit_r": 0.0,
                "steps_achieved": 0,
                "reason": "zero_risk_distance",
            }

        if direction == "buy":
            current_profit_r = (current_price - entry_price) / risk
        else:
            current_profit_r = (entry_price - current_price) / risk

        if current_profit_r <= 0:
            return {
                "new_stop": None,
                "should_update": False,
                "current_profit_r": current_profit_r,
                "steps_achieved": 0,
                "reason": "not_in_profit",
            }

        steps = int(current_profit_r / step_size_r)
        trail_r = (steps * step_size_r) - trail_offset_r

        if direction == "buy":
            new_stop_price = entry_price + (trail_r * risk)
            # Never move stop backwards
            if new_stop_price <= current_trail_stop:
                return {
                    "new_stop": None,
                    "should_update": False,
                    "current_profit_r": current_profit_r,
                    "steps_achieved": steps,
                    "reason": "ratchet_not_advanced",
                }
        else:
            new_stop_price = entry_price - (trail_r * risk)
            if new_stop_price >= current_trail_stop:
                return {
                    "new_stop": None,
                    "should_update": False,
                    "current_profit_r": current_profit_r,
                    "steps_achieved": steps,
                    "reason": "ratchet_not_advanced",
                }

        return {
            "new_stop": _fmt(new_stop_price, display_precision),
            "should_update": True,
            "current_profit_r": current_profit_r,
            "steps_achieved": steps,
            "reason": f"ratchet_step_{steps}",
        }

    # ------------------------------------------------------------------
    # Method 6: Composite trailing update
    # ------------------------------------------------------------------

    def calculate_trailing_update(
        self,
        entry_price: float,
        current_price: float,
        current_stop: float,
        initial_stop: float,
        direction: str,
        candles: List[Dict],
        atr_value: float,
        bars_in_trade: int,
        tp_state: Dict,
        display_precision: int,
    ) -> Dict:
        """Determine the best trailing stop update.

        Applies the tightest of all applicable trailing methods:

        1. Profit lock (0.25R) when profit >= 1.0R and stop still below lock.
        2. Chandelier Exit (tight 2.0x after TP2, standard 3.0x after TP1).
        3. Time-based decay overlay after 24+ bars.
        4. Never moves stop backwards.

        Args:
            entry_price: Trade entry price.
            current_price: Current market price.
            current_stop: Current trailing stop level.
            initial_stop: Original stop-loss price.
            direction: ``"buy"`` or ``"sell"``.
            candles: Recent candle data for Chandelier.
            atr_value: Current ATR value.
            bars_in_trade: How many bars this trade has been open.
            tp_state: Dict with ``tp1_hit`` and ``tp2_hit`` bools.
            display_precision: Decimal places for Oanda price strings.

        Returns:
            Dict with ``should_update``, ``new_stop`` (str or None),
            ``method_used``, ``reason``, ``profit_r``, ``at_profit_lock``.
        """
        risk = abs(entry_price - initial_stop)
        if risk == 0:
            return {
                "should_update": False,
                "new_stop": None,
                "method_used": "none",
                "reason": "zero_risk",
                "profit_r": 0.0,
                "at_profit_lock": False,
            }

        # Current profit in R multiples
        if direction == "buy":
            profit_r = (current_price - entry_price) / risk
        else:
            profit_r = (entry_price - current_price) / risk

        # --- Candidate stops from each method ---
        candidates = []  # (stop_price, method_name, reason)

        # 1. Profit lock at 0.25R when profit >= 1.0R
        if direction == "buy":
            lock_price = entry_price + 0.25 * risk
        else:
            lock_price = entry_price - 0.25 * risk

        at_profit_lock = False
        if profit_r >= 1.0:
            at_profit_lock = True
            if direction == "buy" and current_stop < lock_price:
                candidates.append((lock_price, "profit_lock", "profit_reached_1R_locking_0.25R"))
            elif direction == "sell" and current_stop > lock_price:
                candidates.append((lock_price, "profit_lock", "profit_reached_1R_locking_0.25R"))

        # 2. Chandelier Exit
        if tp_state.get("tp2_hit"):
            chand_mult = 2.0  # Tight after TP2
        elif tp_state.get("tp1_hit"):
            chand_mult = 3.0  # Standard after TP1
        else:
            chand_mult = 3.0

        # Time-based decay overlay: after 24 bars, tighten multiplier
        if bars_in_trade > 24:
            progress = min(1.0, bars_in_trade / 48.0)
            chand_mult = chand_mult - (progress * (chand_mult - 0.75))

        if len(candles) >= 3:
            chand = self.chandelier_exit(
                candles, direction,
                multiplier=chand_mult,
                display_precision=display_precision,
            )
            chand_stop = chand["trail_stop_float"]
            method = "chandelier"
            if bars_in_trade > 24:
                method = "time_decay"
            candidates.append((chand_stop, method, f"chandelier_mult_{chand_mult:.2f}"))

        # --- Pick the tightest (highest for buy, lowest for sell) ---
        best_stop = None
        best_method = "none"
        best_reason = "no_candidates"

        for stop_val, method, reason in candidates:
            if direction == "buy":
                if best_stop is None or stop_val > best_stop:
                    best_stop = stop_val
                    best_method = method
                    best_reason = reason
            else:
                if best_stop is None or stop_val < best_stop:
                    best_stop = stop_val
                    best_method = method
                    best_reason = reason

        # Never move stop backwards
        if best_stop is not None:
            if direction == "buy" and best_stop <= current_stop:
                best_stop = None
            elif direction == "sell" and best_stop >= current_stop:
                best_stop = None

        should_update = best_stop is not None
        new_stop = _fmt(best_stop, display_precision) if best_stop else None

        return {
            "should_update": should_update,
            "new_stop": new_stop,
            "method_used": best_method if should_update else "none",
            "reason": best_reason if should_update else "stop_not_advanced",
            "profit_r": profit_r,
            "at_profit_lock": at_profit_lock,
        }

    # ------------------------------------------------------------------
    # Method 7: Safety validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_stop_placement(
        entry_price: float,
        stop_price: float,
        take_profit_price: float,
        direction: str,
        min_rr_ratio: float = 2.0,
    ) -> Dict:
        """Validate that stop/TP placement is correct and meets R:R minimum.

        Args:
            entry_price: Trade entry price.
            stop_price: Proposed stop-loss price.
            take_profit_price: Proposed take-profit price.
            direction: ``"buy"`` or ``"sell"``.
            min_rr_ratio: Minimum acceptable R:R ratio (default 2.0).

        Returns:
            Dict with ``valid`` (bool), ``issues`` (list of strings),
            ``actual_rr_ratio`` (float).
        """
        issues = []
        risk = abs(entry_price - stop_price)
        reward = abs(take_profit_price - entry_price)

        # Stop on correct side
        if direction == "buy":
            if stop_price >= entry_price:
                issues.append("Stop must be below entry for a buy trade")
            if take_profit_price <= entry_price:
                issues.append("Take profit must be above entry for a buy trade")
        else:
            if stop_price <= entry_price:
                issues.append("Stop must be above entry for a sell trade")
            if take_profit_price >= entry_price:
                issues.append("Take profit must be below entry for a sell trade")

        # Stop distance > 0
        if risk <= 0:
            issues.append("Stop distance must be greater than zero")

        # R:R check
        actual_rr = reward / risk if risk > 0 else 0.0
        if actual_rr < min_rr_ratio:
            issues.append(
                f"R:R ratio {actual_rr:.2f} is below minimum {min_rr_ratio:.1f}"
            )

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "actual_rr_ratio": actual_rr,
        }
