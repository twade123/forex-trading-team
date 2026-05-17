"""
Position Manager — live trade management with all 12 exit rules.

Monitors open positions and decides whether to hold, tighten, partial exit,
or kill. Mirrors the V3 backtester exit logic plus live-only rules.

Exit Rules:
  1.  Take Profit hit
  2.  Stop Loss hit
  3.  Trailing Stop (move SL to breakeven at 1× risk)
  4.  Partial Exit (close half at 1:1 R:R)
  5.  Max Hold Time (50 candles / ~2 days on H1)
  6.  Ambiguous candle (SL+TP hit same candle — conservative SL-first)
  7.  Regime Change (hostile regime mid-trade)
  8.  News Override (high-impact event within 30 min)
  9.  Correlation Exposure (too much same-direction risk)
  10. Performance Drift (setup underperforming vs backtest)
  11. Session Boundary (setup bad in current session)
  12. Spread Widening (spread > 2× normal)

Usage::

    from Source.position_manager import PositionManager

    pm = PositionManager()
    actions = pm.check_positions(open_positions, market_state)
    # actions = [{"trade_id": "...", "action": "CLOSE", "reason": "..."}, ...]
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trading_bot.position_manager")

# Lazy singletons
_trading_db = None


def _get_trading_db():
    global _trading_db
    if _trading_db is None:
        try:
            from Source.backtester.trading_db import TradingDB
            _trading_db = TradingDB()
        except Exception as e:
            logger.warning("TradingDB not available: %s", e)
    return _trading_db


# ======================================================================
# Configuration
# ======================================================================

# Spread thresholds (pips) — 2× normal triggers warning
NORMAL_SPREADS = {
    "EUR_USD": 1.2, "GBP_USD": 1.5, "USD_JPY": 1.5,
    "USD_CHF": 1.5, "AUD_USD": 1.5, "NZD_USD": 1.8,
    "USD_CAD": 1.8, "EUR_GBP": 2.0, "EUR_JPY": 2.0,
    "GBP_JPY": 2.5, "EUR_AUD": 2.5, "EUR_CHF": 2.0,
    "AUD_JPY": 2.0,
}

JPY_PAIRS = {"USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"}

MAX_HOLD_CANDLES = 50  # ~2 days on H1

# Correlation pairs — these move together, double risk if same direction
CORRELATED_PAIRS = {
    ("EUR_USD", "GBP_USD"): 0.87,
    ("EUR_USD", "EUR_GBP"): -0.75,
    ("AUD_USD", "NZD_USD"): 0.92,
    ("USD_CHF", "EUR_USD"): -0.85,
    ("EUR_JPY", "USD_JPY"): 0.82,
    ("GBP_JPY", "USD_JPY"): 0.80,
}

# Hostile regimes per setup type
HOSTILE_REGIMES = {
    # Trend-following setups hate ranging
    "S1": ["ranging", "squeeze"],
    "S2": ["ranging", "squeeze"],
    "S3": ["ranging"],
    "S6": ["ranging", "squeeze"],
    "S10": ["ranging"],
    # Mean-reversion setups hate strong trends
    "S5": ["strong_trend"],
    "S8": ["strong_trend"],
    "S11": ["strong_trend"],
    "S13": ["strong_trend"],
    "S15": ["strong_trend"],
    "S16": ["strong_trend"],
    # Volatility setups hate squeezes
    "S14": ["squeeze"],
    "S19": ["squeeze"],
}

# Session performance — which setups are bad in which sessions
# Populated from backtest data (can be overridden)
BAD_SESSIONS = {
    # Format: "base_setup": ["bad_session1", ...]
    # Will be loaded from DB on init
}


@dataclass
class PositionAction:
    """Action to take on a position."""
    trade_id: str
    action: str  # HOLD, CLOSE, TIGHTEN_SL, PARTIAL_EXIT, MOVE_TO_BE
    reason: str
    rule: str  # which of the 12 rules triggered
    urgency: str = "normal"  # normal, high, critical
    new_sl: float = None  # for TIGHTEN_SL / MOVE_TO_BE
    close_fraction: float = None  # for PARTIAL_EXIT (e.g., 0.5)
    details: Dict = field(default_factory=dict)


@dataclass
class OpenPosition:
    """Represents an open live trade."""
    trade_id: str
    pair: str
    direction: str  # buy / sell
    setup: str  # e.g., "S15_rr2.0_sl2.5"
    regime_at_entry: str
    entry_price: float
    entry_time: str  # ISO timestamp
    sl_price: float
    tp_price: float
    original_sl_price: float  # before any trailing
    units: int = 0
    candles_held: int = 0
    session_at_entry: str = ""
    be_triggered: bool = False
    partial_exit_done: bool = False
    max_favorable_pips: float = 0.0
    max_adverse_pips: float = 0.0
    decision_id: str = ""
    h4_agrees: bool = True


class PositionManager:
    """Manages open positions with all 12 exit rules."""

    def __init__(self):
        self._db = None
        self._bad_sessions_loaded = False

    @property
    def db(self):
        if self._db is None:
            self._db = _get_trading_db()
        return self._db

    def _load_bad_sessions(self):
        """Load session performance data from DB to identify bad sessions."""
        if self._bad_sessions_loaded:
            return
        self._bad_sessions_loaded = True

        db = self.db
        if not db:
            return

        try:
            # Use the pre-aggregated performance table (39K rows) instead of 8.5M trades
            rows = db.conn.execute("""
                SELECT setup, 
                       CASE 
                         WHEN setup LIKE '%_rr%' THEN SUBSTR(setup, 1, INSTR(setup, '_rr') - 1)
                         ELSE setup 
                       END as base_setup,
                       regime,
                       trade_count as cnt, win_rate as wr
                FROM backtest_setup_performance
                WHERE trade_count >= 20 AND win_rate < 40
            """).fetchall()

            # Note: this uses regime as proxy since session isn't in the performance table
            # The actual session-level analysis needs backtest_trades — we'll cache it
            # in a separate background job. For now, use the HOSTILE_REGIMES dict above.
            logger.info("Bad sessions: using HOSTILE_REGIMES config (%d setups)", len(HOSTILE_REGIMES))
        except Exception as e:
            logger.warning("Failed to load bad sessions: %s", e)

    def _pip_multiplier(self, pair: str) -> float:
        return 100.0 if pair in JPY_PAIRS else 10000.0

    def check_positions(
        self,
        positions: List[OpenPosition],
        market_state: Dict[str, Any],
    ) -> List[PositionAction]:
        """Check all open positions and return actions.

        Args:
            positions: List of OpenPosition objects.
            market_state: Current market data:
                {
                    "prices": {"EUR_USD": {"bid": 1.0850, "ask": 1.0852, "spread_pips": 1.2}},
                    "regimes": {"EUR_USD": "ranging"},
                    "sessions": {"EUR_USD": "London"},
                    "upcoming_news": [{"currencies_affected": "EUR,USD", ...}],
                    "candle_data": {"EUR_USD": {"high": ..., "low": ..., "close": ...}},
                }

        Returns:
            List of PositionAction objects.
        """
        self._load_bad_sessions()
        actions = []

        for pos in positions:
            pos_actions = self._check_single_position(pos, market_state, positions)
            actions.extend(pos_actions)

        # Sort by urgency: critical first
        urgency_order = {"critical": 0, "high": 1, "normal": 2}
        actions.sort(key=lambda a: urgency_order.get(a.urgency, 2))

        return actions

    def _check_single_position(
        self,
        pos: OpenPosition,
        market_state: Dict[str, Any],
        all_positions: List[OpenPosition],
    ) -> List[PositionAction]:
        """Run all 12 rules on a single position."""
        actions = []
        pair_prices = market_state.get("prices", {}).get(pos.pair, {})
        candle = market_state.get("candle_data", {}).get(pos.pair, {})
        current_regime = market_state.get("regimes", {}).get(pos.pair, "unknown")
        current_session = market_state.get("sessions", {}).get(pos.pair, "")
        pip_mult = self._pip_multiplier(pos.pair)

        bid = pair_prices.get("bid", 0)
        ask = pair_prices.get("ask", 0)
        high = candle.get("high", bid)
        low = candle.get("low", bid)
        current_price = bid if pos.direction == "buy" else ask

        if not current_price:
            return actions

        # Update MFE / MAE
        if pos.direction == "buy":
            favorable = (high - pos.entry_price) * pip_mult
            adverse = (pos.entry_price - low) * pip_mult
        else:
            favorable = (pos.entry_price - low) * pip_mult
            adverse = (high - pos.entry_price) * pip_mult
        pos.max_favorable_pips = max(pos.max_favorable_pips, favorable)
        pos.max_adverse_pips = max(pos.max_adverse_pips, adverse)

        risk_pips = abs(pos.entry_price - pos.original_sl_price) * pip_mult

        # ==========================================================
        # RULE 1: Take Profit
        # ==========================================================
        tp_hit = False
        if pos.direction == "buy" and high >= pos.tp_price:
            tp_hit = True
        elif pos.direction == "sell" and low <= pos.tp_price:
            tp_hit = True

        if tp_hit:
            actions.append(PositionAction(
                trade_id=pos.trade_id, action="CLOSE",
                reason=f"TP hit at {pos.tp_price:.5f}",
                rule="rule_1_take_profit", urgency="high",
            ))
            return actions  # Trade is done

        # ==========================================================
        # RULE 2: Stop Loss
        # ==========================================================
        sl_hit = False
        if pos.direction == "buy" and low <= pos.sl_price:
            sl_hit = True
        elif pos.direction == "sell" and high >= pos.sl_price:
            sl_hit = True

        if sl_hit:
            actions.append(PositionAction(
                trade_id=pos.trade_id, action="CLOSE",
                reason=f"SL hit at {pos.sl_price:.5f}",
                rule="rule_2_stop_loss", urgency="critical",
            ))
            return actions  # Trade is done

        # ==========================================================
        # RULE 3: Trailing Stop (Breakeven Move)
        # ==========================================================
        if not pos.be_triggered and risk_pips > 0:
            if pos.max_favorable_pips >= 1.0 * risk_pips:
                pos.be_triggered = True
                actions.append(PositionAction(
                    trade_id=pos.trade_id, action="MOVE_TO_BE",
                    reason=f"Price moved {pos.max_favorable_pips:.1f} pips in favor (1× risk), moving SL to breakeven",
                    rule="rule_3_trailing_stop",
                    new_sl=pos.entry_price,
                ))
                pos.sl_price = pos.entry_price

        # ==========================================================
        # RULE 4: Partial Exit (1:1 R:R)
        # ==========================================================
        if not pos.partial_exit_done and risk_pips > 0:
            tp1_pips = risk_pips  # 1:1 R:R
            if pos.direction == "buy":
                tp1_price = pos.entry_price + (tp1_pips / pip_mult)
                if high >= tp1_price:
                    pos.partial_exit_done = True
                    actions.append(PositionAction(
                        trade_id=pos.trade_id, action="PARTIAL_EXIT",
                        reason=f"1:1 R:R hit — closing 50% at {tp1_price:.5f}",
                        rule="rule_4_partial_exit",
                        close_fraction=0.5,
                    ))
            else:
                tp1_price = pos.entry_price - (tp1_pips / pip_mult)
                if low <= tp1_price:
                    pos.partial_exit_done = True
                    actions.append(PositionAction(
                        trade_id=pos.trade_id, action="PARTIAL_EXIT",
                        reason=f"1:1 R:R hit — closing 50% at {tp1_price:.5f}",
                        rule="rule_4_partial_exit",
                        close_fraction=0.5,
                    ))

        # ==========================================================
        # RULE 5: Max Hold Time
        # ==========================================================
        if pos.candles_held >= MAX_HOLD_CANDLES:
            actions.append(PositionAction(
                trade_id=pos.trade_id, action="CLOSE",
                reason=f"Max hold time ({MAX_HOLD_CANDLES} candles) exceeded",
                rule="rule_5_max_hold", urgency="high",
            ))
            return actions

        # ==========================================================
        # RULE 7: Regime Change (hostile regime mid-trade)
        # ==========================================================
        base_setup = pos.setup.split("_rr")[0] if "_rr" in pos.setup else pos.setup
        hostile = HOSTILE_REGIMES.get(base_setup, [])
        if current_regime in hostile and current_regime != pos.regime_at_entry:
            # Only trigger if regime CHANGED (don't kill trades that entered in this regime)
            pnl_pips = (current_price - pos.entry_price) * pip_mult if pos.direction == "buy" \
                else (pos.entry_price - current_price) * pip_mult

            if pnl_pips < 0:
                # Losing + hostile regime = kill it
                actions.append(PositionAction(
                    trade_id=pos.trade_id, action="CLOSE",
                    reason=f"Regime changed to {current_regime} (hostile for {base_setup}), currently {pnl_pips:.1f} pips",
                    rule="rule_7_regime_change", urgency="high",
                ))
            else:
                # Winning but hostile regime = tighten stop
                new_sl = pos.entry_price  # at least breakeven
                actions.append(PositionAction(
                    trade_id=pos.trade_id, action="TIGHTEN_SL",
                    reason=f"Regime changed to {current_regime} (hostile), tightening SL to BE while in profit",
                    rule="rule_7_regime_change",
                    new_sl=new_sl,
                ))

        # ==========================================================
        # RULE 8: News Override (high-impact event within 30 min)
        # ==========================================================
        upcoming_news = market_state.get("upcoming_news", [])
        pair_currencies = set(pos.pair.replace("_", ""))  # e.g., {"EUR", "USD"}
        # Actually split properly
        pair_currencies = set(pos.pair.split("_"))

        relevant_news = []
        for event in upcoming_news:
            affected = set((event.get("currencies_affected") or "").split(","))
            if pair_currencies & affected:
                relevant_news.append(event)

        if relevant_news:
            pnl_pips = (current_price - pos.entry_price) * pip_mult if pos.direction == "buy" \
                else (pos.entry_price - current_price) * pip_mult

            if pnl_pips > 0:
                # In profit with news coming — tighten SL to lock in
                actions.append(PositionAction(
                    trade_id=pos.trade_id, action="TIGHTEN_SL",
                    reason=f"High-impact news in <30min ({relevant_news[0].get('headline', 'unknown')}), locking profit",
                    rule="rule_8_news_override",
                    new_sl=pos.entry_price,  # at least breakeven
                    details={"events": [e.get("headline") for e in relevant_news[:3]]},
                ))
            else:
                # Losing with news coming — close to avoid volatility
                actions.append(PositionAction(
                    trade_id=pos.trade_id, action="CLOSE",
                    reason=f"High-impact news in <30min, currently losing — exiting to avoid volatility",
                    rule="rule_8_news_override", urgency="high",
                    details={"events": [e.get("headline") for e in relevant_news[:3]]},
                ))

        # ==========================================================
        # RULE 9: Correlation Exposure
        # ==========================================================
        other_same_dir = [
            p for p in all_positions
            if p.trade_id != pos.trade_id and p.direction == pos.direction
        ]
        for other in other_same_dir:
            pair_key = tuple(sorted([pos.pair, other.pair]))
            correlation = CORRELATED_PAIRS.get(pair_key)
            if correlation and abs(correlation) > 0.7:
                actions.append(PositionAction(
                    trade_id=pos.trade_id, action="TIGHTEN_SL",
                    reason=f"Correlated with {other.pair} (r={correlation:.2f}), same direction — tighten SL",
                    rule="rule_9_correlation",
                    new_sl=pos.entry_price if pos.be_triggered else None,
                    details={"correlated_pair": other.pair, "correlation": correlation},
                ))

        # ==========================================================
        # RULE 10: Performance Drift
        # ==========================================================
        db = self.db
        if db and pos.candles_held % 10 == 0 and pos.candles_held > 0:
            # Check every 10 candles
            try:
                drift = db.check_performance_drift(pos.pair, pos.setup, pos.regime_at_entry)
                if drift.get("alert"):
                    actions.append(PositionAction(
                        trade_id=pos.trade_id, action="TIGHTEN_SL",
                        reason=drift.get("alert_message", "Performance drift detected"),
                        rule="rule_10_performance_drift",
                        new_sl=pos.entry_price,
                        details=drift,
                    ))
            except Exception as e:
                # 2026-04-24: upgraded from debug. Drift check failure means
                # rule_10_performance_drift can't fire TIGHTEN_SL — losing a
                # risk-management safety net silently is bad.
                logger.warning("Drift check failed (rule_10 skipped) for %s %s: %s: %s",
                               pos.pair, pos.setup, type(e).__name__, e)

        # ==========================================================
        # RULE 11: Session Boundary
        # ==========================================================
        if current_session and current_session != pos.session_at_entry:
            if base_setup in BAD_SESSIONS and current_session in BAD_SESSIONS[base_setup]:
                pnl_pips = (current_price - pos.entry_price) * pip_mult if pos.direction == "buy" \
                    else (pos.entry_price - current_price) * pip_mult

                if pnl_pips < risk_pips * 0.3:
                    # Not much profit, bad session coming — tighten
                    actions.append(PositionAction(
                        trade_id=pos.trade_id, action="TIGHTEN_SL",
                        reason=f"Entering {current_session} (historically bad for {base_setup}), tightening SL",
                        rule="rule_11_session_boundary",
                        new_sl=pos.entry_price if pos.be_triggered else None,
                    ))

        # ==========================================================
        # RULE 12: Spread Widening
        # ==========================================================
        current_spread = pair_prices.get("spread_pips", 0)
        normal_spread = NORMAL_SPREADS.get(pos.pair, 2.0)
        if current_spread > normal_spread * 2:
            pnl_pips = (current_price - pos.entry_price) * pip_mult if pos.direction == "buy" \
                else (pos.entry_price - current_price) * pip_mult

            if pnl_pips > 0:
                # In profit, spread widening — close to lock in
                actions.append(PositionAction(
                    trade_id=pos.trade_id, action="CLOSE",
                    reason=f"Spread widened to {current_spread:.1f} pips (normal: {normal_spread:.1f}), locking profit",
                    rule="rule_12_spread_widening", urgency="high",
                    details={"current_spread": current_spread, "normal_spread": normal_spread},
                ))
            elif current_spread > normal_spread * 3:
                # Extreme spread — close regardless
                actions.append(PositionAction(
                    trade_id=pos.trade_id, action="CLOSE",
                    reason=f"Extreme spread: {current_spread:.1f} pips (3× normal), closing all",
                    rule="rule_12_spread_widening", urgency="critical",
                ))

        # ==========================================================
        # Default: HOLD
        # ==========================================================
        if not actions:
            actions.append(PositionAction(
                trade_id=pos.trade_id, action="HOLD",
                reason="All rules passed — no action needed",
                rule="hold",
            ))

        return actions

    def deduplicate_actions(self, actions: List[PositionAction]) -> List[PositionAction]:
        """When multiple rules fire, pick the most aggressive action per trade.

        Priority: CLOSE > PARTIAL_EXIT > TIGHTEN_SL > MOVE_TO_BE > HOLD
        """
        action_priority = {"CLOSE": 0, "PARTIAL_EXIT": 1, "TIGHTEN_SL": 2, "MOVE_TO_BE": 3, "HOLD": 4}
        by_trade: Dict[str, PositionAction] = {}

        for action in actions:
            if action.action == "HOLD":
                continue
            existing = by_trade.get(action.trade_id)
            if existing is None or action_priority.get(action.action, 5) < action_priority.get(existing.action, 5):
                by_trade[action.trade_id] = action

        return list(by_trade.values())
