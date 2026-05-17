"""FastAPI gateway — accepts agent requests, queues by tenant priority,
forwards to one of N MLX 35B backends, returns response.

Routes:
  POST /v1/chat/completions  — main inference path
  GET  /v1/models            — passthrough to first backend
  GET  /healthz              — gateway liveness (does NOT probe backends)
  GET  /readyz               — gateway + at-least-one backend ready
  GET  /metrics              — Prometheus-style counters

Tenant resolution: X-Jarvis-Tenant header > model prefix > default.
Backend pool: round-robin across configured backends. Each backend has its
own in-flight slot count; gateway holds requests until a slot frees up.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict

import yaml
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from .backend import MLXBackend
from .pinned_prompts import warm_all, refresh_loop
from .request_queue import PriorityRequestQueue
from .tenants import resolve as resolve_tenant

logger = logging.getLogger("serving.gateway")
_HERE = Path(__file__).parent
_DEFAULT_CFG = _HERE / "config.yaml"


# ── App state ─────────────────────────────────────────────────────────────
class _State:
    cfg: dict | None = None
    backends: list[MLXBackend] = []
    backend_slots: list[asyncio.Semaphore] = []  # per-backend in-flight gating
    backend_rr = itertools.cycle([])  # round-robin iterator
    queue: PriorityRequestQueue | None = None
    workers: list[asyncio.Task] = []
    refresher: asyncio.Task | None = None
    counters: Dict[str, int] = {
        "requests_total": 0,
        "backend_errors": 0,
    }


state = _State()


def _next_backend_idx() -> int:
    """Round-robin among backends. Workers pick the next slot."""
    return next(state.backend_rr)


# ── Worker — pulls from queue, picks a backend, forwards, fulfills future ──
async def _worker(worker_id: int) -> None:
    assert state.queue is not None
    while True:
        item = await state.queue.get()
        body, tenant_name, future = item.payload
        # Pick a backend round-robin, wait for its in-flight slot
        idx = _next_backend_idx()
        backend = state.backends[idx]
        slot = state.backend_slots[idx]
        async with slot:
            try:
                t0 = time.time()
                resp = await backend.chat_completion(body)
                dt_ms = (time.time() - t0) * 1000
                logger.info("[w%d] tenant=%s prio=%d backend=%d (%s) latency=%.0fms",
                            worker_id, tenant_name, item.priority, idx,
                            state.cfg["backends"][idx]["name"], dt_ms)
                if not future.done():
                    future.set_result(resp)
            except Exception as e:
                state.counters["backend_errors"] += 1
                logger.warning("[w%d] backend=%d error tenant=%s: %s",
                               worker_id, idx, tenant_name, e)
                if not future.done():
                    future.set_exception(e)


# ── Lifespan: startup warm + worker pool, shutdown cleanup ───────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg_path = getattr(app.state, "config_path", str(_DEFAULT_CFG))
    state.cfg = yaml.safe_load(open(cfg_path))

    # Build backend pool
    state.backends = []
    state.backend_slots = []
    for be_cfg in state.cfg["backends"]:
        be = MLXBackend(be_cfg["url"], be_cfg["request_path"], float(be_cfg["request_timeout_s"]))
        state.backends.append(be)
        state.backend_slots.append(asyncio.Semaphore(int(be_cfg.get("in_flight_capacity", 1))))
    state.backend_rr = itertools.cycle(range(len(state.backends)))

    state.queue = PriorityRequestQueue()

    # Warm pinned prompts on the first backend (single-instance assumption ok for v1)
    logger.info("[gateway] warming %d pinned prompts on backend[0]...",
                len(state.cfg["pinned_prompts"]))
    warm_results = await warm_all(state.backends[0], state.cfg["pinned_prompts"])
    logger.info("[gateway] warmup complete: %s", warm_results)

    # Worker pool
    pool_size = int(state.cfg["worker_pool"]["size"])
    state.workers = [asyncio.create_task(_worker(i)) for i in range(pool_size)]

    # Periodic refresher
    state.refresher = asyncio.create_task(
        refresh_loop(state.backends[0], state.cfg["pinned_prompts"])
    )

    logger.info("[gateway] ready — port %d, %d backends, %d workers, %d pinned prompts",
                state.cfg["gateway"]["port"], len(state.backends),
                pool_size, len(state.cfg["pinned_prompts"]))

    yield

    # Shutdown
    for w in state.workers:
        w.cancel()
    if state.refresher:
        state.refresher.cancel()
    for be in state.backends:
        await be.close()


app = FastAPI(lifespan=lifespan)


# ── Routes ───────────────────────────────────────────────────────────────
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if not state.backends:
        raise HTTPException(503, "gateway not initialized")
    # At least one backend must be healthy
    for be in state.backends:
        if await be.health():
            return {"status": "ready", "healthy_backends": sum(1 for _ in state.backends)}
    raise HTTPException(503, "no healthy backends")


@app.get("/v1/models")
async def models():
    if not state.backends:
        raise HTTPException(503, "gateway not initialized")
    import httpx
    # MLX serializes requests; /v1/models can stall behind a long generation.
    # 30s ceiling is generous but bounded so a wedged backend can't hang us.
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{state.backends[0].url}/v1/models")
        r.raise_for_status()
        return r.json()


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    if not state.backends or state.queue is None or state.cfg is None:
        raise HTTPException(503, "gateway not initialized")
    body = await req.json()
    headers = dict(req.headers)
    cfg_path = getattr(req.app.state, "config_path", str(_DEFAULT_CFG))
    tenant = resolve_tenant(headers, body, config_path=cfg_path)

    state.counters["requests_total"] += 1
    counter_key = f"requests_by_tenant_{tenant.name}"
    state.counters[counter_key] = state.counters.get(counter_key, 0) + 1

    # Enqueue + wait for worker to fulfill
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    await state.queue.put(priority=tenant.priority, payload=(body, tenant.name, fut))
    try:
        return await fut
    except Exception as e:
        raise HTTPException(502, f"backend error: {e}")


@app.get("/metrics")
async def metrics():
    """Prometheus-style text format — counters + queue depth + backend health."""
    if state.queue is None:
        return PlainTextResponse("# gateway not initialized\n")
    lines = [
        "# HELP gateway_requests_total Total requests received",
        "# TYPE gateway_requests_total counter",
        f"gateway_requests_total {state.counters['requests_total']}",
        "# HELP gateway_backend_errors_total Total upstream errors",
        "# TYPE gateway_backend_errors_total counter",
        f"gateway_backend_errors_total {state.counters['backend_errors']}",
        "# HELP gateway_queue_depth Current queue depth",
        "# TYPE gateway_queue_depth gauge",
        f"gateway_queue_depth {state.queue.qsize()}",
        "# HELP gateway_backends_count Number of configured backends",
        "# TYPE gateway_backends_count gauge",
        f"gateway_backends_count {len(state.backends)}",
    ]
    for k, v in state.counters.items():
        if k.startswith("requests_by_tenant_"):
            tenant = k.replace("requests_by_tenant_", "")
            lines.append(f'gateway_requests_by_tenant{{tenant="{tenant}"}} {v}')
    return PlainTextResponse("\n".join(lines) + "\n")
