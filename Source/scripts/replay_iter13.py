"""replay_iter13.py — iter 9 exact formula against the 14-trade extended cohort.

iter 9 won 9/10 on the 10-trade cohort with this exact setup:
- System prompt: iter13_iter2_plus_exhaustion.md (full ghost_validator_v1.md minus
  bar-by-bar-narrative tax)
- NO skill files
- Per-trade user message: numerical indicator block (cascade_phase, cross sequence,
  fan_state, RSI, MACD hist, BB width, EMAs) + production chart
- NO production narrative (skipped — narrative was the poison in iter 8)

Cohort: 14 trades 2026-04-23 to 2026-05-07 (8 winners, 6 losers).

Run:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python3 scripts/replay_iter13.py
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

TRADING_DB = os.path.expanduser("~/Jarvis/Database/v2/trading_forex.db")
ITER13_PROMPT = "/tmp/prompt_variants/iter13_iter2_plus_exhaustion.md"
INDICATOR_BLOCKS_JSON = "/tmp/cohort_indicator_blocks.json"
SKILLS_DIR = "<repo_root>/Skills"
SKILL_FILES = []  # iter 9 won without skills
LOCAL_ENDPOINT = "http://127.0.0.1:11502/v1/chat/completions"
LOCAL_MODEL_NAME = "mlx-community/Qwen3.5-35B-A3B-4bit"

OUT_RESULTS = "/tmp/iter13_results.json"
OUT_LOG = "/tmp/iter13_replay.log"

# Cohort: (trade_id, vtd_id, pair, direction, actual_pips, category)
# 14-trade extended set: 8 winners + 6 losers, 2026-04-23 to 2026-05-07
COHORT = [
    ("13138", 1984, "AUD_JPY", "SELL", -44.5, "loser"),
    ("13310", 2098, "AUD_JPY", "SELL",  71.9, "winner"),
    ("13362", 2109, "AUD_JPY", "SELL",   8.2, "winner"),
    ("13396", 2156, "EUR_CHF", "SELL",  17.9, "winner"),
    ("13424", 2195, "USD_CAD", "SELL",   4.1, "winner"),
    ("13452", 2584, "EUR_AUD", "SELL",   7.1, "winner"),
    ("13578", 2756, "AUD_USD", "SELL",   3.5, "winner"),
    ("13621", 3111, "GBP_USD", "BUY",    6.2, "winner"),
    ("13665", 3146, "USD_CAD", "SELL",   4.6, "winner"),
    ("13681", 3269, "USD_CHF", "SELL", -11.1, "loser"),
    ("13705", 3438, "EUR_USD", "BUY",  -10.2, "loser"),
    ("13713", 3441, "NZD_USD", "BUY",  -16.0, "loser"),
    ("13727", 3565, "AUD_USD", "SELL", -30.4, "loser"),
    ("13743", 3576, "AUD_JPY", "SELL", -26.7, "loser"),
]


def load_system_prompt() -> str:
    parts = [Path(ITER13_PROMPT).read_text().strip()]
    for sf in SKILL_FILES:
        p = Path(SKILLS_DIR) / sf
        if p.exists():
            parts.append(f"\n\n---\n\n# Skill: {sf}\n\n{p.read_text().strip()}")
    return "\n\n".join(parts)


def load_indicator_blocks() -> dict:
    if not Path(INDICATOR_BLOCKS_JSON).exists():
        raise RuntimeError(f"{INDICATOR_BLOCKS_JSON} not found — run build_cohort_indicators.py first")
    return json.loads(Path(INDICATOR_BLOCKS_JSON).read_text())


def fetch_chart_path(vtd_id: int) -> str | None:
    conn = sqlite3.connect(TRADING_DB, timeout=10)
    row = conn.execute(
        "SELECT chart_path FROM vision_training_data WHERE id = ?", (vtd_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def load_chart_b64(chart_path: str) -> tuple:
    if not chart_path or not os.path.exists(chart_path):
        return None, "image/png"
    raw = open(chart_path, "rb").read()
    media = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return base64.b64encode(raw).decode(), media


def build_task_text(pair: str, direction: str, indicator_block: str) -> str:
    """iter 9's task structure: indicator block FIRST, then chart-read instruction."""
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
    log("ITER 13 — iter 9 exact formula on 14-trade extended cohort")
    log("=" * 70)
    log(f"Prompt: {ITER13_PROMPT}")
    log(f"Skill files: {SKILL_FILES}")
    log(f"Indicator blocks: {INDICATOR_BLOCKS_JSON}")
    system_prompt = load_system_prompt()
    indicator_blocks = load_indicator_blocks()
    log(f"System prompt size: {len(system_prompt)} chars")
    log(f"Indicator blocks loaded: {len(indicator_blocks)}")
    log(f"Cohort: {len(COHORT)} trades "
        f"({sum(1 for c in COHORT if c[5]=='winner')} winners, "
        f"{sum(1 for c in COHORT if c[5]=='loser')} losers)")
    log("")

    results = []
    t0 = time.time()
    for trade_id, vtd_id, pair, direction, actual_pips, category in COHORT:
        log(f"\n[{trade_id}] {pair} {direction} | actual: {actual_pips:+}p ({category})")
        ind = indicator_blocks.get(trade_id)
        if not ind or "block_text" not in ind:
            log(f"  ERROR: no indicator block for {trade_id}")
            continue
        chart_path = fetch_chart_path(vtd_id)
        chart_b64, chart_media = load_chart_b64(chart_path)
        log(f"  Chart loaded={'YES' if chart_b64 else 'NO'} ({chart_path})")
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
                "trade_id": trade_id, "vtd_id": vtd_id, "pair": pair, "direction": direction,
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
            "trade_id": trade_id, "vtd_id": vtd_id, "pair": pair, "direction": direction,
            "actual_pips": actual_pips, "category": category,
            "verdict": verdict, "verdict_direction": verdict_dir, "confidence": conf,
            "reasoning_snippet": reasoning, "correct": correct,
            "elapsed_s": round(dt, 1),
        })
        log(f"  → {verdict} {verdict_dir} conf={conf} "
            f"[{ 'CORRECT' if correct else 'INCORRECT' }] ({dt:.1f}s)")

    elapsed_min = (time.time() - t0) / 60
    log("")
    log("=" * 70)
    correct_count = sum(1 for r in results if r["correct"])
    n_winners = sum(1 for c in COHORT if c[5] == "winner")
    n_losers = sum(1 for c in COHORT if c[5] == "loser")
    winner_correct = sum(1 for r in results if r["category"] == "winner" and r["correct"])
    loser_correct = sum(1 for r in results if r["category"] == "loser" and r["correct"])
    log(f"ITER 13 SUMMARY: {correct_count}/{len(COHORT)} correct "
        f"(winners {winner_correct}/{n_winners}, losers {loser_correct}/{n_losers})")
    log(f"Elapsed: {elapsed_min:.1f} min")
    log("=" * 70)

    Path(OUT_RESULTS).write_text(json.dumps(results, indent=2))
    Path(OUT_LOG).write_text("\n".join(log_lines))
    log(f"Results: {OUT_RESULTS}")
    log(f"Log: {OUT_LOG}")


if __name__ == "__main__":
    main()
