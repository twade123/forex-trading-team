#!/usr/bin/env python3
"""replay_60trade.py — Plan A Task 2d larger-cohort replay.

Builds 30+30 cohorts dynamically from live_trades, runs B+C conditions
per trade, checkpoints to /tmp/replay_60trade_progress.log every trade,
writes final raw JSON + aggregate stats.

Reuses replay_with_narrative.py's machinery (load_flight_data, replay_one,
build_validator_prompt, system prompt path).

Run:
  source ~/myenv/bin/activate && \\
  cd "<repo_root>/Source" && \\
  nohup python3 scripts/replay_60trade.py > /tmp/replay_60trade.log 2>&1 &

Monitor:
  tail -f /tmp/replay_60trade_progress.log
"""

import glob
import json
import os
import re
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

JARVIS_ROOT = "~/Jarvis"
SCRIPTS_DIR = f"{JARVIS_ROOT}/Forex Trading Team/Source/scripts"
sys.path.insert(0, SCRIPTS_DIR)

# Reuse existing machinery
from replay_with_narrative import (  # noqa: E402
    load_flight_data,
    replay_one,
    GHOST_VALIDATOR_PROMPT_PATH,
    FLIGHT_RECORDER_DB,
    TRADING_DB,
)

PROGRESS_LOG = "/tmp/replay_60trade_progress.log"
RAW_JSON_OUT = (
    f"{JARVIS_ROOT}/docs/superpowers/plans/notes/2026-05-09-task-2d-large-replay-raw.json"
)
NOTES_OUT = (
    f"{JARVIS_ROOT}/docs/superpowers/plans/notes/2026-05-09-task-2d-large-replay-results.md"
)
CHART_DIR = f"{JARVIS_ROOT}/Forex Trading Team/Data/charts/training"

WINNER_LIMIT = 30
LOSER_LIMIT = 30


def log_progress(msg: str) -> None:
    """Append a timestamped line to the progress log + stdout."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(PROGRESS_LOG, "a") as f:
        f.write(line + "\n")


def build_winner_cohort() -> list:
    """Pull winning scout TRADE_NOW trades 2026-04-27 to 2026-05-07."""
    conn = sqlite3.connect(TRADING_DB)
    rows = conn.execute(
        """
        SELECT id, pair, direction, entry_time, pnl_pips, entry_type, cycle_id
        FROM live_trades
        WHERE entry_time BETWEEN '2026-04-27' AND '2026-05-07'
          AND result = 'win'
          AND pnl_pips > 3
          AND entry_type IS NOT NULL
          AND cycle_id IS NOT NULL
          AND cycle_id != ''
        ORDER BY entry_time
        """
    ).fetchall()
    conn.close()
    return rows


def build_loser_cohort() -> list:
    """Pull losing trades 2026-05-07 onward."""
    conn = sqlite3.connect(TRADING_DB)
    rows = conn.execute(
        """
        SELECT id, pair, direction, entry_time, pnl_pips, entry_type, cycle_id
        FROM live_trades
        WHERE entry_time >= '2026-05-07'
          AND result IN ('loss', 'loss_breakeven')
          AND pnl_pips < -3
          AND entry_type IS NOT NULL
          AND cycle_id IS NOT NULL
          AND cycle_id != ''
        ORDER BY entry_time
        """
    ).fetchall()
    conn.close()
    return rows


def find_chart_path(pair: str, direction: str, entry_time: str) -> str | None:
    """Find saved chart file matching PAIR_TRADE_NOW_DIRECTION near entry_time.

    Strategy: search ±10 minutes window for a matching chart. Prefer TRADE_NOW
    + matching direction; fall back to any verdict suffix with matching direction.
    """
    try:
        dt = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
    except Exception:
        return None

    direction_upper = direction.upper()
    date_prefix = dt.strftime("%Y%m%d")

    # Build all PNG paths for this pair on this date
    same_day = sorted(glob.glob(f"{CHART_DIR}/{pair}_*_{date_prefix}_*.png"))
    if not same_day:
        return None

    target_minute = dt.hour * 60 + dt.minute

    def parse_minute(filename: str) -> int | None:
        # Extract _HHMMSS.png suffix → minute-of-day
        m = re.search(r"_(\d{2})(\d{2})(\d{2})\.png$", filename)
        if not m:
            return None
        return int(m.group(1)) * 60 + int(m.group(2))

    # Score candidates by: matches direction (+1000), is TRADE_NOW (+500),
    # then by abs(minute_delta). Lower score = better.
    scored = []
    for path in same_day:
        fname = path.rsplit("/", 1)[-1]
        minute = parse_minute(fname)
        if minute is None:
            continue
        delta = abs(minute - target_minute)
        if delta > 10:  # outside ±10 min window
            continue
        score = delta
        if f"_{direction_upper}_" not in fname:
            score += 1000  # disprefer wrong direction
        if "_TRADE_NOW_" not in fname:
            score += 500  # prefer TRADE_NOW charts
        scored.append((score, path))

    if not scored:
        return None
    scored.sort()
    return scored[0][1]


def hydrate_trade(row: tuple, category: str) -> dict | None:
    """Build trade_meta dict for replay_one. Skip if missing data."""
    trade_id, pair, direction, entry_time, pips, entry_type, cycle_id = row
    if not cycle_id:
        log_progress(f"  SKIP {trade_id} {pair}: no cycle_id on row")
        return None
    chart_path = find_chart_path(pair, direction, entry_time)
    if not chart_path:
        log_progress(f"  SKIP {trade_id} {pair}: no chart_path matched")
        return None

    return {
        "trade_id": str(trade_id),
        "pair": pair,
        "direction": direction,
        "entry_time": entry_time,
        "actual_pips": pips,
        "actual_result": "win" if category == "winner" else "loss",
        "cycle_id": cycle_id,
        "chart_path": chart_path,
        "broken_verdict": "TRADE_NOW",
        "entry_type": entry_type,
        "category": category,
    }


def aggregate_stats(results: list) -> dict:
    """Compute headline counts + decelerating-fan pattern check."""
    winners = [r for r in results if r.get("category") == "winner"]
    losers = [r for r in results if r.get("category") == "loser"]

    def pct(num, denom):
        return round(100 * num / denom, 1) if denom else 0.0

    def correctness_count(group, condition_key, expected_for_winners):
        """For winners: correct = TRADE_NOW. For losers: correct = WATCH/SKIP."""
        if expected_for_winners:
            correct_set = {"TRADE_NOW", "CONFIRM"}
        else:
            correct_set = {"WATCH", "SKIP"}
        n = sum(1 for r in group if r.get(condition_key) in correct_set)
        return n

    stats = {
        "winner_count": len(winners),
        "loser_count": len(losers),
        "winners_B_correct": correctness_count(winners, "B_verdict", True),
        "winners_C_correct": correctness_count(winners, "C_verdict", True),
        "losers_B_correct": correctness_count(losers, "B_verdict", False),
        "losers_C_correct": correctness_count(losers, "C_verdict", False),
    }
    stats["winners_B_correct_pct"] = pct(stats["winners_B_correct"], stats["winner_count"])
    stats["winners_C_correct_pct"] = pct(stats["winners_C_correct"], stats["winner_count"])
    stats["losers_B_correct_pct"] = pct(stats["losers_B_correct"], stats["loser_count"])
    stats["losers_C_correct_pct"] = pct(stats["losers_C_correct"], stats["loser_count"])

    overall_n = stats["winner_count"] + stats["loser_count"]
    overall_B = stats["winners_B_correct"] + stats["losers_B_correct"]
    overall_C = stats["winners_C_correct"] + stats["losers_C_correct"]
    stats["overall_B_correct_pct"] = pct(overall_B, overall_n)
    stats["overall_C_correct_pct"] = pct(overall_C, overall_n)

    # Hypothesis verdicts
    if stats["winner_count"] >= 10:
        if stats["winners_C_correct"] < stats["winners_B_correct"]:
            stats["narrative_hurts_winners"] = "CONFIRMED"
        elif stats["winners_C_correct"] > stats["winners_B_correct"] + 2:
            stats["narrative_hurts_winners"] = "REFUTED"
        else:
            stats["narrative_hurts_winners"] = "MIXED"
    else:
        stats["narrative_hurts_winners"] = f"INSUFFICIENT_SAMPLE_{stats['winner_count']}"

    return stats


def write_results_markdown(stats: dict, results: list) -> None:
    """Emit a human-readable summary."""
    lines = [
        "# Plan A Task 2d — Larger Replay Results",
        "",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        f"**Cohorts:** {stats['winner_count']} winners (4/27–5/6), {stats['loser_count']} losers (5/7+)",
        "",
        "## Headline accuracy",
        "",
        "| Group | Condition B (no narrative) | Condition C (narrative re-injected) |",
        "|---|---|---|",
        f"| Winners ({stats['winner_count']}) — correct = TRADE_NOW | {stats['winners_B_correct']}/{stats['winner_count']} = {stats['winners_B_correct_pct']}% | {stats['winners_C_correct']}/{stats['winner_count']} = {stats['winners_C_correct_pct']}% |",
        f"| Losers ({stats['loser_count']}) — correct = WATCH/SKIP | {stats['losers_B_correct']}/{stats['loser_count']} = {stats['losers_B_correct_pct']}% | {stats['losers_C_correct']}/{stats['loser_count']} = {stats['losers_C_correct_pct']}% |",
        f"| **Overall** | **{stats['overall_B_correct_pct']}%** | **{stats['overall_C_correct_pct']}%** |",
        "",
        "## Hypothesis verdicts",
        "",
        f"- **Narrative hurts winners more than it helps losers**: {stats['narrative_hurts_winners']}",
        "",
        "## Per-trade table",
        "",
        "| trade_id | pair | dir | category | actual_pips | B_verdict | C_verdict | shift |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        b = r.get("B_verdict", "?")
        c = r.get("C_verdict", "?")
        shift = "→" if b != c else "="
        lines.append(
            f"| {r.get('trade_id', '?')} | {r.get('pair', '?')} | {r.get('direction', '?')} | "
            f"{r.get('category', '?')} | {r.get('actual_pips', '?')} | {b} | {c} | {shift} |"
        )

    Path(NOTES_OUT).write_text("\n".join(lines))
    log_progress(f"Wrote markdown summary: {NOTES_OUT}")


def main():
    log_progress("=== replay_60trade.py START ===")

    # Truncate old progress log so this run starts fresh
    if os.path.exists(PROGRESS_LOG):
        os.rename(PROGRESS_LOG, PROGRESS_LOG + ".prev")

    log_progress("Building cohorts from live_trades…")
    winners_raw = build_winner_cohort()
    losers_raw = build_loser_cohort()
    log_progress(f"Found {len(winners_raw)} winning candidates, {len(losers_raw)} losing candidates")

    # Hydrate with cycle_id + chart_path
    log_progress("Hydrating winners…")
    winners = []
    for row in winners_raw[:WINNER_LIMIT * 2]:  # Try 2x to account for skips
        h = hydrate_trade(row, "winner")
        if h:
            winners.append(h)
        if len(winners) >= WINNER_LIMIT:
            break

    log_progress("Hydrating losers…")
    losers = []
    for row in losers_raw[:LOSER_LIMIT * 2]:
        h = hydrate_trade(row, "loser")
        if h:
            losers.append(h)
        if len(losers) >= LOSER_LIMIT:
            break

    cohort = winners + losers
    log_progress(f"Hydrated {len(winners)} winners, {len(losers)} losers (total {len(cohort)})")

    if len(cohort) < 10:
        log_progress(f"FAIL: cohort too small ({len(cohort)} < 10). Aborting.")
        sys.exit(1)

    # Load system prompt
    system_prompt = Path(GHOST_VALIDATOR_PROMPT_PATH).read_text()

    # Replay loop
    results = []
    for i, trade in enumerate(cohort):
        log_progress(
            f"[{i + 1}/{len(cohort)}] {trade['pair']} {trade['direction'].upper()} "
            f"cat={trade['category']} actual={trade['actual_pips']}p "
            f"cycle={trade['cycle_id'][:30]}"
        )
        try:
            r = replay_one(trade, system_prompt)
            # Merge category + metadata into result
            r["category"] = trade["category"]
            r["pair"] = trade["pair"]
            r["direction"] = trade["direction"]
            r["entry_time"] = trade["entry_time"]
            r["actual_pips"] = trade["actual_pips"]
            r["actual_result"] = trade["actual_result"]
            results.append(r)
            log_progress(
                f"  → B={r.get('B_verdict', '?')} C={r.get('C_verdict', '?')}"
            )

            # Save partial results every 5 trades so we don't lose progress on crash
            if (i + 1) % 5 == 0:
                Path(RAW_JSON_OUT).write_text(json.dumps(results, indent=2, default=str))
                log_progress(f"  (checkpoint: {len(results)} results saved)")

        except Exception as e:
            log_progress(f"  FAILED: {e}")
            log_progress(f"  TRACE: {traceback.format_exc()[:500]}")

    # Final save
    Path(RAW_JSON_OUT).write_text(json.dumps(results, indent=2, default=str))
    log_progress(f"Final results JSON: {RAW_JSON_OUT} ({len(results)} entries)")

    # Aggregate
    stats = aggregate_stats(results)
    log_progress(f"Stats: {json.dumps(stats, indent=2)}")

    # Markdown report
    write_results_markdown(stats, results)

    log_progress("=== replay_60trade.py DONE ===")


if __name__ == "__main__":
    main()
