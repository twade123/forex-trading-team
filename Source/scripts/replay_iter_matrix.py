"""replay_iter_matrix.py — Plan A Task 2e prompt-variant matrix.

For each of 5 prompt variants, runs a fixed 10-trade cohort through the local 35B
and scores by outcome-alignment (did the verdict match what actually happened?).

Usage:
    source ~/myenv/bin/activate && \\
    cd "<repo_root>/Source" && \\
    python3 scripts/replay_iter_matrix.py

COHORT:
  Winners (7): validator should KEEP as TRADE_NOW (or WATCH that fires)
  Losers  (3): validator should REJECT to WATCH or SKIP

Vision verification dump written to /tmp/vision_check_iter0.txt BEFORE the matrix starts.
Script sleeps 30 seconds after the dump so Tim can intervene.
"""

import json
import re
import sqlite3
import sys
import os
import base64
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
JARVIS_ROOT = "~/Jarvis"
SOURCE_DIR = f"{JARVIS_ROOT}/Forex Trading Team/Source"
sys.path.insert(0, SOURCE_DIR)

TRADING_DB = f"{JARVIS_ROOT}/Database/v2/trading_forex.db"
CHART_TEMP_DIR = "/tmp/replay_charts"
PARTIAL_JSON = "/tmp/iter_matrix_partial.json"
INDICATOR_BLOCKS_JSON = "/tmp/cohort_indicator_blocks.json"
VISION_CHECK_FILE = "/tmp/vision_check_iter0.txt"
NOTES_OUT = f"{JARVIS_ROOT}/docs/superpowers/plans/notes/2026-05-10-task-2e-prompt-iteration-matrix.md"
RAW_JSON_OUT = f"{JARVIS_ROOT}/docs/superpowers/plans/notes/2026-05-10-task-2e-prompt-iteration-matrix-raw.json"

LOCAL_MODEL_PORT = 11502
LOCAL_MODEL_NAME = "mlx-community/Qwen3.5-35B-A3B-4bit"

VARIANTS = [
    # (name, prompt_path, sample_kwargs, [extras]) — extras: {"inject_indicator_block": True}
    # extras is a dict; absent or empty means baseline behavior (chart only).
    ("iter0_baseline",            "/tmp/prompt_variants/iter0_baseline.md",            {}),
    ("iter1_threshold",           "/tmp/prompt_variants/iter1_threshold.md",           {}),
    ("iter2_no_narrative_tax",    "/tmp/prompt_variants/iter2_no_narrative_tax.md",    {}),
    ("iter3_exhaustion_example",  "/tmp/prompt_variants/iter3_exhaustion_example.md",  {}),
    ("iter4_all_combined",        "/tmp/prompt_variants/iter4_all_combined.md",        {}),
    # iter5: vault-recommended deterministic params for Qwen 35B MoE — break stuck routing
    ("iter5_temp01_baseline",     "/tmp/prompt_variants/iter5_temp01_baseline.md",     {"temperature": 0.1, "top_p": 0.9}),
    # iter6: production-faithful skill stack
    ("iter6_with_skills",         "/tmp/prompt_variants/iter6_with_skills.md",         {}),
    # iter7: cross-checklist (failed without indicator data)
    ("iter7_cross_checklist",     "/tmp/prompt_variants/iter7_cross_checklist.md",     {}),
    # iter8: LEAN prompt + production input_prompt (WITH NARRATIVE) — ran via separate
    # replay_iter8_production_faithful.py script. Result: 1/10 — narrative biased model.
    # Skipped here to keep this matrix cohesive.
    # iter9: iter 2 + numerical block — won 9/10 on 10-trade cohort
    ("iter9_iter2_with_numbers",  "/tmp/prompt_variants/iter2_no_narrative_tax.md",    {}, {"inject_indicator_block": True}),
]

# ── Cohort ─────────────────────────────────────────────────────────────────────
# 7 winning scout trades (validator must KEEP as TRADE_NOW)
# 3 losing scout trades (validator must REJECT to WATCH/SKIP)
# All charts pre-exist in CHART_TEMP_DIR from Task 2d regen.

COHORT = [
    # --- WINNERS (7): validator should say TRADE_NOW matching direction ---
    {
        "trade_id": "13310",
        "pair": "AUD_JPY",
        "direction": "SELL",
        "entry_time": "2026-04-30T09:49:57",
        "actual_pips": 71.9,
        "actual_result": "win",
        "category": "winner",
        "chart_path": f"{CHART_TEMP_DIR}/trade_13310_AUD_JPY.png",
        "entry_type": "scout",
    },
    {
        "trade_id": "13396",
        "pair": "EUR_CHF",
        "direction": "SELL",
        "entry_time": "2026-04-30T13:48:54",
        "actual_pips": 17.9,
        "actual_result": "win",
        "category": "winner",
        "chart_path": f"{CHART_TEMP_DIR}/trade_13396_EUR_CHF.png",
        "entry_type": "scout",
    },
    {
        "trade_id": "13362",
        "pair": "AUD_JPY",
        "direction": "SELL",
        "entry_time": "2026-04-30T10:50:05",
        "actual_pips": 8.2,
        "actual_result": "win",
        "category": "winner",
        "chart_path": f"{CHART_TEMP_DIR}/trade_13362_AUD_JPY.png",
        "entry_type": "scout",
    },
    {
        "trade_id": "13452",
        "pair": "EUR_AUD",
        "direction": "SELL",
        "entry_time": "2026-05-01T16:34:10",
        "actual_pips": 7.1,
        "actual_result": "win",
        "category": "winner",
        "chart_path": f"{CHART_TEMP_DIR}/trade_13452_EUR_AUD.png",
        "entry_type": "scout",
    },
    {
        "trade_id": "13621",
        "pair": "GBP_USD",
        "direction": "BUY",
        "entry_time": "2026-05-05T23:51:09",
        "actual_pips": 6.2,
        "actual_result": "win",
        "category": "winner",
        "chart_path": f"{CHART_TEMP_DIR}/trade_13621_GBP_USD.png",
        "entry_type": "scout",
    },
    {
        "trade_id": "13665",
        "pair": "USD_CAD",
        "direction": "SELL",
        "entry_time": "2026-05-06T02:09:42",
        "actual_pips": 4.6,
        "actual_result": "win",
        "category": "winner",
        "chart_path": f"{CHART_TEMP_DIR}/trade_13665_USD_CAD.png",
        "entry_type": "scout",
    },
    {
        "trade_id": "13424",
        "pair": "USD_CAD",
        "direction": "SELL",
        "entry_time": "2026-04-30T15:45:49",
        "actual_pips": 4.1,
        "actual_result": "win",
        "category": "winner",
        "chart_path": f"{CHART_TEMP_DIR}/trade_13424_USD_CAD.png",
        "entry_type": "scout",
    },
    # --- LOSERS (3): validator should say WATCH or SKIP ---
    {
        "trade_id": "13705",
        "pair": "EUR_USD",
        "direction": "BUY",
        "entry_time": "2026-05-07T10:17:52",
        "actual_pips": -10.2,
        "actual_result": "loss",
        "category": "loser",
        "chart_path": f"{CHART_TEMP_DIR}/trade_13705_EUR_USD.png",
        "entry_type": "scout",
    },
    {
        "trade_id": "13713",
        "pair": "NZD_USD",
        "direction": "BUY",
        "entry_time": "2026-05-07T10:28:41",
        "actual_pips": -16.0,
        "actual_result": "loss",
        "category": "loser",
        "chart_path": f"{CHART_TEMP_DIR}/trade_13713_NZD_USD.png",
        "entry_type": "scout",
    },
    {
        "trade_id": "13727",
        "pair": "AUD_USD",
        "direction": "SELL",
        "entry_time": "2026-05-07T21:21:27",
        "actual_pips": -30.4,
        "actual_result": "loss",
        "category": "loser",
        "chart_path": f"{CHART_TEMP_DIR}/trade_13727_AUD_USD.png",
        "entry_type": "scout",
    },
]


def load_prompt(path: str) -> str:
    """Load a prompt variant from file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return p.read_text()


def load_chart_as_base64(chart_path: str) -> tuple:
    """Load chart PNG and return (base64_str, media_type). Returns (None, None) if missing."""
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


def build_task_text(trade: dict, indicator_block: str | None = None) -> str:
    """Build the user message text for a trade.

    If indicator_block is provided (iter 8+), it is prepended before the chart
    instruction so the model sees computed indicator data alongside the chart.
    """
    pair_display = trade["pair"].replace("_", "/")
    direction = trade["direction"]
    base = (
        f"M15 chart — {pair_display}. Scout identified a {direction} setup. "
        f"Read the chart fresh and form YOUR OWN thesis from the structure you see.\n\n"
        f"Return ONLY a ```json code block with: verdict (TRADE_NOW/WATCH/SKIP), "
        f"direction (BUY/SELL), confidence (0-10 INTEGER), reasoning (start with CHART READ:), "
        f"re_entry_conditions (list of {{field, op, value, reason}} dicts), "
        f"snipe_entry_zone, snipe_invalidation, snipe_target.\n\n"
        f"After analyzing the chart, respond with ONLY a ```json code block. "
        f"No prose outside the JSON."
    )
    if indicator_block:
        return f"{indicator_block}\n\n---\n\n{base}"
    return base


def call_local_35b(system_prompt: str, task_text: str, chart_b64: str, chart_media_type: str, sample_kwargs: dict = None) -> str:
    """Call the local 35B model via OpenAI-compatible API.

    sample_kwargs may override defaults: temperature, top_p, etc.
    """
    from openai import OpenAI

    client = OpenAI(base_url=f"http://localhost:{LOCAL_MODEL_PORT}", api_key="mlx-local")

    content = []
    if chart_b64:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{chart_media_type};base64,{chart_b64}"},
        })
    content.append({"type": "text", "text": task_text})

    api_params = {
        "model": LOCAL_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "max_tokens": 2500,
        "temperature": 0,
        "timeout": 300,
    }
    if sample_kwargs:
        api_params.update(sample_kwargs)

    response = client.chat.completions.create(**api_params)
    raw = response.choices[0].message.content or ""
    # Strip Qwen3 thinking tags
    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
    return raw


def parse_verdict(response_text: str) -> dict:
    """Extract verdict, direction, confidence, and reasoning snippet from JSON response."""
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


def score_outcome_alignment(trade: dict, verdict: str, direction: str) -> bool:
    """
    Return True if the verdict aligned with the actual trade outcome.

    Winner (actual win, pips > 0):
      CORRECT if verdict == TRADE_NOW AND direction matches trade direction
      INCORRECT if verdict == WATCH/SKIP or direction is wrong

    Loser (actual loss, pips < 0):
      CORRECT if verdict in (WATCH, SKIP)  — rejected the bad trade
      INCORRECT if verdict == TRADE_NOW (with correct direction)
    """
    category = trade["category"]
    trade_dir = trade["direction"].upper() if trade["direction"] else ""
    verdict_up = (verdict or "").upper()
    dir_up = (direction or "").upper()

    if category == "winner":
        return verdict_up == "TRADE_NOW" and dir_up == trade_dir
    else:  # loser
        return verdict_up in ("WATCH", "SKIP")


def do_vision_check(system_prompt: str, trade: dict) -> tuple:
    """
    Run one validator call for vision verification.
    Writes full dump to VISION_CHECK_FILE.
    Returns (raw_response, parsed).
    """
    chart_b64, chart_media = load_chart_as_base64(trade["chart_path"])
    task_text = build_task_text(trade)

    # Truncate chart_b64 for display
    chart_b64_preview = (chart_b64[:200] + "...[truncated]") if chart_b64 else "MISSING"

    print(f"\n=== VISION CHECK: {trade['trade_id']} {trade['pair']} ===")
    print(f"  Chart: {trade['chart_path']}")
    print(f"  Chart loaded: {'YES' if chart_b64 else 'NO'}")
    print(f"  Calling 35B...", flush=True)

    raw = call_local_35b(system_prompt, task_text, chart_b64, chart_media)
    parsed = parse_verdict(raw)

    # Write dump
    dump = [
        "=" * 70,
        "VISION CHECK — replay_iter_matrix.py Plan A Task 2e",
        "=" * 70,
        f"Trade: {trade['trade_id']} {trade['pair']} {trade['direction']} (actual: {trade['actual_pips']}p {trade['actual_result']})",
        f"Chart path: {trade['chart_path']}",
        "",
        "--- SYSTEM PROMPT (first 500 chars) ---",
        system_prompt[:500],
        "...[truncated]",
        "",
        "--- USER MESSAGE TEXT ---",
        task_text,
        "",
        f"--- CHART IMAGE (base64 first 200 chars) ---",
        chart_b64_preview,
        "",
        "--- RAW MODEL RESPONSE ---",
        raw,
        "",
        "--- PARSED ---",
        f"verdict: {parsed['verdict']}",
        f"direction: {parsed['direction']}",
        f"confidence: {parsed['confidence']}",
        f"reasoning (first 300): {parsed['reasoning']}",
        "",
        "Tim: Does the reasoning quote SPECIFIC chart numbers (RSI value, EMA prices,",
        "BB width, candle pattern names, fan separation distances)?",
        "If response is generic ('fan looks expanding, RSI bullish') with no specifics,",
        "vision may not have fired and the matrix results are unreliable.",
    ]
    Path(VISION_CHECK_FILE).write_text("\n".join(dump))
    print(f"\nVISION CHECK FILE: {VISION_CHECK_FILE} — Tim please review. Continuing in 30 seconds.")
    time.sleep(30)

    return raw, parsed


def save_partial(results: list) -> None:
    """Save current results to partial JSON checkpoint."""
    with open(PARTIAL_JSON, "w") as f:
        json.dump(results, f, indent=2)


def run_matrix() -> list:
    """
    Main matrix runner.

    For each variant (5 total):
      For each trade in COHORT (10 total):
        Call 35B, capture verdict, score outcome alignment.

    Vision check runs FIRST on iter0 + trade[0], result reused as iter0/trade0.
    Returns list of per-(variant, trade) result dicts.
    """
    print("\n" + "=" * 70)
    print("PLAN A TASK 2e — PROMPT ITERATION MATRIX")
    print("=" * 70)
    print(f"Variants: {len(VARIANTS)}")
    print(f"Cohort:   {len(COHORT)} trades ({sum(1 for t in COHORT if t['category']=='winner')} winners, {sum(1 for t in COHORT if t['category']=='loser')} losers)")
    print()

    # Print cohort
    print("COHORT (10 trades):")
    print("  Winners (validator should KEEP as TRADE_NOW):")
    for t in COHORT:
        if t["category"] == "winner":
            print(f"    [{t['trade_id']}] {t['pair']} {t['direction']} {t['entry_time']} +{t['actual_pips']}p ({t['entry_type']})")
    print("  Losing trades (validator should REJECT to WATCH/SKIP):")
    for t in COHORT:
        if t["category"] == "loser":
            print(f"    [{t['trade_id']}] {t['pair']} {t['direction']} {t['entry_time']} {t['actual_pips']}p")
    print()

    # Resume support: load any prior partial results, key by (variant, trade_id).
    prior_partial = []
    if Path(PARTIAL_JSON).exists():
        try:
            prior_partial = json.loads(Path(PARTIAL_JSON).read_text()) or []
        except Exception as e:
            print(f"  WARN: failed to load partial checkpoint ({e}) — starting fresh")
            prior_partial = []
    prior_lookup = {(r.get("variant"), r.get("trade_id")): r for r in prior_partial}
    if prior_lookup:
        print(f"\nRESUME: found {len(prior_lookup)} prior entries in {PARTIAL_JSON} — will skip those.")

    all_results = list(prior_partial)
    vision_check_result = None  # will hold (raw, parsed) from iter0/trade0

    # Load per-trade indicator blocks once (used by variants with inject_indicator_block=True)
    indicator_blocks = {}
    if Path(INDICATOR_BLOCKS_JSON).exists():
        try:
            indicator_blocks = json.loads(Path(INDICATOR_BLOCKS_JSON).read_text())
            print(f"\nLoaded indicator blocks for {len(indicator_blocks)} trades from {INDICATOR_BLOCKS_JSON}")
        except Exception as e:
            print(f"  WARN: failed to load indicator blocks ({e})")

    for iter_idx, variant_tuple in enumerate(VARIANTS):
        # Tuple shapes (backward-compat):
        #   (name, prompt_path)
        #   (name, prompt_path, sample_kwargs)
        #   (name, prompt_path, sample_kwargs, extras_dict)
        if len(variant_tuple) == 2:
            variant_name, prompt_path = variant_tuple
            sample_kwargs = {}
            extras = {}
        elif len(variant_tuple) == 3:
            variant_name, prompt_path, sample_kwargs = variant_tuple
            extras = {}
        else:
            variant_name, prompt_path, sample_kwargs, extras = variant_tuple
        inject_ind = bool(extras.get("inject_indicator_block"))
        print(f"\n{'='*70}")
        print(f"VARIANT {iter_idx}: {variant_name}")
        print(f"  Prompt: {prompt_path}")
        print(f"{'='*70}")

        try:
            system_prompt = load_prompt(prompt_path)
        except FileNotFoundError as e:
            print(f"  ERROR loading prompt: {e}")
            continue

        variant_results = []

        for trade_idx, trade in enumerate(COHORT):
            trade_id = trade["trade_id"]
            pair = trade["pair"]
            direction = trade["direction"]

            print(f"\n  [iter {iter_idx}, trade {trade_idx+1}/{len(COHORT)}] {pair} {direction} (trade {trade_id})", flush=True)

            # Resume: skip if this (variant, trade) already has a result
            if (variant_name, trade_id) in prior_lookup:
                cached = prior_lookup[(variant_name, trade_id)]
                variant_results.append(cached)
                alignment_str = "CORRECT" if cached.get("correct") else "INCORRECT"
                print(f"  → [cached] {cached.get('verdict')} {cached.get('verdict_direction')} conf={cached.get('confidence')} [{alignment_str}]", flush=True)
                continue

            # Vision check: iter0, trade0 — dump file, sleep 30s, then use the result
            if iter_idx == 0 and trade_idx == 0:
                try:
                    raw, parsed = do_vision_check(system_prompt, trade)
                    vision_check_result = parsed
                except Exception as e:
                    print(f"  VISION CHECK ERROR: {e}")
                    traceback.print_exc()
                    raw = ""
                    parsed = {"verdict": "ERROR", "direction": None, "confidence": None, "reasoning": str(e)}

                correct = score_outcome_alignment(trade, parsed["verdict"], parsed["direction"])
                result = {
                    "variant": variant_name,
                    "trade_id": trade_id,
                    "pair": pair,
                    "direction": direction,
                    "actual_pips": trade["actual_pips"],
                    "actual_result": trade["actual_result"],
                    "category": trade["category"],
                    "verdict": parsed["verdict"],
                    "verdict_direction": parsed["direction"],
                    "confidence": parsed["confidence"],
                    "reasoning_snippet": parsed["reasoning"],
                    "correct": correct,
                }
                variant_results.append(result)
                all_results.append(result)

                alignment_str = "CORRECT" if correct else "INCORRECT"
                print(f"  → {parsed['verdict']} {parsed['direction']} conf={parsed['confidence']} [{alignment_str}]")
                save_partial(all_results)
                continue

            # Regular call
            chart_b64, chart_media = load_chart_as_base64(trade["chart_path"])
            if not chart_b64:
                print(f"  WARNING: Chart not found at {trade['chart_path']}")

            ind_block = None
            if inject_ind:
                ind_data = indicator_blocks.get(trade_id)
                if ind_data and "block_text" in ind_data:
                    ind_block = ind_data["block_text"]
                    print(f"  [indicator block injected: phase={ind_data.get('phase')} "
                          f"fan={ind_data.get('fan',{}).get('fan_direction')} "
                          f"{ind_data.get('fan',{}).get('fan_state')}]")
                else:
                    print(f"  WARNING: inject_indicator_block=True but no block for {trade_id}")
            task_text = build_task_text(trade, indicator_block=ind_block)

            try:
                raw = call_local_35b(system_prompt, task_text, chart_b64, chart_media, sample_kwargs=sample_kwargs)
                parsed = parse_verdict(raw)
            except Exception as e:
                print(f"  ERROR calling 35B: {e}")
                traceback.print_exc()
                parsed = {"verdict": "ERROR", "direction": None, "confidence": None, "reasoning": str(e)}

            correct = score_outcome_alignment(trade, parsed["verdict"], parsed["direction"])
            result = {
                "variant": variant_name,
                "trade_id": trade_id,
                "pair": pair,
                "direction": direction,
                "actual_pips": trade["actual_pips"],
                "actual_result": trade["actual_result"],
                "category": trade["category"],
                "verdict": parsed["verdict"],
                "verdict_direction": parsed["direction"],
                "confidence": parsed["confidence"],
                "reasoning_snippet": parsed["reasoning"],
                "correct": correct,
            }
            variant_results.append(result)
            all_results.append(result)

            alignment_str = "CORRECT" if correct else "INCORRECT"
            print(f"  → {parsed['verdict']} {parsed['direction']} conf={parsed['confidence']} [{alignment_str}]", flush=True)
            save_partial(all_results)

        # Per-variant summary
        correct_count = sum(1 for r in variant_results if r["correct"])
        winner_correct = sum(1 for r in variant_results if r["category"] == "winner" and r["correct"])
        loser_correct = sum(1 for r in variant_results if r["category"] == "loser" and r["correct"])
        print(f"\n  VARIANT {variant_name} SUMMARY: {correct_count}/10 correct "
              f"(winners {winner_correct}/7, losers {loser_correct}/3)")

    return all_results


def build_report(all_results: list) -> str:
    """Build the markdown comparison report."""
    variant_names = [v[0] for v in VARIANTS]

    # Per-variant scores
    variant_scores = {}
    for vn in variant_names:
        vr = [r for r in all_results if r["variant"] == vn]
        correct = sum(1 for r in vr if r["correct"])
        winner_correct = sum(1 for r in vr if r["category"] == "winner" and r["correct"])
        loser_correct = sum(1 for r in vr if r["category"] == "loser" and r["correct"])
        variant_scores[vn] = {
            "correct": correct,
            "winner_correct": winner_correct,
            "loser_correct": loser_correct,
        }

    best_variant = max(variant_scores, key=lambda v: variant_scores[v]["correct"])
    best_score = variant_scores[best_variant]["correct"]

    # Per-trade matrix
    trade_ids = [t["trade_id"] for t in COHORT]

    lines = [
        "# Plan A Task 2e — Prompt Iteration Matrix",
        "",
        f"**Run date:** 2026-05-10",
        f"**Model:** {LOCAL_MODEL_NAME} @ port {LOCAL_MODEL_PORT}",
        "",
        "## Vision verification",
        "",
        f"See `{VISION_CHECK_FILE}` — Tim should eyeball whether the model's reasoning",
        "quotes specific chart numbers (RSI value, EMA prices, BB width, candle pattern names,",
        "fan separation distances). If the response is generic with no specifics, vision may not",
        "have fired and the matrix results are unreliable.",
        "",
        "## Cohort",
        "",
        "| trade_id | pair | dir | category | actual_pips | entry_type |",
        "|---|---|---|---|---|---|",
    ]
    for t in COHORT:
        pips_str = f"+{t['actual_pips']}" if t["actual_pips"] > 0 else str(t["actual_pips"])
        lines.append(
            f"| {t['trade_id']} | {t['pair']} | {t['direction']} | {t['category']} | {pips_str} | {t['entry_type']} |"
        )

    lines += [
        "",
        "**Cohort notes:** All 7 winners are scout entry_type. "
        "Losers are scout trades that the live validator (incorrectly) passed through with CONFIRM verdict.",
        "",
        "## Per-variant outcome-alignment",
        "",
        "| Variant | Winners caught (7 must keep TRADE_NOW) | Losers rejected (3 must shift to WATCH/SKIP) | Total Correct / 10 |",
        "|---|---|---|---|",
    ]
    for vn in variant_names:
        s = variant_scores[vn]
        lines.append(
            f"| {vn} | {s['winner_correct']}/7 | {s['loser_correct']}/3 | {s['correct']}/10 |"
        )

    lines += [
        "",
        "## Per-trade verdict matrix",
        "",
    ]

    # Build header
    header = "| trade_id | actual | " + " | ".join(variant_names) + " |"
    sep = "|---|---|" + "|".join(["---"] * len(variant_names)) + "|"
    lines.append(header)
    lines.append(sep)

    for trade_id in trade_ids:
        trade_meta = next(t for t in COHORT if t["trade_id"] == trade_id)
        pips_str = f"+{trade_meta['actual_pips']}" if trade_meta["actual_pips"] > 0 else str(trade_meta["actual_pips"])
        actual_label = f"{trade_meta['actual_result']} {pips_str}p"

        cells = [trade_id, actual_label]
        for vn in variant_names:
            result = next((r for r in all_results if r["variant"] == vn and r["trade_id"] == trade_id), None)
            if result:
                mark = "✓" if result["correct"] else "✗"
                conf = result["confidence"] if result["confidence"] is not None else "?"
                cells.append(f"{result['verdict']} {result['verdict_direction']} c={conf} {mark}")
            else:
                cells.append("—")

        lines.append("| " + " | ".join(cells) + " |")

    # Verdict reasoning snippets
    lines += [
        "",
        "## Sample reasoning snippets (first 300 chars, iter0 baseline)",
        "",
    ]
    for t in COHORT:
        r = next((x for x in all_results if x["variant"] == "iter0_baseline" and x["trade_id"] == t["trade_id"]), None)
        if r:
            lines.append(f"**Trade {t['trade_id']} {t['pair']} {t['direction']}:** {r['reasoning_snippet']}")
            lines.append("")

    # Conclusion
    if best_score >= 8:
        rec = f"Ship {best_variant} — {best_score}/10 meets the ≥8 threshold."
    elif best_score >= 6:
        rec = f"Best variant {best_variant} at {best_score}/10 is an improvement but below ship threshold. Investigate further."
    else:
        rec = f"No variant recovers strongly ({best_score}/10). Deeper investigation needed — prompt tuning may not be the root cause."

    lines += [
        "## Conclusion",
        "",
        f"- **Best variant:** {best_variant} with **{best_score}/10 correct**",
        f"- **Baseline (iter0):** {variant_scores['iter0_baseline']['correct']}/10 correct",
        f"- **Recommendation:** {rec}",
        "",
        "### Threshold change analysis (iter1 vs iter0)",
    ]
    iter0_score = variant_scores.get("iter0_baseline", {}).get("correct", 0)
    iter1_score = variant_scores.get("iter1_threshold", {}).get("correct", 0)
    delta = iter1_score - iter0_score
    lines.append(
        f"iter1 threshold-only change: {iter1_score}/10 vs iter0 {iter0_score}/10 (delta: {delta:+d}). "
        + ("Threshold is the key lever." if delta >= 2 else "Threshold alone doesn't explain recovery." if delta < 0 else "Marginal improvement from threshold alone.")
    )
    lines.append("")

    return "\n".join(lines)


def main():
    os.makedirs(os.path.dirname(NOTES_OUT), exist_ok=True)
    os.makedirs(CHART_TEMP_DIR, exist_ok=True)

    start = datetime.now(timezone.utc)
    all_results = run_matrix()

    # Save raw JSON
    with open(RAW_JSON_OUT, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nRaw JSON saved to: {RAW_JSON_OUT}")

    # Build and save report
    report = build_report(all_results)
    with open(NOTES_OUT, "w") as f:
        f.write(report)
    print(f"Report saved to: {NOTES_OUT}")

    # Final summary
    elapsed = (datetime.now(timezone.utc) - start).total_seconds() / 60
    print(f"\n{'='*70}")
    print(f"COMPLETE — {elapsed:.1f} minutes elapsed")
    print(f"Partial checkpoint: {PARTIAL_JSON}")
    print(f"Report: {NOTES_OUT}")
    print(f"Raw JSON: {RAW_JSON_OUT}")

    # Per-variant headline
    variant_names = [v[0] for v in VARIANTS]
    for vn in variant_names:
        vr = [r for r in all_results if r["variant"] == vn]
        correct = sum(1 for r in vr if r["correct"])
        winner_c = sum(1 for r in vr if r["category"] == "winner" and r["correct"])
        loser_c = sum(1 for r in vr if r["category"] == "loser" and r["correct"])
        print(f"  {vn}: {correct}/10 correct (winners {winner_c}/7, losers {loser_c}/3)")


if __name__ == "__main__":
    main()
