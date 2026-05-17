"""Shadow-compare a single agent request against 9B (:11500) and 35B (:11502).

Usage:
    python3 Source/scripts/shadow_compare.py <agent_name> [--max-tokens N]

Where <agent_name> is one of: floor_chat, snipe_cleanup, guardian_narrator,
intelligence_prep. Each agent has a representative payload baked in.

Prints both responses + a unified diff. Exit 0 always — this is a
diagnostic, not a gate.
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
import time
import urllib.request

ENDPOINTS = {
    "9b": "http://127.0.0.1:11500/chat/completions",
    "35b": "http://127.0.0.1:11502/v1/chat/completions",
}
MODELS = {
    "9b": "mlx-community/Qwen3.5-9B-4bit",
    "35b": "mlx-community/Qwen3.5-35B-A3B-4bit",
}

# Representative payloads — one per agent, baked in for repeatability.
PAYLOADS = {
    "floor_chat": {
        "system": (
            "You are an orchestrator that picks ONE handler for a user message. "
            "Reply with JSON: {\"handler\": \"narrator|coach|coach_chat|null\"}."
        ),
        "user": "How is my EUR_JPY trade doing?",
        "max_tokens": 300,
        "temperature": 0.2,
    },
    "snipe_cleanup": {
        "system": (
            "You are a trading setup analyst. Decide if a snipe is still worth "
            "watching.\n\nReply in this exact format:\nDECISION: KEEP or REMOVE\n"
            "SUMMARY: One sentence.\nMARKET NOW: One sentence.\nREASON: 1-2 sentences."
        ),
        "user": (
            "Snipe: SELL on EUR_USD waiting for E100 retest. Created 4h ago.\n"
            "Market now: Bearish fan intact, price retracing toward E100 as expected."
        ),
        "max_tokens": 350,
        "temperature": 0,
    },
    "guardian_narrator": {
        "system": (
            "You are the position monitor narrator. Translate guardian threat "
            "data into a 1-2 sentence narrative. Calm, factual."
        ),
        "user": (
            "Pair: EUR_USD BUY. PnL: +6.4 pips. Threat: 22 (GREEN). "
            "Phase: trending. Fan: expanding. RSI: 62. BB: expanding."
        ),
        "max_tokens": 200,
        "temperature": 0.3,
    },
    "intelligence_prep": {
        "system": (
            "You are a senior forex macro analyst. Synthesize the data into a "
            "brief intelligence note. End with BIAS: BULLISH/BEARISH/NEUTRAL."
        ),
        "user": (
            "Pair: EUR_USD\nMACRO: EUR rate 4.0%, USD rate 5.25%, diff -1.25%.\n"
            "NEWS: ECB held rates; Fed minutes hawkish.\nCORRELATIONS: GBP_USD r=0.81."
        ),
        "max_tokens": 800,
        "temperature": 0.3,
    },
}


def call(endpoint_key: str, payload_key: str) -> tuple[str, float, str | None]:
    p = PAYLOADS[payload_key]
    body = json.dumps({
        "model": MODELS[endpoint_key],
        "messages": [
            {"role": "system", "content": p["system"]},
            {"role": "user", "content": p["user"]},
        ],
        "max_tokens": p["max_tokens"],
        "temperature": p["temperature"],
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        ENDPOINTS[endpoint_key], data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        content = data["choices"][0]["message"].get("content") or ""
        return content.strip(), time.time() - t0, None
    except Exception as e:
        return "", time.time() - t0, str(e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("agent", choices=sorted(PAYLOADS.keys()))
    args = ap.parse_args()

    out_9b, lat_9b, err_9b = call("9b", args.agent)
    out_35b, lat_35b, err_35b = call("35b", args.agent)

    print(f"=== 9B ({lat_9b:.1f}s)" + (f" ERR: {err_9b}" if err_9b else "") + " ===")
    print(out_9b or "(empty)")
    print()
    print(f"=== 35B ({lat_35b:.1f}s)" + (f" ERR: {err_35b}" if err_35b else "") + " ===")
    print(out_35b or "(empty)")
    print()
    print("=== DIFF (9B → 35B) ===")
    for line in difflib.unified_diff(
        out_9b.splitlines(), out_35b.splitlines(),
        fromfile="9b", tofile="35b", lineterm="", n=2,
    ):
        print(line)


if __name__ == "__main__":
    main()
