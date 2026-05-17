# Boardroom PhD-Level Prompts + Skill Bindings (Design)

**Date:** 2026-04-27
**Author:** claude-code
**Status:** approved (verbal, 2026-04-27)
**Related:** `boardroom-seat-mapping.md` (vault) + `2026-04-26-agent-35b-collapse-design.md` (preceding effort)

## Purpose

Upgrade every boardroom seat from MBA-level skeleton (currently ~40 lines / 3.5 KB each) to **PhD-level domain-expert prompt** with explicit skill bindings to the 357-skill vault inventory. Add **5 new seats** that close real gaps in the current 17-seat registry. Final state: **22 seats**, each represented by a system prompt that activates a senior, research-grounded perspective when convened, with the right skills bound for delegation.

## Why now

1. The **35B agent fleet** ships everything that runs *in* the trading system. The boardroom is what runs *across* workspaces — strategy, decisions, deliberations. It's where Tim's actual cognitive offloading happens. Skeleton prompts cap the value.
2. The 17 existing prompts were template-generated 2026-03-26. They're decent practitioner prompts, but they don't activate frontier-research depth. PhD-level lets the boardroom actually *think harder* than Tim does on a given topic.
3. The skill vault has **357 skills** — most seats currently bind 4-8. There's a 4× under-binding gap on average.
4. The 5 added seats close real domain gaps (markets, AI strategy, regulatory compliance, customer success, SRE) that the current 17 cannot cover even with strong prompts.

## Final seat list — 22 seats

### Existing 17 (rewritten to PhD level)

| ID | Title | Domain |
|---|---|---|
| CEO | Board Chair / CEO | Facilitation, synthesis, final decisions |
| CTO | Chief Technology Officer | Architecture, code, implementation, systems |
| CRO | Chief Risk Officer | Risk, quality gates, compliance validation |
| CSO | Chief Strategy Officer | Business strategy, market analysis, planning |
| CPO | Chief Product Officer | Roadmaps, features, user research, metrics |
| CMO | Chief Marketing Officer | Brand, campaigns, SEO, paid ads, content |
| CRvO | Chief Revenue Officer | Sales, pipeline, outreach, lead scoring |
| CDO | Chief Data Officer | Data architecture, intelligence, patterns |
| CFO | Chief Financial Officer | Budgets, forecasting, pricing, financials |
| COO | Chief Operating Officer | Process, automation, scheduling |
| CCO | Chief Creative Officer | Design, UX/UI, brand identity |
| CHRO | Chief HR Officer | Team, onboarding, internal comms |
| CISO | Chief Information Security Officer | Security, privacy, threat model |
| CXO | Chief Experience Officer | Customer experience, onboarding, churn |
| VPE | VP Engineering | CI/CD, infra, Docker, DevOps |
| CDS | Chief Data Scientist | ML models, training pipelines |
| GC | General Counsel | Contract, NDA, legal risk |

### New 5 (gap-closers)

| ID | Title | Why it closes a gap | Server |
|---|---|---|---|
| **CTrO** | Chief Trading Officer | Forex/markets domain expert. Microstructure, execution algos, P&L attribution. No current seat covers deep markets. | A (35B chair — domain depth + distilled trading skill stack) |
| **CAIO** | Chief AI Officer | Frontier-model governance, alignment, capability/safety planning, multi-vendor model strategy, distillation roadmap. CDS = pipelines; CTO = systems; nobody owns AI strategy. | A (35B) or B (DeepSeek for deep-think when convened) |
| **CCmpO** | Chief Compliance Officer | Regulatory compliance: CFTC/NFA for forex, SOC 2, audit prep, AI regulation tracking. GC = legal contracts; CISO = security; gap for ongoing regulatory work. | E (Qwen2.5-7B — paperwork-heavy, doesn't need 35B) |
| **VPCS** | VP Customer Success | Post-sale account ownership, expansion, retention, NPS. CXO = UX design; CRvO = acquisition; gap for tenant retention. | E (Qwen2.5-7B — relationship-narrative work) |
| **VPSRE** | VP Site Reliability | Observability, incident response, SLOs, postmortems. Connection Doctor workspace is exactly this concern — needs a boardroom voice. | F (Qwen2.5-1.5B — fast ops responses) |

**Naming notes (collision-avoidance):**
- `CCO` already taken by Creative → compliance uses `CCmpO`
- `CMO` already taken by Marketing → trading uses `CTrO`
- `CSO` already taken by Strategy → no collision with new seats

**Server assignment notes:** Server `A` (35B) ends up with CEO + CTrO + CAIO. Three seats sharing the chair model — fine, all distinct system prompts. The 35B is the carrier of distilled trading skill, so CTrO benefits from running there.

## What "PhD-level" means concretely

A PhD-level prompt has six layers, in this order, sized to the domain:

### 1. Theoretical foundation (the literature)
The academic frameworks practitioners reach for under stress. Examples:
- **CFO**: Modigliani-Miller, Damodaran valuation, Sharpe ratio, ASC 606 revenue recognition
- **CTrO**: market microstructure (Kyle 1985, Glosten-Milgrom), execution algos (VWAP/TWAP/IS), Almgren-Chriss optimal execution
- **CAIO**: Kaplan scaling laws, Constitutional AI, RLHF, distillation theory, alignment frameworks
- **CDS**: bias-variance, double descent, ICL theory, MoE routing

### 2. Mental models and decision protocols
How the senior practitioner thinks. Not "here are concepts" but "here is the decision tree under uncertainty."

### 3. Edge cases and exceptions
What an MBA misses but a PhD catches. The "but actually" knowledge.

### 4. Recent developments (post-2024)
Current methods + critiques of stale ones. Avoids stale prompt → stale boardroom.

### 5. Skill toolkit (bindings to 357-skill vault)
Concrete list of skills this seat reaches for, with **when-to-use** guidance. Not just "skills: [a, b, c]" — also "use `pricing-strategy` when CEO asks about packaging; use `variance-analysis` after end-of-month close."

### 6. Voice / cross-seat handoffs
- Voice: how this seat speaks (CFO precise/conservative; CMO creative/hypothesis-driven)
- Handoffs: when to defer to another seat (CFO defers to GC on tax-shelter structure; CMO defers to CDO on attribution data quality)

**Length target:** 200-400 lines per seat. Long enough to encode depth; short enough to stay within prompt cache budget.

## Skill mapping (foundation work)

The 357 skills in `knowledge/skills/` need grouping by function before per-seat binding. Output: a `boardroom/seat_skill_inventory.md` doc that maps:

```
Function clusters → individual skills → recommended seats (primary, secondary)
```

Example clusters:
- **Marketing & Growth** (~40 skills): ad-creative, brand-voice, campaign-planning, copywriting, seo-audit, etc. → CMO primary, CRvO secondary
- **Finance & Accounting** (~20 skills): financial-statements, variance-analysis, reconciliation, pricing-strategy → CFO primary
- **Engineering & Infrastructure** (~50 skills): backend-domain-orchestrator, agent-development, browser-specialist → CTO + VPE
- **Trading-specific** (likely ~10 skills): probably already in the trading team skill stack → CTrO primary
- ... etc

This mapping is built once and underpins every per-seat skill binding.

## Non-goals

- **Boardroom collapse to single 35B (Track C from the prior plan)** — out of scope. This spec keeps the multi-model architecture; richer prompts make the multi-model variety more valuable, not less.
- **Skill rewrites** — only mapping existing skills to seats; not authoring new skills.
- **Boardroom UI work** — voices, animations, etc. — out of scope.
- **Multi-tenant agent registry** (the gateway concern) — out of scope; that's Phase 2.
- **Per-seat distillation adapters** — out of scope. PhD-level prompts work on base models. Distillation is a future optimization.

## Phased plan

### Phase 0 — Registry update (½ day)
- Add 5 new seats to `Handler/seat_registry.py` (CTrO, CAIO, CCmpO, VPCS, VPSRE) with placeholder prompts
- Create skeleton prompt files in `knowledge/boardroom/prompts/` so the registry's `vault_prompt` paths resolve
- Smoke test: each new seat resolves to a server; smoke each model server

### Phase 1 — Skill mapping (1-2 days)
- Inventory all 357 skills by reading their frontmatter / first-paragraph descriptions
- Cluster into ~15-20 functional groups (marketing, finance, eng, trading, AI, legal, ops, etc.)
- Map each cluster to its primary + secondary seats
- Output: `knowledge/boardroom/seat_skill_inventory.md`
- Cross-validate: every seat has 15-25 skills bound; no skill is orphaned (uncrowded under any seat is acceptable; a skill with NO seat suggests the skill is workspace-specific not boardroom-relevant — flag for review)

### Phase 2 — Pilot 3 seats deeply (3-5 days)
Pilot mix: **CFO** (existing, finance), **CTrO** (new, trading domain), **CMO** (existing, creative).
- Per seat:
  1. Research: gather PhD-level domain knowledge via Anthropic API (Opus) — frameworks, edge cases, recent developments
  2. Draft: 200-400 line system prompt with all six layers
  3. Bind: skills from Phase 1 mapping
  4. A/B test: run a real boardroom deliberation under OLD vs NEW prompt, compare outputs (Tim or human grader rates which version is more useful)
- Output: 3 production-quality seat prompts + an A/B test rubric

### Phase 3 — Scale to remaining 19 seats (~2 weeks)
- Apply the pilot's pattern to: CEO, CTO, CRO, CSO, CPO, CRvO, CDO, COO, CCO, CHRO, CISO, CXO, VPE, CDS, GC + CAIO, CCmpO, VPCS, VPSRE (19 total)
- Per seat: ~½ day research + drafting
- Cross-seat consistency review at the end (do handoff protocols align? are voices distinct enough?)

### Phase 4 — Verification + ship (1-2 days)
- Convene-test: run a complex deliberation that pulls 6+ seats. Verify each contributes domain depth, not generic advice.
- Memory check: peak model memory under multi-tier convene (post addition of CTrO, CAIO, CCmpO, VPCS, VPSRE) — confirm under 60 GB.
- Vault writeup of process + deliverables.

## Dependencies / parallelism

- Phase 0 + Phase 1 can run in parallel
- Phase 2 (pilot) blocks on Phase 1 (skill mapping)
- Phase 3 blocks on Phase 2 sign-off
- Total wall-clock: **~3-4 weeks** assuming half-time effort
- Total seat count to research: **22** (17 rewrites + 5 new)

## Quality gates

| Gate | How verified |
|---|---|
| Each prompt has all 6 layers (theory, mental models, edge cases, recent dev, skills, voice/handoffs) | Manual checklist per seat |
| Skill bindings are non-empty (15-25 per seat) | Phase 1 inventory check |
| A/B test on 3 pilot seats shows new > old | Tim or human grader |
| Multi-tier convene handles 6+ seats under 60 GB | Phase 4 smoke test |
| Boardroom convene latency stays acceptable (<3 min for 6-seat deliberation including cold-starts) | Phase 4 timing |

## Open questions for Tim before Phase 0

1. **Phase 0 timing** — kick off this week, or after the trading 35B collapse settles for a few days?
2. **Pilot seat selection** — CFO, CTrO, CMO is my proposal. Want to swap any? (e.g., CTO instead of CMO if engineering deliberations are more common.)
3. **PhD-knowledge source** — Anthropic API (Opus) for research per seat is my default. Want web search added? Web search costs ~$0.01-$0.05 per query, modest. Or do you have source materials (textbooks, courses) you'd want me to ingest?
4. **A/B test grader** — you, or should I write an automated grading prompt + use a different model as judge?

---

## REVISION 2026-04-27 — Tim's directives (post-AgentBuilder discovery)

**Generation method.** All 22 seat prompts (17 rewrites + 5 new) generated by `Handler/handler_agent_builder.py::create_agent_simple()`. Builder takes a structured `AgentSpecialization` (domain, expertise=10, capabilities, tools, knowledge_base list) and emits prompt + skill registrations.

**Generator model = Opus** (not the builder's Sonnet default). Configure via the LLMRouter inside AgentBuilder. PhD-depth requires the stronger model; Sonnet would re-create the MBA-skeleton problem.

**Boardroom reads from registry.** Architectural change: boardroom code stops reading `knowledge/boardroom/prompts/*.md` files. Instead reads from PromptRegistry + AgentRegistry — same path every other Jarvis agent uses. Eliminates the storage gap (no need to write generated prompts to two places). One source of truth.

**Required spike before scaling.** Run ONE test invocation (regenerate CFO via `create_agent_simple` with Opus) to answer empirically:
- Does PromptRegistry receive the prompt?
- Does AgentRegistry receive the agent + skills?
- Does the existing seat's AgentRegistry entry get overwritten or duplicated?
- What's the prompt quality from Opus + a rich `knowledge_base`?

Spike outputs a go/no-go: if quality + storage path land cleanly, scale to 22. If not, iterate the spec inputs.

**PAUSED 2026-04-27.** Trading floor + validator producing odd output post-35B migration. Investigation takes priority over boardroom work. Resume after trading is stable.

## Files this spec touches

| File | Action |
|---|---|
| `Handler/seat_registry.py` | Add 5 new seats |
| `knowledge/boardroom/prompts/{ctrO,caio,ccmpo,vpcs,vpsre}.md` | Create (initially skeleton, then PhD-level) |
| `knowledge/boardroom/prompts/{17 existing}.md` | Rewrite to PhD-level |
| `knowledge/boardroom/seat_skill_inventory.md` | Create — Phase 1 output |
| `knowledge/collective/models/boardroom-seat-mapping.md` | Update for 22 seats |
| Vault learnings entry | Final writeup |
