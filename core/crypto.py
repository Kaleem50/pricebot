"""
core/crypto.py — AES-256-GCM Credential Encryption

Encrypts and decrypts platform API credentials before storage in Supabase.
Credentials are encrypted before any DB write and decrypted in memory at
runtime only.  The plaintext never appears in logs, DB responses, or API
responses.

Security spec (SECURITY.md §3.2):
  - Algorithm:    AES-256-GCM (256-bit key, 12-byte nonce, 16-byte auth tag)
  - Key source:   CREDENTIAL_ENCRYPTION_KEY env var (64-char hex string)
  - Nonce:        os.urandom(12) — fresh for every encryption call
  - Storage:      base64( nonce[12] | ciphertext+tag ) stored as TEXT in DB
  - Tamper check: InvalidTag raised automatically by AESGCM.decrypt()

The key must be exactly 32 bytes (64 hex chars).  Generate with:
    python -c "import secrets; print(secrets.token_hex(32))"
"""

from __future__ import annotations

import base64
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# Re-export so callers can catch this without importing cryptography directly.
__all__ = ["encrypt_credential", "decrypt_credential", "InvalidTag"]

_KEY_ENV_VAR = "CREDENTIAL_ENCRYPTION_KEY"
_NONCE_BYTES = 12
_KEY_BYTES = 32


def _load_key() -> bytes:
    """
    Load and validate the AES key from the environment.

    Returns:
        32-byte key derived from the hex-encoded env var.

    Raises:
        RuntimeError: If the env var is missing or the wrong length.
    """
    hex_key = os.environ.get(_KEY_ENV_VAR, "").strip()
    if not hex_key:
        raise RuntimeError(
            f"{_KEY_ENV_VAR} is not set. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    try:
        key = bytes.fromhex(hex_key)
    except ValueError as exc:
        raise RuntimeError(f"{_KEY_ENV_VAR} is not valid hex: {exc}") from exc
    if len(key) != _KEY_BYTES:
        raise RuntimeError(
            f"{_KEY_ENV_VAR} must be exactly 64 hex chars (32 bytes). "
            f"Got {len(key)} bytes."
        )
    return key


def encrypt_credential(plaintext: str) -> str:
    """
    Encrypt a plaintext string with AES-256-GCM.

    A fresh 12-byte nonce is generated for every call, so encrypting the
    same plaintext twice produces different ciphertext.  This is intentional
    and required by GCM's security model.

    Args:
        plaintext: The credential string to encrypt (e.g. a JSON-serialised
                   credentials dict).  Must not be empty.

    Returns:
        base64-encoded string of ``nonce[12] || ciphertext+tag``.
        Safe to store in a TEXT column.

    Raises:
        RuntimeError:  If CREDENTIAL_ENCRYPTION_KEY is missing or invalid.
        ValueError:    If plaintext is empty.
    """
    if not plaintext:
        raise ValueError("plaintext must not be empty")

    key = _load_key()
    nonce = os.urandom(_NONCE_BYTES)
    aesgcm = AESGCM(key)
    # AESGCM.encrypt() appends the 16-byte GCM authentication tag to the ciphertext
    ciphertext_with_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    blob = nonce + ciphertext_with_tag
    return base64.b64encode(blob).decode("ascii")


def decrypt_credential(encrypted_b64: str) -> str:
    """
    Decrypt a credential string previously produced by encrypt_credential().

    Args:
        encrypted_b64: base64-encoded ``nonce || ciphertext+tag`` as returned
                       by encrypt_credential().

    Returns:
        The original plaintext string.

    Raises:
        RuntimeError:                   If CREDENTIAL_ENCRYPTION_KEY is missing or invalid.
        cryptography.exceptions.InvalidTag: If the ciphertext has been tampered with
                                        or the key does not match.  The caller must treat
                                        this as a CRITICAL security event.
        ValueError:                     If the blob is too short to contain a valid nonce.
    """
    key = _load_key()
    try:
        blob = base64.b64decode(encrypted_b64)
    except Exception as exc:
        raise ValueError(f"encrypted_b64 is not valid base64: {exc}") from exc

    if len(blob) <= _NONCE_BYTES:
        raise ValueError(
            f"Encrypted blob is too short ({len(blob)} bytes). "
            f"Expected at least {_NONCE_BYTES + 1} bytes."
        )

    nonce = blob[:_NONCE_BYTES]
    ciphertext_with_tag = blob[_NONCE_BYTES:]
    aesgcm = AESGCM(key)
    # Raises cryptography.exceptions.InvalidTag if ciphertext is tampered or key is wrong
    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext_with_tag, None)
    return plaintext_bytes.decode("utf-8")
