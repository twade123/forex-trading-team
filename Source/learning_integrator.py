"""
Learning Integrator — Closes the loop between analysis and agent improvement.

The bridge between trade analysis systems (auditor, scout learning, risk tuner)
and the vault (VaultWriter). Every trade audit, rolling audit, thesis audit,
and risk tuning event produces learnings that flow back into agent prompts
via the vault, making every agent smarter on the next cycle.

Called from:
    - trade_auditor.py  → after _store_audit(), rolling_audit(), thesis_audit()
    - risk_auto_tuner.py → after apply_recommendations()
    - scout_learning_system.py → after record_trade_outcome()

Writes to:
    - Vault agents/{agent}/learnings.md  (per-agent corrections)
    - Vault collective/patterns/{date}.md  (universal insights)
    - Vault agents/{agent}/improvements.md  (prompt change proposals)
    - Data/{pair}/knowledge.json  (live performance overlay)
    - dashboard/learning_events.json  (UI feed)

Usage:
    from learning_integrator import LearningIntegrator

    integrator = LearningIntegrator()
    learnings = integrator.process_trade_audit(audit_result)
    retro = integrator.full_session_retrospective(audit_result)
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from db_pool import get_trading_forex

logger = logging.getLogger("trading_bot.learning_integrator")


def _get_flight_recorder():
    """Import the flight recorder singleton."""
    try:
        from flight_recorder import flight, FlightStage
        return flight, FlightStage
    except ImportError:
        return None, None

# Add parent paths for imports
_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SOURCE_DIR)
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

# Vault writer location — search worktrees for the canonical copy
_VAULT_WRITER_DIRS = []
_worktrees = os.path.join(_PROJECT_DIR, ".claude", "worktrees")
if os.path.isdir(_worktrees):
    for wt in os.listdir(_worktrees):
        kdir = os.path.join(_worktrees, wt, "knowledge")
        if os.path.isfile(os.path.join(kdir, "vault_writer.py")):
            _VAULT_WRITER_DIRS.append(kdir)
            break

# Also check jarvis root
_jarvis_knowledge = os.path.expanduser("~/jarvis/knowledge")
if os.path.isfile(os.path.join(_jarvis_knowledge, "vault_writer.py")):
    _VAULT_WRITER_DIRS.append(_jarvis_knowledge)


def _get_vault_writer():
    """Import VaultWriter from wherever it lives."""
    for vdir in _VAULT_WRITER_DIRS:
        if vdir not in sys.path:
            sys.path.insert(0, vdir)
        # Also add the parent dir so `from knowledge.indexer import ...`
        # works inside vault_writer.py (knowledge must be a package on sys.path)
        parent = os.path.dirname(vdir)
        if parent and parent not in sys.path:
            sys.path.insert(0, parent)
        try:
            from vault_writer import VaultWriter
            return VaultWriter()
        except ImportError:
            continue
    # Last resort — try direct import
    try:
        from knowledge.vault_writer import VaultWriter
        return VaultWriter()
    except ImportError:
        logger.error("Could not import VaultWriter from any location")
        return None


# Thresholds for when to write learnings (avoid noise)
SIGNAL_ACCURACY_WARN = 70.0       # Below this → write scout learning
ENTRY_TIMING_WARN = 50.0          # Below this → write validator learning
EXIT_QUALITY_WARN = 50.0          # Below this → write guardian learning
GUARDIAN_FALSE_ALARM_WARN = 0.40  # Above this → write guardian learning
GUARDIAN_MISS_WARN = 0.30         # Above this → write guardian learning
BACKTEST_DELTA_WARN = -0.15       # Live WR below backtest by this → alert
MIN_TRADES_FOR_DELTA = 10         # Minimum trades before comparing to backtest
DRIFT_THRESHOLD_PP = 15           # Percentage point drop to flag drift

# Dashboard events file
_DASHBOARD_DIR = os.path.join(_PROJECT_DIR, "dashboard")
_LEARNING_EVENTS_PATH = os.path.join(_DASHBOARD_DIR, "learning_events.json")


class LearningIntegrator:
    """Bridge between trade analysis and the vault learning system.

    Extracts actionable learnings from audit results and writes them
    to agent-specific and collective vault locations. Also updates
    per-pair live performance in KnowledgeStore and pushes events
    to the dashboard feed.
    """

    def __init__(self):
        self._vault = _get_vault_writer()
        self._knowledge = None  # lazy load
        self._events: List[Dict] = []  # batch for dashboard push
        self._flight, self._FlightStage = _get_flight_recorder()

    def _record(self, stage_name: str, pair: str = "", cycle_id: str = "",
                 trade_id: str = "", data: Dict = None, status: str = "ok",
                 duration_ms: float = 0, note: str = ""):
        """Record a learning stage to the flight recorder."""
        if not self._flight or not self._FlightStage:
            return
        try:
            stage = self._FlightStage(stage_name)
            self._flight.record(
                stage=stage, pair=pair, cycle_id=cycle_id,
                trade_id=trade_id, data=data or {},
                status=status, duration_ms=duration_ms, note=note,
            )
        except (ValueError, Exception) as e:
            logger.debug("Flight record failed for %s: %s", stage_name, e)

    @property
    def vault(self):
        if self._vault is None:
            self._vault = _get_vault_writer()
        return self._vault

    @property
    def knowledge(self):
        if self._knowledge is None:
            try:
                from knowledge_store import KnowledgeStore
                self._knowledge = KnowledgeStore()
            except ImportError:
                logger.warning("KnowledgeStore not available")
        return self._knowledge

    # ==================================================================
    # Vault: same-day code changes awareness
    # ==================================================================

    def _load_todays_code_changes(self) -> List[Dict[str, Any]]:
        """Load vault entries from today that describe code/config changes.

        Returns a list of dicts with keys: date, agent, type, summary, context, tags.
        The learning loop uses these to annotate trade evaluations — a trade that
        closed BEFORE a change was activated should not be judged by the new logic.
        """
        changes: List[Dict[str, Any]] = []
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Search all agent learnings files for today's entries with MODULE: marker
        vault_root = None
        for vdir in _VAULT_WRITER_DIRS:
            _agents_dir = os.path.join(vdir, "agents")
            if os.path.isdir(_agents_dir):
                vault_root = vdir
                break
        if not vault_root:
            # Try standard Jarvis path
            _jarvis_vault = os.path.join(os.path.dirname(_PROJECT_DIR), "knowledge")
            if os.path.isdir(os.path.join(_jarvis_vault, "agents")):
                vault_root = _jarvis_vault

        if not vault_root:
            return changes

        agents_dir = os.path.join(vault_root, "agents")
        try:
            for agent_name in os.listdir(agents_dir):
                learnings_path = os.path.join(agents_dir, agent_name, "learnings.md")
                if not os.path.isfile(learnings_path):
                    continue
                try:
                    content = open(learnings_path, "r").read()
                except Exception:
                    continue

                # Parse markdown entries — each starts with ## heading
                # and has **Date:** YYYY-MM-DDTHH:MM:SS
                import re
                entries = re.split(r'\n---\n', content)
                for entry in entries:
                    if today_str not in entry:
                        continue
                    # Only include entries that describe code changes (have MODULE: marker)
                    if "MODULE:" not in entry and "LAYER:" not in entry:
                        continue

                    date_match = re.search(r'\*\*Date:\*\*\s*(\S+)', entry)
                    type_match = re.search(r'\*\*Type:\*\*\s*(\S+)', entry)
                    tags_match = re.search(r'\*\*Tags:\*\*\s*(.+)', entry)
                    # Grab the heading text (after ## and optional emoji)
                    heading_match = re.search(r'^## [^\n]+', entry, re.MULTILINE)
                    summary_text = ""
                    if heading_match:
                        summary_text = re.sub(r'^## [^\w]*', '', heading_match.group(0)).strip()

                    # Extract ACTIVATED timestamp from context
                    activated_match = re.search(r'ACTIVATED:\s*~?(\d{2}:\d{2})\s*UTC', entry)

                    changes.append({
                        "agent": agent_name,
                        "date": date_match.group(1) if date_match else today_str,
                        "type": type_match.group(1) if type_match else "unknown",
                        "summary": summary_text,
                        "tags": tags_match.group(1).strip() if tags_match else "",
                        "activated_utc": activated_match.group(1) if activated_match else None,
                        "raw": entry[:500],
                    })
        except Exception as e:
            logger.debug("Failed to load today's code changes from vault: %s", e)

        return changes

    def _annotate_trade_with_changes(self, audit_result: Dict, changes: List[Dict]) -> Dict[str, Any]:
        """Check if this trade was affected by any code change deployed today.

        If the trade CLOSED before a change was activated, the trade ran under
        the OLD logic and should be annotated accordingly. If the trade opened
        AFTER the change, it ran under the NEW logic.

        Returns:
            Dict with keys: affected_by (list of change summaries),
            ran_under_old_logic (bool), annotation (str for audit note)
        """
        result = {"affected_by": [], "ran_under_old_logic": False, "annotation": ""}
        if not changes:
            return result

        entry_time_str = audit_result.get("entry_time", "")
        close_time_str = audit_result.get("close_time", audit_result.get("exit_time", ""))

        entry_dt = self._parse_time(entry_time_str) if entry_time_str else None
        close_dt = self._parse_time(close_time_str) if close_time_str else None

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        annotations = []

        for ch in changes:
            activated = ch.get("activated_utc")
            if not activated:
                continue

            try:
                activated_dt = datetime.strptime(
                    f"{today_str}T{activated}:00+00:00", "%Y-%m-%dT%H:%M:%S%z"
                )
            except ValueError:
                continue

            # Relevant tags for matching to trade components
            tags = ch.get("tags", "").lower()
            summary = ch.get("summary", "")

            if close_dt and close_dt < activated_dt:
                # Trade closed BEFORE this change was activated → old logic
                annotations.append(f"[OLD LOGIC] {summary} (activated {activated} UTC, trade closed before)")
                result["ran_under_old_logic"] = True
            elif entry_dt and entry_dt >= activated_dt:
                # Trade opened AFTER change → new logic
                annotations.append(f"[NEW LOGIC] {summary} (activated {activated} UTC)")

            result["affected_by"].append(summary)

        result["annotation"] = "; ".join(annotations) if annotations else ""
        return result

    # ==================================================================
    # Phase 1: Single Trade Audit → Vault Learnings
    # ==================================================================

    def process_trade_audit(self, audit_result: Dict[str, Any]) -> List[str]:
        """Extract learnings from a single trade audit and write to vault.

        Called after trade_auditor.audit_trade() stores its result.

        Returns:
            List of learning descriptions written.
        """
        loop_start = time.time()

        if not self.vault:
            logger.warning("No vault writer available — skipping learning integration")
            return []

        # ── Provenance: stamp source_user_id so all collective writes are traceable ──
        # audit_result["user_id"] is set by callers who pull it from live_trades.user_id.
        # System/scheduler processes may omit it — that's fine, we write None.
        source_user_id = audit_result.get("user_id")
        if source_user_id is not None:
            source_user_id = int(source_user_id)
        audit_result["_source_user_id"] = source_user_id

        # ── Load today's code changes from vault ──
        # Annotate the audit with which logic version the trade ran under
        todays_changes = self._load_todays_code_changes()
        change_annotation = self._annotate_trade_with_changes(audit_result, todays_changes)
        if change_annotation.get("annotation"):
            audit_result["_code_change_context"] = change_annotation["annotation"]
            audit_result["_ran_under_old_logic"] = change_annotation.get("ran_under_old_logic", False)
            logger.info(
                "Learning loop: trade %s code change context: %s",
                audit_result.get("trade_id", "?"), change_annotation["annotation"][:200]
            )

        learnings_written: List[str] = []
        pair = audit_result.get("pair", "UNKNOWN")
        cycle_id = audit_result.get("cycle_id", "")
        trade_id = audit_result.get("trade_id", "")
        outcome = audit_result.get("outcome", "")
        pnl_pips = audit_result.get("pnl_pips", 0)
        setup_name = audit_result.get("setup_name", "")
        entry_type = audit_result.get("entry_type", "")
        direction = audit_result.get("direction", "")

        # ── Flight: LEARNING_AUDIT ──
        self._record("learning_audit", pair=pair, cycle_id=cycle_id,
                      trade_id=trade_id, data={
                          "audit_id": trade_id, "pair": pair, "outcome": outcome,
                          "pnl_pips": pnl_pips, "setup": setup_name,
                      }, note=f"Learning extraction started for {pair} {outcome}")

        # 1. Scout signal accuracy
        t0 = time.time()
        scout_learnings = self._extract_scout_learnings(audit_result)
        learnings_written += scout_learnings
        self._record("learning_scout", pair=pair, cycle_id=cycle_id,
                      trade_id=trade_id, data={
                          "learnings_count": len(scout_learnings),
                          "learnings": scout_learnings[:3],
                      }, duration_ms=(time.time() - t0) * 1000,
                      status="ok" if scout_learnings else "skip",
                      note=f"{len(scout_learnings)} scout learnings")

        # 2. Validator verdict correctness + entry timing
        t0 = time.time()
        validator_learnings = self._extract_validator_learnings(audit_result)
        learnings_written += validator_learnings
        self._record("learning_validator", pair=pair, cycle_id=cycle_id,
                      trade_id=trade_id, data={
                          "learnings_count": len(validator_learnings),
                          "learnings": validator_learnings[:3],
                      }, duration_ms=(time.time() - t0) * 1000,
                      status="ok" if validator_learnings else "skip",
                      note=f"{len(validator_learnings)} validator learnings")

        # 3. Guardian exit management
        t0 = time.time()
        guardian_learnings = self._extract_guardian_learnings(audit_result)
        learnings_written += guardian_learnings
        self._record("learning_guardian", pair=pair, cycle_id=cycle_id,
                      trade_id=trade_id, data={
                          "learnings_count": len(guardian_learnings),
                          "learnings": guardian_learnings[:3],
                      }, duration_ms=(time.time() - t0) * 1000,
                      status="ok" if guardian_learnings else "skip",
                      note=f"{len(guardian_learnings)} guardian learnings")

        # 3b. Winning setup revenue → vault (keeps vault + DB in sync)
        t0 = time.time()
        revenue_learnings = self._extract_winning_setup_learnings(audit_result)
        learnings_written += revenue_learnings
        self._record("learning_revenue", pair=pair, cycle_id=cycle_id,
                      trade_id=trade_id, data={
                          "learnings_count": len(revenue_learnings),
                          "learnings": revenue_learnings[:3],
                      }, duration_ms=(time.time() - t0) * 1000,
                      status="ok" if revenue_learnings else "skip",
                      note=f"{len(revenue_learnings)} winning setup learnings")

        # 4. Update per-pair live performance
        t0 = time.time()
        self._update_live_knowledge(audit_result)
        live_perf = self._get_live_perf_snapshot(audit_result)
        self._record("learning_knowledge", pair=pair, cycle_id=cycle_id,
                      trade_id=trade_id, data={
                          "pair": pair, "setup": setup_name,
                          "live_win_rate": live_perf.get("win_rate", 0),
                          "trades": live_perf.get("trades", 0),
                          "backtest_delta": live_perf.get("backtest_delta", 0),
                      }, duration_ms=(time.time() - t0) * 1000,
                      note=f"Knowledge updated: {pair} {setup_name}")

        # 5. Push to dashboard
        t0 = time.time()
        self._push_learning_event({
            "type": "trade_audit_learning",
            "pair": pair,
            "outcome": outcome,
            "pnl_pips": pnl_pips,
            "setup": setup_name,
            "learnings_count": len(learnings_written),
            "learnings": learnings_written[:5],
        })
        self._flush_events()
        self._record("learning_dashboard", pair=pair, cycle_id=cycle_id,
                      trade_id=trade_id, data={
                          "events_written": 1,
                      }, duration_ms=(time.time() - t0) * 1000,
                      note="Dashboard learning events updated")

        # ── Flight: LEARNING_COMPLETE ──
        total_ms = (time.time() - loop_start) * 1000
        self._record("learning_complete", pair=pair, cycle_id=cycle_id,
                      trade_id=trade_id, data={
                          "total_learnings": len(learnings_written),
                          "duration_ms": round(total_ms),
                          "scout_count": len(scout_learnings),
                          "validator_count": len(validator_learnings),
                          "guardian_count": len(guardian_learnings),
                          "outcome": outcome,
                          "pnl_pips": pnl_pips,
                      }, duration_ms=total_ms,
                      note=f"Learning loop complete: {len(learnings_written)} learnings in {total_ms:.0f}ms")

        logger.info(
            "Trade audit → %d vault learnings for %s %s (%+.1f pips) [%.0fms]",
            len(learnings_written), pair, outcome, pnl_pips, total_ms,
        )

        # Refresh pipeline lineage report after each trade audit
        try:
            from pipeline_lineage import PipelineLineage
            PipelineLineage().write_dashboard_report(hours_back=24)
            self._push_sse("lineage_update", {"refresh": True})
        except Exception as e:
            logger.debug("Lineage report refresh failed (non-fatal): %s", e)

        return learnings_written

    def _get_live_perf_snapshot(self, audit_result: Dict) -> Dict:
        """Get a snapshot of live performance for flight recorder data."""
        try:
            if not self.knowledge:
                return {}
            pair = audit_result.get("pair", "")
            setup = audit_result.get("setup_name", "")
            if not pair or not setup:
                return {}
            k = self.knowledge.get_knowledge(pair)
            return k.get("live_performance", {}).get(setup, {})
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Scout learning extraction
    # ------------------------------------------------------------------

    def _extract_scout_learnings(self, a: Dict) -> List[str]:
        """Extract scout-specific learnings from audit result.

        If _code_change_context is set, learnings include the annotation so
        the vault records show awareness of which logic version the trade used.
        """
        learnings = []
        _change_note = f" [CODE CHANGE: {a['_code_change_context'][:150]}]" if a.get("_code_change_context") else ""
        pair = a.get("pair", "")
        setup_name = a.get("setup_name", "")
        entry_type = a.get("entry_type", "")
        direction = a.get("direction", "")
        outcome = a.get("outcome", "")
        pnl_pips = a.get("pnl_pips", 0)
        signal_accuracy = a.get("scout_signal_accuracy", 100)
        thesis_correct = a.get("scout_thesis_correct", 1)
        mfe = a.get("max_favorable_pips", 0)
        mae = a.get("max_adverse_pips", 0)
        what_changed = a.get("what_changed", {})
        market_story = a.get("market_story_at_close", {})
        accuracy_trend = a.get("accuracy_trend", "stable")

        # Parse JSON fields if they're strings
        if isinstance(what_changed, str):
            try:
                what_changed = json.loads(what_changed)
            except (json.JSONDecodeError, TypeError):
                what_changed = {}
        if isinstance(market_story, str):
            try:
                market_story = json.loads(market_story)
            except (json.JSONDecodeError, TypeError):
                market_story = {}

        # Low signal accuracy
        if signal_accuracy and signal_accuracy < SIGNAL_ACCURACY_WARN:
            scout_claims = a.get("scout_claims", {})
            reality = a.get("reality_at_entry", {})
            if isinstance(scout_claims, str):
                try:
                    scout_claims = json.loads(scout_claims)
                except (json.JSONDecodeError, TypeError):
                    scout_claims = {}
            if isinstance(reality, str):
                try:
                    reality = json.loads(reality)
                except (json.JSONDecodeError, TypeError):
                    reality = {}

            self.vault.record_agent_learning("scout", {
                "type": "correction",
                "summary": (
                    f"{pair} {setup_name}: signal accuracy {signal_accuracy:.0f}% "
                    f"— scout claims didn't match market reality"
                ),
                "context": (
                    f"Scout claimed: {json.dumps(scout_claims, default=str)[:300]}. "
                    f"Reality at entry: {json.dumps(reality, default=str)[:300]}. "
                    f"Outcome: {outcome} ({pnl_pips:+.1f} pips). "
                    f"Direction: {direction}."
                ),
                "evidence": (
                    f"signal_accuracy={signal_accuracy:.1f}%, "
                    f"thesis_correct={'yes' if thesis_correct else 'no'}, "
                    f"MFE={mfe:.1f}p, MAE={mae:.1f}p"
                ),
                "tags": [pair, setup_name, "signal_accuracy", outcome],
                "universal": False,
            })
            learnings.append(f"scout_signal_accuracy_{pair}_{signal_accuracy:.0f}pct")

        # Thesis failure
        if thesis_correct == 0 and entry_type:
            change_summary = ""
            if what_changed:
                changes = [f"{k}: {v.get('entry','?')}→{v.get('close','?')}"
                           for k, v in what_changed.items()
                           if isinstance(v, dict)]
                change_summary = "; ".join(changes[:3])

            self.vault.record_agent_learning("scout", {
                "type": "failure",
                "summary": (
                    f"{pair}: {entry_type} thesis did not play out — "
                    f"{outcome} ({pnl_pips:+.1f} pips)"
                ),
                "context": (
                    f"Setup: {setup_name}. Direction: {direction}. "
                    f"Expected {entry_type} move but trade resulted in {outcome}. "
                    f"What changed between entry and close: {change_summary or 'unknown'}. "
                    f"MFE={mfe:.1f}p, MAE={mae:.1f}p."
                ),
                "evidence": (
                    f"thesis_correct=0, outcome={outcome}, pnl={pnl_pips:+.1f}p, "
                    f"entry_type={entry_type}"
                ),
                "tags": [pair, entry_type, "thesis_failure", direction],
                "universal": True,
                "metadata": {
                    "source_user_id": str(a["_source_user_id"]) if a.get("_source_user_id") is not None else None,
                    "trade_id": a.get("trade_id"),
                    "pair": pair,
                },
            })
            learnings.append(f"scout_thesis_failure_{pair}_{entry_type}")

        # Accuracy trend degrading
        if accuracy_trend == "degrading":
            prior_acc = a.get("prior_5_accuracy", 0)
            self.vault.record_agent_learning("scout", {
                "type": "correction",
                "summary": (
                    f"{pair}: signal accuracy trending DOWN — "
                    f"recent 5-trade avg {prior_acc:.0f}%"
                ),
                "context": (
                    f"The last 5 audited trades on {pair} show declining signal accuracy. "
                    f"Scout's market reads are becoming less reliable on this pair. "
                    f"Consider raising confluence threshold or reviewing scan conditions."
                ),
                "evidence": f"accuracy_trend=degrading, prior_5_accuracy={prior_acc:.0f}%",
                "tags": [pair, "drift_alert", "accuracy_degrading"],
                "universal": False,
            })
            learnings.append(f"scout_accuracy_degrading_{pair}")

        return learnings

    # ------------------------------------------------------------------
    # Validator learning extraction
    # ------------------------------------------------------------------

    def _extract_validator_learnings(self, a: Dict) -> List[str]:
        """Extract validator-specific learnings from audit result."""
        learnings = []
        pair = a.get("pair", "")
        setup_name = a.get("setup_name", "")
        direction = a.get("direction", "")
        outcome = a.get("outcome", "")
        pnl_pips = a.get("pnl_pips", 0)
        validator_verdict = a.get("validator_verdict", "")
        validator_correct = a.get("validator_correct", 1)
        entry_timing = a.get("entry_timing_score", 100)
        mfe = a.get("max_favorable_pips", 0)
        mae = a.get("max_adverse_pips", 0)
        time_to_mfe = a.get("time_to_max_favorable_min", 0)
        what_changed = a.get("what_changed", {})
        market_story = a.get("market_story_at_close", {})

        if isinstance(what_changed, str):
            try:
                what_changed = json.loads(what_changed)
            except (json.JSONDecodeError, TypeError):
                what_changed = {}
        if isinstance(market_story, str):
            try:
                market_story = json.loads(market_story)
            except (json.JSONDecodeError, TypeError):
                market_story = {}

        # Validator was wrong: confirmed but trade lost
        if (validator_correct == 0
                and validator_verdict
                and validator_verdict.upper() in ("CONFIRM", "APPROVE")
                and outcome == "loss"):
            change_desc = ""
            if what_changed:
                changes = [f"{k}: {v.get('entry','?')}→{v.get('close','?')}"
                           for k, v in what_changed.items()
                           if isinstance(v, dict)]
                change_desc = "; ".join(changes[:3])

            self.vault.record_agent_learning("validator", {
                "type": "correction",
                "summary": (
                    f"False confirmation: {pair} {setup_name} confirmed "
                    f"but lost {abs(pnl_pips):.1f} pips"
                ),
                "context": (
                    f"Validator said {validator_verdict} on {pair} {direction} {setup_name}. "
                    f"Trade lost {abs(pnl_pips):.1f} pips. "
                    f"Entry timing: {entry_timing:.0f}/100. "
                    f"MAE reached {mae:.1f} pips. MFE was only {mfe:.1f} pips. "
                    f"What changed: {change_desc or 'unknown'}."
                ),
                "evidence": (
                    f"validator_correct=0, verdict={validator_verdict}, "
                    f"outcome=loss, pnl={pnl_pips:+.1f}p, "
                    f"entry_timing={entry_timing:.0f}, MFE={mfe:.1f}p, MAE={mae:.1f}p"
                ),
                "tags": [pair, setup_name, "false_confirm", direction],
                "universal": False,
            })
            learnings.append(f"validator_false_confirm_{pair}")

        # Poor entry timing
        if entry_timing is not None and entry_timing < ENTRY_TIMING_WARN:
            early_or_late = "early" if mae > mfe * 0.5 else "late"
            self.vault.record_agent_learning("validator", {
                "type": "improvement",
                "summary": (
                    f"{pair}: entry timing {entry_timing:.0f}/100 "
                    f"— entered too {early_or_late}"
                ),
                "context": (
                    f"MFE {mfe:.1f} pips reached {time_to_mfe} min after entry. "
                    f"MAE {mae:.1f} pips means price went {mae:.1f} pips against before recovering. "
                    f"Setup: {setup_name}. Direction: {direction}. "
                    f"{'Wait for pullback completion before entering.' if early_or_late == 'early' else 'Enter earlier — setup was already moving.'}"
                ),
                "evidence": (
                    f"entry_timing={entry_timing:.0f}, MFE={mfe:.1f}p, MAE={mae:.1f}p, "
                    f"time_to_mfe={time_to_mfe}min, outcome={outcome}"
                ),
                "tags": [pair, "entry_timing", setup_name, early_or_late],
                "universal": False,
            })
            learnings.append(f"validator_entry_timing_{pair}_{entry_timing:.0f}")

        return learnings

    # ------------------------------------------------------------------
    # Guardian learning extraction
    # ------------------------------------------------------------------

    def _extract_guardian_learnings(self, a: Dict) -> List[str]:
        """Extract guardian-specific learnings from audit result."""
        learnings = []
        pair = a.get("pair", "")
        outcome = a.get("outcome", "")
        pnl_pips = a.get("pnl_pips", 0)
        mfe = a.get("max_favorable_pips", 0)
        mae = a.get("max_adverse_pips", 0)
        exit_quality = a.get("exit_quality_score", 100)

        # ── Code change awareness ──
        # If this trade ran under old logic (before a code change was deployed),
        # skip generating corrective learnings — the issue may already be fixed.
        _change_ctx = a.get("_code_change_context", "")
        _old_logic = a.get("_ran_under_old_logic", False)
        if _old_logic and outcome == "loss":
            logger.info(
                "Guardian learning: SKIPPING corrective learnings for %s — "
                "trade ran under OLD logic: %s", a.get("trade_id", "?"), _change_ctx[:200]
            )
            # Still record a note so the vault shows we're aware
            if self.vault:
                self.vault.record_agent_learning("guardian", {
                    "type": "note",
                    "summary": f"{pair}: loss under OLD logic — fix already deployed",
                    "context": (
                        f"Trade {a.get('trade_id', '?')} lost {abs(pnl_pips):.1f}p but ran under "
                        f"pre-fix logic. Change deployed mid-session: {_change_ctx[:300]}. "
                        f"Not generating corrective learnings — fix should address this."
                    ),
                    "tags": [pair, "old_logic", "code_change_aware"],
                    "universal": False,
                })
                learnings.append(f"guardian_old_logic_noted_{pair}")
            return learnings
        time_in_drawdown = a.get("time_in_drawdown_min", 0)
        g_assessments = a.get("guardian_assessments", 0) or 0
        g_correct = a.get("guardian_correct_calls", 0) or 0
        g_false = a.get("guardian_false_alarms", 0) or 0
        g_misses = a.get("guardian_misses", 0) or 0
        g_accuracy = a.get("guardian_accuracy", 100) or 0

        # Poor exit quality — leaving pips on the table
        if exit_quality is not None and exit_quality < EXIT_QUALITY_WARN and mfe > 5:
            pips_left = mfe - pnl_pips if pnl_pips > 0 else mfe
            if pnl_pips > 0:
                exit_desc = (
                    f"Trailed too tight — captured {pnl_pips:.1f}p of {mfe:.1f}p available. "
                    f"Left {pips_left:.1f} pips on the table."
                )
            else:
                exit_desc = (
                    f"Exit too late — gave back gains. MFE was +{mfe:.1f}p but "
                    f"closed at {pnl_pips:+.1f}p."
                )

            self.vault.record_agent_learning("guardian", {
                "type": "improvement",
                "summary": (
                    f"{pair}: captured only {exit_quality:.0f}% of available move "
                    f"({pnl_pips:+.1f}p of {mfe:.1f}p MFE)"
                ),
                "context": (
                    f"{exit_desc} "
                    f"Time in drawdown: {time_in_drawdown} min. "
                    f"Guardian accuracy: {g_accuracy:.0f}%. "
                    f"Review trailing stop behavior — "
                    f"{'consider wider trail to let winners run' if pnl_pips > 0 else 'tighten trail to protect gains earlier'}."
                ),
                "evidence": (
                    f"exit_quality={exit_quality:.0f}%, MFE={mfe:.1f}p, "
                    f"pnl={pnl_pips:+.1f}p, time_in_drawdown={time_in_drawdown}min"
                ),
                "tags": [pair, "exit_quality", "guardian_tuning"],
                "universal": False,
            })
            learnings.append(f"guardian_exit_quality_{pair}_{exit_quality:.0f}pct")

        # High false alarm rate
        if g_assessments > 0:
            false_rate = g_false / g_assessments
            if false_rate > GUARDIAN_FALSE_ALARM_WARN:
                self.vault.record_agent_learning("guardian", {
                    "type": "correction",
                    "summary": (
                        f"{pair}: guardian false alarm rate {false_rate:.0%} "
                        f"— threat scoring too sensitive"
                    ),
                    "context": (
                        f"{g_false} false alarms out of {g_assessments} threat assessments. "
                        f"Guardian is seeing threats that don't materialize, "
                        f"causing premature exits or unnecessary tightening. "
                        f"Consider raising ZONE_YELLOW threshold or reducing "
                        f"momentum weight in threat scoring."
                    ),
                    "evidence": (
                        f"false_alarms={g_false}, correct={g_correct}, "
                        f"misses={g_misses}, accuracy={g_accuracy:.0f}%"
                    ),
                    "tags": [pair, "guardian_sensitivity", "false_alarms"],
                    "universal": True,
                    "metadata": {
                        "source_user_id": str(a["_source_user_id"]) if a.get("_source_user_id") is not None else None,
                        "trade_id": a.get("trade_id"),
                        "pair": pair,
                    },
                })
                learnings.append(f"guardian_false_alarms_{pair}_{false_rate:.0%}")

        # High miss rate
        if g_assessments > 0:
            miss_rate = g_misses / g_assessments
            if miss_rate > GUARDIAN_MISS_WARN:
                self.vault.record_agent_learning("guardian", {
                    "type": "failure",
                    "summary": (
                        f"{pair}: guardian missed {g_misses} threats — "
                        f"not catching reversals early enough"
                    ),
                    "context": (
                        f"Miss rate {miss_rate:.0%}. MAE of {mae:.1f} pips suggests "
                        f"price moved significantly against us without triggering "
                        f"threat escalation. Consider lowering ZONE_YELLOW threshold "
                        f"or adding faster momentum shift detection."
                    ),
                    "evidence": (
                        f"misses={g_misses}, MAE={mae:.1f}p, "
                        f"accuracy={g_accuracy:.0f}%"
                    ),
                    "tags": [pair, "guardian_sensitivity", "missed_threats"],
                    "universal": True,
                    "metadata": {
                        "source_user_id": str(a["_source_user_id"]) if a.get("_source_user_id") is not None else None,
                        "trade_id": a.get("trade_id"),
                        "pair": pair,
                    },
                })
                learnings.append(f"guardian_misses_{pair}_{miss_rate:.0%}")

        # ── Guardian action timeline analysis ──
        # Analyze the actual SL/TP modifications the guardian made during the trade
        actions = a.get("guardian_actions", [])
        phases = a.get("guardian_phases", [])
        sl_moves = a.get("guardian_sl_moves", 0)
        tp_moves = a.get("guardian_tp_moves", 0)

        if actions:
            # Build action summary for vault
            action_types = {}
            sl_timeline = []
            for act in actions:
                atype = act.get("action", "unknown")
                action_types[atype] = action_types.get(atype, 0) + 1
                if act.get("old_sl") and act.get("new_sl"):
                    sl_timeline.append({
                        "action": atype,
                        "old_sl": act["old_sl"],
                        "new_sl": act["new_sl"],
                        "pnl_pips": act.get("pnl_pips", 0),
                        "timestamp": act.get("timestamp", ""),
                    })

            action_summary = ", ".join(f"{k}={v}" for k, v in action_types.items())

            # Was trail too aggressive? (SL moved many times but trade lost or left pips)
            if sl_moves >= 3 and outcome == "loss":
                self.vault.record_agent_learning("guardian", {
                    "type": "correction",
                    "summary": (
                        f"{pair}: {sl_moves} SL modifications but trade lost {abs(pnl_pips):.1f}p "
                        f"— trail may be too aggressive"
                    ),
                    "context": (
                        f"Guardian modified SL {sl_moves} times during trade but still "
                        f"ended in a {pnl_pips:+.1f}p loss. MFE was +{mfe:.1f}p. "
                        f"Actions taken: {action_summary}. "
                        f"Consider: wider buffer on trail, slower trail speed, "
                        f"or different phase-based thresholds."
                    ),
                    "evidence": (
                        f"sl_moves={sl_moves}, tp_moves={tp_moves}, "
                        f"pnl={pnl_pips:+.1f}p, MFE={mfe:.1f}p, "
                        f"actions={action_summary}"
                    ),
                    "tags": [pair, "guardian_trail", "sl_too_aggressive"],
                    "universal": True,
                    "metadata": {
                        "source_user_id": str(a["_source_user_id"]) if a.get("_source_user_id") is not None else None,
                        "trade_id": a.get("trade_id"),
                        "pair": pair,
                    },
                })
                learnings.append(f"guardian_trail_aggressive_{pair}")

            # Did ratchet TP help? (TP extended and trade won)
            if tp_moves > 0 and outcome == "win" and pnl_pips > 5:
                self.vault.record_agent_learning("guardian", {
                    "type": "discovery",
                    "summary": (
                        f"{pair}: ratchet TP extended {tp_moves}x — captured {pnl_pips:+.1f}p"
                    ),
                    "context": (
                        f"Guardian extended TP {tp_moves} times during trade, "
                        f"letting the winner run to +{pnl_pips:.1f}p (MFE={mfe:.1f}p). "
                        f"Actions: {action_summary}. "
                        f"Ratchet TP is working well on {pair}."
                    ),
                    "evidence": (
                        f"tp_moves={tp_moves}, pnl={pnl_pips:+.1f}p, MFE={mfe:.1f}p"
                    ),
                    "tags": [pair, "ratchet_tp", "guardian_success"],
                    "universal": True,
                    "metadata": {
                        "source_user_id": str(a["_source_user_id"]) if a.get("_source_user_id") is not None else None,
                        "trade_id": a.get("trade_id"),
                        "pair": pair,
                    },
                })
                learnings.append(f"guardian_ratchet_success_{pair}")

            # Breakeven move effectiveness — did moving to BE save us or clip us?
            be_actions = [a for a in actions if a.get("action") == "moved_sl_to_breakeven"]
            if be_actions and outcome == "loss" and pnl_pips > -3:
                # Lost but only slightly — BE move likely clipped a recovering trade
                self.vault.record_agent_learning("guardian", {
                    "type": "improvement",
                    "summary": (
                        f"{pair}: BE move may have clipped trade — lost only {abs(pnl_pips):.1f}p"
                    ),
                    "context": (
                        f"SL moved to breakeven but trade closed at {pnl_pips:+.1f}p. "
                        f"MFE was +{mfe:.1f}p. The trade likely retraced through BE "
                        f"and could have recovered. Consider wider BE buffer or "
                        f"phase-aware BE activation."
                    ),
                    "evidence": (
                        f"pnl={pnl_pips:+.1f}p, MFE={mfe:.1f}p, MAE={mae:.1f}p"
                    ),
                    "tags": [pair, "breakeven_move", "guardian_tuning"],
                    "universal": False,
                })
                learnings.append(f"guardian_be_clip_{pair}")

            # Always write the full action timeline to vault for the guardian agent
            self.vault.record_agent_learning("guardian", {
                "type": "note",
                "summary": (
                    f"{pair}: {len(actions)} guardian actions, {sl_moves} SL moves, "
                    f"{tp_moves} TP moves → {outcome} {pnl_pips:+.1f}p"
                ),
                "context": (
                    f"Full action timeline: {action_summary}. "
                    f"SL moves: {sl_timeline[:5]}. "
                    f"Phases: {[p.get('phase','?') for p in phases[:10]]}. "
                    f"Outcome: {outcome} {pnl_pips:+.1f}p (MFE={mfe:.1f}p, MAE={mae:.1f}p)."
                ),
                "evidence": (
                    f"actions={len(actions)}, sl_moves={sl_moves}, tp_moves={tp_moves}"
                ),
                "tags": [pair, "guardian_timeline", outcome],
                "universal": False,
            })
            learnings.append(f"guardian_timeline_{pair}")

        # Phase transition analysis
        if phases and len(phases) >= 2:
            phase_sequence = [p.get("phase", "?") for p in phases]
            # Count how many times it oscillated between peak and trending
            oscillations = sum(
                1 for i in range(1, len(phase_sequence))
                if phase_sequence[i] != phase_sequence[i-1]
            )
            if oscillations >= 6 and outcome == "loss":
                self.vault.record_agent_learning("guardian", {
                    "type": "correction",
                    "summary": (
                        f"{pair}: {oscillations} phase oscillations before {pnl_pips:+.1f}p loss "
                        f"— choppy market, exit earlier"
                    ),
                    "context": (
                        f"Phase sequence: {' → '.join(phase_sequence[:12])}. "
                        f"Trade oscillated {oscillations} times between phases before losing. "
                        f"This suggests choppy/ranging conditions where guardian should "
                        f"exit sooner rather than trailing."
                    ),
                    "evidence": (
                        f"oscillations={oscillations}, phases={len(phases)}, "
                        f"pnl={pnl_pips:+.1f}p, MFE={mfe:.1f}p"
                    ),
                    "tags": [pair, "phase_oscillation", "choppy_market"],
                    "universal": True,
                    "metadata": {
                        "source_user_id": str(a["_source_user_id"]) if a.get("_source_user_id") is not None else None,
                        "trade_id": a.get("trade_id"),
                        "pair": pair,
                    },
                })
                learnings.append(f"guardian_oscillation_{pair}")

        return learnings

    # ------------------------------------------------------------------
    # Winning setup revenue → vault (keeps vault + DB synchronized)
    # ------------------------------------------------------------------

    def _extract_winning_setup_learnings(self, a: Dict) -> List[str]:
        """Write winning setup revenue data to vault so validator can reference it.

        When a trade wins, query setup_revenue for this pair's lifetime stats
        and write a 'discovery' learning to the validator vault. Also writes
        cross-pair winners to collective patterns so all agents benefit.

        This is the critical bridge that keeps the vault and database in sync
        for winning trade knowledge.
        """
        learnings = []
        outcome = a.get("outcome", "")

        # Only fire on wins — losses are handled by _extract_validator_learnings
        if outcome != "win":
            return learnings

        pair = a.get("pair", "")
        setup_name = a.get("setup_name", "")
        pnl_pips = a.get("pnl_pips", 0)
        direction = a.get("direction", "")

        if not pair or not setup_name:
            return learnings

        try:
            conn = get_trading_forex()
            conn.row_factory = sqlite3.Row

            # ── 1. This setup's lifetime stats on this pair ──
            row = conn.execute(
                "SELECT setup_name, pair, wins, losses, total_pips, total_usd, "
                "win_rate, best_trade_usd FROM setup_revenue "
                "WHERE setup_name = ? AND pair = ?",
                (setup_name, pair)
            ).fetchone()

            if row and dict(row).get("wins", 0) >= 1:
                r = dict(row)
                total_trades = r["wins"] + r["losses"]
                wr_pct = round(r["win_rate"] * 100)
                gross_usd = round(r["total_usd"], 2)
                total_pips_val = round(r["total_pips"], 1)

                # Write to validator vault — this is what the validator will
                # read via load_agent_context() on the next cycle
                self.vault.record_agent_learning("validator", {
                    "type": "discovery",
                    "summary": (
                        f"PROVEN WINNER: {pair} {setup_name} — "
                        f"{r['wins']}W/{r['losses']}L ({wr_pct}% WR), "
                        f"${gross_usd} lifetime, {total_pips_val}p total"
                    ),
                    "context": (
                        f"Setup '{setup_name}' on {pair} has now won {r['wins']} "
                        f"of {total_trades} trades ({wr_pct}% win rate). "
                        f"Lifetime gross: ${gross_usd}, {total_pips_val} pips total. "
                        f"Best single trade: ${r.get('best_trade_usd', 0):.2f}. "
                        f"Latest: +{pnl_pips:.1f} pips {direction}. "
                        f"PRIORITIZE confirmation of this setup on {pair} — "
                        f"it has proven profitable."
                    ),
                    "evidence": (
                        f"setup={setup_name}, pair={pair}, "
                        f"wins={r['wins']}, losses={r['losses']}, "
                        f"wr={wr_pct}%, gross=${gross_usd}, "
                        f"total_pips={total_pips_val}"
                    ),
                    "tags": [pair, setup_name, "proven_winner", "revenue_tracked",
                             direction],
                    "universal": False,
                })
                learnings.append(f"winning_setup_{pair}_{setup_name}")

                # Also write to scout vault so scout knows to look for this
                self.vault.record_agent_learning("scout", {
                    "type": "discovery",
                    "summary": (
                        f"PLAYBOOK: {pair} {setup_name} — "
                        f"{wr_pct}% WR, ${gross_usd} lifetime"
                    ),
                    "context": (
                        f"This setup has proven profitable on {pair}. "
                        f"{r['wins']} wins out of {total_trades} trades. "
                        f"Scout should ACTIVELY SEARCH for this pattern on {pair}. "
                        f"Direction bias: {direction}."
                    ),
                    "evidence": (
                        f"setup={setup_name}, pair={pair}, "
                        f"wr={wr_pct}%, gross=${gross_usd}"
                    ),
                    "tags": [pair, setup_name, "playbook", "scout_priority"],
                    "universal": False,
                })
                learnings.append(f"scout_playbook_{pair}_{setup_name}")

            # ── 2. Cross-pair winners: setups that win on 2+ pairs ──
            cross_rows = conn.execute(
                "SELECT setup_name, COUNT(DISTINCT pair) as pair_count, "
                "SUM(wins) as total_wins, SUM(losses) as total_losses, "
                "SUM(total_usd) as gross_usd, "
                "GROUP_CONCAT(DISTINCT pair) as pairs "
                "FROM setup_revenue WHERE wins >= 1 "
                "GROUP BY setup_name HAVING COUNT(DISTINCT pair) >= 2 "
                "AND SUM(total_usd) > 0 "
                "ORDER BY SUM(total_usd) DESC LIMIT 5"
            ).fetchall()

            for cr in cross_rows:
                c = dict(cr)
                total_t = c["total_wins"] + c["total_losses"]
                cross_wr = round(c["total_wins"] / max(total_t, 1) * 100)
                cross_usd = round(c["gross_usd"], 2)

                # Write to collective patterns so ALL agents see it
                self.vault.record_agent_learning("orchestrator", {
                    "type": "discovery",
                    "summary": (
                        f"CROSS-PAIR WINNER: {c['setup_name']} proven on "
                        f"{c['pair_count']} pairs ({c['pairs']}) — "
                        f"${cross_usd} gross, {cross_wr}% WR"
                    ),
                    "context": (
                        f"Setup '{c['setup_name']}' has won across {c['pair_count']} "
                        f"different pairs: {c['pairs']}. "
                        f"Total: {c['total_wins']}W/{c['total_losses']}L "
                        f"({cross_wr}% WR), ${cross_usd} gross revenue. "
                        f"This is a high-confidence universal pattern. "
                        f"All agents should recognize and prioritize it."
                    ),
                    "evidence": (
                        f"setup={c['setup_name']}, pairs={c['pairs']}, "
                        f"wins={c['total_wins']}, gross=${cross_usd}, wr={cross_wr}%"
                    ),
                    "tags": [c["setup_name"], "cross_pair_winner", "universal",
                             "revenue_graded"],
                    "universal": True,
                    "metadata": {
                        "source_user_id": str(a["_source_user_id"]) if a.get("_source_user_id") is not None else None,
                        "trade_id": a.get("trade_id"),
                        "pair": pair,
                    },
                })
                learnings.append(f"cross_pair_{c['setup_name']}")

        except Exception as e:
            logger.warning("Winning setup revenue extraction failed: %s", e)

        return learnings

    # ==================================================================
    # Phase 2: Rolling Audit → Vault Drift Alerts
    # ==================================================================

    def process_rolling_audit(self, rolling_result: Dict[str, Any]) -> List[str]:
        """Extract drift learnings from a rolling audit report.

        Called after trade_auditor.rolling_audit() stores its report.
        """
        t0 = time.time()
        if not self.vault:
            return []

        learnings = []
        flags = rolling_result.get("flags", [])
        recommendations = rolling_result.get("recommendations", [])

        # Parse if stored as JSON strings
        if isinstance(flags, str):
            try:
                flags = json.loads(flags)
            except (json.JSONDecodeError, TypeError):
                flags = []
        if isinstance(recommendations, str):
            try:
                recommendations = json.loads(recommendations)
            except (json.JSONDecodeError, TypeError):
                recommendations = []

        trades_analyzed = rolling_result.get("trades_analyzed", 0)
        sig_acc = rolling_result.get("overall_signal_accuracy", 0)
        entry_avg = rolling_result.get("entry_timing_avg", 0)
        exit_avg = rolling_result.get("exit_quality_avg", 0)
        g_acc = rolling_result.get("overall_guardian_accuracy", 0)

        for flag in flags:
            flag_str = flag if isinstance(flag, str) else json.dumps(flag)
            agent = self._flag_to_agent(flag_str)

            self.vault.record_agent_learning(agent, {
                "type": "correction",
                "summary": f"DRIFT DETECTED: {flag_str[:120]}",
                "context": (
                    f"Rolling audit of last {trades_analyzed} trades. "
                    f"Signal accuracy: {sig_acc:.0f}%. "
                    f"Entry timing avg: {entry_avg:.0f}/100. "
                    f"Exit quality avg: {exit_avg:.0f}/100. "
                    f"Guardian accuracy: {g_acc:.0f}%."
                ),
                "evidence": flag_str,
                "tags": ["drift_alert", "rolling_audit"],
                "universal": True,
            })
            learnings.append(f"drift_{agent}_{flag_str[:40]}")

        for rec in recommendations:
            if isinstance(rec, dict):
                agent = self._rec_to_agent(rec)
                desc = rec.get("reason", rec.get("description", str(rec)))
                target = rec.get("target", "")

                self.vault.suggest_prompt_improvement(
                    agent_name=agent,
                    suggestion=f"Audit recommends adjusting {target}: {desc[:200]}",
                    evidence=(
                        f"Rolling audit ({trades_analyzed} trades): "
                        f"sig_acc={sig_acc:.0f}%, entry_timing={entry_avg:.0f}, "
                        f"exit_quality={exit_avg:.0f}"
                    ),
                )
                learnings.append(f"improvement_{agent}_{target}")

        if learnings:
            self._push_learning_event({
                "type": "drift_alert",
                "severity": "warning",
                "flags_count": len(flags),
                "recommendations_count": len(recommendations),
                "trades_analyzed": trades_analyzed,
                "learnings": learnings[:5],
            })
            self._flush_events()

        # ── Flight: LEARNING_DRIFT ──
        self._record("learning_drift", data={
            "flags_count": len(flags),
            "recommendations_count": len(recommendations),
            "learnings_count": len(learnings),
            "trades_analyzed": trades_analyzed,
        }, duration_ms=(time.time() - t0) * 1000,
        note=f"Rolling drift: {len(flags)} flags, {len(learnings)} learnings")

        logger.info("Rolling audit → %d drift learnings written", len(learnings))
        return learnings

    # ==================================================================
    # Phase 3: Thesis Audit → Vault Structural Learnings
    # ==================================================================

    def process_thesis_audit(self, thesis_result: Dict[str, Any]) -> List[str]:
        """Extract structural learnings from weekly thesis audit.

        Called after trade_auditor.thesis_audit() stores its report.
        """
        t0 = time.time()
        if not self.vault:
            return []

        learnings = []
        report_data = thesis_result.get("report_data", {})
        if isinstance(report_data, str):
            try:
                report_data = json.loads(report_data)
            except (json.JSONDecodeError, TypeError):
                report_data = {}

        thesis_by_type = report_data.get("thesis_accuracy_by_type", {})
        signal_by_field = report_data.get("signal_accuracy_by_field", {})

        # Thesis type performance
        for thesis_type, accuracy in thesis_by_type.items():
            if not isinstance(accuracy, (int, float)):
                continue

            if accuracy >= 75:
                self.vault.record_agent_learning("scout", {
                    "type": "discovery",
                    "summary": (
                        f"{thesis_type}: {accuracy:.0f}% thesis accuracy — "
                        f"HIGH CONFIDENCE setup type"
                    ),
                    "context": (
                        f"Thesis audit confirms {thesis_type} setups are performing "
                        f"well in live trading. Continue prioritizing this thesis type."
                    ),
                    "evidence": f"thesis_accuracy={accuracy:.0f}%",
                    "tags": [thesis_type, "high_confidence", "proven"],
                    "universal": True,
                })
                learnings.append(f"thesis_proven_{thesis_type}")

            elif accuracy < 50:
                self.vault.record_agent_learning("validator", {
                    "type": "failure",
                    "summary": (
                        f"{thesis_type}: only {accuracy:.0f}% thesis accuracy — "
                        f"AVOID or raise threshold"
                    ),
                    "context": (
                        f"This thesis type is underperforming in live conditions. "
                        f"Consider REJECT verdict for {thesis_type} setups "
                        f"unless confluence is exceptionally strong (>80)."
                    ),
                    "evidence": f"thesis_accuracy={accuracy:.0f}%",
                    "tags": [thesis_type, "underperforming", "avoid"],
                    "universal": True,
                })
                learnings.append(f"thesis_failing_{thesis_type}")

                # Persistent issue → prompt improvement
                self.vault.suggest_prompt_improvement(
                    agent_name="validator",
                    suggestion=(
                        f"Add explicit gate: raise confluence threshold for "
                        f"{thesis_type} setups. Live accuracy is {accuracy:.0f}%."
                    ),
                    evidence=f"Thesis audit: {thesis_type} accuracy={accuracy:.0f}%",
                )

        # Signal field reliability
        for field, accuracy in signal_by_field.items():
            if not isinstance(accuracy, (int, float)):
                continue
            if accuracy < 50:
                self.vault.record_agent_learning("scout", {
                    "type": "correction",
                    "summary": (
                        f"Signal field '{field}' only {accuracy:.0f}% accurate — "
                        f"unreliable indicator"
                    ),
                    "context": (
                        f"The '{field}' signal consistently mismatches between "
                        f"scout claims and market reality. Consider reducing its "
                        f"weight in confluence scoring or improving its calculation."
                    ),
                    "evidence": f"field_accuracy={accuracy:.0f}%",
                    "tags": [field, "signal_reliability", "low_accuracy"],
                    "universal": True,
                })
                learnings.append(f"signal_unreliable_{field}")

        if learnings:
            self._push_learning_event({
                "type": "thesis_audit_learning",
                "severity": "info",
                "learnings_count": len(learnings),
                "learnings": learnings[:5],
            })
            self._flush_events()

        # ── Flight: LEARNING_THESIS ──
        self._record("learning_thesis", data={
            "learnings_count": len(learnings),
            "thesis_types_analyzed": len(thesis_by_type),
            "signal_fields_analyzed": len(signal_by_field),
        }, duration_ms=(time.time() - t0) * 1000,
        note=f"Thesis audit: {len(learnings)} structural learnings")

        logger.info("Thesis audit → %d structural learnings written", len(learnings))
        return learnings

    # ==================================================================
    # Phase 4: Risk Auto-Tuner → Vault Parameter Logging
    # ==================================================================

    def process_tuning_event(self, change_summary: Dict[str, Any]) -> List[str]:
        """Record risk parameter changes as collective learnings.

        Called after risk_auto_tuner.apply_recommendations() applies changes.
        """
        t0 = time.time()
        if not self.vault:
            return []

        learnings = []
        applied = change_summary.get("applied", [])
        if not applied:
            # Try flattening from recommendations structure
            applied = change_summary.get("changes", [])

        for change in applied:
            if not isinstance(change, dict):
                continue

            field = change.get("field", "unknown")
            old_val = change.get("current", change.get("old_value", "?"))
            new_val = change.get("proposed", change.get("new_value", "?"))
            reason = change.get("reason", "performance-driven adjustment")

            self.vault.record_agent_learning("orchestrator", {
                "type": "improvement",
                "summary": (
                    f"Risk parameter adjusted: {field} "
                    f"{old_val} → {new_val}"
                ),
                "context": (
                    f"Reason: {reason}. "
                    f"This change affects position sizing and risk management. "
                    f"All agents should be aware of the updated risk landscape."
                ),
                "evidence": json.dumps(change, default=str),
                "tags": ["risk_tuning", field, "auto_applied"],
                "universal": True,
            })
            learnings.append(f"tuning_{field}")

        if learnings:
            self._push_learning_event({
                "type": "risk_tuning",
                "severity": "info",
                "changes_count": len(learnings),
                "learnings": learnings,
            })
            self._flush_events()

        # ── Flight: LEARNING_TUNING ──
        self._record("learning_tuning", data={
            "changes_count": len(learnings),
            "fields_changed": [l.replace("tuning_", "") for l in learnings],
        }, duration_ms=(time.time() - t0) * 1000,
        note=f"Risk tuning: {len(learnings)} parameter changes recorded")

        logger.info("Risk tuning → %d parameter learnings written", len(learnings))
        return learnings

    # ==================================================================
    # Phase 5: Full-Session Retrospective
    # ==================================================================

    def _load_vault_context_for_retro(self, pair: str, setup_name: str) -> Dict[str, Any]:
        """Load existing vault knowledge to enrich the session retrospective.

        Pulls from three sources:
        1. KnowledgeStore — backtest patterns, win rates, best params for this pair
        2. Vault scout learnings — prior signal accuracy findings, thesis failures
        3. Vault validator learnings — prior entry timing issues, snipe history

        Returns:
            Dict with keys: backtest_patterns, backtest_performance,
            scout_context, validator_context, prior_entry_gaps
        """
        ctx: Dict[str, Any] = {
            "backtest_patterns": {},
            "backtest_performance": {},
            "setup_backtest_wr": None,
            "setup_backtest_pf": None,
            "scout_context": "",
            "validator_context": "",
            "prior_entry_gaps": [],
        }

        # ── 1. KnowledgeStore: backtest patterns & performance ──
        try:
            ks = self.knowledge
            if ks:
                knowledge = ks.get_knowledge(pair)
                if knowledge:
                    ctx["backtest_patterns"] = knowledge.get("patterns", {})
                    ctx["backtest_performance"] = knowledge.get("performance", {})

                    # Extract this specific setup's backtest stats
                    if setup_name:
                        for sname, stats in ctx["backtest_patterns"].items():
                            if (setup_name.lower() in sname.lower()
                                    or sname.lower() in setup_name.lower()):
                                ctx["setup_backtest_wr"] = stats.get("win_rate")
                                ctx["setup_backtest_pf"] = stats.get("profit_factor")
                                break
        except Exception as e:
            logger.debug("KnowledgeStore load for retro failed (non-fatal): %s", e)

        # ── 2. Vault: scout & validator agent learnings ──
        try:
            v = self.vault
            if v:
                # Scout learnings — signal accuracy, thesis failures for this pair
                scout_timeline = v.get_learning_timeline("scout")
                pair_scout = [
                    l for l in scout_timeline
                    if pair in l.get("summary", "") or pair in str(l.get("tags", []))
                ]
                if pair_scout:
                    recent = pair_scout[-5:]  # last 5 relevant
                    ctx["scout_context"] = "; ".join(
                        l.get("summary", "")[:120] for l in recent
                    )

                # Validator learnings — entry timing, snipe gaps for this pair
                val_timeline = v.get_learning_timeline("validator")
                pair_val = [
                    l for l in val_timeline
                    if pair in l.get("summary", "") or pair in str(l.get("tags", []))
                ]
                if pair_val:
                    recent = pair_val[-5:]
                    ctx["validator_context"] = "; ".join(
                        l.get("summary", "")[:120] for l in recent
                    )
                    # Extract prior entry gap numbers for trend analysis
                    for l in pair_val:
                        ev = l.get("evidence", "")
                        if "entry_gap=" in ev:
                            try:
                                gap_str = ev.split("entry_gap=")[1].split("p")[0]
                                ctx["prior_entry_gaps"].append(float(gap_str))
                            except (IndexError, ValueError):
                                pass
        except Exception as e:
            logger.debug("Vault context load for retro failed (non-fatal): %s", e)

        return ctx

    def full_session_retrospective(self, audit_result: Dict[str, Any]) -> Optional[Dict]:
        """Compare trade entry/exit against optimal points in the full session.

        Fetches the full trading session's candles (not just the trade window)
        and identifies where the optimal entry and exit were. Also reads from
        the knowledge vault to enrich analysis with backtest patterns, prior
        learnings, and historical entry gap trends.

        Returns:
            Retrospective dict with optimal vs actual analysis, or None on failure.
        """
        t0 = time.time()
        pair = audit_result.get("pair", "")
        cycle_id = audit_result.get("cycle_id", "")
        trade_id = audit_result.get("trade_id", "")
        direction = audit_result.get("direction", "")
        entry_time_str = audit_result.get("entry_time", "")
        close_time_str = audit_result.get("close_time", "")
        entry_price = audit_result.get("entry_price", 0)
        exit_price = audit_result.get("exit_price", 0)
        pnl_pips = audit_result.get("pnl_pips", 0)
        setup_name = audit_result.get("setup_name", "")

        if not all([pair, direction, entry_time_str]):
            return None

        pip_size = 0.01 if "JPY" in pair else 0.0001
        is_buy = direction.lower() in ("buy", "bullish", "long")

        try:
            # ── Load vault context BEFORE chart analysis ──
            vault_ctx = self._load_vault_context_for_retro(pair, setup_name)

            # Parse entry time
            entry_dt = self._parse_time(entry_time_str)
            close_dt = self._parse_time(close_time_str) if close_time_str else None
            if not entry_dt:
                return None

            # Determine session boundaries (3 hours before entry to close + 1 hour)
            session_start = entry_dt - timedelta(hours=3)
            session_end = (close_dt or entry_dt) + timedelta(hours=1)

            # Fetch full session M15 candles
            candles = self._fetch_session_candles(pair, "M15", session_start, session_end)
            if not candles or len(candles) < 5:
                logger.debug("Not enough session candles for retrospective")
                return None

            # Find optimal entry in the 3 hours before actual entry
            pre_entry = [c for c in candles
                         if self._parse_time(c.get("time", ""))
                         and self._parse_time(c["time"]) < entry_dt]
            if not pre_entry:
                pre_entry = candles[:1]

            if is_buy:
                optimal_entry = min(self._mid(c, "l") for c in pre_entry[-12:])
                opt_entry_candle = next(
                    (c for c in pre_entry[-12:] if self._mid(c, "l") == optimal_entry),
                    pre_entry[-1]
                )
            else:
                optimal_entry = max(self._mid(c, "h") for c in pre_entry[-12:])
                opt_entry_candle = next(
                    (c for c in pre_entry[-12:] if self._mid(c, "h") == optimal_entry),
                    pre_entry[-1]
                )

            optimal_entry_time = opt_entry_candle.get("time", "")
            entry_gap_pips = abs(entry_price - optimal_entry) / pip_size

            # Find optimal exit during trade window
            if close_dt:
                trade_candles = [c for c in candles
                                 if self._parse_time(c.get("time", ""))
                                 and entry_dt <= self._parse_time(c["time"]) <= close_dt]
            else:
                trade_candles = candles

            if trade_candles:
                if is_buy:
                    optimal_exit = max(self._mid(c, "h") for c in trade_candles)
                else:
                    optimal_exit = min(self._mid(c, "l") for c in trade_candles)
            else:
                optimal_exit = exit_price or entry_price

            optimal_pips = abs(optimal_exit - optimal_entry) / pip_size
            capture_rate = (pnl_pips / optimal_pips) if optimal_pips > 0 else 0
            capture_rate = max(0, min(1, capture_rate))

            # ── Build vault-enriched context string ──
            vault_note_parts = []
            bt_wr = vault_ctx.get("setup_backtest_wr")
            bt_pf = vault_ctx.get("setup_backtest_pf")
            if bt_wr is not None:
                vault_note_parts.append(
                    f"Backtest {setup_name}: {bt_wr:.0%} WR, PF {bt_pf:.2f}"
                    if bt_pf else f"Backtest {setup_name}: {bt_wr:.0%} WR"
                )
            prior_gaps = vault_ctx.get("prior_entry_gaps", [])
            if prior_gaps:
                avg_gap = sum(prior_gaps) / len(prior_gaps)
                gap_trend = "improving" if entry_gap_pips < avg_gap else "worsening"
                vault_note_parts.append(
                    f"Entry gap trend: {gap_trend} "
                    f"(this={entry_gap_pips:.1f}p vs avg={avg_gap:.1f}p over {len(prior_gaps)} trades)"
                )
            if vault_ctx.get("scout_context"):
                vault_note_parts.append(f"Scout history: {vault_ctx['scout_context'][:200]}")
            if vault_ctx.get("validator_context"):
                vault_note_parts.append(
                    f"Validator history: {vault_ctx['validator_context'][:200]}"
                )
            vault_enrichment = " | ".join(vault_note_parts) if vault_note_parts else ""

            retro = {
                "optimal_entry": optimal_entry,
                "optimal_entry_time": optimal_entry_time,
                "actual_entry": entry_price,
                "entry_gap_pips": round(entry_gap_pips, 1),
                "optimal_exit": optimal_exit,
                "actual_exit": exit_price,
                "optimal_pips": round(optimal_pips, 1),
                "actual_pips": round(pnl_pips, 1),
                "capture_rate": round(capture_rate, 2),
                "session_candles": len(candles),
                "vault_context": {
                    "setup_backtest_wr": bt_wr,
                    "setup_backtest_pf": bt_pf,
                    "prior_entry_gaps": prior_gaps,
                    "entry_gap_trend": (
                        "improving" if prior_gaps and entry_gap_pips < sum(prior_gaps) / len(prior_gaps)
                        else "worsening" if prior_gaps
                        else "no_history"
                    ),
                    "scout_learnings_loaded": len(vault_ctx.get("scout_context", "")),
                    "validator_learnings_loaded": len(vault_ctx.get("validator_context", "")),
                },
            }

            # Write learning if significant entry gap — now vault-enriched
            if entry_gap_pips > 5 and self.vault:
                context_str = (
                    f"Session optimal entry: {optimal_entry} at {optimal_entry_time}. "
                    f"Our entry: {entry_price} at {entry_time_str}. "
                    f"Gap: {entry_gap_pips:.1f} pips. "
                    f"If entered at optimal: {optimal_pips:.1f} pips available vs "
                    f"{pnl_pips:.1f} pips captured ({capture_rate:.0%})."
                )
                if vault_enrichment:
                    context_str += f" [Vault context: {vault_enrichment}]"

                self.vault.record_agent_learning("validator", {
                    "type": "improvement",
                    "summary": (
                        f"{pair}: entered {entry_gap_pips:.1f} pips from session optimal"
                    ),
                    "context": context_str,
                    "evidence": (
                        f"entry_gap={entry_gap_pips:.1f}p, optimal_pips={optimal_pips:.1f}p, "
                        f"capture_rate={capture_rate:.0%}"
                        + (f", bt_wr={bt_wr:.0%}" if bt_wr else "")
                        + (f", gap_trend={'improving' if prior_gaps and entry_gap_pips < sum(prior_gaps)/len(prior_gaps) else 'worsening'}" if prior_gaps else "")
                    ),
                    "tags": [pair, "entry_gap", "session_retrospective"],
                    "universal": False,
                })

            # Write learning if low capture rate — now vault-enriched
            if capture_rate < 0.5 and optimal_pips > 10 and self.vault:
                context_str = (
                    f"Full session had {optimal_pips:.1f} pip move available. "
                    f"We captured {pnl_pips:.1f} pips ({capture_rate:.0%}). "
                    f"Review trailing stop and exit management for this session type."
                )
                if vault_enrichment:
                    context_str += f" [Vault context: {vault_enrichment}]"

                self.vault.record_agent_learning("guardian", {
                    "type": "improvement",
                    "summary": (
                        f"{pair}: captured only {capture_rate:.0%} of session move "
                        f"({pnl_pips:.1f}p of {optimal_pips:.1f}p available)"
                    ),
                    "context": context_str,
                    "evidence": (
                        f"capture_rate={capture_rate:.0%}, optimal={optimal_pips:.1f}p, "
                        f"actual={pnl_pips:.1f}p"
                        + (f", bt_wr={bt_wr:.0%}" if bt_wr else "")
                    ),
                    "tags": [pair, "capture_rate", "exit_optimization"],
                    "universal": False,
                })

            # Write learning if backtest/live divergence detected
            if bt_wr is not None and pnl_pips < 0:
                live_outcome = "loss"
                self.vault.record_agent_learning("scout", {
                    "type": "observation",
                    "summary": (
                        f"{pair} {setup_name}: live {live_outcome} "
                        f"({pnl_pips:.1f}p) vs backtest {bt_wr:.0%} WR"
                    ),
                    "context": (
                        f"Setup {setup_name} has {bt_wr:.0%} backtest win rate "
                        f"but this live trade was a {live_outcome} ({pnl_pips:.1f} pips). "
                        f"Capture rate: {capture_rate:.0%}. "
                        f"Entry gap from optimal: {entry_gap_pips:.1f} pips. "
                        f"Monitor for live vs backtest divergence on this setup."
                    ),
                    "evidence": (
                        f"setup={setup_name}, bt_wr={bt_wr:.0%}, "
                        f"live_pnl={pnl_pips:.1f}p, capture={capture_rate:.0%}"
                    ),
                    "tags": [pair, setup_name, "backtest_divergence", "session_retrospective"],
                    "universal": False,
                })

            # ── Flight: LEARNING_RETRO ──
            self._record("learning_retro", pair=pair, cycle_id=cycle_id,
                          trade_id=trade_id, data={
                              "capture_rate": retro["capture_rate"],
                              "entry_gap_pips": retro["entry_gap_pips"],
                              "optimal_pips": retro["optimal_pips"],
                              "actual_pips": retro["actual_pips"],
                              "session_candles": retro["session_candles"],
                              "vault_enriched": bool(vault_enrichment),
                              "backtest_wr": bt_wr,
                              "prior_gap_count": len(prior_gaps),
                          }, duration_ms=(time.time() - t0) * 1000,
                          note=f"Session retro: {capture_rate:.0%} capture, "
                               f"{entry_gap_pips:.1f}p entry gap"
                               + (f" [vault-enriched]" if vault_enrichment else ""))

            return retro

        except Exception as e:
            logger.warning("Session retrospective failed for %s: %s", pair, e)
            return None

    # ==================================================================
    # Phase 6: Scout Drift Detection
    # ==================================================================

    def check_scout_drift(self, pair: str, setup_type: str,
                          outcome: str, pips_result: float):
        """Check for scout performance drift after recording an outcome.

        Called from scout_learning_system.record_trade_outcome().
        """
        if not self.vault:
            return

        try:
            from db_connection import get_db
            with get_db() as conn:
                # Get recent performance for this pair+setup
                rows = conn.execute("""
                    SELECT outcome, pips_result
                    FROM scout_findings
                    WHERE pair = ? AND setup_type = ? AND outcome IS NOT NULL
                    ORDER BY updated_at DESC LIMIT 10
                """, (pair, setup_type)).fetchall()

            if len(rows) < 5:
                return

            recent_5 = rows[:5]
            wins = sum(1 for r in recent_5 if r["outcome"] == "win")
            recent_wr = wins / len(recent_5)

            if recent_wr < 0.3:  # Below 30% win rate on last 5
                self.vault.record_agent_learning("scout", {
                    "type": "failure",
                    "summary": (
                        f"{pair} {setup_type}: recent win rate {recent_wr:.0%} "
                        f"over last 5 trades — consider pausing"
                    ),
                    "context": (
                        f"The last 5 {setup_type} trades on {pair} show "
                        f"only {wins} wins. This is significantly below expectations. "
                        f"Consider temporarily raising the confluence threshold for "
                        f"this pair+setup combination."
                    ),
                    "evidence": (
                        f"recent_5_wr={recent_wr:.0%}, wins={wins}/5, "
                        f"last_outcome={outcome}, last_pips={pips_result:+.1f}"
                    ),
                    "tags": [pair, setup_type, "scout_drift", "poor_streak"],
                    "universal": False,
                })

                self._push_learning_event({
                    "type": "scout_drift",
                    "severity": "warning",
                    "pair": pair,
                    "setup": setup_type,
                    "recent_wr": recent_wr,
                })
                self._flush_events()

        except Exception as e:
            logger.warning("Scout drift check failed: %s", e)

    # ==================================================================
    # Per-Pair Live Performance Update
    # ==================================================================

    def _update_live_knowledge(self, audit_result: Dict):
        """Update per-pair knowledge.json with live performance overlay.

        Idempotent: tracks processed trade_ids to prevent double-counting
        if the pipeline is interrupted and re-run.
        """
        if not self.knowledge:
            return

        try:
            pair = audit_result.get("pair", "")
            setup = audit_result.get("setup_name", "")
            outcome = audit_result.get("outcome", "")
            trade_id = audit_result.get("trade_id", "")
            pnl_pips = audit_result.get("pnl_pips", 0)
            entry_timing = audit_result.get("entry_timing_score", 50)
            exit_quality = audit_result.get("exit_quality_score", 50)

            if not pair or not setup:
                return

            knowledge = self.knowledge.get_knowledge(pair)

            # Initialize live_performance section if needed
            if "live_performance" not in knowledge:
                knowledge["live_performance"] = {}

            if setup not in knowledge["live_performance"]:
                knowledge["live_performance"][setup] = {
                    "trades": 0, "wins": 0, "losses": 0,
                    "total_pips": 0.0,
                    "avg_entry_timing": 0.0,
                    "avg_exit_quality": 0.0,
                    "processed_trade_ids": [],
                }

            perf = knowledge["live_performance"][setup]

            # Idempotency guard: skip if this trade was already counted
            processed = perf.get("processed_trade_ids", [])
            if trade_id and str(trade_id) in [str(x) for x in processed]:
                logger.info("Skipping duplicate knowledge update for %s trade %s (already processed)", pair, trade_id)
                return
            processed.append(str(trade_id))
            # Keep only last 200 IDs to prevent unbounded growth
            perf["processed_trade_ids"] = processed[-200:]

            perf["trades"] += 1
            if outcome == "win":
                perf["wins"] += 1
            elif outcome == "loss":
                perf["losses"] += 1
            perf["total_pips"] = round(perf["total_pips"] + pnl_pips, 1)
            perf["win_rate"] = round(perf["wins"] / max(perf["trades"], 1), 3)

            # Running averages
            n = perf["trades"]
            perf["avg_entry_timing"] = round(
                (perf["avg_entry_timing"] * (n - 1) + (entry_timing or 50)) / n, 1
            )
            perf["avg_exit_quality"] = round(
                (perf["avg_exit_quality"] * (n - 1) + (exit_quality or 50)) / n, 1
            )

            # Compare to backtest baseline
            backtest = knowledge.get("patterns", {}).get(setup, {})
            if backtest and perf["trades"] >= MIN_TRADES_FOR_DELTA:
                bt_wr = backtest.get("win_rate", 0)
                live_wr = perf["win_rate"]
                perf["backtest_delta"] = round(live_wr - bt_wr, 3)

                # Alert if significant underperformance
                if perf["backtest_delta"] < BACKTEST_DELTA_WARN:
                    _src_uid = audit_result.get("_source_user_id")
                    self.vault.record_agent_learning("scout", {
                        "type": "correction",
                        "summary": (
                            f"{pair} {setup}: live WR {live_wr:.0%} vs "
                            f"backtest {bt_wr:.0%} — "
                            f"{abs(perf['backtest_delta']):.0%} gap"
                        ),
                        "context": (
                            f"After {perf['trades']} live trades, this setup is "
                            f"significantly underperforming backtest expectations. "
                            f"Consider raising confluence threshold or adding "
                            f"regime filter for this pair."
                        ),
                        "evidence": (
                            f"live={live_wr:.0%}, backtest={bt_wr:.0%}, "
                            f"delta={perf['backtest_delta']:+.0%}, n={perf['trades']}"
                        ),
                        "tags": [pair, setup, "backtest_divergence", "drift"],
                        "universal": True,
                        "metadata": {
                            "source_user_id": str(_src_uid) if _src_uid is not None else None,
                            "trade_id": trade_id,
                            "pair": pair,
                        },
                    })

            perf["updated_at"] = datetime.now(timezone.utc).isoformat()

            # Save back — write to JSON (knowledge_store write path)
            self.knowledge._load_json(pair)  # ensure loaded
            self.knowledge._stores[pair] = knowledge
            self.knowledge._save(pair)

        except Exception as e:
            logger.warning("Live knowledge update failed for %s: %s",
                           audit_result.get("pair", "?"), e)

    # ==================================================================
    # Dashboard Learning Events
    # ==================================================================

    def _push_learning_event(self, event_data: Dict):
        """Queue an event for the dashboard learning feed."""
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event_data,
        }
        self._events.append(event)

    def _flush_events(self):
        """Write queued events to dashboard/learning_events.json."""
        if not self._events:
            return

        try:
            os.makedirs(_DASHBOARD_DIR, exist_ok=True)

            # Load existing events
            existing = {"events": [], "last_updated": "", "agent_health": {}}
            if os.path.exists(_LEARNING_EVENTS_PATH):
                try:
                    with open(_LEARNING_EVENTS_PATH, "r") as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, IOError):
                    pass

            # Append new events, keep last 200
            events = existing.get("events", [])
            events.extend(self._events)
            events = events[-200:]

            existing["events"] = events
            existing["last_updated"] = datetime.now(timezone.utc).isoformat()

            # Update agent health from latest events
            existing["agent_health"] = self._compute_agent_health(events)

            with open(_LEARNING_EVENTS_PATH, "w") as f:
                json.dump(existing, f, indent=2, default=str)

            # Push SSE event so the dashboard refreshes instantly
            self._push_sse("learning_update", {
                "new_events": len(self._events),
                "total_events": len(events),
                "timestamp": existing["last_updated"],
            })

            self._events.clear()

        except Exception as e:
            logger.warning("Failed to flush learning events: %s", e)

    def _push_sse(self, event_type: str, data: Dict):
        """Push an SSE event to the dashboard for real-time updates.

        Tries to import the send_sse_message function from serve_ui.
        Falls back silently — SSE push is a best-effort enhancement.
        """
        try:
            # The SSE push function is registered globally in trading_api_routes
            from trading_api_routes import _sse_push_fn
            if _sse_push_fn is not None:
                _sse_push_fn(event_type, data)
                return
        except (ImportError, AttributeError):
            pass

        try:
            # Alternative: import directly from serve_ui
            from serve_ui import send_sse_message
            send_sse_message(event_type, data)
        except (ImportError, AttributeError):
            pass  # SSE not available — polling will pick it up

    def _compute_agent_health(self, events: List[Dict]) -> Dict:
        """Compute agent health metrics from recent events."""
        health = {}
        for agent in ("scout", "validator", "guardian", "orchestrator"):
            agent_events = [e for e in events[-50:] if agent in str(e)]
            corrections = sum(1 for e in agent_events
                              if "correction" in str(e) or "failure" in str(e))
            discoveries = sum(1 for e in agent_events
                              if "discovery" in str(e) or "improvement" in str(e))
            total = corrections + discoveries

            health[agent] = {
                "recent_events": len(agent_events),
                "corrections_ratio": round(corrections / max(total, 1), 2),
                "last_event": agent_events[-1].get("timestamp", "") if agent_events else "",
            }

        return health

    # ==================================================================
    # Helpers
    # ==================================================================

    def _flag_to_agent(self, flag: str) -> str:
        """Determine which agent a drift flag targets."""
        flag_lower = flag.lower()
        if any(k in flag_lower for k in ("guardian", "false alarm", "miss")):
            return "guardian"
        if any(k in flag_lower for k in ("entry_timing", "timing")):
            return "validator"
        if any(k in flag_lower for k in ("thesis", "signal", "fan_state",
                                          "momentum", "e100")):
            return "scout"
        return "scout"  # default

    def _rec_to_agent(self, rec: Dict) -> str:
        """Determine which agent a recommendation targets."""
        target = rec.get("target", "").lower()
        rec_type = rec.get("type", "").lower()

        if "guardian" in target:
            return "guardian"
        if rec_type == "pause":
            return "validator"
        if any(k in target for k in ("momentum", "fan", "signal", "e100")):
            return "scout"
        return "scout"

    def _parse_time(self, time_str: str) -> Optional[datetime]:
        """Parse various time formats."""
        if not time_str:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S.%f+00:00", "%Y-%m-%dT%H:%M:%S+00:00",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(time_str, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    def _mid(self, candle: Dict, field: str) -> float:
        """Get mid price from candle."""
        mid = candle.get("mid", candle)
        if isinstance(mid, dict):
            return float(mid.get(field, 0))
        return float(candle.get(field, 0))

    def _fetch_session_candles(self, pair: str, granularity: str,
                                from_dt: datetime, to_dt: datetime) -> List[Dict]:
        """Fetch candles for session retrospective via OANDA."""
        try:
            from oanda_client import OandaClient
            client = OandaClient()
            candles = client.get_candles(
                pair, granularity,
                from_time=from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                to_time=to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                count=None
            )
            return candles if candles else []
        except Exception as e:
            logger.debug("Session candle fetch failed: %s", e)
            return []
