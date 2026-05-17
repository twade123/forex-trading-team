"""
Position sizing engine with regime-adaptive, spread-adjusted, margin-aware calculations.

Implements the Fixed Fractional method -- the industry standard for systematic
forex trading.  Each call computes the number of Oanda units to trade given the
current account balance, per-trade risk percentage, ATR-based stop distance,
market regime, spread cost, and available margin.

Core formula (RMGT-01)::

    risk_amount = balance * risk_pct
    stop_distance = atr_value * regime_multiplier * volatility_adjustment
    effective_stop = stop_distance_pips + spread_pips
    pip_value_per_unit = f(pair_type, current_price)
    units = floor(risk_amount / (effective_stop * pip_value_per_unit))

Compounding (RMGT-02): uses current balance (not NAV, not initial deposit)
so position sizes naturally grow as the account grows and shrink on drawdowns.

Usage::

    from trading_bot.source.position_sizer import PositionSizer

    ps = PositionSizer()
    result = ps.calculate_position_size(
        balance=500.0, risk_pct=0.01, atr_value=0.00185,
        pip_size=0.0001, current_price=1.0850, instrument="EUR_USD",
        regime="mixed", spread_pips=1.8,
    )
    print(result["units"])  # integer units ready for Oanda order
"""

import logging
import math
from typing import Dict, Optional

logger = logging.getLogger("trading_bot.position_sizer")

# ---------------------------------------------------------------------------
# Regime-adaptive ATR stop multipliers (RMGT-03)
# ---------------------------------------------------------------------------
REGIME_MULTIPLIERS: Dict[str, float] = {
    "trending": 2.0,   # Trends need room to breathe (ADX > 25)
    "ranging": 1.2,    # Tighter in ranges (ADX < 20)
    "mixed": 1.5,      # Default (ADX 20-25)
}


class PositionSizer:
    """Dynamic position sizing based on account balance, risk, and ATR.

    Research-informed implementation using Fixed Fractional method -- the
    industry standard for systematic forex trading.

    Core formula (RMGT-01)::

        risk_amount = balance * risk_pct
        stop_distance = atr_value * regime_multiplier * volatility_adjustment
        effective_stop = stop_distance_pips + spread_pips
        pip_value_per_unit = f(pair_type, current_price)
        units = floor(risk_amount / (effective_stop * pip_value_per_unit))

    Compounding (RMGT-02): uses current balance (not NAV, not initial deposit)
    so position sizes naturally grow as the account grows and shrink on
    drawdowns.

    This class is pure calculation -- no state, no API calls, no file I/O.
    All prices are expected as floats (conversion from Oanda strings happens
    at the call site).  pip_size is derived from the instrument spec's
    ``pipLocation``: ``pip_size = 10 ** pipLocation``.
    """

    # ------------------------------------------------------------------
    # Regime multiplier
    # ------------------------------------------------------------------

    @staticmethod
    def get_regime_multiplier(regime: str, news_active: bool = False) -> float:
        """Return the ATR stop multiplier for the given market regime.

        Args:
            regime: Market regime from ConfluenceScorer.get_regime() --
                one of ``"trending"``, ``"ranging"``, ``"mixed"``.
            news_active: Whether high-impact news is within 30 minutes.

        Returns:
            ATR multiplier (float).  When *news_active* is True the floor
            is 2.5 regardless of regime.
        """
        mult = REGIME_MULTIPLIERS.get(regime, REGIME_MULTIPLIERS["mixed"])
        if news_active:
            mult = max(mult, 2.5)
        return mult

    # ------------------------------------------------------------------
    # Volatility-regime adjustment
    # ------------------------------------------------------------------

    @staticmethod
    def get_volatility_adjustment(atr_14: float, atr_50: Optional[float]) -> float:
        """Adjust the ATR multiplier based on current-vs-historical volatility.

        Uses the ATR(14)/ATR(50) ratio to detect whether the market is in a
        higher-than-normal or lower-than-normal volatility regime and scales
        the stop accordingly.

        Args:
            atr_14: Current 14-period ATR value.
            atr_50: Longer 50-period ATR value for baseline comparison.
                If ``None`` or ``0``, returns 1.0 (no adjustment).

        Returns:
            Adjustment factor (float) in [0.85, 1.3].
        """
        if atr_50 is None or atr_50 == 0:
            return 1.0

        ratio = atr_14 / atr_50

        if ratio >= 1.5:
            return 1.3    # High vol -- widen stops
        if ratio >= 1.2:
            return 1.15   # Elevated vol
        if ratio <= 0.7:
            return 0.85   # Low vol / compression -- tighten
        if ratio <= 0.85:
            return 0.92   # Below-normal
        return 1.0        # Normal

    # ------------------------------------------------------------------
    # Pip value calculation
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_pip_value(
        instrument: str,
        pip_size: float,
        current_price: float,
        account_currency: str = "USD",
    ) -> float:
        """Return the pip value per unit for position sizing.

        Args:
            instrument: Oanda instrument name (e.g. ``"EUR_USD"``).
            pip_size: Size of one pip (e.g. 0.0001 for EUR/USD).
            current_price: Current instrument mid price.
            account_currency: Three-letter account currency code.

        Returns:
            Pip value per single unit (float).
        """
        base, quote = instrument.split("_")

        if quote == account_currency:
            # XXX_USD pairs -- pip value is simply pip_size
            return pip_size

        if base == account_currency:
            # USD_XXX pairs -- divide pip_size by current price
            if current_price <= 0:
                raise ValueError(
                    f"current_price must be > 0 for {instrument}, got {current_price}"
                )
            return pip_size / current_price

        # Cross pair -- approximation (not a precise home-conversion)
        logger.warning(
            "Cross pair %s: using pip_size / current_price approximation. "
            "For production, use Oanda HomeConversionFactors.",
            instrument,
        )
        if current_price <= 0:
            raise ValueError(
                f"current_price must be > 0 for {instrument}, got {current_price}"
            )
        return pip_size / current_price

    # ------------------------------------------------------------------
    # Core position sizing
    # ------------------------------------------------------------------

    def calculate_position_size(
        self,
        balance: float,
        risk_pct: float,
        atr_value: float,
        pip_size: float,
        current_price: float,
        instrument: str,
        regime: str,
        spread_pips: float,
        atr_50: Optional[float] = None,
        news_active: bool = False,
        margin_available: Optional[float] = None,
        margin_rate: float = 0.02,
        account_currency: str = "USD",
    ) -> Dict:
        """Calculate the number of units to trade.

        This is the primary entry point.  It combines regime-adaptive ATR
        multipliers, volatility-regime adjustment, spread-adjusted stop
        distance, pair-type-aware pip-value, and margin utilisation checks
        to produce an integer unit count ready for Oanda order submission.

        Args:
            balance: Current account balance (float, e.g. 500.0).
            risk_pct: Risk per trade as a decimal (e.g. 0.01 for 1%).
            atr_value: Current ATR(14) value in **price units**.
            pip_size: Size of one pip (e.g. 0.0001 for EUR/USD).
            current_price: Current mid price for the instrument.
            instrument: Oanda instrument name (e.g. ``"EUR_USD"``).
            regime: Market regime (``"trending"``/``"ranging"``/``"mixed"``).
            spread_pips: Current spread in pips.
            atr_50: ATR(50) for volatility-regime adjustment (optional).
            news_active: Whether high-impact news is within 30 min.
            margin_available: Available margin for utilisation check.
            margin_rate: Instrument margin rate (default 0.02 = 50:1).
            account_currency: Account currency code (default ``"USD"``).

        Returns:
            Dict with ``units``, ``risk_amount``, ``stop_distance_pips``,
            ``stop_distance_price``, ``pip_value_per_unit``,
            ``regime_multiplier``, ``volatility_adjustment``,
            ``effective_atr_multiplier``, ``spread_adjustment_pips``,
            ``margin_required``, ``margin_utilization_pct``,
            ``risk_pct``, and ``balance``.

        Raises:
            ValueError: If ``atr_value`` is <= 0 (bad data -- never trade).
        """
        # Guard: bad balance
        if balance <= 0:
            logger.warning("Balance <= 0 (%s). Returning 0 units.", balance)
            return self._zero_result(
                balance=balance,
                risk_pct=risk_pct,
                reason="balance_zero_or_negative",
            )

        # Guard: bad ATR
        if atr_value <= 0:
            raise ValueError(
                f"ATR value must be > 0 (got {atr_value}). "
                "Refusing to size a position on bad data."
            )

        # 1. Determine ATR multiplier
        regime_mult = self.get_regime_multiplier(regime, news_active)
        vol_adj = self.get_volatility_adjustment(atr_value, atr_50)
        effective_mult = regime_mult * vol_adj

        # 2. Calculate stop distance
        stop_distance_price = atr_value * effective_mult
        stop_distance_pips = stop_distance_price / pip_size

        # 3. Add spread to effective stop (research: ignoring spread costs 0.1-0.3%)
        effective_stop_pips = stop_distance_pips + spread_pips

        # 4. Calculate pip value
        pip_value = self.calculate_pip_value(
            instrument, pip_size, current_price, account_currency
        )

        # 5. Calculate units
        risk_amount = balance * risk_pct
        if effective_stop_pips <= 0 or pip_value <= 0:
            raise ValueError(
                f"Invalid stop distance ({effective_stop_pips} pips) "
                f"or pip value ({pip_value})."
            )
        raw_units = risk_amount / (effective_stop_pips * pip_value)

        # 6. Apply safety constraints
        units = max(1, int(raw_units))  # Min 1 unit, floor rounding

        # 10:1 effective leverage cap
        denom = current_price if current_price > 0 else 1.0
        max_units = int(balance * 10 / denom)
        units = min(units, max(1, max_units))

        # 7. Margin utilisation check (cap at 50%)
        margin_required = units * current_price * margin_rate
        margin_utilization_pct = 0.0

        if margin_available is not None and margin_available > 0:
            margin_utilization_pct = (margin_required / margin_available) * 100.0
            if margin_required > margin_available * 0.5:
                adjusted = int(margin_available * 0.5 / (current_price * margin_rate))
                units = max(1, adjusted)
                margin_required = units * current_price * margin_rate
                margin_utilization_pct = (
                    (margin_required / margin_available) * 100.0
                )
                logger.info(
                    "Margin cap applied: units reduced to %d (%.1f%% utilisation).",
                    units,
                    margin_utilization_pct,
                )

        logger.debug(
            "Position sized: %d units | balance=%.2f risk_pct=%.4f "
            "regime=%s effective_mult=%.3f stop=%.2f pips spread=%.2f pips",
            units,
            balance,
            risk_pct,
            regime,
            effective_mult,
            stop_distance_pips,
            spread_pips,
        )

        return {
            "units": units,
            "risk_amount": risk_amount,
            "stop_distance_pips": stop_distance_pips,
            "stop_distance_price": stop_distance_price,
            "pip_value_per_unit": pip_value,
            "regime_multiplier": regime_mult,
            "volatility_adjustment": vol_adj,
            "effective_atr_multiplier": effective_mult,
            "spread_adjustment_pips": spread_pips,
            "margin_required": margin_required,
            "margin_utilization_pct": margin_utilization_pct,
            "risk_pct": risk_pct,
            "balance": balance,
        }

    # ------------------------------------------------------------------
    # Risk-reward helper
    # ------------------------------------------------------------------

    @staticmethod
    def calculate_risk_reward(
        entry_price: float,
        stop_price: float,
        take_profit_price: float,
    ) -> Dict:
        """Calculate risk, reward, and R:R ratio for a trade setup.

        Args:
            entry_price: Entry price.
            stop_price: Stop-loss price.
            take_profit_price: Take-profit price.

        Returns:
            Dict with ``risk_pips``, ``reward_pips``, ``rr_ratio``.
        """
        risk = abs(entry_price - stop_price)
        reward = abs(take_profit_price - entry_price)
        rr_ratio = reward / risk if risk > 0 else 0.0
        return {
            "risk_pips": risk,
            "reward_pips": reward,
            "rr_ratio": rr_ratio,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _zero_result(
        balance: float, risk_pct: float, reason: str
    ) -> Dict:
        """Return a zero-units result dict for edge cases."""
        return {
            "units": 0,
            "risk_amount": 0.0,
            "stop_distance_pips": 0.0,
            "stop_distance_price": 0.0,
            "pip_value_per_unit": 0.0,
            "regime_multiplier": 0.0,
            "volatility_adjustment": 0.0,
            "effective_atr_multiplier": 0.0,
            "spread_adjustment_pips": 0.0,
            "margin_required": 0.0,
            "margin_utilization_pct": 0.0,
            "risk_pct": risk_pct,
            "balance": balance,
            "reason": reason,
        }
