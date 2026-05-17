"""replay_iter8_production_faithful.py — Apples-to-apples replay against production.

Loads the 10 cohort entries from vision_training_data (production-saved validator
calls) and replays each with the LEAN iter 8 master-trader prompt + VALIDATOR_TOOLS
+ pattern_library skill files. Same chart, same TA narrative, same indicator
fields production sent — only the prompt is swapped.

Outputs:
- /tmp/iter8_results.json (per-trade verdicts + outcome alignment)
- /tmp/iter8_replay.log (run log)

Run:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python3 scripts/replay_iter8_production_faithful.py
"""

import base64
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SOURCE_DIR)

from optimizer.ghost_replay import _build_task_string_from_input, _extract_verdict

TRADING_DB = os.path.expanduser("~/Jarvis/Database/v2/trading_forex.db")
LEAN_PROMPT = "/tmp/prompt_variants/iter8_lean_master_trader.md"
SKILLS_DIR = "<repo_root>/Skills"
SKILL_FILES = ["VALIDATOR_TOOLS.md", "pattern_library.md"]
LOCAL_ENDPOINT = "http://127.0.0.1:11502/v1/chat/completions"
LOCAL_MODEL_NAME = "mlx-community/Qwen3.5-35B-A3B-4bit"

OUT_RESULTS = "/tmp/iter8_results.json"
OUT_LOG = "/tmp/iter8_replay.log"

# Cohort: (trade_id, vtd_id, pair, direction, actual_pips, category)
COHORT = [
    ("13310", 2098, "AUD_JPY", "SELL",  71.9, "winner"),
    ("13396", 2156, "EUR_CHF", "SELL",  17.9, "winner"),
    ("13362", 2109, "AUD_JPY", "SELL",   8.2, "winner"),
    ("13452", 2584, "EUR_AUD", "SELL",   7.1, "winner"),
    ("13621", 3111, "GBP_USD", "BUY",    6.2, "winner"),
    ("13665", 3146, "USD_CAD", "SELL",   4.6, "winner"),
    ("13424", 2195, "USD_CAD", "SELL",   4.1, "winner"),
    ("13705", 3438, "EUR_USD", "BUY",  -10.2, "loser"),
    ("13713", 3441, "NZD_USD", "BUY",  -16.0, "loser"),
    ("13727", 3565, "AUD_USD", "SELL", -30.4, "loser"),
]


def load_system_prompt() -> str:
    parts = [Path(LEAN_PROMPT).read_text().strip()]
    for sf in SKILL_FILES:
        p = Path(SKILLS_DIR) / sf
        if p.exists():
            parts.append(f"\n\n---\n\n# Skill: {sf}\n\n{p.read_text().strip()}")
    return "\n\n".join(parts)


def fetch_entry(vtd_id: int) -> dict | None:
    conn = sqlite3.connect(TRADING_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, chart_path, input_prompt, verdict, timestamp "
        "FROM vision_training_data WHERE id = ?",
        (vtd_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["input_prompt"] = json.loads(d["input_prompt"])
    except Exception:
        pass
    return d


def load_chart_b64(chart_path: str) -> tuple[str | None, str]:
    if not chart_path or not os.path.exists(chart_path):
        return None, "image/png"
    raw = open(chart_path, "rb").read()
    media = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return base64.b64encode(raw).decode(), media


def call_35b(system_prompt: str, task_text: str, chart_b64: str | None,
             chart_media: str = "image/png") -> str:
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
    log("ITER 8 PRODUCTION-FAITHFUL REPLAY")
    log("=" * 70)
    log(f"System prompt: lean master-trader + VALIDATOR_TOOLS + pattern_library")
    log(f"Per-trade input: production-saved input_prompt JSON + chart")
    log(f"Cohort: {len(COHORT)} trades ({sum(1 for c in COHORT if c[5]=='winner')} winners, "
        f"{sum(1 for c in COHORT if c[5]=='loser')} losers)")
    log("")

    system_prompt = load_system_prompt()
    log(f"System prompt size: {len(system_prompt)} chars")

    results = []
    t0 = time.time()
    for trade_id, vtd_id, pair, direction, actual_pips, category in COHORT:
        log(f"\n[{trade_id}] {pair} {direction} | actual: {actual_pips:+}p ({category})")
        entry = fetch_entry(vtd_id)
        if not entry:
            log(f"  ERROR: vtd_id {vtd_id} not found")
            continue
        chart_b64, chart_media = load_chart_b64(entry["chart_path"])
        log(f"  Chart: {entry['chart_path']} loaded={'YES' if chart_b64 else 'NO'}")
        task = _build_task_string_from_input(entry["input_prompt"])
        try:
            t_call = time.time()
            raw = call_35b(system_prompt, task, chart_b64, chart_media)
            dt = time.time() - t_call
        except Exception as e:
            log(f"  ERROR calling 35B: {e}")
            results.append({
                "trade_id": trade_id, "vtd_id": vtd_id, "pair": pair, "direction": direction,
                "actual_pips": actual_pips, "category": category,
                "verdict": "ERROR", "verdict_direction": None, "confidence": None,
                "reasoning": str(e), "correct": False,
            })
            continue
        parsed = _extract_verdict(raw)
        verdict = parsed.get("verdict")
        verdict_dir = parsed.get("direction")
        conf = parsed.get("confidence")
        reasoning = (parsed.get("reasoning") or "")[:300]
        correct = score_outcome(category, verdict, verdict_dir, direction)
        results.append({
            "trade_id": trade_id, "vtd_id": vtd_id, "pair": pair, "direction": direction,
            "actual_pips": actual_pips, "category": category,
            "verdict": verdict, "verdict_direction": verdict_dir, "confidence": conf,
            "reasoning_snippet": reasoning, "correct": correct,
            "elapsed_s": round(dt, 1),
        })
        log(f"  → {verdict} {verdict_dir} conf={conf} [{ 'CORRECT' if correct else 'INCORRECT' }] "
            f"({dt:.1f}s)")

    elapsed_min = (time.time() - t0) / 60
    log("")
    log("=" * 70)
    correct_count = sum(1 for r in results if r["correct"])
    winner_correct = sum(1 for r in results if r["category"] == "winner" and r["correct"])
    loser_correct = sum(1 for r in results if r["category"] == "loser" and r["correct"])
    log(f"ITER 8 SUMMARY: {correct_count}/{len(COHORT)} correct "
        f"(winners {winner_correct}/7, losers {loser_correct}/3)")
    log(f"Elapsed: {elapsed_min:.1f} min")
    log("=" * 70)

    Path(OUT_RESULTS).write_text(json.dumps(results, indent=2))
    Path(OUT_LOG).write_text("\n".join(log_lines))
    log(f"Results: {OUT_RESULTS}")
    log(f"Log: {OUT_LOG}")


if __name__ == "__main__":
    main()
