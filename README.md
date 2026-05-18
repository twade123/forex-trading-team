# Forex Trading Team

A multi-agent AI trading system that runs a swarm of LLM specialists to evaluate and manage live forex positions on OANDA. Originally built on the Anthropic API, the production validator now runs on a **locally-served Qwen3.5 35B model distilled from Claude Opus traces** — eliminating per-trade API cost and enabling sub-second iteration loops.

The system monitors 13 forex pairs continuously, evaluates trade setups through a multi-stage pipeline (chart pattern detection → technical analyst → validator judgment → execution), then hands live positions to a guardian agent that manages exits in real time.

## Dashboard walkthrough

https://github.com/twade123/forex-trading-team/raw/main/docs/videos/trading-ui-walkthrough.mp4

> 30-second walkthrough of the dashboard — pair tabs, validator verdict cards, guardian threat zones, cycle stage timeline. (Click the video above; GitHub renders MP4s inline.)

---

## Why this project exists

Most "AI trading bots" are a single model behind a thin wrapper. This system treats each step of a trade's lifecycle as a separate specialist agent with a constrained role, a structured input contract, and a verifiable output. The thesis: **a disciplined swarm of role-bounded LLM agents — backed by deterministic infrastructure for state, observability, and learning — outperforms any single-model approach.**

It's been running live since early 2026 across multiple iteration cycles documented in `/Prompts/` (version-controlled prompt history) and `Source/scripts/replay_iter*.py` (cohort replays against historical trades).

---

## Architecture

```
                                ┌──────────────────────┐
                                │   Scheduler (cron)   │
                                └──────────┬───────────┘
                                           │
                          ┌────────────────┴────────────────┐
                          │                                 │
                          ▼                                 ▼
                  ┌───────────────┐                 ┌───────────────┐
                  │  Trade Scout  │                 │  Intelligence │
                  │ (continuous   │                 │   (news /     │
                  │  M15 scan)    │                 │   macro 3×/d) │
                  └───────┬───────┘                 └───────┬───────┘
                          │ setup alert                     │ briefing
                          ▼                                 │
                  ┌───────────────────────────────────────────┐
                  │           Cycle Orchestrator              │
                  │  (gathers data → fans out to specialists) │
                  └─┬──────────────┬──────────────┬───────────┘
                    │              │              │
                    ▼              ▼              ▼
            ┌─────────────┐ ┌──────────────┐ ┌──────────────┐
            │ OANDA Data  │ │  Technical   │ │   Chart      │
            │   Agent     │ │  Analyst     │ │  Generator   │
            │  (candles,  │ │  (indicators │ │  (PNG for    │
            │   pricing)  │ │   + thesis)  │ │   validator) │
            └──────┬──────┘ └──────┬───────┘ └──────┬───────┘
                   │               │                │
                   └───────────────┴────────────────┘
                                   │
                                   ▼
                       ┌───────────────────────┐
                       │      VALIDATOR        │  ◄── Local Qwen3.5 35B
                       │  (sole trade gate)    │      LoRA-distilled
                       │ CONFIRM/WATCH/REJECT  │      from Opus traces
                       └───────────┬───────────┘      (MLX, vision-enabled)
                                   │ CONFIRM
                                   ▼
                       ┌───────────────────────┐
                       │   Execution Agent     │
                       │  (OANDA order place)  │
                       └───────────┬───────────┘
                                   │ trade open
                                   ▼
                       ┌───────────────────────┐
                       │   Position Guardian   │  ◄── per-trade watcher,
                       │ (live exit manager —  │      monitors M1 price /
                       │  trailing stops, BE,  │      M15 structure,
                       │  exhaustion exits)    │      manages floor + SL
                       └───────────────────────┘

                       ┌───────────────────────┐
                       │   Flight Recorder     │  ◄── every stage logged
                       │  (SQLite, WAL mode)   │      with timing,
                       │                       │      inputs, verdicts
                       └───────────────────────┘

                       ┌───────────────────────┐
                       │   Knowledge Vault     │  ◄── shared memory:
                       │ (FTS5, cross-agent)   │      decisions, fixes,
                       │                       │      patterns, replays
                       └───────────────────────┘
```

---

## The agents

| Agent | Where | Role |
|---|---|---|
| **Trade Scout** | `Source/trade_scout.py` | Continuous M15 scan across 13 pairs. Detects 12 setup classes (fan exhaustion, retrace continuation, cascade, etc.) and queues high-probability setups for validation. |
| **Technical Analyst** | `Source/agents/trading_cycle.py` + `Prompts/technical_analyst_v4.md` | Reads computed indicators (Sniper V4 fan, EMA structure, stoch, RSI, MACD), writes a thesis paragraph with conviction score. |
| **Chart Generator** | `Source/chart_generator.py` | Renders M15 chart PNG (299 candles, EMA overlays, indicator panes) for the vision-capable validator. |
| **Validator** | `Source/trade_validator.py` + `Prompts/ghost_validator_v1.md` | **Sole trade authority.** Receives full data package + chart image. Outputs CONFIRM / WATCH / REJECT with reasoning. Runs on local Qwen3.5 35B (LoRA-distilled from Opus). |
| **Cycle Orchestrator** | `Source/agents/trading_cycle.py` | Coordinates the 11-stage pipeline, manages timeouts, handles partial failures. |
| **Execution Agent** | `Source/position_manager.py` + OANDA MCP | Places, modifies, closes orders. Enforces position-sizing rules. |
| **Position Guardian** | `Source/position_guardian.py` | Per-trade live watcher. Multi-timeframe (M1 for price/EMA, M15 for structure). Manages trailing stops, breakeven moves, exhaustion exits. |
| **Watch Manager** | `Source/agents/watch_manager.py` | Promotes WATCH verdicts to TRADE when entry conditions trigger. Multi-source unification (scout setups + user-flagged + experimental signals). |

---

## Notable technical choices

### Local LLM serving via distillation flywheel
The validator originally ran on Claude Opus / Sonnet. Cost was ~$0.08/call and latency was ~3 minutes per evaluation. We logged every Opus verdict + reasoning into a training dataset, fine-tuned a **Qwen3.5 35B-A3B 4-bit quantized model via LoRA**, and now serve it locally on Apple Silicon via MLX. Result: zero per-call cost, sub-second inference, near-parity verdicts.

The serving stack is in `Source/serving/` (drift checking, adapter fusion, pinned prompts). The training pipeline writes Opus-graded examples to `Source/validator_training_extractor.py`.

### Ghost validator (continuous A/B vs Anthropic)
`Source/optimizer/ghost_replay.py` runs every live validator decision through both the local 35B **and** the original Anthropic model, comparing verdicts. Disagreements feed back into the training set. This is how we know the local model still tracks production quality.

### Vision-capable validator
The validator doesn't just read indicator values — it sees the chart. `vision_validator.py` packages the M15 chart PNG into the prompt, letting the model reason about pattern context (fan compression, trend exhaustion, S/R proximity) that's hard to encode numerically.

### Flight recorder (observability)
`Source/flight_recorder.py` writes every stage of every cycle to SQLite (WAL mode, indexed by `cycle_id` + `stage`). Lets us answer "why did this trade lose?" by replaying the exact data the validator saw at decision time. Used by `scripts/replay_iter*.py` for cohort analysis when tuning the prompt.

### Knowledge vault (shared memory across agents)
Located outside this repo (`~/jarvis/knowledge/`) but referenced throughout. Every agent writes lessons / fixes / patterns to a FTS5-indexed markdown corpus that all other agents read at task start. This is how the system gets smarter over time without monolithic retraining.

### Prompt engineering as code
`Prompts/` contains the live validator (`ghost_validator_v1.md`) and supporting agent prompts (TA, orchestrator, execution, reporter). Iteration history lives in git — every prompt change is a commit, replayed against the same cohort before deploying.

---

## Tech stack

- **Language:** Python 3.11+
- **Local LLM serving:** [MLX](https://github.com/ml-explore/mlx) on Apple Silicon (Qwen3.5 35B-A3B-4bit + LoRA adapter)
- **Original prototype models:** Claude Opus 4.x, Sonnet 4.6 (still optional fallback)
- **Broker:** OANDA REST API
- **Storage:** SQLite (WAL mode, busy_timeout=30s) for trade state + flight recorder
- **Dashboard:** Flask + Server-Sent Events (vanilla JS frontend)
- **Charting:** matplotlib (server-side PNG render)
- **Orchestration:** custom scheduler, no Celery/Airflow

---

## Repository layout

```
Source/                  # All trading logic
  agents/                # Cycle orchestrator, watch manager
  scripts/               # Replay tools, audits, backfills, cohort analysis
  optimizer/             # Backtest harness, ghost replay, optuna studies
  backtester/            # Historical replay engine
  serving/               # MLX serving, LoRA adapters, drift checking
  migrations/            # SQLite schema migrations
  Database/              # DB initialization scripts
  trade_scout.py         # Continuous market scanner
  trade_validator.py     # Validator wrapper (local 35B or Anthropic)
  position_guardian.py   # Live trade manager
  flight_recorder.py     # Observability sink
  scheduler.py           # Cron-style cycle orchestrator
  ...
dashboard/               # Flask + SSE dashboard
Prompts/                 # Live agent prompts (markdown, git-tracked)
MCP/                     # MCP servers (OANDA, technical analysis)
Skills/                  # Plugin skills (chart context, pattern library)
Config/                  # Runtime configuration
scripts/                 # Top-level operational scripts
docs/                    # Research notes, design specs
```

---

## Quick start

> Note: this repo is the trading workspace from a larger Jarvis system. It depends on environment-level config (OANDA credentials, a local MLX server, knowledge vault path) that lives outside the repo. See `.env.example` for required variables.

```bash
# 1. Clone and install
git clone https://github.com/twade123/forex-trading-team.git
cd forex-trading-team
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt  # generate from imports if missing

# 2. Configure
cp .env.example .env
# Edit .env: OANDA_API_KEY, OANDA_ACCOUNT_ID, ANTHROPIC_API_KEY (optional fallback)

# 3. Initialize databases
python Source/migrations/run_migrations.py

# 4. Start the dashboard (port 8766)
python dashboard/api_server.py

# 5. Start the scheduler (runs scout + cycle + guardian on cron)
python Source/scheduler.py
```

For local validator serving (MLX on Apple Silicon), see `Source/serving/` — the model adapter is published separately (not included in this repo).

---

## Experimental components (not active in live trading)

- **Kronos** (`Source/kronos_*.py`) — M15 OHLCV sequence forecaster. Hunter/filter/guardian/threat modules built and shadow-tested but **currently disabled**. See git history for the empirical analysis that led to disabling.
- **Setup discovery** (`Source/setup_discovery.py`) — automated pattern miner that proposes new setup classes from historical winners. Used periodically, not in the live loop.
- **Manual trade learner** (`Source/manual_trade_*.py`) — captures manually-flagged Tim trades for inclusion in the validator training set.

---

## Status

Live since early 2026. Currently in **v1.x recovery** after a TA-validator unification regression (see `project_unified_validator_tradeoff` in vault). Ongoing work: scout late-entry fix, guardian exit-marker tuning, distillation refresh against latest Opus traces.

---

## License

Private — provided here for review purposes.
