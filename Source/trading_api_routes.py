"""
Trading API routes registered via register_trading_routes() onto the Flask app in serve_ui.py (jarvis root).

These routes handle broker connection management for the trading dashboard.
Import and register with: register_trading_routes(app)

All routes require valid Bearer token (same auth as Trevor Desktop).
All routes are under /api/trading/*.
"""

import os
import sys
import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from flask import request, jsonify

from db_connection import DB_PATH, BOARDROOM_PATH
try:
    from db_pool import get_trading_forex
except ImportError:
    def get_trading_forex():
        return sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "Database", "v2", "trading_forex.db"), check_same_thread=False, isolation_level=None)

logger = logging.getLogger(__name__)

def _resolve_admin_user_id() -> int:
    """Resolve the admin user_id from TRADING_USER_ID env (set by serve_ui.py) or core.db lookup."""
    _env = os.environ.get("TRADING_USER_ID")
    if _env:
        return int(_env)
    # Fallback: search common core.db locations
    _src = os.path.dirname(os.path.abspath(__file__))
    for _rel in [
        os.path.join(os.path.dirname(DB_PATH), 'core.db'),
        os.path.join(_src, "..", "Database", "v2", "core.db"),
        os.path.expanduser("~/Jarvis/Database/v2/core.db"),
    ]:
        _core = os.path.normpath(_rel)
        if os.path.exists(_core):
            try:
                _c = sqlite3.connect(_core, isolation_level=None)
                _r = _c.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()
                _c.close()
                if _r:
                    return int(_r[0])
            except Exception:
                continue
    return None

# ── Module-level pair cooldown state ─────────────────────────────────────────
# Shared between _fire_snipe_cycle (reads) and position_guardian (writes).
# Keyed by instrument, value is unix timestamp of last trade close on that pair.
# Guardian imports this dict directly to stamp it on close.
pair_last_close: dict = {}        # (user_id, instrument) → epoch float
PAIR_COOLDOWN_SECS: int = 1800    # 30 minutes

# Flight recorder
try:
    from flight_recorder import flight, FlightStage
except ImportError:
    flight = None
    FlightStage = None

# Add Forex Trading Team Source to path for imports
_TRADING_SOURCE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Source"
)
if _TRADING_SOURCE not in sys.path:
    sys.path.insert(0, _TRADING_SOURCE)

# Config and DB paths
_TRADING_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JARVIS_ROOT = os.path.dirname(_TRADING_BOT_DIR)  # .../Jarvis
_V2_DB_DIR = os.path.join(_JARVIS_ROOT, "Database", "v2")
_TRADING_FOREX_DB = os.path.join(_V2_DB_DIR, "trading_forex.db")
_JOURNEYS_DB = os.path.join(_V2_DB_DIR, "journeys.db")
_RISK_CONFIG_PATH = os.path.join(_TRADING_BOT_DIR, "Config", "risk_config.json")
_DB_PATH = os.path.join(_V2_DB_DIR, "core.db")

# Module-level guardian refs — set inside register_trading_routes,
# accessible to trading_cycle.py for immediate trade registration
_guardian_instance = None
_guardian_loop = None

# SSE push function — wired in by serve_ui.py via register_trading_routes so
# background guardian callbacks can deliver user-scoped events to the SSE system.
_sse_push_fn = None

# Parallel cycle configuration.
# Local-model stack (MLX 9B + 35B) shares one Metal GPU. When 3+ validators run
# concurrently on the 35B, the 9B gets starved of Metal and TAs time out.
# MAX=2 keeps 1 cycle in TA phase (9B) and 1 in validator phase (35B) at a time —
# each model gets its own request, pipelining across models without contention.
# 5 pairs complete in ~9-10 min wall time (within 15-min candle).
# Was 5 (Opus era — cloud validator, 9B had Metal to itself). Dropped 2026-04-23
# after scout batch caused 3 concurrent validators → 9B starvation → TA timeouts.
MAX_CONCURRENT_CYCLES = 2

# Background thread pool — replaces raw threading.Thread() spawns.
# Bounded at 10 workers: cycle threads + background tasks without OS thread exhaustion.
import concurrent.futures as _cf
_BACKGROUND_EXECUTOR = _cf.ThreadPoolExecutor(
    max_workers=10,
    thread_name_prefix="jarvis-bg",
)


def _trigger_guardian_reconcile():
    """Trigger an immediate guardian reconcile (called when a trade is placed).
    
    The guardian polls OANDA every 15s anyway, but this makes new trades
    appear in the guardian within ~1s of placement.
    """
    global _guardian_instance, _guardian_loop
    if _guardian_instance and _guardian_loop and _guardian_loop.is_running():
        asyncio.run_coroutine_threadsafe(
            _guardian_instance._reconcile(), _guardian_loop
        )


def register_trading_routes(app, validate_auth_token_func, sse_push_fn=None):
    """
    Register /api/trading/* routes on the Flask app.

    Args:
        app: Flask app instance
        validate_auth_token_func: Function that takes a token string and
            returns user_info dict (with 'user_id') or None.
        sse_push_fn: Optional callable matching send_sse_message(event_type, data,
            target_session=None, target_user_id=None). When provided, guardian
            callbacks deliver threat/escalation events via the SSE system
            instead of the raw WebSocket broadcast, enabling per-user isolation.
    """
    global _sse_push_fn
    _sse_push_fn = sse_push_fn
    # ── Init Scout Profile Engine once (shared across all cycles) ──
    import sys as _sys
    import os as _os
    import time as _t

    # ── Balance cache: last known good OANDA balance ──────────────────────────
    # Survives transient OANDA timeouts / DNS failures so the header never blanks
    _balance_cache = {}  # user_id → {"live_balance": float, "nav": float, "unrealized_pl": float, "open_trades": int, "ts": float}

    # ── Open trades cache: last known open positions ───────────────────────────
    # Survives transient OANDA timeouts so chart card P&L never blanks
    _open_trades_cache = {}  # user_id → {"trades": list, "ts": float}

    # Use module-level pair cooldown dicts (imported by position_guardian too)
    import trading_api_routes as _self_mod
    _pair_last_close   = _self_mod.pair_last_close
    _PAIR_COOLDOWN_SECS = _self_mod.PAIR_COOLDOWN_SECS

    _profile_engine = None
    try:
        _db = DB_PATH
        if _os.path.exists(_db):
            _src_dir = _os.path.dirname(__file__)
            if _src_dir not in _sys.path:
                _sys.path.insert(0, _src_dir)
            from scout_profiles import ScoutProfileEngine
            _t0 = _t.time()
            _profile_engine = ScoutProfileEngine(_db)
            app.logger.info("Profile engine loaded in %.1fs (%d profiles)",
                           _t.time() - _t0, len(getattr(_profile_engine, 'profiles', {})))
        else:
            app.logger.warning("Profile engine DB not found: %s", _db)
    except Exception as _pe_exc:
        app.logger.warning("Profile engine init failed (candle bonus disabled): %s", _pe_exc)
    app.config['_profile_engine'] = _profile_engine
    # Also inject into trading_cycle module for background thread access
    if _profile_engine is not None:
        try:
            _tc_path = _os.path.join(_os.path.dirname(__file__), "agents", "trading_cycle.py")
            import importlib.util
            spec = importlib.util.spec_from_file_location("Source.agents.trading_cycle", _tc_path)
            # Check if already imported under any name
            _injected = False
            for _mod_name, _mod in _sys.modules.items():
                if hasattr(_mod, '_shared_profile_engine') and 'trading_cycle' in _mod_name:
                    _mod._shared_profile_engine = _profile_engine
                    app.logger.info("Profile engine injected into %s", _mod_name)
                    _injected = True
            if not _injected:
                # Module not yet imported — it'll be imported later during first cycle
                # Store in app config; trading_cycle will check there as fallback
                app.logger.info("Profile engine stored in app.config (will inject on first cycle import)")
        except Exception as _inj_exc:
            app.logger.warning("Failed to inject profile engine: %s", _inj_exc)

    # ── Manual Trade Store (shared, lazy-init) ──
    _manual_store = None

    def _get_manual_store():
        nonlocal _manual_store
        if _manual_store is None:
            try:
                from manual_trade_store import ManualTradeStore
                _manual_store = ManualTradeStore()
            except Exception as e:
                logger.warning("Manual trade store init failed: %s", e)
                return None
        return _manual_store

    def _get_trading_session():
        """Get current forex trading session based on UTC hour."""
        from datetime import datetime, timezone
        h = datetime.now(timezone.utc).hour
        if 0 <= h < 7:
            return "Asian"
        elif 7 <= h < 12:
            return "London"
        elif 12 <= h < 16:
            return "NY_Overlap"
        elif 16 <= h < 21:
            return "NY"
        else:
            return "Off_Hours"

    def _get_authenticated_user():
        """Extract and validate user from Bearer token. Returns (user_info, error_response)."""
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None, (jsonify({"error": "Authentication required"}), 401)

        token = auth_header.split(" ")[1]
        user_info = validate_auth_token_func(token)
        if not user_info or "user_id" not in user_info:
            return None, (jsonify({"error": "Invalid or expired token"}), 401)

        return user_info, None

    def _get_broker_credentials():
        """Lazy import to avoid circular deps."""
        from broker_credentials import BrokerCredentials
        return BrokerCredentials()

    def _ensure_trading_preferences_table():
        """Create trading_preferences table if it doesn't exist."""
        from db_connection import get_db
        with get_db(_DB_PATH, timeout=10) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trading_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    pref_key TEXT NOT NULL,
                    pref_value TEXT NOT NULL,
                    updated_at TEXT DEFAULT (datetime('now')),
                    UNIQUE(user_id, pref_key),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
            conn.commit()

    def _get_trading_preference(user_id, key, default=None):
        """Get a trading preference value for a user."""
        _ensure_trading_preferences_table()
        from db_connection import get_db
        with get_db(_DB_PATH, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT pref_value FROM trading_preferences WHERE user_id = ? AND pref_key = ?",
                (user_id, key)
            ).fetchone()
            return row['pref_value'] if row else default

    def _set_trading_preference(user_id, key, value):
        """Set a trading preference value for a user."""
        _ensure_trading_preferences_table()
        from db_connection import get_db
        with get_db(_DB_PATH, timeout=10) as conn:
            conn.execute("""
                INSERT INTO trading_preferences (user_id, pref_key, pref_value)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, pref_key) DO UPDATE SET
                    pref_value = excluded.pref_value,
                    updated_at = datetime('now')
            """, (user_id, key, str(value)))
            conn.commit()

    def _load_risk_config():
        """Load risk_config.json."""
        try:
            with open(_RISK_CONFIG_PATH, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load risk config: {e}")
            return {"instruments": ["EUR_USD", "GBP_USD", "USD_JPY"], "risk_limits": {}}

    def _get_user_risk_settings(user_id):
        """Get user's risk settings (config defaults + user overrides)."""
        config = _load_risk_config()
        base_settings = dict(config.get("risk_limits", {}))
        # Merge sniper config defaults into the flat settings dict
        sniper_cfg = config.get("sniper", {})
        if sniper_cfg:
            base_settings["sniper_threshold"] = sniper_cfg.get("threshold", 12)
            base_settings["sniper_tp_atr"] = sniper_cfg.get("tp_atr", 0.5)
            base_settings["sniper_sl_atr"] = sniper_cfg.get("sl_atr", 2.5)
        # Merge position sizing defaults
        pos_cfg = config.get("position_sizing", {})
        base_settings["position_sizing_mode"] = pos_cfg.get("mode", "auto")
        base_settings["fixed_units"] = pos_cfg.get("fixed_units", 10000)
        base_settings["fixed_lots"] = pos_cfg.get("fixed_lots", 0.1)
        base_settings["auto_profit"] = "on"
        
        # Apply user overrides
        _ensure_trading_preferences_table()
        from db_connection import get_db
        with get_db(_DB_PATH, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT pref_key, pref_value FROM trading_preferences WHERE user_id = ? AND pref_key LIKE 'risk_%'",
                (user_id,)
            ).fetchall()
        
        user_overrides = {}
        for row in rows:
            key = row['pref_key']
            if key.startswith('risk_'):
                setting_key = key[5:]  # Remove 'risk_' prefix
                try:
                    user_overrides[setting_key] = float(row['pref_value'])
                except ValueError:
                    user_overrides[setting_key] = row['pref_value']
        
        return {**base_settings, **user_overrides}

    # ------------------------------------------------------------------
    # POST /api/trading/validate-key
    # Validate a broker API key without saving. Returns available accounts.
    # ------------------------------------------------------------------
    @app.route("/api/trading/validate-key", methods=["POST", "OPTIONS"])
    def api_trading_validate_key():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        data = request.json or {}
        broker = data.get("broker", "oanda")
        api_key = data.get("api_key")

        if not api_key:
            return jsonify({"error": "api_key is required"}), 400

        bc = _get_broker_credentials()
        result = bc.validate_key(broker, api_key)
        return jsonify(result)

    # ------------------------------------------------------------------
    # POST /api/trading/connect
    # Validate, encrypt, and save broker credentials.
    # ------------------------------------------------------------------
    @app.route("/api/trading/connect", methods=["POST", "OPTIONS"])
    def api_trading_connect():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        data = request.json or {}
        broker = data.get("broker", "oanda")
        api_key = data.get("api_key")
        account_id = data.get("account_id")
        environment = data.get("environment", "demo")

        if not api_key:
            return jsonify({"error": "api_key is required"}), 400
        if not account_id:
            return jsonify({"error": "account_id is required"}), 400
        if environment not in ("demo", "live"):
            return jsonify({"error": "environment must be 'demo' or 'live'"}), 400

        # Live mode requires explicit confirmation
        if environment == "live" and not data.get("confirm_live"):
            return jsonify({
                "error": "Live trading requires confirmation",
                "requires_confirmation": True,
                "message": "⚠️ You are about to enable LIVE trading with real money. "
                           "Send confirm_live: true to proceed.",
            }), 400

        bc = _get_broker_credentials()
        result = bc.connect(
            user_id=user_info["user_id"],
            broker=broker,
            api_key=api_key,
            account_id=account_id,
            environment=environment,
        )

        if result.get("success"):
            return jsonify(result)
        else:
            return jsonify(result), 400

    # ------------------------------------------------------------------
    # GET /api/trading/status
    # Returns broker connection status (no secrets).
    # ------------------------------------------------------------------
    @app.route("/api/trading/status", methods=["GET", "OPTIONS"])
    def api_trading_status():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        broker = request.args.get("broker", "oanda")

        bc = _get_broker_credentials()
        status = bc.get_status(user_info["user_id"], broker)

        # If configured, also fetch live balance (with cache fallback)
        uid = user_info["user_id"]
        if status.get("configured"):
            try:
                conn = bc.get_connection(uid, broker)
                if conn.get("configured") and broker == "oanda":
                    import requests as http_requests
                    headers = {"Authorization": f"Bearer {conn['api_key']}"}
                    r = http_requests.get(
                        f"{conn['base_url']}/v3/accounts/{conn['account_id']}/summary",
                        headers=headers, timeout=4,
                    )
                    if r.status_code == 200:
                        acct = r.json().get("account", {})
                        bal = {
                            "live_balance": float(acct.get("balance", 0)),
                            "unrealized_pl": float(acct.get("unrealizedPL", 0)),
                            "open_trades": int(acct.get("openTradeCount", 0)),
                            "nav": float(acct.get("NAV", 0)),
                            "ts": _t.time(),
                        }
                        _balance_cache[uid] = bal
                        status.update(bal)
            except Exception as e:
                logger.warning(f"Failed to fetch live balance: {e}")

            # Fallback: use cached balance if live fetch failed
            if "live_balance" not in status and uid in _balance_cache:
                cached = _balance_cache[uid]
                status["live_balance"] = cached["live_balance"]
                status["unrealized_pl"] = cached["unrealized_pl"]
                status["open_trades"] = cached["open_trades"]
                status["nav"] = cached["nav"]
                status["balance_cached"] = True
                logger.info(f"Returning cached balance ${cached['live_balance']:,.2f} for user {uid}")

        return jsonify(status)

    # ------------------------------------------------------------------
    # POST /api/trading/switch-env
    # Switch between demo and live environments.
    # ------------------------------------------------------------------
    @app.route("/api/trading/switch-env", methods=["POST", "OPTIONS"])
    def api_trading_switch_env():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        data = request.json or {}
        broker = data.get("broker", "oanda")
        environment = data.get("environment")
        account_id = data.get("account_id")

        if not environment or environment not in ("demo", "live"):
            return jsonify({"error": "environment must be 'demo' or 'live'"}), 400

        # Live mode requires explicit confirmation
        if environment == "live" and not data.get("confirm_live"):
            return jsonify({
                "error": "Live trading requires confirmation",
                "requires_confirmation": True,
                "message": "⚠️ Switching to LIVE trading with real money. "
                           "Send confirm_live: true to proceed.",
            }), 400

        bc = _get_broker_credentials()
        result = bc.switch_environment(
            user_id=user_info["user_id"],
            broker=broker,
            environment=environment,
            account_id=account_id,
        )

        if result.get("success"):
            return jsonify(result)
        else:
            return jsonify(result), 400

    # ------------------------------------------------------------------
    # GET /api/trading/accounts
    # Re-probe broker for available accounts.
    # ------------------------------------------------------------------
    @app.route("/api/trading/accounts", methods=["GET", "OPTIONS"])
    def api_trading_accounts():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        broker = request.args.get("broker", "oanda")

        bc = _get_broker_credentials()
        conn = bc.get_connection(user_info["user_id"], broker)

        if not conn.get("configured"):
            return jsonify({"error": "No broker configured"}), 404

        # Re-validate to get fresh accounts
        result = bc.validate_key(broker, conn["api_key"])
        return jsonify(result)

    # ------------------------------------------------------------------
    # DELETE /api/trading/disconnect
    # Remove broker credentials.
    # ------------------------------------------------------------------
    @app.route("/api/trading/disconnect", methods=["DELETE", "OPTIONS"])
    def api_trading_disconnect():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        data = request.json or {}
        broker = data.get("broker", "oanda")

        bc = _get_broker_credentials()
        result = bc.disconnect(user_info["user_id"], broker)

        if result.get("success"):
            return jsonify(result)
        else:
            return jsonify(result), 404

    # ------------------------------------------------------------------
    # GET /api/trading/config
    # Returns risk_config.json contents (instruments, risk_limits, etc.)
    # ------------------------------------------------------------------
    @app.route("/api/trading/config", methods=["GET", "OPTIONS"])
    def api_trading_config():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        config = _load_risk_config()
        
        # Include user's risk setting overrides
        user_risk_settings = _get_user_risk_settings(user_info["user_id"])
        config["risk_limits"] = user_risk_settings
        
        return jsonify(config)

    # ------------------------------------------------------------------
    # POST /api/trading/active-pair
    # Sets the user's active trading pair
    # ------------------------------------------------------------------
    @app.route("/api/trading/active-pair", methods=["POST", "OPTIONS"])
    def api_trading_set_active_pair():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        data = request.json or {}
        instrument = data.get("instrument")

        if not instrument:
            return jsonify({"error": "instrument is required"}), 400

        _set_trading_preference(user_info["user_id"], "active_instrument", instrument)
        
        return jsonify({"success": True, "instrument": instrument})

    # ------------------------------------------------------------------
    # GET /api/trading/active-pair
    # Gets the user's active trading pair
    # ------------------------------------------------------------------
    @app.route("/api/trading/active-pair", methods=["GET", "OPTIONS"])
    def api_trading_get_active_pair():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        instrument = _get_trading_preference(user_info["user_id"], "active_instrument", "EUR_USD")
        
        return jsonify({"instrument": instrument})

    # ------------------------------------------------------------------
    # POST /api/trading/risk-settings
    # Updates user's risk settings (per-user overrides)
    # ------------------------------------------------------------------
    @app.route("/api/trading/risk-settings", methods=["POST", "OPTIONS"])
    def api_trading_risk_settings():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        data = request.json or {}
        
        # Map of allowed risk setting keys (frontend key -> preference key)
        allowed_keys = {
            "min_confluence": "risk_min_confluence",
            "max_risk_per_trade_pct": "risk_max_risk_per_trade_pct",
            "max_daily_loss_pct": "risk_max_daily_loss_pct",
            "max_concurrent_trades": "risk_max_concurrent_trades",
            "min_rr_ratio": "risk_min_rr_ratio",
            "sniper_threshold": "risk_sniper_threshold",
            "sniper_tp_atr": "risk_sniper_tp_atr",
            "sniper_sl_atr": "risk_sniper_sl_atr",
            "position_sizing_mode": "risk_position_sizing_mode",
            "fixed_units": "risk_fixed_units",
            "fixed_lots": "risk_fixed_lots",
            "auto_profit": "risk_auto_profit",
        }
        
        updated = {}
        for key, value in data.items():
            if key in allowed_keys:
                pref_key = allowed_keys[key]
                _set_trading_preference(user_info["user_id"], pref_key, value)
                updated[key] = value
        
        # Config-level settings (written to risk_config.json, not per-user)
        config_keys = {"watch_ttl_hours", "watch_check_interval_min"}
        for key in config_keys:
            if key in data:
                try:
                    config = _load_risk_config()
                    config[key] = float(data[key])
                    with open(_RISK_CONFIG_PATH, "w") as f:
                        json.dump(config, f, indent=2)
                    updated[key] = data[key]
                except Exception as exc:
                    logger.warning("Failed to save %s to config: %s", key, exc)

        if not updated:
            return jsonify({"error": "No valid risk settings provided"}), 400
        
        return jsonify({"success": True, "updated": updated})

    # ------------------------------------------------------------------
    # GET/POST /api/trading/notification-prefs
    # Per-user notification preferences: which events, what frequency
    # ------------------------------------------------------------------
    @app.route("/api/trading/notification-prefs", methods=["GET", "POST", "OPTIONS"])
    def api_notification_prefs():
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        uid = user_info["user_id"]

        _NOTIF_PREF_KEYS = {
            "notif_trade_opened", "notif_trade_closed",
            "notif_sniper_fired", "notif_eod_summary",
        }
        # Values: "realtime" | "hourly" | "daily" | "off"

        if request.method == "GET":
            prefs = {}
            for k in _NOTIF_PREF_KEYS:
                prefs[k] = _get_trading_preference(uid, k, "realtime")
            return jsonify({"ok": True, "prefs": prefs})

        data = request.json or {}
        saved = {}
        for k, v in data.items():
            if k in _NOTIF_PREF_KEYS and v in ("realtime", "hourly", "daily", "off"):
                _set_trading_preference(uid, k, v)
                saved[k] = v
        return jsonify({"ok": True, "saved": saved})

    # ------------------------------------------------------------------
    # GET /api/trading/chart-data
    # Returns candles + trade markers for Lightweight Charts
    # ------------------------------------------------------------------
    # ─── User Chart Annotations ───────────────────────────────────────────────

    @app.route("/api/trading/annotations", methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"])
    def api_annotations():
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        user_id = user_info["user_id"]
        db_path = _TRADING_FOREX_DB

        if request.method == "GET":
            pair = request.args.get("pair", "").upper().replace("/", "_")
            if not pair:
                return jsonify({"error": "pair required"}), 400
            try:
                import sqlite3 as _sq
                with _sq.connect(db_path, timeout=10, isolation_level=None) as conn:
                    conn.row_factory = _sq.Row
                    rows = conn.execute(
                        "SELECT * FROM user_chart_annotations WHERE pair=? AND user_id=? AND active=1 ORDER BY created_at DESC",
                        (pair, user_id)
                    ).fetchall()
                return jsonify({"annotations": [dict(r) for r in rows]})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        if request.method == "POST":
            data = request.json or {}
            pair = (data.get("pair") or "").upper().replace("/", "_")
            ann_type = data.get("type") or data.get("annotation_type", "note")
            price = data.get("price")
            direction = data.get("direction")
            note = data.get("note", "")
            ema_cross = data.get("ema_cross")
            fan_state = data.get("fan_state")
            bb_state = data.get("bb_state")
            timeframe = data.get("timeframe", "H1")
            bar_time = data.get("bar_time")   # unix timestamp of the candle bar
            # snipe_id: if the user submitted this annotation via a snipe/watch context,
            # record the owning watch ID so the annotation is scoped to that snipe only.
            snipe_id = data.get("snipe_id") or data.get("watch_id") or None
            if not pair:
                return jsonify({"error": "pair required"}), 400
            try:
                import sqlite3 as _sq
                with _sq.connect(db_path, timeout=10, isolation_level=None) as conn:
                    conn.execute("PRAGMA journal_mode=DELETE")
                    # Ensure snipe_id column exists (migration guard)
                    try:
                        conn.execute("ALTER TABLE user_chart_annotations ADD COLUMN snipe_id INTEGER DEFAULT NULL")
                    except Exception:
                        pass
                    cur = conn.execute(
                        "INSERT INTO user_chart_annotations "
                        "(user_id, pair, annotation_type, price, direction, note, ema_cross, fan_state, bb_state, timeframe, bar_time, snipe_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (user_id, pair, ann_type, price, direction, note, ema_cross, fan_state, bb_state, timeframe, bar_time, snipe_id)
                    )
                    conn.commit()
                    ann_id = cur.lastrowid
                logger.info(f"[annotations] User {user_id} added {ann_type} on {pair} snipe_id={snipe_id}: {note}")
                return jsonify({"id": ann_id, "status": "saved", "snipe_id": snipe_id})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        if request.method == "PATCH":
            ann_id = request.args.get("id")
            if not ann_id:
                return jsonify({"error": "id required"}), 400
            data = request.get_json(silent=True) or {}
            try:
                import sqlite3 as _sq
                with _sq.connect(db_path, timeout=10, isolation_level=None) as conn:
                    conn.execute("PRAGMA journal_mode=DELETE")
                    if "note" in data:
                        conn.execute("UPDATE user_chart_annotations SET note=? WHERE id=? AND user_id=?",
                                     (data["note"], ann_id, user_id))
                    if "price" in data:
                        conn.execute("UPDATE user_chart_annotations SET price=? WHERE id=? AND user_id=?",
                                     (data["price"], ann_id, user_id))
                    if "bar_time" in data:
                        conn.execute("UPDATE user_chart_annotations SET bar_time=? WHERE id=? AND user_id=?",
                                     (data["bar_time"], ann_id, user_id))
                    conn.commit()
                return jsonify({"status": "updated"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        if request.method == "DELETE":
            ann_id = request.args.get("id")
            if not ann_id:
                # Clear all for pair
                pair = request.args.get("pair", "").upper().replace("/", "_")
                try:
                    import sqlite3 as _sq
                    with _sq.connect(db_path, timeout=10, isolation_level=None) as conn:
                        conn.execute("PRAGMA journal_mode=DELETE")
                        conn.execute("UPDATE user_chart_annotations SET active=0 WHERE pair=? AND user_id=?", (pair, user_id))
                        conn.commit()
                    return jsonify({"status": "cleared"})
                except Exception as e:
                    return jsonify({"error": str(e)}), 500
            try:
                import sqlite3 as _sq
                with _sq.connect(db_path, timeout=10, isolation_level=None) as conn:
                    conn.execute("PRAGMA journal_mode=DELETE")
                    conn.execute("UPDATE user_chart_annotations SET active=0 WHERE id=? AND user_id=?", (ann_id, user_id))
                    conn.commit()
                return jsonify({"status": "deleted"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

    # ──────────────────────────────────────────────────────────────────────────

    @app.route("/api/trading/chart-data", methods=["GET", "OPTIONS"])
    def api_trading_chart_data():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        instrument = request.args.get("instrument", "EUR_USD")
        timeframe = request.args.get("timeframe", "M15")
        count = int(request.args.get("count", "200"))

        # Get candles from OANDA
        candles = []
        try:
            bc = _get_broker_credentials()
            conn = bc.get_connection(user_info["user_id"], "oanda")
            if conn.get("configured"):
                import requests as http_requests
                headers = {"Authorization": f"Bearer {conn['api_key']}"}
                params = {
                    "granularity": timeframe,
                    "count": min(count, 500),
                    "price": "M",  # midpoint
                }
                r = http_requests.get(
                    f"{conn['base_url']}/v3/instruments/{instrument}/candles",
                    headers=headers, params=params, timeout=15,
                )
                if r.status_code == 200:
                    for c in r.json().get("candles", []):
                        if c.get("complete", True):
                            mid = c.get("mid", {})
                            candles.append({
                                "time": c["time"] if timeframe not in ("D", "W", "M") else c["time"][:10],
                                "open": float(mid.get("o", 0)),
                                "high": float(mid.get("h", 0)),
                                "low": float(mid.get("l", 0)),
                                "close": float(mid.get("c", 0)),
                                "volume": int(c.get("volume", 0)),
                            })
                    # For intraday, use unix timestamps
                    if timeframe not in ("D", "W", "M"):
                        from datetime import datetime as dt
                        for c_item in candles:
                            try:
                                # OANDA uses nanosecond precision — truncate to microseconds for Python
                                ts = c_item["time"].replace("Z", "+00:00")
                                # Truncate fractional seconds to 6 digits max
                                if "." in ts:
                                    parts = ts.split(".")
                                    frac_and_tz = parts[1]
                                    # Split fraction from timezone offset
                                    for i, ch in enumerate(frac_and_tz):
                                        if ch in ('+', '-'):
                                            frac = frac_and_tz[:i][:6]
                                            tz = frac_and_tz[i:]
                                            ts = f"{parts[0]}.{frac}{tz}"
                                            break
                                t = dt.fromisoformat(ts)
                                c_item["time"] = int(t.timestamp())
                            except Exception:
                                pass
        except Exception as e:
            logger.error(f"Failed to fetch candles: {e}")

        # Get recent trades from trade log DB
        trades = []
        try:
            trade_db = os.path.join(_TRADING_BOT_DIR, "Source", "backtester", "trading.db")
            if os.path.exists(trade_db):
                import sqlite3 as sql3
                tconn = sql3.connect(trade_db, isolation_level=None)
                tconn.row_factory = sql3.Row
                rows = tconn.execute("""
                    SELECT instrument, direction, entry_price, exit_price,
                           entry_time, exit_time, profit_loss, status
                    FROM trades
                    WHERE instrument = ?
                    ORDER BY entry_time DESC LIMIT 50
                """, (instrument,)).fetchall()
                tconn.close()
                for r in rows:
                    trades.append(dict(r))
        except Exception as e:
            logger.debug(f"Trade log not available: {e}")

        # Compute indicator series for chart overlays
        indicator_series = {}
        if len(candles) >= 55:
            try:
                import numpy as np
                closes = np.array([c["close"] for c in candles], dtype=float)
                highs = np.array([c["high"] for c in candles], dtype=float)
                lows = np.array([c["low"] for c in candles], dtype=float)
                times = [c["time"] for c in candles]

                # EMA 21, 55, 100
                def _ema(data, period):
                    ema_arr = np.full_like(data, np.nan)
                    if len(data) < period:
                        return ema_arr
                    ema_arr[period - 1] = np.mean(data[:period])
                    mult = 2.0 / (period + 1)
                    for i in range(period, len(data)):
                        ema_arr[i] = data[i] * mult + ema_arr[i - 1] * (1 - mult)
                    return ema_arr

                ema21 = _ema(closes, 21)
                ema55 = _ema(closes, 55)
                ema100 = _ema(closes, 100) if len(closes) >= 100 else None

                indicator_series["ema21"] = [{"time": t, "value": round(float(v), 6)} for t, v in zip(times, ema21) if not np.isnan(v)]
                indicator_series["ema55"] = [{"time": t, "value": round(float(v), 6)} for t, v in zip(times, ema55) if not np.isnan(v)]
                if ema100 is not None:
                    indicator_series["ema100"] = [{"time": t, "value": round(float(v), 6)} for t, v in zip(times, ema100) if not np.isnan(v)]

                # Bollinger Bands (20, 2)
                bb_period = 20
                if len(closes) >= bb_period:
                    bb_mid = np.full_like(closes, np.nan)
                    bb_upper = np.full_like(closes, np.nan)
                    bb_lower = np.full_like(closes, np.nan)
                    for i in range(bb_period - 1, len(closes)):
                        window = closes[i - bb_period + 1:i + 1]
                        m = np.mean(window)
                        s = np.std(window)
                        bb_mid[i] = m
                        bb_upper[i] = m + 2 * s
                        bb_lower[i] = m - 2 * s
                    indicator_series["bb_upper"] = [{"time": t, "value": round(float(v), 6)} for t, v in zip(times, bb_upper) if not np.isnan(v)]
                    indicator_series["bb_mid"] = [{"time": t, "value": round(float(v), 6)} for t, v in zip(times, bb_mid) if not np.isnan(v)]
                    indicator_series["bb_lower"] = [{"time": t, "value": round(float(v), 6)} for t, v in zip(times, bb_lower) if not np.isnan(v)]

                # RSI (14)
                rsi_period = 14
                if len(closes) > rsi_period:
                    deltas = np.diff(closes)
                    gains = np.where(deltas > 0, deltas, 0)
                    losses = np.where(deltas < 0, -deltas, 0)
                    avg_gain = np.full(len(closes), np.nan)
                    avg_loss = np.full(len(closes), np.nan)
                    avg_gain[rsi_period] = np.mean(gains[:rsi_period])
                    avg_loss[rsi_period] = np.mean(losses[:rsi_period])
                    for i in range(rsi_period + 1, len(closes)):
                        avg_gain[i] = (avg_gain[i-1] * (rsi_period - 1) + gains[i-1]) / rsi_period
                        avg_loss[i] = (avg_loss[i-1] * (rsi_period - 1) + losses[i-1]) / rsi_period
                    rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100)
                    rsi_vals = 100 - (100 / (1 + rs))
                    indicator_series["rsi"] = [{"time": t, "value": round(float(v), 2)} for t, v in zip(times, rsi_vals) if not np.isnan(v)]

                # Stochastic (14, 3, 3)
                stoch_period = 14
                if len(closes) >= stoch_period:
                    stoch_k = np.full(len(closes), np.nan)
                    for i in range(stoch_period - 1, len(closes)):
                        h = np.max(highs[i - stoch_period + 1:i + 1])
                        l = np.min(lows[i - stoch_period + 1:i + 1])
                        stoch_k[i] = ((closes[i] - l) / (h - l) * 100) if h != l else 50
                    # %D = 3-period SMA of %K
                    stoch_d = np.full_like(stoch_k, np.nan)
                    valid_k = [(i, v) for i, v in enumerate(stoch_k) if not np.isnan(v)]
                    for j in range(2, len(valid_k)):
                        idx = valid_k[j][0]
                        stoch_d[idx] = np.mean([valid_k[j-2][1], valid_k[j-1][1], valid_k[j][1]])
                    indicator_series["stoch_k"] = [{"time": t, "value": round(float(v), 2)} for t, v in zip(times, stoch_k) if not np.isnan(v)]
                    indicator_series["stoch_d"] = [{"time": t, "value": round(float(v), 2)} for t, v in zip(times, stoch_d) if not np.isnan(v)]

            except Exception as e:
                logger.warning(f"Failed to compute indicator series: {e}")

        # Compute EMA separation signals for chart markers
        ema_signals = []
        try:
            if len(candles) >= 100:
                # Import the EMA separation module  
                import sys
                import os
                current_dir = os.path.dirname(os.path.abspath(__file__))
                sys.path.insert(0, current_dir)
                from backtester.ema_separation import format_chart_signals
                
                # Candles are already normalized to {time, open, high, low, close} floats
                ema_signals = format_chart_signals(candles) if len(candles) >= 100 else []
                logger.info(f"EMA signals computed: {len(ema_signals)} markers for {instrument}")
        except Exception as e:
            import traceback
            logger.warning(f"Failed to compute EMA signals: {e}")
            logger.warning(traceback.format_exc())

        return jsonify({
            "instrument": instrument,
            "timeframe": timeframe,
            "candles": candles,
            "trades": trades,
            "indicators": indicator_series,
            "ema_signals": ema_signals,
        })

    # ------------------------------------------------------------------
    # GET /api/trading/open-trades
    # Returns open trades from OANDA with entry, SL, TP, unrealized P&L
    # ------------------------------------------------------------------
    @app.route("/api/trading/kronos-activity", methods=["GET", "OPTIONS"])
    def api_trading_kronos_activity():
        """Recent kronos_hunter_signal events with action + reason.

        2026-04-23: Kronos signal blocks (session blackout, counter_momentum, 4-rule
        filter, etc.) were invisible to the dashboard. This endpoint surfaces them
        so Tim can see WHY kronos isn't firing.

        Returns list of the last 30 min of kronos signals with action + reason,
        plus aggregated counts by action type.
        """
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        try:
            import sqlite3 as _sq3
            import os
            from pathlib import Path
            _fr_path = Path(__file__).resolve().parent / "flight_recorder.db"
            if not _fr_path.exists():
                return jsonify({"events": [], "counts": {}, "error": "no flight_recorder.db"})
            _c = _sq3.connect(str(_fr_path), timeout=3)
            _c.row_factory = _sq3.Row
            rows = _c.execute("""
                SELECT timestamp, pair,
                       json_extract(data,'$.direction') AS direction,
                       json_extract(data,'$.action') AS action,
                       json_extract(data,'$.reason') AS reason,
                       json_extract(data,'$.drift_pips') AS drift_pips,
                       json_extract(data,'$.confidence') AS confidence,
                       json_extract(data,'$.fan_direction') AS fan_direction
                FROM flight_log
                WHERE stage='kronos_hunter_signal'
                  AND timestamp >= datetime('now','-30 minutes')
                ORDER BY timestamp DESC
                LIMIT 100
            """).fetchall()
            _c.close()
            from collections import Counter
            events = [dict(r) for r in rows]
            counts = Counter(e["action"] or "unknown" for e in events)
            return jsonify({
                "events": events,
                "counts": dict(counts),
                "total": len(events),
            })
        except Exception as _e:
            return jsonify({"events": [], "counts": {}, "error": str(_e)})

    @app.route("/api/trading/open-trades", methods=["GET", "OPTIONS"])
    def api_trading_open_trades():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        trades = []
        oanda_call_succeeded = False
        try:
            bc = _get_broker_credentials()
            conn = bc.get_connection(user_info["user_id"], "oanda")
            if conn.get("configured"):
                import requests as http_requests
                headers = {"Authorization": f"Bearer {conn['api_key']}"}
                r = http_requests.get(
                    f"{conn['base_url']}/v3/accounts/{conn['account_id']}/openTrades",
                    headers=headers, timeout=4,
                )
                if r.status_code == 200:
                    oanda_call_succeeded = True  # 200 = authoritative — empty means no open trades
                    for t in r.json().get("trades", []):
                        trade = {
                            "id": t.get("id"),
                            "instrument": t.get("instrument"),
                            "direction": "buy" if int(t.get("currentUnits", 0)) > 0 else "sell",
                            "units": abs(int(t.get("currentUnits", 0))),
                            "entry_price": float(t.get("price", 0)),
                            "unrealizedPL": float(t.get("unrealizedPL", 0)),
                            "openTime": t.get("openTime"),
                        }
                        # Stop loss
                        sl = t.get("stopLossOrder", {})
                        if sl:
                            trade["stop_loss"] = float(sl.get("price", 0))
                        # Take profit
                        tp = t.get("takeProfitOrder", {})
                        if tp:
                            trade["take_profit"] = float(tp.get("price", 0))
                        # Trailing stop
                        ts = t.get("trailingStopLossOrder", {})
                        if ts:
                            trade["trailing_stop_distance"] = float(ts.get("distance", 0))
                        trades.append(trade)
        except Exception as e:
            logger.error(f"Failed to fetch open trades: {e}")

        uid = user_info["user_id"]
        if trades:
            # Update cache with fresh data
            _open_trades_cache[uid] = {"trades": trades, "ts": _t.time()}
        elif oanda_call_succeeded:
            # OANDA confirmed zero open trades — clear stale cache so UI reflects reality
            if uid in _open_trades_cache:
                logger.info("[open-trades] OANDA returned 0 trades — clearing stale cache for user %s", uid)
                del _open_trades_cache[uid]
        elif uid in _open_trades_cache:
            # OANDA call failed (network/timeout) — serve cache rather than showing empty
            cached_trades = _open_trades_cache[uid]["trades"]
            if cached_trades:
                logger.info(f"Returning {len(cached_trades)} cached open trades for user {uid} (OANDA unreachable)")
                return jsonify({"trades": cached_trades, "cached": True})

        return jsonify({"trades": trades})

    # ------------------------------------------------------------------
    # POST /api/trading/close-trade
    # Manually close (or partially close) an open trade
    # ------------------------------------------------------------------
    @app.route("/api/trading/close-trade", methods=["POST", "OPTIONS"])
    def api_trading_close_trade():
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        data = request.get_json(silent=True) or {}
        trade_id = data.get("trade_id")
        if not trade_id:
            return jsonify({"error": "trade_id is required"}), 400

        units = data.get("units")  # None = close all

        try:
            bc = _get_broker_credentials()
            conn = bc.get_connection(user_info["user_id"], "oanda")
            if not conn.get("configured"):
                return jsonify({"error": "OANDA not connected"}), 400

            import requests as http_requests
            headers = {
                "Authorization": f"Bearer {conn['api_key']}",
                "Content-Type": "application/json",
            }
            body = {}
            if units is not None:
                body["units"] = str(units)

            r = http_requests.put(
                f"{conn['base_url']}/v3/accounts/{conn['account_id']}/trades/{trade_id}/close",
                headers=headers, json=body if body else {"units": "ALL"}, timeout=10,
            )
            if r.status_code in (200, 201):
                resp = r.json()
                fill = resp.get("orderFillTransaction", {})
                realized_pl = float(fill.get("pl", 0))
                close_price = float(fill.get("price", 0)) if fill.get("price") else None

                # Capture exit data in live_trades (unified table)
                try:
                    from db_pool import get_trading_forex as _gtf_exit
                    _exit_conn = _gtf_exit()
                    _exit_conn.row_factory = sqlite3.Row
                    mt = _exit_conn.execute(
                        "SELECT * FROM live_trades WHERE oanda_trade_id = ? OR id = ?",
                        (str(trade_id), str(trade_id))
                    ).fetchone()
                    if mt:
                        mt = dict(mt)
                    if mt and mt.get("result") is None:

                        # Classify and record in setup_revenue so lifetime stats grow
                        try:
                            import sys, os as _os
                            _src = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)))
                            if _src not in sys.path:
                                sys.path.insert(0, _src)
                            from setup_classifier import classify_setups, get_best_setups
                            from setup_revenue import SetupRevenueTracker

                            # Get pair and direction from manual trade record
                            _pair = mt.get('pair', mt.get('instrument', ''))
                            _direction = mt.get('direction', 'buy')
                            _entry_price = float(mt.get('entry_price', 0))

                            # Classify using current M15 indicators
                            _setup_name = 'unknown'
                            try:
                                from oanda_client import OandaClient
                                from backtester.sniper_v4 import add_enhanced_indicators
                                import pandas as pd
                                from broker_credentials import BrokerCredentials as _BC_tmp
                                _bc_tmp = _BC_tmp().get_connection(user_id=uid, broker="oanda")
                                _api_key = _bc_tmp.get("api_key", "")
                                _base_url_tmp = _bc_tmp.get("base_url", "https://api-fxpractice.oanda.com")
                                with OandaClient(_api_key, _base_url_tmp) as _oc:
                                    _candles = _oc.get_candles(_pair, granularity="M15", count=200)
                                    if isinstance(_candles, dict):
                                        _candles = _candles.get('candles', [])
                                    if _candles and len(_candles) > 50:
                                        _rows = [{'open': float(c['mid']['o']), 'high': float(c['mid']['h']),
                                                  'low': float(c['mid']['l']), 'close': float(c['mid']['c']),
                                                  'volume': int(c.get('volume', 0))} for c in _candles]
                                        _df = pd.DataFrame(_rows)
                                        _df = add_enhanced_indicators(_df)
                                        _lt = _df.iloc[-1]
                                        _ind = {k: _lt.get(k, 0) for k in [
                                            'rsi', 'stoch_k', 'stoch_d', 'adx', 'bb_upper', 'bb_lower',
                                            'bb_mid', 'bb_width', 'close', 'ema_21', 'ema_55', 'ema_100', 'atr']}
                                        _ind.update({'macd_value': _lt.get('macd', 0), 'macd_signal': _lt.get('macd_signal', 0),
                                                    'macd_hist': _lt.get('macd_hist', 0)})
                                        _cls = classify_setups(indicators=_ind, candle_patterns={}, chart_patterns=[])
                                        if _cls:
                                            _best = get_best_setups(_cls, min_confidence=0.50, max_results=1)
                                            if _best:
                                                _setup_name = _best[0]['setup']
                            except Exception as _ce:
                                logger.debug(f"Setup classifier in manual close: {_ce}")

                            # Record to setup_revenue
                            _sl = float(mt.get('stop_loss', 0))
                            _pip_size = 0.01 if 'JPY' in _pair else 0.0001
                            _pnl_pips = (((close_price or 0) - _entry_price) / _pip_size) if _direction == 'buy' \
                                else ((_entry_price - (close_price or 0)) / _pip_size)
                            _risk_pips = abs(_entry_price - _sl) / _pip_size if _sl else 0
                            _r_mult = _pnl_pips / _risk_pips if _risk_pips > 0 else 0

                            tracker = SetupRevenueTracker()
                            _rev_result = tracker.record_trade(
                                trade_id=str(trade_id), setup_name=_setup_name,
                                pair=_pair, direction=_direction,
                                pnl_pips=_pnl_pips, pnl_usd=realized_pl,
                                entry_price=_entry_price, exit_price=close_price or 0,
                                stop_loss=_sl, take_profit=float(mt.get('take_profit', 0)),
                                units=float(mt.get('units', 0)),
                                r_multiple=_r_mult, duration_minutes=0,
                                source='manual',
                            )
                            logger.info(f"📊 Manual trade {trade_id} → {_setup_name}: ${realized_pl:+.2f} | "
                                       f"Lifetime: {_rev_result['total_trades']} trades, ${_rev_result['total_usd']:+.2f}"
                                       f"{' 🎯 PROMOTED!' if _rev_result.get('promotion_action') == 'promoted' else ''}")
                        except Exception as _re:
                            logger.debug(f"Setup revenue recording for manual trade: {_re}")

                        # Update live_trades with exit data (single canonical UPDATE)
                        try:
                            _outcome = 'win' if realized_pl > 0 else ('loss' if realized_pl < 0 else 'breakeven')
                            _lt2_conn = _gtf_exit()
                            _lt2_conn.execute("""
                                UPDATE live_trades SET
                                    exit_time = ?, exit_price = ?, result = ?, outcome = ?,
                                    status = 'closed',
                                    pips = ?, pnl_pips = ?, pnl_usd = ?, outcome_pips = ?,
                                    realized_pl = ?, risk_reward_actual = ?,
                                    exit_reason = ?, exit_trigger = 'manual_close',
                                    setup = CASE WHEN setup = 'unknown' THEN ? ELSE setup END,
                                    base_setup = CASE WHEN base_setup = 'unknown' THEN ? ELSE base_setup END,
                                    max_favorable_pips = ?, max_adverse_pips = ?
                                WHERE oanda_trade_id = ? OR id = ?
                            """, (
                                datetime.now(timezone.utc).isoformat(), close_price,
                                _outcome, _outcome,
                                round(_pnl_pips, 2), round(_pnl_pips, 2), round(realized_pl, 4),
                                round(_pnl_pips, 2), round(realized_pl, 4), _r_mult,
                                'manual_close', _setup_name, _setup_name,
                                float(mt.get('max_favorable_pips', 0) or 0),
                                float(mt.get('max_adverse_pips', 0) or 0),
                                str(trade_id), str(trade_id),
                            ))
                            _lt2_conn.commit()
                            logger.info(f"📊 live_trades closed: {trade_id} → {_outcome} {_pnl_pips:+.1f} pips ${realized_pl:+.2f}")
                        except Exception as _lt2_err:
                            logger.debug(f"live_trades exit update: {_lt2_err}")
                except Exception as me:
                    logger.debug(f"Manual trade exit capture: {me}")

                return jsonify({
                    "success": True,
                    "trade_id": trade_id,
                    "realized_pl": realized_pl,
                    "close_price": close_price,
                    "setup": _setup_name if '_setup_name' in dir() else 'unknown',
                })
            else:
                return jsonify({"error": f"OANDA error: {r.text}"}), r.status_code
        except Exception as e:
            logger.error(f"Failed to close trade {trade_id}: {e}")
            return jsonify({"error": str(e)}), 500

    # ------------------------------------------------------------------
    # Trading Team Management — workspace + agents + cycle runner
    # ------------------------------------------------------------------

    # Per-user team state: {user_id: {initialized, team_setup, running, ...}}
    _user_teams = {}

    # ── Chat conversation history (in-memory, per user) ──────────────────
    # Persists for the lifetime of the server process.
    # key: user_id (int), value: list of {"role": ..., "content": ...}
    _chat_histories: dict = {}
    _CHAT_HISTORY_TURNS = 8  # keep last 8 back-and-forth turns (16 messages)

    def _load_api_key() -> str:
        """Load Anthropic API key from file, fallback to env."""
        _key_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "API", "CLAUDE_API_KEY.txt")
        try:
            with open(_key_path) as _kf:
                return _kf.read().strip()
        except Exception:
            return _os.environ.get("ANTHROPIC_API_KEY", "")

    def _build_cycle_context() -> str:
        """Build recent cycle context block from DB."""
        lines = []
        try:
            import sqlite3 as _sqlite3
            with _sqlite3.connect(_TRADING_FOREX_DB, timeout=5, isolation_level=None) as _conn:
                _conn.row_factory = _sqlite3.Row
                cur = _conn.execute(
                    "SELECT pair, validator_verdict, validator_confidence, validator_reasoning, created_at "
                    "FROM trade_decisions ORDER BY created_at DESC LIMIT 5"
                )
                rows = cur.fetchall()
                if rows:
                    lines.append("RECENT CYCLES:")
                    for r in rows:
                        lines.append(
                            f"  {r['pair']}: {r['validator_verdict']} "
                            f"(conf={r['validator_confidence'] or 0:.0%}) — "
                            f"{str(r['validator_reasoning'] or '')[:100]}"
                        )
        except Exception as _db_exc:
            lines.append(f"(DB unavailable: {_db_exc})")
        try:
            from Source.agents.watch_manager import get_active_watches
            watches = get_active_watches() or []
            if watches:
                lines.append(f"ACTIVE WATCHES: {len(watches)}")
                for w in watches[:3]:
                    lines.append(f"  {w.get('instrument','?')}: {str(w.get('conditions',''))[:80]}")
        except Exception:
            pass
        return "\n".join(lines) if lines else "No recent data."

    def _direct_orchestrator_chat(message: str, user_id: int, team,
                                    pair_hint: str = "",
                                    chart_annotation_id: int = None) -> str:
        """
        Intent-routed orchestrator chat.
        Routes to: market confirmation, watch creation, cycle trigger, annotation, or general local model response.
        Falls back to error string on local model failure — never silently bills cloud API.
        pair_hint: fallback pair when intent parser can't extract one from the message text.
        Maintains per-user conversation history for context continuity.
        """
        import anthropic as _anthropic
        import urllib.request as _ureq
        import json as _ujson

        # ── Conversation history ─────────────────────────────────────────
        history = _chat_histories.setdefault(user_id, [])

        def _record_turn(user_msg: str, assistant_reply: str):
            """Append a turn to in-memory history, cap at _CHAT_HISTORY_TURNS pairs."""
            history.append({"role": "user", "content": user_msg})
            history.append({"role": "assistant", "content": assistant_reply})
            max_msgs = _CHAT_HISTORY_TURNS * 2
            if len(history) > max_msgs:
                _chat_histories[user_id] = history[-max_msgs:]

        def _general_response(msg: str) -> str:
            """Contextual orchestrator response using conversation history."""
            context = _build_cycle_context()
            system_prompt = (
                "You are the cycle orchestrator for an OANDA forex trading bot. "
                "You are the user's direct link to the trading team. "
                "You have memory of this conversation — reference prior messages when relevant. "
                "Respond conversationally and directly — no fluff. "
                "If the user describes a market setup, interpret it in terms of EMA fan state, "
                "BB expansion, and whether the validator would CONFIRM/WATCH/REJECT. "
                "IMPORTANT: Do NOT tell the user you will set a snipe or watch — you cannot create them. "
                "Snipes come from the validator only. If the user asks for a snipe, tell them to say "
                "'set a snipe for [pair]' in chat and the system will create one directly. "
                "If you don't have current data, say so and suggest running a cycle. "
                "Be concise — 2-4 sentences unless more detail is asked for.\n\n"
                f"SYSTEM CONTEXT:\n{context}"
            )
            messages_for_llm = list(history) + [{"role": "user", "content": msg}]
            try:
                _payload = _ujson.dumps({
                    "model": "mlx-community/Qwen3.5-9B-4bit",
                    "messages": [{"role": "system", "content": system_prompt}] + messages_for_llm,
                    "max_tokens": 512,
                    "temperature": 0.3,
                    "stop": ["</think>"],
                }).encode()
                _req = _ureq.Request("http://127.0.0.1:11500/chat/completions",
                                     data=_payload, headers={"Content-Type": "application/json"})
                with _ureq.urlopen(_req, timeout=20) as _r:
                    _res = _ujson.loads(_r.read())
                return _res["choices"][0]["message"]["content"]
            except Exception as _mlx_err:
                logger.error("[chat] Local 9B failed (%s) — returning error (no Haiku fallback)", _mlx_err)
                return f"[Local model unavailable: {_mlx_err}]"

        def _create_user_watch(pair: str, direction: str, price: float | None,
                               user_thesis: str, user_id_: int,
                               chart_annotation_id: int = None) -> dict:
            """
            Create a real watch_suggestions record from a user-requested snipe/watch.
            Returns {"watch_id": int, "conditions": list, "msg": str}
            """
            import uuid as _uuid
            from datetime import timedelta as _td

            _now = datetime.now(timezone.utc)
            _expires = _now + _td(hours=12)
            _cycle_id = f"user_watch_{_uuid.uuid4().hex[:8]}"

            # Build conditions
            conditions = []
            if price and direction:
                field = "ask" if direction.upper() == "BUY" else "bid"
                op = "<=" if direction.upper() == "BUY" else ">="
                conditions.append({"field": field, "op": op, "value": price,
                                   "description": f"Price {op} {price}"})
            elif direction:
                fan_val = "bullish_expanding" if direction.upper() == "BUY" else "bearish_expanding"
                conditions.append({"field": "fan_state", "op": "in", "value": [fan_val],
                                   "description": f"Fan {fan_val}"})
            # Always require sniper confirmation
            conditions.append({"field": "sniper_score", "op": ">=", "value": 12,
                               "description": "Sniper ≥ 12"})

            # Auto-detect direction from live sniper if not specified
            _detected_buy = 0
            _detected_sell = 0
            if not direction:
                try:
                    import sys as _sys_uw
                    _sys_uw.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Source"))
                    from Source.backtester.sniper_v4 import score_v4 as _uw_sv4
                    from Source.backtester.ema_separation import generate_market_picture as _uw_gmp
                    _uw_mp = _uw_gmp(pair, "M15")
                    if _uw_mp:
                        _uw_sc = _uw_sv4(_uw_mp)
                        _detected_buy  = _uw_sc.get("buy", 0)
                        _detected_sell = _uw_sc.get("sell", 0)
                        if _detected_buy > _detected_sell:
                            direction = "BUY"
                            logger.info("[watch] Auto-detected direction=BUY for %s (sniper buy=%s > sell=%s)",
                                        pair, _detected_buy, _detected_sell)
                        elif _detected_sell > _detected_buy:
                            direction = "SELL"
                            logger.info("[watch] Auto-detected direction=SELL for %s (sniper sell=%s > buy=%s)",
                                        pair, _detected_sell, _detected_buy)
                        else:
                            logger.info("[watch] Could not auto-detect direction for %s (buy=%s sell=%s tied) — storing neutral",
                                        pair, _detected_buy, _detected_sell)
                except Exception as _uw_err:
                    logger.debug("[watch] Direction auto-detection failed for %s: %s", pair, _uw_err)

            context_obj = {
                "source": "user_chat",
                "user_thesis": user_thesis,
                "user_id": user_id_,
                "direction": direction,
                "entry_price": price,
                "sniper_buy": _detected_buy,
                "sniper_sell": _detected_sell,
            }

            try:
                conn = get_trading_forex()
                # Ensure user_thesis column exists
                try:
                    conn.execute("ALTER TABLE watch_suggestions ADD COLUMN user_thesis TEXT DEFAULT ''")
                    conn.commit()
                except Exception:
                    pass

                cursor = conn.execute("""
                    INSERT INTO watch_suggestions
                    (cycle_id, instrument, suggestion_type, conditions, raw_suggestion,
                     validator_verdict, validator_confidence, created_at, expires_at,
                     status, workspace_task_id, context, user_thesis, origin_type, chart_annotation_id)
                    VALUES (?, ?, 'user_requested', ?, ?, 'WATCH', 0.7, ?, ?, 'watching', NULL, ?, ?, 'chart', ?)
                """, (
                    _cycle_id, pair,
                    json.dumps(conditions),
                    user_thesis[:500],
                    _now.isoformat(),
                    _expires.isoformat(),
                    json.dumps(context_obj),
                    user_thesis[:500],
                    chart_annotation_id,
                ))
                watch_id = cursor.lastrowid
                conn.commit()
                logger.info("[chat] Created user watch #%d for %s %s thesis=%s",
                            watch_id, pair, direction, user_thesis[:60])
                return {"watch_id": watch_id, "conditions": conditions, "ok": True}
            except Exception as _we:
                logger.error("[chat] Failed to create user watch: %s", _we)
                return {"watch_id": None, "conditions": conditions, "ok": False, "error": str(_we)}

        try:
            # Parse intent — pure Python, no LLM, instant
            try:
                from Source.chat_intent_parser import parse_intent
            except ImportError:
                from chat_intent_parser import parse_intent

            intent = parse_intent(message)
            # Apply pair_hint when the message text has no extractable pair (e.g. "set a snipe for this")
            if not intent.pair and pair_hint:
                intent.pair = pair_hint
                logger.info(f"[chat] pair_hint applied: intent.pair set to {pair_hint}")
            logger.info(f"[chat] intent={intent.type} pair={intent.pair} dir={intent.direction}")

            # ── CONFIRM_SETUP: user describes what they see on chart ──
            if intent.type == "CONFIRM_SETUP" and intent.pair:
                try:
                    from Source.market_confirmation import confirm_setup
                except ImportError:
                    from market_confirmation import confirm_setup
                result = confirm_setup(
                    pair=intent.pair,
                    user_description=message,
                    annotations=intent.annotations,
                    direction=intent.direction,
                )
                _reply = result.get("response_text") or _general_response(message)
                _record_turn(message, _reply)
                return _reply

            # ── QUERY: user asking for live market state ──
            elif intent.type == "QUERY" and intent.pair:
                try:
                    from Source.market_confirmation import get_market_snapshot
                except ImportError:
                    from market_confirmation import get_market_snapshot
                result = get_market_snapshot(intent.pair)
                _reply = result.get("response_text") or _general_response(message)
                _record_turn(message, _reply)
                return _reply

            # ── RUN_CYCLE: user wants immediate cycle on a pair ──
            elif intent.type == "RUN_CYCLE" and intent.pair:
                try:
                    import json as _json, urllib.request as _ureq
                    _payload = _json.dumps({
                        "pair": intent.pair, "source": "user_chat",
                        "scout_context": {"user_thesis": message, "triggered_by": "snipe"},
                    }).encode()
                    _req = _ureq.Request("http://localhost:8766/api/trading/run-cycle",
                                         data=_payload, headers={"Content-Type": "application/json"}, method="POST")
                    with _ureq.urlopen(_req, timeout=5): pass
                    _reply = f"🔄 Queuing cycle for {intent.pair.replace('_','/')} now. Results in ~60 seconds."
                except Exception as _ce:
                    _reply = f"Couldn't queue cycle for {intent.pair}: {_ce}"
                _record_turn(message, _reply)
                return _reply

            # ── CLOSE_TRADE: user wants to close open position ──
            elif intent.type == "CLOSE_TRADE" and intent.pair:
                try:
                    import sqlite3 as _sq
                    with _sq.connect(_TRADING_FOREX_DB, timeout=5, isolation_level=None) as _conn:
                        row = _conn.execute(
                            "SELECT trade_id, units FROM live_trades WHERE instrument=? AND status='open' AND user_id=? ORDER BY open_time DESC LIMIT 1",
                            (intent.pair, user_id)
                        ).fetchone()
                    if row:
                        trade_id, units = row
                        from Source.oanda_client import OandaClient
                        from broker_credentials import BrokerCredentials as _BC_vc
                        _vc_conn = _BC_vc().get_connection(user_id=user_id, broker="oanda")
                        oanda_key = _vc_conn.get("api_key", "")
                        _vc_url   = _vc_conn.get("base_url", "https://api-fxpractice.oanda.com")
                        with OandaClient(oanda_key, _vc_url) as oc:
                            oc.close_trade(trade_id)
                        _reply = f"✅ Closed {intent.pair.replace('_','/')} trade #{trade_id}."
                    else:
                        _reply = f"No open {intent.pair.replace('_','/')} trade found for your account."
                except Exception as _cl_err:
                    _reply = f"Couldn't close trade: {_cl_err}"
                _record_turn(message, _reply)
                return _reply

            # ── SET_WATCH: user wants to monitor a setup ──
            elif intent.type == "SET_WATCH" and intent.pair:
                # Build user thesis from annotations + message
                ann_notes = "; ".join(a.get("note","") for a in (intent.annotations or []) if a.get("note"))
                user_thesis = ann_notes or message[:300]
                direction_str = f" ({intent.direction})" if intent.direction else ""
                price_str = f" at {intent.price}" if intent.price else ""
                pair_display = intent.pair.replace('_','/')

                # Actually create the watch
                watch_result = _create_user_watch(
                    pair=intent.pair,
                    direction=intent.direction or "",
                    price=intent.price,
                    user_thesis=user_thesis,
                    user_id_=user_id,
                    chart_annotation_id=chart_annotation_id,
                )

                if watch_result["ok"]:
                    watch_id = watch_result["watch_id"]
                    conds = watch_result["conditions"]
                    cond_desc = " + ".join(c.get("description","?") for c in conds)
                    reply = (
                        f"✅ Watch #{watch_id} created for {pair_display}{direction_str}{price_str}. "
                        f"Monitoring: {cond_desc}. "
                        f"Your thesis: \"{user_thesis[:120]}\". "
                        f"I'll run a full validator cycle when conditions align — "
                        f"or say \"run a cycle on {pair_display}\" to check now."
                    )
                    # Also queue an immediate validation cycle
                    try:
                        import json as _json2, urllib.request as _ureq2
                        _p2 = _json2.dumps({
                            "pair": intent.pair, "source": "user_watch",
                            "scout_context": {"user_thesis": user_thesis, "watch_id": watch_id,
                                              "direction": intent.direction, "triggered_by": "snipe"},
                        }).encode()
                        _r2 = _ureq2.Request("http://localhost:8766/api/trading/run-cycle",
                                              data=_p2, headers={"Content-Type": "application/json"}, method="POST")
                        with _ureq2.urlopen(_r2, timeout=5): pass
                        reply += f"\n🔄 Running initial cycle now to validate your thesis."
                    except Exception as _qe:
                        logger.warning("[chat] Failed to queue cycle after watch creation: %s", _qe)
                else:
                    reply = (
                        f"⚠️ Watch creation for {pair_display} failed ({watch_result.get('error','?')}). "
                        f"Try: \"run a cycle on {pair_display}\" to validate manually."
                    )

                _record_turn(message, reply)
                return reply

            # ── ANNOTATE_TRADE: user giving feedback on a closed trade ──
            elif intent.type == "ANNOTATE_TRADE":
                # Phase 4 — for now acknowledge
                ann_type = intent.annotation_type or "feedback"
                pair_str = f" on {intent.pair.replace('_','/')}" if intent.pair else ""
                return (
                    f"📝 Feedback noted{pair_str}: {ann_type}. "
                    f"Trade annotation logging coming in Phase 4 — your input will train the validator."
                )

            # ── PAUSE/RESUME ──
            elif intent.type == "PAUSE":
                try:
                    from Source.agents import trading_cycle as _tc
                    _tc._trading_paused = True
                    return "⏸ Trading paused."
                except Exception:
                    return "Couldn't pause trading."
            elif intent.type == "RESUME":
                try:
                    from Source.agents import trading_cycle as _tc
                    _tc._trading_paused = False
                    return "▶️ Trading resumed."
                except Exception:
                    return "Couldn't resume trading."

            # ── GENERAL or fallback ──
            else:
                return _general_response(message)

        except Exception as e:
            logger.error(f"[chat] Intent routing failed, falling back to general: {e}", exc_info=True)
            try:
                return _general_response(message)
            except Exception as e2:
                return f"Chat unavailable: {e2}"

    def _get_user_team_state(user_id):
        """Get or create team state dict for a user."""
        if user_id not in _user_teams:
            _user_teams[user_id] = {
                "initialized": False,
                "team_setup": None,
                "workspace": None,
                "cycle_runner": None,
                "running": False,
                "running_count": 0,
                "running_pairs": set(),
                "running_contexts": {},    # {instrument: scout_context_summary}
                "last_cycle": None,
                "cycle_count": 0,
                "error": None,
                "notifications": [],       # Pending UI notifications
                "cycle_queue": [],          # Priority queue: [{"instrument":..., "priority":..., "source":...}]
                "current_cycle_pair": None, # Which pair is currently running
                "cycle_started_at": None,   # Timestamp when current cycle began
            }
        # Backfill for existing states missing new keys
        state = _user_teams[user_id]
        if "notifications" not in state:
            state["notifications"] = []
        if "cycle_queue" not in state:
            state["cycle_queue"] = []
        if "current_cycle_pair" not in state:
            state["current_cycle_pair"] = None
        if "cycle_started_at" not in state:
            state["cycle_started_at"] = None
        if "running_count" not in state:
            state["running_count"] = 0
        if "running_pairs" not in state:
            state["running_pairs"] = set()
        return state

    def _ensure_team_initialized(user_id):
        """Load user's team from registry into SwarmHandler.
        Returns (team_setup, error_string)."""
        state = _get_user_team_state(user_id)
        if state["initialized"] and state["team_setup"]:
            return state["team_setup"], None

        try:
            trading_bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            source_dir = os.path.join(trading_bot_dir, "Source")
            jarvis_dir = os.path.dirname(trading_bot_dir)
            for p in [source_dir, trading_bot_dir, jarvis_dir]:
                if p not in sys.path:
                    sys.path.insert(0, p)

            # Get user's team_id from their record
            from workspace_provisioner import get_user_team_id
            team_id = get_user_team_id(user_id)
            if not team_id:
                return None, f"Setting up your trading team — this may take a moment on first load."

            from Source.agents.team_setup import TradingTeamSetup
            from Source.journey_tracker import JourneyTracker
            tracker = JourneyTracker(_JOURNEYS_DB)
            team = TradingTeamSetup(team_id=team_id, tracker=tracker)
            result = team.setup_team()

            if result.get("agent_ids"):
                state["team_setup"] = team
                state["initialized"] = True
                state["error"] = None
                agent_names = list(result["agent_ids"].keys())
                logger.info(f"Team loaded for user {user_id}: {len(agent_names)} agents — {agent_names}")
                # Inject team's SwarmHandler into trading_cycle._swarm so floor_chat sees the same agents
                try:
                    import importlib
                    tc = importlib.import_module("Source.agents.trading_cycle")
                    if hasattr(team, "swarm") and team.swarm is not None:
                        tc._swarm = team.swarm
                        logger.info("Injected team swarm into trading_cycle._swarm")
                    elif hasattr(team, "_swarm") and team._swarm is not None:
                        tc._swarm = team._swarm
                        logger.info("Injected team._swarm into trading_cycle._swarm")
                except Exception as _inj_e:
                    logger.warning("Could not inject swarm into trading_cycle: %s", _inj_e)
                return team, None
            else:
                err = "setup_team returned no agent_ids"
                state["error"] = err
                return None, err

        except Exception as e:
            err = f"Team init failed: {e}"
            logger.error(err)
            state["error"] = err
            return None, err

    def _run_cycle_background(user_id, instrument, timeframe="M15", source="manual", scout_context=None):
        """Run a single trading cycle in the background thread.
        
        After completion, automatically dequeues the next pending cycle if any.
        source: 'manual' | 'scout' | 'snipe' — for notification context.
        scout_context: dict from Scout with setup_name, direction, win_rate, etc.
        """
        state = _get_user_team_state(user_id)

        # Helper for early-return paths — release the slot we acquired in
        # _queue_cycle / _dequeue_next_cycle (running_pairs.add + running_count++).
        # 2026-05-06: was missing on init-fail and stale-skip paths, leaving
        # ghost entries that cycles couldn't be re-dequeued past until the
        # team-status ghost cleanup ran. Pure leak fix, no behavior change.
        def _release_slot(reason: str = ""):
            try:
                state["running_count"] = max(0, state.get("running_count", 0) - 1)
                state.get("running_pairs", set()).discard(instrument)
                state.get("running_contexts", {}).pop(instrument, None)
                state.get("cycle_instances", {}).pop(instrument, None)
            except Exception:
                pass
            if reason:
                logger.info(f"Released slot for {instrument} ({reason})")

        team, err = _ensure_team_initialized(user_id)
        if err:
            state["error"] = err
            state["running"] = False
            state["current_cycle_pair"] = None
            state["cycle_started_at"] = None
            _release_slot("team_init_failed")
            _dequeue_next_cycle(user_id)
            return

        # ── Staleness guard: skip if scout context is too old ──
        STALE_THRESHOLD_SECONDS = 600  # 10 minutes
        if scout_context and source == "scout":
            queued_at = scout_context.get("queued_at")
            if queued_at and (time.time() - queued_at) > STALE_THRESHOLD_SECONDS:
                age_min = (time.time() - queued_at) / 60
                logger.warning(
                    f"Skipping stale scout cycle for {instrument} — "
                    f"queued {age_min:.1f}min ago (limit {STALE_THRESHOLD_SECONDS/60:.0f}min)"
                )
                if flight:
                    flight.record(FlightStage.QUEUE_DEQUEUE, pair=instrument, data={
                        "action": "skipped_stale", "age_seconds": round(time.time() - queued_at),
                        "source": source,
                    }, note=f"stale scout context ({age_min:.1f}min old) — skipped")
                state["running"] = False
                state["current_cycle_pair"] = None
                state["cycle_started_at"] = None
                _release_slot("stale_scout_context")
                _dequeue_next_cycle(user_id)
                return

        try:
            from Source.agents.comment_protocol import CommentProtocol
            from Source.agents import trading_cycle as _tc_mod
            from Source.agents.trading_cycle import TradingCycle

            # Inject profile engine into trading_cycle module (background thread can't use Flask context)
            if _profile_engine is not None and _tc_mod._shared_profile_engine is None:
                _tc_mod._shared_profile_engine = _profile_engine
                logger.info("Profile engine injected into trading_cycle (first cycle)")

            protocol = CommentProtocol()
            cycle = TradingCycle(team, protocol, user_id=user_id)
            state["running"] = True
            state["current_cycle_pair"] = instrument
            state["cycle_started_at"] = time.time()
            state["error"] = None
            state["cycle_instance"] = cycle  # Expose for live progress (legacy)
            state.setdefault("cycle_instances", {})[instrument] = cycle  # Per-pair live progress

            result = cycle.run_cycle(instrument, timeframe, scout_context=scout_context)

            result["_source"] = source  # Tag for dashboard
            result["_instrument"] = instrument
            state["last_cycle"] = result
            state.setdefault("last_cycles", {})[instrument] = result  # Per-pair results
            state["cycle_instance"] = None
            state.get("cycle_instances", {}).pop(instrument, None)
            state["cycle_count"] += 1
            state["running_count"] = max(0, state.get("running_count", 0) - 1)
            state.get("running_pairs", set()).discard(instrument)
            state.get("running_contexts", {}).pop(instrument, None)
            state["current_cycle_pair"] = None
            state["cycle_started_at"] = None
            logger.info(f"Cycle #{state['cycle_count']} for user {user_id} ({source}): {result.get('status', 'unknown')}")

            # Persist last cycle per pair for dashboard reload
            try:
                import json as _pjson
                _persist_dir = os.path.join(os.path.dirname(__file__), '..', 'dashboard', 'cycle_state')
                os.makedirs(_persist_dir, exist_ok=True)
                _persist_path = os.path.join(_persist_dir, f"{instrument}.json")
                with open(_persist_path, 'w') as _pf:
                    _pjson.dump(result, _pf, default=str)
            except Exception as _pe:
                logger.debug("Failed to persist cycle state for %s: %s", instrument, _pe)

            # Notify dashboard of cycle completion (all sources)
            _cycle_decision = "hold"
            _cycle_status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
            _skip_reason = result.get("skip_reason", "") if isinstance(result, dict) else ""
            _skip_detail = result.get("skip_detail", "") if isinstance(result, dict) else ""
            if isinstance(result, dict):
                _cd = result.get("decision", {})
                if isinstance(_cd, dict):
                    _cycle_decision = _cd.get("action", "hold")

            # ── Flight recorder: log snipe lifecycle events ──────────────
            if source == "snipe" and flight:
                _fr_watch_id = (scout_context or {}).get("_watch_id", "")
                if _cycle_status == "skipped":
                    flight.record("SNIPE_BLOCKED", pair=instrument, data={
                        "watch_id": _fr_watch_id,
                        "skip_reason": _skip_reason,
                        "skip_detail": _skip_detail,
                    }, note=f"Snipe blocked: {_skip_reason} — {_skip_detail}")
                elif _cycle_status == "complete":
                    _exec = result.get("execution", {}) if isinstance(result, dict) else {}
                    _trade_id = _exec.get("trade_id", "") if isinstance(_exec, dict) else ""
                    if _trade_id:
                        flight.record("SNIPE_OPENED", pair=instrument, data={
                            "watch_id": _fr_watch_id,
                            "trade_id": _trade_id,
                            "direction": (scout_context or {}).get("direction", ""),
                        }, note=f"Snipe opened trade {_trade_id}")
                    else:
                        flight.record("SNIPE_NO_ENTRY", pair=instrument, data={
                            "watch_id": _fr_watch_id,
                            "decision": _cycle_decision,
                        }, note=f"Snipe cycle complete but no trade opened (decision={_cycle_decision})")

            # If snipe was blocked, send snipe_blocked so UI can clear the flash
            if source == "snipe" and _cycle_status == "skipped" and _skip_reason:
                state["notifications"].append({
                    "type": "snipe_blocked",
                    "instrument": instrument,
                    "watch_id": (scout_context or {}).get("_watch_id", ""),
                    "skip_reason": _skip_reason,
                    "skip_detail": _skip_detail,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

            state["notifications"].append({
                "type": "cycle_complete",
                "source": source,
                "instrument": instrument,
                "status": _cycle_status,
                "decision": _cycle_decision,
                "skip_reason": _skip_reason,
                "skip_detail": _skip_detail,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            # ── Link cycle back to watch_suggestion when source=snipe ──
            # Updates trade_cycle_id so the watch stops re-firing on CONFIRM/REJECT.
            # On validator WATCH the cycle_id stays NULL → watch keeps re-firing.
            if source == "snipe" and isinstance(result, dict):
                try:
                    _watch_id = (scout_context or {}).get("_watch_id")
                    _cycle_id = result.get("cycle_id") or result.get("task_id")
                    _val_verdict = ""
                    _v = result.get("validation", {})
                    if isinstance(_v, dict):
                        _val_verdict = (_v.get("verdict") or "").upper()
                    # snipe_direct trades set execution in result but never set decision.action,
                    # so also detect entry via execution dict or status=complete with execution data
                    _entered = _cycle_decision in ("enter", "buy", "sell")
                    if not _entered and isinstance(result.get("execution"), dict):
                        # snipe_direct path: has execution with trade_id = trade entered
                        _entered = bool(result["execution"].get("trade_id"))
                    _rejected = _val_verdict == "REJECT"
                    if _watch_id and _entered:
                        # ENTERED → link cycle + stop re-firing (trade is open)
                        from db_pool import get_trading_forex as _gtf_snipe
                        _bc_snipe = _gtf_snipe()
                        _bc_snipe.execute(
                            "UPDATE watch_suggestions SET trade_cycle_id=? WHERE id=? AND trade_cycle_id IS NULL",
                            (str(_cycle_id or "linked"), _watch_id)
                        )
                        _bc_snipe.commit()
                        logger.info(
                            "🔗 Watch #%s → cycle %s (ENTERED — stopping re-fire)",
                            _watch_id, _cycle_id
                        )
                    elif _watch_id and _rejected:
                        # REJECTED → reset to watching so it can re-fire next time conditions hit
                        from db_pool import get_trading_forex as _gtf_snipe
                        _bc_snipe = _gtf_snipe()
                        _bc_snipe.execute(
                            "UPDATE watch_suggestions SET status='watching', triggered_at=NULL, trade_cycle_id=NULL WHERE id=?",
                            (_watch_id,)
                        )
                        _bc_snipe.commit()
                        logger.info(
                            "🔄 Watch #%s → cycle %s REJECTED — reset to watching (will re-fire)",
                            _watch_id, _cycle_id
                        )
                    elif _watch_id:
                        # FIX 2026-03-27: Reset watch to watching with NULL cycle_id
                        # so it re-enters the check_active_watches query.
                        # Previously the watch stayed triggered with a stale trade_cycle_id
                        # from a prior fill — the query filters "triggered AND cycle_id IS NULL"
                        # so the watch was permanently dead after a gate-blocked re-trigger.
                        try:
                            from db_pool import get_trading_forex as _gtf_reset
                            _bc_reset = _gtf_reset()
                            _bc_reset.execute(
                                "UPDATE watch_suggestions SET status='watching', "
                                "trade_cycle_id=NULL, triggered_at=NULL WHERE id=?",
                                (_watch_id,)
                            )
                            _bc_reset.commit()
                            logger.info(
                                "🔄 Watch #%s cycle complete: verdict=%s, no entry — "
                                "reset to watching (cycle_id cleared, will re-fire)",
                                _watch_id, _val_verdict or "WATCH"
                            )
                        except Exception as _reset_err:
                            logger.warning("Watch #%s reset failed: %s", _watch_id, _reset_err)
                            logger.info(
                                "⏳ Watch #%s cycle complete: validator=%s — will re-fire next check",
                                _watch_id, _val_verdict or "WATCH"
                            )
                except Exception as _link_err:
                    logger.debug("Snipe cycle link-back failed: %s", _link_err)

        except Exception as e:
            state["running_count"] = max(0, state.get("running_count", 0) - 1)
            state.get("running_pairs", set()).discard(instrument)
            state.get("running_contexts", {}).pop(instrument, None)
            state.get("cycle_instances", {}).pop(instrument, None)
            state["current_cycle_pair"] = None
            state["cycle_started_at"] = None
            state["error"] = str(e)
            logger.error(f"Cycle error for user {user_id}: {e}")

        # Dequeue next cycle
        _dequeue_next_cycle(user_id)

    MAX_QUEUE_DEPTH = 5  # Max concurrent trades = max queue depth

    def _scout_score(scout_context):
        """Compute queue priority score from scout context. Higher = better."""
        if not scout_context or not isinstance(scout_context, dict):
            return 0.0
        confidence = scout_context.get("scout_confidence", 0) or 0
        profile_boost = scout_context.get("market_snapshot", {}).get("profile_confidence", 0) or 0
        session_quality = scout_context.get("market_snapshot", {}).get("session_quality", 0) or 0
        win_rate = (scout_context.get("win_rate", 80) or 80) / 100.0
        return confidence * (1 + profile_boost) * (1 + session_quality * 0.5) * win_rate

    def _queue_cycle(user_id, instrument, priority="normal", source="manual", scout_context=None):
        """Queue a cycle with smart scoring. Max 5 queued — weakest gets replaced by stronger.

        Snipes (priority='high' or source='snipe') BYPASS the queue entirely —
        direct-to-trade only, fired immediately on _BACKGROUND_EXECUTOR. They
        never share queue slots with scout/manual cycles, never block on
        running_pairs, and never get routed through the validator.

        Scout/manual cycles compete on score and respect MAX_CONCURRENT_CYCLES.
        Returns: 'started' | 'queued' | 'replaced' | 'already_running' | 'too_weak'
        """
        state = _get_user_team_state(user_id)

        # Defined here (was nested below) so the snipe-bypass block can call it
        # without UnboundLocalError. Same body, just hoisted.
        def _store_running_context(st, inst, sc):
            ctx = sc or {}
            stored = {
                "direction": (ctx.get("direction") or "").upper(),
                "setup_name": ctx.get("setup_name", ""),
                "setup_id": ctx.get("setup_id", ""),
                "win_rate": ctx.get("win_rate"),
                "scout_confidence": ctx.get("scout_confidence"),
                "score": ctx.get("score", 0),
            }
            st.setdefault("running_contexts", {})[inst] = stored
            logger.info(f"[RUNNING_CTX] {inst}: {stored}")

        # ── SNIPE QUEUE BYPASS (2026-05-07) ───────────────────────────
        # Snipes never queue. They fire immediately into SNIPE_DIRECT (10 gates)
        # via _BACKGROUND_EXECUTOR with source="snipe". Without this bypass, a
        # snipe arriving while a scout cycle was queued for the same pair got
        # absorbed into that scout entry (line ~2085), then ran as a validator
        # cycle when dequeued — validator returned SKIP and the trade was lost.
        # Multiple trades on the same pair (manual + snipe) are allowed by design.
        if priority == "high" or source == "snipe":
            state["running_count"] = state.get("running_count", 0) + 1
            state.setdefault("running_pairs", set()).add(instrument)
            _store_running_context(state, instrument, scout_context)
            if flight:
                flight.record(FlightStage.QUEUE_ENTER, pair=instrument, data={
                    "position": 0, "source": source,
                    "score": _scout_score(scout_context),
                    "action": "started_immediately_snipe",
                }, note=f"{source} → snipe direct (queue bypass)")
            _BACKGROUND_EXECUTOR.submit(
                _run_cycle_background, user_id, instrument, "M15", source,
                scout_context=scout_context,
            )
            logger.info("⚡ Snipe %s started immediately (queue bypass, source=%s)",
                        instrument, source)
            return "started"

        # Don't duplicate — check if already running or queued for this pair
        if instrument in state.get("running_pairs", set()):
            return "already_running"
        for q in state["cycle_queue"]:
            if q["instrument"] == instrument:
                # Update with fresher scout context if score is higher
                new_score = _scout_score(scout_context)
                old_score = q.get("score", 0)
                if new_score > old_score and scout_context:
                    q["scout_context"] = scout_context
                    q["score"] = new_score
                    logger.info(f"Updated {instrument} in queue: score {old_score:.3f} → {new_score:.3f}")
                # Upgrade priority if snipe
                if priority == "high" and q["priority"] != "high":
                    q["priority"] = "high"
                    q["source"] = source
                return "queued"
        
        new_score = _scout_score(scout_context)
        entry = {"instrument": instrument, "priority": priority, "source": source,
                 "scout_context": scout_context, "score": new_score}

        if priority == "high":
            # Snipes always start immediately — no concurrency cap
            # NOTE 2026-05-07: this branch is now unreachable for snipes — the
            # bypass at the top of _queue_cycle handles all priority="high" /
            # source="snipe" calls. Kept as a safety net for any future caller
            # that passes priority="high" without source="snipe".
            state["running_count"] = state.get("running_count", 0) + 1
            state.setdefault("running_pairs", set()).add(instrument)
            _store_running_context(state, instrument, scout_context)
            if flight:
                flight.record(FlightStage.QUEUE_ENTER, pair=instrument, data={
                    "position": 0, "source": source, "score": new_score,
                    "action": "started_immediately_snipe",
                }, note=f"{source} → snipe started immediately (bypass concurrency)")
            _BACKGROUND_EXECUTOR.submit(
                _run_cycle_background, user_id, instrument, "M15", source,
                scout_context=scout_context,
            )
            return "started"
        elif state.get("running_count", 0) < MAX_CONCURRENT_CYCLES:
            # Regular scout cycle — start if slot available
            state["running_count"] = state.get("running_count", 0) + 1
            state.setdefault("running_pairs", set()).add(instrument)
            _store_running_context(state, instrument, scout_context)
            if flight:
                flight.record(FlightStage.QUEUE_ENTER, pair=instrument, data={
                    "position": 0, "source": source, "score": new_score,
                    "action": "started_immediately",
                }, note=f"{source} → started immediately")
            _BACKGROUND_EXECUTOR.submit(
                _run_cycle_background, user_id, instrument, "M15", source,
                scout_context=scout_context,
            )
            return "started"

        queue = state["cycle_queue"]

        # If queue has room, just add
        if len(queue) < MAX_QUEUE_DEPTH:
            if priority == "high":
                # Insert before all normal priority items
                idx = 0
                for i, q in enumerate(queue):
                    if q["priority"] != "high":
                        idx = i
                        break
                    idx = i + 1
                queue.insert(idx, entry)
            else:
                queue.append(entry)
            # Sort non-high by score descending (best first)
            high_items = [q for q in queue if q["priority"] == "high"]
            normal_items = sorted([q for q in queue if q["priority"] != "high"],
                                  key=lambda q: q.get("score", 0), reverse=True)
            queue.clear()
            queue.extend(high_items + normal_items)
            logger.info(f"Queued {source} cycle for {instrument} (score={new_score:.3f}, depth={len(queue)})")
            if flight:
                flight.record(FlightStage.QUEUE_ENTER, pair=instrument, data={
                    "position": len(queue), "source": source, "score": new_score,
                    "action": "queued", "queue_depth": len(queue),
                }, note=f"{source} → queued at position {len(queue)}")
            return "queued"
        
        # Queue full — snipes always get in (bump weakest normal)
        if priority == "high":
            # Remove weakest normal-priority item
            normal_items = [q for q in queue if q["priority"] != "high"]
            if normal_items:
                weakest = min(normal_items, key=lambda q: q.get("score", 0))
                queue.remove(weakest)
                queue.insert(0, entry)
                logger.info(f"Snipe {instrument} bumped {weakest['instrument']} (score {weakest.get('score', 0):.3f})")
                return "replaced"
            queue.insert(0, entry)
            return "queued"
        
        # Normal priority — replace weakest if we're stronger
        weakest = min(queue, key=lambda q: q.get("score", 0) if q["priority"] != "high" else 999)
        if weakest["priority"] != "high" and new_score > weakest.get("score", 0):
            logger.info(f"{instrument} (score={new_score:.3f}) replaces {weakest['instrument']} (score={weakest.get('score', 0):.3f})")
            queue.remove(weakest)
            queue.append(entry)
            # Re-sort
            high_items = [q for q in queue if q["priority"] == "high"]
            normal_items = sorted([q for q in queue if q["priority"] != "high"],
                                  key=lambda q: q.get("score", 0), reverse=True)
            queue.clear()
            queue.extend(high_items + normal_items)
            return "replaced"
        
        logger.info(f"{instrument} too weak (score={new_score:.3f}) — queue full with stronger setups")
        return "too_weak"

    CYCLE_TIMEOUT_SECONDS = 600  # 10 minutes — force-clear stuck cycles

    def _dequeue_next_cycle(user_id):
        """Pop the next cycle from the queue and run it.
        
        Includes stuck-cycle watchdog: if a cycle has been 'running' for longer
        than CYCLE_TIMEOUT_SECONDS, force-clear the state and dequeue anyway.
        """
        state = _get_user_team_state(user_id)

        # Stuck-cycle watchdog
        if state.get("running") and state.get("cycle_started_at"):
            elapsed = time.time() - state["cycle_started_at"]
            if elapsed > CYCLE_TIMEOUT_SECONDS:
                stuck_pair = state.get("current_cycle_pair", "unknown")
                logger.warning(
                    f"Stuck cycle detected for {stuck_pair} — running for {elapsed:.0f}s "
                    f"(limit {CYCLE_TIMEOUT_SECONDS}s). Force-clearing."
                )
                # For parallel cycles, just reset the counter - individual timeouts will handle themselves
                state["running_count"] = 0
                state["running_pairs"] = set()
                state["current_cycle_pair"] = None
                state["cycle_started_at"] = None
                state["cycle_instance"] = None
                if flight:
                    flight.record(FlightStage.QUEUE_DEQUEUE, pair=stuck_pair, data={
                        "action": "force_clear_stuck", "elapsed_seconds": round(elapsed),
                    }, note=f"stuck cycle force-cleared after {elapsed:.0f}s")

        if state.get("running_count", 0) >= MAX_CONCURRENT_CYCLES or not state["cycle_queue"]:
            return
        
        next_item = state["cycle_queue"].pop(0)
        state["running_count"] = state.get("running_count", 0) + 1
        state.setdefault("running_pairs", set()).add(next_item["instrument"])
        # Inline context store (avoids scope issue with nested _store_running_context)
        _sc = next_item.get("scout_context") or {}
        state.setdefault("running_contexts", {})[next_item["instrument"]] = {
            "direction": (_sc.get("direction") or "").upper(),
            "setup_name": _sc.get("setup_name", ""),
            "score": _sc.get("score", 0),
        }
        _BACKGROUND_EXECUTOR.submit(
            _run_cycle_background,
            user_id, next_item["instrument"], "M15", next_item["source"],
            scout_context=next_item.get("scout_context"),
        )
        if flight:
            flight.record(FlightStage.QUEUE_DEQUEUE, pair=next_item["instrument"], data={
                "source": next_item["source"], "score": next_item.get("score", 0),
                "remaining": len(state["cycle_queue"]),
            }, note=f"dequeued {next_item['source']}, {len(state['cycle_queue'])} remaining")
        logger.info(f"Dequeued {next_item['source']} cycle for {next_item['instrument']} (remaining: {len(state['cycle_queue'])})")

    # GET /api/trading/team-status
    @app.route("/api/trading/team-status", methods=["GET", "OPTIONS"])
    def api_trading_team_status():
        """Return team initialization state, cycle count, and last result."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        uid = user_info["user_id"]
        state = _get_user_team_state(uid)

        team = state["team_setup"]
        agent_ids = dict(getattr(team, '_agent_ids', {})) if team else {}

        # Get user's workspace
        user_ws = None
        try:
            from workspace_provisioner import get_user_workspace
            user_ws = get_user_workspace(uid)
        except Exception:
            pass

        # Sanitize last_cycle to ensure JSON-serializable (may contain pandas objects)
        last_cycle = state.get("last_cycle")
        safe_last_cycle = None
        if last_cycle:
            try:
                import json as _json
                _json.dumps(last_cycle)  # test serialization
                safe_last_cycle = last_cycle
            except (TypeError, ValueError):
                # Extract only safe fields
                safe_last_cycle = {
                    "status": last_cycle.get("status"),
                    "instrument": last_cycle.get("instrument"),
                    "cycle_number": last_cycle.get("cycle_number"),
                    "timing": last_cycle.get("timing"),
                    "phases": last_cycle.get("phases"),
                    "decisions": last_cycle.get("decisions"),
                    "decision": last_cycle.get("decision"),
                    "end_time": last_cycle.get("end_time"),
                    "steps_completed": last_cycle.get("steps_completed"),
                    "analysis": last_cycle.get("analysis"),
                    "indicators": last_cycle.get("indicators"),
                    "sniper": last_cycle.get("sniper"),
                    "validation": last_cycle.get("validation"),
                    "ta_explanation": last_cycle.get("ta_explanation"),
                    "full_confluence": last_cycle.get("full_confluence"),
                }

        # Per-pair live progress from all running cycle instances
        live_progress = None  # Legacy single-cycle
        per_pair_progress = {}
        cycle_instances = state.get("cycle_instances", {})
        for inst_pair, cycle_inst in list(cycle_instances.items()):
            if cycle_inst and hasattr(cycle_inst, 'live_cycle_result') and cycle_inst.live_cycle_result:
                live = cycle_inst.live_cycle_result
                pp = {
                    "status": "running",
                    "instrument": live.get("instrument", inst_pair),
                    "phases": live.get("phases", []),
                    "steps_completed": live.get("steps_completed", []),
                }
                per_pair_progress[inst_pair] = pp
                if live_progress is None:
                    live_progress = pp  # Legacy compat: first running cycle

        # Per-pair last completed cycles
        per_pair_last = {}
        for pp_pair, pp_result in state.get("last_cycles", {}).items():
            if pp_pair not in per_pair_progress:  # Don't overwrite running with stale
                try:
                    import json as _json2
                    _json2.dumps(pp_result)
                    per_pair_last[pp_pair] = pp_result
                except (TypeError, ValueError):
                    per_pair_last[pp_pair] = {"instrument": pp_pair, "status": "completed"}

        # Merge: running progress takes priority over last completed
        all_pair_cycles = {**per_pair_last, **per_pair_progress}

        # Drain pending notifications (consume on read)
        pending_notifications = list(state.get("notifications", []))
        state["notifications"] = []

        # ── Ghost cleanup: prune running_pairs that have no active cycle_instance ──
        _rp = state.get("running_pairs", set())
        _ci = state.get("cycle_instances", {})
        _ghost_pairs = {p for p in _rp if p not in _ci}
        if _ghost_pairs:
            for gp in _ghost_pairs:
                _rp.discard(gp)
                state.get("running_contexts", {}).pop(gp, None)
            state["running_count"] = max(0, len(_rp))
            logger.info("Cleaned %d ghost running pairs: %s", len(_ghost_pairs), _ghost_pairs)

        return jsonify({
            "initialized": state["initialized"],
            "running": state.get("running_count", 0) > 0,
            "running_count": state.get("running_count", 0),
            "running_pairs": list(state.get("running_pairs", set())),
            "running_detail": [{"instrument": p, **state.get("running_contexts", {}).get(p, {})} for p in state.get("running_pairs", set())],
            "max_concurrent": MAX_CONCURRENT_CYCLES,
            "cycle_count": state["cycle_count"],
            "last_cycle": live_progress or safe_last_cycle,
            "pair_cycles": all_pair_cycles,
            "error": state["error"],
            "agents": list(agent_ids.keys()),
            "agent_count": len(agent_ids),
            "workspace": user_ws,
            "notifications": pending_notifications,
            "current_cycle_pair": state.get("current_cycle_pair"),
            "queue": [q["instrument"] for q in state.get("cycle_queue", [])],
            "queue_detail": [{
                "instrument": q["instrument"],
                "priority": q.get("priority", "normal"),
                "source": q.get("source", ""),
                "score": round(q.get("score", 0), 3),
                "direction": (q.get("scout_context") or {}).get("direction", ""),
                "setup_name": (q.get("scout_context") or {}).get("setup_name", ""),
                "win_rate": (q.get("scout_context") or {}).get("win_rate"),
                "scout_confidence": (q.get("scout_context") or {}).get("scout_confidence"),
            } for q in state.get("cycle_queue", [])],
            "queue_depth": len(state.get("cycle_queue", [])),
        })

    # POST /api/trading/start-team
    @app.route("/api/trading/start-team", methods=["POST", "OPTIONS"])
    def api_trading_start_team():
        """Initialize the trading team (load from registry) and optionally run a cycle."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        # Provision workspace if needed (first login to trading dashboard)
        uid = user_info["user_id"]
        state = _get_user_team_state(uid)

        # Provision workspace if needed
        try:
            from workspace_provisioner import provision_trading_workspace, get_user_workspace
            ws = get_user_workspace(uid)
            if not ws:
                ws = provision_trading_workspace(
                    uid,
                    user_info.get("username", f"user_{uid}"),
                )
                logger.info("Provisioned workspace for user %s: %s", uid, ws)
            state["workspace"] = ws
        except Exception as e:
            logger.warning("Workspace provisioning: %s", e)
            ws = None

        # Clear stale queue/running state from prior session
        state["cycle_queue"] = []
        state["running_count"] = 0
        state["running_pairs"] = set()
        state["running_contexts"] = {}
        state["current_cycle_pair"] = None
        state["cycle_started_at"] = None
        state["cycle_instance"] = None
        state["cycle_instances"] = {}
        logger.info("Cleared stale cycle state for user %s on team init", uid)

        # Initialize user's team
        team, init_err = _ensure_team_initialized(uid)
        if init_err:
            return jsonify({"error": init_err}), 500

        data = request.json or {}
        run_cycle = data.get("run_cycle", False)
        instrument = data.get("instrument")

        # Get active pair from DB if not specified
        if not instrument:
            try:
                users_db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "..", "Database", "v2", "core.db")
                users_db = os.path.normpath(users_db)
                from db_connection import get_db
                with get_db(users_db, timeout=10) as conn:
                    row = conn.execute(
                        "SELECT pref_value FROM trading_preferences WHERE user_id=? AND pref_key='active_pair'",
                        (uid,)
                    ).fetchone()
                    instrument = row[0] if row else "EUR_USD"
            except Exception:
                instrument = "EUR_USD"

        result = {
            "initialized": True,
            "agents": list(getattr(team, '_agent_ids', {}).keys()),
            "agent_count": len(getattr(team, '_agent_ids', {})),
            "instrument": instrument,
            "workspace": ws,
        }

        if run_cycle and state.get("running_count", 0) < MAX_CONCURRENT_CYCLES:
            import threading
            state["running_count"] = state.get("running_count", 0) + 1
            state.setdefault("running_pairs", set()).add(instrument)
            _future = _BACKGROUND_EXECUTOR.submit(_run_cycle_background, uid, instrument)
            state["cycle_runner"] = _future
            result["cycle_started"] = True
        elif state.get("running_count", 0) > 0:
            result["cycle_started"] = False
            result["message"] = "Cycle already running"

        return jsonify(result)

    # POST /api/trading/scout-pause
    @app.route("/api/trading/scout-pause", methods=["POST", "OPTIONS"])
    def api_trading_scout_pause():
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        import pathlib
        pathlib.Path("/tmp/scout_paused").touch()
        return jsonify({"paused": True})

    # POST /api/trading/scout-resume
    @app.route("/api/trading/scout-resume", methods=["POST", "OPTIONS"])
    def api_trading_scout_resume():
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            os.remove("/tmp/scout_paused")
        except FileNotFoundError:
            pass
        return jsonify({"paused": False})

    # GET /api/trading/scout-status
    @app.route("/api/trading/card-data/<pair>", methods=["GET", "OPTIONS"])
    def api_trading_card_data(pair):
        """Return last trade decision + cached intelligence for a pair from DB."""
        import json as _json
        try:
            result = {"pair": pair}
            conn = get_trading_forex()
            conn.row_factory = sqlite3.Row

            # Last trade decision for this pair
            row = conn.execute(
                "SELECT * FROM trade_decisions WHERE pair=? ORDER BY created_at DESC LIMIT 1",
                (pair,)
            ).fetchone()
            if row:
                d = dict(row)
                result["validation"] = {
                    "verdict": d.get("validator_verdict", ""),
                    "confidence": d.get("validator_confidence", 0),
                    "reasoning": d.get("validator_reasoning", ""),
                }
                # Parse JSON fields
                for jf in ["validator_db_evidence", "validator_loss_patterns"]:
                    try:
                        result["validation"][jf.replace("validator_", "")] = _json.loads(d.get(jf, "{}") or "{}")
                    except Exception:
                        pass
                result["last_decision"] = {
                    "action": d.get("final_action", "hold"),
                    "reason": d.get("final_action_reason", ""),
                    "direction": d.get("direction", ""),
                    "setup_name": d.get("setup", ""),
                    "regime": d.get("regime", ""),
                    "confluence_score": d.get("validator_confluence", 0),
                    "timestamp": d.get("created_at", ""),
                }
                # Intelligence from the decision row
                intel = {}
                if d.get("news_agent_data"):
                    try: intel["news"] = _json.loads(d["news_agent_data"])
                    except (ValueError, TypeError): intel["news"] = d["news_agent_data"]
                if d.get("weather_agent_data"):
                    try: intel["weather"] = _json.loads(d["weather_agent_data"])
                    except (ValueError, TypeError): intel["weather"] = d["weather_agent_data"]
                if d.get("wolfram_agent_data"):
                    try: intel["macro"] = _json.loads(d["wolfram_agent_data"])
                    except (ValueError, TypeError): intel["macro"] = d["wolfram_agent_data"]
                if d.get("market_agent_data"):
                    try: intel["market"] = _json.loads(d["market_agent_data"])
                    except (ValueError, TypeError): intel["market"] = d["market_agent_data"]
                if intel:
                    result["intelligence"] = intel

            # Also pull latest intelligence cache entries for this pair
            cache_rows = conn.execute(
                "SELECT cache_key, data, fetched_at, expires_at FROM intelligence_cache WHERE instrument=? ORDER BY fetched_at DESC",
                (pair,)
            ).fetchall()
            if cache_rows:
                cache = {}
                for cr in cache_rows:
                    key = cr["cache_key"]
                    try: cache[key] = _json.loads(cr["data"])
                    except (ValueError, TypeError): cache[key] = cr["data"]
                result["intelligence_cache"] = cache

            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/trading/scout-status", methods=["GET", "OPTIONS"])
    def api_trading_scout_status():
        """Return current scout activity status and high-separation pairs."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        try:
            # Scout status reads from the EXTERNAL scout process via its DB/shared state.
            # Do NOT create a TradeScout() instance here — it triggers a 47-second profile
            # engine build that blocks the server. Just read scout_alerts from DB.
            import sqlite3 as _sq
            _scout_db = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                     'Database', 'v2', 'trading_forex.db')
            _scout_db = os.path.normpath(_scout_db)
            with _sq.connect(_scout_db, isolation_level=None) as _sconn:
                _sconn.row_factory = _sq.Row
                recent = _sconn.execute(
                    "SELECT * FROM scout_alerts ORDER BY id DESC LIMIT 20"
                ).fetchall()
                alerts = [dict(r) for r in recent] if recent else []
                last_scan = alerts[0]['timestamp'] if alerts else None

            # Prefer heartbeat file (written every scan even with 0 alerts)
            try:
                with open("/tmp/scout_last_scan") as _hb:
                    _hb_ts = _hb.read().strip()
                    if _hb_ts:
                        last_scan = _hb_ts
            except Exception:
                pass

            return jsonify({
                "last_scan": last_scan,
                "pairs_scanned": 13,
                "active_alerts": alerts[:10],
                "alert_count": len(alerts),
                "running": True,
                "paused": os.path.exists("/tmp/scout_paused"),
                "next_scan_in": 300,
            })

        except ImportError as e:
            logger.warning("TradeScout not available: %s", e)
            return jsonify({
                "error": "Scout not available",
                "last_scan": None,
                "pairs_scanned": 0,
                "high_separation_pairs": [],
                "active_alerts": [],
                "next_scan_in": 300,
                "running": False
            })
        except Exception as e:
            logger.error("Scout status error: %s", e)
            return jsonify({
                "error": str(e),
                "last_scan": None,
                "pairs_scanned": 0,
                "high_separation_pairs": [],
                "active_alerts": [],
                "next_scan_in": 300,
                "running": False
            })

    # POST /api/trading/run-cycle
    @app.route("/api/trading/run-cycle", methods=["POST", "OPTIONS"])
    def api_trading_run_cycle():
        """Trigger a single trading cycle. Localhost (scout) can call without auth."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        # Allow localhost calls from scout without auth token
        remote = request.remote_addr or ""
        if remote in ("127.0.0.1", "::1", "localhost"):
            # Resolve admin user from token or DB — never hardcode user_id
            user_info, err = _get_authenticated_user()
            if err:
                # No auth header (scout internal call) — use the localhost auto-login user
                _localhost_user = validate_auth_token_func("trevor-local-tim-wade-2")
                uid = _localhost_user["user_id"] if _localhost_user else None
                if not uid:
                    return jsonify({"error": "No authenticated user for localhost"}), 401
            else:
                uid = user_info["user_id"]
        else:
            user_info, err = _get_authenticated_user()
            if err:
                return err
            uid = user_info["user_id"]
        state = _get_user_team_state(uid)

        if not state["initialized"]:
            team, init_err = _ensure_team_initialized(uid)
            if init_err:
                return jsonify({"error": init_err}), 500

        data = request.json or {}
        instrument = data.get("pair", data.get("instrument", "EUR_USD"))
        timeframe = data.get("timeframe", "M15")
        scout_context = data.get("scout_context", None)
        source = data.get("source", "scout" if scout_context else "manual")
        priority = "high" if source == "snipe" else "normal"

        result = _queue_cycle(uid, instrument, priority=priority, source=source,
                              scout_context=scout_context)

        return jsonify({
            "started": result == "started",
            "queued": result == "queued",
            "status": result,
            "instrument": instrument,
            "timeframe": timeframe,
            "cycle_number": state["cycle_count"] + 1,
            "queue_depth": len(state["cycle_queue"]),
        })

    # ------------------------------------------------------------------
    # POST /api/trading/ghost-mode — swap validator model for ghost comparison
    # ------------------------------------------------------------------
    @app.route("/api/trading/ghost-mode", methods=["POST", "OPTIONS"])
    def api_trading_ghost_mode():
        """Swap the validator agent's model between Anthropic and local 35B.

        POST {"enabled": true}  → swap to mlx/CSO (35B on port 11502)
        POST {"enabled": false} → restore to claude-sonnet-4-6
        GET  → return current state
        """
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        data = request.json or {}
        enabled = data.get("enabled")

        # Get the swarm from the user's team state (where agents are registered)
        _localhost_user = validate_auth_token_func("trevor-local-tim-wade-2")
        uid = _localhost_user["user_id"] if _localhost_user else None
        if uid:
            _ensure_team_initialized(uid)
            state = _get_user_team_state(uid)
            team_setup = state.get("team_setup")
            swarm = getattr(team_setup, 'swarm', None) if team_setup else None
        else:
            swarm = None

        if not swarm or not hasattr(swarm, 'agents'):
            from agents.trading_cycle import _get_swarm
            swarm = _get_swarm()

        GHOST_MODEL = "mlx/CSO"
        ORIGINAL_MODEL = "claude-sonnet-4-6"

        # Find validator agent in swarm
        validator_agent = None
        for agent in swarm.agents.values():
            if agent.name == "validator":
                validator_agent = agent
                break

        if not validator_agent:
            return jsonify({"error": "Validator agent not found in swarm",
                            "agents": list(swarm.agents.keys())}), 404

        if enabled is None:
            # GET-style: return current state
            return jsonify({
                "ghost_mode": validator_agent.model == GHOST_MODEL,
                "current_model": validator_agent.model,
            })

        old_model = validator_agent.model
        if enabled:
            validator_agent.model = GHOST_MODEL
        else:
            validator_agent.model = ORIGINAL_MODEL

        logger.info("[GHOST] Validator model swapped: %s → %s", old_model, validator_agent.model)

        return jsonify({
            "ghost_mode": enabled,
            "old_model": old_model,
            "new_model": validator_agent.model,
        })

    # ------------------------------------------------------------------
    # POST /api/trading/cancel-queued — remove one pair from cycle queue
    # ------------------------------------------------------------------
    @app.route("/api/trading/cancel-queued", methods=["POST", "OPTIONS"])
    def api_trading_cancel_queued():
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        data = request.json or {}
        pair = data.get("pair")
        state = _get_user_team_state(user_info["user_id"])
        before = len(state["cycle_queue"])
        state["cycle_queue"] = [q for q in state["cycle_queue"] if q.get("instrument") != pair]
        removed = before - len(state["cycle_queue"])
        return jsonify({"removed": removed, "queue_depth": len(state["cycle_queue"])})

    # ------------------------------------------------------------------
    # POST /api/trading/flush-stale — clear scout entries from queue (snipes preserved)
    # ------------------------------------------------------------------
    @app.route("/api/trading/flush-stale", methods=["POST", "OPTIONS"])
    def api_trading_flush_stale():
        """Flush stale scout-source entries from queue. Snipe entries preserved.
        No auth required — called by scout (localhost only), just clears queue entries."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        # Internal endpoint — called by scout on localhost only
        if request.remote_addr not in ('127.0.0.1', '::1'):
            return jsonify({"error": "Localhost only"}), 403
        data = request.json or {}
        source_filter = data.get("source", "scout")
        # Try auth token first, fall back to user_id in body (internal scout calls)
        user_info, _ = _get_authenticated_user()
        uid = user_info["user_id"] if user_info else data.get("user_id")
        if not uid:
            return jsonify({"error": "user_id required"}), 400
        state = _get_user_team_state(uid)
        before = len(state["cycle_queue"])
        state["cycle_queue"] = [
            q for q in state["cycle_queue"]
            if q.get("source") != source_filter
        ]
        flushed = before - len(state["cycle_queue"])
        if flushed > 0 and flight:
            flight.record(FlightStage.QUEUE_ENTER, pair="ALL", data={
                "action": "flush_stale", "source": source_filter,
                "flushed": flushed, "remaining": len(state["cycle_queue"]),
            }, note=f"flushed {flushed} stale {source_filter} entries")
        return jsonify({"flushed": flushed, "remaining": len(state["cycle_queue"])})

    # POST /api/trading/clear-queue — clear entire cycle queue
    # ------------------------------------------------------------------
    @app.route("/api/trading/clear-queue", methods=["POST", "OPTIONS"])
    def api_trading_clear_queue():
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        state = _get_user_team_state(user_info["user_id"])
        count = len(state["cycle_queue"])
        state["cycle_queue"].clear()
        return jsonify({"cleared": count})

    # ------------------------------------------------------------------
    # GET /api/trading/health-findings
    @app.route("/api/trading/health-findings", methods=["GET", "OPTIONS"])
    def api_trading_health_findings():
        """Return recent workflow health findings from the reporter's health check."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        try:
            from Source.cycle_health_check import get_recent_findings, acknowledge_finding
            severity = request.args.get("severity")
            limit = int(request.args.get("limit", 20))
            findings = get_recent_findings(limit=limit, severity=severity)
            return jsonify({"findings": findings})
        except Exception as e:
            return jsonify({"findings": [], "error": str(e)})

    # POST /api/trading/health-findings/acknowledge
    @app.route("/api/trading/health-findings/acknowledge", methods=["POST", "OPTIONS"])
    def api_trading_acknowledge_finding():
        """Acknowledge a health finding so it stops showing."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        try:
            from Source.cycle_health_check import acknowledge_finding
            data = request.get_json() or {}
            finding_id = data.get("finding_id")
            if not finding_id:
                return jsonify({"error": "finding_id required"}), 400
            acknowledge_finding(int(finding_id))
            return jsonify({"acknowledged": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # GET /api/trading/dashboard
    # Returns dashboard data (cycleData) for the trading dashboard frontend
    # ------------------------------------------------------------------
    @app.route("/api/trading/dashboard", methods=["GET", "OPTIONS"])
    def api_trading_dashboard():
        """Return dashboard data structure expected by the frontend."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        uid = user_info["user_id"]
        state = _get_user_team_state(uid)

        # Get user's active instrument
        instrument = _get_trading_preference(uid, "active_instrument", "EUR_USD")
        
        # Initialize response structure
        cycle_data = {
            "cycle_number": state.get("cycle_count", 0),
            "status": "running" if state.get("running_count", 0) > 0 else "idle",
            "instrument": instrument,
            "timestamp": None,
            "cycle_start": None,
            "decision": {
                "action": "hold",
                "allowed": True,
                "blocking_reasons": [],
                "reasons": ["No recent cycle"]
            },
            "timing": {
                "phases": {
                    "data_collection": 0.0,
                    "analysis": 0.0,
                    "validation": 0.0,
                    "total": 0.0
                }
            },
            "data_collection": {
                "account": {
                    "balance": 0.0,
                    "unrealizedPL": 0.0,
                    "open_trade_count": 0,
                    "currency": "USD"
                }
            },
            "execution": None,
            "phases": [],
            "decisions": []
        }

        # Get live account data from OANDA
        try:
            bc = _get_broker_credentials()
            conn = bc.get_connection(uid, "oanda")
            if conn.get("configured"):
                import requests as http_requests
                headers = {"Authorization": f"Bearer {conn['api_key']}"}
                r = http_requests.get(
                    f"{conn['base_url']}/v3/accounts/{conn['account_id']}/summary",
                    headers=headers, timeout=10,
                )
                if r.status_code == 200:
                    acct = r.json().get("account", {})
                    cycle_data["data_collection"]["account"] = {
                        "balance": float(acct.get("balance", 0)),
                        "unrealizedPL": float(acct.get("unrealizedPL", 0)),
                        "open_trade_count": int(acct.get("openTradeCount", 0)),
                        "currency": acct.get("currency", "USD")
                    }
        except Exception as e:
            logger.warning(f"Failed to fetch account data: {e}")

        # If cycle is running, use LIVE progress from cycle instance
        live_cycle = None
        if state.get("running_count", 0) > 0:
            cycle_inst = state.get("cycle_instance")
            if cycle_inst and hasattr(cycle_inst, 'live_cycle_result') and cycle_inst.live_cycle_result:
                live_cycle = cycle_inst.live_cycle_result

        # Get last cycle data if available (live takes priority, then memory, then disk)
        last_cycle = live_cycle or state.get("last_cycle")
        if not last_cycle:
            # Load from persisted cycle state (survives restart)
            try:
                import json as _ljson
                _persist_dir = os.path.join(os.path.dirname(__file__), '..', 'dashboard', 'cycle_state')
                _persist_path = os.path.join(_persist_dir, f"{instrument}.json")
                if os.path.exists(_persist_path):
                    with open(_persist_path) as _lf:
                        last_cycle = _ljson.load(_lf)
                    state["last_cycle"] = last_cycle  # Cache in memory
                    logger.info("Loaded persisted cycle state for %s", instrument)
            except Exception as _le:
                logger.debug("Failed to load persisted cycle state: %s", _le)
        if last_cycle:
            from datetime import datetime as dt
            
            # Update status based on current state
            if state.get("running_count", 0) > 0:
                cycle_data["status"] = "running"
            else:
                cycle_data["status"] = last_cycle.get("status", "completed")
            
            # Update cycle data from last result — use the cycle's instrument, not the preference
            cycle_data["instrument"] = last_cycle.get("instrument", instrument)
            cycle_data["timestamp"] = last_cycle.get("end_time") or dt.now().isoformat()
            cycle_data["cycle_start"] = last_cycle.get("cycle_start") or last_cycle.get("start_time") or dt.now().isoformat()
            
            # Decision data
            decision_data = last_cycle.get("decision", {})
            cycle_data["decision"] = {
                "action": decision_data.get("action", "hold"),
                "allowed": decision_data.get("allowed", True),
                "blocking_reasons": decision_data.get("blocking_reasons", []),
                "reasons": decision_data.get("reasons", ["No clear signal"])
            }
            
            # Timing data
            timing_data = last_cycle.get("timing", {})
            if timing_data:
                cycle_data["timing"] = timing_data
            
            # Phases data
            phases_data = last_cycle.get("phases", [])
            if phases_data:
                cycle_data["phases"] = phases_data
            
            # Execution data
            execution_data = last_cycle.get("execution")
            if execution_data:
                cycle_data["execution"] = execution_data

            # Decisions from cycle (agent activity log)
            cycle_decisions = last_cycle.get("decisions", [])
            if cycle_decisions:
                cycle_data["decisions"] = cycle_decisions

            # Scout context (what triggered the cycle)
            sc = last_cycle.get("scout_context")
            if sc:
                cycle_data["scout_context"] = sc

            # ── Indicator data for chart overlays ──
            analysis = last_cycle.get("analysis", {})
            sniper = analysis.get("sniper_score", {}) if isinstance(analysis, dict) else {}
            indicators = sniper.get("indicators", {}) if isinstance(sniper, dict) else {}
            if indicators:
                cycle_data["indicators"] = {
                    "rsi": indicators.get("rsi"),
                    "rsi_slope": indicators.get("rsi_slope"),
                    "stoch_k": indicators.get("stoch_k"),
                    "stoch_d": indicators.get("stoch_d"),
                    "macd": indicators.get("macd"),
                    "macd_signal": indicators.get("macd_signal"),
                    "macd_histogram": indicators.get("macd_histogram"),
                    "adx": indicators.get("adx"),
                    "cci": indicators.get("cci"),
                    "ema_21": indicators.get("ema_21"),
                    "ema_55": indicators.get("ema_55"),
                    "ema_100": indicators.get("ema_100"),
                    "bb_upper": indicators.get("bb_upper"),
                    "bb_mid": indicators.get("bb_mid"),
                    "bb_lower": indicators.get("bb_lower"),
                    "bb_lower_pen": indicators.get("bb_lower_pen"),
                    "bb_upper_pen": indicators.get("bb_upper_pen"),
                    "sar_bullish": indicators.get("sar_bullish"),
                    "at_key_fib": indicators.get("at_key_fib"),
                    "consec_bull": indicators.get("consec_bull"),
                    "consec_bear": indicators.get("consec_bear"),
                }
            # Sniper scores
            if sniper:
                cycle_data["sniper"] = {
                    "buy_score": sniper.get("buy_score", 0),
                    "sell_score": sniper.get("sell_score", 0),
                    "direction": sniper.get("direction", "neutral"),
                    "signal": sniper.get("signal", "HOLD"),
                    "h4_bias": sniper.get("h4_bias", "none"),
                    "detected_patterns": sniper.get("detected_patterns", []),
                    "divergence": sniper.get("divergence", {}),
                }
            # TA agent explanation — always a dict with consistent schema
            ta_data = analysis.get("ta_interpretation", analysis.get("technical_analysis", {}))
            if isinstance(ta_data, str) and ta_data:
                ta_data = {"narrative": ta_data}
            if not isinstance(ta_data, dict):
                ta_data = {}
            # Ensure thesis_progress always present (Python-computed in trading_cycle.py)
            if "thesis_progress" not in ta_data:
                ta_data["thesis_progress"] = {}
            cycle_data["ta_explanation"] = ta_data
            # Validator explanation
            validation = last_cycle.get("validation", {})
            if isinstance(validation, dict) and validation:
                cycle_data["validation"] = {
                    "verdict": validation.get("verdict", validation.get("action")),
                    "confidence": validation.get("confidence"),
                    "reasoning": validation.get("reasoning", validation.get("explanation", "")),
                    "db_evidence": validation.get("db_evidence", {}),
                    "risk_flags": validation.get("risk_flags", []),
                    "re_entry_conditions": validation.get("re_entry_conditions", []),
                    "missing_items": validation.get("missing_items", []),
                    "re_entry_count": validation.get("re_entry_count", 0),
                    "best_setup": validation.get("best_setup"),
                    "win_rate": validation.get("win_rate"),
                }

            # Intelligence data (wolfram, macro, news, briefing) for chart card
            intel_data = last_cycle.get("intelligence_data")
            if isinstance(intel_data, dict) and intel_data:
                cycle_data["intelligence_data"] = intel_data

            # Also copy from data_collection.intelligence as fallback
            if not cycle_data.get("intelligence_data"):
                dc_intel = last_cycle.get("data_collection", {}).get("intelligence")
                if isinstance(dc_intel, dict) and dc_intel:
                    cycle_data["intelligence_data"] = dc_intel

            # Confluence score for chart card
            fc = last_cycle.get("full_confluence")
            if isinstance(fc, dict) and fc:
                cycle_data["confluence_score"] = fc.get("total_score", 0)
                cycle_data["full_confluence"] = fc

        # Get recent trade decisions from database
        try:
            import sqlite3 as sql3
            trevor_db_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "..", "Database", "v2", "trading_forex.db"
            )
            trevor_db_path = os.path.normpath(trevor_db_path)
            
            if os.path.exists(trevor_db_path):
                conn = sql3.connect(trevor_db_path, isolation_level=None)
                conn.row_factory = sql3.Row
                rows = conn.execute("""
                    SELECT instrument, direction, entry_price, stop_loss, take_profit, 
                           timestamp, decision_type, confidence, status
                    FROM trade_decisions 
                    WHERE instrument = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 20
                """, (instrument,)).fetchall()
                conn.close()
                
                decisions = []
                for row in rows:
                    decisions.append({
                        "timestamp": row["timestamp"],
                        "agent": "orchestrator",
                        "action": row["decision_type"] or "HOLD",
                        "result": f"{row['direction'] or 'No entry'} signal",
                        "confidence": row["confidence"],
                        "instrument": row["instrument"],
                        "entry_price": row["entry_price"],
                        "stop_loss": row["stop_loss"],
                        "take_profit": row["take_profit"],
                        "status": row["status"]
                    })
                
                if decisions:
                    cycle_data["decisions"] = decisions
                    
        except Exception as e:
            logger.debug(f"Could not fetch trade decisions: {e}")

        # If we're currently running, update status and add placeholder phases
        if state.get("running_count", 0) > 0:
            cycle_data["status"] = "running"
            cycle_data["cycle_number"] = state.get("cycle_count", 0) + 1
            if not cycle_data.get("timestamp"):
                from datetime import datetime as dt
                cycle_data["timestamp"] = dt.now().isoformat()
                cycle_data["cycle_start"] = dt.now().isoformat()

        return jsonify(cycle_data)

    # ------------------------------------------------------------------
    # GET /api/trading/agent-comms
    # Returns agent communications and workspace task comments
    # ------------------------------------------------------------------
    @app.route("/api/trading/agent-comms", methods=["GET", "OPTIONS"])
    def api_trading_agent_comms():
        """Return agent communications for the dashboard lightbox."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        agent_name = request.args.get("agent")  # optional filter
        limit = min(int(request.args.get("limit", 50)), 200)

        # Map dashboard agent names to DB names (some differ)
        AGENT_ALIASES = {
            "validator": ["validator", "data_validator"],
            "cycle_orchestrator": ["cycle_orchestrator", "trading_orchestrator"],
            "reporter": ["reporter", "reporting"],
            "technical_analyst": ["technical_analyst", "technical_analysis"],
            "intelligence": ["intelligence"],
            "oanda_data": ["oanda_data"],
            "execution": ["execution"],
            "trade_monitor": ["trade_monitor"],
        }
        search_names = AGENT_ALIASES.get(agent_name, [agent_name]) if agent_name else None

        boardroom_db = os.path.normpath(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "..", "Database", "v2", "conversations.db")
        )

        result = {
            "communications": [],  # agent-to-agent messages
            "task_comments": [],   # workspace task results
        }

        try:
            import sqlite3 as sql3
            conn = sql3.connect(boardroom_db, timeout=30, isolation_level=None)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sql3.Row

            # 1. Agent-to-agent communications (V2 schema: agent_communications plural)
            if search_names:
                placeholders = ",".join("?" for _ in search_names)
                comms = conn.execute(f"""
                    SELECT from_agent_id, to_agent_id,
                           message, context, timestamp
                    FROM agent_communications
                    WHERE from_agent_id IN ({placeholders}) OR to_agent_id IN ({placeholders})
                    ORDER BY timestamp DESC LIMIT ?
                """, (*search_names, *search_names, limit)).fetchall()
            else:
                comms = conn.execute("""
                    SELECT from_agent_id, to_agent_id,
                           message, context, timestamp
                    FROM agent_communications
                    ORDER BY timestamp DESC LIMIT ?
                """, (limit,)).fetchall()

            for row in comms:
                result["communications"].append({
                    "from": row["from_agent_id"],
                    "to": row["to_agent_id"],
                    "type": "agent_message",
                    "content": row["message"],
                    "timestamp": row["timestamp"],
                })

            conn.close()

            # 2. Task comments (agent results posted to workspace)
            # workspace_tasks and workspace_task_comments live in v2/workspaces.db
            workspaces_db = os.path.normpath(
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "..", "Database", "v2", "workspaces.db"))
            ws_conn = sql3.connect(workspaces_db, timeout=30, isolation_level=None)
            ws_conn.execute("PRAGMA busy_timeout=30000")
            ws_conn.row_factory = sql3.Row

            latest_task = ws_conn.execute("""
                SELECT id FROM workspace_tasks
                WHERE title LIKE '%Trading Cycle%' OR title LIKE '%trading_cycle%'
                ORDER BY created_at DESC LIMIT 1
            """).fetchone()

            if latest_task:
                task_id = latest_task["id"]
                if search_names:
                    placeholders = ",".join("?" for _ in search_names)
                    comments = ws_conn.execute(f"""
                        SELECT author_id, content, technical_details, created_at
                        FROM workspace_task_comments
                        WHERE task_id = ? AND author_id IN ({placeholders})
                        ORDER BY created_at DESC LIMIT ?
                    """, (task_id, *search_names, limit)).fetchall()
                else:
                    comments = ws_conn.execute("""
                        SELECT author_id, content, technical_details, created_at
                        FROM workspace_task_comments
                        WHERE task_id = ?
                        ORDER BY created_at DESC LIMIT ?
                    """, (task_id, limit)).fetchall()

                for row in comments:
                    td = row["technical_details"]
                    td_parsed = {}
                    if td:
                        try:
                            td_parsed = json.loads(td) if isinstance(td, str) else td
                        except (json.JSONDecodeError, TypeError):
                            td_parsed = {"raw": str(td)[:500]}

                    result["task_comments"].append({
                        "agent": row["author_id"],
                        "content": row["content"],
                        "details": td_parsed,
                        "timestamp": row["created_at"],
                    })

                result["task_id"] = task_id

            # 3. Also get historical task comments (last 5 cycles)
            recent_tasks = ws_conn.execute("""
                SELECT id, title, created_at FROM workspace_tasks
                WHERE title LIKE '%Trading Cycle%' OR title LIKE '%trading_cycle%'
                ORDER BY created_at DESC LIMIT 5
            """).fetchall()
            result["recent_cycles"] = [
                {"task_id": t["id"], "title": t["title"], "created_at": t["created_at"]}
                for t in recent_tasks
            ]

            ws_conn.close()
        except Exception as e:
            logger.error(f"Agent comms query failed: {e}")
            result["error"] = str(e)

        return jsonify(result)

    # ------------------------------------------------------------------
    # GET /api/trading/agent-comms/<task_id>
    # Returns task comments for a specific cycle
    # ------------------------------------------------------------------
    @app.route("/api/trading/agent-comms/<int:task_id>", methods=["GET", "OPTIONS"])
    def api_trading_agent_comms_by_task(task_id):
        """Return task comments for a specific cycle task."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        agent_name = request.args.get("agent")
        boardroom_db = os.path.normpath(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "..", "Database", "v2", "workspaces.db")
        )

        comments = []
        try:
            import sqlite3 as sql3
            conn = sql3.connect(boardroom_db, timeout=30, isolation_level=None)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sql3.Row

            if agent_name:
                rows = conn.execute("""
                    SELECT author_id, content, technical_details, created_at
                    FROM workspace_task_comments
                    WHERE task_id = ? AND author_id = ?
                    ORDER BY created_at ASC
                """, (task_id, agent_name)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT author_id, content, technical_details, created_at
                    FROM workspace_task_comments
                    WHERE task_id = ?
                    ORDER BY created_at ASC
                """, (task_id,)).fetchall()

            conn.close()

            for row in rows:
                td = row["technical_details"]
                td_parsed = {}
                if td:
                    try:
                        td_parsed = json.loads(td) if isinstance(td, str) else td
                    except (json.JSONDecodeError, TypeError):
                        td_parsed = {"raw": str(td)[:500]}

                comments.append({
                    "agent": row["author_id"],
                    "content": row["content"],
                    "details": td_parsed,
                    "timestamp": row["created_at"],
                })
        except Exception as e:
            logger.error(f"Agent comms by task query failed: {e}")

        return jsonify({"task_id": task_id, "comments": comments})

    # ── Watch Manager API ──

    @app.route("/api/trading/watches", methods=["GET", "OPTIONS"])
    def api_trading_watches():
        """Get active watches and suggestion performance stats."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from Source.agents.watch_manager import get_active_watches, get_suggestion_stats
            uid = user_info["user_id"]
            return jsonify({
                "watches": get_active_watches(user_id=uid),
                "stats": get_suggestion_stats(),
            })
        except Exception as e:
            return jsonify({"watches": [], "stats": {}, "error": str(e)})

    @app.route("/api/trading/watches/check", methods=["POST", "OPTIONS"])
    def api_trading_watches_check():
        """Manually trigger a check of all active watches."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        uid = user_info["user_id"]
        try:
            from Source.agents.watch_manager import check_active_watches
            triggered = check_active_watches(user_id=uid)
            # Check open trades once for all pairs
            _open_instruments = set()
            try:
                import requests as _rq3
                from broker_credentials import BrokerCredentials as _BC3
                _bc3 = _BC3().get_connection(user_id=uid, broker="oanda")
                _ok3, _bu3, _aid3 = _bc3.get("api_key",""), _bc3.get("base_url",""), _bc3.get("account_id","")
                _ot3 = _rq3.get(
                    f"{_bu3}/v3/accounts/{_aid3}/openTrades",
                    headers={"Authorization": f"Bearer {_ok3}"}, timeout=4
                ).json().get("trades", [])
                _open_instruments = {t2.get("instrument") for t2 in _ot3}
            except Exception as _oanda_wc_err:
                logger.warning("[WATCH CHECK] OANDA check failed (%s) — skipping all notifications (fail safe)",
                               _oanda_wc_err)
                return jsonify({"triggered": triggered, "count": len(triggered),
                                "notifications_skipped": "oanda_unreachable"})

            for t in triggered:
                instrument = t["instrument"]
                watch_id = t.get("watch_id")
                state = _get_user_team_state(uid)
                # Only notify if no open trade on this pair
                if instrument not in _open_instruments:
                    state["notifications"].append({
                        "type": "snipe_triggered",
                        "instrument": instrument,
                        "watch_id": watch_id,
                        "conditions_met": t.get("conditions_met", []),
                        "raw_suggestion": t.get("raw_suggestion", ""),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                # Pull full watch context so snipe direct has direction + reasoning
                _wctx = {}
                _wc_suggestion_type = t.get("suggestion_type", "")
                _wc_direction_db = ""
                try:
                    import json as _wj
                    from db_pool import get_trading_forex as _wgtf
                    _wbc = _wgtf()
                    _wr = _wbc.execute(
                        "SELECT context, validator_verdict, user_thesis, suggestion_type, direction "
                        "FROM watch_suggestions WHERE id=?",
                        (watch_id,)
                    ).fetchone()
                    if _wr:
                        if _wr[0]:
                            _wctx = _wj.loads(_wr[0])
                        _wc_suggestion_type = _wr[3] or _wc_suggestion_type
                        _wc_direction_db = (_wr[4] or "").upper()
                except Exception:
                    pass
                # ── PAIR-LEVEL COOLDOWN — enforce BEFORE concurrency bypass ──────────
                # Snipes use priority="high" which bypasses MAX_CONCURRENT_CYCLES,
                # so the cooldown MUST be checked here, not inside _queue_cycle.
                _pair_close_ts = _pair_last_close.get((uid, instrument), 0)
                if _pair_close_ts and (time.time() - _pair_close_ts) < _PAIR_COOLDOWN_SECS:
                    _mins_remaining = (_PAIR_COOLDOWN_SECS - (time.time() - _pair_close_ts)) / 60
                    logger.info(
                        "[WATCH CHECK] %s watch #%s: PAIR cooldown active (%.1f min remaining) — skipping snipe",
                        instrument, watch_id, _mins_remaining
                    )
                    t["cycle_status"] = "cooldown_blocked"
                    continue

                # Determine triggered_by: user-submitted watches get user_chat/user_watch
                # so trading_cycle.py injects their chart context and prior validator analysis.
                _watch_source = _wctx.get("source", "")
                # All snipes trigger the same way — snipe conditions met → trade opens.
                # No distinction between scout-created, user-submitted, or cycle-created snipes.
                _triggered_by_val = "snipe"

                snipe_ctx = {
                    "_watch_id": watch_id,
                    "watch_id": watch_id,
                    "finding_id": watch_id,
                    "triggered_by": _triggered_by_val,
                    # 2026-04-23: expose suggestion_type so trading_cycle's live_trades INSERT
                    # labels entry_type='kronos_snipe' correctly.
                    "suggestion_type": _wc_suggestion_type,
                    # Setup ID required by trading_cycle setup validation gate
                    # Primary: from stored watch context (set by scout → trading_cycle → watch_manager)
                    # Safety fallback: setup_name or synthetic ID (should not happen with proper pipeline)
                    "setup_id": _wctx.get("setup_id") or _wctx.get("setup_name", "") or f"snipe_watch_{watch_id}",
                    "setup_name": _wctx.get("setup_name", "") or f"snipe_watch_{watch_id}",
                    # 2026-04-23: suggestion_type-aware direction resolution:
                    # kronos_path_snipe honors watch.direction (kronos prediction);
                    # other snipes use live_direction first (avoids stale validator watches).
                    "direction": (
                        (_wc_direction_db
                         or _wctx.get("re_entry_direction")
                         or _wctx.get("direction", ""))
                        if _wc_suggestion_type == "kronos_path_snipe"
                        else (t.get("live_direction")
                              or _wctx.get("re_entry_direction")
                              or _wctx.get("direction", ""))
                    ),
                    "raw_suggestion": t.get("raw_suggestion", ""),
                    "conditions_met": t.get("conditions_met", []),
                    "confluence_score": _wctx.get("confluence_score", 0),
                    "sniper_buy":  t.get("live_sniper_buy",  _wctx.get("sniper_buy", 0)),
                    "sniper_sell": t.get("live_sniper_sell", _wctx.get("sniper_sell", 0)),
                    "sniper_threshold": _wctx.get("sniper_threshold", 12),
                    "validator_reasoning": _wctx.get("validator_reasoning", ""),
                    # story_score for live_trades INSERT — stored as opportunity_score in watch context
                    "opportunity_score": _wctx.get("opportunity_score", _wctx.get("story_opportunity_score", 0)),
                    # User context fields — forwarded so cycle injects Tim's chart + analysis
                    "user_thesis": _wctx.get("user_thesis", ""),
                    "validator_context": _wctx.get("validator_context", ""),
                    "validator_full_analysis": _wctx.get("validator_full_analysis", ""),
                    "user_chart_path": _wctx.get("user_chart_path", ""),
                    "conversation_context": _wctx.get("conversation_context", []),
                }
                # triggered_by="snipe" routes to snipe_direct path in trading_cycle
                # which skips the full pipeline and goes straight to place_market_order()
                # with safety gates (open trade check, news, momentum trap, direction sanity)
                result = _queue_cycle(uid, instrument, priority="high", source="snipe",
                                      scout_context=snipe_ctx)
                t["cycle_status"] = result
                logger.info("🎯 Snipe triggered for %s (watch #%s) → %s", instrument, watch_id, result)
            return jsonify({"triggered": triggered, "count": len(triggered)})
        except Exception as e:
            return jsonify({"triggered": [], "error": str(e)})

    @app.route("/api/trading/reload-keys", methods=["POST", "OPTIONS"])
    def api_trading_reload_keys():
        """Force reload API key from file into all live SwarmHandler clients.
        Call this after rotating your Anthropic API key — no restart needed.
        Localhost can call without auth (same as run-cycle).
        """
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        remote = request.remote_addr or ""
        if remote not in ("127.0.0.1", "::1", "localhost"):
            user_info, err = _get_authenticated_user()
            if err:
                return err

        reloaded = []
        errors = []
        try:
            import os as _os
            _key_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                                      "..", "API", "CLAUDE_API_KEY.txt")
            _key_path = _os.path.normpath(_key_path)
            with open(_key_path) as _kf:
                fresh_key = _kf.read().strip()
        except Exception as ke:
            return jsonify({"error": f"Could not read key file: {ke}"}), 500

        for uid, state in _user_teams.items():
            team = state.get("team_setup")
            if not team:
                continue
            swarm = getattr(team, '_swarm', None)
            if not swarm:
                continue
            try:
                from anthropic import Anthropic as _Ant
                swarm.anthropic_client = _Ant(api_key=fresh_key)
                if hasattr(swarm, 'llm_router') and swarm.llm_router:
                    from Handler.modules.claude_client import AnthropicClient
                    _new_ac = AnthropicClient(api_key=fresh_key)
                    swarm.llm_router.register_client("anthropic/", _new_ac, is_default=True)
                    swarm.llm_router.register_client("claude-", _new_ac)
                    swarm.anthropic_client = _new_ac.client
                reloaded.append(f"user_{uid}")
                logger.info("🔑 API key reloaded for user %s swarm", uid)
            except Exception as re_exc:
                errors.append(f"user_{uid}: {re_exc}")

        return jsonify({
            "reloaded": reloaded,
            "errors": errors,
            "key_prefix": fresh_key[:8] + "...",
            "message": f"Key reloaded in {len(reloaded)} active swarm(s). No restart needed."
        })

    @app.route("/api/trading/snipe-clean", methods=["POST", "OPTIONS"])
    def api_trading_snipe_clean():
        """CRO-powered snipe relevance check.
        POST body: {"confirm": true}  → auto-cancels REMOVE decisions
        POST body: {} or omit         → dry-run, returns decisions for UI review
        Logs every CRO decision to flight_recorder for distillation training.
        """
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            body = request.get_json(silent=True) or {}
            auto_cancel = bool(body.get("confirm", False))
            from Source.snipe_cleanup import run_snipe_cleanup
            results = run_snipe_cleanup(auto_cancel=auto_cancel)
            removes = [r for r in results if r["decision"] == "REMOVE"]
            keeps   = [r for r in results if r["decision"] == "KEEP"]
            return jsonify({
                "total":    len(results),
                "keep":     len(keeps),
                "remove":   len(removes),
                "confirmed": auto_cancel,
                "results":  results,
            })
        except Exception as e:
            logger.exception("snipe-clean error")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/trading/watches/<int:watch_id>/cancel", methods=["POST", "OPTIONS"])
    def api_trading_watch_cancel(watch_id):
        """Cancel an active watch."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from Source.agents.watch_manager import cancel_watch
            ok = cancel_watch(watch_id)
            return jsonify({"cancelled": ok})
        except Exception as e:
            return jsonify({"error": str(e)})

    # ------------------------------------------------------------------
    # Server-side watch checker (runs even without dashboard open)
    # ------------------------------------------------------------------
    def _watch_checker_loop():
        """Background thread that checks active watches on interval."""
        import time as _time

        def _get_uid():
            try:
                import sqlite3 as _s3
                _udb = os.path.join(os.path.dirname(_RISK_CONFIG_PATH), "..", "..", "Database", "v2", "core.db")
                _uc = _s3.connect(_udb, isolation_level=None)
                _ur = _uc.execute("SELECT user_id FROM broker_credentials LIMIT 1").fetchone()
                _uc.close()
                return _ur[0] if _ur else None
            except Exception:
                return None

        def _fire_snipe_cycle(t):
            """Queue a cycle and notify dashboard for a triggered snipe."""
            instrument = t["instrument"]
            watch_id = t.get("watch_id")
            uid = _get_uid()
            if not uid:
                logger.warning("[WATCH TIMER] Could not resolve uid for %s", instrument)
                return

            # Pull the full watch record so the validator gets its original reasoning + conditions
            _orig_verdict = "WATCH"
            _orig_reasoning = t.get("raw_suggestion", "")
            _orig_conditions = t.get("conditions_met", [])
            _user_thesis = ""
            _watch_ctx = {}
            _watch_suggestion_type = t.get("suggestion_type", "")  # also available from watch_manager.check_active_watches payload
            _watch_direction_db = ""
            if watch_id:
                try:
                    import json as _j2
                    from db_pool import get_trading_forex as _gtf2
                    _bc2 = _gtf2()
                    _wr = _bc2.execute(
                        "SELECT validator_verdict, context, conditions_progress, user_thesis, "
                        "suggestion_type, direction FROM watch_suggestions WHERE id=?",
                        (watch_id,)
                    ).fetchone()
                    if _wr:
                        _orig_verdict = _wr[0] or "WATCH"
                        _user_thesis = _wr[3] or ""
                        _watch_suggestion_type = _wr[4] or _watch_suggestion_type
                        _watch_direction_db = (_wr[5] or "").upper()
                        try:
                            _ctx = _j2.loads(_wr[1]) if _wr[1] else {}
                            _orig_reasoning = _ctx.get("validator_reasoning", _orig_reasoning)
                            _watch_ctx = _ctx
                        except Exception:
                            pass
                        try:
                            _orig_conditions = _j2.loads(_wr[2]) if _wr[2] else _orig_conditions
                        except Exception:
                            pass
                except Exception as _we:
                    logger.debug("Could not enrich snipe context for watch #%s: %s", watch_id, _we)

            # ── Suppress notifications while a trade is already open on this pair ──
            _has_open_trade = False
            _prev_filled = bool(_watch_ctx.get("_snipe_filled"))  # watch was previously filled
            try:
                import requests as _rq2
                from broker_credentials import BrokerCredentials as _BC2
                _bc2 = _BC2().get_connection(user_id=uid, broker="oanda")
                _ok2, _bu2, _aid2 = _bc2.get("api_key",""), _bc2.get("base_url",""), _bc2.get("account_id","")
                _ot2 = _rq2.get(
                    f"{_bu2}/v3/accounts/{_aid2}/openTrades",
                    headers={"Authorization": f"Bearer {_ok2}"}, timeout=4
                ).json().get("trades", [])
                _has_open_trade = any(t2.get("instrument") == instrument for t2 in _ot2)
            except Exception as _oanda_err:
                logger.warning("[WATCH TIMER] %s: OANDA check failed (%s) — suppressing (fail safe)",
                               instrument, _oanda_err)
                return  # Skip cycle entirely — better than firing duplicate on OANDA outage

            state = _get_user_team_state(uid)
            if not _has_open_trade:
                state["notifications"].append({
                    "type": "snipe_triggered",
                    "instrument": instrument,
                    "watch_id": watch_id,
                    "conditions_met": _orig_conditions,
                    "raw_suggestion": t.get("raw_suggestion", ""),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            else:
                logger.debug("[WATCH TIMER] %s: suppressing snipe notification — trade already open", instrument)
            # Build rich scout_context — tells the validator:
            # 1. This is a snipe re-evaluation (triggered_by = "snipe")
            # 2. What it previously decided and why
            # 3. Which conditions were met
            # Never fire snipe direct for REJECT-verdict watches
            if _orig_verdict == "REJECT":
                logger.info("[WATCH TIMER] %s watch #%s skipped — validator REJECT verdict, not firing",
                            instrument, watch_id)
                return

            # ── PAIR-LEVEL COOLDOWN (30 min) ──────────────────────────────────
            # Applies to ALL watches on this pair — not just the one that filled.
            # Fixes the churn where different watches on the same pair kept firing
            # every 5 minutes immediately after cooldown expired.
            _pair_close_ts = _pair_last_close.get((uid, instrument), 0)
            if _pair_close_ts and (time.time() - _pair_close_ts) < _PAIR_COOLDOWN_SECS:
                _mins_remaining = (_PAIR_COOLDOWN_SECS - (time.time() - _pair_close_ts)) / 60
                logger.info(
                    "[WATCH TIMER] %s watch #%s: PAIR cooldown active (%.1f min remaining) — skipping",
                    instrument, watch_id, _mins_remaining
                )
                return

            # Watch-level fallback: if this specific watch just filled a trade that closed,
            # also respect the per-watch close timestamp (belt-and-suspenders).
            _last_close = _watch_ctx.get("_last_fill_close_time", 0)
            if _last_close and (time.time() - _last_close) < _PAIR_COOLDOWN_SECS:
                _mins_ago = (time.time() - _last_close) / 60
                logger.info("[WATCH TIMER] %s watch #%s: watch cooldown active (closed %.1f min ago) — skipping",
                            instrument, watch_id, _mins_ago)
                return

            # ── EUR_AUD London open volatility block (01:00–03:00 AM ET) ──────
            # EUR_AUD whips violently at London open — two consecutive full-stop losses
            # in this window (trades 1534/1544). Block new entries during this window.
            _VOLATILE_PAIRS = {"EUR_AUD"}
            if instrument in _VOLATILE_PAIRS:
                try:
                    from datetime import datetime as _dt2, timezone as _tz2
                    import zoneinfo as _zi2
                    _et_now = _dt2.now(_zi2.ZoneInfo("America/New_York"))
                    _et_hour = _et_now.hour + _et_now.minute / 60.0
                    if 1.0 <= _et_hour < 3.0:  # 01:00–02:59 AM ET = London open
                        logger.info(
                            "[WATCH TIMER] %s watch #%s: London open block (ET %.2fh is in 01:00–03:00) — skipping",
                            instrument, watch_id, _et_hour
                        )
                        return
                except Exception as _tz_err:
                    logger.debug("Could not check London open block: %s", _tz_err)

            # Detect user-submitted watches — pass their context so validator
            # gets Tim's chart + thesis + prior analysis instead of starting fresh
            _watch_source = _watch_ctx.get("source", "")
            _fired_by = "snipe"

            snipe_ctx = {
                "_watch_id": watch_id,
                "triggered_by": _fired_by,
                "watch_id": watch_id,
                # 2026-04-23: expose suggestion_type downstream so trading_cycle's
                # live_trades INSERT can set entry_type='kronos_snipe' vs 'snipe_direct',
                # and so _is_kronos_snipe gate-bypass checks resolve correctly.
                "suggestion_type": _watch_suggestion_type,
                # Setup ID required by trading_cycle setup validation gate
                # Primary: from stored watch context (set by scout → trading_cycle → watch_manager)
                "setup_id": _watch_ctx.get("setup_id") or _watch_ctx.get("setup_name", "") or f"snipe_watch_{watch_id}",
                "setup_name": _watch_ctx.get("setup_name", "") or f"snipe_watch_{watch_id}",
                "validator_verdict": _orig_verdict,
                "validator_reasoning": _orig_reasoning,
                "raw_suggestion": t.get("raw_suggestion", ""),
                "conditions_met": _orig_conditions,
                "user_thesis": _user_thesis or _watch_ctx.get("user_thesis", ""),
                # User chart context — injected into validator when snipe fires
                "validator_context": _watch_ctx.get("validator_context", ""),
                "validator_full_analysis": _watch_ctx.get("validator_full_analysis", ""),
                "user_chart_path": _watch_ctx.get("user_chart_path", ""),
                "conversation_context": _watch_ctx.get("conversation_context", []),
                # Pass through original watch context fields (confluence, direction, etc.)
                "confluence_score": _watch_ctx.get("confluence_score", 0),
                # Direction resolution — suggestion_type-aware:
                # - kronos_path_snipe: HONOR KRONOS'S DIRECTION (from watch_suggestions.direction
                #   or re_entry_direction in context). Kronos predicts reversals; live sniper
                #   always shows current momentum AGAINST kronos's prediction by design.
                #   Using live_direction here cancels kronos's edge. 2026-04-23: previously
                #   kronos BUY watches fired SELL trades (watch 2101 EUR_JPY, 2117 GBP_JPY).
                # - other snipes: keep prior behavior — live direction first, then watch
                #   direction, to avoid stale 2h+ validator watches firing wrong-way.
                "direction": (
                    (_watch_direction_db
                     or _watch_ctx.get("re_entry_direction")
                     or _watch_ctx.get("direction", ""))
                    if _watch_suggestion_type == "kronos_path_snipe"
                    else (t.get("live_direction")
                          or _watch_ctx.get("re_entry_direction")
                          or _watch_ctx.get("direction", ""))
                ),
                # Live sniper scores from the check that just ran (always current)
                "sniper_buy":  t.get("live_sniper_buy",  _watch_ctx.get("sniper_buy", 0)),
                "sniper_sell": t.get("live_sniper_sell", _watch_ctx.get("sniper_sell", 0)),
                "sniper_threshold": _watch_ctx.get("sniper_threshold", 12),
                # EMA fan direction and state from watch_manager — avoids re-fetch in snipe_direct
                "fan_direction": t.get("fan_direction", ""),
                "fan_state": t.get("fan_state", ""),
                # re-entry only counts if the previous fill is STILL OPEN — stale flag after close = fresh snipe
                "_prev_snipe_filled": _watch_ctx.get("_snipe_filled", False) and _has_open_trade,
                # Story score from watch creation — flows to live_trades INSERT
                "story_score": _watch_ctx.get("story_score", 0),
            }
            # ── Final gate: if trade is ALREADY OPEN on this pair, don't queue a cycle at all ──
            # The notification was already suppressed above. Now also skip the cycle.
            # This prevents the full pipeline (Haiku + Sonnet) running just to produce a HOLD.
            if _has_open_trade:
                logger.info("[WATCH TIMER] %s watch #%s: trade already open — skipping cycle queue",
                            instrument, watch_id)
                return
            result = _queue_cycle(uid, instrument, priority="high", source="snipe",
                                  scout_context=snipe_ctx)
            logger.info("🎯 [WATCH TIMER] Snipe triggered for %s (watch #%s verdict=%s) → %s",
                        instrument, watch_id, _orig_verdict, result)

        # ── On startup: re-queue any recently triggered snipes with no cycle ──
        # Catches snipes that triggered while serve_ui was down/restarting.
        try:
            _time.sleep(15)  # brief startup delay
            import sqlite3 as _s3b
            from db_pool import get_trading_forex as _gtf
            _bc = _gtf()
            from datetime import timedelta as _td2
            _cutoff = (datetime.now(timezone.utc) - _td2(hours=2)).isoformat()
            _orphans = _bc.execute("""
                SELECT id, instrument, raw_suggestion, conditions_progress
                FROM watch_suggestions
                WHERE status='triggered' AND trade_cycle_id IS NULL
                AND triggered_at > ?
            """, (_cutoff,)).fetchall()
            if _orphans:
                logger.info("[WATCH TIMER] Startup: %d orphaned triggered snipes found — re-queuing cycles", len(_orphans))
                _startup_fired = set()  # one cycle per pair on startup
                for _o in _orphans:
                    try:
                        import json as _j
                        _pair = _o[1]
                        if _pair in _startup_fired:
                            logger.info("[WATCH TIMER] Startup: skipping duplicate watch for %s", _pair)
                            continue
                        _fire_snipe_cycle({
                            "instrument": _pair,
                            "watch_id": _o[0],
                            "raw_suggestion": _o[2],
                            "conditions_met": _j.loads(_o[3]) if _o[3] else [],
                        })
                        _startup_fired.add(_pair)
                    except Exception as _oe:
                        logger.warning("[WATCH TIMER] Startup re-queue failed for %s: %s", _o[1], _oe)
        except Exception as _se:
            logger.warning("[WATCH TIMER] Startup orphan check failed: %s", _se)

        logger.info("[WATCH TIMER] Background thread running — first check in 60s")
        _time.sleep(60)  # Short initial delay (was full interval — missed first window)

        while True:
            try:
                cfg = _load_risk_config()
                interval = int(cfg.get("watch_check_interval_min", 5)) * 60
                from Source.agents.watch_manager import check_active_watches, get_active_watches
                from db_pool import get_trading_forex as _gtf_timer
                # Per-user iteration: each user gets their own watch check
                _tc = _gtf_timer()
                _active_users = _tc.execute(
                    "SELECT DISTINCT user_id FROM watch_suggestions WHERE status IN ('watching','triggered')"
                ).fetchall()
                if not _active_users:
                    logger.info("[WATCH TIMER] No active watches for any user — sleeping")
                    _time.sleep(interval)
                    continue
                for (_uid_row,) in _active_users:
                    uid_timer = _uid_row
                    try:
                        triggered = check_active_watches(user_id=uid_timer)
                        if triggered:
                            logger.info("[WATCH TIMER] user_id=%s: %d triggered", uid_timer, len(triggered))
                        fired_pairs = set()
                        for t in triggered:
                            try:
                                pair = t["instrument"]
                                if pair in fired_pairs:
                                    logger.info("[WATCH TIMER] Skipping watch #%s %s — already fired this interval",
                                                t.get("watch_id"), pair)
                                    continue
                                logger.info("[WATCH TIMER] FIRING snipe cycle for %s watch #%s (user=%s)",
                                            pair, t.get("watch_id"), uid_timer)
                                if flight:
                                    # 2026-04-23: log the EFFECTIVE direction (what the
                                    # trade will actually open as), not live_direction.
                                    # _fire_snipe_cycle uses watch_direction for kronos
                                    # path snipes (honoring kronos prediction) — logging
                                    # live_direction here caused false-positive "direction
                                    # flip" appearance on path snipes (trade 9967 case).
                                    _log_dir = (
                                        (t.get("watch_direction") or t.get("live_direction") or "")
                                        if t.get("suggestion_type") == "kronos_path_snipe"
                                        else (t.get("live_direction") or t.get("watch_direction") or "")
                                    )
                                    flight.record("SNIPE_TRIGGERED", pair=pair, data={
                                        "watch_id": t.get("watch_id", ""),
                                        "direction": _log_dir,
                                        "watch_direction": t.get("watch_direction", ""),
                                        "live_direction": t.get("live_direction", ""),
                                        "conditions_met": len(t.get("conditions_met", [])),
                                    }, note=f"Snipe triggered for watch #{t.get('watch_id')}")
                                _fire_snipe_cycle(t)
                                fired_pairs.add(pair)
                            except Exception as we:
                                logger.warning("[WATCH TIMER] Failed to start cycle: %s", we, exc_info=True)
                    except Exception as _ue:
                        logger.warning("[WATCH TIMER] Error checking user %s: %s", uid_timer, _ue)
                _time.sleep(interval)
            except Exception as exc:
                logger.warning("[WATCH TIMER] Error: %s", exc, exc_info=True)
                _time.sleep(60)

    import threading
    _watch_thread = threading.Thread(target=_watch_checker_loop, daemon=True)
    _watch_thread.start()
    logger.info("Watch checker background thread started (fallback — Scout is primary snipe checker)")

    # ── Startup data retention cleanup ───────────────────────────────────────
    # Runs once at startup. Prevents unbounded table growth over weeks/months.
    # Deferred to a background thread so Flask startup isn't blocked.
    def _run_data_cleanup():
        import time as _t
        _t.sleep(30)  # Wait for startup to settle
        try:
            from db_pool import get_trading_forex
            from Database.v2.db_helper import connection as v2_connection
            # agent_communications: keep 30 days (v2/agents.db)
            with v2_connection("agents") as _ac:
                _ac.execute("DELETE FROM agent_communication WHERE created_at < datetime('now', '-30 days')")
            # watch_suggestions: keep completed/expired/cancelled for 7 days (v2/trading_forex.db)
            _tf = get_trading_forex()
            _tf.execute("""DELETE FROM watch_suggestions
                WHERE status IN ('triggered', 'expired', 'cancelled', 'completed', 'superseded')
                AND created_at < datetime('now', '-7 days')""")
            # scout_findings: keep 90 days (v2/trading_forex.db)
            _tf.execute("DELETE FROM scout_findings WHERE timestamp < datetime('now', '-90 days')")
            logger.info("[STARTUP] Data retention cleanup complete")
        except Exception as _e:
            logger.debug("[STARTUP] Data retention cleanup failed (non-critical): %s", _e)

    threading.Thread(target=_run_data_cleanup, daemon=True, name="data-cleanup").start()

    # ── Position Guardian (parallel trade monitoring with threat scoring) ──
    # Module-level refs so trading_cycle.py can trigger immediate reconcile
    global _guardian_instance, _guardian_loop
    _guardian_instance = None
    _guardian_loop = None

    def _start_guardian_async():
        """Start the Position Guardian in its own asyncio event loop (background thread).

        2026-04-03: Added retry loop with exponential backoff. Previously, if this
        function threw ANY exception the daemon thread died silently and ALL trades
        ran unprotected. EUR_AUD #4427 lost -23.1p ($160) because the guardian was
        dead for 3+ hours after a server restart. No guardian = no threat scoring,
        no ratchet, no emergency close.
        """
        global _guardian_instance, _guardian_loop
        import time as _time
        _MAX_GUARDIAN_RETRIES = 10
        _retry_delay = 10  # seconds, doubles each failure (max 120s)

        for _attempt in range(1, _MAX_GUARDIAN_RETRIES + 1):
            try:
                return _start_guardian_inner(_attempt)
            except Exception as _guardian_err:
                _guardian_instance = None
                _guardian_loop = None
                logger.critical(
                    "🚨 [GUARDIAN] Startup CRASHED (attempt %d/%d): %s — "
                    "ALL TRADES UNPROTECTED until guardian restarts. Retrying in %ds.",
                    _attempt, _MAX_GUARDIAN_RETRIES, _guardian_err, _retry_delay,
                    exc_info=True,
                )
                _time.sleep(_retry_delay)
                _retry_delay = min(120, _retry_delay * 2)

        logger.critical(
            "🚨🚨🚨 [GUARDIAN] FAILED TO START after %d attempts — "
            "TRADES ARE COMPLETELY UNPROTECTED. Manual intervention required.",
            _MAX_GUARDIAN_RETRIES,
        )

    def _start_guardian_inner(_attempt_num=1):
        """Inner guardian startup — separated so retry wrapper can catch all exceptions."""
        global _guardian_instance, _guardian_loop
        import time as _time
        if _attempt_num == 1:
            _time.sleep(10)  # Wait for server startup on first attempt only

        # Resolve user_id from env (set by serve_ui.py) or DB fallback
        _guardian_user_id = _resolve_admin_user_id()
        if not _guardian_user_id:
            raise RuntimeError("Cannot resolve user_id — no TRADING_USER_ID env and no admin in core.db")

        try:
            from Source.oanda_client import OandaClient
            from Source.position_guardian import PositionGuardian
        except ImportError:
            from oanda_client import OandaClient
            from position_guardian import PositionGuardian

        _g_client = OandaClient()

        # ── Callbacks ──

        async def _on_status(trade_id, threat_dict):
            """Every M1 tick for every trade — broadcast to dashboard."""
            try:
                state = _get_user_team_state(_guardian_user_id)
                if 'guardian_threats' not in state:
                    state['guardian_threats'] = {}
                state['guardian_threats'][trade_id] = threat_dict

                # Rolling threat history for sustained-threat gating of auto_close_threat90.
                # Backtest (Apr 7-20): 0/10 WR on threat90 closes — threat spikes >=90
                # briefly during retrace->trending flips, then trend continues. Requiring
                # N consecutive ticks >= threshold filters out these transient spikes.
                if 'guardian_threat_history' not in state:
                    state['guardian_threat_history'] = {}
                _th_hist = state['guardian_threat_history'].setdefault(trade_id, [])
                _th_hist.append(int(threat_dict.get('threat_level', 0) or 0))
                if len(_th_hist) > 20:
                    del _th_hist[:-20]

                # Push via SSE (user-scoped) when available; fall back to WebSocket broadcast
                event_data = {'type': 'threat_update', 'user_id': _guardian_user_id, **threat_dict}
                if _sse_push_fn is not None:
                    _sse_push_fn('threat_update', event_data, target_user_id=_guardian_user_id)
                elif hasattr(app, '_ws_clients'):
                    msg = json.dumps(event_data)
                    dead = set()
                    for ws in app._ws_clients:
                        try:
                            await asyncio.wait_for(ws.send(msg), timeout=3)
                        except Exception:
                            dead.add(ws)
                    app._ws_clients -= dead
            except Exception:
                pass

        async def _on_escalation(trade_id, report_dict):
            """RED zone — evaluate whether to close the trade.

            Threat 61-74: Send to Trade Monitor LLM for evaluation (HOLD/TIGHTEN/CLOSE).
            Threat 75+:  Auto-close — too dangerous to wait for LLM reasoning.
            """
            state = _get_user_team_state(_guardian_user_id)
            state.setdefault('guardian_escalations', []).append({
                **report_dict,
                'escalated_at': datetime.now(timezone.utc).isoformat(),
            })
            state['guardian_escalations'] = state['guardian_escalations'][-50:]

            threat_level = report_dict.get('threat_level', 0)
            pair = report_dict.get('pair', '?')
            direction = report_dict.get('direction', '?')

            logger.warning("[GUARDIAN] RED escalation for %s %s — threat %d: %s",
                          trade_id, pair, threat_level,
                          report_dict.get('reasons', []))

            # ── TIERED RESPONSE ──
            # Threat 90+: Hard auto-close — no LLM deliberation, too dangerous.
            # 2026-04-07: Raised from 75→90. Backtest of 113 trades showed 75 and 90
            # kill the same 5 winners on M15; difference is only 4 marginal loss trades.
            # Trades #4792/#4796 killed at 87/95 during retrace oscillation — at 90,
            # #4792 would have survived. Combined with M15-gated retrace state machine,
            # this provides much better retrace protection.
            # GUARD: require minimum 5 M1 candles (~5 min) before auto-close fires.
            # True emergencies (margin, spread spike) bypass this via emergency_threat
            # path in score_threat() and watcher._evaluate_once() directly.
            _candles_in = report_dict.get('candles_in_trade', 0)
            _is_true_emergency = report_dict.get('emergency', False)  # spread spike + margin only
            _is_entry_noise = (not _is_true_emergency and _candles_in < 5)  # trend noise in first 5 min

            # ── RETRACEMENT DEFENSE-IN-DEPTH ──
            # The guardian already suppresses escalation during retracement, but if
            # a high threat somehow reaches this auto-close path while the trade is
            # in retracement / continuing / post-retrace cooldown, do NOT auto-close.
            # True emergencies (spread spike, margin) bypass this — they must close.
            _retrace_ctx = report_dict.get('retrace_context', {})
            _retrace_st = _retrace_ctx.get('retrace_state', '')
            _in_retrace_protection = _retrace_st in ('retracing', 'continuing')
            # 2026-04-23: NEVER suppress for never-in-profit trades.
            # Mirrors position_guardian.py:2072 logic. Trades 9435 EUR_JPY and
            # 9729 NZD_USD hit BLACK 84 after never going green, and this path
            # suppressed them 8+ times each while they bled to -18/-19p.
            # Position_guardian has the _ever_in_profit guard already, but
            # trades reaching THIS path (post rate-limit cooldown, emergency
            # downgrade) bypass that check. Mirror it here.
            _peak_cached = float(_retrace_ctx.get('peak_pips_cached', 0.0) or 0.0)
            _ever_in_profit = _peak_cached > 1.0
            if not _ever_in_profit:
                _in_retrace_protection = False
            # Kronos trades use a different threat scorer (score_threat_kronos)
            # that has its own retrace awareness: it caps RED until fan actually
            # flips or price breaks E100 against direction. When Kronos scorer
            # says RED, it IS a structural break — scout's retrace machine is
            # measuring scout-thesis retrace (BB contraction after fan peak) which
            # is orthogonal. Verified against today's USD_JPY #6506 (scorer went
            # RED at -17.1p, suppressed 6 times, trade ran to -31.3p). Bypass
            # retrace-suppression when a Kronos-sourced trade's scorer fires.
            _scorer = report_dict.get('scorer', 'scout')
            if _scorer == 'kronos':
                _in_retrace_protection = False
            if _in_retrace_protection and not _is_true_emergency:
                logger.warning(
                    "[GUARDIAN] Threat %d >= 90 for %s BUT retrace_state=%s — "
                    "SUPPRESSING auto-close (defense-in-depth). Trade stays open.",
                    threat_level, trade_id, _retrace_st)
                if flight:
                    flight.record(FlightStage.GUARDIAN_ACTION, pair=pair,
                                  trade_id=trade_id, data={
                        "action": "auto_close_suppressed_retrace",
                        "threat_level": threat_level,
                        "retrace_state": _retrace_st,
                        "pnl_pips": report_dict.get('current_pnl_pips', 0),
                    }, status="info", note=f"Auto-close suppressed: retrace_state={_retrace_st}")
                return  # Do NOT close — retracement protection active

            # ── AUTO_CLOSE_THREAT90 KILL SWITCH (2026-04-20) ──
            # Disabled because the threat scorer over-fires on normal M15 oscillation
            # (fan compression near E100 on SELL approaching support is scored as
            # "trend structure gone" even when candles still respect EMAs in trade
            # direction). Trade 7815 EUR_AUD closed at -1.5p with 93% of SL unused
            # on a flat position — scorer sustained 90+ for 5+ minutes on what was
            # normal behavior. Dynamic SL trail + planned SL + true emergency
            # (spread spike / margin) still protect the trade.
            # Re-enable only after scorer is rewritten to use candle-to-EMA position
            # as primary signal instead of fan structure alone.
            try:
                from tuning_config import get as _tc_get
                _threat90_enabled = bool(_tc_get("guardian.auto_close_threat90_enabled", False))
            except Exception:
                _threat90_enabled = False
            if threat_level >= 90 and not _is_entry_noise and not _threat90_enabled and not _is_true_emergency:
                logger.warning(
                    "[GUARDIAN] Threat %d >= 90 for %s — auto_close DISABLED via "
                    "guardian.auto_close_threat90_enabled=False. Trade stays open. "
                    "Dynamic SL trail + planned SL still protect.",
                    threat_level, trade_id)
                if flight:
                    flight.record(FlightStage.GUARDIAN_ACTION, pair=pair,
                                  trade_id=trade_id, data={
                        "action": "auto_close_threat90_disabled",
                        "threat_level": threat_level,
                        "reasons": report_dict.get('reasons', [])[:3],
                        "pnl_pips": report_dict.get('current_pnl_pips', 0),
                    }, status="info",
                       note=f"auto_close_threat90 disabled (kill switch) at threat={threat_level}")
                return

            # 2026-05-13 (Tim approved): fan-intact bypass. If the fan structure
            # (EMA21/EMA55) is still ordered in trade direction, the trend has
            # NOT actually failed — threat scorer may be reading retrace as
            # structure loss. 14 historical auto_close_threat90 fires = 14
            # losses (-$651) — all closed during retrace while fan still intact.
            # Per Tim: "the fan is there until the EMAs cross". Bypass close.
            _fan_intact_for_kill = bool(report_dict.get('fan_intact', False))
            if (threat_level >= 90 and not _is_entry_noise
                    and _fan_intact_for_kill and not _is_true_emergency):
                logger.warning(
                    "[GUARDIAN] Threat %d >= 90 for %s BUT fan_intact=True — "
                    "SUPPRESSING auto-close (trend structure still ordered). "
                    "Trade stays open. SL trail will catch true failure.",
                    threat_level, trade_id)
                if flight:
                    flight.record(FlightStage.GUARDIAN_ACTION, pair=pair,
                                  trade_id=trade_id, data={
                        "action": "auto_close_suppressed_fan_intact",
                        "threat_level": threat_level,
                        "pnl_pips": report_dict.get('current_pnl_pips', 0),
                    }, status="info",
                       note=f"Auto-close suppressed: fan_intact=True at threat={threat_level}")
                return

            if threat_level >= 90 and not _is_entry_noise:
                logger.warning("[GUARDIAN] Threat %d >= 90 — AUTO-CLOSING %s immediately (no LLM)",
                               threat_level, trade_id)
                try:
                    from Source.oanda_client import OandaClient as _OC75
                    _oc75 = _OC75()
                    _oc75.close_trade(trade_id)
                    logger.info("[GUARDIAN] Auto-closed trade %s at threat=%d", trade_id, threat_level)
                    # Cleanup threat history buffer for this trade (closed)
                    if state and 'guardian_threat_history' in state:
                        state['guardian_threat_history'].pop(trade_id, None)
                    if flight:
                        flight.record(FlightStage.GUARDIAN_ACTION, pair=pair,
                                      trade_id=trade_id, data={
                            "action": "auto_close_threat90",
                            "threat_level": threat_level,
                            "reasons": report_dict.get('reasons', [])[:3],
                            "pnl_pips": report_dict.get('current_pnl_pips', 0),
                        }, status="warn", note=f"Auto-close: threat {threat_level} >= 90")
                except Exception as _ce75:
                    logger.error("[GUARDIAN] Auto-close failed for trade %s: %s", trade_id, _ce75)
                return

            # ══════════════════════════════════════════════════════════════════
            # TRADE MONITOR LLM CLOSE AUTHORITY — DISABLED (2026-04-06)
            #
            # Audit of 46 losses found 20 (43%, $697) were killed early by the
            # Trade Monitor LLM. It receives threat data but lacks thesis
            # awareness (entry_type, is_mean_reversion, invalidation_level)
            # and closes trades the guardian's retrace logic says to hold.
            #
            # Trade #4754 EUR_AUD confirmed: guardian showed TREND RESUMING,
            # candle-EMA logic said hold, LLM closed at -11.5p. OANDA log
            # confirms market close order (not SL hit).
            #
            # The guardian now has full retrace state machine, candle-E55
            # conviction scoring, 12 specialized close paths, and structural
            # exit system. It is the sole trade manager.
            #
            # The Trade Monitor LLM is repurposed as a UI narrator only (V5 prompt) —
            # it narrates what the guardian is doing, not makes close decisions.
            #
            # Vision escalation: REMOVED. The Validator agent already owns chart
            # vision via VisionValidator class — it was only called here as a
            # tiebreaker through the Trade Monitor LLM, which is now disabled.
            # The Validator evaluates charts during normal pipeline + floor chat.
            # ══════════════════════════════════════════════════════════════════
            logger.info(
                "[GUARDIAN] RED zone threat=%d for %s — guardian manages (LLM close disabled). "
                "Retrace: %s, P&L: %.1fp",
                threat_level, trade_id, _retrace_st,
                report_dict.get('current_pnl_pips', 0))
            if flight:
                flight.record(FlightStage.GUARDIAN_ACTION, pair=pair,
                              trade_id=trade_id, data={
                    "action": "red_zone_guardian_manages",
                    "threat_level": threat_level,
                    "retrace_state": _retrace_st,
                    "pnl_pips": report_dict.get('current_pnl_pips', 0),
                    "reasons": report_dict.get('reasons', [])[:3],
                }, status="info", note=f"RED {threat_level} — guardian sole manager (LLM close disabled)")

            # Generate narrative via local 9B model for push notification
            _narrative = ""
            try:
                from guardian_narrator import narrate_escalation
                _narrative = narrate_escalation(report_dict)
                logger.info("[NARRATOR] %s: %s", trade_id, _narrative)
            except Exception as _narr_err:
                logger.debug("[NARRATOR] Failed for %s: %s", trade_id, _narr_err)

            # Also broadcast escalation to dashboard (user-scoped)
            try:
                event_data = {'type': 'threat_escalation', 'user_id': _guardian_user_id,
                              'narrative': _narrative, **report_dict}
                if _sse_push_fn is not None:
                    _sse_push_fn('threat_escalation', event_data, target_user_id=_guardian_user_id)
                elif hasattr(app, '_ws_clients'):
                    msg = json.dumps(event_data)
                    for ws in list(getattr(app, '_ws_clients', set())):
                        try:
                            await asyncio.wait_for(ws.send(msg), timeout=3)
                        except Exception:
                            pass
            except Exception:
                pass

        async def _on_emergency(trade_id, reason_str):
            """BLACK zone — trade already killed. Log and notify."""
            logger.critical("[GUARDIAN] BLACK EMERGENCY — trade %s killed: %s", trade_id, reason_str)
            state = _get_user_team_state(_guardian_user_id)
            state.setdefault('guardian_emergencies', []).append({
                'trade_id': trade_id,
                'reason': reason_str,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
            state['guardian_emergencies'] = state['guardian_emergencies'][-20:]

            state['notifications'].append({
                'type': 'emergency_close',
                'trade_id': trade_id,
                'action': f'🚨 EMERGENCY CLOSE: {reason_str}',
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })

            # Broadcast to dashboard (user-scoped)
            try:
                event_data = {'type': 'emergency_close', 'user_id': _guardian_user_id,
                              'trade_id': trade_id, 'reason': reason_str}
                if _sse_push_fn is not None:
                    _sse_push_fn('emergency_close', event_data, target_user_id=_guardian_user_id)
                elif hasattr(app, '_ws_clients'):
                    msg = json.dumps(event_data)
                    for ws in list(getattr(app, '_ws_clients', set())):
                        try:
                            await asyncio.wait_for(ws.send(msg), timeout=3)
                        except Exception:
                            pass
            except Exception:
                pass

        # Create and run
        _guardian_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_guardian_loop)

        _guardian_instance = PositionGuardian(
            oanda_client=_g_client,
            on_status_update=_on_status,
            on_escalation=_on_escalation,
            on_emergency=_on_emergency,
        )

        _guardian_loop.run_until_complete(_guardian_instance.start())
        logger.info("Position Guardian running — watching for open trades")
        _guardian_loop.run_forever()

    import threading
    _guardian_thread = threading.Thread(target=_start_guardian_async, daemon=True)
    _guardian_thread.start()
    logger.info("Position Guardian background thread started")

    # ── Start Kronos Hunter (M15 discovery loop) ─────────────────────────
    try:
        from tuning_config import TUNING
        if TUNING["kronos.enabled"]["value"] and TUNING["kronos.hunter_enabled"]["value"]:
            from kronos_runtime import get_kronos_hunter
            from kronos_hunter import run_forever as kronos_run_forever
            hunter = get_kronos_hunter()
            if hunter is not None:
                def _hunter_thread():
                    try:
                        kronos_run_forever(
                            hunter,
                            master_enabled_fn=lambda: TUNING["kronos.enabled"]["value"],
                            hunter_enabled_fn=lambda: TUNING["kronos.hunter_enabled"]["value"],
                        )
                    except Exception as exc:
                        logger.exception("Kronos Hunter loop crashed: %s", exc)
                t = threading.Thread(target=_hunter_thread, name="kronos-hunter",
                                     daemon=True)
                t.start()
                logger.info("Kronos Hunter thread started (shadow_mode=%s)",
                            TUNING["kronos.shadow_mode"]["value"])
    except Exception as exc:
        logger.warning("Kronos Hunter boot skipped: %s", exc)

    # ── Kronos Rollback Tripwire — independent daemon ────────────────────
    try:
        import subprocess
        from pathlib import Path as _Path
        _tripwire_path = str(_Path(__file__).resolve().parent / "scripts" / "kronos_rollback_tripwire.py")
        # Singleton guard: skip spawn if another instance already running.
        # Without this, every serve_ui restart stacks another daemon — we hit 50 zombies 2026-04-23.
        _existing = subprocess.run(
            ["pgrep", "-f", "kronos_rollback_tripwire.py"],
            capture_output=True, text=True,
        )
        _existing_pids = [p for p in _existing.stdout.strip().splitlines() if p]
        if _existing_pids:
            logger.info("[KRONOS] rollback tripwire already running (pid=%s) — skipping spawn",
                        ",".join(_existing_pids))
        else:
            _tripwire_proc = subprocess.Popen(
                [sys.executable, _tripwire_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            logger.info("[KRONOS] rollback tripwire spawned (pid=%s)", _tripwire_proc.pid)
    except Exception as exc:
        logger.error("[KRONOS] tripwire spawn failed: %s", exc)
    # ─────────────────────────────────────────────────────────────────────

    # ── Intelligence Briefing Scheduler (3x/day: 6 AM, 12 PM, 5 PM ET) ──
    _intel_last_refresh = {"timestamp": None, "summary": None}

    def _intelligence_scheduler_loop():
        """Background thread: refreshes intelligence cache 3x/day + once on startup."""
        import time as _time
        _time.sleep(15)  # Wait for server startup

        try:
            from intelligence_agent_prep import run_refresh, ALL_PAIRS
        except Exception:
            try:
                from Source.intelligence_agent_prep import run_refresh, ALL_PAIRS
            except Exception:
                logger.error("Intelligence scheduler: could not import intelligence_agent_prep", exc_info=True)
                return

        # Refresh schedule: session-aligned
        # Asia open 17:00 ET, London open 03:00 ET, NY open 08:00 ET
        REFRESH_HOURS = [3, 8, 17]

        def _current_et_hour():
            """Get current hour in ET."""
            from datetime import datetime, timezone, timedelta
            utc_now = datetime.now(timezone.utc)
            # ET = UTC-5 (EST) or UTC-4 (EDT). Use rough check.
            import calendar
            month = utc_now.month
            # EDT: March second Sunday to November first Sunday
            is_dst = 3 < month < 11  # Rough approximation
            offset = timedelta(hours=-4 if is_dst else -5)
            et_now = utc_now + offset
            return et_now.hour, et_now.minute

        def _do_refresh(reason: str):
            """Execute a full refresh and store result."""
            logger.info(f"[INTEL SCHEDULER] Starting refresh: {reason}")
            try:
                summary = run_refresh(ALL_PAIRS, reason)
                _intel_last_refresh["timestamp"] = datetime.now(timezone.utc).isoformat()
                _intel_last_refresh["summary"] = {
                    "session": summary.get("session"),
                    "pairs_ok": summary.get("pairs_ok"),
                    "pairs_failed": summary.get("pairs_failed"),
                    "wolfram_calls": summary.get("wolfram_calls"),
                    "elapsed_seconds": summary.get("elapsed_seconds"),
                }
                logger.info(f"[INTEL SCHEDULER] Refresh complete: {summary.get('pairs_ok')}/{summary.get('pairs_total')} OK in {summary.get('elapsed_seconds')}s")
            except Exception as e:
                logger.error(f"[INTEL SCHEDULER] Refresh failed: {e}", exc_info=True)

        import subprocess as _subprocess, socket as _sock, time as _t

        # Match the watchdog's launch config — same base + distilled adapter.
        # Without --adapter-path, a scheduler-launched 35B serves the BASE model,
        # while validator calls expect the distilled one. Keep them identical so
        # it doesn't matter who wins the race to port 11502.
        _CSO_CMD = [
            sys.executable,
            os.path.join(_JARVIS_ROOT, "scripts", "mlx_vlm_server_with_tools.py"),
            "--model", "mlx-community/Qwen3.5-35B-A3B-4bit",
            "--adapter-path", os.path.join(_JARVIS_ROOT, "models", "adapters", "35b_mlx"),
            "--port", "11502",
            "--host", "127.0.0.1",
        ]
        _cso_proc = None  # track the launched process

        def _is_cso_up():
            try:
                with _sock.create_connection(("127.0.0.1", 11502), timeout=2):
                    return True
            except Exception:
                return False

        def _start_cso(timeout_s=120):
            """Launch the 35B CSO model server and wait until it accepts connections."""
            nonlocal _cso_proc

            if _is_cso_up():
                logger.info("[INTEL SCHEDULER] CSO 35B already up on port 11502")
                return True

            logger.info("[INTEL SCHEDULER] Starting CSO 35B model server...")
            import os as _os
            env = dict(_os.environ)
            _pybin = os.path.dirname(sys.executable)
            env["PATH"] = f"{_pybin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            _cso_log = os.path.join(_JARVIS_ROOT, "Logs", "mlx", "CSO.log")
            os.makedirs(os.path.dirname(_cso_log), exist_ok=True)
            _cso_proc = _subprocess.Popen(
                _CSO_CMD,
                stdout=open(_cso_log, "a"),
                stderr=_subprocess.STDOUT,
                env=env,
            )
            logger.info("[INTEL SCHEDULER] CSO 35B launched (PID %d) — waiting for ready...", _cso_proc.pid)

            waited = 0
            while waited < timeout_s:
                _t.sleep(5); waited += 5
                if _is_cso_up():
                    logger.info("[INTEL SCHEDULER] CSO 35B ready after %ds", waited)
                    return True
                if _cso_proc.poll() is not None:
                    logger.error("[INTEL SCHEDULER] CSO 35B process exited early (rc=%d)", _cso_proc.returncode)
                    _cso_proc = None
                    return False

            logger.error("[INTEL SCHEDULER] CSO 35B not ready after %ds — synthesis will fail this run", timeout_s)
            return False

        def _stop_cso():
            """Terminate the 35B CSO model server IF the scheduler launched it.

            Never touch a 35B we didn't start — the watchdog and trading cycle
            may own it for validator calls. Prior lsof-fallback killed the
            watchdog-managed 35B every refresh (observed 2026-04-23 11:46).
            """
            nonlocal _cso_proc
            if _cso_proc is not None and _cso_proc.poll() is None:
                logger.info("[INTEL SCHEDULER] Stopping CSO 35B (PID %d) that we launched...", _cso_proc.pid)
                _cso_proc.terminate()
                try:
                    _cso_proc.wait(timeout=15)
                except _subprocess.TimeoutExpired:
                    _cso_proc.kill()
                    _cso_proc.wait()
                logger.info("[INTEL SCHEDULER] CSO 35B stopped")
                _cso_proc = None
            else:
                logger.info("[INTEL SCHEDULER] CSO 35B belongs to another owner (watchdog/trading cycle) — leaving it up")

        _INTEL_PAUSE_FILE = os.path.join(_JARVIS_ROOT, "intel_scheduler.pause")

        def _is_intel_paused():
            """Check if intelligence scheduler is paused via control panel."""
            if not os.path.exists(_INTEL_PAUSE_FILE):
                return False
            try:
                mtime = os.path.getmtime(_INTEL_PAUSE_FILE)
                now = _t.time()
                expires = mtime + 7200  # default 2h
                try:
                    with open(_INTEL_PAUSE_FILE) as f:
                        data = json.load(f)
                    if "expires" in data:
                        expires = float(data["expires"])
                except Exception:
                    pass
                if now >= expires:
                    os.remove(_INTEL_PAUSE_FILE)
                    return False
                remaining = int((expires - now) / 60)
                logger.info("[INTEL SCHEDULER] PAUSED (%dm remaining) — skipping refresh", remaining)
                return True
            except Exception:
                return False

        def _run_refresh_with_cso(reason: str):
            """Start 35B, run refresh for all pairs, then stop 35B."""
            if _is_intel_paused():
                return
            started = _start_cso(timeout_s=120)
            if not started:
                logger.error("[INTEL SCHEDULER] Skipping refresh '%s' — CSO 35B failed to start", reason)
                return
            try:
                _do_refresh(reason)
            finally:
                _stop_cso()

        # Initial refresh on startup
        if _is_intel_paused():
            logger.info("[INTEL SCHEDULER] Startup refresh skipped — paused via control panel")
        else:
            _run_refresh_with_cso("startup")

        # Then check every 10 minutes if we've hit a scheduled hour
        _last_triggered_hour = None
        while True:
            try:
                _time.sleep(600)  # Check every 10 min
                hour, minute = _current_et_hour()

                # Check if we're within the first 15 minutes of a refresh hour
                if hour in REFRESH_HOURS and minute < 15 and _last_triggered_hour != hour:
                    _last_triggered_hour = hour
                    session_pairs = ALL_PAIRS  # Always refresh all 13 pairs
                    label = {3: "london_open", 8: "ny_open", 17: "asia_open"}.get(hour, f"{hour}:00")
                    logger.info(f"[INTEL SCHEDULER] Session refresh: {label} → {len(session_pairs)} pairs (all pairs)")
                    _run_refresh_with_cso(label)
                    _last_triggered_hour = hour
                    continue

            except Exception as exc:
                logger.warning(f"[INTEL SCHEDULER] Loop error: {exc}")
                _time.sleep(60)

    from datetime import datetime, timezone
    _intel_thread = threading.Thread(target=_intelligence_scheduler_loop, daemon=True)
    _intel_thread.start()
    logger.info("Intelligence briefing scheduler started (refreshes at 3 AM London, 8 AM NY, 5 PM Asia + startup)")

    # ── Intelligence API endpoints ──

    @app.route("/api/trading/intelligence/status", methods=["GET", "OPTIONS"])
    def api_intelligence_status():
        """Get current intelligence cache status and last refresh info."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        # Read the status file
        status_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "dashboard", "intelligence_status.json"
        )
        file_status = None
        if os.path.exists(status_path):
            try:
                with open(status_path) as f:
                    file_status = json.load(f)
            except Exception:
                pass
        
        return jsonify({
            "scheduler_running": _intel_thread.is_alive(),
            "last_refresh": _intel_last_refresh,
            "file_status": file_status,
        })

    @app.route("/api/trading/intelligence/refresh", methods=["POST", "OPTIONS"])
    def api_intelligence_refresh():
        """Trigger an immediate intelligence refresh."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        
        try:
            from intelligence_agent_prep import run_refresh, ALL_PAIRS
        except ImportError:
            from Source.intelligence_agent_prep import run_refresh, ALL_PAIRS
        
        pairs = request.json.get("pairs", ALL_PAIRS) if request.is_json else ALL_PAIRS
        summary = run_refresh(pairs, "manual_trigger")
        _intel_last_refresh["timestamp"] = datetime.now(timezone.utc).isoformat()
        _intel_last_refresh["summary"] = {
            "pairs_ok": summary.get("pairs_ok"),
            "pairs_failed": summary.get("pairs_failed"),
            "elapsed_seconds": summary.get("elapsed_seconds"),
        }
        return jsonify(summary)

    @app.route("/api/trading/intelligence/briefing/<instrument>", methods=["GET", "OPTIONS"])
    def api_intelligence_briefing(instrument):
        """Get the AI-synthesized briefing for a specific pair."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from intelligence_store import IntelligenceStore
            store = IntelligenceStore()
            cached = store.get_cached(f"briefing:ai:{instrument}")
            store.close()
            if cached:
                data = json.loads(cached) if isinstance(cached, str) else cached
                return jsonify({"instrument": instrument, "briefing": data.get("briefing", cached), "macro": data.get("macro", {}), "generated_at": data.get("generated_at")})
            else:
                return jsonify({"instrument": instrument, "briefing": None, "error": "No cached briefing. Refresh pending."}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Guardian API endpoints ──

    @app.route("/api/trading/guardian/status", methods=["GET", "OPTIONS"])
    def api_guardian_status():
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        if _guardian_instance:
            return jsonify({
                "running": _guardian_instance.is_running,
                "active_watchers": _guardian_instance.active_watchers,
                "stats": _guardian_instance.get_stats(),
            })
        return jsonify({"running": False, "active_watchers": 0})

    @app.route("/api/trading/guardian/threats", methods=["GET", "OPTIONS"])
    def api_guardian_threats():
        """Get current threat assessment for all watched trades."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        state = _get_user_team_state(user_info['user_id'])
        threats = state.get('guardian_threats', {}) if state else {}
        return jsonify({"threats": threats, "count": len(threats)})

    @app.route("/api/trading/guardian/escalations", methods=["GET", "OPTIONS"])
    def api_guardian_escalations():
        """Get recent RED zone escalations."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        state = _get_user_team_state(user_info['user_id'])
        return jsonify({"escalations": state.get('guardian_escalations', []) if state else []})

    @app.route("/api/trading/guardian/kill/<trade_id>", methods=["POST", "OPTIONS"])
    def api_guardian_kill(trade_id):
        """Emergency kill switch — force-close a trade from dashboard."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        if _guardian_instance and _guardian_loop:
            future = asyncio.run_coroutine_threadsafe(
                _guardian_instance.force_close(trade_id, reason="dashboard_kill_switch"),
                _guardian_loop,
            )
            try:
                result = future.result(timeout=10)
                return jsonify({"success": result, "trade_id": trade_id})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500
        return jsonify({"success": False, "error": "Guardian not running"}), 503

    # ── Setup Revenue API endpoints ──

    @app.route("/api/trading/revenue/pair/<pair>", methods=["GET", "OPTIONS"])
    def api_revenue_by_pair(pair):
        """Get setup revenue breakdown for a specific pair."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from Source.setup_revenue import SetupRevenueTracker
            tracker = SetupRevenueTracker()
            user_id = user_info["user_id"]  # SECURITY: from token, not query params
            revenue = tracker.get_revenue_by_pair(pair, user_id)
            recent = tracker.get_recent_trades(pair=pair, limit=10, user_id=user_id)
            return jsonify({"pair": pair, "setups": revenue, "recent_trades": recent})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/trading/revenue/all", methods=["GET", "OPTIONS"])
    def api_revenue_all():
        """Get all setup revenue data."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from Source.setup_revenue import SetupRevenueTracker
            tracker = SetupRevenueTracker()
            user_id = user_info["user_id"]  # SECURITY: from token, not query params
            return jsonify({
                "setups": tracker.get_all_revenue(user_id),
                "top_setups": tracker.get_top_setups(user_id=user_id),
                "pair_summary": tracker.get_pair_summary(user_id),
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/trading/revenue/snipes", methods=["GET", "OPTIONS"])
    def api_revenue_snipes():
        """Get user's promoted snipe list with lifetime revenue."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from Source.setup_revenue import SetupRevenueTracker
            tracker = SetupRevenueTracker()
            user_id = user_info["user_id"]  # SECURITY: from token, not query params
            active_only = request.args.get('active_only', 'true').lower() == 'true'
            return jsonify({"snipes": tracker.get_snipe_list(user_id, active_only)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/trading/revenue/snipes/add", methods=["POST", "OPTIONS"])
    def api_revenue_snipe_add():
        """Manually add a setup to snipe list."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from Source.setup_revenue import SetupRevenueTracker
            data = request.get_json() or {}
            tracker = SetupRevenueTracker()
            result = tracker.manual_add_snipe(
                setup_name=data.get('setup_name', ''),
                pair=data.get('pair', ''),
                direction=data.get('direction'),
                notes=data.get('notes', ''),
                user_id=data.get('user_id') or _resolve_admin_user_id(),
            )
            return jsonify({"success": result})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Test endpoint: inject fake guardian threat for UI testing ──
    @app.route("/api/trading/guardian/test-inject", methods=["POST", "OPTIONS"])
    def api_guardian_test_inject():
        """Inject a fake threat for UI testing. POST with {"pair": "EUR_GBP", "zone": "YELLOW"}"""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        data = request.get_json() or {}
        pair = data.get('pair', 'EUR_GBP')
        test_zone = data.get('zone', 'YELLOW')
        levels = {'GREEN': 15, 'YELLOW': 45, 'RED': 70, 'BLACK': 85}
        breakdowns = {
            'GREEN': {'trend': -15, 'structure': 10, 'momentum': 0, 'emergency': 0},
            'YELLOW': {'trend': 20, 'structure': 15, 'momentum': 10, 'emergency': 0},
            'RED': {'trend': 20, 'structure': 35, 'momentum': 15, 'emergency': 0},
            'BLACK': {'trend': 0, 'structure': 0, 'momentum': 0, 'emergency': 85},
        }
        reasons_map = {
            'GREEN': ['Trend intact: bullish fan expanding, health 85'],
            'YELLOW': ['Trend peaked — bullish momentum maxed out', 'Price testing E100 (0.032%) — no rejection yet', 'Momentum fading: 2/3 indicators against trade'],
            'RED': ['Trend peaked — bullish momentum maxed out', 'Reversal pattern (bearish_engulfing) at E100 — high-conviction exit signal', 'Momentum exhaustion: RSI 81, Stoch K 83, MACD against — trend weak'],
            'BLACK': ['SPREAD SPIKE: 0.00060 (normal 0.00012)'],
        }
        fake_threat = {
            'trade_id': 'TEST_001',
            'pair': pair,
            'instrument': pair,
            'direction': 'buy',
            'zone': test_zone,
            'threat_level': levels.get(test_zone, 45),
            'breakdown': breakdowns.get(test_zone, breakdowns['YELLOW']),
            'reasons': reasons_map.get(test_zone, reasons_map['YELLOW']),
            'pnl_pips': 12.3,
            'r_multiple': 0.82,
            'entry_price': 0.8345,
            'current_spread': 0.00020,
            'projection': {
                'current_pl_usd': 24.60,
                'tp_pl_usd': 48.00,
                'sl_pl_usd': -30.00,
                'est_time_to_tp': '~18 min',
                'momentum': 'steady',
                'rr_live': '1.6:1',
                'pip_value_usd': 2.0,
                'units': 10000,
            },
        }
        state = _get_user_team_state(user_info['user_id'])
        state.setdefault('guardian_threats', {})[f'TEST_{pair}'] = fake_threat
        return jsonify({"success": True, "injected": fake_threat})

    @app.route("/api/trading/guardian/test-clear", methods=["POST", "OPTIONS"])
    def api_guardian_test_clear():
        """Clear all test threats."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        state = _get_user_team_state(user_info['user_id'])
        state['guardian_threats'] = {}
        return jsonify({"success": True})

    # ── Flight Recorder API ──

    @app.route("/api/trading/flight/summary", methods=["GET", "OPTIONS"])
    def api_flight_summary():
        """Flight recorder health summary."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        if not flight:
            return jsonify({"error": "Flight recorder not available"})
        return jsonify(flight.summary(user_id=user_info["user_id"]))

    @app.route("/api/trading/flight/cycles/<pair>", methods=["GET", "OPTIONS"])
    def api_flight_cycles(pair):
        """Recent cycles for a pair with flow audit."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        if not flight:
            return jsonify({"error": "Flight recorder not available"})
        limit = request.args.get("limit", 4, type=int)
        return jsonify(flight.get_cycles(pair, limit=limit, user_id=user_info["user_id"]))

    @app.route("/api/trading/flight/check/<cycle_id>", methods=["GET", "OPTIONS"])
    def api_flight_check(cycle_id):
        """Full flow audit for a specific cycle."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        if not flight:
            return jsonify({"error": "Flight recorder not available"})
        return jsonify(flight.check_flow(cycle_id, user_id=user_info["user_id"]))

    @app.route("/api/trading/flight/issues", methods=["GET", "OPTIONS"])
    def api_flight_issues():
        """Recent warnings, errors, and data gaps."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        if not flight:
            return jsonify({"error": "Flight recorder not available"})
        limit = request.args.get("limit", 20, type=int)
        return jsonify(flight.get_latest_issues(limit=limit, user_id=user_info["user_id"]))

    @app.route("/api/trading/flight/timings", methods=["GET", "OPTIONS"])
    def api_flight_timings():
        """Stage timing analysis (bottleneck detection)."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        if not flight:
            return jsonify({"error": "Flight recorder not available"})
        pair = request.args.get("pair")
        return jsonify(flight.get_stage_timings(pair=pair, user_id=user_info["user_id"]))

    @app.route("/api/trading/agent-comms", methods=["GET", "OPTIONS"])
    def api_agent_comms():
        """Fetch recent agent communications for a pair (from boardroom DB)."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        pair = request.args.get("pair")
        limit = min(int(request.args.get("limit", 50)), 200)
        minutes = int(request.args.get("minutes", 30))  # how far back to look
        try:
            import sqlite3 as _sql3
            _bdb = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'Database', 'v2', 'conversations.db'))
            conn = _sql3.connect(_bdb, isolation_level=None)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = _sql3.Row
            query = """SELECT timestamp, from_agent_id as 'from', to_agent_id as 'to',
                              'agent_message' as message_type, message as content
                       FROM agent_communications
                       WHERE conversation_id LIKE '%trading%'
                         AND timestamp > datetime('now', ?)
                       ORDER BY timestamp DESC LIMIT ?"""
            params = [f'-{minutes} minutes', limit]
            rows = conn.execute(query, params).fetchall()
            conn.close()
            
            comms = []
            for r in rows:
                d = dict(r)
                # Detect pair from content
                content = d.get('content', '')
                comm_pair = None
                for p in ['EUR_USD','GBP_USD','USD_JPY','AUD_JPY','EUR_AUD','GBP_JPY',
                           'USD_CHF','NZD_USD','EUR_GBP','EUR_JPY','AUD_USD','USD_CAD','EUR_CHF']:
                    if p in content:
                        comm_pair = p
                        break
                if pair and comm_pair and comm_pair != pair:
                    continue  # filter to requested pair
                d['pair'] = comm_pair
                # Suppress internal DB query noise from UI
                content = d.get('content', '')
                if content.startswith('[DB QUERY]'):
                    continue
                comms.append(d)
            
            comms.reverse()  # oldest first
            return jsonify({"comms": comms, "count": len(comms)})
        except Exception as e:
            logger.error("Failed to fetch agent comms: %s", e)
            return jsonify({"comms": [], "error": str(e)})

    # ── Manual Trade — user places BUY/SELL directly from chart card ──
    # ------------------------------------------------------------------

    @app.route("/api/trading/manual-trade", methods=["POST", "OPTIONS"])
    def api_trading_manual_trade():
        """Place a manual BUY/SELL trade with full market snapshot capture."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        data = request.get_json(silent=True) or {}
        pair = data.get("pair")
        direction = data.get("direction")  # 'buy' or 'sell'

        if not pair or direction not in ('buy', 'sell'):
            return jsonify({"error": "pair and direction ('buy'/'sell') required"}), 400

        try:
            # Get broker connection
            bc = _get_broker_credentials()
            conn_info = bc.get_connection(user_info["user_id"], "oanda")
            if not conn_info.get("configured"):
                return jsonify({"error": "OANDA not connected"}), 400

            api_key = conn_info["api_key"]
            account_id = conn_info["account_id"]
            base_url = conn_info["base_url"]

            import requests as http_requests
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            # ── Step 1: Fetch candles + compute market state ──
            candle_resp = http_requests.get(
                f"{base_url}/v3/instruments/{pair}/candles",
                headers=headers,
                params={"granularity": "M15", "count": "800", "price": "M"},
                timeout=15,
            )
            if candle_resp.status_code != 200:
                return jsonify({"error": f"Failed to fetch candles: {candle_resp.text}"}), 500

            candles_raw = candle_resp.json().get("candles", [])
            if len(candles_raw) < 200:
                return jsonify({"error": f"Insufficient candle data ({len(candles_raw)})"}), 400

            # Build candle dicts
            candle_dicts = []
            for c in candles_raw:
                mid = c.get("mid", {})
                candle_dicts.append({
                    "time": c.get("time", ""),
                    "open": float(mid.get("o", 0)),
                    "high": float(mid.get("h", 0)),
                    "low": float(mid.get("l", 0)),
                    "close": float(mid.get("c", 0)),
                    "volume": int(c.get("volume", 0)),
                })

            # Market picture
            from backtester.ema_separation import generate_market_picture
            market_picture = generate_market_picture(pair, candle_dicts)

            # Market story
            from market_story import read_market_story
            market_story = read_market_story(pair, candle_dicts, market_picture)

            # Sniper scores
            import pandas as pd
            from backtester.indicators import compute_all
            from backtester.sniper_v4 import add_enhanced_indicators, score_v4
            rows = []
            for c in candle_dicts:
                rows.append({
                    "open": c["open"], "high": c["high"],
                    "low": c["low"], "close": c["close"],
                    "volume": c.get("volume", 0),
                })
            df = pd.DataFrame(rows)
            df = compute_all(df)
            df = add_enhanced_indicators(df)
            latest = df.iloc[-1]
            from backtester.sniper_v4 import TF_PARAMS
            tf_params = TF_PARAMS.get("M15", TF_PARAMS["H1"])
            bull_score, bear_score = score_v4(latest, tf_params)
            sniper_scores = {
                "buy": round(bull_score, 1),
                "sell": round(bear_score, 1),
                "max": round(max(bull_score, bear_score), 1),
                "sniper_direction": "buy" if bull_score > bear_score else "sell",
            }

            # Candle structure
            from backtester.candle_structure import analyze_candle_structure
            from backtester.ema_separation import calculate_ema
            closes = [c["close"] for c in candle_dicts]
            ema21 = calculate_ema(closes, 21)
            ema55 = calculate_ema(closes, 55)
            ema100 = calculate_ema(closes, 100)
            candle_struct = analyze_candle_structure(candle_dicts, ema21, ema55, ema100)

            # ATR for SL/TP
            from backtester.indicators import atr as compute_atr_series
            atr_df = pd.DataFrame(candle_dicts)
            atr_series = compute_atr_series(atr_df, 14)
            atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0

            # ── Step 2: Compute SL/TP ──
            # 2026-04-01: Manual trades get 3.0× ATR SL (was 2.5×).
            # Trade #3695 EUR_AUD had 35.9p SL (2.5×ATR) hit by 38.2p retrace;
            # at 3.0× the SL would have been 40.9p — clearing the retrace.
            # Analysis of 72hrs: ALL 10 manual losses would have survived at 3.0×.
            # Snipe/auto trades keep their own multipliers (set elsewhere).
            sl_mult = data.get("stop_loss_atr", 3.0)
            tp_mult = data.get("take_profit_atr", 2.0)
            current_price = candle_dicts[-1]["close"]
            _price_decimals = 3 if 'JPY' in pair else 5

            if direction == "buy":
                sl_price = round(current_price - atr_val * sl_mult, _price_decimals)
                tp_price = round(current_price + atr_val * tp_mult, _price_decimals)
            else:
                sl_price = round(current_price + atr_val * sl_mult, _price_decimals)
                tp_price = round(current_price - atr_val * tp_mult, _price_decimals)

            # ── Step 3: Determine units ──
            # Priority: explicit UI lot selection > position_sizing_mode from risk config > risk% auto-calc
            units = data.get("units")
            if not units or units == "auto":
                # Load risk config first — user's panel settings drive everything
                risk_cfg = {}
                try:
                    with open(_RISK_CONFIG_PATH) as f:
                        risk_cfg = json.load(f)
                except Exception:
                    pass

                sizing_mode = risk_cfg.get("position_sizing", {}).get("mode", risk_cfg.get("position_sizing_mode", "auto"))
                fixed_units  = risk_cfg.get("position_sizing", {}).get("fixed_units", risk_cfg.get("fixed_units"))

                # User DB overrides (trading_preferences in core.db) take priority
                try:
                    import sqlite3 as _psz_sq2
                    _psz_core2 = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "Database", "v2", "core.db")
                    _psz_uid2 = user_info["user_id"]
                    with _psz_sq2.connect(_psz_core2, timeout=5) as _psz_conn2:
                        _psz_rows2 = _psz_conn2.execute(
                            "SELECT pref_key, pref_value FROM trading_preferences WHERE user_id=? "
                            "AND pref_key IN ('risk_position_sizing_mode','risk_fixed_units')",
                            (_psz_uid2,)
                        ).fetchall()
                        for _pk2, _pv2 in _psz_rows2:
                            if _pk2 == 'risk_position_sizing_mode':
                                sizing_mode = _pv2
                            elif _pk2 == 'risk_fixed_units':
                                fixed_units = int(float(_pv2))
                    logger.info("Manual trade sizing: mode=%s fixed_units=%s (user DB applied)", sizing_mode, fixed_units)
                except Exception as _psz_err:
                    logger.debug("Manual trade: user DB sizing lookup failed: %s", _psz_err)

                if sizing_mode == "fixed" and fixed_units and str(fixed_units) != "auto":
                    # User explicitly set fixed lot size in the risk panel — honour it
                    try:
                        units = int(fixed_units)
                        logger.info("Manual trade: using fixed units %d from risk panel", units)
                    except (ValueError, TypeError):
                        units = 1000
                else:
                    # Risk-based sizing: risk% of account balance / (SL pips × pip value)
                    try:
                        acct_resp = http_requests.get(
                            f"{base_url}/v3/accounts/{account_id}/summary",
                            headers=headers, timeout=10,
                        )
                        balance = float(acct_resp.json()['account']['balance'])
                        risk_pct = risk_cfg.get('risk_limits', {}).get('max_risk_per_trade_pct', 2.0)
                        risk_amount = balance * risk_pct / 100
                        pip_size = 0.01 if 'JPY' in pair else 0.0001
                        sl_distance_pips = abs(current_price - sl_price) / pip_size
                        quote_ccy = pair.split('_')[1] if '_' in pair else ''
                        base_ccy  = pair.split('_')[0] if '_' in pair else ''
                        if quote_ccy == 'USD':
                            pip_value_per_unit = pip_size
                        elif base_ccy == 'USD':
                            pip_value_per_unit = pip_size / current_price if current_price > 0 else pip_size
                        else:
                            pip_value_per_unit = pip_size * 1.3  # conservative cross-pair estimate
                        if sl_distance_pips > 0 and pip_value_per_unit > 0:
                            units = int(risk_amount / (sl_distance_pips * pip_value_per_unit))
                            try:
                                margin_avail = float(acct_resp.json()['account'].get('marginAvailable', balance))
                                max_units_margin = int(margin_avail * 50 / current_price) if current_price > 0 else 100000
                                units = max(1, min(units, max_units_margin, 100000))
                            except Exception:
                                units = max(1, min(units, 100000))
                        else:
                            units = 1000
                    except Exception:
                        units = 1000
            else:
                # Explicit lot selected in UI — use it directly
                try:
                    units = int(units)
                except (ValueError, TypeError):
                    units = 1000

            # 2026-04-01: Reject nonsense auto-sized trades. When margin is nearly
            # exhausted, auto-sizing can calculate 1 unit (trade #3651 USD_CHF).
            # A 1-unit trade costs $0.0001/pip — user clearly didn't intend this.
            if units < 100:
                return jsonify({
                    "error": f"Insufficient margin for a meaningful trade — auto-sizing calculated only {units} units. "
                             f"Close existing positions or select a specific lot size."
                }), 400

            # Negative units for sell
            order_units = str(units) if direction == "buy" else str(-abs(units))

            # ── Step 3b: Spread check — refuse to trade in wide spreads ──
            try:
                _pricing_resp = http_requests.get(
                    f"{base_url}/v3/accounts/{account_id}/pricing",
                    headers=headers,
                    params={"instruments": pair},
                    timeout=10,
                )
                if _pricing_resp.status_code == 200:
                    _pr = _pricing_resp.json().get("prices", [{}])[0]
                    _bid = float(_pr.get("bids", [{}])[0].get("price", 0))
                    _ask = float(_pr.get("asks", [{}])[0].get("price", 0))
                    _spread = _ask - _bid
                    _pip_size = 0.01 if 'JPY' in pair else 0.0001
                    _spread_pips = _spread / _pip_size
                    # Normal spreads by pair type (approximate)
                    _normal_spreads = {
                        'EUR_USD': 1.2, 'GBP_USD': 1.5, 'USD_JPY': 1.3, 'USD_CHF': 1.5,
                        'USD_CAD': 1.8, 'AUD_USD': 1.5, 'NZD_USD': 2.0,
                        'EUR_GBP': 1.5, 'EUR_JPY': 2.0, 'GBP_JPY': 3.0,
                        'EUR_AUD': 2.5, 'EUR_CHF': 2.0, 'AUD_JPY': 2.0,
                    }
                    _normal = _normal_spreads.get(pair, 2.0)
                    _spread_ratio = _spread_pips / _normal if _normal > 0 else 1
                    if _spread_ratio > 3.0:
                        return jsonify({
                            "error": f"Spread too wide: {_spread_pips:.1f} pips ({_spread_ratio:.1f}× normal). "
                                     f"Wait for tighter spread — best between 8AM-4PM ET."
                        }), 400
                    # Also warn in response if spread is elevated
                    _spread_warning = f" (spread {_spread_pips:.1f} pips — {_spread_ratio:.1f}× normal)" if _spread_ratio > 2.0 else ""
                else:
                    _spread_warning = ""
            except Exception as _sp_err:
                logger.debug(f"Spread check failed: {_sp_err}")
                _spread_warning = ""

            # ── Step 4: Place the order ──
            order_body = {
                "order": {
                    "type": "MARKET",
                    "instrument": pair,
                    "units": order_units,
                    "timeInForce": "IOC",
                    "stopLossOnFill": {"price": str(sl_price)},
                    "takeProfitOnFill": {"price": str(tp_price)},
                }
            }

            order_resp = http_requests.post(
                f"{base_url}/v3/accounts/{account_id}/orders",
                headers=headers,
                json=order_body,
                timeout=15,
            )

            if order_resp.status_code not in (200, 201):
                _rej_reason = ""
                try:
                    _rej_data = order_resp.json()
                    _rej_txn = _rej_data.get("orderCancelTransaction", {})
                    _rej_reason = _rej_txn.get("reason", "")
                except Exception:
                    pass
                _err_msg = f"OANDA rejected: {_rej_reason}" if _rej_reason else f"OANDA order failed ({order_resp.status_code})"
                if "MARGIN" in _rej_reason.upper():
                    _err_msg = f"Insufficient margin for {abs(units)} units. Reduce lot size or close existing positions."
                return jsonify({"error": _err_msg}), 400

            order_data = order_resp.json()
            fill = order_data.get("orderFillTransaction", {})
            trade_id = (
                fill.get("tradeOpened", {}).get("tradeID") or
                (fill.get("tradesClosed", [{}])[0].get("tradeID") if fill.get("tradesClosed") else None) or
                (fill.get("tradesReduced", [{}])[0].get("tradeID") if fill.get("tradesReduced") else None) or
                fill.get("id") or
                order_data.get("relatedTransactionIDs", [None])[-1]
            )
            fill_price = float(fill.get("price", current_price))
            logger.info(f"Order response — trade_id={trade_id}, fill keys={list(fill.keys())}")

            if not fill:
                # Log full OANDA response so we can see what happened
                cancel_txn = order_data.get("orderCancelTransaction", {})
                cancel_reason = cancel_txn.get("reason", "unknown")
                logger.error(f"OANDA order not filled for {pair} {order_units} units — cancel reason: {cancel_reason} | full response: {order_data}")

            if not trade_id:
                cancel_txn = order_data.get("orderCancelTransaction", {})
                cancel_reason = cancel_txn.get("reason", "")
                err = f"OANDA order cancelled: {cancel_reason}" if cancel_reason else "Order may have filled but no trade ID returned"
                return jsonify({
                    "error": err,
                    "oanda_response": order_data
                }), 500

            # ── Step 5a: Classify setup at entry ──
            _entry_setup = 'unknown'
            _entry_setup_name = ''
            _entry_setup_confidence = 0
            _classified_setups = []
            try:
                from setup_classifier import classify_setups, get_best_setups
                _ind = {
                    'rsi': latest.get('rsi', latest.get('RSI', 50)),
                    'stoch_k': latest.get('stoch_k', 50), 'stoch_d': latest.get('stoch_d', 50),
                    'adx': latest.get('adx', latest.get('ADX', 25)),
                    'macd_value': latest.get('macd', latest.get('MACD', 0)),
                    'macd_signal': latest.get('macd_signal', latest.get('MACD_signal', 0)),
                    'macd_hist': latest.get('macd_hist', latest.get('MACD_hist', 0)),
                    'bb_upper': latest.get('bb_upper', 0), 'bb_lower': latest.get('bb_lower', 0),
                    'bb_mid': latest.get('bb_mid', 0), 'bb_width': latest.get('bb_width', 0),
                    'close': latest.get('close', 0),
                    'ema_21': latest.get('ema_21', 0), 'ema_55': latest.get('ema_55', 0),
                    'ema_100': latest.get('ema_100', 0), 'atr': latest.get('atr', 0),
                    'sma50': latest.get('sma50', latest.get('SMA_50', 0)),
                    'sma100': latest.get('sma100', latest.get('SMA_100', 0)),
                    'sar': latest.get('sar', latest.get('SAR', 0)),
                    'cci': latest.get('cci', latest.get('CCI', 0)),
                    'adx_slope': latest.get('adx_slope', 0),
                }
                _classified_setups = classify_setups(indicators=_ind, candle_patterns={}, chart_patterns=[])
                if _classified_setups:
                    _best = get_best_setups(_classified_setups, min_confidence=0.50, max_results=1)
                    if _best:
                        _entry_setup = _best[0]['setup']
                        _entry_setup_name = _best[0].get('name', '')
                        _entry_setup_confidence = _best[0].get('confidence', 0)
                        logger.info(f"🔍 Manual trade classified: {_entry_setup} ({_entry_setup_name}) conf={_entry_setup_confidence:.0%}")
            except Exception as _cse:
                logger.debug(f"Setup classification at entry: {_cse}")

            # ── Step 5b: Write to unified live_trades (single canonical table) ──
            # Computes derived fields (fan analysis, fingerprint) and writes everything
            # in one INSERT — rich JSON snapshots + structured indicators + decision linkage.
            try:
                import sqlite3 as _lt_sql
                from db_pool import get_trading_forex as _gtf_manual

                _ema_data = (market_picture or {}).get('ema', {})
                _bb_data = (market_picture or {}).get('bollinger', {})
                _session = _get_trading_session()
                _regime = 'strong_trend' if latest.get('adx', 25) >= 25 else 'ranging'
                _entry_type = 'manual'  # always 'manual' for manual trades — story entry_type goes to story_entry_type column

                # ── Derived fan/story fields (from ManualTradeStore logic) ──
                _emas = _ema_data.get('current_emas', {})
                _e21 = _emas.get('ema_21') or _emas.get('ema21', 0)
                _e55 = _emas.get('ema_55') or _emas.get('ema55', 0)
                _e100 = _emas.get('ema_100') or _emas.get('ema100', 0)
                _fan_state = _ema_data.get('fan_state', 'unknown')
                _fan_direction = _ema_data.get('fan_direction', 'unknown')
                _e100_role = _ema_data.get('ema100_role', 'unknown')
                _trend_health = _ema_data.get('trend_health', 0)

                if direction == 'buy':
                    _fan_ordered = 1 if (fill_price > _e21 > _e55 > _e100 and _e21 > 0) else 0
                else:
                    _fan_ordered = 1 if (fill_price < _e21 < _e55 < _e100 and _e21 > 0) else 0

                _fan_width_pct = abs(_e21 - _e100) / fill_price * 100 if fill_price > 0 and _e21 > 0 else 0
                _bb_expanding_val = 1 if _bb_data.get('bb_expanding', False) else 0

                _mom = (market_story or {}).get('layers', {}).get('momentum', {})
                _momentum_state = _mom.get('state', 'unknown')
                _mom_rsi = _mom.get('rsi', 50)
                _mom_stoch_k = _mom.get('stoch_k', 50)
                _story_score = (market_story or {}).get('opportunity_score', 0)
                _story_entry_type = (market_story or {}).get('entry_type', 'none')

                # Cascade/retracement fields (empty for manual trades without scout context)
                _dual_cross_cascade = 0
                _cascade_direction = None
                _retracement_type = None
                _bb_re_expanding = 0
                _tested_e55 = 0
                _tested_e100 = 0
                _entry_setup_type = 'manual'

                # ── Pattern fingerprint ──
                def _rsi_bucket(v):
                    if v < 30: return "oversold"
                    elif v < 45: return "low"
                    elif v < 55: return "neutral"
                    elif v < 70: return "high"
                    else: return "overbought"
                def _stoch_bucket(v):
                    if v < 20: return "oversold"
                    elif v < 40: return "low"
                    elif v < 60: return "mid"
                    elif v < 80: return "high"
                    else: return "overbought"
                _fp_parts = [
                    _fan_state or "unknown", _fan_direction or "unknown",
                    "ordered" if _fan_ordered else "unordered",
                    _e100_role or "unknown",
                    "bb_exp" if _bb_expanding_val else "bb_flat",
                    _momentum_state or "unknown",
                    _rsi_bucket(_mom_rsi or 50), _stoch_bucket(_mom_stoch_k or 50),
                    direction,
                    f"cascade_{_cascade_direction}" if _cascade_direction else "no_cascade",
                    f"retrace_{_retracement_type}" if _retracement_type else "no_retrace",
                ]
                _fingerprint = "|".join(_fp_parts)

                # ── JSON serializer ──
                def _json(obj):
                    if obj is None: return None
                    try: return json.dumps(obj, default=str)
                    except: return json.dumps({"error": "serialization_failed"})

                # Serialize indicators
                _ind_dict = None
                if latest is not None:
                    try:
                        _ind_dict = {}
                        _src = latest if isinstance(latest, dict) else (latest.to_dict() if hasattr(latest, 'to_dict') else {})
                        for k, v in _src.items():
                            try: _ind_dict[k] = float(v) if hasattr(v, '__float__') else str(v)
                            except: _ind_dict[k] = str(v)
                    except: _ind_dict = {"error": "conversion_failed"}

                # Look up the most recent trade_decision for this pair
                _decision_id = None
                _val_verdict = None
                _val_confidence = None
                try:
                    _lt_conn_pre = _gtf_manual()
                    _lt_conn_pre.row_factory = _lt_sql.Row
                    _recent_dec = _lt_conn_pre.execute("""
                        SELECT id, validator_verdict, validator_confidence
                        FROM trade_decisions
                        WHERE pair = ?
                        ORDER BY timestamp DESC LIMIT 1
                    """, (pair,)).fetchone()
                    if _recent_dec:
                        _decision_id = _recent_dec['id']
                        _val_verdict = _recent_dec['validator_verdict']
                        _val_confidence = _recent_dec['validator_confidence']
                except Exception as _dec_err:
                    logger.debug("Decision lookup for live_trades link: %s", _dec_err)

                _now_utc = datetime.now(timezone.utc).isoformat()
                _lt_conn = _gtf_manual()
                _lt_conn.execute("""
                    INSERT OR REPLACE INTO live_trades (
                        id, source, oanda_trade_id, pair, timeframe, setup, base_setup,
                        direction, entry_time, session, entry_price, sl_price, tp_price,
                        spread_at_entry, status, regime, user_id, units,
                        adx, adx_slope, rsi, macd_value, macd_signal, macd_hist,
                        stoch_k, stoch_d, cci, bb_upper, bb_mid, bb_lower, bb_width, bb_expanding,
                        sma50, sma100, atr, sar,
                        entry_candle_pattern, confidence, trigger_reason,
                        concurrent_setups, concurrent_directions,
                        decision_id, cycle_id, entry_type,
                        validator_verdict, validator_confidence,
                        market_picture, market_story, sniper_scores,
                        candle_structure, indicators,
                        fan_state, fan_direction, fan_ordered, e100_role, fan_width_pct,
                        momentum_state, trend_health, story_score, story_entry_type,
                        dual_cross_cascade, cascade_direction, retracement_type,
                        bb_re_expanding, tested_e55, tested_e100, entry_setup_type,
                        pattern_fingerprint
                    ) VALUES (
                        ?, 'manual', ?, ?, 'M15', ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, 'open', ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?, ?,
                        ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?
                    )
                """, (
                    str(trade_id), str(trade_id), pair, _entry_setup, _entry_setup,
                    direction, _now_utc, _session,
                    fill_price, sl_price, tp_price,
                    float(latest.get('spread', 0)),
                    _regime, user_info["user_id"], abs(units),
                    float(latest.get('adx', 0)), float(latest.get('adx_slope', 0)),
                    float(latest.get('rsi', 0)), float(latest.get('macd', 0)),
                    float(latest.get('macd_signal', 0)), float(latest.get('macd_hist', 0)),
                    float(latest.get('stoch_k', 0)), float(latest.get('stoch_d', 0)),
                    float(latest.get('cci', 0)),
                    float(latest.get('bb_upper', 0)), float(latest.get('bb_mid', 0)),
                    float(latest.get('bb_lower', 0)), float(latest.get('bb_width', 0)),
                    _bb_expanding_val,
                    float(latest.get('sma50', latest.get('SMA_50', 0))),
                    float(latest.get('sma100', latest.get('SMA_100', 0))),
                    float(latest.get('atr', 0)), float(latest.get('sar', 0)),
                    json.dumps(candle_struct.get('patterns', [])) if candle_struct else None,
                    _entry_setup_confidence,
                    f"manual_entry|sniper_buy={sniper_scores.get('buy',0)}|sniper_sell={sniper_scores.get('sell',0)}",
                    ','.join(s['setup'] for s in _classified_setups[:3]) if _classified_setups else None,
                    ','.join(s['direction'] for s in _classified_setups[:3]) if _classified_setups else None,
                    _decision_id, _decision_id, _entry_type,
                    _val_verdict, _val_confidence,
                    _json(market_picture), _json(market_story), _json(sniper_scores),
                    _json(candle_struct), _json(_ind_dict),
                    _fan_state, _fan_direction, _fan_ordered, _e100_role, round(_fan_width_pct, 4),
                    _momentum_state, round(_trend_health, 1), round(_story_score, 1), _story_entry_type,
                    _dual_cross_cascade, _cascade_direction, _retracement_type,
                    _bb_re_expanding, _tested_e55, _tested_e100, _entry_setup_type,
                    _fingerprint,
                ))
                _lt_conn.commit()
                logger.info(
                    f"📊 Manual trade {trade_id} recorded in live_trades: {_entry_setup} "
                    f"(decision={_decision_id}, verdict={_val_verdict}, fingerprint={_fingerprint})"
                )
            except Exception as _lt_err:
                logger.warning(f"live_trades INSERT failed: {_lt_err}", exc_info=True)

            # ── Step 5d: Link manual trade to active watch on this pair ──
            # When user opens a manual trade on a pair with a watching/triggered snipe,
            # mark the most relevant watch as triggered + link the trade so the snipe
            # card shows the live P&L.
            try:
                from db_pool import get_trading_forex as _gtf_manual_link
                _ml_conn = _gtf_manual_link()
                _ml_conn.row_factory = sqlite3.Row
                _ml_watch = _ml_conn.execute("""
                    SELECT id, status FROM watch_suggestions
                    WHERE instrument = ? AND status IN ('triggered', 'watching')
                    ORDER BY
                        CASE status WHEN 'triggered' THEN 0 ELSE 1 END,
                        created_at DESC
                    LIMIT 1
                """, (pair,)).fetchone()
                if _ml_watch:
                    _ml_conn.execute(
                        "UPDATE watch_suggestions SET status='triggered', triggered_at=?, trade_cycle_id=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), f"manual_{trade_id}", _ml_watch["id"])
                    )
                    _ml_conn.commit()
                    logger.info("🔗 Manual trade %s linked to watch #%s on %s", trade_id, _ml_watch["id"], pair)
            except Exception as _ml_err:
                logger.debug("Manual trade watch linking failed: %s", _ml_err)

            # ── Step 6: Register thesis + trigger guardian ──
            # Manual trades carry full market context — register it as thesis so the
            # guardian gets the same behavioural guards as team-placed trades.
            # This enables: _is_retracement_entry, _is_snipe_direct, retrace suppression,
            # score_threat is_manual grace, and all other context-dependent rules.
            if _guardian_instance and _guardian_loop:
                try:
                    import asyncio
                    _ema_at_entry = market_picture.get('ema', {}) if market_picture else {}
                    _bb_at_entry  = market_picture.get('bollinger', {}) if market_picture else {}
                    _story_entry_type = (market_story or {}).get('entry_type', 'unknown')
                    _story_direction  = (market_story or {}).get('direction', direction)
                    _fan_state        = _ema_at_entry.get('fan_state', 'unknown')
                    _fan_direction    = _ema_at_entry.get('fan_direction', 'neutral')

                    # Build thesis matching the same schema as trading_cycle register_thesis calls
                    _manual_thesis = {
                        'entry_type':       _story_entry_type,
                        'direction':        _story_direction,
                        'fan_state_at_entry': _fan_state,
                        'fan_direction_at_entry': _fan_direction,
                        'bb_expanding_at_entry': _bb_at_entry.get('bb_expanding', False),
                        'trend_health_at_entry': _ema_at_entry.get('trend_health', 50),
                        'opportunity_score': (market_story or {}).get('opportunity_score', 0),
                        'thesis': (market_story or {}).get('story_narrative', '')[:400],
                        'is_manual': True,   # flags score_threat grace period
                        'source': 'manual',
                    }
                    # Register thesis BEFORE reconcile so watcher spawns with full context
                    _guardian_instance.register_thesis(pair, _manual_thesis)
                    logger.info(f"Manual trade thesis registered for {pair}: entry_type={_story_entry_type} fan={_fan_state}/{_fan_direction}")

                    # Trigger immediate reconcile (correct method is _reconcile, not reconcile_positions)
                    asyncio.run_coroutine_threadsafe(
                        _guardian_instance._reconcile(),
                        _guardian_loop
                    )
                except Exception as ge:
                    logger.warning(f"Guardian thesis/reconcile after manual trade: {ge}")

            # ── Build response with market conditions ──
            ema_data = market_picture.get("ema", {})
            emas = ema_data.get("current_emas", {})

            return jsonify({
                "success": True,
                "trade_id": str(trade_id),
                "fill_price": fill_price,
                "direction": direction,
                "units": abs(units),
                "pair": pair,
                "sl": sl_price,
                "tp": tp_price,
                "atr": round(atr_val, 6),
                "market_conditions": {
                    "fan_state": ema_data.get("fan_state"),
                    "fan_direction": ema_data.get("fan_direction"),
                    "fan_ordered": ema_data.get("fan_ordered", False),
                    "e100_role": ema_data.get("ema100_role"),
                    "trend_health": ema_data.get("trend_health"),
                    "bb_expanding": market_picture.get("bollinger", {}).get("bb_expanding"),
                    "story_score": market_story.get("opportunity_score"),
                    "story_entry_type": market_story.get("entry_type"),
                    "thesis_direction": market_story.get("direction"),
                    "sniper": sniper_scores,
                    "momentum": market_story.get("layers", {}).get("momentum", {}).get("state"),
                    "rsi": market_story.get("layers", {}).get("momentum", {}).get("rsi"),
                },
                "manual_trade_row_id": str(trade_id),
            })

        except Exception as e:
            logger.error(f"Manual trade failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    # GET /api/trading/manual-trade-preview — get market conditions without placing
    @app.route("/api/trading/manual-trade-preview", methods=["GET", "OPTIONS"])
    def api_trading_manual_trade_preview():
        """Get current market conditions for a pair (for the confirmation popup)."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()

        user_info, err = _get_authenticated_user()
        if err:
            return err

        pair = request.args.get("pair")
        direction = request.args.get("direction", "buy")
        if not pair:
            return jsonify({"error": "pair required"}), 400

        try:
            bc = _get_broker_credentials()
            conn_info = bc.get_connection(user_info["user_id"], "oanda")
            if not conn_info.get("configured"):
                return jsonify({"error": "OANDA not connected"}), 400

            api_key = conn_info["api_key"]
            account_id = conn_info["account_id"]
            base_url = conn_info["base_url"]

            import requests as http_requests
            headers = {"Authorization": f"Bearer {api_key}"}
            
            # Get account balance
            balance = 1905.0  # fallback
            try:
                acct_resp = http_requests.get(
                    f"{base_url}/v3/accounts/{account_id}/summary",
                    headers=headers, timeout=10,
                )
                if acct_resp.status_code == 200:
                    balance = float(acct_resp.json()['account']['balance'])
            except Exception:
                pass  # use fallback balance
            
            candle_resp = http_requests.get(
                f"{base_url}/v3/instruments/{pair}/candles",
                headers=headers,
                params={"granularity": "M15", "count": "800", "price": "M"},
                timeout=15,
            )
            candles_raw = candle_resp.json().get("candles", [])
            candle_dicts = []
            for c in candles_raw:
                mid = c.get("mid", {})
                candle_dicts.append({
                    "time": c.get("time", ""),
                    "open": float(mid.get("o", 0)),
                    "high": float(mid.get("h", 0)),
                    "low": float(mid.get("l", 0)),
                    "close": float(mid.get("c", 0)),
                    "volume": int(c.get("volume", 0)),
                })

            from backtester.ema_separation import generate_market_picture
            mkt = generate_market_picture(pair, candle_dicts)

            from market_story import read_market_story
            story = read_market_story(pair, candle_dicts, mkt)

            import pandas as pd
            from backtester.indicators import compute_all
            from backtester.sniper_v4 import add_enhanced_indicators, score_v4, TF_PARAMS
            rows = [{"open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "volume": c.get("volume", 0)} for c in candle_dicts]
            df = pd.DataFrame(rows)
            df = compute_all(df)
            df = add_enhanced_indicators(df)
            latest = df.iloc[-1]
            tf_params = TF_PARAMS.get("M15", TF_PARAMS["H1"])
            bull, bear = score_v4(latest, tf_params)

            from backtester.indicators import atr as compute_atr_series
            atr_df = pd.DataFrame(candle_dicts)
            atr_s = compute_atr_series(atr_df, 14)
            atr_val = float(atr_s.iloc[-1]) if not pd.isna(atr_s.iloc[-1]) else 0

            ema = mkt.get("ema", {})
            emas = ema.get("current_emas", {})
            e21 = emas.get("ema_21") or emas.get("ema21", 0)
            e55 = emas.get("ema_55") or emas.get("ema55", 0)
            e100 = emas.get("ema_100") or emas.get("ema100", 0)
            price = candle_dicts[-1]["close"]

            if direction == "buy":
                fan_ordered = price > e21 > e55 > e100 if e21 > 0 else False
            else:
                fan_ordered = price < e21 < e55 < e100 if e21 > 0 else False

            fan_width = abs(e21 - e100) / price * 100 if price > 0 and e21 > 0 else 0

            # Thesis/sniper agreement
            thesis_dir = story.get("direction", "none")
            sniper_dir = "buy" if bull > bear else "sell"
            thesis_agrees = thesis_dir == direction
            sniper_agrees = sniper_dir == direction

            # 2026-04-01: Manual preview matches execution — 3.0× ATR SL
            sl_mult = 3.0
            tp_mult = 2.0
            _pd = 3 if 'JPY' in pair else 5
            if direction == "buy":
                sl = round(price - atr_val * sl_mult, _pd)
                tp = round(price + atr_val * tp_mult, _pd)
            else:
                sl = round(price + atr_val * sl_mult, _pd)
                tp = round(price - atr_val * tp_mult, _pd)

            # Load user's saved lot-size preference so the chart card can default to it
            _user_fixed_units = 10000  # fallback
            _user_sizing_mode = "fixed"
            try:
                import sqlite3 as _psz_sq3
                _psz_core3 = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "Database", "v2", "core.db")
                with _psz_sq3.connect(_psz_core3, timeout=5) as _psz_c3:
                    _psz_r3 = _psz_c3.execute(
                        "SELECT pref_key, pref_value FROM trading_preferences WHERE user_id=? "
                        "AND pref_key IN ('risk_position_sizing_mode','risk_fixed_units')",
                        (user_info["user_id"],)
                    ).fetchall()
                    for _pk3, _pv3 in _psz_r3:
                        if _pk3 == 'risk_position_sizing_mode':
                            _user_sizing_mode = _pv3
                        elif _pk3 == 'risk_fixed_units':
                            _user_fixed_units = int(float(_pv3))
            except Exception:
                pass

            preview_data = {
                "pair": pair,
                "direction": direction,
                "price": price,
                "atr": round(atr_val, 6),
                "sl": sl,
                "tp": tp,
                "fan_state": ema.get("fan_state"),
                "fan_direction": ema.get("fan_direction"),
                "fan_ordered": fan_ordered,
                "fan_width_pct": round(fan_width, 3),
                "e100_role": ema.get("ema100_role"),
                "trend_health": ema.get("trend_health"),
                "bb_expanding": mkt.get("bollinger", {}).get("bb_expanding"),
                "story_score": story.get("opportunity_score"),
                "story_entry_type": story.get("entry_type"),
                "thesis_direction": thesis_dir,
                "thesis_agrees": thesis_agrees,
                "sniper_buy": round(bull, 1),
                "sniper_sell": round(bear, 1),
                "sniper_direction": sniper_dir,
                "sniper_agrees": sniper_agrees,
                "momentum_state": story.get("layers", {}).get("momentum", {}).get("state"),
                "rsi": story.get("layers", {}).get("momentum", {}).get("rsi"),
                "stoch_k": story.get("layers", {}).get("momentum", {}).get("stoch_k"),
                "balance": balance,
                "pip_size": 0.01 if 'JPY' in pair else 0.0001,
                "user_fixed_units": _user_fixed_units,
                "user_sizing_mode": _user_sizing_mode,
            }

            # Add live spread info to preview
            try:
                _pr_resp = http_requests.get(
                    f"{base_url}/v3/accounts/{account_id}/pricing",
                    headers=headers, params={"instruments": pair}, timeout=10,
                )
                if _pr_resp.status_code == 200:
                    _pr2 = _pr_resp.json().get("prices", [{}])[0]
                    _bid2 = float(_pr2.get("bids", [{}])[0].get("price", 0))
                    _ask2 = float(_pr2.get("asks", [{}])[0].get("price", 0))
                    _sp2 = _ask2 - _bid2
                    _pip2 = 0.01 if 'JPY' in pair else 0.0001
                    _sp_pips2 = _sp2 / _pip2
                    _normal_sp = {'EUR_USD':1.2,'GBP_USD':1.5,'USD_JPY':1.3,'USD_CHF':1.5,
                                  'USD_CAD':1.8,'AUD_USD':1.5,'NZD_USD':2.0,'EUR_GBP':1.5,
                                  'EUR_JPY':2.0,'GBP_JPY':3.0,'EUR_AUD':2.5,'EUR_CHF':2.0,'AUD_JPY':2.0}
                    _norm2 = _normal_sp.get(pair, 2.0)
                    _ratio2 = _sp_pips2 / _norm2 if _norm2 > 0 else 1
                    preview_data["spread_pips"] = round(_sp_pips2, 1)
                    preview_data["spread_ratio"] = round(_ratio2, 1)
                    preview_data["spread_warning"] = _ratio2 > 2.0
            except Exception:
                pass

            return jsonify(preview_data)

        except Exception as e:
            logger.error(f"Manual trade preview failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    # GET /api/trading/manual-trade-stats
    @app.route("/api/trading/manual-trade-stats", methods=["GET", "OPTIONS"])
    def api_trading_manual_trade_stats():
        """Get manual trade statistics and pattern analysis."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        pair = request.args.get("pair")
        store = _get_manual_store()
        return jsonify({
            "stats": store.get_stats(user_info["user_id"], pair),
            "patterns": store.analyze_patterns(pair),
        })

    # GET /api/trading/training-stats — MLX training pipeline status
    @app.route("/api/trading/training-stats", methods=["GET", "OPTIONS"])
    def api_training_stats():
        """Get MLX LoRA training pipeline statistics."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err

        try:
            from training_collector import get_training_stats
            from lora_trainer import check_training_status, should_train

            # Get training data stats
            stats = get_training_stats()

            # Add training status for each model
            stats['ta_9b']['training_status'] = check_training_status('ta_9b')
            stats['ta_9b']['should_train'] = should_train('ta_9b')

            stats['trade_monitor_35b']['training_status'] = check_training_status('trade_monitor_35b')
            stats['trade_monitor_35b']['should_train'] = should_train('trade_monitor_35b')

            return jsonify({
                "success": True,
                "stats": stats
            })

        except Exception as e:
            logger.error("Training stats error: %s", e)
            return jsonify({
                "success": False,
                "error": str(e)
            }), 500

    # POST /api/trading/annotated-submission — Save user-annotated chart as training data
    @app.route("/api/trading/annotated-submission", methods=["POST", "OPTIONS"])
    def api_annotated_submission():
        """Save an annotated chart screenshot as training data."""
        if request.method == "OPTIONS":
            return _cors_preflight()
        try:
            user_info, auth_err = _get_authenticated_user()
            if auth_err:
                return auth_err
            user_id = user_info["user_id"]
            data    = request.get_json(force=True) or {}
            pair          = data.get("pair", "UNKNOWN")
            timeframe     = data.get("timeframe", "M15")
            thesis        = data.get("thesis", "")
            notes         = data.get("notes", "")
            drawing_count = int(data.get("drawing_count", 0))
            image_b64     = data.get("image_b64", "")

            if not image_b64:
                return jsonify({"ok": False, "error": "No image provided"}), 400

            import base64 as _b64
            import os as _os
            from datetime import datetime as _dt

            save_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                     "..", "Data", "charts", "user_annotations")
            save_dir = _os.path.normpath(save_dir)
            _os.makedirs(save_dir, exist_ok=True)

            ts_str  = _dt.now().strftime("%Y-%m-%d_%H-%M-%S")
            img_file = f"{ts_str}_{pair}.png"
            img_path = _os.path.join(save_dir, img_file)

            with open(img_path, "wb") as f:
                f.write(_b64.b64decode(image_b64))

            # Append to manifest
            manifest_path = _os.path.join(save_dir, "manifest.json")
            import json as _json
            manifest = []
            if _os.path.exists(manifest_path):
                try:
                    with open(manifest_path) as mf:
                        manifest = _json.load(mf)
                except Exception:
                    manifest = []

            entry = {
                "id":            len(manifest) + 1,
                "user_id":       user_id,
                "pair":          pair,
                "timeframe":     timeframe,
                "timestamp":     _dt.now().isoformat(),
                "thesis":        thesis,
                "notes":         notes,
                "drawing_count": drawing_count,
                "image_file":    img_file,
                "trade_id":      None,
            }
            manifest.append(entry)
            with open(manifest_path, "w") as mf:
                _json.dump(manifest, mf, indent=2)

            logger.info("[annotated-submission] saved %s (#%d drawings, thesis=%s)",
                        img_file, drawing_count, bool(thesis))
            return jsonify({"ok": True, "id": entry["id"], "saved_to": img_file}), 200

        except Exception as e:
            logger.error("annotated-submission error: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 500

    # POST /api/trading/command — User talks to the orchestrator
    @app.route("/api/trading/command", methods=["POST", "OPTIONS"])
    def api_trading_command():
        """Send a user command/message to the cycle orchestrator via the live swarm."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err

        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        user_image = (data.get("user_image") or "").strip() or None   # base64 PNG from annotation canvas
        if not message and not user_image:
            return jsonify({"error": "message is required"}), 400
        if not message:
            message = "(see annotated chart)"

        try:
            user_state = _get_user_team_state(user_info["user_id"])
            team = user_state.get("team_setup")
            uid  = user_info["user_id"]
            _ts  = datetime.utcnow().isoformat() + "Z"

            # Fast-path simple commands — no LLM needed
            cmd_lower = message.strip().lower()
            if cmd_lower in ("pause", "pause_trading"):
                from Source.agents import trading_cycle as _tc
                _tc._trading_paused = True
                floor_messages = [{"agent": "cycle_orchestrator", "text": "⏸ Trading paused.", "timestamp": _ts}]
            elif cmd_lower in ("resume", "resume_trading"):
                from Source.agents import trading_cycle as _tc
                _tc._trading_paused = False
                floor_messages = [{"agent": "cycle_orchestrator", "text": "▶️ Trading resumed.", "timestamp": _ts}]
            else:
                # ── Floor chat: multi-agent dispatch ──
                # Detect pair from message for annotation + last cycle context loading
                try:
                    from Source.chat_intent_parser import _extract_pair
                except ImportError:
                    from chat_intent_parser import _extract_pair

                # Pair resolution: explicit (from frontend) > NLP extract from message > history
                _req_pair = (data.get("pair") or "").strip().upper().replace("/", "_")
                _msg_pair = _req_pair or _extract_pair(message)
                if not _msg_pair:
                    for _prev in reversed(user_state.get("command_history", [])[-10:]):
                        _prev_pair = _extract_pair(_prev.get("message", ""))
                        if _prev_pair:
                            _msg_pair = _prev_pair
                            break

                # Build chat history from command log for floor_chat context
                _chat_hist = []
                for _prev in user_state.get("command_history", [])[-8:]:
                    if _prev.get("message"):
                        _chat_hist.append({"agent": "tim", "text": _prev["message"]})
                    if _prev.get("response"):
                        _chat_hist.append({"agent": "cycle_orchestrator", "text": _prev["response"][:200]})

                # Check if this is an explicit action (SET_WATCH, CLOSE_TRADE) — use legacy handler
                try:
                    from Source.chat_intent_parser import parse_intent
                except ImportError:
                    from chat_intent_parser import parse_intent

                _intent = parse_intent(message)
                _explicit_actions = ("SET_WATCH", "CLOSE_TRADE", "ANNOTATE_TRADE")

                _chart_ann_id = data.get("chart_annotation_id") or None
                if _intent.type in _explicit_actions:
                    # Legacy handler handles these with DB writes, watch creation etc.
                    _legacy_text = _direct_orchestrator_chat(message=message, user_id=uid, team=team,
                                                              pair_hint=_msg_pair or "",
                                                              chart_annotation_id=_chart_ann_id)
                    floor_messages = [{"agent": "cycle_orchestrator", "text": _legacy_text, "timestamp": _ts}]
                else:
                    # Ensure team is registered before floor chat calls _agent_task
                    team, _init_err = _ensure_team_initialized(uid)
                    if _init_err:
                        logger.warning("[floor_chat] team init: %s", _init_err)

                    # Floor chat — multi-agent dispatch
                    try:
                        from Source.floor_chat import handle_floor_message
                    except ImportError:
                        from floor_chat import handle_floor_message

                    floor_messages = handle_floor_message(
                        message=message,
                        user_id=uid,
                        pair=_msg_pair,
                        chat_history=_chat_hist,
                        user_image=user_image,
                    )

            # Log to command history
            _response_text = " | ".join(m["text"] for m in floor_messages)
            cmd_entry = {
                "type": "user_command",
                "message": message,
                "response": _response_text[:500],
                "timestamp": _ts,
            }

            # ── Persist validator response back to annotation manifest ────────
            # When the user submitted an annotated chart image, save the full
            # floor chat response alongside it so we have image+thesis+response
            # as a complete vision training pair.
            if user_image and _msg_pair:
                try:
                    import os as _ann_os, json as _ann_json
                    _ann_dir = _ann_os.path.normpath(_ann_os.path.join(
                        _ann_os.path.dirname(_ann_os.path.abspath(__file__)),
                        "..", "Data", "charts", "user_annotations"))
                    _ann_manifest = _ann_os.path.join(_ann_dir, "manifest.json")
                    if _ann_os.path.exists(_ann_manifest):
                        with open(_ann_manifest) as _mf:
                            _manifest = _ann_json.load(_mf)
                        # Match most recent entry for this pair without a validator_response
                        for _entry in reversed(_manifest):
                            if (_entry.get("pair") == _msg_pair and
                                    not _entry.get("validator_response")):
                                _entry["validator_response"] = _response_text
                                _entry["validator_agents"] = [m.get("agent","?") for m in floor_messages]
                                break
                        with open(_ann_manifest, "w") as _mf:
                            _ann_json.dump(_manifest, _mf, indent=2)
                except Exception as _ann_err:
                    logger.debug("[floor_chat] manifest writeback failed: %s", _ann_err)
            user_state.setdefault("command_history", []).append(cmd_entry)
            if len(user_state["command_history"]) > 50:
                user_state["command_history"] = user_state["command_history"][-50:]

            return jsonify({
                "ok": True,
                "response": floor_messages[0]["text"] if floor_messages else "",
                "messages": floor_messages,
                "timestamp": _ts,
            })
        except Exception as e:
            logger.error(f"Orchestrator command failed: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500


    @app.route("/api/trading/scout-intelligence", methods=["GET", "OPTIONS"])
    def scout_intelligence():
        """Serve scout retrospective data + live today data for the Team Intelligence panel."""
        if request.method == "OPTIONS":
            return _cors("")
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            import glob, re as _re, sqlite3 as _sqlite3
            from datetime import datetime, timezone

            RETRO_DIR = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                '../../knowledge/collective/scout-retrospective'
            )
            PATTERNS_DIR = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                '../../knowledge/collective/patterns'
            )
            TREVOR_DB = _TRADING_FOREX_DB
            _uid = user_info["user_id"]

            today_str = datetime.now().strftime('%Y-%m-%d')

            # ── Live today data from scout_alerts ─────────────────────────────
            today_live = None
            today_snipes = []
            _si_conn = None
            try:
                _si_conn = get_trading_forex()
                _si_conn.row_factory = _sqlite3.Row

                # Today's scout alert count (distinct pairs × decisions)
                row = _si_conn.execute("""
                    SELECT COUNT(*) as cnt,
                           COUNT(DISTINCT pair) as pairs
                    FROM scout_alerts
                    WHERE date(timestamp) = ?
                      AND (user_id = ? OR user_id IS NULL)
                """, (today_str, _uid)).fetchone()
                total_today = row['cnt'] if row else 0
                pairs_today = row['pairs'] if row else 0

                # Today's unique pairs alerted (with best reasoning for direction hint)
                pair_rows = _si_conn.execute("""
                    SELECT pair, MAX(score) as top_score,
                           COUNT(*) as scans, MAX(timestamp) as last_seen,
                           MAX(CASE WHEN score = (SELECT MAX(s2.score) FROM scout_alerts s2
                               WHERE s2.pair = scout_alerts.pair AND date(s2.timestamp) = ?
                               AND (s2.user_id = ? OR s2.user_id IS NULL))
                               THEN reasoning ELSE NULL END) as top_reasoning
                    FROM scout_alerts
                    WHERE date(timestamp) = ?
                      AND (user_id = ? OR user_id IS NULL)
                    GROUP BY pair
                    ORDER BY top_score DESC
                    LIMIT 12
                """, (today_str, _uid, today_str, _uid)).fetchall()

                import re as _re2
                def _extract_direction(reasoning):
                    if not reasoning:
                        return ''
                    r = reasoning.upper()
                    if 'SHORT' in r or 'BEARISH' in r or 'SELL' in r:
                        return 'SHORT'
                    if 'LONG' in r or 'BULLISH' in r or 'BUY' in r:
                        return 'LONG'
                    return ''

                today_alerts = [
                    {
                        "pair": r['pair'],
                        "direction": _extract_direction(r['top_reasoning']),
                        "top_score": r['top_score'],
                        "scans": r['scans'],
                        "last_seen": r['last_seen'][:16] if r['last_seen'] else '--',
                        "reasoning_hint": (r['top_reasoning'] or '')[:80],
                    }
                    for r in pair_rows
                ]

                today_live = {
                    "date": today_str,
                    "total_scout_scans": total_today,
                    "pairs_alerted": pairs_today,
                    "alerts": today_alerts,
                    "is_live": True,
                }
            except Exception as _e:
                logger.warning(f"scout-intelligence: live today query failed: {_e}")
            except Exception as _e:
                pass  # logged above

            # ── Active snipes from watch_suggestions (trading_forex) ────────────
            try:
                _snipe_conn = get_trading_forex()
                _snipe_conn.row_factory = _sqlite3.Row
                snipe_rows = _snipe_conn.execute("""
                    SELECT id, instrument, suggestion_type, status,
                           created_at, expires_at, conditions_met_count,
                           conditions_total_count, peak_progress, context,
                           triggered_at, trade_outcome, pips_result
                    FROM watch_suggestions
                    WHERE status IN ('watching', 'triggered')
                      AND (user_id = ? OR user_id IS NULL)
                    ORDER BY created_at DESC
                    LIMIT 20
                """, (_uid,)).fetchall()

                import json as _json
                for r in snipe_rows:
                    ctx = {}
                    try:
                        ctx = _json.loads(r['context']) if r['context'] else {}
                    except Exception:
                        pass
                    today_snipes.append({
                        "id": r['id'],
                        "pair": r['instrument'],
                        "type": r['suggestion_type'],
                        "status": r['status'],
                        "created_at": r['created_at'][:16] if r['created_at'] else '--',
                        "expires_at": r['expires_at'][:16] if r['expires_at'] else '--',
                        "conditions_met": r['conditions_met_count'] or 0,
                        "conditions_total": r['conditions_total_count'] or 0,
                        "peak_progress": round(r['peak_progress'] or 0, 1),
                        "triggered_at": r['triggered_at'][:16] if r['triggered_at'] else None,
                        "trade_outcome": r['trade_outcome'],
                        "pips_result": r['pips_result'],
                        "direction": ctx.get('direction', ''),
                        "validator_verdict": ctx.get('validator_verdict', ''),
                    })
            except Exception as _e:
                logger.warning(f"scout-intelligence: snipe query failed: {_e}")

            # ── Historical daily reports — DB-first with markdown fallback ────
            # The retrospective markdown files were historically broken (reading from
            # nonexistent text logs), so we now compute daily summaries directly from
            # signal_log in trade_log.db. Markdown files used as fallback/supplement.
            daily_reports = []
            import json as _json2
            try:
                from db_pool import get_trading_forex as _gtf_hist
                _tl_conn = _gtf_hist()
                _tl_conn.row_factory = sqlite3.Row
                # Get per-day decision counts and verdict breakdown for the last 14 days
                _day_rows = _tl_conn.execute("""
                    SELECT substr(timestamp, 1, 10) as day,
                           COUNT(*) as total,
                           action, decision_reasoning
                    FROM signal_log
                    WHERE timestamp >= datetime('now', '-14 days')
                      AND (user_id = ? OR user_id IS NULL)
                    ORDER BY timestamp
                """, (_uid,)).fetchall()

                # Aggregate by day
                from collections import defaultdict as _defaultdict
                _day_data = _defaultdict(lambda: {
                    'total': 0, 'watch': 0, 'reject': 0, 'skip': 0, 'confirm': 0
                })
                for row in _day_rows:
                    day = row['day']
                    action = row['action']
                    dr = row['decision_reasoning'] or '{}'
                    _day_data[day]['total'] += 1

                    verdict = 'SKIP'
                    if action in ('buy', 'sell'):
                        verdict = 'CONFIRM'
                    else:
                        try:
                            _reasons = _json2.loads(dr).get('reasons', [])
                            if _reasons:
                                _vm = _re.match(r'^([A-Z_]+):', _reasons[0])
                                if _vm:
                                    verdict = _vm.group(1)
                        except Exception:
                            pass

                    if verdict in ('CONFIRM', 'WATCH'):
                        _day_data[day]['watch'] += 1   # alert-type decisions
                    elif verdict in ('REJECT', 'SKIP', 'SL'):
                        _day_data[day]['reject'] += 1  # block-type decisions

                # Build daily reports from DB data
                for day in sorted(_day_data.keys(), reverse=True)[:10]:
                    dd = _day_data[day]
                    if dd['total'] == 0:
                        continue
                    # For accuracy: WATCH+CONFIRM = "scout alerted" (action taken),
                    # REJECT+SKIP = "scout blocked" (action suppressed).
                    # Without OANDA price outcome data, we show decision ratios.
                    total_dec = dd['total']
                    alerts = dd['watch']     # CONFIRM + WATCH
                    blocks = dd['reject']    # REJECT + SKIP

                    # Try to enrich from retrospective markdown if it has real scores
                    retro_path = os.path.join(RETRO_DIR, f'{day}.md')
                    retro_accuracy = None
                    if os.path.exists(retro_path):
                        try:
                            _content = open(retro_path).read()
                            _fm = {}
                            for _line in _content.split('\n'):
                                for _key in ['correct_blocks', 'correct_alerts',
                                             'missed_opportunities', 'false_alerts']:
                                    _m = _re.match(rf'^{_key}:\s*(.+)', _line)
                                    if _m:
                                        _val = _m.group(1).strip()
                                        _fm[_key] = int(_val) if _val.isdigit() else 0
                            _correct = _fm.get('correct_blocks', 0) + _fm.get('correct_alerts', 0)
                            if _correct > 0:  # Only use retro if it has real scored data
                                retro_accuracy = {
                                    'correct_blocks': _fm.get('correct_blocks', 0),
                                    'correct_alerts': _fm.get('correct_alerts', 0),
                                    'missed_opportunities': _fm.get('missed_opportunities', 0),
                                    'false_alerts': _fm.get('false_alerts', 0),
                                }
                        except Exception:
                            pass

                    if retro_accuracy:
                        # Use scored data from retrospective (has OANDA price verification)
                        _ca = retro_accuracy['correct_alerts']
                        _cb = retro_accuracy['correct_blocks']
                        _mi = retro_accuracy['missed_opportunities']
                        _fa = retro_accuracy['false_alerts']
                        _tot = _ca + _cb + _mi + _fa or 1
                        daily_reports.append({
                            'date': day,
                            'total_decisions': _tot,
                            'correct_alerts': _ca,
                            'correct_blocks': _cb,
                            'missed_opportunities': _mi,
                            'false_alerts': _fa,
                            'accuracy_pct': round((_ca + _cb) / _tot * 100),
                            'miss_rate_pct': round(_mi / _tot * 100),
                            'is_today': (day == today_str),
                            'source': 'retrospective',
                        })
                    else:
                        # No scored retrospective — show decision activity from signal_log
                        # Alert ratio = fraction of decisions that triggered alerts vs blocks
                        alert_ratio = round(alerts / total_dec * 100) if total_dec else 0
                        daily_reports.append({
                            'date': day,
                            'total_decisions': total_dec,
                            'correct_alerts': alerts,    # alert-type (WATCH/CONFIRM)
                            'correct_blocks': blocks,    # block-type (REJECT/SKIP)
                            'missed_opportunities': 0,   # can't compute without price data
                            'false_alerts': 0,
                            'accuracy_pct': alert_ratio,  # % of decisions that were alerts
                            'miss_rate_pct': 0,
                            'is_today': (day == today_str),
                            'source': 'signal_log',
                        })
            except Exception as _db_err:
                logger.warning(f"scout-intelligence: signal_log daily query failed: {_db_err}")
                # Fallback to markdown-only approach
                retro_files = sorted(glob.glob(os.path.join(RETRO_DIR, '*.md')), reverse=True)[:7]
                for filepath in retro_files:
                    try:
                        content = open(filepath).read()
                        fm = {}
                        for line in content.split('\n'):
                            for key in ['date', 'total_decisions', 'missed_opportunities',
                                        'correct_blocks', 'correct_alerts', 'false_alerts']:
                                m = _re.match(rf'^{key}:\s*(.+)', line)
                                if m:
                                    val = m.group(1).strip()
                                    fm[key] = int(val) if val.isdigit() else val
                        if fm:
                            total = fm.get('total_decisions', 0)
                            correct = fm.get('correct_blocks', 0) + fm.get('correct_alerts', 0)
                            missed  = fm.get('missed_opportunities', 0)
                            fm['accuracy_pct'] = round(correct / total * 100) if total else 0
                            fm['miss_rate_pct'] = round(missed / total * 100) if total else 0
                            fm['is_today'] = (fm.get('date') == today_str)
                            fm['source'] = 'retrospective'
                            daily_reports.append(fm)
                    except Exception:
                        pass

            # ── Pattern recommendations ────────────────────────────────────────
            pattern_files = sorted(glob.glob(os.path.join(PATTERNS_DIR, 'scout-learnings-*.md')), reverse=True)
            latest_patterns = ""
            recommendations = []
            if pattern_files:
                try:
                    content = open(pattern_files[0]).read()
                    latest_patterns = pattern_files[0].split('/')[-1]
                    in_rec = False
                    for line in content.split('\n'):
                        # Match both "## Recommended" and "## 📋 Recommended Scout Tuning"
                        if '## Recommended' in line or 'Recommended Scout Tuning' in line:
                            in_rec = True
                        elif in_rec and line.startswith('##'):
                            in_rec = False
                        elif in_rec and line.startswith('- **'):
                            recommendations.append(line[4:].strip())
                        elif in_rec and line.startswith('- ') and not line.startswith('- **'):
                            # Also capture non-bold recommendations
                            recommendations.append(line[2:].strip())
                except Exception:
                    pass

            # ── Aggregate totals (historical only; today is live) ─────────────
            total_days   = len(daily_reports)
            total_dec    = sum(r.get('total_decisions', 0) for r in daily_reports)
            total_missed = sum(r.get('missed_opportunities', 0) for r in daily_reports)
            total_correct= sum(r.get('correct_alerts', 0) + r.get('correct_blocks', 0)
                               for r in daily_reports)
            avg_accuracy = round(total_correct / total_dec * 100) if total_dec else 0

            return jsonify({
                "ok": True,
                "summary": {
                    "days_tracked": total_days,
                    "total_decisions": total_dec,
                    "avg_accuracy_pct": avg_accuracy,
                    "total_missed": total_missed,
                    "miss_rate_pct": round(total_missed / total_dec * 100) if total_dec else 0,
                },
                "daily": daily_reports,
                "today_live": today_live,
                "active_snipes": today_snipes,
                "recommendations": recommendations,
                "latest_pattern_file": latest_patterns,
            })
        except Exception as e:
            logger.error(f"scout-intelligence endpoint: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500


    # ──────────────────────────────────────────────────────────────────────────
    # GET /api/trading/performance
    # ── Helper: session comparison (last 5 sessions vs today) ────────────────
    def _build_session_compare():
        """Compare today's key metrics against the last 5 trading sessions."""
        try:
            import sqlite3 as _s5
            from db_pool import get_trading_forex as _gtf_sc
            _sc_conn = _gtf_sc()
            _sc_conn.row_factory = _s5.Row
            # 2026-05-05: bucket by LOCAL date, not UTC. exit_time is stored UTC;
            # a trade closing 22:00 ET on 05-04 lives at 02:00Z 05-05 — UTC date
            # would put it on the wrong session. 'localtime' modifier converts to
            # the server's local tz before extracting the date.
            rows = _sc_conn.execute("""
                SELECT
                  date(exit_time, 'localtime') as session_date,
                  COUNT(*) as trades,
                  SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                  ROUND(COALESCE(SUM(realized_pl), 0), 2) as total_usd,
                  ROUND(COALESCE(SUM(pips), 0), 1) as total_pips,
                  ROUND(AVG(pips), 1) as avg_pips
                FROM live_trades
                WHERE exit_time IS NOT NULL
                  AND exit_time >= datetime('now', '-8 days')
                GROUP BY session_date
                ORDER BY session_date DESC
                LIMIT 6
            """).fetchall()
            # Do NOT close pooled connection
            sessions = []
            for r in rows:
                t = r['trades'] or 0
                w = r['wins'] or 0
                sessions.append({
                    "date": r['session_date'],
                    "trades": t,
                    "wins": w,
                    "losses": t - w,
                    "win_rate": round(w/t*100,1) if t else 0,
                    "total_usd": r['total_usd'] or 0,
                    "total_pips": r['total_pips'] or 0,
                    "avg_pips": r['avg_pips'],
                })
            return sessions
        except Exception as _sc_err:
            return []

    # ── Helper: tune recommendations from live data ───────────────────────────
    def _build_tune_recommendations(phases, funnel, summary, guardian_rules):
        """Generate specific tuning suggestions based on today's data."""
        recs = []
        trades = summary.get('trades', 0) or 0
        wins   = summary.get('wins', 0) or 0
        wr     = wins / trades if trades else 0

        # WR analysis
        if trades >= 5 and wr < 0.40:
            recs.append({
                "category": "Win Rate",
                "issue": f"WR {wr:.0%} below 40% target over {trades} trades",
                "check": "Review direction gate thresholds — are bad-direction snipes getting through?",
                "metric": f"current: {wr:.0%}",
                "target": "≥50%",
            })

        # Execution failures
        exec_fails = len(funnel.get('execution_failures', []))
        if exec_fails > 0:
            recs.append({
                "category": "Execution",
                "issue": f"{exec_fails} order(s) confirmed by validator but never placed",
                "check": "Check logs for 'execution_failed' entries — identify which pairs and times",
                "metric": f"{exec_fails} failures today",
                "target": "0",
            })

        # Pipeline drop-off
        confirms = funnel.get('validator_confirms', 0) or 0
        watches  = funnel.get('validator_watches', 0) or 0
        if (confirms + watches) > 0 and trades < (confirms + watches) * 0.3:
            recs.append({
                "category": "Snipe Conditions",
                "issue": f"Only {trades} trades from {confirms+watches} confirms+watches ({trades/(confirms+watches)*100:.0f}%)",
                "check": "Snipe conditions may be too strict — trades timing out before conditions fire",
                "metric": f"{trades}/{confirms+watches} executed",
                "target": "≥50% of confirms should trade",
            })

        # Phase 5 exhaustion — did it fire?
        phase5 = next((p for p in (phases or []) if p.get('phase') == 'exhaustion'), None)
        if not phase5 and trades >= 3:
            recs.append({
                "category": "Phase 5 Exit",
                "issue": "Exhaustion exit never triggered today",
                "check": "Threshold may be too high (8p + RSI>70). Lower to 6p + RSI>65 if trades peaked early",
                "metric": "0 exhaustion exits",
                "target": "Should fire on extended trending trades",
            })
        elif phase5:
            avg_at_exhaustion = phase5.get('avg_pips', 0) or 0
            recs.append({
                "category": "Phase 5 Exit",
                "issue": None,
                "check": f"Firing at avg {avg_at_exhaustion:+.1f}p — compare to actual trade peak to calibrate",
                "metric": f"{phase5.get('count',0)}× at avg {avg_at_exhaustion:+.1f}p",
                "target": "Should match or slightly lead actual peak",
            })

        # Phase 3 retrace data
        phase3 = next((p for p in (phases or []) if p.get('phase') == 'retracing'), None)
        if not phase3:
            recs.append({
                "category": "Phase 3 Data",
                "issue": "No retrace phase transitions recorded",
                "check": "This populates as live trades go through retracements — normal for first session",
                "metric": "0 retrace events",
                "target": "Will build with more live trades",
            })
        elif phase3 and phase3.get('avg_pips', 0) < -3:
            recs.append({
                "category": "Phase 3 SL Trail",
                "issue": f"Retraces averaging {phase3['avg_pips']:+.1f}p when Phase 3 starts — SL trailing may be too fast",
                "check": "If trades hit SL during Phase 3, slow the trail from 30% to 20% per tick",
                "metric": f"avg pips at retrace start: {phase3['avg_pips']:+.1f}p",
                "target": "Should be near-zero or slightly positive at Phase 3 entry",
            })

        if not recs:
            recs.append({
                "category": "System Health",
                "issue": None,
                "check": "No tuning flags — system performing within expected parameters",
                "metric": "All checks pass",
                "target": "—",
            })
        return recs

    # Tim-only admin performance dashboard — real data from flight_recorder.
    # Replaces the stale markdown-based accuracy numbers in Team Intelligence.
    # ──────────────────────────────────────────────────────────────────────────
    @app.route("/api/trading/performance", methods=["GET", "OPTIONS"])
    def api_trading_performance():
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        if not user_info.get("is_admin"):
            return jsonify({"error": "Admin only"}), 403

        import sqlite3 as _psq, json as _pj
        from datetime import datetime, timezone, timedelta

        FLIGHT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flight_recorder.db")
        _uid = user_info["user_id"]

        now = datetime.now(timezone.utc)
        fc = None
        try:
            fc = _psq.connect(FLIGHT_DB, timeout=5, isolation_level=None)
            fc.execute("PRAGMA mmap_size=0")  # FUSE safety
            fc.row_factory = _psq.Row

            # ── 1. Today's session summary ─────────────────────────────────
            # Trade stats come from live_trades (unified) in v2/trading_forex.db.
            # Scout/validator counts still come from flight_recorder.
            _trade_summary = {"trades": 0, "wins": 0, "losses": 0, "total_usd": 0, "avg_pips": None}
            try:
                from db_pool import get_trading_forex as _gtf_perf
                _tc = _gtf_perf()
                _tc.row_factory = _psq.Row
                _ts = _tc.execute("""
                    SELECT
                      SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) as trades,
                      SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                      SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) as losses,
                      ROUND(COALESCE(SUM(CASE WHEN status='closed' THEN realized_pl END), 0), 2) as total_usd,
                      ROUND(AVG(CASE WHEN status='closed' THEN pips END), 1) as avg_pips
                    FROM live_trades
                    WHERE datetime(entry_time) >= datetime('now', 'localtime', 'start of day', 'utc')
                      AND (user_id = ? OR user_id IS NULL)
                      AND COALESCE(exit_trigger, '') != 'ghost_trade_debounce_artifact'
                """, (_uid,)).fetchone()
                if _ts and _ts["trades"]:
                    _trade_summary = dict(_ts)
                # Do NOT close pooled connection
            except Exception as _te:
                logger.warning(f"performance: v2 trading_forex query failed: {_te}")

            _event_counts = fc.execute("""
                SELECT
                  COUNT(CASE WHEN stage='scout_alert' THEN 1 END) as scout_alerts,
                  COUNT(CASE WHEN stage='validator_verdict' THEN 1 END) as validator_calls
                FROM flight_log
                WHERE timestamp >= date('now', 'start of day')
                  AND (user_id = ? OR user_id IS NULL)
            """, (_uid,)).fetchone()

            # Merge into a unified day_summary dict
            day_summary = {
                "trades":          _trade_summary["trades"] or 0,
                "wins":            _trade_summary["wins"] or 0,
                "losses":          _trade_summary["losses"] or 0,
                "total_usd":       _trade_summary["total_usd"] or 0,
                "avg_pips":        _trade_summary["avg_pips"],
                "scout_alerts":    _event_counts["scout_alerts"] or 0,
                "validator_calls": _event_counts["validator_calls"] or 0,
            }

            # ── 2. Scout→Trade correlation (last 24h) ──────────────────────
            # Per-pair trade stats from manual_trades, with scout-led detection
            # from flight_recorder scout_alert events.
            scout_corr = []
            try:
                from db_pool import get_trading_forex as _gtf_corr
                _tc2 = _gtf_corr()
                _tc2.row_factory = _psq.Row
                _corr_rows = _tc2.execute("""
                    SELECT
                      pair,
                      COUNT(*) as trades,
                      SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) as wins,
                      SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) as losses,
                      ROUND(AVG(CASE WHEN pips IS NOT NULL THEN pips END), 1) as avg_pips,
                      ROUND(COALESCE(SUM(realized_pl), 0), 2) as total_usd,
                      SUM(CASE WHEN entry_type = 'snipe_direct' THEN 1 ELSE 0 END) as snipe_count,
                      SUM(CASE WHEN entry_type = 'manual' OR entry_type IS NULL OR entry_type = 'none' THEN 1 ELSE 0 END) as manual_count,
                      SUM(CASE WHEN entry_type NOT IN ('snipe_direct', 'manual', 'none') AND entry_type IS NOT NULL THEN 1 ELSE 0 END) as scout_count
                    FROM live_trades
                    WHERE datetime(entry_time) >= datetime('now', 'localtime', 'start of day', 'utc')
                      AND (user_id = ? OR user_id IS NULL)
                    GROUP BY pair
                    ORDER BY COUNT(*) DESC
                """, (_uid,)).fetchall()
                # Do NOT close pooled connection

                # Enrich with scout-led count from flight_recorder
                for r in _corr_rows:
                    row = dict(r)
                    scout_led = 0
                    try:
                        scout_led = fc.execute("""
                            SELECT COUNT(DISTINCT pair || substr(timestamp,1,13))
                            FROM flight_log
                            WHERE stage='scout_alert' AND pair=?
                              AND timestamp >= date('now', 'start of day')
                              AND (user_id = ? OR user_id IS NULL)
                        """, (row["pair"], _uid)).fetchone()[0] or 0
                    except Exception:
                        pass
                    row["scout_led"] = min(scout_led, row["trades"])
                    scout_corr.append(row)
            except Exception as _ce:
                logger.warning(f"performance: scout correlation query failed: {_ce}")

            # ── 2b. Scout alert detail (always populated, even without trades) ──
            scout_alert_detail = []
            try:
                _alert_rows = fc.execute("""
                    SELECT pair,
                           COUNT(*) as alerts,
                           MAX(timestamp) as last_alert,
                           GROUP_CONCAT(DISTINCT COALESCE(note,'')) as verdicts
                    FROM flight_log
                    WHERE stage='scout_alert'
                      AND timestamp >= date('now', 'start of day')
                      AND (user_id = ? OR user_id IS NULL)
                    GROUP BY pair
                    ORDER BY COUNT(*) DESC
                """, (_uid,)).fetchall()
                for r in _alert_rows:
                    row = dict(r)
                    # Check if this pair had a cycle and what the validator said
                    _vrow = fc.execute("""
                        SELECT note FROM flight_log
                        WHERE stage='validator_verdict' AND pair=?
                          AND timestamp >= date('now', 'start of day')
                          AND (user_id = ? OR user_id IS NULL)
                        ORDER BY timestamp DESC LIMIT 1
                    """, (row["pair"], _uid)).fetchone()
                    row["validator_verdict"] = _vrow[0] if _vrow else None
                    # Check if a watch was created
                    _wrow = fc.execute("""
                        SELECT COUNT(*) FROM flight_log
                        WHERE stage='watch_create' AND pair=?
                          AND timestamp >= date('now', 'start of day')
                          AND (user_id = ? OR user_id IS NULL)
                    """, (row["pair"], _uid)).fetchone()
                    row["watches_created"] = _wrow[0] if _wrow else 0
                    scout_alert_detail.append(row)
            except Exception as _sad_err:
                logger.warning(f"performance: scout alert detail failed: {_sad_err}")

            # ── 3. Pipeline funnel — the only meaningful metric ──────────────
            # Measures the SCOUT → CYCLE → VALIDATOR → TRADE → WIN funnel.
            # Each drop-off is an actionable problem, not just a count.
            #
            # Real missed opportunity = validator said CONFIRM/WATCH but no trade opened.
            # NOT: scout alert fired and pipeline correctly rejected it (that's working).

            # Step A: how many scout alerts became cycles?
            funnel_alerts = fc.execute("""
                SELECT COUNT(DISTINCT pair || substr(timestamp,1,13)) as alerts
                FROM flight_log
                WHERE stage='scout_alert' AND timestamp >= date('now', 'start of day')
                  AND (user_id = ? OR user_id IS NULL)
            """, (_uid,)).fetchone()[0] or 0

            funnel_cycles = fc.execute("""
                SELECT COUNT(*) FROM flight_log
                WHERE stage='cycle_start' AND timestamp >= date('now', 'start of day')
                  AND (user_id = ? OR user_id IS NULL)
            """, (_uid,)).fetchone()[0] or 0

            # Step B: validator verdicts breakdown
            funnel_verdicts = fc.execute("""
                SELECT
                  SUM(CASE WHEN note LIKE '%CONFIRM%' OR note LIKE '%TRADE_NOW%' THEN 1 ELSE 0 END) as confirms,
                  SUM(CASE WHEN note LIKE '%WATCH%' THEN 1 ELSE 0 END) as watches,
                  SUM(CASE WHEN note LIKE '%REJECT%' OR note LIKE '%SKIP%' THEN 1 ELSE 0 END) as rejects
                FROM flight_log
                WHERE stage='validator_verdict' AND timestamp >= date('now', 'start of day')
                  AND (user_id = ? OR user_id IS NULL)
            """, (_uid,)).fetchone()

            # Step C: REAL missed = validator confirmed but execution_failed
            real_missed = fc.execute("""
                SELECT pair, COUNT(*) as count, MAX(timestamp) as last_time
                FROM flight_log
                WHERE stage='execution'
                  AND (note LIKE '%execution_failed%' OR note LIKE '%No trade_id%' OR note LIKE '%prose without calling%')
                  AND timestamp >= date('now', 'start of day')
                  AND (user_id = ? OR user_id IS NULL)
                GROUP BY pair
                ORDER BY count DESC
            """, (_uid,)).fetchall()

            # Step D: validator said WATCH but no snipe was created (validator set bad conditions?)
            # Note: timestamps are stored as ISO with +00:00 suffix (e.g. 2026-03-24T17:47:15+00:00)
            # but datetime() returns space-separated without TZ (e.g. 2026-03-24 17:49:15).
            # String comparison of 'T' > ' ' breaks BETWEEN, so we normalize with replace().
            watch_no_snipe = fc.execute("""
                SELECT vv.pair, COUNT(*) as watches_no_snipe
                FROM flight_log vv
                WHERE vv.stage='validator_verdict'
                  AND vv.note LIKE '%WATCH%'
                  AND vv.timestamp >= date('now', 'start of day')
                  AND (vv.user_id = ? OR vv.user_id IS NULL)
                  AND NOT EXISTS (
                    SELECT 1 FROM flight_log wc
                    WHERE wc.stage='watch_create' AND wc.pair=vv.pair
                    AND (wc.user_id = ? OR wc.user_id IS NULL)
                    AND replace(wc.timestamp,'T',' ') BETWEEN replace(vv.timestamp,'T',' ')
                        AND datetime(replace(substr(vv.timestamp,1,19),'T',' '), '+2 minutes')
                  )
                GROUP BY vv.pair
            """, (_uid, _uid)).fetchall()

            pipeline_funnel = {
                "scout_alerts": funnel_alerts,
                "cycles_run": funnel_cycles,
                "validator_confirms": funnel_verdicts[0] if funnel_verdicts else 0,
                "validator_watches": funnel_verdicts[1] if funnel_verdicts else 0,
                "validator_rejects": funnel_verdicts[2] if funnel_verdicts else 0,
                "execution_failures": [dict(r) for r in real_missed],
                "watch_no_snipe": [dict(r) for r in watch_no_snipe],
            }
            # Keep backward compat key for UI
            missed = real_missed

            # ── 4. Guardian rule attribution (what closed each trade) ─────────
            guardian_rules = fc.execute("""
                SELECT
                  json_extract(data,'$.action') as rule,
                  COUNT(*) as fires,
                  ROUND(AVG(CAST(json_extract(data,'$.pnl_pips') AS REAL)),1) as avg_pips_at_fire
                FROM flight_log
                WHERE stage='guardian_action'
                  AND timestamp >= date('now', 'start of day')
                  AND (user_id = ? OR user_id IS NULL)
                GROUP BY rule
                ORDER BY fires DESC
            """, (_uid,)).fetchall()

            # ── 5. Cascade phase stats ─────────────────────────────────────
            # Reads from trade_phases table if it has data today
            phase_stats = []
            try:
                phase_rows = fc.execute("""
                    SELECT phase, COUNT(*) as count,
                           ROUND(AVG(pnl_pips),1) as avg_pips,
                           ROUND(MIN(pnl_pips),1) as min_pips,
                           ROUND(MAX(pnl_pips),1) as max_pips,
                           COUNT(CASE WHEN pnl_pips > 0 THEN 1 END) as in_profit
                    FROM trade_phases
                    WHERE timestamp >= date('now', 'start of day')
                    GROUP BY phase
                    ORDER BY CASE phase
                        WHEN 'trending' THEN 1 WHEN 'peak' THEN 2
                        WHEN 'retracing' THEN 3 WHEN 'continuing' THEN 4
                        WHEN 'exhaustion' THEN 5 ELSE 6 END
                """).fetchall()
                phase_stats = [dict(r) for r in phase_rows]
            except Exception:
                pass  # table may be empty on first run today

            if fc:
                fc.close()
                fc = None

            # ── 6. Snipe leaderboard (conditions patterns ranked by win rate) ──
            leaderboard = []
            try:
                bc = get_trading_forex()
                bc.row_factory = _psq.Row
                # snipe_leaderboard may not have user_id column yet —
                # try filtered query, fall back to unfiltered
                try:
                    lb_rows = bc.execute("""
                        SELECT instrument, suggestion_type,
                               times_triggered, times_won,
                               ROUND(win_rate,1) as win_rate,
                               ROUND(avg_pips,1) as avg_pips,
                               ROUND(total_pips,1) as total_pips,
                               last_triggered_at
                        FROM snipe_leaderboard
                        WHERE times_triggered >= 2
                          AND (user_id = ? OR user_id IS NULL)
                        ORDER BY win_rate DESC, avg_pips DESC
                        LIMIT 20
                    """, (_uid,)).fetchall()
                except Exception:
                    lb_rows = bc.execute("""
                        SELECT instrument, suggestion_type,
                               times_triggered, times_won,
                               ROUND(win_rate,1) as win_rate,
                               ROUND(avg_pips,1) as avg_pips,
                               ROUND(total_pips,1) as total_pips,
                               last_triggered_at
                        FROM snipe_leaderboard
                        WHERE times_triggered >= 2
                        ORDER BY win_rate DESC, avg_pips DESC
                        LIMIT 20
                    """).fetchall()
                leaderboard = [dict(r) for r in lb_rows]

                # ── 7. Active snipe staleness summary ────────────────────────
                stale_snipes = bc.execute("""
                    SELECT id, instrument,
                           json_extract(context,'$.direction') as direction,
                           conditions_met_count, conditions_total_count,
                           ROUND((julianday('now') - julianday(created_at)) * 24, 1) as age_h,
                           CASE
                             WHEN (julianday('now') - julianday(created_at)) * 24 > 48 THEN 'stale'
                             WHEN (julianday('now') - julianday(created_at)) * 24 > 24 THEN 'aging'
                             ELSE 'fresh'
                           END as freshness
                    FROM watch_suggestions
                    WHERE status='watching'
                      AND (user_id = ? OR user_id IS NULL)
                    ORDER BY age_h DESC
                """, (_uid,)).fetchall()
                stale_snipes = [dict(r) for r in stale_snipes]
            except Exception as _lb_err:
                logger.error("performance endpoint: %s", _lb_err)
                if 'stale_snipes' not in dir():
                    stale_snipes = []
                if 'leaderboard' not in dir():
                    leaderboard = []

            # ── Individual trades list (all trades today, one row per trade) ──
            trades_today = []
            try:
                import sqlite3 as _tt_sq
                from db_pool import get_trading_forex as _gtf_tt
                _tt_conn = _gtf_tt()
                _tt_conn.row_factory = _tt_sq.Row
                # 2026-04-29: include trades that CLOSED today even if they opened
                # yesterday. Original query filtered on entry_time only, so a trade
                # opened 11:30 PM ET (yesterday in local time) and closed 02:12 AM ET
                # (today) was invisible to "TRADES TODAY" / SESSION P&L despite the
                # win realizing today. Now: include rows where either entry OR exit
                # is in today's local-day window.
                trades_today = [dict(r) for r in _tt_conn.execute("""
                    SELECT id, pair, direction, entry_type, source, status, result,
                           entry_price, exit_price, pips, realized_pl,
                           entry_time, exit_time, oanda_trade_id,
                           fan_state, setup, sl_price, tp_price
                    FROM live_trades
                    WHERE (
                        datetime(entry_time) >= datetime('now', 'localtime', 'start of day', 'utc')
                        OR datetime(exit_time)  >= datetime('now', 'localtime', 'start of day', 'utc')
                    )
                      AND (user_id = ? OR user_id IS NULL)
                      AND COALESCE(exit_trigger, '') != 'ghost_trade_debounce_artifact'
                    ORDER BY COALESCE(exit_time, entry_time) DESC
                """, (_uid,)).fetchall()]

                # ── Inline reconciliation: fix stale 'open' rows ──
                # If server recycled during a close, the guardian's reconcile loop
                # never ran for that trade. Check OANDA for any 'open' rows that
                # are actually closed and update them in-place.
                _open_rows = [t for t in trades_today if t.get('status') == 'open' and t.get('oanda_trade_id')]
                if _open_rows:
                    try:
                        # Get current OANDA open trade IDs (cached per-request)
                        if not hasattr(request, '_oanda_open_ids'):
                            try:
                                from oanda_client import OandaClient as _OC_tt
                                _oc_tt = _OC_tt()
                                _oanda_open = _oc_tt.get_open_trades()
                                request._oanda_open_ids = {str(t.get('id', '')) for t in _oanda_open}
                            except Exception as _open_ids_err:
                                # 2026-04-24: upgraded from silent. If this raises,
                                # ALL inline reconciliation is skipped this request —
                                # stale 'open' trades accumulate in live_trades.
                                logger.warning("[PERF] OANDA get_open_trades failed — "
                                               "inline reconciliation DISABLED this request "
                                               "(stale open trades won't be updated): %s: %s",
                                               type(_open_ids_err).__name__, _open_ids_err)
                                request._oanda_open_ids = None

                        if request._oanda_open_ids is not None:
                            for _row in _open_rows:
                                _otid = str(_row['oanda_trade_id'])
                                if _otid not in request._oanda_open_ids:
                                    # Trade closed on OANDA but DB still says open — reconcile
                                    try:
                                        _oc_rec = _OC_tt()
                                        _closed_t = _oc_rec.get_trade(_otid)
                                        if _closed_t:
                                            _rpl = float(_closed_t.get('realizedPL', 0))
                                            _exit_p = float(_closed_t.get('averageClosePrice', 0))
                                            _close_time = _closed_t.get('closeTime', '')
                                            _entry_p = float(_row.get('entry_price') or 0)
                                            _pip_sz = 0.01 if 'JPY' in (_row.get('pair') or '') else 0.0001
                                            _dir = _row.get('direction', 'buy')
                                            if _exit_p > 0 and _entry_p > 0 and _pip_sz > 0:
                                                _pips = ((_exit_p - _entry_p) / _pip_sz) if _dir == 'buy' else ((_entry_p - _exit_p) / _pip_sz)
                                            else:
                                                _pips = 0
                                            _pips = round(_pips, 1)
                                            _outcome = 'win' if _rpl > 0 else 'loss'

                                            # 2026-04-06: Only set exit_method if guardian hasn't
                                            # already written it. Guardian writes exit_method='guardian'
                                            # with MFE/MAE data — don't overwrite with 'reconcile_inline'.
                                            _existing_method = _tt_conn.execute(
                                                "SELECT exit_method FROM live_trades WHERE oanda_trade_id=?",
                                                (_otid,)).fetchone()
                                            _em = 'reconcile_inline'
                                            if _existing_method and _existing_method[0] and _existing_method[0] != 'reconcile_inline':
                                                _em = _existing_method[0]  # preserve guardian's exit_method
                                            # Preserve guardian's exit_trigger if already set
                                            _existing_trigger = _tt_conn.execute(
                                                "SELECT exit_trigger FROM live_trades WHERE oanda_trade_id=?",
                                                (_otid,)).fetchone()
                                            _et = 'oanda_auto_close'
                                            if _existing_trigger and _existing_trigger[0]:
                                                _et = _existing_trigger[0]
                                            _tt_conn.execute("""
                                                UPDATE live_trades SET
                                                    status='closed', pips=?, realized_pl=?, result=?,
                                                    exit_price=?, exit_time=?, pnl_pips=?, pnl_usd=?,
                                                    outcome=?, exit_method=?, exit_trigger=?
                                                WHERE oanda_trade_id=?
                                            """, (_pips, round(_rpl, 2), _outcome,
                                                  _exit_p, _close_time, _pips, round(_rpl, 2),
                                                  _outcome, _em, _et, _otid))
                                            _tt_conn.commit()

                                            # Update in-memory row for this response
                                            _row['status'] = 'closed'
                                            _row['pips'] = _pips
                                            _row['realized_pl'] = round(_rpl, 2)
                                            _row['result'] = _outcome
                                            _row['exit_price'] = _exit_p
                                            _row['exit_time'] = _close_time
                                            logger.info("[PERF] Inline reconciled trade %s: %s %+.1fp $%.2f",
                                                        _otid, _outcome, _pips, _rpl)
                                    except Exception as _rec_err:
                                        # If OANDA returns 404 (trade doesn't exist), close it
                                        # so it doesn't stay "open" in the DB forever.
                                        # 2026-04-21: Check if guardian already wrote pnl_pips
                                        # before zeroing — prevents wiping real P&L data.
                                        _is_404 = '404' in str(_rec_err) or 'does not exist' in str(_rec_err).lower()
                                        if _is_404:
                                            _now_utc = datetime.now(timezone.utc).isoformat()
                                            # Preserve existing pnl/trigger if guardian wrote them
                                            _existing = _tt_conn.execute(
                                                "SELECT pnl_pips, pips, realized_pl, exit_trigger, exit_method "
                                                "FROM live_trades WHERE oanda_trade_id=?", (_otid,)
                                            ).fetchone()
                                            _has_pnl = _existing and _existing[0] and float(_existing[0]) != 0
                                            _has_trigger = _existing and _existing[3] and _existing[3] not in ('', 'oanda_404_not_found', 'oanda_auto_close')
                                            if _has_pnl or _has_trigger:
                                                # Guardian already wrote real data — just close status
                                                _tt_conn.execute("""
                                                    UPDATE live_trades SET status='closed', exit_time=?
                                                    WHERE oanda_trade_id=? AND status='open'
                                                """, (_now_utc, _otid))
                                                _p = float(_existing[0] or 0)
                                                _outcome_404 = 'win' if _p > 0 else 'loss' if _p < 0 else 'unknown'
                                                _tt_conn.execute(
                                                    "UPDATE live_trades SET outcome=?, result=? WHERE oanda_trade_id=? AND outcome IS NULL",
                                                    (_outcome_404, _outcome_404, _otid))
                                                logger.info("[PERF] Trade %s 404 but guardian data preserved (%.1fp %s)", _otid, _p, _existing[3])
                                            else:
                                                # 2026-04-23: first try OANDA transactions API to find the
                                                # actual ORDER_FILL for this trade — get_trade may 404 within
                                                # seconds of close but transactions API still has the fill.
                                                # Trade 9967 case: dashboard showed 0p/0USD/unknown when actual
                                                # was -3.4p/-$21.85.
                                                _tx_pips = None
                                                _tx_usd = 0.0
                                                _tx_close_price = 0.0
                                                _tx_close_time = _now_utc
                                                try:
                                                    _tx = _oc_tt.get_trade_close_from_transactions(_otid)
                                                    if _tx and _tx.get('close_price') and _tx.get('close_time'):
                                                        _entry_p = float(_row.get('entry_price') or _tx.get('open_price') or 0)
                                                        _exit_p = float(_tx.get('close_price', 0))
                                                        _dir = (_row.get('direction') or '').lower()
                                                        _pip_sz = 0.01 if 'JPY' in (_row.get('pair') or '') else 0.0001
                                                        if _exit_p > 0 and _entry_p > 0 and _pip_sz > 0:
                                                            _tx_pips = round(
                                                                ((_exit_p - _entry_p) / _pip_sz) if _dir == 'buy'
                                                                else ((_entry_p - _exit_p) / _pip_sz),
                                                                1,
                                                            )
                                                        _tx_usd = round(float(_tx.get('realized_pl', 0) or 0), 2)
                                                        _tx_close_price = _exit_p
                                                        _tx_close_time = _tx.get('close_time') or _now_utc
                                                except Exception:
                                                    pass
                                                if _tx_pips is not None:
                                                    # Use transactions-API data (authoritative)
                                                    _outcome_404 = 'win' if _tx_pips > 0 else 'loss' if _tx_pips < 0 else 'unknown'
                                                    _tt_conn.execute("""
                                                        UPDATE live_trades SET
                                                            status='closed', pips=?, realized_pl=?, result=?,
                                                            exit_time=?, exit_price=?,
                                                            pnl_pips=?, pnl_usd=?,
                                                            outcome=?, outcome_pips=?, outcome_usd=?,
                                                            exit_method='reconcile_transactions_api',
                                                            exit_trigger='oanda_404_recovered'
                                                        WHERE oanda_trade_id=?
                                                    """, (_tx_pips, _tx_usd, _outcome_404,
                                                          _tx_close_time, _tx_close_price,
                                                          _tx_pips, _tx_usd,
                                                          _outcome_404, _tx_pips, _tx_usd,
                                                          _otid))
                                                    _fr_pnl = _tx_pips  # alias for downstream dict update
                                                    logger.warning("[PERF] Trade %s 404 — recovered via transactions API: %.1fp ($%.2f) %s",
                                                                   _otid, _tx_pips, _tx_usd, _outcome_404)
                                                else:
                                                    # Fall back to flight recorder's last-known (stale) pnl
                                                    _fr_pnl = 0.0
                                                    try:
                                                        import sqlite3 as _s3_fr
                                                        _fr_path = os.path.join(os.path.dirname(__file__), 'flight_recorder.db')
                                                        if os.path.exists(_fr_path):
                                                            _fr_c = _s3_fr.connect(_fr_path, timeout=2)
                                                            _fr_row = _fr_c.execute(
                                                                "SELECT json_extract(data, '$.pnl_pips') FROM flight_log "
                                                                "WHERE trade_id=? AND stage LIKE '%guardian_threat%' "
                                                                "ORDER BY timestamp DESC LIMIT 1", (str(_otid),)
                                                            ).fetchone()
                                                            _fr_c.close()
                                                            if _fr_row and _fr_row[0]:
                                                                _fr_pnl = round(float(_fr_row[0]), 1)
                                                    except Exception:
                                                        pass
                                                    _outcome_404 = 'win' if _fr_pnl > 0 else 'loss' if _fr_pnl < 0 else 'unknown'
                                                    _tt_conn.execute("""
                                                        UPDATE live_trades SET
                                                            status='closed', pips=?, realized_pl=0, result=?,
                                                            exit_time=?, pnl_pips=?, pnl_usd=0,
                                                            outcome=?, exit_method='reconcile_inline',
                                                            exit_trigger='oanda_404_not_found'
                                                        WHERE oanda_trade_id=?
                                                    """, (_fr_pnl, _outcome_404, _now_utc, _fr_pnl, _outcome_404, _otid))
                                                    logger.warning("[PERF] Trade %s 404 — transactions API also empty, used flight recorder PnL %.1fp (%s)",
                                                                   _otid, _fr_pnl, _outcome_404)
                                            _tt_conn.commit()
                                            _row['status'] = 'closed'
                                            _row['result'] = _outcome_404 if '_outcome_404' in dir() else 'unknown'
                                            _row['pips'] = _fr_pnl if '_fr_pnl' in dir() else (_existing[0] if _has_pnl else 0)
                                            _row['realized_pl'] = _existing[2] if _has_pnl else 0
                                        else:
                                            # 2026-04-24: upgraded from silent debug.
                                            # Non-404 exception during per-trade reconcile → DB stays stale.
                                            # Discovered via trade 10094 AUD_USD which closed at OANDA 11:07 ET
                                            # but DB showed 'open' for 5+ hours until manual investigation.
                                            logger.warning(
                                                "[PERF] Inline reconcile FAILED for trade %s: %s: %s "
                                                "(DB status remains 'open' — this trade needs manual reconcile)",
                                                _otid, type(_rec_err).__name__, _rec_err)
                    except Exception as _rec_outer:
                        # 2026-04-24: upgraded from silent debug. Outer exception in
                        # the reconcile block — potentially means reconciliation
                        # never touched ANY trade this request.
                        logger.warning(
                            "[PERF] Inline reconciliation block FAILED (no trades reconciled this request): %s: %s",
                            type(_rec_outer).__name__, _rec_outer)

            except Exception as _tt_err:
                logger.warning("performance: trades_today query failed: %s", _tt_err)

            # ── Recompute session_summary from trades_today (post-reconciliation) ──
            # This ensures any inline-reconciled trades are reflected immediately
            # rather than waiting for the next poll cycle.
            if trades_today:
                _closed_today = [t for t in trades_today if t.get('status') == 'closed'
                                 and t.get('exit_trigger') != 'ghost_trade_debounce_artifact']
                _recomp_trades = len(_closed_today)
                _recomp_wins = sum(1 for t in _closed_today if t.get('result') == 'win')
                _recomp_losses = sum(1 for t in _closed_today if t.get('result') == 'loss')
                _recomp_usd = round(sum(float(t.get('realized_pl') or 0) for t in _closed_today), 2)
                _pip_vals = [float(t.get('pips') or 0) for t in _closed_today if t.get('pips') is not None]
                _recomp_avg_pips = round(sum(_pip_vals) / max(len(_pip_vals), 1), 1) if _pip_vals else None
                day_summary = {
                    "trades": _recomp_trades,
                    "wins": _recomp_wins,
                    "losses": _recomp_losses,
                    "total_usd": _recomp_usd,
                    "avg_pips": _recomp_avg_pips,
                    "scout_alerts": day_summary.get("scout_alerts", 0),
                    "validator_calls": day_summary.get("validator_calls", 0),
                }

            return jsonify({
                "ok": True,
                "generated_at": now.isoformat(),
                "session_summary": {
                    "trades":        day_summary["trades"] or 0,
                    "wins":          day_summary["wins"] or 0,
                    "losses":        day_summary["losses"] or 0,
                    "total_usd":     day_summary["total_usd"] or 0,
                    "avg_pips":      day_summary["avg_pips"],
                    "win_rate":      round(day_summary["wins"] / max(day_summary["trades"],1) * 100, 1),
                    "scout_alerts":  day_summary["scout_alerts"] or 0,
                    "validator_calls": day_summary["validator_calls"] or 0,
                },
                "trades_today": trades_today,
                "scout_correlation": [dict(r) for r in scout_corr],
                "scout_alert_detail": scout_alert_detail,
                "missed_opportunities": [dict(r) for r in missed],
                "pipeline_funnel": pipeline_funnel,
                "guardian_rules": [dict(r) for r in guardian_rules],
                "cascade_phases": phase_stats,
                "snipe_leaderboard": leaderboard,
                "active_snipes": stale_snipes,
                "session_compare": _build_session_compare(),
                "tune_recommendations": _build_tune_recommendations(phase_stats, pipeline_funnel, dict(day_summary) if day_summary else {}, [dict(r) for r in guardian_rules]),
            })

        except Exception as e:
            logger.error("performance endpoint: %s", e, exc_info=True)
            return jsonify({"error": str(e)}), 500

    # ══════════════════════════════════════════════════════════════════════
    # LIVE ENDPOINTS: Learning Events, Sentry Report, Pipeline Lineage
    # These return fresh data from the databases on every call so the
    # dashboard doesn't depend on static JSON files.
    # ══════════════════════════════════════════════════════════════════════

    @app.route("/api/trading/learning-events", methods=["GET", "OPTIONS"])
    def learning_events_live():
        """Live learning events from flight recorder + setup_trades + learning_events.json fallback."""
        if request.method == "OPTIONS":
            return _cors_preflight()
        user_info, err = _get_authenticated_user()
        if err:
            return err

        try:
            import sqlite3, os, json as _json
            from datetime import datetime, timedelta

            _src_dir = os.path.dirname(os.path.abspath(__file__))
            _proj_dir = os.path.dirname(_src_dir)

            events = []
            agent_health = {"scout": {}, "validator": {}, "guardian": {}, "orchestrator": {}}
            last_updated = None

            # ── Source 1: Flight recorder learning_* stages ──────────────
            fr_path = os.path.join(_src_dir, "flight_recorder.db")
            if os.path.exists(fr_path):
                conn = None
                try:
                    conn = sqlite3.connect(fr_path, timeout=5)
                    conn.execute("PRAGMA mmap_size=0")
                    conn.row_factory = sqlite3.Row
                    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()

                    rows = conn.execute("""
                        SELECT stage, pair, cycle_id, trade_id, status, data, note,
                               duration_ms, timestamp
                        FROM flight_log
                        WHERE stage LIKE 'learning_%' AND timestamp > ?
                        ORDER BY timestamp DESC LIMIT 50
                    """, (cutoff,)).fetchall()

                    for r in rows:
                        data = {}
                        try:
                            data = _json.loads(r["data"]) if r["data"] else {}
                        except Exception:
                            pass
                        ev_type = r["stage"].replace("learning_", "")
                        events.append({
                            "type": ev_type,
                            "pair": r["pair"] or "",
                            "cycle_id": r["cycle_id"] or "",
                            "status": r["status"] or "ok",
                            "note": r["note"] or "",
                            "timestamp": r["timestamp"],
                            "duration_ms": r["duration_ms"] or 0,
                            "data": data,
                        })

                    if rows:
                        last_updated = rows[0]["timestamp"]

                    for agent in ["scout", "validator", "guardian"]:
                        try:
                            stage = f"learning_{agent}"
                            cnt = conn.execute(
                                "SELECT COUNT(*) FROM flight_log WHERE stage = ? AND timestamp > ?",
                                (stage, cutoff)).fetchone()[0]
                            errs = conn.execute(
                                "SELECT COUNT(*) FROM flight_log WHERE stage = ? AND status = 'error' AND timestamp > ?",
                                (stage, cutoff)).fetchone()[0]
                            agent_health[agent] = {
                                "recent_events": cnt,
                                "corrections_ratio": round(errs / max(cnt, 1), 2),
                            }
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug("flight_recorder learning query: %s", e)
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass

            # ── Source 2: Build trade audit events from setup_trades in v2 DB ──
            # This is the ACTUAL trade outcome data. Generates trade_audit_learning
            # events that the UI can render as win/loss cards.
            try:
                from db_pool import get_trading_forex as _gtf_learn
                tl_conn = _gtf_learn()
                tl_conn.row_factory = sqlite3.Row
                # Recent closed trades with outcomes
                closed = tl_conn.execute("""
                    SELECT trade_id, setup_name, pair, direction, outcome,
                           pnl_pips, pnl_usd, r_multiple, close_reason,
                           duration_minutes, scout_confidence,
                           opened_at, closed_at, source
                    FROM setup_trades
                    WHERE outcome IS NOT NULL
                    ORDER BY closed_at DESC
                    LIMIT 30
                """).fetchall()

                # Aggregate stats per agent from setup_revenue
                scout_wins = 0; scout_total = 0
                val_total = 0; val_errors = 0
                try:
                    rev_rows = tl_conn.execute(
                        "SELECT wins, losses, total_trades FROM setup_revenue"
                    ).fetchall()
                    for rv in rev_rows:
                        scout_total += rv["total_trades"] or 0
                        scout_wins += rv["wins"] or 0
                except Exception:
                    pass

                for t in closed:
                    learnings = []
                    pair = t["pair"] or ""
                    outcome = t["outcome"] or ""
                    pips = t["pnl_pips"] or 0
                    setup = t["setup_name"] or ""

                    # Generate learnings based on outcome
                    if outcome == "loss":
                        learnings.append(f"scout_thesis_failure_{pair.replace('/','_')}_{setup}")
                        if t["scout_confidence"] and t["scout_confidence"] < 60:
                            learnings.append(f"scout_accuracy_degrading_{pair.replace('/','_')}")
                        learnings.append(f"validator_entry_timing_{pair.replace('/','_')}_{int(t['scout_confidence'] or 0)}")
                    elif outcome == "win":
                        learnings.append(f"scout_signal_accuracy_{pair.replace('/','_')}_{int(t['scout_confidence'] or 0)}pct")

                    if t["close_reason"] and "guardian" in (t["close_reason"] or "").lower():
                        learnings.append(f"guardian_{t['close_reason'].replace(' ','_')}")

                    events.append({
                        "type": "trade_audit_learning",
                        "pair": pair,
                        "outcome": outcome,
                        "pnl_pips": round(pips, 1) if pips else 0,
                        "setup": setup,
                        "timestamp": t["closed_at"] or t["opened_at"] or "",
                        "learnings": learnings,
                        "learnings_count": len(learnings),
                        "data": {
                            "r_multiple": t["r_multiple"],
                            "duration_minutes": t["duration_minutes"],
                            "close_reason": t["close_reason"],
                            "source": t["source"],
                        },
                    })

                    if not last_updated and (t["closed_at"] or t["opened_at"]):
                        last_updated = t["closed_at"] or t["opened_at"]

                # Build agent health from setup_revenue aggregates
                if scout_total > 0:
                    loss_rate = round((scout_total - scout_wins) / max(scout_total, 1), 2)
                    agent_health["scout"] = {
                        "recent_events": scout_total,
                        "corrections_ratio": loss_rate,
                    }
                # Validator: count validation_log entries with issues
                try:
                    v_total = tl_conn.execute(
                        "SELECT COUNT(*) FROM validation_log WHERE timestamp > datetime('now', '-7 days')"
                    ).fetchone()[0]
                    v_passed = tl_conn.execute(
                        "SELECT COUNT(*) FROM validation_log WHERE overall_passed = 1 AND timestamp > datetime('now', '-7 days')"
                    ).fetchone()[0]
                    agent_health["validator"] = {
                        "recent_events": v_total,
                        "corrections_ratio": round((v_total - v_passed) / max(v_total, 1), 2),
                    }
                except Exception:
                    pass

                # Do NOT close pooled connection
            except Exception as e:
                logger.debug("v2 trading_forex learning query: %s", e)

            # ── Source 3: Fallback to static learning_events.json ─────────
            # Written by learning_integrator.py — use if flight recorder is empty
            if not events:
                le_path = os.path.join(_proj_dir, "dashboard", "learning_events.json")
                if os.path.exists(le_path):
                    try:
                        with open(le_path) as f:
                            le_data = _json.load(f)
                        events = le_data.get("events", [])
                        last_updated = le_data.get("last_updated", last_updated)
                        for agent in ["scout", "validator", "guardian", "orchestrator"]:
                            ah = le_data.get("agent_health", {}).get(agent, {})
                            if ah and not agent_health.get(agent):
                                agent_health[agent] = ah
                    except Exception as e:
                        logger.debug("learning_events.json fallback: %s", e)

            # Sort events by timestamp descending
            events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

            return jsonify({
                "ok": True,
                "events": events,
                "agent_health": agent_health,
                "last_updated": last_updated,
            })
        except Exception as e:
            logger.error("learning-events-live: %s", e, exc_info=True)
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/trading/pool-stats", methods=["GET", "OPTIONS"])
    def pool_stats_endpoint():
        """Connection pool health stats for dashboard monitoring."""
        if request.method == "OPTIONS":
            return _cors_preflight()
        try:
            from db_pool import pool_stats
            stats = pool_stats()
            return jsonify({"ok": True, **stats})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/trading/sentry-report", methods=["GET", "OPTIONS"])
    def sentry_report_live():
        """Live sentry report from flight recorder — no stale JSON dependency."""
        if request.method == "OPTIONS":
            return _cors_preflight()
        user_info, err = _get_authenticated_user()
        if err:
            return err

        try:
            import sqlite3, os, json as _json
            from datetime import datetime, timedelta

            # flight_recorder.db lives in Source/ (same directory as this file)
            fr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flight_recorder.db")

            report = {
                "healthy": True,
                "trade_closes": 0,
                "wins": 0,
                "losses": 0,
                "learning_loops_complete": 0,
                "avg_learnings_per_trade": 0,
                "avg_duration_ms": 0,
                "stage_coverage": {},
                "scout_learnings": 0,
                "validator_learnings": 0,
                "guardian_learnings": 0,
                "errors": 0,
                "issues": [],
                "timestamp": datetime.utcnow().isoformat(),
            }

            if os.path.exists(fr_path):
                from db_connection import get_db
                with get_db(fr_path, timeout=5) as conn:
                    conn.row_factory = sqlite3.Row
                    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()

                    # Trade closes (learning_audit entries = one per trade close)
                    closes = conn.execute("""
                        SELECT COUNT(*) FROM flight_log
                        WHERE stage = 'learning_audit' AND timestamp > ?
                    """, (cutoff,)).fetchone()[0]
                    report["trade_closes"] = closes

                    # Win/Loss — primary source: setup_trades (covers scout + manual)
                    # Fallback: flight_recorder learning_audit (scout-only) if DB unavailable
                    try:
                        from db_pool import get_trading_forex as _gtf_sentry
                        _st_conn = _gtf_sentry()
                        _st_conn.row_factory = sqlite3.Row
                        _st_rows = _st_conn.execute("""
                            SELECT outcome FROM setup_trades
                            WHERE outcome IS NOT NULL
                              AND COALESCE(closed_at, opened_at) > ?
                        """, (cutoff,)).fetchall()
                        for _r in _st_rows:
                            _o = (_r["outcome"] or "").lower()
                            if _o == "win":
                                report["wins"] += 1
                            elif _o in ("loss", "lose"):
                                report["losses"] += 1
                        # trade_closes = all closed trades (not just learning_audit entries)
                        report["trade_closes"] = len(_st_rows)
                    except Exception:
                        # Fallback: flight_recorder learning_audit (scout-only)
                        try:
                            audit_rows = conn.execute("""
                                SELECT data FROM flight_log
                                WHERE stage = 'learning_audit' AND timestamp > ?
                                  AND data IS NOT NULL
                            """, (cutoff,)).fetchall()
                            for row in audit_rows:
                                try:
                                    d = _json.loads(row["data"]) if row["data"] else {}
                                    outcome = d.get("outcome", "")
                                    if outcome == "win":
                                        report["wins"] += 1
                                    elif outcome in ("loss", "lose"):
                                        report["losses"] += 1
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    # Complete learning loops
                    completes = conn.execute("""
                        SELECT COUNT(*) FROM flight_log
                        WHERE stage = 'learning_complete' AND timestamp > ?
                    """, (cutoff,)).fetchone()[0]
                    report["learning_loops_complete"] = completes

                    # Avg duration
                    avg_dur = conn.execute("""
                        SELECT AVG(duration_ms) FROM flight_log
                        WHERE stage LIKE 'learning_%' AND timestamp > ?
                          AND duration_ms > 0
                    """, (cutoff,)).fetchone()[0]
                    report["avg_duration_ms"] = round(avg_dur or 0, 1)

                    # Stage coverage — count each learning stage
                    all_stages = [
                        "learning_audit", "learning_scout", "learning_validator",
                        "learning_guardian", "learning_knowledge", "learning_retro",
                        "learning_drift", "learning_thesis", "learning_tuning",
                        "learning_dashboard", "learning_complete"
                    ]
                    for stage in all_stages:
                        cnt = conn.execute("""
                            SELECT COUNT(*) FROM flight_log
                            WHERE stage = ? AND timestamp > ?
                        """, (stage, cutoff)).fetchone()[0]
                        pct = min(100, round(cnt / max(closes, 1) * 100, 1)) if closes > 0 else (100.0 if cnt > 0 else 0)
                        report["stage_coverage"][stage] = pct

                    # Per-agent vault write counts
                    for agent, key in [("scout", "scout_learnings"),
                                       ("validator", "validator_learnings"),
                                       ("guardian", "guardian_learnings")]:
                        cnt = conn.execute("""
                            SELECT COUNT(*) FROM flight_log
                            WHERE stage = ? AND timestamp > ?
                        """, (f"learning_{agent}", cutoff)).fetchone()[0]
                        report[key] = cnt

                    # Error count
                    errs = conn.execute("""
                        SELECT COUNT(*) FROM flight_log
                        WHERE stage LIKE 'learning_%' AND status = 'error'
                          AND timestamp > ?
                    """, (cutoff,)).fetchone()[0]
                    report["errors"] = errs

                    # Avg learnings per trade
                    if closes > 0:
                        total_learning_stages = conn.execute("""
                            SELECT COUNT(*) FROM flight_log
                            WHERE stage LIKE 'learning_%' AND timestamp > ?
                        """, (cutoff,)).fetchone()[0]
                        report["avg_learnings_per_trade"] = round(
                            total_learning_stages / closes, 1)

                    # Health check
                    if closes > 0 and completes == 0:
                        report["healthy"] = False
                        report["issues"].append(
                            f"{closes} trade(s) closed but 0 learning loops completed")
                    if errs > 2:
                        report["healthy"] = False
                        report["issues"].append(f"{errs} learning pipeline errors in 24h")

            return jsonify({"ok": True, **report})
        except Exception as e:
            logger.error("sentry-report-live: %s", e, exc_info=True)
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/trading/lineage-report", methods=["GET", "OPTIONS"])
    def lineage_report_live():
        """Live pipeline lineage from databases — stitches the chain in real time."""
        if request.method == "OPTIONS":
            return _cors_preflight()
        user_info, err = _get_authenticated_user()
        if err:
            return err

        try:
            from pipeline_lineage import PipelineLineage
            pl = PipelineLineage()
            report = pl.generate_report(hours_back=24)
            return jsonify({"ok": True, **report})
        except Exception as e:
            logger.error("lineage-report-live: %s", e, exc_info=True)
            return jsonify({"ok": False, "error": str(e)}), 500

    # ── Connection Sentry API endpoints ──

    @app.route("/api/trading/sentry", methods=["GET", "OPTIONS"])
    def api_trading_sentry():
        """Full connection health report from the Connection Sentry."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from connection_sentry import sentry
            report = sentry.get_report()
            return jsonify(report)
        except ImportError:
            return jsonify({"error": "Connection Sentry not available"}), 503
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/trading/sentry/check/<connection_id>", methods=["POST", "OPTIONS"])
    def api_trading_sentry_check(connection_id):
        """Run an immediate health check on a specific connection."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from connection_sentry import sentry
            result = sentry.check(connection_id)
            if result:
                return jsonify(result.to_dict())
            return jsonify({"error": f"Unknown connection: {connection_id}"}), 404
        except ImportError:
            return jsonify({"error": "Connection Sentry not available"}), 503
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/trading/sentry/connections", methods=["GET", "OPTIONS"])
    def api_trading_sentry_connections():
        """List all monitored connection IDs."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from connection_sentry import sentry
            return jsonify({"connections": sentry.get_connection_ids()})
        except ImportError:
            return jsonify({"error": "Connection Sentry not available"}), 503

    @app.route("/api/trading/sentry/circuit-breaker", methods=["GET", "OPTIONS"])
    def api_trading_circuit_breaker():
        """Get OANDA circuit breaker status."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            from connection_sentry import oanda_breaker
            return jsonify(oanda_breaker.get_status())
        except ImportError:
            return jsonify({"error": "Connection Sentry not available"}), 503

    # ══════════════════════════════════════════════════════════════════════
    # TUNING DASHBOARD API
    # ══════════════════════════════════════════════════════════════════════

    @app.route("/api/trading/tuning/history", methods=["GET", "OPTIONS"])
    def api_trading_tuning_history():
        """Return all tuning overrides with backtest data and trade links."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            import sqlite3 as _tsql
            with _tsql.connect(_TRADING_FOREX_DB, timeout=5) as _tc:
                _tc.row_factory = _tsql.Row
                rows = _tc.execute("""
                    SELECT id, param, value, previous_value, reason,
                           backtest_result, approved_by, approved_at,
                           active, created_at
                    FROM tuning_overrides
                    ORDER BY created_at DESC
                """).fetchall()
            result = []
            for r in rows:
                entry = dict(r)
                # Parse backtest_result JSON if present
                if entry.get("backtest_result"):
                    try:
                        entry["backtest_result"] = json.loads(entry["backtest_result"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                result.append(entry)
            return jsonify({"tuning_history": result, "count": len(result)})
        except Exception as e:
            logger.error("tuning/history error: %s", e)
            return jsonify({"tuning_history": [], "count": 0, "error": str(e)})

    @app.route("/api/trading/tuning/performance", methods=["GET", "OPTIONS"])
    def api_trading_tuning_performance():
        """Compare trade performance before vs after each tuning change.

        Returns ALL tuning history (active + reverted) grouped by batch_label.
        For each tuning override, pulls trades from the 48hrs before the change
        and trades after the change (up to the NEXT change on the same param),
        computing win rate, avg PnL, and SL-hit rate.
        """
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            import sqlite3 as _tsql
            with _tsql.connect(_TRADING_FOREX_DB, timeout=5) as _tc:
                _tc.row_factory = _tsql.Row

                overrides = _tc.execute("""
                    SELECT id, param, value, previous_value, created_at, reason,
                           backtest_result, active,
                           COALESCE(batch_label, '') as batch_label,
                           COALESCE(change_type, 'param_change') as change_type
                    FROM tuning_overrides
                    ORDER BY created_at DESC
                """).fetchall()

                # Build a map of next-change timestamps per param
                # so "after" window ends at next change, not forever
                param_timestamps = {}
                for ov in overrides:
                    p = ov["param"]
                    if p not in param_timestamps:
                        param_timestamps[p] = []
                    param_timestamps[p].append(ov["created_at"])

                results = []
                for ov in overrides:
                    ov_dict = dict(ov)
                    ts = ov_dict["created_at"]
                    if not ts:
                        results.append({**ov_dict, "before": None, "after": None})
                        continue

                    # Determine which source types this param affects
                    param = ov_dict["param"]
                    if "manual" in param:
                        source_filter = "source = 'manual'"
                    elif any(k in param for k in ("snipe", "guardian", "gate.", "watch.", "scout.", "validator.")):
                        source_filter = "source IN ('snipe_direct','scout','snipe')"
                    elif "fix." in param:
                        source_filter = "1=1"  # bug fixes affect all trades
                    else:
                        source_filter = "1=1"

                    # Find the "after" window end: next change on same param, or now
                    pts = sorted(param_timestamps.get(param, []))
                    after_end = None
                    for pt in pts:
                        if pt > ts:
                            after_end = pt
                            break
                    after_end_clause = f"AND entry_time < datetime('{after_end}')" if after_end else ""

                    # Before: 48hrs before the tuning change
                    before_trades = _tc.execute(f"""
                        SELECT COUNT(*) as total,
                               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                               SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                               ROUND(AVG(pnl_usd), 2) as avg_pnl,
                               ROUND(SUM(pnl_usd), 2) as total_pnl,
                               ROUND(AVG(pnl_pips), 1) as avg_pips
                        FROM live_trades
                        WHERE {source_filter}
                          AND exit_price IS NOT NULL
                          AND entry_time BETWEEN datetime(?, '-48 hours') AND datetime(?)
                    """, (ts, ts)).fetchone()

                    # After: from tuning change to next change (or now)
                    after_trades = _tc.execute(f"""
                        SELECT COUNT(*) as total,
                               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                               SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                               ROUND(AVG(pnl_usd), 2) as avg_pnl,
                               ROUND(SUM(pnl_usd), 2) as total_pnl,
                               ROUND(AVG(pnl_pips), 1) as avg_pips
                        FROM live_trades
                        WHERE {source_filter}
                          AND exit_price IS NOT NULL
                          AND entry_time > datetime(?)
                          {after_end_clause}
                    """, (ts,)).fetchone()

                    # SL-hit rate (reconcile_inline = broker hit the SL)
                    before_sl = _tc.execute(f"""
                        SELECT COUNT(*) as sl_hits
                        FROM live_trades
                        WHERE {source_filter}
                          AND exit_method = 'reconcile_inline'
                          AND pnl_usd < 0
                          AND entry_time BETWEEN datetime(?, '-48 hours') AND datetime(?)
                    """, (ts, ts)).fetchone()

                    after_sl = _tc.execute(f"""
                        SELECT COUNT(*) as sl_hits
                        FROM live_trades
                        WHERE {source_filter}
                          AND exit_method = 'reconcile_inline'
                          AND pnl_usd < 0
                          AND entry_time > datetime(?)
                          {after_end_clause}
                    """, (ts,)).fetchone()

                    def _stats(row, sl_row):
                        if not row or not row["total"]:
                            return None
                        total = row["total"]
                        wins = row["wins"] or 0
                        return {
                            "total": total,
                            "wins": wins,
                            "losses": row["losses"] or 0,
                            "win_rate": round(wins / total * 100, 1) if total else 0,
                            "avg_pnl": row["avg_pnl"],
                            "total_pnl": row["total_pnl"],
                            "avg_pips": row["avg_pips"],
                            "sl_hits": sl_row["sl_hits"] if sl_row else 0,
                        }

                    entry = {**ov_dict}
                    if entry.get("backtest_result"):
                        try:
                            entry["backtest_result"] = json.loads(entry["backtest_result"])
                        except (json.JSONDecodeError, TypeError):
                            pass
                    entry["before"] = _stats(before_trades, before_sl)
                    entry["after"] = _stats(after_trades, after_sl)
                    results.append(entry)

            return jsonify({"tuning_performance": results})
        except Exception as e:
            logger.error("tuning/performance error: %s", e)
            return jsonify({"tuning_performance": [], "error": str(e)})

    @app.route("/api/trading/tuning/snapshot", methods=["GET", "OPTIONS"])
    def api_trading_tuning_snapshot():
        """Return current active tuning values as a flat key→value map."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            import sqlite3 as _tsql
            with _tsql.connect(_TRADING_FOREX_DB, timeout=5) as _tc:
                rows = _tc.execute("""
                    SELECT param, value FROM tuning_overrides WHERE active = 1
                """).fetchall()
            return jsonify({"active_tuning": {r[0]: r[1] for r in rows}})
        except Exception as e:
            return jsonify({"active_tuning": {}, "error": str(e)})

    # ── QA Audit Report Endpoints ──────────────────────────────────────────

    @app.route("/api/trading/qa-audit", methods=["GET", "OPTIONS"])
    def api_trading_qa_audit():
        """Return the latest QA audit report and any pending recommendations.

        Reads from Forex Trading Team/Reports/qa_audit_*.md (latest by date)
        and from tuning_overrides where approved_by LIKE 'qa-auditor%'.
        """
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            import sqlite3 as _tsql, glob as _glob, re as _re

            _src_dir = os.path.dirname(os.path.abspath(__file__))
            _proj_dir = os.path.dirname(_src_dir)
            reports_dir = os.path.join(_proj_dir, "Reports")

            # Find latest audit report
            report_text = ""
            report_date = ""
            report_files = sorted(_glob.glob(os.path.join(reports_dir, "qa_audit_*.md")), reverse=True)
            if report_files:
                report_date = os.path.basename(report_files[0]).replace("qa_audit_", "").replace(".md", "")
                with open(report_files[0], "r") as f:
                    report_text = f.read()

            # Detect report format. New format ("Trade Performance Report") emits
            # ## Headline (by source) instead of ## Executive Summary.
            is_new_format = (
                "## Headline" in report_text
                and "## Executive Summary" not in report_text
            )

            summary = {}
            trade_grades = []
            proposed_tuning = []
            recommendations = []

            if is_new_format and report_text:
                # ── Headline (by source) → trades_audited + weighted win_rate
                head_match = _re.search(
                    r'## Headline.*?\n\|.*?\n\|[-| ]+\n(.*?)(?=\n## |\Z)',
                    report_text, _re.DOTALL
                )
                if head_match:
                    total_n = 0
                    total_wins = 0.0
                    for row in head_match.group(1).strip().split("\n"):
                        cols = [c.strip() for c in row.split("|") if c.strip()]
                        if len(cols) >= 3:
                            try:
                                n = int(cols[1])
                                wr_pct = float(cols[2].rstrip("%"))
                                total_n += n
                                total_wins += n * wr_pct / 100.0
                            except (ValueError, IndexError):
                                continue
                    if total_n > 0:
                        summary["trades_audited"] = total_n
                        summary["win_rate"] = round(100.0 * total_wins / total_n, 1)

                # ── Regressions section → critical_findings + recommendations
                regressions_match = _re.search(
                    r'## Regressions.*?\n(.*?)(?=\n## |\Z)',
                    report_text, _re.DOTALL
                )
                if regressions_match:
                    regressions_text = regressions_match.group(1).strip()
                    summary["critical_findings"] = len(
                        _re.findall(r'\*\*CRITICAL\*\*', regressions_text)
                    )
                    for line in regressions_text.split("\n"):
                        line = line.strip()
                        if line.startswith("- "):
                            recommendations.append(line[2:].strip())
                else:
                    summary["critical_findings"] = 0

                # ── Profit Zones → trade_grades (per pair/source view)
                pz_match = _re.search(
                    r'## Profit Zones.*?\n(.*?)(?=\n## |\Z)',
                    report_text, _re.DOTALL
                )
                if pz_match:
                    zone_re = _re.compile(
                        r"\*\*#?(\d+)\*\*\s*\{[^}]*'pair':\s*'([^']+)'[^}]*"
                        r"'source':\s*'([^']+)'[^}]*\}\s*[—-]+\s*"
                        r"(\d+)\s*trades?,\s*(-?[\d.]+)p\s*total,\s*WR\s*([\d.]+)%"
                    )
                    for line in pz_match.group(1).strip().split("\n"):
                        m = zone_re.search(line)
                        if m:
                            rank, pair, source, n, pips, wr = m.groups()
                            trade_grades.append({
                                "id": "#" + rank,
                                "pair": pair,
                                "dir": source,
                                "pips": pips + "p",
                                "entry_grade": n + " trades",
                                "exit_grade": "WR " + wr + "%",
                                "issues": "",
                            })

                # ── Snipe Quality by Origin (table) → snipe_section text
                snipe_match = _re.search(
                    r'## Snipe Quality.*?\n(.*?)(?=\n## |\Z)',
                    report_text, _re.DOTALL
                )
                if snipe_match:
                    summary["snipe_section"] = snipe_match.group(1).strip()

                # ── Tuning Impact list (positive/negative verdicts on completed
                # changes) → expose for display, not approval.
                tuning_impact_match = _re.search(
                    r'## Tuning Impact.*?\n(.*?)(?=\n## |\Z)',
                    report_text, _re.DOTALL
                )
                if tuning_impact_match:
                    summary["tuning_impact_section"] = tuning_impact_match.group(1).strip()

            elif report_text:
                # ── Legacy format: "## Executive Summary" + per-trade grades
                summary_match = _re.search(
                    r'## Executive Summary\n(.*?)(?=\n## |\Z)',
                    report_text, _re.DOTALL
                )
                if summary_match:
                    summary["raw"] = summary_match.group(1).strip()
                    for line in summary_match.group(1).split("\n"):
                        line = line.strip("- ")
                        if "Win rate:" in line:
                            m = _re.search(r'(\d+\.?\d*)%', line)
                            if m:
                                summary["win_rate"] = float(m.group(1))
                        elif "Trades audited:" in line:
                            m = _re.search(r'(\d+)', line)
                            if m:
                                summary["trades_audited"] = int(m.group(1))
                        elif "Critical findings:" in line:
                            m = _re.search(r'(\d+)', line)
                            if m:
                                summary["critical_findings"] = int(m.group(1))

                grade_match = _re.search(
                    r'## Trade-by-Trade Grades\n\|.*?\n\|[-| ]+\n(.*?)(?=\n## |\Z)',
                    report_text, _re.DOTALL
                )
                if grade_match:
                    for row in grade_match.group(1).strip().split("\n"):
                        cols = [c.strip() for c in row.split("|") if c.strip()]
                        if len(cols) >= 6:
                            trade_grades.append({
                                "id": cols[0], "pair": cols[1], "dir": cols[2],
                                "pips": cols[3], "entry_grade": cols[4],
                                "exit_grade": cols[5],
                                "issues": cols[6] if len(cols) > 6 else "",
                            })

                snipe_match = _re.search(
                    r'## Snipe Health\n(.*?)(?=\n## |\Z)',
                    report_text, _re.DOTALL
                )
                if snipe_match:
                    summary["snipe_section"] = snipe_match.group(1).strip()

                tuning_match = _re.search(
                    r'## Proposed Tuning Changes\n\|.*?\n\|[-| ]+\n(.*?)(?=\n## |\Z)',
                    report_text, _re.DOTALL
                )
                if tuning_match:
                    for row in tuning_match.group(1).strip().split("\n"):
                        cols = [c.strip() for c in row.split("|") if c.strip()]
                        if len(cols) >= 4:
                            proposed_tuning.append({
                                "param": cols[0], "current": cols[1],
                                "proposed": cols[2], "reason": cols[3],
                                "expected_impact": cols[4] if len(cols) > 4 else "",
                            })

                rec_match = _re.search(
                    r'## Recommendations.*?\n(.*?)(?=\n## |\Z)',
                    report_text, _re.DOTALL
                )
                if rec_match:
                    for line in rec_match.group(1).strip().split("\n"):
                        line = line.strip()
                        if line and (line[0].isdigit() or line.startswith("-")):
                            recommendations.append(line.lstrip("0123456789.- "))

            # Get QA-auditor tuning proposals (pending approval)
            # Uses tuning_proposals table (proper pipeline) with fallback to tuning_overrides
            pending_proposals = []
            try:
                with _tsql.connect(_TRADING_FOREX_DB, timeout=5) as _tc:
                    _tc.row_factory = _tsql.Row
                    # Primary: tuning_proposals (proper pipeline with backtest)
                    rows = _tc.execute("""
                        SELECT id, param, proposed_value as value,
                               current_value as previous_value, reason, created_at,
                               backtest_status, backtest_improvement,
                               status, approved_by,
                               'proposal' as source
                        FROM tuning_proposals
                        WHERE status = 'pending'
                        ORDER BY created_at DESC LIMIT 100
                    """).fetchall()
                    pending_proposals = [dict(r) for r in rows]

                    # Fallback: tuning_overrides written directly by qa-auditor
                    if not pending_proposals:
                        rows = _tc.execute("""
                            SELECT id, param, value, previous_value, reason, created_at,
                                   backtest_result, active, approved_by,
                                   'override' as source
                            FROM tuning_overrides
                            WHERE approved_by LIKE 'qa-auditor%'
                              AND approved_by LIKE '%pending%'
                              AND active = 0
                            ORDER BY created_at DESC LIMIT 20
                        """).fetchall()
                        pending_proposals = [dict(r) for r in rows]
            except Exception:
                pass

            return jsonify({
                "report_date": report_date,
                "summary": summary,
                "trade_grades": trade_grades,
                "recommendations": recommendations,
                "proposed_tuning": proposed_tuning,
                "pending_proposals": pending_proposals,
                "has_report": bool(report_text),
                "report_text": report_text[:5000] if report_text else "",
            })

        except Exception as e:
            logger.error("QA audit endpoint error: %s", e)
            return jsonify({"has_report": False, "error": str(e)})

    @app.route("/api/trading/optimizer", methods=["GET", "OPTIONS"])
    def api_trading_optimizer():
        """Return the latest parameter optimizer report.

        Reads from Forex Trading Team/Reports/optimizer_report_*.md (latest by date)
        and parses summary metrics for the dashboard.
        """
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            import glob as _glob, re as _re

            _src_dir = os.path.dirname(os.path.abspath(__file__))
            _proj_dir = os.path.dirname(_src_dir)
            reports_dir = os.path.join(_proj_dir, "Reports")

            report_files = sorted(
                _glob.glob(os.path.join(reports_dir, "optimizer_report_*.md")),
                reverse=True,
            )
            if not report_files:
                return jsonify({"status": "no_reports", "message": "No optimizer reports yet"})

            latest_file = report_files[0]
            filename = os.path.basename(latest_file)
            with open(latest_file, "r") as f:
                content = f.read()

            # Parse summary section
            summary = {}
            for line in content.splitlines():
                line = line.strip("- ")
                if "Baseline win rate:" in line:
                    m = _re.search(r'([\d.]+)%', line)
                    if m:
                        summary["baseline_win_rate"] = float(m.group(1))
                elif "Optimized win rate:" in line:
                    m = _re.search(r'([\d.]+)%', line)
                    if m:
                        summary["optimized_win_rate"] = float(m.group(1))
                elif "Improvement:" in line:
                    m = _re.search(r'([+-]?[\d.]+)pp', line)
                    if m:
                        summary["improvement_pp"] = float(m.group(1))
                elif "Evaluations:" in line:
                    m = _re.search(r'(\d+)', line)
                    if m:
                        summary["evaluations"] = int(m.group(1))

            return jsonify({
                "status": "ok",
                "latest_report": filename,
                "summary": summary,
                "full_report": content,
            })

        except Exception as e:
            logger.error("Optimizer endpoint error: %s", e)
            return jsonify({"status": "error", "error": str(e)}), 500

    @app.route("/api/trading/qa-audit/approve", methods=["POST", "OPTIONS"])
    def api_trading_qa_audit_approve():
        """Approve or reject a QA auditor tuning proposal.

        Uses tuning_config.approve_proposal() which:
        1. Runs backtest (if not already done)
        2. Creates active tuning_overrides entry
        3. Applies value to in-memory TUNING dict (live on next cycle)
        4. Updates proposal status
        """
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            import sqlite3 as _tsql
            data = request.get_json(force=True)
            proposal_id = data.get("id")
            action = data.get("action")  # "approve" or "reject"
            source = data.get("source", "proposal")  # "proposal" or "override"

            if not proposal_id or action not in ("approve", "reject"):
                return jsonify({"error": "id and action (approve/reject) required"}), 400

            if source == "proposal":
                # Use proper tuning_config pipeline
                import sys
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from tuning_config import approve_proposal, reject_proposal, backtest_proposal, get_proposal

                if action == "approve":
                    # Ensure backtest is complete before approval
                    proposal = get_proposal(proposal_id)
                    if proposal and proposal.get("backtest_status") != "complete":
                        logger.info("[QA_AUDIT] Running backtest for proposal #%d before approval", proposal_id)
                        backtest_proposal(proposal_id)

                    result = approve_proposal(proposal_id, approved_by="Tim (QA audit)")
                    if "error" in result:
                        return jsonify({"ok": False, "error": result["error"]}), 400
                    logger.info("[QA_AUDIT] APPROVED proposal #%d: %s = %s",
                                proposal_id, result.get("param"), result.get("new_value"))
                    return jsonify({"ok": True, "action": "approve", "id": proposal_id, "result": result})
                else:
                    result = reject_proposal(proposal_id, reason="Rejected by Tim via QA audit dashboard")
                    logger.info("[QA_AUDIT] REJECTED proposal #%d", proposal_id)
                    return jsonify({"ok": True, "action": "reject", "id": proposal_id})

            else:
                # Fallback: direct tuning_overrides entry (legacy path)
                with _tsql.connect(_TRADING_FOREX_DB, timeout=5) as _tc:
                    if action == "approve":
                        _tc.execute("""
                            UPDATE tuning_overrides
                            SET active = 1,
                                approved_by = REPLACE(approved_by, '(pending Tim review)', '(Tim approved)'),
                                approved_at = datetime('now')
                            WHERE id = ?
                        """, (proposal_id,))
                        logger.info("[QA_AUDIT] APPROVED override #%d (legacy path)", proposal_id)
                    else:
                        _tc.execute("""
                            UPDATE tuning_overrides
                            SET active = 0,
                                approved_by = REPLACE(approved_by, '(pending Tim review)', '(Tim rejected)')
                            WHERE id = ?
                        """, (proposal_id,))
                        logger.info("[QA_AUDIT] REJECTED override #%d", proposal_id)
                    _tc.commit()
                return jsonify({"ok": True, "action": action, "id": proposal_id})

        except Exception as e:
            logger.error("[QA_AUDIT] Approve/reject error: %s", e)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/trading/tuning/timeline", methods=["GET", "OPTIONS"])
    def api_trading_tuning_timeline():
        """Return daily win rate + tuning change markers with snapshot verdicts."""
        if request.method == "OPTIONS":
            return app.make_default_options_response()
        user_info, err = _get_authenticated_user()
        if err:
            return err
        try:
            import sqlite3 as _tsql
            days = int(request.args.get("days", 30))

            with _tsql.connect(_TRADING_FOREX_DB, timeout=5) as _tc:
                _tc.row_factory = _tsql.Row

                # Daily performance
                daily = _tc.execute("""
                    SELECT DATE(exit_time) as date,
                           COUNT(*) as trades,
                           SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                           ROUND(100.0 * SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate,
                           ROUND(SUM(pnl_usd), 2) as pnl,
                           ROUND(AVG(pnl_pips), 2) as avg_pips
                    FROM live_trades
                    WHERE exit_price IS NOT NULL
                      AND exit_time >= datetime('now', ? || ' days')
                    GROUP BY DATE(exit_time)
                    ORDER BY date
                """, (f"-{days}",)).fetchall()

                daily_perf = [dict(r) for r in daily]

                # Tuning events with snapshots
                events = _tc.execute("""
                    SELECT t.id, t.param, t.value, t.previous_value, t.reason,
                           DATE(t.created_at) as date, t.created_at,
                           t.change_type, t.batch_label, t.active
                    FROM tuning_overrides t
                    WHERE t.created_at >= datetime('now', ? || ' days')
                    ORDER BY t.created_at
                """, (f"-{days}",)).fetchall()

                # Group changes by date — batch changes on the same day into one marker
                by_date = {}
                for ev in events:
                    ev_dict = dict(ev)
                    d = ev_dict["date"]
                    if d not in by_date:
                        by_date[d] = {"date": d, "changes": [], "verdicts": [], "params": []}
                    by_date[d]["changes"].append(ev_dict)
                    by_date[d]["params"].append(ev_dict["param"])

                    # Get performance snapshots
                    try:
                        snapshots = _tc.execute("""
                            SELECT window_label, verdict, win_rate_delta, avg_pips_delta,
                                   pnl_delta, after_total, after_win_rate, measured_at
                            FROM tuning_performance_snapshots
                            WHERE override_id = ?
                            ORDER BY
                                CASE window_label
                                    WHEN '24h' THEN 1 WHEN '48h' THEN 2
                                    WHEN '7d' THEN 3 WHEN '14d' THEN 4
                                END
                        """, (ev_dict["id"],)).fetchall()
                        if snapshots:
                            latest = dict(snapshots[-1])
                            by_date[d]["verdicts"].append(latest.get("verdict", "pending"))
                    except Exception:
                        pass

                tuning_events = []
                for d, group in sorted(by_date.items()):
                    n = len(group["changes"])
                    verdicts = group["verdicts"]

                    # Determine overall verdict for the group
                    if not verdicts or all(v == "pending" for v in verdicts):
                        verdict = "pending"
                    elif sum(1 for v in verdicts if v == "positive") > sum(1 for v in verdicts if v == "negative"):
                        verdict = "positive"
                    elif sum(1 for v in verdicts if v == "negative") > sum(1 for v in verdicts if v == "positive"):
                        verdict = "negative"
                    else:
                        verdict = "neutral"

                    # Build a readable label
                    if n == 1:
                        label = group["params"][0].split(".")[-1]
                    elif n <= 3:
                        label = ", ".join(p.split(".")[-1] for p in group["params"][:3])
                    else:
                        # Group by prefix
                        prefixes = {}
                        for p in group["params"]:
                            prefix = p.split(".")[0]
                            prefixes[prefix] = prefixes.get(prefix, 0) + 1
                        parts = [f"{cnt} {pfx}" for pfx, cnt in sorted(prefixes.items(), key=lambda x: -x[1])]
                        label = f"{n} changes: " + ", ".join(parts)

                    tuning_events.append({
                        "date": d,
                        "param": label,
                        "count": n,
                        "overall_verdict": verdict,
                        "overall_wr_delta": 0,
                        "params": group["params"],
                        "changes": [{
                            "param": c["param"],
                            "value": c["value"],
                            "previous_value": c["previous_value"],
                        } for c in group["changes"]],
                    })

            return jsonify({
                "daily_performance": daily_perf,
                "tuning_events": tuning_events,
                "target_win_rate": 80.0,
                "days": days,
            })

        except Exception as e:
            logger.error("Tuning timeline error: %s", e)
            return jsonify({"daily_performance": [], "tuning_events": [], "error": str(e)})

    logger.info("Trading API routes registered: /api/trading/*")
