"""
Cycle Health Check — Post-cycle pipeline sanity checker.

Runs after every cycle (called by reporter step). Pure Python, zero LLM cost.
Scans the cycle result for data quality issues, pipeline breaks, and
performance drift. Writes findings to flight_recorder.db workflow_findings table
and returns them for dashboard notification.

Findings are severity-tagged:
  INFO     — observational, no action needed
  WARNING  — something looks off, worth watching
  CRITICAL — actively breaking trades, needs attention now
"""

import logging
import sqlite3
import time
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

# ── Severity levels ──
INFO = "INFO"
WARNING = "WARNING"
CRITICAL = "CRITICAL"

# ── Thresholds ──
PF_SENTINEL = 100.0          # Anything above this is probably a sentinel (real PF rarely >20)
MAX_CONSECUTIVE_HOLDS = 8    # Same pair HOLD this many times → something's wrong
MIN_INTELLIGENCE_FIELDS = 3  # Intelligence should have at least this many populated fields
CYCLE_TIME_WARNING_S = 300   # 5 min cycle → warning
CYCLE_TIME_CRITICAL_S = 600  # 10 min cycle → critical
DB_POINTS_ZERO_WITH_SETUPS = True  # Flag when classified_setups exist but db_points=0


def _normalize_direction(d: str) -> str:
    """PROBLEM 4 FIX: Normalize direction strings to handle BEAR/BEARISH and BULL/BULLISH variants."""
    if not d:
        return ""
    d = str(d).lower().strip()
    if d.startswith('bear'):
        return 'bearish'
    if d.startswith('bull'):
        return 'bullish'
    return d


def run_health_check(cycle_result: Dict[str, Any], instrument: str) -> List[Dict[str, Any]]:
    """Run all health checks against a completed cycle result.
    
    Args:
        cycle_result: The full cycle_result dict from trading_cycle.py
        instrument: The currency pair (e.g. 'EUR_USD')
    
    Returns:
        List of finding dicts: [{"severity": ..., "category": ..., "message": ..., "details": ...}]
    """
    findings = []
    
    try:
        findings.extend(_check_data_integrity(cycle_result, instrument))
    except Exception as e:
        logger.warning("Health check data_integrity failed: %s", e)
    
    try:
        findings.extend(_check_pipeline_gaps(cycle_result, instrument))
    except Exception as e:
        logger.warning("Health check pipeline_gaps failed: %s", e)
    
    try:
        findings.extend(_check_timing(cycle_result, instrument))
    except Exception as e:
        logger.warning("Health check timing failed: %s", e)
    
    try:
        findings.extend(_check_decision_quality(cycle_result, instrument))
    except Exception as e:
        logger.warning("Health check decision_quality failed: %s", e)
    
    try:
        findings.extend(_check_consecutive_holds(instrument))
    except Exception as e:
        logger.warning("Health check consecutive_holds failed: %s", e)
    
    # Persist findings
    if findings:
        _persist_findings(findings, instrument, cycle_result.get("cycle_id"))
    
    return findings


# ═══════════════════════════════════════════════════════════════
# Check 1: Data Integrity
# ═══════════════════════════════════════════════════════════════

def _check_data_integrity(cr: Dict, instrument: str) -> List[Dict]:
    """Check for garbage data values that shouldn't reach the dashboard."""
    findings = []
    
    # --- Profit factor sentinel values ---
    validation = cr.get("validation", {}) or {}
    db_evidence = validation.get("db_evidence", {}) or {}
    
    pf = db_evidence.get("best_profit_factor", 0) or 0
    if pf >= PF_SENTINEL:
        findings.append({
            "severity": CRITICAL,
            "category": "data_integrity",
            "message": f"Profit factor sentinel value ({pf}) reached dashboard for {instrument}. "
                       f"This means a setup with zero losses wasn't filtered — user sees fake 'infinite edge'.",
            "details": {"field": "best_profit_factor", "value": pf, "setup": db_evidence.get("best_setup")},
        })
    
    # --- Win rate sanity ---
    wr = db_evidence.get("overall_win_rate", 0) or 0
    tc = db_evidence.get("total_trades", 0) or 0
    wc = db_evidence.get("total_wins", 0) or 0
    if tc > 0 and wc > tc:
        findings.append({
            "severity": CRITICAL,
            "category": "data_integrity",
            "message": f"Win count ({wc}) exceeds trade count ({tc}) for {instrument}. Data corruption.",
            "details": {"total_trades": tc, "total_wins": wc},
        })
    if tc > 100 and wr == 100.0:
        findings.append({
            "severity": WARNING,
            "category": "data_integrity",
            "message": f"100% win rate across {tc} trades for {instrument} — likely aggregating sentinel rows.",
            "details": {"win_rate": wr, "total_trades": tc},
        })
    
    # --- Confluence score sanity ---
    fc = cr.get("full_confluence", {}) or {}
    total = fc.get("total_score", 0) or 0
    if total > 120:
        findings.append({
            "severity": WARNING,
            "category": "data_integrity",
            "message": f"Confluence score {total} exceeds max 120 for {instrument}. Scoring bug.",
            "details": fc,
        })
    
    return findings


# ═══════════════════════════════════════════════════════════════
# Check 2: Pipeline Gaps
# ═══════════════════════════════════════════════════════════════

def _check_pipeline_gaps(cr: Dict, instrument: str) -> List[Dict]:
    """Check for missing or empty data that should have been populated."""
    findings = []
    
    # --- Intelligence data missing ---
    intel = cr.get("intelligence_data", {}) or {}
    if not intel or intel.get("verdict") == "PENDING":
        findings.append({
            "severity": WARNING,
            "category": "pipeline_gap",
            "message": f"Intelligence data missing or PENDING for {instrument}. "
                       f"Daily briefing card will be empty. Check Wolfram cache and intelligence agent.",
            "details": {"verdict": intel.get("verdict"), "keys": list(intel.keys()) if intel else []},
        })
    else:
        # Check for empty macro data specifically
        macro = intel.get("macro", {}) or {}
        if not macro.get("base_currency_rate") and not macro.get("pair_current_price"):
            findings.append({
                "severity": INFO,
                "category": "pipeline_gap",
                "message": f"Wolfram macro data empty for {instrument}. Cache may need refresh.",
                "details": {"macro_keys": list(macro.keys())},
            })
    
    # --- DB points zero when classified setups exist ---
    fc = cr.get("full_confluence", {}) or {}
    db_pts = fc.get("db", fc.get("db_points", 0)) or 0
    scout_ctx = cr.get("scout_context", {}) or {}
    classified = scout_ctx.get("classified_setups", {})
    if db_pts == 0 and classified:
        findings.append({
            "severity": INFO,
            "category": "pipeline_gap",
            "message": f"db_points=0 for {instrument} despite classified_setups: {classified}. "
                       f"Setup may not be backtested in current regime — validator treats as neutral.",
            "details": {"db_points": db_pts, "classified_setups": classified},
        })
    
    # --- Validation missing ---
    validation = cr.get("validation", {}) or {}
    if not validation or not validation.get("verdict"):
        findings.append({
            "severity": WARNING,
            "category": "pipeline_gap",
            "message": f"Validation result missing or has no verdict for {instrument}.",
            "details": {"validation_keys": list(validation.keys()) if validation else []},
        })
    
    # --- Confluence not in cycle result ---
    if not cr.get("full_confluence"):
        findings.append({
            "severity": WARNING,
            "category": "pipeline_gap",
            "message": f"full_confluence missing from cycle result for {instrument}. "
                       f"Dashboard confluence display will show '--'.",
            "details": {},
        })
    
    # --- Scout context missing ---
    if not scout_ctx:
        findings.append({
            "severity": INFO,
            "category": "pipeline_gap",
            "message": f"No scout context for {instrument} — manual cycle trigger?",
            "details": {},
        })
    
    return findings


# ═══════════════════════════════════════════════════════════════
# Check 3: Timing
# ═══════════════════════════════════════════════════════════════

def _check_timing(cr: Dict, instrument: str) -> List[Dict]:
    """Check for slow steps and overall cycle time."""
    findings = []
    
    timing = cr.get("timing", {}) or {}
    phase_timings = timing.get("phases", timing) if isinstance(timing, dict) else {}
    
    total_time = phase_timings.get("total", 0) or 0
    if not total_time:
        # Try to compute from start/end
        start = cr.get("cycle_start") or cr.get("start_time")
        end = cr.get("end_time")
        if start and end:
            try:
                from dateutil.parser import parse as parse_dt
                total_time = (parse_dt(end) - parse_dt(start)).total_seconds()
            except Exception:
                pass
    
    if total_time > CYCLE_TIME_CRITICAL_S:
        findings.append({
            "severity": CRITICAL,
            "category": "timing",
            "message": f"Cycle took {total_time:.0f}s ({total_time/60:.1f}min) for {instrument}. "
                       f"This blocks the queue — other pairs wait.",
            "details": {"total_seconds": total_time, "phases": phase_timings},
        })
    elif total_time > CYCLE_TIME_WARNING_S:
        findings.append({
            "severity": WARNING,
            "category": "timing",
            "message": f"Cycle took {total_time:.0f}s for {instrument}. "
                       f"Consider which agent is slowest.",
            "details": {"total_seconds": total_time, "phases": phase_timings},
        })
    
    # Check individual slow phases
    for phase, elapsed in phase_timings.items():
        if phase == "total":
            continue
        if isinstance(elapsed, (int, float)) and elapsed > 120:
            findings.append({
                "severity": WARNING,
                "category": "timing",
                "message": f"{phase} agent took {elapsed:.0f}s on {instrument} — over 2 minutes for a single step.",
                "details": {"phase": phase, "elapsed": elapsed},
            })
    
    return findings


# ═══════════════════════════════════════════════════════════════
# Check 4: Decision Quality
# ═══════════════════════════════════════════════════════════════

def _check_decision_quality(cr: Dict, instrument: str) -> List[Dict]:
    """Check for suspicious decision patterns."""
    findings = []
    
    decision = cr.get("decision", {}) or {}
    validation = cr.get("validation", {}) or {}
    fc = cr.get("full_confluence", {}) or {}
    
    action = decision.get("action", "hold") if isinstance(decision, dict) else "hold"
    tradeable = fc.get("tradeable", False)
    total_score = fc.get("total_score", 0) or 0
    
    # --- Confluence says tradeable but orchestrator held ---
    if tradeable and action == "hold":
        findings.append({
            "severity": INFO,
            "category": "decision_quality",
            "message": f"Confluence {total_score}/100 was tradeable for {instrument} but orchestrator held. "
                       f"Reasons: {decision.get('hold_reasons', decision.get('reasons', []))}",
            "details": {
                "confluence": total_score,
                "tradeable": True,
                "hold_reasons": decision.get("hold_reasons", []),
                "validator_verdict": validation.get("verdict"),
                "validator_confidence": validation.get("confidence"),
            },
        })
    
    # --- Validator high confidence but hold ---
    val_conf = validation.get("confidence", 0) or 0
    # Normalize: validator returns integer scale (1-5) OR decimal (0.0-1.0).
    # Integer scale: 3 was being read as 300% — only flag if it's a decimal > 0.7
    # or an integer >= 4 (out of 5 = 80%+ confidence).
    _conf_normalized = val_conf / 5.0 if isinstance(val_conf, (int, float)) and val_conf > 1 else val_conf
    if isinstance(_conf_normalized, (int, float)) and _conf_normalized > 0.7 and action == "hold":
        _conf_display = f"{int(val_conf)}/5" if val_conf > 1 else f"{val_conf:.0%}"
        # Only flag as warning if verdict was CONFIRM (not WATCH — WATCH→HOLD is correct)
        _val_verdict = validation.get("verdict", "")
        if _val_verdict not in ("WATCH", "watch"):
            findings.append({
                "severity": WARNING,
                "category": "decision_quality",
                "message": f"Validator had {_conf_display} confidence for {instrument} but trade was held. "
                           f"Check if confluence scoring or orchestrator logic is too conservative.",
                "details": {"confidence": val_conf, "verdict": _val_verdict},
            })
    
    # --- Direction contradiction between TA and scout ---
    scout_dir_raw = (cr.get("scout_context", {}) or {}).get("direction", "")
    scout_dir = _normalize_direction(scout_dir_raw)
    analysis = cr.get("analysis", {}) or {}
    ta_interp = analysis.get("ta_interpretation", {}) or {}
    ta_dir_raw = ""
    if isinstance(ta_interp, dict):
        ta_dir_raw = ta_interp.get("direction", "")
    elif isinstance(ta_interp, str):
        ta_dir_raw = ""
    ta_dir = _normalize_direction(ta_dir_raw)
    
    if scout_dir and ta_dir and scout_dir != ta_dir and scout_dir != "neutral" and ta_dir != "neutral":
        findings.append({
            "severity": INFO,
            "category": "decision_quality",
            "message": f"Scout said {scout_dir_raw.upper()} (normalized: {scout_dir.upper()}) but TA said {ta_dir_raw.upper()} (normalized: {ta_dir.upper()}) for {instrument}. "
                       f"Direction disagreement between scout thesis and technical analysis.",
            "details": {"scout_direction": scout_dir, "ta_direction": ta_dir, "scout_raw": scout_dir_raw, "ta_raw": ta_dir_raw},
        })
    
    return findings


# ═══════════════════════════════════════════════════════════════
# Check 5: Consecutive Holds
# ═══════════════════════════════════════════════════════════════

def _check_consecutive_holds(instrument: str) -> List[Dict]:
    """Check flight recorder for consecutive HOLD decisions on this pair."""
    findings = []
    
    try:
        fr_path = os.path.join(os.path.dirname(__file__), "flight_recorder.db")
        if not os.path.exists(fr_path):
            return findings
        
        conn = sqlite3.connect(fr_path)
        conn.row_factory = sqlite3.Row
        
        # Get last N cycle_end entries for this pair
        rows = conn.execute("""
            SELECT data FROM flight_log 
            WHERE pair=? AND stage='cycle_end'
            ORDER BY id DESC LIMIT 20
        """, (instrument,)).fetchall()
        conn.close()
        
        if not rows:
            return findings
        
        consecutive_holds = 0
        for r in rows:
            try:
                d = json.loads(r["data"]) if r["data"] else {}
                if d.get("action", "hold") == "hold":
                    consecutive_holds += 1
                else:
                    break
            except Exception:
                break
        
        if consecutive_holds >= MAX_CONSECUTIVE_HOLDS:
            findings.append({
                "severity": WARNING,
                "category": "pattern",
                "message": f"{instrument} has held {consecutive_holds} consecutive cycles. "
                           f"Either the market genuinely has no setup, or something is blocking trades "
                           f"(check db_points gate, confluence threshold, validator rejection patterns).",
                "details": {"consecutive_holds": consecutive_holds},
            })
    except Exception as e:
        logger.warning("Consecutive holds check failed: %s", e)
    
    return findings


# ═══════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════

def _persist_findings(findings: List[Dict], instrument: str, cycle_id: str = None):
    """Write findings to flight_recorder.db workflow_findings table."""
    try:
        fr_path = os.path.join(os.path.dirname(__file__), "flight_recorder.db")
        conn = sqlite3.connect(fr_path)
        try:  # conn.close() in finally below
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    cycle_id TEXT,
                    pair TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT,
                    acknowledged INTEGER DEFAULT 0
                )
            """)
            
            now = datetime.now(timezone.utc).isoformat()
            for f in findings:
                conn.execute("""
                    INSERT INTO workflow_findings (timestamp, cycle_id, pair, severity, category, message, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (now, cycle_id, instrument, f["severity"], f["category"], f["message"],
                      json.dumps(f.get("details", {}))))
            
            conn.commit()
            
            # Auto-purge: keep last 500 findings
            conn.execute("DELETE FROM workflow_findings WHERE id NOT IN (SELECT id FROM workflow_findings ORDER BY id DESC LIMIT 500)")
            conn.commit()
        finally:
            conn.close()
        
    except Exception as e:
        logger.warning("Failed to persist health check findings: %s", e)


def get_recent_findings(limit: int = 20, severity: str = None, acknowledged: bool = False) -> List[Dict]:
    """Retrieve recent findings for dashboard display."""
    try:
        fr_path = os.path.join(os.path.dirname(__file__), "flight_recorder.db")
        if not os.path.exists(fr_path):
            return []
        
        conn = sqlite3.connect(fr_path)
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM workflow_findings WHERE 1=1"
            params = []
            if severity:
                query += " AND severity=?"
                params.append(severity)
            if not acknowledged:
                query += " AND acknowledged=0"
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        
        return [dict(r) for r in rows]
    except Exception:
        return []


def acknowledge_finding(finding_id: int):
    """Mark a finding as acknowledged (won't show in dashboard)."""
    try:
        fr_path = os.path.join(os.path.dirname(__file__), "flight_recorder.db")
        conn = sqlite3.connect(fr_path)
        try:
            conn.execute("UPDATE workflow_findings SET acknowledged=1 WHERE id=?", (finding_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Failed to acknowledge finding %d: %s", finding_id, e)
