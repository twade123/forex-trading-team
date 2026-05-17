"""
Decision Logger — orchestrates the full decision pipeline and records everything.

Called by the orchestrator before every trade. Runs the story-aware validation
pipeline, logs the complete decision to trade_decisions table, and returns
the final verdict.

The validation pipeline:
  1. Hard risk limits (instant pass/fail)
  2. Thesis validation (does the market story make sense?)
  3. Historical evidence (does the DB support this thesis?)
  4. Profile evidence (does the condition fingerprint match winners?)
  5. Final verdict (blend story confidence with historical evidence)

KEY PRINCIPLE: No historical data does NOT mean REJECT. If the market story
is strong (score 70+, thesis confirmed by structure/momentum), the trade
is valid even without historical precedent.

Usage::

    from Source.decision_logger import DecisionLogger

    dl = DecisionLogger()
    result = dl.evaluate_and_log(
        pair="EUR_USD", timeframe="M15", setup="S15_rr2.0_sl2.5",
        direction="buy", regime="ranging",
        market_story={...},  # from read_market_story()
        indicators={...}, h4_agrees=True, session="London",
        market_data={...}, news_data={...},
    )
    # result["verdict"] → "APPROVE" / "REJECT" / "CAUTION"
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trading_bot.decision_logger")

_trading_db = None
_trade_validator = None


def _get_trading_db():
    global _trading_db
    if _trading_db is None:
        try:
            from Source.backtester.trading_db import TradingDB
            _trading_db = TradingDB()
            logger.info("TradingDB connected for decision logging")
        except Exception as e:
            logger.error("Failed to load TradingDB: %s", e)
    return _trading_db


def _get_trade_validator():
    global _trade_validator
    if _trade_validator is None:
        try:
            from Source.trade_validator import TradeValidator
            _trade_validator = TradeValidator()
            logger.info("TradeValidator loaded")
        except Exception as e:
            logger.warning("TradeValidator not available: %s", e)
    return _trade_validator


class DecisionLogger:
    """Orchestrates the story-aware decision pipeline."""

    def __init__(self):
        self._db = None
        self._validator = None

    @property
    def db(self):
        if self._db is None:
            self._db = _get_trading_db()
        return self._db

    @property
    def validator(self):
        if self._validator is None:
            self._validator = _get_trade_validator()
        return self._validator

    def evaluate_and_log(
        self,
        pair: str,
        timeframe: str,
        setup: str,
        direction: str,
        regime: str,
        market_story: Dict[str, Any] = None,
        indicators: Dict[str, Any] = None,
        h4_agrees: bool = None,
        session: str = None,
        market_data: Dict[str, Any] = None,
        news_data: Dict[str, Any] = None,
        weather_data: Dict[str, Any] = None,
        wolfram_data: Dict[str, Any] = None,
        candles: list = None,
        confluence_output: Dict = None,
        profile_match: Dict = None,
    ) -> Dict[str, Any]:
        """Run the story-aware validation pipeline and log the decision.

        Returns:
            {
                "decision_id": str,
                "verdict": "APPROVE" | "REJECT" | "CAUTION",
                "confidence": float,
                "recommended_action": "EXECUTE" | "SKIP" | "REDUCE",
                "recommended_params": {...},
                "warnings": [...],
                "loss_patterns": [...],
                "pipeline_steps": {...},
                "execution_time_ms": int,
            }
        """
        t0 = time.time()
        pipeline = {}
        warnings = []
        story = market_story or {}

        # ============================================================
        # STEP 1: Hard Risk Limits (instant)
        # ============================================================
        risk_result = self._check_risk_limits(market_data, news_data)
        pipeline["step1_risk_limits"] = risk_result
        if not risk_result["passed"]:
            warnings.extend(risk_result["issues"])
            return self._build_result(
                "REJECT", 0.05, "SKIP", warnings, pipeline, [],
                risk_result["issues"][0] if risk_result["issues"] else "Risk limit hit",
                pair, timeframe, setup, direction, regime, t0,
            )

        # ============================================================
        # STEP 2: Thesis Validation — Does the story make sense?
        # ============================================================
        thesis_result = self._validate_thesis(story, direction)
        pipeline["step2_thesis_validation"] = thesis_result
        warnings.extend(thesis_result.get("warnings", []))

        # ============================================================
        # STEP 3: Historical Evidence (DB)
        # ============================================================
        db = self.db
        db_result = None
        loss_patterns = []
        best_params = None

        if db:
            try:
                db_result = db.validate_trade_setup(
                    pair=pair, regime=regime, setup=setup,
                    direction=direction, indicators=indicators,
                    h4_agrees=h4_agrees, session=session,
                )
                pipeline["step3_historical"] = db_result
                warnings.extend(db_result.get("warnings", []))
                loss_patterns = db_result.get("loss_patterns", [])
                best_params = db_result.get("best_params")
            except Exception as e:
                pipeline["step3_historical"] = {"error": str(e)}
                logger.warning("DB validation failed: %s", e)
        else:
            pipeline["step3_historical"] = {"skipped": "no TradingDB"}

        # ============================================================
        # STEP 4: Profile Engine Evidence
        # ============================================================
        profile = profile_match or {}
        profile_confidence = profile.get("profile_confidence", profile.get("confidence", 0))
        pipeline["step4_profile"] = {
            "confidence": profile_confidence,
            "match_quality": profile.get("profile_match_quality", profile.get("match_quality", "none")),
            "historical_wr": profile.get("profile_historical_wr", profile.get("historical_win_rate", 0)),
            "trades": profile.get("profile_trades", profile.get("historical_trades", 0)),
        }

        # ============================================================
        # STEP 5: Final Verdict — Blend story + history + profile
        # ============================================================
        verdict, confidence, action, action_reason = self._compute_verdict(
            story, thesis_result, db_result, profile_confidence, news_data, weather_data,
        )
        pipeline["step5_final"] = {
            "verdict": verdict,
            "confidence": confidence,
            "action": action,
            "reason": action_reason,
        }

        # Data integrity check (runs but doesn't block — just logs issues)
        tv = self.validator
        if tv and candles:
            try:
                integrity = tv.validate_data_integrity(candles=candles, indicators_result=indicators)
                pipeline["data_integrity"] = {
                    "passed": integrity.passed,
                    "issues": integrity.issues,
                }
                if not integrity.passed:
                    warnings.append(f"Data integrity: {'; '.join(integrity.issues[:2])}")
            except Exception as e:
                pipeline["data_integrity"] = {"error": str(e)}

        return self._build_result(
            verdict, confidence, action, warnings, pipeline, loss_patterns,
            action_reason, pair, timeframe, setup, direction, regime, t0,
            best_params=best_params, confluence=None,
        )

    # ──────────────────────────────────────────────────────────────
    # STEP 1: Risk limits
    # ──────────────────────────────────────────────────────────────

    def _check_risk_limits(self, market_data: Dict = None, news_data: Dict = None) -> Dict:
        """Binary pass/fail risk checks."""
        issues = []
        md = market_data or {}

        daily_loss = md.get("current_daily_loss_pct", 0)
        max_loss = md.get("max_daily_loss_pct", 3.0)
        if daily_loss >= max_loss:
            issues.append(f"Daily loss {daily_loss:.1f}% >= max {max_loss:.1f}%")

        open_trades = md.get("current_open_trades", 0)
        max_trades = md.get("max_concurrent_trades", 3)
        if open_trades >= max_trades:
            issues.append(f"Open trades {open_trades} >= max {max_trades}")

        # High-impact news within 30 min
        if news_data:
            if news_data.get("high_impact_within_30min") or news_data.get("events", {}).get("high_impact_within_30min"):
                issues.append("High-impact news event within 30 minutes")

        return {"passed": len(issues) == 0, "issues": issues}

    # ──────────────────────────────────────────────────────────────
    # STEP 2: Thesis validation
    # ──────────────────────────────────────────────────────────────

    def _validate_thesis(self, story: Dict, direction: str) -> Dict:
        """Check if the market story's thesis actually holds up."""
        if not story or not story.get("has_opportunity"):
            return {
                "valid": False,
                "score": 0,
                "warnings": ["No market story thesis provided"],
                "checks": [],
            }

        entry_type = story.get("entry_type", "none")
        layers = story.get("layers", {})
        trend = layers.get("trend", {})
        structure = layers.get("structure", {})
        momentum = layers.get("momentum", {})

        fan_state = trend.get("fan_state", "unknown")
        fan_dir = trend.get("fan_direction", "mixed")
        mom_state = momentum.get("state", "neutral")
        mom_exhausted = momentum.get("exhausted", False)
        e100_int = structure.get("e100_interaction", {}).get("interaction", "distant")
        wick_pressure = structure.get("wick_pressure", {}).get("dominant_pressure", "balanced")
        body_trend = structure.get("body_trend", {}).get("body_trend", "steady")
        consec = structure.get("consecutive", {}).get("run_state", "neutral")

        checks = []
        warnings = []
        valid = True

        if entry_type == "counter_trend_reversal":
            # Fan must be peaked/decelerating/contracting
            if fan_state in ("peaked", "decelerating", "contracting"):
                checks.append(f"✅ Fan {fan_state} — trend exhausting, supports counter-trend")
            elif fan_state in ("expanding", "accelerating"):
                checks.append(f"❌ Fan {fan_state} — trend still strengthening, counter-trend is high risk")
                warnings.append(f"Counter-trend into {fan_state} fan — high risk")
                valid = False
            else:
                checks.append(f"⚠️ Fan {fan_state} — ambiguous for counter-trend")

            # Momentum should be exhausted
            if mom_exhausted:
                checks.append(f"✅ Momentum exhausted ({mom_state}) — confirms reversal thesis")
            elif mom_state in ("overbought", "oversold"):
                checks.append(f"⚠️ Momentum {mom_state} but not exhausted — partial confirmation")
            else:
                checks.append(f"⚠️ Momentum {mom_state} — no exhaustion signal")
                warnings.append("No momentum exhaustion — reversal thesis weakened")

            # Wick pressure should support direction
            expected_pressure = "buying" if direction == "buy" else "selling"
            if wick_pressure == expected_pressure:
                checks.append(f"✅ Wicks show {wick_pressure} pressure — structural confirmation")
            elif wick_pressure == "balanced":
                checks.append("⚠️ Balanced wick pressure — no clear structural confirmation")

        elif entry_type == "trend_continuation":
            # Fan must be expanding/stable in trade direction
            expected_dir = "bullish" if direction == "buy" else "bearish"
            if fan_dir == expected_dir and fan_state in ("expanding", "accelerating", "stable"):
                checks.append(f"✅ Fan {fan_dir} {fan_state} — supports continuation")
            elif fan_state in ("peaked", "contracting"):
                checks.append(f"❌ Fan {fan_state} — trend fading, late for continuation")
                warnings.append(f"Fan {fan_state} — trend losing steam for continuation")
                valid = False
            else:
                checks.append(f"⚠️ Fan {fan_dir} {fan_state} — mixed for continuation")

        elif entry_type == "e100_bounce":
            # E100 must show support/resistance, not broken
            if e100_int in ("strong_support", "strong_resistance"):
                checks.append(f"✅ E100 {e100_int} — strong structural level")
            elif e100_int in ("support", "resistance"):
                checks.append(f"✅ E100 {e100_int} — level holding")
            elif e100_int == "broken":
                checks.append("❌ E100 broken — bounce thesis invalid")
                warnings.append("E100 is broken — structural level lost")
                valid = False
            elif e100_int == "testing":
                checks.append("⚠️ E100 testing — no confirmation yet")
                warnings.append("E100 test in progress, no rejection confirmed")

        elif entry_type == "breakout":
            range_trend = structure.get("consecutive", {}).get("range_trend", "steady")
            if range_trend == "compressing" or fan_state == "just_crossed":
                checks.append(f"✅ Ranges compressing / fresh cross — breakout setup")
            else:
                checks.append("⚠️ No compression detected — breakout thesis weak")
                warnings.append("No range compression or EMA cross for breakout")

            if body_trend == "growing":
                checks.append("✅ Bodies growing — breakout conviction")

        # Story warnings pass through
        warnings.extend(story.get("warnings", []))

        return {
            "valid": valid,
            "entry_type": entry_type,
            "score": story.get("opportunity_score", 0),
            "story_confidence": story.get("confidence", 0),
            "warnings": warnings,
            "checks": checks,
        }

    # ──────────────────────────────────────────────────────────────
    # STEP 5: Compute final verdict
    # ──────────────────────────────────────────────────────────────

    def _compute_verdict(
        self, story, thesis_result, db_result, profile_confidence,
        news_data, weather_data,
    ):
        """Blend story confidence with historical evidence for final verdict."""
        story_score = story.get("opportunity_score", 0) if story else 0
        story_conf = story.get("confidence", 0) if story else 0
        thesis_valid = thesis_result.get("valid", False)

        # Start from story confidence
        confidence = story_conf

        # ── Thesis validation impact ──
        if not thesis_valid:
            # Thesis doesn't hold up — major penalty
            confidence *= 0.4
            if confidence < 0.3:
                return "REJECT", confidence, "SKIP", "Thesis validation failed"

        # ── Historical evidence impact ──
        if db_result:
            db_verdict = db_result.get("verdict", "REJECT")
            db_wr = db_result.get("historical_stats", {}).get("win_rate", 0)
            db_trades = db_result.get("historical_stats", {}).get("trade_count", 0)

            if db_verdict == "APPROVE" and db_wr >= 85 and db_trades >= 100:
                confidence = min(0.95, confidence + 0.15)  # Strong historical boost
            elif db_verdict == "APPROVE" and db_wr >= 75:
                confidence = min(0.95, confidence + 0.08)
            elif db_verdict == "REJECT" and db_trades >= 50:
                # History says no — reduce but don't override strong story
                if story_score >= 70 and thesis_valid:
                    confidence *= 0.85  # Slight penalty, story can override
                else:
                    confidence *= 0.5  # Major penalty
            # No data or insufficient data: no adjustment (story stands on its own)
        else:
            # No DB available — story confidence stands alone
            # This is fine if the story is strong
            if story_score >= 70 and thesis_valid:
                pass  # Story is enough
            elif story_score >= 50 and thesis_valid:
                confidence *= 0.9  # Slight uncertainty without historical confirmation

        # ── Profile engine impact ──
        if profile_confidence > 0.80:
            confidence = min(0.95, confidence + 0.10)
        elif profile_confidence > 0.65:
            confidence = min(0.95, confidence + 0.05)
        elif profile_confidence < 0.40 and profile_confidence > 0:
            confidence *= 0.95  # Minor flag, not a dealbreaker

        # ── News/weather overrides ──
        if news_data:
            upcoming = news_data.get("upcoming_high_impact", [])
            if upcoming:
                confidence = min(confidence, 0.6)  # Cap confidence

        if weather_data and weather_data.get("severity", 0) >= 4:
            confidence = min(confidence, 0.6)

        # ── Map to verdict ──
        if confidence >= 0.65 and thesis_valid:
            verdict = "APPROVE"
            action = "EXECUTE"
            reason = f"Story score {story_score}/100, thesis confirmed, confidence {confidence:.0%}"
        elif confidence >= 0.45 and thesis_valid:
            verdict = "CAUTION"
            action = "REDUCE"
            reason = f"Marginal — score {story_score}/100, confidence {confidence:.0%}"
        else:
            verdict = "REJECT"
            action = "SKIP"
            if not thesis_valid:
                reason = f"Thesis invalid: {'; '.join(thesis_result.get('warnings', [])[:2])}"
            else:
                reason = f"Insufficient confidence ({confidence:.0%})"

        return verdict, confidence, action, reason

    # ──────────────────────────────────────────────────────────────
    # Result builder + DB logging
    # ──────────────────────────────────────────────────────────────

    def _build_result(
        self, verdict, confidence, action, warnings, pipeline, loss_patterns,
        reason, pair, timeframe, setup, direction, regime, t0,
        best_params=None, confluence=None,
    ):
        elapsed_ms = int((time.time() - t0) * 1000)

        # Log to DB
        decision_id = "no_db"
        db = self.db
        if db:
            try:
                db_evidence = pipeline.get("step3_historical", {}).get("historical_stats")
                decision_id = db.log_decision(
                    pair=pair, timeframe=timeframe, setup=setup,
                    direction=direction, regime=regime,
                    verdict=verdict, confidence=confidence,
                    reasoning=reason or "; ".join(warnings[:5]),
                    db_evidence=db_evidence,
                    loss_patterns=loss_patterns, confluence=confluence,
                    recommended_rr=best_params.get("rr_mult") if best_params else None,
                    recommended_sl=best_params.get("sl_mult") if best_params else None,
                    final_action=action, action_reason=reason,
                    execution_time_ms=elapsed_ms,
                )
            except Exception as e:
                logger.error("Failed to log decision: %s", e)

        return {
            "decision_id": decision_id,
            "verdict": verdict,
            "confidence": confidence,
            "recommended_action": action,
            "recommended_params": best_params,
            "warnings": warnings,
            "loss_patterns": loss_patterns,
            "confluence": confluence,
            "pipeline_steps": pipeline,
            "execution_time_ms": elapsed_ms,
        }

    def update_outcome(self, decision_id: str, trade_id: str = None,
                        outcome: str = None, pips: float = None):
        """Update the decision record after trade closes."""
        db = self.db
        if db:
            db.update_trade_outcome(
                trade_id=trade_id, decision_id=decision_id,
                outcome=outcome, pips=pips,
            )
