"""Scout-as-entry-engine diagnostics.

Twelve analytics that treat the scout as the primary entry engine: learning-loop
health (finding_id population), score/story calibration, confidence-threshold
sweep, setup-catalog performance (stars and dogs), pipeline blockage from
flight_log, pattern feature importance, direct-scout trade audit, pair bias,
stochastic-cross quality, PATH A retracement performance, and multi-timeframe
alignment impact.

Pool-managed connections (db_pool.get_trading_forex / get_flight_recorder) are
thread-local and cached; we do NOT close them. Lifecycle is owned by the pool.
Matches pattern established in diagnostics.context (A1), diagnostics.live_health
(A2), and diagnostics.watch_health (A10).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

from db_pool import get_trading_forex, get_flight_recorder

from diagnostics.context import Window


def learning_loop_health(window: Window) -> Dict[str, Any]:
    """Check finding_id population rate in live_trades.

    Known bug per tuning-system.md: finding_id is ~100% NULL since 2026-03-29,
    which means the learning loop that ties trade outcomes back to scout
    findings is broken. `broken=True` when > 50% of trades have NULL finding_id.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    row = conn.execute(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN finding_id IS NULL THEN 1 ELSE 0 END) AS null_count,
               MAX(exit_time) AS last_trade,
               MAX(CASE WHEN finding_id IS NOT NULL THEN exit_time END) AS last_populated
        FROM live_trades
        WHERE {window.to_sql_clause('exit_time')}
          AND exit_time IS NOT NULL
    """).fetchone()
    total = row["total"] or 0
    null_n = row["null_count"] or 0
    null_rate = null_n / total if total else 0.0
    return {
        "window": window.label,
        "total_trades": total,
        "finding_id_null": null_n,
        "finding_id_populated": total - null_n,
        "null_rate": null_rate,
        "last_trade": row["last_trade"],
        "last_populated_trade": row["last_populated"],
        "broken": null_rate > 0.50,
        "message": (
            f"Learning loop BROKEN: {null_rate*100:.0f}% of trades have NULL finding_id"
            if null_rate > 0.50 else f"Learning loop OK: {(1-null_rate)*100:.0f}% populated"
        ),
    }


def score_calibration(window: Window) -> Dict[str, Any]:
    """Bucket scout_alerts by sniper_score; report WR and avg pips per bucket.

    Note: scout_alerts.outcome is mostly NULL in current DB (learning loop
    not populating it). Buckets with n=0 are still reported for structural
    completeness.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    buckets = [(0, 40), (40, 55), (55, 70), (70, 85), (85, 101)]
    out: Dict[str, Any] = {}
    for lo, hi in buckets:
        row = conn.execute("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
                   AVG(pips_result) AS avg_pips
            FROM scout_alerts
            WHERE timestamp >= ? AND timestamp <= ?
              AND sniper_score >= ? AND sniper_score < ?
              AND outcome IS NOT NULL
        """, (window.start.isoformat(), window.end.isoformat(), lo, hi)).fetchone()
        n = row["n"] or 0
        out[f"{lo:.0f}-{hi:.0f}"] = {
            "n": n,
            "win_rate": (row["wins"] or 0) / n if n else 0.0,
            "avg_pips": row["avg_pips"] or 0.0,
        }
    return out


def story_score_calibration(window: Window) -> Dict[str, Any]:
    """Bucket scout_alerts by story_score; report WR and avg pips per bucket."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    buckets = [(0, 30), (30, 50), (50, 70), (70, 90), (90, 101)]
    out: Dict[str, Any] = {}
    for lo, hi in buckets:
        row = conn.execute("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
                   AVG(pips_result) AS avg_pips
            FROM scout_alerts
            WHERE timestamp >= ? AND timestamp <= ?
              AND story_score >= ? AND story_score < ?
              AND outcome IS NOT NULL
        """, (window.start.isoformat(), window.end.isoformat(), lo, hi)).fetchone()
        n = row["n"] or 0
        out[f"{lo:.0f}-{hi:.0f}"] = {
            "n": n,
            "win_rate": (row["wins"] or 0) / n if n else 0.0,
            "avg_pips": row["avg_pips"] or 0.0,
        }
    return out


def confidence_threshold_sweep(window: Window) -> Dict[str, Any]:
    """Simulate different scout.min_confidence values — what would each retain/win?

    live_trades has no scout_confidence column, so confidence is sourced from
    scout_findings.scout_confidence joined via live_trades.finding_id. With
    finding_id near-100% NULL (learning-loop bug), this sweep is currently
    near-empty — the upstream fix is to repair finding_id population.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    thresholds = [0.40, 0.50, 0.55, 0.60, 0.65, 0.70]
    out: Dict[str, Any] = {}
    for th in thresholds:
        row = conn.execute(f"""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN lt.outcome='win' THEN 1 ELSE 0 END) AS wins,
                   AVG(lt.pnl_pips) AS avg_pips,
                   SUM(lt.pnl_pips) AS total_pips
            FROM live_trades lt
            JOIN scout_findings sf ON CAST(sf.id AS TEXT) = lt.finding_id
            WHERE {window.to_sql_clause('lt.exit_time')}
              AND lt.exit_time IS NOT NULL
              AND sf.scout_confidence >= ?
        """, (th,)).fetchone()
        n = row["n"] or 0
        out[f"{th:.2f}"] = {
            "retained": n,
            "win_rate": (row["wins"] or 0) / n if n else 0.0,
            "avg_pips": row["avg_pips"] or 0.0,
            "total_pips": row["total_pips"] or 0.0,
        }
    return out


def setup_catalog_performance(window: Window, min_trades: int = 5) -> List[Dict[str, Any]]:
    """Per setup_code: n, WR, avg/total pips; flag `star` (WR>=70% n>=10)
    and `dog` (WR<40% n>=10)."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"""
        SELECT setup_code,
               COUNT(*) AS n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
               AVG(pnl_pips) AS avg_pips,
               SUM(pnl_pips) AS total_pips
        FROM live_trades
        WHERE {window.to_sql_clause('exit_time')}
          AND exit_time IS NOT NULL
          AND setup_code IS NOT NULL
        GROUP BY setup_code
        HAVING n >= ?
        ORDER BY total_pips DESC
    """, (min_trades,)).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        n = r["n"]
        wr = (r["wins"] or 0) / n
        flag = "star" if wr >= 0.70 and n >= 10 else "dog" if wr < 0.40 and n >= 10 else "normal"
        out.append({
            "setup_code": r["setup_code"],
            "n": n,
            "win_rate": wr,
            "avg_pips": r["avg_pips"] or 0.0,
            "total_pips": r["total_pips"] or 0.0,
            "flag": flag,
        })
    return out


def scout_blockage(window: Window) -> Dict[str, Any]:
    """Where do scout cycles die?

    Reads flight_log to attribute scout-cycle outcomes. The plan's stage names
    (`GATE_1_REJECT`, `VALIDATOR_SKIP`, etc.) do not exist in this schema.
    Actual stages used here (all lowercase): `kronos_filter_reject` (hunter-side
    filter), `validator_verdict` (filtered to non-TRADE verdicts), and
    `scout_scan` / `scout_alert` for scan volume. Scout-cycle queue uses
    `queue_enter`.
    """
    conn = get_flight_recorder()
    conn.row_factory = sqlite3.Row
    queued_row = conn.execute(f"""
        SELECT COUNT(*) FROM flight_log
        WHERE stage = 'queue_enter'
          AND {window.to_sql_clause('timestamp')}
    """).fetchone()
    queued = queued_row[0] if queued_row else 0

    # Stage-level rejections that exist in schema. Validator WATCH/REJECT
    # verdicts are inferred from the data JSON; simpler to count status != 'ok'
    # on validator_verdict as "rejections" (includes timeouts / errors).
    rejections_rows = conn.execute(f"""
        SELECT stage, COUNT(*) AS n
        FROM flight_log
        WHERE stage IN ('kronos_filter_reject')
          AND {window.to_sql_clause('timestamp')}
        GROUP BY stage
    """).fetchall()
    rejections: Dict[str, int] = {r["stage"]: r["n"] for r in rejections_rows}

    # Validator verdicts other than TRADE (WATCH / REJECT / null) count as
    # pipeline filtering. Read verdict from JSON data column.
    validator_rows = conn.execute(f"""
        SELECT data FROM flight_log
        WHERE stage = 'validator_verdict'
          AND {window.to_sql_clause('timestamp')}
    """).fetchall()
    validator_watch = 0
    validator_reject = 0
    validator_skip = 0
    for r in validator_rows:
        raw = r["data"] or ""
        verdict_upper = ""
        # Cheap substring check avoids a json.loads on every row.
        if '"verdict"' in raw:
            lower = raw.lower()
            if '"verdict": "trade"' in lower or '"verdict":"trade"' in lower:
                continue
            if '"verdict": "watch"' in lower or '"verdict":"watch"' in lower:
                validator_watch += 1
                continue
            if '"verdict": "reject"' in lower or '"verdict":"reject"' in lower:
                validator_reject += 1
                continue
        validator_skip += 1
    if validator_watch:
        rejections["validator_watch"] = validator_watch
    if validator_reject:
        rejections["validator_reject"] = validator_reject
    if validator_skip:
        rejections["validator_skip"] = validator_skip

    return {
        "scout_cycles_queued": queued,
        "rejections_by_stage": rejections,
    }


def pattern_feature_importance(window: Window) -> Dict[str, Any]:
    """Correlate scout_alerts features to outcomes; per-value WR and n."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    features = ["is_retracement", "both_expanding", "both_contracting", "cascade_phase", "h4_bias"]
    out: Dict[str, Any] = {}
    for feat in features:
        rows = conn.execute(f"""
            SELECT {feat} AS val, COUNT(*) AS n,
                   SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins
            FROM scout_alerts
            WHERE timestamp >= ? AND timestamp <= ?
              AND outcome IS NOT NULL
            GROUP BY {feat}
        """, (window.start.isoformat(), window.end.isoformat())).fetchall()
        out[feat] = {
            str(r["val"]): {
                "n": r["n"],
                "win_rate": (r["wins"] or 0) / r["n"] if r["n"] else 0.0,
            }
            for r in rows
        }
    return out


def direct_scout_trade_audit(window: Window) -> Dict[str, Any]:
    """Performance of trades whose source == 'scout' (scout-direct path).

    Recommendation: `deprecate_path` (n>0 and WR<40%), `investigate` (n<5),
    else `keep`.
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    row = conn.execute(f"""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
               AVG(pnl_pips) AS avg_pips,
               SUM(pnl_pips) AS total_pips
        FROM live_trades
        WHERE source = 'scout'
          AND {window.to_sql_clause('exit_time')}
          AND exit_time IS NOT NULL
    """).fetchone()
    n = row["n"] or 0
    return {
        "n": n,
        "win_rate": (row["wins"] or 0) / n if n else 0.0,
        "avg_pips": row["avg_pips"] or 0.0,
        "total_pips": row["total_pips"] or 0.0,
        "recommendation": (
            "deprecate_path" if n > 0 and (row["wins"] or 0) / n < 0.4
            else "investigate" if n < 5
            else "keep"
        ),
    }


def pair_bias_analysis(window: Window) -> Dict[str, Any]:
    """Per-pair scout_findings counts; `biased_pairs` are > 2x average."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT pair, COUNT(*) AS findings
        FROM scout_findings
        WHERE timestamp >= ? AND timestamp <= ?
        GROUP BY pair
        ORDER BY findings DESC
    """, (window.start.isoformat(), window.end.isoformat())).fetchall()
    if not rows:
        return {"pairs": {}, "biased_pairs": []}
    total = sum(r["findings"] for r in rows)
    avg = total / len(rows)
    pairs = {r["pair"]: {"n": r["findings"], "pct_of_total": r["findings"] / total} for r in rows}
    biased = [r["pair"] for r in rows if r["findings"] > 2 * avg]
    return {"pairs": pairs, "biased_pairs": biased, "avg_per_pair": avg}


def stoch_cross_quality(window: Window) -> Dict[str, Any]:
    """%K/%D cross at oversold/overbought — does it correlate with wins?

    Classifies scout_alerts as "with cross" when direction='buy' and
    current_stoch_k < 35 and %K > %D, or direction='sell' and
    current_stoch_k > 65 and %K < %D. All others are "without cross".
    """
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    with_cross = conn.execute("""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
               AVG(pips_result) AS avg_pips
        FROM scout_alerts
        WHERE timestamp >= ? AND timestamp <= ?
          AND outcome IS NOT NULL
          AND ((direction='buy'  AND current_stoch_k < 35 AND current_stoch_k > current_stoch_d)
            OR (direction='sell' AND current_stoch_k > 65 AND current_stoch_k < current_stoch_d))
    """, (window.start.isoformat(), window.end.isoformat())).fetchone()
    without = conn.execute("""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
               AVG(pips_result) AS avg_pips
        FROM scout_alerts
        WHERE timestamp >= ? AND timestamp <= ?
          AND outcome IS NOT NULL
          AND NOT ((direction='buy'  AND current_stoch_k < 35 AND current_stoch_k > current_stoch_d)
                OR (direction='sell' AND current_stoch_k > 65 AND current_stoch_k < current_stoch_d))
    """, (window.start.isoformat(), window.end.isoformat())).fetchone()
    return {
        "with_stoch_cross": {
            "n": with_cross["n"] or 0,
            "win_rate": (with_cross["wins"] or 0) / (with_cross["n"] or 1),
            "avg_pips": with_cross["avg_pips"] or 0.0,
        },
        "without_stoch_cross": {
            "n": without["n"] or 0,
            "win_rate": (without["wins"] or 0) / (without["n"] or 1),
            "avg_pips": without["avg_pips"] or 0.0,
        },
    }


def path_a_retracement_performance(window: Window) -> Dict[str, Any]:
    """scout_alerts where is_retracement=1 — did PATH A help?"""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
               AVG(pips_result) AS avg_pips
        FROM scout_alerts
        WHERE timestamp >= ? AND timestamp <= ?
          AND is_retracement = 1
          AND outcome IS NOT NULL
    """, (window.start.isoformat(), window.end.isoformat())).fetchone()
    n = row["n"] or 0
    return {
        "n": n,
        "win_rate": (row["wins"] or 0) / n if n else 0.0,
        "avg_pips": row["avg_pips"] or 0.0,
    }


def multi_timeframe_alignment_impact(window: Window) -> Dict[str, Any]:
    """Aligned (h4_bias matches direction) vs counter-trend performance."""
    conn = get_trading_forex()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT h4_bias, direction, COUNT(*) AS n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
               AVG(pips_result) AS avg_pips
        FROM scout_alerts
        WHERE timestamp >= ? AND timestamp <= ?
          AND outcome IS NOT NULL
          AND h4_bias IS NOT NULL
        GROUP BY h4_bias, direction
    """, (window.start.isoformat(), window.end.isoformat())).fetchall()
    out: Dict[str, Any] = {"aligned": {"n": 0, "wins": 0, "total_pips": 0.0},
                           "counter": {"n": 0, "wins": 0, "total_pips": 0.0}}
    for r in rows:
        aligned = (r["h4_bias"] or "").lower() == (r["direction"] or "").lower()[:4].rstrip("y")
        bucket = "aligned" if aligned else "counter"
        out[bucket]["n"] += r["n"]
        out[bucket]["wins"] += r["wins"] or 0
        out[bucket]["total_pips"] += (r["avg_pips"] or 0) * (r["n"] or 0)
    for b in out.values():
        b["win_rate"] = b["wins"] / b["n"] if b["n"] else 0.0
        b["avg_pips"] = b["total_pips"] / b["n"] if b["n"] else 0.0
    return out
