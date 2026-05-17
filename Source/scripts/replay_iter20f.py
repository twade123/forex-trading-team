"""replay_iter20f.py — iter 20f: LATE-ENTRY GATE (Stage 1-4 + Exit-marker timing).

Same stack as iter 20d PLUS a new "LATE-ENTRY GATE" section in the prompt
that teaches the validator to stage the chart 1-4 by candle position vs
E21/E55 and SKIP/WATCH stages 2-4 even when continuation count is high.

Cohort: iter 20d 19-trade + 5 new late-entry losers (24 trades).

Pass criteria:
- ≥9/11 winners stay TRADE_NOW
- ≥4/6 NEW late-entry losers downgrade to WATCH or SKIP
- Net P&L ≥ iter 20d baseline (+48.1p)

Run:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python3 scripts/replay_iter20f.py
"""

# Iter 20d stack docstring follows:
"""

Stack of changes vs iter 16 v2 baseline:
1. Swing-trace overlay (red/green dots + connecting line)
2. Pattern detectors fire for each chart, tunable via DETECTOR_ENABLED
3. Detected patterns labeled on the chart at fire-bar with verbatim names
4. Prompt dynamically includes "DETECTED PATTERNS" section with library quotes
5. Session-gate awareness (AUD UTC 21-22 weekday + existing rules)
6. Confirmation-candle filter + invalidation-tripwire on patterns (iter 20)
7. Scout history backfill (as-of-entry-time, non-leaky) (iter 20)
8. Iter 20a: badge thresholds n≥5 + strengthened scout guardrail language
9. Iter 20c: 6-signal CONTINUATION composite (fan ordering + candle-vs-all-EMAs +
   candle color + fan velocity + BB state + band-tracing); 4+ of 6 confirm =
   CONTINUATION, deep RSI alone insufficient to SKIP. Recovered 13362 BAD→IDEAL.
10. **NEW iter 20d**: PATTERN-CONFLICT VETO inside the continuation composite —
    confirmed reversal pattern at entry bar against trade direction (e.g.
    Bearish Engulfing on a BUY) subtracts 2 from continuation count.
    Effectively forces WATCH unless 6/6 still confirm post-veto. Targets 13843
    regression (TRADE_NOW BUY despite Bearish Engulfing + Doji gravestone).

Run:
    cd "<repo_root>/Source"
    source ~/myenv/bin/activate
    python3 scripts/replay_iter20d.py
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

from scripts.oanda_chart_pattern_regen import regenerate_chart_with_patterns
from scripts.pattern_library_quotes import build_pattern_section
from scripts.pattern_detectors import DETECTOR_ENABLED

PROMPT_PATH = "/tmp/prompt_variants/iter20f.md"
INDICATOR_BLOCKS_JSON = "/tmp/cohort_indicator_blocks.json"
LOCAL_ENDPOINT = "http://127.0.0.1:11502/v1/chat/completions"
LOCAL_MODEL_NAME = "mlx-community/Qwen3.5-35B-A3B-4bit"

OUT_RESULTS = "/tmp/iter20f_results.json"
OUT_LOG = "/tmp/iter20f_replay.log"
PATTERN_CHART_DIR = "/tmp/replay_charts_pattern_20f"

COHORT = [
    # Original iter 20d 19-trade cohort (8 winners + 11 losers — keep all)
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
    # NEW iter 20f late-entry losers — recent live losses after iter 20d deploy
    ("13913", "EUR_GBP", "SELL", "2026-05-08T15:23:00+00:00", -33.2, "loser_late"),
    ("14088", "EUR_CHF", "BUY",  "2026-05-11T09:32:00+00:00", -13.9, "loser_late"),
    ("14249", "GBP_JPY", "BUY",  "2026-05-11T17:21:00+00:00", -48.9, "loser_late"),
    ("14431", "AUD_JPY", "BUY",  "2026-05-12T05:02:00+00:00", -22.1, "loser_late"),
    ("14485", "EUR_AUD", "BUY",  "2026-05-12T08:02:00+00:00", -27.2, "loser_late"),
]


def load_chart_b64(p):
    if not p or not os.path.exists(p):
        return None, "image/png"
    raw = open(p, "rb").read()
    media = "image/jpeg" if raw[:3] == b"\xff\xd8\xff" else "image/png"
    return base64.b64encode(raw).decode(), media


def build_task_text(pair, direction, indicator_block, pattern_section):
    pd = pair.replace("_", "/")
    pattern_part = f"\n\n{pattern_section}" if pattern_section else ""
    base = (
        f"M15 chart — {pd}. Scout identified a {direction} setup. "
        f"Read the chart fresh and form YOUR OWN thesis from the structure you see.\n\n"
        f"Return ONLY a ```json code block with: verdict (TRADE_NOW/WATCH/SKIP), "
        f"direction (BUY/SELL), confidence (0-10 INTEGER), reasoning (start with CHART READ:), "
        f"re_entry_conditions (list of {{field, op, value, reason}} dicts), "
        f"snipe_entry_zone, snipe_invalidation, snipe_target.\n\n"
        f"After analyzing the chart, respond with ONLY a ```json code block. "
        f"No prose outside the JSON."
    )
    return f"{indicator_block}{pattern_part}\n\n---\n\n{base}"


def call_35b(system_prompt, task_text, chart_b64, chart_media="image/png"):
    content = []
    if chart_b64:
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{chart_media};base64,{chart_b64}"}})
    content.append({"type": "text", "text": task_text})
    payload = json.dumps({
        "model": LOCAL_MODEL_NAME,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": content}],
        "temperature": 0, "max_tokens": 2500, "stream": False,
    }).encode()
    req = urllib.request.Request(LOCAL_ENDPOINT, data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    out = data["choices"][0]["message"].get("content", "") or ""
    return re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL).strip()


def parse_verdict(raw):
    cleaned = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", cleaned)
    js = m.group(1) if m else None
    if not js:
        i = cleaned.find("{")
        if i == -1:
            return {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": ""}
        depth, end = 0, -1
        for k, ch in enumerate(cleaned[i:]):
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + k + 1; break
        js = cleaned[i:end] if end > 0 else None
    if not js:
        return {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": ""}
    try:
        d = json.loads(js)
        return {"verdict": d.get("verdict", "UNKNOWN"),
                "direction": d.get("direction"),
                "confidence": d.get("confidence"),
                "reasoning": str(d.get("reasoning", ""))[:300]}
    except json.JSONDecodeError:
        return {"verdict": "PARSE_ERROR", "direction": None, "confidence": None, "reasoning": ""}


def bucket(category, verdict, direction, trade_dir):
    v = (verdict or "").upper(); d = (direction or "").upper(); td = trade_dir.upper()
    if category == "winner":
        if v == "TRADE_NOW" and d == td: return "IDEAL"
        if v == "WATCH": return "OK"
        return "BAD"
    else:
        if v == "SKIP": return "IDEAL"
        if v == "WATCH": return "OK"
        if v == "TRADE_NOW": return "BAD"
        return "BAD"


def main():
    log_lines = []
    def log(msg):
        print(msg, flush=True); log_lines.append(msg)

    log("=" * 70)
    log("ITER 20a — scout-history threshold n≥3 → n≥5, structural-primary guardrail")
    log("=" * 70)
    log(f"Prompt: {PROMPT_PATH}")
    log(f"Detectors enabled: {DETECTOR_ENABLED}")
    log(f"Chart source: {PATTERN_CHART_DIR} (pattern overlay)")
    system_prompt = Path(PROMPT_PATH).read_text().strip()
    indicator_blocks = json.load(open(INDICATOR_BLOCKS_JSON))
    log(f"System prompt size: {len(system_prompt)} chars")
    log("")

    os.makedirs(PATTERN_CHART_DIR, exist_ok=True)
    results = []
    t0 = time.time()
    for trade_id, pair, direction, entry_iso, actual_pips, category in COHORT:
        log(f"\n[{trade_id}] {pair} {direction} | actual: {actual_pips:+}p ({category})")
        ind = indicator_blocks.get(trade_id)
        if not ind or "block_text" not in ind:
            log(f"  ERROR: no indicator block for {trade_id}"); continue
        chart_out = f"{PATTERN_CHART_DIR}/{trade_id}_{pair}_pattern.png"
        chart_path, fires = regenerate_chart_with_patterns(pair, entry_iso, chart_out)
        if not chart_path:
            log(f"  ERROR: pattern chart regen failed for {trade_id}")
            continue
        pattern_section = build_pattern_section(fires)
        chart_b64, chart_media = load_chart_b64(chart_path)
        sess = "BLOCKED" if ind.get("session_blocked") else "OPEN"
        pattern_names = [f["name"] for f in fires]
        scout = ind.get("scout_history") or {}
        scout_n = scout.get("trade_count", 0)
        scout_wr = scout.get("win_rate")
        log(f"  Chart: {chart_path} ({os.path.getsize(chart_path)//1024}KB) | session={sess}")
        log(f"  Patterns: {pattern_names if pattern_names else 'none'}")
        log(f"  Scout: n={scout_n} WR={scout_wr}%")
        log(f"  Indicator: phase={ind.get('phase')} fan={ind.get('fan',{}).get('fan_direction')} "
            f"{ind.get('fan',{}).get('fan_state')}")
        task = build_task_text(pair, direction, ind["block_text"], pattern_section)
        try:
            tc = time.time()
            raw = call_35b(system_prompt, task, chart_b64, chart_media)
            dt = time.time() - tc
        except Exception as e:
            log(f"  ERROR calling 35B: {e}")
            results.append({"trade_id": trade_id, "pair": pair, "direction": direction,
                            "actual_pips": actual_pips, "category": category,
                            "verdict": "ERROR", "verdict_direction": None, "confidence": None,
                            "reasoning_snippet": str(e), "bucket": "BAD",
                            "patterns": pattern_names})
            continue
        parsed = parse_verdict(raw)
        v = parsed.get("verdict"); vd = parsed.get("direction"); cf = parsed.get("confidence")
        rs = (parsed.get("reasoning") or "")[:300]
        bk = bucket(category, v, vd, direction)
        results.append({"trade_id": trade_id, "pair": pair, "direction": direction,
                        "actual_pips": actual_pips, "category": category,
                        "verdict": v, "verdict_direction": vd, "confidence": cf,
                        "reasoning_snippet": rs, "bucket": bk,
                        "session_blocked": ind.get("session_blocked", False),
                        "patterns": pattern_names,
                        "scout_n": scout_n, "scout_wr": scout_wr,
                        "elapsed_s": round(dt, 1)})
        log(f"  → {v} {vd} conf={cf} [{bk}] ({dt:.1f}s)")
        Path(OUT_RESULTS).write_text(json.dumps(results, indent=2))
        Path(OUT_LOG).write_text("\n".join(log_lines))

    elapsed_min = (time.time() - t0) / 60
    log("")
    log("=" * 70)
    ideal = sum(1 for r in results if r["bucket"] == "IDEAL")
    ok = sum(1 for r in results if r["bucket"] == "OK")
    bad = sum(1 for r in results if r["bucket"] == "BAD")
    raw_pips = sum(r["actual_pips"] for r in results if r["bucket"] == "IDEAL")
    log(f"ITER 20a SUMMARY: IDEAL={ideal}  OK={ok}  BAD={bad}  Acceptable={ideal+ok}/19")
    log(f"  Raw pips (IDEAL only): {raw_pips:+.1f}p")
    log(f"  Baseline iter 16 v2: 10 IDEAL + 5 OK + 4 BAD = 15/19 (raw +18.9p)")
    log(f"  Iter 18b session:     8 IDEAL + 9 OK + 2 BAD = 17/19 (raw -2.1p)")
    log(f"  Iter 19 patterns:     9 IDEAL + 9 OK + 1 BAD = 18/19 (raw +5.6p)")
    log(f"  Iter 20 filters:      7 IDEAL +11 OK + 1 BAD = 18/19 (raw +30.1p)")
    log(f"Elapsed: {elapsed_min:.1f} min")
    log("=" * 70)
    Path(OUT_RESULTS).write_text(json.dumps(results, indent=2))
    Path(OUT_LOG).write_text("\n".join(log_lines))


if __name__ == "__main__":
    main()
