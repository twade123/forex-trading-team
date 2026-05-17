# Shared 35B Serving Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI gateway in front of the existing MLX 35B server (port 11502) that adds priority queue, tenant routing, pinned-prompt cache warming, and OpenAI-compatible surface on port 11503 — so all trading + boardroom + future-workspace traffic shares the single 35B efficiently with prompt caching as the win.

**Architecture:** Two-process stack. MLX server (`mlx_vlm_server_with_tools.py` on port 11502) keeps the model+adapter loaded and unchanged. New FastAPI gateway (port 11503) sits in front: receives all agent traffic, tags by tenant, queues by priority, forwards to MLX, and runs a startup warmer that pre-loads pinned prompts into MLX's LRU prompt cache. Agents migrate from `:11502` → `:11503` one path at a time. Reversible (point back at 11502 if anything breaks).

**Tech Stack:** Python 3.11 (myenv), FastAPI + uvicorn, asyncio (priority queue + worker pool), httpx (async client to MLX), MLX server `--prompt-cache-size` LRU cache (already present, just tuned).

**Spec:** `Forex Trading Team/docs/superpowers/specs/2026-04-26-agent-35b-collapse-design.md` (Phase 1 + Phase 2 sections, collapsed)

**Tim's directives 2026-04-27 (deviations from spec):**
- Mac-only — no RunPod, no GPU rental
- Use the currently deployed `Qwen3.5-35B-A3B-4bit + 35b_mlx` adapter — no re-distillation
- Phase 1 + 2 collapsed into one delivery
- Migrate trading agents incrementally, gateway lives alongside MLX

---

## Lowest-risk path (decision)

| Option considered | Verdict |
|---|---|
| vLLM-on-Mac (CPU/MPS backend) | **Rejected.** vLLM is CUDA-first; Apple Silicon support is experimental and slow. Would replace MLX which is already production. |
| Fork `mlx_vlm_server_with_tools.py` to manage per-prompt-id caches in-process | **Deferred to v2.** Most powerful but invasive. Big change to a custom-patched file. |
| **FastAPI gateway in front of unchanged MLX server, using MLX's existing `--prompt-cache-size N` LRU + keepalive pings** | **CHOSEN.** Lowest risk. MLX stays as-is; gateway is additive. If gateway breaks, agents fall back to direct `:11502`. Achieves: queue, tenants, pinning via keepalive. |

The keepalive-driven cache approach: bump MLX's prompt-cache-size from 2 → 10 (enough slots for all pinned prompts), and the gateway sends a tiny pre-warming request per pinned prompt every N seconds to keep them at the top of the LRU. This works because MLX's LRU only evicts when something newer arrives AND there are no slots — keeping pinned prompts "newer" via keepalives prevents eviction.

---

## File Structure

| File | Purpose | Action |
|---|---|---|
| `Forex Trading Team/Source/serving/__init__.py` | Module marker | Create |
| `Forex Trading Team/Source/serving/gateway.py` | FastAPI app: routes, tenant detection, priority queue, MLX forwarder | Create |
| `Forex Trading Team/Source/serving/tenants.py` | Tenant config + priority resolution | Create |
| `Forex Trading Team/Source/serving/pinned_prompts.py` | Pinned-prompt registry + warmer | Create |
| `Forex Trading Team/Source/serving/metrics.py` | Cache-hit-rate / latency / queue-depth counters | Create |
| `Forex Trading Team/Source/serving/run_gateway.py` | uvicorn launcher (CLI entrypoint) | Create |
| `Forex Trading Team/Source/serving/config.yaml` | Tenant priorities + pinned-prompt list | Create |
| `Forex Trading Team/Source/serving/test_gateway.py` | Pytest suite for queue, tenant routing, warmer | Create |
| `Forex Trading Team/Source/trading_launcher.sh` | Add gateway service start/stop alongside MLX | Modify |
| `Handler/handler_swarm.py:1046` | Port `mlx/CSO` 11502 → 11503 (atomic swap when ready) | Modify |
| `Forex Trading Team/Source/intelligence_agent_prep.py:255` | URL update | Modify |
| `Forex Trading Team/Source/snipe_cleanup.py:28` | URL update | Modify |
| `Forex Trading Team/Source/guardian_narrator.py:22` | URL update | Modify |
| `Forex Trading Team/Source/news_sentiment_scorer.py:21` | URL update | Modify |
| `Forex Trading Team/Source/floor_chat.py:200` | URL update | Modify |
| `~/jarvis/scripts/mlx_servers.sh` | Bump `prompt-cache-size 2` → 10 on the CSO seat launcher | Modify |

---

## Pre-flight

- All bash commands run with `source ~/myenv/bin/activate && cd "<repo_root>"`
- DO NOT stop the MLX 35B server during this work — agents will fall back to direct calls if the gateway is down. The plan is additive.
- The gateway does NOT require any agent code changes to start working — it only takes effect once you migrate an agent's URL from `:11502` → `:11503`. Migration is per-agent and reversible.
- All commits go on `feature/kronos-scout` branch (consistent with prior work).

**Baseline metrics to capture before starting (so we can measure improvement):**

```bash
# Run a validator cycle and pull validator_call latency + prefill from flight log
source ~/myenv/bin/activate && python3 -c "
import sqlite3
c = sqlite3.connect('~/Jarvis/Database/v2/flight_recorder.db', timeout=5)
c.row_factory = sqlite3.Row
rows = c.execute('''
    SELECT timestamp, duration_ms FROM flight_log
    WHERE lower(stage)='validator_verdict' AND timestamp > datetime('now','-2 hours')
    ORDER BY timestamp DESC LIMIT 10
''').fetchall()
durations = [r['duration_ms'] or 0 for r in rows]
print(f'baseline: {len(durations)} validator calls, P50={sorted(durations)[len(durations)//2]}ms, mean={sum(durations)/max(1,len(durations)):.0f}ms')
"
```

Save the output. We compare against this after the warmer + queue ship.

---

## Task 1: Module skeleton + config file

**Files:**
- Create: `Forex Trading Team/Source/serving/__init__.py`
- Create: `Forex Trading Team/Source/serving/config.yaml`

- [ ] **Step 1.1: Create the module dir + __init__.py**

```bash
mkdir -p "<repo_root>/Source/serving" && \
echo '"""Serving stack — gateway + queue + cache warmer in front of MLX 35B."""' > \
"<repo_root>/Source/serving/__init__.py"
```

- [ ] **Step 1.2: Write the tenant + pinned-prompt config**

Create `Forex Trading Team/Source/serving/config.yaml`:

```yaml
# Serving stack config — tenants + priorities + pinned prompts.
# Loaded by gateway.py at startup. Reload requires service restart.

# Backend (the existing MLX 35B server). Gateway forwards to this.
backend:
  url: "http://127.0.0.1:11502"
  health_path: "/v1/models"
  request_path: "/v1/chat/completions"
  request_timeout_s: 240

# Listen address for the gateway itself.
gateway:
  host: "127.0.0.1"
  port: 11503

# Worker pool — single backend serializes requests anyway, but small pool lets
# us hold a few in flight without blocking on the wire.
worker_pool:
  size: 3

# Tenants — every request is tagged with one tenant. Higher priority = lower
# numeric value (priority queue is min-heap).
tenants:
  trading:
    priority: 0
    description: "Live forex trading agents (validator, TA, intelligence, guardian narrator, snipe cleanup, floor chat). Latency-sensitive."
  boardroom:
    priority: 5
    description: "Boardroom seat deliberations. Tolerates seconds."
  background:
    priority: 10
    description: "Distillation, replay, training data prep. Never preempts live work."
  default:
    priority: 7
    description: "Untagged requests. Between boardroom and background."

# Pinned prompts — the warmer pre-loads these into MLX's LRU prompt cache and
# refreshes them periodically so they don't get evicted.
# system_prompt_path is relative to the Forex Trading Team root.
pinned_prompts:
  - id: "validator-v1-canonical"
    tenant: "trading"
    system_prompt_path: "Prompts/ghost_validator_v1.md"
    warmup_user_msg: "Warmup ping. Reply with the single token: OK."
    refresh_seconds: 180
  - id: "guardian-narrator-v5"
    tenant: "trading"
    system_prompt_path: "Prompts/position_monitor_v5.md"
    warmup_user_msg: "Warmup ping. Reply: OK."
    refresh_seconds: 180

# Metrics — cache hit rate is approximated by tracking which requests share
# system-prompt prefixes with a recently-served request to the same backend.
metrics:
  prometheus_endpoint: "/metrics"
  log_every_n_requests: 50
```

- [ ] **Step 1.3: Verify YAML parses**

```bash
source ~/myenv/bin/activate && python3 -c "
import yaml
with open('<repo_root>/Source/serving/config.yaml') as f:
    cfg = yaml.safe_load(f)
assert cfg['gateway']['port'] == 11503
assert len(cfg['tenants']) == 4
assert len(cfg['pinned_prompts']) == 2
print('config OK — backend:', cfg['backend']['url'], 'gateway port:', cfg['gateway']['port'])
"
```

Expected: `config OK — backend: http://127.0.0.1:11502 gateway port: 11503`. If `yaml` import fails, install pyyaml: `pip install pyyaml`.

- [ ] **Step 1.4: Commit**

```bash
cd "<repo_root>" && \
git add Source/serving/__init__.py Source/serving/config.yaml && \
git commit -m "serving: scaffold module + tenant/pinned-prompt config"
```

---

## Task 2: Tenant resolver

**Files:**
- Create: `Forex Trading Team/Source/serving/tenants.py`
- Create: `Forex Trading Team/Source/serving/test_gateway.py` (initial)

- [ ] **Step 2.1: Write the failing test**

Create `Forex Trading Team/Source/serving/test_gateway.py`:

```python
"""Gateway tests — tenant routing, queue order, warmer behavior."""
import pytest
from pathlib import Path
import sys

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))

from serving import tenants


_CFG_PATH = str(_HERE / "config.yaml")


def test_tenant_resolves_explicit_header():
    """X-Jarvis-Tenant header takes precedence."""
    headers = {"x-jarvis-tenant": "boardroom"}
    body = {}
    resolved = tenants.resolve(headers, body, config_path=_CFG_PATH)
    assert resolved.name == "boardroom"
    assert resolved.priority == 5


def test_tenant_resolves_default_when_no_signal():
    """No header, no model prefix → default tenant."""
    resolved = tenants.resolve({}, {}, config_path=_CFG_PATH)
    assert resolved.name == "default"
    assert resolved.priority == 7


def test_tenant_resolves_from_model_prefix():
    """model='trading/qwen3.5-35b-a3b-4bit' → trading tenant."""
    body = {"model": "trading/Qwen3.5-35B-A3B-4bit"}
    resolved = tenants.resolve({}, body, config_path=_CFG_PATH)
    assert resolved.name == "trading"


def test_tenant_unknown_falls_back_to_default():
    """Unknown tenant header → default with a warning logged."""
    resolved = tenants.resolve({"x-jarvis-tenant": "asdf"}, {}, config_path=_CFG_PATH)
    assert resolved.name == "default"
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>/Source/serving" && \
python3 -m pytest test_gateway.py::test_tenant_resolves_explicit_header -v 2>&1 | tail -10
```

Expected: FAIL with `ModuleNotFoundError: No module named 'serving.tenants'`.

- [ ] **Step 2.3: Implement tenants.py**

Create `Forex Trading Team/Source/serving/tenants.py`:

```python
"""Tenant resolution — request → which tenant lane.

Resolution order:
  1. X-Jarvis-Tenant header (explicit)
  2. Model name prefix: 'trading/...' → trading; 'boardroom/...' → boardroom
  3. Fallback to 'default' tenant
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger("serving.tenants")


@dataclass(frozen=True)
class Tenant:
    name: str
    priority: int
    description: str = ""


@lru_cache(maxsize=4)
def _load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def _build_tenant(name: str, cfg: dict) -> Tenant:
    return Tenant(
        name=name,
        priority=int(cfg.get("priority", 7)),
        description=cfg.get("description", ""),
    )


def resolve(headers: dict, body: dict, config_path: str) -> Tenant:
    """Resolve a tenant from an incoming request."""
    cfg = _load_config(config_path)
    tenants_cfg = cfg.get("tenants") or {}

    # Normalize header keys to lowercase
    norm_headers = {k.lower(): v for k, v in (headers or {}).items()}
    explicit = norm_headers.get("x-jarvis-tenant")

    if explicit:
        if explicit in tenants_cfg:
            return _build_tenant(explicit, tenants_cfg[explicit])
        logger.warning("Unknown tenant '%s' from X-Jarvis-Tenant — falling back to default", explicit)

    # Model prefix routing: 'trading/...' or 'boardroom/...'
    model = (body or {}).get("model", "")
    if isinstance(model, str) and "/" in model:
        prefix = model.split("/", 1)[0]
        if prefix in tenants_cfg:
            return _build_tenant(prefix, tenants_cfg[prefix])

    # Fallback
    if "default" in tenants_cfg:
        return _build_tenant("default", tenants_cfg["default"])
    return Tenant(name="default", priority=7)
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>/Source/serving" && \
python3 -m pytest test_gateway.py -v 2>&1 | tail -15
```

Expected: 4 passed.

- [ ] **Step 2.5: Commit**

```bash
cd "<repo_root>" && \
git add Source/serving/tenants.py Source/serving/test_gateway.py && \
git commit -m "serving: tenant resolver — header > model-prefix > default"
```

---

## Task 3: Priority queue

**Files:**
- Create: `Forex Trading Team/Source/serving/queue.py`
- Modify: `Forex Trading Team/Source/serving/test_gateway.py` (append)

- [ ] **Step 3.1: Write the failing test**

Append to `test_gateway.py`:

```python
import asyncio
from serving.queue import PriorityRequestQueue


@pytest.mark.asyncio
async def test_queue_orders_by_priority():
    """Lower priority value dequeues first (min-heap)."""
    q = PriorityRequestQueue()
    await q.put(priority=5, payload="boardroom-request")
    await q.put(priority=0, payload="trading-request")
    await q.put(priority=10, payload="background-request")

    first = await q.get()
    second = await q.get()
    third = await q.get()

    assert first.payload == "trading-request"
    assert second.payload == "boardroom-request"
    assert third.payload == "background-request"


@pytest.mark.asyncio
async def test_queue_fifo_within_same_priority():
    """Same priority → FIFO."""
    q = PriorityRequestQueue()
    await q.put(priority=0, payload="A")
    await q.put(priority=0, payload="B")
    await q.put(priority=0, payload="C")

    assert (await q.get()).payload == "A"
    assert (await q.get()).payload == "B"
    assert (await q.get()).payload == "C"
```

- [ ] **Step 3.2: Run test to verify it fails**

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>/Source/serving" && \
python3 -m pytest test_gateway.py::test_queue_orders_by_priority -v 2>&1 | tail -10
```

Expected: FAIL with `ModuleNotFoundError: No module named 'serving.queue'`.

- [ ] **Step 3.3: Implement queue.py**

Create `Forex Trading Team/Source/serving/queue.py`:

```python
"""Priority queue with FIFO tie-break.

asyncio.PriorityQueue uses heapq under the hood. We push (priority, seq, payload)
tuples so equal priorities resolve in insertion order via the monotonic seq.
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from typing import Any


@dataclass
class QueueItem:
    priority: int
    payload: Any


class PriorityRequestQueue:
    """Async priority queue. Lower priority value = served first."""

    def __init__(self) -> None:
        self._q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()

    async def put(self, priority: int, payload: Any) -> None:
        seq = next(self._seq)
        await self._q.put((priority, seq, payload))

    async def get(self) -> QueueItem:
        priority, _seq, payload = await self._q.get()
        return QueueItem(priority=priority, payload=payload)

    def qsize(self) -> int:
        return self._q.qsize()
```

- [ ] **Step 3.4: Install pytest-asyncio if missing + run tests**

```bash
source ~/myenv/bin/activate && pip show pytest-asyncio >/dev/null 2>&1 || pip install pytest-asyncio
source ~/myenv/bin/activate && \
cd "<repo_root>/Source/serving" && \
python3 -m pytest test_gateway.py -v -p asyncio --asyncio-mode=auto 2>&1 | tail -15
```

Expected: 6 passed.

- [ ] **Step 3.5: Commit**

```bash
cd "<repo_root>" && \
git add Source/serving/queue.py Source/serving/test_gateway.py && \
git commit -m "serving: async priority queue with FIFO tie-break"
```

---

## Task 4: MLX backend forwarder

**Files:**
- Create: `Forex Trading Team/Source/serving/backend.py`

- [ ] **Step 4.1: Write the implementation directly (integration-test only — depends on live MLX)**

Create `Forex Trading Team/Source/serving/backend.py`:

```python
"""MLX backend forwarder — async POST to mlx_vlm_server_with_tools.

We forward the body verbatim (OpenAI-compat shape) and stream back the response.
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
            r = await self._client.get(f"{self.url}/v1/models", timeout=5)
            return r.status_code == 200
        except Exception as e:
            logger.warning("MLX health probe failed: %s", e)
            return False

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4.2: Smoke-test via an inline runner**

```bash
source ~/myenv/bin/activate && python3 << 'EOF'
import asyncio, sys
sys.path.insert(0, "<repo_root>/Source")
from serving.backend import MLXBackend

async def main():
    b = MLXBackend("http://127.0.0.1:11502", "/v1/chat/completions", 30)
    ok = await b.health()
    assert ok, "MLX 35B not reachable on port 11502"
    print("health OK")
    resp = await b.chat_completion({
        "model": "mlx-community/Qwen3.5-35B-A3B-4bit",
        "messages": [{"role": "user", "content": "Say OK."}],
        "max_tokens": 10, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    })
    content = resp["choices"][0]["message"].get("content") or ""
    print(f"backend roundtrip OK: {content[:60]!r}")
    await b.close()

asyncio.run(main())
EOF
```

Expected: `health OK` then `backend roundtrip OK: 'OK'` (or similar). If it errors, MLX server is down or timing out.

- [ ] **Step 4.3: Commit**

```bash
cd "<repo_root>" && \
git add Source/serving/backend.py && \
git commit -m "serving: async MLX backend forwarder (httpx, OpenAI-compat passthrough)"
```

---

## Task 5: Pinned-prompt warmer

**Files:**
- Create: `Forex Trading Team/Source/serving/pinned_prompts.py`

- [ ] **Step 5.1: Implement the warmer**

Create `Forex Trading Team/Source/serving/pinned_prompts.py`:

```python
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
    # Build per-spec next-fire timestamps
    next_fire = {s["id"]: 0.0 for s in pinned_specs}  # fire ASAP at start
    loop = asyncio.get_event_loop()
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
                next_fire[spec["id"]] = now + float(spec.get("refresh_seconds", 180))
        # Sleep until next-fire of the soonest pinned prompt
        await asyncio.sleep(min(max(next_fire.values()) - loop.time(), 30))
```

- [ ] **Step 5.2: Smoke-test the warmer once**

```bash
source ~/myenv/bin/activate && python3 << 'EOF'
import asyncio, sys, yaml
sys.path.insert(0, "<repo_root>/Source")
from serving.backend import MLXBackend
from serving.pinned_prompts import warm_all

async def main():
    cfg = yaml.safe_load(open("<repo_root>/Source/serving/config.yaml"))
    b = MLXBackend(cfg["backend"]["url"], cfg["backend"]["request_path"], 90)
    results = await warm_all(b, cfg["pinned_prompts"])
    print("warm results:", results)
    assert all(results.values()), f"Some prompts failed: {results}"
    await b.close()

asyncio.run(main())
EOF
```

Expected: `warm results: {'validator-v1-canonical': True, 'guardian-narrator-v5': True}`. Each warmup call may take ~30-60s on first run (cold cache), then ~1-3s on subsequent calls.

- [ ] **Step 5.3: Commit**

```bash
cd "<repo_root>" && \
git add Source/serving/pinned_prompts.py && \
git commit -m "serving: pinned-prompt warmer with periodic refresh"
```

---

## Task 6: Bump MLX prompt-cache-size

**Files:**
- Modify: `~/jarvis/scripts/mlx_servers.sh`

- [ ] **Step 6.1: Find the current prompt-cache-size flag for CSO**

```bash
grep -n "prompt-cache-size\|prompt_cache_size" ~/jarvis/scripts/mlx_servers.sh
```

Expected: a line with `--prompt-cache-size 2` somewhere in the CSO seat launch command. If not present, the launcher uses MLX's default (which is 1 — only cache last request).

- [ ] **Step 6.2: Edit to set 10**

If the flag exists, change `2` → `10`. If it doesn't exist, add `--prompt-cache-size 10` to the CSO seat launch command in mlx_servers.sh. Use the Edit tool with `replace_all=False`. The CSO seat is the 35B (port 11502).

After edit, verify:

```bash
grep -n "prompt-cache-size" ~/jarvis/scripts/mlx_servers.sh
```

Expected: shows `--prompt-cache-size 10` on the CSO line.

- [ ] **Step 6.3: Lint (zsh script)**

```bash
zsh -n ~/jarvis/scripts/mlx_servers.sh && echo "zsh syntax OK"
```

- [ ] **Step 6.4: Commit (in ~/jarvis repo)**

```bash
cd ~/jarvis && \
git add scripts/mlx_servers.sh && \
git commit -m "mlx_servers: bump CSO prompt-cache-size 2->10 to fit pinned + live prompts"
```

Note: this won't take effect on the running 35B until you restart it. Don't restart yet — wait until the gateway is wired in (Task 11).

---

## Task 7: Gateway FastAPI app

**Files:**
- Create: `Forex Trading Team/Source/serving/gateway.py`
- Create: `Forex Trading Team/Source/serving/run_gateway.py`

- [ ] **Step 7.1: Implement gateway.py**

Create `Forex Trading Team/Source/serving/gateway.py`:

```python
"""FastAPI gateway — accepts agent requests, queues by tenant priority,
forwards to MLX 35B backend, returns response.

Routes:
  POST /v1/chat/completions  — main inference path
  GET  /v1/models            — passthrough to backend
  GET  /healthz              — gateway liveness (does NOT probe backend)
  GET  /readyz               — gateway + backend ready check
  GET  /metrics              — counters (added in Task 9)

Tenant resolution: X-Jarvis-Tenant header > model prefix > default.
Priority queue: lower = served first. Single backend serializes inference;
the queue + worker pool just keeps requests ordered while waiting.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

import yaml
from fastapi import FastAPI, Request, HTTPException

from .backend import MLXBackend
from .pinned_prompts import warm_all, refresh_loop
from .queue import PriorityRequestQueue
from .tenants import resolve as resolve_tenant

logger = logging.getLogger("serving.gateway")
_HERE = Path(__file__).parent
_DEFAULT_CFG = _HERE / "config.yaml"


# ── App state ─────────────────────────────────────────────────────────────
class _State:
    cfg: dict | None = None
    backend: MLXBackend | None = None
    queue: PriorityRequestQueue | None = None
    workers: list[asyncio.Task] = []
    refresher: asyncio.Task | None = None
    counters: Dict[str, int] = {
        "requests_total": 0,
        "requests_by_tenant_trading": 0,
        "requests_by_tenant_boardroom": 0,
        "requests_by_tenant_background": 0,
        "requests_by_tenant_default": 0,
        "backend_errors": 0,
    }


state = _State()


# ── Worker — pulls from queue, forwards to backend, fulfills future ──────
async def _worker(worker_id: int) -> None:
    assert state.queue is not None and state.backend is not None
    while True:
        item = await state.queue.get()
        body, tenant_name, future = item.payload
        try:
            t0 = time.time()
            resp = await state.backend.chat_completion(body)
            dt_ms = (time.time() - t0) * 1000
            logger.info("[w%d] tenant=%s prio=%d backend=%.0fms",
                        worker_id, tenant_name, item.priority, dt_ms)
            future.set_result(resp)
        except Exception as e:
            state.counters["backend_errors"] += 1
            logger.warning("[w%d] backend error tenant=%s: %s", worker_id, tenant_name, e)
            if not future.done():
                future.set_exception(e)


# ── Lifespan: startup warm + worker pool, shutdown cleanup ───────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg_path = app.state.config_path if hasattr(app.state, "config_path") else _DEFAULT_CFG
    state.cfg = yaml.safe_load(open(cfg_path))
    state.backend = MLXBackend(
        state.cfg["backend"]["url"],
        state.cfg["backend"]["request_path"],
        float(state.cfg["backend"]["request_timeout_s"]),
    )
    state.queue = PriorityRequestQueue()

    # Warm pinned prompts at startup (block until done so we know agents will hit cache)
    logger.info("[gateway] warming %d pinned prompts...", len(state.cfg["pinned_prompts"]))
    warm_results = await warm_all(state.backend, state.cfg["pinned_prompts"])
    logger.info("[gateway] warmup complete: %s", warm_results)

    # Spawn worker pool
    pool_size = int(state.cfg["worker_pool"]["size"])
    state.workers = [asyncio.create_task(_worker(i)) for i in range(pool_size)]

    # Start the periodic refresher
    state.refresher = asyncio.create_task(
        refresh_loop(state.backend, state.cfg["pinned_prompts"])
    )

    logger.info("[gateway] ready — port %d, %d workers, %d pinned prompts",
                state.cfg["gateway"]["port"], pool_size, len(state.cfg["pinned_prompts"]))

    yield

    # Shutdown
    for w in state.workers:
        w.cancel()
    if state.refresher:
        state.refresher.cancel()
    if state.backend:
        await state.backend.close()


app = FastAPI(lifespan=lifespan)


# ── Routes ───────────────────────────────────────────────────────────────
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    if state.backend is None:
        raise HTTPException(503, "gateway not initialized")
    backend_ok = await state.backend.health()
    if not backend_ok:
        raise HTTPException(503, "backend unhealthy")
    return {"status": "ready"}


@app.get("/v1/models")
async def models():
    if state.backend is None:
        raise HTTPException(503, "gateway not initialized")
    # Passthrough — tell the caller what the backend serves
    import httpx
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{state.backend.url}/v1/models")
        r.raise_for_status()
        return r.json()


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    if state.backend is None or state.queue is None or state.cfg is None:
        raise HTTPException(503, "gateway not initialized")
    body = await req.json()
    headers = dict(req.headers)
    cfg_path = req.app.state.config_path if hasattr(req.app.state, "config_path") else str(_DEFAULT_CFG)
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
```

- [ ] **Step 7.2: Implement run_gateway.py**

Create `Forex Trading Team/Source/serving/run_gateway.py`:

```python
"""CLI launcher — starts the gateway via uvicorn.

Usage:
  python3 -m serving.run_gateway [--config PATH] [--host HOST] [--port PORT]

Defaults: config.yaml in this directory; host/port from config.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn
import yaml

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))

from serving.gateway import app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(_HERE / "config.yaml"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg = yaml.safe_load(open(args.config))
    host = args.host or cfg["gateway"]["host"]
    port = args.port or int(cfg["gateway"]["port"])

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Pin config path on the app so lifespan loads the right one
    app.state.config_path = args.config

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
```

- [ ] **Step 7.3: Lint**

```bash
source ~/myenv/bin/activate && python3 -c "
import ast
for p in ['gateway.py', 'run_gateway.py']:
    with open(f'<repo_root>/Source/serving/{p}') as f:
        ast.parse(f.read())
print('syntax OK')
"
```

- [ ] **Step 7.4: Install deps if missing**

```bash
source ~/myenv/bin/activate && pip install fastapi uvicorn httpx pyyaml 2>&1 | tail -3
```

- [ ] **Step 7.5: Commit**

```bash
cd "<repo_root>" && \
git add Source/serving/gateway.py Source/serving/run_gateway.py && \
git commit -m "serving: FastAPI gateway with priority queue + warmer + tenant routing"
```

---

## Task 8: Smoke-test the gateway end-to-end

- [ ] **Step 8.1: Start the gateway in a terminal**

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>/Source" && \
python3 -m serving.run_gateway 2>&1 | tee /tmp/gateway_smoke.log &
```

Expected log output: `warming 2 pinned prompts...`, then `warmup complete`, then `ready — port 11503`.

- [ ] **Step 8.2: Smoke probe healthz + readyz**

```bash
sleep 3
curl -s http://127.0.0.1:11503/healthz
echo
curl -s http://127.0.0.1:11503/readyz
echo
```

Expected: `{"status":"ok"}` then `{"status":"ready"}`.

- [ ] **Step 8.3: Send a real chat completion through the gateway**

```bash
curl -s -X POST http://127.0.0.1:11503/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Jarvis-Tenant: trading" \
  -d '{
    "model": "mlx-community/Qwen3.5-35B-A3B-4bit",
    "messages": [{"role":"user","content":"Reply with the single word OK."}],
    "max_tokens": 5,
    "temperature": 0,
    "chat_template_kwargs": {"enable_thinking": false}
  }' | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['choices'][0]['message'].get('content'))"
```

Expected: `OK` (or similar single-token reply).

- [ ] **Step 8.4: Verify tenant counter incremented**

```bash
grep -E "tenant=trading" /tmp/gateway_smoke.log | tail -2
```

Expected: a log line `[w0] tenant=trading prio=0 backend=...ms`.

- [ ] **Step 8.5: Stop the gateway (move to next task)**

```bash
# Find and kill the gateway process
pkill -f "serving.run_gateway"
sleep 1
```

- [ ] **Step 8.6: No commit** — verification only.

---

## Task 9: Metrics endpoint

**Files:**
- Modify: `Forex Trading Team/Source/serving/gateway.py` (add /metrics route)

- [ ] **Step 9.1: Add /metrics route**

In `gateway.py`, after the existing `@app.get("/v1/models")` route, add:

```python
@app.get("/metrics")
async def metrics():
    """Prometheus-style text format — counters + queue depth."""
    if state.queue is None:
        return "# gateway not initialized\n"
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
    ]
    for k, v in state.counters.items():
        if k.startswith("requests_by_tenant_"):
            tenant = k.replace("requests_by_tenant_", "")
            lines.append(f"gateway_requests_by_tenant{{tenant=\"{tenant}\"}} {v}")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n")
```

- [ ] **Step 9.2: Smoke-test**

Restart gateway (Task 8.1), send 3 requests with different tenants, then:

```bash
curl -s http://127.0.0.1:11503/metrics
```

Expected: text with counters per tenant + queue depth.

- [ ] **Step 9.3: Commit**

```bash
cd "<repo_root>" && \
git add Source/serving/gateway.py && \
git commit -m "serving: /metrics endpoint (Prometheus text format)"
```

Stop the gateway again before next task: `pkill -f "serving.run_gateway"`.

---

## Task 10: Wire gateway into trading_launcher.sh

**Files:**
- Modify: `Forex Trading Team/Source/trading_launcher.sh`

- [ ] **Step 10.1: Read current launcher service definitions**

```bash
sed -n '20,55p' "<repo_root>/Source/trading_launcher.sh"
```

- [ ] **Step 10.2: Add gateway service definition**

After the `MLX_TA_*` block (around line 44), insert these lines:

```bash
GATEWAY_NAME="serving-gateway"
GATEWAY_CMD="$PYTHON -m serving.run_gateway"
GATEWAY_PID="$PID_DIR/serving_gateway.pid"
GATEWAY_LOG="$LOG_DIR/serving_gateway.log"
GATEWAY_PORT=11503
```

Use Edit tool with `replace_all=False`. Match the existing block style (no trailing whitespace).

- [ ] **Step 10.3: Add gateway start AFTER MLX warmup (so MLX is ready when warmer fires)**

In the `do_start()` function, after the `[mlx] 35B warm ✓` block, add:

```bash
    # Serving gateway — starts after MLX is warm so the pinned-prompt warmer
    # has a live backend to hit.
    echo "[gateway] starting serving gateway on port $GATEWAY_PORT..."
    cd "$SCRIPT_DIR" && \
    nohup $PYTHON -m serving.run_gateway >> "$GATEWAY_LOG" 2>&1 &
    GW_PID=$!
    echo "$GW_PID" > "$GATEWAY_PID"
    # Wait up to 20s for /healthz
    for i in $(seq 1 20); do
        if curl -s --max-time 2 "http://127.0.0.1:$GATEWAY_PORT/healthz" > /dev/null 2>&1; then
            echo "[gateway] ready (PID $GW_PID, port $GATEWAY_PORT)"
            break
        fi
        sleep 1
    done
```

- [ ] **Step 10.4: Add gateway stop in `do_stop()`**

In `do_stop()`, before `stop_service "$MLX_TA_NAME" ...`, add:

```bash
    # Stop gateway BEFORE MLX (so in-flight gateway requests don't error mid-shutdown)
    if [ -f "$GATEWAY_PID" ]; then
        local gw_pid
        gw_pid=$(cat "$GATEWAY_PID")
        if kill -0 "$gw_pid" 2>/dev/null; then
            echo "[gateway] stopping (PID $gw_pid)..."
            kill "$gw_pid" 2>/dev/null || true
            sleep 2
            kill -9 "$gw_pid" 2>/dev/null || true
        fi
        rm -f "$GATEWAY_PID"
    fi
```

- [ ] **Step 10.5: Add gateway to `do_status()`**

In `do_status()`, the for-loop services list, add `"$GATEWAY_NAME:$GATEWAY_PID:$GATEWAY_PORT"` before the `MLX_TA_NAME` entry.

- [ ] **Step 10.6: Lint**

```bash
zsh -n "<repo_root>/Source/trading_launcher.sh" && echo "syntax OK"
```

- [ ] **Step 10.7: Commit**

```bash
cd "<repo_root>" && \
git add Source/trading_launcher.sh && \
git commit -m "launcher: start serving-gateway alongside MLX 35B"
```

---

## Task 11: Atomic agent migration via handler_swarm

**Files:**
- Modify: `Handler/handler_swarm.py:1046`

- [ ] **Step 11.1: Find the MLX_SERVERS port mapping**

```bash
grep -nE 'CSO.*port.*11502|"port":\s*11502' ~/Jarvis/Handler/handler_swarm.py
```

Expected: line ~1046, `"CSO": {"port": 11502, "hf_repo": "mlx-community/Qwen3.5-35B-A3B-4bit"}`.

- [ ] **Step 11.2: Edit the port to 11503**

Use Edit tool, `replace_all=False`:

```python
# Old:
"CSO": {"port": 11502, "hf_repo": "mlx-community/Qwen3.5-35B-A3B-4bit"},
# New:
"CSO": {"port": 11503, "hf_repo": "mlx-community/Qwen3.5-35B-A3B-4bit"},  # 2026-04-27: route via serving gateway
```

This single change auto-redirects ALL swarm-dispatched agents (validator + 7 trading agents) to the gateway.

- [ ] **Step 11.3: Verify**

```bash
grep -nE '"CSO".*"port":.*11503' ~/Jarvis/Handler/handler_swarm.py
```

Expected: shows the new line.

- [ ] **Step 11.4: Commit (in ~/Jarvis repo if separate)**

```bash
cd ~/Jarvis && \
git add Handler/handler_swarm.py && \
git commit -m "swarm: route mlx/CSO via serving gateway (port 11503)"
```

---

## Task 12: Migrate the 5 direct-call helpers

**Files:**
- Modify: `intelligence_agent_prep.py:255`, `snipe_cleanup.py:28`, `guardian_narrator.py:22`, `news_sentiment_scorer.py:21`, `floor_chat.py:200`

- [ ] **Step 12.1: One-liner sed across all five files (with backup commit boundary)**

Use Edit per file (NOT sed bulk — too easy to miss context). For each file, change `127.0.0.1:11502` or `localhost:11502` → the gateway port `11503`. Add the `X-Jarvis-Tenant: trading` header where the request is constructed.

For example, in `floor_chat.py:200`:

```python
# Old:
"http://127.0.0.1:11502/v1/chat/completions",
data=payload,
headers={"Content-Type": "application/json"},

# New:
"http://127.0.0.1:11503/v1/chat/completions",
data=payload,
headers={"Content-Type": "application/json", "X-Jarvis-Tenant": "trading"},
```

Same pattern for the other four files. Each has a slightly different request construction style — read each file before editing.

- [ ] **Step 12.2: Verify all 11502 references in trading code are gone (except the launcher's MLX_TA_PORT)**

```bash
grep -rn "11502" "<repo_root>/Source/" --include="*.py" 2>/dev/null | grep -v __pycache__
```

Expected: zero matches after migration. The `trading_launcher.sh` still has MLX_TA_PORT=11502 (correct — that's the backend MLX, gateway uses 11503).

- [ ] **Step 12.3: Commit**

```bash
cd "<repo_root>" && \
git add Source/intelligence_agent_prep.py Source/snipe_cleanup.py Source/guardian_narrator.py Source/news_sentiment_scorer.py Source/floor_chat.py && \
git commit -m "agents: route direct-call helpers via serving gateway (11502 -> 11503) + tenant=trading"
```

---

## Task 13: End-to-end verification

- [ ] **Step 13.1: Reload the trading service**

```bash
"<repo_root>/Source/trading_launcher.sh" reload
```

This restarts serve_ui + watchdog + starts the new gateway. MLX 35B stays warm. Wait ~10s for gateway to warm pinned prompts.

- [ ] **Step 13.2: Verify gateway came up**

```bash
curl -s http://127.0.0.1:11503/readyz
echo
curl -s http://127.0.0.1:11503/metrics
```

Expected: `{"status":"ready"}` then metrics text.

- [ ] **Step 13.3: Run a trading cycle**

Either trigger via dashboard, or run manually:

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>" && \
python3 scripts/run_trading_cycle.py 2>&1 | tee /tmp/cycle_post_gateway.log | tail -40
```

Expected: cycle completes without errors. Validator + TA + intelligence calls all succeed.

- [ ] **Step 13.4: Compare prefill tokens against baseline**

Watch the MLX server log during the cycle:

```bash
tail -50 "~/jarvis/Forex Trading Team/Source/logs/mlx_35b.log" | grep -E "Prefill: 100%" | tail -5
```

Expected: validator-shaped requests now show prefill of ~3-5K tokens (was 10-15K before). The system prompt is in MLX's prompt cache from the warmer, so only live data + chart prefill.

- [ ] **Step 13.5: Compare validator latency against baseline**

```bash
source ~/myenv/bin/activate && python3 -c "
import sqlite3
c = sqlite3.connect('~/Jarvis/Database/v2/flight_recorder.db', timeout=5)
c.row_factory = sqlite3.Row
rows = c.execute('''
    SELECT duration_ms FROM flight_log
    WHERE lower(stage)='validator_verdict' AND timestamp > datetime('now','-15 minutes')
    ORDER BY timestamp DESC LIMIT 5
''').fetchall()
durations = [r['duration_ms'] or 0 for r in rows]
print(f'post-gateway: {len(durations)} validator calls, P50={sorted(durations)[len(durations)//2]}ms, mean={sum(durations)/max(1,len(durations)):.0f}ms')
"
```

Expected: P50 latency drops vs the baseline you captured pre-flight. Concrete win signal: 50-70% reduction.

- [ ] **Step 13.6: Verify metrics show real traffic**

```bash
curl -s http://127.0.0.1:11503/metrics | grep tenant
```

Expected: `gateway_requests_by_tenant{tenant="trading"} N` where N > 0.

- [ ] **Step 13.7: No commit** — verification only.

---

## Task 14: Rollback playbook (document for emergency)

**Files:**
- Create: `Forex Trading Team/Source/serving/ROLLBACK.md`

- [ ] **Step 14.1: Write the rollback doc**

Create `Forex Trading Team/Source/serving/ROLLBACK.md`:

```markdown
# Serving Gateway — Rollback Playbook

If the gateway is misbehaving and you need to bypass it immediately:

## Option 1: Point swarm back at MLX directly

```bash
cd ~/Jarvis && \
git checkout Handler/handler_swarm.py
# Then reload the trading service
"<repo_root>/Source/trading_launcher.sh" reload
```

This puts CSO seat back at port 11502 (direct MLX). All swarm-dispatched agents
revert to direct MLX calls. The gateway can stay running but receives no traffic.

## Option 2: Revert ALL gateway-related migrations

```bash
cd "<repo_root>" && \
git revert <gateway commit SHAs in reverse order>
"<repo_root>/Source/trading_launcher.sh" restart
```

## Option 3: Stop just the gateway, agents keep using MLX directly

If swarm is back at 11502 but you want to silence the gateway:

```bash
pkill -f "serving.run_gateway"
```

The gateway also stops on `trading_launcher.sh stop`.

## Symptoms that warrant rollback

- /readyz returns 503 for >5 minutes
- Validator latency INCREASED (warmer might be evicting cache instead of preserving it)
- Errors reach >10% of requests in /metrics
- Backend (MLX) shows 5xx in MLX log because gateway is hammering it
```

- [ ] **Step 14.2: Commit**

```bash
cd "<repo_root>" && \
git add Source/serving/ROLLBACK.md && \
git commit -m "serving: rollback playbook"
```

---

## Self-Review

**Spec coverage:**
- Phase 1 (vLLM/engine choice) → resolved by "lowest-risk path" decision (keep MLX, add gateway in front). Documented in plan header.
- Phase 2 gateway (FastAPI, OpenAI-compat, tenant headers, priority queue, backend registry, health checks, failover) → Tasks 1-7 build it. Task 14 documents failover.
- Pinned prompt cache → Tasks 5-6.
- Tenant routing → Task 2 + Task 7.
- OpenAI-compat /v1/chat/completions surface → Task 7 route definition.
- Adapter routing — same 35B base + 35b_mlx adapter → unchanged (gateway is transparent passthrough; adapter loads at MLX startup).
- Verification: prefill drop, latency drop, cache hit metrics → Task 13 steps.

**Placeholder scan:** Each step has explicit code or commands. No "TBD" / "implement later" / "similar to Task N".

**Type consistency:** `Tenant` dataclass used in tenants.py + gateway.py. `QueueItem.payload` is a tuple `(body, tenant_name, future)` consistently. `MLXBackend.chat_completion` signature stable across files.

**Risk note:** Task 11 (handler_swarm port flip) is the moment trading starts going through the gateway. If gateway is down or buggy, ALL swarm agents fail. Mitigation: Task 14 rollback is one-line. Task 13 verifies BEFORE walking away.

---

## Execution Handoff

**Plan complete and saved to `Forex Trading Team/docs/superpowers/plans/2026-04-27-shared-35b-serving-stack-plan.md`.**

**Two execution options:**

**1. Subagent-Driven** — fresh subagent per task, two-stage review between tasks. Best when tasks are clearly self-contained.

**2. Inline Execution (recommended given Tim's pace today)** — execute tasks in this session with checkpoints between for review. Fewer subagent dispatches, faster iteration loop, you can course-correct mid-task. Tim has been moving fast and prefers iterative checkpoints over big-design-upfront.

**Which approach?**
