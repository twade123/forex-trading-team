"""
floor_chat.py — Trading Floor Multi-Agent Chat Dispatch

Routes user messages through the existing swarm infrastructure using _agent_task
from trading_cycle.py. Agents use their real system prompts, real models, real MCP
tools — nothing duplicated here.

Returns List[{agent, text, timestamp}] for the dashboard to render as colored bubbles.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

import os as _pathmod
_JARVIS_DB_DIR = _pathmod.path.join(_pathmod.path.dirname(_pathmod.path.dirname(_pathmod.path.dirname(_pathmod.path.abspath(__file__)))), "Database", "v2")
_DB_TREVOR    = _pathmod.path.join(_JARVIS_DB_DIR, "trading_forex.db")
_DB_BOARDROOM = _pathmod.path.join(_JARVIS_DB_DIR, "workspaces.db")

from db_pool import get_trading_forex


# Per-pair cache of the last parsed validator JSON — used by set_snipe to get real conditions
_validator_json_cache: dict = {}


def _clean_validator_response(raw: str, pair: str = "") -> str:
    """Strip JSON blocks from validator output for clean floor display.
    Extracts reasoning text only — handles truncated JSON gracefully.
    Side-effect: caches parsed JSON in _validator_json_cache[pair] for set_snipe to use."""
    import re

    def _extract_fields(text: str) -> dict:
        """Extract key fields via regex even if JSON is truncated/malformed."""
        out = {}
        for field in ("verdict", "direction", "reasoning", "watch_for", "watch_trigger"):
            m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
            if m:
                out[field] = m.group(1).replace('\\n', '\n').replace('\\"', '"')
        return out

    # Try full JSON parse first
    try:
        m = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
        if not m:
            m = re.search(r'(\{[\s\S]*\})', raw)
        if m:
            data = json.loads(m.group(1))
            fields = data
            # Cache the full parsed JSON so set_snipe can use real conditions
            if pair and isinstance(data, dict):
                _validator_json_cache[pair] = data
                logger.info("[floor_chat] cached validator JSON for %s — keys: %s", pair, list(data.keys()))
        else:
            fields = _extract_fields(raw)
            # No JSON block found — still cache what we can so auto-snipe has verdict/direction
            if pair and fields:
                _validator_json_cache.setdefault(pair, {}).update(fields)
                logger.warning("[floor_chat] no JSON block in validator response for %s — partial cache: %s",
                               pair, list(fields.keys()))
    except Exception:
        # JSON truncated or malformed — extract fields with regex
        fields = _extract_fields(raw)
        # Still cache partial result so auto-snipe verdict check works
        if pair and fields:
            _validator_json_cache.setdefault(pair, {}).update(fields)
            logger.warning("[floor_chat] malformed validator JSON for %s — partial cache from regex: %s",
                           pair, list(fields.keys()))

    if fields:
        verdict = fields.get("verdict", "")
        direction = fields.get("direction", "")
        reasoning = (fields.get("reasoning") or "").strip()
        watch_for = (fields.get("watch_for") or fields.get("watch_trigger") or "").strip()
        header = f"**{verdict}**" + (f" {direction}" if direction else "")
        parts = [p for p in [header, reasoning, f"_Snipe trigger: {watch_for}_" if watch_for else ""] if p]
        if parts:
            return "\n\n".join(parts)

    # Last resort: strip any raw JSON blocks
    cleaned = re.sub(r'```json[\s\S]*?```', '', raw).strip()
    cleaned = re.sub(r'```[\s\S]*?```', '', cleaned).strip()
    cleaned = re.sub(r'\{[\s\S]{200,}\}', '', cleaned).strip()
    return cleaned or raw


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ── Context loaders ──────────────────────────────────────────────────────────

def _get_user_name(user_id: int) -> str:
    """Return the display name for a user (username column), or 'the user' as fallback."""
    try:
        conn = get_trading_forex()
        row = conn.execute(
            "SELECT username, name FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if row:
            # Prefer username (display name), fall back to name (hostname)
            name = (row[0] or row[1] or "").strip()
            # Skip hostname-style names (contain dots and no spaces)
            if name and "." not in name:
                return name
    except Exception:
        pass
    return ""   # empty = agent uses generic "the user"


def _load_annotations(pair: str, user_id: int) -> List[Dict]:
    # Annotations are stored in trevor_database.db (written by api_annotations endpoint)
    try:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT annotation_type, price, direction, note, ema_cross "
            "FROM user_chart_annotations "
            "WHERE pair=? AND user_id=? AND active=1 ORDER BY created_at DESC LIMIT 10",
            (pair, user_id),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("Annotations load failed: %s", e)
        return []


def _load_last_cycle(pair: str) -> Dict:
    try:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT pair, validator_verdict, validator_confidence, validator_reasoning, "
            "full_confluence_score, ta_narrative, created_at "
            "FROM trade_decisions WHERE pair=? ORDER BY created_at DESC LIMIT 1",
            (pair,),
        ).fetchone()
        return dict(row) if row else {}
    except Exception as e:
        logger.debug("Last cycle load failed for %s: %s", pair, e)
        return {}


def _load_recent_watch(pair: str, max_age_minutes: int = 60) -> Optional[Dict]:
    """Return the most recent active watch_suggestions entry for a pair (within max_age_minutes)."""
    try:
        conn = get_trading_forex()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, instrument, conditions, raw_suggestion, validator_verdict, "
            "validator_confidence, status, created_at "
            "FROM watch_suggestions "
            "WHERE instrument=? AND status IN ('watching','active') "
            "AND datetime(created_at) >= datetime('now', ? || ' minutes') "
            "ORDER BY created_at DESC LIMIT 1",
            (pair, f"-{max_age_minutes}"),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.debug("Recent watch load failed for %s: %s", pair, e)
        return None


def _format_annotations(annotations: List[Dict]) -> str:
    if not annotations:
        return ""
    lines = ["User's chart annotations:"]
    for a in annotations:
        parts = [a.get("annotation_type", "note")]
        if a.get("price"):   parts.append(f"at {a['price']}")
        if a.get("direction"): parts.append(f"({a['direction']})")
        if a.get("note"):    parts.append(f"— {a['note']}")
        if a.get("ema_cross"): parts.append(f"[{a['ema_cross']}]")
        lines.append("  " + " ".join(parts))
    return "\n".join(lines)


# ── Orchestrator dispatch (9B local via _agent_task) ─────────────────────────

def _call_mlx(system: str, user: str, max_tokens: int = 300) -> str:
    """Direct sync call to local MLX 35B (agent fleet, port 11502). Used for routing decisions only."""
    import re
    import urllib.request as _ureq
    payload = json.dumps({
        "model": "mlx-community/Qwen3.5-35B-A3B-4bit",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stop": ["</think>"],
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = _ureq.Request(
        "http://127.0.0.1:11503/v1/chat/completions",  # serving gateway → MLX 35B
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Jarvis-Tenant": "trading",
        },
    )
    with _ureq.urlopen(req, timeout=30) as r:
        result = json.loads(r.read())
    text = (result["choices"][0]["message"].get("content") or "").strip()
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _orchestrator_decide(message: str, pair: Optional[str],
                          last_cycle: Dict, annotations: List[Dict],
                          chat_history: List[Dict]) -> Dict:
    """
    Dispatch routing decision via direct MLX call.
    Routing is lightweight — doesn't need the full swarm agent stack.
    Returns {action, my_message, needs_pair}.
    """
    ctx_parts = []
    if pair:
        ctx_parts.append(f"Pair in focus: {pair}")
    if last_cycle:
        ctx_parts.append(
            f"Last cycle on {last_cycle.get('pair', pair)}: "
            f"verdict={last_cycle.get('validator_verdict','?')} "
            f"({last_cycle.get('validator_confidence', 0):.0%} conf) — "
            f"{str(last_cycle.get('validator_reasoning', ''))[:200]}"
        )
    ann_text = _format_annotations(annotations)
    if ann_text:
        ctx_parts.append(ann_text)
    if chat_history:
        ctx_parts.append("Recent chat:\n" + "\n".join(
            f"  {m['agent']}: {m['text'][:100]}" for m in chat_history[-4:]
        ))

    context_block = "\n\n".join(ctx_parts) if ctx_parts else "No prior context."

    has_image = bool(getattr(_orchestrator_decide, "_has_image", False))
    system = (
        "You are the trading floor orchestrator. Decide what action the floor needs.\n\n"
        "Actions:\n"
        "  respond              — answer directly (general questions, no market data needed)\n"
        "  trade_status         — trader asks about open positions, guardian status, P&L, how a trade is doing\n"
        "  get_ta               — TA pulls fresh live data and reports (trader wants current chart state)\n"
        "  ask_validator        — validator reasons about trader's question using last cycle context\n"
        "  get_ta_then_validator — TA gets fresh data, then validator re-evaluates (trader says things changed)\n"
        "  run_cycle            — queue full pipeline from scratch\n"
        "  set_snipe            — trader explicitly requests a snipe/watch be created (e.g. 'set a snipe', 'watch for', 'snipe this')\n\n"
        "IMPORTANT: If the trader asks 'how is my trade', 'what is the guardian doing', 'position status', "
        "'how are we doing', 'what is open', 'any open trades' — use trade_status.\n"
        "If the trader asks 'what do you think?' or 'should I trade?' with no extra context, "
        "use get_ta_then_validator so the team gets fresh data first.\n"
        "If the trader mentions they see something specific on their chart, use ask_validator.\n"
        "If the trader says anything like 'set a snipe', 'create a watch', 'snipe this', 'watch for', 'monitor this' — use set_snipe.\n"
        "NEVER use 'respond' for snipe/watch requests — the system must actually create it.\n"
        "Your my_message should be 1 sentence max — the agents will handle the detailed analysis.\n\n"
        "Reply with ONLY valid JSON (no other text):\n"
        '{"action":"respond|trade_status|get_ta|ask_validator|get_ta_then_validator|run_cycle|set_snipe",'
        '"my_message":"your 1-sentence response to the trader",'
        '"needs_pair":"pair symbol or null"}'
    )
    user = f"CONTEXT:\n{context_block}\n\nTRADER: {message}"

    try:
        raw = _call_mlx(system, user, max_tokens=200)
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        logger.warning("Orchestrator dispatch failed: %s", e)

    return {"action": "respond", "my_message": "Let me check on that.", "needs_pair": pair}


# ── Main entry point ──────────────────────────────────────────────────────────

def _b64_to_image_dict(b64_data: str) -> Optional[Dict]:
    """Convert a base64 data URL or raw base64 PNG to handler_swarm-compatible image dict.

    handler_swarm.execute_agent_task expects: {"b64": "...", "media_type": "...", "description": "..."}
    The old format {"type": "base64", "data": "..."} was silently skipped — image never reached the LLM.
    """
    try:
        if b64_data.startswith("data:"):
            header, data = b64_data.split(",", 1)
            media_type = header.split(":")[1].split(";")[0]
        else:
            data = b64_data
            media_type = "image/png"  # default, will be corrected below

        # Auto-detect actual image type from magic bytes to prevent Claude API rejection
        # (browser canvas.toBlob often produces JPEG despite PNG declared in header)
        import base64 as _b64
        try:
            raw = _b64.b64decode(data[:16])
            if raw[:3] == b'\xff\xd8\xff':
                media_type = "image/jpeg"
            elif raw[:8] == b'\x89PNG\r\n\x1a\n':
                media_type = "image/png"
            elif raw[:4] == b'RIFF' or raw[8:12] == b'WEBP':
                media_type = "image/webp"
        except Exception:
            pass  # keep whatever media_type was set above

        return {"b64": data, "media_type": media_type, "description": "Trader's submitted chart"}
    except Exception:
        return None


def handle_floor_message(
    message: str,
    user_id: int,
    pair: Optional[str],
    chat_history: List[Dict],
    user_image: Optional[str] = None,
) -> List[Dict]:
    """
    Routes user message through the swarm. Returns [{agent, text, timestamp}].
    All agent calls go through _agent_task — real prompts, real models, real MCP tools.
    user_image: optional base64 PNG the user annotated and wants the validator to analyze.
    """
    out: List[Dict] = []

    # Team init is handled by the caller (api_trading_command) before this is called

    # ── HARD PRE-DISPATCH OVERRIDE FOR CHART SUBMISSIONS ─────────────────────
    # Chart submissions ALWAYS go to the validator. Period.
    # This is checked BEFORE the MLX dispatcher runs so it can never be overridden
    # by the routing model. The __SUBMIT_CHART__ marker is set by the frontend
    # when Tim submits an annotated chart — validator reads it and sets conditions.
    # A full trading cycle must NEVER fire on a chart submission.
    _is_chart_submission_msg = bool(message and message.startswith("__SUBMIT_CHART__"))

    # Pre-process user image for validator calls
    _user_image_dict = _b64_to_image_dict(user_image) if user_image else None
    _user_image_prefix = (
        "\n\n📸 **User has attached an annotated chart screenshot.** "
        "The image is attached to this message. Look at it carefully — "
        "the user has drawn on it to show you what they see. "
        "Analyze their annotations alongside your own assessment.\n\n"
        if _user_image_dict else ""
    )

    def _call_agent(agent_name: str, task: str, context: dict,
                    max_tokens: int = 600, timeout: float = 60.0,
                    images: Optional[List] = None) -> str:
        """
        Call a swarm agent via _agent_task with full tool access.
        Agents fetch their own data through MCP tools — same as the pipeline.
        No pre-loading here.
        """
        try:
            from Source.agents.trading_cycle import _agent_task, _get_swarm
            swarm = _get_swarm()
            if swarm and agent_name in (swarm.agents or {}):
                result = _agent_task(
                    agent_name, task,
                    context=context,
                    max_tokens=max_tokens,
                    max_tool_rounds=5,
                    timeout=timeout,
                    images=images,
                )
                return result.get("response", "").strip() or "No response."
            else:
                raise RuntimeError(f"Agent {agent_name} not registered — is the trading team running?")
        except Exception as e:
            logger.warning("[floor_chat] %s unavailable: %s", agent_name, e)
            return f"Team unavailable right now — start the trading team first, or try running a cycle."

    def _add(agent: str, text: str):
        out.append({"agent": agent, "text": text, "timestamp": _now_iso()})

    def _fetch_oanda_for_chat(pair: str, timeout: float = 30.0) -> str:
        """2026-04-27 (#54): Chat-mode TA was instructed 'Pull fresh H1 market data'
        but TA's MCP tools are computation, not data-fetching — OANDA agent owns the
        fetch tools. The previous flow worked on 9B because 9B hallucinated data
        when asked to fetch; 35B (post-flip) honestly says 'I cannot access OANDA'.

        Mirror the trading-cycle's data_oanda → ta_compute pattern by calling OANDA
        first and feeding its output into TA's task.
        """
        oanda_task = (
            f"Fetch the most recent 299 M15 candles for {pair} (this team trades M15 only — "
            f"299 candles matches the dashboard chart the validator sees). "
            f"Also include current bid/ask + spread. "
            f"Return a structured summary (full OHLC series + current price + spread). "
            f"No commentary or analysis — data only."
        )
        return _call_agent(
            "oanda_data", oanda_task,
            {"instrument": pair, "floor_chat": True},
            max_tokens=800, timeout=timeout,
        )

    # Load only what agents can't get themselves: annotations (user notes on chart)
    # Open trades, candles, account data — agents fetch via their MCP tools
    annotations = _load_annotations(pair, user_id) if pair else []
    last_cycle  = _load_last_cycle(pair) if pair else {}   # for validator reasoning context only
    ann_text    = _format_annotations(annotations)

    # Resolve user's display name — injected into agent tasks so they address them by name
    user_display_name = _get_user_name(user_id)
    user_address      = user_display_name or "the user"
    # First name only for natural address in conversation (e.g. "Tim Wade" → "Tim")
    user_first_name   = user_display_name.split()[0] if user_display_name else ""
    user_greeting     = user_first_name or user_address
    # One-line context injected at top of every agent task
    _user_ctx_line    = f"[You are speaking with {user_greeting}]\n\n" if user_greeting != "the user" else ""

    # ── Short-circuit: Tim agreed with the validator ──────────────────────────
    # If the last chat message was from the validator and Tim's reply is agreement,
    # create the snipe immediately — no routing, no LLM call.
    _AGREEMENT_WORDS = {
        "yes", "yeah", "yep", "yup", "agreed", "agree", "ok", "okay", "sure",
        "do it", "set it", "go", "go ahead", "done", "sounds good", "perfect",
        "confirmed", "confirm", "set the snipe", "set snipe", "snipe it", "lock it in",
        "lets do it", "let's do it", "good call", "makes sense",
        "make this snipe", "make a snipe", "make the snipe", "create this snipe",
        "create the snipe", "create a snipe", "set this snipe", "snipe this",
        "we agree", "i agree", "lets snipe this", "let's snipe this",
    }
    _last_agent = (chat_history[-1].get("agent") if chat_history else None)
    _msg_clean  = message.strip().lower().rstrip("!.").strip()
    _is_agreement = _msg_clean in _AGREEMENT_WORDS or any(
        _msg_clean.startswith(w) for w in (
            "yes ", "yeah ", "agreed ", "ok ", "do it", "set it",
            "make this snipe", "make a snipe", "make the snipe",
            "create this snipe", "we agree", "i agree",
        )
    )

    if _last_agent == "validator" and _is_agreement and (pair or resolved_pair if 'resolved_pair' in dir() else pair):
        import re as _re, uuid as _uuid, sqlite3 as _sqlite3
        from datetime import timedelta as _td

        _snipe_pair = pair or ""
        _val_text   = chat_history[-1].get("text", "")

        # Check if validator already created a watch during the cycle
        _existing = _load_recent_watch(_snipe_pair, max_age_minutes=120) if _snipe_pair else None

        if _existing:
            _conds = []
            try: _conds = json.loads(_existing.get("conditions") or "[]")
            except Exception: pass
            _cond_str = ", ".join(c.get("desc") or c.get("description", "") for c in _conds) or "validator conditions"
            _add("validator",
                 f"Snipe is already set — watch #{_existing['id']} on {_snipe_pair.replace('_','/')} "
                 f"({_existing.get('validator_verdict','SNIPE')}). Monitoring: {_cond_str}.")
        else:
            # Try checklist cache first (set by _clean_validator_response)
            _cached = _validator_json_cache.get(_snipe_pair, {})
            _checklist_map = {
                "ema_cross":        {"field": "ema_fan_state",      "op": "in",  "value": ["just_crossed","expanding","bullish_expanding","bearish_expanding"], "description": "EMA cross must confirm"},
                "fan_opening":      {"field": "ema_fan_state",      "op": "in",  "value": ["expanding","accelerating","bullish_expanding","bearish_expanding"], "description": "Fan must be opening"},
                "fan_accelerating": {"field": "ema_velocity",       "op": ">=",  "value": 0.003, "description": "Fan velocity ≥0.003"},
                "bb_expanding":     {"field": "bb_expanding",       "op": "==",  "value": True,  "description": "BBs expanding"},
                "momentum_candles": {"field": "momentum_candles",   "op": "==",  "value": True,  "description": "Momentum candles required"},
                "rsi_recovering":   {"field": "rsi",                "op": ">=",  "value": 45,    "description": "RSI recovering ≥45"},
                "candles_away":     {"field": "ema_price_near_e100","op": "==",  "value": True,  "description": "Price near E100"},
                "correct_side":     {"field": "ema_fan_state",      "op": "in",  "value": ["bullish_expanding","bearish_expanding"], "description": "Price correct side of fan"},
                "no_wall":          {"field": "ema_trend_health",   "op": "in",  "value": ["strong","recovering"], "description": "No overhead wall"},
            }
            _conditions = []
            _direction = (_cached.get("direction") or "").upper() or None

            if _cached.get("checklist"):
                for _k, _v in _cached["checklist"].items():
                    if _v is False and _k in _checklist_map:
                        _conditions.append(_checklist_map[_k])

            if not _direction or not _conditions:
                # Fallback: keyword scoring from validator text
                _vl = _val_text.lower()
                _buy_score  = sum(1 for w in ["bullish", "buy", "long", "bounce"] if w in _vl)
                _sell_score = sum(1 for w in ["bearish", "sell", "short", "drop"] if w in _vl)
                _direction = _direction or ("BUY" if _buy_score > _sell_score else ("SELL" if _sell_score > _buy_score else None))

            if not _conditions:
                # Extract conditions from the snipe trigger line in validator text
                # e.g. "Snipe trigger: price retest of 0.7777... E21 crossing below E55... BBs widening"
                _trigger_text = ""
                for _marker in ["snipe trigger:", "_snipe trigger:", "watch for:", "entry when:"]:
                    _idx = _val_text.lower().find(_marker)
                    if _idx >= 0:
                        _trigger_text = _val_text[_idx:_idx+400].lower()
                        break
                if not _trigger_text:
                    _trigger_text = _val_text.lower()[-400:]  # last 400 chars often has the trigger

                # Map trigger text keywords to real indicator conditions
                if any(w in _trigger_text for w in ["bb", "band", "widen", "expand"]):
                    _conditions.append({"field": "bb_expanding", "op": "==", "value": True, "description": "BBs must be expanding"})
                if any(w in _trigger_text for w in ["ema cross", "e21", "e55", "fan", "separation", "diverge"]):
                    _fan_vals = ["bullish_expanding","bearish_expanding"] if not _direction else (
                        ["bullish_expanding","just_crossed","expanding"] if _direction == "BUY" else
                        ["bearish_expanding","just_crossed","expanding"])
                    _conditions.append({"field": "ema_fan_state", "op": "in", "value": _fan_vals, "description": "EMA fan must confirm direction"})
                if any(w in _trigger_text for w in ["momentum", "candle", "body", "engulf"]):
                    _conditions.append({"field": "momentum_candles", "op": "==", "value": True, "description": "Momentum candles required"})
                # Price level from trigger text
                import re as _re2
                _price_matches = _re2.findall(r'\b(\d+\.\d{3,5})\b', _trigger_text)
                for _pm in _price_matches:
                    _pf = float(_pm)
                    if 0.5 < _pf < 300:
                        _pfield = "ask" if _direction == "BUY" else "bid"
                        _pop = "<=" if _direction == "BUY" else ">="
                        _conditions.append({"field": _pfield, "op": _pop, "value": _pf, "description": f"Price must reach {_pf}"})
                        break
                logger.info("[floor_chat] agreement: extracted %d conditions from trigger text for %s", len(_conditions), _snipe_pair)

            if not _conditions:
                # No chart analysis available — refuse rather than create a garbage snipe
                _add("validator",
                     f"⚠️ Can't build a quality snipe for {_snipe_pair.replace('_','/')} — "
                     f"I don't have your chart analysis (server restart cleared it). "
                     f"Please resubmit your chart and I'll build the snipe from your actual analysis.")
                logger.warning("[floor_chat] agreement snipe refused — no conditions for %s", _snipe_pair)
                return out

            _now     = datetime.now(timezone.utc)
            _expires = _now + _td(hours=12)
            _cid     = f"user_watch_{_uuid.uuid4().hex[:8]}"

            try:
                _conn = get_trading_forex()
                try: _conn.execute("ALTER TABLE watch_suggestions ADD COLUMN user_thesis TEXT DEFAULT ''"); _conn.commit()
                except Exception: pass
                _cur = _conn.execute("""
                    INSERT INTO watch_suggestions
                    (cycle_id, instrument, suggestion_type, conditions, raw_suggestion,
                     validator_verdict, validator_confidence, created_at, expires_at,
                     status, workspace_task_id, context, user_thesis, user_id)
                    VALUES (?, ?, 'user_requested', ?, ?, 'SNIPE', 0.8, ?, ?, 'watching', NULL, ?, ?, ?)
                """, (_cid, _snipe_pair, json.dumps(_conditions), _val_text[:500],
                      _now.isoformat(), _expires.isoformat(),
                      json.dumps({"source": "user_agreement", "user_id": user_id,
                                  "direction": _direction, "entry_price": _price,
                                  "validator_text": _val_text[:300]}),
                      _val_text[:500], user_id))
                _wid = _cur.lastrowid
                _conn.commit()

                _dir_str   = f" {_direction}" if _direction else ""
                _price_str = f" @ {_price}" if _price else ""
                _cond_str  = ", ".join(c["description"] for c in _conditions)
                _add("validator",
                     f"Done — snipe set for {_snipe_pair.replace('_','/')}{_dir_str}{_price_str} "
                     f"(watch #{_wid}). Monitoring: {_cond_str}.")
                logger.info("[floor_chat] agreement→snipe watch #%d for %s dir=%s", _wid, _snipe_pair, _direction)
            except Exception as _we:
                logger.error("[floor_chat] agreement snipe failed: %s", _we)
                _add("validator", f"Couldn't save the snipe: {_we}")

        return out  # done — skip routing entirely
    # ─────────────────────────────────────────────────────────────────────────

    # ── Chart submissions bypass the dispatcher entirely ────────────────────
    # NEVER let the routing model decide what to do with a chart submission.
    # It goes straight to the validator. Always.
    if _is_chart_submission_msg:
        action        = "ask_validator"
        dispatch      = {"action": "ask_validator", "my_message": "", "needs_pair": pair or ""}
        resolved_pair = pair or ""
        orch_msg      = ""
        logger.info("[floor_chat] __SUBMIT_CHART__ detected → hardcoded action=ask_validator (bypassing MLX dispatcher)")
    else:
        # Orchestrator decides
        dispatch      = _orchestrator_decide(message, pair, last_cycle, annotations, chat_history)
        action        = dispatch.get("action", "respond")
        orch_msg      = (dispatch.get("my_message") or "").strip()
        resolved_pair = pair or dispatch.get("needs_pair") or ""

    logger.info("[floor_chat] user=%d pair=%s action=%s", user_id, resolved_pair, action)

    # If user sent a chart image with NO note → orchestrator asks for context first
    _is_submit_chart = message and message.startswith("__SUBMIT_CHART__")
    _is_developing_thesis = False  # will be set True if trader is showing a FUTURE/FORMING setup
    if _is_submit_chart:
        # Strip the marker — actual note is after it (may be empty)
        _chart_note = message[len("__SUBMIT_CHART__"):].strip()
        if not _chart_note:
            # No explicit note in message — check if user has DB annotations on this pair
            if ann_text:
                # Use their saved annotations as context
                message = f"(annotated chart — see trader notes below)\n{ann_text}"
            else:
                # Truly no context — ask
                _add("cycle_orchestrator",
                     f"Got your chart{' for ' + resolved_pair.replace('_','/') if resolved_pair else ''} 📸 "
                     f"What are you looking at? Are you considering a trade here, reviewing what happened, "
                     f"or want me to check if a setup is forming?")
                return out
        else:
            # Has note in message — use it, and append any DB annotations too
            message = _chart_note
            if ann_text:
                message += f"\n\nTrader's saved chart notes:\n{ann_text}"

        # Detect DEVELOPING/ANTICIPATORY thesis — trader is showing what WILL happen, not what IS
        # happening right now. Key signals: future tense, "will", "gonna", "forming", "when it",
        # "fan will", "cross will", "expecting", "projected", drawn lines showing future candle location.
        _thesis_lower = message.lower()
        _developing_signals = [
            "will form", "will cross", "will develop", "gonna", "going to", "when it reaches",
            "when price", "when candles", "when it gets", "fan will", "expect", "expecting",
            "anticipate", "should form", "should cross", "forming", "crossi", "crossed",
            "has crossed", "about to", "setting up", "setup forming", "developing",
            "in a few", "in the next", "few candles", "next few", "5 candles", "10 candles",
            "snipe would", "snipe when", "entry when", "enter when", "where the snipe",
        ]
        _is_developing_thesis = any(sig in _thesis_lower for sig in _developing_signals)

    # If user sent an annotated image, always go to Validator FIRST.
    # The Validator has vision — it reads Tim's chart and thesis directly.
    # TA is NOT sent the chart; its job is fetching live OANDA data.
    # Sending Tim's chart to the TA first causes the TA to report a current-state
    # snapshot that contradicts Tim's annotated (developing) thesis, producing
    # a wrong directional call. Validator reasons about intent first, requests
    # specific TA data only when it needs numbers it can't see in the chart.
    if _user_image_dict and action not in ("ask_validator", "get_ta_then_validator"):
        action = "ask_validator"   # always Validator first for chart submissions — overrides run_cycle too
        if not resolved_pair:
            resolved_pair = "the current chart"
        # Replace orch_msg with one that includes the trader's actual notes (not "running a cycle")
        _pair_disp = resolved_pair.replace("_", "/")
        _note_preview = message.strip()[:120] if (message and not message.startswith("__SUBMIT_CHART__")) else ""
        if _note_preview:
            orch_msg = f"📸 {_pair_disp} chart received — '{_note_preview}' — sending to validator."
        else:
            orch_msg = f"📸 {_pair_disp} chart received — sending to validator."

    # Guard: validator/TA actions require a pair — stop gracefully if missing
    if action in ("ask_validator", "get_ta", "get_ta_then_validator") and not resolved_pair:
        _add("cycle_orchestrator",
             orch_msg or "Which pair are you looking at? Let me know and I'll pull up the analysis.")
        return out

    # Guard: validator specifically needs a chart image.
    # If user didn't attach one, auto-load the most recent live chart for the pair.
    # Only fall back to SKIP if no chart exists anywhere for this pair.
    if action == "ask_validator" and not _user_image_dict:
        _auto_chart_loaded = False
        if resolved_pair:
            try:
                import glob as _glob, os as _os
                _charts_dir = _os.path.join(
                    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                    "Data", "charts", "live"
                )
                _pattern = _os.path.join(_charts_dir, f"{resolved_pair}_*.png")
                _matches = sorted(_glob.glob(_pattern), reverse=True)
                if _matches:
                    from Source.agents.trading_cycle import _load_v4_chart_image
                    _auto_chart = _load_v4_chart_image(_matches[0])
                    if _auto_chart:
                        _user_image_dict = _auto_chart
                        _user_image_prefix = (
                            f"\n\n📊 AUTO-LOADED LIVE CHART: {_os.path.basename(_matches[0])} "
                            f"(most recent for {resolved_pair}). Trader did not manually attach — using latest cycle chart.\n\n"
                        )
                        _auto_chart_loaded = True
                        logger.info("[floor_chat] auto-loaded live chart for validator: %s", _matches[0])
            except Exception as _ace:
                logger.warning("[floor_chat] could not auto-load chart: %s", _ace)

        if not _auto_chart_loaded:
            _user_image_prefix = (
                "\n\n⛔ NO LIVE CHART RECEIVED — no chart image found for this pair. "
                "Do NOT describe chart patterns, price levels, or anything visual. "
                "Return SKIP with a clear message: 'I need a chart to assess this — please submit one via the Submit Chart button.'\n\n"
            )

    if action == "trade_status":
        # Route to local 9B narrator — fast, no Claude API cost
        try:
            from guardian_narrator import narrate_floor_chat
            from Source.oanda_client import OandaClient as _OC_fc
            from Source.broker_credentials import BrokerCredentials as _BC_fc

            # Get open trades from OANDA
            _bc_fc = _BC_fc().get_connection(user_id=user_id, broker="oanda")
            with _OC_fc(_bc_fc["api_key"], _bc_fc.get("account_id", "")) as _oc_fc:
                _open = _oc_fc.get_open_trades()

            # Get guardian threats from team state
            from trading_api_routes import _get_user_team_state
            _state = _get_user_team_state(user_id) or {}
            _threats = _state.get("guardian_threats", {})

            _narrative = narrate_floor_chat(message, _threats, _open)
            _add("position_monitor", _narrative)
        except Exception as _ts_err:
            logger.warning("[floor_chat] trade_status via narrator failed: %s — falling back", _ts_err)
            _add("cycle_orchestrator", orch_msg or "Let me check your positions.")

    elif action == "respond":
        _add("cycle_orchestrator", orch_msg or "Got it.")

    elif action == "get_ta":
        if orch_msg:
            _add("cycle_orchestrator", orch_msg)
        if not resolved_pair:
            _add("cycle_orchestrator", "Which pair do you want me to pull data on?")
        else:
            # 2026-04-27 (#54): fetch OANDA first; TA can't fetch data itself.
            _oanda_data = _fetch_oanda_for_chat(resolved_pair)
            ta_task = (
                _user_ctx_line
                + f"## Market Data (fetched from OANDA)\n{_oanda_data}\n\n"
                + f"Read the data above and give a concise report of {resolved_pair} right now — "
                f"fan state, velocity, BB, momentum. Be specific with numbers. 3-5 sentences.\n"
                + (f"\nTrader context: {message}\n" if message else "")
                + (f"\n{ann_text}" if ann_text else "")
            )
            resp = _call_agent("technical_analyst", ta_task,
                               {"instrument": resolved_pair, "floor_chat": True},
                               max_tokens=600, timeout=30.0)
            _add("technical_analyst", resp)

    elif action == "ask_validator":
        if orch_msg:
            _add("cycle_orchestrator", orch_msg)
        if not resolved_pair:
            _add("cycle_orchestrator", "Which pair are you asking about?")
        else:
            # Determine if this is a developing/anticipatory thesis vs a live trade question
            _is_chart_submission = bool(_user_image_dict)
            _has_thesis = bool(message and message != "(see annotated chart)")

            if _is_chart_submission:
                # Both DEVELOPING and CURRENT-STATE chart submissions get the same rich analysis.
                # _is_developing_thesis only affects the framing intro line.
                _setup_framing = (
                    "They are showing a DEVELOPING thesis — a setup forming, not necessarily a trade signal RIGHT NOW.\n"
                    "Evaluate the TRAJECTORY, not just the snapshot. DO NOT say 'not happening right now'.\n"
                    if _is_developing_thesis else
                    "They are asking you to validate their current thesis against what you see in the chart.\n"
                )
                val_preamble = (
                    f"📸 **CHART SUBMISSION — {resolved_pair}**\n\n"
                    f"The trader has submitted an annotated chart. {_setup_framing}\n"
                    f"**Your job:**\n"
                    f"1. Look at the chart. What do you actually see? Are the EMAs, fan, and BBs behaving as the trader describes?\n"
                    f"2. Is the directional bias (BUY/SELL) structurally supported?\n"
                    f"3. Run the 10-point checklist — which items are CONFIRMED and which are still FORMING?\n"
                    f"4. Give a SNIPE with the specific conditions that would make this a live trade.\n"
                    f"5. If the thesis is wrong (wrong direction, structure broken), say so clearly.\n\n"
                    f"Return structured JSON with: verdict (SNIPE/SKIP/TRADE_NOW), direction, confidence (1-10), "
                    f"checklist (dict of all 10 items), reasoning (detailed), re_entry_conditions (list of "
                    f"{{field, op, value, reason}} dicts using valid indicator fields), "
                    f"re_entry_direction, re_entry_setup, watch_for (plain english trigger summary).\n\n"
                )
            else:
                val_preamble = f"A trader is asking about {resolved_pair}. Respond directly.\n\n"

            _thesis_line = (
                f"**TRADER'S THESIS / QUESTION:** {message}\n"
                if _has_thesis else ""
            )

            # ── Assemble data package for user chart submission ──
            # Fetch available context so the validator isn't flying blind
            _fc_intel_text = ""
            _fc_indicators = {}
            _fc_pair_history = ""

            # 1. Intelligence briefing from cache
            try:
                from intelligence_store import IntelligenceStore
                _fc_istore = IntelligenceStore()
                _fc_intel = _fc_istore.get_cached_intelligence(resolved_pair)
                if _fc_intel and isinstance(_fc_intel, dict):
                    _fc_briefing = _fc_intel.get("agent_briefing") or _fc_intel.get("briefing", "")
                    _fc_sentiment = _fc_intel.get("overall_sentiment", "N/A")
                    _fc_risk = _fc_intel.get("risk_events_upcoming", [])
                    _fc_intel_text = (
                        f"### Intelligence Briefing (cached)\n"
                        f"Sentiment: {_fc_sentiment} | Risk events: {json.dumps(_fc_risk[:3]) if _fc_risk else 'none'}\n"
                        f"{_fc_briefing[:500] if _fc_briefing else 'No briefing cached yet — intelligence agent may not have run for this pair.'}\n"
                    )
                    logger.info("[floor_chat] Intelligence loaded for %s (sentiment=%s)", resolved_pair, _fc_sentiment)
                else:
                    _fc_intel_text = "### Intelligence\nNo cached intelligence available for this pair. Intelligence agent may not have run yet.\n"
                    logger.info("[floor_chat] No cached intelligence for %s", resolved_pair)
            except Exception as _ie:
                _fc_intel_text = f"### Intelligence\nCould not fetch intelligence: {_ie}\n"
                logger.warning("[floor_chat] Intelligence fetch failed for %s: %s", resolved_pair, _ie)

            # 2. Current indicators from latest flight recorder TA_COMPUTE
            try:
                from ta_summary_fetcher import fetch_ta_summary
                _fc_ta = fetch_ta_summary(resolved_pair)
                if _fc_ta and isinstance(_fc_ta, dict):
                    _fc_indicators = _fc_ta.get("indicators", {})
                    _fc_ta_text = (
                        f"### Current Indicators\n"
                        f"- RSI: {_fc_indicators.get('rsi_14', 'N/A')} | ADX: {_fc_indicators.get('adx_14', 'N/A')}\n"
                        f"- EMA alignment: {_fc_indicators.get('ema_alignment', 'N/A')}\n"
                        f"- BB position: {_fc_indicators.get('bb_position', 'N/A')}\n"
                        f"- Patterns: {', '.join(_fc_ta.get('patterns', {}).get('active_patterns', [])) or 'none'}\n"
                    )
                    logger.info("[floor_chat] Indicators loaded for %s", resolved_pair)
                else:
                    _fc_ta_text = "### Current Indicators\nNo recent TA data available.\n"
            except Exception as _te:
                _fc_ta_text = "### Current Indicators\nTA data unavailable.\n"
                logger.debug("[floor_chat] TA fetch failed: %s", _te)

            # 3. Pair trade history from flight recorder
            _fc_fr = None
            try:
                import sqlite3 as _fc_sq
                _fc_fr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flight_recorder.db")
                if os.path.exists(_fc_fr_path):
                    _fc_fr = _fc_sq.connect(_fc_fr_path, timeout=3)
                    _fc_fr.row_factory = _fc_sq.Row
                    _fc_closes = _fc_fr.execute(
                        "SELECT data FROM flight_log WHERE pair=? AND stage='TRADE_CLOSE' ORDER BY timestamp DESC LIMIT 10",
                        (resolved_pair,)
                    ).fetchall()
                    if _fc_closes:
                        _wins = sum(1 for r in _fc_closes if '"win"' in (r["data"] or ""))
                        _fc_pair_history = f"### Pair History\nLast {len(_fc_closes)} trades: {_wins} wins, {len(_fc_closes)-_wins} losses ({_wins/len(_fc_closes):.0%} WR)\n"
                    else:
                        _fc_pair_history = "### Pair History\nNo recent trades for this pair.\n"
            except Exception:
                _fc_pair_history = ""
            finally:
                if _fc_fr is not None:
                    try:
                        _fc_fr.close()
                    except Exception:
                        pass

            # 4. OANDA live data — pre-fetch so validator doesn't need tool calls for basics
            _fc_live_price = ""
            _fc_account = ""
            _fc_candles_text = ""
            try:
                from Source.oanda_client import OandaClient as _FcOanda
                _fc_oc = _FcOanda()

                # Live price
                _fc_px = _fc_oc.get_pricing([resolved_pair])
                _fc_prices = _fc_px.get("prices", [])
                if _fc_prices:
                    _p = _fc_prices[0]
                    _bid = float(_p.get("bids", [{}])[0].get("price", 0))
                    _ask = float(_p.get("asks", [{}])[0].get("price", 0))
                    _fc_live_price = (
                        f"### Live Price ({resolved_pair})\n"
                        f"Bid: {_bid:.5f} | Ask: {_ask:.5f} | Spread: {(_ask-_bid):.5f} | "
                        f"Tradeable: {_p.get('tradeable', '?')}\n"
                    )

                # Account summary
                _fc_acct_raw = _fc_oc.get_account_summary().get("account", {})
                _fc_account = (
                    f"### Account Status\n"
                    f"Balance: ${_fc_acct_raw.get('balance', '?')} | "
                    f"Open trades: {_fc_acct_raw.get('openTradeCount', 0)} | "
                    f"Open positions: {_fc_acct_raw.get('openPositionCount', 0)} | "
                    f"Unrealized P&L: ${_fc_acct_raw.get('unrealizedPL', '0')} | "
                    f"Margin available: ${_fc_acct_raw.get('marginAvailable', '?')}\n"
                )

                # Recent M15 candles (120 for indicator computation)
                _fc_raw_candles = _fc_oc.get_candles(resolved_pair, granularity="M15", count=120)
                if _fc_raw_candles:
                    _fc_cl = []
                    for _c in _fc_raw_candles[-5:]:
                        _m = _c.get("mid", {})
                        _fc_cl.append(f"  {_c.get('time','?')[:16]} O={_m.get('o')} H={_m.get('h')} L={_m.get('l')} C={_m.get('c')} V={_c.get('volume',0)}")
                    _fc_candles_text = f"### Recent M15 Candles ({resolved_pair})\n" + "\n".join(_fc_cl) + "\n"

                    # Compute indicators from candles
                    try:
                        from Source.indicators import Indicators as _FcInd
                        _fc_ind_obj = _FcInd(_fc_raw_candles)
                        _fc_computed = _fc_ind_obj.compute_all()

                        _fc_emas = _fc_computed.get('emas', {})
                        _e21 = round(float(_fc_emas[21].iloc[-1]), 5) if 21 in _fc_emas else 0
                        _e55 = round(float(_fc_emas[55].iloc[-1]), 5) if 55 in _fc_emas else 0
                        _e100 = round(float(_fc_emas[100].iloc[-1]), 5) if 100 in _fc_emas else 0

                        if _e21 > _e55 > _e100: _fan = 'bullish_ordered'
                        elif _e21 < _e55 < _e100: _fan = 'bearish_ordered'
                        else: _fan = 'mixed'

                        _fc_indicators = {
                            'rsi': round(_fc_computed['rsi']['value'], 1),
                            'atr': round(_fc_computed['atr']['value'], 5),
                            'macd_histogram': round(_fc_computed['macd']['histogram'], 5),
                            'bb_bandwidth': round(_fc_computed['bollinger']['bandwidth'], 5),
                            'bb_squeeze': bool(_fc_computed['bollinger']['squeeze']),
                            'bb_position': str(_fc_computed['bollinger']['position']),
                            'ema_21': _e21, 'ema_55': _e55, 'ema_100': _e100,
                            'fan_state': _fan,
                            'separation_pct': round(abs(_e21 - _e100) / _e100 * 100, 4) if _e100 else 0,
                        }
                        _fc_ta_text = (
                            f"### Computed Indicators ({resolved_pair})\n"
                            + "\n".join(f"- {k}: {v}" for k, v in _fc_indicators.items())
                            + "\n"
                        )
                        logger.info("[floor_chat] Indicators computed for %s: fan=%s rsi=%.1f bb_squeeze=%s",
                                    resolved_pair, _fan, _fc_indicators['rsi'], _fc_indicators['bb_squeeze'])
                    except Exception as _ie:
                        logger.warning("[floor_chat] Indicator computation failed: %s", _ie)

                logger.info("[floor_chat] OANDA pre-fetch for %s: price=%s account=%s candles=%d indicators=%s",
                            resolved_pair, "YES" if _fc_live_price else "NO",
                            "YES" if _fc_account else "NO", len(_fc_raw_candles) if _fc_raw_candles else 0,
                            "YES" if _fc_indicators else "NO")
            except Exception as _oe:
                logger.warning("[floor_chat] OANDA pre-fetch failed: %s", _oe)

            val_task = (
                _user_ctx_line
                + val_preamble
                + f"Last cycle: {last_cycle.get('validator_verdict','no prior cycle')} "
                f"({last_cycle.get('validator_confidence',0) * 10:.0f}% conf) — "
                f"{str(last_cycle.get('validator_reasoning',''))[:200]}\n\n"
                + _fc_intel_text + "\n"
                + _fc_ta_text + "\n"
                + _fc_pair_history + "\n"
                + (f"Trader's saved notes for this pair:\n{ann_text}\n\n" if ann_text else "")
                + _user_image_prefix
                + _thesis_line
            )
            # Save user's base64 chart to temp file for unified validator
            _fc_chart_path = None
            if _user_image_dict and _user_image_dict.get("b64"):
                try:
                    import base64 as _b64mod
                    import os as _fcos
                    _fc_chart_dir = _fcos.path.join(_fcos.path.dirname(_fcos.path.abspath(__file__)),
                                                     "Data", "charts", "user_annotations")
                    _fcos.makedirs(_fc_chart_dir, exist_ok=True)
                    _fc_chart_path = _fcos.path.join(_fc_chart_dir, f"{resolved_pair}_floor_chat_latest.png")
                    with open(_fc_chart_path, "wb") as _cf:
                        _cf.write(_b64mod.b64decode(_user_image_dict["b64"]))
                    logger.info("[floor_chat] Saved user chart to %s", _fc_chart_path)
                except Exception as _se:
                    logger.warning("[floor_chat] Failed to save user chart: %s", _se)
                    _fc_chart_path = None

            # Build data sections for unified validator
            _fc_sections = [
                {"heading": f"Chart Submission: {resolved_pair}", "content": val_preamble},
            ]
            if _fc_intel_text:
                _fc_sections.append({"heading": "Intelligence", "content": _fc_intel_text})
            if _fc_ta_text:
                _fc_sections.append({"heading": "Current Indicators", "content": _fc_ta_text})
            if _fc_pair_history:
                _fc_sections.append({"heading": "Pair History", "content": _fc_pair_history})
            if _fc_live_price:
                _fc_sections.append({"heading": "Live Price", "content": _fc_live_price})
            if _fc_account:
                _fc_sections.append({"heading": "Account Status", "content": _fc_account})
            if _fc_candles_text:
                _fc_sections.append({"heading": "Recent M15 Candles", "content": _fc_candles_text})
            if ann_text:
                _fc_sections.append({"heading": "Trader Annotations", "content": ann_text})
            if message and message != "(see annotated chart)":
                _fc_sections.append({"heading": "Trader Thesis", "content": message})
            _fc_sections.append({"heading": "Last Cycle",
                "content": f"Verdict: {last_cycle.get('validator_verdict','no prior')} "
                           f"Conf: {last_cycle.get('validator_confidence',0)*10:.0f}% — "
                           f"{str(last_cycle.get('validator_reasoning',''))[:200]}"})

            _fc_unified_params = {
                "pair": resolved_pair,
                "chart_path": "",  # No generated chart — user submitted
                "user_chart_path": _fc_chart_path,
                "indicators": _fc_indicators,
                "data_sections": _fc_sections,
                "workspace_id": "forex-trading-team",
            }

            logger.info(
                "[floor_chat] Unified validator for %s: intel=%s indicators=%s history=%s chart=%s annotations=%s sections=%d",
                resolved_pair,
                "YES" if _fc_intel_text and "No cached" not in _fc_intel_text else "NO",
                "YES" if _fc_indicators else "NO",
                "YES" if _fc_pair_history and "No recent" not in _fc_pair_history else "NO",
                "YES" if _fc_chart_path else "NO",
                "YES" if ann_text else "NO",
                len(_fc_sections),
            )

            # Call validator through the swarm agent path (manages its own DB connections)
            # The swarm handler isolates connection lifecycle from serve_ui's thread pool,
            # preventing the "database is locked" contention that occurs with in-process calls.
            import time as _vtime
            _v_start = _vtime.time()
            _val_images = None
            if _user_image_dict:
                try:
                    from Source.agents.trading_cycle import _load_v4_teaching_images
                    _teaching = _load_v4_teaching_images()
                    _val_images = _teaching + [_user_image_dict] if _teaching else [_user_image_dict]
                except Exception:
                    _val_images = [_user_image_dict]
            resp = _call_agent("validator", val_task,
                               {"instrument": resolved_pair, "floor_chat": True},
                               max_tokens=1500, timeout=120.0,
                               images=_val_images)
            _v_elapsed = _vtime.time() - _v_start

            # Flight recorder: track validator call from floor_chat path
            try:
                from flight_recorder import flight, FlightStage
                if flight:
                    # Parse the response for key fields
                    _vr = {}
                    try:
                        _vr = json.loads(resp) if isinstance(resp, str) else resp
                    except Exception:
                        pass
                    flight.record(FlightStage.VALIDATOR_CALL, pair=resolved_pair,
                                 data={"source": "floor_chat", "sections": len(_fc_sections),
                                       "has_chart": bool(_fc_chart_path), "has_indicators": bool(_fc_indicators)},
                                 note=f"Floor chat → unified validator ({len(_fc_sections)} sections)")
                    flight.record(FlightStage.VALIDATOR_VERDICT, pair=resolved_pair,
                                 data={"verdict": _vr.get("verdict"), "confidence": _vr.get("confidence"),
                                       "direction": _vr.get("direction"), "chart_read": str(_vr.get("chart_read",""))[:200],
                                       "setup_identified": _vr.get("setup_identified",""),
                                       "two_pass": _vr.get("two_pass", False),
                                       "tool_calls_count": _vr.get("tool_calls_count", 0),
                                       "vault_education_used": _vr.get("vault_education_used", False)},
                                 duration_ms=_v_elapsed * 1000,
                                 note=f"{_vr.get('verdict','?')} dir={_vr.get('direction','?')} conf={_vr.get('confidence',0)} "
                                      f"tools={_vr.get('tool_calls_count',0)} 2pass={_vr.get('two_pass',False)}")
            except Exception as _fr_err:
                logger.debug("[floor_chat] Flight recorder logging failed: %s", _fr_err)

            _add("validator", _clean_validator_response(resp, pair=resolved_pair))

            # ── Auto-snipe: if validator gives WATCH/SNIPE verdict on a chart submission,
            # create the snipe immediately — no second message needed ──────────────────
            if _is_chart_submission and resolved_pair:
                _auto_cached = _validator_json_cache.get(resolved_pair, {})
                _auto_verdict = (_auto_cached.get("verdict") or "").upper()
                if _auto_verdict in ("WATCH", "SNIPE", "TRADE_NOW"):
                    try:
                        import uuid as _auid, sqlite3 as _asql
                        from datetime import timedelta as _atd
                        # Build conditions from checklist / re_entry_conditions
                        _auto_conds = []
                        _auto_dir = (_auto_cached.get("re_entry_direction") or _auto_cached.get("direction") or "").upper() or None
                        _CKLIST = {
                            "ema_cross":        {"field": "ema_fan_state",      "op": "in",  "value": ["just_crossed","expanding","bullish_expanding","bearish_expanding"], "description": "EMA cross must confirm"},
                            "fan_opening":      {"field": "ema_fan_state",      "op": "in",  "value": ["expanding","accelerating","bullish_expanding","bearish_expanding"], "description": "Fan must be opening"},
                            "fan_accelerating": {"field": "ema_velocity",       "op": ">=",  "value": 0.003, "description": "Fan velocity ≥0.003"},
                            "bb_expanding":     {"field": "bb_expanding",       "op": "==",  "value": True,  "description": "BBs expanding"},
                            "momentum_candles": {"field": "momentum_candles",   "op": "==",  "value": True,  "description": "Momentum candles required"},
                            "rsi_recovering":   {"field": "rsi",                "op": ">=",  "value": 45,    "description": "RSI recovering ≥45"},
                            "candles_away":     {"field": "ema_price_near_e100","op": "==",  "value": True,  "description": "Price near E100"},
                            "correct_side":     {"field": "ema_fan_state",      "op": "in",  "value": ["bullish_expanding","bearish_expanding"], "description": "Price correct side of fan"},
                            "no_wall":          {"field": "ema_trend_health",   "op": "in",  "value": ["strong","recovering"], "description": "No overhead wall"},
                        }
                        logger.info("[floor_chat] auto-snipe eval for %s: verdict=%s dir=%s cache_keys=%s",
                                    resolved_pair, _auto_verdict, _auto_dir, list(_auto_cached.keys()))
                        # Priority 1: re_entry_conditions
                        if _auto_cached.get("re_entry_conditions"):
                            try:
                                from Source.agents.watch_manager import parse_suggestions as _aps
                                _awcs = _aps(_auto_cached, resolved_pair)
                                if _awcs:
                                    _auto_conds = _awcs[0].get("conditions", [])
                                    logger.info("[floor_chat] auto-snipe P1: %d conditions from re_entry_conditions", len(_auto_conds))
                            except Exception as _pe:
                                logger.warning("[floor_chat] auto-snipe P1 parse error: %s", _pe)
                        # Priority 2: checklist False items
                        if not _auto_conds and _auto_cached.get("checklist"):
                            for _k, _v in _auto_cached["checklist"].items():
                                if _v is False and _k in _CKLIST:
                                    _auto_conds.append(_CKLIST[_k])
                            if _auto_conds:
                                logger.info("[floor_chat] auto-snipe P2: %d conditions from checklist", len(_auto_conds))
                        # Priority 3: direction-based defaults when JSON parse failed / fields missing
                        if not _auto_conds:
                            _watch_note = (_auto_cached.get("watch_for") or _auto_cached.get("watch_trigger") or "").strip()
                            if _auto_dir == "BUY":
                                _auto_conds = [
                                    {"field": "ema_fan_state", "op": "in",
                                     "value": ["bullish_expanding", "just_crossed", "expanding"],
                                     "description": "EMA fan bullish — price above fan"},
                                    {"field": "bb_expanding", "op": "==", "value": True,
                                     "description": "BBs expanding (breakout confirmed)"},
                                ]
                            elif _auto_dir == "SELL":
                                _auto_conds = [
                                    {"field": "ema_fan_state", "op": "in",
                                     "value": ["bearish_expanding", "just_crossed", "expanding"],
                                     "description": "EMA fan bearish — price below fan"},
                                    {"field": "bb_expanding", "op": "==", "value": True,
                                     "description": "BBs expanding (breakout confirmed)"},
                                ]
                            else:
                                _auto_conds = [
                                    {"field": "ema_fan_state", "op": "in",
                                     "value": ["bullish_expanding", "bearish_expanding", "just_crossed", "expanding"],
                                     "description": "EMA fan expanding in any direction"},
                                ]
                            if _watch_note:
                                _auto_conds.append({"field": "watch_trigger", "op": "text",
                                                    "value": _watch_note[:200],
                                                    "description": f"Validator: {_watch_note[:100]}"})
                            logger.warning("[floor_chat] auto-snipe P3 fallback for %s: %d default conditions (dir=%s, watch=%s)",
                                           resolved_pair, len(_auto_conds), _auto_dir, bool(_watch_note))
                        if _auto_conds:
                            _anow = datetime.now(timezone.utc)
                            _acid = f"user_watch_{_auid.uuid4().hex[:8]}"
                            _athesis = _auto_cached.get("reasoning", message)[:500]
                            # Save chart image
                            _achart_path = None
                            if _user_image_dict and _user_image_dict.get("b64"):
                                try:
                                    import base64 as _ab64, os as _aos
                                    _acd = _aos.path.join(_aos.path.dirname(_aos.path.abspath(__file__)), "..", "dashboard", "user_charts")
                                    _aos.makedirs(_acd, exist_ok=True)
                                    _achart_path = _aos.path.join(_acd, f"{resolved_pair}_latest.png")
                                    _aid = _user_image_dict["b64"]
                                    if "," in _aid: _aid = _aid.split(",", 1)[1]
                                    with open(_achart_path, "wb") as _af: _af.write(_ab64.b64decode(_aid))
                                except Exception: pass
                            _actx = json.dumps({"source": "user_chat_agreement", "user_thesis": message,
                                                "validator_context": _athesis[:300],
                                                "validator_full_analysis": _athesis,
                                                "user_chart_path": _achart_path or "",
                                                "direction": _auto_dir})
                            # Cancel existing watches for this pair first
                            # Use longer timeout + retry — boardroom.db has many readers
                            # Save snipe to boardroom.db
                            # CRITICAL: busy_timeout=30000 makes SQLite WAIT for locks
                            # instead of failing immediately (which was the root cause
                            # of all "database is locked" errors).
                            _snipe_saved = False
                            _awid = 0
                            try:
                                # Use isolation_level=None (autocommit) + BEGIN IMMEDIATE
                                # Python's default BEGIN DEFERRED ignores busy_timeout on
                                # lock upgrades (read→write). BEGIN IMMEDIATE acquires the
                                # write lock upfront and properly waits up to busy_timeout.
                                # This is the SQLite-recommended pattern for concurrent writes.
                                _ac = get_trading_forex()
                                _ac.execute("PRAGMA busy_timeout=30000")
                                _ac.execute("PRAGMA wal_autocheckpoint=0")
                                _ac.execute("BEGIN IMMEDIATE")
                                # Only supersede watches with EXACT same conditions hash
                                # (snipes accumulate — different conditions coexist)
                                try:
                                    from agents.watch_manager import _compute_conditions_hash
                                    _new_hash = _compute_conditions_hash(
                                        _auto_conds, resolved_pair, (_auto_dir or "unknown")
                                    )
                                    _existing_ws = _ac.execute(
                                        "SELECT id, conditions, context FROM watch_suggestions "
                                        "WHERE instrument=? AND status='watching'",
                                        (resolved_pair,)
                                    ).fetchall()
                                    for _eid, _ecr, _ecx in _existing_ws:
                                        try:
                                            _ec = json.loads(_ecr or "[]")
                                            _ex = json.loads(_ecx or "{}")
                                            _ed = (_ex.get("re_entry_direction") or _ex.get("direction", "unknown"))
                                            if _compute_conditions_hash(_ec, resolved_pair, _ed) == _new_hash:
                                                _ac.execute(
                                                    "UPDATE watch_suggestions SET status='superseded' WHERE id=?",
                                                    (_eid,)
                                                )
                                        except Exception:
                                            pass
                                except Exception as _hash_err:
                                    logger.warning("[floor_chat] conditions-hash dedup failed, skipping supersede: %s", _hash_err)
                                _acur = _ac.execute("""
                                    INSERT INTO watch_suggestions (cycle_id, instrument, suggestion_type, conditions,
                                        raw_suggestion, validator_verdict, validator_confidence, created_at, expires_at,
                                        status, workspace_task_id, context, agent_name, user_id)
                                    VALUES (?, ?, 'user_requested', ?, ?, ?, ?, ?, '9999-12-31T23:59:59',
                                        'watching', NULL, ?, 'snipe', ?)
                                """, (_acid, resolved_pair, json.dumps(_auto_conds), _athesis[:500],
                                      _auto_verdict, float(_auto_cached.get("confidence", 7)) / 10,
                                      _anow.isoformat(), _actx, user_id))
                                _awid = _acur.lastrowid
                                _ac.commit()
                                _ac.close()
                                _snipe_saved = True
                            except Exception as _dbe:
                                logger.error("[floor_chat] auto-snipe save failed: %s", _dbe)
                                try:
                                    _ac.close()
                                except Exception:
                                    pass
                            _acond_summary = ", ".join(c.get("description") or c.get("desc", "?") for c in _auto_conds)
                            _add("cycle_orchestrator",
                                 f"✅ Snipe #{_awid} set for {resolved_pair.replace('_','/')} "
                                 f"({'BUY' if _auto_dir == 'BUY' else 'SELL' if _auto_dir == 'SELL' else 'NEUTRAL'}) — "
                                 f"monitoring: {_acond_summary}.")
                            logger.info("[floor_chat] auto-snipe #%d created for %s (%s) — %d conditions",
                                        _awid, resolved_pair, _auto_verdict, len(_auto_conds))
                    except Exception as _ase:
                        logger.warning("[floor_chat] auto-snipe failed: %s", _ase)

    elif action == "get_ta_then_validator":
        if orch_msg:
            _add("cycle_orchestrator", orch_msg)
        if not resolved_pair:
            _add("cycle_orchestrator", "Which pair do you want me to reassess?")
        else:
            # TA first — if developing thesis, ask TA to specifically describe the CURRENT
            # state of the foundation elements the trader is tracking
            # Retracement awareness preamble — injected into ALL floor-chat TA tasks.
            # The TA's generic system prompt doesn't know our strategy. Without this,
            # it describes contracting fan / narrowing BBs as "trend weakening" — wrong framing.
            # The truth: BB contraction + EMA fan slowing + price pulling toward EMAs is the
            # RETRACEMENT PHASE of a healthy trend. The fan has NOT failed unless EMAs cross.
            _ta_retracement_context = (
                "STRATEGY CONTEXT (read before reporting):\n"
                "This is a COUNTER-TREND RETRACEMENT strategy. The pattern we trade is:\n"
                "1. EMA fan expands (E21>E55>E100 bull, or E21<E55<E100 bear)\n"
                "2. Fan peaks → BBs contract → candles pull back toward EMAs (retracement phase)\n"
                "3. Key test: do candles STOP near E55 (midway) and NOT reach E100? If yes → trend intact.\n"
                "4. BBs then re-expand, fan re-accelerates, trend continues in original direction.\n"
                "CRITICAL: 'BBs contracting' and 'fan slowing' are EXPECTED RETRACEMENT SIGNALS — NOT reversal.\n"
                "The fan has only FAILED if E21 crosses BELOW E55 (bull) or ABOVE E55 (bear).\n"
                "Price holding above E55 on a bullish fan retracement = setup is alive.\n"
                "Price reaching E100 = deep retracement entry zone (best entry, not failure).\n"
                "Do NOT frame contraction as 'trend weakening' — frame it as 'where is price in the retracement cycle?'\n\n"
            )
            # 2026-04-27 (#54): fetch OANDA first; TA can't fetch data itself.
            _oanda_data = _fetch_oanda_for_chat(resolved_pair)
            _market_data_block = f"## Market Data (fetched from OANDA)\n{_oanda_data}\n\n"
            if _is_developing_thesis:
                ta_task = (
                    _ta_retracement_context
                    + _market_data_block
                    + f"Read the data above for {resolved_pair}. DESCRIBE what you see — no opinions. Report:\n"
                    f"1. EMA status: which EMAs have crossed, are they still ordered (E21>E55>E100 bull)?\n"
                    f"2. Fan velocity (%/bar) — is it still positive or has it turned negative?\n"
                    f"3. Where is price relative to E55 and E100? (above E55 = retracement alive, below E100 = deep retracement entry)\n"
                    f"4. BB state: contracting/expanding? (contraction = retracement phase, NOT reversal)\n"
                    f"5. RSI + Stoch values and direction.\n"
                    f"Numbers only. 4-6 sentences. Frame around retracement cycle phase, not 'trend strength'."
                )
            else:
                ta_task = (
                    _ta_retracement_context
                    + _market_data_block
                    + f"Read the data above for {resolved_pair}. DESCRIBE what you see — no opinions, no recommendations. "
                    f"Report: EMA order (still E21>E55>E100?), fan velocity, "
                    f"price level vs E55 and E100, BB state (contracting/expanding), RSI, Stoch. "
                    f"Frame as: where is price in the retracement cycle? Numbers only."
                )
            ta_response = _call_agent("technical_analyst", ta_task,
                                      {"instrument": resolved_pair, "floor_chat": True},
                                      max_tokens=600, timeout=30.0)
            _add("technical_analyst", ta_response)

            # Validator re-evaluates with fresh TA
            # If developing thesis: frame as "evaluate trajectory, not current state"
            if _is_developing_thesis:
                val_task = (
                    _user_ctx_line
                    + f"📸 **DEVELOPING SETUP REVIEW — {resolved_pair}**\n\n"
                    f"The trader has submitted an annotated chart showing a DEVELOPING SETUP — "
                    f"they are NOT asking you to trade right now. They are showing you what they "
                    f"EXPECT to happen over the next 5-15 candles and asking:\n"
                    f"1. Is the current foundation correct?\n"
                    f"2. Is this trajectory plausible given what's happening now?\n"
                    f"3. What specifically needs to happen for the snipe to trigger?\n\n"
                    f"**Trader's thesis:** {message}\n\n"
                    f"**Current market state (from TA):**\n{ta_response}\n\n"
                    f"**Your job:**\n"
                    f"- Look at the chart image. Check the 10-point checklist for what IS already confirmed (foundation).\n"
                    f"- Identify which checklist items are MISSING (what still needs to develop).\n"
                    f"- Tell the trader: Is this trajectory RIGHT? Is the fan actually starting to form? "
                    f"Is the direction correct? Are the EMAs behaving as they expect?\n"
                    f"- Issue a **SNIPE** with:\n"
                    f"  * Which checklist items are missing\n"
                    f"  * Estimated candles until they arrive (based on velocity)\n"
                    f"  * The specific price level / fan state that should trigger the snipe\n"
                    f"  * Any reason this thesis would FAIL (what would invalidate it)\n\n"
                    f"DO NOT say 'this isn't happening right now' — that's expected, it's a DEVELOPING setup. "
                    f"Evaluate whether the TRAJECTORY is correct and what the trigger conditions should be.\n"
                    + (f"\n{ann_text}" if ann_text else "")
                    + _user_image_prefix
                )
            else:
                val_task = (
                    _user_ctx_line
                    + f"TA just reported on {resolved_pair}:\n{ta_response}\n\n"
                    f"Previous verdict: {last_cycle.get('validator_verdict','no prior cycle')} "
                    f"({last_cycle.get('validator_confidence',0):.0%} conf)\n"
                    f"Prior reasoning: {str(last_cycle.get('validator_reasoning',''))[:300]}\n"
                    + (f"\n{ann_text}" if ann_text else "")
                    + f"\n\nTrader says: {message}\n\n"
                    f"Re-evaluate with the fresh data. Has anything changed?"
                )
            # Build validator image list: teaching first, then live chart, then user chart
            # Auto-load live chart if user didn't submit one (same logic as ask_validator path)
            _chart_for_val = _user_image_dict
            if not _chart_for_val and resolved_pair:
                try:
                    import glob as _glob2, os as _os2
                    _charts_dir2 = _os2.path.join(
                        _os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__))),
                        "Data", "charts", "live"
                    )
                    _matches2 = sorted(_glob2.glob(_os2.path.join(_charts_dir2, f"{resolved_pair}_*.png")), reverse=True)
                    if _matches2:
                        from Source.agents.trading_cycle import _load_v4_chart_image
                        _chart_for_val = _load_v4_chart_image(_matches2[0])
                        if _chart_for_val:
                            logger.info("[floor_chat] get_ta_then_validator: auto-loaded live chart %s", _matches2[0])
                except Exception as _ace2:
                    logger.warning("[floor_chat] get_ta_then_validator: could not auto-load chart: %s", _ace2)

            _val_images2 = None
            if _chart_for_val:
                try:
                    from Source.agents.trading_cycle import _load_v4_teaching_images
                    _teaching2 = _load_v4_teaching_images()
                    _val_images2 = _teaching2 + [_chart_for_val] if _teaching2 else [_chart_for_val]
                except Exception:
                    _val_images2 = [_chart_for_val]
            val_response = _call_agent("validator", val_task,
                                       {"instrument": resolved_pair, "floor_chat": True},
                                       max_tokens=1500, timeout=120.0,
                                       images=_val_images2)
            _add("validator", _clean_validator_response(val_response, pair=resolved_pair))

    elif action == "run_cycle":
        try:
            if resolved_pair:
                import json as _json, urllib.request as _ureq
                _payload = _json.dumps({
                    "pair": resolved_pair,
                    "source": "user_chat",
                    "scout_context": {"user_thesis": message, "triggered_by": "user_chat"},
                }).encode()
                _req = _ureq.Request(
                    "http://localhost:8766/api/trading/run-cycle",
                    data=_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _ureq.urlopen(_req, timeout=5) as _resp:
                    _result = _json.loads(_resp.read())
                _add("cycle_orchestrator",
                     orch_msg or f"🔄 Full cycle queued for {resolved_pair.replace('_','/')}. "
                                  f"Team is on it — results in ~60 seconds.")
            else:
                _add("cycle_orchestrator", "Which pair do you want the full team to analyse?")
        except Exception as e:
            _add("cycle_orchestrator", f"Couldn't queue cycle: {e}")

    elif action == "set_snipe":
        if not resolved_pair:
            _add("cycle_orchestrator",
                 orch_msg or "Which pair do you want the snipe on?")
        else:
            import re as _re
            import uuid as _uuid
            import sqlite3 as _sqlite3
            from datetime import timedelta as _td

            _pair_display = resolved_pair.replace("_", "/")

            # ── Step 1: Check if the validator already created a watch this session ──
            _existing = _load_recent_watch(resolved_pair, max_age_minutes=120)
            if _existing:
                _cond_list = []
                try:
                    _cond_list = json.loads(_existing.get("conditions") or "[]")
                except Exception:
                    pass
                _cond_summary = ", ".join(
                    c.get("desc") or c.get("description", "") for c in _cond_list
                ) or _existing.get("raw_suggestion", "conditions from validator")[:200]
                _add("cycle_orchestrator",
                     f"✅ Watch #{_existing['id']} is already set for {_pair_display} "
                     f"(validator {_existing.get('validator_verdict','SNIPE')}). "
                     f"Monitoring: {_cond_summary}. Will fire a full cycle when conditions hit.")
                logger.info("[floor_chat] confirmed existing watch #%d for %s", _existing["id"], resolved_pair)

            else:
                # ── Step 2: No existing watch — build from context ──
                # Prefer: validator message from recent chat_history → last cycle reasoning → user message
                _thesis = message
                direction = None
                price = None
                conditions = []

                # ── Checklist field → indicator condition mapping ──
                _CHECKLIST_TO_COND = {
                    "ema_cross":        {"field": "ema_fan_state",      "op": "in",  "value": ["just_crossed","expanding","bullish_expanding","bearish_expanding"], "desc": "EMA cross must confirm"},
                    "fan_opening":      {"field": "ema_fan_state",      "op": "in",  "value": ["expanding","accelerating","bullish_expanding","bearish_expanding"], "desc": "Fan must be opening/expanding"},
                    "fan_accelerating": {"field": "ema_velocity",       "op": ">=",  "value": 0.003, "desc": "Fan velocity must accelerate (≥0.003)"},
                    "bb_expanding":     {"field": "bb_expanding",       "op": "==",  "value": True,  "desc": "BBs must be expanding"},
                    "momentum_candles": {"field": "momentum_candles",   "op": "==",  "value": True,  "desc": "Need momentum candles"},
                    "rsi_recovering":   {"field": "rsi",                "op": ">=",  "value": 45,    "desc": "RSI must be recovering (≥45)"},
                    "candles_away":     {"field": "ema_price_near_e100","op": "==",  "value": True,  "desc": "Price must be near E100"},
                    "correct_side":     {"field": "ema_fan_state",      "op": "in",  "value": ["bullish_expanding","bearish_expanding"], "desc": "Price on correct side of fan"},
                    "no_wall":          {"field": "ema_trend_health",   "op": "in",  "value": ["strong","recovering"], "desc": "No significant overhead resistance"},
                }

                # ── PRIORITY 1: Use cached parsed validator JSON (real conditions) ──
                _cached_json = _validator_json_cache.get(resolved_pair, {})
                _val_text = ""
                for _cm in reversed(chat_history or []):
                    if _cm.get("agent") == "validator":
                        _val_text = _cm.get("text", "")
                        break

                logger.info("[floor_chat] set_snipe: cache for %s keys=%s", resolved_pair, list(_cached_json.keys()) if _cached_json else "EMPTY")

                if _cached_json and _cached_json.get("re_entry_conditions"):
                    # Full structured conditions from validator — use parse_suggestions
                    try:
                        from Source.agents.watch_manager import parse_suggestions as _ps
                        _watch_configs = _ps(_cached_json, resolved_pair)
                        if _watch_configs:
                            _wc = _watch_configs[0]
                            conditions = _wc.get("conditions", [])
                            direction = (_cached_json.get("re_entry_direction") or
                                         _cached_json.get("direction") or "").upper() or None
                            _thesis = _cached_json.get("reasoning", _val_text)[:500]
                            logger.info("[floor_chat] set_snipe: using %d structured re_entry_conditions", len(conditions))
                    except Exception as _pse:
                        logger.warning("[floor_chat] parse_suggestions failed: %s — falling back", _pse)
                        conditions = []

                if not conditions and _cached_json.get("checklist"):
                    # Build conditions from checklist False items — these ARE the missing conditions
                    _checklist = _cached_json["checklist"]
                    direction = direction or (_cached_json.get("direction") or "").upper() or None
                    _thesis = _cached_json.get("reasoning", _val_text)[:500]
                    for _item, _met in _checklist.items():
                        if _met is False and _item in _CHECKLIST_TO_COND:
                            _cm_def = _CHECKLIST_TO_COND[_item]
                            conditions.append({
                                "field": _cm_def["field"], "op": _cm_def["op"],
                                "value": _cm_def["value"],
                                "description": _cm_def["desc"],
                                "source": "checklist_missing",
                            })
                    if conditions:
                        logger.info("[floor_chat] set_snipe: built %d conditions from checklist missing items for %s",
                                    len(conditions), resolved_pair)

                # ── PRIORITY 2: Build from validator text / last cycle ──
                if not conditions:
                    if _val_text:
                        _thesis = _val_text[:500]
                        _vl = _val_text.lower()
                        _buy_score  = sum(1 for w in ["bullish", "buy", "long", "bounce", "reversal long"] if w in _vl)
                        _sell_score = sum(1 for w in ["bearish", "sell", "short", "drop", "reversal short"] if w in _vl)
                        direction = direction or ("BUY" if _buy_score > _sell_score else ("SELL" if _sell_score > _buy_score else None))
                    elif last_cycle.get("validator_reasoning"):
                        _thesis = str(last_cycle["validator_reasoning"])[:500]
                        _vl = _thesis.lower()
                        _buy_score  = sum(1 for w in ["bullish", "buy", "long", "bounce"] if w in _vl)
                        _sell_score = sum(1 for w in ["bearish", "sell", "short", "drop"] if w in _vl)
                        direction = direction or ("BUY" if _buy_score > _sell_score else ("SELL" if _sell_score > _buy_score else None))
                    else:
                        _lower = message.lower()
                        _buy_score  = sum(1 for w in ["buy", "long", "bull", "bullish", "up", "bounce"] if w in _lower)
                        _sell_score = sum(1 for w in ["sell", "short", "bear", "bearish", "down", "drop"] if w in _lower)
                        direction = direction or ("BUY" if _buy_score > _sell_score else ("SELL" if _sell_score >= _buy_score else None))

                    # Price extraction
                    _price_matches = _re.findall(r'\b(\d+\.\d{2,5})\b', _val_text or message)
                    for _pm in _price_matches:
                        _pf = float(_pm)
                        if 0.5 < _pf < 300:
                            price = _pf
                            break

                    if price and direction:
                        _field = "ask" if direction == "BUY" else "bid"
                        _op    = "<=" if direction == "BUY" else ">="
                        conditions.append({"field": _field, "op": _op, "value": price,
                                           "description": f"Price {_op} {price}"})
                    elif direction:
                        _fan_val = "bullish_expanding" if direction == "BUY" else "bearish_expanding"
                        conditions.append({"field": "fan_state", "op": "in", "value": [_fan_val],
                                           "description": f"Fan {_fan_val}"})
                    # Only add sniper fallback if no real conditions were built
                if not conditions:
                    # Use parse_suggestions to extract structured conditions from validator text
                    # This leverages the enhanced regex patterns (price zones, invalidation,
                    # EMA crosses, BB squeeze break, bandwidth thresholds)
                    try:
                        from Source.agents.watch_manager import parse_suggestions as _ps_text
                        _text_response = {
                            "recommendation": "hold",
                            "reasoning": _val_text or "",
                            "snipe_trigger": "",
                        }
                        # Extract snipe trigger section if present
                        if _val_text:
                            for _marker in ["snipe trigger:", "_snipe trigger:", "watch for:"]:
                                _idx = _val_text.lower().find(_marker)
                                if _idx >= 0:
                                    _text_response["snipe_trigger"] = _val_text[_idx:]
                                    break
                        _watch_cfgs = _ps_text(_text_response, resolved_pair)
                        if _watch_cfgs and _watch_cfgs[0].get("conditions"):
                            conditions = _watch_cfgs[0]["conditions"]
                            logger.info("[floor_chat] set_snipe: parse_suggestions extracted %d conditions from validator text for %s",
                                       len(conditions), resolved_pair)
                    except Exception as _ps_err:
                        logger.warning("[floor_chat] parse_suggestions text extraction failed: %s", _ps_err)

                    # Fallback: minimal keyword extraction if parse_suggestions found nothing
                    if not conditions and _val_text:
                        _trigger_src = _val_text[-400:].lower()
                        for _marker in ["snipe trigger:", "_snipe trigger:", "watch for:"]:
                            _idx = _val_text.lower().find(_marker)
                            if _idx >= 0:
                                _trigger_src = _val_text[_idx:_idx+400].lower()
                                break
                        if any(w in _trigger_src for w in ["bb", "band", "widen", "expand"]):
                            conditions.append({"field": "bb_expanding", "op": "==", "value": True, "description": "BBs must be expanding"})
                        if any(w in _trigger_src for w in ["e21", "e55", "ema cross", "fan", "separation"]):
                            _fv = ["bullish_expanding","just_crossed","expanding"] if direction == "BUY" else ["bearish_expanding","just_crossed","expanding"]
                            conditions.append({"field": "ema_fan_state", "op": "in", "value": _fv, "description": "EMA fan must confirm"})
                        logger.info("[floor_chat] set_snipe: keyword fallback extracted %d conditions for %s", len(conditions), resolved_pair)
                if not conditions:
                    # No chart analysis — refuse rather than create a garbage snipe
                    _add("cycle_orchestrator",
                         f"⚠️ Can't build a quality snipe for {_pair_display} — "
                         f"no chart analysis available (submit a chart first so I can build "
                         f"snipe conditions from your actual thesis).")
                    logger.warning("[floor_chat] set_snipe refused — no conditions for %s", resolved_pair)
                    return out

                _now     = datetime.now(timezone.utc)
                _expires = _now + _td(hours=12)
                _cycle_id = f"user_watch_{_uuid.uuid4().hex[:8]}"

                # Save user's chart image to disk so triggered cycle can re-use it
                _chart_image_path = None
                if _user_image_dict and _user_image_dict.get("b64"):
                    try:
                        import base64 as _b64, os as _os2
                        _chart_dir = _os2.path.join(_os2.path.dirname(_os2.path.abspath(__file__)),
                                                    "..", "dashboard", "user_charts")
                        _os2.makedirs(_chart_dir, exist_ok=True)
                        _chart_image_path = _os2.path.join(_chart_dir, f"{resolved_pair}_latest.png")
                        _img_data = _user_image_dict["b64"]
                        # Strip data URL prefix if present
                        if "," in _img_data:
                            _img_data = _img_data.split(",", 1)[1]
                        with open(_chart_image_path, "wb") as _cf:
                            _cf.write(_b64.b64decode(_img_data))
                        logger.info("[floor_chat] Saved user chart for %s → %s", resolved_pair, _chart_image_path)
                    except Exception as _ce:
                        logger.warning("[floor_chat] Could not save user chart: %s", _ce)
                        _chart_image_path = None

                # Build full context: chart path + validator analysis + conversation excerpt
                _convo_excerpt = []
                for _cm in (chat_history or [])[-6:]:
                    if _cm.get("text"):
                        _convo_excerpt.append({"role": _cm.get("agent","?"), "text": _cm["text"][:300]})

                _context_obj = json.dumps({
                    "source": "user_chat_agreement",
                    "user_thesis": message,
                    "validator_context": _val_text[:1000] if _val_text else "",
                    "validator_full_analysis": _val_text if _val_text else "",
                    "user_id": user_id,
                    "direction": direction,
                    "entry_price": price,
                    "user_chart_path": _chart_image_path,
                    "conversation_context": _convo_excerpt,
                })

                try:
                    _conn = get_trading_forex()
                    try:
                        _conn.execute("ALTER TABLE watch_suggestions ADD COLUMN user_thesis TEXT DEFAULT ''")
                        _conn.commit()
                    except Exception:
                        pass
                    _cur = _conn.execute("""
                        INSERT INTO watch_suggestions
                        (cycle_id, instrument, suggestion_type, conditions, raw_suggestion,
                         validator_verdict, validator_confidence, created_at, expires_at,
                         status, workspace_task_id, context, user_thesis, user_id)
                        VALUES (?, ?, 'user_requested', ?, ?, 'SNIPE', 0.7, ?, ?, 'watching', NULL, ?, ?, ?)
                    """, (
                        _cycle_id, resolved_pair,
                        json.dumps(conditions),
                        _thesis[:500],
                        _now.isoformat(),
                        _expires.isoformat(),
                        _context_obj,
                        _thesis[:500], user_id,
                    ))
                    _watch_id = _cur.lastrowid
                    _conn.commit()

                    _dir_display   = f" {direction}" if direction else ""
                    _price_display = f" @ {price}" if price else ""
                    _cond_summary  = ", ".join(c["description"] for c in conditions)
                    _source_note   = " (from validator)" if _val_text else ""
                    _add("cycle_orchestrator",
                         f"✅ Snipe set for {_pair_display}{_dir_display}{_price_display} "
                         f"(watch #{_watch_id}){_source_note}. "
                         f"Monitoring: {_cond_summary}. Fires a full cycle when conditions hit.")
                    logger.info("[floor_chat] user_snipe watch #%d created for %s dir=%s price=%s",
                                _watch_id, resolved_pair, direction, price)

                except Exception as _we:
                    logger.error("[floor_chat] Failed to create user snipe: %s", _we)
                    _add("cycle_orchestrator",
                         f"⚠️ Couldn't save snipe: {_we}")

    else:
        _add("cycle_orchestrator", orch_msg or "Got it.")

    _result = out if out else [{"agent": "cycle_orchestrator", "text": "On it.", "timestamp": _now_iso()}]

    # ── Capture to training DB ────────────────────────────────────────────────
    try:
        import sys as _sys, os as _os
        _cap_path = _os.path.expanduser("~/jarvis/scripts")
        if _cap_path not in _sys.path:
            _sys.path.insert(0, _cap_path)
        from conversation_capture import log_conversation
        _turns = [{"role": "user", "content": message}]
        for _m in _result:
            if _m.get("text"):
                _turns.append({"role": "assistant", "content": _m["text"], "agent": _m.get("agent","floor")})
        log_conversation(source="floor_chat", turns=_turns, pair=pair,
                         topic=message[:80] if message else None)
    except Exception:
        pass

    return _result
