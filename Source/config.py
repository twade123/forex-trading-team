"""
Configuration module for Oanda Forex Trading Team.

Loads credentials from broker_credentials DB (dashboard-selected account),
falling back to file/env vars for backwards compatibility.
"""

import os
import logging

_logger = logging.getLogger(__name__)

def _resolve_trading_user_id() -> int:
    """Resolve trading user_id from TRADING_USER_ID env var (set by serve_ui.py on startup).
    Falls back to core.db admin lookup if env var not set."""
    _env = os.environ.get("TRADING_USER_ID")
    if _env:
        return int(_env)
    # Fallback: search common core.db locations
    import sqlite3 as _sq
    _src = os.path.dirname(os.path.abspath(__file__))
    for _rel in [
        os.path.join(_src, "..", "Database", "v2", "core.db"),           # Forex Trading Team/Database/v2/
        os.path.join(_src, "..", "..", "Jarvis", "Database", "v2", "core.db"),  # peer Jarvis dir
        os.path.expanduser("~/Jarvis/Database/v2/core.db"),             # absolute Jarvis path
    ]:
        _core = os.path.normpath(_rel)
        if os.path.exists(_core):
            try:
                _c = _sq.connect(_core, timeout=3)
                _r = _c.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()
                _c.close()
                if _r:
                    _uid = int(_r[0])
                    os.environ["TRADING_USER_ID"] = str(_uid)  # cache for future calls
                    return _uid
            except Exception:
                continue
    _logger.warning("Cannot resolve trading user_id — no TRADING_USER_ID env and core.db not found")
    return None

# --- Credential Loading ---
# Priority: 1) broker_credentials DB (user_id from TRADING_USER_ID env or admin lookup)
#           2) Environment variables
#           3) API key file (legacy)

_DB_LOADED = False
API_KEY = ""
ACCOUNT_ID = ""
BASE_URL = ""
_ENVIRONMENT = "demo"

def _load_from_db():
    """Try loading from broker_credentials table in v2/core.db."""
    global API_KEY, ACCOUNT_ID, BASE_URL, _ENVIRONMENT, _DB_LOADED
    try:
        import sys
        _src = os.path.dirname(os.path.abspath(__file__))
        if _src not in sys.path:
            sys.path.insert(0, _src)
        from broker_credentials import BrokerCredentials
        bc = BrokerCredentials()
        user_id = _resolve_trading_user_id()
        conn = bc.get_connection(user_id, "oanda")
        if conn.get("configured"):
            API_KEY = conn["api_key"]
            ACCOUNT_ID = conn["account_id"]
            BASE_URL = conn["base_url"]
            _ENVIRONMENT = conn["environment"]
            _DB_LOADED = True
            _logger.info("Config loaded from DB: account=%s env=%s", ACCOUNT_ID, _ENVIRONMENT)
            return True
    except Exception as e:
        _logger.debug("broker_credentials DB not available: %s", e)
    return False

def _load_from_file():
    """Legacy: load API key from file, account from env."""
    global API_KEY, ACCOUNT_ID, BASE_URL, _ENVIRONMENT
    _api_key_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "API", "OANDA_API_KEY.txt"
    )
    path = os.environ.get("OANDA_API_KEY_PATH", _api_key_path)
    try:
        with open(path, "r") as f:
            API_KEY = f.read().strip()
    except FileNotFoundError:
        # 2026-04-27: Lazy assertion — don't block module import on missing
        # credentials. Code paths that actually call OANDA will fail loudly
        # at request time. This lets unrelated code (tests, gateway, agents
        # that don't touch OANDA) import Source/* without needing the env vars.
        API_KEY = os.environ.get("OANDA_API_KEY", "")
    ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
    _ENVIRONMENT = os.environ.get("OANDA_ENVIRONMENT", "demo")
    BASE_URL = LIVE_URL if _ENVIRONMENT == "live" else PRACTICE_URL

# --- Base URLs (must be defined BEFORE _load_from_file uses them) ---
PRACTICE_URL = "https://api-fxpractice.oanda.com"
LIVE_URL = "https://api-fxtrade.oanda.com"
STREAM_PRACTICE_URL = "https://stream-fxpractice.oanda.com"
STREAM_LIVE_URL = "https://stream-fxtrade.oanda.com"

# Load on import — DB first, file fallback
if not _load_from_db():
    _load_from_file()

# If not set by DB loader, default to practice
if not BASE_URL:
    BASE_URL = PRACTICE_URL

# --- Default Headers ---
def get_default_headers() -> dict:
    """Build fresh headers from current API_KEY. Use this over the module-level dict when possible."""
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Accept-Datetime-Format": "RFC3339",
    }

DEFAULT_HEADERS = get_default_headers()  # backward-compat alias

def reload_credentials():
    """Re-read credentials from DB (call after account switch)."""
    global DEFAULT_HEADERS
    if _load_from_db():
        DEFAULT_HEADERS = get_default_headers()
    return {"account_id": ACCOUNT_ID, "environment": _ENVIRONMENT, "base_url": BASE_URL}


def get_oanda_credentials() -> dict:
    """Return OANDA credentials in the format the connection sentry expects.

    Returns:
        {"token": str, "account_id": str, "api_url": str} or empty dict if not configured.
    """
    if not API_KEY or not ACCOUNT_ID:
        return {}
    return {
        "token": API_KEY,
        "account_id": ACCOUNT_ID,
        "api_url": BASE_URL or "https://api-fxpractice.oanda.com",
    }

# --- Rate Limits (per Oanda best practices) ---
MAX_NEW_CONNECTIONS_PER_SECOND = 2
MAX_REQUESTS_PER_SECOND = 100
MAX_CANDLE_COUNT = 5000
DEFAULT_CANDLE_COUNT = 500

# --- Supported Granularities ---
GRANULARITIES = [
    "S5", "S10", "S15", "S30",
    "M1", "M2", "M4", "M5", "M10", "M15", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12",
    "D", "W", "M",
]
