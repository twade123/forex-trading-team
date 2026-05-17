"""replay_iter18_pivots.py — iter 18: pivot-line overlay on M15 chart.

Same call shape as replay_iter16_v2.py with two changes:
1. System prompt: /tmp/prompt_variants/iter18_pivots.md (iter16 + ONE bullet
   teaching the model what the dashed amber pivot lines mean)
2. Chart source: regenerate via oanda_chart_pivot_regen.py — fetches prior D1
   candle, draws PP/R1/S1/R2/S2 as faint dashed amber horizontal lines on the
   regenerated M15 chart. NOT the production vision_training_data.chart_path.

Indicator block: UNCHANGED from iter16 (/tmp/cohort_indicator_blocks.json).

Run:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python3 scripts/replay_iter18_pivots.py
"""

import base64
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from scripts.oanda_chart_pivot_regen import regenerate_chart_with_pivots

ITER18_PROMPT = "/tmp/prompt_variants/iter18_pivots.md"
INDICATOR_BLOCKS_JSON = "/tmp/cohort_indicator_blocks.json"
LOCAL_ENDPOINT = "http://127.0.0.1:11502/v1/chat/completions"
LOCAL_MODEL_NAME = "mlx-community/Qwen3.5-35B-A3B-4bit"

OUT_RESULTS = "/tmp/iter18_pivots_results.json"
OUT_LOG = "/tmp/iter18_pivots_replay.log"
PIVOT_CHART_DIR = "/tmp/replay_charts_pivot"

# Cohort: (trade_id, pair, direction, entry_iso, actual_pips, category)
COHORT = [
    ("13138", "AUD_JPY", "SELL", "2026-04-29T18:49:36+00:00", -44.5, "loser"),
    ("13310", "AUD_JPY", "SELL", "2026-04-30T09:49:57+00:00",  71.9, "winner"),
    ("13362", "AUD_JPY", "SELL", "2026-04-30T10:50:05+00:00",   8.2, "winner"),
    ("13396", "EUR_CHF", "SELL", "2026-04-30T13:48:54+00:00",  17.9, "winner"),
    ("13424", "USD_CAD", "SELL", "2026-04-30T15:45:49+00:00",   4.1, "winner"),
    ("13452", "EUR_AUD", "SELL", "2026-05-01T16:34:10+00:00",   7.1, "winner"),
    ("13578", "AUD_USD", "SELL", "2026-05-04T16:51:45+00:00",   3.5, "winner"),
    ("13621", "GBP_USD", "BUY",  "2026-05-05T23:51:09+00:00",   6.2, "winner"),
    ("13665", "USD_CAD", "SELL", "2026-05-06T02:09:42+00:00",   4.6, "winner"),
    ("13681", "USD_CHF", "SELL", "2026-05-06T11:08:42+00:00", -11.1, "loser"),
    ("13705", "EUR_USD", "BUY",  "2026-05-07T10:17:52+00:00", -10.2, "loser"),
    ("13713", "NZD_USD", "BUY",  "2026-05-07T10:28:41+00:00", -16.0, "loser"),
    ("13727", "AUD_USD", "SELL", "2026-05-07T21:21:27+00:00", -30.4, "loser"),
    ("13743", "AUD_JPY", "SELL", "2026-05-07T22:04:25+00:00", -26.7, "loser"),
    ("13765", "GBP_JPY", "BUY",  "2026-05-08T07:10:15+00:00",  29.3, "winner"),
    ("13809", "GBP_USD", "BUY",  "2026-05-08T09:36:34+00:00",  -5.1, "loser"),
    ("13817", "EUR_JPY", "BUY",  "2026-05-08T10:02:34+00:00",   5.1, "winner"),
    ("13827", "EUR_USD", "BUY",  "2026-05-08T10:17:53+00:00",   4.7, "winner"),
    ("13843", "AUD_JPY", "BUY",  "2026-05-08T11:17:30+00:00",  -7.7, "loser"),
]


def load_indicator_blocks() -> dict:
    p = Path(INDICATOR_BLOCKS_JSON)
    if not p.exists():
        raise RuntimeError(f"{INDICATOR_BLOCKS_JSON} not found")
    return json.loads(p.read_text())


def load_chart_b64(chart_path: str) -> tuple:
    if not chart_path or not os.path.exists(chart_path):
        return None, "image/png"
    raw = open(chart_path, "rb").read()
    media = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return base64.b64encode(raw).decode(), media


def build_task_text(pair: str, direction: str, indicator_block: str) -> str:
    pair_display = pair.replace("_", "/")
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
    return f"{indicator_block}\n\n---\n\n{base}"


def call_35b(system_prompt: str, task_text: str, chart_b64, chart_media: str = "image/png") -> str:
    content = []
    if chart_b64:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{chart_media};base64,{chart_b64}"}})
    content.append({"type": "text", "text": task_text})
    payload = json.dumps({
        "model": LOCAL_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": 2500,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        LOCAL_ENDPOINT, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    out = data["choices"][0]["message"].get("content", "") or ""
    return re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL).strip()


def parse_verdict(raw: str) -> dict:
    cleaned = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned)
    js = m.group(1) if m else None
    if not js:
        i = cleaned.find("{")
        if i == -1:
            return {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": ""}
        depth = 0
        end = -1
        for k, ch in enumerate(cleaned[i:]):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + k + 1
                    break
        js = cleaned[i:end] if end > 0 else None
    if not js:
        return {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": ""}
    try:
        d = json.loads(js)
        return {
            "verdict": d.get("verdict", "UNKNOWN"),
            "direction": d.get("direction"),
            "confidence": d.get("confidence"),
            "reasoning": str(d.get("reasoning", ""))[:300],
        }
    except json.JSONDecodeError:
        return {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": ""}


def score_outcome(category: str, verdict: str, direction: str, trade_dir: str) -> bool:
    v = (verdict or "").upper()
    d = (direction or "").upper()
    td = trade_dir.upper()
    if category == "winner":
        return v == "TRADE_NOW" and d == td
    return v in ("WATCH", "SKIP")


def main():
    log_lines = []
    def log(msg):
        print(msg, flush=True)
        log_lines.append(msg)

    log("=" * 70)
    log("ITER 18 — pivot-line overlay on M15 chart (iter16 prompt + 1 bullet)")
    log("=" * 70)
    log(f"Prompt: {ITER18_PROMPT}")
    log(f"Indicator blocks: {INDICATOR_BLOCKS_JSON} (unchanged from iter16)")
    log(f"Chart source: oanda_chart_pivot_regen (NOT vtd.chart_path)")
    system_prompt = Path(ITER18_PROMPT).read_text().strip()
    indicator_blocks = load_indicator_blocks()
    log(f"System prompt size: {len(system_prompt)} chars")
    log(f"Indicator blocks loaded: {len(indicator_blocks)}")
    log(f"Cohort: {len(COHORT)} trades "
        f"({sum(1 for c in COHORT if c[5]=='winner')} winners, "
        f"{sum(1 for c in COHORT if c[5]=='loser')} losers)")
    log("")

    os.makedirs(PIVOT_CHART_DIR, exist_ok=True)
    results = []
    t0 = time.time()
    for trade_id, pair, direction, entry_iso, actual_pips, category in COHORT:
        log(f"\n[{trade_id}] {pair} {direction} | actual: {actual_pips:+}p ({category})")
        ind = indicator_blocks.get(trade_id)
        if not ind or "block_text" not in ind:
            log(f"  ERROR: no indicator block for {trade_id}")
            continue

        # Regenerate chart WITH pivot overlay
        chart_out = f"{PIVOT_CHART_DIR}/{trade_id}_{pair}_pivot.png"
        chart_path = regenerate_chart_with_pivots(pair, entry_iso, chart_out)
        if not chart_path:
            log(f"  ERROR: pivot chart regen failed for {trade_id}")
            results.append({
                "trade_id": trade_id, "pair": pair, "direction": direction,
                "actual_pips": actual_pips, "category": category,
                "verdict": "ERROR", "verdict_direction": None, "confidence": None,
                "reasoning_snippet": "pivot chart regen failed", "correct": False,
            })
            continue
        chart_b64, chart_media = load_chart_b64(chart_path)
        log(f"  Pivot chart: {chart_path} ({os.path.getsize(chart_path)//1024}KB)")
        log(f"  Indicator: phase={ind.get('phase')} fan={ind.get('fan',{}).get('fan_direction')} "
            f"{ind.get('fan',{}).get('fan_state')}")

        task = build_task_text(pair, direction, ind["block_text"])
        try:
            t_call = time.time()
            raw = call_35b(system_prompt, task, chart_b64, chart_media)
            dt = time.time() - t_call
        except Exception as e:
            log(f"  ERROR calling 35B: {e}")
            results.append({
                "trade_id": trade_id, "pair": pair, "direction": direction,
                "actual_pips": actual_pips, "category": category,
                "verdict": "ERROR", "verdict_direction": None, "confidence": None,
                "reasoning_snippet": str(e), "correct": False,
            })
            continue
        parsed = parse_verdict(raw)
        verdict = parsed.get("verdict")
        verdict_dir = parsed.get("direction")
        conf = parsed.get("confidence")
        reasoning = (parsed.get("reasoning") or "")[:300]
        correct = score_outcome(category, verdict, verdict_dir, direction)
        results.append({
            "trade_id": trade_id, "pair": pair, "direction": direction,
            "actual_pips": actual_pips, "category": category,
            "verdict": verdict, "verdict_direction": verdict_dir, "confidence": conf,
            "reasoning_snippet": reasoning, "correct": correct,
            "elapsed_s": round(dt, 1),
        })
        log(f"  → {verdict} {verdict_dir} conf={conf} "
            f"[{ 'CORRECT' if correct else 'INCORRECT' }] ({dt:.1f}s)")

        # Stream-write results after each trade so we can monitor mid-run
        Path(OUT_RESULTS).write_text(json.dumps(results, indent=2))
        Path(OUT_LOG).write_text("\n".join(log_lines))

    elapsed_min = (time.time() - t0) / 60
    log("")
    log("=" * 70)
    correct_count = sum(1 for r in results if r["correct"])
    n_winners = sum(1 for c in COHORT if c[5] == "winner")
    n_losers = sum(1 for c in COHORT if c[5] == "loser")
    winner_correct = sum(1 for r in results if r["category"] == "winner" and r["correct"])
    loser_correct = sum(1 for r in results if r["category"] == "loser" and r["correct"])
    log(f"ITER 18 SUMMARY: {correct_count}/{len(COHORT)} correct "
        f"(winners {winner_correct}/{n_winners}, losers {loser_correct}/{n_losers})")
    log(f"Baseline (iter 16 v2): 13/19 (winners 9/11, losers 4/8)")
    log(f"Elapsed: {elapsed_min:.1f} min")
    log("=" * 70)

    Path(OUT_RESULTS).write_text(json.dumps(results, indent=2))
    Path(OUT_LOG).write_text("\n".join(log_lines))
    log(f"Results: {OUT_RESULTS}")
    log(f"Log: {OUT_LOG}")


if __name__ == "__main__":
    main()
