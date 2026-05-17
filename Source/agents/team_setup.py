"""
Trading Team Setup -- dynamic agent creation via AgentBuilder infrastructure.

Creates agents through Jarvis AgentBuilder (not hardcoded definitions),
registers Source computation modules as versioned skills in agent_skills table,
and forms the trading team via SwarmHandler.

Workspace IDs persist to .trading_team_workspaces.json for restart recovery.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("trading_bot.team_setup")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PARENT_WORKSPACE_NAME = "Forex Trading Team"
PARENT_WORKSPACE_DESC = "Automated forex trading -- 8 agents coordinated via swarm"

# Base paths for prompt and skill files (resolved relative to Forex Trading Team root)
_TRADING_BOT_ROOT = Path(__file__).parent.parent.parent  # .../Forex Trading Team/
_PROMPTS_DIR = _TRADING_BOT_ROOT / "Prompts"  # Legacy fallback only — vault is canonical
_SKILLS_DIR = _TRADING_BOT_ROOT / "Skills"

# Knowledge vault — canonical source for all agent prompts
_VAULT_DIR = Path(__file__).parent.parent.parent.parent / "knowledge"  # .../jarvis/knowledge/


_AGENT_REGISTRY_DB = _TRADING_BOT_ROOT.parent / "Database" / "v2" / "agents.db"
_TEAM_ID = "forex-v4-prod"  # Production team — change to run a different team


def _get_registry_vault_path(agent_name: str) -> str | None:
    """Look up the agent's vault_path from v2/agents.db registry."""
    try:
        import sqlite3 as _sq
        conn = _sq.connect(str(_AGENT_REGISTRY_DB))
        cur  = conn.cursor()
        cur.execute("SELECT vault_path FROM agent_registry WHERE agent_name=?", (agent_name,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row and row[0] else None
    except Exception as e:
        logger.debug("Registry vault_path lookup failed for %s: %s", agent_name, e)
        return None


def _load_prompt_from_vault(agent_name: str) -> str | None:
    """Load agent system prompt from vault — checks registry for vault_path first.

    Priority:
    1. Registry vault_path (v2/agents.db) — authoritative per-agent path
    2. Convention path: knowledge/agents/{agent_name}/prompt.md
    Returns prompt text or None if not found.
    """
    # 1. Registry-backed path (authoritative)
    registry_path = _get_registry_vault_path(agent_name)
    if registry_path:
        vault_prompt = _VAULT_DIR / registry_path
        if vault_prompt.exists():
            text = vault_prompt.read_text().strip()
            logger.info("Loaded prompt via registry for %s: %s (%d chars)",
                        agent_name, registry_path, len(text))
            return text

    # 2. Convention fallback
    vault_prompt = _VAULT_DIR / "agents" / agent_name / "prompt.md"
    if vault_prompt.exists():
        text = vault_prompt.read_text().strip()
        logger.info("Loaded prompt via convention for %s (%d chars)", agent_name, len(text))
        return text

    return None


def _load_local_agent_prompt(spec: Dict[str, Any]) -> str | None:
    """Load the lean local-model system prompt + local skill files (if defined in spec).

    When an agent has a distilled local variant (e.g. validator on mlx/CSO), its
    full vault prompt is too large for the local model's prefill budget. This
    helper returns the compact `prompt_file_local` from Prompts/ — which carries
    identity + domain knowledge — concatenated with any `skill_files_local`
    (the tools/hammers the agent can swing). Returns None if neither defined.

    Architecture (Anthropic-style separation):
      prompt_file_local   = IDENTITY + KNOWLEDGE (who you are + what you know)
      skill_files_local   = TOOLS (the hammer you swing)
    """
    parts: List[str] = []

    # 1. Identity + knowledge (the prompt)
    local_file = spec.get("prompt_file_local")
    if local_file:
        p = _PROMPTS_DIR / local_file
        if p.exists():
            text = p.read_text().strip()
            parts.append(text)
            logger.info("Loaded LOCAL-model prompt for %s: %s (%d chars)",
                        spec["name"], local_file, len(text))
        else:
            logger.warning("Local prompt file %s not found for agent %s", p, spec["name"])

    # 2. Skill files (the tools)
    for skill_file in spec.get("skill_files_local", []):
        s = _SKILLS_DIR / skill_file
        if s.exists():
            skill_text = s.read_text().strip()
            parts.append(f"\n\n---\n\n# Skill: {skill_file}\n\n{skill_text}")
            logger.info("Appended LOCAL skill %s (%d chars) for agent %s",
                        skill_file, len(skill_text), spec["name"])
        else:
            logger.warning("Local skill file %s not found for agent %s", s, spec["name"])

    if not parts:
        return None
    return "\n\n".join(parts)


def _load_agent_prompt(spec: Dict[str, Any]) -> str:
    """Build the full system prompt for an agent from vault (canonical) + skill files.

    Priority:
    1. Load from vault: knowledge/agents/{name}/prompt.md  ← canonical source
    2. Append vault learnings: knowledge/agents/{name}/learnings.md (accumulated knowledge)
    3. Append each ``skill_files`` from Skills/ as reference sections
    4. Fallback: legacy Prompts/ directory (temporary, until all prompts are in vault)
    5. Last resort: join ``knowledge_base`` bullets

    Returns the assembled prompt string.
    """
    parts: List[str] = []
    agent_name = spec["name"]

    # --- Primary prompt from vault (canonical) ---
    vault_prompt = _load_prompt_from_vault(agent_name)
    if vault_prompt:
        parts.append(vault_prompt)

        # Append vault learnings — what this agent has accumulated across all sessions
        learnings_path = _VAULT_DIR / "agents" / agent_name / "learnings.md"
        if learnings_path.exists():
            learnings_text = learnings_path.read_text().strip()
            # Only append if there are actual learnings (not just the empty stub)
            if len(learnings_text) > 200 and "No learnings yet" not in learnings_text:
                parts.append(f"\n\n---\n\n## YOUR INSTITUTIONAL MEMORY\n\n{learnings_text}")
                logger.info("Appended vault learnings for agent %s", agent_name)

    else:
        # --- Legacy fallback: Prompts/ directory ---
        prompt_file = spec.get("prompt_file")
        if prompt_file:
            p = _PROMPTS_DIR / prompt_file
            if p.exists():
                parts.append(p.read_text().strip())
                logger.warning("Agent %s: loaded from legacy Prompts/ (migrate to vault)", agent_name)
            else:
                logger.warning("Prompt file %s not found for agent %s (checked vault + legacy)", p, agent_name)

    # --- Skill reference docs from Skills/*.md ---
    for skill_file in spec.get("skill_files", []):
        s = _SKILLS_DIR / skill_file
        if s.exists():
            content = s.read_text().strip()
            parts.append(f"\n\n---\n\n# Skill Reference: {skill_file}\n\n{content}")
            logger.info("Appended skill file %s (%d chars) for agent %s",
                        skill_file, len(content), spec["name"])
        else:
            logger.warning("Skill file %s not found for agent %s", s, spec["name"])

    # --- Fallback: knowledge_base bullets ---
    if not parts:
        kb = spec.get("knowledge_base", [])
        if kb:
            parts.append("## Domain Knowledge\n\n" + "\n".join(f"- {item}" for item in kb))
            logger.info("Using knowledge_base fallback (%d items) for agent %s",
                        len(kb), spec["name"])

    return "\n\n".join(parts) if parts else spec.get("role", "You are a trading agent.")

# ---------------------------------------------------------------------------
# Agent specifications -- 8 agents (V2 restructure, Feb 2026)
#
# Each spec feeds into AgentBuilder for prompt generation and AgentRegistry
# for skill/performance tracking.  The ``knowledge_base`` list drives the
# system prompt content -- AgentBuilder uses it in all prompt generation
# paths (orchestrator, AI, fallback template).
#
# Merged news_analyst + weather_analyst + wolfram_analyst → intelligence
# (single agent with 3 MCPs, currency-aware via currency_intelligence_map).
# ---------------------------------------------------------------------------

AGENT_SPECS: List[Dict[str, Any]] = [
    {
        "name": "oanda_data",
        "model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). Tool calling on 35B verified via swarm dispatch.
        "role": "Market data collection — fetch candles, pricing, account state from OANDA",
        "agent_type": "data_collection",
        "workspace": "Oanda Data",
        "expertise_level": 9,
        "capabilities": ["analytical", "data_analysis"],
        "mcp_tools": ["handler_oanda"],
        "prompt_file": "oanda_data.md",
        "skill_files": ["OANDA_MCP.md"],
        "knowledge_base": [
            "OANDA REST API v20: /instruments/{instrument}/candles, /accounts/{id}/summary, /pricing",
            "Supported granularities: S5 S10 S15 S30 M1 M2 M4 M5 M10 M15 M30 H1 H2 H3 H4 H6 H8 H12 D W M",
            "Primary timeframe H1, confirmation H4, detail M15",
            "Always fetch 250 candles per timeframe for indicator warmup",
            "Practice account: 101-001-24637237-001, practice URL: api-fxpractice.oanda.com",
            "Bid/ask pricing — use mid for indicators, bid for sells, ask for buys",
            "Spread awareness: normal EUR_USD ~1.2 pips, wide >3 pips = warning",
            "13 instruments traded: EUR_USD USD_JPY GBP_USD AUD_USD NZD_USD USD_CAD USD_CHF EUR_GBP EUR_JPY GBP_JPY AUD_NZD EUR_CHF EUR_AUD",
            "Market hours: Sun 5pm ET open, Fri 5pm ET close. Sessions: Sydney, Tokyo, London, New York",
            "Data freshness: candles older than 2 hours stale for H1 analysis",
        ],
        "skills": [
            {"name": "fetch_candles", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "fetch_candles"}},
            {"name": "get_account_summary", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "get_account_summary"}},
            {"name": "fetch_multi_timeframe", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "fetch_multi_timeframe"}},
            {"name": "get_current_pricing", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "get_current_pricing"}},
            {"name": "get_instrument_specs", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "get_instrument_specs"}},
        ],
    },
    {
        "name": "intelligence",
        "model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). Reads pre-cached briefings; cache populated by intelligence_agent_prep.py.
        "role": "Currency-aware news, weather, and statistical analysis for forex trading",
        "agent_type": "data_collection",
        "workspace": "Intelligence",
        "expertise_level": 9,
        "capabilities": ["analytical", "research", "news_analysis", "statistical_analysis", "weather_forecasting"],
        "mcp_tools": ["handler_news_info", "handler_weather", "handler_wolfram"],
        "prompt_file": "intelligence.md",
        "skill_files": ["NEWS_MCP.md", "WEATHER_MCP.md", "WOLFRAM_MCP.md"],
        "knowledge_base": [
            "Currency intelligence mapping: each pair maps to specific news queries, weather regions, and statistical checks",
            "News scoring: central bank decisions HIGH impact, employment data HIGH, PMI MEDIUM, sentiment LOW",
            "Economic calendar awareness: never trade 30min before high-impact events (NFP, CPI, rate decisions)",
            "Weather is a FILTER not a signal: only speaks up for extreme events (severity >= 3) affecting commodity currencies",
            "Commodity-linked currencies: AUD (iron ore, coal), CAD (oil), NZD (dairy, agriculture)",
            "Non-commodity pairs (EUR_USD, GBP_USD, USD_JPY): skip weather check entirely",
            "Wolfram for statistical validation: correlation checks between open positions, significance testing, Kelly criterion sizing",
            "Correlated pairs: EUR_USD/GBP_USD (0.87), AUD_USD/NZD_USD (0.92), USD_CHF/EUR_USD (-0.85)",
            "Log all intelligence to: news_events, weather_events, wolfram_analyses tables in trading_forex.db",
            "Post combined intelligence as single DATA_DELIVERY to task thread, @mention technical_analyst and validator",
            "Sentiment scoring: -1.0 (extreme bearish) to +1.0 (extreme bullish), use absolute value for impact weight",
        ],
        "skills": [
            {"name": "gather_intelligence", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "gather_intelligence"}},
            {"name": "query_news_for_pair", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "query_news_for_pair"}},
            {"name": "check_weather_for_pair", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "check_weather_for_pair"}},
            {"name": "run_statistical_checks", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "run_statistical_checks"}},
            {"name": "forex_news_impact_analysis", "type": "mcp_tool",
             "definition": {"handler": "handler_news_info", "action": "get_news"}},
            {"name": "commodity_weather_impact_analysis", "type": "mcp_tool",
             "definition": {"handler": "handler_weather", "action": "get_weather"}},
            {"name": "forex_mathematical_interpretation", "type": "mcp_tool",
             "definition": {"handler": "handler_wolfram", "action": "query"}},
        ],
    },
    {
        "name": "technical_analyst",
        "model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). TA narrative on 35B's distilled trading skill stack.
        "role": "Technical analysis with indicators, candlestick/chart patterns, and historical context from backtest DB",
        "agent_type": "analysis",
        "workspace": "Technical Analysis",
        "expertise_level": 10,
        "capabilities": ["analytical", "data_analysis", "statistical_analysis", "domain_specific"],
        "mcp_tools": [],
        "prompt_file": "technical_analyst_v4.md",
        "skill_files": ["TECHNICAL_ANALYSIS.md"],
        "knowledge_base": [
            "20 trading setups (S1-S20) defined in complete_visual_knowledge_base.md, each with unique entry criteria",
            "S15 divergence = king setup: 96-100% win rate in exhaustion/ranging regimes",
            "Regime detection: strong_trend (ADX>30), ranging (ADX<20 + BB squeeze), exhaustion (RSI extreme + momentum fade), squeeze (BB width < threshold), high_volatility (ATR spike)",
            "Indicators: EMA(21/55/100), RSI(14), MACD(12,26,9), Bollinger Bands(20,2), ATR(14), Stochastic(14,3,3), ADX(14)",
            "22 candlestick patterns: hammer, engulfing, doji, morning/evening star, harami, piercing, dark cloud, etc.",
            "14 chart patterns: head & shoulders, double top/bottom, triangles, flags, cup & handle, wedges",
            "Confluence scoring: combine indicator + candle + chart signals, only trade when >70/100",
            "H4 filter: +4.1 percentage point edge when H4 agrees with H1 direction",
            "BEFORE scanning setups: query TradingDB.get_best_params(pair, regime) for historical context",
            "SUPPRESS signals from setups with known poor performance: query TradingDB.get_loss_patterns(pair, setup, regime)",
            "Priority-rank signals: S15 in ranging = HIGH, S3 when ADX<22 = suppress",
            "Read prior task comments from intelligence agent for news sentiment + weather warnings",
            "Best trading window: 8AM-12PM ET (London-NY overlap), worst: Asian session for EUR pairs",
            "backtest_setup_performance table: 39,692 rows, 308 patterns per pair for EUR_USD",
        ],
        "skills": [],
    },
    {
        "name": "validator",
        "model": "mlx/CSO",  # LOCAL 35B — was claude-sonnet-4-6 (swap back if needed)
        "role": "V4 VISION TRADING BRAIN — reads chart images + teaching examples, sole trade decision maker with DB evidence tools",
        "agent_type": "validation",
        "workspace": "Data Validator",
        "expertise_level": 10,
        "capabilities": ["analytical", "data_analysis", "domain_specific", "vision"],
        "mcp_tools": ["handler_data_validator"],
        "prompt_file": "validator_v4.md",  # Opus/Sonnet cloud path — full V4 with teaching examples
        "prompt_file_local": "ghost_validator_v1.md",  # Local 35B path — distilled model, lean thesis-first prompt + snipe-criteria knowledge
        "skill_files_local": ["VALIDATOR_TOOLS.md", "pattern_library.md", "tier1_setup_catalog.md"],  # 2026-04-27: pattern_library.md reverted — empirical comparison showed yesterday-with-library produced strong reads (bearish ORDERED, fishing-line vocab, dir=SELL committed) while today-without-library defaulted to flat-ranging non-commits even on identical TA narratives. Library acts as semantic primer; the rare tim_teach_X regurgitation is mitigated by ghost_validator_v1.md's NEVER-FABRICATE / IMAGE_UNCLEAR rules added today. 2026-04-29: tier1_setup_catalog.md added — supplementary catalog of 7 backtested non-fan setups (C1/C3/C4/C5/C8/C9/C11). When scout fires one of these alert_types, validator uses the catalog's REQUIRED+BONUS+ANTI-PATTERNS instead of fan checklist.
        "skill_files": ["DATA_VALIDATOR_MCP.md", "DATA_VALIDATOR.md"],
        "knowledge_base": [
            "4-step validation pipeline: Gate 1 (data quality) → Gate 2 (trade quality) → DB evidence → final verdict",
            "Gate 1: check data freshness, spread width, indicator completeness, candle count",
            "Gate 2: check setup validity, regime match, session timing, news proximity",
            "DB evidence: query backtest_setup_performance for win_rate, profit_factor, trade_count, best_session",
            "Minimum thresholds: win_rate >= 60%, profit_factor >= 1.3, trade_count >= 20",
            "Loss pattern detection: query backtest_trades WHERE result='loss' grouped by indicator ranges",
            "Confluence check: when multiple setups fire together, check historical combined win rate",
            "Performance drift detection: compare last N live trades against historical baseline",
            "Verdicts: APPROVE (high confidence), CAUTION/REDUCE (marginal), REJECT (poor evidence or data issues)",
            "Read ENTIRE task thread: oanda_data + intelligence + technical_analyst results before deciding",
            "Log every decision to trade_decisions table with full agent recommendations and reasoning",
            "DecisionLogger pipeline: 212ms end-to-end, logs to trading_forex.db",
            "HOSTILE_REGIMES config for session filtering instead of scanning 8.5M rows",
        ],
        "skills": [
            {"name": "run_full_validation", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "run_full_validation"}},
            {"name": "trade_validator.validate", "type": "python_callable",
             "definition": {"module": "Source.trade_validator", "function": "validate"}},
            {"name": "validation_analyst.analyze_on_demand", "type": "python_callable",
             "definition": {"module": "Source.validation_analyst", "function": "analyze_on_demand"}},
        ],
    },
    {
        "name": "execution",
        "model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). Order placement via swarm dispatch.
        "role": "Trade execution and position management with 12 exit rules",
        "agent_type": "execution",
        "workspace": "Execution",
        "expertise_level": 9,
        "capabilities": ["analytical", "domain_specific"],
        "mcp_tools": ["handler_oanda"],
        "prompt_file": "execution.md",
        "skill_files": ["OANDA_MCP.md"],
        "knowledge_base": [
            "OANDA order types: MARKET (immediate), LIMIT (entry price), STOP (stop-loss), TRAILING_STOP",
            "Position sizing: Kelly criterion from Wolfram, capped at 2% account risk per trade",
            "12 exit rules: TP hit, SL hit, trailing stop, partial exit (50% at 1:1), max hold time (48h), regime change, news event, correlation breach, drift detection, session end, spread widening, manual override",
            "Partial exits: take 50% at 1:1 RR, move SL to breakeven, let remainder run to full TP",
            "Trailing stop: activate after 1:1 reached, trail at 1.5×ATR",
            "Spread awareness: if spread > 3× normal, delay entry or reduce size",
            "Deduplication: CLOSE > PARTIAL_EXIT > TIGHTEN_SL > MOVE_TO_BE > HOLD",
            "PositionManager checks all 12 rules against live market state",
            "Log execution to live_trades table mirroring backtest_trades schema (67 columns)",
            "Link live_trades.decision_id to trade_decisions for full audit trail",
            "Correlated pair exposure: max 1 position per correlated group (EUR_USD+GBP_USD = same group)",
        ],
        "skills": [
            {"name": "place_market_order", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "place_market_order"}},
            {"name": "get_position_status", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "get_position_status"}},
            {"name": "update_monitored_positions", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "update_monitored_positions"}},
            {"name": "position_manager.check_positions", "type": "python_callable",
             "definition": {"module": "Source.position_manager", "function": "check_positions"}},
        ],
    },
    {
        "name": "trade_monitor",
        "model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). Narrator role only — guardian remains sole close authority.
        "role": "Position narrator & market awareness — narrates guardian state, watches snipes, reports to floor chat",
        "agent_type": "monitoring",
        "workspace": "Trade Monitor",
        "expertise_level": 8,
        "capabilities": ["analytical", "data_analysis", "monitoring"],
        "mcp_tools": ["handler_oanda"],
        "prompt_file": "position_monitor_v5.md",
        "skill_files": ["OANDA_MCP.md"],
        "knowledge_base": [
            "V5 ARCHITECTURE (2026-04-06): Guardian is sole trade manager. You NARRATE, never close/tighten/escalate.",
            "TWO JOBS: (1) Narrate guardian state to the trader in human-readable language. (2) Watch active snipes for condition progress.",
            "GUARDIAN NARRATOR: Translate threat levels, phases, and retrace state into plain English. 'YELLOW 39 in retrace' → 'Normal pullback, candles bouncing off E55.'",
            "SNIPE WATCHING: Report condition progress as percentage. 'EUR_USD snipe at 70% — missing BB expansion.'",
            "FLOOR CHAT: When user asks 'how is my trade?' — pull live data, read guardian threats, give 1-2 sentence status per trade.",
            "You NEVER make close/tighten/escalate decisions. Guardian handles ALL trade exits with retrace-aware logic.",
            "You NEVER look at chart images. You read numbers: threat levels, phases, P&L, conditions progress.",
            "When guardian holds during RED — EXPLAIN why (retrace awareness, candle-EMA conviction), don't question it.",
            "Deactivation: when zero open trades AND zero active snipes, go dormant until next alert.",
            "Every narrative you produce = training data for local model distillation. Be clean and consistent.",
        ],
        "skills": [
            {"name": "monitor_open_positions", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "monitor_open_positions"}},
            {"name": "check_spread_conditions", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "check_spread_conditions"}},
            {"name": "get_position_status", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "get_position_status"}},
            {"name": "alert_orchestrator", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "alert_orchestrator"}},
        ],
    },
    {
        "name": "reporter",
        "model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). Cycle summaries + structured logging.
        "role": "Trade logging, knowledge management, and performance reporting",
        "agent_type": "reporting",
        "workspace": "Reporting",
        "expertise_level": 8,
        "capabilities": ["analytical", "data_analysis", "communication"],
        "mcp_tools": [],
        "prompt_file": "reporter.md",
        "skill_files": [],
        "knowledge_base": [
            "Log every trade to live_trades (67-column schema matching backtest_trades)",
            "Log every decision to trade_decisions with all agent recommendations",
            "Update trade_decisions.outcome when trades close — win/loss/breakeven + outcome_matched_prediction",
            "KnowledgeStore V2: reads from SQLite backtest_setup_performance (308 patterns per pair), JSON for custom data",
            "TradeLogger V2: wraps TradingDB as canonical source, log_trade_unified() and log_decision_unified()",
            "Generate cycle summary: phases completed, timing, decision, outcome",
            "Track what each agent recommended vs what happened — builds data for performance improvement",
            "Market snapshots: capture full indicator state at trade time for later analysis",
            "Performance drift: compare live win rates vs backtest expectations per setup",
        ],
        "skills": [
            {"name": "generate_cycle_summary", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "generate_cycle_summary"}},
            {"name": "log_trade_to_knowledge", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "log_trade_to_knowledge"}},
            {"name": "trade_logger.log_signal", "type": "python_callable",
             "definition": {"module": "Source.trade_logger", "function": "log_signal"}},
            {"name": "trade_logger.log_trade", "type": "python_callable",
             "definition": {"module": "Source.trade_logger", "function": "log_trade"}},
            {"name": "knowledge_store.store_decision", "type": "python_callable",
             "definition": {"module": "Source.knowledge_store", "function": "store_decision"}},
            {"name": "knowledge_store.get_instrument_knowledge", "type": "python_callable",
             "definition": {"module": "Source.knowledge_store", "function": "get_instrument_knowledge"}},
        ],
    },
    {
        "name": "cycle_orchestrator",
        "model": "mlx/CSO",  # Qwen3.5-35B local (port 11502) — agent fleet (was 9B/CRO; flipped 2026-04-26). UI layer, team coordinator — does NOT make trade decisions.
        "role": "TEAM COORDINATOR & USER INTERFACE — manages pipeline, handles Tim's requests, narrates everything, does NOT make trade decisions",
        "agent_type": "coordinator",
        "workspace": "Orchestrator",
        "expertise_level": 8,
        "capabilities": ["management", "communication", "monitoring", "reporting"],
        "mcp_tools": [],
        "prompt_file": "cycle_orchestrator_v4.md",
        "skill_files": ["AGENT_COORDINATION.md", "TEAM_SCHEDULING.md"],
        "knowledge_base": [
            "TEAM COORDINATOR: you manage the trading team workflow and communicate to the user (Tim)",
            "Team: 7 specialists (oanda_data, intelligence, technical_analyst, validator, execution, trade_monitor, reporter)",
            "VALIDATOR is the Trading Authority — it makes ALL trade decisions. You do NOT override or second-guess it.",
            "Your job: narrate what's happening, summarize agent findings, explain decisions in plain English",
            "Communicate at each pipeline step: data collected, TA complete, validator verdict, execution result",
            "Track cycle timing, flag anomalies (slow agents, API errors), monitor team health",
            "Dashboard: update orchestrator container with communication chain and status",
            "Daily context: track trade count, P&L progress, daily target percentage",
            "Sequential execution model: coordinate agents one at a time for memory safety",
            "8 total agents: oanda_data, intelligence, technical_analyst, validator, execution, trade_monitor, reporter, cycle_orchestrator",
        ],
        "skills": [
            {"name": "evaluate_cycle_readiness", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "evaluate_cycle_readiness"}},
            {"name": "make_trade_decision", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "make_trade_decision"}},
            {"name": "get_risk_status", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "get_risk_status"}},
            {"name": "should_escalate_to_llm", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "should_escalate_to_llm"}},
            {"name": "process_operator_command", "type": "python_callable",
             "definition": {"module": "Source.agents.wrappers", "function": "process_operator_command"}},
            {"name": "cycle_orchestration", "type": "python_callable",
             "definition": {"module": "Source.agents.trading_cycle", "function": "run_cycle"}},
            {"name": "quality_control", "type": "python_callable",
             "definition": {"module": "Source.agents.trading_cycle", "function": "assess_quality"}},
            {"name": "trade_decision", "type": "python_callable",
             "definition": {"module": "Source.agents.trading_cycle", "function": "make_trade_decision"}},
        ],
    },
]

# Persistence file for workspace IDs (survives restart)
_STATE_FILE = ".trading_team_workspaces.json"


# ---------------------------------------------------------------------------
# TradingTeamSetup
# ---------------------------------------------------------------------------


class TradingTeamSetup:
    """Creates and manages the Forex Trading Team workspace hierarchy and agent team.

    Uses Jarvis AgentBuilder for dynamic agent creation and AgentRegistry
    for skill registration -- no hardcoded agent definitions.
    """

    def __init__(
        self,
        swarm_handler=None,
        workspace_manager=None,
        agent_registry=None,
        agent_builder=None,
        state_dir: Optional[str] = None,
        team_id: Optional[str] = None,
        tracker=None,
    ):
        """Initialise with existing handler instances or lazy-import them.

        Parameters
        ----------
        swarm_handler : SwarmHandler | None
            Pre-built SwarmHandler instance.  If *None*, lazy-imported from
            ``Handler.handler_swarm``.
        workspace_manager : WorkspaceManager | None
            Pre-built WorkspaceManager.  If *None*, lazy-imported from
            ``Database.database_user``.
        agent_registry : AgentRegistryHandler | None
            Pre-built AgentRegistryHandler.  If *None*, lazy-imported from
            ``Handler.handler_agent_registry``.
        agent_builder : AgentBuilderHandler | None
            Pre-built AgentBuilderHandler.  If *None*, lazy-imported from
            ``Handler.handler_agent_builder``.
        state_dir : str | None
            Directory to store workspace ID persistence file.  Defaults to
            ``Forex Trading Team/`` in the project root.
        """
        self._swarm = swarm_handler
        self._workspace_mgr = workspace_manager
        self._agent_registry = agent_registry
        self._agent_builder = agent_builder
        self._tracker = tracker

        if state_dir is None:
            state_dir = str(
                Path(__file__).resolve().parent.parent.parent  # Forex Trading Team/
            )
        self._state_path = os.path.join(state_dir, _STATE_FILE)

        # Populated after setup_workspaces()
        self._workspace_ids: Dict[str, int] = {}
        # Populated after register_agents()
        self._agent_ids: Dict[str, str] = {}
        # Populated after create_trading_team() — or passed in for per-user teams
        self._team_id: Optional[str] = team_id

    # ------------------------------------------------------------------
    # Lazy accessors
    # ------------------------------------------------------------------

    @property
    def swarm(self):
        """Lazy-load SwarmHandler."""
        if self._swarm is None:
            try:
                from Handler.handler_swarm import SwarmHandler
                self._swarm = SwarmHandler(tracker=self._tracker)
            except ImportError:
                logger.warning("SwarmHandler not available -- running headless")
        return self._swarm

    @property
    def workspace_mgr(self):
        """Lazy-load WorkspaceManager."""
        if self._workspace_mgr is None:
            try:
                from Database.database_user import WorkspaceManager
                self._workspace_mgr = WorkspaceManager()
            except ImportError:
                logger.warning("WorkspaceManager not available -- running headless")
        return self._workspace_mgr

    @property
    def agent_registry(self):
        """Lazy-load AgentRegistryHandler."""
        if self._agent_registry is None:
            try:
                from Handler.handler_agent_registry import AgentRegistryHandler
                self._agent_registry = AgentRegistryHandler()
            except ImportError:
                logger.warning("AgentRegistryHandler not available -- running headless")
        return self._agent_registry

    @property
    def agent_builder(self):
        """Lazy-load AgentBuilderHandler."""
        if self._agent_builder is None:
            try:
                from Handler.handler_agent_builder import AgentBuilderHandler
                self._agent_builder = AgentBuilderHandler()
            except ImportError:
                logger.warning("AgentBuilderHandler not available -- running headless")
        return self._agent_builder

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Persist workspace IDs to JSON for restart recovery."""
        state = {
            "workspace_ids": self._workspace_ids,
            "agent_ids": self._agent_ids,
            "team_id": self._team_id,
        }
        try:
            with open(self._state_path, "w") as fh:
                json.dump(state, fh, indent=2)
            logger.info("Saved team state to %s", self._state_path)
        except OSError as exc:
            logger.error("Failed to save team state: %s", exc)

    def _load_state(self) -> bool:
        """Load previously persisted state.  Returns True if loaded."""
        if not os.path.exists(self._state_path):
            return False
        try:
            with open(self._state_path, "r") as fh:
                state = json.load(fh)
            self._workspace_ids = state.get("workspace_ids", {})
            self._agent_ids = state.get("agent_ids", {})
            self._team_id = state.get("team_id")
            logger.info(
                "Loaded team state: %d workspaces, %d agents",
                len(self._workspace_ids),
                len(self._agent_ids),
            )
            return True
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load team state: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Workspace setup
    # ------------------------------------------------------------------

    def setup_workspaces(self) -> Dict[str, int]:
        """Create parent *Forex Trading Team* workspace and 9 child agent workspaces.

        Idempotent -- if workspaces were already created (persisted state
        exists), returns the cached mapping without creating duplicates.

        Returns
        -------
        dict
            Mapping of agent_name -> workspace_id.  Also includes
            ``"_parent"`` key for the parent workspace.
        """
        # Check for existing state first (idempotent)
        if self._workspace_ids:
            logger.info("Workspaces already set up -- returning cached IDs")
            return dict(self._workspace_ids)

        if self._load_state() and self._workspace_ids:
            logger.info("Workspaces restored from persisted state")
            return dict(self._workspace_ids)

        if self.workspace_mgr is None:
            logger.error("WorkspaceManager unavailable -- cannot create workspaces")
            return {}

        # Create parent workspace
        parent_id = self._create_workspace(
            name=PARENT_WORKSPACE_NAME,
            description=PARENT_WORKSPACE_DESC,
        )
        if parent_id is None:
            logger.error("Failed to create parent workspace")
            return {}

        self._workspace_ids["_parent"] = parent_id
        logger.info("Created parent workspace '%s' (id=%s)", PARENT_WORKSPACE_NAME, parent_id)

        # Create child workspaces -- one per agent spec
        for spec in AGENT_SPECS:
            child_id = self._create_workspace(
                name=spec["workspace"],
                description=f"Agent workspace for {spec['name']}",
                parent_id=parent_id,
            )
            if child_id is not None:
                self._workspace_ids[spec["name"]] = child_id
                logger.info(
                    "Created child workspace '%s' for %s (id=%s)",
                    spec["workspace"],
                    spec["name"],
                    child_id,
                )
            else:
                logger.error("Failed to create workspace for %s", spec["name"])

        # Enable sharing between all workspaces
        self._enable_workspace_sharing()

        # Persist for restart recovery
        self._save_state()

        return dict(self._workspace_ids)

    def _create_workspace(
        self, name: str, description: str, parent_id: Optional[int] = None
    ) -> Optional[int]:
        """Wrapper around WorkspaceManager.create_workspace with error handling."""
        try:
            import asyncio

            metadata = {}
            if parent_id is not None:
                metadata["parent_workspace_id"] = parent_id

            loop = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    ws_id = pool.submit(
                        asyncio.run,
                        self.workspace_mgr.create_workspace(
                            user_id=1,
                            name=name,
                            description=description,
                            metadata=metadata,
                        ),
                    ).result()
            else:
                ws_id = asyncio.run(
                    self.workspace_mgr.create_workspace(
                        user_id=1,
                        name=name,
                        description=description,
                        metadata=metadata,
                    )
                )
            return ws_id
        except Exception as exc:
            logger.error("create_workspace(%s) failed: %s", name, exc)
            return None

    def _enable_workspace_sharing(self) -> None:
        """Enable sharing between all agent workspaces (AGNT-03)."""
        try:
            from Jarvis_Agent_SDK.import_helper import get_workspace_sharing

            sharing = get_workspace_sharing()
            if sharing is None:
                logger.warning("Workspace sharing unavailable -- skipping")
                return

            parent_id = self._workspace_ids.get("_parent")
            if parent_id is None:
                return

            child_ids = [
                wid for key, wid in self._workspace_ids.items() if key != "_parent"
            ]
            for cid in child_ids:
                try:
                    sharing.share_workspace(
                        workspace_id=cid,
                        shared_with_workspace_id=parent_id,
                        permission_level="read",
                    )
                except Exception as exc:
                    logger.warning("share_workspace(%s->%s) failed: %s", cid, parent_id, exc)

            logger.info("Workspace sharing enabled for %d child workspaces", len(child_ids))
        except ImportError:
            logger.warning("Workspace sharing module not available")

    # ------------------------------------------------------------------
    # Agent creation via AgentBuilder
    # ------------------------------------------------------------------

    def _resolve_agent_tools(self) -> Dict[str, List[Callable]]:
        """Resolve AGENT_SPECS skill definitions into actual Python callables.

        For each agent, imports the module/function from the skill definition
        and creates a callable with __name__ matching the skill name, so
        SwarmHandler.execute_tool() can find it by name.
        """
        import importlib
        resolved = {}
        for spec in AGENT_SPECS:
            agent_name = spec["name"]
            callables = []
            for skill in spec.get("skills", []):
                if skill.get("type") != "python_callable":
                    continue
                defn = skill.get("definition", {})
                mod_path = defn.get("module", "")
                func_name = defn.get("function", "")
                if not mod_path or not func_name:
                    continue
                try:
                    mod = importlib.import_module(mod_path)
                    fn = getattr(mod, func_name)
                    # Wrap to set __name__ to skill name (SwarmHandler matches on __name__)
                    skill_name = skill["name"]
                    def make_wrapper(f, name):
                        def wrapper(**kwargs):
                            return f(**kwargs)
                        wrapper.__name__ = name
                        return wrapper
                    callables.append(make_wrapper(fn, skill_name))
                    logger.info("Resolved tool %s -> %s.%s", skill_name, mod_path, func_name)
                except Exception as exc:
                    logger.warning("Could not resolve tool %s: %s", skill.get("name"), exc)
            if callables:
                resolved[agent_name] = callables
        return resolved

    async def _register_all_agents(self, agent_tools: Dict[str, List[Callable]]) -> Dict[str, str]:
        """Register all agents with full metadata pipeline.

        Flow per agent:
        1. AgentBuilder creates config (system_prompt from knowledge_base)
        2. SwarmHandler registers for runtime execution
        3. AgentRegistry stores with FULL metadata (prompt, specialization)
        4. Skills registered: python_callable + mcp_tool + prompt_template
        5. Prompt saved to prompt_registry + agent_prompt_pairings

        Returns mapping of agent_name -> agent_id.
        """
        import asyncio

        agent_ids = {}
        agent_configs = {}  # Cache builder output per agent

        # --- Step 1: Create agents via AgentBuilder (captures system_prompt) ---
        for spec in AGENT_SPECS:
            agent_name = spec["name"]
            if self.agent_builder is not None:
                try:
                    result = self.agent_builder.execute_action(
                        "create_agent",
                        {
                            "name": agent_name,
                            "agent_type": spec["agent_type"],
                            "specialization": {
                                "domain": spec["role"],
                                "expertise_level": spec.get("expertise_level", 8),
                                "capabilities": spec.get("capabilities", []),
                                "tools": spec["mcp_tools"],
                                "knowledge_base": spec.get("knowledge_base", []),
                            },
                            "skills": spec.get("skills", []),
                        },
                    )
                    if isinstance(result, dict) and result.get("success"):
                        config = result.get("agent_config", {})
                        agent_configs[agent_name] = config
                        # Use builder's agent_id if available
                        agent_ids[agent_name] = config.get("agent_id", f"trading_{agent_name}_{int(time.time())}")
                        logger.info(
                            "AgentBuilder created %s with prompt (%d chars), prompt_id=%s",
                            agent_name,
                            len(config.get("system_prompt", "")),
                            config.get("prompt_id"),
                        )
                    else:
                        logger.warning("AgentBuilder create_agent for %s returned: %s", agent_name, result)
                        agent_ids[agent_name] = f"trading_{agent_name}_{int(time.time())}"
                except Exception as exc:
                    logger.warning("AgentBuilder create for %s failed: %s", agent_name, exc)
                    agent_ids[agent_name] = f"trading_{agent_name}_{int(time.time())}"
            else:
                agent_ids[agent_name] = f"trading_{agent_name}_{int(time.time())}"

        # --- Step 2: Register with SwarmHandler (runtime execution) ---
        if self.swarm is not None:
            swarm_tasks = []
            swarm_names = []
            _pending_local_prompts: Dict[str, str] = {}
            for spec in AGENT_SPECS:
                agent_name = spec["name"]
                tools = agent_tools.get(agent_name, [])
                config = agent_configs.get(agent_name, {})
                # Priority: prompt_file + skill_files > AgentBuilder > role string
                instructions = _load_agent_prompt(spec)
                if not instructions or instructions == spec.get("role"):
                    instructions = config.get("system_prompt", spec["role"])
                local_instructions = _load_local_agent_prompt(spec)
                task = self.swarm.register_agent(
                    name=agent_name,
                    instructions=instructions,
                    tools=tools,
                    mcp_tools=spec.get("mcp_tools", []),
                    model=spec.get("model"),  # per-agent model override (None = swarm default)
                )
                swarm_tasks.append(task)
                swarm_names.append(agent_name)
                # Attach lean local-model prompt (used when agent.model is mlx/* or ollama/*)
                if local_instructions:
                    _pending_local_prompts[agent_name] = local_instructions

            try:
                swarm_results = await asyncio.gather(*swarm_tasks, return_exceptions=True)
                for i, result in enumerate(swarm_results):
                    if isinstance(result, Exception):
                        logger.warning("SwarmHandler registration for %s failed: %s", swarm_names[i], result)
                    else:
                        logger.info("Registered %s with SwarmHandler", swarm_names[i])
            except Exception as exc:
                logger.warning("Batch SwarmHandler registration failed: %s", exc)

            # Attach lean local-model prompts to registered agents (picked up
            # by handler_swarm.execute_agent_task when model is mlx/* or ollama/*).
            for _aname, _lprompt in _pending_local_prompts.items():
                _agent = self.swarm.agents.get(_aname)
                if _agent is not None:
                    _agent._instructions_local = _lprompt
                    logger.info("Attached local prompt to %s (%d chars)", _aname, len(_lprompt))

        # --- Step 3: Register with AgentRegistry (full metadata) ---
        if self.agent_registry is not None:
            registry_tasks = []
            registry_names = []
            for spec in AGENT_SPECS:
                agent_name = spec["name"]
                agent_id = agent_ids[agent_name]
                config = agent_configs.get(agent_name, {})

                # Build rich metadata from builder output
                metadata = {
                    "system_prompt": config.get("system_prompt", ""),
                    "prompt_id": config.get("prompt_id"),
                    "domain": spec["role"],
                    "expertise_level": spec.get("expertise_level", 8),
                    "knowledge_base": spec.get("knowledge_base", []),
                    "mcp_tools": spec.get("mcp_tools", []),
                    "workspace": spec.get("workspace", ""),
                    "specialization": config.get("specialization", {}),
                }

                task = self.agent_registry.register_module_agent(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    agent_type=spec["agent_type"],
                    module_name="trading_bot",
                    capabilities=spec.get("capabilities", [spec["role"]]),
                    metadata=metadata,
                    model=spec.get("model"),
                    system_prompt_path=spec.get("prompt_file"),
                )
                registry_tasks.append(task)
                registry_names.append(agent_name)

            try:
                registry_results = await asyncio.gather(*registry_tasks, return_exceptions=True)
                for i, result in enumerate(registry_results):
                    if isinstance(result, Exception):
                        logger.warning("AgentRegistry registration for %s failed: %s", registry_names[i], result)
                    else:
                        logger.info("Registered %s with AgentRegistry (full metadata)", registry_names[i])
            except Exception as exc:
                logger.warning("Batch AgentRegistry registration failed: %s", exc)

        return agent_ids

    def register_agents(self, agent_tools: Dict[str, List[Callable]] = None) -> Dict[str, str]:
        """Create all 7 agents via AgentBuilder and register skills.

        Pipeline: AgentBuilder (prompt) → SwarmHandler (runtime) → AgentRegistry
        (metadata + skills + prompt_template knowledge).

        Parameters
        ----------
        agent_tools : dict | None
            Optional mapping of agent_name -> list of Python callables.
            If provided, callables are passed as tools to the builder.
            If None, auto-resolved from AGENT_SPECS skill definitions.

        Returns
        -------
        dict
            Mapping of agent_name -> agent_id.
        """
        if agent_tools is None:
            agent_tools = {}

        # --- Auto-resolve callable tools from AGENT_SPECS skill definitions ---
        if not agent_tools:
            agent_tools = self._resolve_agent_tools()

        # --- Batch register: AgentBuilder + SwarmHandler + AgentRegistry ---
        # _register_all_agents now handles AgentBuilder creation internally
        # (no separate create loop needed — was duplicating work before)
        import asyncio
        try:
            batch_agent_ids = asyncio.run(self._register_all_agents(agent_tools))
            self._agent_ids.update(batch_agent_ids)
            logger.info("Batch registered %d agents successfully", len(batch_agent_ids))
        except Exception as exc:
            logger.error("Batch agent registration failed: %s", exc)
            logger.info("Falling back to individual agent registration...")
            self._register_agents_individually(agent_tools)

        # --- Register skills (python_callable + mcp_tool + prompt_template) ---
        for spec in AGENT_SPECS:
            agent_name = spec["name"]
            agent_id = self._agent_ids.get(agent_name)
            if agent_id is None:
                continue

            # Register python_callable and mcp_tool skills
            self._register_agent_skills(agent_id, spec)

            # Register prompt_template skills from knowledge_base
            self._register_knowledge_as_skills(agent_id, agent_name, spec)

            # Assign agent to its workspace (with quick timeout)
            ws_id = self._workspace_ids.get(agent_name)
            if ws_id is not None and self.swarm is not None:
                try:
                    self.swarm.set_workspace(ws_id)
                except Exception as exc:
                    logger.warning("set_workspace for %s failed: %s", agent_name, exc)

        self._save_state()
        return dict(self._agent_ids)

    def _register_knowledge_as_skills(
        self, agent_id: str, agent_name: str, spec: Dict[str, Any]
    ) -> None:
        """Register knowledge_base entries as prompt_template skills.

        Each knowledge_base string becomes a prompt_template skill so the
        agent's domain knowledge is queryable via get_agent_skills() and
        survives team duplication.
        """
        if self.agent_registry is None:
            return

        knowledge_base = spec.get("knowledge_base", [])
        if not knowledge_base:
            return

        # Register as a single consolidated prompt_template skill
        try:
            import asyncio
            definition = {
                "agent_name": agent_name,
                "domain": spec.get("role", ""),
                "expertise_level": spec.get("expertise_level", 8),
                "knowledge_items": knowledge_base,
                "item_count": len(knowledge_base),
            }
            asyncio.run(
                self.agent_registry.register_skill(
                    agent_id=agent_id,
                    skill_name=f"{agent_name}_domain_knowledge",
                    skill_type="prompt_template",
                    definition_json=json.dumps(definition),
                )
            )
            logger.info(
                "Registered prompt_template skill for %s (%d knowledge items)",
                agent_name,
                len(knowledge_base),
            )
        except Exception as exc:
            logger.warning(
                "prompt_template skill registration for %s failed: %s",
                agent_name,
                exc,
            )

    def _register_agents_individually(self, agent_tools: Dict[str, List[Callable]]) -> None:
        """Fallback: register agents one by one (slower due to DB locks)."""
        import asyncio
        
        for spec in AGENT_SPECS:
            agent_name = spec["name"]
            tools = agent_tools.get(agent_name, [])
            agent_id = f"trading_{agent_name}_{int(time.time())}"

            # Register with SwarmHandler
            if self.swarm is not None:
                try:
                    asyncio.run(
                        self.swarm.register_agent(
                            name=agent_name,
                            instructions=spec["role"],
                            tools=tools,
                            mcp_tools=spec.get("mcp_tools", []),
                        )
                    )
                    logger.info("Registered %s with SwarmHandler", agent_name)
                except Exception as exc:
                    logger.warning("SwarmHandler registration for %s failed: %s", agent_name, exc)

            # Register with AgentRegistry  
            if self.agent_registry is not None:
                try:
                    asyncio.run(
                        self.agent_registry.register_module_agent(
                            agent_id=agent_id,
                            agent_name=agent_name,
                            agent_type=spec["agent_type"],
                            module_name="trading_bot",
                            capabilities=[spec["role"]],
                        )
                    )
                    logger.info("Registered %s with AgentRegistry", agent_name)
                except Exception as exc:
                    logger.warning("AgentRegistry registration for %s failed: %s", agent_name, exc)

            self._agent_ids[agent_name] = agent_id

    def _register_agent_skills(self, agent_id: str, spec: Dict[str, Any]) -> None:
        """Register Source computation modules as versioned skills."""
        if self.agent_registry is None:
            logger.warning("AgentRegistry unavailable -- skipping skill registration")
            return

        for skill_def in spec.get("skills", []):
            try:
                import asyncio

                asyncio.run(
                    self.agent_registry.register_skill(
                        agent_id=agent_id,
                        skill_name=skill_def["name"],
                        skill_type=skill_def["type"],
                        definition_json=json.dumps(skill_def["definition"]),
                    )
                )
                logger.info(
                    "Registered skill %s (%s) for agent %s",
                    skill_def["name"],
                    skill_def["type"],
                    agent_id,
                )
            except Exception as exc:
                logger.warning(
                    "register_skill(%s) for %s failed: %s",
                    skill_def["name"],
                    agent_id,
                    exc,
                )

    # ------------------------------------------------------------------
    # Team creation
    # ------------------------------------------------------------------

    def create_trading_team(self) -> Dict[str, Any]:
        """Create the Trading Team via SwarmHandler AND AgentRegistry.

        Registers the team in both systems so it can be queried and
        duplicated via AgentRegistry.get_team_members().

        Returns
        -------
        dict
            Team details including team_id and member list.
        """
        member_names = [s["name"] for s in AGENT_SPECS]

        # Register in SwarmHandler (runtime coordination)
        if self.swarm is not None:
            try:
                import asyncio
                result = asyncio.run(
                    self.swarm.create_team(
                        name="Trading Team",
                        members=member_names,
                    )
                )
                if hasattr(result, "data") and result.data:
                    self._team_id = result.data.get("team_id")
                elif isinstance(result, dict):
                    self._team_id = result.get("team_id")
                logger.info("Created Trading Team in SwarmHandler (id=%s)", self._team_id)
            except Exception as exc:
                logger.error("SwarmHandler create_team failed: %s", exc)

        # Register in AgentRegistry (persistence + performance tracking)
        if self.agent_registry is not None and self._agent_ids:
            try:
                import asyncio
                agent_id_list = [
                    self._agent_ids[name]
                    for name in member_names
                    if name in self._agent_ids
                ]
                result = asyncio.run(
                    self.agent_registry.create_team(
                        team_name="Trading Team",
                        agent_ids=agent_id_list,
                    )
                )
                if hasattr(result, "data") and result.data:
                    registry_team_id = result.data.get("team_id")
                    # Use registry team_id if swarm didn't produce one
                    if not self._team_id:
                        self._team_id = registry_team_id
                    logger.info(
                        "Created Trading Team in AgentRegistry (id=%s, %d agents)",
                        registry_team_id,
                        len(agent_id_list),
                    )
            except Exception as exc:
                logger.warning("AgentRegistry create_team failed: %s", exc)

        self._save_state()
        return {
            "team_id": self._team_id,
            "members": member_names,
        }

    # ------------------------------------------------------------------
    # Load existing team from registry (avoid recreation)
    # ------------------------------------------------------------------

    def load_existing_team(self) -> Optional[Dict[str, Any]]:
        """Check AgentRegistry for an existing active trading_bot team.

        If self._team_id is set (passed via constructor), query directly
        by team_id for fast, user-scoped lookup. Otherwise fall back to
        module_name scan.

        Returns dict or None.
        """
        # Fast path: direct DB query by team_id (no registry handler needed)
        if self._team_id:
            return self._load_team_by_id(self._team_id)

        if self.agent_registry is None:
            return None

        try:
            import asyncio

            result = asyncio.run(
                self.agent_registry.list_agents(module_name="trading_bot")
            )
            if not (hasattr(result, "data") and result.data):
                return None

            agents = result.data if isinstance(result.data, list) else result.data.get("agents", []) if isinstance(result.data, dict) else []
            if not agents:
                return None

            expected_names = {s["name"] for s in AGENT_SPECS}
            found = {}
            for agent in agents:
                name = agent.get("agent_name")
                status = agent.get("status", "")
                if name in expected_names and status == "active":
                    existing = found.get(name)
                    if existing is None or agent.get("created_at", 0) > existing.get("created_at", 0):
                        found[name] = agent

            if set(found.keys()) != expected_names:
                missing = expected_names - set(found.keys())
                logger.info("Existing team incomplete — missing: %s", missing)
                return None

            for name, agent in found.items():
                agent_id = agent.get("agent_id")
                skills_result = asyncio.run(
                    self.agent_registry.get_agent_skills(agent_id)
                )
                if hasattr(skills_result, "data") and skills_result.data:
                    skills = skills_result.data.get("skills", [])
                    if not skills:
                        logger.info("Agent %s has no skills — recreating.", name)
                        return None
                else:
                    return None

            for name, agent in found.items():
                self._agent_ids[name] = agent["agent_id"]

            team_id = found[list(found.keys())[0]].get("team_id")
            if team_id:
                self._team_id = team_id

            logger.info("Loaded existing team: %d agents, team=%s", len(found), self._team_id)
            return {
                "agent_ids": dict(self._agent_ids),
                "team_id": self._team_id,
                "loaded_from_registry": True,
            }
        except Exception as exc:
            logger.warning("load_existing_team registry scan failed: %s", exc)
            return None

    def _load_team_by_id(self, team_id: str) -> Optional[Dict[str, Any]]:
        """Direct DB lookup by team_id — fast, no async, user-scoped."""
        try:
            import sqlite3
            from db_pool import get_agents as _get_agents
            conn = _get_agents()
            conn.row_factory = sqlite3.Row

            agents = conn.execute(
                "SELECT id, agent_name, model_preference, vault_path FROM agent_registry WHERE team_id=? AND status='active'",
                (team_id,),
            ).fetchall()

            expected_names = {s["name"] for s in AGENT_SPECS}
            found = {row["agent_name"]: row["id"] for row in agents if row["agent_name"] in expected_names}
            # Store full registry data for swarm loading
            self._registry_data = {}
            for row in agents:
                if row["agent_name"] in expected_names:
                    self._registry_data[row["agent_name"]] = {
                        "agent_id": row["id"],
                        "model": row["model_preference"],
                        "system_prompt_path": row["vault_path"],
                        "metadata": None,
                    }

            if set(found.keys()) != expected_names:
                missing = expected_names - set(found.keys())
                logger.info("Team %s incomplete — missing: %s", team_id[:12], missing)
                return None

            # Verify skills exist
            for name, agent_id in found.items():
                count = conn.execute(
                    "SELECT COUNT(*) FROM agent_skills WHERE agent_id=?", (agent_id,)
                ).fetchone()[0]
                if count == 0:
                    logger.info("Agent %s has no skills in team %s", name, team_id[:12])
                    return None

            self._agent_ids = dict(found)
            self._team_id = team_id
            logger.info("Loaded team %s: %d agents (direct DB)", team_id[:12], len(found))
            return {
                "agent_ids": dict(self._agent_ids),
                "team_id": self._team_id,
                "loaded_from_registry": True,
            }
        except Exception as exc:
            logger.warning("_load_team_by_id failed: %s", exc)
            return None

        except Exception as exc:
            logger.warning("load_existing_team failed: %s", exc)
            return None

    def clone_team(
        self, source_team_id: str, new_workspace_id: str
    ) -> Optional[Dict[str, Any]]:
        """Duplicate a team's agents (with prompts + skills) into a new workspace.

        Parameters
        ----------
        source_team_id : str
            The team_id to clone from.
        new_workspace_id : str
            Target workspace for the cloned agents.

        Returns
        -------
        dict or None
            New team info with cloned agent_ids.
        """
        if self.agent_registry is None:
            logger.error("AgentRegistry unavailable — cannot clone team")
            return None

        try:
            import asyncio

            # Get source team members
            members_result = asyncio.run(
                self.agent_registry.get_team_members(source_team_id)
            )
            if not (hasattr(members_result, "data") and members_result.data):
                logger.error("Could not find source team %s", source_team_id)
                return None

            members = members_result.data.get("members", [])
            new_agent_ids = {}

            for member in members:
                old_id = member.get("agent_id")
                agent_name = member.get("agent_name")
                new_id = f"{agent_name}_{new_workspace_id}_{int(time.time())}"

                # Parse metadata (may be JSON string)
                metadata = member.get("metadata", {})
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (json.JSONDecodeError, TypeError):
                        metadata = {}

                # Update workspace in metadata
                metadata["workspace_id"] = new_workspace_id
                metadata["cloned_from"] = old_id

                # Register cloned agent
                asyncio.run(
                    self.agent_registry.register_module_agent(
                        agent_id=new_id,
                        agent_name=agent_name,
                        agent_type=member.get("agent_type", "system"),
                        module_name="trading_bot",
                        capabilities=json.loads(member.get("capabilities", "[]"))
                        if isinstance(member.get("capabilities"), str)
                        else member.get("capabilities", []),
                        metadata=metadata,
                    )
                )

                # Copy skills from source agent
                skills_result = asyncio.run(
                    self.agent_registry.get_agent_skills(old_id)
                )
                if hasattr(skills_result, "data") and skills_result.data:
                    for skill in skills_result.data.get("skills", []):
                        asyncio.run(
                            self.agent_registry.register_skill(
                                agent_id=new_id,
                                skill_name=skill["skill_name"],
                                skill_type=skill["skill_type"],
                                definition_json=skill["definition_json"],
                            )
                        )

                new_agent_ids[agent_name] = new_id
                logger.info("Cloned %s: %s → %s", agent_name, old_id, new_id)

            # Create new team
            new_team_result = asyncio.run(
                self.agent_registry.create_team(
                    team_name=f"Trading Team ({new_workspace_id})",
                    agent_ids=list(new_agent_ids.values()),
                )
            )
            new_team_id = None
            if hasattr(new_team_result, "data") and new_team_result.data:
                new_team_id = new_team_result.data.get("team_id")

            logger.info(
                "Cloned team %s → %s (%d agents)",
                source_team_id,
                new_team_id,
                len(new_agent_ids),
            )
            return {
                "team_id": new_team_id,
                "agent_ids": new_agent_ids,
                "cloned_from": source_team_id,
                "workspace_id": new_workspace_id,
            }

        except Exception as exc:
            logger.error("clone_team failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Instrument-portable team setup
    # ------------------------------------------------------------------

    @staticmethod
    def load_workspace_config() -> Optional[Dict[str, Any]]:
        """Load pre-created workspace config from Config/workspace_config.json.

        The workspace is created once by scripts/create_trading_workspace.py
        (same pattern as claude_interface.py), not at runtime.  All agents,
        tasks, and tools live in one workspace.

        Returns
        -------
        dict or None
            Workspace config with workspace_id, workspace_name, instruments.
        """
        config_path = os.path.join(
            Path(__file__).resolve().parent.parent.parent,  # Forex Trading Team/
            "Config", "workspace_config.json",
        )
        if not os.path.exists(config_path):
            logger.warning("No workspace config at %s -- run scripts/create_trading_workspace.py first", config_path)
            return None
        try:
            with open(config_path) as fh:
                config = json.load(fh)
            logger.info(
                "Loaded workspace config: %s (%s)",
                config.get("workspace_name", "unknown"),
                config.get("workspace_id", "unknown"),
            )
            return config
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to load workspace config: %s", exc)
            return None

    def setup_team(
        self,
        instruments: Optional[List[str]] = None,
        agent_tools: Optional[Dict[str, list]] = None,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set up the complete trading team for the given instruments.

        If *workspace_id* is provided (or found in workspace_config.json),
        uses the existing workspace instead of creating new ones.  This
        avoids triggering the heavy WorkspaceManager init chain.

        Parameters
        ----------
        instruments : list[str] | None
            Instrument strings to trade.  Defaults to config or ``["EUR_USD"]``.
        agent_tools : dict | None
            Optional mapping of agent_name -> list of Python callables.
        workspace_id : str | None
            Pre-created workspace ID.  If *None*, attempts to load from
            ``Config/workspace_config.json``.

        Returns
        -------
        dict
            Setup result with workspace_id, agent_ids, team_id, instruments.
        """
        # Try to load pre-created workspace config
        ws_config = self.load_workspace_config()
        if workspace_id is None and ws_config:
            workspace_id = ws_config.get("workspace_id") or ws_config.get("parent_workspace_id")
            if instruments is None:
                instruments = ws_config.get("instruments", ["EUR_USD"])

        if instruments is None:
            instruments = ["EUR_USD"]

        # --- CHECK REGISTRY FIRST: reuse existing team if available ---
        existing = self.load_existing_team()
        if existing:
            logger.info("Reusing existing team from AgentRegistry — skipping creation")
            # Still need to load into SwarmHandler for runtime
            if self.swarm is not None:
                # Load agents into SwarmHandler with full system prompts and MCP tools.
                # NO Python callables — agents use LLM + MCP path exclusively.
                # execute_tool() will fall through to _execute_mcp_tool() for handler calls.
                from Handler.handler_swarm import SwarmAgent

                # Load full agent config from registry (model, prompt, metadata)
                _agent_prompts = {}
                _agent_models = {}
                _agent_mcps = {}
                # Use _registry_data if populated by _load_team_by_id
                _reg = getattr(self, '_registry_data', None) or {}
                if not _reg:
                    try:
                        import sqlite3 as _sql3
                        _bdb = Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "agents.db"
                        _bconn = _sql3.connect(str(_bdb))
                        _rows = _bconn.execute(
                            "SELECT agent_name, model, system_prompt_path, metadata FROM agent_registry WHERE team_id=?",
                            (self._team_id,)
                        ).fetchall()
                        _bconn.close()
                        for _aname, _model, _prompt_path, _ameta in _rows:
                            _reg[_aname] = {"model": _model, "system_prompt_path": _prompt_path, "metadata": _ameta}
                    except Exception as _pe:
                        logger.warning("Could not load agent config from registry: %s", _pe)

                for _aname, _rdata in _reg.items():
                    _ameta = _rdata.get("metadata")
                    if isinstance(_ameta, str):
                        try:
                            import json as _json
                            _ameta = _json.loads(_ameta)
                        except Exception:
                            _ameta = {}
                    elif not isinstance(_ameta, dict):
                        _ameta = {}
                    _agent_prompts[_aname] = _ameta.get("system_prompt", "")
                    if _rdata.get("model"):
                        _agent_models[_aname] = _rdata["model"]
                    if _ameta.get("mcp_tools"):
                        _agent_mcps[_aname] = _ameta["mcp_tools"]

                logger.info("Loaded config for %d agents from registry (models: %s)", len(_reg), list(_agent_models.keys()))

                agent_tools_resolved = agent_tools or self._resolve_agent_tools()
                for spec in AGENT_SPECS:
                    agent_name = spec["name"]
                    # Priority: prompt_file + skill_files > DB-stored prompt > role string
                    system_prompt = _load_agent_prompt(spec)
                    if not system_prompt or system_prompt == spec.get("role"):
                        system_prompt = _agent_prompts.get(agent_name, spec["role"])
                    # Model: registry > AGENT_SPECS
                    model = _agent_models.get(agent_name, spec.get("model"))
                    # MCP tools: registry > AGENT_SPECS
                    mcp_tools = _agent_mcps.get(agent_name, spec.get("mcp_tools", []))
                    has_mcps = bool(mcp_tools)
                    tools = [] if has_mcps else agent_tools_resolved.get(agent_name, [])
                    try:
                        agent = SwarmAgent(
                            name=agent_name,
                            instructions=system_prompt,
                            tools=tools,
                            mcp_tools=mcp_tools,
                            model=model,
                        )
                        # Attach lean local-model prompt (picked up in handler_swarm
                        # when model is mlx/* or ollama/*)
                        _local_prompt = _load_local_agent_prompt(spec)
                        if _local_prompt:
                            agent._instructions_local = _local_prompt
                        self.swarm.agents[agent_name] = agent
                        mode = "LLM+MCP" if has_mcps else f"callables({len(tools)})"
                        logger.info(
                            "Loaded agent %s into SwarmHandler: model=%s, %s, prompt=%d chars%s",
                            agent_name, model, mode, len(system_prompt),
                            f" (+ local prompt {len(_local_prompt)} chars)" if _local_prompt else "",
                        )
                    except Exception as exc:
                        logger.warning("SwarmHandler load for %s failed: %s", agent_name, exc)

            # Load workspace assignments from workspaces.db (workspace_agent_assignments,
            # workspaces) and agents.db (agent_registry subquery)
            try:
                import sqlite3
                from db_connection import get_db as _get_db
                _workspaces_db = Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "workspaces.db"
                _agents_db = Path(__file__).parent.parent.parent.parent / "Database" / "v2" / "agents.db"
                if _workspaces_db.exists():
                    # Use context manager (not pool) because ATTACH mutates session state
                    with _get_db(str(_workspaces_db), timeout=10) as _conn:
                        _conn.execute(f"ATTACH DATABASE '{_agents_db}' AS agents_db")
                        _assignments = _conn.execute(
                            """SELECT waa.agent_name, waa.workspace_id, w.name, w.parent_workspace_id
                               FROM workspace_agent_assignments waa
                               JOIN workspaces w ON w.id = waa.workspace_id
                               WHERE waa.agent_id IN (
                                   SELECT id FROM agents_db.agent_registry WHERE team_id=?
                               )""",
                            (self._team_id,)
                        ).fetchall()
                        for agent_name, ws_id, ws_name, parent_id in _assignments:
                            self._workspace_ids[agent_name] = ws_id
                            if parent_id:
                                self._workspace_ids["_parent"] = parent_id
                    logger.info(
                        "Loaded %d workspace assignments from workspaces.db",
                        len(_assignments),
                    )
            except Exception as exc:
                logger.warning("Could not load workspace assignments: %s", exc)

            # Set workspace on swarm and load MCP configuration
            parent_ws = self._workspace_ids.get("_parent")
            if parent_ws and self.swarm is not None:
                try:
                    self.swarm.set_workspace(parent_ws)
                    logger.info("Set swarm workspace to parent: %s", parent_ws)
                    # Load workspace MCP config so agents can route to handlers
                    import asyncio as _aio
                    try:
                        _loop = _aio.get_running_loop()
                    except RuntimeError:
                        _loop = None
                    if _loop and _loop.is_running():
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as _pool:
                            _pool.submit(_aio.run, self.swarm.load_workspace_mcp()).result()
                    else:
                        _aio.run(self.swarm.load_workspace_mcp())
                    logger.info("Loaded workspace MCP configuration")
                except Exception as exc:
                    logger.warning("set_workspace/load_workspace_mcp failed: %s", exc)

            if workspace_id:
                self._workspace_ids["_workspace"] = workspace_id

            self._save_state()
            return {
                "workspace_id": workspace_id or parent_ws,
                "workspace_ids": dict(self._workspace_ids),
                "agent_ids": dict(self._agent_ids),
                "team_id": self._team_id,
                "instruments": instruments,
                "loaded_from_registry": True,
            }

        # --- No existing team — create fresh ---
        if workspace_id:
            # Fast path: use existing workspace, skip WorkspaceManager entirely
            logger.info("Using pre-created workspace: %s", workspace_id)
            self._workspace_ids["_workspace"] = workspace_id
            for spec in AGENT_SPECS:
                self._workspace_ids[spec["name"]] = workspace_id

            # Register agents (AgentBuilder → SwarmHandler → AgentRegistry)
            agent_ids = self.register_agents(agent_tools=agent_tools)

            # Create trading team (SwarmHandler + AgentRegistry)
            team_info = self.create_trading_team()

            self._save_state()

            return {
                "workspace_id": workspace_id,
                "workspace_ids": dict(self._workspace_ids),
                "agent_ids": dict(self._agent_ids),
                "team_id": self._team_id,
                "instruments": instruments,
            }

        # Legacy path: create workspaces at runtime (slow, may hang)
        logger.warning(
            "No pre-created workspace -- falling back to runtime creation. "
            "Run scripts/create_trading_workspace.py to avoid this."
        )
        workspace_ids = self.setup_workspaces()
        agent_ids = self.register_agents(agent_tools=agent_tools)
        team_info = self.create_trading_team()

        instrument_workspaces: Dict[str, Optional[int]] = {}
        parent_id = workspace_ids.get("_parent")
        for instrument in instruments:
            ws_name = f"trading_{instrument.lower()}"
            child_id = self._create_workspace(
                name=ws_name,
                description=f"Instrument workspace for {instrument}",
                parent_id=parent_id,
            )
            instrument_workspaces[instrument] = child_id

        self._workspace_ids["_instruments"] = instrument_workspaces  # type: ignore[assignment]
        self._save_state()

        return {
            "workspace_ids": dict(self._workspace_ids),
            "agent_ids": dict(self._agent_ids),
            "team_id": self._team_id,
            "instruments": instruments,
            "instrument_workspaces": instrument_workspaces,
        }

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_team_status(self) -> Dict[str, Any]:
        """Return composite status of the trading team.

        Returns
        -------
        dict
            workspace_ids, agent_ids, team_id, and workspace_sharing_status.
        """
        return {
            "workspace_ids": dict(self._workspace_ids),
            "agent_ids": dict(self._agent_ids),
            "team_id": self._team_id,
            "agent_count": len(self._agent_ids),
            "workspace_count": len(self._workspace_ids),
            "state_file": self._state_path,
            "state_file_exists": os.path.exists(self._state_path),
        }

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def teardown(self) -> None:
        """Graceful cleanup for testing/restart."""
        self._workspace_ids.clear()
        self._agent_ids.clear()
        self._team_id = None
        if os.path.exists(self._state_path):
            try:
                os.remove(self._state_path)
                logger.info("Removed state file %s", self._state_path)
            except OSError as exc:
                logger.warning("Failed to remove state file: %s", exc)
