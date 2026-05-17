"""
Ghost Validator Batch Replay — reconstruct saved inputs, call 35B, compare to Anthropic.

Pulls validator inputs from vision_training_data (chart PNG + input_prompt) and the
validator system prompt (validator_v4.md). Sends the exact same teaching images + live
chart + prompt to the 35B VLM via OpenAI-compatible API. Compares verdicts.

Usage:
    python -m optimizer.ghost_replay [--date 2026-04-14] [--force]
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("optimizer.ghost_replay")

TRADING_DB = os.path.expanduser("~/Jarvis/Database/v2/trading_forex.db")
FLIGHT_DB = os.path.expanduser(
    "~/Jarvis/Forex Trading Team/Source/flight_recorder.db"
)
MLX_SCRIPT = os.path.expanduser("~/Jarvis/scripts/mlx_servers.sh")
MLX_VLM_SCRIPT = os.path.expanduser(
    "~/Jarvis/scripts/mlx_vlm_server_with_tools.py"
)
VALIDATOR_PROMPT_PATH = os.path.expanduser(
    "~/Jarvis/Forex Trading Team/Prompts/validator_v4.md"
)
# Lean-mode prompt stack — matches team_setup.py:_load_local_agent_prompt()
# Used for the 35B distilled path where the heavy validator_v4.md is too large
# for the distilled model's prefill budget. IDENTITY + SKILLS are concatenated
# using the same separator team_setup.py uses so the model sees the same prompt
# shape live and in replay.
LEAN_PROMPT_PATH = os.path.expanduser(
    "~/Jarvis/Forex Trading Team/Prompts/ghost_validator_v1.md"
)
SKILLS_DIR = os.path.expanduser("~/Jarvis/Forex Trading Team/Skills")
LEAN_SKILL_FILES = ["VALIDATOR_TOOLS.md", "pattern_library.md"]
TEACHING_DIR = os.path.expanduser(
    "~/Jarvis/Forex Trading Team/Data/charts/teaching"
)
LOCAL_MODEL_PORT = 11503  # serving gateway → MLX 35B (was direct 11502)
LOCAL_MODEL_NAME = "mlx-community/Qwen3.5-35B-A3B-4bit"
LOCAL_MODEL_ENDPOINT = f"http://127.0.0.1:{LOCAL_MODEL_PORT}/v1/chat/completions"
ADAPTER_PATH = os.path.expanduser("~/Jarvis/models/adapters/35b_mlx")

# Module-level model cache — loaded once, reused across calls
_model_cache = {"model": None, "processor": None, "config": None}

# Teaching image manifest — same order as trading_cycle.py _V4_TEACHING_MANIFEST
TEACHING_MANIFEST = [
    {"file": "tim_teach_stage1_fan_entry.png",
     "description": "PHASE 2.5 ENTRY EXAMPLE — EUR/AUD LONG"},
    {"file": "tim_teach_euraud_phase25_e100_retest.png",
     "description": "RETRACEMENT ENTRY LESSON — PRICE ON E100/E55 = BUY ZONE"},
    {"file": "tim_teach_eurchf_bearish_fan_flip.png",
     "description": "TRADE EXAMPLE — EUR/CHF SHORT: Bearish fan flip after BB squeeze"},
    {"file": "tim_teach_1.png",
     "description": "TRADE EXAMPLE — Fan opening wide, BBs expanding"},
    {"file": "tim_teach_2.png",
     "description": "TRADE EXAMPLE — Clear downward expansion after cross"},
    {"file": "tim_teach_3.png",
     "description": "SKIP EXAMPLE — TANGLED FAN (not retracement)"},
    {"file": "tim_teach_4.png",
     "description": "RETRACEMENT SETUP — PEAKED FAN + BBs CONTRACTING"},
    {"file": "trade_364_USD_JPY_SHORT_WIN_+190p.png",
     "description": "TRADE EXAMPLE — USD_JPY SHORT +190 pips: Perfect expansion"},
    {"file": "trade_103_AUD_JPY_SHORT_LOSS_-34p.png",
     "description": "SKIP EXAMPLE — AUD_JPY SHORT -34 pips LOSS: Choppy"},
]

MAX_IMAGE_DIM = 1920


# ---------------------------------------------------------------------------
# Image loading
# ---------------------------------------------------------------------------

def _load_image_as_b64(path: str) -> Optional[str]:
    """Load an image file, resize if needed, return base64 PNG string."""
    if not os.path.exists(path):
        logger.warning("Image file missing: %s", path)
        return None
    try:
        from PIL import Image
        with open(path, "rb") as f:
            raw = f.read()
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if max(w, h) > MAX_IMAGE_DIM:
            if w >= h:
                new_w, new_h = MAX_IMAGE_DIM, int(h * MAX_IMAGE_DIM / w)
            else:
                new_h, new_w = MAX_IMAGE_DIM, int(w * MAX_IMAGE_DIM / h)
            img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.standard_b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.error("Failed to load image %s: %s", path, e)
        return None


def load_teaching_images() -> List[Dict[str, str]]:
    """Load all teaching images as OpenAI-format content blocks."""
    images = []
    for entry in TEACHING_MANIFEST:
        fpath = os.path.join(TEACHING_DIR, entry["file"])
        b64 = _load_image_as_b64(fpath)
        if b64:
            images.append({
                "b64": b64,
                "description": entry["description"],
            })
    logger.info("Loaded %d/%d teaching images", len(images), len(TEACHING_MANIFEST))
    return images


# ---------------------------------------------------------------------------
# Build OpenAI messages with vision
# ---------------------------------------------------------------------------

def _build_openai_content_blocks(
    teaching_images: List[Dict],
    chart_b64: Optional[str],
    task_text: str,
) -> list:
    """Build OpenAI multi-modal content array: teaching images + live chart + text."""
    blocks = []

    # Teaching images first (same order as Anthropic gets them)
    for img in teaching_images:
        blocks.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{img['b64']}",
            },
        })
        blocks.append({
            "type": "text",
            "text": img["description"],
        })

    # Live chart
    if chart_b64:
        blocks.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{chart_b64}",
            },
        })
        blocks.append({"type": "text", "text": "LIVE CHART — analyze this chart."})

    # Main task text
    blocks.append({"type": "text", "text": task_text})
    return blocks


# ---------------------------------------------------------------------------
# Validator prompt + task string reconstruction
# ---------------------------------------------------------------------------

def _load_validator_system_prompt(
    prompt_mode: str = "lean",
    ablate_library: bool = False,
) -> str:
    """Assemble the validator system prompt.

    Args:
        prompt_mode: "lean" = ghost_validator_v1.md + Skills/* (live 35B stack,
            default); "heavy" = validator_v4.md (Opus-era prompt).
        ablate_library: When True and prompt_mode="lean", omit pattern_library.md
            from the skill files — lets us measure the library's contribution.
    """
    if prompt_mode == "heavy":
        try:
            with open(VALIDATOR_PROMPT_PATH, "r") as f:
                return f.read()
        except FileNotFoundError:
            logger.error("Heavy validator prompt not found: %s", VALIDATOR_PROMPT_PATH)
            return "You are a forex trade validator. Analyze the chart and return a JSON verdict."

    # lean mode — mirror team_setup.py:_load_local_agent_prompt() exactly
    parts: list[str] = []
    try:
        with open(LEAN_PROMPT_PATH, "r") as f:
            parts.append(f.read().strip())
    except FileNotFoundError:
        logger.error("Lean validator prompt not found: %s", LEAN_PROMPT_PATH)
        return "You are a forex trade validator. Analyze the chart and return a JSON verdict."

    for skill_name in LEAN_SKILL_FILES:
        if ablate_library and skill_name == "pattern_library.md":
            continue
        skill_path = os.path.join(SKILLS_DIR, skill_name)
        try:
            with open(skill_path, "r") as f:
                skill_text = f.read().strip()
            parts.append(f"\n\n---\n\n# Skill: {skill_name}\n\n{skill_text}")
        except FileNotFoundError:
            logger.warning("Lean skill file missing: %s", skill_path)

    return "\n\n".join(parts)


def _build_task_string_from_input(input_prompt: dict) -> str:
    """Reconstruct the validator task string from saved input_prompt JSON.

    Matches the live format from trading_cycle.py — asks for full snipe output
    including re_entry_conditions, watch_trigger, snipe_entry_zone, etc.
    """
    pair = input_prompt.get("pair", "UNKNOWN")
    pair_display = pair.replace("_", "/")
    narrative = input_prompt.get("narrative", "No narrative available.")
    fan_state = input_prompt.get("fan_state", "unknown")
    bb_expanding = input_prompt.get("bb_expanding", False)
    indicators = input_prompt.get("indicators", {})

    # Build indicator summary
    ind_lines = []
    for key, val in indicators.items():
        if val is not None:
            ind_lines.append(f"  - {key}: {val}")
    ind_text = "\n".join(ind_lines) if ind_lines else "  (no indicator data)"

    task = (
        f"📊 **TRADE THESIS — {pair_display}**\n\n"
        f"The technical analysis team has identified this setup:\n\n"
        f"**THESIS:** {narrative}\n\n"
        f"**Fan state:** {fan_state}\n"
        f"**BB expanding:** {bb_expanding}\n\n"
        f"**Your job:**\n"
        f"1. Look at the chart. Does the structure SUPPORT this thesis?\n"
        f"2. Run the 10-point checklist — which items CONFIRM the thesis?\n"
        f"3. If the thesis is right, give a SNIPE with entry conditions.\n"
        f"4. If the thesis is wrong, explain what the chart ACTUALLY shows.\n"
        f"5. Use fishing line theory — what phase is this, and what comes next?\n\n"
        f"## Indicator Data\n{ind_text}\n\n"
        f"---\n"
        f"After analyzing the chart, respond with ONLY a ```json code block. "
        f"No prose outside the JSON.\n\n"
        f"Return structured JSON with: verdict (TRADE_NOW/WATCH/SKIP), direction, "
        f"confidence (1-10), "
        f"reasoning (detailed — start with CHART READ:), "
        f"re_entry_conditions (list of {{field, op, value, reason}} dicts), "
        f"re_entry_direction, watch_trigger (SPECIFIC prices: "
        f"entry zone like 212.45-212.60, invalidation like below 212.20, target), "
        f"watch_for (plain english trigger summary with prices), "
        f"snipe_entry_zone, snipe_invalidation, snipe_target, "
        f"estimated_candles_to_entry (integer), price_target_entry (price level)."
    )
    return task


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------

def _extract_verdict(raw: str) -> dict:
    """Parse verdict JSON from model response (handles prose + JSON, code blocks, etc.)."""
    if not raw or not raw.strip():
        return {"verdict": "PARSE_ERROR", "direction": "", "confidence": 0,
                "reasoning": "Empty response"}

    # Try ```json code block first
    code_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
    if code_match:
        try:
            parsed = json.loads(code_match.group(1))
            if "verdict" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass

    # Walk the string looking for JSON objects containing "verdict"
    for i in range(len(raw)):
        if raw[i] != '{':
            continue
        depth = 0
        for j in range(i, len(raw)):
            if raw[j] == '{':
                depth += 1
            elif raw[j] == '}':
                depth -= 1
            if depth == 0:
                candidate = raw[i:j + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict) and "verdict" in parsed:
                        return parsed
                except json.JSONDecodeError:
                    break
                break

    return {"verdict": "PARSE_ERROR", "direction": "", "confidence": 0,
            "reasoning": f"Could not parse verdict from: {raw[:200]}"}


def _build_parsed(raw_response: str) -> dict:
    """Alias for _extract_verdict for backward compatibility."""
    return _extract_verdict(raw_response)


# ---------------------------------------------------------------------------
# Data queries
# ---------------------------------------------------------------------------

def check_open_trades() -> List[Dict]:
    """Check for open trades that might conflict with replay."""
    conn = sqlite3.connect(TRADING_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, pair, direction, entry_time FROM live_trades "
        "WHERE exit_time IS NULL AND result IS NULL "
        "AND entry_time >= datetime('now', '-7 days')"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_todays_validator_calls(date_str: str = None) -> List[Dict]:
    """Get Anthropic validator calls from vision_training_data for a given date."""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(TRADING_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, agent, chart_path, input_prompt, output_response,
               verdict, model_used, timestamp
        FROM vision_training_data
        WHERE date(timestamp) = ?
          AND model_used = 'claude-sonnet-4-6'
        ORDER BY timestamp
    """, (date_str,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_validator_calls_by_ids(ids: List[int]) -> List[Dict]:
    """Get specific validator calls by vision_training_data IDs."""
    if not ids:
        return []
    conn = sqlite3.connect(TRADING_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(f"""
        SELECT id, agent, chart_path, input_prompt, output_response,
               verdict, model_used, timestamp
        FROM vision_training_data
        WHERE id IN ({placeholders})
        ORDER BY timestamp
    """, ids).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_validator_calls_last_n_days(days: int = 14) -> List[Dict]:
    """Get all Anthropic validator calls from the last N days."""
    conn = sqlite3.connect(TRADING_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, agent, chart_path, input_prompt, output_response,
               verdict, model_used, timestamp
        FROM vision_training_data
        WHERE date(timestamp) >= date('now', ?)
          AND model_used = 'claude-sonnet-4-6'
        ORDER BY timestamp
    """, (f"-{days} days",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Model management
# ---------------------------------------------------------------------------

def stop_9b() -> bool:
    try:
        subprocess.run(["bash", MLX_SCRIPT, "stop", "CRO"],
                       capture_output=True, timeout=15)
        time.sleep(2)
        return True
    except Exception as e:
        logger.error("Failed to stop 9B: %s", e)
        return False


def start_35b() -> bool:
    """Pre-load 35B model into memory via direct MLX calls (no server needed)."""
    try:
        _load_model()
        return True
    except Exception as e:
        logger.error("Failed to load 35B: %s", e)
        return False


def stop_35b() -> bool:
    """Unload 35B model from memory."""
    import gc
    _model_cache["model"] = None
    _model_cache["processor"] = None
    _model_cache["config"] = None
    gc.collect()
    try:
        import mlx.core as mx
        mx.metal.clear_cache()
    except Exception:
        pass
    time.sleep(2)
    return True


def start_9b() -> bool:
    try:
        subprocess.run(["bash", MLX_SCRIPT, "start", "CRO"],
                       capture_output=True, timeout=30)
        time.sleep(5)
        return True
    except Exception:
        return False


def is_35b_running() -> bool:
    """Check if the 35B is already loaded in memory."""
    return _model_cache["model"] is not None


# ---------------------------------------------------------------------------
# Call 35B
# ---------------------------------------------------------------------------

def _load_model():
    """Load 35B model + processor + config once. Returns (model, processor, config)."""
    if _model_cache["model"] is not None:
        return _model_cache["model"], _model_cache["processor"], _model_cache["config"]
    logger.info("Loading 35B model + adapter (this takes 30-60s)...")
    from mlx_vlm import load
    model, processor = load(LOCAL_MODEL_NAME, adapter_path=ADAPTER_PATH)
    config = model.config
    _model_cache["model"] = model
    _model_cache["processor"] = processor
    _model_cache["config"] = config
    logger.info("35B model loaded.")
    return model, processor, config


def call_35b(system_prompt: str, content_blocks: list) -> str:
    """Call the 35B VLM via the live HTTP server at LOCAL_MODEL_ENDPOINT.

    Uses the same MLX server the live validator uses (shared warm model,
    no second in-process load). Matches the trading_cycle.py:6790-6817
    live payload shape exactly: system/user messages, chat_template_kwargs
    to suppress Qwen3.5 thinking, strip any leaked <think> blocks post-hoc.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content_blocks},
    ]
    payload = json.dumps({
        "model": LOCAL_MODEL_NAME,
        "messages": messages,
        "temperature": 0.7,
        "top_p": 0.8,
        "max_tokens": 4096,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        LOCAL_MODEL_ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Jarvis-Tenant": "background",
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    output = data["choices"][0]["message"].get("content", "") or ""
    return re.sub(r"<think>.*?</think>", "", output, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# DB logging
# ---------------------------------------------------------------------------

def _ensure_ghost_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ghost_verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            pair TEXT NOT NULL,
            anthropic_verdict TEXT,
            anthropic_direction TEXT,
            anthropic_confidence REAL,
            anthropic_reasoning TEXT,
            local_verdict TEXT,
            local_direction TEXT,
            local_confidence REAL,
            local_reasoning TEXT,
            local_raw_response TEXT,
            verdict_match BOOLEAN,
            direction_match BOOLEAN,
            confidence_delta REAL,
            local_model TEXT,
            vision_training_id INTEGER,
            chart_path TEXT,
            local_snipe_entry TEXT,
            local_snipe_invalidation TEXT,
            local_snipe_target TEXT,
            local_conditions_json TEXT,
            local_conditions_count INTEGER
        )
    """)
    # Add columns that may be missing from older schema
    for col, col_type in [
        ("vision_training_id", "INTEGER"),
        ("chart_path", "TEXT"),
        ("local_snipe_entry", "TEXT"),
        ("local_snipe_invalidation", "TEXT"),
        ("local_snipe_target", "TEXT"),
        ("local_conditions_json", "TEXT"),
        ("local_conditions_count", "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE ghost_verdicts ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists


def log_ghost_verdict(
    pair: str,
    anthropic: Dict,
    local: Dict,
    vtd_id: int,
    chart_path: str,
    local_raw: str,
) -> None:
    """Log comparison to ghost_verdicts table, including snipe conditions."""
    anth_v = str(anthropic.get("verdict", "")).upper()
    local_v = str(local.get("verdict", "")).upper()
    verdict_match = anth_v == local_v
    anth_dir = str(anthropic.get("direction", "")).upper()
    local_dir = str(local.get("direction", "")).upper()
    direction_match = anth_dir == local_dir
    conf_delta = abs(
        float(local.get("confidence", 0) or 0)
        - float(anthropic.get("confidence", 0) or 0)
    )

    # Extract snipe conditions from 35B response
    snipe_entry = str(local.get("snipe_entry_zone", "") or "")[:500]
    snipe_inv = str(local.get("snipe_invalidation", "") or "")[:500]
    snipe_target = str(local.get("snipe_target", "") or "")[:500]
    conditions = local.get("re_entry_conditions", []) or []
    conditions_json = json.dumps(conditions)[:3000] if conditions else "[]"
    conditions_count = len(conditions) if isinstance(conditions, list) else 0

    conn = sqlite3.connect(TRADING_DB, timeout=10)
    _ensure_ghost_table(conn)
    conn.execute("""
        INSERT INTO ghost_verdicts (
            timestamp, pair,
            anthropic_verdict, anthropic_direction, anthropic_confidence,
            anthropic_reasoning,
            local_verdict, local_direction, local_confidence,
            local_reasoning, local_raw_response,
            verdict_match, direction_match, confidence_delta,
            local_model, vision_training_id, chart_path,
            local_snipe_entry, local_snipe_invalidation, local_snipe_target,
            local_conditions_json, local_conditions_count
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(), pair,
        anth_v, anth_dir,
        float(anthropic.get("confidence", 0) or 0),
        str(anthropic.get("reasoning", ""))[:2000],
        local_v, local_dir,
        float(local.get("confidence", 0) or 0),
        str(local.get("reasoning", ""))[:2000],
        local_raw[:5000],
        verdict_match, direction_match, conf_delta,
        f"mlx/CSO ({LOCAL_MODEL_NAME})",
        vtd_id, chart_path,
        snipe_entry, snipe_inv, snipe_target,
        conditions_json, conditions_count,
    ))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Outcome comparison — cross-ref ghost verdicts with actual trade results
# ---------------------------------------------------------------------------

def get_trade_outcomes(date_str: str = None, days: int = 1) -> Dict[str, Dict]:
    """Get actual trade outcomes from live_trades for comparison.

    Returns dict keyed by pair_direction (e.g. 'EUR_CHF_SELL') with:
    - result: 'win'/'loss'/'breakeven'
    - pnl_pips: float
    - exit_trigger: str
    """
    conn = sqlite3.connect(TRADING_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    if date_str:
        where = f"date(entry_time) = '{date_str}'"
    else:
        where = f"entry_time >= datetime('now', '-{days} days')"
    rows = conn.execute(f"""
        SELECT pair, direction, result, pnl_pips, exit_trigger, entry_time, exit_time
        FROM live_trades
        WHERE {where} AND exit_time IS NOT NULL
        ORDER BY entry_time
    """).fetchall()
    conn.close()

    outcomes = {}
    for r in rows:
        key = f"{r['pair']}_{(r['direction'] or '').upper()}"
        # Keep latest outcome per pair+direction
        outcomes[key] = {
            "result": r["result"],
            "pnl_pips": r["pnl_pips"] or 0,
            "exit_trigger": r["exit_trigger"] or "",
            "entry_time": r["entry_time"],
        }
    return outcomes


def print_outcome_analysis(results: List[Dict], outcomes: Dict[str, Dict]) -> None:
    """Print ghost-vs-outcome analysis: would the 35B have avoided losers / caught winners?"""
    if not outcomes:
        print("\n  No closed trades found for outcome comparison.")
        return

    print(f"\n{'=' * 70}")
    print(f"  GHOST vs ACTUAL OUTCOMES")
    print(f"{'=' * 70}")

    ghost_would_trade = []  # 35B said TRADE_NOW
    ghost_would_skip = []   # 35B said SKIP/WATCH
    opus_traded = []        # Opus said TRADE_NOW

    for r in results:
        if r.get("error"):
            continue
        pair = r.get("pair", "?")
        local_v = r.get("local_verdict", "").upper()
        local_d = r.get("local_direction", "").upper()
        anth_v = r.get("anthropic_verdict", "").upper()

        # Find matching outcome
        key = f"{pair}_{local_d}" if local_d else f"{pair}_SELL"
        outcome = outcomes.get(key)
        if not outcome:
            # Try opposite direction or any match for this pair
            for k, v in outcomes.items():
                if k.startswith(pair):
                    outcome = v
                    break

        if not outcome:
            continue

        entry = {
            "pair": pair,
            "ghost_verdict": local_v,
            "opus_verdict": anth_v,
            "result": outcome["result"],
            "pnl_pips": outcome["pnl_pips"],
            "exit_trigger": outcome["exit_trigger"],
        }

        if local_v == "TRADE_NOW":
            ghost_would_trade.append(entry)
        else:
            ghost_would_skip.append(entry)

        if anth_v == "TRADE_NOW":
            opus_traded.append(entry)

    # Analysis: would ghost have traded these?
    if ghost_would_trade:
        wins = [e for e in ghost_would_trade if e["result"] == "win"]
        losses = [e for e in ghost_would_trade if e["result"] == "loss"]
        win_pips = sum(e["pnl_pips"] for e in wins)
        loss_pips = sum(e["pnl_pips"] for e in losses)
        print(f"\n  35B would TRADE ({len(ghost_would_trade)} entries):")
        print(f"    Wins:   {len(wins)}  (+{win_pips:.1f}p)")
        print(f"    Losses: {len(losses)} ({loss_pips:.1f}p)")
        for e in ghost_would_trade:
            icon = "✅" if e["result"] == "win" else "❌"
            print(f"    {icon} {e['pair']:10s} {e['result']:6s} {e['pnl_pips']:+.1f}p  (Opus={e['opus_verdict']})")

    if ghost_would_skip:
        # These are trades the 35B would NOT have taken
        avoided_losses = [e for e in ghost_would_skip if e["result"] == "loss" and e["opus_verdict"] == "TRADE_NOW"]
        missed_wins = [e for e in ghost_would_skip if e["result"] == "win" and e["opus_verdict"] == "TRADE_NOW"]
        if avoided_losses:
            saved = sum(abs(e["pnl_pips"]) for e in avoided_losses)
            print(f"\n  35B would have AVOIDED these losers (saved {saved:.1f}p):")
            for e in avoided_losses:
                print(f"    🛡️ {e['pair']:10s} {e['pnl_pips']:+.1f}p  (Opus said TRADE_NOW)")
        if missed_wins:
            missed = sum(e["pnl_pips"] for e in missed_wins)
            print(f"\n  35B would have MISSED these winners (lost {missed:.1f}p):")
            for e in missed_wins:
                print(f"    ⚠️ {e['pair']:10s} {e['pnl_pips']:+.1f}p  (Opus said TRADE_NOW)")

    # Net impact
    if ghost_would_trade or ghost_would_skip:
        ghost_pnl = sum(e["pnl_pips"] for e in ghost_would_trade)
        opus_pnl = sum(e["pnl_pips"] for e in opus_traded)
        print(f"\n  NET COMPARISON:")
        print(f"    Opus P&L:  {opus_pnl:+.1f}p ({len(opus_traded)} trades)")
        print(f"    35B P&L:   {ghost_pnl:+.1f}p ({len(ghost_would_trade)} trades)")
        diff = ghost_pnl - opus_pnl
        better = "35B" if diff > 0 else "Opus"
        print(f"    Delta:     {diff:+.1f}p ({better} better)")

    print(f"\n{'=' * 70}")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: List[Dict]) -> None:
    total = len(results)
    errors = sum(1 for r in results if r.get("error"))
    valid = total - errors
    matches = sum(1 for r in results if r.get("verdict_match"))
    dir_matches = sum(1 for r in results if r.get("direction_match"))

    # Group by verdict type
    by_verdict = {}
    for r in results:
        v = r.get("anthropic_verdict", "?")
        by_verdict.setdefault(v, []).append(r)

    print(f"\n{'=' * 70}")
    print(f"  GHOST VALIDATOR COMPARISON REPORT")
    print(f"{'=' * 70}")
    print(f"  Total entries:   {total}")
    print(f"  Errors:          {errors}")
    if valid > 0:
        print(f"  Verdict match:   {matches}/{valid} ({matches / valid * 100:.1f}%)")
        print(f"  Direction match: {dir_matches}/{valid} ({dir_matches / valid * 100:.1f}%)")
    print(f"  Target:          95%+ verdict match")

    # Per-verdict breakdown
    print(f"\n  By verdict type:")
    for vtype in ["TRADE_NOW", "WATCH", "SKIP"]:
        group = by_verdict.get(vtype, [])
        if not group:
            continue
        g_valid = [r for r in group if not r.get("error")]
        g_match = sum(1 for r in g_valid if r.get("verdict_match"))
        pct = f"{g_match / len(g_valid) * 100:.0f}%" if g_valid else "N/A"
        print(f"    {vtype:12s}  {g_match}/{len(g_valid)} ({pct})")
    print()

    # Per-entry detail
    for i, r in enumerate(results, 1):
        if r.get("error"):
            status = "ERROR"
        elif r.get("verdict_match"):
            status = "✓"
        else:
            status = "✗"
        pair = r.get("pair", "?")
        anth = r.get("anthropic_verdict", "?")
        local = r.get("local_verdict", "?")
        anth_dir = r.get("anthropic_direction", "")
        local_dir = r.get("local_direction", "")
        anth_conf = r.get("anthropic_confidence", 0)
        local_conf = r.get("local_confidence", 0)
        elapsed = r.get("elapsed_s", 0)
        print(f"  [{i:2d}/{total}] {pair:10s}  "
              f"Opus={anth:10s} {anth_dir:4s} c={anth_conf:<4}  "
              f"35B={local:10s} {local_dir:4s} c={local_conf:<4}  "
              f"{status}  {elapsed:.0f}s")

        # Show snipe details for TRADE_NOW/WATCH
        local_full = r.get("local_full_verdict", {})
        if local_full and local in ("TRADE_NOW", "WATCH"):
            entry = local_full.get("snipe_entry_zone", "")
            inv = local_full.get("snipe_invalidation", "")
            target = local_full.get("snipe_target", "")
            conditions = local_full.get("re_entry_conditions", [])
            if entry or conditions:
                print(f"           Snipe: entry={entry}")
                if inv:
                    print(f"                  invalidation={inv}")
                if target:
                    print(f"                  target={target}")
                if conditions:
                    print(f"                  conditions ({len(conditions)}):")
                    for c in conditions[:5]:
                        if isinstance(c, dict):
                            print(f"                    {c.get('field','?')} "
                                  f"{c.get('op','?')} {c.get('value','?')}")

    print(f"\n{'=' * 70}")


# ---------------------------------------------------------------------------
# Main replay
# ---------------------------------------------------------------------------

def run_ghost_replay(
    date_str: str = None,
    force: bool = False,
    skip_model_swap: bool = False,
    ids: List[int] = None,
    days: int = None,
    no_teaching: bool = False,
    prompt_mode: str = "lean",
    ablate_library: bool = False,
) -> List[Dict]:
    """
    Main entry point. Reconstructs validator inputs from vision_training_data,
    calls 35B with same teaching images + chart + prompt, compares to Anthropic.

    Args:
        date_str: Date to replay (YYYY-MM-DD, default: today)
        force: Run even with open trades
        skip_model_swap: If True, assume 35B is already loaded
        ids: Specific vision_training_data IDs to replay
        days: Replay all entries from last N days
        no_teaching: Skip teaching images (35B already has this knowledge from distillation)
        prompt_mode: "lean" (default, live 35B stack) or "heavy" (validator_v4.md)
        ablate_library: When True and prompt_mode="lean", strip pattern_library.md
            from the skill files (ablation control for library contribution).
    """
    # 1. Pre-check open trades
    if not force:
        open_trades = check_open_trades()
        if open_trades:
            print(f"\n  {len(open_trades)} open trade(s) — use --force to override")
            for t in open_trades:
                print(f"    {t['pair']} {t['direction']}")
            return []

    # 2. Get entries based on mode
    if ids:
        entries = get_validator_calls_by_ids(ids)
        label = f"{len(ids)} specific entries"
    elif days:
        entries = get_validator_calls_last_n_days(days)
        label = f"last {days} days"
    else:
        entries = get_todays_validator_calls(date_str)
        label = date_str or datetime.now().strftime('%Y-%m-%d')

    if not entries:
        print(f"No validator calls found for {label}. Nothing to replay.")
        return []

    # Count by verdict
    verdict_counts = {}
    for e in entries:
        v = e.get("verdict", "?")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
    vc_str = ", ".join(f"{k}:{v}" for k, v in sorted(verdict_counts.items()))

    print(f"\n  Ghost Replay: {len(entries)} entries ({vc_str})")
    print(f"  Source: {label}")

    # 3. Load validator system prompt (once)
    system_prompt = _load_validator_system_prompt(
        prompt_mode=prompt_mode, ablate_library=ablate_library
    )
    print(f"  Prompt mode: {prompt_mode}"
          f"{' (library ablated)' if ablate_library and prompt_mode == 'lean' else ''}"
          f" — {len(system_prompt)} chars")

    # 4. Load teaching images (once) — skip if distilled model already knows them
    if no_teaching:
        teaching_images = []
        print("  Teaching images: SKIPPED (distilled model has this knowledge)")
    else:
        print("  Loading teaching images...", end=" ", flush=True)
        teaching_images = load_teaching_images()
        print(f"{len(teaching_images)} loaded")

    # 5. Model swap (unless already running)
    if not skip_model_swap:
        if is_35b_running():
            print("  35B already running — skipping model swap")
        else:
            print("  Stopping 9B...", end=" ", flush=True)
            stop_9b()
            print("done")

            print("  Starting 35B...", end=" ", flush=True)
            if not start_35b():
                print("FAILED — restarting 9B")
                start_9b()
                return []
            print("ready")

    # 6. Replay each entry
    results = []
    for i, entry in enumerate(entries, 1):
        vtd_id = entry["id"]
        chart_path = entry["chart_path"]

        # Parse input_prompt JSON
        try:
            input_prompt = json.loads(entry["input_prompt"]) if isinstance(
                entry["input_prompt"], str) else entry["input_prompt"]
        except (json.JSONDecodeError, TypeError):
            print(f"  [{i:2d}/{len(entries)}] SKIP — bad input_prompt JSON")
            results.append({"pair": "?", "error": "bad input_prompt JSON"})
            continue

        pair = input_prompt.get("pair", "UNKNOWN")
        print(f"  [{i:2d}/{len(entries)}] {pair}...", end=" ", flush=True)

        # Parse Anthropic's verdict from output_response
        try:
            anthropic_verdict = json.loads(entry["output_response"]) if isinstance(
                entry["output_response"], str) else entry["output_response"]
        except (json.JSONDecodeError, TypeError):
            anthropic_verdict = {"verdict": entry.get("verdict", "?"),
                                 "direction": "", "confidence": 0}

        # Load live chart
        chart_b64 = _load_image_as_b64(chart_path)
        if not chart_b64:
            print(f"SKIP — chart missing: {chart_path}")
            results.append({"pair": pair, "error": f"chart missing: {chart_path}"})
            continue

        # Build task string from saved input_prompt
        task_text = _build_task_string_from_input(input_prompt)

        # Build OpenAI content blocks (teaching images + chart + text)
        content_blocks = _build_openai_content_blocks(
            teaching_images, chart_b64, task_text
        )

        # Call 35B
        t0 = time.time()
        try:
            raw_response = call_35b(system_prompt, content_blocks)
        except Exception as e:
            print(f"35B ERROR: {e}")
            results.append({"pair": pair, "error": str(e)})
            continue
        elapsed = time.time() - t0

        # Parse local verdict (full JSON including snipe fields)
        local_verdict = _extract_verdict(raw_response)

        # Compare
        anth_v = str(anthropic_verdict.get("verdict", "")).upper()
        local_v = str(local_verdict.get("verdict", "")).upper()
        anth_dir = str(anthropic_verdict.get("direction", "")).upper()
        local_dir = str(local_verdict.get("direction", "")).upper()
        v_match = anth_v == local_v
        d_match = anth_dir == local_dir

        result = {
            "pair": pair,
            "anthropic_verdict": anth_v,
            "anthropic_direction": anth_dir,
            "local_verdict": local_v,
            "local_direction": local_dir,
            "verdict_match": v_match,
            "direction_match": d_match,
            "anthropic_confidence": anthropic_verdict.get("confidence", 0),
            "local_confidence": local_verdict.get("confidence", 0),
            "local_full_verdict": local_verdict,
            "elapsed_s": elapsed,
        }
        results.append(result)

        # Log to DB
        try:
            log_ghost_verdict(pair, anthropic_verdict, local_verdict,
                              vtd_id, chart_path, raw_response)
        except Exception as e:
            logger.warning("DB log failed: %s", e)

        status = "MATCH ✓" if v_match else "MISMATCH ✗"
        print(f"{status}  Anthropic={anth_v}  Local={local_v}")

    # 7. Restore models
    if not skip_model_swap:
        print(f"\n  Stopping 35B...", end=" ", flush=True)
        stop_35b()
        print("done")

        print("  Restarting 9B...", end=" ", flush=True)
        start_9b()
        print("ready")

    # 8. Report
    print_report(results)

    # 9. Outcome comparison — cross-ref with actual trade results
    _outcome_days = days or 1
    _outcome_date = date_str
    outcomes = get_trade_outcomes(date_str=_outcome_date, days=_outcome_days)
    if outcomes:
        print_outcome_analysis(results, outcomes)

    return results


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description="Ghost Validator Batch Replay")
    parser.add_argument("--date", default=None,
                        help="Date (YYYY-MM-DD, default: today)")
    parser.add_argument("--force", action="store_true",
                        help="Run even with open trades")
    parser.add_argument("--skip-model-swap", action="store_true",
                        help="Assume 35B is already loaded in memory")
    parser.add_argument("--ids", type=int, nargs="+",
                        help="Specific vision_training_data IDs to replay")
    parser.add_argument("--days", type=int, default=None,
                        help="Replay all entries from last N days")
    parser.add_argument("--no-teaching", action="store_true",
                        help="Skip teaching images (35B already has this from distillation)")
    parser.add_argument("--prompt-mode", choices=["lean", "heavy"], default="lean",
                        help="lean (default) = ghost_validator_v1.md + Skills/* — live 35B stack; "
                             "heavy = validator_v4.md (Opus prompt)")
    parser.add_argument("--ablate-library", action="store_true",
                        help="When --prompt-mode=lean, strip pattern_library.md from the skill files "
                             "(ablation control for library contribution)")
    args = parser.parse_args()

    run_ghost_replay(
        date_str=args.date,
        force=args.force,
        skip_model_swap=args.skip_model_swap,
        ids=args.ids,
        days=args.days,
        no_teaching=args.no_teaching,
        prompt_mode=args.prompt_mode,
        ablate_library=args.ablate_library,
    )
