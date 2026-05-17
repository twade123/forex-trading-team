"""Pinned-prompt warmer.

Sends a tiny chat completion (max_tokens=1) for each pinned prompt at startup
AND on a periodic refresh schedule. Goal: keep MLX's --prompt-cache-size LRU
populated with these prompts so live agent calls hit the cache.

This is the simplest pinning strategy — we don't reach into MLX's process,
we just keep the prompts "fresh" via traffic.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List

from .backend import MLXBackend

logger = logging.getLogger("serving.pinned_prompts")

# Repo root (Forex Trading Team/) — used to resolve system_prompt_path.
_REPO_ROOT = Path("<repo_root>")


async def _warm_one(backend: MLXBackend, prompt_id: str, system_prompt: str,
                    warmup_user_msg: str) -> bool:
    """Send a single warmup request. Returns True on success."""
    try:
        await backend.chat_completion({
            "model": "mlx-community/Qwen3.5-35B-A3B-4bit",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": warmup_user_msg},
            ],
            "max_tokens": 1,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        })
        logger.info("[warmer] pinned prompt warmed: %s (%d chars system)",
                    prompt_id, len(system_prompt))
        return True
    except Exception as e:
        logger.warning("[warmer] failed to warm %s: %s", prompt_id, e)
        return False


async def warm_all(backend: MLXBackend, pinned_specs: List[dict]) -> dict:
    """Warm every pinned prompt once. Returns dict of id -> bool success."""
    results = {}
    for spec in pinned_specs:
        prompt_path = _REPO_ROOT / spec["system_prompt_path"]
        if not prompt_path.exists():
            logger.warning("[warmer] skip %s — prompt file missing: %s",
                           spec["id"], prompt_path)
            results[spec["id"]] = False
            continue
        system_prompt = prompt_path.read_text()
        results[spec["id"]] = await _warm_one(
            backend, spec["id"], system_prompt, spec.get("warmup_user_msg", "Warmup. Reply: OK."),
        )
    return results


async def refresh_loop(backend: MLXBackend, pinned_specs: List[dict]) -> None:
    """Background task: periodically re-warm each pinned prompt at its
    refresh_seconds cadence. Runs forever."""
    loop = asyncio.get_event_loop()
    next_fire = {s["id"]: 0.0 for s in pinned_specs}  # fire ASAP at start
    while True:
        now = loop.time()
        for spec in pinned_specs:
            if now >= next_fire[spec["id"]]:
                prompt_path = _REPO_ROOT / spec["system_prompt_path"]
                if prompt_path.exists():
                    await _warm_one(
                        backend, spec["id"], prompt_path.read_text(),
                        spec.get("warmup_user_msg", "Warmup. Reply: OK."),
                    )
                next_fire[spec["id"]] = loop.time() + float(spec.get("refresh_seconds", 180))
        # Sleep until soonest next-fire (capped at 30s for responsiveness)
        soonest = min(next_fire.values())
        sleep_s = max(1.0, min(soonest - loop.time(), 30.0))
        await asyncio.sleep(sleep_s)
