#!/usr/bin/env python3
"""
Lightweight Registrar — Direct SQLite agent registration bypassing heavy handler chain.

Writes to the SAME tables in v2/agents.db that the full handlers use:
  - agent_registry (agents)
  - agent_skills (skills: python_callable, mcp_tool, prompt_template)
  - prompt_registry + prompt_versions (generated prompts)
  - agent_prompt_pairings (links agents → prompts)

When the unified DB fix lands, swap these calls back to the full handlers.
The data is identical — same schema, same DB.
"""

import ast
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from db_pool import get_agents

# ---------------------------------------------------------------------------
# Prompt generation (ported from handler_agent_builder._generate_fallback_template_prompt)
# ---------------------------------------------------------------------------

EXPERTISE_LEVELS = {
    10: "world-leading authority with groundbreaking expertise",
    9: "distinguished expert with exceptional mastery",
    8: "highly specialized expert with comprehensive knowledge",
    7: "advanced specialist with substantial experience",
    6: "proficient professional with thorough understanding",
    5: "competent practitioner with solid practical knowledge",
}

AGENT_TYPE_INSTRUCTIONS = {
    "data_collection": """As a data collection specialist, you should:
1. Fetch data reliably with proper error handling and retry logic
2. Validate data freshness and completeness before passing downstream
3. Normalize data formats for consistent consumption by other agents
4. Log data quality metrics (staleness, gaps, anomalies)
5. Respect API rate limits and connection management""",

    "analysis": """As an analysis specialist, you should:
1. Apply rigorous analytical methods appropriate to market analysis
2. Consider multiple timeframes and confluence of signals
3. Quantify confidence in your assessments (never just 'bullish' or 'bearish')
4. Document which indicators and patterns drove your conclusion
5. Flag when conditions are ambiguous or contradictory""",

    "validation": """As a validation specialist, you should:
1. Apply systematic checks against historical evidence
2. Never approve a trade without quantified supporting data
3. Document rejection reasons with specific metrics
4. Detect performance drift by comparing live results to backtest baselines
5. Err on the side of caution — rejected good trades are better than approved bad ones""",

    "execution": """As an execution specialist, you should:
1. Execute orders with precise sizing and risk management
2. Monitor positions against all exit rules continuously
3. Never exceed position size limits or correlated exposure limits
4. Log every execution detail for audit trail
5. Handle partial fills and slippage gracefully""",

    "reporting": """As a reporting specialist, you should:
1. Log complete trade data with all 67 forensic fields
2. Track what each agent recommended vs actual outcome
3. Identify patterns in wins and losses for system improvement
4. Generate clear summaries for both humans and other agents
5. Maintain knowledge store with distilled insights""",

    "coordinator": """As a coordination specialist, you should:
1. Orchestrate agents in correct sequence with proper data flow
2. Handle agent failures gracefully — log and continue
3. Make final trade decisions by weighing all agent inputs
4. Maintain cycle timing and prevent runaway execution
5. Escalate to human when confidence is low or signals conflict""",
}


SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "Skills"

# Map MCP handler names → SKILL.md files
MCP_SKILL_FILES = {
    "handler_oanda": "OANDA_MCP.md",
    "handler_news_info": "NEWS_MCP.md",
    "handler_weather": "WEATHER_MCP.md",
    "handler_wolfram": "WOLFRAM_MCP.md",
    "handler_data_validator": "DATA_VALIDATOR_MCP.md",
}

# Agent → which skill files to include (beyond their MCP tools)
AGENT_EXTRA_SKILLS = {
    "cycle_orchestrator": ["AGENT_COORDINATION.md"],
    "validator": ["DATA_VALIDATOR_MCP.md"],
    "execution": ["OANDA_MCP.md", "DATA_VALIDATOR_MCP.md"],
    "reporter": ["DATA_VALIDATOR_MCP.md"],
    "technical_analyst": ["DATA_VALIDATOR_MCP.md"],  # for validate_trade_setup, get_loss_patterns
}


def _load_skill_file(filename: str) -> str:
    """Load a SKILL.md file from the Skills directory."""
    path = SKILLS_DIR / filename
    if path.exists():
        return path.read_text()
    return ""


def _get_skill_content_for_agent(spec: Dict[str, Any]) -> str:
    """Collect all relevant SKILL.md content for an agent."""
    files_to_load = set()
    
    # Add skill files for each MCP the agent uses
    for mcp in spec.get("mcp_tools", []):
        if mcp in MCP_SKILL_FILES:
            files_to_load.add(MCP_SKILL_FILES[mcp])
    
    # Add extra skill files for this agent
    for f in AGENT_EXTRA_SKILLS.get(spec["name"], []):
        files_to_load.add(f)
    
    sections = []
    for filename in sorted(files_to_load):
        content = _load_skill_file(filename)
        if content:
            sections.append(f"--- BEGIN SKILL: {filename} ---\n{content}\n--- END SKILL ---")
    
    return "\n\n".join(sections)


def generate_system_prompt(spec: Dict[str, Any]) -> str:
    """Generate a system prompt from an AGENT_SPEC dict.
    
    Includes:
    1. Agent identity and role
    2. Knowledge base (domain expertise)
    3. Tool inventory
    4. Full SKILL.md content for each MCP (how to call, parameters, responses)
    5. Agent-type-specific instructions
    6. Coordination context
    """
    name = spec["name"]
    role = spec["role"]
    agent_type = spec["agent_type"]
    expertise = spec.get("expertise_level", 7)
    capabilities = spec.get("capabilities", [])
    knowledge_base = spec.get("knowledge_base", [])
    mcp_tools = spec.get("mcp_tools", [])
    skills = spec.get("skills", [])

    expertise_desc = EXPERTISE_LEVELS.get(expertise, f"professional with level {expertise}/10 expertise")
    type_instructions = AGENT_TYPE_INSTRUCTIONS.get(agent_type, AGENT_TYPE_INSTRUCTIONS.get("analysis", ""))

    # Format sections
    caps_text = "\n".join(f"- {c}" for c in capabilities)
    kb_text = "\n".join(f"- {item}" for item in knowledge_base)
    
    tools_list = []
    for s in skills:
        tools_list.append(f"- {s['name']} ({s['type']})")
    for m in mcp_tools:
        tools_list.append(f"- {m} (MCP handler)")
    tools_text = "\n".join(tools_list) if tools_list else "- No specialized tools assigned"

    # List skill files this agent can load on-demand (NOT embedded in prompt)
    skill_files = set()
    for mcp in mcp_tools:
        if mcp in MCP_SKILL_FILES:
            skill_files.add(MCP_SKILL_FILES[mcp])
    for f in AGENT_EXTRA_SKILLS.get(name, []):
        skill_files.add(f)
    skill_files.add("AGENT_COORDINATION.md")  # all agents get coordination reference
    
    skills_ref = "\n".join(f"- {f}" for f in sorted(skill_files))

    prompt = f"""You are {name}, a specialized {agent_type} AI agent in a 7-agent forex trading team.

Role: {role}

Expertise Level: {expertise}/10 ({expertise_desc})

## CAPABILITIES
{caps_text}

## DOMAIN KNOWLEDGE
{kb_text}

## TOOLS
{tools_text}

## SKILL FILES (loaded on-demand at runtime, NOT in this prompt)
The following skill files contain detailed tool documentation — exact function signatures,
parameter types, response schemas, usage patterns, and coordination rules.
They are loaded into your context when you activate, so you will have full docs available.
{skills_ref}

## OPERATING INSTRUCTIONS
{type_instructions}

## OUTPUT REQUIREMENTS
- Always provide outputs that are directly actionable
- Include specific numbers, thresholds, and confidence levels
- When uncertain, quantify the uncertainty (e.g., "65% confidence" not "somewhat confident")
- Your outputs feed directly into other agents' decision-making pipelines
- Post results to the task thread using CommentProtocol format
- Read prior posts in the thread before making your assessment"""

    return prompt


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    return get_agents()  # pool manages lifecycle — do not close


def register_agent(
    agent_id: str,
    spec: Dict[str, Any],
    system_prompt: str,
    module_name: str = "trading_bot",
) -> Dict[str, Any]:
    """Register an agent in agent_registry with full metadata."""
    conn = _get_conn()
    c = conn.cursor()

    metadata = {
        "system_prompt": system_prompt,
        "specialization": {
            "domain": spec["role"],
            "expertise_level": spec.get("expertise_level", 7),
            "capabilities": spec.get("capabilities", []),
        },
        "knowledge_base": spec.get("knowledge_base", []),
        "mcp_tools": spec.get("mcp_tools", []),
        "workspace": spec.get("workspace", ""),
    }

    # Upsert: update if exists, insert if not
    # agent_registry schema: id, agent_name, agent_type, capabilities, status,
    #   created_at, updated_at, model_preference, vault_path, team_id
    c.execute("SELECT id FROM agent_registry WHERE id = ? OR agent_name = ?", (agent_id, spec["name"]))
    existing = c.fetchone()

    if existing:
        c.execute("""
            UPDATE agent_registry
            SET agent_name=?, agent_type=?, capabilities=?,
                updated_at=?, status='active', model_preference=?
            WHERE id=?
        """, (
            spec["name"], spec["agent_type"],
            json.dumps(spec.get("capabilities", [])),
            time.time(), spec.get("model", ""),
            existing[0],
        ))
    else:
        c.execute("""
            INSERT INTO agent_registry
            (id, agent_name, agent_type, capabilities,
             created_at, updated_at, status, model_preference)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
        """, (
            agent_id, spec["name"], spec["agent_type"],
            json.dumps(spec.get("capabilities", [])),
            time.time(), time.time(), spec.get("model", ""),
        ))

    conn.commit()
    return {"agent_id": agent_id, "status": "registered"}


def register_prompt(
    agent_id: str,
    agent_name: str,
    system_prompt: str,
) -> str:
    """Save prompt to prompt_registry + prompt_versions + agent_prompt_pairings."""
    conn = _get_conn()
    c = conn.cursor()
    prompt_id = f"trading_{agent_name}_{int(time.time())}"

    # prompt_registry — schema: id, prompt_id, name, description, current_version,
    #   created_at, updated_at, author, prompt_family, metadata, is_active
    reg_id = str(uuid.uuid4())
    c.execute("SELECT prompt_id FROM prompt_registry WHERE prompt_id = ?", (prompt_id,))
    if not c.fetchone():
        c.execute("""
            INSERT INTO prompt_registry
            (id, prompt_id, name, description, current_version,
             created_at, updated_at, author, prompt_family, metadata, is_active)
            VALUES (?, ?, ?, ?, '1.0', ?, ?, 'trading_bot_setup', 'trading_bot', ?, 1)
        """, (
            reg_id, prompt_id,
            f"trading_{agent_name}",
            f"System prompt for trading bot agent: {agent_name}",
            time.time(), time.time(),
            json.dumps({"agent_name": agent_name, "source": "lightweight_registrar"}),
        ))

    # prompt_versions — schema: id, prompt_id, version, content, created_at, author, changelog, is_active
    version_id = str(uuid.uuid4())
    c.execute("""
        INSERT OR IGNORE INTO prompt_versions
        (id, prompt_id, version, content, created_at, author, changelog, is_active)
        VALUES (?, ?, '1.0', ?, ?, 'trading_bot_setup', 'Initial creation via lightweight registrar', 1)
    """, (version_id, prompt_id, system_prompt, time.time()))

    # agent_prompt_pairings — schema: pairing_id, agent_id, prompt_id, compatibility_key, ...
    pairing_id = str(uuid.uuid4())
    c.execute("""
        INSERT OR IGNORE INTO agent_prompt_pairings
        (pairing_id, agent_id, prompt_id, compatibility_key, is_active)
        VALUES (?, ?, ?, ?, 1)
    """, (pairing_id, agent_id, prompt_id, f"{agent_name}_v1"))

    conn.commit()
    return prompt_id


def register_skills(agent_id: str, spec: Dict[str, Any]) -> int:
    """Register all skills for an agent.
    
    Registers 4 types of skills:
    1. python_callable — wrapper functions the agent can call
    2. mcp_tool — MCP handler actions
    3. prompt_template (knowledge) — domain knowledge bullet points
    4. prompt_template (skill_file) — .md skill files loaded on-demand at runtime
    
    Returns count of skills registered.
    """
    conn = _get_conn()
    c = conn.cursor()
    count = 0

    def _insert_skill(aid, sname, stype, definition):
        nonlocal count
        # agent_skills schema: id, agent_id, skill_name, skill_config, proficiency_score
        c.execute("""
            INSERT OR REPLACE INTO agent_skills
            (id, agent_id, skill_name, skill_config, proficiency_score)
            VALUES (?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), aid, sname, json.dumps({"type": stype, "definition": definition}), 0.5))
        count += 1

    # 1. Explicit skills from spec (python_callable + mcp_tool)
    for skill in spec.get("skills", []):
        _insert_skill(agent_id, skill["name"], skill["type"], skill.get("definition", {}))

    # 2. Domain knowledge as prompt_template
    kb = spec.get("knowledge_base", [])
    if kb:
        _insert_skill(agent_id, f"{spec['name']}_domain_knowledge", "prompt_template", {
            "knowledge_items": kb,
            "item_count": len(kb),
            "load_type": "inline",  # injected into prompt directly
        })

    # 3. SKILL.md files as prompt_template (on-demand loading at runtime)
    skill_files = set()
    for mcp in spec.get("mcp_tools", []):
        if mcp in MCP_SKILL_FILES:
            skill_files.add(MCP_SKILL_FILES[mcp])
    for f in AGENT_EXTRA_SKILLS.get(spec["name"], []):
        skill_files.add(f)
    skill_files.add("AGENT_COORDINATION.md")

    for filename in sorted(skill_files):
        filepath = str(SKILLS_DIR / filename)
        if Path(filepath).exists():
            _insert_skill(agent_id, f"skill_file:{filename}", "prompt_template", {
                "file_path": filepath,
                "filename": filename,
                "load_type": "on_demand",  # loaded into context when agent activates
                "description": f"Detailed tool documentation from {filename}",
            })

    conn.commit()
    return count


def create_team(team_name: str, agent_ids: List[str]) -> str:
    """Assign a team_id to a group of agents."""
    conn = _get_conn()
    c = conn.cursor()
    team_id = str(uuid.uuid4())

    placeholders = ",".join(["?"] * len(agent_ids))
    c.execute(
        f"UPDATE agent_registry SET team_id=? WHERE id IN ({placeholders})",
        (team_id, *agent_ids),
    )
    conn.commit()
    return team_id


def load_existing_team(module_name: str = "trading_bot") -> Optional[Dict[str, Any]]:
    """Check if a complete team already exists in the registry.
    
    Returns dict with agent_ids, team_id, metadata if all 7 agents exist.
    Returns None if team is incomplete or missing.
    """
    conn = _get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT id, agent_name, team_id
        FROM agent_registry
        WHERE status='active'
        ORDER BY agent_name
    """)
    rows = c.fetchall()

    if len(rows) < 7:
        return None

    agents = {}
    team_id = None
    for row in rows:
        agent_id, name, meta_json, tid = row
        agents[name] = {
            "agent_id": agent_id,
            "metadata": json.loads(meta_json) if meta_json else {},
        }
        if tid:
            team_id = tid

    # Check we have all 7
    expected = {"oanda_data", "intelligence", "technical_analyst", "validator",
                "execution", "reporter", "cycle_orchestrator"}
    if not expected.issubset(set(agents.keys())):
        return None

    return {"agents": agents, "team_id": team_id}


# ---------------------------------------------------------------------------
# Main: register all 7 agents from AGENT_SPECS
# ---------------------------------------------------------------------------

def register_full_team(agent_specs: List[Dict[str, Any]], force: bool = False) -> Dict[str, Any]:
    """Register all agents, prompts, skills, and team in one shot.
    
    Args:
        agent_specs: The AGENT_SPECS list from team_setup.py
        force: If True, re-register even if team exists
        
    Returns:
        Dict with agent_ids, prompt_ids, skill_counts, team_id
    """
    # Check existing first
    if not force:
        existing = load_existing_team()
        if existing:
            print(f"✅ Team already exists with {len(existing['agents'])} agents (team_id={existing['team_id']})")
            return existing

    print(f"🚀 Registering {len(agent_specs)} agents...")
    
    agent_ids = []
    results = {}
    
    for spec in agent_specs:
        name = spec["name"]
        agent_id = f"trading_{name}_{int(time.time())}"
        
        # 1. Generate prompt
        prompt = generate_system_prompt(spec)
        
        # 2. Register agent with metadata
        register_agent(agent_id, spec, prompt)
        
        # 3. Save prompt to registry
        prompt_id = register_prompt(agent_id, name, prompt)
        
        # 4. Register all skills
        skill_count = register_skills(agent_id, spec)
        
        agent_ids.append(agent_id)
        results[name] = {
            "agent_id": agent_id,
            "prompt_id": prompt_id,
            "skill_count": skill_count,
        }
        print(f"  ✅ {name}: {skill_count} skills, prompt={prompt_id[:40]}...")

    # 5. Create team
    team_id = create_team("trading_bot_team", agent_ids)
    print(f"\n✅ Team created: {team_id}")
    print(f"   {len(agent_ids)} agents, {sum(r['skill_count'] for r in results.values())} total skills")

    return {
        "agents": results,
        "team_id": team_id,
        "agent_ids": agent_ids,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys, re
    
    print("Loading AGENT_SPECS...")
    
    spec_file = Path(__file__).resolve().parent / "team_setup.py"
    source = spec_file.read_text()
    
    # Find the line "AGENT_SPECS ... = [" and extract everything until the
    # matching top-level "]" followed by a blank line or new top-level def/class/var.
    lines = source.split("\n")
    start_line = None
    for idx, line in enumerate(lines):
        if line.startswith("AGENT_SPECS"):
            start_line = idx
            break
    
    if start_line is None:
        print("❌ Could not find AGENT_SPECS in team_setup.py")
        sys.exit(1)
    
    # Find the line with "= [" that starts the actual list (contains "{")
    # Skip the type annotation line
    actual_start = None
    for idx in range(start_line, min(start_line + 5, len(lines))):
        if "= [" in lines[idx] or (lines[idx].strip() == "[" and idx > start_line):
            actual_start = idx
            break
        # Also check: line is just "[" after the annotation
        if lines[idx].strip().startswith("{"):
            actual_start = idx - 1  # the [ is on previous line
            break
    
    if actual_start is None:
        actual_start = start_line
    
    block_lines = []
    depth = 0
    started = False
    for idx in range(actual_start, len(lines)):
        line = lines[idx]
        for ch in line:
            if ch == '[':
                depth += 1
                started = True
            elif ch == ']': depth -= 1
        block_lines.append(line)
        if started and depth == 0:
            break
    
    block_text = "\n".join(block_lines)
    # Find first "[" that's part of the list (after "=")
    eq_idx = block_text.find("= [")
    if eq_idx >= 0:
        list_text = block_text[eq_idx + 2:]
    else:
        # Maybe the [ is on its own line
        list_text = block_text[block_text.index("["):]
    
    # safe eval — only literal Python data structures
    AGENT_SPECS = ast.literal_eval(list_text)
    print(f"  Loaded {len(AGENT_SPECS)} agent specs")
    
    force = "--force" in sys.argv
    result = register_full_team(AGENT_SPECS, force=force)
    
    print("\n" + "=" * 60)
    print("Registration complete.")
