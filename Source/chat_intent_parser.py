"""
chat_intent_parser.py
Classifies user chat messages into actionable intents and extracts entities.
Uses Haiku for fast/cheap classification. Returns structured IntentResult.
"""

import os
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

# Known forex pairs the system trades
KNOWN_PAIRS = [
    "EUR_USD", "USD_JPY", "GBP_USD", "AUD_USD", "NZD_USD",
    "USD_CAD", "USD_CHF", "EUR_GBP", "EUR_JPY", "GBP_JPY",
    "EUR_CHF", "EUR_AUD", "AUD_JPY"
]

# Aliases users might type
PAIR_ALIASES = {
    "eurusd": "EUR_USD", "euraud": "EUR_AUD", "gbpusd": "GBP_USD",
    "usdjpy": "USD_JPY", "audusd": "AUD_USD", "nzdusd": "NZD_USD",
    "usdcad": "USD_CAD", "usdchf": "USD_CHF", "eurgbp": "EUR_GBP",
    "eurjpy": "EUR_JPY", "gbpjpy": "GBP_JPY", "eurchf": "EUR_CHF",
    "audjpy": "AUD_JPY",
    "eur/usd": "EUR_USD", "eur/aud": "EUR_AUD", "gbp/usd": "GBP_USD",
    "usd/jpy": "USD_JPY", "aud/usd": "AUD_USD", "nzd/usd": "NZD_USD",
    "usd/cad": "USD_CAD", "usd/chf": "USD_CHF", "eur/gbp": "EUR_GBP",
    "eur/jpy": "EUR_JPY", "gbp/jpy": "GBP_JPY", "eur/chf": "EUR_CHF",
    "aud/jpy": "AUD_JPY",
}


@dataclass
class IntentResult:
    type: str                          # CONFIRM_SETUP | SET_WATCH | RUN_CYCLE | ANNOTATE_TRADE | CLOSE_TRADE | QUERY | GENERAL
    pair: Optional[str] = None         # EUR_AUD etc.
    direction: Optional[str] = None    # BUY | SELL
    price: Optional[float] = None      # specific price level mentioned
    condition: Optional[str] = None    # free-text condition description
    annotation_type: Optional[str] = None  # WRONG_CLOSE | GOOD_ENTRY etc.
    annotations: List[Dict] = field(default_factory=list)  # structured chart annotations
    raw_message: str = ""
    confidence: float = 1.0


def _extract_pair(text: str) -> Optional[str]:
    """Extract forex pair from text using aliases and direct matching."""
    lower = text.lower().replace(" ", "")
    # Check aliases first
    for alias, pair in PAIR_ALIASES.items():
        if alias.replace("/", "").replace("_", "") in lower.replace("/", "").replace("_", ""):
            return pair
    # Check known pairs directly
    for pair in KNOWN_PAIRS:
        if pair.lower().replace("_", "") in lower.replace("_", ""):
            return pair
    return None


def _extract_price(text: str) -> Optional[float]:
    """Extract a price level from text."""
    # Match patterns like 1.6532, 153.40, 0.6234
    matches = re.findall(r'\b(\d+\.\d{2,5})\b', text)
    if matches:
        # Return the most "forex-like" price (typically 4-5 decimals or JPY 2-3 decimals)
        for m in matches:
            f = float(m)
            if 0.5 < f < 300:  # Reasonable forex range
                return f
    return None


def _extract_direction(text: str) -> Optional[str]:
    """Extract trade direction from text."""
    lower = text.lower()
    buy_words = ["buy", "long", "bull", "bullish", "up", "rise", "bounce", "long side"]
    sell_words = ["sell", "short", "bear", "bearish", "down", "fall", "drop", "short side"]
    buy_score = sum(1 for w in buy_words if w in lower)
    sell_score = sum(1 for w in sell_words if w in lower)
    if buy_score > sell_score:
        return "BUY"
    if sell_score > buy_score:
        return "SELL"
    return None


def _extract_annotations_from_text(text: str, pair: Optional[str], direction: Optional[str]) -> List[Dict]:
    """Convert natural language description into structured annotation objects."""
    annotations = []
    lower = text.lower()

    # EMA crosses
    if "e21" in lower and "e55" in lower and any(w in lower for w in ["cross", "crossed", "crossing"]):
        annotations.append({"type": "pattern", "note": "E21 crossed E55 (user observed)", "ema_cross": "E21xE55"})
    if "e21" in lower and "e100" in lower and any(w in lower for w in ["cross", "crossed", "crossing"]):
        annotations.append({"type": "pattern", "note": "E21 crossed E100 (user observed)", "ema_cross": "E21xE100"})
    if "e55" in lower and "e100" in lower and any(w in lower for w in ["cross", "crossed", "crossing"]):
        annotations.append({"type": "pattern", "note": "E55 crossed E100 (user observed)", "ema_cross": "E55xE100"})

    # Fan states
    if any(w in lower for w in ["fan open", "fan opening", "fan is open", "fan expanding"]):
        annotations.append({"type": "bias", "note": "Fan opening/expanding (user observed)", "fan_state": "expanding"})
    if any(w in lower for w in ["fan clos", "fan contract", "fan collaps"]):
        annotations.append({"type": "bias", "note": "Fan closing/contracting (user observed)", "fan_state": "contracting"})

    # Bollinger bands
    if any(w in lower for w in ["bb open", "bb expand", "bollinger open", "bollinger expand", "bands open", "bands expand"]):
        annotations.append({"type": "indicator", "note": "Bollinger Bands expanding (user observed)", "bb_state": "expanding"})
    if any(w in lower for w in ["bb contract", "bb squeez", "bands squeez", "bollinger squeez"]):
        annotations.append({"type": "indicator", "note": "Bollinger Bands squeezing (user observed)", "bb_state": "contracting"})

    # Support/resistance
    price = _extract_price(text)
    if price:
        if any(w in lower for w in ["support", "floor", "bounce", "hold"]):
            annotations.append({"type": "support", "price": price, "note": f"Support at {price} (user marked)"})
        elif any(w in lower for w in ["resist", "ceiling", "cap", "reject"]):
            annotations.append({"type": "resistance", "price": price, "note": f"Resistance at {price} (user marked)"})

    # Retracement
    if any(w in lower for w in ["retrace", "pullback", "pull back", "dip"]):
        annotations.append({"type": "pattern", "note": "Retracement/pullback observed by user"})

    # Phase labels
    if "phase 2.5" in lower or "phase2.5" in lower:
        annotations.append({"type": "bias", "note": "User identifies Phase 2.5 fan entry (E21xE55, not yet E100)"})
    if "phase 3" in lower or "phase3" in lower:
        annotations.append({"type": "bias", "note": "User identifies Phase 3 full fan (all EMAs ordered)"})

    # Direction bias
    if direction:
        annotations.append({"type": "bias", "direction": direction, "note": f"User bias: {direction}"})

    return annotations


def _classify_intent_fast(text: str) -> str:
    """
    Fast rule-based pre-classification before LLM call.
    Catches obvious cases cheaply.
    """
    lower = text.lower().strip()

    # Explicit commands
    if any(lower.startswith(w) for w in ["close ", "close my", "exit my", "get out"]):
        return "CLOSE_TRADE"
    if any(lower.startswith(w) for w in ["pause", "stop trading", "halt"]):
        return "PAUSE"
    if any(lower.startswith(w) for w in ["resume", "start trading", "unpause"]):
        return "RESUME"
    if any(lower.startswith(w) for w in ["run a cycle", "run cycle", "scan "]):
        return "RUN_CYCLE"
    if any(lower.startswith(w) for w in ["watch ", "monitor ", "set a watch", "watch for"]):
        return "SET_WATCH"

    # "have the team look / ask the team / ask the validator" — run full cycle, get team's answer
    # NOTE: "look at the open trade" / "look at my trade" = GENERAL (question about position)
    # Only RUN_CYCLE when explicitly asking the team to analyse the market/chart
    if any(w in lower for w in [
        "have the team", "team look", "team check", "team take a look",
        "look at this chart", "look at the chart", "check this chart", "check the chart",
        "ask the validator", "what does the validator", "validator think", "validator opinion",
        "get the team", "team analyse", "team analyze", "have a look at the chart",
    ]):
        return "RUN_CYCLE"

    # "I see a trade coming" — create watch + queue cycle
    # NOTE: "snipe" removed — snipe creation is handled by floor_chat.py's validator flow
    if any(w in lower for w in [
        "i see a trade", "trade coming", "trade setting up",
        "trade opportunity", "opportunity on", "i see an opportunity",
        "set up a watch", "create a watch",
    ]):
        return "SET_WATCH"

    # Annotation keywords — feedback on COMPLETED trades only
    # NOTE: "too early" excluded — "I got in too early" on an OPEN trade is an analysis question, not annotation
    if any(w in lower for w in ["shouldn't have closed", "wrong close", "bad close", "good close", "good skip", "bad trade", "good trade", "that was wrong", "that was right", "closed too early", "exited too early"]):
        return "ANNOTATE_TRADE"

    # Confirmation keywords — user describing what they see on chart (compare only, no cycle)
    if any(w in lower for w in ["i see", "i'm seeing", "looks like", "i think", "phase 2", "phase 3", "fan open", "bb open", "e21", "e55", "e100", "ema"]):
        return "CONFIRM_SETUP"

    # Queries — factual questions about current state
    if any(w in lower for w in ["what is", "what's", "whats", "show me", "tell me", "how is", "status of", "fan state", "current"]):
        return "QUERY"

    return "GENERAL"


def parse_intent(message: str, api_key: Optional[str] = None) -> IntentResult:
    """
    Parse a user chat message into a structured IntentResult.
    Uses fast rule-based classification first; falls back to Haiku for ambiguous cases.
    """
    if not message or not message.strip():
        return IntentResult(type="GENERAL", raw_message=message)

    text = message.strip()
    pair = _extract_pair(text)
    direction = _extract_direction(text)
    price = _extract_price(text)

    # Fast path — rule-based
    fast_type = _classify_intent_fast(text)

    # For clear intents, skip LLM entirely
    if fast_type in ("CLOSE_TRADE", "PAUSE", "RESUME", "RUN_CYCLE"):
        return IntentResult(
            type=fast_type,
            pair=pair,
            direction=direction,
            price=price,
            raw_message=text,
            confidence=0.95
        )

    # For CONFIRM_SETUP and SET_WATCH, extract annotations from text
    if fast_type in ("CONFIRM_SETUP", "SET_WATCH", "ANNOTATE_TRADE"):
        annotations = _extract_annotations_from_text(text, pair, direction)

        # Determine annotation type for ANNOTATE_TRADE
        annotation_type = None
        if fast_type == "ANNOTATE_TRADE":
            lower = text.lower()
            if any(w in lower for w in ["too early", "wrong close", "shouldn't", "bad close"]):
                annotation_type = "WRONG_CLOSE"
            elif any(w in lower for w in ["good close", "right call", "good exit"]):
                annotation_type = "CORRECT_CLOSE"
            elif any(w in lower for w in ["missed", "should have taken", "good entry"]):
                annotation_type = "MISSED_ENTRY"
            elif any(w in lower for w in ["good skip", "right to skip", "correct to skip"]):
                annotation_type = "GOOD_SKIP"

        return IntentResult(
            type=fast_type,
            pair=pair,
            direction=direction,
            price=price,
            annotations=annotations,
            annotation_type=annotation_type,
            raw_message=text,
            confidence=0.85
        )

    # QUERY or GENERAL — return as-is with entities
    return IntentResult(
        type=fast_type,
        pair=pair,
        direction=direction,
        price=price,
        raw_message=text,
        confidence=0.8
    )
