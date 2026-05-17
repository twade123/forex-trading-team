"""
Trade outcome reconciliation and accuracy tracking for the validator.

After a trade closes, ``reconcile_trade()`` links the outcome back to the
``validator_decisions`` row and fills in accuracy flags.

``get_accuracy_report()`` and ``export_for_distillation()`` aggregate these
results for the learning / distillation pipeline.

Usage::

    from Source.validator_reconciliation import reconcile_trade, get_accuracy_report

    # Called from outcome_reconciler.py when a trade closes
    reconcile_trade("trade_abc123")

    # Called from daily review or dashboard
    report = get_accuracy_report(days_back=30)
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from db_pool import get_trading_forex, get_intelligence


def reconcile_trade(trade_id: str) -> bool:
    """
    Link a closed trade's outcome back to its validator_decisions row.

    Called from ``outcome_reconciler.py`` (or equivalent) after a trade is
    confirmed closed in ``live_trades``.

    Args:
        trade_id: The trade identifier (matches ``live_trades.trade_id``).

    Returns:
        True if the decision row was found and updated, False otherwise.
    """
    trading_conn = get_trading_forex()
    trading_conn.row_factory = sqlite3.Row
    intel_conn = get_intelligence()
    intel_conn.row_factory = sqlite3.Row

    # Get the closed trade from live_trades (trading_forex.db)
    trade = trading_conn.execute(
        """
        SELECT instrument, direction, pips_result, status
        FROM live_trades
        WHERE trade_id = ?
        """,
        (trade_id,),
    ).fetchone()

    if not trade or trade["status"] != "closed":
        return False

    # Get the matching validator_decisions row (intelligence.db)
    decision = intel_conn.execute(
        """
        SELECT id, verdict
        FROM validator_decisions
        WHERE trade_id = ?
        """,
        (trade_id,),
    ).fetchone()

    if not decision:
        return False

    pips = trade["pips_result"] or 0
    trade_won = pips > 2
    trade_lost = pips < -2
    result = "W" if trade_won else "L" if trade_lost else "BE"

    # Validator accuracy
    verdict = decision["verdict"].lower() if decision["verdict"] else ""
    if verdict == "approve":
        validator_correct = trade_won
    elif verdict == "reject":
        # Hypothetical: if it had been taken and lost, the reject was correct
        validator_correct = trade_lost
    else:
        # hold / watch — cannot assess directional accuracy
        validator_correct = None

    intel_conn.execute(
        """
        UPDATE validator_decisions SET
            trade_result = ?,
            trade_pips = ?,
            trade_closed_at = datetime('now'),
            validator_correct = ?,
            reconciled_at = datetime('now')
        WHERE trade_id = ?
        """,
        (result, pips, validator_correct, trade_id),
    )
    intel_conn.commit()
    return True


def get_accuracy_report(days_back: int = 30) -> Dict[str, Any]:
    """
    Generate an accuracy report for the validator.

    Aggregates reconciled ``validator_decisions`` rows over the specified window.
    Used by the distillation pipeline and daily performance review.

    Args:
        days_back: How many days to look back.

    Returns:
        Dict with accuracy stats and per-instrument breakdown.
    """
    conn = get_intelligence()
    conn.row_factory = sqlite3.Row

    cutoff = (datetime.utcnow() - timedelta(days=days_back)).isoformat()

    rows = conn.execute(
        """
        SELECT validator_correct,
               verdict,
               trade_result, trade_pips, instrument
        FROM validator_decisions
        WHERE reconciled_at IS NOT NULL
        AND created_at > ?
        """,
        (cutoff,),
    ).fetchall()

    if not rows:
        return {"error": f"No reconciled decisions in the last {days_back} days"}

    rows = [dict(r) for r in rows]

    validator_decisions = [r for r in rows if r["validator_correct"] is not None]

    def accuracy_pct(items: List[dict], key: str) -> float:
        correct = sum(1 for r in items if r.get(key))
        return round(correct / len(items) * 100, 1) if items else 0.0

    return {
        "period_days": days_back,
        "total_reconciled": len(rows),
        "validator": {
            "assessed": len(validator_decisions),
            "correct": sum(1 for r in validator_decisions if r["validator_correct"]),
            "accuracy_pct": accuracy_pct(validator_decisions, "validator_correct"),
        },
        "by_instrument": _group_by_instrument(rows),
    }


def export_for_distillation(days_back: int = 30) -> Dict[str, Any]:
    """
    Export full validator decision context for the distillation pipeline.

    Joins ``validator_decisions`` with ``intelligence_packages`` so downstream
    models can learn which intelligence inputs corresponded to correct/wrong calls.

    Args:
        days_back: How many days to look back.

    Returns:
        Dict with decision rows (with full package context), accuracy report,
        and export metadata.
    """
    conn = get_intelligence()
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT vd.*, ip.package_text
        FROM validator_decisions vd
        LEFT JOIN intelligence_packages ip ON vd.package_id = ip.id
        WHERE vd.reconciled_at IS NOT NULL
        AND vd.created_at > datetime('now', ?)
        """,
        (f"-{days_back} days",),
    ).fetchall()

    return {
        "decisions": [dict(r) for r in rows],
        "accuracy_report": get_accuracy_report(days_back),
        "meta": {
            "exported_at": datetime.utcnow().isoformat(),
            "days_back": days_back,
            "decision_count": len(rows),
        },
    }


def record_decision(
    cycle_id: str,
    instrument: str,
    verdict: str,
    confidence_raw: Optional[int],
    confidence_adjusted: Optional[int],
    confidence_adjustments: Optional[str],  # JSON
    flags: Optional[str],                    # JSON list
    position_size_recommendation: Optional[str],
    reasoning: Optional[str],
    package_id: Optional[int],
    window: Optional[str],
    trade_id: Optional[str],
    # Snapshot fields from package / rules
    macro_bias: Optional[str] = None,
    cot_signal: Optional[str] = None,
    calendar_clear: Optional[bool] = None,
    vix_level: Optional[float] = None,
    news_sentiment_score: Optional[float] = None,
) -> int:
    """
    Insert a new row into ``validator_decisions`` at the time of a verdict.

    Called by ``ValidationAnalyst.analyze_on_demand()`` after the LLM returns
    its enhanced verdict.

    Returns:
        The inserted row ID.
    """
    conn = get_intelligence()
    cursor = conn.execute(
        """
        INSERT INTO validator_decisions (
            cycle_id, trade_id, instrument, window, package_id,
            verdict, confidence_raw, confidence_adjusted,
            confidence_adjustments, flags, position_size_recommendation,
            reasoning,
            macro_bias, cot_signal, calendar_clear, vix_level,
            news_sentiment_score
        ) VALUES (
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?,
            ?, ?, ?, ?,
            ?
        )
        """,
        (
            cycle_id, trade_id, instrument, window or "unknown", package_id,
            verdict, confidence_raw, confidence_adjusted,
            confidence_adjustments, flags, position_size_recommendation,
            reasoning,
            macro_bias, cot_signal,
            1 if calendar_clear else 0 if calendar_clear is not None else None,
            vix_level,
            news_sentiment_score,
        ),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _group_by_instrument(rows: List[dict]) -> Dict[str, Any]:
    """Per-instrument breakdown of validator accuracy."""
    by_inst: Dict[str, List[dict]] = {}
    for r in rows:
        inst = r.get("instrument", "UNKNOWN")
        by_inst.setdefault(inst, []).append(r)

    result = {}
    for inst, items in sorted(by_inst.items()):
        assessed = [i for i in items if i.get("validator_correct") is not None]
        correct = sum(1 for i in assessed if i.get("validator_correct"))
        result[inst] = {
            "total": len(items),
            "assessed": len(assessed),
            "correct": correct,
            "accuracy_pct": round(correct / len(assessed) * 100, 1) if assessed else None,
        }
    return result
