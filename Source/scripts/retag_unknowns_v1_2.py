#!/usr/bin/env python3
"""Hand-classify unknown trades from beginning of live trading (2026-02-17 onwards).

20 winners + 18 losers + 1 orphan_closed = 39 'unknown' trades on the cohort.
Hand-classification by claude-code 2026-05-10 from indicator+fan state per trade.

Pattern findings:
- 18 winners + 5 losers fit C12_CASCADE_CONTINUATION (ordered bearish fan, non-contracting,
  stoch not overbought, no counter-momentum) — 78% WR
- 2 winners are S15-like (mean reversion from mid-range stoch on weakening fan)
- Several losers were counter-momentum entries (bullish momentum despite bearish fan)
- 3 losers were buys on bullish fans (would be C12 bullish-side)
- 3 losers had no fan/indicator data populated (older scout entries)

Usage:
    source ~/myenv/bin/activate
    python -m scripts.retag_unknowns_v1_2 --dry-run
    python -m scripts.retag_unknowns_v1_2 --execute
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from db_pool import get_trading_forex

logger = logging.getLogger("retag_unknowns_v1_2")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# Hand-classification map: trade_id -> (new_setup_name, rationale)
RETAG_MAP: dict[int, tuple[str, str]] = {
    # ── WINNERS ──
    354:  ("C12_CASCADE_CONTINUATION", "expanding/bear/strong_trend AUD_JPY"),
    458:  ("C12_CASCADE_CONTINUATION", "expanding/bear/strong_trend AUD_JPY"),
    366:  ("C12_CASCADE_CONTINUATION", "expanding/bear/bearish-momentum AUD_USD"),
    1320: ("S15", "stable/neutral fan + ranging — mean-reversion-ish, not cascade"),
    3601: ("C12_CASCADE_CONTINUATION", "expanding/bearish EUR_AUD"),
    4365: ("C12_CASCADE_CONTINUATION", "expanding/bearish EUR_AUD"),
    5294: ("C12_CASCADE_CONTINUATION", "expanding/bearish EUR_CHF"),
    5438: ("C12_CASCADE_CONTINUATION", "expanding/bearish EUR_CHF"),
    5591: ("C12_CASCADE_CONTINUATION", "expanding/bearish EUR_CHF"),
    4467: ("C12_CASCADE_CONTINUATION", "just_crossed/bearish — beginning of cascade EUR_USD"),
    4493: ("C12_CASCADE_CONTINUATION", "expanding/bearish EUR_USD"),
    404:  ("C12_CASCADE_CONTINUATION", "decelerating but bearish-momentum strong_trend GBP_JPY"),
    578:  ("C12_CASCADE_CONTINUATION", "peaked but caught early NZD_USD"),
    4009: ("C12_CASCADE_CONTINUATION", "decelerating/bearish NZD_USD"),
    272:  ("C12_CASCADE_CONTINUATION", "stable/bear/bearish-momentum USD_CAD"),
    6482: ("C12_CASCADE_CONTINUATION", "expanding/bearish USD_CAD"),
    3353: ("C12_CASCADE_CONTINUATION", "decelerating/bear/ranging USD_CHF"),
    3451: ("C12_CASCADE_CONTINUATION", "stable/bear/bearish-momentum USD_CHF"),
    5300: ("C12_CASCADE_CONTINUATION", "expanding/bearish USD_CHF"),
    3893: ("S15", "contracting/bearish + stoch 69/RSI 54 — late mean-reversion USD_JPY"),

    # ── LOSERS — fit C12 pattern but lost (still tag for accuracy) ──
    4896: ("C12_CASCADE_CONTINUATION", "expanding/bearish EUR_GBP — fit but lost"),
    4507: ("C12_CASCADE_CONTINUATION", "expanding/bearish GBP_JPY — fit but lost"),
    227:  ("C12_CASCADE_CONTINUATION", "stable/bear/neutral USD_CAD — fit but lost"),
    1295: ("C12_CASCADE_CONTINUATION", "stable/bear/neutral USD_CAD — fit but lost"),
    4712: ("C12_CASCADE_CONTINUATION", "expanding/bearish USD_JPY — fit but lost"),

    # ── LOSERS — C12-like but had counter-momentum or other disqualifier ──
    408:  ("S15_FAILED", "contracting/bear + stoch 98.7 + bullish-momentum — counter-momentum AUD_USD"),
    239:  ("C12_CASCADE_CONTINUATION", "stable/bear/bearish-momentum EUR_AUD — fit but small loss"),
    276:  ("C12_CONTRACTING_FAIL", "contracting/bear EUR_AUD — exhausted fan"),
    209:  ("C12_CASCADE_CONTINUATION", "stable/bear/neutral EUR_GBP — fit but lost"),
    221:  ("C12_CASCADE_CONTINUATION", "stable/bear/bearish EUR_GBP — fit but lost"),
    3433: ("UNCLASSIFIED_NO_FAN_DIR", "contracting/neutral fan GBP_JPY — no clear setup"),
    3445: ("C12_CONTRACTING_FAIL", "contracting/bearish USD_JPY — exhausted fan"),
    3477: ("COUNTER_MOMENTUM_FAIL", "contracting/neutral + bullish-momentum USD_JPY — counter to fan"),
    3807: ("UNCLASSIFIED_NO_FAN_DIR", "contracting/neutral USD_JPY — no clear setup"),

    # ── LOSERS — buys on bullish fan (would be C12_BULLISH if it had won) ──
    3141: ("C12_CASCADE_CONTINUATION", "expanding/bull/bullish-momentum/support GBP_USD — bull-side C12, lost"),

    # ── No-data scout entries ──
    3931: ("UNCLASSIFIED_NO_DATA", "no fan/indicator data populated"),
    4780: ("UNCLASSIFIED_NO_DATA", "no fan/indicator data populated"),
    13713: ("UNCLASSIFIED_NO_DATA", "no fan/indicator data populated"),
    4896: ("C12_CASCADE_CONTINUATION", "expanding/bearish EUR_GBP — fit but lost"),  # dup safe

    # ── Orphan ──
    4856: ("C12_CASCADE_CONTINUATION", "expanding/bearish USD_CAD orphan_closed"),
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    if not (args.dry_run or args.execute):
        p.error("specify --dry-run or --execute")

    conn = get_trading_forex()

    if args.execute:
        conn.execute("BEGIN")
    try:
        updated = 0
        for tid, (new_setup, rationale) in RETAG_MAP.items():
            row = conn.execute(
                "SELECT setup, pair, direction, outcome, ROUND(pnl_pips,1) FROM live_trades WHERE id = ?",
                (tid,)
            ).fetchone()
            if not row:
                logger.warning("trade_id %s not found, skipping", tid)
                continue
            old_setup, pair, direction, outcome, pnl = row
            if old_setup != 'unknown':
                logger.info("trade_id %s already retagged as %s, skipping", tid, old_setup)
                continue
            logger.info(
                "%s trade %s %s/%s/%s pnl=%s: setup '%s' → '%s' (%s)",
                "UPDATE" if args.execute else "DRY",
                tid, pair, direction, outcome, pnl, old_setup, new_setup, rationale,
            )
            if args.execute:
                conn.execute(
                    "UPDATE live_trades SET setup = ? WHERE id = ?",
                    (new_setup, tid),
                )
                updated += 1
        if args.execute:
            conn.execute("COMMIT")
        logger.info("Done. Updated=%d", updated)
    except Exception:
        if args.execute:
            conn.execute("ROLLBACK")
        raise


if __name__ == "__main__":
    main()
