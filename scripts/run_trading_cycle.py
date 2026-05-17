#!/usr/bin/env python3
"""
Quick runner script for TradingCycle.

This script:
1. Activates the right paths (jarvis root + Forex Trading Team)
2. Loads workspace config from Config/workspace_config.json
3. Creates TradingTeamSetup, calls setup_team() (uses pre-built workspace)
4. Creates CommentProtocol and TradingCycle
5. Calls cycle.run_cycle("EUR_USD")
6. Prints the full result as pretty JSON
7. Has a top-level timeout of 120 seconds (whole script)
8. Catches all exceptions with full traceback

Usage:
    cd ~/jarvis
    source ~/myenv/bin/activate
    python "Forex Trading Team/scripts/run_trading_cycle.py"
"""

import sys
import os
import json
import logging
import traceback
import signal
from pathlib import Path

# Setup paths
JARVIS_ROOT = Path(__file__).resolve().parent.parent.parent  # jarvis/
sys.path.insert(0, str(JARVIS_ROOT))
sys.path.insert(0, str(JARVIS_ROOT / "Forex Trading Team"))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("trading_bot.runner")

# Timeout configuration
SCRIPT_TIMEOUT = 300  # seconds (5 min — agent registration takes ~20s each due to DB locks)


def timeout_handler(signum, frame):
    """Handle timeout by raising an exception."""
    raise TimeoutError(f"Script timed out after {SCRIPT_TIMEOUT} seconds")


def load_workspace_config():
    """Load workspace configuration from Config/workspace_config.json."""
    config_file = JARVIS_ROOT / "Forex Trading Team" / "Config" / "workspace_config.json"
    if not config_file.exists():
        raise FileNotFoundError(f"Workspace config not found: {config_file}")
    
    with open(config_file, 'r') as f:
        return json.load(f)


def main():
    """Run a complete trading cycle for EUR_USD."""
    
    # Set up timeout
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(SCRIPT_TIMEOUT)
    
    try:
        logger.info("Starting trading cycle runner...")
        
        # Step 1: Load workspace config
        logger.info("Loading workspace configuration...")
        workspace_config = load_workspace_config()
        logger.info(f"Loaded workspace config with {len(workspace_config.get('agent_workspaces', {}))} agent workspaces")
        
        # Step 2: Import required classes
        logger.info("Importing trading bot components...")
        from Source.agents.team_setup import TradingTeamSetup
        from Source.agents.comment_protocol import CommentProtocol
        from Source.agents.trading_cycle import TradingCycle
        
        # Step 3: Create TradingTeamSetup and set it up
        logger.info("Setting up trading team...")
        team_setup = TradingTeamSetup()
        
        # The setup_team() method will use the pre-built workspace from the config
        team_result = team_setup.setup_team()
        logger.info(f"Team setup result: {team_result}")
        
        # Step 4: Create CommentProtocol
        logger.info("Creating comment protocol...")
        comment_protocol = CommentProtocol()
        
        # Step 5: Create TradingCycle
        logger.info("Creating trading cycle...")
        trading_cycle = TradingCycle(team_setup, comment_protocol)
        
        # Step 6: Run cycle for EUR_USD
        logger.info("Running trading cycle for EUR_USD...")
        result = trading_cycle.run_cycle("EUR_USD")
        
        # Step 7: Print result as pretty JSON
        logger.info("Trading cycle completed successfully!")
        print("\n" + "="*50)
        print("TRADING CYCLE RESULT")
        print("="*50)
        print(json.dumps(result, indent=2, default=str))
        print("="*50 + "\n")
        
        return result
        
    except TimeoutError as e:
        logger.error(f"Script timeout: {e}")
        print(f"\nERROR: {e}")
        return {"error": str(e), "timeout": True}
        
    except Exception as e:
        logger.error(f"Trading cycle failed: {e}")
        logger.error("Full traceback:")
        logger.error(traceback.format_exc())
        
        print(f"\nERROR: Trading cycle failed: {e}")
        print("\nFull traceback:")
        print(traceback.format_exc())
        
        return {"error": str(e), "traceback": traceback.format_exc()}
        
    finally:
        # Cancel the alarm
        signal.alarm(0)


if __name__ == "__main__":
    try:
        result = main()
        # Exit with error code if there was an error
        if isinstance(result, dict) and result.get("error"):
            sys.exit(1)
        else:
            sys.exit(0)
    except KeyboardInterrupt:
        logger.info("Script interrupted by user")
        print("\nScript interrupted by user")
        sys.exit(130)