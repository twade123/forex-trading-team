# Agent 35B Collapse + Boardroom Refactor Finish (Design)

**Date:** 2026-04-26 (revised same day after Tim flagged the team_setup.py lever)
**Author:** claude-code
**Status:** approved → revised → re-approval needed
**Supersedes:** Phase 4 of the 2026-04-26 architecture plan ("9B subagent backend integration") — deleted.

## Purpose

Two adjacent gaps in the local-LLM topology, addressed in one coordinated change:

1. **Agent collapse:** every trading agent uses 35B. The 9B leaves the trading hot path entirely. Memory and ops simplify; distillation has one target.
2. **Boardroom refactor finish:** the previous Claude Code instance shipped `Handler/seat_registry.py` (17 seats × 6 model servers A-F) but did not update `scripts/mlx_servers.sh`. Today only ~5 of 17 seats can convene. Strategy and ops tiers are dark.

The two are coupled: they both touch model-server topology, and neither makes sense without the other. Doing them together avoids two restart windows.

## Architectural rule (the framing)

**All agents run on the 35B. Boardroom seats keep their distinct base models for perspective diversity.**

| Lane | Model | Why |
|---|---|---|
| Agents (validator + 7 swarm-dispatched trading agents + 4 direct-call helpers) | One shared 35B (`A` server, port 11502, alias `mlx/CSO`) | Distilled on the trading skill stack; A3B speed ≈ 9B compute; one cache, one distill target |
| Boardroom seats (17) | Diverse: DeepSeek-R1-14B, Qwen3.5-9B, Qwen3-30B-A3B, Qwen2.5-7B, Qwen2.5-1.5B | Variety of reasoning styles is the *point* — different "model personalities" at the table |

The 9B (`C` server, port 11500) **is not deleted** — it stays alive as the boardroom CRO seat (managed by `mlx_servers.sh`, not by trading). It just stops being a trading dependency.

## How the trading team is wired (key insight, originally missed)

Trading agents are defined declaratively in `Forex Trading Team/Source/agents/team_setup.py` as `AGENT_SPECS`. Each agent has a `"model"` field. `Handler/handler_swarm.py` (lines ~1044-1048) maps seat names to MLX ports:

```python
MLX_SERVERS = {
    "CRO":   {"port": 11500, "hf_repo": "mlx-community/Qwen3.5-9B-4bit"},
    "CTO":   {"port": 11501, "hf_repo": "mlx-community/DeepSeek-R1-Distill-Qwen-14B-4bit"},
    "CSO":   {"port": 11502, "hf_repo": "mlx-community/Qwen3.5-35B-A3B-4bit"},
    "CDO":   {"port": 11503, "hf_repo": "mlx-community/Qwen2.5-7B-Instruct-4bit"},
    "Coder": {"port": 11504, "hf_repo": "mlx-community/Qwen2.5-Coder-32B-Instruct-4bit"},
}
```

So **the primary lever for the agent collapse is one file edit** in `team_setup.py`: every `"model": "mlx/CRO"` becomes `"model": "mlx/CSO"`. The swarm reroutes to port 11502 automatically.

A small number of helpers BYPASS the swarm (they call MLX directly via urllib), and they need their own URL flips. Those are the secondary work.

## Trading team agents (from team_setup.py AGENT_SPECS)

| Line | Agent | Current model | After |
|---|---|---|---|
| 202 | `oanda_data` | `mlx/CRO` | **`mlx/CSO`** |
| 238 | `intelligence` | `mlx/CRO` | **`mlx/CSO`** |
| 279 | `technical_analyst` | `mlx/CRO` | **`mlx/CSO`** |
| 308 | `validator` | `mlx/CSO` ✓ | unchanged (already 35B) |
| 345 | `execution` | `mlx/CRO` | **`mlx/CSO`** |
| 380 | `trade_monitor` | `mlx/CRO` | **`mlx/CSO`** |
| 414 | `reporter` | `mlx/CRO` | **`mlx/CSO`** |
| 451 | `cycle_orchestrator` | `mlx/CRO` | **`mlx/CSO`** |

Seven flips. One commit.

## Direct-call helpers that bypass the swarm

These call MLX servers via raw urllib and need explicit URL/model migration:

| File | Why direct (not via swarm) |
|---|---|
| `Forex Trading Team/Source/intelligence_agent_prep.py` | Cron-driven cache builder — runs outside the swarm cycle |
| `Forex Trading Team/Source/snipe_cleanup.py` | Utility script invoked by dashboard button + future cron |
| `Forex Trading Team/Source/guardian_narrator.py` | Called from `position_guardian.py` — separate from the swarm-managed agents |
| `Forex Trading Team/Source/floor_chat.py:184-206` (`_call_mlx`) | Fast routing-decision shortcut. The rest of the file uses the swarm; this one helper is a deliberate bypass |

Four files. Four small commits.

## Non-goals

- **Boardroom collapse.** Do not fold seats into the 35B. The multi-model design is intentional. Persona consolidation is a separate research thread (Track C in the broader plan), not this spec.
- **Engine swap (MLX → vLLM).** Out of scope — Phase 1 of the broader plan covers that.
- **Adapter changes.** Current `35b_mlx` adapter is the production target. No retrains.
- **Mac Studio replicas.** Phase 3.
- **Other workspaces' team configs.** This spec migrates the trading team only. See "Future workspaces" below.

## Track A — Trading Agent Collapse (revised)

### A.1 Primary lever — `team_setup.py`

Single file edit. Replace 7 occurrences of `"model": "mlx/CRO"` with `"model": "mlx/CSO"`. Comments adjacent to each line should also be updated (they say "Qwen3.5-9B local (port 11500) — ..."; replace 9B/11500 with 35B/11502 and adjust the rationale to reflect agent-fleet 35B).

**Verification:** import the spec list, count the `mlx/CSO` entries — should be 8 (7 flipped + 1 already there for validator).

### A.2 Direct-call helpers (4 files)

Same migration pattern per file:
- URL `:11500/chat/completions` → `:11502/v1/chat/completions`
- Model name `Qwen3.5-9B-4bit` → `Qwen3.5-35B-A3B-4bit`
- Add `chat_template_kwargs: {"enable_thinking": False}` to payload (mandatory for Qwen3.5 — issue #1 in `qwen3.5-35b/03-known-issues-and-fixes.md`)
- Add `.get("content") or ""` null-guard (issue #5)
- Bump timeout 1.5× (35B can be slower than 9B was)

Order (lowest blast radius first):
1. `floor_chat.py` (interactive routing only)
2. `snipe_cleanup.py` (off live trading path)
3. `guardian_narrator.py` (narrative-only, no trade-gating)
4. `intelligence_agent_prep.py` (cache builder, runs out-of-band)

Each gets a shadow-test (using the `shadow_compare.py` helper from Task A0) before the URL flip — confirm 35B output is consumable by the existing parser.

### A.3 Health checks — `test_system_changes.py`

- 35B (port 11502) becomes a REQUIRED check (it's the agent fleet)
- 9B (port 11500) becomes OPTIONAL (boardroom-only — warn if down, don't fail)

### A.4 Launcher decommission — `trading_launcher.sh`

Remove the `mlx-execution` (9B) entries entirely. The 9B server is now managed exclusively by `mlx_servers.sh` for the boardroom CRO seat.

Specific deletions: lines 34-38 (definitions), 221/281 (pattern matchers), 308 (comment), warmup block (`_WARMUP_9B`), reload guard, help text references.

### A.5 End-to-end verification

Stop the 9B server. Run one full trading cycle. Confirm every agent path works against the 35B. No `port 11500` errors.

### Track A risks + mitigations

| Risk | Mitigation |
|---|---|
| `mlx/CSO` doesn't accept all the same request shapes the 7 swarm-dispatched agents send | The validator already uses `mlx/CSO` and works in production — the swarm dispatch path is identical for any agent on `mlx/CSO`. Smoke-test one cycle after A.1; if a specific agent breaks, fix that agent's prompt/payload, don't revert the whole flip. |
| 35B response length differs enough to break a downstream parser | Per-helper shadow test in A.2 catches this. For swarm-dispatched agents, the swarm framework already handles JSON parsing variance. |
| Concurrent agent calls within a cycle serialize on one MLX 35B | Phase 1 (vLLM batching) is the structural fix. For now, one cycle is mostly serial anyway (agents fire sequentially: intelligence → TA → validator → execution). |
| 35B latency > 9B was on every agent | Likely — 35B is bigger. But A3B compute means per-token speed is similar. Measure during A.5 — if cycle latency grows >2x, this needs follow-up tuning, not a revert. |

### Track A rollback

Each commit is independently revertible. To roll back the team_setup flip: `git revert <sha>` — every swarm-dispatched agent goes back to 9B. To roll back a single direct-call helper: same approach. No DB state or migration to undo.

## Track B — Finish the boardroom refactor

(Unchanged from previous spec revision — `mlx_servers.sh` SEAT_CONFIG aligns to `seat_registry.py`, port 11504 → Qwen3-30B-A3B, port 11505 → Qwen2.5-1.5B added, RESIDENT_SEATS = "CSO CRO" per option B.2b, smoke tests for D and F tiers + full convene.)

## Future workspaces (Tim's flag — 2026-04-26)

`team_setup.py` is **trading-team-only**. As more workspaces come online (Open Claw, additional trading-style workspaces, agent-hosted skills), each one would currently need its own equivalent — agent specs duplicated per workspace.

This is a future architectural concern, not in scope for this spec. The model gateway (Phase 2 of the broader plan) is the natural home for centralized agent→model mapping — workspaces declare their agents to the gateway, gateway resolves the model lane. Memory entry: `project_workspace_agent_registry.md`.

## Architecture plan deltas (knock-on effects, unchanged from prior revision)

| Phase | Before | After |
|---|---|---|
| Phase 1 (vLLM) | "vLLM 35B alongside MLX" | "vLLM as engine for the single shared agent 35B." |
| Phase 2 (gateway) | Routes by tenant | Routes by lane: `agent` → port 11502 (one shared 35B); `boardroom seat` → registry-mapped server. **Future:** also serve as agent-spec registry across workspaces. |
| Phase 3 (Mac Studio) | "2nd 35B replica" | "2nd agent-lane 35B replica behind gateway." |
| Phase 4 | "9B subagent backend" | **DELETED.** |
| Phase 5 (distill) | Super-35B for everything | Distill skills into agent 35B only. Boardroom seats stay multi-model. |

## Sequencing

1. **Track A.0 (already done):** `shadow_compare.py` helper shipped (commit `f37de7cf`).
2. **Track A.1:** team_setup.py — the 7-flip primary lever.
3. **Track B.1-B.5:** mlx_servers.sh + smoke tests (parallelizable with A.2 — independent files).
4. **Track A.2:** four direct-call helpers, per-file commits.
5. **Track A.3-A.4:** test health checks + launcher decommission.
6. **Track A.5:** end-to-end verify.
7. **Documentation:** vault write-up.

## Verification gates (Definition of Done)

- [ ] `team_setup.py` has zero `mlx/CRO` references; 8 `mlx/CSO` (7 flipped + 1 validator).
- [ ] Each of the 4 direct-call helpers points at `:11502/v1/chat/completions`.
- [ ] `:11500` server stoppable without breaking trading (`mlx_servers.sh stop CRO` then a full cycle works).
- [ ] Boardroom can convene a deliberation that includes CMO (server D) and COO (server F).
- [ ] 24-hour live trading on 35B-only agents — no win-rate regression vs prior 7-day baseline.
- [ ] `boardroom-seat-mapping.md` divergence section marked RESOLVED.
- [ ] Vault entry written.

## References

### Code
- `Forex Trading Team/Source/agents/team_setup.py` — primary lever (Track A.1)
- `Handler/handler_swarm.py:1044-1048` — MLX_SERVERS port map (read-only context)
- `Forex Trading Team/Source/intelligence_agent_prep.py`, `snipe_cleanup.py`, `guardian_narrator.py`, `floor_chat.py` — direct-call helpers (Track A.2)
- `Forex Trading Team/Source/test_system_changes.py` — health checks (A.3)
- `Forex Trading Team/Source/trading_launcher.sh` — launcher (A.4)
- `Handler/seat_registry.py` — boardroom registry (read-only context for Track B)
- `~/jarvis/scripts/mlx_servers.sh` — boardroom launcher (Track B)
- `Forex Trading Team/Source/scripts/shadow_compare.py` — helper, already shipped

### Vault
- `collective/models/boardroom-seat-mapping.md`
- `collective/models/qwen3.5-35b/00-overview.md` + `03-known-issues-and-fixes.md`
- `collective/models/qwen3.5-9b/05-TA-agent-integration.md`
- `collective/models/qwen3-30b-a3b.md`, `qwen2.5-1.5b-instruct.md`
- Memory: `project_workspace_agent_registry.md`
