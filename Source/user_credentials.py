"""
User credential management for OANDA API.

Stores credentials securely per user:
- API key encrypted with Fernet (symmetric key derived from user token)
- Account IDs stored in JSON config per user
- Never exposes raw API key in config files, logs, or dashboard

Storage layout:
  API/users/{user_id}/credentials.enc   — encrypted API key
  API/users/{user_id}/config.json       — account IDs, environment, preferences

The system master key is stored in API/master.key (generated once).
Each user's API key is encrypted with: Fernet(master_key + user_id salt).
"""

import os
import json
import hashlib
import base64
import secrets
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

# Paths
_BASE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "API"
)
_USERS_DIR = os.path.join(_BASE_DIR, "users")
_MASTER_KEY_PATH = os.path.join(_BASE_DIR, "master.key")

PRACTICE_URL = "https://api-fxpractice.oanda.com"
LIVE_URL = "https://api-fxtrade.oanda.com"


# =====================================================================
# Encryption helpers (using Fernet from cryptography if available,
# otherwise falls back to base64 obfuscation + file permissions)
# =====================================================================

try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


def _get_master_key() -> bytes:
    """Get or create the master encryption key."""
    if os.path.exists(_MASTER_KEY_PATH):
        with open(_MASTER_KEY_PATH, "rb") as f:
            return f.read()
    
    # Generate new master key
    if HAS_CRYPTO:
        key = Fernet.generate_key()
    else:
        key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    
    os.makedirs(os.path.dirname(_MASTER_KEY_PATH), exist_ok=True)
    with open(_MASTER_KEY_PATH, "wb") as f:
        f.write(key)
    os.chmod(_MASTER_KEY_PATH, 0o600)  # owner read/write only
    
    return key


def _derive_user_key(user_id: str) -> bytes:
    """Derive a per-user encryption key from master key + user_id."""
    master = _get_master_key()
    # SHA256(master + user_id) → Fernet-compatible key
    raw = hashlib.sha256(master + user_id.encode()).digest()
    return base64.urlsafe_b64encode(raw)


def _encrypt(plaintext: str, user_id: str) -> bytes:
    """Encrypt a string for a specific user."""
    key = _derive_user_key(user_id)
    if HAS_CRYPTO:
        f = Fernet(key)
        return f.encrypt(plaintext.encode())
    else:
        # Fallback: XOR with key + base64 (NOT cryptographically secure,
        # but better than plaintext. File permissions are the real guard.)
        key_bytes = base64.urlsafe_b64decode(key)
        encrypted = bytes(a ^ key_bytes[i % len(key_bytes)] 
                         for i, a in enumerate(plaintext.encode()))
        return base64.urlsafe_b64encode(encrypted)


def _decrypt(ciphertext: bytes, user_id: str) -> str:
    """Decrypt a string for a specific user."""
    key = _derive_user_key(user_id)
    if HAS_CRYPTO:
        f = Fernet(key)
        return f.decrypt(ciphertext).decode()
    else:
        key_bytes = base64.urlsafe_b64decode(key)
        decoded = base64.urlsafe_b64decode(ciphertext)
        decrypted = bytes(a ^ key_bytes[i % len(key_bytes)] 
                         for i, a in enumerate(decoded))
        return decrypted.decode()


# =====================================================================
# User directory management
# =====================================================================

def _user_dir(user_id: str) -> str:
    """Get or create user directory."""
    path = os.path.join(_USERS_DIR, user_id)
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)  # owner only
    return path


def _user_config_path(user_id: str) -> str:
    return os.path.join(_user_dir(user_id), "config.json")


def _user_cred_path(user_id: str) -> str:
    return os.path.join(_user_dir(user_id), "credentials.enc")


def _load_user_config(user_id: str) -> Dict[str, Any]:
    """Load user config, return empty dict if not exists."""
    path = _user_config_path(user_id)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save_user_config(user_id: str, config: Dict[str, Any]):
    """Save user config."""
    path = _user_config_path(user_id)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(path, 0o600)


# =====================================================================
# Public API
# =====================================================================

def save_api_key(user_id: str, api_key: str) -> Dict[str, Any]:
    """
    Save a user's OANDA API key (encrypted).
    
    Returns:
        {"saved": True, "encryption": "fernet"|"fallback"}
    """
    encrypted = _encrypt(api_key, user_id)
    cred_path = _user_cred_path(user_id)
    
    with open(cred_path, "wb") as f:
        f.write(encrypted)
    os.chmod(cred_path, 0o600)
    
    # Update config timestamp
    config = _load_user_config(user_id)
    config["api_key_updated"] = datetime.now(timezone.utc).isoformat()
    config["has_api_key"] = True
    _save_user_config(user_id, config)
    
    return {"saved": True, "encryption": "fernet" if HAS_CRYPTO else "fallback"}


def get_api_key(user_id: str) -> Optional[str]:
    """
    Retrieve a user's decrypted OANDA API key.
    Returns None if not configured.
    """
    cred_path = _user_cred_path(user_id)
    if not os.path.exists(cred_path):
        return None
    
    with open(cred_path, "rb") as f:
        encrypted = f.read()
    
    return _decrypt(encrypted, user_id)


def validate_api_key(api_key: str) -> Dict[str, Any]:
    """
    Validate an OANDA API key by probing both endpoints.
    
    Returns:
        {
            "valid": True/False,
            "demo": {"available": True, "accounts": [...]},
            "live": {"available": True, "accounts": [...]},
        }
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    result = {"valid": False, "demo": {"available": False, "accounts": []}, 
              "live": {"available": False, "accounts": []}}
    
    # Check demo/practice
    try:
        r = requests.get(f"{PRACTICE_URL}/v3/accounts", headers=headers, timeout=10)
        if r.status_code == 200:
            accounts = r.json().get("accounts", [])
            result["demo"]["available"] = True
            # Get details for each account
            for acct in accounts:
                acct_id = acct.get("id", "")
                try:
                    r2 = requests.get(f"{PRACTICE_URL}/v3/accounts/{acct_id}", 
                                     headers=headers, timeout=10)
                    if r2.status_code == 200:
                        a = r2.json().get("account", {})
                        result["demo"]["accounts"].append({
                            "id": acct_id,
                            "balance": float(a.get("balance", 0)),
                            "currency": a.get("currency", "USD"),
                            "alias": a.get("alias", ""),
                            "tags": a.get("tags", []),
                            "open_trades": a.get("openTradeCount", 0),
                        })
                except Exception:
                    result["demo"]["accounts"].append({"id": acct_id, "balance": None})
            result["valid"] = True
    except Exception:
        pass
    
    # Check live
    try:
        r = requests.get(f"{LIVE_URL}/v3/accounts", headers=headers, timeout=10)
        if r.status_code == 200:
            accounts = r.json().get("accounts", [])
            result["live"]["available"] = True
            for acct in accounts:
                acct_id = acct.get("id", "")
                try:
                    r2 = requests.get(f"{LIVE_URL}/v3/accounts/{acct_id}", 
                                     headers=headers, timeout=10)
                    if r2.status_code == 200:
                        a = r2.json().get("account", {})
                        result["live"]["accounts"].append({
                            "id": acct_id,
                            "balance": float(a.get("balance", 0)),
                            "currency": a.get("currency", "USD"),
                            "alias": a.get("alias", ""),
                            "tags": a.get("tags", []),
                            "open_trades": a.get("openTradeCount", 0),
                        })
                except Exception:
                    result["live"]["accounts"].append({"id": acct_id, "balance": None})
            result["valid"] = True
    except Exception:
        pass
    
    return result


def setup_user_account(user_id: str, api_key: str, account_id: str, 
                       environment: str = "demo") -> Dict[str, Any]:
    """
    Full user onboarding: validate key, save encrypted, store account config.
    
    Args:
        user_id: Unique user identifier
        api_key: OANDA API key (raw)
        account_id: OANDA account ID to trade on
        environment: "demo" or "live"
    
    Returns:
        {
            "success": True/False,
            "environment": "demo"|"live",
            "account_id": "...",
            "balance": 2000.00,
            "validation": {...},
            "error": "..." (if failed)
        }
    """
    # Step 1: Validate key
    validation = validate_api_key(api_key)
    if not validation["valid"]:
        return {"success": False, "error": "API key invalid — no response from OANDA"}
    
    # Step 2: Verify the selected account exists in the right environment
    env_data = validation.get(environment, {})
    if not env_data.get("available"):
        return {"success": False, "error": f"Key not authorized for {environment} environment",
                "validation": validation}
    
    account_ids = [a["id"] for a in env_data.get("accounts", [])]
    if account_id not in account_ids:
        return {"success": False, 
                "error": f"Account {account_id} not found. Available: {account_ids}",
                "validation": validation}
    
    # Step 3: Get account details
    account_info = next(a for a in env_data["accounts"] if a["id"] == account_id)
    
    # Step 4: Save encrypted API key
    save_result = save_api_key(user_id, api_key)
    
    # Step 5: Save config
    config = _load_user_config(user_id)
    config.update({
        "user_id": user_id,
        "oanda_account_id": account_id,
        "environment": environment,
        "base_url": PRACTICE_URL if environment == "demo" else LIVE_URL,
        "account_currency": account_info.get("currency", "USD"),
        "account_alias": account_info.get("alias", ""),
        "setup_completed": datetime.now(timezone.utc).isoformat(),
        "has_api_key": True,
        "api_key_updated": datetime.now(timezone.utc).isoformat(),
        # Available accounts for switching later
        "available_accounts": {
            "demo": validation["demo"]["accounts"] if validation["demo"]["available"] else [],
            "live": validation["live"]["accounts"] if validation["live"]["available"] else [],
        },
    })
    _save_user_config(user_id, config)
    
    return {
        "success": True,
        "environment": environment,
        "account_id": account_id,
        "balance": account_info.get("balance"),
        "currency": account_info.get("currency"),
        "alias": account_info.get("alias"),
        "encryption": save_result.get("encryption"),
        "validation": validation,
    }


def switch_environment(user_id: str, environment: str, 
                       account_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Switch a user between demo and live.
    
    If account_id not specified, uses the first available account in that environment.
    Re-validates the key to get fresh account list.
    """
    api_key = get_api_key(user_id)
    if not api_key:
        return {"success": False, "error": "No API key configured"}
    
    config = _load_user_config(user_id)
    current_env = config.get("environment", "demo")
    
    if environment == current_env and not account_id:
        return {"success": True, "message": f"Already on {environment}", 
                "account_id": config.get("oanda_account_id")}
    
    # Re-validate to get fresh accounts
    validation = validate_api_key(api_key)
    env_data = validation.get(environment, {})
    
    if not env_data.get("available"):
        return {"success": False, 
                "error": f"Key not authorized for {environment}. "
                         f"Toggle to {environment} in your OANDA account settings first."}
    
    accounts = env_data.get("accounts", [])
    if not accounts:
        return {"success": False, "error": f"No accounts found for {environment}"}
    
    # Pick account
    if account_id:
        matching = [a for a in accounts if a["id"] == account_id]
        if not matching:
            return {"success": False, 
                    "error": f"Account {account_id} not found. Available: {[a['id'] for a in accounts]}"}
        account_info = matching[0]
    else:
        account_info = accounts[0]
    
    # Update config
    config.update({
        "oanda_account_id": account_info["id"],
        "environment": environment,
        "base_url": PRACTICE_URL if environment == "demo" else LIVE_URL,
        "account_currency": account_info.get("currency", "USD"),
        "account_alias": account_info.get("alias", ""),
        "environment_switched": datetime.now(timezone.utc).isoformat(),
        "available_accounts": {
            "demo": validation["demo"]["accounts"] if validation["demo"]["available"] else [],
            "live": validation["live"]["accounts"] if validation["live"]["available"] else [],
        },
    })
    _save_user_config(user_id, config)
    
    return {
        "success": True,
        "environment": environment,
        "account_id": account_info["id"],
        "balance": account_info.get("balance"),
        "currency": account_info.get("currency"),
        "warning": "⚠️ LIVE MODE — Real money at risk!" if environment == "live" else None,
    }


def get_user_connection(user_id: str) -> Dict[str, Any]:
    """
    Get the current user's OANDA connection details.
    Used by trading_cycle.py and wrappers to know which account/URL to use.
    
    Returns:
        {
            "configured": True/False,
            "api_key": "...",           # decrypted
            "account_id": "...",
            "environment": "demo"|"live",
            "base_url": "https://...",
        }
    """
    config = _load_user_config(user_id)
    if not config.get("has_api_key"):
        return {"configured": False}
    
    api_key = get_api_key(user_id)
    if not api_key:
        return {"configured": False, "error": "Key file missing"}
    
    return {
        "configured": True,
        "api_key": api_key,
        "account_id": config.get("oanda_account_id"),
        "environment": config.get("environment", "demo"),
        "base_url": config.get("base_url", PRACTICE_URL),
        "account_currency": config.get("account_currency", "USD"),
        "account_alias": config.get("account_alias", ""),
    }


def get_dashboard_status(user_id: str) -> Dict[str, Any]:
    """
    Get connection status for dashboard display (no secrets exposed).
    
    Returns:
        {
            "configured": True,
            "environment": "demo",
            "account_id": "101-001-24637237-001",
            "account_alias": "Primary",
            "key_last_4": "●●●●●●●●●●●●ab3f",
            "last_updated": "2026-02-17T...",
            "available_accounts": {...}
        }
    """
    config = _load_user_config(user_id)
    if not config.get("has_api_key"):
        return {"configured": False}
    
    # Get last 4 chars of key for display
    api_key = get_api_key(user_id)
    key_display = f"●●●●●●●●●●●●{api_key[-4:]}" if api_key else "not set"
    
    return {
        "configured": True,
        "environment": config.get("environment", "demo"),
        "account_id": config.get("oanda_account_id", ""),
        "account_alias": config.get("account_alias", ""),
        "account_currency": config.get("account_currency", "USD"),
        "key_display": key_display,
        "last_updated": config.get("api_key_updated", ""),
        "setup_completed": config.get("setup_completed", ""),
        "available_accounts": config.get("available_accounts", {}),
    }


def update_api_key(user_id: str, new_api_key: str) -> Dict[str, Any]:
    """
    Update a user's API key. Re-validates and re-saves everything.
    Preserves the current environment and account_id if still valid.
    """
    config = _load_user_config(user_id)
    current_env = config.get("environment", "demo")
    current_account = config.get("oanda_account_id", "")
    
    # Validate new key
    validation = validate_api_key(new_api_key)
    if not validation["valid"]:
        return {"success": False, "error": "New API key invalid"}
    
    # Try to keep same account
    env_data = validation.get(current_env, {})
    if env_data.get("available"):
        account_ids = [a["id"] for a in env_data.get("accounts", [])]
        if current_account in account_ids:
            # Same account still accessible
            return setup_user_account(user_id, new_api_key, current_account, current_env)
    
    # Account changed — user needs to re-select
    save_api_key(user_id, new_api_key)
    return {
        "success": True,
        "needs_account_selection": True,
        "validation": validation,
        "message": "Key saved. Please select your account.",
    }
