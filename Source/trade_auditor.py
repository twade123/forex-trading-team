"""
Trade Auditor — verifies trading signals against market reality.

Extension of the Reporter Agent's learning loop. The reporter logs what agents
*claim*; the auditor checks if those claims were *true*.

Three audit tiers:
  1. audit_trade()   — single trade verification (auto after every close)
  2. rolling_audit() — drift detection across last N trades (every 5th audit)
  3. thesis_audit()  — lifetime deep dive per thesis type (weekly)
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from db_pool import get_trading_forex

logger = logging.getLogger("trading_bot.auditor")

# Thresholds
ADVERSE_MOVE_THRESHOLD_PIPS = 3.0   # Guardian threat is "real" if >3 pips adverse follows
MISS_THRESHOLD_PIPS = 5.0           # Unwarned adverse move >5 pips = guardian miss
ROLLING_TRIGGER_EVERY = 5           # Run rolling audit every N trade audits
DRIFT_ALERT_THRESHOLD_PP = 15       # Flag if accuracy drops >15 percentage points


class _PooledConnWrapper:
    """Thin wrapper that makes .close() a no-op for pooled connections."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass  # pooled — do not close

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class TradeAuditor:
    """Verify trading signals against actual market data."""

    def __init__(self, db_path: str = None):
        self._db_path = db_path  # only used if caller passes a custom path
        self._ensure_tables()

    def _conn(self) -> sqlite3.Connection:
        """Return a connection. Pooled conns have .close() as a safe no-op."""
        if self._db_path:
            # Custom path — use short-lived connection (non-pooled)
            conn = sqlite3.connect(self._db_path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=DELETE")
            return conn
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        return _PooledConnWrapper(conn)  # .close() is safe no-op

    def _ensure_tables(self):
        conn = self._conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trade_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    trade_id TEXT,
                    pair TEXT NOT NULL,
                    direction TEXT,
                    setup_name TEXT,
                    entry_type TEXT,
                    outcome TEXT,
                    pnl_pips REAL,
                    pnl_usd REAL,

                    -- Signal verification
                    scout_claims TEXT,
                    reality_at_entry TEXT,
                    signal_matches TEXT,
                    scout_signal_accuracy REAL,
                    scout_thesis_correct INTEGER,

                    -- Guardian verification
                    guardian_assessments INTEGER,
                    guardian_correct_calls INTEGER,
                    guardian_false_alarms INTEGER,
                    guardian_misses INTEGER,
                    guardian_accuracy REAL,

                    -- Validator verification
                    validator_verdict TEXT,
                    validator_correct INTEGER,

                    -- Execution quality
                    max_favorable_pips REAL,
                    max_adverse_pips REAL,
                    entry_timing_score REAL,
                    exit_quality_score REAL,
                    time_to_max_favorable_min INTEGER,
                    time_in_drawdown_min INTEGER,

                    -- Market context
                    market_story_at_close TEXT,
                    what_changed TEXT,

                    -- Lookback
                    prior_5_accuracy REAL,
                    prior_5_wr REAL,
                    accuracy_trend TEXT,

                    -- Meta
                    audited_at TEXT DEFAULT (datetime('now')),
                    audit_version INTEGER DEFAULT 1,
                    audit_duration_ms INTEGER,

                    UNIQUE(cycle_id, trade_id)
                );

                CREATE TABLE IF NOT EXISTS audit_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_type TEXT NOT NULL,
                    trades_analyzed INTEGER,
                    overall_signal_accuracy REAL,
                    overall_thesis_accuracy REAL,
                    overall_guardian_accuracy REAL,
                    entry_timing_avg REAL,
                    exit_quality_avg REAL,
                    flags TEXT,
                    recommendations TEXT,
                    report_data TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_audit_pair ON trade_audits(pair);
                CREATE INDEX IF NOT EXISTS idx_audit_cycle ON trade_audits(cycle_id);
                CREATE INDEX IF NOT EXISTS idx_audit_entry_type ON trade_audits(entry_type);
                CREATE INDEX IF NOT EXISTS idx_audit_date ON trade_audits(audited_at);
            """)
            conn.commit()
        finally:
            conn.close()

    # ── Phase 1: Single Trade Audit ──

    def audit_trade(
        self,
        cycle_id: str,
        trade_id: str,
        pair: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        stop_loss: float,
        take_profit: float,
        pnl_pips: float,
        pnl_usd: float,
        setup_name: str = "",
        entry_type: str = "",
        entry_time: str = "",
        close_time: str = "",
        outcome: str = "",
        validator_verdict: str = "",
        scout_context: Dict = None,
        user_id: int = None,
    ) -> Dict[str, Any]:
        """Full post-trade verification against market reality.

        Returns audit result dict with accuracy scores.
        """
        audit_start = time.time()

        # Determine pip size
        pip_size = 0.01 if "JPY" in pair else 0.0001

        # ── Step 1: Get flight recorder data ──
        flight_data = self._get_flight_data(cycle_id)

        # ── Step 2: Fetch raw OANDA candles for verification ──
        entry_dt = self._parse_time(entry_time)
        close_dt = self._parse_time(close_time)
        candles_m15 = self._fetch_candles(pair, "M15", entry_dt - timedelta(hours=4),
                                           close_dt + timedelta(hours=2))
        candles_m1 = self._fetch_candles(pair, "M1", entry_dt - timedelta(minutes=30),
                                          close_dt + timedelta(minutes=30))

        # ── Step 3: Recompute market story at entry ──
        scout_claims, reality, signal_matches, signal_accuracy = \
            self._verify_signals(pair, candles_m15, entry_dt, flight_data,
                                 scout_context=scout_context)

        # ── Step 4: Verify thesis played out ──
        thesis_correct = self._verify_thesis(
            pair, entry_type, direction, candles_m15, entry_dt, close_dt, pip_size)

        # ── Step 5: Execution quality (MFE/MAE) ──
        mfe, mae, entry_timing, exit_quality, time_to_mfe, time_underwater = \
            self._compute_execution_quality(
                candles_m1, direction, entry_price, exit_price, entry_dt, close_dt, pip_size)

        # ── Step 6: Guardian verification ──
        g_total, g_correct, g_false, g_misses, g_accuracy = \
            self._verify_guardian(trade_id, candles_m1, direction, entry_dt, close_dt, pip_size)

        # ── Step 6b: Guardian action timeline (SL/TP modifications) ──
        guardian_actions = self._get_guardian_actions(trade_id)
        guardian_phases = self._get_guardian_phases(trade_id)

        # ── Step 7: Recompute story at close ──
        close_story, what_changed = self._story_at_close(
            pair, candles_m15, entry_dt, close_dt, scout_claims)

        # ── Step 8: Validator verification ──
        if not outcome:
            outcome = "win" if pnl_pips > 0 else "loss"
        validator_correct = self._verify_validator(validator_verdict, outcome)

        # ── Step 9: Prior-5 lookback ──
        prior_acc, prior_wr, acc_trend = self._prior_lookback(pair)

        audit_ms = int((time.time() - audit_start) * 1000)

        # ── Store ──
        result = {
            "cycle_id": cycle_id,
            "trade_id": trade_id,
            "user_id": user_id,
            "pair": pair,
            "direction": direction,
            "setup_name": setup_name,
            "entry_type": entry_type,
            "outcome": outcome,
            "pnl_pips": pnl_pips,
            "pnl_usd": pnl_usd,
            "scout_claims": scout_claims,
            "reality_at_entry": reality,
            "signal_matches": signal_matches,
            "scout_signal_accuracy": signal_accuracy,
            "scout_thesis_correct": thesis_correct,
            "guardian_assessments": g_total,
            "guardian_correct_calls": g_correct,
            "guardian_false_alarms": g_false,
            "guardian_misses": g_misses,
            "guardian_accuracy": g_accuracy,
            "guardian_actions": guardian_actions,
            "guardian_phases": guardian_phases,
            "guardian_sl_moves": len([a for a in guardian_actions if "sl" in a.get("action", "").lower()]),
            "guardian_tp_moves": len([a for a in guardian_actions if "tp" in a.get("action", "").lower()]),
            "validator_verdict": validator_verdict,
            "validator_correct": validator_correct,
            "max_favorable_pips": mfe,
            "max_adverse_pips": mae,
            "entry_timing_score": entry_timing,
            "exit_quality_score": exit_quality,
            "time_to_max_favorable_min": time_to_mfe,
            "time_in_drawdown_min": time_underwater,
            "market_story_at_close": close_story,
            "what_changed": what_changed,
            "prior_5_accuracy": prior_acc,
            "prior_5_wr": prior_wr,
            "accuracy_trend": acc_trend,
            "audit_duration_ms": audit_ms,
        }

        self._store_audit(result)

        # ── Learning Integration: write audit findings to vault ──
        try:
            from learning_integrator import LearningIntegrator
            integrator = LearningIntegrator()
            vault_learnings = integrator.process_trade_audit(result)
            retro = integrator.full_session_retrospective(result)
            result["learnings_written"] = vault_learnings
            result["session_retrospective"] = retro
            logger.info(
                "Trade audit → %d vault learnings written", len(vault_learnings))
        except Exception as e:
            logger.warning("Learning integration failed (non-fatal): %s", e)

        # Check if we should run rolling audit
        self._maybe_rolling_audit()

        logger.info(
            "Audit %s %s: signal_acc=%.0f%%, thesis=%s, entry_timing=%.0f, "
            "guardian_acc=%.0f%%, MFE=%.1f MAE=%.1f [%dms]",
            pair, outcome, signal_accuracy, "✓" if thesis_correct else "✗",
            entry_timing, g_accuracy, mfe, mae, audit_ms,
        )

        return result

    # ── Signal Verification ──

    def _verify_signals(
        self, pair: str, candles_m15: List[Dict], entry_dt: datetime,
        flight_data: Dict, scout_context: Dict = None,
    ) -> Tuple[Dict, Dict, Dict, float]:
        """Recompute market story from raw candles and compare to scout claims.

        When flight_data has no scout_scan (e.g. reconciled trades), falls back
        to scout_context dict which carries the original scout fields stored in
        live_trades at entry time.
        """
        scout_claims = {}
        reality = {}
        matches = {}

        # Extract scout claims from flight data
        scout_stage = flight_data.get("scout_scan", {})

        # Fallback: use scout_context from live_trades when flight_data is empty
        if not scout_stage and scout_context:
            logger.info("Using scout_context fallback for %s (reconciled trade)", pair)
            scout_stage = {
                "fan_state": scout_context.get("fan_state"),
                "fan_direction": scout_context.get("fan_direction"),
                "momentum_state": scout_context.get("momentum_state"),
                "momentum_exhausted": False,  # not stored in live_trades
                "e100_interaction": scout_context.get("e100_role", "none"),
                "entry_type": scout_context.get("story_entry_type") or scout_context.get("entry_type", "unknown"),
                "wick_pressure": "unknown",
                "body_trend": "unknown",
            }

        scout_claims = {
            "fan_state": scout_stage.get("fan_state", "unknown"),
            "fan_direction": scout_stage.get("fan_direction", "unknown"),
            "momentum_state": scout_stage.get("momentum_state", "unknown"),
            "momentum_exhausted": scout_stage.get("momentum_exhausted", False),
            "e100_interaction": scout_stage.get("e100_interaction", "none"),
            "entry_type": scout_stage.get("entry_type", "unknown"),
            "wick_pressure": scout_stage.get("wick_pressure", "unknown"),
            "body_trend": scout_stage.get("body_trend", "unknown"),
        }

        # Recompute from raw candles up to entry time
        entry_candles = self._candles_up_to(candles_m15, entry_dt)
        if len(entry_candles) < 20:
            logger.warning("Not enough candles for signal verification (%d)", len(entry_candles))
            return scout_claims, {}, {}, 0.0

        try:
            candle_dicts = self._oanda_to_dicts(entry_candles)

            from backtester.ema_separation import generate_market_picture
            from market_story import read_market_story

            mkt_picture = generate_market_picture(candle_dicts)
            story = read_market_story(pair, candle_dicts, mkt_picture)

            reality = {
                "fan_state": mkt_picture.get("fan_state", "unknown"),
                "fan_direction": mkt_picture.get("fan_direction", "unknown"),
                "momentum_state": story["layers"].get("momentum", {}).get("state", "unknown"),
                "momentum_exhausted": story["layers"].get("momentum", {}).get("exhausted", False),
                "e100_interaction": "none",  # from candle_structure
                "entry_type": story.get("entry_type", "none"),
                "wick_pressure": "unknown",
                "body_trend": "unknown",
            }

            # Candle structure for wick/body/E100
            try:
                from backtester.candle_structure import (
                    analyze_wick_rejection, analyze_body_progression,
                    analyze_ema_interaction,
                )
                wick = analyze_wick_rejection(candle_dicts[-20:])
                body = analyze_body_progression(candle_dicts[-10:])
                e100_vals = [c.get("ema_100", c.get("ema100", 0)) for c in candle_dicts[-5:]]
                if any(e100_vals):
                    ema_int = analyze_ema_interaction(candle_dicts[-10:], "ema_100")
                    reality["e100_interaction"] = ema_int.get("interaction", "none")
                reality["wick_pressure"] = wick.get("pressure", "unknown")
                reality["body_trend"] = body.get("trend", "unknown")
            except Exception as e:
                # 2026-04-24: upgraded — silent = reality fields missing in audit.
                logger.warning("Candle structure recompute FAILED: %s: %s (reality fields missing)",
                               type(e).__name__, e)

        except Exception as e:
            logger.warning("Signal recompute failed: %s", e)
            return scout_claims, {}, {}, 0.0

        # Compare
        fields_checked = 0
        fields_matched = 0
        for field in scout_claims:
            if field in reality and reality[field] not in ("unknown", "none", None):
                fields_checked += 1
                if scout_claims[field] == reality[field]:
                    matches[field] = True
                    fields_matched += 1
                else:
                    matches[field] = False
            else:
                matches[field] = None  # Can't verify

        accuracy = (fields_matched / fields_checked * 100) if fields_checked > 0 else 0.0
        return scout_claims, reality, matches, accuracy

    # ── Thesis Verification ──

    def _verify_thesis(
        self, pair: str, entry_type: str, direction: str,
        candles_m15: List[Dict], entry_dt: datetime, close_dt: datetime,
        pip_size: float,
    ) -> int:
        """Check if the thesis type actually played out in the candles."""
        post_entry = self._candles_after(candles_m15, entry_dt, count=6)
        if len(post_entry) < 2:
            return 0  # Can't verify

        is_buy = direction.lower() in ("buy", "bullish", "long")

        if entry_type == "counter_trend_reversal":
            # Price should move in our direction within 4 candles
            return self._check_directional_move(post_entry[:4], is_buy, pip_size, min_pips=3)

        elif entry_type == "trend_continuation":
            # Price should continue trending in our direction
            return self._check_directional_move(post_entry[:4], is_buy, pip_size, min_pips=2)

        elif entry_type == "e100_bounce":
            # Price should bounce (hold direction) after touching E100
            return self._check_directional_move(post_entry[:4], is_buy, pip_size, min_pips=2)

        elif entry_type == "breakout":
            # Price should follow through strongly
            return self._check_directional_move(post_entry[:6], is_buy, pip_size, min_pips=5)

        return 0  # Unknown thesis type

    def _check_directional_move(
        self, candles: List[Dict], is_buy: bool, pip_size: float, min_pips: float,
    ) -> int:
        """Check if price moved in expected direction by min_pips."""
        if not candles:
            return 0
        first_open = self._mid(candles[0], "o")
        best = first_open
        for c in candles:
            if is_buy:
                best = max(best, self._mid(c, "h"))
            else:
                best = min(best, self._mid(c, "l"))

        move_pips = abs(best - first_open) / pip_size
        if is_buy:
            return 1 if (best > first_open and move_pips >= min_pips) else 0
        else:
            return 1 if (best < first_open and move_pips >= min_pips) else 0

    # ── Execution Quality ──

    def _compute_execution_quality(
        self, candles_m1: List[Dict], direction: str, entry_price: float,
        exit_price: float, entry_dt: datetime, close_dt: datetime,
        pip_size: float,
    ) -> Tuple[float, float, float, float, int, int]:
        """Compute MFE, MAE, entry timing, exit quality."""
        is_buy = direction.lower() in ("buy", "bullish", "long")
        mfe = 0.0
        mae = 0.0
        mfe_time = None
        underwater_minutes = 0

        trade_candles = self._candles_between(candles_m1, entry_dt, close_dt)

        for c in trade_candles:
            high = self._mid(c, "h")
            low = self._mid(c, "l")

            if is_buy:
                fav = (high - entry_price) / pip_size
                adv = (entry_price - low) / pip_size
            else:
                fav = (entry_price - low) / pip_size
                adv = (high - entry_price) / pip_size

            if fav > mfe:
                mfe = fav
                mfe_time = c.get("time", "")
            mae = max(mae, adv)

            # Track time underwater (close below entry for buy, above for sell)
            close_price = self._mid(c, "c")
            if is_buy and close_price < entry_price:
                underwater_minutes += 1
            elif not is_buy and close_price > entry_price:
                underwater_minutes += 1

        # Entry timing: how good was our entry? MFE/(MFE+MAE)
        entry_timing = (mfe / (mfe + mae) * 100) if (mfe + mae) > 0 else 50.0

        # Exit quality: how much of the MFE did we capture?
        actual_pips = 0
        if exit_price:
            if is_buy:
                actual_pips = (exit_price - entry_price) / pip_size
            else:
                actual_pips = (entry_price - exit_price) / pip_size
        exit_quality = (actual_pips / mfe * 100) if mfe > 0 else 0.0
        exit_quality = max(0, min(100, exit_quality))

        # Time to MFE
        time_to_mfe = 0
        if mfe_time and entry_dt:
            mfe_dt = self._parse_time(mfe_time)
            if mfe_dt:
                time_to_mfe = int((mfe_dt - entry_dt).total_seconds() / 60)

        return mfe, mae, entry_timing, exit_quality, time_to_mfe, underwater_minutes

    # ── Guardian Verification ──

    def _verify_guardian(
        self, trade_id: str, candles_m1: List[Dict], direction: str,
        entry_dt: datetime, close_dt: datetime, pip_size: float,
    ) -> Tuple[int, int, int, int, float]:
        """Verify guardian threat assessments against actual price movement."""
        is_buy = direction.lower() in ("buy", "bullish", "long")

        # Get guardian threat records from flight_recorder
        threats = self._get_guardian_threats(trade_id)
        if not threats:
            return 0, 0, 0, 0, 0.0

        correct = 0
        false_alarms = 0
        total_escalations = 0

        for t in threats:
            zone = t.get("zone", "GREEN")
            if zone in ("YELLOW", "RED", "BLACK"):
                total_escalations += 1
                t_time = self._parse_time(t.get("timestamp", ""))
                if not t_time:
                    continue

                # Check next 15 M1 candles after threat
                after = self._candles_after(candles_m1, t_time, count=15)
                max_adverse = 0.0
                for c in after:
                    if is_buy:
                        adv = (self._mid(c, "o") - self._mid(c, "l")) / pip_size
                    else:
                        adv = (self._mid(c, "h") - self._mid(c, "o")) / pip_size
                    max_adverse = max(max_adverse, adv)

                if max_adverse > ADVERSE_MOVE_THRESHOLD_PIPS:
                    correct += 1
                else:
                    false_alarms += 1

        # Check for misses: large adverse moves with no preceding threat
        misses = self._count_guardian_misses(
            candles_m1, threats, is_buy, pip_size, entry_dt, close_dt)

        total = total_escalations + misses
        accuracy = 0.0
        if total > 0:
            accuracy = (correct / total) * 100

        return total_escalations, correct, false_alarms, misses, accuracy

    def _count_guardian_misses(
        self, candles_m1: List[Dict], threats: List[Dict], is_buy: bool,
        pip_size: float, entry_dt: datetime, close_dt: datetime,
    ) -> int:
        """Count adverse moves >MISS_THRESHOLD that weren't preceded by a threat."""
        trade_candles = self._candles_between(candles_m1, entry_dt, close_dt)
        threat_times = set()
        for t in threats:
            tt = self._parse_time(t.get("timestamp", ""))
            if tt:
                # Consider threat "covers" next 15 minutes
                for m in range(16):
                    threat_times.add((tt + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M"))

        misses = 0
        for i, c in enumerate(trade_candles):
            if is_buy:
                drop = (self._mid(c, "o") - self._mid(c, "l")) / pip_size
            else:
                drop = (self._mid(c, "h") - self._mid(c, "o")) / pip_size

            if drop >= MISS_THRESHOLD_PIPS:
                c_time = self._parse_time(c.get("time", ""))
                if c_time:
                    c_key = c_time.strftime("%Y-%m-%dT%H:%M")
                    if c_key not in threat_times:
                        misses += 1
        return misses

    # ── Story at Close ──

    def _story_at_close(
        self, pair: str, candles_m15: List[Dict], entry_dt: datetime,
        close_dt: datetime, scout_claims: Dict,
    ) -> Tuple[Dict, Dict]:
        """Recompute market story at close time; find what changed."""
        close_candles = self._candles_up_to(candles_m15, close_dt)
        close_story = {}
        what_changed = {}

        if len(close_candles) < 20:
            return close_story, what_changed

        try:
            candle_dicts = self._oanda_to_dicts(close_candles)
            from backtester.ema_separation import generate_market_picture
            from market_story import read_market_story

            mkt = generate_market_picture(candle_dicts)
            story = read_market_story(pair, candle_dicts, mkt)

            close_story = {
                "fan_state": mkt.get("fan_state"),
                "fan_direction": mkt.get("fan_direction"),
                "momentum_state": story["layers"].get("momentum", {}).get("state"),
                "entry_type": story.get("entry_type", "none"),
                "has_opportunity": story.get("has_opportunity", False),
                "opportunity_score": story.get("opportunity_score", 0),
            }

            # What changed between entry and close
            for field in scout_claims:
                if field in close_story and scout_claims[field] != close_story.get(field):
                    what_changed[field] = {
                        "entry": scout_claims[field],
                        "close": close_story[field],
                    }

        except Exception as e:
            # 2026-04-24: upgraded — silent = what_changed missing on audit.
            logger.warning("Close story recompute FAILED: %s: %s (what_changed missing)",
                           type(e).__name__, e)

        return close_story, what_changed

    # ── Validator Verification ──

    def _verify_validator(self, verdict: str, outcome: str) -> int:
        """Was the validator's decision correct?"""
        if not verdict:
            return 0
        v = verdict.upper()
        if v == "APPROVE" and outcome == "win":
            return 1
        if v in ("REJECT", "CAUTION") and outcome == "loss":
            return 1
        return 0

    # ── Prior Lookback ──

    def _prior_lookback(self, pair: str) -> Tuple[float, float, str]:
        """Get rolling accuracy and trend from prior 5 audits."""
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT scout_signal_accuracy, outcome
                FROM trade_audits
                WHERE pair = ? AND scout_signal_accuracy > 0
                ORDER BY audited_at DESC LIMIT 10
            """, (pair,)).fetchall()
        finally:
            conn.close()

        if len(rows) < 2:
            return 0.0, 0.0, "insufficient_data"

        recent_5 = rows[:5]
        older_5 = rows[5:10]

        recent_acc = sum(r["scout_signal_accuracy"] for r in recent_5) / len(recent_5)
        recent_wr = sum(1 for r in recent_5 if r["outcome"] == "win") / len(recent_5)

        if older_5:
            older_acc = sum(r["scout_signal_accuracy"] for r in older_5) / len(older_5)
            delta = recent_acc - older_acc
            if delta > 5:
                trend = "improving"
            elif delta < -5:
                trend = "degrading"
            else:
                trend = "stable"
        else:
            trend = "insufficient_data"

        return recent_acc, recent_wr, trend

    # ── Storage ──

    def _store_audit(self, r: Dict):
        conn = self._conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO trade_audits (
                    cycle_id, trade_id, pair, direction, setup_name, entry_type,
                    outcome, pnl_pips, pnl_usd,
                    scout_claims, reality_at_entry, signal_matches,
                    scout_signal_accuracy, scout_thesis_correct,
                    guardian_assessments, guardian_correct_calls,
                    guardian_false_alarms, guardian_misses, guardian_accuracy,
                    validator_verdict, validator_correct,
                    max_favorable_pips, max_adverse_pips,
                    entry_timing_score, exit_quality_score,
                    time_to_max_favorable_min, time_in_drawdown_min,
                    market_story_at_close, what_changed,
                    prior_5_accuracy, prior_5_wr, accuracy_trend,
                    audit_duration_ms
                ) VALUES (?,?,?,?,?,?, ?,?,?, ?,?,?, ?,?, ?,?,?,?,?, ?,?, ?,?, ?,?, ?,?, ?,?, ?,?,?, ?)
            """, (
                r["cycle_id"], r["trade_id"], r["pair"], r["direction"],
                r["setup_name"], r["entry_type"],
                r["outcome"], r["pnl_pips"], r["pnl_usd"],
                json.dumps(r["scout_claims"], default=str),
                json.dumps(r["reality_at_entry"], default=str),
                json.dumps(r["signal_matches"], default=str),
                r["scout_signal_accuracy"], r["scout_thesis_correct"],
                r["guardian_assessments"], r["guardian_correct_calls"],
                r["guardian_false_alarms"], r["guardian_misses"], r["guardian_accuracy"],
                r["validator_verdict"], r["validator_correct"],
                r["max_favorable_pips"], r["max_adverse_pips"],
                r["entry_timing_score"], r["exit_quality_score"],
                r["time_to_max_favorable_min"], r["time_in_drawdown_min"],
                json.dumps(r["market_story_at_close"], default=str),
                json.dumps(r["what_changed"], default=str),
                r["prior_5_accuracy"], r["prior_5_wr"], r["accuracy_trend"],
                r["audit_duration_ms"],
            ))
            conn.commit()
        finally:
            conn.close()

    # ── Phase 2: Rolling Audit ──

    def _maybe_rolling_audit(self):
        """Run rolling audit every ROLLING_TRIGGER_EVERY audits."""
        conn = self._conn()
        try:
            count = conn.execute("SELECT COUNT(*) FROM trade_audits").fetchone()[0]
        finally:
            conn.close()

        if count > 0 and count % ROLLING_TRIGGER_EVERY == 0:
            try:
                report = self.rolling_audit()
                if report.get("flags"):
                    logger.warning("DRIFT ALERT: %s", report["flags"])
            except Exception as e:
                logger.warning("Rolling audit failed: %s", e)

    def rolling_audit(self, last_n: int = 20) -> Dict[str, Any]:
        """Detect drift across last N trade audits."""
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT * FROM trade_audits
                ORDER BY audited_at DESC LIMIT ?
            """, (last_n,)).fetchall()
        finally:
            conn.close()

        if len(rows) < 3:
            return {"report_type": "rolling", "trades_analyzed": len(rows),
                    "flags": [], "recommendations": [], "error": "Not enough data"}

        audits = [dict(r) for r in rows]
        flags = []
        recommendations = []

        # Parse signal_matches for per-field accuracy
        field_results = {}
        for a in audits:
            try:
                matches = json.loads(a["signal_matches"]) if a["signal_matches"] else {}
            except (json.JSONDecodeError, TypeError):
                continue
            for field, matched in matches.items():
                if matched is not None:
                    field_results.setdefault(field, []).append(matched)

        # Per-field accuracy + drift detection
        signal_accuracy_by_field = {}
        for field, results in field_results.items():
            overall = sum(results) / len(results) * 100 if results else 0
            signal_accuracy_by_field[field] = overall

            # Check recent vs older
            recent = results[:5]
            older = results[5:15]
            if recent and older:
                recent_acc = sum(recent) / len(recent) * 100
                older_acc = sum(older) / len(older) * 100
                delta = recent_acc - older_acc
                if delta < -DRIFT_ALERT_THRESHOLD_PP:
                    flags.append(
                        f"{field} accuracy dropped {abs(delta):.0f}pp "
                        f"({older_acc:.0f}%→{recent_acc:.0f}%)")
                    recommendations.append({
                        "type": "threshold",
                        "target": field,
                        "reason": f"Accuracy dropped from {older_acc:.0f}% to {recent_acc:.0f}%",
                    })

        # Thesis accuracy
        thesis_results = {}
        for a in audits:
            et = a.get("entry_type", "")
            tc = a.get("scout_thesis_correct")
            if et and tc is not None:
                thesis_results.setdefault(et, []).append(tc)

        for thesis, results in thesis_results.items():
            if len(results) >= 3:
                acc = sum(results) / len(results)
                if acc < 0.6:
                    flags.append(f"{thesis} thesis accuracy {acc*100:.0f}% — below 60%")
                    recommendations.append({
                        "type": "pause",
                        "target": thesis,
                        "reason": f"Thesis accuracy {acc*100:.0f}% over {len(results)} trades",
                    })

        # Guardian accuracy
        g_total = sum(a.get("guardian_assessments", 0) or 0 for a in audits)
        g_false = sum(a.get("guardian_false_alarms", 0) or 0 for a in audits)
        g_misses = sum(a.get("guardian_misses", 0) or 0 for a in audits)
        if g_total > 0:
            false_rate = g_false / g_total
            if false_rate > 0.4:
                flags.append(f"Guardian false alarm rate {false_rate*100:.0f}%")
                recommendations.append({
                    "type": "weight",
                    "target": "guardian.layer2",
                    "reason": f"False alarm rate {false_rate*100:.0f}%",
                })

        # Entry timing
        timings = [a["entry_timing_score"] for a in audits
                    if a.get("entry_timing_score") is not None]
        avg_timing = sum(timings) / len(timings) if timings else 50
        if avg_timing < 40:
            flags.append(f"Entry timing poor ({avg_timing:.0f}/100)")

        # Overall metrics
        sig_accs = [a["scout_signal_accuracy"] for a in audits
                    if a.get("scout_signal_accuracy") is not None and a["scout_signal_accuracy"] > 0]
        thesis_accs = [a["scout_thesis_correct"] for a in audits
                       if a.get("scout_thesis_correct") is not None]
        g_accs = [a["guardian_accuracy"] for a in audits
                  if a.get("guardian_accuracy") is not None and a["guardian_accuracy"] > 0]
        exit_quals = [a["exit_quality_score"] for a in audits
                      if a.get("exit_quality_score") is not None]

        report = {
            "report_type": "rolling",
            "trades_analyzed": len(audits),
            "overall_signal_accuracy": sum(sig_accs) / len(sig_accs) if sig_accs else 0,
            "overall_thesis_accuracy": (sum(thesis_accs) / len(thesis_accs) * 100) if thesis_accs else 0,
            "overall_guardian_accuracy": sum(g_accs) / len(g_accs) if g_accs else 0,
            "entry_timing_avg": avg_timing,
            "exit_quality_avg": sum(exit_quals) / len(exit_quals) if exit_quals else 0,
            "signal_accuracy_by_field": signal_accuracy_by_field,
            "thesis_accuracy_by_type": {t: sum(r)/len(r)*100 for t, r in thesis_results.items()},
            "flags": flags,
            "recommendations": recommendations,
        }

        # Store report
        conn = self._conn()
        try:
            conn.execute("""
                INSERT INTO audit_reports (
                    report_type, trades_analyzed, overall_signal_accuracy,
                    overall_thesis_accuracy, overall_guardian_accuracy,
                    entry_timing_avg, exit_quality_avg, flags, recommendations,
                    report_data
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                "rolling", len(audits), report["overall_signal_accuracy"],
                report["overall_thesis_accuracy"], report["overall_guardian_accuracy"],
                avg_timing, report["exit_quality_avg"],
                json.dumps(flags), json.dumps(recommendations),
                json.dumps(report, default=str),
            ))
            conn.commit()
        finally:
            conn.close()

        # Create tuning proposals from recommendations + backtest them
        if recommendations:
            self._create_proposals_from_recommendations(recommendations)

        # ── Learning Integration: write drift findings to vault ──
        try:
            from learning_integrator import LearningIntegrator
            integrator = LearningIntegrator()
            drift_learnings = integrator.process_rolling_audit(report)
            report["learnings_written"] = drift_learnings
            logger.info(
                "Rolling audit → %d drift learnings written", len(drift_learnings))
        except Exception as e:
            logger.warning("Rolling audit learning integration failed: %s", e)

        return report

    def _create_proposals_from_recommendations(self, recommendations: List[Dict]):
        """Convert audit recommendations into tuning proposals, then backtest."""
        try:
            import tuning_config as tc
        except ImportError:
            logger.debug("tuning_config not available — skipping proposals")
            return

        param_map = {
            # Map recommendation targets to tuning parameters
            "momentum_exhausted": ("story.momentum.rsi_oversold", lambda cur: max(cur - 5, 15)),
            "fan_state": ("story.thesis_threshold", lambda cur: min(cur + 5, 70)),
            "e100_interaction": ("story.thesis_threshold", lambda cur: min(cur + 3, 70)),
            "guardian.layer2": ("guardian.layer2_structure_max", lambda cur: max(cur - 5, 20)),
            "guardian.layer3": ("guardian.layer3_momentum_max", lambda cur: max(cur - 3, 5)),
        }

        proposals_created = []
        for rec in recommendations:
            target = rec.get("target", "")
            rec_type = rec.get("type", "")

            if rec_type == "pause" and target:
                # Thesis pause → add to pairs.restricted_thesis
                try:
                    # Parse "counter_trend_reversal on GBP_USD" or just thesis name
                    parts = target.split(" on ") if " on " in target else [target]
                    thesis = parts[0].strip()
                    pair = parts[1].strip() if len(parts) > 1 else None
                    if pair:
                        current = tc.get("pairs.restricted_thesis", {})
                        proposed = dict(current)
                        proposed.setdefault(pair, [])
                        if thesis not in proposed[pair]:
                            proposed[pair].append(thesis)
                        pid = tc.propose_change(
                            "pairs.restricted_thesis", proposed,
                            reason=rec.get("reason", f"Audit: pause {thesis} on {pair}"),
                        )
                        proposals_created.append(pid)
                except Exception as e:
                    logger.debug("Failed to create pause proposal: %s", e)
                continue

            # Threshold/weight changes
            if target in param_map:
                param, adjuster = param_map[target]
                current = tc.get(param)
                if current is not None:
                    proposed = adjuster(current)
                    if proposed != current:
                        try:
                            pid = tc.propose_change(
                                param, proposed,
                                reason=rec.get("reason", f"Audit recommendation for {target}"),
                            )
                            proposals_created.append(pid)
                        except Exception as e:
                            logger.debug("Failed to create proposal for %s: %s", param, e)

        # Backtest all proposals
        for pid in proposals_created:
            try:
                result = tc.backtest_proposal(pid)
                imp = result.get("improvement", {})
                verdict = imp.get("verdict", "unknown")
                logger.info(
                    "Proposal #%d backtested: %s (WR: %.1f%% → %.1f%%, PF: %.2f → %.2f)",
                    pid, verdict,
                    imp.get("win_rate", {}).get("before", 0),
                    imp.get("win_rate", {}).get("after", 0),
                    imp.get("profit_factor", {}).get("before", 0),
                    imp.get("profit_factor", {}).get("after", 0),
                )
            except Exception as e:
                logger.warning("Backtest failed for proposal #%d: %s", pid, e)

        if proposals_created:
            logger.info("Created %d tuning proposals — awaiting approval", len(proposals_created))

    # ── Phase 3: Thesis Audit (weekly) ──

    def thesis_audit(self) -> Dict[str, Any]:
        """Deep dive per thesis type × pair. Run weekly."""
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT * FROM trade_audits ORDER BY audited_at DESC
            """).fetchall()
        finally:
            conn.close()

        if not rows:
            return {"report_type": "thesis", "error": "No audits yet"}

        audits = [dict(r) for r in rows]

        # Group by thesis × pair
        combos = {}
        for a in audits:
            key = (a.get("entry_type", "unknown"), a.get("pair", "unknown"))
            combos.setdefault(key, []).append(a)

        thesis_report = {}
        for (thesis, pair), group in combos.items():
            if len(group) < 2:
                continue

            wins = sum(1 for a in group if a["outcome"] == "win")
            sig_accs = [a["scout_signal_accuracy"] for a in group
                        if a.get("scout_signal_accuracy") and a["scout_signal_accuracy"] > 0]
            thesis_corr = [a["scout_thesis_correct"] for a in group
                           if a.get("scout_thesis_correct") is not None]
            timings = [a["entry_timing_score"] for a in group
                       if a.get("entry_timing_score") is not None]
            exits = [a["exit_quality_score"] for a in group
                     if a.get("exit_quality_score") is not None]
            mfes = [a["max_favorable_pips"] for a in group
                    if a.get("max_favorable_pips") is not None]
            maes = [a["max_adverse_pips"] for a in group
                    if a.get("max_adverse_pips") is not None]

            # Per-signal reliability
            signal_reliability = {}
            for a in group:
                try:
                    matches = json.loads(a["signal_matches"]) if a["signal_matches"] else {}
                except (json.JSONDecodeError, TypeError):
                    continue
                for field, matched in matches.items():
                    if matched is not None:
                        signal_reliability.setdefault(field, []).append(matched)
            signal_reliability = {
                f: sum(v)/len(v)*100 for f, v in signal_reliability.items() if v
            }

            thesis_report[f"{thesis}|{pair}"] = {
                "thesis": thesis,
                "pair": pair,
                "total_trades": len(group),
                "wins": wins,
                "win_rate": wins / len(group) * 100,
                "signal_accuracy": sum(sig_accs) / len(sig_accs) if sig_accs else 0,
                "thesis_accuracy": sum(thesis_corr) / len(thesis_corr) * 100 if thesis_corr else 0,
                "avg_entry_timing": sum(timings) / len(timings) if timings else 0,
                "avg_exit_quality": sum(exits) / len(exits) if exits else 0,
                "avg_mfe": sum(mfes) / len(mfes) if mfes else 0,
                "avg_mae": sum(maes) / len(maes) if maes else 0,
                "signal_reliability": signal_reliability,
            }

        # Global signal reliability
        global_signals = {}
        for a in audits:
            try:
                matches = json.loads(a["signal_matches"]) if a["signal_matches"] else {}
            except (json.JSONDecodeError, TypeError):
                continue
            for field, matched in matches.items():
                if matched is not None:
                    global_signals.setdefault(field, []).append(matched)
        signal_leaderboard = sorted(
            [(f, sum(v)/len(v)*100) for f, v in global_signals.items() if v],
            key=lambda x: x[1], reverse=True,
        )

        report = {
            "report_type": "thesis",
            "total_audits": len(audits),
            "thesis_breakdown": thesis_report,
            "signal_leaderboard": signal_leaderboard,
        }

        # Store
        conn = self._conn()
        try:
            conn.execute("""
                INSERT INTO audit_reports (
                    report_type, trades_analyzed, report_data
                ) VALUES (?, ?, ?)
            """, ("thesis", len(audits), json.dumps(report, default=str)))
            conn.commit()
        finally:
            conn.close()

        # ── Learning Integration: write structural findings to vault ──
        try:
            from learning_integrator import LearningIntegrator
            integrator = LearningIntegrator()
            # Reshape for integrator: needs thesis_accuracy_by_type and signal_accuracy_by_field
            thesis_for_integrator = {
                "report_data": json.dumps({
                    "thesis_accuracy_by_type": {
                        v["thesis"]: v["thesis_accuracy"]
                        for v in thesis_report.values()
                        if isinstance(v, dict) and v.get("total_trades", 0) >= 5
                    },
                    "signal_accuracy_by_field": dict(signal_leaderboard),
                }),
            }
            structural_learnings = integrator.process_thesis_audit(thesis_for_integrator)
            report["learnings_written"] = structural_learnings
            logger.info(
                "Thesis audit → %d structural learnings written",
                len(structural_learnings))
        except Exception as e:
            logger.warning("Thesis audit learning integration failed: %s", e)

        return report

    # ── Query Methods (for dashboard) ──

    def get_latest_audit(self, pair: str = None) -> Optional[Dict]:
        conn = self._conn()
        try:
            if pair:
                row = conn.execute(
                    "SELECT * FROM trade_audits WHERE pair = ? ORDER BY audited_at DESC LIMIT 1",
                    (pair,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM trade_audits ORDER BY audited_at DESC LIMIT 1"
                ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_latest_report(self, report_type: str = "rolling") -> Optional[Dict]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM audit_reports WHERE report_type = ? ORDER BY created_at DESC LIMIT 1",
                (report_type,)
            ).fetchone()
            if row:
                r = dict(row)
                r["report_data"] = json.loads(r["report_data"]) if r["report_data"] else {}
                return r
            return None
        finally:
            conn.close()

    def get_signal_accuracy_summary(self) -> Dict[str, float]:
        """Get current per-signal accuracy across all audits."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT signal_matches FROM trade_audits WHERE signal_matches IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()

        totals = {}
        for r in rows:
            try:
                matches = json.loads(r["signal_matches"])
                for field, val in matches.items():
                    if val is not None:
                        totals.setdefault(field, []).append(val)
            except (json.JSONDecodeError, TypeError):
                continue

        return {f: sum(v)/len(v)*100 for f, v in totals.items() if v}

    # ── Helpers ──

    def _get_flight_data(self, cycle_id: str) -> Dict:
        """Get flight recorder data for a cycle, keyed by stage."""
        try:
            from flight_recorder import get_flight_recorder
            fr = get_flight_recorder()
            if not fr:
                return {}
            with fr._conn() as conn:
                rows = conn.execute("""
                    SELECT stage, data, note FROM flight_log
                    WHERE cycle_id = ? ORDER BY timestamp
                """, (cycle_id,)).fetchall()

            result = {}
            for r in rows:
                stage = r["stage"]
                try:
                    data = json.loads(r["data"]) if r["data"] else {}
                except (json.JSONDecodeError, TypeError):
                    data = {}
                result[stage] = data
            return result
        except Exception:
            return {}

    def _get_guardian_threats(self, trade_id: str) -> List[Dict]:
        """Get guardian threat records for a trade from flight log."""
        try:
            from flight_recorder import get_flight_recorder
            fr = get_flight_recorder()
            if not fr:
                return []
            with fr._conn() as conn:
                rows = conn.execute("""
                    SELECT data, timestamp FROM flight_log
                    WHERE stage = 'guardian_threat' AND trade_id = ?
                    ORDER BY timestamp
                """, (trade_id,)).fetchall()
            results = []
            for r in rows:
                try:
                    d = json.loads(r["data"]) if r["data"] else {}
                    d["timestamp"] = r["timestamp"]
                    results.append(d)
                except (json.JSONDecodeError, TypeError):
                    pass
            return results
        except Exception:
            return []

    def _get_guardian_actions(self, trade_id: str) -> List[Dict]:
        """Get guardian SL/TP modification actions for a trade from flight log.

        Returns a timeline of every order modification the guardian made:
        SL moves, TP ratchets, partial closes, fan failure tightens, etc.
        """
        try:
            from flight_recorder import get_flight_recorder
            fr = get_flight_recorder()
            if not fr:
                return []
            with fr._conn() as conn:
                rows = conn.execute("""
                    SELECT data, timestamp, note FROM flight_log
                    WHERE stage = 'guardian_action' AND trade_id = ?
                    ORDER BY timestamp
                """, (trade_id,)).fetchall()
            results = []
            for r in rows:
                try:
                    d = json.loads(r["data"]) if r["data"] else {}
                    d["timestamp"] = r["timestamp"]
                    d["note"] = r["note"] or ""
                    results.append(d)
                except (json.JSONDecodeError, TypeError):
                    pass
            return results
        except Exception:
            return []

    def _get_guardian_phases(self, trade_id: str) -> List[Dict]:
        """Get trade phase transitions from flight log.

        Returns the phase timeline: trending→peak→retracing→continuing etc.
        """
        try:
            from flight_recorder import get_flight_recorder
            fr = get_flight_recorder()
            if not fr:
                return []
            with fr._conn() as conn:
                rows = conn.execute("""
                    SELECT data, timestamp, note FROM flight_log
                    WHERE stage = 'trade_phase' AND trade_id = ?
                    ORDER BY timestamp
                """, (trade_id,)).fetchall()
            results = []
            for r in rows:
                try:
                    d = json.loads(r["data"]) if r["data"] else {}
                    d["timestamp"] = r["timestamp"]
                    results.append(d)
                except (json.JSONDecodeError, TypeError):
                    pass
            return results
        except Exception:
            return []

    def _fetch_candles(
        self, pair: str, granularity: str, from_dt: datetime, to_dt: datetime,
    ) -> List[Dict]:
        """Fetch OANDA candles for verification."""
        try:
            from oanda_client import OandaClient
            client = OandaClient()
            return client.get_candles(
                pair, granularity=granularity,
                from_time=from_dt, to_time=to_dt, price="M",
            )
        except Exception as e:
            logger.warning("Failed to fetch candles for audit: %s", e)
            return []

    def _oanda_to_dicts(self, candles: List[Dict]) -> List[Dict]:
        """Convert OANDA candle format to simple OHLCV dicts."""
        result = []
        for c in candles:
            mid = c.get("mid", {})
            if not mid:
                continue
            result.append({
                "time": c.get("time", ""),
                "open": float(mid.get("o", 0)),
                "high": float(mid.get("h", 0)),
                "low": float(mid.get("l", 0)),
                "close": float(mid.get("c", 0)),
                "volume": int(c.get("volume", 0)),
            })
        return result

    def _mid(self, candle: Dict, field: str) -> float:
        """Get mid price field from OANDA candle."""
        mid = candle.get("mid", {})
        if mid:
            return float(mid.get(field, 0))
        # Fallback for already-converted dicts
        name_map = {"o": "open", "h": "high", "l": "low", "c": "close"}
        return float(candle.get(name_map.get(field, field), 0))

    def _parse_time(self, time_str: str) -> Optional[datetime]:
        if not time_str:
            return None
        try:
            # Handle OANDA RFC3339 format
            t = time_str.replace("Z", "+00:00")
            if "." in t:
                # Truncate microseconds to 6 digits
                parts = t.split(".")
                frac = parts[1]
                tz_part = ""
                for sep in ("+", "-"):
                    if sep in frac[1:]:  # skip first char for negative
                        idx = frac.index(sep, 1)
                        tz_part = frac[idx:]
                        frac = frac[:idx]
                        break
                frac = frac[:6]
                t = parts[0] + "." + frac + tz_part
            return datetime.fromisoformat(t)
        except Exception:
            return None

    def _candles_up_to(self, candles: List[Dict], dt: datetime) -> List[Dict]:
        result = []
        for c in candles:
            ct = self._parse_time(c.get("time", ""))
            if ct and ct <= dt:
                result.append(c)
        return result

    def _candles_after(self, candles: List[Dict], dt: datetime, count: int = 10) -> List[Dict]:
        result = []
        for c in candles:
            ct = self._parse_time(c.get("time", ""))
            if ct and ct > dt:
                result.append(c)
                if len(result) >= count:
                    break
        return result

    def _candles_between(
        self, candles: List[Dict], from_dt: datetime, to_dt: datetime,
    ) -> List[Dict]:
        result = []
        for c in candles:
            ct = self._parse_time(c.get("time", ""))
            if ct and from_dt <= ct <= to_dt:
                result.append(c)
        return result


# ── Async wrapper for guardian integration ──

async def audit_trade_async(
    cycle_id: str, trade_id: str, pair: str, direction: str,
    entry_price: float, exit_price: float, stop_loss: float, take_profit: float,
    pnl_pips: float, pnl_usd: float, setup_name: str = "",
    entry_type: str = "", entry_time: str = "", close_time: str = "",
    outcome: str = "", validator_verdict: str = "", user_id: int = None,
):
    """Non-blocking audit wrapper. Call from guardian's async reconcile loop."""
    loop = asyncio.get_event_loop()
    try:
        auditor = TradeAuditor()
        result = await loop.run_in_executor(None, lambda: auditor.audit_trade(
            cycle_id=cycle_id, trade_id=trade_id, pair=pair, direction=direction,
            entry_price=entry_price, exit_price=exit_price,
            stop_loss=stop_loss, take_profit=take_profit,
            pnl_pips=pnl_pips, pnl_usd=pnl_usd,
            setup_name=setup_name, entry_type=entry_type,
            entry_time=entry_time, close_time=close_time,
            outcome=outcome, validator_verdict=validator_verdict,
            user_id=user_id,
        ))
        return result
    except Exception as e:
        logger.warning("Async audit failed for %s %s: %s", pair, trade_id, e)
        return None
