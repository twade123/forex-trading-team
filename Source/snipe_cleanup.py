#!/usr/bin/env python3
"""
Snipe Cleanup — CRO-powered snipe relevance check.

For each active snipe, pulls current market state from flight_recorder and
asks CRO (local 9B TA model, port 11500) whether the setup is still valid.
Logs every decision to flight_recorder for distillation training.

Called by:
  - /api/trading/snipe-clean  (dashboard button)
  - Daily auto-cleanup cron   (future)

Returns list of decisions: [{snipe_id, instrument, decision, reason, model}]
"""

import json
import logging
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from db_connection import get_db

logger = logging.getLogger(__name__)

_CRO_URL  = "http://127.0.0.1:11503/v1/chat/completions"  # serving gateway → MLX 35B
_CRO_MODEL = "mlx-community/Qwen3.5-35B-A3B-4bit"

_SOURCE_DIR = Path(__file__).parent
_FLIGHT_DB  = _SOURCE_DIR / "flight_recorder.db"

def _get_trading_forex_conn():
    """Get trading_forex DB connection for watch_suggestions queries."""
    try:
        from db_pool import get_trading_forex
        return get_trading_forex(), True   # (conn, pooled)
    except Exception:
        db = str(Path(__file__).parent.parent.parent.resolve()
                 / "Database" / "v2" / "trading_forex.db")
        return sqlite3.connect(db, timeout=10, isolation_level=None), False



# ── Helpers ───────────────────────────────────────────────────────────────────

def _cro_call(prompt: str, max_tokens: int = 350) -> str:
    """Call CRO (9B local model). Returns raw text or empty string on failure."""
    system = (
        "You are a trading setup analyst reviewing active snipes for a trader. "
        "Decide whether each snipe is still worth watching given current market conditions.\n\n"
        "Reply in this exact format — nothing else:\n"
        "DECISION: KEEP or REMOVE\n"
        "SUMMARY: One sentence — what the snipe was watching for.\n"
        "MARKET NOW: One sentence — what the market is doing right now.\n"
        "REASON: One to two sentences in plain English explaining to the trader why to keep or remove.\n\n"
        "REMOVE example:\n"
        "DECISION: REMOVE\n"
        "SUMMARY: Waiting for a SELL entry when the bearish fan opens and BBs expand.\n"
        "MARKET NOW: Fan has flipped bullish — price is now above E55 with bullish EMA ordering.\n"
        "REASON: The bearish setup this snipe was watching for has been invalidated. "
        "The market moved in the opposite direction and the original thesis no longer applies.\n\n"
        "KEEP example:\n"
        "DECISION: KEEP\n"
        "SUMMARY: Waiting for price to retest E100 resistance before a SELL entry.\n"
        "MARKET NOW: Bearish fan intact, price retracing upward toward E100 as expected.\n"
        "REASON: The setup is progressing exactly as expected and may trigger soon."
    )
    payload = json.dumps({
        "model": _CRO_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stop": ["</think>"],
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    try:
        req = urllib.request.Request(
            _CRO_URL, data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Jarvis-Tenant": "trading",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        return (result["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        logger.warning("CRO snipe-check call failed: %s", e)
        return ""


def _get_active_snipes() -> list:
    """Fetch all watching snipes from trading_forex.db."""
    conn, pooled = _get_trading_forex_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, instrument, suggestion_type, created_at,
               conditions_met_count, conditions_total_count, peak_progress,
               validator_confidence, context, conditions_progress
        FROM watch_suggestions
        WHERE status = 'watching'
        ORDER BY created_at ASC
    """).fetchall()
    if not pooled:
        conn.close()
    return [dict(r) for r in rows]


def _get_market_state(pair: str) -> dict:
    """Get latest scout scan data for a pair from flight_recorder.db."""
    try:
        with get_db(str(_FLIGHT_DB)) as conn:
            row = conn.execute("""
                SELECT data FROM flight_log
                WHERE stage='scout_scan' AND pair=?
                ORDER BY timestamp DESC LIMIT 1
            """, (pair,)).fetchone()
            if row:
                return json.loads(row["data"] or "{}")
    except Exception as e:
        logger.warning("Could not get market state for %s: %s", pair, e)
    return {}


def _log_decision(snipe_id: int, pair: str, decision: str, reason: str,
                  market_state: dict, snipe_age_hours: float):
    """Write CRO's decision to flight_recorder for distillation training."""
    try:
        _uid = int(os.environ.get('TRADING_USER_ID', 0)) or None
        with get_db(str(_FLIGHT_DB)) as conn:
            conn.execute("""
                INSERT INTO flight_log
                (timestamp, stage, pair, status, note, data, user_id)
                VALUES (?, 'snipe_review', ?, 'ok', ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                pair,
                f"{decision}: {reason}",
                json.dumps({
                    "snipe_id": snipe_id,
                    "decision": decision,
                    "reason": reason,
                    "model": "CRO-9B",
                    "market_snapshot": {
                        "fan_state":      market_state.get("fan_state"),
                        "fan_direction":  market_state.get("fan_direction"),
                        "story_score":    market_state.get("story_score"),
                        "bb_expanding":   market_state.get("bb_expanding"),
                        "entry_type":     market_state.get("entry_type"),
                    },
                    "snipe_age_hours": round(snipe_age_hours, 1),
                }),
                _uid,
            ))
    except Exception as e:
        logger.warning("Could not log snipe decision: %s", e)


def _cancel_snipe(snipe_id: int, reason: str):
    """Set snipe status to cancelled in trading_forex.db."""
    conn, pooled = _get_trading_forex_conn()
    conn.execute(
        "UPDATE watch_suggestions SET status='cancelled' WHERE id=?",
        (snipe_id,)
    )
    conn.commit()
    if not pooled:
        conn.close()


# ── Main entry point ──────────────────────────────────────────────────────────

def run_snipe_cleanup(auto_cancel: bool = False) -> list:
    """
    Run CRO-powered snipe relevance check on all active snipes.

    Args:
        auto_cancel: If True, immediately cancel snipes CRO marks REMOVE.
                     If False (default), returns decisions for UI confirmation.

    Returns:
        List of decision dicts:
        [{snipe_id, instrument, suggestion_type, decision, reason, model,
          peak_progress, age_hours, auto_cancelled}]
    """
    snipes = _get_active_snipes()
    if not snipes:
        return []

    now = datetime.now(timezone.utc)
    results = []

    for snipe in snipes:
        pair = snipe["instrument"]
        snipe_id = snipe["id"]

        # Age in hours
        try:
            created = datetime.fromisoformat(snipe["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_hours = (now - created).total_seconds() / 3600
        except Exception:
            age_hours = 0

        peak = snipe.get("peak_progress") or 0
        conditions_met  = snipe.get("conditions_met_count") or 0
        conditions_total = snipe.get("conditions_total_count") or 1
        market = _get_market_state(pair)

        # Build context for snipe direction
        ctx = {}
        try:
            ctx = json.loads(snipe.get("context") or "{}")
        except Exception:
            pass
        snipe_direction = ctx.get("direction", ctx.get("re_entry_direction", "unknown"))

        # Build conditions progress summary
        cond_summary = ""
        try:
            conds = json.loads(snipe.get("conditions_progress") or "[]")
            met_lines   = [c.get("condition", c.get("field", ""))[:60]
                           for c in conds if c.get("met")]
            unmet_lines = [c.get("condition", c.get("field", ""))[:60]
                           for c in conds if not c.get("met")]
            if met_lines:
                cond_summary += f"MET: {'; '.join(met_lines[:3])}. "
            if unmet_lines:
                cond_summary += f"UNMET: {'; '.join(unmet_lines[:3])}."
        except Exception:
            pass

        # ── Pull leaderboard history for this conditions fingerprint ──────────
        lb_summary = ""
        try:
            import sys as _sys
            _src = str(Path(__file__).parent)
            if _src not in _sys.path:
                _sys.path.insert(0, _src)
            from agents.watch_manager import _compute_conditions_hash
            import json as _jlb
            _conds = _jlb.loads(snipe.get("conditions") or "[]")
            _hash  = _compute_conditions_hash(_conds, pair, snipe_direction)
            conn_lb, _pooled_lb = _get_trading_forex_conn()
            lb_row = conn_lb.execute(
                "SELECT times_triggered, times_won, avg_pips, win_rate "
                "FROM snipe_leaderboard WHERE conditions_hash=? AND instrument=?",
                (_hash, pair)
            ).fetchone()
            if lb_row and lb_row[0]:
                lb_summary = (
                    f"\nHISTORICAL PERFORMANCE (same conditions pattern):\n"
                    f"  Triggered {lb_row[0]}x | Won {lb_row[1]}x | "
                    f"Win rate {lb_row[3]:.0f}% | Avg pips {lb_row[2]:+.1f}"
                )
        except Exception:
            pass

        prompt = f"""Snipe review for {pair}:
SNIPE: direction={snipe_direction}, type={snipe["suggestion_type"]}, age={age_hours:.1f}h
PROGRESS: {conditions_met}/{conditions_total} conditions met, peak ever reached={peak:.0%}
{cond_summary}{lb_summary}

CURRENT MARKET ({pair}):
  Fan state: {market.get("fan_state", "unknown")}
  Fan direction: {market.get("fan_direction", "unknown")}
  Story score: {market.get("story_score", "?")}
  BB expanding: {market.get("bb_expanding", "?")}
  Entry type: {market.get("entry_type", "none")}

IMPORTANT: Age alone is NEVER a reason to remove a snipe. A snipe that has been
waiting 3 days is fine if the thesis is still valid and conditions are progressing.
Only REMOVE if the market structure directly contradicts the snipe direction
(e.g. bearish fan on a BUY snipe, or conditions that can no longer be met).
KEEP if there is any reasonable path for conditions to be met.

Is this snipe still market-relevant? KEEP or REMOVE?"""

        raw = _cro_call(prompt)

        # Parse CRO structured response
        decision   = "KEEP"
        summary    = ""
        market_now = ""
        reason     = ""

        if not raw:
            reason = "CRO unavailable — kept by default"
        else:
            for line in raw.splitlines():
                line = line.strip()
                if line.upper().startswith("DECISION:"):
                    val = line.split(":", 1)[1].strip().upper()
                    decision = "REMOVE" if "REMOVE" in val else "KEEP"
                elif line.upper().startswith("SUMMARY:"):
                    summary = line.split(":", 1)[1].strip()
                elif line.upper().startswith("MARKET NOW:"):
                    market_now = line.split(":", 1)[1].strip()
                elif line.upper().startswith("REASON:"):
                    reason = line.split(":", 1)[1].strip()
            # Fallback if CRO didn't follow format
            if not reason:
                if "REMOVE" in raw.upper():
                    decision = "REMOVE"
                reason = raw[:200]

        # Log to flight_recorder (training data)
        _log_decision(snipe_id, pair, decision,
                      f"{summary} | {market_now} | {reason}", market, age_hours)

        cancelled = False
        if auto_cancel and decision == "REMOVE":
            _cancel_snipe(snipe_id, reason)
            cancelled = True
            logger.info("Auto-cancelled snipe #%d %s: %s", snipe_id, pair, reason)

        results.append({
            "snipe_id":        snipe_id,
            "instrument":      pair,
            "suggestion_type": snipe["suggestion_type"],
            "decision":        decision,
            "summary":         summary,
            "market_now":      market_now,
            "reason":          reason,
            "model":           "CRO-9B" if raw else "default-keep",
            "peak_progress":   peak,
            "age_hours":       round(age_hours, 1),
            "auto_cancelled":  cancelled,
        })

        logger.info("Snipe #%d %s → %s (%s)", snipe_id, pair, decision, reason[:60])

    removes = sum(1 for r in results if r["decision"] == "REMOVE")
    keeps   = sum(1 for r in results if r["decision"] == "KEEP")
    logger.info("Snipe cleanup complete: %d keep, %d remove, %d total", keeps, removes, len(results))
    return results
