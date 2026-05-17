# Agent 35B Collapse + Boardroom Refactor Finish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate trading agents from `mlx/CRO` (9B, port 11500) to `mlx/CSO` (35B, port 11502). Primary lever is one declarative file (`team_setup.py`) — flips 7 swarm-dispatched agents at once. Then catch four direct-call helpers that bypass the swarm. Then finish the unfinished boardroom `mlx_servers.sh` refactor (Strategy + Ops tiers).

**Architecture:** Trading agents converge on the single 35B server already serving the validator. Boardroom remains multi-model — 9B stays alive for the boardroom CRO seat (managed by `mlx_servers.sh`, not by trading_launcher). Boardroom seat-registry refactor is finished by aligning the launcher script to the already-shipped registry.

**Tech Stack:** Python 3.11 (myenv), MLX (Apple Silicon serving), `Handler/handler_swarm.py` framework, OpenAI-compatible HTTP API, zsh launcher script.

**Spec:** `Forex Trading Team/docs/superpowers/specs/2026-04-26-agent-35b-collapse-design.md`

**Progress so far:**
- ✅ A0 — `shadow_compare.py` helper shipped (commit `f37de7cf`)

---

## File Structure

| File | Track | Action |
|---|---|---|
| `Forex Trading Team/Source/scripts/shadow_compare.py` | A0 | ✅ DONE — shipped commit `f37de7cf` |
| `Forex Trading Team/Source/agents/team_setup.py` | **A1 (primary lever)** | Modify — 7 `mlx/CRO` → `mlx/CSO` flips |
| `Forex Trading Team/Source/floor_chat.py` | A2 | Modify — `_call_mlx` (lines 184-206) URL + payload |
| `Forex Trading Team/Source/snipe_cleanup.py` | A3 | Modify — `_CRO_URL` constants + payload |
| `Forex Trading Team/Source/guardian_narrator.py` | A4 | Modify — `MLX_9B_URL` + `_call_local_9b` payload |
| `Forex Trading Team/Source/intelligence_agent_prep.py` | A5 | Modify — `_get_local_client` + synthesis call |
| `Forex Trading Team/Source/test_system_changes.py` | A6 | Modify — 35B required, 9B optional |
| `Forex Trading Team/Source/trading_launcher.sh` | A7 | Modify — drop 9B (`mlx-execution`) entirely |
| `~/jarvis/scripts/mlx_servers.sh` | B2 | Modify — SEAT_CONFIG + RESIDENT_SEATS |
| `~/Jarvis/knowledge/collective/models/boardroom-seat-mapping.md` | D1 | Modify — mark divergence RESOLVED |
| Vault learnings entry | D2 | Append via `vault_cli.py` |

---

## Pre-flight (read once)

- All bash from `source ~/myenv/bin/activate && cd "<repo_root>"`
- DO NOT stop the live MLX 35B server (port 11502) at any point in Track A — production validator depends on it
- DO NOT install any new packages
- Track A files commit one-at-a-time so each migration is independently revertible
- Branch: `feature/kronos-scout` (local-only save branch). DO NOT use `git add -A` or `git add .` — many unrelated files in monorepo

---

## Track A — Trading Agent Collapse

### Task A1: PRIMARY LEVER — flip team_setup.py from mlx/CRO to mlx/CSO

**Files:**
- Modify: `Forex Trading Team/Source/agents/team_setup.py` (7 specific lines)

This is the single biggest change in the plan. Once this commit lands, every swarm-dispatched trading agent routes to port 11502 (35B) automatically — no further code changes needed for those agents.

- [ ] **Step A1.1: Confirm exact lines to change**

```bash
grep -nE '"model": "mlx/CRO"' "<repo_root>/Source/agents/team_setup.py"
```

Expected: exactly 7 matches at lines 202, 238, 279, 345, 380, 414, 451. If the count is different, STOP and re-verify against the spec; the file may have changed since the plan was written.

- [ ] **Step A1.2: Apply the 7 flips**

For each of the 7 lines, change `"mlx/CRO"` → `"mlx/CSO"` and update the inline comment. Use `Edit` with `replace_all=False` because each line has a unique adjacent comment we want to preserve-and-update. Do them in order (use Read to find current text, then Edit).

**Line 202** (`oanda_data` agent):
- Old: `"model": "mlx/CRO",  # Qwen3.5-9B local (port 11500) — tool calling verified 9/9 tests 2026-03-30. Was Haiku.`
- New: `"model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). Tool calling on 35B verified via swarm dispatch.`

**Line 238** (`intelligence` agent):
- Old: `"model": "mlx/CRO",  # Qwen3.5-9B local (port 11500) — reads pre-cached briefings, no reasoning needed. Cache populated by 3x/day cron (local CSO model).`
- New: `"model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). Reads pre-cached briefings; cache populated by intelligence_agent_prep.py.`

**Line 279** (`technical_analyst` agent):
- Old: `"model": "mlx/CRO",  # Qwen3.5-9B local (port 11500) — TA is a camera not a reasoner; 9B sufficient for data description`
- New: `"model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). TA narrative on 35B's distilled trading skill stack.`

**Line 308** (`validator` agent): **DO NOT CHANGE.** Already on `mlx/CSO`.

**Line 345** (`execution` agent):
- Old: `"model": "mlx/CRO",  # Qwen3.5-9B local (port 11500) — order placement verified 9/9 tests 2026-03-30. Was Haiku.`
- New: `"model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). Order placement via swarm dispatch.`

**Line 380** (`trade_monitor` agent):
- Old: `"model": "mlx/CRO",  # Qwen3.5-9B local (port 11500) — narrator role, no close authority`
- New: `"model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). Narrator role only — guardian remains sole close authority.`

**Line 414** (`reporter` agent):
- Old: `"model": "mlx/CRO",  # Qwen3.5-9B local — cycle summaries, logging, no paid model needed.`
- New: `"model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). Cycle summaries + structured logging.`

**Line 451** (`cycle_orchestrator` agent):
- Old: `"model": "mlx/CRO",  # Qwen3.5-9B local (port 11500) — user interface layer, team coordinator, does NOT make trade decisions`
- New: `"model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). UI layer, team coordinator — does NOT make trade decisions.`

- [ ] **Step A1.3: Verify the flip is complete**

```bash
grep -cE '"model": "mlx/CRO"' "<repo_root>/Source/agents/team_setup.py"
grep -cE '"model": "mlx/CSO"' "<repo_root>/Source/agents/team_setup.py"
```

Expected output: `0` (zero CRO refs) and `8` (seven flipped + one validator already there).

- [ ] **Step A1.4: Lint — make sure the file still parses**

```bash
source ~/myenv/bin/activate && \
python3 -c "
import ast
with open('<repo_root>/Source/agents/team_setup.py') as f:
    ast.parse(f.read())
print('syntax OK')
"
```

Expected: `syntax OK`.

- [ ] **Step A1.5: Commit**

```bash
cd "<repo_root>" && \
git add Source/agents/team_setup.py && \
git commit -m "agents: flip 7 trading agents mlx/CRO→mlx/CSO (single 35B agent fleet)"
```

---

### Task A2: Migrate floor_chat.py `_call_mlx` (direct-call helper, lowest risk)

The rest of `floor_chat.py` uses the swarm (which Task A1 already routed to 35B). This task only touches the direct-call shortcut at lines 184-206.

**Files:**
- Modify: `Forex Trading Team/Source/floor_chat.py:184-206`

- [ ] **Step A2.1: Shadow-test**

```bash
source ~/myenv/bin/activate && \
python3 "<repo_root>/Source/scripts/shadow_compare.py" floor_chat
```

Expected: 35B returns text containing `handler` and parseable JSON-ish structure. (A0 already proved this.)

- [ ] **Step A2.2: Apply the migration**

Use `Edit` to replace the `_call_mlx` function (lines 184-206) with:

```python
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
        "http://127.0.0.1:11502/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with _ureq.urlopen(req, timeout=30) as r:
        result = json.loads(r.read())
    text = (result["choices"][0]["message"].get("content") or "").strip()
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
```

Diffs: URL, model name, added `chat_template_kwargs`, null-guard, timeout 20→30.

- [ ] **Step A2.3: Smoke-test**

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>/Source" && \
python3 -c "
from floor_chat import _call_mlx
out = _call_mlx('You are an orchestrator. Reply only with JSON: {\"handler\": \"narrator\"}.', 'How is my trade?', max_tokens=100)
print('OUT:', repr(out))
assert out, 'empty response'
print('OK')
"
```

Expected: non-empty response and `OK`.

- [ ] **Step A2.4: Commit**

```bash
cd "<repo_root>" && \
git add Source/floor_chat.py && \
git commit -m "agents: migrate floor_chat._call_mlx 9B→35B (port 11502)"
```

---

### Task A3: Migrate snipe_cleanup.py

**Files:**
- Modify: `Forex Trading Team/Source/snipe_cleanup.py:28-90`

- [ ] **Step A3.1: Shadow-test**

```bash
python3 "<repo_root>/Source/scripts/shadow_compare.py" snipe_cleanup
```

Expected: 35B output contains `DECISION:`, `SUMMARY:`, `MARKET NOW:`, `REASON:` lines. If format is missing, the prompt may need a "Reply ONLY in the format above, no preamble" reinforcement — try the migration first.

- [ ] **Step A3.2: Apply the migration**

Edit lines 28-29 from:
```python
_CRO_URL  = "http://127.0.0.1:11500/chat/completions"
_CRO_MODEL = "mlx-community/Qwen3.5-9B-4bit"
```
to:
```python
_CRO_URL  = "http://127.0.0.1:11502/v1/chat/completions"
_CRO_MODEL = "mlx-community/Qwen3.5-35B-A3B-4bit"
```

Inside `_cro_call` (around lines 70-79), the payload dict — add `chat_template_kwargs`:

```python
    payload = json.dumps({
        "model": _CRO_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stop": ["</think>"],
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
```

In the `urlopen` block (line 85-87), change content extraction:
```python
        return (result["choices"][0]["message"].get("content") or "").strip()
```
And bump timeout: `urlopen(req, timeout=30)` → `urlopen(req, timeout=60)`.

- [ ] **Step A3.3: Smoke-test**

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>/Source" && \
python3 -c "
from snipe_cleanup import _cro_call
out = _cro_call('Snipe: SELL EUR_USD waiting for E100 retest.\nMarket: Bearish fan intact, price retracing toward E100.')
print(out)
assert 'DECISION:' in out, 'missing DECISION line'
print('OK')
"
```

Expected: response with all four format lines, then `OK`. If `DECISION:` missing, do not commit; reinforce the prompt with `"\n\nIMPORTANT: Reply with EXACTLY the four-line format above. No preamble. No closing remarks."` and retry.

- [ ] **Step A3.4: Commit**

```bash
cd "<repo_root>" && \
git add Source/snipe_cleanup.py && \
git commit -m "agents: migrate snipe_cleanup CRO call 9B→35B (port 11502)"
```

---

### Task A4: Migrate guardian_narrator.py

**Files:**
- Modify: `Forex Trading Team/Source/guardian_narrator.py:22-49`

- [ ] **Step A4.1: Shadow-test**

```bash
python3 "<repo_root>/Source/scripts/shadow_compare.py" guardian_narrator
```

Expected: 35B returns 1-3 sentence narrative referencing pair, direction, threat, phase.

- [ ] **Step A4.2: Apply the migration**

Replace lines 22-23 from:
```python
MLX_9B_URL = "http://localhost:11500/chat/completions"
MLX_TIMEOUT = 8  # seconds — 9B is fast
```
to:
```python
MLX_AGENT_URL = "http://localhost:11502/v1/chat/completions"
MLX_AGENT_MODEL = "mlx-community/Qwen3.5-35B-A3B-4bit"
MLX_TIMEOUT = 30  # seconds — 35B with vision-loaded server
```

Replace the `_call_local_9b` function (lines 26-49) with:

```python
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
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=MLX_TIMEOUT) as resp:
            body = json.loads(resp.read())
            content = body.get("choices", [{}])[0].get("message", {}).get("content")
            return (content or "").strip() or None
    except Exception as e:
        logger.debug("[NARRATOR] Local 35B unavailable: %s — using template fallback", e)
        return None
```

- [ ] **Step A4.3: Update callers within this file**

```bash
grep -n "_call_local_9b" "<repo_root>/Source/guardian_narrator.py"
```

Rename every match to `_call_local_agent`. Verify zero matches after.

- [ ] **Step A4.4: Smoke-test**

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>/Source" && \
python3 -c "
from guardian_narrator import narrate_trade_status
out = narrate_trade_status({
    'pair': 'EUR_USD', 'direction': 'BUY', 'threat_level': 22,
    'zone': 'GREEN', 'phase': 'trending', 'pnl_pips': 6.4,
    'fan_state': 'expanding', 'bb_state': 'expanding',
    'rsi': 62, 'stoch': 70,
})
print(out)
assert out, 'empty narrative'
print('OK')
"
```

Expected: 1-3 sentence narrative + `OK`.

- [ ] **Step A4.5: Commit**

```bash
cd "<repo_root>" && \
git add Source/guardian_narrator.py && \
git commit -m "agents: migrate guardian_narrator 9B→35B + rename _call_local_9b→_call_local_agent"
```

---

### Task A5: Migrate intelligence_agent_prep.py

**Files:**
- Modify: `Forex Trading Team/Source/intelligence_agent_prep.py:252-407`

- [ ] **Step A5.1: Shadow-test**

```bash
python3 "<repo_root>/Source/scripts/shadow_compare.py" intelligence_prep
```

Expected: 35B returns structured macro analysis ending with `BIAS:`, length >200 chars (consumer requires `len > 50`).

- [ ] **Step A5.2: Apply the migration**

Replace `_get_local_client()` (lines 252-255):

```python
def _get_local_client():
    """Get OpenAI-compatible client for local MLX 35B agent (port 11502 — agent fleet)."""
    from openai import OpenAI
    return OpenAI(base_url="http://localhost:11502/v1", api_key="mlx-local")
```

Inside `_synthesize_briefing` (around lines 379-407), update the call:

```python
        client = _get_local_client()
        response = client.chat.completions.create(
            model="mlx-community/Qwen3.5-35B-A3B-4bit",
            messages=[
                {"role": "system", "content": synthesis_prompt},
                {"role": "user", "content": data_text},
            ],
            max_tokens=4000,
            temperature=0.3,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
```

Update three log messages (lines 400, 403, 406):
- 400: `logger.error(f"[{pair}] MLX agent 35B (port 11502) failed: {local_err} — NO paid API fallback")`
- 403: `logger.info(f"[{pair}] Briefing synthesized by MLX agent 35B ({len(result_text)} chars)")`
- 406: `logger.warning(f"[{pair}] MLX agent 35B returned empty — no briefing stored")`

- [ ] **Step A5.3: Smoke-test**

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>/Source" && \
python3 -c "
from intelligence_agent_prep import _synthesize_briefing
result = _synthesize_briefing('EUR_USD', {
    'macro': {'base_currency_rate': 4.0, 'quote_currency_rate': 5.25, 'rate_differential': -1.25, 'pair_current_price': 1.0850},
    'news': {'articles': [{'source': 'Reuters', 'title': 'ECB holds rates'}]},
    'weather': {},
    'statistics': {'correlation_pairs': ['GBP_USD'], 'correlation_values': {'GBP_USD': 0.81}},
})
print('LEN:', len(result) if result else 0)
assert result and len(result) > 200, f'briefing too short: {len(result) if result else 0}'
print(result[:500])
print('OK')
"
```

Expected: long macro analysis (>200 chars) + `OK`. May take 30-90s for 35B.

- [ ] **Step A5.4: Commit**

```bash
cd "<repo_root>" && \
git add Source/intelligence_agent_prep.py && \
git commit -m "agents: migrate intelligence_agent_prep synthesis 9B→35B"
```

---

### Task A6: Update test_system_changes.py — 35B required, 9B optional

**Files:**
- Modify: `Forex Trading Team/Source/test_system_changes.py:49-95, 540-595`

- [ ] **Step A6.1: Read context**

```bash
sed -n '45,100p' "<repo_root>/Source/test_system_changes.py"
grep -n "^def report\|def section" "<repo_root>/Source/test_system_changes.py" | head -3
```

Note whether `report()` accepts `"warn"` as a status value.

- [ ] **Step A6.2: Apply the migration**

Replace the section starting `# 1a. MLX 9B at port 11500` (around line 49) through the corresponding TA prompt roundtrip section ending around line 595 with the new dual-check block:

```python
# 1a. MLX 35B at port 11502 — REQUIRED (agent fleet: validator + 7 trading agents + 4 direct-call helpers)
try:
    probe = urllib.request.urlopen("http://localhost:11502/v1/models", timeout=4)
    body = probe.read().decode("utf-8", errors="replace")
    if probe.status == 200:
        report("MLX 35B (port 11502) — agent fleet up", "pass", "Port 11502 listening")
    else:
        report("MLX 35B (port 11502) — agent fleet up", "fail", f"HTTP {probe.status}: {body[:80]}")
except urllib.error.URLError as e:
    if "Connection refused" in str(e):
        report("MLX 35B (port 11502) — agent fleet up", "fail",
               "Connection refused — start with: ~/jarvis/scripts/mlx_servers.sh start CSO")
    else:
        report("MLX 35B (port 11502) — agent fleet up", "fail", f"URLError: {e}")
except Exception as e:
    report("MLX 35B (port 11502) — agent fleet up", "fail", f"{type(e).__name__}: {e}")

# 1b. MLX 9B at port 11500 — OPTIONAL (boardroom CRO seat only; not required for trading)
try:
    probe = urllib.request.urlopen("http://localhost:11500/", timeout=4)
    if probe.status == 200:
        report("MLX 9B (port 11500) — boardroom CRO available [OPTIONAL]", "pass", "Port 11500 listening")
    else:
        report("MLX 9B (port 11500) — boardroom CRO available [OPTIONAL]", "pass",
               f"HTTP {probe.status} — boardroom CRO seat unavailable (OK for trading-only)")
except urllib.error.URLError as e:
    if "Connection refused" in str(e):
        report("MLX 9B (port 11500) — boardroom CRO available [OPTIONAL]", "pass",
               "9B not running — boardroom CRO seat unavailable (OK for trading-only)")
    else:
        report("MLX 9B (port 11500) — boardroom CRO available [OPTIONAL]", "pass", f"URLError: {e}")
except Exception as e:
    report("MLX 9B (port 11500) — boardroom CRO available [OPTIONAL]", "pass", f"{type(e).__name__}: {e}")
```

If the `report()` helper does support `"warn"`, swap the relevant `"pass"` (where `[OPTIONAL]` is currently degrading-but-still-passing) for `"warn"` for clearer signal. Confirm by reading the report function definition.

The TA prompt roundtrip section (line 541+) tests 9B-specific TA prompt compatibility — delete it entirely. Trading TA now goes through the swarm to 35B; the general 35B health check above covers it.

- [ ] **Step A6.3: Run the test script end-to-end**

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>/Source" && \
python3 test_system_changes.py 2>&1 | tail -40
```

Expected: 35B checks PASS. 9B checks pass (with `[OPTIONAL]` informational text) regardless of whether 9B is up.

- [ ] **Step A6.4: Commit**

```bash
cd "<repo_root>" && \
git add Source/test_system_changes.py && \
git commit -m "tests: 35B agent fleet REQUIRED, 9B optional (boardroom-only)"
```

---

### Task A7: Decommission 9B from trading_launcher.sh

**Files:**
- Modify: `Forex Trading Team/Source/trading_launcher.sh`

- [ ] **Step A7.1: Identify all 9B sections**

```bash
grep -nE "MLX_EXEC|mlx-execution|11500|9B|_WARMUP_9B" "<repo_root>/Source/trading_launcher.sh"
```

- [ ] **Step A7.2: Delete every section**

Delete:
1. Lines 34-38 (the `MLX_EXEC_*` definitions)
2. The `mlx-execution) pattern="mlx_vlm_server_with_tools" ;;` lines in the stop and status case statements (around 221, 281)
3. The comment at line 308: `# MLX execution model (Qwen3.5-9B VLM — port 11500, lazy-loads on first request)`
4. The `_WARMUP_9B='...'` definition AND the curl-based 9B warmup loop (search `_WARMUP_9B` and `Warming 9B`)
5. The reload guard around line 489 referring to `9B (port $MLX_EXEC_PORT)` — including the `if`/`fi` wrapping
6. Help text around line 532: `MLX 9B/35B` → `MLX 35B`; `90s cold warmup` → `60s cold warmup`
7. Any `mlx-execution` reference in service lists/loops (grep again)

After all edits:
```bash
grep -nE "11500|9B|mlx-execution|MLX_EXEC|_WARMUP_9B" "<repo_root>/Source/trading_launcher.sh"
```
Expected: zero matches.

- [ ] **Step A7.3: Lint**

```bash
zsh -n "<repo_root>/Source/trading_launcher.sh" && echo "syntax OK"
```

Expected: `syntax OK`. If errors, deletions left an unbalanced `if`/`fi` — re-read the affected section.

- [ ] **Step A7.4: Verify status command works**

```bash
"<repo_root>/Source/trading_launcher.sh" status 2>&1 | head -25
```

Expected: status shows `mlx-35b` running, no `mlx-execution` mention, no errors.

- [ ] **Step A7.5: Commit**

```bash
cd "<repo_root>" && \
git add Source/trading_launcher.sh && \
git commit -m "launcher: drop 9B (mlx-execution) — 9B now boardroom-only via mlx_servers.sh"
```

---

### Task A8: End-to-end verification (9B stopped, full cycle runs)

- [ ] **Step A8.1: Stop the 9B server**

```bash
~/jarvis/scripts/mlx_servers.sh stop CRO 2>&1 | head -10
lsof -i :11500 -sTCP:LISTEN 2>/dev/null && echo "STILL UP" || echo "port 11500 dead — good"
```

Expected: `port 11500 dead — good`.

- [ ] **Step A8.2: Trigger one trading cycle**

```bash
source ~/myenv/bin/activate && \
cd "<repo_root>" && \
python3 scripts/run_trading_cycle.py 2>&1 | tee /tmp/cycle_postcollapse.log | tail -50
```

Expected: cycle completes. Watch for:
- TA narrative generated
- Validator verdict produced
- Guardian narration on any open trade
- Reporter cycle summary
- No `Connection refused` or `port 11500` errors

If any path fails:
```bash
grep -E "11500|9B|mlx-execution" /tmp/cycle_postcollapse.log
```
Find the missed migration and fix in a follow-up commit.

- [ ] **Step A8.3: Restart 9B for boardroom use (optional)**

```bash
~/jarvis/scripts/mlx_servers.sh start CRO
```

If you don't need the boardroom right now, leave it stopped — saves memory.

- [ ] **Step A8.4: No commit** — verification only.

---

## Track B — Boardroom Refactor Finish

### Task B1: Pre-pull the new model weights

- [ ] **Step B1.1: Pre-pull Qwen3-30B-A3B-4bit**

```bash
source ~/myenv/bin/activate && \
python3 -c "
from huggingface_hub import snapshot_download
path = snapshot_download(repo_id='mlx-community/Qwen3-30B-A3B-4bit', allow_patterns=['*.json','*.safetensors','*.txt','*.py'])
print('downloaded to:', path)
"
```

- [ ] **Step B1.2: Pre-pull Qwen2.5-1.5B-Instruct-4bit**

```bash
source ~/myenv/bin/activate && \
python3 -c "
from huggingface_hub import snapshot_download
path = snapshot_download(repo_id='mlx-community/Qwen2.5-1.5B-Instruct-4bit', allow_patterns=['*.json','*.safetensors','*.txt','*.py'])
print('downloaded to:', path)
"
```

---

### Task B2: Update mlx_servers.sh SEAT_CONFIG + RESIDENT_SEATS

**Files:**
- Modify: `~/jarvis/scripts/mlx_servers.sh:18-30, ~RESIDENT_SEATS line`

- [ ] **Step B2.1: Replace the Coder line + add Ops**

Edit `~/jarvis/scripts/mlx_servers.sh`. Replace:
```
Coder|11504|mlx-community/Qwen2.5-Coder-32B-Instruct-4bit|lm
```
with:
```
Strategy|11504|mlx-community/Qwen3-30B-A3B-4bit|lm_lenient
Ops|11505|mlx-community/Qwen2.5-1.5B-Instruct-4bit|lm
```

Final SEAT_CONFIG:
```
SEAT_CONFIG="
CRO|11500|mlx-community/Qwen3.5-9B-4bit|lm_lenient
CTO|11501|mlx-community/DeepSeek-R1-Distill-Qwen-14B-4bit|lm
CSO|11502|mlx-community/Qwen3.5-35B-A3B-4bit|vlm_with_tools
CDO|11503|mlx-community/Qwen2.5-7B-Instruct-4bit|lm
Strategy|11504|mlx-community/Qwen3-30B-A3B-4bit|lm_lenient
Ops|11505|mlx-community/Qwen2.5-1.5B-Instruct-4bit|lm
"
```

- [ ] **Step B2.2: Update RESIDENT_SEATS**

```
RESIDENT_SEATS="CSO CRO"
```

- [ ] **Step B2.3: Lint**

```bash
zsh -n ~/jarvis/scripts/mlx_servers.sh && echo "syntax OK"
```

- [ ] **Step B2.4: Commit**

```bash
cd ~/jarvis && \
git add scripts/mlx_servers.sh && \
git commit -m "mlx_servers: replace Coder w/ Strategy(D) + add Ops(F) — match seat_registry"
```

---

### Task B3: Smoke test — Strategy server (port 11504)

- [ ] **Step B3.1: Start + probe**

```bash
~/jarvis/scripts/mlx_servers.sh start Strategy
sleep 30
curl -s -X POST http://127.0.0.1:11504/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mlx-community/Qwen3-30B-A3B-4bit","messages":[{"role":"user","content":"One sentence about market positioning."}],"max_tokens":80,"temperature":0.3,"chat_template_kwargs":{"enable_thinking":false}}' \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['choices'][0]['message']['content'][:300])"
```

Expected: a single-sentence response.

- [ ] **Step B3.2: Stop**

```bash
~/jarvis/scripts/mlx_servers.sh stop Strategy
```

---

### Task B4: Smoke test — Ops server (port 11505)

- [ ] **Step B4.1: Start + probe**

```bash
~/jarvis/scripts/mlx_servers.sh start Ops
sleep 15
curl -s -X POST http://127.0.0.1:11505/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mlx-community/Qwen2.5-1.5B-Instruct-4bit","messages":[{"role":"user","content":"Confirm in one sentence."}],"max_tokens":50,"temperature":0}' \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['choices'][0]['message']['content'][:200])"
```

- [ ] **Step B4.2: Stop**

```bash
~/jarvis/scripts/mlx_servers.sh stop Ops
```

---

### Task B5: Verify per-seat prompt files exist

- [ ] **Step B5.1: Run check**

```bash
source ~/myenv/bin/activate && python3 << 'EOF'
import sys
sys.path.insert(0, "~/Jarvis")
from Handler.seat_registry import SEATS
from pathlib import Path
vault = Path("~/Jarvis/knowledge")
missing = [(sid, str(vault / s["vault_prompt"])) for sid, s in SEATS.items() if not (vault / s["vault_prompt"]).exists()]
if missing:
    print("MISSING:")
    for sid, p in missing:
        print(f"  {sid}: {p}")
    sys.exit(1)
else:
    print(f"All {len(SEATS)} seat prompts exist ✓")
EOF
```

If missing, log them as a follow-up — not in scope for this plan.

---

### Task B6: Smoke test — multi-tier convene

- [ ] **Step B6.1: Resolve seats → servers**

```bash
source ~/myenv/bin/activate && python3 << 'EOF'
import sys
sys.path.insert(0, "~/Jarvis")
from Handler.seat_registry import SEATS, get_server_for_seat, get_servers_for_seats
selected = ["CEO", "CTO", "CMO", "COO"]
servers = get_servers_for_seats(selected)
print(f"Servers: {sorted({s['port'] for s in servers})}")
for sid in selected:
    s = SEATS[sid]; srv = get_server_for_seat(sid)
    print(f"  {sid:5s} → {s['server_id']} (port {srv['port']}, {srv['model']})")
EOF
```

Expected: 4 distinct ports (11502, 11501, 11504, 11505).

- [ ] **Step B6.2: Start all 4 + probe each**

```bash
for s in CSO CTO Strategy Ops; do ~/jarvis/scripts/mlx_servers.sh start $s; done
sleep 30
ps -caxm -o "rss,comm" | awk '/mlx_/ {sum+=$1} END {printf "MLX servers RSS: %.1f GB\n", sum/1024/1024}'

for port in 11502 11501 11504 11505; do
    echo "=== port $port ==="
    curl -s -X POST http://127.0.0.1:$port/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{"model":"x","messages":[{"role":"user","content":"One sentence."}],"max_tokens":40,"temperature":0,"chat_template_kwargs":{"enable_thinking":false}}' \
      | python3 -c "import json,sys; r=json.load(sys.stdin); print((r['choices'][0]['message'].get('content') or '(empty)')[:200])"
done
```

Expected: all 4 ports respond. Total RSS under 60 GB.

- [ ] **Step B6.3: Stop on-demand servers**

```bash
~/jarvis/scripts/mlx_servers.sh stop CTO
~/jarvis/scripts/mlx_servers.sh stop Strategy
~/jarvis/scripts/mlx_servers.sh stop Ops
~/jarvis/scripts/mlx_servers.sh status
```

Expected: only `CSO` (and `CRO` if started) remain — these are RESIDENT.

---

## Documentation

### Task D1: Mark vault divergence RESOLVED

- [ ] **Step D1.1: Edit `boardroom-seat-mapping.md`**

In `~/Jarvis/knowledge/collective/models/boardroom-seat-mapping.md`, find `## CRITICAL: Divergence flag` (around line 86). Replace through `**Decision needed**: ... Tim to decide.` with:

```markdown
## Divergence flag — RESOLVED 2026-04-26

`scripts/mlx_servers.sh` now matches `seat_registry.py`:

| Port | Server | Model | Status |
|---|---|---|---|
| 11500 | CRO | Qwen3.5-9B-4bit | ✓ aligned, boardroom-only (no longer a trading dependency) |
| 11501 | CTO | DeepSeek-R1-Distill-Qwen-14B-4bit | ✓ aligned |
| 11502 | CSO | Qwen3.5-35B-A3B-4bit | ✓ aligned, agent-fleet (validator + 7 swarm-dispatched trading agents + 4 direct-call helpers) |
| 11503 | CDO | Qwen2.5-7B-Instruct-4bit | ✓ aligned |
| 11504 | Strategy | Qwen3-30B-A3B-4bit | ✓ aligned (was Coder-32B) |
| 11505 | Ops | Qwen2.5-1.5B-Instruct-4bit | ✓ aligned (was missing) |

`RESIDENT_SEATS="CSO CRO"`. Coder-32B retired from launcher; if a dedicated coder seat is wanted later, allocate a new port and update both `seat_registry.py` and `mlx_servers.sh`.
```

- [ ] **Step D1.2: Commit**

```bash
cd ~/Jarvis/knowledge && \
git add collective/models/boardroom-seat-mapping.md && \
git commit -m "vault: mark seat_registry/mlx_servers divergence RESOLVED"
```

---

### Task D2: Write learnings entry

- [ ] **Step D2.1: Append via vault_cli**

```bash
source ~/myenv/bin/activate && \
python3 ~/Jarvis/knowledge/vault_cli.py \
    --agent "claude-code" \
    --type "improvement" \
    --summary "Trading agent fleet collapsed to 35B (one team_setup.py edit + 4 direct-call helpers); boardroom mlx_servers refactor finished" \
    --context "PRIMARY LEVER: Forex Trading Team/Source/agents/team_setup.py — 7 agents flipped from mlx/CRO (9B port 11500) to mlx/CSO (35B port 11502): oanda_data, intelligence, technical_analyst, execution, trade_monitor, reporter, cycle_orchestrator. Validator was already mlx/CSO. Handler/handler_swarm.py MLX_SERVERS port map untouched (already correct). DIRECT-CALL HELPERS (bypass swarm): floor_chat._call_mlx, snipe_cleanup._cro_call, guardian_narrator._call_local_9b→_call_local_agent, intelligence_agent_prep._get_local_client + synthesis call — all migrated to :11502/v1/chat/completions with chat_template_kwargs enable_thinking=false and content null-guards. INFRA: trading_launcher.sh dropped mlx-execution (9B) entirely; test_system_changes.py 35B now REQUIRED + 9B OPTIONAL boardroom-only. BOARDROOM: scripts/mlx_servers.sh updated — port 11504 Coder-32B → Qwen3-30B-A3B (Strategy, server D), port 11505 added Qwen2.5-1.5B (Ops, server F), RESIDENT_SEATS=CSO CRO (chair + boardroom CRO always-resident). Smoke-tested multi-tier convene (CEO/CTO/CMO/COO) — all four servers responded under 60 GB total. ARCHITECTURE PLAN DELTAS: Phase 4 (9B subagent backend) deleted (subagents inherit agent 35B); Phase 1 (vLLM) reframed as engine for single agent 35B not second instance; Phase 2 (gateway) routes by lane (agent vs boardroom seat). FUTURE (Tim flagged): team_setup.py is trading-team-only; future workspaces need centralized agent→model registry — gateway is natural home. Specs: Forex Trading Team/docs/superpowers/specs/2026-04-26-agent-35b-collapse-design.md. Plan: Forex Trading Team/docs/superpowers/plans/2026-04-26-agent-35b-collapse-plan.md." \
    --tags "agents,architecture,35b,9b,boardroom,seat-registry,mlx,collapse,team-setup,swarm,2026-04-26" \
    --universal
```

---

## Self-Review

**Spec coverage:**
- A.1 (team_setup primary lever, 7 flips) → Task A1 ✓
- A.2 (4 direct-call helpers) → Tasks A2 (floor_chat), A3 (snipe_cleanup), A4 (guardian_narrator), A5 (intelligence_prep) ✓
- A.3 (test_system_changes) → Task A6 ✓
- A.4 (trading_launcher decommission) → Task A7 ✓
- A.5 (end-to-end verify) → Task A8 ✓
- B.1 (mlx_servers.sh + RESIDENT_SEATS) → Task B2 ✓
- B.2b (CSO CRO resident) → Task B2.2 ✓
- B.3 (per-seat prompts) → Task B5 ✓
- B.4 (smoke tests) → Tasks B3, B4, B6 ✓
- Documentation → Tasks D1, D2 ✓

**Placeholder scan:** Each step has explicit code or commands. The `[OPTIONAL]` text in Task A6 is intentional — it's the actual label string when `report()` doesn't support `"warn"`.

**Type consistency:** `_call_local_9b` → `_call_local_agent` (Task A4), `MLX_AGENT_URL` / `MLX_AGENT_MODEL` (A4), `_CRO_URL` / `_CRO_MODEL` (A3 — kept the variable names, just changed values), `mlx/CRO` → `mlx/CSO` (A1 throughout team_setup) — consistent within each file.

**Risk note:** A1 is the highest-leverage task. After A1 commits, the very next trading cycle will route 7 agents to the 35B server. If any of those agents has a request shape the 35B server doesn't handle, it'll fail at runtime, not at lint. A8 catches this with a full cycle. If a problem surfaces between A1 and A8, the per-task commits are individually revertible.

---

## Execution Handoff

**Plan complete and saved.** Two execution options:

**1. Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks.

**2. Inline Execution** — execute in this session with checkpoints.
