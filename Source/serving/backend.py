"""MLX backend forwarder — async POST to mlx_vlm_server_with_tools.

We forward the body verbatim (OpenAI-compat shape) and return the response.
The single MLX backend serializes requests internally; we just hold a single
in-flight slot per worker.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger("serving.backend")


class MLXBackend:
    def __init__(self, url: str, request_path: str, timeout_s: float) -> None:
        self.url = url.rstrip("/")
        self.request_path = request_path
        self.timeout_s = timeout_s
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def chat_completion(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v1/chat/completions to MLX. Returns the parsed JSON response."""
        endpoint = f"{self.url}{self.request_path}"
        # Strip our tenant-prefix routing — backend doesn't know 'trading/...'
        if isinstance(body.get("model"), str) and "/" in body["model"]:
            body = {**body, "model": body["model"].split("/", 1)[1]}
        resp = await self._client.post(endpoint, json=body)
        resp.raise_for_status()
        return resp.json()

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.url}/v1/models")
            return r.status_code == 200
        except Exception as e:
            logger.warning("MLX health probe failed: %s (%s)", e, type(e).__name__)
            return False

    async def close(self) -> None:
        await self._client.aclose()
