"""Oanda MCP Server -- data collection tools for the trading pipeline."""

import sys
import os

# Add Forex Trading Team to path so Source package is importable
_bot_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)

from mcp.server.fastmcp import FastMCP
from Source.oanda_client import OandaClient

mcp = FastMCP("oanda")

# Shared client -- created once, reused by all tools
_client = None


def get_client() -> OandaClient:
    """Return the shared OandaClient instance, creating it on first call."""
    global _client
    if _client is None:
        _client = OandaClient()
    return _client


# Register all tools
from .tools import register_all  # noqa: E402

register_all(mcp, get_client)
