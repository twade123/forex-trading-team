# 35B Validator Pattern Library Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Verify that the 35B local validator (Qwen3.5-35B-A3B-4bit + 35b_mlx adapter) can read charts using `pattern_library.md` as vocabulary, and decide whether the library + candle-fix stack is production-ready.

**Architecture:** One reusable MLX-call harness module (`bench_35b_validator.py`) that assembles a system prompt from a configurable skill list (identity prompt + any subset of skill files) and posts a multimodal chat/completions request to the MLX server at `http://127.0.0.1:11502`. Two driver scripts consume the harness: one for Test 1 (library ablation on 10 pattern teaching images) and one for Test 2 (50-chart stratified WIN/LOSS replay). Outputs are JSON + a single combined markdown report for the audit skill to consume.

**Tech Stack:** Python 3.11, `requests` (MLX HTTP), `base64` for image encoding, `random` with fixed seed 42, local SQLite for chart metadata if needed. No new dependencies — everything already installed in `~/myenv`.

**References:**
- Spec: `docs/superpowers/specs/2026-04-24-35b-pattern-library-audit-design.md`
- Live validator config: `Source/agents/team_setup.py:317`
- MLX URL + model id example: `Source/news_sentiment_scorer.py:21-22`
- Pattern teaching images: `Data/charts/teaching/patterns/pattern_*.png` (10 files, `pattern_01` through `pattern_17` — pattern_08/12/13/14/17 may be skipped or used; see Task 2)
- Labeled real-trade charts: `Data/charts/labeled/*.png` (417 files)
- Identity prompt: `Prompts/ghost_validator_v1.md` (143 lines)
- Skill files live loaded: `Skills/VALIDATOR_TOOLS.md`, `Skills/pattern_library.md`

---

## File structure

Files that will be created:

| Path | Purpose |
|---|---|
| `Source/scripts/audit_35b_bench.py` | Reusable harness — one function `call_35b(image_path, skill_files, timeout=90) -> dict` |
| `Source/scripts/audit_35b_test1_library_ablation.py` | Driver — runs 10 pattern images × 2 skill configs, writes `/tmp/stack_audit_2026-04-24_test1.json` |
| `Source/scripts/audit_35b_test2_labeled_replay.py` | Driver — samples 50 labeled charts, runs current stack, writes `/tmp/stack_audit_2026-04-24_test2.json` |
| `Source/scripts/audit_35b_report.py` | Merges test1+test2 JSONs into `/tmp/stack_audit_2026-04-24.md` |

Files that will be **read only** (no modification):

| Path | Why |
|---|---|
| `Prompts/ghost_validator_v1.md` | Identity prompt base for system-prompt construction |
| `Skills/VALIDATOR_TOOLS.md`, `Skills/pattern_library.md` | Skill files to include or omit |
| `Data/charts/teaching/patterns/pattern_*.png` | Test 1 inputs |
| `Data/charts/labeled/*.png` | Test 2 inputs |

Files that will NOT be touched: `team_setup.py`, `trading_cycle.py`, `wrappers.py`, `ghost_validator_v1.md`, the live serve_ui process.

---

### Task 1: Create the reusable 35B call harness

**Files:**
- Create: `Source/scripts/audit_35b_bench.py`

- [ ] **Step 1: Write the harness**

```python
"""35B validator bench harness — audit-only, does NOT touch live path.

Assembles a system prompt from ghost_validator_v1.md + an arbitrary list of
skill files, base64-encodes the chart image, and POSTs to the local MLX server.

Does not import from Source/ — keeps this script hermetic so it can run while
the live validator is busy.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import requests

MLX_URL = "http://127.0.0.1:11502/v1/chat/completions"
MLX_MODEL = "mlx-community/Qwen3.5-35B-A3B-4bit"

BASE_DIR = Path("<repo_root>")
PROMPTS_DIR = BASE_DIR / "Prompts"
SKILLS_DIR = BASE_DIR / "Skills"


def build_system_prompt(identity_file: str, skill_files: list[str]) -> str:
    """Concatenate identity + skill files the way team_setup.py does."""
    parts: list[str] = []
    identity_path = PROMPTS_DIR / identity_file
    parts.append(identity_path.read_text().strip())
    for sf in skill_files:
        p = SKILLS_DIR / sf
        parts.append(f"\n\n---\n\n# Skill: {sf}\n\n{p.read_text().strip()}")
    return "\n\n".join(parts)


def call_35b(
    image_path: Path,
    skill_files: list[str],
    identity_file: str = "ghost_validator_v1.md",
    user_text: str | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Call the 35B validator once. Returns dict with keys:
      verdict, direction, pattern, confidence (if model provides),
      raw_text, latency_s, error (if any).
    """
    system_prompt = build_system_prompt(identity_file, skill_files)
    img_bytes = image_path.read_bytes()
    img_b64 = base64.b64encode(img_bytes).decode("ascii")
    ut = user_text or (
        "Read this M15 chart. Return ONLY a json code block with fields: "
        "pair, direction_recent (UP/DOWN/SIDEWAYS), fan_state, pattern "
        "(use pattern_library.md vocabulary), verdict (TRADE_NOW/WATCH/SKIP), "
        "direction (BUY/SELL/null), reason (1-2 sentences)."
    )
    payload = {
        "model": MLX_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": ut},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    },
                ],
            },
        ],
        "max_tokens": 512,
        "temperature": 0.2,
    }
    t0 = time.time()
    try:
        resp = requests.post(MLX_URL, json=payload, timeout=timeout)
        latency = time.time() - t0
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
        return parse_response(raw, latency)
    except Exception as e:
        return {
            "error": str(e),
            "latency_s": round(time.time() - t0, 2),
            "raw_text": "",
            "verdict": None,
            "direction": None,
            "pattern": None,
            "confidence": None,
        }


def parse_response(raw: str, latency: float) -> dict[str, Any]:
    """Extract json fields even if model wraps them oddly."""
    import re
    text = raw.strip()
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not m:
        m = re.search(r"(\{[^{}]*\"verdict\"[^{}]*\})", text, re.DOTALL)
    parsed: dict[str, Any] = {}
    if m:
        try:
            parsed = json.loads(m.group(1))
        except Exception:
            pass
    return {
        "verdict": parsed.get("verdict"),
        "direction": parsed.get("direction"),
        "pattern": parsed.get("pattern"),
        "confidence": parsed.get("confidence"),
        "reason": parsed.get("reason"),
        "raw_text": text,
        "latency_s": round(latency, 2),
        "error": None if parsed else "parse_failed",
    }
```

- [ ] **Step 2: Smoke-test the harness with one pattern image**

Run:

```bash
cd "<repo_root>"
source ~/myenv/bin/activate
python3 -c "
from pathlib import Path
import sys
sys.path.insert(0, 'Source/scripts')
from audit_35b_bench import call_35b
r = call_35b(
    Path('Data/charts/teaching/patterns/pattern_10_bb_squeeze_breakout.png'),
    skill_files=['VALIDATOR_TOOLS.md', 'pattern_library.md'],
)
print({k: v for k, v in r.items() if k != 'raw_text'})
"
```

Expected: dict with non-null `verdict`, `pattern`, `direction`, and `latency_s` between 5 and 120 seconds, `error` is None or `parse_failed` but `raw_text` non-empty. If the HTTP call itself errored ("connection refused" or timeout), stop and debug the MLX server before proceeding.

- [ ] **Step 3: Commit the harness**

```bash
git add "Source/scripts/audit_35b_bench.py"
git commit -m "feat(scripts): add 35B validator audit harness

Hermetic bench harness for pattern library audit — assembles system
prompt from ghost_validator_v1.md + configurable skill list, posts
multimodal payload to MLX server at 11502, returns parsed verdict dict.

Does not touch live validator path. Script-only, no imports from Source/.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Write Test 1 driver (library ablation)

**Files:**
- Create: `Source/scripts/audit_35b_test1_library_ablation.py`

- [ ] **Step 1: Write the driver**

Select 10 pattern teaching images, each with an `expected_pattern` keyword and `expected_direction` to grade against:

```python
"""Test 1 — library ablation. 10 pattern teaching images × 2 runs (ON/OFF).

Output: /tmp/stack_audit_2026-04-24_test1.json

Scoring per image:
  LIB_HIT     = model's pattern field contains a term from pattern_library.md
  DIR_CORRECT = model's direction matches the expected bias for the pattern
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_35b_bench import call_35b

BASE = Path("<repo_root>")
PATTERNS_DIR = BASE / "Data/charts/teaching/patterns"
LIBRARY_VOCAB_FILE = BASE / "Skills/pattern_library.md"

OUT = Path("/tmp/stack_audit_2026-04-24_test1.json")

# 10 teaching images + expected_pattern keyword + expected_direction.
# Direction = "either" when the pattern has bilateral bias (e.g. triangles).
CASES: list[dict] = [
    {"file": "pattern_01_hammer_pin_bar.png",          "expected_pattern": "hammer",              "expected_direction": "BUY"},
    {"file": "pattern_02_engulfing_bullish.png",       "expected_pattern": "engulfing",           "expected_direction": "BUY"},
    {"file": "pattern_03_engulfing_bearish.png",       "expected_pattern": "engulfing",           "expected_direction": "SELL"},
    {"file": "pattern_04_morning_evening_star.png",    "expected_pattern": "star",                "expected_direction": "either"},
    {"file": "pattern_05_doji_extreme.png",            "expected_pattern": "doji",                "expected_direction": "either"},
    {"file": "pattern_06_ascending_triangle.png",      "expected_pattern": "triangle",            "expected_direction": "BUY"},
    {"file": "pattern_07_descending_triangle.png",     "expected_pattern": "triangle",            "expected_direction": "SELL"},
    {"file": "pattern_09_support_resistance_break.png","expected_pattern": "break",               "expected_direction": "either"},
    {"file": "pattern_10_bb_squeeze_breakout.png",     "expected_pattern": "bb_squeeze",          "expected_direction": "either"},
    {"file": "pattern_11_momentum_divergence.png",     "expected_pattern": "divergence",          "expected_direction": "either"},
]


def load_library_vocab() -> set[str]:
    """Extract named-pattern keywords from pattern_library.md (lowercased)."""
    text = LIBRARY_VOCAB_FILE.read_text().lower()
    seeds = [
        "hammer", "pin bar", "engulfing", "morning star", "evening star",
        "doji", "triangle", "ascending triangle", "descending triangle",
        "bb_squeeze", "bb squeeze", "squeeze", "fan_expansion", "fan expansion",
        "divergence", "head and shoulders", "double top", "double bottom",
        "w", "m", "shooting star", "marubozu", "three_white_soldiers",
        "three_black_crows", "flag", "pennant", "wedge", "channel",
        "support_resistance", "s/r",
    ]
    return {w for w in seeds if w in text}


def grade(result: dict, case: dict, vocab: set[str]) -> dict:
    patt = (result.get("pattern") or "").lower()
    direction = (result.get("direction") or "").upper()
    lib_hit = any(v in patt for v in vocab) if patt else False
    exp_p = case["expected_pattern"].lower()
    pattern_correct = exp_p in patt if patt else False
    exp_d = case["expected_direction"]
    dir_correct = (exp_d == "either") or (direction == exp_d)
    return {
        "lib_hit": lib_hit,
        "pattern_correct": pattern_correct,
        "dir_correct": dir_correct,
        "verdict": result.get("verdict"),
        "pattern": result.get("pattern"),
        "direction": result.get("direction"),
        "latency_s": result.get("latency_s"),
        "error": result.get("error"),
    }


def run() -> None:
    vocab = load_library_vocab()
    rows: list[dict] = []
    for case in CASES:
        img = PATTERNS_DIR / case["file"]
        for skills_name, skills in [
            ("ON",  ["VALIDATOR_TOOLS.md", "pattern_library.md"]),
            ("OFF", ["VALIDATOR_TOOLS.md"]),
        ]:
            print(f"  [{case['file']} | lib={skills_name}] running...", flush=True)
            result = call_35b(img, skill_files=skills)
            row = {"case": case["file"], "lib": skills_name, **grade(result, case, vocab)}
            print(f"    verdict={row['verdict']} pattern={row['pattern']!r} dir={row['direction']} "
                  f"lib_hit={row['lib_hit']} pat_correct={row['pattern_correct']} dir_correct={row['dir_correct']}",
                  flush=True)
            rows.append(row)
    OUT.write_text(json.dumps({"rows": rows, "vocab": sorted(vocab)}, indent=2))
    # Summary
    on = [r for r in rows if r["lib"] == "ON"]
    off = [r for r in rows if r["lib"] == "OFF"]
    def pct(rs: list[dict], key: str) -> str:
        if not rs: return "n/a"
        return f"{sum(1 for r in rs if r[key])}/{len(rs)} = {100 * sum(1 for r in rs if r[key]) // len(rs)}%"
    print("\n=== TEST 1 SUMMARY ===")
    print(f"  LIB ON:  lib_hit {pct(on,'lib_hit')}  pat_correct {pct(on,'pattern_correct')}  dir_correct {pct(on,'dir_correct')}")
    print(f"  LIB OFF: lib_hit {pct(off,'lib_hit')}  pat_correct {pct(off,'pattern_correct')}  dir_correct {pct(off,'dir_correct')}")
    print(f"  -> written {OUT}")


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Run Test 1**

```bash
cd "<repo_root>"
source ~/myenv/bin/activate
python3 Source/scripts/audit_35b_test1_library_ablation.py 2>&1 | tee /tmp/stack_audit_2026-04-24_test1.log
```

Expected: 20 lines of per-call output, then a `=== TEST 1 SUMMARY ===` with three ratios per lib state. Runtime 4-15 min (20 calls × 15-45 s each).

If TEST 1 SUMMARY shows `lib OFF` beating `lib ON` by more than 10 percentage points on `pattern_correct` or `dir_correct`, **STOP** — report to user, do NOT proceed to Task 4. The library is hurting accuracy; design needs revision.

- [ ] **Step 3: Commit Test 1 driver + results log**

```bash
git add "Source/scripts/audit_35b_test1_library_ablation.py"
git commit -m "feat(scripts): Test 1 library ablation driver

Runs 10 pattern teaching images × 2 skill configs (library ON/OFF).
Grades per-image: library_hit, pattern_correct, direction_correct.
Output: /tmp/stack_audit_2026-04-24_test1.json

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Write Test 2 driver (labeled WIN/LOSS replay)

**Files:**
- Create: `Source/scripts/audit_35b_test2_labeled_replay.py`

- [ ] **Step 1: Write the driver**

```python
"""Test 2 — 50-chart stratified WIN/LOSS replay against current 35B stack.

Output: /tmp/stack_audit_2026-04-24_test2.json

Sampling: stratified on outcome × pair × direction. Seed=42.
Target: 25 WIN + 25 LOSS. Across WIN bucket, cover >=4 pairs with both
BUY and SELL directions. Across LOSS bucket, same.

Per chart:
  PASS if:
    outcome=WIN  and verdict in {TRADE_NOW, WATCH} and direction == truth
    outcome=LOSS and (verdict == SKIP or direction != truth)
  FAIL otherwise.
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_35b_bench import call_35b

BASE = Path("<repo_root>")
LABELED_DIR = BASE / "Data/charts/labeled"
OUT = Path("/tmp/stack_audit_2026-04-24_test2.json")

SKILLS_LIVE = ["VALIDATOR_TOOLS.md", "pattern_library.md"]

FILENAME_RE = re.compile(
    r"^(?P<pair>[A-Z]{3}_[A-Z]{3})_(?P<dir>buy|sell)_(?P<outcome>WIN|LOSS)_(?P<pips>[+\-]?\d+)p_(?P<t>\d+)\.png$"
)


def parse_filename(name: str) -> dict | None:
    m = FILENAME_RE.match(name)
    if not m:
        return None
    d = m.groupdict()
    return {
        "pair": d["pair"],
        "direction": "BUY" if d["dir"] == "buy" else "SELL",
        "outcome": d["outcome"],
        "pips": int(d["pips"]),
        "file": name,
    }


def stratified_sample(n_win: int, n_loss: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    all_meta = []
    for p in sorted(LABELED_DIR.glob("*.png")):
        m = parse_filename(p.name)
        if m:
            all_meta.append(m)
    wins = [x for x in all_meta if x["outcome"] == "WIN"]
    losses = [x for x in all_meta if x["outcome"] == "LOSS"]
    rng.shuffle(wins); rng.shuffle(losses)

    def pick(pool, target):
        picked = []
        pair_dir_seen = defaultdict(int)
        # First pass — pick diverse pairs/directions.
        for x in pool:
            k = (x["pair"], x["direction"])
            if pair_dir_seen[k] < max(1, target // 8):
                picked.append(x); pair_dir_seen[k] += 1
                if len(picked) >= target:
                    break
        # Top up with random picks if diversity pass underfilled.
        remaining = [x for x in pool if x not in picked]
        while len(picked) < target and remaining:
            picked.append(remaining.pop(0))
        return picked[:target]

    return pick(wins, n_win) + pick(losses, n_loss)


def grade(result: dict, meta: dict) -> str:
    verdict = (result.get("verdict") or "").upper()
    direction = (result.get("direction") or "").upper()
    truth_dir = meta["direction"]
    if meta["outcome"] == "WIN":
        return "PASS" if verdict in ("TRADE_NOW", "WATCH") and direction == truth_dir else "FAIL"
    # LOSS
    return "PASS" if verdict == "SKIP" or (direction and direction != truth_dir) else "FAIL"


def run() -> None:
    sample = stratified_sample(n_win=25, n_loss=25, seed=42)
    print(f"Sample size: {len(sample)}. Seed=42.")
    rows: list[dict] = []
    for i, meta in enumerate(sample, 1):
        img = LABELED_DIR / meta["file"]
        result = call_35b(img, skill_files=SKILLS_LIVE)
        mark = grade(result, meta)
        rows.append({
            **meta,
            "verdict": result.get("verdict"),
            "got_direction": result.get("direction"),
            "pattern": result.get("pattern"),
            "confidence": result.get("confidence"),
            "latency_s": result.get("latency_s"),
            "error": result.get("error"),
            "grade": mark,
        })
        print(f"  [{i:02d}/{len(sample)}] {meta['outcome']} {meta['pair']} {meta['direction']} "
              f"-> verdict={result.get('verdict')} dir={result.get('direction')} "
              f"pat={result.get('pattern')!r} | {mark}",
              flush=True)
    OUT.write_text(json.dumps({"rows": rows}, indent=2))

    # Summary
    wins = [r for r in rows if r["outcome"] == "WIN"]
    losses = [r for r in rows if r["outcome"] == "LOSS"]
    win_pass = sum(1 for r in wins if r["grade"] == "PASS")
    loss_pass = sum(1 for r in losses if r["grade"] == "PASS")
    total_pass = win_pass + loss_pass
    total = len(rows)
    print("\n=== TEST 2 SUMMARY ===")
    print(f"  Overall: {total_pass}/{total} = {100*total_pass/total:.0f}%")
    print(f"  Wins caught:   {win_pass}/{len(wins)} = {100*win_pass/len(wins):.0f}%")
    print(f"  Losses avoided:{loss_pass}/{len(losses)} = {100*loss_pass/len(losses):.0f}%")
    pat_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("pattern"):
            pat_counts[str(r["pattern"]).lower()] += 1
    print(f"  Distinct patterns named: {len(pat_counts)}")
    top = sorted(pat_counts.items(), key=lambda x: -x[1])[:5]
    print(f"  Top 5: {top}")
    print(f"  -> written {OUT}")


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Run Test 2**

```bash
cd "<repo_root>"
source ~/myenv/bin/activate
python3 Source/scripts/audit_35b_test2_labeled_replay.py 2>&1 | tee /tmp/stack_audit_2026-04-24_test2.log
```

Expected: 50 lines of per-call output, then a `=== TEST 2 SUMMARY ===`. Runtime 15-40 min.

The prior 8-chart benchmark scored 6/8 = 75%. Overall pass rate on this 50-chart bench should land within ±5pp of 75% or better. If it's significantly worse (e.g. <60%), flag loudly in the report — the stack is likely over-triggering.

- [ ] **Step 3: Commit Test 2 driver + results log**

```bash
git add "Source/scripts/audit_35b_test2_labeled_replay.py"
git commit -m "feat(scripts): Test 2 labeled WIN/LOSS replay driver

Stratified 50-chart sample from Data/charts/labeled/ (25 WIN + 25 LOSS),
seed=42 for reproducibility. Runs each through current 35B stack.
WIN pass = TRADE_NOW/WATCH with correct direction.
LOSS pass = SKIP or opposite direction.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Write combined audit report generator

**Files:**
- Create: `Source/scripts/audit_35b_report.py`

- [ ] **Step 1: Write the report generator**

```python
"""Merge Test 1 + Test 2 JSON outputs into a human-readable markdown report
at /tmp/stack_audit_2026-04-24.md. This is what feeds into /trade-audit-repair.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

T1 = Path("/tmp/stack_audit_2026-04-24_test1.json")
T2 = Path("/tmp/stack_audit_2026-04-24_test2.json")
OUT = Path("/tmp/stack_audit_2026-04-24.md")


def pct(num: int, den: int) -> str:
    return "n/a" if not den else f"{num}/{den} = {100*num//den}%"


def render() -> str:
    t1 = json.loads(T1.read_text())["rows"] if T1.exists() else []
    t2 = json.loads(T2.read_text())["rows"] if T2.exists() else []

    lines: list[str] = []
    lines.append("# 35B Validator Pattern Library Audit — 2026-04-24")
    lines.append("")
    lines.append(f"- Test 1 calls: {len(t1)}")
    lines.append(f"- Test 2 calls: {len(t2)}")
    lines.append("")

    # === Test 1 ===
    lines.append("## Test 1 — Library ablation")
    lines.append("")
    for lib in ("ON", "OFF"):
        rs = [r for r in t1 if r["lib"] == lib]
        hit  = sum(1 for r in rs if r["lib_hit"])
        pc   = sum(1 for r in rs if r["pattern_correct"])
        dc   = sum(1 for r in rs if r["dir_correct"])
        err  = sum(1 for r in rs if r.get("error"))
        lines.append(f"**LIB {lib}** ({len(rs)} calls, errors {err}):")
        lines.append(f"- library_vocab_hit: {pct(hit, len(rs))}")
        lines.append(f"- pattern_name_correct: {pct(pc, len(rs))}")
        lines.append(f"- direction_correct: {pct(dc, len(rs))}")
        lines.append("")
    lines.append("### Per-case table (Test 1)")
    lines.append("")
    lines.append("| Case | Lib | Verdict | Pattern | Dir | lib_hit | pat_correct | dir_correct |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in t1:
        lines.append(f"| {r['case']} | {r['lib']} | {r['verdict']} | {r['pattern']!r} | {r['direction']} | {r['lib_hit']} | {r['pattern_correct']} | {r['dir_correct']} |")
    lines.append("")

    # === Test 2 ===
    lines.append("## Test 2 — 50-chart WIN/LOSS replay")
    lines.append("")
    wins   = [r for r in t2 if r["outcome"] == "WIN"]
    losses = [r for r in t2 if r["outcome"] == "LOSS"]
    win_pass  = sum(1 for r in wins if r["grade"] == "PASS")
    loss_pass = sum(1 for r in losses if r["grade"] == "PASS")
    total = len(t2); total_pass = win_pass + loss_pass
    lines.append(f"- Overall: {pct(total_pass, total)}")
    lines.append(f"- Wins caught: {pct(win_pass, len(wins))}")
    lines.append(f"- Losses avoided: {pct(loss_pass, len(losses))}")
    lines.append(f"- Prior 8-chart baseline: 6/8 = 75%")
    lines.append("")

    # By pair
    lines.append("### By pair (Test 2)")
    lines.append("")
    lines.append("| Pair | n | Pass | Pass% |")
    lines.append("|---|---|---|---|")
    by_pair = defaultdict(list)
    for r in t2:
        by_pair[r["pair"]].append(r)
    for pair in sorted(by_pair):
        rs = by_pair[pair]
        p = sum(1 for r in rs if r["grade"] == "PASS")
        lines.append(f"| {pair} | {len(rs)} | {p} | {pct(p, len(rs))} |")
    lines.append("")

    # Confusion
    lines.append("### Confusion (Test 2)")
    lines.append("")
    c = defaultdict(int)
    for r in t2:
        key = (r["outcome"], (r.get("verdict") or "NONE").upper(),
               (r.get("got_direction") or "NULL").upper())
        c[key] += 1
    lines.append("| Truth outcome | Model verdict | Model dir | n |")
    lines.append("|---|---|---|---|")
    for (o, v, d), n in sorted(c.items(), key=lambda x: -x[1]):
        lines.append(f"| {o} | {v} | {d} | {n} |")
    lines.append("")

    # Pattern vocab usage
    lines.append("### Pattern naming (Test 2)")
    lines.append("")
    names = defaultdict(int)
    for r in t2:
        if r.get("pattern"):
            names[str(r["pattern"]).lower()] += 1
    lines.append(f"- Distinct pattern names returned: {len(names)}")
    for nm, n in sorted(names.items(), key=lambda x: -x[1])[:15]:
        lines.append(f"- `{nm}` × {n}")
    lines.append("")

    # FAIL cases detail (for audit skill to chew on)
    fails = [r for r in t2 if r["grade"] == "FAIL"]
    lines.append(f"### Failing cases ({len(fails)})")
    lines.append("")
    lines.append("| File | Truth | Got verdict | Got dir | Pattern |")
    lines.append("|---|---|---|---|---|")
    for r in fails:
        lines.append(f"| {r['file']} | {r['outcome']} {r['direction']} | {r.get('verdict')} | {r.get('got_direction')} | {r.get('pattern')!r} |")
    lines.append("")

    lines.append("## Verdict on stack")
    lines.append("")
    verdict_lines: list[str] = []
    if t1:
        on = [r for r in t1 if r["lib"] == "ON"]
        off = [r for r in t1 if r["lib"] == "OFF"]
        on_pc = sum(1 for r in on if r["pattern_correct"])
        off_pc = sum(1 for r in off if r["pattern_correct"])
        delta = on_pc - off_pc
        sign = "helps" if delta > 0 else ("hurts" if delta < 0 else "neutral")
        verdict_lines.append(f"- Library {sign} pattern naming (delta {delta} of {len(on)} cases)")
    if t2:
        rate = (total_pass / total) if total else 0
        if rate >= 0.75:
            verdict_lines.append(f"- Test 2 meets or exceeds 75% bar ({100*rate:.0f}%)")
        elif rate >= 0.60:
            verdict_lines.append(f"- Test 2 below 75% bar but not catastrophic ({100*rate:.0f}%)")
        else:
            verdict_lines.append(f"- Test 2 regression — investigate urgently ({100*rate:.0f}%)")
    lines.extend(verdict_lines or ["- insufficient data"])
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    OUT.write_text(render())
    print(f"Wrote {OUT}")
```

- [ ] **Step 2: Run the report generator**

```bash
cd "<repo_root>"
source ~/myenv/bin/activate
python3 Source/scripts/audit_35b_report.py
cat /tmp/stack_audit_2026-04-24.md | head -80
```

Expected: `Wrote /tmp/stack_audit_2026-04-24.md` then the first 80 lines of the report with the Test 1 summary visible.

- [ ] **Step 3: Commit the report generator**

```bash
git add "Source/scripts/audit_35b_report.py"
git commit -m "feat(scripts): 35B audit report generator

Merges Test 1 + Test 2 JSON outputs into a single markdown report
at /tmp/stack_audit_2026-04-24.md. Feeds /trade-audit-repair.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Write findings to vault

**Files:**
- Create: `~/jarvis/knowledge/agents/claude-code/2026-04-24-35b-pattern-library-audit-results.md`

- [ ] **Step 1: Write the vault entry**

Use the CLI so the FTS index rebuilds:

```bash
source ~/myenv/bin/activate
python3 ~/jarvis/knowledge/vault_cli.py \
    --agent "claude-code" \
    --type "discovery" \
    --summary "35B validator pattern-library audit — <fill in concrete verdict from /tmp/stack_audit_2026-04-24.md>" \
    --context "Test 1 (library ablation): LIB_ON vs LIB_OFF on 10 pattern teaching images. Test 2 (50-chart stratified WIN/LOSS replay): overall pass rate vs prior 6/8 = 75% baseline. Full report at /tmp/stack_audit_2026-04-24.md. Next: /trade-audit-repair pass to propose any prompt/guardrail changes (NO code changes without backtest gate)." \
    --tags "35b,validator,pattern-library,audit,post-distillation"
```

The concrete `--summary` line must include two numbers: Test 1 library delta (e.g. `+2/10 pattern-name accuracy`) and Test 2 overall pass rate (e.g. `38/50 = 76%`). Pull them from the generated report.

- [ ] **Step 2: Verify the vault entry is indexed**

```bash
sqlite3 ~/Jarvis/knowledge/_index.db \
  "SELECT path FROM fts_content WHERE fts_content MATCH '35b audit pattern library' LIMIT 5"
```

Expected: the new file appears in the results.

---

### Task 6: Run /trade-audit-repair on the findings

- [ ] **Step 1: Invoke the skill with the audit report as context**

In this chat, call:

```
/trade-audit-repair Review /tmp/stack_audit_2026-04-24.md — the 35B validator pattern-library audit report. Identify: (1) which failing cases share a root cause (e.g. fan-state misread, over-triggering on one-bar candle events, ignoring library vocabulary), (2) whether the library adds or subtracts accuracy vs ablation, (3) whether any proposed prompt/guardrail change would address >=3 of the failing cases. Do not ship any code change — per skill rules, proposals only, backtest gate applies.
```

- [ ] **Step 2: Record the skill's output summary**

The skill will produce a set of candidate fixes ranked by evidence weight. Append them to the vault entry from Task 5 as a "Proposed fixes (not shipped)" section. Do NOT modify validator code in this task — proposals go to Tim for sign-off, then to a backtest gate, then to a separate plan.

---

## Self-review

### Spec coverage

- Test 1 (library ablation) → Task 2 ✓
- Test 2 (50-chart stratified replay) → Task 3 ✓
- Combined audit report at `/tmp/stack_audit_2026-04-24.md` → Task 4 ✓
- Vault write → Task 5 ✓
- `/trade-audit-repair` pass → Task 6 ✓
- Guardrail: Test 1 first, stop if library hurts → Task 2 Step 2 ✓
- Guardrail: no code changes without backtest → Task 6 Step 2 ✓
- Seed 42 for reproducibility → Task 3 Step 1 ✓
- Harness does not touch live path → Task 1 docstring + "does not import from Source/" note ✓

### Placeholder scan

No TBDs. Task 5 Step 1 has a "`<fill in concrete verdict>`" placeholder — that's intentional because the number comes from the just-generated report, and the plan tells the engineer exactly which file to pull it from and which two metrics to include.

### Type consistency

- Harness returns `dict[str, Any]` with fixed keys: `verdict, direction, pattern, confidence, reason, raw_text, latency_s, error`. Both driver scripts consume those same keys. ✓
- `parse_filename` returns `dict` with keys `pair, direction, outcome, pips, file`. Consumed consistently by `stratified_sample` and `grade` and the output rows. ✓
- Chart filename grammar: `^[A-Z]{3}_[A-Z]{3}_(buy|sell)_(WIN|LOSS)_[+-]?\d+p_\d+\.png$`. Verified against sampled filenames in the archive listing.

No issues found — proceeding to execution handoff.
