"""Guardian Narrator — Local 35B agent translates guardian state into human-readable narrative.

Two jobs:
1. Floor chat: Answer "how's my trade?" from guardian state data
2. Push alerts: Generate narrative when guardian escalates (RED zone, phase transitions, SL moves)

Uses the local MLX 35B agent (port 11502) via OpenAI-compatible /chat/completions.
Falls back to template-based formatting if the model is unavailable.

2026-04-06: Created after Trade Monitor LLM was disabled. The guardian is now sole
trade manager — this module only narrates, never makes close decisions.
"""

import json
import logging
import os
import urllib.request
from typing import Dict, Optional, List

logger = logging.getLogger("trading_bot.narrator")

MLX_AGENT_URL = "http://localhost:11503/v1/chat/completions"  # serving gateway → MLX 35B
MLX_AGENT_MODEL = "mlx-community/Qwen3.5-35B-A3B-4bit"
MLX_TIMEOUT = 30  # seconds — 35B with vision-loaded server


def _call_local_agent(system_prompt: str, user_message: str, max_tokens: int = 300) -> Optional[str]:
    """Call the local MLX 35B agent. Returns response text or None on failure."""
    try:
        payload = json.dumps({
            "model": MLX_AGENT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode()
        req = urllib.request.Request(
            MLX_AGENT_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Jarvis-Tenant": "trading",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=MLX_TIMEOUT) as resp:
            body = json.loads(resp.read())
            content = body.get("choices", [{}])[0].get("message", {}).get("content")
            return (content or "").strip() or None
    except Exception as e:
        logger.debug("[NARRATOR] Local 35B unavailable: %s — using template fallback", e)
        return None


# ═══════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT — loaded from the agent's prompt file in the vault
# Falls back to a minimal inline prompt if file not found
# ═══════════════════════════════════════════════════════════════════════

_PROMPT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Prompts", "position_monitor_v5.md"
)

def _load_narrator_system() -> str:
    """Load the narrator system prompt from the vault prompt file."""
    try:
        with open(_PROMPT_FILE, 'r') as f:
            return f.read()
    except Exception:
        logger.debug("[NARRATOR] Could not load %s — using inline fallback", _PROMPT_FILE)
        return _NARRATOR_FALLBACK

_NARRATOR_FALLBACK = """You are the position monitor narrator for an M15 forex trading team.
You translate structured guardian threat data into clear, concise narratives for the trader.
Speak like a risk desk — calm, factual, current. Never recommend closing — you only narrate.
Threat zones: GREEN (0-30) safe, YELLOW (31-60) watch, RED (61-80) concern, BLACK (81+) danger.
Phases: trending, retracing (normal pullback), continuing (resuming), peak (stalling), exhaustion."""

# Lazy-loaded and cached
_narrator_system_cache = None

def _get_narrator_system() -> str:
    global _narrator_system_cache
    if _narrator_system_cache is None:
        _narrator_system_cache = _load_narrator_system()
    return _narrator_system_cache


def narrate_trade_status(guardian_state: Dict) -> str:
    """Generate a narrative for current trade status.

    Args:
        guardian_state: Dict with keys like:
            trade_id, pair, direction, threat_level, zone, phase,
            pnl_pips, entry_price, current_price, sl_price, tp_price,
            fan_state, bb_state, rsi, stoch, reasons, retrace_state
    """
    # Build a structured message for the model
    pair = guardian_state.get("pair", "?")
    direction = guardian_state.get("direction", "?").upper()
    threat = guardian_state.get("threat_level", 0)
    zone = guardian_state.get("zone", "GREEN")
    phase = guardian_state.get("retrace_state", guardian_state.get("phase", "trending"))
    pnl = guardian_state.get("pnl_pips", 0)
    reasons = guardian_state.get("reasons", [])

    user_msg = (
        f"Trade: {pair} {direction}\n"
        f"Threat: {threat}/100 ({zone})\n"
        f"Phase: {phase}\n"
        f"P&L: {pnl:+.1f} pips\n"
        f"Signals: {'; '.join(reasons[:5]) if reasons else 'none'}\n"
        f"\nGive me a 1-2 sentence status update."
    )

    response = _call_local_agent(_get_narrator_system(), user_msg, max_tokens=150)
    if response:
        return response

    # Template fallback if 35B is unavailable
    return _template_narrative(guardian_state)


def narrate_escalation(report_dict: Dict) -> str:
    """Generate a narrative for a guardian escalation event (RED zone push notification).

    Args:
        report_dict: The escalation report from build_escalation_report()
    """
    pair = report_dict.get("pair", report_dict.get("instrument", "?"))
    threat = report_dict.get("threat_level", 0)
    pnl = report_dict.get("current_pnl_pips", 0)
    reasons = report_dict.get("reasons", [])
    retrace = report_dict.get("retrace_context", {})
    retrace_state = retrace.get("retrace_state", "")

    user_msg = (
        f"ESCALATION: {pair} trade in RED zone\n"
        f"Threat: {threat}/100\n"
        f"P&L: {pnl:+.1f} pips\n"
        f"Retrace state: {retrace_state or 'none'}\n"
        f"Reasons: {'; '.join(reasons[:5])}\n"
        f"\nGenerate a concise alert message (1-2 sentences). "
        f"If in retrace, note that this is expected behavior."
    )

    response = _call_local_agent(_get_narrator_system(), user_msg, max_tokens=150)
    if response:
        return response

    # Template fallback
    if retrace_state in ("retracing", "continuing"):
        return f"{pair} threat {threat} during {retrace_state} — guardian holding, normal retrace behavior."
    return f"{pair} RED zone (threat {threat}) — {reasons[0] if reasons else 'structural concern'}. P&L: {pnl:+.1f}p."


def narrate_floor_chat(message: str, guardian_threats: Dict, open_trades: List[Dict]) -> str:
    """Answer a floor chat question about trade status using guardian data.

    Args:
        message: User's question ("how's my trade?", "what's the guardian doing?")
        guardian_threats: Dict of trade_id -> threat assessment
        open_trades: List of open trade dicts from OANDA
    """
    if not open_trades:
        return "No open trades right now."

    # Build context from all open trades
    trade_lines = []
    for trade in open_trades[:5]:
        tid = trade.get("trade_id", trade.get("id", "?"))
        pair = trade.get("instrument", trade.get("pair", "?"))
        direction = "LONG" if float(trade.get("units", trade.get("currentUnits", 0))) > 0 else "SHORT"
        pnl = float(trade.get("unrealizedPL", 0))

        # Get guardian threat for this trade
        threat_data = guardian_threats.get(str(tid), {})
        threat_level = threat_data.get("threat_level", 0)
        zone = threat_data.get("zone", "GREEN")
        phase = threat_data.get("retrace_state", "trending")
        reasons = threat_data.get("reasons", [])

        trade_lines.append(
            f"- {pair} {direction}: P&L ${pnl:+.2f}, threat {threat_level} ({zone}), "
            f"phase: {phase}, signals: {'; '.join(reasons[:3]) if reasons else 'none'}"
        )

    user_msg = (
        f"User asks: {message}\n\n"
        f"Open trades:\n" + "\n".join(trade_lines) + "\n\n"
        f"Answer the user's question about their trades. Be concise and direct."
    )

    response = _call_local_agent(_get_narrator_system(), user_msg, max_tokens=250)
    if response:
        return response

    # Template fallback — just list the trades
    lines = []
    for trade in open_trades[:5]:
        pair = trade.get("instrument", trade.get("pair", "?"))
        pnl = float(trade.get("unrealizedPL", 0))
        lines.append(f"{pair}: ${pnl:+.2f}")
    return "Open positions: " + ", ".join(lines)


def _template_narrative(state: Dict) -> str:
    """Fallback template when 9B is unavailable."""
    pair = state.get("pair", "?")
    zone = state.get("zone", "GREEN")
    threat = state.get("threat_level", 0)
    phase = state.get("retrace_state", state.get("phase", "trending"))
    pnl = state.get("pnl_pips", 0)

    if zone == "GREEN":
        return f"{pair} healthy — threat {threat}, {phase}. P&L: {pnl:+.1f}p."
    elif zone == "YELLOW":
        if phase in ("retracing", "continuing"):
            return f"{pair} in {phase} (normal) — threat {threat}. P&L: {pnl:+.1f}p. Guardian holding."
        return f"{pair} YELLOW zone — threat {threat}, {phase}. P&L: {pnl:+.1f}p. Monitoring."
    elif zone == "RED":
        if phase in ("retracing", "continuing"):
            return f"{pair} RED zone during {phase} — threat {threat}. Expected during retrace. P&L: {pnl:+.1f}p."
        return f"{pair} RED zone — threat {threat}, {phase}. P&L: {pnl:+.1f}p. Guardian evaluating."
    else:
        return f"{pair} BLACK zone — threat {threat}. P&L: {pnl:+.1f}p. Guardian managing."
