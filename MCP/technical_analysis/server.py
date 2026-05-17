"""Technical Analysis MCP Server -- indicator and pattern tools."""

import os
import sys

_bot_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("technical-analysis")

from .tools import register_all  # noqa: E402

register_all(mcp)
