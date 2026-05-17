"""
replay_winners_with_narrative.py — Plan A Task 2b Ghost Replay (Inverse Experiment)
=====================================================================================
Tests whether the current (post-bypass) validator pipeline still recognizes
WINNING scout trades from the 2026-04-29 to 2026-05-06 80%+ window.

Conditions:
  B - Without narrative (matches current live pipeline — local 35B gets only indicators + scout)
  C - With narrative (saved ta_llm narrative re-injected into the prompt)

Historical verdict: All 8 trades below were CONFIRM (TRADE_NOW) and resulted in wins.

Goal: Determine if the current pipeline (B) still calls them TRADE_NOW, and whether
narrative re-injection (C) helps if B misses.

Calls the local 35B directly via OpenAI-compatible API at localhost:11502,
using the ghost_validator_v1.md system prompt — the same path as live trading.

Usage:
    source ~/myenv/bin/activate && \\
    cd "<repo_root>/Source" && \\
    python3 scripts/replay_winners_with_narrative.py
"""

import json
import re
import sqlite3
import sys
import os
import base64
from pathlib import Path
from datetime import datetime, timezone

# ── Paths ─────────────────────────────────────────────────────────────────────
JARVIS_ROOT = "~/Jarvis"
FLIGHT_RECORDER_DB = "<repo_root>/Source/flight_recorder.db"
TRADING_DB = "~/Jarvis/Database/v2/trading_forex.db"
GHOST_VALIDATOR_PROMPT_PATH = "<repo_root>/Prompts/ghost_validator_v1.md"
LOCAL_MODEL_PORT = 11502
LOCAL_MODEL_NAME = "mlx-community/Qwen3.5-35B-A3B-4bit"
CHARTS_DIR = "~/jarvis/Forex Trading Team/Data/charts/training"

# ── Cohort: 8 winning scout TRADE_NOW trades from 4/30-5/6 (4/29 had no scout wins)
# All resulted in wins. Historical validator verdict was CONFIRM for all.
# Note: live_trades.validator_verdict is NULL for scout-originated trades in this era
# (stored in flight_log validator_verdict stage as "CONFIRM"), but they all triggered
# execution, confirming the validator approved them as TRADE_NOW.
COHORT = [
    {
        "trade_id": "13310",
        "pair": "AUD_JPY",
        "direction": "sell",
        "entry_time": "2026-04-30T09:49:57",
        "actual_pips": 71.9,
        "actual_result": "win",
        "cycle_id": "cycle_1_2026-04-30T09:46:00.882892+00:00",
        "chart_path": f"{CHARTS_DIR}/AUD_JPY_TRADE_NOW_SELL_20260430_094957.png",
        "historical_verdict": "CONFIRM",  # from flight_log validator_verdict stage
        "historical_direction": "SELL",
    },
    {
        "trade_id": "13362",
        "pair": "AUD_JPY",
        "direction": "sell",
        "entry_time": "2026-04-30T10:50:05",
        "actual_pips": 8.2,
        "actual_result": "win",
        "cycle_id": "cycle_1_2026-04-30T10:46:00.634272+00:00",
        "chart_path": f"{CHARTS_DIR}/AUD_JPY_TRADE_NOW_SELL_20260430_105005.png",
        "historical_verdict": "CONFIRM",
        "historical_direction": "SELL",
    },
    {
        "trade_id": "13396",
        "pair": "EUR_CHF",
        "direction": "sell",
        "entry_time": "2026-04-30T13:48:54",
        "actual_pips": 17.9,
        "actual_result": "win",
        "cycle_id": "cycle_1_2026-04-30T13:44:28.232856+00:00",
        "chart_path": f"{CHARTS_DIR}/EUR_CHF_TRADE_NOW_SELL_20260430_134853.png",
        "historical_verdict": "CONFIRM",
        "historical_direction": "SELL",
    },
    {
        "trade_id": "13424",
        "pair": "USD_CAD",
        "direction": "sell",
        "entry_time": "2026-04-30T15:45:49",
        "actual_pips": 4.1,
        "actual_result": "win",
        "cycle_id": "cycle_1_2026-04-30T15:42:21.223204+00:00",
        "chart_path": f"{CHARTS_DIR}/USD_CAD_TRADE_NOW_SELL_20260430_154548.png",
        "historical_verdict": "CONFIRM",
        "historical_direction": "SELL",
    },
    {
        "trade_id": "13452",
        "pair": "EUR_AUD",
        "direction": "sell",
        "entry_time": "2026-05-01T16:34:10",
        "actual_pips": 7.1,
        "actual_result": "win",
        "cycle_id": "cycle_1_2026-05-01T16:31:02.326229+00:00",
        "chart_path": f"{CHARTS_DIR}/EUR_AUD_TRADE_NOW_SELL_20260501_163410.png",
        "historical_verdict": "CONFIRM",
        "historical_direction": "SELL",
    },
    {
        "trade_id": "13578",
        "pair": "AUD_USD",
        "direction": "sell",
        "entry_time": "2026-05-04T16:51:45",
        "actual_pips": 3.5,
        "actual_result": "win",
        "cycle_id": "cycle_1_2026-05-04T16:46:03.283998+00:00",
        "chart_path": f"{CHARTS_DIR}/AUD_USD_TRADE_NOW_SELL_20260504_165144.png",
        "historical_verdict": "CONFIRM",
        "historical_direction": "SELL",
    },
    {
        "trade_id": "13621",
        "pair": "GBP_USD",
        "direction": "buy",
        "entry_time": "2026-05-05T23:51:09",
        "actual_pips": 6.2,
        "actual_result": "win",
        "cycle_id": "cycle_1_2026-05-05T23:46:22.051903+00:00",
        "chart_path": f"{CHARTS_DIR}/GBP_USD_TRADE_NOW_BUY_20260505_235109.png",
        "historical_verdict": "CONFIRM",
        "historical_direction": "BUY",
    },
    {
        "trade_id": "13665",
        "pair": "USD_CAD",
        "direction": "sell",
        "entry_time": "2026-05-06T02:09:42",
        "actual_pips": 4.6,
        "actual_result": "win",
        "cycle_id": "cycle_1_2026-05-06T02:05:15.105459+00:00",
        "chart_path": f"{CHARTS_DIR}/USD_CAD_TRADE_NOW_SELL_20260506_020941.png",
        "historical_verdict": "CONFIRM",
        "historical_direction": "SELL",
    },
]


def load_ghost_validator_prompt() -> str:
    """Load the ghost_validator_v1.md system prompt."""
    prompt_path = Path(GHOST_VALIDATOR_PROMPT_PATH)
    if not prompt_path.exists():
        raise FileNotFoundError(f"ghost_validator_v1.md not found at {prompt_path}")
    return prompt_path.read_text()


def load_flight_data(cycle_id: str) -> dict:
    """Pull ta_llm + ta_compute data for a cycle from flight_recorder.db."""
    conn = sqlite3.connect(FLIGHT_RECORDER_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT stage, data FROM flight_log WHERE cycle_id = ? AND stage IN ('ta_llm', 'ta_compute') ORDER BY id",
            (cycle_id,),
        ).fetchall()

        result = {}
        for row in rows:
            try:
                result[row["stage"]] = json.loads(row["data"] or "{}")
            except json.JSONDecodeError:
                result[row["stage"]] = {}
        return result
    finally:
        conn.close()


def build_indicator_section(ta_compute: dict, pair: str) -> str:
    """Build indicator section text for the prompt (matches live pipeline format)."""
    fan_state = ta_compute.get("fan_state", "unknown")
    fan_direction = ta_compute.get("fan_direction", "unknown")
    trend_health = ta_compute.get("trend_health", 0)
    reversal_risk = ta_compute.get("reversal_risk", "unknown")
    buy_score = ta_compute.get("buy_score", 0)
    sell_score = ta_compute.get("sell_score", 0)

    return (
        f"## Indicator Data: {pair} M15\n"
        f"fan_state: {fan_state}\n"
        f"fan_direction: {fan_direction}\n"
        f"trend_health: {trend_health}\n"
        f"reversal_risk: {reversal_risk}\n"
        f"buy_score: {buy_score}\n"
        f"sell_score: {sell_score}\n"
    )


def build_task_B(pair: str, story_score: int, indicator_section: str) -> str:
    """Build Condition B task: no narrative (current pipeline behavior)."""
    pair_display = pair.replace("_", "/")
    preamble = (
        f"M15 chart — {pair_display}. Read it fresh and form YOUR OWN "
        f"thesis from the structure you see (story_score={story_score} "
        f"is informational only, not a directive).\n\n"
        f"Return ONLY a ```json code block with: verdict (TRADE_NOW/WATCH/SKIP), "
        f"direction (BUY/SELL), confidence (0-10), reasoning (start with CHART READ:), "
        f"re_entry_conditions (list of {{field, op, value, reason}} dicts), "
        f"re_entry_direction, re_entry_setup, watch_trigger (SPECIFIC prices: "
        f"entry zone, invalidation, target), watch_for, snipe_entry_zone, "
        f"snipe_invalidation, snipe_target, estimated_candles_to_entry, "
        f"price_target_entry, watch_manifest (MANDATORY for WATCH).\n\n"
    )
    return (
        preamble
        + indicator_section
        + "\n---\n"
        "After analyzing the chart, respond with ONLY a ```json code block. "
        "No prose outside the JSON."
    )


def build_task_C(pair: str, story_score: int, indicator_section: str, ta_narrative: str, ta_llm: dict) -> str:
    """Build Condition C task: narrative re-injected."""
    pair_display = pair.replace("_", "/")
    preamble = (
        f"M15 chart — {pair_display}. Read it fresh and form YOUR OWN "
        f"thesis from the structure you see (story_score={story_score} "
        f"is informational only, not a directive).\n\n"
        f"Return ONLY a ```json code block with: verdict (TRADE_NOW/WATCH/SKIP), "
        f"direction (BUY/SELL), confidence (0-10), reasoning (start with CHART READ:), "
        f"re_entry_conditions (list of {{field, op, value, reason}} dicts), "
        f"re_entry_direction, re_entry_setup, watch_trigger (SPECIFIC prices: "
        f"entry zone, invalidation, target), watch_for, snipe_entry_zone, "
        f"snipe_invalidation, snipe_target, estimated_candles_to_entry, "
        f"price_target_entry, watch_manifest (MANDATORY for WATCH).\n\n"
    )

    clarity = ta_llm.get("clarity", "UNKNOWN")
    ema_state = ta_llm.get("ema_state", "")
    bb_state = ta_llm.get("bb_state", "")
    rsi_state = ta_llm.get("rsi_state", "")
    cascade_phase = ta_llm.get("cascade_phase", "")
    candle_tests = ta_llm.get("candle_tests", "")
    retracement_status = ta_llm.get("retracement_status", "")
    conflicting_signals = ta_llm.get("conflicting_signals", [])

    ta_parts = [f"## TA Narrative (from TA agent)\n**Clarity:** {clarity}"]
    if ta_narrative:
        ta_parts.append(f"**Summary:** {ta_narrative}")
    if ema_state:
        ta_parts.append(f"**EMA State:** {ema_state}")
    if bb_state:
        ta_parts.append(f"**BB State:** {bb_state}")
    if rsi_state:
        ta_parts.append(f"**RSI/Momentum:** {rsi_state}")
    if cascade_phase:
        ta_parts.append(f"**Cascade Phase:** {cascade_phase}")
    if candle_tests:
        ta_parts.append(f"**Candle Tests:** {candle_tests}")
    if retracement_status:
        ta_parts.append(f"**Retracement:** {retracement_status}")
    if conflicting_signals:
        ta_parts.append(f"**Conflicting Signals:** {', '.join(conflicting_signals)}")

    ta_section = "\n".join(ta_parts)

    return (
        preamble
        + indicator_section
        + "\n\n"
        + ta_section
        + "\n---\n"
        "After analyzing the chart, respond with ONLY a ```json code block. "
        "No prose outside the JSON."
    )


def load_chart_as_base64(chart_path: str) -> tuple:
    """Load chart PNG and return (base64_str, media_type)."""
    path = Path(chart_path)
    if not path.exists():
        return None, None
    raw = path.read_bytes()
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        media_type = "image/png"
    elif raw[:3] == b'\xff\xd8\xff':
        media_type = "image/jpeg"
    else:
        media_type = "image/png"
    return base64.b64encode(raw).decode("utf-8"), media_type


def call_local_35b(system_prompt: str, task_text: str, chart_b64: str, chart_media_type: str) -> str:
    """Call the local 35B model via OpenAI-compatible API."""
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai package not installed — run: pip install openai")

    client = OpenAI(base_url=f"http://localhost:{LOCAL_MODEL_PORT}", api_key="mlx-local")

    content = []
    if chart_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{chart_media_type};base64,{chart_b64}"},
        })
    content.append({"type": "text", "text": task_text})

    response = client.chat.completions.create(
        model=LOCAL_MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        max_tokens=2500,
        temperature=0,
        timeout=300,
    )
    raw = response.choices[0].message.content or ""
    # Strip Qwen3 thinking tags if present
    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
    return raw


def parse_verdict(response_text: str) -> dict:
    """Extract verdict, direction, confidence, and first 300 chars of reasoning from JSON response."""
    default = {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": ""}

    cleaned = response_text.strip()
    json_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned)
    if json_match:
        json_str = json_match.group(1)
    else:
        brace_start = cleaned.find("{")
        if brace_start == -1:
            return default
        json_str = cleaned[brace_start:]
        depth = 0
        end_idx = -1
        for i, ch in enumerate(json_str):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break
        if end_idx > 0:
            json_str = json_str[:end_idx]
        else:
            return default

    try:
        data = json.loads(json_str)
        return {
            "verdict": data.get("verdict", "UNKNOWN"),
            "direction": data.get("direction"),
            "confidence": data.get("confidence"),
            "reasoning": str(data.get("reasoning", ""))[:300],
        }
    except json.JSONDecodeError:
        return default


def replay_one(trade_meta: dict, system_prompt: str) -> dict:
    """Run conditions B and C for one winning trade. Returns result dict."""
    pair = trade_meta["pair"]
    cycle_id = trade_meta["cycle_id"]
    chart_path = trade_meta["chart_path"]

    print(f"\n  [{trade_meta['trade_id']}] {pair} {trade_meta['direction'].upper()} "
          f"entry={trade_meta['entry_time']} (+{trade_meta['actual_pips']}p WIN)")

    # Load flight data
    flight = load_flight_data(cycle_id)
    ta_llm = flight.get("ta_llm", {})
    ta_compute = flight.get("ta_compute", {})
    ta_narrative = ta_llm.get("narrative", "")
    ta_clarity = ta_llm.get("clarity", "UNKNOWN")

    if ta_narrative:
        print(f"    TA narrative: PRESENT ({len(ta_narrative)} chars, clarity={ta_clarity})")
    else:
        print(f"    TA narrative: EMPTY (clarity={ta_clarity})")

    # Load chart
    chart_b64, chart_media = load_chart_as_base64(chart_path)
    if not chart_b64:
        print(f"    WARNING: Chart not found at {chart_path}")

    # Build indicator section
    indicator_section = build_indicator_section(ta_compute, pair)

    # story_score proxy
    buy_score = ta_compute.get("buy_score", 0)
    sell_score = ta_compute.get("sell_score", 0)
    story_score = max(buy_score, sell_score)

    result = {
        "trade_id": trade_meta["trade_id"],
        "pair": pair,
        "direction": trade_meta["direction"],
        "entry_time": trade_meta["entry_time"],
        "actual_pips": trade_meta["actual_pips"],
        "actual_result": trade_meta["actual_result"],
        "ta_narrative_present": bool(ta_narrative),
        "ta_clarity": ta_clarity,
        "historical_verdict": trade_meta["historical_verdict"],
        "historical_direction": trade_meta["historical_direction"],
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
        # Computed after both runs
        "B_preserved_win": False,     # B == TRADE_NOW (current pipeline still right)
        "C_preserved_win": False,     # C == TRADE_NOW
        "C_restored_win": False,      # B != TRADE_NOW but C == TRADE_NOW (narrative helps)
    }

    # ── Condition B: Without narrative ──
    print(f"    Running B (no narrative)...", end="", flush=True)
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
        print(f" {parsed_B['verdict']} {parsed_B['direction']} conf={parsed_B['confidence']}")
    except Exception as e:
        result["B_error"] = str(e)
        print(f" ERROR: {e}")

    # ── Condition C: With narrative ──
    print(f"    Running C (with narrative)...", end="", flush=True)
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
        print(f" {parsed_C['verdict']} {parsed_C['direction']} conf={parsed_C['confidence']}")
    except Exception as e:
        result["C_error"] = str(e)
        print(f" ERROR: {e}")

    # ── Compute outcome flags ──
    b_ok = result["B_verdict"] == "TRADE_NOW"
    c_ok = result["C_verdict"] == "TRADE_NOW"
    result["B_preserved_win"] = b_ok
    result["C_preserved_win"] = c_ok
    result["C_restored_win"] = (not b_ok) and c_ok

    return result


def summarize(results: list) -> dict:
    """Print and return aggregate counts by bucket."""
    total = len(results)
    completed = [
        r for r in results
        if r["B_verdict"] and r["C_verdict"]
        and "ERROR" not in (r["B_verdict"] or "")
        and "ERROR" not in (r["C_verdict"] or "")
        and r["B_verdict"] != "PARSE_ERROR"
        and r["C_verdict"] != "PARSE_ERROR"
    ]

    # Bucket counts
    b_preserved = sum(1 for r in completed if r["B_preserved_win"])
    c_restored = sum(1 for r in completed if r["C_restored_win"])
    both_missed = sum(1 for r in completed if not r["B_preserved_win"] and not r["C_preserved_win"])
    both_ok = sum(1 for r in completed if r["B_preserved_win"] and r["C_preserved_win"])
    b_ok_c_not = sum(1 for r in completed if r["B_preserved_win"] and not r["C_preserved_win"])
    with_narrative = sum(1 for r in results if r["ta_narrative_present"])

    print("\n" + "=" * 70)
    print("SUMMARY — Task 2b Winners (Inverse Experiment)")
    print("=" * 70)
    print(f"  Cohort size:             {total}")
    print(f"  Completed runs:          {len(completed)}/{total}")
    print(f"  With TA narrative:       {with_narrative}/{total}")
    print()
    print("  BUCKET BREAKDOWN:")
    print(f"  B=TRADE_NOW (current still right):      {b_preserved}/{len(completed)}")
    print(f"  B=TRADE_NOW + C=TRADE_NOW (both right): {both_ok}/{len(completed)}")
    print(f"  B=TRADE_NOW, C≠TRADE_NOW (B better):   {b_ok_c_not}/{len(completed)}")
    print(f"  B≠TRADE_NOW, C=TRADE_NOW (C restores):  {c_restored}/{len(completed)}  ← narrative helps")
    print(f"  B≠TRADE_NOW, C≠TRADE_NOW (both miss):  {both_missed}/{len(completed)}  ← narrative not enough")
    print()

    print("  Per-trade:")
    print(f"  {'ID':>6}  {'Pair':>8}  {'Pips':>6}  {'Narrative':>9}  {'Hist':>8}  {'B':>10}  {'C':>10}  {'Bucket'}")
    print(f"  {'-'*6}  {'-'*8}  {'-'*6}  {'-'*9}  {'-'*8}  {'-'*10}  {'-'*10}  {'-'*20}")
    for r in results:
        narr = "YES" if r["ta_narrative_present"] else "NO"
        hist = r["historical_verdict"] or "?"
        b = r["B_verdict"] or "FAIL"
        c = r["C_verdict"] or "FAIL"

        if b == "TRADE_NOW" and c == "TRADE_NOW":
            bucket = "BOTH_RIGHT"
        elif b == "TRADE_NOW" and c != "TRADE_NOW":
            bucket = "B_ONLY"
        elif b != "TRADE_NOW" and c == "TRADE_NOW":
            bucket = "C_RESTORES"
        elif b != "TRADE_NOW" and c != "TRADE_NOW":
            bucket = "BOTH_MISS"
        else:
            bucket = "ERROR"

        print(f"  {r['trade_id']:>6}  {r['pair']:>8}  {r['actual_pips']:>6.1f}  {narr:>9}  {hist:>8}  {b:>10}  {c:>10}  {bucket}")

    print()

    # Hypothesis assessment for winners
    if len(completed) == 0:
        hypothesis = "UNDECIDED — no completed runs"
    elif b_preserved == len(completed):
        hypothesis = "STRONG — current pipeline preserves TRADE_NOW on ALL winners (no regression on recognized wins)"
    elif b_preserved >= len(completed) * 0.75:
        hypothesis = "MOSTLY_PRESERVED — current pipeline still calls most winners correctly (≥75%)"
    elif c_restored > 0 and b_preserved < len(completed) * 0.5:
        hypothesis = "CONFIRMED_REGRESSION — pipeline misses >50% of wins, narrative restores some"
    elif c_restored > both_missed:
        hypothesis = "MIXED_WITH_HOPE — narrative helps more than it fails (C restores > both miss)"
    else:
        hypothesis = "MIXED — partial regression, narrative insufficient to restore wins"

    print(f"  Hypothesis: {hypothesis}")
    print()

    return {
        "total": total,
        "completed": len(completed),
        "with_narrative": with_narrative,
        "b_preserved": b_preserved,
        "c_restored": c_restored,
        "both_missed": both_missed,
        "both_ok": both_ok,
        "b_ok_c_not": b_ok_c_not,
        "hypothesis": hypothesis,
    }


def main():
    print("=" * 70)
    print("Plan A Task 2b — Inverse Ghost Replay (Winners)")
    print(f"Cohort: {len(COHORT)} winning scout trades (2026-04-30 to 2026-05-06)")
    print(f"Model: {LOCAL_MODEL_NAME} @ localhost:{LOCAL_MODEL_PORT}")
    print("Historical: ALL were CONFIRM (TRADE_NOW) and resulted in wins")
    print("=" * 70)

    # Check model is reachable
    try:
        import urllib.request
        url = f"http://localhost:{LOCAL_MODEL_PORT}/v1/models"
        with urllib.request.urlopen(url, timeout=5) as resp:
            models_data = json.loads(resp.read())
            model_ids = [m["id"] for m in models_data.get("data", [])]
            if LOCAL_MODEL_NAME not in model_ids:
                print(f"WARNING: {LOCAL_MODEL_NAME} not in model list: {model_ids}")
            else:
                print(f"Model confirmed: {LOCAL_MODEL_NAME}")
    except Exception as e:
        print(f"BLOCKED: Cannot reach local model at localhost:{LOCAL_MODEL_PORT}: {e}")
        print("Start the 35B: bash scripts/mlx_servers.sh start CSO")
        sys.exit(1)

    # Load system prompt
    try:
        system_prompt = load_ghost_validator_prompt()
        print(f"System prompt loaded: {len(system_prompt)} chars")
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # Run cohort
    results = []
    for i, trade_meta in enumerate(COHORT):
        print(f"\n[{i+1}/{len(COHORT)}] Processing trade {trade_meta['trade_id']}...")
        try:
            r = replay_one(trade_meta, system_prompt)
            results.append(r)
        except Exception as e:
            print(f"  FAILED: {e}")
            results.append({
                **trade_meta,
                "ta_narrative_present": False,
                "ta_clarity": "ERROR",
                "B_verdict": None,
                "B_direction": None,
                "B_confidence": None,
                "B_reasoning": "",
                "C_verdict": None,
                "C_direction": None,
                "C_confidence": None,
                "C_reasoning": "",
                "B_error": str(e),
                "C_error": str(e),
                "B_preserved_win": False,
                "C_preserved_win": False,
                "C_restored_win": False,
            })

    # Summarize
    agg = summarize(results)

    # Save raw results
    output_path = Path("/tmp/replay_winners_results.json")
    output_path.write_text(json.dumps({"results": results, "aggregate": agg}, indent=2, default=str))
    print(f"Raw results: {output_path}")

    return results, agg


if __name__ == "__main__":
    main()
