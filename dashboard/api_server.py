#!/usr/bin/env python3
"""
Forex Trading Team Dashboard API Server

Lightweight Flask API that reads workspace state from the shard DB
and trading bot databases. No heavy jarvis imports — just SQLite reads.

Usage:
    cd ~/jarvis
    source ~/myenv/bin/activate
    python "Forex Trading Team/dashboard/api_server.py"

Opens dashboard at http://localhost:8800
"""

import sqlite3
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
import threading
import urllib.parse

JARVIS_ROOT = Path(__file__).resolve().parent.parent.parent
TRADING_BOT_ROOT = JARVIS_ROOT / "Forex Trading Team"
CONFIG_FILE = TRADING_BOT_ROOT / "Config" / "workspace_config.json"
SHARD_DB = JARVIS_ROOT / "Database" / "workspace_shard_00.db"
TRADE_LOG_DB = TRADING_BOT_ROOT / "Data" / "trade_log.db"
KNOWLEDGE_DIR = TRADING_BOT_ROOT / "Data" / "knowledge"
BOARDROOM_DB = JARVIS_ROOT / "Database" / "boardroom.db"

# Agent definitions (no imports needed)
AGENTS = {
    "oanda_data": {"role": "Market Data", "icon": "📊", "mcp": "handler_oanda"},
    "technical_analyst": {"role": "Technical Analysis", "icon": "📈", "mcp": None},
    "wolfram_analyst": {"role": "Math Analysis", "icon": "🔢", "mcp": "handler_wolfram"},
    "news_analyst": {"role": "News Impact", "icon": "📰", "mcp": "handler_news_info"},
    "weather_analyst": {"role": "Weather Impact", "icon": "🌤️", "mcp": "handler_weather"},
    "validator": {"role": "Signal Validation", "icon": "✅", "mcp": "handler_data_validator"},
    "execution": {"role": "Trade Execution", "icon": "⚡", "mcp": "handler_oanda"},
    "reporter": {"role": "Reporting", "icon": "📋", "mcp": None},
    "cycle_orchestrator": {"role": "Orchestrator", "icon": "🎯", "mcp": None},
}


def get_workspace_config():
    """Load workspace config."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def get_workspace_conversations(workspace_id, limit=50):
    """Get recent workspace conversations (agent messages)."""
    if not SHARD_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(SHARD_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT participant_name, participant_type, message_content, 
                      phase, timestamp, event_type, metadata
               FROM workspace_conversations 
               WHERE workspace_id = ? 
               ORDER BY timestamp DESC LIMIT ?""",
            (workspace_id, limit)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def get_workspace_activities(workspace_id, limit=50):
    """Get recent workspace activities."""
    if not SHARD_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(SHARD_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT activity_type, activity_data, user_id, created_at
               FROM workspace_activities 
               WHERE workspace_id = ? 
               ORDER BY created_at DESC LIMIT ?""",
            (workspace_id, limit)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def get_trade_log(limit=20):
    """Get recent trades from trade_log.db."""
    if not TRADE_LOG_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(TRADE_LOG_DB))
        conn.row_factory = sqlite3.Row
        # Check what tables exist
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        
        results = {}
        for table in tables[:5]:  # Check first few tables
            try:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
                if rows:
                    results[table] = [dict(r) for r in rows]
            except:
                pass
        conn.close()
        return results
    except Exception as e:
        return {"error": str(e)}


def get_knowledge_store():
    """Get knowledge store contents."""
    if not KNOWLEDGE_DIR.exists():
        return {}
    knowledge = {}
    for instrument_dir in KNOWLEDGE_DIR.iterdir():
        if instrument_dir.is_dir():
            instrument = instrument_dir.name
            knowledge[instrument] = {}
            for json_file in instrument_dir.glob("*.json"):
                try:
                    with open(json_file) as f:
                        knowledge[instrument][json_file.stem] = json.load(f)
                except:
                    pass
    return knowledge


def get_agent_performance():
    """Get agent performance from boardroom DB."""
    if not BOARDROOM_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(str(BOARDROOM_DB))
        conn.row_factory = sqlite3.Row
        
        # Check for agent-related tables
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%agent%'"
        ).fetchall()]
        
        results = {}
        for table in tables[:5]:
            try:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 20").fetchall()
                if rows:
                    results[table] = [dict(r) for r in rows]
            except:
                pass
        conn.close()
        return results
    except Exception as e:
        return {"error": str(e)}


def build_api_response(path):
    """Route API requests."""
    config = get_workspace_config()
    ws_id = config.get("workspace_id", "")
    
    if path == "/api/status":
        return {
            "workspace": config,
            "agents": AGENTS,
            "server_time": datetime.now(timezone.utc).isoformat(),
        }
    elif path == "/api/conversations":
        return get_workspace_conversations(ws_id)
    elif path == "/api/activities":
        return get_workspace_activities(ws_id)
    elif path == "/api/trades":
        return get_trade_log()
    elif path == "/api/knowledge":
        return get_knowledge_store()
    elif path == "/api/performance":
        return get_agent_performance()
    else:
        return {"error": "unknown endpoint", "path": path}


class DashboardHandler(SimpleHTTPRequestHandler):
    """Serve dashboard HTML + API endpoints."""
    
    def __init__(self, *args, **kwargs):
        self.directory = str(Path(__file__).parent)
        super().__init__(*args, directory=self.directory, **kwargs)
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path.startswith("/api/"):
            # API endpoint
            data = build_api_response(parsed.path)
            response = json.dumps(data, indent=2, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(response)
        elif parsed.path == "/" or parsed.path == "":
            # Serve index.html
            self.path = "/index.html"
            super().do_GET()
        else:
            super().do_GET()
    
    def log_message(self, format, *args):
        pass  # Quiet logging


if __name__ == "__main__":
    PORT = 8800
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"🚀 Forex Trading Team Dashboard running at http://localhost:{PORT}")
    print(f"   Workspace: {get_workspace_config().get('workspace_id', 'not configured')}")
    print(f"   Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
