"""
LLM-powered validation analysis for the trading bot.

Provides three analysis modes:

- **On-demand** (:meth:`ValidationAnalyst.analyze_on_demand`): Called when
  the heuristic gates flag a gray-zone decision (``needs_llm_escalation``).
  Returns a conservative proceed/hold/reduce_size recommendation.
- **Hourly** (:meth:`ValidationAnalyst.analyze_hourly`): Batch analysis of
  accumulated validation failures to catch systematic issues (regime
  changes, data feed problems).
- **Daily** (:meth:`ValidationAnalyst.analyze_daily`): Parameter
  effectiveness review with specific tuning recommendations.

The knowledge base at
``{snapshot_dir}/{instrument}/knowledge_base/positive_patterns.json``
provides winning-trade examples for LLM context once Phase 11
(backtesting) populates it.

Uses the Anthropic SDK (Claude) with lazy import -- the client is only
created on the first actual LLM call.  All LLM calls wrapped in
try/except for graceful degradation when the API is unavailable.

Usage::

    from Source.validation_analyst import ValidationAnalyst
    from Source.trade_validator import ValidationResult

    analyst = ValidationAnalyst()
    analyst.record(validation_result)
    advice = analyst.analyze_on_demand(trade_decision, ...)
"""

import json
import logging
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from db_pool import get_intelligence

logger = logging.getLogger("trading_bot.validation_analyst")

try:
    from flight_recorder import FlightRecorder, FlightStage
    _flight: Any = FlightRecorder()
except Exception:
    _flight = None  # flight recorder not available — silent degradation

# Maximum number of ValidationResults held in memory.
_MAX_HISTORY = 1000


class ValidationAnalyst:
    """LLM-powered validation analysis -- hourly batch and on-demand escalation.

    Uses Anthropic SDK (Claude) for deep analysis of:

    - Accumulated validation failures (hourly).
    - Gray-zone trade decisions needing confirmation (on-demand).
    - Daily parameter effectiveness review (daily).

    Args:
        model: Anthropic model ID for LLM calls.
        snapshot_base_dir: Root directory where trade snapshots (and the
            knowledge base) are stored.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        snapshot_base_dir: str = "Forex Trading Team/Data",
    ) -> None:
        self.model = model
        self.snapshot_dir = Path(snapshot_base_dir)
        self._history: List[Any] = []  # recent ValidationResults
        self._client: Any = None  # lazy Anthropic client

    # ------------------------------------------------------------------
    # Lazy Anthropic client
    # ------------------------------------------------------------------

    @property
    def client(self) -> Any:
        """Lazy-init Anthropic client.

        Only imports and creates the client on first access, so the
        module can be imported without the ``anthropic`` package
        installed (it will fail only when an LLM call is actually made).
        """
        if self._client is None:
            from anthropic import Anthropic
            import os
            from pathlib import Path
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                key_path = Path(__file__).parent.parent / "API" / "CLAUDE_API_KEY.txt"
                if not key_path.exists():
                    key_path = Path(__file__).parent.parent.parent / "API" / "CLAUDE_API_KEY.txt"
                if key_path.exists():
                    api_key = key_path.read_text().strip()
            self._client = Anthropic(api_key=api_key)
        return self._client

    # ------------------------------------------------------------------
    # Record results
    # ------------------------------------------------------------------

    def record(self, result: Any) -> None:
        """Append a validation result to history for batch analysis.

        Maintains a rolling window of at most :data:`_MAX_HISTORY`
        entries (oldest are dropped).

        Args:
            result: A :class:`ValidationResult` (or any object with
                ``gate``, ``passed``, ``issues``, ``data_type``,
                ``confidence``, and ``needs_llm_escalation`` attrs).
        """
        self._history.append(result)
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]

    # ------------------------------------------------------------------
    # On-demand analysis (gray-zone decisions)
    # ------------------------------------------------------------------

    def analyze_on_demand(
        self,
        trade_decision: Dict[str, Any],
        indicators_result: Optional[Dict[str, Any]] = None,
        advanced_result: Optional[Dict[str, Any]] = None,
        confluence_output: Optional[Dict[str, Any]] = None,
        contradictions: Optional[Dict[str, Any]] = None,
        snapshot_path: Optional[str] = None,
        instrument: str = "EUR_USD",
        backtest_evidence: Optional[Dict[str, Any]] = None,
        analysis_results: Optional[Dict[str, Any]] = None,
        intelligence_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """On-demand LLM analysis for gray-zone decisions.

        Called when Gate 2 sets ``needs_llm_escalation=True``.

        Args:
            trade_decision: Output from ``StrategyEngine.evaluate()``.
            indicators_result: Core indicators snapshot.
            advanced_result: Advanced indicators snapshot.
            confluence_output: Confluence scorer output.
            contradictions: Output from
                ``TradeValidator.detect_contradictions()``.
            snapshot_path: Optional path to the chart PNG for human
                reference (logged but not sent to API text endpoint).
            instrument: Instrument name for knowledge base lookup.

        Returns:
            Dict with ``recommendation`` (``'proceed'`` | ``'hold'`` |
            ``'reduce_size'``), ``reasoning`` (str), and
            ``confidence`` (float 0-1).
        """
        _t0 = time.time()
        _pair = instrument
        if _flight:
            _flight.record(FlightStage.VALIDATOR_CALL, pair=_pair, data={
                "action": trade_decision.get("action"),
                "score": trade_decision.get("adjusted_score"),
            }, note="validator LLM call start")

        # Build prompt context
        parts: List[str] = []

        # Trade decision summary
        action = trade_decision.get("action", "unknown")
        score = trade_decision.get("adjusted_score", 0)
        regime = (trade_decision.get("confluence", {}) or {}).get("regime", "unknown")
        direction = (trade_decision.get("confluence", {}) or {}).get("direction", "unknown")
        parts.append(
            f"Trade signal: {action}, adjusted score: {score}, "
            f"direction: {direction}, regime: {regime}"
        )

        # Key indicator values
        if indicators_result and isinstance(indicators_result, dict):
            rsi = (indicators_result.get("rsi") or {}).get("value")
            macd = (indicators_result.get("macd") or {})
            ema_trend = (indicators_result.get("ema200_trend") or {}).get("direction")
            bb = (indicators_result.get("bollinger") or {})
            parts.append(
                f"Indicators: RSI={rsi}, MACD crossover={macd.get('crossover')}, "
                f"EMA200 trend={ema_trend}, Bollinger position={bb.get('position')}"
            )

        if advanced_result and isinstance(advanced_result, dict):
            adx = (advanced_result.get("adx") or {}).get("adx")
            parts.append(f"ADX={adx}")

        # Confluence breakdown
        if confluence_output and isinstance(confluence_output, dict):
            breakdown = confluence_output.get("breakdown", {})
            parts.append(f"Score breakdown: {json.dumps(breakdown, default=str)}")

        # Contradictions
        if contradictions and isinstance(contradictions, dict):
            c_list = contradictions.get("contradictions", [])
            parts.append(f"Contradictions ({len(c_list)}):")
            for c in c_list:
                parts.append(
                    f"  [{c.get('severity', 'warning').upper()}] "
                    f"Rule {c.get('rule', '?')}: {c.get('description', '')}"
                )

        # Snapshot reference
        if snapshot_path:
            parts.append(f"Chart snapshot saved at: {snapshot_path}")

        # Backtest database evidence (39K+ setup performance rows)
        if backtest_evidence and isinstance(backtest_evidence, dict):
            parts.append("Backtest database evidence:")
            verdict = backtest_evidence.get("verdict", "unknown")
            conf = backtest_evidence.get("confidence", 0)
            parts.append(f"  Validator verdict: {verdict} (confidence: {conf})")
            
            db_ev = backtest_evidence.get("db_evidence", {})
            if isinstance(db_ev, dict):
                parts.append(
                    f"  Best setup: {db_ev.get('best_setup', 'none')} | "
                    f"Win rate: {db_ev.get('best_win_rate') or 0:.1f}% | "
                    f"PF: {db_ev.get('best_profit_factor') or 0:.2f} | "
                    f"Trades: {db_ev.get('best_trade_count') or 0} | "
                    f"Total pips: {db_ev.get('total_pips') or 0:.1f}"
                )
            
            loss_patterns = backtest_evidence.get("loss_patterns", [])
            if loss_patterns:
                parts.append("  Loss patterns from backtest:")
                for lp in loss_patterns[:4]:
                    if isinstance(lp, dict):
                        parts.append(f"    - {lp.get('description', lp.get('pattern', ''))}")
                        parts.append(f"      Filter: {lp.get('filter_suggestion', '')}")
            
            setups = backtest_evidence.get("setups_evaluated", [])
            if setups:
                parts.append(f"  Setups evaluated: {', '.join(str(s) for s in setups[:5])}")

        # Knowledge base context (legacy file-based)
        kb_summary = self._load_knowledge_base(instrument)
        if kb_summary:
            parts.append(f"Known winning patterns:\n{kb_summary}")

        # ── Two-layer intelligence context ────────────────────────────────
        # Layer 1: Intelligence package (facts),
        # Layer 2: Decision rules (pre-computed adjustments/flags)
        # Use caller-supplied intel if provided; otherwise fetch from DB.
        if intelligence_data and isinstance(intelligence_data, dict):
            intel = intelligence_data
        else:
            intel = self.get_cached_intelligence(instrument)

        # Build three-layer context sections
        intel_section = self._format_intelligence_for_prompt(intel, instrument)
        # Run programmatic rules engine if we have enough data
        rules_summary: Dict[str, Any] = {}
        try:
            from intelligence_rules_engine import evaluate_all_rules, summarize_rules  # noqa
            trade_dir = direction if direction != "unknown" else "buy"
            rule_results = evaluate_all_rules(
                pair=instrument,
                trade_direction=trade_dir,
                package=intel,
                ta_confluence=float(score) if score else 0.0,
            )
            rules_summary = summarize_rules(rule_results)
        except Exception as exc:
            # Was silent debug — upgraded 2026-04-24 for visibility after kronos
            # 4-rule gate taught us silent exception swallows hide real bugs.
            logger.warning("Rules engine skipped (fallback active): %s: %s",
                           type(exc).__name__, exc)
            if _flight:
                _flight.record(FlightStage.VALIDATOR_CALL, pair=_pair,
                               data={"fallback": "rules_engine_skipped",
                                     "error_type": type(exc).__name__,
                                     "error": str(exc)[:200]},
                               note="rules engine exception — using 'Rules engine output unavailable' fallback")

        rules_section = (
            self._format_rules_for_prompt(rules_summary) if rules_summary else
            "Rules engine output unavailable."
        )

        # Append to prompt
        parts.append("\n\n---\n## LAYER 1: INTELLIGENCE PACKAGE (Facts)")
        parts.append(intel_section)
        parts.append("\n---\n## LAYER 2: DECISION RULES (Pre-computed)")
        parts.append(rules_section)
        # ──────────────────────────────────────────────────────────────────

        user_content = "\n".join(parts)

        system_prompt = (
            "You are a forex trading risk analyst reviewing a trade signal. "
            "You have access to backtest evidence from 39,000+ setup performance rows, "
            "8.5M historical trades, and a two-layer intelligence context:\n"
            "  - Layer 1 (Facts): macro/cross-asset/COT/calendar intelligence package\n"
            "  - Layer 2 (Rules): pre-computed confidence adjustments and flags\n\n"
            "Apply the intelligence context integration rules:\n"
            "  1. Calendar veto: High-impact event within 4h → HOLD\n"
            "  2. VIX >25 → reduce position size\n"
            "  3. COT extreme + crowded → squeeze risk warning\n"
            "  4. Losing streak 3+ → reduce confidence\n"
            "  5. Strong opposing news → narrative headwind flag\n"
            "  6. User thesis conflict → flag disagreement\n"
            "  7. 3+ cross-asset contradictions → CROSS_ASSET_DIVERGENCE flag\n\n"
            "Key TA factors to also weigh:\n"
            "- Backtest win rate and profit factor (PF > 1.5 = strong edge)\n"
            "- Loss pattern warnings (e.g. low ADX, H4 disagrees)\n"
            "- Confluence score and indicator agreement\n\n"
            "Be conservative — when evidence is conflicting or marginal, "
            "recommend hold.\n\n"
            "Respond in exactly this JSON format:\n"
            '{"recommendation": "proceed|hold|reduce_size", '
            '"reasoning": "analysis integrating TA + intelligence context", '
            '"confidence": 0.0-1.0, '
            '"flags": [], '
            '"intelligence_alignment": {'
            '"macro": "YES|NO|NEUTRAL", '
            '"calendar_clear": true, '
            '"cross_asset": "CONFIRMED|CONTRADICTED|MIXED"'
            '}}'
        )

        result = self._call_llm(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0,
            max_tokens=800,
            fallback_recommendation="hold",
        )

        # Record decision in validator_decisions table
        try:
            from validator_reconciliation import record_decision  # noqa
            verdict_map = {
                "proceed": "approve", "hold": "hold", "reduce_size": "approve"
            }
            verdict = verdict_map.get(
                result.get("recommendation", "hold"), "hold"
            )
            record_decision(
                cycle_id=str(trade_decision.get("cycle_id", "unknown")),
                instrument=instrument,
                verdict=verdict,
                confidence_raw=int(score * 10) if score else None,
                confidence_adjusted=int(
                    (result.get("confidence", 0)) * 100 + (
                        rules_summary.get("total_confidence_adjustment", 0)
                    )
                ),
                confidence_adjustments=json.dumps(
                    rules_summary.get("rules_triggered", [])
                ),
                flags=json.dumps(rules_summary.get("flags", [])),
                position_size_recommendation=rules_summary.get("position_size_label"),
                reasoning=result.get("reasoning"),
                package_id=intel.get("package_id"),
                window=intel.get("package_window"),
                trade_id=str(trade_decision.get("trade_id", "")) or None,
                vix_level=float(
                    (intel.get("cross_asset", {}) or {})
                    .get("vix", {}).get("current_price", 0) or 0
                ) or None,
            )
        except Exception as exc:
            # Was silent debug — upgraded 2026-04-24 for visibility.
            # Decision recording is the audit trail; silent failure = we don't
            # know decisions aren't being written until the table is queried.
            logger.warning("Decision recording skipped (audit trail missing): %s: %s",
                           type(exc).__name__, exc)
            if _flight:
                _flight.record(FlightStage.VALIDATOR_VERDICT, pair=_pair,
                               data={"fallback": "decision_recording_skipped",
                                     "error_type": type(exc).__name__,
                                     "error": str(exc)[:200]},
                               note="decision record insert failed — trade proceeded but no DB row")
        if _flight:
            _flight.record(FlightStage.VALIDATOR_VERDICT, pair=_pair,
                           duration_ms=(time.time() - _t0) * 1000,
                           data={
                               "recommendation": result.get("recommendation"),
                               "confidence": result.get("confidence"),
                           },
                           note=result.get("reasoning", "")[:200])
        return result

    # ------------------------------------------------------------------
    # Hourly batch analysis
    # ------------------------------------------------------------------

    def analyze_hourly(self) -> Dict[str, Any]:
        """Hourly batch analysis of accumulated failures.

        Summarises the last hour of validation results, grouped by
        gate and failure reason, and asks the LLM to identify
        systematic issues.

        Returns:
            Dict with ``status`` (str), ``findings`` (str),
            ``severity`` (str), and ``parameter_suggestions`` (list).
        """
        if not self._history:
            return {
                "status": "no_data",
                "findings": "No validation results recorded yet.",
                "severity": "none",
                "parameter_suggestions": [],
            }

        # Aggregate stats from history
        gate1_pass = sum(1 for r in self._history if r.gate == "gate_1" and r.passed)
        gate1_fail = sum(1 for r in self._history if r.gate == "gate_1" and not r.passed)
        gate2_pass = sum(1 for r in self._history if r.gate == "gate_2" and r.passed)
        gate2_fail = sum(1 for r in self._history if r.gate == "gate_2" and not r.passed)

        # Common failure reasons
        all_issues: List[str] = []
        for r in self._history:
            if not r.passed:
                all_issues.extend(r.issues)
        issue_counts = Counter(all_issues).most_common(10)

        # Escalation count
        escalation_count = sum(
            1 for r in self._history
            if getattr(r, "needs_llm_escalation", False)
        )

        parts = [
            f"Gate 1: {gate1_pass} passed, {gate1_fail} failed",
            f"Gate 2: {gate2_pass} passed, {gate2_fail} failed",
            f"LLM escalations triggered: {escalation_count}",
            f"Total results in window: {len(self._history)}",
            "",
            "Most common failure reasons:",
        ]
        for issue, count in issue_counts:
            parts.append(f"  ({count}x) {issue}")

        user_content = "\n".join(parts)

        system_prompt = (
            "You are a trading system health analyst. Analyze the last "
            "hour of validation failures. Identify systematic issues "
            "(regime changes, data feed problems) vs normal market "
            "behavior. Rate severity: none/low/medium/high/critical.\n\n"
            "Respond in exactly this JSON format:\n"
            '{"status": "summary", "findings": "your analysis", '
            '"severity": "none|low|medium|high|critical", '
            '"parameter_suggestions": ["suggestion1", "suggestion2"]}'
        )

        result = self._call_llm(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0.2,
            max_tokens=1000,
            fallback_recommendation="hold",
        )

        # Map to hourly output format
        return {
            "status": result.get("status", result.get("recommendation", "unknown")),
            "findings": result.get("findings", result.get("reasoning", "")),
            "severity": result.get("severity", "unknown"),
            "parameter_suggestions": result.get("parameter_suggestions", []),
        }

    # ------------------------------------------------------------------
    # Daily parameter review
    # ------------------------------------------------------------------

    def analyze_daily(self, instrument: str = "EUR_USD") -> Dict[str, Any]:
        """Daily parameter effectiveness review.

        Summarises a full day of validation stats and asks the LLM
        whether thresholds need adjustment.

        Args:
            instrument: Instrument name for knowledge base lookup.

        Returns:
            Dict with ``date`` (str), ``analysis`` (str),
            ``recommend_changes`` (bool), and ``recommendations``
            (list of str).
        """
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Aggregate daily stats
        total = len(self._history)
        passed = sum(1 for r in self._history if r.passed)
        failed = total - passed

        gate1_pass = sum(1 for r in self._history if r.gate == "gate_1" and r.passed)
        gate1_fail = sum(1 for r in self._history if r.gate == "gate_1" and not r.passed)
        gate2_pass = sum(1 for r in self._history if r.gate == "gate_2" and r.passed)
        gate2_fail = sum(1 for r in self._history if r.gate == "gate_2" and not r.passed)

        # Confidence distribution
        confidences = [r.confidence for r in self._history if hasattr(r, "confidence")]
        if confidences:
            mean_conf = sum(confidences) / len(confidences)
            below_threshold = sum(1 for c in confidences if c < 0.7) / len(confidences) * 100
        else:
            mean_conf = 0
            below_threshold = 0

        # Top contradictions
        all_issues: List[str] = []
        for r in self._history:
            if not r.passed:
                all_issues.extend(r.issues)
        issue_counts = Counter(all_issues).most_common(5)

        parts = [
            f"Date: {today}",
            f"Total validations: {total} ({passed} passed, {failed} failed)",
            f"Gate 1: {gate1_pass} pass / {gate1_fail} fail",
            f"Gate 2: {gate2_pass} pass / {gate2_fail} fail",
            f"Mean confidence: {mean_conf:.2f}",
            f"Below threshold (< 0.7): {below_threshold:.1f}%",
            "",
            "Top failure reasons:",
        ]
        for issue, count in issue_counts:
            parts.append(f"  ({count}x) {issue}")

        # Knowledge base
        kb_summary = self._load_knowledge_base(instrument)
        if kb_summary:
            parts.append(f"\nKnown winning patterns:\n{kb_summary}")

        user_content = "\n".join(parts)

        system_prompt = (
            "You are a trading system parameter optimizer. Review "
            "today's validation performance. Identify if thresholds "
            "need adjustment. Consider: Is the system too conservative "
            "(blocking good trades) or too permissive (letting bad "
            "trades through)? Recommend specific parameter changes if "
            "warranted.\n\n"
            "Respond in exactly this JSON format:\n"
            '{"date": "YYYY-MM-DD", "analysis": "your analysis", '
            '"recommend_changes": true/false, '
            '"recommendations": ["rec1", "rec2"]}'
        )

        result = self._call_llm(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0.3,
            max_tokens=2000,
            fallback_recommendation="hold",
        )

        return {
            "date": result.get("date", today),
            "analysis": result.get("analysis", result.get("reasoning", "")),
            "recommend_changes": result.get("recommend_changes", False),
            "recommendations": result.get("recommendations", []),
        }

    # ------------------------------------------------------------------
    # Intelligence package DB lookup
    # ------------------------------------------------------------------

    def get_cached_intelligence(self, pair: str) -> Dict[str, Any]:
        """
        Read the latest intelligence package + MiroFish consensus from DB.

        This is a READ-ONLY DB query — zero latency, no API calls.  Called
        right before the validator LLM call to populate Layers 1 and 2.

        Falls back to ``intelligence_snapshots_v2`` when no v2 package exists.

        Args:
            pair: OANDA instrument string, e.g. ``"EUR_USD"``.

        Returns:
            Dict with keys: ``package_id``, ``package_window``,
            ``package_age_minutes``, ``staleness_warning``,
            ``intelligence_briefing``, ``pair_data``, ``cross_asset``,
            ``cot``, ``calendar``, ``risk_factors``.
        """
        try:
            conn = get_intelligence()
            conn.row_factory = sqlite3.Row

            row = conn.execute(
                """
                SELECT id, window, generated_at, package_text, per_pair_data,
                       cross_asset_data, cot_data, calendar_data, risk_factors,
                       data_sources_used, data_sources_failed
                FROM intelligence_packages
                ORDER BY generated_at DESC LIMIT 1
                """
            ).fetchone()

            if not row:
                return self._fallback_to_legacy_snapshots(conn, pair)

            per_pair = json.loads(row["per_pair_data"] or "{}")
            pair_data = per_pair.get(pair, {})

            generated_str = row["generated_at"].replace("Z", "+00:00")
            try:
                generated = datetime.fromisoformat(generated_str)
                age_minutes = (
                    datetime.utcnow() - generated.replace(tzinfo=None)
                ).total_seconds() / 60
            except (ValueError, TypeError):
                age_minutes = 0

            return {
                "package_id": row["id"],
                "package_window": row["window"],
                "package_age_minutes": round(age_minutes, 1),
                "staleness_warning": (
                    "Intelligence package is >8 hours old"
                    if age_minutes > 480
                    else None
                ),
                "intelligence_briefing": row["package_text"],
                "pair_data": pair_data,
                "cross_asset": json.loads(row["cross_asset_data"] or "{}"),
                "cot": json.loads(row["cot_data"] or "{}"),
                "calendar": json.loads(row["calendar_data"] or "{}"),
                "risk_factors": json.loads(row["risk_factors"] or "{}"),
            }

        except Exception as exc:
            logger.warning("Intelligence DB lookup failed: %s", exc)
            return {
                "package_id": None,
                "package_window": None,
                "package_age_minutes": None,
                "staleness_warning": f"Intelligence lookup error: {exc}",
                "intelligence_briefing": None,
                "pair_data": {},
                "cross_asset": {},
                "cot": {},
                "calendar": {},
                "risk_factors": {},
            }

    def _fallback_to_legacy_snapshots(
        self, conn: sqlite3.Connection, pair: str
    ) -> Dict[str, Any]:
        """
        Fallback to ``intelligence_snapshots_v2`` when no v2 package exists.
        """
        try:
            row = conn.execute(
                """
                SELECT * FROM intelligence_snapshots_v2
                WHERE instrument = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (pair,),
            ).fetchone()
        except Exception:
            row = None

        if not row:
            return {
                "package_id": None,
                "package_window": None,
                "package_age_minutes": None,
                "staleness_warning": "No intelligence package available",
                "intelligence_briefing": None,
                "pair_data": {},
                "cross_asset": {},
                "cot": {},
                "calendar": {},
                "risk_factors": {},
            }

        return {
            "package_id": None,
            "package_window": None,
            "package_age_minutes": None,
            "staleness_warning": (
                "Using legacy intelligence snapshot (v2 package not yet available)"
            ),
            "intelligence_briefing": dict(row).get("full_report"),
            "pair_data": dict(row),
            "cross_asset": {},
            "cot": {},
            "calendar": {},
            "risk_factors": {},
        }

    # ------------------------------------------------------------------
    # Intelligence context formatters
    # ------------------------------------------------------------------

    def _format_intelligence_for_prompt(
        self, intel: Dict[str, Any], pair: str
    ) -> str:
        """Format intelligence package context for the LLM prompt."""
        parts: List[str] = []

        if intel.get("staleness_warning"):
            parts.append(f"⚠️  {intel['staleness_warning']}")

        briefing = intel.get("intelligence_briefing")
        if briefing:
            parts.append(briefing)
        else:
            parts.append("No intelligence briefing available for this cycle.")

        age = intel.get("package_age_minutes")
        if age is not None:
            parts.append(f"\n[Package age: {age:.0f} minutes | Window: {intel.get('package_window', 'unknown')}]")

        return "\n".join(parts)

    def _format_rules_for_prompt(self, rules_summary: Dict[str, Any]) -> str:
        """Format pre-computed rules engine output for the LLM prompt."""
        parts: List[str] = []

        total_adj = rules_summary.get("total_confidence_adjustment", 0)
        sign = "+" if total_adj >= 0 else ""
        parts.append(f"**Pre-computed confidence adjustment:** {sign}{total_adj} points")

        flags = rules_summary.get("flags", [])
        if flags:
            parts.append("\n**Flags triggered:**")
            for f in flags:
                parts.append(f"  🚩 {f}")

        pos_label = rules_summary.get("position_size_label", "standard")
        if pos_label != "standard":
            parts.append(f"\n**Position size recommendation:** {pos_label}")

        triggered = rules_summary.get("rules_triggered", [])
        if triggered:
            parts.append("\n**Rules that fired:**")
            for r in triggered:
                adj = r.get("adjustment", 0)
                sign = "+" if adj >= 0 else ""
                adj_str = f" [{sign}{adj}]" if adj != 0 else ""
                parts.append(f"  - {r['rule']}{adj_str}: {r['detail']}")

        clear = rules_summary.get("rules_clear", [])
        if clear:
            parts.append(f"\n**Rules clear:** {', '.join(clear)}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Knowledge base loader
    # ------------------------------------------------------------------

    def _load_knowledge_base(self, instrument: str) -> Optional[str]:
        """Load positive pattern examples for LLM context.

        Reads ``{snapshot_dir}/{instrument}/knowledge_base/positive_patterns.json``
        and returns a summarised string, or *None* if the file does
        not exist (pre-backtesting phase).

        Args:
            instrument: Oanda instrument name.

        Returns:
            Summary string or *None*.
        """
        kb_path = (
            self.snapshot_dir / instrument / "knowledge_base"
            / "positive_patterns.json"
        )
        if not kb_path.exists():
            return None

        try:
            with open(kb_path) as f:
                kb = json.load(f)

            patterns = kb.get("patterns", [])
            if not patterns:
                return None

            lines = [f"  {len(patterns)} winning trades in knowledge base:"]
            for p in patterns[:5]:  # Summarise top 5
                lines.append(
                    f"    - {p.get('pattern_type', 'unknown')}: "
                    f"action={p.get('action')}, score={p.get('score')}, "
                    f"regime={p.get('regime')}, pnl={p.get('pnl_pips')} pips"
                )
            if len(patterns) > 5:
                lines.append(f"    ... and {len(patterns) - 5} more")

            return "\n".join(lines)
        except Exception as exc:
            logger.warning("Failed to load knowledge base: %s", exc)
            return None

    # ------------------------------------------------------------------
    # LLM call with graceful degradation
    # ------------------------------------------------------------------

    def _call_llm(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float,
        max_tokens: int,
        fallback_recommendation: str,
    ) -> Dict[str, Any]:
        """Call the Anthropic API and parse the JSON response.

        If the API is unavailable or the response cannot be parsed,
        returns a conservative fallback.

        Args:
            system_prompt: System-level instruction.
            user_content: User message content.
            temperature: Sampling temperature.
            max_tokens: Maximum response tokens.
            fallback_recommendation: Default recommendation on failure.

        Returns:
            Parsed JSON dict from the LLM, or a fallback dict.
        """
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )

            text = response.content[0].text.strip()

            # Extract JSON from response (handle markdown fences)
            if "```" in text:
                # Extract content between code fences
                start = text.find("{")
                end = text.rfind("}") + 1
                if start >= 0 and end > start:
                    text = text[start:end]

            return json.loads(text)

        except json.JSONDecodeError as exc:
            logger.warning("LLM response not valid JSON: %s", exc)
            return {
                "recommendation": fallback_recommendation,
                "reasoning": f"LLM response parse error -- defaulting to conservative {fallback_recommendation}",
                "confidence": 0.0,
            }
        except Exception as exc:
            logger.warning("LLM call failed: %s: %s", type(exc).__name__, exc)
            return {
                "recommendation": fallback_recommendation,
                "reasoning": f"LLM unavailable -- defaulting to conservative {fallback_recommendation}",
                "confidence": 0.0,
            }
