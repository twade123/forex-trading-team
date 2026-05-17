"""Tier 1 setup catalog — live-performance updater.

Maintains the `Live 30d:` line inside the LIVE_PERF_START/END markers in
`Forex Trading Team/Skills/tier1_setup_catalog.md`. Called on trade close
(per-trade hook) and at migration time (one-time bulk run).

The catalog's `Backtest 90d:` lines are immutable — written by Tim from the
walk-forward backtest. Only the `Live 30d:` marker block is rewritten here.

Data source: `live_trades` filtered by entry_time within last 30 days, joined
against the originating Tier 1 alert. We try multiple match paths because the
production tagging may evolve:
  1. live_trades.setup_code == setup_name (cleanest)
  2. live_trades.setup == setup_name (current trading naming convention)
  3. live_trades.metadata.alert_type == setup_name (if scout writes it)
  4. Through metadata.finding_id → scout_alerts.alert_type
Any one of these matching counts as "from this Tier 1 setup."

If zero trades match, the Live 30d line shows "pending — no closed trades yet"
so the validator still sees the setup as known-via-backtest but flagged as
not-yet-validated-live.

Usage:
  python -m setup_perf_updater all              # rewrite all 7 setups
  python -m setup_perf_updater C3_RSI_DIV_GOLDEN  # rewrite one

Programmatic:
  from setup_perf_updater import update_catalog_perf
  update_catalog_perf("C9_BEAR_EXP_PULLBACK")
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(
    "<repo_root>/Skills/tier1_setup_catalog.md"
)
DB_PATH = Path("~/Jarvis/Database/v2/trading_forex.db")

TIER1_SETUPS = (
    "C1_STOCH_EXTREME_BB",
    "C3_RSI_DIV_GOLDEN",
    "C4_CHART_PATTERN_BREAK",
    "C5_FIB_REACTION",
    "C8_TRIANGLE_BREAKOUT",
    "C9_BEAR_EXP_PULLBACK",
    "C11_BIG_MOVE",
)

LIVE_WINDOW_DAYS = 30


def _fetch_setup_trades(setup_name: str, conn: sqlite3.Connection) -> List[Dict]:
    """Return live_trades rows from last 30 days matching this setup via any path.

    Match paths (any one wins):
      1. setup_code == setup_name
      2. setup == setup_name
      3. metadata.alert_type == setup_name
      4. finding_id → scout_alerts.alert_type == setup_name

    Returns rows with: id, pair, outcome, pnl_pips, pnl_usd, exit_time.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LIVE_WINDOW_DAYS)).isoformat()

    sql = """
        SELECT
            lt.id, lt.pair,
            COALESCE(lt.outcome, lt.result) AS outcome,
            COALESCE(lt.pnl_pips, lt.outcome_pips, lt.pips) AS pnl_pips,
            COALESCE(lt.pnl_usd, lt.outcome_usd) AS pnl_usd,
            lt.exit_time
        FROM live_trades lt
        LEFT JOIN scout_alerts sa
          ON sa.id = lt.finding_id
        WHERE lt.entry_time >= ?
          AND lt.exit_time IS NOT NULL
          AND COALESCE(lt.outcome, lt.result) IN ('win', 'loss')
          AND (
                lt.setup_code = ?
             OR lt.setup = ?
             OR sa.alert_type = ?
             OR json_extract(lt.metadata, '$.alert_type') = ?
          )
        ORDER BY lt.exit_time ASC
    """
    rows = conn.execute(
        sql, (cutoff, setup_name, setup_name, setup_name, setup_name)
    ).fetchall()
    return [
        {
            "id": r[0],
            "pair": r[1],
            "outcome": r[2],
            "pnl_pips": float(r[3] or 0),
            "pnl_usd": float(r[4] or 0),
            "exit_time": r[5],
        }
        for r in rows
    ]


def _compute_aggregate(trades: List[Dict]) -> Dict:
    """Aggregate stats from a list of trades. Returns:
    {n, wins, losses, wr_pct, net_pips, net_usd, streak}
    Streak is current run of consecutive same-outcome trades, sign-coded.
    """
    n = len(trades)
    if n == 0:
        return {
            "n": 0, "wins": 0, "losses": 0, "wr_pct": 0.0,
            "net_pips": 0.0, "net_usd": 0.0, "streak": 0,
        }

    wins = sum(1 for t in trades if t["outcome"] == "win")
    losses = sum(1 for t in trades if t["outcome"] == "loss")
    net_pips = sum(t["pnl_pips"] for t in trades)
    net_usd = sum(t["pnl_usd"] for t in trades)
    wr_pct = (wins / n * 100.0) if n else 0.0

    # Current streak — walk from most recent backwards
    sorted_trades = sorted(trades, key=lambda t: t["exit_time"] or "", reverse=True)
    streak = 0
    if sorted_trades:
        latest_outcome = sorted_trades[0]["outcome"]
        for t in sorted_trades:
            if t["outcome"] == latest_outcome:
                streak += 1
            else:
                break
        if latest_outcome == "loss":
            streak = -streak

    return {
        "n": n, "wins": wins, "losses": losses,
        "wr_pct": wr_pct, "net_pips": net_pips, "net_usd": net_usd,
        "streak": streak,
    }


def _format_live_line(stats: Dict) -> str:
    """Format the Live 30d markdown line."""
    if stats["n"] == 0:
        return "- Live 30d: pending — no closed trades yet"
    streak_sign = "+" if stats["streak"] > 0 else ""
    streak_str = f"{streak_sign}{stats['streak']}"
    return (
        f"- Live 30d: **{stats['wr_pct']:.1f}% WR** "
        f"({stats['wins']}W/{stats['losses']}L), "
        f"{'+' if stats['net_pips'] >= 0 else ''}{stats['net_pips']:.1f}p, "
        f"{'+' if stats['net_usd'] >= 0 else ''}${stats['net_usd']:.2f}, "
        f"streak {streak_str}"
    )


def _read_catalog() -> str:
    return CATALOG_PATH.read_text(encoding="utf-8")


def _atomic_write_catalog(content: str) -> None:
    """Atomic write so concurrent reads (agent registration) never see a partial file."""
    parent = CATALOG_PATH.parent
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".tier1_setup_catalog.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, CATALOG_PATH)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _replace_live_block(text: str, setup_name: str, new_line: str) -> str:
    """Replace content between LIVE_PERF_START:{name} and LIVE_PERF_END:{name} markers.
    If markers don't exist for this setup, raise — migration must add them first.
    """
    start_marker = f"<!-- LIVE_PERF_START:{setup_name} -->"
    end_marker = f"<!-- LIVE_PERF_END:{setup_name} -->"
    pattern = re.compile(
        re.escape(start_marker) + r"(.*?)" + re.escape(end_marker),
        re.DOTALL,
    )
    if not pattern.search(text):
        raise ValueError(
            f"LIVE_PERF markers for {setup_name} not found in catalog. "
            f"Run migration first to add them."
        )
    replacement = f"{start_marker}\n{new_line}\n{end_marker}"
    return pattern.sub(replacement, text)


def update_catalog_perf(setup_name: str) -> Dict:
    """Recompute and rewrite the Live 30d line for one setup. Returns the stats dict."""
    if setup_name not in TIER1_SETUPS:
        raise ValueError(
            f"{setup_name} is not a Tier 1 setup. "
            f"Tier 1 names: {', '.join(TIER1_SETUPS)}"
        )

    conn = sqlite3.connect(str(DB_PATH))
    try:
        trades = _fetch_setup_trades(setup_name, conn)
    finally:
        conn.close()

    stats = _compute_aggregate(trades)
    new_line = _format_live_line(stats)

    text = _read_catalog()
    new_text = _replace_live_block(text, setup_name, new_line)
    if new_text != text:
        _atomic_write_catalog(new_text)
        logger.info(
            "Tier1 catalog: updated Live 30d for %s — n=%d wr=%.1f%% pips=%+.1f usd=%+.2f",
            setup_name, stats["n"], stats["wr_pct"], stats["net_pips"], stats["net_usd"],
        )
    return stats


def update_all_catalog_perf() -> Dict[str, Dict]:
    """Refresh every Tier 1 setup. Returns {setup_name: stats}."""
    results: Dict[str, Dict] = {}
    for name in TIER1_SETUPS:
        try:
            results[name] = update_catalog_perf(name)
        except Exception as e:
            logger.error("Failed to update %s: %s", name, e)
            results[name] = {"error": str(e)}
    return results


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = sys.argv[1:]
    if not args or args[0] in ("--all", "all"):
        out = update_all_catalog_perf()
        for name, stats in out.items():
            print(f"{name}: {stats}")
    else:
        for name in args:
            stats = update_catalog_perf(name)
            print(f"{name}: {stats}")


if __name__ == "__main__":
    main()
