#!/usr/bin/env python3
"""replay_60trade_oanda.py — Plan A Task 2d Large Cohort Replay (OANDA-regen charts).

Builds up to 25-winner + 25-loser cohort from live_trades. Regenerates M15 charts
at historical entry_time via OANDA candles (bypassing the sparse saved-PNG limitation).
Runs B (no narrative) and C (with narrative) conditions through the local 35B.
Writes checkpoint JSON every 5 trades. Emits final markdown + raw JSON reports.

Run in background:
  source ~/myenv/bin/activate && \\
  cd "<repo_root>/Source" && \\
  nohup python3 scripts/replay_60trade_oanda.py > /tmp/replay_60trade_oanda.log 2>&1 &
  echo "PID: $!"

Monitor:
  tail -f /tmp/replay_60trade_progress.log
"""

import json
import os
import shutil
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
JARVIS_ROOT = "~/Jarvis"
SOURCE_DIR = f"{JARVIS_ROOT}/Forex Trading Team/Source"
SCRIPTS_DIR = f"{SOURCE_DIR}/scripts"

sys.path.insert(0, SOURCE_DIR)
sys.path.insert(0, SCRIPTS_DIR)

FLIGHT_RECORDER_DB = f"{SOURCE_DIR}/flight_recorder.db"
TRADING_DB = f"{JARVIS_ROOT}/Database/v2/trading_forex.db"
GHOST_VALIDATOR_PROMPT_PATH = f"{JARVIS_ROOT}/Forex Trading Team/Prompts/ghost_validator_v1.md"

PROGRESS_LOG = "/tmp/replay_60trade_progress.log"
CHART_TEMP_DIR = "/tmp/replay_charts"
RAW_JSON_OUT = f"{JARVIS_ROOT}/docs/superpowers/plans/notes/2026-05-09-task-2d-large-replay-raw.json"
NOTES_OUT = f"{JARVIS_ROOT}/docs/superpowers/plans/notes/2026-05-09-task-2d-large-replay-results.md"

WINNER_TARGET = 25
LOSER_TARGET = 25

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs(CHART_TEMP_DIR, exist_ok=True)
os.makedirs(os.path.dirname(RAW_JSON_OUT), exist_ok=True)


def log_progress(msg: str) -> None:
    """Append timestamped line to progress log + stdout."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(PROGRESS_LOG, "a") as f:
        f.write(line + "\n")


# ── Cohort building ────────────────────────────────────────────────────────────

def build_winner_cohort() -> list:
    """Pull winning scout TRADE_NOW trades 2026-04-19 to 2026-05-07."""
    conn = sqlite3.connect(TRADING_DB)
    rows = conn.execute(
        """
        SELECT id, pair, direction, entry_time, pnl_pips, entry_type, cycle_id,
               fan_state, fan_direction, rsi, stoch_k, story_score,
               validator_verdict, indicators, market_picture, market_story
        FROM live_trades
        WHERE entry_time BETWEEN '2026-04-19' AND '2026-05-07'
          AND result = 'win'
          AND pnl_pips > 3
          AND cycle_id IS NOT NULL AND cycle_id != ''
        ORDER BY entry_time DESC
        """
    ).fetchall()
    conn.close()
    return rows


def build_loser_cohort() -> list:
    """Pull losing trades 2026-05-07 onward."""
    conn = sqlite3.connect(TRADING_DB)
    rows = conn.execute(
        """
        SELECT id, pair, direction, entry_time, pnl_pips, entry_type, cycle_id,
               fan_state, fan_direction, rsi, stoch_k, story_score,
               validator_verdict, indicators, market_picture, market_story
        FROM live_trades
        WHERE entry_time >= '2026-05-07'
          AND result IN ('loss', 'loss_breakeven')
          AND pnl_pips < -3
          AND cycle_id IS NOT NULL AND cycle_id != ''
        ORDER BY entry_time
        """
    ).fetchall()
    conn.close()
    return rows


# ── Flight data hydration ──────────────────────────────────────────────────────

def load_flight_stages(cycle_id: str) -> dict:
    """Pull ta_llm, ta_compute, validator_verdict stages from flight_recorder.db."""
    result = {}
    try:
        conn = sqlite3.connect(FLIGHT_RECORDER_DB, timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT stage, data FROM flight_log
            WHERE cycle_id = ?
              AND stage IN ('ta_llm', 'ta_compute', 'validator_verdict', 'data_intelligence')
            ORDER BY id
            """,
            (cycle_id,),
        ).fetchall()
        conn.close()
        for row in rows:
            try:
                result[row["stage"]] = json.loads(row["data"] or "{}")
            except json.JSONDecodeError:
                result[row["stage"]] = {}
    except Exception as e:
        log_progress(f"  flight_log load error for {cycle_id[:40]}: {e}")
    return result


# ── Trade hydration ────────────────────────────────────────────────────────────

def hydrate_trade(row: tuple, category: str) -> dict | None:
    """Convert a live_trades row into trade_meta for replay_one.

    Generates chart via OANDA regen instead of looking up saved files.
    Returns None if chart regen fails (trade is skipped).
    """
    (trade_id, pair, direction, entry_time, pips, entry_type, cycle_id,
     fan_state, fan_direction, rsi, stoch_k, story_score,
     validator_verdict, indicators, market_picture, market_story) = row

    if not cycle_id:
        log_progress(f"  SKIP {trade_id} {pair}: no cycle_id")
        return None

    # Regenerate chart from OANDA historical candles
    chart_path = os.path.join(CHART_TEMP_DIR, f"trade_{trade_id}_{pair}.png")

    from oanda_chart_regen import regenerate_chart_at

    # Rate limit: don't burst OANDA
    time.sleep(0.3)

    regen_result = regenerate_chart_at(pair, entry_time, chart_path)
    if not regen_result:
        log_progress(f"  SKIP {trade_id} {pair}: chart regen failed at {entry_time}")
        return None

    # Try to parse indicators JSON
    indicators_dict = {}
    try:
        if indicators:
            indicators_dict = json.loads(indicators) if isinstance(indicators, str) else indicators
    except Exception:
        pass

    return {
        "trade_id": str(trade_id),
        "pair": pair,
        "direction": direction or "unknown",
        "entry_time": entry_time,
        "actual_pips": pips,
        "actual_result": "win" if category == "winner" else "loss",
        "cycle_id": cycle_id,
        "chart_path": chart_path,
        "broken_verdict": validator_verdict or "TRADE_NOW",
        "entry_type": entry_type,
        "category": category,
        # Extra metadata for pattern analysis
        "fan_state": fan_state or "",
        "fan_direction": fan_direction or "",
        "rsi": rsi,
        "stoch_k": stoch_k,
        "story_score": story_score,
        "indicators": indicators_dict,
    }


# ── Prompt assembly ────────────────────────────────────────────────────────────

def build_indicator_section_from_meta(trade_meta: dict) -> str:
    """Build indicator section from live_trades columns + flight ta_compute."""
    pair = trade_meta["pair"]
    fan_state = trade_meta.get("fan_state", "unknown")
    fan_direction = trade_meta.get("fan_direction", "unknown")
    rsi = trade_meta.get("rsi", "unknown")
    stoch_k = trade_meta.get("stoch_k", "unknown")
    story_score = trade_meta.get("story_score", 0) or 0

    # Try to get richer data from flight ta_compute
    flight = trade_meta.get("_flight", {})
    ta_compute = flight.get("ta_compute", {})
    trend_health = ta_compute.get("trend_health", "unknown")
    reversal_risk = ta_compute.get("reversal_risk", "unknown")
    buy_score = ta_compute.get("buy_score", 0)
    sell_score = ta_compute.get("sell_score", 0)

    return (
        f"## Indicator Data: {pair} M15\n"
        f"fan_state: {fan_state}\n"
        f"fan_direction: {fan_direction}\n"
        f"rsi: {rsi}\n"
        f"stoch_k: {stoch_k}\n"
        f"story_score: {story_score}\n"
        f"trend_health: {trend_health}\n"
        f"reversal_risk: {reversal_risk}\n"
        f"buy_score: {buy_score}\n"
        f"sell_score: {sell_score}\n"
    )


# ── Replay logic ───────────────────────────────────────────────────────────────
# Reuse replay machinery from replay_with_narrative.py

def replay_one_oanda(trade_meta: dict, system_prompt: str) -> dict:
    """Run conditions B and C for one trade using the regen'd chart.

    Delegates to replay_with_narrative.replay_one but patches chart_path
    in trade_meta to point at the OANDA-regen'd file.
    """
    from replay_with_narrative import (
        build_task_B,
        build_task_C,
        call_local_35b,
        load_chart_as_base64,
        load_flight_data,
        parse_verdict,
    )

    pair = trade_meta["pair"]
    cycle_id = trade_meta["cycle_id"]
    chart_path = trade_meta["chart_path"]

    log_progress(
        f"  [{trade_meta['trade_id']}] {pair} {trade_meta['direction'].upper()} "
        f"entry={trade_meta['entry_time']} ({trade_meta['actual_pips']}p) "
        f"cat={trade_meta['category']}"
    )

    # Load flight data for narrative
    flight = load_flight_stages(cycle_id)
    trade_meta["_flight"] = flight
    ta_llm = flight.get("ta_llm", {})
    ta_compute = flight.get("ta_compute", {})
    ta_narrative = ta_llm.get("narrative", "")
    ta_clarity = ta_llm.get("clarity", "UNKNOWN")

    log_progress(
        f"    TA narrative: {'PRESENT (' + str(len(ta_narrative)) + ' chars)' if ta_narrative else 'EMPTY'} "
        f"clarity={ta_clarity}"
    )

    # Load regen'd chart
    chart_b64, chart_media = load_chart_as_base64(chart_path)
    if not chart_b64:
        log_progress(f"    WARNING: chart at {chart_path} unreadable")

    # Build indicator section using live_trades + flight data
    indicator_section = build_indicator_section_from_meta(trade_meta)

    # story_score: prefer live_trades column, fall back to ta_compute scores
    story_score = trade_meta.get("story_score", 0) or max(
        ta_compute.get("buy_score", 0),
        ta_compute.get("sell_score", 0),
    )

    result = {
        "trade_id": trade_meta["trade_id"],
        "pair": pair,
        "direction": trade_meta["direction"],
        "entry_time": trade_meta["entry_time"],
        "actual_pips": trade_meta["actual_pips"],
        "actual_result": trade_meta["actual_result"],
        "category": trade_meta["category"],
        "fan_state": trade_meta.get("fan_state", ""),
        "fan_direction": trade_meta.get("fan_direction", ""),
        "rsi": trade_meta.get("rsi"),
        "stoch_k": trade_meta.get("stoch_k"),
        "story_score": story_score,
        "ta_narrative_present": bool(ta_narrative),
        "ta_clarity": ta_clarity,
        "broken_verdict": trade_meta["broken_verdict"],
        "B_verdict": None,
        "B_direction": None,
        "B_confidence": None,
        "B_reasoning": "",
        "C_verdict": None,
        "C_direction": None,
        "C_confidence": None,
        "C_reasoning": "",
        "B_error": None,
        "C_error": None,
        "chart_size_kb": os.path.getsize(chart_path) // 1024 if os.path.exists(chart_path) else 0,
    }

    # ── Condition B: Without narrative ──────────────────────────────────────
    log_progress(f"    Running B (no narrative)...")
    try:
        task_B = build_task_B(pair, story_score, indicator_section)
        raw_B = call_local_35b(system_prompt, task_B, chart_b64, chart_media)
        parsed_B = parse_verdict(raw_B)
        result.update({
            "B_verdict": parsed_B["verdict"],
            "B_direction": parsed_B["direction"],
            "B_confidence": parsed_B["confidence"],
            "B_reasoning": parsed_B["reasoning"],
        })
        log_progress(f"    B → {parsed_B['verdict']} {parsed_B['direction']} conf={parsed_B['confidence']}")
    except Exception as e:
        result["B_error"] = str(e)
        log_progress(f"    B ERROR: {e}")

    # ── Condition C: With narrative ──────────────────────────────────────────
    log_progress(f"    Running C (with narrative)...")
    try:
        task_C = build_task_C(pair, story_score, indicator_section, ta_narrative, ta_llm)
        raw_C = call_local_35b(system_prompt, task_C, chart_b64, chart_media)
        parsed_C = parse_verdict(raw_C)
        result.update({
            "C_verdict": parsed_C["verdict"],
            "C_direction": parsed_C["direction"],
            "C_confidence": parsed_C["confidence"],
            "C_reasoning": parsed_C["reasoning"],
        })
        log_progress(f"    C → {parsed_C['verdict']} {parsed_C['direction']} conf={parsed_C['confidence']}")
    except Exception as e:
        result["C_error"] = str(e)
        log_progress(f"    C ERROR: {e}")

    return result


# ── Statistics + reporting ─────────────────────────────────────────────────────

def aggregate_stats(results: list) -> dict:
    """Compute headline accuracy + hypothesis verdicts."""
    winners = [r for r in results if r.get("category") == "winner"]
    losers = [r for r in results if r.get("category") == "loser"]

    def pct(num, denom):
        return round(100 * num / denom, 1) if denom else 0.0

    def correct_count(group, key, is_winner):
        correct_set = {"TRADE_NOW", "CONFIRM"} if is_winner else {"WATCH", "SKIP"}
        return sum(1 for r in group if r.get(key) in correct_set)

    stats = {
        "winner_count": len(winners),
        "loser_count": len(losers),
        "total": len(results),
        "winners_B_correct": correct_count(winners, "B_verdict", True),
        "winners_C_correct": correct_count(winners, "C_verdict", True),
        "losers_B_correct": correct_count(losers, "B_verdict", False),
        "losers_C_correct": correct_count(losers, "C_verdict", False),
    }

    for key in ("winners_B", "winners_C", "losers_B", "losers_C"):
        cat, cond = key.split("_")
        n = stats[f"{'winner' if cat == 'winners' else 'loser'}_count"]
        stats[f"{key}_correct_pct"] = pct(stats[f"{key}_correct"], n)

    total = stats["total"]
    overall_B = stats["winners_B_correct"] + stats["losers_B_correct"]
    overall_C = stats["winners_C_correct"] + stats["losers_C_correct"]
    stats["overall_B_correct_pct"] = pct(overall_B, total)
    stats["overall_C_correct_pct"] = pct(overall_C, total)

    # Narrative-hurts-winners hypothesis
    wn = stats["winner_count"]
    if wn >= 10:
        if stats["winners_C_correct"] < stats["winners_B_correct"]:
            stats["narrative_hurts_winners"] = "CONFIRMED"
        elif stats["winners_C_correct"] > stats["winners_B_correct"] + 2:
            stats["narrative_hurts_winners"] = "REFUTED"
        else:
            stats["narrative_hurts_winners"] = "MIXED"
    else:
        stats["narrative_hurts_winners"] = f"INSUFFICIENT_SAMPLE_{wn}"

    # Narrative-helps-losers hypothesis
    ln = stats["loser_count"]
    if ln >= 5:
        if stats["losers_C_correct"] > stats["losers_B_correct"]:
            stats["narrative_helps_losers"] = "CONFIRMED"
        else:
            stats["narrative_helps_losers"] = "REFUTED"
    else:
        stats["narrative_helps_losers"] = f"INSUFFICIENT_SAMPLE_{ln}"

    # Decelerating-fan breakdown
    decel_winners = [r for r in winners if r.get("fan_state", "").lower() == "decelerating"]
    decel_B = correct_count(decel_winners, "B_verdict", True)
    stats["decel_fan_winners"] = len(decel_winners)
    stats["decel_fan_B_correct"] = decel_B
    stats["decel_fan_B_correct_pct"] = pct(decel_B, len(decel_winners))

    return stats


def write_results_markdown(stats: dict, results: list) -> None:
    """Emit human-readable summary markdown."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "# Plan A Task 2d — Large Replay Results (OANDA-regen charts)",
        "",
        f"**Date:** {now}",
        f"**Method:** M15 charts regenerated from OANDA historical candles (not saved PNGs)",
        f"**Cohorts:** {stats['winner_count']} winners (4/19–5/6), {stats['loser_count']} losers (5/7+)",
        f"**Total completed:** {stats['total']}",
        "",
        "## Headline accuracy",
        "",
        "| Group | Condition B (no narrative) | Condition C (narrative re-injected) |",
        "|---|---|---|",
        f"| Winners ({stats['winner_count']}) — correct = TRADE_NOW/CONFIRM | "
        f"{stats['winners_B_correct']}/{stats['winner_count']} = {stats['winners_B_correct_pct']}% | "
        f"{stats['winners_C_correct']}/{stats['winner_count']} = {stats['winners_C_correct_pct']}% |",
        f"| Losers ({stats['loser_count']}) — correct = WATCH/SKIP | "
        f"{stats['losers_B_correct']}/{stats['loser_count']} = {stats['losers_B_correct_pct']}% | "
        f"{stats['losers_C_correct']}/{stats['loser_count']} = {stats['losers_C_correct_pct']}% |",
        f"| **Overall** | **{stats['overall_B_correct_pct']}%** | **{stats['overall_C_correct_pct']}%** |",
        "",
        "## Hypothesis verdicts",
        "",
        f"- **Narrative hurts winners (B > C for winners):** {stats['narrative_hurts_winners']}",
        f"- **Narrative helps losers (C > B for losers):** {stats['narrative_helps_losers']}",
        f"- **Decelerating-fan winners misread:** {stats['decel_fan_B_correct']}/{stats['decel_fan_winners']} correct on B ({stats['decel_fan_B_correct_pct']}%)",
        "",
        "## Per-trade table",
        "",
        "| trade_id | pair | dir | cat | pips | fan_state | rsi | B_verdict | C_verdict | shift | narrative |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    for r in results:
        b = r.get("B_verdict", "?")
        c = r.get("C_verdict", "?")
        shift = "→" if b != c else "="
        narr = "Y" if r.get("ta_narrative_present") else "N"
        rsi_val = f"{r.get('rsi', '?'):.1f}" if r.get("rsi") is not None else "?"
        lines.append(
            f"| {r.get('trade_id','?')} | {r.get('pair','?')} | {r.get('direction','?')} | "
            f"{r.get('category','?')} | {r.get('actual_pips','?')} | {r.get('fan_state','?')} | "
            f"{rsi_val} | {b} | {c} | {shift} | {narr} |"
        )

    Path(NOTES_OUT).write_text("\n".join(lines))
    log_progress(f"Wrote markdown: {NOTES_OUT}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import urllib.request

    # Rotate old progress log
    if os.path.exists(PROGRESS_LOG):
        os.rename(PROGRESS_LOG, PROGRESS_LOG + ".prev")

    log_progress("=== replay_60trade_oanda.py START (Task 2d) ===")
    log_progress(f"Winner target: {WINNER_TARGET}, Loser target: {LOSER_TARGET}")

    # Verify 35B model is reachable
    try:
        url = "http://localhost:11502/v1/models"
        with urllib.request.urlopen(url, timeout=5) as resp:
            models_data = json.loads(resp.read())
            model_ids = [m["id"] for m in models_data.get("data", [])]
            log_progress(f"Model server OK: {model_ids}")
    except Exception as e:
        log_progress(f"BLOCKED: Cannot reach 35B at localhost:11502: {e}")
        sys.exit(1)

    # Load system prompt
    system_prompt = Path(GHOST_VALIDATOR_PROMPT_PATH).read_text()
    log_progress(f"System prompt loaded ({len(system_prompt)} chars)")

    # Build raw cohorts from DB
    log_progress("Building cohorts from live_trades...")
    winners_raw = build_winner_cohort()
    losers_raw = build_loser_cohort()
    log_progress(f"Raw candidates: {len(winners_raw)} winners, {len(losers_raw)} losers")

    # Smoke-test chart regen on 1 winner before committing to the full run
    log_progress("Smoke-testing chart regen on first winner candidate...")
    if winners_raw:
        first_row = winners_raw[0]
        (tid, pair, direction, entry_time, pips, etype, cycle_id,
         fan_state, fan_dir, rsi, stochk, sscore, vverdict, inds, mpic, mstory) = first_row
        test_path = f"/tmp/replay_charts/smoke_{tid}_{pair}.png"
        from oanda_chart_regen import regenerate_chart_at
        smoke_ok = regenerate_chart_at(pair, entry_time, test_path)
        if smoke_ok:
            size_kb = os.path.getsize(test_path) // 1024
            log_progress(f"Smoke test PASS: {pair} {entry_time} → {size_kb}KB")
        else:
            log_progress("Smoke test FAIL: chart regen returned None — aborting")
            sys.exit(1)

    # Hydrate winners
    log_progress("Hydrating winners with OANDA chart regen...")
    winners = []
    for row in winners_raw:
        if len(winners) >= WINNER_TARGET:
            break
        h = hydrate_trade(row, "winner")
        if h:
            winners.append(h)
            log_progress(f"  Winner hydrated: {h['trade_id']} {h['pair']} {h['actual_pips']}p (chart: {h['chart_path'].split('/')[-1]})")
        else:
            pass  # hydrate_trade already logged skip reason

    log_progress(f"Winners hydrated: {len(winners)}/{WINNER_TARGET}")

    # Hydrate losers
    log_progress("Hydrating losers with OANDA chart regen...")
    losers = []
    for row in losers_raw:
        if len(losers) >= LOSER_TARGET:
            break
        h = hydrate_trade(row, "loser")
        if h:
            losers.append(h)
            log_progress(f"  Loser hydrated: {h['trade_id']} {h['pair']} {h['actual_pips']}p")

    log_progress(f"Losers hydrated: {len(losers)}/{LOSER_TARGET}")

    cohort = winners + losers
    log_progress(f"Total cohort: {len(cohort)} trades")

    if len(cohort) < 5:
        log_progress(f"FAIL: cohort too small ({len(cohort)}). Aborting.")
        sys.exit(1)

    # Replay loop
    results = []
    for i, trade in enumerate(cohort):
        log_progress(
            f"\n[{i+1}/{len(cohort)}] {trade['pair']} {trade['direction'].upper()} "
            f"id={trade['trade_id']} cat={trade['category']}"
        )
        try:
            r = replay_one_oanda(trade, system_prompt)
            results.append(r)
            log_progress(f"  DONE → B={r.get('B_verdict','?')} C={r.get('C_verdict','?')}")
        except Exception as e:
            log_progress(f"  FAILED: {e}")
            log_progress(f"  TRACE: {traceback.format_exc()[:500]}")
            # Append partial failure record
            results.append({
                "trade_id": trade["trade_id"],
                "pair": trade["pair"],
                "direction": trade["direction"],
                "entry_time": trade["entry_time"],
                "actual_pips": trade["actual_pips"],
                "actual_result": trade["actual_result"],
                "category": trade["category"],
                "fan_state": trade.get("fan_state", ""),
                "B_verdict": None, "C_verdict": None,
                "B_error": str(e), "C_error": str(e),
                "ta_narrative_present": False,
                "chart_size_kb": 0,
            })

        # Checkpoint every 5 trades
        if (i + 1) % 5 == 0:
            Path(RAW_JSON_OUT).write_text(json.dumps(results, indent=2, default=str))
            log_progress(f"  (checkpoint: {len(results)} results saved to {RAW_JSON_OUT})")

    # Final save
    Path(RAW_JSON_OUT).write_text(json.dumps(results, indent=2, default=str))
    log_progress(f"Final raw JSON: {RAW_JSON_OUT} ({len(results)} entries)")

    # Aggregate + report
    stats = aggregate_stats(results)
    log_progress(f"Stats: {json.dumps(stats)}")
    write_results_markdown(stats, results)

    log_progress("=== replay_60trade_oanda.py DONE ===")


if __name__ == "__main__":
    main()
