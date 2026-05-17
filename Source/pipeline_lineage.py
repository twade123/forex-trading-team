"""
Pipeline Lineage — End-to-end chain tracking for the trading pipeline.

Stitches together the full journey of every trade using snipe_id as the
primary thread:

    SOURCE → FINDING → SNIPE → TRIGGER → TRADE → OUTCOME

Sources:
    - Scout scan (automated)      → scout_findings.id
    - User chart submission       → watch_suggestions.id (source_type='user_chart')
    - Trading cycle (direct)      → watch_suggestions via cycle

Tables queried:
    - scout_findings    (v2/trading_forex.db)  — finding_id, snipe_id, trade_id
    - watch_suggestions (v2/trading_forex.db) — snipe_id, trade_cycle_id, context
    - setup_trades      (v2/trading_forex.db) — trade_id, watch_id, outcome
    - flight_log        (flight_recorder.db)  — stage timeline per trade

The snipe_id (watch_suggestions.id) is the golden thread that connects
the entire pipeline.

Usage:
    from pipeline_lineage import PipelineLineage

    lineage = PipelineLineage()
    chains = lineage.get_recent_chains(hours_back=24)
    report = lineage.generate_report(hours_back=24)
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("trading_bot.pipeline_lineage")

_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_SOURCE_DIR)
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

from db_pool import get_trading_forex
from db_connection import get_db


# ── Database paths ─────────────────────────────────────────────────────────

def _resolve_db(relative_parts: list) -> str:
    """Resolve DB path relative to Jarvis root (3 levels up from Source)."""
    # Source/ → Forex Trading Team/ → Jarvis/ (matches db_connection.py)
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.realpath(os.path.join(base, *relative_parts))


class _PooledNoClose:
    """Wrapper that makes .close() a no-op for pooled connections."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass

    def __bool__(self):
        return True


def _get_forex_conn() -> Optional[sqlite3.Connection]:
    """Get a pooled connection to trading_forex.db. Do NOT close."""
    try:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        return _PooledNoClose(conn)
    except Exception as e:
        logger.debug("Could not get trading_forex pool conn: %s", e)
        return None


def _get_flight_conn(flight_db: str) -> Optional[sqlite3.Connection]:
    """Get a short-lived connection to flight_recorder.db via get_db."""
    if not os.path.exists(flight_db):
        return None
    try:
        conn = sqlite3.connect(flight_db, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn
    except Exception as e:
        logger.debug("Could not open %s: %s", flight_db, e)
        return None


class PipelineLineage:
    """Traces the full pipeline chain for every snipe/trade."""

    def __init__(self):
        # flight_recorder.db has: flight_log (pipeline stage timeline)
        self._flight_db = os.path.join(_SOURCE_DIR, "flight_recorder.db")

    # ── Core: Build a single chain for a snipe_id ──────────────────────────

    def get_chain(self, snipe_id: int) -> Dict[str, Any]:
        """Build the full pipeline chain for a single snipe_id.

        Returns a dict with sections:
            source, finding, snipe, trigger, trade, outcome, flight_stages, health
        """
        chain = {
            "snipe_id": snipe_id,
            "source": None,        # How this snipe originated
            "finding": None,       # Scout finding (if scout-originated)
            "snipe": None,         # Watch/snipe details
            "trigger": None,       # When/how conditions were met
            "trade": None,         # Execution details
            "outcome": None,       # Win/loss/pips
            "flight_stages": [],   # Flight recorder timeline
            "health": {
                "chain_complete": False,
                "missing_links": [],
                "chain_score": 0,    # 0-100
            },
        }

        # 1. Get snipe (watch_suggestions)
        snipe_data = self._get_snipe(snipe_id)
        if not snipe_data:
            chain["health"]["missing_links"].append("snipe_not_found")
            return chain

        chain["snipe"] = {
            "id": snipe_data["id"],
            "instrument": snipe_data["instrument"],
            "suggestion_type": snipe_data.get("suggestion_type"),
            "conditions": _safe_json(snipe_data.get("conditions")),
            "validator_verdict": snipe_data.get("validator_verdict"),
            "validator_confidence": snipe_data.get("validator_confidence"),
            "created_at": snipe_data.get("created_at"),
            "expires_at": snipe_data.get("expires_at"),
            "status": snipe_data.get("status"),
            "agent_name": snipe_data.get("agent_name"),
        }

        # Parse context for source info and trade linking
        ctx = _safe_json(snipe_data.get("context"))

        # Determine source type — use origin_type column first, then context clues
        origin = snipe_data.get("origin_type") or snipe_data.get("source_type") or ""
        source_type = origin if origin else "scout"
        if not origin:
            if ctx.get("source") == "user_chart" or ctx.get("_chart_submission"):
                source_type = "user_chart"
            elif ctx.get("source") == "manual" or ctx.get("_from_floor_chat"):
                source_type = "user_manual"
            elif ctx.get("source") == "cycle":
                source_type = "trading_cycle"
        chain["source"] = {
            "type": source_type,
            "context": {k: v for k, v in ctx.items()
                        if not k.startswith("_snipe_fill") and k != "conditions_progress"},
        }

        # 2. Get scout finding linked to this snipe
        finding = self._get_finding_for_snipe(snipe_id)
        if finding:
            chain["finding"] = {
                "id": finding["id"],
                "pair": finding["pair"],
                "setup_type": finding.get("setup_type"),
                "setup_name": finding.get("setup_name"),
                "direction": finding.get("direction"),
                "scout_confidence": finding.get("scout_confidence"),
                "sniper_score": finding.get("sniper_score"),
                "alert_type": finding.get("alert_type"),
                "timestamp": finding.get("timestamp"),
                "reasoning": (finding.get("reasoning") or "")[:200],
            }
        elif source_type == "scout":
            chain["health"]["missing_links"].append("finding_not_linked")

        # 3. Trigger info
        triggered_at = snipe_data.get("triggered_at")
        if triggered_at:
            chain["trigger"] = {
                "triggered_at": triggered_at,
                "check_count": snipe_data.get("check_count", 0),
                "peak_progress": snipe_data.get("peak_progress"),
                "trade_cycle_id": snipe_data.get("trade_cycle_id"),
            }
        elif snipe_data.get("status") in ("triggered", "completed"):
            chain["health"]["missing_links"].append("trigger_time_missing")

        # 4. Trade info — get from context or setup_trades
        trade_id = ctx.get("_snipe_fill_trade_id") or snipe_data.get("trade_cycle_id")
        if trade_id:
            trade_data = self._get_trade(trade_id, snipe_id, snipe_data.get("instrument"))
            if trade_data:
                chain["trade"] = trade_data
                # 5. Outcome
                chain["outcome"] = {
                    "result": trade_data.get("outcome"),
                    "pnl_pips": trade_data.get("pnl_pips"),
                    "pnl_usd": trade_data.get("pnl_usd"),
                    "r_multiple": trade_data.get("r_multiple"),
                    "duration_minutes": trade_data.get("duration_minutes"),
                    "close_reason": trade_data.get("close_reason"),
                    "closed_at": trade_data.get("closed_at"),
                }
            else:
                chain["health"]["missing_links"].append("trade_not_found_in_setup_trades")
        elif snipe_data.get("status") == "completed":
            chain["health"]["missing_links"].append("trade_id_missing")

        # Also check watch_suggestions outcome columns
        if snipe_data.get("trade_outcome") and not chain["outcome"]:
            chain["outcome"] = {
                "result": snipe_data["trade_outcome"],
                "pnl_pips": snipe_data.get("pips_result"),
            }

        # 6. Flight recorder stages for this trade/snipe
        if trade_id:
            chain["flight_stages"] = self._get_flight_stages(trade_id, snipe_data.get("instrument"))

        # Calculate chain health score
        chain["health"] = self._score_chain(chain)

        return chain

    # ── Batch: Get all chains in a time window ─────────────────────────────

    def get_recent_chains(self, hours_back: int = 24,
                          include_watching: bool = False) -> List[Dict]:
        """Get all pipeline chains from the last N hours.

        By default only returns triggered/completed snipes. Set
        include_watching=True to also include active watching snipes.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()

        conn = _get_forex_conn()
        if not conn:
            return []

        try:
            status_filter = "('triggered', 'completed', 'watching')" if include_watching \
                else "('triggered', 'completed')"

            rows = conn.execute(f"""
                SELECT id FROM watch_suggestions
                WHERE (created_at >= ? OR triggered_at >= ?)
                AND status IN {status_filter}
                ORDER BY COALESCE(triggered_at, created_at) DESC
                LIMIT 100
            """, (cutoff, cutoff)).fetchall()

            snipe_ids = [r["id"] for r in rows]
        finally:
            conn.close()

        return [self.get_chain(sid) for sid in snipe_ids]

    # ── Report: Pipeline health summary ────────────────────────────────────

    def generate_report(self, hours_back: int = 24) -> Dict[str, Any]:
        """Generate a comprehensive pipeline lineage report.

        Returns stats on:
        - Total snipes, triggered, traded, won
        - Conversion rates at each stage
        - Chain completeness scores
        - Broken chains needing attention
        - Source breakdown (scout vs user_chart vs manual)
        - Per-pair pipeline performance
        """
        chains = self.get_recent_chains(hours_back, include_watching=True)

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "period_hours": hours_back,
            "total_snipes": len(chains),
            "pipeline_stages": {
                "with_finding": 0,
                "triggered": 0,
                "traded": 0,
                "outcome_recorded": 0,
                "won": 0,
                "lost": 0,
            },
            "conversion_rates": {},
            "source_breakdown": {},
            "pair_breakdown": {},
            "chain_health": {
                "avg_score": 0,
                "complete_chains": 0,
                "broken_chains": [],
            },
            "broken_links": {},     # Which links break most often
            "top_issues": [],
        }

        if not chains:
            report["top_issues"].append("No snipes found in period")
            return report

        total_score = 0
        link_counts = {}

        for chain in chains:
            # Source breakdown
            src = chain.get("source", {}).get("type", "unknown")
            report["source_breakdown"][src] = report["source_breakdown"].get(src, 0) + 1

            # Pair breakdown
            pair = (chain.get("snipe") or {}).get("instrument", "unknown")
            if pair not in report["pair_breakdown"]:
                report["pair_breakdown"][pair] = {
                    "snipes": 0, "triggered": 0, "traded": 0,
                    "won": 0, "lost": 0, "pips": 0,
                }
            pb = report["pair_breakdown"][pair]
            pb["snipes"] += 1

            if chain.get("finding"):
                report["pipeline_stages"]["with_finding"] += 1

            if chain.get("trigger"):
                report["pipeline_stages"]["triggered"] += 1
                pb["triggered"] += 1

            if chain.get("trade"):
                report["pipeline_stages"]["traded"] += 1
                pb["traded"] += 1

            if chain.get("outcome"):
                report["pipeline_stages"]["outcome_recorded"] += 1
                result = chain["outcome"].get("result", "")
                pips = chain["outcome"].get("pnl_pips") or 0
                if result == "win":
                    report["pipeline_stages"]["won"] += 1
                    pb["won"] += 1
                elif result == "loss":
                    report["pipeline_stages"]["lost"] += 1
                    pb["lost"] += 1
                pb["pips"] += pips

            # Chain health
            health = chain.get("health", {})
            score = health.get("chain_score", 0)
            total_score += score

            if health.get("chain_complete"):
                report["chain_health"]["complete_chains"] += 1
            elif health.get("missing_links"):
                report["chain_health"]["broken_chains"].append({
                    "snipe_id": chain["snipe_id"],
                    "pair": pair,
                    "missing": health["missing_links"],
                    "score": score,
                })

            # Count which links break
            for link in health.get("missing_links", []):
                link_counts[link] = link_counts.get(link, 0) + 1

        # Conversion rates
        total = report["total_snipes"]
        stages = report["pipeline_stages"]
        if total > 0:
            report["conversion_rates"] = {
                "finding_rate": round(stages["with_finding"] / total * 100, 1),
                "trigger_rate": round(stages["triggered"] / total * 100, 1),
                "trade_rate": round(stages["traded"] / total * 100, 1),
                "outcome_rate": round(stages["outcome_recorded"] / total * 100, 1),
            }
            if stages["traded"] > 0:
                report["conversion_rates"]["win_rate"] = round(
                    stages["won"] / stages["traded"] * 100, 1)

        report["chain_health"]["avg_score"] = round(total_score / total) if total else 0
        report["broken_links"] = dict(sorted(link_counts.items(),
                                              key=lambda x: -x[1]))

        # Top issues
        if stages["triggered"] > 0 and stages["traded"] == 0:
            report["top_issues"].append(
                "Snipes triggering but no trades executing — check execution agent")
        if stages["traded"] > 0 and stages["outcome_recorded"] == 0:
            report["top_issues"].append(
                "Trades executed but no outcomes recorded — check guardian close flow")
        if link_counts.get("finding_not_linked", 0) > total * 0.5:
            report["top_issues"].append(
                "Most snipes missing scout findings — check scout→snipe linking")
        if link_counts.get("trade_id_missing", 0) > stages.get("triggered", 1) * 0.3:
            report["top_issues"].append(
                "Triggered snipes missing trade_id — check trading_cycle write-back")

        # Round pair pips
        for pb in report["pair_breakdown"].values():
            pb["pips"] = round(pb["pips"], 1)

        # Limit broken chains to top 10
        report["chain_health"]["broken_chains"] = sorted(
            report["chain_health"]["broken_chains"],
            key=lambda x: x["score"]
        )[:10]

        return report

    # ── Write report to dashboard ──────────────────────────────────────────

    def write_dashboard_report(self, hours_back: int = 24) -> str:
        """Generate and write the lineage report to dashboard/lineage_report.json."""
        report = self.generate_report(hours_back)

        dashboard_dir = os.path.join(_PROJECT_DIR, "dashboard")
        os.makedirs(dashboard_dir, exist_ok=True)
        path = os.path.join(dashboard_dir, "lineage_report.json")

        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)

        logger.info("Pipeline lineage report written to %s (%d chains)",
                     path, report["total_snipes"])
        return path

    # ── Private: DB queries ────────────────────────────────────────────────

    def _get_snipe(self, snipe_id: int) -> Optional[Dict]:
        """Get watch_suggestions row by ID from trading_forex.db."""
        conn = _get_forex_conn()
        if not conn:
            return None
        try:
            row = conn.execute(
                "SELECT * FROM watch_suggestions WHERE id = ?",
                (snipe_id,)
            ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.debug("Error getting snipe %d: %s", snipe_id, e)
            return None
        finally:
            conn.close()

    def _get_finding_for_snipe(self, snipe_id: int) -> Optional[Dict]:
        """Get scout_findings row linked to this snipe."""
        conn = _get_forex_conn()
        if not conn:
            return None
        try:
            # scout_findings has snipe_id column
            row = conn.execute(
                "SELECT * FROM scout_findings WHERE snipe_id = ? ORDER BY id DESC LIMIT 1",
                (snipe_id,)
            ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.debug("Error getting finding for snipe %d: %s", snipe_id, e)
            return None
        finally:
            conn.close()

    def _get_trade(self, trade_id: str, snipe_id: int,
                   instrument: str = "") -> Optional[Dict]:
        """Get trade details from setup_trades (v2 DB)."""
        conn = _get_forex_conn()
        if not conn:
            # Fallback: try trevor DB
            conn = _get_forex_conn()
            if not conn:
                return None

        try:
            # Try by trade_id first
            row = conn.execute(
                "SELECT * FROM setup_trades WHERE trade_id = ? LIMIT 1",
                (str(trade_id),)
            ).fetchone()

            if not row:
                # Fallback: try by watch_id (snipe_id)
                row = conn.execute(
                    "SELECT * FROM setup_trades WHERE watch_id = ? LIMIT 1",
                    (snipe_id,)
                ).fetchone()

            return dict(row) if row else None
        except Exception as e:
            logger.debug("Error getting trade %s: %s", trade_id, e)
            return None
        finally:
            conn.close()

    def _get_flight_stages(self, trade_id: str,
                           instrument: str = "") -> List[Dict]:
        """Get flight recorder stages for this trade."""
        conn = _get_flight_conn(self._flight_db)
        if not conn:
            return []
        try:
            rows = conn.execute("""
                SELECT stage, timestamp, status, duration_ms, note, data
                FROM flight_log
                WHERE trade_id = ? OR (pair = ? AND trade_id = '')
                ORDER BY timestamp ASC
                LIMIT 50
            """, (str(trade_id), instrument)).fetchall()

            return [
                {
                    "stage": r["stage"],
                    "timestamp": r["timestamp"],
                    "status": r["status"],
                    "duration_ms": r["duration_ms"],
                    "note": (r["note"] or "")[:100],
                }
                for r in rows
            ]
        except Exception as e:
            logger.debug("Error getting flight stages for %s: %s", trade_id, e)
            return []
        finally:
            conn.close()

    def _score_chain(self, chain: Dict) -> Dict:
        """Score the completeness of a pipeline chain (0-100)."""
        score = 0
        missing = []

        # Snipe exists (20 points)
        if chain.get("snipe"):
            score += 20
        else:
            missing.append("snipe_not_found")
            return {"chain_complete": False, "missing_links": missing, "chain_score": 0}

        status = (chain.get("snipe") or {}).get("status", "")

        # Source identified (10 points)
        if chain.get("source") and chain["source"].get("type"):
            score += 10
        else:
            missing.append("source_unknown")

        # Finding linked — only expected for scout-sourced (15 points)
        src_type = (chain.get("source") or {}).get("type", "scout")
        if chain.get("finding"):
            score += 15
        elif src_type == "scout" and status in ("triggered", "completed"):
            missing.append("finding_not_linked")

        # Triggered (15 points)
        if chain.get("trigger"):
            score += 15
        elif status in ("triggered", "completed"):
            missing.append("trigger_time_missing")

        # Trade executed (20 points)
        if chain.get("trade"):
            score += 20
        elif status == "completed":
            missing.append("trade_not_found")

        # Outcome recorded (20 points)
        if chain.get("outcome") and chain["outcome"].get("result"):
            score += 20
        elif status == "completed":
            missing.append("outcome_missing")

        # Still watching — not a broken chain, just in progress
        if status == "watching":
            # Scale score to what we expect so far
            expected = 30  # source + snipe exists
            if chain.get("finding"):
                expected += 15
            score = min(score, expected)
            # Don't flag missing links for active watches
            missing = [m for m in missing if m not in
                       ("trigger_time_missing", "trade_not_found", "outcome_missing")]

        complete = (score >= 80 and not missing)

        return {
            "chain_complete": complete,
            "missing_links": missing,
            "chain_score": min(score, 100),
        }


# ── Helpers ────────────────────────────────────────────────────────────────

def _safe_json(val) -> dict:
    """Safely parse a JSON string or return empty dict."""
    if isinstance(val, dict):
        return val
    if not val:
        return {}
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return {}
