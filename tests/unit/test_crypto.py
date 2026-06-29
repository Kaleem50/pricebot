"""
tests/unit/test_crypto.py — AES-256-GCM Credential Encryption Tests

Tests for core/crypto.py:
  - Round-trip: encrypt → decrypt returns original plaintext
  - Tampered ciphertext raises InvalidTag
  - Different nonce each call (two encryptions of same plaintext differ)
  - Empty string raises ValueError
  - Key length validation
  - Missing env var raises RuntimeError
"""

from __future__ import annotations

import base64
import os
from unittest.mock import patch

import pytest
from cryptography.exceptions import InvalidTag

from core.crypto import decrypt_credential, encrypt_credential

# 32-byte test key as 64 hex chars (NOT a real key — test use only)
_TEST_KEY_HEX = "a" * 64


def _with_test_key(func):
    """Decorator to inject CREDENTIAL_ENCRYPTION_KEY for a test function."""
    def wrapper(*args, **kwargs):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


class TestEncryptRoundTrip:
    """Encrypt → decrypt produces the original plaintext."""

    def test_simple_string(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            plaintext = "hello world"
            encrypted = encrypt_credential(plaintext)
            assert decrypt_credential(encrypted) == plaintext

    def test_json_credentials(self):
        import json
        creds = {
            "refresh_token": "Atzr|IwEB...",
            "client_id": "amzn1.application-oa2-client.abc",
            "client_secret": "secret123",
            "marketplace_id": "ATVPDKIKX0DER",
            "merchant_id": "MERCHANT_001",
        }
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            plaintext = json.dumps(creds)
            encrypted = encrypt_credential(plaintext)
            result = json.loads(decrypt_credential(encrypted))
        assert result == creds

    def test_unicode_plaintext(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            plaintext = "Ünïcödé têxt with spécial chars: £€¥"
            encrypted = encrypt_credential(plaintext)
            assert decrypt_credential(encrypted) == plaintext

    def test_long_plaintext(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            plaintext = "x" * 10_000
            encrypted = encrypt_credential(plaintext)
            assert decrypt_credential(encrypted) == plaintext

    def test_single_char(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            encrypted = encrypt_credential("a")
            assert decrypt_credential(encrypted) == "a"


class TestTampering:
    """Tampered ciphertext always raises InvalidTag."""

    def test_flip_one_bit_in_ciphertext(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            encrypted = encrypt_credential("sensitive data")
            blob = bytearray(base64.b64decode(encrypted))
            # Flip a byte in the ciphertext (after the 12-byte nonce)
            blob[12] ^= 0xFF
            tampered = base64.b64encode(bytes(blob)).decode()
            with pytest.raises(InvalidTag):
                decrypt_credential(tampered)

    def test_truncated_ciphertext(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            encrypted = encrypt_credential("test")
            blob = bytearray(base64.b64decode(encrypted))
            # Remove last byte (truncates auth tag)
            truncated = base64.b64encode(bytes(blob[:-1])).decode()
            with pytest.raises((InvalidTag, ValueError)):
                decrypt_credential(truncated)

    def test_wrong_key_raises_invalid_tag(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            encrypted = encrypt_credential("test data")

        wrong_key = "b" * 64
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": wrong_key}):
            with pytest.raises(InvalidTag):
                decrypt_credential(encrypted)

    def test_random_bytes_raises_invalid_tag_or_value_error(self):
        import secrets
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            # 30 random bytes is too short (< 12 nonce + 1 ciphertext)
            short_blob = base64.b64encode(secrets.token_bytes(5)).decode()
            with pytest.raises((ValueError, InvalidTag)):
                decrypt_credential(short_blob)


class TestNonceUniqueness:
    """Each encrypt call uses a fresh nonce — same plaintext produces different ciphertext."""

    def test_two_encryptions_differ(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            plaintext = "same plaintext every time"
            enc1 = encrypt_credential(plaintext)
            enc2 = encrypt_credential(plaintext)
        assert enc1 != enc2, "Same plaintext must not produce identical ciphertext (nonce must differ)"

    def test_hundred_encryptions_all_unique(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            results = {encrypt_credential("fixed") for _ in range(100)}
        assert len(results) == 100, "All 100 encryptions must be unique"


class TestInputValidation:
    """Invalid inputs are rejected early with clear errors."""

    def test_empty_string_raises_value_error(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            with pytest.raises(ValueError, match="must not be empty"):
                encrypt_credential("")

    def test_missing_env_var_raises_runtime_error(self):
        env = os.environ.copy()
        env.pop("CREDENTIAL_ENCRYPTION_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEY"):
                encrypt_credential("test")

    def test_wrong_key_length_raises_runtime_error(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": "abcd"}):
            with pytest.raises(RuntimeError, match="64 hex chars"):
                encrypt_credential("test")

    def test_non_hex_key_raises_runtime_error(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": "z" * 64}):
            with pytest.raises(RuntimeError, match="not valid hex"):
                encrypt_credential("test")

    def test_invalid_base64_decrypt_raises_value_error(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            with pytest.raises(ValueError, match="not valid base64"):
                decrypt_credential("this is not base64!!!")

    def test_too_short_blob_raises_value_error(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            # Only 5 bytes — too short for 12-byte nonce
            short = base64.b64encode(b"short").decode()
            with pytest.raises(ValueError, match="too short"):
                decrypt_credential(short)


class TestOutputFormat:
    """Encrypted output is a valid base64 string."""

    def test_output_is_valid_base64(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            encrypted = encrypt_credential("test")
        # Should not raise
        decoded = base64.b64decode(encrypted)
        # Must be at least nonce (12) + GCM tag (16) + 1 byte ciphertext = 29 bytes
        assert len(decoded) >= 29

    def test_output_is_ascii_string(self):
        with patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION_KEY": _TEST_KEY_HEX}):
            encrypted = encrypt_credential("test")
        assert isinstance(encrypted, str)
        encrypted.encode("ascii")  # Would raise if non-ASCII
