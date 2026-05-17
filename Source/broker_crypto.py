"""
Broker credential encryption for Forex Trading Team.

Provides Fernet-based encryption for broker API keys stored in v2/core.db.
Each user+broker combo gets a unique derived key from a master key.

Master key stored at Database/.broker_master.key (0o600 permissions).
Generated once on first use. If lost, all encrypted keys become unreadable
(users must re-enter their API keys).

Usage:
    from broker_crypto import encrypt_api_key, decrypt_api_key

    ciphertext, salt = encrypt_api_key(user_id=<your_user_id>, broker="oanda", plaintext="your-api-key")
    plaintext = decrypt_api_key(user_id=<your_user_id>, broker="oanda", ciphertext=ciphertext, salt=salt)
"""

import os
import hashlib
import base64
import secrets
from typing import Tuple

# Paths
_DB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "Database"
)
_MASTER_KEY_PATH = os.path.join(_DB_DIR, ".broker_master.key")

# Try Fernet (preferred), fall back to HMAC-XOR (still file-permission guarded)
try:
    from cryptography.fernet import Fernet, InvalidToken
    HAS_FERNET = True
except ImportError:
    HAS_FERNET = False

    class InvalidToken(Exception):
        pass


def _get_master_key() -> bytes:
    """Get or create the master encryption key. Created once, read thereafter."""
    if os.path.exists(_MASTER_KEY_PATH):
        with open(_MASTER_KEY_PATH, "rb") as f:
            return f.read().strip()

    # Generate
    if HAS_FERNET:
        key = Fernet.generate_key()
    else:
        key = base64.urlsafe_b64encode(secrets.token_bytes(32))

    os.makedirs(os.path.dirname(_MASTER_KEY_PATH), exist_ok=True)
    with open(_MASTER_KEY_PATH, "wb") as f:
        f.write(key)
    os.chmod(_MASTER_KEY_PATH, 0o600)

    return key


def _derive_key(user_id: int, broker: str, salt: str) -> bytes:
    """
    Derive a per-user, per-broker encryption key.

    Uses HKDF-like construction: SHA256(master || user_id || broker || salt)
    truncated to 32 bytes → base64 → Fernet-compatible key.
    """
    master = _get_master_key()
    raw = hashlib.sha256(
        master + f"{user_id}:{broker}:{salt}".encode()
    ).digest()
    return base64.urlsafe_b64encode(raw)


def encrypt_api_key(user_id: int, broker: str, plaintext: str) -> Tuple[bytes, str]:
    """
    Encrypt a broker API key.

    Args:
        user_id: Integer user ID from v2/core.db
        broker: Broker identifier (e.g., "oanda", "ibkr")
        plaintext: The raw API key

    Returns:
        (ciphertext_bytes, salt_string) — both must be stored in DB.
    """
    salt = secrets.token_hex(16)  # 32-char random salt
    key = _derive_key(user_id, broker, salt)

    if HAS_FERNET:
        f = Fernet(key)
        ciphertext = f.encrypt(plaintext.encode())
    else:
        # Fallback: XOR with derived key (file permissions are the real guard)
        key_bytes = base64.urlsafe_b64decode(key)
        encrypted = bytes(
            a ^ key_bytes[i % len(key_bytes)]
            for i, a in enumerate(plaintext.encode())
        )
        ciphertext = base64.urlsafe_b64encode(encrypted)

    return ciphertext, salt


def decrypt_api_key(user_id: int, broker: str, ciphertext: bytes, salt: str) -> str:
    """
    Decrypt a broker API key.

    Args:
        user_id: Integer user ID from v2/core.db
        broker: Broker identifier
        ciphertext: Encrypted bytes from DB
        salt: Salt string from DB

    Returns:
        The raw API key string.

    Raises:
        InvalidToken: If decryption fails (wrong key, corrupted data).
    """
    key = _derive_key(user_id, broker, salt)

    if HAS_FERNET:
        f = Fernet(key)
        return f.decrypt(ciphertext).decode()
    else:
        key_bytes = base64.urlsafe_b64decode(key)
        decoded = base64.urlsafe_b64decode(ciphertext)
        decrypted = bytes(
            a ^ key_bytes[i % len(key_bytes)]
            for i, a in enumerate(decoded)
        )
        return decrypted.decode()


def verify_encryption_available() -> dict:
    """Check encryption status for diagnostics."""
    master_exists = os.path.exists(_MASTER_KEY_PATH)
    return {
        "fernet_available": HAS_FERNET,
        "master_key_exists": master_exists,
        "master_key_path": _MASTER_KEY_PATH,
        "encryption_method": "fernet" if HAS_FERNET else "xor_fallback",
    }
