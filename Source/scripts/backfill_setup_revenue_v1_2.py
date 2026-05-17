#!/usr/bin/env python3
"""Backfill setup_revenue from live_trades.setup for v1.2 audit window.

Background
----------
position_guardian.py was reading setup_name from trade_decisions.setup (always empty)
and falling through to a classifier that ran on the EXIT bar — mis-attributing
every winning S16/V4/C trade to S15/S5/S1 bleeders. See
`.planning/v1.2-audit/LOOP-BREAK-FINDINGS.md`.

This script repairs the damage in setup_revenue + setup_trades for the cohort
window 2026-04-17 → today by:

1. For each (setup_name, pair, user_id) currently in setup_revenue, compute the
   cohort's contribution from setup_trades (cohort = closed_at >= 2026-04-17).
2. Subtract that contribution from setup_revenue (or delete the row if the row
   becomes empty after subtraction).
3. Delete setup_trades rows for the cohort window.
4. Replay each closed live_trades entry from the cohort through
   SetupRevenueTracker.record_trade() with the correct entry-time setup name
   (live_trades.setup).

This re-runs the auto-promote check on each replayed trade, so winning setups
that now correctly accumulate their wins will get promoted naturally.

Usage:
    source ~/myenv/bin/activate
    python -m scripts.backfill_setup_revenue_v1_2 --dry-run   # show plan
    python -m scripts.backfill_setup_revenue_v1_2 --execute   # apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure parent Source/ on path so relative imports work from any CWD
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from db_pool import get_trading_forex
from setup_revenue import SetupRevenueTracker

logger = logging.getLogger("backfill_setup_revenue_v1_2")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

COHORT_START = "2026-02-17"  # Extended 2026-05-10 to cover all manual trades since
                              # beginning of live trading (first manual: 2026-02-25;
                              # first paper: 2026-02-17). Was originally "2026-04-17"
                              # for the v1.2 audit cohort — re-running with extended
                              # range subtracts + replays all non-kronos history.


def compute_cohort_contributions(conn) -> dict:
    """Aggregate setup_trades cohort rows by (setup_name, pair, user_id)."""
    rows = conn.execute(
        """
        SELECT setup_name, pair, user_id,
               COUNT(*) AS n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
               SUM(pnl_pips) AS total_pips,
               SUM(pnl_usd) AS total_usd
        FROM setup_trades
        WHERE closed_at >= ?
        GROUP BY setup_name, pair, user_id
        """,
        (COHORT_START,),
    ).fetchall()
    return {(r[0], r[1], r[2]): {
        "n": r[3], "wins": r[4], "losses": r[5],
        "total_pips": r[6] or 0.0, "total_usd": r[7] or 0.0,
    } for r in rows}


def subtract_from_setup_revenue(conn, contributions: dict, execute: bool):
    """Subtract cohort contributions from setup_revenue (or delete row if empty)."""
    for (setup, pair, uid), c in contributions.items():
        rev = conn.execute(
            "SELECT total_trades, wins, losses, total_pips, total_usd "
            "FROM setup_revenue WHERE setup_name=? AND pair=? AND user_id=?",
            (setup, pair, uid),
        ).fetchone()
        if not rev:
            continue
        new_n = rev[0] - c["n"]
        new_w = rev[1] - c["wins"]
        new_l = rev[2] - c["losses"]
        new_pips = (rev[3] or 0) - c["total_pips"]
        new_usd = (rev[4] or 0) - c["total_usd"]
        if new_n <= 0:
            logger.info("DELETE setup_revenue %s/%s (uid=%s) — fully cohort-attributed", setup, pair, uid)
            if execute:
                conn.execute(
                    "DELETE FROM setup_revenue WHERE setup_name=? AND pair=? AND user_id=?",
                    (setup, pair, uid),
                )
        else:
            new_wr = new_w / new_n if new_n else 0
            logger.info(
                "UPDATE setup_revenue %s/%s (uid=%s) trades %d→%d wins %d→%d pips %.1f→%.1f usd %.0f→%.0f",
                setup, pair, uid, rev[0], new_n, rev[1], new_w,
                rev[3] or 0, new_pips, rev[4] or 0, new_usd,
            )
            if execute:
                conn.execute(
                    """
                    UPDATE setup_revenue SET
                        total_trades=?, wins=?, losses=?, total_pips=?, total_usd=?, win_rate=?
                    WHERE setup_name=? AND pair=? AND user_id=?
                    """,
                    (new_n, new_w, new_l, new_pips, new_usd, new_wr, setup, pair, uid),
                )


def delete_cohort_setup_trades(conn, execute: bool) -> int:
    """Delete setup_trades rows in the cohort window."""
    n = conn.execute(
        "SELECT COUNT(*) FROM setup_trades WHERE closed_at >= ?",
        (COHORT_START,),
    ).fetchone()[0]
    logger.info("DELETE %d setup_trades rows where closed_at >= %s", n, COHORT_START)
    if execute:
        conn.execute("DELETE FROM setup_trades WHERE closed_at >= ?", (COHORT_START,))
    return n


def replay_cohort(conn, execute: bool) -> tuple[int, int]:
    """Replay each closed live_trade in the cohort through tracker.record_trade."""
    rows = conn.execute(
        """
        SELECT id, pair, direction, setup, market_story, pnl_pips, pnl_usd,
               entry_price, exit_price, sl_price, tp_price, units,
               outcome_r, source, entry_time, exit_time, user_id, oanda_trade_id
        FROM live_trades
        WHERE entry_time >= ? AND status='closed'
          AND entry_type NOT IN ('kronos_hunter','kronos_snipe')
        ORDER BY exit_time
        """,
        (COHORT_START,),
    ).fetchall()
    logger.info("Replaying %d closed trades from cohort", len(rows))
    if not execute:
        for r in rows[:5]:
            logger.info("DRY-RUN sample: trade %s %s %s setup=%s pnl_pips=%.1f", r[0], r[1], r[2], r[3], r[5] or 0)
        return len(rows), 0

    tracker = SetupRevenueTracker()
    promoted = 0
    for r in rows:
        try:
            (trade_id, pair, direction, setup, story_json, pnl_pips, pnl_usd,
             entry_p, exit_p, sl_p, tp_p, units, r_mult, source, entry_time,
             exit_time, user_id, oanda_id) = r
            setup_name = setup or 'unknown'
            duration_min = 0
            try:
                if entry_time and exit_time:
                    et = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
                    xt = datetime.fromisoformat(exit_time.replace('Z', '+00:00'))
                    duration_min = max(0, int((xt - et).total_seconds() // 60))
            except Exception:
                pass
            story_kwargs = {}
            if story_json:
                try:
                    import json as _json
                    story_kwargs = _json.loads(story_json) if isinstance(story_json, str) else story_json
                except Exception:
                    story_kwargs = {}
            result = tracker.record_trade(
                trade_id=str(trade_id),
                setup_name=setup_name,
                pair=pair,
                direction=direction,
                pnl_pips=pnl_pips or 0.0,
                pnl_usd=pnl_usd or 0.0,
                entry_price=entry_p or 0.0,
                exit_price=exit_p or 0.0,
                stop_loss=sl_p,
                take_profit=tp_p,
                units=int(units or 0),
                r_multiple=r_mult or 0.0,
                duration_minutes=duration_min,
                source=source or 'unknown',
                threat_zone_at_close=None,
                opened_at=entry_time,
                user_id=user_id,
                **{k: v for k, v in story_kwargs.items() if k in {
                    'fan_state', 'fan_direction', 'fan_ordered', 'momentum_state',
                    'cascade_direction', 'retracement_type', 'e100_role',
                }},
            )
            if result.get('promotion_action') == 'promoted':
                promoted += 1
                logger.info("PROMOTED %s on %s after replay", setup_name, pair)
        except Exception as e:
            logger.warning("Replay failed for trade %s: %s", r[0], e)
    return len(rows), promoted


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="show plan, don't apply")
    p.add_argument("--execute", action="store_true", help="apply changes")
    args = p.parse_args()
    if not (args.dry_run or args.execute):
        p.error("specify --dry-run or --execute")

    conn = get_trading_forex()
    conn.row_factory = None  # tuple rows

    logger.info("=== STEP 1: compute cohort contributions ===")
    contribs = compute_cohort_contributions(conn)
    logger.info("Cohort affects %d (setup_name, pair, user_id) rows", len(contribs))

    # Steps 2+3 in one transaction (subtract then delete must be atomic together).
    # Step 4 is replay via tracker.record_trade which manages its own per-trade transactions.
    if args.execute:
        conn.execute("BEGIN")
        try:
            logger.info("=== STEP 2: subtract cohort contributions from setup_revenue ===")
            subtract_from_setup_revenue(conn, contribs, True)
            logger.info("=== STEP 3: delete setup_trades cohort rows ===")
            delete_cohort_setup_trades(conn, True)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    else:
        logger.info("=== STEP 2 (dry-run): subtract cohort contributions ===")
        subtract_from_setup_revenue(conn, contribs, False)
        logger.info("=== STEP 3 (dry-run): delete setup_trades cohort rows ===")
        delete_cohort_setup_trades(conn, False)

    logger.info("=== STEP 4: replay live_trades cohort with correct setup_name ===")
    replayed, promoted = replay_cohort(conn, args.execute)
    logger.info("Done. Replayed=%d Promoted=%d", replayed, promoted)


if __name__ == "__main__":
    main()
