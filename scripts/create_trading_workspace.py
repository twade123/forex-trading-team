#!/usr/bin/env python3
"""
Create persistent trading bot workspace using the same pattern as claude_interface.py.

This creates the workspace ONCE in the shard database, just like a user conversation
would. The workspace_id is then saved to a config file for the trading bot to use
on every cycle — no re-creation needed.

Usage:
    cd ~/jarvis
    source ~/myenv/bin/activate
    python -m "Forex Trading Team.scripts.create_trading_workspace"

Or directly:
    python "Forex Trading Team/scripts/create_trading_workspace.py"
"""

import sys
import os
import json
import hashlib
import time
import logging
import asyncio
from pathlib import Path

# Setup paths
JARVIS_ROOT = Path(__file__).resolve().parent.parent.parent  # jarvis/
sys.path.insert(0, str(JARVIS_ROOT))
sys.path.insert(0, str(JARVIS_ROOT / "Forex Trading Team"))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("trading_bot.workspace_setup")

# Where we persist the workspace config
CONFIG_FILE = JARVIS_ROOT / "Forex Trading Team" / "Config" / "workspace_config.json"


def generate_workspace_id(name: str, user_id: int = 1) -> str:
    """Generate workspace ID using same pattern as claude_interface._generate_unique_workspace_id"""
    content_hash = hashlib.md5(name.encode()).hexdigest()[:8]
    timestamp = int(time.time())
    slug = name.lower().replace(" ", "_").replace("-", "_")
    return f"ws_{slug}_{content_hash}_{timestamp}"


def create_workspace_in_shard(workspace_id: str, workspace_name: str, 
                                description: str, user_id: int = 1) -> bool:
    """Create workspace in shard database — same as claude_interface._create_workspace_in_database"""
    try:
        from Database.database_sharding_service import DatabaseShardingService

        workspace_data = {
            'workspace_id': workspace_id,
            'workspace_name': workspace_name,
            'owner_id': f"user_{user_id}",
            'settings': {
                'created_from': 'trading_bot_setup',
                'original_request': description[:100],
                'workspace_type': 'trading_bot'
            }
        }

        base_path = Path("~/Jarvis/Database")
        sharding_service = DatabaseShardingService(base_path)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        loop.run_until_complete(sharding_service.initialize_shards(num_shards=4))
        result = loop.run_until_complete(sharding_service.create_workspace(workspace_data))
        loop.close()

        if result:
            logger.info(f"✅ Created workspace in shard: {workspace_id}")
            return True
        else:
            logger.warning(f"⚠️ Shard creation returned False for {workspace_id}")
            return False

    except Exception as e:
        logger.error(f"❌ Shard workspace creation failed: {e}")
        return False


def create_workspace_in_sharing(workspace_id: str, workspace_name: str,
                                  description: str, parent_id: int = None) -> int:
    """Create workspace via WorkspaceSharingManager (for inter-agent sharing)."""
    try:
        from Jarvis_Agent_SDK.import_helper import get_workspace_sharing
        sharing = get_workspace_sharing()
        if sharing is None:
            logger.warning("WorkspaceSharingManager not available")
            return None

        metadata = {"workspace_type": "trading_bot"}
        if parent_id:
            metadata["parent_workspace_id"] = parent_id

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ws_id = loop.run_until_complete(
            sharing.create_workspace(
                user_id=1,
                name=workspace_name,
                description=description,
                metadata=metadata,
            )
        )
        loop.close()

        if ws_id:
            logger.info(f"✅ Created sharing workspace: {workspace_name} (id={ws_id})")
        return ws_id

    except Exception as e:
        logger.error(f"❌ Sharing workspace creation failed: {e}")
        return None


def setup_trading_workspaces():
    """Create the full workspace hierarchy for the trading bot.
    
    Creates workspaces in shard DB only (same as claude_interface.py).
    WorkspaceSharingManager is skipped because it triggers the full jarvis
    init chain (80 DBs, spaCy, BoardRoom) and blocks on circular imports.
    
    The sharing workspace can be created later when the full system boots
    via launch_trevor_desktop.sh.
    
    Saves config to Forex Trading Team/Config/workspace_config.json
    """
    
    # Check if config already exists
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            existing = json.load(f)
        logger.info(f"Workspace config already exists: {CONFIG_FILE}")
        logger.info(f"  Parent workspace: {existing.get('parent_workspace_id')}")
        logger.info(f"  Agent workspaces: {len(existing.get('agent_workspaces', {}))}")
        logger.info(f"  Instrument workspaces: {len(existing.get('instrument_workspaces', {}))}")
        
        response = input("Re-create workspaces? (y/N): ").strip().lower()
        if response != 'y':
            logger.info("Using existing workspace config.")
            return existing

    # Agent names from AGENT_SPECS (hardcoded to avoid importing team_setup.py
    # which triggers the entire jarvis init chain via SwarmHandler)
    AGENT_NAMES = [
        ("oanda_data", "Oanda Data", "Market data collection and historical analysis"),
        ("technical_analyst", "Technical Analysis", "Technical analysis with indicators and patterns"),
        ("wolfram_analyst", "Wolfram Analyst", "Mathematical analysis using Wolfram Alpha"),
        ("news_analyst", "News Analyst", "Forex market impact analysis from news"),
        ("weather_analyst", "Weather Analyst", "Commodity-linked weather impact analysis"),
        ("validator", "Data Validator", "Trade signal validation with multi-gate QA"),
        ("execution", "Execution", "Trade execution and position management"),
        ("reporter", "Reporting", "Trade logging, knowledge management, reporting"),
        ("cycle_orchestrator", "Orchestrator", "Orchestrate trading cycle phases"),
    ]

    # 1. Create parent workspace in shard DB (like claude_interface does)
    parent_ws_id = generate_workspace_id("trading_bot")
    parent_created = create_workspace_in_shard(
        workspace_id=parent_ws_id,
        workspace_name="trading_bot",
        description="Automated forex trading - 9 agents coordinated via swarm",
    )

    # 2. Create agent child workspaces (shard only)
    agent_workspaces = {}
    for agent_name, ws_name, role in AGENT_NAMES:
        agent_ws_id = generate_workspace_id(f"trading_{agent_name}")
        create_workspace_in_shard(
            workspace_id=agent_ws_id,
            workspace_name=f"trading_{agent_name}",
            description=f"Agent workspace for {agent_name}: {role}",
        )

        agent_workspaces[agent_name] = {
            "workspace_id": agent_ws_id,
            "workspace_name": ws_name,
        }
        logger.info(f"  Agent '{agent_name}' → {agent_ws_id}")

    # 3. Create instrument workspaces (shard only)
    instruments = ["EUR_USD", "USD_JPY"]
    instrument_workspaces = {}
    for instrument in instruments:
        inst_ws_id = generate_workspace_id(f"trading_{instrument.lower()}")
        create_workspace_in_shard(
            workspace_id=inst_ws_id,
            workspace_name=f"trading_{instrument.lower()}",
            description=f"Instrument workspace for {instrument}",
        )

        instrument_workspaces[instrument] = {
            "workspace_id": inst_ws_id,
        }
        logger.info(f"  Instrument '{instrument}' → {inst_ws_id}")

    # 5. Save config
    config = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "parent_workspace_id": parent_ws_id,
        "parent_shard_created": parent_created,
        "agent_workspaces": agent_workspaces,
        "instrument_workspaces": instrument_workspaces,
        "instruments": instruments,
    }

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

    logger.info(f"\n✅ Workspace config saved to {CONFIG_FILE}")
    logger.info(f"   Parent: {parent_ws_id}")
    logger.info(f"   Agents: {len(agent_workspaces)}")
    logger.info(f"   Instruments: {len(instrument_workspaces)}")

    return config


if __name__ == "__main__":
    print("=" * 60)
    print("TRADING BOT WORKSPACE SETUP")
    print("Creates workspace hierarchy like claude_interface.py does")
    print("=" * 60)
    print()
    
    config = setup_trading_workspaces()
    
    print()
    print("Done. The trading bot can now use these workspace IDs")
    print(f"Config file: {CONFIG_FILE}")
