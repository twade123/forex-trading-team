"""build_scout_history.py — Backfill scout-history context for the cohort.

Computes AS-OF-ENTRY-TIME stats for each cohort trade's setup+pair from
live_trades historical data (excluding the trade itself + any trade with
entry_time >= cohort entry). Mirrors the live scout enrichment fields:

    win_rate, trade_count, wins, losses, gross_revenue ($), gross_revenue_pips,
    profit_factor, promoted

Reads the existing /tmp/cohort_indicator_blocks.json, appends a SCOUT CONTEXT
section to each trade's block_text + stores raw stats in a new scout_history
dict per trade. Writes back to the same path.

Run AFTER any active replay has finished:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python3 scripts/build_scout_history.py
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

TRADING_DB = os.path.expanduser("~/Jarvis/Database/v2/trading_forex.db")
INDICATOR_JSON = "/tmp/cohort_indicator_blocks.json"

# Cohort: (trade_id, pair, direction, entry_iso, setup_name)
# Setup names pulled from live_trades.setup query (2026-05-10)
COHORT = [
    ("13138", "AUD_JPY", "SELL", "2026-04-29T18:49:36+00:00", "S16"),
    ("13310", "AUD_JPY", "SELL", "2026-04-30T09:49:57+00:00", "S16"),
    ("13362", "AUD_JPY", "SELL", "2026-04-30T10:50:05+00:00", "S16"),
    ("13396", "EUR_CHF", "SELL", "2026-04-30T13:48:54+00:00", "C4_CHART_PATTERN_BREAK"),
    ("13424", "USD_CAD", "SELL", "2026-04-30T15:45:49+00:00", "C5_FIB_REACTION"),
    ("13452", "EUR_AUD", "SELL", "2026-05-01T16:34:10+00:00", "S16"),
    ("13578", "AUD_USD", "SELL", "2026-05-04T16:51:45+00:00", "S16"),
    ("13621", "GBP_USD", "BUY",  "2026-05-05T23:51:09+00:00", "S16"),
    ("13665", "USD_CAD", "SELL", "2026-05-06T02:09:42+00:00", "C9_BEAR_EXP_PULLBACK"),
    ("13681", "USD_CHF", "SELL", "2026-05-06T11:08:42+00:00", "S16"),
    ("13705", "EUR_USD", "BUY",  "2026-05-07T10:17:52+00:00", "S16"),
    ("13713", "NZD_USD", "BUY",  "2026-05-07T10:28:41+00:00", "UNCLASSIFIED_NO_DATA"),
    ("13727", "AUD_USD", "SELL", "2026-05-07T21:21:27+00:00", "C11_BIG_MOVE"),
    ("13743", "AUD_JPY", "SELL", "2026-05-07T22:04:25+00:00", "S16"),
    ("13765", "GBP_JPY", "BUY",  "2026-05-08T07:10:15+00:00", "C4_CHART_PATTERN_BREAK"),
    ("13809", "GBP_USD", "BUY",  "2026-05-08T09:36:34+00:00", "C4_CHART_PATTERN_BREAK"),
    ("13817", "EUR_JPY", "BUY",  "2026-05-08T10:02:34+00:00", "C9_BEAR_EXP_PULLBACK"),
    ("13827", "EUR_USD", "BUY",  "2026-05-08T10:17:53+00:00", "S16"),
    ("13843", "AUD_JPY", "BUY",  "2026-05-08T11:17:30+00:00", "C9_BEAR_EXP_PULLBACK"),
]


def fetch_as_of_history(setup: str, pair: str, as_of_iso: str) -> dict:
    """Aggregate closed trades with same setup+pair where entry_time < as_of_iso.

    Returns dict mirroring the live scout enrichment fields. Non-leaky:
    the cohort trade itself plus any later trades are excluded by the time filter.
    """
    if setup == "UNCLASSIFIED_NO_DATA":
        return {"trade_count": 0, "setup": setup, "pair": pair, "note": "unclassified at entry"}

    conn = sqlite3.connect(TRADING_DB, timeout=10)
    try:
        rows = conn.execute(
            """SELECT pips, pnl_usd FROM live_trades
               WHERE setup = ? AND pair = ?
                 AND status = 'closed'
                 AND entry_time < ?""",
            (setup, pair, as_of_iso),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {
            "trade_count": 0, "wins": 0, "losses": 0,
            "win_rate": None, "gross_revenue": 0.0, "gross_revenue_pips": 0.0,
            "profit_factor": None, "promoted": False,
            "setup": setup, "pair": pair,
            "note": "no prior trades (new setup × pair combo)",
        }

    pips_list = [r[0] or 0.0 for r in rows]
    usd_list = [r[1] or 0.0 for r in rows]
    wins = sum(1 for p in pips_list if p > 0)
    losses = sum(1 for p in pips_list if p <= 0)
    total_pips = sum(pips_list)
    total_usd = sum(usd_list)
    win_rate = wins / len(rows) * 100 if rows else None
    gross_win_usd = sum(u for p, u in zip(pips_list, usd_list) if p > 0)
    gross_loss_usd = abs(sum(u for p, u in zip(pips_list, usd_list) if p <= 0))
    if gross_loss_usd == 0:
        pf = float("inf") if gross_win_usd > 0 else None
    else:
        pf = gross_win_usd / gross_loss_usd
    return {
        "trade_count": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1) if win_rate is not None else None,
        "gross_revenue": round(total_usd, 2),
        "gross_revenue_pips": round(total_pips, 1),
        "profit_factor": round(pf, 2) if pf not in (None, float("inf")) else pf,
        "promoted": False,  # historical row, promotion flag is current-state
        "setup": setup,
        "pair": pair,
    }


def format_scout_section(history: dict, direction: str) -> str:
    setup = history.get("setup", "?")
    pair = history.get("pair", "?")
    n = history.get("trade_count", 0)
    if n == 0:
        note = history.get("note", "no prior trades")
        return (
            f"**Scout context:** {setup} → {direction} on {pair}\n"
            f"- Track Record on this pair: **{note}** — no historical edge to lean on\n"
        )
    wr = history.get("win_rate")
    w = history.get("wins", 0)
    l = history.get("losses", 0)
    usd = history.get("gross_revenue", 0)
    pips = history.get("gross_revenue_pips", 0)
    pf = history.get("profit_factor")
    pf_str = "∞" if pf == float("inf") else (f"{pf}" if pf is not None else "N/A")
    badge = ""
    if wr is not None and wr >= 75 and n >= 5:
        badge = " 🎯"
    elif wr is not None and wr <= 40 and n >= 5:
        badge = " ⚠️"
    return (
        f"**Scout context:** {setup} → {direction} on {pair}\n"
        f"- Track Record on this pair: {w}W/{l}L ({wr}% WR over {n} trades) | "
        f"Gross: ${usd:+.2f} / {pips:+.1f}p | PF={pf_str}{badge}\n"
    )


def main():
    if not Path(INDICATOR_JSON).exists():
        raise SystemExit(f"{INDICATOR_JSON} not found — run build_cohort_indicators.py first")
    data = json.loads(Path(INDICATOR_JSON).read_text())
    for trade_id, pair, direction, entry_iso, setup in COHORT:
        history = fetch_as_of_history(setup, pair, entry_iso)
        scout_section = format_scout_section(history, direction)
        if trade_id not in data:
            print(f"[{trade_id}] not in indicator JSON, skipping")
            continue
        entry = data[trade_id]
        entry["scout_history"] = history
        entry["scout_section"] = scout_section
        # Replace the existing minimal scout line in block_text with the full section
        bt = entry.get("block_text", "")
        # Find and replace the old minimal scout line "**Scout context:** SELL alert on AUD_JPY"
        import re as _re
        bt = _re.sub(
            r"\*\*Scout context:\*\* [A-Z]+ alert on [A-Z_]+\n",
            scout_section,
            bt,
        )
        entry["block_text"] = bt
        print(f"[{trade_id}] {pair} {setup}: {history.get('trade_count', 0)} prior trades, "
              f"WR={history.get('win_rate')}%")
    Path(INDICATOR_JSON).write_text(json.dumps(data, indent=2, default=str))
    print(f"\nUpdated {INDICATOR_JSON} ({len(data)} entries)")


if __name__ == "__main__":
    main()
