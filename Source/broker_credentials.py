"""
Broker credential management backed by v2/core.db.

Stores encrypted API keys in the broker_credentials table alongside
the existing users and user_sessions tables. One row per user+broker.

When we add new brokers (IBKR, etc.), it's just another row with a
different broker value — same table, same encryption, same API pattern.

Usage:
    from broker_credentials import BrokerCredentials

    bc = BrokerCredentials()  # connects to Database/v2/core.db
    
    # Validate a key before saving
    validation = bc.validate_oanda_key("your-api-key-here")
    
    # Save (validates, encrypts, stores)
    result = bc.connect(user_id=user_id, broker="oanda", api_key="...", 
                        account_id="101-001-...", environment="demo")
    
    # Get connection for trading (decrypts)
    conn = bc.get_connection(user_id=user_id, broker="oanda")
    # → {"api_key": "...", "account_id": "...", "base_url": "...", ...}
    
    # Dashboard display (no secrets)
    status = bc.get_status(user_id=user_id, broker="oanda")
    # → {"configured": True, "key_display": "●●●●●●6261", ...}
    
    # Switch environment
    bc.switch_environment(user_id=user_id, broker="oanda", environment="live")
    
    # Disconnect
    bc.disconnect(user_id=user_id, broker="oanda")
"""

import os
import json
import sqlite3
import logging
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from broker_crypto import encrypt_api_key, decrypt_api_key
from db_pool import get_core

logger = logging.getLogger(__name__)

# OANDA endpoints
OANDA_PRACTICE_URL = "https://api-fxpractice.oanda.com"
OANDA_LIVE_URL = "https://api-fxtrade.oanda.com"


class BrokerCredentials:
    """Manages broker API credentials in v2/core.db."""

    def __init__(self, db_path: str = None):
        self._custom_db_path = db_path  # Only used for testing
        self._ensure_table()

    def _get_conn(self) -> sqlite3.Connection:
        if self._custom_db_path:
            conn = sqlite3.connect(self._custom_db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        conn = get_core()
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn  # pooled — do NOT close

    def _ensure_table(self):
        """Create broker_credentials table if it doesn't exist."""
        conn = self._get_conn()
        conn.execute("""
        CREATE TABLE IF NOT EXISTS broker_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            broker TEXT NOT NULL DEFAULT 'oanda',
            encrypted_key BLOB NOT NULL,
            key_salt TEXT NOT NULL,
            account_id TEXT,
            environment TEXT DEFAULT 'demo',
            base_url TEXT,
            account_currency TEXT DEFAULT 'USD',
            account_alias TEXT,
            available_accounts TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, broker)
        )
        """)
        conn.commit()

    # ------------------------------------------------------------------
    # Broker-specific validation (OANDA first, add others later)
    # ------------------------------------------------------------------

    def validate_oanda_key(self, api_key: str) -> Dict[str, Any]:
        """
        Probe OANDA with an API key. Returns available accounts on both
        demo and live endpoints.

        Returns:
            {
                "valid": True/False,
                "demo": {"available": True, "accounts": [...]},
                "live": {"available": True, "accounts": [...]},
            }
        """
        headers = {"Authorization": f"Bearer {api_key}"}
        result = {
            "valid": False,
            "demo": {"available": False, "accounts": []},
            "live": {"available": False, "accounts": []},
        }

        for env, url in [("demo", OANDA_PRACTICE_URL), ("live", OANDA_LIVE_URL)]:
            try:
                r = requests.get(f"{url}/v3/accounts", headers=headers, timeout=10)
                if r.status_code == 200:
                    accounts = r.json().get("accounts", [])
                    result[env]["available"] = True
                    result["valid"] = True

                    for acct in accounts:
                        acct_id = acct.get("id", "")
                        try:
                            r2 = requests.get(
                                f"{url}/v3/accounts/{acct_id}",
                                headers=headers, timeout=10,
                            )
                            if r2.status_code == 200:
                                a = r2.json().get("account", {})
                                result[env]["accounts"].append({
                                    "id": acct_id,
                                    "balance": float(a.get("balance", 0)),
                                    "currency": a.get("currency", "USD"),
                                    "alias": a.get("alias", ""),
                                    "open_trades": a.get("openTradeCount", 0),
                                })
                            else:
                                result[env]["accounts"].append({
                                    "id": acct_id, "balance": None
                                })
                        except Exception:
                            result[env]["accounts"].append({
                                "id": acct_id, "balance": None
                            })
            except Exception:
                pass

        return result

    def validate_key(self, broker: str, api_key: str) -> Dict[str, Any]:
        """Validate an API key for any supported broker."""
        if broker == "oanda":
            return self.validate_oanda_key(api_key)
        else:
            return {"valid": False, "error": f"Unsupported broker: {broker}"}

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def connect(self, user_id: int, broker: str, api_key: str,
                account_id: str, environment: str = "demo") -> Dict[str, Any]:
        """
        Full broker onboarding: validate → encrypt → save.

        Args:
            user_id: users.id from v2/core.db
            broker: "oanda" (or future brokers)
            api_key: Raw API key
            account_id: Broker account ID to trade on
            environment: "demo" or "live"

        Returns:
            {"success": True, "account_id": ..., "balance": ..., ...}
        """
        # Validate
        validation = self.validate_key(broker, api_key)
        if not validation.get("valid"):
            return {"success": False, "error": "API key invalid — no response from broker"}

        # Find account across all endpoints
        all_accounts = {}
        actual_endpoint = {}
        for ep in ["demo", "live"]:
            for a in validation.get(ep, {}).get("accounts", []):
                aid = a.get("id")
                if aid and (aid not in all_accounts or a.get("balance") is not None):
                    all_accounts[aid] = a
                    actual_endpoint[aid] = ep

        if account_id not in all_accounts:
            return {
                "success": False,
                "error": f"Account {account_id} not found. Available: {list(all_accounts.keys())}",
                "validation": validation,
            }

        account_info = all_accounts[account_id]

        # Base URL from which endpoint actually has the account
        ep = actual_endpoint.get(account_id, "demo")
        if broker == "oanda":
            base_url = OANDA_PRACTICE_URL if ep == "demo" else OANDA_LIVE_URL
        else:
            base_url = ""

        # Encrypt
        ciphertext, salt = encrypt_api_key(user_id, broker, api_key)
        now = datetime.now(timezone.utc).isoformat()

        # Upsert into DB
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO broker_credentials 
                (user_id, broker, encrypted_key, key_salt, account_id, environment,
                 base_url, account_currency, account_alias, available_accounts,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, broker) DO UPDATE SET
                encrypted_key = excluded.encrypted_key,
                key_salt = excluded.key_salt,
                account_id = excluded.account_id,
                environment = excluded.environment,
                base_url = excluded.base_url,
                account_currency = excluded.account_currency,
                account_alias = excluded.account_alias,
                available_accounts = excluded.available_accounts,
                updated_at = excluded.updated_at
        """, (
            user_id, broker, ciphertext, salt, account_id, environment,
            base_url, account_info.get("currency", "USD"),
            account_info.get("alias", ""),
            json.dumps({"demo": validation["demo"]["accounts"],
                        "live": validation["live"]["accounts"]}),
            now, now,
        ))
        conn.commit()

        logger.info(f"Broker credentials saved: user={user_id} broker={broker} "
                     f"env={environment} account={account_id}")

        return {
            "success": True,
            "environment": environment,
            "account_id": account_id,
            "balance": account_info.get("balance"),
            "currency": account_info.get("currency"),
            "alias": account_info.get("alias"),
            "encryption": "fernet" if True else "fallback",  # broker_crypto handles this
        }

    def get_connection(self, user_id: int, broker: str = "oanda") -> Dict[str, Any]:
        """
        Get decrypted connection details for trading.
        Called by trading_cycle.py / wrappers.py.

        Returns:
            {"configured": True, "api_key": "...", "account_id": "...", 
             "base_url": "...", "environment": "...", ...}
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM broker_credentials WHERE user_id = ? AND broker = ?",
            (user_id, broker),
        ).fetchone()

        if not row:
            return {"configured": False}

        try:
            api_key = decrypt_api_key(
                user_id, broker,
                row["encrypted_key"], row["key_salt"],
            )
        except Exception as e:
            logger.error(f"Failed to decrypt credentials for user={user_id} broker={broker}: {e}")
            return {"configured": False, "error": "Decryption failed"}

        return {
            "configured": True,
            "api_key": api_key,
            "account_id": row["account_id"],
            "environment": row["environment"],
            "base_url": row["base_url"],
            "account_currency": row["account_currency"],
            "account_alias": row["account_alias"],
        }

    def get_status(self, user_id: int, broker: str = "oanda") -> Dict[str, Any]:
        """
        Get display-safe broker status for dashboard. No secrets exposed.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM broker_credentials WHERE user_id = ? AND broker = ?",
            (user_id, broker),
        ).fetchone()

        if not row:
            return {"configured": False}

        # Mask the key: decrypt just to get last 4 chars
        try:
            api_key = decrypt_api_key(
                user_id, broker,
                row["encrypted_key"], row["key_salt"],
            )
            key_display = f"●●●●●●●●{api_key[-4:]}"
        except Exception:
            key_display = "●●●●●●●●????"

        # Parse available accounts
        available = {}
        if row["available_accounts"]:
            try:
                available = json.loads(row["available_accounts"])
            except json.JSONDecodeError:
                pass

        return {
            "configured": True,
            "broker": broker,
            "environment": row["environment"],
            "account_id": row["account_id"],
            "account_alias": row["account_alias"],
            "account_currency": row["account_currency"],
            "key_display": key_display,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "available_accounts": available,
        }

    def switch_environment(self, user_id: int, broker: str,
                           environment: str, account_id: str = None) -> Dict[str, Any]:
        """
        Switch account and/or environment label.

        The environment is user-assigned (demo or live). The account_id
        determines which broker account to trade on. We search all
        available accounts across both endpoints to find the match.
        """
        current = self.get_connection(user_id, broker)
        if not current.get("configured"):
            return {"success": False, "error": "No broker configured"}

        api_key = current["api_key"]

        # Re-validate to get fresh account list from all endpoints
        validation = self.validate_key(broker, api_key)

        # Flatten all accounts (dedupe by ID, prefer the one with balance info)
        all_accounts = {}
        actual_endpoint = {}  # track which endpoint each account lives on
        for ep in ["demo", "live"]:
            for a in validation.get(ep, {}).get("accounts", []):
                aid = a.get("id")
                if not aid:
                    continue
                if aid not in all_accounts or a.get("balance") is not None:
                    all_accounts[aid] = a
                    actual_endpoint[aid] = ep

        if not all_accounts:
            return {"success": False, "error": "No accounts found"}

        # Pick account
        if account_id:
            if account_id not in all_accounts:
                return {
                    "success": False,
                    "error": f"Account {account_id} not found. "
                             f"Available: {list(all_accounts.keys())}",
                }
            account_info = all_accounts[account_id]
        else:
            account_info = next(iter(all_accounts.values()))
            account_id = account_info["id"]

        # Base URL comes from which endpoint actually has the account
        ep = actual_endpoint.get(account_id, "demo")
        if broker == "oanda":
            base_url = OANDA_PRACTICE_URL if ep == "demo" else OANDA_LIVE_URL
        else:
            base_url = ""

        now = datetime.now(timezone.utc).isoformat()

        conn = self._get_conn()
        conn.execute("""
            UPDATE broker_credentials
            SET account_id = ?, environment = ?, base_url = ?,
                account_currency = ?, account_alias = ?,
                available_accounts = ?, updated_at = ?
            WHERE user_id = ? AND broker = ?
        """, (
            account_id, environment, base_url,
            account_info.get("currency", "USD"),
            account_info.get("alias", ""),
            json.dumps({"demo": validation["demo"]["accounts"],
                        "live": validation["live"]["accounts"]}),
            now, user_id, broker,
        ))
        conn.commit()

        return {
            "success": True,
            "environment": environment,
            "account_id": account_id,
            "balance": account_info.get("balance"),
            "currency": account_info.get("currency"),
            "warning": "⚠️ LIVE MODE — Real money at risk!" if environment == "live" else None,
        }

    def disconnect(self, user_id: int, broker: str) -> Dict[str, Any]:
        """Remove broker credentials for a user."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM broker_credentials WHERE user_id = ? AND broker = ?",
            (user_id, broker),
        )
        conn.commit()

        if cursor.rowcount > 0:
            logger.info(f"Broker credentials removed: user={user_id} broker={broker}")
            return {"success": True}
        else:
            return {"success": False, "error": "No credentials found"}

    def get_all_configured_users(self, broker: str = "oanda") -> List[Dict[str, Any]]:
        """
        Get all users with configured broker credentials.
        Used by multi-user cycle scheduler.
        """
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT bc.user_id, bc.account_id, bc.environment, bc.account_alias,
                      u.username, u.display_name
               FROM broker_credentials bc
               JOIN users u ON bc.user_id = u.id
               WHERE bc.broker = ?""",
            (broker,),
        ).fetchall()

        return [dict(r) for r in rows]
